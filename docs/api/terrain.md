# terrain

Terrain discovery and consolidation for HEC-RAS projects.

## Overview

The `terrain` module discovers named terrain layers from a HEC-RAS project's rasmap configuration and consolidates each surface's TIFF members independently. This is useful for:

- **Inspecting terrain configuration**: Enumerate all terrain layers, their CRS, resolution, and file locations
- **Consolidating terrain**: Merge the TIFF members of each named terrain into its own authoritative file
- **Downsampling without upsampling**: Select a whole native-cell multiple with a 5 ft publication floor
- **Recovering relocated projects**: Consolidate an explicit, priority-ordered TIFF list when stored RASMapper paths cannot be resolved on the processing host
- **Publishing source construction**: Export source TIFF footprints and terrain-modification vectors
- **Creating HEC-RAS terrain HDFs**: Generate new terrain HDF files via RasProcess.exe (required for result mapping)
- **Native modification-aware export**: Ask RAS Mapper to consolidate a registered terrain into one validated GeoTIFF while preserving source priority, stitches, masks, and vector modifications

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
2. **Keep terrain names separate**: different named RASMapper terrains are never merged implicitly
3. **Choose a target grid**: preserve native resolution at or above 5 ft; otherwise use the smallest whole native-cell multiple at or above 5 ft. A mixed-resolution mosaic requires an explicit target that is a whole multiple of its coarsest native grid; every source factor is retained in provenance.
4. **Merge by windows**: reproject each member to the target grid and let the first RASMapper source win in overlaps without allocating the full mosaic in memory
5. **Optionally create HEC-RAS terrain HDF** via `RasTerrain.create_terrain_from_rasters()` (requires RasProcess.exe)
6. **Optionally register** the new terrain in the project's rasmap

Steps 5-6 require RasProcess.exe (Windows or Wine). Steps 1-4 are pure Python (rasterio).

## Native Registered-Terrain Export

`export_modified_terrain()` is the production path for a single,
modification-aware terrain TIFF. It delegates to
`RasTerrain.export_rasmapper_terrain()` rather than selecting the first source
TIFF or reconstructing terrain-modification mathematics in Python. RAS Mapper
performs the consolidation and nearest-neighbor resampling on the authoritative
source grid. Exact source-derived scale factors are `1`, `2`, `4`, and `8`.

The output is committed only after ras-commander semantically validates it; a
JSON receipt is written beside the TIFF. Existing output and receipt files are
protected unless `overwrite=True` (or `--overwrite` in the CLI).

The optional `geometry` argument is deprecated and ignored because native
registered-terrain export does not consume a geometry HDF. `terrain_name` may
be omitted only when the project registers exactly one terrain; ambiguous
projects must select the exact RAS Mapper terrain name. The compatibility
argument is scheduled for removal in ras2cng 1.1.

This path requires a ras-commander release that exposes
`RasTerrain.export_rasmapper_terrain()`. An older installation receives an
explicit upgrade error instead of falling back to the numerically different
row sampler.

Qualified runtimes are HEC-RAS 6.4.1, 6.5, 6.6, and 7.0.1 on native Windows
and Wine. HEC-RAS 6.3, 6.4.0, 6.7 beta builds, and 7.0.0 are rejected. Stable
7.1 is forward-open: ras-commander will accept it only when the installed
binary satisfies the exact managed API contract.

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

::: ras2cng.terrain.consolidate_terrain_files
    options:
      show_source: true

::: ras2cng.terrain.consolidate_project_terrains
    options:
      show_source: true

::: ras2cng.terrain.export_modified_terrain
    options:
      show_source: true

::: ras2cng.terrain.extract_terrain_source_footprints
    options:
      show_source: true

::: ras2cng.terrain.extract_terrain_modification_layers
    options:
      show_source: true
