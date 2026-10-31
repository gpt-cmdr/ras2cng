"""The direct archive path: engine selection, fallback, nothing lost, manifest 3.0.

Most tests use a stand-in engine, so refusals and failures can be forced. The
``real_*`` tests run the real engine on HEC-RAS models when both are available
(fork terrio with ``write_hecras_archive``; ``RAS2CNG_TEST_CORPUS``).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import geopandas as gpd
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import shapely
from shapely.geometry import LineString, Polygon

from ras2cng import archive_direct as direct
from ras2cng.catalog import PYTHON_PATH_SCHEMA_VERSION, SCHEMA_VERSION, Manifest
from ras2cng.geoparquet import write_geoparquet
from ras2cng.project import archive_project
from ras2cng.spatial_index import HILBERT_COLUMN

CORPUS = Path(os.environ.get("RAS2CNG_TEST_CORPUS", "G:/RustDev/data/corpus"))
CRS = "EPSG:2271"


def _cells(n=3):
    return gpd.GeoDataFrame(
        {"mesh_name": ["M"] * n, "cell_id": list(range(n))},
        geometry=[Polygon([(i, 0), (i + 1, 0), (i + 1, 1)]) for i in range(n)],
        crs=CRS,
    )


def _faces(n=2):
    return gpd.GeoDataFrame(
        {"mesh_name": ["M"] * n, "face_id": list(range(n))},
        geometry=[LineString([(i, 0), (i, 1)]) for i in range(n)],
        crs=CRS,
    )


def _summary(name, n=3):
    frame = _cells(n)
    frame[name] = [float(i) for i in range(n)]
    return frame


def _python_geometry(hdf_path, text_path, *, sort=True, hdf_layers=None):
    """Stands in for merge_all_layers: mesh areas, bc lines, one text layer."""
    frames = []
    for name in hdf_layers if hdf_layers is not None else ["mesh_cells", "mesh_faces", "mesh_areas", "bc_lines"]:
        if name == "mesh_cells":
            frames.append(_cells().assign(layer=name))
        elif name == "mesh_faces":
            frames.append(_faces().assign(layer=name))
        elif name == "mesh_areas":
            frames.append(
                gpd.GeoDataFrame(
                    {"mesh_name": ["M"], "layer": [name]},
                    geometry=[Polygon([(0, 0), (9, 0), (9, 9)])],
                    crs=CRS,
                )
            )
        elif name == "bc_lines":
            frames.append(
                gpd.GeoDataFrame(
                    {"bc_line_id": [0], "Name": ["up"], "Length": [12.5], "layer": [name]},
                    geometry=[LineString([(0, 0), (1, 1)])],
                    crs=CRS,
                )
            )
    if text_path is not None:
        frames.append(
            gpd.GeoDataFrame(
                {"Name": ["sa"], "layer": ["storage_areas_text"]},
                geometry=[Polygon([(0, 0), (2, 0), (2, 2)])],
                crs=CRS,
            )
        )
    if not frames:
        return None
    return gpd.GeoDataFrame(pd.concat(frames, ignore_index=True), geometry="geometry", crs=CRS)


class FakeEngine:
    """``terrio._terrio`` with scripted refusals, written by the pyarrow engine."""

    def __init__(self):
        self.calls = []
        self.geometry_layers = {"mesh_cells": _cells(), "mesh_faces": _faces()}
        self.variables = {"Maximum Depth": _summary("maximum_depth")}
        self.refuse_layers = {}  # layer -> reason
        self.refuse_variables = {}  # stored variable -> reason
        self.silently_omit = set()
        self.raise_on_write = None
        self.misreport_rows = 0

    def hecras_archive_layers(self, path, *, geometry=True, results=True, variables=None, **kwargs):
        layers = []
        for name in variables or []:
            if name in self.variables and name not in self.refuse_variables:
                layers.append(
                    {"name": name.lower().replace(" ", "_"), "variable": name, "kind": "summary"}
                )
        return {"layers": layers, "skipped": self._skipped(variables or [], [])}

    def _skipped(self, variables, layers):
        out = [
            {"layer": n, "variable": None, "kind": "unsupported", "code": "x", "reason": self.refuse_layers[n]}
            for n in layers
            if n in self.refuse_layers
        ]
        for v in variables:
            if v in self.refuse_variables:
                out.append(
                    {
                        "layer": v.lower().replace(" ", "_"),
                        "variable": v,
                        "kind": "unsupported",
                        "code": "schema",
                        "reason": self.refuse_variables[v],
                    }
                )
        return out

    def write_hecras_archive(
        self, path, out, *, extra_layers=None, extra_identity=None, order=None, crs=None,
        geometry=True, results=True, layers=None, variables=None, **kwargs,
    ):
        self.calls.append(
            dict(path=path, extra=[n for n, _ in extra_layers or []], order=order, crs=crs,
                 geometry=geometry, results=results, layers=layers, variables=variables,
                 extra_identity=extra_identity, **kwargs)
        )
        if self.raise_on_write:
            raise self.raise_on_write
        frames, layer_rows = [], {}
        if geometry:
            for name in layers or []:
                if name in self.refuse_layers or name in self.silently_omit:
                    continue
                frames.append(self.geometry_layers[name].assign(layer=name))
                layer_rows[name] = len(self.geometry_layers[name])
        if results:
            for name in variables or []:
                if name in self.variables and name not in self.refuse_variables:
                    slug = name.lower().replace(" ", "_")
                    frames.append(self.variables[name].assign(layer=slug))
                    layer_rows[slug] = len(self.variables[name])
        for name, table in extra_layers or []:
            frame = pa.table(table).to_pandas()
            frame["geometry"] = shapely.from_wkb(frame["geometry"].to_numpy())
            frames.append(gpd.GeoDataFrame(frame, geometry="geometry", crs=CRS).assign(layer=name))
        if order:
            frames.sort(key=lambda frame: order.index(frame["layer"].iloc[0]))
        merged = gpd.GeoDataFrame(pd.concat(frames, ignore_index=True), geometry="geometry", crs=CRS)
        info = write_geoparquet(
            merged, Path(out), hilbert=True, hilbert_column=HILBERT_COLUMN, engine="python"
        )
        return {
            "rows": len(merged) + self.misreport_rows,
            "layers": merged["layer"].nunique(),
            "row_groups": info["row_groups"],
            "layer_rows": layer_rows,
            "skipped": self._skipped(variables or [], layers or []),
            "crs_wkt": None,
            "seconds": 0.0,
        }


@pytest.fixture
def engine(monkeypatch):
    fake = FakeEngine()
    fake_module = SimpleNamespace(_terrio=fake)
    monkeypatch.setitem(sys.modules, "terrio", fake_module)
    monkeypatch.setitem(sys.modules, "terrio._terrio", fake)
    monkeypatch.setenv("RAS2CNG_GEOPARQUET_WRITER", "python")
    with patch("ras2cng.geometry.merge_all_layers", side_effect=_python_geometry):
        yield fake


def _project(tmp_path, *, plan=False, text=False):
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    (project_dir / "Fake.prj").write_text("Proj Title=Fake\nEnglish Units\n")
    hdf = project_dir / "Fake.g01.hdf"
    hdf.touch()
    text_path = project_dir / "Fake.g01"
    if text:
        text_path.write_text("Geom Title=x\n")
    if plan:
        (project_dir / "Fake.p01.hdf").touch()
    ras = MagicMock()
    ras.project_name = "Fake"
    ras.project_folder = project_dir
    ras.geom_df = pd.DataFrame(
        {
            "geom_number": ["01"],
            "geom_title": ["G"],
            "full_path": [str(text_path)],
            "hdf_path": [str(hdf)],
        }
    )
    ras.plan_df = pd.DataFrame(
        {
            "plan_number": ["01"],
            "geometry_number": ["01"],
            "unsteady_number": [None],
            "Plan Title": ["P"],
        }
    )
    ras.results_df = pd.DataFrame()
    ras.flow_df = pd.DataFrame()
    ras.unsteady_df = ras.boundaries_df = ras.rasmap_df = None
    return ras, project_dir


def _archive(tmp_path, ras, project_dir, **kwargs):
    kwargs.setdefault("archive_engine", "prefer-native")
    with (
        patch("ras2cng.project.init_ras_project", return_value=ras),
        patch("ras2cng.project._detect_project_crs", return_value=CRS),
        patch("ras2cng.project.merge_all_layers", side_effect=_python_geometry),
        patch("ras2cng.results.extract_steady_cross_section_results", return_value=pd.DataFrame()),
        patch("ras2cng.results.extract_unsteady_cross_section_results", return_value=pd.DataFrame()),
        patch("ras2cng.results.extract_auxiliary_result_tables", return_value=[]),
    ):
        return archive_project(project_dir / "Fake.prj", tmp_path / "out", **kwargs)


# ---------------------------------------------------------------------------
# Engine selection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["native", "direct", "terrio", "rust"])
def test_strict_aliases(engine, value):
    resolved = direct.resolve_engine(value)
    assert resolved.requested == "native" and resolved.strict and resolved.active


def test_invalid_engine_is_refused(engine):
    with pytest.raises(ValueError, match="RAS2CNG_ARCHIVE_ENGINE"):
        direct.resolve_engine("fast")


def test_python_never_tries(engine):
    assert not direct.resolve_engine("python").active


def test_env_selects_and_argument_wins(engine, monkeypatch):
    monkeypatch.setenv("RAS2CNG_ARCHIVE_ENGINE", "python")
    assert not direct.resolve_engine().active
    assert direct.resolve_engine("native").active


def test_auto_follows_explicit_pyarrow_writer(engine, monkeypatch):
    monkeypatch.delenv("RAS2CNG_ARCHIVE_ENGINE", raising=False)
    monkeypatch.setenv("RAS2CNG_GEOPARQUET_WRITER", "python")
    assert not direct.resolve_engine().active
    monkeypatch.setenv("RAS2CNG_GEOPARQUET_WRITER", "auto")
    assert direct.resolve_engine().active


def test_auto_default_switch(engine, monkeypatch):
    monkeypatch.delenv("RAS2CNG_ARCHIVE_ENGINE", raising=False)
    monkeypatch.delenv("RAS2CNG_GEOPARQUET_WRITER", raising=False)
    monkeypatch.setattr(direct, "AUTO_PREFERS_DIRECT", False)
    assert not direct.resolve_engine().active


def test_capability_requires_the_function(monkeypatch):
    old = SimpleNamespace(_terrio=SimpleNamespace())
    monkeypatch.setitem(sys.modules, "terrio", old)
    monkeypatch.setitem(sys.modules, "terrio._terrio", old._terrio)
    assert "no write_hecras_archive" in direct.direct_reason()
    assert not direct.resolve_engine("auto").active
    with pytest.raises(direct.DirectArchiveRequired):
        direct.resolve_engine("native")


# ---------------------------------------------------------------------------
# Old terrio: nothing changes
# ---------------------------------------------------------------------------


def test_old_terrio_output_is_the_python_path(tmp_path, monkeypatch):
    old = SimpleNamespace(_terrio=SimpleNamespace())
    monkeypatch.setitem(sys.modules, "terrio", old)
    monkeypatch.setitem(sys.modules, "terrio._terrio", old._terrio)
    monkeypatch.setenv("RAS2CNG_GEOPARQUET_WRITER", "python")
    ras, project_dir = _project(tmp_path)
    manifest = _archive(tmp_path, ras, project_dir, archive_engine="auto")
    assert manifest.schema_version == PYTHON_PATH_SCHEMA_VERSION == "2.7"
    assert "archive" not in manifest.geometry[0]
    stored = json.loads((tmp_path / "out" / "manifest.json").read_text())
    assert stored["schema_version"] == "2.7"
    assert "archive" not in stored["geometry"][0]
    assert manifest.geometry[0]["geoparquet"]["engine"] == "python"


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------


def test_geometry_direct_with_python_layers_and_record(engine, tmp_path):
    ras, project_dir = _project(tmp_path, text=True)
    manifest = _archive(tmp_path, ras, project_dir)
    assert manifest.schema_version == SCHEMA_VERSION == "3.0"

    call = engine.calls[0]
    assert call["layers"] == ["mesh_cells", "mesh_faces"]
    assert call["time_days"] is False and call["ghost_cell_points"] is False
    assert call["results"] is False
    assert call["crs"] is not None, "crs must always be passed"
    assert call["extra"] == ["mesh_areas", "bc_lines", "storage_areas_text"]

    entry = manifest.geometry[0]
    assert entry["file_type"] == "hdf+text"
    assert entry["archive"]["path"] == "direct"
    assert entry["archive"]["fallback_reason"] == ""
    assert entry["archive"]["native_layers"] == ["mesh_cells", "mesh_faces"]
    assert entry["archive"]["python_layers"]["storage_areas_text"] == "text"
    assert entry["geoparquet"]["engine"] == "terrio"
    assert entry["geoparquet"]["hilbert_column"] == HILBERT_COLUMN
    rows = {layer["layer"]: layer["rows"] for layer in entry["layers"]}
    assert rows == {
        "mesh_cells": 3,
        "mesh_faces": 2,
        "mesh_areas": 1,
        "bc_lines": 1,
        "storage_areas_text": 1,
    }
    types = {layer["layer"]: layer["geometry_type"] for layer in entry["layers"]}
    assert types["mesh_cells"] == "Polygon" and types["mesh_faces"] == "LineString"
    assert entry["layers"][0]["crs"] == CRS

    table = pq.read_table(tmp_path / "out" / "Fake.g01.parquet")
    assert set(table["layer"].to_pylist()) == set(rows)
    assert table.num_rows == sum(rows.values())


def test_crs_falls_back_to_the_project_when_no_frame_or_file_has_one(engine, tmp_path):
    frame = _python_geometry(None, None, hdf_layers=["mesh_areas"]).set_crs(None, allow_override=True)
    crs, source = direct._file_crs(tmp_path / "missing.hdf", frame, CRS)
    assert source == "project" and crs["id"]["code"] == 2271
    assert direct._file_crs(tmp_path / "missing.hdf", frame, None) == (None, "none")


def test_engine_error_falls_back_and_records_why(engine, tmp_path):
    engine.raise_on_write = ValueError("GeoParquet: field x is Float32 in one layer and Float64 in another")
    ras, project_dir = _project(tmp_path)
    manifest = _archive(tmp_path, ras, project_dir)
    entry = manifest.geometry[0]
    assert entry["archive"]["path"] == "python"
    assert "Float32" in entry["archive"]["fallback_reason"]
    assert entry["geoparquet"]["engine"] == "python"
    assert {layer["layer"] for layer in entry["layers"]} == {
        "mesh_cells",
        "mesh_faces",
        "mesh_areas",
        "bc_lines",
    }
    assert (tmp_path / "out" / "Fake.g01.parquet").exists()
    assert manifest.schema_version == "3.0"


def test_strict_raises_through_skip_errors(engine, tmp_path):
    engine.raise_on_write = ValueError("no")
    ras, project_dir = _project(tmp_path)
    with pytest.raises(direct.DirectArchiveRequired, match="no"):
        _archive(tmp_path, ras, project_dir, archive_engine="native", skip_errors=True)


def test_refused_mesh_layer_is_filled_from_python(engine, tmp_path):
    engine.refuse_layers = {"mesh_faces": "row 7: a line part has fewer than two points"}
    ras, project_dir = _project(tmp_path)
    manifest = _archive(tmp_path, ras, project_dir)
    entry = manifest.geometry[0]
    assert entry["archive"]["path"] == "direct"
    assert entry["archive"]["native_layers"] == ["mesh_cells"]
    assert entry["archive"]["python_layers"]["mesh_faces"] == "refused"
    assert entry["archive"]["refused"][0]["layer"] == "mesh_faces"
    assert entry["archive"]["refused"][0]["filled"] is True
    assert "fewer than two" in entry["archive"]["refused"][0]["reason"]
    assert {layer["layer"]: layer["rows"] for layer in entry["layers"]}["mesh_faces"] == 2
    assert len(engine.calls) == 2


def test_refusal_python_cannot_fill_is_recorded_not_hidden(engine, tmp_path):
    engine.refuse_layers = {"mesh_faces": "no"}

    def no_faces(hdf_path, text_path, *, sort=True, hdf_layers=None):
        names = [n for n in hdf_layers if n != "mesh_faces"]
        return _python_geometry(hdf_path, text_path, hdf_layers=names) if names else None

    ras, project_dir = _project(tmp_path)
    with patch("ras2cng.geometry.merge_all_layers", side_effect=no_faces):
        manifest = _archive(tmp_path, ras, project_dir)
    refused = manifest.geometry[0]["archive"]["refused"]
    assert refused[0]["filled"] is False
    assert "mesh_faces" not in {layer["layer"] for layer in manifest.geometry[0]["layers"]}


def test_silent_omission_of_a_mesh_layer_falls_back(engine, tmp_path):
    engine.silently_omit = {"mesh_cells"}
    ras, project_dir = _project(tmp_path)
    manifest = _archive(tmp_path, ras, project_dir)
    entry = manifest.geometry[0]
    assert entry["archive"]["path"] == "python"
    assert "no mesh_cells" in entry["archive"]["fallback_reason"]
    assert "mesh_cells" in {layer["layer"] for layer in entry["layers"]}


def test_report_mismatch_falls_back(engine, tmp_path):
    engine.misreport_rows = 1
    ras, project_dir = _project(tmp_path)
    manifest = _archive(tmp_path, ras, project_dir)
    assert manifest.geometry[0]["archive"]["path"] == "python"
    assert "does not match" in manifest.geometry[0]["archive"]["fallback_reason"]


def test_no_sort_stays_python_and_strict_refuses(engine, tmp_path):
    ras, project_dir = _project(tmp_path)
    manifest = _archive(tmp_path, ras, project_dir, sort=False)
    assert manifest.geometry[0]["archive"]["path"] == "python"
    assert "no-sort" in manifest.geometry[0]["archive"]["fallback_reason"]
    assert not engine.calls
    with pytest.raises(direct.DirectArchiveRequired):
        _archive(tmp_path, ras, project_dir, sort=False, archive_engine="native")


def test_geometry_without_hdf_is_not_a_strict_failure(engine, tmp_path):
    ras, project_dir = _project(tmp_path, text=True)
    (project_dir / "Fake.g01.hdf").unlink()
    manifest = _archive(tmp_path, ras, project_dir, archive_engine="native")
    assert manifest.geometry[0]["archive"] == {"path": "python", "fallback_reason": "no geometry HDF"}


# ---------------------------------------------------------------------------
# Plans
# ---------------------------------------------------------------------------


def _with_plan(tmp_path, variables, merge_variables=None, **kwargs):
    ras, project_dir = _project(tmp_path, plan=True)
    patches = [patch("ras2cng.results.selected_summary_variables", return_value=variables)]
    if merge_variables is not None:
        patches.append(patch("ras2cng.results.merge_all_variables", side_effect=merge_variables))
    for item in patches:
        item.start()
    try:
        return _archive(tmp_path, ras, project_dir, include_results=True, **kwargs)
    finally:
        for item in patches:
            item.stop()


def test_plan_direct(engine, tmp_path):
    manifest = _with_plan(tmp_path, ["Maximum Depth"])
    plan = manifest.results[0]
    assert plan["archive"]["path"] == "direct"
    assert plan["archive"]["crs_source"] == "geometry-file"
    assert plan["geoparquet"]["engine"] == "terrio"
    assert plan["parquet"] == "Fake.p01.parquet"
    assert [v["variable"] for v in plan["variables"]] == ["maximum_depth"]
    assert plan["variables"][0]["index_column"] == "cell_id"
    assert plan["variables"][0]["geometry_filter"] == "mesh_cells"
    call = engine.calls[-1]
    assert call["geometry"] is False and call["variables"] == ["Maximum Depth"]
    assert call["time_days"] is False
    assert call["crs"] is not None


def test_refused_variable_is_filled_from_python_with_the_archived_cells(engine, tmp_path):
    engine.variables["Cell Last Iteration"] = _summary("cell_last_iteration")
    engine.refuse_variables = {"Cell Last Iteration": "stored Location attribute absent"}
    seen = {}

    def merge(plan_hdf, mesh_cells_gdf=None, variables=None, geometry_mode="polygon"):
        seen.update(cells=mesh_cells_gdf, variables=variables)
        return _summary("cell_last_iteration").assign(layer="cell_last_iteration")

    manifest = _with_plan(tmp_path, ["Cell Last Iteration", "Maximum Depth"], merge)
    plan = manifest.results[0]
    assert seen["variables"] == ["Cell Last Iteration"]
    assert sorted(seen["cells"]["cell_id"].tolist()) == [0, 1, 2]
    assert "layer" not in seen["cells"].columns and HILBERT_COLUMN not in seen["cells"].columns
    assert "bc_line_id" not in seen["cells"].columns, "all-null union columns are dropped"
    assert plan["archive"]["path"] == "direct"
    assert plan["archive"]["python_layers"] == {"cell_last_iteration": "refused"}
    assert plan["archive"]["refused"][0]["filled"] is True
    assert [v["variable"] for v in plan["variables"]] == ["cell_last_iteration", "maximum_depth"]
    assert engine.calls[-1]["variables"] == ["Maximum Depth"]
    assert engine.calls[-1]["order"] == ["cell_last_iteration", "maximum_depth"]


def test_refused_variable_python_cannot_read_is_recorded(engine, tmp_path):
    engine.variables["Starting Differences WSE"] = _summary("x")
    engine.refuse_variables = {"Starting Differences WSE": "stored Location attribute absent"}
    manifest = _with_plan(tmp_path, ["Starting Differences WSE", "Maximum Depth"], lambda *a, **k: None)
    plan = manifest.results[0]
    assert plan["archive"]["path"] == "direct"
    assert plan["archive"]["refused"][0]["filled"] is False
    assert [v["variable"] for v in plan["variables"]] == ["maximum_depth"]


def test_every_variable_refused_falls_back_to_python(engine, tmp_path):
    """Spring_BLE p02: a HEC-RAS 6.1 plan stores no Location on any summary."""
    engine.refuse_variables = {"Maximum Depth": "stored Location attribute absent"}

    def merge(plan_hdf, mesh_cells_gdf=None, variables=None, geometry_mode="polygon"):
        return _summary("maximum_depth").assign(layer="maximum_depth")

    manifest = _with_plan(tmp_path, ["Maximum Depth"], merge)
    plan = manifest.results[0]
    assert plan["archive"]["path"] == "python"
    assert "refused every" in plan["archive"]["fallback_reason"]
    assert [v["variable"] for v in plan["variables"]] == ["maximum_depth"]
    assert (tmp_path / "out" / "Fake.p01.parquet").exists()


def test_variable_the_engine_does_not_mention_is_still_read(engine, tmp_path):
    def merge(plan_hdf, mesh_cells_gdf=None, variables=None, geometry_mode="polygon"):
        return _summary("velocity").assign(layer="velocity")

    manifest = _with_plan(tmp_path, ["Maximum Depth", "Velocity"], merge)
    plan = manifest.results[0]
    assert plan["archive"]["refused"][0]["code"] == "absent"
    assert {v["variable"] for v in plan["variables"]} == {"maximum_depth", "velocity"}


def test_no_summary_variables_writes_no_plan_file(engine, tmp_path):
    manifest = _with_plan(tmp_path, [])
    assert manifest.results[0]["archive"]["path"] == "none"
    assert not (tmp_path / "out" / "Fake.p01.parquet").exists()


def test_other_results_options_stay_python(engine, tmp_path):
    def merge(plan_hdf, mesh_cells_gdf=None, variables=None, geometry_mode="polygon"):
        return _summary("maximum_depth").drop(columns="geometry").assign(layer="maximum_depth")

    manifest = _with_plan(tmp_path, ["Maximum Depth"], merge, results_geometry="none")
    plan = manifest.results[0]
    assert plan["archive"]["path"] == "python"
    assert "results_geometry=none" in plan["archive"]["fallback_reason"]
    assert manifest.geometry[0]["archive"]["path"] == "direct"


def test_strict_refuses_other_results_options_up_front(engine, tmp_path):
    with pytest.raises(direct.DirectArchiveRequired, match="results_layout=variable"):
        _with_plan(tmp_path, ["Maximum Depth"], results_layout="variable", archive_engine="native")


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------


def test_older_and_unversioned_manifests_still_read(tmp_path):
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps({"schema_version": "2.4", "project": {"name": "x"}, "geometry": []}))
    assert Manifest.load(path).schema_version == "2.4"
    path.write_text(json.dumps({"project": {"name": "x"}}))
    assert Manifest.load(path).schema_version == "2.7"


def test_manifest_3_round_trips_its_records(engine, tmp_path):
    ras, project_dir = _project(tmp_path)
    written = _archive(tmp_path, ras, project_dir)
    loaded = Manifest.load(tmp_path / "out" / "manifest.json")
    assert loaded.schema_version == "3.0"
    assert loaded.geometry[0]["archive"] == written.geometry[0]["archive"]


def test_summarize_rejects_a_file_without_layer_row_groups(tmp_path):
    frame = _cells().assign(layer=["a", "a", "b"])
    path = tmp_path / "x.parquet"
    frame.to_wkb().to_parquet(path, index=False)
    with pytest.raises(direct._Fallback):
        direct.summarize_layers(path)


# ---------------------------------------------------------------------------
# The real engine
# ---------------------------------------------------------------------------


def _real_engine():
    return direct.direct_reason() is None


def _layers(path):
    table = pq.read_table(path)
    names = list(dict.fromkeys(table["layer"].to_pylist()))
    return {name: table.filter(pa.compute.equal(table["layer"], name)) for name in names}


real_project = pytest.mark.skipif(
    not (CORPUS / "davis").is_dir(), reason="HEC-RAS corpus not available"
)
real_engine = pytest.mark.skipif(not _real_engine(), reason="terrio has no write_hecras_archive")


@real_engine
@real_project
def test_real_davis_equals_the_python_path(tmp_path):
    import pyarrow.compute  # noqa: F401

    project = CORPUS / "davis"
    python = archive_project(
        project, tmp_path / "python", include_results=True, archive_engine="python"
    )
    native = archive_project(
        project, tmp_path / "direct", include_results=True, archive_engine="native"
    )
    assert python.schema_version == "2.7" and native.schema_version == "3.0"
    assert native.geometry[0]["archive"]["path"] == "direct"
    assert native.results[0]["archive"]["path"] == "direct"
    assert native.results[0]["archive"]["python_layers"] == {"cell_last_iteration": "refused"}

    for name in ("DavisStormSystem.g02.parquet", "DavisStormSystem.p02.parquet"):
        a, b = _layers(tmp_path / "python" / name), _layers(tmp_path / "direct" / name)
        assert list(a) == list(b)
        for layer, table in a.items():
            other = b[layer]
            assert table.num_rows == other.num_rows
            assert table["geometry"].to_pylist() == other["geometry"].to_pylist(), layer
            assert table[HILBERT_COLUMN].to_pylist() == other[HILBERT_COLUMN].to_pylist(), layer
            present = [c for c in table.column_names if table[c].null_count < table.num_rows]
            for column in present:
                assert other.schema.field(column).type == table.schema.field(column).type, column
                assert table[column].to_pylist() == other[column].to_pylist() or column in (
                    "bbox",
                ), (layer, column)
        crs = direct.stored_crs(tmp_path / "python" / name)
        assert crs == direct.stored_crs(tmp_path / "direct" / name)


@real_engine
@real_project
def test_real_float_widths_refuse_cleanly_and_the_file_falls_back(tmp_path):
    """Mixing a float64 Python frame with the engine's float32 column of that name."""
    import terrio._terrio as engine_module

    from ras2cng.geometry import _extract_hdf_layer

    hdf = CORPUS / "bridges" / "BaldEagleDamBrk.g03.hdf"
    frame = _extract_hdf_layer(hdf, "breaklines").assign(layer="breaklines_python")
    assert frame["cell_spacing_near"].dtype == "float64"
    extras, _ = direct._extra_layers(frame)
    out = tmp_path / "x.parquet"
    with pytest.raises(Exception, match="cell_spacing_near is Float32 in one layer and Float64"):
        engine_module.write_hecras_archive(
            str(hdf), str(out), layers=["breaklines"], extra_layers=extras, crs=CRS,
            results=False, time_days=False, ghost_cell_points=False,
        )
    assert not out.exists()

    # The default native layers never meet that conflict; force it through ras2cng.
    with patch.object(direct, "NATIVE_GEOMETRY_LAYERS", ("mesh_cells", "breaklines")):
        with patch("ras2cng.geometry.ALL_HDF_LAYERS", ["mesh_areas"]):
            original = direct._extra_layers

            def with_conflict(python_frame):
                joined = pd.concat([python_frame, frame], ignore_index=True)
                return original(gpd.GeoDataFrame(joined, geometry="geometry", crs=frame.crs))

            with patch.object(direct, "_extra_layers", side_effect=with_conflict):
                with pytest.raises(Exception, match="in one layer and .* in another"):
                    direct.write_geometry(
                        hdf, None, out, project_crs=CRS, engine=direct.ArchiveEngine("prefer-native")
                    )


@real_engine
@real_project
def test_real_old_format_refusals_stay_on_python_or_get_filled():
    """Spring_BLE p02 stores no Location: the engine returns refusals, it does not raise."""
    import terrio._terrio as engine_module

    plan = CORPUS / "spring_ble" / "Spring_BLE.p02.hdf"
    if not plan.exists():
        pytest.skip("Spring_BLE not available")
    found = engine_module.hecras_archive_layers(
        str(plan), geometry=False, summary_geometry=False, time_days=False, ghost_cell_points=False
    )
    assert not found["layers"]
    assert found["skipped"]


@real_engine
@real_project
def test_real_manifest_3_archive_works_with_readers(tmp_path):
    from ras2cng.duckdb_session import query_parquet
    from ras2cng.spatial_index import postprocess_archive

    out = tmp_path / "archive"
    archive_project(CORPUS / "davis", out, include_results=True, archive_engine="native")
    before = (out / "DavisStormSystem.p02.parquet").read_bytes()
    summary = postprocess_archive(out)
    assert summary["error_count"] == 0
    assert (out / "DavisStormSystem.p02.parquet").read_bytes() == before, "already indexed"

    rows = query_parquet(
        out / "DavisStormSystem.g02.parquet",
        "SELECT count(*) AS n FROM _ WHERE layer = 'mesh_cells' AND bbox.xmin IS NOT NULL",
    )
    assert int(rows["n"].iloc[0]) == 2716

    frame = gpd.read_parquet(out / "DavisStormSystem.p02.parquet")
    assert frame.crs is not None and "maximum_water_surface" in set(frame["layer"])
    cells = direct.read_mesh_cells(out / "DavisStormSystem.g02.parquet")
    assert len(cells) == 2716 and {"mesh_name", "cell_id", "geometry"} <= set(cells.columns)
