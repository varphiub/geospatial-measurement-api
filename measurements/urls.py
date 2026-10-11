from django.urls import path
from drf_spectacular.renderers import OpenApiJsonRenderer, OpenApiYamlRenderer
from drf_spectacular.views import (
    SpectacularAPIView,
    SpectacularRedocView,
    SpectacularSwaggerView,
)

from . import views


class OpenApiSchemaView(SpectacularAPIView):
    """Serve OpenAPI 3 schema with JSON as the preferred default format."""

    renderer_classes = [OpenApiJsonRenderer, OpenApiYamlRenderer]


urlpatterns = [
    # OpenAPI 3 Schema & Interactive Documentation
    path("schema/", OpenApiSchemaView.as_view(), name="schema"),
    path(
        "docs/",
        SpectacularSwaggerView.as_view(url_name="schema"),
        name="swagger-ui",
    ),
    path(
        "redoc/",
        SpectacularRedocView.as_view(url_name="schema"),
        name="redoc",
    ),

    # Core Geospatial API Endpoints
    path("health/", views.health_check, name="health-check"),
    path("files/", views.GeospatialFileUploadView.as_view(), name="file-upload"),
    path(
        "files/<uuid:file_id>/",
        views.GeospatialFileDetailView.as_view(),
        name="file-detail",
    ),
    path(
        "files/<uuid:file_id>/measurements/",
        views.GeospatialFileMeasurementsView.as_view(),
        name="file-measurements",
    ),
]
