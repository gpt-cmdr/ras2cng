"""Stored vs active terrain modifications (``.rasmap`` activation).

RAS Mapper applies a terrain HDF's modification layers only when the
project's ``.rasmap`` lists them in the ``ElevationModificationGroup`` under
the terrain layer. The fixtures are synthetic terrain HDFs and ``.rasmap``
files.
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


def _rasmap(path, terrain_filename=r".\Terrain\T.hdf", listed=None):
    """A .rasmap with one terrain layer; ``listed`` None = no Modifications group."""
    group = ""
    if listed is not None:
        children = "".join(
            f'<Layer Name="{name}" Type="ElevationModificationLayer" '
            f'Filename="{terrain_filename}" />'
            for name in listed
        )
        group = f'<Layer Name="Modifications" Type="ElevationModificationGroup">{children}</Layer>'
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


def test_listed_layers_are_active(tmp_path):
    hdf = _terrain(tmp_path / "Terrain" / "T.hdf")
    rasmap = _rasmap(tmp_path / "m.rasmap", listed=["Channels"])

    status = terrain_modification_status(hdf, rasmap)
    assert status.active == ["Channels"]
    assert status.inactive == ["Round_Piers"]
    assert active_terrain_modifications(hdf, rasmap) == ["hdf:Channels"]
    assert status.to_dict()["inactive"] == ["Round_Piers"]


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
    rasmap = _rasmap(tmp_path / "project" / "m.rasmap", listed=[])
    status = terrain_modification_status(hdf, rasmap)
    assert status.basis == "rasmap"
    assert status.active == []


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
