"""ras2cng: Full-project archival and cloud-native export for HEC-RAS."""

from __future__ import annotations

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
    skip_errors: bool = typer.Option(
        True, "--skip-errors/--fail-fast", help="Skip individual layer errors vs abort"
    ),
    no_sort: bool = typer.Option(
        False, "--no-sort", help="Disable Hilbert spatial sorting (on by default)"
    ),
):
    """Archive a HEC-RAS project to consolidated GeoParquet files.

    Produces one parquet per geometry file and one per plan, plus a project
    metadata parquet. All layers within each file are distinguished by a
    ``layer`` column — query with ``WHERE layer = 'mesh_cells'``.

    Geometry is exported by default. Results and terrain are opt-in.
    """

    from ras2cng.project import archive_project

    plans_list = [p.strip() for p in plans.split(",")] if plans else None

    try:
        archive_project(
            project,
            output,
            include_results=results,
            include_terrain=terrain,
            include_plan_geometry=plan_geometry,
            plans=plans_list,
            skip_errors=skip_errors,
            sort=not no_sort,
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
            "Geometry layer: mesh_cells, mesh_areas, cross_sections, centerlines, "
            "bc_lines, breaklines, refinement_regions, reference_lines, "
            "reference_points, structures, storage_areas"
        ),
    ),
):
    """Export HEC-RAS geometry to GeoParquet."""

    from ras2cng.geometry import export_geometry_layers

    console.print(f"[bold blue]Exporting geometry:[/bold blue] {geom_file}")
    try:
        export_geometry_layers(geom_file, output, layer=layer)
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


if __name__ == "__main__":
    app()
