import marimo

__generated_with = "0.9.0"
app = marimo.App(width="medium")


@app.cell
def __():
    """
    07 — Result Maps (map and map-hdf)
    ===================================
    Demonstrates rendered result rasters via the HEC-RAS mapping engine:

    1. `generate_result_maps()` on a full project (the `map` command)
    2. `read_plan_hdf_metadata()` + `build_scaffold()` — maps from ONLY a
       plan HDF + terrain raster, no project needed (the `map-hdf` command)
    3. Whole-simulation map types: Arrival Time, Duration, Percent Time
       Inundated with an `arrival_depth` wet/dry threshold

    Requires a Windows HEC-RAS install (6.6 recommended) and a COMPUTED plan
    HDF. `BaldEagleCrkMulti2D` ships without results — set RUN_MODEL = True
    below to compute one plan first (several minutes), or point PLAN_HDF at
    any computed plan HDF you already have.
    """
    import marimo as mo
    mo.md("## 07 — Result Maps (map and map-hdf)")


@app.cell
def __():
    from pathlib import Path

    from ras_commander import RasExamples

    project_path = RasExamples.extract_project("BaldEagleCrkMulti2D")
    print(f"Project path: {project_path}")

    plan_hdfs = sorted(project_path.glob("*.p??.hdf"))
    print(f"Computed plan HDFs: {[p.name for p in plan_hdfs] or 'none'}")

    return Path, RasExamples, plan_hdfs, project_path


@app.cell
def __(plan_hdfs, project_path):
    # Optional: compute a plan so results exist (several minutes).
    RUN_MODEL = False

    if RUN_MODEL and not plan_hdfs:
        from ras_commander import init_ras_project, RasCmdr

        init_ras_project(project_path, ras_object="new")
        RasCmdr.compute_plan("07", ras_object="new")
        print("Plan 07 computed.")
    elif not plan_hdfs:
        print("No computed results — set RUN_MODEL = True (or use your own HDF below).")

    return (RUN_MODEL,)


@app.cell
def __(Path, project_path):
    # Pick the plan HDF to map. Swap in any computed plan HDF you have —
    # the filename does NOT need to match the project naming convention.
    candidates = sorted(project_path.glob("*.p??.hdf"))
    PLAN_HDF = candidates[0] if candidates else None
    print(f"PLAN_HDF = {PLAN_HDF}")

    out_root = Path("out/07_result_maps")
    out_root.mkdir(parents=True, exist_ok=True)

    return PLAN_HDF, candidates, out_root


@app.cell
def __(PLAN_HDF):
    # --- Part 1: what the plan HDF carries (basis of the map-hdf command) ---
    if PLAN_HDF is not None:
        from ras2cng import read_plan_hdf_metadata

        meta = read_plan_hdf_metadata(PLAN_HDF)
        print(f"Project name : {meta.project_name}")
        print(f"Plan number  : {meta.plan_number}")
        print(f"Plan ShortID : {meta.plan_short_id}  (RASMapper output folder)")
        print(f"Units        : {meta.units}")
        print(f"Projection   : {'embedded WKT' if meta.projection_wkt else 'MISSING'}")
        print(f"Simulation   : {meta.sim_start} -> {meta.sim_end}")
    else:
        meta = None
        print("Skipped: no computed plan HDF available.")

    return (meta,)


@app.cell
def __(PLAN_HDF, out_root, project_path):
    # --- Part 2: full-project mapping (equivalent to `ras2cng map`) ---------
    # WSE + Depth at the Max profile, sloping water surface.
    if PLAN_HDF is not None:
        from ras2cng.mapping import generate_result_maps

        results = generate_result_maps(
            project_path,
            out_root / "from_project",
            wse=True,
            depth=True,
            velocity=False,
            render_mode="sloping",
            ras_version="6.6",
        )
        for r in results:
            for map_type, paths in r.map_types.items():
                print(f"{r.plan_id} {map_type}: {[p.name for p in paths]}")
    else:
        print("Skipped: no computed plan HDF available.")


@app.cell
def __(PLAN_HDF, meta, out_root):
    # --- Part 3: minimal inputs (equivalent to `ras2cng map-hdf`) -----------
    # Only the plan HDF + a raw terrain GeoTIFF. build_scaffold() synthesizes
    # the project around the HDF and builds the HEC-RAS terrain headlessly.
    #
    # CLI equivalent:
    #   ras2cng map-hdf results.pNN.hdf out/ --terrain dem.tif
    #   ras2cng map-hdf results.pNN.hdf out/ --terrain-hdf Terrain50.hdf
    TERRAIN_TIF = None  # <- set to a DEM GeoTIFF covering the model, e.g. Path("dem.tif")

    if PLAN_HDF is not None and TERRAIN_TIF is not None:
        from ras2cng import build_scaffold
        from ras2cng.mapping import generate_result_maps as _maps

        info = build_scaffold(
            PLAN_HDF,
            out_root / "scaffold",
            terrain_tifs=[TERRAIN_TIF],
            ras_version="6.6",
        )
        print(f"Scaffold: {info.project_dir}")

        _maps(
            info.prj_file,
            out_root / "from_hdf_only",
            plans=[f"p{meta.plan_number}"],
            wse=True,
            depth=True,
            velocity=False,
            render_mode="sloping",
            ras_version="6.6",
        )
    else:
        print("Skipped: set TERRAIN_TIF to a DEM GeoTIFF to run the minimal-inputs path.")


@app.cell
def __(PLAN_HDF, out_root, project_path):
    # --- Part 4: whole-simulation map types ----------------------------------
    # Arrival Time / Duration / Percent Time Inundated are computed over the
    # entire simulation (the profile option does not apply) and are labeled by
    # the arrival_depth wet/dry threshold: "Arrival Time (0.5ft hrs).tif".
    #
    # CLI equivalent:
    #   ras2cng map PROJECT out/ --no-wse --no-depth --no-velocity ^
    #       --arrival-time --duration --percent-inundated --arrival-depth 0.5
    if PLAN_HDF is not None:
        from ras2cng.mapping import generate_result_maps as _maps_adr

        results_adr = _maps_adr(
            project_path,
            out_root / "whole_simulation",
            wse=False,
            depth=False,
            velocity=False,
            arrival_time=True,
            duration=True,
            percent_inundated=True,
            arrival_depth=0.5,
            render_mode="sloping",
            ras_version="6.6",
        )
        for r in results_adr:
            for map_type, paths in r.map_types.items():
                print(f"{r.plan_id} {map_type}: {[p.name for p in paths]}")
    else:
        print("Skipped: no computed plan HDF available.")


@app.cell
def __(out_root):
    # --- Inspect one output raster with rasterio -----------------------------
    tifs = sorted(out_root.rglob("*.tif"))
    if tifs:
        import rasterio

        with rasterio.open(tifs[0]) as ds:
            data = ds.read(1, masked=True)
            print(f"{tifs[0].name}")
            print(f"  CRS   : {ds.crs}")
            print(f"  Size  : {ds.width} x {ds.height} @ {ds.res[0]:.1f}")
            print(f"  Range : {data.min():.2f} to {data.max():.2f}")
            print(f"  Wet px: {data.count():,}")
    else:
        print("No rasters generated yet.")


if __name__ == "__main__":
    app.run()
