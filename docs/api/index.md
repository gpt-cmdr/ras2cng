# API Reference

ras2cng exposes all public functions for programmatic use.

```python
# Package-level imports (convenience)
from ras2cng import (
    export_geometry_layers,
    export_results_layer,
    export_all_variables,
    DuckSession,
    query_parquet,
    spatial_join,
    generate_pmtiles_from_input,
    sync_to_postgres,
    read_from_postgres,
)

# Module-level imports
from ras2cng.geometry import export_geometry_layers
from ras2cng.results import export_results_layer, export_all_variables, list_available_summary_variables
from ras2cng.duckdb_session import DuckSession, query_parquet, spatial_join
from ras2cng.pmtiles import generate_pmtiles_from_input
from ras2cng.postgis_sync import sync_to_postgres, read_from_postgres
```

## Modules

- [geometry](geometry.md) — HDF and text geometry export
- [results](results.md) — Plan results export and polygon join
- [duckdb_session](duckdb_session.md) — DuckDB wrapper with spatial extension
- [pmtiles](pmtiles.md) — Vector/raster PMTiles pipeline
- [postgis_sync](postgis_sync.md) — GeoParquet → PostGIS
- [CLI](cli.md) — Command-line interface reference
