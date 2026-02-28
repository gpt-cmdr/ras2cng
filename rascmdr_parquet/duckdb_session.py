"""DuckDB helpers for querying GeoParquet (including GeoParquet geometry columns)."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import duckdb
import pandas as pd


class DuckSession:
    """DuckDB session with spatial extension pre-loaded when available."""

    def __init__(self, db_path: str = ":memory:"):
        self.con = duckdb.connect(db_path)
        self._load_spatial_extension()

    def _load_spatial_extension(self):
        # Prefer LOAD first (works if extension already installed), then INSTALL.
        try:
            self.con.execute("LOAD spatial;")
            return
        except Exception:
            pass

        try:
            self.con.execute("INSTALL spatial;")
            self.con.execute("LOAD spatial;")
        except Exception as e:
            raise RuntimeError(
                "DuckDB spatial extension could not be loaded. "
                "If you're offline, pre-install extensions or skip spatial queries. "
                f"Underlying error: {e}"
            )

    def register_parquet(self, path: Path, name: str = "_"):
        """Register a (Geo)Parquet file as a view.

        If a GeoParquet 'geometry' column exists (WKB), it will be converted into DuckDB's
        GEOMETRY type in the view so that ST_* functions work.
        """

        p = str(Path(path))

        # Try to wrap geometry column into GEOMETRY type.
        try:
            self.con.execute(
                f"""
                CREATE OR REPLACE VIEW {name} AS
                SELECT * EXCLUDE (geometry),
                       ST_GeomFromWKB(geometry) AS geometry
                FROM read_parquet('{p}');
                """
            )
        except Exception:
            # Fallback for non-GeoParquet or files without geometry column.
            self.con.execute(
                f"CREATE OR REPLACE VIEW {name} AS SELECT * FROM read_parquet('{p}');"
            )
        return self

    def query(self, sql: str) -> pd.DataFrame:
        return self.con.execute(sql).df()

    def close(self):
        self.con.close()


def query_parquet(input_file: Path, sql: str) -> pd.DataFrame:
    session = DuckSession()
    session.register_parquet(input_file)
    try:
        return session.query(sql)
    finally:
        session.close()


def spatial_join(
    left_file: Path,
    right_file: Path,
    predicate: str = "ST_Intersects",
    output_file: Optional[Path] = None,
) -> pd.DataFrame:
    session = DuckSession()
    session.register_parquet(left_file, "l")
    session.register_parquet(right_file, "r")

    sql = f"""
    SELECT l.*, r.*
    FROM l
    JOIN r
    ON {predicate}(l.geometry, r.geometry)
    """

    df = session.query(sql)
    session.close()

    if output_file:
        df.to_parquet(output_file, index=False)

    return df
