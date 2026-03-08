<p align="center">
  <img src="assets/ras2cng_logo.svg" alt="ras2cng logo" width="420"/>
</p>

# ras2cng — RAS to Cloud Native GIS

**ras2cng** exports HEC-RAS geometry and simulation results to cloud-native geospatial formats,
eliminating the traditional geospatial tax through columnar Arrow memory structures, serverless
tile delivery, and in-process spatial analytics.

## What is "Cloud Native GIS"?

The cloud-native geospatial stack replaces legacy GIS workflows with open formats designed for
the web and analytical engines:

| Legacy | Cloud Native | Benefit |
|--------|-------------|---------|
| Shapefile / File GDB | **GeoParquet** | Columnar, Arrow-native, ZSTD-compressed |
| WMS/WFS tile servers | **PMTiles** | Serverless HTTP range requests, no tile server |
| Desktop GIS queries | **DuckDB** | In-process spatial SQL, no server needed |
| SDE / file GDB layers | **PostGIS** | Open standard, cloud-ready, GIST indexed |

## ras2cng Stack

```
HEC-RAS model files (.g??.hdf, .g??, .p??.hdf)
  → geometry.py / results.py    (parse via ras-commander → GeoDataFrame)
    → GeoParquet                (intermediate, columnar, Arrow-native, ZSTD)
      → DuckDB                 (serverless SQL analytics, spatial joins)
      → PMTiles                (serverless vector tiles via HTTP range)
      → PostGIS                (enterprise spatial database)
```

## Quick Start

```bash
pip install ras2cng

# Export mesh cell geometry
ras2cng geometry model.g01.hdf mesh_cells.parquet --layer mesh_cells

# Export max depth results joined to polygons
ras2cng results model.p01.hdf max_depth.parquet \
  --geometry mesh_cells.parquet --var "Maximum Depth"

# Query with DuckDB (use _ as table name)
ras2cng query max_depth.parquet \
  "SELECT mesh_name, AVG(maximum_depth) FROM _ GROUP BY mesh_name"

# Generate PMTiles for web visualization
ras2cng pmtiles max_depth.parquet flood_depth.pmtiles --layer flood

# Sync to PostGIS
ras2cng sync max_depth.parquet "postgresql://user:pass@host/db" max_depth
```

```python
from ras2cng import export_geometry_layers, export_results_layer, query_parquet
from pathlib import Path

# Export geometry to GeoDataFrame or GeoParquet
export_geometry_layers(Path("model.g01.hdf"), Path("mesh_cells.parquet"), layer="mesh_cells")

# Export results joined to polygon geometry
export_results_layer(
    plan_hdf=Path("model.p01.hdf"),
    output=Path("max_depth.parquet"),
    variable="Maximum Depth",
    geom_file=Path("mesh_cells.parquet"),
)

# Query with DuckDB
df = query_parquet(Path("max_depth.parquet"), "SELECT * FROM _ WHERE maximum_depth > 3.0")
```

## Key Features

- **GeoParquet export** — Mesh cell polygons, cross sections, and centerlines from HDF and text geometry files
- **Results export** — 2D mesh summary variables (Maximum Depth, WSE, Velocity, etc.) spatially joined with polygon geometry
- **DuckDB queries** — SQL analytics directly on GeoParquet files, no database server needed
- **PMTiles generation** — Vector tile pipeline via tippecanoe, serverless HTTP delivery
- **PostGIS sync** — Upload to enterprise spatial databases with automatic GIST indices
- **File type detection** — Suffix-based detection of HDF vs text geometry, plan vs geometry files

## Installation

```bash
# Core (geometry + results export)
pip install ras2cng

# All optional dependencies (DuckDB, PostGIS, PMTiles rasterio)
pip install "ras2cng[all]"
```

See [Installation](getting-started/installation.md) for full details including external CLI tools.
