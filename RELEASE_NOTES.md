# ras2cng (unreleased)

## Modified terrains can use the native effective terrain (`rasmapper-effective-terrain/2`)

Until now every terrain with modifications went to RAS Mapper's terrain export (HEC-RAS),
whatever the engine advertised (user decision 2026-10-30). That decision is superseded: the
project engineer confirmed the reversal on 2026-11-11, now that it is implemented.
`ras2cng map` with the fast-map engine now builds such a terrain
natively, with the modifications applied, **only when all of these hold**
(`fast_map.TerrainResolver.modified_route`):

- the installed engine advertises `rasmapper-effective-terrain/2` with a `modifications` record
  (Fast-Map 0.18.0 and later);
- ras-commander's terrain-modification reader stages every modification layer without
  refusing one: the public `RasFim.read_terrain_modifications` (ras-commander 0.119.0 and
  later), else the private `ras_commander.fim._terrain_mods.read_layers` on a ras-commander
  that has only that. The reader used is recorded as `modification_reader`;
- every staged layer kind, ground-line subtype and elevation type is in the engine's qualified
  list, and none is a form the engine refuses;
- the existing native-terrain conditions hold (effective terrain enabled and supported, layers
  stored in the terrain HDF, HEC-RAS 6.6 rules only with an engine that advertises them).

Otherwise the RAS Mapper export builds the terrain exactly as before. Either way the decision and
its reason are recorded in `MapResult.fast_map_terrain_route` and in
`_fast_map_terrain/fast_map_terrain_route.json`; an engine refusal on the native route falls back
to the export and is recorded as `fallback`. Inundation boundaries on modified terrains stay on
RAS Mapper.

- **No change with Fast-Map 0.17.0 or ras-commander 0.117/0.118:** 0.17.0 advertises only
  `rasmapper-effective-terrain/1`, so every modified terrain still goes to RAS Mapper (reason
  recorded).
- **Schema 1 results change with `/2`.** Fast-Map runs one rule, `rasmapper-effective-terrain/2`,
  for both request schemas, so the native effective terrain of an *unmodified* terrain (schema
  1) from a `/2` engine differs numerically from a `/1` engine's; receipts report `/2`. Products
  mapped on the effective terrain can change when the engine is upgraded.
- **Not yet qualified through ras2cng:** modified terrains via the engine must still be checked
  against repeat HEC-RAS exports before the result is relied on.

# ras2cng 0.16.0 (2026-11-09)

## Fix: `ras2cng map --wgs84` did not reproject

**`--wgs84` (`reproject_wgs84=True`) has returned rasters in the model CRS**, with a
"rasterio not available for reprojection" warning: the function imported a rasterio name from
the wrong module and caught the `ImportError`. The only test mocked the function. It now
reprojects, and a raster that cannot be reprojected is an error instead of a pass-through.
**Rasters published with `--wgs84` from an earlier version are in the model CRS: check them and
re-run.** With the CLB terrio build the reprojection runs in terrio (Bald Eagle Terrain50: 7.8 s
against 30.3 s; same grid, equal masks, values equal to rasterio's to float rounding); otherwise
in rasterio. `RAS2CNG_REPROJECT_ENGINE=auto|terrio|rasterio`.

## Fix: the tippecanoe path thinned point layers

`ras2cng maplibre` and `ras2cng pmtiles` now pass `--drop-rate=1` to tippecanoe. Its default
rate drops points below the maximum zoom whatever the size limits say: the Davis pipe network
lost 2,243 node points over z0-z16. **Rebuild tippecanoe-built tilesets that have point
layers.** freestiler tilesets were always complete.

## Engines

- **Requires ras-commander 0.117.0** (`ras-commander[full]>=0.117.0`, also in the `fast-hdf`
  and `fast-map` extras; was 0.116.0 in development and 0.113.0 in 0.15.0). A plain install
  resolves **ras-commander-fast-hdf 0.5.0** and **ras-commander-fast-map 0.16.0**.
- **Now in Rust by default with a plain PyPI install** (engine features are still detected at
  run time, never from a version number; an older engine keeps its previous behaviour):
  - Max shear stress in plain sloping and horizontal (fast-map 0.16.0
    `rasmapper-face-shear-max/1`, ras-commander 0.117.0);
  - slopingPretty Max velocity, D x V, D x V² and Froude, with the `.rasmap`'s
    shallow-reduction and depth-weighting flags passed unchanged (fast-map 0.16.0
    `velocity_with_faces`, ras-commander 0.117.0);
  - HDF reads of 2D summaries that store no `Location` (HEC-RAS 5.0.x / 6.1 plan files) in
    ras-commander, through fast-hdf 0.5.0, where 0.3.0 fell back to h5py.
- **Optional fork builds**, published only as GitHub Release assets, not on PyPI. Without them
  ras2cng keeps its Python paths; with them:
  - **freestiler 0.3.0.4** adds `deduplicate`, `sort_values` and per-zoom line simplification
    for vector tiles (below), on top of Arrow input, per-zoom polygon simplification and short
    lines from 0.3.0.3:
    `pip install freestiler==0.3.0.4 --find-links https://github.com/gpt-cmdr/freestiler/releases/expanded_assets/v0.3.0.4-gpt-cmdr`
  - **terrio 0.5.0.4** (CLB build) is built against fast-hdf 0.5.0, so older plan files take
    the direct archive path (below), and it reports rows it leaves out of a layer
    (`archive.omitted`). It links two HDF5 readers: hdf5-reader 0.12.1 inside the HEC-RAS
    engine, 0.10.1 for terrio's own NetCDF/HDF5 code.
    `pip install terrio==0.5.0.4 --find-links https://github.com/CLB-Engineering-Corporation/terrio-clb/releases/expanded_assets/gpt-cmdr-v0.5.0.4`
  - In a uv project, install them **after** `uv sync` and run with `uv run --no-sync`. A later
    `uv sync`, even with `--inexact`, puts the locked PyPI builds back; earlier notes said
    otherwise.
- **What stays on RAS Mapper:** 1D and mixed models (fast-map is built for FIM library creation
  on pure 2D models, not for general use across all model types), storage-area plans unless
  `--fast-map-storage-areas` is given (and even then velocity, derived Max, shear stress and
  duration), non-Max profiles, shear stress in slopingPretty, terrains with modifications, and
  any product the installed engine does not advertise. Each falls back per product group,
  with the reason in `MapResult.fast_map_skipped`.
- **What stays on Python:** without the CLB terrio build, archives use the Python path
  (manifest 2.7); with it, every geometry layer other than `mesh_cells` and `mesh_faces`, every
  text layer, and summary variables the engine refuses still come from Python. Without the
  freestiler fork build, vector tiles go through NDGeoJSON (or tippecanoe).

## Mapping

- **Max shear stress in Rust** (`Shear Stress (Max).tif`, `fast_map_shear_manifest.json`), as its
  own engine call like velocity, in plain sloping and horizontal. Against RAS Mapper 7.0.1
  through `ras2cng map`: 0 mask and 0 bit differences on Bald Eagle bridges p05 (973,902 px
  sloping, 973,532 px horizontal) and Davis p02 (22,041 / 20,560 px). bridges: 4.7 s against RAS
  Mapper's 84.8 s. slopingPretty and storage-area plans stay on RAS Mapper.
  `--no-fast-map-shear` (`fast_map_shear=False`) keeps it on RAS Mapper.
- **slopingPretty velocity and derived Max in Rust by default** (fast-map 0.16.0 advertises it):
  0 mask and 0 bit differences on bridges p05 and Davis p02 for plain, shallow reduction,
  depth-weighted and both (48 product comparisons). The earlier 75 + 28 px difference on bridges
  came from comparing a plain run with a reference made with shallow reduction on.
- **Davis-like terrains in slopingPretty no longer go wholly to RAS Mapper.** When the engine
  refuses the CRS identity of the RAS Mapper terrain export, fast-map now maps on the export's
  CRS-canonical copy (the same pixels with EPSG GeoTIFF keys); `MapResult.fast_map_terrain`
  records `export-canonical`.
- **Inundation boundaries: `--fast-map-boundary-method native-serial`** (opt-in;
  `fast_map_boundary_method`, `RAS2CNG_FAST_MAP_BOUNDARY_METHOD`). The default stays the
  deterministic contour method, which is close to, not identical with, RAS Mapper's polygons.
  The native-serial method is labelled unqualified by the engine; it is exact against RAS
  Mapper's **serial** boundary export on the fixtures named in ras-commander's mapping guide and
  nowhere else. Through `ras2cng map` against RAS Mapper 7.0.1: Davis p02, bridges p05 (two
  terrain sources) and Bald Eagle p07 (two sources, a storage area) each have equal geometry
  and equal unrounded coordinates. It accepts multi-source terrains and, with
  `--fast-map-storage-areas`, storage areas, which the deterministic method refuses.
- The reasons the cheaper terrains were passed over are now printed also when every terrain
  candidate is refused.

## Vector tiles (with freestiler 0.3.0.4)

- **Per-zoom line simplification** (`line_simplify=1.0` tile pixels) on the lines of every layer
  except mesh faces; **`deduplicate`** on every tileset; **`sort_values`** on tilesets without
  mesh layers (it made mesh tilesets 3-6% larger). All lossless or within 1.6 tile pixels, and
  no feature is dropped: every breakline is still at every zoom.
  `geometry.pmtiles` against tippecanoe: Bald Eagle p07 966,399 vs 972,440 bytes (-0.6%), Davis
  343,964 vs 350,842 (-2.0%), UPGU3 7,248,146 vs 7,048,586 (+2.8%).
  New: `--tile-line-simplify PX`, `--no-tile-line-simplify`, `--compact-tiles/--no-compact-tiles`,
  `RAS2CNG_TILE_LINE_SIMPLIFY`, `RAS2CNG_VECTOR_TILE_COMPACT`, `RAS2CNG_VECTOR_TILE_SORT_VALUES`;
  manifest `vectorSimplification.lines`, `vectorArchive`, per-layer `simplification.tileLinePixels`.
  `sort_values` changes archive bytes (tiles decode the same).
- **`Terrain_Profiles` is no longer delivered in tiles** (every backend). The property was the
  text of a NumPy array on every pipe conduit and 70% of the Davis `geometry.pmtiles`
  (1,129,401 to 343,964 bytes). It stays in the archive GeoParquet. A consumer that reads it
  from tiles must read the archive instead.

## Archive

- **Older plan files on the direct path** (with terrio 0.5.0.4, which links fast-hdf 0.5.0):
  Spring p01 has 5 native summary variables and none filled from Python (3 were with 0.5.0.3);
  Spring_BLE p02 takes the direct path with 8 (it went wholly to Python). Every layer, row and
  non-null column still equals the Python path. `archive --results`, median of three, against
  terrio 0.5.0.3: Spring 20.4 s / 2,257 MB to 13.7 s / 1,367 MB; Spring_BLE 25.4 s / 3,354 MB to
  13.3 s / 1,441 MB. `Cell Last Iteration` on 7.x plans is read by the engine with the same type as the
  Python path.
- Manifest `archive.omitted` (terrio 0.5.0.4): rows the engine left out of a layer it wrote. Empty on every model checked, because ras2cng asks the engine only for mesh layers and summary variables; recorded so such a row is never silent.

## Terrain

- `RAS2CNG_TERRAIN_MERGE_ENGINE=terrio` (opt-in, off by default) merges terrain sources with one
  whole-output terrio warp. It is bit-identical to GDAL's whole-output warp and **changes
  today's output by up to 4.4 ft**, because today's blockwise merge is the one that differs
  from GDAL. The terrain provenance records `merge_engine`.

## Earlier in this cycle

- **terrio fork build 0.5.0.3** replaced 0.5.0.2 as the recommended CLB terrio build (now
  superseded by 0.5.0.4, above):
  `pip install terrio==0.5.0.3 --find-links https://github.com/CLB-Engineering-Corporation/terrio-clb/releases/expanded_assets/gpt-cmdr-v0.5.0.3`.
  GeoTIFF/COG ZSTD and LERC_ZSTD still use the zstd C library (COGs byte-identical to 0.5.0.2's);
  GeoParquet, Zarr and EPT Zstandard are pure Rust. Archive GeoParquet written by the native writer
  has the same rows, order, schema, row groups and metadata as before; on Bald Eagle p07 the files
  are 0.15-1.63% larger (0.36% for the whole archive).
  No ras2cng code change is needed; 0.5.0.2 keeps working.
- Viewer manifests: a terrain modification's `provenance.sourceHdf` is the file name only, also
  when a Windows-written archive is packaged on Linux (it was the whole Windows path there).
- CI: a pull-request test workflow (locked dependencies, locked plus the fork wheels, and a fresh
  unlocked install, on Ubuntu and Windows).

# ras2cng 0.15.0

## Rebuild any published multi-mesh viewer

**Earlier versions gave features in multi-mesh viewers values from the other 2D flow area,** because cell and face IDs restart in every area and `ras2cng maplibre --vector-results` joined on the ID alone. On Davis p02, max WSE was wrong by up to 9.43 ft. Which value a viewer showed depended on draw order.

**Rebuild the tiles of any published viewer of a model with more than one 2D flow area. Archives were always correct and need no rebuild;** run `ras2cng maplibre` again on the archive you have. Single-mesh viewers are not affected by this. Details below.

## 0.14.0 was never published to PyPI

0.14.0 was tagged on GitHub but never uploaded to PyPI, and it has this join bug, so it will not be. **0.15.0 includes all of its changes.** If you are coming from PyPI 0.13.0, these 0.14.0 changes are new to you as well (full text in the 0.14.0 section below):

- **1 ft tile filter by default.** `ras2cng maplibre` and `maplibre-stored-vector` simplify the polygons of every layer except mesh cells and faces at 1 ft before tiling; 0.13.0 did not simplify. Tile polygons can differ from RAS Mapper's outline by up to 1 ft. `--simplify-tolerance 0.1` gives RAS Mapper's filter, `--no-simplify` none.
- **Per-zoom polygon simplification** with freestiler 0.3.0.2 or later and Arrow input: non-mesh polygons at lower zooms no longer carry every vertex. Never applied to mesh cells or faces. `--no-tile-polygon-simplify` turns it off.
- **Ghost rows are gone from results without geometry.** `archive --results-geometry none` and `extract_results_variable_no_geometry` no longer publish placeholder ghost cells, so row counts drop (Bald Eagle p07: 89,879 to 87,039 per cell variable; every removed row held 0.0).
- **Relative manifest paths.** Spatial-index error paths in `manifest.json` are relative to the archive, not absolute local paths.
- **`netcdf` extra** (`netCDF4>=1.6.5`, included in `all`), which `ras2cng.rate_stack.write_rate_stack` needs.
- **`ras2cng audit-overviews`**, which checks whether a raster's stored overviews preserve wet extent.
- Vector results now work on models with reference lines; `build_raster_asset_catalog` skips hidden directories; `archive --simplify-tolerance` is an opt-in filter for archives.

## Fork wheels are optional

A plain PyPI install keeps the Python paths and gets **none of the fork speedups**. Every fork feature is detected at run time, and the multi-mesh fix and the removed properties below apply either way. These need the fork builds, which are published only as GitHub Release assets, not on PyPI:

- **freestiler 0.3.0.3:** Arrow input, per-zoom polygon simplification, short lines kept, and the lower-memory result join.
  `pip install freestiler==0.3.0.3 --find-links https://github.com/gpt-cmdr/freestiler/releases/expanded_assets/v0.3.0.3-gpt-cmdr`
- **terrio 0.5.0.2:** direct archive path, native GeoParquet writer, COG report, streaming tiles.
  `pip install terrio==0.5.0.2 --find-links https://github.com/CLB-Engineering-Corporation/terrio-clb/releases/expanded_assets/gpt-cmdr-v0.5.0.2`

The timings in these notes (for example UPGU3 geometry tiles at 14.9 s) are with the fork builds. Without per-zoom simplification, the same run measured about 150 s with the 1 ft filter alone.

## Tile content changes

**Against 0.14.0, viewers with vector results and viewers with short lines change content in three ways, and in no other.** Decoded per tile, layer and feature against 0.14.0-equivalent output on UPGU3, UPGU4, Spring, Bald Eagle p07 and Davis (0 failures): every other feature is unchanged and in the same order, and every other tileset is byte-identical.

1. **Three properties are gone from result tiles:** `mesh_name_result`, `layer_result`, `hilbert_index_result`. They were a copy of `mesh_name`, the variable the layer already is, and a sort key, left by the join's name suffixes. Nothing in the viewer or in the repositories under the shared workspace names them. A consumer outside those that reads one must read `mesh_name` instead, or use the layer's `rawResult.variable`.
2. **Multi-mesh models: mesh results now attach to the right mesh** (below). Features are removed from Davis's tiles.
3. **Short mesh faces and breaklines are now in the tiles at every zoom,** with freestiler 0.3.0.3 (below). Features are added.

## Mesh results were misattributed on multi-mesh models

`ras2cng maplibre --vector-results` joined each mesh result on the cell or face ID alone, but IDs restart in every 2D flow area. **Every viewer of a model with more than one 2D flow area was affected, since vector results first shipped.** Each ID present in two meshes gave four features, two of them carrying the other mesh's value, drawn over the right ones. The archive itself was right; only the tiles were wrong.

Of the ten plans on hand only Davis p02 has two meshes. Its 11-cell `DS Channel` and the first 11 cells of `area2`, and 35 faces of each:

| Variable | Features before / after | Features with the other mesh's value | That value was off by (median / max) |
|---|---:|---:|---:|
| `maximum_water_surface` | 2,738 / 2,716 | 22 | 6.94 / 9.43 ft |
| `minimum_water_surface` | 2,738 / 2,716 | 22 | 9.37 / 11.10 ft |
| `maximum_face_velocity` | 5,660 / 5,590 | 70 (46 differing) | 0.10 / 1.46 ft/s |
| `minimum_face_velocity` | 5,660 / 5,590 | 70 (0 differing) | 0 |
| `cell_last_iteration` | 2,738 / 2,716 | 22 | 119 / 6,935 |
| `cell_maximum_water_surface_error` | 2,738 / 2,716 | 22 | 0.005 / 0.007 ft |
| `cell_cumulative_iteration` | 2,738 / 2,716 | 22 (0 differing) | 0 |

Both joins now use `mesh_name` when both tables have it. Keys that are not unique are refused with an error rather than attributed to a feature, so **a multi-mesh archive whose result files have no `mesh_name` now fails instead of showing wrong values**; none of the archives on hand is like that, including manifest 2.6. A result row with no feature, or a feature with no result row, raises a `RuntimeWarning`. The manifest records `rawResult.joinKeys`. Rebuild any published multi-mesh viewer.

## Short lines at every zoom

With freestiler 0.3.0.3 and Arrow input, `mesh_faces` and `breaklines`, alone or joined to results, pass `keep_short_lines`: a line whose two ends snap to one tile pixel is written once per zoom as a one-unit segment with its properties, where it used to be absent at that zoom. Those layers are now complete at every zoom against the archive on all five models. Lines already written do not change.

- **Other line layers stay as they were** (conduits, BC and reference lines, structures): Davis `geometry.pmtiles` was +18.37% with its conduits on. Widening is one line, `vector_tiles.KEEP_SHORT_LINES_KINDS`.
- **Off switch:** `RAS2CNG_KEEP_SHORT_LINES=0` or `--no-keep-short-lines`. The manifest's `vectorShortLines` records the layers or why none got it. Older freestiler, GeoJSON input and tippecanoe tile as before.
- **Rendered in MapLibre 6.16 (headless Edge, software WebGL), UPGU3 breaklines at z0-z12, width 1 px:** at most 4 pixels differ at any zoom and none is away from existing line work. On this model the stand-ins land on what is already drawn. Other models were not rendered.

## Sizes and peak

Fresh process, median of three, base and new alternating, peak of the process tree. Base is `master` before these changes with freestiler 0.3.0.2, whose tiles are byte-identical to 0.14.0. **Wall times are not comparable:** background CPU reached 100%, and more often during the new runs. Peak is steady to about 1%.

| Model, with vector results | Peak MiB | `results-detail.pmtiles` | `geometry.pmtiles` | `geometry-detail.pmtiles` |
|---|---:|---:|---:|---:|
| UPGU4 | 2,253 -> **1,642** (-27%) | 1,196.3 -> **893.0 MB** (-25.4%) | +2.35% | +220 B |
| UPGU3 | 832 -> **654** (-21%) | 450.8 -> **343.3 MB** (-23.9%) | +0.82% | -4 B |
| Spring | 864 -> **689** (-20%) | 446.2 -> **345.4 MB** (-22.6%) | +0.77% | identical |
| Bald Eagle p07 | 440 -> **366** (-17%) | 78.3 -> **52.4 MB** (-33.0%) | +0.03% | identical |
| Davis | 206 -> 202 | 3.05 -> 2.17 MB (-28.8%) | identical | identical |

## Fixes

- The Arrow result join failed with `ArrowInvalid` when the result rows with no feature were the last rows of the variable.
- A result whose geometry layer does not exist in the archive was skipped without a word; it now raises a `RuntimeWarning`. On Davis that is `one_d_structure_summary`.
- The sdist no longer ships development notes, agent instructions, CI files or tests, which held workstation paths. Code and docs are unchanged. Run tests from a Git checkout.

## Lower memory for viewers with vector results

`ras2cng maplibre --vector-results` read the whole plan file (every variable, with its geometry) through pandas once per variable, and held each mesh layer as shapely objects for the whole run. With freestiler Arrow input, a mesh layer is now written to disk in EPSG:4326 batch by batch, each variable is read with row-group and column pruning and joined on its keys, and the tiler attaches the one geometry column when it reaches the layer.

**Tiles are byte-identical** to the previous output on Davis, Bald Eagle p07, UPGU3 and UPGU4, on manifest 2.6 and Python-path archives of Bald Eagle p07, and through GeoJSON input and PyPI freestiler on Davis; the viewer manifest differs only in `simplification.seconds`. tippecanoe's input files are byte-identical on Davis and Bald Eagle p07 (tippecanoe itself was not run: Docker is down on this host). The pandas join remains the fallback, with its composite-key refusals and reference-line pivot, and now reads only the variable's rows without geometry.

Fresh process, median of three, peak of the process tree, base and new runs alternating on a shared host (21-95% background CPU), so read the times as approximate:

| Model | Peak before | Peak now | Wall before | Wall now |
|---|---:|---:|---:|---:|
| UPGU4 (1,063,548 cells) | 16,283 MiB | **2,265 MiB** | 408.6 s | **235.0 s** |
| UPGU3 (292,676 cells) | 4,064 MiB | **836 MiB** | 131.2 s | **87.1 s** |
| Bald Eagle p07 (87,039 cells) | 1,390 MiB | **438 MiB** | 42.7 s | **15.3 s** |
| Davis (2,716 cells) | 243 MiB | **205 MiB** | 4.7 s | 4.5 s |

The peak is now freestiler's in-memory form of the largest layer (about 1.1 GB for UPGU4's 2.27 million faces).

# ras2cng 0.14.0

**Tagged on GitHub, never published to PyPI.** Use 0.15.0, which includes everything below and fixes the multi-mesh join.

Web-map tiling of models with a huge land-cover polygon goes from minutes to seconds, vector results work on models with reference lines, and several bugs found while reviewing the open branches are fixed. Fork engine features stay capability-detected: with the PyPI builds of terrio and freestiler ras2cng keeps its Python paths.

## Requirements

- Optional fork builds, published only as GitHub Release assets (not on PyPI; the `terrio` and `freestiler` extras keep PyPI-installable floors):
  - freestiler 0.3.0.2: `pip install freestiler==0.3.0.2 --find-links https://github.com/gpt-cmdr/freestiler/releases/expanded_assets/v0.3.0.2-gpt-cmdr`
  - terrio 0.5.0.2: `pip install terrio==0.5.0.2 --find-links https://github.com/CLB-Engineering-Corporation/terrio-clb/releases/expanded_assets/gpt-cmdr-v0.5.0.2`
- **New `netcdf` extra** (`netCDF4>=1.6.5`), included in `all`. `ras2cng.rate_stack.write_rate_stack` needs it; before this release no extra declared it, so it failed after `pip install ras2cng[all]`. The error now names the extra.
- The terrio floor is `>=0.5.0.1`: PyPI has yanked every release up to 0.5.0 ("Superseded by 0.6.0"). It stays below 0.6 so that the CLB builds still satisfy it. A plain install gets PyPI terrio 0.6.x, which has none of the fork features and keeps the Python and GDAL paths (full test suite passes on 0.6.1).

## Behaviour changes

- **Web-map tiles are simplified at 1 ft by default, up from none in 0.13.0.** `ras2cng maplibre` and `maplibre-stored-vector` simplify the polygons of every layer except mesh cells and faces with Douglas-Peucker at 1 ft, converted to the CRS units (0.3048 m in a metre CRS), before tiling. RAS Mapper's own default filter is 0.1 ft: **tile polygons therefore differ from RAS Mapper's outline by up to 1 ft**, about half a CSS pixel at z17, and the UPGU3 and UPGU4 land-cover areas change by about -0.2%. Lines, points, geographic CRSs, layers with no CRS and the mesh are unchanged. `--simplify-tolerance FT`, `--no-simplify`, `RAS2CNG_SIMPLIFY_TOLERANCE` (feet; `0`/`off`), `RAS2CNG_SIMPLIFY_ENGINE` (`auto`/`terrio`/`shapely`). Pass `--simplify-tolerance 0.1` for RAS Mapper's filter.
- **Per-zoom polygon simplification in the tiler.** With freestiler 0.3.0.2 and Arrow input, the same non-mesh polygon layers get `polygon_simplify=0.5` tile pixels: each ring is simplified once per zoom and cut down a tile tree. Tile polygons at lower zooms therefore no longer carry every vertex; the maximum zoom does. 0.5 was chosen over 1.0 for less area bias at middle zooms (-4.8% against -7.3% at z8 on the thin UPGU3 land-cover network, where unsimplified tiles are within 0.05%), for tiles about 7.5% larger. **It is never applied to `mesh_cells` or `mesh_faces`.** `--tile-polygon-simplify PX`, `--no-tile-polygon-simplify`, `RAS2CNG_TILE_POLYGON_SIMPLIFY` (`0`/`off`); `--no-simplify` turns off both. Older freestiler, GeoJSON input and tippecanoe tile as before. Recorded in the viewer manifest: `vectorSimplification` and its `perZoom`, and `simplification` per layer.
- **Ghost cells are gone from results without geometry: row counts change.** `archive --results-geometry none` and `extract_results_variable_no_geometry` used to publish the placeholder ghost cells as results. Bald Eagle p07 goes from 89,879 to 87,039 rows per cell variable; all 2,840 removed rows held 0.0. Consumers counting rows, or joining on them, see fewer. Polygon and point output already excluded them.
- **Spatial-index error paths in `manifest.json` are now relative to the archive** instead of absolute local paths, so a manifest no longer exposes the machine that built it. A consumer that opened those paths directly must resolve them against the archive.
- `build_raster_asset_catalog` no longer picks up manifests under hidden directories, so a partial release in a dot directory stays out of the catalog.
- `ras2cng archive --simplify-tolerance FT` (`archive_project(simplify_tolerance_ft=)`): the filter for the archive GeoParquet, **opt-in and off by default**, because the archive is the data of record; the environment variable does not apply. Recorded in `manifest.geometry[].simplification`.

## Measured

`ras2cng maplibre`, fresh process per run, median of three, same harness (peak is the process tree). The 0.13.0 and 0.1 ft columns were measured on a more loaded host (47-78% background CPU against 9-18% now), so read the same-host rows below for the cause.

| Model | Operation | Before | 0.14.0 | Peak |
|---|---|---:|---:|---:|
| UPGU3 (292,676 cells) | geometry tiles | 1,615 s (0.13.0), 613 s (0.1 ft) | **14.9 s** | 641 MiB |
| UPGU4 (1,063,548 cells) | geometry tiles | 400 s (0.1 ft) | **32.9 s** | 1,556 MiB |
| Spring (340,163 cells) | geometry tiles | 76 s (0.1 ft) | **20.4 s** | 751 MiB |
| UPGU3 | tiles + vector results | failed | **92.6 s** | 4,081 MiB |
| UPGU4 | tiles + vector results, one run | not run | 302 s | 16.3 GB |

On one host at one load, one run each, UPGU3 geometry tiles: 0.1 ft alone 468 s, 1 ft alone 150 s, per-zoom alone 16.1 s, both 14.9 s. **The speed comes from the per-zoom option;** without freestiler 0.3.0.2, 1 ft is what is left. `geometry-detail.pmtiles` (the mesh) is byte-identical to the previous output on all four models, and no polygon layer lost a feature at any zoom.

## Fixes

- **Vector results on models with reference lines failed** (`composite result keys are not unique`). Reference summaries hold one row per feature, variable and statistic, which the join refused. The viewer now pivots them to one feature with `flow_maximum`, `flow_maximum_time`, `flow_units` and so on; every value is kept, a table that would lose one is refused, and the archive keeps the long table. UPGU3: 60 of 60 values on the 15 reference lines.
- A published rate stack was mode 0600 on Linux, readable only by its owner; it now follows the umask.
- `is_ghost_cell` is documented in `ras2cng/schemas.py` (schema version 1.3).
- A terrain test compared a warning's file name as a string, which could fail on a lower-case drive letter.

## New

- `ras2cng audit-overviews` and `ras2cng.cog.audit_overview_extent`: check whether a raster's stored overviews preserve wet extent, on rasters ras2cng did not write too. No new dependency.

## Known limits

- freestiler omits a line whose two ends snap to the same tile pixel at a zoom, so very short mesh faces and breaklines thin out at low zoom: UPGU3 4 of 637,473 faces absent at z13 and none below; UPGU4 2 of 2,273,372 absent even at z17 (faces of 0.10 ft). Not new in this release: the mesh tiles are byte-identical to before. Polygons, mesh cells included, are complete at every zoom.

## Upgrade notes

- Pass `--simplify-tolerance 0.1` for RAS Mapper's filter, `--no-tile-polygon-simplify` for every vertex at every zoom after the filter, or `--no-simplify` for no simplification at all.
- Code reading `--results-geometry none` tables sees fewer rows (ghost cells).
- Code reading spatial-index errors in `manifest.json` sees archive-relative paths.
- `pip install "ras2cng[netcdf]"` for `write_rate_stack`, if you do not use `[all]`.

# ras2cng 0.13.0

Rust engines release. Every engine feature is capability-detected: with older engines, or the PyPI builds of terrio and freestiler, ras2cng keeps its Python paths.

## Requirements

- Requires ras-commander 0.113.0 (`ras-commander[full]>=0.113.0`; was 0.100.0), which brings ras-commander-fast-hdf 0.3.0 and, through `full`, ras-commander-fast-map 0.12.0. ras-commander reads HEC-RAS HDF results through the Rust engine where it proves the layout.
- Optional fork builds, published only as GitHub Release assets (freestiler on the gpt-cmdr fork, terrio on CLB-Engineering-Corporation/terrio-clb; not on PyPI; the `terrio` and `freestiler` extras keep the PyPI floors):
  - freestiler 0.3.0.1: `pip install freestiler==0.3.0.1 --find-links https://github.com/gpt-cmdr/freestiler/releases/expanded_assets/v0.3.0.1-gpt-cmdr`
  - terrio 0.5.0.1: `pip install terrio==0.5.0.1 --find-links https://github.com/CLB-Engineering-Corporation/terrio-clb/releases/expanded_assets/gpt-cmdr-v0.5.0.1`

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

- `ras2cng archive` writes geometry and `--results` through terrio's direct HEC-RAS path with the CLB terrio build 0.5.0.1, as manifest 3.0, with the Python path as per-file fallback (`RAS2CNG_ARCHIVE_ENGINE`, `--archive-engine`). Bald Eagle p07 with results: 29.1 s to 12.5 s, peak 4.0 GB to 0.76 GB. Each file's `archive` record says what was native, what came from Python and what was refused.
- terrio's `write_geoparquet_layers` is the default archive GeoParquet writer when installed (`RAS2CNG_GEOPARQUET_WRITER`), with identical rows, order, schema and row groups to the pyarrow writer, which stays the fallback.
- Vector tiles: a freestiler build with Arrow input (the fork build) skips the NDGeoJSON step: Bald Eagle p07 viewer with vector results 36 s against 1,745 s for tippecanoe, with identical decoded tiles to the GeoJSON route. `auto` prefers it.
- COGs: with the CLB terrio build 0.5.0.1 the area-ratio report is written in the same pass as the overviews, so the pre-scan is gone (Terrain50 13-14 s to 6-7 s, byte-identical output).
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
