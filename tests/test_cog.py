"""Tests for the shared COG policy: overviews, CRS safety, validation, atomicity."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
import rasterio
from rasterio.shutil import copy as copy_raster
from rasterio.transform import from_origin

from ras2cng.cog import (
    area_matched_cog,
    numeric_predictor,
    assert_plausible_wgs84_bounds,
    atomic_output,
    cog_creation_options,
    describe_crs,
    overview_factors,
    resolve_crs_authority,
    validate_cog,
)

NODATA = -9999.0

# A Texas Centric Albers definition in US survey feet.  It has no exact EPSG
# match; the nearest registered code, EPSG:3083, is the *metre* variant.  Only
# the linear unit distinguishes them, which is exactly what a permissive
# confidence threshold is designed to ignore.
TEXAS_CENTRIC_ALBERS_FEET_WKT = (
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


def _flood_raster(path: Path, *, height: int = 600, width: int = 520, seed: int = 3) -> np.ndarray:
    """Write a depth raster with a ragged wet margin and return its wet mask."""

    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:height, 0:width]
    centre = height / 2 + 70 * np.sin(2 * np.pi * xx / 190)
    distance = np.abs(yy - centre)
    half_width = 60 + 25 * np.sin(2 * np.pi * xx / 130)
    wet = (distance + rng.normal(0, 16, size=(height, width))) < half_width
    depth = np.clip((half_width - distance) / half_width, 0, None) * 9.0 + 0.05
    values = np.where(wet, depth, NODATA).astype("float32")
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        width=width,
        height=height,
        count=1,
        dtype="float32",
        nodata=NODATA,
        crs="EPSG:3857",
        transform=from_origin(0.0, 0.0, 3.0, 3.0),
        tiled=True,
        blockxsize=128,
        blockysize=128,
    ) as destination:
        destination.write(values, 1)
    return wet


def _wet_area_ratios(path: Path, base_wet: int) -> list[float]:
    """Wet area of each overview level, scaled back to level-0 units."""

    import math

    ratios = []
    with rasterio.open(path) as source:
        for factor in source.overviews(1):
            data = source.read(
                1,
                out_shape=(
                    math.ceil(source.height / factor),
                    math.ceil(source.width / factor),
                ),
                masked=True,
            )
            wet_cells = int((~np.ma.getmaskarray(data)).sum())
            ratios.append(wet_cells * factor * factor / base_wet)
    return ratios


# ---------------------------------------------------------------------------
# Overview level policy
# ---------------------------------------------------------------------------


def test_overview_factors_continue_until_a_level_fits_one_block() -> None:
    factors = overview_factors(20000, 12000, blocksize=512)

    assert factors[0] == 2
    # The top level must fit inside a single block; stopping earlier leaves a
    # zoomed-out read pulling many blocks from a mid-level overview.
    assert 20000 / factors[-1] <= 512
    assert 20000 / factors[-2] > 512


def test_overview_factors_are_empty_when_the_raster_already_fits_a_block() -> None:
    assert overview_factors(400, 300, blocksize=512) == []


# ---------------------------------------------------------------------------
# M3 -- never publish a guessed authority code
# ---------------------------------------------------------------------------


def test_us_survey_foot_albers_is_not_matched_to_the_metre_epsg_code() -> None:
    from pyproj import CRS

    source = CRS.from_wkt(TEXAS_CENTRIC_ALBERS_FEET_WKT)

    # The trap, pinned: at the old threshold PROJ returns the metre variant of
    # this projection.  Stamping that code onto a foot-based raster displaces it
    # by roughly 25,000 km, and the linear unit is the only thing that differs.
    assert source.to_authority(min_confidence=25) == ("EPSG", "3083")
    assert source.to_authority(min_confidence=70) is None
    assert source.axis_info[0].unit_conversion_factor == pytest.approx(0.3048006096)
    assert CRS.from_epsg(3083).axis_info[0].unit_conversion_factor == pytest.approx(1.0)

    assert resolve_crs_authority(source) is None
    described = describe_crs(source)
    assert described["authority"] is None
    assert described["crs"] != "EPSG:3083"
    # The browser still gets a usable definition, which is what it actually needs.
    assert described["proj4"] and "+proj=aea" in described["proj4"]


def test_an_exact_registered_crs_still_resolves_to_its_code() -> None:
    from pyproj import CRS

    assert resolve_crs_authority(CRS.from_epsg(26916)) == ("EPSG", "26916")
    assert describe_crs(CRS.from_epsg(26916))["crs"] == "EPSG:26916"


def test_a_us_survey_foot_state_plane_resolves_to_its_own_foot_code() -> None:
    from pyproj import CRS

    # EPSG:2965 is Indiana East (ftUS).  A correct unit must not be rejected.
    assert resolve_crs_authority(CRS.from_epsg(2965)) == ("EPSG", "2965")


def test_implausible_wgs84_bounds_are_rejected() -> None:
    assert_plausible_wgs84_bounds([-86.5, 40.1, -86.4, 40.2])

    with pytest.raises(ValueError, match="longitude is out of range"):
        assert_plausible_wgs84_bounds([-4_000_000.0, 40.0, -3_999_000.0, 41.0])
    with pytest.raises(ValueError, match="degenerate"):
        assert_plausible_wgs84_bounds([-86.5, 40.1, -86.5, 40.1])


# ---------------------------------------------------------------------------
# M2 -- OVERVIEWS is always stated explicitly
# ---------------------------------------------------------------------------


def test_overviews_auto_silently_reuses_a_source_pyramid(tmp_path: Path) -> None:
    """The reuse trap, pinned: AUTO discards the requested resampling entirely.

    HEC-RAS terrain TIFFs routinely ship internal overviews or .ovr sidecars, so
    a source pyramid is the normal case rather than an edge case -- which makes
    every other overview fix a silent no-op until OVERVIEWS is stated.
    """

    from rasterio.enums import Resampling

    source = tmp_path / "depth.tif"
    wet = _flood_raster(source)
    base_wet = int(wet.sum())
    with rasterio.open(source, "r+") as handle:
        handle.build_overviews([2, 4], Resampling.average)

    shared = dict(
        driver="COG",
        compress="DEFLATE",
        predictor=2,
        blocksize=128,
        overview_resampling="nearest",
        BIGTIFF="IF_SAFER",
    )
    auto = tmp_path / "auto.tif"
    copy_raster(str(source), str(auto), **shared)  # OVERVIEWS left at its AUTO default
    explicit = tmp_path / "explicit.tif"
    copy_raster(str(source), str(explicit), OVERVIEWS="IGNORE_EXISTING", **shared)

    auto_ratios = _wet_area_ratios(auto, base_wet)
    explicit_ratios = _wet_area_ratios(explicit, base_wet)

    # Both asked for `nearest`.  Only one of them got it.
    assert auto_ratios[0] > 1.05, "AUTO inherited the source's average pyramid"
    assert explicit_ratios[0] == pytest.approx(1.0, abs=0.02)


def test_area_matched_cog_ignores_a_pre_existing_source_pyramid(tmp_path: Path) -> None:
    from rasterio.enums import Resampling

    source = tmp_path / "depth.tif"
    wet = _flood_raster(source)
    with rasterio.open(source, "r+") as handle:
        handle.build_overviews([2, 4], Resampling.average)

    output = tmp_path / "depth_cog.tif"
    area_matched_cog(source, output, compress="DEFLATE", predictor=2, blocksize=128)

    for ratio in _wet_area_ratios(output, int(wet.sum())):
        assert ratio == pytest.approx(1.0, abs=0.01)


def test_creation_options_always_state_overviews() -> None:
    options = cog_creation_options()

    # AUTO (the driver default) silently reuses a source pyramid and discards
    # every resampling setting, so the option must never be left unset.
    assert "OVERVIEWS=IGNORE_EXISTING" in options
    assert "OVERVIEWS=FORCE_USE_EXISTING" in cog_creation_options(reuse_overviews=True)
    assert "OVERVIEWS=AUTO" not in options


def _defaults_overridden() -> bool:
    import os

    return bool(
        os.environ.get("RAS2CNG_COG_COMPRESS")
        or os.environ.get("RAS2CNG_COG_PREDICTOR")
        or os.environ.get("RAS2CNG_LERC_MAX_Z_ERROR")
    )


def test_float_rasters_default_to_lerc_for_delivery(tmp_path: Path) -> None:
    """LERC at 0.01 raster units is the shipping default for float surfaces.

    These artifacts exist to be served. At 0.01 ft the error is one to two
    orders of magnitude below the accuracy of the terrain and boundary
    conditions the values were computed from, and it returns 73-95% of the file
    size. It is nonetheless a lossy default, which is why the tolerance is
    recorded on the artifact and why one env var turns it off.
    """

    if _defaults_overridden():
        pytest.skip("compression defaults are overridden in this environment")

    source = tmp_path / "depth.tif"
    _flood_raster(source)
    output = tmp_path / "depth_cog.tif"

    area_matched_cog(source, output, blocksize=128)

    with rasterio.open(output) as handle:
        structure = handle.tags(ns="IMAGE_STRUCTURE")
        tags = handle.tags()
    assert structure["COMPRESSION"] == "LERC_DEFLATE"
    assert structure["MAX_Z_ERROR"] == "0.01"
    assert structure["LAYOUT"] == "COG"
    assert tags["compression_lossy"] == "true"
    assert "COMPRESS=LERC_DEFLATE" in cog_creation_options()
    assert "MAX_Z_ERROR=0.01" in cog_creation_options()


def test_lossless_is_one_argument_away(tmp_path: Path) -> None:
    """The scientific path: lossless, and back to the benchmarked predictor 3."""

    if _defaults_overridden():
        pytest.skip("compression defaults are overridden in this environment")

    source = tmp_path / "depth.tif"
    _flood_raster(source)
    output = tmp_path / "depth_cog.tif"

    report = area_matched_cog(source, output, blocksize=128, lerc_max_z_error=None)

    with rasterio.open(output) as handle:
        structure = handle.tags(ns="IMAGE_STRUCTURE")
        assert "compression_lossy" not in handle.tags()
    assert structure["COMPRESSION"] == "DEFLATE"
    # Predictor 2 was 16-25% larger than 3 on every surface benchmarked,
    # including the smooth WSE case where it is reported to win.
    assert structure["PREDICTOR"] == "3"
    assert report.lossy_max_z_error is None
    assert "COMPRESS=DEFLATE" in cog_creation_options(lerc_max_z_error=None)


def test_numeric_predictor_is_float_only_for_three() -> None:
    """The GTiff driver takes only the numeric form, and 3 is float-only."""

    assert numeric_predictor("float32") == 3
    assert numeric_predictor("float64") == 3
    assert numeric_predictor("int32") == 2
    assert numeric_predictor("uint8") == 2
    # A class raster gets 2 regardless of storage type.
    assert numeric_predictor("float32", categorical=True) == 2


def test_class_data_keeps_predictor_2_even_when_stored_as_float(tmp_path: Path) -> None:
    """Predictor 3 is preferred for continuous surfaces, and only for those.

    On a float32 *class* raster predictor 3 measured 0.475 MB against 0.195 MB
    for predictor 2 -- 2.4x larger. Class values are a handful of repeated
    magnitudes, which horizontal differencing collapses and the floating-point
    predictor's byte shuffle does not. Predictor 3 is legal here, just wrong.
    """

    source = tmp_path / "hazard.tif"
    _flood_raster(source)
    with rasterio.open(source) as handle:
        depth = handle.read(1, masked=True)
    classes = np.where(
        ~np.ma.getmaskarray(depth), np.digitize(depth.filled(0), [2.0, 4.0]) * 2 + 2, NODATA
    ).astype("float32")
    with rasterio.open(source, "r+") as handle:
        handle.write(classes, 1)

    output = tmp_path / "hazard_cog.tif"
    area_matched_cog(source, output, categorical=True, blocksize=128, scratch_dir=tmp_path)

    with rasterio.open(output) as handle:
        assert handle.dtypes[0] == "float32"
        assert handle.tags(ns="IMAGE_STRUCTURE")["PREDICTOR"] == "2"


def test_an_integer_raster_falls_back_to_a_legal_predictor(tmp_path: Path) -> None:
    """Predictor 3 is float-only, and GDAL *hard-fails* rather than degrading.

    Writing the literal 3 as the continuous default would abort the conversion
    of any integer-dtype raster that reached the continuous path.  ``YES`` still
    yields 3 for every float raster, which is the setting that was benchmarked.
    """

    source = tmp_path / "counts.tif"
    values = np.where(np.mgrid[0:700, 0:700][0] < 350, 5, 255).astype("uint8")
    with rasterio.open(
        source,
        "w",
        driver="GTiff",
        width=700,
        height=700,
        count=1,
        dtype="uint8",
        nodata=255,
        crs="EPSG:3857",
        transform=from_origin(0.0, 0.0, 3.0, 3.0),
        tiled=True,
        blockxsize=128,
        blockysize=128,
    ) as destination:
        destination.write(values, 1)

    # Continuous path on purpose: categorical=False is what would have crashed.
    area_matched_cog(source, tmp_path / "counts_cog.tif", blocksize=128, scratch_dir=tmp_path)

    with rasterio.open(tmp_path / "counts_cog.tif") as handle:
        assert handle.tags(ns="IMAGE_STRUCTURE")["PREDICTOR"] == "2"
        assert handle.dtypes[0] == "uint8"


# ---------------------------------------------------------------------------
# M6 -- atomic replacement
# ---------------------------------------------------------------------------


def test_atomic_output_replaces_only_on_success(tmp_path: Path) -> None:
    target = tmp_path / "tiles.pmtiles"
    target.write_bytes(b"previous-good-artifact")

    with atomic_output(target) as staged:
        staged.write_bytes(b"replacement")
    assert target.read_bytes() == b"replacement"


def test_atomic_output_keeps_the_previous_artifact_when_the_write_fails(tmp_path: Path) -> None:
    target = tmp_path / "tiles.pmtiles"
    target.write_bytes(b"previous-good-artifact")

    with pytest.raises(RuntimeError):
        with atomic_output(target) as staged:
            staged.write_bytes(b"half-written")
            raise RuntimeError("converter died")

    # The release keeps a working tileset, and no partial is left behind.
    assert target.read_bytes() == b"previous-good-artifact"
    assert list(tmp_path.glob(".*partial*")) == []


def test_atomic_output_fails_loudly_when_nothing_was_written(tmp_path: Path) -> None:
    target = tmp_path / "tiles.pmtiles"
    target.write_bytes(b"previous-good-artifact")

    with pytest.raises(FileNotFoundError):
        with atomic_output(target):
            pass
    assert target.read_bytes() == b"previous-good-artifact"


# ---------------------------------------------------------------------------
# M1 -- area-matched overviews
# ---------------------------------------------------------------------------


def test_average_overviews_inflate_wet_area(tmp_path: Path) -> None:
    """The failure this module exists to fix, pinned as a test."""

    source = tmp_path / "depth.tif"
    wet = _flood_raster(source)
    inflated = tmp_path / "average.tif"
    copy_raster(
        str(source),
        str(inflated),
        driver="COG",
        compress="DEFLATE",
        predictor=2,
        blocksize=128,
        overview_resampling="average",
        OVERVIEWS="IGNORE_EXISTING",
        BIGTIFF="IF_SAFER",
    )

    ratios = _wet_area_ratios(inflated, int(wet.sum()))
    assert ratios, "the fixture must be large enough to build overviews"
    # `average` marks a coarse cell valid when any sub-cell is valid, so the
    # published inundation extent grows monotonically with zoom.
    assert ratios[0] > 1.05
    assert ratios[-1] > ratios[0]


def test_area_matched_overviews_preserve_wet_area(tmp_path: Path) -> None:
    source = tmp_path / "depth.tif"
    wet = _flood_raster(source)
    output = tmp_path / "depth_cog.tif"

    report = area_matched_cog(source, output, compress="DEFLATE", predictor=2, blocksize=128)

    assert report.method == "area_matched_mean"
    assert report.fallback_reason is None
    for ratio in _wet_area_ratios(output, int(wet.sum())):
        assert ratio == pytest.approx(1.0, abs=0.01)


def test_area_matched_overviews_carry_the_mean_of_wet_contributors(tmp_path: Path) -> None:
    source = tmp_path / "depth.tif"
    wet = _flood_raster(source)
    with rasterio.open(source) as handle:
        base = handle.read(1, masked=True)
    base_max = float(base.max())

    output = tmp_path / "depth_cog.tif"
    area_matched_cog(source, output, compress="DEFLATE", predictor=2, blocksize=128)

    with rasterio.open(output) as handle:
        factor = handle.overviews(1)[0]
        import math

        coarse = handle.read(
            1,
            out_shape=(math.ceil(handle.height / factor), math.ceil(handle.width / factor)),
            masked=True,
        )
    # MEAN, not MAX: a coarse cell inheriting its deepest sub-cell would inflate
    # flood volume, which is the wrong conserved quantity for depth.
    assert float(coarse.max()) < base_max
    assert float(coarse.mean()) == pytest.approx(float(base.mean()), rel=0.15)
    assert int(wet.sum()) > 0


def test_area_matched_overviews_are_deterministic(tmp_path: Path) -> None:
    source = tmp_path / "depth.tif"
    _flood_raster(source)
    first = tmp_path / "a.tif"
    second = tmp_path / "b.tif"

    area_matched_cog(source, first, compress="DEFLATE", predictor=2, blocksize=128)
    area_matched_cog(source, second, compress="DEFLATE", predictor=2, blocksize=128)

    import math

    with rasterio.open(first) as a, rasterio.open(second) as b:
        for factor in a.overviews(1):
            shape = (math.ceil(a.height / factor), math.ceil(a.width / factor))
            left = a.read(1, out_shape=shape, masked=True)
            right = b.read(1, out_shape=shape, masked=True)
            assert np.array_equal(np.ma.getmaskarray(left), np.ma.getmaskarray(right))
            assert np.array_equal(left.filled(0), right.filled(0))


def test_lossless_output_reproduces_the_base_level_exactly(tmp_path: Path) -> None:
    source = tmp_path / "depth.tif"
    _flood_raster(source)
    output = tmp_path / "depth_cog.tif"

    area_matched_cog(
        source, output, compress="DEFLATE", predictor=2, blocksize=128, lerc_max_z_error=None
    )

    with rasterio.open(source) as original, rasterio.open(output) as converted:
        assert np.array_equal(original.read(1), converted.read(1))
        assert converted.nodata == original.nodata
        assert converted.crs == original.crs


def test_default_output_reproduces_the_base_level_within_tolerance(tmp_path: Path) -> None:
    """Under the lossy default the base level is bounded, not identical.

    Worth stating explicitly: code that relied on byte-equality with the source
    must ask for lossless.
    """

    if _defaults_overridden():
        pytest.skip("compression defaults are overridden in this environment")

    source = tmp_path / "depth.tif"
    _flood_raster(source)
    output = tmp_path / "depth_cog.tif"

    area_matched_cog(source, output, blocksize=128)

    with rasterio.open(source) as original, rasterio.open(output) as converted:
        before = original.read(1, masked=True)
        after = converted.read(1, masked=True)
        valid = ~np.ma.getmaskarray(before)
        assert not np.array_equal(before.data, after.data), "default is lossy"
        assert np.abs(after.data[valid] - before.data[valid]).max() <= 0.0101
        # The wet/dry edge is a hard boundary and must survive the codec intact.
        assert np.array_equal(np.ma.getmaskarray(before), np.ma.getmaskarray(after))
        assert converted.nodata == original.nodata
        assert converted.crs == original.crs


def test_area_matched_overviews_handle_odd_dimensions(tmp_path: Path) -> None:
    source = tmp_path / "odd.tif"
    wet = _flood_raster(source, height=577, width=463, seed=11)
    output = tmp_path / "odd_cog.tif"

    area_matched_cog(source, output, compress="DEFLATE", predictor=2, blocksize=128)

    for ratio in _wet_area_ratios(output, int(wet.sum())):
        assert ratio == pytest.approx(1.0, abs=0.02)


# ---------------------------------------------------------------------------
# M4 -- categorical rasters decimate by mode
# ---------------------------------------------------------------------------


def test_categorical_overviews_never_invent_a_class(tmp_path: Path) -> None:
    source = tmp_path / "hazard.tif"
    wet = _flood_raster(source)
    with rasterio.open(source) as handle:
        depth = handle.read(1, masked=True)
    classes = np.where(
        ~np.ma.getmaskarray(depth), np.digitize(depth.filled(0), [2.0, 4.0, 6.0]) * 2 + 2, NODATA
    ).astype("float32")
    with rasterio.open(source, "r+") as handle:
        handle.write(classes, 1)

    output = tmp_path / "hazard_cog.tif"
    report = area_matched_cog(
        source, output, categorical=True, compress="DEFLATE", predictor=2, blocksize=128
    )

    assert report.method == "area_matched_mode"
    allowed = set(np.unique(classes[classes != NODATA]).tolist())
    import math

    with rasterio.open(output) as handle:
        for factor in handle.overviews(1):
            data = handle.read(
                1,
                out_shape=(math.ceil(handle.height / factor), math.ceil(handle.width / factor)),
                masked=True,
            )
            # Averaging 2 and 4 yields 3, a class that was never in the source.
            assert set(np.unique(data.compressed()).tolist()) <= allowed
    for ratio in _wet_area_ratios(output, int(wet.sum())):
        assert ratio == pytest.approx(1.0, abs=0.02)


def test_a_raster_without_nodata_falls_back_and_says_so(tmp_path: Path) -> None:
    source = tmp_path / "nomask.tif"
    with rasterio.open(
        source,
        "w",
        driver="GTiff",
        width=700,
        height=700,
        count=1,
        dtype="float32",
        crs="EPSG:3857",
        transform=from_origin(0.0, 0.0, 3.0, 3.0),
        tiled=True,
        blockxsize=128,
        blockysize=128,
    ) as destination:
        destination.write(np.ones((700, 700), dtype="float32"), 1)

    output = tmp_path / "nomask_cog.tif"
    report = area_matched_cog(source, output, compress="DEFLATE", predictor=2, blocksize=128)

    # A fallback that is not labelled is indistinguishable from the fix working.
    assert report.fallback_reason is not None
    assert report.method == "average"
    with rasterio.open(output) as handle:
        assert handle.tags()["overview_fallback_reason"] == report.fallback_reason


def test_area_matched_cog_can_be_required(tmp_path: Path) -> None:
    source = tmp_path / "nomask.tif"
    with rasterio.open(
        source,
        "w",
        driver="GTiff",
        width=700,
        height=700,
        count=1,
        dtype="float32",
        crs="EPSG:3857",
        transform=from_origin(0.0, 0.0, 3.0, 3.0),
        tiled=True,
        blockxsize=128,
        blockysize=128,
    ) as destination:
        destination.write(np.ones((700, 700), dtype="float32"), 1)

    with pytest.raises(ValueError, match="nodata"):
        area_matched_cog(source, tmp_path / "out.tif", require_area_matched=True)


# ---------------------------------------------------------------------------
# I5 -- LERC is opt-in, bounded, and self-documenting
# ---------------------------------------------------------------------------


def test_lerc_records_the_tolerance_it_cost(tmp_path: Path) -> None:
    """LERC is lossy, so the artifact must carry the tolerance it was built at."""

    source = tmp_path / "wse.tif"
    _flood_raster(source)
    lossless = tmp_path / "lossless.tif"
    lossy = tmp_path / "lossy.tif"

    area_matched_cog(source, lossless, blocksize=128, lerc_max_z_error=None)
    report = area_matched_cog(source, lossy, blocksize=128, lerc_max_z_error=0.01)

    assert report.compression == "LERC_DEFLATE"
    assert report.lossy_max_z_error == 0.01
    with rasterio.open(lossy) as handle:
        structure = handle.tags(ns="IMAGE_STRUCTURE")
        tags = handle.tags()
        got = handle.read(1, masked=True)
    assert structure["COMPRESSION"] == "LERC_DEFLATE"
    assert structure["MAX_Z_ERROR"] == "0.01"
    # The loss must travel with the file, not just the build log.
    assert tags["compression_lossy"] == "true"
    assert tags["compression_max_z_error"] == "0.01"

    with rasterio.open(source) as handle:
        original = handle.read(1, masked=True)
    valid = ~np.ma.getmaskarray(original)
    # The bound is honoured; the small slack is float32 representation.
    assert np.abs(got.data[valid] - original.data[valid]).max() <= 0.0101
    # The wet mask is a hard edge and must survive a lossy codec untouched.
    assert np.array_equal(np.ma.getmaskarray(got), np.ma.getmaskarray(original))
    assert lossy.stat().st_size < lossless.stat().st_size


def test_lossless_output_carries_no_lossy_tags(tmp_path: Path) -> None:
    source = tmp_path / "depth.tif"
    _flood_raster(source)
    output = tmp_path / "depth_cog.tif"

    report = area_matched_cog(source, output, blocksize=128, lerc_max_z_error=None)

    assert report.lossy_max_z_error is None
    with rasterio.open(output) as handle:
        assert "compression_lossy" not in handle.tags()


def test_the_default_steps_aside_but_an_explicit_request_is_refused(tmp_path: Path) -> None:
    """A default arriving on its own is not the same as a caller asking for it.

    On class or integer data the default simply does not apply. An explicit
    request for a lossy class raster is a mistake and says so.
    """

    from ras2cng.cog import resolve_compression

    # Default arriving on its own -> quietly lossless.
    assert resolve_compression("DEFLATE", lerc_max_z_error=0.01, categorical=True,
                               explicit=False) == ("DEFLATE", None)
    assert resolve_compression("DEFLATE", lerc_max_z_error=0.01, floating_point=False,
                               explicit=False) == ("DEFLATE", None)
    # Explicitly asked for -> refused.
    with pytest.raises(ValueError, match="categorical"):
        resolve_compression("DEFLATE", lerc_max_z_error=0.01, categorical=True, explicit=True)
    with pytest.raises(ValueError, match="integer"):
        resolve_compression("DEFLATE", lerc_max_z_error=0.01, floating_point=False, explicit=True)


def test_the_lerc_default_can_be_switched_off_by_environment() -> None:
    """The scientific escape hatch has to be one variable, and unambiguous."""

    from ras2cng.cog import _parse_lerc_default

    for text in ("off", "none", "0", "", "no", "false", "lossless"):
        assert _parse_lerc_default(text) is None, text
    assert _parse_lerc_default("0.01") == 0.01
    assert _parse_lerc_default("0.5") == 0.5
    # A typo must fail loudly rather than silently publishing lossy rasters.
    for bad in ("yes", "true", "-1", "nan", "0.01ft"):
        with pytest.raises(ValueError, match="RAS2CNG_LERC_MAX_Z_ERROR"):
            _parse_lerc_default(bad)


def test_lerc_is_refused_where_a_tolerance_is_meaningless(tmp_path: Path) -> None:
    source = tmp_path / "depth.tif"
    _flood_raster(source)

    # A class value that is "within tolerance" is simply a different class.
    with pytest.raises(ValueError, match="categorical"):
        area_matched_cog(
            source, tmp_path / "a.tif", categorical=True, lerc_max_z_error=0.01, blocksize=128
        )
    for tolerance in (0.0, -1.0):
        with pytest.raises(ValueError, match="positive tolerance"):
            area_matched_cog(
                source, tmp_path / "b.tif", lerc_max_z_error=tolerance, blocksize=128
            )


def test_lerc_reaches_the_gdal_translate_option_form_too() -> None:
    options = cog_creation_options(lerc_max_z_error=0.01)

    assert "COMPRESS=LERC_DEFLATE" in options
    assert "MAX_Z_ERROR=0.01" in options
    # LERC does its own encoding; GDAL rejects a byte predictor on top of it
    # rather than ignoring it, so the option must not be emitted.
    assert not any(o.startswith("PREDICTOR=") for o in options)

    lossless = cog_creation_options(lerc_max_z_error=None)
    assert "COMPRESS=DEFLATE" in lossless
    assert "PREDICTOR=YES" in lossless
    assert not any(o.startswith("MAX_Z_ERROR=") for o in lossless)

    # Integer rasters stay lossless even under the default.
    integral = cog_creation_options(floating_point=False)
    assert "COMPRESS=DEFLATE" in integral
    assert not any(o.startswith("MAX_Z_ERROR=") for o in integral)


# ---------------------------------------------------------------------------
# I8 -- terrain keeps its footprint
# ---------------------------------------------------------------------------


def test_terrain_footprint_does_not_creep_outward_with_zoom(tmp_path: Path) -> None:
    """Terrain is continuous but still carries a nodata footprint.

    `average` marks a coarse cell valid when any sub-cell is, so the terrain
    edge grows one coarse cell per level and the published terrain covers more
    ground than the model does.
    """

    from ras2cng.cog import TERRAIN_CREATION_OPTIONS

    height = width = 700
    yy, xx = np.mgrid[0:height, 0:width]
    footprint = ((xx - width / 2) ** 2 + (yy - height / 2) ** 2) < (0.42 * width) ** 2
    values = np.where(footprint, 118.0 - (xx / width) * 6.0, NODATA).astype("float32")
    source = tmp_path / "terrain.tif"
    with rasterio.open(
        source,
        "w",
        driver="GTiff",
        width=width,
        height=height,
        count=1,
        dtype="float32",
        nodata=NODATA,
        crs="EPSG:3857",
        transform=from_origin(0.0, 0.0, 3.0, 3.0),
        tiled=True,
        blockxsize=128,
        blockysize=128,
    ) as destination:
        destination.write(values, 1)
    base_valid = int(footprint.sum())

    output = tmp_path / "terrain_cog.tif"
    # The exact call shape the archive uses, so the creation-option passthrough
    # (SPARSE_OK, LEVEL) and PREDICTOR=YES are covered.
    report = area_matched_cog(
        source,
        output,
        predictor="YES",
        blocksize=128,
        scratch_dir=tmp_path,
        creation_options=TERRAIN_CREATION_OPTIONS,
    )

    assert report.method == "area_matched_mean"
    for ratio in _wet_area_ratios(output, base_valid):
        assert ratio == pytest.approx(1.0, abs=0.01)
    with rasterio.open(source) as original, rasterio.open(output) as converted:
        before, after = original.read(1), converted.read(1)
        keep = before != NODATA
        assert np.abs(after[keep] - before[keep]).max() <= 0.0101
        assert converted.tags(ns="IMAGE_STRUCTURE")["LAYOUT"] == "COG"


# ---------------------------------------------------------------------------
# M5 -- validation gate
# ---------------------------------------------------------------------------


def test_validate_cog_accepts_a_real_cog(tmp_path: Path) -> None:
    source = tmp_path / "depth.tif"
    _flood_raster(source)
    output = tmp_path / "depth_cog.tif"
    area_matched_cog(source, output, compress="DEFLATE", predictor=2, blocksize=128)

    result = validate_cog(output)

    assert result.valid, result.errors
    assert result.layout_checked, "rio-cogeo must be installed for the layout gate to mean anything"


def test_validate_cog_rejects_a_plain_untiled_geotiff(tmp_path: Path) -> None:
    plain = tmp_path / "plain.tif"
    with rasterio.open(
        plain,
        "w",
        driver="GTiff",
        width=2000,
        height=2000,
        count=1,
        dtype="float32",
        nodata=NODATA,
        crs="EPSG:3857",
        transform=from_origin(0.0, 0.0, 3.0, 3.0),
    ) as destination:
        destination.write(np.ones((2000, 2000), dtype="float32"), 1)

    result = validate_cog(plain)

    # A striped GeoTIFF serves, but at one HTTP range request per tile.
    assert not result.valid
    assert result.errors


def test_validate_cog_rejects_a_raster_with_no_mask(tmp_path: Path) -> None:
    nomask = tmp_path / "nomask.tif"
    with rasterio.open(
        nomask,
        "w",
        driver="GTiff",
        width=256,
        height=256,
        count=1,
        dtype="float32",
        crs="EPSG:3857",
        transform=from_origin(0.0, 0.0, 3.0, 3.0),
        tiled=True,
        blockxsize=128,
        blockysize=128,
    ) as destination:
        destination.write(np.ones((256, 256), dtype="float32"), 1)

    result = validate_cog(nomask)

    assert not result.valid
    assert any("nodata" in message or "mask" in message for message in result.errors)
