# Rendering Defaults Audit — COG + PMTiles (2026-08-24)

**Status:** findings only. No code in this repo was changed by this audit, and the
pipeline was not run.

**Repo audited:** `G:\GH\ras2cng` — branch `master`, `origin
https://github.com/gpt-cmdr/ras2cng.git`, HEAD `95e05e5` (2026-08-03),
version 0.7.0.

**Divergent second copy:** `H:\CLB-Repos\ras2cng` is on branch
`feat/lwi-pipeline-gaps`, HEAD `697722e` (2026-07-08), version 0.6.0. It predates
essentially all of the raster/publication surface — it has no `maplibre.py`,
`raster_recipes.py`, `publication.py`, `webgis_service.py`, `viewer_manifest.py`, or
`stored_maps.py`, and a repo-wide grep for `compress=` / `overview_resampling` /
`OVERVIEWS=` in `ras2cng/` returns nothing. **Everything below is against the `G:`
copy**; none of these findings can be applied to the `H:` tree as-is. That copy
should be refreshed from `origin/master` before anyone edits it.

## Why this audit exists

The downstream RBFS flood-webmap pipeline (`clb_tx_webmap`) worked these tradeoffs
out the hard way in August 2026. The findings were written up in
`H:\CLB-Repos\clb_lwi_webmap\WEBMAP_BEST_PRACTICES_FROM_RBFS_2026-08-24.md`
(sections 2 and 5). `ras2cng` is the upstream renderer for those maps, so the
defaults here decide what the downstream pipeline has to undo.

### Honesty note about the "how RBFS fixed it downstream" pointers

The RBFS best-practices document is a **findings** document. For overview
resampling specifically, it describes what *should* be done — it is not a
description of shipped code. Verified against `clb_tx_webmap/pipeline` as of
2026-08-24:

* The area-matched coverage-mask overview builder **is not implemented anywhere**.
  Greps for `coverage_mask`, `area_matched`, `coherence`, `dither`, `LERC`,
  `MAX_Z_ERROR`, and `IGNORE_EXISTING` return zero hits outside the
  best-practices markdown itself.
* `postprocess_result_cogs.py` still calls `finish_cog(...,
  overview_resampling="nearest")` at both call sites (`:281`, `:336`), and
  `terrain_grid.py:450` defaults to `"nearest"` too.
* `mosaic_result_cogs.py:62` still mosaics wet/dry result rasters with
  `gdalwarp ... -r average`, unconditionally.

So RBFS has **diagnosed** the overview problem and **not yet fixed** it. What *is*
genuinely implemented downstream and safe to copy: the `finish_cog` atomic-write
durability pattern, the `cog_validate`-style gating expression, the tippecanoe
invocation, and the never-guess-a-CRS enforcement. Those are cited as such below.
Where a fix exists only as a described method, this doc says "described, not
shipped" rather than pointing at code that does not exist.

---

## Must-fix (correctness)

### M1 — Result-map COGs build `average` overviews over a wet/dry surface

**Where:** `ras2cng/mapping.py:1013` — `overview_resampling="average"` in
`_convert_to_cog()` (`ras2cng/mapping.py:970`). This is the conversion every
RASMapper-generated depth / WSE / velocity result raster passes through, so it is
the single highest-impact default in the repo.

**Problem:** A flood result raster is a continuous value *plus* an implicit wet/dry
mask carried by nodata. GDAL's `average` decimation treats a coarse cell as valid
if *any* contributing sub-cell is valid, so the wet mask grows monotonically with
every overview level. RBFS measured **113–326% wet-area inflation** from
`average`/`mode` overviews
(`WEBMAP_BEST_PRACTICES_FROM_RBFS_2026-08-24.md:47`). At the zoom levels most
users actually look at, the published inundation extent is materially larger than
the model produced.

The obvious alternative is not a fix either: `nearest` preserves neither area nor
value — RBFS measured speckled wet/dry margins and coarse-zoom value errors of
**up to 50 ft depth / 21 ft WSE** (same line).

**Required fix:** Build result-raster overviews with an **area-matched coverage
mask + mean of wet contributors**:

1. At each level, compute per-coarse-cell wet *coverage fraction* (wet
   sub-cells / total sub-cells).
2. Choose a per-level threshold from the coverage histogram such that the count of
   cells kept wet reproduces the level-0 wet area (scaled by the decimation
   factor) — i.e. solve for the threshold rather than hardcoding 0.5.
3. Break ties at the threshold by a coherence rank (prefer cells adjacent to
   already-wet cells) with a deterministic dither, so the result is stable across
   runs and does not produce checkerboarding.
4. For every cell kept wet, write the **mean** of its wet contributors only.
   **MEAN is mandatory for depth** — `MAX` inflates flood volume (a coarse cell
   inherits the deepest sub-cell), which is the wrong conserved quantity.

This preserves wet area to ~100% and values to tolerance.

**How RBFS handled it downstream:** *Described, not shipped.* The method is
specified in `WEBMAP_BEST_PRACTICES_FROM_RBFS_2026-08-24.md:47`; the downstream
code still uses `nearest`
(`clb_tx_webmap/pipeline/postprocess_result_cogs.py:281,336`) and still mosaics
with `-r average` (`mosaic_result_cogs.py:62`). An implementer here is writing the
first real implementation, not porting one. That argues for putting it in
`ras2cng` — upstream — where both RBFS and LWI inherit it.

**Interim mitigation if the full algorithm is out of scope:** at minimum make
`overview_resampling` an explicit caller-supplied argument on `_convert_to_cog()`
rather than a hardcoded literal, record the chosen method in the raster's
`rio_overview` tag (as `raster_recipes.py:284-287` already does), and surface it in
the manifest so downstream consumers know which tradeoff they inherited.

---

### M2 — `OVERVIEWS=AUTO` silently discards every `overview_resampling` setting

**Where — all three COG writers, none of which pass `OVERVIEWS`:**

* `ras2cng/mapping.py:1006-1016` — result maps.
* `ras2cng/project.py:1355-1366` — `_terrain_cog_creation_options()`, used at
  `ras2cng/project.py:700` and `ras2cng/project.py:1078`.
* `ras2cng/raster_recipes.py:293-301` — recipe outputs.

**Problem:** The GDAL COG driver's `OVERVIEWS` creation option defaults to `AUTO`,
which means *"if the source dataset already has overviews, reuse them verbatim."*
When that happens, `OVERVIEW_RESAMPLING=AVERAGE` (`project.py:1364`) and
`overview_resampling="average"` (`mapping.py:1013`, `raster_recipes.py:299`) are
**silently ignored** — no warning, no error, and the resulting file is a perfectly
valid COG carrying somebody else's pyramid.

This is not hypothetical here. It bites in exactly the common cases:

* HEC-RAS / RASMapper terrain TIFFs frequently ship with internal overviews or
  `.ovr` sidecars. `project.py:700` and `:1078` translate those source TIFFs
  directly, so the reuse path is the *normal* path for terrain.
* `raster_recipes.py:280-287` builds overviews on the intermediate GTiff, then
  `raster_recipes.py:293-301` copies that intermediate to COG. The COG driver
  reuses the pyramid built at line 283. Today both are `average` so the outcome
  matches by luck — the moment M1 is fixed at line 299 and not at line 283, the fix
  is a no-op.

**Required fix:** Pass `OVERVIEWS=IGNORE_EXISTING` on every COG creation
(`-co OVERVIEWS=IGNORE_EXISTING` for the `gdal_translate` paths,
`OVERVIEWS="IGNORE_EXISTING"` for the `rasterio.shutil.copy` paths) so the declared
resampling actually runs. If a pre-built pyramid is deliberately being carried
forward (the coverage-mask case in M1 builds its own), use `OVERVIEWS=FORCE_USE_EXISTING`
so the intent is explicit and a missing pyramid fails loudly instead of silently
regenerating with the wrong method.

**How RBFS handled it downstream:** *Described, not shipped* —
`WEBMAP_BEST_PRACTICES_FROM_RBFS_2026-08-24.md:48`. Downstream code passes no
`OVERVIEWS=` option either, so it has the same latent trap.

---

### M3 — A guessed EPSG code is stamped onto CRSs that have no authority match

**Where:**

* `ras2cng/maplibre.py:714-715`
  ```python
  epsg = source.crs.to_epsg(confidence_threshold=25)
  crs = f"EPSG:{epsg}" if epsg else source.crs.to_string()
  ```
  The result is written to the viewer manifest as `sourceCrs`
  (`ras2cng/maplibre.py:732`).
* `ras2cng/project.py:1290-1291` — `CRS.from_wkt(...).to_authority(min_confidence=25)`
  in `_tif_crs()`, whose output becomes the manifest's terrain `crs` field
  (`ras2cng/project.py:711`, `:1084`).

**Problem:** `confidence_threshold=25` / `min_confidence=25` is extremely
permissive — it will return an authority code for a CRS that merely resembles a
registered one. RBFS hit precisely this: a Texas Centric Albers definition
(`lat_0=18`, US survey foot) has **no exact EPSG match**, and the "closest" code,
EPSG:3083, is the **metre** variant. Stamping it would displace rasters by roughly
**25,000 km** (`WEBMAP_BEST_PRACTICES_FROM_RBFS_2026-08-24.md:52`). The linear-unit
mismatch is exactly the kind of difference a low confidence threshold is designed
to ignore.

The comment at `maplibre.py:711-713` correctly identifies the real problem —
proj4js does not bundle EPSG data, so the browser needs an explicit definition —
but the chosen remedy (guess a code) is the wrong one. The `sourceProj4` field
emitted at `maplibre.py:716-721` already solves the browser's actual need, from
the verbatim source CRS.

**Required fix:**

1. Raise the threshold to PROJ's default (`to_epsg()` with no argument, i.e.
   confidence 70) or higher, and on no match emit the source WKT/PROJ string
   verbatim — never a code.
2. Where a code *is* returned at low confidence, verify the linear unit and datum
   round-trip against the source before accepting it, and refuse on mismatch.
3. Keep `sourceProj4` as the authoritative browser-side definition. Consider
   emitting `sourceWkt` too, and marking `sourceCrs` as advisory.
4. Add a sanity gate on the derived WGS84 bounds (`maplibre.py:722-727`) — a
   wrong CRS shows up immediately as out-of-range or out-of-domain lon/lat.

**How RBFS handled it downstream — this one *is* shipped:**

* `clb_tx_webmap/pipeline/terrain_to_cog.py:173-174` hard-refuses reprojection:
  `if args.dst_crs: sys.exit("--dst-crs is incompatible with the source-aligned terrain grid policy")`.
* `resolution_policy.py:60-77` regex-parses the literal `UNIT["...", factor]`
  clause out of HEC-RAS WKT rather than guessing a code, precisely because
  "HEC-RAS projection WKT uses GDAL aliases such as `Albers` and `Foot_US` that
  older PROJ builds cannot fully instantiate."
* `build_ras_pmtiles.py:374-416` (`infer_missing_crs`) recovers a missing CRS only
  from unanimous, spatially-overlapping siblings and raises `ValueError` on any
  conflict (`:404-411`).
* `build_benefits_extents.py:20-41,63-70` enforces a `±180/±90` degree-range
  assertion plus an `EXPECTED_BBOX_TOLERANCE_DEGREES = 0.01` bbox check so a
  wrong-but-plausible CRS is still caught.

---

### M4 — `_convert_to_cog()` applies continuous-raster overviews to every map type

**Where:** `ras2cng/mapping.py:1013` — `overview_resampling="average"` with no
branch on map type. Compare `ras2cng/raster_recipes.py:282` and `:299`, which
*do* get this right:
`Resampling.nearest if recipe.categorical else Resampling.average`, driven by
`RasterRecipe.categorical` (`raster_recipes.py:28`).

**Problem:** The result-map surface is not uniformly continuous.
`mapping.py:76-93` and `:130-134` enumerate `inundation_boundary`,
`arrival_time`, `duration`, and `percent_inundated` alongside depth/WSE/velocity.
Averaging a class or boolean raster produces values that are not members of the
class set — a uint8 hazard/inundation raster averaged 2-and-4 yields 3, silently
inventing a class. Any classified or boolean raster that reaches
`_convert_to_cog()` is corrupted at every overview level.

**Required fix:** Give `_convert_to_cog()` the same categorical/continuous
discriminator `raster_recipes.py` already has. Thread the map type (or an explicit
`categorical: bool`) from the caller at `mapping.py:351-365` into the conversion,
and select `mode`/`nearest` for categorical, the M1 coverage-mask method for
continuous. Record the choice in the `rio_overview` tag either way, as
`raster_recipes.py:284-287` does.

Note that `raster_recipes.py`'s categorical handling is itself only half right:
`nearest` on a class raster preserves class membership but not class *proportion*.
`mode` is the better categorical decimator and should be preferred at both
`raster_recipes.py:282` and `:299`.

---

### M5 — No `rio_cogeo.cog_validate` gate anywhere; hand-rolled checks accept non-COGs

**Where:**

* `ras2cng/mapping.py:1018-1030` — post-conversion structural check.
* `ras2cng/publication.py:753-779` — `_validate_local_cog()`, the publication gate.
* `pyproject.toml:34-71` — **`rio-cogeo` is not a dependency in any extra.** A
  repo-wide grep for `cog_validate`, `rio_cogeo`, and `cog_translate` returns zero
  hits.

**Problem:** Both validators check the same four things: tiled, has overviews, has
a nodata/mask, and (in `publication.py:761-764`) driver and CRS present. None of
that establishes cloud-optimized *layout*. A plain tiled GTiff with a `.ovr`
sidecar or with its IFDs in the wrong order passes every one of these checks and
is not a COG — it will still serve, but with one HTTP range request per tile
instead of a header read, which is the entire point of the format.

Note also `publication.py:761` — `if source.driver != "GTiff"`. That is correct
(the COG driver reports as `GTiff` on read) but it means the check cannot
distinguish a COG from a non-COG at all.

**Required fix:** Add `rio-cogeo` to the `pmtiles`/`webgis`/`all` extras and gate
on the boolean, not on an exit code:

```python
from rio_cogeo.cogeo import cog_validate
is_valid, errors, warnings = cog_validate(path)
if not is_valid:
    ...  # fail with `errors`
```

Keep the existing nodata/mask check — `cog_validate` does not cover it and it is
the thing that makes the raster display transparently.

**Critical trap to preserve in the fix:** the `rio cogeo validate` **CLI always
exits 0**, including on an invalid file
(`WEBMAP_BEST_PRACTICES_FROM_RBFS_2026-08-24.md:50`). Any CI or shell step that
shells out to the CLI and trusts `$?` is a no-op gate. Gate on
`cog_validate(...)[0]` from Python.

**How RBFS handled it downstream — shipped:**
`clb_tx_webmap/pipeline/validate_serving_release.py:239` gates on the equivalent
expression against TiTiler's `/cog/validate` endpoint:
```python
result["ok"] = payload.get("COG") is True and not payload.get("COG_errors")
```
— the pass boolean *and* an empty error list, not an HTTP status or return code.

---

### M6 — COG and PMTiles writes are non-atomic and destroy the previous good artifact

**Where:**

* `ras2cng/maplibre.py:817-823` — the worst case:
  ```python
  output.unlink(missing_ok=True)
  subprocess.run([_pmtiles_command(), "convert", str(mbtiles), str(output)], ...)
  ```
  The existing artifact is deleted **before** the replacement is produced. A crash,
  a full disk, or a missing `pmtiles` binary between those two statements leaves
  the release with no tileset at all.
* `ras2cng/maplibre.py:498,516-525` — `_run_tippecanoe()` writes its intermediate
  `.mbtiles` next to the final output (in the release `tiles/` directory, not a
  temp dir) and `pmtiles convert` writes straight to the final path. A kill
  between the two leaves a truncated `.pmtiles` at the published path plus a
  possibly multi-GB orphan `.mbtiles` in the release tree.
* `ras2cng/project.py:693-705` and `:1071-1083` — terrain `gdal_translate` writes
  directly to `cog_path`. Failure is caught and downgraded to a console warning
  (`project.py:714-715`), so a truncated `_cog.tif` can survive on disk with no
  manifest entry and no non-zero exit.

`mapping.py:1004,1032` and `raster_recipes.py:289-304` already do this correctly
(`.{name}.{uuid}.tmp` + `Path.replace` + `finally` unlink). The pattern exists in
the repo; it just is not applied uniformly.

**Required fix:** Apply the staged-write pattern to all four sites: write to a
hidden sibling partial in the *destination directory* (same filesystem, so the
rename is atomic), `os.replace()` on success, `unlink(missing_ok=True)` in a
`finally`. Move `_run_tippecanoe`'s intermediate `.mbtiles` into the caller's
temp dir. Stop swallowing the terrain conversion failure at `project.py:714-715`
— or at minimum delete the partial output before continuing.

**How RBFS handled it downstream — shipped.** `finish_cog()` at
`clb_tx_webmap/pipeline/postprocess_result_cogs.py:205-222` is the reference:

```python
partial = output_path.with_name(f".{output_path.name}.partial-{os.getpid()}")
try:
    rio_copy(str(staged_tif), str(partial), driver="COG", compress="DEFLATE",
             predictor=2, blocksize=512, overview_resampling=overview_resampling,
             num_threads="all_cpus", BIGTIFF="IF_SAFER")
    os.replace(partial, output_path)
finally:
    partial.unlink(missing_ok=True)
```

PID-namespaced partial (so concurrent writers do not collide) + `os.replace` +
`finally` cleanup. The same pattern recurs at `terrain_grid.py:447-540` and
`build_ras_pmtiles.py:1191-1221`. RBFS also validates artifacts by magic bytes
before trusting them — `valid_pmtiles()` at `build_ras_pmtiles.py:45-49` requires
`size >= 127` and a leading `b"PMTiles"` — and deletes failures before rebuild
(`rbfs_tranche_worker.py:3103-3105`).

---

### M7 — Legacy vector PMTiles path never reprojects to EPSG:4326

**Where:** `ras2cng/pmtiles.py:59-64`
```python
gdf = gpd.read_parquet(input_file)
with tempfile.NamedTemporaryFile(suffix=".geojson", delete=False) as tmp:
    geojson_path = Path(tmp.name)
    geojson_path.write_text(gdf.to_json(), encoding="utf-8")
```

**Problem:** `GeoDataFrame.to_json()` emits raw coordinates with no reprojection.
Tippecanoe requires GeoJSON in WGS84 lon/lat (RFC 7946). Archive GeoParquet from
this repo is written in the model's native CRS — for these projects, Texas Centric
Albers or a State Plane zone in feet. Feeding State Plane feet to tippecanoe
produces coordinates in the millions, which are clamped or wrapped into a
meaningless tileset. There is no error; the output is a valid, wrong `.pmtiles`.

`ras2cng/maplibre.py:417` gets this right — `return gdf.to_crs("EPSG:4326")` — so
the modern path is safe. `pmtiles.py` is the older CLI-facing surface and is what a
user reaches via `ras2cng pmtiles`.

**Secondary problems in the same function:** `gdf.to_json()` materializes the whole
layer as a single Python string before writing — a RAS mesh of millions of cells
will exhaust memory. And `pmtiles.py:84-87` captures tippecanoe's stderr and
explicitly discards it (`pass`), so tippecanoe's feature-dropping warnings are
invisible.

**Required fix:** Reproject to EPSG:4326 before serialization (or refuse when the
source CRS is not 4326); stream via `gdf.to_file(..., driver="GeoJSONSeq")` or
`to_json` in chunks; log tippecanoe's stderr at warning level instead of
discarding it. If `pmtiles.py` is considered superseded by `maplibre.py`, mark it
deprecated in the CLI so it stops being a live footgun.

**How RBFS handled it downstream:** N/A — RBFS builds its own GeoJSON in 4326
before invoking tippecanoe (`build_ras_pmtiles.py:671-682`).

---

### M8 — `gdaladdo -r average` over RGBA with black transparent nodata darkens every flood edge

**Where:** `ras2cng/maplibre.py:812`
```python
[_gdaladdo_command(), "-r", "average", str(mbtiles), "2", "4", "8", "16", "32", "64", "128"]
```
combined with the nodata palette entry `nv 0 0 0 0` at `ras2cng/maplibre.py:629`
(terrain ramp) and `ras2cng/maplibre.py:676` (result ramp).

**Problem:** `gdaladdo` averages each band independently and does **not**
premultiply RGB by alpha. Nodata pixels are colored `(0, 0, 0, 0)` — black at zero
alpha. At every overview level, an edge tile mixing wet and nodata pixels averages
the wet RGB against pure black, so the flood boundary acquires a dark fringe that
gets wider the further you zoom out. The alpha channel is correct; the color is
not.

This is a display artifact rather than a data error, so it is lower severity than
M1 — but it is on the most-viewed product and the fix is nearly free.

**Required fix (cheap, low-risk):** change the `nv` entries at
`maplibre.py:629` and `:676` to carry the RGB of the adjacent ramp end with alpha
0 — e.g. `nv <r> <g> <b> 0` using the ramp's low color — so the transparent
pixels contribute a neutral color instead of black. Belt-and-braces: premultiply
before overview generation, or build the overviews from the pre-colorized numeric
raster instead. Verify on a single canary tile before applying fleet-wide
(`WEBMAP_BEST_PRACTICES_FROM_RBFS_2026-08-24.md:84-91` — canary-before-fleet).

**Note on the rest of this path — it is well built.** `maplibre.py:773-778` applies
`gdaldem color-relief` at *native* resolution before any downsampling, which is the
correct order (classify, then resample colors) and sidesteps the M1 value-averaging
problem entirely for the PNG display tiles. `maplibre.py:752-758` caps the max zoom
at the native no-upsample zoom, and `:789-790` warps to an exact Web Mercator
zoom resolution with `-tap` so `ZOOM_LEVEL_STRATEGY=LOWER` (`:803`) lines up. Only
the overview color mixing is wrong.

---

## Improvements (quality, size, interoperability)

### I1 — Vector tiles are unbounded: `--no-tile-size-limit --no-feature-limit`, no simplification control

**Where:** `ras2cng/maplibre.py:499-510`, in `_run_tippecanoe()`:
```python
"--force", "--read-parallel", "--no-tile-size-limit", "--no-feature-limit",
"--extend-zooms-if-still-dropping",
f"--minimum-zoom={min_zoom}", f"--maximum-zoom={max_zoom}",
```

Three separate issues:

1. **Unbounded tiles.** `--no-tile-size-limit` + `--no-feature-limit` chooses
   fidelity over size with no ceiling. A RAS mesh at high zoom will produce
   multi-megabyte individual tiles; MapLibre stalls on them and the CDN pays for
   them. This is the mechanism behind the ~2.5 GB of oversized model-dir
   `.pmtiles` observed in RBFS.
2. **`--extend-zooms-if-still-dropping` is dead code here.** It only acts when
   features are being dropped, and the two preceding flags guarantee nothing ever
   drops.
3. **No shared-node protection.** Tippecanoe's default simplification (`-S 10`)
   is active and simplifies polygon boundaries independently. Adjacent RAS mesh
   cells share edges; simplified independently, those edges diverge and open
   visible slivers between cells at low zoom.

**Suggested fix:** add `--no-simplification-of-shared-nodes` (or
`--detect-shared-borders`) for mesh polygon layers; make the size policy explicit
and per-layer rather than a blanket opt-out — `--drop-densest-as-needed` for dense
mesh layers, keeping `--no-tile-size-limit` only where completeness genuinely
matters; expose `--simplification` as a parameter. Log tippecanoe's stderr
(currently captured and dropped at `maplibre.py:517`) so dropping is observable.

**How RBFS handled it downstream — shipped:**
`clb_tx_webmap/pipeline/build_ras_pmtiles.py:671-682` and
`build_hms_pmtiles.py:85-87` both use
```
tippecanoe -o <partial.mbtiles> -l <layer> -z14 -Z0 --no-tile-compression
  --drop-densest-as-needed --extend-zooms-if-still-dropping
  --temporary-directory <dir> -f <geojson>
```
— i.e. the opposite tradeoff: bounded tiles, dropping the densest features as
needed, fixed `-Z0 -z14`. `--drop-fraction-as-needed` is opt-in only
(`build_ras_pmtiles.py:683-688`) because "a dense mixed geometry layer can make
tippecanoe retry sparsest features until the native binary crashes". Per-layer
`.mbtiles` are merged with `tile-join --no-tile-size-limit -f`
(`build_ras_pmtiles.py:1199-1209`). Neither pipeline sets an explicit `-S`.

### I2 — ZSTD everywhere is a serving-interoperability risk; DEFLATE is the safe default

**Where:** `ras2cng/project.py:1359` (`COMPRESS=ZSTD`, plus `OVERVIEW_COMPRESS=ZSTD`
at `:1362`), `ras2cng/mapping.py:1010`, `ras2cng/raster_recipes.py:247,297`.

ZSTD-in-TIFF requires a GDAL built with libzstd. It is widely available now but
not universal across tile servers, QGIS builds, and older GDAL in client
environments — and a reader without it fails hard rather than degrading. The
entire downstream RBFS stack standardized on DEFLATE for exactly this reason:
`postprocess_result_cogs.py:177-191` and `:205-222`, `terrain_grid.py:444-543`, and
`mosaic_result_cogs.py:61-68` all use `COMPRESS=DEFLATE` with `PREDICTOR=2`.

**Suggested fix:** make DEFLATE the default and ZSTD an opt-in flag, or at minimum
document the reader requirement in the manifest. This is a compatibility call, not
a correctness one — hence improvement, not must-fix.

### I3 — Predictor: `FLOATING_POINT`/3 is not automatically the best choice

**Where:** `ras2cng/mapping.py:1011` (`predictor="FLOATING_POINT"`),
`ras2cng/raster_recipes.py:248` (`predictor=2 if recipe.categorical else 3`),
`ras2cng/project.py:1361,1363` (`PREDICTOR=YES` / `OVERVIEW_PREDICTOR=YES`, which
resolve to 3 for float).

Predictor 3 (floating-point) is the textbook choice for float rasters, but RBFS
found **predictor 2 can beat 3 on smooth WSE surfaces** and standardized on
`predictor=2` throughout. Worth benchmarking on representative depth and WSE
rasters rather than assuming.

### I4 — Predictor is silently dropped in the `raster_recipes` final COG copy

**Where:** `ras2cng/raster_recipes.py:293-301`. The intermediate GTiff is written
with `predictor=2 if recipe.categorical else 3` (`:248`), but the final
`copy_raster(..., driver="COG", compress="ZSTD", blocksize=..., overview_resampling=..., BIGTIFF=...)`
passes **no predictor at all** — so the published COG falls back to the COG
driver default `PREDICTOR=NO`. The carefully chosen predictor applies only to a
temp file that is deleted. Pure size regression on the shipped artifact; add
`predictor=` to the copy call.

### I5 — LERC / LERC_ZSTD for float rasters (an engineering call, not a default)

Not used anywhere in this repo. RBFS measured **0.40× file size at 0.01 ft
tolerance** (`WEBMAP_BEST_PRACTICES_FROM_RBFS_2026-08-24.md:49`) — a large saving
on depth/WSE/terrain. It is **lossy**, so it is a deliberate engineering decision
per product, not something to switch on by default. If adopted: expose
`MAX_Z_ERROR` explicitly, bind the chosen tolerance into the manifest/provenance
so the loss is documented, and never apply it to a raster whose values feed a
downstream calculation. Not implemented downstream in RBFS either.

### I6 — Overview level counts are inconsistent and stop short of the tile size

Three different policies in one repo:

* `ras2cng/raster_recipes.py:507-513` — halve while `max(w,h)/factor >= 256`,
  cap factor 128.
* `ras2cng/terrain.py:1001-1004` — factors from `(2,4,8,16,32,64)` while
  `min(w,h)//factor >= 128`; caps at 64.
* `ras2cng/maplibre.py:812` — hardcoded `2 4 8 16 32 64 128` regardless of raster
  size.

With `BLOCKSIZE=512`, the pyramid should continue until the top level fits in a
single block (`max(w,h) <= 512`). Stopping at 256 or, worse, capping at factor 64
(`terrain.py:1003`) leaves large rasters without a cheap top level, so a
zoomed-out view reads many blocks from a mid-level overview. Unify on one helper
that terminates at the block size.

### I7 — Legacy raster PMTiles path in `pmtiles.py` is unsuited to float rasters

**Where:** `ras2cng/pmtiles.py:92-124`. `gdal_translate -of MBTiles -co
TILE_FORMAT=PNG` with a float32 depth/WSE COG produces an 8-bit rendered image
with no color ramp and no alpha, so nodata renders as opaque black. No `-r`
resampling is specified, and no overviews are generated at all (the MBTiles driver
writes the base zoom; `gdaladdo` is never run), so `MINZOOM` at `:113-114` has
nothing to apply to. `maplibre.py:741-823` is the correct implementation of this
job. Deprecate `generate_raster_pmtiles()` or route it through the `maplibre.py`
path.

### I8 — Terrain intermediate overviews use `average` with a nodata edge

**Where:** `ras2cng/terrain.py:1006` — `destination.build_overviews(overview_factors,
Resampling.average)`. Terrain is genuinely continuous, so this is far less harmful
than M1, but the same valid-if-any-sub-cell-is-valid rule expands the terrain
footprint outward at each level. Since these overviews are then likely reused
verbatim by the COG driver (see M2), the effect persists into the published
artifact. Interacts with M2 — fix that first, then decide the method deliberately.

### I9 — `except (ImportError, Exception)` swallows everything

**Where:** `ras2cng/terrain.py:1030`. `Exception` subsumes `ImportError`, so this
is a bare catch-all around the pyproj CRS comparison in `_crs_equivalent()`. A
genuine pyproj failure silently falls through to the EPSG-code and
whitespace-normalized-WKT fallbacks (`terrain.py:1033-1048`), which are much
weaker tests — two different CRSs can compare equal. Given M3, CRS comparison is
load-bearing. Narrow the catch and log.

---

## Priority order for an implementer

| # | Item | Why first |
|---|------|-----------|
| 1 | **M2** `OVERVIEWS=IGNORE_EXISTING` | One-line change per site, and **M1 and M4 are no-ops until it lands** |
| 2 | **M3** stop guessing EPSG | Silent ~25,000 km displacement; cheap to fix |
| 3 | **M5** `cog_validate` gate | Cheap; makes every other raster fix verifiable |
| 4 | **M6** atomic writes | Cheap; the reference implementation already exists in-repo and downstream |
| 5 | **M1** coverage-mask overviews | Highest correctness impact, largest effort — first real implementation anywhere |
| 6 | **M4** categorical discriminator | Small once M2 lands |
| 7 | **M7 / M8** | M7 is a legacy-path footgun; M8 is a two-line ramp change |
| 8 | I1–I9 | Quality, size, interoperability |

## Files touched by these findings

* `ras2cng/mapping.py` — M1, M4, M5, I3
* `ras2cng/project.py` — M2, M3, M6, I2, I3
* `ras2cng/raster_recipes.py` — M2, M4, I4, I6
* `ras2cng/maplibre.py` — M3, M6, M8, I1, I6
* `ras2cng/publication.py` — M5
* `ras2cng/pmtiles.py` — M7, I7
* `ras2cng/terrain.py` — I6, I8, I9
* `pyproject.toml` — M5 (`rio-cogeo` dependency)

## Sources

* `H:\CLB-Repos\clb_lwi_webmap\WEBMAP_BEST_PRACTICES_FROM_RBFS_2026-08-24.md` —
  sections 2 (tile serving / COG) and 5 (process notes). Section 2's rows 2.1–2.6
  are the origin of M1, M2, M3, M5, and I5. Its own reference section (lines
  94–101) notes that the consolidated 5-review brief behind these numbers is
  **not** checked into any repo and must be requested separately — the measured
  figures quoted in this audit trace to that markdown alone.
* `H:\CLB-Repos\clb_tx_webmap\pipeline\` — `postprocess_result_cogs.py`,
  `mosaic_result_cogs.py`, `terrain_grid.py`, `terrain_to_cog.py`,
  `resolution_policy.py`, `validate_serving_release.py`, `build_ras_pmtiles.py`,
  `build_benefits_extents.py`, `build_hms_pmtiles.py`.
