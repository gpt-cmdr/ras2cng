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


needs_report = pytest.mark.skipif(
    cog.terrio_cog_writer() is None or not cog.terrio_writes_report(cog.terrio_cog_writer()),
    reason="installed terrio does not write the area report (gpt-cmdr/terrio #23)",
)


@needs_writer
def test_nan_under_a_numeric_nodata_keeps_the_gdal_path_without_the_report(tmp_path, monkeypatch):
    """Older terrio treats NaN as invalid; rasterio's mask does not. Never mix them."""
    data = _flood(nodata=-9999.0)
    data[600, 500] = np.nan
    source = _write(tmp_path / "nan.tif", data, nodata=-9999.0)
    monkeypatch.setenv(cog.COG_ENGINE_ENV, "auto")
    monkeypatch.setattr(cog, "terrio_writes_report", lambda terrio: False)
    report = cog.area_matched_cog(source, tmp_path / "out.tif", blocksize=128, lerc_max_z_error=None)
    assert report.engine == "gdal" and report.method == "area_matched_mean"


@needs_report
def test_nan_under_a_numeric_nodata_stays_on_terrio_with_the_report(tmp_path, monkeypatch):
    """terrio #23 counts NaN as valid, as rasterio's mask does: same file as GDAL."""
    data = _flood(nodata=-9999.0)
    data[600, 500] = np.nan
    data[17, 500:505] = np.nan
    source = _write(tmp_path / "nan.tif", data, nodata=-9999.0)
    reference, native = _both_engines(
        monkeypatch, source, tmp_path, blocksize=128, lerc_max_z_error=None
    )
    assert (reference.engine, native.engine, native.ratio_source) == ("gdal", "terrio", "terrio")
    assert native.nan_pixels == 6
    _same_report(reference, native)
    _assert_same_cog(tmp_path / "gdal.tif", tmp_path / "native.tif")


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
# terrio's overview report (gpt-cmdr/terrio #18) cross-checks the scanned tags
# ---------------------------------------------------------------------------


def _scanned(method="area_matched_mean"):
    return cog.OverviewReport(
        method=method, factors=[2, 4], base_valid_pixels=1000,
        area_ratios=[1.0, 0.9999996], engine="terrio",
    )


def test_terrio_report_check_accepts_the_tagged_pyramid():
    native = {
        "method": "area_matched", "factors": [2, 4], "base_valid_pixels": [1000],
        # Equal at the tag's precision (%.6f) is what the file records.
        "area_ratios": [[1.0, 0.99999962]],
    }
    cog._check_terrio_report(native, _scanned())
    cog._check_terrio_report({**native, "method": "area_matched_mode"}, _scanned("area_matched_mode"))


@pytest.mark.parametrize(
    "change",
    [
        {"method": "area_matched_mode"},
        {"factors": [2, 4, 8]},
        {"base_valid_pixels": [999]},
        {"area_ratios": [[1.0, 0.999]]},
        {"area_ratios": [[1.0, 1.0], [1.0, 1.0]]},
        {"area_ratios": []},
    ],
)
def test_terrio_report_check_refuses_any_disagreement(change):
    native = {
        "method": "area_matched", "factors": [2, 4], "base_valid_pixels": [1000],
        "area_ratios": [[1.0, 0.9999996]], **change,
    }
    with pytest.raises(cog._TerrioReportMismatch):
        cog._check_terrio_report(native, _scanned())


@needs_writer
def test_report_mismatch_discards_the_terrio_file_and_uses_gdal(tmp_path, monkeypatch):
    source = _flood_source(tmp_path, "mismatch", -9999.0)
    monkeypatch.setenv(cog.COG_ENGINE_ENV, "auto")

    def disagree(native, report):
        raise cog._TerrioReportMismatch("forced")

    monkeypatch.setattr(cog, "_check_terrio_report", disagree)
    # A writer without the report never reaches the check; force one that has it.
    real = cog._terrio_area_matched_cog

    def with_report(terrio, *args, **kwargs):
        class Wrapped:
            def __getattr__(self, name):
                return getattr(terrio, name)

            def open(self, path):
                raster = terrio.open(path)

                class R:
                    def write(self, *a, **k):
                        raster.write(*a, **k)
                        return {"method": "area_matched"}

                return R()

        return real(Wrapped(), *args, **kwargs)

    monkeypatch.setattr(cog, "_terrio_area_matched_cog", with_report)
    out = tmp_path / "out.tif"
    with pytest.warns(RuntimeWarning, match="overview report (disagrees|for out.tif is unusable)"):
        report = cog.area_matched_cog(source, out, blocksize=128, lerc_max_z_error=None)
    assert report.engine == "gdal" and report.ratio_source == "builder"
    assert not list(tmp_path.glob(".out.tif.partial-*"))
    assert cog.validate_cog(out).valid


@needs_writer
@pytest.mark.parametrize("nodata", [-9999.0, np.nan])
def test_terrio_report_equals_the_scanned_tags(tmp_path, monkeypatch, nodata):
    """The tag the scan wrote is what terrio's writer measured, digit for digit."""
    source = _flood_source(tmp_path, "report", nodata)
    monkeypatch.setenv(cog.COG_ENGINE_ENV, "auto")
    monkeypatch.setattr(cog, "terrio_writes_report", lambda terrio: False)
    report = cog.area_matched_cog(source, tmp_path / "out.tif", blocksize=128)
    assert report.engine == "terrio"
    if report.ratio_source != "scan+terrio":
        pytest.skip("installed terrio does not report area ratios (gpt-cmdr/terrio #18)")
    with rasterio.open(tmp_path / "out.tif") as src:
        tags, native = src.tags(), src.tags(ns="OVERVIEW_REPORT")
    assert native["OVERVIEW_FACTORS"] == tags["overview_factors"]
    assert native["OVERVIEW_BASE_VALID_PIXELS"] == tags["overview_base_valid_pixels"]
    assert ",".join(
        f"{float(r):.6f}" for r in native["OVERVIEW_AREA_RATIOS"].split(",")
    ) == tags["overview_area_ratios"]


# ---------------------------------------------------------------------------
# terrio writes the report itself (gpt-cmdr/terrio #23): no scan, same file
# ---------------------------------------------------------------------------


def _scan_oracle(path: Path, blocksize: int):
    with rasterio.open(path) as probe:
        factors = cog.overview_factors(probe.width, probe.height, blocksize=blocksize)
        return factors, cog._scan_area_matched(probe, factors)


@needs_report
@pytest.mark.parametrize(
    ("name", "nodata", "kwargs"),
    [
        ("lossless", -9999.0, {"lerc_max_z_error": None}),
        ("lerc_nan", np.nan, {}),
        ("terrain", -9999.0, {"creation_options": cog.TERRAIN_CREATION_OPTIONS}),
    ],
)
def test_report_path_writes_the_scan_paths_file(tmp_path, monkeypatch, name, nodata, kwargs):
    """Dropping the scan changes nothing on disk: byte-identical to the scan path."""
    source = _flood_source(tmp_path, name, nodata)
    monkeypatch.setenv(cog.COG_ENGINE_ENV, "auto")
    calls = []
    real_scan = cog._scan_area_matched

    def counted(*args, **kwargs):
        calls.append(1)
        return real_scan(*args, **kwargs)

    monkeypatch.setattr(cog, "_scan_area_matched", counted)
    native = cog.area_matched_cog(source, tmp_path / "report.tif", blocksize=128, **kwargs)
    assert calls == [] and native.engine == "terrio" and native.ratio_source == "terrio"
    monkeypatch.setattr(cog, "terrio_writes_report", lambda terrio: False)
    scanned = cog.area_matched_cog(source, tmp_path / "scan.tif", blocksize=128, **kwargs)
    assert calls == [1] and scanned.ratio_source in ("scan", "scan+terrio")
    assert (tmp_path / "report.tif").read_bytes() == (tmp_path / "scan.tif").read_bytes()
    factors, (base_valid, ratios, _) = _scan_oracle(source, 128)
    assert native.factors == factors and native.base_valid_pixels == base_valid
    assert [f"{r:.6f}" for r in native.area_ratios] == [f"{r:.6f}" for r in ratios]
    assert native.as_tags() == scanned.as_tags()
    with rasterio.open(tmp_path / "report.tif") as src:
        tags = src.tags()
    assert tags["overview_area_ratios"] == ",".join(f"{r:.6f}" for r in ratios)
    assert tags["overview_base_valid_pixels"] == str(base_valid)


@needs_report
def test_report_path_drops_stale_report_tags_from_a_recogged_source(tmp_path, monkeypatch):
    """A ras2cng COG fed back in carries the report tags; terrio rewrites them."""
    source = _flood_source(tmp_path, "first", -9999.0)
    monkeypatch.setenv(cog.COG_ENGINE_ENV, "auto")
    cog.area_matched_cog(source, tmp_path / "first_cog.tif", blocksize=128, lerc_max_z_error=None)
    with rasterio.open(tmp_path / "first_cog.tif") as src:
        assert "overview_area_ratios" in src.tags()
    again = cog.area_matched_cog(
        tmp_path / "first_cog.tif", tmp_path / "again.tif", blocksize=256, lerc_max_z_error=None
    )
    assert again.engine == "terrio" and again.ratio_source == "terrio"
    factors, (base_valid, ratios, _) = _scan_oracle(tmp_path / "first_cog.tif", 256)
    with rasterio.open(tmp_path / "again.tif") as src:
        assert src.tags()["overview_area_ratios"] == ",".join(f"{r:.6f}" for r in ratios)
        assert src.tags()["overview_factors"] == ",".join(str(f) for f in factors)
        assert src.tags()["overview_base_valid_pixels"] == str(base_valid)


@needs_report
def test_report_path_sends_an_empty_raster_to_gdal(tmp_path, monkeypatch):
    data = np.full((700, 900), -9999.0, dtype="float32")
    source = _write(tmp_path / "empty.tif", data, nodata=-9999.0)
    monkeypatch.setenv(cog.COG_ENGINE_ENV, "auto")
    report = cog.area_matched_cog(source, tmp_path / "out.tif", blocksize=128)
    assert report.engine == "gdal" and report.fallback_reason
    assert not list(tmp_path.glob(".out.tif.partial-*"))


def test_written_report_check_refuses_inconsistent_reports():
    report = cog.OverviewReport(method="area_matched_mean", factors=[2, 4])
    good = {"method": "area_matched", "factors": [2, 4], "base_valid_pixels": [5],
            "area_ratios": [[1.0, 1.0]], "nan_pixels": [0]}
    cog._check_terrio_written_report(good, report, "area_matched")
    for change in ({"method": "area_matched_mode"}, {"factors": [2]}, {"area_ratios": [[1.0]]},
                   {"base_valid_pixels": [1, 2]}, {"area_ratios": []}):
        with pytest.raises(cog._TerrioReportMismatch):
            cog._check_terrio_written_report({**good, **change}, report, "area_matched")
    with pytest.raises(cog._TerrioReportMismatch):
        cog._check_terrio_written_report(None, report, "area_matched")
    with pytest.raises(cog._TerrioDeclined):
        cog._check_terrio_written_report({**good, "base_valid_pixels": [0]}, report, "area_matched")


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
