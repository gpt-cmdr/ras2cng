# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**ras2cng** (RAS to Cloud Native GIS) — CLI tool for exporting HEC-RAS geometry and results to GeoParquet, querying with DuckDB, generating PMTiles vector/raster tiles, and syncing to PostGIS. Supports **full-project archival**: discovers all geometry configurations, plan runs, and terrain rasters in a HEC-RAS project and exports them to a structured, cloud-native archive with `manifest.json` catalog.

Built on [`ras-commander`](https://github.com/gpt-cmdr/ras-commander) for HEC-RAS file parsing.

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

# Archive a full project (geometry only, then with results)
uv run ras2cng inspect path/to/project
uv run ras2cng archive path/to/project /output/dir
uv run ras2cng archive path/to/project /output/dir --results --terrain

# Build distributable wheel
uv build

# Add a runtime dependency
uv add somepackage

# Add a dev-only dependency
uv add --dev somepackage
```

Python >= 3.10 required (`.python-version` pins 3.12). Virtual environment is managed by uv in `.venv/`.

## Architecture

**CLI layer** (`cli.py`): Typer app with 7 commands (`inspect`, `archive`, `geometry`, `results`, `query`, `pmtiles`, `sync`). Uses lazy imports — heavy dependencies are imported inside command functions, not at module level.

**Core modules** — each handles one concern:
- `project.py` — Full-project orchestration. `archive_project()` produces consolidated GeoParquet files (one per geometry source, one per plan) with `layer` discriminator column. `export_project_metadata()` writes RasPrj dataframes to a plain Parquet with `_table` column. `_write_geoparquet()` helper adds per-row bbox columns and ZSTD compression with GeoParquet `covering` metadata. `inspect_project()` returns a `ProjectInfo` dataclass (no file extraction). **Note**: `init_ras_project` and `export_all_variables` are imported at module level (not lazy) to enable mock patching in tests.
- `catalog.py` — Schema v2.0. `Manifest` dataclass manages `manifest.json`. `ManifestGeomEntry` has a `parquet` field (single consolidated file) and `layers` list with `filter_value` for each layer. `ManifestPlanEntry` similarly has a `parquet` field. `project_parquet` field on `Manifest` references the metadata file.
- `geometry.py` — Detects file type (HDF `.g??.hdf` vs text `.g??`) and routes to ras-commander parsers. `HDF_LAYERS` dict maps 10 layer names to `(class, method_name)` for dispatch. `merge_all_layers()` consolidates all HDF + text layers into a single GeoDataFrame with `layer` column (text layers get `_text` suffix). `_hilbert_sort()` uses DuckDB for spatial sorting. Legacy `export_all_hdf_layers()` / `export_all_text_layers()` remain for backward compat.
- `results.py` — Exports 2D mesh summary variables from plan HDF files. `merge_all_variables()` consolidates all variables into a single GeoDataFrame with `layer` column. Accepts `mesh_cells_gdf` GeoDataFrame directly for polygon join (avoids write-then-read).
- `duckdb_session.py` — `DuckSession` class wraps DuckDB with auto-loaded spatial extension. `register_parquet()` detects WKB geometry columns and converts them to DuckDB GEOMETRY type. The table alias is always `_`.
- `pmtiles.py` — Dispatches between vector (GeoParquet → GeoJSON → tippecanoe → PMTiles) and raster (GeoTIFF → gdal_translate → pmtiles) pipelines. Requires external CLIs: `tippecanoe`, `gdal_translate`, `pmtiles`.
- `postgis_sync.py` — GeoParquet → PostGIS via SQLAlchemy/GeoAlchemy2 with automatic spatial index creation.

**Data flow**: HEC-RAS files → ras-commander → GeoDataFrame → GeoParquet (ZSTD compression, bbox columns, Hilbert sorted). Archive output uses `_write_geoparquet()` which adds `covering` metadata for spatial predicate pushdown.

## Archive Output Structure

```
{output_dir}/
├── manifest.json                         # Always written (schema v2.0)
├── {ProjectName}.parquet                 # Project metadata (RasPrj dataframes, _table column)
├── {ProjectName}.g01.parquet             # All geometry from g01 HDF + text, layer column
├── {ProjectName}.g02.parquet             # (if multiple geometry files)
├── {ProjectName}.p01.parquet             # All results from p01, layer column (--results)
└── terrain/                              # Only with --terrain
    └── Terrain50_cog.tif
```

### Querying consolidated parquets

```sql
-- Filter by layer column for homogeneous geometry types
SELECT * FROM 'Model.g01.parquet' WHERE layer = 'mesh_cells'
SELECT * FROM 'Model.g01.parquet' WHERE layer = 'bc_lines'
SELECT * FROM 'Model.p01.parquet' WHERE layer = 'maximum_depth'

-- Project metadata
SELECT * FROM 'Model.parquet' WHERE _table = 'plan_df'
```

Each consolidated GeoParquet includes per-row bbox columns (`bbox_xmin`, `bbox_ymin`, `bbox_xmax`, `bbox_ymax`) with GeoParquet `covering` metadata for spatial predicate pushdown. Rows are Hilbert-sorted within each layer for optimal spatial locality.

## Key Conventions

- ras-commander normalizes column names to **snake_case** (e.g., "Maximum Depth" → `maximum_depth`)
- DuckDB queries use `_` as the table name placeholder
- All 10 HDF geometry layers: `mesh_cells`, `mesh_areas`, `cross_sections`, `centerlines`, `bc_lines`, `breaklines`, `refinement_regions`, `reference_lines`, `reference_points`, `structures`
- Text geometry layers: `cross_sections`, `centerlines`, `storage_areas`
- File type detection is suffix-based: `.g01.hdf` = HDF geometry, `.g01` = text geometry, `.p01.hdf` = plan results
- Tests mock ras-commander calls since actual HEC-RAS model files are not in the repo
- **Results are NOT exported by default** — plan HDF files contain a copy of geometry (redundant); use `--results` flag explicitly
- Archive output uses **ZSTD compression** (not snappy) per GeoParquet best practices. Legacy per-file exports still use snappy.
- `merge_all_layers()` and `merge_all_variables()` produce consolidated GeoDataFrames with `layer` column; text layers get `_text` suffix
- Hilbert spatial sorting (via DuckDB) is default ON for archives; disable with `--no-sort`
