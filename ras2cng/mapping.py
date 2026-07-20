"""
Result raster generation for ras2cng.

Provides:
- generate_result_maps(): Generate WSE/Depth/Velocity/etc. rasters via RasStoreMapHelper.exe
- MapResult: Structured output from map generation
"""

from __future__ import annotations

import inspect
import platform
import shutil
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from rich.console import Console

from ras_commander import RasMap, RasProcess, init_ras_project

try:
    from ras_commander import StoreMapPerformanceOptions
except ImportError:  # ras-commander < 0.99.0 compatibility
    StoreMapPerformanceOptions = None  # type: ignore[assignment,misc]

console = Console()


DEFAULT_LOCAL_MAP_PERFORMANCE = (
    StoreMapPerformanceOptions(
        max_workers=None,
        memory_policy="enforce",
        reserve_memory_mb=8192,
        reserve_memory_fraction=0.25,
        gdal_num_threads_per_helper=1,
        gdal_cachemax_mb=64,
    )
    if StoreMapPerformanceOptions is not None
    else None
)


@dataclass
class MapResult:
    """Result of map generation for a single plan."""
    plan_id: str
    plan_number: str
    map_types: dict[str, list[Path]] = field(default_factory=dict)  # {"depth": [Path(...)]}
    output_dir: Path = field(default_factory=lambda: Path("."))
    errors: list[str] = field(default_factory=list)


def _supports_optimized_store_maps() -> bool:
    """Return whether the installed ras-commander has the canonical API."""
    if StoreMapPerformanceOptions is None:
        return False
    try:
        parameters = inspect.signature(RasMap.store_all_maps).parameters
    except (TypeError, ValueError, AttributeError):
        return False
    return {"mode", "performance", "output_path"}.issubset(parameters)


# Map type names to RasProcess store_maps variable names
MAP_TYPE_VARIABLES = {
    "wse": "Water Surface",
    "depth": "Depth",
    "velocity": "Velocity",
    "froude": "Froude Number",
    "shear_stress": "Shear Stress",
    "depth_x_velocity": "Depth x Velocity",
    "depth_x_velocity_sq": "Depth x Velocity²",
    "inundation_boundary": "Inundation Boundary",
    "arrival_time": "Arrival Time",
    "duration": "Duration",
    "recession": "Recession",
    "percent_inundated": "Percent Time Inundated",
}

# Whole-simulation map types: RasMapperLib labels their outputs by the
# ArrivalDepth threshold (e.g. "Arrival Time (0.1ft hrs)") instead of the
# profile, and always computes them over the full simulation. XML MapType
# names verified against the RasMapperLib.dll MapTypes table (6.6 and 7.0.1).
ADR_MAP_TYPES = {
    "arrival_time": ("arrival time", "Arrival Time"),
    "duration": ("duration", "Duration"),
    "percent_inundated": ("fraction inundated", "Percent Time Inundated"),
}

TERRAIN_STORED_MAP_TYPES = {
    "wse": ("elevation", "WSE"),
    "depth": ("depth", "Depth"),
    "velocity": ("velocity", "Velocity"),
    "froude": ("froude", "Froude"),
    "shear_stress": ("Shear", "Shear Stress"),
    "depth_x_velocity": ("depth and velocity", "D _ V"),
    "depth_x_velocity_sq": ("depth and velocity squared", "D _ V^2"),
}

GENERATED_RASTER_PREFIXES = {
    "wse": ("WSE (",),
    "depth": ("Depth (",),
    "velocity": ("Velocity (",),
    "froude": ("Froude (",),
    "shear_stress": ("Shear Stress (",),
    "depth_x_velocity": ("D _ V (", "Depth x Velocity ("),
    "depth_x_velocity_sq": ("D _ V^2 (", "Depth x Velocity squared ("),
    "arrival_time": ("Arrival Time (",),
    "duration": ("Duration (",),
    "percent_inundated": ("Percent Time Inundated (",),
}


def generate_result_maps(
    project_path: Path,
    output_dir: Path,
    *,
    plans: Optional[list[str]] = None,
    profile: str = "Max",
    wse: bool = True,
    depth: bool = True,
    velocity: bool = True,
    froude: bool = False,
    shear_stress: bool = False,
    depth_x_velocity: bool = False,
    depth_x_velocity_sq: bool = False,
    inundation_boundary: bool = False,
    arrival_time: bool = False,
    duration: bool = False,
    recession: bool = False,
    percent_inundated: bool = False,
    arrival_depth: float = 0.0,
    terrain_name: Optional[str] = None,
    ras_version: Optional[str] = None,
    rasprocess_path: Optional[Path] = None,
    render_mode: Optional[str] = None,
    min_depth: float = 0.0,
    reproject_wgs84: bool = False,
    convert_cog: bool = False,
    timeout: int = 10800,
    skip_errors: bool = True,
    keep_postprocessing: bool = False,
    performance: Optional["StoreMapPerformanceOptions"] = None,
) -> list[MapResult]:
    """Generate result rasters for plans in a HEC-RAS project.

    Uses the canonical ``RasMap.store_all_maps(mode="selected")`` API with
    RasStoreMapHelper.exe to generate raw TIFs from completed plan HDF files.
    With ras-commander 0.99.0 or newer, the default is memory-aware local auto
    parallelism. ras-commander 0.98.2 retains its serial compatibility path.

    Args:
        project_path: Path to .prj file or project directory
        output_dir: Directory for output raster files
        plans: Plan IDs to process (e.g. ["p01", "p02"]). None = all with results
        profile: Output profile: "Max", "Min", or timestamp
        wse: Generate Water Surface Elevation rasters
        depth: Generate Depth rasters
        velocity: Generate Velocity rasters
        froude: Generate Froude Number rasters
        shear_stress: Generate Shear Stress rasters
        depth_x_velocity: Generate Depth x Velocity rasters
        depth_x_velocity_sq: Generate Depth x Velocity² rasters
        inundation_boundary: Generate Inundation Boundary polygon (shapefile)
        arrival_time: Generate Arrival Time rasters (hours; whole-simulation,
            ignores `profile`)
        duration: Generate Duration rasters (hours; whole-simulation)
        recession: Not supported — RasMapperLib has no recession map type;
            a warning is printed and the flag is ignored
        percent_inundated: Generate Percent Time Inundated rasters
        arrival_depth: Wet/dry depth threshold (model vertical units) for
            arrival/duration/recession/percent_inundated (default: 0.0)
        terrain_name: Specific terrain name from rasmap to use for mapping
        ras_version: HEC-RAS version (auto-detected if None)
        rasprocess_path: Path to HEC-RAS install directory (for helper deployment)
        render_mode: Water surface render mode: "horizontal", "sloping", or "slopingPretty".
            If None, reads from the .rasmap file (default: horizontal).
        min_depth: Minimum depth threshold for depth rasters (default: 0.0)
        reproject_wgs84: Reproject output rasters to WGS84
        convert_cog: Convert output to Cloud Optimized GeoTIFF
        timeout: Per-plan timeout in seconds (default: 10800 = 3 hours)
        skip_errors: If True, log and continue past errors
        keep_postprocessing: Keep the PostProcessing.hdf cache RasMapperLib
            creates for derived map types (can exceed the plan HDF in size).
            Default: delete it from the output directory.
        performance: Optional ras-commander StoreMap performance policy. None
            selects :data:`DEFAULT_LOCAL_MAP_PERFORMANCE` when the canonical
            API is available. Pass ``StoreMapPerformanceOptions(max_workers=1)``
            for the legacy serial execution policy.

    Returns:
        List of MapResult, one per processed plan
    """
    from ras2cng.project import resolve_project_path

    project_path = Path(project_path)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    project_dir, prj_file = resolve_project_path(project_path)

    console.print(f"\n[bold cyan]ras2cng map[/bold cyan] -> {output_dir}")
    console.print(f"  Project : {prj_file.name}")
    console.print(f"  Profile : {profile}")

    optimized_store_maps = _supports_optimized_store_maps()
    if performance is not None and not optimized_store_maps:
        raise RuntimeError(
            "performance requires ras-commander>=0.99.0; upgrade ras-commander "
            "or omit performance to use the serial compatibility path"
        )
    if performance is not None:
        effective_performance = performance
    elif optimized_store_maps:
        effective_performance = DEFAULT_LOCAL_MAP_PERFORMANCE
    else:
        effective_performance = None
    if effective_performance is not None:
        worker_label = (
            "auto"
            if effective_performance.max_workers is None
            else str(effective_performance.max_workers)
        )
        console.print(
            f"  Mapping : {worker_label} helper(s), "
            f"{effective_performance.memory_policy} memory admission"
        )
    else:
        console.print("  Mapping : serial compatibility path")

    # Configure RasProcess
    _configure_rasprocess(rasprocess_path, ras_version)

    # Initialize project (pass ras_version to avoid auto-detecting old versions from plan files)
    init_kwargs = dict(ras_object="new", load_results_summary=True)
    if ras_version:
        init_kwargs["ras_version"] = ras_version
    ras = init_ras_project(project_dir, **init_kwargs)
    if rasprocess_path:
        exe_path = Path(rasprocess_path)
        ras.ras_exe_path = str(
            exe_path
            if exe_path.suffix.lower() == ".exe"
            else exe_path / "RasProcess.exe"
        )

    # RasMapperLib has no recession MapType (verified 6.6/7.0.1) — only
    # RasMapperLib-native products are generated.
    if recession:
        console.print(
            "  [yellow]Warning:[/yellow] recession has no RasMapperLib map type "
            "- skipping (arrival_time and duration are available)"
        )
        recession = False

    # Build list of requested map types
    requested_types = _build_requested_types(
        wse=wse, depth=depth, velocity=velocity,
        froude=froude, shear_stress=shear_stress,
        depth_x_velocity=depth_x_velocity,
        depth_x_velocity_sq=depth_x_velocity_sq,
        inundation_boundary=inundation_boundary,
        arrival_time=arrival_time, duration=duration,
        percent_inundated=percent_inundated,
    )

    if not requested_types:
        console.print("[yellow]Warning:[/yellow] No map types selected")
        return []

    if "inundation_boundary" in requested_types and (reproject_wgs84 or convert_cog):
        console.print(
            "  [yellow]Note:[/yellow] raster post-processing (--wgs84/--cog) does "
            "not apply to the inundation boundary shapefile - it stays in the "
            "model CRS"
        )

    console.print(f"  Map types: {', '.join(requested_types)}")

    # Determine which plans to process
    plan_filter = set(plans) if plans else None
    plan_rows = ras.plan_df if ras.plan_df is not None and not ras.plan_df.empty else None

    if plan_rows is None:
        console.print("[yellow]Warning:[/yellow] No plans found in project")
        return []

    results: list[MapResult] = []

    for _, row in plan_rows.iterrows():
        plan_num = str(row.get("plan_number", "")).zfill(2)
        plan_id = f"p{plan_num}"

        if plan_filter and plan_id not in plan_filter:
            continue

        plan_hdf = project_dir / f"{ras.project_name}.p{plan_num}.hdf"
        if not plan_hdf.exists():
            console.print(f"  [{plan_id}] No HDF results - skipping")
            continue

        plan_output = output_dir / plan_id
        plan_output.mkdir(parents=True, exist_ok=True)

        map_result = MapResult(
            plan_id=plan_id,
            plan_number=plan_num,
            output_dir=plan_output,
        )

        console.print(f"  [{plan_id}] Generating maps...")

        try:
            # Build boolean flags for RasProcess.store_maps()
            type_flags = {t: (t in requested_types) for t in MAP_TYPE_VARIABLES}

            plan_run_started = time.time() - 2  # filesystem mtime slack

            result_dict = _generate_plan_maps(
                ras=ras,
                plan_number=plan_num,
                profile=profile,
                output_dir=plan_output,
                terrain_name=terrain_name,
                render_mode=render_mode,
                timeout=timeout,
                arrival_depth=arrival_depth,
                ras_version=ras_version,
                performance=effective_performance,
                **type_flags,
            )

            # Shapefile outputs (inundation boundary) are moved by store_maps
            # but not included in its TIF-oriented return dict — glob them,
            # restricted to files produced by this run.
            if "inundation_boundary" in requested_types and not result_dict.get("inundation_boundary"):
                shp_paths = sorted(
                    p for p in plan_output.glob("*.shp")
                    if p.stat().st_mtime >= plan_run_started
                )
                if shp_paths:
                    result_dict["inundation_boundary"] = shp_paths

            for map_type in requested_types:
                tif_paths = result_dict.get(map_type, [])

                # Raster post-processing does not apply to shapefile outputs
                if map_type != "inundation_boundary":
                    # Post-process: depth threshold
                    if map_type == "depth" and min_depth > 0.0:
                        tif_paths = _apply_depth_threshold(tif_paths, min_depth)

                    # Post-process: reproject to WGS84
                    if reproject_wgs84:
                        tif_paths = _reproject_tifs(tif_paths, "EPSG:4326")

                    # Post-process: convert to COG
                    if convert_cog:
                        tif_paths = _convert_to_cog(tif_paths)

                if tif_paths:
                    map_result.map_types[map_type] = tif_paths
                    console.print(f"    {map_type}: {len(tif_paths)} raster(s)")

            postprocessing_hdf = plan_output / "PostProcessing.hdf"
            if postprocessing_hdf.exists() and not keep_postprocessing:
                size_mb = postprocessing_hdf.stat().st_size / 1e6
                postprocessing_hdf.unlink()
                console.print(f"    Removed PostProcessing.hdf cache ({size_mb:.0f} MB)")

        except Exception as e:
            error_msg = f"plan {plan_id}: {e}"
            map_result.errors.append(error_msg)
            console.print(f"    [yellow]Warning:[/yellow] {error_msg}")
            if not skip_errors:
                raise

        results.append(map_result)

    total_maps = sum(
        sum(len(paths) for paths in r.map_types.values())
        for r in results
    )
    total_errors = sum(len(r.errors) for r in results)
    console.print(f"\n[green]OK[/green] Generated {total_maps} raster(s) from {len(results)} plan(s)")
    if total_errors:
        console.print(f"  [yellow]{total_errors} error(s)[/yellow]")

    return results


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _configure_rasprocess(
    rasprocess_path: Optional[Path] = None,
    ras_version: Optional[str] = None,
) -> None:
    """Configure RasProcess executable path and version."""
    if rasprocess_path:
        rasprocess_path = Path(rasprocess_path)
        ras_install_dir = rasprocess_path.parent if rasprocess_path.suffix.lower() == ".exe" else rasprocess_path

        if platform.system() == "Linux":
            # configure_wine expects ras_install_dir (directory containing RasProcess.exe).
            try:
                RasProcess.configure_wine(ras_install_dir=str(ras_install_dir))
            except AttributeError:
                # Older ras-commander versions may not have configure_wine.
                pass
        else:
            rasprocess_exe = (
                ras_install_dir
                if ras_install_dir.name.lower() == "rasprocess.exe"
                else ras_install_dir / "RasProcess.exe"
            )
            if not rasprocess_exe.exists():
                raise FileNotFoundError(f"RasProcess.exe not found: {rasprocess_exe}")

    if ras_version:
        try:
            RasProcess.find_rasprocess(version=ras_version)
        except Exception:
            pass  # Will fail later with a clearer error if exe not found


def _build_requested_types(**kwargs) -> list[str]:
    """Build list of requested map type names from boolean flags."""
    return [name for name, enabled in kwargs.items() if enabled]


def _store_maps_supports_native_adr() -> bool:
    """True if the installed ras-commander accepts arrival_time et al. natively."""
    try:
        return "arrival_time" in inspect.signature(RasProcess.store_maps).parameters
    except (TypeError, ValueError):
        return False


def _inject_adr_stored_maps(
    rasmap_path: Path,
    plan_hdf_name: str,
    adr_requested: dict[str, bool],
    arrival_depth: float,
) -> None:
    """Pre-inject ADR stored-map entries into the rasmap (older ras-commander shim).

    Mirrors the XML schema of RasProcess._add_stored_map_to_rasmap, adding the
    ArrivalDepth threshold attribute these whole-simulation map types require.
    StoreAllMaps executes every stored map present in the rasmap, so entries
    injected before RasProcess.store_maps() ride along in its single run.
    """
    tree = ET.parse(rasmap_path)
    root = tree.getroot()

    results_elem = root.find(".//Results")
    if results_elem is None:
        results_elem = ET.SubElement(root, "Results", {"Checked": "True"})

    plan_layer = None
    for layer in results_elem.findall("Layer"):
        # Rasmap Filenames are backslash-relative (".\\Model.p01.hdf"); on
        # POSIX, Path does not split on backslashes, so normalize first.
        filename = layer.get("Filename", "").replace("\\", "/")
        if Path(filename).name.lower() == plan_hdf_name.lower():
            plan_layer = layer
            break
    if plan_layer is None:
        plan_layer = ET.SubElement(results_elem, "Layer", {
            "Name": Path(plan_hdf_name).stem,
            "Type": "RASResults",
            "Filename": f".\\{plan_hdf_name}",
        })

    for key, (xml_name, display_name) in ADR_MAP_TYPES.items():
        if not adr_requested.get(key):
            continue
        # Folder-qualified like ras-commander's writer; RasMapperLib 6.x
        # overrides the directory with the Plan ShortID folder either way.
        stored = f".\\{Path(plan_hdf_name).stem}\\{display_name}.vrt"
        layer_elem = ET.SubElement(plan_layer, "Layer", {
            "Name": display_name,
            "Type": "RASResultsMap",
            "Checked": "True",
            "Filename": stored,
        })
        ET.SubElement(layer_elem, "MapParameters", {
            "MapType": xml_name,
            "OutputMode": "Stored Current Terrain",
            "StoredFilename": stored,
            "ProfileIndex": "2147483647",
            "ProfileName": "Max",
            "ArrivalDepth": str(arrival_depth),
        })

    tree.write(rasmap_path, encoding="utf-8", xml_declaration=True)


def _inject_terrain_stored_maps(
    rasmap_path: Path,
    plan_hdf_name: str,
    output_folder: str,
    profile: str,
    type_flags: dict[str, bool],
    terrain_name: str,
    arrival_depth: float,
) -> None:
    """Inject a complete Stored Map set bound to one named terrain."""

    tree = ET.parse(rasmap_path)
    root = tree.getroot()
    terrain_names = {
        str(layer.get("Name") or "") for layer in root.findall(".//Terrains/Layer")
    }
    if terrain_name not in terrain_names:
        available = ", ".join(sorted(name for name in terrain_names if name)) or "none"
        raise ValueError(
            f"Terrain {terrain_name!r} is not present in {rasmap_path.name}; "
            f"available terrains: {available}"
        )

    results_elem = root.find(".//Results")
    if results_elem is None:
        results_elem = ET.SubElement(root, "Results", {"Checked": "True"})

    plan_layer = None
    for layer in results_elem.findall("Layer"):
        filename = layer.get("Filename", "").replace("\\", "/")
        if Path(filename).name.lower() == plan_hdf_name.lower():
            plan_layer = layer
            break
    if plan_layer is None:
        plan_layer = ET.SubElement(
            results_elem,
            "Layer",
            {
                "Name": output_folder,
                "Type": "RASResults",
                "Filename": f".\\{plan_hdf_name}",
            },
        )

    def add_layer(
        display_name: str,
        map_type: str,
        profile_name: str,
        *,
        output_mode: str = "Stored Current Terrain",
        arrival: float | None = None,
    ) -> None:
        extension = ".shp" if "Polygon" in output_mode else ".vrt"
        stored = f".\\{output_folder}\\{display_name} ({profile_name}){extension}"
        layer_elem = ET.SubElement(
            plan_layer,
            "Layer",
            {
                "Name": display_name,
                "Type": "RASResultsMap",
                "Checked": "True",
                "Filename": stored,
            },
        )
        attributes = {
            "MapType": map_type,
            "OutputMode": output_mode,
            "StoredFilename": stored,
            "ProfileIndex": "2147483647",
            "ProfileName": profile_name,
            "Terrain": terrain_name,
        }
        if arrival is not None:
            attributes["ArrivalDepth"] = str(arrival)
        ET.SubElement(layer_elem, "MapParameters", attributes)

    for key, (map_type, display_name) in TERRAIN_STORED_MAP_TYPES.items():
        if type_flags.get(key):
            add_layer(
                display_name,
                map_type,
                profile,
                output_mode="Stored Specified Terrain",
            )
    for key, (map_type, display_name) in ADR_MAP_TYPES.items():
        if type_flags.get(key):
            add_layer(
                display_name,
                map_type,
                "Max",
                output_mode="Stored Specified Terrain",
                arrival=arrival_depth,
            )
    if type_flags.get("inundation_boundary"):
        add_layer(
            "Inundation Boundary",
            "depth",
            profile,
            output_mode="Stored Polygon Specified Depth",
        )

    tree.write(rasmap_path, encoding="utf-8", xml_declaration=True)


def _plan_output_folder(ras, plan_number: str) -> str:
    plan_rows = getattr(ras, "plan_df", None)
    if plan_rows is not None and not plan_rows.empty:
        normalized = plan_rows["plan_number"].astype(str).str.zfill(2)
        matches = plan_rows.loc[normalized == str(plan_number).zfill(2)]
        if not matches.empty:
            row = matches.iloc[0]
            for column in ("Short Identifier", "plan_title", "Plan Title"):
                value = row.get(column)
                if value is not None and str(value).strip():
                    return str(value).strip()
    return f"Plan {str(plan_number).zfill(2)}"


def _discover_generated_rasters(
    output_dir: Path,
    type_flags: dict[str, bool],
    run_started: float,
) -> dict[str, list[Path]]:
    discovered: dict[str, list[Path]] = {}
    recent = [
        path
        for path in output_dir.glob("*.tif")
        if path.stat().st_mtime >= run_started and not path.stem.endswith("_cog")
    ]
    for key, prefixes in GENERATED_RASTER_PREFIXES.items():
        if not type_flags.get(key):
            continue
        matches = sorted(
            path
            for path in recent
            if any(path.name.startswith(prefix) for prefix in prefixes)
        )
        if matches:
            discovered[key] = matches
    return discovered


def _generate_plan_maps(
    ras,
    plan_number: str,
    profile: str,
    output_dir: Path,
    terrain_name: Optional[str] = None,
    render_mode: Optional[str] = None,
    timeout: int = 600,
    arrival_depth: float = 0.0,
    ras_version: Optional[str] = None,
    performance: Optional["StoreMapPerformanceOptions"] = None,
    **type_flags,
) -> dict[str, list[Path]]:
    """Generate all requested map types for a plan via RasStoreMapHelper.exe.

    Uses the canonical RasMap API when available and preserves the ras-commander
    0.98.2 RasProcess path as a compatibility fallback.

    Args:
        ras: Initialized RAS project object
        plan_number: Plan number (e.g., "01")
        profile: Profile to map ("Max", "Min", or timestamp)
        output_dir: Directory for output rasters
        terrain_name: Specific terrain name (optional)
        render_mode: Water surface render mode ("horizontal", "sloping", or
            "slopingPretty"). None reads from .rasmap file.
        timeout: Command timeout in seconds
        ras_version: Optional installed HEC-RAS version
        performance: StoreMap execution and memory policy
        **type_flags: Boolean flags for each map type (wse, depth, velocity, etc.)

    Returns:
        Dict mapping our type names to lists of output TIFF paths
    """
    if _supports_optimized_store_maps():
        effective_performance = performance or DEFAULT_LOCAL_MAP_PERFORMANCE
        selected_types = [
            map_type
            for map_type in MAP_TYPE_VARIABLES
            if type_flags.get(map_type, False) and map_type != "recession"
        ]
        summary = RasMap.store_all_maps(
            plan_number=plan_number,
            mode="selected",
            output_path=output_dir,
            profile=profile,
            map_types=selected_types,
            render_mode=render_mode,
            terrain_name=terrain_name,
            arrival_depth=arrival_depth,
            ras_version=ras_version,
            timeout=timeout,
            ras_object=ras,
            performance=effective_performance,
            raise_on_error=True,
        )
        normalized_plan = str(plan_number).zfill(2)
        plan_summary = summary.get("plans", {}).get(normalized_plan)
        if not isinstance(plan_summary, dict):
            raise RuntimeError(
                f"StoreMap summary did not contain plan {normalized_plan}"
            )
        if not plan_summary.get("success", False):
            raise RuntimeError(
                plan_summary.get("error")
                or f"StoreMap generation failed for plan {normalized_plan}"
            )
        return {
            map_type: [Path(path) for path in paths if Path(path).exists()]
            for map_type, paths in plan_summary.get("files_by_type", {}).items()
        }

    if performance is not None:
        raise RuntimeError(
            "performance requires ras-commander>=0.99.0; the installed version "
            "only supports the serial compatibility path"
        )

    # Map our type names to RasProcess.store_maps() parameter names.
    # arrival_time/duration/percent_inundated are passed natively when the
    # installed ras-commander supports them; otherwise handled via the
    # rasmap pre-injection shim below.
    PARAM_MAP = {
        "wse": "wse",
        "depth": "depth",
        "velocity": "velocity",
        "froude": "froude",
        "shear_stress": "shear_stress",
        "depth_x_velocity": "depth_x_velocity",
        "depth_x_velocity_sq": "depth_x_velocity_sq",
        "inundation_boundary": "inundation_boundary",
    }

    native_adr = _store_maps_supports_native_adr()
    adr_requested = {
        key: type_flags.get(key, False) for key in ADR_MAP_TYPES
    }

    # Build kwargs for store_maps
    store_kwargs = {}
    for our_name, param_name in PARAM_MAP.items():
        if param_name and our_name in type_flags:
            store_kwargs[param_name] = type_flags[our_name]

    if native_adr:
        store_kwargs.update(adr_requested)
        store_kwargs["arrival_depth"] = arrival_depth

    rasmap_path = Path(str(ras.project_folder)) / f"{ras.project_name}.rasmap"
    plan_hdf_name = f"{ras.project_name}.p{plan_number}.hdf"
    terrain_override = bool(terrain_name)
    shim_needed = not native_adr and any(adr_requested.values())
    manual_injection = shim_needed or terrain_override
    shim_backup = rasmap_path.with_suffix(".rasmap.adrbak")

    # A leftover backup from a hard-killed prior run must never be restored
    # over the current rasmap (the user may have edited it since). Discard it
    # loudly; this run tracks its own backup via shim_created.
    if shim_backup.exists():
        console.print(
            f"    [yellow]Warning:[/yellow] discarding stale {shim_backup.name} "
            f"from an interrupted previous run"
        )
        shim_backup.unlink()
    shim_created = False

    # Files generated by this run are identified by mtime >= run start (with
    # filesystem-granularity slack) so output globs never claim stale files
    # from previous runs in the same directory.
    run_started = time.time() - 2

    try:
        if manual_injection:
            # Pre-inject ADR stored-map entries into the rasmap so the same
            # StoreAllMaps execution generates them. store_maps' own
            # clear_existing pass would remove injected entries, so clear
            # here first (same semantics) and disable it for the call.
            shutil.copy2(rasmap_path, shim_backup)
            shim_created = True
            RasProcess._remove_stored_maps_from_rasmap(rasmap_path, plan_hdf_name)
            if terrain_override:
                _inject_terrain_stored_maps(
                    rasmap_path,
                    plan_hdf_name,
                    _plan_output_folder(ras, plan_number),
                    profile,
                    type_flags,
                    str(terrain_name),
                    arrival_depth,
                )
                # All requested entries now exist with an explicit Terrain
                # attribute. Keep RasProcess from adding unbound duplicates.
                store_kwargs = {name: False for name in PARAM_MAP.values()}
                if native_adr:
                    store_kwargs.update({name: False for name in ADR_MAP_TYPES})
                    store_kwargs["arrival_depth"] = arrival_depth
            else:
                _inject_adr_stored_maps(
                    rasmap_path,
                    plan_hdf_name,
                    adr_requested,
                    arrival_depth,
                )
            store_kwargs["clear_existing"] = False

        raw_results = RasProcess.store_maps(
            plan_number=plan_number,
            output_path=str(output_dir),
            profile=profile,
            render_mode=render_mode,
            timeout=timeout,
            ras_object=ras,
            **store_kwargs,
        )
    finally:
        if shim_created:
            shutil.copy2(shim_backup, rasmap_path)
            shim_backup.unlink()

    # Normalize results: raw_results is Dict[str, List[Path]]
    # Map RasProcess output keys back to our type names
    REVERSE_MAP = {
        "wse": "wse",
        "depth": "depth",
        "velocity": "velocity",
        "froude": "froude",
        "shear_stress": "shear_stress",
        "depth_x_velocity": "depth_x_velocity",
        "depth_x_velocity_sq": "depth_x_velocity_sq",
        "inundation_boundary": "inundation_boundary",
        "arrival_time": "arrival_time",
        "duration": "duration",
        "percent_inundated": "percent_inundated",
    }

    result = {}
    if isinstance(raw_results, dict):
        for key, paths in raw_results.items():
            norm_key = key.lower().replace(" ", "_")
            mapped = REVERSE_MAP.get(norm_key, norm_key)
            result[mapped] = [Path(p) for p in paths if Path(p).exists()]

    if terrain_override:
        for key, paths in _discover_generated_rasters(
            output_dir,
            type_flags,
            run_started,
        ).items():
            if paths and not result.get(key):
                result[key] = paths

    # Shim path: ADR outputs were moved to output_dir by store_maps'
    # move-loop but are absent from its return dict — collect them by their
    # threshold-labeled display-name prefix (e.g. "Arrival Time (0.1ft hrs)"),
    # restricted to files produced by THIS run so stale outputs from earlier
    # runs at a different threshold are never misattributed.
    if shim_needed:
        for key, (_, display_name) in ADR_MAP_TYPES.items():
            if adr_requested.get(key) and not result.get(key):
                tifs = sorted(
                    p for p in output_dir.glob(f"{display_name} (*.tif")
                    if p.stat().st_mtime >= run_started
                )
                if tifs:
                    result[key] = tifs
                else:
                    console.print(
                        f"    [yellow]Warning:[/yellow] {key} was requested but "
                        f"no output was produced (rasmap injection may not be "
                        f"supported by this ras-commander/HEC-RAS combination)"
                    )

    return result


def _apply_depth_threshold(tif_paths: list[Path], min_depth: float) -> list[Path]:
    """Apply minimum depth threshold to depth rasters.

    Pixels with depth < min_depth are set to NoData.
    Output is written alongside the original with _filtered suffix.
    """
    try:
        processed = []
        for tif in tif_paths:
            out_path = tif.parent / f"{tif.stem}_filtered{tif.suffix}"
            result = RasProcess.apply_depth_threshold(
                input_tiff=str(tif),
                output_tiff=str(out_path),
                min_depth=min_depth,
            )
            if result and Path(result.get("output", "")).exists():
                processed.append(Path(result["output"]))
            else:
                processed.append(out_path if out_path.exists() else tif)
        return processed
    except Exception:
        return tif_paths


def _reproject_tifs(tif_paths: list[Path], target_crs: str) -> list[Path]:
    """Reproject TIFF files to target CRS using rasterio.

    Outputs are written alongside originals with _wgs84 suffix.
    """
    try:
        import rasterio
        from rasterio.crs import CRS
        from rasterio.transform import calculate_default_transform
        from rasterio.warp import reproject, Resampling
    except ImportError:
        console.print("[yellow]Warning:[/yellow] rasterio not available for reprojection")
        return tif_paths

    reprojected = []
    dst_crs = CRS.from_string(target_crs)

    for tif in tif_paths:
        out_path = tif.parent / f"{tif.stem}_wgs84{tif.suffix}"
        try:
            with rasterio.open(tif) as src:
                transform, width, height = calculate_default_transform(
                    src.crs, dst_crs, src.width, src.height, *src.bounds
                )
                kwargs = src.meta.copy()
                kwargs.update({
                    "crs": dst_crs,
                    "transform": transform,
                    "width": width,
                    "height": height,
                })
                with rasterio.open(out_path, "w", **kwargs) as dst:
                    for i in range(1, src.count + 1):
                        reproject(
                            source=rasterio.band(src, i),
                            destination=rasterio.band(dst, i),
                            src_transform=src.transform,
                            src_crs=src.crs,
                            dst_transform=transform,
                            dst_crs=dst_crs,
                            resampling=Resampling.bilinear,
                        )
            reprojected.append(out_path)
        except Exception:
            reprojected.append(tif)

    return reprojected


def _matching_vrt_source(tif_paths: list[Path]) -> Path | None:
    """Return the RASMapper VRT that mosaics a map's terrain-source TIFFs."""

    if not tif_paths or len({Path(path).parent for path in tif_paths}) != 1:
        return None
    parent = Path(tif_paths[0]).parent
    candidates = []
    for vrt_path in parent.glob("*.vrt"):
        prefix = f"{vrt_path.stem}."
        if all(
            Path(path).stem == vrt_path.stem or Path(path).name.startswith(prefix)
            for path in tif_paths
        ):
            candidates.append(vrt_path)
    return max(candidates, key=lambda path: len(path.stem)) if candidates else None


def _convert_to_cog(tif_paths: list[Path]) -> list[Path]:
    """Convert one result map to a validated Cloud Optimized GeoTIFF.

    Rasterio ships with a compatible GDAL runtime, while a system
    ``gdal_translate`` may be too old to provide the COG driver.  Conversion
    therefore stays inside that runtime and fails loudly rather than silently
    returning a non-COG source TIFF. RASMapper writes one TIFF per terrain
    source plus a VRT mosaic; when that VRT is present, publish the complete
    mosaic as one COG rather than exposing or selecting a source fragment.
    """
    import uuid

    import rasterio
    from rasterio.enums import MaskFlags
    from rasterio.shutil import copy as copy_raster

    sources = [Path(path) for path in tif_paths]
    for source in sources:
        if not source.is_file():
            raise FileNotFoundError(f"Result raster does not exist: {source}")

    vrt_source = _matching_vrt_source(sources)
    if len(sources) > 1 and vrt_source is None:
        raise RuntimeError(
            "Multiple terrain-source rasters were generated but their "
            "RASMapper VRT mosaic could not be identified"
        )

    conversion_sources = [vrt_source] if vrt_source is not None else sources
    converted: list[Path] = []
    for source in conversion_sources:
        assert source is not None

        cog_path = source.parent / f"{source.stem}_cog.tif"
        staged_path = cog_path.with_name(f".{cog_path.name}.{uuid.uuid4().hex}.tmp")
        try:
            copy_raster(
                source,
                staged_path,
                driver="COG",
                compress="ZSTD",
                predictor="FLOATING_POINT",
                blocksize=512,
                overview_resampling="average",
                BIGTIFF="IF_SAFER",
                NUM_THREADS="ALL_CPUS",
            )

            with rasterio.open(staged_path) as source:
                if max(source.width, source.height) > 512 and not source.is_tiled:
                    raise ValueError("large COG is not internally tiled")
                if max(source.width, source.height) > 1024 and not source.overviews(1):
                    raise ValueError("large COG has no internal overviews")
                mask_flags = set(source.mask_flag_enums[0]) if source.mask_flag_enums else set()
                has_mask = (
                    source.nodata is not None
                    or MaskFlags.alpha in mask_flags
                    or MaskFlags.per_dataset in mask_flags
                )
                if not has_mask:
                    raise ValueError("COG has no nodata value or validity mask")

            staged_path.replace(cog_path)
            converted.append(cog_path)
        except Exception as error:
            raise RuntimeError(
                f"COG conversion failed for {source}: {error}"
            ) from error
        finally:
            staged_path.unlink(missing_ok=True)

    return converted
