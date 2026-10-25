import json
import zlib

import numpy as np
import pytest

rasterio = pytest.importorskip("rasterio")
netCDF4 = pytest.importorskip("netCDF4")

from ras2cng.rate_stack import rasterize_cell_values, write_rate_stack


def raster(path, values, x=100):
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        count=1,
        width=3,
        height=2,
        dtype="float32",
        crs="EPSG:2278",
        nodata=np.nan,
        transform=rasterio.transform.from_origin(x, 200, 10, 10),
    ) as dst:
        dst.write(np.asarray(values, dtype="float32"), 1)
    return path


def test_rate_stack_and_exact_range_decode(tmp_path):
    values = np.array([[0, 1, np.nan], [2, 3, 4]], dtype="float32")
    a = raster(tmp_path / "a.tif", values)
    b = raster(tmp_path / "b.tif", values + 1)
    target = tmp_path / "stack.nc"
    index = write_rate_stack(
        {"depth": [a, b], "wse": [b, a]},
        target,
        rate_k16=[-32, -16],
        units={"depth": "ft", "wse": "ft"},
        provenance={"datum": "unknown"},
    )
    json.dumps(index, allow_nan=False)
    with netCDF4.Dataset(target) as nc:
        assert list(nc.dimensions) == ["rate_k16", "y", "x"]
        assert nc["rate_k16"][:].tolist() == [-32, -16]
        assert nc["rate_in_per_h"][:].tolist() == pytest.approx([0.01, 0.1])
        np.testing.assert_array_equal(nc["depth"][0].filled(np.nan), values)
        assert nc["wse"].units == "ft"
    for name, expected in (
        ("depth", [values, values + 1]),
        ("wse", [values + 1, values]),
    ):
        var = index["variables"][name]
        with target.open("rb") as stream:
            for chunk in var["chunks"]:
                stream.seek(chunk["offset"])
                raw = zlib.decompress(stream.read(chunk["length"]))
                unshuffled = (
                    np.frombuffer(raw, dtype="u1").reshape(4, -1).T.copy().tobytes()
                )
                decoded = np.frombuffer(unshuffled, dtype="<f4").reshape(
                    var["chunk_shape"]
                )
                np.testing.assert_array_equal(decoded[0], expected[chunk["origin"][0]])
    with pytest.raises(ValueError, match="new"):
        write_rate_stack(
            {"depth": [a]}, target, rate_k16=[-32], units={"depth": "ft"}, provenance={}
        )


@pytest.mark.parametrize("keys", [[-16, -32], [-32, -32], [True], []])
def test_rate_axis_rejects_invalid_keys(tmp_path, keys):
    with pytest.raises(ValueError, match="rate_k16"):
        write_rate_stack(
            {"depth": []},
            tmp_path / "x.nc",
            rate_k16=keys,
            units={"depth": "ft"},
            provenance={},
        )


def test_grid_mismatch_and_negative_depth(tmp_path):
    a = raster(tmp_path / "a.tif", [[1, 1, 1], [1, 1, 1]])
    b = raster(tmp_path / "b.tif", [[1, 1, 1], [1, 1, 1]], x=110)
    with pytest.raises(ValueError, match="grids differ"):
        write_rate_stack(
            {"depth": [a, b]},
            tmp_path / "x.nc",
            rate_k16=[-32, -16],
            units={"depth": "ft"},
            provenance={},
        )
    c = raster(tmp_path / "c.tif", [[-1, 1, 1], [1, 1, 1]])
    with pytest.raises(ValueError, match="negative"):
        write_rate_stack(
            {"depth": [c]},
            tmp_path / "x.nc",
            rate_k16=[-32],
            units={"depth": "ft"},
            provenance={},
        )
    assert not (tmp_path / "x.nc").exists()


def test_native_cell_scalars(tmp_path):
    import geopandas as gpd
    from shapely.geometry import box

    template = raster(tmp_path / "t.tif", [[1, 1, 1], [1, 1, 1]])
    cells = gpd.GeoDataFrame(
        geometry=[box(100, 180, 110, 200), box(110, 180, 130, 200)], crs=2278
    )
    target = rasterize_cell_values(cells, [2, 3], template, tmp_path / "v.tif")
    with rasterio.open(target) as src:
        np.testing.assert_array_equal(src.read(1), [[2, 3, 3], [2, 3, 3]])


@pytest.mark.parametrize("mutation", ["rate", "empty", "units", "filters", "scale"])
def test_index_rejects_incompatible_layouts(tmp_path, mutation):
    import h5py

    from ras2cng.rate_stack import rate_stack_index

    a = raster(tmp_path / "a.tif", [[1, 1, 1], [1, 1, 1]])
    path = tmp_path / "stack.nc"
    write_rate_stack(
        {"depth": [a]}, path, rate_k16=[-32], units={"depth": "ft"}, provenance={}
    )
    with h5py.File(path, "r+") as hdf:
        if mutation == "rate":
            hdf["rate_in_per_h"][0] = 2
        elif mutation == "empty":
            del hdf["depth"]
        elif mutation == "units":
            hdf["depth"].attrs["units"] = "m"
        elif mutation == "scale":
            hdf["depth"].attrs["scale_factor"] = 2.0
        else:
            del hdf["depth"]
            hdf.create_dataset(
                "depth",
                data=np.ones((1, 2, 3), "f4"),
                chunks=(1, 2, 3),
                compression="gzip",
                shuffle=True,
                fletcher32=True,
                fillvalue=np.nan,
            )
    with pytest.raises(ValueError):
        rate_stack_index(path)


def test_scaled_sources_are_refused(tmp_path):
    path = raster(tmp_path / "scaled.tif", [[1, 1, 1], [1, 1, 1]])
    with rasterio.open(path, "r+") as src:
        src.scales = (2.0,)
    with pytest.raises(ValueError):
        write_rate_stack(
            {"depth": [path]},
            tmp_path / "x.nc",
            rate_k16=[-32],
            units={"depth": "ft"},
            provenance={},
        )
