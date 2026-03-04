# 00 — Using RasExamples

**Notebook**: [examples/00_using_ras_examples.py](https://github.com/gpt-cmdr/ras2cng/blob/master/examples/00_using_ras_examples.py)

Introduces the `RasExamples` API from ras-commander. Shows how to list available example projects
and extract `BaldEagleCrkMulti2D` — the example project used by all other notebooks.

## What it demonstrates

- `RasExamples.list_projects()` — list all available example HEC-RAS projects
- `RasExamples.extract_project("BaldEagleCrkMulti2D")` — extract to a local directory
- Inspecting the extracted project: finding `.g??.hdf`, `.g??`, and `.p??.hdf` files

## Run it

```bash
marimo edit examples/00_using_ras_examples.py
# or
python examples/00_using_ras_examples.py
```
