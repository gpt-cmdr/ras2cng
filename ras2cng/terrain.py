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
import logging
import math
import re
import warnings
from pathlib import Path
from typing import Optional

from rich.console import Console

console = Console()
LOGGER = logging.getLogger(__name__)


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
    merge_engine, merge_fallback = select_terrain_merge_engine(sources)
    if merge_fallback:
        console.print(f"  [yellow]Note:[/yellow] terrain merge uses rasterio: {merge_fallback}")
    _merge_tifs(
        sources,
        merged_tif,
        target_resolution=decision.target_resolution,
        engine=merge_engine,
    )
    console.print(f"  Merged -> {merged_tif.name} ({merge_engine})")

    provenance = {
        "schema": "ras2cng.terrain-consolidation/v1",
        "terrain_name": source_terrain_name or terrain_name,
        "output_name": terrain_name,
        "source_priority": "first-valid-value-wins",
        "resampling": "bilinear",
        # "rasterio": one GDAL warp per 512-px destination block (the default).
        # "terrio": one whole-output warp, bit-identical to GDAL's own
        # whole-output painter warp; opt-in (RAS2CNG_TERRAIN_MERGE_ENGINE).
        "merge_engine": merge_engine,
        "merge_engine_fallback_reason": merge_fallback,
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


#: Terrain-modification layers written by :func:`extract_terrain_modification_layers`.
TERRAIN_MODIFICATION_LAYERS = (
    "terrain_modification_lines",
    "terrain_modification_polygons",
    "terrain_modification_footprints",
    "terrain_modification_control_points",
)

#: RAS Mapper's affine shape modifications: dataset name under
#: ``/Modifications/<layer>`` -> ``shape_type`` (RasMapperLib ``*FeatureLayer``).
_TERRAIN_SHAPE_DATASETS = {
    "Piers": "pier",
    "Rectangles": "rectangle",
    "Circles": "ellipse",
    "Triangles": "triangle",
}


def extract_terrain_modification_layers(
    terrain_hdf_path: Path,
    *,
    crs: Optional[str] = None,
) -> dict[str, object]:
    """Read RASMapper terrain modification vectors from a terrain HDF.

    Each ``/Modifications/<layer>`` group can hold several features; every
    feature becomes its own row (``feature_index`` within the layer, with that
    feature's own ``Attributes`` row).

    Returns four GeoDataFrames (always present, possibly empty):

    * ``terrain_modification_lines``: line, levee and channel centrelines with
      their ``width`` (top width), ``left_slope``, ``right_slope`` and
      ``max_extent`` (RAS Mapper's "Max Reach": how far either side of the
      centreline the modification may change the terrain).
    * ``terrain_modification_polygons``: polygon modifications, and the
      outlines of pier, rectangle, ellipse and triangle modifications
      (``shape_type``), built from their stored parameters the way RAS Mapper
      builds them.
    * ``terrain_modification_footprints``: one polygon per feature covering
      the terrain the feature can change. Lines are buffered by ``max_extent``
      (or by half the top width when no max extent is stored;
      ``footprint_basis`` says which and ``footprint_half_width`` by how
      much); polygons and shapes are their own footprint. A line with neither
      value has no footprint row.
    * ``terrain_modification_control_points``.

    Args:
        terrain_hdf_path: HEC-RAS terrain HDF.
        crs: CRS to assign (default: the terrain HDF's projection).

    Raises:
        FileNotFoundError: The terrain HDF does not exist.
        ValueError: No CRS is given and the HDF has none.
    """

    import geopandas as gpd
    import h5py
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
    rows: dict[str, list[dict]] = {name: [] for name in TERRAIN_MODIFICATION_LAYERS}
    with h5py.File(terrain_hdf_path, "r") as hdf:
        modifications = hdf.get("Modifications")
        if modifications is not None:
            for name, group in modifications.items():
                if isinstance(group, h5py.Group):
                    _collect_modification_group(
                        name, group, metadata_by_name.get(name, {}), rows
                    )

    return {
        layer: gpd.GeoDataFrame(
            records if records else {"geometry": []}, geometry="geometry", crs=source_crs
        )
        for layer, records in rows.items()
    }


def _collect_modification_group(name: str, group, metadata: dict, rows: dict) -> None:
    """Append one modification layer's features to ``rows`` (by output layer)."""
    import numpy as np
    from shapely.geometry import Point

    base = _terrain_modification_properties(name, group, metadata)
    attributes = _attribute_rows(group)

    def feature_properties(index: int) -> dict:
        row = attributes[index] if index < len(attributes) else {}
        props = {**base, "feature_index": index}
        props["feature_name"] = str(row.get("Name") or row.get("System Name") or name)
        for key, names in _FEATURE_FLOAT_ATTRIBUTES.items():
            if any(field in row for field in names):
                # The feature's own field wins; present but invalid is None.
                props[key] = _first_float(row, names)
            else:
                # Absent from the feature's row: keep the layer-level value.
                props[key] = _finite_or_none(props.get(key))
        elevation_type = row.get("Elevation Type")
        props["elevation_type"] = (
            None if elevation_type is None else str(_decode_hdf_value(elevation_type))
        )
        return props

    if "Polyline Points" in group:
        for index, parts in enumerate(_poly_feature_parts(group, "Polyline")):
            geometry = _line_geometry(parts)
            if geometry is None:
                continue
            props = feature_properties(index)
            rows["terrain_modification_lines"].append({**props, "geometry": geometry})
            footprint = _line_footprint(geometry, props)
            if footprint is not None:
                rows["terrain_modification_footprints"].append(footprint)
    elif "Polygon Points" in group:
        boundary_stats = _per_feature_boundary_stats(group)
        for index, parts in enumerate(_poly_feature_parts(group, "Polygon")):
            geometry = _polygon_geometry(parts)
            if geometry is None or geometry.is_empty:
                continue
            boundary_min, boundary_max = boundary_stats(index)
            props = {
                **feature_properties(index),
                "shape_type": "polygon",
                "boundary_elevation_min": boundary_min,
                "boundary_elevation_max": boundary_max,
            }
            rows["terrain_modification_polygons"].append({**props, "geometry": geometry})
            rows["terrain_modification_footprints"].append(
                {**props, "footprint_basis": "polygon", "footprint_half_width": None,
                 "geometry": geometry}
            )
    for dataset_name, shape_type in _TERRAIN_SHAPE_DATASETS.items():
        if dataset_name not in group:
            continue
        values = np.asarray(group[dataset_name][:], dtype="float64")
        for index, geometry in enumerate(_shape_geometries(shape_type, values)):
            if geometry is None or geometry.is_empty:
                continue
            props = {**feature_properties(index), "shape_type": shape_type}
            rows["terrain_modification_polygons"].append({**props, "geometry": geometry})
            rows["terrain_modification_footprints"].append(
                {**props, "footprint_basis": shape_type, "footprint_half_width": None,
                 "geometry": geometry}
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
            rows["terrain_modification_control_points"].append(
                {
                    "parent_modification": name,
                    "control_name": names[index],
                    "elevation": float(elevations[index]) if np.isfinite(elevations[index]) else None,
                    "modification_mode": base.get("modification_mode"),
                    "geometry": Point(float(point[0]), float(point[1])),
                }
            )


#: Per-feature numeric attributes: output column -> HDF column names (stored
#: name first, then the RAS Mapper display name).
_FEATURE_FLOAT_ATTRIBUTES = {
    "width": ("Top Width", "Width"),
    "left_slope": ("Left Slope", "LeftSlope"),
    "right_slope": ("Right Slope", "RightSlope"),
    "max_extent": ("Max Reach", "Max Extent"),
    "top_elevation": ("Top Elevation",),
    "elevation_value": ("Elevation Value",),
}


def _finite_or_none(value) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _attribute_rows(group) -> list[dict]:
    """The group's ``Attributes`` compound rows as dicts (one per feature)."""
    dataset = group.get("Attributes")
    if dataset is None or getattr(dataset, "dtype", None) is None or not dataset.dtype.names:
        return []
    names = dataset.dtype.names
    return [
        {key: _decode_hdf_value(record[key]) for key in names}
        for record in dataset[:]
    ]


def _first_float(row: dict, names) -> Optional[float]:
    for key in names:
        if key in row:
            value = _finite_or_none(row[key])
            if value is not None:
                return value
    return None


def _per_feature_boundary_stats(group):
    """``index -> (min, max)`` of a polygon feature's ``Boundary Elevations``.

    When the dataset has one value per polygon point, each feature's values
    are its ``Polygon Info`` point span. Otherwise (another layout, or no
    ``Info``) every feature gets the statistics of the whole dataset.
    """
    import numpy as np

    whole = (
        _finite_dataset_stat(group, "Boundary Elevations", "min"),
        _finite_dataset_stat(group, "Boundary Elevations", "max"),
    )
    if "Boundary Elevations" not in group or "Polygon Info" not in group:
        return lambda index: whole
    values = np.asarray(group["Boundary Elevations"][:], dtype="float64").reshape(-1)
    if len(values) != len(group["Polygon Points"]):
        return lambda index: whole
    info = np.asarray(group["Polygon Info"][:], dtype="int64").reshape(-1, 4)

    def stats(index: int):
        if index >= len(info):
            return whole
        start, count = int(info[index][0]), int(info[index][1])
        span = values[start:start + count]
        span = span[np.isfinite(span)]
        if not len(span):
            return (None, None)
        return (float(span.min()), float(span.max()))

    return stats


def _poly_feature_parts(group, prefix: str) -> list[list]:
    """Per-feature point parts of a RAS Mapper poly feature set.

    ``<prefix> Info`` rows are ``[point start, point count, part start, part
    count]``; ``<prefix> Parts`` rows are ``[start within the feature, point
    count]`` (HDF5Storage.ReadPolyFeatures). Without an ``Info`` dataset the
    whole point array is one feature.
    """
    import numpy as np

    points = np.asarray(group[f"{prefix} Points"][:], dtype="float64")
    if points.ndim != 2 or points.shape[1] < 2:
        return []
    parts = (
        np.asarray(group[f"{prefix} Parts"][:], dtype="int64").reshape(-1, 2)
        if f"{prefix} Parts" in group else None
    )
    info = (
        np.asarray(group[f"{prefix} Info"][:], dtype="int64").reshape(-1, 4)
        if f"{prefix} Info" in group else None
    )
    if info is None:
        spans = [(0, len(points), 0, len(parts) if parts is not None else 0)]
    else:
        spans = [tuple(int(v) for v in row) for row in info]
    features = []
    for start, count, part_start, part_count in spans:
        feature = points[start:start + count, :2]
        if parts is not None and part_count > 0:
            part_rows = parts[part_start:part_start + part_count]
        else:
            part_rows = [(0, len(feature))]
        features.append(
            [feature[int(s):int(s) + int(c)] for s, c in part_rows if int(c) > 0]
        )
    return features


def _line_geometry(parts):
    from shapely.geometry import LineString, MultiLineString

    lines = [LineString(part) for part in parts if len(part) >= 2]
    if not lines:
        return None
    return lines[0] if len(lines) == 1 else MultiLineString(lines)


def _polygon_geometry(parts):
    """Rings combined by the even-odd rule (a ring inside another is a hole)."""
    from shapely import make_valid
    from shapely.geometry import Polygon

    geometry = None
    for part in parts:
        if len(part) < 3:
            continue
        ring = Polygon(part)
        if not ring.is_valid:
            ring = _polygonal_part(make_valid(ring))
            if ring is None:
                continue
        geometry = ring if geometry is None else geometry.symmetric_difference(ring)
    return geometry


def _polygonal_part(geometry):
    """The polygonal components of a repaired geometry, or ``None``.

    ``make_valid`` can return a ``GeometryCollection`` mixing a polygon with
    lines or points; only the area belongs in a polygon layer.
    """
    from shapely.geometry import MultiPolygon, Polygon

    if isinstance(geometry, (Polygon, MultiPolygon)):
        return None if geometry.is_empty else geometry
    polygons = []
    for part in getattr(geometry, "geoms", ()):
        if isinstance(part, Polygon):
            polygons.append(part)
        elif isinstance(part, MultiPolygon):
            polygons.extend(part.geoms)
    polygons = [p for p in polygons if not p.is_empty]
    if not polygons:
        return None
    return polygons[0] if len(polygons) == 1 else MultiPolygon(polygons)


def _line_footprint(geometry, props: dict) -> Optional[dict]:
    """Buffer a line modification by the distance it can change the terrain.

    RAS Mapper indexes a ground-line modification by the polygon offset
    ``Max Reach`` either side of its centreline
    (``GroundLineModificationLayer.PolygonSpatialIndex``). Without a max
    extent, half the top width is used, which leaves out the side slopes.
    """
    max_extent = props.get("max_extent")
    width = props.get("width")
    half_top = (width / 2.0) if width is not None and width > 0 else 0.0
    if max_extent is not None and max_extent > 0:
        basis, half_width = "max_extent", max(max_extent, half_top)
    elif half_top > 0:
        basis, half_width = "top_width", half_top
    else:
        return None
    return {
        **props,
        "footprint_basis": basis,
        "footprint_half_width": float(half_width),
        "geometry": geometry.buffer(half_width, cap_style="flat", join_style="round"),
    }


def _shape_geometries(shape_type: str, values) -> list:
    """Outlines of RAS Mapper's affine shape modifications, one per row.

    Angles are degrees clockwise (RasMapperLib rotates by ``-angle`` in a
    y-up frame); shapes rotate about their centre.
    """
    from shapely import affinity
    from shapely.geometry import Polygon, box

    if values.ndim != 2:
        return []

    def rectangle(r):
        local = box(-r[2] / 2, -r[3] / 2, r[2] / 2, r[3] / 2) if r[2] > 0 and r[3] > 0 else None
        return (r[0], r[1]), local, r[4]

    def ellipse(r):
        local = _ellipse_arc(r[2], r[3], 0.0, 2 * math.pi) if r[2] > 0 and r[3] > 0 else None
        return (r[0], r[1]), local, r[4]

    def triangle(r):
        local = Polygon([
            (r[2] - r[0], r[3] - r[1]), (r[4] - r[0], r[5] - r[1]), (r[6] - r[0], r[7] - r[1]),
        ])
        return (r[0], r[1]), local, r[8]

    builders = {
        "pier": (15, _pier_outline),
        "rectangle": (5, rectangle),
        "ellipse": (5, ellipse),
        "triangle": (9, triangle),
    }
    columns, build = builders[shape_type]
    if values.shape[1] < columns:
        raise ValueError(
            f"{shape_type} modification dataset has {values.shape[1]} columns; "
            f"expected {columns}"
        )
    outlines = []
    for row in values:
        center, local, angle = build(row)
        if local is None or local.is_empty or local.area <= 0:
            outlines.append(None)
            continue
        placed = affinity.translate(local, center[0], center[1])
        if angle:
            placed = affinity.rotate(placed, -float(angle), origin=center)
        outlines.append(placed)
    return outlines


def _ellipse_arc(radius_x: float, radius_y: float, start: float, stop: float,
                 y0: float = 0.0, segments: int = 32):
    """Polygon of an elliptical arc about ``(0, y0)``, closed through its chord."""
    import numpy as np
    from shapely.geometry import Polygon

    angles = np.linspace(start, stop, segments + 1)
    return Polygon(list(zip(radius_x * np.cos(angles), y0 + radius_y * np.sin(angles))))


def _pier_outline(row):
    """Pier outline in its local frame, as ``Pier.MakePierFromParams`` builds it.

    Columns (``Column Ordering``): Body X, Body Y, Body Width, Body Height,
    TopCap X, TopCap Y, TopCap RadiusX, TopCap RadiusY, TopCap Type,
    BottomCap X, BottomCap Y, BottomCap RadiusX, BottomCap RadiusY,
    BottomCap Type (0 round, 1 sharp), AngleRotation. The centre and width
    come from the body when it has a position, else from a cap
    (``HDF5Storage.ReadPierFeatures``). The body is ``width`` across and
    ``height`` along the pier axis (+y before rotation). A round cap is a half
    ellipse of radii (width/2, cap RadiusY) on the body end; a sharp cap is a
    triangle of base ``width`` and height 2 x cap RadiusY. Without a body the
    caps sit on the centre; a lone sharp cap is centred on it.
    """
    from shapely.geometry import Polygon, box
    from shapely.ops import unary_union

    (body_x, body_y, body_width, body_height, top_x, top_y, _top_rx, top_ry, top_type,
     bottom_x, bottom_y, _bottom_rx, bottom_ry, bottom_type, angle) = (
        float(v) for v in row[:15]
    )
    center, width = (0.0, 0.0), 0.0
    if body_x != 0.0 and body_y != 0.0:
        center, width = (body_x, body_y), body_width
    else:
        if top_x != 0.0 and top_y != 0.0:
            center, width = (top_x, top_y), _top_rx * 2.0
        if bottom_x != 0.0 and bottom_y != 0.0:
            center, width = (bottom_x, bottom_y), _bottom_rx * 2.0
    if width <= 0:
        return center, None, angle
    half = width / 2.0
    has_body = body_height > 0
    if not has_body and top_ry <= 0 and bottom_ry <= 0:
        return center, None, angle
    pieces = []
    if has_body:
        pieces.append(box(-half, -body_height / 2.0, half, body_height / 2.0))

    def cap(radius_y: float, cap_type: float, sign: float, other_cap: bool):
        base_y = sign * body_height / 2.0 if has_body else 0.0
        if round(cap_type) == 0:  # round nose: half ellipse beyond the base
            return _ellipse_arc(half, sign * radius_y, 0.0, math.pi, y0=base_y)
        height = 2.0 * radius_y
        if has_body or other_cap:  # base on the body end / centre, apex outward
            apex_y = base_y + sign * height
        else:  # a lone sharp cap is centred on the pier centre
            base_y, apex_y = -sign * height / 2.0, sign * height / 2.0
        return Polygon([(-half, base_y), (half, base_y), (0.0, apex_y)])

    if top_ry > 0:
        pieces.append(cap(top_ry, top_type, 1.0, bottom_ry > 0))
    if bottom_ry > 0:
        pieces.append(cap(bottom_ry, bottom_type, -1.0, top_ry > 0))
    return center, unary_union(pieces), angle


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


def terrain_modifications(
    terrain_hdf: Path, rasmap_path: Optional[Path] = None
) -> list[str]:
    """Names of the terrain modifications a terrain carries, or ``[]``.

    Reads the terrain HDF's ``/Modifications`` group and, when given, the
    ``ElevationModificationGroup`` layers the ``.rasmap`` registers under
    this terrain. The HDF is what counts: RAS Mapper's export applies every
    modification stored there, ticked in the ``.rasmap`` or not (T14). A
    ``/Modifications`` entry that is not a plain group (a link) or cannot be
    read is reported as a modification, so an unreadable terrain is never
    treated as unmodified.

    A modified terrain goes to RAS Mapper's export unless the engine
    advertises ``rasmapper-effective-terrain/2`` and every modification form
    is qualified (see ``fast_map.TerrainResolver.modified_route``).
    """
    import xml.etree.ElementTree as ET

    import h5py

    terrain_hdf = Path(terrain_hdf)
    found: list[str] = []
    with h5py.File(terrain_hdf, "r") as handle:
        link = handle.get("Modifications", getlink=True)
        if link is not None:
            if not isinstance(link, h5py.HardLink):
                found.append("hdf:/Modifications (link)")
            else:
                try:
                    group = handle["Modifications"]
                    if isinstance(group, h5py.Group):
                        found.extend(f"hdf:{name}" for name in group.keys())
                    else:
                        found.append("hdf:/Modifications (not a group)")
                except (KeyError, OSError, RuntimeError):
                    found.append("hdf:/Modifications (unreadable)")
    if rasmap_path is not None and Path(rasmap_path).is_file():
        rasmap = Path(rasmap_path)
        try:
            root = ET.parse(rasmap).getroot()
        except ET.ParseError:
            return found
        target = terrain_hdf.resolve()
        for layer in root.iter("Layer"):
            filename = layer.get("Filename")
            if layer.get("Type") != "TerrainLayer" or not filename:
                continue
            path = (rasmap.parent / filename.replace("\\", "/")).resolve()
            if path != target:
                continue
            for group_layer in layer.findall("Layer"):
                if group_layer.get("Type") == "ElevationModificationGroup":
                    found.extend(
                        f"rasmap:{child.get('Name')}" for child in group_layer.findall("Layer")
                    )
    return found


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


_TERRIO_MERGE_KWARGS = (
    "dst", "t_srs", "te", "tr", "resampling", "overview", "dstnodata", "ot",
    "compress", "tile_size", "bigtiff",
)


def select_terrain_merge_engine(
    tif_files: list[Path], engine: Optional[str] = None
) -> tuple[str, Optional[str]]:
    """``(engine, fallback_reason)`` for :func:`_merge_tifs`.

    ``RAS2CNG_TERRAIN_MERGE_ENGINE`` = ``auto`` | ``terrio`` | ``rasterio``.
    ``auto`` is **rasterio** (:data:`ras2cng.warp.TERRAIN_MERGE_AUTO`): the
    terrio mosaic is bit-identical to GDAL's own whole-output warp, but that is
    not today's output. The rasterio path warps each 512-px destination block
    separately, and on Bald Eagle (dtm_20ft + baldeagledem at 40 ft) 18.5 M of
    49.7 M pixels differ from the whole-output warp, by up to 4.44 ft (274 over
    1 ft), with equal masks. So terrio is opt-in until the user decides the
    default. ``terrio`` falls back to rasterio, with the reason returned and
    recorded in the terrain provenance, when the installed terrio has no
    streaming warp or the sources do not share one CRS.
    """
    from ras2cng import raster_tiles, warp

    choice = warp.resolve_terrain_merge_engine(engine)
    if choice != warp.TERRIO:
        return warp.RASTERIO, None
    if not warp.terrio_warp_supported():
        return warp.RASTERIO, (
            "the installed terrio has no streaming path-to-path warp (CLB terrio build)"
        )
    import terrio

    if not raster_tiles._signature_has(terrio.warp, *_TERRIO_MERGE_KWARGS):
        return warp.RASTERIO, "the installed terrio warp lacks the mosaic options"
    import rasterio

    crs = None
    for path in tif_files:
        if not Path(path).is_file():
            continue
        with rasterio.open(path) as dataset:
            if dataset.crs is None:
                return warp.RASTERIO, f"{Path(path).name} has no CRS"
            if crs is None:
                crs = dataset.crs
            elif not (dataset.crs == crs or _crs_equivalent(dataset.crs, crs)):
                return warp.RASTERIO, "the terrain sources do not share one CRS"
    return warp.TERRIO, None


def _merge_tifs_terrio(
    valid_tifs: list[Path],
    output_tif: Path,
    *,
    ref_crs,
    bounds: tuple[float, float, float, float],
    resolution: float,
    block_size: int,
) -> None:
    """One whole-output terrio warp of every source (the gdalwarp painter order)."""
    import terrio

    from ras2cng import warp

    result = terrio.warp(
        # Painter order: later sources are drawn over earlier ones, so the
        # first (highest-priority) source goes last.
        [str(path) for path in reversed(valid_tifs)],
        dst=str(output_tif),
        t_srs=ref_crs.to_wkt(),
        te=bounds,
        tr=(resolution, resolution),
        resampling="bilinear",
        # Never read source overviews: at a coarser target the terrio default
        # (gdalwarp -ovr AUTO) reads them, worth up to 151 ft on Bald Eagle.
        overview="NONE",
        dstnodata=float("nan"),
        ot="float32",
        compress="DEFLATE",
        tile_size=block_size,
        bigtiff=True,
        **warp.memory_kwargs(),
    )
    del result


def _merge_tifs(
    tif_files: list[Path],
    output_tif: Path,
    *,
    target_resolution: Optional[float] = None,
    block_size: int = 512,
    engine: Optional[str] = None,
) -> Path:
    """Merge terrain in bounded windows with first-valid-value priority.

    The old implementation materialized the complete native-resolution mosaic
    in memory before downsampling. This implementation creates the target grid
    first, then reprojects one destination block at a time. Peak memory is
    therefore controlled by ``block_size`` rather than total terrain extent.

    ``engine="terrio"`` (see :func:`select_terrain_merge_engine`; ``None`` and
    ``"rasterio"`` keep the path above) writes the same grid with one terrio
    warp of all sources instead. It is strict: a terrio failure raises.
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
        if engine == "terrio":
            valid_tifs = [Path(dataset.name) for dataset in datasets]
            for dataset in datasets:
                dataset.close()
            datasets = []
            _merge_tifs_terrio(
                valid_tifs, output_tif, ref_crs=ref_crs,
                bounds=(left, bottom, right, top), resolution=resolution,
                block_size=block_size,
            )
            from ras2cng.cog import overview_factors as _overview_factors

            with rasterio.open(output_tif, "r+") as destination:
                destination.update_tags(ras2cng_terrain_merge_engine="terrio")
                factors = _overview_factors(destination.width, destination.height)
                if factors:
                    destination.build_overviews(factors, Resampling.average)
                    destination.update_tags(ns="rio_overview", resampling="average")
            return output_tif
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

            # One pyramid-depth policy for the repo: continue until the top
            # level fits a single block, rather than capping at factor 64 and
            # leaving a large terrain without a cheap top level.
            from ras2cng.cog import overview_factors as _overview_factors

            factors = _overview_factors(width, height)
            if factors:
                # Terrain is a genuinely continuous surface, so `average` is the
                # right decimator for its interior.  It does expand the terrain
                # footprint outward by one coarse cell per level at the nodata
                # edge, but this pyramid no longer reaches the published COG:
                # the terrain COG writer now passes OVERVIEWS=IGNORE_EXISTING
                # and rebuilds, so this serves direct readers of the merged
                # intermediate only.
                destination.build_overviews(factors, Resampling.average)
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
    except ImportError:
        LOGGER.debug("pyproj is unavailable; falling back to weaker CRS comparison")
    except Exception as error:
        # The fallbacks below (EPSG code, normalized WKT) are much weaker tests
        # -- two different CRSs can compare equal under them -- so a genuine
        # pyproj failure must be visible rather than silently downgraded.
        LOGGER.warning(
            "pyproj could not compare CRSs (%s); falling back to weaker comparison", error
        )

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

    Returns path to a temporary reprojected TIFF in work_dir. The work is
    :func:`ras2cng.warp.reproject_path` on the dataset's file (terrio when the
    installed build has the streaming warp, else rasterio).
    """
    from ras2cng.warp import reproject_path

    work_dir.mkdir(parents=True, exist_ok=True)
    tmp_path = work_dir / f"_reproj_{src_dataset.name.split('/')[-1].split(chr(92))[-1]}"
    reproject_path(Path(src_dataset.name), tmp_path, target_crs)
    return tmp_path


def export_modified_terrain(
    project_path: Path,
    output_tif: Path,
    *,
    geometry: Optional[str] = None,
    terrain_name: Optional[str] = None,
    downsample_factor: int = 1,
    overwrite: bool = False,
    hecras_version: Optional[str] = None,
) -> Path:
    """Export a registered terrain with modifications applied as one GeoTIFF.

    Delegates consolidation, grid alignment, nearest-neighbor downsampling, and
    vector-modification rasterization to RAS Mapper's native terrain export.
    Registered source priority, stitches, and masks are therefore preserved.

    HEC-RAS 6.4.1, 6.5, 6.6, and 7.0.1 are qualified on Windows and Wine.
    Stable 7.1 is forward-open subject to ras-commander's managed-contract
    check. Unsupported project/runtime versions fail before native execution.

    Args:
        project_path: Path to .prj file or project directory
        output_tif: Output GeoTIFF path
        geometry: Deprecated compatibility argument. Native registered-terrain
            export does not use a geometry HDF, so any supplied value is ignored.
        terrain_name: Exact registered terrain name. May be omitted only when
            the project registers exactly one terrain.
        downsample_factor: Exact source-derived factor: 1, 2, 4, or 8.
        overwrite: Replace an existing output and receipt when true.
        hecras_version: Installed HEC-RAS version to export with (e.g.
            "7.0.1"). None lets ras-commander choose from the project.

    Returns:
        Path to the output GeoTIFF

    Raises:
        RuntimeError: If ras-commander lacks native terrain export support or
            RAS Mapper reports an operational failure.
    """
    from ras_commander import RasTerrain, init_ras_project
    from ras2cng.project import resolve_project_path

    _, prj_file = resolve_project_path(Path(project_path))
    if geometry is not None:
        warnings.warn(
            "export_modified_terrain(..., geometry=...) is deprecated because "
            "native registered-terrain export does not use a geometry HDF; the "
            "argument is ignored and will be removed in ras2cng 1.1.",
            DeprecationWarning,
            stacklevel=2,
        )

    export_native = getattr(RasTerrain, "export_rasmapper_terrain", None)
    if not callable(export_native):
        raise RuntimeError(
            "The installed ras-commander does not provide native registered-terrain "
            "export. Upgrade to a release containing "
            "RasTerrain.export_rasmapper_terrain()."
        )

    # The project object's version must agree with the export runtime, or
    # ras-commander uses (and rejects) the version stored in the project.
    ras = init_ras_project(
        prj_file,
        ras_object="new",
        load_results_summary=False,
        **({"ras_version": hecras_version} if hecras_version else {}),
    )

    console.print(f"\n[bold cyan]ras2cng terrain-mod[/bold cyan] -> {output_tif}")
    console.print(f"  Project  : {prj_file.name}")
    console.print(f"  Terrain  : {terrain_name or '[auto: single registered terrain]'}")
    console.print(f"  Scale    : {downsample_factor}x source resolution")
    console.print("  Exporting through native RAS Mapper...")

    result = export_native(
        **({"hecras_version": hecras_version} if hecras_version else {}),
        ras_project_path=prj_file,
        output_tif=Path(output_tif),
        terrain_name=terrain_name,
        downsample_factor=downsample_factor,
        rasterize_modifications=True,
        overwrite=overwrite,
        ras_object=ras,
    )
    if not result:
        detail = getattr(result, "error", None) or "RAS Mapper terrain export failed"
        receipt = getattr(result, "receipt_path", None)
        if receipt:
            detail = f"{detail} (receipt: {receipt})"
        raise RuntimeError(detail)

    committed_output = Path(result.output_path)
    console.print(f"  Terrain  : {result.terrain_name}")
    console.print(f"  Receipt  : {result.receipt_path}")
    console.print(f"[green]OK[/green] Modified terrain raster: {committed_output}")
    return committed_output


MANNINGS_METHODS = ("auto", "rasmapper", "estimate")
#: Method names recorded in the GeoTIFF tag ``ras2cng_mannings_method``.
MANNINGS_RASMAPPER = "rasmapper-final-n"
MANNINGS_ESTIMATE = "estimate"


def export_mannings_raster(
    project_path: Path,
    output_tif: Path,
    *,
    geometry: Optional[str] = None,
    method: str = "auto",
) -> Path:
    """Export the final Manning's n raster of a geometry as GeoTIFF.

    ``method="auto"`` (default) writes RAS Mapper's "Final n Values" raster in
    Rust (``HdfLandCover.export_final_mannings_raster``: ras-commander 0.113,
    fast-hdf 0.3), with no HEC-RAS: 0 differing pixels against RAS Mapper
    7.0.1's *Export Raster* on Bald Eagle g01/g09/g11/g13 and Weise_2D, about
    2 s against 45-49 s. Its grid is RAS Mapper's (geometry extent, land-cover
    cell size). Geometries the engine refuses (1D elements, pipe networks,
    virtual land-cover layers, multi-part polygons) fall back with a warning to
    the older Python estimate (``HdfLandCover.estimate_final_mannings_raster``),
    which differs from RAS Mapper on 44-51% of common pixels and uses another
    grid. ``"rasmapper"`` raises instead of falling back; ``"estimate"`` always
    uses the estimate (the pre-0.13 behaviour).

    The method that produced the file is printed and written to the GeoTIFF
    tags ``ras2cng_mannings_method`` (:data:`MANNINGS_RASMAPPER` or
    :data:`MANNINGS_ESTIMATE`) and, after a fallback,
    ``ras2cng_mannings_fallback_reason``.

    Args:
        project_path: Path to .prj file or project directory
        output_tif: Output GeoTIFF path
        geometry: Geometry number (e.g. "g01"). None = g01
        method: ``"auto"``, ``"rasmapper"`` or ``"estimate"``

    Returns:
        Path to the output GeoTIFF
    """
    from ras_commander import init_ras_project
    from ras_commander.hdf import HdfLandCover
    from ras2cng.project import resolve_project_path

    method = str(method).strip().lower()
    if method not in MANNINGS_METHODS:
        raise ValueError(
            f"Manning's n method must be one of {', '.join(MANNINGS_METHODS)}; got {method!r}"
        )

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

    fallback_reason: Optional[str] = None
    used = MANNINGS_ESTIMATE
    if method in ("auto", "rasmapper"):
        try:
            _export_rasmapper_mannings(HdfLandCover, geom_hdf, output_tif, ras)
            used = MANNINGS_RASMAPPER
        except _ManningsUnavailable as exc:
            if method == "rasmapper":
                raise RuntimeError(f"RAS Mapper Final n Values raster refused: {exc}") from exc
            fallback_reason = str(exc)
            console.print(
                f"  [yellow]WARNING:[/yellow] the Rust Final n Values raster is unavailable "
                f"({exc}); falling back to the Python estimate, which differs from RAS "
                "Mapper's Final n Values (different grid and values)"
            )

    if used == MANNINGS_ESTIMATE:
        console.print("  Computing Manning's n estimate (not RAS Mapper's Final n Values)...")
        estimate = getattr(HdfLandCover, "estimate_final_mannings_raster", None)
        if estimate is None:  # ras-commander before the rename
            estimate = HdfLandCover.compute_final_mannings_raster
        result = estimate(
            hdf_path=geom_hdf,
            output_tif_path=str(output_tif),
            ras_object=ras,
        )
        if result is None:
            raise RuntimeError(
                "Manning's n raster computation failed. "
                "Check that the geometry HDF has land cover associations configured."
            )

    tags = {"ras2cng_mannings_method": used}
    if fallback_reason:
        tags["ras2cng_mannings_fallback_reason"] = fallback_reason[:1000]
    _tag_geotiff(output_tif, tags)
    _print_mannings_summary(output_tif)
    label = (
        "RAS Mapper Final n Values (Rust)" if used == MANNINGS_RASMAPPER
        else "Python estimate"
    )
    console.print(f"  Method   : {label}")
    console.print(f"[green]OK[/green] Manning's n raster: {output_tif}")
    return output_tif


class _ManningsUnavailable(Exception):
    """The Rust Final n Values raster cannot be produced for this geometry."""


def _export_rasmapper_mannings(HdfLandCover, geom_hdf: Path, output_tif: Path, ras) -> None:
    """Write RAS Mapper's Final n Values raster; raise _ManningsUnavailable on refusal."""
    available = getattr(HdfLandCover, "rasmapper_final_mannings_available", None)
    export = getattr(HdfLandCover, "export_final_mannings_raster", None)
    if export is None or available is None:
        raise _ManningsUnavailable(
            "ras-commander has no HdfLandCover.export_final_mannings_raster (needs 0.113.0+)"
        )
    if not available():
        raise _ManningsUnavailable(
            "ras-commander-fast-hdf with FinalManningsModel is not installed (needs 0.3.0+)"
        )
    refusals: tuple[type[BaseException], ...] = ()
    try:
        import ras_commander_fast_hdf as fh

        refusals = tuple(
            getattr(fh, name) for name in ("UnsupportedError", "TopologyError")
            if isinstance(getattr(fh, name, None), type)
        )
    except ImportError:
        pass
    console.print("  Computing RAS Mapper Final n Values raster (Rust)...")
    try:
        export(geom_hdf, output_tif, ras_object=ras)
    except refusals as exc:
        raise _ManningsUnavailable(f"{type(exc).__name__}: {exc}") from exc


def _tag_geotiff(path: Path, tags: dict[str, str]) -> None:
    """Add default-domain tags to an existing GeoTIFF (method provenance)."""
    import rasterio

    with rasterio.open(path, "r+") as dst:
        dst.update_tags(**tags)


def _print_mannings_summary(path: Path) -> None:
    import numpy as np
    import rasterio

    low, high = math.inf, -math.inf
    with rasterio.open(path) as src:
        console.print(f"  Shape    : {src.width}x{src.height}")
        nodata = src.nodata
        for _, window in src.block_windows(1):  # block-wise: rasters reach ~100 M px
            block = src.read(1, window=window)
            keep = np.isfinite(block) & (block > 0)
            if nodata is not None:
                keep &= block != nodata
            if keep.any():
                values = block[keep]
                low, high = min(low, float(values.min())), max(high, float(values.max()))
    if low <= high:
        console.print(f"  Range    : {low:.4f} to {high:.4f}")


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
