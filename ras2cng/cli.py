"""ras2cng: Full-project archival and cloud-native export for HEC-RAS."""

from __future__ import annotations

import json
from enum import Enum
from pathlib import Path
from typing import List, Optional

import typer
from rich.console import Console

app = typer.Typer(
    help=(
        "ras2cng — HEC-RAS to Cloud Native GIS.\n\n"
        "Archive full projects or export individual files to GeoParquet, "
        "DuckDB, PMTiles, and PostGIS."
    )
)
console = Console()


class BoundaryMethod(str, Enum):
    """Explicit inundation-boundary generation authority."""

    rasmapper = "rasmapper"
    depth_raster = "depth-raster"


@app.command("inspect")
def inspect_command(
    project: Path = typer.Argument(
        ..., help="HEC-RAS project directory or .prj file"
    ),
    as_json: bool = typer.Option(False, "--json", help="Output as JSON instead of table"),
):
    """Inspect a HEC-RAS project structure without extracting any data."""

    from ras2cng.project import inspect_project, print_project_info

    try:
        info = inspect_project(project)
        print_project_info(info, as_json=as_json)
    except Exception as e:
        Console().print(f"[red]ERROR:[/red] {e}")
        raise typer.Exit(1)


@app.command("archive")
def archive_command(
    project: Path = typer.Argument(
        ..., help="HEC-RAS project directory or .prj file"
    ),
    output: Path = typer.Argument(
        ..., help="Archive output directory (created if needed)"
    ),
    results: bool = typer.Option(
        False, "--results/--no-results", help="Include plan results (summary variables)"
    ),
    terrain: bool = typer.Option(
        False, "--terrain/--no-terrain", help="Convert terrain TIFFs to Cloud Optimized GeoTIFF"
    ),
    plan_geometry: bool = typer.Option(
        False, "--plan-geometry", help="Also extract geometry copy embedded in plan HDF files"
    ),
    plans: Optional[str] = typer.Option(
        None, "--plans", help="Comma-separated plan IDs to include, e.g. p01,p02 (default: all)"
    ),
    result_variables: Optional[str] = typer.Option(
        None,
        "--result-variables",
        help=(
            "Comma-separated result summary variables or slugs to include "
            "(default: all available variables)"
        ),
    ),
    results_layout: str = typer.Option(
        "plan",
        "--results-layout",
        help="Results output layout: plan or variable",
    ),
    results_geometry: str = typer.Option(
        "polygon",
        "--results-geometry",
        help="Results geometry mode: polygon, point, or none",
    ),
    auxiliary_results: bool = typer.Option(
        True,
        "--auxiliary-results/--mesh-results-only",
        help="Include reference, structure, pump, and pipe raw result summaries",
    ),
    skip_errors: bool = typer.Option(
        True, "--skip-errors/--fail-fast", help="Skip individual layer errors vs abort"
    ),
    no_sort: bool = typer.Option(
        False, "--no-sort", help="Disable Hilbert spatial post-processing (on by default)"
    ),
    map_results: bool = typer.Option(
        False, "--map/--no-map", help="Generate result rasters via RasStoreMapHelper"
    ),
    consolidate_terrain: bool = typer.Option(
        False,
        "--consolidate-terrain",
        help="Create one authoritative COG per named terrain",
    ),
    terrain_resolution: list[str] = typer.Option(
        [],
        "--terrain-resolution",
        help="Explicit named-terrain cell size as NAME=VALUE; repeat as needed",
    ),
    render_mode: Optional[str] = typer.Option(
        None, "--render-mode", help="Water surface render mode: horizontal, sloping, slopingPretty"
    ),
    ras_version: Optional[str] = typer.Option(
        None, "--ras-version", help="HEC-RAS version for RasProcess mapping"
    ),
    rasprocess: Optional[Path] = typer.Option(
        None, "--rasprocess", help="Path to HEC-RAS install directory (for helper deployment)"
    ),
    crs: Optional[str] = typer.Option(
        None,
        "--crs",
        help="Validated project CRS override when it is absent from source HDF files",
    ),
):
    """Archive a HEC-RAS project to consolidated GeoParquet files.

    Produces one parquet per geometry file and one per plan, plus a project
    metadata parquet. All layers within each file are distinguished by a
    ``layer`` column — query with ``WHERE layer = 'mesh_cells'``.

    Geometry is exported by default. Results, terrain, and map generation are opt-in.
    """

    from ras2cng.project import archive_project

    plans_list = [p.strip() for p in plans.split(",")] if plans else None
    result_variables_list = [v.strip() for v in result_variables.split(",")] if result_variables else None
    terrain_targets: dict[str, float] = {}
    for item in terrain_resolution:
        name, separator, raw_value = item.partition("=")
        if not separator or not name.strip() or not raw_value.strip():
            Console().print("[red]ERROR:[/red] --terrain-resolution must use NAME=VALUE syntax")
            raise typer.Exit(2)
        try:
            terrain_targets[name.strip()] = float(raw_value)
        except ValueError:
            Console().print(f"[red]ERROR:[/red] Invalid terrain resolution: {item}")
            raise typer.Exit(2)

    try:
        archive_project(
            project,
            output,
            include_results=results,
            include_terrain=terrain,
            include_plan_geometry=plan_geometry,
            plans=plans_list,
            result_variables=result_variables_list,
            results_layout=results_layout,
            results_geometry=results_geometry,
            include_auxiliary_results=auxiliary_results,
            skip_errors=skip_errors,
            sort=not no_sort,
            map_results=map_results,
            consolidate_terrain=consolidate_terrain,
            terrain_target_resolutions=terrain_targets or None,
            render_mode=render_mode,
            ras_version=ras_version,
            rasprocess_path=rasprocess,
            crs=crs,
        )
    except Exception as e:
        Console().print(f"[red]ERROR:[/red] {e}")
        raise typer.Exit(1)


@app.command("spatial-index")
def spatial_index_command(
    archive_dir: Path = typer.Argument(..., help="ras2cng archive directory containing manifest.json"),
    hilbert_level: int = typer.Option(16, "--hilbert-level", help="Hilbert curve level"),
    skip_errors: bool = typer.Option(
        True, "--skip-errors/--fail-fast", help="Skip individual parquet errors vs abort"
    ),
):
    """Post-process an existing archive with Hilbert sorting and join indexes."""

    from ras2cng.spatial_index import postprocess_archive

    try:
        summary = postprocess_archive(
            archive_dir,
            hilbert_level=hilbert_level,
            skip_errors=skip_errors,
        )
        console.print(
            "[green]OK[/green] Spatial index complete: "
            f"{summary.get('geometry_file_count', 0)} geometry file(s), "
            f"{summary.get('result_file_count', 0)} result file(s), "
            f"{summary.get('error_count', 0)} error(s)"
        )
    except Exception as e:
        Console().print(f"[red]ERROR:[/red] {e}")
        raise typer.Exit(1)


@app.command("geometry")
def export_geometry(
    geom_file: Path = typer.Argument(
        ..., help="HEC-RAS geometry file (*.g??) or geometry HDF (*.g??.hdf)"
    ),
    output: Path = typer.Argument(..., help="Output GeoParquet file path"),
    layer: Optional[str] = typer.Option(
        None,
        "--layer",
        "-l",
        help=(
            "Geometry layer: mesh_cells, mesh_faces, mesh_areas, cross_sections, "
            "centerlines, bank_lines, bc_lines, breaklines, refinement_regions, "
            "reference_lines, reference_points, structures, pipe_conduits, "
            "pipe_nodes, storage_areas"
        ),
    ),
    out_crs: Optional[str] = typer.Option(
        "EPSG:4326",
        "--out-crs",
        help="Output CRS (default EPSG:4326). Set to empty string to skip reprojection.",
    ),
):
    """Export HEC-RAS geometry to GeoParquet."""

    from ras2cng.geometry import export_geometry_layers

    # Treat empty string as None (no reprojection)
    effective_crs = out_crs if out_crs else None

    console.print(f"[bold blue]Exporting geometry:[/bold blue] {geom_file}")
    try:
        export_geometry_layers(geom_file, output, layer=layer, out_crs=effective_crs)
        console.print(f"[green]OK[/green] Exported to {output}")
    except Exception as e:
        console.print(f"[red]ERROR:[/red] {e}")
        raise typer.Exit(1)


@app.command("results")
def export_results(
    plan_hdf: Path = typer.Argument(..., help="HEC-RAS plan HDF file (*.p??.hdf)"),
    output: Path = typer.Argument(
        ..., help="Output GeoParquet path (or directory when using --all)"
    ),
    geom_file: Optional[Path] = typer.Option(
        None, "--geometry", "-g", help="Geometry GeoParquet for spatial join"
    ),
    variable: str = typer.Option(
        "Maximum Depth", "--var", "-v", help="Result variable to export"
    ),
    export_all: bool = typer.Option(
        False,
        "--all",
        help="Export all available summary variables to the output directory",
    ),
):
    """Export HEC-RAS 2D mesh summary results to GeoParquet."""

    from ras2cng.results import export_all_variables, export_results_layer

    console.print(f"[bold blue]Exporting results:[/bold blue] {plan_hdf}")
    try:
        if export_all:
            out_dir = Path(output)
            console.print(f"[dim]Exporting all variables → {out_dir}[/dim]")
            exported = export_all_variables(plan_hdf, out_dir, geom_file=geom_file)
            console.print(f"[green]OK[/green] Exported {len(exported)} variables")
        else:
            console.print(f"[dim]Variable: {variable}[/dim]")
            export_results_layer(plan_hdf, output, variable=variable, geom_file=geom_file)
            console.print(f"[green]OK[/green] Exported to {output}")
    except Exception as e:
        console.print(f"[red]ERROR:[/red] {e}")
        raise typer.Exit(1)


@app.command("precip")
def export_precipitation(
    hdf_file: Path = typer.Argument(
        ..., help="HEC-RAS plan or unsteady HDF file containing gridded precipitation"
    ),
    output: Path = typer.Argument(
        ..., help="Output directory for precipitation GeoTIFFs"
    ),
    source: str = typer.Option(
        "auto", "--source", help="Precipitation source: auto, processed, or imported"
    ),
    timestamps: Optional[str] = typer.Option(
        None,
        "--timestamps",
        help=(
            "Comma-separated timestamp labels or integer indices to export. "
            "Each token is matched against timestamp labels first, then "
            "interpreted as a zero-based index if no label matches."
        ),
    ),
    units: str = typer.Option(
        "native",
        "--units",
        help=(
            "Output units: native (default, no conversion), in, or mm. "
            "in/mm convert raster values (mm->in /25.4, in->mm *25.4)."
        ),
    ),
    incremental: bool = typer.Option(
        True,
        "--incremental/--no-incremental",
        help="Write per-timestep precipitation rasters",
    ),
    cumulative: bool = typer.Option(
        True,
        "--cumulative/--no-cumulative",
        help="Write cumulative-through-timestep precipitation rasters",
    ),
    prefix: Optional[str] = typer.Option(
        None, "--prefix", help="Optional filename prefix"
    ),
    no_overwrite: bool = typer.Option(
        False, "--no-overwrite", help="Fail if an output GeoTIFF already exists"
    ),
):
    """Export gridded precipitation and cumulative precipitation GeoTIFFs."""

    from ras2cng.precipitation import export_precipitation_rasters

    timestamp_list: list[str | int] | None = None
    if timestamps:
        timestamp_list = [part.strip() for part in timestamps.split(",") if part.strip()]

    try:
        result = export_precipitation_rasters(
            hdf_file,
            output,
            source=source,  # type: ignore[arg-type]
            timestamps=timestamp_list,
            units=units,  # type: ignore[arg-type]
            export_incremental=incremental,
            export_cumulative=cumulative,
            prefix=prefix,
            overwrite=not no_overwrite,
        )
        console.print(f"[green]OK[/green] Exported precipitation rasters to {output}")
        console.print(f"  Source     : {result.source} ({result.values_path})")
        console.print(f"  Timesteps  : {len(result.timestamps)}")
        console.print(f"  Incremental: {len(result.incremental)}")
        console.print(f"  Cumulative : {len(result.cumulative)}")
        if result.units:
            console.print(f"  Units      : {result.units}")
    except Exception as e:
        console.print(f"[red]ERROR:[/red] {e}")
        raise typer.Exit(1)


@app.command("query")
def query_parquet(
    input_file: Path = typer.Argument(..., help="Input GeoParquet file"),
    sql: str = typer.Argument(..., help="SQL query (use _ as table name)"),
    output: Optional[Path] = typer.Option(
        None, "--output", "-o", help="Optional output file (CSV or Parquet)"
    ),
):
    """Query GeoParquet files using DuckDB SQL."""

    from ras2cng.duckdb_session import query_parquet as _query

    console.print(f"[bold blue]Querying:[/bold blue] {input_file}")
    try:
        df = _query(input_file, sql)
        console.print(f"[green]OK[/green] Query returned {len(df)} rows")

        if output:
            if output.suffix.lower() == ".csv":
                df.to_csv(output, index=False)
            else:
                df.to_parquet(output, index=False)
            console.print(f"[green]OK[/green] Results saved to {output}")
        else:
            console.print(df.head(20).to_string())
    except Exception as e:
        console.print(f"[red]ERROR:[/red] {e}")
        raise typer.Exit(1)


@app.command("pmtiles")
def generate_pmtiles(
    input_file: Path = typer.Argument(..., help="Input GeoParquet file or GeoTIFF"),
    output: Path = typer.Argument(..., help="Output PMTiles file path"),
    layer_name: str = typer.Option("layer", "--layer", "-l", help="Vector tile layer name"),
    min_zoom: Optional[int] = typer.Option(None, "--min-zoom", help="Minimum zoom"),
    max_zoom: Optional[int] = typer.Option(None, "--max-zoom", help="Maximum zoom"),
):
    """Generate PMTiles from GeoParquet (vector) or GeoTIFF (raster)."""

    from ras2cng.pmtiles import generate_pmtiles_from_input

    console.print(f"[bold blue]Generating PMTiles:[/bold blue] {input_file}")
    try:
        generate_pmtiles_from_input(
            input_file,
            output,
            layer_name=layer_name,
            min_zoom=min_zoom,
            max_zoom=max_zoom,
        )
        console.print(f"[green]OK[/green] PMTiles created: {output}")
    except Exception as e:
        console.print(f"[red]ERROR:[/red] {e}")
        raise typer.Exit(1)


@app.command("maplibre")
def maplibre_command(
    archive_dir: Path = typer.Argument(..., help="ras2cng archive directory containing manifest.json"),
    output: Path = typer.Argument(..., help="Empty output directory for the MapLibre viewer bundle"),
    geometry_hdf: List[str] = typer.Option(
        ...,
        "--geometry-hdf",
        help="Original geometry HDF mapping, repeat as g01=path/to/model.g01.hdf",
    ),
    title: Optional[str] = typer.Option(None, "--title", help="Viewer title (default: archive project title)"),
    source_project: Optional[str] = typer.Option(
        None,
        "--source-project",
        help="Public project metadata URL or relative path",
    ),
    crs: Optional[str] = typer.Option(
        None,
        "--crs",
        help="Validated project CRS override when it is absent from the geometry HDF",
    ),
    vector_results: bool = typer.Option(
        False,
        "--vector-results/--geometry-only",
        help="Publish raw HDF vector result values joined to source geometry",
    ),
    primary_geometry: Optional[str] = typer.Option(
        None,
        "--primary-geometry",
        help="Geometry ID enabled initially, such as g02",
    ),
    all_primary_geometry: bool = typer.Option(
        False,
        "--all-primary-geometry/--standard-primary-geometry",
        help="Enable every published layer in the primary geometry initially",
    ),
    scratch_dir: Optional[Path] = typer.Option(
        None,
        "--scratch-dir",
        help="Large local scratch directory for temporary GeoJSON and Tippecanoe work files",
    ),
    min_zoom: int = typer.Option(0, "--min-zoom", help="Tippecanoe minimum zoom"),
    max_zoom: int = typer.Option(17, "--max-zoom", help="Tippecanoe maximum zoom"),
):
    """Build a MapLibre PMTiles bundle from a completed ras2cng archive.

    Geometry HDF mappings are required so model footprints come from
    ``HdfProject.get_project_extent(geometry_type='footprint')``. Raster
    results are deliberately excluded; publish RasProcess stored-map COGs in a
    later raster-results step.
    """

    from ras2cng.maplibre import package_maplibre_viewer

    mappings: dict[str, Path] = {}
    for item in geometry_hdf:
        if "=" not in item:
            console.print("[red]ERROR:[/red] --geometry-hdf must use geom_id=path syntax")
            raise typer.Exit(2)
        geom_id, raw_path = item.split("=", 1)
        geom_id = geom_id.strip()
        if not geom_id or not raw_path.strip():
            console.print("[red]ERROR:[/red] --geometry-hdf must use geom_id=path syntax")
            raise typer.Exit(2)
        mappings[geom_id] = Path(raw_path.strip())

    try:
        summary = package_maplibre_viewer(
            archive_dir,
            output,
            geometry_hdfs=mappings,
            title=title,
            source_project=source_project,
            crs=crs,
            include_vector_results=vector_results,
            primary_geometry=primary_geometry,
            show_all_primary_geometry=all_primary_geometry,
            scratch_dir=scratch_dir,
            min_zoom=min_zoom,
            max_zoom=max_zoom,
        )
        console.print(
            "[green]OK[/green] MapLibre bundle created: "
            f"{summary.geometry_layer_count} geometry layer(s), "
            f"{summary.result_layer_count} raw result layer(s)"
        )
    except Exception as e:
        console.print(f"[red]ERROR:[/red] {e}")
        raise typer.Exit(1)


@app.command("maplibre-terrain")
def maplibre_terrain_command(
    cog_path: Path = typer.Argument(..., help="Archived HEC-RAS terrain Cloud Optimized GeoTIFF"),
    viewer_dir: Path = typer.Argument(..., help="Existing MapLibre viewer directory containing manifest.json"),
    name: str = typer.Option("Terrain", "--name", help="Terrain layer display name"),
    source_cog: Optional[str] = typer.Option(
        None,
        "--source-cog",
        help="Source COG href relative to the viewer manifest (for exact identify values)",
    ),
    units: str = typer.Option("ft", "--units", help="Elevation units shown in identify results"),
    max_zoom: Optional[int] = typer.Option(
        None,
        "--max-zoom",
        help="Maximum display zoom; never exceeds the terrain's native resolution",
    ),
    scratch_dir: Optional[Path] = typer.Option(
        None,
        "--scratch-dir",
        help="Large local scratch directory for colorization and raster tile generation",
    ),
    overwrite: bool = typer.Option(False, "--overwrite", help="Replace an existing terrain layer"),
):
    """Publish a RAS-styled, queryable terrain layer into an existing viewer."""

    from ras2cng.maplibre import package_maplibre_terrain

    try:
        summary = package_maplibre_terrain(
            cog_path,
            viewer_dir,
            name=name,
            source_cog=source_cog,
            units=units,
            max_zoom=max_zoom,
            scratch_dir=scratch_dir,
            overwrite=overwrite,
        )
        console.print(
            "[green]OK[/green] Terrain PMTiles created: "
            f"{summary.pmtiles_path} (maximum native zoom {summary.max_zoom})"
        )
    except Exception as e:
        console.print(f"[red]ERROR:[/red] {e}")
        raise typer.Exit(1)


@app.command("maplibre-stored-map")
def maplibre_stored_map_command(
    cog_path: Path = typer.Argument(..., help="Numeric RASMapper Stored Map COG"),
    viewer_dir: Path = typer.Argument(..., help="Existing MapLibre viewer directory"),
    plan: str = typer.Option(..., "--plan", help="Source plan identifier, such as p03"),
    map_type: str = typer.Option(..., "--map-type", help="RASMapper map type, such as Depth or Velocity"),
    name: Optional[str] = typer.Option(None, "--name", help="Layer display name"),
    profile: Optional[str] = typer.Option(None, "--profile", help="Profile, summary, or time label"),
    geometry: Optional[str] = typer.Option(None, "--geometry", help="Associated geometry identifier"),
    layer_id: Optional[str] = typer.Option(None, "--layer-id", help="Stable manifest layer identifier"),
    source_cog: Optional[str] = typer.Option(
        None,
        "--source-cog",
        help="Public or manifest-relative numeric COG href used by Identify",
    ),
    units: str = typer.Option("ft", "--units", help="Result units shown in legends and Identify"),
    visible: bool = typer.Option(False, "--visible/--hidden", help="Initial layer visibility"),
    domain_policy: str = typer.Option(
        "fixed",
        "--domain-policy",
        help="Legend domain policy: fixed or current-view",
    ),
    max_zoom: Optional[int] = typer.Option(
        None,
        "--max-zoom",
        help="Maximum display zoom; never exceeds native raster resolution",
    ),
    scratch_dir: Optional[Path] = typer.Option(
        None,
        "--scratch-dir",
        help="Large local scratch directory for colorization and raster tiling",
    ),
    overwrite: bool = typer.Option(False, "--overwrite", help="Replace the layer and display derivative"),
):
    """Publish a queryable RASMapper Stored Map under its source plan."""

    from ras2cng.maplibre import package_maplibre_stored_map

    try:
        summary = package_maplibre_stored_map(
            cog_path,
            viewer_dir,
            plan=plan,
            map_type=map_type,
            name=name,
            profile=profile,
            geometry=geometry,
            layer_id=layer_id,
            source_cog=source_cog,
            units=units,
            visible=visible,
            domain_policy=domain_policy,
            max_zoom=max_zoom,
            scratch_dir=scratch_dir,
            overwrite=overwrite,
        )
        console.print(
            "[green]OK[/green] Stored Map PMTiles created: "
            f"{summary.pmtiles_path} ({summary.layer_id})"
        )
    except Exception as e:
        console.print(f"[red]ERROR:[/red] {e}")
        raise typer.Exit(1)


@app.command("maplibre-stored-vector")
def maplibre_stored_vector_command(
    vector_path: Path = typer.Argument(..., help="RASMapper Stored Map vector GeoParquet or GIS file"),
    viewer_dir: Path = typer.Argument(..., help="Existing MapLibre viewer directory"),
    plan: str = typer.Option(..., "--plan", help="Source plan identifier, such as p03"),
    map_type: str = typer.Option(..., "--map-type", help="RASMapper vector map type"),
    name: Optional[str] = typer.Option(None, "--name", help="Layer display name"),
    profile: Optional[str] = typer.Option(None, "--profile", help="Profile, summary, or time label"),
    geometry: Optional[str] = typer.Option(None, "--geometry", help="Associated geometry identifier"),
    layer_id: Optional[str] = typer.Option(None, "--layer-id", help="Stable manifest layer identifier"),
    crs: Optional[str] = typer.Option(None, "--crs", help="Validated source CRS fallback"),
    visible: bool = typer.Option(False, "--visible/--hidden", help="Initial layer visibility"),
    min_zoom: int = typer.Option(0, "--min-zoom", help="Tippecanoe minimum zoom"),
    max_zoom: int = typer.Option(17, "--max-zoom", help="Tippecanoe maximum zoom"),
    scratch_dir: Optional[Path] = typer.Option(None, "--scratch-dir", help="Large local scratch directory"),
    overwrite: bool = typer.Option(False, "--overwrite", help="Replace the layer and PMTiles derivative"),
):
    """Publish a queryable RASMapper vector Stored Map under its source plan."""

    from ras2cng.maplibre import package_maplibre_stored_vector

    try:
        summary = package_maplibre_stored_vector(
            vector_path,
            viewer_dir,
            plan=plan,
            map_type=map_type,
            name=name,
            profile=profile,
            geometry=geometry,
            layer_id=layer_id,
            crs=crs,
            visible=visible,
            min_zoom=min_zoom,
            max_zoom=max_zoom,
            scratch_dir=scratch_dir,
            overwrite=overwrite,
        )
        Console().print(
            f"[green]OK[/green] Stored Map vector PMTiles created: "
            f"{summary.pmtiles_path} ({summary.layer_id})"
        )
    except Exception as error:
        Console().print(f"[red]ERROR:[/red] {error}")
        raise typer.Exit(1)


@app.command("maplibre-import-stored-maps")
def maplibre_import_stored_maps_command(
    maps_dir: Path = typer.Argument(..., help="RasProcess output directory containing pNN folders"),
    archive_dir: Path = typer.Argument(..., help="Existing ras2cng archive directory"),
    viewer_dir: Path = typer.Argument(..., help="Existing MapLibre viewer directory"),
    scratch_dir: Optional[Path] = typer.Option(None, "--scratch-dir", help="Large local scratch directory"),
    domain_policy: str = typer.Option(
        "fixed",
        "--domain-policy",
        help="Initial raster legend policy; attach the service before using current-view",
    ),
    max_zoom: Optional[int] = typer.Option(
        16,
        "--max-zoom",
        help="Maximum zoom for precolored PMTiles; numeric COGs retain full fidelity",
    ),
    require_all: bool = typer.Option(
        True,
        "--require-all/--allow-partial",
        help="Require all supported raster families and Inundation Boundary for every completed plan",
    ),
    overwrite: bool = typer.Option(False, "--overwrite", help="Replace imported artifacts and viewer layers"),
):
    """Import and publish a complete distributed RasProcess Stored Map tranche."""

    from ras2cng.stored_maps import import_rasprocess_stored_maps

    try:
        summary = import_rasprocess_stored_maps(
            maps_dir,
            archive_dir,
            viewer_dir,
            scratch_dir=scratch_dir,
            domain_policy=domain_policy,
            max_zoom=max_zoom,
            require_all=require_all,
            overwrite=overwrite,
        )
        console.print(
            "[green]OK[/green] Imported Stored Maps: "
            f"{summary.plan_count} plan(s), {summary.raster_count} raster(s), "
            f"{summary.vector_count} vector(s)"
        )
    except Exception as error:
        console.print(f"[red]ERROR:[/red] {error}")
        raise typer.Exit(1)


@app.command("validate-publication")
def validate_publication_command(
    viewer_manifest: Path = typer.Argument(..., help="MapLibre viewer manifest.json"),
    archive_manifest: Path = typer.Argument(..., help="ras2cng archive manifest.json"),
    check_files: bool = typer.Option(
        True,
        "--check-files/--manifest-only",
        help="Open local manifest-relative artifacts and validate COG structure",
    ),
    check_http_ranges: bool = typer.Option(
        False,
        "--check-http-ranges",
        help="Require HTTP 206 byte-range responses for hosted artifacts",
    ),
    as_json: bool = typer.Option(False, "--json", help="Print the validation report as JSON"),
):
    """Enforce the Example Library catalog-admission contract."""

    from ras2cng.publication import validate_example_publication

    try:
        report = validate_example_publication(
            viewer_manifest,
            archive_manifest,
            check_files=check_files,
            check_http_ranges=check_http_ranges,
        )
    except Exception as error:
        console.print(f"[red]ERROR:[/red] {error}")
        raise typer.Exit(1)

    if as_json:
        console.print_json(json.dumps(report.to_dict()))
    else:
        status = "PASS" if report.ok else "FAIL"
        color = "green" if report.ok else "red"
        console.print(f"[{color}]{status}[/{color}] Example Library publication gate")
        for name, count in report.counts.items():
            console.print(f"  {name}: {count}")
        for issue in report.issues:
            label = issue.severity.upper()
            context = f" ({issue.context})" if issue.context else ""
            console.print(f"  {label} {issue.code}{context}: {issue.message}")
    if not report.ok:
        raise typer.Exit(1)


@app.command("maplibre-calculated-map")
def maplibre_calculated_map_command(
    cog_path: Path = typer.Argument(..., help="Numeric COG created by raster-calculate"),
    viewer_dir: Path = typer.Argument(..., help="Existing MapLibre viewer directory"),
    plan: str = typer.Option(..., "--plan", help="Source plan identifier, such as p03"),
    recipe: str = typer.Option(..., "--recipe", help="Allowlisted raster recipe identifier"),
    name: Optional[str] = typer.Option(None, "--name", help="Layer display name"),
    profile: Optional[str] = typer.Option(None, "--profile", help="Synchronized profile or timestep"),
    geometry: Optional[str] = typer.Option(None, "--geometry", help="Associated geometry identifier"),
    layer_id: Optional[str] = typer.Option(None, "--layer-id", help="Stable manifest layer identifier"),
    source_cog: Optional[str] = typer.Option(
        None,
        "--source-cog",
        help="Public or manifest-relative numeric COG href used by Identify",
    ),
    units: Optional[str] = typer.Option(None, "--units", help="Override provenance output units"),
    provenance: Optional[Path] = typer.Option(
        None,
        "--provenance",
        help="raster-calculate provenance JSON; defaults beside the COG",
    ),
    visible: bool = typer.Option(False, "--visible/--hidden", help="Initial layer visibility"),
    domain_policy: str = typer.Option(
        "fixed",
        "--domain-policy",
        help="Legend domain policy: fixed or current-view",
    ),
    max_zoom: Optional[int] = typer.Option(None, "--max-zoom", help="Maximum display zoom"),
    scratch_dir: Optional[Path] = typer.Option(None, "--scratch-dir", help="Local raster scratch directory"),
    overwrite: bool = typer.Option(False, "--overwrite", help="Replace the layer and display derivative"),
):
    """Publish a controlled calculated raster under its source plan."""

    from ras2cng.maplibre import package_maplibre_calculated_map

    try:
        summary = package_maplibre_calculated_map(
            cog_path,
            viewer_dir,
            plan=plan,
            recipe_id=recipe,
            name=name,
            profile=profile,
            geometry=geometry,
            layer_id=layer_id,
            source_cog=source_cog,
            units=units,
            provenance_path=provenance,
            visible=visible,
            domain_policy=domain_policy,
            max_zoom=max_zoom,
            scratch_dir=scratch_dir,
            overwrite=overwrite,
        )
        console.print(
            "[green]OK[/green] Calculated Map PMTiles created: "
            f"{summary.pmtiles_path} ({summary.layer_id})"
        )
    except Exception as error:
        console.print(f"[red]ERROR:[/red] {error}")
        raise typer.Exit(1)


@app.command("raster-service-catalog")
def raster_service_catalog_command(
    data_root: Path = typer.Argument(..., help="WebGIS artifact data root"),
    output: Path = typer.Argument(..., help="Output raster-assets.json allowlist"),
    manifests: list[Path] = typer.Option(
        [],
        "--manifest",
        help="Viewer manifest to catalog; repeat or omit to scan the data root",
    ),
    service_base_url: str = typer.Option(
        "/ras-raster",
        "--service-base-url",
        help="Public reverse-proxy base URL recorded in manifests",
    ),
    attach_manifests: bool = typer.Option(
        False,
        "--attach-manifests",
        help="Attach service asset IDs and revisions to each manifest",
    ),
    public_url_prefix: Optional[str] = typer.Option(
        None,
        "--public-url-prefix",
        help="Public URL prefix mapped to the local data root",
    ),
):
    """Build the allowlist used by the bounded numeric raster service."""

    from ras2cng.webgis_service import build_raster_asset_catalog

    try:
        result = build_raster_asset_catalog(
            data_root,
            output,
            manifest_paths=manifests or None,
            service_base_url=service_base_url,
            attach_manifests=attach_manifests,
            public_url_prefix=public_url_prefix,
        )
        console.print(f"[green]OK[/green] Raster service catalog: {result}")
    except Exception as error:
        console.print(f"[red]ERROR:[/red] {error}")
        raise typer.Exit(1)


@app.command("raster-service")
def raster_service_command(
    catalog: Path = typer.Argument(..., help="raster-assets.json allowlist"),
    data_root: Path = typer.Argument(..., help="WebGIS artifact data root"),
    host: str = typer.Option("127.0.0.1", "--host", help="Loopback listener address"),
    port: int = typer.Option(8000, "--port", min=1, max=65535, help="Loopback listener port"),
):
    """Run the isolated numeric raster service behind a reverse proxy."""

    import ipaddress

    import uvicorn

    from ras2cng.webgis_service import create_raster_app

    try:
        address = ipaddress.ip_address(host.strip("[]"))
        if not address.is_loopback:
            raise ValueError("raster-service must bind to a loopback address")
        service = create_raster_app(catalog, data_root)
        uvicorn.run(service, host=host, port=port, workers=1)
    except Exception as error:
        console.print(f"[red]ERROR:[/red] {error}")
        raise typer.Exit(1)


@app.command("raster-calculate")
def raster_calculate_command(
    recipe: str = typer.Argument(..., help="Allowlisted raster recipe identifier"),
    output: Path = typer.Argument(..., help="Output numeric Cloud Optimized GeoTIFF"),
    inputs: list[str] = typer.Option(
        ...,
        "--input",
        help="Recipe input as ROLE=PATH; repeat for every required role",
    ),
    input_units: list[str] = typer.Option(
        [],
        "--input-unit",
        help="Input units as ROLE=UNIT when not tagged in the source; repeatable",
    ),
    parameters: list[str] = typer.Option(
        [],
        "--parameter",
        help="Allowlisted recipe parameter as NAME=VALUE; repeatable",
    ),
    plan: Optional[str] = typer.Option(None, "--plan", help="Associated HEC-RAS plan"),
    profile: Optional[str] = typer.Option(
        None,
        "--profile",
        help="Synchronized profile/timestep; required for depth-velocity and hazard recipes",
    ),
    scratch_dir: Optional[Path] = typer.Option(
        None,
        "--scratch-dir",
        help="Scratch directory for the intermediate tiled GeoTIFF",
    ),
    block_size: int = typer.Option(
        512,
        "--block-size",
        min=64,
        max=4096,
        help="Bounded processing window size in pixels",
    ),
    hash_assets: bool = typer.Option(
        False,
        "--hash-assets",
        help="Record SHA-256 hashes; adds a full read of every large raster",
    ),
    overwrite: bool = typer.Option(False, "--overwrite", help="Atomically replace output"),
):
    """Run a controlled, unit-aware raster calculation over aligned COGs."""

    from ras2cng.raster_recipes import run_raster_recipe

    try:
        input_map = _key_value_paths(inputs, "--input")
        unit_map = _key_value_strings(input_units, "--input-unit")
        parameter_map = {
            key: _parse_parameter(value)
            for key, value in _key_value_strings(parameters, "--parameter").items()
        }
        result = run_raster_recipe(
            recipe,
            input_map,
            output,
            input_units=unit_map,
            parameters=parameter_map,
            plan=plan,
            profile=profile,
            scratch_dir=scratch_dir,
            block_size=block_size,
            hash_assets=hash_assets,
            overwrite=overwrite,
        )
        console.print(f"[green]OK[/green] Calculated COG: {result.output_path}")
        console.print(f"  Provenance: {result.provenance_path}")
    except Exception as error:
        console.print(f"[red]ERROR:[/red] {error}")
        raise typer.Exit(1)


def _key_value_strings(items: list[str], option: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for item in items:
        key, separator, value = item.partition("=")
        if not separator or not key.strip() or not value.strip():
            raise ValueError(f"{option} must use NAME=VALUE syntax: {item!r}")
        key = key.strip()
        if key in values:
            raise ValueError(f"{option} was provided more than once for {key!r}")
        values[key] = value.strip()
    return values


def _key_value_paths(items: list[str], option: str) -> dict[str, Path]:
    return {key: Path(value) for key, value in _key_value_strings(items, option).items()}


def _parse_parameter(value: str):
    lowered = value.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    try:
        return int(value)
    except ValueError:
        try:
            return float(value)
        except ValueError:
            return value


@app.command("sync")
def sync_to_postgis(
    input_file: Path = typer.Argument(..., help="Input GeoParquet file"),
    postgres_uri: str = typer.Argument(..., help="PostgreSQL connection URI"),
    table_name: str = typer.Argument(..., help="Target table name"),
    schema: str = typer.Option("public", "--schema", "-s", help="Target schema"),
    if_exists: str = typer.Option(
        "replace",
        "--if-exists",
        help="replace|append|fail",
    ),
):
    """Sync GeoParquet data to PostGIS."""

    from ras2cng.postgis_sync import sync_to_postgres

    console.print(f"[bold blue]Syncing to PostGIS:[/bold blue] {schema}.{table_name}")
    try:
        sync_to_postgres(input_file, postgres_uri, table_name, schema=schema, if_exists=if_exists)
        console.print(f"[green]OK[/green] Synced to {schema}.{table_name}")
    except Exception as e:
        console.print(f"[red]ERROR:[/red] {e}")
        raise typer.Exit(1)


@app.command("terrain")
def terrain_command(
    project: Path = typer.Argument(
        ..., help="HEC-RAS project directory or .prj file"
    ),
    output: Path = typer.Argument(
        ..., help="Output directory for consolidated terrain files"
    ),
    name: str = typer.Option(
        "Consolidated", "--name", help="Terrain name (default: Consolidated)"
    ),
    downsample: Optional[float] = typer.Option(
        None, "--downsample", help="Downsample factor (2.0 = half resolution)"
    ),
    resolution: Optional[float] = typer.Option(
        None, "--resolution", help="Target cell size in project units"
    ),
    terrains: Optional[str] = typer.Option(
        None, "--terrains", help="Comma-separated terrain names to include"
    ),
    units: str = typer.Option(
        "Feet", "--units", help="Vertical units: Feet or Meters (default: Feet)"
    ),
    ras_version: str = typer.Option(
        "6.6", "--ras-version", help="HEC-RAS version (default: 6.6)"
    ),
    tiff_only: bool = typer.Option(
        False, "--tiff-only", help="Only produce merged TIFF, skip HDF creation"
    ),
    no_register: bool = typer.Option(
        False, "--no-register", help="Don't register new terrain in rasmap"
    ),
):
    """Consolidate project terrains into a single merged TIFF and HEC-RAS terrain HDF.

    Discovers all terrain layers from the project rasmap, merges their TIFFs
    (first-wins priority in overlaps), and optionally creates a new HEC-RAS
    terrain HDF via RasProcess.exe.
    """

    from ras2cng.terrain import consolidate_terrain

    terrain_list = [t.strip() for t in terrains.split(",")] if terrains else None

    try:
        result = consolidate_terrain(
            project,
            output,
            terrain_name=name,
            downsample_factor=downsample,
            target_resolution=resolution,
            terrain_names=terrain_list,
            units=units,
            ras_version=ras_version,
            create_hdf=not tiff_only,
            register_rasmap=not no_register,
        )
        console.print(f"[green]OK[/green] Terrain output: {result}")
    except Exception as e:
        Console().print(f"[red]ERROR:[/red] {e}")
        raise typer.Exit(1)


@app.command("boundary-from-depth")
def boundary_from_depth_command(
    depth_cog: Path = typer.Argument(
        ..., help="Complete RASMapper/RasProcess Depth COG"
    ),
    output_shp: Path = typer.Argument(
        ..., help="Output inundation-boundary shapefile"
    ),
    threshold: float = typer.Option(
        0.0, "--threshold", help="Strict depth threshold (depth > threshold)"
    ),
    resolution: Optional[float] = typer.Option(
        None, "--resolution", help="Optional coarser output cell size"
    ),
    max_edges: int = typer.Option(
        5_000_000,
        "--max-edges",
        help="Maximum wet/dry edges allowed before polygonization",
    ),
    profile: Optional[str] = typer.Option(
        None, "--profile", help="Profile recorded in provenance"
    ),
    units: Optional[str] = typer.Option(
        None, "--units", help="Depth units recorded in provenance"
    ),
    source_id: Optional[str] = typer.Option(
        None,
        "--source-id",
        help="Portable relative source identifier (default: raster basename)",
    ),
):
    """Derive a bounded 4-connected inundation polygon from a Depth COG."""

    from ras2cng.boundary import derive_inundation_boundary

    try:
        result = derive_inundation_boundary(
            depth_cog,
            output_shp,
            threshold=threshold,
            resolution=resolution,
            max_edges=max_edges,
            profile=profile,
            units=units,
            source_identifier=source_id,
        )
        console.print(f"[green]OK[/green] Boundary: {result.output_path}")
        console.print(f"  Provenance: {result.provenance_path}")
    except Exception as e:
        Console().print(f"[red]ERROR:[/red] {e}")
        raise typer.Exit(1)


@app.command("map")
def map_command(
    project: Path = typer.Argument(
        ..., help="HEC-RAS project directory or .prj file"
    ),
    output: Path = typer.Argument(
        ..., help="Output directory for result rasters"
    ),
    plans: Optional[str] = typer.Option(
        None, "--plans", help="Comma-separated plan IDs (default: all with results)"
    ),
    profile: str = typer.Option(
        "Max", "--profile", help="Max, Min, or timestamp (default: Max)"
    ),
    wse: bool = typer.Option(True, "--wse/--no-wse", help="Water Surface Elevation (default: on)"),
    depth: bool = typer.Option(True, "--depth/--no-depth", help="Depth (default: on)"),
    velocity: bool = typer.Option(True, "--velocity/--no-velocity", help="Velocity (default: on)"),
    froude: bool = typer.Option(False, "--froude", help="Froude number"),
    shear_stress: bool = typer.Option(False, "--shear-stress", help="Shear stress"),
    dv: bool = typer.Option(False, "--dv", help="Depth x Velocity"),
    dv_sq: bool = typer.Option(False, "--dv-sq", help="Depth x Velocity²"),
    inundation_boundary: bool = typer.Option(False, "--inundation-boundary", help="Inundation boundary polygon"),
    boundary_method: BoundaryMethod = typer.Option(
        BoundaryMethod.rasmapper,
        "--boundary-method",
        help="Boundary authority: rasmapper or depth-raster",
    ),
    boundary_threshold: float = typer.Option(
        0.0,
        "--boundary-threshold",
        help="Strict threshold for a depth-raster boundary",
    ),
    boundary_resolution: Optional[float] = typer.Option(
        None,
        "--boundary-resolution",
        help="Optional coarser depth-raster boundary cell size",
    ),
    boundary_max_edges: int = typer.Option(
        5_000_000,
        "--boundary-max-edges",
        help="Maximum edges before depth-raster polygonization",
    ),
    arrival_time: bool = typer.Option(False, "--arrival-time", help="Arrival time (hours, whole-simulation)"),
    duration: bool = typer.Option(False, "--duration", help="Inundation duration (hours)"),
    recession: bool = typer.Option(False, "--recession", help="Not supported (no RasMapperLib map type); ignored with a warning"),
    percent_inundated: bool = typer.Option(False, "--percent-inundated", help="Percent time inundated"),
    arrival_depth: float = typer.Option(
        0.0, "--arrival-depth",
        help="Wet/dry depth threshold for arrival/duration/recession/percent-inundated",
    ),
    terrain_name: Optional[str] = typer.Option(
        None, "--terrain", help="Specific terrain name from rasmap"
    ),
    render_mode: Optional[str] = typer.Option(
        None, "--render-mode", help="Water surface render mode: horizontal, sloping, slopingPretty"
    ),
    ras_version: Optional[str] = typer.Option(
        None, "--ras-version", help="HEC-RAS version (e.g. 6.6)"
    ),
    rasprocess: Optional[Path] = typer.Option(
        None, "--rasprocess", help="Path to HEC-RAS install directory (for helper deployment)"
    ),
    min_depth: float = typer.Option(
        0.0, "--min-depth", help="Min depth threshold (default: 0.0)"
    ),
    wgs84: bool = typer.Option(False, "--wgs84", help="Reproject output to WGS84"),
    cog: bool = typer.Option(False, "--cog", help="Convert output to Cloud Optimized GeoTIFF"),
    timeout: int = typer.Option(
        10800, "--timeout", help="Per-plan timeout in seconds (default: 10800 = 3 hours)"
    ),
    skip_errors: bool = typer.Option(
        True, "--skip-errors/--fail-fast", help="Skip errors vs abort"
    ),
    keep_postprocessing: bool = typer.Option(
        False, "--keep-postprocessing",
        help="Keep the (large) PostProcessing.hdf cache in the output directory",
    ),
):
    """Generate result rasters (WSE, Depth, Velocity, etc.) via RasStoreMapHelper.

    Renders completed plan results to GeoTIFF rasters using the HEC-RAS
    mapping engine via RasStoreMapHelper.exe (bundled with ras-commander).
    """

    from ras2cng.mapping import generate_result_maps

    plans_list = [p.strip() for p in plans.split(",")] if plans else None

    if (
        inundation_boundary
        and boundary_method is BoundaryMethod.depth_raster
        and not depth
    ):
        Console().print(
            "[red]ERROR:[/red] Depth must be enabled with "
            "--boundary-method depth-raster"
        )
        raise typer.Exit(1)

    try:
        generate_result_maps(
            project,
            output,
            plans=plans_list,
            profile=profile,
            wse=wse,
            depth=depth,
            velocity=velocity,
            froude=froude,
            shear_stress=shear_stress,
            depth_x_velocity=dv,
            depth_x_velocity_sq=dv_sq,
            inundation_boundary=inundation_boundary,
            boundary_method=boundary_method.value,
            boundary_threshold=boundary_threshold,
            boundary_resolution=boundary_resolution,
            boundary_max_edges=boundary_max_edges,
            arrival_time=arrival_time,
            duration=duration,
            recession=recession,
            percent_inundated=percent_inundated,
            arrival_depth=arrival_depth,
            terrain_name=terrain_name,
            ras_version=ras_version,
            rasprocess_path=rasprocess,
            render_mode=render_mode,
            min_depth=min_depth,
            reproject_wgs84=wgs84,
            convert_cog=cog,
            timeout=timeout,
            skip_errors=skip_errors,
            keep_postprocessing=keep_postprocessing,
        )
    except Exception as e:
        Console().print(f"[red]ERROR:[/red] {e}")
        raise typer.Exit(1)


@app.command("map-hdf")
def map_hdf_command(
    plan_hdf: Path = typer.Argument(
        ..., help="Computed plan results HDF (*.pNN.hdf, any filename)"
    ),
    output: Path = typer.Argument(
        ..., help="Output directory for result rasters"
    ),
    terrain: Optional[list[Path]] = typer.Option(
        None, "--terrain", help="Raw terrain GeoTIFF (repeatable; tiles are stitched)"
    ),
    terrain_hdf: Optional[Path] = typer.Option(
        None, "--terrain-hdf",
        help="Pre-built HEC-RAS terrain HDF (its .vrt and tile TIFFs must sit beside it)",
    ),
    projection: Optional[Path] = typer.Option(
        None, "--projection",
        help="ESRI .prj projection file (default: read WKT from the plan HDF)",
    ),
    workdir: Optional[Path] = typer.Option(
        None, "--workdir",
        help="Scaffold directory (default: OUTPUT/_scaffold; reused across reruns)",
    ),
    rm_scaffold: bool = typer.Option(
        False, "--rm-scaffold", help="Delete the scaffold directory after the run"
    ),
    profile: str = typer.Option(
        "Max", "--profile", help="Max, Min, or timestamp (default: Max)"
    ),
    wse: bool = typer.Option(True, "--wse/--no-wse", help="Water Surface Elevation (default: on)"),
    depth: bool = typer.Option(True, "--depth/--no-depth", help="Depth (default: on)"),
    velocity: bool = typer.Option(True, "--velocity/--no-velocity", help="Velocity (default: on)"),
    froude: bool = typer.Option(False, "--froude", help="Froude number"),
    shear_stress: bool = typer.Option(False, "--shear-stress", help="Shear stress"),
    dv: bool = typer.Option(False, "--dv", help="Depth x Velocity"),
    dv_sq: bool = typer.Option(False, "--dv-sq", help="Depth x Velocity²"),
    inundation_boundary: bool = typer.Option(False, "--inundation-boundary", help="Inundation boundary polygon"),
    boundary_method: BoundaryMethod = typer.Option(
        BoundaryMethod.rasmapper,
        "--boundary-method",
        help="Boundary authority: rasmapper or depth-raster",
    ),
    boundary_threshold: float = typer.Option(
        0.0,
        "--boundary-threshold",
        help="Strict threshold for a depth-raster boundary",
    ),
    boundary_resolution: Optional[float] = typer.Option(
        None,
        "--boundary-resolution",
        help="Optional coarser depth-raster boundary cell size",
    ),
    boundary_max_edges: int = typer.Option(
        5_000_000,
        "--boundary-max-edges",
        help="Maximum edges before depth-raster polygonization",
    ),
    arrival_time: bool = typer.Option(False, "--arrival-time", help="Arrival time (hours, whole-simulation)"),
    duration: bool = typer.Option(False, "--duration", help="Inundation duration (hours)"),
    recession: bool = typer.Option(False, "--recession", help="Not supported (no RasMapperLib map type); ignored with a warning"),
    percent_inundated: bool = typer.Option(False, "--percent-inundated", help="Percent time inundated"),
    arrival_depth: float = typer.Option(
        0.0, "--arrival-depth",
        help="Wet/dry depth threshold for arrival/duration/recession/percent-inundated",
    ),
    render_mode: Optional[str] = typer.Option(
        "sloping", "--render-mode", help="Water surface render mode: horizontal, sloping, slopingPretty"
    ),
    ras_version: str = typer.Option(
        "6.6", "--ras-version", help="HEC-RAS version (default: 6.6)"
    ),
    rasprocess: Optional[Path] = typer.Option(
        None, "--rasprocess", help="Path to HEC-RAS install directory (for helper deployment)"
    ),
    min_depth: float = typer.Option(
        0.0, "--min-depth", help="Min depth threshold (default: 0.0)"
    ),
    wgs84: bool = typer.Option(False, "--wgs84", help="Reproject output to WGS84"),
    cog: bool = typer.Option(False, "--cog", help="Convert output to Cloud Optimized GeoTIFF"),
    timeout: int = typer.Option(
        10800, "--timeout", help="Timeout in seconds (default: 10800 = 3 hours)"
    ),
    keep_postprocessing: bool = typer.Option(
        False, "--keep-postprocessing",
        help="Keep the (large) PostProcessing.hdf cache in the output directory",
    ),
):
    """Generate result rasters from just a plan HDF + terrain (no project needed).

    Synthesizes a barebones HEC-RAS project around the plan HDF (projection,
    units, and plan metadata are read from the HDF itself), builds the HEC-RAS
    terrain from raw GeoTIFF(s) via RasProcess.exe CreateTerrain (or reuses a
    pre-built terrain HDF), then renders stored maps through RASMapper.

    Examples:

        ras2cng map-hdf results.p01.hdf ./maps --terrain dem.tif

        ras2cng map-hdf results.p01.hdf ./maps --terrain-hdf Terrain50.hdf
    """
    import shutil

    from ras2cng.mapping import generate_result_maps
    from ras2cng.scaffold import SCAFFOLD_MARKER, build_scaffold

    console = Console()

    if bool(terrain) == bool(terrain_hdf):
        console.print("[red]ERROR:[/red] Provide exactly one of --terrain or --terrain-hdf")
        raise typer.Exit(1)

    if (
        inundation_boundary
        and boundary_method is BoundaryMethod.depth_raster
        and not depth
    ):
        console.print(
            "[red]ERROR:[/red] Depth must be enabled with "
            "--boundary-method depth-raster"
        )
        raise typer.Exit(1)

    if rasprocess and terrain:
        console.print(
            "[yellow]Warning:[/yellow] the terrain build locates HEC-RAS by "
            "--ras-version, not --rasprocess; a portable install may not be "
            "found for the CreateTerrain step"
        )

    scaffold_dir = workdir if workdir is not None else output / "_scaffold"

    try:
        info = build_scaffold(
            plan_hdf,
            scaffold_dir,
            terrain_tifs=list(terrain) if terrain else None,
            terrain_hdf=terrain_hdf,
            projection_file=projection,
            render_mode=render_mode or "sloping",
            ras_version=ras_version,
        )
        console.print(
            f"  Scaffold: {info.project_dir} "
            f"({info.meta.project_name} p{info.meta.plan_number})"
        )

        generate_result_maps(
            info.prj_file,
            output,
            plans=[f"p{info.meta.plan_number}"],
            profile=profile,
            wse=wse,
            depth=depth,
            velocity=velocity,
            froude=froude,
            shear_stress=shear_stress,
            depth_x_velocity=dv,
            depth_x_velocity_sq=dv_sq,
            inundation_boundary=inundation_boundary,
            boundary_method=boundary_method.value,
            boundary_threshold=boundary_threshold,
            boundary_resolution=boundary_resolution,
            boundary_max_edges=boundary_max_edges,
            arrival_time=arrival_time,
            duration=duration,
            recession=recession,
            percent_inundated=percent_inundated,
            arrival_depth=arrival_depth,
            ras_version=ras_version,
            rasprocess_path=rasprocess,
            render_mode=render_mode,
            min_depth=min_depth,
            reproject_wgs84=wgs84,
            convert_cog=cog,
            timeout=timeout,
            skip_errors=False,
            keep_postprocessing=keep_postprocessing,
        )
    except Exception as e:
        Console().print(f"[red]ERROR:[/red] {e}")
        raise typer.Exit(1)
    finally:
        # Only ever delete a directory this tool owns — the marker proves it
        # is a ras2cng scaffold, not a user directory passed via --workdir.
        if rm_scaffold and (scaffold_dir / SCAFFOLD_MARKER).exists():
            shutil.rmtree(scaffold_dir, ignore_errors=True)


@app.command("terrain-mod")
def terrain_mod_command(
    project: Path = typer.Argument(
        ..., help="HEC-RAS project directory or .prj file"
    ),
    output: Path = typer.Argument(
        ..., help="Output GeoTIFF path"
    ),
    geometry: Optional[str] = typer.Option(
        None, "--geometry", "-g", help="Geometry number (e.g. g01). Default: first"
    ),
    terrain_name: Optional[str] = typer.Option(
        None, "--terrain", help="Specific terrain name from rasmap"
    ),
):
    """Export terrain with modifications (channels, levees, etc.) as GeoTIFF.

    Samples the modified terrain surface at full raster resolution via
    RasMapperLib. Requires HEC-RAS 6.6+ and pythonnet (Windows only).
    """

    from ras2cng.terrain import export_modified_terrain

    try:
        export_modified_terrain(
            project,
            output,
            geometry=geometry,
            terrain_name=terrain_name,
        )
    except Exception as e:
        Console().print(f"[red]ERROR:[/red] {e}")
        raise typer.Exit(1)


@app.command("mannings")
def mannings_command(
    project: Path = typer.Argument(
        ..., help="HEC-RAS project directory or .prj file"
    ),
    output: Path = typer.Argument(
        ..., help="Output GeoTIFF path"
    ),
    geometry: Optional[str] = typer.Option(
        None, "--geometry", "-g", help="Geometry number (e.g. g01). Default: first"
    ),
):
    """Export final Manning's n raster (base landcover + calibration overrides).

    Produces a full-resolution GeoTIFF of Manning's n values matching the
    land cover raster grid, with all calibration region overrides applied.
    """

    from ras2cng.terrain import export_mannings_raster

    try:
        export_mannings_raster(
            project,
            output,
            geometry=geometry,
        )
    except Exception as e:
        Console().print(f"[red]ERROR:[/red] {e}")
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
