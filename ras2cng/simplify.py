"""Douglas-Peucker polygon simplification before vector tiling (and, opt-in, archiving).

RAS Mapper filters polygon outlines with Douglas-Peucker at 0.1 ft by default.
ras2cng applies the same first-pass filter to the **polygon** layers of the web
map before they are tiled: Manning's n land cover, 2D flow area perimeters,
refinement regions, storage areas, terrain modification and source footprint
polygons, model extents and Stored Map polygons. Some FEMA eBFE models carry a
single land-cover polygon of one million vertices (UPGU3), which dominated the
whole tiling run; at 0.1 ft the filter removes 54-62% of those vertices.

What is never simplified:

- ``mesh_cells`` and ``mesh_faces`` (:data:`NEVER_SIMPLIFY`): neighbouring
  cells share edges, and simplifying each cell on its own would open gaps and
  overlaps along them. The tilers keep those edges coincident.
- Lines and points. HEC-RAS lines (cross sections, centerlines, breaklines, BC
  and reference lines, structures) are digitized sparsely, so the filter has
  little to remove, and results are joined to them by identity.
- Layers in a geographic CRS (the tolerance is a length; they are skipped with a
  warning) and layers without a CRS.

Tolerance (``RAS2CNG_SIMPLIFY_TOLERANCE``, CLI ``--simplify-tolerance`` /
``--no-simplify``) is given in **feet** and converted to the layer's CRS units:
0.1 ft stays 0.1 in a US survey foot or international foot CRS and becomes
0.03048 m in a metre CRS. ``0`` or ``off`` turns the filter off. Web-map tiles
use :data:`DEFAULT_TILE_TOLERANCE_FT` (0.1 ft) by default; the archive GeoParquet
is the data of record and is simplified only when asked
(``ras2cng archive --simplify-tolerance``; the environment variable does not
apply to archives).

0.1 ft is also below what a tile can show: at the default maximum zoom (17) one
MVT coordinate unit (extent 4096) is about 0.12-0.13 ft at Texas latitudes, so
the tiles drawn from simplified polygons are visually unchanged.

Engines (``RAS2CNG_SIMPLIFY_ENGINE`` = ``auto`` | ``terrio`` | ``shapely``):
``auto`` uses ``terrio.simplify_wkb`` (Rust, parallel over features; the CLB
terrio build) when the installed terrio has it, otherwise shapely. Both run
GEOS's ``DouglasPeuckerSimplifier`` algorithm (shapely's
``simplify(..., preserve_topology=False)``) ring by ring and give the same
vertices:

- every output vertex is an input vertex; a ring may start at its second vertex
  when the first lies within the tolerance (as GEOS does);
- a ring that would be left with fewer than four points keeps its original
  vertices, so no polygon or hole disappears (GEOS would drop it);
- there is no repair step: GEOS rebuilds an invalid polygonal result with a
  zero-width buffer, which re-orders vertices; here, as in terrio, the
  simplified rings are returned as they are. At 0.1 ft this has not happened on
  the models measured.

Every simplified layer gets a record (method, tolerance in feet and in CRS
units, engine, vertices in and out, collapsed rings kept, seconds), written to
the viewer manifest (``simplification`` on the layer, ``vectorSimplification``
at the top) or to the archive manifest (``simplification`` on the geometry
entry).
"""

from __future__ import annotations

import contextlib
import contextvars
import os
import time
import warnings
from typing import Any, Iterator, Optional

import numpy as np
import shapely

TOLERANCE_ENV = "RAS2CNG_SIMPLIFY_TOLERANCE"
ENGINE_ENV = "RAS2CNG_SIMPLIFY_ENGINE"
DEFAULT_TILE_TOLERANCE_FT = 0.1
METHOD = "douglas-peucker"
FOOT_M = 0.3048
NEVER_SIMPLIFY = frozenset({"mesh_cells", "mesh_faces"})
TERRIO = "terrio"
SHAPELY = "shapely"
_OFF = {"0", "off", "none", "false", "no"}
_POLYGONAL = (3, 6)  # shapely type ids: Polygon, MultiPolygon

_OPTIONS: contextvars.ContextVar[dict] = contextvars.ContextVar("ras2cng_simplify_options", default={})


@contextlib.contextmanager
def simplify_options(*, tolerance_ft: Optional[float] = None, enabled: Optional[bool] = None) -> Iterator[None]:
    """Override the web-map simplification for the enclosed calls.

    ``enabled=False`` turns it off; ``tolerance_ft`` sets the tolerance in feet
    (``0`` turns it off). ``None`` leaves each to the environment or default.
    """

    current = dict(_OPTIONS.get())
    if tolerance_ft is not None:
        current["tolerance_ft"] = _checked_tolerance(tolerance_ft, "simplify tolerance")
    if enabled is not None:
        current["enabled"] = bool(enabled)
    token = _OPTIONS.set(current)
    try:
        yield
    finally:
        _OPTIONS.reset(token)


def _checked_tolerance(value: Any, source: str) -> Optional[float]:
    if isinstance(value, str) and value.strip().lower() in _OFF:
        return None
    try:
        tolerance = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{source} must be a non-negative number of feet or 'off'; got {value!r}") from None
    if not np.isfinite(tolerance) or tolerance < 0:
        raise ValueError(f"{source} must be a non-negative number of feet or 'off'; got {value!r}")
    return tolerance or None


def tile_tolerance_ft() -> Optional[float]:
    """The web-map tile tolerance in feet, or None when simplification is off."""

    options = _OPTIONS.get()
    if options.get("enabled") is False:
        return None
    if "tolerance_ft" in options:
        return options["tolerance_ft"]
    raw = os.environ.get(TOLERANCE_ENV, "").strip()
    if not raw:
        return DEFAULT_TILE_TOLERANCE_FT
    return _checked_tolerance(raw, TOLERANCE_ENV)


def terrio_simplify() -> Any:
    """``terrio.simplify_wkb`` when the installed terrio has it, else None."""

    try:
        import terrio
    except Exception:
        return None
    return getattr(terrio, "simplify_wkb", None)


def resolve_engine(engine: Optional[str] = None) -> str:
    """``terrio`` or ``shapely``, from ``engine`` or ``RAS2CNG_SIMPLIFY_ENGINE``."""

    choice = (engine or os.environ.get(ENGINE_ENV, "") or "auto").strip().lower()
    if choice in {"auto", "prefer-native"}:
        return TERRIO if terrio_simplify() is not None else SHAPELY
    if choice in {TERRIO, "native", "rust"}:
        if terrio_simplify() is None:
            raise RuntimeError(
                f"{ENGINE_ENV}={choice} needs a terrio build with simplify_wkb (the CLB terrio build); "
                "use auto or shapely"
            )
        return TERRIO
    if choice in {SHAPELY, "python", "geos"}:
        return SHAPELY
    raise ValueError(f"{ENGINE_ENV} must be auto, terrio or shapely; got {choice!r}")


def tolerance_in_crs_units(crs: Any, tolerance_ft: float) -> Optional[tuple[float, str]]:
    """The tolerance in the CRS's linear unit and the unit's name; None for geographic CRSs.

    A foot CRS (US survey or international) takes the value as given; any other
    linear unit gets it converted from international feet (0.1 ft = 0.03048 m).
    """

    import pyproj

    crs = pyproj.CRS.from_user_input(crs)
    if crs.is_geographic or not crs.axis_info:
        return None
    axis = crs.axis_info[0]
    unit = axis.unit_name or ""
    factor = axis.unit_conversion_factor
    if not factor or not np.isfinite(factor):
        return None
    if "foot" in unit.lower() or "feet" in unit.lower():
        return float(tolerance_ft), unit
    if unit.lower() in {"degree", "radian", "grad", "arc-second"}:
        return None
    return float(tolerance_ft) * FOOT_M / factor, unit


def _shapely_polygons(geoms: np.ndarray, tolerance: float) -> tuple[np.ndarray, dict]:
    """GEOS's Douglas-Peucker ring by ring, keeping rings that would collapse."""

    parts, part_of = shapely.get_parts(geoms, return_index=True)
    rings, ring_of = shapely.get_rings(parts, return_index=True)
    simple = shapely.simplify(rings, tolerance, preserve_topology=False)
    collapsed = (shapely.get_type_id(simple) != 2) | (shapely.get_num_coordinates(simple) < 4)
    simple = np.where(collapsed, rings, simple)
    polygons = np.empty(len(parts), dtype=object)
    starts = np.searchsorted(ring_of, np.arange(len(parts) + 1))
    for p in range(len(parts)):
        members = simple[starts[p]:starts[p + 1]]
        polygons[p] = shapely.Polygon(members[0], list(members[1:])) if len(members) else shapely.Polygon()
    out = geoms.copy()
    part_starts = np.searchsorted(part_of, np.arange(len(geoms) + 1))
    for g in range(len(geoms)):
        members = polygons[part_starts[g]:part_starts[g + 1]]
        if shapely.get_type_id(geoms[g]) == 6:
            out[g] = shapely.MultiPolygon(list(members))
        elif len(members):
            out[g] = members[0]
    stats = {
        "vertices_in": int(shapely.get_num_coordinates(geoms).sum()),
        "vertices_out": int(shapely.get_num_coordinates(out).sum()),
        "rings_kept": int(collapsed.sum()),
    }
    return out, stats


def simplify_polygons(geoms: np.ndarray, tolerance: float, engine: str) -> tuple[np.ndarray, dict]:
    """Douglas-Peucker on an array of non-empty Polygon/MultiPolygon geometries."""

    if engine == TERRIO:
        wkb = shapely.to_wkb(geoms)
        out, stats = terrio_simplify()(list(wkb), tolerance, stats=True)
        return shapely.from_wkb(np.asarray(out, dtype=object)), {
            "vertices_in": int(stats["vertices_in"]),
            "vertices_out": int(stats["vertices_out"]),
            "rings_kept": int(stats["rings_kept"]),
        }
    return _shapely_polygons(geoms, tolerance)


_WARNED: set[str] = set()


def _warn_once(message: str) -> None:
    if message not in _WARNED:
        _WARNED.add(message)
        warnings.warn(message, stacklevel=3)


def simplify_frame(
    gdf: Any,
    *,
    kind: str,
    tolerance_ft: Optional[float],
    fallback_crs: Any = None,
    engine: Optional[str] = None,
) -> tuple[Any, Optional[dict]]:
    """Simplify the polygons of one layer in its own CRS.

    Returns the frame (a copy when anything changed) and a record, or None when
    nothing was simplified: tolerance off, a never-simplified kind, no polygons,
    or no projected CRS (skipped with a warning).
    """

    if tolerance_ft is None or kind in NEVER_SIMPLIFY or gdf is None or not len(gdf):
        return gdf, None
    geoms = np.asarray(gdf.geometry.values, dtype=object)
    mask = np.isin(shapely.get_type_id(geoms), _POLYGONAL) & ~shapely.is_empty(geoms)
    if not mask.any():
        return gdf, None
    crs = gdf.crs or fallback_crs
    if crs is None:
        _warn_once(f"Polygon simplification skipped for {kind}: the layer has no CRS")
        return gdf, None
    converted = tolerance_in_crs_units(crs, tolerance_ft)
    if converted is None:
        import pyproj

        name = pyproj.CRS.from_user_input(crs).name
        _warn_once(f"Polygon simplification skipped for {kind}: CRS {name!r} is not projected")
        return gdf, None
    tolerance, unit = converted
    chosen = resolve_engine(engine)
    started = time.perf_counter()
    simple, stats = simplify_polygons(geoms[mask], tolerance, chosen)
    import geopandas as gpd

    values = geoms.copy()
    values[mask] = simple
    out = gdf.copy()
    out[gdf.geometry.name] = gpd.GeoSeries(values, index=gdf.index, crs=gdf.crs)
    record = {
        "method": METHOD,
        "tolerance_ft": float(tolerance_ft),
        "tolerance": tolerance,
        "units": unit,
        "engine": chosen,
        "features": int(mask.sum()),
        "vertices_in": stats["vertices_in"],
        "vertices_out": stats["vertices_out"],
        "rings_kept": stats["rings_kept"],
        "seconds": round(time.perf_counter() - started, 4),
    }
    return out, record


def simplify_layers(frame: Any, *, tolerance_ft: Optional[float], fallback_crs: Any = None, engine: Optional[str] = None) -> tuple[Any, list[dict]]:
    """:func:`simplify_frame` on each layer of a merged frame with a ``layer`` column."""

    if tolerance_ft is None or frame is None or not len(frame) or "layer" not in frame.columns:
        return frame, []
    records: list[dict] = []
    geometry: Optional[np.ndarray] = None
    for kind in list(dict.fromkeys(frame["layer"])):
        rows = np.flatnonzero((frame["layer"] == kind).to_numpy())
        part, record = simplify_frame(
            frame.iloc[rows], kind=str(kind), tolerance_ft=tolerance_ft, fallback_crs=fallback_crs, engine=engine
        )
        if record is None:
            continue
        if geometry is None:
            geometry = np.asarray(frame.geometry.values, dtype=object).copy()
        geometry[rows] = np.asarray(part.geometry.values, dtype=object)
        records.append({"layer": str(kind), **record})
    if geometry is None:
        return frame, records
    import geopandas as gpd

    out = frame.copy()
    out[frame.geometry.name] = gpd.GeoSeries(geometry, index=frame.index, crs=frame.crs)
    return out, records


def camel_record(record: dict) -> dict:
    """A record with the viewer manifest's camelCase keys."""

    def camel(key: str) -> str:
        head, *rest = key.split("_")
        return head + "".join(word.capitalize() for word in rest)

    return {camel(key): value for key, value in record.items()}
