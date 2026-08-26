# Rendering Defaults Audit — Implementation Record (2026-08-24)

Companion to `rendering_defaults_audit_2026-08-24.md`. That document is
findings; this one records what was **built**, what was **measured**, and what
was deliberately **not** shipped.

**Repos changed:** `G:\GH\ras2cng` (the canonical 0.7.0 copy) and
`H:\CLB-Repos\hms2cng`. `H:\CLB-Repos\ras2cng` was **not** touched — it is on
`feat/lwi-pipeline-gaps` at 0.6.0 and predates the entire raster surface, so
none of this applies to it as-is. It still needs a refresh from `origin/master`,
and it carries unpushed feature-branch commits, so that refresh is a decision
for its owner rather than a side effect of this work.

## The structural change

Everything lands in one new module, `ras2cng/cog.py`, rather than being patched
per call site. That was the audit's own takeaway: the area-matched overview
method does not exist anywhere downstream, so writing it in `ras2cng` — the
upstream renderer — means RBFS (`clb_tx_webmap`) and the LWI webmap inherit it
instead of each writing their own. The helpers are re-exported from the package
root (`from ras2cng import area_matched_cog, validate_cog, atomic_output, ...`)
specifically so downstream can import them directly.

`cog.py` owns: overview construction, overview depth policy, COG creation
options, CRS authority resolution, WGS84 bounds sanity, COG validation, and
atomic replacement.

## Must-fix items

| # | Status | Where |
|---|--------|-------|
| M1 area-matched overviews | **Shipped** | `cog.py` `build_area_matched_overviews`, used by `mapping.py`, `raster_recipes.py` |
| M2 `OVERVIEWS=IGNORE_EXISTING` | **Shipped** | `cog.py` `cog_creation_options` / `area_matched_cog`; `project.py`, `mapping.py`, `raster_recipes.py` |
| M3 stop guessing EPSG | **Shipped** | `cog.py` `resolve_crs_authority` / `describe_crs`; `maplibre.py`, `project.py::_tif_crs` |
| M4 categorical discriminator | **Shipped** | `cog.py` mode decimator; `mapping.py::CATEGORICAL_MAP_TYPES` |
| M5 `cog_validate` gate | **Shipped** | `cog.py` `validate_cog`; `mapping.py`, `publication.py`; `rio-cogeo` added to extras |
| M6 atomic writes | **Shipped** | `cog.py` `atomic_output`; `maplibre.py` ×2, `project.py` ×2, `pmtiles.py`, hms2cng |
| M7 PMTiles reprojection | **Shipped** | `ras2cng/pmtiles.py`, `hms2cng/pmtiles.py` |
| M8 black nodata fringe | **Shipped** | `maplibre.py` terrain and result ramps |

### M1 — how it is actually built

GDAL does not let a caller supply overview pixels, and the `osgeo` Python
bindings are not a dependency of this repo. The levels are therefore written as
sidecar GeoTIFFs, attached to a VRT wrapper via `<Overview>` elements, and
embedded by the COG driver under `OVERVIEWS=FORCE_USE_EXISTING`. This route was
verified end to end: the base level comes through byte-identical and the custom
overview pixels survive verbatim.

The algorithm is as specified in M1 — per-level coverage fraction, threshold
**solved for** from the coverage histogram rather than hardcoded at 0.5,
coherence rank plus deterministic dither to break ties at the threshold, and the
**mean** of wet contributors (never max) for every cell kept wet.

Measured on a 4097×3001 synthetic depth raster with a ragged margin (1.47 M wet
cells, odd dimensions on purpose to exercise the padding path). All three are
read at each level's native stored size — a floor-division read makes GDAL
resample the level, which biases the wet-cell count:

| factor | `average` wet area | `nearest` wet area | area-matched wet area | `nearest` speckle | area-matched speckle |
|--------|-------------------|--------------------|-----------------------|-------------------|----------------------|
| 2 | 133.6 % | 100.1 % | 100.0 % | 2.92 % | 0.64 % |
| 4 | 144.8 % | 100.3 % | 100.0 % | 2.96 % | 0.49 % |
| 8 | 156.3 % | 100.5 % | 100.0 % | 2.97 % | 0.12 % |
| 16 | — | — | 100.0 % | — | 0.00 % |

`average` reproduces the audit's reported wet-area inflation, in the same
direction and the same order of magnitude, and it degrades monotonically with
zoom. `nearest` holds area only statistically and pays for it in speckled
margins — the audit's other complaint — which the coherence rank removes.

Values track the same way: mean depth over the wet cells is 5.83–6.77 ft under
`average` (it is mixing dry cells into the mean), against 8.76–8.79 ft for
`nearest` and 8.96–9.05 ft for area-matched. Max depth stays at or below the
source under area-matched, which is the point of specifying MEAN — MAX would
have inflated flood volume.

Note also the level counts: `average` produced three levels and area-matched
four, on the same raster. That is I6 — the pyramid now continues until the top
level fits a single block.

Rasters that cannot take the algorithm — no nodata, multi-band, or past
`RAS2CNG_AREA_MATCHED_MAX_PIXELS` — fall back to GDAL resampling and **record
the reason in the raster's tags**. A fallback that is not labelled is
indistinguishable from the fix having worked.

### M2 — the trap, reproduced

Worth stating plainly because it makes M1 and M4 no-ops. A source raster with an
`average` pyramid was copied to COG twice, both times *requesting* `nearest`:

* `OVERVIEWS` unset (the driver's `AUTO` default, i.e. the old code) → wet area
  115.9 % / 127.4 %, and only the source's two levels. The requested `nearest`
  was discarded silently.
* `OVERVIEWS=IGNORE_EXISTING` → wet area 100.0 % across three correctly
  rebuilt levels.

Both cases are pinned as tests.

### M3 — the trap, reproduced

`CRS.to_authority(min_confidence=25)` on a NAD83 Texas Centric Albers definition
in **US survey feet** returns `('EPSG', '3083')` — the **metre** variant. At
PROJ's own default confidence of 70 it correctly returns nothing. The linear
unit is the only difference, and a permissive threshold exists precisely to
ignore that kind of difference.

`resolve_crs_authority` now returns a code only when the registered definition
round-trips the source's **linear unit** and **ellipsoid**, and returns the
verbatim source definition otherwise. `sourceProj4` remains the authoritative
browser-side field; `sourceCrs` is advisory, `sourceCrsAuthority` states whether
a code was actually verified, and `sourceWkt` is now emitted too. Derived WGS84
bounds are gated against ±180/±90 and degeneracy, since a wrong CRS shows up
there first.

## Improvements

| # | Status | Note |
|---|--------|------|
| I1 shared nodes / stderr | **Shipped** | `--no-simplification-of-shared-nodes` always on; tippecanoe stderr logged instead of discarded |
| I1 bounded tiles | **Shipped, per-tileset** | bounded on the dense `geometry-detail` tileset only; overview stays unbounded. `RAS2CNG_TIPPECANOE_BOUNDED=1` forces it everywhere |
| I2 DEFLATE default | **Shipped** | DEFLATE is now the default after benchmarking; `RAS2CNG_COG_COMPRESS=ZSTD` restores the old behaviour |
| I3 predictor | **Measured; predictor 3 confirmed** | predictor 3 beat 2 on every surface tested, including smooth WSE. Expressed as `PREDICTOR=YES`, which resolves to 3 for float and 2 for integer — the literal `3` hard-fails an integer raster. `RAS2CNG_COG_PREDICTOR=2` available |
| I4 predictor dropped in recipe copy | **Shipped** | the final COG copy now carries the predictor |
| I5 LERC | **Shipped, now the DEFAULT (0.9.0)** | `lerc_max_z_error=` on `area_matched_cog` / `cog_creation_options`; refused on categorical and on a non-positive tolerance; tolerance written into the artifact's tags |
| I6 overview depth | **Shipped** | one policy in `cog.overview_factors`, terminating at the block size |
| I7 legacy raster PMTiles | **Shipped** | `generate_raster_pmtiles` now routes through the `maplibre.py` pipeline |
| I8 terrain footprint | **Shipped** | terrain COGs now build through `area_matched_cog`, so the terrain edge stops creeping outward with zoom |
| I9 `except (ImportError, Exception)` | **Shipped** | narrowed and logged |

## hms2cng

hms2cng has no raster, COG, or CRS-guessing surface, so M1–M5 and M8 do not
apply. What did apply was in `hms2cng/pmtiles.py`, and all of it is fixed:

* **M7** — it had the same never-reproject bug, via
  `gdf.to_file(driver="GeoJSON")` rather than `to_json()`. Same outcome: a
  valid, silently wrong tileset. It is arguably worse there, because `crs_epsg`
  is optional, so a CRS-less layer could reach tippecanoe. That is now refused
  with an actionable message rather than tiled.
* **M6** — the `.mbtiles` intermediate was written beside the output *and never
  deleted*, and `pmtiles convert --force` wrote straight over the live tileset.
  Both fixed: scratch directory plus atomic swap.
* **I1** — `--no-simplification-of-shared-nodes` matters more here than in
  ras2cng: HMS subbasins tile the watershed and share every interior boundary.
  Tippecanoe stderr is now logged.

Nine tests added (`tests/test_pmtiles.py`), including one that asserts a Texas
State Plane (ftUS) fixture actually lands in Texas after tiling.

## Defaults changed after benchmarking (I2 / I3 / I1)

Both were initially left alone as publisher decisions, then measured and
changed. The benchmark is 3000x3000 float32, 512 px blocks (36 MB uncompressed
band), `vs best` relative to the smallest in each row group:

| surface | DEFLATE p2 | DEFLATE p3 | ZSTD p2 | ZSTD p3 |
|---------|-----------|-----------|---------|---------|
| Depth (ragged wet/dry) | 6.8 MB (+16.0%) | **6.2 MB (+6.0%)** | 6.6 MB (+12.7%) | 5.8 MB |
| WSE (smooth) | 4.9 MB (+19.2%) | **4.5 MB (+8.4%)** | 4.4 MB (+7.5%) | 4.1 MB |
| Terrain (no nodata) | 33.4 MB (+25.5%) | **27.1 MB (+2.0%)** | 31.1 MB (+16.9%) | 26.6 MB |

Windowed read times — how a tile server actually touches the file — were
0.009–0.012 s across every combination, i.e. indistinguishable.

**1. Compression is now DEFLATE (was ZSTD).** ZSTD-in-TIFF needs a GDAL built
with libzstd, and a reader without it fails hard rather than degrading. The
exposure is client-side and outside our control — older QGIS bundles, a tile
server on a distro GDAL, a client's ArcGIS. The measured cost is 2–8% at the
same predictor, which is cheap insurance for anything that leaves the network.
`RAS2CNG_COG_COMPRESS=ZSTD` restores the old behaviour for internal archives.

**2. Predictor stays 3 — do not copy RBFS's pairing.** RBFS uses DEFLATE +
predictor **2**, which measured 16–25% larger than predictor 3 on every surface
here. The audit's I3 reports that predictor 2 can beat 3 on smooth WSE; that did
not reproduce — predictor 3 won the smooth WSE case by 8%. The synthetic noise
floor may not match real RAS output, so this is "unproven", not "wrong": measure
on real WSE rasters before adopting predictor 2.

Predictor 3 is expressed as **`PREDICTOR=YES`**, not as the literal `3`, and the
distinction is load-bearing:

* `YES` is GDAL's "pick for me": it resolves to **3 for Float32/Float64** — the
  setting that was benchmarked, and what every result, terrain, WSE and recipe
  raster actually gets — and to 2 for integer types.
* The literal `3` is float-only and GDAL **hard-fails** on anything else:
  `PREDICTOR=3 is only supported with Float32 or Float64`. A count or class
  raster reaching the continuous path would abort the conversion rather than
  fall back. That was a live latent bug in the first pass and is now pinned by
  `test_an_integer_raster_falls_back_to_a_legal_predictor`.
* `YES` is a **COG driver** option. The plain **GTiff** driver rejects it
  outright (`PREDICTOR=YES is not supported`), so intermediates written with
  GTiff — the recipe scratch raster — must resolve the numeric form themselves.
  `cog.numeric_predictor(dtype, categorical=...)` is the one place that rule
  lives.

Verified end to end on float32: result maps, terrain, the shared default, and
recipe outputs all land at `PREDICTOR=3 COMPRESSION=DEFLATE LAYOUT=COG`.

**The preference reverses on class data.** Predictor 3 wins on continuous
surfaces and loses badly on categorical ones, measured on a 2000x2000 class
raster:

| storage | predictor 2 | predictor 3 |
|---------|-------------|-------------|
| float32 class | **0.195 MB** | 0.475 MB (2.4x larger) |
| int32 class | **0.188 MB** | rejected by GDAL |

Class values are a handful of repeated magnitudes, which horizontal differencing
collapses and the floating-point predictor's byte shuffle does not. Predictor 3
is *legal* on a float32 class raster, just wrong — so `categorical` keeps
predictor 2 whatever the storage type, and
`test_class_data_keeps_predictor_2_even_when_stored_as_float` pins that against a
well-meaning future "make it consistent" change.

Net: the selection is optimal in all three cases — continuous float gets 3,
categorical gets 2, continuous integer gets 2 because 3 is illegal.

**3. Tile size policy is per-tileset, not global.** `maplibre.py` already routes
`mesh_cells`/`mesh_faces` into a separate `geometry-detail.pmtiles` built at
**z13+** (`_is_detail_geometry`), while the sparse engineering layers go to the
overview tileset. That existing split is the per-layer structure the audit's I1
asked for, so the policy is applied there rather than globally:

* `geometry-detail.pmtiles` → **bounded** (`--drop-densest-as-needed`). At z13 a
  typical RAS cell is roughly half a pixel, so unbounded tiles pay multi-megabyte
  transfers for geometry no one can resolve.
* `geometry.pmtiles` (overview) → **unbounded**. Cross sections, structures, BC
  and reference lines are sparse; the ceiling would never trigger and those
  layers must be complete at every zoom.

`--extend-zooms-if-still-dropping` was dead code under the unbounded policy —
it only acts when features drop. Paired with `--drop-densest-as-needed` it is now
load-bearing: features are not discarded, they move to the zoom where they are
legible. `--drop-fraction-as-needed` is still never used; RBFS found it makes
tippecanoe retry the sparsest features until the native binary crashes on mixed
dense layers.

The asymmetry is pinned by `test_only_the_dense_detail_tileset_bounds_tile_size`
and `test_bounded_tiles_swap_the_size_policy_flags`.

**Not measured:** tippecanoe is not installed on this workstation, so the tile
argument is mechanism plus the audit's cited RBFS observation, not local numbers.

### Residual risk accepted

At z13 on the detail tileset, some mesh cells may be absent. Someone eyeballing
"is my whole mesh there?" at that zoom could see gaps that are not in the model.
The model extent polygon lives in the overview tileset and still tells the
coverage story, and anything computing on the mesh should be reading the
GeoParquet rather than a vector tile.

### `results.pmtiles` — now bounded too

Raised as an open question in the previous pass, since resolved. `result_sources`
(`maplibre.py:2051`) holds result values joined to geometry; when the join is on
`mesh_cells` that is one polygon per cell — as dense as the detail tileset, but
spanning the **full** `min_zoom..max_zoom` range rather than starting at z13.

The deciding argument is that `--drop-densest-as-needed` is a *ceiling, not a
mandate*: on a sparse join (reference lines or points) it never fires, so it
costs nothing where density is not a problem. That also explains why the
overview tileset stays unbounded — not because the ceiling would fire, but
because if it ever did, on a pathological model with hundreds of thousands of
cross sections, it would drop engineering features that must be complete.

## I5 — LERC, opt-in and self-documenting

Measured on a 2400x2400 smooth WSE surface at 50% valid coverage, against
lossless DEFLATE + predictor 3:

| compression | MAX_Z_ERROR | size | vs lossless | max abs error |
|-------------|-------------|------|-------------|---------------|
| DEFLATE p3 | — | 7.22 MB | — | 0 |
| LERC_DEFLATE | 0.001 ft | 2.98 MB | **-58.7%** | 0.0010 |
| LERC_DEFLATE | 0.01 ft | 1.14 MB | **-84.2%** | 0.0100 |
| LERC_DEFLATE | 0.1 ft | 0.25 MB | -96.5% | 0.1000 |
| LERC_ZSTD | 0.01 ft | 1.12 MB | -84.5% | 0.0100 |

The tolerance is honoured exactly (float32 representation adds ~2e-6 of slack)
and the nodata mask survives untouched — important, because the wet/dry edge is
a hard boundary that a lossy codec must not blur.

That is a larger saving than the 0.40x the audit cites from RBFS. **Treat these
numbers as optimistic**: the benchmark surface is smoother than real WSE output,
and LERC rewards smoothness. Benchmark on real rasters before committing a
product to a tolerance.

It is opt-in and will stay that way. Three guardrails:

* Refused on a **categorical** raster — a class value "within tolerance" is
  simply a different class.
* Refused on a **non-positive tolerance**, which would otherwise read as
  "lossless" while silently selecting a lossy codec.
* The tolerance is written into the artifact's tags (`compression_lossy`,
  `compression_max_z_error`) as well as `IMAGE_STRUCTURE`, so the loss travels
  with the file rather than living in a build log.

Never set it on a raster whose values feed a downstream calculation.

## I8 — terrain keeps its footprint

Terrain is genuinely continuous, so this was deferred in the first pass on the
grounds that M2 stopped the intermediate pyramid from reaching the published
COG. That was only half the problem: the published terrain COG was still built
with `OVERVIEW_RESAMPLING=AVERAGE`, and terrain still carries a nodata
footprint, so the terrain edge crept outward one coarse cell per level.

Measured on an irregular footprint with an interior hole (the shape a tiled,
clipped HEC-RAS terrain actually has):

| factor | `average` | area-matched |
|--------|-----------|--------------|
| 2 | 100.2 % | 100.0 % |
| 4 | 100.5 % | 100.0 % |
| 8 | 101.1 % | 100.0 % |

Far milder than the flood case (133–156%), exactly as the audit predicted —
a terrain footprint is a large blob with a small perimeter-to-area ratio, while
a flood margin is ragged. But the machinery already existed, so the fix was
free.

Both terrain COG sites in `project.py` now call `area_matched_cog` with
`TERRAIN_CREATION_OPTIONS` (`LEVEL=9`, `SPARSE_OK=YES`). Two side effects worth
knowing:

* The archive no longer shells out to `gdal_translate` for terrain, so it no
  longer depends on a system GDAL new enough to have the COG driver — the same
  argument `mapping.py` already made for result rasters.
* Terrains past `RAS2CNG_AREA_MATCHED_MAX_PIXELS` fall back to GDAL resampling
  automatically, with the reason tagged on the artifact.

`_terrain_cog_creation_options()` is kept as the CLI rendering of the same
policy for callers that do shell out.

## Verification

* ras2cng: 351 passed, 3 skipped. `tests/test_cog.py` is new (24 tests) and
  pins both traps (M2 reuse, M3 EPSG:3083) as executable evidence rather than
  prose.
* hms2cng: 23 passed, 14 skipped (integration tests need real HMS data).
* `tests/test_webgis_service.py` was not run — `fastapi` is not installed in the
  local venv. That gap pre-dates this work and is unrelated to it.
* No pipeline was run against real HEC-RAS output. The measurements above are
  from synthetic rasters built to the failure's shape. Before a fleet-wide
  rebuild, verify on a canary tile, per the RBFS process note.


---

# 0.9.0 — LERC becomes the default (2026-08-26)

Shipped in 0.8.0 as opt-in on synthetic numbers. Re-measured on **full
production rasters** from the LWI Region 6 catalogue and promoted to the
default, because these artifacts exist to be served.

| full production raster | lossless | LERC @ 0.01 | saving |
|---|---|---|---|
| `wse_100yr` (752 MB as published) | 313 MB | 35 MB | **−95.4%** |
| `depth_100yr` (1068 MB as published) | 656 MB | 289 MB | **−72.9%** |

Across the measured ~200 GB published catalogue that is roughly 164 GB against
93 GB for the best lossless setting.

## Why 0.01 is defensible for mapping

The tolerance is in the raster's own vertical units. For HEC-RAS output in US
survey feet, 0.01 ft is about an eighth of an inch — one to two orders of
magnitude below the accuracy of the terrain, roughness and boundary conditions
the value was computed from. For WSE, depth and velocity *mapping* it is inside
the model's own noise floor.

## What the default deliberately does not touch

* **Integer rasters** — a sub-unit tolerance on a count buys nothing.
* **Categorical rasters** — a class "within tolerance" is a different class.
* **The nodata mask** — the wet/dry edge survives byte for byte; only values
  inside the valid area move.

`resolve_compression` distinguishes a default arriving on its own from a caller
explicitly asking. On class or integer data the default steps aside quietly; an
explicit request is refused, because that one is a mistake.

## The consequence that must be stated

Under the lossy default the base level is **no longer byte-identical** to the
source — it is bounded, not exact. Anything relying on exact equality has to ask
for lossless. `RAS2CNG_LERC_MAX_Z_ERROR=off` (or `lerc_max_z_error=None`) does
that in one step, falling back to DEFLATE + predictor 3. A malformed value
raises rather than silently publishing lossy rasters.

Documented for users in `docs/user-guide/raster-delivery.md`.

## I3 predictor — the RBFS claim is now refuted, not merely unproven

0.8.0 recorded predictor 2 as "unproven, not wrong" because the counter-evidence
was synthetic. Re-measured on the same production rasters:

| real raster | DEFLATE p2 | DEFLATE p3 | result |
|---|---|---|---|
| `wse_100yr` (range 5.193–5.245 ft, maximally smooth) | 9.73 MB | **9.28 MB** | predictor 3 by 4.8% |
| `depth_100yr` | 30.32 MB | **26.25 MB** | predictor 3 by 15.5% |

The WSE window is a near-flat water surface — precisely the case the claim named
as predictor 2's win. Predictor 3 still won. The claim does not reproduce on
synthetic or real data, and its provenance traces to a consolidated brief that
is not checked into any repository.

## Independent production validation of the overview fix

The LWI fleet rebuilt its catalogue on 2026-08-25 with its own implementation of
the same algorithm, measuring "before" out of a pre-rebuild snapshot:

| raster | wet area | mean error |
|---|---|---|
| `middle_red_coushatta/wse_100yr` | 169.8% → **100.0%** | 0.415 → **0.011** ft |
| `saline_bayou/wse_10yr` | **224.0%** → **100.0%** | 0.290 → **0.015** ft |
| `mckinney_posten_bayous/wse_5yr` | 176.4% → **100.0%** | 3.452 → **0.072** ft |

224% inflation on real Louisiana models is worse than the 133–156% measured
here synthetically, and sits inside the 113–326% band the original audit cited.
Worst single-cell error found anywhere: 85.29 ft.
