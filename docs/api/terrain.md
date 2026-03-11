# terrain

Terrain discovery and consolidation for HEC-RAS projects.

## Overview

The `terrain` module discovers terrain layers from a HEC-RAS project's rasmap configuration and can consolidate multiple terrain TIFFs into a single merged raster. This is useful for:

- **Inspecting terrain configuration**: Enumerate all terrain layers, their CRS, resolution, and file locations
- **Consolidating terrain**: Merge multiple terrain tiles into a single file for simplified workflows
- **Downsampling**: Reduce terrain resolution for faster mapping or smaller file sizes
- **Creating HEC-RAS terrain HDFs**: Generate new terrain HDF files via RasProcess.exe (required for result mapping)

## How Terrain Discovery Works

1. Reads the project's `.rasmap` file to get terrain names in priority order
2. For each terrain name, locates the corresponding `.hdf` file in the `Terrain/` directory
3. Discovers associated `.tif` files by matching the HDF stem against TIF file names
4. Optionally reads CRS and resolution from TIF files using rasterio

### Terrain Name Matching

TIF files are associated with a terrain by matching the file stem against the terrain name. The matching is case-insensitive and allows suffixes separated by `.`, `_`, or `-`:

| TIF Stem | Terrain Name | Match? |
|----------|-------------|--------|
| `Terrain50` | `Terrain50` | Yes (exact) |
| `Terrain50.muncie_clip` | `Terrain50` | Yes (dot separator) |
| `Terrain50_tile2` | `Terrain50` | Yes (underscore separator) |
| `Terrain50-highres` | `Terrain50` | Yes (dash separator) |
| `Terrain50WithChannel` | `Terrain50` | No (alphanumeric continuation) |

## How Terrain Consolidation Works

1. **Discover** terrain TIFs from rasmap (priority ordered)
2. **Harmonize CRS**: If TIFs have different but equivalent CRS representations, reprojects to match the first TIF's CRS
3. **Merge** via `rasterio.merge.merge(method='first')` — first terrain wins in overlapping areas
4. **Optionally downsample** — reduce resolution by a factor or to a target cell size
5. **Optionally create HEC-RAS terrain HDF** via `RasTerrain.create_terrain_from_rasters()` (requires RasProcess.exe)
6. **Optionally register** the new terrain in the project's rasmap

Steps 5-6 require RasProcess.exe (Windows or Wine). Steps 1-4 are pure Python (rasterio).

## API Reference

::: ras2cng.terrain.TerrainInfo
    options:
      show_source: true

::: ras2cng.terrain.discover_terrains
    options:
      show_source: true

::: ras2cng.terrain.consolidate_terrain
    options:
      show_source: true
