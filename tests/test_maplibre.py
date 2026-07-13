from __future__ import annotations

import json
from pathlib import Path

import geopandas as gpd
from shapely.geometry import LineString, box
from typer.testing import CliRunner

from ras2cng import maplibre
from ras2cng.cli import app


def _write_archive(tmp_path: Path) -> tuple[Path, Path]:
    project_dir = tmp_path / "project"
    archive_dir = project_dir / "archive"
    archive_dir.mkdir(parents=True)
    project_dir.joinpath("project.json").write_text(
        json.dumps({"title": "Synthetic Model", "href": "/data/synthetic/project.json"}),
        encoding="utf-8",
    )

    geometry = gpd.GeoDataFrame(
        {
            "layer": ["mesh_cells", "centerlines"],
            "cell_id": [7, None],
            "river": [None, "Example Creek"],
            "hilbert_index": [4, 8],
        },
        geometry=[box(-85.0, 40.0, -84.99, 40.01), LineString([(-85.0, 40.0), (-84.99, 40.01)])],
        crs="EPSG:4326",
    )
    geometry.to_parquet(archive_dir / "model.g01.parquet")
    raw_results = gpd.GeoDataFrame(
        {"cell_id": [7], "maximum_depth": [4.25]},
        geometry=[None],
        crs="EPSG:4326",
    )
    raw_results.to_parquet(archive_dir / "results.p01.depth.parquet")

    manifest = {
        "project": {"name": "Synthetic Model"},
        "geometry": [
            {
                "geom_id": "g01",
                "parquet": "model.g01.parquet",
                "layers": [
                    {"layer": "mesh_cells", "filter_value": "mesh_cells"},
                    {"layer": "centerlines", "filter_value": "centerlines"},
                ],
            }
        ],
        "results": [
            {
                "plan_id": "p01",
                "geom_id": "g01",
                "variables": [
                    {
                        "variable": "maximum_depth",
                        "parquet": "results.p01.depth.parquet",
                        "geometry_filter": "mesh_cells",
                        "index_column": "cell_id",
                    }
                ],
            }
        ],
    }
    archive_dir.joinpath("manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    hdf = project_dir / "model.g01.hdf"
    hdf.touch()
    return archive_dir, hdf


def test_package_uses_api_footprint_and_groups_raw_results(monkeypatch, tmp_path: Path):
    archive_dir, hdf = _write_archive(tmp_path)
    calls: list[tuple[Path, list[tuple[str, Path]]]] = []

    def fake_tippecanoe(output: Path, layers, min_zoom: int, max_zoom: int):
        calls.append((output, list(layers)))
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"pmtiles")

    footprint = gpd.GeoDataFrame(geometry=[box(-85.01, 39.99, -84.98, 40.02)], crs="EPSG:4326")
    monkeypatch.setattr(maplibre, "_require_cli", lambda _: None)
    monkeypatch.setattr(maplibre, "_extent_from_hdf", lambda _: footprint)
    monkeypatch.setattr(maplibre, "_run_tippecanoe", fake_tippecanoe)

    summary = maplibre.package_maplibre_viewer(
        archive_dir,
        tmp_path / "viewer",
        geometry_hdfs={"g01": hdf},
        include_vector_results=True,
    )

    manifest = json.loads(summary.manifest_path.read_text(encoding="utf-8"))
    geometry_tiles = manifest["tilesets"][0]
    layers = {layer["kind"]: layer for layer in geometry_tiles["layers"]}
    assert layers["mesh_cells"]["visible"] is True
    assert layers["centerlines"]["visible"] is False
    assert layers["model_extents"]["extentSource"].startswith("HdfProject.get_project_extent")
    assert manifest["groups"][0] == {"id": "ras-geometry-g01", "name": "Geometry g01", "visible": True}
    assert manifest["groups"][1]["id"] == "ras-results-p01"
    assert manifest["groups"][1]["resultKind"] == "raw_hdf"
    assert manifest["tilesets"][1]["resultKind"] == "raw_hdf"
    result_layer = manifest["tilesets"][1]["layers"][0]
    assert result_layer["rawResult"]["source"] == "Raw HEC-RAS HDF summary result values"
    assert summary.geometry_layer_count == 3
    assert summary.result_layer_count == 1
    assert len(calls) == 2
    assert summary.result_pmtiles and summary.result_pmtiles.is_file()


def test_package_requires_a_geometry_hdf_for_every_archive_geometry(tmp_path: Path):
    archive_dir, _ = _write_archive(tmp_path)

    try:
        maplibre.package_maplibre_viewer(archive_dir, tmp_path / "viewer", geometry_hdfs={})
    except ValueError as error:
        assert "Missing geometry HDF mapping" in str(error)
    else:
        raise AssertionError("Expected API footprint mapping validation to fail")


def test_cli_passes_hdf_mappings_and_vector_results(monkeypatch, tmp_path: Path):
    received = {}

    def fake_package(archive_dir, output, **kwargs):
        received["archive_dir"] = archive_dir
        received["output"] = output
        received.update(kwargs)
        return maplibre.PackageSummary(
            manifest_path=output / "manifest.json",
            geometry_pmtiles=output / "tiles" / "geometry.pmtiles",
            result_pmtiles=None,
            geometry_layer_count=3,
            result_layer_count=0,
            bounds=(-85.0, 40.0, -84.9, 40.1),
        )

    monkeypatch.setattr(maplibre, "package_maplibre_viewer", fake_package)
    result = CliRunner().invoke(
        app,
        [
            "maplibre",
            str(tmp_path / "archive"),
            str(tmp_path / "viewer"),
            "--geometry-hdf",
            "g01=model.g01.hdf",
            "--vector-results",
        ],
    )

    assert result.exit_code == 0, result.output
    assert received["geometry_hdfs"] == {"g01": Path("model.g01.hdf")}
    assert received["include_vector_results"] is True
