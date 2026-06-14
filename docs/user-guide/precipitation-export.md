# Precipitation Export

## Overview

HEC-RAS plan HDF files with gridded meteorology store precipitation under
`Event Conditions/Meteorology/Precipitation`. ras2cng can export those grids to
GeoTIFF rasters for each timestep and can also write cumulative-through-timestep
rasters.

This is a data extraction workflow. It does not render frames or assemble video.

## Command Line

```bash
# Export timestep and cumulative precipitation rasters
ras2cng precip model.p01.hdf ./precipitation/

# Export only selected timesteps. Tokens match timestamp labels first, then
# fall back to a zero-based index when no label matches.
ras2cng precip model.p01.hdf ./precipitation/ --timestamps 0,6,12
ras2cng precip model.p01.hdf ./precipitation/ --timestamps "01JAN2020 06:00:00"

# Export only cumulative rasters
ras2cng precip model.p01.hdf ./precipitation/ --no-incremental

# Require imported raster data instead of processed plan-HDF values
ras2cng precip model.u01.hdf ./precipitation/ --source imported

# Convert output values to inches (default is native, no conversion)
ras2cng precip model.u01.hdf ./precipitation/ --units in
```

## Timestamp Selection

`--timestamps` accepts a comma-separated list of timestamp labels or integer
indices. Each token is first matched against the actual timestamp labels stored
in the HDF (both the raw label and the filename-safe form). Only when a token
does not match any label is it interpreted as a zero-based integer index. This
lets purely-numeric timestamp labels be selected by label rather than being
treated as indices.

## Units

By default (`--units native`) raster values are written exactly as stored in the
HDF and tagged with the source units. Processed plan-HDF precipitation is
typically in inches; imported raster data is typically in millimeters.

Pass `--units in` or `--units mm` to convert values to a consistent unit:

| Conversion | Factor |
|---|---|
| mm to in | divide by 25.4 |
| in to mm | multiply by 25.4 |

The output GeoTIFF's `units` tag and band description are set to the requested
unit. NoData/NaN cells are preserved through the conversion. If the source units
already match the requested unit, values are passed through unchanged.

## HDF Sources

| Source | HDF dataset | Typical file | Notes |
|---|---|---|---|
| `processed` | `Precipitation/Values` | `*.p??.hdf` | Processed timestep precipitation amounts used by the computed plan |
| `imported` | `Precipitation/Imported Raster Data/Values` | `*.u??.hdf` or `*.p??.hdf` | Original imported raster time series; cumulative sources are differenced for timestep rasters |
| `auto` | processed, then imported | either | Default behavior |

GeoTIFF georeferencing is read from the HDF raster attributes:

- `Projection`
- `Raster Cellsize`
- `Raster Cols`
- `Raster Rows`
- `Raster Left`
- `Raster Top`
- `Units`

## Output Files

Output names include the HDF stem, raster type, zero-padded timestep index, and
timestamp:

```text
model_p01_precip_0000_20220617_090000.tif
model_p01_precip_cumulative_0000_20220617_090000.tif
```

Each GeoTIFF includes tags for the source HDF, source HDF dataset, timestamp,
precipitation units, and whether the raster is incremental or cumulative.

## Python API

```python
from pathlib import Path
from ras2cng.precipitation import (
    export_precipitation_rasters,
    list_precipitation_timestamps,
    read_precipitation_grid_info,
)

plan_hdf = Path("model.p01.hdf")

timestamps = list_precipitation_timestamps(plan_hdf)
info = read_precipitation_grid_info(plan_hdf)

result = export_precipitation_rasters(
    plan_hdf,
    Path("./precipitation"),
    timestamps=[0, 1, 2],
)

print(result.incremental)
print(result.cumulative)
```
