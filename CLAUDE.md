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
# extra installed: 720 passed / 4 skipped (the 4 need the I: drive). For the
# Rust fast paths add the fork wheels (see *Engine installs* below).
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
- `vector_tiles.py` — **Vector tiler backend selection for every PMTiles writer.** `RAS2CNG_VECTOR_TILER` = `auto` (default: freestiler when the build has Arrow input and `buffer`: the gpt-cmdr fork build 0.3.0.1, GitHub Release assets only, see *Engine installs*; else tippecanoe when `tippecanoe` + `pmtiles` are on PATH; else any freestiler) | `tippecanoe` | `freestiler` (in-process Rust tiler, optional `ras2cng[freestiler]` extra). Both keep every feature at every zoom and keep shared mesh edges coincident (freestiler snaps vertices to the tile pixel grid). **Arrow input** (`RAS2CNG_FREESTILER_INPUT` = `auto`|`arrow`|`geojson`) skips NDGeoJSON: mesh layers not joined to results go from the archive GeoParquet in the model CRS and reproject in Rust; layers the viewer already reprojected (sparse layers, cached mesh, joined results) travel as EPSG:4326 Arrow IPC spills, because exact manifest bounds and the join need pyproj anyway. freestiler refuses NAD83(HARN), NAD27 and others that PROJ shifts by datum grids (Davis is HARN); `run_freestiler` catches the `ValueError`, reprojects that CRS in Python and retries, remembering it per process. A layer with no CRS is refused. `delivery_table()` shapes properties as the GeoJSON path did (timestamp text, NaN as null, nested values as text, all-null columns dropped), so decoded tiles are identical across inputs: 0 differences of 6.1 M features on Bald Eagle p07, and none on Davis. `buffer=5` is always passed (`TILE_BUFFER`; freestiler's default is 12.8 px). Measured, Bald Eagle p07 viewer with vector results: Arrow 36 s / 3.4 GB, freestiler via GeoJSON 1,841 s / 6.3 GB, tippecanoe 1,745 s (Linux in Docker, with 8.5 GB of NDGeoJSON). Size against tippecanoe with the default (no feature IDs): -10.0% on Bald Eagle p07 results, +2.8% on Davis (with IDs: +5.4% / +12.3%). **The 40-50% gap is gone, but do not claim freestiler is always smaller:** sparse low-zoom `geometry.pmtiles` is +34% (Bald Eagle, IDs off) and +9.5% (Davis). Evidence: `feature_dev_notes/rust_engines/T12.md`. Multi-layer tilesets use `freestile_layers` (one layer in memory) when available. `RAS2CNG_VECTOR_TILE_GZIP_LEVEL` defaults to 6. MVT feature IDs are **off by default** (user decision 2026-10-31; -14% on the 87k mesh; tippecanoe tiles never had them); `RAS2CNG_VECTOR_TILE_IDS=1` / `--feature-ids` keeps them for consumers using MapLibre `feature-state` without `promoteId`. Never pass `drop_rate`.
- `raster_tiles.py` — **Raster tiler backend selection for every raster PMTiles writer** (terrain, Stored Maps, calculated layers, `pmtiles` on a GeoTIFF). `RAS2CNG_RASTER_TILER` = `auto` (default: terrio when installed and the raster is under `RAS2CNG_TERRIO_MAX_PIXELS`, 400 M) | `terrio` (pure-Rust, in-process; no GDAL/pmtiles CLIs) | `gdal` (the gdaldem→gdalwarp→gdal_translate→gdaladdo→`pmtiles convert` chain, streams any size). Measured on Bald Eagle: terrain 6.8 s vs 13.4 s, depth 1.6 s vs 4.7 s. terrio result overviews use `NEAREST`, holding visible wet area at 1.000× at z10 (GDAL chain: 1.225×), and its terrain edges carry ramp colour (the GDAL chain's were black despite the alpha-0 ramp trick, because `gdalwarp -dstalpha` writes black RGB). Terrain uses `AVERAGE`; override with `RAS2CNG_RASTER_TILE_RESAMPLING`. `package_maplibre_terrain(hillshade=True)` (default; CLI `--no-hillshade`) multiplies a terrio multidirectional hillshade into the display colours. It shades only visible pixels, because darkening RGB under alpha 0 brings the edge fringe back. It is not gdaldem-identical, which is accepted for display. Tests pin `gdal` via an autouse fixture in `tests/conftest.py`, because the older raster tests mock the GDAL subprocesses. With a streaming terrio (`dem` has `max_memory_mib` and `hillshade_strength`), relief or one-pass shaded relief goes file to file, the warp is chunked when `warp` has `max_memory_mib`, and PMTiles stream from the lazily opened warp. Tiles are byte-identical to the in-memory path and there is no pixel cap (Terrain50 z14: 13.4 GB -> 1.0 GB peak). `RAS2CNG_TERRIO_MAX_MEMORY_MIB` bounds the strips.
- `geoparquet.py` — **Every archive GeoParquet goes through `write_geoparquet()`.** It writes a GeoParquet 1.1 `bbox` struct covering (manifest 2.7; older archives have flat `bbox_xmin`… and every reader accepts both), ZSTD level 3 with dictionary encoding on string columns only, and row groups of 65,536 rows that also end at every layer boundary. Per-row-group dictionaries on unique numeric columns cost 12–15% and ZSTD level 1 another 15%. Rows are Hilbert-ordered within each layer by DuckDB `ST_Hilbert` of the bbox centre over the layer extent; a bit-identical NumPy port is used, with a warning, only when DuckDB spatial is missing. With sorting on, the writer stores `hilbert_index` and the spatial-index pass leaves the file alone. terrio's `write_geoparquet_layers(..., profile="archive")` (gpt-cmdr/terrio #27) is the **one** Rust implementation of the archive profile and the **default** engine; fast-hdf's former `export_geoparquet_ipc_files` is retired (fast-hdf 0.3.0), because terrio owns file formats. It writes rows, order, schema, row groups and `geo` metadata identical to the pyarrow engine, which stays the fallback and the oracle: `RAS2CNG_GEOPARQUET_WRITER=auto` (= `prefer-native`) uses terrio when it has `write_geoparquet_layers` and otherwise (or when terrio refuses a file) falls back to pyarrow, warning once per reason and recording it in `manifest.geometry[].geoparquet.fallback_reason`; `native` (`terrio`, `rust`; old `fast-hdf` accepted) is strict, `python` never tries it. The default is one switch, `geoparquet.AUTO_PREFERS_NATIVE`. ras2cng passes the Arrow table it would write plus each layer's identity (`KNOWN_IDENTITY` / `_layer_identity`); terrio splits on `layer`, so any Arrow type, Z and geometry collections pass. Measured on Bald Eagle p07 (16 files, fresh process each, loaded host): 10.4 s vs 12.7 s for pyarrow (an earlier, more loaded run: 19.9 vs 30.2 s, and 36.7 s for the old fast-hdf path), peak 561 vs 1184 MB; sizes -5.4..+2.2% vs pyarrow (terrio keeps WKB min/max statistics that pyarrow drops over 4 KB). Never call `gdf.to_parquet` for archive files.
- `archive_direct.py` — **Direct archive path (manifest 3.0).** `archive_project()` writes geometry and plan files through terrio's `write_hecras_archive` (gpt-cmdr/terrio #29, in the terrio fork build 0.5.0.1, which is published only as GitHub Release assets, not on PyPI; capability-detected on `terrio._terrio`), with the Python path kept as fallback and oracle. `RAS2CNG_ARCHIVE_ENGINE` / `--archive-engine` = `auto` (default; = `prefer-native`, but off when `RAS2CNG_GEOPARQUET_WRITER=python`) | `native` (`direct`, `terrio`, `rust`: raises instead of falling back for a whole file, even under `skip_errors`) | `python`. The one switch for the default is `AUTO_PREFERS_DIRECT`. Without the capability, or with `python`, output is what it was: Python path, manifest 2.7, no `archive` records. **Never lose data silently:** only `mesh_cells`, `mesh_faces` and the 2D summary variables are native, because their columns and values equal the Python path's. Every other geometry layer and every `*_text` layer comes from Python through `extra_layers`: the engine omits `river_reaches` and the joined `structures`/`cross_sections` columns and `centerlines` `length`, has no text layers, keeps float32/uint64 where Python has float64/int64, and refuses a whole breakline family over one bad row. They read in about 0.1 s per file. Do not widen the native list without equal columns and types: a same-named float32 native column and float64 Python column is refused (`field X is Float32 in one layer and Float64 in another`, no file written), which sends the file to the Python path. Refusals come back in `skipped`, not as errors: geometry refusals are filled by a second write, plan refusals are found first by a geometry-free `hecras_archive_layers` call and filled from Python, joined to `mesh_cells` read back from the archived file. Older plans whose summaries store no `Location` go this way (Spring p01), or wholly to Python when every variable is refused (Spring_BLE p02); `cell_cumulative_iter_lookup` and `starting_differences_*` are unreadable in Python too, so they are recorded with `filled: false` and are absent from either path's archive. `crs=` is always passed (frame CRS, else stored projection, else project; plans use their geometry file's), because the engine reads only a stored `Projection` and otherwise writes `crs: null`. `time_days=False`, `ghost_cell_points=False` and `layers=[...]` keep the engine's extra outputs out. `--no-sort`, `--results-layout variable`, `--results-geometry point|none`, steady plans and geometry without an HDF stay on Python. Each file's manifest `archive` record has `path`, `fallback_reason`, `native_layers`, `python_layers`, `refused`, `crs_source`, `seconds`. Measured (three runs, median, shared host; `feature_dev_notes/rust_engines/T11.md`): Bald Eagle p07 with results 29.1 s to 12.5 s, peak 4.0 GB to 0.76 GB; Spring 22.4 to 16.2 s, 3.0 to 2.0 GB; Spring_BLE 27.9 to 24.7 s, 4.0 to 3.1 GB. Every layer, row and non-null column is equal on five models; geometry files are byte-identical, and plan files differ only by dropping the all-null union columns of geometry layers.
- `postgis_sync.py` — GeoParquet → PostGIS via SQLAlchemy/GeoAlchemy2 with automatic spatial index creation.
- `terrain.py` — Terrain discovery, consolidation, and raster export. `discover_terrains()` finds terrain layers from rasmap. `consolidate_terrain()` provides a pure-Python publication mosaic. `export_modified_terrain()` delegates registered-terrain consolidation, exact 1x/2x/4x/8x downsampling, and modification rasterization to `RasTerrain.export_rasmapper_terrain()`; it does not use a geometry HDF or the deprecated row sampler. Native terrain export is qualified on Windows and Wine for HEC-RAS 6.4.1, 6.5, 6.6, and 7.0.1, with stable 7.1 forward-open behind ras-commander's exact managed-contract check. `export_mannings_raster()` (`ras2cng mannings`) writes RAS Mapper's "Final n Values" raster in Rust by default (`HdfLandCover.export_final_mannings_raster`, ras-commander 0.113 / fast-hdf 0.3; 0 px differences vs RAS Mapper 7.0.1 on Bald Eagle g01/g09/g11/g13 and Weise_2D, ~2 s vs 45-49 s, no HEC-RAS). Geometries the engine refuses (1D, pipe networks, virtual land cover, multi-part polygons) fall back with a warning to the old Python estimate (`estimate_final_mannings_raster`), which differs from RAS Mapper on 44-51% of common pixels and uses another grid; `--method rasmapper|estimate` forces one. The GeoTIFF tag `ras2cng_mannings_method` (`rasmapper-final-n` / `estimate`, plus `ras2cng_mannings_fallback_reason`) records which ran. Behaviour change in 0.13.0.
- `mapping.py` — Result raster generation. `generate_result_maps()` generates WSE/Depth/Velocity/etc. GeoTIFFs via `RasProcess.store_maps()` using `RasStoreMapHelper.exe`. Supports `depth_x_velocity_sq` and `inundation_boundary` map types (ras-commander ≥0.92.0). Deletes the large `PostProcessing.hdf` cache from outputs unless `keep_postprocessing=True`. Whole-simulation types `arrival_time`/`duration`/`percent_inundated` (threshold via `arrival_depth`; correct RasMapperLib XML names are `arrival time`/`duration`/`fraction inundated`) pass through natively on newer ras-commander or via a rasmap pre-injection shim on older versions (capability-detected from `store_maps`' signature). `recession` is a warned no-op — RasMapperLib has no recession MapType, and only RasMapperLib-native outputs are produced (a derived arrival+duration approach is sketched in `feature_dev_notes/adr_maps_implementation_plan.md` but intentionally not shipped pending methodology verification).
- `fast_map.py` — Rust mapping for **pure 2D models (FIM library scope)** (the default `map` engine; `--engine rasmapper` / env `RAS2CNG_MAP_ENGINE` forces RAS Mapper) via `RasFim` and `ras-commander-fast-map`, qualified against RAS Mapper stored maps. `map_plan()` does Max WSE/Depth, `map_velocity()` Max velocity, `map_derived()` Max D x V / D x V^2 / Froude (`RasFim.map_derived_max`, one `reduce_max/1` time-series pass), `map_whole_simulation()` arrival time, percent inundated and duration, `map_boundary()` the boundary (original single-source terrain TIFF + HDF); each product group falls back to RAS Mapper on its own, and `velocity_plan()` / `derived_plan()` / `whole_simulation_types()` route products before any terrain work. Storage areas are **opt-in** (`--fast-map-storage-areas`, `fast_map_storage_areas=True`, `RAS2CNG_FAST_MAP_STORAGE_AREAS=1`; user decision 2026-10-31: not a default until faster than RAS Mapper): by default a storage-area plan goes wholly to RAS Mapper and `MapResult.fast_map_skipped` records why; opted in, they are drawn as level pools when the engine advertises `storage-area-level-pool/1` (`storage_areas_supported()`; WSE/Depth/arrival/percent only). `--fast-map-terrain-crs-hash` (`fast_map_terrain_crs_fnv1a64`) passes a user-verified engine WKT hash for Rust boundaries. `with_terrain()` tries the priority-1 source TIFF (no HEC-RAS export), its CRS-canonical copy after a CRS refusal, the native effective terrain (`RasFim.build_effective_terrain`), then the export, and goes straight to the export for any terrain with modifications (`terrain.terrain_modifications()`; Rust terrain operations only on unmodified terrains); `MapResult.fast_map_terrain` records which. 1D, non-Max profiles, shear stress and unqualified render modes stay on RAS Mapper (`--fast-map-strict` raises for engine failures and non-Max profiles). `invalid_cells` (`--fast-map-invalid-cells`, ras-commander 0.110.2+) sets pixel ownership for cells whose faces do not form a valid polygon; the engine's `fim-invalid-cells/1` record lands in `MapResult.invalid_cells` and every fast-map manifest. Sloping WSE/Depth are written by the engine (terrio LZW, `--fast-map-writer auto`) when it advertises `native_geotiff_outputs`. New capabilities are detected from `RasFim`'s methods/kwargs. See *Mapping engines* below.
- `scaffold.py` — Barebones project synthesis for `map-hdf`. `read_plan_hdf_metadata()` extracts project name, plan number, ShortID, units, and projection WKT from a plan HDF; `build_scaffold()` synthesizes `.prj`/`.pNN`/`.rasmap` stubs, hardlinks the HDF under its canonical name, and builds the HEC-RAS terrain from raw TIFFs (`RasTerrain.create_terrain_from_rasters`) or imports a pre-built terrain HDF sidecar set. The ESRI projection is written to `Terrain\Projection.prj` — never in the project root, where it would collide with the HEC-RAS `.prj`. Scaffolds carry a `.ras2cng-scaffold` marker and are reused across runs when the source HDF is unchanged.

**HDF engine**: ras-commander ≥0.110.1 (floor 0.113.0, which requires fast-hdf ≥0.3.0; its `[full]` extra brings fast-map ≥0.12) reads HDF through the Rust `ras-commander-fast-hdf` engine by default (h5py fallback; `RAS_COMMANDER_FAST_HDF=auto|h5py|native`). ras2cng needs no code for this — `get_mesh_cell_polygons` alone is 4–19× faster.

**Data flow**: HEC-RAS files → ras-commander → GeoDataFrame → GeoParquet (ZSTD compression, bbox columns, Hilbert sorted). Archive output uses `_write_geoparquet()` which adds `covering` metadata for spatial predicate pushdown.

## Engine installs

- **PyPI, required:** `ras-commander[full]>=0.113.0` brings ras-commander-fast-hdf 0.3.0 and
  ras-commander-fast-map 0.12.0 (storage-area level pools, effective terrain, velocity, Max
  derived products, duration, Rust boundaries, the Rust Final n Values raster).
- **Fork builds, optional** (GitHub Release assets on the gpt-cmdr forks, never PyPI, which
  rejects direct-URL dependencies; the `terrio`/`freestiler` extras keep the PyPI floors and
  every fork feature is capability-detected, so PyPI builds keep the Python paths):
  - freestiler **0.3.0.1** (Arrow input, `buffer`):
    `pip install freestiler==0.3.0.1 --find-links https://github.com/gpt-cmdr/freestiler/releases/expanded_assets/v0.3.0.1-gpt-cmdr`
  - terrio **0.5.0.1** (direct archive path, native GeoParquet writer, COG report, streaming
    tiles): `pip install terrio==0.5.0.1 --find-links https://github.com/gpt-cmdr/terrio/releases/expanded_assets/gpt-cmdr-v0.5.0.1`
  - In a uv project env use `uv pip install ...` with the same arguments, and re-sync with
    `uv sync --all-extras --inexact` so the sync does not replace them with the PyPI builds.

## Mapping engines (agents: read before running `ras2cng map`)

**fast-map is the default** `ras2cng map` engine (`fast_map.DEFAULT_MAP_ENGINE`).
It is built for **FIM library creation on pure 2D models** (no 1D reaches or
cross sections; storage areas only on opt-in), not for general use
across all model types. Every fast-map product is **qualified against RAS
Mapper output**: pixel conformance against native HEC-RAS 7.0.1 stored maps
on the named models. Everything else falls back to RAS Mapper **per product
group**, with the reason printed and recorded in `MapResult.engines` and
`MapResult.fast_map_skipped`: 1D elements, storage areas (unless opted in),
non-Max profiles, shear stress, products not qualified with storage areas,
and render modes without qualification. What fast-map maps
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
| Velocity (fast-map #20 `velocity_depth`) | 0 value / 0 mask differences on bridges p05 Max, sloping (973,902 px) and horizontal (973,532 px); the earlier 1,196 / 826 px mask difference was a reference artifact | **default** in sloping and horizontal; slopingPretty opt-in (`--fast-map-velocity`: mask follows the slopingPretty depth, which differs on bridges Max); not with storage areas; older engines: opt-in, plain sloping |
| D x V, D x V², Froude, Max (fast-map #20 `reduce_max/1`) | bridges p05 Max over 433 times, sloping and horizontal: 0 / 0; Davis ≤5 px within 2 ulp | **default** in sloping and horizontal (`--no-fast-map-derived`); slopingPretty and storage areas stay on RAS Mapper |
| Shear stress | not implemented | RAS Mapper |
| Inundation boundary | deterministic contours, **not** vector parity | see below; not with storage areas |

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
  (user decision 2026-10-30): `terrain.terrain_modifications()` reads the
  terrain HDF's `/Modifications` group and the `.rasmap`'s
  `ElevationModificationGroup` layers first, and any modification (or an
  unreadable terrain HDF) sends the plan's terrain to RAS Mapper's export:
  the source TIFF and the effective terrain are skipped, and the Rust
  boundary is refused so RAS Mapper draws it. Mapping then runs on that
  export, whose WSE/Depth fast-map reproduces exactly. This holds even with
  an engine that accepts modifications: fast-map #21 / ras-commander #790
  (T14) are **parked**, because none of 11 real modified terrains came out
  pixel-identical (`feature_dev_notes/rust_engines/T14.md`).
  `--no-fast-map-effective-terrain` skips it; `--fast-map-terrain` supplies
  an export instead; `--no-fast-map-source-terrain` skips the source TIFF.
  `MapResult.fast_map_terrain` records `source`, `source-canonical`,
  `effective`, `export` or `supplied` per product.
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
  `--fast-map-terrain-hdf`) and horizontal or plain sloping render mode, no
  storage areas, and ras-commander with `RasFim.map_inundation_boundaries`
  (0.113.0); otherwise they fall back to RAS Mapper automatically. A terrain
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
- Everything above is in the released engines: ras-commander 0.113.0
  (#724, #755, #757, #758, #788, #786) with ras-commander-fast-map 0.12.0
  (#11-#13, #18-#20) and ras-commander-fast-hdf 0.3.0, which ras2cng
  requires. Capabilities are still detected at run time, so an older engine
  keeps the previous behaviour.

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
- **Vector tiles never drop features; tile weight is bounded by each tileset's minimum zoom.** `maplibre.py` splits dense mesh (`mesh_cells`/`mesh_faces`, via `_is_detail_geometry`) into `geometry-detail.pmtiles`, and results joined to those elements into `results-detail.pmtiles`; both start at the *detail floor* (`_detail_min_zoom`: z13, raised for a fine mesh to the first zoom where the median cell spans 2 CSS px). Sparse engineering layers stay in `geometry.pmtiles`, and sparse-joined results (cross sections, reference lines/points) in `results.pmtiles`, across the full range. Below z13 complete mesh tiles are multi-megabyte (1.2–1.6 MB at z12, 4–9 MB at z10). Do **not** reintroduce `--drop-densest-as-needed`: it renders a thinned mesh as visible stripes (17–54% of cells drawn at z10–z11) and was removed for that reason. Never use `--drop-fraction-as-needed` (crashes tippecanoe on mixed dense layers) or freestiler's `drop_rate` (drops by bounding-box size, emptying whole zoom levels of a uniform mesh and cutting the refined cells along breaklines first).
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
