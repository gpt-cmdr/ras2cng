# mapping

Result raster generation for HEC-RAS projects using RasProcess.exe.

## Overview

The `mapping` module generates georeferenced raster files (GeoTIFF) from completed HEC-RAS plan results. It wraps `RasProcess.store_maps()` from ras-commander, which invokes `RasProcess.exe StoreAllMaps` to render hydraulic variables to raster grids.

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
| `arrival_time` | `--arrival-time` | Off | Arrival Time |
| `duration` | `--duration` | Off | Duration |
| `recession` | `--recession` | Off | Recession |

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
