"""Display terrain on a native-aligned coarser grid with explicit rounding.

A display terrain is a derived raster: the authoritative native terrain is only read.
The primitives here are deliberately small so a pipeline can apply its own policy
(which encodings it allows, how it qualifies them, where units come from):

- :func:`plan_display_grid` -- an integer multiple of the native cell nearest a target
  size in metres, keeping the native upper-left origin and CRS, so every display cell
  edge is a native cell edge.
- :func:`aggregate_block` / :func:`build_rounded_reference` -- the float64 mean of the
  valid native cells of each block, rounded half away from zero to an explicit step,
  streamed band by band into a float64 reference GeoTIFF.
- :func:`write_display_cog` -- one encoding of that reference written through
  :func:`ras2cng.cog.area_matched_cog`, losslessly, with the same rounding applied to
  the area-matched overview means.
- :func:`measure_against_reference` -- bytes, grid/nodata identity and the decoded error
  against the rounded reference.

Elevations are never reprojected or resampled across native cells, and no vertical unit is
inferred: values stay in the source's vertical unit.
"""

from __future__ import annotations

import math
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ras2cng.cog import round_to_step

#: Nodata of floating-point display rasters.
NODATA = -9999.0
#: Nodata of integer (scaled) display rasters.
INT_NODATA = -2147483647


@dataclass(frozen=True)
class DisplayEncoding:
    """One lossless (or explicitly lossy) COG encoding of a rounded display terrain.

    ``float32`` stores the rounded value to within half a float32 ulp and needs no
    scale handling by readers. ``int32`` stores the value in units of the rounding step
    with a band scale, which is exact but needs scale-aware readers.
    """

    name: str
    dtype: str
    compress: str
    predictor: int | None
    lerc_max_z_error: float | None = None

    @property
    def lossy(self) -> bool:
        return self.lerc_max_z_error is not None

    @property
    def scaled(self) -> bool:
        return self.dtype == "int32"


DISPLAY_ENCODINGS: dict[str, DisplayEncoding] = {
    e.name: e
    for e in (
        DisplayEncoding("float32-deflate-p3", "float32", "DEFLATE", 3),
        DisplayEncoding("float32-zstd-p3", "float32", "ZSTD", 3),
        DisplayEncoding("int32-centi-deflate-p2", "int32", "DEFLATE", 2),
        DisplayEncoding("int32-centi-zstd-p2", "int32", "ZSTD", 2),
        DisplayEncoding("float32-lerc-zstd-0.005", "float32", "LERC_ZSTD", None, 0.005),
    )
}
#: Decoded float32 elevations with DEFLATE, readable without scale handling or libzstd.
DEFAULT_DISPLAY_ENCODINGS = ("float32-deflate-p3",)


@dataclass(frozen=True)
class DisplayGrid:
    """The display lattice: an integer multiple of the native lattice with the same origin."""

    factor: int
    native_cell: float
    cell: float
    origin_x: float
    origin_y: float
    width: int
    height: int
    cell_m: float

    def transform(self) -> Any:
        from rasterio.transform import Affine

        return Affine(self.cell, 0.0, self.origin_x, 0.0, -self.cell, self.origin_y)

    def as_dict(self) -> dict[str, Any]:
        return {
            "factor": self.factor,
            "native_cell": self.native_cell,
            "cell": self.cell,
            "origin_x": self.origin_x,
            "origin_y": self.origin_y,
            "width": self.width,
            "height": self.height,
            "cell_m": self.cell_m,
            "transform": list(self.transform())[:6],
        }


def plan_display_grid(
    transform: Any, width: int, height: int, m_per_unit: float, target_m: float
) -> DisplayGrid:
    """Return the display grid of a north-up square native grid.

    ``factor`` is the integer nearest ``target_m`` divided by the native cell in
    metres, at least 1. The upper-left origin is the native one; a partial last row or
    column covers the remaining native cells. Raises ``ValueError`` for rotated,
    non-square or south-up grids and for non-positive sizes.
    """
    a, b, c, d, e, f = (
        transform.a,
        transform.b,
        transform.c,
        transform.d,
        transform.e,
        transform.f,
    )
    if b != 0 or d != 0 or a <= 0 or e >= 0 or not math.isclose(a, -e, rel_tol=1e-9):
        raise ValueError("the native grid must be north-up with square cells")
    if width <= 0 or height <= 0:
        raise ValueError("native dimensions must be positive")
    if not all(math.isfinite(v) for v in (a, b, c, d, e, f, m_per_unit, target_m)):
        raise ValueError("grid values must be finite")
    if not (m_per_unit > 0 and target_m > 0):
        raise ValueError("m_per_unit and target_m must be positive")
    factor = max(1, round(target_m / (a * m_per_unit)))
    return DisplayGrid(
        factor=factor,
        native_cell=a,
        cell=a * factor,
        origin_x=c,
        origin_y=f,
        width=-(-width // factor),
        height=-(-height // factor),
        cell_m=a * factor * m_per_unit,
    )


def aggregate_block(
    native: Any, valid: Any, factor: int, min_valid_fraction: float
) -> tuple[Any, Any, Any]:
    """Aggregate a native window of whole display rows (edge blocks may be partial).

    Returns ``(mean, display_valid, valid_count)`` per display cell. ``mean`` is float64
    and NaN where no native cell is valid. A block is valid when it has at least one valid
    cell and ``valid_count >= min_valid_fraction`` times the native cells it covers.
    """
    import numpy as np

    if not isinstance(factor, int) or isinstance(factor, bool) or factor < 1:
        raise ValueError("factor must be a positive integer")
    if not math.isfinite(min_valid_fraction) or not 0 <= min_valid_fraction <= 1:
        raise ValueError("min_valid_fraction must be between zero and one")
    if native.shape != valid.shape or native.ndim != 2:
        raise ValueError("native and valid must have the same two-dimensional shape")
    rows, cols = native.shape
    out_rows, out_cols = -(-rows // factor), -(-cols // factor)
    pad = ((0, out_rows * factor - rows), (0, out_cols * factor - cols))
    values = np.pad(np.where(valid, native, 0.0).astype("float64"), pad)
    present = np.pad(np.ones(native.shape, dtype=bool), pad)
    good = np.pad(valid, pad)
    shape = (out_rows, factor, out_cols, factor)
    total = values.reshape(shape).sum(axis=(1, 3))
    count = good.reshape(shape).sum(axis=(1, 3))
    covered = present.reshape(shape).sum(axis=(1, 3))
    display_valid = (count > 0) & (count >= min_valid_fraction * covered)
    with np.errstate(invalid="ignore", divide="ignore"):
        mean = np.where(count > 0, total / np.maximum(count, 1), np.nan)
    return mean, display_valid, count


def build_rounded_reference(
    source: Path,
    destination: Path,
    grid: DisplayGrid,
    *,
    round_step: float,
    min_valid_fraction: float = 0.5,
    strict_mask: Callable[[Any, Any], Any] | None = None,
    strict_destination: Path | None = None,
    band_rows: int = 768,
) -> dict[str, Any]:
    """Write the rounded display reference (float64 GeoTIFF) and measure it.

    Reads ``source`` in horizontal bands of whole display rows (``band_rows`` native rows,
    rounded down to a multiple of the factor). ``strict_mask(shape, window_transform)``
    returns a boolean native-grid mask of a strict publication footprint; its block-wise
    "any" is written as uint8 to ``strict_destination`` and values are never clipped.

    Returns counts (native/display valid cells, native valid cells lost in nodata blocks,
    display cells valid without native support -- always 0), the native and display value
    ranges, and the aggregation error ``|display - native|`` over the valid native cells
    of valid blocks (RMSE, max, p95/p99 upper bounds in 0.05-unit bins), all in the
    source's vertical unit.
    """
    import numpy as np
    import rasterio
    from rasterio.windows import Window

    round_to_step([], round_step)
    rows_per_band = max(grid.factor, (band_rows // grid.factor) * grid.factor)
    stats: dict[str, Any] = {
        "native_cells": 0,
        "native_valid_cells": 0,
        "native_valid_cells_in_nodata_blocks": 0,
        "display_cells": grid.width * grid.height,
        "display_valid_cells": 0,
        "display_valid_without_native": 0,
        "strict_display_cells": None,
    }
    native_lo = display_lo = math.inf
    native_hi = display_hi = -math.inf
    err_sq, err_max, err_n = 0.0, 0.0, 0
    histogram = np.zeros(64, dtype="int64")
    strict_cells = 0
    ones = np.ones((grid.factor, grid.factor))
    with rasterio.open(source) as src:
        if (grid.width, grid.height) != (
            -(-src.width // grid.factor),
            -(-src.height // grid.factor),
        ) or not src.transform.almost_equals(
            grid.transform() * rasterio.Affine.scale(1 / grid.factor), precision=1e-9
        ):
            raise ValueError("display grid does not match the source lattice")
        nodata = src.nodata
        profile = {
            "driver": "GTiff",
            "dtype": "float64",
            "nodata": NODATA,
            "width": grid.width,
            "height": grid.height,
            "count": 1,
            "crs": src.crs,
            "transform": grid.transform(),
            "tiled": True,
            "blockxsize": 512,
            "blockysize": 512,
            "compress": "DEFLATE",
            "predictor": 3,
            "BIGTIFF": "IF_SAFER",
        }
        band_tags = dict(src.tags(1))
        strict_out = None
        if strict_mask is not None and strict_destination is not None:
            strict_out = rasterio.open(
                strict_destination,
                "w",
                **(profile | {"dtype": "uint8", "nodata": 0, "predictor": 2}),
            )
        try:
            with rasterio.open(destination, "w", **profile) as out:
                for top in range(0, src.height, rows_per_band):
                    window = Window(
                        0, top, src.width, min(rows_per_band, src.height - top)
                    )
                    native = src.read(1, window=window).astype("float64")
                    valid = (src.read_masks(1, window=window) > 0) & np.isfinite(native)
                    if nodata is not None:
                        valid &= native != nodata
                    mean, ok, count = aggregate_block(
                        native, valid, grid.factor, min_valid_fraction
                    )
                    rounded = round_to_step(mean, round_step)
                    if np.any(ok & (rounded == NODATA)):
                        raise ValueError(
                            "valid rounded elevation collides with display nodata"
                        )
                    shape = native.shape
                    expanded_ok = np.kron(ok, ones).astype(bool)[: shape[0], : shape[1]]
                    stats["native_cells"] += native.size
                    stats["native_valid_cells"] += int(valid.sum())
                    stats["display_valid_cells"] += int(ok.sum())
                    stats["display_valid_without_native"] += int(
                        (ok & (count == 0)).sum()
                    )
                    stats["native_valid_cells_in_nodata_blocks"] += int(
                        (valid & ~expanded_ok).sum()
                    )
                    if valid.any():
                        native_lo = min(native_lo, float(native[valid].min()))
                        native_hi = max(native_hi, float(native[valid].max()))
                    if ok.any():
                        display_lo = min(display_lo, float(rounded[ok].min()))
                        display_hi = max(display_hi, float(rounded[ok].max()))
                        expanded = np.kron(np.where(ok, rounded, 0.0), ones)[
                            : shape[0], : shape[1]
                        ]
                        both = valid & expanded_ok
                        error = np.abs(expanded[both] - native[both])
                        if error.size:
                            err_sq += float((error**2).sum())
                            err_max = max(err_max, float(error.max()))
                            err_n += int(error.size)
                            histogram += np.bincount(
                                np.minimum((error / 0.05).astype("int64"), 63),
                                minlength=64,
                            )
                    display_window = Window(
                        0, top // grid.factor, grid.width, ok.shape[0]
                    )
                    out.write(np.where(ok, rounded, NODATA), 1, window=display_window)
                    if strict_out is not None:
                        inside = strict_mask(shape, src.window_transform(window))
                        block = aggregate_block(
                            inside.astype("float64"), inside, grid.factor, 1e-12
                        )[1]
                        strict_cells += int(block.sum())
                        strict_out.write(
                            block.astype("uint8"), 1, window=display_window
                        )
                out.update_tags(
                    1,
                    **{k: v for k, v in band_tags.items() if k in ("UNITS", "TYPE")},
                    ROUNDING_STEP=f"{round_step:g}",
                    AGGREGATION="mean_of_valid_native_cells",
                    NATIVE_FACTOR=str(grid.factor),
                )
        finally:
            if strict_out is not None:
                strict_out.close()
                stats["strict_display_cells"] = strict_cells
    cumulative = histogram.cumsum()

    def bound(q: float) -> float | None:
        if not err_n:
            return None
        index = int(np.searchsorted(cumulative, q * err_n))
        return (
            math.ceil(err_max * 20) / 20
            if index == 63
            else round((index + 1) * 0.05, 2)
        )

    return stats | {
        "native_range": [native_lo, native_hi] if math.isfinite(native_lo) else None,
        "display_range": [display_lo, display_hi]
        if math.isfinite(display_lo)
        else None,
        "aggregation_error": {
            "definition": "|rounded display value - native cell| over valid native cells of valid blocks",
            "cells": err_n,
            "rmse": round(math.sqrt(err_sq / err_n), 4) if err_n else None,
            "max": round(err_max, 4),
            "p95_upper_bound": bound(0.95),
            "p99_upper_bound": bound(0.99),
        },
    }


def write_display_cog(
    reference: Path,
    destination: Path,
    encoding: DisplayEncoding,
    *,
    round_step: float,
    scratch_dir: Path,
    blocksize: int = 512,
    require_area_matched: bool = True,
) -> dict[str, Any]:
    """Write one encoding of a rounded reference as a COG; return the writer report.

    The reference is cast to the encoding's dtype (``int32`` stores ``value / round_step``
    with band scale ``round_step``) and written by :func:`ras2cng.cog.area_matched_cog`
    with explicit compression, predictor and LERC setting (``None`` = lossless) and with
    the overview means rounded to ``round_step``. ``require_area_matched`` makes a GDAL
    overview fallback (which cannot round) an error instead of a silent label.
    """
    import numpy as np
    import rasterio

    from ras2cng.cog import area_matched_cog

    round_to_step([], round_step)
    if encoding.scaled:
        raise ValueError(
            "scaled integer display encoding is not qualified for overview rounding"
        )
    source = Path(scratch_dir) / f"{encoding.name}.source.tif"
    nodata = INT_NODATA if encoding.scaled else NODATA
    with rasterio.open(reference) as ref:
        profile = ref.profile | {
            "dtype": encoding.dtype,
            "nodata": nodata,
            "predictor": 2 if encoding.scaled else 3,
        }
        with rasterio.open(source, "w", **profile) as out:
            for _, window in ref.block_windows(1):
                values = ref.read(1, window=window)
                empty = values == NODATA
                stored = np.rint(values / round_step) if encoding.scaled else values
                out.write(
                    np.where(empty, nodata, stored).astype(encoding.dtype),
                    1,
                    window=window,
                )
            out.update_tags(1, **ref.tags(1))
            if encoding.scaled:
                out.scales = (round_step,)
                out.offsets = (0.0,)
    report = area_matched_cog(
        source,
        destination,
        categorical=False,
        compress=encoding.compress,
        predictor=encoding.predictor,
        blocksize=blocksize,
        scratch_dir=Path(scratch_dir),
        require_area_matched=require_area_matched,
        lerc_max_z_error=encoding.lerc_max_z_error,
        round_step=None if encoding.scaled else round_step,
    )
    return report.as_tags() | {"overview_method": report.method}


def measure_against_reference(
    path: Path, reference: Path, round_step: float
) -> dict[str, Any]:
    """Measure a written display COG against its rounded reference.

    Values are decoded with the band scale/offset. ``max_error_vs_rounded_reference`` is in
    the vertical unit; ``representation_tolerance`` is half a float32 ulp at the largest
    magnitude for floats and 1e-9 for scaled integers. ``values_off_rounding_grid`` counts
    decoded values farther than half their own ulp from a multiple of ``round_step``.
    Every stored overview is checked window by window against an independently
    rebuilt area-matched pyramid from the verified rounded reference cast to the
    stored base dtype. The same mask, error and grid checks include all levels.
    This reuses the writer's bounded quarter-grid accumulators, not a whole native
    raster array; its pixel ceiling also applies to qualification. Scaled integers
    are explicitly unqualified pending a consumer and integer tie-rule review.
    """
    import numpy as np
    import rasterio
    from rasterio.windows import Window

    from ras2cng.cog import build_area_matched_overviews, overview_factors

    round_to_step([], round_step)
    max_error = max_abs = 0.0
    off_grid = mismatched = 0
    levels = []

    def compare(actual, expected, expected_transform=None):
        nonlocal max_error, max_abs, off_grid, mismatched
        level_error = 0.0
        level_mismatched = level_off_grid = 0
        transform = (
            expected.transform if expected_transform is None else expected_transform
        )
        if (
            actual.shape != expected.shape
            or actual.crs != expected.crs
            or not actual.transform.almost_equals(transform, precision=1e-9)
        ):
            return {
                "same_grid": False,
                "max_error": None,
                "mask_mismatches": None,
                "off_grid": None,
            }
        for _, window in expected.block_windows(1):
            want = expected.read(1, window=window).astype("float64")
            raw = actual.read(1, window=window)
            got = raw.astype("float64") * scale + offset
            want_ok = (expected.read_masks(1, window=window) > 0) & np.isfinite(want)
            got_ok = (actual.read_masks(1, window=window) > 0) & np.isfinite(got)
            level_mismatched += int((want_ok != got_ok).sum())
            both = want_ok & got_ok
            if both.any():
                level_error = max(
                    level_error, float(np.abs(got[both] - want[both]).max())
                )
                max_abs = max(max_abs, float(np.abs(got[both]).max()))
                spacing = (
                    1e-9
                    if integer
                    else np.abs(np.spacing(raw[both].astype("float32"))) / 2
                )
                residue = np.abs(got[both] - round_to_step(got[both], round_step))
                level_off_grid += int((residue > spacing + 1e-12).sum())
        max_error = max(max_error, level_error)
        mismatched += level_mismatched
        off_grid += level_off_grid
        return {
            "same_grid": True,
            "max_error": level_error,
            "mask_mismatches": level_mismatched,
            "off_grid": level_off_grid,
        }

    with rasterio.open(path) as cog, rasterio.open(reference) as ref:
        integer = np.dtype(cog.dtypes[0]).kind in "iu"
        scale = (cog.scales or (1.0,))[0] or 1.0
        offset = (cog.offsets or (0.0,))[0] or 0.0
        same_grid = (
            cog.shape == ref.shape
            and cog.crs == ref.crs
            and cog.transform == ref.transform
        )
        base = compare(cog, ref)
        overviews = cog.overviews(1)
        expected_factors = overview_factors(
            ref.width, ref.height, blocksize=cog.block_shapes[0][0]
        )
        levels_match = overviews == expected_factors
        # Expected means use the same *rounded reference* represented as float32,
        # never the candidate's potentially corrupted base values or masks.
        if not integer and same_grid:
            with tempfile.TemporaryDirectory(prefix="ras2cng-display-check-") as temp:
                root = Path(temp)
                cast = root / "base.tif"
                with rasterio.open(
                    cast, "w", **(ref.profile | {"dtype": cog.dtypes[0]})
                ) as output:
                    for _, window in ref.block_windows(1):
                        values = ref.read(1, window=window)
                        valid = (ref.read_masks(1, window=window) > 0) & np.isfinite(
                            values
                        )
                        output.write(
                            np.where(valid, values, NODATA).astype(cog.dtypes[0]),
                            1,
                            window=window,
                        )
                paths, _ = build_area_matched_overviews(
                    cast, root, factors=expected_factors, round_step=round_step
                )
                for index, expected_path in enumerate(paths):
                    if index >= len(overviews):
                        break
                    with (
                        rasterio.open(path, overview_level=index) as actual,
                        rasterio.open(expected_path) as expected,
                    ):
                        # TIFF overviews retain the base extent. At a partial
                        # edge their advertised pixel size is width/ceil(width/k),
                        # while the stored contributors still use k-by-k blocks.
                        transform = cog.transform * rasterio.Affine.scale(
                            cog.width / expected.width, cog.height / expected.height
                        )
                        result = compare(actual, expected, transform)
                        levels.append({"factor": expected_factors[index], **result})
                        same_grid &= result["same_grid"]
        structure = cog.tags(ns="IMAGE_STRUCTURE")
        tags = cog.tags()
        started = time.perf_counter()
        side = min(512, cog.width, cog.height)
        cog.read(1, window=Window(0, 0, side, side))
        if overviews:
            cog.read(
                1,
                out_shape=(
                    max(1, cog.height // overviews[-1]),
                    max(1, cog.width // overviews[-1]),
                ),
            )
        read_seconds = time.perf_counter() - started
        block_shape = list(cog.block_shapes[0])
    return {
        "bytes": Path(path).stat().st_size,
        "dtype": "int32" if integer else "float32",
        "scale": scale,
        "representation_tolerance": 1e-9
        if integer
        else float(np.spacing(np.float32(max_abs))) / 2,
        "same_grid_as_reference": same_grid and levels_match,
        "max_error_vs_rounded_reference": max_error,
        "values_off_rounding_grid": off_grid,
        "nodata_mismatch_cells": mismatched,
        "overviews": list(overviews),
        "overview_levels": levels,
        "overview_levels_verified": not integer
        and levels_match
        and same_grid
        and all(
            level["mask_mismatches"] == 0
            and level["off_grid"] == 0
            and level["max_error"]
            <= float(abs(np.spacing(np.float32(max_abs)))) / 2 + 1e-12
            for level in levels
        ),
        "qualified_selectable": not integer
        and structure.get("COMPRESSION") in {"DEFLATE", "ZSTD"}
        and levels_match
        and same_grid
        and base["same_grid"]
        and mismatched == 0
        and off_grid == 0
        and max_error <= float(abs(np.spacing(np.float32(max_abs)))) / 2 + 1e-12,
        "overview_method": tags.get("overview_method"),
        "overview_rounding_step": tags.get("overview_rounding_step"),
        "block_shape": block_shape,
        "layout": structure.get("LAYOUT"),
        "compression": structure.get("COMPRESSION"),
        "first_block_and_coarsest_overview_read_seconds": round(read_seconds, 4),
    }


__all__ = [
    "DEFAULT_DISPLAY_ENCODINGS",
    "DISPLAY_ENCODINGS",
    "INT_NODATA",
    "NODATA",
    "DisplayEncoding",
    "DisplayGrid",
    "aggregate_block",
    "build_rounded_reference",
    "measure_against_reference",
    "plan_display_grid",
    "round_to_step",
    "write_display_cog",
]
