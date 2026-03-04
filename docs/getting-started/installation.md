# Installation

## Core Install

```bash
pip install ras2cng
```

Installs the geometry and results export pipeline. No DuckDB, PostGIS, or PMTiles extras needed for basic use.

## Full Install (All Extras)

```bash
pip install "ras2cng[all]"
```

Using `uv` (recommended, 10–100x faster):

```bash
uv pip install "ras2cng[all]"
```

## Install Specific Extras

```bash
pip install "ras2cng[duckdb]"    # DuckDB SQL analytics
pip install "ras2cng[postgis]"   # PostGIS sync (SQLAlchemy + GeoAlchemy2 + psycopg2)
pip install "ras2cng[pmtiles]"   # rasterio (PMTiles pipeline also needs external CLIs)
```

## Dev Install from Source

```bash
git clone https://github.com/gpt-cmdr/ras2cng.git
cd ras2cng
uv sync --all-extras
uv run ras2cng --help
```

## PMTiles External CLI Tools

The `ras2cng pmtiles` command for vector tiles requires two external CLI tools (not available via pip):

- **tippecanoe** — Generates vector tiles from GeoJSON
- **pmtiles** — go-pmtiles CLI for MBTiles → PMTiles conversion

Install via conda-forge:

```bash
conda install -c conda-forge tippecanoe pmtiles
```

Or download pre-built binaries from [protomaps/go-pmtiles releases](https://github.com/protomaps/go-pmtiles/releases) and [felt/tippecanoe releases](https://github.com/felt/tippecanoe/releases).

For raster PMTiles, `gdal_translate` (part of GDAL) is also required.

## Requirements

- Python >= 3.10
- [ras-commander](https://github.com/gpt-cmdr/ras-commander) — installed automatically as a dependency
- Core: pandas, geopandas, pyarrow, typer, rich
- Optional: duckdb, sqlalchemy, geoalchemy2, psycopg2-binary, rasterio
