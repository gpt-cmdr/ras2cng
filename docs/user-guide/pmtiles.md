# PMTiles Generation

## Overview

ras2cng generates PMTiles archives from GeoParquet (vector) or GeoTIFF (raster) inputs.
PMTiles is a single-file archive format that enables serverless HTTP range requests —
serve flood map tiles directly from S3, Cloudflare R2, or GitHub Pages with no tile server.

## Requirements

Vector tiles need either the `tippecanoe` + `pmtiles` CLIs or the in-process
freestiler tiler (`pip install ras2cng[freestiler]`); `RAS2CNG_VECTOR_TILER`
chooses between them (`auto`, the default, uses tippecanoe when it is on PATH).
See [MapLibre delivery](maplibre.md#vector-tiler). External CLI tools:

| Tool | Source | Purpose |
|---|---|---|
| `tippecanoe` | [felt/tippecanoe](https://github.com/felt/tippecanoe) | GeoParquet/GeoJSON → vector tiles |
| `pmtiles` | [protomaps/go-pmtiles](https://github.com/protomaps/go-pmtiles/releases) | MBTiles → PMTiles conversion |
| `gdal_translate` | [GDAL](https://gdal.org) | GeoTIFF → raster tiles (raster pipeline only) |

## Vector Pipeline (GeoParquet → PMTiles)

```
GeoParquet → GeoJSON (temp) → tippecanoe → PMTiles
GeoParquet → freestiler → PMTiles                      (RAS2CNG_VECTOR_TILER=freestiler)
```

Without `--min-zoom`/`--max-zoom`, tippecanoe guesses the zoom range (`-zg`);
freestiler cannot guess and uses 0-14. Archive bbox covering columns (the
`bbox` struct or the older flat `bbox_*` columns) are dropped before tiling.
`--no-feature-ids` (or `RAS2CNG_VECTOR_TILE_IDS=0`) omits freestiler's MVT
feature IDs; see the MapLibre guide for this and the gzip level.

```bash
ras2cng pmtiles max_depth.parquet flood_depth.pmtiles \
  --layer flood_depth \
  --min-zoom 8 \
  --max-zoom 14
```

## Direct HDF mesh export with Rust

The optional native backend accepts geometry HDF or a computed plan HDF that
contains mesh geometry. It constructs cell rings in Rust, transforms coordinates
with pyproj in one batch, and passes packed geometry to freestiler-core. No
GeoParquet, GeoJSON or per-cell Python geometry objects are required.

```bash
ras2cng pmtiles model.g01.hdf mesh.pmtiles --layer mesh_cells --min-zoom 10 --max-zoom 14 --threads 4
```

Use repeated `--mesh` options to select mesh names. `--source-crs` supplies an
explicit CRS override; otherwise the HDF CRS is required. Default HDF zooms are
10-14. Each feature retains its `mesh_name` and original `cell_id`. Output is
staged beside the destination and atomically published only on success.

This route uses the `ras-commander-fast-hdf` wheel, which ras-commander 0.110.1
and later install by default on Windows x64, Linux x64 and macOS arm64. On
other platforms, `ras2cng[fast-hdf]` requests it explicitly (a source build).
Installing the `freestiler` Python extra alone does not supply the HDF reader. The HDF route
explicitly uses the native backend; `RAS2CNG_VECTOR_TILER` continues to select
backends for GeoParquet/general-layer export.

```python
from ras2cng.pmtiles import generate_mesh_pmtiles

generate_mesh_pmtiles(
    "model.g01.hdf", "mesh.pmtiles", layer_name="mesh_cells",
    min_zoom=10, max_zoom=14, threads=4,
    # Optional result arrays, keyed by mesh, indexed by original cell ID:
    # properties={"LockHaven": {"maximum_depth": depth_array}},
)
```

Cell polygons can collapse at low zoom through tile quantization even with
feature dropping disabled. Qualify decoded coverage and validity for the chosen
zoom range. This geometry export does not calculate hydraulic results.

## Raster Pipeline (GeoTIFF → PMTiles)

```
GeoTIFF → gdal_translate → MBTiles → pmtiles convert → PMTiles      (gdal)
GeoTIFF → terrio dem → terrio warp → terrio PMTiles                  (terrio)
```

`RAS2CNG_RASTER_TILER` chooses the backend for every raster PMTiles writer
(terrain, Stored Maps, calculated layers); see `ras2cng.raster_tiles`.

### Streaming terrio tiles

With a terrio that streams (capability-detected: `terrio.dem` takes
`max_memory_mib` and `hillshade_strength`; gpt-cmdr/terrio #8, #11, #12), no
step holds the raster whole:

1. The colour relief is computed path to path, a strip of rows at a time. For
   terrain, the hillshade is blended in the same pass (`"shaded-relief"`). The
   result matches the old NumPy blend exactly (0 DN difference).
2. The warp to Web Mercator runs in chunks when `terrio.warp` takes
   `max_memory_mib` (#13). Otherwise the relief is warped in memory.
3. The PMTiles pyramid is written a tile row at a time from the warped raster,
   which is opened lazily.

The intermediates are tiled LZW GeoTIFFs in the scratch directory. The tiles
are **byte-identical** to the in-memory path (pinned by
`tests/test_terrio_equivalence.py`). A streaming terrio has no pixel ceiling,
so `RAS2CNG_TERRIO_MAX_PIXELS` only applies to older terrio releases.
`RAS2CNG_TERRIO_MAX_MEMORY_MIB` bounds the dem and warp strips. When it is
unset, terrio uses its defaults of 256 and 64 MiB. A smaller warp chunk can
change a few pixels by 1 DN, as `gdalwarp -wm` does.

Bald Eagle Terrain50 (15,777 × 12,599, z14, hillshaded), peak working set in a
fresh process:

| terrio | wall | peak | output |
|---|---|---|---|
| 0.5.0 (in memory, NumPy hillshade) | 81.7 s | 13,372 MiB | 145.5 MB |
| streaming | 38.7 s | 1,008 MiB | byte-identical |

```bash
ras2cng pmtiles results.tif results.pmtiles
```

Detection is automatic: `.tif` / `.tiff` → raster pipeline; everything else → vector pipeline.

## Python API

```python
from ras2cng.pmtiles import generate_pmtiles_from_input
from pathlib import Path

generate_pmtiles_from_input(
    input_path=Path("max_depth.parquet"),
    output_path=Path("flood_depth.pmtiles"),
    layer_name="flood_depth",
    min_zoom=8,
    max_zoom=14,
)
```

## Serving PMTiles

Once generated, host the `.pmtiles` file on any static file host that supports HTTP range requests:

```javascript
// MapLibre GL JS with pmtiles plugin
import { Protocol } from "pmtiles";
maplibregl.addProtocol("pmtiles", new Protocol().tile);

map.addSource("flood", {
  type: "vector",
  url: "pmtiles://https://your-bucket.s3.amazonaws.com/flood_depth.pmtiles",
});
```
