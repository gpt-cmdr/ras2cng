# Architecture Overview

## Module Map

```
ras2cng/
├── cli.py           — Typer CLI (14 commands): inspect, archive, spatial-index, geometry, results, precip, query, pmtiles, sync, terrain, map, map-hdf, terrain-mod, mannings
├── project.py       — Full-project orchestration: inspect, archive, metadata export
├── catalog.py       — Manifest schema v2.4 for archive catalogs (manifest.json)
├── geometry.py      — HDF + text geometry export via ras-commander (14 HDF + 3 text layers)
├── results.py       — Plan HDF results export with polygon/point/geometryless modes and cell/face join keys
├── precipitation.py — Gridded precipitation GeoTIFF export from HDF meteorology results
├── mapping.py       — Result raster generation via RasStoreMapHelper.exe (WSE, Depth, Velocity, etc.)
├── scaffold.py      — Barebones project synthesis from a plan HDF (powers map-hdf)
├── terrain.py       — Terrain discovery, consolidation, and downsampling
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
     │  Orchestrates extraction via geometry.py / results.py / mapping.py / terrain.py
     ▼
geometry.py / results.py          precipitation.py / mapping.py / terrain.py
     │  ras-commander parses            │  RasStoreMapHelper.exe generates
     │  HDF/text → GeoDataFrame         │  precipitation/result rasters + terrain HDFs
     ▼                                  ▼
GeoParquet/Parquet (ZSTD, bbox, indexes)  GeoTIFF rasters + terrain HDFs
     │
     ├── catalog.py         →  manifest.json (schema v2.4)
     ├── duckdb_session.py  →  SQL analytics
     ├── pmtiles.py         →  GeoJSON → tippecanoe → PMTiles
     └── postgis_sync.py    →  SQLAlchemy → PostGIS + GIST index
```

## Maps Without a Project (`map-hdf`)

`ras2cng map` needs a full HEC-RAS project on disk. When only deliverables are
available — a computed plan HDF and a terrain raster, the typical LWI handoff —
use `ras2cng map-hdf` instead. `scaffold.py` synthesizes a barebones project
around the HDF: the projection WKT, unit system, plan titles, and Plan ShortID
are all read from the HDF itself (which also carries a full geometry copy), the
HEC-RAS terrain is built headlessly from the raw TIFF via `RasProcess.exe
CreateTerrain`, and a minimal `.rasmap` is generated. The scaffold then flows
through the same `generate_result_maps()` pipeline as `map`.

The scaffold directory (default `OUTPUT/_scaffold`) is kept between runs so the
expensive terrain build is reused; pass `--rm-scaffold` to delete it. The ESRI
projection file is written to `Terrain\Projection.prj` inside the scaffold —
never next to the HEC-RAS `.prj`, which would break project resolution.

## Key Design Decisions

- **Spatial post-processing by default**: `archive_project()` runs the archive index pass unless `sort=False` or `--no-sort` is used. The CLI command `ras2cng spatial-index ARCHIVE_DIR` runs the same pass later for extraction-only archives.
- **ZSTD compression for archives**: `archive_project()` uses ZSTD compression with per-row bbox columns and GeoParquet `covering` metadata for spatial predicate pushdown. Post-processing preserves that metadata while adding `hilbert_index` to GeoParquet geometry and sorting by `layer,hilbert_index`. Legacy single-file exports still use snappy for maximum read performance
- **snake_case columns**: ras-commander normalizes all column names (e.g., "Maximum Depth" → `maximum_depth`)
- **Table alias `_`**: DuckDB queries always use `_` as the table name — consistent, short, collision-free
- **Suffix-based detection**: File type is determined from the file extension, not file inspection — fast and predictable
- **Result join keys**: Cell variables join geometry by `(mesh_name, cell_id)`, face variables join native `mesh_faces` by `(mesh_name, face_id)`. Geometryless archive result tables get `join_index` and, when matching mesh geometry exists, inherit `hilbert_index` with `index_status=spatial_join`.
