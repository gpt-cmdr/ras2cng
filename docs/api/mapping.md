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
| `inundation_boundary` | `--inundation-boundary` | Off | Inundation Boundary (shapefile; GeoJSON with `--engine fast-map`, [much faster](#inundation-boundaries-in-rust-recommended-with-fast-map)) |
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

By default every map type is rendered by RAS Mapper (`RasStoreMapHelper.exe`).
For **pure 2D models**, `--engine fast-map` maps WSE and Depth through the
Rust `ras-commander-fast-map` engine instead. ras-commander 0.110.1 installs it
through `ras-commander[full]` on Windows x64 and Linux x64 (`ras2cng[fast-map]`
requests it explicitly). The mapping arithmetic needs no HEC-RAS and no GDAL.

```bash
# Rust WSE/Depth; velocity etc. still go through RAS Mapper
ras2cng map /path/to/project /output/maps --engine fast-map --ras-version 7.0.1

# Reuse a terrain export across runs (skips the per-run RAS Mapper export)
ras2cng map /path/to/project /output/maps --engine fast-map \
    --fast-map-terrain /output/maps/_fast_map_terrain/Terrain50.tif

# Fail instead of falling back (qualification runs)
ras2cng map /path/to/project /output/maps --engine fast-map --fast-map-strict
```

`RAS2CNG_MAP_ENGINE=fast-map` selects it everywhere, including
`ras2cng archive --map`.

**Why only pure 2D.** On five models, ras-commander compared fast-map with
native RAS Mapper 7.0.1. It found identical float32 values and validity masks,
and measured 1.2–3.6× speed-ups for horizontal and plain sloping and 1.8–7.3×
for depth-weighted sloping faces. The largest gains were on 340k–420k-cell eBFE
meshes ([evidence](https://rascommander.info/ras/development/fim-mapping-performance/)).
That method covers 2D cells only. A plan is sent to RAS Mapper instead when:

| Condition | Why |
|---|---|
| 1D river reaches or cross sections | Outside the engine's method (`RasFim.validate_2d_model`) |
| Storage areas | fast-map does not render storage-area surfaces. On a Bald Eagle dam-break plan, every 2D-mesh pixel matched RAS Mapper bit for bit, but the 466,912 storage-area pixels were blank |
| A profile other than `Max` | Timestep mapping is not wired through ras2cng yet |
| No usable terrain | Needs one float32 GeoTIFF of the registered terrain with modifications |
| Engine missing or failed | No arithmetic fallback inside fast-map |

**Terrain.** fast-map needs the exact registered terrain as one float32
GeoTIFF. Without `--fast-map-terrain`, ras2cng exports the plan's terrain once
per run through RAS Mapper (`RasTerrain.export_rasmapper_terrain`, HEC-RAS
6.4.1/6.5/6.6/7.0.1/7.1; pass `--ras-version`). The export still needs HEC-RAS
and is not included in the published speed-ups. On a small model it can take
longer than the mapping. `map-hdf` with a single raw `--terrain` TIFF passes
that TIFF directly.

**Output.** Products are named like RAS Mapper's (`WSE (Max).tif`,
`Depth (Max).tif`) on the terrain's pixel grid, with NaN nodata, next to
`fast_map_manifest.json` (engine version, method, timings).
`MapResult.engines` records which engine produced each map type. `--min-depth`,
`--wgs84` and `--cog` apply unchanged. Other options: `--fast-map-threads`
(default CPUs − 1) and `--fast-map-memory-mib` (planned working memory; default
a quarter of RAM, 1–16 GiB).

### Inundation boundaries in Rust (recommended with fast-map)

RAS Mapper's inundation-boundary export is very slow. With `--engine fast-map`,
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
| Terrain modifications | Refused by the engine (it cannot infer modifications from a TIFF/HDF pair) |
| `slopingPretty` render mode | Contours support horizontal and plain sloping only |
| ras-commander without `RasFim.map_inundation_boundaries` | Needs ras-commander PR #709 and ras-commander-fast-map PR #5 (unreleased as of 2026-10-27) |

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
