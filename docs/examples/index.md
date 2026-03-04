# Example Notebooks

All examples are [marimo](https://marimo.io) reactive notebooks (`.py` format). They auto-extract
the `BaldEagleCrkMulti2D` HEC-RAS example project via `RasExamples` from ras-commander —
no manual path configuration needed.

## Run Interactively

```bash
# Install ras2cng with dev extras (includes marimo)
pip install "ras2cng[all]"
pip install marimo

# Open a notebook in the marimo editor
marimo edit examples/01_export_geometry.py

# Or run as a plain Python script
python examples/01_export_geometry.py
```

## Notebooks

| Notebook | Description |
|----------|-------------|
| [00_using_ras_examples.py](https://github.com/gpt-cmdr/ras2cng/blob/master/examples/00_using_ras_examples.py) | Introduction to `RasExamples`: list projects, extract BaldEagleCrkMulti2D, inspect files |
| [01_export_geometry.py](https://github.com/gpt-cmdr/ras2cng/blob/master/examples/01_export_geometry.py) | Export mesh cells, cross sections, and centerlines from HDF and text geometry files |
| [02_export_results.py](https://github.com/gpt-cmdr/ras2cng/blob/master/examples/02_export_results.py) | Export 2D mesh summary results and join to polygon geometry |
| [03_duckdb_queries.py](https://github.com/gpt-cmdr/ras2cng/blob/master/examples/03_duckdb_queries.py) | SQL analytics on exported GeoParquet using DuckDB |
| [04_generate_pmtiles.py](https://github.com/gpt-cmdr/ras2cng/blob/master/examples/04_generate_pmtiles.py) | Generate PMTiles for web visualization (requires tippecanoe + pmtiles on PATH) |
| [05_cloud_native_stack.py](https://github.com/gpt-cmdr/ras2cng/blob/master/examples/05_cloud_native_stack.py) | Full end-to-end workflow: extract → geometry → results → DuckDB → PMTiles → PostGIS |

## Output

Each notebook writes output to `out/<notebook_name>/`. This directory is git-ignored.
Geometry outputs from notebook 01 are shared by notebooks 02–05.

## Notes

- Marimo notebooks are plain `.py` files — clean git diffs, runnable as scripts
- `RasExamples.extract_project()` downloads and extracts example projects from ras-commander to a temp directory
- `BaldEagleCrkMulti2D` does not include pre-run plan results — notebook 02 guides you through running the model or using available files
- If tippecanoe/pmtiles are not on PATH, notebook 04 will print a helpful message and skip tile generation
