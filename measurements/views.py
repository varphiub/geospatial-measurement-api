from django.db import transaction
from django.shortcuts import get_object_or_404
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import (
    OpenApiExample,
    OpenApiParameter,
    OpenApiResponse,
    extend_schema,
)
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.pagination import PageNumberPagination
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from measurements.models import Feature, GeospatialFile
from measurements.serializers import (
    ErrorResponseSerializer,
    FeatureMeasurementSerializer,
    FileMeasurementsEnvelopeSerializer,
    FileValidationErrorSerializer,
    GeospatialFileDetailSerializer,
    GeospatialFileUploadResponseSerializer,
    GeospatialFileUploadSerializer,
    HealthCheckResponseSerializer,
)
from measurements.services import InvalidGeospatialFile, read_geospatial_file


class FeatureResultsPagination(PageNumberPagination):
    page_size = 50
    page_size_query_param = "page_size"
    max_page_size = 200


@extend_schema(
    operation_id="health_check",
    summary="Health check",
    description="Confirms that the Geospatial File Measurement API is running and operational.",
    tags=["Health"],
    responses={
        200: OpenApiResponse(
            response=HealthCheckResponseSerializer,
            description="Service is healthy and ready to accept requests.",
            examples=[
                OpenApiExample(
                    name="Healthy Service Response",
                    value={
                        "status": "success",
                        "message": "Geospatial Measurement API is running",
                    },
                    response_only=True,
                    status_codes=["200"],
                )
            ],
        ),
    },
)
@api_view(["GET"])
def health_check(request):
    return Response(
        {
            "status": "success",
            "message": "Geospatial Measurement API is running",
        },
        status=status.HTTP_200_OK,
    )


class GeospatialFileUploadView(APIView):
    """Accept KML or Shapefile ZIP uploads, process geometries, and persist results."""

    parser_classes = [MultiPartParser, FormParser]

    @extend_schema(
        operation_id="upload_geospatial_file",
        summary="Upload and process a geospatial file",
        description=(
            "Accepts a multipart file upload containing a .kml file or a .zip archive "
            "with an ESRI Shapefile (.shp, .shx, and .dbf required). "
            "The service extracts vector features, transforms them into a suitable local "
            "projected coordinate reference system (e.g. UTM), calculates metric area "
            "(square metres and hectares for polygons) or length (metres and kilometres for lines), "
            "and stores metadata and measurements in the database."
        ),
        tags=["File Uploads"],
        request={
            "multipart/form-data": GeospatialFileUploadSerializer,
        },
        responses={
            201: OpenApiResponse(
                response=GeospatialFileUploadResponseSerializer,
                description="File successfully processed and persisted in the database.",
                examples=[
                    OpenApiExample(
                        name="KML Upload Response",
                        value={
                            "id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
                            "filename": "sample_parcel.kml",
                            "file_format": "kml",
                            "original_crs": "EPSG:4326",
                            "measurement_crs": "EPSG:32643",
                            "feature_count": 1,
                            "status": "completed",
                        },
                        response_only=True,
                        status_codes=["201"],
                    ),
                    OpenApiExample(
                        name="Shapefile ZIP Upload Response",
                        value={
                            "id": "a1b2c3d4-e5f6-7a8b-9c0d-1e2f3a4b5c6d",
                            "filename": "parcels.zip",
                            "file_format": "zip",
                            "original_crs": "EPSG:4326",
                            "measurement_crs": "EPSG:32643",
                            "feature_count": 3,
                            "status": "completed",
                        },
                        response_only=True,
                        status_codes=["201"],
                    ),
                ],
            ),
            400: OpenApiResponse(
                response=FileValidationErrorSerializer,
                description="Invalid upload (unsupported format, missing file, corrupt archive, or invalid geometry).",
                examples=[
                    OpenApiExample(
                        name="Unsupported Format",
                        value={"file": ["Only .kml and .zip (Shapefile) files are accepted."]},
                        response_only=True,
                        status_codes=["400"],
                    ),
                    OpenApiExample(
                        name="Corrupt or Missing Archive Components",
                        value={"detail": "Shapefile is missing .shx."},
                        response_only=True,
                        status_codes=["400"],
                    ),
                    OpenApiExample(
                        name="Missing CRS",
                        value={
                            "detail": (
                                "The file has no CRS. Measurement cannot be calculated "
                                "reliably without coordinate reference information."
                            )
                        },
                        response_only=True,
                        status_codes=["400"],
                    ),
                ],
            ),
            500: OpenApiResponse(
                response=ErrorResponseSerializer,
                description="Internal processing or database failure; transaction rolled back.",
                examples=[
                    OpenApiExample(
                        name="Database Persistence Failure",
                        value={
                            "detail": "Processing failed while saving results. No file record was stored."
                        },
                        response_only=True,
                        status_codes=["500"],
                    )
                ],
            ),
        },
    )
    def post(self, request):
        serializer = GeospatialFileUploadSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        uploaded = serializer.validated_data["file"]

        try:
            parsed = read_geospatial_file(uploaded)
        except InvalidGeospatialFile as exc:
            return Response(
                {"detail": str(exc)},
                status=status.HTTP_400_BAD_REQUEST,
            )

        filename = parsed["filename"]
        extension = filename.rsplit(".", 1)[-1].lower()
        file_format = (
            GeospatialFile.FileFormat.KML
            if extension == "kml"
            else GeospatialFile.FileFormat.ZIP
        )

        if hasattr(uploaded, "seek"):
            uploaded.seek(0)

        try:
            with transaction.atomic():
                geospatial_file = GeospatialFile(
                    original_filename=filename,
                    file_format=file_format,
                    original_crs=parsed["crs"],
                    measurement_crs=parsed["measurement_crs"],
                    feature_count=parsed["feature_count"],
                    status=GeospatialFile.ProcessingStatus.COMPLETED,
                )
                geospatial_file.uploaded_file.save(filename, uploaded, save=False)
                geospatial_file.save()

                feature_rows = []
                for feature in parsed["features"]:
                    measurement = feature["measurement"]
                    feature_rows.append(
                        Feature(
                            geospatial_file=geospatial_file,
                            feature_index=str(feature["feature_id"]),
                            geometry_type=feature["geometry_type"],
                            geometry=feature["geometry"],
                            properties=feature["properties"],
                            area_sqm=measurement.get("area_sqm"),
                            area_hectares=measurement.get("area_hectares"),
                            length_m=measurement.get("length_m"),
                            length_km=measurement.get("length_km"),
                        )
                    )

                Feature.objects.bulk_create(feature_rows)

        except Exception:
            return Response(
                {
                    "detail": (
                        "Processing failed while saving results. "
                        "No file record was stored."
                    )
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        return Response(
            {
                "id": str(geospatial_file.id),
                "filename": geospatial_file.original_filename,
                "file_format": str(geospatial_file.file_format),
                "original_crs": geospatial_file.original_crs,
                "measurement_crs": geospatial_file.measurement_crs,
                "feature_count": geospatial_file.feature_count,
                "status": str(geospatial_file.status),
            },
            status=status.HTTP_201_CREATED,
        )



class GeospatialFileDetailView(APIView):
    """Retrieve uploaded geospatial file metadata by UUID."""

    @extend_schema(
        operation_id="get_file_metadata",
        summary="Retrieve uploaded file metadata",
        description=(
            "Retrieves record metadata for a previously uploaded geospatial file, "
            "including original filename, detected format, coordinate reference systems, "
            "total feature count, processing status, and timestamp."
        ),
        tags=["File Metadata"],
        parameters=[
            OpenApiParameter(
                name="file_id",
                type=OpenApiTypes.UUID,
                location=OpenApiParameter.PATH,
                description="UUID identifier of the geospatial file record",
            ),
        ],
        responses={
            200: OpenApiResponse(
                response=GeospatialFileDetailSerializer,
                description="File metadata retrieved successfully.",
                examples=[
                    OpenApiExample(
                        name="Completed File Metadata",
                        value={
                            "id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
                            "original_filename": "sample_parcel.kml",
                            "file_format": "kml",
                            "original_crs": "EPSG:4326",
                            "measurement_crs": "EPSG:32643",
                            "feature_count": 1,
                            "status": "completed",
                            "created_at": "2026-10-11T12:00:00Z",
                            "error_message": "",
                        },
                        response_only=True,
                        status_codes=["200"],
                    ),
                ],
            ),
            404: OpenApiResponse(
                response=ErrorResponseSerializer,
                description="Geospatial file with the specified UUID was not found.",
                examples=[
                    OpenApiExample(
                        name="File Not Found",
                        value={"detail": "Not found."},
                        response_only=True,
                        status_codes=["404"],
                    )
                ],
            ),
        },
    )
    def get(self, request, file_id):
        geospatial_file = get_object_or_404(GeospatialFile, pk=file_id)
        serializer = GeospatialFileDetailSerializer(geospatial_file)
        return Response(serializer.data, status=status.HTTP_200_OK)


class GeospatialFileMeasurementsView(APIView):
    """Retrieve geometric measurements and feature attributes for an uploaded file."""

    @extend_schema(
        operation_id="get_file_measurements",
        summary="Retrieve calculated measurements",
        description=(
            "Retrieves geometric measurements and feature properties for a specific "
            "geospatial file by its UUID. For Polygons, area is returned in square metres "
            "and hectares. For LineStrings, length is returned in metres and kilometres. "
            "Points have null measurements. Results are paginated."
        ),
        tags=["Measurements"],
        parameters=[
            OpenApiParameter(
                name="file_id",
                type=OpenApiTypes.UUID,
                location=OpenApiParameter.PATH,
                description="UUID identifier of the geospatial file",
            ),
            OpenApiParameter(
                name="page",
                type=OpenApiTypes.INT,
                location=OpenApiParameter.QUERY,
                required=False,
                description="Page number within the paginated result set (default: 1).",
            ),
            OpenApiParameter(
                name="page_size",
                type=OpenApiTypes.INT,
                location=OpenApiParameter.QUERY,
                required=False,
                description="Number of feature results to return per page (max: 200, default: 50).",
            ),
        ],
        responses={
            200: OpenApiResponse(
                response=FileMeasurementsEnvelopeSerializer,
                description="Paginated list of feature measurements and file CRS metadata.",
                examples=[
                    OpenApiExample(
                        name="Polygon Measurement Example",
                        value={
                            "file_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
                            "original_filename": "parcels.kml",
                            "file_format": "kml",
                            "status": "completed",
                            "original_crs": "EPSG:4326",
                            "measurement_crs": "EPSG:32643",
                            "feature_count": 1,
                            "count": 1,
                            "next": None,
                            "previous": None,
                            "results": [
                                {
                                    "feature_id": "0",
                                    "geometry_type": "Polygon",
                                    "geometry": {
                                        "type": "Polygon",
                                        "coordinates": [
                                            [
                                                [77.59, 12.97],
                                                [77.591, 12.97],
                                                [77.591, 12.971],
                                                [77.59, 12.971],
                                                [77.59, 12.97],
                                            ]
                                        ],
                                    },
                                    "properties": {
                                        "name": "Parcel A",
                                        "zone": "Commercial",
                                    },
                                    "area_sqm": 12016.97,
                                    "area_hectares": 1.2017,
                                    "length_m": None,
                                    "length_km": None,
                                }
                            ],
                        },
                        response_only=True,
                        status_codes=["200"],
                    ),
                    OpenApiExample(
                        name="LineString Measurement Example",
                        value={
                            "file_id": "4bb95f64-5717-4562-b3fc-2c963f66afa7",
                            "original_filename": "roads.zip",
                            "file_format": "zip",
                            "status": "completed",
                            "original_crs": "EPSG:4326",
                            "measurement_crs": "EPSG:32643",
                            "feature_count": 1,
                            "count": 1,
                            "next": None,
                            "previous": None,
                            "results": [
                                {
                                    "feature_id": "0",
                                    "geometry_type": "LineString",
                                    "geometry": {
                                        "type": "LineString",
                                        "coordinates": [
                                            [77.59, 12.97],
                                            [77.591, 12.97],
                                            [77.591, 12.971],
                                        ],
                                    },
                                    "properties": {
                                        "road_id": "NH-44",
                                        "lanes": 4,
                                    },
                                    "area_sqm": None,
                                    "area_hectares": None,
                                    "length_m": 155.04,
                                    "length_km": 0.155,
                                }
                            ],
                        },
                        response_only=True,
                        status_codes=["200"],
                    ),
                    OpenApiExample(
                        name="Point Measurement (Null Values)",
                        value={
                            "file_id": "5cc95f64-5717-4562-b3fc-2c963f66afa8",
                            "original_filename": "points.zip",
                            "file_format": "zip",
                            "status": "completed",
                            "original_crs": "EPSG:4326",
                            "measurement_crs": "EPSG:32643",
                            "feature_count": 1,
                            "count": 1,
                            "next": None,
                            "previous": None,
                            "results": [
                                {
                                    "feature_id": "0",
                                    "geometry_type": "Point",
                                    "geometry": {
                                        "type": "Point",
                                        "coordinates": [77.5905, 12.9705],
                                    },
                                    "properties": {
                                        "station": "Station A",
                                        "elevation": 920,
                                    },
                                    "area_sqm": None,
                                    "area_hectares": None,
                                    "length_m": None,
                                    "length_km": None,
                                }
                            ],
                        },
                        response_only=True,
                        status_codes=["200"],
                    ),
                ],
            ),
            404: OpenApiResponse(
                response=ErrorResponseSerializer,
                description="Geospatial file with the specified UUID was not found.",
                examples=[
                    OpenApiExample(
                        name="File Not Found",
                        value={"detail": "Not found."},
                        response_only=True,
                        status_codes=["404"],
                    )
                ],
            ),
        },
    )
    def get(self, request, file_id):
        geospatial_file = get_object_or_404(GeospatialFile, pk=file_id)
        queryset = Feature.objects.filter(geospatial_file=geospatial_file).order_by(
            "id"
        )

        paginator = FeatureResultsPagination()
        page = paginator.paginate_queryset(queryset, request, view=self)
        if page is None:
            page = queryset
            total_count = queryset.count()
            next_link = None
            previous_link = None
        else:
            page_obj = paginator.page
            assert page_obj is not None
            total_count = page_obj.paginator.count
            next_link = paginator.get_next_link()
            previous_link = paginator.get_previous_link()

        feature_serializer = FeatureMeasurementSerializer(page, many=True)
        return Response(
            {
                "file_id": str(geospatial_file.id),
                "original_filename": geospatial_file.original_filename,
                "file_format": geospatial_file.file_format,
                "status": geospatial_file.status,
                "original_crs": geospatial_file.original_crs,
                "measurement_crs": geospatial_file.measurement_crs,
                "feature_count": geospatial_file.feature_count,
                "count": total_count,
                "next": next_link,
                "previous": previous_link,
                "results": feature_serializer.data,
            },
            status=status.HTTP_200_OK,
        )
