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
# extra installed: 468 passed / 4 skipped (the 4 need the I: drive).
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
  - **terrio engine** (`RAS2CNG_COG_ENGINE=auto|terrio|gdal`, capability-detected via `Raster.write(metadata=)`): terrio writes the whole COG — area-matched overviews and encoding, LERC included. Lossless output (float DEFLATE/ZSTD, integer, categorical) is pixel-identical to the GDAL path at every level with the same tags. **LERC holds the same `MAX_Z_ERROR` bound but not the same values**: terrio uses the pure-Rust `lerc-writer`, which stores each 8x8 micro-block's exact minimum as its offset. On Bald Eagle Depth (Max) both encoders' max error is 0.01000 and mean 0.0048, and 8.1% of pixels differ by at most one quantization step (<=0.02). Sizes vs GDAL (terrio main with #22, which masks partial-edge-tile padding as GDAL does): `LERC_DEFLATE` +0.37% Depth / -0.06% Terrain50; plain `LERC` +17.7% Depth / +0.13% Terrain50. The rest of the plain-LERC gap is lerc-writer's encoder; a fix is pending upstream (roteiro-gis/lerc-rust #48), not depended on. Mask, `compression_*` and overview provenance tags are identical. **Code relying on byte or value equality with GDAL-written artifacts must ask for lossless.** `overview_area_ratios` come from the exact `_scan_area_matched` pass, run before the write because the tags go into the COG header; it is also the NaN-under-numeric-nodata gate. terrio #18's `Raster.write()` report is checked against the scan (factors, valid count, ratios at `%.6f`) and any mismatch discards terrio's file for the GDAL path (`_check_terrio_report`). The scan is ~45% of terrio's write time; removing it needs terrio to write the report into the default domain (draft on the gpt-cmdr fork). ras2cng never passes overview factors to terrio's COG writer (terrio #19 honours them); it relies on the automatic pyramid matching `overview_factors()`. NaN under a numeric nodata, masks/alpha, multi-band, no nodata, unknown options and >400 M px stay on GDAL. `validate_cog` prefers `terrio.validate_cog`, falling back to rio-cogeo. terrio still reads the base level whole, so peak memory stays near raster size. Tests: `tests/test_terrio_equivalence.py`.
  - Env knobs: `RAS2CNG_COG_COMPRESS` (set `ZSTD` for internal-only archives), `RAS2CNG_COG_PREDICTOR`, `RAS2CNG_AREA_MATCHED_MAX_PIXELS`.
- `pmtiles.py` — Legacy CLI surface for `ras2cng pmtiles`. Vector: GeoParquet → **reproject to EPSG:4326** → newline-delimited GeoJSON → tippecanoe. Tippecanoe does not reproject; feeding it projected coordinates yields a valid, silently wrong tileset, so a layer with no CRS is refused rather than tiled. Raster generation is delegated to `maplibre.py`. Requires external CLIs: `tippecanoe`, `gdal_translate`, `pmtiles` — or, for vectors, the freestiler backend (see `vector_tiles.py`).
- `vector_tiles.py` — **Vector tiler backend selection for every PMTiles writer.** `RAS2CNG_VECTOR_TILER` = `auto` (default: tippecanoe when `tippecanoe` + `pmtiles` are on PATH, else freestiler when installed) | `tippecanoe` | `freestiler` (in-process Rust tiler, optional `ras2cng[freestiler]` extra). Both keep every feature at every zoom and keep shared mesh edges coincident (freestiler snaps vertices to the tile pixel grid). Measured on HEC-RAS meshes, freestiler is 8–20× faster with identical completeness but 40–50% larger archives (it does not simplify), and it holds every layer of a tileset in memory — so tippecanoe stays the preferred backend where it is installed. Evidence: `feature_dev_notes/vector_tiler_evaluation_2026-10-12.md`. Multi-layer tilesets use `freestile_layers` (one layer in memory) when available. `RAS2CNG_VECTOR_TILE_GZIP_LEVEL` defaults to 6. `RAS2CNG_VECTOR_TILE_IDS=0` / `--no-feature-ids` drops MVT feature IDs (off by default; -14% on the 87k mesh; tippecanoe tiles never had them).
- `raster_tiles.py` — **Raster tiler backend selection for every raster PMTiles writer** (terrain, Stored Maps, calculated layers, `pmtiles` on a GeoTIFF). `RAS2CNG_RASTER_TILER` = `auto` (default: terrio when installed and the raster is under `RAS2CNG_TERRIO_MAX_PIXELS`, 400 M) | `terrio` (pure-Rust, in-process; no GDAL/pmtiles CLIs) | `gdal` (the gdaldem→gdalwarp→gdal_translate→gdaladdo→`pmtiles convert` chain, streams any size). Measured on Bald Eagle: terrain 6.8 s vs 13.4 s, depth 1.6 s vs 4.7 s. terrio result overviews use `NEAREST`, holding visible wet area at 1.000× at z10 (GDAL chain: 1.225×), and its terrain edges carry ramp colour (the GDAL chain's were black despite the alpha-0 ramp trick, because `gdalwarp -dstalpha` writes black RGB). Terrain uses `AVERAGE`; override with `RAS2CNG_RASTER_TILE_RESAMPLING`. `package_maplibre_terrain(hillshade=True)` (default; CLI `--no-hillshade`) multiplies a terrio multidirectional hillshade into the display colours. It shades only visible pixels, because darkening RGB under alpha 0 brings the edge fringe back. It is not gdaldem-identical, which is accepted for display. Tests pin `gdal` via an autouse fixture in `tests/conftest.py`, because the older raster tests mock the GDAL subprocesses. With a streaming terrio (`dem` has `max_memory_mib` and `hillshade_strength`), relief or one-pass shaded relief goes file to file, the warp is chunked when `warp` has `max_memory_mib`, and PMTiles stream from the lazily opened warp. Tiles are byte-identical to the in-memory path and there is no pixel cap (Terrain50 z14: 13.4 GB -> 1.0 GB peak). `RAS2CNG_TERRIO_MAX_MEMORY_MIB` bounds the strips.
- `geoparquet.py` — **Every archive GeoParquet goes through `write_geoparquet()`.** It writes a GeoParquet 1.1 `bbox` struct covering (manifest 2.7; older archives have flat `bbox_xmin`… and every reader accepts both), ZSTD level 3 with dictionary encoding on string columns only, and row groups of 65,536 rows that also end at every layer boundary. Per-row-group dictionaries on unique numeric columns cost 12–15% and ZSTD level 1 another 15%. Rows are Hilbert-ordered within each layer by DuckDB `ST_Hilbert` of the bbox centre over the layer extent; a bit-identical NumPy port is used, with a warning, only when DuckDB spatial is missing. With sorting on, the writer stores `hilbert_index` and the spatial-index pass leaves the file alone. fast-hdf's `export_geoparquet_ipc_files` (fast-hdf #36/#38/#39) writes identical rows, order, schema and metadata and is the **default**: `RAS2CNG_GEOPARQUET_WRITER=auto` (= `prefer-native`) uses it when the installed build accepts the file and otherwise falls back to pyarrow, warning once per reason and recording it in `manifest.geometry[].geoparquet.fallback_reason`; `native` is strict, `python` never tries it. The default is one switch, `geoparquet.AUTO_PREFERS_NATIVE`. `_contract_type()` mirrors the merged engine: float32, tz-naive ms/us/ns timestamps, top-level null, and list/struct nesting pass; large_list, fixed_size_list and tz-aware timestamps stay on pyarrow. Both engines name list items `element` (pyarrow's compliant default). ras2cng requests `statistics: "chunk"` (pyarrow's statistics, no page index) when the build accepts it. Measured on Bald Eagle p07 (loaded host): native files +1.4% (+4.4% with the Rust default page indexes), and the archive is **not faster** (55-67 s vs 49-54 s): the Rust write (10-15 s) replaces pyarrow's write plus Python Hilbert keys (~10 s), and Arrow IPC staging adds ~5 s; most time is the ras-commander geometry read/merge (10-14 s), frame-to-Arrow (5 s) and row-identity checks (3 s). Never call `gdf.to_parquet` for archive files.
- `postgis_sync.py` — GeoParquet → PostGIS via SQLAlchemy/GeoAlchemy2 with automatic spatial index creation.
- `terrain.py` — Terrain discovery, consolidation, and raster export. `discover_terrains()` finds terrain layers from rasmap. `consolidate_terrain()` provides a pure-Python publication mosaic. `export_modified_terrain()` delegates registered-terrain consolidation, exact 1x/2x/4x/8x downsampling, and modification rasterization to `RasTerrain.export_rasmapper_terrain()`; it does not use a geometry HDF or the deprecated row sampler. Native terrain export is qualified on Windows and Wine for HEC-RAS 6.4.1, 6.5, 6.6, and 7.0.1, with stable 7.1 forward-open behind ras-commander's exact managed-contract check. `export_mannings_raster()` separately wraps `HdfLandCover.compute_final_mannings_raster()` and still requires HEC-RAS 6.6+ on Windows.
- `mapping.py` — Result raster generation. `generate_result_maps()` generates WSE/Depth/Velocity/etc. GeoTIFFs via `RasProcess.store_maps()` using `RasStoreMapHelper.exe`. Supports `depth_x_velocity_sq` and `inundation_boundary` map types (ras-commander ≥0.92.0). Deletes the large `PostProcessing.hdf` cache from outputs unless `keep_postprocessing=True`. Whole-simulation types `arrival_time`/`duration`/`percent_inundated` (threshold via `arrival_depth`; correct RasMapperLib XML names are `arrival time`/`duration`/`fraction inundated`) pass through natively on newer ras-commander or via a rasmap pre-injection shim on older versions (capability-detected from `store_maps`' signature). `recession` is a warned no-op — RasMapperLib has no recession MapType, and only RasMapperLib-native outputs are produced (a derived arrival+duration approach is sketched in `feature_dev_notes/adr_maps_implementation_plan.md` but intentionally not shipped pending methodology verification).
- `fast_map.py` — Optional Rust mapping for **pure 2D models (FIM library scope)** (`map --engine fast-map`, env `RAS2CNG_MAP_ENGINE`) via `RasFim` and `ras-commander-fast-map`, qualified against RAS Mapper stored maps. `map_plan()` does Max WSE/Depth (+ opt-in plain-sloping velocity), `map_whole_simulation()` arrival time and percent inundated, `map_boundary()` the boundary (original single-source terrain TIFF + HDF); each falls back to RAS Mapper on its own. `with_terrain()` tries the priority-1 source TIFF (no HEC-RAS export), its CRS-canonical copy after a CRS refusal, then the export; `MapResult.fast_map_terrain` records which. Storage areas, 1D, non-Max profiles and duration stay on RAS Mapper (`--fast-map-strict` raises instead). New capabilities are detected from `RasFim`'s methods/kwargs. See *Mapping engines* below.
- `scaffold.py` — Barebones project synthesis for `map-hdf`. `read_plan_hdf_metadata()` extracts project name, plan number, ShortID, units, and projection WKT from a plan HDF; `build_scaffold()` synthesizes `.prj`/`.pNN`/`.rasmap` stubs, hardlinks the HDF under its canonical name, and builds the HEC-RAS terrain from raw TIFFs (`RasTerrain.create_terrain_from_rasters`) or imports a pre-built terrain HDF sidecar set. The ESRI projection is written to `Terrain\Projection.prj` — never in the project root, where it would collide with the HEC-RAS `.prj`. Scaffolds carry a `.ras2cng-scaffold` marker and are reused across runs when the source HDF is unchanged.

**HDF engine**: ras-commander ≥0.110.1 reads HDF through the Rust `ras-commander-fast-hdf` engine by default (h5py fallback; `RAS_COMMANDER_FAST_HDF=auto|h5py|native`). ras2cng needs no code for this — `get_mesh_cell_polygons` alone is 4–19× faster.

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
