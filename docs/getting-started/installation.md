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

Vector tiles can be built without any external tools by installing the
freestiler extra. `auto` prefers a freestiler build with Arrow input over
tippecanoe; an older build is chosen only when tippecanoe is not on PATH (for
example on Windows):

```bash
pip install "ras2cng[freestiler]"
```

The fastest vector path (Arrow input, no GeoJSON step) needs the gpt-cmdr fork
build of freestiler, published as GitHub Release assets rather than on PyPI:

```bash
pip install freestiler==0.3.0.1 \
  --find-links https://github.com/gpt-cmdr/freestiler/releases/expanded_assets/v0.3.0.1-gpt-cmdr
```

Likewise the direct HDF archive path, the native GeoParquet writer and the
COG report need the gpt-cmdr fork build of terrio:

```bash
pip install terrio==0.5.0.1 \
  --find-links https://github.com/gpt-cmdr/terrio/releases/expanded_assets/gpt-cmdr-v0.5.0.1
```

Both are optional and detected at run time; with the PyPI builds ras2cng keeps
its Python paths.

Otherwise, vector tiles use two external CLI tools (not available via pip):

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
- [ras-commander](https://github.com/gpt-cmdr/ras-commander) 0.113.0 or newer (with its `full` extra: ras-commander-fast-hdf 0.3.0 and ras-commander-fast-map 0.12.0) — installed automatically as a dependency
- Core: pandas, geopandas, pyarrow, typer, rich
- Optional: duckdb, sqlalchemy, geoalchemy2, psycopg2-binary, rasterio
