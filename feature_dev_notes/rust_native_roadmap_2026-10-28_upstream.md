# Upstream submissions (2026-10-28)

Approved by the user: terrio 1-5 + edge padding (hold dem-shaded-relief), freestiler x3,
lerc-rust x2; area-matched overviews contributed under terrio's licence (NOTICE entry dropped,
branch amended to e4e9cd0); AI-assistance notes kept.

Opened:
- opengeos/terrio #362 cog-validate, #363 dem-streaming
- walkerke/freestiler #31 deterministic-coalesce, #32 gzip-level, #33 layered-tiler (stacked on #31, #32)
- roteiro-gis/lerc-rust #48 lossy-block-modes, #49 gdal-encoder-profile (stacked on #48)

Blocked: terrio refused raster-tiles-streaming, warp-mosaic-bounded, cog-options-overviews and
edge-tile-nodata-padding with "gpt-cmdr does not have the correct permissions to execute
CreatePullRequest" (REST: 404) right after #362/#363 opened. Token and repo access are normal and
freestiler/lerc-rust accepted PRs afterwards, so it looks like a per-contributor open-PR cap on
terrio. Bodies: G:\RustDev\upstream\terrio\*.md; submit script G:\RustDev\upstream\_submit\submit.py.
Held: dem-shaded-relief (overlaps upstream #346 blend).

## Architecture decision (user, 2026-10-28): pure Rust where possible, lean engine

- **fast-hdf = engine.** HDF reading, every HEC-RAS rule, and the canonical layer schema
  (names, fields, dtypes, identities), returned as Arrow behind a feature flag. No new file formats.
- **terrio = formats.** GeoParquet/PMTiles/FlatGeobuf/COG writers, plus the HEC-RAS driver as a thin
  adapter over fast-hdf (fork PR #1; included once the upstream PRs settle).
- **ras2cng = orchestration.** Archive profile, manifest, RAS Mapper workflows. The Python pipeline
  stays for RAS Mapper-driven work and as the equivalence oracle.
- **fast-hdf's existing writers** (GeoParquet/PMTiles/FGB/GPKG) are frozen as a bridge and retire once
  terrio carries the HEC-RAS driver upstream. ras2cng's archive profile (Hilbert, ZSTD level, layer
  column, bbox struct) becomes generic terrio GeoParquet options.
- **Known divergence to fix:** layer names differ between ras-commander Python (ras2cng archives),
  terrio's driver and fast-hdf's export contract. fast-hdf defines the catalog; the others alias it.
- **Agent fh-direct:** assessment in `G:\RustDev\work\direct\ASSESSMENT.md`, then the HDF → Arrow
  layers API and direct HDF → GeoParquet for ras2cng (draft PR).

## Layer-schema assessment (agent fh-direct, 2026-10-28; full text G:\RustDev\work\direct\ASSESSMENT.md)

1. **Three schemes today.** ras2cng archives use ras-commander names, raw HDF attribute columns and
   synthetic ids (`bc_line_id`). terrio's HEC-RAS driver uses the engine family names, snake_case
   fields and `source_row`. fast-hdf's export contract has no layer catalog, only the
   `(mesh_name, cell_id)` identity.
   - Name differences: `mesh_areas` = `mesh_perimeters`; `centerlines`/`bank_lines`/`edge_lines` =
     `river_*`; `is_ghost_cell` = `is_ghost`.
   - Result types: ras2cng stores float32 values and `_time` as timestamp[ns] rounded to 100 ms;
     terrio uses f64 values and `_time` in days.
   - p07 quirk: ras2cng cell-result layers carry all 191 g01 union columns as all-null. Face results
     take their lines from the plan HDF; cell results take polygons from the geometry HDF.
2. **Catalog.** fast-hdf owns one layer catalog using the engine family names (terrio's names).
   Other names map onto it through an alias table.
3. **Lean cost.** A feature-gated `arrow` output (hdf, mesh, results plus arrow-array/schema/buffer
   with ffi) needs no pyo3, parquet or tempfile. Every crate is already in the exports dependency
   graph, so no new crates.
4. **Writers.** Freeze fast-hdf's format writers (GeoParquet/PMTiles/FGB/GPKG) and retire them once
   terrio's HEC-RAS driver is upstream. Before it can take over ras2cng's archive profile, terrio's
   GeoParquet writer needs:
   - Hilbert order plus a stored `hilbert_index`;
   - a ZSTD level setting;
   - a layer column, with row groups ending at layer boundaries;
   - the bbox struct covering;
   - dictionary encoding on Utf8 only, with chunk statistics;
   - a union schema with non-null identity columns.

## Decisions (user, 2026-10-28)

- **ras2cng adopts terrio's schema later.** When the terrio HEC-RAS driver and its GeoParquet logic
  are upstream, ras2cng archives move to the terrio/engine layer schema, as a manifest major-version
  change. **Until then, ras2cng waits:** no schema change and no direct HDF → GeoParquet archive path
  in ras2cng. Pick this up once all terrio PRs have settled. fast-hdf can keep building the canonical
  catalog and Arrow layer API in terrio naming, since that's the shared foundation.
- **ras2cng keeps every RAS Mapper and Python path.** Accelerated Rust paths are the DEFAULT, with
  RAS Mapper/Python as explicit alternatives and as automatic fallback:
  - rasters/COGs/raster tiles: terrio (default already);
  - archive GeoParquet: fast-hdf native writer (default, falls back to pyarrow);
  - **mapping for 2D models: fast-map by default.** Use the interpolation (render mode:
    horizontal / sloping) from the project's `.rasmap`. Fall back to RAS Mapper for anything out of
    scope (1D, storage areas, unsupported products or render modes). `--engine rasmapper` forces the
    native path.
- **Auto feed stopped.** The terrio upstream queue submits only on explicit instruction.
