"""Direct archive path: HEC-RAS HDF straight to archive GeoParquet (manifest 3.0).

``terrio._terrio.write_hecras_archive`` reads a geometry or plan HDF through the
fast-hdf Arrow layers and writes the archive profile with no GeoDataFrame in
between. Its catalog does not hold everything an archive holds, so this module
decides, per file, what comes from where, and records it:

- **Native:** ``mesh_cells`` and ``mesh_faces`` of a geometry file, and the 2D
  summary variables of a plan file. These are the expensive layers, and their
  columns and values equal the Python path's.
- **Python, through ``extra_layers``:** every other geometry layer and every
  ``*_text`` layer. The native catalog omits ``river_reaches``, the joined
  ``structures`` and ``cross_sections`` columns and ``centerlines`` ``length``;
  it has no text layers; it stores some columns as float32/uint64 where the
  Python frames hold float64/int64; and it refuses a whole breakline family over
  one bad row where ras-commander skips that row. These layers read in about
  0.1 s per file, so nothing is gained by risking them.
- **Refused by the engine** (``skipped``; refusals are returned, not raised):
  filled from Python through ``extra_layers``. What Python cannot read either is
  absent from a Python-path archive too, and is recorded as unfilled.
- **Anything else** that goes wrong falls back to the Python path for that file
  (``fallback_reason``). Nothing is dropped silently.

``RAS2CNG_ARCHIVE_ENGINE`` = ``auto`` (default) | ``prefer-native`` | ``native`` |
``python``. ``auto`` uses the direct path when terrio has it, unless
``RAS2CNG_GEOPARQUET_WRITER=python`` asks for pyarrow. ``native`` (aliases
``direct``, ``terrio``, ``rust``) raises instead of falling back to the Python
path for a whole file; a refused layer is still filled and recorded, because
leaving it out would lose data. ``python`` never tries it. Without the capability
the output is what it was: Python path, manifest 2.7, no ``archive`` records.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Sequence

import pyarrow as pa
import pyarrow.parquet as pq

from ras2cng.geoparquet import (
    BBOX_COLUMN,
    COVERING_STRUCT,
    ENGINE_NATIVE,
    WRITER_ENV,
    _arrow_table,
    _layer_identities,
    _layer_slices,
    concat_layers,
    drop_covering_columns,
)
from ras2cng.spatial_index import HILBERT_COLUMN

ENGINE_ENV = "RAS2CNG_ARCHIVE_ENGINE"
PATH_DIRECT = "direct"
PATH_PYTHON = "python"

#: What ``auto`` means: the direct path when available (``True``), or always the
#: Python path (``False``). The one switch for the default.
AUTO_PREFERS_DIRECT = True
PREFER_NATIVE = "prefer-native"

#: Geometry layers the engine writes. Every other layer comes from Python.
NATIVE_GEOMETRY_LAYERS = ("mesh_cells", "mesh_faces")


class DirectArchiveRequired(RuntimeError):
    """``RAS2CNG_ARCHIVE_ENGINE=native``, and the direct path cannot do the file."""


class _Fallback(Exception):
    """This file goes to the Python path, for the stated reason."""


def direct_reason() -> Optional[str]:
    """None when terrio has the direct path, else why not."""

    try:
        from terrio import _terrio
    except Exception as exc:
        return f"terrio is not installed ({type(exc).__name__}: {exc})"
    if not callable(getattr(_terrio, "write_hecras_archive", None)):
        return "the installed terrio has no write_hecras_archive"
    if not callable(getattr(_terrio, "hecras_archive_layers", None)):
        return "the installed terrio has no hecras_archive_layers"
    return None


@dataclass(frozen=True)
class ArchiveEngine:
    """The resolved engine setting for one archive run."""

    requested: str  # native, prefer-native or python
    reason: str = ""  # why the direct path is off, when it is

    @property
    def active(self) -> bool:
        return self.requested != "python" and not self.reason

    @property
    def strict(self) -> bool:
        return self.requested == "native"

    def not_applicable(self, reason: str) -> dict:
        """The record of a file the engine has nothing to do with; never raises."""

        return {"path": PATH_PYTHON, "fallback_reason": reason}

    def fallback(self, reason: str) -> dict:
        """The record of a file that went to the Python path; raises when strict."""

        if self.strict:
            raise DirectArchiveRequired(f"{ENGINE_ENV}=native: {reason}")
        return {"path": PATH_PYTHON, "fallback_reason": reason}


def resolve_engine(engine: Optional[str] = None) -> ArchiveEngine:
    """Resolve the argument or ``RAS2CNG_ARCHIVE_ENGINE``; strict without capability raises."""

    explicit = (engine or os.environ.get(ENGINE_ENV, "") or "auto").strip().lower()
    aliases = {"direct": "native", "terrio": "native", "rust": "native", "pyarrow": "python"}
    choice = aliases.get(explicit, explicit)
    if choice not in {"auto", "native", PREFER_NATIVE, "python"}:
        raise ValueError(
            f"{ENGINE_ENV} must be one of auto, native, {PREFER_NATIVE}, python; got {explicit!r}"
        )
    if choice == "python":
        return ArchiveEngine("python", "python engine requested")
    reason = direct_reason()
    if choice == "native":
        if reason:
            raise DirectArchiveRequired(f"{ENGINE_ENV}=native: {reason}")
        return ArchiveEngine("native")
    if choice == "auto":
        if not AUTO_PREFERS_DIRECT:
            return ArchiveEngine("python", "auto prefers the Python path")
        writer = os.environ.get(WRITER_ENV, "").strip().lower()
        if writer in {"python", "pyarrow"}:
            return ArchiveEngine("python", f"{WRITER_ENV}={writer}")
    return ArchiveEngine(PREFER_NATIVE, reason or "")


def fallback_text(exc: Exception) -> str:
    """The manifest reason for a file that went to the Python path."""

    return str(exc) if isinstance(exc, _Fallback) else f"{type(exc).__name__}: {exc}"


def options_reason(*, sort: bool) -> Optional[str]:
    """Why the requested options rule out the direct path for every file."""

    return None if sort else "--no-sort: the direct path always writes Hilbert order"


def plan_options_reason(*, results_layout: str, results_geometry: str) -> Optional[str]:
    """Why the requested results options rule out the direct path for plan files."""

    if results_layout != "plan":
        return f"results_layout={results_layout}: the direct path writes one file per plan"
    if results_geometry != "polygon":
        return f"results_geometry={results_geometry}: the direct path joins element geometry"
    return None


@dataclass
class DirectWrite:
    """One file written by the direct path."""

    geoparquet: dict  # the manifest writer record
    archive: dict  # the manifest ``archive`` record
    layers: list[dict] = field(default_factory=list)  # from summarize_layers


# ---------------------------------------------------------------------------
# What the written file holds
# ---------------------------------------------------------------------------


def summarize_layers(path: Path) -> list[dict]:
    """Per layer of an archive file, from the file: rows, geometry type, join key.

    Row groups end at layer boundaries, so the ``layer`` chunk statistics give
    the row counts and only each layer's first row group is read.
    """

    import shapely

    parquet = pq.ParquetFile(path)
    metadata = parquet.metadata
    names = parquet.schema_arrow.names
    wanted = [c for c in ("layer", "geometry", "cell_id", "face_id") if c in names]
    leaf = {
        metadata.row_group(0).column(i).path_in_schema: i for i in range(metadata.num_columns)
    } if metadata.num_row_groups else {}

    layers: dict[str, dict] = {}
    for index in range(metadata.num_row_groups):
        group = metadata.row_group(index)
        stats = group.column(leaf["layer"]).statistics
        if stats is None or not stats.has_min_max or stats.min != stats.max:
            raise _Fallback(f"{path.name}: row group {index} does not hold one layer")
        name = str(stats.min)
        entry = layers.setdefault(name, {"layer": name, "rows": 0, "first_group": index})
        entry["rows"] += group.num_rows

    out = []
    for entry in layers.values():
        first = parquet.read_row_group(entry.pop("first_group"), columns=wanted)
        wkb = first["geometry"].drop_null()
        # None for a layer without geometry, as the Python path records it.
        entry["geometry_type"] = (
            _TYPE_NAMES.get(int(shapely.get_type_id(shapely.from_wkb(wkb[0].as_py()))), "Unknown")
            if len(wkb)
            else None
        )
        entry["index_column"], entry["geometry_filter"] = "", ""
        if "face_id" in first.column_names and first["face_id"].null_count < first.num_rows:
            entry["index_column"], entry["geometry_filter"] = "face_id", "mesh_faces"
        elif "cell_id" in first.column_names and first["cell_id"].null_count < first.num_rows:
            entry["index_column"], entry["geometry_filter"] = "cell_id", "mesh_cells"
        out.append(entry)
    return out


_TYPE_NAMES = {
    0: "Point",
    1: "LineString",
    2: "LinearRing",
    3: "Polygon",
    4: "MultiPoint",
    5: "MultiLineString",
    6: "MultiPolygon",
    7: "GeometryCollection",
}


def read_mesh_cells(path: Path):
    """The ``mesh_cells`` layer of a written archive file, for a Python result join."""

    import geopandas as gpd

    frame = gpd.read_parquet(path, filters=[("layer", "==", "mesh_cells")])
    frame = frame.drop(columns=[c for c in ("layer", BBOX_COLUMN, HILBERT_COLUMN) if c in frame])
    empty = [c for c in frame.columns if c != "geometry" and frame[c].isna().all()]
    return frame.drop(columns=empty).reset_index(drop=True)


def stored_crs(path: Path) -> Any:
    """The ``crs`` of a GeoParquet's ``geo`` metadata, or None."""

    raw = (pq.read_schema(path).metadata or {}).get(b"geo")
    if not raw:
        return None
    meta = json.loads(raw)
    return meta.get("columns", {}).get(meta.get("primary_column", "geometry"), {}).get("crs")


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


def _file_crs(hdf_path: Path, frame, project_crs: Optional[str]) -> tuple[Any, str]:
    """The CRS the Python path would give this file, as PROJJSON, and its source.

    The engine reads only a ``Projection`` stored in the file, which most
    geometry HDFs do not have, and writes ``crs: null`` without complaint. So it
    is always passed: the Python frames' CRS, else the file's, else the project's.
    """

    from pyproj import CRS

    if frame is not None and getattr(frame, "crs", None) is not None:
        return frame.crs.to_json_dict(), "python-layers"
    try:
        from ras_commander.hdf import HdfBase

        stored = HdfBase.get_projection(hdf_path)
    except Exception:
        stored = None
    if stored:
        return CRS.from_user_input(stored).to_json_dict(), "stored"
    if project_crs:
        return CRS.from_user_input(project_crs).to_json_dict(), "project"
    return None, "none"


def _extra_layers(frame) -> tuple[list[tuple[str, pa.Table]], dict[str, list[str]]]:
    """``extra_layers`` and ``extra_identity`` from a merged frame with a layer column.

    The tables share the merged frame's schema, so a column one Python layer
    holds as int and another as float is unified the way the Python path does.
    """

    if frame is None or not len(frame):
        return [], {}
    frame = drop_covering_columns(frame)
    frame = frame[[c for c in frame.columns if c != "layer"] + ["layer"]]
    slices = _layer_slices(frame, "layer")
    table = _arrow_table(frame)
    identities = _layer_identities(table, slices, "layer")
    base = table.drop_columns(["layer"]).replace_schema_metadata(None)
    extras = [(name, base.take(pa.array(rows))) for name, rows in slices]
    identity = {
        name: [c for c in ident if c] for (name, _), ident in zip(slices, identities) if ident
    }
    return extras, identity


def _refusal(item: dict, *, filled: bool) -> dict:
    return {
        "layer": item.get("layer"),
        "variable": item.get("variable"),
        "code": item.get("code"),
        "reason": item.get("reason"),
        "filled": filled,
    }


def _result(
    report: dict,
    output: Path,
    *,
    engine: ArchiveEngine,
    native: Sequence[str],
    python_layers: dict[str, str],
    refused: list[dict],
    crs_source: str,
    started: float,
) -> DirectWrite:
    layers = summarize_layers(output)
    written = {layer["layer"]: layer["rows"] for layer in layers}
    expected = dict(report.get("layer_rows") or {})
    lost = {k: v for k, v in expected.items() if written.get(k) != v}
    missing = sorted(set(python_layers) - set(written))
    if lost or missing or sum(written.values()) != int(report.get("rows", -1)):
        output.unlink(missing_ok=True)
        raise _Fallback(
            f"written file does not match the report: rows by layer {lost}, missing {missing}"
        )
    return DirectWrite(
        geoparquet={
            "engine": ENGINE_NATIVE,
            "fallback_reason": "",
            "hilbert": ENGINE_NATIVE,
            "hilbert_column": HILBERT_COLUMN,
            "bbox_covering": COVERING_STRUCT,
            "rows": int(report["rows"]),
            "row_groups": int(report["row_groups"]),
        },
        archive={
            "path": PATH_DIRECT,
            "engine": engine.requested,
            "fallback_reason": "",
            "native_layers": list(native),
            "python_layers": python_layers,
            "refused": refused,
            "crs_source": crs_source,
            "seconds": round(time.perf_counter() - started, 3),
        },
        layers=layers,
    )


def write_geometry(
    hdf_path: Path,
    text_path: Optional[Path],
    output: Path,
    *,
    project_crs: Optional[str],
    engine: ArchiveEngine,
) -> Optional[DirectWrite]:
    """Write one geometry file directly; None when there is nothing to write.

    Raises when the file must go to the Python path; the caller records why.
    """

    from terrio import _terrio

    from ras2cng.geometry import ALL_HDF_LAYERS, merge_all_layers

    started = time.perf_counter()
    frame = merge_all_layers(
        hdf_path,
        text_path,
        sort=False,
        hdf_layers=[n for n in ALL_HDF_LAYERS if n not in NATIVE_GEOMETRY_LAYERS],
    )
    crs, crs_source = _file_crs(hdf_path, frame, project_crs)
    python_layers = _python_layer_reasons(frame)

    native = list(NATIVE_GEOMETRY_LAYERS)
    refused: list[dict] = []
    kwargs = dict(
        geometry=True,
        results=False,
        ghost_cell_points=False,
        time_days=False,
        crs=crs,
    )
    extras, identity = _extra_layers(frame)
    report = dict(
        _terrio.write_hecras_archive(
            str(hdf_path),
            str(output),
            layers=native,
            extra_layers=extras or None,
            extra_identity=identity or None,
            **kwargs,
        )
    )
    skipped = [dict(item) for item in report.get("skipped") or []]
    has_mesh = frame is not None and (frame["layer"] == "mesh_areas").any()
    written_native = set(report.get("layer_rows") or {})
    for name in native:
        if name not in written_native and has_mesh and not any(s.get("layer") == name for s in skipped):
            # The Python path has a mesh here, and the engine neither wrote this
            # layer nor said why. Do not trust the file.
            output.unlink(missing_ok=True)
            raise _Fallback(f"the engine wrote no {name} for a file with a 2D mesh")

    if skipped:
        # A refusal is only a gap until Python fills it. Rewrite with the filled
        # layers ahead of the others, the order the Python path uses.
        names = [s.get("layer") for s in skipped]
        filled = merge_all_layers(hdf_path, None, sort=False, hdf_layers=names)
        filled_names = set(filled["layer"]) if filled is not None else set()
        for item in skipped:
            refused.append(_refusal(item, filled=item.get("layer") in filled_names))
        if filled_names:
            frame = concat_layers([f for f in (filled, frame) if f is not None])
            python_layers = {**_python_layer_reasons(frame), **{n: "refused" for n in filled_names}}
            extras, identity = _extra_layers(frame)
        native = [n for n in native if n in written_native and n not in names]
        report = dict(
            _terrio.write_hecras_archive(
                str(hdf_path),
                str(output),
                layers=native,
                extra_layers=extras or None,
                extra_identity=identity or None,
                **kwargs,
            )
        )
        if report.get("skipped"):
            raise _Fallback(f"the engine refused layers on the second pass: {report['skipped']}")

    if not int(report.get("rows", 0)):
        output.unlink(missing_ok=True)
        return None
    return _result(
        report,
        output,
        engine=engine,
        native=native,
        python_layers=python_layers,
        refused=refused,
        crs_source=crs_source,
        started=started,
    )


def _python_layer_reasons(frame) -> dict[str, str]:
    if frame is None or not len(frame):
        return {}
    return {
        str(name): "text" if str(name).endswith("_text") else "python-layer"
        for name in frame["layer"].unique()
    }


def write_plan(
    plan_hdf: Path,
    output: Path,
    *,
    selected: Sequence[str],
    crs_from: Optional[Path],
    project_crs: Optional[str],
    mesh_cells,
    engine: ArchiveEngine,
) -> Optional[DirectWrite]:
    """Write one plan's 2D summary variables directly.

    ``selected`` are the stored variable names the Python path would export.
    ``mesh_cells`` is a callable returning the mesh-cell frame the Python path
    joins to; it is called only when a refused variable has to be read in Python.
    Raises when the file must go to the Python path.
    """

    from terrio import _terrio

    from ras2cng.results import merge_all_variables, result_variable_slug

    started = time.perf_counter()
    selected = list(selected)
    if not selected:
        return None

    # Refusals are known without building any geometry, so the file is written once.
    found = dict(
        _terrio.hecras_archive_layers(
            str(plan_hdf),
            geometry=False,
            results=True,
            variables=selected,
            summary_geometry=False,
            ghost_cell_points=False,
            time_days=False,
        )
    )
    native = [str(layer["variable"]) for layer in found["layers"]]
    layer_names = {str(layer["variable"]): str(layer["name"]) for layer in found["layers"]}
    skipped = [dict(item) for item in found.get("skipped") or []]
    del found
    if not native:
        raise _Fallback("the engine refused every summary variable")

    # A variable the engine neither wrote nor refused is still read in Python.
    named = {s.get("variable") for s in skipped}
    for variable in selected:
        if variable not in native and variable not in named:
            skipped.append(
                {
                    "layer": result_variable_slug(variable),
                    "variable": variable,
                    "code": "absent",
                    "reason": "not returned by the engine",
                }
            )
    wanted = [v for v in selected if v not in native]
    frame = None
    if wanted:
        frame = merge_all_variables(
            plan_hdf, mesh_cells_gdf=mesh_cells(), variables=wanted, geometry_mode="polygon"
        )
        if frame is not None and not len(frame):
            frame = None
    filled = set(frame["layer"].unique()) if frame is not None else set()
    refused = [
        _refusal(
            item,
            filled=result_variable_slug(str(item.get("variable") or item.get("layer"))) in filled,
        )
        for item in skipped
        if item.get("variable") in selected
    ]
    crs = stored_crs(crs_from) if crs_from else None
    crs_source = "geometry-file" if crs is not None else ""
    if crs is None:
        crs, crs_source = _file_crs(plan_hdf, frame, project_crs)

    extras, identity = _extra_layers(frame)
    order = [
        layer_names.get(v) or result_variable_slug(v)
        for v in selected
        if v in layer_names or result_variable_slug(v) in filled
    ]
    report = dict(
        _terrio.write_hecras_archive(
            str(plan_hdf),
            str(output),
            geometry=False,
            results=True,
            variables=native,
            extra_layers=extras or None,
            extra_identity=identity or None,
            order=order,
            summary_geometry=True,
            ghost_cell_points=False,
            time_days=False,
            crs=crs,
        )
    )
    late = [s for s in report.get("skipped") or [] if s.get("variable") in native]
    if late:
        raise _Fallback(f"the engine refused variables only when joining geometry: {late}")
    return _result(
        report,
        output,
        engine=engine,
        native=[layer_names[v] for v in native],
        python_layers={str(n): "refused" for n in filled},
        refused=refused,
        crs_source=crs_source,
        started=started,
    )
