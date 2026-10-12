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

Neither backend is ever asked to drop features to cap tile size.
Tippecanoe's ``--drop-densest-as-needed`` thins a uniform mesh into visible
stripes, and freestiler's ``drop_rate`` removes features by bounding-box size,
which empties whole zoom levels of a mesh and cuts the refined cells along
breaklines first. Tile weight is controlled by each tileset's minimum zoom
instead.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
from pathlib import Path
from typing import Mapping, Sequence

import geopandas as gpd
import numpy as np

TIPPECANOE = "tippecanoe"
FREESTILER = "freestiler"
_CHOICES = (TIPPECANOE, FREESTILER)


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


def run_freestiler(
    output: Path,
    layers: Mapping[str, gpd.GeoDataFrame],
    min_zoom: int,
    max_zoom: int,
) -> None:
    """Write ``layers`` to one PMTiles archive, one source layer per key.

    Every feature is kept at every zoom (no ``drop_rate``); the output is
    staged beside ``output`` and replaced atomically.
    """

    require_freestiler()
    import freestiler

    from ras2cng.cog import atomic_output

    frames = {name: _freestiler_frame(frame) for name, frame in layers.items()}
    frames = {name: frame for name, frame in frames.items() if not frame.empty}
    if not frames:
        raise ValueError("A vector tileset needs at least one non-empty source layer.")
    output = Path(output)
    with atomic_output(output, suffix=".pmtiles") as staged:
        freestiler.freestile(
            frames,
            str(staged),
            min_zoom=int(min_zoom),
            max_zoom=int(max_zoom),
            quiet=True,
        )
