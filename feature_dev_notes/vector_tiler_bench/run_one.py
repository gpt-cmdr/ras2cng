"""Run one tiler on one prepared dataset. Usage: run_one.py TILER DATASET OUT"""
import os, sys, time, json
from pathlib import Path
import geopandas as gpd
HERE = Path(__file__).parent
os.environ["RAS2CNG_TIPPECANOE"] = str(HERE / "bin" / "tippecanoe.exe")
os.environ["RAS2CNG_PMTILES"] = str(HERE / "bin" / "pmtiles.exe")
tiler, dataset, out = sys.argv[1], sys.argv[2], Path(sys.argv[3])
spec = json.loads((HERE / "data" / dataset / "spec.json").read_text())
Z0, Z1 = spec["min_zoom"], spec["max_zoom"]
layers = {name: HERE / "data" / dataset / f"{name}.parquet" for name in spec["layers"]}
t0 = time.perf_counter()
if tiler.startswith("tippecanoe"):
    from ras2cng import maplibre as m
    srcs = []
    for name, p in layers.items():
        nd = out.parent / f"{out.stem}.{name}.ndgeojson"
        m._write_ndgeojson(gpd.read_parquet(p), nd)
        srcs.append((name, nd))
    m._run_tippecanoe(out, srcs, Z0, Z1, out.parent / f"{out.stem}.tmp", bounded=(tiler == "tippecanoe-bounded"))
    for _, nd in srcs: nd.unlink()
elif tiler.startswith("freestiler"):
    import freestiler
    kw = {"drop_rate": 2.5} if tiler == "freestiler-drop" else {}
    gdfs = {name: gpd.read_parquet(p) for name, p in layers.items()}
    freestiler.freestile(gdfs if len(gdfs) > 1 else next(iter(gdfs.values())), str(out),
                         layer_name=None if len(gdfs) > 1 else next(iter(gdfs)),
                         min_zoom=Z0, max_zoom=Z1, quiet=True, **kw)
elif tiler == "tylertoo-verbatim":
    import tylertoo
    assert len(layers) == 1, "tylertoo python API is single-layer"
    name, p = next(iter(layers.items()))
    ov = out.with_suffix(".ov.parquet")
    tylertoo.overview(str(p), str(ov), min_zoom=Z0, max_zoom=Z1, polygon_thinning=0.0, line_thinning=0.0,
                      polygon_visibility=0.0, line_visibility=0.0, simplify_factor=0.0, density_drop=False)
    tylertoo.export_pmtiles(str(ov), str(out), layer_name=name, tile_size_limit=None)
    ov.unlink()
else:
    raise SystemExit(f"unknown tiler {tiler}")
print(json.dumps({"seconds": time.perf_counter() - t0}))
