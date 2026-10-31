"""terrio raster tiler: backend selection and real (small) PMTiles output."""

from __future__ import annotations

import io
import json
from pathlib import Path

import numpy as np
import pytest

from ras2cng import raster_tiles


# ---------------------------------------------------------------------------
# Backend selection (no terrio needed)
# ---------------------------------------------------------------------------

def test_auto_prefers_terrio_when_installed(monkeypatch):
    monkeypatch.setenv(raster_tiles.TILER_ENV, "auto")
    monkeypatch.setattr(raster_tiles, "terrio_available", lambda: True)
    assert raster_tiles.resolve_backend() == "terrio"
    monkeypatch.setattr(raster_tiles, "terrio_available", lambda: False)
    assert raster_tiles.resolve_backend() == "gdal"


def test_explicit_terrio_requires_install(monkeypatch):
    monkeypatch.setenv(raster_tiles.TILER_ENV, "terrio")
    monkeypatch.setattr(raster_tiles, "terrio_available", lambda: False)
    with pytest.raises(RuntimeError, match="not installed"):
        raster_tiles.resolve_backend()


def test_unknown_backend_rejected(monkeypatch):
    monkeypatch.setenv(raster_tiles.TILER_ENV, "mapnik")
    with pytest.raises(ValueError):
        raster_tiles.resolve_backend()


def test_overview_resampling_defaults_and_override(monkeypatch):
    monkeypatch.delenv(raster_tiles.RESAMPLING_ENV, raising=False)
    assert raster_tiles.overview_resampling("result") == "NEAREST"
    assert raster_tiles.overview_resampling("terrain") == "AVERAGE"
    monkeypatch.setenv(raster_tiles.RESAMPLING_ENV, "average")
    assert raster_tiles.overview_resampling("result") == "AVERAGE"
    monkeypatch.setenv(raster_tiles.RESAMPLING_ENV, "cubic")
    with pytest.raises(ValueError):
        raster_tiles.overview_resampling("result")


# ---------------------------------------------------------------------------
# Real terrio output
# ---------------------------------------------------------------------------

terrio = pytest.importorskip("terrio")
rasterio = pytest.importorskip("rasterio")
pytest.importorskip("pmtiles")
PIL = pytest.importorskip("PIL.Image")


def _write_dem(path: Path, *, with_hole: bool = True) -> Path:
    from rasterio.transform import from_origin

    y, x = np.mgrid[0:256, 0:256].astype("float32")
    dem = 500 + 0.4 * x + 30 * np.sin(y / 18.0)
    if with_hole:
        dem[:, :40] = -9999.0  # nodata strip along the west edge
    profile = dict(
        driver="GTiff", width=256, height=256, count=1, dtype="float32",
        crs="EPSG:2271", nodata=-9999.0, transform=from_origin(2_000_000, 300_000, 20, 20),
    )
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(dem, 1)
    return path


def _tiles(path: Path):
    from pmtiles.reader import MmapSource, Reader, all_tiles

    with open(path, "rb") as f:
        reader = Reader(MmapSource(f))
        header = reader.header()
        tiles = {key: np.asarray(PIL.open(io.BytesIO(bytes(data))).convert("RGBA"))
                 for key, data in all_tiles(reader.get_bytes)}
    return header, tiles


def _ramp(path: Path) -> Path:
    path.write_text("500 0 128 0 255\n700 200 50 0 255\nnv 0 128 0 0\n", encoding="ascii")
    return path


def test_raster_info_matches_band_statistics(tmp_path):
    dem = _write_dem(tmp_path / "dem.tif")
    info = raster_tiles.raster_info(dem)
    band = info["bands"][0]
    with rasterio.open(dem) as src:
        data = src.read(1, masked=True)
    assert band["minimum"] == pytest.approx(float(data.min()))
    assert band["maximum"] == pytest.approx(float(data.max()))
    assert band["noDataValue"] == -9999.0


@pytest.mark.parametrize("hillshade", [False, True])
def test_render_pmtiles_writes_transparent_png_pyramid(tmp_path, hillshade):
    dem = _write_dem(tmp_path / "dem.tif")
    out = tmp_path / "terrain.pmtiles"
    raster_tiles.render_pmtiles(dem, out, ramp_path=_ramp(tmp_path / "ramp.txt"),
                                max_zoom=16, kind="terrain", hillshade=hillshade)
    header, tiles = _tiles(out)
    assert header["max_zoom"] == 16 and header["min_zoom"] < 16
    assert str(header["tile_type"]).endswith("PNG")
    top = [img for (z, _, _), img in tiles.items() if z == 16]
    alpha = np.concatenate([img[..., 3].ravel() for img in top])
    assert (alpha == 0).any() and (alpha == 255).any()
    # Transparent pixels keep ramp-coloured (not black) RGB: no dark fringe.
    for img in top:
        edge = (img[..., 3] > 0) & (img[..., 3] < 255)
        if edge.any():
            assert img[..., :3][edge].mean() > 20
    assert not list(tmp_path.glob(".terrain.pmtiles.partial-*"))


def test_hillshade_changes_colours(tmp_path):
    dem = _write_dem(tmp_path / "dem.tif")
    ramp = _ramp(tmp_path / "ramp.txt")
    plain, shaded = tmp_path / "plain.pmtiles", tmp_path / "shaded.pmtiles"
    raster_tiles.render_pmtiles(dem, plain, ramp_path=ramp, max_zoom=13, kind="terrain")
    raster_tiles.render_pmtiles(dem, shaded, ramp_path=ramp, max_zoom=13, kind="terrain",
                                hillshade=True)
    _, a = _tiles(plain)
    _, b = _tiles(shaded)
    key = next(k for k in a if k[0] == 13 and (a[k][..., 3] == 255).any())
    visible = a[key][..., 3] == 255
    assert np.abs(a[key][..., :3].astype(int) - b[key][..., :3].astype(int))[visible].mean() > 1
    assert np.array_equal(a[key][..., 3] > 0, b[key][..., 3] > 0), "hillshade must not change coverage"


def test_result_overviews_hold_visible_area(tmp_path):
    """NEAREST overviews keep the wet area at every zoom (no extent growth)."""
    dem = _write_dem(tmp_path / "depth.tif")
    out = tmp_path / "depth.pmtiles"
    raster_tiles.render_pmtiles(dem, out, ramp_path=_ramp(tmp_path / "ramp.txt"),
                                max_zoom=14, kind="result")
    _, tiles = _tiles(out)
    area = {}
    for (z, _, _), img in tiles.items():
        area[z] = area.get(z, 0) + int((img[..., 3] > 0).sum())
    top = max(area)
    for z, count in area.items():
        if z >= top - 3:
            assert count * 4 ** (top - z) / area[top] == pytest.approx(1.0, abs=0.06)


def _without_streaming(monkeypatch):
    """Behave like a terrio without streaming dem/warp (stock 0.5.0)."""
    monkeypatch.setattr(raster_tiles, "streaming_dem_supported", lambda: False)
    monkeypatch.setattr(raster_tiles, "streaming_warp_supported", lambda: False)


def test_too_large_raster_is_refused(tmp_path, monkeypatch):
    _without_streaming(monkeypatch)
    dem = _write_dem(tmp_path / "dem.tif")
    monkeypatch.setenv(raster_tiles.MAX_PIXELS_ENV, "1000")
    with pytest.raises(raster_tiles.TerrioTooLarge):
        raster_tiles.check_fits(dem)


def test_maplibre_auto_falls_back_to_gdal_when_too_large(tmp_path, monkeypatch):
    from ras2cng import maplibre

    _without_streaming(monkeypatch)
    dem = _write_dem(tmp_path / "dem.tif")
    monkeypatch.setenv(raster_tiles.TILER_ENV, "auto")
    monkeypatch.setenv(raster_tiles.MAX_PIXELS_ENV, "1000")
    monkeypatch.setattr(maplibre, "_require_gdal_raster_tools", lambda: None)
    assert maplibre._raster_backend_for(dem) == "gdal"
    monkeypatch.setenv(raster_tiles.TILER_ENV, "terrio")
    with pytest.raises(raster_tiles.TerrioTooLarge):
        maplibre._raster_backend_for(dem)


def test_package_terrain_with_terrio_records_hillshade(tmp_path, monkeypatch):
    from ras2cng import maplibre

    monkeypatch.setenv(raster_tiles.TILER_ENV, "terrio")
    dem = _write_dem(tmp_path / "terrain.tif")
    viewer = tmp_path / "viewer"
    viewer.mkdir()
    (viewer / "manifest.json").write_text(json.dumps({"tilesets": []}), encoding="utf-8")
    summary = maplibre.package_maplibre_terrain(dem, viewer, max_zoom=13)
    manifest = json.loads(summary.manifest_path.read_text(encoding="utf-8"))
    terrain = next(t for t in manifest["tilesets"] if t["id"] == "terrain")
    assert terrain["hillshade"] is True
    assert terrain["rasterTiler"] == "terrio"
    assert summary.pmtiles_path.is_file()
