import marimo

__generated_with = "0.9.0"
app = marimo.App(width="medium")


@app.cell
def __():
    """
    06 — Project Archive (Consolidated Format)
    ===========================================
    Demonstrates full-project archival using archive_project().
    Archives BaldEagleCrkMulti2D (geometry only — no plan HDF results
    since the example project hasn't been run through HEC-RAS).

    Output: one consolidated parquet per geometry source file, plus a
    project metadata parquet — all at the archive root level.
    """
    import marimo as mo
    mo.md("## 06 — Project Archive (Consolidated Format)")


@app.cell
def __():
    import json
    from pathlib import Path

    from ras_commander import RasExamples

    project_path = RasExamples.extract_project("BaldEagleCrkMulti2D")
    print(f"Project path: {project_path}")

    # Show what files are present
    prj_files = sorted(project_path.glob("*.prj"))
    geom_hdfs = sorted(project_path.glob("*.g??.hdf"))
    plan_hdfs = sorted(project_path.glob("*.p??.hdf"))
    print(f"  .prj files:   {[f.name for f in prj_files]}")
    print(f"  .g??.hdf:     {[f.name for f in geom_hdfs]}")
    print(f"  .p??.hdf:     {[f.name for f in plan_hdfs]} (none = model not run)")

    return Path, RasExamples, geom_hdfs, json, plan_hdfs, project_path, prj_files


@app.cell
def __(Path, project_path):
    from ras2cng import inspect_project

    info = inspect_project(project_path)
    print(f"Project name : {info.name}")
    print(f"CRS          : {info.crs or 'unknown'}")
    print(f"Units        : {info.units}")
    print(f"Geometry files ({len(info.geom_files)}):")
    for g in info.geom_files:
        hdf_size = (
            f"{Path(g['hdf_path']).stat().st_size / 1e6:.1f} MB"
            if g.get("hdf_path") and Path(g["hdf_path"]).exists()
            else "no HDF"
        )
        print(f"  {g['id']}  hdf={hdf_size}")
    print(f"Plan files ({len(info.plan_files)}):")
    for p in info.plan_files:
        print(f"  {p['id']}  title={p.get('title','?')}  hdf_exists={p.get('hdf_exists', False)}")

    return info, inspect_project


@app.cell
def __(Path, project_path):
    from ras2cng import archive_project

    out_dir = Path("out/06_project_archive")
    out_dir.mkdir(parents=True, exist_ok=True)

    print("Archiving project (geometry only) ...")
    manifest = archive_project(
        project_path,
        out_dir,
        include_results=False,   # plan HDFs don't exist for this example
        include_terrain=False,   # skip COG conversion
    )
    print("Done.")
    print(f"manifest.json: {out_dir / 'manifest.json'}")

    return archive_project, manifest, out_dir


@app.cell
def __(manifest):
    print("=== Manifest Summary ===")
    print(f"schema_version : {manifest.schema_version}")
    print(f"project name   : {manifest.project['name']}")
    print(f"crs            : {manifest.project.get('crs', 'unknown')}")
    print(f"geometry IDs   : {manifest.geom_ids}")
    print(f"plan IDs       : {manifest.plan_ids}")
    print(f"terrain entries: {len(manifest.terrain)}")
    print(f"project parquet: {manifest.project_parquet}")
    print()
    for entry in manifest.geometry:
        print(f"  geom {entry['geom_id']}  file={entry['parquet']}  ({len(entry['layers'])} layers):")
        for lyr in entry["layers"]:
            print(f"    WHERE layer = '{lyr['filter_value']}'  {lyr['rows']:>7,} rows  {lyr['geometry_type']}")

    return


@app.cell
def __(out_dir):
    import os

    print("=== Archive Directory Structure ===")
    for root, dirs, files in os.walk(out_dir):
        level = root.replace(str(out_dir), "").count(os.sep)
        indent = "  " * level
        folder_name = os.path.basename(root) or out_dir.name
        print(f"{indent}{folder_name}/")
        subindent = "  " * (level + 1)
        for f in sorted(files):
            size = os.path.getsize(os.path.join(root, f))
            print(f"{subindent}{f}  ({size / 1024:.0f} KB)")

    return indent, level, os, root, subindent


@app.cell
def __(out_dir):
    import geopandas as gpd

    # Find the consolidated geometry parquet
    geom_files = sorted(out_dir.glob("*.g??.parquet"))
    if not geom_files:
        print("No consolidated geometry parquet found")
    else:
        gdf = gpd.read_parquet(geom_files[0])
        print(f"Consolidated geometry: {geom_files[0].name}")
        print(f"  total rows : {len(gdf):,}")
        print(f"  CRS        : {gdf.crs}")
        print(f"  layers     : {sorted(gdf['layer'].unique())}")
        print()
        # Show mesh_cells subset
        mc = gdf[gdf["layer"] == "mesh_cells"]
        if len(mc) > 0:
            print(f"  mesh_cells ({len(mc):,} rows):")
            print(mc.head(3))

    return gdf, gpd, geom_files, mc


@app.cell
def __(out_dir):
    from ras2cng import DuckSession

    geom_pqs = sorted(out_dir.glob("*.g??.parquet"))
    if not geom_pqs:
        print("No geometry parquet found - skipping DuckDB query")
    else:
        with DuckSession() as db:
            db.register_parquet("cells", geom_pqs[0])
            df = db.query(
                "SELECT layer, COUNT(*) AS n_rows "
                "FROM _ GROUP BY layer ORDER BY n_rows DESC"
            )
        print("Row counts by layer (DuckDB query):")
        print(df.to_string(index=False))
        print()
        print("Query pattern: SELECT * FROM 'Model.g01.parquet' WHERE layer = 'mesh_cells'")

    return DuckSession, df, geom_pqs


@app.cell
def __(out_dir):
    import json as _json

    manifest_path = out_dir / "manifest.json"
    raw = _json.loads(manifest_path.read_text())
    print("manifest.json (pretty-printed):")
    print(_json.dumps(raw, indent=2)[:2000], "..." if len(_json.dumps(raw)) > 2000 else "")

    return _json, manifest_path, raw


if __name__ == "__main__":
    app.run()
