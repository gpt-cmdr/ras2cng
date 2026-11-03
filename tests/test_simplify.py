"""Douglas-Peucker polygon simplification before tiling (ras2cng.simplify)."""

from __future__ import annotations

import json
import math
from pathlib import Path

import geopandas as gpd
import numpy as np
import pytest
import shapely
from shapely.geometry import LineString, MultiPolygon, Polygon, box

from ras2cng import maplibre, simplify

US_FEET = "EPSG:2278"  # NAD83 / Texas South Central (ftUS)
METRES = "EPSG:26915"


def _wobbly(cx: float, cy: float, radius: float, n: int, seed: int, noise: float = 0.002) -> list[tuple[float, float]]:
    rng = np.random.default_rng(seed)
    angles = np.linspace(0, 2 * math.pi, n, endpoint=False)
    r = radius * (1 + noise * (rng.random(n) - 0.5))
    return list(zip(cx + r * np.cos(angles), cy + r * np.sin(angles)))


def _land_cover() -> gpd.GeoDataFrame:
    shell = _wobbly(3_000_000, 13_900_000, 500, 4000, 1)
    hole = _wobbly(3_000_000, 13_900_000, 100, 800, 2)[::-1]
    thin = [(3_000_100, 13_900_300), (3_000_101, 13_900_300), (3_000_101, 13_900_300.05), (3_000_100, 13_900_300.05)]
    far = Polygon(_wobbly(3_002_000, 13_900_000, 200, 1000, 3))
    return gpd.GeoDataFrame(
        {"Name": ["a", "b", "c"]},
        geometry=[Polygon(shell, [hole, thin]), MultiPolygon([far, box(3_003_000, 13_900_000, 3_003_010, 13_900_010)]), None],
        crs=US_FEET,
    )


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in (simplify.TOLERANCE_ENV, simplify.ENGINE_ENV):
        monkeypatch.delenv(name, raising=False)


def test_tolerance_is_feet_converted_to_crs_units():
    assert simplify.tolerance_in_crs_units(US_FEET, 0.1) == (0.1, "US survey foot")
    tol, unit = simplify.tolerance_in_crs_units(METRES, 0.1)
    assert unit == "metre" and tol == pytest.approx(0.03048)
    assert simplify.tolerance_in_crs_units("EPSG:4326", 0.1) is None


def test_tile_tolerance_defaults_env_and_overrides(monkeypatch):
    assert simplify.tile_tolerance_ft() == simplify.DEFAULT_TILE_TOLERANCE_FT == 1.0
    monkeypatch.setenv(simplify.TOLERANCE_ENV, "0.5")
    assert simplify.tile_tolerance_ft() == 0.5
    for off in ("off", "0", "none"):
        monkeypatch.setenv(simplify.TOLERANCE_ENV, off)
        assert simplify.tile_tolerance_ft() is None
    monkeypatch.setenv(simplify.TOLERANCE_ENV, "-1")
    with pytest.raises(ValueError):
        simplify.tile_tolerance_ft()
    monkeypatch.delenv(simplify.TOLERANCE_ENV)
    with simplify.simplify_options(tolerance_ft=2.0):
        assert simplify.tile_tolerance_ft() == 2.0
        with simplify.simplify_options(enabled=False):
            assert simplify.tile_tolerance_ft() is None
    with simplify.simplify_options(tolerance_ft=0):
        assert simplify.tile_tolerance_ft() is None
    assert simplify.tile_tolerance_ft() == 1.0


def test_engine_selection(monkeypatch):
    monkeypatch.setenv(simplify.ENGINE_ENV, "shapely")
    assert simplify.resolve_engine() == simplify.SHAPELY
    monkeypatch.setenv(simplify.ENGINE_ENV, "bogus")
    with pytest.raises(ValueError):
        simplify.resolve_engine()
    monkeypatch.setattr(simplify, "terrio_simplify", lambda: None)
    assert simplify.resolve_engine("auto") == simplify.SHAPELY
    with pytest.raises(RuntimeError, match="simplify_wkb"):
        simplify.resolve_engine("terrio")


def test_shapely_engine_matches_geos_and_keeps_collapsed_rings():
    gdf = _land_cover()
    out, record = simplify.simplify_frame(gdf, kind="mannings_n_regions", tolerance_ft=0.1, engine="shapely")
    assert record["engine"] == "shapely" and record["features"] == 2
    assert record["vertices_out"] < record["vertices_in"]
    assert record["rings_kept"] == 1  # the 0.05 ft sliver hole
    assert out.geometry.iloc[2] is None and gdf.geometry.iloc[0].equals(_land_cover().geometry.iloc[0])
    first = out.geometry.iloc[0]
    assert len(first.interiors) == 2
    assert list(first.interiors[1].coords) == list(gdf.geometry.iloc[0].interiors[1].coords)
    # Where no ring collapses, the vertices are GEOS's DouglasPeuckerSimplifier's.
    geos = shapely.simplify(gdf.geometry.iloc[1], 0.1, preserve_topology=False)
    assert shapely.get_coordinates(out.geometry.iloc[1]).tolist() == shapely.get_coordinates(geos).tolist()
    shell_only = Polygon(gdf.geometry.iloc[0].exterior)
    ours, _ = simplify.simplify_polygons(np.array([shell_only], dtype=object), 0.1, simplify.SHAPELY)
    assert ours[0].equals_exact(shapely.simplify(shell_only, 0.1, preserve_topology=False), 0)


@pytest.mark.skipif(simplify.terrio_simplify() is None, reason="needs a terrio build with simplify_wkb")
def test_terrio_engine_equals_shapely_engine():
    gdf = _land_cover()
    a, ra = simplify.simplify_frame(gdf, kind="mannings_n_regions", tolerance_ft=0.1, engine="terrio")
    b, rb = simplify.simplify_frame(gdf, kind="mannings_n_regions", tolerance_ft=0.1, engine="shapely")
    assert ra["engine"] == "terrio"
    for key in ("features", "vertices_in", "vertices_out", "rings_kept"):
        assert ra[key] == rb[key], key
    for x, y in zip(a.geometry, b.geometry):
        assert (x is None and y is None) or shapely.equals_exact(x, y, tolerance=0)
        if x is not None:
            assert shapely.get_coordinates(x).tolist() == shapely.get_coordinates(y).tolist()


def test_mesh_lines_points_and_geographic_layers_are_untouched():
    gdf = _land_cover()
    for kind in ("mesh_cells", "mesh_faces"):
        out, record = simplify.simplify_frame(gdf, kind=kind, tolerance_ft=0.1)
        assert out is gdf and record is None
    lines = gpd.GeoDataFrame(geometry=[LineString(_wobbly(0, 0, 100, 50, 4))], crs=US_FEET)
    out, record = simplify.simplify_frame(lines, kind="centerlines", tolerance_ft=0.1)
    assert out is lines and record is None
    geographic = gdf.to_crs("EPSG:4326")
    with pytest.warns(UserWarning, match="not projected"):
        out, record = simplify.simplify_frame(geographic, kind="refinement_regions_geo", tolerance_ft=0.1)
    assert out is geographic and record is None
    out, record = simplify.simplify_frame(gdf, kind="mannings_n_regions", tolerance_ft=None)
    assert out is gdf and record is None


def test_metric_layers_use_the_metric_tolerance():
    gdf = _land_cover().to_crs(METRES)
    _, record = simplify.simplify_frame(gdf, kind="mesh_areas", tolerance_ft=0.1)
    assert record["units"] == "metre" and record["tolerance"] == pytest.approx(0.03048)


def test_simplify_layers_skips_mesh_and_records_each_layer():
    poly = _land_cover().geometry.iloc[0]
    frame = gpd.GeoDataFrame(
        {"layer": ["mesh_cells", "mannings_n_regions", "centerlines", "mesh_areas"]},
        geometry=[poly, poly, LineString([(0, 0), (1, 0.01), (2, 0)]), poly],
        crs=US_FEET,
    )
    out, records = simplify.simplify_layers(frame, tolerance_ft=0.1)
    assert [r["layer"] for r in records] == ["mannings_n_regions", "mesh_areas"]
    assert out.geometry.iloc[0].equals_exact(poly, 0)
    assert out.geometry.iloc[2].equals_exact(frame.geometry.iloc[2], 0)
    assert shapely.get_num_coordinates(out.geometry.iloc[1]) < shapely.get_num_coordinates(poly)
    assert out.crs == frame.crs and list(out["layer"]) == list(frame["layer"])
    same, none = simplify.simplify_layers(frame, tolerance_ft=None)
    assert same is frame and none == []


def _projected_archive(tmp_path: Path) -> tuple[Path, Path]:
    project_dir = tmp_path / "project"
    archive_dir = project_dir / "archive"
    archive_dir.mkdir(parents=True)
    land = _land_cover().iloc[:2]
    geometry = gpd.GeoDataFrame(
        {"layer": ["mesh_cells", "mannings_n_regions", "mannings_n_regions"]},
        geometry=[land.geometry.iloc[0], *land.geometry],
        crs=US_FEET,
    )
    geometry.to_parquet(archive_dir / "model.g01.parquet")
    manifest = {
        "project": {"name": "Synthetic Model"},
        "geometry": [
            {
                "geom_id": "g01",
                "parquet": "model.g01.parquet",
                "layers": [
                    {"layer": "mesh_cells", "filter_value": "mesh_cells"},
                    {"layer": "mannings_n_regions", "filter_value": "mannings_n_regions"},
                ],
            }
        ],
        "results": [],
    }
    archive_dir.joinpath("manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    hdf = project_dir / "model.g01.hdf"
    hdf.touch()
    return archive_dir, hdf


def _package(monkeypatch, tmp_path: Path, **options) -> tuple[dict, dict[str, gpd.GeoDataFrame]]:
    archive_dir, hdf = _projected_archive(tmp_path)
    delivered: dict[str, gpd.GeoDataFrame] = {}

    def fake_tiler(output: Path, layers, *_args, **_kwargs):
        for name, source in layers:
            if isinstance(source, Path):
                lines = source.read_text(encoding="utf-8").splitlines()
                geoms = shapely.from_geojson([json.dumps(json.loads(line)["geometry"]) for line in lines])
                delivered[name] = gpd.GeoDataFrame(geometry=geoms, crs="EPSG:4326")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"pmtiles")

    footprint = gpd.GeoDataFrame(geometry=[box(-95.01, 29.99, -94.98, 30.02)], crs="EPSG:4326")
    monkeypatch.setattr(maplibre, "_require_cli", lambda _: None)
    monkeypatch.setattr(maplibre, "_arrow_delivery", lambda _backend: False)
    monkeypatch.setattr(maplibre, "_require_vector_tiler", lambda: "tippecanoe")
    monkeypatch.setattr(maplibre, "_extent_from_hdf", lambda *_: footprint)
    monkeypatch.setattr(maplibre, "_run_vector_tiler", fake_tiler)
    with simplify.simplify_options(**options):
        summary = maplibre.package_maplibre_viewer(
            archive_dir, tmp_path / "viewer", geometry_hdfs={"g01": hdf}, scratch_dir=tmp_path / "scratch"
        )
    return json.loads(summary.manifest_path.read_text(encoding="utf-8")), delivered


def _layers(manifest: dict) -> dict[str, dict]:
    return {layer["kind"]: layer for tileset in manifest["tilesets"] for layer in tileset["layers"]}


def test_viewer_simplifies_polygon_layers_by_default_and_records_it(monkeypatch, tmp_path: Path):
    manifest, delivered = _package(monkeypatch, tmp_path)
    summary = manifest["vectorSimplification"]
    assert summary["enabled"] is True and summary["toleranceFt"] == 1.0 and summary["method"] == "douglas-peucker"
    layers = _layers(manifest)
    record = layers["mannings_n_regions"]["simplification"]
    assert record["toleranceFt"] == 1.0 and record["units"] == "US survey foot"
    assert record["verticesOut"] < record["verticesIn"] and record["ringsKept"] == 1
    assert "simplification" not in layers["mesh_cells"]
    land = next(frame for name, frame in delivered.items() if name.endswith("mannings-n-regions"))
    assert shapely.get_num_coordinates(land.geometry).sum() == record["verticesOut"]


def test_viewer_without_simplification(monkeypatch, tmp_path: Path):
    manifest, delivered = _package(monkeypatch, tmp_path, enabled=False)
    assert manifest["vectorSimplification"]["enabled"] is False and "method" not in manifest["vectorSimplification"]
    assert "simplification" not in _layers(manifest)["mannings_n_regions"]
    land = next(frame for name, frame in delivered.items() if name.endswith("mannings-n-regions"))
    assert shapely.get_num_coordinates(land.geometry).sum() == shapely.get_num_coordinates(_land_cover().geometry.iloc[:2]).sum()


def test_cli_passes_simplify_options(monkeypatch, tmp_path: Path):
    from typer.testing import CliRunner

    from ras2cng.cli import app

    seen: list = []

    def fake_package(*_args, **_kwargs):
        seen.append(simplify.tile_tolerance_ft())
        raise RuntimeError("stop")

    monkeypatch.setattr(maplibre, "package_maplibre_viewer", fake_package)
    hdf = tmp_path / "m.g01.hdf"
    hdf.touch()
    base = ["maplibre", str(tmp_path), str(tmp_path / "out"), "--geometry-hdf", f"g01={hdf}"]
    runner = CliRunner()
    runner.invoke(app, base)
    runner.invoke(app, [*base, "--simplify-tolerance", "0.5"])
    runner.invoke(app, [*base, "--no-simplify"])
    runner.invoke(app, [*base, "--simplify-tolerance", "0"])
    assert seen == [1.0, 0.5, None, None]


def test_archive_simplification_is_opt_in(tmp_path: Path, monkeypatch):
    from unittest.mock import patch

    from tests.test_project_archive import _make_fake_ras
    from ras2cng.project import archive_project

    poly = _land_cover().geometry.iloc[0]
    merged = gpd.GeoDataFrame(
        {"layer": ["mesh_cells", "mannings_n_regions"]}, geometry=[poly, poly], crs=US_FEET
    )
    monkeypatch.setenv("RAS2CNG_ARCHIVE_ENGINE", "python")
    monkeypatch.setenv(simplify.TOLERANCE_ENV, "5")  # tiles only: never applies to archives
    ras, project_dir, _ = _make_fake_ras(tmp_path)
    prj = project_dir / "FakeModel.prj"
    with patch("ras2cng.project.init_ras_project", return_value=ras), patch(
        "ras2cng.project.merge_all_layers", side_effect=lambda **_: merged.copy()
    ):
        plain = archive_project(prj, tmp_path / "plain", sort=False)
        simple = archive_project(prj, tmp_path / "simple", sort=False, simplify_tolerance_ft=0.1)
    assert "simplification" not in plain.geometry[0]
    record = simple.geometry[0]["simplification"]
    assert record["tolerance_ft"] == 0.1 and [r["layer"] for r in record["layers"]] == ["mannings_n_regions"]
    stored = json.loads((tmp_path / "simple" / "manifest.json").read_text(encoding="utf-8"))
    assert stored["geometry"][0]["simplification"] == record
    plain_frame = gpd.read_parquet(tmp_path / "plain" / "FakeModel.g01.parquet")
    simple_frame = gpd.read_parquet(tmp_path / "simple" / "FakeModel.g01.parquet")
    counts = lambda f, k: int(shapely.get_num_coordinates(f[f["layer"] == k].geometry).sum())  # noqa: E731
    assert counts(plain_frame, "mannings_n_regions") == shapely.get_num_coordinates(poly)
    assert counts(simple_frame, "mannings_n_regions") == record["layers"][0]["vertices_out"] < counts(plain_frame, "mannings_n_regions")
    assert counts(simple_frame, "mesh_cells") == counts(plain_frame, "mesh_cells")
