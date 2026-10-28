# AGENTS.md

This file provides guidance to Codex (Codex.ai/code) when working with code in this repository.

## Project Overview

**ras2cng** (RAS to Cloud Native GIS) — CLI tool for exporting HEC-RAS geometry and results to GeoParquet, querying with DuckDB, generating PMTiles vector/raster tiles, and syncing to PostGIS. Supports **full-project archival**: discovers all geometry configurations, plan runs, and terrain rasters in a HEC-RAS project and exports them to a structured, cloud-native archive with `manifest.json` catalog.

Built on [`ras-commander`](https://github.com/gpt-cmdr/ras-commander) for HEC-RAS file parsing.

## Build & Development Commands

```bash
# Install with all optional dependencies (creates/updates .venv and uv.lock)
uv sync --all-extras

# Run tests
uv run pytest

# Run a single test
uv run pytest tests/test_geometry_detection.py -v

# Run the CLI
uv run ras2cng --help

# Archive a full project (geometry only, then with results)
uv run ras2cng inspect path/to/project
uv run ras2cng archive path/to/project /output/dir
uv run ras2cng archive path/to/project /output/dir --results --terrain

# Build distributable wheel
uv build

# Add a runtime dependency
uv add somepackage

# Add a dev-only dependency
uv add --dev somepackage
```

Python >= 3.10 required (`.python-version` pins 3.12). Virtual environment is managed by uv in `.venv/`.

## Architecture

**CLI layer** (`cli.py`): Typer app with 10 commands (`inspect`, `archive`, `geometry`, `results`, `query`, `pmtiles`, `sync`, `terrain`, `map`, `terrain-mod`, `mannings`). Uses lazy imports — heavy dependencies are imported inside command functions, not at module level.

**Core modules** — each handles one concern:
- `project.py` — Full-project orchestration. `archive_project()` produces consolidated GeoParquet files (one per geometry source, one per plan) with `layer` discriminator column. `export_project_metadata()` writes RasPrj dataframes to a plain Parquet with `_table` column. `_write_geoparquet()` helper adds per-row bbox columns and ZSTD compression with GeoParquet `covering` metadata. `inspect_project()` returns a `ProjectInfo` dataclass (no file extraction). **Note**: `init_ras_project` and `export_all_variables` are imported at module level (not lazy) to enable mock patching in tests.
- `catalog.py` — Schema v2.0. `Manifest` dataclass manages `manifest.json`. `ManifestGeomEntry` has a `parquet` field (single consolidated file) and `layers` list with `filter_value` for each layer. `ManifestPlanEntry` similarly has a `parquet` field. `project_parquet` field on `Manifest` references the metadata file.
- `geometry.py` — Detects file type (HDF `.g??.hdf` vs text `.g??`) and routes to ras-commander parsers. `HDF_LAYERS` dict maps 10 layer names to `(class, method_name)` for dispatch. `merge_all_layers()` consolidates all HDF + text layers into a single GeoDataFrame with `layer` column (text layers get `_text` suffix). `_hilbert_sort()` uses DuckDB `ST_Hilbert`; before 0.13 it called a function DuckDB does not have and silently did nothing (the spatial-index post-pass sorted instead). Legacy `export_all_hdf_layers()` / `export_all_text_layers()` remain for backward compat.
- `results.py` — Exports 2D mesh summary variables from plan HDF files. `merge_all_variables()` consolidates all variables into a single GeoDataFrame with `layer` column. Accepts `mesh_cells_gdf` GeoDataFrame directly for polygon join (avoids write-then-read).
- `duckdb_session.py` — `DuckSession` class wraps DuckDB with auto-loaded spatial extension. `register_parquet()` detects WKB geometry columns and converts them to DuckDB GEOMETRY type. The table alias is always `_`.
- `pmtiles.py` — Dispatches between vector (GeoParquet → GeoJSON → tippecanoe → PMTiles, or GeoParquet → freestiler → PMTiles) and raster (GeoTIFF → gdal_translate → pmtiles) pipelines. Requires external CLIs: `tippecanoe`, `gdal_translate`, `pmtiles`; vectors can instead use the optional `ras2cng[freestiler]` extra, selected by `RAS2CNG_VECTOR_TILER` (`vector_tiles.py`). Vector tiles never drop features to cap tile size.
- `raster_tiles.py` — **Raster tiler backend selection for every raster PMTiles writer** (terrain, Stored Maps, calculated layers, `pmtiles` on a GeoTIFF). `RAS2CNG_RASTER_TILER` = `auto` (default: terrio when installed and the raster is under `RAS2CNG_TERRIO_MAX_PIXELS`, 400 M) | `terrio` (pure-Rust, in-process; no GDAL/pmtiles CLIs) | `gdal` (the gdaldem→gdalwarp→gdal_translate→gdaladdo→`pmtiles convert` chain, streams any size). Measured on Bald Eagle: terrain 6.8 s vs 13.4 s, depth 1.6 s vs 4.7 s. terrio result overviews use `NEAREST`, holding visible wet area at 1.000× at z10 (GDAL chain: 1.225×), and its terrain edges carry ramp colour (the GDAL chain's were black despite the alpha-0 ramp trick, because `gdalwarp -dstalpha` writes black RGB). Terrain uses `AVERAGE`; override with `RAS2CNG_RASTER_TILE_RESAMPLING`. `package_maplibre_terrain(hillshade=True)` (default; CLI `--no-hillshade`) multiplies a terrio multidirectional hillshade into the display colours. It shades only visible pixels, because darkening RGB under alpha 0 brings the edge fringe back. It is not gdaldem-identical, which is accepted for display. Tests pin `gdal` via an autouse fixture in `tests/conftest.py`, because the older raster tests mock the GDAL subprocesses. With a streaming terrio (`dem` has `max_memory_mib` and `hillshade_strength`), relief or one-pass shaded relief goes file to file, the warp is chunked when `warp` has `max_memory_mib`, and PMTiles stream from the lazily opened warp. Tiles are byte-identical to the in-memory path and there is no pixel cap (Terrain50 z14: 13.4 GB -> 1.0 GB peak). `RAS2CNG_TERRIO_MAX_MEMORY_MIB` bounds the strips.
- `geoparquet.py` — **Every archive GeoParquet goes through `write_geoparquet()`.** It writes a GeoParquet 1.1 `bbox` struct covering (manifest 2.7; older archives have flat `bbox_xmin`… and every reader accepts both), ZSTD level 3 with dictionary encoding on string columns only, and row groups of 65,536 rows that also end at every layer boundary. Per-row-group dictionaries on unique numeric columns cost 12–15% and ZSTD level 1 another 15%. Rows are Hilbert-ordered within each layer by DuckDB `ST_Hilbert` of the bbox centre over the layer extent; a bit-identical NumPy port is used, with a warning, only when DuckDB spatial is missing. With sorting on, the writer stores `hilbert_index` and the spatial-index pass leaves the file alone. fast-hdf's `export_geoparquet_ipc_files` (fast-hdf #36/#38/#39) writes identical rows, order, schema and metadata and is the **default**: `RAS2CNG_GEOPARQUET_WRITER=auto` (= `prefer-native`) uses it when the installed build accepts the file and otherwise falls back to pyarrow, warning once per reason and recording it in `manifest.geometry[].geoparquet.fallback_reason`; `native` is strict, `python` never tries it. The default is one switch, `geoparquet.AUTO_PREFERS_NATIVE`. `_contract_type()` mirrors the merged engine: float32, tz-naive ms/us/ns timestamps, top-level null, and list/struct nesting pass; large_list, fixed_size_list and tz-aware timestamps stay on pyarrow. Both engines name list items `element` (pyarrow's compliant default). ras2cng requests `statistics: "chunk"` (pyarrow's statistics, no page index) when the build accepts it. Measured on Bald Eagle p07 (loaded host): native files +1.4% (+4.4% with the Rust default page indexes), and the archive is **not faster** (55-67 s vs 49-54 s): the Rust write (10-15 s) replaces pyarrow's write plus Python Hilbert keys (~10 s), and Arrow IPC staging adds ~5 s; most time is the ras-commander geometry read/merge (10-14 s), frame-to-Arrow (5 s) and row-identity checks (3 s). Never call `gdf.to_parquet` for archive files.
- `postgis_sync.py` — GeoParquet → PostGIS via SQLAlchemy/GeoAlchemy2 with automatic spatial index creation.
- `terrain.py` — Terrain discovery, consolidation, and raster export. `discover_terrains()` finds terrain layers from rasmap. `consolidate_terrain()` provides a pure-Python publication mosaic. `export_modified_terrain()` delegates registered-terrain consolidation, exact 1x/2x/4x/8x downsampling, and modification rasterization to `RasTerrain.export_rasmapper_terrain()`; it does not use a geometry HDF or the deprecated row sampler. Native terrain export is qualified on Windows and Wine for HEC-RAS 6.4.1, 6.5, 6.6, and 7.0.1, with stable 7.1 forward-open behind ras-commander's exact managed-contract check. `export_mannings_raster()` separately wraps `HdfLandCover.compute_final_mannings_raster()` and still requires HEC-RAS 6.6+ on Windows.
- `mapping.py` — Result raster generation. `generate_result_maps()` generates WSE/Depth/Velocity/etc. GeoTIFFs via `RasProcess.store_maps()` using `RasStoreMapHelper.exe`. Supports `depth_x_velocity_sq` and `inundation_boundary` map types (ras-commander ≥0.92.0).
- `fast_map.py` — Optional Rust mapping for **pure 2D models (FIM library scope)** (`map --engine fast-map`, env `RAS2CNG_MAP_ENGINE`) via `RasFim` and `ras-commander-fast-map`, qualified against RAS Mapper stored maps. `map_plan()` does Max WSE/Depth (+ opt-in plain-sloping velocity), `map_whole_simulation()` arrival time and percent inundated, `map_boundary()` the boundary (original single-source terrain TIFF + HDF); each falls back to RAS Mapper on its own. `with_terrain()` tries the priority-1 source TIFF (no HEC-RAS export), its CRS-canonical copy after a CRS refusal, then the export; `MapResult.fast_map_terrain` records which. Storage areas, 1D, non-Max profiles and duration stay on RAS Mapper (`--fast-map-strict` raises instead). New capabilities are detected from `RasFim`'s methods/kwargs. See *Mapping engines* below.

**Data flow**: HEC-RAS files → ras-commander → GeoDataFrame → GeoParquet (ZSTD compression, bbox columns, Hilbert sorted). Archive output uses `_write_geoparquet()` which adds `covering` metadata for spatial predicate pushdown.

## Mapping engines (agents: read before running `ras2cng map`)

RAS Mapper stays the **default** engine. `--engine fast-map` is built for
**FIM library creation on pure 2D models** (no 1D reaches or cross sections,
no storage areas), not for general use across all model types. Every fast-map
product is **qualified against RAS Mapper output**: pixel conformance against
native HEC-RAS 7.0.1 stored maps on the named models. Everything else falls
back to RAS Mapper. For pure 2D models, agents should **recommend and use**
it for WSE, Depth, arrival time, percent inundated and especially
**inundation boundaries**:

```bash
ras2cng map <project> <out> --engine fast-map --inundation-boundary --ras-version 7.0.1
```

| Product (Max unless noted) | Qualification | Notes |
|---|---|---|
| WSE, Depth | identical values and masks (five-model ras-commander comparison; Davis p02, Bald Eagle bridges p05) | horizontal, sloping, slopingPretty |
| Arrival time, percent time inundated | identical on Davis p02 (4,879 px) and bridges p05 (959,043 px) at 0.5 ft | horizontal and plain sloping; threshold `--arrival-depth`; `--no-fast-map-whole-simulation` to opt out |
| Duration | not qualified | stays on RAS Mapper |
| Velocity | identical values on every common pixel; mask differs on bridges p05 (1,196 / 826 of 973,902 px) | **opt-in** `--fast-map-velocity`, plain sloping only |
| Inundation boundary | deterministic contours, **not** vector parity | see below |

- **Terrain without the export.** With ras-commander's
  `RasFim.top_priority_terrain_source` (ras-commander #724, fast-map #11),
  ras2cng maps on the terrain's unmodified **priority-1 source TIFF** and
  skips the RAS Mapper terrain export (about 160-240 s on Bald Eagle). The
  engine refuses when a wet cell would draw outside that source's footprint
  (bridges p05 whole-simulation: 28 such pixels); ras2cng then exports once
  per run. A CRS refusal (Davis) retries on the source's CRS-canonical copy
  first. `--fast-map-terrain` supplies an export instead;
  `--no-fast-map-source-terrain` always exports. `MapResult.fast_map_terrain`
  records `source`, `source-canonical`, `export` or `supplied` per product.
- **Measured (shared, loaded host):** bridges p05 WSE + Depth on the source
  TIFF 4.5 s end to end with no HEC-RAS (0 differences on 973,902 px);
  all five products on a reused export 16.6 s; RAS Mapper took about 215 s
  for the seven Max products and 26 s for arrival/percent. Davis p02, all
  five products on the canonical source copy: 5.4 s, no HEC-RAS.
- **Inundation boundaries are the strongest case.** RAS Mapper's boundary
  export is very slow. In ras-commander's Davis comparison the Rust batch
  (Max plus one timestep) took 3.5 s against 85 s for the two native calls.
  The Rust result is a deterministic contour method
  (`deterministic-zero-depth-contours/1`): byte-identical across runs,
  platforms and worker counts. It is **close to, not identical with,** RAS
  Mapper's polygons, which themselves vary between identical exports (Davis
  Max: 66 vs 65 holes, ~7,400 ft² difference, almost all one hole). Say so
  when reporting; never claim vector parity. Boundaries need a
  **single-source, unmodified** terrain (original TIFF + terrain HDF;
  `--fast-map-terrain-hdf`) and horizontal or plain sloping render mode, and
  ras-commander with `RasFim.map_inundation_boundaries` (ras-commander PR
  #709 + fast-map PR #5, unreleased as of 2026-10-28); until then they fall
  back to RAS Mapper automatically.
- Out-of-scope plans fall back to RAS Mapper per product group, with the
  reason printed. Use `--fast-map-strict` when the user needs proof that Rust
  ran. `MapResult.engines` records which engine produced each map type.
  Outputs are named like RAS Mapper's (`WSE (Max).tif`,
  `Arrival Time (0.5ft hrs).tif`, `Percent Time Inundated (0.5ft).tif`);
  the boundary is `Inundation Boundary (Max).geojson` (RAS Mapper writes a
  shapefile). Manifests: `fast_map_manifest.json`,
  `fast_map_whole_simulation_manifest.json`, `fast_map_boundary_manifest.json`.
- The new capabilities need ras-commander with #724 (merged 2026-10-28,
  unreleased) and a fast-map engine from its `main` (#11-#13). Older
  installs are capability-detected and keep the previous behaviour.

## Archive Output Structure

```
{output_dir}/
├── manifest.json                         # Always written (schema v2.7)
├── {ProjectName}.parquet                 # Project metadata (RasPrj dataframes, _table column)
├── {ProjectName}.g01.parquet             # All geometry from g01 HDF + text, layer column
├── {ProjectName}.g02.parquet             # (if multiple geometry files)
├── {ProjectName}.p01.parquet             # All results from p01, layer column (--results)
└── terrain/                              # Only with --terrain
    └── Terrain50_cog.tif
```

### Querying consolidated parquets

```sql
-- Filter by layer column for homogeneous geometry types
SELECT * FROM 'Model.g01.parquet' WHERE layer = 'mesh_cells'
SELECT * FROM 'Model.g01.parquet' WHERE layer = 'bc_lines'
SELECT * FROM 'Model.p01.parquet' WHERE layer = 'maximum_depth'

-- Project metadata
SELECT * FROM 'Model.parquet' WHERE _table = 'plan_df'
```

Each consolidated GeoParquet includes a per-row `bbox` struct column (`bbox.xmin`, `bbox.ymin`, `bbox.xmax`, `bbox.ymax`; archives before manifest 2.7 use flat `bbox_xmin`… columns) with GeoParquet 1.1 `covering` metadata for spatial predicate pushdown. Rows are Hilbert-sorted within each layer for optimal spatial locality.

## Key Conventions

- ras-commander normalizes column names to **snake_case** (e.g., "Maximum Depth" → `maximum_depth`)
- DuckDB queries use `_` as the table name placeholder
- All 10 HDF geometry layers: `mesh_cells`, `mesh_areas`, `cross_sections`, `centerlines`, `bc_lines`, `breaklines`, `refinement_regions`, `reference_lines`, `reference_points`, `structures`
- Text geometry layers: `cross_sections`, `centerlines`, `storage_areas`
- File type detection is suffix-based: `.g01.hdf` = HDF geometry, `.g01` = text geometry, `.p01.hdf` = plan results
- Tests mock ras-commander calls since actual HEC-RAS model files are not in the repo
- **Results are NOT exported by default** — plan HDF files contain a copy of geometry (redundant); use `--results` flag explicitly
- Archive output uses **ZSTD compression** (not snappy) per GeoParquet best practices. Legacy per-file exports still use snappy.
- `merge_all_layers()` and `merge_all_variables()` produce consolidated GeoDataFrames with `layer` column; text layers get `_text` suffix
- Hilbert spatial sorting (via DuckDB) is default ON for archives; disable with `--no-sort`

## Documentation Site

- The canonical documentation is `https://rascommander.info/ras2cng/`.
- The `ras-commander-docs` umbrella builds this repo's `master` branch and injects the shared
  cross-product theme at publish time. Keep this repo's own `mkdocs.yml` and docs content here;
  do not copy the umbrella theme into the repo.
- `ras2cng.readthedocs.io` is a legacy URL and should redirect to the canonical site.
- When a public DataFrame/GeoDataFrame contract changes, update `ras2cng/schemas.py` in the same
  change so the agent-native surface at `/ras2cng/llms/api/` remains accurate.
