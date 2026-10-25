# Precipitation-indexed raster stacks

`ras2cng.rate_stack.write_rate_stack(sources, target, rate_k16=..., units=..., provenance=...)`
writes a new NetCDF4 raster stack. It leaves inputs unchanged. Sources map each quantity
to single-band float32 rasters on one projected north-up grid. Native CRS is preserved.
Quantities and units are `depth` and `wse` in ft, `velocity` in ft/s, and `hazard` dimensionless.
The caller defines the hazard categories. Negative depth, speed, and hazard values are refused.

The `ras2cng-rate-stack/v1` schema has dimensions `(rate_k16, y, x)`, an int32
`rate_k16` coordinate, and float64 `rate_in_per_h = 10**(rate_k16/16)`.
Keys are unique and strictly increasing. Each quantity is float32 with NaN fill and
shuffle/deflate chunks `(1, min(256,height), min(256,width))`. Pixel values are retained
without resampling or quantization. CRS WKT and GDAL geotransform are on `crs`.
Only explicit public string provenance is included; source paths are never exported.

The returned `ras2cng-rate-stack-ranges/v1` object is written by the caller as `ranges.json`.
It contains the NetCDF byte count and SHA-256, rate coordinates, CRS/geotransform,
and per-quantity dimensions, shape, chunk shape, dtype, units, fill, filters, and chunks.
Each chunk records its array origin, absolute byte offset, and compressed byte length.
An HTTP client requests those bytes, inflates them, reverses four-byte shuffle, and
decodes little-endian float32. Edge chunks retain the full chunk shape, including fill.
The file is immutable; regeneration requires a new object and index.
`rate_stack_index(path)` independently indexes an existing stack read-only.

`rasterize_cell_values(cells, values, template, target)` maps native cell scalars
onto the template grid by pixel center, without interpolation. Both CRS must agree.
This is suitable for explicit cell-based diagnostics such as sampled maximum speed.
It does not reconstruct a water surface or make a hydraulic acceptance decision.

These APIs are additive. Discharge-indexed depth stacks and their v1 indexes retain
their existing contracts.
