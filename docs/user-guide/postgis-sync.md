# PostGIS Sync

## Overview

ras2cng uploads GeoParquet to PostGIS using SQLAlchemy + GeoAlchemy2, with automatic
GIST spatial index creation after upload.

## Requirements

```bash
pip install "ras2cng[postgis]"
```

A running PostgreSQL + PostGIS instance. The `postgis` extension must be enabled in the target database.

## CLI

```bash
# Basic sync (replace if exists)
ras2cng sync max_depth.parquet "postgresql://user:pass@localhost/mydb" max_depth

# Specify schema
ras2cng sync max_depth.parquet "postgresql://user:pass@localhost/mydb" max_depth \
  --schema hydraulics

# Append to existing table
ras2cng sync max_depth.parquet "postgresql://user:pass@localhost/mydb" max_depth \
  --if-exists append

# Fail if table already exists
ras2cng sync max_depth.parquet "postgresql://user:pass@localhost/mydb" max_depth \
  --if-exists fail
```

## Python API

```python
from ras2cng.postgis_sync import sync_to_postgres, read_from_postgres
from pathlib import Path

# Upload to PostGIS
sync_to_postgres(
    parquet_path=Path("max_depth.parquet"),
    postgres_uri="postgresql://user:pass@localhost/mydb",
    table_name="max_depth",
    schema="hydraulics",
    if_exists="replace",  # "replace", "append", or "fail"
)

# Read back from PostGIS
gdf = read_from_postgres(
    postgres_uri="postgresql://user:pass@localhost/mydb",
    table_name="max_depth",
    schema="hydraulics",
)
```

## Spatial Index

A GIST spatial index is automatically created on the `geometry` column after upload:

```sql
CREATE INDEX ON hydraulics.max_depth USING GIST (geometry);
```

This enables fast spatial queries in PostGIS and downstream tools (QGIS, ArcGIS, etc.).
