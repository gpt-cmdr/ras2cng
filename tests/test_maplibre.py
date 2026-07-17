from __future__ import annotations

import json
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import pytest
import rasterio
from rasterio.transform import from_origin
from shapely.geometry import LineString, Point, box
from typer.testing import CliRunner

from ras2cng import maplibre
from ras2cng.cli import app


def test_raster_source_metadata_includes_browser_projection_definition(tmp_path: Path) -> None:
    raster = tmp_path / "depth.tif"
    with rasterio.open(
        raster,
        "w",
        driver="GTiff",
        width=2,
        height=2,
        count=1,
        dtype="float32",
        crs="EPSG:2965",
        transform=from_origin(400000, 1805000, 5, 5),
        nodata=-9999.0,
    ) as target:
        target.write(np.ones((1, 2, 2), dtype="float32"))

    metadata = maplibre._raster_source_metadata(raster)

    assert metadata["sourceCrs"] == "EPSG:2965"
    assert metadata["sourceProj4"].startswith("+proj=tmerc")
    assert "+units=us-ft" in metadata["sourceProj4"]
    assert "=True" not in metadata["sourceProj4"]


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

    def fake_tippecanoe(output: Path, layers, min_zoom: int, max_zoom: int, temporary_directory: Path):
        calls.append((output, list(layers), temporary_directory))
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"pmtiles")

    footprint = gpd.GeoDataFrame(geometry=[box(-85.01, 39.99, -84.98, 40.02)], crs="EPSG:4326")
    monkeypatch.setattr(maplibre, "_require_cli", lambda _: None)
    monkeypatch.setattr(maplibre, "_extent_from_hdf", lambda *_: footprint)
    monkeypatch.setattr(maplibre, "_run_tippecanoe", fake_tippecanoe)

    summary = maplibre.package_maplibre_viewer(
        archive_dir,
        tmp_path / "viewer",
        geometry_hdfs={"g01": hdf},
        include_vector_results=True,
        scratch_dir=tmp_path / "scratch",
    )

    manifest = json.loads(summary.manifest_path.read_text(encoding="utf-8"))
    assert manifest["schema"] == "rascommander.maplibre/v2"
    assert [root["id"] for root in manifest["tree"]] == [
        "features",
        "geometries",
        "results",
        "map-layers",
        "terrains",
    ]
    assert manifest["resources"]["geometry"]["type"] == "vector-pmtiles"
    assert manifest["resources"]["results"]["type"] == "vector-pmtiles"
    assert manifest["layers"]["ras-results-p01-maximum-depth"]["sourceKind"] == "raw-hdf"
    assert manifest["associations"][0]["type"] == "plan-geometry"
    geometry_tiles = [tileset for tileset in manifest["tilesets"] if tileset["id"].startswith("geometry")]
    layers = {layer["kind"]: layer for tileset in geometry_tiles for layer in tileset["layers"]}
    assert layers["model_extents"]["visible"] is True
    assert layers["mesh_cells"]["visible"] is True
    assert layers["centerlines"]["visible"] is False
    assert layers["model_extents"]["extentSource"].startswith("HdfProject.get_project_extent")
    assert manifest["groups"][0] == {"id": "ras-geometry-g01", "name": "Geometry 01", "visible": True}
    assert manifest["groups"][1]["id"] == "ras-results-p01"
    assert manifest["groups"][1]["resultKind"] == "raw_hdf"
    detail_tiles = next(tileset for tileset in geometry_tiles if tileset["id"] == "geometry-detail")
    assert detail_tiles["minzoom"] == 13
    result_tiles = next(tileset for tileset in manifest["tilesets"] if tileset["id"] == "results")
    assert result_tiles["resultKind"] == "raw_hdf"
    result_layer = result_tiles["layers"][0]
    assert result_layer["rawResult"]["source"] == "Raw HEC-RAS HDF summary result values"
    assert summary.geometry_layer_count == 3
    assert summary.result_layer_count == 1
    assert len(calls) == 3
    assert all(call[2].is_relative_to(tmp_path / "scratch") for call in calls)
    assert summary.result_pmtiles and summary.result_pmtiles.is_file()


def test_package_reads_plan_layout_raw_results(monkeypatch, tmp_path: Path):
    archive_dir, hdf = _write_archive(tmp_path)
    raw_results = pd.DataFrame(
        {
            "layer": ["maximum_depth", "unrelated_variable"],
            "cell_id": [7, 7],
            "value": [4.25, 99.0],
        }
    )
    raw_results.to_parquet(archive_dir / "model.p01.parquet")
    manifest_path = archive_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["results"][0]["parquet"] = "model.p01.parquet"
    manifest["results"][0]["variables"][0].update(
        parquet="",
        filter_value="maximum_depth",
    )
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    emitted_results: list[dict] = []

    def fake_tippecanoe(output: Path, layers, min_zoom: int, max_zoom: int, temporary_directory: Path):
        for source_layer, source_path in layers:
            if source_layer.startswith("ras-results-"):
                emitted_results.extend(
                    json.loads(line) for line in source_path.read_text(encoding="utf-8").splitlines()
                )
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"pmtiles")

    footprint = gpd.GeoDataFrame(geometry=[box(-85.01, 39.99, -84.98, 40.02)], crs="EPSG:4326")
    monkeypatch.setattr(maplibre, "_require_cli", lambda _: None)
    monkeypatch.setattr(maplibre, "_extent_from_hdf", lambda *_: footprint)
    monkeypatch.setattr(maplibre, "_run_tippecanoe", fake_tippecanoe)

    summary = maplibre.package_maplibre_viewer(
        archive_dir,
        tmp_path / "viewer",
        geometry_hdfs={"g01": hdf},
        include_vector_results=True,
        scratch_dir=tmp_path / "scratch",
    )

    viewer_manifest = json.loads(summary.manifest_path.read_text(encoding="utf-8"))
    layer = viewer_manifest["layers"]["ras-results-p01-maximum-depth"]
    assert summary.result_layer_count == 1
    assert len(emitted_results) == 1
    assert emitted_results[0]["properties"]["value"] == 4.25
    assert layer["provenance"]["archiveFilter"] == {
        "column": "layer",
        "value": "maximum_depth",
    }


def test_default_visibility_uses_1d_centerlines_without_cross_sections() -> None:
    manifest = {
        "groups": [
            {"id": "ras-geometry-g01", "name": "Geometry g01", "visible": True},
            {"id": "ras-geometry-g02", "name": "Geometry g02", "visible": False},
        ],
        "tilesets": [
            {
                "type": "vector",
                "layers": [
                    {"groupId": "ras-geometry-g01", "kind": "model_extents", "visible": False},
                    {"groupId": "ras-geometry-g01", "kind": "centerlines", "visible": False},
                    {"groupId": "ras-geometry-g01", "kind": "cross_sections", "visible": True},
                    {"groupId": "ras-geometry-g02", "kind": "model_extents", "visible": True},
                    {"groupId": "ras-geometry-g02", "kind": "centerlines", "visible": True},
                ],
            }
        ],
    }

    maplibre.apply_maplibre_default_visibility(manifest)

    layers = manifest["tilesets"][0]["layers"]
    assert [layer["visible"] for layer in layers] == [True, True, False, False, False]
    assert manifest["groups"][0]["visible"] is True
    assert manifest["groups"][1]["visible"] is False


def test_default_visibility_uses_2d_refinement_context_when_present() -> None:
    manifest = {
        "groups": [{"id": "ras-geometry-g01", "name": "Geometry g01", "visible": True}],
        "tilesets": [
            {
                "type": "vector",
                "layers": [
                    {"groupId": "ras-geometry-g01", "kind": "model_extents", "visible": False},
                    {"groupId": "ras-geometry-g01", "kind": "mesh_areas", "visible": False},
                    {"groupId": "ras-geometry-g01", "kind": "mesh_cells", "visible": False},
                    {"groupId": "ras-geometry-g01", "kind": "breaklines", "visible": False},
                    {"groupId": "ras-geometry-g01", "kind": "refinement_regions", "visible": False},
                    {"groupId": "ras-geometry-g01", "kind": "mesh_faces", "visible": True},
                ],
            }
        ],
    }

    maplibre.apply_maplibre_default_visibility(manifest)

    layers = {layer["kind"]: layer for layer in manifest["tilesets"][0]["layers"]}
    assert all(layers[kind]["visible"] is True for kind in (
        "model_extents", "mesh_areas", "mesh_cells", "breaklines", "refinement_regions"
    ))
    assert layers["mesh_faces"]["visible"] is False


def test_default_visibility_includes_pipe_network_geometry() -> None:
    manifest = {
        "groups": [{"id": "ras-geometry-g01", "name": "Geometry g01", "visible": True}],
        "tilesets": [
            {
                "type": "vector",
                "layers": [
                    {"groupId": "ras-geometry-g01", "kind": "model_extents", "visible": False},
                    {"groupId": "ras-geometry-g01", "kind": "pipe_conduits", "visible": False},
                    {"groupId": "ras-geometry-g01", "kind": "pipe_nodes", "visible": False},
                ],
            }
        ],
    }

    maplibre.apply_maplibre_default_visibility(manifest)

    assert all(layer["visible"] is True for layer in manifest["tilesets"][0]["layers"])


def test_default_visibility_can_enable_every_primary_geometry_layer() -> None:
    manifest = {
        "groups": [
            {"id": "ras-geometry-g01", "name": "Geometry 01", "visible": True},
            {"id": "ras-geometry-g02", "name": "Geometry 02", "visible": False},
        ],
        "tilesets": [
            {
                "type": "vector",
                "layers": [
                    {"groupId": "ras-geometry-g01", "kind": "model_extents", "visible": False},
                    {"groupId": "ras-geometry-g01", "kind": "cross_sections", "visible": False},
                    {"groupId": "ras-geometry-g01", "kind": "structures", "visible": False},
                    {"groupId": "ras-geometry-g02", "kind": "model_extents", "visible": True},
                ],
            }
        ],
    }

    maplibre.apply_maplibre_default_visibility(
        manifest,
        primary_geometry_group_id="ras-geometry-g01",
        show_all_primary_geometry=True,
    )

    layers = manifest["tilesets"][0]["layers"]
    assert [layer["visible"] for layer in layers] == [True, True, True, False]
    assert manifest["groups"][0]["visible"] is True
    assert manifest["groups"][1]["visible"] is False


def test_default_visibility_prefers_computed_plan_geometry() -> None:
    manifest = {
        "groups": [
            {"id": "ras-geometry-g01", "name": "Geometry g01", "visible": True},
            {"id": "ras-geometry-g02", "name": "Geometry g02", "visible": False},
        ],
        "tilesets": [
            {
                "type": "vector",
                "layers": [
                    {"groupId": "ras-geometry-g01", "kind": "model_extents", "visible": True},
                    {"groupId": "ras-geometry-g01", "kind": "centerlines", "visible": True},
                    {"groupId": "ras-geometry-g02", "kind": "model_extents", "visible": False},
                    {"groupId": "ras-geometry-g02", "kind": "mesh_areas", "visible": False},
                ],
            }
        ],
    }
    archive = {
        "results": [
            {"plan_id": "p03", "geom_id": "g02", "variables": [{"variable": "Depth"}]}
        ]
    }

    maplibre.apply_maplibre_default_visibility(
        manifest,
        primary_geometry_group_id=maplibre._preferred_result_geometry_group_id(archive),
    )

    layers = manifest["tilesets"][0]["layers"]
    assert [layer["visible"] for layer in layers] == [False, False, True, True]
    assert manifest["groups"][0]["visible"] is False
    assert manifest["groups"][1]["visible"] is True


def test_tippecanoe_command_allows_a_host_wrapper(monkeypatch) -> None:
    monkeypatch.setenv("RAS2CNG_TIPPECANOE", r"C:\\tools\\tippecanoe.cmd")

    assert maplibre._tippecanoe_command() == r"C:\\tools\\tippecanoe.cmd"


def test_run_tippecanoe_converts_mbtiles_to_pmtiles(monkeypatch, tmp_path: Path) -> None:
    calls: list[list[str]] = []
    output = tmp_path / "tiles" / "geometry.pmtiles"

    def fake_run(command, **_kwargs):
        calls.append(command)
        if command[0] == "tippecanoe":
            Path(command[command.index("--output") + 1]).write_bytes(b"mbtiles")
        else:
            Path(command[-1]).write_bytes(b"pmtiles")

    monkeypatch.setattr(maplibre.subprocess, "run", fake_run)
    maplibre._run_tippecanoe(output, [("geometry", tmp_path / "geometry.ndgeojson")], 0, 17)

    assert calls[0][calls[0].index("--output") + 1] == str(output.with_suffix(".mbtiles"))
    assert calls[1] == ["pmtiles", "convert", str(output.with_suffix(".mbtiles")), str(output)]
    assert output.read_bytes() == b"pmtiles"
    assert not output.with_suffix(".mbtiles").exists()


def test_package_terrain_adds_default_queryable_raster(monkeypatch, tmp_path: Path) -> None:
    viewer_dir = tmp_path / "project" / "viewer"
    archive_dir = viewer_dir.parent / "archive" / "terrain"
    viewer_dir.mkdir(parents=True)
    archive_dir.mkdir(parents=True)
    cog = archive_dir / "terrain.cog.tif"
    cog.write_bytes(b"cog")
    archive_dir.parent.joinpath("manifest.json").write_text(
        json.dumps(
            {
                "geometry": [{"geom_id": "g01", "geom_title": "Muncie Geometry"}],
                "results": [],
            }
        ),
        encoding="utf-8",
    )
    viewer_dir.joinpath("manifest.json").write_text(
        json.dumps(
            {
                "tilesets": [
                    {
                        "id": "geometry",
                        "type": "vector",
                        "href": "tiles/geometry.pmtiles",
                        "layers": [
                            {
                                "id": "ras-geometry-g01-model-extents",
                                "name": "Model Extents",
                                "sourceLayer": "ras-geometry-g01-model-extents",
                                "groupId": "ras-geometry-g01",
                                "kind": "model_extents",
                                "visible": True,
                            }
                        ],
                    }
                ],
                "groups": [{"id": "ras-geometry-g01", "name": "Geometry g01"}],
            }
        ),
        encoding="utf-8",
    )
    source_info = {
        "bands": [{"minimum": 466.0, "maximum": 2542.0, "mean": 1436.0, "stdDev": 200.0}],
    }
    projected_info = {"geoTransform": [0.0, 1.5, 0.0, 0.0, 0.0, -1.5]}
    gdalinfo_calls = 0
    commands: list[list[str]] = []

    def fake_run(command, **_kwargs):
        nonlocal gdalinfo_calls
        commands.append(command)
        if command[0] == "gdalinfo":
            gdalinfo_calls += 1
            info = source_info if gdalinfo_calls == 1 else projected_info
            return type("Completed", (), {"stdout": json.dumps(info)})()
        if command[0] == "gdaldem":
            Path(command[-2]).write_bytes(b"raster")
        elif command[0] in {"gdalwarp", "gdal_translate"}:
            Path(command[-1]).write_bytes(b"raster")
        elif command[0] == "pmtiles":
            Path(command[-1]).write_bytes(b"pmtiles")
        return type("Completed", (), {"stdout": ""})()

    monkeypatch.setattr(maplibre, "_require_cli", lambda _: None)
    monkeypatch.setattr(
        maplibre,
        "_raster_source_metadata",
        lambda _: {
            "sourceCrs": "EPSG:2271",
            "sourceBounds": [0.0, 0.0, 100.0, 100.0],
            "bounds": [-85.0, 40.0, -84.99, 40.01],
            "dtype": "float32",
            "nodata": -9999.0,
        },
    )
    monkeypatch.setattr(maplibre.subprocess, "run", fake_run)

    summary = maplibre.package_maplibre_terrain(cog, viewer_dir, scratch_dir=tmp_path / "scratch")

    manifest = json.loads(summary.manifest_path.read_text(encoding="utf-8"))
    terrain = next(item for item in manifest["tilesets"] if item["id"] == "terrain")
    assert summary.max_zoom == 16
    assert terrain["id"] == "terrain"
    assert terrain["visible"] is True
    assert terrain["groupId"] == "ras-terrains"
    assert terrain["sourceCog"] == "../archive/terrain/terrain.cog.tif"
    assert terrain["queryable"] is True
    assert terrain["rasterStats"] == {
        "minimum": 466.0,
        "maximum": 2542.0,
        "mean": 1436.0,
        "stddev": 200.0,
    }
    assert manifest["groups"][-1] == {"id": "ras-terrains", "name": "Terrain", "visible": True}
    assert manifest["schema"] == "rascommander.maplibre/v2"
    assert manifest["resources"]["terrain-display"]["type"] == "raster-pmtiles"
    assert manifest["resources"]["terrain-numeric"]["type"] == "cog"
    assert manifest["resources"]["terrain-numeric"]["crs"] == "EPSG:2271"
    assert manifest["resources"]["terrain-numeric"]["bounds"] == [-85.0, 40.0, -84.99, 40.01]
    assert manifest["layers"]["terrain"]["query"]["numericResource"] == "terrain-numeric"
    assert manifest["legends"]["legend-terrain"]["preset"] == "rasmapper.terrain"
    assert manifest["legends"]["legend-terrain"]["domainPolicy"] == "fixed"
    geometry_root = next(root for root in manifest["tree"] if root["id"] == "geometries")
    assert geometry_root["children"][0]["name"] == "Geometry 01 - Muncie Geometry"
    assert manifest["layers"]["ras-geometry-g01-model-extents"]["geometryTitle"] == "Muncie Geometry"
    assert any(command[:2] == ["gdaldem", "color-relief"] for command in commands)
    warp = next(command for command in commands if command[0] == "gdalwarp")
    assert "-srcalpha" in warp
    assert "-dstalpha" in warp
    translate = next(command for command in commands if command[0] == "gdal_translate")
    assert "ZOOM_LEVEL_STRATEGY=LOWER" in translate


def test_terrain_color_ramp_makes_nodata_transparent(tmp_path: Path) -> None:
    ramp = tmp_path / "terrain-ramp.txt"

    maplibre._terrain_color_ramp({"minimum": 466.0, "maximum": 2542.0}, ramp)

    assert ramp.read_text(encoding="ascii").splitlines()[-1] == "nv 0 0 0 0"


def test_package_stored_map_adds_plan_raster_with_numeric_provenance(monkeypatch, tmp_path: Path) -> None:
    viewer_dir = tmp_path / "project" / "viewer"
    archive_dir = viewer_dir.parent / "archive" / "maps" / "p03"
    viewer_dir.mkdir(parents=True)
    archive_dir.mkdir(parents=True)
    cog = archive_dir / "depth-max.cog.tif"
    cog.write_bytes(b"numeric-cog")
    archive_root = viewer_dir.parent / "archive"
    archive_root.joinpath("manifest.json").write_text(
        json.dumps(
            {
                "geometry": [{"geom_id": "g04", "geom_title": "2D 50ft Grid"}],
                "results": [
                    {
                        "plan_id": "p03",
                        "plan_title": "Muncie 2D Unsteady",
                        "geom_id": "g04",
                        "completed": True,
                        "variables": [],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    viewer_dir.joinpath("manifest.json").write_text(
        json.dumps(
            {
                "tilesets": [],
                "groups": [{"id": "ras-geometry-g04", "name": "Geometry g04"}],
            }
        ),
        encoding="utf-8",
    )
    source_info = {
        "bands": [
            {
                "minimum": 0.01,
                "maximum": 18.5,
                "mean": 2.3,
                "stdDev": 1.8,
                "noDataValue": -9999.0,
                "type": "Float32",
            }
        ]
    }
    projected_info = {"geoTransform": [0.0, 2.0, 0.0, 0.0, 0.0, -2.0]}
    gdalinfo_calls = 0

    def fake_run(command, **_kwargs):
        nonlocal gdalinfo_calls
        if command[0] == "gdalinfo":
            gdalinfo_calls += 1
            return type(
                "Completed",
                (),
                {"stdout": json.dumps(source_info if gdalinfo_calls == 1 else projected_info)},
            )()
        if command[0] == "gdaldem":
            Path(command[-2]).write_bytes(b"raster")
        elif command[0] in {"gdalwarp", "gdal_translate"}:
            Path(command[-1]).write_bytes(b"raster")
        elif command[0] == "pmtiles":
            Path(command[-1]).write_bytes(b"pmtiles")
        return type("Completed", (), {"stdout": ""})()

    monkeypatch.setattr(maplibre, "_require_cli", lambda _: None)
    monkeypatch.setattr(
        maplibre,
        "_raster_source_metadata",
        lambda _: {
            "sourceCrs": "EPSG:2271",
            "sourceBounds": [0.0, 0.0, 100.0, 100.0],
            "bounds": [-85.0, 40.0, -84.99, 40.01],
            "dtype": "float32",
            "nodata": -9999.0,
        },
    )
    monkeypatch.setattr(maplibre.subprocess, "run", fake_run)

    summary = maplibre.package_maplibre_stored_map(
        cog,
        viewer_dir,
        plan="03",
        map_type="Depth",
        profile="Max",
        geometry="g04",
        scratch_dir=tmp_path / "scratch",
    )

    manifest = json.loads(summary.manifest_path.read_text(encoding="utf-8"))
    tileset = next(item for item in manifest["tilesets"] if item["id"] == "p03-depth-max")
    layer = manifest["layers"]["p03-depth-max"]
    result_root = next(root for root in manifest["tree"] if root["id"] == "results")
    plan_node = next(node for node in result_root["children"] if node["metadata"]["planId"] == "p03")
    raster_branch = next(node for node in plan_node["children"] if node["role"] == "published-raster-maps")

    assert summary.layer_id == "p03-depth-max"
    assert tileset["visible"] is False
    assert tileset["sourceKind"] == "stored-map"
    assert tileset["storedMap"]["source"] == "RASMapper/RasProcess Stored Map"
    assert tileset["storedMap"]["interpolationAuthority"] == "RASMapper/RasProcess"
    assert tileset["nodata"] == -9999.0
    assert layer["sourceKind"] == "stored-map"
    assert layer["plan"] == "p03"
    assert layer["geometry"] == "g04"
    assert layer["query"]["numericResource"] == "p03-depth-max-numeric"
    assert layer["provenance"]["profile"] == "Max"
    assert raster_branch["children"][0]["layerId"] == "p03-depth-max"
    assert plan_node["name"] == "P03 - Muncie 2D Unsteady"
    assert plan_node["metadata"]["geometryLabel"] == "Geometry 04 - 2D 50ft Grid"
    assert manifest["resources"]["p03-depth-max-numeric"]["type"] == "cog"


def test_result_color_ramp_makes_nodata_transparent(tmp_path: Path) -> None:
    ramp = tmp_path / "depth-ramp.txt"

    preset, colors = maplibre._result_color_ramp(
        {"minimum": 0.0, "maximum": 10.0},
        "Depth",
        ramp,
    )

    assert preset == "rasmapper.depth"
    assert len(colors) == 4
    assert ramp.read_text(encoding="ascii").splitlines()[-1] == "nv 0 0 0 0"


@pytest.mark.parametrize(
    ("map_type", "preset"),
    (
        ("Froude Number", "rascommander.froude"),
        ("Shear Stress", "rascommander.shear-stress"),
        ("Depth x Velocity", "rascommander.depth-velocity"),
        ("Depth x Velocity Squared", "rascommander.depth-velocity"),
        ("Arrival Time", "rascommander.arrival-time"),
        ("Duration", "rascommander.duration"),
        ("Percent Time Inundated", "rascommander.percent-inundated"),
    ),
)
def test_supported_stored_map_types_use_allowlisted_presets(
    tmp_path: Path,
    map_type: str,
    preset: str,
) -> None:
    ramp = tmp_path / f"{map_type}.txt"

    actual, _colors = maplibre._result_color_ramp(
        {"minimum": 0.0, "maximum": 10.0},
        map_type,
        ramp,
    )

    assert actual == preset
    assert ramp.read_text(encoding="ascii").splitlines()[-1] == "nv 0 0 0 0"


def test_numeric_raster_overwrite_preserves_service_identity(monkeypatch, tmp_path: Path) -> None:
    """A catalog-attached fixed layer can be promoted to current-view styling."""
    viewer_dir = tmp_path / "viewer"
    viewer_dir.mkdir()
    cog = tmp_path / "depth.cog.tif"
    cog.write_bytes(b"numeric-cog")
    viewer_dir.joinpath("manifest.json").write_text(
        json.dumps(
            {
                "schema": "rascommander.maplibre.project/2",
                "tilesets": [
                    {
                        "id": "result-p03-depth-max",
                        "type": "raster",
                        "serviceAsset": "muncie/result-p03-depth-max",
                        "serviceRevision": "sha256:abc123",
                    }
                ],
                "groups": [],
                "services": {
                    "numericRaster": {
                        "baseUrl": "/ras-raster",
                        "statisticsPath": "/stats",
                        "samplePath": "/sample",
                        "tilePath": "/tiles/{z}/{x}/{y}.png",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    viewer_dir.joinpath("tiles").mkdir()
    viewer_dir.joinpath("tiles", "result-p03-depth-max.pmtiles").write_bytes(b"old")

    source_info = {
        "bands": [{"minimum": 0.0, "maximum": 12.0, "noDataValue": -9999.0, "type": "Float32"}]
    }

    monkeypatch.setattr(maplibre, "_require_cli", lambda _: None)
    monkeypatch.setattr(maplibre, "_gdalinfo", lambda _: source_info)
    def fake_render(_source, output, *, ramp_writer, **_kwargs):
        ramp_writer(tmp_path / "ramp.txt")
        output.write_bytes(b"pmtiles")
        return 16

    monkeypatch.setattr(maplibre, "_render_raster_pmtiles", fake_render)
    monkeypatch.setattr(
        maplibre,
        "_raster_source_metadata",
        lambda _: {
            "sourceCrs": "EPSG:2965",
            "sourceBounds": [0.0, 0.0, 100.0, 100.0],
            "bounds": [-85.4, 40.1, -85.3, 40.2],
            "dtype": "float32",
            "nodata": -9999.0,
        },
    )
    summary = maplibre.package_maplibre_stored_map(
        cog,
        viewer_dir,
        plan="p03",
        map_type="Depth",
        layer_id="result-p03-depth-max",
        domain_policy="current-view",
        overwrite=True,
        scratch_dir=tmp_path / "scratch",
    )

    manifest = json.loads(summary.manifest_path.read_text(encoding="utf-8"))
    tileset = next(item for item in manifest["tilesets"] if item["id"] == summary.layer_id)
    assert tileset["serviceAsset"] == "muncie/result-p03-depth-max"
    assert tileset["serviceRevision"] == "sha256:abc123"
    assert manifest["layers"][summary.layer_id]["style"]["domainPolicy"] == "current-view"


def test_package_stored_vector_adds_rasmapper_result_to_plan(monkeypatch, tmp_path: Path) -> None:
    import geopandas as gpd
    from shapely.geometry import box

    viewer_dir = tmp_path / "viewer"
    viewer_dir.mkdir()
    viewer_dir.joinpath("manifest.json").write_text(
        json.dumps({"tilesets": [], "groups": []}),
        encoding="utf-8",
    )
    source = tmp_path / "inundation.parquet"
    gpd.GeoDataFrame(
        {"profile": ["Max"], "geometry": [box(500000, 500000, 501000, 501000)]},
        crs="EPSG:2965",
    ).to_parquet(source)

    monkeypatch.setattr(maplibre, "_require_cli", lambda _: None)

    def fake_tippecanoe(output, _layers, _min_zoom, _max_zoom, _temporary_directory):
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"pmtiles")

    monkeypatch.setattr(maplibre, "_run_tippecanoe", fake_tippecanoe)

    summary = maplibre.package_maplibre_stored_vector(
        source,
        viewer_dir,
        plan="03",
        map_type="Inundation Boundary",
        profile="Max",
        geometry="g02",
        layer_id="result-p03-inundation-boundary-max",
        scratch_dir=tmp_path / "scratch",
    )

    manifest = json.loads(summary.manifest_path.read_text(encoding="utf-8"))
    layer = manifest["layers"][summary.layer_id]
    plan = next(node for node in manifest["tree"][2]["children"] if node["metadata"]["planId"] == "p03")
    published = next(node for node in plan["children"] if node["role"] == "published-raster-maps")
    assert layer["sourceKind"] == "stored-map"
    assert layer["geometry"] == "g02"
    assert layer["provenance"]["source"] == "RASMapper/RasProcess Stored Map"
    assert published["children"][0]["layerId"] == summary.layer_id


def test_constant_raster_stats_and_calculated_presets_are_supported(tmp_path: Path) -> None:
    assert maplibre._raster_stats({"bands": [{"minimum": 0, "maximum": 0}]}) == {
        "minimum": 0.0,
        "maximum": 0.0,
    }
    ramp = tmp_path / "threshold-ramp.txt"
    preset, _colors = maplibre._result_color_ramp(
        {"minimum": 0.0, "maximum": 0.0},
        "inundation_threshold",
        ramp,
    )
    assert preset == "rascommander.threshold"
    values = [float(line.split()[0]) for line in ramp.read_text().splitlines()[:-1]]
    assert values[0] < values[-1]


def test_terrain_commands_allow_worker_wrappers(monkeypatch) -> None:
    monkeypatch.setenv("RAS2CNG_GDALWARP", "/opt/ras2cng/bin/gdalwarp")
    monkeypatch.setenv("RAS2CNG_GDAL_THREADS", "2")

    assert maplibre._gdalwarp_command() == "/opt/ras2cng/bin/gdalwarp"
    assert maplibre._gdal_thread_count() == "2"


def test_package_requires_a_geometry_hdf_for_every_archive_geometry(tmp_path: Path):
    archive_dir, _ = _write_archive(tmp_path)

    try:
        maplibre.package_maplibre_viewer(archive_dir, tmp_path / "viewer", geometry_hdfs={})
    except ValueError as error:
        assert "Missing geometry HDF mapping" in str(error)
    else:
        raise AssertionError("Expected API footprint mapping validation to fail")


def test_geometry_only_package_streams_dense_mesh_delivery(monkeypatch, tmp_path: Path):
    archive_dir, hdf = _write_archive(tmp_path)
    calls: list[tuple[Path, list[tuple[str, Path]], Path]] = []
    sources: dict[str, str] = {}

    def fake_tippecanoe(output: Path, layers, min_zoom: int, max_zoom: int, temporary_directory: Path):
        calls.append((output, list(layers), temporary_directory))
        for source_layer, source_path in layers:
            sources[source_layer] = source_path.read_text(encoding="utf-8")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"pmtiles")

    footprint = gpd.GeoDataFrame(geometry=[box(-85.01, 39.99, -84.98, 40.02)], crs="EPSG:4326")
    monkeypatch.setattr(maplibre, "_require_cli", lambda _: None)
    monkeypatch.setattr(maplibre, "_extent_from_hdf", lambda *_: footprint)
    monkeypatch.setattr(maplibre, "_run_tippecanoe", fake_tippecanoe)

    maplibre.package_maplibre_viewer(
        archive_dir,
        tmp_path / "viewer",
        geometry_hdfs={"g01": hdf},
        scratch_dir=tmp_path / "scratch",
    )

    detail_call = next(call for call in calls if call[0].name == "geometry-detail.pmtiles")
    delivery = sources[detail_call[1][0][0]]
    assert '"cell_id":7.0' in delivery
    assert "hilbert_index" not in delivery
    assert detail_call[2].is_relative_to(tmp_path / "scratch")


def test_package_includes_terrain_modification_vectors(monkeypatch, tmp_path: Path):
    archive_dir, hdf = _write_archive(tmp_path)
    modification_dir = archive_dir / "terrain" / "modifications" / "existing"
    modification_dir.mkdir(parents=True)
    modification_path = modification_dir / "terrain_modification_lines.parquet"
    gpd.GeoDataFrame(
        {
            "name": ["Channel Cut"],
            "modification_mode": ["take_lower"],
        },
        geometry=[LineString([(-85.0, 40.0), (-84.99, 40.01)])],
        crs="EPSG:4326",
    ).to_parquet(modification_path)
    archive_manifest_path = archive_dir / "manifest.json"
    archive_manifest = json.loads(archive_manifest_path.read_text(encoding="utf-8"))
    archive_manifest["terrain_modifications"] = [
        {
            "terrain_name": "Existing",
            "source_hdf": r"H:\models\Terrain.hdf",
            "layers": [
                {
                    "layer": "terrain_modification_lines",
                    "parquet": modification_path.relative_to(archive_dir).as_posix(),
                }
            ],
        }
    ]
    archive_manifest_path.write_text(json.dumps(archive_manifest), encoding="utf-8")

    def fake_tippecanoe(output: Path, layers, *_args):
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"pmtiles")

    footprint = gpd.GeoDataFrame(geometry=[box(-85.01, 39.99, -84.98, 40.02)], crs="EPSG:4326")
    monkeypatch.setattr(maplibre, "_require_cli", lambda _: None)
    monkeypatch.setattr(maplibre, "_extent_from_hdf", lambda *_: footprint)
    monkeypatch.setattr(maplibre, "_run_tippecanoe", fake_tippecanoe)

    summary = maplibre.package_maplibre_viewer(
        archive_dir,
        tmp_path / "viewer",
        geometry_hdfs={"g01": hdf},
    )

    manifest = json.loads(summary.manifest_path.read_text(encoding="utf-8"))
    layer_id = "ras-terrain-modifications-existing-terrain-modification-lines"
    assert manifest["layers"][layer_id]["sourceKind"] == "terrain-modification"
    assert manifest["layers"][layer_id]["provenance"]["sourceHdf"] == "Terrain.hdf"
    terrain_root = next(root for root in manifest["tree"] if root["id"] == "terrains")
    modification_group = next(
        node for node in terrain_root["children"] if node["role"] == "terrain-modifications"
    )
    assert modification_group["children"][0]["layerId"] == layer_id


def test_package_includes_terrain_source_footprints(monkeypatch, tmp_path: Path):
    archive_dir, hdf = _write_archive(tmp_path)
    source_dir = archive_dir / "terrain" / "sources" / "existing"
    source_dir.mkdir(parents=True)
    source_path = source_dir / "terrain_source_footprints.parquet"
    gpd.GeoDataFrame(
        {
            "source_file": ["tile-01.tif"],
            "priority": [0],
            "resolution_x": [5.0],
            "resolution_y": [5.0],
        },
        geometry=[box(-85.0, 40.0, -84.99, 40.01)],
        crs="EPSG:4326",
    ).to_parquet(source_path)
    archive_manifest_path = archive_dir / "manifest.json"
    archive_manifest = json.loads(archive_manifest_path.read_text(encoding="utf-8"))
    archive_manifest["terrain_sources"] = [
        {
            "terrain_name": "Existing",
            "layers": [
                {
                    "layer": "terrain_source_footprints",
                    "parquet": source_path.relative_to(archive_dir).as_posix(),
                }
            ],
        }
    ]
    archive_manifest_path.write_text(json.dumps(archive_manifest), encoding="utf-8")

    def fake_tippecanoe(output: Path, layers, *_args):
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"pmtiles")

    footprint = gpd.GeoDataFrame(geometry=[box(-85.01, 39.99, -84.98, 40.02)], crs="EPSG:4326")
    monkeypatch.setattr(maplibre, "_require_cli", lambda _: None)
    monkeypatch.setattr(maplibre, "_extent_from_hdf", lambda *_: footprint)
    monkeypatch.setattr(maplibre, "_run_tippecanoe", fake_tippecanoe)

    summary = maplibre.package_maplibre_viewer(
        archive_dir,
        tmp_path / "viewer",
        geometry_hdfs={"g01": hdf},
    )

    manifest = json.loads(summary.manifest_path.read_text(encoding="utf-8"))
    layer_id = "ras-terrain-sources-existing-terrain-source-footprints"
    assert manifest["layers"][layer_id]["sourceKind"] == "terrain-source"
    terrain_root = next(root for root in manifest["tree"] if root["id"] == "terrains")
    source_group = next(node for node in terrain_root["children"] if node["role"] == "terrain-sources")
    assert source_group["children"][0]["layerId"] == layer_id


def test_package_calculated_map_uses_recipe_provenance_and_tree(monkeypatch, tmp_path: Path):
    viewer_dir = tmp_path / "viewer"
    viewer_dir.mkdir()
    (viewer_dir / "manifest.json").write_text(
        json.dumps(
            {
                "schema": "rascommander.maplibre.project/1",
                "sourceProject": "Example",
                "sourceCrs": "EPSG:4326",
                "tilesets": [],
                "groups": [],
            }
        ),
        encoding="utf-8",
    )
    cog = tmp_path / "hazard.tif"
    cog.write_bytes(b"cog")
    cog.with_suffix(".provenance.json").write_text(
        json.dumps(
            {
                "recipe": {"recipe_id": "hazard_class"},
                "profile": "01JAN2026 12:00:00",
                "parameters": {"standard": "aidr-2017"},
                "inputs": {"depth": {"file": "depth.tif"}},
                "output": {"units": "H1-H6"},
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(maplibre, "_require_cli", lambda *_: None)
    monkeypatch.setattr(maplibre, "_gdalinfo", lambda *_: {"bands": [{"minimum": 1, "maximum": 6}]})
    monkeypatch.setattr(
        maplibre,
        "_raster_source_metadata",
        lambda *_: {"sourceCrs": "EPSG:4326", "bounds": [-85, 40, -84.9, 40.1], "dtype": "uint8", "nodata": 0},
    )

    def fake_render(_source, output, *, ramp_writer, **_kwargs):
        ramp_writer(tmp_path / "ramp.txt")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"pmtiles")
        return 14

    monkeypatch.setattr(maplibre, "_render_raster_pmtiles", fake_render)

    summary = maplibre.package_maplibre_calculated_map(
        cog,
        viewer_dir,
        plan="p03",
        recipe_id="hazard_class",
        geometry="g03",
    )

    manifest = json.loads(summary.manifest_path.read_text(encoding="utf-8"))
    layer = manifest["layers"]["p03-hazard-class-01jan2026-12-00-00"]
    assert layer["sourceKind"] == "calculated"
    assert layer["query"]["valueSemantics"] == "calculated-from-rasmapper-rasters"
    assert layer["provenance"]["recipeId"] == "hazard_class"
    legend = manifest["legends"][layer["style"]["legendRef"]]
    assert legend["type"] == "categorical"
    assert legend["preset"] == "rascommander.hazard-aidr-2017"
    assert [category["label"] for category in legend["categories"]] == [
        "H1",
        "H2",
        "H3",
        "H4",
        "H5",
        "H6",
    ]
    plan_group = next(node for node in manifest["tree"][2]["children"] if node["id"] == "plan-p03")
    calculated = next(node for node in plan_group["children"] if node["role"] == "calculated-layers")
    assert calculated["children"][0]["layerId"] == summary.layer_id


def test_package_splits_steady_cross_section_results_by_profile(monkeypatch, tmp_path: Path):
    archive_dir, hdf = _write_archive(tmp_path)
    geometry = gpd.GeoDataFrame(
        {
            "layer": ["cross_sections", "cross_sections"],
            "River": ["River A", "River A"],
            "Reach": ["Reach A", "Reach A"],
            "RS": ["1000", "900"],
        },
        geometry=[
            LineString([(-85.0, 40.0), (-84.99, 40.0)]),
            LineString([(-85.0, 40.01), (-84.99, 40.01)]),
        ],
        crs="EPSG:4326",
    )
    geometry.to_parquet(archive_dir / "model.g01.parquet")
    raw_results = pd.DataFrame(
        {
            "river": ["River A"] * 4,
            "reach": ["Reach A"] * 4,
            "node_id": ["1000", "900", "1000", "900"],
            "profile": ["10-percent AEP", "10-percent AEP", "1-percent AEP", "1-percent AEP"],
            "wsel": [101.0, 100.5, 102.0, 101.5],
            "flow": [1000.0, 1000.0, 1500.0, 1500.0],
        }
    )
    raw_results.to_parquet(archive_dir / "results.p01.steady_cross_sections.parquet")
    manifest_path = archive_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["geometry"][0]["layers"] = [{"layer": "cross_sections", "filter_value": "cross_sections"}]
    manifest["results"] = [
        {
            "plan_id": "p01",
            "geom_id": "g01",
            "variables": [
                {
                    "variable": "steady_cross_sections",
                    "parquet": "results.p01.steady_cross_sections.parquet",
                    "geometry_filter": "cross_sections",
                    "join_columns": {"River": "river", "Reach": "reach", "RS": "node_id"},
                    "profile_column": "profile",
                    "source": "Raw HEC-RAS HDF steady cross-section result values",
                }
            ],
        }
    ]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    sources: dict[str, str] = {}

    def fake_tippecanoe(output: Path, layers, min_zoom: int, max_zoom: int, temporary_directory: Path):
        for source_layer, source_path in layers:
            sources[source_layer] = source_path.read_text(encoding="utf-8")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"pmtiles")

    footprint = gpd.GeoDataFrame(geometry=[box(-85.01, 39.99, -84.98, 40.02)], crs="EPSG:4326")
    monkeypatch.setattr(maplibre, "_require_cli", lambda _: None)
    monkeypatch.setattr(maplibre, "_extent_from_hdf", lambda *_: footprint)
    monkeypatch.setattr(maplibre, "_run_tippecanoe", fake_tippecanoe)

    summary = maplibre.package_maplibre_viewer(
        archive_dir,
        tmp_path / "viewer",
        geometry_hdfs={"g01": hdf},
        include_vector_results=True,
        scratch_dir=tmp_path / "scratch",
    )

    viewer_manifest = json.loads(summary.manifest_path.read_text(encoding="utf-8"))
    result_tiles = next(tileset for tileset in viewer_manifest["tilesets"] if tileset["id"] == "results")
    assert summary.result_layer_count == 2
    assert [layer["rawResult"]["profile"] for layer in result_tiles["layers"]] == [
        "10-percent AEP",
        "1-percent AEP",
    ]
    assert all(layer["rawResult"]["joinColumns"]["RS"] == "node_id" for layer in result_tiles["layers"])
    result_sources = [source for name, source in sources.items() if "steady-cross-sections" in name]
    assert len(result_sources) == 2
    assert all(len([json.loads(line) for line in source.splitlines()]) == 2 for source in result_sources)
    assert all('"river"' not in source and '"node_id"' not in source for source in result_sources)


def test_wgs84_conversion_accepts_a_verified_fallback_crs():
    unknown_crs = gpd.GeoDataFrame(geometry=[box(-85.0, 40.0, -84.9, 40.1)])

    converted = maplibre._to_wgs84(unknown_crs, Path("model.g01.hdf"), "EPSG:4326")

    assert converted.crs.to_epsg() == 4326


def test_wgs84_conversion_drops_delivery_only_z_coordinates():
    three_dimensional = gpd.GeoDataFrame(geometry=[Point(-85.0, 40.0, 4.0)], crs="EPSG:4326")

    converted = maplibre._to_wgs84(three_dimensional, Path("model.g01.hdf"))

    assert converted.geometry.iloc[0].has_z is False


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
            "--primary-geometry",
            "g02",
            "--all-primary-geometry",
            "--scratch-dir",
            str(tmp_path / "scratch"),
        ],
    )

    assert result.exit_code == 0, result.output
    assert received["geometry_hdfs"] == {"g01": Path("model.g01.hdf")}
    assert received["include_vector_results"] is True
    assert received["primary_geometry"] == "g02"
    assert received["show_all_primary_geometry"] is True
    assert received["scratch_dir"] == tmp_path / "scratch"


def test_stored_map_cli_passes_plan_provenance(monkeypatch, tmp_path: Path):
    received = {}

    def fake_package(cog_path, viewer_dir, **kwargs):
        received["cog_path"] = cog_path
        received["viewer_dir"] = viewer_dir
        received.update(kwargs)
        return maplibre.RasterPackageSummary(
            manifest_path=viewer_dir / "manifest.json",
            pmtiles_path=viewer_dir / "tiles" / "p04-velocity-max.pmtiles",
            source_cog=cog_path,
            raster_stats={"minimum": 0.0, "maximum": 8.0},
            max_zoom=15,
            layer_id="p04-velocity-max",
        )

    monkeypatch.setattr(maplibre, "package_maplibre_stored_map", fake_package)
    result = CliRunner().invoke(
        app,
        [
            "maplibre-stored-map",
            str(tmp_path / "velocity.tif"),
            str(tmp_path / "viewer"),
            "--plan", "p04",
            "--map-type", "Velocity",
            "--profile", "Max",
            "--geometry", "g02",
            "--domain-policy", "current-view",
            "--scratch-dir", str(tmp_path / "scratch"),
        ],
    )

    assert result.exit_code == 0, result.output
    assert received["plan"] == "p04"
    assert received["map_type"] == "Velocity"
    assert received["profile"] == "Max"
    assert received["geometry"] == "g02"
    assert received["domain_policy"] == "current-view"
