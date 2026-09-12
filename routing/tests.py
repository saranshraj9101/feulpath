import json
import tempfile
from pathlib import Path
from unittest import mock

import numpy as np
from django.core.cache import cache
from django.test import SimpleTestCase, override_settings

from routing import cities, services
from routing.optimizer import UnreachableRoute, plan_fuel_stops
from routing.services import (
    LocationError,
    ORSAuthError,
    ORSRateLimited,
    RouteNotFound,
    RouteTooLong,
    build_station_index,
    densify,
    geocode,
    get_route,
    simplify_line,
    stations_along_route,
)

FAKE_CITIES = {
    ("CHICAGO", "IL"): (41.85, -87.65),
    ("ANCHORAGE", "AK"): (61.2181, -149.9003),
    ("DENVER", "CO"): (39.74, -104.99),
}


@mock.patch.object(cities, "_cities", FAKE_CITIES)
class GeocodeTests(SimpleTestCase):
    def test_lat_lng(self):
        self.assertEqual(geocode(" 41.88, -87.63 "), (41.88, -87.63))

    def test_city_state_case_and_whitespace_insensitive(self):
        self.assertEqual(geocode("  chicago ,  il "), (41.85, -87.65))

    def test_alaska_allowed(self):
        self.assertEqual(geocode("Anchorage, AK"), (61.2181, -149.9003))

    def test_hawaii_allowed(self):
        self.assertEqual(geocode("21.3,-157.85"), (21.3, -157.85))

    def test_unknown_city(self):
        with self.assertRaisesMessage(LocationError, "Unknown US city"):
            geocode("Atlantis, ZZ")

    def test_unparseable(self):
        for bad in ["", "Chicago", "1600 Pennsylvania Ave", "Chicago, Illinois", "41.8"]:
            with self.subTest(bad=bad), self.assertRaisesMessage(LocationError, '"City, ST"'):
                geocode(bad)

    def test_outside_usa(self):
        for bad in ["51.5,-0.12", "19.43,-99.13", "23.11,-82.37"]:  # London, Mexico City, Havana
            with self.subTest(bad=bad), self.assertRaisesMessage(LocationError, "outside the USA"):
                geocode(bad)


def _response(status: int, payload: dict) -> mock.Mock:
    resp = mock.Mock(status_code=status, reason="Error")
    resp.json.return_value = payload
    return resp


ORS_OK = {
    "features": [
        {
            "geometry": {"coordinates": [[-87.65, 41.85], [-86.9, 40.8], [-86.15, 39.77]]},
            "properties": {"summary": {"distance": 295000.0, "duration": 11000.0}},
        }
    ]
}


@override_settings(ORS_API_KEY="test-key", ORS_BASE_URL="https://ors.test")
class GetRouteTests(SimpleTestCase):
    def test_single_post_with_lng_lat_order(self):
        with mock.patch.object(services._session, "post", return_value=_response(200, ORS_OK)) as post:
            route = get_route((41.85, -87.65), (39.77, -86.15))

        post.assert_called_once()
        args, kwargs = post.call_args
        self.assertEqual(args[0], "https://ors.test/v2/directions/driving-hgv/geojson")
        self.assertEqual(kwargs["json"]["coordinates"], [[-87.65, 41.85], [-86.15, 39.77]])
        self.assertEqual(kwargs["headers"]["Authorization"], "test-key")
        self.assertEqual(kwargs["timeout"], 15)

        self.assertEqual(route.coords[0], (41.85, -87.65))  # converted back to (lat, lng)
        self.assertEqual(len(route.coords), 3)
        self.assertAlmostEqual(route.distance_miles, 183.30, places=2)
        self.assertEqual(route.duration_seconds, 11000.0)

    def test_error_mapping_and_no_retry(self):
        cases = [
            (_response(403, {"error": "Access to this API has been disallowed"}), ORSAuthError),
            (_response(429, {"error": "Rate limit exceeded"}), ORSRateLimited),
            (_response(404, {"error": {"code": 2009, "message": "Route could not be found"}}), RouteNotFound),
            (_response(404, {"error": {"code": 2010, "message": "No routable point"}}), RouteNotFound),
            (_response(400, {"error": {"code": 2004, "message": "Distance limit"}}), RouteTooLong),
        ]
        for resp, exc in cases:
            with self.subTest(exc=exc.__name__):
                with mock.patch.object(services._session, "post", return_value=resp) as post:
                    with self.assertRaises(exc):
                        get_route((41.85, -87.65), (39.77, -86.15))
                post.assert_called_once()

    @override_settings(ORS_API_KEY="")
    def test_missing_key_makes_no_call(self):
        with mock.patch.object(services._session, "post") as post:
            with self.assertRaises(ORSAuthError):
                get_route((41.85, -87.65), (39.77, -86.15))
        post.assert_not_called()


class CityDuplicateTests(SimpleTestCase):
    def test_nearby_duplicates_averaged_far_duplicates_keep_first(self):
        csv_text = (
            "ID,STATE_CODE,STATE_NAME,CITY,COUNTY,LATITUDE,LONGITUDE\n"
            "1,IN,Indiana,Indianapolis,Hamilton,39.938417,-86.13894\n"
            "2,IN,Indiana,Indianapolis,Marion,39.775006,-86.109348\n"
            "3,MO,Missouri,Springfield,Greene,37.2,-93.3\n"
            "4,MO,Missouri,Springfield,Far,39.2,-90.3\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "us_cities.csv"
            path.write_text(csv_text, encoding="utf-8")
            with mock.patch.object(cities, "CITIES_PATH", path), mock.patch.object(cities, "_cities", None):
                indy = cities.lookup("Indianapolis", "IN")
                springfield = cities.lookup("Springfield", "MO")
        self.assertAlmostEqual(indy[0], (39.938417 + 39.775006) / 2)
        self.assertAlmostEqual(indy[1], (-86.13894 + -86.109348) / 2)
        self.assertEqual(springfield, (37.2, -93.3))


def _station(opis_id: int, lat: float, lng: float, price: float = 3.0) -> dict:
    return {"opis_id": opis_id, "name": f"S{opis_id}", "address": "", "city": "", "state": "",
            "lat": lat, "lng": lng, "price": price}


class StationsAlongRouteTests(SimpleTestCase):
    # Straight east-west route along latitude 40, about 265 miles long, given as 2 points.
    ROUTE = [(40.0, -100.0), (40.0, -95.0)]

    def test_densify_caps_step_and_keeps_length(self):
        lat, lng, cumulative = densify(self.ROUTE)
        steps = np.diff(cumulative)
        self.assertLessEqual(steps.max(), 1.0 + 1e-9)
        self.assertGreater(len(lat), 260)
        self.assertAlmostEqual(cumulative[-1], 265.3, delta=1.0)

    def test_corridor_filter_and_mile_markers(self):
        index = build_station_index([
            _station(1, 40.05, -97.5),  # ~3.5 mi off route, mid-way
            _station(2, 41.0, -97.5),  # ~69 mi off route: excluded
            _station(3, 40.0, -99.5),  # on route near the start
            _station(4, 40.1, -96.0),  # ~7 mi off route, near the end
            _station(5, 40.0, -90.0),  # beyond the end of the route: excluded
        ])
        with mock.patch.object(services, "_station_index", index):
            found = stations_along_route(self.ROUTE, corridor_miles=10)

        self.assertEqual([s["opis_id"] for s in found], [3, 1, 4])
        for s in found:
            expected = float(cities.haversine_miles(40.0, -100.0, 40.0, s["lng"]))
            self.assertAlmostEqual(s["mile_marker"], expected, delta=1.0)

    def test_narrower_corridor_drops_offset_station(self):
        index = build_station_index([_station(1, 40.05, -97.5), _station(4, 40.1, -96.0)])
        with mock.patch.object(services, "_station_index", index):
            found = stations_along_route(self.ROUTE, corridor_miles=5)
        self.assertEqual([s["opis_id"] for s in found], [1])


class SimplifyLineTests(SimpleTestCase):
    def test_collinear_points_collapse_to_endpoints(self):
        line = [(40.0, -100.0 + i * 0.01) for i in range(101)]
        self.assertEqual(simplify_line(line, 0.001).tolist(), [[40.0, -100.0], [40.0, -99.0]])

    def test_corner_beyond_tolerance_is_kept_small_wiggle_is_not(self):
        line = [(40.0, -100.0), (40.0002, -99.5), (40.0, -99.0), (41.0, -99.0)]
        self.assertEqual(simplify_line(line, 0.001).tolist(), [[40.0, -100.0], [40.0, -99.0], [41.0, -99.0]])

    def test_zero_tolerance_returns_every_point(self):
        line = [(40.0, -100.0), (40.0002, -99.5), (40.0, -99.0)]
        self.assertEqual(len(simplify_line(line, 0)), 3)


def _stop(mile: float, price: float, opis_id: int | None = None) -> dict:
    return {"opis_id": opis_id if opis_id is not None else int(mile), "mile_marker": mile, "price": price}


class PlanFuelStopsTests(SimpleTestCase):
    def assertTotalsConsistent(self, plan: dict):
        self.assertAlmostEqual(plan["total_gallons"], sum(s["gallons"] for s in plan["stops"]))
        self.assertAlmostEqual(plan["total_cost"], sum(s["cost"] for s in plan["stops"]))
        for s in plan["stops"]:
            self.assertGreater(s["gallons"], 0)
            self.assertAlmostEqual(s["cost"], s["gallons"] * s["price"])

    def test_short_trip_needs_no_stops(self):
        plan = plan_fuel_stops([_stop(100, 3.0), _stop(250, 2.5)], total_miles=300)
        self.assertEqual(plan["stops"], [])
        for key in ("total_gallons", "total_cost"):
            self.assertIsInstance(plan[key], float)
            self.assertEqual(plan[key], 0.0)
            self.assertIsInstance(round(plan[key], 2), float)  # as serialized by services

    def test_cheaper_station_ahead_causes_partial_purchase(self):
        # Reach A (mile 400) with 100 mi left; B (mile 600) is cheaper, so buy only 100 mi at A.
        plan = plan_fuel_stops([_stop(400, 4.0), _stop(600, 3.0)], total_miles=900)
        stops = [(s["mile_marker"], s["gallons"], s["cost"]) for s in plan["stops"]]
        self.assertEqual(len(stops), 2)
        self.assertEqual(stops[0][0], 400)
        self.assertAlmostEqual(stops[0][1], 10.0)  # partial: not a full 50-gallon fill
        self.assertAlmostEqual(stops[0][2], 40.0)
        self.assertEqual(stops[1][0], 600)
        self.assertAlmostEqual(stops[1][1], 30.0)  # just enough for the last 300 miles
        self.assertAlmostEqual(plan["total_cost"], 130.0)
        self.assertTotalsConsistent(plan)

    def test_fill_up_then_cheapest_in_window(self):
        # At A (cheapest, $3.00) nothing ahead is cheaper and the end is 750 mi away:
        # fill up, then go to the cheapest station in range (B, not C).
        plan = plan_fuel_stops([_stop(450, 3.0), _stop(700, 3.5), _stop(900, 4.0)], total_miles=1200)
        stops = [(s["mile_marker"], s["gallons"]) for s in plan["stops"]]
        self.assertEqual(len(stops), 2)
        self.assertEqual(stops[0][0], 450)
        self.assertAlmostEqual(stops[0][1], 45.0)  # arrived with 50 mi, filled to 500
        self.assertEqual(stops[1][0], 700)
        self.assertAlmostEqual(stops[1][1], 25.0)  # C is never used
        self.assertAlmostEqual(plan["total_gallons"], 70.0)  # (1200 - 500 starting miles) / 10 mpg
        self.assertAlmostEqual(plan["total_cost"], 45 * 3.0 + 25 * 3.5)
        self.assertTotalsConsistent(plan)

    def test_gap_longer_than_range_is_unreachable(self):
        with self.assertRaises(UnreachableRoute) as ctx:
            plan_fuel_stops([_stop(400, 3.0), _stop(1000, 3.0)], total_miles=1500)
        self.assertEqual(ctx.exception.gap_start_mile, 400)
        self.assertIn("mile 400.0", str(ctx.exception))

    def test_no_stations_and_destination_out_of_range(self):
        with self.assertRaises(UnreachableRoute) as ctx:
            plan_fuel_stops([], total_miles=600)
        self.assertEqual(ctx.exception.gap_start_mile, 0)

    def test_same_mile_picks_cheapest_and_ignores_stations_past_destination(self):
        plan = plan_fuel_stops(
            [_stop(300, 3.9, opis_id=1), _stop(300, 3.1, opis_id=2), _stop(1000, 1.0, opis_id=3)],
            total_miles=700,
        )
        self.assertEqual([s["opis_id"] for s in plan["stops"]], [2])
        self.assertAlmostEqual(plan["stops"][0]["gallons"], 20.0)  # 400 mi left minus 200 in the tank

    def test_totals_equal_sum_of_stops_on_long_random_route(self):
        rng = np.random.default_rng(42)
        miles = np.sort(rng.uniform(1, 2800, 300))
        stations = [_stop(float(m), float(p), opis_id=i)
                    for i, (m, p) in enumerate(zip(miles, rng.uniform(2.8, 4.5, 300)))]
        plan = plan_fuel_stops(stations, total_miles=2800)
        self.assertTotalsConsistent(plan)
        # Starting with 500 mi, at least (2800 - 500) / 10 gallons must be bought.
        self.assertGreaterEqual(plan["total_gallons"], 230 - 1e-6)
        # ...and never more than that plus one tank (can't arrive holding more than a full tank).
        self.assertLessEqual(plan["total_gallons"], 230 + 50 + 1e-6)
        self.assertEqual([s["mile_marker"] for s in plan["stops"]],
                         sorted(s["mile_marker"] for s in plan["stops"]))


# A real ORS driving-hgv response (Chicago, IL -> Denver, CO, ~1002 mi) so view tests need no network.
ORS_FIXTURE = Path(__file__).parent / "testdata" / "ors_chicago_denver.json"


@override_settings(ORS_API_KEY="test-key")
class RouteViewTests(SimpleTestCase):
    URL = "/api/route/"
    QUERY = {"start": "Chicago, IL", "finish": "Denver, CO"}

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.ors_payload = json.loads(ORS_FIXTURE.read_text())
        coords = cls.ors_payload["features"][0]["geometry"]["coordinates"]  # [lng, lat]
        # Four synthetic stations sitting on the real route at 20/40/60/80% of its points.
        cls.index = build_station_index([
            _station(i, coords[int(len(coords) * frac)][1], coords[int(len(coords) * frac)][0], price)
            for i, (frac, price) in enumerate([(0.2, 3.5), (0.4, 3.0), (0.6, 3.2), (0.8, 2.9)], start=1)
        ])

    def setUp(self):
        cache.clear()
        for target, attr, value in [
            (services, "_station_index", self.index),
            (cities, "_cities", FAKE_CITIES),
        ]:
            patcher = mock.patch.object(target, attr, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(services, "_session")
        self.session = patcher.start()
        self.addCleanup(patcher.stop)
        self.session.post.return_value = _response(200, self.ors_payload)

    def test_fresh_request_makes_one_ors_call_and_repeat_makes_zero(self):
        with self.assertLogs("routing.services", level="INFO") as logs:
            first = self.client.get(self.URL, self.QUERY)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(self.session.post.call_count, 1)
        self.assertRegex(logs.output[0], r"ors=\d+ms matching=\d+ms optimizer=[\d.]+ms total=\d+ms")

        # Different spelling of the same places resolves to the same cache entry.
        again = self.client.get(self.URL, {"start": " chicago ,il", "finish": "DENVER, co"})
        self.assertEqual(again.status_code, 200)
        self.assertEqual(self.session.post.call_count, 1)
        self.assertEqual(again.json()["start"]["input"], "chicago ,il")  # echoes this request's input
        self.assertEqual(again.json()["fuel_stops"], first.json()["fuel_stops"])

    def test_response_shape(self):
        body = self.client.get(self.URL, self.QUERY).json()
        self.assertEqual(list(body), [
            "start", "finish", "distance_miles", "duration_hours", "fuel_stops",
            "total_gallons", "total_fuel_cost", "assumptions", "map_url", "geojson",
        ])
        self.assertEqual(body["start"], {"input": "Chicago, IL", "lat": 41.85, "lng": -87.65})
        self.assertEqual(body["distance_miles"], 1002.1)  # 1,612,715.8 m in the fixture
        self.assertEqual(body["assumptions"], {
            "max_range_miles": 500, "mpg": 10, "starts_with_full_tank": True, "corridor_miles": 10,
        })
        self.assertEqual(body["map_url"],
                         "http://testserver/api/route/map/?start=Chicago%2C+IL&finish=Denver%2C+CO")

        stops = body["fuel_stops"]
        self.assertTrue(stops)
        self.assertEqual([s["stop_number"] for s in stops], list(range(1, len(stops) + 1)))
        self.assertAlmostEqual(body["total_fuel_cost"], sum(s["cost"] for s in stops), delta=0.01 * len(stops))
        # Starting with a full 500-mile tank, the rest of the ~1002 miles must be bought at 10 mpg.
        self.assertAlmostEqual(body["total_gallons"], (1002.1 - 500) / 10, delta=0.1)

        features = body["geojson"]["features"]
        ors_coords = self.ors_payload["features"][0]["geometry"]["coordinates"]
        line = features[0]["geometry"]["coordinates"]
        self.assertEqual(features[0]["geometry"]["type"], "LineString")
        # Simplified for the response, but the endpoints (in [lng, lat] order) are preserved.
        self.assertLess(len(line), len(ors_coords) / 2)
        self.assertEqual(line[0], [round(ors_coords[0][0], 5), round(ors_coords[0][1], 5)])
        self.assertEqual(line[-1], [round(ors_coords[-1][0], 5), round(ors_coords[-1][1], 5)])
        self.assertEqual(len(features), 1 + len(stops))
        self.assertEqual(features[1]["geometry"]["coordinates"], [stops[0]["lng"], stops[0]["lat"]])
        self.assertEqual(features[1]["properties"]["stop_number"], 1)

    def test_map_view_reuses_cached_result(self):
        map_url = self.client.get(self.URL, self.QUERY).json()["map_url"]
        resp = self.client.get(map_url)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.session.post.call_count, 1)  # still just the JSON request's call
        self.assertContains(resp, 'id="route-geojson"')
        self.assertContains(resp, "leaflet.min.js")

    def test_missing_param_returns_400(self):
        resp = self.client.get(self.URL, {"start": "Chicago, IL"})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("finish", resp.json()["error"])
        self.session.post.assert_not_called()

    def test_bad_location_returns_400_without_ors_call(self):
        resp = self.client.get(self.URL, {"start": "Nowhere", "finish": "Denver, CO"})
        self.assertEqual(resp.status_code, 400)
        self.assertTrue(resp.json()["error"].startswith("start:"))
        self.session.post.assert_not_called()

    def test_unreachable_route_returns_422(self):
        far_away = build_station_index([_station(1, 25.77, -80.19)])  # Miami: nowhere near the route
        with mock.patch.object(services, "_station_index", far_away):
            resp = self.client.get(self.URL, self.QUERY)
        self.assertEqual(resp.status_code, 422)
        self.assertIn("no reachable fuel station", resp.json()["error"])

    def test_ors_failure_returns_502(self):
        self.session.post.return_value = _response(404, {"error": {"code": 2009, "message": "Route could not be found"}})
        resp = self.client.get(self.URL, self.QUERY)
        self.assertEqual(resp.status_code, 502)
        self.assertIn("No truck route found", resp.json()["error"])

    def test_root_index_returns_200(self):
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(set(resp.json()["endpoints"]), {"route", "route-map"})

    def test_map_view_shows_errors_with_status(self):
        resp = self.client.get("/api/route/map/", {"start": "Nowhere", "finish": "Denver, CO"})
        self.assertContains(resp, "Could not plan route", status_code=400)
