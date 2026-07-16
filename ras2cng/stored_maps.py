"""Import separately generated RasProcess Stored Maps into a viewer bundle."""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import geopandas as gpd

from ras2cng.catalog import Manifest, ManifestMapEntry
from ras2cng.maplibre import (
    package_maplibre_stored_map,
    package_maplibre_stored_vector,
)


_RASTER_PATTERN = re.compile(
    r"^(Depth|WSE|Velocity) \(([^)]+)\).*_cog\.tif$",
    re.IGNORECASE,
)
_BOUNDARY_PATTERN = re.compile(r"^Inundation Boundary \(([^)]+)\)\.shp$", re.IGNORECASE)
_REQUIRED_TYPES = {"depth", "wse", "velocity", "inundation_boundary"}


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


def _discover_plan_maps(plan_dir: Path) -> dict[str, tuple[Path, str]]:
    discovered: dict[str, tuple[Path, str]] = {}
    for path in sorted(plan_dir.iterdir()):
        if not path.is_file():
            continue
        raster_match = _RASTER_PATTERN.match(path.name)
        if raster_match:
            map_type = raster_match.group(1).lower()
            discovered[map_type] = (path, _profile(raster_match.group(2)))
            continue
        boundary_match = _BOUNDARY_PATTERN.match(path.name)
        if boundary_match:
            discovered["inundation_boundary"] = (
                path,
                _profile(boundary_match.group(1)),
            )
    return discovered


def import_rasprocess_stored_maps(
    maps_dir: Path,
    archive_dir: Path,
    viewer_dir: Path,
    *,
    scratch_dir: Path | None = None,
    domain_policy: str = "fixed",
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
        missing = sorted(_REQUIRED_TYPES - set(plan_maps))
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

        for map_key in ("depth", "wse", "velocity"):
            if map_key not in plan_maps:
                continue
            source, profile = plan_maps[map_key]
            target = plan_archive / f"{map_key}-{_slug(profile)}.cog.tif"
            if target.exists() and not overwrite:
                raise FileExistsError(f"Stored Map COG already exists: {target}")
            shutil.copy2(source, target)
            layer_id = f"result-{plan_id}-{map_key}-{_slug(profile)}"
            display_type = "Water Surface Elevation" if map_key == "wse" else map_key.title()
            units = "ft/s" if map_key == "velocity" else "ft"
            package_maplibre_stored_map(
                target,
                viewer_dir,
                plan=plan_id,
                map_type="WSE" if map_key == "wse" else map_key.title(),
                name=f"{display_type} ({profile}) - RASMapper Stored Map",
                profile=profile,
                geometry=geometry_id,
                layer_id=layer_id,
                source_cog=f"../archive/stored-maps/{plan_id}/{target.name}",
                units=units,
                visible=False,
                domain_policy=domain_policy,
                scratch_dir=(Path(scratch_dir) / plan_id / map_key) if scratch_dir else None,
                overwrite=overwrite,
            )
            raster_records.append(
                {
                    "type": map_key,
                    "file": target.relative_to(archive_dir).as_posix(),
                    "size_bytes": target.stat().st_size,
                    "profile": profile,
                    "geometry": geometry_id,
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
