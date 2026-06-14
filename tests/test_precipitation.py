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


def _make_nodata_hdf(path: Path, nodata: float = -9999.0) -> Path:
    """Processed-style HDF with a NoData attribute and a masked cell."""
    values = np.array(
        [
            [[1.0, 2.0], [3.0, nodata]],
            [[0.5, 1.0], [nodata, 2.0]],
        ],
        dtype="float32",
    )
    with h5py.File(path, "w") as hdf:
        group = hdf.require_group(PRECIPITATION_GROUP)
        _write_base_attrs(group)
        group.attrs["Data Type"] = "per-cum"
        group.attrs["NoData"] = str(nodata)
        group.create_dataset(
            "Timestamp",
            data=np.array([b"01JAN2020 00:00:00", b"01JAN2020 01:00:00"]),
        )
        group.create_dataset("Values", data=values.reshape(2, 4))
    return path


def test_export_precipitation_nodata_round_trip(tmp_path: Path):
    rasterio = pytest.importorskip("rasterio")
    nodata = -9999.0
    hdf_path = _make_nodata_hdf(tmp_path / "model.p01.hdf", nodata=nodata)

    info = read_precipitation_grid_info(hdf_path)
    assert info.nodata == nodata

    result = export_precipitation_rasters(hdf_path, tmp_path / "precip", timestamps=[0])

    with rasterio.open(result.incremental[0]) as src:
        # NoData declared in the GeoTIFF profile matches the HDF NoData value.
        assert src.nodata == nodata
        data = src.read(1)
        # The originally-masked cell round-trips as the NoData sentinel...
        assert data[1, 1] == nodata
        # ...and rasterio masks it, while valid cells stay unmasked.
        masked = src.read(1, masked=True)
        assert masked.mask[1, 1]
        assert not masked.mask[0, 0]
        assert masked[0, 0] == 1.0


def test_select_timestamps_label_first_for_numeric_label(tmp_path: Path):
    rasterio = pytest.importorskip("rasterio")
    # Numeric timestamp labels: "0" is also a valid index, so label-first
    # resolution must select the label, not index 0.
    values = np.array(
        [
            [[1.0, 2.0], [3.0, 4.0]],
            [[5.0, 6.0], [7.0, 8.0]],
            [[9.0, 10.0], [11.0, 12.0]],
        ],
        dtype="float32",
    )
    hdf_path = tmp_path / "model.p01.hdf"
    with h5py.File(hdf_path, "w") as hdf:
        group = hdf.require_group(PRECIPITATION_GROUP)
        _write_base_attrs(group)
        group.attrs["Data Type"] = "per-cum"
        group.create_dataset("Timestamp", data=np.array([b"2", b"0", b"1"]))
        group.create_dataset("Values", data=values.reshape(3, 4))

    # Token "0" matches the label at index 1, not index 0.
    result = export_precipitation_rasters(hdf_path, tmp_path / "p", timestamps=["0"])
    assert result.timestamps == ["0"]
    with rasterio.open(result.incremental[0]) as src:
        assert np.allclose(src.read(1), [[5.0, 6.0], [7.0, 8.0]])

    # A non-matching numeric token still falls back to index interpretation:
    # "1" has no label match here (labels are "2","0","1" -> "1" IS a label),
    # so use a token that is neither a label nor an in-range index. "99" is not
    # a label, so it is treated as an out-of-range index and rejected.
    with pytest.raises(IndexError):
        export_precipitation_rasters(hdf_path, tmp_path / "p2", timestamps=["99"])

    # With non-numeric labels, a numeric token is unambiguously an index.
    hdf_path2 = _make_processed_hdf(tmp_path / "model2.p01.hdf")
    result3 = export_precipitation_rasters(hdf_path2, tmp_path / "p3", timestamps=["1"])
    assert result3.timestamps == ["01JAN2020 01:00:00"]


def test_export_precipitation_units_conversion_in_to_mm(tmp_path: Path):
    rasterio = pytest.importorskip("rasterio")
    # _write_base_attrs tags Units as "in"; convert to mm on export.
    hdf_path = _make_processed_hdf(tmp_path / "model.p01.hdf")

    result = export_precipitation_rasters(
        hdf_path, tmp_path / "precip", timestamps=[0], units="mm"
    )

    assert result.units == "mm"
    with rasterio.open(result.incremental[0]) as src:
        assert src.tags()["units"] == "mm"
        assert np.allclose(src.read(1), np.array([[1.0, 2.0], [3.0, 4.0]]) * 25.4)
