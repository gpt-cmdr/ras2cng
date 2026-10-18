"""Display terrain primitives: native-aligned grid, explicit rounding, rounded overviews."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

from ras2cng.cog import area_matched_cog
from ras2cng.terrain_display import (
    DEFAULT_DISPLAY_ENCODINGS,
    DISPLAY_ENCODINGS,
    NODATA,
    aggregate_block,
    build_rounded_reference,
    measure_against_reference,
    plan_display_grid,
    round_to_step,
    write_display_cog,
)

FTUS = 1200 / 3937
ONE_METRE_FTUS = 3.280833333333333


def write_terrain(
    path: Path, values: np.ndarray, *, cell: float = ONE_METRE_FTUS
) -> None:
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        dtype="float32",
        width=values.shape[1],
        height=values.shape[0],
        count=1,
        crs="EPSG:2278",
        transform=from_origin(1748195.900941442, 13952577.090534868, cell, cell),
        nodata=NODATA,
    ) as out:
        out.write(values.astype("float32"), 1)
        out.update_tags(1, UNITS="Feet")


@pytest.fixture
def terrain(tmp_path: Path) -> tuple[Path, np.ndarray]:
    rng = np.random.default_rng(11)
    rows, cols = 700, 601  # neither divisible by 3; display 234 x 201 needs overviews
    y, x = np.mgrid[0:rows, 0:cols]
    values = (
        2200
        + 0.02 * x
        + 0.01 * y
        + np.cumsum(rng.normal(0, 0.05, (rows, cols)), axis=1) * 0.2
    )
    values[:30, :45] = NODATA
    values[350, 300] = NODATA
    path = tmp_path / "native.tif"
    write_terrain(path, values)
    return path, values


def test_metre_grid_in_us_feet_gives_factor_three_and_keeps_the_origin() -> None:
    transform = from_origin(
        1748195.900941442, 13952577.090534868, ONE_METRE_FTUS, ONE_METRE_FTUS
    )
    grid = plan_display_grid(transform, 9984, 7936, FTUS, 3.0)
    assert grid.factor == 3
    assert grid.cell_m == pytest.approx(3.0, abs=1e-9)
    assert (grid.origin_x, grid.origin_y) == (transform.c, transform.f)
    assert (grid.width, grid.height) == (3328, 2646)
    one_foot = plan_display_grid(from_origin(0, 0, 1.0, 1.0), 10, 10, FTUS, 3.0)
    assert one_foot.factor == 10 and one_foot.cell_m == pytest.approx(3.048, abs=1e-3)


def test_rotated_or_non_square_grids_are_refused() -> None:
    with pytest.raises(ValueError, match="north-up"):
        plan_display_grid(from_origin(0, 0, 1.0, 2.0), 10, 10, 1.0, 3.0)


def test_rounding_is_half_away_from_zero_on_the_decimal_value() -> None:
    rounded = round_to_step(np.array([1.005, 1.004999, -1.005, 2275.364581, 0.0]), 0.01)
    assert np.allclose(rounded, [1.01, 1.0, -1.01, 2275.36, 0.0])


def test_block_validity_uses_the_cells_a_partial_block_covers() -> None:
    native = np.arange(16, dtype="float64").reshape(4, 4)
    valid = np.ones((4, 4), dtype=bool)
    valid[0, 0] = valid[0, 1] = valid[1, 0] = False
    mean, ok, count = aggregate_block(native, valid, 3, 0.5)
    assert ok[0, 0] and count[0, 0] == 6
    assert ok[0, 1] and mean[0, 1] == pytest.approx(native[0:3, 3].mean())
    sparse = np.zeros((3, 3), dtype=bool)
    sparse[2, 2] = True
    assert not aggregate_block(np.ones((3, 3)), sparse, 3, 0.5)[1][0, 0]


def test_reference_is_rounded_aligned_and_never_invents_support(
    tmp_path, terrain
) -> None:
    source, _ = terrain
    with rasterio.open(source) as src:
        grid = plan_display_grid(src.transform, src.width, src.height, FTUS, 3.0)
    reference = tmp_path / "reference.tif"
    stats = build_rounded_reference(
        source, reference, grid, round_step=0.01, band_rows=64
    )
    assert stats["display_valid_without_native"] == 0
    lo, hi = stats["native_range"]
    assert (
        lo - 0.005 <= stats["display_range"][0]
        and stats["display_range"][1] <= hi + 0.005
    )
    with rasterio.open(reference) as ref, rasterio.open(source) as src:
        data = ref.read(1)
        assert ref.dtypes[0] == "float64" and ref.tags(1)["UNITS"] == "Feet"
        assert (ref.transform.c, ref.transform.f) == (src.transform.c, src.transform.f)
        valid = data != NODATA
        assert np.allclose(data[valid], round_to_step(data[valid], 0.01), atol=1e-9)
        assert data[0, 0] == NODATA


@pytest.mark.parametrize("name", sorted(DISPLAY_ENCODINGS))
def test_each_encoding_decodes_to_the_rounded_reference(
    tmp_path, terrain, name
) -> None:
    source, _ = terrain
    with rasterio.open(source) as src:
        grid = plan_display_grid(src.transform, src.width, src.height, FTUS, 3.0)
    reference = tmp_path / "reference.tif"
    build_rounded_reference(source, reference, grid, round_step=0.01)
    encoding = DISPLAY_ENCODINGS[name]
    output = tmp_path / f"{name}.tif"
    if encoding.scaled:
        with pytest.raises(ValueError, match="not qualified"):
            write_display_cog(
                reference, output, encoding, round_step=0.01, scratch_dir=tmp_path
            )
        assert not output.exists()
        return
    report = write_display_cog(
        reference,
        output,
        encoding,
        round_step=0.01,
        scratch_dir=tmp_path,
        blocksize=128,
    )
    measure = measure_against_reference(output, reference, 0.01)
    assert measure["layout"] == "COG" and measure["overviews"]
    assert measure["same_grid_as_reference"] and measure["nodata_mismatch_cells"] == 0
    allowed = (encoding.lerc_max_z_error or 0.0) + measure["representation_tolerance"]
    assert measure["max_error_vs_rounded_reference"] <= allowed
    assert report["overview_method"] == "area_matched_mean"
    if encoding.lossy:
        assert not measure["qualified_selectable"]
    if encoding.scaled:
        assert (
            measure["max_error_vs_rounded_reference"] == 0.0
            and measure["scale"] == 0.01
        )
    elif not encoding.lossy:
        assert measure["values_off_rounding_grid"] == 0
        assert measure["overview_rounding_step"] == "0.01"
        assert measure["overview_levels_verified"] and measure["qualified_selectable"]


def test_overview_means_are_rounded_only_when_asked(tmp_path, terrain) -> None:
    source, _ = terrain
    with rasterio.open(source) as src:
        grid = plan_display_grid(src.transform, src.width, src.height, FTUS, 3.0)
    reference = tmp_path / "reference.tif"
    build_rounded_reference(source, reference, grid, round_step=0.01)
    plain = tmp_path / "plain.tif"
    rounded = tmp_path / "rounded.tif"
    write_display_cog(
        reference,
        rounded,
        DISPLAY_ENCODINGS["float32-deflate-p3"],
        round_step=0.01,
        scratch_dir=tmp_path,
        blocksize=128,
    )
    float32 = tmp_path / "float32-deflate-p3.source.tif"
    area_matched_cog(
        float32, plain, compress="DEFLATE", blocksize=128, lerc_max_z_error=None
    )

    def overview_residue(path: Path) -> float:
        with rasterio.open(path) as cog:
            level = cog.overviews(1)[0]
            data = cog.read(
                1, out_shape=(-(-cog.height // level), -(-cog.width // level))
            )
            values = data[data != cog.nodata].astype("float64")
            return float(np.abs(values - round_to_step(values, 0.01)).max())

    assert overview_residue(rounded) <= 2.5e-4  # half a float32 ulp near 2,200 ft
    assert overview_residue(plain) > 1e-3
    with rasterio.open(rounded) as cog:
        assert cog.tags()["overview_rounding_step"] == "0.01"


def test_round_step_must_be_positive(tmp_path, terrain) -> None:
    source, _ = terrain
    with pytest.raises(ValueError, match="round_step"):
        area_matched_cog(
            source,
            tmp_path / "x.tif",
            blocksize=128,
            round_step=0.0,
            require_area_matched=True,
        )


def test_default_display_encoding_is_decoded_float_without_zstd() -> None:
    assert DEFAULT_DISPLAY_ENCODINGS == ("float32-deflate-p3",)
    assert not any(
        DISPLAY_ENCODINGS[n].scaled or DISPLAY_ENCODINGS[n].lossy
        for n in DEFAULT_DISPLAY_ENCODINGS
    )


def test_rounding_never_moves_material_non_ties_and_rejects_invalid_step():
    values = np.array([2000.0049999, -2000.0049999, 10000000.001, -10000000.001])
    np.testing.assert_array_equal(
        round_to_step(values, 0.01), [2000.0, -2000.0, 10000000.0, -10000000.0]
    )
    for step in [0, -1, np.nan, np.inf, True]:
        with pytest.raises(ValueError, match="positive and finite"):
            round_to_step(values, step)
    with pytest.raises(ValueError, match="magnitude"):
        round_to_step([1e20], 0.01)
    assert float(round_to_step(np.nextafter(1.005, -np.inf), 0.01)) == 1.0
    assert float(round_to_step(np.nextafter(-1.005, np.inf), 0.01)) == -1.0


def test_masked_finite_native_pixels_do_not_invent_support(tmp_path):
    source, reference = tmp_path / "masked.tif", tmp_path / "reference.tif"
    write_terrain(source, np.full((3, 3), 2000.0))
    with rasterio.open(source, "r+") as src:
        mask = np.zeros((3, 3), dtype="uint8")
        mask[0, 0] = 255
        src.write_mask(mask)
        grid = plan_display_grid(src.transform, 3, 3, FTUS, 3.0)
    stats = build_rounded_reference(source, reference, grid, round_step=0.01)
    assert stats["native_valid_cells"] == 1
    assert stats["display_valid_cells"] == 0
    assert stats["native_valid_cells_in_nodata_blocks"] == 1
    with rasterio.open(reference) as ref:
        assert ref.read(1)[0, 0] == NODATA


def test_negative_elevations_remain_on_grid_at_all_levels(tmp_path):
    reference, output = tmp_path / "reference.tif", tmp_path / "negative.tif"
    write_terrain(reference, np.full((256, 512), -2000.0))
    write_display_cog(
        reference,
        output,
        DISPLAY_ENCODINGS["float32-deflate-p3"],
        round_step=0.01,
        scratch_dir=tmp_path,
        blocksize=128,
    )
    measured = measure_against_reference(output, reference, 0.01)
    assert measured["values_off_rounding_grid"] == 0
    assert measured["overview_levels_verified"] and measured["qualified_selectable"]
    assert len(measured["overview_levels"]) == len(measured["overviews"]) >= 2


@pytest.mark.parametrize("corruption", [2001.0, NODATA])
def test_corrupt_intermediate_overview_fails_numeric_qualification(
    tmp_path, corruption
):
    reference, output = tmp_path / "reference.tif", tmp_path / "corrupt.tif"
    write_terrain(reference, np.full((256, 512), 2000.0))
    write_display_cog(
        reference,
        output,
        DISPLAY_ENCODINGS["float32-deflate-p3"],
        round_step=0.01,
        scratch_dir=tmp_path,
        blocksize=128,
    )
    with (
        rasterio.Env(IGNORE_COG_LAYOUT_BREAK="YES"),
        rasterio.open(
            output, "r+", overview_level=0, IGNORE_COG_LAYOUT_BREAK="YES"
        ) as level,
    ):
        from rasterio.windows import Window

        level.write(
            np.array([[corruption]], dtype="float32"),
            1,
            window=Window(10, 10, 1, 1),
        )
    measured = measure_against_reference(output, reference, 0.01)
    assert (
        not measured["overview_levels_verified"]
        and not measured["qualified_selectable"]
    )
    assert (
        measured["max_error_vs_rounded_reference"] >= 1
        if corruption != NODATA
        else measured["nodata_mismatch_cells"] >= 1
    )


def test_missing_pyramid_cannot_qualify_from_tags(tmp_path):
    reference = tmp_path / "reference.tif"
    write_terrain(reference, np.full((256, 512), 2000.0))
    with rasterio.open(reference, "r+") as dataset:
        dataset.update_tags(
            overview_method="area_matched_mean", overview_rounding_step="0.01"
        )
    measured = measure_against_reference(reference, reference, 0.01)
    assert (
        not measured["overview_levels_verified"]
        and not measured["qualified_selectable"]
    )
    assert not measured["same_grid_as_reference"]


def test_overflow_quantile_bound_is_honest(tmp_path):
    source, reference = tmp_path / "rough.tif", tmp_path / "reference.tif"
    write_terrain(source, np.tile([0.0, 100.0, 0.0], (3, 1)))
    with rasterio.open(source) as src:
        grid = plan_display_grid(src.transform, 3, 3, FTUS, 3.0)
    stats = build_rounded_reference(source, reference, grid, round_step=0.01)
    assert (
        stats["aggregation_error"]["p95_upper_bound"]
        >= stats["aggregation_error"]["max"]
        > 3.2
    )
