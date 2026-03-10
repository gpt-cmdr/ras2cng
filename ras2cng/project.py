"""
Project-level orchestration for ras2cng.

Provides:
- inspect_project(): Discover project structure without extraction
- archive_project(): Full project archive (geometry, optionally results + terrain)
- export_project_metadata(): Export RasPrj dataframes to plain Parquet
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import pandas as pd
from ras_commander import init_ras_project

from ras2cng.results import export_all_variables

from rich.console import Console
from rich.table import Table

from ras2cng.catalog import (
    Manifest,
    ManifestGeomEntry,
    ManifestLayer,
    ManifestPlanEntry,
    ManifestResultVariable,
    ManifestTerrainEntry,
)
from ras2cng.geometry import (
    export_all_hdf_layers,
    export_all_text_layers,
    merge_all_layers,
)

console = Console()


# ---------------------------------------------------------------------------
# Project discovery
# ---------------------------------------------------------------------------

def resolve_project_path(path: Path) -> tuple[Path, Path]:
    """Return (project_dir, prj_file) from either a .prj file or a directory.

    ras-commander's init_ras_project accepts either, so we just validate here.
    """
    path = Path(path)
    if path.is_file() and path.suffix.lower() == ".prj":
        return path.parent, path
    elif path.is_dir():
        # Let ras-commander find the .prj; we just need the folder
        prj_files = list(path.glob("*.prj"))
        if not prj_files:
            raise FileNotFoundError(f"No .prj file found in {path}")
        if len(prj_files) > 1:
            raise ValueError(
                f"Multiple .prj files found in {path}: {[p.name for p in prj_files]}. "
                "Pass the specific .prj file instead of the directory."
            )
        return path, prj_files[0]
    else:
        raise ValueError(f"Not a .prj file or directory: {path}")


# ---------------------------------------------------------------------------
# Inspect
# ---------------------------------------------------------------------------

@dataclass
class GeomFileInfo:
    geom_id: str           # e.g. "g01"
    geom_number: str       # e.g. "01"
    hdf_path: Optional[Path]
    text_path: Optional[Path]
    hdf_exists: bool
    text_exists: bool
    has_2d_mesh: bool = False
    has_1d_xs: bool = False


@dataclass
class PlanFileInfo:
    plan_id: str           # e.g. "p01"
    plan_number: str       # e.g. "01"
    plan_title: str
    geom_number: Optional[str]
    flow_id: Optional[str]
    hdf_path: Optional[Path]
    hdf_exists: bool
    completed: Optional[bool] = None


@dataclass
class TerrainFileInfo:
    """Detailed terrain layer information from rasmap."""
    name: str
    hdf_path: Optional[Path] = None
    hdf_exists: bool = False
    tif_files: list[Path] = field(default_factory=list)
    crs: Optional[str] = None
    resolution: Optional[str] = None       # e.g. "50.0 x 50.0"
    total_size_mb: float = 0.0


@dataclass
class ProjectInfo:
    name: str
    prj_file: Path
    project_dir: Path
    crs: Optional[str]
    units: str
    geom_files: list[GeomFileInfo] = field(default_factory=list)
    plan_files: list[PlanFileInfo] = field(default_factory=list)
    terrain_files: list[Path] = field(default_factory=list)
    ras_version: Optional[str] = None
    terrain_details: list[TerrainFileInfo] = field(default_factory=list)
    rasmap_path: Optional[Path] = None


def inspect_project(project_path: Path) -> ProjectInfo:
    """Discover HEC-RAS project structure without extracting any data.

    Args:
        project_path: Path to .prj file or project directory

    Returns:
        ProjectInfo with geometry files, plan files, and terrain discovered
    """
    project_dir, prj_file = resolve_project_path(Path(project_path))

    # Initialize with load_results_summary=False for speed (we just want structure)
    ras = init_ras_project(project_dir, ras_object="new", load_results_summary=False)

    # Detect CRS from a geometry HDF if available
    crs = _detect_project_crs(ras)

    # Build geometry file list
    geom_files: list[GeomFileInfo] = []
    if ras.geom_df is not None and not ras.geom_df.empty:
        for _, row in ras.geom_df.iterrows():
            geom_num = str(row.get("geom_number", "")).zfill(2)
            hdf_p = Path(str(row["hdf_path"])) if row.get("hdf_path") else None
            text_p = Path(str(row["full_path"])) if row.get("full_path") else None
            geom_files.append(GeomFileInfo(
                geom_id=f"g{geom_num}",
                geom_number=geom_num,
                hdf_path=hdf_p,
                text_path=text_p,
                hdf_exists=hdf_p.exists() if hdf_p else False,
                text_exists=text_p.exists() if text_p else False,
                has_2d_mesh=bool(row.get("has_2d_mesh", False)),
                has_1d_xs=bool(row.get("has_1d_xs", False)),
            ))

    # Build plan file list
    plan_files: list[PlanFileInfo] = []
    if ras.plan_df is not None and not ras.plan_df.empty:
        for _, row in ras.plan_df.iterrows():
            plan_num = str(row.get("plan_number", "")).zfill(2)
            geom_num = str(row.get("geometry_number", "")).zfill(2) if row.get("geometry_number") else None
            unsteady = row.get("unsteady_number")
            flow_num = str(unsteady).zfill(2) if unsteady else None
            hdf_p = project_dir / f"{ras.project_name}.p{plan_num}.hdf"
            plan_files.append(PlanFileInfo(
                plan_id=f"p{plan_num}",
                plan_number=plan_num,
                plan_title=str(row.get("Plan Title", row.get("plan_title", ""))),
                geom_number=geom_num,
                flow_id=f"u{flow_num}" if flow_num else None,
                hdf_path=hdf_p,
                hdf_exists=hdf_p.exists(),
            ))

    # Detect RAS version from plan_df
    ras_version = _detect_ras_version(ras)

    # Detect rasmap
    rasmap_path = None
    rasmap_files = list(project_dir.glob("*.rasmap"))
    if rasmap_files:
        rasmap_path = rasmap_files[0]

    # Discover terrain files (legacy flat list, deduplicate for case-insensitive FS)
    terrain_dir_path = project_dir / "Terrain"
    if terrain_dir_path.exists():
        terrain_files = sorted(set(
            list(terrain_dir_path.glob("*.tif")) + list(terrain_dir_path.glob("*.TIF"))
        ))
    else:
        terrain_files = []

    # Discover detailed terrain info
    terrain_details = _discover_terrain_details(ras, project_dir)

    # Populate plan completed status from results_df if available
    if ras.plan_df is not None and not ras.plan_df.empty:
        try:
            ras_with_results = init_ras_project(project_dir, ras_object="new", load_results_summary=True)
            if ras_with_results.results_df is not None and not ras_with_results.results_df.empty:
                for pf in plan_files:
                    mask = ras_with_results.results_df["plan_number"].astype(str).str.zfill(2) == pf.plan_number
                    if mask.any():
                        pf.completed = bool(ras_with_results.results_df.loc[mask, "completed"].iloc[0])
        except Exception:
            pass

    return ProjectInfo(
        name=ras.project_name,
        prj_file=prj_file,
        project_dir=project_dir,
        crs=crs,
        units=_detect_units(project_dir, prj_file),
        geom_files=geom_files,
        plan_files=plan_files,
        terrain_files=sorted(set(terrain_files)),
        ras_version=ras_version,
        terrain_details=terrain_details,
        rasmap_path=rasmap_path,
    )


def print_project_info(info: ProjectInfo, as_json: bool = False) -> None:
    """Print ProjectInfo to console as rich table or JSON."""
    if as_json:
        import json
        data = {
            "project": {
                "name": info.name,
                "prj_file": str(info.prj_file),
                "crs": info.crs,
                "units": info.units,
                "ras_version": info.ras_version,
                "rasmap": str(info.rasmap_path) if info.rasmap_path else None,
            },
            "geometry_files": [
                {
                    "geom_id": g.geom_id,
                    "hdf_exists": g.hdf_exists,
                    "text_exists": g.text_exists,
                    "has_2d_mesh": g.has_2d_mesh,
                    "has_1d_xs": g.has_1d_xs,
                }
                for g in info.geom_files
            ],
            "plan_files": [
                {
                    "plan_id": p.plan_id,
                    "plan_title": p.plan_title,
                    "geom_number": p.geom_number,
                    "flow_id": p.flow_id,
                    "hdf_exists": p.hdf_exists,
                    "completed": p.completed,
                }
                for p in info.plan_files
            ],
            "terrain_files": [str(t) for t in info.terrain_files],
            "terrain_details": [
                {
                    "name": td.name,
                    "hdf_path": str(td.hdf_path) if td.hdf_path else None,
                    "hdf_exists": td.hdf_exists,
                    "tif_count": len(td.tif_files),
                    "crs": td.crs,
                    "resolution": td.resolution,
                    "total_size_mb": round(td.total_size_mb, 2),
                }
                for td in info.terrain_details
            ],
        }
        console.print_json(json.dumps(data, indent=2))
        return

    console.print(f"\n[bold]Project:[/bold] {info.name}")
    console.print(f"  PRJ file    : {info.prj_file.name}")
    console.print(f"  RAS version : {info.ras_version or 'Unknown'}")
    console.print(f"  CRS         : {info.crs or 'Unknown'}")
    console.print(f"  Units       : {info.units}")
    console.print(f"  Rasmap      : {info.rasmap_path.name if info.rasmap_path else 'Not found'}")

    # Geometry table
    geom_table = Table(title="Geometry Files", show_lines=True)
    geom_table.add_column("ID", style="cyan")
    geom_table.add_column("HDF", justify="center")
    geom_table.add_column("Text", justify="center")
    geom_table.add_column("2D Mesh", justify="center")
    geom_table.add_column("1D XS", justify="center")
    for g in info.geom_files:
        geom_table.add_row(
            g.geom_id,
            "Y" if g.hdf_exists else "N",
            "Y" if g.text_exists else "N",
            "Y" if g.has_2d_mesh else "-",
            "Y" if g.has_1d_xs else "-",
        )
    console.print(geom_table)

    # Plan table with Completed column
    plan_table = Table(title="Plan Files", show_lines=True)
    plan_table.add_column("ID", style="cyan")
    plan_table.add_column("Title")
    plan_table.add_column("Geom", justify="center")
    plan_table.add_column("Flow", justify="center")
    plan_table.add_column("HDF Results", justify="center")
    plan_table.add_column("Completed", justify="center")
    for p in info.plan_files:
        completed_str = "-"
        if p.completed is True:
            completed_str = "Y"
        elif p.completed is False:
            completed_str = "N"
        plan_table.add_row(
            p.plan_id,
            p.plan_title or "-",
            f"g{p.geom_number}" if p.geom_number else "-",
            p.flow_id or "-",
            "Y" if p.hdf_exists else "N",
            completed_str,
        )
    console.print(plan_table)

    # Terrain details table
    if info.terrain_details:
        terrain_table = Table(title="Terrain", show_lines=True)
        terrain_table.add_column("Name", style="cyan")
        terrain_table.add_column("HDF", justify="center")
        terrain_table.add_column("TIF Count", justify="center")
        terrain_table.add_column("CRS")
        terrain_table.add_column("Resolution")
        terrain_table.add_column("Size (MB)", justify="right")
        for td in info.terrain_details:
            terrain_table.add_row(
                td.name,
                "Y" if td.hdf_exists else "N",
                str(len(td.tif_files)),
                td.crs or "-",
                td.resolution or "-",
                f"{td.total_size_mb:.1f}" if td.total_size_mb > 0 else "-",
            )
        console.print(terrain_table)
    elif info.terrain_files:
        console.print(f"\n[bold]Terrain:[/bold] {len(info.terrain_files)} raster(s)")
        for t in info.terrain_files:
            console.print(f"  {t.name}")


# ---------------------------------------------------------------------------
# GeoParquet writer with bbox + covering metadata
# ---------------------------------------------------------------------------

def _write_geoparquet(gdf, output_path: Path) -> None:
    """Write a GeoDataFrame to GeoParquet with bbox columns and covering metadata.

    Adds per-row bounding box columns (bbox_xmin, bbox_ymin, bbox_xmax, bbox_ymax),
    writes with ZSTD compression, and patches the GeoParquet ``geo`` metadata to
    include the ``covering`` spec so DuckDB/BigQuery can do spatial predicate pushdown.
    """
    import pyarrow.parquet as pq

    gdf = gdf.copy()

    # Add per-row bbox columns from geometry bounds
    bounds = gdf.geometry.bounds
    gdf["bbox_xmin"] = bounds["minx"].values
    gdf["bbox_ymin"] = bounds["miny"].values
    gdf["bbox_xmax"] = bounds["maxx"].values
    gdf["bbox_ymax"] = bounds["maxy"].values

    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Write initial GeoParquet (geopandas handles geo metadata + WKB encoding)
    gdf.to_parquet(output_path, compression="zstd", index=False)

    # Patch geo metadata to add covering (bbox) spec for spatial predicate pushdown
    table = pq.read_table(output_path)
    raw_geo = table.schema.metadata.get(b"geo")
    if raw_geo:
        geo_meta = json.loads(raw_geo.decode("utf-8"))
        geom_col = geo_meta.get("primary_column", "geometry")
        col_meta = geo_meta.get("columns", {}).get(geom_col, {})
        col_meta["covering"] = {
            "bbox": {
                "xmin": ["bbox_xmin"],
                "ymin": ["bbox_ymin"],
                "xmax": ["bbox_xmax"],
                "ymax": ["bbox_ymax"],
            }
        }
        geo_meta.setdefault("columns", {})[geom_col] = col_meta
        new_meta = {**table.schema.metadata, b"geo": json.dumps(geo_meta).encode("utf-8")}
        table = table.replace_schema_metadata(new_meta)
        pq.write_table(table, output_path, compression="zstd")


# ---------------------------------------------------------------------------
# Project metadata export
# ---------------------------------------------------------------------------

def export_project_metadata(ras, output_path: Path) -> None:
    """Export RasPrj dataframes to a single Parquet file with ``_table`` discriminator.

    Reads plan_df, geom_df, flow_df, unsteady_df, boundaries_df, results_df,
    rasmap_df from the RasPrj object and unions them into one DataFrame.
    """
    table_names = [
        "plan_df", "geom_df", "flow_df", "unsteady_df",
        "boundaries_df", "results_df", "rasmap_df",
    ]
    all_dfs: list[pd.DataFrame] = []

    for name in table_names:
        df = getattr(ras, name, None)
        if df is None or (hasattr(df, "empty") and df.empty):
            continue
        df = df.copy()
        df["_table"] = name
        all_dfs.append(df)

    if not all_dfs:
        return

    merged = pd.concat(all_dfs, ignore_index=True)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    merged.to_parquet(output_path, compression="zstd", index=False)


# ---------------------------------------------------------------------------
# Archive
# ---------------------------------------------------------------------------

def archive_project(
    project_path: Path,
    output_dir: Path,
    *,
    include_results: bool = False,
    include_terrain: bool = False,
    include_plan_geometry: bool = False,
    plans: Optional[list[str]] = None,
    skip_errors: bool = True,
    sort: bool = True,
    map_results: bool = False,
    consolidate_terrain: bool = False,
    ras_version: Optional[str] = None,
    rasprocess_path: Optional[Path] = None,
) -> Manifest:
    """Archive a HEC-RAS project to consolidated GeoParquet files.

    Produces one parquet per geometry source file and one per plan, plus a
    project metadata parquet. All geometry layers are merged into a single
    file with a ``layer`` discriminator column.

    Args:
        project_path: Path to .prj file or project directory
        output_dir: Archive root directory (created if needed)
        include_results: If True, export plan results summary variables
        include_terrain: If True, convert terrain TIFFs to Cloud Optimized GeoTIFF
        include_plan_geometry: If True (and include_results=True), also extract the
            geometry copy embedded in each plan HDF
        plans: Restrict results export to specific plan IDs (e.g. ["p01", "p02"]).
            None = all plans with .hdf results
        skip_errors: If True, log and continue past per-layer extraction errors
        sort: If True (default), apply Hilbert spatial sort within each layer
        map_results: If True, generate result rasters via RasProcess after export
        consolidate_terrain: If True, merge terrains into single COG
        ras_version: HEC-RAS version for RasProcess mapping
        rasprocess_path: Path to RasProcess.exe (required on Linux/Wine)

    Returns:
        Manifest: The completed project manifest (also written to output_dir/manifest.json)
    """
    project_path = Path(project_path)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    project_dir, prj_file = resolve_project_path(project_path)
    console.print(f"\n[bold cyan]ras2cng archive[/bold cyan] -> {output_dir}")
    console.print(f"  Project : {prj_file.name}")

    ras = init_ras_project(project_dir, ras_object="new", load_results_summary=include_results)

    crs = _detect_project_crs(ras)
    units = _detect_units(project_dir, prj_file)

    plan_count = len(ras.plan_df) if ras.plan_df is not None else 0
    geom_count = len(ras.geom_df) if ras.geom_df is not None else 0

    manifest = Manifest.create(
        project_name=ras.project_name,
        prj_file=prj_file,
        source_path=project_dir,
        archive_path=output_dir,
        crs=crs,
        units=units,
        plan_count=plan_count,
        geom_count=geom_count,
    )

    # -----------------------------------------------------------------
    # Step 1: Export geometry from all geometry files (consolidated)
    # -----------------------------------------------------------------
    console.print(f"\n[bold]Geometry:[/bold] {geom_count} file(s)")

    mesh_cells_by_geom: dict[str, object] = {}  # geom_number -> mesh_cells GeoDataFrame

    if ras.geom_df is not None and not ras.geom_df.empty:
        for _, row in ras.geom_df.iterrows():
            geom_num = str(row.get("geom_number", "")).zfill(2)
            geom_id = f"g{geom_num}"

            # Which plans use this geometry?
            plans_using = []
            if ras.plan_df is not None and not ras.plan_df.empty and "geometry_number" in ras.plan_df.columns:
                mask = ras.plan_df["geometry_number"].astype(str).str.zfill(2) == geom_num
                plans_using = [f"p{n.zfill(2)}" for n in ras.plan_df.loc[mask, "plan_number"].tolist()]

            # Resolve paths
            hdf_p = Path(str(row["hdf_path"])) if row.get("hdf_path") else None
            text_p = Path(str(row["full_path"])) if row.get("full_path") else None
            hdf_path = hdf_p if hdf_p and hdf_p.exists() else None
            text_path = text_p if text_p and text_p.exists() else None

            parquet_name = f"{ras.project_name}.g{geom_num}.parquet"
            parquet_path = output_dir / parquet_name

            geom_entry = ManifestGeomEntry(
                geom_id=geom_id,
                source_file=prj_file.parent.name + f"/{ras.project_name}.{geom_id}.hdf",
                file_type="",
                parquet=parquet_name,
                plans_using=plans_using,
            )
            file_types = []

            console.print(f"  [{geom_id}] -> {parquet_name}")
            try:
                merged_gdf = merge_all_layers(
                    hdf_path=hdf_path,
                    text_path=text_path,
                    sort=sort,
                )
                if merged_gdf is not None and len(merged_gdf) > 0:
                    if hdf_path:
                        file_types.append("hdf")
                    if text_path:
                        # Check if any text layers were actually extracted
                        text_layers = merged_gdf[merged_gdf["layer"].str.endswith("_text")]
                        if len(text_layers) > 0:
                            file_types.append("text")

                    _write_geoparquet(merged_gdf, parquet_path)

                    # Build layer metadata for manifest
                    for layer_val in merged_gdf["layer"].unique():
                        layer_subset = merged_gdf[merged_gdf["layer"] == layer_val]
                        geom_type = layer_subset.geometry.geom_type.iloc[0] if len(layer_subset) > 0 else "Unknown"
                        layer_crs = str(layer_subset.crs) if layer_subset.crs else None
                        if layer_subset.crs:
                            try:
                                epsg = layer_subset.crs.to_epsg()
                                if epsg:
                                    layer_crs = f"EPSG:{epsg}"
                            except Exception:
                                pass
                        geom_entry.add_layer(ManifestLayer(
                            layer=layer_val,
                            filter_value=layer_val,
                            rows=len(layer_subset),
                            geometry_type=geom_type,
                            crs=layer_crs,
                        ))

                    geom_entry.size_bytes = parquet_path.stat().st_size

                    # Extract mesh_cells subset for results join
                    mc_mask = merged_gdf["layer"] == "mesh_cells"
                    if mc_mask.any():
                        mc_gdf = merged_gdf[mc_mask].drop(columns=["layer"]).copy()
                        import geopandas as gpd
                        mesh_cells_by_geom[geom_num] = gpd.GeoDataFrame(
                            mc_gdf, geometry="geometry"
                        )

            except Exception as e:
                console.print(f"  [yellow]Warning:[/yellow] Geometry extraction failed for {geom_id}: {e}")
                if not skip_errors:
                    raise

            geom_entry.file_type = "+".join(file_types) if file_types else "unknown"
            if file_types:
                manifest.add_geom_entry(geom_entry)

    # -----------------------------------------------------------------
    # Step 2: Terrain conversion (opt-in)
    # -----------------------------------------------------------------
    if include_terrain:
        terrain_dir = project_dir / "Terrain"
        tif_files = sorted(set(list(terrain_dir.glob("*.tif")) + list(terrain_dir.glob("*.TIF")))) if terrain_dir.exists() else []
        if tif_files:
            console.print(f"\n[bold]Terrain:[/bold] {len(tif_files)} raster(s) -> COG")
            cog_out_dir = output_dir / "terrain"
            cog_out_dir.mkdir(parents=True, exist_ok=True)
            for tif in tif_files:
                cog_path = cog_out_dir / (tif.stem + "_cog.tif")
                try:
                    subprocess.run(
                        ["gdal_translate", "-of", "COG", str(tif), str(cog_path)],
                        check=True, capture_output=True,
                    )
                    terrain_crs = _tif_crs(tif)
                    manifest.add_terrain_entry(ManifestTerrainEntry(
                        source_file=str(tif.relative_to(project_dir)),
                        cog_file=str(cog_path.relative_to(output_dir)),
                        size_bytes=cog_path.stat().st_size,
                        crs=terrain_crs,
                    ))
                    console.print(f"  {tif.name} -> {cog_path.name}")
                except Exception as e:
                    console.print(f"  [yellow]Warning:[/yellow] COG conversion failed for {tif.name}: {e}")
                    if not skip_errors:
                        raise
        else:
            console.print("\n[bold]Terrain:[/bold] No .tif files found in Terrain/")

    # -----------------------------------------------------------------
    # Step 3: Plan results (opt-in, consolidated)
    # -----------------------------------------------------------------
    if include_results:
        from ras2cng.results import merge_all_variables, list_available_summary_variables

        plan_filter = set(plans) if plans else None
        plan_rows = ras.plan_df if ras.plan_df is not None and not ras.plan_df.empty else []

        console.print(f"\n[bold]Results:[/bold] {len(plan_rows)} plan(s) in project")

        for _, row in (plan_rows.iterrows() if hasattr(plan_rows, "iterrows") else []):
            plan_num = str(row.get("plan_number", "")).zfill(2)
            plan_id = f"p{plan_num}"

            if plan_filter and plan_id not in plan_filter:
                continue

            plan_hdf = project_dir / f"{ras.project_name}.p{plan_num}.hdf"
            if not plan_hdf.exists():
                console.print(f"  [{plan_id}] No HDF results - skipping")
                continue

            geom_num = str(row.get("geometry_number", "")).zfill(2)
            unsteady = row.get("unsteady_number")
            flow_id = f"u{str(unsteady).zfill(2)}" if unsteady else None
            mesh_cells_gdf = mesh_cells_by_geom.get(geom_num)

            # Determine completed status from results_df if available
            completed = None
            if ras.results_df is not None and not ras.results_df.empty:
                mask = ras.results_df["plan_number"] == row["plan_number"]
                if mask.any():
                    completed = bool(ras.results_df.loc[mask, "completed"].iloc[0])

            parquet_name = f"{ras.project_name}.p{plan_num}.parquet"
            parquet_path = output_dir / parquet_name

            plan_entry = ManifestPlanEntry(
                plan_id=plan_id,
                plan_title=str(row.get("Plan Title", row.get("plan_title", ""))),
                geom_id=f"g{geom_num}",
                flow_id=flow_id,
                hdf_exists=True,
                completed=bool(completed) if completed is not None else True,
                parquet=parquet_name,
            )

            console.print(f"  [{plan_id}] -> {parquet_name}")
            try:
                results_gdf = merge_all_variables(plan_hdf, mesh_cells_gdf=mesh_cells_gdf)
                if results_gdf is not None and len(results_gdf) > 0:
                    _write_geoparquet(results_gdf, parquet_path)

                    for var_name in results_gdf["layer"].unique():
                        var_subset = results_gdf[results_gdf["layer"] == var_name]
                        plan_entry.add_variable(ManifestResultVariable(
                            variable=var_name,
                            filter_value=var_name,
                            rows=len(var_subset),
                        ))

                    plan_entry.size_bytes = parquet_path.stat().st_size
            except Exception as e:
                console.print(f"  [yellow]Warning:[/yellow] Results export failed for {plan_id}: {e}")
                if not skip_errors:
                    raise

            manifest.add_plan_entry(plan_entry)

            # Optional: geometry copy from plan HDF
            if include_plan_geometry:
                pg_out = output_dir / "plan_geometry" / plan_id
                try:
                    export_all_hdf_layers(plan_hdf, pg_out)
                except Exception as e:
                    console.print(f"  [yellow]Warning:[/yellow] Plan geometry extraction failed for {plan_id}: {e}")
                    if not skip_errors:
                        raise

    # -----------------------------------------------------------------
    # Step 3b: Terrain consolidation (opt-in)
    # -----------------------------------------------------------------
    if consolidate_terrain:
        try:
            from ras2cng.terrain import consolidate_terrain as _consolidate_terrain

            console.print("\n[bold]Terrain Consolidation:[/bold]")
            terrain_out = output_dir / "terrain"
            consolidated_path = _consolidate_terrain(
                project_path,
                terrain_out,
                terrain_name="Consolidated",
                create_hdf=False,  # For archive, just produce the COG
                register_rasmap=False,
            )

            # Convert consolidated TIFF to COG for the archive
            if consolidated_path.exists() and consolidated_path.suffix.lower() == ".tif":
                import subprocess as _sp
                cog_path = terrain_out / f"{consolidated_path.stem}_cog.tif"
                try:
                    _sp.run(
                        ["gdal_translate", "-of", "COG", str(consolidated_path), str(cog_path)],
                        check=True, capture_output=True,
                    )
                    terrain_crs = _tif_crs(consolidated_path)
                    manifest.add_terrain_entry(ManifestTerrainEntry(
                        source_file="Consolidated",
                        cog_file=str(cog_path.relative_to(output_dir)),
                        size_bytes=cog_path.stat().st_size,
                        crs=terrain_crs,
                    ))
                    console.print(f"  Consolidated COG -> {cog_path.name}")
                except Exception as e:
                    console.print(f"  [yellow]Warning:[/yellow] COG conversion of consolidated terrain failed: {e}")
                    if not skip_errors:
                        raise
        except Exception as e:
            console.print(f"  [yellow]Warning:[/yellow] Terrain consolidation failed: {e}")
            if not skip_errors:
                raise

    # -----------------------------------------------------------------
    # Step 3c: Map generation (opt-in)
    # -----------------------------------------------------------------
    if map_results:
        try:
            from ras2cng.mapping import generate_result_maps
            from ras2cng.catalog import ManifestMapEntry

            console.print("\n[bold]Map Generation:[/bold]")
            plan_filter_list = list(set(plans)) if plans else None
            map_output = output_dir / "maps"

            map_results_list = generate_result_maps(
                project_path,
                map_output,
                plans=plan_filter_list,
                ras_version=ras_version,
                rasprocess_path=rasprocess_path,
                skip_errors=skip_errors,
            )

            for mr in map_results_list:
                raster_list = []
                for map_type, paths in mr.map_types.items():
                    for p in paths:
                        raster_list.append({
                            "type": map_type,
                            "file": str(p.relative_to(output_dir)) if p.is_relative_to(output_dir) else str(p),
                            "size_bytes": p.stat().st_size if p.exists() else 0,
                        })

                manifest.add_map_entry(ManifestMapEntry(
                    plan_id=mr.plan_id,
                    profile="Max",
                    rasters=raster_list,
                ))
        except Exception as e:
            console.print(f"  [yellow]Warning:[/yellow] Map generation failed: {e}")
            if not skip_errors:
                raise

    # -----------------------------------------------------------------
    # Step 4: Project metadata parquet
    # -----------------------------------------------------------------
    meta_parquet_name = f"{ras.project_name}.parquet"
    meta_parquet_path = output_dir / meta_parquet_name
    try:
        export_project_metadata(ras, meta_parquet_path)
        manifest.project_parquet = meta_parquet_name
        console.print(f"\n[bold]Metadata:[/bold] {meta_parquet_name}")
    except Exception as e:
        console.print(f"  [yellow]Warning:[/yellow] Project metadata export failed: {e}")
        if not skip_errors:
            raise

    # -----------------------------------------------------------------
    # Write manifest
    # -----------------------------------------------------------------
    manifest_path = output_dir / "manifest.json"
    manifest.write(manifest_path)
    console.print(f"\n[green]OK[/green] manifest.json written -> {manifest_path}")
    console.print(f"[green]OK[/green] Archive complete: {output_dir}\n")

    return manifest


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _detect_project_crs(ras) -> Optional[str]:
    """Try to detect CRS from the first available geometry HDF."""
    try:
        from ras_commander.hdf import HdfBase
        if ras.geom_df is None or ras.geom_df.empty:
            return None
        for _, row in ras.geom_df.iterrows():
            hdf_p = Path(str(row["hdf_path"])) if row.get("hdf_path") else None
            if hdf_p and hdf_p.exists():
                proj = HdfBase.get_projection(hdf_p)
                if proj:
                    # Try to convert WKT/PROJ string to EPSG code
                    try:
                        from pyproj import CRS
                        epsg = CRS.from_user_input(proj).to_epsg()
                        return f"EPSG:{epsg}" if epsg else proj[:60]
                    except Exception:
                        return proj[:60]
    except Exception:
        pass
    return None


def _detect_units(project_dir: Path, prj_file: Path) -> str:
    """Read project units from .prj file ('English Units' or 'Metric Units')."""
    try:
        text = prj_file.read_text(encoding="utf-8", errors="replace")
        if "English Units" in text:
            return "US Survey Feet"
        if "Metric Units" in text:
            return "Meters"
    except Exception:
        pass
    return "Unknown"


def _parquet_meta(path: Path) -> dict:
    """Read row count, geometry type, and CRS from a parquet file."""
    try:
        import geopandas as gpd
        gdf = gpd.read_parquet(path)
        geom_type = gdf.geometry.geom_type.iloc[0] if len(gdf) > 0 else "Unknown"
        crs_str = gdf.crs.to_epsg() if gdf.crs else None
        crs_out = f"EPSG:{crs_str}" if crs_str else (str(gdf.crs) if gdf.crs else None)
        return {"rows": len(gdf), "geometry_type": geom_type, "crs": crs_out}
    except Exception:
        return {"rows": 0, "geometry_type": "Unknown", "crs": None}


def _tif_crs(tif_path: Path) -> Optional[str]:
    """Get CRS string from a GeoTIFF."""
    try:
        import rasterio
        with rasterio.open(tif_path) as src:
            epsg = src.crs.to_epsg() if src.crs else None
            return f"EPSG:{epsg}" if epsg else None
    except Exception:
        return None


def _detect_ras_version(ras) -> Optional[str]:
    """Detect HEC-RAS version from plan_df or plan HDF attributes."""
    # Try plan_df first (ras-commander parses "Program Version=" from plan files)
    if ras.plan_df is not None and not ras.plan_df.empty:
        for col in ["program_version", "Program Version", "ras_version"]:
            if col in ras.plan_df.columns:
                val = ras.plan_df[col].dropna()
                if len(val) > 0:
                    return str(val.iloc[0]).strip()

    # Fall back to reading plan HDF attribute
    try:
        if ras.plan_df is not None and not ras.plan_df.empty:
            plan_num = str(ras.plan_df.iloc[0].get("plan_number", "01")).zfill(2)
            project_dir = Path(ras.project_folder) if hasattr(ras, "project_folder") else None
            if project_dir:
                plan_hdf = project_dir / f"{ras.project_name}.p{plan_num}.hdf"
                if plan_hdf.exists():
                    import h5py
                    with h5py.File(plan_hdf, "r") as hf:
                        # Try common attribute locations
                        for attr_path in [
                            "Plan Data/Plan Information",
                            "Plan Data/Plan Parameters",
                        ]:
                            grp = hf.get(attr_path)
                            if grp is not None:
                                for attr_name in ["Program Version", "HEC-RAS Version"]:
                                    if attr_name in grp.attrs:
                                        val = grp.attrs[attr_name]
                                        if isinstance(val, bytes):
                                            val = val.decode("utf-8")
                                        return str(val).strip()
    except Exception:
        pass

    return None


def _discover_terrain_details(ras, project_dir: Path) -> list[TerrainFileInfo]:
    """Discover detailed terrain information from rasmap and filesystem.

    Uses RasMap.get_terrain_names() if available, falls back to scanning
    the Terrain/ directory for HDF and TIF files.
    """
    terrain_details: list[TerrainFileInfo] = []

    # Try to get terrain info from rasmap
    try:
        from ras_commander import RasMap
        rasmap_files = list(project_dir.glob("*.rasmap"))
        if rasmap_files:
            terrain_names = RasMap.get_terrain_names(str(rasmap_files[0]))
            if terrain_names:
                # Get HDF paths from rasmap_df if available
                hdf_paths: dict[str, Path] = {}
                if ras.rasmap_df is not None and not ras.rasmap_df.empty:
                    if "terrain_hdf_path" in ras.rasmap_df.columns:
                        for _, row in ras.rasmap_df.iterrows():
                            name = str(row.get("terrain_name", ""))
                            hdf_p = row.get("terrain_hdf_path")
                            if hdf_p and str(hdf_p).strip():
                                p = Path(str(hdf_p))
                                if not p.is_absolute():
                                    p = project_dir / p
                                hdf_paths[name] = p

                for name in terrain_names:
                    hdf_path = hdf_paths.get(name)
                    tif_files = _discover_terrain_tifs(hdf_path, project_dir, name)
                    raster_info = _get_terrain_raster_info(tif_files)

                    terrain_details.append(TerrainFileInfo(
                        name=name,
                        hdf_path=hdf_path,
                        hdf_exists=hdf_path.exists() if hdf_path else False,
                        tif_files=tif_files,
                        crs=raster_info.get("crs"),
                        resolution=raster_info.get("resolution"),
                        total_size_mb=sum(
                            f.stat().st_size for f in tif_files if f.exists()
                        ) / (1024 * 1024) if tif_files else 0.0,
                    ))
                return terrain_details
    except Exception:
        pass

    # Fallback: scan Terrain/ directory
    terrain_dir = project_dir / "Terrain"
    if not terrain_dir.exists():
        return terrain_details

    hdf_files = sorted(terrain_dir.glob("*.hdf"))
    if hdf_files:
        for hdf_f in hdf_files:
            tif_files = _discover_terrain_tifs(hdf_f, project_dir, hdf_f.stem)
            raster_info = _get_terrain_raster_info(tif_files)
            terrain_details.append(TerrainFileInfo(
                name=hdf_f.stem,
                hdf_path=hdf_f,
                hdf_exists=True,
                tif_files=tif_files,
                crs=raster_info.get("crs"),
                resolution=raster_info.get("resolution"),
                total_size_mb=sum(
                    f.stat().st_size for f in tif_files if f.exists()
                ) / (1024 * 1024) if tif_files else 0.0,
            ))
    else:
        # Just TIF files, no HDFs
        tif_files = sorted(set(
            list(terrain_dir.glob("*.tif")) + list(terrain_dir.glob("*.TIF"))
        ))
        if tif_files:
            raster_info = _get_terrain_raster_info(tif_files)
            terrain_details.append(TerrainFileInfo(
                name="Terrain",
                tif_files=tif_files,
                crs=raster_info.get("crs"),
                resolution=raster_info.get("resolution"),
                total_size_mb=sum(
                    f.stat().st_size for f in tif_files if f.exists()
                ) / (1024 * 1024),
            ))

    return terrain_details


def _discover_terrain_tifs(
    hdf_path: Optional[Path],
    project_dir: Path,
    terrain_name: str,
) -> list[Path]:
    """Find TIF files associated with a terrain HDF or name."""
    tifs: list[Path] = []

    # Look near the HDF file first
    if hdf_path and hdf_path.parent.exists():
        parent = hdf_path.parent
        stem = hdf_path.stem
        tifs = sorted(set(
            list(parent.glob(f"{stem}*.tif"))
            + list(parent.glob(f"{stem}*.TIF"))
        ))

    # Fall back to Terrain/ directory
    if not tifs:
        terrain_dir = project_dir / "Terrain"
        if terrain_dir.exists():
            all_tifs = sorted(set(
                list(terrain_dir.glob("*.tif")) + list(terrain_dir.glob("*.TIF"))
            ))
            tifs = sorted(f for f in all_tifs if terrain_name.lower() in f.stem.lower())
            # If still nothing, grab all TIFs
            if not tifs:
                tifs = all_tifs

    return tifs


def _get_terrain_raster_info(tif_files: list[Path]) -> dict:
    """Read CRS and resolution from the first available TIFF (lazy rasterio import)."""
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
                return {"crs": crs_str, "resolution": f"{res_x:.1f} x {res_y:.1f}"}
        except Exception:
            continue
    return {}
