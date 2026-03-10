# Geometry Layers Reference

ras2cng can extract up to 13 geometry layers from HEC-RAS project files. This page describes each layer, its source file type, and its geometry.

---

## HDF Geometry Layers (`.g##.hdf`)

These layers are extracted from HEC-RAS geometry HDF files. A layer may or may not be present depending on the model configuration (e.g., a 1D-only model has no `mesh_cells`).

| Layer | Class/Method | Geometry Type | Description |
|---|---|---|---|
| `mesh_cells` | `HdfMesh.get_mesh_cell_polygons()` | Polygon (fallback: Point) | 2D mesh cell faces. Falls back to centroid points if polygon faces are not stored. |
| `mesh_areas` | `HdfMesh.get_mesh_areas()` | Polygon | Perimeter polygons of each 2D flow area (mesh domain boundary). |
| `cross_sections` | `HdfXsec.get_cross_sections()` | LineString | 1D cross-section cut lines with station/elevation data. |
| `centerlines` | `HdfXsec.get_river_centerlines()` | LineString | River and reach centerlines for 1D geometry. |
| `bc_lines` | `HdfBndry.get_bc_lines()` | LineString | Boundary condition lines (inflow/outflow/normal depth locations). |
| `breaklines` | `HdfBndry.get_breaklines()` | LineString | Mesh breaklines (force mesh edges to follow terrain features). |
| `refinement_regions` | `HdfBndry.get_refinement_regions()` | Polygon | Areas with custom mesh cell size targets. |
| `reference_lines` | `HdfBndry.get_reference_lines()` | LineString | User-defined reference lines for output extraction. |
| `reference_points` | `HdfBndry.get_reference_points()` | Point | User-defined reference points for time-series output. |
| `structures` | `HdfStruc.get_structures()` | LineString/Point | Inline structures (weirs, culverts, bridges) and lateral structures. |

### Auto-selection (when `--layer` is not specified)

When no layer is specified for a single-file export, ras2cng prefers:

1. **`mesh_cells`** — most useful for flood visualization (depth/velocity overlaid on cell polygons)
2. First available layer, if `mesh_cells` is not present

For `archive` command: **all available layers** are merged into a single consolidated GeoParquet per geometry source, with a `layer` column discriminator. Text layers receive a `_text` suffix (e.g., `cross_sections_text`).

---

## Text Geometry Layers (`.g##`)

These layers are extracted from HEC-RAS plain-text geometry files (`.g01`, `.g02`, etc.). Available in both HDF-format projects (most modern models have both) and legacy text-only models.

| Layer | Class/Method | Geometry Type | Description |
|---|---|---|---|
| `cross_sections` | `GeomParser.get_xs_cut_lines()` | LineString | 1D cross-section cut lines parsed from the text geometry file. |
| `centerlines` | `GeomParser.get_river_centerlines()` | LineString | River/reach centerlines. |
| `storage_areas` | `GeomStorage.get_storage_areas()` | Polygon | Storage area polygons (offline storage or 2D flow areas defined as storage in older models). |

### Auto-selection (when `--layer` is not specified)

1. **`cross_sections`** — preferred
2. First available layer

---

## Layer Availability by Model Type

| Layer | 2D HDF | 1D HDF | 1D Text | Notes |
|---|---|---|---|---|
| `mesh_cells` | ✓ | — | — | Core layer for 2D models |
| `mesh_areas` | ✓ | — | — | Domain boundary polygons |
| `bc_lines` | ✓ | — | — | Inflow/outflow lines |
| `breaklines` | ✓ | — | — | Only if breaklines defined |
| `refinement_regions` | ✓ | — | — | Only if refinement regions defined |
| `reference_lines` | ✓ | — | — | Only if reference lines defined |
| `reference_points` | ✓ | — | — | Only if reference points defined |
| `structures` | ✓ | ✓ | — | Inline/lateral structures |
| `cross_sections` | ✓ | ✓ | ✓ | Available in all geometry types |
| `centerlines` | ✓ | ✓ | ✓ | Available in all geometry types |
| `storage_areas` | — | — | ✓ | Text geometry only |

---

## Exporting Specific Layers

### Single layer via CLI

```bash
# HDF geometry
ras2cng geometry model.g01.hdf output.parquet --layer bc_lines
ras2cng geometry model.g01.hdf output.parquet --layer breaklines
ras2cng geometry model.g01.hdf output.parquet --layer structures

# Text geometry
ras2cng geometry model.g01 xs_cutlines.parquet --layer cross_sections
ras2cng geometry model.g01 storage_areas.parquet --layer storage_areas
```

### All layers via archive (consolidated)

```bash
ras2cng archive path/to/project /output/archive
# Creates BaldEagle.g01.parquet with all layers, use WHERE layer = 'mesh_cells'
```

### All layers merged (Python API)

```python
from pathlib import Path
from ras2cng import merge_all_layers, HDF_LAYERS, ALL_HDF_LAYERS, ALL_TEXT_LAYERS

# See all known HDF layers
print(ALL_HDF_LAYERS)   # ['mesh_cells', 'mesh_areas', 'bc_lines', ...]
print(ALL_TEXT_LAYERS)   # ['cross_sections', 'centerlines', 'storage_areas']

# Merge all layers into one GeoDataFrame with `layer` column
gdf = merge_all_layers(
    Path("BaldEagle.g01.hdf"),
    text_path=Path("BaldEagle.g01"),  # optional text file
    sort=True,                         # Hilbert spatial sorting
)
# gdf has `layer` column: 'mesh_cells', 'bc_lines', 'cross_sections_text', ...
```

### Legacy per-file export (Python API)

```python
from ras2cng import export_all_hdf_layers, export_all_text_layers

# Export all available layers from one HDF file to separate parquets
written = export_all_hdf_layers(
    Path("BaldEagle.g01.hdf"),
    Path("/output/geometry/g01"),
)
# written = {"mesh_cells": Path("..."), "bc_lines": Path("..."), ...}

# Export all available layers from a text file
written = export_all_text_layers(
    Path("BaldEagle.g01"),
    Path("/output/geometry/g01_text"),
)
```

---

## Column Conventions

All layers follow ras-commander's snake_case column naming:

| Column pattern | Example | Notes |
|---|---|---|
| `mesh_name` | `"Perimeter 1"` | 2D flow area name |
| `cell_id` | `42` | Cell index within a mesh |
| `reach_id` | `"Upper Reach"` | River/reach identifier |
| `station` | `12345.6` | 1D XS river station |
| `geometry` | WKB | Always the last column; WKB-encoded for parquet |

---

## Error Handling

Layers absent from a given model are skipped gracefully — no error is raised. This is intentional: most models have only a subset of these layers.

```python
# If bc_lines doesn't exist in this model, written dict simply won't have it
written = export_all_hdf_layers(hdf_path, out_dir)
if "bc_lines" in written:
    print("BC lines exported:", written["bc_lines"])
else:
    print("No BC lines in this geometry")
```
