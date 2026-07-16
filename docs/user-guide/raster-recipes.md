# Controlled Raster Recipes

`ras2cng raster-calculate` creates analysis rasters from aligned numeric COGs. It accepts
only named recipes with typed inputs and parameters; arbitrary Python, GDAL expressions,
and user-supplied scripts are not evaluated.

## Available Recipes

| Recipe | Required inputs | Output |
| --- | --- | --- |
| `compare_wse` | `baseline`, `comparison` WSE | Comparison minus baseline WSE |
| `compare_depth` | `baseline`, `comparison` depth | Comparison minus baseline depth |
| `compare_velocity` | `baseline`, `comparison` velocity | Comparison minus baseline velocity |
| `depth_velocity` | Synchronized `depth`, `velocity` | Depth multiplied by velocity |
| `depth_velocity_squared` | Synchronized `depth`, `velocity` | Depth multiplied by velocity squared |
| `hazard_class` | Synchronized `depth`, `velocity` | AIDR 2017 H1-H6 hazard class |
| `inundation_threshold` | `depth` | Below/at-or-above threshold mask |
| `terrain_mod_delta` | `original`, `modified` terrain | Modified minus original elevation |

Depth-velocity and hazard recipes require the same explicit profile or timestep for both
inputs. Independent `Max` surfaces are rejected because their maxima can occur at different
times. Input rasters must have exactly matching CRS, transform, dimensions, and pixel grid.
Compatible US customary and metric units are converted before calculation.

```bash
ras2cng raster-calculate depth_velocity output/dv.cog.tif \
  --input depth=maps/Depth_01JAN2026_120000.cog.tif \
  --input velocity=maps/Velocity_01JAN2026_120000.cog.tif \
  --input-unit depth=ft --input-unit velocity=ft/s \
  --plan p03 --profile "01JAN2026 12:00:00"
```

Processing is windowed and writes a staged tiled GeoTIFF before atomically replacing the
final COG. Nodata remains transparent. The adjacent `.provenance.json` records the recipe
version, input roles, units, parameters, plan/profile, and output metadata. Full SHA-256
hashes are opt-in because hashing adds another complete read of every large raster.

## Viewer Publication

Publish a recipe output under its plan's `Calculated Layers` branch:

```bash
ras2cng maplibre-calculated-map output/dv.cog.tif VIEWER_DIR \
  --plan p03 --recipe depth_velocity \
  --profile "01JAN2026 12:00:00" --geometry g03 \
  --source-cog ../archive/calculated/p03/dv.cog.tif
```

The numeric COG remains authoritative. The PMTiles derivative is a fast display layer.
Calculated-layer provenance distinguishes ras2cng arithmetic from the RASMapper/RasProcess
interpolation used to create its source surfaces. Hazard and threshold outputs use fixed
categorical legends and cannot use a current-view stretch.
