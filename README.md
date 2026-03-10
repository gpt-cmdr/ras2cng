<p align="center">
  <img src="docs/assets/ras2cng_logo.svg" alt="ras2cng logo" width="420"/>
</p>

# ras2cng — RAS to Cloud Native GIS

[![PyPI version](https://badge.fury.io/py/ras2cng.svg)](https://badge.fury.io/py/ras2cng)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Documentation](https://readthedocs.org/projects/ras2cng/badge/?version=latest)](https://ras2cng.readthedocs.io/en/latest/)

Full-project archival and cloud-native export tool for HEC-RAS. Extracts geometry, results, and
terrain from any HEC-RAS project into hierarchical GeoParquet archives with a `manifest.json`
catalog — ready for DuckDB analytics, PMTiles tile delivery, and PostGIS sync.

Built on [`ras-commander`](https://github.com/gpt-cmdr/ras-commander) by [CLB Engineering Corporation](https://clbengineering.com/).

## Installation

```bash
# Core (geometry + results + project archive)
pip install ras2cng

# All optional extras (DuckDB analytics, PostGIS sync, PMTiles rasterio)
pip install "ras2cng[all]"

# Individual extras
pip install "ras2cng[duckdb]"    # DuckDB SQL analytics
pip install "ras2cng[postgis]"   # PostGIS sync
pip install "ras2cng[pmtiles]"   # rasterio (PMTiles also needs tippecanoe + pmtiles CLIs)
```

## Quick Start

### Full Project Archive (recommended)

```bash
# Inspect project structure (no export)
ras2cng inspect path/to/MyProject/

# Archive all geometry from all geometry files (default — safe, no results duplication)
ras2cng archive path/to/MyProject/ ./archive/

# Also export plan results summary variables
ras2cng archive path/to/MyProject/ ./archive/ --results

# Also convert terrain TIFFs to Cloud Optimized GeoTIFF
ras2cng archive path/to/MyProject/ ./archive/ --results --terrain
```

Output structure (consolidated parquet per source file):
```
archive/
├── manifest.json              # Project catalog (schema v2.0)
├── MyProject.parquet          # Project metadata (RasPrj dataframes, _table column)
├── MyProject.g01.parquet      # All geometry from g01 (HDF + text), layer column
├── MyProject.g06.parquet      # All geometry from g06
├── MyProject.p01.parquet      # All results from p01, layer column (--results)
└── terrain/                   # (--terrain flag)
    └── Terrain50_cog.tif
```

Query layers within consolidated files:
```sql
SELECT * FROM 'MyProject.g01.parquet' WHERE layer = 'mesh_cells'
SELECT * FROM 'MyProject.p01.parquet' WHERE layer = 'maximum_depth'
SELECT * FROM 'MyProject.parquet' WHERE _table = 'plan_df'
```

### Single-File Export

```bash
# Export mesh cell geometry from HDF
ras2cng geometry model.g01.hdf mesh_cells.parquet --layer mesh_cells

# Export max depth results joined to polygon geometry
ras2cng results model.p01.hdf max_depth.parquet \
  --geometry mesh_cells.parquet --var "Maximum Depth"

# Query with DuckDB (use _ as table name)
ras2cng query max_depth.parquet \
  "SELECT mesh_name, AVG(maximum_depth) FROM _ GROUP BY mesh_name"

# Generate PMTiles (requires tippecanoe + pmtiles on PATH)
ras2cng pmtiles max_depth.parquet flood_depth.pmtiles --layer flood --min-zoom 8 --max-zoom 14

# Sync to PostGIS
ras2cng sync max_depth.parquet "postgresql://user:pass@host/db" max_depth --schema hydraulics
```

## Python API

```python
from ras2cng import (
    archive_project,
    inspect_project,
    export_geometry_layers,
    export_results_layer,
    export_all_variables,
    DuckSession,
    query_parquet,
    generate_pmtiles_from_input,
    sync_to_postgres,
)
from pathlib import Path

# Full project archive
manifest = archive_project(
    Path("path/to/MyProject/"),
    Path("./archive/"),
    include_results=True,
    include_terrain=True,
)
print(f"Exported {len(manifest.geometry)} geometry configurations")

# Inspect project without extracting
info = inspect_project(Path("path/to/MyProject/"))
print(f"{info.name}: {len(info.geom_files)} geometry files, {len(info.plan_files)} plans")

# Single file export
export_geometry_layers(Path("model.g01.hdf"), Path("mesh_cells.parquet"), layer="mesh_cells")

# DuckDB query (table alias is always _)
df = query_parquet(Path("max_depth.parquet"), "SELECT * FROM _ WHERE maximum_depth > 3.0")
```

## Extractable Data

### Geometry Layers (from `.g##.hdf`)

| Layer | Geometry | Source |
|---|---|---|
| `mesh_cells` | Polygon (Point fallback) | `HdfMesh` |
| `mesh_areas` | Polygon | `HdfMesh` |
| `bc_lines` | LineString | `HdfBndry` |
| `breaklines` | LineString | `HdfBndry` |
| `refinement_regions` | Polygon | `HdfBndry` |
| `reference_lines` | LineString | `HdfBndry` |
| `reference_points` | Point | `HdfBndry` |
| `structures` | LineString | `HdfStruc` |
| `cross_sections` | LineString | `HdfXsec` |
| `centerlines` | LineString | `HdfXsec` |
| `storage_areas` | Polygon | Text geometry |

### Results Variables (from `.p##.hdf`, opt-in)

Exported from plan HDF files. Common 2D mesh summary variables:

- `Maximum Depth` → `maximum_depth`
- `Maximum Water Surface` → `maximum_water_surface`
- `Maximum Face Velocity` → `maximum_face_velocity`

Column names are **snake_case** (ras-commander normalization). Use `--all` flag to export every available variable.

> **Why results are opt-in:** Plan HDF files contain a copy of the geometry.
> Exporting geometry first (`archive` default), then adding `--results` avoids redundant extraction.

### Output Formats

| Format | Command | Requirements |
|---|---|---|
| GeoParquet | `geometry`, `results`, `archive` | Built-in |
| Cloud Optimized GeoTIFF | `archive --terrain` | gdal_translate CLI |
| DuckDB SQL | `query` | `pip install "ras2cng[duckdb]"` |
| Vector PMTiles | `pmtiles` | tippecanoe + pmtiles CLIs |
| Raster PMTiles | `pmtiles` | gdal_translate + pmtiles CLIs |
| PostGIS | `sync` | `pip install "ras2cng[postgis]"` |

## External CLIs for PMTiles / COG

```bash
# via conda-forge
conda install -c conda-forge tippecanoe pmtiles gdal
```

Or download from [felt/tippecanoe](https://github.com/felt/tippecanoe/releases) and [protomaps/go-pmtiles](https://github.com/protomaps/go-pmtiles/releases).

## Documentation

Full documentation: [https://ras2cng.readthedocs.io/en/latest/](https://ras2cng.readthedocs.io/en/latest/)

GitHub Pages mirror: [https://gpt-cmdr.github.io/ras2cng/](https://gpt-cmdr.github.io/ras2cng/)

## About CLB Engineering

**ras2cng** is an open-source project of [CLB Engineering Corporation](https://clbengineering.com/), the creators of [ras-commander](https://github.com/gpt-cmdr/ras-commander) and [hms-commander](https://github.com/gpt-cmdr/hms-commander).

CLB pioneered the **[LLM Forward](https://clbengineering.com/llm-forward)** approach to civil engineering — a framework where licensed professional engineers leverage Large Language Models to accelerate H&H modeling workflows while maintaining full professional responsibility.

**Contact**: [info@clbengineering.com](mailto:info@clbengineering.com) | **Website**: [clbengineering.com](https://clbengineering.com/)

## License

MIT License — see [LICENSE](LICENSE) for details.
