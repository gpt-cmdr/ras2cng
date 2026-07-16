from __future__ import annotations

from copy import deepcopy

import pytest

from ras2cng.viewer_manifest import (
    LEGACY_MAPLIBRE_SCHEMA,
    MAPLIBRE_SCHEMA,
    apply_manifest_v2,
    validate_manifest_v2,
)


def _legacy_manifest() -> dict:
    return {
        "schema": LEGACY_MAPLIBRE_SCHEMA,
        "generatedBy": "ras2cng maplibre",
        "sourceProject": "../project.json",
        "sourceCrs": "EPSG:26916",
        "rasterQuery": {
            "sourceCrs": "EPSG:26916",
            "sourceProj4": "+proj=utm +zone=16 +datum=NAD83 +units=m +no_defs",
        },
        "groups": [
            {"id": "ras-geometry-g01", "name": "Geometry g01", "visible": True},
            {"id": "ras-results-p01", "name": "Vector Results p01", "visible": False},
            {"id": "ras-raster-results", "name": "Raster Results", "visible": False},
            {"id": "ras-terrains", "name": "Terrain", "visible": True},
        ],
        "tilesets": [
            {
                "id": "geometry",
                "type": "vector",
                "href": "tiles/geometry.pmtiles",
                "bytes": 100,
                "layers": [
                    {
                        "id": "ras-geometry-g01-model-extents",
                        "name": "Model Extents",
                        "sourceLayer": "ras-geometry-g01-model-extents",
                        "groupId": "ras-geometry-g01",
                        "visible": True,
                        "kind": "model_extents",
                        "style": {"line": "#ea580c"},
                        "sort": 0,
                        "queryable": True,
                    },
                    {
                        "id": "ras-geometry-g01-mesh-cells",
                        "name": "2D Mesh Cells",
                        "sourceLayer": "ras-geometry-g01-mesh-cells",
                        "groupId": "ras-geometry-g01",
                        "visible": True,
                        "kind": "mesh_cells",
                        "style": {"line": "#2563eb"},
                        "sort": 20,
                        "queryable": True,
                    },
                    {
                        "id": "ras-geometry-g01-reference-lines",
                        "name": "Reference Lines",
                        "sourceLayer": "ras-geometry-g01-reference-lines",
                        "groupId": "ras-geometry-g01",
                        "visible": False,
                        "kind": "reference_lines",
                        "style": {"line": "#0f766e"},
                        "sort": 70,
                        "queryable": True,
                    },
                ],
            },
            {
                "id": "results",
                "type": "vector",
                "href": "tiles/results.pmtiles",
                "bytes": 200,
                "resultKind": "raw_hdf",
                "layers": [
                    {
                        "id": "ras-results-p01-maximum-depth",
                        "name": "Maximum Depth",
                        "sourceLayer": "ras-results-p01-maximum-depth",
                        "groupId": "ras-results-p01",
                        "visible": False,
                        "kind": "p01_maximum_depth",
                        "style": {"fill": "#2563eb"},
                        "queryable": True,
                        "rawResult": {
                            "source": "Raw HEC-RAS HDF summary result values",
                            "plan": "p01",
                            "variable": "maximum_depth",
                            "geometryJoin": "mesh_cells",
                        },
                    }
                ],
            },
            {
                "id": "result-p01-depth-max",
                "name": "p01 Depth (Max)",
                "type": "raster",
                "groupId": "ras-raster-results",
                "href": "tiles/result-p01-depth-max.pmtiles",
                "sourceCog": "../archive/stored-maps/p01/depth-max.cog.tif",
                "bytes": 300,
                "visible": False,
                "opacity": 0.78,
                "rasterStats": {"minimum": 0.0, "maximum": 18.5},
                "storedMap": {
                    "plan": "p01",
                    "profile": "Max",
                    "mapType": "depth",
                    "source": "RasProcess.store_maps",
                    "cogBytes": 400,
                },
                "queryable": True,
                "units": "ft",
            },
            {
                "id": "terrain",
                "name": "Terrain",
                "type": "raster",
                "groupId": "ras-terrains",
                "href": "tiles/terrain.pmtiles",
                "sourceCog": "../archive/terrain/terrain.cog.tif",
                "bytes": 500,
                "visible": True,
                "opacity": 1.0,
                "rasterStats": {"minimum": 900.0, "maximum": 1010.0},
                "storedMap": {
                    "mapType": "terrain",
                    "source": "HEC-RAS terrain GeoTIFF",
                    "cogBytes": 600,
                },
                "queryable": True,
                "units": "ft",
            },
        ],
    }


def _archive_manifest() -> dict:
    return {
        "schema_version": "2.4",
        "geometry": [{"geom_id": "g01"}],
        "results": [
            {
                "plan_id": "p01",
                "plan_title": "Existing Conditions",
                "geom_id": "g01",
            }
        ],
    }


def _tree_layer_ids(node: dict) -> set[str]:
    ids = {node["layerId"]} if node.get("layerId") else set()
    for child in node.get("children", []):
        ids.update(_tree_layer_ids(child))
    return ids


def test_apply_manifest_v2_builds_semantic_contract_and_keeps_legacy_fields() -> None:
    manifest = _legacy_manifest()

    apply_manifest_v2(manifest, archive=_archive_manifest())

    assert manifest["schema"] == MAPLIBRE_SCHEMA
    assert manifest["compatibility"] == {
        "legacySchema": LEGACY_MAPLIBRE_SCHEMA,
        "legacyFields": ["tilesets", "groups"],
        "legacyViewerSupported": True,
    }
    assert manifest["tilesets"]
    assert manifest["groups"]
    assert [root["id"] for root in manifest["tree"]] == [
        "features",
        "geometries",
        "results",
        "map-layers",
        "terrains",
    ]
    assert manifest["resources"]["geometry"]["type"] == "vector-pmtiles"
    assert manifest["resources"]["result-p01-depth-max-display"]["type"] == "raster-pmtiles"
    assert manifest["resources"]["result-p01-depth-max-numeric"] == {
        "type": "cog",
        "href": "../archive/stored-maps/p01/depth-max.cog.tif",
        "numeric": True,
        "bytes": 400,
        "units": "ft",
        "crs": "EPSG:26916",
        "proj4": "+proj=utm +zone=16 +datum=NAD83 +units=m +no_defs",
        "statistics": {"minimum": 0.0, "maximum": 18.5},
    }
    assert manifest["layers"]["ras-results-p01-maximum-depth"]["sourceKind"] == "raw-hdf"
    assert manifest["layers"]["result-p01-depth-max"]["sourceKind"] == "stored-map"
    assert manifest["layers"]["terrain"]["sourceKind"] == "terrain"
    assert manifest["layers"]["result-p01-depth-max"]["query"]["numericResource"] == (
        "result-p01-depth-max-numeric"
    )
    assert manifest["interaction"]["activeLayerId"] == "ras-geometry-g01-model-extents"
    assert manifest["interaction"]["identify"]["mode"] == "active-and-pinned"

    features = manifest["tree"][0]
    geometries = manifest["tree"][1]
    results = manifest["tree"][2]
    assert _tree_layer_ids(features) == {"ras-geometry-g01-reference-lines"}
    assert _tree_layer_ids(geometries) == {
        "ras-geometry-g01-model-extents",
        "ras-geometry-g01-mesh-cells",
    }
    plan = results["children"][0]
    assert plan["name"] == "Existing Conditions"
    assert plan["metadata"] == {"planId": "p01", "geometryId": "g01"}
    assert [branch["role"] for branch in plan["children"]] == [
        "raw-computation-values",
        "published-raster-maps",
    ]
    assert _tree_layer_ids(plan["children"][0]) == {"ras-results-p01-maximum-depth"}
    assert _tree_layer_ids(plan["children"][1]) == {"result-p01-depth-max"}
    assert _tree_layer_ids(manifest["tree"][4]) == {"terrain"}

    plan_geometry = [
        item for item in manifest["associations"] if item["type"] == "plan-geometry"
    ]
    assert plan_geometry == [
        {
            "type": "plan-geometry",
            "plan": "p01",
            "geometry": "g01",
            "basis": "archive-manifest",
        }
    ]
    assert {
        "type": "geometry-terrain",
        "geometry": "g01",
        "terrain": "terrain",
        "basis": "viewer-default",
    } in manifest["associations"]
    validate_manifest_v2(manifest)


def test_apply_manifest_v2_keeps_required_roots_but_omits_empty_subgroups() -> None:
    manifest = _legacy_manifest()
    manifest["tilesets"] = [
        tileset
        for tileset in manifest["tilesets"]
        if tileset["id"] not in {"results", "result-p01-depth-max", "terrain"}
    ]
    manifest["groups"] = [
        group
        for group in manifest["groups"]
        if group["id"] not in {"ras-results-p01", "ras-terrains"}
    ]
    archive = _archive_manifest()
    archive["results"].append(
        {
            "plan_id": "p02",
            "plan_title": "No Published Results",
            "geom_id": "g01",
        }
    )

    apply_manifest_v2(manifest, archive=archive)

    results = next(root for root in manifest["tree"] if root["id"] == "results")
    terrains = next(root for root in manifest["tree"] if root["id"] == "terrains")
    assert results["children"] == []
    assert terrains["children"] == []


def test_apply_manifest_v2_is_idempotent() -> None:
    manifest = _legacy_manifest()
    apply_manifest_v2(manifest, archive=_archive_manifest())
    expected = deepcopy(manifest)

    apply_manifest_v2(manifest, archive=_archive_manifest())

    assert manifest == expected


def test_current_view_requires_cataloged_numeric_service() -> None:
    manifest = _legacy_manifest()
    depth = next(item for item in manifest["tilesets"] if item["id"] == "result-p01-depth-max")
    depth["domainPolicy"] = "current-view"
    depth["legend"] = {
        "type": "continuous",
        "preset": "rasmapper.depth",
        "domainPolicy": "current-view",
    }

    with pytest.raises(ValueError, match="serviceAsset and serviceRevision"):
        apply_manifest_v2(manifest)

    depth["serviceAsset"] = "muncie/result-p01-depth-max"
    depth["serviceRevision"] = "revision-1"
    manifest["services"] = {
        "numericRaster": {
            "baseUrl": "/ras-raster",
            "statisticsPath": "/stats",
            "samplePath": "/sample",
            "tilePath": "/tiles/{z}/{x}/{y}.png",
        }
    }
    apply_manifest_v2(manifest)
    validate_manifest_v2(manifest)


def test_apply_manifest_v2_upgrades_legacy_result_ids_without_provenance() -> None:
    manifest = _legacy_manifest()
    results = next(tileset for tileset in manifest["tilesets"] if tileset["id"] == "results")
    layer = results["layers"][0]
    layer.pop("rawResult")
    layer.pop("queryable")
    results.pop("resultKind")

    apply_manifest_v2(manifest)

    upgraded = manifest["layers"][layer["id"]]
    assert upgraded["sourceKind"] == "raw-hdf"
    assert upgraded["plan"] == "p01"
    assert upgraded["query"]["enabled"] is True
    assert upgraded["provenance"] == {
        "source": "Raw HEC-RAS HDF summary result values",
        "plan": "p01",
        "variable": "p01_maximum_depth",
        "interpolationAuthority": "none",
    }
    raw_branch = manifest["tree"][2]["children"][0]["children"][0]
    assert _tree_layer_ids(raw_branch) == {layer["id"]}


def test_validate_manifest_v2_rejects_missing_layer_resource() -> None:
    manifest = _legacy_manifest()
    apply_manifest_v2(manifest, archive=_archive_manifest())
    manifest["layers"]["terrain"]["resource"] = "missing"

    with pytest.raises(ValueError, match="references missing resource"):
        validate_manifest_v2(manifest)


def test_terrain_modification_vectors_are_nested_under_terrains() -> None:
    manifest = {
        "groups": [{"id": "ras-terrain-modifications-existing", "name": "Modifications"}],
        "tilesets": [
            {
                "id": "geometry",
                "type": "vector",
                "href": "tiles/geometry.pmtiles",
                "layers": [
                    {
                        "id": "existing-modification-lines",
                        "name": "Modification Lines",
                        "sourceLayer": "existing-modification-lines",
                        "groupId": "ras-terrain-modifications-existing",
                        "kind": "terrain_modification_lines",
                        "sourceKind": "terrain-modification",
                        "visible": False,
                        "provenance": {"terrain": "Existing"},
                    }
                ],
            }
        ],
    }

    apply_manifest_v2(manifest)

    terrain_root = next(root for root in manifest["tree"] if root["id"] == "terrains")
    modifications = next(
        node for node in terrain_root["children"] if node["role"] == "terrain-modifications"
    )
    assert modifications["children"][0]["layerId"] == "existing-modification-lines"
    assert manifest["layers"]["existing-modification-lines"]["sourceKind"] == "terrain-modification"
    assert manifest["layers"]["existing-modification-lines"]["provenance"]["terrain"] == "Existing"


def test_terrain_source_footprints_are_nested_under_terrains() -> None:
    manifest = {
        "groups": [{"id": "ras-terrain-sources-existing", "name": "Sources"}],
        "tilesets": [
            {
                "id": "geometry",
                "type": "vector",
                "href": "tiles/geometry.pmtiles",
                "layers": [
                    {
                        "id": "existing-source-footprints",
                        "name": "Source Raster Footprints",
                        "sourceLayer": "existing-source-footprints",
                        "groupId": "ras-terrain-sources-existing",
                        "kind": "terrain_source_footprints",
                        "sourceKind": "terrain-source",
                        "visible": False,
                    }
                ],
            }
        ],
    }

    apply_manifest_v2(manifest)

    terrain_root = next(root for root in manifest["tree"] if root["id"] == "terrains")
    sources = next(node for node in terrain_root["children"] if node["role"] == "terrain-sources")
    assert sources["children"][0]["layerId"] == "existing-source-footprints"
