"""
Result raster generation for ras2cng.

Provides:
- generate_result_maps(): Generate WSE/Depth/Velocity/etc. rasters via RasProcess.exe
- MapResult: Structured output from map generation
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from rich.console import Console

from ras_commander import init_ras_project, RasProcess

console = Console()


@dataclass
class MapResult:
    """Result of map generation for a single plan."""
    plan_id: str
    plan_number: str
    map_types: dict[str, list[Path]] = field(default_factory=dict)  # {"depth": [Path(...)]}
    output_dir: Path = field(default_factory=lambda: Path("."))
    errors: list[str] = field(default_factory=list)


# Map type names to RasProcess store_maps variable names
MAP_TYPE_VARIABLES = {
    "wse": "Water Surface",
    "depth": "Depth",
    "velocity": "Velocity",
    "froude": "Froude Number",
    "shear_stress": "Shear Stress",
    "depth_x_velocity": "Depth x Velocity",
    "arrival_time": "Arrival Time",
    "duration": "Duration",
    "recession": "Recession",
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
    arrival_time: bool = False,
    duration: bool = False,
    recession: bool = False,
    terrain_name: Optional[str] = None,
    ras_version: Optional[str] = None,
    rasprocess_path: Optional[Path] = None,
    min_depth: float = 0.0,
    reproject_wgs84: bool = False,
    convert_cog: bool = False,
    timeout: int = 10800,
    skip_errors: bool = True,
) -> list[MapResult]:
    """Generate result rasters for plans in a HEC-RAS project.

    Uses RasProcess.store_maps() to generate raw TIFs from completed plan HDF files.

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
        arrival_time: Generate Arrival Time rasters
        duration: Generate Duration rasters
        recession: Generate Recession rasters
        terrain_name: Specific terrain name from rasmap to use for mapping
        ras_version: HEC-RAS version (auto-detected if None)
        rasprocess_path: Path to RasProcess.exe (required on Linux/Wine)
        min_depth: Minimum depth threshold for depth rasters (default: 0.0)
        reproject_wgs84: Reproject output rasters to WGS84
        convert_cog: Convert output to Cloud Optimized GeoTIFF
        timeout: Per-plan timeout in seconds (default: 10800 = 3 hours)
        skip_errors: If True, log and continue past errors

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

    # Configure RasProcess
    _configure_rasprocess(rasprocess_path, ras_version)

    # Initialize project
    ras = init_ras_project(project_dir, ras_object="new", load_results_summary=True)

    # Build list of requested map types
    requested_types = _build_requested_types(
        wse=wse, depth=depth, velocity=velocity,
        froude=froude, shear_stress=shear_stress,
        depth_x_velocity=depth_x_velocity,
        arrival_time=arrival_time, duration=duration,
        recession=recession,
    )

    if not requested_types:
        console.print("[yellow]Warning:[/yellow] No map types selected")
        return []

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

            result_dict = _generate_plan_maps(
                ras=ras,
                plan_number=plan_num,
                profile=profile,
                output_dir=plan_output,
                terrain_name=terrain_name,
                timeout=timeout,
                **type_flags,
            )

            for map_type in requested_types:
                tif_paths = result_dict.get(map_type, [])

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
        # configure_wine expects ras_install_dir (directory containing RasProcess.exe)
        # Use suffix check instead of is_file() since path may not exist on current OS
        ras_install_dir = rasprocess_path.parent if rasprocess_path.suffix.lower() == ".exe" else rasprocess_path
        try:
            RasProcess.configure_wine(ras_install_dir=str(ras_install_dir))
        except AttributeError:
            # Older ras-commander versions may not have configure_wine
            pass

    if ras_version:
        try:
            RasProcess.find_rasprocess(version=ras_version)
        except Exception:
            pass  # Will fail later with a clearer error if exe not found


def _build_requested_types(**kwargs) -> list[str]:
    """Build list of requested map type names from boolean flags."""
    return [name for name, enabled in kwargs.items() if enabled]


def _generate_plan_maps(
    ras,
    plan_number: str,
    profile: str,
    output_dir: Path,
    terrain_name: Optional[str] = None,
    timeout: int = 600,
    **type_flags,
) -> dict[str, list[Path]]:
    """Generate all requested map types for a plan in a single RasProcess call.

    Uses RasProcess.store_maps() which calls RasProcess.exe StoreAllMaps
    to render all requested variables to GeoTIFF rasters at once.

    Args:
        ras: Initialized RAS project object
        plan_number: Plan number (e.g., "01")
        profile: Profile to map ("Max", "Min", or timestamp)
        output_dir: Directory for output rasters
        terrain_name: Specific terrain name (optional)
        timeout: Command timeout in seconds
        **type_flags: Boolean flags for each map type (wse, depth, velocity, etc.)

    Returns:
        Dict mapping our type names to lists of output TIFF paths
    """
    # Map our type names to RasProcess.store_maps() parameter names
    PARAM_MAP = {
        "wse": "wse",
        "depth": "depth",
        "velocity": "velocity",
        "froude": "froude",
        "shear_stress": "shear_stress",
        "depth_x_velocity": "depth_x_velocity",
        "arrival_time": None,  # Not directly supported by store_maps
        "duration": None,
        "recession": None,
    }

    # Build kwargs for store_maps
    store_kwargs = {}
    for our_name, param_name in PARAM_MAP.items():
        if param_name and our_name in type_flags:
            store_kwargs[param_name] = type_flags[our_name]

    raw_results = RasProcess.store_maps(
        plan_number=plan_number,
        output_folder=str(output_dir),
        profile=profile,
        timeout=timeout,
        ras_object=ras,
        **store_kwargs,
    )

    # Normalize results: raw_results is Dict[str, List[Path]]
    # Map RasProcess output keys back to our type names
    REVERSE_MAP = {
        "wse": "wse",
        "depth": "depth",
        "velocity": "velocity",
        "froude": "froude",
        "shear_stress": "shear_stress",
        "depth_x_velocity": "depth_x_velocity",
    }

    result = {}
    if isinstance(raw_results, dict):
        for key, paths in raw_results.items():
            norm_key = key.lower().replace(" ", "_")
            mapped = REVERSE_MAP.get(norm_key, norm_key)
            result[mapped] = [Path(p) for p in paths if Path(p).exists()]

    return result


def _apply_depth_threshold(tif_paths: list[Path], min_depth: float) -> list[Path]:
    """Apply minimum depth threshold to depth rasters.

    Pixels with depth < min_depth are set to NoData.
    """
    try:
        processed = []
        for tif in tif_paths:
            result = RasProcess.apply_depth_threshold(
                str(tif), min_depth=min_depth
            )
            processed.append(Path(result) if result else tif)
        return processed
    except Exception:
        return tif_paths


def _reproject_tifs(tif_paths: list[Path], target_crs: str) -> list[Path]:
    """Reproject TIFF files to target CRS using rasterio.

    Outputs are written alongside originals with _wgs84 suffix.
    """
    try:
        import numpy as np
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


def _convert_to_cog(tif_paths: list[Path]) -> list[Path]:
    """Convert TIFFs to Cloud Optimized GeoTIFF using gdal_translate.

    Outputs are written alongside originals with _cog suffix.
    """
    import subprocess

    converted = []
    for tif in tif_paths:
        cog_path = tif.parent / f"{tif.stem}_cog{tif.suffix}"
        try:
            subprocess.run(
                ["gdal_translate", "-of", "COG", str(tif), str(cog_path)],
                check=True, capture_output=True,
            )
            converted.append(cog_path)
        except Exception:
            converted.append(tif)

    return converted
