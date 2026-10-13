"""Render the same tile from each mesh_cells output, cell outlines + fill, to eyeball slivers."""
import gzip, sys
from pathlib import Path
import matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt
from shapely.geometry import shape
from pmtiles.reader import Reader, MmapSource
from pmtiles.tile import zxy_to_tileid
import mapbox_vector_tile as mvt
import geopandas as gpd, math
ds = sys.argv[1] if len(sys.argv) > 1 else "mesh_cells"
tilers = ["tippecanoe-unbounded", "tippecanoe-bounded", "freestiler", "freestiler-drop", "tylertoo-verbatim"]
cells = gpd.read_parquet(f"data/{ds}/mesh_cells.parquet")
lon, lat = cells.geometry.iloc[len(cells)//2].centroid.coords[0]
for z in (10, 12, 14):
    n = 2 ** z; x = int((lon + 180) / 360 * n); y = int((1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n)
    fig, axes = plt.subplots(1, len(tilers), figsize=(5 * len(tilers), 5.4))
    for ax, t in zip(axes, tilers):
        p = Path(f"out/{ds}.{t}.pmtiles"); ax.set_title(f"{t}\nz{z}", fontsize=9); ax.set_axis_off()
        if not p.exists(): continue
        with open(p, "rb") as fh:
            data = Reader(MmapSource(fh)).get(z, x, y)
        if not data: ax.text(.5, .5, "no tile", ha="center"); continue
        raw = gzip.decompress(data) if data[:2] == b"\x1f\x8b" else data
        feats = mvt.decode(raw, default_options={"y_coord_down": True}).get("mesh_cells", {"features": []})["features"]
        g = gpd.GeoSeries([shape(f["geometry"]) for f in feats])
        g.plot(ax=ax, facecolor="#2b6cb0", edgecolor="none")  # gaps show as white
        ax.set_xlim(1024, 2048); ax.set_ylim(2048, 1024)  # central 1/16 of the tile
        ax.set_title(f"{t}\nz{z}  {len(feats)} features", fontsize=9)
    fig.tight_layout(); fig.savefig(f"render_{ds}_z{z}.png", dpi=110); plt.close(fig)
    print(f"render_{ds}_z{z}.png")
