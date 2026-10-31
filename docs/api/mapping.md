# mapping

Result raster generation for HEC-RAS projects using the RASMapper engine.

## Overview

The `mapping` module generates georeferenced raster files (GeoTIFF) from completed HEC-RAS plan results. It uses a private compatibility adapter around ras-commander's stored-map APIs so profile selection is explicit and validated before map generation mutates the project.

`Max` and `Min` remain explicit summary selections. An exact named steady profile is resolved from the plan HDF and sent through `RasMap.store_all_maps(mode="steady_profiles")`; a missing or duplicate name raises an error instead of silently producing `Max`. Unsteady timestamps are likewise validated exactly. Whole-simulation products such as arrival time are generated separately because they do not have a profile dimension.

On Linux, RasProcess.exe runs under Wine. See the [Linux/Wine Setup](../user-guide/linux-wine-setup.md) guide.

## Supported Map Types

| Map Type | CLI Flag | Default | Description |
|----------|----------|---------|-------------|
| `wse` | `--wse/--no-wse` | On | Water Surface Elevation |
| `depth` | `--depth/--no-depth` | On | Depth |
| `velocity` | `--velocity/--no-velocity` | On | Velocity |
| `froude` | `--froude` | Off | Froude Number |
| `shear_stress` | `--shear-stress` | Off | Shear Stress |
| `depth_x_velocity` | `--dv` | Off | Depth x Velocity |
| `depth_x_velocity_sq` | `--dv-sq` | Off | Depth x Velocity² |
| `inundation_boundary` | `--inundation-boundary` | Off | Inundation Boundary (GeoJSON from the default fast-map engine, [much faster](#inundation-boundaries-in-rust-recommended-with-fast-map); shapefile from RAS Mapper) |
| `arrival_time` | `--arrival-time` | Off | Arrival Time (hours, whole-simulation) |
| `duration` | `--duration` | Off | Inundation Duration (hours, whole-simulation) |
| `percent_inundated` | `--percent-inundated` | Off | Percent Time Inundated (whole-simulation) |

`--recession` is accepted for compatibility but ignored with a warning —
RasMapperLib has no recession map type, and only RasMapperLib-native outputs
are produced.

### Whole-simulation map types

`arrival_time`, `duration`, and `percent_inundated` are computed over the
entire simulation — the `--profile` option does not apply to them. Their
filenames carry the `--arrival-depth` wet/dry threshold instead of the profile
name, e.g. `Arrival Time (0.1ft hrs).tif`. They work with any ras-commander
version: newer versions generate them natively via `store_maps()`; on older
versions ras2cng pre-injects the stored-map entries into the `.rasmap`
(restored afterwards) so the same StoreAllMaps run produces them.

Generating these types causes RasMapperLib to build a `PostProcessing.hdf`
cache that can exceed the plan HDF in size; ras2cng deletes it from the output
directory unless `--keep-postprocessing` is passed.

## Local performance policy

The default `DEFAULT_LOCAL_MAP_PERFORMANCE` preset is tuned for local
processing on the 8-core, 31.8 GiB development workstation:

- memory-aware automatic helper selection (`max_workers=None`);
- enforced memory admission with an 8192 MiB / 25 percent reserve;
- one GDAL thread per helper to prevent nested oversubscription;
- a 64 MiB GDAL cache cap charged to every helper's memory estimate.

Only independent WSE, Depth, and Velocity products run concurrently. Large
terrain estimates and products that require shared ordered state automatically
use one helper. On the Spring River fixture, the measured estimate is about
11.2 GiB per helper, so this 31.8 GiB machine remains serial; a machine with
more available memory can admit two or three map helpers without changing the
call.

Use `--map-workers 1` for a controlled serial comparison, or set the helper
ceiling, reserve, and cache explicitly:

```bash
ras2cng map model.prj ./maps \
  --map-workers 2 \
  --map-reserve-memory-mb 8192 \
  --map-gdal-cache-mb 64
```

Python callers can provide the full typed policy without adding a second map
function:

```python
from ras_commander import StoreMapPerformanceOptions
from ras2cng import generate_result_maps

results = generate_result_maps(
    "model.prj",
    "maps",
    performance=StoreMapPerformanceOptions(
        max_workers=None,
        reserve_memory_mb=8192,
        gdal_cachemax_mb=64,
    ),
)
```

## Render Mode

The `--render-mode` option controls how the water surface is rendered to raster grids. This is critical for pixel-perfect output — HEC-RAS 6.x's `RasProcess.exe` ignores the render mode from the `.rasmap` file, so ras-commander uses `RasStoreMapHelper.exe` to set the mode explicitly before generating maps.

| Mode | Flag | Description |
|------|------|-------------|
| `horizontal` | `--render-mode horizontal` | Flat water surface within each mesh cell (default) |
| `sloping` | `--render-mode sloping` | Interpolated sloping water surface |
| `slopingPretty` | `--render-mode slopingPretty` | Sloping with depth-weighted face reduction (HEC-RAS 6.4+) |

If `--render-mode` is not specified, the mode is read from the project's `.rasmap` file (defaults to `horizontal` if not set).

```bash
# Generate maps with sloping render mode
ras2cng map /path/to/project /output/maps --render-mode sloping

# Archive with slopingPretty render mode (requires HEC-RAS 6.4+)
ras2cng archive /path/to/project ./archive/ --results --map --render-mode slopingPretty
```

## Mapping engine: RAS Mapper or fast-map

**fast-map is the default engine.** It maps selected products through the
Rust `ras-commander-fast-map` engine and hands everything else to RAS Mapper
(`RasStoreMapHelper.exe`) per product group, printing the reason;
`--engine rasmapper` (or `RAS2CNG_MAP_ENGINE=rasmapper`) renders every
product through RAS Mapper. The water-surface render mode comes from the
project's `.rasmap`, as RAS Mapper takes it (`--render-mode` overrides it);
a project with no stored mode goes to RAS Mapper, which applies its own
default. fast-map is built for **FIM library
creation on pure 2D models**, not for general use across all model types:
every fast-map product is qualified against RAS Mapper output (pixel
conformance against native HEC-RAS 7.0.1 stored maps on the named models),
and everything else falls back to RAS Mapper. ras-commander installs the
engine through `ras-commander[full]` on Windows x64 and Linux x64
(`ras2cng[fast-map]` requests it explicitly). The mapping arithmetic needs no
GDAL, and with the source-terrain path below no HEC-RAS either.

```bash
# Default: Rust WSE/Depth, arrival time and percent inundated; the rest via RAS Mapper
ras2cng map /path/to/project /output/maps \
    --arrival-time --percent-inundated --arrival-depth 0.5 --ras-version 7.0.1

# Everything through RAS Mapper
ras2cng map /path/to/project /output/maps --engine rasmapper

# Reuse a terrain export across runs
ras2cng map /path/to/project /output/maps --engine fast-map \
    --fast-map-terrain /output/maps/_fast_map_terrain/Terrain50.tif

# Fail instead of falling back (qualification runs)
ras2cng map /path/to/project /output/maps --engine fast-map --fast-map-strict

# Map a plan with storage areas in Rust (opt-in; otherwise RAS Mapper maps it)
ras2cng map /path/to/project /output/maps --fast-map-storage-areas
```

`RAS2CNG_MAP_ENGINE` selects the engine everywhere, including
`ras2cng archive --map`. Scope, engine and render mode are checked from the
plan HDF and `.rasmap` before any terrain work, so an out-of-scope plan goes
to RAS Mapper without a terrain export.

| Product | fast-map | Qualification against RAS Mapper |
|---|---|---|
| WSE, Depth (Max) | yes | identical float32 values and masks on five models (1.2–3.6× faster for horizontal and plain sloping, 1.8–7.3× for depth-weighted sloping faces; [evidence](https://rascommander.info/ras/development/fim-mapping-performance/)); Davis p02 and Bald Eagle bridges p05 again on the source-terrain path |
| WSE, Depth with storage areas | **opt-in** (`--fast-map-storage-areas`; slower than RAS Mapper today), as RAS Mapper level pools, when the engine advertises `storage-area-level-pool/1` | identical on Bald Eagle p07 and p04 in every render mode (466,912 / 461,639 storage-area px); p07 Max sloping through `ras2cng map`: 0 mask and 0 bit differences on 1,345,691 px |
| Arrival time, percent time inundated | yes (`--no-fast-map-whole-simulation` to opt out) | identical on Davis p02 (4,879 px) and bridges p05 (959,043 px) at 0.5 ft, horizontal and plain sloping; with a storage area, p07 (1,333,183 px) |
| Duration | yes, when the engine advertises RAS Mapper's recession rule (`rasmapper-backward-window-recession/1`); not with storage areas | identical on Davis p02 and bridges p05 at 0.5 ft |
| Velocity (Max) | yes in plain sloping and horizontal when the engine advertises `velocity_depth`; slopingPretty opt-in (`--fast-map-velocity`); `--no-fast-map-velocity` to opt out; not with storage areas | bridges p05: 0 value and 0 mask differences in sloping (973,902 px) and horizontal (973,532 px). slopingPretty: identical values on every common pixel, but its mask follows the slopingPretty depth, which differs on bridges Max (75 + 28 px). Older engines: opt-in, plain sloping only |
| D × V, D × V², Froude (Max) | yes in plain sloping and horizontal (`--no-fast-map-derived` to opt out); not with storage areas or slopingPretty | per-time product, pixelwise Max over every output time, as RAS Mapper computes it: 0 / 0 on bridges p05 over 433 times (973,956 px sloping); Davis within 2 ulp on ≤ 5 px |
| Shear stress | no | not implemented in the engine |
| Inundation boundary | yes (see below); not with storage areas | deterministic contours, not vector parity |

A plan is sent to RAS Mapper instead when:

| Condition | Why |
|---|---|
| 1D river reaches or cross sections | Outside the engine's method (`RasFim.validate_2d_model`) |
| Storage areas, unless `--fast-map-storage-areas` (`fast_map_storage_areas=True`, `RAS2CNG_FAST_MAP_STORAGE_AREAS=1`) is given | Not a default until it is faster than RAS Mapper: Bald Eagle p07 WSE + Depth took 18.7 s in Rust against 12.2 s. Opt in to map storage-area plans without HEC-RAS (Linux, FIM batches) |
| Storage areas, opted in, with an engine that does not advertise `storage-area-level-pool/1` | It would leave the storage-area pixels blank |
| A profile other than `Max` (per-profile products only; arrival, duration and percent are whole-simulation) | Timestep mapping is not wired through ras2cng yet. Decided before any terrain work |
| No usable terrain | See below |
| Engine missing or failed | No arithmetic fallback inside fast-map |

**Terrain.** With ras-commander's `RasFim.top_priority_terrain_source`
(ras-commander #724, fast-map #11), ras2cng maps on the terrain's unmodified
**priority-1 source TIFF**, with its terrain-HDF footprint mask, and skips the
RAS Mapper terrain export. Inside that footprint the source TIFF is the
effective terrain, and the engine refuses when a wet cell would draw outside
it. ras2cng then:

1. retries on the source's CRS-canonical copy, only when the refusal was
   about the CRS (Davis);
2. otherwise builds the **native effective terrain** once per run, when the
   engine advertises `rasmapper-effective-terrain/1` and RasFim has
   `build_effective_terrain`: RAS Mapper's multi-source mosaic (stitch TIN
   first, then each source in priority order) rebuilt in Rust from the
   source TIFFs, their level-0 masks and the stitch TIN, without HEC-RAS. On
   Bald Eagle Terrain50 the valid mask is identical on 197,179,058 px and 8
   values differ, all in stitch-TIN corners where RAS Mapper's own exports
   disagree (max 1.53 ft); it took 7.0 s and 1.4 GB, against about 190 s
   for the export. Terrains with modifications are refused;
3. otherwise exports the plan's terrain once per run through RAS Mapper
   (`RasTerrain.export_rasmapper_terrain`, HEC-RAS 6.4.1/6.5/6.6/7.0.1/7.1;
   pass `--ras-version`).

`--fast-map-terrain` supplies a float32 export (with modifications) instead,
`--no-fast-map-effective-terrain` skips step 2, and
`--no-fast-map-source-terrain` skips the source TIFF.

**Terrains with modifications always go to RAS Mapper.** Before any of the
steps above, ras2cng reads the terrain HDF's `/Modifications` group and the
`.rasmap`'s modification layers (`ras2cng.terrain.terrain_modifications`).
If it finds any, or cannot read the terrain HDF, steps 1 and 2 are skipped
and RAS Mapper's export builds the terrain (step 3), and the inundation
boundary is drawn by RAS Mapper. Rust terrain operations run only on
unmodified terrains, even with an engine build that accepts modifications:
that work is parked because it does not yet reproduce RAS Mapper on real
modified terrains. `map-hdf` with a
single raw `--terrain` TIFF passes that TIFF directly.

Measured on a shared, loaded host:

| Run | Time | Terrain |
|---|---|---|
| Bald Eagle bridges p05, WSE + Depth | 4.5 s end to end, no HEC-RAS | priority-1 source TIFF |
| bridges p05, WSE, Depth, velocity, arrival, percent | 16.6 s | reused export |
| bridges p05, same five, nothing supplied | 181 s (≈160 s is the HEC-RAS export; arrival/percent were refused on the source) | source, then export |
| RAS Mapper, bridges p05 | ≈215 s for the seven Max products; ≈26 s for arrival/percent | |
| Davis p02, all five | 5.4 s, no HEC-RAS | CRS-canonical source copy |
| bridges p05 sloping, nine products (adds velocity, D × V, D × V², Froude, duration) | 251 s / 1.5 GB with one output time in flight in the derived pass; the derived pass alone is now 141 s / 2.6 GB | source; effective terrain for arrival/duration/percent |
| RAS Mapper, the same nine | 235 s / 2.4 GB; derived only 206 s / 2.3 GB; the other six 34 s | |
| fast-map, the other six | 25 s / 1.5 GB | source and effective |
| Bald Eagle p07 (storage area), WSE + Depth | 18.7 s / 1.5 GB (RAS Mapper: 12.2 s / 1.2 GB) | native effective terrain (the source TIFF is refused: 28 storage-area pixels outside it) |
| p07, arrival + percent | 31 s (RAS Mapper: 82 s) | native effective terrain |

Every fast-map product in the bridges and p07 rows was compared with RAS
Mapper 7.0.1 stored maps: 0 mask and 0 bit differences (bridges: 18 products
over sloping and horizontal; p07: 1,345,691 and 1,333,183 px).

**Output.** Products are named like RAS Mapper's (`WSE (Max).tif`,
`Depth (Max).tif`, `Velocity (Max).tif`, `D _ V (Max).tif`,
`D _ V^2 (Max).tif`, `Froude (Max).tif`, `Arrival Time (0.5ft hrs).tif`,
`Duration (0.5ft hrs).tif`, `Percent Time Inundated (0.5ft).tif`) on the
terrain's pixel grid, with NaN nodata, next to `fast_map_manifest.json`,
`fast_map_velocity_manifest.json`, `fast_map_derived_manifest.json` and
`fast_map_whole_simulation_manifest.json` (engine version, method, terrain,
storage areas, timings). Arrival time is in hours since the simulation start.
`MapResult.engines` records which engine produced each map type,
`MapResult.fast_map_skipped` why a map type went to RAS Mapper under the
fast-map engine (for example storage areas without the opt-in), and
`MapResult.fast_map_terrain` which terrain fast-map used (`source`,
`source-canonical`, `effective`, `export` or `supplied`). Velocity is its own
engine call, so a velocity refusal never costs WSE/Depth, and WSE/Depth keep
the native GeoTIFF writer; in horizontal mode it is rendered plain sloping
and clipped with the horizontal depth, as RAS Mapper does. `--min-depth`, `--wgs84` and
`--cog` apply unchanged. Other options: `--fast-map-threads` (default CPUs − 1)
and `--fast-map-memory-mib` (planned working memory; default a quarter of
RAM, 1–16 GiB).

**Invalid cell polygons.** HEC-RAS can compute 2D cells whose face polylines
cross. With ras-commander 0.110.2 or newer, fast-map maps those meshes under
an explicit policy instead of refusing them:

| `--fast-map-invalid-cells` | Pixel ownership of an invalid cell |
|---|---|
| `repair` (engine default) | polygonize the cell's faces, remove area owned by valid cells, keep the largest part |
| `hull` | the face-point convex hull, with the same overlap removal |
| `omit` | the cell owns no pixels |
| `raise` | refuse; the products fall back to RAS Mapper (`--fast-map-strict` raises) |

Hydraulic connectivity and interpolation always use the complete HDF
topology; only pixel ownership changes. RAS Mapper's own rendering of such
cells has not been qualified against `repair`. Every affected cell (mesh,
cell id, reason, action, original and mapped area) is recorded as
`fim-invalid-cells/1` in `MapResult.invalid_cells`, `fast_map_manifest.json`
and `fast_map_whole_simulation_manifest.json`, and a non-zero count is printed.

**GeoTIFF delivery.** With ras-commander-fast-map 0.11 the engine writes the
sloping WSE/Depth GeoTIFFs itself (terrio, LZW) instead of handing float32
arrays back to Python for rasterio. `--fast-map-writer auto` (default) uses
it when the engine supports it and velocity is not requested;
`native` requires it and `rasterio` keeps the previous path
(`RAS2CNG_FAST_MAP_WRITER` sets the default). On Bald Eagle bridges p05 Max
the two writers gave identical pixels and the same LZW strip layout; native
files were about 4% smaller and the engine call used about 18% less peak
memory. `--cog` converts either through the shared COG policy with identical
results. `MapResult.fast_map_writer` records the writer.

### Inundation boundaries in Rust (recommended with fast-map)

RAS Mapper's inundation-boundary export is very slow. With fast-map (the default engine),
`--inundation-boundary` is mapped by the Rust engine
(`RasFim.map_inundation_boundaries`) whenever the plan is in scope. In
ras-commander's Davis pipes/conduits comparison, the Rust batch for Max plus one
timestep took **3.5 s against 85 s** for the two native RAS Mapper calls. It is
not the default engine, but it is the recommended way to produce boundaries for
pure 2D models.

```bash
# Rust WSE/Depth and boundary; velocity still through RAS Mapper
ras2cng map /path/to/project /output/maps --engine fast-map --inundation-boundary

# Explicit single-source terrain HDF (its TIFF is found beside it)
ras2cng map /path/to/project /output/maps --engine fast-map --inundation-boundary \
    --fast-map-terrain-hdf /path/to/project/Terrain/Terrain.hdf

# Keep boundaries on RAS Mapper while still using Rust rasters
ras2cng map /path/to/project /output/maps --engine fast-map --inundation-boundary \
    --no-fast-map-boundary
```

**It is a different method, not a copy of RAS Mapper's polygons.** The Rust
engine traces deterministic zero-depth contours
(`deterministic-zero-depth-contours/1`): the same inputs always give
byte-identical GeoJSON, on Windows and Linux and at any worker count. RAS
Mapper's own boundary polygons are order-dependent and can differ between
identical exports, so the two are close but not equal. On Davis Max the Rust
contour had 66 holes and 47,426 vertices, against RAS Mapper's 65 and 47,396.
Almost all of the 7,423 ft² difference was one hole that RAS Mapper filled.
The Depth/WSE rasters are unaffected; they still match RAS Mapper bit for bit.

**Extra scope for boundaries**, on top of the pure-2D rules above (each falls
back to RAS Mapper on its own, so the rasters can still use Rust):

| Condition | Why |
|---|---|
| Terrain with more than one source TIFF | The method needs the original terrain HDF's masks, perimeters and stitch TIN for a single source. The flattened raster export cannot stand in for them |
| Terrain modifications | Refused by ras2cng before the engine runs (`terrain_modifications`): modified terrains always go to RAS Mapper |
| `slopingPretty` render mode | Contours support horizontal and plain sloping only |
| ras-commander without `RasFim.map_inundation_boundaries` | Needs ras-commander 0.113.0 |
| Storage areas | The contour method draws 2D cells only; the level pool would be missing |
| A terrain CRS the engine and rasterio read differently (Davis), without `--fast-map-terrain-crs-hash` | The engine cannot name an EPSG code for it; ras2cng never guesses the identity (see below) |

**User-supplied terrain CRS.** When the engine cannot name the terrain's EPSG
code and reads its WKT differently from rasterio (Davis), the boundary falls
back to RAS Mapper, and the message prints the engine's WKT hash, for example:

```
engine terrain CRS differs from rasterio CRS EPSG:2871; the engine reports
crs_wkt_fnv1a64=e446f7bd6bdb4d7f for Terrain (1).Davis_terrain2.tif ...
```

Check the CRS first (`ras2fim-batch terrain-info <terrain.tif>` prints the
engine's WKT and `crs_wkt_fnv1a64`), then pass the hash:

```bash
ras2cng map /path/to/Davis /output/maps --inundation-boundary \
    --fast-map-terrain-crs-hash e446f7bd6bdb4d7f
```

The Python API takes `fast_map_terrain_crs_fnv1a64=`. It applies to the
boundary terrain only, is used only when the engine reports no EPSG code, and
a value that does not equal the engine's hash is refused. The boundary
manifest records it as `ras2cng.terrain_crs_fnv1a64`.

**Output.** `Inundation Boundary (Max).geojson`: a MultiPolygon with holes, in
the model CRS. RAS Mapper writes a shapefile. It sits next to
`fast_map_boundary_manifest.json`, which records the method, polygon/hole/vertex
counts, area, terrain and timings. A plan with no flooded area writes no
boundary.

## Post-Processing Options

- **Minimum depth threshold** (`--min-depth`): Set pixels below a depth threshold to NoData
- **WGS84 reprojection** (`--wgs84`): Reproject output rasters to EPSG:4326 using rasterio
- **Cloud Optimized GeoTIFF** (`--cog`): Convert output to COG using `gdal_translate`

## API Reference

::: ras2cng.mapping.MapResult
    options:
      show_source: true

::: ras2cng.mapping.generate_result_maps
    options:
      show_source: true

::: ras2cng.mapping.DEFAULT_LOCAL_MAP_PERFORMANCE
    options:
      show_source: true

::: ras2cng.mapping.MAP_TYPE_VARIABLES
    options:
      show_source: true
