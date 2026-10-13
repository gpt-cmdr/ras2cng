import json, subprocess, sys, time
from pathlib import Path
import psutil
PY = sys.executable
MATRIX = {
    "mesh_cells":    ["tippecanoe-unbounded", "tippecanoe-bounded", "freestiler", "freestiler-drop", "tylertoo-verbatim"],
    "mesh_faces":    ["tippecanoe-unbounded", "tippecanoe-bounded", "freestiler", "freestiler-drop", "tylertoo-verbatim"],
    "sparse_multi":  ["tippecanoe-unbounded", "freestiler"],
    "mesh_cells_1m": ["tippecanoe-unbounded", "tippecanoe-bounded", "freestiler", "tylertoo-verbatim"],
}
Path("out").mkdir(exist_ok=True)
results = json.loads(Path("results.json").read_text()) if Path("results.json").exists() else {}
for ds, tilers in MATRIX.items():
    for t in tilers:
        key = f"{ds}|{t}"
        if key in results: continue
        out = Path("out") / f"{ds}.{t}.pmtiles"; out.unlink(missing_ok=True)
        p = psutil.Popen([PY, "run_one.py", t, ds, str(out)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        peak = 0; t0 = time.time()
        while p.poll() is None:
            try: peak = max(peak, sum(c.memory_info().rss for c in [p] + p.children(recursive=True)))
            except psutil.Error: pass
            time.sleep(0.1)
        so, se = p.communicate()
        r = {"wall_s": round(time.time() - t0, 1), "peak_MB": round(peak / 1e6), "rc": p.returncode}
        if p.returncode: r["err"] = se[-1500:]
        else:
            r["tile_s"] = round(json.loads(so.strip().splitlines()[-1])["seconds"], 1)
            a = subprocess.run([PY, "analyze.py", str(out), f"data/{ds}"], capture_output=True, text=True)
            r.update(json.loads(a.stdout) if a.returncode == 0 else {"analyze_err": a.stderr[-1500:]})
        results[key] = r
        Path("results.json").write_text(json.dumps(results, indent=1, default=str))
        print(key, {k: r.get(k) for k in ("rc", "wall_s", "tile_s", "peak_MB", "size_MB")}, flush=True)
