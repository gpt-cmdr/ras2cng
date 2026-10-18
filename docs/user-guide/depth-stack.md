# NetCDF depth catalogs

`ras2cng.depth_stack.write_depth_stack` packages one reach's ordered depth
profiles into a single NetCDF4 file. Install `ras2cng[netcdf]`. This is a raster
publishing primitive, not a HEC-RAS parser or a hydraulic approval gate.

```python
from ras2cng.depth_stack import write_depth_stack

write_depth_stack(
    ["p01-depth.tif", "p02-depth.tif"], "reach.nc",
    profiles=["p01", "p02"], vertical_unit="ft",
    provenance={"project": "public-project", "reach": "reach-identifier"},
)
```

The caller supplies the complete selected catalog in stable order. No files are
searched for, sorted, reprojected or resampled. Inputs must have a single band,
identical north-up EPSG:3857 grid/shape and identical explicitly declared nodata
(NaN is allowed). Source scale and offset decode the physical values. Valid
values must be finite and exactly representable as float32; otherwise the writer
rejects them instead of introducing rounding. Valid dry zero remains zero;
missing source cells become NaN `_FillValue`. No packing or lossy compression is
used, and no terrain or COG policy is changed.

The initial display schema is `ras2cng-depth-stack/v1`, CF-1.8:

| Field | Meaning |
| --- | --- |
| `depth(flow,y,x)` | float32 physical depth; units explicitly `ft` or `m` |
| `flow(flow)` | Ordered integer profile indices by default, units `1`, `axis_semantics=profile_index` |
| `profile(flow)` | Ordered unique nonblank UTF-8 labels |
| `x(x)`, `y(y)` | EPSG:3857 pixel-center coordinates in metres; y descends |
| `crs` | WKT, EPSG identity, GeoTransform and spherical Mercator grid mapping |
| `source_sha256_json` | Ordered input SHA256 identities, with no source paths |
| `provenance_json` | Caller-supplied explicit public string metadata only |
| `role` | `display_derivative`; no approval or datum claim |

If every physical discharge is known, pass `flows=[...]` and
`flow_units="cfs"` or `"cms"`. The flow coordinate then contains these exact
finite values, with CF units `ft3 s-1` or `m3 s-1` and
`axis_semantics=discharge`. Without physical flows, profile indices must never be
interpreted or presented as discharge. Profile labels remain the catalog join
key. No discharge ordering or hydraulic completeness is inferred.

Chunks contain one profile and at most 256 x 256 spatial cells. Standard HDF5
shuffle+deflate supports level 4 (default) or 6 explicitly. A small synthetic
256 x 256 float32 ramp measured level 4 at 24,568 bytes / 0.020 s and level 6 at
23,670 bytes / 0.015 s on one workstation. This checks compatibility and provides
no production speed claim; level 4 is the conservative default pending a fleet
benchmark. Browser range-request performance requires a separate viewer test.

Writing uses a unique temporary file beside the destination and atomic replace
on success. Grid/contract rejection or a value/write failure leaves any previous
file intact and removes the temporary. Sources are opened read-only and hashes
are checked after writing each profile. Inputs must remain immutable during
packaging. The caller owns selection lineage, vertical-unit provenance, public
metadata safety, product review status, container execution and delivery receipts.

Physical discharge coordinates must be strictly increasing or decreasing; they are never sorted. For duplicate or nonmonotonic physical discharge catalogs, omit flows, retain profile-index order, and carry actual discharges in the bound external catalog. All grid transform coefficients must be finite.
