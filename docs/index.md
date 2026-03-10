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

### Full Project Archive (recommended)

```bash
pip install ras2cng

# Inspect project structure (no export)
ras2cng inspect path/to/MyProject/

# Archive all geometry to consolidated GeoParquet files
ras2cng archive path/to/MyProject/ ./archive/

# Also export plan results summary variables
ras2cng archive path/to/MyProject/ ./archive/ --results

# Also convert terrain TIFFs to Cloud Optimized GeoTIFF
ras2cng archive path/to/MyProject/ ./archive/ --results --terrain
```

```python
from ras2cng import archive_project, inspect_project
from pathlib import Path

# Full project archive
manifest = archive_project(
    Path("path/to/MyProject/"),
    Path("./archive/"),
    include_results=True,
)
print(f"Exported {len(manifest.geometry)} geometry configurations")

# Inspect project without extracting
info = inspect_project(Path("path/to/MyProject/"))
print(f"{info.name}: {len(info.geom_files)} geometry files, {len(info.plan_files)} plans")
```

### Single-File Export

```bash
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

## Key Features

- **Full project archival** — Discovers all geometry configs, plan runs, and terrain; produces consolidated GeoParquet archives with `manifest.json` catalog
- **Consolidated GeoParquet** — One file per geometry source / plan, with `layer` column discriminator, ZSTD compression, bbox columns, and Hilbert spatial sorting
- **10 HDF geometry layers** — mesh_cells, mesh_areas, cross_sections, centerlines, bc_lines, breaklines, refinement_regions, reference_lines, reference_points, structures
- **Results export** — 2D mesh summary variables (Maximum Depth, WSE, Velocity, etc.) spatially joined with polygon geometry
- **DuckDB queries** — SQL analytics directly on GeoParquet files, no database server needed
- **PMTiles generation** — Vector tile pipeline via tippecanoe, serverless HTTP delivery
- **PostGIS sync** — Upload to enterprise spatial databases with automatic GIST indices
- **File type detection** — Suffix-based detection of HDF vs text geometry, plan vs geometry files

## Installation

```bash
# Core (geometry + results + project archive)
pip install ras2cng

# All optional dependencies (DuckDB, PostGIS, PMTiles rasterio)
pip install "ras2cng[all]"
```

See [Installation](getting-started/installation.md) for full details including external CLI tools.
