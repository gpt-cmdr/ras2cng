# CLI Reference

## ras2cng --help

```
Usage: ras2cng [OPTIONS] COMMAND [ARGS]...

  Export HEC-RAS geometry/results to GeoParquet; query with DuckDB; generate
  PMTiles; sync to PostGIS.

Commands:
  geometry  Export HEC-RAS geometry to GeoParquet.
  results   Export HEC-RAS 2D mesh summary results to GeoParquet.
  query     Query GeoParquet files using DuckDB SQL.
  pmtiles   Generate PMTiles from GeoParquet (vector) or GeoTIFF (raster).
  sync      Sync GeoParquet data to PostGIS.
```

## ras2cng geometry

```
Usage: ras2cng geometry [OPTIONS] GEOM_FILE OUTPUT

  Export HEC-RAS geometry to GeoParquet.

Arguments:
  GEOM_FILE  HEC-RAS geometry file (*.g??) or geometry HDF (*.g??.hdf)
  OUTPUT     Output GeoParquet file path

Options:
  -l, --layer TEXT  Geometry layer: mesh_cells, cross_sections, centerlines
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
