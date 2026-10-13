import json
from pathlib import Path
import numpy as np, geopandas as gpd, pandas as pd, shapely
from ras2cng.maplibre import _to_wgs84, _drop_internal_columns
SRC = Path("archive/BaldEagleDamBrk.g01.parquet")
def layer(name, cols):
    g = gpd.read_parquet(SRC, filters=[("layer", "==", name)])
    g = _drop_internal_columns(_to_wgs84(g, SRC))
    return g[[c for c in cols if c in g.columns] + ["geometry"]].reset_index(drop=True)
def write(ds, layers, z0, z1, ids):
    d = Path("data") / ds; d.mkdir(parents=True, exist_ok=True)
    for n, g in layers.items(): g.to_parquet(d / f"{n}.parquet", geometry_encoding="WKB", write_covering_bbox=False)
    (d / "spec.json").write_text(json.dumps({"layers": list(layers), "min_zoom": z0, "max_zoom": z1, "ids": ids}))
    print(ds, {n: len(g) for n, g in layers.items()})
cells = layer("mesh_cells", ["mesh_name", "cell_id"])
rng = np.random.default_rng(0)
c = cells.geometry.centroid
cells["max_depth"] = (np.hypot(c.x - c.x.mean(), c.y - c.y.mean()) * 100 + rng.normal(0, .05, len(cells))).round(4)
write("mesh_cells", {"mesh_cells": cells}, 10, 15, {"mesh_cells": "cell_id"})
faces = layer("mesh_faces", ["mesh_name", "face_id"])
write("mesh_faces", {"mesh_faces": faces}, 12, 15, {"mesh_faces": "face_id"})
sparse = {n: layer(n, ["Name", "mesh_name", "SA-2D", "bc_line_id", "bl_id", "region_id"]) for n in
          ["mesh_areas", "bc_lines", "breaklines", "storage_areas", "mannings_n_regions"]}
write("sparse_multi", sparse, 8, 14, {})
# ~1M-cell synthetic scale test: tile the real mesh in a 4x3 grid
w, h = np.ptp(cells.total_bounds[[0, 2]]), np.ptp(cells.total_bounds[[1, 3]])
parts = []
for i in range(4):
    for j in range(3):
        k = i * 3 + j
        p = cells.copy(); p["geometry"] = shapely.transform(p.geometry.values, lambda a, i=i, j=j: a + [i * w * 1.02, j * h * 1.02])
        p["cell_id"] = p["cell_id"] + k * 1_000_000; parts.append(p)
big = gpd.GeoDataFrame(pd.concat(parts, ignore_index=True), crs=4326)
write("mesh_cells_1m", {"mesh_cells": big}, 10, 14, {"mesh_cells": "cell_id"})
