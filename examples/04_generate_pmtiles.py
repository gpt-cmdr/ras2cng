"""Example: generate PMTiles from a GeoParquet file.

Requires:
- tippecanoe on PATH
"""

from pathlib import Path

from rascmdr_parquet import generate_pmtiles_from_input

in_path = Path("outputs") / "max_depth.parquet"
out_path = Path("outputs") / "max_depth.pmtiles"

generate_pmtiles_from_input(in_path, out_path, layer_name="max_depth", min_zoom=8, max_zoom=16)
print("Wrote:", out_path)
