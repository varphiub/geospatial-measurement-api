import uuid

from django.db import models


def default_properties() -> dict:
    return {}


class GeospatialFile(models.Model):
    """
    One uploaded KML or Shapefile ZIP and its processing outcome.

    Features hang off this record so file metadata is stored once while each
    geometry keeps its own attributes and measurements.
    """

    class FileFormat(models.TextChoices):
        KML = "kml", "KML"
        ZIP = "zip", "Shapefile ZIP"

    class ProcessingStatus(models.TextChoices):
        PENDING = "pending", "Pending"
        PROCESSING = "processing", "Processing"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    original_filename = models.CharField(max_length=255)
    uploaded_file = models.FileField(upload_to="geospatial_uploads/%Y/%m/%d/")
    file_format = models.CharField(max_length=8, choices=FileFormat.choices)
    original_crs = models.CharField(max_length=64, blank=True, null=True)
    measurement_crs = models.CharField(max_length=64, blank=True, null=True)
    feature_count = models.PositiveIntegerField(default=0)
    status = models.CharField(
        max_length=16,
        choices=ProcessingStatus.choices,
        default=ProcessingStatus.PENDING,
        db_index=True,
    )
    error_message = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status", "created_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.original_filename} ({self.id})"


class Feature(models.Model):
    """
    A single vector feature from an upload, with metric measurements.

    Area and length fields are nullable because points have no area/length and
    empty geometries produce no measurements.
    """

    geospatial_file = models.ForeignKey(
        GeospatialFile,
        on_delete=models.CASCADE,
        related_name="features",
    )
    feature_index = models.CharField(max_length=64)
    geometry_type = models.CharField(max_length=32, blank=True, null=True)
    geometry = models.JSONField(blank=True, null=True)
    properties = models.JSONField(default=default_properties, blank=True)
    area_sqm = models.FloatField(blank=True, null=True)
    area_hectares = models.FloatField(blank=True, null=True)
    length_m = models.FloatField(blank=True, null=True)
    length_km = models.FloatField(blank=True, null=True)

    class Meta:
        ordering = ["id"]
        indexes = [
            models.Index(fields=["geospatial_file", "feature_index"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["geospatial_file", "feature_index"],
                name="unique_feature_index_per_file",
            ),
        ]

    def __str__(self) -> str:
        return f"Feature {self.feature_index} ({self.geometry_type})"


# Alias to support GeospatialFeature naming convention.
GeospatialFeature = Feature

