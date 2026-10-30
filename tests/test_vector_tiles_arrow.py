"""freestiler Arrow input: preference, column rules, CRS refusal fallback, viewer wiring."""

from __future__ import annotations

import dataclasses
import json
import sys
import types
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from shapely.geometry import LineString, Point, box

from ras2cng import maplibre, vector_tiles

# NAD83(HARN) / California zone 2 (ftUS): Davis. freestiler #5 refuses HARN
# (PROJ shifts it by about 1 m), so it exercises the Python fallback.
HARN = "EPSG:2871"
PA_NORTH = "EPSG:2271"  # NAD83 / Pennsylvania North (ftUS): Bald Eagle


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    for name in (
        "RAS2CNG_VECTOR_TILER",
        vector_tiles.INPUT_ENV,
        vector_tiles.GZIP_LEVEL_ENV,
        vector_tiles.FEATURE_IDS_ENV,
    ):
        monkeypatch.delenv(name, raising=False)
    vector_tiles._WARNED.clear()
    vector_tiles._RUST_REFUSED_CRS.clear()
    yield
    vector_tiles._RUST_REFUSED_CRS.clear()


def _fake_arrow_freestiler(monkeypatch, *, refuse: tuple[str, ...] = ()):
    """A freestiler with Arrow input and buffer; refuses CRSs containing ``refuse``."""

    calls: dict = {"attempts": []}

    @dataclasses.dataclass
    class FreestileLayer:
        input: object
        min_zoom: int | None = None
        max_zoom: int | None = None
        crs: object = None
        geometry_column: str | None = None

    class _LayeredTiler:
        def add_arrow_layer(self, *args, **kwargs):  # pragma: no cover - capability marker
            raise NotImplementedError

    def freestile(frames, output, *, min_zoom, max_zoom, quiet, generate_ids, gzip_level=None, buffer=None):
        raise AssertionError("Arrow layers go through freestile_layers")

    def freestile_layers(pairs, output, *, min_zoom, max_zoom, quiet, generate_ids, gzip_level=None, buffer=None):
        attempt = {"layers": [], "options": dict(gzip_level=gzip_level, buffer=buffer, generate_ids=generate_ids)}
        calls["attempts"].append(attempt)
        for name, layer in pairs:
            if isinstance(layer, FreestileLayer):
                if any(token in str(layer.crs) for token in refuse):
                    raise ValueError(f"Error: datum NAD83(HARN) is not supported for layer '{name}'")
                attempt["layers"].append((name, layer.crs, layer.input))
            else:
                attempt["layers"].append((name, "frame", layer))
        Path(output).write_bytes(b"pmtiles")

    module = types.ModuleType("freestiler")
    module.freestile = freestile
    module.freestile_layers = freestile_layers
    module.FreestileLayer = FreestileLayer
    module._LayeredTiler = _LayeredTiler
    monkeypatch.setitem(sys.modules, "freestiler", module)
    monkeypatch.setattr(vector_tiles, "freestiler_available", lambda: True)
    return calls


# --------------------------------------------------------------------------
# capability detection and backend preference
# --------------------------------------------------------------------------


def test_capabilities_detect_arrow_and_buffer(monkeypatch):
    _fake_arrow_freestiler(monkeypatch)
    caps = vector_tiles.freestiler_capabilities(sys.modules["freestiler"])
    assert caps == {
        "layers": True,
        "gzip": True,
        "layers_gzip": True,
        "buffer": True,
        "layers_buffer": True,
        "arrow": True,
    }


def test_auto_prefers_freestiler_with_arrow_and_buffer_over_tippecanoe(monkeypatch):
    _fake_arrow_freestiler(monkeypatch)
    monkeypatch.setattr(vector_tiles.shutil, "which", lambda command: f"/usr/bin/{command}")
    assert vector_tiles.vector_tiler_backend() == vector_tiles.FREESTILER
    # tippecanoe stays available as the explicit choice.
    monkeypatch.setenv("RAS2CNG_VECTOR_TILER", "tippecanoe")
    assert vector_tiles.vector_tiler_backend() == vector_tiles.TIPPECANOE


def test_auto_keeps_tippecanoe_over_an_older_freestiler(monkeypatch):
    module = types.ModuleType("freestiler")

    def freestile(frames, output, *, min_zoom, max_zoom, quiet, generate_ids):
        pass

    module.freestile = freestile
    monkeypatch.setitem(sys.modules, "freestiler", module)
    monkeypatch.setattr(vector_tiles, "freestiler_available", lambda: True)
    monkeypatch.setattr(vector_tiles.shutil, "which", lambda command: f"/usr/bin/{command}")
    assert vector_tiles.vector_tiler_backend() == vector_tiles.TIPPECANOE
    assert vector_tiles.arrow_input_enabled() is False
    monkeypatch.setattr(vector_tiles.shutil, "which", lambda command: None)
    assert vector_tiles.vector_tiler_backend() == vector_tiles.FREESTILER


def test_input_env(monkeypatch):
    _fake_arrow_freestiler(monkeypatch)
    assert vector_tiles.arrow_input_enabled() is True
    monkeypatch.setenv(vector_tiles.INPUT_ENV, "geojson")
    assert vector_tiles.arrow_input_enabled() is False
    monkeypatch.setenv(vector_tiles.INPUT_ENV, "parquet")
    with pytest.raises(ValueError, match=vector_tiles.INPUT_ENV):
        vector_tiles.arrow_input_enabled()
    monkeypatch.setenv(vector_tiles.INPUT_ENV, "arrow")
    monkeypatch.setattr(vector_tiles, "freestiler_available", lambda: False)
    with pytest.raises(RuntimeError, match="no Arrow input"):
        vector_tiles.arrow_input_enabled()


# --------------------------------------------------------------------------
# column rules: properties as the GeoJSON path delivered them
# --------------------------------------------------------------------------


def test_delivery_table_shapes_properties_like_the_geojson_path():
    table = pa.table(
        {
            "geometry": pa.array([Point(0, 0).wkb, Point(1, 1).wkb], pa.binary()),
            "wse": pa.array([647.5546, float("nan")], pa.float32()),
            "time": pa.array(
                [pd.Timestamp("1999-01-01 12:00:00"), None], pa.timestamp("ns")
            ),
            "flag": pa.array([1, 0], pa.uint64()),
            "small": pa.array([3, None], pa.int32()),
            "empty": pa.array([None, None], pa.null()),
            "all_nan": pa.array([float("nan"), None], pa.float64()),
            "kind": pa.array(["a", "b"]).dictionary_encode(),
            "profile": pa.array([[1.5, 2.5], None], pa.list_(pa.float64())),
            "blob": pa.array([b"x", b"y"], pa.binary()),
        }
    )
    out = vector_tiles.delivery_table(table)

    assert out.column_names == ["geometry", "wse", "time", "flag", "small", "kind", "profile"]
    assert out["wse"].type == pa.float32() and out["wse"].to_pylist()[1] is None
    # str(pandas.Timestamp), as json.dump(default=str) wrote it, not ISO "T".
    assert out["time"].to_pylist() == ["1999-01-01 12:00:00", None]
    assert out["flag"].type == pa.int64() and out["small"].type == pa.int64()
    assert out["kind"].to_pylist() == ["a", "b"]
    # NumPy arrays went through json.dump(default=str): their str() text.
    assert out["profile"].to_pylist() == [str(np.array([1.5, 2.5])), None]
    # The pmtiles CLI handed GeoDataFrames to freestiler directly: compact JSON.
    direct = vector_tiles.delivery_table(table, geojson_nested=False)
    assert direct["profile"].to_pylist() == ["[1.5,2.5]", None]


def test_frame_table_matches_the_ndgeojson_round_trip(tmp_path: Path):
    frame = gpd.GeoDataFrame(
        {
            "Name": ["weir", "culvert"],
            "Profile_Data": [np.array([{"Station": 0.0, "Elevation": 683.0}]), None],
            "mixed": ["text", 4],
            "depth": [1.25, np.nan],
            "when": pd.to_datetime(["2020-01-02 03:04:05", None]),
            "ghost": pd.Series([False, None], dtype=object),
        },
        geometry=[LineString([(-95, 30), (-94, 30)]), Point(-95, 30)],
        crs="EPSG:4326",
    )
    table = vector_tiles.frame_table(frame)

    path = tmp_path / "layer.ndgeojson"
    maplibre._write_ndgeojson(frame, path)
    back = vector_tiles._freestiler_frame(vector_tiles.read_ndgeojson(path))

    assert table["Name"].to_pylist() == list(back["Name"])
    assert table["Profile_Data"].to_pylist()[0] == back["Profile_Data"].iloc[0]
    assert table["mixed"].to_pylist() == ["text", "4"]
    assert table["depth"].to_pylist() == [1.25, None]
    assert table["when"].to_pylist() == [str(back["when"].iloc[0]), None]
    assert table["ghost"].to_pylist() == [False, None]
    assert table.column_names[-1] == "geometry"


def test_geoparquet_crs_rules():
    def schema(geo):
        return pa.schema([("geometry", pa.binary())], metadata={b"geo": json.dumps(geo)} if geo else None)

    from pyproj import CRS

    projjson = CRS.from_user_input(PA_NORTH).to_json_dict()
    column, crs = vector_tiles.geoparquet_crs(schema({"primary_column": "geometry", "columns": {"geometry": {"crs": projjson}}}))
    assert column == "geometry" and CRS.from_user_input(crs) == CRS.from_user_input(PA_NORTH)
    # An absent crs key is OGC:CRS84 per the GeoParquet spec.
    assert vector_tiles.geoparquet_crs(schema({"primary_column": "geometry", "columns": {"geometry": {}}}))[1] == "EPSG:4326"
    # crs: null is unknown: the fallback, else a refusal.
    unknown = schema({"primary_column": "geometry", "columns": {"geometry": {"crs": None}}})
    with pytest.raises(ValueError, match="no CRS"):
        vector_tiles.geoparquet_crs(unknown)
    assert CRS.from_user_input(vector_tiles.geoparquet_crs(unknown, HARN)[1]) == CRS.from_user_input(HARN)
    with pytest.raises(ValueError, match="no CRS"):
        vector_tiles.geoparquet_crs(schema(None))


def test_python_reprojection_matches_geopandas():
    frame = gpd.GeoDataFrame(
        {"cell_id": [1, 2]},
        geometry=[box(6_300_000, 2_000_000, 6_300_100, 2_000_100), None],
        crs=HARN,
    )
    table = pa.table({"cell_id": [1, 2], "geometry": pa.array(frame.geometry.to_wkb(), pa.binary())})
    out = vector_tiles.reproject_table_to_wgs84(table, vector_tiles.crs_text(HARN))
    expected = frame.to_crs("EPSG:4326")
    got = gpd.GeoSeries.from_wkb(out["geometry"].to_pylist(), crs="EPSG:4326")
    assert got.iloc[1] is None
    assert got.iloc[0].equals_exact(expected.geometry.iloc[0], tolerance=0)


# --------------------------------------------------------------------------
# run_freestiler with Arrow layers
# --------------------------------------------------------------------------


def _arrow_layer(crs: str, rows: int = 2) -> vector_tiles.ArrowLayer:
    geometries = [box(6_300_000 + i * 100, 2_000_000, 6_300_100 + i * 100, 2_000_100) for i in range(rows)]
    table = pa.table(
        {"cell_id": list(range(rows)), "geometry": pa.array([g.wkb for g in geometries], pa.binary())}
    )
    return vector_tiles.ArrowLayer(load=lambda: table, crs=vector_tiles.crs_text(crs))


def test_arrow_layers_go_to_freestile_layers_with_crs_and_buffer(monkeypatch, tmp_path: Path):
    calls = _fake_arrow_freestiler(monkeypatch)
    layers = {
        "cells": _arrow_layer(PA_NORTH),
        "none": _arrow_layer(PA_NORTH, rows=0),
        "extent": gpd.GeoDataFrame(geometry=[box(-77.8, 40.9, -77.7, 41.0)], crs="EPSG:4326"),
    }
    applied = vector_tiles.run_freestiler(tmp_path / "out.pmtiles", layers, 13, 16)

    assert applied == {
        "generate_ids": True,
        "gzip_level": 6,
        "layered": True,
        "input": "arrow",
        "buffer": 5,
        "python_reprojected": [],
    }
    (attempt,) = calls["attempts"]
    assert attempt["options"] == {"gzip_level": 6, "buffer": 5, "generate_ids": True}
    assert [name for name, _, _ in attempt["layers"]] == ["cells", "extent"]
    name, crs, table = attempt["layers"][0]
    # Reprojected in Rust: the table stays in the model CRS.
    assert crs != vector_tiles.WGS84 and "Pennsylvania North" in crs
    assert table.num_rows == 2
    assert (tmp_path / "out.pmtiles").read_bytes() == b"pmtiles"


def test_refused_crs_is_reprojected_in_python(monkeypatch, tmp_path: Path):
    calls = _fake_arrow_freestiler(monkeypatch, refuse=("HARN",))
    layers = {"cells": _arrow_layer(HARN), "faces": _arrow_layer(HARN), "bc": _arrow_layer(PA_NORTH)}

    with pytest.warns(RuntimeWarning, match="reprojected in Python"):
        applied = vector_tiles.run_freestiler(tmp_path / "out.pmtiles", layers, 13, 16)

    assert applied["python_reprojected"] == ["cells", "faces"]
    # One refused attempt, then the whole archive again with HARN in Python.
    assert len(calls["attempts"]) == 2
    final = calls["attempts"][1]["layers"]
    assert [(name, crs) for name, crs, _ in final[:2]] == [("cells", "EPSG:4326"), ("faces", "EPSG:4326")]
    assert "Pennsylvania North" in final[2][1]
    lon, lat = gpd.GeoSeries.from_wkb(final[0][2]["geometry"].to_pylist()).iloc[0].exterior.coords[0]
    assert -123 < lon < -121 and 38 < lat < 40  # northern California

    # The refusal is remembered: the next tileset skips the Rust attempt.
    calls["attempts"].clear()
    vector_tiles.run_freestiler(tmp_path / "again.pmtiles", {"cells": _arrow_layer(HARN)}, 13, 16)
    assert len(calls["attempts"]) == 1


def test_errors_on_wgs84_layers_are_not_retried(monkeypatch, tmp_path: Path):
    calls = _fake_arrow_freestiler(monkeypatch, refuse=("4326",))
    with pytest.raises(ValueError, match="not supported"):
        vector_tiles.run_freestiler(
            tmp_path / "out.pmtiles", {"cells": _arrow_layer("EPSG:4326")}, 13, 16
        )
    assert len(calls["attempts"]) == 1
    assert not (tmp_path / "out.pmtiles").exists()


def test_all_empty_arrow_layers_are_refused(monkeypatch, tmp_path: Path):
    _fake_arrow_freestiler(monkeypatch)

    def refuse_empty(pairs, output, **kwargs):
        if not list(pairs):
            raise ValueError("at least one layer is required")

    sys.modules["freestiler"].freestile_layers = refuse_empty
    with pytest.raises(ValueError, match="non-empty source layer"):
        vector_tiles.run_freestiler(tmp_path / "a.pmtiles", {"a": _arrow_layer(PA_NORTH, rows=0)}, 10, 14)


# --------------------------------------------------------------------------
# viewer and pmtiles wiring
# --------------------------------------------------------------------------


def _projected_archive(tmp_path: Path) -> Path:
    from shapely.affinity import translate

    cells = [translate(box(0, 0, 100, 100), 1_970_000 + 100 * (i % 50), 300_000 + 100 * (i // 50)) for i in range(500)]
    faces = [LineString(list(cell.exterior.coords)[:2]) for cell in cells]
    frame = gpd.GeoDataFrame(
        {
            "layer": ["mesh_cells"] * 500 + ["mesh_faces"] * 500 + ["bc_lines"],
            "mesh_name": ["Mesh"] * 1000 + [None],
            "cell_id": list(range(500)) + [None] * 501,
            "face_id": [None] * 500 + list(range(500)) + [None],
            "Name": [None] * 1000 + ["Upstream"],
        },
        geometry=cells + faces + [LineString([(1_970_000, 300_000), (1_975_000, 300_000)])],
        crs=PA_NORTH,
    )
    path = tmp_path / "model.g01.parquet"
    frame.to_parquet(path)
    return path


def test_dense_arrow_metadata_matches_the_ndgeojson_writer(tmp_path: Path):
    source = _projected_archive(tmp_path)
    for kind in ("mesh_cells", "mesh_faces"):
        old_sizes: list[np.ndarray] = []
        new_sizes: list[np.ndarray] = []
        old = maplibre._stream_dense_layer_ndgeojson(
            source, kind, kind, tmp_path / f"{kind}.ndgeojson", None, batch_size=300, pixel_sizes=old_sizes
        )
        layer, *new = maplibre._dense_layer_arrow(source, kind, kind, None, batch_size=300, pixel_sizes=new_sizes)
        assert tuple(new) == old
        np.testing.assert_array_equal(np.concatenate(new_sizes), np.concatenate(old_sizes))
        table = layer.load()
        assert table.num_rows == 500
        assert set(table.column_names) == {"geometry", "mesh_name", "cell_id" if kind == "mesh_cells" else "face_id"}
        assert "Pennsylvania North" in layer.crs


def test_viewer_arrow_mode_writes_no_ndgeojson(monkeypatch, tmp_path: Path):
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    _projected_archive(archive_dir)
    manifest = {
        "project": {"name": "Synthetic"},
        "geometry": [
            {
                "geom_id": "g01",
                "parquet": "model.g01.parquet",
                "layers": [
                    {"layer": "mesh_cells", "filter_value": "mesh_cells"},
                    {"layer": "mesh_faces", "filter_value": "mesh_faces"},
                    {"layer": "bc_lines", "filter_value": "bc_lines"},
                ],
            }
        ],
    }
    (archive_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    hdf = tmp_path / "model.g01.hdf"
    hdf.touch()
    footprint = gpd.GeoDataFrame(geometry=[box(-77.8, 40.9, -77.7, 41.0)], crs="EPSG:4326")
    monkeypatch.setattr(maplibre, "_extent_from_hdf", lambda *_: footprint)
    monkeypatch.setattr(maplibre, "_require_cli", lambda _: None)

    def package(name: str, tiler):
        monkeypatch.setattr(maplibre, "_run_vector_tiler", tiler)
        summary = maplibre.package_maplibre_viewer(
            archive_dir, tmp_path / name, geometry_hdfs={"g01": hdf}, scratch_dir=tmp_path / f"{name}-scratch"
        )
        return json.loads(summary.manifest_path.read_text(encoding="utf-8"))

    def fake_tiler(output, layers, min_zoom, max_zoom, temporary_directory=None):
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"pmtiles")

    monkeypatch.setenv("RAS2CNG_VECTOR_TILER", "tippecanoe")
    geojson_manifest = package("geojson", fake_tiler)

    _fake_arrow_freestiler(monkeypatch)
    monkeypatch.setenv("RAS2CNG_VECTOR_TILER", "freestiler")
    seen: list = []

    def arrow_tiler(output, layers, min_zoom, max_zoom, temporary_directory=None):
        assert not list((tmp_path / "arrow-scratch").rglob("*.ndgeojson"))
        seen.append((output.name, list(layers), min_zoom))
        assert all(isinstance(source, vector_tiles.ArrowLayer) for _, source in layers)
        fake_tiler(output, layers, min_zoom, max_zoom)

    arrow_manifest = package("arrow", arrow_tiler)

    def layer_metadata(manifest):
        return {
            layer["id"]: (layer["featureCount"], layer["geometryTypes"], layer["bounds"])
            for tileset in manifest["tilesets"]
            for layer in tileset["layers"]
        }

    assert layer_metadata(arrow_manifest) == layer_metadata(geojson_manifest)
    detail = next(layers for name, layers, _ in seen if name == "geometry-detail.pmtiles")
    assert all(vector_tiles.WGS84 != source.crs for _, source in detail)
    overview = next(layers for name, layers, _ in seen if name == "geometry.pmtiles")
    assert all(source.crs == vector_tiles.WGS84 for _, source in overview)


def test_dense_layer_with_no_crs_is_refused(tmp_path: Path):
    frame = gpd.GeoDataFrame(
        {"layer": ["mesh_cells"], "cell_id": [1]}, geometry=[box(0, 0, 1, 1)], crs=None
    )
    frame.to_parquet(tmp_path / "nocrs.parquet")
    with pytest.raises(ValueError, match="no CRS"):
        maplibre._dense_layer_arrow(tmp_path / "nocrs.parquet", "mesh_cells", "mesh_cells", None)


def test_pmtiles_cli_geoarrow_encoded_file_takes_the_geodataframe_path(monkeypatch, tmp_path: Path):
    from ras2cng import pmtiles

    calls = _fake_arrow_freestiler(monkeypatch)
    monkeypatch.setenv("RAS2CNG_VECTOR_TILER", "freestiler")
    frame = gpd.GeoDataFrame({"a": [1]}, geometry=[Point(-77.7, 41.0)], crs="EPSG:4326")
    frame.to_parquet(tmp_path / "native.parquet", geometry_encoding="geoarrow")
    assert pmtiles._arrow_layer(tmp_path / "native.parquet") is None

    def fake_freestile(frames, output, *, buffer=None, **kwargs):
        kwargs["buffer"] = buffer
        calls["frames"] = (frames, kwargs)
        Path(output).write_bytes(b"pmtiles")

    sys.modules["freestiler"].freestile = fake_freestile
    pmtiles.generate_vector_pmtiles(tmp_path / "native.parquet", tmp_path / "out.pmtiles", layer_name="mesh")
    frames, kwargs = calls["frames"]
    assert list(frames) == ["mesh"]
    assert kwargs["buffer"] == vector_tiles.TILE_BUFFER == 5
    assert "drop_rate" not in kwargs


def test_pmtiles_cli_arrow_layer(monkeypatch, tmp_path: Path):
    from ras2cng import pmtiles

    calls = _fake_arrow_freestiler(monkeypatch)
    monkeypatch.setenv("RAS2CNG_VECTOR_TILER", "freestiler")
    source = _projected_archive(tmp_path)
    pmtiles.generate_vector_pmtiles(source, tmp_path / "out.pmtiles", layer_name="mesh")
    (attempt,) = calls["attempts"]
    ((name, crs, table),) = attempt["layers"]
    assert name == "mesh" and "Pennsylvania North" in crs
    assert table.num_rows == 1001 and "layer" in table.column_names

    no_crs = gpd.GeoDataFrame({"a": [1]}, geometry=[Point(0, 0)], crs=None)
    no_crs.to_parquet(tmp_path / "nocrs.parquet")
    with pytest.raises(ValueError, match="no CRS"):
        pmtiles.generate_vector_pmtiles(tmp_path / "nocrs.parquet", tmp_path / "x.pmtiles")


# --------------------------------------------------------------------------
# the real engine (skipped without a freestiler that has Arrow input)
# --------------------------------------------------------------------------


def _real_freestiler():
    freestiler = pytest.importorskip("freestiler")
    if not vector_tiles.freestiler_capabilities(freestiler)["arrow"]:
        pytest.skip("installed freestiler has no Arrow input")
    return freestiler


def _decoded(path: Path) -> list:
    """Every tile's decoded layers, as sorted (layer, geometry, properties) tuples."""

    import gzip

    from pmtiles.reader import MmapSource, Reader, all_tiles

    out = []
    with open(path, "rb") as handle:
        for key, data in all_tiles(Reader(MmapSource(handle)).get_bytes):
            out.append((key, gzip.decompress(data) if data[:2] == b"\x1f\x8b" else data))
    return sorted(out)


def test_real_arrow_path_matches_the_geodataframe_path(tmp_path: Path):
    _real_freestiler()
    pytest.importorskip("pmtiles")
    source = _projected_archive(tmp_path)
    frame = gpd.read_parquet(source, filters=[("layer", "==", "mesh_cells")])[["mesh_name", "cell_id", "geometry"]]
    frame = frame.to_crs("EPSG:4326")

    vector_tiles.run_freestiler(tmp_path / "frame.pmtiles", {"cells": frame, "x": frame.iloc[:1]}, 12, 15)
    layer, *_ = maplibre._dense_layer_arrow(source, "mesh_cells", "mesh_cells", None)
    wgs84 = vector_tiles.write_arrow_spill(vector_tiles.frame_table(frame.iloc[:1]), tmp_path / "x.arrow")
    vector_tiles.run_freestiler(tmp_path / "arrow.pmtiles", {"cells": layer, "x": wgs84}, 12, 15)

    assert _decoded(tmp_path / "arrow.pmtiles") == _decoded(tmp_path / "frame.pmtiles")


def test_real_harn_layer_falls_back_to_python(tmp_path: Path):
    _real_freestiler()
    from shapely.affinity import translate

    cells = [translate(box(0, 0, 100, 100), 6_630_000 + 100 * i, 1_960_000) for i in range(20)]
    table = pa.table({"cell_id": list(range(20)), "geometry": pa.array([c.wkb for c in cells], pa.binary())})
    layer = vector_tiles.ArrowLayer(load=lambda: table, crs=vector_tiles.crs_text(HARN))

    with pytest.warns(RuntimeWarning, match="reprojected in Python"):
        applied = vector_tiles.run_freestiler(tmp_path / "harn.pmtiles", {"cells": layer}, 12, 15)
    assert applied["python_reprojected"] == ["cells"]
    assert (tmp_path / "harn.pmtiles").stat().st_size > 0
