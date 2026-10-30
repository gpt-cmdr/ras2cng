"""Declarative DataFrame contracts published with the ras2cng documentation.

The archive layers inherit many source-specific columns from ras-commander, so these contracts
describe the stable discriminator/join columns and mark the remaining surface as dynamic.
"""

SCHEMA_VERSION = "1.2"

# Columns every archive GeoParquet file carries in addition to its DataFrame
# contract (manifest schema 2.7+). Archives from manifest schema <= 2.6 carry
# flat ``bbox_xmin``/``bbox_ymin``/``bbox_xmax``/``bbox_ymax`` float64 columns
# instead of the ``bbox`` struct.
ARCHIVE_GEOPARQUET_COLUMNS = [
    {
        "name": "bbox",
        "dtype": "struct<xmin: float64, ymin: float64, xmax: float64, ymax: float64>",
        "description": (
            "GeoParquet 1.1 bbox covering of the row geometry (null for null or empty "
            "geometry). Filter with bbox.xmin etc.; manifest schema <= 2.6 archives use "
            "flat bbox_xmin ... bbox_ymax columns."
        ),
    },
    {
        "name": "hilbert_index",
        "dtype": "uint64",
        "description": (
            "Added by spatial post-processing: DuckDB spatial ST_Hilbert of the bbox "
            "centre over the layer extent; rows are ordered by it within each layer. "
            "Null for null or empty geometry."
        ),
    },
]

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
            {
                "name": "cell_id",
                "dtype": "Int64",
                "description": "Nullable integer mesh-cell key (mesh_cells rows); integer keys "
                "are no longer widened to float when layers are merged.",
            },
        ],
        "archive_columns": ARCHIVE_GEOPARQUET_COLUMNS,
    },
    "mesh_cell_results": {
        "description": "Per-cell HEC-RAS result values, with variable-specific value/time columns. Plan files from the direct archive path (manifest 3.0) omit the all-null columns of geometry layers.",
        "accessor": "ras2cng.extract_results_variable_no_geometry(...) or archive_project(..., include_results=True)",
        "source": "ras2cng.results.extract_results_variable_no_geometry",
        "extra_columns": True,
        "dynamic": True,
        "columns": [
            {"name": "mesh_name", "dtype": "str", "description": "HEC-RAS 2D flow-area name."},
            {"name": "cell_id", "dtype": "int", "description": "Zero-based mesh-cell join key."},
        ],
        "archive_columns": ARCHIVE_GEOPARQUET_COLUMNS,
    },
    "mesh_face_results": {
        "description": "Per-face HEC-RAS result values, with variable-specific value/time columns. Plan files from the direct archive path (manifest 3.0) omit the all-null columns of geometry layers.",
        "accessor": "ras2cng.extract_results_variable_no_geometry(...) or archive_project(..., include_results=True)",
        "source": "ras2cng.results.extract_results_variable_no_geometry",
        "extra_columns": True,
        "dynamic": True,
        "columns": [
            {"name": "mesh_name", "dtype": "str", "description": "HEC-RAS 2D flow-area name."},
            {"name": "face_id", "dtype": "int", "description": "Zero-based mesh-face join key."},
        ],
        "archive_columns": ARCHIVE_GEOPARQUET_COLUMNS,
    },
    "project_metadata": {
        "description": "Union of ras-commander project DataFrames written to project metadata Parquet. Cells holding dicts, lists or sets are stored as JSON text.",
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
