# Rust-native assessment for ras2cng (2026-10-29)

Question (user): what else can move to Rust, including major portions of ras2cng?
Builds on `rust_native_roadmap_2026-10-27.md` and `rust_native_roadmap_2026-10-28_upstream.md`.
No new measurements in this pass; every number below is quoted from those notes or from release notes.

## Engine releases since the last note

| Engine | Latest | What matters for ras2cng |
|---|---|---|
| ras-commander | 0.110.3 (2026-10-29) | fast-hdf >=0.2.4 floor; fast-map range `>=0.10.1,<0.12`; invalid-cell policy for horizontal/whole-simulation mapping (#745); FIM catalog scheduling (#739); retained mapping state + native TIFF delivery (#741); native DSS reads (#592) |
| ras-commander-fast-map | 0.11.0 (2026-10-29) | LRU retention of sloping timesteps across reach windows; retained ownership/terrain windows; opt-in direct terrio LZW GeoTIFF delivery |
| ras-commander-fast-map | 0.10.1 (2026-10-28) | first publish since 0.8: velocity (bit-for-bit on bridges p05 / p07), whole-simulation arrival/percent, top-priority source terrain, polygon AOIs |
| ras-commander-fast-hdf | 0.2.4 (2026-10-28) | crates.io `hdf5-reader` 0.10.1; extensible-array chunk indexes; pipe conduits/nodes; `hec_ras_file_type` (for terrio's HEC-RAS driver) |
| terrio | 0.5.0 (PyPI) | our streaming/COG work is merged on the gpt-cmdr fork only; upstream queue on HOLD except `hecras-driver` |

Immediate housekeeping:
- `pyproject.toml` floors `ras-commander>=0.110.1`. Raise to `>=0.110.3` so installs get fast-hdf 0.2.4
  and the fast-map 0.11 range (0.110.2 capped fast-map `<0.11`).
- ras2cng does not yet use fast-map 0.11's retained state or native TIFF delivery, and does not record
  the `invalid_cells` report in `MapResult`. The defaults already changed upstream (`"repair"`), so
  invalid meshes map instead of falling back; ras2cng should surface the report.
- The shared `H:\CLB-Repos\ras2cng\.venv` has ras-commander 0.93.0 and no terrio/freestiler/fast-hdf,
  and is not readable by every user profile. It is not the environment the measurements came from.

## Where the time goes now (Bald Eagle p07 archive, loaded host)

Archive end to end: 49-67 s.
- ras-commander geometry read + merge into GeoDataFrames: 10-14 s
- GeoDataFrame -> Arrow: ~5 s
- row-identity checks: ~3 s
- GeoParquet write: 10-15 s (Rust) or pyarrow + Python Hilbert keys (~10 s)
- Arrow IPC staging for the native writer: ~5 s

The Rust writer did not make the archive faster because the writer was never the bottleneck: the
**HDF -> GeoDataFrame -> Arrow round trip** is. HDF reading itself is already Rust (fast-hdf) inside
ras-commander. The cost is converting its output into shapely/geopandas objects and back.

## Inventory: what is Rust, what is not

| Area | Today | Rust status | Remaining Python/GDAL cost |
|---|---|---|---|
| HDF reads | fast-hdf via ras-commander | done | shapely/geopandas materialisation |
| Archive GeoParquet write | fast-hdf writer (default) | done | IPC staging, frame conversion |
| Archive pipeline (HDF -> GeoParquet) | Python orchestration over GeoDataFrames | **blocked by decision** (waits for terrio HEC-RAS driver + schema v3) | the 20-25 s above |
| Results export (`results.py`) | ras-commander summary vars + geopandas join to cell polygons | not started | same pattern as geometry |
| COG writing (`cog.py`) | terrio (default), GDAL fallback | done | `_scan_area_matched` = ~45% of terrio write time |
| Raster tiles | terrio streaming (default) | done | GDAL chain only as fallback |
| Terrain mosaic/reproject (`terrain._merge_tifs`, `_reproject_to_match`) | rasterio | engine work done (fork terrio #13), upstream HELD | whole-raster memory |
| Vector tiles | tippecanoe preferred, freestiler fallback | engine done | GeoParquet -> EPSG:4326 -> **NDGeoJSON text** -> tiler |
| Mapping, pure 2D | fast-map default | done for WSE/Depth/arrival/percent; velocity opt-in | out-of-scope plans -> RAS Mapper |
| Mapping, storage areas | RAS Mapper | fast-map #10 bit-identical, **parked** by decision | ~200 s per plan via RAS Mapper |
| Terrain export (multi-source, modified) | HEC-RAS .NET via RasTerrain | fast-map #11 partial (multi-source 84% fit); modifications not started | 160-240 s on Bald Eagle |
| Inundation boundary | RAS Mapper unless unreleased API | fast-map PR #5 / ras-commander #709 | 85 s vs 3.5 s |
| Duration, Froude, shear, D x V | RAS Mapper | duration unqualified; shear formula unknown; Max derived needs time-series pass | |
| Manning's raster | HdfLandCover (HEC-RAS 6.6+, Windows) | not started | Windows-only |
| Precipitation GeoTIFFs | h5py + rasterio | not started | small |
| DuckDB queries, spatial index | DuckDB (C++) | n/a | none worth moving |
| PostGIS sync | SQLAlchemy | n/a | network-bound |
| CLI, manifest, viewer/webgis service, publication checks | Python | n/a | none (orchestration) |
| Text geometry / `.prj` / `.rasmap` parsing | ras-commander (Python) | **no Rust parser exists** | small files; matters only for a Python-free binary |

## Options for "major portions in Rust"

### A. Python orchestrator, Rust engines (current decision; recommended)

fast-hdf = HEC-RAS reading and the layer catalog, terrio = formats, fast-map = mapping, ras2cng =
orchestration plus RAS Mapper/Python as fallback and equivalence oracle. Remaining gains come from
removing the GeoDataFrame round trip, not from rewriting ras2cng modules.

### B. A `ras2cng-core` Rust crate (PyO3) for the archive and viewer pipelines

One crate that goes HDF -> Arrow -> GeoParquet / PMTiles / manifest entries without Python objects.
This is what A arrives at through terrio's HEC-RAS driver. Building it inside ras2cng instead would
duplicate the driver and contradict the 2026-10-28 decision ("fast-hdf = engine, terrio = formats").
It would only be worth it if terrio upstream does not accept the driver.

### C. Rust ras2cng binary (no Python)

Needs Rust parsers for text geometry, `.prj`, `.p##`, `.u##` and `.rasmap`, a Rust manifest writer,
and still shells out to HEC-RAS for everything RAS Mapper-only. Benefit: single-file distribution to
agencies. Performance benefit: small, because the text files are tiny and the heavy paths already run
in engines. **Not recommended now**; revisit if a no-Python deliverable becomes a requirement.

## Ranked opportunities

| # | Change | Expected gain | Blocker / owner |
|---|---|---|---|
| 1 | **Direct HDF -> Arrow -> GeoParquet archive** (fast-hdf Arrow layer API + terrio GeoParquet profile; manifest 3.0 schema) | biggest single win: removes ~20-25 s of the 49-67 s p07 archive (estimate, not measured) and most peak memory | waits for terrio HEC-RAS driver upstream (user decision 2026-10-28) |
| 2 | **Results export the same way** (summary variables joined to cell/face ids in Arrow, no geopandas) | same pattern for `--results`; p07 cell results also stop carrying 191 all-null union columns | rides on #1's schema |
| 3 | **Arrow straight into the vector tiler**: reproject in Rust and pass GeoArrow to freestiler (or fast-hdf #37 PMTiles with columnar properties) instead of writing NDGeoJSON | removes the Python reproject + JSON serialisation per layer; tippecanoe still needs text, so this favours freestiler | freestiler archives are 40-50% larger (no simplification; meshes are <1% simplifiable anyway). Decide whether speed beats size for the viewer |
| 4 | **Storage-area mapping in fast-map** (un-park #10) | removes ~200 s RAS Mapper per SA plan; SA models are common | user decision to un-park; already bit-identical on p07 |
| 5 | **Native effective terrain** (fast-map #6: multi-source mosaic + modifications) | removes the 160-240 s HEC-RAS terrain export and the Windows/Wine dependency for mapping | research: multi-source reproduction 84%; needs a reference where flood reaches a lower-priority source |
| 6 | **Inundation boundary via Rust** | 85 s -> 3.5 s on Davis | waits for ras-commander #709 + fast-map #5 release |
| 7 | **Bounded-memory warp/mosaic** for `terrain._merge_tifs`, `mapping._reproject_tifs` | removes rasterio whole-raster paths | terrio warp-mosaic-bounded, HELD after hecras-driver |
| 8 | **terrio COG report in the default domain** | drops `_scan_area_matched` (~45% of terrio COG write time) | terrio change (draft on fork) |
| 9 | **Adopt fast-map 0.11 retention + native TIFF delivery** in `map` for multi-profile/multi-plan runs | helps batch FIM runs; small for one plan | none; capability-detect like the rest of `fast_map.py` |
| 10 | Velocity default-on, duration, Froude/D x V, shear | fewer RAS Mapper fallbacks | velocity mask differs on bridges p05; duration unqualified; shear formula unknown |
| 11 | Manning's raster and precipitation rasters in Rust | removes HEC-RAS 6.6 / Windows requirement for Manning's | low priority; small rasters |

Not worth Rust: DuckDB queries and the spatial-index pass (already C++), PostGIS sync, CLI, manifest,
webgis service, publication validation, viewer manifest. Also still true from 10-27: polars, SedonaDB,
geoarrow-rust, a custom overview kernel.

## Recommendation

1. Now, no blockers: bump the ras-commander floor to 0.110.3; surface `invalid_cells` and adopt
   fast-map 0.11 retention in `fast_map.py` (#9).
2. Decide on un-parking storage areas (#4): the largest remaining RAS Mapper cost with work already
   done.
3. Keep #1-#2 as the main ras2cng performance project, sequenced after the terrio HEC-RAS driver lands
   upstream, as decided. Meanwhile fast-hdf can build the Arrow layer API in terrio naming.
4. Decide #3 (freestiler-first viewer tiles, accepting larger archives) separately; it is the only
   viewer-side change with a sizeable gain.
5. Treat #5 (native terrain) as research; it is what makes mapping fully HEC-RAS-free.
