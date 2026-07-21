# mapping

Result raster generation for HEC-RAS projects using the RASMapper engine.

## Overview

The `mapping` module generates georeferenced raster files (GeoTIFF) from completed HEC-RAS plan results. With ras-commander 0.99.0 or newer it drives the canonical `RasMap.store_all_maps(mode="selected")` API, which deploys isolated `RasStoreMapHelper.exe` processes and preserves the correct water-surface render mode. ras-commander 0.98.2 remains supported through the serial compatibility path.

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
| `inundation_boundary` | `--inundation-boundary` | Off | Inundation Boundary (shapefile) |
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
