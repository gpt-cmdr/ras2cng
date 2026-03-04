import marimo

__generated_with = "0.9.0"
app = marimo.App(width="medium")


@app.cell
def __():
    """
    04 — Generate PMTiles
    =====================
    Generates vector PMTiles from mesh geometry GeoParquet.
    Requires outputs from notebook 01 (mesh_cells.parquet).
    Requires tippecanoe and pmtiles on PATH.
    Output goes to out/04_generate_pmtiles/
    """
    import marimo as mo
    mo.md("## 04 — Generate PMTiles")


@app.cell
def __():
    import shutil

    # Check for external CLI dependencies
    tippecanoe_path = shutil.which("tippecanoe")
    pmtiles_path = shutil.which("pmtiles")

    print("External CLI tools:")
    print(f"  tippecanoe: {'✓ found at ' + tippecanoe_path if tippecanoe_path else '✗ NOT FOUND'}")
    print(f"  pmtiles:    {'✓ found at ' + pmtiles_path if pmtiles_path else '✗ NOT FOUND'}")

    has_tools = bool(tippecanoe_path and pmtiles_path)

    if not has_tools:
        print("\n⚠  tippecanoe and/or pmtiles not found on PATH.")
        print("   Install via conda-forge:")
        print("     conda install -c conda-forge tippecanoe pmtiles")
        print("   Or download from GitHub releases:")
        print("     https://github.com/felt/tippecanoe/releases")
        print("     https://github.com/protomaps/go-pmtiles/releases")

    return has_tools, pmtiles_path, tippecanoe_path


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
        print(f"Input: mesh_cells.parquet — {len(gdf)} features, CRS={gdf.crs}")
        has_geometry = True

    return Path, has_geometry, mesh_cells_path


@app.cell
def __(Path, has_geometry, has_tools, mesh_cells_path):
    from ras2cng.pmtiles import generate_pmtiles_from_input

    out_dir = Path("out/04_generate_pmtiles")
    out_dir.mkdir(parents=True, exist_ok=True)

    if not has_geometry:
        print("No geometry data — skipping PMTiles generation")
    elif not has_tools:
        print("Missing CLI tools — skipping PMTiles generation")
        print("\nCLI equivalent (once tippecanoe + pmtiles are installed):")
        print("  ras2cng pmtiles out/01_export_geometry/mesh_cells.parquet \\")
        print("    out/04_generate_pmtiles/mesh_cells.pmtiles \\")
        print("    --layer mesh_cells --min-zoom 8 --max-zoom 14")
    else:
        pmtiles_out = out_dir / "mesh_cells.pmtiles"
        print(f"Generating PMTiles → {pmtiles_out}")
        generate_pmtiles_from_input(
            mesh_cells_path,
            pmtiles_out,
            layer_name="mesh_cells",
            min_zoom=8,
            max_zoom=14,
        )
        print(f"✓ Done: {pmtiles_out.name}  ({pmtiles_out.stat().st_size / 1e6:.1f} MB)")
        print("\nServe from any static host (S3, GitHub Pages, Cloudflare R2).")
        print("Use with MapLibre GL JS + pmtiles plugin for serverless web maps.")

    return generate_pmtiles_from_input, out_dir


@app.cell
def __(has_tools):
    # Show CLI equivalent
    print("=== CLI equivalent ===")
    print("ras2cng pmtiles out/01_export_geometry/mesh_cells.parquet \\")
    print("  out/04_generate_pmtiles/mesh_cells.pmtiles \\")
    print("  --layer mesh_cells --min-zoom 8 --max-zoom 14")

    if not has_tools:
        print("\n(Requires tippecanoe + pmtiles on PATH)")


if __name__ == "__main__":
    app.run()
