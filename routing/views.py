from urllib.parse import urlencode

from django.conf import settings
from django.http import JsonResponse
from django.shortcuts import render
from django.urls import reverse
from rest_framework.decorators import api_view
from rest_framework.response import Response

from routing.optimizer import UnreachableRoute
from routing.services import LocationError, ORSError, plan_route


def index(request):
    """GET / : small JSON index of the API endpoints."""
    params = {"start": '"City, ST" or "lat,lng"', "finish": '"City, ST" or "lat,lng"'}
    example = "?" + urlencode({"start": "Chicago, IL", "finish": "Denver, CO"})
    return JsonResponse({
        "endpoints": {
            name: {"url": reverse(name), "params": params,
                   "example": request.build_absolute_uri(reverse(name) + example)}
            for name in ("route", "route-map")
        }
    })


def _plan(params) -> tuple[dict | None, int, str]:
    """Validate start/finish query params and run the pipeline.

    Returns (result, http_status, error_message); result is None on error.
    """
    start, finish = params.get("start", "").strip(), params.get("finish", "").strip()
    missing = [name for name, value in (("start", start), ("finish", finish)) if not value]
    if missing:
        return None, 400, (
            f"Missing query parameter(s): {', '.join(missing)}. "
            'Use "City, ST" or "lat,lng", e.g. ?start=Chicago, IL&finish=Denver, CO'
        )
    try:
        return plan_route(start, finish), 200, ""
    except LocationError as exc:
        return None, 400, str(exc)
    except UnreachableRoute as exc:
        return None, 422, str(exc)
    except ORSError as exc:
        return None, 502, str(exc)


@api_view(["GET"])
def route(request):
    """GET /api/route/?start=&finish= : fuel stops, total cost and route GeoJSON."""
    result, status, error = _plan(request.query_params)
    if result is None:
        return Response({"error": error}, status=status)

    geojson = result.pop("geojson")  # re-added last so map_url sits above the large geometry
    query = urlencode({"start": result["start"]["input"], "finish": result["finish"]["input"]})
    result["map_url"] = request.build_absolute_uri(f"{reverse('route-map')}?{query}")
    result["geojson"] = geojson
    return Response(result)


def route_map(request):
    """GET /api/route/map/?start=&finish= : Leaflet map of the same (cached) plan."""
    result, status, error = _plan(request.GET)
    tiles = {"url": settings.MAP_TILE_URL, "attribution": settings.MAP_TILE_ATTRIBUTION,
             "maxZoom": settings.MAP_TILE_MAX_ZOOM}
    return render(request, "routing/map.html", {"result": result, "error": error, "tiles": tiles},
                  status=status)
