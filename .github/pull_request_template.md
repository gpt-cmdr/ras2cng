## Summary

<!-- What does this PR do? 1-3 sentences. -->

## Type of Change

- [ ] Bug fix (non-breaking change that fixes an issue)
- [ ] New feature (non-breaking change that adds functionality)
- [ ] Breaking change (fix or feature that would cause existing functionality to change)
- [ ] Documentation update
- [ ] Refactoring (no functional changes)
- [ ] Test coverage improvement

## LLM Self-Review

I (or my LLM agent) reviewed this PR against the [CONTRIBUTING.md](../CONTRIBUTING.md) checklist:

### Code Quality
- [ ] Public functions have docstrings (Args, Returns, Raises)
- [ ] Uses `logging` module, not bare `print()` for operational output
- [ ] Errors raise appropriate exceptions with clear messages
- [ ] File paths use `pathlib.Path`
- [ ] Type hints on function signatures

### CLI
- [ ] New commands use Typer with help text and proper argument types
- [ ] GeoParquet output includes CRS and follows column conventions
- [ ] `--help` output is accurate for any new/modified commands
- [ ] N/A (no CLI changes)

### HEC-RAS
- [ ] Uses `ras-commander` for data extraction (no direct HDF parsing)
- [ ] No hardcoded paths or version assumptions
- [ ] N/A (no HEC-RAS data changes)

## Test Plan

<!-- How was this tested? List CLI commands run, test files executed, or manual verification steps. -->

- [ ] `pytest` passes
- [ ] Tested with real HEC-RAS project (name: _______)
- [ ] Manual CLI verification: `ras2cng <command> ...`

## LLM Attribution

<!-- If an LLM assisted with this PR, note which one. This is encouraged, not penalized. -->

- [ ] LLM-assisted (tool: _______)
- [ ] Fully manual
