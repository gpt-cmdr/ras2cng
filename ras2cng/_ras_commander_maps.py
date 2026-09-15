"""Private compatibility boundary for ras-commander stored-map APIs."""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any, Optional, Sequence

from ras_commander import HdfResultsPlan, RasMap, RasProcess


SUMMARY_PROFILES = frozenset({"max", "min"})
WHOLE_SIMULATION_MAP_TYPES = frozenset(
    {"arrival_time", "duration", "percent_inundated"}
)
STEADY_PROFILE_MAP_TYPES = (
    "wse",
    "depth",
    "velocity",
    "froude",
    "shear_stress",
    "depth_x_velocity",
    "depth_x_velocity_sq",
)


def supports_optimized_store_maps() -> bool:
    """Return whether ras-commander exposes the selected-map facade."""
    try:
        parameters = inspect.signature(RasMap.store_all_maps).parameters
    except (TypeError, ValueError, AttributeError):
        return False
    return {"mode", "performance", "output_path"}.issubset(parameters)


def supports_exact_steady_profiles() -> bool:
    """Return whether ras-commander exposes exact steady-profile selection."""
    try:
        parameters = inspect.signature(RasMap.store_all_maps).parameters
    except (TypeError, ValueError, AttributeError):
        return False
    return {"mode", "profiles", "output_path"}.issubset(parameters)


def _plan_hdf_path(ras: Any, plan_number: str) -> Path:
    normalized_plan = str(plan_number).zfill(2)
    return Path(str(ras.project_folder)) / f"{ras.project_name}.p{normalized_plan}.hdf"


def _require_exact_steady_profile(plan_hdf: Path, profile: str) -> None:
    """Validate one exact, unambiguous steady-profile name before mutation."""
    available = HdfResultsPlan.get_steady_profile_names(plan_hdf)
    matches = [index for index, name in enumerate(available) if name == profile]
    if not matches:
        raise ValueError(
            f"Steady profile {profile!r} was not found. Available profiles: {available}"
        )
    if len(matches) > 1:
        raise ValueError(
            f"Steady profile name {profile!r} is ambiguous at indexes {matches}"
        )


def _require_exact_unsteady_timestamp(
    ras: Any,
    plan_number: str,
    profile: str,
) -> None:
    """Validate an unsteady timestamp before the legacy API can fall back."""
    available = RasProcess.get_plan_timestamps(plan_number, ras)
    if profile not in available:
        raise ValueError(
            f"Unsteady timestamp {profile!r} was not found. "
            f"First available timestamps: {available[:5]}"
        )


def _require_plan_summary(summary: Any, plan_number: str, label: str) -> dict:
    normalized_plan = str(plan_number).zfill(2)
    if not isinstance(summary, dict):
        raise RuntimeError(f"{label} did not return a summary mapping")
    plan_summary = summary.get("plans", {}).get(normalized_plan)
    if not isinstance(plan_summary, dict):
        raise RuntimeError(f"{label} did not contain plan {normalized_plan}")
    if not plan_summary.get("success", False):
        raise RuntimeError(
            plan_summary.get("error") or f"{label} failed for plan {normalized_plan}"
        )
    return plan_summary


def _normalize_selected_summary(plan_summary: dict) -> dict[str, list[Path]]:
    files_by_type = plan_summary.get("files_by_type")
    if not isinstance(files_by_type, dict):
        raise RuntimeError("StoreMap plan summary has invalid files_by_type")
    return {
        str(map_type): [Path(path) for path in paths if Path(path).exists()]
        for map_type, paths in files_by_type.items()
    }


def _normalize_steady_summary(
    plan_summary: dict,
    requested_profile: str,
) -> dict[str, list[Path]]:
    profiles = plan_summary.get("profiles")
    if not isinstance(profiles, list):
        raise RuntimeError("Steady StoreMap summary did not contain profiles")
    reported_names = [
        str(item.get("profile_name"))
        for item in profiles
        if isinstance(item, dict) and item.get("profile_name") is not None
    ]
    if reported_names != [requested_profile]:
        raise RuntimeError(
            "Steady StoreMap summary did not report the exact requested "
            f"profile {requested_profile!r}; reported {reported_names!r}"
        )

    stored_maps = plan_summary.get("stored_maps")
    if not isinstance(stored_maps, list):
        raise RuntimeError("Steady StoreMap summary did not contain stored_maps")

    normalized: dict[str, list[Path]] = {}
    for record in stored_maps:
        if not isinstance(record, dict):
            raise RuntimeError("Steady StoreMap returned an invalid product record")
        returned_profile = str(record.get("profile_name", ""))
        if returned_profile != requested_profile:
            raise RuntimeError(
                "Steady StoreMap returned a product for unexpected profile "
                f"{returned_profile!r}; requested {requested_profile!r}"
            )
        map_type = str(record.get("map_type", "")).strip().casefold()
        paths = record.get("files")
        if not map_type or not isinstance(paths, list):
            raise RuntimeError("Steady StoreMap returned an incomplete product record")
        allowed_suffixes = (
            {".shp"} if map_type == "inundation_boundary" else {".tif", ".tiff"}
        )
        collected = normalized.setdefault(map_type, [])
        for value in paths:
            path = Path(value)
            if (
                path.exists()
                and path.suffix.casefold() in allowed_suffixes
                and path not in collected
            ):
                collected.append(path)
    return {map_type: paths for map_type, paths in normalized.items() if paths}


def _merge_products(
    target: dict[str, list[Path]],
    source: dict[str, list[Path]],
) -> None:
    for map_type, paths in source.items():
        collected = target.setdefault(map_type, [])
        for path in paths:
            if path not in collected:
                collected.append(path)


def _run_selected_maps(
    *,
    ras: Any,
    plan_number: str,
    profile: str,
    output_dir: Path,
    map_types: Sequence[str],
    render_mode: Optional[str],
    terrain_name: Optional[str],
    arrival_depth: float,
    ras_version: Optional[str],
    timeout: int,
    performance: Any,
) -> dict[str, list[Path]]:
    summary = RasMap.store_all_maps(
        plan_number=plan_number,
        mode="selected",
        output_path=output_dir,
        profile=profile,
        map_types=list(map_types),
        render_mode=render_mode,
        terrain_name=terrain_name,
        arrival_depth=arrival_depth,
        ras_version=ras_version,
        timeout=timeout,
        ras_object=ras,
        performance=performance,
        raise_on_error=True,
    )
    plan_summary = _require_plan_summary(summary, plan_number, "StoreMap summary")
    return _normalize_selected_summary(plan_summary)


def _run_exact_steady_maps(
    *,
    ras: Any,
    plan_number: str,
    profile: str,
    output_dir: Path,
    map_types: Sequence[str],
    render_mode: Optional[str],
    terrain_name: Optional[str],
    ras_version: Optional[str],
    timeout: int,
    inundation_boundary: bool,
) -> dict[str, list[Path]]:
    # Individual product flags allow inundation_boundary=False on the current
    # facade; combining map_types with that explicit toggle is rejected.
    product_flags = {
        map_type: map_type in map_types for map_type in STEADY_PROFILE_MAP_TYPES
    }
    summary = RasMap.store_all_maps(
        plan_number=plan_number,
        mode="steady_profiles",
        profiles=[profile],
        output_path=output_dir,
        render_mode=render_mode,
        terrain_name=terrain_name,
        ras_version=ras_version,
        timeout=timeout,
        ras_object=ras,
        inundation_boundary=inundation_boundary,
        raise_on_error=True,
        **product_flags,
    )
    plan_summary = _require_plan_summary(
        summary,
        plan_number,
        "Steady StoreMap summary",
    )
    return _normalize_steady_summary(plan_summary, profile)


def generate_plan_maps(
    *,
    ras: Any,
    plan_number: str,
    profile: str,
    output_dir: Path,
    map_types: Sequence[str],
    render_mode: Optional[str],
    terrain_name: Optional[str],
    arrival_depth: float,
    ras_version: Optional[str],
    timeout: int,
    performance: Any,
    optimized_available: bool,
) -> Optional[dict[str, list[Path]]]:
    """Generate maps safely, or return ``None`` for the legacy Max/Min path.

    Exact named steady profiles never reach ``RasProcess.store_maps``, whose
    historical behavior substitutes ``Max`` when its timestamp lookup misses.
    """
    if not isinstance(profile, str) or not profile:
        raise ValueError("profile must be a non-empty string")

    selected_types = list(dict.fromkeys(map_types))
    profile_dependent = [
        map_type
        for map_type in selected_types
        if map_type not in WHOLE_SIMULATION_MAP_TYPES
    ]
    whole_simulation = [
        map_type
        for map_type in selected_types
        if map_type in WHOLE_SIMULATION_MAP_TYPES
    ]
    summary_profile = profile.casefold() in SUMMARY_PROFILES

    if summary_profile:
        if not optimized_available:
            return None
        return _run_selected_maps(
            ras=ras,
            plan_number=plan_number,
            profile=profile,
            output_dir=output_dir,
            map_types=selected_types,
            render_mode=render_mode,
            terrain_name=terrain_name,
            arrival_depth=arrival_depth,
            ras_version=ras_version,
            timeout=timeout,
            performance=performance,
        )

    # Whole-simulation products do not use a profile. Avoid sending an
    # irrelevant named selector through the legacy timestamp fallback.
    if not profile_dependent:
        if not optimized_available:
            return None
        return _run_selected_maps(
            ras=ras,
            plan_number=plan_number,
            profile="Max",
            output_dir=output_dir,
            map_types=whole_simulation,
            render_mode=render_mode,
            terrain_name=terrain_name,
            arrival_depth=arrival_depth,
            ras_version=ras_version,
            timeout=timeout,
            performance=performance,
        )

    plan_hdf = _plan_hdf_path(ras, plan_number)
    if HdfResultsPlan.is_steady_plan(plan_hdf):
        _require_exact_steady_profile(plan_hdf, profile)
        if not supports_exact_steady_profiles():
            raise RuntimeError(
                "Exact named steady-profile mapping requires a ras-commander "
                "RasMap.store_all_maps API with mode='steady_profiles'"
            )
        if whole_simulation and not optimized_available:
            raise RuntimeError(
                "Mixed steady-profile and whole-simulation maps require "
                "the optimized ras-commander selected-map API"
            )
        steady_types = [
            map_type
            for map_type in profile_dependent
            if map_type != "inundation_boundary"
        ]
        boundary_requested = "inundation_boundary" in profile_dependent
        if not steady_types:
            raise ValueError(
                "Named steady-profile mapping requires at least one raster "
                "product in addition to inundation_boundary"
            )
        result = _run_exact_steady_maps(
            ras=ras,
            plan_number=plan_number,
            profile=profile,
            output_dir=output_dir,
            map_types=steady_types,
            render_mode=render_mode,
            terrain_name=terrain_name,
            ras_version=ras_version,
            timeout=timeout,
            inundation_boundary=boundary_requested,
        )
        if whole_simulation:
            whole_result = _run_selected_maps(
                ras=ras,
                plan_number=plan_number,
                profile="Max",
                output_dir=output_dir,
                map_types=whole_simulation,
                render_mode=render_mode,
                terrain_name=terrain_name,
                arrival_depth=arrival_depth,
                ras_version=ras_version,
                timeout=timeout,
                performance=performance,
            )
            _merge_products(result, whole_result)
        return result

    _require_exact_unsteady_timestamp(ras, plan_number, profile)
    if not optimized_available:
        return None
    return _run_selected_maps(
        ras=ras,
        plan_number=plan_number,
        profile=profile,
        output_dir=output_dir,
        map_types=selected_types,
        render_mode=render_mode,
        terrain_name=terrain_name,
        arrival_depth=arrival_depth,
        ras_version=ras_version,
        timeout=timeout,
        performance=performance,
    )
