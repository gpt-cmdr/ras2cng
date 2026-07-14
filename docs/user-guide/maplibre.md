# MapLibre Project Bundles

`ras2cng maplibre` converts a completed GeoParquet archive into the compact delivery
bundle used by the RAS Commander Example Project Library. It creates one PMTiles source
for model geometry, an optional second PMTiles source for raw vector results, and a
viewer manifest that keeps the layer controls grouped like RASMapper.

## Required Inputs

The archive must have a valid CRS. Supply an original geometry HDF for every geometry
configuration in the archive:

```bash
ras2cng maplibre ARCHIVE_DIR VIEWER_DIR \
  --geometry-hdf g01=/models/Example.g01.hdf \
  --geometry-hdf g02=/models/Example.g02.hdf \
  --scratch-dir /large-local-scratch
```

The command calls:

```python
HdfProject.get_project_extent(
    geometry_hdf,
    geometry_type="footprint",
    buffer_percent=0,
)
```

This makes the published extent an API-derived model footprint based on the 1D and 2D
model elements, rather than a bounding box or an approximation reconstructed from tiles.
Install ras-commander from current `main`; the released package before the footprint API
will be rejected with a clear error.

For large 2D models, pass `--scratch-dir` on a local volume with substantially more free
space than the system temporary directory. ras2cng writes temporary NDGeoJSON there and
directs Tippecanoe's own workspace beneath it; no source features are simplified or dropped.

Some legacy HDF files omit their projection while the archive's sibling `project.json`
records a verified CRS from packaged projection material. In that case the command uses
the archive CRS. A validated CRS can be supplied explicitly with `--crs`; an unknown CRS
remains a hard error.

## Layer Behavior

- Geometry is delivered as one vector PMTiles file, with source layers for every archive
  sublayer and a `Model Extents` layer for each geometry configuration.
- Only the first geometry group is enabled at startup. Within it, `2D Mesh Cells` is the
  preferred default; for a 1D model the first useful line layer is selected instead.
- Every additional geometry and every other sublayer remains available in the manifest but
  starts hidden.
- Browser delivery is always 2D. A source geometry with a `NaN` Z ordinate is normalized
  to 2D before reprojection so it cannot invalidate a PMTiles layer; the archived
  GeoParquet remains unchanged.
- Dense `2D Mesh Cells` and `2D Mesh Faces` are delivered in a separate PMTiles source
  with a minimum zoom of 13. This preserves full mesh fidelity without forcing a large
  cell layer into the initial overview request.
- `--vector-results` creates a separate source, grouped by plan. Each result is raw HDF
  summary data joined to the matching model feature only for visual delivery. Its manifest
  record identifies its raw HDF source and geometry join key.
- Steady 1D cross-section results are split into one layer per HDF profile. Each layer joins
  to its source cross section on `River`, `Reach`, and `RS`, so a profile selection does not
  create duplicate coincident features. The records remain raw HDF element values, not an
  interpolated water-surface or velocity surface.
- This command does not create interpolated result maps. Generate RASMapper stored maps
  with `RasProcess.store_maps`, publish their COGs, and add those as raster result sources.

## Output

```text
viewer/
├── manifest.json
├── model_extent.geojson
└── tiles/
    ├── geometry.pmtiles
    ├── geometry-detail.pmtiles  # Dense mesh cells/faces, zoom 13+
    └── results.pmtiles       # Only with --vector-results
```

The output directory must be empty. This prevents a failed or partial run from silently
mixing artifacts from different input archives.

## Terrain Publication

Publish terrain after the vector viewer is built. `ras2cng maplibre-terrain` consumes the
archived terrain COG, adds a terrain PMTiles layer to the existing viewer manifest, and
keeps the COG as the numerical source for map identify queries:

```bash
ras2cng maplibre-terrain ARCHIVE_DIR/terrain/Terrain_cog.tif VIEWER_DIR \
  --scratch-dir /large-local-scratch
```

The command creates `viewer/tiles/terrain.pmtiles`, enables the terrain layer by default,
and adds it to the `Terrain` control group. Its display palette is the RASMapper terrain
palette stretched over the source elevation range. The original COG remains unmodified and
is referenced as `sourceCog`, allowing a click to report the original elevation rather than
the colorized tile value.

The display raster is reprojected to Web Mercator only for tiled delivery. Its maximum zoom
is capped at the native terrain cell resolution; `--max-zoom` may lower that cap but cannot
force an upsample. Use an external `--source-cog` href only when the archive layout differs
from the normal sibling `archive/` and `viewer/` directories.
