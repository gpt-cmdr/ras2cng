"""Lossless, range-readable NetCDF depth catalogs for an aligned display grid.

One file stores the ordered profiles of one caller-selected reach. This module
neither parses HEC-RAS nor establishes hydraulic acceptance. Install
``ras2cng[netcdf]`` to write files; imports remain optional until invoked.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from collections.abc import Mapping, Sequence
from itertools import pairwise
from pathlib import Path


def write_depth_stack(
    sources: Sequence[str | Path],
    target: str | Path,
    *,
    profiles: Sequence[str],
    vertical_unit: str,
    flows: Sequence[float] | None = None,
    flow_units: str | None = None,
    provenance: Mapping[str, str] | None = None,
    compression_level: int = 4,
    include_source_hashes: bool = True,
) -> Path:
    """Write one atomic NetCDF4 depth catalog without modifying source rasters.

    Inputs must be single-band rasters on the same north-up EPSG:3857 grid,
    with identical declared nodata (including NaN). Values are decoded using
    band scale/offset and must be exactly representable as float32; no rounding,
    reprojection or lossy packing is performed. Missing cells become NaN fill;
    valid dry zero is preserved. Nonfinite valid values are rejected.

    ``vertical_unit`` explicitly declares physical depth units ``ft`` or ``m``;
    it is a caller assertion, not inferred from horizontal CRS. ``profiles`` is
    a nonempty unique ordered sequence of nonblank strings. Optional ``flows``
    must contain a finite, strictly monotonic discharge per profile and require ``flow_units`` of
    ``cfs`` or ``cms``. Otherwise the flow axis is explicitly dimensionless
    profile indices, never invented discharge. Caller-supplied provenance is
    explicit public string metadata: no source paths are exported automatically.

    The CF-1.8 file contains ``depth(flow,y,x)``, ``profile(flow)``, coordinate
    centers, CRS/grid metadata, source SHA256 identities and display_derivative
    role. Chunking is (1,min(256,height),min(256,width)), with shuffle and standard
    deflate level 4 or 6. Existing targets are replaced only after success.
    ``include_source_hashes=False`` omits private input identities from public
    display files; hashes are still checked privately for source immutability.
    Source selection/completeness, units and hydraulic review remain caller
    responsibilities; the file never asserts approval.
    """
    try:
        import netCDF4
        import numpy as np
        import rasterio
        from rasterio.windows import Window
    except ImportError as exc:
        raise ImportError("Install ras2cng[netcdf] to write depth stacks") from exc

    paths = [Path(source) for source in sources]
    labels = list(profiles)
    destination = Path(target)
    if not paths or len(paths) != len(labels):
        raise ValueError("sources and profiles must be nonempty and equal length")
    if any(
        not isinstance(label, str) or not label.strip() or "\x00" in label
        for label in labels
    ):
        raise ValueError("profiles must be nonblank strings without NUL")
    if len(set(labels)) != len(labels):
        raise ValueError("profiles must be unique")
    if vertical_unit not in {"ft", "m"}:
        raise ValueError("vertical_unit must be ft or m")
    if type(compression_level) is not int or compression_level not in {4, 6}:
        raise ValueError("compression_level must be 4 or 6")
    if flows is None:
        if flow_units is not None:
            raise ValueError("flow_units requires flows")
        coordinates = list(range(len(paths)))
    else:
        if flow_units not in {"cfs", "cms"} or len(flows) != len(paths):
            raise ValueError("flows require matching length and cfs or cms flow_units")
        if any(isinstance(value, bool) or not math.isfinite(value) for value in flows):
            raise ValueError("flows must be finite numbers")
        coordinates = list(flows)
        steps = [b - a for a, b in pairwise(coordinates)]
        if steps and not (
            all(step > 0 for step in steps) or all(step < 0 for step in steps)
        ):
            raise ValueError(
                "flows must be strictly monotonic; use profile indices for unordered discharge"
            )
    if type(include_source_hashes) is not bool:
        raise ValueError("include_source_hashes must be a boolean")
    public = dict(provenance or {})
    if any(
        not isinstance(k, str)
        or not k.strip()
        or not isinstance(v, str)
        or "\x00" in k + v
        for k, v in public.items()
    ):
        raise ValueError("provenance must map nonblank strings to strings without NUL")
    if destination.resolve() in {path.resolve() for path in paths}:
        raise ValueError("target must not replace an input")

    def identity(path):
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def grid(dataset):
        if dataset.crs is None or dataset.crs.to_epsg() != 3857:
            raise ValueError("sources must declare EPSG:3857")
        transform = dataset.transform
        if not all(math.isfinite(value) for value in tuple(transform)):
            raise ValueError("source grid transform must be finite")
        if transform.b != 0 or transform.d != 0 or transform.a <= 0 or transform.e >= 0:
            raise ValueError("sources must use a north-up axis-aligned grid")
        if np.dtype(dataset.dtypes[0]).kind not in "iuf":
            raise ValueError("sources require real numeric depth values")
        if not all(
            math.isfinite(value) for value in (*dataset.scales, *dataset.offsets)
        ):
            raise ValueError("source scale and offset must be finite")
        if dataset.count != 1 or dataset.nodata is None:
            raise ValueError("sources require one band and declared nodata")
        nodata = dataset.nodata
        return (
            dataset.width,
            dataset.height,
            tuple(transform),
            "nan" if math.isnan(nodata) else nodata,
        )

    hashes = []
    expected = None
    for path in paths:
        with rasterio.open(path) as dataset:
            current = grid(dataset)
            if expected is not None and current != expected:
                raise ValueError("sources must have matching grid, shape and nodata")
            expected = current
        hashes.append(identity(path))
    width, height, transform_values, _ = expected
    transform = rasterio.Affine(*transform_values[:6])
    destination.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".partial", dir=destination.parent
    )
    os.close(handle)
    try:
        with netCDF4.Dataset(temporary, "w", format="NETCDF4") as output:
            output.Conventions = "CF-1.8"
            output.schema = "ras2cng-depth-stack/v1"
            output.role = "display_derivative"
            output.provenance_json = json.dumps(public, sort_keys=True, allow_nan=False)
            if include_source_hashes:
                output.source_sha256_json = json.dumps(hashes)
            output.createDimension("flow", len(paths))
            output.createDimension("y", height)
            output.createDimension("x", width)
            flow = output.createVariable(
                "flow", "f8" if flows is not None else "i4", ("flow",)
            )
            flow[:] = coordinates
            flow.units = {"cfs": "ft3 s-1", "cms": "m3 s-1"}.get(flow_units, "1")
            flow.axis_semantics = "discharge" if flows is not None else "profile_index"
            flow.long_name = (
                "Discharge" if flows is not None else "Ordered profile index"
            )
            if flow_units is not None:
                flow.flow_units = flow_units
            profile = output.createVariable("profile", str, ("flow",))
            profile[:] = np.asarray(labels, dtype=object)
            for name, length, origin, spacing, standard in (
                ("x", width, transform.c, transform.a, "projection_x_coordinate"),
                ("y", height, transform.f, transform.e, "projection_y_coordinate"),
            ):
                coordinate = output.createVariable(name, "f8", (name,))
                coordinate[:] = origin + (np.arange(length) + 0.5) * spacing
                coordinate.standard_name = standard
                coordinate.units = "m"
                coordinate.axis = name.upper()
            crs = output.createVariable("crs", "i4")
            crs.spatial_ref = rasterio.crs.CRS.from_epsg(3857).to_wkt()
            crs.crs_wkt = crs.spatial_ref
            crs.epsg_code = "EPSG:3857"
            crs.grid_mapping_name = "mercator"
            crs.longitude_of_projection_origin = 0.0
            crs.standard_parallel = 0.0
            crs.false_easting = 0.0
            crs.false_northing = 0.0
            crs.earth_radius = 6378137.0
            crs.GeoTransform = " ".join(str(value) for value in transform.to_gdal())
            depth = output.createVariable(
                "depth",
                "f4",
                ("flow", "y", "x"),
                fill_value=np.float32(np.nan),
                zlib=True,
                complevel=compression_level,
                shuffle=True,
                chunksizes=(1, min(256, height), min(256, width)),
            )
            depth.units = vertical_unit
            depth.long_name = "Depth"
            depth.grid_mapping = "crs"
            depth.coordinates = "profile"
            for index, path in enumerate(paths):
                with rasterio.open(path) as dataset:
                    if grid(dataset) != expected:
                        raise ValueError("source grid changed while writing")
                    for row in range(0, height, 256):
                        for col in range(0, width, 256):
                            window = Window(
                                col, row, min(256, width - col), min(256, height - row)
                            )
                            raw = dataset.read(1, window=window, masked=True)
                            mask = np.ma.getmaskarray(raw)
                            if raw.dtype.kind in "iu" and np.any(
                                np.abs(raw.data[~mask].astype("f8")) >= 2**53
                            ):
                                raise ValueError(
                                    "integer depth values exceed exact decoding range"
                                )
                            values = (
                                raw.data.astype("f8") * dataset.scales[0]
                                + dataset.offsets[0]
                            )
                            valid = values[~mask]
                            converted = valid.astype("f4")
                            if not np.isfinite(valid).all() or not np.array_equal(
                                converted.astype("f8"), valid
                            ):
                                raise ValueError(
                                    "decoded valid values must be finite and exactly representable as float32"
                                )
                            stored = np.full(values.shape, np.nan, dtype="f4")
                            stored[~mask] = converted
                            depth[
                                index,
                                row : row + int(window.height),
                                col : col + int(window.width),
                            ] = stored
                if identity(path) != hashes[index]:
                    raise ValueError("source changed while writing")
        os.replace(temporary, destination)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return destination
