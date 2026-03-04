import marimo

__generated_with = "0.9.0"
app = marimo.App(width="medium")


@app.cell
def __():
    """
    05 — Cloud Native Stack
    =======================
    Full end-to-end workflow:
      extract → geometry → results → DuckDB → PMTiles → PostGIS

    Optional steps (PMTiles, PostGIS) are skipped gracefully when
    external tools or environment variables are not available.

    Set POSTGRES_URI env var to enable PostGIS sync:
      POSTGRES_URI=postgresql://user:pass@localhost/mydb
    """
    import marimo as mo
    mo.md("## 05 — Full Cloud Native Stack")


@app.cell
def __():
    import os
    import shutil
    from pathlib import Path

    # --- Check optional dependencies ---
    has_tippecanoe = bool(shutil.which("tippecanoe") and shutil.which("pmtiles"))
    postgres_uri = os.environ.get("POSTGRES_URI", "")
    has_postgis = bool(postgres_uri)

    print("Optional dependencies:")
    print(f"  PMTiles (tippecanoe + pmtiles): {'✓' if has_tippecanoe else '✗ not on PATH'}")
    print(f"  PostGIS (POSTGRES_URI):         {'✓ ' + postgres_uri[:30] + '...' if has_postgis else '✗ env var not set'}")

    return Path, has_postgis, has_tippecanoe, os, postgres_uri, shutil


@app.cell
def __(Path):
    from ras_commander import RasExamples

    # Step 1: Extract BaldEagleCrkMulti2D
    project_path = RasExamples.extract_project("BaldEagleCrkMulti2D")
    print(f"[1/5] Project extracted: {project_path}")

    geom_hdfs = sorted(project_path.glob("*.g??.hdf"))
    plan_hdfs = sorted(project_path.glob("*.p??.hdf"))

    if not geom_hdfs:
        raise FileNotFoundError("No geometry HDF files found")

    geom_hdf = geom_hdfs[0]
    plan_hdf = plan_hdfs[0] if plan_hdfs else None

    print(f"     Geometry HDF: {geom_hdf.name}")
    print(f"     Plan HDFs: {[f.name for f in plan_hdfs] if plan_hdfs else 'none (run model first)'}")

    return Path, RasExamples, geom_hdf, plan_hdf, plan_hdfs, project_path


@app.cell
def __(Path, geom_hdf):
    from ras2cng.geometry import export_geometry_layers

    # Step 2: Export geometry
    out_dir = Path("out/05_cloud_native_stack")
    out_dir.mkdir(parents=True, exist_ok=True)

    mesh_out = out_dir / "mesh_cells.parquet"
    export_geometry_layers(geom_hdf, mesh_out, layer="mesh_cells")

    import geopandas as gpd
    gdf_mesh = gpd.read_parquet(mesh_out)
    print(f"[2/5] Geometry exported: {len(gdf_mesh)} mesh cells → {mesh_out.name}")
    print(f"     CRS: {gdf_mesh.crs}")

    return export_geometry_layers, gdf_mesh, mesh_out, out_dir


@app.cell
def __(mesh_out, out_dir, plan_hdf):
    results_out = None

    if plan_hdf is None:
        print("[3/5] ⚠  No plan HDF — skipping results export")
        print("       Run HEC-RAS to generate .p??.hdf files, then re-run this notebook")
    else:
        from ras2cng.results import export_results_layer, list_available_summary_variables

        variables = list_available_summary_variables(plan_hdf)
        if not variables:
            print(f"[3/5] ⚠  No 2D summary variables in {plan_hdf.name}")
        else:
            var = "Maximum Depth" if "Maximum Depth" in variables else variables[0]
            results_out = out_dir / "max_depth.parquet"
            export_results_layer(plan_hdf, results_out, variable=var, geom_file=mesh_out)

            import geopandas as gpd
            gdf_results = gpd.read_parquet(results_out)
            print(f"[3/5] Results exported: {len(gdf_results)} cells with '{var}' → {results_out.name}")

    return results_out,


@app.cell
def __(mesh_out, results_out):
    from ras2cng.duckdb_session import DuckSession, query_parquet

    # Step 4: DuckDB analytics
    target = results_out if results_out is not None else mesh_out
    print(f"[4/5] DuckDB analytics on {target.name}")

    df_stats = query_parquet(target, "SELECT COUNT(*) AS n FROM _")
    print(f"     Row count: {df_stats['n'].iloc[0]}")

    if results_out is not None:
        with DuckSession() as duck:
            duck.register_parquet(results_out)
            df_mesh_stats = duck.query(
                "SELECT mesh_name, COUNT(*) AS n, ROUND(MAX(maximum_depth), 2) AS max_d FROM _ GROUP BY mesh_name"
            )
            print("     Per-mesh statistics:")
            print(df_mesh_stats.to_string(index=False))

    return DuckSession, query_parquet


@app.cell
def __(has_tippecanoe, mesh_out, out_dir):
    # Step 5a: PMTiles (optional)
    if not has_tippecanoe:
        print("[5a/5] PMTiles skipped — tippecanoe not on PATH")
        print("       conda install -c conda-forge tippecanoe pmtiles")
    else:
        from ras2cng.pmtiles import generate_pmtiles_from_input
        pmtiles_out = out_dir / "mesh_cells.pmtiles"
        generate_pmtiles_from_input(mesh_out, pmtiles_out, layer_name="mesh_cells", min_zoom=8, max_zoom=14)
        print(f"[5a/5] PMTiles generated: {pmtiles_out.name}  ({pmtiles_out.stat().st_size / 1e6:.1f} MB)")


@app.cell
def __(has_postgis, mesh_out, postgres_uri):
    # Step 5b: PostGIS sync (optional)
    if not has_postgis:
        print("[5b/5] PostGIS skipped — set POSTGRES_URI env var to enable")
        print("       Example: POSTGRES_URI=postgresql://user:pass@localhost/mydb")
    else:
        from ras2cng.postgis_sync import sync_to_postgres
        sync_to_postgres(mesh_out, postgres_uri, "ras2cng_mesh_cells", schema="public", if_exists="replace")
        print(f"[5b/5] Synced to PostGIS: public.ras2cng_mesh_cells")


@app.cell
def __(out_dir):
    print("\n=== Output files ===")
    for f in sorted(out_dir.glob("*")):
        print(f"  {f.name}  ({f.stat().st_size / 1e3:.1f} KB)")
    print("\n✓ Cloud native stack complete.")


if __name__ == "__main__":
    app.run()
