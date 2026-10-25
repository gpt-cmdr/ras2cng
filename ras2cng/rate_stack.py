"""Lossless precipitation-indexed raster stacks and HTTP chunk indexes.

These APIs do not change the discharge-indexed depth-stack v1 contract.
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


def write_rate_stack(
    sources: Mapping[str, Sequence[str | Path]],
    target: str | Path,
    *,
    rate_k16: Sequence[int],
    units: Mapping[str, str],
    provenance: Mapping[str, str],
) -> dict:
    """Write a new NetCDF4 file and return its range index (no source mutation).

    Sources are single-band float32 rasters on one north-up projected grid with
    declared nodata. Quantities share the integer ``rate_k16`` axis; display rates
    are ``10**(rate_k16/16)`` in/h. Native CRS and units are preserved. Every
    compressed chunk spans one rate and at most 256 by 256 pixels. Callers write
    the returned JSON beside the stack as ``ranges.json``.
    """
    import netCDF4
    import numpy as np
    import rasterio
    from rasterio.windows import Window

    keys = list(rate_k16)
    if not keys or any(type(k) is not int or not -128 <= k <= 128 for k in keys):
        raise ValueError("rate_k16 must contain int keys within [-128, 128]")
    if any(a >= b for a, b in pairwise(keys)):
        raise ValueError("rate_k16 must be strictly increasing")
    allowed = {"depth": "ft", "wse": "ft", "velocity": "ft/s", "hazard": "1"}
    if not sources or set(sources) - set(allowed) or set(units) != set(sources):
        raise ValueError("quantities must be depth, wse, velocity or hazard with units")
    if any(units[k] != allowed[k] or len(v) != len(keys) for k, v in sources.items()):
        raise ValueError("quantity units or rate counts differ")
    if any(
        not isinstance(k, str) or not isinstance(v, str) for k, v in provenance.items()
    ):
        raise ValueError("provenance must map strings to strings")
    paths = {k: [Path(p) for p in v] for k, v in sources.items()}
    target = Path(target)
    if target.exists() or target.is_symlink():
        raise ValueError("target must be new")
    expected = None
    for group in paths.values():
        for path in group:
            with rasterio.open(path) as src:
                grid = (src.crs, src.transform, src.width, src.height)
                t = src.transform
                if (
                    src.crs is None
                    or not src.crs.is_projected
                    or src.count != 1
                    or src.nodata is None
                    or src.dtypes != ("float32",)
                    or src.scales != (1.0,)
                    or src.offsets != (0.0,)
                    or t.b
                    or t.d
                    or t.a <= 0
                    or t.e >= 0
                    or not all(math.isfinite(x) for x in tuple(t))
                ):
                    raise ValueError(
                        "sources require a north-up projected float32 nodata grid"
                    )
                if expected is not None and grid != expected:
                    raise ValueError("source grids differ")
                expected = grid
    crs, transform, width, height = expected
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(suffix=".nc", dir=target.parent)
    os.close(fd)
    try:
        with netCDF4.Dataset(temporary, "w", format="NETCDF4") as nc:
            nc.Conventions = "CF-1.8"
            nc.schema = "ras2cng-rate-stack/v1"
            nc.provenance_json = json.dumps(
                dict(provenance), sort_keys=True, allow_nan=False
            )
            for name, size in (("rate_k16", len(keys)), ("y", height), ("x", width)):
                nc.createDimension(name, size)
            axis = nc.createVariable("rate_k16", "i4", ("rate_k16",))
            axis[:] = keys
            axis.units = "1"
            axis.long_name = "Sixteenths of log10 excess precipitation rate in in/h"
            rate = nc.createVariable("rate_in_per_h", "f8", ("rate_k16",))
            rate[:] = [10 ** (k / 16) for k in keys]
            rate.units = "in h-1"
            for name, size, origin, spacing in (
                ("x", width, transform.c, transform.a),
                ("y", height, transform.f, transform.e),
            ):
                var = nc.createVariable(name, "f8", (name,))
                var[:] = origin + (np.arange(size) + 0.5) * spacing
                var.standard_name = "projection_" + name + "_coordinate"
                var.units = crs.linear_units
            reference = nc.createVariable("crs", "i4")
            reference.spatial_ref = crs.to_wkt()
            reference.GeoTransform = " ".join(str(v) for v in transform.to_gdal())
            for quantity, group in paths.items():
                variable = nc.createVariable(
                    quantity,
                    "f4",
                    ("rate_k16", "y", "x"),
                    fill_value=np.float32(np.nan),
                    zlib=True,
                    complevel=4,
                    shuffle=True,
                    chunksizes=(1, min(256, height), min(256, width)),
                )
                variable.units = units[quantity]
                variable.grid_mapping = "crs"
                variable.coordinates = "rate_in_per_h"
                for index, path in enumerate(group):
                    with rasterio.open(path) as src:
                        if (src.crs, src.transform, src.width, src.height) != expected:
                            raise ValueError("source grid changed")
                        for row in range(0, height, 256):
                            for col in range(0, width, 256):
                                window = Window(
                                    col,
                                    row,
                                    min(256, width - col),
                                    min(256, height - row),
                                )
                                data = src.read(1, window=window, masked=True).filled(
                                    np.nan
                                )
                                if np.isinf(data).any():
                                    raise ValueError("source contains infinite values")
                                if (
                                    quantity in {"depth", "velocity", "hazard"}
                                    and (data < 0).any()
                                ):
                                    raise ValueError(
                                        "source contains negative non-elevation values"
                                    )
                                variable[
                                    index,
                                    row : row + data.shape[0],
                                    col : col + data.shape[1],
                                ] = data
        # The index records exact compressed bytes; it never changes the NetCDF.
        index = rate_stack_index(Path(temporary))
        # A hard-link publication refuses concurrent targets atomically. The
        # temporary lives on the same filesystem and is removed below.
        os.link(temporary, target)
        return index
    finally:
        Path(temporary).unlink(missing_ok=True)


def rate_stack_index(path: str | Path) -> dict:
    """Inspect a rate stack read-only and index each raw HDF5 chunk.

    Offsets and sizes are bytes from the start of the NetCDF. ``filter_mask``
    must be zero; decode deflate then reverse byte shuffle into little-endian
    float32. Edge chunks have the full declared chunk shape, including fill.
    The whole-object SHA-256 binds both numeric values and metadata.
    """
    import h5py
    import numpy as np

    path = Path(path)
    variables = {}
    with h5py.File(path, "r") as hdf:
        schema = hdf.attrs.get("schema")
        if isinstance(schema, bytes):
            schema = schema.decode()
        if schema != "ras2cng-rate-stack/v1":
            raise ValueError("unsupported rate-stack schema")
        keys = hdf["rate_k16"][:]
        rates = hdf["rate_in_per_h"][:]
        if (
            keys.ndim != 1
            or not len(keys)
            or keys.dtype.kind != "i"
            or (keys < -128).any()
            or (keys > 128).any()
            or (np.diff(keys) <= 0).any()
            or rates.shape != keys.shape
            or not np.allclose(rates, 10.0 ** (keys / 16), rtol=1e-14, atol=0)
        ):
            raise ValueError("invalid precipitation rate coordinates")
        shape = (len(keys), len(hdf["y"]), len(hdf["x"]))
        for name in ("depth", "wse", "velocity", "hazard"):
            if name not in hdf:
                continue
            dataset = hdf[name]
            if set(dataset.attrs) & {
                "scale_factor",
                "add_offset",
                "_Unsigned",
                "missing_value",
            }:
                raise ValueError("unsupported quantity decoding attributes")
            if (
                dataset.shape != shape
                or dataset.chunks != (1, min(256, shape[1]), min(256, shape[2]))
                or not np.isnan(dataset.fillvalue)
            ):
                raise ValueError("unsupported quantity shape, chunks or fill")
            if (
                dataset.dtype != np.dtype("<f4")
                or dataset.compression != "gzip"
                or not dataset.shuffle
            ):
                raise ValueError("unsupported chunk encoding")
            pipeline = dataset.id.get_create_plist()
            filters = [pipeline.get_filter(i) for i in range(pipeline.get_nfilters())]
            if [f[0] for f in filters] != [2, 1] or filters[0][2] != (4,):
                raise ValueError("unsupported HDF5 filter pipeline")
            chunks = []
            for ordinal in range(dataset.id.get_num_chunks()):
                info = dataset.id.get_chunk_info(ordinal)
                if info.filter_mask:
                    raise ValueError("skipped HDF5 filter")
                chunks.append(
                    {
                        "origin": list(info.chunk_offset),
                        "offset": info.byte_offset,
                        "length": info.size,
                    }
                )
            unit = dataset.attrs["units"]
            if isinstance(unit, bytes):
                unit = unit.decode()
            if (
                unit
                != {"depth": "ft", "wse": "ft", "velocity": "ft/s", "hazard": "1"}[name]
            ):
                raise ValueError("unsupported quantity units")
            variables[name] = {
                "dimensions": ["rate_k16", "y", "x"],
                "shape": list(dataset.shape),
                "chunk_shape": list(dataset.chunks),
                "dtype": "<f4",
                "units": unit,
                "fill_value": "NaN",
                "filters": ["shuffle:4", "deflate"],
                "chunks": chunks,
            }
        if not variables:
            raise ValueError("rate stack contains no quantity")
        keys = keys.tolist()
        rates = rates.tolist()
        reference = hdf["crs"].attrs["spatial_ref"]
        if isinstance(reference, bytes):
            reference = reference.decode()
        transform = hdf["crs"].attrs["GeoTransform"]
        if isinstance(transform, bytes):
            transform = transform.decode()
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return {
        "schema": "ras2cng-rate-stack-ranges/v1",
        "bytes": path.stat().st_size,
        "sha256": digest.hexdigest(),
        "rate_k16": keys,
        "rate_in_per_h": rates,
        "grid": {
            "crs": reference,
            "geotransform": [float(x) for x in transform.split()],
        },
        "variables": variables,
    }


def rasterize_cell_values(
    cells, values, template: str | Path, target: str | Path
) -> Path:
    """Rasterize native cell scalars onto a template grid without interpolation.

    Cell polygons and template must share a projected CRS. Values are float32;
    NaN remains nodata. Pixel centers select their native cell. Source order
    resolves boundary ties. Inputs remain read-only and target must be new.
    """
    import numpy as np
    import rasterio
    from rasterio.features import rasterize

    target = Path(target)
    if target.exists() or target.is_symlink() or len(values) != len(cells):
        raise ValueError("new target and one value per cell required")
    with rasterio.open(template) as src:
        if cells.crs != src.crs or not src.crs.is_projected:
            raise ValueError("cell and raster CRS must agree and be projected")
        profile = src.profile.copy()
        profile.update(dtype="float32", count=1, nodata=np.nan)
        # Block windows keep mapping bounded by tile size rather than raster area.
        with rasterio.open(target, "w", **profile) as dst:
            for _, window in dst.block_windows(1):
                bounds = rasterio.windows.bounds(window, dst.transform)
                selected = sorted(cells.sindex.intersection(bounds))
                shapes = [(cells.geometry.iloc[i], float(values[i])) for i in selected]
                data = (
                    rasterize(
                        shapes,
                        out_shape=(int(window.height), int(window.width)),
                        transform=dst.window_transform(window),
                        fill=np.nan,
                        dtype="float32",
                    )
                    if shapes
                    else np.full((int(window.height), int(window.width)), np.nan, "f4")
                )
                dst.write(data, 1, window=window)
    return target
