"""
Unit tests for project.py and catalog.py (v2.0 consolidated format).

All tests are fully mocked -- no real HEC-RAS files needed.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import geopandas as gpd
import pandas as pd
import pytest
from shapely.geometry import Point, Polygon

from ras2cng.catalog import (
    Manifest,
    ManifestGeomEntry,
    ManifestLayer,
    ManifestPlanEntry,
    ManifestResultVariable,
    ManifestTerrainEntry,
    SCHEMA_VERSION,
)
from ras2cng.project import resolve_project_path


# ---------------------------------------------------------------------------
# resolve_project_path
# ---------------------------------------------------------------------------

def test_resolve_project_path_with_prj_file(tmp_path):
    prj = tmp_path / "MyModel.prj"
    prj.write_text("Proj Title=Test\n")
    project_dir, found_prj = resolve_project_path(prj)
    assert project_dir == tmp_path
    assert found_prj == prj


def test_resolve_project_path_with_directory(tmp_path):
    prj = tmp_path / "MyModel.prj"
    prj.write_text("Proj Title=Test\n")
    project_dir, found_prj = resolve_project_path(tmp_path)
    assert project_dir == tmp_path
    assert found_prj == prj


def test_resolve_project_path_no_prj_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        resolve_project_path(tmp_path)


def test_resolve_project_path_multiple_prj_raises(tmp_path):
    (tmp_path / "A.prj").write_text("Proj Title=A\n")
    (tmp_path / "B.prj").write_text("Proj Title=B\n")
    with pytest.raises(ValueError, match="Multiple .prj"):
        resolve_project_path(tmp_path)


def test_resolve_project_path_bad_input_raises(tmp_path):
    with pytest.raises(ValueError):
        resolve_project_path(tmp_path / "nonexistent.txt")


# ---------------------------------------------------------------------------
# Manifest / catalog v2.0
# ---------------------------------------------------------------------------

def test_manifest_create(tmp_path):
    prj = tmp_path / "M.prj"
    m = Manifest.create(
        project_name="TestProject",
        prj_file=prj,
        source_path=tmp_path,
        archive_path=tmp_path / "archive",
        crs="EPSG:4326",
        units="US Survey Feet",
        plan_count=3,
        geom_count=2,
    )
    assert m.project["name"] == "TestProject"
    assert m.project["crs"] == "EPSG:4326"
    assert m.project["plan_count"] == 3
    assert m.schema_version == "2.1"
    assert m.geometry == []
    assert m.results == []
    assert m.terrain == []
    assert m.project_parquet is None


def test_manifest_json_roundtrip(tmp_path):
    prj = tmp_path / "M.prj"
    m = Manifest.create("MyModel", prj, tmp_path, tmp_path / "out")
    entry = ManifestGeomEntry(
        geom_id="g01",
        source_file="M.g01.hdf",
        file_type="hdf",
        parquet="MyModel.g01.parquet",
    )
    entry.add_layer(ManifestLayer(
        layer="mesh_cells",
        filter_value="mesh_cells",
        rows=1000,
        geometry_type="Polygon",
        crs="EPSG:2271",
    ))
    m.add_geom_entry(entry)
    m.project_parquet = "MyModel.parquet"

    manifest_path = tmp_path / "manifest.json"
    m.write(manifest_path)

    loaded = Manifest.load(manifest_path)
    assert loaded.project["name"] == "MyModel"
    assert loaded.project_parquet == "MyModel.parquet"
    assert len(loaded.geometry) == 1
    assert loaded.geometry[0]["geom_id"] == "g01"
    assert loaded.geometry[0]["parquet"] == "MyModel.g01.parquet"
    assert len(loaded.geometry[0]["layers"]) == 1
    assert loaded.geometry[0]["layers"][0]["rows"] == 1000
    assert loaded.geometry[0]["layers"][0]["filter_value"] == "mesh_cells"


def test_manifest_layer_paths_deduplicated(tmp_path):
    prj = tmp_path / "M.prj"
    m = Manifest.create("M", prj, tmp_path, tmp_path / "out")
    entry = ManifestGeomEntry(
        geom_id="g01", source_file="M.g01.hdf", file_type="hdf",
        parquet="M.g01.parquet",
    )
    entry.add_layer(ManifestLayer(
        layer="mesh_cells", filter_value="mesh_cells",
        rows=100, geometry_type="Polygon",
    ))
    entry.add_layer(ManifestLayer(
        layer="bc_lines", filter_value="bc_lines",
        rows=5, geometry_type="LineString",
    ))
    m.add_geom_entry(entry)
    # Two layers but one parquet file
    paths = m.layer_paths()
    assert paths == ["M.g01.parquet"]


def test_manifest_geom_ids_property(tmp_path):
    prj = tmp_path / "M.prj"
    m = Manifest.create("M", prj, tmp_path, tmp_path / "out")
    m.add_geom_entry(ManifestGeomEntry(
        geom_id="g01", source_file="a", file_type="hdf", parquet="a.parquet",
    ))
    m.add_geom_entry(ManifestGeomEntry(
        geom_id="g06", source_file="b", file_type="hdf", parquet="b.parquet",
    ))
    assert m.geom_ids == ["g01", "g06"]


def test_manifest_plan_entry(tmp_path):
    prj = tmp_path / "M.prj"
    m = Manifest.create("M", prj, tmp_path, tmp_path / "out")
    entry = ManifestPlanEntry(
        plan_id="p01",
        plan_title="Dam Break",
        geom_id="g01",
        flow_id="u01",
        hdf_exists=True,
        completed=True,
        parquet="M.p01.parquet",
    )
    entry.add_variable(ManifestResultVariable(
        variable="maximum_depth",
        filter_value="maximum_depth",
        rows=87039,
    ))
    m.add_plan_entry(entry)
    assert m.plan_ids == ["p01"]
    assert m.results[0]["parquet"] == "M.p01.parquet"
    assert m.results[0]["variables"][0]["variable"] == "maximum_depth"
    assert m.results[0]["variables"][0]["filter_value"] == "maximum_depth"


def test_manifest_result_paths_deduplicated(tmp_path):
    prj = tmp_path / "M.prj"
    m = Manifest.create("M", prj, tmp_path, tmp_path / "out")
    entry = ManifestPlanEntry(
        plan_id="p01", plan_title="Plan", geom_id="g01",
        flow_id=None, hdf_exists=True, completed=True,
        parquet="M.p01.parquet",
    )
    entry.add_variable(ManifestResultVariable(
        variable="maximum_depth", filter_value="maximum_depth", rows=100,
    ))
    entry.add_variable(ManifestResultVariable(
        variable="maximum_velocity", filter_value="maximum_velocity", rows=100,
    ))
    m.add_plan_entry(entry)
    paths = m.result_paths()
    assert paths == ["M.p01.parquet"]


def test_manifest_to_json_is_valid(tmp_path):
    prj = tmp_path / "M.prj"
    m = Manifest.create("M", prj, tmp_path, tmp_path / "out")
    json_str = m.to_json()
    parsed = json.loads(json_str)
    assert parsed["schema_version"] == "2.1"
    assert "project" in parsed
    assert "project_parquet" in parsed


# ---------------------------------------------------------------------------
# archive_project (mocked, v2 consolidated format)
# ---------------------------------------------------------------------------

def _make_fake_ras(tmp_path, geom_hdf_exists=True):
    """Build a fake RasPrj-like object with minimal DataFrames."""
    project_name = "FakeModel"
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    (project_dir / "Terrain").mkdir()

    # Create a fake .prj
    (project_dir / f"{project_name}.prj").write_text("Proj Title=Fake\nEnglish Units\n")

    # Create a fake geometry HDF
    hdf_path = project_dir / f"{project_name}.g01.hdf"
    if geom_hdf_exists:
        hdf_path.touch()

    geom_df = pd.DataFrame({
        "geom_file": ["g01"],
        "geom_number": ["01"],
        "full_path": [str(project_dir / f"{project_name}.g01")],
        "hdf_path": [str(hdf_path)],
        "has_2d_mesh": [True],
        "has_1d_xs": [False],
    })
    plan_df = pd.DataFrame({
        "plan_number": ["01"],
        "geometry_number": ["01"],
        "unsteady_number": [None],
        "Plan Title": ["Test Plan"],
        "flow_type": ["Unsteady"],
    })

    ras = MagicMock()
    ras.project_name = project_name
    ras.project_folder = project_dir
    ras.geom_df = geom_df
    ras.plan_df = plan_df
    ras.results_df = pd.DataFrame()
    ras.flow_df = pd.DataFrame()
    ras.unsteady_df = None
    ras.boundaries_df = None
    ras.rasmap_df = None

    return ras, project_dir, hdf_path


def _make_fake_gdf(geom_type="Polygon", n=3):
    """Create a minimal GeoDataFrame for testing."""
    if geom_type == "Polygon":
        geoms = [Polygon([(i, 0), (i + 1, 0), (i + 1, 1), (i, 0)]) for i in range(n)]
    else:
        geoms = [Point(i, i) for i in range(n)]
    return gpd.GeoDataFrame(
        {"mesh_name": ["TestMesh"] * n, "cell_id": list(range(n))},
        geometry=geoms,
        crs="EPSG:4326",
    )


def _make_fake_merged_gdf():
    """Create a merged GDF with layer column (simulates merge_all_layers output)."""
    polys = [Polygon([(i, 0), (i + 1, 0), (i + 1, 1), (i, 0)]) for i in range(3)]
    lines = [Point(i, i) for i in range(2)]  # using Point for simplicity
    gdf1 = gpd.GeoDataFrame(
        {"mesh_name": ["M"] * 3, "cell_id": [0, 1, 2], "layer": ["mesh_cells"] * 3},
        geometry=polys, crs="EPSG:4326",
    )
    gdf2 = gpd.GeoDataFrame(
        {"mesh_name": [None, None], "cell_id": [None, None], "layer": ["bc_lines"] * 2},
        geometry=lines, crs="EPSG:4326",
    )
    merged = pd.concat([gdf1, gdf2], ignore_index=True)
    return gpd.GeoDataFrame(merged, geometry="geometry")


@patch("ras2cng.project.merge_all_layers")
@patch("ras2cng.project.init_ras_project")
def test_archive_geometry_only_creates_flat_parquet(
    mock_init, mock_merge, tmp_path
):
    """archive_project writes consolidated parquet files, not nested dirs."""
    ras, project_dir, hdf_path = _make_fake_ras(tmp_path)
    mock_init.return_value = ras
    mock_merge.return_value = _make_fake_merged_gdf()

    prj = project_dir / "FakeModel.prj"
    archive_out = tmp_path / "archive"

    from ras2cng.project import archive_project

    manifest = archive_project(prj, archive_out, sort=False)

    # Consolidated parquet should exist at root level
    assert (archive_out / "FakeModel.g01.parquet").exists()
    # No nested geometry/ directory
    assert not (archive_out / "geometry").exists()
    # manifest.json should exist
    assert (archive_out / "manifest.json").exists()
    # No results
    assert not (archive_out / "results").exists()
    # Manifest v2.1 fields
    assert len(manifest.geometry) >= 1
    assert manifest.geometry[0]["geom_id"] == "g01"
    assert manifest.geometry[0]["parquet"] == "FakeModel.g01.parquet"
    assert manifest.schema_version == "2.1"


@patch("ras2cng.project.merge_all_layers")
@patch("ras2cng.project.init_ras_project")
def test_archive_writes_project_metadata_parquet(
    mock_init, mock_merge, tmp_path
):
    """archive_project writes a project metadata parquet."""
    ras, project_dir, hdf_path = _make_fake_ras(tmp_path)
    mock_init.return_value = ras
    mock_merge.return_value = _make_fake_merged_gdf()

    prj = project_dir / "FakeModel.prj"
    archive_out = tmp_path / "archive"

    from ras2cng.project import archive_project

    manifest = archive_project(prj, archive_out, sort=False)

    meta_path = archive_out / "FakeModel.parquet"
    assert meta_path.exists()
    assert manifest.project_parquet == "FakeModel.parquet"

    # Read and verify _table column
    df = pd.read_parquet(meta_path)
    assert "_table" in df.columns
    # Should have plan_df and geom_df at minimum
    tables = set(df["_table"].unique())
    assert "plan_df" in tables
    assert "geom_df" in tables


@patch("ras2cng.project.merge_all_layers")
@patch("ras2cng.project.init_ras_project")
def test_archive_no_results_when_include_results_false(
    mock_init, mock_merge, tmp_path
):
    ras, project_dir, hdf_path = _make_fake_ras(tmp_path)
    mock_init.return_value = ras
    mock_merge.return_value = None  # No layers extracted

    from ras2cng.project import archive_project

    manifest = archive_project(project_dir / "FakeModel.prj", tmp_path / "out", sort=False)
    assert manifest.results == []


@patch("ras2cng.project.merge_all_layers")
@patch("ras2cng.project.init_ras_project")
def test_archive_results_flag_writes_consolidated_plan(
    mock_init, mock_merge, tmp_path
):
    ras, project_dir, hdf_path = _make_fake_ras(tmp_path)
    mock_init.return_value = ras
    mock_merge.return_value = _make_fake_merged_gdf()

    # Create fake plan HDF
    plan_hdf = project_dir / "FakeModel.p01.hdf"
    plan_hdf.touch()

    from ras2cng.project import archive_project

    # Build a fake results GDF with layer column
    results_gdf = _make_fake_gdf("Polygon", n=3)
    results_gdf["layer"] = "maximum_depth"

    with patch("ras2cng.results.merge_all_variables", return_value=results_gdf):
        manifest = archive_project(
            project_dir / "FakeModel.prj",
            tmp_path / "out",
            include_results=True,
            sort=False,
        )

    assert len(manifest.results) == 1
    assert manifest.results[0]["plan_id"] == "p01"
    assert manifest.results[0]["parquet"] == "FakeModel.p01.parquet"
    assert (tmp_path / "out" / "FakeModel.p01.parquet").exists()


# ---------------------------------------------------------------------------
# export_project_metadata
# ---------------------------------------------------------------------------

def test_export_project_metadata(tmp_path):
    """export_project_metadata should write all RasPrj DataFrames with _table column."""
    from ras2cng.project import export_project_metadata

    ras = MagicMock()
    ras.plan_df = pd.DataFrame({"plan_number": ["01"], "Plan Title": ["Test"]})
    ras.geom_df = pd.DataFrame({"geom_number": ["01"], "geom_file": ["g01"]})
    ras.flow_df = pd.DataFrame({"flow_number": ["01"]})
    ras.unsteady_df = None
    ras.boundaries_df = pd.DataFrame()  # empty
    ras.results_df = None
    ras.rasmap_df = None

    out = tmp_path / "meta.parquet"
    export_project_metadata(ras, out)

    assert out.exists()
    df = pd.read_parquet(out)
    assert "_table" in df.columns
    tables = set(df["_table"].unique())
    assert "plan_df" in tables
    assert "geom_df" in tables
    assert "flow_df" in tables
    # None and empty should be excluded
    assert "unsteady_df" not in tables
    assert "boundaries_df" not in tables


# ---------------------------------------------------------------------------
# Manifest v2.1 — maps field
# ---------------------------------------------------------------------------

def test_manifest_map_entry(tmp_path):
    from ras2cng.catalog import ManifestMapEntry

    prj = tmp_path / "M.prj"
    m = Manifest.create("M", prj, tmp_path, tmp_path / "out")
    entry = ManifestMapEntry(
        plan_id="p01",
        profile="Max",
        rasters=[{"type": "depth", "file": "maps/p01/depth.tif", "size_bytes": 1024}],
        min_depth=0.1,
        reprojected_wgs84=False,
    )
    m.add_map_entry(entry)

    assert len(m.maps) == 1
    assert m.maps[0]["plan_id"] == "p01"
    assert m.maps[0]["profile"] == "Max"
    assert len(m.maps[0]["rasters"]) == 1


def test_manifest_maps_in_json(tmp_path):
    from ras2cng.catalog import ManifestMapEntry

    prj = tmp_path / "M.prj"
    m = Manifest.create("M", prj, tmp_path, tmp_path / "out")
    m.add_map_entry(ManifestMapEntry(
        plan_id="p01", profile="Max",
        rasters=[{"type": "wse", "file": "wse.tif", "size_bytes": 512}],
    ))

    d = m.to_dict()
    assert "maps" in d
    assert len(d["maps"]) == 1

    # Roundtrip through JSON
    manifest_path = tmp_path / "manifest.json"
    m.write(manifest_path)
    loaded = Manifest.load(manifest_path)
    assert len(loaded.maps) == 1
    assert loaded.maps[0]["plan_id"] == "p01"


def test_manifest_maps_omitted_when_empty(tmp_path):
    """maps key should not appear in JSON when empty."""
    prj = tmp_path / "M.prj"
    m = Manifest.create("M", prj, tmp_path, tmp_path / "out")
    d = m.to_dict()
    assert "maps" not in d


# ---------------------------------------------------------------------------
# Enhanced inspect — new fields
# ---------------------------------------------------------------------------

def test_project_info_has_new_fields():
    """ProjectInfo should have ras_version, terrain_details, rasmap_path fields."""
    from ras2cng.project import ProjectInfo, TerrainFileInfo
    info = ProjectInfo(
        name="Test",
        prj_file=Path("test.prj"),
        project_dir=Path("."),
        crs="EPSG:4326",
        units="US Survey Feet",
        ras_version="6.5",
        terrain_details=[
            TerrainFileInfo(name="T50", crs="EPSG:2271", resolution="50.0 x 50.0"),
        ],
        rasmap_path=Path("test.rasmap"),
    )
    assert info.ras_version == "6.5"
    assert len(info.terrain_details) == 1
    assert info.terrain_details[0].name == "T50"
    assert info.rasmap_path == Path("test.rasmap")


def test_terrain_file_info_defaults():
    from ras2cng.project import TerrainFileInfo
    tfi = TerrainFileInfo(name="Test")
    assert tfi.hdf_path is None
    assert tfi.hdf_exists is False
    assert tfi.tif_files == []
    assert tfi.crs is None
    assert tfi.resolution is None
    assert tfi.total_size_mb == 0.0


# ---------------------------------------------------------------------------
# archive_project — new flags acceptance test
# ---------------------------------------------------------------------------

@patch("ras2cng.project.merge_all_layers")
@patch("ras2cng.project.init_ras_project")
def test_archive_accepts_new_flags(mock_init, mock_merge, tmp_path):
    """archive_project should accept map_results and consolidate_terrain flags."""
    ras, project_dir, hdf_path = _make_fake_ras(tmp_path)
    mock_init.return_value = ras
    mock_merge.return_value = _make_fake_merged_gdf()

    from ras2cng.project import archive_project

    # Just verify the new flags don't cause errors (map/terrain will skip
    # since there's no RasProcess/rasterio in test env)
    manifest = archive_project(
        project_dir / "FakeModel.prj",
        tmp_path / "out",
        sort=False,
        map_results=False,
        consolidate_terrain=False,
        ras_version="6.6",
    )

    assert manifest is not None
    assert (tmp_path / "out" / "manifest.json").exists()
