# Raster Delivery Defaults

Every Cloud Optimized GeoTIFF `ras2cng` writes goes through one module,
[`ras2cng.cog`][], so the overview method, compression, CRS handling, validation
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
| `RAS2CNG_TIPPECANOE_BOUNDED` | unset | cap vector tile size on every tileset |

Rasters that exceed the pixel ceiling fall back to GDAL resampling and **record
the reason in their tags** — a fallback that is not labelled is
indistinguishable from the fix having worked.
