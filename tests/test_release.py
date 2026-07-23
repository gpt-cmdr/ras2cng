from __future__ import annotations

from pathlib import Path

import pandas as pd

from ras2cng.release import enrich_vector_result_renderers


def test_enrich_vector_result_renderers_adds_style_and_query_contract(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "archive"
    result_path = archive / "results" / "p01" / "maximum_water_surface.parquet"
    result_path.parent.mkdir(parents=True)
    pd.DataFrame(
        {
            "cell_id": [1, 2, 3],
            "maximum_water_surface": [100.0, 102.0, 105.0],
            "join_index": [0, 1, 2],
        }
    ).to_parquet(result_path)
    manifest = {
        "schema": "rascommander.maplibre/v2",
        "layers": {
            "p01-wse": {
                "role": "maximum_water_surface",
                "sourceKind": "raw-hdf",
                "query": {"enabled": True},
                "provenance": {
                    "variable": "maximum_water_surface",
                    "geometryJoin": "mesh_cells",
                    "archiveParquet": "results/p01/maximum_water_surface.parquet",
                    "indexColumn": "cell_id",
                },
            }
        },
    }

    result, count = enrich_vector_result_renderers(
        manifest,
        archive_root=archive,
        project_units="English",
    )

    layer = result["layers"]["p01-wse"]
    assert count == 1
    assert layer["renderer"]["valueField"] == "maximum_water_surface"
    assert layer["renderer"]["geometryMode"] == "cell-fill"
    assert layer["renderer"]["units"] == "ft"
    assert layer["query"]["valueField"] == "maximum_water_surface"
    assert layer["query"]["fields"][0]["name"] == "Maximum Water Surface"
