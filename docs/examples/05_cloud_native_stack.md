# 05 — Cloud Native Stack

**Notebook**: [examples/05_cloud_native_stack.py](https://github.com/gpt-cmdr/ras2cng/blob/master/examples/05_cloud_native_stack.py)

Full end-to-end workflow demonstrating the complete ras2cng cloud-native pipeline.

## What it demonstrates

1. Extract `BaldEagleCrkMulti2D` via `RasExamples`
2. Export geometry → GeoParquet
3. Export results → GeoParquet (with polygon join if results available)
4. DuckDB analytics: filter by depth, compute statistics
5. PMTiles generation (skipped gracefully if tippecanoe not on PATH)
6. PostGIS sync (skipped if `POSTGRES_URI` env var not set)
7. Optional deps handled gracefully throughout

## Run it

```bash
marimo edit examples/05_cloud_native_stack.py
# or
python examples/05_cloud_native_stack.py

# With PostGIS target
POSTGRES_URI="postgresql://user:pass@localhost/mydb" python examples/05_cloud_native_stack.py
```
