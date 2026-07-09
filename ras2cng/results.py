"""Results export functions for ras2cng."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional, Sequence

import geopandas as gpd
import h5py
import pandas as pd

from ras_commander.hdf import HdfBase, HdfResultsMesh, HdfUtils


VALID_RESULTS_GEOMETRY_MODES = {"polygon", "point", "none"}


def result_variable_slug(variable: str) -> str:
    """Return the stable layer/file slug for a HEC-RAS summary variable."""
    return variable.lower().replace(" ", "_")


def result_variable_index_column(variable: str) -> str:
    """Return the mesh key column used by a HEC-RAS summary variable."""
    tokens = set(re.split(r"[^a-z0-9]+", str(variable).lower()))
    return "face_id" if "face" in tokens else "cell_id"


def selected_summary_variables(plan_hdf: Path, variables: Optional[Sequence[str]] = None) -> list[str]:
    """List available variables, optionally restricted by exact name or slug.

    Requested variables are matched case-insensitively against the raw HEC-RAS
    name and the ras2cng slug. Missing requested variables are ignored so one
    archive profile can be reused across mixed projects.
    """
    available = list_available_summary_variables(plan_hdf)
    if not variables:
        return available

    by_name = {value.lower(): value for value in available}
    by_slug = {result_variable_slug(value): value for value in available}
    selected: list[str] = []
    seen: set[str] = set()
    for requested in variables:
        key = str(requested).strip()
        if not key:
            continue
        match = by_name.get(key.lower()) or by_slug.get(result_variable_slug(key))
        if match and match not in seen:
            selected.append(match)
            seen.add(match)
    return selected


def _geometry_types(gdf: gpd.GeoDataFrame) -> set[str]:
    if "geometry" not in gdf.columns:
        return set()
    return set(gdf.geometry.dropna().geom_type.unique())


def _is_pointy(gdf: gpd.GeoDataFrame) -> bool:
    geom_types = _geometry_types(gdf)
    return (not geom_types) or geom_types <= {"Point"}


def _apply_results_geometry_mode(
    results_gdf: gpd.GeoDataFrame,
    *,
    mesh_cells_gdf: Optional[gpd.GeoDataFrame],
    geometry_mode: str,
) -> pd.DataFrame | gpd.GeoDataFrame:
    """Return result rows with polygon, point, or no geometry."""
    if geometry_mode not in VALID_RESULTS_GEOMETRY_MODES:
        raise ValueError(
            f"Unsupported results geometry mode: {geometry_mode}. "
            f"Expected one of {sorted(VALID_RESULTS_GEOMETRY_MODES)}."
        )

    if geometry_mode == "none":
        return pd.DataFrame(results_gdf.drop(columns=["geometry"], errors="ignore"))

    if geometry_mode == "point" or mesh_cells_gdf is None or not _is_pointy(results_gdf):
        return results_gdf

    results_df = results_gdf.drop(columns=["geometry"], errors="ignore")
    merged = mesh_cells_gdf.merge(results_df, on=["mesh_name", "cell_id"], how="left")
    return gpd.GeoDataFrame(merged, geometry="geometry", crs=mesh_cells_gdf.crs)


def extract_results_variable_no_geometry(
    plan_hdf: Path,
    variable: str,
    *,
    round_to: str = "100ms",
) -> pd.DataFrame:
    """Extract one mesh summary variable without constructing geometry.

    This avoids the HdfResultsMesh.get_mesh_summary_output() GeoDataFrame path,
    which builds mesh cell/face geometry internally. It is the preferred path
    for large archives that store results as DRY attribute tables.
    """
    rows: list[pd.DataFrame] = []
    plan_path = Path(plan_hdf)
    variable_slug = result_variable_slug(variable)

    with h5py.File(plan_path, "r") as hdf:
        start_time = HdfBase.get_simulation_start_time(hdf)
        d2_flow_areas = hdf.get("Geometry/2D Flow Areas/Attributes")
        if d2_flow_areas is None:
            return pd.DataFrame()

        for d2_flow_area in d2_flow_areas[:]:
            mesh_name = HdfUtils.convert_ras_string(d2_flow_area[0])
            try:
                group = HdfResultsMesh.get_mesh_summary_output_group(hdf, mesh_name, variable)
            except ValueError:
                continue

            data = group[:]
            id_column = result_variable_index_column(variable)

            if data.ndim == 2 and data.shape[0] == 2:
                frame = pd.DataFrame(
                    {
                        "mesh_name": [mesh_name] * data.shape[1],
                        id_column: range(data.shape[1]),
                        variable_slug: data[0, :],
                        f"{variable_slug}_time": HdfUtils.convert_timesteps_to_datetimes(
                            data[1, :],
                            start_time,
                            time_unit="days",
                            round_to=round_to,
                        ),
                    }
                )
            elif data.ndim == 1:
                frame = pd.DataFrame(
                    {
                        "mesh_name": [mesh_name] * len(data),
                        id_column: range(len(data)),
                        variable_slug: data,
                    }
                )
            else:
                raise ValueError(
                    f"Unexpected data shape for {variable} in {mesh_name}. Got shape {data.shape}"
                )
            rows.append(frame)

    if not rows:
        return pd.DataFrame()
    return pd.concat(rows, ignore_index=True)


def extract_results_variable(
    plan_hdf: Path,
    variable: str,
    *,
    mesh_cells_gdf: Optional[gpd.GeoDataFrame] = None,
    geometry_mode: str = "polygon",
) -> pd.DataFrame | gpd.GeoDataFrame:
    """Extract one summary variable without retaining other variables in memory."""
    if geometry_mode == "none":
        return extract_results_variable_no_geometry(plan_hdf, variable)

    results_gdf = HdfResultsMesh.get_mesh_summary_output(Path(plan_hdf), variable)
    if len(results_gdf) == 0:
        return results_gdf
    return _apply_results_geometry_mode(
        results_gdf,
        mesh_cells_gdf=mesh_cells_gdf,
        geometry_mode=geometry_mode,
    )


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
    if _is_pointy(results_gdf) and geom_file:
        geom_gdf = gpd.read_parquet(geom_file)
        results_gdf = _apply_results_geometry_mode(
            results_gdf,
            mesh_cells_gdf=geom_gdf,
            geometry_mode="polygon",
        )

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


# ---------------------------------------------------------------------------
# Consolidated merge function (v2 archive format)
# ---------------------------------------------------------------------------

def merge_all_variables(
    plan_hdf: Path,
    mesh_cells_gdf: Optional[gpd.GeoDataFrame] = None,
    variables: Optional[Sequence[str]] = None,
    geometry_mode: str = "polygon",
) -> Optional[gpd.GeoDataFrame]:
    """Extract and merge all summary variables into a single GeoDataFrame.

    Each variable becomes rows distinguished by a ``layer`` column with the
    snake_case variable name. When results are points and ``mesh_cells_gdf``
    is provided, values are joined onto the polygon geometry.

    Args:
        plan_hdf: Path to ``*.p??.hdf`` plan results file
        mesh_cells_gdf: Optional mesh cell polygons for spatial join

    Returns:
        A merged GeoDataFrame with ``layer`` column, or None if nothing extracted
    """
    plan_path = Path(plan_hdf)
    selected_variables = selected_summary_variables(plan_path, variables)
    if not selected_variables:
        return None

    all_gdfs: list[gpd.GeoDataFrame] = []

    for var in selected_variables:
        try:
            results_gdf = extract_results_variable(
                plan_path,
                var,
                mesh_cells_gdf=mesh_cells_gdf,
                geometry_mode=geometry_mode,
            )
            if len(results_gdf) == 0:
                continue
        except Exception as e:
            print(f"Warning: Could not extract '{var}': {e}")
            continue

        var_snake = result_variable_slug(var)
        results_gdf["layer"] = var_snake
        all_gdfs.append(results_gdf)

    if not all_gdfs:
        return None

    merged = pd.concat(all_gdfs, ignore_index=True)
    if geometry_mode == "none" or "geometry" not in merged.columns:
        return gpd.GeoDataFrame(merged)
    return gpd.GeoDataFrame(merged, geometry="geometry")
