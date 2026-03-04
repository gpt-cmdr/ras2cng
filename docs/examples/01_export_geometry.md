# 01 — Export Geometry

**Notebook**: [examples/01_export_geometry.py](https://github.com/gpt-cmdr/ras2cng/blob/master/examples/01_export_geometry.py)

Exports all geometry layers from the `BaldEagleCrkMulti2D` example project to GeoParquet.

## What it demonstrates

- Export `mesh_cells` from HDF geometry (`.g??.hdf`)
- Export `cross_sections` from HDF geometry
- Export `centerlines` from HDF geometry
- Export `cross_sections` from text geometry (`.g??`)
- Inspect output: GeoDataFrame shape, CRS, column names

## Run it

```bash
marimo edit examples/01_export_geometry.py
# or
python examples/01_export_geometry.py
```

## Output files

Written to `out/01_export_geometry/`:
- `mesh_cells.parquet`
- `cross_sections_hdf.parquet`
- `centerlines_hdf.parquet`
- `cross_sections_text.parquet`
