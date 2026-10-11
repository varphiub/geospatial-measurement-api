from django.contrib import admin

from measurements.models import Feature, GeospatialFile


class FeatureInline(admin.TabularInline):
    model = Feature
    extra = 0
    can_delete = False
    fields = (
        "feature_index",
        "geometry_type",
        "area_sqm",
        "area_hectares",
        "length_m",
        "length_km",
    )
    readonly_fields = fields


@admin.register(GeospatialFile)
class GeospatialFileAdmin(admin.ModelAdmin):
    list_display = (
        "original_filename",
        "file_format",
        "status",
        "feature_count",
        "original_crs",
        "measurement_crs",
        "created_at",
    )
    list_filter = ("status", "file_format", "created_at")
    search_fields = ("original_filename", "id", "original_crs", "measurement_crs")
    readonly_fields = ("id", "created_at", "uploaded_file")
    inlines = [FeatureInline]


@admin.register(Feature)
class FeatureAdmin(admin.ModelAdmin):
    list_display = (
        "geospatial_file",
        "feature_index",
        "geometry_type",
        "area_sqm",
        "area_hectares",
        "length_m",
        "length_km",
    )
    list_filter = ("geometry_type",)
    search_fields = (
        "feature_index",
        "geospatial_file__original_filename",
        "geospatial_file__id",
    )
    readonly_fields = (
        "geospatial_file",
        "feature_index",
        "geometry_type",
        "geometry",
        "properties",
        "area_sqm",
        "area_hectares",
        "length_m",
        "length_km",
    )
