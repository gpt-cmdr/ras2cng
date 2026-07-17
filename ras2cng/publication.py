"""Validate an Example Library viewer bundle before public catalog admission."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
import math
from pathlib import Path
import re
from typing import Any, Mapping
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from ras2cng.stored_maps import (
    REQUIRED_STORED_MAP_TYPE_KEYS,
    stored_map_type_key,
)
from ras2cng.viewer_manifest import validate_manifest_v2


_WINDOWS_PATH = re.compile(r"^[A-Za-z]:[\\/]")
_MODEL_EXTENT_ROLE = "model_extents"
_TWO_DIMENSIONAL_ROLES = {"mesh_areas", "mesh_cells", "mesh_faces"}


@dataclass(frozen=True)
class PublicationIssue:
    severity: str
    code: str
    message: str
    context: str = ""


@dataclass
class PublicationReport:
    manifest: str
    issues: list[PublicationIssue] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)

    @property
    def errors(self) -> list[PublicationIssue]:
        return [issue for issue in self.issues if issue.severity == "error"]

    @property
    def warnings(self) -> list[PublicationIssue]:
        return [issue for issue in self.issues if issue.severity == "warning"]

    @property
    def ok(self) -> bool:
        return not self.errors

    def add(self, severity: str, code: str, message: str, context: str = "") -> None:
        self.issues.append(PublicationIssue(severity, code, message, context))

    def raise_for_errors(self) -> None:
        if self.errors:
            details = "; ".join(f"{issue.code}: {issue.message}" for issue in self.errors)
            raise ValueError(f"Example Library publication gate failed: {details}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "manifest": self.manifest,
            "counts": self.counts,
            "issues": [asdict(issue) for issue in self.issues],
        }


def validate_example_publication(
    viewer_manifest: Path | Mapping[str, Any],
    archive_manifest: Path | Mapping[str, Any] | None = None,
    *,
    check_files: bool = True,
    check_http_ranges: bool = False,
) -> PublicationReport:
    """Run the strict RAS Commander Example Library publication gate."""

    manifest, manifest_path = _load_document(viewer_manifest)
    archive, _ = _load_document(archive_manifest) if archive_manifest is not None else (None, None)
    base_dir = manifest_path.parent if manifest_path else None
    report = PublicationReport(str(manifest_path or "<mapping>"))

    try:
        validate_manifest_v2(manifest)
    except Exception as error:
        report.add("error", "manifest.v2", str(error))

    resources = manifest.get("resources") if isinstance(manifest.get("resources"), Mapping) else {}
    layers = manifest.get("layers") if isinstance(manifest.get("layers"), Mapping) else {}
    completed_plan_ids = {
        str(plan.get("plan_id"))
        for plan in (archive or {}).get("results", [])
        if isinstance(plan, Mapping) and plan.get("completed") is True and plan.get("plan_id")
    }
    source_crs = (manifest.get("provenance") or {}).get("sourceCrs") or manifest.get("sourceCrs")
    if not source_crs:
        report.add("error", "project.crs", "The viewer manifest has no validated project CRS.")

    _validate_no_local_paths(manifest, report)
    _validate_resources(
        resources,
        report,
        base_dir=base_dir,
        check_files=check_files,
        check_http_ranges=check_http_ranges,
    )
    _validate_extent_color_service(manifest, resources, layers, report)

    geometry_layers = {
        layer_id: layer for layer_id, layer in layers.items()
        if layer.get("sourceKind") == "geometry"
    }
    extent_layers = {
        layer_id: layer for layer_id, layer in geometry_layers.items()
        if layer.get("role") == _MODEL_EXTENT_ROLE
    }
    geometry_ids = sorted(
        {
            str(layer.get("geometry"))
            for layer in geometry_layers.values()
            if layer.get("geometry")
        }
    )
    if not extent_layers:
        report.add("error", "geometry.extent", "No API-derived Model Extents layer is published.")
    for geometry_id in geometry_ids:
        matches = [layer for layer in extent_layers.values() if layer.get("geometry") == geometry_id]
        if not matches:
            report.add(
                "error",
                "geometry.extent",
                f"Geometry {geometry_id} has no Model Extents layer.",
                geometry_id,
            )
    if extent_layers and not any(layer.get("visible") is True for layer in extent_layers.values()):
        report.add("error", "defaults.extent", "Model Extents must be enabled by default.")
    for layer_id, layer in extent_layers.items():
        if not _valid_wgs84_bounds(layer.get("bounds")):
            report.add("error", "geometry.extent-bounds", "Model Extents has invalid WGS84 bounds.", layer_id)

    plan_ids = sorted(
        {
            str(layer.get("plan"))
            for layer in layers.values()
            if layer.get("plan")
        }
    )
    raw_layers = {
        layer_id: layer for layer_id, layer in layers.items()
        if layer.get("sourceKind") == "raw-hdf"
    }
    stored_layers = {
        layer_id: layer for layer_id, layer in layers.items()
        if layer.get("sourceKind") == "stored-map"
    }
    terrain_layers = {
        layer_id: layer for layer_id, layer in layers.items()
        if layer.get("sourceKind") == "terrain"
    }
    if not plan_ids:
        report.add("error", "results.plan", "No result plan is published.")
    admission_plan_ids = sorted(set(plan_ids) | completed_plan_ids)
    geometry_2d_ids = {
        str(layer.get("geometry"))
        for layer in geometry_layers.values()
        if layer.get("role") in _TWO_DIMENSIONAL_ROLES and layer.get("geometry")
    }
    archive_plans = {
        str(plan.get("plan_id")): plan
        for plan in (archive or {}).get("results", [])
        if isinstance(plan, Mapping) and plan.get("plan_id")
    }
    stored_map_exempt_plans: set[str] = set()
    for plan_id in admission_plan_ids:
        plan_raw = [layer for layer in raw_layers.values() if layer.get("plan") == plan_id]
        plan_stored = [layer for layer in stored_layers.values() if layer.get("plan") == plan_id]
        if not plan_raw:
            report.add(
                "error",
                "results.raw-hdf",
                "No raw HDF vector result layers are published.",
                plan_id,
            )
        expected_variables = _mappable_archive_variables(archive_plans.get(plan_id, {}))
        published_variables = {
            str((layer.get("provenance") or {}).get("variable") or "").casefold()
            for layer in plan_raw
            if (layer.get("provenance") or {}).get("variable")
        }
        for normalized_name, variable_name in expected_variables.items():
            if normalized_name not in published_variables:
                report.add(
                    "error",
                    "results.raw-variable",
                    f"Joinable raw HDF result variable {variable_name} is not published.",
                    f"{plan_id}:{variable_name}",
                )
        plan_geometry = str(archive_plans.get(plan_id, {}).get("geom_id") or "")
        stored_maps_applicable = bool(terrain_layers) or (
            plan_geometry in geometry_2d_ids
            if plan_geometry
            else bool(geometry_2d_ids)
        )
        if not plan_stored and stored_maps_applicable:
            report.add(
                "error",
                "results.stored-map",
                "No RASMapper Stored Map rasters are published.",
                plan_id,
            )
        elif not plan_stored:
            stored_map_exempt_plans.add(plan_id)
            report.add(
                "warning",
                "results.stored-map-not-applicable",
                "Pure 1D plan has no project terrain; continuous RASMapper Stored Map rasters are not applicable.",
                plan_id,
            )
        elif stored_maps_applicable:
            published_map_types = {
                map_type
                for layer in plan_stored
                if (
                    map_type := stored_map_type_key(
                        str((layer.get("provenance") or {}).get("mapType") or layer.get("role") or "")
                    )
                )
            }
            missing_map_types = sorted(
                REQUIRED_STORED_MAP_TYPE_KEYS - published_map_types
            )
            if missing_map_types:
                report.add(
                    "error",
                    "results.stored-map-type",
                    "Complete Stored Map set is missing: "
                    + ", ".join(missing_map_types),
                    plan_id,
                )

    for layer_id, layer in raw_layers.items():
        query = layer.get("query") or {}
        provenance = layer.get("provenance") or {}
        if query.get("enabled") is not True:
            report.add("error", "results.raw-query", "Raw HDF layer is not queryable.", layer_id)
        if provenance.get("interpolationAuthority") != "none":
            report.add(
                "error",
                "results.raw-provenance",
                "Raw HDF values must explicitly declare no interpolation authority.",
                layer_id,
            )

    for layer_id, layer in stored_layers.items():
        query = layer.get("query") or {}
        provenance = layer.get("provenance") or {}
        numeric_id = query.get("numericResource")
        display_resource = resources.get(layer.get("resource")) or {}
        is_vector_stored_map = display_resource.get("type") == "vector-pmtiles"
        if (
            not is_vector_stored_map
            and (not numeric_id or (resources.get(numeric_id) or {}).get("type") != "cog")
        ):
            report.add(
                "error",
                "results.numeric-cog",
                "Stored Map has no authoritative numeric COG resource.",
                layer_id,
            )
        if is_vector_stored_map and query.get("enabled") is not True:
            report.add(
                "error",
                "results.vector-query",
                "Vector Stored Map is not queryable.",
                layer_id,
            )
        if provenance.get("interpolationAuthority") != "RASMapper/RasProcess":
            report.add(
                "error",
                "results.stored-provenance",
                "Stored Map must identify RASMapper/RasProcess as interpolation authority.",
                layer_id,
            )

    has_2d = any(layer.get("role") in _TWO_DIMENSIONAL_ROLES for layer in geometry_layers.values())
    if has_2d and not terrain_layers:
        report.add("error", "terrain.required", "A 2D model has no published terrain layer.")
    if has_2d and terrain_layers and not any(layer.get("visible") is True for layer in terrain_layers.values()):
        report.add("error", "defaults.terrain", "A 2D project terrain must be enabled by default.")
    for layer_id, layer in terrain_layers.items():
        query = layer.get("query") or {}
        numeric_id = query.get("numericResource")
        if not numeric_id or (resources.get(numeric_id) or {}).get("type") != "cog":
            report.add(
                "error",
                "terrain.numeric-cog",
                "Terrain has no associated queryable numeric COG.",
                layer_id,
            )

    basemap = next(
        (layer for layer in layers.values() if layer.get("role") == "basemap"),
        None,
    )
    if not basemap or basemap.get("visible") is not True:
        report.add("error", "defaults.basemap", "Hybrid satellite imagery must be enabled by default.")

    if archive is None:
        report.add(
            "error",
            "archive.required",
            "The archive manifest is required to verify successful plan completion.",
        )
    else:
        if not completed_plan_ids:
            report.add("error", "results.completed", "No successfully computed plan is recorded.")
        for plan_id in plan_ids:
            if plan_id not in completed_plan_ids:
                report.add(
                    "error",
                    "results.completed",
                    "Published result plan is not recorded as successfully computed.",
                    plan_id,
                )

    model_bounds = [layer.get("bounds") for layer in extent_layers.values() if _valid_wgs84_bounds(layer.get("bounds"))]
    numeric_stored_layers = {
        layer_id: layer
        for layer_id, layer in stored_layers.items()
        if (layer.get("query") or {}).get("numericResource")
    }
    for layer_id, layer in {**terrain_layers, **numeric_stored_layers}.items():
        numeric_id = (layer.get("query") or {}).get("numericResource")
        numeric = resources.get(numeric_id) or {}
        if not numeric.get("crs"):
            report.add("error", "raster.crs", "Numeric COG has no source CRS metadata.", layer_id)
        bounds = numeric.get("bounds")
        if not _valid_wgs84_bounds(bounds):
            report.add("error", "raster.bounds", "Numeric COG has invalid WGS84 bounds.", layer_id)
        elif model_bounds and not any(_bounds_intersect(bounds, extent) for extent in model_bounds):
            report.add("error", "raster.location", "Numeric COG does not intersect Model Extents.", layer_id)

    report.counts = {
        "resources": len(resources),
        "layers": len(layers),
        "geometries": len(geometry_ids),
        "plans": len(plan_ids),
        "completed_plans": len(completed_plan_ids),
        "raw_results": len(raw_layers),
        "stored_maps": len(stored_layers),
        "stored_map_exempt_plans": len(stored_map_exempt_plans),
        "terrains": len(terrain_layers),
    }
    return report


def _mappable_archive_variables(plan: Mapping[str, Any]) -> dict[str, str]:
    """Return archive variables that have enough metadata for a geometry join."""

    expected: dict[str, str] = {}
    for variable in plan.get("variables", []):
        if not isinstance(variable, Mapping):
            continue
        rows = variable.get("rows")
        if rows is not None:
            try:
                if int(rows) <= 0:
                    continue
            except (TypeError, ValueError):
                pass
        variable_name = str(variable.get("variable") or "").strip()
        if not variable_name or not variable.get("geometry_filter"):
            continue
        if not (variable.get("index_column") or variable.get("join_columns")):
            continue
        expected[variable_name.casefold()] = variable_name
    return expected


def _validate_extent_color_service(
    manifest: Mapping[str, Any],
    resources: Mapping[str, Mapping[str, Any]],
    layers: Mapping[str, Mapping[str, Any]],
    report: PublicationReport,
) -> None:
    """Require continuous public rasters to support extent-based color mapping."""

    services = (
        manifest.get("services") if isinstance(manifest.get("services"), Mapping) else {}
    )
    service = (
        services.get("numericRaster")
        if isinstance(services.get("numericRaster"), Mapping)
        else {}
    )
    legends = (
        manifest.get("legends") if isinstance(manifest.get("legends"), Mapping) else {}
    )
    for layer_id, layer in layers.items():
        numeric_id = (layer.get("query") or {}).get("numericResource")
        if not numeric_id:
            continue
        legend_id = (layer.get("style") or {}).get("legendRef")
        legend = legends.get(legend_id) or {}
        if legend.get("type") == "categorical":
            continue
        if not legend.get("preset"):
            report.add(
                "error",
                "raster.extent-color-preset",
                "Color Map by Extents requires a style preset in the continuous legend.",
                layer_id,
            )
        numeric = resources.get(numeric_id) or {}
        if numeric.get("type") != "cog":
            report.add(
                "error",
                "raster.extent-color-cog",
                "Color Map by Extents requires an authoritative numeric COG.",
                layer_id,
            )
        if not numeric.get("serviceAsset") or not numeric.get("serviceRevision"):
            report.add(
                "error",
                "raster.extent-color-asset",
                "Color Map by Extents requires a cataloged service asset and revision.",
                layer_id,
            )
        if not all(
            service.get(key)
            for key in ("baseUrl", "statisticsPath", "samplePath", "tilePath")
        ):
            report.add(
                "error",
                "raster.extent-color-service",
                "Color Map by Extents requires the numeric raster service contract.",
                layer_id,
            )


def _load_document(
    document: Path | Mapping[str, Any] | None,
) -> tuple[dict[str, Any], Path | None]:
    if document is None:
        return {}, None
    if isinstance(document, Mapping):
        return dict(document), None
    path = Path(document)
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object: {path}")
    return value, path.resolve()


def _validate_resources(
    resources: Mapping[str, Mapping[str, Any]],
    report: PublicationReport,
    *,
    base_dir: Path | None,
    check_files: bool,
    check_http_ranges: bool,
) -> None:
    for resource_id, resource in resources.items():
        if resource.get("type") in {"viewer-basemap", "basemap"}:
            continue
        href = resource.get("href")
        if not isinstance(href, str) or not href.strip():
            report.add("error", "resource.href", "Resource has no href.", resource_id)
            continue
        parsed = urlparse(href)
        if parsed.scheme in {"http", "https"}:
            if check_http_ranges:
                try:
                    _require_http_range(href)
                except Exception as error:
                    report.add("error", "resource.range", str(error), resource_id)
            continue
        if parsed.scheme == "file" or _WINDOWS_PATH.match(href):
            report.add("error", "resource.local-path", "Public manifest contains a local file path.", resource_id)
            continue
        if check_files and base_dir is not None:
            local_path = (base_dir / href.split("?", 1)[0]).resolve()
            if not local_path.is_file():
                report.add("error", "resource.missing", f"Artifact does not exist: {local_path}", resource_id)
            elif local_path.stat().st_size <= 0:
                report.add("error", "resource.empty", f"Artifact is empty: {local_path}", resource_id)
            elif resource.get("type") == "cog":
                _validate_local_cog(local_path, resource_id, report)


def _require_http_range(url: str) -> None:
    request = Request(url, headers={"Range": "bytes=0-0", "User-Agent": "ras2cng-publication-gate/1.0"})
    with urlopen(request, timeout=30) as response:  # noqa: S310 - manifest controls reviewed public URLs.
        status = getattr(response, "status", response.getcode())
        content_range = response.headers.get("Content-Range", "")
        if status != 206 or not content_range.lower().startswith("bytes 0-0/"):
            raise ValueError(f"Artifact does not support HTTP byte ranges: {url}")


def _validate_local_cog(path: Path, resource_id: str, report: PublicationReport) -> None:
    """Check the local numeric raster structure used by the public COG href."""

    try:
        import rasterio
        from rasterio.enums import MaskFlags

        with rasterio.open(path) as source:
            if source.driver != "GTiff":
                report.add("error", "cog.driver", "Numeric COG is not a GeoTIFF.", resource_id)
            if source.crs is None:
                report.add("error", "cog.crs", "Numeric COG has no embedded CRS.", resource_id)
            if max(source.width, source.height) > 512 and not source.is_tiled:
                report.add("error", "cog.tiling", "Large numeric COG is not internally tiled.", resource_id)
            if max(source.width, source.height) > 1024 and not source.overviews(1):
                report.add("error", "cog.overviews", "Large numeric COG has no overviews.", resource_id)
            mask_flags = set(source.mask_flag_enums[0]) if source.mask_flag_enums else set()
            has_mask = source.nodata is not None or MaskFlags.alpha in mask_flags or MaskFlags.per_dataset in mask_flags
            if not has_mask:
                report.add(
                    "error",
                    "cog.nodata",
                    "Numeric COG has no nodata value or validity mask for transparent display.",
                    resource_id,
                )
    except Exception as error:
        report.add("error", "cog.read", f"Numeric COG could not be opened: {error}", resource_id)


def _validate_no_local_paths(value: Any, report: PublicationReport, key: str = "manifest") -> None:
    if isinstance(value, Mapping):
        for child_key, child in value.items():
            _validate_no_local_paths(child, report, f"{key}.{child_key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _validate_no_local_paths(child, report, f"{key}[{index}]")
    elif isinstance(value, str) and (_WINDOWS_PATH.match(value) or value.lower().startswith("file://")):
        report.add("error", "manifest.local-path", "Public manifest contains a local path.", key)


def _valid_wgs84_bounds(value: Any) -> bool:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return False
    try:
        minx, miny, maxx, maxy = (float(item) for item in value)
    except (TypeError, ValueError):
        return False
    return (
        all(math.isfinite(item) for item in (minx, miny, maxx, maxy))
        and -180 <= minx < maxx <= 180
        and -90 <= miny < maxy <= 90
    )


def _bounds_intersect(first: Any, second: Any) -> bool:
    return not (
        float(first[2]) < float(second[0])
        or float(first[0]) > float(second[2])
        or float(first[3]) < float(second[1])
        or float(first[1]) > float(second[3])
    )
