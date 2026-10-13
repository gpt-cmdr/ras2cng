# Vector tiler evaluation: tippecanoe vs freestiler vs tylertoo (2026-10-12)

Question: can `freestiler` (or `tylertoo`) replace or sit beside tippecanoe in
`ras2cng/maplibre.py::_run_tippecanoe` and `hms2cng/pmtiles.py::generate_vector_tiles`?

Harness and raw results: `feature_dev_notes/vector_tiler_bench/` (`results.json`).

## Setup

- Host: CLB04 (Windows 11 VM, 8 cores, no nested virtualization → no WSL2/Docker).
- tippecanoe 2.79.0 built natively under MSYS2 (Cygwin runtime): `pacman -S gcc make
  libsqlite-devel zlib-devel`, then `sed -i 's/-std=c++17/-std=gnu++17/' Makefile && make`.
  Unit tests pass. Run with the five `msys-*.dll` beside the exe. Direct `.pmtiles` output
  fails on this build; `.mbtiles` + `pmtiles convert` (go-pmtiles 1.31.2) works, which is
  the path ras2cng already uses.
- tippecanoe runs went through ras2cng's real `_write_ndgeojson` + `_run_tippecanoe`.
- freestiler 0.3.0 (`freestile(gdf_or_dict)`), tylertoo 0.8.0 (`overview` + `export_pmtiles`
  with thinning, visibility, simplification and density drop all disabled).
- Data: BaldEagleCrkMulti2D `g01` → mesh_cells (87,039, plus synthetic float `max_depth`),
  mesh_faces (177,301 lines), 5 sparse engineering layers in one tileset, and a
  1,044,468-cell synthetic mesh (real mesh tiled 4×3).

## Metrics

Completeness = distinct ids present per zoom / source rows. Gap = mesh footprint area
(inset 2 px) not covered by decoded cells, per tile. Id integrity = decoded cell's
representative point inside the source cell with that id (1-in-25 sample).

## Results

| Dataset | Tiler | Time (s) | Peak MB | Archive MB | Low-zoom completeness | Gap % | Invalid polys |
|---|---|---|---|---|---|---|---|
| 87k cells z10–15 | tippecanoe unbounded | 31.1 | 414 | 9.1 | 100% | 0.002 | 0 |
| | tippecanoe bounded | 34.9 | 419 | 7.6 | z10 33%, z11 54% | 66 / 45 | 0 |
| | freestiler | 2.4 | 312 | 13.7 | 100% | 0 | 2 (z10–12) |
| | freestiler drop_rate=2.5 | 1.8 | 295 | 11.9 | **z10 no tiles** | — | 2 |
| | tylertoo verbatim | 1.5 | 195 | 11.2 | 100% | 0 | 0 |
| 177k faces z12–15 | tippecanoe | 23.7 | 455 | 7.0 | 100% | | |
| | freestiler | 2.5 | 366 | 9.7 | 100% | | |
| | freestiler drop_rate=2.5 | 2.1 | 325 | 5.6 | z12 4%, z13 6% | | |
| | tylertoo verbatim | 1.4 | 283 | 8.0 | 99.98% | | |
| 1.04M cells z10–14 | tippecanoe unbounded | 380 | 1286 | 85.1 | 100% | 0.002 | 0 |
| | tippecanoe bounded | 444 | 1217 | 63.1 | z10 17%, z11 40% | 83 / 59 | 0 |
| | freestiler | 18.1 | 2182 | 128.6 | 100% | 0 | 48 at z10 → 0 at z14 |
| | tylertoo verbatim | 12.9 | 1062 | 105.1 | 100% | 0 | 0 |

Id integrity: 0 mismatches in every run. Sparse multi-layer tileset: both tippecanoe
`-L` and freestiler dict input produce all 5 named layers, z8–14, ~40 KB.

Time split for 1M cells, tippecanoe path: 68 s `_write_ndgeojson`, 304 s tippecanoe,
<1 s `pmtiles convert`. **Caveat:** this tippecanoe is a Cygwin build; native Linux
tippecanoe is expected to be materially faster, so the speed gap is an upper bound.

hms2cng (river_bend, castro): tippecanoe `-zg` and freestiler at the same zoom range
give identical feature counts and identical position error. Separately, `-zg` picks
max zoom 2–5 for these sparse basins, quantizing points by 100–670 m — an hms2cng
issue independent of tiler choice.

## Findings

1. **freestiler matches tippecanoe "unbounded" on correctness**: 100% completeness,
   no gaps between cells (pixel-grid snapping keeps shared vertices coincident), ids intact,
   multi-layer works. 8–20× faster here, no CLI/WSL, no ndgeojson/mbtiles intermediates.
2. **freestiler archives are 40–50% larger** (13.7 vs 9.1 MB; 128.6 vs 85.1 MB) and its
   largest tiles 20–30% bigger. It snaps but does not simplify; tippecanoe applies
   tile-resolution simplification. This counts against freestiler for CDN-served results.
3. **freestiler emits a few invalid polygons at low zoom** (cells collapsing on the pixel
   grid): 48 of ~47k sampled at z10 for 1M cells, 0 by z14. MapLibre tolerates them;
   a validating consumer would not.
4. **freestiler has no equivalent of the bounded policy.** `drop_rate` drops by bbox area,
   not tile size, so a uniform mesh disappears entirely at low zoom (z10 empty for cells;
   4% of faces at z12). It must not be used for mesh. Bounded tilesets stay on tippecanoe.
5. **freestiler peak memory is higher at scale** (2.2 GB vs 1.3 GB for 1M cells, GeoDataFrame
   input). `freestile_file()` on the GeoParquet is untested and may be lower.
6. **No auto max-zoom in freestiler** (hms2cng uses `-zg`); a zoom heuristic would be needed.
7. **tylertoo verbatim** was the fastest and leanest, with no invalid polygons, but it is
   single-layer from Python, requires lon/lat GeoParquet input, its defaults thin coverage
   data heavily, `convert()` fails on Windows, and it is self-described as not production-ready.

## Recommendation

Add freestiler as an opt-in backend (`RAS2CNG_VECTOR_TILER=freestiler`) for **unbounded**
tilesets only, with tippecanoe still the default and the only bounded path. Before making
freestiler a default anywhere:

- rerun the 1M case against native Linux tippecanoe;
- test `freestile_file()` memory;
- decide whether ~45% larger archives are acceptable for published results tilesets;
- do a visual QA pass in MapLibre at z10–12, where invalid polygons cluster.

## Visual comparison

`vector_tiler_bench/zoom_stack.png` renders the same view in MapLibre at z10, z11, z12,
z14 and z16.5. From left to right the panels are tippecanoe unbounded, tippecanoe
bounded, freestiler, tylertoo verbatim and freestiler with `drop_rate=2.5`
(`viewer/index.html`, `serve.py` and `shoot.py` reproduce it).

- From z12 up, the five panels are identical.
- At z10–z11, `--drop-densest-as-needed` shows as horizontal striping.
- At z10, `drop_rate` draws nothing at all.
- At z11, `drop_rate` cuts white lines along the breaklines, where the smallest
  cells are.

## Outcome (ras2cng 0.12.0, hms2cng 0.3.0)

- `--drop-densest-as-needed` is removed from both repos, because it caused the
  striping seen in published ras2cng output.
- ras2cng starts the dense tilesets at the detail floor instead. This covers
  `geometry-detail` and the new `results-detail`.
- `RAS2CNG_VECTOR_TILER` adds freestiler as a backend. `auto` falls back to it when
  tippecanoe is absent.
- hms2cng uses freestiler by default and has an explicit z0–14 range.
