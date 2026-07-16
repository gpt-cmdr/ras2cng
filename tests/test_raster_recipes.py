from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

from ras2cng.raster_recipes import RECIPES, run_raster_recipe


def _write_raster(path: Path, values, *, units: str, transform=None, nodata=-9999.0) -> Path:
    data = np.asarray(values, dtype="float32")
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        width=data.shape[1],
        height=data.shape[0],
        count=1,
        dtype="float32",
        crs="EPSG:2271",
        transform=transform or from_origin(0, data.shape[0] * 5, 5, 5),
        nodata=nodata,
    ) as destination:
        destination.write(data, 1)
        destination.update_tags(1, units=units)
    return path


def test_recipe_allowlist_contains_tier_one_products():
    assert set(RECIPES) == {
        "compare_wse",
        "compare_depth",
        "compare_velocity",
        "depth_velocity",
        "depth_velocity_squared",
        "hazard_class",
        "inundation_threshold",
        "terrain_mod_delta",
    }


def test_compare_depth_is_windowed_cog_with_transparent_nodata(tmp_path: Path):
    baseline = _write_raster(tmp_path / "baseline.tif", [[1, 2], [3, -9999]], units="ft")
    candidate = _write_raster(tmp_path / "candidate.tif", [[2, 1], [5, 8]], units="ft")
    output = tmp_path / "difference.tif"

    result = run_raster_recipe(
        "compare_depth",
        {"baseline": baseline, "candidate": candidate},
        output,
        block_size=64,
    )

    with rasterio.open(output) as source:
        values = source.read(1, masked=True)
        assert source.driver == "GTiff"
        assert source.is_tiled
        assert source.nodata == -9999.0
        assert values[0, 0] == 1
        assert values[0, 1] == -1
        assert values[1, 0] == 2
        assert values.mask[1, 1]
    provenance = json.loads(result.provenance_path.read_text(encoding="utf-8"))
    assert provenance["recipe"]["recipe_id"] == "compare_depth"
    assert provenance["interpolationAuthority"] == "RASMapper/RasProcess source rasters"
    assert provenance["output"]["units"] == "ft"
    assert result.statistics["valid_pixels"] == 3


def test_raster_recipe_refuses_grid_mismatch(tmp_path: Path):
    baseline = _write_raster(tmp_path / "baseline.tif", [[1, 2], [3, 4]], units="ft")
    candidate = _write_raster(
        tmp_path / "candidate.tif",
        [[1, 2], [3, 4]],
        units="ft",
        transform=from_origin(1, 10, 5, 5),
    )
    with pytest.raises(ValueError, match="not aligned"):
        run_raster_recipe(
            "compare_depth",
            {"baseline": baseline, "candidate": candidate},
            tmp_path / "difference.tif",
        )


def test_synchronized_products_reject_independent_maxima(tmp_path: Path):
    depth = _write_raster(tmp_path / "depth.tif", [[2]], units="ft")
    velocity = _write_raster(tmp_path / "velocity.tif", [[3]], units="ft/s")
    with pytest.raises(ValueError, match="synchronized"):
        run_raster_recipe(
            "depth_velocity",
            {"depth": depth, "velocity": velocity},
            tmp_path / "dv.tif",
            profile="Max",
        )

    run_raster_recipe(
        "depth_velocity",
        {"depth": depth, "velocity": velocity},
        tmp_path / "dv.tif",
        profile="01JAN2026 12:00:00",
    )
    with rasterio.open(tmp_path / "dv.tif") as source:
        assert source.read(1)[0, 0] == 6
        assert source.tags(1)["units"] == "ft2/s"


def test_hazard_class_uses_aidr_limits_and_converts_us_units(tmp_path: Path):
    depths_m = np.array([[0.2, 0.4, 1.0, 1.5, 3.0, 5.0]], dtype="float32")
    velocities_mps = np.array([[1.0, 1.2, 0.5, 0.6, 1.0, 1.0]], dtype="float32")
    depth = _write_raster(tmp_path / "depth.tif", depths_m / 0.3048, units="ft")
    velocity = _write_raster(tmp_path / "velocity.tif", velocities_mps / 0.3048, units="ft/s")

    result = run_raster_recipe(
        "hazard_class",
        {"depth": depth, "velocity": velocity},
        tmp_path / "hazard.tif",
        profile="01JAN2026 12:00:00",
    )

    with rasterio.open(result.output_path) as source:
        assert source.read(1).tolist() == [[1, 2, 3, 4, 5, 6]]
        assert source.nodata == 0
        assert source.tags(1)["units"] == "H1-H6"


def test_threshold_recipe_and_parameter_allowlist(tmp_path: Path):
    depth = _write_raster(tmp_path / "depth.tif", [[0.1, 0.5, 1.0]], units="m")
    run_raster_recipe(
        "inundation_threshold",
        {"depth": depth},
        tmp_path / "threshold.tif",
        parameters={"threshold": 0.5},
    )
    with rasterio.open(tmp_path / "threshold.tif") as source:
        assert source.read(1).tolist() == [[0, 0, 1]]

    with pytest.raises(ValueError, match="Unsupported parameters"):
        run_raster_recipe(
            "inundation_threshold",
            {"depth": depth},
            tmp_path / "bad.tif",
            parameters={"script": "anything"},
        )
