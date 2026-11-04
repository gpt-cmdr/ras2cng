# Display COG (EPSG:3857 derivative)

`ras2cng.publish_display_cog` builds a **visualization derivative** of a native raster for viewers
that only accept EPSG:3857. The native raster (for example EPSG:2278 or 2277, US survey feet) stays
the authoritative product and is never modified.

```python
from ras2cng import publish_display_cog

result = publish_display_cog("depth_native.tif", "depth_3857.tif")          # numerical
result = publish_display_cog("class_native.tif", "class_3857.tif", categorical=True)
print(result.to_dict())   # JSON-safe provenance
```

## Guarantees

- **Nearest base grid.** The full-resolution EPSG:3857 grid is resampled nearest-neighbour only (no
  interpolation, routing or terrain subtraction), so every base-grid value existed in the source.
  Changing the CRS does **not** convert pixel value units (feet stay feet).
- **Overviews are a separate, labelled policy** (`overview_policy`), not nearest: continuous
  rasters get the existing area-matched **mean** overviews; `categorical=True` rasters (winner
  index, valid count, classes) get discrete **mode** overviews and are never numerically
  interpolated (an unavailable area-matched build falls back to GDAL `mode`, not `average`).
  `overview_method` records what actually ran.
- **Preserved:** band values, `units` tag, dtype, nodata (including NaN) and, with
  `categorical=True`, integer class codes (modal overviews). The supported dtype boundary is
  integer types up to 32 bits, `float32` and `float64`: GDAL warps through
  float64, which holds integers exactly only up to 2**53, so `int64` and `uint64` are rejected
  (nearest would turn 9007199254740993 into 9007199254740992 under an unchanged dtype). Values are
  never cast or silently altered; convert such a raster to a narrower type yourself first.
- **Streaming.** The warp runs one 512-px window at a time (`WarpedVRT`) into a scratch GeoTIFF; the
  COG is then written by the existing `ras2cng.cog.area_matched_cog` (lossless: `lerc_max_z_error=None`)
  and gated by `ras2cng.cog.assert_valid_cog`. There is no second COG renderer.
- **Source immutable.** Full SHA-256 of the source before and after; the result records both.
- **No replace.** The destination must not exist; it is adopted atomically (hard link, or Windows
  non-overwriting rename) so a destination that appears mid-publish wins and is untouched. Only this
  call's own hidden partials are cleaned up.
- **Clean metadata.** Output tags contain no paths or source captions: `display_derivative`,
  `derivative_kind=visualization`, `base_resampling=nearest`, `overview_policy`, `value_conversion=none`, `source_sha256`,
  `source_crs`, plus the standard overview/compression tags. The published `source_crs` tag is
  only a verified authority identifier (e.g. `EPSG:2278`), or the neutral `unverified` for an
  unregistered/custom CRS; a custom CRS's name or WKT (which can embed a private path) is never
  written to the TIFF.

## Rejected inputs

Source and destination that alias (same path, hard link, symlink), existing destination, missing
CRS, band count other than 1, `int64`/`uint64` bands, missing/non-finite (non-NaN) or unrepresentable nodata, and a
non-finite or out-of-extent output grid. Failures leave no destination.

## `DisplayCogResult`

Frozen dataclass: `schema` (`ras2cng.display_cog/1`), `kind` (`visualization_derivative`),
`authoritative` (`False`), `source`, `destination`, `categorical`, `base_resampling` (`nearest`), `overview_policy` (`area_matched_mean` | `categorical_mode`), `value_conversion`,
`source_sha256_before/after`, `source_unchanged`, `output_sha256`, `output_size_bytes`, `source_crs`
(`describe_crs`), `output_crs`, `source/output_dtype`, `source/output_nodata`, `source/output_units`,
`source/output_shape` (height, width), `source/output_transform`, `output_bounds`, `overview_method`,
`overview_factors`, `compression`, `lossy_max_z_error`, `validation`. `to_dict()` is `json.dumps`-safe
(NaN nodata becomes `"nan"`).

The result is **internal provenance, not a public-site receipt**: `source`, `destination` and the full
`source_crs` (including verbatim WKT for custom CRSs) may contain local paths or private names.
Downstream publishers must project an explicit field allowlist and never serialize `to_dict()`
wholesale into a public catalog.

This tool makes no scientific or H&H approval claim; the derivative is for display only.
