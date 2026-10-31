# ras2cng 0.13.0

Rust engines release. Every engine feature is capability-detected: with older engines, or the PyPI builds of terrio and freestiler, ras2cng keeps its Python paths.

## Requirements

- Requires ras-commander 0.113.0 (`ras-commander[full]>=0.113.0`; was 0.100.0), which brings ras-commander-fast-hdf 0.3.0 and, through `full`, ras-commander-fast-map 0.12.0. ras-commander reads HEC-RAS HDF results through the Rust engine where it proves the layout.
- Optional fork builds, published only as GitHub Release assets on the gpt-cmdr forks (not on PyPI; the `terrio` and `freestiler` extras keep the PyPI floors):
  - freestiler 0.3.0.1: `pip install freestiler==0.3.0.1 --find-links https://github.com/gpt-cmdr/freestiler/releases/expanded_assets/v0.3.0.1-gpt-cmdr`
  - terrio 0.5.0.1: `pip install terrio==0.5.0.1 --find-links https://github.com/gpt-cmdr/terrio/releases/expanded_assets/gpt-cmdr-v0.5.0.1`

## Behaviour changes

- **`ras2cng mannings` writes RAS Mapper's "Final n Values" raster** in Rust (`HdfLandCover.export_final_mannings_raster`): 0 differing pixels against RAS Mapper 7.0.1's Export Raster on Bald Eagle g01, g09, g11, g13 and Weise_2D, about 2 s against 45-49 s, with no HEC-RAS. **The output changes:** the raster written before this release was a Python estimate that differed from RAS Mapper on 44-51% of common pixels (it ignored calibration-table base n values, used the land-cover TIFF's grid clipped to the perimeter plus 500 units, and left pixels with no class empty instead of taking the 2D area's default n). The grid is now RAS Mapper's (geometry extent, land-cover cell size). Geometries the engine refuses (1D elements, pipe networks, virtual land-cover layers, multi-part polygons) fall back to the estimate with a printed warning. `--method rasmapper` refuses instead; `--method estimate` keeps the old output. The GeoTIFF tag `ras2cng_mannings_method` (`rasmapper-final-n` or `estimate`, plus `ras2cng_mannings_fallback_reason`) records which method ran.
- **Vector-tile feature IDs are off by default.** `--feature-ids` (or `RAS2CNG_VECTOR_TILE_IDS=1`) keeps them, for consumers that use MapLibre `feature-state` without `promoteId`; the ras2cng viewer does not read them and tippecanoe tiles never had them. With the freestiler fork build, the Bald Eagle p07 viewer with results is 10.0% smaller than tippecanoe's (5.4% larger with IDs); Davis is 2.8% larger (12.3% with IDs). The sparse low-zoom `geometry.pmtiles` stays larger than tippecanoe's (+34% Bald Eagle, +9.5% Davis).
- **`ras2cng map` uses fast-map by default** for pure 2D models (FIM library scope), product group by product group, and falls back to RAS Mapper for everything it is not qualified for (1D elements, non-Max profiles, shear stress, unqualified render modes). `--engine rasmapper` or `RAS2CNG_MAP_ENGINE=rasmapper` restores RAS Mapper for every product.
- **Project metadata parquet:** archives built with ras-commander 0.100+ wrote `<Project>.parquet` as a 0-byte file; dict columns are now written as Parquet structs, and variable-key or all-empty ones as `MAP<string, string>`.

## Mapping (fast-map 0.12 through ras-commander 0.113)

Every product below was compared with native HEC-RAS 7.0.1 stored maps: 0 mask and 0 float32 bit differences on the named models.

- Max WSE and Depth in horizontal, sloping and slopingPretty; Max velocity in sloping and horizontal (slopingPretty opt-in with `--fast-map-velocity`); Max D x V, D x V^2 and Froude in sloping and horizontal (`--no-fast-map-derived` to opt out); arrival time, percent time inundated and duration (Bald Eagle bridges p05: 18 of 18 products identical over sloping and horizontal).
- **Storage areas are opt-in:** `--fast-map-storage-areas` (`fast_map_storage_areas=True`, `RAS2CNG_FAST_MAP_STORAGE_AREAS=1`) draws them as RAS Mapper level pools in WSE/Depth, arrival time and percent inundated (identical on Bald Eagle p07 and p04). By default a plan with storage areas goes to RAS Mapper, because one plan is still slower in Rust (p07 WSE + Depth 18.7 s against 12.2 s); opt in to map such plans without HEC-RAS (Linux, FIM batches).
- Terrain without the HEC-RAS export: the priority-1 source TIFF, its CRS-canonical copy, then RAS Mapper's effective terrain rebuilt in Rust (Bald Eagle Terrain50: identical mask on 197 M px, 8 values differ in stitch-TIN corners; 7 s against about 190 s), then the export. Terrains with modifications always use RAS Mapper's export, and their boundaries RAS Mapper.
- Rust inundation boundaries (deterministic contours, close to but not identical with RAS Mapper's polygons). New `--fast-map-terrain-crs-hash` (`fast_map_terrain_crs_fnv1a64`): a user-verified engine WKT hash for a terrain whose CRS the engine cannot name as an EPSG code (Davis). ras2cng never guesses it; the refusal message prints the value `ras2fim-batch terrain-info` reports.
- `MapResult.fast_map_skipped` records why a map type went to RAS Mapper; `MapResult.invalid_cells` and `--fast-map-invalid-cells` record and set pixel ownership for cells whose face polylines cross; sloping WSE/Depth are written by the engine (`--fast-map-writer`).

## Archive, GeoParquet, tiles, COG

- `ras2cng archive` writes geometry and `--results` through terrio's direct HEC-RAS path with the terrio fork build, as manifest 3.0, with the Python path as per-file fallback (`RAS2CNG_ARCHIVE_ENGINE`, `--archive-engine`). Bald Eagle p07 with results: 29.1 s to 12.5 s, peak 4.0 GB to 0.76 GB. Each file's `archive` record says what was native, what came from Python and what was refused.
- terrio's `write_geoparquet_layers` is the default archive GeoParquet writer when installed (`RAS2CNG_GEOPARQUET_WRITER`), with identical rows, order, schema and row groups to the pyarrow writer, which stays the fallback.
- Vector tiles: a freestiler build with Arrow input (the fork build) skips the NDGeoJSON step: Bald Eagle p07 viewer with vector results 36 s against 1,745 s for tippecanoe, with identical decoded tiles to the GeoJSON route. `auto` prefers it.
- COGs: with the terrio fork build the area-ratio report is written in the same pass as the overviews, so the pre-scan is gone (Terrain50 13-14 s to 6-7 s, byte-identical output).
- Results without geometry come from ras-commander's value table (`include_geometry=False`) instead of a separate h5py reader; identical on all 202 readable summary variables of the 28 public 2D plans in the HEC-RAS test corpus.
- New `ras2cng.rate_stack.write_rate_stack`: precipitation-indexed NetCDF raster stacks with exact compressed-chunk byte ranges (`docs/rate-stack.md`).

## Fixes

- `ras2cng map` with a relative output directory wrote RAS Mapper's rasters under the project folder and left the requested folder empty; the directory is now resolved first.

## Upgrade notes

- `ras2cng mannings` output differs from 0.12 (see above). Pass `--method estimate` to reproduce an older raster.
- Pass `--feature-ids` where a consumer needs MVT feature IDs.
- Plans with storage areas are mapped by RAS Mapper unless you pass `--fast-map-storage-areas`.

# ras2cng 0.12.0

## Highlights

- Vector tiles never drop features. `--drop-densest-as-needed` rendered thinned mesh layers as visible stripes (17-54% of cells drawn at z10-z11) and is removed, together with `RAS2CNG_TIPPECANOE_BOUNDED`.
- Tile weight is bounded by each tileset's minimum zoom instead. Mesh cells and faces start at the detail floor: z13, raised for fine meshes to the first zoom where the median cell spans two screen pixels.
- Raw results joined to mesh cells or faces move to a new `results-detail.pmtiles` tileset that shares the detail floor. Results joined to sparse features (cross sections, reference lines and points) stay in `results.pmtiles` at every zoom.
- New `RAS2CNG_VECTOR_TILER` (`auto`, `tippecanoe`, `freestiler`) for every vector PMTiles writer. `auto` uses tippecanoe when its CLIs are on PATH and falls back to the in-process freestiler tiler, so Windows hosts without tippecanoe can build vector tiles. Install it with `pip install "ras2cng[freestiler]"` (also part of `all` on platforms with prebuilt wheels).

## Upgrade note

Viewer manifests built with `--vector-results` now list mesh-joined results under a `results-detail` tileset with `minzoom`, and those layers carry `style.minzoom`. `PackageSummary` gains `result_detail_pmtiles` and `detail_min_zoom`; `result_pmtiles` is `None` when every result joins to mesh elements. Use the published stored-map rasters for overview zooms.

freestiler archives are about 40-50% larger than tippecanoe's and hold every layer of a tileset in memory; tippecanoe remains preferred where it is installed. Evidence: `feature_dev_notes/vector_tiler_evaluation_2026-10-12.md`.

# ras2cng 0.11.0

## Highlights

- Selects named steady profiles exactly through a private ras-commander adapter instead of allowing an unresolved name to fall back silently to `Max`.
- Validates missing and duplicate steady profile names before stored-map generation changes project state.
- Validates unsteady timestamps exactly and preserves explicit `Max` and `Min` summary selections.
- Preserves boundary-only steady-profile exports and separates whole-simulation products from profile-specific map generation.
- Requires `ras-commander[full]>=0.100.0` for the exact steady-profile contract.

## Upgrade note

Calls that previously supplied an unknown or ambiguous named profile may now raise `ValueError`. This is intentional: ras2cng no longer substitutes a different result surface for the requested profile. Existing explicit `Max`, `Min`, and valid timestamp workflows remain supported.
