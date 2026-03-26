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

from dataclasses import dataclass, field
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
        terrain_names: Specific terrain names to include (None = all from rasmap)
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

    # Filter by name if requested
    if terrain_names:
        name_set = set(terrain_names)
        terrains = [t for t in terrains if t.name in name_set]
        if not terrains:
            raise ValueError(f"No terrains matching names: {terrain_names}")

    # Collect all TIF files in priority order
    all_tifs: list[Path] = []
    for t in terrains:
        all_tifs.extend(t.tif_files)

    if not all_tifs:
        raise ValueError("No TIFF files found for terrain consolidation")

    console.print(f"[bold]Terrain consolidation:[/bold] {len(all_tifs)} TIFF(s) from {len(terrains)} terrain(s)")

    # Step 2: Merge TIFFs
    merged_tif = output_dir / f"{terrain_name}_merged.tif"
    _merge_tifs(all_tifs, merged_tif)
    console.print(f"  Merged -> {merged_tif.name}")

    # Step 3: Optionally downsample
    final_tif = merged_tif
    if downsample_factor or target_resolution:
        downsampled_tif = output_dir / f"{terrain_name}_downsampled.tif"
        _downsample_tif(
            merged_tif, downsampled_tif,
            factor=downsample_factor,
            resolution=target_resolution,
        )
        final_tif = downsampled_tif
        console.print(f"  Downsampled -> {downsampled_tif.name}")

    # Step 4: Create HEC-RAS terrain HDF (requires RasProcess.exe)
    if not create_hdf:
        console.print(f"[green]OK[/green] TIFF-only mode: {final_tif}")
        return final_tif

    try:
        from ras_commander import RasTerrain

        from ras2cng.project import resolve_project_path
        project_dir, prj_file = resolve_project_path(Path(project_path))

        terrain_hdf = RasTerrain.create_terrain_from_rasters(
            raster_files=[str(final_tif)],
            terrain_name=terrain_name,
            project_folder=str(project_dir),
            units=units,
            ras_version=ras_version,
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
                    rasmap_path=str(rasmap_path),
                    terrain_name=terrain_name,
                    terrain_hdf_path=str(terrain_hdf),
                )
                console.print(f"  Registered in rasmap: {rasmap_path.name}")
        except Exception as e:
            console.print(f"[yellow]Warning:[/yellow] Could not register terrain in rasmap: {e}")

    console.print(f"[green]OK[/green] Terrain consolidation complete: {terrain_hdf}")
    return terrain_hdf


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


def _merge_tifs(tif_files: list[Path], output_tif: Path) -> Path:
    """Merge multiple TIFFs using rasterio with first-wins priority.

    Handles CRS harmonization: when TIFFs have equivalent but differently-
    represented CRS (e.g., EPSG:2965 vs raw WKT for the same projection),
    reprojects non-matching TIFFs to the first file's CRS before merging.

    Args:
        tif_files: List of input TIFF paths (priority order: first wins)
        output_tif: Path for merged output TIFF

    Returns:
        Path to the merged TIFF
    """
    import rasterio
    from rasterio.merge import merge

    datasets = []
    reprojected_temps = []
    try:
        for tif in tif_files:
            if tif.exists():
                datasets.append(rasterio.open(tif))

        if not datasets:
            raise ValueError("No valid TIFF files to merge")

        # Harmonize CRS: reproject any datasets that don't match the first
        ref_crs = datasets[0].crs
        harmonized = [datasets[0]]
        for ds in datasets[1:]:
            if ds.crs == ref_crs:
                harmonized.append(ds)
            elif _crs_equivalent(ds.crs, ref_crs):
                # Same projection, different representation — override CRS
                harmonized.append(ds)
            else:
                # Actually different CRS — reproject to match the reference
                reprojected = _reproject_to_match(ds, ref_crs, output_tif.parent)
                reprojected_temps.append(reprojected)
                harmonized.append(rasterio.open(reprojected))

        mosaic, out_transform = merge(harmonized, method="first")

        out_meta = harmonized[0].meta.copy()
        out_meta.update({
            "driver": "GTiff",
            "height": mosaic.shape[1],
            "width": mosaic.shape[2],
            "transform": out_transform,
            "compress": "deflate",
        })

        output_tif.parent.mkdir(parents=True, exist_ok=True)
        with rasterio.open(output_tif, "w", **out_meta) as dest:
            dest.write(mosaic)
    finally:
        for ds in datasets:
            ds.close()
        # Clean up any temporary reprojected files
        for tmp in reprojected_temps:
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass

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
