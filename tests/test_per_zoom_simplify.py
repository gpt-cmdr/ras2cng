"""The tiler's per-zoom polygon option: which layers get it, and that the mesh never does."""

from __future__ import annotations

import inspect
import json
from pathlib import Path

import geopandas as gpd
import pyarrow as pa
import pytest
from shapely.geometry import LineString, box

from ras2cng import maplibre, simplify, vector_tiles


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in (simplify.TOLERANCE_ENV, simplify.TILE_POLYGON_SIMPLIFY_ENV):
        monkeypatch.delenv(name, raising=False)


def _polygons() -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame({"Name": ["a"]}, geometry=[box(-95.01, 29.99, -94.98, 30.02)], crs="EPSG:4326")


def _lines() -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame({"Name": ["a"]}, geometry=[LineString([(-95, 30), (-94.9, 30.1)])], crs="EPSG:4326")


def test_default_env_and_options(monkeypatch):
    assert simplify.tile_pixels_setting() == simplify.DEFAULT_TILE_POLYGON_SIMPLIFY_PX == 0.5
    monkeypatch.setenv(simplify.TILE_POLYGON_SIMPLIFY_ENV, "2")
    assert simplify.tile_pixels_setting() == 2.0
    for off in ("0", "off"):
        monkeypatch.setenv(simplify.TILE_POLYGON_SIMPLIFY_ENV, off)
        assert simplify.tile_pixels_setting() is None
    monkeypatch.setenv(simplify.TILE_POLYGON_SIMPLIFY_ENV, "65")
    with pytest.raises(ValueError, match="at most 64"):
        simplify.tile_pixels_setting()
    monkeypatch.delenv(simplify.TILE_POLYGON_SIMPLIFY_ENV)
    with simplify.simplify_options(tile_pixels=1.0):
        assert simplify.tile_pixels_setting() == 1.0
    with simplify.simplify_options(tile_pixels_enabled=False):
        assert simplify.tile_pixels_setting() is None
        assert simplify.tile_tolerance_ft() == simplify.DEFAULT_TILE_TOLERANCE_FT
    with simplify.simplify_options(enabled=False):
        assert simplify.tile_pixels_setting() is None and simplify.tile_tolerance_ft() is None
    # Turning off the first pass alone leaves the per-zoom option on.
    with simplify.simplify_options(tolerance_ft=0):
        assert simplify.tile_tolerance_ft() is None and simplify.tile_pixels_setting() == 0.5


@pytest.mark.parametrize("kind", sorted(simplify.NEVER_SIMPLIFY))
def test_mesh_kinds_cannot_get_the_option(monkeypatch, kind):
    monkeypatch.setenv(simplify.TILE_POLYGON_SIMPLIFY_ENV, "4")
    assert simplify.tile_polygon_pixels(kind) is None
    with simplify.simplify_options(tile_pixels=8.0):
        assert simplify.tile_polygon_pixels(kind) is None
    with pytest.raises(ValueError, match="polygon_simplify"):
        vector_tiles.ArrowLayer(load=pa.table, crs="EPSG:4326", polygon_simplify=0.5, kind=kind)


def test_layer_without_a_kind_cannot_get_the_option():
    assert simplify.tile_polygon_pixels(None) is None
    with pytest.raises(ValueError, match="polygon_simplify"):
        vector_tiles.ArrowLayer(load=pa.table, crs="EPSG:4326", polygon_simplify=0.5)


def test_delivery_source_sets_the_option_on_non_mesh_polygons_only(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(vector_tiles, "polygon_simplify_available", lambda: True)
    write = maplibre._write_delivery_source
    assert write(_polygons(), tmp_path / "a.ndgeojson", True, kind="mannings_n_regions")[0].polygon_simplify == 0.5
    assert write(_polygons(), tmp_path / "b.ndgeojson", True, kind="mesh_cells")[0].polygon_simplify is None
    assert write(_polygons(), tmp_path / "c.ndgeojson", True, kind="mesh_faces")[0].polygon_simplify is None
    assert write(_polygons(), tmp_path / "d.ndgeojson", True)[0].polygon_simplify is None
    assert write(_lines(), tmp_path / "e.ndgeojson", True, kind="breaklines")[0].polygon_simplify is None
    # Results joined to mesh cells keep the same guard.
    joined = _polygons().assign(maximum_depth=1.0)
    assert write(joined, tmp_path / "f.ndgeojson", True, kind="mesh_cells")[0].polygon_simplify is None
    # NDGeoJSON has no per-layer options.
    path = write(_polygons(), tmp_path / "g.ndgeojson", False, kind="mannings_n_regions")[0]
    assert isinstance(path, Path)


def test_older_freestiler_is_not_asked_for_the_option(monkeypatch, tmp_path: Path):
    seen: list[dict] = []

    class OldLayer:
        __dataclass_fields__ = {"input": None, "crs": None, "geometry_column": None}

        def __init__(self, input, crs=None, geometry_column=None):
            seen.append({"crs": crs})

    class Tiler:
        add_arrow_layer = None

    class Old:
        FreestileLayer = OldLayer
        _LayeredTiler = Tiler

        @staticmethod
        def freestile(frames, output, **kwargs):
            raise AssertionError

        @staticmethod
        def freestile_layers(layers, output, **kwargs):
            list(layers)
            Path(output).write_bytes(b"x")

    assert vector_tiles.freestiler_capabilities(Old)["polygon_simplify"] is False
    monkeypatch.setattr(vector_tiles, "polygon_simplify_available", lambda: False)
    layer = maplibre._write_delivery_source(_polygons(), tmp_path / "a.ndgeojson", True, kind="mannings_n_regions")[0]
    assert layer.polygon_simplify is None
    # Even a layer that asks for it: the older class has no such keyword.
    asked = vector_tiles.ArrowLayer(load=layer.load, crs=layer.crs, polygon_simplify=0.5, kind="mannings_n_regions")
    vector_tiles._run_freestiler_arrow(Old, tmp_path / "out.pmtiles", {"a": asked}, {}, {})
    assert seen == [{"crs": "EPSG:4326"}]


def _archive(tmp_path: Path) -> tuple[Path, Path]:
    project = tmp_path / "project"
    archive = project / "archive"
    archive.mkdir(parents=True)
    geometry = gpd.GeoDataFrame(
        {"layer": ["mesh_cells", "mesh_faces", "mannings_n_regions", "breaklines"]},
        geometry=[
            box(3_000_000, 13_900_000, 3_000_100, 13_900_100),
            LineString([(3_000_000, 13_900_000), (3_000_100, 13_900_000)]),
            box(3_000_000, 13_900_000, 3_001_000, 13_901_000),
            LineString([(3_000_000, 13_900_000), (3_000_500, 13_900_500)]),
        ],
        crs="EPSG:2278",
    )
    geometry.to_parquet(archive / "model.g01.parquet")
    layers = [{"layer": k, "filter_value": k} for k in geometry["layer"]]
    (archive / "manifest.json").write_text(
        json.dumps(
            {
                "project": {"name": "Synthetic"},
                "geometry": [{"geom_id": "g01", "parquet": "model.g01.parquet", "layers": layers}],
                "results": [],
            }
        ),
        encoding="utf-8",
    )
    hdf = project / "model.g01.hdf"
    hdf.touch()
    return archive, hdf


def test_real_freestiler_gets_the_option_only_on_land_cover(monkeypatch, tmp_path: Path):
    freestiler = pytest.importorskip("freestiler")
    if "polygon_simplify" not in inspect.signature(freestiler.FreestileLayer).parameters:
        pytest.skip("this freestiler has no polygon_simplify (older or PyPI build)")
    monkeypatch.setenv("RAS2CNG_VECTOR_TILER", "freestiler")
    calls: list[float | None] = []
    original = freestiler.FreestileLayer

    class Spy(original):
        __signature__ = inspect.signature(original)

        def __init__(self, *args, **kwargs):
            calls.append(kwargs.get("polygon_simplify"))
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(freestiler, "FreestileLayer", Spy)
    monkeypatch.setattr(
        maplibre,
        "_extent_from_hdf",
        lambda *_: gpd.GeoDataFrame(geometry=[box(-95.01, 29.99, -94.98, 30.02)], crs="EPSG:4326"),
    )
    archive, hdf = _archive(tmp_path)
    summary = maplibre.package_maplibre_viewer(
        archive, tmp_path / "viewer", geometry_hdfs={"g01": hdf}, scratch_dir=tmp_path / "scratch", max_zoom=12
    )
    manifest = json.loads(summary.manifest_path.read_text(encoding="utf-8"))
    per_zoom = manifest["vectorSimplification"]["perZoom"]
    assert per_zoom["enabled"] is True and per_zoom["tilePixels"] == 0.5
    assert per_zoom["method"] == "freestiler polygon_simplify"
    assert any(name.endswith("mannings-n-regions") for name in per_zoom["layers"])
    assert not any("mesh" in name for name in per_zoom["layers"])
    # Model extent and land cover ask for it; breaklines and the two mesh layers do not.
    assert calls.count(0.5) == len(per_zoom["layers"]) >= 1
    assert calls.count(None) >= 3

    off = maplibre.package_maplibre_viewer
    with simplify.simplify_options(tile_pixels_enabled=False):
        calls.clear()
        summary = off(
            archive, tmp_path / "viewer-off", geometry_hdfs={"g01": hdf}, scratch_dir=tmp_path / "scratch", max_zoom=12
        )
    manifest = json.loads(summary.manifest_path.read_text(encoding="utf-8"))
    assert manifest["vectorSimplification"]["perZoom"] == {"enabled": False, "reason": "turned off"}
    assert set(calls) == {None}


def test_cli_passes_the_per_zoom_options(monkeypatch, tmp_path: Path):
    from typer.testing import CliRunner

    from ras2cng.cli import app

    seen: list = []

    def fake_package(*_args, **_kwargs):
        seen.append(simplify.tile_pixels_setting())
        raise RuntimeError("stop")

    monkeypatch.setattr(maplibre, "package_maplibre_viewer", fake_package)
    hdf = tmp_path / "m.g01.hdf"
    hdf.touch()
    base = ["maplibre", str(tmp_path), str(tmp_path / "out"), "--geometry-hdf", f"g01={hdf}"]
    runner = CliRunner()
    runner.invoke(app, base)
    runner.invoke(app, [*base, "--tile-polygon-simplify", "1"])
    runner.invoke(app, [*base, "--tile-polygon-simplify", "0"])
    runner.invoke(app, [*base, "--no-tile-polygon-simplify"])
    runner.invoke(app, [*base, "--no-simplify"])
    assert seen == [0.5, 1.0, None, None, None]
