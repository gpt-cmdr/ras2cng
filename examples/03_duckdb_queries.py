import marimo

__generated_with = "0.9.0"
app = marimo.App(width="medium")


@app.cell
def __():
    """
    03 — DuckDB Queries
    ===================
    SQL analytics on exported GeoParquet files using DuckDB.
    Requires outputs from notebook 01 (mesh_cells.parquet).
    Output goes to out/03_duckdb_queries/
    """
    import marimo as mo
    mo.md("## 03 — DuckDB Queries")


@app.cell
def __():
    from pathlib import Path

    # Check for required inputs from notebook 01
    mesh_cells_path = Path("out/01_export_geometry/mesh_cells.parquet")

    if not mesh_cells_path.exists():
        print("⚠  mesh_cells.parquet not found.")
        print("   Run notebook 01 first: python examples/01_export_geometry.py")
        has_geometry = False
    else:
        import geopandas as gpd
        gdf = gpd.read_parquet(mesh_cells_path)
        print(f"mesh_cells.parquet: {len(gdf)} features, CRS={gdf.crs}")
        print(f"  Columns: {list(gdf.columns)}")
        has_geometry = True

    return Path, has_geometry, mesh_cells_path


@app.cell
def __(has_geometry, mesh_cells_path):
    from ras2cng.duckdb_session import query_parquet

    if not has_geometry:
        print("No geometry data — skipping basic queries")
    else:
        # Basic query using _ table alias
        print("=== Row count ===")
        df_count = query_parquet(mesh_cells_path, "SELECT COUNT(*) AS n FROM _")
        print(df_count)

        print("\n=== Mesh names ===")
        df_meshes = query_parquet(mesh_cells_path, "SELECT DISTINCT mesh_name FROM _ ORDER BY mesh_name")
        print(df_meshes)

        print("\n=== First 5 rows (no geometry) ===")
        cols = [c for c in __import__("geopandas").read_parquet(mesh_cells_path).columns if c != "geometry"]
        col_select = ", ".join(cols[:5])
        df_head = query_parquet(mesh_cells_path, f"SELECT {col_select} FROM _ LIMIT 5")
        print(df_head)

    return query_parquet,


@app.cell
def __(has_geometry, mesh_cells_path):
    from ras2cng.duckdb_session import DuckSession

    if not has_geometry:
        print("No geometry data — skipping DuckSession demo")
    else:
        print("=== DuckSession spatial queries ===")
        with DuckSession() as duck:
            duck.register_parquet(mesh_cells_path)

            # Cell count per mesh
            df_per_mesh = duck.query(
                "SELECT mesh_name, COUNT(*) AS cell_count FROM _ GROUP BY mesh_name ORDER BY cell_count DESC"
            )
            print("Cell count per mesh:")
            print(df_per_mesh)

            # Cell areas using ST_Area (geometry must be in a projected CRS for meaningful area)
            df_area = duck.query("""
                SELECT
                    mesh_name,
                    COUNT(*) AS n_cells,
                    ROUND(AVG(ST_Area(geometry)), 2) AS avg_cell_area,
                    ROUND(MIN(ST_Area(geometry)), 2) AS min_cell_area,
                    ROUND(MAX(ST_Area(geometry)), 2) AS max_cell_area
                FROM _
                GROUP BY mesh_name
                ORDER BY mesh_name
            """)
            print("\nCell area statistics by mesh:")
            print(df_area)


@app.cell
def __(Path, has_geometry, mesh_cells_path, query_parquet):
    # Check for results from notebook 02
    results_path = Path("out/02_export_results/max_depth_polygons.parquet")
    has_results = results_path.exists()

    if not has_results:
        print("max_depth_polygons.parquet not found — skipping multi-table join demo")
        print("Run notebook 02 first (requires HEC-RAS model run)")
    elif not has_geometry:
        print("No geometry data available")
    else:
        from ras2cng.duckdb_session import DuckSession

        print("=== Multi-table join: geometry + results ===")
        with DuckSession() as duck:
            duck.register_parquet(mesh_cells_path, alias="cells")
            duck.register_parquet(results_path, alias="results")

            df_joined = duck.query("""
                SELECT
                    r.mesh_name,
                    COUNT(*) AS n_cells,
                    ROUND(AVG(r.maximum_depth), 3) AS avg_depth,
                    ROUND(MAX(r.maximum_depth), 3) AS max_depth
                FROM results r
                GROUP BY r.mesh_name
                ORDER BY max_depth DESC
            """)
            print(df_joined)

    return has_results, results_path


@app.cell
def __(Path, has_geometry, query_parquet):
    # Save a filtered subset for use by notebook 04
    if has_geometry:
        out_dir = Path("out/03_duckdb_queries")
        out_dir.mkdir(parents=True, exist_ok=True)

        # Show CLI equivalent
        print("=== CLI equivalent ===")
        print("ras2cng query out/01_export_geometry/mesh_cells.parquet \\")
        print('  "SELECT mesh_name, COUNT(*) AS n FROM _ GROUP BY mesh_name"')

    print("\nDone. See out/03_duckdb_queries/ for saved outputs.")


if __name__ == "__main__":
    app.run()
