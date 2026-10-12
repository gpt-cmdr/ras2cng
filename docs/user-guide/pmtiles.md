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
freestiler cannot guess and uses 0-14.

```bash
ras2cng pmtiles max_depth.parquet flood_depth.pmtiles \
  --layer flood_depth \
  --min-zoom 8 \
  --max-zoom 14
```

## Raster Pipeline (GeoTIFF → PMTiles)

```
GeoTIFF → gdal_translate → MBTiles → pmtiles convert → PMTiles
```

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
