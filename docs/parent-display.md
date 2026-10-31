# Parent display publication

Status: PROPOSED, tested but uncommitted. `ras2cng.parent_display` publishes parent-model
display layers as canonical GeoParquet and PMTiles. fim-commander decides which layers exist
and are available (see its `docs/plans/parent-display.md`); this module publishes a layer it is
handed and refuses one it should not draw. It adds no renderer.

```python
from ras2cng.parent_display import publish_parent_layer, PublishRefused

record = publish_parent_layer(cells_gdf, "parent_mesh", out_dir)
# {"layer_id": "parent_mesh", "feature_count": 413150, "crs": "EPSG:2278",
#  "geoparquet": "parent_mesh.geoparquet", "pmtiles": "parent_mesh.pmtiles",
#  "tiler": {"backend": "freestiler", "preferred": "freestiler", "fallback_reason": null}}
```

| Layer | Written | Notes |
|---|---|---|
| `parent_domain`, `parent_refinement`, `parent_structures`, `parent_connections` | GeoParquet | Small; read directly |
| `parent_mesh` | GeoParquet and PMTiles | Tiles start at z13, the detail floor in `maplibre.py` |
| `parent_breaklines` | GeoParquet and PMTiles | Full zoom range |

- GeoParquet keeps the model's native projected CRS and units, with bbox columns and
  `covering` metadata from `project._write_geoparquet`. PMTiles are reprojected to EPSG:4326.
- Freestiler is requested first. When it is not installed the existing `RAS2CNG_VECTOR_TILER`
  resolution applies and `tiler.fallback_reason` says so; a receipt never names a backend that
  did not run. Features are never dropped.
- Refusals raise `PublishRefused` with a stable `reason_code` and write nothing:
  `UNKNOWN_LAYER`, `LAYER_EMPTY`, `CRS_MISSING`, `GEOMETRY_NULL`, `GEOMETRY_TYPE_MISMATCH`,
  `OUTPUT_EXISTS`. An empty layer is refused, not published as a zero-feature file.

## Output directory contract (not atomic)

Publication is **not** atomic across the GeoParquet and PMTiles. Give each attempt a fresh
unit directory.

- If a layer's final GeoParquet or PMTiles name is already in the directory, the call refuses
  with `OUTPUT_EXISTS` before writing, so a completed artifact is never overwritten.
- If the GeoParquet is written and the tiler then fails, the exception propagates (the call
  never reports success) and the files this call created are removed on a best-effort basis.
  A crash or a failed removal can still leave partial files.
- A directory is publication-ready only when the caller has written a completed receipt after
  every layer returned. Absent that receipt, treat whatever is in it as unpublished.
- Other layers' files in the same directory are never touched by a failed call.
