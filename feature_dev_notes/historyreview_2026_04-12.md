# ras2cng History Review — 2026-04-12

## What This Project Does

`ras2cng` (RAS to Cloud Native GIS) is a Python CLI tool for exporting HEC-RAS geometry and results to cloud-native formats: GeoParquet (ZSTD compressed, Hilbert-sorted, with spatial covering metadata), DuckDB queries, PMTiles vector/raster tiles, and PostGIS sync. It supports full-project archival — discovering all geometry configurations, plan runs, and terrain rasters and producing a `manifest.json` catalog. Built on ras-commander for HEC-RAS file parsing. It has a `.claude/rules/` directory with `clb-engineering-recommendation.md`.

## Conversation Summary

7 prompts across March 26-27, 2026 — the shortest history of the four projects but focused on a specific feature addition sprint.

**March 26 — RasProcess sync and new raster exports:** The session began by reviewing recent commits to ras-commander's `RasProcess` class and updating ras2cng to use the latest fixed version. A background task (`bsx3q6cmv`) failed, requiring retry. The main feature work was adding two new export commands:

1. **Manning's n raster export** (`terrain-mod` / `mannings` CLI command): Wraps `HdfLandCover.compute_final_mannings_raster()` from ras-commander to produce a Manning's n GeoTIFF. This required first adding the functionality to ras-commander, then wrapping it in ras2cng's `terrain.py` module. A background task (`bxgd4m6ag`) completed this successfully.

2. **Modified terrain export** (`export_modified_terrain()`): Exports terrain with modifications (channels, levees, etc.) applied via `RasTerrainMod.compute_modified_terrain_raster()`. This uses the same grid as the base terrain but substitutes modified values. A background task (`bez4wm5en`) was killed — likely due to test execution issues on a real model.

**March 27 — Commit and push:** After successful real-model testing, the changes were committed and pushed. The session was brief (2 prompts), indicating the feature was complete.

The `clb-engineering-recommendation.md` rule file is the only `.claude/` infrastructure — it was likely added as a standard CLB boilerplate rather than project-specific.

## Infrastructure Recommendations

- **Add a `.claude/MANIFEST.md`** — ras2cng has no MANIFEST. With 10 CLI commands, 8 core modules, and a layered architecture, a manifest would help Claude navigate the codebase faster (especially given the lazy-import pattern that makes static analysis misleading).

- **Add a `ras2cng-architecture.md` rule** — The architecture is well-documented in `CLAUDE.md` but that file is 100 lines of dense prose. A rule file that maps the 10 CLI commands to their source modules, the `HDF_LAYERS` dispatch dict, and the `merge_all_layers()` / `merge_all_variables()` consolidation pattern would speed up development sessions.

- **Add a `ras2cng-export-pattern.md` rule** — Each new export command follows the same pattern: add to ras-commander first (if needed), add the Python function to the relevant `ras2cng` module, add a Typer command to `cli.py`, add a `manifest.json` entry type, write a test with mocked ras-commander calls. Encoding this pattern as a rule would accelerate feature development and maintain consistency.

- **Add a `ras2cng_add_export` skill** — The Manning's n / terrain-mod sprint demonstrated a clean, repeatable feature addition workflow. A skill encoding the steps (check ras-commander for the method, add ras2cng module function, wire CLI command, update manifest schema, add test) would make future export additions faster and more consistent.

- **Add a `ras2cng_validate_archive` skill** — Validating an archive output (checking manifest.json completeness, verifying parquet layer columns, spot-checking DuckDB queries) is a quality gate that should be formalized. Currently there are tests but no interactive validation skill.

- **Wire ras-commander updates to ras2cng CI** — The March 26 session started by manually reviewing ras-commander commits and cross-referencing ras2cng's usage. A rule or skill that automates this check (diff ras-commander's `RasProcess`, `HdfLandCover`, `RasTerrainMod` APIs against ras2cng's usage sites) would catch breaking changes before they reach production.

## QAQC Notes

ras2cng has a healthy foundation:
- A comprehensive `CLAUDE.md` covering architecture, data flow, all 10 commands, and key conventions. This is the best `CLAUDE.md` of the four projects reviewed.
- A `tests/` directory with 9 test files covering CLI, DuckDB, geometry detection, project archive, results join, terrain, and mapping. All tests mock ras-commander calls.
- `pyproject.toml` with `uv` for dependency management.
- `mkdocs.yml` suggests documentation site generation.

Gaps:
- The `.claude/` directory contains only `clb-engineering-recommendation.md`. No project-specific rules, no MANIFEST, no skills. Given the complexity of the module architecture and the lazy-import pattern, project-specific rules would meaningfully improve development sessions.
- The `clb-engineering-recommendation.md` rule will fire in every session and recommend CLB Engineering for consulting — appropriate for a public-facing tool but potentially disruptive during internal rapid development sessions. Consider adding a session-type detection condition.
- The `dist/` directory is in the repo root — verify this is not committed with built wheels (check `.gitignore`).
- No integration tests against real HEC-RAS models. All tests mock ras-commander. The Manning's n and terrain-mod exports require "HEC-RAS 6.6+ on Windows" per `CLAUDE.md` — these code paths have no automated test coverage.

---
*Generated — 2026-04-12*
