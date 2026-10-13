"""hms2cng: tippecanoe (via hms2cng.generate_vector_tiles, -zg) vs freestiler, position fidelity."""
import os, gzip, math, json
from pathlib import Path
import numpy as np, geopandas as gpd, shapely
from shapely.geometry import shape
from pmtiles.reader import Reader, MmapSource, all_tiles
import mapbox_vector_tile as mvt
import freestiler
B = Path("bin").resolve()
os.environ["PATH"] = str(B) + os.pathsep + os.environ["PATH"]
from hms2cng.pmtiles import generate_vector_tiles
cases = {"river_bend.subbasins": ("hms/subbasins.parquet", None), "river_bend.reaches": ("hms/reaches.parquet", None),
         "castro.reaches": ("hms/castro.reaches.parquet", 26910), "castro.subbasins": ("hms/castro.subbasins.parquet", 26910)}
def decode(pm):
    out = {}
    with open(pm, "rb") as fh:
        r = Reader(MmapSource(fh)); hdr = r.header()
        for (z, x, y), data in all_tiles(r.get_bytes):
            raw = gzip.decompress(data) if data[:2] == b"\x1f\x8b" else data
            for lname, lay in mvt.decode(raw, default_options={"y_coord_down": True}).items():
                n = 2 ** z
                def f(c):
                    lon = (c[:, 0] / 4096 + x) / n * 360 - 180
                    lat = np.degrees(np.arctan(np.sinh(math.pi * (1 - 2 * (c[:, 1] / 4096 + y) / n))))
                    return np.column_stack([lon, lat])
                for ft in lay["features"]:
                    out.setdefault(z, []).append((ft["properties"].get("name"), shapely.transform(shape(ft["geometry"]), f)))
    return hdr["min_zoom"], hdr["max_zoom"], out
for name, (p, epsg) in cases.items():
    g = gpd.read_parquet(p)
    if epsg: g = g.set_crs(epsg, allow_override=True); p = f"hms/_{name}.parquet"; g.to_parquet(p)
    g4 = g.to_crs(4326); src = dict(zip(g4["name"], g4.geometry))
    runs = {}
    tp = Path(f"out/hms.{name}.tippecanoe.pmtiles"); generate_vector_tiles(Path(p), tp, layer_name="layer"); runs["tippecanoe -zg"] = tp
    z0, z1, _ = decode(tp)
    fp = Path(f"out/hms.{name}.freestiler.pmtiles"); freestiler.freestile(g4, str(fp), layer_name="layer", min_zoom=z0, max_zoom=z1, quiet=True); runs[f"freestiler z{z0}-{z1}"] = fp
    for k, pm in runs.items():
        a, b, dec = decode(pm)
        errs = []
        for z, feats in dec.items():
            if z != b: continue
            for nm, geom in feats:
                if nm in src: errs.append(src[nm].hausdorff_distance(geom) * 111_000)
        print(f"{name:22s} {k:22s} zooms {a}-{b}  features@maxz {len(dec.get(b, []))}/{len(g)}  max position error {max(errs) if errs else float('nan'):.2f} m")
