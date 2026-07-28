"""
Terrain discovery, consolidation, and downsampling for ras2cng.

Provides:
- discover_terrains(): Discover terrain layers from rasmap in priority order
- consolidate_terrain(): Merge multiple terrain TIFFs, optionally downsample,
  and create a new HEC-RAS terrain HDF via RasProcess.exe
- export_modified_terrain(): Export terrain with modifications as GeoTIFF
- export_mannings_raster(): Export final Manning's n values as GeoTIFF
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
import math
import re
from pathlib import Path
from typing import Optional

from rich.console import Console

console = Console()


def _glob_tifs(directory: Path, pattern: str = "*.tif") -> list[Path]:
    """Glob for TIF files, deduplicating for case-insensitive filesystems."""
    return sorted(set(
        list(directory.glob(pattern))
        + list(directory.glob(pattern.replace(".tif", ".TIF")))
    ))


@dataclass
class TerrainInfo:
    """Information about a single terrain layer discovered from a RAS project."""
    name: str
    hdf_path: Optional[Path] = None
    hdf_exists: bool = False
    tif_files: list[Path] = field(default_factory=list)
    crs: Optional[str] = None
    resolution: Optional[str] = None       # e.g. "50.0 x 50.0 ft"
    bounds: Optional[tuple] = None         # (xmin, ymin, xmax, ymax)
    total_size_mb: float = 0.0


@dataclass(frozen=True)
class TerrainResolutionDecision:
    """Recorded no-upsample resolution decision for one named terrain."""

    native_resolutions: tuple[float, ...]
    target_resolution: float
    minimum_resolution: float
    horizontal_units: str
    factors: tuple[float, ...]
    mixed_native_resolution: bool
    policy: str = "whole-native-multiple-no-upsample"


def select_terrain_resolution(
    native_resolutions: list[float] | tuple[float, ...],
    *,
    requested: Optional[float] = None,
    horizontal_units: str = "Feet",
    minimum_feet: float = 5.0,
) -> TerrainResolutionDecision:
    """Choose a publication cell size without inventing finer terrain data.

    A single native grid finer than five feet is reduced to the smallest
    whole-number multiple of its native cell size that reaches five feet.
    Native grids already at or above that threshold retain their resolution.
    Mixed native grids require an explicit target so the resampling decision is
    visible and reviewable. Their target must be a whole-number multiple of the
    coarsest source grid; requiring a common multiple of unlike source grids
    (for example, 2-foot and 1-meter tiles) would force an unusably coarse map.
    """

    values = tuple(float(value) for value in native_resolutions)
    if not values or any(not math.isfinite(value) or value <= 0 for value in values):
        raise ValueError("Terrain native resolutions must be finite positive numbers")

    unit_key = horizontal_units.strip().lower()
    if unit_key.startswith(("foot", "feet", "ft", "us survey foot")):
        minimum = float(minimum_feet)
        normalized_units = "Feet"
    elif unit_key.startswith(("met", "m")):
        minimum = float(minimum_feet) / 3.280839895013123
        normalized_units = "Meters"
    else:
        raise ValueError(
            "Terrain horizontal units must be Feet or Meters so the five-foot "
            "publication floor can be enforced"
        )

    first = values[0]
    mixed = any(not math.isclose(value, first, rel_tol=1e-7, abs_tol=1e-9) for value in values[1:])
    if mixed and requested is None:
        raise ValueError(
            "Mixed native terrain resolutions require an explicit target_resolution"
        )

    if requested is None:
        if first >= minimum or math.isclose(first, minimum, rel_tol=1e-7, abs_tol=1e-9):
            target = first
        else:
            target = math.ceil((minimum / first) - 1e-9) * first
    else:
        target = float(requested)
        if not math.isfinite(target) or target <= 0:
            raise ValueError("target_resolution must be a finite positive number")

    required_minimum = max(minimum, max(values))
    if target < required_minimum and not math.isclose(
        target, required_minimum, rel_tol=1e-7, abs_tol=1e-9
    ):
        raise ValueError(
            f"target_resolution {target:g} would upsample terrain or violate the "
            f"{minimum:g} {normalized_units} publication floor"
        )

    factors = tuple(target / value for value in values)
    reference_resolution = max(values) if mixed else first
    reference_factor = target / reference_resolution
    if not math.isclose(
        reference_factor, round(reference_factor), rel_tol=1e-7, abs_tol=1e-7
    ):
        qualifier = "the coarsest native cell size" if mixed else "the native cell size"
        raise ValueError(
            f"target_resolution must be a whole-number multiple of {qualifier}"
        )

    return TerrainResolutionDecision(
        native_resolutions=values,
        target_resolution=target,
        minimum_resolution=minimum,
        horizontal_units=normalized_units,
        factors=factors,
        mixed_native_resolution=mixed,
        policy=(
            "whole-coarsest-native-multiple-no-upsample"
            if mixed
            else "whole-native-multiple-no-upsample"
        ),
    )


def inspect_terrain_sources(tif_files: list[Path]) -> list[dict]:
    """Return the source inventory recorded beside a consolidated terrain."""

    import rasterio

    inventory: list[dict] = []
    for priority, tif in enumerate(tif_files):
        tif = Path(tif)
        if not tif.is_file():
            raise FileNotFoundError(f"Terrain source does not exist: {tif}")
        with rasterio.open(tif) as src:
            if src.crs is None:
                raise ValueError(f"Terrain source has no CRS: {tif}")
            inventory.append(
                {
                    "priority": priority,
                    "path": str(tif.resolve()),
                    "size_bytes": tif.stat().st_size,
                    "crs": str(src.crs),
                    "resolution_x": abs(float(src.res[0])),
                    "resolution_y": abs(float(src.res[1])),
                    "bounds": [float(value) for value in src.bounds],
                    "width": int(src.width),
                    "height": int(src.height),
                    "dtype": str(src.dtypes[0]),
                    "nodata": None if src.nodata is None else float(src.nodata),
                }
            )
    return inventory


def consolidate_terrain_files(
    tif_files: list[Path],
    output_dir: Path,
    *,
    terrain_name: str = "Consolidated",
    source_terrain_name: Optional[str] = None,
    downsample_factor: Optional[float] = None,
    target_resolution: Optional[float] = None,
    horizontal_units: str = "Feet",
    source_paths: Optional[list[str | Path]] = None,
) -> Path:
    """Consolidate an explicit, priority-ordered terrain TIFF mosaic.

    This entry point supports projects whose RAS Mapper paths cannot be resolved
    on the processing host. The first source wins where valid pixels overlap,
    and the normal no-upsample publication policy is always enforced.
    """

    sources = [Path(path) for path in tif_files]
    if not sources:
        raise ValueError("No TIFF files provided for terrain consolidation")

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    source_inventory = inspect_terrain_sources(sources)
    if source_paths is not None:
        if len(source_paths) != len(source_inventory):
            raise ValueError("source_paths must contain one display path per TIFF source")
        for item, display_path in zip(source_inventory, source_paths):
            item["path"] = Path(display_path).as_posix()

    native_resolutions = [
        max(item["resolution_x"], item["resolution_y"])
        for item in source_inventory
    ]
    if downsample_factor is not None and target_resolution is not None:
        raise ValueError("Use either downsample_factor or target_resolution, not both")

    requested_resolution = target_resolution
    if downsample_factor is not None:
        if downsample_factor < 1:
            raise ValueError("downsample_factor must be at least 1; upsampling is prohibited")
        requested_resolution = max(native_resolutions) * float(downsample_factor)

    decision = select_terrain_resolution(
        native_resolutions,
        requested=requested_resolution,
        horizontal_units=horizontal_units,
    )

    console.print(f"[bold]Terrain consolidation:[/bold] {len(sources)} TIFF(s)")
    merged_tif = output_dir / f"{terrain_name}_merged.tif"
    _merge_tifs(
        sources,
        merged_tif,
        target_resolution=decision.target_resolution,
    )
    console.print(f"  Merged -> {merged_tif.name}")

    provenance = {
        "schema": "ras2cng.terrain-consolidation/v1",
        "terrain_name": source_terrain_name or terrain_name,
        "output_name": terrain_name,
        "source_priority": "first-valid-value-wins",
        "resampling": "bilinear",
        "resolution": asdict(decision),
        "sources": source_inventory,
        "output": merged_tif.name,
    }
    provenance_path = output_dir / f"{terrain_name}_terrain-provenance.json"
    provenance_path.write_text(json.dumps(provenance, indent=2) + "\n", encoding="utf-8")
    console.print(f"  Provenance -> {provenance_path.name}")
    return merged_tif


def discover_terrains(project_path: Path) -> list[TerrainInfo]:
    """Discover terrain layers from rasmap in priority order.

    Uses RasMap.get_terrain_names() + rasmap_df['terrain_hdf_path'].
    For each terrain HDF, discovers associated .tif files in same directory.

    Args:
        project_path: Path to .prj file or project directory

    Returns:
        List of TerrainInfo in rasmap priority order
    """
    from ras2cng.project import resolve_project_path
    from ras_commander import init_ras_project

    project_dir, prj_file = resolve_project_path(Path(project_path))
    ras = init_ras_project(project_dir, ras_object="new", load_results_summary=False)

    terrains: list[TerrainInfo] = []

    # Try to get terrain names from rasmap
    terrain_names = _get_terrain_names_safe(project_dir)

    # Try to get terrain HDF paths from rasmap_df
    terrain_hdf_paths: dict[str, Path] = {}
    if ras.rasmap_df is not None and not ras.rasmap_df.empty:
        if "terrain_hdf_path" in ras.rasmap_df.columns:
            for _, row in ras.rasmap_df.iterrows():
                hdf_p = row.get("terrain_hdf_path")
                name = row.get("terrain_name", "")
                if hdf_p and str(hdf_p).strip():
                    terrain_hdf_paths[str(name)] = Path(str(hdf_p))

    # If no rasmap terrain info, fall back to scanning Terrain/ directory
    if not terrain_names and not terrain_hdf_paths:
        terrain_dir = project_dir / "Terrain"
        if terrain_dir.exists():
            hdf_files = sorted(terrain_dir.glob("*.hdf"))
            for hdf_f in hdf_files:
                name = hdf_f.stem
                tif_files = _discover_tifs_for_hdf(hdf_f)
                info = _get_raster_info(tif_files)
                terrains.append(TerrainInfo(
                    name=name,
                    hdf_path=hdf_f,
                    hdf_exists=hdf_f.exists(),
                    tif_files=tif_files,
                    crs=info.get("crs"),
                    resolution=info.get("resolution"),
                    bounds=info.get("bounds"),
                    total_size_mb=sum(f.stat().st_size for f in tif_files if f.exists()) / (1024 * 1024),
                ))
            # Also check for standalone TIFs
            if not hdf_files:
                tif_files = _glob_tifs(terrain_dir)
                if tif_files:
                    info = _get_raster_info(tif_files)
                    terrains.append(TerrainInfo(
                        name="Terrain",
                        tif_files=tif_files,
                        crs=info.get("crs"),
                        resolution=info.get("resolution"),
                        bounds=info.get("bounds"),
                        total_size_mb=sum(f.stat().st_size for f in tif_files if f.exists()) / (1024 * 1024),
                    ))
        return terrains

    # Build terrain info from rasmap data
    seen_names = set()
    for name in terrain_names or list(terrain_hdf_paths.keys()):
        if name in seen_names:
            continue
        seen_names.add(name)

        hdf_path = terrain_hdf_paths.get(name)
        if hdf_path and not hdf_path.is_absolute():
            hdf_path = project_dir / hdf_path

        tif_files = _discover_tifs_for_hdf(hdf_path) if hdf_path else []
        # Also check Terrain/ directory for TIFs matching the name
        if not tif_files:
            terrain_dir = project_dir / "Terrain"
            if terrain_dir.exists():
                all_tifs = _glob_tifs(terrain_dir)
                # Exact stem match: "Terrain" matches "Terrain.tif" and
                # "Terrain_tile2.tif" but NOT "TerrainWithChannel.tif"
                tif_files = sorted(
                    f for f in all_tifs
                    if _stem_matches_name(f.stem, name)
                )

        info = _get_raster_info(tif_files)
        terrains.append(TerrainInfo(
            name=name,
            hdf_path=hdf_path,
            hdf_exists=hdf_path.exists() if hdf_path else False,
            tif_files=tif_files,
            crs=info.get("crs"),
            resolution=info.get("resolution"),
            bounds=info.get("bounds"),
            total_size_mb=sum(f.stat().st_size for f in tif_files if f.exists()) / (1024 * 1024),
        ))

    return terrains


def consolidate_terrain(
    project_path: Path,
    output_dir: Path,
    *,
    terrain_name: str = "Consolidated",
    downsample_factor: Optional[float] = None,
    target_resolution: Optional[float] = None,
    terrain_names: Optional[list[str]] = None,
    horizontal_units: str = "Feet",
    units: str = "Feet",
    ras_version: str = "6.6",
    create_hdf: bool = True,
    register_rasmap: bool = True,
) -> Path:
    """Consolidate project terrains and create a new HEC-RAS terrain HDF.

    Full pipeline:
    1. Discover terrain TIFFs from rasmap (priority ordered)
    2. Merge via rasterio.merge.merge(method='first') -- first wins in overlaps
    3. Optionally downsample (reduce resolution)
    4. Create HEC-RAS terrain HDF via RasTerrain.create_terrain_from_rasters()
    5. Register new terrain in rasmap via RasMap.add_terrain_layer()

    Steps 4-5 require RasProcess.exe. If create_hdf=False, only produces
    the merged TIFF (useful for exporting to cloud-native COG pipeline).

    Args:
        project_path: Path to .prj file or project directory
        output_dir: Directory for output terrain files
        terrain_name: Name for the consolidated terrain (default: "Consolidated")
        downsample_factor: Factor to reduce resolution (2.0 = half resolution)
        target_resolution: Target cell size in project units (overrides downsample_factor)
        terrain_names: One named terrain to consolidate. Required when the
            project contains multiple named terrains.
        horizontal_units: Horizontal raster units (Feet or Meters), used to
            enforce the five-foot publication floor.
        units: Vertical units "Feet" or "Meters"
        ras_version: HEC-RAS version for RasProcess.exe
        create_hdf: If True, create HEC-RAS terrain HDF (requires RasProcess.exe)
        register_rasmap: If True, register new terrain in project rasmap

    Returns:
        Path to consolidated terrain HDF (if create_hdf) or TIFF (if not)
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Step 1: Discover terrains
    terrains = discover_terrains(project_path)
    if not terrains:
        raise ValueError("No terrain data found in project")

    # Select exactly one named terrain. TIFF members of that terrain are a
    # mosaic; separate named terrain surfaces are distinct model inputs.
    if terrain_names:
        if len(terrain_names) != 1:
            raise ValueError(
                "Consolidate one named terrain at a time; distinct named terrains "
                "must never be merged"
            )
        name_set = set(terrain_names)
        terrains = [t for t in terrains if t.name in name_set]
        if not terrains:
            raise ValueError(f"No terrains matching names: {terrain_names}")
    elif len(terrains) > 1:
        raise ValueError(
            "Multiple named terrains were discovered. Pass terrain_names=[<name>] "
            "and publish each named surface separately."
        )

    # Collect all TIF files in priority order
    all_tifs: list[Path] = []
    for t in terrains:
        all_tifs.extend(t.tif_files)

    if not all_tifs:
        raise ValueError("No TIFF files found for terrain consolidation")

    # Step 2: Merge TIFFs and record the publication-resolution decision.
    final_tif = consolidate_terrain_files(
        all_tifs,
        output_dir,
        terrain_name=terrain_name,
        source_terrain_name=terrains[0].name,
        downsample_factor=downsample_factor,
        target_resolution=target_resolution,
        horizontal_units=horizontal_units,
    )

    # Step 4: Create HEC-RAS terrain HDF (requires RasProcess.exe)
    if not create_hdf:
        console.print(f"[green]OK[/green] TIFF-only mode: {final_tif}")
        return final_tif

    try:
        from ras_commander import RasTerrain

        from ras2cng.project import resolve_project_path
        project_dir, prj_file = resolve_project_path(Path(project_path))

        terrain_hdf = RasTerrain.create_terrain_from_rasters(
            input_rasters=[final_tif],
            output_folder=output_dir,
            terrain_name=terrain_name,
            units=units,
            hecras_version=ras_version,
        )
        terrain_hdf = Path(terrain_hdf)
        console.print(f"  HEC-RAS terrain HDF -> {terrain_hdf.name}")
    except ImportError:
        console.print("[yellow]Warning:[/yellow] RasTerrain not available; returning TIFF only")
        return final_tif
    except Exception as e:
        console.print(f"[yellow]Warning:[/yellow] Terrain HDF creation failed: {e}")
        console.print("  Returning merged TIFF instead")
        return final_tif

    # Step 5: Register in rasmap
    if register_rasmap:
        try:
            from ras_commander import RasMap

            rasmap_path = project_dir / f"{prj_file.stem}.rasmap"
            if rasmap_path.exists():
                RasMap.add_terrain_layer(
                    terrain_hdf=terrain_hdf,
                    rasmap_path=rasmap_path,
                    layer_name=terrain_name,
                )
                console.print(f"  Registered in rasmap: {rasmap_path.name}")
        except Exception as e:
            console.print(f"[yellow]Warning:[/yellow] Could not register terrain in rasmap: {e}")

    console.print(f"[green]OK[/green] Terrain consolidation complete: {terrain_hdf}")
    return terrain_hdf


def consolidate_project_terrains(
    project_path: Path,
    output_dir: Path,
    *,
    target_resolutions: Optional[dict[str, float]] = None,
    horizontal_units: str = "Feet",
) -> dict[str, Path]:
    """Consolidate TIFF members separately for every named project terrain."""

    terrains = discover_terrains(project_path)
    if not terrains:
        raise ValueError("No terrain data found in project")
    targets = target_resolutions or {}
    unknown = sorted(set(targets) - {terrain.name for terrain in terrains})
    if unknown:
        raise ValueError(f"Target resolutions reference unknown terrains: {unknown}")

    outputs: dict[str, Path] = {}
    used_names: set[str] = set()
    for terrain in terrains:
        slug = re.sub(r"[^A-Za-z0-9]+", "_", terrain.name).strip("_") or "Terrain"
        output_name = slug
        suffix = 2
        while output_name.lower() in used_names:
            output_name = f"{slug}_{suffix}"
            suffix += 1
        used_names.add(output_name.lower())
        outputs[terrain.name] = consolidate_terrain(
            project_path,
            output_dir,
            terrain_name=output_name,
            terrain_names=[terrain.name],
            target_resolution=targets.get(terrain.name),
            horizontal_units=horizontal_units,
            create_hdf=False,
            register_rasmap=False,
        )
    return outputs


def extract_terrain_modification_layers(
    terrain_hdf_path: Path,
    *,
    crs: Optional[str] = None,
) -> dict[str, object]:
    """Read RASMapper terrain modification vectors from a terrain HDF."""

    import geopandas as gpd
    import h5py
    import numpy as np
    from shapely.geometry import LineString, Point, Polygon
    from ras_commander.hdf import HdfBase
    from ras_commander.terrain import RasTerrainModWriter

    terrain_hdf_path = Path(terrain_hdf_path)
    if not terrain_hdf_path.is_file():
        raise FileNotFoundError(f"Terrain HDF does not exist: {terrain_hdf_path}")
    source_crs = crs
    if source_crs is None:
        try:
            source_crs = HdfBase.get_projection(terrain_hdf_path)
        except Exception:
            source_crs = None
    if not source_crs:
        raise ValueError(
            f"Terrain modifications require a validated CRS: {terrain_hdf_path}"
        )

    metadata = RasTerrainModWriter.list_modifications(terrain_hdf_path)
    metadata_by_name = {
        str(row["name"]): row.to_dict()
        for _, row in metadata.iterrows()
    }
    lines: list[dict] = []
    polygons: list[dict] = []
    control_points: list[dict] = []
    with h5py.File(terrain_hdf_path, "r") as hdf:
        modifications = hdf.get("Modifications")
        if modifications is None:
            return {
                "terrain_modification_lines": gpd.GeoDataFrame(geometry=[], crs=source_crs),
                "terrain_modification_polygons": gpd.GeoDataFrame(geometry=[], crs=source_crs),
                "terrain_modification_control_points": gpd.GeoDataFrame(geometry=[], crs=source_crs),
            }
        for name, group in modifications.items():
            properties = _terrain_modification_properties(name, group, metadata_by_name.get(name, {}))
            if "Polyline Points" in group:
                points = np.asarray(group["Polyline Points"][:], dtype="float64")
                if points.ndim == 2 and points.shape[1] >= 2 and len(points) >= 2:
                    lines.append({**properties, "geometry": LineString(points[:, :2])})
            elif "Polygon Points" in group and "Polygon Parts" in group:
                points = np.asarray(group["Polygon Points"][:], dtype="float64")
                parts = np.asarray(group["Polygon Parts"][:], dtype="int64")
                rings = [
                    points[start:start + count, :2]
                    for start, count in parts[:, :2]
                    if count >= 3
                ]
                if rings:
                    polygons.append(
                        {
                            **properties,
                            "boundary_elevation_min": _finite_dataset_stat(group, "Boundary Elevations", "min"),
                            "boundary_elevation_max": _finite_dataset_stat(group, "Boundary Elevations", "max"),
                            "geometry": Polygon(rings[0], holes=rings[1:]),
                        }
                    )

            controls = group.get("Control Points")
            if controls is not None and "Points" in controls:
                points = np.asarray(controls["Points"][:], dtype="float64")
                elevations = (
                    np.asarray(controls["Elevations"][:], dtype="float64")
                    if "Elevations" in controls
                    else np.full(len(points), np.nan)
                )
                names = _terrain_control_names(controls, len(points))
                for index, point in enumerate(points):
                    if len(point) < 2:
                        continue
                    control_points.append(
                        {
                            "parent_modification": name,
                            "control_name": names[index],
                            "elevation": float(elevations[index]) if np.isfinite(elevations[index]) else None,
                            "modification_mode": properties.get("modification_mode"),
                            "geometry": Point(float(point[0]), float(point[1])),
                        }
                    )

    return {
        "terrain_modification_lines": gpd.GeoDataFrame(
            lines if lines else {"geometry": []}, geometry="geometry", crs=source_crs
        ),
        "terrain_modification_polygons": gpd.GeoDataFrame(
            polygons if polygons else {"geometry": []}, geometry="geometry", crs=source_crs
        ),
        "terrain_modification_control_points": gpd.GeoDataFrame(
            control_points if control_points else {"geometry": []},
            geometry="geometry",
            crs=source_crs,
        ),
    }


def export_terrain_modifications(
    terrain_hdf_path: Path,
    output_dir: Path,
    *,
    crs: Optional[str] = None,
) -> dict[str, Path]:
    """Export each terrain-modification feature class as GeoParquet."""

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}
    for layer_name, frame in extract_terrain_modification_layers(
        terrain_hdf_path, crs=crs
    ).items():
        if frame.empty:
            continue
        output_path = output_dir / f"{layer_name}.parquet"
        frame.to_parquet(output_path, compression="zstd", index=False)
        written[layer_name] = output_path
    return written


def extract_terrain_source_footprints(
    tif_files: list[Path],
    *,
    out_crs: Optional[str] = None,
):
    """Build one queryable footprint polygon per native terrain TIFF member."""

    import geopandas as gpd
    import pandas as pd
    import rasterio
    from shapely.geometry import box

    frames = []
    for priority, path in enumerate(tif_files):
        path = Path(path)
        with rasterio.open(path) as source:
            if source.crs is None:
                raise ValueError(f"Terrain source has no CRS: {path}")
            frame = gpd.GeoDataFrame(
                {
                    "priority": [priority],
                    "source_file": [path.name],
                    "size_bytes": [path.stat().st_size],
                    "source_crs": [source.crs.to_string()],
                    "resolution_x": [abs(float(source.res[0]))],
                    "resolution_y": [abs(float(source.res[1]))],
                    "width": [int(source.width)],
                    "height": [int(source.height)],
                    "dtype": [str(source.dtypes[0])],
                    "nodata": [None if source.nodata is None else float(source.nodata)],
                },
                geometry=[box(*source.bounds)],
                crs=source.crs,
            )
            if out_crs:
                frame = frame.to_crs(out_crs)
            frames.append(frame)
    if not frames:
        return gpd.GeoDataFrame(geometry=[], crs=out_crs)
    target_crs = frames[0].crs
    normalized = [
        frame if frame.crs == target_crs else frame.to_crs(target_crs)
        for frame in frames
    ]
    return gpd.GeoDataFrame(
        pd.concat(normalized, ignore_index=True),
        geometry="geometry",
        crs=target_crs,
    )


def export_terrain_source_footprints(
    tif_files: list[Path],
    output_path: Path,
    *,
    out_crs: Optional[str] = None,
) -> Path:
    """Write native terrain source footprints as GeoParquet."""

    footprints = extract_terrain_source_footprints(tif_files, out_crs=out_crs)
    if footprints.empty:
        raise ValueError("No terrain TIFF source footprints were available")
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    footprints.to_parquet(output_path, compression="zstd", index=False)
    return output_path


def _terrain_modification_properties(name: str, group, metadata: dict) -> dict:
    return {
        "name": name,
        "feature_type": str(metadata.get("type") or _decode_hdf_value(group.attrs.get("Type", ""))),
        "subtype": str(metadata.get("subtype") or _decode_hdf_value(group.attrs.get("Subtype", ""))),
        "priority": int(metadata.get("priority", group.attrs.get("Priority", 0))),
        "modification_mode": metadata.get("modification_mode"),
        "width": metadata.get("width"),
        "left_slope": metadata.get("left_slope"),
        "right_slope": metadata.get("right_slope"),
        "max_extent": metadata.get("max_extent"),
        "profile_points": metadata.get("profile_points"),
        "boundary_elevation_method": _decode_hdf_value(
            group.attrs.get("Boundary Elevation Method", "")
        ),
    }


def _terrain_control_names(group, count: int) -> list[str]:
    if "Attributes" not in group or not group["Attributes"].dtype.names:
        return [f"Control {index + 1}" for index in range(count)]
    attributes = group["Attributes"][:]
    if "Name" not in attributes.dtype.names:
        return [f"Control {index + 1}" for index in range(count)]
    return [
        str(_decode_hdf_value(attributes[index]["Name"]))
        if index < len(attributes)
        else f"Control {index + 1}"
        for index in range(count)
    ]


def _finite_dataset_stat(group, name: str, operation: str) -> Optional[float]:
    import numpy as np

    if name not in group:
        return None
    values = np.asarray(group[name][:], dtype="float64")
    values = values[np.isfinite(values)]
    if not len(values):
        return None
    return float(values.min() if operation == "min" else values.max())


def _decode_hdf_value(value):
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace").strip("\x00 ")
    return value.item() if hasattr(value, "item") else value


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _get_terrain_names_safe(project_dir: Path) -> list[str]:
    """Try to get terrain names from rasmap, returning empty list on failure."""
    try:
        from ras_commander import RasMap

        rasmap_files = list(project_dir.glob("*.rasmap"))
        if not rasmap_files:
            return []
        return RasMap.get_terrain_names(str(rasmap_files[0]))
    except Exception:
        return []


def _stem_matches_name(stem: str, name: str) -> bool:
    """Check if a TIF file stem matches a terrain name exactly.

    Matches "Terrain.muncie_clip" and "Terrain_tile2" for name "Terrain",
    but NOT "TerrainWithChannel" (which is a different terrain).

    The stem must either equal the name, or start with the name followed
    by a non-alphanumeric separator (dot, underscore, dash, space).
    """
    stem_lower = stem.lower()
    name_lower = name.lower()

    if stem_lower == name_lower:
        return True

    if stem_lower.startswith(name_lower):
        # Character after the name must be a separator, not alphanumeric
        next_char = stem_lower[len(name_lower)]
        return not next_char.isalnum()

    return False


def _discover_tifs_for_hdf(hdf_path: Optional[Path]) -> list[Path]:
    """Find TIFF files associated with a terrain HDF file.

    Looks for TIFFs in the same directory as the HDF, matching the
    HDF stem pattern (e.g. Terrain50.hdf -> Terrain50*.tif).
    """
    if hdf_path is None or not hdf_path.exists():
        return []

    parent = hdf_path.parent
    stem = hdf_path.stem

    # Look for TIFFs matching the HDF stem (deduplicate for case-insensitive FS)
    tifs = _glob_tifs(parent, f"{stem}*.tif")

    # If no matching TIFs, try all TIFs in the directory
    if not tifs:
        tifs = _glob_tifs(parent)

    return tifs


def _get_raster_info(tif_files: list[Path]) -> dict:
    """Read CRS, resolution, and bounds from the first available TIFF.

    Uses rasterio (lazy import, optional dependency).
    """
    if not tif_files:
        return {}

    try:
        import rasterio
    except ImportError:
        return {}

    for tif in tif_files:
        if not tif.exists():
            continue
        try:
            with rasterio.open(tif) as src:
                crs_str = None
                if src.crs:
                    epsg = src.crs.to_epsg()
                    crs_str = f"EPSG:{epsg}" if epsg else str(src.crs)[:60]

                res_x, res_y = abs(src.res[0]), abs(src.res[1])
                resolution = f"{res_x:.1f} x {res_y:.1f}"

                return {
                    "crs": crs_str,
                    "resolution": resolution,
                    "bounds": src.bounds,
                }
        except Exception:
            continue

    return {}


def _merge_tifs(
    tif_files: list[Path],
    output_tif: Path,
    *,
    target_resolution: Optional[float] = None,
    block_size: int = 512,
) -> Path:
    """Merge terrain in bounded windows with first-valid-value priority.

    The old implementation materialized the complete native-resolution mosaic
    in memory before downsampling. This implementation creates the target grid
    first, then reprojects one destination block at a time. Peak memory is
    therefore controlled by ``block_size`` rather than total terrain extent.
    """

    import numpy as np
    import rasterio
    from affine import Affine
    from rasterio.enums import Resampling
    from rasterio.warp import reproject, transform_bounds
    from rasterio.windows import Window, from_bounds, transform as window_transform

    datasets = [rasterio.open(tif) for tif in tif_files if Path(tif).is_file()]
    try:
        if not datasets:
            raise ValueError("No valid TIFF files to merge")
        if any(dataset.crs is None for dataset in datasets):
            raise ValueError("Every terrain source must have a CRS")

        ref_crs = datasets[0].crs
        resolution = float(target_resolution or max(abs(value) for value in datasets[0].res))
        if not math.isfinite(resolution) or resolution <= 0:
            raise ValueError("target_resolution must be a finite positive number")

        source_bounds = []
        for dataset in datasets:
            if dataset.crs == ref_crs or _crs_equivalent(dataset.crs, ref_crs):
                source_bounds.append(tuple(dataset.bounds))
            else:
                source_bounds.append(
                    transform_bounds(dataset.crs, ref_crs, *dataset.bounds, densify_pts=21)
                )

        left = math.floor(min(bounds[0] for bounds in source_bounds) / resolution) * resolution
        bottom = math.floor(min(bounds[1] for bounds in source_bounds) / resolution) * resolution
        right = math.ceil(max(bounds[2] for bounds in source_bounds) / resolution) * resolution
        top = math.ceil(max(bounds[3] for bounds in source_bounds) / resolution) * resolution
        width = max(1, int(round((right - left) / resolution)))
        height = max(1, int(round((top - bottom) / resolution)))
        transform = Affine(resolution, 0.0, left, 0.0, -resolution, top)

        output_tif.parent.mkdir(parents=True, exist_ok=True)
        profile = datasets[0].profile.copy()
        profile.update(
            driver="GTiff",
            count=1,
            dtype="float32",
            nodata=float("nan"),
            crs=ref_crs,
            transform=transform,
            width=width,
            height=height,
            tiled=True,
            blockxsize=block_size,
            blockysize=block_size,
            compress="deflate",
            predictor=3,
            bigtiff="IF_SAFER",
            sparse_ok=True,
        )

        with rasterio.open(output_tif, "w+", **profile) as destination:
            # GeoTIFF initializes unwritten blocks as zero, which is a valid
            # elevation. Explicit NaN initialization preserves transparent
            # nodata while remaining bounded to one block in memory.
            for _, window in destination.block_windows(1):
                destination.write(
                    np.full((int(window.height), int(window.width)), np.nan, dtype="float32"),
                    1,
                    window=window,
                )

            for dataset, bounds in zip(datasets, source_bounds):
                source_window = from_bounds(*bounds, transform=transform)
                col_start = max(0, int(math.floor(source_window.col_off)))
                row_start = max(0, int(math.floor(source_window.row_off)))
                col_stop = min(width, int(math.ceil(source_window.col_off + source_window.width)))
                row_stop = min(height, int(math.ceil(source_window.row_off + source_window.height)))

                for row_off in range(row_start, row_stop, block_size):
                    for col_off in range(col_start, col_stop, block_size):
                        window = Window(
                            col_off,
                            row_off,
                            min(block_size, col_stop - col_off),
                            min(block_size, row_stop - row_off),
                        )
                        existing = destination.read(1, window=window)
                        empty = np.isnan(existing)
                        if not empty.any():
                            continue

                        candidate = np.full(existing.shape, np.nan, dtype="float32")
                        reproject(
                            source=rasterio.band(dataset, 1),
                            destination=candidate,
                            src_transform=dataset.transform,
                            src_crs=dataset.crs,
                            src_nodata=dataset.nodata,
                            dst_transform=window_transform(window, transform),
                            dst_crs=ref_crs,
                            dst_nodata=np.nan,
                            resampling=Resampling.bilinear,
                            num_threads=2,
                        )
                        fill = empty & np.isfinite(candidate)
                        if fill.any():
                            existing[fill] = candidate[fill]
                            destination.write(existing, 1, window=window)

            overview_factors = [
                factor for factor in (2, 4, 8, 16, 32, 64)
                if min(width, height) // factor >= 128
            ]
            if overview_factors:
                destination.build_overviews(overview_factors, Resampling.average)
                destination.update_tags(ns="rio_overview", resampling="average")
    finally:
        for dataset in datasets:
            dataset.close()

    return output_tif


def _crs_equivalent(crs1, crs2) -> bool:
    """Check if two CRS objects represent the same coordinate system.

    Handles the common HEC-RAS case where one TIF has EPSG:XXXX and another
    has the equivalent raw WKT/PROJCS string.
    """
    if crs1 == crs2:
        return True

    try:
        from pyproj import CRS as PyprojCRS

        p1 = PyprojCRS(crs1.to_wkt())
        p2 = PyprojCRS(crs2.to_wkt())
        return p1.equals(p2)
    except (ImportError, Exception):
        pass

    # Fallback: compare EPSG codes if both resolve
    try:
        e1 = crs1.to_epsg()
        e2 = crs2.to_epsg()
        if e1 and e2:
            return e1 == e2
    except Exception:
        pass

    # Fallback: compare WKT strings after normalizing whitespace
    try:
        w1 = " ".join(crs1.to_wkt().split())
        w2 = " ".join(crs2.to_wkt().split())
        return w1 == w2
    except Exception:
        pass

    return False


def _reproject_to_match(src_dataset, target_crs, work_dir: Path) -> Path:
    """Reproject an open rasterio dataset to a target CRS.

    Returns path to a temporary reprojected TIFF in work_dir.
    """
    import rasterio
    from rasterio.crs import CRS
    from rasterio.warp import calculate_default_transform, reproject, Resampling

    work_dir.mkdir(parents=True, exist_ok=True)
    tmp_path = work_dir / f"_reproj_{src_dataset.name.split('/')[-1].split(chr(92))[-1]}"

    dst_crs = CRS(target_crs.to_wkt()) if not isinstance(target_crs, CRS) else target_crs

    transform, width, height = calculate_default_transform(
        src_dataset.crs, dst_crs,
        src_dataset.width, src_dataset.height,
        *src_dataset.bounds,
    )

    out_meta = src_dataset.meta.copy()
    out_meta.update({
        "driver": "GTiff",
        "crs": dst_crs,
        "transform": transform,
        "width": width,
        "height": height,
    })

    with rasterio.open(tmp_path, "w", **out_meta) as dst:
        for i in range(1, src_dataset.count + 1):
            reproject(
                source=rasterio.band(src_dataset, i),
                destination=rasterio.band(dst, i),
                src_transform=src_dataset.transform,
                src_crs=src_dataset.crs,
                dst_transform=transform,
                dst_crs=dst_crs,
                resampling=Resampling.bilinear,
            )

    return tmp_path


def export_modified_terrain(
    project_path: Path,
    output_tif: Path,
    *,
    geometry: Optional[str] = None,
    terrain_name: Optional[str] = None,
) -> Path:
    """Export terrain with modifications applied as a GeoTIFF.

    Reads the original terrain raster grid, then samples the modified terrain
    (channels, levees, polygon overrides, etc.) at each cell center via
    RasMapperLib and writes the result.

    Requires HEC-RAS 6.6+ installed and pythonnet (Windows only).

    Args:
        project_path: Path to .prj file or project directory
        output_tif: Output GeoTIFF path
        geometry: Geometry number (e.g. "g01"). None = first geometry
        terrain_name: Specific terrain to use. None = first terrain from rasmap

    Returns:
        Path to the output GeoTIFF
    """
    from ras_commander import init_ras_project
    from ras_commander.terrain import RasTerrainMod
    from ras2cng.project import resolve_project_path

    project_dir, prj_file = resolve_project_path(Path(project_path))
    ras = init_ras_project(project_dir, ras_object="new", load_results_summary=False)

    # Resolve rasmap path
    rasmap_path = project_dir / f"{prj_file.stem}.rasmap"
    if not rasmap_path.exists():
        rasmap_files = list(project_dir.glob("*.rasmap"))
        if not rasmap_files:
            raise FileNotFoundError("No .rasmap file found in project")
        rasmap_path = rasmap_files[0]

    # Resolve geometry HDF
    if geometry:
        geom_num = geometry.replace("g", "").zfill(2)
    else:
        geom_num = "01"
    geom_hdf = project_dir / f"{ras.project_name}.g{geom_num}.hdf"
    if not geom_hdf.exists():
        raise FileNotFoundError(f"Geometry HDF not found: {geom_hdf}")

    # Discover terrain TIF
    terrains = discover_terrains(project_path)
    if not terrains:
        raise ValueError("No terrain data found in project")

    if terrain_name:
        matches = [t for t in terrains if t.name == terrain_name]
        if not matches:
            raise ValueError(f"Terrain '{terrain_name}' not found. Available: {[t.name for t in terrains]}")
        terrain_info = matches[0]
    else:
        terrain_info = terrains[0]

    if not terrain_info.tif_files:
        raise ValueError(f"No TIF files found for terrain '{terrain_info.name}'")

    terrain_tif = terrain_info.tif_files[0]

    console.print(f"\n[bold cyan]ras2cng terrain-mod[/bold cyan] -> {output_tif}")
    console.print(f"  Project  : {prj_file.name}")
    console.print(f"  Geometry : g{geom_num}")
    console.print(f"  Terrain  : {terrain_info.name} ({terrain_tif.name})")

    # One-time setup for RasMapperLib
    console.print("  Setting up GDAL bridge...")
    RasTerrainMod.setup_gdal_bridge()

    console.print("  Sampling modified terrain (this may take a while)...")
    output_tif = Path(output_tif)
    output_tif.parent.mkdir(parents=True, exist_ok=True)

    RasTerrainMod.compute_modified_terrain_raster(
        rasmap_path=str(rasmap_path),
        geom_hdf_path=str(geom_hdf),
        terrain_tif_path=str(terrain_tif),
        output_tif_path=str(output_tif),
    )

    console.print(f"[green]OK[/green] Modified terrain raster: {output_tif}")
    return output_tif


def export_mannings_raster(
    project_path: Path,
    output_tif: Path,
    *,
    geometry: Optional[str] = None,
) -> Path:
    """Export final Manning's n raster (base + calibration overrides) as GeoTIFF.

    Combines base land cover raster with calibration table and region polygon
    overrides to produce the full-resolution Final Manning's N raster. Replicates
    what RASMapper's FinalNValueLayer computes internally.

    Args:
        project_path: Path to .prj file or project directory
        output_tif: Output GeoTIFF path
        geometry: Geometry number (e.g. "g01"). None = first geometry

    Returns:
        Path to the output GeoTIFF
    """
    from ras_commander import init_ras_project
    from ras_commander.hdf import HdfLandCover
    from ras2cng.project import resolve_project_path

    project_dir, prj_file = resolve_project_path(Path(project_path))
    ras = init_ras_project(project_dir, ras_object="new", load_results_summary=False)

    # Resolve geometry HDF
    if geometry:
        geom_num = geometry.replace("g", "").zfill(2)
    else:
        geom_num = "01"
    geom_hdf = project_dir / f"{ras.project_name}.g{geom_num}.hdf"
    if not geom_hdf.exists():
        raise FileNotFoundError(f"Geometry HDF not found: {geom_hdf}")

    console.print(f"\n[bold cyan]ras2cng mannings[/bold cyan] -> {output_tif}")
    console.print(f"  Project  : {prj_file.name}")
    console.print(f"  Geometry : g{geom_num}")

    output_tif = Path(output_tif)
    output_tif.parent.mkdir(parents=True, exist_ok=True)

    console.print("  Computing final Manning's n raster...")
    result = HdfLandCover.compute_final_mannings_raster(
        hdf_path=geom_hdf,
        output_tif_path=str(output_tif),
        ras_object=ras,
    )

    if result is None:
        raise RuntimeError(
            "Manning's n raster computation failed. "
            "Check that the geometry HDF has land cover associations configured."
        )

    console.print(f"  Shape: {result.shape[1]}x{result.shape[0]}")
    console.print(f"  Range: {result[result > 0].min():.4f} to {result.max():.4f}")
    console.print(f"[green]OK[/green] Manning's n raster: {output_tif}")
    return output_tif


def _downsample_tif(
    input_tif: Path,
    output_tif: Path,
    *,
    factor: Optional[float] = None,
    resolution: Optional[float] = None,
) -> Path:
    """Downsample a TIFF via rasterio resampling.

    Args:
        input_tif: Input TIFF path
        output_tif: Output TIFF path
        factor: Downsample factor (2.0 = half resolution, output cells are 2x larger)
        resolution: Target cell size in source units (overrides factor)

    Returns:
        Path to the downsampled TIFF
    """
    import numpy as np
    import rasterio
    from rasterio.enums import Resampling

    with rasterio.open(input_tif) as src:
        src_res = abs(src.res[0])

        if resolution:
            scale = src_res / resolution
        elif factor:
            scale = 1.0 / factor
        else:
            raise ValueError("Either factor or resolution must be provided")

        new_height = max(1, int(src.height * scale))
        new_width = max(1, int(src.width * scale))

        data = src.read(
            out_shape=(src.count, new_height, new_width),
            resampling=Resampling.bilinear,
        )

        new_transform = src.transform * src.transform.scale(
            src.width / new_width,
            src.height / new_height,
        )

        out_meta = src.meta.copy()
        out_meta.update({
            "driver": "GTiff",
            "height": new_height,
            "width": new_width,
            "transform": new_transform,
            "compress": "deflate",
        })

        output_tif.parent.mkdir(parents=True, exist_ok=True)
        with rasterio.open(output_tif, "w", **out_meta) as dest:
            dest.write(data)

    return output_tif
