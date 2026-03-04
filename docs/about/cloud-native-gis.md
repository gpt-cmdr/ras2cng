# Cloud-Native GIS Philosophy

## The Traditional Geospatial Tax

Traditional GIS workflows impose significant overhead on hydraulic model delivery:

- Results must be exported to Shapefile or File GDB
- A GIS analyst must symbolize, project, and publish to a map server
- Users need licensed GIS software to view or query results
- Every analysis requires loading the full dataset into memory

## Cloud-Native Formats Break This Pattern

Cloud-native geospatial formats were designed for the web and cloud environments from the ground up:

**GeoParquet** stores geometry alongside attributes in Apache Parquet's columnar format. Reading
10 columns out of 50 reads only those columns from disk — not the full row. Snappy compression
achieves 5–10x size reduction versus Shapefiles. The Arrow memory layout enables zero-copy
transfer into pandas, DuckDB, and other analytical engines.

**PMTiles** is a single archive file containing all zoom levels of a tile pyramid. HTTP range
requests fetch only the tiles needed for the current viewport. No tile server process needs to
run — serve directly from S3, Cloudflare R2, or a CDN. A flood model result that previously
required a $500/month tile server can be served for pennies.

**DuckDB** is an in-process analytical SQL engine that reads GeoParquet files directly with its
spatial extension. Complex queries across millions of mesh cells run in seconds on a laptop.
No server to provision, no connection pool to manage, no network latency.

## Why This Matters for Flood Modeling

Flood model results are uniquely suited to the cloud-native stack:

1. **Large**: 2D mesh models have 100k–10M cells. Columnar formats make per-column reads fast.
2. **Spatial**: Every result is geometry. PMTiles serves flood extents at web speed.
3. **Queryable**: "Show all cells where depth > 2 ft" is a trivial SQL filter on GeoParquet.
4. **Shareable**: A single `.pmtiles` file on S3 can be embedded in any web map, shared via URL,
   or downloaded without special software.

## The ras2cng Philosophy

ras2cng is the bridge between HEC-RAS simulation outputs and the cloud-native geospatial stack.
It handles the parsing complexity of HEC-RAS HDF files (via ras-commander) and produces
standards-compliant GeoParquet that works everywhere — QGIS, DuckDB, MapLibre, PostGIS,
Observable, R, JavaScript.
