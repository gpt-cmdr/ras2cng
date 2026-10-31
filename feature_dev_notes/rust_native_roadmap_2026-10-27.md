# Rust-native roadmap for ras2cng (2026-10-27)

Goal: make ras2cng Rust-native wherever it pays, by implementing the missing
features in our Rust engines and forks (then submitting the general ones
upstream). Each issue below carries its own evidence, API sketch and acceptance
criteria. This note maps each one to the ras2cng code it unblocks.

## Already Rust in ras2cng

| Component | Engine | ras2cng entry |
|---|---|---|
| HDF reads (default) | ras-commander-fast-hdf 0.2.3 via ras-commander 0.110.1 | everywhere ras-commander reads HDF |
| WSE/Depth mapping, pure 2D (opt-in) | ras-commander-fast-map 0.8.0 | `fast_map.map_plan`, `map --engine fast-map` |
| Inundation boundary (opt-in, unreleased API) | fast-map PR #5 / ras-commander PR #709 | `fast_map.map_boundary` |
| Vector tiles | freestiler | `vector_tiles.py` |
| Raster tiles + hillshade (default when installed) | terrio 0.5.0 | `raster_tiles.py` |

## Measured terrio memory (Bald Eagle Terrain50 export, 15,777 x 12,599 float32, 795 MB)

| Operation | Time | Peak |
|---|---:|---:|
| `info` | 0.03 s | 33 MiB |
| lazy window read 2048^2 | 0.6 s | 59 MiB |
| `GeoTiffWriter` row streaming (whole raster) | 4.7 s | 197 MiB |
| `dem color-relief` path to path | 9.9 s | 8,375 MiB |
| `dem hillshade` path to path | 1.5 s | 1,738 MiB |
| `warp` RGBA to 3857 at z14 | 11.8 s | 1,885 MiB |
| PMTiles write (143 M px RGBA) | 19.6 s | 10,952 MiB |
| COG write (DEFLATE) | 3.9 s | 1,657 MiB |

The primitives (windowed reads, streaming writes) exist; the operations do not
use them. Hence `RAS2CNG_TERRIO_MAX_PIXELS` (400 M) and the GDAL fallback.

COG probe (terrio 0.5.0): LERC with `MAX_Z_ERROR` works (max error exactly the
tolerance, nodata kept). ZSTD, DEFLATE, `PREDICTOR=3` and `BLOCKSIZE` work.
`PREDICTOR=YES` gives predictor 2 on float (GDAL gives 3).
`OVERVIEWS=FORCE_USE_EXISTING` is silently ignored. Overview resampling is
limited to nearest/average/min/max/mode, and there is no validation API.

## Issues filed

| # | Repo / issue | Unblocks in ras2cng |
|---|---|---|
| 1 | [terrio#2](https://github.com/gpt-cmdr/terrio/issues/2) windowed `dem` | `raster_tiles.render_pmtiles` beyond 400 M px |
| 2 | [terrio#3](https://github.com/gpt-cmdr/terrio/issues/3) streaming PMTiles writer | same; removes the GDAL raster fallback |
| 3 | [terrio#4](https://github.com/gpt-cmdr/terrio/issues/4) chunked warp / mosaic | `raster_tiles` warp, `mapping._reproject_tifs`, `terrain._merge_tifs` |
| 4 | [terrio#5](https://github.com/gpt-cmdr/terrio/issues/5) COG supplied/area-matched overviews, `PREDICTOR=YES`, strict options | `cog.area_matched_cog` off rasterio/GDAL |
| 5 | [terrio#6](https://github.com/gpt-cmdr/terrio/issues/6) COG validation | `cog.validate_cog`; drop rio-cogeo |
| 6 | [terrio#7](https://github.com/gpt-cmdr/terrio/issues/7) one-pass shaded relief | hillshade without the NumPy round trip |
| 7 | [freestiler#1](https://github.com/gpt-cmdr/freestiler/issues/1) topology-preserving simplification | freestiler preferred over tippecanoe in `auto` |
| 8 | [freestiler#2](https://github.com/gpt-cmdr/freestiler/issues/2) multi-layer without all in memory | large multi-layer viewer tilesets |
| 9 | [fast-hdf#34](https://github.com/gpt-cmdr/ras-commander-fast-hdf/issues/34) GeoParquet archive profile | `project._write_geoparquet`, `geometry._hilbert_sort`, archive mesh layers HDF to Parquet natively |
| 10 | [fast-hdf#35](https://github.com/gpt-cmdr/ras-commander-fast-hdf/issues/35) PMTiles holes/multipart, columnar properties | native results-detail tilesets, boundary tiles |
| 11 | [fast-map#6](https://github.com/gpt-cmdr/ras-commander-fast-map/issues/6) native effective terrain (no HEC-RAS) | `fast_map.TerrainResolver`; multi-source boundaries; HEC-RAS-free mapping |
| 12 | [fast-map#7](https://github.com/gpt-cmdr/ras-commander-fast-map/issues/7) storage-area surfaces | lift the storage-area exclusion in `fast_map._check_scope` |
| 13 | [fast-map#8](https://github.com/gpt-cmdr/ras-commander-fast-map/issues/8) arrival/duration/percent | route ADR map types through fast-map |
| 14 | [fast-map#9](https://github.com/gpt-cmdr/ras-commander-fast-map/issues/9) velocity and derived maps | default `map --engine fast-map` without HEC-RAS |

Issues were enabled on the gpt-cmdr/terrio and gpt-cmdr/freestiler forks for
this. Upstream references in the issues are in backticks so they do not post
backlinks on upstream trackers. Related upstream work:
`opengeos/terrio#338` (stream first-level overview reads),
`walkerke/freestiler#16`, `#17` and `#30`.

## Suggested order

1. terrio #3 and #2 (biggest memory wins, and they remove the GDAL raster fallback).
2. fast-map #6 (removes the HEC-RAS terrain export, about 190 s on Bald Eagle, and unlocks multi-source boundaries).
3. fast-map #7 (storage-area models are common).
4. terrio #5 and #6 (move `cog.py` off rasterio; needs care, since downstream webmaps import it).
5. fast-hdf #34 and #35, fast-map #8 and #9, freestiler #1 and #2, terrio #4 and #7.

## Not worth Rust (from the 2026-10-27 survey)

polars, SedonaDB (no Windows wheel), geoarrow-rust, geo-index, async-tiff,
cramjam, and a custom PyO3 overview kernel (`cog.py` is already vectorized and
I/O-bound). Cheap non-Rust wins: ruff (no linter configured), orjson for
NDGeoJSON writing, and the stale `rio-tiler<8` pin (9.4 is current).

## Results (all workstreams finished 2026-10-27)

All PRs are drafts on the gpt-cmdr forks, base `main`. Nothing merged, nothing upstream.
Measurements are peak working set (psutil, fresh process) on Bald Eagle unless noted.

| Repo PR | Issue | Status | Key result |
|---|---|---|---|
| terrio #8 | #3 streaming PMTiles | done | byte-identical; 10.9 GB / 50.8 s -> 85 MiB / 7.5 s (z14 NEAREST) |
| terrio #11 | #2 windowed dem | done except `calc` | color relief 8.4 GB / 15.7 s -> 182 MiB / 1.9 s; hillshade 1.7 GB -> 192 MiB |
| terrio #12 (on #11) | #7 shaded relief | done | 0 DN vs ras2cng NumPy blend; 17 s / 8.4 GB -> 4.2 s / 171 MiB |
| terrio #13 (on #8) | #4 chunked warp | partial | 1.9 GB -> 928 MiB pixel-identical at default chunk; smaller chunks change <=381k px by ~1 DN (gdalwarp -wm behaviour, accepted) |
| terrio #9 | #5 COG overviews / PREDICTOR=YES / strict options | done | area_matched = ras2cng, 0 px diff; Terrain50 77.7 s / 2.1 GB -> 9.1 s / 1.9 GB (base level not streamed) |
| terrio #10 | #6 validate_cog | done | 31 files x strict on/off: 0 disagreements with rio-cogeo; remote 4.77 s -> 0.47 s |
| fast-map #10 | #7 storage areas | done, **parked** | p07 bit-identical incl. 466,912 SA px |
| fast-map #11 (on #10) | #6 native terrain | partial | multi-source mosaic not reproducible (84% best fit); top-priority source TIFF matches RAS Mapper exactly |
| fast-map #12 (on #10) | #8 arrival/duration/percent | done (horizontal) | arrival + percent bit-identical on 1,333,183 px; 8 duration px +1 interval; 110 s -> 8-10 s |
| fast-map #13 (on #10) | #9 velocity | research | ~78% bit-identical, ~92% within 4 ulp; fails on non-square, wet/dry-edge and perimeter cells |
| fast-hdf #36 | #34 GeoParquet profile | done | identical rows/bbox vs `_write_geoparquet`, order = DuckDB ST_Hilbert; 2.8-4.1 s / 1460 MiB -> 1.5-2.1 s / 855 MiB |
| fast-hdf #37 | #35 PMTiles holes + columnar props | done | byte-identical; Python prep 3 s -> 0.01 s on 340k cells |
| freestiler #3 | #2 multi-layer streaming | done | byte-identical; 10 layers 1,728 -> 311 MiB; deterministic coalesce |
| freestiler #4 | #1 simplification | rejected for meshes | <1% of mesh vertices are simplifiable; adds `gzip_level` |

Velocity findings (#9): cell vector = unweighted least squares of face-normal velocities;
face vector = normal + tangential from mean of wet neighbours; corner = mean of touching wet faces;
pixel = slopingPretty stencil per component. Same in horizontal and sloping. D x V and D x V^2
bit-identical given RAS Mapper's own d and v; Froude 87% (1 ulp rest); shear formula unknown;
Max derived products need a time-series pass, not Max d x Max v.

### ras2cng bugs found

- `geometry._hilbert_sort` calls `hilbert_encode`, which does not exist in DuckDB (checked 1.5.6
  and 2.0, with and without spatial). The bare `except` swallows it, so **no archive has ever been
  Hilbert sorted**. Fix with `ST_Hilbert` or the fast-hdf #36 writer.
- Flat `bbox_xmin..` columns fail the geoparquet-io spec check (4 failures); the `bbox` struct passes 30/30.

## Decisions (user, 2026-10-27)

1. **bbox:** switch archives to the spec `bbox` struct (`bbox.xmin`). Update docs/queries/schemas.py.
2. **MVT feature IDs:** make dropping them an **option** (default keep). On the 87k mesh, z10-15:
   default 13.8 MB; `gzip_level=6` 11.9 MB (-14%); plus `generate_ids=False` 10.2 MB
   (-14% more, 1.7 MB; -26% total vs default; tippecanoe 9.1 MB).
3. **Storage areas:** not supported yet. Refuse in fast-map and fall back to RAS Mapper (ras2cng
   `fast_map.py` already falls back on storage areas). Park fast-map #10; restack #12/#13 onto
   `main` so they do not depend on it, and qualify them on SA-free models.
4. **Velocity reference corpus:** use the fast-map qualification corpus (eBFE Spring River 422,715
   cells, eBFE Spring Creek 340,163, Bald Eagle dam break 87,039, Bald Eagle bridges 24,718, Davis
   pipes/conduits 2,716; see ras-commander `docs/development/fim-mapping-performance.md`) and/or
   other eBFE models, for irregular-mesh, wet/dry-edge and perimeter rules.
5. **terrio #4:** chunk-size dependence accepted. File follow-up issues for streaming `calc` and
   exact-rounding windowed VRT reads. terrio #5 follow-ups: expose area ratios, honour int
   overview factors on COG, strict GTiff options (later).

## Remaining tasks

- fast-map: restack #12/#13 on main; velocity on the corpus (#9); sloping mode for #8; shear
  formula; Max derived products via time-series pass; #11 multi-source mosaic needs a reference
  where flood reaches the lower-priority source; terrain modifications not started.
- fast-map clippy `dead_code` error in `tests/hardening.rs` is on `main` (Windows only).
- terrio: file follow-up issues (above); MBTiles still buffers encoded tiles; non-grid rasters
  still warped whole before tiling; supplied overviews refuse explicit masks/alpha.
- freestiler: `gzip_level` pass-through in the layered path once #3 and #4 both land; R binding
  unchanged; Rust tests cannot build on this host (test-only C++ dep TLS failure), CI must run them.
- fast-hdf: add new binding name to ras-commander's allowed-bindings list before qualifying a
  wheel; switch ras-commander `export_mesh_pmtiles` to columnar properties.
- **ras2cng integration (not started):** `cog.py` -> terrio area_matched + validate_cog;
  `raster_tiles.py` -> terrio streaming PMTiles + shaded relief; `project.py` -> fast-hdf
  GeoParquet writer + bbox struct + Hilbert fix; `vector_tiles.py` -> `freestile_layers`,
  `gzip_level`, optional `generate_ids=False`; `fast_map.py` -> arrival/duration/percent.
  Open question: merge fork PRs first, or build against branches.
- Cleanup: `G:\RustDev\target\D` ~26 GB; worktrees terrio-A2, terrio-cog-validate, fast-hdf-35.

## LERC: terrio vs GDAL (measured 2026-10-27)

terrio encodes LERC with the third-party pure-Rust `lerc-writer` 0.5 (i-norden/lerc-rust), not
Esri's C++ library that GDAL uses. Bald Eagle Depth (Max), 1,345,691 wet px, MAX_Z_ERROR=0.01,
512 tiles: both honour the bound (max error 0.01000 each, mean 0.0048 each). 8.1% of pixels
decode differently, by at most one quantization step (<= 0.02). The differences come in whole
8x8 micro-blocks (1,928 of 23,177 blocks): in those blocks lerc-writer stores the exact block
minimum as the offset, GDAL/Esri does not in 77% of them, so every pixel's step grid shifts.
Neither is wrong; the LERC format only guarantees the error bound. terrio's file is 10.6% larger
(1.76 vs 1.59 MB). ras2cng therefore keeps GDAL for the final LERC encode (smaller files,
byte-identical to existing artifacts). Possible upstream item for lerc-writer: compression gap
vs Esri, not value parity. Not filed (needs user approval).

## Decisions (user, 2026-10-27, later)

- **ras2cng goes full terrio**, including LERC: both encoders hold MAX_Z_ERROR, so use terrio and
  document the <= one-step difference from GDAL. GDAL stays only as the stock-terrio fallback.
- **Upstream:** review each external fork PR (terrio -> opengeos/terrio, freestiler ->
  walkerke/freestiler, lerc-writer size gap -> i-norden/lerc-rust) with a QAQC pass against that
  repo's contribution guidelines, explain it to the user, and submit only after per-PR approval.
  **Send PRs with fixes, never issues.** Upstream-ready branches are `upstream/<slug>` on the
  gpt-cmdr forks; PR bodies are in `G:\RustDev\upstream\<repo>\`.
- **LERC GDAL-parity option:** add an opt-in lerc-writer mode that reproduces GDAL's LERC output
  (the algorithm in the C library GDAL links, `Lerc2.cpp`), framed as GDAL parity, not Esri
  parity. Then expose it in terrio as a creation option. The default stays best-compression.
