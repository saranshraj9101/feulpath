"""Offline US city -> (lat, lng) lookup backed by data/us_cities.csv.

Shared by the load_stations command and input geocoding. The CSV is downloaded
if missing, then read once per process and cached at module level.
"""
import csv
from pathlib import Path

import numpy as np
import requests
from django.conf import settings

CITIES_URL = "https://raw.githubusercontent.com/kelvins/US-Cities-Database/main/csv/us_cities.csv"
CITIES_PATH: Path = Path(settings.BASE_DIR) / "data" / "us_cities.csv"
EARTH_RADIUS_MILES = 3958.8
# Duplicate (CITY, ST) entries closer than this are one metro split across counties.
DUPLICATE_MERGE_MILES = 20.0

_cities: dict[tuple[str, str], tuple[float, float]] | None = None


def haversine_miles(lat1, lng1, lat2, lng2):
    """Great-circle distance in miles; works on scalars or numpy arrays (broadcasting)."""
    lat1, lng1, lat2, lng2 = map(np.radians, (lat1, lng1, lat2, lng2))
    a = np.sin((lat2 - lat1) / 2) ** 2 + np.cos(lat1) * np.cos(lat2) * np.sin((lng2 - lng1) / 2) ** 2
    return 2 * EARTH_RADIUS_MILES * np.arcsin(np.sqrt(a))


def lookup(city: str, state: str) -> tuple[float, float] | None:
    """Return (lat, lng) for a US city/state pair, or None if unknown.

    Duplicate keys in the dataset: if all entries lie within DUPLICATE_MERGE_MILES of
    each other they are averaged (e.g. Indianapolis, IN listed under two counties);
    otherwise they are distinct towns sharing a name and the first entry is kept.
    """
    global _cities
    if _cities is None:
        if not CITIES_PATH.exists():
            CITIES_PATH.parent.mkdir(parents=True, exist_ok=True)
            resp = requests.get(CITIES_URL, timeout=30)
            resp.raise_for_status()
            CITIES_PATH.write_bytes(resp.content)
        grouped: dict[tuple[str, str], list[tuple[float, float]]] = {}
        with open(CITIES_PATH, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                key = (row["CITY"].strip().upper(), row["STATE_CODE"].strip().upper())
                grouped.setdefault(key, []).append((float(row["LATITUDE"]), float(row["LONGITUDE"])))
        cities: dict[tuple[str, str], tuple[float, float]] = {}
        for key, points in grouped.items():
            lats, lngs = np.array(points).T
            spread = haversine_miles(lats[:, None], lngs[:, None], lats[None, :], lngs[None, :]).max()
            if len(points) > 1 and spread <= DUPLICATE_MERGE_MILES:
                cities[key] = (float(lats.mean()), float(lngs.mean()))
            else:
                cities[key] = points[0]
        _cities = cities
    return _cities.get((city.strip().upper(), state.strip().upper()))
