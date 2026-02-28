"""Example: export Tickfaw results to GeoParquet.

Set env var TICKFAW_MODEL_DIR to the folder containing the plan HDF (*.p??.hdf).

Note: ras-commander normalizes result columns to snake_case.
"""

from pathlib import Path
import os

from rascmdr_parquet import export_results_layer

model_dir = Path(os.environ.get("TICKFAW_MODEL_DIR", r"C:\Tickfaw\Model"))
plan_hdf = next(model_dir.glob("*.p??.hdf"))
mesh_cells = Path("outputs") / "mesh_cells.parquet"

out_dir = Path("outputs")
out_dir.mkdir(exist_ok=True)

export_results_layer(
    plan_hdf,
    out_dir / "max_depth.parquet",
    variable="Maximum Depth",
    geom_file=mesh_cells if mesh_cells.exists() else None,
)

print("Wrote:", out_dir / "max_depth.parquet")
