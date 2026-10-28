"""Vector tiler backend selection shared by every PMTiles writer in ras2cng.

Two backends produce the same delivery contract -- every feature at every zoom
of the tileset, coincident shared edges between mesh cells, one named source
layer per input:

``tippecanoe``
    External ``tippecanoe`` + ``pmtiles`` CLIs. The long-standing default.
    Not available natively on Windows.

``freestiler``
    In-process Rust tiler from the optional ``freestiler`` extra
    (``pip install ras2cng[freestiler]``). Measured on HEC-RAS meshes it is
    8-20x faster with identical completeness and no inter-cell gaps (it snaps
    vertices to the tile pixel grid, so shared vertices stay shared), but its
    archives are 40-50% larger because it does not simplify, and every layer of
    a tileset is held in memory at once. See
    ``feature_dev_notes/vector_tiler_evaluation_2026-10-12.md``.

``RAS2CNG_VECTOR_TILER`` selects ``tippecanoe``, ``freestiler`` or ``auto``
(the default): tippecanoe when its CLIs are on PATH, otherwise freestiler when
installed.

freestiler options (ignored by tippecanoe, whose tiles never carry IDs):

``RAS2CNG_VECTOR_TILE_IDS``
    ``1`` (default) keeps an MVT feature ID on every feature; ``0`` omits them
    (``generate_ids=False``). On the 87k-cell Bald Eagle mesh, z10-15, omitting
    IDs saves 1.7 MB (-14%). Nothing in the ras2cng viewer reads feature IDs
    (tippecanoe output never had them), but a consumer using MapLibre
    ``feature-state`` without ``promoteId`` needs them. The CLI flag
    ``--feature-ids/--no-feature-ids`` and :func:`vector_tile_options` override it.

``RAS2CNG_VECTOR_TILE_GZIP_LEVEL``
    Tile gzip level 0-9 when the installed freestiler accepts ``gzip_level``;
    default 6 (-14% on the same mesh, tile contents unchanged). freestiler's
    own default is 1.

Multi-layer tilesets go through ``freestiler.freestile_layers`` when it is
available, which loads and tiles one layer at a time (byte-identical output;
10 mesh layers 1.7 GB -> 311 MiB peak), and layers are read lazily from their
GeoJSON sources so the Python side holds one layer at a time too.

Neither backend is ever asked to drop features to cap tile size.
Tippecanoe's ``--drop-densest-as-needed`` thins a uniform mesh into visible
stripes, and freestiler's ``drop_rate`` removes features by bounding-box size,
which empties whole zoom levels of a mesh and cuts the refined cells along
breaklines first. Tile weight is controlled by each tileset's minimum zoom
instead.
"""

from __future__ import annotations

import contextlib
import contextvars
import importlib.util
import inspect
import json
import os
import shutil
import warnings
from pathlib import Path
from typing import Callable, Iterator, Mapping, Optional, Sequence, Union

import geopandas as gpd
import numpy as np

TIPPECANOE = "tippecanoe"
FREESTILER = "freestiler"
_CHOICES = (TIPPECANOE, FREESTILER)

FEATURE_IDS_ENV = "RAS2CNG_VECTOR_TILE_IDS"
GZIP_LEVEL_ENV = "RAS2CNG_VECTOR_TILE_GZIP_LEVEL"
DEFAULT_GZIP_LEVEL = 6

_OPTIONS: contextvars.ContextVar[dict] = contextvars.ContextVar(
    "ras2cng_vector_tile_options", default={}
)
_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


@contextlib.contextmanager
def vector_tile_options(
    *, feature_ids: Optional[bool] = None, gzip_level: Optional[int] = None
) -> Iterator[None]:
    """Override the freestiler tile options for the enclosed calls.

    ``None`` leaves a setting to its environment variable or default.
    """

    current = dict(_OPTIONS.get())
    if feature_ids is not None:
        current["feature_ids"] = bool(feature_ids)
    if gzip_level is not None:
        current["gzip_level"] = _checked_gzip_level(gzip_level, "gzip_level")
    token = _OPTIONS.set(current)
    try:
        yield
    finally:
        _OPTIONS.reset(token)


def feature_ids_enabled() -> bool:
    """Whether vector tiles carry MVT feature IDs (default True)."""

    override = _OPTIONS.get().get("feature_ids")
    if override is not None:
        return override
    raw = os.environ.get(FEATURE_IDS_ENV, "").strip().lower()
    if not raw or raw in _TRUE:
        return True
    if raw in _FALSE:
        return False
    raise ValueError(f"{FEATURE_IDS_ENV} must be 1 or 0 (true/false); got {raw!r}")


def _checked_gzip_level(value, source: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{source} must be an integer from 0 to 9; got {value!r}")
    try:
        level = int(str(value).strip())
    except (TypeError, ValueError):
        level = -1
    if not 0 <= level <= 9:
        raise ValueError(f"{source} must be an integer from 0 to 9; got {value!r}")
    return level


def gzip_level() -> int:
    """Requested tile gzip level (default 6)."""

    override = _OPTIONS.get().get("gzip_level")
    if override is not None:
        return override
    raw = os.environ.get(GZIP_LEVEL_ENV, "").strip()
    return _checked_gzip_level(raw, GZIP_LEVEL_ENV) if raw else DEFAULT_GZIP_LEVEL


def tippecanoe_command() -> str:
    """Return the tippecanoe executable.

    ``tippecanoe`` is the portable default. Windows hosts that bridge to a
    Linux Tippecanoe installation can provide the full wrapper path through
    ``RAS2CNG_TIPPECANOE``.
    """

    return os.environ.get("RAS2CNG_TIPPECANOE", "tippecanoe")


def pmtiles_command() -> str:
    """Return the PMTiles conversion executable used after tippecanoe."""

    return os.environ.get("RAS2CNG_PMTILES", "pmtiles")


def freestiler_available() -> bool:
    return importlib.util.find_spec("freestiler") is not None


def vector_tiler_backend(required_commands: Sequence[str] | None = None) -> str:
    """Resolve ``RAS2CNG_VECTOR_TILER`` to a concrete backend name.

    ``required_commands`` are the CLIs the tippecanoe path needs at this call
    site; ``auto`` only chooses tippecanoe when all of them are on PATH.
    """

    choice = os.environ.get("RAS2CNG_VECTOR_TILER", "").strip().lower() or "auto"
    if choice in _CHOICES:
        return choice
    if choice != "auto":
        raise ValueError(
            f"RAS2CNG_VECTOR_TILER must be one of auto, tippecanoe, freestiler; got {choice!r}"
        )
    commands = required_commands or (tippecanoe_command(), pmtiles_command())
    if all(shutil.which(command) for command in commands):
        return TIPPECANOE
    if freestiler_available():
        return FREESTILER
    # Neither is usable; report the missing CLI, which is the documented default.
    return TIPPECANOE


def require_freestiler() -> None:
    if not freestiler_available():
        raise RuntimeError(
            "RAS2CNG_VECTOR_TILER selects freestiler, but it is not installed. "
            "Install it with `pip install ras2cng[freestiler]`, or install the "
            "tippecanoe and pmtiles CLIs."
        )


def read_ndgeojson(path: Path) -> gpd.GeoDataFrame:
    """Read a delivery source written by ``_write_ndgeojson`` back as WGS84."""

    frame = gpd.read_file(f"GeoJSONSeq:{path}")
    if frame.crs is None:
        frame = frame.set_crs("EPSG:4326")
    return frame


def _freestiler_frame(frame: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Shape a layer the way tippecanoe would read it from GeoJSON.

    freestiler rejects null geometries and nested property values; tippecanoe
    skips the former and stringifies the latter, so match that.
    """

    frame = frame[frame.geometry.notna() & ~frame.geometry.is_empty]
    nested = [
        column
        for column in frame.columns
        if column != frame.geometry.name
        and frame[column].dtype == object
        and frame[column].map(_is_nested).any()
    ]
    if nested:
        frame = frame.copy()
        for column in nested:
            frame[column] = frame[column].map(
                lambda value: _json_text(value) if _is_nested(value) else value
            )
    return frame


def _is_nested(value: object) -> bool:
    return isinstance(value, (list, tuple, dict, np.ndarray))


def _json_text(value: object) -> str:
    if isinstance(value, np.ndarray):
        value = value.tolist()
    return json.dumps(value, default=str, separators=(",", ":"))


def _accepts(function, name: str) -> bool:
    try:
        return name in inspect.signature(function).parameters
    except (TypeError, ValueError):  # pragma: no cover - builtins without signatures
        return False


def freestiler_capabilities(module=None) -> dict[str, bool]:
    """What the installed freestiler supports, detected from its API.

    ``layers``: ``freestile_layers`` (one layer in memory at a time);
    ``gzip``: ``freestile(gzip_level=...)``; ``layers_gzip``:
    ``freestile_layers(gzip_level=...)``.
    """

    if module is None:
        import freestiler as module
    layered = getattr(module, "freestile_layers", None)
    return {
        "layers": callable(layered),
        "gzip": _accepts(module.freestile, "gzip_level"),
        "layers_gzip": callable(layered) and _accepts(layered, "gzip_level"),
    }


_WARNED: set[str] = set()


def _warn_once(key: str, message: str) -> None:
    if key not in _WARNED:
        _WARNED.add(key)
        warnings.warn(message, RuntimeWarning, stacklevel=3)


LayerSource = Union[gpd.GeoDataFrame, Callable[[], gpd.GeoDataFrame]]


def _load(source: LayerSource) -> gpd.GeoDataFrame:
    if callable(source) and not isinstance(source, gpd.GeoDataFrame):
        return source()
    return source


def run_freestiler(
    output: Path,
    layers: Mapping[str, LayerSource],
    min_zoom: int,
    max_zoom: int,
) -> dict:
    """Write ``layers`` to one PMTiles archive, one source layer per key.

    A layer is a GeoDataFrame or a zero-argument callable returning one;
    callables are loaded only when their turn comes. Every feature is kept at
    every zoom (no ``drop_rate``); the output is staged beside ``output`` and
    replaced atomically. Returns the options actually applied
    (``generate_ids``, ``gzip_level``, ``layered``).
    """

    require_freestiler()
    import freestiler

    from ras2cng.cog import atomic_output

    capabilities = freestiler_capabilities(freestiler)
    applied: dict = {"generate_ids": feature_ids_enabled(), "gzip_level": None, "layered": False}
    kwargs: dict = {
        "min_zoom": int(min_zoom),
        "max_zoom": int(max_zoom),
        "quiet": True,
        "generate_ids": applied["generate_ids"],
    }
    level = gzip_level()
    output = Path(output)
    empty_message = "A vector tileset needs at least one non-empty source layer."

    if len(layers) > 1 and capabilities["layers"]:
        applied["layered"] = True
        if capabilities["layers_gzip"]:
            kwargs["gzip_level"] = applied["gzip_level"] = level
        elif capabilities["gzip"]:
            _warn_once(
                "layers_gzip",
                "freestiler.freestile_layers has no gzip_level pass-through in this build; "
                "multi-layer tiles use freestiler's default gzip level instead of "
                f"{GZIP_LEVEL_ENV}={level}.",
            )

        def prepared():
            for name, source in layers.items():
                frame = _freestiler_frame(_load(source))
                if not frame.empty:
                    yield name, frame
                del frame

        with atomic_output(output, suffix=".pmtiles") as staged:
            try:
                freestiler.freestile_layers(prepared(), str(staged), **kwargs)
            except ValueError as exc:
                if "at least one layer" in str(exc):
                    raise ValueError(empty_message) from exc
                raise
        return applied

    frames = {name: _freestiler_frame(_load(source)) for name, source in layers.items()}
    frames = {name: frame for name, frame in frames.items() if not frame.empty}
    if not frames:
        raise ValueError(empty_message)
    if capabilities["gzip"]:
        kwargs["gzip_level"] = applied["gzip_level"] = level
    with atomic_output(output, suffix=".pmtiles") as staged:
        freestiler.freestile(frames, str(staged), **kwargs)
    return applied
