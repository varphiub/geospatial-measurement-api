from rest_framework import serializers

from measurements.models import Feature, GeospatialFile
from measurements.services import MAX_UPLOAD_SIZE


class HealthCheckResponseSerializer(serializers.Serializer):
    """Response serializer for API health check."""

    status = serializers.CharField(
        help_text="Operational status of the service (e.g. 'success')"
    )
    message = serializers.CharField(
        help_text="Status message confirming service availability"
    )


class GeospatialFileUploadSerializer(serializers.Serializer):
    """Validate multipart uploads before geospatial processing."""

    file = serializers.FileField(
        help_text=(
            "Geospatial file to upload. Supported formats: .kml, "
            ".zip (Shapefile archive containing .shp, .shx, and .dbf). Max 25 MB."
        )
    )

    def validate_file(self, uploaded):
        name = uploaded.name.replace("\\", "/").split("/")[-1]
        extension = name.rsplit(".", 1)[-1].lower() if "." in name else ""

        if extension not in {"kml", "zip"}:
            raise serializers.ValidationError(
                "Only .kml and .zip (Shapefile) files are accepted."
            )

        size = getattr(uploaded, "size", None)
        if size is not None and size > MAX_UPLOAD_SIZE:
            raise serializers.ValidationError(
                "Upload exceeds the 25 MB limit."
            )

        return uploaded


class GeospatialFileUploadResponseSerializer(serializers.Serializer):
    """Summary returned immediately upon successful upload and processing."""

    id = serializers.UUIDField(
        help_text="Unique identifier of the persisted geospatial file record"
    )
    filename = serializers.CharField(
        help_text="Original uploaded filename"
    )
    file_format = serializers.ChoiceField(
        choices=GeospatialFile.FileFormat.choices,
        help_text="Detected format: 'kml' or 'zip'",
    )
    original_crs = serializers.CharField(
        allow_null=True,
        help_text="Original Coordinate Reference System detected in file (e.g. EPSG:4326)",
    )
    measurement_crs = serializers.CharField(
        allow_null=True,
        help_text="Projected CRS used for metric calculations (e.g. EPSG:32643)",
    )
    feature_count = serializers.IntegerField(
        help_text="Total number of features extracted and persisted"
    )
    status = serializers.ChoiceField(
        choices=GeospatialFile.ProcessingStatus.choices,
        help_text="Processing status ('completed', 'failed', etc.)",
    )


class GeospatialFileDetailSerializer(serializers.ModelSerializer):
    """File metadata record and processing status."""

    class Meta:
        model = GeospatialFile
        fields = (
            "id",
            "original_filename",
            "file_format",
            "original_crs",
            "measurement_crs",
            "feature_count",
            "status",
            "created_at",
            "error_message",
        )
        read_only_fields = fields


class FeatureMeasurementSerializer(serializers.ModelSerializer):
    """Feature geometry, original attributes, and calculated metric measurements."""

    feature_id = serializers.CharField(
        source="feature_index",
        help_text="Feature identifier or index from the source dataset",
    )
    geometry_type = serializers.CharField(
        allow_null=True,
        help_text="Geometry type (e.g. Polygon, MultiPolygon, LineString, Point)",
    )
    geometry = serializers.JSONField(
        allow_null=True,
        help_text="GeoJSON-compatible geometry dictionary with coordinates",
    )
    properties = serializers.JSONField(
        help_text="Key-value dictionary of original feature attributes",
    )
    area_sqm = serializers.FloatField(
        allow_null=True,
        help_text="Calculated planar area in square metres (polygons only; null for lines and points)",
    )
    area_hectares = serializers.FloatField(
        allow_null=True,
        help_text="Calculated area in hectares (area_sqm / 10000; null for lines and points)",
    )
    length_m = serializers.FloatField(
        allow_null=True,
        help_text="Calculated planar length in metres (lines only; null for polygons and points)",
    )
    length_km = serializers.FloatField(
        allow_null=True,
        help_text="Calculated length in kilometres (length_m / 1000; null for polygons and points)",
    )

    class Meta:
        model = Feature
        fields = (
            "feature_id",
            "geometry_type",
            "geometry",
            "properties",
            "area_sqm",
            "area_hectares",
            "length_m",
            "length_km",
        )
        read_only_fields = fields


class FileMeasurementsEnvelopeSerializer(serializers.Serializer):
    """File metadata envelope containing paginated feature measurements."""

    file_id = serializers.UUIDField(
        help_text="Unique identifier of the geospatial file record"
    )
    original_filename = serializers.CharField(
        help_text="Original uploaded filename"
    )
    file_format = serializers.CharField(
        help_text="File format ('kml' or 'zip')"
    )
    status = serializers.CharField(
        help_text="Processing status ('completed', 'failed', etc.)"
    )
    original_crs = serializers.CharField(
        allow_null=True,
        help_text="Original Coordinate Reference System of the dataset",
    )
    measurement_crs = serializers.CharField(
        allow_null=True,
        help_text="Projected CRS used for metric measurements",
    )
    feature_count = serializers.IntegerField(
        help_text="Total number of features in the dataset"
    )
    count = serializers.IntegerField(
        help_text="Total number of features available across all pages"
    )
    next = serializers.CharField(
        allow_null=True,
        help_text="URL to the next page of results, or null if on the last page",
    )
    previous = serializers.CharField(
        allow_null=True,
        help_text="URL to the previous page of results, or null if on the first page",
    )
    results = FeatureMeasurementSerializer(
        many=True,
        help_text="Page of feature measurement items",
    )


class ErrorResponseSerializer(serializers.Serializer):
    """Standard error response format."""

    detail = serializers.CharField(
        help_text="Human-readable description of the error"
    )


class FileValidationErrorSerializer(serializers.Serializer):
    """Validation failure response format for file upload."""

    file = serializers.ListField(
        child=serializers.CharField(),
        required=False,
        help_text="Validation error messages associated with the 'file' field",
    )
    detail = serializers.CharField(
        required=False,
        help_text="General validation or processing error message",
    )
