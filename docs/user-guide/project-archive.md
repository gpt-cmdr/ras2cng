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
├── manifest.json                         # Project catalog (schema v2.5, index metadata)
├── {ProjectName}.parquet                 # Project metadata (RasPrj dataframes, _table column)
├── {ProjectName}.g01.parquet             # All geometry from g01 (HDF + text layers)
├── {ProjectName}.g06.parquet             # All geometry from g06
├── {ProjectName}.p01.parquet             # All results from p01, layer column (--results)
└── terrain/                              # Only with --terrain
    └── Terrain50_cog.tif
```

Large 2D projects can also use a partitioned results layout:

```
{output_dir}/
├── {ProjectName}.g01.parquet
└── results/
    └── p01/
        ├── maximum_depth.parquet
        └── maximum_water_surface.parquet
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
- **Per-row bbox columns** — `bbox_xmin`, `bbox_ymin`, `bbox_xmax`, `bbox_ymax` with GeoParquet `covering` metadata for spatial predicate pushdown; the metadata is preserved during post-processing
- **Hilbert spatial indexing** — The default post-processing pass adds `hilbert_index` to GeoParquet geometry and sorts by `layer,hilbert_index` (disable with `--no-sort`)
- **Result join indexes** — Geometryless result tables get `join_index`; when matching mesh geometry exists, they inherit `hilbert_index` through `mesh_name` plus `cell_id` or `face_id`
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

# Large-project-safe selected results: one table per plan/variable, no repeated geometry
ras2cng archive path/to/project /output/archive --results \
  --result-variables maximum_depth,maximum_water_surface \
  --results-layout variable \
  --results-geometry none \
  --no-sort

# Full archive
ras2cng archive path/to/project /output/archive --results --terrain

# Generate result rasters (WSE, Depth, Velocity) alongside the archive
ras2cng archive path/to/project /output/archive --results --map

# Generate rasters with a specific render mode
ras2cng archive path/to/project /output/archive --results --map --render-mode sloping

# Create one authoritative COG per named RASMapper terrain
ras2cng archive path/to/project /output/archive --terrain --consolidate-terrain

# Set an explicit target for a mixed-resolution named terrain
ras2cng archive path/to/project /output/archive --terrain --consolidate-terrain \
  --terrain-resolution "Existing Terrain=6"

# Skip spatial post-processing during extraction
ras2cng archive path/to/project /output/archive --no-sort

# Post-process an existing archive later
ras2cng spatial-index /output/archive

# Fail fast on any extraction error (default: skip and continue)
ras2cng archive path/to/project /output/archive --fail-fast
```

---

## Large 2D Result Archives

The default `--results` mode writes one GeoParquet per plan and repeats geometry
inside each result variable. That is convenient for direct map rendering, but it
can create large memory spikes on multi-million-cell 2D meshes.

For large projects, prefer:

- `--results-layout variable` — writes one parquet per plan/variable, so ras2cng
  does not hold every variable for a plan in memory at once.
- `--results-geometry none` — writes attribute tables keyed by `mesh_name` plus
  `cell_id` or `face_id`. Mesh geometry remains in the geometry parquet once.
- `--result-variables ...` — restricts export to display or analysis variables
  that are actually needed.
- `--no-sort` — skips the default post-processing pass when the extraction
  worker is memory constrained. Run `ras2cng spatial-index /output/archive`
  later on a worker with enough RAM to add `hilbert_index` to geometry and
  `join_index` or inherited `hilbert_index` to geometryless result tables
  without re-extracting the model.

Geometryless result tables are not directly renderable by MapLibre as a layer.
They are the canonical DRY storage form. A viewer or tile service should join
them to the geometry parquet on `mesh_name, cell_id` for cell variables or
`mesh_name, face_id` for face variables. If a standalone renderable layer is
required, use `--results-geometry point` or `--results-geometry polygon` for a
small selected subset of variables, or generate derived map rasters/tiles.

When spatial post-processing is enabled (the default for `archive_project()`
unless `sort=False` or `--no-sort` is used), geometry parquet files are sorted
by `layer,hilbert_index` and keep their GeoParquet bbox `covering` metadata.
Geometryless result tables are sorted by matching geometry `hilbert_index` when
the archive has a joinable `mesh_cells` or `mesh_faces` layer; otherwise they
fall back to deterministic `mesh_name` plus cell/face key ordering. The manifest
records `index_status=spatial_join` for inherited spatial ordering and
`index_status=join_key` for key-only ordering.

Auxiliary raw result summaries are enabled by default with `--results`. They cover reference
points/lines, SA/2D connections, pumps, pipe conduits/nodes, and 1D structures when those
datasets exist. The exporter reduces time-series datasets in bounded HDF chunks and records
the source geometry join instead of duplicating geometry. Use `--mesh-results-only` only for
an intentionally limited archive.

---

## Named Terrain Policy

Terrain names in the RASMapper configuration are separate surfaces. ras2cng never merges
different named terrains implicitly. With `--consolidate-terrain`, every named terrain gets
its own authoritative COG, source-inventory provenance JSON, source TIFF footprint
GeoParquet, and any available terrain-modification construction vectors.

The default publication resolution follows a no-upsample policy:

- Native cells finer than 5 ft are reduced to the smallest whole-number native-cell multiple
  at or above 5 ft.
- Native cells at or above 5 ft retain their native resolution, including 30-ft terrain.
- A named terrain containing mixed native cell sizes requires an explicit
  `--terrain-resolution NAME=VALUE` decision. The target must be a whole-number multiple
  of the coarsest source cell. This avoids upsampling without forcing an impractical common
  multiple for mosaics that mix grids such as 2-foot and 1-meter tiles.
- Every source resolution and resampling factor is retained in the terrain provenance JSON.
- The merge is target-grid and windowed, with the first RASMapper source winning where TIFFs
  overlap. Memory use scales with the processing block rather than the full mosaic.

The output uses transparent nodata, tiled COG storage, ZSTD compression, and overviews. This
keeps the numerical terrain usable for Identify and analysis while supporting byte-range web
delivery.

---

## 1D Steady Result Archives

Computed steady-flow plan HDFs are archived as raw cross-section records rather than as
interpolated surfaces. Each row retains the source `river`, `reach`, `node_id`, and `profile`,
with the HDF values available at that element, such as WSEL, flow, total top width, and total
flow area. Optional variables that are not stored in a row-aligned HDF dataset remain null.

Use the same memory-safe archive command for a 1D steady model:

```bash
ras2cng archive path/to/SteadyModel /output/archive --results \
  --results-layout variable --results-geometry none --crs EPSG:2249
```

The result table is stored as `results/pNN/steady_cross_sections.parquet`. Its manifest entry
declares the exact viewer join: `River -> river`, `Reach -> reach`, and `RS -> node_id`.
No terrain or floodplain surface is fabricated. RASMapper Stored Map COGs are the separate,
authoritative delivery path when an interpolated raster display is available.

Use `--crs` only when that CRS has been independently verified for the project. It stamps the
source geometry GeoParquet and archive manifest when a public release omits a `.rasmap` file or
HDF projection record; it does not reproject or infer coordinates.

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
    sort=False,                  # Skip spatial post-processing
    skip_errors=False,           # Fail fast on errors
)
```

---

## manifest.json Schema

Every archive includes a `manifest.json` that catalogs all exported layers for downstream tooling (DuckDB, PostGIS sync, PMTiles generation, etc.). Schema 2.5 records spatial post-processing metadata, composite raw-result joins, named-terrain provenance, terrain source footprints, and terrain-modification vectors.

```json
{
  "schema_version": "2.5",
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
          "crs": "EPSG:2271",
          "hilbert_index": "hilbert_index",
          "sort_order": "layer,hilbert_index",
          "bbox_columns": ["bbox_xmin", "bbox_ymin", "bbox_xmax", "bbox_ymax"]
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
      "parquet": "",
      "layout": "variable",
      "geometry_mode": "none",
      "variables": [
        {
          "variable": "maximum_depth",
          "filter_value": "maximum_depth",
          "rows": 87039,
          "parquet": "results/p01/maximum_depth.parquet",
           "geometry_mode": "none",
           "index_column": "cell_id",
           "geometry_filter": "mesh_cells",
           "join_columns": {},
           "profile_column": "",
           "source": "",
          "hilbert_index": "hilbert_index",
          "join_index": "join_index",
          "sort_order": "hilbert_index",
          "index_status": "spatial_join",
          "size_bytes": 3145728
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
      "crs": "EPSG:2271",
      "terrain_name": "Terrain50",
      "source_files": ["Terrain/Terrain50.tile-01.tif"],
      "target_resolution": 5.0,
      "horizontal_units": "Feet",
      "provenance_file": "terrain/Terrain50_terrain-provenance.json",
      "authoritative": true
    }
  ],
  "terrain_sources": [
    {
      "terrain_name": "Terrain50",
      "layers": [{"layer": "terrain_source_footprints", "parquet": "terrain/sources/terrain50/terrain_source_footprints.parquet"}]
    }
  ],
  "terrain_modifications": [
    {
      "terrain_name": "Terrain50",
      "layers": [{"layer": "terrain_modification_lines", "parquet": "terrain/modifications/terrain50/terrain_modification_lines.parquet"}]
    }
  ],
  "postprocessing": {
    "spatial_index": {
      "hilbert_column": "hilbert_index",
      "join_index_column": "join_index",
      "hilbert_level": 16,
      "geometry_file_count": 1,
      "result_file_count": 1,
      "error_count": 0
    }
  }
}
```

### Using the manifest with Python

```python
from ras2cng import Manifest

m = Manifest.load(Path("/output/archive/manifest.json"))
print(m.geom_ids)      # ['g01', 'g06', ...]
print(m.plan_ids)      # ['p01', 'p02', ...]

# List geometry and result parquet files
print(m.layer_paths())   # ['BaldEagle.g01.parquet', ...]
print(m.result_paths())  # ['BaldEagle.p01.parquet', 'results/p01/maximum_depth.parquet', ...]

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
| `--no-sort` | Skip default spatial post-processing; run `ras2cng spatial-index ARCHIVE_DIR` later |
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
