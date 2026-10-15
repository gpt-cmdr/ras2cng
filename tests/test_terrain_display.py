"""Tests for the display-only terrain derivative policy (tiny synthetic arrays)."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

from ras2cng import cog
from ras2cng import terrain_display as td

METRE_CRS = "EPSG:32614"
# NAD83 / Texas Centric Albers in US survey feet (no exact EPSG match).
FOOT_CRS_WKT = (
    'PROJCS["NAD_1983_Texas_Centric_Albers_Equal_Area",'
    'GEOGCS["GCS_North_American_1983",'
    'DATUM["D_North_American_1983",SPHEROID["GRS_1980",6378137.0,298.257222101]],'
    'PRIMEM["Greenwich",0.0],UNIT["Degree",0.0174532925199433]],'
    'PROJECTION["Albers"],'
    'PARAMETER["False_Easting",1500000.0],PARAMETER["False_Northing",6000000.0],'
    'PARAMETER["Central_Meridian",-100.0],'
    'PARAMETER["Standard_Parallel_1",27.5],PARAMETER["Standard_Parallel_2",35.0],'
    'PARAMETER["Latitude_Of_Origin",18.0],'
    'UNIT["Foot_US",0.3048006096012192]]'
)
NODATA = -32768.0


def _write_source(
    path: Path,
    array: np.ndarray,
    *,
    pixel: float,
    crs=METRE_CRS,
    nodata: float | None = NODATA,
    transform=None,
    units: str | None = None,
    dtype: str = "float32",
) -> Path:
    height, width = array.shape
    with rasterio.open(
        path, "w", driver="GTiff", width=width, height=height, count=1, dtype=dtype,
        nodata=nodata, crs=crs, tiled=True, blockxsize=128, blockysize=128,
        transform=transform or from_origin(500_000.0, 3_000_000.0, pixel, pixel),
    ) as dst:
        dst.write(array.astype(dtype), 1)
        if units:
            dst.units = (units,)
    return path


def _sloped(height: int, width: int, *, hole: bool = True) -> np.ndarray:
    yy, xx = np.mgrid[0:height, 0:width]
    values = 100.0 + 0.0123 * xx + 0.0071 * yy + 0.37 * np.sin(xx / 17.0)
    if hole:
        values[: height // 5, : width // 4] = NODATA
    return values.astype("float32")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# Units
# ---------------------------------------------------------------------------


def test_bare_foot_is_ambiguous_and_never_inferred() -> None:
    with pytest.raises(td.UnitsError, match="international_foot or us_survey_foot"):
        td.resolve_vertical_unit("ft")
    with pytest.raises(td.UnitsError, match="missing"):
        td.resolve_vertical_unit(None)
    assert td.resolve_vertical_unit("us-survey foot") == td.US_SURVEY_FOOT
    assert td.resolve_vertical_unit("International Foot") == td.INTERNATIONAL_FOOT
    assert td.resolve_vertical_unit("m") == td.METRE


def test_declared_and_embedded_vertical_units_must_agree() -> None:
    with pytest.raises(td.UnitsError, match="contradiction"):
        td.resolve_vertical_unit("metre", "us_survey_foot")
    # An ambiguous embedded spelling does not contradict a precise declaration.
    assert td.resolve_vertical_unit("us_survey_foot", "ft") == td.US_SURVEY_FOOT
    # ...but it cannot stand in for one.
    with pytest.raises(td.UnitsError, match="bare foot"):
        td.resolve_vertical_unit(None, "ft")


def test_inspect_keeps_horizontal_and_vertical_units_separate(tmp_path: Path) -> None:
    path = _write_source(tmp_path / "f.tif", _sloped(40, 40), pixel=3.0, crs=FOOT_CRS_WKT)
    info = td.inspect_source(path, vertical_unit="metre", vertical_datum="declared-datum")
    assert info.horizontal_unit == td.US_SURVEY_FOOT
    assert info.vertical_unit == td.METRE
    assert info.horizontal_unit_metres == pytest.approx(1200 / 3937)
    assert info.vertical_datum == "declared-datum"
    assert info.size_bytes == path.stat().st_size and info.sha256 is None

    unknown = td.inspect_source(path, vertical_unit="metre")
    assert unknown.vertical_datum is None  # unknown stays unknown


def test_inspect_rejects_missing_vertical_unit_and_geographic_crs(tmp_path: Path) -> None:
    path = _write_source(tmp_path / "m.tif", _sloped(20, 20), pixel=3.0)
    with pytest.raises(td.UnitsError):
        td.inspect_source(path)
    geographic = _write_source(
        tmp_path / "g.tif", _sloped(20, 20), pixel=0.001, crs="EPSG:4326"
    )
    with pytest.raises(td.GridError, match="geographic"):
        td.inspect_source(geographic, vertical_unit="metre")


def test_band_unit_is_used_when_unambiguous(tmp_path: Path) -> None:
    path = _write_source(tmp_path / "u.tif", _sloped(20, 20), pixel=3.0, units="metre")
    assert td.inspect_source(path).vertical_unit == td.METRE


# ---------------------------------------------------------------------------
# Grid / factor
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("native_m", "factor", "disposition"),
    [
        (3.0, 1, "native_grid"),
        (1.0, 3, "integer_multiple"),
        (0.5, 6, "integer_multiple"),
        (0.9144, 3, "integer_multiple"),  # 3 ft -> 9 ft
        (0.3048, 10, "integer_multiple"),  # 1 ft -> 10 ft
        (3.048, 1, "native_grid"),
        (30.0, 1, "native_coarser_than_window"),
        (9.5546, 1, "native_coarser_than_window"),  # never upsample
    ],
)
def test_factor_is_the_integer_multiple_nearest_ten_feet(native_m, factor, disposition) -> None:
    assert td.choose_factor(native_m, td.DisplayPolicy()) == (factor, disposition)


def test_no_integer_multiple_in_window_is_refused_not_fitted() -> None:
    # 2 m: 1x = 2.0 m (below 7.5 ft), 2x = 4.0 m (above 12.5 ft).
    with pytest.raises(td.GridError, match="no integer multiple"):
        td.choose_factor(2.0, td.DisplayPolicy())


def test_plan_preserves_origin_and_integer_aligns(tmp_path: Path) -> None:
    path = _write_source(tmp_path / "s.tif", _sloped(99, 132), pixel=1.0)
    plan = td.plan_display_terrain(td.inspect_source(path, vertical_unit="metre"))
    assert plan.factor == 3
    assert plan.origin == (500_000.0, 3_000_000.0)
    assert plan.transform[0] == 3.0 and plan.transform[4] == -3.0
    # Divisible grids only: the display grid covers the source exactly.
    assert (plan.width, plan.height) == (44, 33)
    assert plan.width * plan.factor == 132 and plan.height * plan.factor == 99
    assert plan.decoder_qualification == "unqualified"
    assert plan.to_dict()["factor"] == 3


def test_plan_in_feet_gives_nine_foot_cells(tmp_path: Path) -> None:
    path = _write_source(tmp_path / "ft.tif", _sloped(60, 60), pixel=3.0, crs=FOOT_CRS_WKT)
    plan = td.plan_display_terrain(td.inspect_source(path, vertical_unit="us_survey_foot"))
    assert plan.factor == 3
    assert plan.transform[0] == 9.0  # 9 ft in CRS units, not 2.743 m of "3 m"
    assert plan.pixel_size_m == pytest.approx(9 * 1200 / 3937)


def test_plan_refuses_contradictory_sources(tmp_path: Path) -> None:
    good = td.inspect_source(
        _write_source(tmp_path / "g.tif", _sloped(30, 30), pixel=3.0), vertical_unit="metre"
    )
    with pytest.raises(td.NodataError):
        td.plan_display_terrain(replace(good, nodata=None))
    with pytest.raises(td.GridError, match="rotated"):
        td.plan_display_terrain(replace(good, transform=(3.0, 0.2, 0.0, 0.2, -3.0, 0.0)))
    with pytest.raises(td.GridError, match="non-square"):
        td.plan_display_terrain(replace(good, transform=(3.0, 0.0, 0.0, 0.0, -4.0, 0.0)))
    with pytest.raises(td.GridError, match="scale/offset"):
        td.plan_display_terrain(replace(good, scale=0.01))
    with pytest.raises(td.UnitsError):
        td.plan_display_terrain(replace(good, vertical_unit="furlong"))


def test_policy_rejects_nonsense() -> None:
    with pytest.raises(td.DisplayTerrainError):
        td.DisplayPolicy(increment=0.3)  # 1/0.3 is not an integer
    with pytest.raises(td.DisplayTerrainError):
        td.DisplayPolicy(min_spacing_m=5.0, target_spacing_m=3.0)
    with pytest.raises(td.DisplayTerrainError):
        td.DisplayPolicy(output_nodata=0.1)  # not exactly float32
    with pytest.raises(td.DisplayTerrainError):
        td.CodecPolicy("lerc", "LERC_ZSTD", 19, "YES")  # predictor on LERC
    with pytest.raises(td.DisplayTerrainError):
        td.CodecPolicy("zstd", "ZSTD", 99)


# ---------------------------------------------------------------------------
# Pure array steps
# ---------------------------------------------------------------------------


def test_average_uses_valid_cells_only_and_threshold_decides_footprint() -> None:
    values = np.array([[1.0, 2.0, 0.0], [3.0, 4.0, 0.0], [0.0, 0.0, 0.0]])
    valid = np.array([[1, 1, 0], [1, 1, 0], [0, 0, 0]], dtype=bool)
    out, kept = td.reduce_to_display_grid(
        values, valid, 3, resampling="average", min_valid_fraction=0.4
    )
    assert out.shape == (1, 1) and kept[0, 0]
    assert out[0, 0] == pytest.approx(2.5)  # mean of 4 valid, nodata not averaged in

    _, kept = td.reduce_to_display_grid(
        values, valid, 3, resampling="average", min_valid_fraction=0.5
    )
    assert not kept[0, 0]  # 4 / 9 < 0.5


def test_partial_edge_block_is_judged_against_in_extent_cells() -> None:
    values = np.arange(20, dtype="float64").reshape(4, 5)
    valid = np.ones((4, 5), dtype=bool)
    out, kept = td.reduce_to_display_grid(
        values, valid, 3, resampling="average", min_valid_fraction=1.0
    )
    assert out.shape == (2, 2) and kept.all()  # edge blocks are fully valid in-extent
    assert out[1, 1] == pytest.approx(values[3:, 3:].mean())


def test_nearest_takes_the_centre_cell_and_clamps_on_edges() -> None:
    values = np.arange(25, dtype="float64").reshape(5, 5)
    valid = np.ones((5, 5), dtype=bool)
    out, _ = td.reduce_to_display_grid(
        values, valid, 3, resampling="nearest", min_valid_fraction=0.5
    )
    assert out[0, 0] == values[1, 1]
    assert out[1, 1] == values[4, 4]  # block 3..4, centre 4 is the last in-extent cell


def test_quantization_rounds_half_even_on_exact_ties_and_bounds_error() -> None:
    values = np.array([0.125, 0.375, 1.234, -2.718, 7.0, 9999.0])
    valid = np.array([1, 1, 1, 1, 1, 0], dtype=bool)
    out = td.quantize_elevation(values, valid, 0.01)
    assert out[0] == pytest.approx(0.12)  # 12.5 -> even step
    assert out[1] == pytest.approx(0.38)  # 37.5 -> even step
    assert out[5] == 9999.0  # invalid untouched
    assert np.abs(out[valid] - values[valid]).max() <= 0.005 + 1e-12
    assert td.quantization_error_bound(1000.0) == pytest.approx(0.005 + 1000 * 2.0**-24)


# ---------------------------------------------------------------------------
# End to end on a tiny synthetic raster
# ---------------------------------------------------------------------------


@pytest.fixture()
def written(tmp_path: Path):
    source = _write_source(tmp_path / "src.tif", _sloped(1500, 1800), pixel=1.0)
    before = _sha(source)
    plan = td.plan_display_terrain(td.inspect_source(source, vertical_unit="metre", vertical_datum="declared"))
    out = tmp_path / "display.tif"
    result = td.write_display_terrain(plan, source, out)
    return source, before, plan, out, result


def test_write_produces_aligned_quantized_cog_and_leaves_source_alone(written) -> None:
    source, before, plan, out, result = written
    assert _sha(source) == before
    assert (plan.width, plan.height) == (600, 500)

    with rasterio.open(out) as dst, rasterio.open(source) as src:
        assert dst.dtypes[0] == "float32" and dst.nodata == -9999.0
        assert dst.crs == src.crs
        assert dst.transform.c == src.transform.c and dst.transform.f == src.transform.f
        assert dst.transform.a == 3.0 and dst.transform.e == -3.0
        base = dst.read(1)
        assert dst.overviews(1) == cog.overview_factors(600, 500, blocksize=512)
        assert dst.profile["blockxsize"] == 512
        tags = dst.tags()
    valid = base != np.float32(-9999.0)
    assert valid.sum() == result.display_valid_cells
    assert (np.rint(base[valid].astype("float64") * 100) / 100).astype("float32").tolist() == base[valid].tolist()

    # Nodata hole is carried, not interpolated into land.
    assert not valid[:90, :140].any()
    assert valid[200:, 200:].all()
    assert result.footprint_area_ratio == pytest.approx(1.0, abs=0.02)
    assert result.observed_quantization_error <= 0.005 + 1e-9
    assert result.observed_quantization_error <= result.quantization_error_bound

    # The unit travels with the file: re-inspecting the derivative needs no declaration.
    assert td.inspect_source(out).vertical_unit == td.METRE
    assert tags["display_quantum"] == "0.01"
    assert tags["display_quantization_scope"] == "base_and_all_overviews"
    assert tags["overview_value_quantum"] == "0.01"
    assert tags["vertical_unit"] == "metre" and tags["vertical_datum"] == "declared"
    assert tags["vertical_datum_assurance"] == "declared_unverified"
    assert tags["vertical_unit_assurance"] == "declared_only"
    assert tags["display_source_shape"] == "1800x1500"
    assert tags["display_decoder_qualification"] == "unqualified"
    assert tags["overview_method"] == "area_matched_mean"
    assert "compression_lossy" not in tags


def test_validate_accepts_its_own_output(written) -> None:
    _, _, plan, out, result = written
    verdict = td.validate_display_terrain(plan, out, expected=result)
    assert verdict.valid, verdict.errors
    # Every embedded level was read back and is on the quantum, not just tagged.
    assert verdict.overview_levels_checked == [2]
    assert verdict.overview_levels_quantized is True
    assert not any("vacuous" in w for w in verdict.warnings)


def test_lossy_cog_environment_default_cannot_leak_into_the_derivative(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cog, "DEFAULT_LERC_MAX_Z_ERROR", 0.01)
    monkeypatch.setattr(cog, "DEFAULT_COMPRESS", "LERC_ZSTD")
    source = _write_source(tmp_path / "s.tif", _sloped(1500, 1500, hole=False), pixel=1.0)
    plan = td.plan_display_terrain(td.inspect_source(source, vertical_unit="metre"))
    out = tmp_path / "d.tif"
    result = td.write_display_terrain(plan, source, out)
    with rasterio.open(out) as dst:
        assert dst.tags(ns="IMAGE_STRUCTURE")["COMPRESSION"] == "DEFLATE"
        assert "compression_lossy" not in dst.tags()
    assert td.validate_display_terrain(plan, out, expected=result).valid


@pytest.mark.parametrize("codec", [td.DEFLATE_INTEGER_DIFF, td.ZSTD_HIGH, td.ZSTD_INTEGER_DIFF])
def test_alternative_lossless_codecs_write_and_validate(tmp_path: Path, codec) -> None:
    source = _write_source(tmp_path / "s.tif", _sloped(1302, 1302), pixel=1.0)
    info = td.inspect_source(source, vertical_unit="metre")
    plan = td.plan_display_terrain(info, td.DisplayPolicy(codec=codec))
    out = tmp_path / "o.tif"
    result = td.write_display_terrain(plan, source, out)
    verdict = td.validate_display_terrain(plan, out, expected=result)
    assert verdict.valid, verdict.errors
    with rasterio.open(out) as dst:
        assert dst.tags(ns="IMAGE_STRUCTURE")["COMPRESSION"] == codec.compress
        assert dst.tags()["display_codec"] == codec.name


def test_validate_flags_values_off_the_quantum_and_lossy_tags(written, tmp_path: Path) -> None:
    _, _, plan, out, _ = written
    with rasterio.open(out) as dst:
        data = dst.read(1)
    valid = data != np.float32(-9999.0)
    noisy = data.copy()
    noisy[valid] += np.float32(0.003)
    tampered_base = tmp_path / "tampered_base.tif"
    with rasterio.open(
        tampered_base, "w", driver="GTiff", width=plan.width, height=plan.height, count=1,
        dtype="float32", nodata=-9999.0, crs=plan.source.crs_wkt,
        transform=rasterio.Affine(*plan.transform), tiled=True, blockxsize=256, blockysize=256,
    ) as dst:
        dst.write(noisy, 1)
    lossy = tmp_path / "lossy.tif"
    cog.area_matched_cog(tampered_base, lossy, lerc_max_z_error=0.01)

    verdict = td.validate_display_terrain(plan, lossy)
    assert not verdict.valid
    assert any("quantum" in e for e in verdict.errors)
    assert any("lossy codec tolerance" in e for e in verdict.errors)
    assert any("compression" in e for e in verdict.errors)
    assert any("missing provenance tag display_policy" in e for e in verdict.errors)


def test_write_refuses_a_source_that_changed_after_planning(tmp_path: Path) -> None:
    source = _write_source(tmp_path / "s.tif", _sloped(60, 60), pixel=3.0)
    plan = td.plan_display_terrain(td.inspect_source(source, vertical_unit="metre"))
    _write_source(source, _sloped(60, 66), pixel=3.0)
    with pytest.raises(td.GridError, match="no longer matches"):
        td.write_display_terrain(plan, source, tmp_path / "out.tif")
    assert not (tmp_path / "out.tif").exists()


def test_values_too_large_for_the_quantum_are_refused(tmp_path: Path) -> None:
    array = _sloped(30, 30, hole=False)
    array[5, 5] = 2.0e6
    source = _write_source(tmp_path / "big.tif", array, pixel=3.0)
    plan = td.plan_display_terrain(td.inspect_source(source, vertical_unit="metre"))
    with pytest.raises(td.RepresentationError):
        td.write_display_terrain(plan, source, tmp_path / "o.tif")


def test_valid_value_equal_to_output_nodata_is_refused(tmp_path: Path) -> None:
    array = _sloped(30, 30, hole=False)
    array[3, 3] = -9999.0
    source = _write_source(tmp_path / "c.tif", array, pixel=3.0)
    plan = td.plan_display_terrain(td.inspect_source(source, vertical_unit="metre"))
    with pytest.raises(td.NodataError, match="output nodata"):
        td.write_display_terrain(plan, source, tmp_path / "o.tif")


def test_all_nodata_source_is_refused(tmp_path: Path) -> None:
    source = _write_source(tmp_path / "e.tif", np.full((30, 30), NODATA, "float32"), pixel=3.0)
    plan = td.plan_display_terrain(td.inspect_source(source, vertical_unit="metre"))
    with pytest.raises(td.NodataError, match="no valid"):
        td.write_display_terrain(plan, source, tmp_path / "o.tif")


def test_integer_source_is_supported_and_stays_unmodified(tmp_path: Path) -> None:
    array = (np.arange(40 * 40).reshape(40, 40) % 900).astype("int16")
    source = _write_source(tmp_path / "i.tif", array, pixel=3.0, nodata=-1, dtype="int16")
    before = _sha(source)
    plan = td.plan_display_terrain(td.inspect_source(source, vertical_unit="metre"))
    result = td.write_display_terrain(plan, source, tmp_path / "o.tif")
    assert _sha(source) == before
    assert result.observed_quantization_error == 0.0  # integers are already on the quantum


# ---------------------------------------------------------------------------
# Codec benchmark
# ---------------------------------------------------------------------------


def test_benchmark_reports_measured_bytes_and_selects_the_smallest_lossless(tmp_path: Path) -> None:
    source = _write_source(tmp_path / "s.tif", _sloped(900, 900), pixel=3.0)
    plan = td.plan_display_terrain(td.inspect_source(source, vertical_unit="metre"))
    results = td.benchmark_codecs(plan, source, tmp_path / "bench")

    assert [r.codec for r in results] == list(td.CANDIDATE_CODECS)
    assert not any(r.decoder_qualified for r in results)
    written_ok = [r for r in results if r.bytes is not None]
    assert written_ok, [r.error for r in results]  # DEFLATE is always available
    assert all(r.lossless for r in written_ok)
    assert all(r.error for r in results if r.bytes is None)

    best = td.select_smallest(results)
    assert best is not None
    assert best.bytes == min(r.bytes for r in written_ok)
    assert td.select_smallest([td.CodecBenchmark(td.DEFLATE_FLOAT, None, None, "x")]) is None


# ---------------------------------------------------------------------------
# T1 -- the destination can never be, or link to, the source
# ---------------------------------------------------------------------------


def _tiny_plan(tmp_path: Path, name: str = "s.tif", size: int = 30, **kw):
    source = _write_source(tmp_path / name, _sloped(size, size), pixel=3.0, **kw)
    return source, td.plan_display_terrain(td.inspect_source(source, vertical_unit="metre"))


def test_t1_same_path_is_refused_and_the_source_bytes_survive(tmp_path: Path) -> None:
    source, plan = _tiny_plan(tmp_path)
    before = _sha(source)
    for spelling in (source, tmp_path / "nowhere" / ".." / "s.tif"):
        with pytest.raises(td.AliasError, match="resolves to the source"):
            td.write_display_terrain(plan, source, spelling, overwrite=True)
    assert _sha(source) == before
    # Nothing was staged next to it either.
    assert [p.name for p in tmp_path.iterdir()] == ["s.tif"]


def test_t1_hard_link_alias_is_refused(tmp_path: Path) -> None:
    import os

    source, plan = _tiny_plan(tmp_path)
    before = _sha(source)
    linked = tmp_path / "hard.tif"
    os.link(source, linked)
    with pytest.raises(td.AliasError, match="hard link"):
        td.write_display_terrain(plan, source, linked, overwrite=True)
    assert _sha(source) == before and _sha(linked) == before


def test_t1_symlink_alias_is_refused(tmp_path: Path) -> None:
    import os

    source, plan = _tiny_plan(tmp_path)
    before = _sha(source)
    link = tmp_path / "soft.tif"
    try:
        os.symlink(source, link)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is not permitted on this host")
    with pytest.raises(td.AliasError):
        td.write_display_terrain(plan, source, link, overwrite=True)
    assert _sha(source) == before


def test_t1_overwrite_is_explicit_and_replacement_is_not_silent(tmp_path: Path) -> None:
    source, plan = _tiny_plan(tmp_path)
    out = tmp_path / "out.tif"
    td.write_display_terrain(plan, source, out)
    first = _sha(out)
    with pytest.raises(FileExistsError, match="overwrite=True"):
        td.write_display_terrain(plan, source, out)
    assert _sha(out) == first
    td.write_display_terrain(plan, source, out, overwrite=True)
    assert out.exists()
    with pytest.raises(td.AliasError, match="directory"):
        td.write_display_terrain(plan, source, tmp_path)


# ---------------------------------------------------------------------------
# T2 -- non-divisible grids fail closed; the footprint deviation is reported
# ---------------------------------------------------------------------------


def test_t2_non_divisible_grid_is_refused_not_padded(tmp_path: Path) -> None:
    # A fully valid 4x4 1 m source used to become a valid 2x2 3 m raster
    # spanning 6x6 m (area ratio 2.25): coverage the source never had.
    source = _write_source(tmp_path / "s.tif", np.full((4, 4), 5.0, "float32"), pixel=1.0)
    with pytest.raises(td.GridError, match="not divisible"):
        td.plan_display_terrain(td.inspect_source(source, vertical_unit="metre"))
    # Factor 1 divides everything, so odd dimensions are fine there.
    odd = _write_source(tmp_path / "o.tif", _sloped(31, 37), pixel=3.0)
    plan = td.plan_display_terrain(td.inspect_source(odd, vertical_unit="metre"))
    assert (plan.factor, plan.width, plan.height) == (1, 37, 31)


def _boundary_source(tmp_path: Path) -> Path:
    array = np.linspace(10.0, 20.0, 36, dtype="float32").reshape(6, 6)
    array[1, 1] = NODATA  # one hole inside the first 3x3 block
    return _write_source(tmp_path / "b.tif", array, pixel=1.0)


def test_t2_coarse_mask_cannot_follow_a_fine_hole_and_says_so(tmp_path: Path) -> None:
    source = _boundary_source(tmp_path)
    info = td.inspect_source(source, vertical_unit="metre")
    result = td.write_display_terrain(td.plan_display_terrain(info), source, tmp_path / "o.tif")
    # 8 of 9 valid at the default 0.5: the block is kept, so one invalid source
    # cell is published as valid coverage.  Reported, not hidden.
    assert result.source_valid_cells == 35
    assert result.display_valid_cells == 4
    assert result.footprint_added_source_cells == 1
    assert result.footprint_dropped_source_cells == 0
    assert result.footprint_area_ratio == pytest.approx(36 / 35)
    assert result.footprint_mismatch_fraction == pytest.approx(1 / 35)


def test_t2_threshold_one_drops_the_boundary_block_and_gate_can_refuse(tmp_path: Path) -> None:
    source = _boundary_source(tmp_path)
    info = td.inspect_source(source, vertical_unit="metre")
    strict = td.plan_display_terrain(info, td.DisplayPolicy(min_valid_fraction=1.0))
    result = td.write_display_terrain(strict, source, tmp_path / "strict.tif")
    assert result.display_valid_cells == 3
    assert result.footprint_dropped_source_cells == 8  # real data lost, reported
    assert result.footprint_added_source_cells == 0

    gated = td.plan_display_terrain(info, td.DisplayPolicy(max_footprint_mismatch_fraction=0.01))
    with pytest.raises(td.FootprintError, match="exceeds"):
        td.write_display_terrain(gated, source, tmp_path / "gated.tif")
    assert not (tmp_path / "gated.tif").exists()
    loose = td.plan_display_terrain(info, td.DisplayPolicy(max_footprint_mismatch_fraction=0.05))
    td.write_display_terrain(loose, source, tmp_path / "loose.tif")


# ---------------------------------------------------------------------------
# T3 -- validation compares every material provenance tag
# ---------------------------------------------------------------------------


def test_t3_contradictory_provenance_does_not_validate(written) -> None:
    _, _, plan, out, result = written
    assert td.validate_display_terrain(plan, out, expected=result).valid

    other = replace(
        plan,
        source=replace(plan.source, vertical_datum="a-different-datum"),
        policy=replace(plan.policy, resampling="nearest", min_valid_fraction=0.9),
    )
    verdict = td.validate_display_terrain(other, out)
    assert not verdict.valid
    joined = " | ".join(verdict.errors)
    assert "vertical_datum" in joined
    assert "display_resampling" in joined
    assert "display_min_valid_fraction" in joined
    assert "display_footprint_policy" in joined

    wrong_factor = replace(plan, factor=6)
    assert any("display_factor" in e for e in td.validate_display_terrain(wrong_factor, out).errors)
    qualified = replace(plan, decoder_qualification="qualified")
    assert any(
        "display_decoder_qualification" in e for e in td.validate_display_terrain(qualified, out).errors
    )


# ---------------------------------------------------------------------------
# T4 -- min_valid_fraction applies to every resampler
# ---------------------------------------------------------------------------


def test_t4_nearest_honours_min_valid_fraction() -> None:
    values = np.arange(9, dtype="float64").reshape(3, 3)
    valid = np.zeros((3, 3), dtype=bool)
    valid[1, 1] = True  # only the centre: one of nine
    for resampling in ("nearest", "average"):
        _, kept = td.reduce_to_display_grid(
            values, valid, 3, resampling=resampling, min_valid_fraction=1.0
        )
        assert not kept[0, 0], resampling
        out, kept = td.reduce_to_display_grid(
            values, valid, 3, resampling=resampling, min_valid_fraction=0.1
        )
        assert kept[0, 0] and out[0, 0] == values[1, 1], resampling


def test_t4_nearest_with_an_invalid_centre_is_invalid_even_when_the_share_is_met() -> None:
    values = np.arange(9, dtype="float64").reshape(3, 3)
    valid = np.ones((3, 3), dtype=bool)
    valid[1, 1] = False  # 8 of 9 valid, but the chosen centre is not
    _, kept = td.reduce_to_display_grid(
        values, valid, 3, resampling="nearest", min_valid_fraction=0.5
    )
    assert not kept[0, 0]
    _, kept = td.reduce_to_display_grid(
        values, valid, 3, resampling="average", min_valid_fraction=0.5
    )
    assert kept[0, 0]


def test_t4_even_factor_centre_is_the_documented_lower_right_middle_cell() -> None:
    values = np.arange(16, dtype="float64").reshape(4, 4)
    out, kept = td.reduce_to_display_grid(
        values, np.ones((4, 4), dtype=bool), 2, resampling="nearest", min_valid_fraction=0.5
    )
    assert kept.all()
    assert out[0, 0] == values[1, 1] and out[1, 1] == values[3, 3]


# ---------------------------------------------------------------------------
# T5 -- the source is re-inspected and every material fact compared
# ---------------------------------------------------------------------------


def test_t5_scale_offset_added_after_planning_is_refused(tmp_path: Path) -> None:
    source, plan = _tiny_plan(tmp_path)
    with rasterio.open(source, "r+") as dst:
        dst.scales = (2.0,)
        dst.offsets = (100.0,)
    with pytest.raises(td.GridError, match="no longer matches"):
        td.write_display_terrain(plan, source, tmp_path / "o.tif")
    assert not (tmp_path / "o.tif").exists()


def test_t5_changed_embedded_unit_is_refused_even_without_a_declaration(tmp_path: Path) -> None:
    source = _write_source(tmp_path / "u.tif", _sloped(30, 30), pixel=3.0, units="metre")
    info = td.inspect_source(source)
    assert info.vertical_unit_assurance == "embedded_only"
    plan = td.plan_display_terrain(info)
    with rasterio.open(source, "r+") as dst:
        dst.units = ("us_survey_foot",)
    with pytest.raises(td.GridError, match="vertical_unit"):
        td.write_display_terrain(plan, source, tmp_path / "o.tif")


def test_t5_a_declaration_that_now_contradicts_the_raster_is_refused(tmp_path: Path) -> None:
    source = _write_source(tmp_path / "d.tif", _sloped(30, 30), pixel=3.0, units="metre")
    info = td.inspect_source(source, vertical_unit="metre")
    assert info.vertical_unit_assurance == "declared_and_embedded_agree"
    plan = td.plan_display_terrain(info)
    with rasterio.open(source, "r+") as dst:
        dst.units = ("us_survey_foot",)
    with pytest.raises(td.GridError, match="contradiction"):
        td.write_display_terrain(plan, source, tmp_path / "o.tif")


def test_t5_datum_assurance_is_declared_unverified_or_unknown(tmp_path: Path) -> None:
    source = _write_source(tmp_path / "s.tif", _sloped(30, 30), pixel=3.0)
    assert td.inspect_source(source, vertical_unit="metre").vertical_datum_assurance == "unknown"
    declared = td.inspect_source(source, vertical_unit="metre", vertical_datum="a-datum")
    assert declared.vertical_datum_assurance == "declared_unverified"
    assert declared.embedded_vertical_crs is None  # nothing embedded to compare with


# ---------------------------------------------------------------------------
# Per-level quantization of the derivative itself
# ---------------------------------------------------------------------------


def test_every_overview_level_is_the_rounded_unrounded_mean_of_the_base(written) -> None:
    _, _, plan, out, _ = written
    nodata = np.float32(-9999.0)
    with rasterio.open(out) as dst:
        base = dst.read(1)
        factors = list(dst.overviews(1))
        assert factors == [2]
        with rasterio.open(out, overview_level=0) as level:
            got = level.read(1)
    valid = base != nodata
    h, w = base.shape
    block = base.astype("float64") * valid
    sums = block.reshape(h // 2, 2, w // 2, 2).sum(axis=(1, 3))
    counts = valid.reshape(h // 2, 2, w // 2, 2).sum(axis=(1, 3))
    kept = got != nodata
    expected = np.rint(sums[kept] / counts[kept] * 100) / 100
    assert kept.any()
    np.testing.assert_allclose(got[kept], expected.astype("float32"), rtol=0, atol=1e-6)
    # The unrounded aggregate is generally off the quantum, so the level really
    # was rounded after averaging.
    assert (expected != sums[kept] / counts[kept]).any()


def test_validation_flags_a_level_that_was_not_rounded(tmp_path: Path) -> None:
    from ras2cng.terrain_display import _render_display_raster

    source = _write_source(tmp_path / "again.tif", _sloped(1500, 1800), pixel=1.0)
    plan = td.plan_display_terrain(td.inspect_source(source, vertical_unit="metre", vertical_datum="declared"))
    base = tmp_path / "base.tif"
    _render_display_raster(plan, source, base)
    out = tmp_path / "unrounded_levels.tif"
    cog.area_matched_cog(
        base, out, compress="DEFLATE", predictor="YES", lerc_max_z_error=None,
        creation_options=dict(cog.TERRAIN_CREATION_OPTIONS),
    )
    verdict = td.validate_display_terrain(plan, out)
    assert not verdict.valid
    assert verdict.overview_levels_quantized is False
    assert any("overview level 2" in e and "quantum" in e for e in verdict.errors)
    assert any("overview_value_quantum" in e for e in verdict.errors)


def test_single_block_output_reports_that_level_checks_are_vacuous(tmp_path: Path) -> None:
    source, plan = _tiny_plan(tmp_path)
    out = tmp_path / "o.tif"
    result = td.write_display_terrain(plan, source, out)
    assert result.overview["factors"] == []
    verdict = td.validate_display_terrain(plan, out, expected=result)
    assert verdict.valid, verdict.errors
    assert verdict.overview_levels_checked == []
    assert verdict.overview_levels_quantized is False
    assert any("vacuous" in w for w in verdict.warnings)
