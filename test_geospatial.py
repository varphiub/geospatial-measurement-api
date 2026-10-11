import fiona
import geopandas as gpd 
import pyogrio
import shapely
from collections.abc import Mapping
from typing import cast

from pyproj import Transformer
from shapely.geometry import Polygon, LineString
from shapely.ops import transform


# 1. Create a sample land parcel using longitude and latitude.
polygon = Polygon([
    (77.5900, 12.9700),
    (77.5910, 12.9700),
    (77.5910, 12.9710),
    (77.5900, 12.9710),
    (77.5900, 12.9700),
])

# 2. Create a sample road segment.
line = LineString([
    (77.5900, 12.9700),
    (77.5910, 12.9710),
])

# 3. Define the coordinate transformation.
# EPSG:4326 = longitude/latitude in degrees.
# EPSG:32643 = UTM Zone 43N, with coordinates in metres.
transformer = Transformer.from_crs(
    "EPSG:4326",
    "EPSG:32643",
    always_xy=True,
)

import numpy as np
project_geometry = lambda geometry: shapely.transform(
    geometry,
    lambda coords: np.column_stack(transformer.transform(coords[:, 0], coords[:, 1])),
)



# 4. Transform both geometries into projected coordinates.
projected_polygon = project_geometry(polygon)
projected_line = project_geometry(line)

# 5. Calculate measurements in projected coordinates.
area_m2 = projected_polygon.area
length_m = projected_line.length

print("GeoPandas version:", gpd.__version__)
print("Shapely version:", shapely.__version__)
print("Polygon type:", polygon.geom_type)
print("Line type:", line.geom_type)
print(f"Polygon area: {area_m2:.2f} m²")
print(f"Polygon area: {area_m2 / 10000:.4f} hectares")
print(f"Line length: {length_m:.2f} m")

# 6. Check whether the installed GDAL engines expose KML support.
print("Pyogrio KML driver:", pyogrio.list_drivers().get("KML"))
fiona_drivers = cast(Mapping[str, str], fiona.supported_drivers)
print("Fiona KML driver:", fiona_drivers.get("KML"))

