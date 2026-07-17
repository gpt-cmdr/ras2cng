from __future__ import annotations

from copy import deepcopy
import json

import pytest
from typer.testing import CliRunner

from ras2cng.cli import app
from ras2cng.publication import _require_http_range, validate_example_publication
from ras2cng.viewer_manifest import apply_manifest_v2


def _valid_bundle():
    bounds = [-85.01, 39.99, -84.98, 40.02]
    manifest = {
        "sourceCrs": "EPSG:26916",
        "sourceProject": "/data/rasexamples/example/project.json",
        "groups": [
            {"id": "ras-geometry-g01", "name": "Geometry g01", "visible": True},
            {"id": "ras-results-p01", "name": "Plan p01", "visible": False},
        ],
        "tilesets": [
            {
                "id": "geometry",
                "type": "vector",
                "href": "https://rascommander.info/data/example/geometry.pmtiles",
                "layers": [
                    {
                        "id": "g01-model-extents",
                        "name": "Model Extents",
                        "sourceLayer": "g01-model-extents",
                        "groupId": "ras-geometry-g01",
                        "kind": "model_extents",
                        "visible": True,
                        "bounds": bounds,
                    },
                    {
                        "id": "g01-mesh-cells",
                        "name": "2D Mesh Cells",
                        "sourceLayer": "g01-mesh-cells",
                        "groupId": "ras-geometry-g01",
                        "kind": "mesh_cells",
                        "visible": True,
                        "bounds": bounds,
                    },
                ],
            },
            {
                "id": "results",
                "type": "vector",
                "href": "https://rascommander.info/data/example/results.pmtiles",
                "resultKind": "raw_hdf",
                "layers": [
                    {
                        "id": "p01-face-velocity",
                        "name": "Face Velocity",
                        "sourceLayer": "p01-face-velocity",
                        "groupId": "ras-results-p01",
                        "kind": "face_velocity",
                        "visible": False,
                        "queryable": True,
                        "rawResult": {
                            "plan": "p01",
                            "source": "Raw HEC-RAS HDF summary result values",
                        },
                    }
                ],
            },
            {
                "id": "terrain",
                "name": "Terrain",
                "type": "raster",
                "href": "https://rascommander.info/data/example/terrain.pmtiles",
                "sourceCog": "https://rascommander.info/data/example/terrain.cog.tif",
                "sourceCrs": "EPSG:26916",
                "sourceBounds": [100.0, 200.0, 300.0, 400.0],
                "bounds": bounds,
                "groupId": "ras-terrains",
                "sourceKind": "terrain",
                "visible": True,
                "queryable": True,
                "rasterStats": {"minimum": 100.0, "maximum": 200.0},
                "legend": {"type": "continuous", "preset": "rasmapper.terrain"},
                "storedMap": {"mapType": "terrain", "source": "HEC-RAS terrain GeoTIFF"},
            },
            {
                "id": "p01-depth-max",
                "name": "Depth Max",
                "type": "raster",
                "href": "https://rascommander.info/data/example/p01-depth-max.pmtiles",
                "sourceCog": "https://rascommander.info/data/example/p01-depth-max.cog.tif",
                "sourceCrs": "EPSG:26916",
                "sourceBounds": [100.0, 200.0, 300.0, 400.0],
                "bounds": bounds,
                "groupId": "ras-results-p01",
                "sourceKind": "stored-map",
                "visible": False,
                "queryable": True,
                "rasterStats": {"minimum": 0.01, "maximum": 12.0},
                "legend": {"type": "continuous", "preset": "rasmapper.depth"},
                "storedMap": {
                    "mapType": "Depth",
                    "plan": "p01",
                    "geometry": "g01",
                    "source": "RASMapper/RasProcess Stored Map",
                    "interpolationAuthority": "RASMapper/RasProcess",
                },
            },
        ],
    }
    depth_tileset = manifest["tilesets"][-1]
    for slug, map_type, preset in (
        ("wse", "WSE", "rasmapper.wse"),
        ("velocity", "Velocity", "rasmapper.velocity"),
        ("froude", "Froude Number", "rasmapper.froude"),
        ("shear-stress", "Shear Stress", "rasmapper.shear-stress"),
        ("depth-x-velocity", "Depth x Velocity", "rasmapper.depth-x-velocity"),
        (
            "depth-x-velocity-sq",
            "Depth x Velocity Squared",
            "rasmapper.depth-x-velocity-sq",
        ),
        ("arrival-time", "Arrival Time", "rasmapper.arrival-time"),
        ("duration", "Duration", "rasmapper.duration"),
        (
            "percent-inundated",
            "Percent Time Inundated",
            "rasmapper.percent-inundated",
        ),
    ):
        tileset = deepcopy(depth_tileset)
        tileset.update(
            {
                "id": f"p01-{slug}-max",
                "name": f"{map_type} Max",
                "href": f"https://rascommander.info/data/example/p01-{slug}-max.pmtiles",
                "sourceCog": f"https://rascommander.info/data/example/p01-{slug}-max.cog.tif",
            }
        )
        tileset["legend"]["preset"] = preset
        tileset["storedMap"]["mapType"] = map_type
        manifest["tilesets"].append(tileset)
    manifest["tilesets"].append(
        {
            "id": "p01-inundation-boundary-max",
            "type": "vector",
            "href": "https://rascommander.info/data/example/p01-inundation-boundary-max.pmtiles",
            "groupId": "ras-results-p01",
            "resultKind": "stored_map",
            "layers": [
                {
                    "id": "p01-inundation-boundary-max",
                    "name": "Inundation Boundary (Max)",
                    "sourceLayer": "p01-inundation-boundary-max",
                    "groupId": "ras-results-p01",
                    "geometryId": "g01",
                    "kind": "inundation_boundary",
                    "sourceKind": "stored-map",
                    "visible": False,
                    "queryable": True,
                    "provenance": {
                        "source": "RASMapper/RasProcess Stored Map",
                        "interpolationAuthority": "RASMapper/RasProcess",
                        "mapType": "Inundation Boundary",
                        "plan": "p01",
                    },
                }
            ],
        }
    )
    apply_manifest_v2(manifest)
    for layer in manifest["layers"].values():
        if layer.get("sourceKind") in {"raw-hdf", "stored-map"}:
            layer["planTitle"] = "Existing Conditions"
            layer["geometryTitle"] = "Main Channel Geometry"
    manifest["services"] = {
        "numericRaster": {
            "baseUrl": "/ras-raster",
            "statisticsPath": "/stats",
            "samplePath": "/sample",
            "tilePath": "/tiles/{z}/{x}/{y}.png",
        }
    }
    for layer_id, layer in manifest["layers"].items():
        numeric_id = layer.get("query", {}).get("numericResource")
        if not numeric_id:
            continue
        numeric = manifest["resources"][numeric_id]
        numeric["serviceAsset"] = f"example/{layer_id}"
        numeric["serviceRevision"] = f"revision-{layer_id}"
        tileset = next(item for item in manifest["tilesets"] if item["id"] == layer_id)
        tileset["serviceAsset"] = numeric["serviceAsset"]
        tileset["serviceRevision"] = numeric["serviceRevision"]
    archive = {
        "results": [
            {"plan_id": "p01", "geom_id": "g01", "completed": True, "variables": [{}]}
        ]
    }
    return manifest, archive


def test_valid_example_publication_passes_gate():
    manifest, archive = _valid_bundle()

    report = validate_example_publication(manifest, archive, check_files=False)

    assert report.ok, report.to_dict()
    assert report.counts["raw_results"] == 1
    assert report.counts["stored_maps"] == 11
    assert report.counts["terrains"] == 1


def test_publication_gate_accepts_queryable_vector_stored_map():
    manifest, archive = _valid_bundle()

    report = validate_example_publication(manifest, archive, check_files=False)

    assert report.ok, report.to_dict()
    assert manifest["layers"]["p01-inundation-boundary-max"]["query"]["enabled"] is True
    assert report.counts["stored_maps"] == 11


def test_publication_gate_rejects_missing_result_families():
    manifest, archive = _valid_bundle()
    manifest["layers"] = {
        layer_id: layer
        for layer_id, layer in manifest["layers"].items()
        if layer.get("sourceKind") not in {"raw-hdf", "stored-map"}
    }
    # Rebuild from compatibility fields with both result tilesets removed.
    manifest["tilesets"] = [
        item
        for item in manifest["tilesets"]
        if item.get("resultKind") not in {"raw_hdf", "stored_map"}
        and item.get("sourceKind") != "stored-map"
    ]
    apply_manifest_v2(manifest)

    report = validate_example_publication(manifest, archive, check_files=False)

    codes = {issue.code for issue in report.errors}
    assert "results.plan" in codes


def test_publication_gate_rejects_2d_project_without_terrain():
    manifest, archive = _valid_bundle()
    manifest["tilesets"] = [item for item in manifest["tilesets"] if item["id"] != "terrain"]
    apply_manifest_v2(manifest)

    report = validate_example_publication(manifest, archive, check_files=False)

    assert any(issue.code == "terrain.required" for issue in report.errors)


def test_publication_gate_rejects_uncomputed_plan():
    manifest, archive = _valid_bundle()
    archive["results"][0]["completed"] = False

    report = validate_example_publication(manifest, archive, check_files=False)

    assert any(issue.code == "results.completed" for issue in report.errors)


def test_publication_gate_requires_both_result_families_for_every_completed_plan():
    manifest, archive = _valid_bundle()
    archive["results"].append(
        {"plan_id": "p02", "geom_id": "g01", "completed": True, "variables": [{}]}
    )

    report = validate_example_publication(manifest, archive, check_files=False)

    assert any(
        issue.code == "results.raw-hdf" and issue.context == "p02"
        for issue in report.errors
    )
    assert any(
        issue.code == "results.stored-map" and issue.context == "p02"
        for issue in report.errors
    )
    assert report.counts["plans"] == 1
    assert report.counts["completed_plans"] == 2


def test_publication_gate_requires_every_stored_map_type():
    manifest, archive = _valid_bundle()
    froude_layer = manifest["layers"]["p01-froude-max"]
    froude_layer["provenance"]["mapType"] = "Unsupported"
    froude_layer["role"] = "unsupported"

    report = validate_example_publication(manifest, archive, check_files=False)

    assert any(
        issue.code == "results.stored-map-type"
        and issue.context == "p01"
        and "froude" in issue.message
        for issue in report.errors
    )


def test_publication_gate_requires_result_titles_and_hidden_defaults():
    manifest, archive = _valid_bundle()
    layer = manifest["layers"]["p01-depth-max"]
    layer.pop("planTitle")
    layer["visible"] = True

    report = validate_example_publication(manifest, archive, check_files=False)

    assert any(
        issue.code == "results.metadata" and issue.context == "p01-depth-max"
        for issue in report.errors
    )
    assert any(
        issue.code == "defaults.results" and issue.context == "p01-depth-max"
        for issue in report.errors
    )


def test_publication_gate_requires_one_default_geometry():
    manifest, archive = _valid_bundle()
    for layer in manifest["layers"].values():
        if layer.get("sourceKind") == "geometry":
            layer["visible"] = False

    report = validate_example_publication(manifest, archive, check_files=False)

    assert any(issue.code == "defaults.geometry" for issue in report.errors)


def test_publication_gate_requires_every_joinable_raw_result_variable():
    manifest, archive = _valid_bundle()
    archive["results"][0]["variables"] = [
        {
            "variable": "sa2d_structure_summary",
            "parquet": "results/p01/sa2d_structure_summary.parquet",
            "rows": 1,
            "geometry_filter": "structures",
            "join_columns": {"Connection": "structure_name"},
        },
        {
            "variable": "empty_result",
            "rows": 0,
            "geometry_filter": "mesh_cells",
            "index_column": "cell_index",
        },
        {"variable": "not_joinable", "rows": 1},
    ]

    report = validate_example_publication(manifest, archive, check_files=False)

    assert any(
        issue.code == "results.raw-variable"
        and issue.context == "p01:sa2d_structure_summary"
        for issue in report.errors
    )
    assert not any("empty_result" in issue.context for issue in report.errors)
    assert not any("not_joinable" in issue.context for issue in report.errors)

    manifest["layers"]["p01-face-velocity"]["provenance"]["variable"] = (
        "SA2D_STRUCTURE_SUMMARY"
    )
    report = validate_example_publication(manifest, archive, check_files=False)

    assert not any(issue.code == "results.raw-variable" for issue in report.errors)


def test_publication_gate_accepts_pure_1d_plan_without_terrain_stored_maps():
    manifest, archive = _valid_bundle()
    geometry = next(item for item in manifest["tilesets"] if item["id"] == "geometry")
    geometry["layers"] = [
        layer for layer in geometry["layers"] if layer["kind"] == "model_extents"
    ]
    geometry["layers"].append(
        {
            "id": "g01-river-reaches",
            "name": "River Reaches",
            "sourceLayer": "g01-river-reaches",
            "groupId": "ras-geometry-g01",
            "kind": "river_reaches",
            "visible": True,
            "bounds": [-85.01, 39.99, -84.98, 40.02],
        }
    )
    manifest["tilesets"] = [
        item
        for item in manifest["tilesets"]
        if item["id"] != "terrain"
        and item.get("resultKind") != "stored_map"
        and item.get("sourceKind") != "stored-map"
    ]
    archive["geometry"] = [
        {
            "geom_id": "g01",
            "layers": [{"layer": "river_reaches"}, {"layer": "cross_sections"}],
        }
    ]
    apply_manifest_v2(manifest, archive=archive)

    report = validate_example_publication(manifest, archive, check_files=False)

    assert report.ok, report.to_dict()
    assert report.counts["stored_map_exempt_plans"] == 1
    assert any(
        issue.code == "results.stored-map-not-applicable" and issue.context == "p01"
        for issue in report.warnings
    )
    assert manifest["capabilities"]["terrain"] == {
        "applicable": False,
        "published": False,
        "reason": "pure-1d-source-without-project-terrain",
    }
    assert manifest["capabilities"]["plans"]["p01"]["storedMaps"] == {
        "applicable": False,
        "published": False,
        "reason": "pure-1d-source-without-project-terrain",
    }


def test_publication_gate_rejects_raster_outside_model_extent():
    manifest, archive = _valid_bundle()
    manifest["resources"]["p01-depth-max-numeric"]["bounds"] = [-100.0, 30.0, -99.0, 31.0]

    report = validate_example_publication(manifest, archive, check_files=False)

    assert any(
        issue.code == "raster.location" and issue.context == "p01-depth-max"
        for issue in report.errors
    )


def test_publication_gate_rejects_local_paths():
    manifest, archive = _valid_bundle()
    manifest["resources"]["geometry"]["href"] = r"H:\private\geometry.pmtiles"

    report = validate_example_publication(manifest, archive, check_files=False)

    assert any(issue.code in {"resource.local-path", "manifest.local-path"} for issue in report.errors)


def test_publication_gate_rejects_unserved_extent_color_layer():
    manifest, archive = _valid_bundle()
    manifest.pop("services")
    numeric = manifest["resources"]["p01-depth-max-numeric"]
    numeric.pop("serviceAsset")
    numeric.pop("serviceRevision")
    manifest["layers"]["p01-depth-max"]["style"]["domainPolicy"] = "current-view"

    report = validate_example_publication(manifest, archive, check_files=False)

    codes = {issue.code for issue in report.errors}
    assert "manifest.v2" in codes
    assert "raster.extent-color-asset" in codes
    assert "raster.extent-color-service" in codes


def test_publication_gate_requires_extent_color_service_for_fixed_continuous_raster():
    manifest, archive = _valid_bundle()
    numeric = manifest["resources"]["terrain-numeric"]
    numeric.pop("serviceAsset")
    numeric.pop("serviceRevision")

    report = validate_example_publication(manifest, archive, check_files=False)

    assert any(
        issue.code == "raster.extent-color-asset" and issue.context == "terrain"
        for issue in report.errors
    )


def test_publication_gate_exempts_fixed_categorical_raster_from_extent_color_service():
    manifest, archive = _valid_bundle()
    manifest["legends"]["legend-terrain"]["type"] = "categorical"
    numeric = manifest["resources"]["terrain-numeric"]
    numeric.pop("serviceAsset")
    numeric.pop("serviceRevision")

    report = validate_example_publication(manifest, archive, check_files=False)

    assert not any(
        issue.code.startswith("raster.extent-color-") and issue.context == "terrain"
        for issue in report.errors
    )


def test_publication_gate_requires_extent_color_style_preset():
    manifest, archive = _valid_bundle()
    manifest["legends"]["legend-terrain"].pop("preset")

    report = validate_example_publication(manifest, archive, check_files=False)

    assert any(
        issue.code == "raster.extent-color-preset" and issue.context == "terrain"
        for issue in report.errors
    )


def test_http_range_probe_requires_partial_content(monkeypatch):
    class Response:
        status = 200
        headers = {}

        def getcode(self):
            return self.status

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr("ras2cng.publication.urlopen", lambda *_args, **_kwargs: Response())

    with pytest.raises(ValueError, match="does not support HTTP byte ranges"):
        _require_http_range("https://example.com/model.pmtiles")


def test_publication_cli_returns_success_for_valid_manifests(tmp_path):
    manifest, archive = _valid_bundle()
    viewer_path = tmp_path / "viewer.json"
    archive_path = tmp_path / "archive.json"
    viewer_path.write_text(json.dumps(manifest), encoding="utf-8")
    archive_path.write_text(json.dumps(archive), encoding="utf-8")

    result = CliRunner().invoke(
        app,
        [
            "validate-publication",
            str(viewer_path),
            str(archive_path),
            "--manifest-only",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "PASS Example Library publication gate" in result.output


def test_publication_cli_emits_json_report(tmp_path):
    manifest, archive = _valid_bundle()
    viewer_path = tmp_path / "viewer.json"
    archive_path = tmp_path / "archive.json"
    viewer_path.write_text(json.dumps(manifest), encoding="utf-8")
    archive_path.write_text(json.dumps(archive), encoding="utf-8")

    result = CliRunner().invoke(
        app,
        [
            "validate-publication",
            str(viewer_path),
            str(archive_path),
            "--manifest-only",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["ok"] is True
