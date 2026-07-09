# scaffold

Barebones HEC-RAS project synthesis from a computed plan HDF — powers the
`map-hdf` command.

## Overview

RASMapper's stored-map engine only consumes a `.rasmap` file and a plan HDF,
and the plan HDF carries everything a project scaffold needs: projection WKT
(root `Projection` attribute), unit system, plan/project titles, Plan ShortID,
and a full geometry copy. The `scaffold` module exploits this to generate maps
from **minimal inputs** — a plan HDF plus a terrain raster — with no original
HEC-RAS project on disk.

`build_scaffold()` synthesizes:

- `{Project}.prj` / `{Project}.pNN` — minimal text stubs (~11 lines total)
- `{Project}.uNN` / `{Project}.gNN` — 1-line stubs (silence missing-file log noise)
- `{Project}.rasmap` — projection + terrain layer + empty Results element
- `Terrain\Projection.prj` — ESRI WKT extracted from the plan HDF (kept inside
  `Terrain\` so it never collides with the HEC-RAS project `.prj`)
- `Terrain\*.hdf/.vrt/.tif` — built headlessly from raw GeoTIFFs via
  `RasProcess.exe CreateTerrain` (`RasTerrain.create_terrain_from_rasters`),
  or imported from a pre-built terrain HDF sidecar set

The plan HDF itself is hardlinked (same volume) or copied under its canonical
`{Project}.pNN.hdf` name — the input file may have any name; project name and
plan number are recovered from `Plan Data/Plan Information` attributes.

Scaffolds carry a `.ras2cng-scaffold` marker and are reused across runs when
the source HDF is unchanged, so the expensive terrain build happens once.

## Usage

```python
from ras2cng import build_scaffold, read_plan_hdf_metadata
from ras2cng.mapping import generate_result_maps

meta = read_plan_hdf_metadata("results_renamed.hdf")
print(meta.project_name, meta.plan_number, meta.plan_short_id)

info = build_scaffold(
    "results_renamed.hdf",
    "workdir/",
    terrain_tifs=["dem.tif"],          # or terrain_hdf="Terrain50.hdf"
    ras_version="6.6",
)
generate_result_maps(info.prj_file, "maps/", plans=[f"p{info.meta.plan_number}"])
```

## Requirements

- Windows HEC-RAS install (RasMapperLib + bundled GDAL) — the one dependency
  that cannot be synthesized
- The plan HDF must be computed (`Results` group present) and carry a
  `Projection` attribute, or pass `projection_file=` explicitly
- Pre-built terrain sidecar sets must be complete: the `.hdf`, its sibling
  `.vrt`, and every tile TIFF referenced by the HDF's `/Terrain` group

## API Reference

::: ras2cng.scaffold.PlanHdfMetadata
    options:
      show_source: true

::: ras2cng.scaffold.ScaffoldInfo
    options:
      show_source: true

::: ras2cng.scaffold.read_plan_hdf_metadata
    options:
      show_source: true

::: ras2cng.scaffold.build_scaffold
    options:
      show_source: true

::: ras2cng.scaffold.terrain_sidecar_files
    options:
      show_source: true
