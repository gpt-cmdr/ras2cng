"""Real NetCDF4 round trips and rejected-input publication guards."""

import hashlib
import json
import time

import numpy as np
import pytest
import rasterio
from netCDF4 import Dataset
from rasterio.transform import from_origin

from ras2cng.depth_stack import write_depth_stack


def raster(
    tmp_path, name="a", *, values=None, crs="EPSG:3857", transform=None, nodata=-9999
):
    path = tmp_path / f"{name}.tif"
    values = (
        np.array([[0, 1.25], [2.5, nodata]], dtype="f4") if values is None else values
    )
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        width=values.shape[1],
        height=values.shape[0],
        count=1,
        dtype=values.dtype,
        crs=crs,
        transform=transform or from_origin(100, 200, 3, 3),
        nodata=nodata,
    ) as dst:
        dst.write(values, 1)
    return path


def call(sources, target, **kwargs):
    return write_depth_stack(
        sources,
        target,
        profiles=kwargs.pop("profiles", ["p02", "p01"][: len(sources)]),
        vertical_unit=kwargs.pop("vertical_unit", "ft"),
        **kwargs,
    )


def test_roundtrip_order_dry_nodata_and_source_immutability(tmp_path):
    a = raster(tmp_path)
    b = raster(tmp_path, "b", values=np.array([[3, 0], [-9999, 8.25]], dtype="f4"))
    before = [hashlib.sha256(p.read_bytes()).hexdigest() for p in [a, b]]
    output = call([a, b], tmp_path / "reach.nc", provenance={"project": "public-slug"})
    with Dataset(output) as nc:
        assert nc.variables["depth"].dimensions == ("flow", "y", "x")
        assert nc.variables["profile"][:].tolist() == ["p02", "p01"]
        assert nc.variables["flow"][:].tolist() == [0, 1]
        assert nc.variables["flow"].units == "1"
        assert nc.variables["flow"].axis_semantics == "profile_index"
        assert nc.variables["depth"][0, 0, 0] == 0
        assert nc.variables["depth"][1, 1, 1] == 8.25
        assert nc.variables["depth"][0, 1, 1] is np.ma.masked
        assert nc.variables["depth"].chunking() == [1, 2, 2]
        assert nc.variables["depth"].filters()["complevel"] == 4
        assert nc.variables["depth"].filters()["shuffle"] is True
        assert nc.variables["depth"].units == "ft"
        assert nc.variables["x"][:].tolist() == [101.5, 104.5]
        assert nc.variables["y"][:].tolist() == [198.5, 195.5]
        assert json.loads(nc.source_sha256_json) == before
        assert json.loads(nc.provenance_json) == {"project": "public-slug"}
        assert str(tmp_path) not in str(nc.__dict__)
    assert before == [hashlib.sha256(p.read_bytes()).hexdigest() for p in [a, b]]


@pytest.mark.parametrize("unit,cf", [("cfs", "ft3 s-1"), ("cms", "m3 s-1")])
def test_real_discharge_coordinates(tmp_path, unit, cf):
    path = raster(tmp_path)
    output = call(
        [path],
        tmp_path / "reach.nc",
        flows=[123.75],
        flow_units=unit,
        vertical_unit="m",
    )
    with Dataset(output) as nc:
        assert nc.variables["flow"][:].tolist() == [123.75]
        assert nc.variables["flow"].units == cf
        assert nc.variables["flow"].axis_semantics == "discharge"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"profiles": []},
        {"profiles": ["p01", "p01"]},
        {"profiles": [""]},
        {"profiles": [float("nan")]},
        {"profiles": ["p\0"]},
        {"vertical_unit": "ftUS"},
        {"vertical_unit": ""},
        {"flows": []},
        {"flows": [1], "flow_units": "gpm"},
        {"flows": [float("nan")], "flow_units": "cfs"},
        {"flows": [float("inf")], "flow_units": "cms"},
        {"flows": [True], "flow_units": "cfs"},
        {"flow_units": "cfs"},
        {"compression_level": 9},
        {"compression_level": True},
        {"provenance": {"x": 1}},
        {"provenance": {"": "bad"}},
    ],
)
def test_bad_contract_preserves_target(tmp_path, kwargs):
    source = raster(tmp_path)
    target = tmp_path / "reach.nc"
    target.write_bytes(b"previous-good")
    with pytest.raises(ValueError):
        call([source], target, **kwargs)
    assert target.read_bytes() == b"previous-good"
    assert not list(tmp_path.glob("*.partial"))


@pytest.mark.parametrize(
    "change",
    [
        {"crs": "EPSG:4326"},
        {"crs": None},
        {"nodata": None},
        {"transform": from_origin(101, 200, 3, 3)},
        {"values": np.ones((3, 2), dtype="f4")},
        {"nodata": -9998},
        {"transform": rasterio.Affine(3, 1, 100, 0, -3, 200)},
    ],
)
def test_grid_mismatch_refused(tmp_path, change):
    a = raster(tmp_path)
    b = raster(tmp_path, "b", **change)
    with pytest.raises(ValueError):
        call([a, b], tmp_path / "reach.nc")
    assert not (tmp_path / "reach.nc").exists()


def test_nan_nodata_and_nonfinite_valid(tmp_path):
    source = raster(tmp_path, values=np.array([[0, np.nan]], dtype="f4"), nodata=np.nan)
    output = call([source], tmp_path / "reach.nc")
    with Dataset(output) as nc:
        assert nc.variables["depth"][0, 0, 0] == 0
        assert nc.variables["depth"][0, 0, 1] is np.ma.masked
    bad = raster(tmp_path, "bad", values=np.array([[0, np.inf]], dtype="f4"))
    with pytest.raises(ValueError, match="finite"):
        call([bad], output)
    with Dataset(output) as nc:
        assert nc.variables["depth"][0, 0, 0] == 0


def test_scaled_values_are_decoded_without_loss(tmp_path):
    source = raster(tmp_path, values=np.array([[0, 2]], dtype="i2"), nodata=-9999)
    with rasterio.open(source, "r+") as dst:
        dst.scales = (0.5,)
        dst.offsets = (1.0,)
    with Dataset(call([source], tmp_path / "reach.nc")) as nc:
        assert nc.variables["depth"][0, 0, :].tolist() == [1.0, 2.0]


def test_unrepresentable_float64_is_not_silently_rounded(tmp_path):
    source = raster(tmp_path, values=np.array([[0.1]], dtype="f8"))
    with pytest.raises(ValueError, match="exactly representable"):
        call([source], tmp_path / "reach.nc")
    assert not (tmp_path / "reach.nc").exists()


def test_target_cannot_overwrite_input(tmp_path):
    source = raster(tmp_path)
    with pytest.raises(ValueError, match="input"):
        call([source], source)


def test_deflate_levels_small_synthetic_benchmark(tmp_path, record_property):
    """Compare supported levels without an unstable speed/size assertion."""
    y, x = np.indices((256, 256))
    values = ((x + y) / 4).astype("f4")
    source = raster(tmp_path, values=values)
    for level in [4, 6]:
        start = time.perf_counter()
        output = call([source], tmp_path / f"level-{level}.nc", compression_level=level)
        record_property(f"level_{level}_seconds", time.perf_counter() - start)
        record_property(f"level_{level}_bytes", output.stat().st_size)
        with Dataset(output) as nc:
            assert nc.variables["depth"].filters()["complevel"] == level
            np.testing.assert_array_equal(nc.variables["depth"][0], values)


@pytest.mark.parametrize("flows", [[1, 1, 2], [1, 3, 2]])
def test_nonmonotonic_discharge_preserves_target(tmp_path, flows):
    sources = [raster(tmp_path, str(i)) for i in range(3)]
    target = tmp_path / "reach.nc"
    target.write_bytes(b"previous-good")
    with pytest.raises(ValueError, match="strictly monotonic"):
        call(
            sources, target, profiles=["p0", "p1", "p2"], flows=flows, flow_units="cfs"
        )
    assert target.read_bytes() == b"previous-good"


def test_decreasing_discharge_does_not_reorder_profiles(tmp_path):
    sources = [raster(tmp_path, str(i)) for i in range(2)]
    output = call(
        sources,
        tmp_path / "reach.nc",
        profiles=["p02", "p01"],
        flows=[2, 1],
        flow_units="cfs",
    )
    with Dataset(output) as nc:
        assert nc.variables["flow"][:].tolist() == [2, 1]
        assert nc.variables["profile"][:].tolist() == ["p02", "p01"]


@pytest.mark.parametrize(
    "transform",
    [
        rasterio.Affine(3, 0, np.inf, 0, -3, 200),
        rasterio.Affine(np.inf, 0, 100, 0, -3, 200),
    ],
)
def test_nonfinite_grid_preserves_target(tmp_path, transform):
    source = raster(tmp_path, transform=transform)
    target = tmp_path / "reach.nc"
    target.write_bytes(b"previous-good")
    with pytest.raises(ValueError, match="finite"):
        call([source], target)
    assert target.read_bytes() == b"previous-good"


def test_public_display_omits_private_source_identities(tmp_path):
    source = raster(tmp_path)
    output = call(
        [source],
        tmp_path / "reach.nc",
        include_source_hashes=False,
        provenance={"producer": "public-writer", "role": "display_derivative"},
    )
    with Dataset(output) as nc:
        assert "source_sha256_json" not in nc.ncattrs()
        assert json.loads(nc.provenance_json) == {
            "producer": "public-writer",
            "role": "display_derivative",
        }
        assert nc.variables["depth"][0, 0, 0] == 0
        assert nc.variables["depth"][0, 1, 1] is np.ma.masked
    with pytest.raises(ValueError, match="boolean"):
        call([source], output, include_source_hashes="false")
