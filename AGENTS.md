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
- `catalog.py` — Schema 3.0 (2.7 when only the Python path ran). `Manifest` dataclass manages `manifest.json`. `ManifestGeomEntry` has a `parquet` field (single consolidated file) and `layers` list with `filter_value` for each layer. `ManifestPlanEntry` similarly has a `parquet` field. `project_parquet` field on `Manifest` references the metadata file.
- `geometry.py` — Detects file type (HDF `.g??.hdf` vs text `.g??`) and routes to ras-commander parsers. `HDF_LAYERS` dict maps 10 layer names to `(class, method_name)` for dispatch. `merge_all_layers()` consolidates all HDF + text layers into a single GeoDataFrame with `layer` column (text layers get `_text` suffix). `_hilbert_sort()` uses DuckDB `ST_Hilbert`; before 0.13 it called a function DuckDB does not have and silently did nothing (the spatial-index post-pass sorted instead). Legacy `export_all_hdf_layers()` / `export_all_text_layers()` remain for backward compat.
- `results.py` — Exports 2D mesh summary variables from plan HDF files. `merge_all_variables()` consolidates all variables into a single GeoDataFrame with `layer` column. Accepts `mesh_cells_gdf` GeoDataFrame directly for polygon join (avoids write-then-read).
- `duckdb_session.py` — `DuckSession` class wraps DuckDB with auto-loaded spatial extension. `register_parquet()` detects WKB geometry columns and converts them to DuckDB GEOMETRY type. The table alias is always `_`.
- `pmtiles.py` — Dispatches between vector (GeoParquet → GeoJSON → tippecanoe → PMTiles, or GeoParquet → freestiler → PMTiles) and raster (GeoTIFF → gdal_translate → pmtiles) pipelines. Requires external CLIs: `tippecanoe`, `gdal_translate`, `pmtiles`; vectors can instead use the optional `ras2cng[freestiler]` extra, selected by `RAS2CNG_VECTOR_TILER` (`vector_tiles.py`; `auto` prefers a freestiler build with Arrow input). Vector tiles never drop features to cap tile size.
- `raster_tiles.py` — **Raster tiler backend selection for every raster PMTiles writer** (terrain, Stored Maps, calculated layers, `pmtiles` on a GeoTIFF). `RAS2CNG_RASTER_TILER` = `auto` (default: terrio when installed and the raster is under `RAS2CNG_TERRIO_MAX_PIXELS`, 400 M) | `terrio` (pure-Rust, in-process; no GDAL/pmtiles CLIs) | `gdal` (the gdaldem→gdalwarp→gdal_translate→gdaladdo→`pmtiles convert` chain, streams any size). Measured on Bald Eagle: terrain 6.8 s vs 13.4 s, depth 1.6 s vs 4.7 s. terrio result overviews use `NEAREST`, holding visible wet area at 1.000× at z10 (GDAL chain: 1.225×), and its terrain edges carry ramp colour (the GDAL chain's were black despite the alpha-0 ramp trick, because `gdalwarp -dstalpha` writes black RGB). Terrain uses `AVERAGE`; override with `RAS2CNG_RASTER_TILE_RESAMPLING`. `package_maplibre_terrain(hillshade=True)` (default; CLI `--no-hillshade`) multiplies a terrio multidirectional hillshade into the display colours. It shades only visible pixels, because darkening RGB under alpha 0 brings the edge fringe back. It is not gdaldem-identical, which is accepted for display. Tests pin `gdal` via an autouse fixture in `tests/conftest.py`, because the older raster tests mock the GDAL subprocesses. With a streaming terrio (`dem` has `max_memory_mib` and `hillshade_strength`), relief or one-pass shaded relief goes file to file, the warp is chunked when `warp` has `max_memory_mib`, and PMTiles stream from the lazily opened warp. Tiles are byte-identical to the in-memory path and there is no pixel cap (Terrain50 z14: 13.4 GB -> 1.0 GB peak). `RAS2CNG_TERRIO_MAX_MEMORY_MIB` bounds the strips.
- `geoparquet.py` — **Every archive GeoParquet goes through `write_geoparquet()`.** It writes a GeoParquet 1.1 `bbox` struct covering (manifest 2.7; older archives have flat `bbox_xmin`… and every reader accepts both), ZSTD level 3 with dictionary encoding on string columns only, and row groups of 65,536 rows that also end at every layer boundary. Per-row-group dictionaries on unique numeric columns cost 12–15% and ZSTD level 1 another 15%. Rows are Hilbert-ordered within each layer by DuckDB `ST_Hilbert` of the bbox centre over the layer extent; a bit-identical NumPy port is used, with a warning, only when DuckDB spatial is missing. With sorting on, the writer stores `hilbert_index` and the spatial-index pass leaves the file alone. terrio's `write_geoparquet_layers(..., profile="archive")` (CLB-Engineering-Corporation/terrio #27) is the **one** Rust implementation of the archive profile and the **default** engine; fast-hdf's former `export_geoparquet_ipc_files` is retired (fast-hdf 0.3.0), because terrio owns file formats. It writes rows, order, schema, row groups and `geo` metadata identical to the pyarrow engine, which stays the fallback and the oracle: `RAS2CNG_GEOPARQUET_WRITER=auto` (= `prefer-native`) uses terrio when it has `write_geoparquet_layers` and otherwise (or when terrio refuses a file) falls back to pyarrow, warning once per reason and recording it in `manifest.geometry[].geoparquet.fallback_reason`; `native` (`terrio`, `rust`; old `fast-hdf` accepted) is strict, `python` never tries it. The default is one switch, `geoparquet.AUTO_PREFERS_NATIVE`. ras2cng passes the Arrow table it would write plus each layer's identity (`KNOWN_IDENTITY` / `_layer_identity`); terrio splits on `layer`, so any Arrow type, Z and geometry collections pass. Measured on Bald Eagle p07 (16 files, fresh process each, loaded host): 10.4 s vs 12.7 s for pyarrow (an earlier, more loaded run: 19.9 vs 30.2 s, and 36.7 s for the old fast-hdf path), peak 561 vs 1184 MB; sizes -5.4..+2.2% vs pyarrow (terrio keeps WKB min/max statistics that pyarrow drops over 4 KB). Never call `gdf.to_parquet` for archive files.
- `archive_direct.py` — **Direct archive path (manifest 3.0).** `archive_project()` writes geometry and plan files through terrio's `write_hecras_archive` (CLB-Engineering-Corporation/terrio #29, in the CLB terrio build 0.5.0.1, which is published only as GitHub Release assets on CLB-Engineering-Corporation/terrio-clb, not on PyPI; capability-detected on `terrio._terrio`), with the Python path kept as fallback and oracle. `RAS2CNG_ARCHIVE_ENGINE` / `--archive-engine` = `auto` (default; = `prefer-native`, but off when `RAS2CNG_GEOPARQUET_WRITER=python`) | `native` (`direct`, `terrio`, `rust`: raises instead of falling back for a whole file, even under `skip_errors`) | `python`. The one switch for the default is `AUTO_PREFERS_DIRECT`. Without the capability, or with `python`, output is what it was: Python path, manifest 2.7, no `archive` records. **Never lose data silently:** only `mesh_cells`, `mesh_faces` and the 2D summary variables are native, because their columns and values equal the Python path's. Every other geometry layer and every `*_text` layer comes from Python through `extra_layers`: the engine omits `river_reaches` and the joined `structures`/`cross_sections` columns and `centerlines` `length`, has no text layers, keeps float32/uint64 where Python has float64/int64, and refuses a whole breakline family over one bad row. They read in about 0.1 s per file. Do not widen the native list without equal columns and types: a same-named float32 native column and float64 Python column is refused (`field X is Float32 in one layer and Float64 in another`, no file written), which sends the file to the Python path. Refusals come back in `skipped`, not as errors: geometry refusals are filled by a second write, plan refusals are found first by a geometry-free `hecras_archive_layers` call and filled from Python, joined to `mesh_cells` read back from the archived file. Older plans whose summaries store no `Location` go this way (Spring p01), or wholly to Python when every variable is refused (Spring_BLE p02); `cell_cumulative_iter_lookup` and `starting_differences_*` are unreadable in Python too, so they are recorded with `filled: false` and are absent from either path's archive. `crs=` is always passed (frame CRS, else stored projection, else project; plans use their geometry file's), because the engine reads only a stored `Projection` and otherwise writes `crs: null`. `time_days=False`, `ghost_cell_points=False` and `layers=[...]` keep the engine's extra outputs out. `--no-sort`, `--results-layout variable`, `--results-geometry point|none`, steady plans and geometry without an HDF stay on Python. Each file's manifest `archive` record has `path`, `fallback_reason`, `native_layers`, `python_layers`, `refused`, `crs_source`, `seconds`. Measured (three runs, median, shared host; `feature_dev_notes/rust_engines/T11.md`): Bald Eagle p07 with results 29.1 s to 12.5 s, peak 4.0 GB to 0.76 GB; Spring 22.4 to 16.2 s, 3.0 to 2.0 GB; Spring_BLE 27.9 to 24.7 s, 4.0 to 3.1 GB. Every layer, row and non-null column is equal on five models; geometry files are byte-identical, and plan files differ only by dropping the all-null union columns of geometry layers.
- `postgis_sync.py` — GeoParquet → PostGIS via SQLAlchemy/GeoAlchemy2 with automatic spatial index creation.
- `terrain.py` — Terrain discovery, consolidation, and raster export. `discover_terrains()` finds terrain layers from rasmap. `consolidate_terrain()` provides a pure-Python publication mosaic. `export_modified_terrain()` delegates registered-terrain consolidation, exact 1x/2x/4x/8x downsampling, and modification rasterization to `RasTerrain.export_rasmapper_terrain()`; it does not use a geometry HDF or the deprecated row sampler. Native terrain export is qualified on Windows and Wine for HEC-RAS 6.4.1, 6.5, 6.6, and 7.0.1, with stable 7.1 forward-open behind ras-commander's exact managed-contract check. `export_mannings_raster()` (`ras2cng mannings`) writes RAS Mapper's "Final n Values" raster in Rust by default (`HdfLandCover.export_final_mannings_raster`, ras-commander 0.113 / fast-hdf 0.3; 0 px differences vs RAS Mapper 7.0.1 on Bald Eagle g01/g09/g11/g13 and Weise_2D, ~2 s vs 45-49 s, no HEC-RAS). Geometries the engine refuses (1D, pipe networks, virtual land cover, multi-part polygons) fall back with a warning to the old Python estimate (`estimate_final_mannings_raster`), which differs from RAS Mapper on 44-51% of common pixels and uses another grid; `--method rasmapper|estimate` forces one. The GeoTIFF tag `ras2cng_mannings_method` (`rasmapper-final-n` / `estimate`, plus `ras2cng_mannings_fallback_reason`) records which ran. Behaviour change in 0.13.0.
- `mapping.py` — Result raster generation. `generate_result_maps()` generates WSE/Depth/Velocity/etc. GeoTIFFs via `RasProcess.store_maps()` using `RasStoreMapHelper.exe`. Supports `depth_x_velocity_sq` and `inundation_boundary` map types (ras-commander ≥0.92.0).
- `fast_map.py` — Rust mapping for **pure 2D models (FIM library scope)** (the default `map` engine; `--engine rasmapper` / env `RAS2CNG_MAP_ENGINE` forces RAS Mapper) via `RasFim` and `ras-commander-fast-map`, qualified against RAS Mapper stored maps. `map_plan()` does Max WSE/Depth, `map_velocity()` Max velocity, `map_derived()` Max D x V / D x V^2 / Froude (`RasFim.map_derived_max`, one `reduce_max/1` time-series pass), `map_whole_simulation()` arrival time, percent inundated and duration, `map_boundary()` the boundary (original single-source terrain TIFF + HDF); each product group falls back to RAS Mapper on its own, and `velocity_plan()` / `derived_plan()` / `whole_simulation_types()` route products before any terrain work. Storage areas are **opt-in** (`--fast-map-storage-areas`, `fast_map_storage_areas=True`, `RAS2CNG_FAST_MAP_STORAGE_AREAS=1`; user decision 2026-10-31: not a default until faster than RAS Mapper): by default a storage-area plan goes wholly to RAS Mapper and `MapResult.fast_map_skipped` records why; opted in, they are drawn as level pools when the engine advertises `storage-area-level-pool/1` (`storage_areas_supported()`; WSE/Depth/arrival/percent only). `--fast-map-terrain-crs-hash` (`fast_map_terrain_crs_fnv1a64`) passes a user-verified engine WKT hash for Rust boundaries. `with_terrain()` tries the priority-1 source TIFF (no HEC-RAS export), its CRS-canonical copy after a CRS refusal, the native effective terrain (`RasFim.build_effective_terrain`), then the export, and goes straight to the export for any terrain with modifications (`terrain.terrain_modifications()`; Rust terrain operations only on unmodified terrains); `MapResult.fast_map_terrain` records which. 1D, non-Max profiles, shear stress and unqualified render modes stay on RAS Mapper (`--fast-map-strict` raises for engine failures and non-Max profiles). `invalid_cells` (`--fast-map-invalid-cells`, ras-commander 0.110.2+) sets pixel ownership for cells whose faces do not form a valid polygon; the engine's `fim-invalid-cells/1` record lands in `MapResult.invalid_cells` and every fast-map manifest. Sloping WSE/Depth are written by the engine (terrio LZW, `--fast-map-writer auto`) when it advertises `native_geotiff_outputs`. New capabilities are detected from `RasFim`'s methods/kwargs. See *Mapping engines* below.

**Data flow**: HEC-RAS files → ras-commander → GeoDataFrame → GeoParquet (ZSTD compression, bbox columns, Hilbert sorted). Archive output uses `_write_geoparquet()` which adds `covering` metadata for spatial predicate pushdown.

## Engine installs

- **PyPI, required:** `ras-commander[full]>=0.113.0` brings ras-commander-fast-hdf 0.3.0 and
  ras-commander-fast-map 0.12.0 (storage-area level pools, effective terrain, velocity, Max
  derived products, duration, Rust boundaries, the Rust Final n Values raster).
- **Fork builds, optional** (GitHub Release assets: freestiler on the gpt-cmdr fork, terrio on
  CLB-Engineering-Corporation/terrio-clb; never PyPI, which
  rejects direct-URL dependencies; the `terrio`/`freestiler` extras keep the PyPI floors and
  every fork feature is capability-detected, so PyPI builds keep the Python paths):
  - freestiler **0.3.0.1** (Arrow input, `buffer`):
    `pip install freestiler==0.3.0.1 --find-links https://github.com/gpt-cmdr/freestiler/releases/expanded_assets/v0.3.0.1-gpt-cmdr`
  - terrio **0.5.0.1** (direct archive path, native GeoParquet writer, COG report, streaming
    tiles): `pip install terrio==0.5.0.1 --find-links https://github.com/CLB-Engineering-Corporation/terrio-clb/releases/expanded_assets/gpt-cmdr-v0.5.0.1`
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
