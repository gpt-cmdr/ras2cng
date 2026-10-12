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
