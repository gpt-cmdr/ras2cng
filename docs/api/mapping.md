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
| `inundation_boundary` | `--inundation-boundary` | Off | Native RASMapper Inundation Boundary, or an explicitly requested raster-derived Calculated Layer |
| `arrival_time` | `--arrival-time` | Off | Arrival Time (hours, whole-simulation) |
| `duration` | `--duration` | Off | Inundation Duration (hours, whole-simulation) |
| `percent_inundated` | `--percent-inundated` | Off | Percent Time Inundated (whole-simulation) |

`--recession` is accepted for compatibility but ignored with a warning —
RasMapperLib has no recession map type, and only RasMapperLib-native outputs
are produced.

## Inundation Boundary Authority

`--inundation-boundary` uses RASMapper's native Stored Polygon by default. This is the
preferred output because RASMapper remains the derivation authority:

```bash
ras2cng map PROJECT OUTPUT --inundation-boundary
```

Some large projects cannot produce that polygon reliably within the available memory.
Use `--boundary-method depth-raster` only as an explicit fallback. It requires the Depth
Stored Map and creates a ras2cng **Calculated Layer**, not a native RASMapper Stored Map:

```bash
ras2cng map PROJECT OUTPUT --inundation-boundary \
  --boundary-method depth-raster --boundary-threshold 0 \
  --boundary-resolution 4 --boundary-max-edges 5000000
```

The derived path builds the wet mask in bounded windows, applies the strict comparison
`depth > threshold`, excludes masked, nodata, NaN, and infinite samples, and uses
4-connected polygonization. The edge count is checked before polygonization. If the
fixed 5,000,000-edge guard is exceeded, retry at an even multiple of the native grid
resolution. `--boundary-resolution` may only coarsen the source and uses maximum
resampling so a wet source cell remains represented; it never upsamples.

Successful derivation publishes one atomic six-file family:

```text
Inundation Boundary (PROFILE).raster-derived.shp
Inundation Boundary (PROFILE).raster-derived.shx
Inundation Boundary (PROFILE).raster-derived.dbf
Inundation Boundary (PROFILE).raster-derived.prj
Inundation Boundary (PROFILE).raster-derived.cpg
Inundation Boundary (PROFILE).raster-derived.provenance.json
```

The provenance records the source and output resolutions, threshold, scale/offset,
nodata handling, connectivity, edge count, and both authorities. Do not rename a derived
family to look native, publish both native and derived boundaries for one plan, or raise
the edge cap merely to force polygonization; the importer rejects partial or ambiguous
families.

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
