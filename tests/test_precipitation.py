"""Tests for HEC-RAS gridded precipitation raster export."""

from __future__ import annotations

from pathlib import Path

import h5py
import numpy as np
import pytest

from ras2cng.precipitation import (
    PRECIPITATION_GROUP,
    export_precipitation_rasters,
    list_precipitation_timestamps,
    read_precipitation_grid_info,
)


def _write_base_attrs(obj, *, rows=2, cols=2, cellsize=10.0):
    obj.attrs["Mode"] = "Gridded"
    obj.attrs["Projection"] = "EPSG:5070"
    obj.attrs["Raster Cellsize"] = str(cellsize)
    obj.attrs["Raster Cols"] = str(cols)
    obj.attrs["Raster Left"] = "100.0"
    obj.attrs["Raster Rows"] = str(rows)
    obj.attrs["Raster Top"] = "200.0"
    obj.attrs["Units"] = "in"


def _make_processed_hdf(path: Path) -> Path:
    values = np.array(
        [
            [[1.0, 2.0], [3.0, 4.0]],
            [[0.5, 1.0], [1.5, 2.0]],
        ],
        dtype="float32",
    )
    with h5py.File(path, "w") as hdf:
        group = hdf.require_group(PRECIPITATION_GROUP)
        _write_base_attrs(group)
        group.attrs["Data Type"] = "per-cum"
        group.create_dataset(
            "Timestamp",
            data=np.array([b"01JAN2020 00:00:00", b"01JAN2020 01:00:00"]),
        )
        group.create_dataset("Values", data=values.reshape(2, 4))
    return path


def _make_imported_hdf(path: Path) -> Path:
    values = np.array(
        [
            [[1.0, 2.0], [3.0, 4.0]],
            [[3.0, 5.0], [6.0, 8.0]],
        ],
        dtype="float32",
    )
    with h5py.File(path, "w") as hdf:
        hdf.require_group(PRECIPITATION_GROUP)
        imported = hdf.require_group(f"{PRECIPITATION_GROUP}/Imported Raster Data")
        dataset = imported.create_dataset("Values", data=values.reshape(2, 4))
        _write_base_attrs(dataset)
        dataset.attrs["Data Type"] = "cumulative"
        dataset.attrs["Times"] = np.array(
            [b"2020-01-01 00:00:00", b"2020-01-01 01:00:00"]
        )
    return path


def test_read_precipitation_grid_info_processed(tmp_path: Path):
    hdf_path = _make_processed_hdf(tmp_path / "model.p01.hdf")

    info = read_precipitation_grid_info(hdf_path)

    assert info.source == "processed"
    assert info.rows == 2
    assert info.cols == 2
    assert info.cellsize == 10.0
    assert info.units == "in"
    assert info.timestamps == ["01JAN2020 00:00:00", "01JAN2020 01:00:00"]
    assert info.source_is_cumulative is False


def test_list_precipitation_timestamps(tmp_path: Path):
    hdf_path = _make_processed_hdf(tmp_path / "model.p01.hdf")

    assert list_precipitation_timestamps(hdf_path) == [
        "01JAN2020 00:00:00",
        "01JAN2020 01:00:00",
    ]


def test_export_precipitation_rasters_processed(tmp_path: Path):
    rasterio = pytest.importorskip("rasterio")
    hdf_path = _make_processed_hdf(tmp_path / "model.p01.hdf")

    result = export_precipitation_rasters(hdf_path, tmp_path / "precip", timestamps=[1])

    assert result.source == "processed"
    assert len(result.incremental) == 1
    assert len(result.cumulative) == 1

    with rasterio.open(result.incremental[0]) as src:
        data = src.read(1)
        assert np.allclose(data, [[0.5, 1.0], [1.5, 2.0]])
        assert src.crs.to_epsg() == 5070
        assert src.transform.a == 10.0
        assert src.transform.e == -10.0
        assert src.transform.c == 100.0
        assert src.transform.f == 200.0
        assert src.tags()["timestamp"] == "01JAN2020 01:00:00"
        assert src.tags()["units"] == "in"

    with rasterio.open(result.cumulative[0]) as src:
        assert np.allclose(src.read(1), [[1.5, 3.0], [4.5, 6.0]])
        assert src.descriptions[0] == "Cumulative precipitation (in)"


def test_export_precipitation_rasters_imported_cumulative_fallback(tmp_path: Path):
    rasterio = pytest.importorskip("rasterio")
    hdf_path = _make_imported_hdf(tmp_path / "model.u01.hdf")

    result = export_precipitation_rasters(
        hdf_path,
        tmp_path / "precip",
        timestamps=["20200101_010000"],
        export_cumulative=True,
    )

    assert result.source == "imported"
    assert result.timestamps == ["2020-01-01 01:00:00"]

    with rasterio.open(result.incremental[0]) as src:
        assert np.allclose(src.read(1), [[2.0, 3.0], [3.0, 4.0]])

    with rasterio.open(result.cumulative[0]) as src:
        assert np.allclose(src.read(1), [[3.0, 5.0], [6.0, 8.0]])


def test_precipitation_export_requires_raster_metadata(tmp_path: Path):
    hdf_path = tmp_path / "bad.p01.hdf"
    with h5py.File(hdf_path, "w") as hdf:
        group = hdf.require_group(PRECIPITATION_GROUP)
        group.create_dataset("Values", data=np.zeros((1, 4), dtype="float32"))

    with pytest.raises(ValueError, match="Raster Rows"):
        read_precipitation_grid_info(hdf_path)


def test_invalid_precipitation_source_raises(tmp_path: Path):
    hdf_path = _make_processed_hdf(tmp_path / "model.p01.hdf")

    with pytest.raises(ValueError, match="Invalid precipitation source"):
        read_precipitation_grid_info(hdf_path, source="bad")  # type: ignore[arg-type]
