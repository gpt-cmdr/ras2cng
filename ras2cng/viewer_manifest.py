"""Build and validate the RAS Commander MapLibre viewer manifest contract."""

from __future__ import annotations

from copy import deepcopy
import json
import re
from typing import Any, Mapping


MAPLIBRE_SCHEMA = "rascommander.maplibre/v2"
LEGACY_MAPLIBRE_SCHEMA = "rascommander.maplibre.project/1"

ROOT_DEFINITIONS = (
    ("features", "Features"),
    ("geometries", "Geometries"),
    ("results", "Results"),
    ("map-layers", "Map Layers"),
    ("terrains", "Terrains"),
)

_FEATURE_KINDS = {
    "profile_lines",
    "reference_lines",
    "reference_points",
}

_TERRAIN_MODIFICATION_KINDS = {
    "terrain_modification_lines",
    "terrain_modification_polygons",
    "terrain_modification_control_points",
}

_TERRAIN_SOURCE_KINDS = {"terrain_source_footprints"}

_GEOMETRY_BRANCHES = (
    (
        "model",
        "Model",
        {
            "model_extents",
        },
    ),
    (
        "one-dimensional",
        "1D River Network",
        {
            "bank_lines",
            "blocked_obstructions",
            "centerlines",
            "cross_sections",
            "edge_lines",
            "flow_paths",
            "ineffective_areas",
            "junctions",
            "river_reaches",
            "river_stations",
            "xs_interpolation_surface",
        },
    ),
    (
        "two-dimensional",
        "2D Flow Areas",
        {
            "bc_lines",
            "breaklines",
            "mesh_areas",
            "mesh_cells",
            "mesh_faces",
            "refinement_regions",
        },
    ),
    (
        "structures",
        "Structures",
        {
            "pipe_conduits",
            "pipe_inlets",
            "pipe_nodes",
            "pump_stations",
            "storage_areas",
            "structures",
        },
    ),
    (
        "spatial-parameters",
        "Spatial Parameters",
        {
            "boundary_conditions",
            "flow_drag",
            "infiltration",
            "infiltration_regions",
            "land_cover",
            "mannings_n",
            "mannings_n_regions",
            "percent_impervious",
            "porosity",
            "soils",
        },
    ),
)


def apply_manifest_v2(
    manifest: dict[str, Any],
    *,
    archive: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Add the v2 semantic contract while retaining v1 compatibility fields."""

    compatibility = manifest.get("compatibility") or {}
    legacy_schema = str(
        compatibility.get("legacySchema")
        or manifest.get("schema")
        or LEGACY_MAPLIBRE_SCHEMA
    )
    resources: dict[str, dict[str, Any]] = {}
    layers: dict[str, dict[str, Any]] = {}
    legends: dict[str, dict[str, Any]] = deepcopy(manifest.get("legends") or {})

    for tileset in manifest.get("tilesets", []):
        display_resource, numeric_resource = _add_tileset_resources(resources, tileset)
        if tileset.get("type") == "vector":
            for legacy_layer in tileset.get("layers", []):
                layer_id = str(legacy_layer.get("id") or "")
                if not layer_id:
                    raise ValueError("Every vector viewer layer requires an id.")
                if layer_id in layers:
                    raise ValueError(f"Duplicate viewer layer id: {layer_id}")
                layers[layer_id] = _vector_layer_record(
                    legacy_layer,
                    tileset,
                    display_resource,
                )
        elif tileset.get("type") == "raster":
            layer_id = str(tileset.get("id") or "")
            if not layer_id:
                raise ValueError("Every raster viewer layer requires an id.")
            if layer_id in layers:
                raise ValueError(f"Duplicate viewer layer id: {layer_id}")
            legend_id, legend = _raster_legend(tileset)
            legends[legend_id] = legend
            layers[layer_id] = _raster_layer_record(
                tileset,
                display_resource,
                numeric_resource,
                legend_id,
            )

    raster_query = manifest.get("rasterQuery") or {}
    for resource in resources.values():
        if resource.get("type") != "cog":
            continue
        if raster_query.get("sourceCrs"):
            resource.setdefault("crs", raster_query["sourceCrs"])
        if raster_query.get("sourceProj4"):
            resource.setdefault("proj4", raster_query["sourceProj4"])

    _add_hybrid_basemap(resources, layers)
    associations = _build_associations(manifest, layers, archive)
    tree = _build_tree(manifest, layers, archive, associations)
    interaction = _build_interaction(manifest, layers)

    provenance = deepcopy(manifest.get("provenance") or {})
    provenance.setdefault("generatedBy", manifest.get("generatedBy", "ras2cng maplibre"))
    provenance.setdefault("sourceProject", manifest.get("sourceProject"))
    provenance.setdefault("sourceCrs", manifest.get("sourceCrs"))
    if archive:
        provenance.setdefault("archiveSchemaVersion", archive.get("schema_version"))
    provenance.setdefault(
        "resultSemantics",
        {
            "rawHdf": "Values at HEC-RAS computation elements; no surface interpolation.",
            "storedMap": "Raster surface generated by RASMapper/RasProcess.",
        },
    )

    manifest["schema"] = MAPLIBRE_SCHEMA
    manifest["resources"] = resources
    manifest["layers"] = layers
    manifest["tree"] = tree
    manifest["associations"] = associations
    manifest["legends"] = legends
    manifest["interaction"] = interaction
    manifest.setdefault("timeAxes", {})
    manifest["provenance"] = provenance
    manifest["compatibility"] = {
        "legacySchema": legacy_schema,
        "legacyFields": ["tilesets", "groups"],
        "legacyViewerSupported": True,
    }
    validate_manifest_v2(manifest)
    return manifest


def validate_manifest_v2(manifest: Mapping[str, Any]) -> None:
    """Raise ``ValueError`` when required v2 references are inconsistent."""

    if manifest.get("schema") != MAPLIBRE_SCHEMA:
        raise ValueError(f"Viewer manifest schema must be {MAPLIBRE_SCHEMA!r}.")
    resources = manifest.get("resources")
    layers = manifest.get("layers")
    tree = manifest.get("tree")
    if not isinstance(resources, Mapping):
        raise ValueError("Viewer manifest resources must be an object.")
    if not isinstance(layers, Mapping):
        raise ValueError("Viewer manifest layers must be an object.")
    if not isinstance(tree, list):
        raise ValueError("Viewer manifest tree must be an array.")

    expected_roots = [root_id for root_id, _ in ROOT_DEFINITIONS]
    observed_roots = [node.get("id") for node in tree]
    if observed_roots != expected_roots:
        raise ValueError(
            "Viewer manifest roots must be ordered as " + ", ".join(expected_roots)
        )

    for layer_id, layer in layers.items():
        resource_id = layer.get("resource")
        if resource_id not in resources:
            raise ValueError(f"Layer {layer_id!r} references missing resource {resource_id!r}.")
        query = layer.get("query") or {}
        numeric_resource = query.get("numericResource")
        if numeric_resource and numeric_resource not in resources:
            raise ValueError(
                f"Layer {layer_id!r} references missing numeric resource {numeric_resource!r}."
            )
        domain_policy = (layer.get("style") or {}).get("domainPolicy", "fixed")
        if domain_policy == "current-view":
            legend_id = (layer.get("style") or {}).get("legendRef")
            legend = (manifest.get("legends") or {}).get(legend_id, {})
            if legend.get("type") == "categorical":
                raise ValueError(
                    f"Categorical layer {layer_id!r} cannot use current-view styling."
                )
            numeric = resources.get(numeric_resource, {})
            if numeric.get("type") != "cog":
                raise ValueError(
                    f"Current-view layer {layer_id!r} requires an authoritative numeric COG."
                )
            if not numeric.get("serviceAsset") or not numeric.get("serviceRevision"):
                raise ValueError(
                    f"Current-view layer {layer_id!r} requires serviceAsset and serviceRevision metadata."
                )
            service = (manifest.get("services") or {}).get("numericRaster") or {}
            if not all(
                service.get(key)
                for key in ("baseUrl", "statisticsPath", "samplePath", "tilePath")
            ):
                raise ValueError(
                    f"Current-view layer {layer_id!r} requires the numericRaster service contract."
                )

    tree_layer_ids: set[str] = set()

    def visit(node: Mapping[str, Any]) -> None:
        layer_id = node.get("layerId")
        if layer_id:
            if layer_id not in layers:
                raise ValueError(f"Tree references missing layer {layer_id!r}.")
            if layer_id in tree_layer_ids:
                raise ValueError(f"Tree references layer {layer_id!r} more than once.")
            tree_layer_ids.add(str(layer_id))
        for child in node.get("children", []):
            visit(child)

    for root in tree:
        visit(root)

    if tree_layer_ids != set(layers):
        missing = sorted(set(layers) - tree_layer_ids)
        raise ValueError("Viewer layers missing from semantic tree: " + ", ".join(missing))

    interaction = manifest.get("interaction") or {}
    active_layer = interaction.get("activeLayerId")
    if active_layer and active_layer not in layers:
        raise ValueError(f"Active layer {active_layer!r} does not exist.")
    for layer_id in interaction.get("pinnedLayerIds", []):
        if layer_id not in layers:
            raise ValueError(f"Pinned layer {layer_id!r} does not exist.")


def _add_tileset_resources(
    resources: dict[str, dict[str, Any]],
    tileset: Mapping[str, Any],
) -> tuple[str, str | None]:
    tileset_id = str(tileset.get("id") or "")
    if not tileset_id:
        raise ValueError("Every viewer tileset requires an id.")
    tileset_type = str(tileset.get("type") or "")
    display_id = tileset_id if tileset_type == "vector" else f"{tileset_id}-display"
    resource_type = "vector-pmtiles" if tileset_type == "vector" else "raster-pmtiles"
    resource = {
        "type": resource_type,
        "href": tileset.get("href"),
    }
    _copy_present(
        tileset,
        resource,
        "bytes",
        "minzoom",
        "maxzoom",
        "tileSize",
        "bounds",
    )
    resources[display_id] = resource

    numeric_id: str | None = None
    if tileset.get("sourceCog"):
        numeric_id = f"{tileset_id}-numeric"
        numeric = {
            "type": "cog",
            "href": tileset.get("sourceCog"),
            "numeric": True,
        }
        stored_map = tileset.get("storedMap") or {}
        if stored_map.get("cogBytes") is not None:
            numeric["bytes"] = stored_map["cogBytes"]
        _copy_present(
            tileset,
            numeric,
            "units",
            "nodata",
            "dtype",
            "scale",
            "offset",
            "serviceAsset",
            "serviceRevision",
        )
        if tileset.get("sourceCrs"):
            numeric["crs"] = tileset["sourceCrs"]
        if tileset.get("sourceBounds"):
            numeric["sourceBounds"] = deepcopy(tileset["sourceBounds"])
        if tileset.get("bounds"):
            numeric["bounds"] = deepcopy(tileset["bounds"])
        if tileset.get("rasterStats"):
            numeric["statistics"] = deepcopy(tileset["rasterStats"])
        resources[numeric_id] = numeric
    return display_id, numeric_id


def _vector_layer_record(
    legacy_layer: Mapping[str, Any],
    tileset: Mapping[str, Any],
    resource_id: str,
) -> dict[str, Any]:
    raw_result = deepcopy(legacy_layer.get("rawResult") or {})
    group_id = str(legacy_layer.get("groupId") or "")
    declared_source_kind = legacy_layer.get("sourceKind")
    source_kind = str(declared_source_kind) if declared_source_kind else (
        "raw-hdf"
        if raw_result
        or tileset.get("resultKind") == "raw_hdf"
        or tileset.get("id") == "results"
        or group_id == "ras-results"
        or group_id.startswith("ras-results-")
        else "geometry"
    )
    geometry_id = _normalize_id(legacy_layer.get("geometryId")) or _geometry_id(
        legacy_layer.get("groupId")
    )
    plan_id = _normalize_id(raw_result.get("plan")) or _plan_id(legacy_layer, tileset)
    if source_kind == "raw-hdf" and not raw_result:
        raw_result = {
            "source": "Raw HEC-RAS HDF summary result values",
            "plan": plan_id,
            "variable": legacy_layer.get("kind"),
        }
    query = {
        "enabled": legacy_layer.get("queryable") is not False,
        "sourceKind": source_kind,
        "valueSemantics": (
            "raw-computation-element" if source_kind == "raw-hdf" else "feature-attributes"
        ),
        "fields": list(legacy_layer.get("queryFields") or []),
    }
    record: dict[str, Any] = {
        "name": legacy_layer.get("name") or legacy_layer.get("id"),
        "resource": resource_id,
        "sourceLayer": legacy_layer.get("sourceLayer"),
        "role": str(legacy_layer.get("kind") or "vector-layer"),
        "sourceKind": source_kind,
        "visible": bool(legacy_layer.get("visible", False)),
        "sort": legacy_layer.get("sort", 9999),
        "style": deepcopy(legacy_layer.get("style") or {}),
        "query": query,
    }
    _copy_present(
        legacy_layer,
        record,
        "bounds",
        "featureCount",
        "geometryTypes",
        "groupId",
    )
    if geometry_id:
        record["geometry"] = geometry_id
    if plan_id:
        record["plan"] = plan_id
    if raw_result.get("profile") is not None:
        record["profile"] = raw_result["profile"]
    if raw_result:
        raw_result["interpolationAuthority"] = "none"
        record["provenance"] = raw_result
    elif legacy_layer.get("extentSource"):
        record["provenance"] = {"source": legacy_layer["extentSource"]}
    elif legacy_layer.get("provenance"):
        record["provenance"] = deepcopy(legacy_layer["provenance"])
    return record


def _raster_layer_record(
    tileset: Mapping[str, Any],
    display_resource: str,
    numeric_resource: str | None,
    legend_id: str,
) -> dict[str, Any]:
    stored_map = deepcopy(tileset.get("storedMap") or {})
    map_type = str(stored_map.get("mapType") or tileset.get("mapType") or "raster")
    if map_type == "terrain" or tileset.get("sourceKind") == "terrain":
        source_kind = "terrain"
    elif tileset.get("sourceKind") == "calculated" or stored_map.get("recipeId"):
        source_kind = "calculated"
    else:
        source_kind = "stored-map"
    query: dict[str, Any] = {
        "enabled": bool(tileset.get("queryable", False)),
        "sourceKind": source_kind,
        "valueSemantics": (
            "terrain-cell"
            if source_kind == "terrain"
            else "calculated-from-rasmapper-rasters"
            if source_kind == "calculated"
            else "rasmapper-interpolated-raster"
        ),
        "fields": [],
    }
    if numeric_resource:
        query["numericResource"] = numeric_resource
    plan_id = _normalize_id(stored_map.get("plan")) or _plan_id(tileset, tileset)
    record: dict[str, Any] = {
        "name": tileset.get("name") or tileset.get("id"),
        "resource": display_resource,
        "role": map_type,
        "sourceKind": source_kind,
        "visible": bool(tileset.get("visible", False)),
        "sort": tileset.get("sort", 9999),
        "style": {
            "opacity": tileset.get("opacity", 1.0),
            "legendRef": legend_id,
            "domainPolicy": tileset.get("domainPolicy", "fixed"),
        },
        "query": query,
        "raster": {
            "units": tileset.get("units"),
            "statistics": deepcopy(tileset.get("rasterStats") or {}),
        },
    }
    _copy_present(tileset, record, "bounds", "groupId")
    _copy_present(tileset, record["raster"], "nodata", "dtype", "scale", "offset")
    if plan_id:
        record["plan"] = plan_id
    if stored_map.get("profile") is not None:
        record["profile"] = stored_map["profile"]
    geometry_id = _normalize_id(tileset.get("geometryId") or stored_map.get("geometry"))
    if geometry_id:
        record["geometry"] = geometry_id
    if stored_map:
        stored_map.setdefault(
            "interpolationAuthority",
            "none" if source_kind == "terrain" else "RASMapper/RasProcess",
        )
        record["provenance"] = stored_map
    return record


def _raster_legend(tileset: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    legend_id = str(tileset.get("legendRef") or f"legend-{tileset.get('id')}")
    supplied = deepcopy(tileset.get("legend") or {})
    stats = tileset.get("rasterStats") or {}
    domain: dict[str, Any] = {}
    if stats.get("minimum") is not None:
        domain["minimum"] = stats["minimum"]
    if stats.get("maximum") is not None:
        domain["maximum"] = stats["maximum"]
    legend = {
        "type": supplied.pop("type", "continuous"),
        "mode": supplied.pop("mode", tileset.get("ramp", "stretched")),
        "preset": supplied.pop("preset", None),
        "units": supplied.pop("units", tileset.get("units")),
        "domainPolicy": supplied.pop(
            "domainPolicy",
            tileset.get("domainPolicy", "fixed"),
        ),
        "domain": supplied.pop("domain", domain),
        **supplied,
    }
    return legend_id, legend


def _add_hybrid_basemap(
    resources: dict[str, dict[str, Any]],
    layers: dict[str, dict[str, Any]],
) -> None:
    resources["basemap-hybrid"] = {
        "type": "viewer-basemap",
        "provider": "Esri",
        "managedBy": "viewer",
    }
    layers["basemap-hybrid"] = {
        "name": "Hybrid Satellite",
        "resource": "basemap-hybrid",
        "role": "basemap",
        "sourceKind": "map-layer",
        "visible": True,
        "sort": 0,
        "style": {},
        "query": {
            "enabled": False,
            "sourceKind": "map-layer",
            "valueSemantics": "none",
            "fields": [],
        },
    }


def _build_associations(
    manifest: Mapping[str, Any],
    layers: Mapping[str, Mapping[str, Any]],
    archive: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    associations = [deepcopy(item) for item in manifest.get("associations", [])]
    if archive:
        for plan in archive.get("results", []):
            plan_id = _normalize_id(plan.get("plan_id"))
            geometry_id = _normalize_id(plan.get("geom_id"))
            if plan_id and geometry_id:
                associations.append(
                    {
                        "type": "plan-geometry",
                        "plan": plan_id,
                        "geometry": geometry_id,
                        "basis": "archive-manifest",
                    }
                )

    for layer in layers.values():
        plan_id = _normalize_id(layer.get("plan"))
        geometry_id = _normalize_id(layer.get("geometry"))
        if plan_id and geometry_id:
            associations.append(
                {
                    "type": "plan-geometry",
                    "plan": plan_id,
                    "geometry": geometry_id,
                    "basis": "layer-provenance",
                }
            )

    geometry_ids = _geometry_ids(manifest, layers, archive)
    default_geometry = geometry_ids[0] if geometry_ids else None
    for layer_id, layer in layers.items():
        if layer.get("sourceKind") != "terrain":
            continue
        geometry_id = _normalize_id(layer.get("geometry")) or default_geometry
        if geometry_id:
            associations.append(
                {
                    "type": "geometry-terrain",
                    "geometry": geometry_id,
                    "terrain": layer_id,
                    "basis": (
                        "layer-provenance" if layer.get("geometry") else "viewer-default"
                    ),
                }
            )
    return _deduplicate_dicts(associations)


def _build_tree(
    manifest: Mapping[str, Any],
    layers: Mapping[str, Mapping[str, Any]],
    archive: Mapping[str, Any] | None,
    associations: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    roots = {
        root_id: {"id": root_id, "name": name, "role": root_id, "children": []}
        for root_id, name in ROOT_DEFINITIONS
    }
    geometry_ids = _geometry_ids(manifest, layers, archive)
    for geometry_id in geometry_ids:
        geometry_layers = [
            layer_id
            for layer_id, layer in layers.items()
            if layer.get("sourceKind") == "geometry"
            and _normalize_id(layer.get("geometry")) == geometry_id
            and layer.get("role") not in _FEATURE_KINDS
            and layer.get("role") not in _TERRAIN_MODIFICATION_KINDS
            and layer.get("role") not in _TERRAIN_SOURCE_KINDS
        ]
        feature_layers = [
            layer_id
            for layer_id, layer in layers.items()
            if _normalize_id(layer.get("geometry")) == geometry_id
            and layer.get("role") in _FEATURE_KINDS
        ]
        if feature_layers:
            roots["features"]["children"].append(
                _collection_node(
                    f"features-{geometry_id}",
                    f"Geometry {geometry_id}",
                    "geometry-features",
                    feature_layers,
                    layers,
                    metadata={"geometryId": geometry_id},
                )
            )

        branches: list[dict[str, Any]] = []
        assigned: set[str] = set()
        for branch_id, branch_name, kinds in _GEOMETRY_BRANCHES:
            branch_layers = [
                layer_id for layer_id in geometry_layers if layers[layer_id].get("role") in kinds
            ]
            if not branch_layers:
                continue
            assigned.update(branch_layers)
            branches.append(
                _collection_node(
                    f"geometry-{geometry_id}-{branch_id}",
                    branch_name,
                    branch_id,
                    branch_layers,
                    layers,
                )
            )
        other_layers = [layer_id for layer_id in geometry_layers if layer_id not in assigned]
        if other_layers:
            branches.append(
                _collection_node(
                    f"geometry-{geometry_id}-other",
                    "Other Geometry",
                    "other-geometry",
                    other_layers,
                    layers,
                )
            )
        roots["geometries"]["children"].append(
            {
                "id": f"geometry-{geometry_id}",
                "name": f"Geometry {geometry_id}",
                "role": "geometry",
                "metadata": {"geometryId": geometry_id},
                "children": branches,
            }
        )

    plan_info = _plan_info(layers, archive, associations)
    for plan_id, info in plan_info.items():
        raw_layers = _layers_for_plan(layers, plan_id, "raw-hdf")
        raster_layers = _layers_for_plan(layers, plan_id, "stored-map")
        calculated_layers = _layers_for_plan(layers, plan_id, "calculated")
        roots["results"]["children"].append(
            {
                "id": f"plan-{plan_id}",
                "name": info["title"],
                "role": "plan",
                "metadata": {
                    "planId": plan_id,
                    "geometryId": info.get("geometry"),
                },
                "children": [
                    _collection_node(
                        f"plan-{plan_id}-raw",
                        "Raw Computation Values",
                        "raw-computation-values",
                        raw_layers,
                        layers,
                    ),
                    _collection_node(
                        f"plan-{plan_id}-rasters",
                        "Published Raster Maps",
                        "published-raster-maps",
                        raster_layers,
                        layers,
                    ),
                    _collection_node(
                        f"plan-{plan_id}-calculated",
                        "Calculated Layers",
                        "calculated-layers",
                        calculated_layers,
                        layers,
                    ),
                ],
            }
        )

    map_layer_ids = [
        layer_id for layer_id, layer in layers.items() if layer.get("sourceKind") == "map-layer"
    ]
    roots["map-layers"]["children"] = [_leaf(layer_id, layers[layer_id]) for layer_id in map_layer_ids]
    terrain_ids = [
        layer_id for layer_id, layer in layers.items() if layer.get("sourceKind") == "terrain"
    ]
    roots["terrains"]["children"] = [_leaf(layer_id, layers[layer_id]) for layer_id in terrain_ids]
    modification_ids = [
        layer_id for layer_id, layer in layers.items()
        if layer.get("sourceKind") == "terrain-modification"
        or layer.get("role") in _TERRAIN_MODIFICATION_KINDS
    ]
    if modification_ids:
        roots["terrains"]["children"].append(
            _collection_node(
                "terrain-modifications",
                "Terrain Modifications",
                "terrain-modifications",
                modification_ids,
                layers,
            )
        )
    source_ids = [
        layer_id for layer_id, layer in layers.items()
        if layer.get("sourceKind") == "terrain-source"
        or layer.get("role") in _TERRAIN_SOURCE_KINDS
    ]
    if source_ids:
        roots["terrains"]["children"].append(
            _collection_node(
                "terrain-sources",
                "Terrain Sources",
                "terrain-sources",
                source_ids,
                layers,
            )
        )

    assigned_layers: set[str] = set()
    for root in roots.values():
        assigned_layers.update(_tree_layer_ids(root))
    unassigned = [layer_id for layer_id in layers if layer_id not in assigned_layers]
    roots["map-layers"]["children"].extend(
        _leaf(layer_id, layers[layer_id]) for layer_id in unassigned
    )
    return [roots[root_id] for root_id, _ in ROOT_DEFINITIONS]


def _plan_info(
    layers: Mapping[str, Mapping[str, Any]],
    archive: Mapping[str, Any] | None,
    associations: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    info: dict[str, dict[str, Any]] = {}
    if archive:
        for plan in archive.get("results", []):
            plan_id = _normalize_id(plan.get("plan_id"))
            if plan_id:
                title = str(plan.get("plan_title") or f"Plan {plan_id}")
                info[plan_id] = {
                    "title": title,
                    "geometry": _normalize_id(plan.get("geom_id")),
                }
    for layer in layers.values():
        plan_id = _normalize_id(layer.get("plan"))
        if plan_id:
            info.setdefault(plan_id, {"title": f"Plan {plan_id}", "geometry": None})
    for association in associations:
        if association.get("type") != "plan-geometry":
            continue
        plan_id = _normalize_id(association.get("plan"))
        if plan_id:
            info.setdefault(plan_id, {"title": f"Plan {plan_id}", "geometry": None})
            info[plan_id]["geometry"] = _normalize_id(association.get("geometry"))
    return dict(sorted(info.items()))


def _geometry_ids(
    manifest: Mapping[str, Any],
    layers: Mapping[str, Mapping[str, Any]],
    archive: Mapping[str, Any] | None,
) -> list[str]:
    ids: list[str] = []
    for group in manifest.get("groups", []):
        geometry_id = _geometry_id(group.get("id"))
        if geometry_id and geometry_id not in ids:
            ids.append(geometry_id)
    if archive:
        for entry in archive.get("geometry", []):
            geometry_id = _normalize_id(entry.get("geom_id"))
            if geometry_id and geometry_id not in ids:
                ids.append(geometry_id)
    for layer in layers.values():
        geometry_id = _normalize_id(layer.get("geometry"))
        if geometry_id and geometry_id not in ids:
            ids.append(geometry_id)
    return ids


def _build_interaction(
    manifest: Mapping[str, Any],
    layers: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    existing = deepcopy(manifest.get("interaction") or {})
    active_layer = existing.get("activeLayerId")
    if active_layer not in layers:
        active_layer = next(
            (
                layer_id
                for layer_id, layer in layers.items()
                if layer.get("visible") and (layer.get("query") or {}).get("enabled")
            ),
            None,
        )
    pinned = [layer_id for layer_id in existing.get("pinnedLayerIds", []) if layer_id in layers]
    return {
        "activeLayerId": active_layer,
        "pinnedLayerIds": pinned,
        "identify": {
            "mode": "active-and-pinned",
            "maxPinnedLayers": 3,
        },
    }


def _collection_node(
    node_id: str,
    name: str,
    role: str,
    layer_ids: list[str],
    layers: Mapping[str, Mapping[str, Any]],
    *,
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    node: dict[str, Any] = {
        "id": node_id,
        "name": name,
        "role": role,
        "children": [
            _leaf(layer_id, layers[layer_id])
            for layer_id in sorted(
                layer_ids,
                key=lambda item: (layers[item].get("sort", 9999), str(layers[item].get("name", item))),
            )
        ],
    }
    if metadata:
        node["metadata"] = dict(metadata)
    return node


def _leaf(layer_id: str, layer: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": f"layer-{layer_id}",
        "name": layer.get("name") or layer_id,
        "role": layer.get("role") or "layer",
        "layerId": layer_id,
    }


def _layers_for_plan(
    layers: Mapping[str, Mapping[str, Any]],
    plan_id: str,
    source_kind: str,
) -> list[str]:
    return [
        layer_id
        for layer_id, layer in layers.items()
        if _normalize_id(layer.get("plan")) == plan_id and layer.get("sourceKind") == source_kind
    ]


def _tree_layer_ids(node: Mapping[str, Any]) -> set[str]:
    ids: set[str] = set()
    if node.get("layerId"):
        ids.add(str(node["layerId"]))
    for child in node.get("children", []):
        ids.update(_tree_layer_ids(child))
    return ids


def _plan_id(layer: Mapping[str, Any], tileset: Mapping[str, Any]) -> str | None:
    group_id = str(layer.get("groupId") or tileset.get("groupId") or "")
    for prefix in ("ras-results-", "ras-raster-results-"):
        if group_id.startswith(prefix):
            return _normalize_id(group_id.removeprefix(prefix))
    subgroup = tileset.get("resultSubgroup") or {}
    explicit = _normalize_id(subgroup.get("plan") or subgroup.get("planId"))
    if explicit:
        return explicit
    for value in (
        layer.get("id"),
        layer.get("name"),
        layer.get("kind"),
        tileset.get("id"),
    ):
        match = re.search(r"(?:^|[^a-z0-9])(p\d+)(?=$|[^a-z0-9])", str(value or ""), re.I)
        if match:
            return _normalize_id(match.group(1))
    return None


def _geometry_id(group_id: Any) -> str | None:
    value = str(group_id or "")
    prefix = "ras-geometry-"
    return _normalize_id(value.removeprefix(prefix)) if value.startswith(prefix) else None


def _normalize_id(value: Any) -> str | None:
    normalized = str(value or "").strip().lower()
    return normalized or None


def _copy_present(source: Mapping[str, Any], target: dict[str, Any], *keys: str) -> None:
    for key in keys:
        if source.get(key) is not None:
            target[key] = deepcopy(source[key])


def _deduplicate_dicts(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduplicated: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        association_type = item.get("type")
        if association_type == "plan-geometry":
            identity = {
                "type": association_type,
                "plan": item.get("plan"),
                "geometry": item.get("geometry"),
            }
        elif association_type == "geometry-terrain":
            identity = {
                "type": association_type,
                "geometry": item.get("geometry"),
                "terrain": item.get("terrain"),
            }
        else:
            identity = item
        key = json.dumps(identity, sort_keys=True, default=str)
        if key in seen:
            continue
        seen.add(key)
        deduplicated.append(item)
    return deduplicated
