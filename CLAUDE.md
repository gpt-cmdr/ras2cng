# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**ras2cng** (RAS to Cloud Native GIS) — CLI tool for exporting HEC-RAS geometry and results to GeoParquet, querying with DuckDB, generating PMTiles vector/raster tiles, and syncing to PostGIS. Built on [`ras-commander`](https://github.com/gpt-cmdr/ras-commander) for HEC-RAS file parsing.

## Build & Development Commands

```bash
# Install with all optional dependencies (creates/updates .venv and uv.lock)
uv sync --all-extras

# Run tests
uv run pytest

# Run a single test
uv run pytest tests/test_geometry_detection.py -v

# Run the CLI
uv run ras2cng --help

# Build distributable wheel
uv build

# Add a runtime dependency
uv add somepackage

# Add a dev-only dependency
uv add --dev somepackage
```

Python >= 3.10 required (`.python-version` pins 3.12). Virtual environment is managed by uv in `.venv/`.

## Architecture

**CLI layer** (`cli.py`): Typer app with 5 commands (`geometry`, `results`, `query`, `pmtiles`, `sync`). Uses lazy imports — heavy dependencies are imported inside command functions, not at module level.

**Core modules** — each handles one concern:
- `geometry.py` — Detects file type (HDF `.g??.hdf` vs text `.g??`) and routes to the appropriate ras-commander parser (`HdfMesh`, `HdfXsec`, `GeomParser`). Falls back from mesh cell polygons to points if polygons unavailable.
- `results.py` — Exports 2D mesh summary variables from plan HDF files. When results are points and a polygon geometry file is provided, joins results onto polygons via `mesh_name`/`cell_id` merge.
- `duckdb_session.py` — `DuckSession` class wraps DuckDB with auto-loaded spatial extension. `register_parquet()` detects WKB geometry columns and converts them to DuckDB GEOMETRY type. The table alias is always `_`.
- `pmtiles.py` — Dispatches between vector (GeoParquet → GeoJSON → tippecanoe → PMTiles) and raster (GeoTIFF → gdal_translate → pmtiles) pipelines. Requires external CLIs: `tippecanoe`, `gdal_translate`, `pmtiles`.
- `postgis_sync.py` — GeoParquet → PostGIS via SQLAlchemy/GeoAlchemy2 with automatic spatial index creation.

**Data flow**: HEC-RAS files → ras-commander → GeoDataFrame → GeoParquet (snappy compression). All output uses `to_parquet(..., compression="snappy", index=False)`.

## Key Conventions

- ras-commander normalizes column names to **snake_case** (e.g., "Maximum Depth" → `maximum_depth`)
- DuckDB queries use `_` as the table name placeholder
- Geometry layers: `mesh_cells`, `cross_sections`, `centerlines`
- File type detection is suffix-based: `.g01.hdf` = HDF geometry, `.g01` = text geometry, `.p01.hdf` = plan results
- Tests mock ras-commander calls since actual HEC-RAS model files are not in the repo
