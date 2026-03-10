# Geometry Export

## File Type Detection

Detection is suffix-based — no file inspection required:

| Suffix Pattern | Type | Parsers Used |
|---|---|---|
| `*.g01.hdf`, `*.g02.hdf`, ... | HDF geometry | `HdfMesh` (mesh cells), `HdfXsec` (cross sections, centerlines) |
| `*.g01`, `*.g02`, ... | Text geometry | `GeomParser` (cross sections, centerlines) |

## Available Layers

### HDF geometry layers (`.g##.hdf`)

| Layer | Geometry | Source Class |
|---|---|---|
| `mesh_cells` | Polygon (Point fallback) | `HdfMesh` |
| `mesh_areas` | Polygon | `HdfMesh` |
| `cross_sections` | LineString | `HdfXsec` |
| `centerlines` | LineString | `HdfXsec` |
| `bc_lines` | LineString | `HdfBndry` |
| `breaklines` | LineString | `HdfBndry` |
| `refinement_regions` | Polygon | `HdfBndry` |
| `reference_lines` | LineString | `HdfBndry` |
| `reference_points` | Point | `HdfBndry` |
| `structures` | LineString | `HdfStruc` |

### Text geometry layers (`.g##`)

| Layer | Geometry | Source Class |
|---|---|---|
| `cross_sections` | LineString | `GeomParser` |
| `centerlines` | LineString | `GeomParser` |
| `storage_areas` | Polygon | `GeomStorage` |

See [Geometry Layers Reference](geometry-layers.md) for full details on each layer.

## Default Behavior

When `--layer` is not specified:
- **HDF geometry** → exports `mesh_cells` (most useful layer for flood depth visualization)
- **Text geometry** → exports `cross_sections`

## CLI Usage

```bash
# HDF geometry — specific layers
ras2cng geometry model.g01.hdf mesh_cells.parquet --layer mesh_cells
ras2cng geometry model.g01.hdf bc_lines.parquet --layer bc_lines
ras2cng geometry model.g01.hdf structures.parquet --layer structures
ras2cng geometry model.g01.hdf cross_sections.parquet --layer cross_sections

# Text geometry
ras2cng geometry model.g01 cross_sections.parquet --layer cross_sections
ras2cng geometry model.g01 storage_areas.parquet --layer storage_areas
```

For full-project export with all layers consolidated into one file per geometry source,
use `ras2cng archive` instead. See [Project Archive](project-archive.md).

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
