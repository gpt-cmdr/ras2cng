import marimo

__generated_with = "0.9.0"
app = marimo.App(width="medium")


@app.cell
def __():
    """
    02 — Export Results
    ===================
    Exports 2D mesh summary results from BaldEagleCrkMulti2D plan HDF files
    and joins them to polygon geometry from notebook 01.

    NOTE: BaldEagleCrkMulti2D does not include pre-run plan results.
    Run the HEC-RAS model first to generate .p??.hdf files,
    then re-run this notebook.
    Output goes to out/02_export_results/
    """
    import marimo as mo
    mo.md("## 02 — Export Results")


@app.cell
def __():
    from pathlib import Path
    from ras_commander import RasExamples

    project_path = RasExamples.extract_project("BaldEagleCrkMulti2D")
    print(f"Project: {project_path}")

    # Check for plan HDF files
    plan_hdfs = sorted(project_path.glob("*.p??.hdf"))
    print(f"\nPlan HDF files found: {len(plan_hdfs)}")
    if not plan_hdfs:
        print("\n⚠  No plan HDF files found.")
        print("   BaldEagleCrkMulti2D does not include pre-run simulation results.")
        print("   To use this notebook:")
        print("   1. Open BaldEagleCrkMulti2D in HEC-RAS")
        print("   2. Run one or more plans")
        print("   3. Re-run this notebook")
        print("\n   The cells below will show what the output looks like when results are available.")
    else:
        for f in plan_hdfs:
            print(f"  {f.name}  ({f.stat().st_size / 1e6:.1f} MB)")

    return Path, RasExamples, plan_hdfs, project_path


@app.cell
def __(Path, plan_hdfs):
    from ras2cng.results import list_available_summary_variables

    if not plan_hdfs:
        print("No plan HDF files — skipping variable listing")
        variables = []
        plan_hdf = None
    else:
        plan_hdf = plan_hdfs[0]
        variables = list_available_summary_variables(plan_hdf)
        print(f"Available summary variables in {plan_hdf.name}:")
        for v in variables:
            print(f"  {v}")

    return list_available_summary_variables, plan_hdf, variables


@app.cell
def __(Path, plan_hdf, variables):
    from ras2cng.results import export_results_layer
    import geopandas as gpd

    out_dir = Path("out/02_export_results")
    out_dir.mkdir(parents=True, exist_ok=True)

    if plan_hdf is None or not variables:
        print("No results to export — skipping")
        gdf_points = None
    else:
        # Export Maximum Depth as points (no geometry join)
        var = "Maximum Depth" if "Maximum Depth" in variables else variables[0]
        points_out = out_dir / "max_depth_points.parquet"
        export_results_layer(plan_hdf, points_out, variable=var)

        gdf_points = gpd.read_parquet(points_out)
        print(f"Results (points): {len(gdf_points)} features")
        print(f"  Variable: {var}")
        print(f"  Geometry type: {gdf_points.geometry.geom_type.value_counts().to_dict()}")
        print(f"  Columns: {list(gdf_points.columns)}")
        print(gdf_points.head(3))

    return export_results_layer, gdf_points, out_dir, var


@app.cell
def __(Path, export_results_layer, gdf_points, out_dir, plan_hdf, var):
    import geopandas as gpd

    # Check if mesh_cells.parquet exists from notebook 01
    mesh_cells_path = Path("out/01_export_geometry/mesh_cells.parquet")

    if plan_hdf is None or gdf_points is None:
        print("No results — skipping polygon join")
    elif not mesh_cells_path.exists():
        print(f"mesh_cells.parquet not found at {mesh_cells_path}")
        print("Run notebook 01 first to generate geometry output")
    else:
        # Export with polygon join
        poly_out = out_dir / "max_depth_polygons.parquet"
        export_results_layer(plan_hdf, poly_out, variable=var, geom_file=mesh_cells_path)

        gdf_poly = gpd.read_parquet(poly_out)
        print(f"Results (joined to polygons): {len(gdf_poly)} features")
        print(f"  Geometry type: {gdf_poly.geometry.geom_type.value_counts().to_dict()}")
        print(gdf_poly.head(3))


@app.cell
def __(Path, out_dir, plan_hdf, variables):
    from ras2cng.results import export_all_variables

    if plan_hdf is None or not variables:
        print("No results — skipping export all")
    else:
        all_out_dir = out_dir / "all_variables"
        all_out_dir.mkdir(exist_ok=True)

        # Use mesh_cells if available
        mesh_cells_path = Path("out/01_export_geometry/mesh_cells.parquet")
        geom = mesh_cells_path if mesh_cells_path.exists() else None

        exported = export_all_variables(plan_hdf, all_out_dir, geom_file=geom)
        print(f"Exported {len(exported)} variables:")
        for v in exported:
            fname = f"{v.lower().replace(' ', '_')}.parquet"
            fpath = all_out_dir / fname
            size = fpath.stat().st_size / 1e3 if fpath.exists() else 0
            print(f"  {v} → {fname}  ({size:.1f} KB)")


if __name__ == "__main__":
    app.run()
