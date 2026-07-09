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
