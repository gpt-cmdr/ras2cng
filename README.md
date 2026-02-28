# rascmdr-parquet-cli

CLI tool for exporting HEC-RAS **geometry** and **results** to **GeoParquet**, querying with **DuckDB**, generating **PMTiles**, and syncing to **PostGIS**.

Built on top of [`ras-commander`](https://github.com/gpt-cmdr/ras-commander).

## Extractable Data Types

### Geometry Layers

| Layer | Description | Geometry Type | Source Files |
|---|---|---|---|
| `mesh_cells` | 2D mesh cell polygons; falls back to cell center points if polygons are unavailable | Polygon (or Point fallback) | HDF geometry only (`*.g??.hdf`) |
| `cross_sections` | 1D cross-section cut lines | LineString | HDF geometry (`*.g??.hdf`) and text geometry (`*.g??`) |
| `centerlines` | River/reach centerlines | LineString | HDF geometry (`*.g??.hdf`) and text geometry (`*.g??`) |

Source file type detection is suffix-based:
- `*.g??.hdf` -- HDF geometry (supports all three layers including mesh cells)
- `*.g??` -- Text geometry (supports `cross_sections` and `centerlines` only)

### Results / Summary Variables

Results are exported from plan HDF files (`*.p??.hdf`). The tool reads **2D mesh summary output variables** dynamically from the HDF file structure, so any variable present in the model output can be exported. Common examples include:

- Maximum Depth
- Maximum Velocity
- Maximum Water Surface Elevation
- Minimum Water Surface Elevation
- Maximum Face Velocity
- Cell Volume

Use `--all` to export every available summary variable to separate GeoParquet files in a directory. The actual variables available depend on what was computed during the HEC-RAS simulation.

Results geometry:
- By default, results are exported as **cell center points** (as returned by ras-commander).
- When a polygon geometry GeoParquet is provided via `--geometry`, results are **spatially joined onto polygons** by matching `mesh_name` and `cell_id`, producing polygon-based output suitable for mapping.

Column names are normalized to **snake_case** by ras-commander (e.g., "Maximum Depth" becomes `maximum_depth`).

### Output Formats

| Format | Description | Requirements |
|---|---|---|
| **GeoParquet** | Primary output format, snappy compression | None (built-in) |
| **Vector PMTiles** | Vector tiles from GeoParquet via tippecanoe | `tippecanoe` on PATH |
| **Raster PMTiles** | Raster tiles from GeoTIFF via gdal_translate | `gdal_translate` and `pmtiles` on PATH |
| **PostGIS** | Sync to PostgreSQL/PostGIS table with automatic spatial index creation | PostgreSQL with PostGIS extension |
| **CSV** | Query output can be saved as CSV | None (built-in) |

### Query Capabilities

DuckDB SQL queries on any GeoParquet file with the spatial extension auto-loaded:
- WKB geometry columns are automatically converted to DuckDB GEOMETRY type
- Full support for `ST_*` spatial functions (e.g., `ST_Intersects`, `ST_Area`, `ST_Buffer`)
- Use `_` as the table name placeholder in queries
- Query output can be saved as GeoParquet or CSV

## Installation

```bash
# Base installation (GeoParquet export)
pip install rascmdr-parquet-cli

# All features (duckdb + postgis + pmtiles helpers + dev deps)
pip install "rascmdr-parquet-cli[all]"
```

## Quick Start (CLI)

### Export Geometry

```bash
# Geometry HDF
rascmdr-parquet geometry model.g01.hdf mesh_cells.parquet --layer mesh_cells

# Text geometry
rascmdr-parquet geometry model.g01 cross_sections.parquet --layer cross_sections

# River centerlines
rascmdr-parquet geometry model.g01 centerlines.parquet --layer centerlines
```

### Export Results

```bash
# Max depth (usually exported as points unless you join polygons)
rascmdr-parquet results model.p01.hdf max_depth.parquet --var "Maximum Depth"

# Join results to polygon mesh cells (recommended for mapping)
rascmdr-parquet results model.p01.hdf max_depth_poly.parquet \
  --var "Maximum Depth" \
  --geometry mesh_cells.parquet

# Export all available summary variables into a directory
rascmdr-parquet results model.p01.hdf ./results_out --all --geometry mesh_cells.parquet
```

Notes:
- ras-commander normalizes output column names to snake_case. Example: "Maximum Depth" -> `maximum_depth`.

### Query with DuckDB

```bash
# Filter by depth threshold
rascmdr-parquet query max_depth_poly.parquet \
  "SELECT * FROM _ WHERE maximum_depth > 3 ORDER BY maximum_depth DESC"

# Save results
rascmdr-parquet query max_depth_poly.parquet \
  "SELECT * FROM _ WHERE maximum_depth > 3" \
  --output deep_flooding.parquet
```

### Generate PMTiles

Vector tiles require **tippecanoe** installed and on PATH.

```bash
rascmdr-parquet pmtiles mesh_cells.parquet mesh_cells.pmtiles \
  --layer mesh_cells \
  --min-zoom 8 \
  --max-zoom 16
```

Raster tiles require **gdal_translate** and **pmtiles** CLIs.

```bash
rascmdr-parquet pmtiles depth.tif depth.pmtiles --min-zoom 10 --max-zoom 18
```

### Sync to PostGIS

```bash
rascmdr-parquet sync mesh_cells.parquet \
  "postgresql://user:pass@your-host:5432/gis_data" \
  ras_mesh_cells \
  --schema public
```

## Tickfaw Model (CLB01)

Example workflow used for validation on CLB01 (paths will vary):

```bash
# 1) Find files (PowerShell)
Get-ChildItem C:\GH\ras-commander\test_models -Recurse -Include *.g??,*.g??.hdf,*.p??.hdf,*.prj | Select-Object -First 50

# 2) Export mesh cells
rascmdr-parquet geometry "<Tickfaw>\\*.g01.hdf" tickfaw_mesh_cells.parquet --layer mesh_cells

# 3) Export max depth + join polygons
rascmdr-parquet results "<Tickfaw>\\*.p01.hdf" tickfaw_max_depth.parquet --var "Maximum Depth" --geometry tickfaw_mesh_cells.parquet

# 4) Generate PMTiles
rascmdr-parquet pmtiles tickfaw_max_depth.parquet tickfaw_max_depth.pmtiles --layer max_depth

# 5) Sync to PostGIS
rascmdr-parquet sync tickfaw_max_depth.parquet "postgresql://user:pass@your-host:5432/gis_data" tickfaw_max_depth --schema public
```

## Python API

```python
from rascmdr_parquet import export_geometry_layers, export_results_layer, DuckSession

export_geometry_layers("model.g01.hdf", "mesh_cells.parquet", layer="mesh_cells")
export_results_layer(
    "model.p01.hdf",
    "max_depth.parquet",
    variable="Maximum Depth",
    geom_file="mesh_cells.parquet",
)

df = DuckSession().register_parquet("max_depth.parquet").query(
    "SELECT * FROM _ WHERE maximum_depth > 3"
)
print(df.head())
```
