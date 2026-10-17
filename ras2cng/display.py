"""EPSG:3857 nearest-neighbour *display* derivatives of native rasters.

A native hydraulic raster (for example a depth grid in EPSG:2278) is the
authoritative product and is never altered.  Web viewers that only accept
EPSG:3857 need a separate artifact; :func:`publish_display_cog` builds exactly
that and labels it a visualization derivative.

Design constraints, each of which exists because the obvious route is wrong:

* **Nearest base grid only.**  Any interpolating kernel invents values that were never
  computed (and blends class codes).  Reprojection moves pixels; it does not
  convert pixel *value* units -- a depth in feet is still feet in EPSG:3857.
  "Nearest" describes the full-resolution base grid only.  Overview levels
  follow the existing ras2cng display policy and are labelled separately:
  continuous rasters get area-matched **mean** overviews, categorical rasters
  (winner index, valid count, classes) get discrete **mode** overviews, never a
  numerical interpolation of class codes.
* **Streamed.**  The warp runs window by window through ``WarpedVRT`` into a
  tiled scratch GeoTIFF, so peak memory is one block, not the raster.
* **One COG writer.**  The COG itself is written by
  :func:`ras2cng.cog.area_matched_cog` (lossless: ``lerc_max_z_error=None``)
  and gated by :func:`ras2cng.cog.assert_valid_cog`; there is no second renderer.
* **No replace.**  The destination is adopted with a hard link (or a
  non-overwriting rename on Windows), so an existing or raced destination is
  never clobbered and only this call's own partials are ever removed.
"""

from __future__ import annotations

import hashlib
import math
import os
import shutil
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DISPLAY_SCHEMA = "ras2cng.display_cog/1"
DISPLAY_CRS = "EPSG:3857"
BASE_RESAMPLING = "nearest"
#: Overview policy by raster kind.  These describe the *pyramid*, never the base grid.
OVERVIEW_POLICY_CONTINUOUS = "area_matched_mean"
OVERVIEW_POLICY_CATEGORICAL = "categorical_mode"
#: Published ``source_crs`` tag when no authority code could be verified.
SOURCE_CRS_UNVERIFIED = "unverified"

#: Half the Web Mercator world width, in metres.
_WEB_MERCATOR_LIMIT = 20037508.342789244
_BLOCK = 512
_HASH_CHUNK = 8 * 1024 * 1024


def _json_number(value: float | None) -> float | str | None:
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return "nan"
    return value


@dataclass(frozen=True)
class DisplayCogResult:
    """What :func:`publish_display_cog` produced, with source/output evidence."""

    source: str
    destination: str
    categorical: bool
    source_sha256_before: str
    source_sha256_after: str
    output_sha256: str
    output_size_bytes: int
    source_crs: dict[str, Any]
    source_dtype: str
    output_dtype: str
    source_nodata: float | None
    output_nodata: float | None
    source_units: str | None
    output_units: str | None
    source_shape: tuple[int, int]
    output_shape: tuple[int, int]
    source_transform: tuple[float, ...]
    output_transform: tuple[float, ...]
    output_bounds: tuple[float, float, float, float]
    overview_method: str
    overview_factors: tuple[int, ...]
    compression: str
    lossy_max_z_error: float | None
    validation: dict[str, Any]
    schema: str = DISPLAY_SCHEMA
    kind: str = "visualization_derivative"
    authoritative: bool = False
    output_crs: str = DISPLAY_CRS
    base_resampling: str = BASE_RESAMPLING
    overview_policy: str = OVERVIEW_POLICY_CONTINUOUS
    value_conversion: str = "none"

    @property
    def source_unchanged(self) -> bool:
        return self.source_sha256_before == self.source_sha256_after

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe mapping (tuples become lists, NaN becomes ``"nan"``)."""

        return {
            "schema": self.schema,
            "kind": self.kind,
            "authoritative": self.authoritative,
            "source": self.source,
            "destination": self.destination,
            "categorical": self.categorical,
            "base_resampling": self.base_resampling,
            "overview_policy": self.overview_policy,
            "value_conversion": self.value_conversion,
            "source_sha256_before": self.source_sha256_before,
            "source_sha256_after": self.source_sha256_after,
            "source_unchanged": self.source_unchanged,
            "output_sha256": self.output_sha256,
            "output_size_bytes": self.output_size_bytes,
            "source_crs": dict(self.source_crs),
            "output_crs": self.output_crs,
            "source_dtype": self.source_dtype,
            "output_dtype": self.output_dtype,
            "source_nodata": _json_number(self.source_nodata),
            "output_nodata": _json_number(self.output_nodata),
            "source_units": self.source_units,
            "output_units": self.output_units,
            "source_shape": list(self.source_shape),
            "output_shape": list(self.output_shape),
            "source_transform": list(self.source_transform),
            "output_transform": list(self.output_transform),
            "output_bounds": list(self.output_bounds),
            "overview_method": self.overview_method,
            "overview_factors": list(self.overview_factors),
            "compression": self.compression,
            "lossy_max_z_error": self.lossy_max_z_error,
            "validation": {
                "valid": bool(self.validation.get("valid")),
                "layout_checked": bool(self.validation.get("layout_checked")),
                "errors": list(self.validation.get("errors", [])),
                "warnings": list(self.validation.get("warnings", [])),
            },
        }


def sha256_file(path: Path) -> str:
    """Full-content SHA-256 of ``path``."""

    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while chunk := handle.read(_HASH_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def _check_paths(source: Path, destination: Path) -> None:
    if not source.is_file():
        raise FileNotFoundError(f"Source raster not found: {source}")
    if destination.parent.exists() and not destination.parent.is_dir():
        raise ValueError(f"Destination parent is not a directory: {destination.parent}")

    resolved_source = source.resolve()
    resolved_destination = destination.parent.resolve() / destination.name
    if os.path.normcase(resolved_source) == os.path.normcase(resolved_destination):
        raise ValueError(
            "Destination is the same file as the source; refusing to overwrite it."
        )
    # lexists: a dangling symlink is still something we must not write through.
    if os.path.lexists(destination):
        if destination.exists() and os.path.samefile(source, destination):
            raise ValueError(
                "Destination aliases the source (symlink or hard link); refusing to overwrite it."
            )
        raise FileExistsError(
            f"Destination already exists and will not be replaced: {destination}"
        )


def _validate_source(src) -> tuple[Any, float | None]:
    """Return ``(dtype, nodata)`` after rejecting rasters we cannot publish faithfully."""

    import numpy as np

    if src.crs is None:
        raise ValueError("Source raster has no CRS; refusing to guess one.")
    if src.count != 1:
        raise ValueError(f"Source raster must have exactly 1 band, found {src.count}.")
    if src.width < 1 or src.height < 1:
        raise ValueError("Source raster is empty.")
    nodata = src.nodata
    if nodata is None:
        raise ValueError(
            "Source raster has no nodata value; a display derivative cannot mark "
            "the area outside the warped footprint without one."
        )
    dtype = np.dtype(src.dtypes[0])
    if dtype.kind not in "uif":
        raise ValueError(f"Unsupported band dtype {dtype}.")
    if dtype.kind in "iu" and dtype.itemsize > 4:
        # GDAL warps through float64, which holds integers exactly only up to
        # 2**53: nearest silently turns 9007199254740993 into ...992 while the
        # dtype stays int64.  Refuse rather than publish altered class codes.
        raise ValueError(
            f"Unsupported band dtype {dtype}: 64-bit integers cannot be warped without "
            "losing values above 2**53; use 32-bit or narrower integers."
        )
    nodata = float(nodata)
    if math.isinf(nodata):
        raise ValueError("Source nodata must be finite or NaN.")
    if math.isnan(nodata):
        if dtype.kind != "f":
            raise ValueError("NaN nodata is only valid for floating-point rasters.")
    elif dtype.kind != "f":
        info = np.iinfo(dtype)
        if nodata != int(nodata) or not info.min <= nodata <= info.max:
            raise ValueError(f"Source nodata {nodata} is not representable as {dtype}.")
    else:
        # float32 nodata that overflows to inf on cast cannot round-trip.
        if not math.isfinite(float(dtype.type(nodata))):
            raise ValueError(f"Source nodata {nodata} is not representable as {dtype}.")
    return dtype, nodata


def _output_grid(src) -> tuple[Any, int, int]:
    from rasterio.crs import CRS
    from rasterio.transform import Affine
    from rasterio.warp import calculate_default_transform

    target = CRS.from_epsg(3857)
    try:
        if src.crs == target:
            transform, width, height = src.transform, src.width, src.height
        else:
            transform, width, height = calculate_default_transform(
                src.crs, target, src.width, src.height, *src.bounds
            )
    except Exception as error:
        raise ValueError(
            f"Could not derive an EPSG:3857 output grid: {error}"
        ) from error

    coefficients = tuple(transform)[:6]
    if not all(math.isfinite(value) for value in coefficients):
        raise ValueError("Output grid transform is not finite.")
    if not (
        isinstance(width, int) and isinstance(height, int) and width > 0 and height > 0
    ):
        raise ValueError(f"Output grid has invalid size {width}x{height}.")
    if transform.a == 0 or transform.e == 0 or transform.b != 0 or transform.d != 0:
        raise ValueError("Output grid must be north-up with non-zero pixel size.")
    left, top = transform * (0, 0)
    right, bottom = transform * (width, height)
    if max(abs(left), abs(right), abs(top), abs(bottom)) > _WEB_MERCATOR_LIMIT * 1.0001:
        raise ValueError("Output grid lies outside the valid EPSG:3857 extent.")
    return Affine(*coefficients), width, height


def _stream_warp(
    source_path: Path, grid, width: int, height: int, dtype, nodata, staged: Path
):
    """Nearest-warp ``source_path`` into a tiled GeoTIFF, one window at a time."""

    import rasterio
    from rasterio.crs import CRS
    from rasterio.enums import Resampling
    from rasterio.vrt import WarpedVRT
    from rasterio.windows import Window

    with rasterio.open(source_path) as src:
        units = src.units[0] if src.units else None
        profile = {
            "driver": "GTiff",
            "dtype": dtype.name,
            "count": 1,
            "width": width,
            "height": height,
            "crs": CRS.from_epsg(3857),
            "transform": grid,
            "nodata": nodata,
            "tiled": True,
            "blockxsize": _BLOCK,
            "blockysize": _BLOCK,
            "compress": "DEFLATE",
            "BIGTIFF": "IF_SAFER",
        }
        with (
            WarpedVRT(
                src,
                crs=CRS.from_epsg(3857),
                transform=grid,
                width=width,
                height=height,
                resampling=Resampling.nearest,
                src_nodata=nodata,
                nodata=nodata,
                dtype=dtype.name,
            ) as vrt,
            rasterio.open(staged, "w", **profile) as dst,
        ):
            for row in range(0, height, _BLOCK):
                for col in range(0, width, _BLOCK):
                    window = Window(
                        col, row, min(_BLOCK, width - col), min(_BLOCK, height - row)
                    )
                    dst.write(vrt.read(1, window=window), 1, window=window)
            if units:
                dst.units = (units,)
    return units


def _adopt_without_replace(staged: Path, destination: Path) -> None:
    """Move ``staged`` to ``destination`` failing if ``destination`` appears."""

    try:
        os.link(staged, destination)
    except FileExistsError:
        raise FileExistsError(
            f"Destination appeared during publish and was not replaced: {destination}"
        ) from None
    except OSError:
        # Filesystem without hard links.  Windows os.rename refuses to
        # overwrite; elsewhere there is no safe fallback, so fail loudly.
        if os.name != "nt":
            raise
        try:
            os.rename(staged, destination)
        except FileExistsError:
            raise FileExistsError(
                f"Destination appeared during publish and was not replaced: {destination}"
            ) from None
        return
    staged.unlink(missing_ok=True)


def publish_display_cog(
    source: str | os.PathLike,
    destination: str | os.PathLike,
    *,
    categorical: bool = False,
) -> DisplayCogResult:
    """Publish an EPSG:3857 nearest-neighbour display COG derived from ``source``.

    The source is opened read-only and verified unchanged by full SHA-256
    before and after.  The destination must not exist; it is created
    atomically and never replaced.  The base grid is nearest-neighbour; overviews follow the
    ras2cng display policy (``overview_policy``: area-matched mean, or discrete
    mode when ``categorical``).  The result is a *visualization
    derivative*: the native raster remains the authoritative product, and the
    EPSG:3857 grid does not convert pixel value units.
    """

    import rasterio

    from ras2cng.cog import area_matched_cog, assert_valid_cog, describe_crs

    source_path = Path(source)
    destination_path = Path(destination)
    _check_paths(source_path, destination_path)

    sha_before = sha256_file(source_path)
    with rasterio.open(source_path) as src:
        dtype, nodata = _validate_source(src)
        grid, width, height = _output_grid(src)
        source_crs = describe_crs(src.crs)
        source_shape = (src.height, src.width)
        source_transform = tuple(src.transform)[:6]
        source_units = src.units[0] if src.units else None

    overview_policy = (
        OVERVIEW_POLICY_CATEGORICAL if categorical else OVERVIEW_POLICY_CONTINUOUS
    )
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex[:12]
    staged_cog = destination_path.with_name(
        f".{destination_path.name}.display-{token}.partial"
    )
    scratch = Path(
        tempfile.mkdtemp(
            prefix=f".{destination_path.name}.display-{token}-",
            dir=destination_path.parent,
        )
    )
    try:
        warped = scratch / "warped_3857.tif"
        _stream_warp(source_path, grid, width, height, dtype, nodata, warped)
        _tag_provenance(warped, sha_before, source_crs, overview_policy)

        report = area_matched_cog(
            warped,
            staged_cog,
            categorical=categorical,
            lerc_max_z_error=None,
            scratch_dir=scratch,
            # If the area-matched builder cannot run, a categorical raster must
            # still never be averaged.
            fallback_resampling="mode" if categorical else "average",
        )
        validation = assert_valid_cog(staged_cog)
        with rasterio.open(staged_cog) as out:
            output_dtype = out.dtypes[0]
            output_nodata = out.nodata
            output_units = out.units[0] if out.units else None
            output_shape = (out.height, out.width)
            output_transform = tuple(out.transform)[:6]
            output_bounds = tuple(out.bounds)
            if out.crs is None or out.crs.to_epsg() != 3857:
                raise ValueError("Written display COG is not EPSG:3857.")
        output_sha = sha256_file(staged_cog)
        output_size = staged_cog.stat().st_size

        sha_after = sha256_file(source_path)
        if sha_after != sha_before:
            raise RuntimeError(
                "Source raster changed while the display COG was being built."
            )

        _adopt_without_replace(staged_cog, destination_path)
    finally:
        staged_cog.unlink(missing_ok=True)
        shutil.rmtree(scratch, ignore_errors=True)

    return DisplayCogResult(
        source=str(source_path),
        destination=str(destination_path),
        categorical=categorical,
        source_sha256_before=sha_before,
        source_sha256_after=sha_after,
        output_sha256=output_sha,
        output_size_bytes=output_size,
        source_crs=source_crs,
        source_dtype=dtype.name,
        output_dtype=output_dtype,
        source_nodata=nodata,
        output_nodata=output_nodata,
        source_units=source_units,
        output_units=output_units,
        source_shape=source_shape,
        output_shape=output_shape,
        source_transform=source_transform,
        output_transform=output_transform,
        output_bounds=output_bounds,
        overview_policy=overview_policy,
        overview_method=report.method,
        overview_factors=tuple(report.factors),
        compression=report.compression,
        lossy_max_z_error=report.lossy_max_z_error,
        validation={
            "valid": validation.valid,
            "layout_checked": validation.layout_checked,
            "errors": list(validation.errors),
            "warnings": list(validation.warnings),
        },
    )


def _tag_provenance(
    path: Path, source_sha256: str, source_crs: dict[str, Any], overview_policy: str
) -> None:
    """Stamp clean provenance tags (no paths, no source captions) on the intermediate.

    ``source_crs`` is a verified authority identifier (``EPSG:2278``) or the
    neutral ``unverified``.  The unverified CRS string is verbatim WKT whose
    name may be a private path, so it stays in the internal result only.
    """

    import rasterio

    with rasterio.open(path, "r+") as dst:
        dst.update_tags(
            display_derivative="true",
            derivative_kind="visualization",
            base_resampling=BASE_RESAMPLING,
            overview_policy=overview_policy,
            value_conversion="none",
            source_sha256=source_sha256,
            source_crs=str(source_crs.get("authority") or SOURCE_CRS_UNVERIFIED),
        )
