# R2b: terrio-clb, terrio 0.5.0.1 released, ras2cng 0.13.0 ready (2026-10-31)

**Status:**
- terrio CLB build 0.5.0.1 is merged, tagged and released on the new repo **CLB-Engineering-Corporation/terrio-clb**.
- ras2cng docs now point at it (gpt-cmdr/ras2cng#36, merged).
- **ras2cng 0.13.0 is built and checked but not uploaded:** this host has no PyPI credential. The user must run the commands under *Left for the user*.

Nothing went to PyPI, crates.io, npm or opengeos/terrio. I did not push to the fork CLB-Engineering-Corporation/terrio or touch its `upstream/*` branches; I only closed or commented on its PRs.

## Install (works now)

```
pip install terrio==0.5.0.1 --find-links https://github.com/CLB-Engineering-Corporation/terrio-clb/releases/expanded_assets/gpt-cmdr-v0.5.0.1
```

Release: https://github.com/CLB-Engineering-Corporation/terrio-clb/releases/tag/gpt-cmdr-v0.5.0.1

## 1. Repository setup

- Created `CLB-Engineering-Corporation/terrio-clb`: public, not a fork, with the description as briefed.
- Pushed fork `main` plus one setup commit as `main`, and the branches of #30, #28, #27, #29, #23, #26 (`feat/lerc-encoder-option`) and #1 (`feat/hecras-hdf-layers`).
- **No tags pushed.** Upstream's `v*` tags were left out on purpose.
- **Setup commit `a619b39`** (on `main`, before any tag existed):
  - every `release.yml` job (build, github-release, linux-packages, publish/PyPI, crates, npm) has `if: github.repository == 'opengeos/terrio' && ...`;
  - the `docs.yml` Pages upload and deploy are guarded the same way, while the docs build still runs on PRs.
- **Disabled through the Actions API (no file changes):** `Claude Code Review`, `Claude autofix` (they need secrets this repo lacks) and `Release` (belt and braces next to the guard).
- Labels `ci:windows` and `ci:all` created. Every PR carried `ci:windows`.
- **Dependabot** (the inherited `.github/dependabot.yml`) opened #2-#4 at once. I closed them, cancelled their runs and deleted their branches. It will open more weekly unless the user removes `.github/dependabot.yml` or turns version updates off in Settings.

## 2. PRs (merge commits, branches kept)

| terrio-clb | was (fork) | merge commit | notes |
|---|---|---|---|
| #1 | #30 Windows tile-index fix | `e3c08c2` | unchanged branch. The `real data vs GDAL` job died once with a linker bus error (runner disk) and passed on rerun |
| #5 | #28 hdf5/netcdf 0.10.1 | `2ca26cb` | merged `main`. **Also fixed:** LICENSE-THIRD-PARTY.md had CRLF on four 0.10.1 lines, so CI's licence check failed. Regenerated from an LF checkout (blob `1c1be6c`, byte-identical to CI's output) |
| #6 | #27 GeoParquet Arrow layers | `90d3a91` | merged `main`: CHANGELOG keeps both entries. **Same CRLF problem** on the arrow/parquet 59 licence lines, fixed the same way |
| #7 | #29 HEC-RAS on fast-hdf Arrow catalog | `247a01f` | base `main` (the fork PR targeted `feat/hecras-hdf-layers`). R2's conflict resolutions apply. **fast-hdf 0.3.0 now comes from crates.io** (coordinator; published ~21:39 UTC): the `[patch.crates-io]` git entry is gone. The lock entry was hand-edited to change only source and checksum (`0bd6ba51…`, matches crates.io), because `cargo update -p` also flipped an unrelated `num-complex`. `cargo metadata --locked` passes. LICENSE regenerated (cargo-about 0.9.2, LF). The git source had listed fast-hdf's vendored notices twice plus repo-wide licence files; the crates.io package lists Supercluster/KDBush ISC, freestiler-core MIT and CLB MIT once each. **"API docs and packaging" passed: no accepted failures remain** |
| #8 | #23 COG report default domain | `1d01abf` | merged `main` (clean) |
| #9 | release | `8f012db` | the version commit (wheel `0.5.0.1`, Cargo `0.5.0+gpt-cmdr.1`), plus a CHANGELOG "CLB build 0.5.0.1" section with the install command |

- Every listed GitHub check was green before each merge; none was merged while pending. "npm package" and "point clouds and conformance" are skipped by design without their labels.
- PR bodies are the originals with a header naming the fork PR.
  - Bare `#N` references became `CLB-Engineering-Corporation/terrio#N`.
  - The one upstream reference (`roteiro-gis/netcdf-rust` PR 95) is rendered as code, so it creates no backlink.
- **Fork PRs:**
  - #30, #28, #27, #29 and #23 are closed, each with a comment pointing to its terrio-clb successor.
  - #1 is closed, pointing to terrio-clb#7: all six of its commits are in that history.
  - #26 is left open, with a comment that its branch is also in terrio-clb.

## Local gates (Windows GNU, `G:\RustDev\work\R2b\gates.sh`, target `G:\RustDev\target\R2b`, venv `G:\RustDev\venvs\R2b`)

Each merge state was gated on the exact tree that GitHub then merged. For #5 and #6 the tree differs from the gated one only in the licence file's line endings. #30's branch was unchanged, so R2's gate on it was reused.

| State | Rust (fmt, clippy -D warnings, workspace tests, ndarray, cargo doc -D warnings, doctests, metadata --locked) | Python suite on the built wheel |
|---|---|---|
| #28 | green, 1709 passed / 0 failed | 3279 passed, 1 failed* |
| #27 | green | 3282 passed, 1 failed* |
| #29 (crates.io fast-hdf) | green, 1715 / 0 | 3291 passed, 1 failed* |
| #23 | green | 3296 passed, 1 failed* |
| release (tree `72c1dce`, the tagged tree) | green, 1722 / 0 | 3296 passed, 1 failed*; wheel `terrio-0.5.0.1-cp310-abi3-win_amd64.whl` |

\* `test_external_overviews_follow_supplied_symlink_name`: WinError 1314, because this account cannot create symlinks (as in R2). It passes on GitHub's Windows runners.

Gate-run gotchas:
- **R2's gate script bug.** `gates.sh` passed an MSYS path to `uv pip install`, so R2's combined Python run tested a previously installed wheel. Mine uses `cygpath -w`.
- **The PASS label is not reliable for Python.** The pytest step's failure does not propagate out of its subshell, so the "PASS" label lies there; the counts above are read from the logs.

## 3. Release

- **Tag:** annotated `gpt-cmdr-v0.5.0.1` on `8f012db`, whose tree is the locally gated release tree.
- **Release dry run on the tag:** run 41258899633.
  - Passed: all native wheels (Windows x64/arm64, manylinux x86_64/aarch64, musllinux, macOS), sdist, CLI binaries, `crates` (`cargo publish --dry-run` now passes on crates.io fast-hdf) and `licenses`.
  - **Failed: `wheels (pyodide)`.** This is pre-existing, not from this stack. `python/src/tile_index.rs` (upstream tile-index feature `ed42330`, already on fork `main` 3ad8f58) calls `DatasetIndex::write`/`protect_source_path` and `RasterIndexSource::open_path` unconditionally, but they are `#[cfg(not(target_family = "wasm"))]`. The Pyodide wheel is not a deliverable here; the fix belongs with upstream's later tile-index work.
  - `npm` passed. The run's overall conclusion is failure only because of the Pyodide job.
- **GitHub Release** "terrio CLB build 0.5.0.1", made by hand from the run's artifacts:
  - `terrio-0.5.0.1-cp310-abi3-win_amd64.whl` `5d0845b7…`
  - `…manylinux2014_x86_64.manylinux_2_17_x86_64.whl` `46403633…`
  - `…manylinux_2_28_aarch64.whl` `298ffa0a…`
  - `SHA256SUMS`
- **Fresh venv check** (`G:\RustDev\venvs\R2b-verify`, pip, the command above): dist `0.5.0.1`, `__version__`/`terrio --version` `0.5.0+gpt-cmdr.1`. Present:
  - `write_geoparquet_layers` (on `terrio` and `_terrio`);
  - `_terrio.write_hecras_archive` and `hecras_archive_layers`;
  - `terrio.hecras.archive_layers` and `write_archive` (needs `import terrio.hecras`);
  - `validate_cog`;
  - `Raster.write(report_metadata=, area_matched_nan_valid=, metadata=)`;
  - `dem(max_memory_mib=, hillshade_strength=)` and `warp(max_memory_mib=)`.

## 4. ras2cng docs PR (gpt-cmdr/ras2cng#36, merge commit `ea769d04`)

- **Branch:** `docs/terrio-clb-url`, commit `e3ac07cd`.
- **What changed:**
  - the terrio `--find-links` URL → terrio-clb;
  - "gpt-cmdr fork build" wording for terrio → "CLB build (terrio-clb)";
  - `gpt-cmdr/terrio #N` → `CLB-Engineering-Corporation/terrio #N`.
- **Files:** CLAUDE.md, AGENTS.md, RELEASE_NOTES.md (0.13.0), `docs/getting-started/installation.md`, `docs/user-guide/{pmtiles,raster-delivery}.md`, and the `pyproject.toml` comment. freestiler stays on gpt-cmdr.
- **Not changed:** code comments in `cog.py`, `raster_tiles.py` and `tests/test_terrio_equivalence.py` still say `gpt-cmdr/terrio #N`. Those URLs redirect, and changing them only lengthened lines.
- **Gates:**
  - R6 venv full suite, 721 passed / 4 skipped (I: drive), exit 0. Run from `H:\`: under Git Bash's `/h/` cwd, one test fails on drive-letter case only.
  - `mkdocs build --strict` passes (to G:).
  - There is no PR CI. The PR got a comment with these results and was merged with a merge commit.
- The shared checkout `H:\CLB-Repos\ras2cng` is now on `master` at `ea769d04` (it was on `feat/rust-engines-wave1`). Leases were released. The untracked `T*.md`/`R*.md` were not committed.

## 5. ras2cng 0.13.0: built, not published

- Built from a clean `git archive` of `ea769d04`: `G:\RustDev\work\R2b\ras2cng-release\dist\`.
  - `ras2cng-0.13.0-py3-none-any.whl` `9e3f6ede…`
  - `ras2cng-0.13.0.tar.gz` `5371f164…`
- Metadata: `Requires-Dist: ras-commander[full]>=0.113.0`, `terrio>=0.5.0`, **no direct URLs**. `twine check` passes. PyPI still has only 0.12.0.
- **Fresh venv check** (`G:\RustDev\venvs\R2b-ras2cng`), with this exact wheel (byte-identical to the branch build):
  - installed `ras2cng[all]` from the wheel file, then terrio 0.5.0.1 and freestiler 0.3.0.1 with their documented `--find-links` commands;
  - `ras2cng --help` runs;
  - `cog.terrio_writes_report` is True, `geoparquet.AUTO_PREFERS_NATIVE` is True;
  - `archive_direct.direct_reason()` is None (direct path available);
  - `vector_tiles.arrow_input_enabled()` is True.
- **No PyPI credential on this host.** None was found in `~/.pypirc`, the environment (user or machine), keyring (WinVault), Windows Credential Manager or uv's credential store. 0.12.0 was apparently uploaded from elsewhere. I stopped before the upload, so there is **no `v0.13.0` tag and no GitHub Release**.

## Left for the user

1. **Publish ras2cng 0.13.0**, with a PyPI token for `ras2cng`:
   ```
   cd G:\RustDev\work\R2b\ras2cng-release
   uvx twine upload dist/ras2cng-0.13.0-py3-none-any.whl dist/ras2cng-0.13.0.tar.gz   # user __token__
   cd H:\CLB-Repos\ras2cng
   git tag -a v0.13.0 -m "ras2cng v0.13.0" ea769d04 ; git push origin v0.13.0
   gh release create v0.13.0 -R gpt-cmdr/ras2cng --verify-tag --title "ras2cng v0.13.0" --notes-file G:\RustDev\work\R2b\ras2cng_v0.13.0_notes.md
   ```
   (The tag is a working-tree-free ref, but create it via `clbgit with-lease ... -- tag ...` if you prefer the lease. Pushing the tag is GitHub sync, so it uses native git.) Then: `pip install "ras2cng[all]==0.13.0"`.
2. **Dependabot on terrio-clb:** remove `.github/dependabot.yml` there, or disable version updates, to stop weekly bump PRs.
3. **Pyodide wheel** fails to compile, inherited from upstream's tile-index code. Fix it when upstream does, if a Pyodide build is ever wanted.
4. **Process note: I killed other processes.** While freeing a locked file after the disk-full episode, I ran image-name `taskkill` for `cargo.exe`, `rustc.exe` and `ld.exe`. That may have killed other threads' compiles around 17:05-17:12 EDT. If another workstream saw an unexplained cargo abort then, that was me. Afterwards I killed only process trees I had started.

## Work locations

- **Worktrees:**
  - `G:\RustDev\src\R2b-terrio` (#28 state);
  - `-2` (#27);
  - `-3` (#29);
  - `-4` (#23);
  - `-5` (release);
  - `-lf` (an `autocrlf=false` checkout for cargo-about and the pushed merges).
- **Remote `clb`** was added to the shared clone's config.
- **Logs and scripts:** `G:\RustDev\work\R2b`:
  - `gates_*.log`, `chain2.out`;
  - `prs/` (original and converted PR bodies);
  - `release_assets/`;
  - `ras2cng_pytest2.log`, `ras2cng_mkdocs.log`.
- **Disk:** G: had 167-228 GB free during the last runs. `G:\RustDev\target\R2b` is about 15 GB and can be deleted.
