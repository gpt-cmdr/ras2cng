# Feasibility: Stored Maps from Plan HDF + Terrain TIFF only

**Date:** 2026-07-07 · **Status:** CONFIRMED FEASIBLE — validated by live testing on this machine (HEC-RAS 6.6 + 7.0.1)

## Goal

Simplify `ras2cng map` inputs from "full HEC-RAS project" to:
- **Plan HDF** (results file) — required
- **Terrain TIFF** — required (or optional pre-built terrain HDF sidecar set to skip the terrain build)

while still running through RASMapper (RasMapperLib) for rendered stored maps.

## Key discovery: no HEC-RAS project is needed at all

`RasStoreMapHelper.exe` (ships inside ras-commander at `ras_commander/native/`, reflects into
the install's `RasMapperLib.dll`) has this usage:

```
RasStoreMapHelper.exe <hecrasDir> <renderMode> StoreAllMaps <rasmapFile> [resultHdf]
```

It consumes **only** a `.rasmap` file + the plan HDF. The `.rasmap` never references the
HEC-RAS `.prj`. The plan HDF carries everything else needed:

| Needed for map export | Source in plan HDF |
|---|---|
| Projection WKT | root attr `Projection` |
| Units | root attr `Units System` |
| Plan ShortID (output folder name) | `Plan Data/Plan Information` attr `Plan ShortID` |
| Plan/Project/Geometry/Flow names | `Plan Data/Plan Information` attrs |
| Geometry (mesh, XS) | `Geometry/` group (full copy) |

## Validated pipeline (test 1 — no project, direct helper call)

Test dir contained ONLY `BaldEagleDamBrk.p07.hdf` (460 MB, 6.6 results) + 2 raw terrain TIFFs.

1. **Projection**: extract `Projection` attr from plan HDF → write `Terrain\Projection.prj`.
2. **Terrain build (headless, no RASMapper GUI, no project):**
   ```
   RasProcess.exe CreateTerrain units=Feet stitch=true prj=<Projection.prj> out=<Terrain\Terrain50.hdf> <dem1.tif> <dem2.tif>
   ```
   Produces `Terrain50.hdf` + `Terrain50.vrt` + imported per-source TIFFs. `units=` from HDF
   `Units System` (`US Customary`→`Feet`). This is already wrapped by
   `RasTerrain.create_terrain_from_rasters()` (ras-commander `terrain/RasTerrain.py:1934`),
   incl. an isolated-GDAL child env (CreateTerrain is sensitive to inherited GDAL/PROJ vars).
3. **Synthesize ~25-line `.rasmap`**: `RASProjectionFilename`, `<Terrains>` layer →
   `Terrain\Terrain50.hdf`, `<Results>` layer → plan HDF with `RASResultsMap` children
   (`MapParameters MapType="depth|elevation|velocity" OutputMode="Stored Current Terrain"
   ProfileIndex="2147483647" ProfileName="Max"`). Schema matches what
   `RasProcess._add_stored_map_to_rasmap()` (RasProcess.py:1141) generates.
4. **Run helper** → `StoreAllMaps completed successfully` in ~15 s. Output lands in
   `.\<Plan ShortID>\` (RasMapperLib overrides the StoredFilename folder) as per-terrain-tile
   TIFF + VRT.

Validated with rasterio: Depth (Max) min 0 / max 84.05 ft, WSE 549.2–670.4 ft, EPSG:2271,
8643×6902 @ terrain resolution. Real dam-break results, correct georeferencing.

**Cross-version:** the same helper exe run with `hecrasDir` = 7.0.1 install (which no longer
ships RasStoreMapHelper.exe) worked identically — `RasMapperLib.Scripting.StoreMapCommand` /
`StoreAllMapsCommand` still present in 7.0.1's RasMapperLib.dll (verified by binary scan +
live run of a 6.6 plan HDF through the 7.0.1 lib).

## Validated pipeline (test 2 — barebones project through ras2cng/ras-commander)

To reuse ras-commander's `RasProcess.store_maps()` (map-type registry, profile handling,
rasmap mutation/backup, GDAL staging, output move, georef fix), `init_ras_project` must
succeed. Synthesized scaffold — all content derived from plan HDF attrs:

- `BaldEagleDamBrk.prj` — 6 lines (`Proj Title`, `Current Plan`, `English Units`,
  `Geom File=g04`, `Unsteady File=u01`, `Plan File=p07`)
- `BaldEagleDamBrk.p07` — 5 lines (`Plan Title`, `Short Identifier`, `Simulation Date`,
  `Geom File`, `Flow File`)
- `BaldEagleDamBrk.rasmap` — synthesized as above (Results element left empty; store_maps
  populates it)
- `Terrain\` (pre-built) + plan HDF. **No** `.g04`, `.g04.hdf`, `.u01` files.

`ras2cng.mapping.generate_result_maps(project_dir, out, ras_version="6.6",
render_mode="sloping")` ran end-to-end: WSE + Depth + Velocity rasters generated, moved to
`map_output/p07/`, georeferencing fixed. **No template plans needed** — 11 lines of
synthesized text files suffice. (ras-commander's template/clone APIs operate within an
existing project; unnecessary here.)

## Blockers found (all minor, all solved)

1. **`.prj` collision** — `ras2cng.project.resolve_project_path` errors on multiple `.prj`
   files; the ESRI projection file must go in `Terrain\Projection.prj` (matches real-project
   convention), or the specific project `.prj` must be passed.
2. **Missing `.u01`/geometry files** → noisy non-fatal ERROR logs from `RasPrj._load_project_data`
   / `GeomMetadata`. Cosmetic; could write 1-line stubs or suppress.
3. **Terrain must be a HEC-RAS terrain HDF** — a bare TIFF is insufficient for RasMapperLib,
   but `RasProcess.exe CreateTerrain` builds it headlessly (~1–2 min for 180 MB). If the user
   supplies the terrain sidecar, they must supply the **full set** (`.hdf` + `.vrt` + imported
   `.tif`s — the HDF references the others by relative path).
4. **Plan HDF filename convention** — scaffold requires `{Project}.pNN.hdf` naming; if the
   input HDF has an arbitrary name, hardlink/copy it into the scaffold under a canonical name
   (project name + plan number are recoverable from `Plan Data/Plan Information` attrs).
5. **Hard environmental dependency (unchanged):** a Windows HEC-RAS install (RasMapperLib +
   bundled GDAL). Everything else about a "project" is synthesizable.

## Open items — RESOLVED by round-2 testing (same day)

All tested and passing (see `map_hdf_implementation_plan.md` §2 for the full matrix):
- Timestamp profiles: PASS (`02JAN1999 00:00:00` → per-timestep WSE/Depth, physically consistent).
- All 7 raster map types incl. Froude/Shear/D×V/D×V²: PASS — note RasMapperLib creates a
  large `PostProcessing.hdf` (640 MB observed) for derived types.
- `inundation_boundary` shapefile: generated correctly, but ras2cng's result collector
  misses non-TIF outputs (fix planned).
- 1D-only model (Muncie): PASS, including hyphenated Plan ShortID.
- `slopingPretty`: PASS on 6.6.
- Arbitrary input HDF filename → hardlink to canonical name: PASS.
- Arrival time / duration / recession: **pre-existing** ras2cng limitation
  (`mapping.py` `PARAM_MAP` maps them to None) — unrelated to the scaffold approach.

## Proposed ras2cng feature shape

New CLI command (working name `map-hdf`):

```
ras2cng map-hdf RESULTS.pNN.hdf --terrain dem.tif [dem2.tif ...] OUTPUT_DIR
ras2cng map-hdf RESULTS.pNN.hdf --terrain-hdf Terrain.hdf OUTPUT_DIR   # skip CreateTerrain
```

Implementation sketch (new module `ras2cng/scaffold.py`):
1. Read plan HDF attrs → project name, plan number, ShortID, units, projection WKT.
2. Build scaffold dir (temp or `--workdir`): write projection, minimal `.prj`/`.pNN`/`.rasmap`;
   hardlink (same volume) or copy the plan HDF in under canonical name.
3. If `--terrain` TIFFs: `RasTerrain.create_terrain_from_rasters()`; if `--terrain-hdf`: copy/link sidecar set.
4. Delegate to existing `generate_result_maps()` unchanged (keeps all map-type flags,
   `--out-crs`/COG handling, render modes).

Test artifacts from this session: `scratchpad\minimal_test\` and `scratchpad\barebones_test.py`
(+ `barebones_test\`) under the session scratchpad dir.
