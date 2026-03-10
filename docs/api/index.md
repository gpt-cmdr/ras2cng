# API Reference

ras2cng exposes all public functions for programmatic use.

```python
# Package-level imports (convenience)
from ras2cng import (
    # Project archival
    archive_project,
    inspect_project,
    export_project_metadata,
    ProjectInfo,
    # Catalog
    Manifest,
    ManifestLayer,
    ManifestGeomEntry,
    ManifestPlanEntry,
    # Geometry
    export_geometry_layers,
    export_all_hdf_layers,
    export_all_text_layers,
    merge_all_layers,
    HDF_LAYERS,
    ALL_HDF_LAYERS,
    ALL_TEXT_LAYERS,
    # Results
    export_results_layer,
    export_all_variables,
    merge_all_variables,
    # DuckDB
    DuckSession,
    query_parquet,
    spatial_join,
    # PMTiles
    generate_pmtiles_from_input,
    # PostGIS
    sync_to_postgres,
    read_from_postgres,
)

# Module-level imports
from ras2cng.project import archive_project, inspect_project, export_project_metadata, ProjectInfo
from ras2cng.catalog import Manifest, ManifestLayer, ManifestGeomEntry, ManifestPlanEntry
from ras2cng.geometry import export_geometry_layers, merge_all_layers, HDF_LAYERS
from ras2cng.results import export_results_layer, export_all_variables, merge_all_variables
from ras2cng.duckdb_session import DuckSession, query_parquet, spatial_join
from ras2cng.pmtiles import generate_pmtiles_from_input
from ras2cng.postgis_sync import sync_to_postgres, read_from_postgres
```

## Modules

- [CLI](cli.md) — Command-line interface reference (7 commands)
- [geometry](geometry.md) — HDF and text geometry export (10 HDF + 3 text layers)
- [results](results.md) — Plan results export and polygon join
- [duckdb_session](duckdb_session.md) — DuckDB wrapper with spatial extension
- [pmtiles](pmtiles.md) — Vector/raster PMTiles pipeline
- [postgis_sync](postgis_sync.md) — GeoParquet → PostGIS
