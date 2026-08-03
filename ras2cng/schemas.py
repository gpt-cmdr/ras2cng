"""Declarative DataFrame contracts published with the ras2cng documentation.

The archive layers inherit many source-specific columns from ras-commander, so these contracts
describe the stable discriminator/join columns and mark the remaining surface as dynamic.
"""

SCHEMA_VERSION = "1.0"

DATAFRAME_SCHEMAS = {
    "merged_geometry": {
        "description": "Geometry layers returned by merge_all_layers(), distinguished by layer.",
        "accessor": "ras2cng.merge_all_layers(...) or geometry files written by archive_project()",
        "source": "ras2cng.geometry.merge_all_layers",
        "extra_columns": True,
        "dynamic": True,
        "columns": [
            {"name": "layer", "dtype": "str", "description": "Geometry layer discriminator."},
            {"name": "geometry", "dtype": "GeoSeries", "description": "Layer geometry."},
        ],
    },
    "mesh_cell_results": {
        "description": "Per-cell HEC-RAS result values, with variable-specific value/time columns.",
        "accessor": "ras2cng.extract_results_variable_no_geometry(...) or archive_project(..., include_results=True)",
        "source": "ras2cng.results.extract_results_variable_no_geometry",
        "extra_columns": True,
        "dynamic": True,
        "columns": [
            {"name": "mesh_name", "dtype": "str", "description": "HEC-RAS 2D flow-area name."},
            {"name": "cell_id", "dtype": "int", "description": "Zero-based mesh-cell join key."},
        ],
    },
    "mesh_face_results": {
        "description": "Per-face HEC-RAS result values, with variable-specific value/time columns.",
        "accessor": "ras2cng.extract_results_variable_no_geometry(...) or archive_project(..., include_results=True)",
        "source": "ras2cng.results.extract_results_variable_no_geometry",
        "extra_columns": True,
        "dynamic": True,
        "columns": [
            {"name": "mesh_name", "dtype": "str", "description": "HEC-RAS 2D flow-area name."},
            {"name": "face_id", "dtype": "int", "description": "Zero-based mesh-face join key."},
        ],
    },
    "project_metadata": {
        "description": "Union of ras-commander project DataFrames written to project metadata Parquet.",
        "accessor": "archive_project(...) output <project>.parquet",
        "source": "ras2cng.project.export_project_metadata",
        "extra_columns": True,
        "dynamic": True,
        "columns": [
            {"name": "_table", "dtype": "str", "description": "Source ras-commander DataFrame name."},
        ],
    },
    "duckdb_query_results": {
        "description": "Arbitrary tabular result returned by query_parquet() or DuckSession.query().",
        "accessor": "ras2cng.query_parquet(path, sql)",
        "source": "ras2cng.duckdb_session.query_parquet",
        "extra_columns": True,
        "dynamic": True,
        "columns": [],
    },
}
