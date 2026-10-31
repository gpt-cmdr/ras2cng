"""fast-map ``invalid_cells`` policy/record and native GeoTIFF delivery.

ras-commander 0.110.2+ maps meshes with invalid cell polygons under an
``invalid_cells`` policy and records the cells (``fim-invalid-cells/1``);
0.110.3 with fast-map 0.11 can write sloping GeoTIFFs natively
(``product_writer="native"``). RasFim is mocked: the fakes below carry real
signatures, because ras2cng capability-detects keyword arguments.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from ras2cng import fast_map
from ras2cng.mapping import generate_result_maps

_SLOPING = {"mode": "sloping", "reduce_shallow_to_horizontal": False,
            "use_depth_weighted_faces": False}

RECORD = {
    "schema": "fim-invalid-cells/1",
    "policy": "repair",
    "count": 1,
    "cells": [{
        "mesh": "BaldEagleCr", "cell_id": 4711, "reason": "self_intersection: Ring Self-intersection",
        "action": "repaired", "original_area": 812.5, "fallback_area": 790.0,
        "pieces": 1, "overlap_with": [4710], "intersects_aoi": None,
    }],
}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv(fast_map.PRODUCT_WRITER_ENV, raising=False)
    monkeypatch.setattr(
        fast_map, "preflight", lambda plan, ras, mode, **kw: fast_map.render_settings(ras, mode)
    )


def _manifest(folder: Path, record=RECORD) -> pd.DataFrame:
    depth, wse = folder / "09_Max_depth.tif", folder / "10_Max_wsel.tif"
    depth.write_bytes(b"depth")
    wse.write_bytes(b"wse")
    engine = {"status": "complete"}
    if record is not None:
        engine["invalid_cells"] = record
    (folder / "fim_manifest.json").write_text(json.dumps(engine))
    frame = pd.DataFrame([{"profile": "Max", "status": "complete",
                           "depth_tif": str(depth), "wse_tif": str(wse), "error": None}])
    if record is not None:
        frame.attrs["invalid_cells"] = record
    return frame


def _new_sloping(calls, record=RECORD):
    """A ras-commander 0.110.3 ``map_sloping_profiles`` (real keyword names)."""
    def map_sloping_profiles(hdf_path, terrain_path, output_folder, *, profiles=None,
                             render_mode="sloping", reduce_shallow_to_horizontal=False,
                             use_depth_weighted_faces=False, threads=1, writer_threads=1,
                             product_writer="rasterio", max_working_mib=1024, timeout=None,
                             write_floodplain=False, terrain_hdf=None, velocity=False,
                             invalid_cells="repair"):
        calls.append(dict(product_writer=product_writer, invalid_cells=invalid_cells,
                          velocity=velocity))
        return _manifest(Path(output_folder), record)
    return map_sloping_profiles


def _old_sloping(calls):
    """A ras-commander 0.110.1 ``map_sloping_profiles``: no policy, no writer."""
    def map_sloping_profiles(hdf_path, terrain_path, output_folder, *, profiles=None,
                             render_mode="sloping", reduce_shallow_to_horizontal=False,
                             use_depth_weighted_faces=False, threads=1, writer_threads=1,
                             max_working_mib=1024, timeout=None, write_floodplain=False,
                             terrain_hdf=None, velocity=False):
        calls.append({})
        return _manifest(Path(output_folder), record=None)
    return map_sloping_profiles


def _map(tmp_path, **kwargs):
    return fast_map.map_plan(
        tmp_path / "x.p01.hdf", tmp_path / "t.tif", tmp_path / "p01",
        map_types=["wse", "depth"], render=_SLOPING, **kwargs,
    )


# ---------------------------------------------------------------------------
# invalid_cells
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value, expected", [
    (None, None), ("repair", "repair"), (" Hull ", "hull"), ("OMIT", "omit"), ("raise", "raise"),
])
def test_normalize_invalid_cells(value, expected):
    assert fast_map.normalize_invalid_cells(value) == expected


def test_normalize_invalid_cells_rejects_unknown():
    with pytest.raises(ValueError, match="invalid_cells must be one of"):
        fast_map.normalize_invalid_cells("drop")


@patch("ras_commander.RasFim")
def test_map_plan_passes_policy_and_records_the_cells(mock_fim, tmp_path, monkeypatch):
    monkeypatch.setattr(fast_map, "native_writer_supported", lambda: False)
    calls = []
    mock_fim.map_sloping_profiles = _new_sloping(calls)

    result = _map(tmp_path, invalid_cells="hull", product_writer="auto")

    assert calls[0]["invalid_cells"] == "hull"
    assert result.invalid_cells == RECORD
    details = json.loads((tmp_path / "p01" / fast_map.FAST_MAP_MANIFEST).read_text())
    assert details["invalid_cells"]["schema"] == fast_map.INVALID_CELLS_SCHEMA
    assert details["ras2cng"]["invalid_cells_policy"] == "hull"
    assert details["ras2cng"]["invalid_cells_count"] == 1


@patch("ras_commander.RasFim")
def test_map_plan_default_policy_is_left_to_the_engine(mock_fim, tmp_path, monkeypatch):
    monkeypatch.setattr(fast_map, "native_writer_supported", lambda: False)
    calls = []
    mock_fim.map_sloping_profiles = _new_sloping(calls)
    result = _map(tmp_path)
    assert calls[0]["invalid_cells"] == "repair"  # the engine's own default, not passed
    assert result.invalid_cells["policy"] == "repair"
    details = json.loads((tmp_path / "p01" / fast_map.FAST_MAP_MANIFEST).read_text())
    assert details["ras2cng"]["invalid_cells_policy"] is None


@patch("ras_commander.RasFim")
def test_older_ras_commander_keeps_working_without_a_record(mock_fim, tmp_path):
    calls = []
    mock_fim.map_sloping_profiles = _old_sloping(calls)
    result = _map(tmp_path)
    assert result == {"wse": [tmp_path / "p01" / "WSE (Max).tif"],
                      "depth": [tmp_path / "p01" / "Depth (Max).tif"]}
    assert result.invalid_cells is None
    assert result.product_writer == "rasterio"
    # Strict assembly is what older releases did: "raise" needs no keyword.
    assert _map(tmp_path, invalid_cells="raise").invalid_cells is None


@patch("ras_commander.RasFim")
def test_older_ras_commander_refuses_a_policy_it_cannot_apply(mock_fim, tmp_path):
    mock_fim.map_sloping_profiles = _old_sloping([])
    with pytest.raises(fast_map.InvalidCellsUnsupported, match="0.110.2") as info:
        _map(tmp_path, invalid_cells="omit")
    # Not a terrain refusal: with_terrain must not move on to a terrain export.
    assert not isinstance(info.value, fast_map.FastMapUnavailable)


def test_unsupported_policy_does_not_try_the_next_terrain(tmp_path):
    resolver = fast_map.TerrainResolver(tmp_path / "m.prj", tmp_path / "w",
                                        terrain_path=tmp_path / "dem.tif")
    (tmp_path / "dem.tif").write_bytes(b"x")
    seen = []

    def run(tif, hdf, kind):
        seen.append(kind)
        raise fast_map.InvalidCellsUnsupported("needs ras-commander>=0.110.2")

    with pytest.raises(fast_map.InvalidCellsUnsupported):
        fast_map.with_terrain(resolver, tmp_path / "a.p01.hdf", run)
    assert seen == [fast_map.TERRAIN_SUPPLIED]


@patch("ras_commander.RasFim")
def test_horizontal_passes_the_same_policy_to_cells_and_mapping(mock_fim, tmp_path):
    seen = {}

    def get_cells_from_plan_hdf(hdf_path, *, mesh_name=None, profile_name="wsel_max",
                                min_depth=0.0, invalid_cells="repair"):
        seen["cells"] = invalid_cells
        return MagicMock()

    def map_horizontal_profiles(cells, terrain_path, output_folder, *, profiles=None,
                                hdf_path=None, terrain_hdf=None, max_working_mib=1024,
                                threads=1, writer_threads=1, timeout=None,
                                write_floodplain=False, invalid_cells="repair"):
        seen["map"] = invalid_cells
        return _manifest(Path(output_folder))

    mock_fim.get_cells_from_plan_hdf = get_cells_from_plan_hdf
    mock_fim.map_horizontal_profiles = map_horizontal_profiles
    result = fast_map.map_plan(
        tmp_path / "x.p01.hdf", tmp_path / "t.tif", tmp_path / "p01",
        map_types=["depth"], render={"mode": "horizontal"}, invalid_cells="omit",
    )
    assert seen == {"cells": "omit", "map": "omit"}
    assert result.invalid_cells == RECORD
    assert result.product_writer is None  # horizontal has one delivery path


@patch("ras_commander.RasFim")
def test_whole_simulation_passes_and_records_the_policy(mock_fim, tmp_path, monkeypatch):
    monkeypatch.setattr(fast_map, "whole_simulation_supported", lambda: True)
    seen = {}

    def map_whole_simulation(hdf_path, terrain_path, output_folder, *, products=(),
                             threshold=0.0, terrain_hdf=None, threads=1, writer_threads=1,
                             max_working_mib=1024, timeout=None, invalid_cells="repair"):
        seen["policy"] = invalid_cells
        folder = Path(output_folder)
        rows = []
        for name in products:
            tif = folder / f"{name}.tif"
            tif.write_bytes(b"x")
            rows.append({"product": name, "status": "complete", "tif": str(tif),
                         "units": "hours", "output_times": 4, "wet_pixels": 1,
                         "qualified": True, "error": None})
        frame = pd.DataFrame(rows)
        frame.attrs["invalid_cells"] = {**RECORD, "policy": invalid_cells}
        return frame

    mock_fim.map_whole_simulation = map_whole_simulation
    out = tmp_path / "p01"
    result = fast_map.map_whole_simulation(
        tmp_path / "x.p01.hdf", tmp_path / "t.tif", out, map_types=["arrival_time"],
        threshold=0.5, render=_SLOPING, invalid_cells="omit",
    )
    assert seen["policy"] == "omit"
    assert result.invalid_cells["policy"] == "omit"
    details = json.loads((out / fast_map.FAST_MAP_WHOLE_SIMULATION_MANIFEST).read_text())
    assert details["invalid_cells_policy"] == "omit"
    assert details["invalid_cells"]["count"] == 1


@patch("ras_commander.RasFim")
def test_boundary_passes_the_policy_when_the_api_takes_it(mock_fim, tmp_path, monkeypatch):
    monkeypatch.setattr(fast_map, "boundary_supported", lambda: True)
    mock_fim.validate_2d_model.return_value = {"dimension": "2D-only", "storage_areas": 0}
    seen = {}

    def map_inundation_boundaries(hdf_path, terrain_path, output_folder, *, terrain_hdf=None,
                                  profiles=None, interpolation="sloping", threads=1,
                                  max_working_mib=1024, max_prepared_mib=1024, timeout=None,
                                  invalid_cells="repair"):
        seen["policy"] = invalid_cells
        geojson = Path(output_folder) / "b.geojson"
        geojson.write_text('{"type": "FeatureCollection", "features": []}')
        frame = pd.DataFrame([{"status": "complete", "boundary_geojson": str(geojson),
                               "polygons": 1, "holes": 0, "vertices": 4, "area": 1.0,
                               "crs": "EPSG:2271", "error": None}])
        frame.attrs["invalid_cells"] = RECORD
        return frame

    mock_fim.map_inundation_boundaries = map_inundation_boundaries
    out = tmp_path / "p01"
    result = fast_map.map_boundary(
        tmp_path / "x.p01.hdf", tmp_path / "t.tif", tmp_path / "t.hdf", out,
        render=_SLOPING, invalid_cells="hull",
    )
    assert seen["policy"] == "hull"
    assert result.invalid_cells == RECORD
    details = json.loads((out / fast_map.FAST_MAP_BOUNDARY_MANIFEST).read_text())
    assert details["invalid_cells"]["count"] == 1
    assert details["ras2cng"]["invalid_cells_policy"] == "hull"


# ---------------------------------------------------------------------------
# Native GeoTIFF delivery
# ---------------------------------------------------------------------------

def test_resolve_product_writer(monkeypatch):
    assert fast_map.resolve_product_writer() == "auto"
    assert fast_map.resolve_product_writer(" Native ") == "native"
    monkeypatch.setenv(fast_map.PRODUCT_WRITER_ENV, "rasterio")
    assert fast_map.resolve_product_writer() == "rasterio"
    with pytest.raises(ValueError, match="product writer"):
        fast_map.resolve_product_writer("gdal")


@patch("ras_commander.RasFim")
def test_auto_uses_native_delivery_when_the_engine_has_it(mock_fim, tmp_path, monkeypatch):
    monkeypatch.setattr(fast_map, "native_writer_supported", lambda: True)
    calls = []
    mock_fim.map_sloping_profiles = _new_sloping(calls)
    result = _map(tmp_path)
    assert calls[0]["product_writer"] == "native"
    assert result.product_writer == "native"
    details = json.loads((tmp_path / "p01" / fast_map.FAST_MAP_MANIFEST).read_text())
    assert details["ras2cng"]["product_writer"] == "native"


@patch("ras_commander.RasFim")
def test_env_can_keep_rasterio_delivery(mock_fim, tmp_path, monkeypatch):
    monkeypatch.setattr(fast_map, "native_writer_supported", lambda: True)
    monkeypatch.setenv(fast_map.PRODUCT_WRITER_ENV, "rasterio")
    calls = []
    mock_fim.map_sloping_profiles = _new_sloping(calls)
    assert _map(tmp_path).product_writer == "rasterio"
    assert calls[0]["product_writer"] == "rasterio"


@patch("ras_commander.RasFim")
def test_auto_without_engine_support_uses_rasterio(mock_fim, tmp_path, monkeypatch):
    monkeypatch.setattr(fast_map, "native_writer_supported", lambda: False)
    calls = []
    mock_fim.map_sloping_profiles = _new_sloping(calls)
    assert _map(tmp_path).product_writer == "rasterio"
    assert calls[0]["product_writer"] == "rasterio"


@patch("ras_commander.RasFim")
def test_required_native_without_support_raises(mock_fim, tmp_path, monkeypatch):
    monkeypatch.setattr(fast_map, "native_writer_supported", lambda: False)
    calls = []
    mock_fim.map_sloping_profiles = _new_sloping(calls)
    with pytest.raises(RuntimeError, match="native_geotiff_outputs") as info:
        _map(tmp_path, product_writer="native")
    assert not isinstance(info.value, fast_map.FastMapUnavailable)
    assert calls == []


@patch("ras_commander.RasFim")
def test_velocity_requests_use_rasterio_delivery(mock_fim, tmp_path, monkeypatch):
    """ras-commander refuses native delivery with velocity."""
    monkeypatch.setattr(fast_map, "native_writer_supported", lambda: True)
    monkeypatch.setattr(fast_map, "velocity_supported", lambda: True)
    calls = []

    def sloping(hdf_path, terrain_path, output_folder, **kw):
        frame = _new_sloping(calls)(hdf_path, terrain_path, output_folder, **kw)
        speed = Path(output_folder) / "12_Max_velocity.tif"
        speed.write_bytes(b"v")
        frame["velocity_tif"] = str(speed)
        return frame

    import inspect
    sloping.__signature__ = inspect.signature(_new_sloping([]))
    mock_fim.map_sloping_profiles = sloping
    result = fast_map.map_plan(
        tmp_path / "x.p01.hdf", tmp_path / "t.tif", tmp_path / "p01",
        map_types=["depth", "velocity"], render=_SLOPING, velocity=True,
    )
    assert calls[0] == {"product_writer": "rasterio", "invalid_cells": "repair", "velocity": True}
    assert result.product_writer == "rasterio"
    with pytest.raises(RuntimeError, match="excludes velocity"):
        fast_map.map_plan(
            tmp_path / "x.p01.hdf", tmp_path / "t.tif", tmp_path / "p02",
            map_types=["depth", "velocity"], render=_SLOPING, velocity=True,
            product_writer="native",
        )


@patch("ras_commander.RasFim")
def test_older_ras_commander_gets_no_writer_keyword(mock_fim, tmp_path, monkeypatch):
    calls = []
    mock_fim.map_sloping_profiles = _old_sloping(calls)
    assert _map(tmp_path, product_writer="auto").product_writer == "rasterio"


def test_native_writer_supported_reads_the_engine_capability(monkeypatch):
    from ras_commander import RasFim
    from ras_commander.fim import _engine

    if not fast_map._has_kwargs(RasFim.map_sloping_profiles, "product_writer"):
        pytest.skip("installed ras-commander predates product_writer (0.110.3)")
    monkeypatch.setattr(RasFim, "find_engine", staticmethod(lambda *a, **k: Path("engine")))
    monkeypatch.setattr(_engine, "engine_method",
                        lambda engine: {"sloping": {"native_geotiff_outputs": True}})
    assert fast_map.native_writer_supported() is True
    monkeypatch.setattr(_engine, "engine_method", lambda engine: {"sloping": {}})
    assert fast_map.native_writer_supported() is False

    def missing(*a, **k):
        raise FileNotFoundError("no engine")

    monkeypatch.setattr(RasFim, "find_engine", staticmethod(missing))
    assert fast_map.native_writer_supported() is False


# ---------------------------------------------------------------------------
# generate_result_maps -> MapResult
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


@patch("ras2cng.mapping._generate_plan_maps")
@patch("ras2cng.mapping._configure_rasprocess")
@patch("ras2cng.mapping.init_ras_project")
@patch("ras2cng.fast_map.render_settings", return_value=_SLOPING)
@patch("ras2cng.fast_map.map_whole_simulation")
@patch("ras2cng.fast_map.map_plan")
def test_map_result_records_invalid_cells_and_writer(
    mock_map_plan, mock_whole, _render, mock_init, _config, mock_gen, tmp_path,
):
    ras, project_dir = _fake_ras(tmp_path)
    mock_init.return_value = ras
    terrain = tmp_path / "dem.tif"
    terrain.write_bytes(b"x")
    out = tmp_path / "maps" / "p01"
    rasters = fast_map.FastMapProducts({"depth": [out / "Depth (Max).tif"]})
    rasters.invalid_cells = RECORD
    rasters.product_writer = "native"
    mock_map_plan.return_value = rasters
    whole = fast_map.FastMapProducts({"arrival_time": [out / "Arrival Time (0.5ft hrs).tif"]})
    whole.invalid_cells = RECORD
    mock_whole.return_value = whole
    mock_gen.return_value = {"velocity": [out / "Velocity (Max).tif"]}

    results = generate_result_maps(
        project_dir, tmp_path / "maps", wse=False, depth=True, velocity=True,
        arrival_time=True, arrival_depth=0.5, map_engine="fast-map",
        fast_map_terrain=terrain, fast_map_invalid_cells="Repair", fast_map_writer="auto",
    )
    result = results[0]
    assert result.invalid_cells == {"depth": RECORD, "arrival_time": RECORD}
    assert result.fast_map_writer == {"depth": "native"}
    assert "velocity" not in result.invalid_cells  # RAS Mapper produced it
    assert mock_map_plan.call_args.kwargs["invalid_cells"] == "repair"
    assert mock_map_plan.call_args.kwargs["product_writer"] == "auto"
    assert mock_whole.call_args.kwargs["invalid_cells"] == "repair"


def test_generate_result_maps_rejects_an_unknown_policy(tmp_path):
    with pytest.raises(ValueError, match="invalid_cells"):
        generate_result_maps(tmp_path, tmp_path / "maps", fast_map_invalid_cells="drop")
