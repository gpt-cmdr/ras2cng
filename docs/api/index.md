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
    # Precipitation
    export_precipitation_rasters,
    list_precipitation_timestamps,
    read_precipitation_grid_info,
    PrecipitationExportResult,
    PrecipitationGridInfo,
    # DuckDB
    DuckSession,
    query_parquet,
    spatial_join,
    # PMTiles
    generate_pmtiles_from_input,
    package_maplibre_viewer,
    package_maplibre_terrain,
    package_maplibre_stored_map,
    package_maplibre_calculated_map,
    validate_example_publication,
    run_raster_recipe,
    build_raster_asset_catalog,
    create_raster_app,
    # PostGIS
    sync_to_postgres,
    read_from_postgres,
    # Mapping (result rasters)
    generate_result_maps,
    MapResult,
    # Scaffold (maps from plan HDF + terrain only)
    build_scaffold,
    read_plan_hdf_metadata,
    PlanHdfMetadata,
    ScaffoldInfo,
    # Terrain
    consolidate_terrain,
    consolidate_project_terrains,
    discover_terrains,
    export_terrain_modifications,
    export_terrain_source_footprints,
    select_terrain_resolution,
    TerrainInfo,
)

# Module-level imports
from ras2cng.project import archive_project, inspect_project, export_project_metadata, ProjectInfo
from ras2cng.catalog import Manifest, ManifestLayer, ManifestGeomEntry, ManifestPlanEntry
from ras2cng.geometry import export_geometry_layers, merge_all_layers, HDF_LAYERS
from ras2cng.results import export_results_layer, export_all_variables, merge_all_variables
from ras2cng.precipitation import export_precipitation_rasters, list_precipitation_timestamps
from ras2cng.duckdb_session import DuckSession, query_parquet, spatial_join
from ras2cng.pmtiles import generate_pmtiles_from_input
from ras2cng.postgis_sync import sync_to_postgres, read_from_postgres
from ras2cng.mapping import generate_result_maps, MapResult
from ras2cng.scaffold import build_scaffold, read_plan_hdf_metadata
from ras2cng.terrain import consolidate_terrain, consolidate_project_terrains, discover_terrains, TerrainInfo
```

## Modules

- [CLI](cli.md) — Command-line interface reference
- [geometry](geometry.md) — HDF and text geometry export (20 HDF + 3 text layers)
- [results](results.md) — Plan results export and polygon join
- [precipitation](precipitation.md) — Gridded precipitation GeoTIFF export from HDF meteorology results
- [mapping](mapping.md) — Result raster generation via RasProcess.exe
- [scaffold](scaffold.md) — Barebones project synthesis from a plan HDF (map-hdf)
- [terrain](terrain.md) — Terrain discovery and consolidation
- [duckdb_session](duckdb_session.md) — DuckDB wrapper with spatial extension
- [pmtiles](pmtiles.md) — Vector/raster PMTiles pipeline
- [maplibre](maplibre.md) — Viewer manifest, raster packaging, and publication validation
- [raster_recipes](raster_recipes.md) — Controlled, unit-aware COG calculations
- [webgis_service](webgis_service.md) — Bounded COG statistics and styled tiles
- [postgis_sync](postgis_sync.md) — GeoParquet → PostGIS
