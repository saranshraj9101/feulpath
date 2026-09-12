import logging
import os
import sys
import threading
import time
from pathlib import Path

from django.apps import AppConfig, apps

logger = logging.getLogger(__name__)


def _warm_up() -> None:
    """Load the cities lookup and station index so the first request doesn't pay for it."""
    apps.ready_event.wait()  # querying the DB before app loading finishes triggers a Django warning
    from routing import cities, services

    t0 = time.perf_counter()
    try:
        cities.lookup("", "")
        stations = len(services.get_station_index().rows)
    except Exception as exc:  # missing table, missing CSV offline, etc.: never break startup
        logger.warning("Warm-up skipped: %s", exc)
        return
    if stations:
        logger.info("Warm-up done in %.0fms (%d stations)", (time.perf_counter() - t0) * 1000, stations)
    else:
        logger.warning("Warm-up: station table is empty; run `python manage.py load_stations`.")


class RoutingConfig(AppConfig):
    name = 'routing'

    def ready(self) -> None:
        # Warm up only in processes that serve requests: skip migrate, load_stations, test, shell, ...
        if Path(sys.argv[0]).name == "manage.py":
            if sys.argv[1:2] != ["runserver"]:
                return
            if "--noreload" not in sys.argv and os.environ.get("RUN_MAIN") != "true":
                return  # the autoreloader's file-watcher process; its child serves requests
        threading.Thread(target=_warm_up, name="routing-warm-up", daemon=True).start()
