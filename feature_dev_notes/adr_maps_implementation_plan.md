# Implementation Plan: Arrival Time / Duration / Recession (+ Percent Time Inundated) Stored Maps

**Date:** 2026-07-08 · **Status:** IMPLEMENTED same day — both tracks
**Track A** shipped in ras2cng 0.6.0 (`mapping.py`: capability detection + rasmap
pre-injection shim; `--arrival-time --duration --percent-inundated
--arrival-depth` on `map` and `map-hdf`).
**Recession: NOT shipped** (2026-07-08 decision): only RasMapperLib-native map
outputs are produced for now. The derived approach (§3, recession = arrival +
duration) was implemented and validated numerically against the dam-break test
case, then removed pending methodology verification — in particular whether
RASMapper's duration semantics (time to *first* cessation vs total inundated
time under re-wetting) make the sum a defensible recession time. `--recession`
remains on the CLI as a warned no-op. To revisit, implement per §3: pair
arrival/duration rasters by tile suffix, masked-add with rasterio (NoData
propagates), write "Recession (<depth>ft hrs).<tile>.tif" — and first verify
the duration semantics question above against a re-wetting test case.
**Track B** implemented on ras-commander branch `feat/adr-stored-maps`
(worktree `G:\GH\ras-commander-wt-adr`): MAP_TYPES XML-name fixes,
`percent_inundated` type, `extra_attrs` on `_add_stored_map_to_rasmap`, native
`arrival_time/duration/percent_inundated/arrival_depth` params on `store_maps`,
threshold-labeled output globbing; tests in `tests/ras_process_store_maps_adr_test.py`.
**Depends on:** `map_hdf_implementation_plan.md` (implemented; `map-hdf` shipped in 0.6.0)

## 1. Why these are no-ops today

`ras2cng.mapping.PARAM_MAP` maps `arrival_time`/`duration`/`recession` to `None`
("not directly supported by store_maps"), and ras-commander's `RasProcess.MAP_TYPES`
carries **wrong XML names** for them:

| ras-commander MAP_TYPES | Actual RasMapperLib XML name (6.6 & 7.0.1 verified) |
|---|---|
| `'arrival_time': ('arrivaltime', ...)` | **`arrival time`** (with space) |
| `'duration': ('duration', ...)` | `duration` ✓ |
| `'recession': ('recession', ...)` | **does not exist** — no such MapType in RasMapperLib |
| — (missing) | **`fraction inundated`** ("Percent Time Inundated") — free bonus type |

Source: MapTypes string table extracted from `RasMapperLib.dll` (identical in 6.6
and 7.0.1): `arrival time` — "The time (from a specified Start Time) for water to
reach its maximum flood depth"; `duration` — "Length of Time until a location
ceases to be inundated to a specified flood depth"; `fraction inundated` —
"Percent of Time a location is inundated during the simulation".

## 2. Validated stored-map schema (live prototype, BaldEagle p07 dam break)

This rasmap `MapParameters` element generated all three maps on the first try via
`RasStoreMapHelper.exe ... StoreAllMaps` (no project, minimal-inputs scaffold):

```xml
<Layer Name="Arrival Time" Type="RASResultsMap" Checked="True" Filename=".\out\Arrival Time.vrt">
  <MapParameters MapType="arrival time" OutputMode="Stored Current Terrain"
                 StoredFilename=".\out\Arrival Time.vrt"
                 ProfileIndex="2147483647" ProfileName="Max"
                 ArrivalDepth="0.1" />
</Layer>
<!-- likewise MapType="duration" and MapType="fraction inundated" -->
```

Results (72-hr simulation, threshold 0.1 ft):

| Output file | Values | Physically sensible |
|---|---|---|
| `Arrival Time (0.1ft hrs).*.tif` + `.vrt` | 0–63.2 hrs | ✓ (dam-break wave propagation) |
| `Duration (0.1ft hrs).*.tif` + `.vrt` | 0.33–72 hrs | ✓ |
| `Percent Time Inundated (0.1ft).*.tif` + `.vrt` | 0.46–100 % | ✓ |

Notes:
- `ArrivalDepth` (the wet/dry threshold, model vertical units) is the one map-specific
  attribute; it appears in the output filename (`(0.1ft hrs)`). Default 0.
- Output TIFFs carry correct CRS + geotransform out of RasMapperLib 6.6 — no
  georeferencing fix required (verified with rasterio; `_fix_georeferencing` remains
  belt-and-suspenders parity, not a necessity).
- ADR types trigger creation of `PostProcessing.hdf` (214 MB observed) — the
  existing `keep_postprocessing=False` cleanup already handles it.
- Optional attributes discovered in the binary for later exposure (start-time
  control, date display): `ArrivalStartMode`, `ArrivalAbsDateTime`,
  `ArrivalSimDays/Hours/Minutes`, `ArrivalStartProfile`, `ArrivalEndProfile`,
  `ArrivalAsDelta`. None are required; defaults are sane (start = sim start).

## 3. Recession: derive, don't map

There is no recession MapType. Per the MapTypes descriptions, duration runs from
arrival until the location "ceases to be inundated", so:

```
recession_time = arrival_time + duration        (per-pixel raster algebra)
```

Implement as a rasterio post-processing step in ras2cng (both rasters share the
terrain grid — element-wise add, propagate NoData). Document the caveat: for
locations that re-wet after first recession, RASMapper's duration semantics govern
(time to *first* cessation), so derived recession is time of first recession.

## 4. Two implementation tracks

### Track A (short-term, ras2cng-only — works with current ras-commander)

`RasProcess.store_maps()` runs `StoreAllMaps` on the **whole rasmap** and its
move-loop relocates **every** file created/changed in the ShortID folder (verified:
it moved PostProcessing.hdf and inundation shapefiles). It backs up the rasmap and
restores it in a `finally`. Therefore ras2cng can:

1. **Pre-inject** ADR `<Layer Type="RASResultsMap">` entries into the rasmap for the
   target plan HDF (new helper in `mapping.py`, mirroring ras-commander's
   `_add_stored_map_to_rasmap` schema + `ArrivalDepth` attr) *before* calling
   `store_maps`. Injected entries ride along in the same StoreAllMaps execution; the
   restore-from-backup afterwards is harmless (scaffold rasmaps are disposable; for
   `map` on real projects the restore actually cleans up after us — a feature).
2. **Collect** outputs by glob after the run: `Arrival Time (*` / `Duration (*` /
   `Percent Time Inundated (*` with `.tif` (mirror of the shipped
   inundation-boundary glob fix). Attach under `arrival_time` / `duration` /
   `percent_inundated` keys in `MapResult.map_types`.
3. **Derive recession** (§3) when `recession=True`: require arrival + duration
   (auto-enable them), then write `Recession (<depth>ft hrs).tif`.
4. Wire `PARAM_MAP` entries from `None` to the new injection path; delete the
   "not yet supported" warning added in 0.6.0; expose `--arrival-depth FLOAT`
   (default 0.0) on `map` and `map-hdf`; add `--percent-inundated` flag.

Estimated: ~120 LOC in `mapping.py` + ~30 in `cli.py` + tests (~150).

Tests (mocked, repo convention): injected XML structure (parse the rasmap the
helper would receive — patch `RasProcess.store_maps` and inspect the rasmap at
call time); glob collection; recession algebra on synthetic rasters (rasterio in
tmp_path — tiny 4×4 grids, NoData propagation); CLI flag wiring. Gated live test:
Muncie 1D (fast) asserting arrival ≤ duration ≤ sim duration.

### Track B (proper fix, upstream ras-commander — CLB-owned)

1. Correct `RasProcess.MAP_TYPES`: `'arrival_time': ('arrival time', 'Arrival Time', False)`;
   drop or re-implement `'recession'` as a derived product; add
   `'percent_inundated': ('fraction inundated', 'Percent Time Inundated', False)`.
2. Extend `_add_stored_map_to_rasmap` with an optional `extra_attrs: dict` param
   (writes `ArrivalDepth` etc. onto `MapParameters`).
3. Add `arrival_time=False, duration=False, percent_inundated=False,
   arrival_depth=0.0` params to `store_maps`; append to `maps_to_add`; the
   existing display-name glob then picks the files up automatically — BUT the
   glob pattern `f"{display_name} ({safe_profile})*.tif"` assumes the profile in
   the label; ADR labels use `({depth}ft hrs)` instead. Use pattern
   `f"{display_name} (*" + "*.tif"` for these types.
4. ras2cng then deletes its Track-A injection shim and passes flags straight
   through (keep the glob fallback for older ras-commander pins).

Recommended sequence: **Track A now** (unblocks LWI without a ras-commander
release), file the ras-commander issue with this doc attached, **Track B** in the
next ras-commander minor, then simplify ras2cng.

## 5. Open questions (small, non-blocking)

- Output time units: filenames say `hrs`; check whether `ArrivalSimDays`-style
  attrs switch label/units (affects only the glob, which uses a wildcard).
- SI-unit models: threshold label presumably `(0.1m hrs)` — glob unaffected;
  verify `ArrivalDepth` is interpreted in model units (assumed).
- `ArrivalStartMode`/`ArrivalAbsDateTime` semantics — expose later as
  `--arrival-start` if users need arrival referenced to an event time (e.g.,
  levee breach) rather than simulation start.
- Whether `fraction inundated` respects `ArrivalDepth` only or also a range —
  label `(0.1ft)` suggests depth-threshold only.

## 6. Test evidence

Prototype artifacts (session scratchpad, 2026-07-08): `minimal_test/adr_test.rasmap`,
outputs in `minimal_test/SA-2DDETFEQP00001/` (`Arrival Time (0.1ft hrs)`,
`Duration (0.1ft hrs)`, `Percent Time Inundated (0.1ft)` TIFF+VRT sets), validated
value ranges above. Binary-analysis evidence: MapTypes string table + Arrival*
attribute names from `RasMapperLib.dll` 6.6/7.0.1.
