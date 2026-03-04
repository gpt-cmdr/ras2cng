# 03 — DuckDB Queries

**Notebook**: [examples/03_duckdb_queries.py](https://github.com/gpt-cmdr/ras2cng/blob/master/examples/03_duckdb_queries.py)

SQL analytics on exported GeoParquet files using DuckDB.

## What it demonstrates

- `query_parquet()` for basic SELECT + WHERE + GROUP BY
- `DuckSession` multi-table: join geometry and results
- Spatial queries: `ST_Area()`, `ST_Centroid()`
- The `_` table alias convention
- Depth-weighted area statistics

## Run it

```bash
marimo edit examples/03_duckdb_queries.py
# or
python examples/03_duckdb_queries.py
```

Requires outputs from notebook 01 (geometry parquet files).
