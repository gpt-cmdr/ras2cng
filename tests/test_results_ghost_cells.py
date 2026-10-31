"""Ghost cells are dropped from per-cell result exports.

HEC-RAS lists ghost cells (outside the 2D perimeter) after the physical cells
in cell result arrays, with placeholder values such as a 0.0 maximum water
surface. ``Geometry/2D Flow Areas/Attributes`` ``Cell Count`` is the physical
count.
"""
from pathlib import Path

import h5py
import numpy as np
import pytest

from ras2cng.results import extract_results_variable

SUMMARY = "Results/Unsteady/Output/Output Blocks/Base Output/Summary Output/2D Flow Areas/Area"


def _write_plan_hdf(path: Path) -> Path:
    physical, ghosts = 3, 2
    with h5py.File(path, "w") as hdf:
        hdf.require_group("Plan Data/Plan Information").attrs[
            "Simulation Start Time"
        ] = "01Jan2024 00:00:00"
        hdf.create_dataset(
            "Geometry/2D Flow Areas/Attributes",
            data=np.array([(b"Area", physical)], dtype=[("Name", "S16"), ("Cell Count", "i4")]),
        )
        hdf.create_dataset(
            "Geometry/2D Flow Areas/Area/Cells Center Coordinate",
            data=np.column_stack([np.arange(physical + ghosts, dtype=float), np.zeros(physical + ghosts)]),
        )
        hdf.create_dataset(
            f"{SUMMARY}/Maximum Water Surface",
            data=np.array([[10.0, 11.0, 12.0, 0.0, 0.0], [0.1] * 5], dtype=np.float32),
        )
        hdf.create_dataset(
            f"{SUMMARY}/Maximum Face Velocity",
            data=np.array([[1.0, 2.0, 3.0, 4.0], [0.1] * 4], dtype=np.float32),
        )
    return path


@pytest.mark.parametrize("mode", ["none", "point"])
def test_cell_results_exclude_ghost_cells(tmp_path, mode):
    plan = _write_plan_hdf(tmp_path / "model.p01.hdf")
    result = extract_results_variable(plan, "Maximum Water Surface", geometry_mode=mode)
    assert result["cell_id"].tolist() == [0, 1, 2]
    assert result["maximum_water_surface"].tolist() == [10.0, 11.0, 12.0]
    assert "is_ghost_cell" not in result.columns


def test_face_results_are_unchanged(tmp_path):
    plan = _write_plan_hdf(tmp_path / "model.p01.hdf")
    result = extract_results_variable(plan, "Maximum Face Velocity", geometry_mode="none")
    assert result["face_id"].tolist() == [0, 1, 2, 3]
