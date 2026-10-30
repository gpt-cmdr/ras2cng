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
directs the vector tiler's own workspace beneath it; no source features are dropped.

Some legacy HDF files omit their projection while the archive's sibling `project.json`
records a verified CRS from packaged projection material. In that case the command uses
the archive CRS. A validated CRS can be supplied explicitly with `--crs`; an unknown CRS
remains a hard error.

## Vector Tiler

Vector PMTiles are built by one of two interchangeable backends, chosen with
`RAS2CNG_VECTOR_TILER`:

| Value | Backend | Notes |
|---|---|---|
| `auto` (default) | freestiler when the build has Arrow input; otherwise tippecanoe when `tippecanoe` and `pmtiles` are on PATH; otherwise any freestiler | An older freestiler keeps the previous preference |
| `tippecanoe` | `tippecanoe` + `pmtiles` CLIs | Always available by name. Smaller sparse low-zoom layers |
| `freestiler` | In-process Rust tiler, `pip install ras2cng[freestiler]` | No external tools. Bald Eagle p07 viewer with vector results: 36 s against 1,745 s for tippecanoe. Total size +5% with feature IDs, -10% without; Davis +12% and +3%. One layer of a tileset in memory at a time when the build has `freestile_layers` |

freestiler options (tippecanoe ignores them; its tiles never carry feature IDs):

| Setting | Default | Effect |
|---|---|---|
| `RAS2CNG_VECTOR_TILE_IDS`, CLI `--feature-ids/--no-feature-ids` | on | `0` / `--no-feature-ids` omits MVT feature IDs (`generate_ids=False`). Bald Eagle 87k-cell mesh, z10-15: 1.7 MB smaller (-14%). The ras2cng viewer does not use IDs; keep them if a consumer uses MapLibre `feature-state` without `promoteId` |
| `RAS2CNG_VECTOR_TILE_GZIP_LEVEL` | 6 | Tile gzip level 0-9, when the installed freestiler accepts `gzip_level` (freestiler's own default is 1). Same mesh: -14%, tile contents unchanged |
| `RAS2CNG_FREESTILER_INPUT` | `auto` | `auto` / `arrow` hand layers over as Arrow with no GeoJSON step (`arrow` fails on a build without Arrow input); `geojson` keeps the older path. Decoded tiles are identical on Bald Eagle p07 and Davis |

With Arrow input, mesh cell and face layers that are not joined to results go
from the archive GeoParquet to freestiler in the model CRS and are reprojected
in Rust. Other layers are already reprojected for the viewer metadata and join,
and travel as EPSG:4326 Arrow tables. freestiler refuses CRSs that PROJ shifts by
datum grids (NAD83(HARN), NAD27 and a few others); those layers are reprojected
in Python with pyproj, with a warning, and still skip GeoJSON. A layer with no
CRS is refused. The clip buffer is always `buffer=5` when the build accepts it.

Multi-layer tilesets use `freestiler.freestile_layers` when available, reading
each layer's source only when its turn comes. If that build's
`freestile_layers` has no `gzip_level`, ras2cng warns once and those tilesets
keep freestiler's default compression.

Both keep every feature at every zoom of a tileset and keep shared mesh edges
coincident. Neither ever drops features to cap tile size: thinning a uniform
mesh renders as stripes. Tile weight is bounded by each tileset's minimum zoom
instead (see below). The comparison behind this choice is in
`feature_dev_notes/vector_tiler_evaluation_2026-10-12.md`.

## Raster Tiler

Terrain and result rasters (Stored Maps, calculated layers, `ras2cng pmtiles` on
a GeoTIFF) are colorized, warped to Web Mercator and written as PNG PMTiles by
one of two backends, chosen with `RAS2CNG_RASTER_TILER`:

| Value | Backend | Notes |
|---|---|---|
| `auto` (default) | terrio when installed and the raster fits, otherwise GDAL | terrio ships in the `pmtiles`, `terrio` and `all` extras |
| `terrio` | In-process pure-Rust tiler ([terrio](https://github.com/opengeos/terrio)) | No GDAL or `pmtiles` CLIs; terrain hillshade; whole raster held in memory |
| `gdal` | `gdaldem` → `gdalwarp` → `gdal_translate` → `gdaladdo` → `pmtiles convert` | Streams through files, so any size; no hillshade |

Measured on Bald Eagle Creek rasters through the real packagers:

| Layer | terrio | GDAL chain |
|---|---|---|
| Terrain, 6705 × 4852 px at 20 ft | 6.8 s (hillshaded) | 13.4 s |
| Depth Stored Map | 1.6 s | 4.7 s |

At full resolution terrio's tile colours are within 0.2/255 of the GDAL chain on
average. terrio also fixes two display defects of the GDAL chain:

- **Flood extent holds at every zoom.** Result-layer overviews use `NEAREST`, so
  the visible wet area at z10 is 1.000× the full-resolution area. The GDAL
  chain's `gdaladdo -r average` showed 1.225× at z10. That is the same extent
  growth `ras2cng.cog` prevents in COG overviews.
- **No dark terrain fringe.** The GDAL chain's semi-transparent terrain-edge
  pixels were black at every overview level; terrio's keep the ramp colour.

Terrain overviews use `AVERAGE` (a smooth, fully covered surface).
`RAS2CNG_RASTER_TILE_RESAMPLING=NEAREST|AVERAGE` overrides both kinds. Rasters
larger than `RAS2CNG_TERRIO_MAX_PIXELS` (default 400 million pixels) use the
GDAL chain under `auto`, because terrio processes the raster in memory.

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
  that starts at the *detail floor*: zoom 13, or higher for a fine mesh -- the first zoom
  where the median cell is at least two screen pixels across. Every cell is present at
  every zoom where the layer is drawn. Below the floor, complete mesh tiles are
  multi-megabyte (measured 1.2-1.6 MB at z12 and 4-9 MB at z10), and thinning them to
  fit renders as stripes, so the layer is simply not drawn there.
- `--vector-results` creates separate sources, grouped by plan. Each result is raw HDF
  summary data joined to the matching model feature only for visual delivery. Its manifest
  record identifies its raw HDF source and geometry join key. Results joined to sparse
  features (cross sections, reference lines and points) go in `results.pmtiles` at every
  zoom; results joined to mesh cells or faces are as dense as the mesh and go in
  `results-detail.pmtiles`, which shares the detail floor. Use the published stored-map
  rasters for the overview zooms.
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
    ├── geometry-detail.pmtiles  # Dense mesh cells/faces, from the detail floor (z13+)
    ├── results.pmtiles          # --vector-results joined to sparse features
    └── results-detail.pmtiles   # --vector-results joined to mesh cells/faces, detail floor
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

With the terrio raster tiler the display tiles are **hillshaded** by default: a
multidirectional terrio hillshade is multiplied into the terrain colours, as RAS
Mapper shades terrain. terrio's hillshade is not pixel-identical to `gdaldem`;
that is accepted for a display layer. Identify values still come from the
unshaded COG. `--no-hillshade` publishes flat colour relief, and the manifest
records `hillshade` and `rasterTiler` on the terrain tileset. Hillshaded archives
are larger (Bald Eagle: 6.3 MB against 3.9 MB flat), because shading adds texture.

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
Arrival Time, Duration, Percent Time Inundated, and the inundation boundary. Reserve
`--allow-partial` for explicit diagnostic or exploratory bundles.

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
