"""Tests for raw 1D steady-flow result extraction."""

from pathlib import Path

import pandas as pd

from ras2cng import results


def test_extract_steady_cross_section_results_preserves_source_identity(monkeypatch):
    expected = pd.DataFrame(
        {
            "river": ["River A"],
            "reach": ["Reach A"],
            "node_id": ["1000"],
            "profile": ["1-percent AEP"],
            "wsel": [102.0],
        }
    )
    monkeypatch.setattr(results.HdfResultsPlan, "is_steady_plan", lambda _: True)
    monkeypatch.setattr(results.HdfResultsPlan, "get_steady_results", lambda _: expected)

    actual = results.extract_steady_cross_section_results(Path("model.p01.hdf"))

    assert actual.equals(expected)


def test_extract_steady_cross_section_results_skips_nonsteady_plans(monkeypatch):
    monkeypatch.setattr(results.HdfResultsPlan, "is_steady_plan", lambda _: False)

    actual = results.extract_steady_cross_section_results(Path("model.p01.hdf"))

    assert actual.empty
