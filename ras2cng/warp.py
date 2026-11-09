"""Path-to-path raster reprojection, and the terrain-merge engine switch.

``reproject_path`` is the one reprojection used by ``ras2cng map
--reproject-wgs84`` (:func:`ras2cng.mapping._reproject_tifs`) and
:func:`ras2cng.terrain._reproject_to_match`.

Engines (``RAS2CNG_REPROJECT_ENGINE`` = ``auto`` | ``terrio`` | ``rasterio``):

``terrio``
    ``terrio.warp(path, dst=path)`` of a CLB terrio build whose warp streams
    path to path (``max_memory_mib``; capability-detected, see
    :func:`ras2cng.raster_tiles.streaming_warp_supported`). The output grid is
    GDAL's suggested warp output, the same as rasterio's
    ``calculate_default_transform``. Measured on Bald Eagle Terrain50 to
    EPSG:4326 (17,367 x 10,487): 7.8 s against 30.3 s, reprojection peak 1,033
    against 1,682 MiB; 876 of 182 M pixels differ from rasterio's by at most
    2.4e-4 ft (float rounding), with equal masks
    (``feature_dev_notes/rust_engines/T31.md``).

    ``overview="NONE"`` is always passed. terrio's default is gdalwarp's
    ``-ovr AUTO``, which reads a source overview when the output is coarser
    than the source; rasterio reads full resolution. Leaving it out was worth
    up to 151 ft on a terrain mosaic.

``rasterio``
    GDAL through rasterio. The fallback in ``auto`` (PyPI terrio has no
    streaming warp) and whenever terrio refuses a file.

The chunk memory is terrio's 64 MB default unless
``RAS2CNG_TERRIO_MAX_MEMORY_MIB`` is set. A smaller value lowers the warp's
peak and, exactly as ``gdalwarp -wm`` does, changes interpolated pixels.
"""

from __future__ import annotations

import os
import warnings
from pathlib import Path
from typing import Any, Optional

AUTO = "auto"
TERRIO = "terrio"
RASTERIO = "rasterio"
REPROJECT_ENGINE_ENV = "RAS2CNG_REPROJECT_ENGINE"
REPROJECT_ENGINES = (AUTO, TERRIO, RASTERIO)

#: Terrain consolidation (:func:`ras2cng.terrain._merge_tifs`) engine. ``auto``
#: is ``rasterio`` (:data:`TERRAIN_MERGE_AUTO`): the terrio mosaic is
#: bit-identical to GDAL's whole-output warp but changes today's blockwise
#: output by up to 4.4 ft, so it is opt-in until the user decides the default.
TERRAIN_MERGE_ENGINE_ENV = "RAS2CNG_TERRAIN_MERGE_ENGINE"
TERRAIN_MERGE_ENGINES = (AUTO, TERRIO, RASTERIO)
TERRAIN_MERGE_AUTO = RASTERIO


def _resolve(env: str, choices: tuple[str, ...], value: Optional[str]) -> str:
    raw = value if value is not None else os.environ.get(env, AUTO)
    text = str(raw).strip().lower() or AUTO
    if text == "gdal":
        text = RASTERIO
    if text not in choices:
        raise ValueError(f"{env} must be one of {', '.join(choices)}; got {raw!r}")
    return text


def resolve_reproject_engine(engine: Optional[str] = None) -> str:
    """``auto``, ``terrio`` or ``rasterio``; ``None`` reads the env var."""
    return _resolve(REPROJECT_ENGINE_ENV, REPROJECT_ENGINES, engine)


def resolve_terrain_merge_engine(engine: Optional[str] = None) -> str:
    """``terrio`` or ``rasterio`` for the terrain merge (``auto`` = rasterio)."""
    choice = _resolve(TERRAIN_MERGE_ENGINE_ENV, TERRAIN_MERGE_ENGINES, engine)
    return TERRAIN_MERGE_AUTO if choice == AUTO else choice


def terrio_warp_supported() -> bool:
    """True when the installed terrio warps path to path in bounded chunks."""
    from ras2cng import raster_tiles

    if not raster_tiles.streaming_warp_supported():
        return False
    import terrio

    return raster_tiles._signature_has(terrio.warp, "dst", "t_srs", "overview")


def memory_kwargs() -> dict[str, Any]:
    """``{"max_memory_mib": n}`` when ``RAS2CNG_TERRIO_MAX_MEMORY_MIB`` is set."""
    from ras2cng import raster_tiles

    memory = raster_tiles.max_memory_mib()
    return {"max_memory_mib": memory} if memory else {}


def _wkt(crs: Any) -> str:
    from rasterio.crs import CRS

    if isinstance(crs, CRS):
        return crs.to_wkt()
    to_wkt = getattr(crs, "to_wkt", None)
    if callable(to_wkt):
        return to_wkt()
    return CRS.from_user_input(crs).to_wkt()


def _reproject_terrio(src: Path, dst: Path, dst_crs: Any, resampling: str) -> None:
    import terrio

    result = terrio.warp(
        str(src), dst=str(dst), t_srs=_wkt(dst_crs), resampling=resampling,
        # Never read source overviews: rasterio does not (see the module notes).
        overview="NONE",
        **memory_kwargs(),
    )
    del result  # a lazily opened raster; release the file before it is used


def _reproject_rasterio(src: Path, dst: Path, dst_crs: Any, resampling: str) -> None:
    import rasterio
    from rasterio.crs import CRS
    from rasterio.warp import Resampling, calculate_default_transform, reproject

    target = dst_crs if isinstance(dst_crs, CRS) else CRS.from_wkt(_wkt(dst_crs))
    with rasterio.open(src) as source:
        transform, width, height = calculate_default_transform(
            source.crs, target, source.width, source.height, *source.bounds
        )
        meta = source.meta.copy()
        meta.update(
            driver="GTiff", crs=target, transform=transform, width=width, height=height
        )
        with rasterio.open(dst, "w", **meta) as destination:
            for band in range(1, source.count + 1):
                reproject(
                    source=rasterio.band(source, band),
                    destination=rasterio.band(destination, band),
                    src_transform=source.transform,
                    src_crs=source.crs,
                    dst_transform=transform,
                    dst_crs=target,
                    resampling=Resampling[resampling],
                )


def reproject_path(
    src: Path,
    dst: Path,
    dst_crs: Any,
    *,
    resampling: str = "bilinear",
    engine: Optional[str] = None,
) -> str:
    """Reproject ``src`` to ``dst`` in ``dst_crs``; return the engine used.

    The grid is GDAL's suggested warp output in both engines, the data type,
    band count and nodata are the source's, and values agree to float
    rounding. ``engine`` (``None`` reads ``RAS2CNG_REPROJECT_ENGINE``):
    ``auto`` uses terrio when it has the streaming warp and falls back to
    rasterio, with a warning, when terrio refuses the file; ``terrio`` is
    strict; ``rasterio`` never tries terrio. A source without a CRS raises.
    """
    import rasterio

    src, dst = Path(src), Path(dst)
    choice = resolve_reproject_engine(engine)
    with rasterio.open(src) as source:
        if source.crs is None:
            raise ValueError(f"{src.name} has no CRS; it cannot be reprojected")
    dst.parent.mkdir(parents=True, exist_ok=True)
    if choice == TERRIO and not terrio_warp_supported():
        raise RuntimeError(
            f"{REPROJECT_ENGINE_ENV}=terrio needs a terrio whose warp streams path to "
            "path (the CLB terrio build; see the Engine installs notes)"
        )
    if choice in (AUTO, TERRIO) and terrio_warp_supported():
        try:
            _reproject_terrio(src, dst, dst_crs, resampling)
            return TERRIO
        except Exception as exc:
            if choice == TERRIO:
                raise
            dst.unlink(missing_ok=True)
            warnings.warn(
                f"terrio could not reproject {src.name} ({exc}); using rasterio",
                RuntimeWarning,
                stacklevel=2,
            )
    _reproject_rasterio(src, dst, dst_crs, resampling)
    return RASTERIO
