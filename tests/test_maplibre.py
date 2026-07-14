from __future__ import annotations

import json
from pathlib import Path

import geopandas as gpd
import pandas as pd
from shapely.geometry import LineString, Point, box
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
    geometry_tiles = [tileset for tileset in manifest["tilesets"] if tileset["id"].startswith("geometry")]
    layers = {layer["kind"]: layer for tileset in geometry_tiles for layer in tileset["layers"]}
    assert layers["model_extents"]["visible"] is True
    assert layers["mesh_cells"]["visible"] is True
    assert layers["centerlines"]["visible"] is False
    assert layers["model_extents"]["extentSource"].startswith("HdfProject.get_project_extent")
    assert manifest["groups"][0] == {"id": "ras-geometry-g01", "name": "Geometry g01", "visible": True}
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
    viewer_dir.joinpath("manifest.json").write_text(
        json.dumps({"tilesets": [], "groups": [{"id": "ras-geometry-g01", "name": "Geometry g01"}]}),
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
    monkeypatch.setattr(maplibre.subprocess, "run", fake_run)

    summary = maplibre.package_maplibre_terrain(cog, viewer_dir, scratch_dir=tmp_path / "scratch")

    manifest = json.loads(summary.manifest_path.read_text(encoding="utf-8"))
    terrain = manifest["tilesets"][0]
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
    assert any(command[:2] == ["gdaldem", "color-relief"] for command in commands)
    translate = next(command for command in commands if command[0] == "gdal_translate")
    assert "ZOOM_LEVEL_STRATEGY=LOWER" in translate


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
            "--scratch-dir",
            str(tmp_path / "scratch"),
        ],
    )

    assert result.exit_code == 0, result.output
    assert received["geometry_hdfs"] == {"g01": Path("model.g01.hdf")}
    assert received["include_vector_results"] is True
    assert received["scratch_dir"] == tmp_path / "scratch"
