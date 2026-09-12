"""Greedy cost-optimal fuel stop planner. Pure Python: no Django, no I/O."""
import bisect
import math


class UnreachableRoute(Exception):
    """No fuel station is reachable to cover a stretch of the route."""

    def __init__(self, gap_start_mile: float):
        self.gap_start_mile = gap_start_mile
        super().__init__(
            f"Route cannot be completed: no reachable fuel station after mile {gap_start_mile:.1f}."
        )


def plan_fuel_stops(
    stations: list[dict],
    total_miles: float,
    max_range: float = 500,
    mpg: float = 10,
    start_fuel_miles: float = 500,
) -> dict:
    """Choose where to buy fuel, and how much, to minimize cost over the route.

    `stations` are dicts with at least "mile_marker" and "price" (per gallon); every other
    key is copied into the stop it produces. Fuel is tracked in miles of range. The truck
    starts with `start_fuel_miles` and only fuel bought along the route is costed.

    Greedy rule at each position:
      1. A station within range is cheaper than here -> buy just enough to reach the first one.
      2. Otherwise, if the destination is within range -> buy just enough to finish.
      3. Otherwise -> fill the tank and go to the cheapest station within range.

    Returns {"stops", "total_gallons", "total_cost"} at full precision; only stops with
    gallons > 0 are included. Raises UnreachableRoute if a gap exceeds the range.
    """
    # Stations at the same mile (same city centroid) are ordered cheapest-first, so the
    # "first cheaper station" rule picks the cheapest one at that spot.
    route_stations = sorted(
        (s for s in stations if 0 < s["mile_marker"] < total_miles),
        key=lambda s: (s["mile_marker"], s["price"]),
    )
    markers = [s["mile_marker"] for s in route_stations]

    pos, fuel, price, here = 0.0, min(start_fuel_miles, max_range), math.inf, None
    stops: list[dict] = []
    while True:
        window = route_stations[bisect.bisect_right(markers, pos) : bisect.bisect_right(markers, pos + max_range)]
        cheaper = next((s for s in window if s["price"] < price), None)
        # fill_to: fuel (in miles) wanted when leaving here; drive: miles to the next stop.
        if cheaper is not None:
            target = cheaper
            fill_to = drive = cheaper["mile_marker"] - pos
        elif total_miles - pos <= max_range:
            target = None
            fill_to = drive = total_miles - pos
        elif window:
            target = min(window, key=lambda s: s["price"])
            fill_to, drive = max_range, target["mile_marker"] - pos
        else:
            raise UnreachableRoute(pos)

        buy = max(0.0, fill_to - fuel)
        if buy > 1e-9:
            if here is None:  # starting fuel can't reach the first station, and there's nowhere to buy
                raise UnreachableRoute(pos + fuel)
            gallons = buy / mpg
            stops.append({**here, "gallons": gallons, "cost": gallons * price})
        fuel += buy - drive

        if target is None:
            break
        pos, price, here = target["mile_marker"], target["price"], target

    return {
        "stops": stops,
        "total_gallons": sum((s["gallons"] for s in stops), 0.0),
        "total_cost": sum((s["cost"] for s in stops), 0.0),
    }
