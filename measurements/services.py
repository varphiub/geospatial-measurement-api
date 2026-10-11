import math
import shutil
import stat
import uuid
import zipfile
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path, PurePosixPath
from collections.abc import Hashable
from typing import Any, Iterator

import geopandas as gpd  # pyright: ignore[reportMissingModuleSource]
import pandas as pd
from shapely.geometry import mapping
from shapely.geometry.base import BaseGeometry


# Upload and archive limits.
MAX_UPLOAD_SIZE = 25 * 1024 * 1024       # 25 MB
MAX_EXTRACTED_SIZE = 100 * 1024 * 1024   # 100 MB
MAX_ARCHIVE_FILES = 500
TEMP_ROOT = Path(__file__).resolve().parent.parent / ".local_tmp"


class InvalidGeospatialFile(Exception):
    """Raised when an uploaded geospatial file cannot be processed."""


# Reject datasets whose bounds are too large for one local UTM zone (~5° ≈ regional).
MAX_MEASUREMENT_LON_SPAN_DEG = 5.0
MAX_MEASUREMENT_LAT_SPAN_DEG = 5.0


@contextmanager
def _temporary_directory():
    """Create a writable temporary directory and clean it up."""
    TEMP_ROOT.mkdir(exist_ok=True)
    directory = TEMP_ROOT / f"geospatial_{uuid.uuid4().hex}"
    directory.mkdir()

    try:
        yield directory
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def _validate_geographic_extent(geographic_gdf: gpd.GeoDataFrame) -> None:
    """Ensure the dataset fits a single local projected CRS (e.g. one UTM zone)."""
    if geographic_gdf.empty:
        raise InvalidGeospatialFile("The file contains no features.")

    valid_geoms = geographic_gdf[
        geographic_gdf.geometry.notna() & ~geographic_gdf.geometry.is_empty
    ]
    if valid_geoms.empty:
        raise InvalidGeospatialFile("The file contains only empty or missing geometries.")

    minx, miny, maxx, maxy = valid_geoms.total_bounds
    if not all(math.isfinite(val) for val in (minx, miny, maxx, maxy)):
        raise InvalidGeospatialFile("The file contains invalid geometry bounds.")

    lon_span = maxx - minx
    lat_span = maxy - miny

    if lon_span > MAX_MEASUREMENT_LON_SPAN_DEG or lat_span > MAX_MEASUREMENT_LAT_SPAN_DEG:
        raise InvalidGeospatialFile(
            "The dataset geographic extent is too large for a single local "
            "measurement projection. Split the data by region or use a "
            "purpose-built projected CRS."
        )


def calculate_measurements(gdf: gpd.GeoDataFrame):
    """
    Calculate polygon area and line length in metric units.

    Geometries in EPSG:4326 are never measured directly in degrees. The file
    CRS is preserved conceptually; features are transformed to an estimated
    local UTM CRS (via ``estimate_utm_crs`` on WGS84 bounds) so area is in
    square metres and length in metres.

    Assumes a local or regional extent (see ``MAX_MEASUREMENT_*_SPAN_DEG``).
    Returns projected geometries and the CRS used for measurement.
    """

    if gdf.crs is None:
        raise InvalidGeospatialFile(
            "The file has no CRS. Measurement cannot be "
            "calculated reliably without coordinate reference information."
        )

    # Pre-validate geometry validity before CRS transformations
    for geom in gdf.geometry:
        if geom is not None and not geom.is_empty and not geom.is_valid:
            raise InvalidGeospatialFile("The file contains an invalid geometry.")

    try:
        geographic_gdf = gdf.to_crs("EPSG:4326")
        _validate_geographic_extent(geographic_gdf)

        valid_geoms = geographic_gdf[
            geographic_gdf.geometry.notna() & ~geographic_gdf.geometry.is_empty
        ]
        measurement_crs = valid_geoms.estimate_utm_crs()
        if measurement_crs is None:
            raise InvalidGeospatialFile(
                "Could not determine a suitable measurement CRS."
            )

        projected_gdf = geographic_gdf.to_crs(measurement_crs)

    except InvalidGeospatialFile:
        raise
    except Exception as exc:
        raise InvalidGeospatialFile(
            "Could not transform the file into a suitable "
            "coordinate reference system."
        ) from exc


    measurements = []

    for geometry in projected_gdf.geometry:
        if geometry is None or geometry.is_empty:
            measurements.append({
                "area_sqm": None,
                "area_hectares": None,
                "length_m": None,
                "length_km": None,
            })

        elif not geometry.is_valid:
            raise InvalidGeospatialFile(
                "The file contains an invalid geometry."
            )

        elif geometry.geom_type in ("Polygon", "MultiPolygon"):
            area_sqm = float(geometry.area)
            measurements.append({
                "area_sqm": area_sqm,
                "area_hectares": area_sqm / 10000,
                "length_m": None,
                "length_km": None,
            })

        elif geometry.geom_type in ("LineString", "MultiLineString"):
            length_m = float(geometry.length)
            measurements.append({
                "area_sqm": None,
                "area_hectares": None,
                "length_m": length_m,
                "length_km": length_m / 1000,
            })

        elif geometry.geom_type in ("Point", "MultiPoint"):
            measurements.append({
                "area_sqm": None,
                "area_hectares": None,
                "length_m": None,
                "length_km": None,
            })

        else:
            raise InvalidGeospatialFile(
                f"Unsupported geometry type: {geometry.geom_type}."
            )

    return projected_gdf, measurement_crs, measurements

def _json_safe(value):
    """Convert common Python and Pandas values into JSON-safe values."""
    if value is None:
        return None

    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}

    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]

    if hasattr(value, "item"):
        value = value.item()

    if isinstance(value, float) and not math.isfinite(value):
        return None

    if isinstance(value, (pd.Timestamp, datetime, date)):
        return value.isoformat()

    if pd.isna(value):
        return None

    if isinstance(value, (str, int, float, bool)):
        return value

    return str(value)


def _row_geometry(row: pd.Series, geometry_column: Hashable) -> BaseGeometry | None:
    """Return a single Shapely geometry from a GeoDataFrame row."""
    raw: Any = row[geometry_column]
    if raw is None or (not isinstance(raw, BaseGeometry) and pd.isna(raw)):
        return None
    if isinstance(raw, BaseGeometry):
        return raw
    raise InvalidGeospatialFile("Feature geometry is not a valid shape.")


def _extract_shapefile_zip(zip_path, destination):
    """Safely extract a ZIP and locate its single Shapefile."""

    try:
        with zipfile.ZipFile(zip_path, "r") as archive:
            entries = archive.infolist()

            if not entries or len(entries) > MAX_ARCHIVE_FILES:
                raise InvalidGeospatialFile(
                    "ZIP must contain between 1 and 500 entries."
                )

            total_size = 0
            shp_entries = []

            for entry in entries:
                name = entry.filename.replace("\\", "/")
                path = PurePosixPath(name)

                # Reject absolute paths and path traversal.
                if (
                    path.is_absolute()
                    or ".." in path.parts
                    or (path.parts and ":" in path.parts[0])
                ):
                    raise InvalidGeospatialFile(
                        "ZIP contains an unsafe file path."
                    )

                # Reject symbolic links.
                mode = entry.external_attr >> 16
                if stat.S_ISLNK(mode):
                    raise InvalidGeospatialFile(
                        "ZIP symbolic links are not allowed."
                    )

                if entry.is_dir():
                    continue

                total_size += entry.file_size

                if total_size > MAX_EXTRACTED_SIZE:
                    raise InvalidGeospatialFile(
                        "Extracted ZIP contents exceed 100 MB."
                    )

                # Reject extreme compression ratios.
                if (
                    entry.file_size > 1024 * 1024
                    and (
                        entry.compress_size == 0
                        or entry.file_size / entry.compress_size > 200
                    )
                ):
                    raise InvalidGeospatialFile(
                        "ZIP has a suspicious compression ratio."
                    )

                if path.suffix.lower() == ".shp":
                    shp_entries.append((entry, path))

            if len(shp_entries) != 1:
                raise InvalidGeospatialFile(
                    "ZIP must contain exactly one .shp file."
                )

            shp_entry, shp_path = shp_entries[0]

            # Require the main Shapefile components in the same folder as the .shp.
            sibling_names: set[str] = set()
            for entry in entries:
                if entry.is_dir():
                    continue
                sibling_path = PurePosixPath(
                    entry.filename.replace("\\", "/")
                )
                if sibling_path.parent == shp_path.parent:
                    sibling_names.add(sibling_path.name.lower())

            stem = shp_path.stem.lower()

            for extension in (".shp", ".shx", ".dbf"):
                if f"{stem}{extension}" not in sibling_names:
                    raise InvalidGeospatialFile(
                        f"Shapefile is missing {extension}."
                    )

            # Extract validated entries manually. Never use extractall().
            for entry in entries:
                if entry.is_dir():
                    continue

                relative = PurePosixPath(
                    entry.filename.replace("\\", "/")
                )
                output_path = destination.joinpath(*relative.parts)

                output_path.parent.mkdir(
                    parents=True,
                    exist_ok=True,
                )

                with archive.open(entry) as source:
                    with output_path.open("wb") as target:
                        while True:
                            chunk = source.read(1024 * 1024)
                            if not chunk:
                                break
                            target.write(chunk)

            return destination.joinpath(*shp_path.parts)

    except (zipfile.BadZipFile, RuntimeError) as exc:
        raise InvalidGeospatialFile(
            "The uploaded ZIP file is invalid."
        ) from exc



def read_geospatial_file(uploaded_file):
    """
    Read a KML file or a ZIP containing a Shapefile.

    Returns file metadata, feature details, and measurements.
    """

    original_name = getattr(uploaded_file, "name", "")
    filename = original_name.replace("\\", "/").split("/")[-1]
    extension = Path(filename).suffix.lower()

    if extension not in {".kml", ".zip"}:
        raise InvalidGeospatialFile(
            "Only .kml and .zip files are accepted."
        )

    size = getattr(uploaded_file, "size", None)

    if size is not None and size > MAX_UPLOAD_SIZE:
        raise InvalidGeospatialFile(
            "Upload exceeds the 25 MB limit."
        )

    with _temporary_directory() as root:
        source_path = root / ("upload" + extension)

        written_size = 0

        # Stream the uploaded file into temporary storage.
        with source_path.open("wb") as destination:
            for chunk in uploaded_file.chunks():
                written_size += len(chunk)

                if written_size > MAX_UPLOAD_SIZE:
                    raise InvalidGeospatialFile(
                        "Upload exceeds the 25 MB limit."
                    )

                destination.write(chunk)

        if extension == ".zip":
            data_path = _extract_shapefile_zip(
                source_path,
                root / "extracted",
             )
        else:
            data_path = source_path

        engine = "pyogrio"

        try:
            gdf = gpd.read_file(
                data_path,
                engine=engine,
            )
        except Exception as exc:
            raise InvalidGeospatialFile(
                "Unable to read the geospatial file. "
                "Check its contents, format and reading-engine support."
            ) from exc

        _projected_gdf, measurement_crs, measurements = calculate_measurements(gdf)
        geometry_column = gdf.geometry.name
        features = []

        for position, (index, row) in enumerate(gdf.iterrows()):
            geometry = _row_geometry(row, geometry_column)
            measurement = measurements[position]

            properties = row.drop(
                labels=[geometry_column]
            ).to_dict()

            features.append({
                "feature_id": _json_safe(index),
                "geometry_type": (
                    geometry.geom_type if geometry is not None else None
                ),
                "geometry": (
                    _json_safe(mapping(geometry))
                    if geometry is not None
                    else None
                ),
                "properties": _json_safe(properties),
                "measurement": _json_safe(measurement),
            })

        return {
            "filename": filename,
            "feature_count": len(gdf),
            "crs": (
                gdf.crs.to_string()
                if gdf.crs is not None
                else None
            ),
            "measurement_crs": measurement_crs.to_string(),
            "features": features,
        }


class ChunkedUpload:
    """Minimal upload stand-in for running this module without Django."""

    def __init__(self, name: str, data: bytes) -> None:
        self.name = name
        self.size = len(data)
        self._data = data

    def chunks(self, chunk_size: int = 64 * 1024) -> Iterator[bytes]:
        for offset in range(0, len(self._data), chunk_size):
            yield self._data[offset : offset + chunk_size]


def _run_self_demo() -> None:
    """Smoke-test KML ingestion when executed as a script."""
    sample_kml = b"""<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Document>
    <Placemark>
      <name>Demo parcel</name>
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
    upload = ChunkedUpload("demo.kml", sample_kml)
    result = read_geospatial_file(upload)
    print(f"Read {result['feature_count']} feature(s) from {result['filename']}")
    print(f"CRS: {result['crs']}")
    if result["features"]:
        first = result["features"][0]
        print(f"First geometry: {first['geometry_type']}")


if __name__ == "__main__":
    _run_self_demo()



