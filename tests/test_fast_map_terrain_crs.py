"""Terrain CRSs without an EPSG code on the native route (Fast-Map ``crs_any``).

A modified terrain whose CRS has no EPSG code (Helena: ESRI:102630, NAD83 /
Alabama West in US survey feet) takes the native effective terrain only when
the engine's ``effective_terrain`` and ``sloping`` records advertise
``crs_any`` (ras-commander-fast-map 0.19+) and ras-commander passes it on.
Otherwise it goes to the RAS Mapper export up front, with the reason
recorded, instead of after a Commander refusal. EPSG terrains are unchanged.
The engine records and Commander's reader are faked; no engine is needed.
"""
from __future__ import annotations

import h5py
import pytest
from pyproj import CRS

from ras2cng import fast_map
from test_fast_map_modified_terrain import ENGINE_V2, LEVEE, _reader, _run, _setup, _manifest

ESRI = "ESRI:102630"


def _with_projection(tmp_path, crs):
    with h5py.File(tmp_path / "T.hdf", "a") as handle:
        handle.attrs["Projection"] = CRS.from_user_input(crs).to_wkt().encode()


def _engine(monkeypatch, effective_crs_any, sloping_crs_any):
    records = {
        "effective_terrain": dict(ENGINE_V2, **({"crs_any": True} if effective_crs_any else {})),
        "sloping": {"crs_any": True} if sloping_crs_any else {},
    }
    monkeypatch.setattr(fast_map, "engine_method", lambda kind: dict(records.get(kind, {})))


def test_esri_terrain_with_crs_any_takes_the_native_route(tmp_path, monkeypatch):
    resolver, built = _setup(tmp_path, monkeypatch, ENGINE_V2, _reader([LEVEE]))
    _with_projection(tmp_path, ESRI)
    _engine(monkeypatch, True, True)
    monkeypatch.setattr(fast_map, "commander_crs_any", lambda: True)
    kind, kinds = _run(resolver, tmp_path)
    assert kind == fast_map.TERRAIN_EFFECTIVE and kinds == ["effective"] and len(built) == 1
    route = resolver.modified_route(tmp_path / "a.p01.hdf")
    assert route["route"] == fast_map.MODIFIED_ROUTE_NATIVE and route["reason"] is None
    assert route["terrain_crs"] == {"name": "NAD_1983_StatePlane_Alabama_West_FIPS_0102_Feet",
                                    "authority": ESRI, "epsg": None, "request": "geokeys"}
    assert _manifest(tmp_path)["routes"][0]["terrain_crs"]["request"] == "geokeys"


@pytest.mark.parametrize("effective, sloping, missing", [
    (False, False, "effective_terrain and sloping"),
    (True, False, "sloping"),
    (False, True, "effective_terrain"),
])
def test_esri_terrain_without_crs_any_goes_to_rasmapper_with_the_reason(
        tmp_path, monkeypatch, effective, sloping, missing):
    reader = _reader([LEVEE])
    resolver, built = _setup(tmp_path, monkeypatch, ENGINE_V2, reader)
    _with_projection(tmp_path, ESRI)
    _engine(monkeypatch, effective, sloping)
    monkeypatch.setattr(fast_map, "commander_crs_any", lambda: True)
    kind, kinds = _run(resolver, tmp_path)
    assert kind == fast_map.TERRAIN_EXPORT and kinds == ["export"] and built == []
    assert reader.calls == [], "decided before reading the modifications"
    route = resolver.modified_route(tmp_path / "a.p01.hdf")
    assert route["route"] == fast_map.MODIFIED_ROUTE_RASMAPPER
    assert f"{ESRI} has no EPSG code" in route["reason"]
    assert f"engine's {missing} method(s) do not advertise crs_any" in route["reason"]
    assert route["terrain_crs"]["request"] == "geokeys"


def test_esri_terrain_needs_commander_crs_any_too(tmp_path, monkeypatch):
    resolver, built = _setup(tmp_path, monkeypatch, ENGINE_V2, _reader([LEVEE]))
    _with_projection(tmp_path, ESRI)
    _engine(monkeypatch, True, True)
    monkeypatch.setattr(fast_map, "commander_crs_any", lambda: False)
    kind, _ = _run(resolver, tmp_path)
    assert kind == fast_map.TERRAIN_EXPORT and built == []
    route = resolver.modified_route(tmp_path / "a.p01.hdf")
    assert "ras-commander does not pass crs_any" in route["reason"]


def test_epsg_terrain_is_unchanged_without_crs_any(tmp_path, monkeypatch):
    resolver, built = _setup(tmp_path, monkeypatch, ENGINE_V2, _reader([LEVEE]))
    _with_projection(tmp_path, "EPSG:2277")
    _engine(monkeypatch, False, False)
    monkeypatch.setattr(fast_map, "commander_crs_any", lambda: False)
    kind, _ = _run(resolver, tmp_path)
    assert kind == fast_map.TERRAIN_EFFECTIVE and len(built) == 1
    route = resolver.modified_route(tmp_path / "a.p01.hdf")
    assert route["terrain_crs"]["epsg"] == 2277 and route["terrain_crs"]["request"] == "epsg"


def test_terrain_crs_route_without_a_readable_projection(tmp_path):
    with h5py.File(tmp_path / "none.hdf", "w"):
        pass
    assert fast_map.terrain_crs_route(tmp_path / "none.hdf") == (None, {})
    assert fast_map.terrain_crs_route(None) == (None, {})
    assert fast_map.terrain_crs_route(tmp_path / "missing.hdf") == (None, {})


def test_commander_crs_any_reflects_the_installed_ras_commander():
    from ras_commander.fim import _engine

    assert fast_map.commander_crs_any() is callable(getattr(_engine, "crs_any", None))


def test_unmodified_untagged_terrain_goes_to_commander_for_boundaries(tmp_path, monkeypatch):
    """Unmodified Helena: RAS Mapper's source TIFF carries no CRS (ESRI:102630 has
    no EPSG code). ras2cng applies no CRS gate of its own: the source goes to
    ras-commander, which takes the terrain HDF Projection for crs_any engines
    and refuses otherwise (then RAS Mapper maps the boundary)."""
    import numpy as np
    import rasterio
    from rasterio.transform import from_origin
    from test_fast_map import _terrain_h5

    terrain = tmp_path / "Terrain"
    terrain.mkdir()
    hdf = _terrain_h5(terrain / "Helena.hdf")
    with h5py.File(hdf, "a") as handle:
        handle.attrs["Projection"] = CRS.from_user_input(ESRI).to_wkt().encode()
    tif = terrain / "Helena.a.tif"
    with rasterio.open(tif, "w", driver="GTiff", width=2, height=2, count=1, dtype="float32",
                       transform=from_origin(0, 2, 1, 1), nodata=-9999) as dst:
        dst.write(np.zeros((1, 2, 2), "float32"))
    monkeypatch.setattr(fast_map, "terrain_crs_route",
                        lambda *a: pytest.fail("the CRS route gates modified terrains only"))
    resolver = fast_map.TerrainResolver(tmp_path / "m.prj", tmp_path / "w", terrain_hdf=hdf)
    assert resolver.boundary_terrain(tmp_path / "m.p01.hdf") == (tif, hdf)
