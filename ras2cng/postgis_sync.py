"""PostGIS sync helpers."""

from __future__ import annotations

from pathlib import Path

import geopandas as gpd
from sqlalchemy import create_engine, text


def sync_to_postgres(
    input_file: Path,
    postgres_uri: str,
    table_name: str,
    schema: str = "public",
    if_exists: str = "replace",
):
    """Sync a GeoParquet file to a PostGIS table."""

    gdf = gpd.read_parquet(input_file)

    engine = create_engine(postgres_uri)

    # Ensure schema exists
    with engine.begin() as conn:
        conn.execute(text(f"CREATE SCHEMA IF NOT EXISTS {schema}"))

    # Write table
    gdf.to_postgis(
        table_name,
        engine,
        schema=schema,
        if_exists=if_exists,
        index=False,
    )

    # Spatial index
    if "geometry" in gdf.columns:
        idx_name = f"{table_name}_geom_idx"
        with engine.begin() as conn:
            conn.execute(
                text(
                    f'CREATE INDEX IF NOT EXISTS "{idx_name}" ON {schema}."{table_name}" USING GIST (geometry)'
                )
            )


def read_from_postgres(
    postgres_uri: str,
    table_name: str,
    schema: str = "public",
    geometry_column: str = "geometry",
) -> gpd.GeoDataFrame:
    """Read a PostGIS table to a GeoDataFrame."""

    engine = create_engine(postgres_uri)
    sql = f"SELECT * FROM {schema}.\"{table_name}\""
    return gpd.read_postgis(sql, engine, geom_col=geometry_column)
