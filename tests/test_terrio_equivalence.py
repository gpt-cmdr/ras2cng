"""Pin the terrio-native raster paths to the GDAL/NumPy paths they replace.

ras2cng uses terrio's area-matched COG writer, ``validate_cog``, streaming
``dem``/``warp`` and streaming PMTiles only when the installed terrio has them
(capability-detected). These tests require that each native path writes what
the fallback path writes: the same pixels at every level, the same tags and
the same report for COGs, and byte-identical PMTiles. They skip on a terrio
without the capability (stock 0.5.0), where the fallback is the only path.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from ras2cng import cog, raster_tiles

rasterio = pytest.importorskip("rasterio")
from rasterio.transform import from_origin  # noqa: E402


def _write(path: Path, data: np.ndarray, *, nodata, tags=None, band_tags=None, description=None):
    height, width = data.shape
    with rasterio.open(
        path, "w", driver="GTiff", width=width, height=height, count=1, dtype=data.dtype,
        nodata=nodata, crs="EPSG:2271", transform=from_origin(2_000_000, 300_000, 10, 10),
    ) as destination:
        destination.write(data, 1)
        if tags:
            destination.update_tags(**tags)
        if band_tags:
            destination.update_tags(1, **band_tags)
        if description:
            destination.set_band_description(1, description)
    return path


def _flood(shape=(1201, 1037), dtype="float32", nodata=np.nan, seed=7):
    """A smooth surface inside an ellipse with a speckled wet/dry margin."""
    rng = np.random.default_rng(seed)
    height, width = shape
    y, x = np.mgrid[0:height, 0:width]
    values = (10 + 5 * np.sin(x / 50.0) + 3 * np.cos(y / 40.0)).astype(dtype)
    radius = (x - width / 2) ** 2 / (0.45 * width) ** 2 + (y - height / 2) ** 2 / (0.4 * height) ** 2
    dry = (radius > 1) | ((radius > 0.8) & (rng.random(shape) < 0.5))
    values[dry] = nodata
    return values


def _classes(shape=(1100, 900)):
    rng = np.random.default_rng(11)
    values = rng.integers(1, 6, size=shape).astype("uint8")
    values[:, : shape[1] // 3] = 0
    return values


def _assert_same_cog(left: Path, right: Path) -> None:
    with rasterio.open(left) as a, rasterio.open(right) as b:
        assert a.tags() == b.tags()
        assert (a.tags(1), a.descriptions, a.units, a.scales, a.offsets) == (
            b.tags(1), b.descriptions, b.units, b.scales, b.offsets
        )
        assert a.profile["dtype"] == b.profile["dtype"]
        assert (a.width, a.height, a.transform, a.crs) == (b.width, b.height, b.transform, b.crs)
        assert a.block_shapes == b.block_shapes
        assert a.overviews(1) == b.overviews(1)
        assert a.mask_flag_enums == b.mask_flag_enums
        np.testing.assert_equal(a.nodata, b.nodata)
        assert a.tags(ns="IMAGE_STRUCTURE")["COMPRESSION"] == b.tags(ns="IMAGE_STRUCTURE")["COMPRESSION"]
        levels = len(a.overviews(1))
    for level in [None, *range(levels)]:
        kwargs = {} if level is None else {"overview_level": level}
        with rasterio.open(left, **kwargs) as a, rasterio.open(right, **kwargs) as b:
            assert np.array_equal(a.read(1), b.read(1), equal_nan=True), f"level {level} differs"


def _assert_lerc_within_tolerance(native: Path, lossless: Path, lerc_reference: Path, tolerance: float) -> None:
    """Every level of ``native`` is within ``tolerance`` of the lossless pyramid.

    ``lossless`` is the GDAL path's lossless file (the exact area-matched
    values); ``lerc_reference`` is the GDAL path's LERC file, whose nodata mask
    ``native`` must reproduce exactly. terrio's and GDAL's LERC values may differ
    by one quantization step; both stay inside the bound.
    """
    with rasterio.open(native) as a, rasterio.open(lossless) as b:
        levels = len(a.overviews(1))
        assert levels == len(b.overviews(1))
    for level in [None, *range(levels)]:
        kwargs = {} if level is None else {"overview_level": level}
        with rasterio.open(native, **kwargs) as a, rasterio.open(lossless, **kwargs) as b,                 rasterio.open(lerc_reference, **kwargs) as c:
            got = a.read(1, masked=True)
            exact = b.read(1, masked=True)
            gdal = c.read(1, masked=True)
        mask = np.ma.getmaskarray(got)
        assert np.array_equal(mask, np.ma.getmaskarray(exact)), f"level {level} mask"
        assert np.array_equal(mask, np.ma.getmaskarray(gdal)), f"level {level} mask vs GDAL LERC"
        error = np.abs(got.filled(0).astype("float64") - exact.filled(0).astype("float64"))
        # float32 storage of the decoded value adds at most one ulp.
        slack = tolerance + np.spacing(np.abs(exact.filled(0)).astype("float32")).astype("float64")
        assert (error <= slack).all(), f"level {level}: max error {error.max()}"


def _both_engines(monkeypatch, source: Path, tmp_path: Path, **kwargs):
    monkeypatch.setenv(cog.COG_ENGINE_ENV, "gdal")
    reference = cog.area_matched_cog(source, tmp_path / "gdal.tif", **kwargs)
    monkeypatch.setenv(cog.COG_ENGINE_ENV, "auto")
    native = cog.area_matched_cog(source, tmp_path / "native.tif", **kwargs)
    return reference, native


def _same_report(reference, native) -> None:
    for name in (
        "method", "factors", "base_valid_pixels", "area_ratios", "fallback_reason",
        "compression", "lossy_max_z_error",
    ):
        assert getattr(reference, name) == getattr(native, name), name
    assert reference.as_tags() == native.as_tags()


# ---------------------------------------------------------------------------
# Area ratios: computed without the float accumulators (no terrio needed)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("density", [0.002, 0.05, 0.5, 0.97])
def test_scanned_area_ratios_equal_the_builders(tmp_path, density):
    """The ratio scan is exact, including levels with fewer wet cells than the target."""
    rng = np.random.default_rng(int(density * 1000))
    data = rng.random((777, 1301)).astype("float32")
    data[rng.random(data.shape) > density] = -9999.0
    source = _write(tmp_path / "speckle.tif", data, nodata=-9999.0)
    with rasterio.open(source) as probe:
        factors = cog.overview_factors(probe.width, probe.height, blocksize=64)
        base_valid, ratios, has_nan = cog._scan_area_matched(probe, factors)
    _, report = cog.build_area_matched_overviews(source, tmp_path / "levels", factors=factors)
    assert (base_valid, ratios, has_nan) == (report.base_valid_pixels, report.area_ratios, False)


def test_scan_flags_nan_under_a_numeric_nodata(tmp_path):
    data = _flood(nodata=-9999.0)
    data[600, 500] = np.nan
    source = _write(tmp_path / "nan.tif", data, nodata=-9999.0)
    with rasterio.open(source) as probe:
        assert cog._scan_area_matched(probe, [2, 4])[2] is True


# ---------------------------------------------------------------------------
# Native COG writer == GDAL VRT path
# ---------------------------------------------------------------------------

needs_writer = pytest.mark.skipif(
    cog.terrio_cog_writer() is None, reason="installed terrio has no area-matched COG writer"
)


def _flood_source(tmp_path: Path, name: str, nodata) -> Path:
    return _write(
        tmp_path / f"{name}.tif", _flood(nodata=nodata), nodata=nodata,
        tags={"TIFFTAG_IMAGEDESCRIPTION": "Depth (Max)", "TIFFTAG_ARTIST": "HEC-RAS", "custom": "x"},
        band_tags={"UNITS": "feet", "TYPE": "Depth"}, description="depth",
    )


@needs_writer
@pytest.mark.parametrize(
    ("name", "kwargs"),
    [
        ("lossless", {"lerc_max_z_error": None}),
        ("zstd_lossless", {"lerc_max_z_error": None, "compress": "ZSTD"}),
    ],
)
def test_native_lossless_float_cog_matches_the_gdal_path(tmp_path, monkeypatch, name, kwargs):
    source = _flood_source(tmp_path, name, -9999.0)
    reference, native = _both_engines(monkeypatch, source, tmp_path, blocksize=128, **kwargs)
    assert (reference.engine, native.engine) == ("gdal", "terrio")
    _same_report(reference, native)
    _assert_same_cog(tmp_path / "gdal.tif", tmp_path / "native.tif")
    assert cog.validate_cog(tmp_path / "native.tif").valid


@needs_writer
@pytest.mark.parametrize(
    ("name", "nodata", "kwargs"),
    [
        ("lerc_nan_nodata", np.nan, {}),
        ("lerc_numeric_nodata", -9999.0, {}),
        ("terrain_options", -9999.0, {"creation_options": cog.TERRAIN_CREATION_OPTIONS}),
        ("lerc_tighter", -9999.0, {"lerc_max_z_error": 0.001}),
    ],
)
def test_native_lerc_cog_holds_the_bound(tmp_path, monkeypatch, name, nodata, kwargs):
    """terrio encodes LERC itself: same bound, same mask, same tags as GDAL."""
    source = _flood_source(tmp_path, name, nodata)
    reference, native = _both_engines(monkeypatch, source, tmp_path, blocksize=128, **kwargs)
    tolerance = kwargs.get("lerc_max_z_error", cog.DEFAULT_LERC_MAX_Z_ERROR)
    assert (reference.engine, native.engine) == ("gdal", "terrio")
    assert native.lossy_max_z_error == tolerance
    _same_report(reference, native)

    monkeypatch.setenv(cog.COG_ENGINE_ENV, "gdal")
    lossless_kwargs = {**kwargs, "lerc_max_z_error": None}
    cog.area_matched_cog(source, tmp_path / "lossless.tif", blocksize=128, **lossless_kwargs)
    _assert_lerc_within_tolerance(
        tmp_path / "native.tif", tmp_path / "lossless.tif", tmp_path / "gdal.tif", tolerance
    )
    with rasterio.open(tmp_path / "gdal.tif") as a, rasterio.open(tmp_path / "native.tif") as b:
        assert a.tags() == b.tags()
        assert b.tags()["compression_lossy"] == "true"
        assert b.tags()["compression_max_z_error"] == f"{tolerance:g}"
        for key in ("overview_method", "overview_factors", "overview_area_ratios",
                    "overview_base_valid_pixels"):
            assert key in b.tags()
        assert (a.tags(1), a.descriptions, a.units) == (b.tags(1), b.descriptions, b.units)
        assert a.overviews(1) == b.overviews(1) and a.block_shapes == b.block_shapes
        assert b.tags(ns="IMAGE_STRUCTURE")["COMPRESSION"] == "LERC_DEFLATE"
    assert cog.validate_cog(tmp_path / "native.tif").valid


@needs_writer
def test_native_integer_cog_matches_the_gdal_path(tmp_path, monkeypatch):
    data = np.rint(_flood(nodata=-1) * 100).astype("int16")
    source = _write(tmp_path / "int.tif", data, nodata=-1)
    reference, native = _both_engines(monkeypatch, source, tmp_path, blocksize=128)
    assert native.engine == "terrio" and native.lossy_max_z_error is None
    _same_report(reference, native)
    _assert_same_cog(tmp_path / "gdal.tif", tmp_path / "native.tif")


@needs_writer
def test_native_categorical_cog_matches_the_gdal_path(tmp_path, monkeypatch):
    source = _write(tmp_path / "classes.tif", _classes(), nodata=0)
    reference, native = _both_engines(monkeypatch, source, tmp_path, blocksize=128, categorical=True)
    assert native.engine == "terrio" and native.method == "area_matched_mode"
    _same_report(reference, native)
    _assert_same_cog(tmp_path / "gdal.tif", tmp_path / "native.tif")


@needs_writer
def test_nan_under_a_numeric_nodata_keeps_the_gdal_path(tmp_path, monkeypatch):
    """terrio treats NaN as invalid; rasterio's mask does not. Never mix them."""
    data = _flood(nodata=-9999.0)
    data[600, 500] = np.nan
    source = _write(tmp_path / "nan.tif", data, nodata=-9999.0)
    monkeypatch.setenv(cog.COG_ENGINE_ENV, "auto")
    report = cog.area_matched_cog(source, tmp_path / "out.tif", blocksize=128, lerc_max_z_error=None)
    assert report.engine == "gdal" and report.method == "area_matched_mean"


@needs_writer
def test_no_nodata_still_falls_back_and_says_so(tmp_path, monkeypatch):
    source = _write(tmp_path / "no_nodata.tif", _flood(nodata=0.0), nodata=None)
    monkeypatch.setenv(cog.COG_ENGINE_ENV, "auto")
    report = cog.area_matched_cog(source, tmp_path / "out.tif", blocksize=128)
    assert report.engine == "gdal" and report.fallback_reason


def test_explicit_terrio_engine_requires_the_writer(tmp_path, monkeypatch):
    source = _write(tmp_path / "flood.tif", _flood(nodata=-9999.0), nodata=-9999.0)
    monkeypatch.setenv(cog.COG_ENGINE_ENV, "terrio")
    monkeypatch.setattr(cog, "terrio_cog_writer", lambda: None)
    with pytest.raises(RuntimeError, match="area-matched"):
        cog.area_matched_cog(source, tmp_path / "out.tif", blocksize=128)


def test_gdal_engine_disables_terrio(monkeypatch):
    monkeypatch.setenv(cog.COG_ENGINE_ENV, "gdal")
    assert cog.terrio_cog_writer() is None
    assert cog.terrio_cog_validator() is None
    monkeypatch.setenv(cog.COG_ENGINE_ENV, "bogus")
    with pytest.raises(ValueError):
        cog.cog_engine()


# ---------------------------------------------------------------------------
# terrio.validate_cog == rio_cogeo.cog_validate
# ---------------------------------------------------------------------------


@pytest.mark.skipif(cog.terrio_cog_validator() is None, reason="installed terrio has no validate_cog")
def test_native_layout_check_agrees_with_rio_cogeo(tmp_path, monkeypatch):
    rio_cogeo = pytest.importorskip("rio_cogeo.cogeo")
    good = tmp_path / "good.tif"
    cog.area_matched_cog(
        _write(tmp_path / "src.tif", _flood(nodata=-9999.0), nodata=-9999.0), good, blocksize=128
    )
    plain = _write(tmp_path / "plain.tif", _flood(nodata=-9999.0), nodata=-9999.0)
    for path in (good, plain):
        native = cog._check_layout(path)
        expected = rio_cogeo.cog_validate(str(path), quiet=True)
        assert native[0] == expected[0]
        assert list(native[1]) == list(expected[1]) and list(native[2]) == list(expected[2])
    assert cog.validate_cog(good).valid and cog.validate_cog(good).layout_checked
    assert not cog.validate_cog(plain).valid


# ---------------------------------------------------------------------------
# Streaming raster tiles == in-memory raster tiles
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not raster_tiles.streaming_dem_supported(), reason="installed terrio cannot stream dem"
)
@pytest.mark.parametrize(
    ("kind", "hillshade", "bounded_warp"),
    [("terrain", True, True), ("terrain", False, True), ("result", False, True),
     ("terrain", True, False)],
)
def test_streaming_pmtiles_are_byte_identical(tmp_path, monkeypatch, kind, hillshade, bounded_warp):
    pytest.importorskip("terrio")
    y, x = np.mgrid[0:700, 0:900].astype("float32")
    dem = 500 + 0.4 * x + 30 * np.sin(y / 18.0)
    dem[:, :60] = -9999.0
    dem[300:340, 400:460] = -9999.0
    source = _write(tmp_path / "dem.tif", dem.astype("float32"), nodata=-9999.0)
    ramp = tmp_path / "ramp.txt"
    ramp.write_text("500 0 128 0 255\n900 200 50 0 255\nnv 0 128 0 0\n", encoding="ascii")
    if not bounded_warp:
        monkeypatch.setattr(raster_tiles, "streaming_warp_supported", lambda: False)
    common = dict(ramp_path=ramp, max_zoom=16, kind=kind, hillshade=hillshade, name="t")
    raster_tiles._render_streaming(
        source, tmp_path / "streamed.pmtiles", scratch_dir=tmp_path, **common
    )
    raster_tiles._render_in_memory(source, tmp_path / "memory.pmtiles", **common)
    assert (tmp_path / "streamed.pmtiles").read_bytes() == (tmp_path / "memory.pmtiles").read_bytes()
    assert not [p for p in tmp_path.iterdir() if p.name.startswith("ras2cng-tiles-")]
