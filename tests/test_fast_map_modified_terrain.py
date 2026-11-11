"""Routing of modified terrains: native effective terrain (/2) or RAS Mapper.

A modified terrain goes to the native effective terrain with modifications
(``rasmapper-effective-terrain/2``, Fast-Map 0.18 / ras-commander 0.119) only
when the engine advertises ``/2`` with a ``modifications`` record, Commander's
reader stages every layer and every staged form is in the engine's qualified
list. Otherwise it goes to the RAS Mapper export, as before, with the reason
recorded. The engine record and Commander's reader are faked here; no HEC-RAS,
engine or real terrain is needed.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from ras2cng import fast_map
from ras2cng.mapping import generate_result_maps

SCHEMA_V1 = "ras-commander-effective-terrain-request/1"

# Fast-Map 0.17.0: /1 only, no modifications record.
ENGINE_V1 = {
    "method_id": "rasmapper-effective-terrain/1",
    "request_schema": SCHEMA_V1,
}

# Fast-Map 0.18.0 (main after #40/#41): /2 with the qualified forms.
SUPPORTED = {
    "ground_line": {
        "subtypes": ["levee", "channel"],
        "elevation_types": ["SetValue", "SetIfHigher", "SetIfLower", "Add"],
    },
    "polygon": {"elevation_types": ["SetValue", "SetIfHigher", "SetIfLower", "Add"]},
    "ellipse": {"elevation_types": ["SetValue", "SetIfHigher", "SetIfLower"]},
}
ENGINE_V2 = {
    "method_id": fast_map.EFFECTIVE_TERRAIN_V2,
    "request_schema": fast_map.EFFECTIVE_TERRAIN_SCHEMA_V2,
    "request_schemas": [SCHEMA_V1, fast_map.EFFECTIVE_TERRAIN_SCHEMA_V2],
    "modifications": {
        "supported": SUPPORTED,
        "hecras_version": "7.0.1 (default) or 6.6",
        "refused": "boundary elevations sampled from the terrain, shape-Z polygons, ...",
    },
}

LEVEE = {
    "kind": "ground_line", "name": "Levee A", "priority": 0, "subtype": "levee",
    "features": [{"elevation_type": "SetIfHigher"}, {"elevation_type": "SetValue"}],
}
PAD = {
    "kind": "polygon", "name": "Pad", "priority": 1,
    "features": [{"elevation_type": "SetValue", "rings": [[[0, 0], [1, 0], [1, 1]]],
                  "generate_boundary": False, "use_shape_z": False}],
}
CIRCLE_ADD = {
    "kind": "ellipse", "name": "Mound", "priority": 2,
    "features": [{"elevation_type": "Add"}],
}


def _terrain_h5(path, modifications=("Levee A",)):
    import h5py

    with h5py.File(path, "w") as handle:
        handle.create_group("Terrain")
        group = handle.create_group("Modifications")
        for name in modifications:
            group.create_group(name)
    return path


def _reader(layers=None, error=None, version=("7.0.1", "default")):
    """A fake ras_commander.fim._terrain_mods."""
    calls = []

    def read_layers(hdf, length_units="feet", hecras_version="7.0.1"):
        calls.append((hdf, length_units, hecras_version))
        if error is not None:
            raise error
        return [dict(layer) for layer in (layers or [])]

    return SimpleNamespace(
        read_layers=read_layers,
        project_length_units=lambda hdf, rasmap=None, ras=None: "feet",
        project_hecras_version=lambda hdf, rasmap=None, ras=None: version,
        hecras_version_for=lambda v: "6.6" if str(v).startswith("6.6") else "7.0.1",
        calls=calls,
    )


def _setup(tmp_path, monkeypatch, engine, reader, *, effective=True):
    """A resolver over a modified terrain HDF with the given engine record and reader."""
    hdf = _terrain_h5(tmp_path / "T.hdf")
    resolver = fast_map.TerrainResolver(
        tmp_path / "a.prj", tmp_path / "w", terrain_name="T", effective=effective
    )
    monkeypatch.setattr(fast_map, "engine_method", lambda kind: dict(engine) if kind == "effective_terrain" else {})
    monkeypatch.setattr(fast_map, "_modification_reader", lambda: reader)
    monkeypatch.setattr(fast_map, "effective_terrain_supported", lambda: True)
    monkeypatch.setattr(fast_map, "source_terrain_supported", lambda: True)
    monkeypatch.setattr(resolver, "_terrain_hdf_for", lambda plan: hdf)
    monkeypatch.setattr(
        resolver, "_source", lambda plan: pytest.fail("the unmodified source TIFF must not be used")
    )
    monkeypatch.setattr(resolver, "_canonical", lambda tif: None)
    monkeypatch.setattr(resolver, "for_plan", lambda plan: tmp_path / "export.tif")
    built = []

    def effective(plan):
        built.append(plan)
        return tmp_path / "w" / "T.effective.tif"

    monkeypatch.setattr(resolver, "_effective", effective)
    return resolver, built


def _run(resolver, tmp_path, refuse=()):
    kinds = []

    def run(tif, hdf, kind):
        kinds.append(kind)
        if kind in refuse:
            raise fast_map.FastMapUnavailable(f"{kind} refused for the test")
        return "ok"

    _, kind = fast_map.with_terrain(resolver, tmp_path / "a.p01.hdf", run)
    return kind, kinds


def _manifest(tmp_path):
    return json.loads((tmp_path / "w" / fast_map.TERRAIN_ROUTE_MANIFEST).read_text())


# ---------------------------------------------------------------------------
# The three routing decisions
# ---------------------------------------------------------------------------


def test_v1_only_engine_sends_a_modified_terrain_to_rasmapper(tmp_path, monkeypatch):
    """Fast-Map 0.17.0 (/1 only): unchanged, the RAS Mapper export builds it."""
    reader = _reader([LEVEE])
    resolver, built = _setup(tmp_path, monkeypatch, ENGINE_V1, reader)
    kind, kinds = _run(resolver, tmp_path)
    assert kind == fast_map.TERRAIN_EXPORT and kinds == ["export"]
    assert built == [], "the effective terrain is not built"
    assert reader.calls == [], "the reader is not needed when the engine lacks /2"
    route = resolver.modified_route(tmp_path / "a.p01.hdf")
    assert route["route"] == fast_map.MODIFIED_ROUTE_RASMAPPER
    assert "rasmapper-effective-terrain/1" in route["reason"]
    assert fast_map.EFFECTIVE_TERRAIN_V2 in route["reason"]
    assert _manifest(tmp_path)["routes"][0]["route"] == "rasmapper-export"
    assert any("RAS Mapper export" in note for note in resolver.notes)


def test_v2_engine_with_every_form_qualified_uses_the_native_effective_terrain(tmp_path, monkeypatch):
    reader = _reader([LEVEE, PAD])
    resolver, built = _setup(tmp_path, monkeypatch, ENGINE_V2, reader)
    kind, kinds = _run(resolver, tmp_path)
    assert kind == fast_map.TERRAIN_EFFECTIVE and kinds == ["effective"]
    assert len(built) == 1
    assert reader.calls and reader.calls[0][1:] == ("feet", "7.0.1")
    route = resolver.modified_route(tmp_path / "a.p01.hdf")
    assert route["route"] == fast_map.MODIFIED_ROUTE_NATIVE and route["reason"] is None
    assert route["method_id"] == fast_map.EFFECTIVE_TERRAIN_V2
    assert [layer["name"] for layer in route["layers"]] == ["Levee A", "Pad"]
    assert route["layers"][0]["elevation_types"] == ["SetIfHigher", "SetValue"]
    assert _manifest(tmp_path)["routes"][0]["route"] == "native-effective"


def test_v2_engine_with_an_unqualified_form_goes_to_rasmapper_with_a_reason(tmp_path, monkeypatch):
    """An ellipse with Add is not in the engine's qualified list."""
    reader = _reader([LEVEE, CIRCLE_ADD])
    resolver, built = _setup(tmp_path, monkeypatch, ENGINE_V2, reader)
    kind, kinds = _run(resolver, tmp_path)
    assert kind == fast_map.TERRAIN_EXPORT and kinds == ["export"]
    assert built == []
    route = resolver.modified_route(tmp_path / "a.p01.hdf")
    assert route["route"] == fast_map.MODIFIED_ROUTE_RASMAPPER
    assert "Mound" in route["reason"] and "'Add'" in route["reason"]
    assert route["unqualified"] == [route["reason"]]
    assert "Mound" in _manifest(tmp_path)["routes"][0]["reason"]


# ---------------------------------------------------------------------------
# Other gates
# ---------------------------------------------------------------------------


def test_a_reader_refusal_goes_to_rasmapper_with_its_reason(tmp_path, monkeypatch):
    from ras_commander.fim.errors import FimUnsupportedError

    reader = _reader(error=FimUnsupportedError(
        "modification 'Ring': boundary elevations sampled from the terrain are not qualified"))
    resolver, built = _setup(tmp_path, monkeypatch, ENGINE_V2, reader)
    assert _run(resolver, tmp_path)[0] == fast_map.TERRAIN_EXPORT and built == []
    reason = resolver.modified_route(tmp_path / "a.p01.hdf")["reason"]
    assert "sampled from the terrain" in reason


def test_commander_without_the_reader_goes_to_rasmapper(tmp_path, monkeypatch):
    """ras-commander 0.117/0.118 with a /2 engine: no reader, no native route."""
    resolver, built = _setup(tmp_path, monkeypatch, ENGINE_V2, None)
    assert _run(resolver, tmp_path)[0] == fast_map.TERRAIN_EXPORT and built == []
    assert "0.119.0" in resolver.modified_route(tmp_path / "a.p01.hdf")["reason"]


def test_disabled_effective_terrain_keeps_modified_terrains_on_rasmapper(tmp_path, monkeypatch):
    resolver, built = _setup(tmp_path, monkeypatch, ENGINE_V2, _reader([LEVEE]), effective=False)
    assert _run(resolver, tmp_path)[0] == fast_map.TERRAIN_EXPORT and built == []
    assert "--no-fast-map-effective-terrain" in resolver.modified_route(tmp_path / "a.p01.hdf")["reason"]


def test_rasmap_only_modification_layers_go_to_rasmapper(tmp_path, monkeypatch):
    resolver, built = _setup(tmp_path, monkeypatch, ENGINE_V2, _reader([LEVEE]))
    monkeypatch.setattr(resolver, "modifications", lambda plan: ["hdf:Levee A", "rasmap:Berm"])
    assert _run(resolver, tmp_path)[0] == fast_map.TERRAIN_EXPORT and built == []
    assert "Berm" in resolver.modified_route(tmp_path / "a.p01.hdf")["reason"]


def test_an_unreadable_modification_group_goes_to_rasmapper(tmp_path, monkeypatch):
    resolver, built = _setup(tmp_path, monkeypatch, ENGINE_V2, _reader([LEVEE]))
    monkeypatch.setattr(resolver, "modifications", lambda plan: ["hdf:/Modifications (link)"])
    assert _run(resolver, tmp_path)[0] == fast_map.TERRAIN_EXPORT and built == []
    assert "could not be read" in resolver.modified_route(tmp_path / "a.p01.hdf")["reason"]


def test_hecras_66_rules_need_the_engines_hecras_version_record(tmp_path, monkeypatch):
    engine = json.loads(json.dumps(ENGINE_V2))
    del engine["modifications"]["hecras_version"]
    resolver, built = _setup(
        tmp_path, monkeypatch, engine, _reader([LEVEE], version=("6.6", "Program Version 6.60"))
    )
    assert _run(resolver, tmp_path)[0] == fast_map.TERRAIN_EXPORT and built == []
    assert "HEC-RAS 6.6" in resolver.modified_route(tmp_path / "a.p01.hdf")["reason"]


def test_ras_version_selects_the_modification_rules(tmp_path, monkeypatch):
    reader = _reader([LEVEE])
    resolver, _ = _setup(tmp_path, monkeypatch, ENGINE_V2, reader)
    resolver.ras_version = "6.6"
    assert _run(resolver, tmp_path)[0] == fast_map.TERRAIN_EFFECTIVE
    assert reader.calls[0][2] == "6.6"
    assert resolver.modified_route(tmp_path / "a.p01.hdf")["hecras_version"] == "6.6"


def test_an_engine_refusal_on_the_native_route_falls_back_to_the_export(tmp_path, monkeypatch):
    resolver, built = _setup(tmp_path, monkeypatch, ENGINE_V2, _reader([LEVEE]))
    kind, kinds = _run(resolver, tmp_path, refuse=(fast_map.TERRAIN_EFFECTIVE,))
    assert kind == fast_map.TERRAIN_EXPORT and kinds == ["effective", "export"]
    route = resolver.modified_route(tmp_path / "a.p01.hdf")
    assert route["route"] == fast_map.MODIFIED_ROUTE_NATIVE
    assert "refused for the test" in route["fallback"][0]
    assert _manifest(tmp_path)["routes"][0]["fallback"]


@patch("ras_commander.RasFim")
def test_a_failed_native_build_falls_back_to_the_export(fim, tmp_path, monkeypatch):
    reader = _reader([LEVEE])
    resolver, _ = _setup(tmp_path, monkeypatch, ENGINE_V2, reader)
    monkeypatch.setattr(resolver, "_effective", fast_map.TerrainResolver._effective.__get__(resolver))
    fim.build_effective_terrain.side_effect = RuntimeError("named ground lines with control points")
    kind, kinds = _run(resolver, tmp_path)
    assert kind == fast_map.TERRAIN_EXPORT and kinds == ["export"]
    route = resolver.modified_route(tmp_path / "a.p01.hdf")
    assert "control points" in route["fallback"][0]


def test_unmodified_terrains_have_no_route_record(tmp_path, monkeypatch):
    resolver, _ = _setup(tmp_path, monkeypatch, ENGINE_V2, _reader([LEVEE]))
    monkeypatch.setattr(resolver, "modifications", lambda plan: [])
    assert resolver.modified_route(tmp_path / "a.p01.hdf") is None


def test_a_supplied_terrain_has_no_route_record(tmp_path, monkeypatch):
    resolver, _ = _setup(tmp_path, monkeypatch, ENGINE_V2, _reader([LEVEE]))
    resolver.terrain_path = tmp_path / "supplied.tif"
    resolver.terrain_path.write_bytes(b"x")
    assert _run(resolver, tmp_path)[0] == fast_map.TERRAIN_SUPPLIED
    assert resolver.modified_route(tmp_path / "a.p01.hdf") is None


# ---------------------------------------------------------------------------
# Capability record and form checks (pure)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("record, fragment", [
    ({}, "no effective_terrain"),
    (ENGINE_V1, "rasmapper-effective-terrain/1"),
    ({**ENGINE_V2, "request_schemas": [SCHEMA_V1]}, "does not accept"),
    ({k: v for k, v in ENGINE_V2.items() if k != "modifications"}, "qualified modification forms"),
])
def test_modified_terrain_capability_refusals(monkeypatch, record, fragment):
    monkeypatch.setattr(fast_map, "engine_method", lambda kind: dict(record))
    found, reason = fast_map.modified_terrain_capability()
    assert found is None and fragment in reason


def test_modified_terrain_capability_accepts_a_compatible_method_id(monkeypatch):
    record = {**ENGINE_V2, "method_id": "rasmapper-effective-terrain/3",
              "compatible_method_ids": [fast_map.EFFECTIVE_TERRAIN_V2]}
    monkeypatch.setattr(fast_map, "engine_method", lambda kind: dict(record))
    found, reason = fast_map.modified_terrain_capability()
    assert reason is None and found["supported"] == SUPPORTED


@pytest.mark.parametrize("layer, fragment", [
    ({"kind": "rectangle", "name": "R", "features": [{"elevation_type": "SetValue"}]}, "'rectangle'"),
    ({**LEVEE, "subtype": "pier"}, "subtype 'pier'"),
    ({**PAD, "features": [{**PAD["features"][0], "use_shape_z": True}]}, "shape-Z"),
    ({**PAD, "features": [{**PAD["features"][0], "generate_boundary": True,
                           "rings": [[[0, 0], [1, 0], [1, 1]], [[5, 5], [6, 5], [6, 6]]]}]},
     "multi-part"),
])
def test_unqualified_modification_forms(layer, fragment):
    problems = fast_map.unqualified_modification_forms([LEVEE, layer], ENGINE_V2["modifications"])
    assert len(problems) == 1 and fragment in problems[0]


def test_qualified_forms_report_nothing():
    shape_z_with_boundary = {**PAD, "features": [{**PAD["features"][0], "use_shape_z": True,
                                                  "generate_boundary": True}]}
    assert fast_map.unqualified_modification_forms(
        [LEVEE, PAD, shape_z_with_boundary], ENGINE_V2["modifications"]) == []


# ---------------------------------------------------------------------------
# Through generate_result_maps: the decision lands on MapResult
# ---------------------------------------------------------------------------


def _fake_ras(tmp_path):
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    (project_dir / "TestModel.prj").write_text("Proj Title=Test\nEnglish Units\n")
    (project_dir / "TestModel.p01.hdf").touch()
    ras = MagicMock()
    ras.project_name = "TestModel"
    ras.project_folder = str(project_dir)
    ras.plan_df = pd.DataFrame([{"plan_number": "01", "geometry_number": "01"}])
    return ras, project_dir


@pytest.mark.parametrize("engine, layers, expected_kind, expected_route", [
    (ENGINE_V1, [LEVEE], "export", "rasmapper-export"),
    (ENGINE_V2, [LEVEE, PAD], "effective", "native-effective"),
    (ENGINE_V2, [LEVEE, CIRCLE_ADD], "export", "rasmapper-export"),
])
def test_map_result_records_the_route(tmp_path, monkeypatch, engine, layers, expected_kind,
                                      expected_route):
    ras, project_dir = _fake_ras(tmp_path)
    hdf = _terrain_h5(tmp_path / "T.hdf")
    monkeypatch.setattr(fast_map, "preflight", lambda plan, r, mode, **kw: {"mode": "sloping"})
    monkeypatch.setattr(fast_map, "engine_method",
                        lambda kind: dict(engine) if kind == "effective_terrain" else {})
    monkeypatch.setattr(fast_map, "_modification_reader", lambda: _reader(layers))
    monkeypatch.setattr(fast_map, "effective_terrain_supported", lambda: True)
    monkeypatch.setattr(fast_map, "source_terrain_supported", lambda: False)
    monkeypatch.setattr(fast_map.TerrainResolver, "_terrain_hdf_for", lambda self, plan: hdf)
    monkeypatch.setattr(fast_map.TerrainResolver, "_effective",
                        lambda self, plan: tmp_path / "effective.tif")
    monkeypatch.setattr(fast_map.TerrainResolver, "for_plan", lambda self, plan: tmp_path / "export.tif")
    seen = []

    def map_plan(plan_hdf, tif, out, **kw):
        seen.append(kw["terrain_kind"])
        return fast_map.FastMapProducts({t: [out / f"{t}.tif"] for t in kw["map_types"]})

    monkeypatch.setattr(fast_map, "map_plan", map_plan)
    with patch("ras2cng.mapping.init_ras_project", return_value=ras), \
            patch("ras2cng.mapping._configure_rasprocess"), \
            patch("ras2cng.mapping._generate_plan_maps", return_value={}):
        result = generate_result_maps(
            project_dir, tmp_path / "maps", map_engine="fast-map", velocity=False,
        )[0]
    assert seen == [expected_kind]
    assert result.fast_map_terrain["wse"] == expected_kind
    assert result.fast_map_terrain_route["wse"]["route"] == expected_route
    assert (result.fast_map_terrain_route["depth"]["reason"] is None) == (
        expected_route == "native-effective"
    )
