# Project Archive

The `archive` and `inspect` commands treat an entire HEC-RAS project as a single unit — discovering all geometry configurations, plan runs, and terrain rasters and exporting them to a structured, cloud-native archive.

## Why Project-Level Archival?

A HEC-RAS project typically contains:

- **Multiple geometry configurations** (`.g01`, `.g02`, …) — each may define different mesh resolutions, channel cross-sections, or land cover representations
- **Multiple plan runs** (`.p01`, `.p02`, …) — dam-break scenarios, design storms, calibration runs, etc.
- **Terrain rasters** (`Terrain/*.tif`) — the underlying DEM used to compute depths
- **Plan result HDFs** (`.p##.hdf`) — contain a _copy_ of the geometry plus computed summary variables

!!! note "Results not exported by default"
    Plan HDF files embed a full copy of the geometry they used. Exporting plan results without a clear need doubles your storage. Use `--results` explicitly when you want result variables (maximum depth, velocity, etc.).

---

## Output Directory Structure

Each geometry source and plan produces one **consolidated** GeoParquet file with a `layer` column
to distinguish different geometry types or result variables. This enables efficient DuckDB queries
with `WHERE layer = 'mesh_cells'` and avoids directory proliferation.

```
{output_dir}/
├── manifest.json                         # Project catalog (schema v2.0, always written)
├── {ProjectName}.parquet                 # Project metadata (RasPrj dataframes, _table column)
├── {ProjectName}.g01.parquet             # All geometry from g01 (HDF + text layers)
├── {ProjectName}.g06.parquet             # All geometry from g06
├── {ProjectName}.p01.parquet             # All results from p01, layer column (--results)
└── terrain/                              # Only with --terrain
    └── Terrain50_cog.tif
```

### Querying consolidated files

```sql
-- Filter by layer column for homogeneous geometry types
SELECT * FROM 'BaldEagle.g01.parquet' WHERE layer = 'mesh_cells'
SELECT * FROM 'BaldEagle.g01.parquet' WHERE layer = 'bc_lines'
SELECT * FROM 'BaldEagle.g01.parquet' WHERE layer = 'cross_sections_text'

-- Results by variable
SELECT * FROM 'BaldEagle.p01.parquet' WHERE layer = 'maximum_depth'

-- Project metadata
SELECT * FROM 'BaldEagle.parquet' WHERE _table = 'plan_df'

-- List available layers in a file
SELECT DISTINCT layer FROM 'BaldEagle.g01.parquet'
```

### GeoParquet features

- **ZSTD compression** — Better compression ratios than snappy
- **Per-row bbox columns** — `bbox_xmin`, `bbox_ymin`, `bbox_xmax`, `bbox_ymax` with GeoParquet `covering` metadata for spatial predicate pushdown
- **Hilbert spatial sorting** — Rows sorted by Hilbert curve within each layer for optimal spatial locality (disable with `--no-sort`)
- **Text layer suffix** — Text geometry layers get a `_text` suffix (e.g., `cross_sections_text`) to avoid collision with HDF layers

---

## Commands

### `inspect` — Discover project structure

Prints a summary of what's in a project without extracting any data.

```bash
ras2cng inspect path/to/BaldEagleCrkMulti2D
ras2cng inspect path/to/BaldEagleCrkMulti2D.prj
ras2cng inspect path/to/project --json    # JSON output for scripting
```

**Output includes:**
- Project name, CRS, units
- Table of geometry files (id, file, type, size)
- Table of plan files (id, geometry, flow file, HDF status)
- Terrain file count and total size

### `archive` — Export project to consolidated GeoParquet archive

```bash
# Geometry only (default — safe, fast)
ras2cng archive path/to/BaldEagleCrkMulti2D /output/bald_eagle

# Include plan result variables
ras2cng archive path/to/project /output/archive --results

# Include terrain COG conversion
ras2cng archive path/to/project /output/archive --terrain

# Also extract geometry copy from plan HDF files
ras2cng archive path/to/project /output/archive --plan-geometry

# Specific plans only
ras2cng archive path/to/project /output/archive --results --plans p01,p02

# Full archive
ras2cng archive path/to/project /output/archive --results --terrain

# Generate result rasters (WSE, Depth, Velocity) alongside the archive
ras2cng archive path/to/project /output/archive --results --map

# Generate rasters with a specific render mode
ras2cng archive path/to/project /output/archive --results --map --render-mode sloping

# Consolidate terrains into a single COG
ras2cng archive path/to/project /output/archive --terrain --consolidate-terrain

# Disable Hilbert spatial sorting
ras2cng archive path/to/project /output/archive --no-sort

# Fail fast on any extraction error (default: skip and continue)
ras2cng archive path/to/project /output/archive --fail-fast
```

---

## Python API

```python
from pathlib import Path
from ras2cng import archive_project, inspect_project, ProjectInfo

# Inspect without extracting
info = inspect_project(Path("BaldEagleCrkMulti2D"))
print(info.name, info.crs, info.units)
for g in info.geom_files:
    print(g.geom_id, g.hdf_path)

# Archive geometry only
manifest = archive_project(
    Path("BaldEagleCrkMulti2D"),
    Path("/output/archive"),
)
print(f"Wrote {len(manifest.geometry)} geometry entries")
print(f"manifest.json at /output/archive/manifest.json")

# Archive with results
manifest = archive_project(
    Path("project.prj"),
    Path("/output/archive"),
    include_results=True,
    plans=["p01", "p03"],    # None = all plans
)

# Archive with terrain COG conversion
manifest = archive_project(
    Path("project.prj"),
    Path("/output/archive"),
    include_terrain=True,
)

# Archive with result raster generation
manifest = archive_project(
    Path("project.prj"),
    Path("/output/archive"),
    include_results=True,
    map_results=True,
    render_mode="horizontal",  # or "sloping", "slopingPretty"
)

# Archive with custom options
manifest = archive_project(
    Path("project.prj"),
    Path("/output/archive"),
    include_results=True,
    include_terrain=True,
    include_plan_geometry=True,  # Also extract geometry from plan HDFs
    sort=False,                  # Disable Hilbert sorting
    skip_errors=False,           # Fail fast on errors
)
```

---

## manifest.json Schema

Every archive includes a `manifest.json` that catalogs all exported layers for downstream tooling (DuckDB, PostGIS sync, PMTiles generation, etc.).

```json
{
  "schema_version": "2.0",
  "project": {
    "name": "BaldEagleDamBrk",
    "prj_file": "BaldEagleDamBrk.prj",
    "source_path": "/abs/path/to/project",
    "archive_path": "/abs/path/to/archive",
    "created_at": "2026-03-04T15:30:00Z",
    "crs": "EPSG:2271",
    "units": "US Survey Feet",
    "plan_count": 6,
    "geom_count": 13
  },
  "project_parquet": "BaldEagleDamBrk.parquet",
  "geometry": [
    {
      "geom_id": "g01",
      "source_file": "BaldEagleDamBrk.g01.hdf",
      "file_type": "hdf+text",
      "parquet": "BaldEagleDamBrk.g01.parquet",
      "plans_using": ["p01", "p05"],
      "layers": [
        {
          "layer": "mesh_cells",
          "filter_value": "mesh_cells",
          "rows": 87039,
          "geometry_type": "Polygon",
          "crs": "EPSG:2271"
        },
        {
          "layer": "bc_lines",
          "filter_value": "bc_lines",
          "rows": 12,
          "geometry_type": "LineString",
          "crs": "EPSG:2271"
        },
        {
          "layer": "cross_sections_text",
          "filter_value": "cross_sections_text",
          "rows": 192,
          "geometry_type": "LineString",
          "crs": "EPSG:2271"
        }
      ],
      "size_bytes": 2621440
    }
  ],
  "results": [
    {
      "plan_id": "p01",
      "plan_title": "Dam Break Scenario",
      "geom_id": "g01",
      "flow_id": "u01",
      "hdf_exists": true,
      "completed": true,
      "parquet": "BaldEagleDamBrk.p01.parquet",
      "variables": [
        {
          "variable": "maximum_depth",
          "filter_value": "maximum_depth",
          "rows": 87039
        }
      ],
      "size_bytes": 3145728
    }
  ],
  "terrain": [
    {
      "source_file": "Terrain/Terrain50.tif",
      "cog_file": "terrain/Terrain50_cog.tif",
      "size_bytes": 12582912,
      "crs": "EPSG:2271"
    }
  ]
}
```

### Using the manifest with Python

```python
from ras2cng import Manifest

m = Manifest.load(Path("/output/archive/manifest.json"))
print(m.geom_ids)      # ['g01', 'g06', ...]
print(m.plan_ids)      # ['p01', 'p02', ...]

# List consolidated parquet files
print(m.layer_paths())   # ['BaldEagle.g01.parquet', ...]
print(m.result_paths())  # ['BaldEagle.p01.parquet', ...]

# Browse geometry layers within each consolidated file
for entry in m.geometry:
    print(f"{entry['geom_id']}: {entry['parquet']}")
    for layer in entry["layers"]:
        print(f"  WHERE layer = '{layer['filter_value']}' → {layer['rows']} rows")
```

---

## When to Use Each Flag

| Flag | When to use |
|---|---|
| _(default, no flags)_ | Archiving geometry for GIS analysis, tile generation, or multi-project inventory |
| `--results` | You need flood depth/velocity maps and the model has been run |
| `--terrain` | You need the DEM in cloud-optimized format for raster tile generation |
| `--map` | Generate result rasters (WSE, Depth, Velocity) via RasStoreMapHelper |
| `--consolidate-terrain` | Merge multiple terrain TIFFs into a single COG |
| `--render-mode` | Set water surface render mode: `horizontal`, `sloping`, or `slopingPretty` |
| `--plan-geometry` | Also extract the geometry copy embedded in plan HDF files |
| `--plans p01,p02` | Only specific scenarios are relevant (saves time/space on large projects) |
| `--no-sort` | Disable Hilbert spatial sorting (on by default) |
| `--fail-fast` | Debugging extraction issues; default is to skip and continue |
| `--ras-version` | Specify HEC-RAS version for RasProcess mapping |
| `--rasprocess` | Path to HEC-RAS install directory (required on Linux/Wine) |

---

## Querying with DuckDB

After archiving, query consolidated files using the `layer` column:

```python
from ras2cng import DuckSession

with DuckSession() as db:
    db.register_parquet("/output/archive/BaldEagle.g01.parquet")

    # List available layers
    df = db.query("SELECT DISTINCT layer FROM _")
    print(df)

    # Query a specific layer
    df = db.query("""
        SELECT COUNT(*) as n, AVG(ST_Area(geometry)) as avg_area
        FROM _ WHERE layer = 'mesh_cells'
    """)
    print(df)
```

See [DuckDB Queries](duckdb-queries.md) for more examples.
