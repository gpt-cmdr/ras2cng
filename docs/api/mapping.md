# mapping

Result raster generation for HEC-RAS projects using RasProcess.exe.

## Overview

The `mapping` module generates georeferenced raster files (GeoTIFF) from completed HEC-RAS plan results. It wraps `RasProcess.store_maps()` from ras-commander, which deploys `RasStoreMapHelper.exe` to set the correct water surface render mode via .NET reflection before calling `StoreAllMapsCommand`. This produces pixel-perfect rasters matching the RASMapper GUI output.

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
| `inundation_boundary` | `--inundation-boundary` | Off | Inundation Boundary |
| `arrival_time` | `--arrival-time` | Off | Arrival Time |
| `duration` | `--duration` | Off | Duration |
| `recession` | `--recession` | Off | Recession |

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

::: ras2cng.mapping.MAP_TYPE_VARIABLES
    options:
      show_source: true
