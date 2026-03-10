"""
Unit tests for terrain.py — terrain discovery and consolidation.

All tests are fully mocked -- no real HEC-RAS files or rasterio needed.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch, mock_open

import pandas as pd
import pytest

from ras2cng.terrain import (
    TerrainInfo,
    discover_terrains,
    consolidate_terrain,
    _discover_tifs_for_hdf,
    _get_raster_info,
    _merge_tifs,
    _downsample_tif,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_fake_ras(tmp_path, terrain_names=None, rasmap_df=None):
    """Build a fake RasPrj-like object."""
    project_dir = tmp_path / "project"
    project_dir.mkdir(exist_ok=True)
    (project_dir / "Terrain").mkdir(exist_ok=True)
    prj = project_dir / "TestModel.prj"
    prj.write_text("Proj Title=Test\nEnglish Units\n")

    ras = MagicMock()
    ras.project_name = "TestModel"
    ras.project_folder = str(project_dir)
    ras.geom_df = pd.DataFrame()
    ras.plan_df = pd.DataFrame()
    ras.results_df = pd.DataFrame()
    ras.rasmap_df = rasmap_df

    return ras, project_dir, prj


# ---------------------------------------------------------------------------
# _discover_tifs_for_hdf
# ---------------------------------------------------------------------------

def test_discover_tifs_for_hdf_finds_matching_tifs(tmp_path):
    terrain_dir = tmp_path / "Terrain"
    terrain_dir.mkdir()
    hdf = terrain_dir / "Terrain50.hdf"
    hdf.touch()
    tif1 = terrain_dir / "Terrain50.tif"
    tif1.touch()
    tif2 = terrain_dir / "Terrain50_tile2.tif"
    tif2.touch()
    other = terrain_dir / "Other.tif"
    other.touch()

    result = _discover_tifs_for_hdf(hdf)
    names = [p.name for p in result]
    assert "Terrain50.tif" in names
    assert "Terrain50_tile2.tif" in names
    assert "Other.tif" not in names


def test_discover_tifs_for_hdf_returns_empty_for_none():
    assert _discover_tifs_for_hdf(None) == []


def test_discover_tifs_for_hdf_returns_empty_for_missing_file(tmp_path):
    assert _discover_tifs_for_hdf(tmp_path / "nonexistent.hdf") == []


def test_discover_tifs_for_hdf_falls_back_to_all_tifs(tmp_path):
    """When no TIFs match the HDF stem, return all TIFs in directory."""
    terrain_dir = tmp_path / "Terrain"
    terrain_dir.mkdir()
    hdf = terrain_dir / "MyTerrain.hdf"
    hdf.touch()
    tif = terrain_dir / "SomeOther.tif"
    tif.touch()

    result = _discover_tifs_for_hdf(hdf)
    assert len(result) == 1
    assert result[0].name == "SomeOther.tif"


# ---------------------------------------------------------------------------
# _get_raster_info
# ---------------------------------------------------------------------------

def test_get_raster_info_empty_list():
    assert _get_raster_info([]) == {}


def test_get_raster_info_with_mock_rasterio(tmp_path):
    """Test raster info extraction with mocked rasterio."""
    tif = tmp_path / "test.tif"
    tif.touch()

    mock_src = MagicMock()
    mock_src.crs.to_epsg.return_value = 2271
    mock_src.res = (50.0, 50.0)
    mock_src.bounds = (100, 200, 300, 400)
    mock_src.__enter__ = MagicMock(return_value=mock_src)
    mock_src.__exit__ = MagicMock(return_value=False)

    with patch("rasterio.open", return_value=mock_src):
        result = _get_raster_info([tif])

    assert result["crs"] == "EPSG:2271"
    assert result["resolution"] == "50.0 x 50.0"


# ---------------------------------------------------------------------------
# discover_terrains (mocked)
# ---------------------------------------------------------------------------

@patch("ras2cng.terrain._get_terrain_names_safe")
@patch("ras_commander.init_ras_project")
def test_discover_terrains_from_filesystem(mock_init, mock_names, tmp_path):
    """When no rasmap terrain info, falls back to scanning Terrain/ dir."""
    ras, project_dir, prj = _make_fake_ras(tmp_path)
    mock_init.return_value = ras
    mock_names.return_value = []

    # Create fake terrain files
    terrain_dir = project_dir / "Terrain"
    hdf = terrain_dir / "Terrain50.hdf"
    hdf.touch()
    tif = terrain_dir / "Terrain50.tif"
    tif.write_bytes(b"fake tif data")

    with patch("ras2cng.terrain._get_raster_info", return_value={"crs": "EPSG:2271", "resolution": "50.0 x 50.0"}):
        result = discover_terrains(project_dir)

    assert len(result) >= 1
    assert result[0].name == "Terrain50"
    assert result[0].hdf_exists is True


@patch("ras2cng.terrain._get_terrain_names_safe")
@patch("ras_commander.init_ras_project")
def test_discover_terrains_from_rasmap(mock_init, mock_names, tmp_path):
    """When rasmap provides terrain names, use them in priority order."""
    rasmap_df = pd.DataFrame({
        "terrain_name": ["HighRes", "LowRes"],
        "terrain_hdf_path": ["Terrain/HighRes.hdf", "Terrain/LowRes.hdf"],
    })
    ras, project_dir, prj = _make_fake_ras(tmp_path, rasmap_df=rasmap_df)
    mock_init.return_value = ras
    mock_names.return_value = ["HighRes", "LowRes"]

    # Create terrain HDFs
    terrain_dir = project_dir / "Terrain"
    (terrain_dir / "HighRes.hdf").touch()
    (terrain_dir / "LowRes.hdf").touch()
    (terrain_dir / "HighRes.tif").write_bytes(b"fake")

    with patch("ras2cng.terrain._get_raster_info", return_value={}):
        result = discover_terrains(project_dir)

    assert len(result) == 2
    assert result[0].name == "HighRes"
    assert result[1].name == "LowRes"


# ---------------------------------------------------------------------------
# consolidate_terrain (mocked)
# ---------------------------------------------------------------------------

@patch("ras2cng.terrain._merge_tifs")
@patch("ras2cng.terrain.discover_terrains")
def test_consolidate_terrain_tiff_only(mock_discover, mock_merge, tmp_path):
    """consolidate_terrain with create_hdf=False should produce a TIFF."""
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    prj = project_dir / "Test.prj"
    prj.write_text("Proj Title=Test\n")

    tif_path = project_dir / "Terrain" / "T50.tif"
    tif_path.parent.mkdir(parents=True, exist_ok=True)
    tif_path.write_bytes(b"fake")

    mock_discover.return_value = [
        TerrainInfo(name="T50", tif_files=[tif_path], hdf_exists=False)
    ]

    output_dir = tmp_path / "output"
    merged_path = output_dir / "Consolidated_merged.tif"
    mock_merge.return_value = merged_path

    # Create the expected output file
    output_dir.mkdir(parents=True, exist_ok=True)
    merged_path.write_bytes(b"merged")

    result = consolidate_terrain(
        project_dir, output_dir,
        terrain_name="Consolidated",
        create_hdf=False,
    )

    assert result == merged_path
    mock_merge.assert_called_once()
    # Verify the TIF files were passed in correct order
    call_args = mock_merge.call_args[0]
    assert tif_path in call_args[0]


@patch("ras2cng.terrain.discover_terrains")
def test_consolidate_terrain_no_terrains_raises(mock_discover, tmp_path):
    """consolidate_terrain should raise if no terrains found."""
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    prj = project_dir / "Test.prj"
    prj.write_text("Proj Title=Test\n")

    mock_discover.return_value = []

    with pytest.raises(ValueError, match="No terrain data found"):
        consolidate_terrain(project_dir, tmp_path / "out", create_hdf=False)


@patch("ras2cng.terrain.discover_terrains")
def test_consolidate_terrain_filters_by_name(mock_discover, tmp_path):
    """consolidate_terrain should filter terrains by name when specified."""
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    prj = project_dir / "Test.prj"
    prj.write_text("Proj Title=Test\n")

    mock_discover.return_value = [
        TerrainInfo(name="HighRes", tif_files=[], hdf_exists=False),
        TerrainInfo(name="LowRes", tif_files=[], hdf_exists=False),
    ]

    with pytest.raises(ValueError, match="No terrains matching"):
        consolidate_terrain(
            project_dir, tmp_path / "out",
            terrain_names=["NonExistent"],
            create_hdf=False,
        )


@patch("ras2cng.terrain._merge_tifs")
@patch("ras2cng.terrain._downsample_tif")
@patch("ras2cng.terrain.discover_terrains")
def test_consolidate_terrain_with_downsample(mock_discover, mock_downsample, mock_merge, tmp_path):
    """consolidate_terrain should call _downsample_tif when factor provided."""
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    prj = project_dir / "Test.prj"
    prj.write_text("Proj Title=Test\n")

    tif = tmp_path / "t.tif"
    tif.write_bytes(b"fake")

    mock_discover.return_value = [
        TerrainInfo(name="T", tif_files=[tif], hdf_exists=False)
    ]

    output_dir = tmp_path / "output"
    merged_path = output_dir / "Consolidated_merged.tif"
    downsampled_path = output_dir / "Consolidated_downsampled.tif"

    output_dir.mkdir(parents=True, exist_ok=True)
    merged_path.write_bytes(b"merged")
    downsampled_path.write_bytes(b"downsampled")

    mock_merge.return_value = merged_path
    mock_downsample.return_value = downsampled_path

    result = consolidate_terrain(
        project_dir, output_dir,
        downsample_factor=2.0,
        create_hdf=False,
    )

    assert result == downsampled_path
    mock_downsample.assert_called_once()
    call_kwargs = mock_downsample.call_args
    assert call_kwargs[1]["factor"] == 2.0
