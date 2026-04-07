"""
Unit tests for mapping.py — result raster generation via RasProcess.

All tests are fully mocked -- no real HEC-RAS files or RasProcess.exe needed.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pandas as pd
import pytest

from ras2cng.mapping import (
    MapResult,
    MAP_TYPE_VARIABLES,
    generate_result_maps,
    _build_requested_types,
    _configure_rasprocess,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_fake_ras(tmp_path, plan_count=1, plan_hdf_exists=True):
    """Build a fake RasPrj-like object with minimal DataFrames."""
    project_dir = tmp_path / "project"
    project_dir.mkdir(exist_ok=True)
    prj = project_dir / "TestModel.prj"
    prj.write_text("Proj Title=Test\nEnglish Units\n")

    plan_rows = []
    for i in range(1, plan_count + 1):
        plan_num = str(i).zfill(2)
        plan_rows.append({
            "plan_number": plan_num,
            "geometry_number": "01",
            "Plan Title": f"Plan {i}",
        })
        if plan_hdf_exists:
            (project_dir / f"TestModel.p{plan_num}.hdf").touch()

    ras = MagicMock()
    ras.project_name = "TestModel"
    ras.project_folder = str(project_dir)
    ras.plan_df = pd.DataFrame(plan_rows) if plan_rows else pd.DataFrame()
    ras.geom_df = pd.DataFrame()
    ras.results_df = pd.DataFrame()

    return ras, project_dir, prj


# ---------------------------------------------------------------------------
# _build_requested_types
# ---------------------------------------------------------------------------

def test_build_requested_types_defaults():
    result = _build_requested_types(
        wse=True, depth=True, velocity=True,
        froude=False, shear_stress=False,
        depth_x_velocity=False,
        depth_x_velocity_sq=False,
        inundation_boundary=False,
        arrival_time=False, duration=False,
        recession=False,
    )
    assert result == ["wse", "depth", "velocity"]


def test_build_requested_types_all_enabled():
    result = _build_requested_types(
        wse=True, depth=True, velocity=True,
        froude=True, shear_stress=True,
        depth_x_velocity=True,
        depth_x_velocity_sq=True,
        inundation_boundary=True,
        arrival_time=True, duration=True,
        recession=True,
    )
    assert len(result) == 11


def test_build_requested_types_none_enabled():
    result = _build_requested_types(
        wse=False, depth=False, velocity=False,
        froude=False, shear_stress=False,
        depth_x_velocity=False,
        depth_x_velocity_sq=False,
        inundation_boundary=False,
        arrival_time=False, duration=False,
        recession=False,
    )
    assert result == []


# ---------------------------------------------------------------------------
# MAP_TYPE_VARIABLES
# ---------------------------------------------------------------------------

def test_map_type_variables_keys():
    expected = {
        "wse", "depth", "velocity", "froude", "shear_stress",
        "depth_x_velocity", "depth_x_velocity_sq", "inundation_boundary",
        "arrival_time", "duration", "recession",
    }
    assert set(MAP_TYPE_VARIABLES.keys()) == expected


# ---------------------------------------------------------------------------
# _configure_rasprocess (mocked)
# ---------------------------------------------------------------------------

@patch("ras2cng.mapping.RasProcess")
def test_configure_rasprocess_with_explicit_path(mock_rp):
    exe_path = Path("/usr/bin/RasProcess.exe")
    _configure_rasprocess(rasprocess_path=exe_path)
    # configure_wine receives the directory containing RasProcess.exe
    mock_rp.configure_wine.assert_called_once_with(ras_install_dir=str(exe_path.parent))


@patch("ras2cng.mapping.RasProcess")
def test_configure_rasprocess_with_version(mock_rp):
    _configure_rasprocess(ras_version="6.6")
    mock_rp.find_rasprocess.assert_called_once_with(version="6.6")


@patch("ras2cng.mapping.RasProcess")
def test_configure_rasprocess_no_args(mock_rp):
    _configure_rasprocess()
    mock_rp.configure_wine.assert_not_called()
    mock_rp.find_rasprocess.assert_not_called()


# ---------------------------------------------------------------------------
# generate_result_maps (mocked)
# ---------------------------------------------------------------------------

@patch("ras2cng.mapping._generate_plan_maps")
@patch("ras2cng.mapping._configure_rasprocess")
@patch("ras2cng.mapping.init_ras_project")
def test_generate_result_maps_basic(mock_init, mock_config, mock_gen, tmp_path):
    """generate_result_maps should process all plans and return MapResult list."""
    ras, project_dir, prj = _make_fake_ras(tmp_path, plan_count=1)
    mock_init.return_value = ras
    output_dir = tmp_path / "maps"

    # Mock plan map generation to return a dict of map type -> TIF paths
    fake_tif = output_dir / "p01" / "depth.tif"
    fake_tif.parent.mkdir(parents=True, exist_ok=True)
    fake_tif.write_bytes(b"fake tif")
    mock_gen.return_value = {"depth": [fake_tif]}

    results = generate_result_maps(
        project_dir,
        output_dir,
        wse=False, depth=True, velocity=False,
    )

    assert len(results) == 1
    assert results[0].plan_id == "p01"
    assert "depth" in results[0].map_types
    assert len(results[0].map_types["depth"]) == 1


@patch("ras2cng.mapping._generate_plan_maps")
@patch("ras2cng.mapping._configure_rasprocess")
@patch("ras2cng.mapping.init_ras_project")
def test_generate_result_maps_plan_filtering(mock_init, mock_config, mock_gen, tmp_path):
    """generate_result_maps should filter to specified plans."""
    ras, project_dir, prj = _make_fake_ras(tmp_path, plan_count=3)
    mock_init.return_value = ras
    output_dir = tmp_path / "maps"

    mock_gen.return_value = {}

    results = generate_result_maps(
        project_dir, output_dir,
        plans=["p02"],
        wse=False, depth=True, velocity=False,
    )

    # Should only process p02
    assert len(results) == 1
    assert results[0].plan_id == "p02"


@patch("ras2cng.mapping._generate_plan_maps")
@patch("ras2cng.mapping._configure_rasprocess")
@patch("ras2cng.mapping.init_ras_project")
def test_generate_result_maps_skips_missing_hdf(mock_init, mock_config, mock_gen, tmp_path):
    """Plans without HDF files should be skipped."""
    ras, project_dir, prj = _make_fake_ras(tmp_path, plan_count=1, plan_hdf_exists=False)
    mock_init.return_value = ras
    output_dir = tmp_path / "maps"

    results = generate_result_maps(project_dir, output_dir, depth=True)

    assert len(results) == 0
    mock_gen.assert_not_called()


@patch("ras2cng.mapping._generate_plan_maps")
@patch("ras2cng.mapping._configure_rasprocess")
@patch("ras2cng.mapping.init_ras_project")
def test_generate_result_maps_error_handling(mock_init, mock_config, mock_gen, tmp_path):
    """Errors should be recorded but not raised when skip_errors=True."""
    ras, project_dir, prj = _make_fake_ras(tmp_path, plan_count=1)
    mock_init.return_value = ras
    output_dir = tmp_path / "maps"

    mock_gen.side_effect = RuntimeError("RasProcess failed")

    results = generate_result_maps(
        project_dir, output_dir,
        wse=False, depth=True, velocity=False,
        skip_errors=True,
    )

    assert len(results) == 1
    assert len(results[0].errors) == 1
    assert "RasProcess failed" in results[0].errors[0]


@patch("ras2cng.mapping._generate_plan_maps")
@patch("ras2cng.mapping._configure_rasprocess")
@patch("ras2cng.mapping.init_ras_project")
def test_generate_result_maps_error_raises_when_fail_fast(mock_init, mock_config, mock_gen, tmp_path):
    """Errors should raise when skip_errors=False."""
    ras, project_dir, prj = _make_fake_ras(tmp_path, plan_count=1)
    mock_init.return_value = ras
    output_dir = tmp_path / "maps"

    mock_gen.side_effect = RuntimeError("RasProcess failed")

    with pytest.raises(RuntimeError, match="RasProcess failed"):
        generate_result_maps(
            project_dir, output_dir,
            wse=False, depth=True, velocity=False,
            skip_errors=False,
        )


@patch("ras2cng.mapping._generate_plan_maps")
@patch("ras2cng.mapping._configure_rasprocess")
@patch("ras2cng.mapping.init_ras_project")
def test_generate_result_maps_no_types_selected(mock_init, mock_config, mock_gen, tmp_path):
    """Should return empty list when no map types are selected."""
    ras, project_dir, prj = _make_fake_ras(tmp_path, plan_count=1)
    mock_init.return_value = ras
    output_dir = tmp_path / "maps"

    results = generate_result_maps(
        project_dir, output_dir,
        wse=False, depth=False, velocity=False,
    )

    assert results == []
    mock_gen.assert_not_called()


@patch("ras2cng.mapping._generate_plan_maps")
@patch("ras2cng.mapping._configure_rasprocess")
@patch("ras2cng.mapping.init_ras_project")
def test_generate_result_maps_passes_render_mode(mock_init, mock_config, mock_gen, tmp_path):
    """render_mode should be forwarded to _generate_plan_maps."""
    ras, project_dir, prj = _make_fake_ras(tmp_path, plan_count=1)
    mock_init.return_value = ras
    mock_gen.return_value = {}

    generate_result_maps(
        project_dir, tmp_path / "maps",
        wse=False, depth=True, velocity=False,
        render_mode="slopingPretty",
    )

    _, kwargs = mock_gen.call_args
    assert kwargs.get("render_mode") == "slopingPretty"


@patch("ras2cng.mapping._generate_plan_maps")
@patch("ras2cng.mapping._configure_rasprocess")
@patch("ras2cng.mapping.init_ras_project")
def test_generate_result_maps_empty_project(mock_init, mock_config, mock_gen, tmp_path):
    """Should handle project with no plans gracefully."""
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    prj = project_dir / "Empty.prj"
    prj.write_text("Proj Title=Empty\n")

    ras = MagicMock()
    ras.project_name = "Empty"
    ras.plan_df = None
    mock_init.return_value = ras

    results = generate_result_maps(project_dir, tmp_path / "maps", depth=True)
    assert results == []


# ---------------------------------------------------------------------------
# MapResult dataclass
# ---------------------------------------------------------------------------

def test_map_result_defaults():
    mr = MapResult(plan_id="p01", plan_number="01")
    assert mr.map_types == {}
    assert mr.errors == []


def test_map_result_with_data(tmp_path):
    tif = tmp_path / "depth.tif"
    tif.touch()
    mr = MapResult(
        plan_id="p01",
        plan_number="01",
        map_types={"depth": [tif]},
        output_dir=tmp_path,
    )
    assert len(mr.map_types["depth"]) == 1
    assert mr.map_types["depth"][0] == tif
