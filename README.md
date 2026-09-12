# Fuel Route API

A Django REST API that plans a truck trip between two US locations and returns:

1. the driving route as GeoJSON, plus a link to an interactive map;
2. cost-optimal fuel stops along the route, for a truck with a 500-mile range;
3. the total fuel cost at 10 miles per gallon.

Each uncached request makes **exactly one** external API call (OpenRouteService directions). Geocoding, station matching and the fuel optimizer all run locally, in memory.

## Quick start

Requires Python 3.12+ and a free OpenRouteService key from [account.heigit.org](https://account.heigit.org).

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env              # then set ORS_API_KEY=<your key>
python manage.py migrate
python manage.py load_stations    # downloads data/us_cities.csv once, loads 6,614 stations
python manage.py runserver
```

On Windows (PowerShell):

```powershell
python -m venv venv
venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env       # then set ORS_API_KEY=<your key>
python manage.py migrate
python manage.py load_stations
python manage.py runserver
```

Then open <http://127.0.0.1:8000/> for an index of the endpoints, try <http://127.0.0.1:8000/api/route/?start=New%20York,%20NY&finish=Los%20Angeles,%20CA>, or import `postman/fuelroute.postman_collection.json` (set `base_url` if you're not on port 8000).

Run the tests (33 of them; no network, no API key needed):

```bash
python manage.py test routing
```

## API reference

### `GET /api/route/?start=<location>&finish=<location>`

A location is either `"City, ST"` (e.g. `Chicago, IL`) or `"lat,lng"` (e.g. `41.88,-87.63`). Both must be in the USA (continental US, Alaska or Hawaii).

Example: `GET /api/route/?start=New York, NY&finish=Los Angeles, CA` (response trimmed: 1 of 17 stops shown, GeoJSON shortened)

```json
{
  "start": {"input": "New York, NY", "lat": 40.74838, "lng": -73.996705},
  "finish": {"input": "Los Angeles, CA", "lat": 33.973093, "lng": -118.247896},
  "distance_miles": 2796.5,
  "duration_hours": 64.84,
  "fuel_stops": [
    {
      "stop_number": 1,
      "opis_id": 72445,
      "name": "SHEETZ #639",
      "address": "I-80 Exit 223",
      "city": "Youngstown",
      "state": "OH",
      "lat": 41.0986,
      "lng": -80.6474,
      "mile_marker": 391.1,
      "price_per_gallon": 3.059,
      "gallons": 5.49,
      "cost": 16.79
    }
  ],
  "total_gallons": 229.65,
  "total_fuel_cost": 694.19,
  "assumptions": {"max_range_miles": 500, "mpg": 10, "starts_with_full_tank": true, "corridor_miles": 10},
  "map_url": "http://127.0.0.1:8000/api/route/map/?start=New+York%2C+NY&finish=Los+Angeles%2C+CA",
  "geojson": {
    "type": "FeatureCollection",
    "features": [
      {"type": "Feature", "geometry": {"type": "LineString", "coordinates": [[-73.99683, 40.74829], "..."]},
       "properties": {"kind": "route", "distance_miles": 2796.5}},
      {"type": "Feature", "geometry": {"type": "Point", "coordinates": [-80.6474, 41.0986]},
       "properties": {"kind": "fuel_stop", "stop_number": 1, "name": "SHEETZ #639", "...": "..."}}
    ]
  }
}
```

- `mile_marker` is the distance along the route to the point nearest the station.
- Values are computed at full precision and rounded only in the response: price 3 dp, gallons and cost 2 dp.
- `start.input` / `finish.input` echo the request's own text, even when the plan comes from the cache.

**Errors**, always `{"error": "<readable message>"}`:

| Status | When |
|---|---|
| 400 | Missing `start`/`finish`, unparseable input, unknown city, or a point outside the USA. No external call is made. |
| 422 | The route has a stretch longer than 500 miles with no fuel station, so it can't be driven. |
| 502 | The routing service failed: bad key, rate limit, no truck route found, route too long, or unreachable. |

### `GET /api/route/map/?start=<location>&finish=<location>`

An HTML page with a Leaflet map (OpenTopoMap tiles, no key; swap via `MAP_TILE_URL` / `MAP_TILE_ATTRIBUTION` / `MAP_TILE_MAX_ZOOM` in settings): the route line, start/finish markers, and numbered fuel-stop markers whose popups show the name, address, price, gallons and cost. It reads the same cache as the JSON endpoint, so opening `map_url` after the JSON call makes **no** extra routing call.

## Data decisions

The fuel data (`data/fuel-prices-for-be-assessment.csv`) is loaded by `python manage.py load_stations`, which wipes and reloads the table each time:

| Step | Rows |
|---|---|
| Rows read | 8,151 |
| Canadian provinces dropped (ON, AB, BC, MB, SK, YT, QC, NS, NB); the API is USA-only | 620 |
| Duplicate OPIS IDs collapsed: 597 IDs appear more than once with different prices, and the **lowest** price is kept | 905 |
| No coordinate match for City + State, skipped | 12 |
| **Stations saved** | **6,614** |

- **City-level geocoding.** The CSV has no coordinates, and addresses are highway exits ("I-44, EXIT 283 & US-69"), which street geocoders handle poorly. Each station is placed at its city's coordinates from the MIT-licensed [US Cities Database](https://github.com/kelvins/US-Cities-Database), matched on uppercased, whitespace-stripped City + State. This works offline, costs no API calls, and is reproducible.
- **Duplicate city names in the cities dataset.** 125 (CITY, STATE) keys appear more than once. For **119** of them, all entries lie within 20 miles of each other: one metro split across counties (e.g. Indianapolis, IN is listed under Marion and Hamilton counties), so the coordinates are averaged. The other **6** are distinct towns that share a name, and the first entry is kept. This applies to both station geocoding and user input.
- **Prices** are stored at full source precision (`DecimalField`, 6 dp, e.g. 3.007333).

## How the fuel stops are chosen

1. **Find the stations on the route.** The route is densified so points are at most 1 mile apart, and the distance along the route is computed at each point. A KD-tree over the route points finds, for every station, the nearest point on the route. Stations within 10 miles are kept, each tagged with its mile marker.
2. **Plan the stops greedily.** The truck starts full. At each stop it looks at every station it can reach on its current range (the next 500 miles):
   - If one of them is **cheaper than here**, buy just enough fuel to reach the **first** such station (possibly nothing) and go there.
   - Otherwise, if the **destination is within range**, buy just enough to finish.
   - Otherwise, this is the cheapest fuel for a while: **fill the tank** and go to the **cheapest** station in range.
   - If no station is in range and the destination is too far, the route is reported as impossible (422), naming the mile where the gap starts.

   This is the classic greedy for this problem: never pay more than you have to, and carry cheap fuel as far as it will go.
3. **Cost** is gallons × price per stop, summed. Only stops where fuel is actually bought are returned.

## Assumptions

- The truck **starts with a full tank** (500 miles). Only fuel bought along the route is costed, and the truck buys only what it needs, so it arrives without unneeded extra fuel.
- **Range 500 miles, 10 mpg** (a full tank is 50 gallons).
- A station counts as "on the route" if it's within a **10-mile corridor**. The corridor is that wide because station positions are city centroids, not exact locations.
- The route is the OpenRouteService `driving-hgv` (heavy goods vehicle) profile.

All of these are settings in `fuelroute/settings.py` (`FUEL_MAX_RANGE_MILES`, `FUEL_MPG`, `FUEL_CORRIDOR_MILES`).

## Performance

- **One external call per uncached request.** Input geocoding uses the local cities dataset; only the directions request goes out (a single POST with a 15 s timeout).
- **In-memory matching.** All 6,614 stations are held in memory as unit-sphere coordinates, and the route is indexed with a SciPy `cKDTree`. Matching is one vectorized query, with no database access per request.
- **Startup warm-up.** The cities lookup and station index are loaded in a background thread when the server starts, so the first request doesn't pay for it. This is skipped for `migrate`, `load_stations`, tests and other management commands, and an empty or missing table only logs a warning.
- **24 h result cache.** The full result is cached (Django `LocMemCache`), keyed by the geocoded coordinates, so `Chicago, IL` and `chicago,il` share an entry. Repeat requests and the map page make no routing call. Errors are not cached.
- **Smaller response.** The route line in the response is simplified with Douglas-Peucker (tolerance 0.0005°, at most ~56 m off the road; `ROUTE_GEOJSON_SIMPLIFY_DEGREES`). Station matching still uses the full-precision route.

Measured on the dev server (New York, NY → Los Angeles, CA, 2,797 miles, 450 stations in the corridor, 17 stops):

| | Time |
|---|---|
| Startup warm-up (background, once) | 0.5–0.65 s |
| Routing call (OpenRouteService) | 2.3–2.6 s |
| Station matching | 27–57 ms |
| Fuel optimizer | 0.6–1.4 ms |
| **First request, uncached** | **2.35–2.65 s total**, of which 30–60 ms is this service; it was 4.4 s before the warm-up was added |
| Repeat request (cached) | 2–12 ms |

The response size for this route went from **473 KB to 65 KB** (route points: 21,743 → 2,572). A short route (Chicago, IL → Indianapolis, IN) takes ~0.45 s uncached, almost all of it the routing call.

## Known limitations

- **City-centroid accuracy.** Stations sit at their city's center, not their actual exit. That's why the corridor is 10 miles, and it means mile markers are approximate. A station in a large metro may be a few miles off.
- **No detour distance.** Driving from the highway to a station and back isn't added to the distance or fuel used.
- **Many small stops.** The spec asks for exact cost minimization, and the greedy stops at every cheaper station ahead, so a long route can include purchases of 1–2 gallons (NY → LA has 17 stops). A real fleet would add a minimum purchase or a per-stop cost; that trades a few dollars for far fewer stops.
- **Bounding-box check.** "Inside the USA" is a set of latitude/longitude boxes. They reject most foreign points, but the continental box also contains parts of southern Ontario (e.g. Toronto). Such a point would pass validation and fail later at routing or produce a cross-border route.
- **HeiGIT base path.** The HeiGIT-hosted OpenRouteService is at `https://api.heigit.org/openrouteservice/v2/...`; `https://api.heigit.org/v2/...` returns 404. The default `ORS_BASE_URL` is the working path and can be overridden in `.env`.
- **Cache per process.** `LocMemCache` isn't shared between worker processes and is cleared on restart. A multi-worker deployment would switch the cache backend to Redis or similar, with no code changes.

## Project layout

```
fuelroute/        settings, root URLs
routing/
  models.py       FuelStation
  cities.py       offline (CITY, ST) -> (lat, lng) lookup, haversine
  services.py     geocoding, the ORS call, station index + matching, plan_route() pipeline + cache
  optimizer.py    plan_fuel_stops(): pure greedy algorithm, no Django
  views.py        JSON endpoint and map page
  apps.py         startup warm-up
  management/commands/load_stations.py
  templates/routing/map.html
  testdata/ors_chicago_denver.json   real ORS response used by the view tests
  tests.py
postman/          Postman collection (3 example requests)
```

# feulpath
