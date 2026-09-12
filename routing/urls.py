from django.urls import path

from routing import views

urlpatterns = [
    path("route/", views.route, name="route"),
    path("route/map/", views.route_map, name="route-map"),
]
