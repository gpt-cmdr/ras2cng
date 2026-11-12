# mapping

Result raster generation for HEC-RAS projects using the RASMapper engine.

## Overview

The `mapping` module generates georeferenced raster files (GeoTIFF) from completed HEC-RAS plan results. It uses a private compatibility adapter around ras-commander's stored-map APIs so profile selection is explicit and validated before map generation mutates the project.

`Max` and `Min` remain explicit summary selections. An exact named steady profile is resolved from the plan HDF and sent through `RasMap.store_all_maps(mode="steady_profiles")`; a missing or duplicate name raises an error instead of silently producing `Max`. Unsteady timestamps are likewise validated exactly. Whole-simulation products such as arrival time are generated separately because they do not have a profile dimension.

On Linux, RasProcess.exe runs under Wine. See the [Linux/Wine Setup](../user-guide/linux-wine-setup.md) guide.

## Supported Map Types

| Map Type | CLI Flag | Default | Description |
|----------|----------|---------|-------------|
| `wse` | `--wse/--no-wse` | On | Water Surface Elevation |
| `depth` | `--depth/--no-depth` | On | Depth |
| `velocity` | `--velocity/--no-velocity` | On | Velocity |
| `froude` | `--froude` | Off | Froude Number |
| `shear_stress` | `--shear-stress` | Off | Shear Stress |
| `depth_x_velocity` | `--dv` | Off | Depth x Velocity |
| `depth_x_velocity_sq` | `--dv-sq` | Off | Depth x Velocity² |
| `inundation_boundary` | `--inundation-boundary` | Off | Inundation Boundary (GeoJSON from the default fast-map engine, [much faster](#inundation-boundaries-in-rust-recommended-with-fast-map); shapefile from RAS Mapper) |
| `arrival_time` | `--arrival-time` | Off | Arrival Time (hours, whole-simulation) |
| `duration` | `--duration` | Off | Inundation Duration (hours, whole-simulation) |
| `percent_inundated` | `--percent-inundated` | Off | Percent Time Inundated (whole-simulation) |

`--recession` is accepted for compatibility but ignored with a warning —
RasMapperLib has no recession map type, and only RasMapperLib-native outputs
are produced.

### Whole-simulation map types

`arrival_time`, `duration`, and `percent_inundated` are computed over the
entire simulation — the `--profile` option does not apply to them. Their
filenames carry the `--arrival-depth` wet/dry threshold instead of the profile
name, e.g. `Arrival Time (0.1ft hrs).tif`. They work with any ras-commander
version: newer versions generate them natively via `store_maps()`; on older
versions ras2cng pre-injects the stored-map entries into the `.rasmap`
(restored afterwards) so the same StoreAllMaps run produces them.

Generating these types causes RasMapperLib to build a `PostProcessing.hdf`
cache that can exceed the plan HDF in size; ras2cng deletes it from the output
directory unless `--keep-postprocessing` is passed.

## Local performance policy

The default `DEFAULT_LOCAL_MAP_PERFORMANCE` preset is tuned for local
processing on the 8-core, 31.8 GiB development workstation:

- memory-aware automatic helper selection (`max_workers=None`);
- enforced memory admission with an 8192 MiB / 25 percent reserve;
- one GDAL thread per helper to prevent nested oversubscription;
- a 64 MiB GDAL cache cap charged to every helper's memory estimate.

Only independent WSE, Depth, and Velocity products run concurrently. Large
terrain estimates and products that require shared ordered state automatically
use one helper. On the Spring River fixture, the measured estimate is about
11.2 GiB per helper, so this 31.8 GiB machine remains serial; a machine with
more available memory can admit two or three map helpers without changing the
call.

Use `--map-workers 1` for a controlled serial comparison, or set the helper
ceiling, reserve, and cache explicitly:

```bash
ras2cng map model.prj ./maps \
  --map-workers 2 \
  --map-reserve-memory-mb 8192 \
  --map-gdal-cache-mb 64
```

Python callers can provide the full typed policy without adding a second map
function:

```python
from ras_commander import StoreMapPerformanceOptions
from ras2cng import generate_result_maps

results = generate_result_maps(
    "model.prj",
    "maps",
    performance=StoreMapPerformanceOptions(
        max_workers=None,
        reserve_memory_mb=8192,
        gdal_cachemax_mb=64,
    ),
)
```

## Render Mode

The `--render-mode` option controls how the water surface is rendered to raster grids. This is critical for pixel-perfect output — HEC-RAS 6.x's `RasProcess.exe` ignores the render mode from the `.rasmap` file, so ras-commander uses `RasStoreMapHelper.exe` to set the mode explicitly before generating maps.

| Mode | Flag | Description |
|------|------|-------------|
| `horizontal` | `--render-mode horizontal` | Flat water surface within each mesh cell (default) |
| `sloping` | `--render-mode sloping` | Interpolated sloping water surface |
| `slopingPretty` | `--render-mode slopingPretty` | Sloping with depth-weighted face reduction (HEC-RAS 6.4+) |

If `--render-mode` is not specified, the mode is read from the project's `.rasmap` file (defaults to `horizontal` if not set).

```bash
# Generate maps with sloping render mode
ras2cng map /path/to/project /output/maps --render-mode sloping

# Archive with slopingPretty render mode (requires HEC-RAS 6.4+)
ras2cng archive /path/to/project ./archive/ --results --map --render-mode slopingPretty
```

## Mapping engine: RAS Mapper or fast-map

**fast-map is the default engine.** It maps selected products through the
Rust `ras-commander-fast-map` engine and hands everything else to RAS Mapper
(`RasStoreMapHelper.exe`) per product group, printing the reason;
`--engine rasmapper` (or `RAS2CNG_MAP_ENGINE=rasmapper`) renders every
product through RAS Mapper. The water-surface render mode comes from the
project's `.rasmap`, as RAS Mapper takes it (`--render-mode` overrides it);
a project with no stored mode goes to RAS Mapper, which applies its own
default. fast-map is built for **FIM library
creation on pure 2D models**, not for general use across all model types:
every fast-map product is qualified against RAS Mapper output (pixel
conformance against native HEC-RAS 7.0.1 stored maps on the named models),
and everything else falls back to RAS Mapper. ras-commander installs the
engine through `ras-commander[full]` on Windows x64 and Linux x64
(`ras2cng[fast-map]` requests it explicitly). The mapping arithmetic needs no
GDAL, and with the source-terrain path below no HEC-RAS either.

```bash
# Default: Rust WSE/Depth, arrival time and percent inundated; the rest via RAS Mapper
ras2cng map /path/to/project /output/maps \
    --arrival-time --percent-inundated --arrival-depth 0.5 --ras-version 7.0.1

# Everything through RAS Mapper
ras2cng map /path/to/project /output/maps --engine rasmapper

# Reuse a terrain export across runs
ras2cng map /path/to/project /output/maps --engine fast-map \
    --fast-map-terrain /output/maps/_fast_map_terrain/Terrain50.tif

# Fail instead of falling back (qualification runs)
ras2cng map /path/to/project /output/maps --engine fast-map --fast-map-strict

# Map a plan with storage areas in Rust (opt-in; otherwise RAS Mapper maps it)
ras2cng map /path/to/project /output/maps --fast-map-storage-areas
```

`RAS2CNG_MAP_ENGINE` selects the engine everywhere, including
`ras2cng archive --map`. Scope, engine and render mode are checked from the
plan HDF and `.rasmap` before any terrain work, so an out-of-scope plan goes
to RAS Mapper without a terrain export.

| Product | fast-map | Qualification against RAS Mapper |
|---|---|---|
| WSE, Depth (Max) | yes | identical float32 values and masks on five models (1.2–3.6× faster for horizontal and plain sloping, 1.8–7.3× for depth-weighted sloping faces; [evidence](https://rascommander.info/ras/development/fim-mapping-performance/)); Davis p02 and Bald Eagle bridges p05 again on the source-terrain path |
| WSE, Depth with storage areas | **opt-in** (`--fast-map-storage-areas`; slower than RAS Mapper today), as RAS Mapper level pools, when the engine advertises `storage-area-level-pool/1` | identical on Bald Eagle p07 and p04 in every render mode (466,912 / 461,639 storage-area px); p07 Max sloping through `ras2cng map`: 0 mask and 0 bit differences on 1,345,691 px |
| Arrival time, percent time inundated | yes (`--no-fast-map-whole-simulation` to opt out) | identical on Davis p02 (4,879 px) and bridges p05 (959,043 px) at 0.5 ft, horizontal and plain sloping; with a storage area, p07 (1,333,183 px) |
| Duration | yes, when the engine advertises RAS Mapper's recession rule (`rasmapper-backward-window-recession/1`); not with storage areas | identical on Davis p02 and bridges p05 at 0.5 ft |
| Velocity (Max) | yes in plain sloping and horizontal when the engine advertises `velocity_depth`; `--no-fast-map-velocity` to opt out; not with storage areas | bridges p05: 0 value and 0 mask differences in sloping (973,902 px) and horizontal (973,532 px). Older engines: opt-in, plain sloping only |
| Velocity (Max), slopingPretty | yes when the engine advertises `rasmapper-with-faces-velocity-derived/1` (fast-map 0.16.0 / ras-commander 0.117.0, the versions ras2cng requires), with the `.rasmap`'s shallow-reduction and depth-weighting flags passed unchanged; with older engines it is opt-in (`--fast-map-velocity`) and plain slopingPretty only | 0 mask and 0 bit differences on bridges p05 and Davis p02 in all four option combinations (plain, shallow reduction, depth-weighted, both). The mask is the depth rendered with the same options, so the wrong flags give a different mask: an earlier 75 + 28 px difference on bridges compared a plain run with a shallow-reduction reference |
| D × V, D × V², Froude (Max) | yes in plain sloping and horizontal (`--no-fast-map-derived` to opt out), and in slopingPretty under the same engine record as velocity; not with storage areas | per-time product, pixelwise Max over every output time, as RAS Mapper computes it: 0 / 0 on bridges p05 over 433 times (973,956 px sloping); Davis within 2 ulp on ≤ 5 px. slopingPretty: 0 / 0 on bridges p05 and Davis p02, four option combinations |
| Shear stress (Max) | yes in plain sloping and horizontal when `RasFim.supports_shear_stress()` is true (`rasmapper-face-shear-max/1`; fast-map 0.16.0 / ras-commander 0.117.0); `--no-fast-map-shear` to opt out; not in slopingPretty or with storage areas. With older engines: RAS Mapper | 0 mask and 0 bit differences on bridges p05 (973,902 px sloping, 973,532 px horizontal) and Davis p02 (22,041 / 20,560 px). bridges 4.7 s against RAS Mapper's 84.8 s |
| Inundation boundary | yes (see below): deterministic contours by default, the native-serial contract on request | deterministic: close to, not identical with, RAS Mapper's polygons. native-serial: exact against RAS Mapper's serial boundary export on named fixtures only; the engine labels it unqualified |

A plan is sent to RAS Mapper instead when:

| Condition | Why |
|---|---|
| 1D river reaches or cross sections | Outside the engine's method (`RasFim.validate_2d_model`) |
| Storage areas, unless `--fast-map-storage-areas` (`fast_map_storage_areas=True`, `RAS2CNG_FAST_MAP_STORAGE_AREAS=1`) is given | Not a default until it is faster than RAS Mapper: Bald Eagle p07 WSE + Depth took 18.7 s in Rust against 12.2 s. Opt in to map storage-area plans without HEC-RAS (Linux, FIM batches) |
| Storage areas, opted in, with an engine that does not advertise `storage-area-level-pool/1` | It would leave the storage-area pixels blank |
| A profile other than `Max` (per-profile products only; arrival, duration and percent are whole-simulation) | Timestep mapping is not wired through ras2cng yet. Decided before any terrain work |
| No usable terrain | See below |
| Engine missing or failed | No arithmetic fallback inside fast-map |

**Terrain.** With ras-commander's `RasFim.top_priority_terrain_source`
(ras-commander #724, fast-map #11), ras2cng maps on the terrain's unmodified
**priority-1 source TIFF**, with its terrain-HDF footprint mask, and skips the
RAS Mapper terrain export. Inside that footprint the source TIFF is the
effective terrain, and the engine refuses when a wet cell would draw outside
it. ras2cng then:

1. retries on the source's CRS-canonical copy, only when the refusal was
   about the CRS (Davis);
2. otherwise builds the **native effective terrain** once per run, when the
   engine advertises `rasmapper-effective-terrain/1` and RasFim has
   `build_effective_terrain`: RAS Mapper's multi-source mosaic (stitch TIN
   first, then each source in priority order) rebuilt in Rust from the
   source TIFFs, their level-0 masks and the stitch TIN, without HEC-RAS. On
   Bald Eagle Terrain50 the valid mask is identical on 197,179,058 px and 8
   values differ, all in stitch-TIN corners where RAS Mapper's own exports
   disagree (max 1.53 ft); it took 7.0 s and 1.4 GB, against about 190 s
   for the export. Terrains with modifications only on the conditions
   below. An unmodified terrain also needs a qualified HEC-RAS release
   (`QUALIFIED_EFFECTIVE_RUNTIMES`: 6.6, 6.7 Beta 4/5, 7.0, 7.0.1, 7.0.2;
   the 2026-11-12 qualification found base terrains on 6.5 and earlier
   differ from RAS Mapper, and later releases are unqualified): the run's
   release (`--ras-version`, else ras-commander's detection) or, when no
   installed HEC-RAS resolves one (Linux), the newest `Program Version` of
   the project's plan and geometry files. Any other release skips this step
   with `reason_code: "hecras_version_unqualified"`, and a release that does
   not resolve with `"hecras_version_unresolved"` (pass `--ras-version`).
   The decision is recorded under `effective_terrain` in
   `MapResult.fast_map_terrain_route` (`route` `native-effective` or
   `rasmapper-export`, `reason`, `reason_code`, `hecras_release`,
   `hecras_release_basis`) and in the route manifest's `effective_terrain`
   list (`TerrainResolver.effective_route`). The priority-1 source TIFF
   path above builds no effective terrain and is used on any release;
3. otherwise exports the plan's terrain once per run through RAS Mapper
   (`RasTerrain.export_rasmapper_terrain`, HEC-RAS 6.4.1/6.5/6.6/7.0.1/7.1;
   pass `--ras-version`).

`--fast-map-terrain` supplies a float32 export (with modifications) instead,
`--no-fast-map-effective-terrain` skips step 2, and
`--no-fast-map-source-terrain` skips the source TIFF.

**Terrains with modifications.** The earlier rule that every modified
terrain goes to RAS Mapper (user decision 2026-10-30) is superseded: the
project engineer confirmed the reversal on 2026-11-11, now that the native
route is implemented. Before any of the steps above, ras2cng
reads the terrain HDF's `/Modifications` group and the `.rasmap`'s
modification layers (`ras2cng.terrain.terrain_modifications`). If it finds
any, or cannot read the terrain HDF, the source TIFF (and step 1) is skipped
and the inundation boundary is drawn by RAS Mapper. Step 2 then builds the
native effective terrain **with the modifications applied** only when every
one of these holds (`TerrainResolver.modified_route`):

- the installed engine advertises `rasmapper-effective-terrain/2` (Fast-Map
  0.18.0 and later; 0.17.0 advertises only `/1`) with a `modifications`
  record and accepts `ras-commander-effective-terrain-request/2`;
- ras-commander has the terrain-modification reader that stages the
  engine's `modification_layers`, and it stages every layer without refusing
  one. ras2cng uses the public `RasFim.read_terrain_modifications`
  (ras-commander 0.119.0 and later) and falls back to the private
  `ras_commander.fim._terrain_mods.read_layers` on a ras-commander that has
  only that; with neither, the terrain goes to RAS Mapper. A polygon with
  more than one ring (holes or separate parts) is refused by the reader
  (ras-commander 0.119.0, code `multi_ring_polygon`) and goes to RAS Mapper
  with `refusal_reason: "multi_ring_polygon"` recorded;
- every staged layer kind, ground-line subtype and elevation type is in the
  engine's qualified list (`effective_terrain.modifications.supported`), and
  no layer is a form the engine refuses (shape-Z polygons without boundary
  elevations, multi-part polygons that need a TIN);
- the modification layers are readable and stored in the terrain HDF (not
  only in the `.rasmap`), HEC-RAS 6.6 projects have an engine that
  advertises `hecras_version`, and the effective terrain is enabled
  (`--fast-map-effective-terrain`, the default) and supported by RasFim;
- the HEC-RAS release of the run resolves to one the engine's modification
  rules are qualified against: **6.6, 6.7 (Beta 4/5), 7.0, 7.0.1 or 7.0.2**
  (`QUALIFIED_MODIFICATION_RUNTIMES`; per-release qualification 2026-11-12,
  0 mask and 0 value differences on every accepted layer). ras2cng resolves
  **one** release per run (`TerrainResolver.hecras_runtime`): `--ras-version`
  when given, else the release ras-commander's `init_ras_project` detects from
  the project's plan files, which is what the export uses. The export,
  Commander's reader and the native build all take it, so the two routes
  never follow different releases. Commander is given the release when it
  maps releases itself (ras-commander after 0.119.0, which picks the rule from
  the engine's advertised table), else the engine rule
  (`MODIFICATION_RELEASE_RULES`: 6.7 -> `6.6`, 7.0.x -> `7.0.1`), never a
  label that 0.119.0 or Fast-Map 0.18.0 would map wrongly or refuse. A
  release before 6.6 or after 7.0.2 (for example 6.5, 6.4.1, 7.1) keeps the
  terrain on the export with `reason_code: "hecras_version_unqualified"`; one
  that cannot be resolved also stays there. The 6.7 betas and 7.0.0 take the
  native route although the export refuses them (prerelease; 7.0.0's known
  modification-export defect), recorded as `export_problem`; if the engine
  then refuses the terrain, the export fallback fails as it always did for
  those releases.

Otherwise RAS Mapper's export builds the terrain (step 3), exactly as before,
and the reason is recorded. An engine refusal on the native route (at build
or mapping time) also falls back to the export. The decision is kept in
`MapResult.fast_map_terrain_route` (per product: `route` is
`native-effective`, `rasmapper-export` or `unmodified` (below), with
`reason`, the staged `layers`, `hecras_runtime` and `hecras_runtime_basis`
(the one release both routes use, and where it came from), `hecras_version`
(the engine rules), any `refusal_reason`, `modification_reader`
(`RasFim.read_terrain_modifications`,
`ras_commander.fim._terrain_mods.read_layers` or `None`) and any `fallback`) and in
`_fast_map_terrain/fast_map_terrain_route.json` under the output folder.
Each product group's entry in `MapResult.fast_map_terrain_route` is its own
snapshot, taken when that group mapped (`product_group`, and `terrain`, the
terrain kind it used), so a later group's `fallback` never appears on an
earlier group's products; the manifest keeps the same per-group snapshots
under `product_groups[<plan HDF>][<group>]`. Each run starts a new manifest:
one left in the folder by an earlier run is removed.

**Modification layers RAS Mapper does not apply.** RAS Mapper (HEC-RAS 6.6,
7.0.1) applies a terrain's HDF modification layers only when its `.rasmap`
layer has an `ElevationModificationGroup` named exactly `Modifications`. When
ras-commander 0.119 reports that RAS Mapper applies none of them
(`RasFim.top_priority_terrain_source(..., rasmap_path=...)` returns
`modified: False` with a `modification_group_misnamed` or
`modification_group_missing` warning), ras2cng maps the terrain as
**unmodified**, as RAS Mapper does: the source TIFF and the unmodified
effective terrain may be used. The route record has `route: "unmodified"`,
the HDF layers under `modifications`, and Commander's warning under
`rasmap_warnings` and `reason`. When Commander cannot tell (no `.rasmap`, an
unreadable terrain), the layers still count. Rust inundation boundaries
still refuse such a terrain (they read the terrain HDF directly).

The native route is qualified through `ras2cng map` (ras-commander 0.119.0,
Fast-Map 0.18.0, HEC-RAS 7.0.1 and 6.6) on Bald Eagle p08 with single-ring
polygons and with rotated ellipses: 0 mask and 0 value differences in the
modified area and in WSE, Depth and Velocity (Max) against two repeat RAS
Mapper exports; the only whole-terrain differences are a few base-terrain
stitch-TIN pixels that the unmodified native build shows too. Multi-ring
polygons go to RAS Mapper with `refusal_reason: "multi_ring_polygon"`. A
terrain whose CRS has no EPSG code (for example ESRI:102630) takes the native
route only when the engine's `effective_terrain` and `sloping` records advertise
`crs_any` (Fast-Map 0.19+) and ras-commander passes it on; otherwise it goes to
the export with the reason recorded (`terrain_crs` in the route record; Fast-Map's own
qualification is in its
`feature_dev_notes/effective_terrain_modifications/modification_rules.md`).
**Effective-terrain values change with `/2` for unmodified terrains too:**
Fast-Map runs one rule, `rasmapper-effective-terrain/2`, for both request
schemas, so schema 1 (no modifications) results from a `/2` engine differ
numerically from a `/1` engine's (its receipts report `/2`). `map-hdf` with
a single raw `--terrain` TIFF passes that TIFF directly.

Measured on a shared, loaded host:

| Run | Time | Terrain |
|---|---|---|
| Bald Eagle bridges p05, WSE + Depth | 4.5 s end to end, no HEC-RAS | priority-1 source TIFF |
| bridges p05, WSE, Depth, velocity, arrival, percent | 16.6 s | reused export |
| bridges p05, same five, nothing supplied | 181 s (≈160 s is the HEC-RAS export; arrival/percent were refused on the source) | source, then export |
| RAS Mapper, bridges p05 | ≈215 s for the seven Max products; ≈26 s for arrival/percent | |
| Davis p02, all five | 5.4 s, no HEC-RAS | CRS-canonical source copy |
| bridges p05 sloping, nine products (adds velocity, D × V, D × V², Froude, duration) | 251 s / 1.5 GB with one output time in flight in the derived pass; the derived pass alone is now 141 s / 2.6 GB | source; effective terrain for arrival/duration/percent |
| RAS Mapper, the same nine | 235 s / 2.4 GB; derived only 206 s / 2.3 GB; the other six 34 s | |
| fast-map, the other six | 25 s / 1.5 GB | source and effective |
| Bald Eagle p07 (storage area), WSE + Depth | 18.7 s / 1.5 GB (RAS Mapper: 12.2 s / 1.2 GB) | native effective terrain (the source TIFF is refused: 28 storage-area pixels outside it) |
| p07, arrival + percent | 31 s (RAS Mapper: 82 s) | native effective terrain |

Every fast-map product in the bridges and p07 rows was compared with RAS
Mapper 7.0.1 stored maps: 0 mask and 0 bit differences (bridges: 18 products
over sloping and horizontal; p07: 1,345,691 and 1,333,183 px).

**Output.** Products are named like RAS Mapper's (`WSE (Max).tif`,
`Depth (Max).tif`, `Velocity (Max).tif`, `D _ V (Max).tif`,
`D _ V^2 (Max).tif`, `Froude (Max).tif`, `Arrival Time (0.5ft hrs).tif`,
`Duration (0.5ft hrs).tif`, `Percent Time Inundated (0.5ft).tif`) on the
terrain's pixel grid, with NaN nodata, next to `fast_map_manifest.json`,
`fast_map_velocity_manifest.json`, `fast_map_derived_manifest.json` and
`fast_map_whole_simulation_manifest.json` (engine version, method, terrain,
storage areas, timings). Arrival time is in hours since the simulation start.
`MapResult.engines` records which engine produced each map type,
`MapResult.fast_map_skipped` why a map type went to RAS Mapper under the
fast-map engine (for example storage areas without the opt-in), and
`MapResult.fast_map_terrain` which terrain fast-map used (`source`,
`source-canonical`, `effective`, `export`, `export-canonical` or `supplied`;
`export-canonical` is the RAS Mapper export's CRS-canonical copy, the same
pixels with EPSG GeoTIFF keys, used only after the engine refuses the
export's CRS identity). Velocity and shear stress are each their own
engine call, so a refusal of either never costs WSE/Depth, and WSE/Depth keep
the native GeoTIFF writer; in horizontal mode they are rendered plain sloping
and clipped with the horizontal depth, as RAS Mapper does. Shear stress is
written as `Shear Stress (Max).tif` with `fast_map_shear_manifest.json`. `--min-depth`, `--wgs84` and
`--cog` apply unchanged. Other options: `--fast-map-threads` (default CPUs − 1)
and `--fast-map-memory-mib` (planned working memory; default a quarter of
RAM, 1–16 GiB).

**Invalid cell polygons.** HEC-RAS can compute 2D cells whose face polylines
cross. With ras-commander 0.110.2 or newer, fast-map maps those meshes under
an explicit policy instead of refusing them:

| `--fast-map-invalid-cells` | Pixel ownership of an invalid cell |
|---|---|
| `repair` (engine default) | polygonize the cell's faces, remove area owned by valid cells, keep the largest part |
| `hull` | the face-point convex hull, with the same overlap removal |
| `omit` | the cell owns no pixels |
| `raise` | refuse; the products fall back to RAS Mapper (`--fast-map-strict` raises) |

Hydraulic connectivity and interpolation always use the complete HDF
topology; only pixel ownership changes. RAS Mapper's own rendering of such
cells has not been qualified against `repair`. Every affected cell (mesh,
cell id, reason, action, original and mapped area) is recorded as
`fim-invalid-cells/1` in `MapResult.invalid_cells`, `fast_map_manifest.json`
and `fast_map_whole_simulation_manifest.json`, and a non-zero count is printed.

**GeoTIFF delivery.** With ras-commander-fast-map 0.11 the engine writes the
sloping WSE/Depth GeoTIFFs itself (terrio, LZW) instead of handing float32
arrays back to Python for rasterio. `--fast-map-writer auto` (default) uses
it when the engine supports it and velocity is not requested;
`native` requires it and `rasterio` keeps the previous path
(`RAS2CNG_FAST_MAP_WRITER` sets the default). On Bald Eagle bridges p05 Max
the two writers gave identical pixels and the same LZW strip layout; native
files were about 4% smaller and the engine call used about 18% less peak
memory. `--cog` converts either through the shared COG policy with identical
results. `MapResult.fast_map_writer` records the writer.

### Inundation boundaries in Rust (recommended with fast-map)

RAS Mapper's inundation-boundary export is very slow. With fast-map (the default engine),
`--inundation-boundary` is mapped by the Rust engine
(`RasFim.map_inundation_boundaries`) whenever the plan is in scope. In
ras-commander's Davis pipes/conduits comparison, the Rust batch for Max plus one
timestep took **3.5 s against 85 s** for the two native RAS Mapper calls. It is
not the default engine, but it is the recommended way to produce boundaries for
pure 2D models.

```bash
# Rust WSE/Depth and boundary; velocity still through RAS Mapper
ras2cng map /path/to/project /output/maps --engine fast-map --inundation-boundary

# Explicit single-source terrain HDF (its TIFF is found beside it)
ras2cng map /path/to/project /output/maps --engine fast-map --inundation-boundary \
    --fast-map-terrain-hdf /path/to/project/Terrain/Terrain.hdf

# Keep boundaries on RAS Mapper while still using Rust rasters
ras2cng map /path/to/project /output/maps --engine fast-map --inundation-boundary \
    --no-fast-map-boundary
```

**The default is a different method, not a copy of RAS Mapper's polygons.** The Rust
engine traces deterministic zero-depth contours
(`deterministic-zero-depth-contours/1`): the same inputs always give
byte-identical GeoJSON, on Windows and Linux and at any worker count. RAS
Mapper's own boundary polygons are order-dependent and can differ between
identical exports, so the two are close but not equal. On Davis Max the Rust
contour had 66 holes and 47,426 vertices, against RAS Mapper's 65 and 47,396.
Almost all of the 7,423 ft² difference was one hole that RAS Mapper filled.
The Depth/WSE rasters are unaffected; they still match RAS Mapper bit for bit.

### The native-serial method (opt-in)

```bash
ras2cng map /path/to/project /output/maps --inundation-boundary \
    --fast-map-boundary-method native-serial
```

`--fast-map-boundary-method native-serial` (`fast_map_boundary_method`,
`RAS2CNG_FAST_MAP_BOUNDARY_METHOD`) asks the engine for its native-serial
contour contract (`rasmapper-serial-zero-depth-contours/1`; since fast-map 0.15.0
and ras-commander 0.116.0). **The engine and ras-commander label this method
unqualified**, and ras2cng requests it with that explicit opt-in. It is not
the default.

What has been checked, and all that should be claimed: on Windows x64, in
plain sloping, with original terrain layouts, the engine's boundary has the
same valid geometry and the same unrounded coordinates (ring start, direction
and part order aside) as the boundary RAS Mapper exports with its **serial**
polygon loop, on the fixtures in ras-commander's
[mapping guide](https://rascommander.info/ras/native-mapping-engine/#compare-native-polygons):
Baxter (three steady profiles), Davis (Max and one saved time, 7.0.1), Bald
Eagle (Max and one saved time, two terrain sources and a storage area) and
Reese (mixed-model Max). Run through `ras2cng map` against RAS Mapper 7.0.1 on
project copies:

| Plan | Polygons / holes / vertices | Against RAS Mapper's serial export | Rust, end to end | RAS Mapper |
|---|---|---|---|---|
| Davis p02 Max | 274 / 65 / 47,272 | geometry and coordinates equal | 4.0 s | 44.6 s (serial) |
| Bald Eagle bridges p05 Max (two terrain sources) | 194 / 344 / 108,189 | geometry and coordinates equal | 8.9 s | 8.5 s (ordinary), 12.4 s (serial) |
| Bald Eagle p07 Max (two sources, one storage area; `--fast-map-storage-areas`) | 117 / 214 / 104,486 | geometry and coordinates equal | 17.2 s | 8.0 s (ordinary), 10.8 s (serial) |

RAS Mapper's ordinary export is not serial, and its polygons have differed
between identical exports. On bridges p05 and p07 the ordinary export happened
to equal the serial one; that is an observation on two plans, not a claim that
the method reproduces every RAS Mapper shapefile. Other models, other
profiles, modified terrains and other platforms are not covered.

**A terrain CRS without an EPSG code** (for example ESRI:102630, whose RAS
Mapper source TIFFs carry no CRS tags) takes both boundary methods when the
engine's boundary methods advertise `crs_any` (Fast-Map 0.19+) and
ras-commander reads the terrain HDF `Projection` for the untagged source (the
release after 0.119.0). The GeoJSON carries GDAL's WKT of that CRS and reads
back exactly. On unmodified Helena (Middle Cahaba p01 Max, 7.0.1) the
native-serial boundary equals RAS Mapper's ordinary export (821 polygons, 320
holes, 712,847 vertices). With older engines or Commander releases the boundary
goes to RAS Mapper with the refusal recorded.

Scope in ras2cng: the Max profile, plain sloping, unmodified terrains. A
terrain with several source TIFFs is accepted (ras2cng passes the first one in
the terrain HDF's priority order; the engine checks it and refuses a wrong
one). Storage areas are included only with `--fast-map-storage-areas`.
Anything else falls back to RAS Mapper. The boundary manifest records
`ras2cng.boundary_method`.

**Extra scope for deterministic boundaries**, on top of the pure-2D rules above (each falls
back to RAS Mapper on its own, so the rasters can still use Rust):

| Condition | Why |
|---|---|
| Terrain with more than one source TIFF | The deterministic method needs the original terrain HDF's masks, perimeters and stitch TIN for a single source. The flattened raster export cannot stand in for them. `--fast-map-boundary-method native-serial` takes every source |
| Terrain modifications | Refused by ras2cng before the engine runs (`terrain_modifications`): boundaries on modified terrains always go to RAS Mapper, even when the raster products use the native effective terrain with modifications |
| `slopingPretty` render mode | Contours support horizontal and plain sloping only |
| ras-commander without `RasFim.map_inundation_boundaries` | Introduced in ras-commander 0.113.0; ras2cng requires 0.119.0 |
| Storage areas | The deterministic method draws 2D cells only; the level pool would be missing. The native-serial method includes them when storage areas are opted in |
| A terrain CRS the engine and rasterio read differently (Davis), without `--fast-map-terrain-crs-hash` | The engine cannot name an EPSG code for it; ras2cng never guesses the identity (see below) |

**User-supplied terrain CRS.** When the engine cannot name the terrain's EPSG
code and reads its WKT differently from rasterio (Davis), the boundary falls
back to RAS Mapper, and the message prints the engine's WKT hash, for example:

```
engine terrain CRS differs from rasterio CRS EPSG:2871; the engine reports
crs_wkt_fnv1a64=e446f7bd6bdb4d7f for Terrain (1).Davis_terrain2.tif ...
```

Check the CRS first (`ras2fim-batch terrain-info <terrain.tif>` prints the
engine's WKT and `crs_wkt_fnv1a64`), then pass the hash:

```bash
ras2cng map /path/to/Davis /output/maps --inundation-boundary \
    --fast-map-terrain-crs-hash e446f7bd6bdb4d7f
```

The Python API takes `fast_map_terrain_crs_fnv1a64=`. It applies to the
boundary terrain only, is used only when the engine reports no EPSG code, and
a value that does not equal the engine's hash is refused. The boundary
manifest records it as `ras2cng.terrain_crs_fnv1a64`.

**Output.** `Inundation Boundary (Max).geojson`: a MultiPolygon with holes, in
the model CRS. RAS Mapper writes a shapefile. It sits next to
`fast_map_boundary_manifest.json`, which records the method, polygon/hole/vertex
counts, area, terrain and timings. A plan with no flooded area writes no
boundary.

## Post-Processing Options

- **Minimum depth threshold** (`--min-depth`): Set pixels below a depth threshold to NoData
- **WGS84 reprojection** (`--wgs84`): Reproject output rasters to EPSG:4326
  (`<name>_wgs84.tif`), with terrio when the installed build has the streaming
  warp (the CLB terrio build) and otherwise with rasterio; the grid is the same
  and values agree to float rounding (`RAS2CNG_REPROJECT_ENGINE` =
  `auto`/`terrio`/`rasterio`). A raster that cannot be reprojected is an error.
  **Before 0.16 this option did nothing**: a wrong import was caught and the
  rasters were returned in the model CRS with a "rasterio not available"
  warning. Rasters published with `--wgs84` from an earlier version are in the
  model CRS; check their CRS and re-run.
- **Cloud Optimized GeoTIFF** (`--cog`): Convert output to COG using `gdal_translate`

## API Reference

::: ras2cng.mapping.MapResult
    options:
      show_source: true

::: ras2cng.mapping.generate_result_maps
    options:
      show_source: true

::: ras2cng.mapping.DEFAULT_LOCAL_MAP_PERFORMANCE
    options:
      show_source: true

::: ras2cng.mapping.MAP_TYPE_VARIABLES
    options:
      show_source: true
