# Raster Delivery Defaults

Every Cloud Optimized GeoTIFF `ras2cng` writes goes through one module,
`ras2cng.cog`, so the overview method, compression, CRS handling, validation
and atomic replacement are decided in one place. Downstream pipelines import the
same helpers rather than re-deriving them:

```python
from ras2cng import area_matched_cog, validate_cog, atomic_output
```

**The defaults are tuned for cloud delivery at scale.** They assume the artifact
exists to be served — read over HTTP range requests, drawn in a browser, and
stored in quantity. If you are producing rasters for analysis rather than for a
map, read [Turning the lossy default off](#turning-the-lossy-default-off).

## Compression: LERC by default

Float rasters are written with **LERC at a 0.01 tolerance** (`LERC_DEFLATE`,
`MAX_Z_ERROR=0.01`).

LERC is *lossy*. It guarantees that no value moves by more than the stated
tolerance — the bound is exact, not statistical — and it returns most of the
file size:

| raster (full production file) | lossless | LERC @ 0.01 | saving |
|---|---|---|---|
| Water surface elevation | 313 MB | 35 MB | **−95.4%** |
| Depth | 656 MB | 289 MB | **−72.9%** |

### Why 0.01 is sufficient for mapping

The tolerance is **in the raster's own vertical units**. For HEC-RAS output in
US survey feet, 0.01 ft is about an eighth of an inch — one to two orders of
magnitude below the accuracy of the terrain, roughness and boundary conditions
the value was computed from. For **water surface elevation, depth and velocity
mapping it is not a meaningful loss**: it is far inside the noise floor of the
model that produced it, let alone the survey that fed it.

!!! warning "The tolerance is in raster units, not always feet"
    A model in metres gets a 1 cm tolerance, not 3 mm. A quantity with a small
    natural range — a dimensionless Froude number, or shear stress in lb/ft²
    where values are often below 1 — may find 0.01 too coarse. Check the value
    against your units and your quantity before trusting the default.

### What the default never touches

The default is narrowed deliberately rather than applied blanket:

- **Integer rasters stay lossless.** A sub-unit tolerance on a count buys
  nothing.
- **Categorical rasters stay lossless.** A class value "within tolerance" is a
  different class. Asking for LERC on one explicitly raises rather than
  silently degrading it.
- **The nodata mask is never approximated.** The wet/dry boundary is a hard
  edge, and it survives the codec byte for byte. Only values inside the valid
  area are affected.

### The loss travels with the file

Every lossy artifact records what it cost, so the decision is discoverable from
the raster rather than from a build log:

```console
$ gdalinfo depth_max_cog.tif
  COMPRESSION=LERC_DEFLATE
  MAX_Z_ERROR=0.01
  compression_lossy=true
  compression_max_z_error=0.01
```

## Turning the lossy default off

**Do this for any raster whose values feed a downstream calculation** rather
than a rendered map — recomputing depth from a stored WSE surface, differencing
two runs to a fine tolerance, or anything supporting a regulatory determination
where you would rather carry the bytes than argue about the eighth of an inch.

One environment variable switches it off for a whole run:

```bash
export RAS2CNG_LERC_MAX_Z_ERROR=off      # also: none, 0, lossless, false
ras2cng archive path/to/project /out --results --terrain
```

Or per call:

```python
area_matched_cog(source, destination, lerc_max_z_error=None)   # lossless
area_matched_cog(source, destination, lerc_max_z_error=0.001)  # tighter bound
```

Lossless output falls back to `DEFLATE` with predictor 3, which is the
benchmarked best lossless pairing for float surfaces.

A malformed value fails loudly rather than quietly publishing lossy rasters —
`RAS2CNG_LERC_MAX_Z_ERROR=yes` is an error, not an "on".

## Compression fallback: DEFLATE, not ZSTD

Where LERC does not apply, compression is **DEFLATE with predictor 3**.

ZSTD-in-TIFF needs a GDAL built with libzstd, and a reader without it fails hard
rather than degrading. DEFLATE costs 2–8% at the same predictor, which is cheap
insurance for an artifact leaving your network. Set
`RAS2CNG_COG_COMPRESS=ZSTD` for internal archives that never leave a known-good
GDAL.

!!! note "LERC needs a modern GDAL too"
    LERC-in-TIFF requires a GDAL built with the LERC codec (GDAL ≥ 3.4 in
    practice). If you publish to clients on older GDAL builds, either set
    `RAS2CNG_LERC_MAX_Z_ERROR=off` or confirm the reader first. This is the same
    class of concern that rules out ZSTD; LERC earns the exception because the
    size reduction is an order of magnitude larger.

### Predictor

Predictor 3 (floating point) is used for continuous float rasters, expressed as
`PREDICTOR=YES` so GDAL resolves it — the literal `3` is float-only and *hard
fails* on integer rasters. Class data uses predictor 2, where predictor 3
measured 2.4× larger.

Predictor is not emitted alongside LERC: LERC does its own encoding and GDAL
rejects the pairing.

## Overviews preserve wet area

A flood raster is a continuous value *plus* an implicit wet/dry mask carried by
nodata. GDAL's `average` marks a coarse cell valid when *any* contributing
sub-cell is valid, so the wet mask grows at every level — measured at **133–156%
of true wet area** on synthetic rasters and **up to 224%** on production models.
`nearest` holds area only statistically and speckles the margin.

`ras2cng` builds overviews that hold the level-0 wet area to **100.0%**: it
solves for a per-level coverage threshold from the coverage histogram, breaks
ties on a coherence rank with a deterministic dither, and writes the **mean** of
each cell's wet contributors — never the max, which would inflate flood volume.

Categorical rasters get the same mask with a modal value.

### Native engine: terrio

When the installed `terrio` has the area-matched COG writer (CLB-Engineering-Corporation/terrio
#9: `Raster.write(metadata=..., overview_resampling="area_matched")`), the
whole COG is written by terrio: the overviews are built in Rust and encoded by
terrio, LERC included. The choice is capability-detected, so stock PyPI terrio
(0.5.0) keeps the GDAL path. Pinned by `tests/test_terrio_equivalence.py`:

- **Lossless output** (DEFLATE/ZSTD floats, integer, categorical) is the GDAL
  path's file, level for level and pixel for pixel, with the same tags.
- **LERC output** holds the same bound, but not the same values. Measured on
  Bald Eagle Depth (Max) (1,345,691 wet pixels, `MAX_Z_ERROR=0.01`, 512 tiles):
    - Both encoders hold the bound exactly: max error 0.01000 each, mean
      0.0048 each.
    - **8.1% of pixels decode differently**, by **at most one quantization
      step (≤ 0.02)**. The differences come in whole 8×8 micro-blocks:
      terrio's lerc-writer stores the exact block minimum as the offset, and
      Esri/GDAL often does not.
    - **Size.** With ras2cng's default `LERC_DEFLATE` the terrio file is
      within 0.4% of GDAL's; with plain `LERC` it is up to 18% larger on a
      sparse result raster (table below). Since CLB-Engineering-Corporation/terrio #22 the
      padding of partial edge tiles is masked as GDAL masks it. The rest of
      the plain-LERC gap is lerc-writer's encoder; a size fix is pending
      upstream (roteiro-gis/lerc-rust #48) and ras2cng does not depend on it.
    - The nodata mask is identical, and so are `compression_lossy`,
      `compression_max_z_error` and the overview provenance tags.

  **Code that relies on byte or value equality with artifacts written by
  the GDAL path must ask for lossless** (`lerc_max_z_error=None` or
  `RAS2CNG_LERC_MAX_Z_ERROR=off`). It is lossless output that is identical
  across engines, not LERC output.
- `overview_area_ratios` and `overview_base_valid_pixels`:
    - **terrio with CLB-Engineering-Corporation/terrio #23** (`report_metadata=` and
      `area_matched_nan_valid=` on `Raster.write`, capability-detected)
      measures them in the pass that builds the levels and writes them into
      the header itself, formatted as ras2cng formats them. ras2cng makes
      **no pre-scan**. NaN under a numeric nodata counts as valid, as in
      rasterio's mask, so those rasters stay on terrio and match the GDAL
      path. Report tags carried by a re-COGed ras2cng COG are dropped from
      the copied source tags and rewritten. The returned report is still
      checked: a missing report or different factors discard terrio's file
      and the GDAL path runs; an empty raster goes to the GDAL path, which
      labels it `average` with a `fallback_reason`. The files are
      byte-identical to the scan path's (measured below).
    - **Older terrio**: one streamed pass over level 0, made before the
      write because the tags go into the COG header. That pass is exact: the
      selection keeps `round(valid / f²)` cells, or every partly wet cell
      when there are fewer. It is also the check for NaN under a numeric
      nodata (below). terrio with CLB-Engineering-Corporation/terrio #18 returns its own
      measurement from `Raster.write()`; ras2cng compares the factors, the
      valid count and every ratio at the tag's precision against the scan,
      and on any difference discards terrio's file and writes the GDAL path
      instead. The pass cost about 45% of terrio's write time. It remains
      the fallback and the oracle the tests compare #23's tags against.
- ras2cng never passes overview factors to terrio's COG writer: it relies on
  the automatic pyramid, which halves until a level fits one block, exactly
  as `overview_factors()` does. Since CLB-Engineering-Corporation/terrio #19 explicit factors are
  honoured on COG, which does not change this path, and the report check
  above would catch a different pyramid.
- The GDAL path still runs for:
    - a raster with no nodata;
    - more than one band;
    - an internal mask or alpha band;
    - a raster above the pixel ceiling;
    - a creation option terrio does not act on;
    - **NaN under a numeric nodata, on terrio without #23**. That terrio
      treats NaN as dry, but rasterio's mask treats it as a wet value.

  Each fallback reports exactly what it did before.

Measured on Bald Eagle (peak working set, fresh process, default LERC 0.01):

| artifact | GDAL path | terrio | size (GDAL → terrio) |
|---|---|---|---|
| Terrain50 COG (15,777 × 12,599, `LERC_DEFLATE`, `LEVEL=9`) | 71.6 s / 2,197 MiB | 25.8 s / 2,071 MiB | 377.96 → 377.74 MB (−0.06%) |
| Depth (Max) COG (`LERC_DEFLATE`) | 4.3–5.6 s / 621 MiB | 2.5 s / 485 MiB | 2.243 → 2.252 MB (+0.37%) |
| Terrain50 COG, plain `LERC` (`SPARSE_OK`) | | | 386.62 → 387.13 MB (+0.13%) |
| Depth (Max) COG, plain `LERC` | | | 3.009 → 3.542 MB (+17.7%) |

terrio main with #18, #19 and #22, 2026-10-28. The terrio times include the
level-0 scan. They were taken on a shared, loaded host, so compare them only
against each other. Before #22 the `LERC_DEFLATE` Depth file was 2.262 MB
(+0.8%). On this Depth COG, 4.5% of base pixels differ from GDAL's, by at
most 0.0197, and every level is within 0.01 of the lossless pyramid with an
identical mask.

Dropping the pre-scan (terrio #23, T13, 2026-10-30; `area_matched_cog`
in-process time, fresh process each, two rounds, default `LERC_DEFLATE`):

| artifact | scan + terrio write | terrio with report | peak (process) | output |
|---|---|---|---|---|
| Terrain50 1x (197,179,058 valid px, terrain options) | 13.0–14.2 s | 6.0–6.7 s | 1,941–2,042 → 1,922 MiB | byte-identical |
| p07 Depth (Max) (1,345,691 valid px) | 0.82 s | 0.32–0.35 s | unchanged (308–375 MiB) | byte-identical |
| p07 WSE (Max) | 0.82 s | 0.33 s | unchanged | byte-identical |

Tags are identical by construction (`overview_area_ratios`
`1.000000,1.000000,1.000000,1.000000,1.000002` and
`overview_base_valid_pixels` `197179058` on Terrain50). Peak memory does not
change, because the scan streamed and terrio holds the base level whole.

terrio reads the base level whole, so peak memory is still about the
raster's size. Only the NumPy accumulators go.

### Layout validation

`validate_cog` uses `terrio.validate_cog` when it exists (CLB-Engineering-Corporation/terrio
#10). It is a port of `rio_cogeo.cog_validate` with the same checks and
messages, and it reads only the TIFF structure. If terrio cannot read a file,
the check falls through to rio-cogeo. Set `RAS2CNG_COG_ENGINE=gdal` to use
GDAL and rio-cogeo for both the writer and the validator.

## CRS: never guessed

An authority code is published only when the registered definition round-trips
the source's linear unit and ellipsoid. Matching at a permissive confidence
returns EPSG:3083 — the **metre** variant — for a Texas Centric Albers
definition in US survey feet, which would displace the raster by roughly
25,000 km. Where no code verifies, the verbatim source definition is emitted
instead, and `sourceProj4` remains the authoritative field for the browser.

## All the knobs

| variable | default | effect |
|---|---|---|
| `RAS2CNG_LERC_MAX_Z_ERROR` | `0.01` | LERC tolerance in raster units; `off` for lossless |
| `RAS2CNG_COG_COMPRESS` | `DEFLATE` | lossless codec / LERC base (`ZSTD` for internal use) |
| `RAS2CNG_COG_PREDICTOR` | `YES` | predictor for lossless float output |
| `RAS2CNG_AREA_MATCHED_MAX_PIXELS` | `400000000` | ceiling above which overviews fall back to GDAL resampling |
| `RAS2CNG_COG_ENGINE` | `auto` | `auto`: terrio where it reproduces the GDAL path; `terrio`: require it; `gdal`: never use it |
| `RAS2CNG_TIPPECANOE_BOUNDED` | unset | cap vector tile size on every tileset |

Rasters that exceed the pixel ceiling fall back to GDAL resampling and **record
the reason in their tags** — a fallback that is not labelled is
indistinguishable from the fix having worked.
