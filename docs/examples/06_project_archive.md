# 06 — Project Archive

**Notebook**: [examples/06_project_archive.py](https://github.com/gpt-cmdr/ras2cng/blob/master/examples/06_project_archive.py)

Full-project archival demonstration using `archive_project()` and `inspect_project()` on the `BaldEagleCrkMulti2D` example project.

## What it demonstrates

1. Extract `BaldEagleCrkMulti2D` via `RasExamples`
2. `inspect_project()` — discover geometry/plan files without extracting anything
3. `archive_project()` — export all geometry layers to a consolidated GeoParquet archive
4. Read `manifest.json` and inspect layer inventory
5. Filter by `layer` column to get homogeneous geometry types
6. Query with DuckDB using `WHERE layer = 'mesh_cells'`

## Run it

```bash
marimo edit examples/06_project_archive.py
# or
python examples/06_project_archive.py
```

## Expected output structure

```
out/06_project_archive/
├── manifest.json
├── BaldEagleCrkMulti2D.parquet       # Project metadata (RasPrj dataframes)
└── BaldEagleCrkMulti2D.g01.parquet   # All geometry layers in one file
```

Each consolidated GeoParquet contains a `layer` column for filtering:

```sql
-- Query specific layers with DuckDB
SELECT * FROM 'Model.g01.parquet' WHERE layer = 'mesh_cells'
SELECT * FROM 'Model.g01.parquet' WHERE layer = 'bc_lines'

-- List available layers
SELECT DISTINCT layer FROM 'Model.g01.parquet'

-- Project metadata
SELECT * FROM 'Model.parquet' WHERE _table = 'plan_df'
```

## Validate with geoparquet-io (optional)

```bash
# Best practice: validate GeoParquet spec compliance
pipx install geoparquet-io
gpio check all output/Model.g01.parquet
gpio inspect output/Model.g01.parquet
```

## Notes

- `BaldEagleCrkMulti2D` has no plan HDF results (model not pre-run), so `--results` would yield no output
- Terrain COG conversion is skipped in this notebook (set `include_terrain=True` to enable)
- All layers that are absent from this particular model are silently skipped
- Hilbert spatial sorting is applied within each layer by default for optimal query performance
- Per-row bounding box columns (`bbox_xmin`, `bbox_ymin`, `bbox_xmax`, `bbox_ymax`) enable spatial predicate pushdown in DuckDB
- ZSTD compression is used instead of snappy for better compression ratios
