# Geometry Export

## File Type Detection

Detection is suffix-based — no file inspection required:

| Suffix Pattern | Type | Parsers Used |
|---|---|---|
| `*.g01.hdf`, `*.g02.hdf`, ... | HDF geometry | `HdfMesh` (mesh cells), `HdfXsec` (cross sections, centerlines) |
| `*.g01`, `*.g02`, ... | Text geometry | `GeomParser` (cross sections, centerlines) |

## Available Layers

| Layer | Source | Contents |
|---|---|---|
| `mesh_cells` | HDF geometry only | 2D mesh cell polygons (falls back to centroid points if polygons unavailable) |
| `cross_sections` | HDF geometry + text geometry | 1D cross section cut lines |
| `centerlines` | HDF geometry + text geometry | River/reach centerlines |

## Default Behavior

When `--layer` is not specified:
- **HDF geometry** → exports `mesh_cells` (most useful layer for flood depth visualization)
- **Text geometry** → exports `cross_sections`

## CLI Usage

```bash
# HDF geometry — all layers
ras2cng geometry model.g01.hdf mesh_cells.parquet --layer mesh_cells
ras2cng geometry model.g01.hdf cross_sections.parquet --layer cross_sections
ras2cng geometry model.g01.hdf centerlines.parquet --layer centerlines

# Text geometry
ras2cng geometry model.g01 cross_sections.parquet --layer cross_sections
ras2cng geometry model.g01 centerlines.parquet --layer centerlines
```

## Python API

```python
from ras2cng.geometry import export_geometry_layers
from pathlib import Path

# Export specific layer
export_geometry_layers(
    Path("model.g01.hdf"),
    Path("mesh_cells.parquet"),
    layer="mesh_cells",
)

# Export all layers in a loop
for layer in ["mesh_cells", "cross_sections", "centerlines"]:
    try:
        export_geometry_layers(
            Path("model.g01.hdf"),
            Path(f"{layer}.parquet"),
            layer=layer,
        )
    except (FileNotFoundError, ValueError) as e:
        print(f"Skipping {layer}: {e}")
```

## Fallback Behavior

For `mesh_cells`, ras-commander first attempts to extract polygon cell geometries. If the HDF file
does not contain cell polygon data (e.g., older HEC-RAS versions), it falls back to cell centroid
points. The output GeoParquet will have Point geometry instead of Polygon geometry in this case.

## Output Schema

All geometry exports produce a GeoParquet with:
- `geometry` column (WKB-encoded, readable by geopandas, DuckDB spatial, QGIS)
- ras-commander attribute columns in snake_case
- CRS preserved from the HEC-RAS model
