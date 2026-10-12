"""Score a PMTiles archive against its source layers."""
import gzip, math, json, sys
from pathlib import Path
import numpy as np, geopandas as gpd, shapely
from shapely.geometry import shape, box
from pmtiles.reader import Reader, MmapSource, all_tiles
import mapbox_vector_tile as mvt
EXT = 4096

def to_tile_px(geom, z, x, y):
    n = 2 ** z
    def f(coords):
        lon, lat = coords[:, 0], np.radians(coords[:, 1])
        px = ((lon + 180) / 360 * n - x) * EXT
        py = ((1 - np.log(np.tan(lat) + 1 / np.cos(lat)) / math.pi) / 2 * n - y) * EXT
        return np.column_stack([px, py])
    return shapely.transform(geom, f)

def analyze(pm, src_dir, spec, id_col_by_layer):
    src = {l: gpd.read_parquet(Path(src_dir) / f"{l}.parquet") for l in spec["layers"]}
    res = {"size_MB": round(Path(pm).stat().st_size / 1e6, 2), "zooms": {}}
    poly_layer = next((l for l, g in src.items() if g.geom_type.isin(["Polygon", "MultiPolygon"]).all() and l in id_col_by_layer), None)
    if poly_layer:
        footprint = shapely.union_all(shapely.make_valid(src[poly_layer].geometry.values))
        idx = src[poly_layer].set_index(id_col_by_layer[poly_layer]).geometry
    with open(pm, "rb") as fh:
        r = Reader(MmapSource(fh))
        layers_seen = {}
        for (z, x, y), data in all_tiles(r.get_bytes):
            raw = gzip.decompress(data) if data[:2] == b"\x1f\x8b" else data
            d = mvt.decode(raw, default_options={"y_coord_down": True})
            zr = res["zooms"].setdefault(z, {"tiles": 0, "tile_KB": [], "ids": {}, "gap_px2": 0.0, "cover_px2": 0.0,
                                             "invalid": 0, "id_mismatch": 0, "checked": 0})
            zr["tiles"] += 1; zr["tile_KB"].append(len(raw) / 1e3)
            for lname, lay in d.items():
                layers_seen[lname] = layers_seen.get(lname, 0) + len(lay["features"])
                idc = id_col_by_layer.get(lname)
                if idc:
                    zr["ids"].setdefault(lname, set()).update(f["properties"].get(idc) for f in lay["features"])
                if lname == poly_layer:
                    geoms = [shape(f["geometry"]) for f in lay["features"]]
                    zr["invalid"] += sum(not g.is_valid for g in geoms)
                    tb = box(0, 0, EXT, EXT)
                    union = shapely.union_all(shapely.make_valid(np.array(geoms, dtype=object))) if geoms else shapely.Polygon()
                    want = to_tile_px(footprint, z, x, y).intersection(tb)
                    # shrink footprint by 2 px so tile-edge / outer-boundary quantisation is not counted as a gap
                    want = want.buffer(-2)
                    zr["gap_px2"] += want.difference(union).area; zr["cover_px2"] += want.area
                    # attribute integrity on a sample: decoded centroid inside the source cell with that id
                    for f, g in list(zip(lay["features"], geoms))[::25]:
                        cid = f["properties"].get(id_col_by_layer[lname])
                        if cid is None or g.is_empty: continue
                        zr["checked"] += 1
                        sp = to_tile_px(idx.get(cid), z, x, y) if cid in idx.index else None
                        if sp is None or not sp.buffer(2).contains(g.representative_point()):
                            zr["id_mismatch"] += 1
    out = {"size_MB": res["size_MB"], "layers_seen": layers_seen, "zooms": {}}
    for z, zr in sorted(res["zooms"].items()):
        kb = np.array(zr["tile_KB"])
        o = {"tiles": zr["tiles"], "max_tile_KB": round(kb.max(), 1), "p95_tile_KB": round(np.percentile(kb, 95), 1)}
        for lname, ids in zr["ids"].items():
            o[f"{lname}_complete_%"] = round(100 * len(ids - {None}) / len(src[lname]), 2)
        if poly_layer:
            o["gap_%"] = round(100 * zr["gap_px2"] / max(zr["cover_px2"], 1), 3)
            o["invalid_polys"] = zr["invalid"]
            o["id_mismatch"] = f'{zr["id_mismatch"]}/{zr["checked"]}'
        out["zooms"][z] = o
    return out

if __name__ == "__main__":
    pm, src_dir = sys.argv[1], sys.argv[2]
    spec = json.loads((Path(src_dir) / "spec.json").read_text())
    print(json.dumps(analyze(pm, src_dir, spec, spec.get("ids", {})), default=str))
