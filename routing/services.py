"""Input geocoding (offline), the single OpenRouteService call, and route/station matching."""
import logging
import re
import time
from typing import NamedTuple

import numpy as np
import requests
from django.conf import settings
from django.core.cache import cache
from scipy.spatial import cKDTree

from routing import cities
from routing.cities import EARTH_RADIUS_MILES, haversine_miles
from routing.models import FuelStation
from routing.optimizer import plan_fuel_stops

logger = logging.getLogger(__name__)

METERS_PER_MILE = 1609.344

# (min_lat, max_lat, min_lng, max_lng), slightly padded around each region.
USA_BOUNDING_BOXES = [
    (24.0, 50.0, -125.5, -66.0),  # continental US
    (51.0, 72.0, -180.0, -129.0),  # Alaska
    (18.5, 23.0, -161.0, -154.0),  # Hawaii
]

_LAT_LNG_RE = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*$")
_CITY_STATE_RE = re.compile(r"^\s*(.+?)\s*,\s*([A-Za-z]{2})\s*$")
_FORMAT_HINT = 'Use "City, ST" (e.g. "Chicago, IL") or "lat,lng" (e.g. "41.88,-87.63").'


class LocationError(ValueError):
    """User input could not be resolved to a US point."""


class ORSError(Exception):
    """Routing via OpenRouteService failed."""


class ORSAuthError(ORSError):
    pass


class ORSRateLimited(ORSError):
    pass


class RouteNotFound(ORSError):
    pass


class RouteTooLong(ORSError):
    pass


class Route(NamedTuple):
    coords: list[tuple[float, float]]  # (lat, lng)
    distance_miles: float
    duration_seconds: float


def geocode(text: str) -> tuple[float, float]:
    """Resolve "lat,lng" or "City, ST" to (lat, lng) without any network call."""
    if m := _LAT_LNG_RE.match(text):
        lat, lng = float(m.group(1)), float(m.group(2))
    elif (m := _CITY_STATE_RE.match(text)) and (point := cities.lookup(m.group(1), m.group(2))):
        lat, lng = point
    elif m:
        raise LocationError(f'Unknown US city "{text.strip()}". {_FORMAT_HINT}')
    else:
        raise LocationError(f'Could not understand location "{text.strip()}". {_FORMAT_HINT}')

    if not any(a <= lat <= b and c <= lng <= d for a, b, c, d in USA_BOUNDING_BOXES):
        raise LocationError(f"Location ({lat}, {lng}) is outside the USA.")
    return lat, lng


_session = requests.Session()


def get_route(start: tuple[float, float], finish: tuple[float, float]) -> Route:
    """Fetch a driving-hgv route between two (lat, lng) points with exactly one ORS POST."""
    if not settings.ORS_API_KEY:
        raise ORSAuthError("ORS_API_KEY is not configured on the server.")

    url = f"{settings.ORS_BASE_URL.rstrip('/')}/v2/directions/driving-hgv/geojson"
    body = {
        "coordinates": [[start[1], start[0]], [finish[1], finish[0]]],  # ORS wants [lng, lat]
        "instructions": False,
    }
    try:
        resp = _session.post(
            url,
            json=body,
            headers={"Authorization": settings.ORS_API_KEY},
            timeout=settings.ORS_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        raise ORSError(f"Routing service unreachable: {exc.__class__.__name__}") from exc

    if resp.status_code != 200:
        try:
            error = resp.json().get("error", {})
        except ValueError:
            error = {}
        # ORS errors are {"error": {"code": int, "message": str}}; the gateway may send a plain string.
        code = error.get("code") if isinstance(error, dict) else None
        message = (error.get("message") if isinstance(error, dict) else error) or resp.reason

        if resp.status_code in (401, 403):
            raise ORSAuthError(f"Routing service rejected the API key: {message}")
        if resp.status_code == 429:
            raise ORSRateLimited("Routing service rate limit reached, try again shortly.")
        if code == 2004:
            raise RouteTooLong(f"Route exceeds the routing service distance limit: {message}")
        if code in (2009, 2010):
            raise RouteNotFound(f"No truck route found between these points: {message}")
        raise ORSError(f"Routing service error ({resp.status_code}): {message}")

    feature = resp.json()["features"][0]
    summary = feature["properties"].get("summary", {})
    return Route(
        coords=[(lat, lng) for lng, lat, *_ in feature["geometry"]["coordinates"]],
        distance_miles=summary.get("distance", 0.0) / METERS_PER_MILE,
        duration_seconds=summary.get("duration", 0.0),
    )


class StationIndex(NamedTuple):
    rows: list[dict]  # display fields + price per station, in the same order as xyz
    xyz: np.ndarray  # (n, 3) unit-sphere coordinates


_station_index: StationIndex | None = None


def _to_xyz(lat: np.ndarray, lng: np.ndarray) -> np.ndarray:
    """Project lat/lng (degrees) onto the unit sphere so Euclidean distance tracks great-circle distance."""
    lat, lng = np.radians(lat), np.radians(lng)
    return np.column_stack((np.cos(lat) * np.cos(lng), np.cos(lat) * np.sin(lng), np.sin(lat)))


def build_station_index(rows: list[dict]) -> StationIndex:
    """Build the in-memory index from station dicts with "lat" and "lng" keys."""
    lat = np.array([r["lat"] for r in rows], dtype=float)
    lng = np.array([r["lng"] for r in rows], dtype=float)
    return StationIndex(rows=rows, xyz=_to_xyz(lat, lng).reshape(-1, 3))


def get_station_index() -> StationIndex:
    """Load all stations from the DB once per process; later calls hit memory only."""
    global _station_index
    if _station_index is None:
        rows = [
            {
                "opis_id": s["opis_id"],
                "name": s["name"],
                "address": s["address"],
                "city": s["city"],
                "state": s["state"],
                "lat": s["latitude"],
                "lng": s["longitude"],
                "price": float(s["price"]),
            }
            for s in FuelStation.objects.values(
                "opis_id", "name", "address", "city", "state", "latitude", "longitude", "price"
            )
        ]
        if not rows:  # don't cache an empty table (e.g. before load_stations has run)
            return build_station_index([])
        _station_index = build_station_index(rows)
    return _station_index


def densify(route_coords: list[tuple[float, float]], max_step_miles: float = 1.0):
    """Insert points so no segment exceeds max_step_miles.

    Returns (lat, lng, cumulative_miles) arrays for the densified route.
    """
    pts = np.asarray(route_coords, dtype=float).reshape(-1, 2)
    lat, lng = pts[:, 0], pts[:, 1]
    if len(pts) >= 2:
        seg_miles = haversine_miles(lat[:-1], lng[:-1], lat[1:], lng[1:])
        pieces = np.maximum(1, np.ceil(seg_miles / max_step_miles)).astype(int)
        seg = np.repeat(np.arange(len(pieces)), pieces)  # segment of each new point
        # fraction along its segment: 0, 1/n, ..., (n-1)/n
        t = (np.arange(pieces.sum()) - np.repeat(np.cumsum(pieces) - pieces, pieces)) / np.repeat(pieces, pieces)
        lat = np.append(lat[seg] + t * (lat[seg + 1] - lat[seg]), lat[-1])
        lng = np.append(lng[seg] + t * (lng[seg + 1] - lng[seg]), lng[-1])
    step = haversine_miles(lat[:-1], lng[:-1], lat[1:], lng[1:])
    return lat, lng, np.concatenate(([0.0], np.cumsum(step)))


def simplify_line(route_coords: list[tuple[float, float]], tolerance: float) -> np.ndarray:
    """Douglas-Peucker simplification of (lat, lng) points; tolerance in degrees. Keeps both endpoints."""
    pts = np.asarray(route_coords, dtype=float).reshape(-1, 2)
    if len(pts) < 3 or tolerance <= 0:
        return pts
    keep = np.zeros(len(pts), dtype=bool)
    keep[[0, -1]] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        i, j = stack.pop()
        if j - i < 2:
            continue
        seg, rel = pts[j] - pts[i], pts[i + 1 : j] - pts[i]
        seg_len = np.hypot(seg[0], seg[1])
        if seg_len == 0:
            dist = np.hypot(rel[:, 0], rel[:, 1])
        else:
            dist = np.abs(seg[0] * rel[:, 1] - seg[1] * rel[:, 0]) / seg_len  # perpendicular distance
        k = int(np.argmax(dist))
        if dist[k] > tolerance:
            mid = i + 1 + k
            keep[mid] = True
            stack += [(i, mid), (mid, j)]
    return pts[keep]


def stations_along_route(
    route_coords: list[tuple[float, float]], corridor_miles: float | None = None
) -> list[dict]:
    """Stations within corridor_miles of the route, each with a mile_marker, sorted along the route."""
    if corridor_miles is None:
        corridor_miles = settings.FUEL_CORRIDOR_MILES
    index = get_station_index()
    if not index.rows or not route_coords:
        return []

    lat, lng, cumulative = densify(route_coords)
    tree = cKDTree(_to_xyz(lat, lng))
    chord = 2 * np.sin(corridor_miles / EARTH_RADIUS_MILES / 2)
    dist, nearest = tree.query(index.xyz, k=1, distance_upper_bound=chord)

    inside = np.flatnonzero(np.isfinite(dist))  # misses come back as dist=inf
    inside = inside[np.argsort(cumulative[nearest[inside]], kind="stable")]
    return [{**index.rows[i], "mile_marker": float(cumulative[nearest[i]])} for i in inside]


def plan_route(start_text: str, finish_text: str) -> dict:
    """Full pipeline: geocode (offline) -> 1 ORS call -> station matching -> fuel plan.

    The result is cached for ROUTE_CACHE_SECONDS, keyed by the geocoded coordinates, so
    "Chicago, IL" and "chicago,il" share an entry. Raises LocationError (prefixed with the
    offending parameter), ORSError or UnreachableRoute; errors are never cached.
    """
    t0 = time.perf_counter()
    points = {}
    for param, text in (("start", start_text), ("finish", finish_text)):
        try:
            points[param] = geocode(text)
        except LocationError as exc:
            raise LocationError(f"{param}: {exc}") from exc
    start, finish = points["start"], points["finish"]
    # Echo this request's own input text, even when the plan comes from the cache.
    echo = {
        "start": {"input": start_text, "lat": start[0], "lng": start[1]},
        "finish": {"input": finish_text, "lat": finish[0], "lng": finish[1]},
    }

    key = f"route:{start[0]:.5f},{start[1]:.5f}:{finish[0]:.5f},{finish[1]:.5f}"
    if (cached := cache.get(key)) is not None:
        logger.info("route %r -> %r: cache hit, total=%.1fms", start_text, finish_text,
                    (time.perf_counter() - t0) * 1000)
        return {**echo, **cached}

    t1 = time.perf_counter()
    route = get_route(start, finish)
    t2 = time.perf_counter()
    stations = stations_along_route(route.coords)
    t3 = time.perf_counter()
    max_range = settings.FUEL_MAX_RANGE_MILES
    plan = plan_fuel_stops(stations, route.distance_miles, max_range=max_range,
                           mpg=settings.FUEL_MPG, start_fuel_miles=max_range)
    t4 = time.perf_counter()

    # Full precision above; rounding happens only here, for display.
    fuel_stops = [
        {
            "stop_number": n,
            "opis_id": s["opis_id"],
            "name": s["name"],
            "address": s["address"],
            "city": s["city"],
            "state": s["state"],
            "lat": s["lat"],
            "lng": s["lng"],
            "mile_marker": round(s["mile_marker"], 1),
            "price_per_gallon": round(s["price"], 3),
            "gallons": round(s["gallons"], 2),
            "cost": round(s["cost"], 2),
        }
        for n, s in enumerate(plan["stops"], start=1)
    ]
    result = {
        "distance_miles": round(route.distance_miles, 1),
        "duration_hours": round(route.duration_seconds / 3600, 2),
        "fuel_stops": fuel_stops,
        "total_gallons": round(plan["total_gallons"], 2),
        "total_fuel_cost": round(plan["total_cost"], 2),
        "assumptions": {
            "max_range_miles": max_range,
            "mpg": settings.FUEL_MPG,
            "starts_with_full_tank": True,
            "corridor_miles": settings.FUEL_CORRIDOR_MILES,
        },
        "geojson": {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "geometry": {
                        "type": "LineString",
                        "coordinates": [
                            [round(lng, 5), round(lat, 5)]
                            for lat, lng in simplify_line(route.coords, settings.ROUTE_GEOJSON_SIMPLIFY_DEGREES)
                        ],
                    },
                    "properties": {"kind": "route", "distance_miles": round(route.distance_miles, 1)},
                },
                *(
                    {
                        "type": "Feature",
                        "geometry": {"type": "Point", "coordinates": [s["lng"], s["lat"]]},
                        "properties": {"kind": "fuel_stop", **s},
                    }
                    for s in fuel_stops
                ),
            ],
        },
    }
    cache.set(key, result, settings.ROUTE_CACHE_SECONDS)
    logger.info(
        "route %r -> %r: ors=%.0fms matching=%.0fms optimizer=%.1fms total=%.0fms "
        "(%d stations in corridor, %d stops)",
        start_text, finish_text, (t2 - t1) * 1000, (t3 - t2) * 1000, (t4 - t3) * 1000,
        (t4 - t0) * 1000, len(stations), len(fuel_stops),
    )
    return {**echo, **result}
