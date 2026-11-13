"""Stored vs active terrain modifications (``.rasmap`` activation).

RAS Mapper applies a terrain HDF's modification layers only when the
project's ``.rasmap`` terrain layer has an ``ElevationModificationGroup``
named exactly ``Modifications``; with one it applies every stored layer,
whatever the group lists. The fixtures are synthetic terrain HDFs and
``.rasmap`` files.
"""

from __future__ import annotations

import h5py
import pytest

from ras2cng import fast_map
from ras2cng.terrain import (
    active_terrain_modifications,
    terrain_modification_status,
    terrain_modifications,
)

LAYERS = ["Channels", "Round_Piers"]


def _terrain(path, modifications=LAYERS):
    path.parent.mkdir(parents=True, exist_ok=True)
    with h5py.File(path, "w") as handle:
        handle.create_group("Terrain")
        group = handle.create_group("Modifications")
        for name in modifications:
            group.create_group(name)
    return path


def _rasmap(path, terrain_filename=r".\Terrain\T.hdf", listed=None, group_name="Modifications"):
    """A .rasmap with one terrain layer; ``listed`` None = no modification group."""
    group = ""
    if listed is not None:
        children = "".join(
            f'<Layer Name="{name}" Type="ElevationModificationLayer" '
            f'Filename="{terrain_filename}" />'
            for name in listed
        )
        group = (
            f'<Layer Name="{group_name}" Type="ElevationModificationGroup">{children}</Layer>'
        )
    path.write_text(
        '<RASMapper><Terrains Checked="True">'
        f'<Layer Name="T" Type="TerrainLayer" Checked="True" Filename="{terrain_filename}">'
        '<ResampleMethod>near</ResampleMethod><Surface On="True" />'
        f"{group}</Layer></Terrains></RASMapper>"
    )
    return path


def test_rasmap_without_modifications_group_activates_nothing(tmp_path):
    hdf = _terrain(tmp_path / "Terrain" / "T.hdf")
    rasmap = _rasmap(tmp_path / "m.rasmap", listed=None)

    status = terrain_modification_status(hdf, rasmap)
    assert status.stored == LAYERS
    assert status.registered == []
    assert status.active == []
    assert status.inactive == LAYERS
    assert status.basis == "rasmap"
    assert active_terrain_modifications(hdf, rasmap) == []
    # The inventory still lists what the HDF stores.
    assert terrain_modifications(hdf, rasmap) == ["hdf:Channels", "hdf:Round_Piers"]


def test_modifications_group_applies_every_stored_layer(tmp_path):
    # RAS Mapper does not consult the group's per-layer entries: a partial
    # listing still applies every stored layer.
    hdf = _terrain(tmp_path / "Terrain" / "T.hdf")
    rasmap = _rasmap(tmp_path / "m.rasmap", listed=["Channels"])

    status = terrain_modification_status(hdf, rasmap)
    assert status.registered == ["Channels"]
    assert status.active == LAYERS
    assert status.inactive == []
    assert active_terrain_modifications(hdf, rasmap) == ["hdf:Channels", "hdf:Round_Piers"]
    assert status.to_dict()["inactive"] == []


def test_misnamed_modification_group_activates_nothing(tmp_path):
    hdf = _terrain(tmp_path / "Terrain" / "T.hdf")
    rasmap = _rasmap(tmp_path / "m.rasmap", listed=LAYERS, group_name="Hydroconnectors")

    status = terrain_modification_status(hdf, rasmap)
    assert status.registered == LAYERS
    assert status.active == []
    assert status.inactive == LAYERS
    # The inventory still lists what the HDF stores.
    assert terrain_modifications(hdf, rasmap)[:2] == ["hdf:Channels", "hdf:Round_Piers"]


def test_unreadable_rasmap_falls_back_to_the_hdf(tmp_path, monkeypatch):
    import xml.etree.ElementTree as ET

    hdf = _terrain(tmp_path / "Terrain" / "T.hdf")
    rasmap = _rasmap(tmp_path / "m.rasmap", listed=None)

    def unreadable(*_args, **_kwargs):
        raise PermissionError("denied")

    monkeypatch.setattr(ET, "parse", unreadable)
    status = terrain_modification_status(hdf, rasmap)
    assert status.basis == "hdf"
    assert status.active == LAYERS
    assert any(problem.startswith("rasmap: unreadable") for problem in status.problems)


def test_existing_terrain_with_the_same_file_name_is_not_matched(tmp_path):
    # The .rasmap's terrain exists; another T.hdf elsewhere is a different terrain.
    _terrain(tmp_path / "project" / "Terrain" / "T.hdf")
    other = _terrain(tmp_path / "elsewhere" / "T.hdf")
    rasmap = _rasmap(tmp_path / "project" / "m.rasmap", listed=None)
    status = terrain_modification_status(other, rasmap)
    assert status.basis == "hdf"
    assert status.active == LAYERS


def test_without_rasmap_every_stored_layer_counts(tmp_path):
    hdf = _terrain(tmp_path / "T.hdf")
    status = terrain_modification_status(hdf)
    assert status.basis == "hdf"
    assert status.active == LAYERS
    assert active_terrain_modifications(hdf) == ["hdf:Channels", "hdf:Round_Piers"]


def test_terrain_missing_from_rasmap_counts_every_stored_layer(tmp_path):
    hdf = _terrain(tmp_path / "Terrain" / "T.hdf")
    rasmap = _rasmap(tmp_path / "m.rasmap", terrain_filename=r".\Terrain\Other.hdf")
    status = terrain_modification_status(hdf, rasmap)
    assert status.basis == "hdf"
    assert status.active == LAYERS
    assert any("no terrain layer" in problem for problem in status.problems)


def test_moved_project_matches_terrain_by_file_name(tmp_path):
    # The .rasmap points at the original location; the HDF was copied elsewhere.
    hdf = _terrain(tmp_path / "copy" / "T.hdf")
    (tmp_path / "project").mkdir()
    rasmap = _rasmap(tmp_path / "project" / "m.rasmap", listed=None)
    status = terrain_modification_status(hdf, rasmap)
    assert status.basis == "rasmap"
    assert status.active == []
    # An empty Modifications group still applies every stored layer.
    rasmap = _rasmap(tmp_path / "project" / "m.rasmap", listed=[])
    assert terrain_modification_status(hdf, rasmap).active == LAYERS


def test_listed_layer_missing_from_hdf_stays_active(tmp_path):
    hdf = _terrain(tmp_path / "Terrain" / "T.hdf", modifications=[])
    rasmap = _rasmap(tmp_path / "m.rasmap", listed=["Berm"])
    assert active_terrain_modifications(hdf, rasmap) == ["rasmap:Berm (not in the HDF)"]


def test_unreadable_modifications_group_is_never_unmodified(tmp_path):
    hdf = tmp_path / "Terrain" / "T.hdf"
    hdf.parent.mkdir()
    with h5py.File(hdf, "w") as handle:
        handle.create_group("Terrain")
        handle["Modifications"] = h5py.SoftLink("/Terrain")
    rasmap = _rasmap(tmp_path / "m.rasmap", listed=None)
    assert active_terrain_modifications(hdf, rasmap) == ["hdf:/Modifications (link)"]


def test_resolver_maps_natively_when_rasmap_activates_nothing(tmp_path, monkeypatch):
    """Stored-but-inactive modifications no longer send the terrain to RAS Mapper."""
    hdf = _terrain(tmp_path / "Terrain" / "T.hdf")
    _rasmap(tmp_path / "a.rasmap", listed=None)
    resolver = fast_map.TerrainResolver(tmp_path / "a.prj", tmp_path / "w", terrain_name="T")
    monkeypatch.setattr(fast_map, "source_terrain_supported", lambda: True)
    source = tmp_path / "src.tif"
    monkeypatch.setattr(resolver, "_source", lambda plan: (source, hdf))
    monkeypatch.setattr(resolver, "_terrain_hdf_for", lambda plan: hdf)
    monkeypatch.setattr(resolver, "for_plan", lambda plan: tmp_path / "export.tif")

    assert resolver.modifications(tmp_path / "a.p01.hdf") == []
    _, kind = fast_map.with_terrain(resolver, tmp_path / "a.p01.hdf", lambda tif, h, k: "ok")
    assert kind == fast_map.TERRAIN_SOURCE
    assert any("activates 0" in note for note in resolver.notes)


def test_resolver_uses_the_export_when_rasmap_activates_a_layer(tmp_path, monkeypatch):
    hdf = _terrain(tmp_path / "Terrain" / "T.hdf")
    _rasmap(tmp_path / "a.rasmap", listed=LAYERS)
    resolver = fast_map.TerrainResolver(tmp_path / "a.prj", tmp_path / "w", terrain_name="T")
    monkeypatch.setattr(fast_map, "source_terrain_supported", lambda: True)
    monkeypatch.setattr(resolver, "_source", lambda plan: (tmp_path / "src.tif", hdf))
    monkeypatch.setattr(resolver, "_terrain_hdf_for", lambda plan: hdf)
    monkeypatch.setattr(resolver, "for_plan", lambda plan: tmp_path / "export.tif")

    _, kind = fast_map.with_terrain(resolver, tmp_path / "a.p01.hdf", lambda tif, h, k: "ok")
    assert kind == fast_map.TERRAIN_EXPORT


def test_boundary_terrain_accepts_inactive_modifications(tmp_path):
    hdf = _terrain(tmp_path / "Terrain" / "T.hdf", modifications=["HighGround"])
    (tmp_path / "Terrain" / "T.a.tif").touch()
    _rasmap(tmp_path / "m.rasmap", listed=None)
    resolver = fast_map.TerrainResolver(tmp_path / "m.prj", tmp_path / "w", terrain_hdf=hdf)
    assert resolver.boundary_terrain(tmp_path / "m.p01.hdf") == (
        tmp_path / "Terrain" / "T.a.tif", hdf,
    )
    _rasmap(tmp_path / "m.rasmap", listed=["HighGround"])
    with pytest.raises(fast_map.FastMapUnavailable, match="active modification"):
        resolver.boundary_terrain(tmp_path / "m.p01.hdf")


def test_inspect_reports_active_and_stored(tmp_path):
    from ras2cng.project import _modification_cell, _terrain_modification_record

    hdf = _terrain(tmp_path / "Terrain" / "T.hdf")
    rasmap = _rasmap(tmp_path / "m.rasmap", listed=None)
    record = _terrain_modification_record(hdf, rasmap)
    assert record["stored"] == LAYERS and record["active"] == []
    assert _modification_cell(record) == "0/2"
    assert _modification_cell(_terrain_modification_record(hdf, None)) == (
        "2/2 (no .rasmap entry: all counted)"
    )


def _modified_resolver(tmp_path, monkeypatch, basis, route=None):
    import json as _json

    hdf = _terrain(tmp_path / "Terrain" / "T.hdf", modifications=["HighGround"])
    (tmp_path / "Terrain" / "T.a.tif").touch()
    _rasmap(tmp_path / "m.rasmap", listed=["HighGround"])
    resolver = fast_map.TerrainResolver(tmp_path / "m.prj", tmp_path / "w", terrain_hdf=hdf)
    effective = tmp_path / "w" / "T.effective.tif"
    effective.parent.mkdir(parents=True)
    effective.touch()
    (tmp_path / "w" / "T.effective.tif.json").write_text(_json.dumps({"basis": basis}))
    monkeypatch.setattr(resolver, "modified_route", lambda plan: route or {"route": fast_map.MODIFIED_ROUTE_NATIVE})
    monkeypatch.setattr(resolver, "_effective", lambda plan: effective)
    return resolver, hdf, effective


def test_modified_boundary_terrain_is_the_stored_map_effective_terrain(tmp_path, monkeypatch):
    monkeypatch.setattr(fast_map, "commander_stored_map_boundaries", lambda: True)
    resolver, hdf, effective = _modified_resolver(tmp_path, monkeypatch, "stored_map")
    for method in (fast_map.BOUNDARY_DETERMINISTIC, fast_map.BOUNDARY_NATIVE_SERIAL):
        assert resolver.boundary_terrain(tmp_path / "m.p01.hdf", method) == (effective, hdf)


@pytest.mark.parametrize("case", ["export_basis", "rasmapper_route", "old_commander"])
def test_modified_boundary_terrain_refusals_name_the_gate(tmp_path, monkeypatch, case):
    monkeypatch.setattr(fast_map, "commander_stored_map_boundaries", lambda: case != "old_commander")
    route = ({"route": fast_map.MODIFIED_ROUTE_RASMAPPER, "reason": "unqualified form"}
             if case == "rasmapper_route" else None)
    resolver, _, _ = _modified_resolver(
        tmp_path, monkeypatch, "export" if case == "export_basis" else "stored_map", route)
    expected = {"export_basis": "stored-map basis", "rasmapper_route": "unqualified form",
                "old_commander": "ras-commander does not map boundaries"}[case]
    with pytest.raises(fast_map.FastMapUnavailable, match=expected):
        resolver.boundary_terrain(tmp_path / "m.p01.hdf")


def test_commander_stored_map_boundaries_probe():
    from ras_commander.fim import _boundary_inputs
    assert fast_map.commander_stored_map_boundaries() is callable(
        getattr(_boundary_inputs, "stored_map_terrain", None))
