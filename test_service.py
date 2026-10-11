"""Standalone service-layer smoke tests (no Django server required)."""

import io
import shutil
import uuid
import zipfile
from contextlib import contextmanager
from pathlib import Path

import geopandas as gpd
from shapely.geometry import LineString, MultiPolygon, Point, Polygon

from measurements.services import (
    ChunkedUpload,
    calculate_measurements,
    read_geospatial_file,
)

TEST_TEMP_ROOT = Path(__file__).resolve().parent / ".local_tmp"


@contextmanager
def _temporary_directory():
    TEST_TEMP_ROOT.mkdir(exist_ok=True)
    directory = TEST_TEMP_ROOT / f"test_{uuid.uuid4().hex}"
    directory.mkdir()

    try:
        yield directory
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def test_kml():
    kml = b"""<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Document>
    <Placemark>
      <name>Parcel A</name>
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
"""

    upload = ChunkedUpload(
        "sample.kml",
        kml,
    )

    result = read_geospatial_file(upload)

    assert result["feature_count"] == 1
    assert result["features"][0]["geometry_type"] == "Polygon"

    print("KML test passed")
    print("CRS:", result["crs"])
    print("Features:", result["feature_count"])


def test_shapefile_zip():
    with _temporary_directory() as directory:
        root = Path(directory)
        shp_path = root / "parcels.shp"

        gdf = gpd.GeoDataFrame(
            {
                "name": ["Parcel B"],
                "geometry": [
                    Polygon(
                        [
                            (77.5900, 12.9700),
                            (77.5910, 12.9700),
                            (77.5910, 12.9710),
                            (77.5900, 12.9710),
                            (77.5900, 12.9700),
                        ]
                    )
                ],
            },
            crs="EPSG:4326",
        )

        gdf.to_file(
            shp_path,
            driver="ESRI Shapefile",
            engine="pyogrio",
        )

        zip_buffer = io.BytesIO()

        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in root.iterdir():
                if path.is_file():
                    archive.write(path, arcname=path.name)

        upload = ChunkedUpload("sample.zip", zip_buffer.getvalue())

        result = read_geospatial_file(upload)

        assert result["feature_count"] == 1
        assert result["features"][0]["geometry_type"] == "Polygon"
        assert result["crs"] is not None
        assert result["measurement_crs"] is not None
        assert result["features"][0]["measurement"]["area_sqm"] > 0

        print("Shapefile ZIP test passed")
        print("CRS:", result["crs"])
        print("Features:", result["feature_count"])


def test_area_calculation():
    polygon = Polygon(
        [
            (77.5900, 12.9700),
            (77.5910, 12.9700),
            (77.5910, 12.9710),
            (77.5900, 12.9710),
            (77.5900, 12.9700),
        ]
    )

    gdf = gpd.GeoDataFrame({"geometry": [polygon]}, crs="EPSG:4326")

    _, measurement_crs, results = calculate_measurements(gdf)

    result = results[0]

    assert result["area_sqm"] > 0
    assert result["area_hectares"] > 0
    assert result["length_m"] is None

    print("Area calculation test passed")
    print("Measurement CRS:", measurement_crs)
    print("Area (m²):", round(result["area_sqm"], 2))
    print("Area (ha):", round(result["area_hectares"], 6))


def test_line_and_point_calculation():
    line = LineString([(77.5900, 12.9700), (77.5910, 12.9710)])
    point = Point(77.5905, 12.9705)
    gdf = gpd.GeoDataFrame({"geometry": [line, point]}, crs="EPSG:4326")

    _, measurement_crs, results = calculate_measurements(gdf)

    line_result = results[0]
    point_result = results[1]

    assert line_result["length_m"] > 0
    assert line_result["length_km"] > 0
    assert line_result["area_sqm"] is None

    assert point_result["area_sqm"] is None
    assert point_result["length_m"] is None

    print("Line and point calculation test passed")
    print("Line length (m):", round(line_result["length_m"], 2))
    print("Line length (km):", round(line_result["length_km"], 4))


def test_multipolygon_calculation():
    p1 = Polygon([(77.5900, 12.9700), (77.5905, 12.9700), (77.5905, 12.9705), (77.5900, 12.9705), (77.5900, 12.9700)])
    p2 = Polygon([(77.5910, 12.9710), (77.5915, 12.9710), (77.5915, 12.9715), (77.5910, 12.9715), (77.5910, 12.9710)])
    mp = MultiPolygon([p1, p2])
    gdf = gpd.GeoDataFrame({"geometry": [mp]}, crs="EPSG:4326")

    _, _, results = calculate_measurements(gdf)
    res = results[0]
    assert res["area_sqm"] > 0
    assert res["area_hectares"] == res["area_sqm"] / 10000
    print("MultiPolygon calculation test passed")


if __name__ == "__main__":
    test_kml()
    test_shapefile_zip()
    test_area_calculation()
    test_line_and_point_calculation()
    test_multipolygon_calculation()
    print("All tests passed")
