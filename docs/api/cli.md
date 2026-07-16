# CLI Reference

## ras2cng --help

```
Usage: ras2cng [OPTIONS] COMMAND [ARGS]...

  ras2cng — HEC-RAS to Cloud Native GIS.

  Archive full projects or export individual files to GeoParquet,
  DuckDB, PMTiles, and PostGIS.

Commands:
  inspect      Inspect a HEC-RAS project structure without extracting any data.
  archive      Archive a HEC-RAS project to consolidated GeoParquet files.
  spatial-index Post-process an existing archive with Hilbert sorting and join indexes.
  geometry     Export HEC-RAS geometry to GeoParquet.
  results      Export HEC-RAS 2D mesh summary results to GeoParquet.
  precip       Export gridded precipitation and cumulative precipitation GeoTIFFs.
  query        Query GeoParquet files using DuckDB SQL.
  pmtiles      Generate PMTiles from GeoParquet (vector) or GeoTIFF (raster).
  maplibre     Build a MapLibre PMTiles bundle from a completed ras2cng archive.
  maplibre-terrain  Publish a RAS-styled terrain PMTiles layer into a MapLibre viewer.
  maplibre-stored-map  Publish a queryable RASMapper Stored Map into a MapLibre viewer.
  validate-publication Enforce the Example Library publication contract.
  sync         Sync GeoParquet data to PostGIS.
  terrain      Consolidate one selected named terrain into a merged TIFF.
  map          Generate result rasters (WSE, Depth, Velocity, etc.).
  terrain-mod  Export terrain with modifications as GeoTIFF.
  mannings     Export final Manning's n raster.
```

## ras2cng inspect

```
Usage: ras2cng inspect [OPTIONS] PROJECT

  Inspect a HEC-RAS project structure without extracting any data.

Arguments:
  PROJECT  HEC-RAS project directory or .prj file

Options:
  --json    Output as JSON instead of table
```

## ras2cng archive

```
Usage: ras2cng archive [OPTIONS] PROJECT OUTPUT

  Archive a HEC-RAS project to consolidated GeoParquet files.

  Produces one parquet per geometry file and one per plan, plus a project
  metadata parquet. All layers within each file are distinguished by a
  `layer` column — query with `WHERE layer = 'mesh_cells'`.

  Geometry is exported by default. Results, terrain, and map generation are opt-in.

Arguments:
  PROJECT  HEC-RAS project directory or .prj file
  OUTPUT   Archive output directory (created if needed)

Options:
  --results / --no-results    Include plan results (summary variables)
  --terrain / --no-terrain    Convert terrain TIFFs to Cloud Optimized GeoTIFF
  --plan-geometry             Also extract geometry copy embedded in plan HDF files
  --plans TEXT                Comma-separated plan IDs to include, e.g. p01,p02 (default: all)
  --result-variables TEXT     Comma-separated result summary variables or slugs to include
  --results-layout TEXT       Results output layout: plan or variable
  --results-geometry TEXT     Results geometry mode: polygon, point, or none
  --auxiliary-results / --mesh-results-only
                               Include raw reference, structure, pump, and pipe summaries
  --skip-errors / --fail-fast Skip individual layer errors vs abort
  --no-sort                   Disable Hilbert spatial post-processing (on by default)
  --map / --no-map            Generate result rasters via RasStoreMapHelper
  --consolidate-terrain       Create one authoritative COG per named terrain
  --terrain-resolution TEXT   Explicit named-terrain cell size as NAME=VALUE; repeatable
  --render-mode TEXT          Water surface render mode: horizontal, sloping, slopingPretty
  --ras-version TEXT          HEC-RAS version for RasProcess mapping
  --rasprocess PATH           Path to HEC-RAS install directory (for helper deployment)
```

`archive` runs the same spatial post-processing pass as `spatial-index` by
default. The pass adds `hilbert_index` to GeoParquet geometry, sorts rows by
`layer,hilbert_index`, and preserves GeoParquet bbox `covering` metadata.
Geometryless result tables receive `join_index`; when matching mesh geometry is
available, they also inherit `hilbert_index` by `mesh_name` plus `cell_id` or
`face_id`. Use `--no-sort` for extraction-only runs on memory-constrained
workers, then run `ras2cng spatial-index ARCHIVE_DIR` later.

## ras2cng spatial-index

```
Usage: ras2cng spatial-index [OPTIONS] ARCHIVE_DIR

  Post-process an existing archive with Hilbert sorting and join indexes.

Arguments:
  ARCHIVE_DIR  ras2cng archive directory containing manifest.json

Options:
  --hilbert-level INTEGER     Hilbert curve level  [default: 16]
  --skip-errors / --fail-fast Skip individual parquet errors vs abort
```

`spatial-index` updates the archive in place and rewrites `manifest.json`
schema 2.5 index metadata. Geometry layers record `hilbert_index`, `sort_order`,
and bbox columns. Result variables record join metadata such as `index_column`,
`geometry_filter`, `join_index`, `hilbert_index`, `sort_order`, and
`index_status` (`spatial_join`, `join_key`, `skipped`, or `error`).

## ras2cng geometry

```
Usage: ras2cng geometry [OPTIONS] GEOM_FILE OUTPUT

  Export HEC-RAS geometry to GeoParquet.

Arguments:
  GEOM_FILE  HEC-RAS geometry file (*.g??) or geometry HDF (*.g??.hdf)
  OUTPUT     Output GeoParquet file path

Options:
  -l, --layer TEXT  Geometry layer: mesh_cells, mesh_faces, mesh_areas,
                    cross_sections, centerlines, river_reaches, edge_lines,
                    bank_lines, bc_lines,
                    breaklines, refinement_regions, reference_lines,
                    reference_points, structures, pipe_conduits, pipe_nodes,
                    storage_areas, pump_stations, mannings_n_regions,
                    infiltration_regions
```

## ras2cng results

```
Usage: ras2cng results [OPTIONS] PLAN_HDF OUTPUT

  Export HEC-RAS 2D mesh summary results to GeoParquet.

Arguments:
  PLAN_HDF  HEC-RAS plan HDF file (*.p??.hdf)
  OUTPUT    Output GeoParquet path (or directory when using --all)

Options:
  -g, --geometry PATH   Geometry GeoParquet for spatial join
  -v, --var TEXT        Result variable to export  [default: Maximum Depth]
  --all                 Export all available summary variables to the output directory
```

## ras2cng precip

```
Usage: ras2cng precip [OPTIONS] HDF_FILE OUTPUT

  Export gridded precipitation and cumulative precipitation GeoTIFFs.

Arguments:
  HDF_FILE  HEC-RAS plan or unsteady HDF file containing gridded precipitation
  OUTPUT    Output directory for precipitation GeoTIFFs

Options:
  --source TEXT                 Precipitation source: auto, processed, or imported
                                 [default: auto]
  --timestamps TEXT             Comma-separated timestamp labels or zero-based indices to export
  --incremental / --no-incremental
                                Write per-timestep precipitation rasters
  --cumulative / --no-cumulative
                                Write cumulative-through-timestep precipitation rasters
  --prefix TEXT                 Optional filename prefix
  --no-overwrite                Fail if an output GeoTIFF already exists
```

## ras2cng query

```
Usage: ras2cng query [OPTIONS] INPUT_FILE SQL

  Query GeoParquet files using DuckDB SQL.

Arguments:
  INPUT_FILE  Input GeoParquet file
  SQL         SQL query (use _ as table name)

Options:
  -o, --output PATH  Optional output file (CSV or Parquet)
```

## ras2cng pmtiles

```
Usage: ras2cng pmtiles [OPTIONS] INPUT_FILE OUTPUT

  Generate PMTiles from GeoParquet (vector) or GeoTIFF (raster).

Arguments:
  INPUT_FILE  Input GeoParquet file or GeoTIFF
  OUTPUT      Output PMTiles file path

Options:
  -l, --layer TEXT    Vector tile layer name  [default: layer]
  --min-zoom INTEGER  Minimum zoom
  --max-zoom INTEGER  Maximum zoom
```

## ras2cng maplibre-stored-map

```
Usage: ras2cng maplibre-stored-map [OPTIONS] COG_PATH VIEWER_DIR

  Publish a queryable RASMapper Stored Map under its source plan.

Required options:
  --plan TEXT       Source plan identifier, such as p03
  --map-type TEXT   RASMapper map type, such as Depth or Velocity

Other options:
  --name TEXT                 Layer display name
  --profile TEXT              Profile, summary, or time label
  --geometry TEXT             Associated geometry identifier
  --source-cog TEXT           Public or manifest-relative numeric COG href
  --units TEXT                Result units shown in legends and Identify
  --visible / --hidden        Initial visibility [default: hidden]
  --domain-policy TEXT        fixed or current-view [default: fixed]
  --max-zoom INTEGER          Maximum display zoom, capped by native resolution
  --scratch-dir PATH          Local scratch for bounded raster processing
  --overwrite                 Replace an existing layer/display derivative
```

## ras2cng validate-publication

```
Usage: ras2cng validate-publication [OPTIONS] VIEWER_MANIFEST ARCHIVE_MANIFEST

  Enforce the Example Library catalog-admission contract.

Options:
  --check-files / --manifest-only  Validate local referenced artifacts [default: check]
  --check-http-ranges              Require HTTP 206 for hosted PMTiles/COGs
  --json                           Emit a machine-readable report
```

## ras2cng sync

```
Usage: ras2cng sync [OPTIONS] INPUT_FILE POSTGRES_URI TABLE_NAME

  Sync GeoParquet data to PostGIS.

Arguments:
  INPUT_FILE    Input GeoParquet file
  POSTGRES_URI  PostgreSQL connection URI
  TABLE_NAME    Target table name

Options:
  -s, --schema TEXT    Target schema  [default: public]
  --if-exists TEXT     replace|append|fail  [default: replace]
```

## ras2cng terrain

```
Usage: ras2cng terrain [OPTIONS] PROJECT OUTPUT

  Consolidate one selected named terrain into a merged TIFF and HEC-RAS terrain HDF.

Arguments:
  PROJECT  HEC-RAS project directory or .prj file
  OUTPUT   Output directory for consolidated terrain files

Options:
  --name TEXT           Terrain name  [default: Consolidated]
  --downsample FLOAT    Downsample factor (2.0 = half resolution)
  --resolution FLOAT    Target cell size in project units
  --terrains TEXT       Comma-separated terrain names to include
  --units TEXT          Vertical units: Feet or Meters  [default: Feet]
  --ras-version TEXT    HEC-RAS version  [default: 6.6]
  --tiff-only           Only produce merged TIFF, skip HDF creation
  --no-register         Don't register new terrain in rasmap
```

`terrain` requires the selected TIFFs to belong to one named RASMapper surface. For
Example Library archives, prefer `archive --consolidate-terrain`, which processes each
named terrain independently and records the resolution decision and source inventory.

## ras2cng map

```
Usage: ras2cng map [OPTIONS] PROJECT OUTPUT

  Generate result rasters (WSE, Depth, Velocity, etc.) via RasStoreMapHelper.

  Renders completed plan results to GeoTIFF rasters using the HEC-RAS
  mapping engine via RasStoreMapHelper.exe (bundled with ras-commander).

Arguments:
  PROJECT  HEC-RAS project directory or .prj file
  OUTPUT   Output directory for result rasters

Options:
  --plans TEXT                   Comma-separated plan IDs (default: all with results)
  --profile TEXT                 Max, Min, or timestamp  [default: Max]
  --wse / --no-wse              Water Surface Elevation  [default: on]
  --depth / --no-depth          Depth  [default: on]
  --velocity / --no-velocity    Velocity  [default: on]
  --froude                      Froude number
  --shear-stress                Shear stress
  --dv                          Depth x Velocity
  --dv-sq                       Depth x Velocity²
  --inundation-boundary         Inundation boundary polygon
  --arrival-time                Arrival time (hours, whole-simulation)
  --duration                    Inundation duration (hours)
  --percent-inundated           Percent time inundated
  --arrival-depth FLOAT         Wet/dry depth threshold for arrival/duration/
                                percent-inundated  [default: 0.0]
  --terrain TEXT                 Specific terrain name from rasmap
  --render-mode TEXT            Water surface render mode: horizontal, sloping, slopingPretty
  --ras-version TEXT            HEC-RAS version (e.g. 6.6)
  --rasprocess PATH             Path to HEC-RAS install directory (for helper deployment)
  --min-depth FLOAT             Min depth threshold  [default: 0.0]
  --wgs84                       Reproject output to WGS84
  --cog                         Convert output to Cloud Optimized GeoTIFF
  --timeout INTEGER             Per-plan timeout in seconds  [default: 10800]
  --skip-errors / --fail-fast   Skip errors vs abort
  --keep-postprocessing         Keep the (large) PostProcessing.hdf cache in the output directory
```

Notes on whole-simulation types: `--arrival-time`, `--duration`, and
`--percent-inundated` are computed over the entire simulation (the `--profile`
option does not apply) and their filenames carry the `--arrival-depth`
threshold, e.g. `Arrival Time (0.1ft hrs).tif`. Works with any ras-commander
version: newer versions generate these natively; older versions are handled by
a rasmap pre-injection shim inside ras2cng. `--recession` is accepted but
ignored with a warning — RasMapperLib has no recession map type.

## ras2cng map-hdf

```
Usage: ras2cng map-hdf [OPTIONS] PLAN_HDF OUTPUT

  Generate result rasters from just a plan HDF + terrain (no project needed).

  Synthesizes a barebones HEC-RAS project around the plan HDF (projection,
  units, and plan metadata are read from the HDF itself), builds the HEC-RAS
  terrain from raw GeoTIFF(s) via RasProcess.exe CreateTerrain (or reuses a
  pre-built terrain HDF), then renders stored maps through RASMapper.

Arguments:
  PLAN_HDF  Computed plan results HDF (*.pNN.hdf, any filename)
  OUTPUT    Output directory for result rasters

Options:
  --terrain PATH                Raw terrain GeoTIFF (repeatable; tiles are stitched)
  --terrain-hdf PATH            Pre-built HEC-RAS terrain HDF (its .vrt and tile
                                TIFFs must sit beside it)
  --projection PATH             ESRI .prj projection file (default: read WKT from
                                the plan HDF)
  --workdir PATH                Scaffold directory (default: OUTPUT/_scaffold;
                                reused across reruns)
  --rm-scaffold                 Delete the scaffold directory after the run
  --profile TEXT                Max, Min, or timestamp  [default: Max]
  --wse / --no-wse              Water Surface Elevation  [default: on]
  --depth / --no-depth          Depth  [default: on]
  --velocity / --no-velocity    Velocity  [default: on]
  --froude                      Froude number
  --shear-stress                Shear stress
  --dv                          Depth x Velocity
  --dv-sq                       Depth x Velocity²
  --inundation-boundary         Inundation boundary polygon
  --arrival-time                Arrival time (hours, whole-simulation)
  --duration                    Inundation duration (hours)
  --percent-inundated           Percent time inundated
  --arrival-depth FLOAT         Wet/dry depth threshold for arrival/duration/
                                percent-inundated  [default: 0.0]
  --render-mode TEXT            Water surface render mode  [default: sloping]
  --ras-version TEXT            HEC-RAS version  [default: 6.6]
  --rasprocess PATH             Path to HEC-RAS install directory (for helper deployment)
  --min-depth FLOAT             Min depth threshold  [default: 0.0]
  --wgs84                       Reproject output to WGS84
  --cog                         Convert output to Cloud Optimized GeoTIFF
  --timeout INTEGER             Timeout in seconds  [default: 10800]
  --keep-postprocessing         Keep the (large) PostProcessing.hdf cache in the output directory
```

Examples:

```bash
# Raw terrain TIFF — terrain HDF is built headlessly via RasProcess.exe
ras2cng map-hdf results.p01.hdf ./maps --terrain dem.tif

# Multiple terrain tiles (stitched)
ras2cng map-hdf results.p01.hdf ./maps --terrain dem_a.tif --terrain dem_b.tif

# Pre-built HEC-RAS terrain (skips the terrain build)
ras2cng map-hdf results.p01.hdf ./maps --terrain-hdf Terrain50.hdf
```

Requires a Windows HEC-RAS install (RasMapperLib + bundled GDAL). Exactly one
of `--terrain` / `--terrain-hdf` must be given. The plan HDF must carry a
`Projection` attribute or `--projection` must be supplied.

## ras2cng terrain-mod

```
Usage: ras2cng terrain-mod [OPTIONS] PROJECT OUTPUT

  Export terrain with modifications (channels, levees, etc.) as GeoTIFF.

  Samples the modified terrain surface at full raster resolution via
  RasMapperLib. Requires HEC-RAS 6.6+ and pythonnet (Windows only).

Arguments:
  PROJECT  HEC-RAS project directory or .prj file
  OUTPUT   Output GeoTIFF path

Options:
  -g, --geometry TEXT   Geometry number (e.g. g01). Default: first
  --terrain TEXT        Specific terrain name from rasmap
```

## ras2cng mannings

```
Usage: ras2cng mannings [OPTIONS] PROJECT OUTPUT

  Export final Manning's n raster (base landcover + calibration overrides).

  Produces a full-resolution GeoTIFF of Manning's n values matching the
  land cover raster grid, with all calibration region overrides applied.

Arguments:
  PROJECT  HEC-RAS project directory or .prj file
  OUTPUT   Output GeoTIFF path

Options:
  -g, --geometry TEXT   Geometry number (e.g. g01). Default: first
```

## ras2cng raster-calculate

Runs one allowlisted, unit-aware recipe over aligned numeric COGs. Supply each required
role with repeatable `--input ROLE=PATH`; synchronized recipes also require `--profile`.
See [Controlled Raster Recipes](../user-guide/raster-recipes.md).

## ras2cng maplibre-calculated-map

Packages a `raster-calculate` output as display PMTiles plus an authoritative numeric COG
manifest resource under its plan's `Calculated Layers` branch.

## ras2cng raster-service-catalog

Builds the WebGIS numeric-raster allowlist. `--attach-manifests` writes stable asset IDs,
revisions, and the public service endpoint into manifest v2 bundles.

## ras2cng raster-service

Runs the bounded statistics and styled-tile API. The listener is restricted to loopback and
must be published through a reverse proxy. See
[Numeric Raster Service](../user-guide/numeric-raster-service.md).
