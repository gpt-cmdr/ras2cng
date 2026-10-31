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


@pytest.fixture(autouse=True)
def _preflight_uses_patched_render(monkeypatch):
    """Routing tests mock RasFim's inputs; keep the preflight to render_settings.

    The real preflight is covered by the tests that undo this.
    """
    monkeypatch.setattr(
        fast_map, "preflight", lambda plan, ras, mode, **kw: fast_map.render_settings(ras, mode)
    )


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

def test_resolve_map_engine_defaults_to_fast_map(monkeypatch):
    monkeypatch.delenv(fast_map.MAP_ENGINE_ENV, raising=False)
    assert fast_map.DEFAULT_MAP_ENGINE == "fast-map"
    assert fast_map.resolve_map_engine() == "fast-map"
    assert fast_map.resolve_map_engine("rasmapper") == "rasmapper"
    monkeypatch.setenv(fast_map.MAP_ENGINE_ENV, "rasmapper")
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


def test_render_settings_without_a_stored_mode_leaves_it_to_rasmapper():
    """No hard-coded default: RAS Mapper applies its own."""
    with patch.object(_RasMap, "get_water_surface_render_mode", return_value=None):
        with pytest.raises(fast_map.FastMapUnavailable, match="render mode"):
            fast_map.render_settings(MagicMock(), None)
        # An explicit mode still works without a stored one.
        assert fast_map.render_settings(MagicMock(), "sloping")["mode"] == "sloping"


@pytest.mark.parametrize("stored", ["horizontal", "sloping"])
def test_render_settings_reads_the_rasmap_mode(stored):
    with patch.object(_RasMap, "get_water_surface_render_mode",
                      return_value={"mode": stored}):
        assert fast_map.render_settings(MagicMock(), None)["mode"] == stored


@pytest.mark.parametrize(
    ("xml", "expected"),
    [
        ("<RenderMode>horizontal</RenderMode>", "horizontal"),
        ("<RenderMode>sloping</RenderMode>", "sloping"),
        ("", None),
    ],
)
def test_render_settings_from_a_real_rasmap_file(tmp_path, xml, expected):
    project = tmp_path / "Model.prj"
    project.write_text("Proj Title=Model\n")
    (tmp_path / "Model.rasmap").write_text(
        "<RASMapper><Version>2.0.0</Version><RenderMode>X</RenderMode></RASMapper>".replace(
            "<RenderMode>X</RenderMode>", xml
        )
    )
    ras = MagicMock()
    ras.project_folder = str(tmp_path)
    ras.project_name = "Model"
    ras.prj_file = str(project)
    ras.rasmap_path = tmp_path / "Model.rasmap"
    if expected is None:
        with pytest.raises(fast_map.FastMapUnavailable):
            fast_map.render_settings(ras, None)
    else:
        assert fast_map.render_settings(ras, None)["mode"] == expected


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
def test_forced_rasmapper_never_calls_fast_map(mock_map_plan, mock_init, _config, _gen, tmp_path, monkeypatch):
    monkeypatch.delenv(fast_map.MAP_ENGINE_ENV, raising=False)
    ras, project_dir = _fake_ras(tmp_path)
    mock_init.return_value = ras
    results = generate_result_maps(project_dir, tmp_path / "maps", map_engine="rasmapper")
    mock_map_plan.assert_not_called()
    monkeypatch.setenv(fast_map.MAP_ENGINE_ENV, "rasmapper")
    generate_result_maps(project_dir, tmp_path / "maps2")
    mock_map_plan.assert_not_called()
    assert all(engine == "rasmapper" for engine in results[0].engines.values())


@patch("ras2cng.mapping._generate_plan_maps")
@patch("ras2cng.mapping._configure_rasprocess")
@patch("ras2cng.mapping.init_ras_project")
@patch("ras2cng.fast_map.render_settings", return_value={"mode": "horizontal"})
@patch("ras2cng.fast_map.map_plan")
def test_default_engine_is_fast_map(mock_map_plan, _render, mock_init, _config, mock_gen, tmp_path, monkeypatch):
    monkeypatch.delenv(fast_map.MAP_ENGINE_ENV, raising=False)
    ras, project_dir = _fake_ras(tmp_path)
    mock_init.return_value = ras
    terrain = tmp_path / "dem.tif"
    terrain.write_bytes(b"x")
    out = tmp_path / "maps" / "p01"
    mock_map_plan.return_value = {"wse": [out / "WSE (Max).tif"], "depth": [out / "Depth (Max).tif"]}
    mock_gen.return_value = {"velocity": [out / "Velocity (Max).tif"]}
    results = generate_result_maps(project_dir, tmp_path / "maps", fast_map_terrain=terrain)
    assert results[0].engines == {"wse": "fast-map", "depth": "fast-map", "velocity": "rasmapper"}
    assert mock_map_plan.call_args.kwargs["render"] == {"mode": "horizontal"}


@patch("ras2cng.mapping._generate_plan_maps")
@patch("ras2cng.mapping._configure_rasprocess")
@patch("ras2cng.mapping.init_ras_project")
@patch("ras2cng.fast_map.map_plan")
def test_out_of_scope_plan_goes_to_rasmapper_before_any_terrain_work(
    mock_map_plan, mock_init, _config, mock_gen, tmp_path, monkeypatch,
):
    """A storage-area (or 1D, or no-render-mode) plan never exports terrain."""
    monkeypatch.delenv(fast_map.MAP_ENGINE_ENV, raising=False)

    def refuse(plan, ras, mode, **kw):
        raise fast_map.FastMapUnavailable("model has 1 storage area(s)")

    monkeypatch.setattr(fast_map, "preflight", refuse)
    ras, project_dir = _fake_ras(tmp_path)
    mock_init.return_value = ras
    mock_gen.return_value = {}
    with patch("ras2cng.terrain.export_modified_terrain") as export:
        results = generate_result_maps(project_dir, tmp_path / "maps")
    export.assert_not_called()
    mock_map_plan.assert_not_called()
    flags = mock_gen.call_args.kwargs
    assert flags["wse"] is True and flags["depth"] is True
    assert results[0].engines == {"wse": "rasmapper", "depth": "rasmapper", "velocity": "rasmapper"}
    with pytest.raises(fast_map.FastMapUnavailable):
        generate_result_maps(project_dir, tmp_path / "maps3", fast_map_fallback=False, skip_errors=False)


@patch("ras_commander.RasFim")
def test_preflight_checks_scope_then_render_mode(mock_fim, tmp_path, monkeypatch):
    monkeypatch.undo()  # the real preflight
    mock_fim.validate_2d_model.return_value = {"storage_areas": 1}
    with pytest.raises(fast_map.FastMapUnavailable, match="storage area"):
        fast_map.preflight(tmp_path / "x.p01.hdf", MagicMock(), "sloping")
    mock_fim.validate_2d_model.return_value = {"storage_areas": 0}
    with patch.object(_RasMap, "get_water_surface_render_mode", return_value=None):
        with pytest.raises(fast_map.FastMapUnavailable, match="render mode"):
            fast_map.preflight(tmp_path / "x.p01.hdf", MagicMock(), None)
    with patch.object(_RasMap, "get_water_surface_render_mode", return_value={"mode": "sloping"}):
        assert fast_map.preflight(tmp_path / "x.p01.hdf", MagicMock(), None)["mode"] == "sloping"


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


# ---------------------------------------------------------------------------
# User-supplied terrain CRS hash for Rust boundaries (user decision 2026-10-31)
# ---------------------------------------------------------------------------

_DAVIS_HASH = "e446f7bd6bdb4d7f"  # Davis terrain, as ras2fim-batch terrain-info reports it


def test_normalize_crs_fnv1a64():
    assert fast_map.normalize_crs_fnv1a64(None) is None
    assert fast_map.normalize_crs_fnv1a64(" E446F7BD6BDB4D7F ") == _DAVIS_HASH
    assert fast_map.normalize_crs_fnv1a64("0x" + _DAVIS_HASH) == _DAVIS_HASH
    for bad in ("", "e446f7bd", "e446f7bd6bdb4d7g", "EPSG:2871", _DAVIS_HASH + "0"):
        with pytest.raises(ValueError, match="crs_wkt_fnv1a64"):
            fast_map.normalize_crs_fnv1a64(bad)


@patch("ras_commander.RasFim")
def test_map_boundary_passes_the_supplied_crs_hash(mock_fim, tmp_path, monkeypatch):
    _boundary_fim(mock_fim)
    monkeypatch.setattr(fast_map, "_has_kwargs", lambda function, *names: True)
    out = tmp_path / "p02"
    fast_map.map_boundary(
        tmp_path / "x.p02.hdf", tmp_path / "t.tif", tmp_path / "t.hdf", out,
        render={"mode": "sloping"}, terrain_crs_fnv1a64=_DAVIS_HASH.upper(),
    )
    assert mock_fim.map_inundation_boundaries.call_args.kwargs["terrain_crs_fnv1a64"] == _DAVIS_HASH
    manifest = json.loads((out / fast_map.FAST_MAP_BOUNDARY_MANIFEST).read_text())
    assert manifest["ras2cng"]["terrain_crs_fnv1a64"] == _DAVIS_HASH


@patch("ras_commander.RasFim")
def test_map_boundary_without_a_hash_passes_none(mock_fim, tmp_path, monkeypatch):
    _boundary_fim(mock_fim)
    monkeypatch.setattr(fast_map, "_has_kwargs", lambda function, *names: True)
    out = tmp_path / "p01"
    fast_map.map_boundary(
        tmp_path / "x.p01.hdf", tmp_path / "t.tif", tmp_path / "t.hdf", out,
        render={"mode": "sloping"},
    )
    assert "terrain_crs_fnv1a64" not in mock_fim.map_inundation_boundaries.call_args.kwargs
    manifest = json.loads((out / fast_map.FAST_MAP_BOUNDARY_MANIFEST).read_text())
    assert manifest["ras2cng"]["terrain_crs_fnv1a64"] is None


@patch("ras_commander.RasFim")
def test_map_boundary_hash_needs_an_api_that_takes_it(mock_fim, tmp_path, monkeypatch):
    _boundary_fim(mock_fim)
    monkeypatch.setattr(fast_map, "_has_kwargs", lambda function, *names: False)
    with pytest.raises(fast_map.FastMapUnavailable, match="terrain_crs_fnv1a64"):
        fast_map.map_boundary(
            tmp_path / "x.p01.hdf", tmp_path / "t.tif", tmp_path / "t.hdf", tmp_path / "out",
            render={"mode": "sloping"}, terrain_crs_fnv1a64=_DAVIS_HASH,
        )
    mock_fim.map_inundation_boundaries.assert_not_called()


@patch("ras_commander.RasFim")
def test_crs_refusal_tells_the_user_the_engine_hash(mock_fim, tmp_path, monkeypatch):
    """Never guessed: the refusal shows the engine's hash and how to pass it."""
    from ras_commander.fim.errors import FimUnsupportedError

    _boundary_fim(mock_fim)
    mock_fim.map_inundation_boundaries.side_effect = FimUnsupportedError(
        "engine terrain CRS differs from rasterio CRS EPSG:2871"
    )
    seen = []
    monkeypatch.setattr(
        fast_map, "terrain_crs_hint",
        lambda tif: seen.append(tif) or f"the engine reports crs_wkt_fnv1a64={_DAVIS_HASH}; "
        f"rerun with --fast-map-terrain-crs-hash {_DAVIS_HASH}",
    )
    with pytest.raises(fast_map.FastMapUnavailable) as info:
        fast_map.map_boundary(
            tmp_path / "x.p02.hdf", tmp_path / "t.tif", tmp_path / "t.hdf", tmp_path / "out",
            render={"mode": "sloping"},
        )
    message = str(info.value)
    assert "EPSG:2871" in message and f"--fast-map-terrain-crs-hash {_DAVIS_HASH}" in message
    assert seen == [tmp_path / "t.tif"]
    assert "terrain_crs_fnv1a64" not in mock_fim.map_inundation_boundaries.call_args.kwargs


@patch("ras_commander.RasFim")
def test_other_refusals_get_no_crs_hint(mock_fim, tmp_path, monkeypatch):
    from ras_commander.fim.errors import FimUnsupportedError

    _boundary_fim(mock_fim)
    mock_fim.map_inundation_boundaries.side_effect = FimUnsupportedError("terrain has 2 sources")
    monkeypatch.setattr(fast_map, "terrain_crs_hint", lambda tif: pytest.fail("not a CRS refusal"))
    with pytest.raises(fast_map.FastMapUnavailable, match="^terrain has 2 sources$"):
        fast_map.map_boundary(
            tmp_path / "x.p01.hdf", tmp_path / "t.tif", tmp_path / "t.hdf", tmp_path / "out",
            render={"mode": "sloping"},
        )


def test_terrain_crs_hint_reads_the_engine_terrain_info(tmp_path, monkeypatch):
    import subprocess

    from ras_commander import RasFim
    from ras_commander.fim import _engine

    info = {"epsg": None, "crs": {"epsg": None, "wkt": 'PROJCS["unknown",GEOGCS["NAD83"]]'},
            "crs_wkt_fnv1a64": _DAVIS_HASH}
    monkeypatch.setattr(RasFim, "find_engine", staticmethod(lambda *a, **k: tmp_path / "engine"))
    monkeypatch.setattr(
        _engine, "run_engine",
        lambda args, what, timeout=None: subprocess.CompletedProcess(
            args, 0, stdout=json.dumps(info), stderr=""),
    )
    hint = fast_map.terrain_crs_hint(tmp_path / "Davis.tif")
    assert f"crs_wkt_fnv1a64={_DAVIS_HASH}" in hint
    assert f"--fast-map-terrain-crs-hash {_DAVIS_HASH}" in hint
    assert "ras2fim-batch terrain-info" in hint and 'PROJCS["unknown"' in hint
    monkeypatch.setattr(_engine, "run_engine", lambda *a, **k: (_ for _ in ()).throw(OSError("x")))
    assert fast_map.terrain_crs_hint(tmp_path / "Davis.tif") is None


def test_generate_result_maps_validates_and_passes_the_hash(tmp_path):
    with pytest.raises(ValueError, match="crs_wkt_fnv1a64"):
        generate_result_maps(tmp_path, tmp_path / "out", fast_map_terrain_crs_fnv1a64="EPSG:2871")


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


def _terrain_h5(path, modifications=()):
    """A minimal terrain HDF; each name becomes a /Modifications member."""
    import h5py

    with h5py.File(path, "w") as handle:
        handle.create_group("Terrain")
        if modifications:
            group = handle.create_group("Modifications")
            for name in modifications:
                group.create_group(name)
    return path


def test_boundary_terrain_requires_single_source(tmp_path):
    terrain = tmp_path / "Terrain"
    terrain.mkdir()
    hdf = _terrain_h5(terrain / "Terrain50.hdf")
    (terrain / "Terrain50.a.tif").touch()
    resolver = fast_map.TerrainResolver(tmp_path / "m.prj", tmp_path / "w", terrain_hdf=hdf)
    assert resolver.boundary_terrain(tmp_path / "m.p01.hdf") == (terrain / "Terrain50.a.tif", hdf)
    (terrain / "Terrain50.b.tif").touch()
    with pytest.raises(fast_map.FastMapUnavailable, match="single-source"):
        resolver.boundary_terrain(tmp_path / "m.p01.hdf")


def test_terrain_modifications_reads_hdf_and_rasmap(tmp_path):
    from ras2cng.terrain import terrain_modifications

    plain = _terrain_h5(tmp_path / "plain.hdf")
    assert terrain_modifications(plain) == []
    empty = tmp_path / "empty.hdf"
    import h5py

    with h5py.File(empty, "w") as handle:
        handle.create_group("Modifications")
    assert terrain_modifications(empty) == []
    modified = _terrain_h5(tmp_path / "mod.hdf", ["Levee A", "Channel B"])
    assert sorted(terrain_modifications(modified)) == ["hdf:Channel B", "hdf:Levee A"]
    # A modification registered only in the .rasmap also counts.
    rasmap = tmp_path / "m.rasmap"
    rasmap.write_text(
        '<RASMapper><Terrains><Layer Name="T" Type="TerrainLayer" Filename=".\plain.hdf">'
        '<Layer Name="Mods" Type="ElevationModificationGroup">'
        '<Layer Name="Berm" Type="LeveeLayer" /></Layer></Layer></Terrains></RASMapper>'
    )
    assert terrain_modifications(plain, rasmap) == ["rasmap:Berm"]
    # A /Modifications soft link is never read as "unmodified".
    linked = _terrain_h5(tmp_path / "link.hdf")
    with h5py.File(linked, "a") as handle:
        handle["Modifications"] = h5py.SoftLink("/Terrain")
    assert terrain_modifications(linked) == ["hdf:/Modifications (link)"]


def test_modified_terrain_skips_rust_terrain_and_uses_the_export(tmp_path, monkeypatch):
    resolver, source, hdf = _resolver_with_source(tmp_path, monkeypatch)
    _terrain_h5(hdf, ["Levee A"])
    monkeypatch.setattr(resolver, "_terrain_hdf_for", lambda plan: hdf)
    monkeypatch.setattr(fast_map, "effective_terrain_supported", lambda: True)
    monkeypatch.setattr(
        resolver, "_effective",
        lambda plan: pytest.fail("the effective terrain must not be built for a modified terrain"),
    )
    kinds = []

    def run(tif, h, k):
        kinds.append(k)
        return "ok"

    assert fast_map.with_terrain(resolver, tmp_path / "a.p01.hdf", run)[1] == fast_map.TERRAIN_EXPORT
    assert kinds == ["export"], "neither the source TIFF nor the effective terrain is tried"
    assert any("1 modification(s)" in note for note in resolver.notes)


def test_unmodified_terrain_still_uses_the_source(tmp_path, monkeypatch):
    resolver, source, hdf = _resolver_with_source(tmp_path, monkeypatch)
    _terrain_h5(hdf)
    monkeypatch.setattr(resolver, "_terrain_hdf_for", lambda plan: hdf)
    _, kind = fast_map.with_terrain(resolver, tmp_path / "a.p01.hdf", lambda tif, h, k: "ok")
    assert kind == fast_map.TERRAIN_SOURCE


def test_boundary_terrain_refuses_a_modified_terrain(tmp_path):
    terrain = tmp_path / "Terrain"
    terrain.mkdir()
    hdf = _terrain_h5(terrain / "Terrain50.hdf", ["HighGround"])
    (terrain / "Terrain50.a.tif").touch()
    resolver = fast_map.TerrainResolver(tmp_path / "m.prj", tmp_path / "w", terrain_hdf=hdf)
    with pytest.raises(fast_map.FastMapUnavailable, match="modification"):
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
    _terrain_h5(terrain_hdf)
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


# ---------------------------------------------------------------------------
# Source terrain (fast-map #11), whole simulation (#12), velocity (#13)
# ---------------------------------------------------------------------------

_SLOPING = {"mode": "sloping", "reduce_shallow_to_horizontal": False,
            "use_depth_weighted_faces": False}


def _velocity_manifest(folder: Path) -> pd.DataFrame:
    frame = _fake_manifest(folder)
    velocity = folder / "12_Max_velocity.tif"
    velocity.write_bytes(b"vel")
    frame["velocity_tif"] = str(velocity)
    return frame


@patch("ras_commander.RasFim")
def test_map_plan_passes_terrain_hdf_and_opt_in_velocity(mock_fim, tmp_path, monkeypatch):
    monkeypatch.setattr(fast_map, "velocity_supported", lambda: True)
    mock_fim.map_sloping_profiles.side_effect = (
        lambda hdf, terrain, folder, **kw: _velocity_manifest(Path(folder))
    )
    out = tmp_path / "p01"
    result = fast_map.map_plan(
        tmp_path / "x.p01.hdf", tmp_path / "src.tif", out,
        map_types=["wse", "depth", "velocity"], render=_SLOPING,
        terrain_hdf=tmp_path / "Terrain.hdf", velocity=True, terrain_kind="source",
    )
    assert result["velocity"] == [out / "Velocity (Max).tif"]
    assert (out / "Velocity (Max).tif").read_bytes() == b"vel"
    kwargs = mock_fim.map_sloping_profiles.call_args.kwargs
    assert kwargs["terrain_hdf"] == tmp_path / "Terrain.hdf" and kwargs["velocity"] is True
    details = json.loads((out / fast_map.FAST_MAP_MANIFEST).read_text())["ras2cng"]
    assert details["terrain_kind"] == "source"


@pytest.mark.parametrize("mode", ["horizontal", "slopingPretty"])
@patch("ras_commander.RasFim")
def test_map_plan_velocity_only_in_plain_sloping(mock_fim, mode, tmp_path, monkeypatch):
    monkeypatch.setattr(fast_map, "velocity_supported", lambda: True)
    mock_fim.map_sloping_profiles.side_effect = (
        lambda hdf, terrain, folder, **kw: _fake_manifest(Path(folder))
    )
    mock_fim.map_horizontal_profiles.side_effect = (
        lambda cells, terrain, folder, **kw: _fake_manifest(Path(folder))
    )
    render = {**_SLOPING, "mode": mode}
    result = fast_map.map_plan(
        tmp_path / "x.p01.hdf", tmp_path / "t.tif", tmp_path / "p01",
        map_types=["depth", "velocity"], render=render, velocity=True,
    )
    assert set(result) == {"depth"}
    if mode == "slopingPretty":
        assert "velocity" not in mock_fim.map_sloping_profiles.call_args.kwargs


@patch("ras_commander.RasFim")
def test_map_plan_without_velocity_flag_leaves_velocity(mock_fim, tmp_path, monkeypatch):
    monkeypatch.setattr(fast_map, "velocity_supported", lambda: True)
    result = fast_map.map_plan(
        tmp_path / "x.p01.hdf", tmp_path / "t.tif", tmp_path / "p01",
        map_types=["velocity"], render=_SLOPING,
    )
    assert result == {}
    mock_fim.map_sloping_profiles.assert_not_called()


def _resolver_with_source(tmp_path, monkeypatch, *, canonical=True):
    resolver = fast_map.TerrainResolver(tmp_path / "a.prj", tmp_path / "w", terrain_name="T")
    monkeypatch.setattr(fast_map, "source_terrain_supported", lambda: True)
    source, hdf = tmp_path / "src.tif", tmp_path / "T.hdf"
    monkeypatch.setattr(resolver, "_source", lambda plan: (source, hdf))
    monkeypatch.setattr(
        resolver, "_canonical", lambda tif: tmp_path / "canon.tif" if canonical else None
    )
    monkeypatch.setattr(resolver, "for_plan", lambda plan: tmp_path / "export.tif")
    return resolver, source, hdf


def test_with_terrain_prefers_the_source_tif(tmp_path, monkeypatch):
    resolver, source, hdf = _resolver_with_source(tmp_path, monkeypatch)
    calls = []

    def run(tif, h, k):
        calls.append((tif, h, k))
        return "ok"

    result, kind = fast_map.with_terrain(resolver, tmp_path / "a.p01.hdf", run)
    assert (result, kind) == ("ok", fast_map.TERRAIN_SOURCE)
    assert calls == [(source, hdf, fast_map.TERRAIN_SOURCE)]


def test_with_terrain_falls_through_to_the_export(tmp_path, monkeypatch):
    resolver, source, hdf = _resolver_with_source(tmp_path, monkeypatch)
    calls = []

    def run(tif, h, k):
        calls.append((tif, h, k))
        if k != fast_map.TERRAIN_EXPORT:
            raise fast_map.FastMapUnavailable("engine terrain CRS differs (" + k + ")")
        return "exported"

    result, kind = fast_map.with_terrain(resolver, tmp_path / "a.p01.hdf", run)
    assert (result, kind) == ("exported", fast_map.TERRAIN_EXPORT)
    assert [c[2] for c in calls] == ["source", "source-canonical", "export"]
    assert calls[1] == (tmp_path / "canon.tif", hdf, "source-canonical")
    assert calls[2][1] is None, "the export is mapped without a terrain scope"
    assert len(resolver.notes) == 2


def test_only_a_crs_refusal_tries_the_canonical_copy(tmp_path, monkeypatch):
    resolver, *_ = _resolver_with_source(tmp_path, monkeypatch)
    kinds = []

    def run(tif, h, k):
        kinds.append(k)
        if k == fast_map.TERRAIN_SOURCE:
            raise fast_map.FastMapUnavailable("28 mapped pixels lie outside the source footprint")
        return "ok"

    assert fast_map.with_terrain(resolver, tmp_path / "a.p01.hdf", run)[1] == fast_map.TERRAIN_EXPORT
    assert kinds == ["source", "export"]


def test_with_terrain_can_skip_the_source(tmp_path, monkeypatch):
    resolver, *_ = _resolver_with_source(tmp_path, monkeypatch)
    _, kind = fast_map.with_terrain(
        resolver, tmp_path / "a.p01.hdf", lambda tif, h, k: "ok", use_source=False,
    )
    assert kind == fast_map.TERRAIN_EXPORT


def test_with_terrain_supplied_terrain_is_the_only_candidate(tmp_path, monkeypatch):
    terrain = tmp_path / "dem.tif"
    terrain.write_bytes(b"x")
    monkeypatch.setattr(fast_map, "source_terrain_supported", lambda: True)
    resolver = fast_map.TerrainResolver(tmp_path / "a.prj", tmp_path / "w", terrain_path=terrain)

    def refuse(tif, h, k):
        raise fast_map.FastMapUnavailable("no")

    with pytest.raises(fast_map.FastMapUnavailable):
        fast_map.with_terrain(resolver, tmp_path / "a.p01.hdf", refuse)
    assert fast_map.with_terrain(resolver, tmp_path / "a.p01.hdf", lambda t, h, k: k) == (
        fast_map.TERRAIN_SUPPLIED, fast_map.TERRAIN_SUPPLIED
    )


def _whole_simulation_manifest(folder: Path, products) -> pd.DataFrame:
    rows = []
    for name in products:
        tif = folder / f"{name}.tif"
        tif.write_bytes(name.encode())
        rows.append({"product": name, "status": "complete", "tif": str(tif),
                     "units": "hours" if name == "arrival_time" else "percent",
                     "output_times": 433, "wet_pixels": 10, "qualified": True, "error": None})
    return pd.DataFrame(rows)


@patch("ras_commander.RasFim")
def test_map_whole_simulation_names_products_like_rasmapper(mock_fim, tmp_path, monkeypatch):
    monkeypatch.setattr(fast_map, "whole_simulation_supported", lambda: True)
    mock_fim.map_whole_simulation.side_effect = (
        lambda hdf, terrain, folder, products, **kw: _whole_simulation_manifest(Path(folder), products)
    )
    out = tmp_path / "p01"
    result = fast_map.map_whole_simulation(
        tmp_path / "x.p01.hdf", tmp_path / "t.tif", out,
        map_types=["arrival_time", "duration", "percent_inundated"],
        threshold=0.5, render=_SLOPING, terrain_hdf=tmp_path / "T.hdf",
    )
    assert result == {
        "arrival_time": [out / "Arrival Time (0.5ft hrs).tif"],
        "percent_inundated": [out / "Percent Time Inundated (0.5ft).tif"],
    }
    kwargs = mock_fim.map_whole_simulation.call_args.kwargs
    assert kwargs["products"] == ("arrival_time", "percent_inundated")
    assert kwargs["threshold"] == 0.5 and kwargs["terrain_hdf"] == tmp_path / "T.hdf"
    details = json.loads((out / fast_map.FAST_MAP_WHOLE_SIMULATION_MANIFEST).read_text())
    assert details["products"]["arrival_time"]["output_times"] == 433
    assert not list(out.glob(".fast-map-*"))


def test_map_whole_simulation_refuses_sloping_pretty(tmp_path):
    with pytest.raises(fast_map.FastMapUnavailable, match="slopingPretty"):
        fast_map.map_whole_simulation(
            tmp_path / "x.p01.hdf", tmp_path / "t.tif", tmp_path / "p01",
            map_types=["arrival_time"], threshold=0.5,
            render={**_SLOPING, "mode": "slopingPretty"},
        )


def test_map_whole_simulation_duration_alone_is_left_to_rasmapper(tmp_path):
    assert fast_map.map_whole_simulation(
        tmp_path / "x.p01.hdf", tmp_path / "t.tif", tmp_path / "p01",
        map_types=["duration"], threshold=0.5, render=_SLOPING,
    ) == {}


@patch("ras2cng.mapping._generate_plan_maps")
@patch("ras2cng.mapping._configure_rasprocess")
@patch("ras2cng.mapping.init_ras_project")
@patch("ras2cng.fast_map.render_settings", return_value=_SLOPING)
@patch("ras2cng.fast_map.map_whole_simulation")
@patch("ras2cng.fast_map.map_plan")
def test_fast_map_routes_arrival_and_percent_and_records_terrain(
    mock_map_plan, mock_whole, _render, mock_init, _config, mock_gen, tmp_path,
):
    ras, project_dir = _fake_ras(tmp_path)
    mock_init.return_value = ras
    terrain = tmp_path / "dem.tif"
    terrain.write_bytes(b"x")
    out = tmp_path / "maps" / "p01"
    mock_map_plan.return_value = {"depth": [out / "Depth (Max).tif"]}
    mock_whole.return_value = {
        "arrival_time": [out / "Arrival Time (0.5ft hrs).tif"],
        "percent_inundated": [out / "Percent Time Inundated (0.5ft).tif"],
    }
    mock_gen.return_value = {"duration": [out / "Duration (0.5ft hrs).tif"]}

    results = generate_result_maps(
        project_dir, tmp_path / "maps", wse=False, depth=True, velocity=False,
        arrival_time=True, duration=True, percent_inundated=True, arrival_depth=0.5,
        map_engine="fast-map", fast_map_terrain=terrain,
    )
    result = results[0]
    assert result.engines == {
        "depth": "fast-map", "arrival_time": "fast-map",
        "percent_inundated": "fast-map", "duration": "rasmapper",
    }
    assert result.fast_map_terrain == {
        "depth": "supplied", "arrival_time": "supplied", "percent_inundated": "supplied",
    }
    assert mock_whole.call_args.kwargs["threshold"] == 0.5
    flags = mock_gen.call_args.kwargs
    assert flags["duration"] is True and flags["arrival_time"] is False


@patch("ras2cng.mapping._generate_plan_maps")
@patch("ras2cng.mapping._configure_rasprocess")
@patch("ras2cng.mapping.init_ras_project")
@patch("ras2cng.fast_map.render_settings", return_value=_SLOPING)
@patch("ras2cng.fast_map.map_whole_simulation")
@patch("ras2cng.fast_map.map_plan")
def test_no_fast_map_whole_simulation_keeps_rasmapper(
    mock_map_plan, mock_whole, _render, mock_init, _config, mock_gen, tmp_path,
):
    ras, project_dir = _fake_ras(tmp_path)
    mock_init.return_value = ras
    terrain = tmp_path / "dem.tif"
    terrain.write_bytes(b"x")
    mock_map_plan.return_value = {}
    mock_gen.return_value = {}
    generate_result_maps(
        project_dir, tmp_path / "maps", wse=False, depth=False, velocity=False,
        arrival_time=True, map_engine="fast-map", fast_map_terrain=terrain,
        fast_map_whole_simulation=False,
    )
    mock_whole.assert_not_called()
    assert mock_gen.call_args.kwargs["arrival_time"] is True


@patch("ras2cng.mapping._generate_plan_maps")
@patch("ras2cng.mapping._configure_rasprocess")
@patch("ras2cng.mapping.init_ras_project")
@patch("ras2cng.fast_map.render_settings", return_value=_SLOPING)
@patch("ras2cng.fast_map.map_velocity")
def test_fast_map_velocity_flag_sends_velocity_to_map_velocity(
    mock_map_velocity, _render, mock_init, _config, mock_gen, tmp_path, monkeypatch,
):
    monkeypatch.setattr(fast_map, "velocity_supported", lambda: True)
    ras, project_dir = _fake_ras(tmp_path)
    mock_init.return_value = ras
    terrain = tmp_path / "dem.tif"
    terrain.write_bytes(b"x")
    vel = tmp_path / "maps" / "p01" / "Velocity (Max).tif"
    mock_map_velocity.return_value = {"velocity": [vel]}
    results = generate_result_maps(
        project_dir, tmp_path / "maps", wse=False, depth=False, velocity=True,
        map_engine="fast-map", fast_map_terrain=terrain, fast_map_velocity=True,
    )
    assert mock_map_velocity.call_args.kwargs["velocity_depth"] == "rendered"
    assert results[0].engines == {"velocity": "fast-map"}
    mock_gen.assert_not_called()
