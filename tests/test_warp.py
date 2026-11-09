"""Reprojection (``ras2cng map --reproject-wgs84``) and the terrain-merge engine switch.

``mapping._reproject_tifs`` used to import ``calculate_default_transform`` from
the wrong rasterio module, catch the ``ImportError`` and return the rasters in
the model CRS. The CLI test mocks that function, so nothing saw it. The tests
here run it for real.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import numpy as np
import pytest

rasterio = pytest.importorskip("rasterio")
from rasterio.transform import from_origin  # noqa: E402

from ras2cng import raster_tiles, terrain, warp  # noqa: E402
from ras2cng.mapping import _reproject_tifs  # noqa: E402


def _surface(path: Path, *, size: int = 96, crs: str = "EPSG:2271", nodata: float = -9999.0) -> Path:
    """A smooth float32 surface in a projected foot CRS, with a nodata corner."""
    y, x = np.mgrid[0:size, 0:size].astype("float32")
    values = (100.0 + 0.5 * x + 0.25 * y + np.sin(x / 7.0) * 3.0).astype("float32")
    values[: size // 6, : size // 5] = nodata
    profile = {
        "driver": "GTiff", "width": size, "height": size, "count": 1, "dtype": "float32",
        "crs": crs, "transform": from_origin(2_000_000.0, 300_000.0, 20.0, 20.0),
        "nodata": nodata,
    }
    with rasterio.open(path, "w", **profile) as destination:
        destination.write(values, 1)
    return path


@pytest.fixture(autouse=True)
def _default_engines(monkeypatch):
    for name in (warp.REPROJECT_ENGINE_ENV, warp.TERRAIN_MERGE_ENGINE_ENV,
                 raster_tiles.MAX_MEMORY_ENV):
        monkeypatch.delenv(name, raising=False)


# ---------------------------------------------------------------------------
# The --reproject-wgs84 defect
# ---------------------------------------------------------------------------


def test_reproject_tifs_changes_the_crs(tmp_path):
    """Not mocked: the function must deliver EPSG:4326, in whichever engine."""
    source = _surface(tmp_path / "Depth (Max).tif")
    outputs = _reproject_tifs([source], "EPSG:4326")

    assert outputs == [tmp_path / "Depth (Max)_wgs84.tif"]
    with rasterio.open(source) as before, rasterio.open(outputs[0]) as after:
        assert before.crs.to_epsg() == 2271
        assert after.crs.to_epsg() == 4326
        assert after.dtypes == before.dtypes and after.count == 1
        assert after.nodata == before.nodata
        # Degrees, not feet: a pass-through would still hold foot coordinates.
        assert -180 <= after.bounds.left < after.bounds.right <= 180
        assert -90 <= after.bounds.bottom < after.bounds.top <= 90
        values = after.read(1, masked=True)
        assert values.count() > 0
        assert float(values.min()) >= 90.0 and float(values.max()) <= 180.0


def test_reproject_tifs_raises_instead_of_returning_the_source(tmp_path, monkeypatch):
    source = _surface(tmp_path / "WSE (Max).tif")

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(warp, "_reproject_rasterio", boom)
    monkeypatch.setattr(warp, "terrio_warp_supported", lambda: False)
    with pytest.raises(RuntimeError, match="reprojection of WSE .Max..tif to EPSG:4326 failed"):
        _reproject_tifs([source], "EPSG:4326")


def test_a_raster_without_a_crs_is_refused(tmp_path):
    path = tmp_path / "nocrs.tif"
    with rasterio.open(path, "w", driver="GTiff", width=4, height=4, count=1, dtype="float32",
                       transform=from_origin(0, 4, 1, 1)) as destination:
        destination.write(np.ones((4, 4), dtype="float32"), 1)
    with pytest.raises(ValueError, match="no CRS"):
        warp.reproject_path(path, tmp_path / "out.tif", "EPSG:4326")


def test_reproject_to_match_uses_the_shared_helper(tmp_path):
    source = _surface(tmp_path / "terrain.tif")
    target = rasterio.crs.CRS.from_epsg(4326)
    with rasterio.open(source) as dataset:
        output = terrain._reproject_to_match(dataset, target, tmp_path / "work")
    assert output == tmp_path / "work" / "_reproj_terrain.tif"
    with rasterio.open(output) as result:
        assert result.crs.to_epsg() == 4326


# ---------------------------------------------------------------------------
# Engine selection
# ---------------------------------------------------------------------------


def test_resolve_engines(monkeypatch):
    assert warp.resolve_reproject_engine() == "auto"
    assert warp.resolve_reproject_engine("GDAL") == "rasterio"
    assert warp.resolve_terrain_merge_engine() == "rasterio", "terrio merge is opt-in"
    monkeypatch.setenv(warp.TERRAIN_MERGE_ENGINE_ENV, "terrio")
    assert warp.resolve_terrain_merge_engine() == "terrio"
    monkeypatch.setenv(warp.REPROJECT_ENGINE_ENV, "proj")
    with pytest.raises(ValueError, match=warp.REPROJECT_ENGINE_ENV):
        warp.resolve_reproject_engine()


def test_without_a_streaming_terrio_rasterio_reprojects(tmp_path, monkeypatch):
    monkeypatch.setattr(warp, "terrio_warp_supported", lambda: False)
    source = _surface(tmp_path / "a.tif")
    assert warp.reproject_path(source, tmp_path / "b.tif", "EPSG:4326") == "rasterio"
    with pytest.raises(RuntimeError, match="needs a terrio"):
        warp.reproject_path(source, tmp_path / "c.tif", "EPSG:4326", engine="terrio")


def test_terrio_is_called_without_source_overviews_and_with_the_memory_env(tmp_path, monkeypatch):
    seen = {}

    def fake_warp(src, dst=None, **kwargs):
        seen.update(src=src, dst=dst, **kwargs)
        Path(dst).write_bytes(b"tif")

    monkeypatch.setitem(sys.modules, "terrio", types.SimpleNamespace(warp=fake_warp))
    monkeypatch.setattr(warp, "terrio_warp_supported", lambda: True)
    source = _surface(tmp_path / "a.tif")

    assert warp.reproject_path(source, tmp_path / "b.tif", "EPSG:4326") == "terrio"
    assert seen["overview"] == "NONE", "terrio's default reads source overviews; rasterio does not"
    assert seen["resampling"] == "bilinear" and "max_memory_mib" not in seen
    assert "WGS 84" in seen["t_srs"]
    assert "tr" not in seen and "te" not in seen and "tap" not in seen

    monkeypatch.setenv(raster_tiles.MAX_MEMORY_ENV, "16")
    warp.reproject_path(source, tmp_path / "c.tif", "EPSG:4326")
    assert seen["max_memory_mib"] == 16.0


def test_auto_falls_back_to_rasterio_when_terrio_refuses(tmp_path, monkeypatch):
    def refuse(src, dst=None, **kwargs):
        Path(dst).write_bytes(b"partial")
        raise ValueError("unsupported")

    monkeypatch.setitem(sys.modules, "terrio", types.SimpleNamespace(warp=refuse))
    monkeypatch.setattr(warp, "terrio_warp_supported", lambda: True)
    source = _surface(tmp_path / "a.tif")
    with pytest.warns(RuntimeWarning, match="using rasterio"):
        assert warp.reproject_path(source, tmp_path / "b.tif", "EPSG:4326") == "rasterio"
    with rasterio.open(tmp_path / "b.tif") as result:
        assert result.crs.to_epsg() == 4326
    with pytest.raises(ValueError, match="unsupported"):
        warp.reproject_path(source, tmp_path / "c.tif", "EPSG:4326", engine="terrio")


@pytest.mark.skipif(not warp.terrio_warp_supported(), reason="needs a terrio with the streaming warp")
def test_terrio_and_rasterio_reprojections_agree(tmp_path):
    source = _surface(tmp_path / "a.tif", size=160)
    assert warp.reproject_path(source, tmp_path / "t.tif", "EPSG:4326", engine="terrio") == "terrio"
    assert warp.reproject_path(source, tmp_path / "r.tif", "EPSG:4326", engine="rasterio") == "rasterio"
    with rasterio.open(tmp_path / "t.tif") as a, rasterio.open(tmp_path / "r.tif") as b:
        assert (a.width, a.height) == (b.width, b.height), "the same suggested grid"
        assert np.allclose(np.array(a.transform), np.array(b.transform), rtol=0, atol=1e-12)
        assert a.crs == b.crs and a.dtypes == b.dtypes and a.nodata == b.nodata
        left, right = a.read(1, masked=True), b.read(1, masked=True)
    assert np.array_equal(np.ma.getmaskarray(left), np.ma.getmaskarray(right))
    assert np.allclose(left.filled(0.0), right.filled(0.0), rtol=0, atol=1e-3)


# ---------------------------------------------------------------------------
# Terrain merge
# ---------------------------------------------------------------------------


def _pair(tmp_path):
    first = _surface(tmp_path / "first.tif", size=32)
    second = tmp_path / "second.tif"
    with rasterio.open(first) as source:
        profile = source.profile
    with rasterio.open(second, "w", **profile) as destination:
        destination.write(np.full((32, 32), 500.0, dtype="float32"), 1)
    return first, second


def test_terrain_merge_engine_defaults_to_rasterio(tmp_path, monkeypatch):
    first, second = _pair(tmp_path)
    assert terrain.select_terrain_merge_engine([first, second]) == ("rasterio", None)
    monkeypatch.setenv(warp.TERRAIN_MERGE_ENGINE_ENV, "terrio")
    monkeypatch.setattr(warp, "terrio_warp_supported", lambda: False)
    engine, reason = terrain.select_terrain_merge_engine([first, second])
    assert engine == "rasterio" and "streaming" in reason


def test_consolidation_records_the_merge_engine(tmp_path):
    import json

    first, second = _pair(tmp_path)
    out = tmp_path / "out"
    terrain.consolidate_terrain_files([first, second], out, terrain_name="T", target_resolution=20.0)
    provenance = json.loads((out / "T_terrain-provenance.json").read_text(encoding="utf-8"))
    assert provenance["merge_engine"] == "rasterio"
    assert provenance["merge_engine_fallback_reason"] is None


@pytest.mark.skipif(not warp.terrio_warp_supported(), reason="needs a terrio with the streaming warp")
def test_terrio_terrain_merge_keeps_the_grid_and_the_priority(tmp_path, monkeypatch):
    first, second = _pair(tmp_path)
    monkeypatch.setenv(warp.TERRAIN_MERGE_ENGINE_ENV, "terrio")
    engine, reason = terrain.select_terrain_merge_engine([first, second])
    assert (engine, reason) == ("terrio", None)

    by_terrio = terrain._merge_tifs([first, second], tmp_path / "t.tif", target_resolution=20.0,
                                    block_size=16, engine="terrio")
    by_rasterio = terrain._merge_tifs([first, second], tmp_path / "r.tif", target_resolution=20.0,
                                      block_size=16)
    with rasterio.open(by_terrio) as a, rasterio.open(by_rasterio) as b, rasterio.open(first) as src:
        assert (a.width, a.height, a.transform) == (b.width, b.height, b.transform)
        assert a.dtypes == ("float32",) and np.isnan(a.nodata)
        assert a.tags().get("ras2cng_terrain_merge_engine") == "terrio"
        left, right, original = a.read(1), b.read(1), src.read(1)
    # The first source wins where it is valid; the second fills its nodata corner.
    hole = original == -9999.0
    assert np.array_equal(left[hole], np.full(int(hole.sum()), 500.0, dtype="float32"))
    assert np.allclose(left[~hole], original[~hole], rtol=0, atol=1e-3)
    assert np.allclose(left, right, rtol=0, atol=1e-3)
