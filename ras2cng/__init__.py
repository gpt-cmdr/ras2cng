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
from ras2cng.duckdb_session import DuckSession, query_parquet, spatial_join
from ras2cng.pmtiles import generate_pmtiles_from_input
from ras2cng.postgis_sync import sync_to_postgres, read_from_postgres
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
)
from ras2cng.mapping import generate_result_maps, MapResult
from ras2cng.terrain import consolidate_terrain, discover_terrains, TerrainInfo

__version__ = "0.4.0"
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
    # DuckDB
    "DuckSession",
    "query_parquet",
    "spatial_join",
    # PMTiles
    "generate_pmtiles_from_input",
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
    # Mapping
    "generate_result_maps",
    "MapResult",
    # Terrain
    "consolidate_terrain",
    "discover_terrains",
    "TerrainInfo",
]
