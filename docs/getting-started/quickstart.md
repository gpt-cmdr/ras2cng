# Quick Start

## 5-Step Workflow

### Step 1 — Export geometry

```bash
# HDF geometry: exports mesh cell polygons
ras2cng geometry model.g01.hdf mesh_cells.parquet --layer mesh_cells

# Text geometry: exports cross section cut lines
ras2cng geometry model.g01 cross_sections.parquet --layer cross_sections
```

Available layers: `mesh_cells`, `cross_sections`, `centerlines`

### Step 2 — Export results

```bash
# Default variable: Maximum Depth
ras2cng results model.p01.hdf max_depth.parquet

# Specific variable, joined to polygon geometry
ras2cng results model.p01.hdf max_depth.parquet \
  --geometry mesh_cells.parquet \
  --var "Maximum Depth"

# Export all available summary variables to a directory
ras2cng results model.p01.hdf ./results/ --all --geometry mesh_cells.parquet
```

### Step 3 — Query with DuckDB

```bash
# Table alias is always `_`
ras2cng query max_depth.parquet \
  "SELECT mesh_name, AVG(maximum_depth) AS avg_depth FROM _ GROUP BY mesh_name"

# Save filtered results
ras2cng query max_depth.parquet \
  "SELECT * FROM _ WHERE maximum_depth > 3.0 ORDER BY maximum_depth DESC" \
  --output deep_areas.parquet
```

### Step 4 — Generate PMTiles

```bash
# Vector PMTiles from GeoParquet (requires tippecanoe + pmtiles on PATH)
ras2cng pmtiles max_depth.parquet flood_depth.pmtiles \
  --layer flood --min-zoom 8 --max-zoom 14

# Raster PMTiles from GeoTIFF (requires gdal_translate + pmtiles on PATH)
ras2cng pmtiles results.tif results_raster.pmtiles
```

### Step 5 — Sync to PostGIS

```bash
ras2cng sync max_depth.parquet "postgresql://user:pass@localhost/mydb" max_depth \
  --schema hydraulics
```

## All CLI Commands

```bash
ras2cng --help

# Export geometry layer
ras2cng geometry GEOM_FILE OUTPUT.parquet [--layer LAYER]

# Export results
ras2cng results PLAN_HDF OUTPUT.parquet [--geometry GEOM.parquet] [--var VAR] [--all]

# Query parquet
ras2cng query INPUT.parquet "SQL" [--output result.csv]

# Generate PMTiles (requires tippecanoe + pmtiles on PATH)
ras2cng pmtiles INPUT.parquet OUTPUT.pmtiles [--layer NAME] [--min-zoom Z] [--max-zoom Z]

# Sync to PostGIS
ras2cng sync INPUT.parquet postgresql://user:pass@host/db TABLE_NAME [--schema public] [--if-exists replace]
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

# Export all variables
exported = export_all_variables(
    plan_hdf=Path("model.p01.hdf"),
    output_dir=Path("./results/"),
    geom_file=Path("mesh_cells.parquet"),
)

# Query with DuckDB
df = query_parquet(Path("max_depth.parquet"), "SELECT * FROM _ WHERE maximum_depth > 3.0")

# Advanced DuckDB session
with DuckSession() as duck:
    duck.register_parquet("max_depth.parquet")
    df = duck.query("SELECT mesh_name, MAX(maximum_depth) FROM _ GROUP BY mesh_name")
```
