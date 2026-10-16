# Display terrain derivatives

`ras2cng.terrain_display` produces a **web display** copy of a terrain elevation
raster: an integer-multiple display grid, elevations rounded to 0.01 of the
stored vertical unit in the base level and in every embedded overview level,
written as a lossless Cloud Optimized GeoTIFF through the shared
[`ras2cng.cog`](user-guide/raster-delivery.md) writer.

It is never an engineering terrain. Do not use the output as an input to a
simulation, a mesh, or any hydraulic or scientific calculation, and keep using
the source terrain for those. The source file is only read, and a destination
that is or links to the source is refused before anything is written. Every
output carries `display_use=web_display_only_not_for_calculation`.

!!! warning "Status"
    This is a source/API proposal with synthetic tests. No real raster has been
    processed, no byte saving is measured, and no codec has been accepted by a
    browser decoder. `decoder_qualification` is `unqualified` on every plan.

## API

```python
from pathlib import Path
from ras2cng import terrain_display as td

info = td.inspect_source(
    Path("terrain.tif"),
    vertical_unit="us_survey_foot",   # required unless the band carries an unambiguous unit
    vertical_datum="<as documented>", # recorded verbatim, never verified; None stays "unknown"
    compute_sha256=True,              # identity for the receipt
)
plan = td.plan_display_terrain(info)           # frozen, JSON-able: plan.to_dict()
result = td.write_display_terrain(plan, Path("terrain.tif"), Path("display.tif"))
verdict = td.validate_display_terrain(plan, Path("display.tif"), expected=result)
assert verdict.valid, verdict.errors
```

`write_display_terrain` refuses an existing destination unless `overwrite=True`;
replacement is atomic, and the source is re-inspected immediately before it is
read.

A manually constructed `SourceGridInfo` must still supply its vertical unit.
If neither embedded nor declared-unit provenance is populated, planning records
that explicit unit as a declaration in the frozen plan; the caller's record is
unchanged. This never infers elevation units from the horizontal CRS. The writer
still compares all source facts and provenance strictly on re-inspection, and a
changed/conflicting embedded unit is refused. Validation also requires output
scale 1 and offset 0 before raw sample precision can describe physical values.

`benchmark_codecs()` / `select_smallest()` encode one rounded raster with each
candidate codec and report measured bytes and a base-level exact-equality check.
Run them on a tiny sample only. A smaller file is a byte decision; it is not
decoder acceptance.

## Grid

The display cell is an integer multiple `n` of the source cell, anchored at the
source origin, so every display cell is exactly an `n x n` block of source cells
and nothing is shifted.

| Source cell | Factor | Display cell |
|---|---|---|
| 3 m | 1 | 3 m |
| 1 m | 3 | 3 m |
| 0.5 m | 6 | 3 m |
| 3 ft | 3 | 9 ft |
| 1 ft | 10 | 10 ft |
| coarser than 12.5 ft | 1 | unchanged (never upsampled) |
| 2 m | refused | no integer multiple falls in 7.5-12.5 ft |

The target is 10 ft (3.048 m) inside a 7.5-12.5 ft window. Both are policy
fields. The nearest multiple wins and ties go to the smaller factor. A source
with no multiple in the window is **refused**, not fitted to some other grid.

**Non-divisible grids are refused.** If the factor does not divide both the
width and the height, planning fails. The alternatives all misstate the source:
padding publishes valid coverage the source never had (a fully valid 4 x 4
source at 1 m would become a valid 6 x 6 m raster), cropping drops real data,
and shrinking the cell breaks alignment. This is an initial limitation. A
caller that needs edge handling needs an explicit edge or strict-footprint
policy, which this revision does not provide; resolve the extent upstream.

Also rejected rather than guessed: geographic or unprojected CRS, rotated or
non-square pixels, missing nodata, a source carrying its own scale/offset, and
anything but a single band. A mosaic is not planned here: build one
authoritative raster upstream, with an explicit controlling grid, overlap and
resampling decision, and pass that.

## Footprint

A coarse cell is valid or it is not. It cannot reproduce every fine mask
boundary, and no setting of `min_valid_fraction` makes it area-neutral.

- `min_valid_fraction` (default 0.5) is a display policy, applied to **both**
  resamplers: a display cell is valid only when at least that share of its
  source cells are valid.
- `average` is then the mean of the valid source cells. `nearest` is the block's
  centre cell and is additionally invalid when that cell is. For an even factor
  the centre is offset `n // 2`: the later of the two middle indices on each
  axis, i.e. the lower-right of the four central cells in row/column array
  order.
- The write result reports what actually happened:
  `footprint_added_source_cells` (invalid source cells inside a valid display
  cell), `footprint_dropped_source_cells` (valid source cells inside an invalid
  display cell), `footprint_mismatch_fraction` = `(added + dropped) /
  source_valid`, and `footprint_area_ratio`.
- There is no built-in threshold. A caller may set
  `max_footprint_mismatch_fraction` to refuse a write that deviates more than
  they allow; `None` reports without refusing.

## Units and datum

Horizontal and vertical units are separate facts.

- Horizontal comes from the CRS (`metre`, `international_foot`,
  `us_survey_foot`, or `other(<factor>m)`).
- Vertical is declared, or read from the band unit when unambiguous. If both
  exist they must agree. The output states which it was in
  `vertical_unit_assurance`: `declared_and_embedded_agree`, `declared_only` or
  `embedded_only`.
- A bare `ft`/`foot`/`feet` is **refused**: the international and US survey
  foot differ by 2 parts per million, and which one a file uses is not
  recoverable from the spelling. Say `international_foot` or `us_survey_foot`.
- The vertical datum is whatever the caller declared, recorded verbatim, and is
  never converted. It does **not** override and is **not** checked against any
  embedded vertical CRS (whose name, if present, is only reported as
  `display_embedded_vertical_crs`). `vertical_datum_assurance` is therefore
  `declared_unverified` or `unknown`, never "verified".

Immediately before reading, the source is inspected again and every material
fact is compared with the plan: shape, transform, CRS, dtype, nodata,
scale/offset, horizontal and vertical unit (declared and embedded), datum,
size and, when the plan carries one, the SHA-256. Any difference is a refusal.
The hash is the only assurance about the pixel *content*; without it the check
covers metadata and size only.

## Precision: rounding is not LERC `MAX_Z_ERROR`

Valid samples are resampled first and then rounded to the nearest 0.01 of the
stored vertical unit. Rounding is `numpy.rint` (half to even) on
`value * 100` in float64, divided by 100. A decimal tie that is exactly
representable (0.125) goes to the even step (0.12); one that is not (0.135)
follows its binary value.

- The error against the resampled value is at most `increment / 2` (0.005),
  plus a float32 representation term of `|value| * 2^-24`. Both are reported:
  `quantization_error_bound` and `observed_quantization_error`.
- That bound is against the *resampled* value, not each source cell: averaging
  an `n x n` block is itself a change, and this policy does not hide it.
- Magnitudes above `increment * 0.1 / 2^-24` (about 16,777 units at 0.01) are
  refused, because float32 could no longer hold 0.01 steps.
- Nodata and the validity mask are preserved. No valid value may equal the
  output nodata value. Non-finite values inside the footprint are refused.

The encoder then compresses the rounded values **losslessly**. There is no
tolerance field on `CodecPolicy`; LERC variants are written with
`MAX_Z_ERROR=0`. `ras2cng.cog`'s default of lossy LERC at 0.01 is switched off
on every write (`lerc_max_z_error=None`), and compression and predictor are
passed explicitly so the `RAS2CNG_COG_*` environment cannot change an artifact.
A test pins this by forcing the lossy default on and checking the output stays
DEFLATE with no `compression_lossy` tag.

### Overview levels

The same quantum applies to **every embedded overview level**, through
`cog.area_matched_cog(overview_quantum=...)` (see below). Each level's value is
the mean of that cell's valid **stored-base** contributors, computed from
accumulators without additional intermediate rounding and rounded once, after
the area-matched mask is chosen. A level is
never rounded from the previous level's rounded values, which would compound.

Base rounding error is at most half a quantum plus float32 representation.
An overview's additional rounding error has that bound relative to its aggregate
of the already-rounded stored base. Relative to the corresponding original-source
aggregate with the same contributors, the conservative combined bound is one
quantum plus representation, not half a quantum. This is not a global difference
bound across spatial resampling, changed footprints or nearest-cell selection.
When an overview quantum is requested, both `MAX_Z_ERROR` and
`MAX_Z_ERROR_OVERVIEW` creation options must be finite zero (case-insensitive);
positive, negative, non-finite and unparseable tolerances are refused before
writing. `overview_quantum=None` preserves the shared writer's legacy behavior.

`validate_display_terrain` reads back the base and each embedded level (not just
the tags) and checks that every valid sample is on the quantum, that the level
shape is right, and that its valid area matches the writer's recorded ratio.
A raster that fits one 512 block has **no** overview levels; the validator says
so, `overview_levels_checked` is empty, and the per-level check is vacuous
there, not evidence that the overview pipeline works.

### Shared-writer option

`ras2cng.cog` gained one optional, keyword-only argument pair:

- `build_area_matched_overviews(..., value_quantum=None)`
- `area_matched_cog(..., overview_quantum=None)`

`None` (the default) leaves every pixel, tag and code path as before. A value is
in stored raster value units. It must be positive and finite, and is refused for
categorical or integer rasters, with any positive LERC tolerance (including a
positive `MAX_Z_ERROR` creation option, and the implicit lossy default), and
when a rounded valid value would be non-finite, unrepresentable in the raster's
dtype, or equal to nodata. Because the GDAL fallback cannot honour a quantum,
the writer raises instead of falling back whenever one was requested. The
report and file tags gain `overview_value_quantum` and `overview_value_rounding`
only when it is used.

## Codecs

Candidate codecs: DEFLATE level 9 and ZSTD level 19, each with the
floating-point predictor and with predictor 2, plus LERC_ZSTD at zero error. The
default is DEFLATE with the floating-point predictor because it is the most
widely readable, not because it is smallest. Predictor 2 is a candidate on
purpose: the winner depends on the surface, and on a smooth synthetic ramp
predictor 2 was smaller than predictor 3, which is the opposite of the
guidance in `ras2cng.cog` for float result surfaces. That synthetic result says
nothing about real terrain; it is why the benchmark must not offer only one
predictor. A candidate the local GDAL cannot write is reported with an error,
not raised. Scaled Int32 is deliberately not offered: it needs an independent
proof of scale/offset and nodata handling in the target reader first.

## Not done here

- Real-raster byte measurements and any claim of savings.
- Browser decoder acceptance, including float32 predictor, LERC and ZSTD
  paths, nodata, and range requests against the pinned renderer.
- Whether the target renderer serves a native (non-Web-Mercator) CRS. If it does
  not, a reprojected derivative needs its own recorded resampling, and exact
  alignment with the native grid can no longer be claimed.
- Non-divisible extents (an explicit edge / strict-footprint policy), mosaic
  planning, CLI exposure, and any change to the `archive --terrain` path.
