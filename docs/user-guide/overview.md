# Architecture Overview

## Module Map

```
ras2cng/
├── cli.py           — Typer CLI (7 commands): inspect, archive, geometry, results, query, pmtiles, sync
├── project.py       — Full-project orchestration: inspect, archive, metadata export
├── catalog.py       — Manifest schema v2.0 for archive catalogs (manifest.json)
├── geometry.py      — HDF + text geometry export via ras-commander (10 HDF + 3 text layers)
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
HEC-RAS project directory
     │
     ▼
project.py (archive_project / inspect_project)
     │  Discovers all geometry, plan, and terrain files
     │  Orchestrates extraction via geometry.py / results.py
     ▼
geometry.py / results.py
     │  ras-commander parses HDF/text
     │  returns GeoDataFrame
     ▼
GeoParquet (ZSTD compression for archives, bbox columns, Hilbert sorted)
     │
     ├── catalog.py         →  manifest.json (schema v2.0)
     ├── duckdb_session.py  →  SQL analytics
     ├── pmtiles.py         →  GeoJSON → tippecanoe → PMTiles
     └── postgis_sync.py    →  SQLAlchemy → PostGIS + GIST index
```

## Key Design Decisions

- **ZSTD compression for archives**: `archive_project()` uses ZSTD compression with per-row bbox columns and GeoParquet `covering` metadata for spatial predicate pushdown. Legacy single-file exports still use snappy for maximum read performance
- **snake_case columns**: ras-commander normalizes all column names (e.g., "Maximum Depth" → `maximum_depth`)
- **Table alias `_`**: DuckDB queries always use `_` as the table name — consistent, short, collision-free
- **Suffix-based detection**: File type is determined from the file extension, not file inspection — fast and predictable
- **Polygon join**: Results from `HdfResultsMesh` are points (one per mesh cell centroid); when a geometry file is provided, they are merged onto polygon cells via `(mesh_name, cell_id)`
