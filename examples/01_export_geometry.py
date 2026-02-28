"""Example: export Tickfaw geometry layers to GeoParquet.

Set env var TICKFAW_MODEL_DIR to the folder containing the RAS project.
"""

from pathlib import Path
import os

from rascmdr_parquet import export_geometry_layers

model_dir = Path(os.environ.get("TICKFAW_MODEL_DIR", r"C:\Tickfaw\Model"))
geom_hdf = next(model_dir.glob("*.g??.hdf"))

out_dir = Path("outputs")
out_dir.mkdir(exist_ok=True)

export_geometry_layers(geom_hdf, out_dir / "mesh_cells.parquet", layer="mesh_cells")
export_geometry_layers(geom_hdf, out_dir / "cross_sections.parquet", layer="cross_sections")
export_geometry_layers(geom_hdf, out_dir / "centerlines.parquet", layer="centerlines")

print("Wrote:", list(out_dir.glob("*.parquet")))
