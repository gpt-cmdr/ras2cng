# Task: QAQC Code Review — map-hdf feature + ADR stored maps (post-merge)

> **Status: written 2026-07, never executed.** Relocated 2026-08-26 from the
> stale `H:/CLB-Repos/ras2cng` checkout to the canonical repo, with the project
> context refreshed. The code it targets merged as `3b92ce2`; the review itself
> is still open. No `OUTPUT.md` exists yet.

## Objective
Independent QAQC review of the map-hdf command and whole-simulation ADR map
types. This code has already merged to `master`; a prior review pass fixed 10
issues (listed below). Focus on anything that pass missed — real correctness
bugs, edge cases, and error-handling gaps.

## Project Context
- **Project**: ras2cng 0.9.0 — HEC-RAS -> cloud-native GIS CLI (Python 3.12,
  Typer, pytest with mocked ras-commander). Full suite: 401 passed / 4 skipped
  (install all extras first; the 4 skips need the absent I: drive).
- **Working Directory**: G:\GH\ras2cng — the canonical copy. This brief was
  written against the H:\CLB-Repos checkout, which was a stale 0.6.0 tree and
  has since been refreshed from origin.
- **Companion (already merged to ras-commander main)**:
  G:\GH\ras-commander\ras_commander\RasProcess.py — store_maps() native ADR.

## Target Files (review these)
1. ras2cng/scaffold.py (new module) — PlanHdfMetadata, read_plan_hdf_metadata,
   build_scaffold (per-input reuse signatures, hardlink/copy, terrain sidecar
   import, rasmap synthesis), terrain_sidecar_files.
2. ras2cng/mapping.py — ADR native-vs-shim capability detection
   (_store_maps_supports_native_adr), rasmap pre-injection shim
   (_inject_adr_stored_maps + backup/restore gated on shim_created),
   run-start mtime filtering of output globs, inundation shapefile collection,
   PostProcessing.hdf cleanup, recession warned no-op.
3. ras2cng/cli.py — map_hdf_command (terrain XOR, marker-gated --rm-scaffold)
   and the new map-command flags.
4. G:\GH\ras-commander\ras_commander\RasProcess.py — store_maps ADR params,
   MAP_TYPES XML names, extra_attrs, threshold-labeled + mtime-filtered globbing,
   PostProcessing.hdf move exclusion.

Use `git -C G:\GH\ras2cng show 3b92ce2 --stat` to see the merged change, or diff
the files directly. Note the surrounding code has moved on since this brief was
written — `mapping.py` now routes COG conversion through `ras2cng.cog`, so review
the current state rather than the state at merge.

## Already found and fixed by the prior pass (verify, do not re-report)
- --rm-scaffold deleting non-scaffold user workdirs (now marker-gated)
- Stale .rasmap.adrbak restored over current rasmap (now shim_created-gated)
- Scaffold reuse ignoring terrain input changes (now per-input signatures)
- Marker written only on success poisoning workdir (now written before build)
- Threshold-agnostic output globs claiming stale files (now mtime >= run-start)
- POSIX backslash Path round-trips in rasmap paths
- ADR names hardcoded vs MAP_TYPES-driven (RasProcess.py)
- PostProcessing.hdf moved cross-volume then deleted (now excluded from move)
- --rasprocess not reaching terrain build (now warns)
- inundation-boundary + --wgs84/--cog leaving shapefile in model CRS (now notes)

## Focus
Logic correctness, error/exception paths leaving partial state, concurrency,
Windows/UNC path handling, bytes-vs-str from h5py, XML special characters in
plan titles/ShortIDs, and test coverage gaps. Severity-ranked; be specific with
file:line and a concrete failure scenario.

## Output Format — write to OUTPUT.md
### Executive Summary
### Severity-Ranked Findings (Severity / File:line / Issue / Evidence / Fix)
### Agreement with Existing Code
### Recommended Test Cases
