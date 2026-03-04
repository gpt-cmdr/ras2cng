# DuckDB Queries

## Overview

ras2cng uses DuckDB with the spatial extension for SQL analytics on GeoParquet files.
No database server is required — DuckDB runs in-process.

## Table Alias Convention

The table alias is always **`_`** (underscore). This is a ras2cng convention — consistent,
short, and collision-free.

```sql
SELECT * FROM _ WHERE maximum_depth > 3.0
SELECT mesh_name, AVG(maximum_depth) FROM _ GROUP BY mesh_name
SELECT COUNT(*) FROM _ WHERE maximum_depth IS NOT NULL
```

## CLI

```bash
# Basic query (prints first 20 rows)
ras2cng query max_depth.parquet \
  "SELECT mesh_name, maximum_depth FROM _ ORDER BY maximum_depth DESC LIMIT 10"

# Save to CSV
ras2cng query max_depth.parquet \
  "SELECT * FROM _ WHERE maximum_depth > 5.0" \
  --output deep_areas.csv

# Save to Parquet
ras2cng query max_depth.parquet \
  "SELECT * FROM _ WHERE maximum_depth > 5.0" \
  --output deep_areas.parquet
```

## Python API — `query_parquet()`

```python
from ras2cng.duckdb_session import query_parquet
from pathlib import Path

df = query_parquet(
    Path("max_depth.parquet"),
    "SELECT mesh_name, MAX(maximum_depth) AS max_depth FROM _ GROUP BY mesh_name"
)
print(df)
```

## Python API — `DuckSession`

For multi-table queries or repeated queries, use `DuckSession` as a context manager:

```python
from ras2cng.duckdb_session import DuckSession

with DuckSession() as duck:
    duck.register_parquet("mesh_cells.parquet", alias="cells")
    duck.register_parquet("max_depth.parquet", alias="results")

    # Join geometry and results
    df = duck.query("""
        SELECT c.mesh_name, c.cell_id, r.maximum_depth, ST_Area(c.geometry) AS area_m2
        FROM cells c
        JOIN results r ON c.mesh_name = r.mesh_name AND c.cell_id = r.cell_id
        WHERE r.maximum_depth > 2.0
    """)
```

## Spatial Queries

The spatial extension is auto-loaded. Use `ST_*` functions directly:

```python
from ras2cng.duckdb_session import DuckSession

with DuckSession() as duck:
    duck.register_parquet("mesh_cells.parquet")
    df = duck.query("""
        SELECT
            mesh_name,
            ST_Area(geometry) AS cell_area_m2,
            ST_Centroid(geometry) AS centroid
        FROM _
        ORDER BY cell_area_m2 DESC
        LIMIT 20
    """)
```

## WKB Geometry Handling

`register_parquet()` auto-detects WKB geometry columns (named `geometry`) and wraps them with
`ST_GeomFromWKB()` so spatial functions work without manual casting.
