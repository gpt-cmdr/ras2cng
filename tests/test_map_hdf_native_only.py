"""Native-only ``map-hdf``: a plan HDF plus a pre-exported terrain GeoTIFF.

No HEC-RAS terrain is built or copied, and RAS Mapper is never called.
RasFim, RAS Mapper and project loading are mocked; the plan HDF is
synthetic.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from ras2cng import fast_map
from ras2cng.cli import app
from ras2cng.mapping import generate_result_maps
from ras2cng.scaffold import build_scaffold
from tests.test_fast_map import _fake_ras
from tests.test_scaffold import make_plan_hdf

runner = CliRunner()


@pytest.fixture(autouse=True)
def _preflight_uses_patched_render(monkeypatch):
    # conftest pins RAS2CNG_MAP_ENGINE=rasmapper; native-only is about the default.
    monkeypatch.delenv(fast_map.MAP_ENGINE_ENV, raising=False)
    monkeypatch.setattr(
        fast_map, "preflight", lambda plan, ras, mode, **kw: fast_map.render_settings(ras, mode)
    )


# ---------------------------------------------------------------------------
# Scaffold
# ---------------------------------------------------------------------------

def test_scaffold_without_terrain_builds_no_terrain(tmp_path):
    plan_hdf = make_plan_hdf(tmp_path / "results.hdf")
    with patch("ras_commander.RasTerrain") as mock_terrain:
        info = build_scaffold(plan_hdf, tmp_path / "s", allow_no_terrain=True)
    mock_terrain.create_terrain_from_rasters.assert_not_called()
    assert info.terrain_hdf is None
    assert info.plan_hdf.exists()
    root = ET.parse(tmp_path / "s" / "TestModel.rasmap").getroot()
    assert root.find("Terrains") is not None
    assert root.find("Terrains/Layer") is None
    assert (tmp_path / "s" / "Terrain" / "Projection.prj").exists()


def test_scaffold_still_requires_a_terrain_by_default(tmp_path):
    plan_hdf = make_plan_hdf(tmp_path / "results.hdf")
    with pytest.raises(ValueError, match="exactly one"):
        build_scaffold(plan_hdf, tmp_path / "s")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _patch_cli(monkeypatch, tmp_path):
    from ras2cng.scaffold import ScaffoldInfo, read_plan_hdf_metadata

    calls: dict = {}

    def fake_build_scaffold(plan_hdf, workdir, **kwargs):
        calls["scaffold"] = kwargs
        meta = read_plan_hdf_metadata(plan_hdf)
        return ScaffoldInfo(
            project_dir=Path(workdir), prj_file=Path(workdir) / "TestModel.prj",
            plan_hdf=Path(workdir) / "TestModel.p07.hdf", terrain_hdf=None, meta=meta,
        )

    def fake_generate(prj_file, output, **kwargs):
        calls["maps"] = kwargs
        return []

    monkeypatch.setattr("ras2cng.scaffold.build_scaffold", fake_build_scaffold)
    monkeypatch.setattr("ras2cng.mapping.generate_result_maps", fake_generate)
    return calls


def test_map_hdf_fast_map_terrain_alone_is_native_only(monkeypatch, tmp_path):
    calls = _patch_cli(monkeypatch, tmp_path)
    plan_hdf = make_plan_hdf(tmp_path / "results.hdf")
    export = tmp_path / "export.tif"
    export.write_bytes(b"TIFF")

    result = runner.invoke(app, [
        "map-hdf", str(plan_hdf), str(tmp_path / "out"), "--fast-map-terrain", str(export),
    ])

    assert result.exit_code == 0, result.output
    assert "Native-only" in result.output
    assert calls["scaffold"]["terrain_tifs"] is None
    assert calls["scaffold"]["terrain_hdf"] is None
    assert calls["scaffold"]["allow_no_terrain"] is True
    assert calls["maps"]["native_only"] is True
    assert calls["maps"]["fast_map_fallback"] is False
    assert calls["maps"]["fast_map_terrain"] == export


def test_map_hdf_native_only_refuses_rasmapper_engine(monkeypatch, tmp_path):
    calls = _patch_cli(monkeypatch, tmp_path)
    plan_hdf = make_plan_hdf(tmp_path / "results.hdf")
    export = tmp_path / "export.tif"
    export.write_bytes(b"TIFF")
    result = runner.invoke(app, [
        "map-hdf", str(plan_hdf), str(tmp_path / "out"),
        "--fast-map-terrain", str(export), "--engine", "rasmapper",
    ])
    assert result.exit_code == 1
    assert "needs --engine fast-map" in result.output
    assert "scaffold" not in calls


def test_map_hdf_native_only_needs_an_existing_terrain(monkeypatch, tmp_path):
    _patch_cli(monkeypatch, tmp_path)
    plan_hdf = make_plan_hdf(tmp_path / "results.hdf")
    result = runner.invoke(app, [
        "map-hdf", str(plan_hdf), str(tmp_path / "out"),
        "--fast-map-terrain", str(tmp_path / "missing.tif"),
    ])
    assert result.exit_code == 1
    assert "not found" in result.output


def test_map_hdf_with_terrain_is_unchanged(monkeypatch, tmp_path):
    calls = _patch_cli(monkeypatch, tmp_path)
    plan_hdf = make_plan_hdf(tmp_path / "results.hdf")
    dem = tmp_path / "dem.tif"
    dem.write_bytes(b"TIFF")
    result = runner.invoke(app, ["map-hdf", str(plan_hdf), str(tmp_path / "out"), "--terrain", str(dem)])
    assert result.exit_code == 0, result.output
    assert calls["scaffold"]["allow_no_terrain"] is False
    assert calls["maps"]["native_only"] is False
    assert calls["maps"]["fast_map_fallback"] is True


# ---------------------------------------------------------------------------
# generate_result_maps(native_only=True)
# ---------------------------------------------------------------------------

@patch("ras2cng.mapping._generate_plan_maps")
@patch("ras2cng.mapping._configure_rasprocess")
@patch("ras2cng.mapping.init_ras_project")
@patch("ras2cng.fast_map.render_settings", return_value={"mode": "sloping"})
@patch("ras2cng.fast_map.map_plan")
def test_native_only_maps_on_the_supplied_terrain(
    mock_map_plan, _render, mock_init, _config, mock_gen, tmp_path,
):
    ras, project_dir = _fake_ras(tmp_path)
    mock_init.return_value = ras
    export = tmp_path / "export.tif"
    export.write_bytes(b"TIFF")
    depth = tmp_path / "Depth (Max).tif"
    mock_map_plan.return_value = {"depth": [depth]}

    results = generate_result_maps(
        project_dir, tmp_path / "maps", wse=False, depth=True, velocity=False,
        map_engine="fast-map", fast_map_terrain=export, native_only=True,
        skip_errors=True,
    )

    mock_gen.assert_not_called()
    assert results[0].engines == {"depth": "fast-map"}
    assert results[0].fast_map_terrain == {"depth": fast_map.TERRAIN_SUPPLIED}
    assert mock_map_plan.call_args.args[1] == export


@patch("ras2cng.mapping._generate_plan_maps")
@patch("ras2cng.mapping._configure_rasprocess")
@patch("ras2cng.mapping.init_ras_project")
@patch("ras2cng.fast_map.render_settings", return_value={"mode": "sloping"})
@patch("ras2cng.fast_map.map_plan")
def test_native_only_fails_closed_when_a_product_needs_rasmapper(
    mock_map_plan, _render, mock_init, _config, mock_gen, tmp_path,
):
    ras, project_dir = _fake_ras(tmp_path)
    mock_init.return_value = ras
    export = tmp_path / "export.tif"
    export.write_bytes(b"TIFF")
    mock_map_plan.return_value = {"depth": [tmp_path / "Depth (Max).tif"]}

    # A non-Max profile is outside fast-map's scope: it would go to RAS Mapper.
    with pytest.raises(fast_map.FastMapUnavailable):
        generate_result_maps(
            project_dir, tmp_path / "maps", wse=False, depth=True, velocity=False,
            profile="PF 1", map_engine="fast-map", fast_map_terrain=export,
            native_only=True, skip_errors=True,
        )
    mock_gen.assert_not_called()


@patch("ras2cng.mapping._generate_plan_maps")
@patch("ras2cng.mapping._configure_rasprocess")
@patch("ras2cng.mapping.init_ras_project")
@patch("ras2cng.fast_map.render_settings", return_value={"mode": "sloping"})
@patch("ras2cng.fast_map.map_plan")
def test_native_only_engine_failure_is_not_retried_in_rasmapper(
    mock_map_plan, _render, mock_init, _config, mock_gen, tmp_path,
):
    ras, project_dir = _fake_ras(tmp_path)
    mock_init.return_value = ras
    export = tmp_path / "export.tif"
    export.write_bytes(b"TIFF")
    mock_map_plan.side_effect = fast_map.FastMapUnavailable("engine refused the terrain")

    with pytest.raises(fast_map.FastMapUnavailable, match="refused"):
        generate_result_maps(
            project_dir, tmp_path / "maps", wse=False, depth=True, velocity=False,
            map_engine="fast-map", fast_map_terrain=export, native_only=True,
        )
    mock_gen.assert_not_called()


def test_native_only_requires_fast_map_and_a_terrain(tmp_path):
    with pytest.raises(ValueError, match="fast-map engine"):
        generate_result_maps(
            tmp_path, tmp_path / "maps", map_engine="rasmapper",
            fast_map_terrain=tmp_path / "x.tif", native_only=True,
        )
    with pytest.raises(ValueError, match="fast_map_terrain"):
        generate_result_maps(tmp_path, tmp_path / "maps", map_engine="fast-map", native_only=True)
