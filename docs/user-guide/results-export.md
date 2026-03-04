# Results Export

## Overview

Results are exported from HEC-RAS plan HDF files (`*.p??.hdf`). ras-commander's
`HdfResultsMesh.get_mesh_summary_output()` returns one row per 2D mesh cell with summary
statistics for the requested variable.

## Available Variables

Common 2D mesh summary variables (exact names depend on what was computed in the HEC-RAS plan):

| HEC-RAS Variable | Output Column | Description |
|---|---|---|
| `Maximum Depth` | `maximum_depth` | Maximum water depth over simulation period |
| `Maximum Water Surface` | `maximum_water_surface` | Maximum water surface elevation |
| `Maximum Velocity` | `maximum_velocity` | Maximum flow velocity |
| `Maximum Face Velocity` | `maximum_face_velocity` | Maximum velocity at cell faces |
| `Minimum Depth` | `minimum_depth` | Minimum water depth |

Column names are **snake_case** — ras-commander normalizes all variable names.

Use `list_available_summary_variables()` to discover what a specific plan HDF contains:

```python
from ras2cng.results import list_available_summary_variables
variables = list_available_summary_variables(Path("model.p01.hdf"))
print(variables)  # ['Maximum Depth', 'Maximum Water Surface', ...]
```

## Polygon Join

Results from `HdfResultsMesh` are **points** (one per mesh cell centroid). To get polygon geometry
suitable for choropleth maps and area calculations, provide a `--geometry` GeoParquet file:

```bash
ras2cng results model.p01.hdf max_depth.parquet \
  --geometry mesh_cells.parquet \
  --var "Maximum Depth"
```

The merge is performed on `(mesh_name, cell_id)` — both columns must be present in the geometry file.
Result: each row is a mesh cell **polygon** with the hydraulic result attribute attached.

## Export All Variables

```bash
# Export all available variables to a directory
ras2cng results model.p01.hdf ./results/ --all --geometry mesh_cells.parquet
```

Each variable is written to a separate file: `results/maximum_depth.parquet`, etc.

## Python API

```python
from ras2cng.results import export_results_layer, export_all_variables, list_available_summary_variables
from pathlib import Path

# List variables
variables = list_available_summary_variables(Path("model.p01.hdf"))

# Export single variable (points, no geometry join)
export_results_layer(
    plan_hdf=Path("model.p01.hdf"),
    output=Path("max_depth_points.parquet"),
    variable="Maximum Depth",
)

# Export with polygon join
export_results_layer(
    plan_hdf=Path("model.p01.hdf"),
    output=Path("max_depth_polygons.parquet"),
    variable="Maximum Depth",
    geom_file=Path("mesh_cells.parquet"),
)

# Export all variables
exported = export_all_variables(
    plan_hdf=Path("model.p01.hdf"),
    output_dir=Path("./results/"),
    geom_file=Path("mesh_cells.parquet"),
)
print(f"Exported {len(exported)} variables: {exported}")
```
