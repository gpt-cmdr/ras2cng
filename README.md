# ras2cng — RAS to Cloud Native GIS

[![PyPI version](https://badge.fury.io/py/ras2cng.svg)](https://badge.fury.io/py/ras2cng)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

CLI tool for exporting HEC-RAS **geometry** and **results** to cloud-native GIS formats:
**GeoParquet**, **DuckDB**, **PMTiles**, and **PostGIS**.

Built on [`ras-commander`](https://github.com/gpt-cmdr/ras-commander) for HEC-RAS file parsing.

## Installation

```bash
# Core (geometry + results export)
pip install ras2cng

# All optional extras (DuckDB analytics, PostGIS sync, PMTiles rasterio)
pip install "ras2cng[all]"

# Individual extras
pip install "ras2cng[duckdb]"    # DuckDB SQL analytics
pip install "ras2cng[postgis]"   # PostGIS sync
pip install "ras2cng[pmtiles]"   # rasterio (PMTiles also needs tippecanoe + pmtiles CLIs)
```

## Quick Start

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
    export_geometry_layers,
    export_results_layer,
    export_all_variables,
    DuckSession,
    query_parquet,
    generate_pmtiles_from_input,
    sync_to_postgres,
)
from pathlib import Path

# Export geometry
export_geometry_layers(Path("model.g01.hdf"), Path("mesh_cells.parquet"), layer="mesh_cells")

# Export results joined to polygon geometry
export_results_layer(
    plan_hdf=Path("model.p01.hdf"),
    output=Path("max_depth.parquet"),
    variable="Maximum Depth",
    geom_file=Path("mesh_cells.parquet"),
)

# DuckDB query (table alias is always _)
df = query_parquet(Path("max_depth.parquet"), "SELECT * FROM _ WHERE maximum_depth > 3.0")

# Advanced DuckDB session with spatial
with DuckSession() as duck:
    duck.register_parquet("max_depth.parquet")
    df = duck.query("SELECT mesh_name, MAX(maximum_depth) FROM _ GROUP BY mesh_name")
```

## Extractable Data

### Geometry Layers

| Layer | Geometry | Source |
|---|---|---|
| `mesh_cells` | Polygon (Point fallback) | HDF geometry only (`*.g??.hdf`) |
| `cross_sections` | LineString | HDF geometry + text geometry (`*.g??`) |
| `centerlines` | LineString | HDF geometry + text geometry |

### Results Variables

Exported from plan HDF files (`*.p??.hdf`). Common 2D mesh summary variables:

- `Maximum Depth` → `maximum_depth`
- `Maximum Water Surface` → `maximum_water_surface`
- `Maximum Velocity` → `maximum_velocity`

Column names are **snake_case** (ras-commander normalization). Use `--all` to export every available variable.

### Output Formats

| Format | Command | Requirements |
|---|---|---|
| GeoParquet | `geometry`, `results` | Built-in |
| DuckDB SQL | `query` | `pip install "ras2cng[duckdb]"` |
| Vector PMTiles | `pmtiles` | tippecanoe + pmtiles CLIs |
| Raster PMTiles | `pmtiles` | gdal_translate + pmtiles CLIs |
| PostGIS | `sync` | `pip install "ras2cng[postgis]"` |

## External CLIs for PMTiles

```bash
# via conda-forge
conda install -c conda-forge tippecanoe pmtiles
```

Or download from [felt/tippecanoe](https://github.com/felt/tippecanoe/releases) and [protomaps/go-pmtiles](https://github.com/protomaps/go-pmtiles/releases).

## Documentation

Full documentation: [https://gpt-cmdr.github.io/ras2cng/](https://gpt-cmdr.github.io/ras2cng/)

## License

MIT License — see [LICENSE](LICENSE) for details.
