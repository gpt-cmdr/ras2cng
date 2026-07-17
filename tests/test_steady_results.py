"""Tests for raw 1D steady-flow result extraction."""

from pathlib import Path

import pandas as pd
import xarray as xr

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


def test_extract_unsteady_cross_section_results_uses_bounded_summary_api(monkeypatch):
    expected = pd.DataFrame(
        {
            "river": ["River A"],
            "reach": ["Reach A"],
            "node_id": ["1000"],
            "maximum_water_surface": [102.0],
        }
    )
    received = {}

    def fake_summary(path, *, chunk_rows):
        received.update(path=path, chunk_rows=chunk_rows)
        return expected

    monkeypatch.setattr(results.HdfResultsXsec, "get_xsec_summary", fake_summary)

    actual = results.extract_unsteady_cross_section_results(
        Path("model.p01.hdf"),
        chunk_rows=128,
    )

    assert actual.equals(expected)
    assert received == {"path": Path("model.p01.hdf"), "chunk_rows": 128}


def test_extract_unsteady_cross_section_results_supports_older_ras_commander(monkeypatch):
    dataset = xr.Dataset(
        {
            "Water_Surface": (
                ["time", "cross_section"],
                [[100.0], [102.5]],
            ),
        },
        coords={
            "time": pd.to_datetime(["2020-01-01", "2020-01-02"]),
            "cross_section": ["XS 1000"],
            "River": ("cross_section", ["River A"]),
            "Reach": ("cross_section", ["Reach A"]),
            "Station": ("cross_section", ["1000"]),
            "Name": ("cross_section", ["XS 1000"]),
        },
    )
    monkeypatch.delattr(results.HdfResultsXsec, "get_xsec_summary")
    monkeypatch.setattr(
        results.HdfResultsXsec,
        "get_xsec_timeseries",
        lambda _: dataset,
    )

    actual = results.extract_unsteady_cross_section_results(Path("model.p01.hdf"))

    assert actual["maximum_water_surface"].tolist() == [102.5]
    assert actual["minimum_water_surface"].tolist() == [100.0]
    assert actual["maximum_water_surface_time_index"].tolist() == [1]
    assert actual["maximum_water_surface_time"].tolist() == [
        pd.Timestamp("2020-01-02")
    ]
