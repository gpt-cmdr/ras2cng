"""Tests for the optional Rust fast-map engine route in ras2cng map.

RasFim and RAS Mapper are mocked; no HEC-RAS model or engine run is needed.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from ras_commander import RasMap as _RasMap

from ras2cng import fast_map
from ras2cng.mapping import generate_result_maps


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


def _fake_manifest(folder: Path, status: str = "complete") -> pd.DataFrame:
    depth = folder / "09_Max_depth.tif"
    wse = folder / "10_Max_wsel.tif"
    depth.write_bytes(b"depth")
    wse.write_bytes(b"wse")
    (folder / "fim_manifest.json").write_text(json.dumps({"status": "complete"}))
    return pd.DataFrame([{
        "profile": "Max", "status": status,
        "depth_tif": str(depth), "wse_tif": str(wse), "error": None,
    }])


# ---------------------------------------------------------------------------
# Engine selection
# ---------------------------------------------------------------------------

def test_resolve_map_engine_defaults_to_rasmapper(monkeypatch):
    monkeypatch.delenv(fast_map.MAP_ENGINE_ENV, raising=False)
    assert fast_map.resolve_map_engine() == "rasmapper"


@pytest.mark.parametrize("value", ["fast-map", "fast_map", "FastMap", "rust"])
def test_resolve_map_engine_aliases(value):
    assert fast_map.resolve_map_engine(value) == "fast-map"


def test_resolve_map_engine_reads_env(monkeypatch):
    monkeypatch.setenv(fast_map.MAP_ENGINE_ENV, "fast-map")
    assert fast_map.resolve_map_engine() == "fast-map"


def test_resolve_map_engine_rejects_unknown():
    with pytest.raises(ValueError, match="map engine"):
        fast_map.resolve_map_engine("gdal")


# ---------------------------------------------------------------------------
# map_plan
# ---------------------------------------------------------------------------

def test_map_plan_rejects_non_max_profile(tmp_path):
    with pytest.raises(fast_map.FastMapUnavailable, match="Max profile"):
        fast_map.map_plan(
            tmp_path / "x.p01.hdf", tmp_path / "t.tif", tmp_path / "out",
            map_types=["depth"], profile="01JAN2020 12:00:00",
            render={"mode": "horizontal"},
        )


def test_map_plan_ignores_non_fast_types(tmp_path):
    assert fast_map.map_plan(
        tmp_path / "x.p01.hdf", tmp_path / "t.tif", tmp_path / "out",
        map_types=["velocity"], render={"mode": "horizontal"},
    ) == {}


@patch("ras_commander.RasFim")
def test_map_plan_sloping_names_products_like_rasmapper(mock_fim, tmp_path):
    mock_fim.map_sloping_profiles.side_effect = (
        lambda hdf, terrain, folder, **kw: _fake_manifest(Path(folder))
    )
    out = tmp_path / "p01"
    render = {"mode": "sloping", "reduce_shallow_to_horizontal": False,
              "use_depth_weighted_faces": False}

    result = fast_map.map_plan(
        tmp_path / "x.p01.hdf", tmp_path / "t.tif", out,
        map_types=["wse", "depth", "velocity"], render=render, threads=4,
    )

    assert result == {"wse": [out / "WSE (Max).tif"], "depth": [out / "Depth (Max).tif"]}
    assert (out / "WSE (Max).tif").read_bytes() == b"wse"
    assert (out / fast_map.FAST_MAP_MANIFEST).is_file()
    assert not list(out.glob(".fast-map-*")), "staging folder must be removed"
    kwargs = mock_fim.map_sloping_profiles.call_args.kwargs
    assert kwargs["render_mode"] == "sloping"
    assert kwargs["threads"] == 4
    assert kwargs["profiles"] == {"Max": "Max"}
    mock_fim.validate_2d_model.assert_called_once()


@patch("ras_commander.RasFim")
def test_map_plan_horizontal_uses_cell_maxima(mock_fim, tmp_path):
    mock_fim.get_cells_from_plan_hdf.return_value = MagicMock()
    mock_fim.map_horizontal_profiles.side_effect = (
        lambda cells, terrain, folder, **kw: _fake_manifest(Path(folder))
    )
    result = fast_map.map_plan(
        tmp_path / "x.p01.hdf", tmp_path / "t.tif", tmp_path / "out",
        map_types=["depth"], render={"mode": "horizontal"},
    )
    assert list(result) == ["depth"]
    assert mock_fim.get_cells_from_plan_hdf.call_args.kwargs["mesh_name"] == "*"
    assert not (tmp_path / "out" / "WSE (Max).tif").exists()


@patch("ras_commander.RasFim")
def test_map_plan_1d_model_is_unavailable(mock_fim, tmp_path):
    from ras_commander.fim.errors import FimUnsupportedError

    mock_fim.validate_2d_model.side_effect = FimUnsupportedError("FIM mapping is 2D-only")
    with pytest.raises(fast_map.FastMapUnavailable, match="2D-only"):
        fast_map.map_plan(
            tmp_path / "x.p01.hdf", tmp_path / "t.tif", tmp_path / "out",
            map_types=["depth"], render={"mode": "sloping"},
        )


@patch("ras_commander.RasFim")
def test_map_plan_storage_areas_are_unavailable(mock_fim, tmp_path):
    mock_fim.validate_2d_model.return_value = {"dimension": "2D-only", "storage_areas": 1}
    with pytest.raises(fast_map.FastMapUnavailable, match="storage area"):
        fast_map.map_plan(
            tmp_path / "x.p01.hdf", tmp_path / "t.tif", tmp_path / "out",
            map_types=["depth"], render={"mode": "sloping"},
        )
    mock_fim.map_sloping_profiles.assert_not_called()


@patch("ras_commander.RasFim")
def test_map_plan_failed_profile_raises(mock_fim, tmp_path):
    mock_fim.map_sloping_profiles.side_effect = (
        lambda hdf, terrain, folder, **kw: _fake_manifest(Path(folder), status="failed")
    )
    with pytest.raises(RuntimeError, match="failed"):
        fast_map.map_plan(
            tmp_path / "x.p01.hdf", tmp_path / "t.tif", tmp_path / "out",
            map_types=["depth"], render={"mode": "sloping",
                                         "reduce_shallow_to_horizontal": False,
                                         "use_depth_weighted_faces": False},
        )


def test_render_settings_flags_only_for_sloping_pretty():
    stored = {"mode": "slopingPretty", "reduce_shallow_to_horizontal": True,
              "use_depth_weighted_faces": True}
    with patch.object(_RasMap, "get_water_surface_render_mode", return_value=stored):
        assert fast_map.render_settings(MagicMock(), None) == stored
        plain = fast_map.render_settings(MagicMock(), "sloping")
    assert plain == {"mode": "sloping", "reduce_shallow_to_horizontal": False,
                     "use_depth_weighted_faces": False}


def test_render_settings_default_horizontal():
    with patch.object(_RasMap, "get_water_surface_render_mode", return_value=None):
        assert fast_map.render_settings(MagicMock(), None)["mode"] == "horizontal"


def test_terrain_resolver_prefers_supplied_terrain(tmp_path):
    terrain = tmp_path / "dem.tif"
    terrain.write_bytes(b"x")
    resolver = fast_map.TerrainResolver(tmp_path / "a.prj", tmp_path / "w", terrain_path=terrain)
    assert resolver.for_plan(tmp_path / "a.p01.hdf") == terrain


def test_terrain_resolver_exports_once_per_terrain(tmp_path):
    resolver = fast_map.TerrainResolver(tmp_path / "a.prj", tmp_path / "w", terrain_name="Terrain")
    with patch("ras2cng.terrain.export_modified_terrain",
               side_effect=lambda prj, target, **kw: target) as export:
        first = resolver.for_plan(tmp_path / "a.p01.hdf")
        second = resolver.for_plan(tmp_path / "a.p02.hdf")
    assert first == second == tmp_path / "w" / "Terrain.tif"
    export.assert_called_once()


# ---------------------------------------------------------------------------
# generate_result_maps routing
# ---------------------------------------------------------------------------

@patch("ras2cng.mapping._generate_plan_maps")
@patch("ras2cng.mapping._configure_rasprocess")
@patch("ras2cng.mapping.init_ras_project")
@patch("ras2cng.fast_map.render_settings", return_value={"mode": "sloping"})
@patch("ras2cng.fast_map.map_plan")
def test_fast_map_routes_wse_depth_and_keeps_velocity_on_rasmapper(
    mock_map_plan, _render, mock_init, _config, mock_gen, tmp_path,
):
    ras, project_dir = _fake_ras(tmp_path)
    mock_init.return_value = ras
    terrain = tmp_path / "dem.tif"
    terrain.write_bytes(b"x")
    out = tmp_path / "maps"
    wse, depth, vel = (out / "p01" / n for n in ("WSE (Max).tif", "Depth (Max).tif", "Velocity (Max).tif"))
    mock_map_plan.return_value = {"wse": [wse], "depth": [depth]}
    mock_gen.return_value = {"velocity": [vel]}

    results = generate_result_maps(
        project_dir, out, map_engine="fast-map", fast_map_terrain=terrain,
    )

    assert results[0].engines == {"wse": "fast-map", "depth": "fast-map", "velocity": "rasmapper"}
    assert results[0].map_types["wse"] == [wse]
    flags = mock_gen.call_args.kwargs
    assert flags["velocity"] is True
    assert flags["wse"] is False and flags["depth"] is False
    assert mock_map_plan.call_args.args[1] == terrain


@patch("ras2cng.mapping._generate_plan_maps")
@patch("ras2cng.mapping._configure_rasprocess")
@patch("ras2cng.mapping.init_ras_project")
@patch("ras2cng.fast_map.render_settings", return_value={"mode": "sloping"})
@patch("ras2cng.fast_map.map_plan")
def test_fast_map_skips_rasmapper_when_everything_mapped(
    mock_map_plan, _render, mock_init, _config, mock_gen, tmp_path,
):
    ras, project_dir = _fake_ras(tmp_path)
    mock_init.return_value = ras
    terrain = tmp_path / "dem.tif"
    terrain.write_bytes(b"x")
    mock_map_plan.return_value = {"depth": [tmp_path / "Depth (Max).tif"]}

    generate_result_maps(
        project_dir, tmp_path / "maps", wse=False, depth=True, velocity=False,
        map_engine="fast-map", fast_map_terrain=terrain,
    )
    mock_gen.assert_not_called()


@patch("ras2cng.mapping._generate_plan_maps")
@patch("ras2cng.mapping._configure_rasprocess")
@patch("ras2cng.mapping.init_ras_project")
@patch("ras2cng.fast_map.render_settings", return_value={"mode": "sloping"})
@patch("ras2cng.fast_map.map_plan",
       side_effect=fast_map.FastMapUnavailable("FIM mapping is 2D-only"))
def test_fast_map_falls_back_to_rasmapper(
    _map_plan, _render, mock_init, _config, mock_gen, tmp_path,
):
    ras, project_dir = _fake_ras(tmp_path)
    mock_init.return_value = ras
    terrain = tmp_path / "dem.tif"
    terrain.write_bytes(b"x")
    depth = tmp_path / "Depth (Max).tif"
    mock_gen.return_value = {"depth": [depth]}

    results = generate_result_maps(
        project_dir, tmp_path / "maps", wse=False, depth=True, velocity=False,
        map_engine="fast-map", fast_map_terrain=terrain,
    )
    assert mock_gen.call_args.kwargs["depth"] is True
    assert results[0].engines == {"depth": "rasmapper"}
    assert results[0].errors == []


@patch("ras2cng.mapping._generate_plan_maps")
@patch("ras2cng.mapping._configure_rasprocess")
@patch("ras2cng.mapping.init_ras_project")
@patch("ras2cng.fast_map.render_settings", return_value={"mode": "sloping"})
@patch("ras2cng.fast_map.map_plan",
       side_effect=fast_map.FastMapUnavailable("FIM mapping is 2D-only"))
def test_fast_map_strict_raises(_map_plan, _render, mock_init, _config, mock_gen, tmp_path):
    ras, project_dir = _fake_ras(tmp_path)
    mock_init.return_value = ras
    terrain = tmp_path / "dem.tif"
    terrain.write_bytes(b"x")
    with pytest.raises(fast_map.FastMapUnavailable):
        generate_result_maps(
            project_dir, tmp_path / "maps", wse=False, depth=True, velocity=False,
            map_engine="fast-map", fast_map_terrain=terrain,
            fast_map_fallback=False, skip_errors=False,
        )
    mock_gen.assert_not_called()


@patch("ras2cng.mapping._generate_plan_maps", return_value={})
@patch("ras2cng.mapping._configure_rasprocess")
@patch("ras2cng.mapping.init_ras_project")
@patch("ras2cng.fast_map.map_plan")
def test_default_engine_never_calls_fast_map(mock_map_plan, mock_init, _config, _gen, tmp_path, monkeypatch):
    monkeypatch.delenv(fast_map.MAP_ENGINE_ENV, raising=False)
    ras, project_dir = _fake_ras(tmp_path)
    mock_init.return_value = ras
    generate_result_maps(project_dir, tmp_path / "maps")
    mock_map_plan.assert_not_called()


# ---------------------------------------------------------------------------
# Rust inundation boundaries
# ---------------------------------------------------------------------------

def _fake_boundary_manifest(folder: Path, status: str = "complete") -> pd.DataFrame:
    geojson = folder / "Max_boundary.geojson"
    if status == "complete":
        geojson.write_text('{"type": "FeatureCollection", "features": []}')
    (folder / "boundary_manifest.json").write_text(json.dumps({"method": "deterministic-zero-depth-contours/1"}))
    return pd.DataFrame([{
        "profile": "Max", "selector": "Max", "status": status,
        "boundary_geojson": str(geojson) if status == "complete" else None,
        "interpolation": "sloping", "polygons": 3, "holes": 1, "vertices": 120,
        "area": 1.5e6, "crs": "EPSG:2271", "error": None,
    }])


def _boundary_fim(mock_fim, status="complete"):
    mock_fim.map_inundation_boundaries.side_effect = (
        lambda hdf, tif, folder, **kw: _fake_boundary_manifest(Path(folder), status)
    )
    mock_fim.validate_2d_model.return_value = {"dimension": "2D-only", "storage_areas": 0}


@patch("ras_commander.RasFim")
def test_map_boundary_writes_named_geojson_and_manifest(mock_fim, tmp_path):
    _boundary_fim(mock_fim)
    out = tmp_path / "p01"
    result = fast_map.map_boundary(
        tmp_path / "x.p01.hdf", tmp_path / "t.tif", tmp_path / "t.hdf", out,
        render={"mode": "sloping"}, threads=4,
    )
    target = out / "Inundation Boundary (Max).geojson"
    assert result == {"inundation_boundary": [target]}
    assert target.is_file()
    manifest = json.loads((out / fast_map.FAST_MAP_BOUNDARY_MANIFEST).read_text())
    assert manifest["ras2cng"]["statistics"]["holes"] == 1
    assert "not RAS Mapper vector parity" in manifest["ras2cng"]["method"]
    kwargs = mock_fim.map_inundation_boundaries.call_args.kwargs
    assert kwargs["interpolation"] == "sloping"
    assert kwargs["terrain_hdf"] == tmp_path / "t.hdf"
    assert kwargs["profiles"] == {"Max": "Max"}
    assert not list(out.glob(".fast-map-boundary-*"))


@patch("ras_commander.RasFim")
def test_map_boundary_empty_returns_no_products(mock_fim, tmp_path):
    _boundary_fim(mock_fim, status="empty")
    result = fast_map.map_boundary(
        tmp_path / "x.p01.hdf", tmp_path / "t.tif", tmp_path / "t.hdf", tmp_path / "out",
        render={"mode": "horizontal"},
    )
    assert result == {"inundation_boundary": []}


def test_map_boundary_rejects_sloping_pretty(tmp_path):
    with pytest.raises(fast_map.FastMapUnavailable, match="plain sloping"):
        fast_map.map_boundary(
            tmp_path / "x.p01.hdf", tmp_path / "t.tif", tmp_path / "t.hdf", tmp_path / "out",
            render={"mode": "slopingPretty"},
        )


@patch("ras_commander.RasFim")
def test_map_boundary_requires_boundary_api(mock_fim, tmp_path):
    del mock_fim.map_inundation_boundaries
    with pytest.raises(fast_map.FastMapUnavailable, match="map_inundation_boundaries"):
        fast_map.map_boundary(
            tmp_path / "x.p01.hdf", tmp_path / "t.tif", tmp_path / "t.hdf", tmp_path / "out",
            render={"mode": "sloping"},
        )


def test_boundary_terrain_requires_single_source(tmp_path):
    terrain = tmp_path / "Terrain"
    terrain.mkdir()
    hdf = terrain / "Terrain50.hdf"
    hdf.touch()
    (terrain / "Terrain50.a.tif").touch()
    resolver = fast_map.TerrainResolver(tmp_path / "m.prj", tmp_path / "w", terrain_hdf=hdf)
    assert resolver.boundary_terrain(tmp_path / "m.p01.hdf") == (terrain / "Terrain50.a.tif", hdf)
    (terrain / "Terrain50.b.tif").touch()
    with pytest.raises(fast_map.FastMapUnavailable, match="single-source"):
        resolver.boundary_terrain(tmp_path / "m.p01.hdf")


@patch("ras2cng.mapping._generate_plan_maps")
@patch("ras2cng.mapping._configure_rasprocess")
@patch("ras2cng.mapping.init_ras_project")
@patch("ras2cng.fast_map.render_settings", return_value={"mode": "sloping"})
@patch("ras2cng.fast_map.map_boundary")
@patch("ras2cng.fast_map.map_plan")
def test_fast_map_boundary_routed_separately_from_rasters(
    mock_map_plan, mock_boundary, _render, mock_init, _config, mock_gen, tmp_path,
):
    ras, project_dir = _fake_ras(tmp_path)
    mock_init.return_value = ras
    terrain_hdf = tmp_path / "Terrain50.hdf"
    terrain_hdf.touch()
    (tmp_path / "Terrain50.tif").touch()
    mock_map_plan.side_effect = fast_map.FastMapUnavailable("no terrain export")
    boundary = tmp_path / "Inundation Boundary (Max).geojson"
    mock_boundary.return_value = {"inundation_boundary": [boundary]}
    mock_gen.return_value = {"depth": [tmp_path / "Depth (Max).tif"]}

    results = generate_result_maps(
        project_dir, tmp_path / "maps", wse=False, depth=True, velocity=False,
        inundation_boundary=True, map_engine="fast-map",
        fast_map_terrain_hdf=terrain_hdf,
    )

    assert results[0].engines == {"depth": "rasmapper", "inundation_boundary": "fast-map"}
    assert results[0].map_types["inundation_boundary"] == [boundary]
    flags = mock_gen.call_args.kwargs
    assert flags["depth"] is True and flags["inundation_boundary"] is False


@patch("ras2cng.mapping._generate_plan_maps", return_value={})
@patch("ras2cng.mapping._configure_rasprocess")
@patch("ras2cng.mapping.init_ras_project")
@patch("ras2cng.fast_map.render_settings", return_value={"mode": "sloping"})
@patch("ras2cng.fast_map.map_boundary")
def test_no_fast_map_boundary_keeps_rasmapper(
    mock_boundary, _render, mock_init, _config, mock_gen, tmp_path,
):
    ras, project_dir = _fake_ras(tmp_path)
    mock_init.return_value = ras
    generate_result_maps(
        project_dir, tmp_path / "maps", wse=False, depth=False, velocity=False,
        inundation_boundary=True, map_engine="fast-map", fast_map_boundary=False,
    )
    mock_boundary.assert_not_called()
    assert mock_gen.call_args.kwargs["inundation_boundary"] is True
