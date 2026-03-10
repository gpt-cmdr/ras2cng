# Data Structure Reference

This page documents the exact output schema from a full ras2cng extraction, using
the `BaldEagleCrkMulti2D` example project bundled with ras-commander.

## Example Project: BaldEagleCrkMulti2D

A multi-area 2D unsteady dam break model on Bald Eagle Creek, Pennsylvania.
Contains 10 geometry configurations (`g01`–`g13`) and multiple plan configurations.

```
BaldEagleCrkMulti2D/
├── BaldEagleDamBrk.g01.hdf    5.0 MB  ← geometry HDF (mesh_cells)
├── BaldEagleDamBrk.g06.hdf    1.0 MB  ← geometry HDF (cross_sections + centerlines)
├── BaldEagleDamBrk.g01        2.9 MB  ← text geometry file (same geometry, ASCII)
├── BaldEagleDamBrk.p01        7.6 KB  ← plan text file (no pre-run .p01.hdf)
│   ...
└── Terrain/Projection.prj            ← CRS source (EPSG:2271)
```

**CRS**: EPSG:2271 — NAD83 / Pennsylvania North (US survey feet)

---

## Output Files from a Full Extraction

```bash
ras2cng geometry BaldEagleDamBrk.g01.hdf  mesh_cells.parquet       --layer mesh_cells
ras2cng geometry BaldEagleDamBrk.g06.hdf  cross_sections.parquet   --layer cross_sections
ras2cng geometry BaldEagleDamBrk.g06.hdf  centerlines.parquet      --layer centerlines
# After running a plan in HEC-RAS:
ras2cng results  BaldEagleDamBrk.p01.hdf  max_depth.parquet        --geometry mesh_cells.parquet
```

| Output File | Size | Rows | Geometry |
|---|---|---|---|
| `mesh_cells.parquet` | 2.5 MB | 87,039 | Polygon |
| `cross_sections.parquet` | 768 KB | 192 | LineString |
| `centerlines.parquet` | 22 KB | 1 | LineString |
| `max_depth.parquet` (joined) | ~3.5 MB | 87,039 | Polygon |
| `max_depth.parquet` (points) | ~2.0 MB | 87,039 | Point |

---

## mesh_cells — 2D Flow Area Cell Polygons

**Source**: `BaldEagleDamBrk.g01.hdf` via `HdfMesh.get_mesh_cell_polygons()`

**Real extraction**: 87,039 cells, EPSG:2271, 2.5 MB

| Column | Type | Description | Sample |
|---|---|---|---|
| `mesh_name` | `str` | Name of 2D flow area | `"BaldEagleCr"` |
| `cell_id` | `int64` | Cell index (0-based) | `0`, `1`, `2`, … |
| `geometry` | `Polygon` | Cell polygon boundary | `POLYGON ((2083650 370850, …))` |

**Mesh statistics for BaldEagleDamBrk.g01.hdf:**

| Mesh Name | Cell Count | Min Cell Area (ft²) | Max Cell Area (ft²) | Mean Cell Area (ft²) |
|---|---|---|---|---|
| `BaldEagleCr` | 87,039 | 1,441 | 25,703 | ~10,000 |

!!! note "Geometry fallback"
    If the HDF file does not contain cell polygon data, `geometry` falls back to
    `Point` at the cell centroid. Both use the same `mesh_name`/`cell_id` keys.

**Read with geopandas:**
```python
import geopandas as gpd
gdf = gpd.read_parquet("mesh_cells.parquet")
# GeoDataFrame with 87039 rows, CRS=EPSG:2271
print(gdf.dtypes)
# mesh_name    ArrowDtype(string)
# cell_id      int64
# geometry     geometry
```

**Query with DuckDB:**
```sql
-- Cell count per mesh area
SELECT mesh_name, COUNT(*) AS n_cells FROM _ GROUP BY mesh_name;

-- Cells with large area (> 20,000 ft²)
SELECT mesh_name, cell_id, ST_Area(geometry) AS area_ft2
FROM _ WHERE ST_Area(geometry) > 20000;
```

---

## cross_sections — 1D Cross Section Cut Lines

**Source**: `BaldEagleDamBrk.g06.hdf` via `HdfXsec.get_cross_sections()`

**Real extraction**: 192 cross sections, river "Bald Eagle Cr.", reach "Lock Haven"

| Column | Type | Description | Sample |
|---|---|---|---|
| `geometry` | `LineString` | XS cut line polyline | `LINESTRING (2053610 …)` |
| `River` | `str` | River name | `"Bald Eagle Cr."` |
| `Reach` | `str` | Reach name | `"Lock Haven"` |
| `RS` | `str` | River station (ft) | `"137520"`, `"136948"` |
| `Name` | `str` | Cross-section name | `"Low Water Bridge"`, `""` |
| `Description` | `str` | Description | `""` |
| `n_lob` | `float64` | Left overbank Manning's n | `0.06` |
| `n_channel` | `float64` | Main channel Manning's n | `0.04` |
| `n_rob` | `float64` | Right overbank Manning's n | `0.08` |
| `Left Bank` | `float32` | Left bank station (ft) | `3149.24` |
| `Right Bank` | `float32` | Right bank station (ft) | `3627.56` |
| `Len Left` | `float32` | LOB reach length (ft) | `478.52` |
| `Len Channel` | `float32` | Channel reach length (ft) | `571.85` |
| `Len Right` | `float32` | ROB reach length (ft) | `590.29` |
| `Friction Mode` | `str` | Friction method | `"Basic Mann n"` |
| `Contr` | `float32` | Contraction coefficient | `0.1` |
| `Expan` | `float32` | Expansion coefficient | `0.3` |
| `HP Count` | `int32` | Hydraulic table entries | `100` |
| `HP Start Elev` | `float32` | Hydraulic table start elevation | `657.31` |
| `HP Vert Incr` | `float32` | Hydraulic table vertical increment | `1.0` |
| `HP LOB Slices` | `int32` | LOB hydraulic table slices | `5` |
| `HP Chan Slices` | `int32` | Channel hydraulic table slices | `5` |
| `HP ROB Slices` | `int32` | ROB hydraulic table slices | `5` |
| `Left Levee Sta` | `object` | Left levee station | `None` |
| `Left Levee Elev` | `object` | Left levee elevation | `None` |
| `Right Levee Sta` | `object` | Right levee station | `None` |
| `Right Levee Elev` | `object` | Right levee elevation | `None` |
| `Ineff Block Mode` | `int64` | Ineffective flow mode flag | `0` |
| `Obstr Block Mode` | `int64` | Obstruction mode flag | `0` |
| `Default Centerline` | `uint8` | Default centerline flag | `0` |
| `Last Edited` | `str` | Last edit timestamp | `""` |
| `station_elevation` | `object` | Array: `[[station, elev], …]` shape `(N, 2)` | `array([[0., 849.52], [6.44, 849.26], …])` |
| `mannings_n` | `object` | Dict: `{'Mann n': array, 'Station': array}` | `{'Mann n': [0.06, 0.04, 0.08], 'Station': [0, 3149, 3627]}` |
| `ineffective_blocks` | `object` | List of ineffective flow blocks | `[]` |

!!! note "River station range"
    In BaldEagleCrkMulti2D g06, RS ranges from `-1867` to `137520` ft
    (negative stations are downstream of the model outlet).

**Text geometry cross sections** (`*.g??`) have a simpler schema:

| Column | Type | Description |
|---|---|---|
| `geometry` | `LineString` | XS cut line |
| `river` | `str` | River name |
| `reach` | `str` | Reach name |
| `station` | `str` | River station |

---

## centerlines — River/Reach Centerlines

**Source**: `BaldEagleDamBrk.g06.hdf` via `HdfXsec.get_river_centerlines()`

**Real extraction**: 1 reach, EPSG:2271, 140,133 ft long

| Column | Type | Description | Sample |
|---|---|---|---|
| `River Name` | `str` | River name | `"Bald Eagle Cr."` |
| `Reach Name` | `str` | Reach name | `"Lock Haven"` |
| `US Type` | `str` | Upstream boundary type | `"External"` |
| `US Name` | `str` | Upstream junction/boundary name | `""` |
| `DS Type` | `str` | Downstream boundary type | `"External"` |
| `DS Name` | `str` | Downstream junction/boundary name | `""` |
| `DS XS to Junction` | `float64` | Distance DS XS to junction | `NaN` |
| `Junction to US XS` | `float64` | Distance junction to US XS | `NaN` |
| `length` | `float64` | Total reach length (ft) | `140,133.5` |
| `geometry` | `LineString` | Reach centerline polyline | `LINESTRING (…)` |

**Text geometry centerlines** (`*.g??`) have a simpler schema:

| Column | Type | Description |
|---|---|---|
| `geometry` | `LineString` | Reach centerline |
| `river` | `str` | River name |
| `reach` | `str` | Reach name |

---

## Results — 2D Mesh Summary Output

**Source**: `*.p??.hdf` via `HdfResultsMesh.get_mesh_summary_output()`

!!! info "BaldEagleCrkMulti2D note"
    This example project does not include pre-run plan results (`.p??.hdf` files).
    Run the model in HEC-RAS to generate them, then use `ras2cng results`.

### Schema — Points (default, no `--geometry`)

| Column | Type | Description | Sample |
|---|---|---|---|
| `mesh_name` | `str` | Name of 2D flow area | `"BaldEagleCr"` |
| `cell_id` | `int64` | Cell index (0-based) | `0`, `1`, `2` |
| `maximum_depth` | `float64` | Maximum water depth (ft) | `4.237`, `0.0`, `12.51` |
| `geometry` | `Point` | Cell centroid | `POINT (2083650 370850)` |

### Schema — Polygons (with `--geometry mesh_cells.parquet`)

Same as above but `geometry` is a `Polygon` (joined from `mesh_cells` on `mesh_name` + `cell_id`):

| Column | Type | Description |
|---|---|---|
| `mesh_name` | `str` | Name of 2D flow area |
| `cell_id` | `int64` | Cell index (0-based) |
| `maximum_depth` | `float64` | Maximum water depth |
| `geometry` | `Polygon` | Cell polygon (from mesh_cells join) |

### All Available Variables (typical 2D unsteady plan)

ras-commander normalizes all variable names to **snake_case**:

| HEC-RAS Variable | Output Column | Units |
|---|---|---|
| `Maximum Depth` | `maximum_depth` | ft (or m) |
| `Maximum Water Surface` | `maximum_water_surface` | ft NAVD88 |
| `Minimum Water Surface` | `minimum_water_surface` | ft NAVD88 |
| `Maximum Face Velocity` | `maximum_face_velocity` | ft/s |
| `Minimum Depth` | `minimum_depth` | ft |
| `Cell Last Iteration` | `cell_last_iteration` | count |
| `Cell Max Courant` | `cell_max_courant` | dimensionless |

Use `list_available_summary_variables()` to discover what a specific plan HDF contains:

```python
from ras2cng.results import list_available_summary_variables
variables = list_available_summary_variables("BaldEagleDamBrk.p01.hdf")
# ['Maximum Depth', 'Maximum Water Surface', 'Maximum Face Velocity', ...]
```

---

## Geometry Encoding & CRS

### GeoParquet Format

All geometry is stored as **GeoParquet** (Apache Parquet + GeoArrow encoding):

- Geometry column name: `geometry`
- Encoding: Well-Known Binary (WKB) via `geopandas.to_parquet()`
- Compression: **ZSTD** for archive output (`archive_project()`), snappy for legacy single-file exports
- CRS: Preserved in parquet metadata (`geo` key)
- Archive output includes per-row bbox columns (`bbox_xmin`, `bbox_ymin`, `bbox_xmax`, `bbox_ymax`) with GeoParquet `covering` metadata for spatial predicate pushdown
- Archive output is Hilbert-sorted within each layer for optimal spatial locality

```python
import geopandas as gpd
gdf = gpd.read_parquet("mesh_cells.parquet")
print(gdf.crs)      # EPSG:2271 — NAD83 / Pennsylvania North (ftUS)
print(gdf.crs.to_epsg())  # 2271
```

### CRS Source

ras-commander detects the CRS from the HEC-RAS project in this order:

1. `Terrain/Projection.prj` (RASMapper projection file) — **most common**
2. `Geometry` HDF group projection attribute
3. `*.prj` project file

For `BaldEagleCrkMulti2D`: **EPSG:2271** (NAD83 / Pennsylvania North, US survey feet).

### Reading in DuckDB

The `DuckSession` automatically wraps WKB geometry columns with `ST_GeomFromWKB()`:

```python
from ras2cng.duckdb_session import DuckSession

with DuckSession() as duck:
    duck.register_parquet("mesh_cells.parquet")
    # geometry column is automatically available as GEOMETRY type
    df = duck.query("SELECT mesh_name, ST_Area(geometry) AS area FROM _ LIMIT 5")
```

---

## Inspecting Output Schema

### With geopandas

```python
import geopandas as gpd

gdf = gpd.read_parquet("mesh_cells.parquet")
print(gdf.dtypes)
print(gdf.crs)
print(gdf.geometry.geom_type.value_counts())
print(gdf.head(3))
```

### With pyarrow

```python
import pyarrow.parquet as pq

table = pq.read_table("cross_sections.parquet")
print(table.schema)      # all column types
print(table.num_rows)    # row count without loading data
```

### With DuckDB

```sql
-- Schema inspection
DESCRIBE SELECT * FROM read_parquet('mesh_cells.parquet');

-- Quick stats
SELECT
    COUNT(*) AS n_cells,
    COUNT(DISTINCT mesh_name) AS n_meshes,
    ROUND(AVG(ST_Area(geometry)), 0) AS avg_cell_area_ft2
FROM read_parquet('mesh_cells.parquet');
```

---

## Full Extraction Script

```python
from pathlib import Path
from ras_commander import RasExamples
from ras2cng.geometry import export_geometry_layers
from ras2cng.results import export_results_layer, list_available_summary_variables

# 1. Get the project
project_path = RasExamples.extract_project("BaldEagleCrkMulti2D")
out = Path("outputs/bald_eagle")
out.mkdir(parents=True, exist_ok=True)

# 2. Find files
geom_hdf = next(project_path.glob("*.g01.hdf"))   # BaldEagleDamBrk.g01.hdf
xs_hdf   = next(project_path.glob("*.g06.hdf"))   # has cross sections
plan_hdfs = sorted(project_path.glob("*.p??.hdf")) # empty until model is run

# 3. Export geometry
export_geometry_layers(geom_hdf, out/"mesh_cells.parquet",    layer="mesh_cells")
export_geometry_layers(xs_hdf,   out/"cross_sections.parquet", layer="cross_sections")
export_geometry_layers(xs_hdf,   out/"centerlines.parquet",    layer="centerlines")

# 4. Export results (requires running the model first)
if plan_hdfs:
    plan_hdf = plan_hdfs[0]
    variables = list_available_summary_variables(plan_hdf)
    print(f"Available variables: {variables}")

    export_results_layer(
        plan_hdf,
        out / "max_depth.parquet",
        variable="Maximum Depth",
        geom_file=out / "mesh_cells.parquet",   # join to polygons
    )

# 5. What you get
import geopandas as gpd
gdf = gpd.read_parquet(out/"mesh_cells.parquet")
print(f"mesh_cells:  {len(gdf):,} rows  |  {gdf.crs.to_epsg()}  |  {gdf.geometry.geom_type.iloc[0]}")
```
