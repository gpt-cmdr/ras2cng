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
  --geometry-hdf g02=/models/Example.g02.hdf
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

## Layer Behavior

- Geometry is delivered as one vector PMTiles file, with source layers for every archive
  sublayer and a `Model Extents` layer for each geometry configuration.
- Only the first geometry group is enabled at startup. Within it, `2D Mesh Cells` is the
  preferred default; for a 1D model the first useful line layer is selected instead.
- Every additional geometry and every other sublayer remains available in the manifest but
  starts hidden.
- `--vector-results` creates a separate source, grouped by plan. Each result is raw HDF
  summary data joined to the matching model feature only for visual delivery. Its manifest
  record identifies its raw HDF source and geometry join key.
- This command does not create interpolated result maps. Generate RASMapper stored maps
  with `RasProcess.store_maps`, publish their COGs, and add those as raster result sources.

## Output

```text
viewer/
├── manifest.json
├── model_extent.geojson
└── tiles/
    ├── geometry.pmtiles
    └── results.pmtiles       # Only with --vector-results
```

The output directory must be empty. This prevents a failed or partial run from silently
mixing artifacts from different input archives.
