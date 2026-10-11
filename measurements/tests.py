import io
import shutil
import uuid
import zipfile
from pathlib import Path
from unittest.mock import patch

import geopandas as gpd
import numpy as np
import pandas as pd
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient
from shapely.geometry import (
    LineString,
    MultiLineString,
    MultiPoint,
    MultiPolygon,
    Point,
    Polygon,
)

from measurements.models import Feature, GeospatialFeature, GeospatialFile
from measurements.services import (
    InvalidGeospatialFile,
    _json_safe,
    read_geospatial_file,
)

TEST_MEDIA_ROOT = Path(__file__).resolve().parent / "test_media"


def _sample_kml_bytes(name: str = "Parcel A") -> bytes:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Document>
    <Placemark>
      <name>{name}</name>
      <Polygon>
        <outerBoundaryIs>
          <LinearRing>
            <coordinates>
              77.5900,12.9700,0
              77.5910,12.9700,0
              77.5910,12.9710,0
              77.5900,12.9710,0
              77.5900,12.9700,0
            </coordinates>
          </LinearRing>
        </outerBoundaryIs>
      </Polygon>
    </Placemark>
  </Document>
</kml>
""".encode()


def _build_shapefile_zip(
    geometries,
    *,
    crs: str | None = "EPSG:4326",
    names: list[str] | None = None,
) -> bytes:
    temp_root = TEST_MEDIA_ROOT / f"shp_{uuid.uuid4().hex}"
    temp_root.mkdir(parents=True, exist_ok=True)
    shp_path = temp_root / "layer.shp"

    data = {"geometry": geometries}
    if names is not None:
        data["name"] = names

    gdf = gpd.GeoDataFrame(data, crs=crs)
    gdf.to_file(shp_path, driver="ESRI Shapefile", engine="pyogrio")

    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in temp_root.iterdir():
            if path.is_file():
                archive.write(path, arcname=path.name)

    shutil.rmtree(temp_root, ignore_errors=True)
    return zip_buffer.getvalue()


@override_settings(MEDIA_ROOT=TEST_MEDIA_ROOT)
class GeospatialAPITestCase(TestCase):
    def setUp(self):
        TEST_MEDIA_ROOT.mkdir(parents=True, exist_ok=True)
        self.client = APIClient()

    def tearDown(self):
        shutil.rmtree(TEST_MEDIA_ROOT, ignore_errors=True)

    # -------------------------------------------------------------------------
    # Health check tests
    # -------------------------------------------------------------------------

    def test_health_endpoint(self):
        response = self.client.get(reverse("health-check"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["status"], "success")
        self.assertEqual(
            response.data["message"],
            "Geospatial Measurement API is running",
        )

    # -------------------------------------------------------------------------
    # Upload and processing tests
    # -------------------------------------------------------------------------

    def test_upload_kml_success(self):
        upload = SimpleUploadedFile(
            "parcel.kml",
            _sample_kml_bytes(),
            content_type="application/vnd.google-earth.kml+xml",
        )
        response = self.client.post(
            reverse("file-upload"),
            {"file": upload},
            format="multipart",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        file_id = response.data["id"]
        self.assertEqual(response.data["filename"], "parcel.kml")
        self.assertEqual(response.data["file_format"], "kml")
        self.assertEqual(response.data["feature_count"], 1)
        self.assertEqual(response.data["original_crs"], "EPSG:4326")
        self.assertTrue(response.data["measurement_crs"].startswith("EPSG:"))
        self.assertEqual(response.data["status"], "completed")

        self.assertTrue(GeospatialFile.objects.filter(pk=file_id).exists())
        self.assertEqual(Feature.objects.filter(geospatial_file_id=file_id).count(), 1)
        self.assertEqual(
            GeospatialFeature.objects.filter(geospatial_file_id=file_id).count(), 1
        )

    def test_upload_shapefile_zip_success(self):
        upload = SimpleUploadedFile(
            "parcels.zip",
            _build_shapefile_zip(
                [
                    Polygon(
                        [
                            (77.5900, 12.9700),
                            (77.5910, 12.9700),
                            (77.5910, 12.9710),
                            (77.5900, 12.9710),
                            (77.5900, 12.9700),
                        ]
                    )
                ]
            ),
            content_type="application/zip",
        )
        response = self.client.post(
            reverse("file-upload"),
            {"file": upload},
            format="multipart",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        feature = Feature.objects.get(geospatial_file_id=response.data["id"])
        self.assertEqual(feature.geometry_type, "Polygon")
        self.assertGreater(feature.area_sqm or 0, 0)
        self.assertGreater(feature.area_hectares or 0, 0)
        self.assertIsNone(feature.length_m)

    def test_missing_upload_field(self):
        response = self.client.post(
            reverse("file-upload"),
            {},
            format="multipart",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("file", response.data)

    def test_unsupported_extension(self):
        upload = SimpleUploadedFile(
            "data.geojson", b"{}", content_type="application/json"
        )
        response = self.client.post(
            reverse("file-upload"),
            {"file": upload},
            format="multipart",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_invalid_kml(self):
        upload = SimpleUploadedFile(
            "bad.kml", b"not xml content", content_type="application/xml"
        )
        response = self.client.post(
            reverse("file-upload"),
            {"file": upload},
            format="multipart",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("detail", response.data)

    def test_invalid_zip(self):
        upload = SimpleUploadedFile(
            "bad.zip", b"not-a-zip", content_type="application/zip"
        )
        response = self.client.post(
            reverse("file-upload"),
            {"file": upload},
            format="multipart",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_missing_shapefile_components(self):
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w") as archive:
            archive.writestr("only.shp", b"fake")
        upload = SimpleUploadedFile("bad.zip", zip_buffer.getvalue())
        response = self.client.post(
            reverse("file-upload"),
            {"file": upload},
            format="multipart",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("detail", response.data)

    def test_zip_path_traversal_rejected(self):
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w") as archive:
            archive.writestr("../evil.shp", b"x")
        upload = SimpleUploadedFile("evil.zip", zip_buffer.getvalue())
        response = self.client.post(
            reverse("file-upload"),
            {"file": upload},
            format="multipart",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_oversized_upload_rejected(self):
        from measurements.services import MAX_UPLOAD_SIZE

        payload = b"x" * (MAX_UPLOAD_SIZE + 1)
        upload = SimpleUploadedFile("big.kml", payload)
        response = self.client.post(
            reverse("file-upload"),
            {"file": upload},
            format="multipart",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_missing_crs_rejected(self):
        upload = SimpleUploadedFile(
            "no_crs.zip",
            _build_shapefile_zip(
                [Polygon([(0, 0), (1, 0), (1, 1), (0, 1), (0, 0)])],
                crs=None,
            ),
        )
        response = self.client.post(
            reverse("file-upload"),
            {"file": upload},
            format="multipart",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("CRS", response.data["detail"])

    def test_large_geographic_extent_rejected(self):
        # Spans 10 degrees longitude, which exceeds the regional 5-degree limit.
        wide_polygon = Polygon(
            [(70.0, 10.0), (80.0, 10.0), (80.0, 15.0), (70.0, 15.0), (70.0, 10.0)]
        )
        upload = SimpleUploadedFile(
            "wide.zip",
            _build_shapefile_zip([wide_polygon], crs="EPSG:4326"),
            content_type="application/zip",
        )
        response = self.client.post(
            reverse("file-upload"),
            {"file": upload},
            format="multipart",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("geographic extent", response.data["detail"])

    def test_invalid_geometry_rejected(self):
        # Bowtie / self-intersecting polygon is invalid in planar geometry.
        bowtie = Polygon([(0, 0), (1, 1), (1, 0), (0, 1), (0, 0)])
        upload = SimpleUploadedFile(
            "invalid_geom.zip",
            _build_shapefile_zip([bowtie], crs="EPSG:4326"),
            content_type="application/zip",
        )
        response = self.client.post(
            reverse("file-upload"),
            {"file": upload},
            format="multipart",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("invalid geometry", response.data["detail"].lower())

    def test_database_rollback_on_save_failure(self):
        upload = SimpleUploadedFile("parcel.kml", _sample_kml_bytes())
        with patch(
            "measurements.views.Feature.objects.bulk_create",
            side_effect=RuntimeError("forced failure"),
        ):
            response = self.client.post(
                reverse("file-upload"),
                {"file": upload},
                format="multipart",
            )
        self.assertEqual(response.status_code, status.HTTP_500_INTERNAL_SERVER_ERROR)
        self.assertEqual(GeospatialFile.objects.count(), 0)
        self.assertEqual(Feature.objects.count(), 0)

    # -------------------------------------------------------------------------
    # Measurement calculations and unit formulas
    # -------------------------------------------------------------------------

    def test_polygon_area_units(self):
        upload = SimpleUploadedFile("parcel.kml", _sample_kml_bytes())
        created = self.client.post(
            reverse("file-upload"),
            {"file": upload},
            format="multipart",
        )
        feature = Feature.objects.get(geospatial_file_id=created.data["id"])
        self.assertIsNotNone(feature.area_sqm)
        self.assertIsNotNone(feature.area_hectares)
        self.assertAlmostEqual(feature.area_hectares, (feature.area_sqm or 0) / 10000)
        self.assertIsNone(feature.length_m)
        self.assertIsNone(feature.length_km)

    def test_multipolygon_area(self):
        poly1 = Polygon(
            [
                (77.5900, 12.9700),
                (77.5905, 12.9700),
                (77.5905, 12.9705),
                (77.5900, 12.9705),
                (77.5900, 12.9700),
            ]
        )
        poly2 = Polygon(
            [
                (77.5910, 12.9710),
                (77.5915, 12.9710),
                (77.5915, 12.9715),
                (77.5910, 12.9715),
                (77.5910, 12.9710),
            ]
        )
        mpoly = MultiPolygon([poly1, poly2])
        upload = SimpleUploadedFile(
            "multipoly.zip",
            _build_shapefile_zip([mpoly]),
            content_type="application/zip",
        )
        response = self.client.post(
            reverse("file-upload"),
            {"file": upload},
            format="multipart",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        feature = Feature.objects.get(geospatial_file_id=response.data["id"])
        self.assertEqual(feature.geometry_type, "MultiPolygon")
        self.assertGreater(feature.area_sqm or 0, 0)
        self.assertAlmostEqual(feature.area_hectares, (feature.area_sqm or 0) / 10000)
        self.assertIsNone(feature.length_m)

    def test_line_length(self):
        line = LineString(
            [(77.5900, 12.9700), (77.5910, 12.9700), (77.5910, 12.9710)]
        )
        upload = SimpleUploadedFile(
            "roads.zip",
            _build_shapefile_zip([line]),
            content_type="application/zip",
        )
        response = self.client.post(
            reverse("file-upload"),
            {"file": upload},
            format="multipart",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        feature = Feature.objects.get(geospatial_file_id=response.data["id"])
        self.assertGreater(feature.length_m or 0, 0)
        self.assertAlmostEqual(feature.length_km, (feature.length_m or 0) / 1000)
        self.assertIsNone(feature.area_sqm)
        self.assertIsNone(feature.area_hectares)

    def test_multilinestring_length(self):
        mline = MultiLineString(
            [
                LineString([(77.5900, 12.9700), (77.5905, 12.9705)]),
                LineString([(77.5910, 12.9710), (77.5915, 12.9715)]),
            ]
        )
        upload = SimpleUploadedFile(
            "multilines.zip",
            _build_shapefile_zip([mline]),
            content_type="application/zip",
        )
        response = self.client.post(
            reverse("file-upload"),
            {"file": upload},
            format="multipart",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        feature = Feature.objects.get(geospatial_file_id=response.data["id"])
        self.assertEqual(feature.geometry_type, "MultiLineString")
        self.assertGreater(feature.length_m or 0, 0)
        self.assertAlmostEqual(feature.length_km, (feature.length_m or 0) / 1000)
        self.assertIsNone(feature.area_sqm)

    def test_point_has_no_measurements(self):
        upload = SimpleUploadedFile(
            "points.zip",
            _build_shapefile_zip([Point(77.5905, 12.9705)]),
            content_type="application/zip",
        )
        response = self.client.post(
            reverse("file-upload"),
            {"file": upload},
            format="multipart",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        feature = Feature.objects.get(geospatial_file_id=response.data["id"])
        self.assertEqual(feature.geometry_type, "Point")
        self.assertIsNone(feature.area_sqm)
        self.assertIsNone(feature.area_hectares)
        self.assertIsNone(feature.length_m)
        self.assertIsNone(feature.length_km)

    def test_multipoint_has_no_measurements(self):
        mpoint = MultiPoint([(77.5905, 12.9705), (77.5910, 12.9710)])
        upload = SimpleUploadedFile(
            "multipoints.zip",
            _build_shapefile_zip([mpoint]),
            content_type="application/zip",
        )
        response = self.client.post(
            reverse("file-upload"),
            {"file": upload},
            format="multipart",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        feature = Feature.objects.get(geospatial_file_id=response.data["id"])
        self.assertEqual(feature.geometry_type, "MultiPoint")
        self.assertIsNone(feature.area_sqm)
        self.assertIsNone(feature.area_hectares)
        self.assertIsNone(feature.length_m)
        self.assertIsNone(feature.length_km)

    # -------------------------------------------------------------------------
    # Retrieval tests
    # -------------------------------------------------------------------------

    def test_retrieve_file_metadata(self):
        upload = SimpleUploadedFile("parcel.kml", _sample_kml_bytes())
        created = self.client.post(
            reverse("file-upload"),
            {"file": upload},
            format="multipart",
        )
        file_id = created.data["id"]
        response = self.client.get(
            reverse("file-detail", kwargs={"file_id": file_id})
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["id"], file_id)
        self.assertEqual(response.data["original_filename"], "parcel.kml")
        self.assertEqual(response.data["file_format"], "kml")
        self.assertEqual(response.data["original_crs"], "EPSG:4326")
        self.assertTrue(response.data["measurement_crs"].startswith("EPSG:"))
        self.assertEqual(response.data["feature_count"], 1)
        self.assertEqual(response.data["status"], "completed")
        self.assertIn("created_at", response.data)

    def test_retrieve_measurements(self):
        upload = SimpleUploadedFile("parcel.kml", _sample_kml_bytes())
        created = self.client.post(
            reverse("file-upload"),
            {"file": upload},
            format="multipart",
        )
        file_id = created.data["id"]
        response = self.client.get(
            reverse("file-measurements", kwargs={"file_id": file_id})
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["file_id"], file_id)
        self.assertEqual(response.data["original_filename"], "parcel.kml")
        self.assertEqual(response.data["original_crs"], "EPSG:4326")
        self.assertTrue(response.data["measurement_crs"].startswith("EPSG:"))
        self.assertEqual(response.data["feature_count"], 1)
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(len(response.data["results"]), 1)

        result = response.data["results"][0]
        self.assertEqual(result["geometry_type"], "Polygon")
        self.assertGreater(result["area_sqm"], 0)
        self.assertGreater(result["area_hectares"], 0)
        self.assertIsNone(result["length_m"])
        self.assertIsNone(result["length_km"])
        self.assertIn("properties", result)
        self.assertIn("geometry", result)

    def test_measurements_pagination(self):
        # Create a shapefile with 3 features
        polys = [
            Polygon([(77.590, 12.970), (77.591, 12.970), (77.591, 12.971), (77.590, 12.971), (77.590, 12.970)]),
            Polygon([(77.592, 12.970), (77.593, 12.970), (77.593, 12.971), (77.592, 12.971), (77.592, 12.970)]),
            Polygon([(77.594, 12.970), (77.595, 12.970), (77.595, 12.971), (77.594, 12.971), (77.594, 12.970)]),
        ]
        upload = SimpleUploadedFile(
            "multi.zip",
            _build_shapefile_zip(polys, names=["A", "B", "C"]),
            content_type="application/zip",
        )
        created = self.client.post(
            reverse("file-upload"),
            {"file": upload},
            format="multipart",
        )
        file_id = created.data["id"]

        # Request page 1 with page_size = 2
        response = self.client.get(
            reverse("file-measurements", kwargs={"file_id": file_id}),
            {"page": 1, "page_size": 2},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 3)
        self.assertEqual(len(response.data["results"]), 2)
        self.assertIsNotNone(response.data["next"])
        self.assertIsNone(response.data["previous"])

        # Request page 2 with page_size = 2
        response_page2 = self.client.get(
            reverse("file-measurements", kwargs={"file_id": file_id}),
            {"page": 2, "page_size": 2},
        )
        self.assertEqual(response_page2.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response_page2.data["results"]), 1)
        self.assertIsNone(response_page2.data["next"])
        self.assertIsNotNone(response_page2.data["previous"])

    def test_unknown_file_id_returns_404(self):
        unknown = uuid.uuid4()
        self.assertEqual(
            self.client.get(
                reverse("file-detail", kwargs={"file_id": unknown})
            ).status_code,
            status.HTTP_404_NOT_FOUND,
        )
        self.assertEqual(
            self.client.get(
                reverse("file-measurements", kwargs={"file_id": unknown})
            ).status_code,
            status.HTTP_404_NOT_FOUND,
        )


@override_settings(MEDIA_ROOT=TEST_MEDIA_ROOT)
class OpenAPIAndSwaggerTestCase(TestCase):
    def setUp(self):
        self.client = APIClient()

    def test_schema_endpoint(self):
        response = self.client.get(reverse("schema"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["openapi"], "3.0.3")
        self.assertEqual(response.data["info"]["title"], "Geospatial File Measurement API")
        self.assertEqual(response.data["info"]["version"], "1.0.0")

    def test_swagger_ui_endpoint(self):
        response = self.client.get(reverse("swagger-ui"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("text/html", response["Content-Type"])
        self.assertIn(b"swagger-ui", response.content)

    def test_redoc_endpoint(self):
        response = self.client.get(reverse("redoc"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("text/html", response["Content-Type"])
        self.assertIn(b"redoc", response.content)

    def test_schema_includes_all_operations(self):
        response = self.client.get(reverse("schema"))
        paths = response.data["paths"]

        expected_operations = {
            "/api/health/": ["get"],
            "/api/files/": ["post"],
            "/api/files/{file_id}/": ["get"],
            "/api/files/{file_id}/measurements/": ["get"],
        }

        for path_pattern, methods in expected_operations.items():
            self.assertIn(path_pattern, paths)
            for method in methods:
                self.assertIn(method, paths[path_pattern])
                operation = paths[path_pattern][method]
                self.assertTrue(bool(operation.get("operationId")))
                self.assertTrue(bool(operation.get("summary")))

    def test_upload_operation_describes_multipart_file(self):
        response = self.client.get(reverse("schema"))
        upload_post = response.data["paths"]["/api/files/"]["post"]
        request_body = upload_post["requestBody"]

        self.assertIn("multipart/form-data", request_body["content"])
        schema_ref = request_body["content"]["multipart/form-data"]["schema"]["$ref"]
        schema_name = schema_ref.split("/")[-1]
        schema_def = response.data["components"]["schemas"][schema_name]
        self.assertIn("file", schema_def["properties"])
        self.assertEqual(schema_def["properties"]["file"]["type"], "string")
        self.assertEqual(schema_def["properties"]["file"]["format"], "binary")

    def test_uuid_path_parameters_in_schema(self):
        response = self.client.get(reverse("schema"))
        detail_get = response.data["paths"]["/api/files/{file_id}/"]["get"]
        parameters = detail_get["parameters"]
        uuid_param = next((p for p in parameters if p["name"] == "file_id"), None)

        self.assertIsNotNone(uuid_param)
        self.assertEqual(uuid_param["in"], "path")
        self.assertEqual(uuid_param["schema"]["type"], "string")
        self.assertEqual(uuid_param["schema"]["format"], "uuid")


class ServiceLayerTestCase(TestCase):
    def test_read_kml_directly(self):
        upload = SimpleUploadedFile("parcel.kml", _sample_kml_bytes())
        result = read_geospatial_file(upload)
        self.assertEqual(result["feature_count"], 1)
        self.assertIsNotNone(result["features"][0]["measurement"]["area_sqm"])

    def test_projected_polygon_predictable_area(self):
        # 100 m × 100 m square in UTM zone 43N (Bangalore area) = exactly 10,000 m².
        gdf = gpd.GeoDataFrame(
            {
                "geometry": [
                    Polygon(
                        [
                            (500000, 1430000),
                            (500100, 1430000),
                            (500100, 1430100),
                            (500000, 1430100),
                            (500000, 1430000),
                        ]
                    )
                ]
            },
            crs="EPSG:32643",
        )
        upload_bytes = _build_shapefile_zip(gdf.geometry, crs="EPSG:32643")
        upload = SimpleUploadedFile("square.zip", upload_bytes)
        result = read_geospatial_file(upload)
        area = result["features"][0]["measurement"]["area_sqm"]
        self.assertAlmostEqual(area, 10000.0, delta=1.0)

    def test_json_safe_helper(self):
        data = {
            "int_val": np.int64(10),
            "float_val": np.float64(3.14),
            "nan_val": float("nan"),
            "timestamp": pd.Timestamp("2026-10-11T12:00:00Z"),
            "none_val": None,
            "nested": {"bool_val": np.bool_(True)},
        }
        converted = _json_safe(data)
        self.assertEqual(converted["int_val"], 10)
        self.assertAlmostEqual(converted["float_val"], 3.14)
        self.assertIsNone(converted["nan_val"])
        self.assertEqual(converted["timestamp"], "2026-10-11T12:00:00+00:00")
        self.assertIsNone(converted["none_val"])
        self.assertEqual(converted["nested"]["bool_val"], True)
