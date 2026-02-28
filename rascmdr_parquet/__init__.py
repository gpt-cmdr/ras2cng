"""
rascmdr-parquet: CLI for HEC-RAS to GeoParquet/PMTiles export
"""
from rascmdr_parquet.geometry import export_geometry_layers
from rascmdr_parquet.results import export_results_layer, export_all_variables
from rascmdr_parquet.duckdb_session import DuckSession, query_parquet, spatial_join
from rascmdr_parquet.pmtiles import generate_pmtiles_from_input
from rascmdr_parquet.postgis_sync import sync_to_postgres, read_from_postgres

__version__ = "0.1.0"
__all__ = [
    "export_geometry_layers",
    "export_results_layer",
    "export_all_variables",
    "DuckSession",
    "query_parquet",
    "spatial_join",
    "generate_pmtiles_from_input",
    "sync_to_postgres",
    "read_from_postgres",
]
