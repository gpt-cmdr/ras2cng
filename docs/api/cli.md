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
  geometry     Export HEC-RAS geometry to GeoParquet.
  results      Export HEC-RAS 2D mesh summary results to GeoParquet.
  query        Query GeoParquet files using DuckDB SQL.
  pmtiles      Generate PMTiles from GeoParquet (vector) or GeoTIFF (raster).
  sync         Sync GeoParquet data to PostGIS.
  terrain      Consolidate project terrains into a single merged TIFF.
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
  --skip-errors / --fail-fast Skip individual layer errors vs abort
  --no-sort                   Disable Hilbert spatial sorting (on by default)
  --map / --no-map            Generate result rasters via RasStoreMapHelper
  --consolidate-terrain       Merge terrains into single COG
  --render-mode TEXT          Water surface render mode: horizontal, sloping, slopingPretty
  --ras-version TEXT          HEC-RAS version for RasProcess mapping
  --rasprocess PATH           Path to HEC-RAS install directory (for helper deployment)
```

## ras2cng geometry

```
Usage: ras2cng geometry [OPTIONS] GEOM_FILE OUTPUT

  Export HEC-RAS geometry to GeoParquet.

Arguments:
  GEOM_FILE  HEC-RAS geometry file (*.g??) or geometry HDF (*.g??.hdf)
  OUTPUT     Output GeoParquet file path

Options:
  -l, --layer TEXT  Geometry layer: mesh_cells, mesh_areas, cross_sections,
                    centerlines, bc_lines, breaklines, refinement_regions,
                    reference_lines, reference_points, structures, storage_areas
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

  Consolidate project terrains into a single merged TIFF and HEC-RAS terrain HDF.

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
  --arrival-time                Arrival time
  --duration                    Duration
  --recession                   Recession
  --terrain TEXT                 Specific terrain name from rasmap
  --render-mode TEXT            Water surface render mode: horizontal, sloping, slopingPretty
  --ras-version TEXT            HEC-RAS version (e.g. 6.6)
  --rasprocess PATH             Path to HEC-RAS install directory (for helper deployment)
  --min-depth FLOAT             Min depth threshold  [default: 0.0]
  --wgs84                       Reproject output to WGS84
  --cog                         Convert output to Cloud Optimized GeoTIFF
  --timeout INTEGER             Per-plan timeout in seconds  [default: 10800]
  --skip-errors / --fail-fast   Skip errors vs abort
```

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
