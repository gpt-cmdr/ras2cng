# Architecture Overview

## Module Map

```
ras2cng/
├── cli.py           — Typer CLI (5 commands): geometry, results, query, pmtiles, sync
├── geometry.py      — HDF + text geometry export via ras-commander
├── results.py       — Plan HDF results export + polygon join
├── duckdb_session.py — DuckDB wrapper with auto-loaded spatial extension
├── pmtiles.py       — Vector/raster PMTiles pipeline
└── postgis_sync.py  — GeoParquet → PostGIS via SQLAlchemy/GeoAlchemy2
```

## CLI Layer

`cli.py` uses **lazy imports** — heavy dependencies (geopandas, duckdb, sqlalchemy) are imported
inside each command function, not at module level. This keeps `ras2cng --help` fast even when
optional extras are not installed.

## Data Flow

```
HEC-RAS file
     │
     ▼
geometry.py / results.py
     │  ras-commander parses HDF/text
     │  returns GeoDataFrame
     ▼
GeoParquet (snappy compression, index=False)
     │
     ├── duckdb_session.py  →  SQL analytics
     ├── pmtiles.py         →  GeoJSON → tippecanoe → PMTiles
     └── postgis_sync.py    →  SQLAlchemy → PostGIS + GIST index
```

## Key Design Decisions

- **Snappy compression**: All output uses `to_parquet(..., compression="snappy", index=False)` — maximum read performance
- **snake_case columns**: ras-commander normalizes all column names (e.g., "Maximum Depth" → `maximum_depth`)
- **Table alias `_`**: DuckDB queries always use `_` as the table name — consistent, short, collision-free
- **Suffix-based detection**: File type is determined from the file extension, not file inspection — fast and predictable
- **Polygon join**: Results from `HdfResultsMesh` are points (one per mesh cell centroid); when a geometry file is provided, they are merged onto polygon cells via `(mesh_name, cell_id)`
