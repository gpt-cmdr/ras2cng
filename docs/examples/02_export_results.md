# 02 — Export Results

**Notebook**: [examples/02_export_results.py](https://github.com/gpt-cmdr/ras2cng/blob/master/examples/02_export_results.py)

Exports 2D mesh summary results from `BaldEagleCrkMulti2D` plan HDF files and joins them to
polygon geometry.

## What it demonstrates

- Check for available plan HDF files (`.p??.hdf`) in the project
- Export `Maximum Depth` as point geometry
- Re-export joined to `mesh_cells` polygon geometry
- Export all available variables with `export_all_variables()`
- Column name snake_case normalization

## Notes

`BaldEagleCrkMulti2D` does not include pre-computed plan results. The notebook guides you to
either run the HEC-RAS model first, or explains what the output would look like with result files present.

## Run it

```bash
marimo edit examples/02_export_results.py
# or
python examples/02_export_results.py
```
