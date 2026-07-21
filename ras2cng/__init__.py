"""
ras2cng: RAS to Cloud Native GIS

An open-source project of CLB Engineering Corporation (https://clbengineering.com/)
GitHub: https://github.com/gpt-cmdr/ras2cng
Docs: https://ras2cng.readthedocs.io
Contact: info@clbengineering.com

Full-project archival and cloud-native export for HEC-RAS.
Exports geometry, results, and terrain to GeoParquet archives with manifest.json catalogs
— ready for DuckDB analytics, PMTiles tile delivery, and PostGIS sync.
Built on ras-commander (https://github.com/gpt-cmdr/ras-commander).
"""

__author__ = "CLB Engineering Corporation"

from ras2cng.geometry import (
    export_geometry_layers,
    export_all_hdf_layers,
    export_all_text_layers,
    merge_all_layers,
    HDF_LAYERS,
    ALL_HDF_LAYERS,
    ALL_TEXT_LAYERS,
)
from ras2cng.results import (
    export_results_layer,
    export_all_variables,
    merge_all_variables,
)
from ras2cng.precipitation import (
    export_precipitation_rasters,
    list_precipitation_timestamps,
    read_precipitation_grid_info,
    PrecipitationExportResult,
    PrecipitationGridInfo,
)
from ras2cng.pmtiles import generate_pmtiles_from_input
from ras2cng.maplibre import (
    package_maplibre_calculated_map,
    package_maplibre_stored_map,
    package_maplibre_terrain,
    package_maplibre_viewer,
    PackageSummary,
    RasterPackageSummary,
    TerrainPackageSummary,
)
from ras2cng.stored_maps import StoredMapImportSummary, import_rasprocess_stored_maps
from ras2cng.viewer_manifest import (
    LEGACY_MAPLIBRE_SCHEMA,
    MAPLIBRE_SCHEMA,
    apply_manifest_v2,
    validate_manifest_v2,
)
from ras2cng.publication import (
    PublicationIssue,
    PublicationReport,
    validate_example_publication,
)
from ras2cng.raster_recipes import (
    RECIPES,
    RasterRecipe,
    RasterRecipeResult,
    get_raster_recipe,
    list_raster_recipes,
    run_raster_recipe,
)
from ras2cng.webgis_service import (
    RASTER_ASSET_SCHEMA,
    STYLE_PRESETS,
    RasterAsset,
    RasterAssetCatalog,
    RasterServiceSettings,
    build_raster_asset_catalog,
    compute_view_statistics,
    create_raster_app,
    render_styled_tile,
)
from ras2cng.project import (
    archive_project,
    inspect_project,
    export_project_metadata,
    ProjectInfo,
    TerrainFileInfo,
)
from ras2cng.catalog import (
    Manifest,
    ManifestLayer,
    ManifestGeomEntry,
    ManifestPlanEntry,
    ManifestMapEntry,
    ManifestTerrainModificationEntry,
    ManifestTerrainSourceEntry,
)
from ras2cng.mapping import (
    DEFAULT_LOCAL_MAP_PERFORMANCE,
    MapResult,
    generate_result_maps,
)
from ras2cng.scaffold import (
    build_scaffold,
    read_plan_hdf_metadata,
    PlanHdfMetadata,
    ScaffoldInfo,
)
from ras2cng.spatial_index import postprocess_archive, postprocess_geoparquet, postprocess_result_table
from ras2cng.terrain import (
    consolidate_terrain,
    consolidate_terrain_files,
    consolidate_project_terrains,
    discover_terrains,
    export_terrain_modifications,
    export_terrain_source_footprints,
    extract_terrain_modification_layers,
    extract_terrain_source_footprints,
    inspect_terrain_sources,
    select_terrain_resolution,
    TerrainInfo,
    TerrainResolutionDecision,
)

__version__ = "0.6.0"

_OPTIONAL_EXPORTS = {
    "DuckSession": ("ras2cng.duckdb_session", "DuckSession", "duckdb"),
    "query_parquet": ("ras2cng.duckdb_session", "query_parquet", "duckdb"),
    "spatial_join": ("ras2cng.duckdb_session", "spatial_join", "duckdb"),
    "sync_to_postgres": ("ras2cng.postgis_sync", "sync_to_postgres", "postgis"),
    "read_from_postgres": ("ras2cng.postgis_sync", "read_from_postgres", "postgis"),
}


def __getattr__(name: str):
    """Load exports backed by optional dependencies only when requested."""

    try:
        module_name, attribute_name, extra = _OPTIONAL_EXPORTS[name]
    except KeyError as error:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from error

    from importlib import import_module

    try:
        value = getattr(import_module(module_name), attribute_name)
    except ModuleNotFoundError as error:
        raise ModuleNotFoundError(
            f"{name} requires the ras2cng[{extra}] optional dependencies"
        ) from error
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_OPTIONAL_EXPORTS))


__all__ = [
    # Geometry
    "export_geometry_layers",
    "export_all_hdf_layers",
    "export_all_text_layers",
    "merge_all_layers",
    "HDF_LAYERS",
    "ALL_HDF_LAYERS",
    "ALL_TEXT_LAYERS",
    # Results
    "export_results_layer",
    "export_all_variables",
    "merge_all_variables",
    "export_precipitation_rasters",
    "list_precipitation_timestamps",
    "read_precipitation_grid_info",
    "PrecipitationExportResult",
    "PrecipitationGridInfo",
    # DuckDB
    "DuckSession",
    "query_parquet",
    "spatial_join",
    # PMTiles
    "generate_pmtiles_from_input",
    "package_maplibre_viewer",
    "package_maplibre_terrain",
    "package_maplibre_stored_map",
    "package_maplibre_calculated_map",
    "PackageSummary",
    "TerrainPackageSummary",
    "RasterPackageSummary",
    "LEGACY_MAPLIBRE_SCHEMA",
    "MAPLIBRE_SCHEMA",
    "apply_manifest_v2",
    "validate_manifest_v2",
    "PublicationIssue",
    "PublicationReport",
    "validate_example_publication",
    "RECIPES",
    "RasterRecipe",
    "RasterRecipeResult",
    "get_raster_recipe",
    "list_raster_recipes",
    "run_raster_recipe",
    "RASTER_ASSET_SCHEMA",
    "STYLE_PRESETS",
    "RasterAsset",
    "RasterAssetCatalog",
    "RasterServiceSettings",
    "build_raster_asset_catalog",
    "compute_view_statistics",
    "create_raster_app",
    "render_styled_tile",
    # PostGIS
    "sync_to_postgres",
    "read_from_postgres",
    # Project
    "archive_project",
    "inspect_project",
    "export_project_metadata",
    "ProjectInfo",
    "TerrainFileInfo",
    # Catalog
    "Manifest",
    "ManifestLayer",
    "ManifestGeomEntry",
    "ManifestPlanEntry",
    "ManifestMapEntry",
    "ManifestTerrainModificationEntry",
    "ManifestTerrainSourceEntry",
    # Mapping
    "generate_result_maps",
    "MapResult",
    "DEFAULT_LOCAL_MAP_PERFORMANCE",
    # Scaffold (map-hdf)
    "build_scaffold",
    "read_plan_hdf_metadata",
    "PlanHdfMetadata",
    "ScaffoldInfo",
    # Spatial indexing
    "postprocess_archive",
    "postprocess_geoparquet",
    "postprocess_result_table",
    # Terrain
    "consolidate_terrain",
    "consolidate_terrain_files",
    "consolidate_project_terrains",
    "discover_terrains",
    "export_terrain_modifications",
    "export_terrain_source_footprints",
    "extract_terrain_modification_layers",
    "extract_terrain_source_footprints",
    "TerrainInfo",
    "TerrainResolutionDecision",
    "inspect_terrain_sources",
    "select_terrain_resolution",
]
