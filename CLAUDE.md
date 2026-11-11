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
# pytest reports green while silently skipping tests. Counts on Windows:
#   uv sync --all-extras:                     860 passed / 50 skipped (45 need the fork wheels)
#   + fork wheels (Engine installs: freestiler 0.3.0.4, terrio 0.5.0.4): 905 passed / 5 skipped
# The 5 are 4 tests needing the I:/L: drives and 1 POSIX file-mode test. Read the
# skips, not just the green: `pytest -rs`.
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

**CLI layer** (`cli.py`): Typer app with commands `inspect`, `archive`, `spatial-index`, `geometry`, `results`, `query`, `pmtiles`, `sync`, `terrain`, `map`, `map-hdf`, `terrain-mod`, `mannings`, `audit-overviews`. Uses lazy imports — heavy dependencies are imported inside command functions, not at module level.

**Core modules** — each handles one concern:
- `project.py` — Full-project orchestration. `archive_project()` produces consolidated GeoParquet files (one per geometry source, one per plan) with `layer` discriminator column. `export_project_metadata()` writes RasPrj dataframes to a plain Parquet with `_table` column. `_write_geoparquet()` helper adds per-row bbox columns and ZSTD compression with GeoParquet `covering` metadata. `inspect_project()` returns a `ProjectInfo` dataclass (no file extraction). **Note**: `init_ras_project` and `export_all_variables` are imported at module level (not lazy) to enable mock patching in tests.
- `catalog.py` — Schema 3.0 (2.7 when only the Python path ran). `Manifest` dataclass manages `manifest.json`. `ManifestGeomEntry` has a `parquet` field (single consolidated file) and `layers` list with `filter_value` for each layer. `ManifestPlanEntry` similarly has a `parquet` field. `project_parquet` field on `Manifest` references the metadata file.
- `geometry.py` — Detects file type (HDF `.g??.hdf` vs text `.g??`) and routes to ras-commander parsers. `HDF_LAYERS` dict maps 10 layer names to `(class, method_name)` for dispatch. `merge_all_layers()` consolidates all HDF + text layers into a single GeoDataFrame with `layer` column (text layers get `_text` suffix). `_hilbert_sort()` uses DuckDB `ST_Hilbert`; before 0.13 it called a function DuckDB does not have and silently did nothing (the spatial-index post-pass sorted instead). Legacy `export_all_hdf_layers()` / `export_all_text_layers()` remain for backward compat.
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
  - **terrio engine** (`RAS2CNG_COG_ENGINE=auto|terrio|gdal`, capability-detected via `Raster.write(metadata=)`): terrio writes the whole COG — area-matched overviews and encoding, LERC included. Lossless output (float DEFLATE/ZSTD, integer, categorical) is pixel-identical to the GDAL path at every level with the same tags. **LERC holds the same `MAX_Z_ERROR` bound but not the same values**: terrio uses the pure-Rust `lerc-writer`, which stores each 8x8 micro-block's exact minimum as its offset. On Bald Eagle Depth (Max) both encoders' max error is 0.01000 and mean 0.0048, and 8.1% of pixels differ by at most one quantization step (<=0.02). Sizes vs GDAL (terrio main with #22, which masks partial-edge-tile padding as GDAL does): `LERC_DEFLATE` +0.37% Depth / -0.06% Terrain50; plain `LERC` +17.7% Depth / +0.13% Terrain50. The rest of the plain-LERC gap is lerc-writer's encoder; a fix is pending upstream (roteiro-gis/lerc-rust #48), not depended on. Mask, `compression_*` and overview provenance tags are identical. **Code relying on byte or value equality with GDAL-written artifacts must ask for lossless.** **With terrio #23** (`terrio_writes_report()`: `Raster.write(report_metadata=, area_matched_nan_valid=)`) terrio writes `overview_area_ratios` / `overview_base_valid_pixels` into the header from the pass that builds the levels, so there is **no pre-scan**; NaN under a numeric nodata counts as valid as in rasterio's mask (those rasters now stay on terrio, same file as the GDAL path), stale report tags of a re-COGed source are dropped, the returned report is still checked (`_check_terrio_written_report`; a missing report or other factors discard the file for GDAL) and an empty raster goes to GDAL, which labels it. Files are byte-identical to the scan path. **Without #23** the tags come from the exact `_scan_area_matched` pass, run before the write; it is also the NaN-under-numeric-nodata gate, and terrio #18's report is checked against it (`_check_terrio_report`). The scan stays as the fallback and as the oracle in `tests/test_terrio_equivalence.py`. ras2cng never passes overview factors to terrio's COG writer (terrio #19 honours them); it relies on the automatic pyramid matching `overview_factors()`. Masks/alpha, multi-band, no nodata, unknown options, >400 M px (and, without #23, NaN under a numeric nodata) stay on GDAL. `validate_cog` prefers `terrio.validate_cog`, falling back to rio-cogeo. terrio still reads the base level whole, so peak memory stays near raster size. Tests: `tests/test_terrio_equivalence.py`.
  - Env knobs: `RAS2CNG_COG_COMPRESS` (set `ZSTD` for internal-only archives), `RAS2CNG_COG_PREDICTOR`, `RAS2CNG_AREA_MATCHED_MAX_PIXELS`.
- `pmtiles.py` — Legacy CLI surface for `ras2cng pmtiles`. Vector: GeoParquet → **reproject to EPSG:4326** → newline-delimited GeoJSON → tippecanoe, or GeoParquet → Arrow → freestiler (see `vector_tiles.py`; `auto` prefers it). Tippecanoe does not reproject; feeding it projected coordinates yields a valid, silently wrong tileset, so a layer with no CRS is refused rather than tiled. Raster generation is delegated to `maplibre.py`. Requires external CLIs: `tippecanoe`, `gdal_translate`, `pmtiles` — or, for vectors, the freestiler backend (see `vector_tiles.py`).
- `vector_tiles.py` — **Vector tiler backend selection for every PMTiles writer.** `RAS2CNG_VECTOR_TILER` = `auto` (default: freestiler when the build has Arrow input and `buffer`: the gpt-cmdr fork build (0.3.0.4 is current), GitHub Release assets only, see *Engine installs*; else tippecanoe when `tippecanoe` + `pmtiles` are on PATH; else any freestiler) | `tippecanoe` | `freestiler` (in-process Rust tiler, optional `ras2cng[freestiler]` extra). Both keep every feature at every zoom and keep shared mesh edges coincident (freestiler snaps vertices to the tile pixel grid). **Arrow input** (`RAS2CNG_FREESTILER_INPUT` = `auto`|`arrow`|`geojson`) skips NDGeoJSON: mesh layers not joined to results go from the archive GeoParquet in the model CRS and reproject in Rust; layers the viewer already reprojected (sparse layers, cached mesh, joined results) travel as EPSG:4326 Arrow IPC spills, because exact manifest bounds and the join need pyproj anyway. freestiler refuses NAD83(HARN), NAD27 and others that PROJ shifts by datum grids (Davis is HARN); `run_freestiler` catches the `ValueError`, reprojects that CRS in Python and retries, remembering it per process. A layer with no CRS is refused. `delivery_table()` shapes properties as the GeoJSON path did (timestamp text, NaN as null, nested values as text, all-null columns dropped), so decoded tiles are identical across inputs: 0 differences of 6.1 M features on Bald Eagle p07, and none on Davis. `buffer=5` is always passed (`TILE_BUFFER`; freestiler's default is 12.8 px). Measured, Bald Eagle p07 viewer with vector results: Arrow 36 s / 3.4 GB, freestiler via GeoJSON 1,841 s / 6.3 GB, tippecanoe 1,745 s (Linux in Docker, with 8.5 GB of NDGeoJSON). Size against tippecanoe with the default (no feature IDs): -10.0% on Bald Eagle p07 results, +2.8% on Davis (with IDs: +5.4% / +12.3%). **The 40-50% gap is gone, but do not claim freestiler is always smaller:** with 0.3.0.3, sparse low-zoom `geometry.pmtiles` is +34% (Bald Eagle, IDs off) and +2.6% (Davis, without `Terrain_Profiles`). Evidence: `feature_dev_notes/rust_engines/T12.md`. **Sparse tilesets with freestiler 0.3.0.4 (#11; `feature_dev_notes/rust_engines/T30.md`, `T32.md`), each capability-detected:** `deduplicate=True` on every tileset (identical tiles stored once, as `pmtiles convert` does; the whole Bald Eagle gap; no effect on mesh tilesets, which have no duplicate tiles); `sort_values=True` **only on tilesets without mesh layers** (`sort_values_for()`: every layer an `ArrowLayer` of a known non-mesh kind; `RAS2CNG_VECTOR_TILE_SORT_VALUES=auto|1|0`), because on mesh layers the unsorted value table follows feature order and sorting it measured `geometry-detail.pmtiles` +6.4% (Bald Eagle p07), +3.3% (Davis), +3.1% (UPGU3) and `results-detail.pmtiles` +4.6% / -1.8% / -0.1%; and per-layer `line_simplify=1.0` tile pixels on non-mesh line layers (`simplify.py`). `RAS2CNG_VECTOR_TILE_COMPACT=0` / `--no-compact-tiles` / `vector_tile_options(compact=False)` turn the two archive options off. **`sort_values` changes archive bytes (decoded tiles are equal), so a byte-identity test against an older output must turn it off.** `geometry.pmtiles` against tippecanoe 2.82.0 with ras2cng's flags (now including `--drop-rate=1`): Bald Eagle p07 966,399 vs 972,440 (-0.6%), Davis 343,964 vs 350,842 (-2.0%), UPGU3 7,248,146 vs 7,048,586 (+2.8%; the remainder is ras2cng's `polygon_simplify=0.5` and the breakline stand-ins tippecanoe does not write). `results.pmtiles` gets 0.2-3.4% smaller (almost all `sort_values`; `line_simplify` moves it by under 0.2%). The viewer manifest records `vectorArchive` (`deduplicate`, `sortValues`, `sortValuesScope`, `archiveOnlyColumns`). **The tippecanoe path passes `--drop-rate=1`** (`maplibre._run_tippecanoe`, `pmtiles.py`): the default rate thinned point layers (Davis lost 2,243 pipe-node points over z0-z16). Multi-layer tilesets use `freestile_layers` (one layer in memory) when available. **Short lines:** with a freestiler that has `keep_short_lines` (0.3.0.3, capability-detected on `FreestileLayer`) and Arrow input, the kinds in `KEEP_SHORT_LINES_KINDS` (`mesh_faces`, `breaklines`, alone or joined to results; one constant, so widening is one line) pass it; `RAS2CNG_KEEP_SHORT_LINES=0` / `--no-keep-short-lines` turn it off and the viewer manifest's `vectorShortLines` records the layers or the reason. `RAS2CNG_VECTOR_TILE_GZIP_LEVEL` defaults to 6. MVT feature IDs are **off by default** (user decision 2026-10-31; -14% on the 87k mesh; tippecanoe tiles never had them); `RAS2CNG_VECTOR_TILE_IDS=1` / `--feature-ids` keeps them for consumers using MapLibre `feature-state` without `promoteId`. Never pass `drop_rate`.
- `simplify.py` — **Douglas-Peucker polygon filter before vector tiling, and the tiler's per-zoom option.** On by default for MapLibre web-map tiles (`maplibre`, `maplibre-stored-vector`) at **1 ft** (user decision 2026-11-02; RAS Mapper's own filter is 0.1 ft), converted to the layer's CRS units (1 in a foot CRS, 0.3048 m in a metre CRS; geographic CRSs and layers without a CRS are skipped with a warning). `RAS2CNG_SIMPLIFY_TOLERANCE` (feet; `0`/`off` disables) and CLI `--simplify-tolerance` / `--no-simplify`. Applies to the **polygons** of every layer (land cover, mesh-area perimeters, refinement regions, storage areas, terrain modification/source polygons, model extents, Stored Map polygons) and is applied in the model CRS before reprojection, inside `maplibre._to_wgs84(simplify_kind=...)`. **Never `mesh_cells`/`mesh_faces`** (`NEVER_SIMPLIFY`: shared edges must stay coincident), and lines/points are unchanged (HEC-RAS breaklines are dense, 30-37% removable at 0.1 ft, but lines are not a tiling bottleneck and results join to them). Archive GeoParquet is the data of record: **off by default**, opt-in with `ras2cng archive --simplify-tolerance FT` (`archive_project(simplify_tolerance_ft=)`; the env var never applies to archives), only on Python-path layers, never the native mesh layers. Engine `RAS2CNG_SIMPLIFY_ENGINE` = `auto` (terrio `simplify_wkb` when the installed terrio has it: CLB terrio build, terrio-clb PR #10) | `terrio` | `shapely`; both run GEOS's DouglasPeuckerSimplifier ring by ring with the same vertices (rings that would collapse keep their vertices, no `buffer(0)` repair). Records: viewer manifest `vectorSimplification` + per-layer `simplification`; archive `manifest.geometry[].simplification`. The UPGU3 land-cover polygon goes 1,062,037 -> 122,122 vertices at 1 ft (-88.5%; 407,116 at 0.1 ft; a smooth over-densified outline, not a raster staircase). Without the per-zoom option tiler time is linear in the polygon's vertices (`clip_polygon` sweeps the whole ring once per tile in the bounding box). Evidence: `feature_dev_notes/rust_engines/T15.md`, `T16.md`. **Per-zoom:** with a freestiler that has `polygon_simplify` (fork build 0.3.0.2, capability-detected on `FreestileLayer`) and Arrow input, non-mesh polygon layers also pass `polygon_simplify=0.5` tile pixels (`DEFAULT_TILE_POLYGON_SIMPLIFY_PX`; `RAS2CNG_TILE_POLYGON_SIMPLIFY`, `--tile-polygon-simplify PX`, `--no-tile-polygon-simplify`; `--no-simplify` turns off both filters). The tiler simplifies once per zoom and cuts down a tile tree instead of sweeping the whole ring per tile, which is where the time was. 0.5 rather than T16's 1.0 for less area bias (-4.8% against -7.3% at z8 on UPGU3) at about 7.5% more size. `simplify.tile_polygon_pixels(kind)` and `ArrowLayer.__post_init__` keep it off `mesh_cells`/`mesh_faces`, because polygons are simplified independently and shared edges would stop matching; results joined to mesh cells go through the same guard. Recorded in `vectorSimplification.perZoom` (enabled, tilePixels, layers, or the reason it did not run). Older freestiler, GeoJSON input and tippecanoe tile as before. Measured (`feature_dev_notes/rust_engines/T19.md`, three runs, median): UPGU3 geometry tiles 1,615 s (0.13.0, no filter) -> 14.9 s, UPGU4 400 s (0.1 ft) -> 32.9 s. On one host at one load, UPGU3: per-zoom alone 16.1 s, 1 ft alone 150 s. **The speed comes from the per-zoom path, and the 1 ft filter adds about 1 s of it**; without this freestiler build 1 ft is what is left. **Per-zoom lines:** with a freestiler that has `line_simplify` (fork build 0.3.0.4, capability-detected on `FreestileLayer`) and Arrow input, the **lines** of every layer except `mesh_faces` (alone or joined to results) pass `line_simplify=1.0` tile pixels (`DEFAULT_TILE_LINE_SIMPLIFY_PX`; `RAS2CNG_TILE_LINE_SIMPLIFY`, `--tile-line-simplify PX`, `--no-tile-line-simplify`; `--no-simplify` turns it off too): Douglas-Peucker per zoom, at every zoom including the maximum, in place of the tiler's default pre-simplification, which kept 1.2-1.6 times tippecanoe's vertices on dense breaklines. 1.0 is tippecanoe's tolerance (0.5 left UPGU3 `geometry.pmtiles` 9% larger). `simplify.tile_line_pixels(kind)` and `ArrowLayer.__post_init__` keep it off `mesh_cells`/`mesh_faces`: a face's vertices are the cells' vertices. Endpoints are kept, a line that would collapse keeps its vertices and `keep_short_lines` still applies, so every breakline is still at every zoom (UPGU3 1,676 of 1,676; T30), and the worst vertex-to-line distance is 1.6 tile pixels against 5.0 for the default path. Recorded in `vectorSimplification.lines` and per layer as `simplification.tileLinePixels`.
- `raster_tiles.py` — **Raster tiler backend selection for every raster PMTiles writer** (terrain, Stored Maps, calculated layers, `pmtiles` on a GeoTIFF). `RAS2CNG_RASTER_TILER` = `auto` (default: terrio when installed and the raster is under `RAS2CNG_TERRIO_MAX_PIXELS`, 400 M) | `terrio` (pure-Rust, in-process; no GDAL/pmtiles CLIs) | `gdal` (the gdaldem→gdalwarp→gdal_translate→gdaladdo→`pmtiles convert` chain, streams any size). Measured on Bald Eagle: terrain 6.8 s vs 13.4 s, depth 1.6 s vs 4.7 s. terrio result overviews use `NEAREST`, holding visible wet area at 1.000× at z10 (GDAL chain: 1.225×), and its terrain edges carry ramp colour (the GDAL chain's were black despite the alpha-0 ramp trick, because `gdalwarp -dstalpha` writes black RGB). Terrain uses `AVERAGE`; override with `RAS2CNG_RASTER_TILE_RESAMPLING`. `package_maplibre_terrain(hillshade=True)` (default; CLI `--no-hillshade`) multiplies a terrio multidirectional hillshade into the display colours. It shades only visible pixels, because darkening RGB under alpha 0 brings the edge fringe back. It is not gdaldem-identical, which is accepted for display. Tests pin `gdal` via an autouse fixture in `tests/conftest.py`, because the older raster tests mock the GDAL subprocesses. With a streaming terrio (`dem` has `max_memory_mib` and `hillshade_strength`), relief or one-pass shaded relief goes file to file, the warp is chunked when `warp` has `max_memory_mib`, and PMTiles stream from the lazily opened warp. Tiles are byte-identical to the in-memory path and there is no pixel cap (Terrain50 z14: 13.4 GB -> 1.0 GB peak). `RAS2CNG_TERRIO_MAX_MEMORY_MIB` bounds the strips.
- `geoparquet.py` — **Every archive GeoParquet goes through `write_geoparquet()`.** It writes a GeoParquet 1.1 `bbox` struct covering (manifest 2.7; older archives have flat `bbox_xmin`… and every reader accepts both), ZSTD level 3 with dictionary encoding on string columns only, and row groups of 65,536 rows that also end at every layer boundary. Per-row-group dictionaries on unique numeric columns cost 12–15% and ZSTD level 1 another 15%. Rows are Hilbert-ordered within each layer by DuckDB `ST_Hilbert` of the bbox centre over the layer extent; a bit-identical NumPy port is used, with a warning, only when DuckDB spatial is missing. With sorting on, the writer stores `hilbert_index` and the spatial-index pass leaves the file alone. terrio's `write_geoparquet_layers(..., profile="archive")` (CLB-Engineering-Corporation/terrio #27) is the **one** Rust implementation of the archive profile and the **default** engine; fast-hdf's former `export_geoparquet_ipc_files` is retired (fast-hdf 0.3.0), because terrio owns file formats. It writes rows, order, schema, row groups and `geo` metadata identical to the pyarrow engine, which stays the fallback and the oracle: `RAS2CNG_GEOPARQUET_WRITER=auto` (= `prefer-native`) uses terrio when it has `write_geoparquet_layers` and otherwise (or when terrio refuses a file) falls back to pyarrow, warning once per reason and recording it in `manifest.geometry[].geoparquet.fallback_reason`; `native` (`terrio`, `rust`; old `fast-hdf` accepted) is strict, `python` never tries it. The default is one switch, `geoparquet.AUTO_PREFERS_NATIVE`. ras2cng passes the Arrow table it would write plus each layer's identity (`KNOWN_IDENTITY` / `_layer_identity`); terrio splits on `layer`, so any Arrow type, Z and geometry collections pass. Measured on Bald Eagle p07 (16 files, fresh process each, loaded host): 10.4 s vs 12.7 s for pyarrow (an earlier, more loaded run: 19.9 vs 30.2 s, and 36.7 s for the old fast-hdf path), peak 561 vs 1184 MB; sizes -5.4..+2.2% vs pyarrow with terrio 0.5.0.2 (terrio keeps WKB min/max statistics that pyarrow drops over 4 KB; 0.5.0.3 writes pages with pure-Rust Zstandard, 0.15-1.63% larger on p07). Never call `gdf.to_parquet` for archive files.
- `archive_direct.py` — **Direct archive path (manifest 3.0).** `archive_project()` writes geometry and plan files through terrio's `write_hecras_archive` (CLB-Engineering-Corporation/terrio #29, in the CLB terrio build (0.5.0.4 is current), which is published only as GitHub Release assets on CLB-Engineering-Corporation/terrio-clb, not on PyPI; capability-detected on `terrio._terrio`), with the Python path kept as fallback and oracle. `RAS2CNG_ARCHIVE_ENGINE` / `--archive-engine` = `auto` (default; = `prefer-native`, but off when `RAS2CNG_GEOPARQUET_WRITER=python`) | `native` (`direct`, `terrio`, `rust`: raises instead of falling back for a whole file, even under `skip_errors`) | `python`. The one switch for the default is `AUTO_PREFERS_DIRECT`. Without the capability, or with `python`, output is what it was: Python path, manifest 2.7, no `archive` records. **Never lose data silently:** only `mesh_cells`, `mesh_faces` and the 2D summary variables are native, because their columns and values equal the Python path's. Every other geometry layer and every `*_text` layer comes from Python through `extra_layers`: the engine omits `river_reaches` and the joined `structures`/`cross_sections` columns and `centerlines` `length`, has no text layers, keeps float32/uint64 where Python has float64/int64, and (before terrio-clb #17) refused a whole breakline family over one row with fewer than two points; with #17 it leaves that row out and reports it in `omitted`, as ras-commander skips it, but breaklines still come from Python for the type reasons. They read in about 0.1 s per file. Do not widen the native list without equal columns and types: a same-named float32 native column and float64 Python column is refused (`field X is Float32 in one layer and Float64 in another`, no file written), which sends the file to the Python path. Refusals come back in `skipped`, not as errors: geometry refusals are filled by a second write, plan refusals are found first by a geometry-free `hecras_archive_layers` call and filled from Python, joined to `mesh_cells` read back from the archived file. Older plans whose summaries store no `Location` go this way with terrio 0.5.0.3 and older, which link fast-hdf 0.3.0 (Spring p01: three face variables filled from Python), or wholly to Python when every variable is refused (Spring_BLE p02). **With terrio 0.5.0.4, which links fast-hdf 0.5.0 (#71), those summaries are read by the engine**, qualified by name and width: Spring p01 has 5 native variables and none from Python, Spring_BLE p02 takes the direct path with 8, and `Cell Last Iteration` on 7.x plans is native too (checked on Davis p02: Int64 in both paths, so no width refusal). Every layer, row and non-null column still equals the Python path (Spring 11 of 11 layers, Spring_BLE 14 of 14, Davis 19 of 19). Measured, `archive --results`, median of three, fresh process, shared host (T32): Spring 20.4 s / 2,257 MB to 13.7 s / 1,367 MB (Python path 23.6 s / 3,298 MB); Spring_BLE 25.4 s / 3,354 MB to 13.3 s / 1,441 MB (Python path 30.9 s / 4,134 MB). `cell_cumulative_iter_lookup` and `starting_differences_*` are unreadable in Python too, so they are recorded with `filled: false` and are absent from either path's archive. `crs=` is always passed (frame CRS, else stored projection, else project; plans use their geometry file's), because the engine reads only a stored `Projection` and otherwise writes `crs: null`. `time_days=False`, `ghost_cell_points=False` and `layers=[...]` keep the engine's extra outputs out. `--no-sort`, `--results-layout variable`, `--results-geometry point|none`, steady plans and geometry without an HDF stay on Python. Each file's manifest `archive` record has `path`, `fallback_reason`, `native_layers`, `python_layers`, `refused`, `crs_source`, `seconds`, and, when the engine reports it (terrio-clb #17), `omitted`: rows the engine left out of a layer it wrote (`layer`, `source_row`, `point_count`, `reason`). ras2cng asks the engine only for the mesh layers and the summary variables, so the list is empty today; it is recorded so that such a row can never be silent. Measured (three runs, median, shared host; `feature_dev_notes/rust_engines/T11.md`): Bald Eagle p07 with results 29.1 s to 12.5 s, peak 4.0 GB to 0.76 GB; Spring 22.4 to 16.2 s, 3.0 to 2.0 GB; Spring_BLE 27.9 to 24.7 s, 4.0 to 3.1 GB. Every layer, row and non-null column is equal on five models; geometry files are byte-identical, and plan files differ only by dropping the all-null union columns of geometry layers.
- `postgis_sync.py` — GeoParquet → PostGIS via SQLAlchemy/GeoAlchemy2 with automatic spatial index creation.
- `terrain.py` — Terrain discovery, consolidation, and raster export. `discover_terrains()` finds terrain layers from rasmap. `consolidate_terrain()` provides a pure-Python publication mosaic. Its merge (`_merge_tifs`) warps each 512-px destination block with rasterio; `RAS2CNG_TERRAIN_MERGE_ENGINE=terrio` is an **opt-in, off by default** (`auto` = `rasterio`; the one switch is `warp.TERRAIN_MERGE_AUTO`) that does one whole-output terrio warp of all sources instead (painter order, `overview="NONE"`). The terrio mosaic is bit-identical to GDAL's own whole-output warp but **not to today's output**, because today's blockwise output is the one that differs from GDAL: on Bald Eagle (dtm_20ft + baldeagledem at 40 ft) 18.5 M of 49.7 M pixels differ, by up to 4.44 ft (274 over 1 ft), with equal masks; 4.9 s against 20.9 s and 453 against 829 MiB (`feature_dev_notes/rust_engines/T31.md`). The user decides the default later; do not flip it. The engine used is recorded in the terrain provenance (`merge_engine`, `merge_engine_fallback_reason`). `export_modified_terrain()` delegates registered-terrain consolidation, exact 1x/2x/4x/8x downsampling, and modification rasterization to `RasTerrain.export_rasmapper_terrain()`; it does not use a geometry HDF or the deprecated row sampler. Native terrain export is qualified on Windows and Wine for HEC-RAS 6.4.1, 6.5, 6.6, and 7.0.1, with stable 7.1 forward-open behind ras-commander's exact managed-contract check. `export_mannings_raster()` (`ras2cng mannings`) writes RAS Mapper's "Final n Values" raster in Rust by default (`HdfLandCover.export_final_mannings_raster`, ras-commander 0.113 / fast-hdf 0.3; 0 px differences vs RAS Mapper 7.0.1 on Bald Eagle g01/g09/g11/g13 and Weise_2D, ~2 s vs 45-49 s, no HEC-RAS). Geometries the engine refuses (1D, pipe networks, virtual land cover, multi-part polygons) fall back with a warning to the old Python estimate (`estimate_final_mannings_raster`), which differs from RAS Mapper on 44-51% of common pixels and uses another grid; `--method rasmapper|estimate` forces one. The GeoTIFF tag `ras2cng_mannings_method` (`rasmapper-final-n` / `estimate`, plus `ras2cng_mannings_fallback_reason`) records which ran. Behaviour change in 0.13.0.
- `mapping.py` — Result raster generation. `generate_result_maps()` generates WSE/Depth/Velocity/etc. GeoTIFFs via `RasProcess.store_maps()` using `RasStoreMapHelper.exe`. Supports `depth_x_velocity_sq` and `inundation_boundary` map types (ras-commander ≥0.92.0). Deletes the large `PostProcessing.hdf` cache from outputs unless `keep_postprocessing=True`. Whole-simulation types `arrival_time`/`duration`/`percent_inundated` (threshold via `arrival_depth`; correct RasMapperLib XML names are `arrival time`/`duration`/`fraction inundated`) pass through natively on newer ras-commander or via a rasmap pre-injection shim on older versions (capability-detected from `store_maps`' signature). `recession` is a warned no-op — RasMapperLib has no recession MapType, and only RasMapperLib-native outputs are produced (a derived arrival+duration approach is sketched in `feature_dev_notes/adr_maps_implementation_plan.md` but intentionally not shipped pending methodology verification). `--reproject-wgs84` (`_reproject_tifs`) goes through `warp.reproject_path` and **raises** when a raster cannot be reprojected: before 0.16 it imported `calculate_default_transform` from the wrong rasterio module, caught the `ImportError`, printed "rasterio not available" and returned the rasters in the model CRS, and the only test mocked the function. `tests/test_warp.py` runs it unmocked.
- `warp.py` — **Path-to-path reprojection** for `--reproject-wgs84` and `terrain._reproject_to_match`, and the terrain-merge engine switch. `RAS2CNG_REPROJECT_ENGINE` = `auto` (terrio when its `warp` streams path to path, i.e. has `max_memory_mib`: the CLB terrio build; else rasterio, also when terrio refuses a file, with a warning) | `terrio` (strict) | `rasterio`. **`overview="NONE"` is always passed**: terrio's default is gdalwarp's `-ovr AUTO`, which reads a source overview when the output is coarser, and rasterio reads full resolution (that was worth 151 ft on a terrain mosaic). The grid is GDAL's suggested warp output in both engines (no `tr`/`te`/`tap`), and nodata, data type and band count are the source's. Measured on Bald Eagle Terrain50 to EPSG:4326 (T31): 7.8 s against 30.3 s, reprojection peak 1,033 against 1,682 MiB, 876 of 182 M pixels differ by at most 2.4e-4 ft, masks equal; on the Davis and bridges Max depth rasters (T32): same grid, equal masks, 10 and 155 pixels differing by at most 6e-8 and 3.8e-6. The chunk memory is terrio's 64 MB unless `RAS2CNG_TERRIO_MAX_MEMORY_MIB` is set; a smaller value changes interpolated pixels, as `gdalwarp -wm` does. Raster tiles already used the bounded warp and are unchanged.
- `fast_map.py` — Rust mapping for **pure 2D models (FIM library scope)** (the default `map` engine; `--engine rasmapper` / env `RAS2CNG_MAP_ENGINE` forces RAS Mapper) via `RasFim` and `ras-commander-fast-map`, qualified against RAS Mapper stored maps. `map_plan()` does Max WSE/Depth, `map_velocity()` Max velocity, `map_derived()` Max D x V / D x V^2 / Froude (`RasFim.map_derived_max`, one `reduce_max/1` time-series pass), `map_shear()` Max shear stress (its own engine call, like velocity), `map_whole_simulation()` arrival time, percent inundated and duration, `map_boundary()` the boundary (original terrain TIFF + HDF; `deterministic` by default, `native-serial` opt-in); each product group falls back to RAS Mapper on its own, and `velocity_plan()` / `derived_plan()` / `shear_plan()` / `whole_simulation_types()` route products before any terrain work. slopingPretty velocity and derived Max are defaults only when the engine advertises `velocity_with_faces` (`WITH_FACES_METHOD`, `with_faces_options()`), and then the `.rasmap`'s shallow-reduction and depth-weighting flags are passed through unchanged, because RAS Mapper clips with the depth rendered with the same options: the wrong flags reproduce the old 75 + 28 px mismatch. Storage areas are **opt-in** (`--fast-map-storage-areas`, `fast_map_storage_areas=True`, `RAS2CNG_FAST_MAP_STORAGE_AREAS=1`; user decision 2026-10-31: not a default until faster than RAS Mapper): by default a storage-area plan goes wholly to RAS Mapper and `MapResult.fast_map_skipped` records why; opted in, they are drawn as level pools when the engine advertises `storage-area-level-pool/1` (`storage_areas_supported()`; WSE/Depth/arrival/percent only). `--fast-map-terrain-crs-hash` (`fast_map_terrain_crs_fnv1a64`) passes a user-verified engine WKT hash for Rust boundaries. `with_terrain()` tries the priority-1 source TIFF (no HEC-RAS export), its CRS-canonical copy after a CRS refusal, the native effective terrain (`RasFim.build_effective_terrain`), then the export and, after a CRS refusal of the export, the export's CRS-canonical copy (`export-canonical`: the same pixels with EPSG GeoTIFF keys; this is how Davis maps in slopingPretty, where the source terrain is refused), and goes straight to the export for any terrain with active modifications (`terrain.active_terrain_modifications()`: the terrain-HDF layers the project's `.rasmap` lists under the terrain; Rust terrain operations only on unmodified terrains); `MapResult.fast_map_terrain` records which. 1D, non-Max profiles, slopingPretty shear stress and unqualified render modes stay on RAS Mapper (`--fast-map-strict` raises for engine failures and non-Max profiles). `invalid_cells` (`--fast-map-invalid-cells`, ras-commander 0.110.2+) sets pixel ownership for cells whose faces do not form a valid polygon; the engine's `fim-invalid-cells/1` record lands in `MapResult.invalid_cells` and every fast-map manifest. Sloping WSE/Depth are written by the engine (terrio LZW, `--fast-map-writer auto`) when it advertises `native_geotiff_outputs`. New capabilities are detected from `RasFim`'s methods/kwargs. See *Mapping engines* below.
- `scaffold.py` — Barebones project synthesis for `map-hdf`. `read_plan_hdf_metadata()` extracts project name, plan number, ShortID, units, and projection WKT from a plan HDF; `build_scaffold()` synthesizes `.prj`/`.pNN`/`.rasmap` stubs, hardlinks the HDF under its canonical name, and builds the HEC-RAS terrain from raw TIFFs (`RasTerrain.create_terrain_from_rasters`) or imports a pre-built terrain HDF sidecar set. The ESRI projection is written to `Terrain\Projection.prj` — never in the project root, where it would collide with the HEC-RAS `.prj`. Scaffolds carry a `.ras2cng-scaffold` marker and are reused across runs when the source HDF is unchanged.

**HDF engine**: ras-commander ≥0.110.1 (floor 0.117.0, which requires fast-hdf ≥0.5.0,<0.6; its `[full]` extra brings fast-map ≥0.16,<0.17) reads HDF through the Rust `ras-commander-fast-hdf` engine by default (h5py fallback; `RAS_COMMANDER_FAST_HDF=auto|h5py|native`). ras2cng needs no code for this — `get_mesh_cell_polygons` alone is 4–19× faster.

**Data flow**: HEC-RAS files → ras-commander → GeoDataFrame → GeoParquet (ZSTD compression, bbox columns, Hilbert sorted). Archive output uses `_write_geoparquet()` which adds `covering` metadata for spatial predicate pushdown.

## Engine installs

- **PyPI terrio:** every release up to 0.5.0 is yanked ("Superseded by 0.6.0"). The floor is
  `terrio>=0.5.0.1`, not `>=0.6`, so that the CLB builds still satisfy it; a plain install gets
  PyPI 0.6 or later (0.8.0 on 2026-11-10), which has none of the fork capabilities and keeps the Python and GDAL paths.
- **PyPI, required:** `ras-commander[full]>=0.117.0` brings ras-commander-fast-hdf 0.5.0
  (2D summaries without a stored `Location` and HEC-RAS 5.0.x mesh inventories read natively;
  HDF5 reader 0.12.1) and ras-commander-fast-map 0.16.0 (storage-area level pools, effective
  terrain, velocity, Max derived products, duration, Rust boundaries including the opt-in
  native-serial contract, the Rust Final n Values raster, and since 0.16.0 slopingPretty
  velocity and derived Max with the `.rasmap` options (`velocity_with_faces`) and Max shear
  stress (`rasmapper-face-shear-max/1`)). Every one is still capability-detected, so an older
  engine keeps the behaviour described for it.
- **Fork builds, optional** (GitHub Release assets: freestiler on the gpt-cmdr fork, terrio on
  CLB-Engineering-Corporation/terrio-clb; never PyPI, which
  rejects direct-URL dependencies; the `terrio`/`freestiler` extras keep the PyPI floors and
  every fork feature is capability-detected, so PyPI builds keep the Python paths):
  - freestiler **0.3.0.4** (Arrow input, `buffer`, per-zoom `polygon_simplify`, `keep_short_lines`,
    and since 0.3.0.4 `deduplicate`, `sort_values`, `line_simplify`):
    `pip install freestiler==0.3.0.4 --find-links https://github.com/gpt-cmdr/freestiler/releases/expanded_assets/v0.3.0.4-gpt-cmdr`
  - terrio **0.5.0.4** (direct archive path, native GeoParquet writer, COG report, streaming
    tiles, `simplify_wkb`, warp `wm=`, `omitted` rows): `pip install terrio==0.5.0.4 --find-links https://github.com/CLB-Engineering-Corporation/terrio-clb/releases/expanded_assets/gpt-cmdr-v0.5.0.4`.
    Built against ras-commander-fast-hdf 0.5.0, so old plan files (2D summaries without a stored
    `Location`: Spring p01, Spring_BLE p02) take the direct archive path. It links **two HDF5
    readers**: hdf5-reader 0.12.1 inside the HEC-RAS engine (fast-hdf) and 0.10.1 for terrio's own
    NetCDF/HDF5 code. Earlier CLB builds (0.5.0.3 and older) link fast-hdf 0.3.0 and send those
    plans to the Python path.
    **Zstandard since 0.5.0.3** (terrio-clb #12-#14): GeoTIFF/COG ZSTD and LERC_ZSTD still use
    the zstd C library, so COGs are byte-identical to 0.5.0.2's; GeoParquet, Zarr and EPT use
    pure-Rust `zstd-rust` (CLB's `ruzstd` fork). Archive GeoParquet keeps the same rows, order,
    schema, row groups and `geo` metadata (still equal to pyarrow's) and only the compressed
    pages differ: on Bald Eagle p07 (`archive --results`, 18 files) the 16 GeoParquet files written
    by terrio are 0.15-1.63% larger and the archive 0.36% (T25).
  - In a uv project env run `uv sync --all-extras` **first**, then `uv pip install ...` with the
    same arguments, and run with `uv run --no-sync` (as CI does). Any later `uv sync`, even
    `--inexact`, puts the locked PyPI builds back.

## Mapping engines (agents: read before running `ras2cng map`)

**fast-map is the default** `ras2cng map` engine (`fast_map.DEFAULT_MAP_ENGINE`).
It is built for **FIM library creation on pure 2D models** (no 1D reaches or
cross sections; storage areas only on opt-in), not for general use
across all model types. Every fast-map product is **qualified against RAS
Mapper output**: pixel conformance against native HEC-RAS 7.0.1 stored maps
on the named models. Everything else falls back to RAS Mapper **per product
group**, with the reason printed and recorded in `MapResult.engines` and
`MapResult.fast_map_skipped`: 1D elements, storage areas (unless opted in),
non-Max profiles, products not qualified with storage areas, products the
installed engine does not advertise, and render modes without qualification. What fast-map maps
depends on what the installed engine advertises (capability-detected from
`ras2fim-batch method` and `RasFim`); older installs keep the earlier
behaviour. `--engine rasmapper` (or `RAS2CNG_MAP_ENGINE=rasmapper`) forces
RAS Mapper for every product; `--fast-map-strict` raises instead of falling
back when an engine call fails.

```bash
ras2cng map <project> <out> --inundation-boundary --ras-version 7.0.1   # fast-map by default
ras2cng map <project> <out> --engine rasmapper                            # native for everything
```

- **Render mode comes from the project's `.rasmap`** (`RenderMode`, plus the
  slopingPretty flags), as RAS Mapper takes it; `--render-mode` overrides
  it. With no stored mode fast-map does not guess: the plan goes to RAS
  Mapper, which applies its own default.
- **Storage areas are opt-in** (user decision 2026-10-31): a plan with
  storage areas goes wholly to RAS Mapper unless `--fast-map-storage-areas`
  (`fast_map_storage_areas=True`, `RAS2CNG_FAST_MAP_STORAGE_AREAS=1`) is
  given, because one plan is slower in Rust today (p07 WSE + Depth 18.7 s vs
  RAS Mapper 12.2 s). Opt in to map storage-area plans without HEC-RAS
  (Linux, FIM batches); the manifests record `storage_areas_opt_in`.
- **Preflight before terrain work:** scope (1D; storage areas without the
  opt-in or on an engine without level pools), engine, render mode, profile and each product's
  qualification are checked from the HDF, `.rasmap` and engine method
  record first, so an out-of-scope plan or product goes to RAS Mapper
  without a terrain export.

| Product (Max unless noted) | Qualification | Notes |
|---|---|---|
| WSE, Depth | identical values and masks (five-model ras-commander comparison; Davis p02, Bald Eagle bridges p05) | horizontal, sloping, slopingPretty |
| WSE, Depth with storage areas (fast-map #18, `storage-area-level-pool/1`) | identical on Bald Eagle p07 and p04 in every mode (T06); p07 Max sloping through `ras2cng map`: 0 mask / 0 bit differences on 1,345,691 px | **opt-in** (`--fast-map-storage-areas`; slower than RAS Mapper today); level pools; older engines send the plan to RAS Mapper |
| Arrival time, percent time inundated | identical on Davis p02 (4,879 px) and bridges p05 (959,043 px) at 0.5 ft; with storage areas p07 1,333,183 px | horizontal and plain sloping; threshold `--arrival-depth`; `--no-fast-map-whole-simulation` to opt out |
| Duration (fast-map #20 `duration_rule`) | identical on Davis p02 and bridges p05 at 0.5 ft | default when the engine advertises `rasmapper-backward-window-recession/1`; not with storage areas (engine refuses) |
| Velocity (fast-map #20 `velocity_depth`) | 0 value / 0 mask differences on bridges p05 Max, sloping (973,902 px) and horizontal (973,532 px); the earlier 1,196 / 826 px mask difference was a reference artifact | **default** in sloping and horizontal; not with storage areas; older engines: opt-in, plain sloping |
| Velocity in slopingPretty (fast-map 0.16 `velocity_with_faces`, ras-commander 0.117) | 0 mask / 0 bit differences on bridges p05 (974,095 / 974,142 / 974,216 / 974,256 px) and Davis p02 (21,402 / 21,591 / 20,703 / 21,114 px) for plain / shallow reduction / depth-weighted / both, through `ras2cng map` (T32) | **default** when the engine advertises `rasmapper-with-faces-velocity-derived/1` and lists the project's options; the `.rasmap` flags are passed unchanged. Without the record (fast-map 0.15 and older): opt-in (`--fast-map-velocity`), plain only |
| D x V, D x V², Froude, Max (fast-map #20 `reduce_max/1`) | bridges p05 Max over 433 times, sloping and horizontal: 0 / 0; Davis ≤5 px within 2 ulp | **default** in sloping and horizontal (`--no-fast-map-derived`); storage areas stay on RAS Mapper |
| D x V, D x V², Froude, Max in slopingPretty (same record as velocity) | 0 / 0 on bridges p05 (974,110 / 974,191 / 974,231 / 974,291 px over 433 times) and Davis p02 (21,315 / 21,862 / 20,880 / 21,287 px over 217 times), same four option sets, through `ras2cng map` (T32) | **default** with the record; otherwise RAS Mapper |
| Shear stress, Max (fast-map 0.16 `rasmapper-face-shear-max/1`, ras-commander 0.117) | 0 mask / 0 bit differences on bridges p05, sloping (973,902 px) and horizontal (973,532 px), and Davis p02 (22,041 / 20,560 px), through `ras2cng map` (T32) | **default** in sloping and horizontal when `RasFim.supports_shear_stress()` is true (`--no-fast-map-shear`); its own engine call; slopingPretty, storage areas and engines without the method: RAS Mapper |
| Inundation boundary, `deterministic` (default) | deterministic contours, **not** vector parity | see below; single-source terrain; not with storage areas |
| Inundation boundary, `native-serial` (**opt-in**, since fast-map 0.15 / ras-commander 0.116) | labelled unqualified by the engine; exact against RAS Mapper's **serial** boundary export on the fixtures named below, and nowhere else | `--fast-map-boundary-method native-serial`; plain sloping; every terrain source; storage areas when opted in |

- **Terrain without the export.** With ras-commander's
  `RasFim.top_priority_terrain_source` (ras-commander #724, fast-map #11),
  ras2cng maps on the terrain's unmodified **priority-1 source TIFF** and
  skips the RAS Mapper terrain export (about 160-240 s on Bald Eagle). The
  engine refuses when a wet cell would draw outside that source's footprint
  (bridges p05 whole-simulation: 28 such pixels); ras2cng then exports once
  per run. A CRS refusal (Davis) retries on the source's CRS-canonical copy
  first. Any other refusal tries the **native effective terrain** next
  (fast-map 0.12 #19 via `RasFim.build_effective_terrain`, ras-commander
  0.113 #788): RAS Mapper's multi-source mosaic rebuilt
  in Rust from the source TIFFs, level-0 masks and stitch TIN, with no
  HEC-RAS. Bald Eagle Terrain50: identical valid mask on 197,179,058 px, 8
  values differ (stitch-TIN corners, max 1.53 ft, where RAS Mapper's own
  exports disagree); 7.0 s / 1.4 GB through RasFim against ~190 s for the
  export. **Rust terrain operations are used only on unmodified terrains**
  (user decision 2026-10-30): `terrain.terrain_modification_status()` reads
  the terrain HDF's `/Modifications` group (stored) and the `.rasmap`'s
  `ElevationModificationGroup` under the terrain layer (active: RAS Mapper
  applies only the layers listed there; a project whose
  `.rasmap` lists none exported identical to the unmodified terrain). Without
  a `.rasmap` entry for the terrain every stored layer counts as active. Any
  active modification (or an unreadable terrain HDF) sends the plan's terrain
  to RAS Mapper's export:
  the source TIFF and the effective terrain are skipped, and the Rust
  boundary is refused so RAS Mapper draws it. Mapping then runs on that
  export, whose WSE/Depth fast-map reproduces exactly. This holds even with
  an engine that accepts modifications: fast-map #21 / ras-commander #790
  (T14) are **parked**, because none of 11 real modified terrains came out
  pixel-identical (`feature_dev_notes/rust_engines/T14.md`).
  `--no-fast-map-effective-terrain` skips it; `--fast-map-terrain` supplies
  an export instead; `--no-fast-map-source-terrain` skips the source TIFF.
  `MapResult.fast_map_terrain` records `source`, `source-canonical`,
  `effective`, `export`, `export-canonical` or `supplied` per product. The
  last one is the export's CRS-canonical copy, tried only after the engine
  refuses the export's CRS identity: on Davis in slopingPretty the source
  terrain is refused ("qualified for plain sloping only"), and both the
  effective terrain and the export are refused for the CRS the engine cannot
  name, so before T32 the whole plan went to RAS Mapper. The copy holds the
  same pixels (`RasFim.canonicalize_terrain_crs` verifies every bit).
- **Measured (shared, loaded host):** bridges p05 WSE + Depth on the source
  TIFF 4.5 s end to end with no HEC-RAS (0 differences on 973,902 px);
  all five products on a reused export 16.6 s; RAS Mapper took about 215 s
  for the seven Max products and 26 s for arrival/percent. Davis p02, all
  five products on the canonical source copy: 5.4 s, no HEC-RAS.
- **Measured end to end with the fast-map #18-#20 engine (T13, fresh
  process, peak RSS of the process tree, shared host; RAS Mapper 7.0.1 on a
  project copy).** Bridges p05 sloping, nine products (WSE, Depth, Velocity,
  D x V, D x V², Froude, arrival, duration, percent at 0.5 ft): every one 0
  mask / 0 bit differences, in sloping and horizontal. Derived Max only:
  141 s / 2.6 GB vs RAS Mapper 206 s / 2.3 GB (it was 213 s / 1.45 GB with
  one output time in flight; `DERIVED_MAX_INFLIGHT = 3`). The other six: 25 s
  / 1.5 GB vs 34 s / 1.1 GB (arrival/duration/percent on the effective
  terrain, where the source TIFF is refused). Bald Eagle p07 (storage area)
  sloping WSE + Depth: 18.7 s / 1.5 GB on the effective terrain vs RAS Mapper
  **12.2 s** / 1.2 GB, so storage areas are about Linux/no-HEC-RAS mapping
  and FIM batches, not speed, for a single plan (hence opt-in); arrival +
  percent 31 s vs 82 s. Details: `feature_dev_notes/rust_engines/T13.md`.
- **Inundation boundaries: two methods, and what may be said about each.**
  RAS Mapper's boundary export can be very slow (Davis: 45 s for Max on this
  host against 2-4 s in Rust; ras-commander's Davis comparison, Max plus one
  timestep: 3.5 s against 85 s). It is not always slow: bridges p05 Max took
  RAS Mapper 8.5 s, and Rust 8.9 s.
  - **`deterministic` (the default,
    `deterministic-zero-depth-contours/1`):** byte-identical across runs,
    platforms and worker counts. It is **close to, not identical with,** RAS
    Mapper's polygons (Davis Max: 66 holes and 405.98 acres against 65 holes
    and 408.63 acres for RAS Mapper's serial export, almost all of it one
    hole). **Never claim vector parity for it.** It needs a **single-source,
    unmodified** terrain (original TIFF + terrain HDF;
    `--fast-map-terrain-hdf`), horizontal or plain sloping, no storage areas,
    and `RasFim.map_inundation_boundaries`.
  - **`native-serial` (opt-in:
    `--fast-map-boundary-method native-serial`,
    `fast_map_boundary_method`, `RAS2CNG_FAST_MAP_BOUNDARY_METHOD`;
    `rasmapper-serial-zero-depth-contours/1`, fast-map 0.15.0 /
    ras-commander 0.116.0):** ras2cng requests it with
    `allow_unqualified=True`, because **the engine and ras-commander still
    label it unqualified** and their receipts keep native vector parity
    false. What ras-commander's mapping guide reports, and all that may be
    said: on Windows x64, plain sloping, zero-depth contours, original terrain
    layouts, against RAS Mapper boundaries exported with RAS Mapper's
    **serial** polygon loop, valid geometry and unrounded coordinates are
    exact (ring start, direction and part order aside) on Baxter (three
    steady profiles, 6.6 and 7.0.1), Davis (Max and one saved time, 7.0.1),
    Bald Eagle (Max and one saved time, two terrain sources and storage, 6.6
    and 7.0.1) and Reese (mixed-model Max); White Oak p19 matches as raw ring
    bits but shares a self-intersection, so it has no valid-vector delivery.
    It does not cover other profiles, modified terrains, another platform or
    RAS Mapper's other execution modes. T32 re-ran three of these through
    `ras2cng map` against RAS Mapper 7.0.1 on project copies: Davis p02
    (274 polygons, 65 holes, 47,272 vertices), bridges p05 (194 / 344 /
    108,189, two terrain sources) and Bald Eagle p07 (117 / 214 / 104,486,
    two sources and a storage area), each geometry-equal and
    coordinate-equal to the serial export. On bridges and p07 RAS Mapper's
    **ordinary** export was also identical to it, parts and holes included;
    that is an observation on two plans, not a claim, because the ordinary
    export is not serial and its polygons have varied between identical
    Davis exports. A peer session's report of equal area with a different
    part/hole split on three HCFCD plans was **not** reproduced or refuted
    here (those plans were not available). ras2cng restricts the method to
    plain sloping, passes the first original terrain source in the terrain
    HDF's priority order (`fast_map.native_primary_source`; the engine checks
    it and refuses a wrong one), and includes storage areas only when they
    are opted in. It is not faster than RAS Mapper on Bald Eagle (p07: 17.2 s
    against 8.0 s); its use is boundaries without HEC-RAS, and multi-source
    terrains the deterministic method refuses.
  Both fall back to RAS Mapper automatically, and both refuse terrain
  modifications. A terrain
  whose CRS the engine cannot name as an EPSG code and reads differently
  from rasterio (Davis) is refused unless the user pins the engine's WKT
  hash: `--fast-map-terrain-crs-hash` (`fast_map_terrain_crs_fnv1a64`), the
  16 hex digits `ras2fim-batch terrain-info <terrain.tif>` reports as
  `crs_wkt_fnv1a64`. ras2cng never guesses it; the refusal message prints
  the engine's value and the command so the user can check the CRS first.
  A hash that does not match the engine's is refused; the boundary manifest
  records `ras2cng.terrain_crs_fnv1a64`.
- **Invalid cell polygons.** HEC-RAS can compute cells whose face polylines
  cross (a "bowtie"). Since ras-commander 0.110.2 fast-map maps such meshes
  instead of refusing them: `--fast-map-invalid-cells` / `fast_map_invalid_cells`
  = `repair` (engine default: polygonize the faces, keep the largest part not
  owned by valid cells), `hull`, `omit`, or `raise` (refuse, so the plan goes
  to RAS Mapper). Hydraulic stencils still use the full HDF topology; only
  pixel ownership changes, and RAS Mapper's own treatment of these cells has
  **not** been qualified against `repair`. The engine's record
  (`fim-invalid-cells/1`: policy, count, each cell, reason, action, areas) is
  kept in `MapResult.invalid_cells` (per map type), in `fast_map_manifest.json`
  (top-level `invalid_cells`, plus `ras2cng.invalid_cells_policy`/`_count`), in
  `fast_map_whole_simulation_manifest.json` and, when the boundary API takes
  it, the boundary manifest. A non-zero count is printed. Older ras-commander:
  `raise` is the old behaviour; any other explicit policy falls back to RAS
  Mapper rather than being ignored.
- **Native GeoTIFF delivery.** With fast-map 0.11 (`native_geotiff_outputs`)
  and ras-commander 0.110.3 (`product_writer`), sloping WSE/Depth are written
  by the engine through terrio instead of piping arrays to Python for
  rasterio. `--fast-map-writer` / `RAS2CNG_FAST_MAP_WRITER` = `auto` (default:
  native when the engine advertises it and velocity is not requested, since
  native delivery excludes velocity) | `native` (required) | `rasterio`.
  Bald Eagle bridges p05 Max on the source TIFF: **pixel-identical** outputs,
  the same plain LZW strip layout (not COGs, as before), files 3.6-4.4%
  smaller, peak RSS 686 vs 805-838 MiB, engine call 8.0 vs 10.9 s median of
  three on a loaded host. These remain the raw RAS-Mapper-named rasters;
  `--cog` still converts them through `ras2cng.cog` (byte-identical COGs from
  either writer). `MapResult.fast_map_writer` records the writer.
- **Retained mapping state (fast-map 0.11 / ras-commander #741) is not used.**
  It lives in `RasFim.prepare_sloping_model` / `map_catalog`, which reuse a
  prepared model across reaches/AOIs and timesteps of one plan. ras2cng maps
  one Max request per plan (non-Max profiles go to RAS Mapper), and prepared
  models refuse `terrain_hdf` (the source-terrain path) and velocity, so there
  is nothing to reuse. Revisit if ras2cng maps timesteps or AOI tiles.
- Use `--fast-map-strict` when the user needs proof that Rust ran.
  `MapResult.engines` records which engine produced each map type.
  Outputs are named like RAS Mapper's (`WSE (Max).tif`, `Velocity (Max).tif`,
  `D _ V (Max).tif`, `D _ V^2 (Max).tif`, `Froude (Max).tif`,
  `Arrival Time (0.5ft hrs).tif`, `Duration (0.5ft hrs).tif`,
  `Percent Time Inundated (0.5ft).tif`); the boundary is
  `Inundation Boundary (Max).geojson` (RAS Mapper writes a shapefile).
  Manifests: `fast_map_manifest.json`, `fast_map_velocity_manifest.json`,
  `fast_map_derived_manifest.json`, `fast_map_whole_simulation_manifest.json`,
  `fast_map_boundary_manifest.json`.
- Velocity is its own engine call (`map_velocity`), separate from WSE/Depth,
  so a velocity refusal never costs WSE/Depth and WSE/Depth keep native
  GeoTIFF delivery. In horizontal mode it renders plain sloping clipped by
  the horizontal depth (`velocity_depth="horizontal"`), as RAS Mapper does.
- **Shear stress** is its own engine call (`map_shear`), in plain sloping
  and in horizontal (rendered plain sloping, clipped by the horizontal depth:
  RAS Mapper's values do not depend on the render mode, only the mask does).
  Through `ras2cng map` on this host: bridges p05 4.7 s / 0.9 GB against RAS
  Mapper's 84.8 s / 0.8 GB; Davis p02 1.1 s against 40.6 s.
- **Released engines.** Everything in this section is in the released
  engines ras2cng requires: ras-commander 0.117.0 with ras-commander-fast-map
  0.16.0 and ras-commander-fast-hdf 0.5.0. Capabilities are still detected at
  run time, so an older engine keeps the previous behaviour: with fast-map
  0.15 slopingPretty velocity is opt-in and plain only, and slopingPretty
  derived Max and shear stress go to RAS Mapper.

## Archive Output Structure

```
{output_dir}/
├── manifest.json                         # Always written (schema v3.0; v2.7 without the direct path)
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
- **Any new COG writer must call `ras2cng.cog`** rather than `rasterio.shutil.copy(driver="COG")` directly — otherwise it silently opts out of area-matched overviews, the `OVERVIEWS` fix, validation, and atomic replacement.
- Raster colour ramps carry the adjacent ramp colour at alpha 0 for nodata, never `0 0 0 0`: `gdaladdo` averages RGB without premultiplying alpha, so black nodata paints a dark fringe along every flood edge at every overview level.
- **Vector tiles never drop features; tile weight is bounded by each tileset's minimum zoom.** That is exact for polygons, mesh cells included. **One known exception, in freestiler:** a line whose two ends snap to the same tile pixel at a zoom has no valid MVT form and is absent at that zoom. `mesh_faces` and `breaklines`, alone or joined to results, are now complete at every zoom with freestiler 0.3.0.3: `keep_short_lines` writes such a line once per zoom as a one-unit segment with its properties, about 1 px from the true line (`vector_tiles.KEEP_SHORT_LINES_KINDS`; `feature_dev_notes/rust_engines/T21.md`, `T22.md`). **Every other line layer still thins out at low zoom** (conduits, BC and reference lines, structures), on purpose: Davis `geometry.pmtiles` was +18.37% with its conduits on, because low-zoom tiles repeat each conduit's properties. Older freestiler, GeoJSON input and tippecanoe also keep the old behaviour for faces and breaklines (UPGU3 `mesh_faces`: 4 of 637,473 absent at z13; UPGU4: 2 of 2,273,372 absent even at z17; 156 of 1,676 UPGU3 breaklines at z0). A multi-part line can still lose a collapsed part while another part survives; none of the models measured has one. `maplibre.py` splits dense mesh (`mesh_cells`/`mesh_faces`, via `_is_detail_geometry`) into `geometry-detail.pmtiles`, and results joined to those elements into `results-detail.pmtiles`; both start at the *detail floor* (`_detail_min_zoom`: z13, raised for a fine mesh to the first zoom where the median cell spans 2 CSS px). Sparse engineering layers stay in `geometry.pmtiles`, and sparse-joined results (cross sections, reference lines/points) in `results.pmtiles`, across the full range. Below z13 complete mesh tiles are multi-megabyte (1.2–1.6 MB at z12, 4–9 MB at z10). Do **not** reintroduce `--drop-densest-as-needed`: it renders a thinned mesh as visible stripes (17–54% of cells drawn at z10–z11) and was removed for that reason. Never use `--drop-fraction-as-needed` (crashes tippecanoe on mixed dense layers) or freestiler's `drop_rate` (drops by bounding-box size, emptying whole zoom levels of a uniform mesh and cutting the refined cells along breaklines first). Polygon simplification before tiling (`simplify.py`) must never touch `mesh_cells`/`mesh_faces`.
- **Mesh results in the viewer join in Arrow, against one geometry on disk** (`maplibre._spill_dense_geometry`, `_join_dense_result`; freestiler Arrow input only). A mesh layer with results is written in EPSG:4326 batch by batch, each variable is read with row-group and column pruning and no geometry, and the tiler's load attaches the one WKB column, so nothing holds shapely objects for the run. Tiles are byte-identical to the pandas join, which stays as the fallback (tippecanoe, GeoJSON input, older freestiler, `join_columns`, nested or stored-index columns) and keeps the composite-key refusals and the reference-line pivot. One thing that looks like a bug is kept on purpose because it is a delivered value: **an integer column with a null anywhere in the plan file is delivered as a double** (`_integers_with_nulls`; that is what pandas did to the whole file). Measured with results, median of three, loaded host (`feature_dev_notes/rust_engines/T20.md`): UPGU4 16,283 -> 2,265 MiB, 408.6 -> 235.0 s; UPGU3 4,064 -> 836 MiB, 131.2 -> 87.1 s; Bald Eagle p07 1,390 -> 438 MiB, 42.7 -> 15.3 s. What holds the peak now is freestiler's per-layer `Vec<Feature>`, about 1.1 GB on UPGU4's 2.27 M faces.
- **Mesh results join on `mesh_name` and the element ID** (`maplibre._index_join_keys`), because cell and face IDs restart in every 2D flow area. Keys that are not unique are refused rather than attributed to a feature, and an unpaired result row or feature raises a `RuntimeWarning`. Joining on the ID alone gave Davis's 11-cell `DS Channel` and the first 11 cells of `area2` both meshes' values, up to 9.4 ft apart in max WSE (T22). The manifest's `rawResult.joinKeys` records the keys.
- `mesh_name_result`, `layer_result` and `hilbert_index_result` are in `_INTERNAL_COLUMNS` and are never delivered: they are a copy of `mesh_name`, the variable the layer already is, and a sort key, and cost about a quarter of `results-detail.pmtiles`.
- **`Terrain_Profiles` stays in the archive and is not delivered in tiles** (`maplibre.ARCHIVE_ONLY_COLUMNS`). It is a station/elevation list on every pipe conduit, was delivered as the text of a NumPy array, and was 70% of Davis `geometry.pmtiles` (1,129,401 bytes with it, 343,964 without). Compact JSON was measured and rejected as not small (1,028,364 bytes at float32 precision, 769,016 at two decimals). Query it from the archive (`layer = 'pipe_conduits'`). `Profile_Data` on structures is still delivered (as JSON) and has not been measured.
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
