# Implementation Plan: `ras2cng map-hdf` — Stored Maps from Plan HDF + Terrain TIFF

**Date:** 2026-07-07 · **Feasibility:** proven (see `minimal_inputs_map_pipeline_2026-07-07.md`)
**Branch target:** follow-on to `feat/lwi-pipeline-gaps`

## 1. Summary

New CLI command that generates RASMapper stored maps from a **plan HDF + raw terrain
TIFF(s)** (or a pre-built terrain HDF sidecar set), with no HEC-RAS project required.
Internally: synthesize a barebones project scaffold from plan-HDF attributes, build the
terrain headlessly via `RasProcess.exe CreateTerrain`, then delegate to the existing
`generate_result_maps()` unchanged.

```
ras2cng map-hdf RESULTS.p01.hdf OUTPUT_DIR --terrain dem.tif [--terrain dem2.tif]
ras2cng map-hdf RESULTS.p01.hdf OUTPUT_DIR --terrain-hdf Terrain.hdf     # skip build
```

## 2. Validated test matrix (2026-07-07, this machine)

| Test | Result |
|---|---|
| Direct helper, no project at all (2D dam break, 460 MB HDF, RAS 6.6) | PASS (~15 s) |
| Same rasmap/HDF against 7.0.1 RasMapperLib | PASS |
| Barebones scaffold through `generate_result_maps()` (full ras2cng path) | PASS |
| All 7 raster map types (WSE/Depth/Vel/Froude/Shear/D×V/D×V²) | PASS (RasMapperLib builds 640 MB `PostProcessing.hdf` for derived types) |
| Inundation boundary shapefile (`Stored Polygon Specified Depth`) | PASS (generated; **not reported** by ras2cng result collector — fix below) |
| Timestamp profile (`02JAN1999 00:00:00`) | PASS (physically consistent vs Max) |
| `sloping` / `slopingPretty` render modes | PASS |
| 1D-only model (Muncie XS + storage areas, hyphenated ShortID `9-SAs`) | PASS |
| Arbitrary input HDF filename → hardlink to canonical name | PASS |
| Headless `CreateTerrain` from raw TIFF(s) — 2 models, multi-TIFF stitch | PASS (~1–2 min / 180 MB) |
| Pre-built terrain sidecar reuse | PASS |
| Arrival time / duration / recession | Already unsupported in `mapping.py` (`PARAM_MAP → None`) — pre-existing, out of scope |

Prototype code: session scratchpad `scaffold_proto.py` (generalized scaffold, working) and
`barebones_test.py`.

## 3. New module: `ras2cng/scaffold.py`

### 3.1 `PlanHdfMetadata` (dataclass)

```python
@dataclass
class PlanHdfMetadata:
    project_name: str      # from "Plan Filename" attr, e.g. "Muncie.p01" -> "Muncie"
    plan_number: str       # "01"
    plan_title: str
    plan_short_id: str     # output folder name RasMapperLib will use
    project_title: str
    geom_ext: str          # "g04" (referenced file need not exist)
    flow_ext: str          # "u01"
    units: str             # "US Customary" | "SI Units" (root attr "Units System")
    projection_wkt: Optional[str]   # root attr "Projection"; may be absent
    sim_start: str         # "01Jan1999 12:00:00"
    sim_end: str
    file_version: str      # root attr "File Version" (for diagnostics)
```

`read_plan_hdf_metadata(plan_hdf: Path) -> PlanHdfMetadata`
- h5py read of root attrs + `Plan Data/Plan Information` attrs.
- Validate: root attr `File Type == "HEC-RAS Results"`, `Results` group present —
  friendly error otherwise ("not a computed plan HDF").
- `Plan Filename` attr may carry a foreign absolute path (compute-machine path) — take
  `Path(...).name`, parse with `re.match(r"(.+)\.p(\d{2})$", stem)`. Fallback if attr
  missing/unparseable: derive from the input filename if it matches `*.pNN.hdf`, else
  error asking for a properly attributed HDF.
- `projection_wkt` may legitimately be absent (rare) → `None`; CLI then requires
  `--projection <file.prj>`.

### 3.2 `build_scaffold(...)`

```python
def build_scaffold(
    plan_hdf: Path,
    workdir: Path,
    *,
    terrain_tifs: Optional[list[Path]] = None,   # exactly one of these two
    terrain_hdf: Optional[Path] = None,
    projection_file: Optional[Path] = None,      # overrides HDF WKT
    render_mode: str = "sloping",
    ras_version: str = "6.6",
    rasprocess_path: Optional[Path] = None,
) -> ScaffoldInfo:   # {project_dir, prj_file, meta}
```

Steps (all proven in prototype):
1. `workdir.mkdir(parents=True)`; error if non-empty unless it's a previous scaffold
   (marker file `.ras2cng-scaffold`, enables terrain reuse across reruns).
2. **Plan HDF** → canonical `{project_name}.p{NN}.hdf` via `os.link()`, fall back to
   `shutil.copy2` on `OSError` (cross-volume). Never move/mutate the user's input.
3. **Projection** → `Terrain\Projection.prj` (inside Terrain/ to avoid the
   `resolve_project_path` multiple-`.prj` collision). Source: `--projection` file if
   given, else HDF WKT, else error.
4. **Terrain**:
   - `terrain_tifs` given → `RasTerrain.create_terrain_from_rasters()` (reuse the
     existing call pattern from `terrain.py:243` — it wraps CreateTerrain with the
     GDAL-isolated child env and stub-HDF validation). `units=` mapped from metadata
     (`US Customary`→`Feet`, else `Meters`).
   - `terrain_hdf` given → validate the sidecar set, then copy/hardlink the whole set
     into `Terrain\`: the `.hdf`, sibling `{stem}.vrt`, and every tile file named by the
     `File` attr of each child of the HDF's `/Terrain` group (bare same-dir filenames —
     verified structure). Missing member → error listing exactly which files are needed.
5. **Text stubs** (all content from metadata; ~11 lines total):
   - `{name}.prj`: `Proj Title`, `Current Plan=pNN`, units line, `Geom File=`,
     `Unsteady File=`, `Plan File=pNN`.
   - `{name}.pNN`: `Plan Title`, `Short Identifier`, `Simulation Date=DDMMMYYYY,HHMM,...`,
     `Geom File`, `Flow File`.
   - `{name}.uNN` + `{name}.gNN` **1-line stubs** (`Flow Title=...` / `Geom Title=...`) —
     purely to silence the noisy-but-harmless `RasPrj` ERROR logs observed in testing.
6. **`{name}.rasmap`** via `xml.etree.ElementTree` (not string templates):
   `<Version>2.0.0</Version>`, `RASProjectionFilename` → `.\Terrain\Projection.prj`,
   empty `<Results Checked="True" Expanded="True" />` (store_maps populates it),
   `<Terrains>` layer → `.\Terrain\{TerrainName}.hdf`, `<Units>`, `<RenderMode>`.
7. Write `.ras2cng-scaffold` marker (JSON: source HDF path, mtime, terrain inputs) for
   safe reuse/cleanup decisions.

### 3.3 Design decisions

- **Workdir default:** `{output_dir}/_scaffold`. Rationale: user-visible (inspectable
  rasmap for debugging), on the output volume (plan HDF hardlink may fall back to copy —
  acceptable), and cleaned with `--rm-scaffold`. `--workdir` overrides. Default is
  **keep** (terrain build is the expensive step; reruns reuse it via the marker check).
- **No template plans needed** — text stubs are smaller and fully derived from the HDF.
  ras-commander's clone/template APIs operate within existing projects; not applicable.
- **Multi-plan:** one scaffold per plan HDF. If multiple HDFs share a project name later,
  scaffolds can merge — out of scope for v1 (`map-hdf` takes exactly one HDF).

## 4. CLI: `map-hdf` command (`cli.py`)

Mirror `map` options, replacing the project argument:

```python
@app.command("map-hdf")
def map_hdf_command(
    plan_hdf: Path = typer.Argument(..., help="Computed plan HDF (*.pNN.hdf)"),
    output: Path = typer.Argument(..., help="Output directory for result rasters"),
    terrain: Optional[list[Path]] = typer.Option(None, "--terrain", help="Raw terrain GeoTIFF (repeatable; stitched)"),
    terrain_hdf: Optional[Path] = typer.Option(None, "--terrain-hdf", help="Pre-built HEC-RAS terrain HDF (sidecar .vrt/.tifs must sit beside it)"),
    projection: Optional[Path] = typer.Option(None, "--projection", help="ESRI .prj override (default: from plan HDF)"),
    workdir: Optional[Path] = typer.Option(None, "--workdir", help="Scaffold directory (default: OUTPUT/_scaffold)"),
    rm_scaffold: bool = typer.Option(False, "--rm-scaffold", help="Delete scaffold after run"),
    # ... then identical passthroughs to `map`: profile, map-type flags, render-mode,
    # ras-version, rasprocess, min-depth, wgs84, cog, timeout, skip-errors
)
```

Validation: exactly one of `--terrain` / `--terrain-hdf`; plan HDF exists and passes
`read_plan_hdf_metadata`. Then:

```python
info = build_scaffold(...)
generate_result_maps(info.prj_file, output, plans=[f"p{info.meta.plan_number}"], ...)
```

Pass the **specific `.prj` file** (not the directory) — sidesteps any `.prj` ambiguity.
No `--plans` option (single plan implied). Everything downstream (profile, map types,
WGS84/COG post-processing, per-plan output subdir) is reused untouched.

## 5. Small fixes riding along

1. **Inundation-boundary reporting gap** (`mapping.py` / result collection): shapefile is
   generated but absent from `MapResult.map_types` because collection only tracks TIFFs
   returned by `store_maps`. Fix: after `_generate_plan_maps`, glob
   `output_dir/*.shp` (+ `.dbf/.prj/.shx`) when `inundation_boundary=True` and attach
   under `"inundation_boundary"`. (Check first whether ras-commander's return dict is the
   right place — if `store_maps` already returns shapefile paths under a key ras2cng drops,
   fix the `REVERSE_MAP` instead.)
2. **`PostProcessing.hdf` handling**: derived map types leave a large (can exceed the plan
   HDF size — 640 MB observed) `PostProcessing.hdf` in the ShortID folder that
   `store_maps` moves to the output dir. Add cleanup flag `--keep-postprocessing`
   (default: delete from output; it's a cache, not a deliverable) — applies to `map` too.
3. **Docs note for `map`**: arrival/duration/recession flags currently no-op
   (`PARAM_MAP → None`). Either hide the CLI flags or emit a visible warning until
   implemented. (Full support = extra `MapParameters` attrs in the rasmap; separate
   feature.)

## 6. Testing strategy (repo convention: mock ras-commander, no real models)

`tests/test_scaffold.py`:
- **Metadata reader**: build tiny synthetic plan HDFs with h5py in `tmp_path`
  (root attrs + `Plan Data/Plan Information`) — happy path; missing `Projection`;
  foreign absolute `Plan Filename`; unparseable plan filename; not-a-results-file.
- **Scaffold builder** (terrain calls mocked): file set + exact stub contents (golden
  strings); rasmap parses via ElementTree and references resolve relative to scaffold;
  hardlink→copy fallback (mock `os.link` to raise `OSError`); non-empty workdir without
  marker → error; rerun with marker reuses terrain (CreateTerrain not called again).
- **Terrain sidecar validation**: synthetic terrain HDF with `/Terrain/<layer>` `File`
  attrs; complete set passes, missing tile/vrt errors with the file list.
- **CLI wiring** (`tests/test_cli.py` additions): `--terrain` xor `--terrain-hdf`
  enforced; passthrough kwargs reach `generate_result_maps` (mock it).
- **Integration (skipif-gated)**: `@pytest.mark.skipif(not RAS66_INSTALLED or not
  EXAMPLE_HDF.exists())` — end-to-end on the Muncie 1D HDF (small/fast, ~10 s total)
  asserting nonzero valid pixels via rasterio. Keeps CI green off-Windows while giving a
  real smoke test on dev machines.

## 7. Documentation

- `docs/api/cli.md`: `map-hdf` section (arguments table, both terrain modes, example).
- `docs/user-guide/overview.md`: "Maps without a project" subsection — when to use
  `map-hdf` vs `map` (LWI deliverable-only inputs: plan HDF + terrain TIFF).
- `CLAUDE.md`: add `scaffold.py` to Core modules list; note the minimal-inputs pipeline
  and the `Terrain\Projection.prj` placement rule.
- `docs/api/mapping.md`: note `PostProcessing.hdf` behavior and cleanup flag.

## 8. Phases / sequencing

| Phase | Deliverable | Files | Est. |
|---|---|---|---|
| 1 | `scaffold.py`: metadata reader + scaffold builder + unit tests (terrain mocked) | `ras2cng/scaffold.py`, `tests/test_scaffold.py` | ~250 LOC + ~250 test |
| 2 | Terrain integration: CreateTerrain path (reuse `RasTerrain.create_terrain_from_rasters`), sidecar validation/copy | `scaffold.py` (+ small `terrain.py` helper extraction if shareable) | ~80 LOC |
| 3 | CLI `map-hdf` + pass-specific-prj change + inundation reporting fix + PostProcessing cleanup | `cli.py`, `mapping.py` | ~150 LOC |
| 4 | Docs, gated integration test, version bump (0.5.x → 0.6.0: new command) | docs, `pyproject.toml`, `__init__.py` | — |

Dependencies: none new (h5py already transitively present via ras-commander; confirm it's
a declared direct dep — if not, add). ras-commander ≥ current pin suffices (all APIs used
exist in 0.93.0).

## 9. Risks & open questions

- **HDF versions older than ~6.x**: `Plan Data/Plan Information` attr names may differ in
  5.x results HDFs. Mitigation: validate attrs and fail with a clear message; 6.x+ is the
  LWI target anyway.
- **Cross-version render**: 6.6 helper exe worked against 6.6 and 7.0.1 RasMapperLib; the
  helper ships with ras-commander so new RAS versions depend on RasMapperLib keeping the
  `Scripting.StoreAllMapsCommand` reflection surface (present in 7.0.1; re-verify per release).
- **Terrain coverage**: mapping silently clips to terrain extent — if the supplied TIFF
  doesn't cover the mesh, outputs are partial. Cheap guard: compare terrain VRT bounds vs
  mesh bbox from the plan HDF (`Geometry/2D Flow Areas/.../Cells Center Coordinate` or the
  simpler `2D Flow Area` perimeter attrs) and warn.
- **Projection mismatch**: TIFF CRS ≠ HDF WKT → CreateTerrain reprojects per its prj arg;
  warn when rasterio-reported TIFF CRS differs from HDF EPSG.
- **Wine/Linux**: `mapping.py` already has a wine branch; scaffold approach should carry
  over (CreateTerrain under wine untested). Out of scope for v1; note in docs.
- **Timestamp profiles** require the mapping-interval outputs to exist in the HDF
  (`get_plan_timestamps` returns [] otherwise) — existing behavior, clear warning already
  logged by ras-commander.
