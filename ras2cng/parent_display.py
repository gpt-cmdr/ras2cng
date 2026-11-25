"""Publish parent-model display layers as canonical GeoParquet and PMTiles.

fim-commander decides *which* layers a parent model has and whether each is
available; this module only publishes a layer it is handed. It adds no renderer:
GeoParquet goes through ``project._write_geoparquet`` and vector tiles through
``vector_tiles`` / ``pmtiles``. Every refusal carries a stable reason code so a
caller can record why a layer is not displayable instead of publishing a wrong
or empty one.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

FREESTILER = "freestiler"
TIPPECANOE = "tippecanoe"

# Dense mesh starts at the detail floor used by maplibre.py: complete mesh tiles
# are multi-megabyte below z13. Features are never dropped to fit a budget.
DETAIL_MIN_ZOOM = 13
DEFAULT_MAX_ZOOM = 14

REFUSAL_CODES = (
    "UNKNOWN_LAYER",
    "LAYER_EMPTY",
    "CRS_MISSING",
    "GEOMETRY_NULL",
    "GEOMETRY_TYPE_MISMATCH",
    "OUTPUT_EXISTS",
)


class PublishRefused(ValueError):
    """A layer cannot be published; ``reason_code`` is one of ``REFUSAL_CODES``."""

    def __init__(self, reason_code: str, message: str):
        super().__init__(f"{reason_code}: {message}")
        self.reason_code = reason_code


@dataclass(frozen=True)
class LayerSpec:
    """Display contract of one parent layer.

    ``tiled`` layers also get a PMTiles archive; small layers are GeoParquet only
    and are read directly by the viewer. ``geometry_types`` are the GeoJSON types
    the layer may hold; anything else is refused rather than drawn.
    """

    layer_id: str
    geometry_types: frozenset[str]
    tiled: bool
    min_zoom: int = 0


_POLYGONS = frozenset({"Polygon", "MultiPolygon"})
_LINES = frozenset({"LineString", "MultiLineString"})

PARENT_LAYERS: Mapping[str, LayerSpec] = {
    spec.layer_id: spec
    for spec in (
        LayerSpec("parent_domain", _POLYGONS, tiled=False),
        LayerSpec("parent_mesh", _POLYGONS, tiled=True, min_zoom=DETAIL_MIN_ZOOM),
        LayerSpec("parent_breaklines", _LINES, tiled=True),
        LayerSpec("parent_refinement", _POLYGONS, tiled=False),
        LayerSpec("parent_structures", _LINES | _POLYGONS, tiled=False),
        LayerSpec("parent_connections", _LINES | _POLYGONS | {"Point"}, tiled=False),
    )
}


@dataclass(frozen=True)
class TilerChoice:
    """Resolved tiler and whether it is the preferred one."""

    backend: str
    preferred: str
    fallback_reason: str | None


def choose_tiler(prefer: str = FREESTILER) -> TilerChoice:
    """Resolve the vector tiler: ``prefer`` when installed, else the repo default.

    Freestiler is requested for parent display (in-process, fast, keeps shared
    mesh edges coincident). Where it is not installed the existing
    ``RAS2CNG_VECTOR_TILER`` resolution applies, and the reason is recorded so a
    receipt never claims a backend that did not run.
    """
    from ras2cng import vector_tiles

    if prefer == FREESTILER and vector_tiles.freestiler_available():
        return TilerChoice(FREESTILER, prefer, None)
    backend = vector_tiles.vector_tiler_backend()
    reason = None if backend == prefer else f"{prefer} unavailable; using {backend}"
    return TilerChoice(backend, prefer, reason)


TileWriter = Callable[..., None]


def _default_tile_writer(
    *,
    output: Path,
    frame,
    geoparquet: Path,
    layer_id: str,
    backend: str,
    min_zoom,
    max_zoom,
) -> None:
    from ras2cng import pmtiles, vector_tiles

    if backend == FREESTILER:
        wgs84 = pmtiles._to_wgs84(frame, geoparquet)
        vector_tiles.run_freestiler(output, {layer_id: wgs84}, min_zoom, max_zoom)
    else:
        pmtiles.generate_vector_pmtiles(
            geoparquet, output, layer_id, min_zoom, max_zoom
        )


def validate_layer_frame(frame, spec: LayerSpec) -> None:
    """Refuse an empty, CRS-less, null-geometry or wrong-type layer. Mutates nothing."""
    if len(frame) == 0:
        raise PublishRefused("LAYER_EMPTY", f"{spec.layer_id} has no features")
    if frame.crs is None:
        raise PublishRefused(
            "CRS_MISSING", f"{spec.layer_id} has no CRS; it cannot be tiled"
        )
    geometry = frame.geometry
    if geometry.isna().any() or geometry.is_empty.any():
        raise PublishRefused(
            "GEOMETRY_NULL", f"{spec.layer_id} has null or empty geometry"
        )
    found = set(geometry.geom_type)
    if not found <= spec.geometry_types:
        raise PublishRefused(
            "GEOMETRY_TYPE_MISMATCH",
            f"{spec.layer_id} holds {sorted(found)}; expected {sorted(spec.geometry_types)}",
        )


def publish_parent_layer(
    frame,
    layer_id: str,
    output_dir: Path,
    *,
    prefer_tiler: str = FREESTILER,
    max_zoom: int = DEFAULT_MAX_ZOOM,
    tile_writer: TileWriter | None = None,
) -> dict:
    """Write ``<layer_id>.geoparquet`` (and ``.pmtiles`` for tiled layers).

    Parameters
    ----------
    frame : GeoDataFrame in the model's native projected CRS; units are preserved.
        GeoParquet keeps that CRS; PMTiles are reprojected to EPSG:4326 for tiling.
    output_dir : a fresh unit or attempt directory (created if missing). The call
        refuses with ``OUTPUT_EXISTS``, before writing anything, when this layer's final
        GeoParquet or PMTiles name is already there, so a completed artifact can never
        be overwritten. Nothing here is atomic across the two outputs: if the tiler
        fails, files this call created are removed on a best-effort basis and the error
        propagates, but a directory is never publication-ready without a completed
        receipt written by the caller after every layer returned successfully.
        The source frame is not modified.
    tile_writer : override for tests; receives keyword arguments ``output, frame,
        geoparquet, layer_id, backend, min_zoom, max_zoom``.

    Returns
    -------
    dict with ``layer_id``, ``feature_count``, ``crs`` (verbatim authority string
    when one exists), ``geoparquet`` and optional ``pmtiles`` file names (relative),
    and ``tiler`` (backend, preferred, fallback_reason) for tiled layers.

    Raises
    ------
    PublishRefused with a stable ``reason_code``.
    """
    spec = PARENT_LAYERS.get(layer_id)
    if spec is None:
        raise PublishRefused(
            "UNKNOWN_LAYER", f"{layer_id!r} is not a parent display layer"
        )
    validate_layer_frame(frame, spec)

    from ras2cng.project import _write_geoparquet

    output_dir = Path(output_dir)
    geoparquet = output_dir / f"{layer_id}.geoparquet"
    pmtiles_path = output_dir / f"{layer_id}.pmtiles"
    targets = [geoparquet, pmtiles_path] if spec.tiled else [geoparquet]
    existing = [target.name for target in targets if target.exists()]
    if existing:
        raise PublishRefused(
            "OUTPUT_EXISTS",
            f"{existing} already in the output directory; use a fresh attempt directory",
        )
    choice = choose_tiler(prefer_tiler) if spec.tiled else None
    record: dict = {
        "layer_id": layer_id,
        "feature_count": int(len(frame)),
        "crs": frame.crs.to_string(),
        "geoparquet": geoparquet.name,
    }
    try:
        _write_geoparquet(frame, geoparquet)
        if choice is not None:
            (tile_writer or _default_tile_writer)(
                output=pmtiles_path,
                frame=frame,
                geoparquet=geoparquet,
                layer_id=layer_id,
                backend=choice.backend,
                min_zoom=spec.min_zoom,
                max_zoom=max_zoom,
            )
    except BaseException:
        # The refusal above proves these names were absent, so they are ours to remove.
        for target in targets:
            target.unlink(missing_ok=True)
        raise
    if choice is not None:
        record["pmtiles"] = pmtiles_path.name
        record["tiler"] = {
            "backend": choice.backend,
            "preferred": choice.preferred,
            "fallback_reason": choice.fallback_reason,
        }
    return record
