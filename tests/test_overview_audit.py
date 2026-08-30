"""Tests for the post-hoc overview extent audit.

Synthetic rasters, not HEC-RAS fixtures: the unit under test is GDAL overview
behaviour on a nodata-masked grid, which is reproducible in a few hundred
pixels and has nothing to do with HEC-RAS file formats. The "no mocks for
HEC-RAS data" rule in CONTRIBUTING.md is about HDF/geometry parsing, and
tests/test_cog.py already builds its rasters the same way.

Appended to tests/test_cog.py, or kept as tests/test_overview_audit.py.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
import rasterio
from rasterio.enums import Resampling
from rasterio.transform import from_origin

from ras2cng.cog import (
    audit_overview_extent,
    format_overview_audit,
)

NODATA = -9999.0
THRESHOLD = 0.5


def _write_depth_raster(
    path: Path,
    data: np.ndarray,
    *,
    overview_resampling: Resampling | None,
    factors: tuple[int, ...] = (2, 4, 8, 16),
) -> Path:
    """Write a masked depth grid, optionally with overviews built a given way."""
    height, width = data.shape
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=height,
        width=width,
        count=1,
        dtype="float32",
        crs="EPSG:5070",
        transform=from_origin(0.0, float(height), 1.0, 1.0),
        nodata=NODATA,
        tiled=True,
        blockxsize=128,
        blockysize=128,
    ) as dst:
        dst.write(data.astype("float32"), 1)
        if overview_resampling is not None:
            dst.build_overviews(list(factors), overview_resampling)
    return path


def _thin_channels(size: int = 512, spacing: int = 64) -> np.ndarray:
    """A masked grid whose wet cells are thin lines: dry is nodata."""
    grid = np.full((size, size), NODATA, dtype="float32")
    grid[::spacing, :] = 1.5   # horizontal channels, one pixel wide
    grid[:, ::spacing] = 1.5   # vertical channels
    return grid


def test_average_overviews_on_masked_grid_are_flagged_broken(tmp_path):
    """The failure this audit exists to catch.

    GDAL's AVERAGE skips nodata children, so a coarse cell covering one wet
    child is fully wet rather than 25% wet. Every level then doubles the linear
    width of every wet feature.
    """
    path = _write_depth_raster(
        tmp_path / "average.tif",
        _thin_channels(),
        overview_resampling=Resampling.average,
    )

    audit = audit_overview_extent(path, threshold=THRESHOLD)

    assert audit.verdict == "broken", format_overview_audit(audit)
    assert audit.max_area_ratio > 3.0
    assert not audit.ok
    # Inflation compounds with depth rather than appearing at one level.
    ratios = [lv.area_ratio for lv in audit.levels]
    assert ratios == sorted(ratios), ratios


def test_unmasked_grid_holds_extent(tmp_path):
    """With no nodata to skip, AVERAGE has nothing to inflate."""
    grid = np.full((512, 512), 2.0, dtype="float32")
    path = _write_depth_raster(
        tmp_path / "solid.tif", grid, overview_resampling=Resampling.average
    )

    audit = audit_overview_extent(path, threshold=THRESHOLD)

    assert audit.verdict == "stable", format_overview_audit(audit)
    assert audit.ok
    assert audit.max_area_ratio == pytest.approx(1.0, abs=0.05)


def test_raster_without_overviews_reports_only_level_one(tmp_path):
    path = _write_depth_raster(
        tmp_path / "flat.tif", _thin_channels(), overview_resampling=None
    )

    audit = audit_overview_extent(path, threshold=THRESHOLD)

    assert [lv.level for lv in audit.levels] == [1]
    assert audit.verdict == "stable"
    assert audit.levels[0].area_ratio == 1.0


def test_fully_dry_raster_reports_empty_not_a_false_pass(tmp_path):
    """No wet pixels means no ratio exists; say so rather than implying health."""
    grid = np.full((256, 256), NODATA, dtype="float32")
    path = _write_depth_raster(
        tmp_path / "dry.tif", grid, overview_resampling=Resampling.average
    )

    audit = audit_overview_extent(path, threshold=THRESHOLD)

    assert audit.verdict == "empty"
    assert audit.levels[0].wet_pixels == 0
    assert len(audit.levels) == 1  # no comparison is meaningful


def test_threshold_is_in_raster_units(tmp_path):
    """A threshold above every stored value finds nothing; below, everything."""
    grid = np.full((128, 128), 0.25, dtype="float32")
    path = _write_depth_raster(
        tmp_path / "shallow.tif", grid, overview_resampling=None
    )

    assert audit_overview_extent(path, threshold=0.5).verdict == "empty"
    deep = audit_overview_extent(path, threshold=0.1)
    assert deep.levels[0].wet_pixels == 128 * 128


def test_nan_is_not_counted_as_wet(tmp_path):
    """NaN compares False against a threshold, but only if it is excluded."""
    grid = np.full((128, 128), np.nan, dtype="float32")
    grid[:8, :] = 2.0
    path = _write_depth_raster(tmp_path / "nan.tif", grid, overview_resampling=None)

    audit = audit_overview_extent(path, threshold=THRESHOLD)

    assert audit.levels[0].wet_pixels == 8 * 128


def test_band_out_of_range_raises(tmp_path):
    path = _write_depth_raster(
        tmp_path / "single.tif", _thin_channels(64, 16), overview_resampling=None
    )

    with pytest.raises(ValueError, match="band 2 not present"):
        audit_overview_extent(path, threshold=THRESHOLD, band=2)


def test_audit_tags_match_the_overview_report_shape(tmp_path):
    """as_tags() must be mergeable with OverviewReport.as_tags() output."""
    path = _write_depth_raster(
        tmp_path / "tags.tif",
        _thin_channels(),
        overview_resampling=Resampling.average,
    )

    tags = audit_overview_extent(path, threshold=THRESHOLD).as_tags()

    assert set(tags) == {
        "overview_audit_verdict",
        "overview_audit_threshold",
        "overview_audit_area_ratios",
    }
    assert all(isinstance(v, str) for v in tags.values())
    assert tags["overview_audit_verdict"] == "broken"


def test_format_overview_audit_renders_every_level_and_a_verdict(tmp_path):
    path = _write_depth_raster(
        tmp_path / "fmt.tif",
        _thin_channels(),
        overview_resampling=Resampling.average,
    )
    audit = audit_overview_extent(path, threshold=THRESHOLD)

    rendered = format_overview_audit(audit)

    assert "VERDICT: BROKEN" in rendered
    assert rendered.count("\n") >= len(audit.levels)
    for level in audit.levels:
        assert str(level.level) in rendered


def test_streaming_matches_a_whole_read(tmp_path):
    """Row-strip counting must equal counting the level in one read.

    Guards the windowed read: the strip loop is what makes a 35k x 30k grid
    auditable, and an off-by-one in the window would silently miscount.
    """
    grid = _thin_channels(600, 37)  # size not a multiple of the strip or spacing
    path = _write_depth_raster(tmp_path / "strip.tif", grid, overview_resampling=None)

    audit = audit_overview_extent(path, threshold=THRESHOLD)

    with rasterio.open(path) as src:
        whole = src.read(1)
    expected = int(((whole >= THRESHOLD) & (whole != NODATA)).sum())
    assert audit.levels[0].wet_pixels == expected
