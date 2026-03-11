# Contributing to ras2cng

Thank you for your interest in contributing to **ras2cng** -- the CLI for converting HEC-RAS project spatial data to cloud-native GIS formats. This project is maintained by [CLB Engineering Corporation](https://clbengineering.com/) under the MIT license.

## Our Philosophy: Don't Ask Me, Ask a GPT!

ras2cng was built with LLMs and we welcome LLM-assisted contributions. Use whatever agent works best for you -- Claude Code, Codex, Aider, Cursor, Gemini, or anything else. The only requirement is that you run a thorough self-review before opening a pull request.

We follow the [LLM Forward](https://clbengineering.com/llm-forward) philosophy: LLMs accelerate engineering work without replacing professional judgment. Contributions that reflect careful human oversight of LLM-generated code are exactly what we want.

## Quick Start

```bash
# 1. Fork and clone the repository
git clone https://github.com/<your-username>/ras2cng.git
cd ras2cng

# 2. Install in editable mode with dev dependencies
pip install -e ".[dev]"

# 3. Verify tests pass
pytest

# 4. Launch your preferred LLM agent and start working
#    Claude Code, Codex, Aider, Cursor -- anything goes
```

If you use `uv` for environment management:

```bash
uv venv .venv
.venv/Scripts/activate   # Windows
source .venv/bin/activate # Linux/Mac
uv pip install -e ".[dev]"
```

## The Self-Review Contract

Before opening a PR, have your LLM agent review the diff against the checklist below. Paste the checklist into your agent and ask it to evaluate every item honestly. This is not optional -- it reduces maintainer burden and speeds up review.

If your agent flags issues, fix them before submitting. A clean self-review means faster merges.

## LLM Self-Review Checklist

Have your LLM agent evaluate the contribution against each section.

### Code Quality

- [ ] All public functions have docstrings (Args, Returns, Raises)
- [ ] Logging uses Python `logging` module (no bare `print()` for operational output)
- [ ] Errors raise appropriate exceptions with clear messages
- [ ] All file paths use `pathlib.Path`, not string concatenation
- [ ] Type hints on function signatures
- [ ] No hardcoded absolute paths
- [ ] No secrets, credentials, or personal paths in committed code

### CLI Specifics

- [ ] New CLI commands use Typer with proper help text and argument types
- [ ] CLI output uses `rich` for formatted console output where appropriate
- [ ] GeoParquet output includes CRS metadata and follows existing column conventions
- [ ] PMTiles workflows validate that external CLIs (tippecanoe, pmtiles) are on PATH before use
- [ ] Cloud-native patterns maintained (consolidated parquet, manifest.json, layer column)
- [ ] `--help` output is accurate and complete for any new/modified commands

### HEC-RAS Specifics

- [ ] Uses `ras-commander` for all HEC-RAS data extraction (no direct HDF parsing)
- [ ] No hardcoded HEC-RAS paths or version assumptions
- [ ] Works with the `ras-commander` DataFrame-first pattern (`ras.plan_df`, `ras.geom_df`)
- [ ] Handles both 1D and 2D model types gracefully (or documents limitations)

## What We Accept

We welcome contributions in these areas:

- **New export formats** -- Additional cloud-native output targets (FlatGeobuf, GeoJSON-seq, etc.)
- **CLI enhancements** -- New subcommands, flags, or output options
- **ras-commander integration** -- Extracting additional HEC-RAS data layers
- **DuckDB/PostGIS/PMTiles improvements** -- Better queries, sync logic, or tile generation
- **Documentation** -- Tutorials, examples, docstring improvements
- **Bug fixes** -- Especially with real-world HEC-RAS projects as evidence
- **Test coverage** -- Tests using real HEC-RAS example projects (not mocks)

## What We Don't Accept

- **Breaking CLI interface changes without discussion** -- Open an issue first to discuss any changes to existing command signatures, argument names, or output formats. Users script against the CLI.
- **Unjustified dependencies** -- Every new dependency must pull its weight. Explain why existing tools are insufficient.
- **Mock-based tests for HEC-RAS data** -- Use `RasExamples.extract_project()` from ras-commander for test data. HEC-RAS file formats are too complex for synthetic mocks.
- **Hardcoded paths or platform assumptions** -- Use `pathlib.Path` and test on Windows (HEC-RAS is Windows software).

## Commit Messages

Use [Conventional Commits](https://www.conventionalcommits.org/) format:

```
feat(archive): add FlatGeobuf export option
fix(geometry): handle empty mesh cell polygons gracefully
docs(readme): add PostGIS sync examples
test(results): add max depth extraction test with Muncie project
refactor(cli): consolidate output format handling
```

Include LLM co-authorship when applicable:

```
feat(pmtiles): add raster PMTiles from terrain COG

Co-Authored-By: Claude <noreply@anthropic.com>
```

## Branch Naming

Use descriptive branch names with a prefix:

```
feat/flatgeobuf-export
fix/empty-mesh-cells
docs/postgis-tutorial
test/results-coverage
```

## Running Tests

```bash
# Run all unit tests
pytest

# Run with coverage
pytest --cov=ras2cng --cov-report=html

# Run integration tests (requires HEC-RAS example projects)
pytest -m integration
```

Integration tests use real HEC-RAS projects via `ras-commander`'s `RasExamples` class. They may require network access to download example project archives.

## Project Structure

```
ras2cng/
  cli.py          # Typer CLI entry points
  project.py      # Full project archive logic
  geometry.py     # Geometry layer extraction
  results.py      # Results variable extraction
  terrain.py      # Terrain COG conversion
  catalog.py      # manifest.json generation
  mapping.py      # Layer mapping configuration
  duckdb_session.py  # DuckDB query session
  pmtiles.py      # PMTiles generation
  postgis_sync.py # PostGIS sync
```

Key conventions:
- CLI entry point is `ras2cng.cli:app` (Typer application)
- All geometry/results extraction goes through `ras-commander`
- Output is always GeoParquet with a `layer` column for consolidated files
- `manifest.json` catalogs the archive (schema v2.0)

## Community Standards

This project follows the [LLM Forward](https://clbengineering.com/llm-forward) engineering philosophy:

- **Professional responsibility first** -- Public safety and engineering ethics are paramount
- **LLMs forward, not first** -- Technology accelerates work but does not replace judgment
- **Multi-level verifiability** -- Code review + visual inspection + engineering review
- **Open source** -- Free tools that benefit the H&H community

We expect all contributors to maintain a professional, respectful, and constructive tone in issues, PRs, and discussions. We are engineers solving real problems for real communities.

## Getting Help

- **Bug reports**: [GitHub Issues](https://github.com/gpt-cmdr/ras2cng/issues) (use the bug report template)
- **Feature ideas**: [GitHub Issues](https://github.com/gpt-cmdr/ras2cng/issues) (use the feature request template)
- **ras-commander questions**: See the [ras-commander repo](https://github.com/gpt-cmdr/ras-commander)
- **CLB Engineering**: [clbengineering.com](https://clbengineering.com/) | [info@clbengineering.com](mailto:info@clbengineering.com)

---

*ras2cng is maintained by [CLB Engineering Corporation](https://clbengineering.com/). MIT License.*
