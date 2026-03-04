# Acknowledgments

## ras-commander

ras2cng is built on top of [ras-commander](https://github.com/gpt-cmdr/ras-commander), the
Python library for HEC-RAS automation and data extraction. ras-commander handles all the
complexity of HEC-RAS HDF file parsing, text geometry parsing, and result extraction.

## Cloud-Native Geospatial Stack

ras2cng uses several excellent open-source projects in its pipeline:

- [Apache Parquet / PyArrow](https://arrow.apache.org/) — columnar storage format
- [GeoParquet](https://geoparquet.org/) — geospatial extension to Parquet
- [DuckDB](https://duckdb.org/) — in-process analytical SQL engine
- [PMTiles / Protomaps](https://protomaps.com/) — serverless tile archive format
- [tippecanoe](https://github.com/felt/tippecanoe) — vector tile generation
- [PostGIS](https://postgis.net/) — spatial extension for PostgreSQL
- [GeoPandas](https://geopandas.org/) — geospatial DataFrames for Python
- [Marimo](https://marimo.io/) — reactive Python notebooks

## CLB Engineering

ras2cng is developed and maintained by [CLB Engineering](https://clbengineering.com), a water
resources engineering firm specializing in flood modeling, hydraulic analysis, and cloud-native
geospatial tooling.

**William M. Katzenmeyer, P.E., C.F.M.** — primary developer
