"""Raster tiler backend selection for every raster PMTiles writer.

Terrain and result layers in the MapLibre viewer are colorized, warped to Web
Mercator and written as PNG PMTiles. Two backends do this:

``terrio``
    Pure-Rust, in-process (``pip install terrio``; no GDAL, no ``pmtiles``
    CLI). It colour-relieves, warps and writes the PMTiles pyramid itself.
    Measured on Bald Eagle rasters against the GDAL chain: 2.9x faster on a
    depth map (2.2 s vs 6.4 s), 2.5x on a 20 ft terrain (6.6 s vs 16.4 s),
    with z14 colours within 0.2/255 on average. It also fixes two display
    defects of the GDAL chain:

    * **Result layers hold their wet area at every zoom.** Overviews use
      ``NEAREST`` by default, so visible flood area at z10 is 1.000x the
      full-resolution area. The GDAL chain (``gdaladdo -r average``) showed
      1.225x at z10, the same extent growth ``ras2cng.cog`` prevents in COGs.
    * **No dark terrain fringe.** The GDAL chain's semi-transparent terrain
      edge pixels were black (``gdalwarp -dstalpha`` writes black RGB under
      alpha 0, which ``gdaladdo`` then averages in); terrio's keep the ramp
      colour.

    It can also bake a **hillshade** into terrain tiles (``terrio.dem``
    multidirectional hillshade, multiplied into the colour relief). terrio's
    hillshade is not pixel-identical to ``gdaldem``; that is accepted for a
    display layer.

    **Streaming** (capability-detected; gpt-cmdr/terrio #8, #11, #12, #13):
    when ``terrio.dem`` takes ``max_memory_mib`` and ``hillshade_strength``,
    the relief is computed path to path a strip of rows at a time, the
    hillshade is blended in the same pass (``"shaded-relief"``, 0 DN from the
    NumPy blend below), the warp runs in chunks when ``terrio.warp`` takes
    ``max_memory_mib``, and the PMTiles pyramid is written a tile row at a
    time from the lazily opened warp. Nothing is held whole, so there is no
    pixel ceiling. Measured on Bald Eagle Terrain50 (15,777 x 12,599): see
    ``docs/user-guide/pmtiles.md``.

    With an older terrio (stock 0.5.0) the whole raster is processed in
    memory, so rasters above ``RAS2CNG_TERRIO_MAX_PIXELS`` (default 400 M
    pixels) use GDAL in ``auto``.

``gdal``
    The original chain: ``gdaldem color-relief`` -> ``gdalwarp`` ->
    ``gdal_translate -of MBTiles`` -> ``gdaladdo`` -> ``pmtiles convert``.
    Streams through files, so it handles any raster size. No hillshade.

``RAS2CNG_RASTER_TILER`` selects ``terrio``, ``gdal`` or ``auto`` (the
default: terrio when installed and the raster fits, else GDAL).
``RAS2CNG_TERRIO_MAX_MEMORY_MIB`` bounds the streaming dem and warp strips
(terrio's defaults, 256 and 64 MiB, when unset).
``RAS2CNG_RASTER_TILE_RESAMPLING`` overrides terrio's overview resampling
(``NEAREST`` or ``AVERAGE``). Defaults: ``NEAREST`` for result layers (area
preserving), ``AVERAGE`` for terrain (smooth continuous surface).
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from typing import Any, Optional

TERRIO = "terrio"
GDAL = "gdal"
AUTO = "auto"
TILER_ENV = "RAS2CNG_RASTER_TILER"
RESAMPLING_ENV = "RAS2CNG_RASTER_TILE_RESAMPLING"
MAX_PIXELS_ENV = "RAS2CNG_TERRIO_MAX_PIXELS"
MAX_MEMORY_ENV = "RAS2CNG_TERRIO_MAX_MEMORY_MIB"
DEFAULT_MAX_PIXELS = 400_000_000
OVERVIEW_RESAMPLING = ("NEAREST", "AVERAGE")
# Hillshade multiplies the relief by 1 - strength * (1 - shade / 255).
HILLSHADE_STRENGTH = 0.6

# Web Mercator resolution of zoom 0 for 256-pixel tiles (metres / pixel).
_WEB_MERCATOR_INITIAL_RESOLUTION = 2 * 6378137 * 3.141592653589793 / 256


class TerrioTooLarge(RuntimeError):
    """The raster exceeds the in-memory terrio budget."""


def terrio_available() -> bool:
    return importlib.util.find_spec("terrio") is not None


def requested_backend() -> str:
    value = os.environ.get(TILER_ENV, AUTO).strip().lower() or AUTO
    if value not in (AUTO, TERRIO, GDAL):
        raise ValueError(f"{TILER_ENV} must be one of auto, terrio, gdal; got {value!r}")
    return value


def resolve_backend() -> str:
    """Return ``terrio`` or ``gdal`` for this process."""
    choice = requested_backend()
    if choice == TERRIO:
        if not terrio_available():
            raise RuntimeError(
                f"{TILER_ENV}=terrio, but terrio is not installed. "
                "Install it with `pip install terrio` (or ras2cng[terrio])."
            )
        return TERRIO
    if choice == GDAL:
        return GDAL
    return TERRIO if terrio_available() else GDAL


def overview_resampling(kind: str) -> str:
    """Overview resampling for terrio pyramids; ``kind`` is terrain or result."""
    value = os.environ.get(RESAMPLING_ENV, "").strip().upper()
    if value:
        if value not in OVERVIEW_RESAMPLING:
            raise ValueError(f"{RESAMPLING_ENV} must be NEAREST or AVERAGE; got {value!r}")
        return value
    return "AVERAGE" if kind == "terrain" else "NEAREST"


def max_pixels() -> int:
    value = os.environ.get(MAX_PIXELS_ENV)
    return int(value) if value else DEFAULT_MAX_PIXELS


def raster_info(path: Path) -> dict[str, Any]:
    """Band statistics in ``gdalinfo -json -stats`` shape, read with terrio."""
    import terrio

    info = terrio.info(str(path), stats=True)
    bands = []
    for band in info.get("bands") or []:
        stats = band.get("statistics") or {}
        bands.append({
            "band": band.get("band"),
            "type": band.get("data_type"),
            "noDataValue": band.get("nodata"),
            "minimum": stats.get("min"),
            "maximum": stats.get("max"),
            "mean": stats.get("mean"),
            "stdDev": stats.get("std_dev"),
        })
    width, height = (info.get("size") or [None, None])[:2]
    return {"driverShortName": info.get("driver"), "size": [width, height], "bands": bands}


def _signature_has(function: Any, *names: str) -> bool:
    """Whether ``function``'s signature has every parameter in ``names``.

    Matched as whole parameter names: ``band_metadata`` is not ``metadata``.
    """
    import re

    text = getattr(function, "__text_signature__", None) or ""
    return all(re.search(rf"(?<![\w]){re.escape(name)}\s*=", text) for name in names)


def streaming_dem_supported() -> bool:
    """terrio's ``dem`` streams path to path and blends shaded relief in one pass.

    gpt-cmdr/terrio #11 (windowed ``dem``, ``max_memory_mib``) and #12
    (``"shaded-relief"``, ``hillshade_strength``). The same release streams
    PMTiles from a lazily opened raster (#8), which this path relies on.
    """
    try:
        import terrio
    except ImportError:
        return False
    return _signature_has(getattr(terrio, "dem", None), "max_memory_mib", "hillshade_strength")


def streaming_warp_supported() -> bool:
    """terrio's ``warp`` writes a path source to a path in chunks (gpt-cmdr/terrio #13)."""
    try:
        import terrio
    except ImportError:
        return False
    return _signature_has(getattr(terrio, "warp", None), "max_memory_mib")


def max_memory_mib() -> Optional[float]:
    value = os.environ.get(MAX_MEMORY_ENV, "").strip()
    if not value:
        return None
    mib = float(value)
    if not mib > 0:
        raise ValueError(f"{MAX_MEMORY_ENV} must be positive; got {value!r}")
    return mib


def check_fits(path: Path) -> None:
    """Raise :class:`TerrioTooLarge` when the raster exceeds the budget.

    The budget applies only to the in-memory path: a terrio that streams the
    relief, warp and pyramid holds no raster whole, so any size fits.
    """
    if streaming_dem_supported() and streaming_warp_supported():
        return
    import terrio

    width, height = terrio.info(str(path))["size"][:2]
    if int(width) * int(height) > max_pixels():
        raise TerrioTooLarge(
            f"{Path(path).name} is {width} x {height} pixels, above "
            f"{MAX_PIXELS_ENV}={max_pixels():,}"
        )


def _hillshade_scale(raster: Any) -> Optional[float]:
    """Vertical/horizontal unit ratio: 111120 for geographic rasters."""
    try:
        from pyproj import CRS

        crs = CRS.from_user_input(raster.crs)
        return 111120.0 if crs.is_geographic else None
    except Exception:
        return None


# Intermediate GeoTIFFs of the streaming path: tiled so the warp and the
# tile writer read them by window, and compressed so a large terrain's RGBA
# relief does not cost its full uncompressed size in scratch space.
_INTERMEDIATE = {"compress": "LZW", "tile_size": 512}


def _pmtiles_options(max_zoom: int, kind: str, name: Optional[str]) -> dict[str, str]:
    return {
        "TILE_FORMAT": "PNG",
        "MAXZOOM": str(int(max_zoom)),
        "RESAMPLING": overview_resampling(kind),
        "NAME": name or "display",
        "DESCRIPTION": name or "display",
    }


def _warp_options(max_zoom: int) -> dict[str, Any]:
    resolution = _WEB_MERCATOR_INITIAL_RESOLUTION / (2 ** int(max_zoom))
    return {
        "t_srs": "EPSG:3857",
        "tr": (resolution, resolution),
        "tap": True,
        "resampling": "bilinear",
    }


def render_pmtiles(
    cog_path: Path,
    output: Path,
    *,
    ramp_path: Path,
    max_zoom: int,
    kind: str = "result",
    hillshade: bool = False,
    name: Optional[str] = None,
    scratch_dir: Optional[Path] = None,
) -> None:
    """Colour-relieve, optionally hillshade, warp and tile a raster with terrio.

    Matches the GDAL chain's grid: bilinear warp to the exact Web Mercator
    resolution of ``max_zoom`` with target-aligned pixels, PNG tiles, and a
    pyramid down to the zoom whose single tile covers the raster.

    Streams when the installed terrio can (see :func:`streaming_dem_supported`);
    the tiles are the in-memory path's (pinned by tests). ``scratch_dir`` holds
    the streaming intermediates; the system temporary directory by default.
    """
    if streaming_dem_supported():
        _render_streaming(
            Path(cog_path), Path(output), ramp_path=Path(ramp_path), max_zoom=max_zoom,
            kind=kind, hillshade=hillshade, name=name, scratch_dir=scratch_dir,
        )
    else:
        _render_in_memory(
            Path(cog_path), Path(output), ramp_path=Path(ramp_path), max_zoom=max_zoom,
            kind=kind, hillshade=hillshade, name=name,
        )


def _render_streaming(
    cog_path: Path,
    output: Path,
    *,
    ramp_path: Path,
    max_zoom: int,
    kind: str,
    hillshade: bool,
    name: Optional[str],
    scratch_dir: Optional[Path],
) -> None:
    """Relief (and hillshade) path to path, chunked warp, streamed pyramid."""
    import tempfile

    import terrio

    from ras2cng.cog import atomic_output

    memory = max_memory_mib()
    memory_kw = {"max_memory_mib": memory} if memory else {}
    with tempfile.TemporaryDirectory(
        prefix="ras2cng-tiles-", dir=str(scratch_dir) if scratch_dir is not None else None,
    ) as temporary:
        work = Path(temporary)
        relief_path = work / "relief.tif"
        common = dict(
            color_file=str(ramp_path), alpha=True, dst=str(relief_path),
            **_INTERMEDIATE, **memory_kw,
        )
        if hillshade:
            # One pass: colour relief darkened by 1 - s * (1 - shade / 255)
            # where visible and shaded -- the blend of _render_in_memory, 0 DN.
            relief = terrio.dem(
                str(cog_path), "shaded-relief", multidirectional=True, compute_edges=True,
                scale=_hillshade_scale(terrio.open(str(cog_path))),
                hillshade_strength=HILLSHADE_STRENGTH, **common,
            )
        else:
            relief = terrio.dem(str(cog_path), "color-relief", **common)

        if streaming_warp_supported():
            web = terrio.warp(
                str(relief_path), dst=str(work / "display-3857.tif"),
                **_warp_options(max_zoom), **_INTERMEDIATE, **memory_kw,
            )
        else:
            # Older terrio: the relief is read whole for the warp, but the
            # pyramid below still streams from it.
            web = terrio.warp(relief, **_warp_options(max_zoom))
        del relief
        with atomic_output(output, suffix=".pmtiles") as staged:
            web.write(str(staged), driver="PMTiles", options=_pmtiles_options(max_zoom, kind, name))
        del web


def _render_in_memory(
    cog_path: Path,
    output: Path,
    *,
    ramp_path: Path,
    max_zoom: int,
    kind: str,
    hillshade: bool,
    name: Optional[str],
) -> None:
    """The original path for terrio releases without streaming (stock 0.5.0)."""
    import numpy as np
    import terrio

    from ras2cng.cog import atomic_output

    check_fits(cog_path)
    relief = terrio.dem(str(cog_path), "color-relief", color_file=str(ramp_path), alpha=True)
    source = relief
    if hillshade:
        shade = terrio.dem(
            str(cog_path), "hillshade", multidirectional=True, compute_edges=True,
            scale=_hillshade_scale(relief),
        )
        rgba = np.asarray(relief.read()).copy()
        values = np.asarray(shade.read()).astype(np.float32)
        if values.ndim == 3:
            values = values[0]
        # Shade only visible pixels with a valid hillshade (0 is hillshade
        # nodata). Darkening the RGB under alpha 0 would bring back the dark
        # fringe that overview averaging paints along the terrain edge.
        factor = 1.0 - HILLSHADE_STRENGTH * (1.0 - values / 255.0)
        factor = np.where((rgba[3] > 0) & (values > 0), factor, 1.0)
        rgba[:3] = np.clip(rgba[:3].astype(np.float32) * factor, 0, 255).astype(np.uint8)
        source = terrio.Raster.from_array(rgba, relief.transform, crs=relief.crs)
        del rgba, values, shade

    web = terrio.warp(source, **_warp_options(max_zoom))
    with atomic_output(Path(output), suffix=".pmtiles") as staged:
        web.write(str(staged), driver="PMTiles", options=_pmtiles_options(max_zoom, kind, name))
