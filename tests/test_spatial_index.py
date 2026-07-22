from pathlib import Path

import geopandas as gpd
import pandas as pd
import pyarrow.parquet as pq
import pytest
from shapely.geometry import Point

import ras2cng.spatial_index as spatial_index
from ras2cng.catalog import (
    Manifest,
    ManifestGeomEntry,
    ManifestLayer,
    ManifestPlanEntry,
    ManifestResultVariable,
)
from ras2cng.spatial_index import HILBERT_COLUMN, JOIN_INDEX_COLUMN, postprocess_archive


def test_postprocess_archive_records_portable_error_paths(tmp_path: Path, monkeypatch):
    archive = tmp_path / "archive"
    result_path = archive / "results" / "p01" / "value.parquet"
    result_path.parent.mkdir(parents=True)
    pd.DataFrame({"cell_id": [1], "value": [2.0]}).to_parquet(result_path, index=False)

    manifest = Manifest.create("M", tmp_path / "M.prj", tmp_path, archive)
    plan = ManifestPlanEntry(
        plan_id="p01",
        plan_title="Plan 01",
        geom_id="g01",
        flow_id=None,
        hdf_exists=True,
        completed=True,
        layout="variable",
        geometry_mode="none",
    )
    plan.add_variable(
        ManifestResultVariable(
            variable="value",
            filter_value="value",
            rows=1,
            parquet="results/p01/value.parquet",
            geometry_mode="none",
            index_column="cell_id",
        )
    )
    manifest.add_plan_entry(plan)
    manifest.write(archive / "manifest.json")

    def fail_index(*args, **kwargs):
        raise RuntimeError("expected test failure")

    monkeypatch.setattr(spatial_index, "postprocess_result_table", fail_index)
    summary = postprocess_archive(archive)

    assert summary["error_count"] == 1
    assert summary["errors"][0]["path"] == "results/p01/value.parquet"
    assert not Path(summary["errors"][0]["path"]).is_absolute()


def test_postprocess_archive_indexes_geometryless_result_tables(tmp_path: Path):
    pytest.importorskip("duckdb")

    archive = tmp_path / "archive"
    archive.mkdir()

    geom_path = archive / "M.g01.parquet"
    geom = gpd.GeoDataFrame(
        {
            "mesh_name": ["m", "m"],
            "cell_id": [2, 1],
            "layer": ["mesh_cells", "mesh_cells"],
        },
        geometry=[Point(2, 0), Point(1, 0)],
        crs="EPSG:4326",
    )
    geom.to_parquet(geom_path, index=False)

    result_path = archive / "results" / "p01" / "maximum_water_surface.parquet"
    result_path.parent.mkdir(parents=True)
    pd.DataFrame(
        {
            "mesh_name": ["m", "m"],
            "cell_id": [1, 2],
            "maximum_water_surface": [10.0, 20.0],
            "layer": ["maximum_water_surface", "maximum_water_surface"],
        }
    ).to_parquet(result_path, index=False)

    manifest = Manifest.create("M", tmp_path / "M.prj", tmp_path, archive)
    geom_entry = ManifestGeomEntry(
        geom_id="g01",
        source_file="M.g01.hdf",
        file_type="hdf",
        parquet="M.g01.parquet",
    )
    geom_entry.add_layer(
        ManifestLayer(
            layer="mesh_cells",
            filter_value="mesh_cells",
            rows=2,
            geometry_type="Point",
            crs="EPSG:4326",
        )
    )
    manifest.add_geom_entry(geom_entry)

    plan_entry = ManifestPlanEntry(
        plan_id="p01",
        plan_title="Plan 01",
        geom_id="g01",
        flow_id=None,
        hdf_exists=True,
        completed=True,
        layout="variable",
        geometry_mode="none",
    )
    plan_entry.add_variable(
        ManifestResultVariable(
            variable="maximum_water_surface",
            filter_value="maximum_water_surface",
            rows=2,
            parquet="results/p01/maximum_water_surface.parquet",
            geometry_mode="none",
            index_column="cell_id",
            geometry_filter="mesh_cells",
        )
    )
    manifest.add_plan_entry(plan_entry)
    manifest.write(archive / "manifest.json")

    summary = postprocess_archive(archive)

    assert summary["error_count"] == 0
    indexed_geom = gpd.read_parquet(geom_path)
    assert HILBERT_COLUMN in indexed_geom.columns
    geom_columns = set(pq.read_schema(geom_path).names)
    assert {"bbox_xmin", "bbox_ymin", "bbox_xmax", "bbox_ymax"}.issubset(geom_columns)

    indexed_result = pd.read_parquet(result_path)
    assert HILBERT_COLUMN in indexed_result.columns
    assert JOIN_INDEX_COLUMN in indexed_result.columns
    assert indexed_result[HILBERT_COLUMN].notna().all()

    loaded = Manifest.load(archive / "manifest.json")
    variable = loaded.results[0]["variables"][0]
    assert variable["index_status"] == "spatial_join"
    assert variable["hilbert_index"] == HILBERT_COLUMN
    assert variable["join_index"] == JOIN_INDEX_COLUMN
    assert loaded.postprocessing["spatial_index"]["geometry_file_count"] == 1


def test_postprocess_archive_indexes_composite_1d_result_join(tmp_path: Path):
    pytest.importorskip("duckdb")

    archive = tmp_path / "archive"
    archive.mkdir()
    geom_path = archive / "M.g01.parquet"
    gpd.GeoDataFrame(
        {
            "River": ["White"],
            "Reach": ["Main"],
            "RS": ["1000"],
            "layer": ["cross_sections"],
        },
        geometry=[Point(1, 1)],
        crs="EPSG:4326",
    ).to_parquet(geom_path, index=False)
    result_path = archive / "results" / "p01" / "steady_cross_sections.parquet"
    result_path.parent.mkdir(parents=True)
    pd.DataFrame(
        {
            "river": [" White "],
            "reach": ["Main"],
            "node_id": ["1000"],
            "profile": ["Max"],
            "water_surface": [10.0],
            "layer": ["steady_cross_sections"],
        }
    ).to_parquet(result_path, index=False)

    manifest = Manifest.create("M", tmp_path / "M.prj", tmp_path, archive)
    geom_entry = ManifestGeomEntry(
        geom_id="g01",
        source_file="M.g01.hdf",
        file_type="hdf",
        parquet="M.g01.parquet",
    )
    geom_entry.add_layer(
        ManifestLayer(
            layer="cross_sections",
            filter_value="cross_sections",
            rows=1,
            geometry_type="Point",
            crs="EPSG:4326",
        )
    )
    manifest.add_geom_entry(geom_entry)
    plan = ManifestPlanEntry(
        plan_id="p01",
        plan_title="Plan 01",
        geom_id="g01",
        flow_id=None,
        hdf_exists=True,
        completed=True,
        layout="variable",
        geometry_mode="none",
    )
    plan.add_variable(
        ManifestResultVariable(
            variable="steady_cross_sections",
            filter_value="steady_cross_sections",
            rows=1,
            parquet="results/p01/steady_cross_sections.parquet",
            geometry_mode="none",
            geometry_filter="cross_sections",
            join_columns={"River": "river", "Reach": "reach", "RS": "node_id"},
        )
    )
    manifest.add_plan_entry(plan)
    manifest.write(archive / "manifest.json")

    summary = postprocess_archive(archive)

    assert summary["error_count"] == 0
    indexed = pd.read_parquet(result_path)
    assert indexed[JOIN_INDEX_COLUMN].tolist() == [0]
    assert indexed[HILBERT_COLUMN].notna().all()
    loaded = Manifest.load(archive / "manifest.json")
    variable = loaded.results[0]["variables"][0]
    assert variable["index_status"] == "spatial_join"
    assert variable["join_index"] == JOIN_INDEX_COLUMN
