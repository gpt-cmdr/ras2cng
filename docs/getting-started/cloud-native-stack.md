# Cloud-Native GIS Stack

## The Problem with Traditional HEC-RAS Workflows

Traditional HEC-RAS result delivery relies on:

- **Shapefiles** — row-oriented, split into multiple files, no compression, 10-character column names
- **File Geodatabases** — proprietary format, requires Esri software to read
- **WMS/WFS servers** — require always-on server infrastructure
- **Desktop GIS** — spatial analysis locked to licensed desktop software

## The Cloud-Native Solution

ras2cng converts HEC-RAS outputs to open, cloud-optimized formats:

### GeoParquet
- **Format**: Apache Parquet + GeoArrow geometry encoding
- **Benefits**: Columnar storage (read only the columns you need), snappy compression (5–10x smaller than Shapefile), Arrow-native (zero-copy into pandas/DuckDB)
- **Access**: Any language with Parquet support — Python, R, JavaScript, SQL

### DuckDB
- **Format**: In-process analytical SQL engine
- **Benefits**: Reads GeoParquet directly without loading into memory, spatial extension for geometry operations, runs in serverless/embedded contexts
- **Access**: `ras2cng query`, Python `DuckSession`, or any DuckDB client

### PMTiles
- **Format**: Single-file archive of map tiles (Protomaps)
- **Benefits**: Serverless HTTP range requests — serve from S3, R2, or GitHub Pages with no tile server
- **Access**: MapLibre GL JS, Leaflet with pmtiles plugin, any HTTP range-capable client

### PostGIS
- **Format**: PostgreSQL + spatial extensions
- **Benefits**: Enterprise-grade, multi-user, GIST spatial index, full SQL with geometry operations
- **Access**: QGIS, ArcGIS, any PostgreSQL client, SQLAlchemy

## Data Flow

```
HEC-RAS project
├── *.g01.hdf  (HDF geometry)
├── *.g01      (text geometry)
└── *.p01.hdf  (plan results)
        │
        ▼
ras2cng geometry / results
        │
        ▼
GeoParquet (.parquet)          ← universal intermediate
    ├── DuckDB queries          ← serverless analytics
    ├── PMTiles generation      ← serverless map tiles
    └── PostGIS sync            ← enterprise database
```

## Why This Matters for Flood Modeling

Flood model results are large (millions of mesh cells), spatial (polygon geometry), and frequently
queried (depth > threshold, area statistics). The cloud-native stack handles all three:

- **Large**: GeoParquet reads only requested columns; DuckDB streams without loading everything to RAM
- **Spatial**: PMTiles serves flood extents to web maps; PostGIS enables spatial joins with other datasets
- **Queryable**: DuckDB SQL filters mesh cells by depth threshold in milliseconds without a GIS application
