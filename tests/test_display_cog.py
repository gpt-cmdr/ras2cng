"""Tests for ras2cng.display.publish_display_cog (tiny synthetic rasters only)."""

from __future__ import annotations

import json
import os
from dataclasses import FrozenInstanceError
from pathlib import Path

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

import ras2cng
from ras2cng.display import DisplayCogResult, publish_display_cog, sha256_file

# NAD83 Texas South Central ftUS (2278) / South (2277); both US survey feet.
SIZE = 160
ORIGIN = (3_100_000.0, 13_800_000.0)  # near Houston in EPSG:2278 ftUS
PIXEL = 50.0


def _write(
    path: Path, data: np.ndarray, *, crs="EPSG:2278", nodata, units=None, tags=None
):
    height, width = data.shape
    if crs == "EPSG:3857":
        transform = from_origin(-10_620_000.0, 3_470_000.0, 30.0, 30.0)
    else:
        transform = from_origin(ORIGIN[0], ORIGIN[1], PIXEL, PIXEL)
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        width=width,
        height=height,
        count=1,
        dtype=data.dtype.name,
        crs=crs,
        transform=transform,
        nodata=nodata,
    ) as dst:
        dst.write(data, 1)
        if units:
            dst.units = (units,)
        if tags:
            dst.update_tags(**tags)
    return path


def _depth(path: Path, **kwargs) -> Path:
    rng = np.random.default_rng(7)
    data = rng.uniform(0.01, 12.0, (SIZE, SIZE)).astype("float32")
    data[:20, :] = -9999.0
    data[:, :10] = -9999.0
    return _write(path, data, nodata=-9999.0, units="ft", **kwargs)


def test_numerical_float_derivative_is_3857_nearest_and_source_untouched(tmp_path):
    source = _depth(tmp_path / "depth.tif", tags={"private_caption": "C:/secret/model"})
    before = source.read_bytes()
    destination = tmp_path / "out" / "depth_3857.tif"

    result = publish_display_cog(source, destination)

    assert isinstance(result, DisplayCogResult)
    assert source.read_bytes() == before
    assert result.source_unchanged and result.source_sha256_before == sha256_file(
        source
    )
    assert result.output_sha256 == sha256_file(destination)
    assert result.authoritative is False and result.kind == "visualization_derivative"
    assert (
        result.base_resampling == "nearest"
        and result.overview_policy == "area_matched_mean"
        and result.value_conversion == "none"
    )
    assert result.lossy_max_z_error is None and result.validation["valid"] is True

    with rasterio.open(source) as src, rasterio.open(destination) as out:
        assert src.crs.to_epsg() == 2278  # native stays native
        assert out.crs.to_epsg() == 3857
        assert out.dtypes[0] == "float32" == result.output_dtype
        assert out.nodata == -9999.0
        assert out.units == ("ft",)  # CRS change does not convert value units
        tags = out.tags()
        assert (
            tags["display_derivative"] == "true"
            and tags["base_resampling"] == "nearest"
            and tags["overview_policy"] == "area_matched_mean"
        )
        assert "resampling" not in tags
        assert tags["source_sha256"] == result.source_sha256_before
        blob = json.dumps(tags) + json.dumps(out.tags(1))
        assert "secret" not in blob and str(tmp_path).replace(
            "\\", "/"
        ) not in blob.replace("\\\\", "/")
        # nearest: every valid output value existed in the source
        source_values = set(np.unique(src.read(1)).tolist())
        out_values = set(np.unique(out.read(1)).tolist())
        assert out_values <= source_values
        assert 0 < (out.read(1) != -9999.0).sum()
        assert tuple(out.bounds) == pytest.approx(result.output_bounds)
        assert (out.height, out.width) == result.output_shape


def test_to_dict_is_json_safe_and_frozen(tmp_path):
    source = _depth(tmp_path / "d.tif")
    result = publish_display_cog(source, tmp_path / "d3857.tif")
    payload = result.to_dict()
    assert json.loads(json.dumps(payload)) == payload
    assert payload["output_crs"] == "EPSG:3857" and payload["source_unchanged"] is True
    assert payload["source_crs"]["authority"] == "EPSG:2278"
    with pytest.raises(FrozenInstanceError):
        result.kind = "other"  # type: ignore[misc]


def test_nan_nodata_roundtrips_as_string(tmp_path):
    data = np.full((SIZE, SIZE), 3.5, dtype="float32")
    data[:30] = np.nan
    source = _write(tmp_path / "n.tif", data, nodata=float("nan"))
    result = publish_display_cog(source, tmp_path / "n3857.tif")
    assert result.to_dict()["source_nodata"] == "nan"
    json.dumps(result.to_dict())
    with rasterio.open(tmp_path / "n3857.tif") as out:
        assert np.isnan(out.nodata)


@pytest.mark.parametrize("crs", ["EPSG:2278", "EPSG:2277"])
def test_categorical_uint_classes_preserved(tmp_path, crs):
    rng = np.random.default_rng(3)
    classes = np.array([1, 2, 3, 7], dtype="uint8")
    data = classes[rng.integers(0, 4, (1400, 1400))]  # > one block, so overviews exist
    data[:25] = 0
    source = _write(tmp_path / "cls.tif", data, crs=crs, nodata=0)

    result = publish_display_cog(source, tmp_path / "cls3857.tif", categorical=True)

    assert result.categorical and result.output_dtype == "uint8"
    assert (
        result.base_resampling == "nearest"
        and result.overview_policy == "categorical_mode"
    )
    assert result.to_dict()["overview_policy"] == "categorical_mode"
    assert result.overview_method != "average" and result.overview_factors
    with rasterio.open(tmp_path / "cls3857.tif") as out:
        values = np.unique(out.read(1))
        assert set(values.tolist()) <= {0, 1, 2, 3, 7}
        assert out.nodata == 0
        assert out.overviews(1), "fixture must be large enough to carry overviews"
        # class codes survive in every overview level (no blended values)
        for factor in out.overviews(1):
            coarse = out.read(1, out_shape=(out.height // factor, out.width // factor))
            assert set(np.unique(coarse).tolist()) <= {0, 1, 2, 3, 7}


@pytest.mark.parametrize("dtype", ["int64", "uint64"])
def test_64_bit_integers_rejected_before_any_output(tmp_path, dtype):
    # 2**53 + 1 is the first integer a float64 warp cannot hold; nearest used to
    # publish ...992 under an unchanged int64 dtype.
    data = np.full((8, 8), 9007199254740993, dtype=dtype)
    source = _write(tmp_path / "big.tif", data, crs="EPSG:3857", nodata=0)
    before = sha256_file(source)
    with pytest.raises(ValueError, match="64-bit integers"):
        publish_display_cog(source, tmp_path / "out" / "big3857.tif", categorical=True)
    with pytest.raises(ValueError, match="64-bit integers"):
        publish_display_cog(source, tmp_path / "out" / "big3857.tif")
    assert sha256_file(source) == before
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "big.tif"
    ]  # no destination dir, no partials


def test_32_bit_integer_codes_still_exact(tmp_path):
    data = np.full((8, 8), 4_000_000_001, dtype="uint32")
    source = _write(tmp_path / "u32.tif", data, crs="EPSG:3857", nodata=0)
    result = publish_display_cog(source, tmp_path / "u32_out.tif", categorical=True)
    assert result.output_dtype == "uint32"
    with rasterio.open(tmp_path / "u32_out.tif") as out:
        assert set(np.unique(out.read(1)).tolist()) == {4_000_000_001}


def test_custom_crs_private_name_never_published_in_tiff_metadata(tmp_path):
    from rasterio.crs import CRS

    private = "C:/private/customer/project"
    crs = CRS.from_proj4(
        "+proj=merc +lon_0=0.123 +k=1 +x_0=0 +y_0=0 +datum=WGS84 +units=m +no_defs"
    )
    crs = CRS.from_wkt(crs.to_wkt().replace('PROJCS["unknown"', f'PROJCS["{private}"'))
    assert private in crs.to_wkt()
    source = tmp_path / "custom.tif"
    with rasterio.open(
        source,
        "w",
        driver="GTiff",
        width=8,
        height=8,
        count=1,
        dtype="float32",
        crs=crs,
        transform=from_origin(-10_620_000.0, 3_470_000.0, 30.0, 30.0),
        nodata=-9999.0,
    ) as dst:
        dst.write(np.ones((8, 8), "float32"), 1)
    destination = tmp_path / "custom_out.tif"

    result = publish_display_cog(source, destination)

    # The internal result keeps the full definition (documented, not public).
    assert result.source_crs["authority"] is None
    assert private in result.source_crs["wkt"]
    with rasterio.open(destination) as out:
        assert out.tags()["source_crs"] == "unverified"
        metadata = []
        for domain in [None, *out.tag_namespaces()]:
            metadata.append(json.dumps(out.tags(ns=domain)))
            metadata.append(json.dumps(out.tags(1, ns=domain)))
        metadata += [
            out.crs.to_wkt(),
            json.dumps(out.descriptions),
            json.dumps(out.units),
        ]
        assert out.crs.to_epsg() == 3857
    assert private not in "".join(metadata).replace("\\\\", "/")
    # and nowhere in the file bytes (GeoTIFF keys, GDAL metadata, overview IFDs)
    assert (
        b"private" not in destination.read_bytes()
        and b"customer" not in destination.read_bytes()
    )


def test_registered_crs_publishes_authority_identifier_only(tmp_path):
    source = _depth(tmp_path / "reg.tif")
    publish_display_cog(source, tmp_path / "reg_out.tif")
    with rasterio.open(tmp_path / "reg_out.tif") as out:
        assert out.tags()["source_crs"] == "EPSG:2278"


def test_already_3857_source_keeps_grid(tmp_path):
    source = _write(
        tmp_path / "m.tif",
        np.full((SIZE, SIZE), 2, dtype="uint16"),
        crs="EPSG:3857",
        nodata=0,
    )
    result = publish_display_cog(source, tmp_path / "m_out.tif", categorical=True)
    assert result.output_shape == result.source_shape
    assert result.output_transform == pytest.approx(result.source_transform)


def test_existing_destination_not_replaced_and_no_partials(tmp_path):
    source = _depth(tmp_path / "s.tif")
    destination = tmp_path / "dest.tif"
    destination.write_bytes(b"precious")
    with pytest.raises(FileExistsError):
        publish_display_cog(source, destination)
    assert destination.read_bytes() == b"precious"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["dest.tif", "s.tif"]


def test_source_destination_alias_rejected(tmp_path):
    source = _depth(tmp_path / "s.tif")
    before = sha256_file(source)
    with pytest.raises(ValueError, match="same file"):
        publish_display_cog(source, source)
    with pytest.raises(ValueError, match="same file"):
        publish_display_cog(source, tmp_path / "sub" / ".." / "s.tif")
    assert sha256_file(source) == before


def test_hardlink_and_symlink_destinations_rejected(tmp_path):
    source = _depth(tmp_path / "s.tif")
    before = sha256_file(source)
    hard = tmp_path / "hard.tif"
    os.link(source, hard)
    with pytest.raises(ValueError, match="aliases"):
        publish_display_cog(source, hard)
    assert sha256_file(source) == before


def test_symlink_destinations_rejected(tmp_path):
    source = _depth(tmp_path / "s.tif")
    before = sha256_file(source)
    link = tmp_path / "link.tif"
    try:
        link.symlink_to(source)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    with pytest.raises(ValueError, match="aliases"):
        publish_display_cog(source, link)
    dangling = tmp_path / "dangling.tif"
    dangling.symlink_to(tmp_path / "nowhere.tif")
    with pytest.raises(FileExistsError):
        publish_display_cog(source, dangling)
    assert sha256_file(source) == before


def test_race_destination_appearing_during_publish_is_not_replaced(
    tmp_path, monkeypatch
):
    source = _depth(tmp_path / "s.tif")
    destination = tmp_path / "raced.tif"
    from ras2cng import cog

    real = cog.area_matched_cog

    def racing(*args, **kwargs):
        report = real(*args, **kwargs)
        destination.write_bytes(b"winner")  # someone else wins the race
        return report

    monkeypatch.setattr(cog, "area_matched_cog", racing)
    with pytest.raises(FileExistsError):
        publish_display_cog(source, destination)
    assert destination.read_bytes() == b"winner"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["raced.tif", "s.tif"]


def test_failed_validation_cleans_own_partials_only(tmp_path, monkeypatch):
    source = _depth(tmp_path / "s.tif")
    bystander = tmp_path / ".other.partial-1"
    bystander.write_bytes(b"x")
    from ras2cng import cog

    def boom(path, **kw):
        raise cog.CogValidationError("synthetic failure")

    monkeypatch.setattr(cog, "assert_valid_cog", boom)
    with pytest.raises(cog.CogValidationError):
        publish_display_cog(source, tmp_path / "x.tif")
    assert sorted(p.name for p in tmp_path.iterdir()) == [".other.partial-1", "s.tif"]


def test_missing_source_and_bad_inputs(tmp_path):
    with pytest.raises(FileNotFoundError):
        publish_display_cog(tmp_path / "nope.tif", tmp_path / "o.tif")

    # no CRS
    path = tmp_path / "nocrs.tif"
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        width=8,
        height=8,
        count=1,
        dtype="float32",
        transform=from_origin(0, 8, 1, 1),
        nodata=-1.0,
    ) as dst:
        dst.write(np.ones((8, 8), "float32"), 1)
    with pytest.raises(ValueError, match="no CRS"):
        publish_display_cog(path, tmp_path / "o1.tif")

    # no nodata
    path = _write(tmp_path / "nonodata.tif", np.ones((8, 8), "float32"), nodata=None)
    with pytest.raises(ValueError, match="no nodata"):
        publish_display_cog(path, tmp_path / "o2.tif")

    # multiband
    path = tmp_path / "rgb.tif"
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        width=8,
        height=8,
        count=2,
        dtype="uint8",
        crs="EPSG:2278",
        transform=from_origin(*ORIGIN, 1, 1),
        nodata=0,
    ) as dst:
        dst.write(np.ones((2, 8, 8), "uint8"))
    with pytest.raises(ValueError, match="exactly 1 band"):
        publish_display_cog(path, tmp_path / "o3.tif")

    # infinite nodata
    path = _write(
        tmp_path / "infnd.tif", np.ones((8, 8), "float32"), nodata=float("inf")
    )
    with pytest.raises(ValueError, match="finite or NaN"):
        publish_display_cog(path, tmp_path / "o4.tif")

    assert [p.name for p in tmp_path.iterdir() if p.name.startswith("o")] == []


def test_nonfinite_output_grid_rejected(tmp_path, monkeypatch):
    source = _depth(tmp_path / "s.tif")
    from rasterio import warp
    from rasterio.transform import Affine

    monkeypatch.setattr(
        warp,
        "calculate_default_transform",
        lambda *a, **k: (Affine(float("inf"), 0, 0, 0, -1, 0), 10, 10),
    )
    with pytest.raises(ValueError, match="not finite"):
        publish_display_cog(source, tmp_path / "o.tif")
    assert not (tmp_path / "o.tif").exists()


def test_public_exports():
    assert ras2cng.publish_display_cog is publish_display_cog
    assert ras2cng.DisplayCogResult is DisplayCogResult
    assert "publish_display_cog" in ras2cng.__all__
