"""Routing of modified terrains: native effective terrain (/2) or RAS Mapper.

A modified terrain goes to the native effective terrain with modifications
(``rasmapper-effective-terrain/2``, Fast-Map 0.18 / ras-commander 0.119) only
when the engine advertises ``/2`` with a ``modifications`` record, Commander's
reader stages every layer and every staged form is in the engine's qualified
list. Otherwise it goes to the RAS Mapper export, as before, with the reason
recorded. Commander's reader is the public ``RasFim.read_terrain_modifications``
(ras-commander 0.119+), else the private ``_terrain_mods`` module; which one
was used is recorded as ``modification_reader``. The engine record and both
readers are faked here; no HEC-RAS, engine or real terrain is needed.
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
    """A fake private ras_commander.fim._terrain_mods (ras-commander without the public API)."""
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


def _public_reader(layers=None, refusal=None, version=("7.0.1", "default"), units="feet",
                   refusal_reason=None):
    """A fake RasFim.read_terrain_modifications (ras-commander 0.119+)."""
    calls = []

    def read(terrain_hdf, *, rasmap_path=None, ras_object=None, hecras_version=None,
             length_units=None, strict=False):
        calls.append((terrain_hdf, rasmap_path, hecras_version))
        if hecras_version is not None:
            resolved = ("6.6" if str(hecras_version).startswith("6.6") else "7.0.1", "argument")
        else:
            resolved = version
        record = {
            "schema": "ras-commander-terrain-modifications/1",
            "terrain_hdf": str(terrain_hdf),
            "method_id": fast_map.EFFECTIVE_TERRAIN_V2,
            "status": "refused" if refusal else ("staged" if layers else "none"),
            "refusal": refusal,
            "refusal_reason": refusal_reason,
            "hecras_version": resolved[0],
            "hecras_version_basis": resolved[1],
            "length_units": units,
            "length_units_basis": "project",
            "layers": [] if refusal else [dict(layer) for layer in (layers or [])],
        }
        return record

    read.calls = calls
    return read


def _readers(monkeypatch, private=None, public=None):
    """Install fakes for Commander's public and private modification readers."""
    monkeypatch.setattr(fast_map, "_public_modification_reader", lambda: public)
    monkeypatch.setattr(fast_map, "_private_modification_reader", lambda: private)


def _runtime(detected="7.0.1"):
    """A fake resolve_hecras_runtime: --ras-version wins, else ``detected``
    stands for the release init_ras_project finds in the project's plan files."""
    calls = []

    def resolve(prj_file, ras_version):
        calls.append(ras_version)
        if ras_version:
            label, basis = str(ras_version), f"--ras-version {ras_version}"
        elif detected is None:
            return fast_map.HecRasRuntime(None, "project plan files", None, "no release found")
        else:
            label, basis = detected, f"detected from the project's plan files ({detected})"
        if label in ("6.6", "6.5", "6.4.1", "7.0.1", "7.0.2"):
            return fast_map.HecRasRuntime(label, basis, label)
        return fast_map.HecRasRuntime(label, basis, None, f"HEC-RAS {label!r} is unsupported")

    resolve.calls = calls
    return resolve


def _setup(tmp_path, monkeypatch, engine, reader, *, effective=True, public=None,
           detected="7.0.1"):
    """A resolver over a modified terrain HDF with the given engine record and readers.

    ``reader`` fakes the private module, ``public`` the public API, ``detected``
    the HEC-RAS release the export would detect from the project.
    """
    hdf = _terrain_h5(tmp_path / "T.hdf")
    resolver = fast_map.TerrainResolver(
        tmp_path / "a.prj", tmp_path / "w", terrain_name="T", effective=effective
    )
    monkeypatch.setattr(fast_map, "resolve_hecras_runtime", _runtime(detected))
    monkeypatch.setattr(fast_map, "engine_method", lambda kind: dict(engine) if kind == "effective_terrain" else {})
    _readers(monkeypatch, private=reader, public=public)
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
# Commander's reader: public API, private fallback, none
# ---------------------------------------------------------------------------


def test_the_public_reader_is_preferred_and_recorded(tmp_path, monkeypatch):
    """ras-commander 0.119+: RasFim.read_terrain_modifications; the private
    module is not consulted even when it is present."""
    public = _public_reader([LEVEE, PAD])
    private = _reader([LEVEE])
    resolver, built = _setup(tmp_path, monkeypatch, ENGINE_V2, private, public=public)
    kind, kinds = _run(resolver, tmp_path)
    assert kind == fast_map.TERRAIN_EFFECTIVE and len(built) == 1
    assert private.calls == [] and len(public.calls) == 1
    hdf, rasmap_path, hecras_version = public.calls[0]
    # The reader is given the export's release, never left to guess its own.
    assert hdf == tmp_path / "T.hdf" and rasmap_path is None and hecras_version == "7.0.1"
    route = resolver.modified_route(tmp_path / "a.p01.hdf")
    assert route["modification_reader"] == fast_map.MODIFICATION_READER_PUBLIC
    assert route["modification_reader"] == "RasFim.read_terrain_modifications"
    assert (route["hecras_version"], route["hecras_version_basis"]) == (
        "7.0.1", "detected from the project's plan files (7.0.1)")
    assert route["hecras_runtime"] == "7.0.1"
    assert route["length_units"] == "feet"
    assert [layer["name"] for layer in route["layers"]] == ["Levee A", "Pad"]
    assert _manifest(tmp_path)["routes"][0]["modification_reader"] == (
        fast_map.MODIFICATION_READER_PUBLIC
    )


def test_the_public_reader_receives_ras_version(tmp_path, monkeypatch):
    public = _public_reader([LEVEE])
    resolver, _ = _setup(tmp_path, monkeypatch, ENGINE_V2, None, public=public)
    resolver.ras_version = "6.6"
    assert _run(resolver, tmp_path)[0] == fast_map.TERRAIN_EFFECTIVE
    assert public.calls[0][2] == "6.6"
    route = resolver.modified_route(tmp_path / "a.p01.hdf")
    assert route["hecras_version"] == "6.6"
    assert route["hecras_version_basis"] == "--ras-version 6.6"
    assert route["hecras_runtime"] == "6.6"


def test_a_public_reader_refusal_goes_to_rasmapper_with_its_reason(tmp_path, monkeypatch):
    public = _public_reader(
        refusal="modification 'Ring' feature 0: boundary elevations sampled from the terrain "
                "(no stored profile, no control points) are not qualified")
    resolver, built = _setup(tmp_path, monkeypatch, ENGINE_V2, None, public=public)
    assert _run(resolver, tmp_path)[0] == fast_map.TERRAIN_EXPORT and built == []
    route = resolver.modified_route(tmp_path / "a.p01.hdf")
    assert route["route"] == fast_map.MODIFIED_ROUTE_RASMAPPER
    assert "reader refused" in route["reason"] and "sampled from the terrain" in route["reason"]
    assert route["modification_reader"] == fast_map.MODIFICATION_READER_PUBLIC


MULTI_RING_MESSAGE = (
    "modification 'Pond' feature 0: multi-ring polygon (2 rings: a hole or separate/overlapping "
    "parts) is not qualified [reason: multi_ring_polygon]; use the RAS Mapper export"
)


def _multi_ring_error(with_code=True):
    from ras_commander.fim.errors import FimUnsupportedError

    error = FimUnsupportedError(MULTI_RING_MESSAGE)
    if with_code:
        error.reason = "multi_ring_polygon"
    return error


@pytest.mark.parametrize("case", ["public", "private-coded", "private-message-only"])
def test_a_multi_ring_polygon_refusal_goes_to_rasmapper_with_its_reason(tmp_path, monkeypatch, case):
    """ras-commander #837 refuses polygons with more than one ring up front
    (reason ``multi_ring_polygon``). The code comes from the public record or
    the error's ``reason`` attribute; an older Commander is matched by message."""
    if case == "public":
        public, private = _public_reader(refusal=MULTI_RING_MESSAGE,
                                         refusal_reason="multi_ring_polygon"), None
    else:
        public = None
        private = _reader(error=_multi_ring_error(with_code=case == "private-coded"))
    resolver, built = _setup(tmp_path, monkeypatch, ENGINE_V2, private, public=public)
    kind, kinds = _run(resolver, tmp_path)
    assert kind == fast_map.TERRAIN_EXPORT and kinds == ["export"] and built == []
    route = resolver.modified_route(tmp_path / "a.p01.hdf")
    assert route["route"] == fast_map.MODIFIED_ROUTE_RASMAPPER
    assert route["refusal_reason"] == fast_map.MULTI_RING_POLYGON
    assert "multi-ring polygon" in route["reason"]
    assert _manifest(tmp_path)["routes"][0]["refusal_reason"] == "multi_ring_polygon"


def test_refusal_reason_codes():
    assert fast_map.modification_refusal_reason("other_code", MULTI_RING_MESSAGE) == "other_code"
    assert fast_map.modification_refusal_reason(None, MULTI_RING_MESSAGE) == "multi_ring_polygon"
    assert fast_map.modification_refusal_reason(None, "shape-Z polygons ...") is None
    assert fast_map.modification_refusal_reason(None, None) is None


def test_a_public_reader_unit_conflict_is_a_resolution_failure(tmp_path, monkeypatch):
    public = _public_reader([LEVEE], refusal="project files state different units", units=None)
    resolver, built = _setup(tmp_path, monkeypatch, ENGINE_V2, None, public=public)
    assert _run(resolver, tmp_path)[0] == fast_map.TERRAIN_EXPORT and built == []
    reason = resolver.modified_route(tmp_path / "a.p01.hdf")["reason"]
    assert reason.startswith("could not resolve the project's HEC-RAS version or units")
    assert "different units" in reason


def test_the_private_reader_is_the_fallback_and_recorded(tmp_path, monkeypatch):
    """ras-commander with _terrain_mods but no public reader."""
    private = _reader([LEVEE, PAD])
    resolver, built = _setup(tmp_path, monkeypatch, ENGINE_V2, private, public=None)
    assert _run(resolver, tmp_path)[0] == fast_map.TERRAIN_EFFECTIVE and len(built) == 1
    assert private.calls and private.calls[0][1:] == ("feet", "7.0.1")
    route = resolver.modified_route(tmp_path / "a.p01.hdf")
    assert route["modification_reader"] == fast_map.MODIFICATION_READER_PRIVATE
    assert route["modification_reader"] == "ras_commander.fim._terrain_mods.read_layers"
    assert _manifest(tmp_path)["routes"][0]["modification_reader"] == (
        fast_map.MODIFICATION_READER_PRIVATE
    )


def test_no_reader_keeps_rasmapper_and_records_none(tmp_path, monkeypatch):
    """ras-commander 0.117/0.118: neither reader."""
    resolver, built = _setup(tmp_path, monkeypatch, ENGINE_V2, None, public=None)
    assert _run(resolver, tmp_path)[0] == fast_map.TERRAIN_EXPORT and built == []
    route = resolver.modified_route(tmp_path / "a.p01.hdf")
    assert route["route"] == fast_map.MODIFIED_ROUTE_RASMAPPER
    assert "0.119.0" in route["reason"]
    assert "modification_reader" in route and route["modification_reader"] is None


def test_reader_selection_follows_the_installed_commander(monkeypatch):
    """_modification_reader() picks public, then private, then None."""
    public = _public_reader([LEVEE])
    _readers(monkeypatch, private=_reader([LEVEE]), public=public)
    assert fast_map._modification_reader().name == fast_map.MODIFICATION_READER_PUBLIC
    _readers(monkeypatch, private=_reader([LEVEE]), public=None)
    assert fast_map._modification_reader().name == fast_map.MODIFICATION_READER_PRIVATE
    _readers(monkeypatch, private=None, public=None)
    assert fast_map._modification_reader() is None


def test_the_installed_commander_reader_is_detected():
    """Whatever ras-commander is installed, detection agrees with its API."""
    from ras_commander.fim import RasFim

    reader = fast_map._modification_reader()
    if callable(getattr(RasFim, "read_terrain_modifications", None)):
        assert reader is not None and reader.name == fast_map.MODIFICATION_READER_PUBLIC
    elif fast_map._private_modification_reader() is not None:
        assert reader is not None and reader.name == fast_map.MODIFICATION_READER_PRIVATE
    else:
        assert reader is None


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
    resolver, built = _setup(tmp_path, monkeypatch, engine, _reader([LEVEE]), detected="6.6")
    assert _run(resolver, tmp_path)[0] == fast_map.TERRAIN_EXPORT and built == []
    assert "HEC-RAS 6.6" in resolver.modified_route(tmp_path / "a.p01.hdf")["reason"]


def test_ras_version_selects_the_modification_rules(tmp_path, monkeypatch):
    reader = _reader([LEVEE])
    resolver, _ = _setup(tmp_path, monkeypatch, ENGINE_V2, reader)
    resolver.ras_version = "6.6"
    assert _run(resolver, tmp_path)[0] == fast_map.TERRAIN_EFFECTIVE
    assert reader.calls[0][2] == "6.6"
    assert resolver.modified_route(tmp_path / "a.p01.hdf")["hecras_version"] == "6.6"


# ---------------------------------------------------------------------------
# One HEC-RAS release for the export and the native route
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("ras_version, detected, expected", [
    (None, "6.6", "6.6"),       # no --ras-version: the release the export detects
    (None, "7.0.1", "7.0.1"),
    ("6.6", "7.0.1", "6.6"),    # --ras-version wins over the project's plan files
    ("7.0.1", "6.6", "7.0.1"),
])
@patch("ras_commander.RasFim")
def test_the_export_reader_and_native_build_use_one_release(
    fim, tmp_path, monkeypatch, ras_version, detected, expected
):
    """Before, without --ras-version the export used the release init_ras_project
    detects from the plan files while the reader and the native build used
    Commander's own guess (newest plan/geometry Program Version, else 7.0.1):
    Middle Cahaba (plans 7.00, geometry 7.01) exported with 7.0 but staged 7.0.1
    rules. Now all three take the one resolved release."""
    public = _public_reader([LEVEE])
    resolver, _ = _setup(tmp_path, monkeypatch, ENGINE_V2, None, public=public, detected=detected)
    resolver.ras_version = ras_version
    monkeypatch.setattr(resolver, "_effective", fast_map.TerrainResolver._effective.__get__(resolver))
    monkeypatch.setattr(resolver, "for_plan", fast_map.TerrainResolver.for_plan.__get__(resolver))
    monkeypatch.setattr(fast_map, "_has_kwargs", lambda function, *names: True)
    exported = []

    def export(prj, target, **kwargs):
        exported.append(kwargs["hecras_version"])
        return target

    monkeypatch.setattr("ras2cng.terrain.export_modified_terrain", export)
    # The native build is refused, so the export runs too: both are recorded.
    fim.build_effective_terrain.side_effect = RuntimeError("refused for the test")
    kind, kinds = _run(resolver, tmp_path)
    assert kinds == ["export"] and kind == fast_map.TERRAIN_EXPORT
    assert public.calls[0][2] == expected
    assert fim.build_effective_terrain.call_args.kwargs["hecras_version"] == expected
    assert exported == [expected]
    route = resolver.modified_route(tmp_path / "a.p01.hdf")
    assert route["route"] == fast_map.MODIFIED_ROUTE_NATIVE and route["fallback"]
    assert route["hecras_runtime"] == route["hecras_version"] == expected


def test_an_unresolved_export_release_keeps_the_terrain_on_rasmapper(tmp_path, monkeypatch):
    """No release can be resolved for the export: the native route does not guess one."""
    public = _public_reader([LEVEE])
    resolver, built = _setup(tmp_path, monkeypatch, ENGINE_V2, None, public=public, detected=None)
    assert _run(resolver, tmp_path)[0] == fast_map.TERRAIN_EXPORT and built == []
    assert public.calls == []
    route = resolver.modified_route(tmp_path / "a.p01.hdf")
    assert route["route"] == fast_map.MODIFIED_ROUTE_RASMAPPER
    assert "could not be resolved" in route["reason"] and "no release found" in route["reason"]


@pytest.mark.parametrize("detected, fragment", [
    ("7.0", "unsupported"),          # 7.0.0: the export refuses it (known modification defect)
    ("6.5", "HEC-RAS 6.5"),          # exported by 6.5; the engine's rules are 6.6 and 7.0.1
    ("7.0.2", "HEC-RAS 7.0.2"),
])
def test_an_unqualified_export_release_keeps_the_terrain_on_rasmapper(
    tmp_path, monkeypatch, detected, fragment
):
    public = _public_reader([LEVEE])
    resolver, built = _setup(tmp_path, monkeypatch, ENGINE_V2, None, public=public, detected=detected)
    assert _run(resolver, tmp_path)[0] == fast_map.TERRAIN_EXPORT and built == []
    assert public.calls == []
    route = resolver.modified_route(tmp_path / "a.p01.hdf")
    assert route["route"] == fast_map.MODIFIED_ROUTE_RASMAPPER
    assert fragment in route["reason"]
    assert route["hecras_runtime"] == detected


def test_resolve_hecras_runtime(monkeypatch):
    """--ras-version, else init_ras_project's detection, checked as the export checks it."""
    runtime = fast_map.resolve_hecras_runtime("x.prj", "7.0.1")
    assert (runtime.label, runtime.canonical, runtime.problem) == ("7.0.1", "7.0.1", None)
    assert runtime.basis == "--ras-version 7.0.1"
    assert fast_map.resolve_hecras_runtime("x.prj", "6.6").canonical == "6.6"
    refused = fast_map.resolve_hecras_runtime("x.prj", "7.0")
    assert refused.canonical is None and "7.0.0" in refused.problem

    import ras_commander

    seen = []

    def init(prj, **kwargs):
        seen.append(kwargs)
        return SimpleNamespace(ras_version="6.6")

    monkeypatch.setattr(ras_commander, "init_ras_project", init)
    detected = fast_map.resolve_hecras_runtime("x.prj", None)
    assert (detected.label, detected.canonical) == ("6.6", "6.6")
    assert "detected" in detected.basis and "ras_version" not in seen[0]
    monkeypatch.setattr(ras_commander, "init_ras_project",
                        lambda prj, **kw: SimpleNamespace(ras_version=None))
    missing = fast_map.resolve_hecras_runtime("x.prj", None)
    assert missing.canonical is None and "--ras-version" in missing.problem


# ---------------------------------------------------------------------------
# HDF layers RAS Mapper does not apply (no .rasmap group named "Modifications")
# ---------------------------------------------------------------------------

MISNAMED = {
    "code": "modification_group_misnamed", "group": "Hydroconnectors",
    "layers": ["HC-0230", "HC-0137"],
    "message": "a.rasmap registers modification group 'Hydroconnectors' under terrain T.hdf; "
               "RAS Mapper (HEC-RAS 6.6, 7.0.1) applies modifications only from a group named "
               "exactly 'Modifications' and ignores this one",
}


def test_layers_rasmapper_does_not_apply_leave_the_terrain_unmodified(tmp_path, monkeypatch):
    """The original Helena terrain: HDF layers under a misnamed .rasmap group.
    ras-commander 0.119 says RAS Mapper applies none, so it is mapped as an
    unmodified terrain (the source TIFF may be used) and the warning is recorded."""
    public = _public_reader(refusal="modification layers share a Priority")
    resolver, built = _setup(tmp_path, monkeypatch, ENGINE_V2, None, public=public)
    (tmp_path / "a.rasmap").write_text("<RASMapper />")
    asked = []

    def unapplied(hdf, rasmap):
        asked.append((hdf, rasmap))
        return [dict(MISNAMED)]

    monkeypatch.setattr(fast_map, "rasmap_unapplied_modifications", unapplied)
    monkeypatch.setattr(resolver, "_source", lambda plan: (tmp_path / "src.tif", tmp_path / "T.hdf"))
    kind, kinds = _run(resolver, tmp_path)
    assert kind == fast_map.TERRAIN_SOURCE and kinds == ["source"]
    assert asked == [(tmp_path / "T.hdf", tmp_path / "a.rasmap")]
    assert public.calls == [] and built == []
    assert resolver.modifications(tmp_path / "a.p01.hdf") == []
    route = resolver.modified_route(tmp_path / "a.p01.hdf")
    assert route["route"] == fast_map.MODIFIED_ROUTE_UNAPPLIED == "unmodified"
    assert route["modifications"] == ["hdf:Levee A"]
    assert route["rasmap_warnings"][0]["code"] == "modification_group_misnamed"
    assert "Hydroconnectors" in route["reason"]
    assert _manifest(tmp_path)["routes"][0]["route"] == "unmodified"
    assert any("does not apply" in note for note in resolver.notes)


def test_applied_layers_without_a_rasmap_stay_modified(tmp_path, monkeypatch):
    """No .rasmap: Commander is not asked, and the HDF layers count."""
    resolver, _ = _setup(tmp_path, monkeypatch, ENGINE_V2, _reader([LEVEE]))
    with patch("ras_commander.RasFim") as fim:
        assert resolver.modifications(tmp_path / "a.p01.hdf") == ["hdf:Levee A"]
        fim.top_priority_terrain_source.assert_not_called()
    assert resolver.modified_route(tmp_path / "a.p01.hdf")["route"] == fast_map.MODIFIED_ROUTE_NATIVE


@pytest.mark.parametrize("info, expected", [
    ({"modified": False, "rasmap_checked": True, "rasmap_warnings": [MISNAMED]}, [MISNAMED]),
    ({"modified": False, "rasmap_checked": True,
      "rasmap_warnings": [{**MISNAMED, "code": "modification_group_missing"}]},
     [{**MISNAMED, "code": "modification_group_missing"}]),
    ({"modified": True, "rasmap_checked": True, "rasmap_warnings": [MISNAMED]}, None),
    ({"modified": False, "rasmap_checked": False, "rasmap_warnings": [MISNAMED]}, None),
    ({"modified": False, "rasmap_checked": True,
      "rasmap_warnings": [{**MISNAMED, "code": "rasmap_not_checked"}]}, None),
    (RuntimeError("unproven priority"), None),
])
def test_rasmap_unapplied_modifications(tmp_path, info, expected):
    with patch("ras_commander.RasFim") as fim:
        if isinstance(info, Exception):
            fim.top_priority_terrain_source.side_effect = info
        else:
            fim.top_priority_terrain_source.return_value = info
        assert fast_map.rasmap_unapplied_modifications(tmp_path / "T.hdf", tmp_path / "a.rasmap") == expected
        assert fast_map.rasmap_unapplied_modifications(tmp_path / "T.hdf", None) is None


# ---------------------------------------------------------------------------
# The route manifest: one per run; a snapshot per product group
# ---------------------------------------------------------------------------


def test_a_new_run_clears_the_previous_runs_route_manifest(tmp_path, monkeypatch):
    """A manifest an earlier run left in the work directory does not describe a
    run whose terrain is now unmodified (no route is written)."""
    work = tmp_path / "w"
    work.mkdir()
    stale = work / fast_map.TERRAIN_ROUTE_MANIFEST
    stale.write_text(json.dumps({"schema": "ras2cng-terrain-route/1",
                                 "routes": [{"route": "native-effective"}]}))
    resolver, _ = _setup(tmp_path, monkeypatch, ENGINE_V2, _reader([LEVEE]))
    assert not stale.exists()
    monkeypatch.setattr(resolver, "modifications", lambda plan: [])
    assert resolver.modified_route(tmp_path / "a.p01.hdf") is None
    assert not stale.exists()


def test_routes_of_this_run_stay_in_the_manifest(tmp_path, monkeypatch):
    """Clearing happens once per run; routes decided for this run's plans are kept."""
    resolver, _ = _setup(tmp_path, monkeypatch, ENGINE_V2, _reader([LEVEE]))
    resolver.modified_route(tmp_path / "a.p01.hdf")
    other = _terrain_h5(tmp_path / "U.hdf")
    monkeypatch.setattr(resolver, "_terrain_hdf_for",
                        lambda plan: other if plan.name == "a.p02.hdf" else tmp_path / "T.hdf")
    resolver.modified_route(tmp_path / "a.p02.hdf")
    hdfs = [r["terrain_hdf"] for r in _manifest(tmp_path)["routes"]]
    assert hdfs == [str(tmp_path / "T.hdf"), str(other)]


def test_each_product_group_keeps_its_own_route_snapshot(tmp_path, monkeypatch):
    """WSE/Depth maps on the native terrain; velocity is then refused it and
    falls back. WSE's record must not show velocity's fallback, in MapResult
    (the returned snapshot) or in the manifest."""
    resolver, _ = _setup(tmp_path, monkeypatch, ENGINE_V2, _reader([LEVEE]))
    plan = tmp_path / "a.p01.hdf"
    assert _run(resolver, tmp_path)[0] == fast_map.TERRAIN_EFFECTIVE
    wse = resolver.record_product_group(plan, "WSE/Depth", fast_map.TERRAIN_EFFECTIVE)
    kind, _ = _run(resolver, tmp_path, refuse=(fast_map.TERRAIN_EFFECTIVE,))
    assert kind == fast_map.TERRAIN_EXPORT
    velocity = resolver.record_product_group(plan, "velocity", kind)
    assert "fallback" not in wse and wse["terrain"] == "effective"
    assert wse["product_group"] == "WSE/Depth" and "product_groups" not in wse
    assert len(velocity["fallback"]) == 1 and velocity["terrain"] == "export"
    # The snapshot is independent of the shared record.
    resolver._record_fallback(plan, "a third refusal")
    assert len(velocity["fallback"]) == 1
    groups = _manifest(tmp_path)["routes"][0]["product_groups"]["a.p01.hdf"]
    assert groups["WSE/Depth"] == {"terrain": "effective", "fallback": []}
    assert groups["velocity"]["terrain"] == "export" and len(groups["velocity"]["fallback"]) == 1
    assert len(_manifest(tmp_path)["routes"][0]["fallback"]) == 2


def test_an_unmodified_terrain_has_no_product_group_snapshot(tmp_path, monkeypatch):
    resolver, _ = _setup(tmp_path, monkeypatch, ENGINE_V2, _reader([LEVEE]))
    monkeypatch.setattr(resolver, "modifications", lambda plan: [])
    assert resolver.record_product_group(tmp_path / "a.p01.hdf", "WSE/Depth", "source") is None
    assert not (tmp_path / "w" / fast_map.TERRAIN_ROUTE_MANIFEST).exists()


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
    monkeypatch.setattr(fast_map, "resolve_hecras_runtime", _runtime())
    monkeypatch.setattr(fast_map, "preflight", lambda plan, r, mode, **kw: {"mode": "sloping"})
    monkeypatch.setattr(fast_map, "engine_method",
                        lambda kind: dict(engine) if kind == "effective_terrain" else {})
    _readers(monkeypatch, public=_public_reader(layers))
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
    assert result.fast_map_terrain_route["wse"]["product_group"] == "WSE/Depth"
    assert result.fast_map_terrain_route["wse"]["terrain"] == expected_kind
    assert (result.fast_map_terrain_route["depth"]["reason"] is None) == (
        expected_route == "native-effective"
    )
    if engine is ENGINE_V2:
        assert result.fast_map_terrain_route["wse"]["modification_reader"] == (
            fast_map.MODIFICATION_READER_PUBLIC
        )
