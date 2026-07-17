# MapLibre Project Bundles

`ras2cng maplibre` converts a completed GeoParquet archive into the compact delivery
bundle used by the RAS Commander Example Project Library. It creates one PMTiles source
for model geometry, an optional second PMTiles source for raw vector results, and a
viewer manifest that keeps the layer controls grouped like RASMapper.

## Viewer Manifest v2

New bundles use `rascommander.maplibre/v2`. The contract separates storage resources,
display layers, navigation, and interaction state:

| Field | Purpose |
| --- | --- |
| `resources` | PMTiles, numeric COGs, and viewer-managed basemaps. A display raster and its numeric COG are separate resources. |
| `layers` | Stable semantic records for style, visibility, provenance, units, query behavior, plan, geometry, and terrain associations. |
| `tree` | Ordered `Features`, `Geometries`, `Results`, `Map Layers`, and `Terrains` hierarchy. |
| `associations` | Explicit plan-to-geometry and geometry-to-terrain links. |
| `legends` | Reusable categorical or continuous legend definitions and fixed/current-view domain policy. |
| `interaction` | The active Identify layer and up to three optional pinned comparison layers. |
| `timeAxes` | Named time axes for time-varying result layers. |
| `provenance` | Generator, source CRS/project, archive schema, and result-value semantics. |

Each plan always contains `Raw Computation Values`, `Published Raster Maps`, and
`Calculated Layers`, even when one branch is empty. Raw HDF layers retain values at
HEC-RAS computation elements and declare that no surface interpolation occurred.
Published raster maps identify RASMapper/RasProcess as the interpolation authority.

The manifest temporarily retains v1 `tilesets` and `groups` fields so deployed viewers
can be upgraded independently. Consumers should treat the v2 fields as authoritative.
Use `ras2cng.apply_manifest_v2()` to upgrade an in-memory v1 manifest and
`ras2cng.validate_manifest_v2()` to reject missing resources, invalid tree references,
or invalid active/pinned layer state.

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
- Native terrain TIFF footprints and terrain-modification construction vectors are delivered
  as queryable vector layers under `Terrains`. Modification lines, polygons, and elevation
  control points preserve their operation metadata; they are not flattened into the final DEM.
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

Raster warps use four GDAL worker threads by default to remain predictable on shared compute
containers. Set `RAS2CNG_GDAL_THREADS` for an isolated worker when a different bounded limit
is appropriate.

## Stored Map Publication

Use the numerical COG created by RASMapper/RasProcess as the authoritative result source.
`maplibre-stored-map` creates a precolored PMTiles display derivative and records both
resources under the associated plan:

```bash
ras2cng maplibre-stored-map maps/p03/Velocity_Max.cog.tif VIEWER_DIR \
  --plan p03 --geometry g03 --map-type Velocity --profile Max \
  --name "Velocity (Max)" --units ft/s \
  --source-cog ../archive/maps/p03/Velocity_Max.cog.tif
```

The PMTiles derivative is the fast default display. Identify reads the numerical COG, not
the colorized pixels. The manifest records `RASMapper/RasProcess` as the interpolation
authority, making the layer distinct from raw HDF computation-element values. Raster nodata
is transparent. Stored Maps are hidden initially unless `--visible` is passed.

For a complete project tranche, use `maplibre-import-stored-maps` with its default
`--require-all` policy. Admission then requires every completed plan to contain Depth,
WSE, Velocity, Froude Number, Shear Stress, Depth x Velocity, Depth x Velocity Squared,
Arrival Time, Duration, and Percent Time Inundated, plus exactly one inundation boundary.
That boundary can be either the native RASMapper Stored Polygon or the strict
ras2cng raster-derived family described below. Reserve `--allow-partial` for explicit
diagnostic or exploratory bundles.

Tranche imports cap precolored display PMTiles at zoom 16 by default. This limits browser
payload and packaging work for very fine or very large result grids without changing the
authoritative numerical COG. Identify and analysis therefore retain the full source fidelity.
Use `--max-zoom` to select a lower display cap; the renderer will not upsample beyond the
native grid resolution.

Use `--domain-policy current-view` only when the deployed WebGIS service supports bounded
window statistics and styled tiles. A precolored PMTiles file cannot be faithfully recolored
because it no longer contains the original scalar values.

Attach service asset IDs after all published paths are final. See the
[Numeric Raster Service](numeric-raster-service.md) guide. Every service-backed continuous
terrain or raster-result layer exposes a **Color Map by Extents** switch plus Dataset and Custom
range modes. The viewer immediately restores the PMTiles dataset view if a statistics or
styled-tile request fails.

## Calculated Layer Publication

Controlled outputs from `raster-calculate` publish under each plan's `Calculated Layers`
branch with `maplibre-calculated-map`. Their provenance records both ras2cng's arithmetic
and RASMapper/RasProcess as the source-surface interpolation authority. See
[Controlled Raster Recipes](raster-recipes.md) for synchronized profile requirements and
the fixed hazard/threshold categories.

An inundation boundary derived from a Depth COG follows the same semantic rule. Prefer
the native RASMapper Stored Polygon. When native generation fails its bounded memory
budget, derive the fallback explicitly:

```bash
ras2cng boundary-from-depth \
  "Depth (Max).cog.tif" \
  "Inundation Boundary (Max).raster-derived.shp" \
  --threshold 0 --resolution 4 --max-edges 5000000 \
  --profile Max --units ft
```

The output is a five-part shapefile plus
`Inundation Boundary (Max).raster-derived.provenance.json`. The importer validates the
complete family and publishes it as `sourceKind: calculated` and
`resultKind: calculated_vector` under `Calculated Layers`. Its provenance identifies the
RASMapper/RasProcess Depth Stored Map as interpolation authority and ras2cng as derivation
authority. It is never labeled as a native RASMapper Stored Polygon.

The derivation is windowed and checks a fixed edge limit before polygonization. It applies
`depth > threshold`, treats nodata and non-finite cells as dry, and uses 4-connectivity.
If the native grid exceeds the edge limit, select a coarser cell size at an even multiple
of the native resolution. Coarsening uses maximum resampling and cannot upsample. Do not
increase the edge limit reflexively: it is the bounded-memory admission control. A plan
directory containing both native and raster-derived boundaries is ambiguous and is
rejected.

## Example Library Publication Gate

Validate a complete archive/viewer pair before catalog admission:

```bash
ras2cng validate-publication VIEWER_DIR/manifest.json ARCHIVE_DIR/manifest.json
ras2cng validate-publication VIEWER_DIR/manifest.json ARCHIVE_DIR/manifest.json \
  --check-http-ranges --json
```

The gate requires manifest v2, a validated CRS, API-derived and initially visible model
extents, valid plan/geometry/terrain associations, raw HDF and Stored Map result families,
numerical COG provenance, and visible terrain for a 2D model. Local COGs are checked for CRS,
tiling, overviews, and nodata/mask behavior; hosted artifacts can be required to return HTTP
`206 Partial Content` for byte-range requests.
