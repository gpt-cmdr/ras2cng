"""Import separately generated RasProcess Stored Maps into a viewer bundle."""

from __future__ import annotations

import json
import math
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from pathlib import PurePosixPath
from typing import Any, Mapping

import geopandas as gpd

from ras2cng.boundary import (
    DERIVED_BOUNDARY_COMPARISON,
    DERIVED_BOUNDARY_DERIVATION_AUTHORITY,
    DERIVED_BOUNDARY_INTERPOLATION_AUTHORITY,
    DERIVED_BOUNDARY_SCHEMA,
    DERIVED_BOUNDARY_SOURCE,
)
from ras2cng.catalog import Manifest, ManifestMapEntry
from ras2cng.maplibre import (
    package_maplibre_calculated_vector,
    package_maplibre_stored_map,
    package_maplibre_stored_vector,
)


_RASTER_PATTERN = re.compile(
    r"^(.+?) \(([^)]+)\)(.*?)_cog\.tif$",
    re.IGNORECASE,
)
_BOUNDARY_PATTERN = re.compile(r"^Inundation Boundary \(([^)]+)\)\.shp$", re.IGNORECASE)
_DERIVED_BOUNDARY_PATTERN = re.compile(
    r"^Inundation Boundary \(([^)]+)\)\.raster-derived\."
    r"(shp|shx|dbf|prj|cpg|provenance\.json)$",
    re.IGNORECASE,
)
_DERIVED_BOUNDARY_PARTS = frozenset(
    {"shp", "shx", "dbf", "prj", "cpg", "provenance.json"}
)
_WINDOWS_DRIVE_PATH = re.compile(r"^[A-Za-z]:")
_URI_PATH = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:/")

DERIVED_BOUNDARY_MAP_KEY = "derived_inundation_boundary"


@dataclass(frozen=True)
class _RasterMapType:
    key: str
    display_name: str
    units: str


_RASTER_TYPES: tuple[_RasterMapType, ...] = (
    _RasterMapType("depth", "Depth", "ft"),
    _RasterMapType("wse", "Water Surface Elevation", "ft"),
    _RasterMapType("velocity", "Velocity", "ft/s"),
    _RasterMapType("froude", "Froude Number", "dimensionless"),
    _RasterMapType("shear_stress", "Shear Stress", "lb/ft^2"),
    _RasterMapType("depth_x_velocity", "Depth x Velocity", "ft^2/s"),
    _RasterMapType("depth_x_velocity_sq", "Depth x Velocity Squared", "ft^3/s^2"),
    _RasterMapType("arrival_time", "Arrival Time", "hr"),
    _RasterMapType("duration", "Duration", "hr"),
    _RasterMapType("percent_inundated", "Percent Time Inundated", "%"),
)
REQUIRED_STORED_RASTER_TYPE_KEYS = frozenset(
    map_type.key for map_type in _RASTER_TYPES
)
REQUIRED_STORED_MAP_TYPE_KEYS = frozenset(
    REQUIRED_STORED_RASTER_TYPE_KEYS | {"inundation_boundary"}
)
_RASTER_TYPE_ALIASES = {
    "depth": "depth",
    "wse": "wse",
    "water surface elevation": "wse",
    "velocity": "velocity",
    "froude": "froude",
    "froude number": "froude",
    "shear stress": "shear_stress",
    "depth x velocity": "depth_x_velocity",
    "depth x velocity squared": "depth_x_velocity_sq",
    "d _ v": "depth_x_velocity",
    "d _ v squared": "depth_x_velocity_sq",
    "arrival time": "arrival_time",
    "duration": "duration",
    "percent time inundated": "percent_inundated",
    "fraction inundated": "percent_inundated",
    "inundation boundary": "inundation_boundary",
}


@dataclass(frozen=True)
class StoredMapImportSummary:
    """Artifacts registered by :func:`import_rasprocess_stored_maps`."""

    archive_manifest: Path
    viewer_manifest: Path
    plan_count: int
    raster_count: int
    vector_count: int
    layer_ids: tuple[str, ...]


def _profile(value: str) -> str:
    normalized = value.strip()
    if normalized.lower().startswith("max"):
        return "Max"
    if normalized.lower().startswith("min"):
        return "Min"
    return normalized


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def stored_map_type_key(value: str) -> str | None:
    """Normalize a published or on-disk Stored Map type name."""

    normalized = value.lower().replace("²", " squared").replace("^2", " squared")
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return _RASTER_TYPE_ALIASES.get(normalized)


def _contains_processing_host_path(value: Any) -> bool:
    if isinstance(value, Mapping):
        return any(_contains_processing_host_path(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_processing_host_path(item) for item in value)
    if not isinstance(value, str):
        return False
    candidate = value.strip()
    return bool(
        candidate.startswith(("/", "\\\\", "file:/", "file:\\", "~/", "~\\"))
        or _WINDOWS_DRIVE_PATH.match(candidate)
    )


def _valid_resolution(value: Any) -> bool:
    return bool(
        isinstance(value, Mapping)
        and {"x", "y"}.issubset(value)
        and all(
            isinstance(item, (int, float))
            and not isinstance(item, bool)
            and math.isfinite(float(item))
            and float(item) > 0
            for item in (value["x"], value["y"])
        )
    )


def _portable_relative_identifier(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    normalized = value.strip()
    portable = PurePosixPath(normalized)
    return bool(
        normalized
        and normalized == portable.as_posix()
        and not portable.is_absolute()
        and not portable.drive
        and ".." not in portable.parts
        and not normalized.startswith("~")
        and not _WINDOWS_DRIVE_PATH.match(normalized)
        and not _URI_PATH.match(normalized)
    )


def derived_boundary_provenance_errors(
    provenance: Any,
    *,
    profile: str | None = None,
) -> list[str]:
    """Return strict derived-boundary provenance contract violations."""

    if not isinstance(provenance, Mapping):
        return ["provenance must be a JSON object"]

    errors: list[str] = []
    exact_values = {
        "schema": DERIVED_BOUNDARY_SCHEMA,
        "sourceKind": "calculated",
        "source": DERIVED_BOUNDARY_SOURCE,
        "sourceMapType": "Depth",
        "interpolationAuthority": DERIVED_BOUNDARY_INTERPOLATION_AUTHORITY,
        "derivationAuthority": DERIVED_BOUNDARY_DERIVATION_AUTHORITY,
        "comparison": DERIVED_BOUNDARY_COMPARISON,
        "connectivity": 4,
    }
    for key, expected in exact_values.items():
        if provenance.get(key) != expected:
            errors.append(f"{key} must be {expected!r}")
    if provenance.get("nativeRasMapperStoredPolygon") is not False:
        errors.append("nativeRasMapperStoredPolygon must be false")

    threshold = provenance.get("threshold")
    if (
        not isinstance(threshold, (int, float))
        or isinstance(threshold, bool)
        or not math.isfinite(float(threshold))
    ):
        errors.append("threshold must be a finite number")

    recorded_profile = provenance.get("profile")
    if not isinstance(recorded_profile, str) or not recorded_profile.strip():
        errors.append("profile must be a non-empty string")
    elif profile is not None and recorded_profile.strip() != profile.strip():
        errors.append(
            f"profile {recorded_profile!r} does not match filename profile {profile!r}"
        )

    if provenance.get("units") not in {"ft", "m"}:
        errors.append("units must be canonical 'ft' or 'm'")

    source_resolution = provenance.get("sourceResolution")
    output_resolution = provenance.get("outputResolution")
    if not _valid_resolution(source_resolution):
        errors.append("sourceResolution must contain two positive finite numbers")
    if not _valid_resolution(output_resolution):
        errors.append("outputResolution must contain two positive finite numbers")
    if _valid_resolution(source_resolution) and _valid_resolution(output_resolution):
        source_values = tuple(float(source_resolution[key]) for key in ("x", "y"))
        output_values = tuple(float(output_resolution[key]) for key in ("x", "y"))
        if any(output < source for source, output in zip(source_values, output_values)):
            errors.append("outputResolution cannot be finer than sourceResolution")
        resampling = provenance.get("resampling")
        if resampling == "none" and output_values != source_values:
            errors.append("resampling 'none' requires equal source and output resolutions")
        elif resampling == "max" and output_values == source_values:
            errors.append("resampling 'max' requires a coarser output resolution")
        elif resampling not in {"none", "max"}:
            errors.append("resampling must be 'none' or 'max'")
    elif provenance.get("resampling") not in {"none", "max"}:
        errors.append("resampling must be 'none' or 'max'")

    nodata = provenance.get("nodata")
    if not isinstance(nodata, Mapping):
        errors.append("nodata must be an object")
    else:
        if "sourceValue" not in nodata:
            errors.append("nodata.sourceValue is required")
        if nodata.get("datasetMaskApplied") is not True:
            errors.append("nodata.datasetMaskApplied must be true")
        if nodata.get("nonFiniteExcluded") is not True:
            errors.append("nodata.nonFiniteExcluded must be true")
        for key in ("maskedPixelCount", "nonFinitePixelCount"):
            value = nodata.get(key)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                errors.append(f"nodata.{key} must be a non-negative integer")

    edge_count = provenance.get("edgeCount")
    edge_limit = provenance.get("edgeLimit")
    if not isinstance(edge_count, int) or isinstance(edge_count, bool) or edge_count < 0:
        errors.append("edgeCount must be a non-negative integer")
    if not isinstance(edge_limit, int) or isinstance(edge_limit, bool) or edge_limit <= 0:
        errors.append("edgeLimit must be a positive integer")
    if (
        isinstance(edge_count, int)
        and not isinstance(edge_count, bool)
        and isinstance(edge_limit, int)
        and not isinstance(edge_limit, bool)
        and edge_count > edge_limit
    ):
        errors.append("edgeCount cannot exceed edgeLimit")

    source_raster = provenance.get("sourceRaster")
    if not _portable_relative_identifier(source_raster):
        errors.append("sourceRaster must be a portable relative identifier")

    output_shapefile = provenance.get("outputShapefile")
    expected_output = (
        f"Inundation Boundary ({profile}).raster-derived.shp" if profile else None
    )
    if not _portable_relative_identifier(output_shapefile):
        errors.append("outputShapefile must be a portable relative identifier")
    elif expected_output and output_shapefile != expected_output:
        errors.append(
            f"outputShapefile {output_shapefile!r} does not match {expected_output!r}"
        )

    if _contains_processing_host_path(provenance):
        errors.append("provenance contains an absolute processing-host path")
    return errors


def _load_derived_boundary_provenance(
    path: Path,
    *,
    profile: str,
) -> dict[str, Any]:
    try:
        provenance = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(
            f"Invalid derived inundation boundary provenance {path}: {error}"
        ) from error
    errors = derived_boundary_provenance_errors(provenance, profile=profile)
    if errors:
        raise ValueError(
            f"Invalid derived inundation boundary provenance {path}: "
            + "; ".join(errors)
        )
    return dict(provenance)


def _discover_plan_maps(plan_dir: Path) -> dict[str, tuple[Path, str]]:
    discovered: dict[str, tuple[Path, str]] = {}
    native_boundaries: list[tuple[Path, str]] = []
    derived_families: dict[str, dict[str, Path]] = {}
    for path in sorted(plan_dir.iterdir()):
        if not path.is_file():
            continue
        raster_match = _RASTER_PATTERN.match(path.name)
        if raster_match:
            map_type = stored_map_type_key(raster_match.group(1))
            if map_type:
                # Retained per-terrain-source COGs may sit beside the complete
                # VRT-derived COG. Prefer the latter (no source-name suffix).
                is_complete_mosaic = not raster_match.group(3)
                if map_type not in discovered or is_complete_mosaic:
                    discovered[map_type] = (path, _profile(raster_match.group(2)))
            continue
        derived_match = _DERIVED_BOUNDARY_PATTERN.match(path.name)
        if derived_match:
            profile = derived_match.group(1).strip()
            derived_families.setdefault(profile, {})[
                derived_match.group(2).lower()
            ] = path
            continue
        boundary_match = _BOUNDARY_PATTERN.match(path.name)
        if boundary_match:
            native_boundaries.append((path, _profile(boundary_match.group(1))))

    derived_boundaries: list[tuple[Path, str]] = []
    for filename_profile, parts in sorted(derived_families.items()):
        missing = sorted(_DERIVED_BOUNDARY_PARTS - set(parts))
        if missing:
            raise ValueError(
                f"{plan_dir}: partial derived inundation boundary family "
                f"for profile {filename_profile!r}; missing {', '.join(missing)}"
            )
        profile = filename_profile.strip()
        _load_derived_boundary_provenance(
            parts["provenance.json"],
            profile=profile,
        )
        derived_boundaries.append((parts["shp"], profile))

    if len(native_boundaries) > 1:
        raise ValueError(f"{plan_dir}: ambiguous native inundation boundaries")
    if len(derived_boundaries) > 1:
        raise ValueError(f"{plan_dir}: ambiguous derived inundation boundaries")
    if native_boundaries and derived_boundaries:
        raise ValueError(
            f"{plan_dir}: native and derived inundation boundaries are ambiguous"
        )
    if native_boundaries:
        discovered["inundation_boundary"] = native_boundaries[0]
    if derived_boundaries:
        discovered[DERIVED_BOUNDARY_MAP_KEY] = derived_boundaries[0]
    return discovered


def import_rasprocess_stored_maps(
    maps_dir: Path,
    archive_dir: Path,
    viewer_dir: Path,
    *,
    scratch_dir: Path | None = None,
    domain_policy: str = "fixed",
    max_zoom: int | None = 16,
    require_all: bool = True,
    overwrite: bool = False,
) -> StoredMapImportSummary:
    """Import all completed-plan Stored Maps from a RasProcess output tree."""

    maps_dir = Path(maps_dir)
    archive_dir = Path(archive_dir)
    viewer_dir = Path(viewer_dir)
    archive_manifest_path = archive_dir / "manifest.json"
    viewer_manifest_path = viewer_dir / "manifest.json"
    if not maps_dir.is_dir():
        raise NotADirectoryError(f"RasProcess maps directory does not exist: {maps_dir}")
    if not archive_manifest_path.is_file():
        raise FileNotFoundError(f"Archive manifest does not exist: {archive_manifest_path}")
    if not viewer_manifest_path.is_file():
        raise FileNotFoundError(f"Viewer manifest does not exist: {viewer_manifest_path}")
    if domain_policy not in {"fixed", "current-view"}:
        raise ValueError("domain_policy must be 'fixed' or 'current-view'")

    manifest = Manifest.load(archive_manifest_path)
    plans = {
        str(plan.get("plan_id")): plan
        for plan in manifest.results
        if plan.get("completed") is True
    }
    if not plans:
        raise ValueError("Archive has no completed result plans to receive Stored Maps")

    discovered: dict[str, dict[str, tuple[Path, str]]] = {}
    errors: list[str] = []
    for plan_id in plans:
        plan_dir = maps_dir / plan_id
        plan_maps = _discover_plan_maps(plan_dir) if plan_dir.is_dir() else {}
        discovered[plan_id] = plan_maps
        missing = sorted(REQUIRED_STORED_RASTER_TYPE_KEYS - set(plan_maps))
        if not {
            "inundation_boundary",
            DERIVED_BOUNDARY_MAP_KEY,
        }.intersection(plan_maps):
            missing.append("inundation_boundary")
        if require_all and missing:
            errors.append(f"{plan_id}: missing {', '.join(missing)}")
    if errors:
        raise ValueError("Stored Map admission failed: " + "; ".join(errors))

    layer_ids: list[str] = []
    raster_count = 0
    vector_count = 0
    imported_maps: list[dict[str, Any]] = []
    for plan_id, plan in plans.items():
        plan_maps = discovered[plan_id]
        if not plan_maps:
            continue
        geometry_id = str(plan.get("geom_id") or "") or None
        plan_archive = archive_dir / "stored-maps" / plan_id
        plan_archive.mkdir(parents=True, exist_ok=True)
        raster_records: list[dict[str, Any]] = []
        vector_records: list[dict[str, Any]] = []

        for map_spec in _RASTER_TYPES:
            map_key = map_spec.key
            if map_key not in plan_maps:
                continue
            source, profile = plan_maps[map_key]
            target = plan_archive / f"{map_key}-{_slug(profile)}.cog.tif"
            if target.exists() and not overwrite:
                raise FileExistsError(f"Stored Map COG already exists: {target}")
            shutil.copy2(source, target)
            layer_id = f"result-{plan_id}-{map_key.replace('_', '-')}-{_slug(profile)}"
            package_maplibre_stored_map(
                target,
                viewer_dir,
                plan=plan_id,
                map_type="WSE" if map_key == "wse" else map_spec.display_name,
                name=f"{map_spec.display_name} ({profile}) - RASMapper Stored Map",
                profile=profile,
                geometry=geometry_id,
                layer_id=layer_id,
                source_cog=f"../archive/stored-maps/{plan_id}/{target.name}",
                units=map_spec.units,
                visible=False,
                domain_policy=domain_policy,
                max_zoom=max_zoom,
                scratch_dir=(Path(scratch_dir) / plan_id / map_key)
                if scratch_dir
                else None,
                overwrite=overwrite,
            )
            raster_records.append(
                {
                    "type": map_key,
                    "file": target.relative_to(archive_dir).as_posix(),
                    "size_bytes": target.stat().st_size,
                    "profile": profile,
                    "geometry": geometry_id,
                    "units": map_spec.units,
                }
            )
            layer_ids.append(layer_id)
            raster_count += 1

        if "inundation_boundary" in plan_maps:
            source, profile = plan_maps["inundation_boundary"]
            target = plan_archive / f"inundation-boundary-{_slug(profile)}.parquet"
            if target.exists() and not overwrite:
                raise FileExistsError(f"Stored Map vector already exists: {target}")
            frame = gpd.read_file(source)
            if frame.crs is None:
                project_crs = str(manifest.project.get("crs") or "")
                if not project_crs:
                    raise ValueError(f"Inundation boundary has no CRS: {source}")
                frame = frame.set_crs(project_crs)
            frame.to_parquet(target, compression="zstd", index=False)
            layer_id = f"result-{plan_id}-inundation-boundary-{_slug(profile)}"
            package_maplibre_stored_vector(
                target,
                viewer_dir,
                plan=plan_id,
                map_type="Inundation Boundary",
                name=f"Inundation Boundary ({profile}) - RASMapper Stored Map",
                profile=profile,
                geometry=geometry_id,
                layer_id=layer_id,
                crs=str(manifest.project.get("crs") or "") or None,
                visible=False,
                scratch_dir=(Path(scratch_dir) / plan_id / "inundation") if scratch_dir else None,
                overwrite=overwrite,
            )
            vector_records.append(
                {
                    "type": "inundation_boundary",
                    "file": target.relative_to(archive_dir).as_posix(),
                    "size_bytes": target.stat().st_size,
                    "profile": profile,
                    "geometry": geometry_id,
                }
            )
            layer_ids.append(layer_id)
            vector_count += 1

        if DERIVED_BOUNDARY_MAP_KEY in plan_maps:
            source, profile = plan_maps[DERIVED_BOUNDARY_MAP_KEY]
            source_provenance = source.with_suffix(".provenance.json")
            provenance = _load_derived_boundary_provenance(
                source_provenance,
                profile=profile,
            )
            calculated_archive = archive_dir / "calculated" / plan_id
            target = calculated_archive / f"inundation-boundary-{_slug(profile)}.parquet"
            provenance_target = target.with_suffix(".provenance.json")
            for artifact in (target, provenance_target):
                if artifact.exists() and not overwrite:
                    raise FileExistsError(
                        f"Calculated inundation boundary already exists: {artifact}"
                    )
            calculated_archive.mkdir(parents=True, exist_ok=True)
            frame = gpd.read_file(source)
            if frame.crs is None:
                project_crs = str(manifest.project.get("crs") or "")
                if not project_crs:
                    raise ValueError(f"Inundation boundary has no CRS: {source}")
                frame = frame.set_crs(project_crs)
            frame.to_parquet(target, compression="zstd", index=False)
            shutil.copy2(source_provenance, provenance_target)
            layer_id = (
                f"calculated-{plan_id}-inundation-boundary-{_slug(profile)}"
            )
            package_maplibre_calculated_vector(
                target,
                viewer_dir,
                plan=plan_id,
                map_type="Inundation Boundary",
                name=(
                    f"Inundation Boundary ({profile}) - Derived from RASMapper Depth"
                ),
                profile=profile,
                geometry=geometry_id,
                layer_id=layer_id,
                crs=str(manifest.project.get("crs") or "") or None,
                provenance=provenance,
                visible=False,
                scratch_dir=(Path(scratch_dir) / plan_id / "inundation-derived")
                if scratch_dir
                else None,
                overwrite=overwrite,
            )
            vector_records.append(
                {
                    "type": "inundation_boundary",
                    "file": target.relative_to(archive_dir).as_posix(),
                    "size_bytes": target.stat().st_size,
                    "profile": profile,
                    "geometry": geometry_id,
                    "source_kind": "calculated",
                    "result_kind": "calculated_vector",
                    "provenance_file": provenance_target.relative_to(
                        archive_dir
                    ).as_posix(),
                    "provenance": provenance,
                }
            )
            layer_ids.append(layer_id)
            vector_count += 1

        profiles = sorted({profile for _, profile in plan_maps.values()})
        imported_maps.append(
            ManifestMapEntry(
                plan_id=plan_id,
                profile=profiles[0] if len(profiles) == 1 else ", ".join(profiles),
                rasters=raster_records,
                vectors=vector_records,
            ).__dict__
        )

    imported_plan_ids = {entry["plan_id"] for entry in imported_maps}
    manifest.maps = [
        entry for entry in manifest.maps if entry.get("plan_id") not in imported_plan_ids
    ] + imported_maps
    manifest.write(archive_manifest_path)
    return StoredMapImportSummary(
        archive_manifest=archive_manifest_path,
        viewer_manifest=viewer_manifest_path,
        plan_count=len(imported_maps),
        raster_count=raster_count,
        vector_count=vector_count,
        layer_ids=tuple(layer_ids),
    )
