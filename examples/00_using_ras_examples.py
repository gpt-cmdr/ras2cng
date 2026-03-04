import marimo

__generated_with = "0.9.0"
app = marimo.App(width="medium")


@app.cell
def __():
    """
    00 — Using RasExamples
    ======================
    Demonstrates the RasExamples API from ras-commander:
    list available example projects and extract BaldEagleCrkMulti2D.
    All other notebooks use this pattern to avoid hardcoded paths.
    """
    import marimo as mo
    mo.md("## 00 — Using RasExamples")


@app.cell
def __():
    from ras_commander import RasExamples

    # List all available example HEC-RAS projects
    projects = RasExamples.list_projects()
    print(f"Available example projects ({len(projects)}):")
    for name in sorted(projects):
        print(f"  {name}")
    return RasExamples, projects


@app.cell
def __(RasExamples):
    # Extract BaldEagleCrkMulti2D — the multi-area 2D mesh example
    # Returns: Path to the extracted project directory
    project_path = RasExamples.extract_project("BaldEagleCrkMulti2D")
    print(f"Extracted project at: {project_path}")
    return project_path,


@app.cell
def __(project_path):
    from pathlib import Path

    # Show all files in the extracted project
    all_files = sorted(project_path.iterdir())
    print(f"\nAll files in {project_path.name}:")
    for f in all_files:
        print(f"  {f.name}")
    return Path, all_files


@app.cell
def __(project_path):
    # Find geometry HDF files (*.g??.hdf)
    geom_hdfs = sorted(project_path.glob("*.g??.hdf"))
    print(f"HDF geometry files ({len(geom_hdfs)}):")
    for f in geom_hdfs:
        print(f"  {f.name}  ({f.stat().st_size / 1e6:.1f} MB)")

    # Find text geometry files (*.g?? without .hdf)
    import re
    text_geoms = [
        f for f in sorted(project_path.iterdir())
        if re.match(r".*\.g\d{2}$", f.name)
    ]
    print(f"\nText geometry files ({len(text_geoms)}):")
    for f in text_geoms:
        print(f"  {f.name}")

    # Find plan HDF files (*.p??.hdf)
    plan_hdfs = sorted(project_path.glob("*.p??.hdf"))
    print(f"\nPlan HDF files ({len(plan_hdfs)}):")
    if plan_hdfs:
        for f in plan_hdfs:
            print(f"  {f.name}  ({f.stat().st_size / 1e6:.1f} MB)")
    else:
        print("  (none — BaldEagleCrkMulti2D does not include pre-run plan results)")
        print("  Run the HEC-RAS model to generate .p??.hdf files before using notebook 02.")

    return geom_hdfs, plan_hdfs, text_geoms


if __name__ == "__main__":
    app.run()
