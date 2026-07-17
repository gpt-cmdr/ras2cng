"""
Unit tests for terrain.py — terrain discovery and consolidation.

All tests are fully mocked -- no real HEC-RAS files or rasterio needed.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from unittest.mock import MagicMock, patch, mock_open

import pandas as pd
import pytest

from ras2cng.terrain import (
    TerrainInfo,
    TerrainResolutionDecision,
    discover_terrains,
    consolidate_terrain,
    consolidate_terrain_files,
    consolidate_project_terrains,
    extract_terrain_modification_layers,
    export_terrain_modifications,
    extract_terrain_source_footprints,
    export_terrain_source_footprints,
    select_terrain_resolution,
    _discover_tifs_for_hdf,
    _get_raster_info,
    _merge_tifs,
    _downsample_tif,
    _stem_matches_name,
    _crs_equivalent,
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
def test_consolidate_terrain_files_records_policy_and_display_paths(mock_merge, tmp_path):
    source = tmp_path / "native.tif"
    source.write_bytes(b"fake")
    output_dir = tmp_path / "output"
    inventory = [{"resolution_x": 3.0, "resolution_y": 3.0, "path": str(source)}]

    with patch("ras2cng.terrain.inspect_terrain_sources", return_value=inventory):
        result = consolidate_terrain_files(
            [source],
            output_dir,
            terrain_name="Published",
            source_terrain_name="Native Terrain",
            source_paths=["Terrain/native.tif"],
        )

    assert result == output_dir / "Published_merged.tif"
    assert mock_merge.call_args.kwargs["target_resolution"] == 6.0
    provenance = json.loads(
        (output_dir / "Published_terrain-provenance.json").read_text(encoding="utf-8")
    )
    assert provenance["terrain_name"] == "Native Terrain"
    assert provenance["sources"][0]["path"] == "Terrain/native.tif"
    assert provenance["resolution"]["target_resolution"] == 6.0
    assert provenance["output"] == "Published_merged.tif"


def test_consolidate_terrain_files_rejects_mismatched_display_paths(tmp_path):
    source = tmp_path / "native.tif"
    source.write_bytes(b"fake")
    inventory = [{"resolution_x": 5.0, "resolution_y": 5.0, "path": str(source)}]

    with patch("ras2cng.terrain.inspect_terrain_sources", return_value=inventory):
        with pytest.raises(ValueError, match="one display path"):
            consolidate_terrain_files(
                [source],
                tmp_path / "output",
                source_paths=[],
            )

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

    inventory = [{"resolution_x": 5.0, "resolution_y": 5.0}]
    with patch("ras2cng.terrain.inspect_terrain_sources", return_value=inventory):
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
    assert mock_merge.call_args.kwargs["target_resolution"] == 5.0
    assert (output_dir / "Consolidated_terrain-provenance.json").is_file()


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
@patch("ras2cng.terrain.discover_terrains")
def test_consolidate_terrain_with_downsample(mock_discover, mock_merge, tmp_path):
    """Consolidation creates the reduced grid directly to bound memory."""
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
    output_dir.mkdir(parents=True, exist_ok=True)
    merged_path.write_bytes(b"merged")

    mock_merge.return_value = merged_path
    inventory = [{"resolution_x": 5.0, "resolution_y": 5.0}]
    with patch("ras2cng.terrain.inspect_terrain_sources", return_value=inventory):
        result = consolidate_terrain(
            project_dir, output_dir,
            downsample_factor=2.0,
            create_hdf=False,
        )

    assert result == merged_path
    assert mock_merge.call_args.kwargs["target_resolution"] == 10.0


@patch("ras2cng.terrain.discover_terrains")
def test_consolidate_terrain_requires_one_named_surface(mock_discover, tmp_path):
    mock_discover.return_value = [
        TerrainInfo(name="Existing", tif_files=[tmp_path / "existing.tif"]),
        TerrainInfo(name="Modified", tif_files=[tmp_path / "modified.tif"]),
    ]

    with pytest.raises(ValueError, match="Multiple named terrains"):
        consolidate_terrain(tmp_path, tmp_path / "out", create_hdf=False)


@patch("ras2cng.terrain.consolidate_terrain")
@patch("ras2cng.terrain.discover_terrains")
def test_consolidate_project_terrains_keeps_named_surfaces_separate(
    mock_discover, mock_consolidate, tmp_path
):
    mock_discover.return_value = [
        TerrainInfo(name="Existing Terrain"),
        TerrainInfo(name="Proposed Terrain"),
    ]
    mock_consolidate.side_effect = [
        tmp_path / "Existing_Terrain_merged.tif",
        tmp_path / "Proposed_Terrain_merged.tif",
    ]

    outputs = consolidate_project_terrains(
        tmp_path,
        tmp_path / "out",
        target_resolutions={"Proposed Terrain": 6.0},
    )

    assert list(outputs) == ["Existing Terrain", "Proposed Terrain"]
    first_call, second_call = mock_consolidate.call_args_list
    assert first_call.kwargs["terrain_names"] == ["Existing Terrain"]
    assert second_call.kwargs["terrain_names"] == ["Proposed Terrain"]
    assert second_call.kwargs["target_resolution"] == 6.0


# ---------------------------------------------------------------------------
# terrain publication resolution policy
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("native", "expected"),
    [
        (1.0, 5.0),
        (2.0, 6.0),
        (3.0, 6.0),
        (5.0, 5.0),
        (7.5, 7.5),
        (30.0, 30.0),
    ],
)
def test_select_terrain_resolution_uses_smallest_native_multiple(native, expected):
    decision = select_terrain_resolution([native], horizontal_units="Feet")

    assert isinstance(decision, TerrainResolutionDecision)
    assert decision.target_resolution == pytest.approx(expected)
    assert decision.factors[0] == pytest.approx(expected / native)


def test_select_terrain_resolution_does_not_upsample_coarse_native_grid():
    with pytest.raises(ValueError, match="upsample"):
        select_terrain_resolution([30.0], requested=10.0)


def test_select_terrain_resolution_requires_explicit_mixed_target():
    with pytest.raises(ValueError, match="Mixed native"):
        select_terrain_resolution([1.0, 3.0])

    decision = select_terrain_resolution([1.0, 3.0], requested=6.0)
    assert decision.target_resolution == 6.0
    assert decision.mixed_native_resolution is True
    assert decision.policy == "whole-coarsest-native-multiple-no-upsample"


def test_select_terrain_resolution_supports_mixed_foot_and_meter_tiles():
    native = [2.0, 3.27873, 3.28084, 3.28]
    target = 2 * max(native)

    decision = select_terrain_resolution(native, requested=target)

    assert decision.target_resolution == pytest.approx(6.56168)
    assert decision.factors[2] == pytest.approx(2.0)
    assert any(not math.isclose(factor, round(factor)) for factor in decision.factors)


def test_select_terrain_resolution_rejects_fractional_native_multiple():
    with pytest.raises(ValueError, match="whole-number multiple"):
        select_terrain_resolution([2.0, 3.0], requested=7.0)


def test_select_terrain_resolution_converts_five_foot_floor_to_meters():
    decision = select_terrain_resolution([0.5], horizontal_units="Meters")
    assert decision.target_resolution == 2.0
    assert decision.minimum_resolution == pytest.approx(1.524)


def test_merge_tifs_preserves_priority_and_transparent_nodata(tmp_path):
    import numpy as np
    import rasterio
    from rasterio.transform import from_origin

    first = tmp_path / "first.tif"
    second = tmp_path / "second.tif"
    profile = {
        "driver": "GTiff",
        "width": 4,
        "height": 4,
        "count": 1,
        "dtype": "float32",
        "crs": "EPSG:2271",
        "transform": from_origin(0, 20, 5, 5),
        "nodata": -9999.0,
    }
    first_values = np.full((4, 4), 10.0, dtype="float32")
    first_values[0, 0] = -9999.0
    with rasterio.open(first, "w", **profile) as destination:
        destination.write(first_values, 1)
    with rasterio.open(second, "w", **profile) as destination:
        destination.write(np.full((4, 4), 20.0, dtype="float32"), 1)

    output = _merge_tifs(
        [first, second],
        tmp_path / "merged.tif",
        target_resolution=5.0,
        block_size=16,
    )

    with rasterio.open(output) as source:
        values = source.read(1)
        assert source.nodata is not None and np.isnan(source.nodata)
        assert values[0, 0] == 20.0
        assert np.all(values[1:, :] == 10.0)
        assert np.all(values[0, 1:] == 10.0)


def test_terrain_modifications_export_lines_polygons_and_controls(tmp_path):
    import h5py
    import numpy as np

    terrain_hdf = tmp_path / "Terrain.hdf"
    with h5py.File(terrain_hdf, "w") as hdf:
        line = hdf.create_group("Modifications/Channel Cut")
        line.attrs["Type"] = np.bytes_("Channel")
        line.attrs["Subtype"] = np.bytes_("GroundLine")
        line.create_dataset("Polyline Points", data=np.array([[0.0, 0.0], [10.0, 5.0]]))

        polygon = hdf.create_group("Modifications/Fill Area")
        polygon.attrs["Type"] = np.bytes_("Polygon")
        polygon.attrs["Subtype"] = np.bytes_("Multipoint")
        polygon.create_dataset(
            "Polygon Points",
            data=np.array([[0.0, 0.0], [10.0, 0.0], [10.0, 10.0], [0.0, 0.0]]),
        )
        polygon.create_dataset("Polygon Parts", data=np.array([[0, 4]], dtype="int32"))
        polygon.create_dataset("Boundary Elevations", data=np.array([100.0, 101.0, 102.0, 100.0]))
        controls = polygon.create_group("Control Points")
        controls.create_dataset("Points", data=np.array([[5.0, 5.0]]))
        controls.create_dataset("Elevations", data=np.array([99.5]))

    layers = extract_terrain_modification_layers(terrain_hdf, crs="EPSG:2271")

    assert len(layers["terrain_modification_lines"]) == 1
    assert len(layers["terrain_modification_polygons"]) == 1
    assert len(layers["terrain_modification_control_points"]) == 1
    assert layers["terrain_modification_lines"].geometry.iloc[0].geom_type == "LineString"
    assert layers["terrain_modification_polygons"].geometry.iloc[0].geom_type == "Polygon"
    assert layers["terrain_modification_polygons"].iloc[0]["boundary_elevation_max"] == 102.0
    assert layers["terrain_modification_control_points"].iloc[0]["elevation"] == 99.5

    outputs = export_terrain_modifications(
        terrain_hdf,
        tmp_path / "modifications",
        crs="EPSG:2271",
    )
    assert set(outputs) == {
        "terrain_modification_lines",
        "terrain_modification_polygons",
        "terrain_modification_control_points",
    }
    assert all(path.is_file() for path in outputs.values())


def test_terrain_source_footprints_preserve_priority_and_native_metadata(tmp_path):
    import numpy as np
    import rasterio
    from rasterio.transform import from_origin

    sources = []
    for index, left in enumerate((0.0, 20.0)):
        path = tmp_path / f"tile-{index}.tif"
        with rasterio.open(
            path,
            "w",
            driver="GTiff",
            width=4,
            height=4,
            count=1,
            dtype="float32",
            crs="EPSG:2271",
            transform=from_origin(left, 20, 5, 5),
            nodata=-9999.0,
        ) as destination:
            destination.write(np.full((4, 4), index + 1, dtype="float32"), 1)
        sources.append(path)

    footprints = extract_terrain_source_footprints(sources)

    assert footprints["priority"].tolist() == [0, 1]
    assert footprints["source_file"].tolist() == ["tile-0.tif", "tile-1.tif"]
    assert footprints["resolution_x"].tolist() == [5.0, 5.0]
    assert footprints.crs.to_epsg() == 2271
    assert footprints.geometry.iloc[0].bounds == (0.0, 0.0, 20.0, 20.0)

    output = export_terrain_source_footprints(
        sources,
        tmp_path / "terrain_source_footprints.parquet",
    )
    assert output.is_file()


# ---------------------------------------------------------------------------
# _stem_matches_name (terrain name filtering)
# ---------------------------------------------------------------------------

def test_stem_matches_name_exact():
    assert _stem_matches_name("Terrain", "Terrain") is True


def test_stem_matches_name_with_dot_suffix():
    """Terrain.muncie_clip should match terrain name 'Terrain'."""
    assert _stem_matches_name("Terrain.muncie_clip", "Terrain") is True


def test_stem_matches_name_with_underscore_suffix():
    """Terrain_tile2 should match terrain name 'Terrain'."""
    assert _stem_matches_name("Terrain_tile2", "Terrain") is True


def test_stem_matches_name_rejects_different_terrain():
    """TerrainWithChannel should NOT match terrain name 'Terrain'."""
    assert _stem_matches_name("TerrainWithChannel", "Terrain") is False


def test_stem_matches_name_rejects_different_terrain_with_suffix():
    """TerrainWithChannel.ChannelOnly should NOT match 'Terrain'."""
    assert _stem_matches_name("TerrainWithChannel.ChannelOnly", "Terrain") is False


def test_stem_matches_name_case_insensitive():
    assert _stem_matches_name("terrain.tif_data", "Terrain") is True


def test_stem_matches_name_dash_separator():
    assert _stem_matches_name("HighRes-50ft", "HighRes") is True


def test_stem_matches_name_no_false_prefix():
    """'Terr' should NOT match 'Terrain' (name is shorter than stem)."""
    assert _stem_matches_name("Terrain", "Terr") is False


# ---------------------------------------------------------------------------
# _crs_equivalent
# ---------------------------------------------------------------------------

def test_crs_equivalent_same_object():
    """Identical CRS objects should be equivalent."""
    mock_crs = MagicMock()
    mock_crs.__eq__ = MagicMock(return_value=True)
    assert _crs_equivalent(mock_crs, mock_crs) is True


def test_crs_equivalent_same_epsg():
    """CRS with same EPSG code should be equivalent."""
    crs1 = MagicMock()
    crs2 = MagicMock()
    crs1.__eq__ = MagicMock(return_value=False)
    crs1.to_epsg.return_value = 2965
    crs2.to_epsg.return_value = 2965
    crs1.to_wkt.side_effect = Exception("no pyproj")
    assert _crs_equivalent(crs1, crs2) is True


def test_crs_equivalent_different_epsg():
    """CRS with different EPSG codes should not be equivalent."""
    crs1 = MagicMock()
    crs2 = MagicMock()
    crs1.__eq__ = MagicMock(return_value=False)
    crs1.to_epsg.return_value = 2965
    crs2.to_epsg.return_value = 4326
    crs1.to_wkt.side_effect = Exception("no pyproj")
    assert _crs_equivalent(crs1, crs2) is False


def test_crs_equivalent_none_epsg_falls_through():
    """When EPSG is None, should try WKT comparison."""
    crs1 = MagicMock()
    crs2 = MagicMock()
    crs1.__eq__ = MagicMock(return_value=False)
    crs1.to_epsg.return_value = None
    crs2.to_epsg.return_value = None
    wkt = 'PROJCS["NAD83 / Indiana East"]'
    crs1.to_wkt.return_value = wkt
    crs2.to_wkt.return_value = wkt
    assert _crs_equivalent(crs1, crs2) is True
