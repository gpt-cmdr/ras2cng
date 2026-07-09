# 07 — Result Maps

**Notebook**: [examples/07_result_maps.py](https://github.com/gpt-cmdr/ras2cng/blob/master/examples/07_result_maps.py)

Rendered result rasters via the HEC-RAS mapping engine — both from a full
project (`map`) and from **minimal inputs**: a plan HDF + terrain raster with
no project at all (`map-hdf`).

## What it demonstrates

1. `read_plan_hdf_metadata()` — everything the plan HDF carries (projection
   WKT, units, plan ShortID, titles) that makes project-free mapping possible
2. `generate_result_maps()` on a full project — WSE/Depth at the Max profile
3. `build_scaffold()` + `generate_result_maps()` — the `map-hdf` path:
   synthesize a barebones project around the HDF, build the terrain headlessly
   from a raw DEM GeoTIFF via `RasProcess.exe CreateTerrain`
4. Whole-simulation map types — Arrival Time, Duration, Percent Time Inundated
   with an `arrival_depth` wet/dry threshold
5. Inspecting output rasters with rasterio

## Run it

```bash
marimo edit examples/07_result_maps.py
# or
python examples/07_result_maps.py
```

## CLI equivalents

```bash
# Full project
ras2cng map path/to/project ./maps --render-mode sloping

# Plan HDF + raw terrain TIFF only (no project needed)
ras2cng map-hdf results.p07.hdf ./maps --terrain dem.tif

# Pre-built HEC-RAS terrain (skips the terrain build)
ras2cng map-hdf results.p07.hdf ./maps --terrain-hdf Terrain50.hdf

# Whole-simulation products at a 0.5 ft wet/dry threshold
ras2cng map-hdf results.p07.hdf ./maps --terrain dem.tif ^
    --no-wse --no-depth --no-velocity ^
    --arrival-time --duration --percent-inundated --arrival-depth 0.5
```

## Requirements & notes

- **Windows HEC-RAS install** (6.6 recommended) — the mapping engine is
  RasMapperLib; ras2cng drives it through ras-commander's bundled
  `RasStoreMapHelper.exe`
- **A computed plan HDF.** `BaldEagleCrkMulti2D` ships without results — the
  notebook has a `RUN_MODEL` flag to compute plan 07 first (several minutes),
  or point `PLAN_HDF` at any computed plan HDF you already have. The filename
  does not need to match the project convention; project name and plan number
  are read from HDF attributes.
- Whole-simulation types ignore `--profile` and label outputs by threshold,
  e.g. `Arrival Time (0.5ft hrs).tif`. They also trigger a large
  `PostProcessing.hdf` cache, which ras2cng removes from the output directory
  unless `--keep-postprocessing` is passed.
- `--recession` is accepted but ignored with a warning — RasMapperLib has no
  recession map type.
- The scaffold directory (`OUTPUT/_scaffold` by default) is reused across
  reruns so the terrain build happens once; pass `--rm-scaffold` to delete it.

## Expected output structure

```
out/07_result_maps/
├── from_project/p07/
│   ├── WSE (Max).Terrain50.baldeagledem.tif
│   └── Depth (Max).Terrain50.baldeagledem.tif
├── from_hdf_only/p07/            # minimal-inputs path (map-hdf)
└── whole_simulation/p07/
    ├── Arrival Time (0.5ft hrs).Terrain50.baldeagledem.tif
    ├── Duration (0.5ft hrs).Terrain50.baldeagledem.tif
    └── Percent Time Inundated (0.5ft).Terrain50.baldeagledem.tif
```
