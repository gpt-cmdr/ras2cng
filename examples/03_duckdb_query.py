"""Example: query GeoParquet outputs with DuckDB.

Requires: pip install "rascmdr-parquet-cli[duckdb]"

Note: ras-commander normalizes result columns to snake_case (e.g. "Maximum Depth" -> maximum_depth).
"""

from pathlib import Path

from rascmdr_parquet import DuckSession

parquet_path = Path("outputs") / "max_depth.parquet"

session = DuckSession().register_parquet(parquet_path)
df = session.query("SELECT COUNT(*) AS n, AVG(maximum_depth) AS avg_depth FROM _")
print(df)
session.close()
