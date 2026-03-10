# Quick Start

## Full Project Archive (recommended)

The fastest way to extract everything from a HEC-RAS project:

```bash
# Inspect project structure (no export)
ras2cng inspect path/to/MyProject/

# Archive all geometry to consolidated GeoParquet files
ras2cng archive path/to/MyProject/ ./archive/

# Also export plan results summary variables
ras2cng archive path/to/MyProject/ ./archive/ --results

# Full archive with terrain COG conversion
ras2cng archive path/to/MyProject/ ./archive/ --results --terrain
```

Query layers within consolidated files:

```sql
SELECT * FROM 'MyProject.g01.parquet' WHERE layer = 'mesh_cells'
SELECT * FROM 'MyProject.p01.parquet' WHERE layer = 'maximum_depth'
```

## Single-File Workflow

### Step 1 — Export geometry

```bash
# HDF geometry: exports mesh cell polygons
ras2cng geometry model.g01.hdf mesh_cells.parquet --layer mesh_cells

# Text geometry: exports cross section cut lines
ras2cng geometry model.g01 cross_sections.parquet --layer cross_sections
```

Available HDF layers: `mesh_cells`, `mesh_areas`, `cross_sections`, `centerlines`, `bc_lines`, `breaklines`, `refinement_regions`, `reference_lines`, `reference_points`, `structures`

Available text layers: `cross_sections`, `centerlines`, `storage_areas`

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

# Project-level commands
ras2cng inspect PROJECT [--json]
ras2cng archive PROJECT OUTPUT [--results] [--terrain] [--plan-geometry] [--plans p01,p02] [--no-sort] [--fail-fast]

# Single-file commands
ras2cng geometry GEOM_FILE OUTPUT.parquet [--layer LAYER]
ras2cng results PLAN_HDF OUTPUT.parquet [--geometry GEOM.parquet] [--var VAR] [--all]
ras2cng query INPUT.parquet "SQL" [--output result.csv]
ras2cng pmtiles INPUT.parquet OUTPUT.pmtiles [--layer NAME] [--min-zoom Z] [--max-zoom Z]
ras2cng sync INPUT.parquet postgresql://user:pass@host/db TABLE_NAME [--schema public] [--if-exists replace]
```

## Python API

```python
from ras2cng import (
    # Project archival
    archive_project,
    inspect_project,
    Manifest,
    # Single-file export
    export_geometry_layers,
    export_results_layer,
    export_all_variables,
    merge_all_layers,
    merge_all_variables,
    # DuckDB
    DuckSession,
    query_parquet,
    # PMTiles & PostGIS
    generate_pmtiles_from_input,
    sync_to_postgres,
)
from pathlib import Path

# Full project archive (recommended)
manifest = archive_project(
    Path("path/to/MyProject/"),
    Path("./archive/"),
    include_results=True,
)

# Inspect project without extracting
info = inspect_project(Path("path/to/MyProject/"))
print(f"{info.name}: {len(info.geom_files)} geometry files, {len(info.plan_files)} plans")

# Single-file export
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
