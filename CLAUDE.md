# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**ras2cng** (RAS to Cloud Native GIS) — CLI tool for exporting HEC-RAS geometry and results to GeoParquet, querying with DuckDB, generating PMTiles vector/raster tiles, and syncing to PostGIS. Supports **full-project archival**: discovers all geometry configurations, plan runs, and terrain rasters in a HEC-RAS project and exports them to a structured, cloud-native archive with `manifest.json` catalog.

Built on [`ras-commander`](https://github.com/gpt-cmdr/ras-commander) for HEC-RAS file parsing.

## Build & Development Commands

```bash
# Install with all optional dependencies (creates/updates .venv and uv.lock)
uv sync --all-extras

# Run tests -- ALWAYS after `uv sync --all-extras`.
# Without the extras, tests/test_webgis_service.py cannot import (needs fastapi)
# and tests/test_full_extraction.py cannot even be COLLECTED (needs duckdb), so
# pytest reports green while silently skipping ~35 tests. Full suite with every
# extra installed: 401 passed / 4 skipped (the 4 need the I: drive).
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

**CLI layer** (`cli.py`): Typer app with commands `inspect`, `archive`, `spatial-index`, `geometry`, `results`, `query`, `pmtiles`, `sync`, `terrain`, `map`, `map-hdf`, `terrain-mod`, `mannings`. Uses lazy imports — heavy dependencies are imported inside command functions, not at module level.

**Core modules** — each handles one concern:
- `project.py` — Full-project orchestration. `archive_project()` produces consolidated GeoParquet files (one per geometry source, one per plan) with `layer` discriminator column. `export_project_metadata()` writes RasPrj dataframes to a plain Parquet with `_table` column. `_write_geoparquet()` helper adds per-row bbox columns and ZSTD compression with GeoParquet `covering` metadata. `inspect_project()` returns a `ProjectInfo` dataclass (no file extraction). **Note**: `init_ras_project` and `export_all_variables` are imported at module level (not lazy) to enable mock patching in tests.
- `catalog.py` — Schema v2.0. `Manifest` dataclass manages `manifest.json`. `ManifestGeomEntry` has a `parquet` field (single consolidated file) and `layers` list with `filter_value` for each layer. `ManifestPlanEntry` similarly has a `parquet` field. `project_parquet` field on `Manifest` references the metadata file.
- `geometry.py` — Detects file type (HDF `.g??.hdf` vs text `.g??`) and routes to ras-commander parsers. `HDF_LAYERS` dict maps 10 layer names to `(class, method_name)` for dispatch. `merge_all_layers()` consolidates all HDF + text layers into a single GeoDataFrame with `layer` column (text layers get `_text` suffix). `_hilbert_sort()` uses DuckDB for spatial sorting. Legacy `export_all_hdf_layers()` / `export_all_text_layers()` remain for backward compat.
- `results.py` — Exports 2D mesh summary variables from plan HDF files. `merge_all_variables()` consolidates all variables into a single GeoDataFrame with `layer` column. Accepts `mesh_cells_gdf` GeoDataFrame directly for polygon join (avoids write-then-read).
- `duckdb_session.py` — `DuckSession` class wraps DuckDB with auto-loaded spatial extension. `register_parquet()` detects WKB geometry columns and converts them to DuckDB GEOMETRY type. The table alias is always `_`.
- `cog.py` — **Shared Cloud Optimized GeoTIFF policy. Every COG this repo writes goes through it, and downstream pipelines (RBFS `clb_tx_webmap`, LWI webmap) import from it so they inherit the same defaults.** `area_matched_cog()` is the main entry. Key behaviours, each of which exists because the obvious implementation is wrong:
  - **Overviews preserve wet area.** GDAL's `average` marks a coarse cell valid when *any* sub-cell is valid, so the inundation extent grows at every zoom level (measured: 133–156% wet-area inflation). `nearest` holds area only statistically and speckles the margin. `build_area_matched_overviews()` solves for a per-level coverage threshold from the coverage histogram, breaks ties by a coherence rank plus deterministic dither, and writes the **mean** of wet contributors (never max — max inflates flood volume). Categorical rasters get the same mask with a **mode** value.
  - **GDAL cannot be handed overview pixels**, and `osgeo` is not a dependency. Levels are written as sidecar GeoTIFFs, attached to a VRT wrapper via `<Overview>` elements, and embedded with `OVERVIEWS=FORCE_USE_EXISTING`. Only the *default* GDAL metadata domain survives the COG CreateCopy, so overview provenance is written as plain tags (`overview_method`, `overview_area_ratios`, `overview_fallback_reason`), not under `rio_overview`.
  - **`OVERVIEWS` is always stated explicitly.** Its `AUTO` default means "reuse the source pyramid verbatim", which silently discards every `OVERVIEW_RESAMPLING` setting — and HEC-RAS terrain TIFFs routinely ship internal overviews, so reuse is the normal path. Never omit it.
  - **`resolve_crs_authority()` never guesses an EPSG code.** `to_authority(min_confidence=25)` returns EPSG:3083 (metres) for a Texas Centric Albers definition in US survey feet — about 25,000 km of displacement. A code is returned only when the linear unit and ellipsoid round-trip; otherwise `describe_crs()` emits the verbatim definition. `proj4` is the authoritative browser field.
  - **`validate_cog()` gates on `rio_cogeo.cog_validate` from Python.** The `rio cogeo validate` CLI always exits 0, including on an invalid file, so any shell gate trusting `$?` is a no-op.
  - **`atomic_output()`** stages a PID-namespaced partial in the *destination* directory and `os.replace`s on success, so a failed rebuild never destroys the previous good artifact.
  - **LERC is the DEFAULT for float rasters** at `MAX_Z_ERROR=0.01` (raster units), because these artifacts exist to be served. Measured on **full production rasters**: -95.4% (WSE), -72.9% (depth). At 0.01 ft the error is one to two orders of magnitude below the accuracy of the terrain and boundary conditions the value was computed from — not a meaningful loss for WSE/depth/velocity *mapping*. The default is narrowed, not blanket: **integer and categorical rasters stay lossless**, and the nodata mask is never approximated. The tolerance is written to the artifact's tags (`compression_lossy`, `compression_max_z_error`) so the loss travels with the file. **Turn it off for scientific use** — any raster whose values feed a downstream calculation — with `RAS2CNG_LERC_MAX_Z_ERROR=off` or `lerc_max_z_error=None`; lossless falls back to DEFLATE + predictor 3. Note the base level is then no longer byte-identical to the source: code relying on exact equality must ask for lossless. See `docs/user-guide/raster-delivery.md`.
  - **Terrain builds through this module too**, not `gdal_translate` — terrain carries a nodata footprint, so `average` crept its edge outward one coarse cell per level (100.2% / 100.5% / 101.1% measured). `TERRAIN_CREATION_OPTIONS` carries `LEVEL=9` / `SPARSE_OK=YES` through the `creation_options` passthrough. `_terrain_cog_creation_options()` remains as the CLI rendering of the same policy.
  - **Compression defaults to DEFLATE + predictor 3**, not ZSTD. Predictor 3 is expressed as `PREDICTOR=YES` (GDAL resolves it to 3 for float, 2 for integer) — never the literal `3`, which **hard-fails** an integer raster with "PREDICTOR=3 is only supported with Float32 or Float64". Note `YES` is a *COG driver* option: the plain GTiff driver rejects it, so GTiff intermediates must use `cog.numeric_predictor(dtype, categorical=...)` for the numeric form. **The preference reverses on class data** — on a float32 *class* raster predictor 3 measured 2.4× larger than predictor 2 (0.475 vs 0.195 MB), so `categorical` keeps predictor 2 whatever the storage type. Don't "make it consistent". ZSTD-in-TIFF needs a GDAL built with libzstd and fails hard on readers without it; measured cost of DEFLATE at the same predictor is 2% (terrain) / 6% (depth) / 8% (WSE), with no read-time difference. Do **not** copy RBFS's DEFLATE + predictor **2** pairing: it is now **refuted on real production rasters** — predictor 3 won by 4.8% on a near-flat WSE surface (the exact case the claim named) and 15.5% on depth, on top of 16–25% on synthetic surfaces.
  - Env knobs: `RAS2CNG_COG_COMPRESS` (set `ZSTD` for internal-only archives), `RAS2CNG_COG_PREDICTOR`, `RAS2CNG_AREA_MATCHED_MAX_PIXELS`, `RAS2CNG_TIPPECANOE_BOUNDED`.
- `pmtiles.py` — Legacy CLI surface for `ras2cng pmtiles`. Vector: GeoParquet → **reproject to EPSG:4326** → newline-delimited GeoJSON → tippecanoe. Tippecanoe does not reproject; feeding it projected coordinates yields a valid, silently wrong tileset, so a layer with no CRS is refused rather than tiled. Raster generation is delegated to `maplibre.py`. Requires external CLIs: `tippecanoe`, `gdal_translate`, `pmtiles`.
- `postgis_sync.py` — GeoParquet → PostGIS via SQLAlchemy/GeoAlchemy2 with automatic spatial index creation.
- `terrain.py` — Terrain discovery, consolidation, and raster export. `discover_terrains()` finds terrain layers from rasmap. `consolidate_terrain()` merges multiple TIFFs. `export_modified_terrain()` produces a full-resolution GeoTIFF of terrain with modifications (channels, levees, etc.) applied via `RasTerrainMod.compute_modified_terrain_raster()`. `export_mannings_raster()` wraps `HdfLandCover.compute_final_mannings_raster()` to produce a Manning's n GeoTIFF. Both terrain-mod and Manning's n exports require HEC-RAS 6.6+ on Windows.
- `mapping.py` — Result raster generation. `generate_result_maps()` generates WSE/Depth/Velocity/etc. GeoTIFFs via `RasProcess.store_maps()` using `RasStoreMapHelper.exe`. Supports `depth_x_velocity_sq` and `inundation_boundary` map types (ras-commander ≥0.92.0). Deletes the large `PostProcessing.hdf` cache from outputs unless `keep_postprocessing=True`. Whole-simulation types `arrival_time`/`duration`/`percent_inundated` (threshold via `arrival_depth`; correct RasMapperLib XML names are `arrival time`/`duration`/`fraction inundated`) pass through natively on newer ras-commander or via a rasmap pre-injection shim on older versions (capability-detected from `store_maps`' signature). `recession` is a warned no-op — RasMapperLib has no recession MapType, and only RasMapperLib-native outputs are produced (a derived arrival+duration approach is sketched in `feature_dev_notes/adr_maps_implementation_plan.md` but intentionally not shipped pending methodology verification).
- `scaffold.py` — Barebones project synthesis for `map-hdf`. `read_plan_hdf_metadata()` extracts project name, plan number, ShortID, units, and projection WKT from a plan HDF; `build_scaffold()` synthesizes `.prj`/`.pNN`/`.rasmap` stubs, hardlinks the HDF under its canonical name, and builds the HEC-RAS terrain from raw TIFFs (`RasTerrain.create_terrain_from_rasters`) or imports a pre-built terrain HDF sidecar set. The ESRI projection is written to `Terrain\Projection.prj` — never in the project root, where it would collide with the HEC-RAS `.prj`. Scaffolds carry a `.ras2cng-scaffold` marker and are reused across runs when the source HDF is unchanged.

**Data flow**: HEC-RAS files → ras-commander → GeoDataFrame → GeoParquet (ZSTD compression, bbox columns, Hilbert sorted). Archive output uses `_write_geoparquet()` which adds `covering` metadata for spatial predicate pushdown.

## Archive Output Structure

```
{output_dir}/
├── manifest.json                         # Always written (schema v2.3)
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

Each consolidated GeoParquet includes per-row bbox columns (`bbox_xmin`, `bbox_ymin`, `bbox_xmax`, `bbox_ymax`) with GeoParquet `covering` metadata for spatial predicate pushdown. Rows are Hilbert-sorted within each layer for optimal spatial locality.

## Key Conventions

- ras-commander normalizes column names to **snake_case** (e.g., "Maximum Depth" → `maximum_depth`)
- DuckDB queries use `_` as the table name placeholder
- All 10 HDF geometry layers: `mesh_cells`, `mesh_areas`, `cross_sections`, `centerlines`, `bc_lines`, `breaklines`, `refinement_regions`, `reference_lines`, `reference_points`, `structures`
- Text geometry layers: `cross_sections`, `centerlines`, `storage_areas`
- File type detection is suffix-based: `.g01.hdf` = HDF geometry, `.g01` = text geometry, `.p01.hdf` = plan results
- Tests mock ras-commander calls since actual HEC-RAS model files are not in the repo
- **Results are NOT exported by default** — plan HDF files contain a copy of geometry (redundant); use `--results` flag explicitly
- Archive output uses **ZSTD compression** (not snappy) per GeoParquet best practices. Legacy per-file exports still use snappy.
- **Any new COG writer must call `ras2cng.cog`** rather than `rasterio.shutil.copy(driver="COG")` directly — otherwise it silently opts out of area-matched overviews, the `OVERVIEWS` fix, validation, and atomic replacement.
- Raster colour ramps carry the adjacent ramp colour at alpha 0 for nodata, never `0 0 0 0`: `gdaladdo` averages RGB without premultiplying alpha, so black nodata paints a dark fringe along every flood edge at every overview level.
- **Vector tile size policy is per-tileset.** `maplibre.py` splits dense mesh (`mesh_cells`/`mesh_faces`, via `_is_detail_geometry`) into `geometry-detail.pmtiles` at z13+, separate from the sparse engineering layers in `geometry.pmtiles`. The dense tilesets — `geometry-detail.pmtiles` and `results.pmtiles` (result values joined per mesh cell) — are built `bounded=True` (`--drop-densest-as-needed`, which pairs with `--extend-zooms-if-still-dropping` to move features to a legible zoom rather than discard them). The overview tileset stays unbounded — those layers must be complete at every zoom. Never use `--drop-fraction-as-needed`: it makes tippecanoe retry the sparsest features until the native binary crashes on mixed dense layers.
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
