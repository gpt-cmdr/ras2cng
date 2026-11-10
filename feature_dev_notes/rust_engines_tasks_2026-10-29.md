# Rust engines task list (2026-10-29)

Source: `rust_native_assessment_2026-10-29.md` (ranked opportunities, all approved by the user).

User decisions (2026-10-29):
- **Option A**: ras2cng stays the Python orchestrator. Heavy work moves into Rust engines. End-to-end
  Rust engines, or additions to them, are welcome where they fix a significant bottleneck.
- **Use the gpt-cmdr terrio fork now**, and develop and maintain it ourselves. This lifts the
  2026-10-28 "wait for the upstream terrio HEC-RAS driver" hold on the direct archive path.
- **freestiler fork**: close the 40-50% archive size gap vs tippecanoe. **Pure Rust only.**
- **Storage-area mapping in fast-map**: approved.
- **All work is done by subagents.** Each agent writes its report to
  `feature_dev_notes/rust_engines/<task-id>.md`; the coordinator updates this list.

## Rules for every task

1. Environment, toolchain, test data and git/PR rules: `G:\RustDev\BRIEF.md` (source `env.ps1`/`env.sh`
   first). `CARGO_BUILD_JOBS=1` (eight agents share 8 cores). Own target dir
   `G:\RustDev\target\<task-id>` and own venv `G:\RustDev\venvs\<task-id>`. Never use
   `H:\CLB-Repos\ras2cng\.venv` (stale, ras-commander 0.93.0).
2. **New worktrees only**: `G:\RustDev\src\<task-id>-<repo>`. Do not modify, reset or delete any
   existing worktree or branch under `G:\RustDev\src` (other threads, including the CLB01 coordinator,
   use them). Branch from the fork's up-to-date `main`, or from a named existing branch.
3. **Forks only.** Push branches to the gpt-cmdr forks and open **draft** PRs there. No PRs, issues,
   comments or `owner/repo#N` references on opengeos/terrio, walkerke/freestiler, roteiro-gis or any
   other upstream. No merges to `main`, tags, or PyPI/crates.io releases: those need the user's approval.
4. `H:\CLB-Repos\ras2cng` is written **only by the one ras2cng task running at that time** (`clbgit`
   with a lease, branch named in the task). Every other task treats it as read-only.
5. Evidence: measured before/after (time, peak memory, size) and equivalence against the existing output
   (row/pixel/byte identity, or a stated tolerance). Report failures and gaps plainly.
6. Commit messages end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Wave 1 (parallel)

| ID | Opportunity | Repo(s) | Scope | Status |
|---|---|---|---|---|
| T01 | quick wins, #9 | ras2cng | ras-commander floor 0.110.3; record the `invalid_cells` report in `MapResult`/manifests; adopt fast-map 0.11 retained state and native TIFF delivery where it helps (capability-detected); tests | **done** (branch `feat/rust-engines-wave1`, local commits 261ee79d..5a7bd9c5; 569 passed / 22 skipped; retained state not adopted, see `rust_engines/T01.md`) |
| T02 | #1, #2 engine | fast-hdf | Arrow layer API (geometry layers and plan summary results) with the canonical catalog in terrio naming and an alias table to the ras2cng v2 names; continue `feat/hdf-arrow-layers` | **done**: fast-hdf #52 (draft, supersedes #41; Windows CI queued at handback); 0 differences in 386 comparisons vs ras-commander 0.110.3; g01 1.64 s -> 0.13 s, 891 -> 136 MB; `Cell Last Iteration` refused (no stored Location); driver notes for T10 in `rust_engines/T02.md` |
| T03 | #1 formats | terrio fork | GeoParquet archive profile as generic writer options: Hilbert order + `hilbert_index`, ZSTD level, layer column with row groups ending at layer boundaries, bbox struct covering, Utf8-only dictionaries, chunk statistics, union schema with non-null identity columns | **done**: terrio #27 (draft; CI pending at handback); 16/16 p07 files content-identical to pyarrow and fast-hdf paths (not byte-identical); 19.9 s vs 30.2 s pyarrow / 36.7 s fast-hdf, half pyarrow's peak memory; API notes for T10 in `rust_engines/T03.md`. **Reopened (user):** the writer was ported, leaving two copies on Arrow 58/59. Fix: align terrio to Arrow 59, keep ONE implementation (retire fast-hdf's writer, or one shared crate only if forced), and move ras2cng's native GeoParquet engine to terrio. **Fixed**: terrio #27 on Arrow/Parquet 59 (one parquet in the build), fast-hdf #53 removes its archive writer (breaking, 0.3.0), terrio #28 moves to crates.io hdf5-reader 0.10.1 (same as fast-hdf); ras2cng `8d767095`..`9ddd9367`, 578 passed / 4 skipped; terrio fork Actions not running since 2026-10-29 23:25Z, so #27/#28 lack CI |
| T04 | #7, #8 | terrio fork | COG report in the default metadata domain (finish `feat/cog-report-default-domain`), so ras2cng can drop `_scan_area_matched`; confirm bounded warp/mosaic and every ras2cng-used feature is on fork `main` (list what is not); document a reproducible fork wheel build | **done**: terrio #23 (draft, CI green; `claude-review` lacks a fork secret); scan removal ~halves COG write (Terrain50 17-22 s -> 10-11 s), tags identical; capability table, merge order and wheel/dependency plan in `rust_engines/T04.md`; fork main Windows CI red (tile-index path separators); fork version collides with PyPI 0.5.0 |
| T05 | #3 | freestiler fork | close the 40-50% size gap vs tippecanoe on HEC-RAS meshes (pure Rust; start from `feat/mesh-size-levers`); Arrow/GeoArrow input with reprojection to EPSG:4326 so ras2cng can skip NDGeoJSON | **done**: freestiler #5 (draft, CI green); `buffer=5` + float32 values: cells +1.5%, results -4.4%, faces -15.5% vs tippecanoe, 100% completeness; Arrow input + proj4rs reprojection (<=73 nm vs PROJ; HARN/NAD27 refused, Davis needs Python reprojection); Arrow 59; R vendor tarball not refreshed; see `rust_engines/T05.md` |
| T06 | #4 | fast-map, ras-commander | storage-area mapping: restack fast-map #10 on `main`, qualify against RAS Mapper 7.0.1 (p07 and at least one other SA model), wire through `RasFim` | **done**: fast-map #18, ras-commander #757 (draft, green); 0 px / 0 bit diffs vs RAS Mapper 7.0.1 on p07 + p04; capability `storage-area-level-pool/1`; see `rust_engines/T06.md` |
| T07 | #5 | fast-map (+ ras-commander) | native effective terrain: multi-source mosaic and terrain modifications without HEC-RAS (fast-map #6/#11); research with a qualified result or a documented reason it cannot match | **partial**: fast-map #19 (draft, green) multi-source effective terrain, 0 mask diffs, 8 / 197 M px differ on Terrain50 (seam/nodata corners, where RAS Mapper is itself inconsistent), 12.4 s vs ~190 s; modifications refused (not implemented); RasFim wiring not done; see `rust_engines/T07.md` |
| T08 | #6, #10 | fast-map, ras-commander | inundation boundary release readiness (ras-commander #709, fast-map #5); velocity default-on readiness, duration qualification, Froude / D x V / shear, Max derived products via a time-series pass | **done except shear**: ras-commander #755 (boundary, merge-ready), fast-map #20 + ras-commander #758 (velocity all modes, duration qualified, D x V / D x V^2 / Froude incl. Max via `reduce_max`); slopingPretty velocity mask gap; awaits user approval to merge/release; see `rust_engines/T08.md` |

## Wave 2 (after wave 1; ras2cng tasks run one at a time)

| ID | Opportunity | Depends on | Scope | Status |
|---|---|---|---|---|
| T09 | #11 | T04 | Manning's raster and precipitation rasters in Rust (fast-hdf read + terrio write) | **done**: fast-hdf #56, ras-commander #786 (draft, CI green); Manning's 0 px vs RAS Mapper 7.0.1 on Bald Eagle g01/g09/g11/g13 + Weise_2D (~2 s vs 45-49 s); precipitation 0 px vs ras2cng on p06. **Today's `ras2cng mannings` differs from RAS Mapper on 44-51% of common pixels**, so switching changes output: user decision pending. 1D geometries refused; see `rust_engines/T09.md` |
| T10 | #1 engine | T02, T03 | terrio HEC-RAS driver on the fast-hdf Arrow layer API, writing the archive profile | **done with gaps**: terrio #29, fast-hdf #58 (draft); p07 7.1-7.8 s vs 41.3-43.3 s, peak 218 vs 959 MB, 119/119 shared layers equal; no terrio CI; Spring p01, Spring_BLE p02 and g01 fail the h5py comparison; v2-only layers and columns listed in `rust_engines/T10.md` |
| T11 | #1, #2 | T10 | ras2cng archive manifest 3.0 direct path (geometry and `--results`), Python path kept as fallback and oracle | **done**: ras2cng `41419d69` (trailer missing, clbgit dropped it), `46c5093b` (local); p07 12.5 s vs 29.1 s, ~0.7 vs ~4.0 GB; 210/210 layers equal on five models; plan files drop all-null columns; 633 passed / 4 skipped; needs terrio #29 released; see `rust_engines/T11.md` |
| T12 | #3 | T05 | ras2cng viewer tiles fed as Arrow to freestiler; tiler preference revisited once sizes are measured | **done**: ras2cng `182d807d`, `c5811aa7` (local); Bald Eagle with results 36-40 s vs 1,745 s tippecanoe, 0 decoded differences vs GeoJSON route; sizes +5..+13.5% vs tippecanoe with feature IDs on, -10..+3.3% off (decision pending); 597 passed / 4 skipped; needs an unreleased freestiler; see `rust_engines/T12.md` |
| T13 | #4-#8 | T04, T06-T08 | ras2cng integration: storage areas, native terrain, boundary, new products, COG report, bounded warp/mosaic | **done with gaps**: ras2cng `efaac393`, `ef0323e6`, `8691dc9d`, `1fdb329c` (local); engine branches `t13/integration` pushed on fast-map (#18-#20 + test fix `ace18dc`) and ras-commander (#755/#757/#758 + terrain wiring), new draft ras-commander #788 (effective terrain in `RasFim`); every new product 0 mask / 0 bit vs RAS Mapper 7.0.1 (bridges p05 18/18, p07 storage areas); COG pre-scan removed (Terrain50 13-14 s -> 6-7 s, byte-identical); storage-area WSE/Depth slower than RAS Mapper (18.7 vs 12.2 s); bounded warp/mosaic NOT adopted (values differ, no memory gain); Davis boundary still RAS Mapper (CRS hash); 669/14 or 673/10 passed/skipped depending on terrio build, no failures; decisions pending; see `rust_engines/T13.md` |

## Wave 3

User decision (2026-10-30): native effective terrain "is only viable if it also simulates terrain
modifications".

User decision (2026-10-31), after T14: **Rust terrain operations only when the terrain has no
modifications; any project with terrain modifications falls back to RAS Mapper.** ras2cng enforces it
itself (`terrain.terrain_modifications`, commit `13ca5185`): a modified or unreadable terrain skips the
source TIFF and the effective terrain (RAS Mapper's export builds it) and the Rust boundary is refused.
The modification work is **parked** on fast-map `feat/effective-terrain-modifications` (#21) and
ras-commander `feat/fim-effective-terrain-modifications` (#790), both titled "[parked]", with docs,
findings and roadmap in `H:\CLB-Repos\ras-commander-fast-map\feature_dev_notes\effective_terrain_modifications`
(new checkout of the fork on that branch, commit `844c792`; the client corpus key there is git-excluded).

| ID | Opportunity | Depends on | Scope | Status |
|---|---|---|---|---|
| T14 | #5 phase 2 | T07, T13 | terrain modifications in fast-map effective terrain, one type at a time, qualified against RAS Mapper 7.0.1 exports on real RAS-Mapper-authored corpora (HCFCD Prop_Topo, Kingswood, NGOM, UPGU1); fix ras-commander modification-writer bugs; viability verdict | **done, not viable yet**: fast-map #21, ras-commander #790 (draft, CI green). 14 modified terrains found (all levees/channels, 8 polygons, 2 ellipses). Levees/channels, constant polygons, unrotated ellipses reproduced; modified pixels exact on Prop_Topo, Kingswood Alt3/existing_1m, 3 px at 1/32 ft on Alt1, 433 px unexplained on Alt2. 0 of 11 runnable terrains whole-terrain exact: 6 refused (control points, varying-edge polygons, System Name groups, additive levees, Max Reach <= Top Width), 5 differ in the T07 base mosaic (seams, nodata, 1/32 ft), reproducible with modifications off. ras-commander writer bugs fixed (channel type, polygon elevation 0, priority). 12-17x faster than export, ~5x memory. Next: fix base mosaic rule first. **Parked 2026-10-31.** See `rust_engines/T14.md` |

## Release (2026-10-31)

User decisions: storage areas **not** a ras2cng default until faster than RAS Mapper; derived products
on by default; `ras2cng mannings` switches to the Rust raster; vector-tile feature IDs off by default;
user-supplied terrain CRS option for Rust boundaries; fork wheels (terrio, freestiler) get a 4th version
component (e.g. 0.5.0.01 = 0.5.0.1) and ship as GitHub Release assets on the forks only. GitHub account
limits fixed; CI also run locally. Merge and release approved. Shared rules: `G:\RustDev\RELEASE_BRIEF.md`.

| ID | Repo | Scope | Depends on | Status |
|---|---|---|---|---|
| R1 | fast-hdf | #52, #58, #53 (breaking), #56 -> 0.3.0 (PyPI, crates.io); close #41 | - | **done (PyPI, GitHub Release, crates.io)**: crates.io published 2026-10-31 by a one-off workflow on a temporary orphan branch of fast-map (deleted after) using fast-map's crates-io environment token, since the user is away; fast-hdf #64 adds its own crates job (needs CARGO_REGISTRY_TOKEN in its crates-io environment for future releases); tag `v0.3.0` (3a9e35e) published 2026-10-31 by a workflow_dispatch rebuild (run 41227209816) after the macos-14 brownout; Windows, manylinux, musllinux and macOS arm64 wheels. #62 (macOS on macos-15 + tag-rebuild dispatch) and #63 (env-block fix; merged before its Windows CI finished, a coordinator slip) merged. Blacksmith unusable: gpt-cmdr is a user account. See `rust_engines/R1.md` |
| R2 | terrio | #28, #27, #29, #23 + #30 | R1 | **done**: released on new non-fork repo `CLB-Engineering-Corporation/terrio-clb` (Actions refused on the fork repo itself), tag `gpt-cmdr-v0.5.0.1`, Windows + manylinux wheels; fast-hdf from crates.io 0.3.0; all checks green before each merge; release-workflow publishing guarded to opengeos/terrio; old fork PRs closed with pointers; Pyodide wheel fails (pre-existing). Install: `pip install terrio==0.5.0.1 --find-links https://github.com/CLB-Engineering-Corporation/terrio-clb/releases/expanded_assets/gpt-cmdr-v0.5.0.1`. See `rust_engines/R2b.md` |
| R3 | fast-map | #18, #19, #20 + `ace18dc` -> 0.12.0 (PyPI, crates.io); close #10; never #21 | - | **done**: 0.12.0 on GitHub, PyPI, crates.io (tag `v0.12.0` at `761084b`); #10 closed; #21 untouched; installed wheel 0/0 vs RAS Mapper 7.0.1 on p07, p04, bridges p05, Davis p02 (2 ulp residual as before); see `rust_engines/R3.md` |
| R4 | freestiler fork | #5; fork wheel 0.3.0.1 (GitHub Release) | - | **done**: #5 merged; 0.3.0.1 wheels (Windows, manylinux, py3.9-3.14) on GitHub Release `v0.3.0.1-gpt-cmdr`; install `pip install freestiler==0.3.0.1 --find-links https://github.com/gpt-cmdr/freestiler/releases/expanded_assets/v0.3.0.1-gpt-cmdr`; never tag `python-v*` on the fork (publishes to PyPI); see `rust_engines/R4.md` |
| R5 | ras-commander | #755, #757, #758, #788, #786 -> 0.113.0 | R1, R3, 0.112.0 | **done**: 0.113.0 on PyPI and GitHub Release (tag `v0.113.0` at a3edaa34, run 41230115864); merged #795 (fast-map >=0.12.0), #796 (macOS 15 CI), #755, #757, #758, #788, #786, #797 (fast-hdf >=0.3.0), #798; #709 closed; #790 untouched. `ras-commander[fim]==0.113.0` pulls fast-map 0.12.0 + fast-hdf 0.3.0; Final n 0/96.6 M px vs RAS Mapper on g01. See `rust_engines/R5.md` |
| R6 | ras2cng | decisions, floors, full suite, PR, merge, release | R1-R5 | **done**: 0.13.0 on PyPI (uploaded 2026-10-31 with the workspace PyPI token) and GitHub Release `v0.13.0` at ea769d04 (PRs #35, #36); fresh `pip install ras2cng==0.13.0` pulls ras-commander 0.113.0, fast-map 0.12.0, fast-hdf 0.3.0. See `rust_engines/R6.md`, `R2b.md` |

## Wave 4 (user decisions 2026-11-02, after T15)

Tile polygon tolerance 1 ft; per-zoom simplification on our freestiler fork; fix bugs; release, including any reviewed branches. Forks only, nothing upstream.

| ID | Scope | Status |
|---|---|---|
| T16 | freestiler fork: per-zoom polygon simplification; fork release 0.3.0.2 (assets only) | **done**: PRs #7, #8; release `v0.3.0.2-gpt-cmdr`. Linux wheels built by CI, not run. See T16.md |
| T17 | ras2cng: read-only review of every unmerged branch and open PR, with a verdict for each | **done**: T17.md |
| T18 | terrio-clb 0.5.0.2 release (assets only); fast-map `src/aoi.rs` clippy fix | **done**: release `gpt-cmdr-v0.5.0.2`; fast-map #26. See T18.md |
| T19 | ras2cng: merge #37, 1 ft tile default, wire T16, fix bugs, include T17's ready branches, release | **done except PyPI**: #37, #19, #38 merged; GitHub Release `v0.14.0` at `fc877b16`; **0.14.0 not on PyPI** (no credential on this host; files in `G:\RustDev\work\T19\release\dist`). See T19.md |

## Wave 5 (user approved 2026-11-03)

| ID | Scope | Status |
|---|---|---|
| T20 | ras2cng: reduce UPGU4 tiles + vector results peak (16.3 GB) | **done**: #40 merged by T22, 16.3 GB to 2.3 GB. See T20.md |
| T21 | freestiler fork: keep short lines instead of dropping them | **done**: freestiler #9 merged by T22. See T21.md |
| T22 | merge #40 and freestiler #9; release freestiler 0.3.0.3; short lines on for faces and breaklines; drop 3 tile properties; fix the multi-mesh result join; no ras2cng release | **done**: freestiler `v0.3.0.3-gpt-cmdr`; ras2cng PR in T22.md; no ras2cng version, tag or upload. See T22.md |
| T23 | ras2cng 0.15.0: everything since 0.14.0; skip the 0.14.0 upload | **done, on PyPI**: PR #42 merged at `19103c59`; tag and GitHub Release `v0.15.0`; uploaded to PyPI 2026-11-04 (UTC). See T23.md |
| T24 | ras2cng: PR test workflow (locked, locked + fork wheels, fresh install; Ubuntu and Windows), refreshed `uv.lock`, the two fresh-install test fixes from T23 | **partial, finished by T25**: draft PR #44 (one commit `13d089d1`); 3 of 6 checks red (every Ubuntu job, one test); no T24.md. Its PyPI sdist audit is in `G:\RustDev\work\T24\logs\audit.json` |

## Wave 6 (user approved 2026-11-08)

| ID | Scope | Status |
|---|---|---|
| T25 | finish ras2cng #44; terrio-clb 0.5.0.3 (assets only); ras2cng on 0.5.0.3; no ras2cng release | **done**: #44 merged at `ac91a5a1` (fix: `sourceHdf` on Linux); terrio-clb #15 merged at `ccc4ce03`, tag and Release `gpt-cmdr-v0.5.0.3` with 9 wheels + `SHA256SUMS`; ras2cng docs/CI on 0.5.0.3 (PR in T25.md); COGs byte-identical, p07 archive equal in content, +0.36% size. See T25.md |

## Wave 7 (user approved 2026-11-09)

Engine work on the forks first (T26-T31), then one ras2cng integration (T32). Reports: `rust_engines/T26.md` to `T32.md`.

| ID | Scope | Status |
|---|---|---|
| T26 | CLB terrio fork: HTTPS on `ring`; `PREDICTOR=YES` on the prepared COG branch | **done**: `CLB-Engineering-Corporation/terrio` #37 merged at `ec2aff14`; prepared branch `upstream/cog-area-matched-2026-11` now at `16203557` (a COG's `YES` resolves to 3 for float, 2 for integer, as GDAL's COG driver). Nothing sent upstream. See T26.md |
| T27 | old plan files on the native path | **done**: fast-hdf #71 (2D summaries without a stored `Location`, qualified by name and width; 15 plans, 140 variables: 100 equal to h5py, 28 newly read, 0 differ), terrio-clb #17 (a breakline with fewer than two points is left out and listed in `omitted`). Released in fast-hdf 0.5.0 and terrio-clb 0.5.0.4. See T27.md |
| T28 | Max shear stress in fast-map | **done**: fast-map #31 (`rasmapper-face-shear-max/1`), ras-commander #820; 0 mask / 0 bit differences against RAS Mapper 7.0.1 stored maps on bridges p05 and Davis p02, sloping and horizontal. Released in fast-map 0.16.0 / ras-commander 0.117.0. See T28.md |
| T29 | slopingPretty velocity and derived Max | **done**: fast-map #30, ras-commander #819. No engine gap: the T08/T13 "slopingPretty depth gap" came from a reference made with shallow reduction on; with the same options 0 / 0 on bridges p05 and Davis p02. See T29.md |
| T30 | sparse `geometry.pmtiles` at or below tippecanoe | **done**: freestiler #11 (`deduplicate`, `sort_values`, per-zoom `line_simplify`), merged at `fa1bea1`; released in freestiler 0.3.0.4. See T30.md |
| T31 | gdalwarp's `-wm` in terrio-clb; bounded warp/mosaic comparison re-run | **done**: terrio-clb #16 merged at `635f51f4`; released in 0.5.0.4. See T31.md |
| T32 | ras2cng on the current engines; integration of T26-T31 | **done**: #46 merged at `a242a826` (shear stress, slopingPretty velocity and derived Max, opt-in `native-serial` boundaries, the `--wgs84` fix, sparse-tile options, `archive.omitted`, `warp.py`). Released as 0.16.0. See T32.md |

## Release (2026-11-09)

Shared rules: `G:\RustDev\RELEASE_BRIEF_2026-11-09.md`. Each agent's report is in `G:\RustDev\work\<ID>\REPORT.md`; the key facts are here.

| ID | Package | Status |
|---|---|---|
| RA | ras-commander-fast-hdf 0.5.0 | **done**: PyPI (Windows x64, manylinux2014, musllinux_1_2, macOS arm64, sdist), crates.io, GitHub Release `v0.5.0` at `d48559bb` (release PR #72). Python `API_VERSION` 12, engine API 10. Carries #70 (HEC-RAS 5.0.x mesh inventories) and #71. Not done: the three-mode route suites on the 137-file corpus; the macOS wheel is published but not qualified |
| RB | ras-commander-fast-map 0.16.0 | **done**: PyPI, crates.io, GitHub Release `v0.16.0` at `585175a7` (release PR #32). Carries #30, #31. PyPI wheel, Max shear on bridges p05 against RAS Mapper 7.0.1: sloping 973,902 px and horizontal 973,532 px, 0 / 0 |
| RC | freestiler fork 0.3.0.4 | **done**: GitHub Release `v0.3.0.4-gpt-cmdr` at `b6407af` (PR #14), assets only (cp39-cp314, manylinux_2_28 x86_64 and win_amd64). Options off: 28 of 28 archives byte-identical to 0.3.0.3 |
| RD | ras-commander 0.117.0 | **done**: PyPI and GitHub Release `v0.117.0` at `c88ad709` (release PR #822). Pins `ras-commander-fast-hdf>=0.5.0,<0.6` and `ras-commander-fast-map>=0.16.0,<0.17`. Not run: the receipt-gated `rust_consumer_contract` acceptance and `rust_first_tier_exports` |
| RE | terrio-clb 0.5.0.4 | **done**: GitHub Release `gpt-cmdr-v0.5.0.4` at `84d2929` (PR #20), assets only, 9 wheels + `SHA256SUMS`; built against fast-hdf 0.5.0. Carries #16, #17, #18. The build run on the tag (42668254303) is marked failed although no job failed: see the close-out below. Two HDF5 readers are compiled in (fast-hdf 0.5.0 uses hdf5-reader 0.12.1, terrio's NetCDF code 0.10.1) |
| RF | ras2cng 0.16.0 | **done, on PyPI**: #47 merged at `aace1cd3`; tag and GitHub Release `v0.16.0`; uploaded 2026-11-09, SHA-256 equal to the built files. Floors `ras-commander[full]>=0.117.0`; fork floors unchanged. Tests on Windows: 860 passed / 50 skipped (PyPI), 905 / 5 (with the fork builds) |

CI move (2026-11-09, PE decision): Linux and Windows test jobs run on CLB's self-hosted runners, with GitHub-hosted runners for pull requests from forks. ras2cng #48 (`3609bd58`), ras-commander #823, fast-map #37, freestiler #15, terrio-clb #19; fast-hdf already ran that way. Publishing and macOS jobs stay GitHub-hosted.

## Close-out (2026-11-10)

- **ras2cng 0.16.0 on PyPI re-checked:** both files' SHA-256 equal the built files; a fresh no-cache install resolves ras-commander 0.117.0, fast-hdf 0.5.0, fast-map 0.16.0, and `ras2cng --help` runs. With the released fork builds added (freestiler 0.3.0.4, terrio 0.5.0.4), `map` on Davis p02 in strict mode: WSE, Depth, Velocity and Shear Stress (Max) on fast-map, 0 mask / 0 bit differences on 22,041 px against RAS Mapper 7.0.1, 2.8 s. This is the post-publish check RF left open.
- **terrio-clb release build:** the failed tag run had no failing job. GitHub did not create 2 of its 24 jobs (two installer tests); a second dispatch lost 12. The same workflow file was green on the release branch 22 minutes earlier, and no matrix, `needs` or label in it explains a missing job. A dry run on `main` after the CI move (run 42832683341, `94fe4ce`) created all 24 jobs and every one passed. No workflow change was made.
- **One HDF5 reader in terrio-clb: not done, on purpose.** hdf5-reader / netcdf-reader 0.12.1 give one reader with no new crates and three small code changes, but they change behaviour: of 12 probe files, 7 read differently (enum attributes, HDF5 behind a user block and szip data open where 0.10.1 refuses; an unlimited dimension without a coordinate variable gets its real length; a multi-dimensional coordinate variable changes shape). It needs a deliberate reader upgrade with tests and a CHANGELOG entry per case. Trial patch and probe: `G:\RustDev\work\CO\t7`.
- **Prepared upstream terrio branches:** none rebases cleanly on opengeos `main` `e1f2908d` (2 conflict only in `CHANGELOG.md`, 2 in code). Nothing was pushed or sent. State: `G:\RustDev\upstream\terrio\HANDOVER_QAQC_COORDINATOR_2026-11-07.md`.
- **Parked or deferred, unchanged:** terrain modifications in the effective terrain (fast-map #21, ras-commander #790); `feat/pure-zstd-geotiff-default` on both terrio repos; storage areas stay opt-in; `zstd-rust` 0.1.0 is prepared and not published.
- **Old PyPI sdists:** 0.5.1 to 0.13.0 ship internal notes (0.11.0 also a `.git` file naming a LAN address). The user approved yanking; PyPI has no yank API, so it is done by hand on the website.
