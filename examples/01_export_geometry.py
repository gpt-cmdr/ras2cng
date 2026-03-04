import marimo

__generated_with = "0.9.0"
app = marimo.App(width="medium")


@app.cell
def __():
    """
    01 — Export Geometry
    ====================
    Exports mesh cells, cross sections, and centerlines from the
    BaldEagleCrkMulti2D example project to GeoParquet.
    Output goes to out/01_export_geometry/
    """
    import marimo as mo
    mo.md("## 01 — Export Geometry")


@app.cell
def __():
    from pathlib import Path
    from ras_commander import RasExamples

    project_path = RasExamples.extract_project("BaldEagleCrkMulti2D")
    print(f"Project: {project_path}")

    # Find the primary geometry HDF file
    geom_hdfs = sorted(project_path.glob("*.g??.hdf"))
    if not geom_hdfs:
        raise FileNotFoundError("No geometry HDF files found in BaldEagleCrkMulti2D")
    geom_hdf = geom_hdfs[0]
    print(f"Geometry HDF: {geom_hdf.name}  ({geom_hdf.stat().st_size / 1e6:.1f} MB)")

    # Find the primary text geometry file
    import re
    text_geoms = [
        f for f in sorted(project_path.iterdir())
        if re.match(r".*\.g\d{2}$", f.name)
    ]
    text_geom = text_geoms[0] if text_geoms else None
    print(f"Text geometry: {text_geom.name if text_geom else 'none'}")

    return Path, RasExamples, geom_hdf, project_path, text_geom


@app.cell
def __(Path, geom_hdf):
    from ras2cng.geometry import export_geometry_layers

    out_dir = Path("out/01_export_geometry")
    out_dir.mkdir(parents=True, exist_ok=True)

    # --- Mesh cells (2D mesh cell polygons) ---
    mesh_out = out_dir / "mesh_cells.parquet"
    export_geometry_layers(geom_hdf, mesh_out, layer="mesh_cells")

    import geopandas as gpd
    gdf_mesh = gpd.read_parquet(mesh_out)
    print(f"mesh_cells: {len(gdf_mesh)} features, CRS={gdf_mesh.crs}")
    print(f"  Geometry type: {gdf_mesh.geometry.geom_type.value_counts().to_dict()}")
    print(f"  Columns: {list(gdf_mesh.columns)}")
    print(gdf_mesh[["mesh_name", "cell_id"] + [c for c in gdf_mesh.columns if c not in ("mesh_name", "cell_id", "geometry")][:3]].head(3))

    return export_geometry_layers, gdf_mesh, mesh_out, out_dir


@app.cell
def __(export_geometry_layers, geom_hdf, out_dir):
    import geopandas as gpd

    # --- Cross sections from HDF geometry ---
    xs_hdf_out = out_dir / "cross_sections_hdf.parquet"
    export_geometry_layers(geom_hdf, xs_hdf_out, layer="cross_sections")

    gdf_xs_hdf = gpd.read_parquet(xs_hdf_out)
    print(f"cross_sections (HDF): {len(gdf_xs_hdf)} features, CRS={gdf_xs_hdf.crs}")
    print(f"  Columns: {list(gdf_xs_hdf.columns)}")

    return gdf_xs_hdf, xs_hdf_out


@app.cell
def __(export_geometry_layers, geom_hdf, out_dir):
    import geopandas as gpd

    # --- Centerlines from HDF geometry ---
    cl_hdf_out = out_dir / "centerlines_hdf.parquet"
    export_geometry_layers(geom_hdf, cl_hdf_out, layer="centerlines")

    gdf_cl_hdf = gpd.read_parquet(cl_hdf_out)
    print(f"centerlines (HDF): {len(gdf_cl_hdf)} features, CRS={gdf_cl_hdf.crs}")
    print(f"  Columns: {list(gdf_cl_hdf.columns)}")

    return cl_hdf_out, gdf_cl_hdf


@app.cell
def __(export_geometry_layers, out_dir, text_geom):
    import geopandas as gpd

    if text_geom is None:
        print("No text geometry file found — skipping text geometry export")
    else:
        # --- Cross sections from text geometry ---
        xs_text_out = out_dir / "cross_sections_text.parquet"
        export_geometry_layers(text_geom, xs_text_out, layer="cross_sections")

        gdf_xs_text = gpd.read_parquet(xs_text_out)
        print(f"cross_sections (text): {len(gdf_xs_text)} features, CRS={gdf_xs_text.crs}")
        print(f"  Columns: {list(gdf_xs_text.columns)}")
        print(gdf_xs_text.head(3))


@app.cell
def __(out_dir):
    import os
    print("\nOutput files:")
    for f in sorted(out_dir.glob("*.parquet")):
        print(f"  {f.name}  ({f.stat().st_size / 1e3:.1f} KB)")


if __name__ == "__main__":
    app.run()
