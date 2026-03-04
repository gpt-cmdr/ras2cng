# 04 — Generate PMTiles

**Notebook**: [examples/04_generate_pmtiles.py](https://github.com/gpt-cmdr/ras2cng/blob/master/examples/04_generate_pmtiles.py)

Generates vector PMTiles from mesh geometry GeoParquet.

## What it demonstrates

- Check for `tippecanoe` and `pmtiles` on PATH
- Generate vector PMTiles from `mesh_cells.parquet`
- Graceful handling when external CLIs are not installed
- CLI equivalent: `ras2cng pmtiles ...`

## Requirements

```bash
# tippecanoe and pmtiles must be on PATH
conda install -c conda-forge tippecanoe pmtiles
# or download binaries from GitHub releases
```

If not available, the notebook prints clear instructions and skips tile generation.

## Run it

```bash
marimo edit examples/04_generate_pmtiles.py
# or
python examples/04_generate_pmtiles.py
```
