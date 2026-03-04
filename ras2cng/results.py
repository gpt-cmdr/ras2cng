"""Results export functions for rascmdr-parquet."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import geopandas as gpd
import h5py

from ras_commander.hdf import HdfResultsMesh


def export_results_layer(
    plan_hdf: Path,
    output: Path,
    variable: str = "Maximum Depth",
    geom_file: Optional[Path] = None,
):
    """Export a single HEC-RAS mesh summary output variable to GeoParquet.

    Notes:
        - ras-commander normalizes the output column names to snake_case.
          Example: "Maximum Depth" -> "maximum_depth".
        - Geometry returned by ras-commander is typically cell/face points. If you pass a
          mesh-cell polygon GeoParquet in `geom_file`, this function joins values onto polygons.
    """

    plan_path = Path(plan_hdf)

    results_gdf = HdfResultsMesh.get_mesh_summary_output(plan_path, variable)
    if len(results_gdf) == 0:
        raise ValueError(f"No results found for variable: {variable}")

    # If results are points (or empty geometry), optionally join to polygons.
    geom_series = results_gdf.geometry if "geometry" in results_gdf.columns else None
    geom_types = []
    if geom_series is not None:
        geom_types = list(geom_series.dropna().geom_type.unique())

    is_pointy = (not geom_types) or (set(geom_types) == {"Point"})

    if is_pointy and geom_file:
        geom_gdf = gpd.read_parquet(geom_file)

        results_df = results_gdf.drop(columns=["geometry"], errors="ignore")
        merged = geom_gdf.merge(results_df, on=["mesh_name", "cell_id"], how="left")
        results_gdf = gpd.GeoDataFrame(merged, geometry="geometry", crs=geom_gdf.crs)

    results_gdf.to_parquet(output, compression="snappy", index=False)


def list_available_summary_variables(plan_hdf: Path) -> list[str]:
    """List available 2D mesh summary output variables in a plan HDF."""

    plan_path = Path(plan_hdf)

    with h5py.File(plan_path, "r") as hdf:
        attrs = hdf.get("Geometry/2D Flow Areas/Attributes")
        if attrs is None or len(attrs) == 0:
            return []

        # Use the first mesh to discover variable names.
        first_mesh = attrs[0]
        mesh_name = first_mesh[0]
        if isinstance(mesh_name, bytes):
            mesh_name = mesh_name.decode("utf-8").strip()

        base = f"Results/Unsteady/Output/Output Blocks/Base Output/Summary Output/2D Flow Areas/{mesh_name}"
        grp = hdf.get(base)
        if grp is None:
            return []

        return sorted(list(grp.keys()))


def export_all_variables(plan_hdf: Path, output_dir: Path, geom_file: Optional[Path] = None):
    """Export all available 2D mesh summary variables to separate GeoParquet files."""

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    variables = list_available_summary_variables(plan_hdf)
    if not variables:
        raise ValueError("No summary output variables found in HDF file")

    exported = []
    for var in variables:
        try:
            out = output_dir / f"{var.lower().replace(' ', '_')}.parquet"
            export_results_layer(plan_hdf, out, variable=var, geom_file=geom_file)
            exported.append(var)
        except Exception as e:
            print(f"✗ Failed to export {var}: {e}")

    return exported
