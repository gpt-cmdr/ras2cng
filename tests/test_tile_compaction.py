"""Sparse-tileset options of the freestiler fork build after 0.3.0.3
(gpt-cmdr/freestiler #11): ``deduplicate``, ``sort_values`` and per-zoom
``line_simplify``; the tippecanoe drop rate; columns kept out of tiles.

Every option is detected from the installed freestiler's signatures, so an
older or PyPI build is never passed one.
"""

from __future__ import annotations

from pathlib import Path

import geopandas as gpd
import numpy as np
import pyarrow as pa
import pytest
from shapely.geometry import LineString, Point, box
from typer.testing import CliRunner

from ras2cng import maplibre, simplify, vector_tiles


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in (vector_tiles.COMPACT_ENV, vector_tiles.SORT_VALUES_ENV,
                 simplify.TILE_LINE_SIMPLIFY_ENV,
                 simplify.TILE_POLYGON_SIMPLIFY_ENV, vector_tiles.KEEP_SHORT_LINES_ENV,
                 "RAS2CNG_VECTOR_TILER", vector_tiles.INPUT_ENV):
        monkeypatch.delenv(name, raising=False)


def _line_table() -> pa.Table:
    import shapely

    wkb = shapely.to_wkb(LineString([(-95, 30), (-94.95, 30.02), (-94.9, 30.1)]))
    return pa.table({"geometry": pa.array([wkb], type=pa.binary()), "Name": ["a"]})


def _layer(kind, **options):
    return vector_tiles.ArrowLayer(load=_line_table, crs=vector_tiles.WGS84, kind=kind, **options)


class _Fake:
    """The freestiler API the tilers use, with or without the #11 options."""

    def __init__(self, *, new: bool):
        self.layers: dict[str, dict] = {}
        self.kwargs: dict = {}
        self.calls: list[str] = []

        def layer_new(input, crs=None, geometry_column="geometry", polygon_simplify=None,
                      keep_short_lines=False, line_simplify=None):
            return {"line_simplify": line_simplify, "keep_short_lines": keep_short_lines,
                    "polygon_simplify": polygon_simplify}

        def layer_old(input, crs=None, geometry_column="geometry", polygon_simplify=None,
                      keep_short_lines=False):
            return {"keep_short_lines": keep_short_lines}

        def layers_new(pairs, output, *, min_zoom=0, max_zoom=14, quiet=False, generate_ids=True,
                       gzip_level=None, buffer=None, deduplicate=False, sort_values=False):
            self._layered(pairs, output, locals())

        def layers_old(pairs, output, *, min_zoom=0, max_zoom=14, quiet=False, generate_ids=True,
                       gzip_level=None, buffer=None):
            self._layered(pairs, output, locals())

        def one_new(frames, output, *, min_zoom=0, max_zoom=14, quiet=False, generate_ids=True,
                    gzip_level=None, buffer=None, deduplicate=False, sort_values=False):
            self._single(output, locals())

        def one_old(frames, output, *, min_zoom=0, max_zoom=14, quiet=False, generate_ids=True,
                    gzip_level=None, buffer=None):
            self._single(output, locals())

        self.FreestileLayer = layer_new if new else layer_old
        self.freestile_layers = layers_new if new else layers_old
        self.freestile = one_new if new else one_old
        self._LayeredTiler = type("_LayeredTiler", (), {"add_arrow_layer": lambda *a, **k: None})
        self.FreestileLayer.__dataclass_fields__ = {"crs": None}

    def _layered(self, pairs, output, scope):
        self.calls.append("freestile_layers")
        self.kwargs = {k: v for k, v in scope.items() if k not in ("self", "pairs", "output")}
        for name, layer in pairs:
            self.layers[name] = layer
        Path(output).write_bytes(b"pmtiles")

    def _single(self, output, scope):
        self.calls.append("freestile")
        self.kwargs = {k: v for k, v in scope.items() if k not in ("self", "frames", "output")}
        Path(output).write_bytes(b"pmtiles")


def _install(monkeypatch, fake):
    import sys

    monkeypatch.setitem(sys.modules, "freestiler", fake)
    monkeypatch.setattr(vector_tiles, "freestiler_available", lambda: True)
    monkeypatch.setattr(vector_tiles, "require_freestiler", lambda: None)
    return fake


def _frame() -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame({"a": [1]}, geometry=[box(-95, 30, -94.9, 30.1)], crs="EPSG:4326")


# ---------------------------------------------------------------------------
# Capabilities
# ---------------------------------------------------------------------------


def test_capabilities_follow_the_signatures():
    new = vector_tiles.freestiler_capabilities(_Fake(new=True))
    old = vector_tiles.freestiler_capabilities(_Fake(new=False))
    names = ("line_simplify", "deduplicate", "layers_deduplicate", "sort_values", "layers_sort_values")
    assert all(new[name] for name in names)
    assert not any(old[name] for name in names)
    assert old["keep_short_lines"] is True, "0.3.0.3 keeps its own options"


# ---------------------------------------------------------------------------
# deduplicate / sort_values
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("layers", "sorted_values"), [
    ({"lines": _layer("breaklines")}, True),               # Arrow input, sparse
    ({"lines": _layer("breaklines"), "faces": _layer("mesh_faces")}, False),  # a mesh layer
    ({"cells": _frame(), "faces": _frame()}, False),       # layered GeoDataFrames: kind unknown
    ({"cells": _frame()}, False),                          # single freestile(): kind unknown
])
def test_deduplicate_everywhere_and_sort_values_only_without_mesh_layers(
    monkeypatch, tmp_path, layers, sorted_values
):
    fake = _install(monkeypatch, _Fake(new=True))
    applied = vector_tiles.run_freestiler(tmp_path / "o.pmtiles", layers, 0, 14)
    assert fake.kwargs["deduplicate"] is True and applied["deduplicate"] is True
    assert fake.kwargs["sort_values"] is sorted_values
    assert applied["sort_values"] is sorted_values


def test_sort_values_rule_and_its_override(monkeypatch):
    sparse = {"bl": _layer("breaklines"), "bc": _layer("bc_lines")}
    assert vector_tiles.sort_values_for(sparse) is True
    assert vector_tiles.sort_values_for({**sparse, "cells": _layer("mesh_cells")}) is False
    assert vector_tiles.sort_values_for({**sparse, "x": _layer(None)}) is False
    assert vector_tiles.sort_values_for({**sparse, "frame": _frame()}) is False
    assert vector_tiles.sort_values_for({}) is False
    monkeypatch.setenv(vector_tiles.SORT_VALUES_ENV, "1")
    assert vector_tiles.sort_values_for({"cells": _layer("mesh_cells")}) is True
    monkeypatch.setenv(vector_tiles.SORT_VALUES_ENV, "0")
    assert vector_tiles.sort_values_for(sparse) is False
    monkeypatch.setenv(vector_tiles.SORT_VALUES_ENV, "sometimes")
    with pytest.raises(ValueError, match=vector_tiles.SORT_VALUES_ENV):
        vector_tiles.sort_values_for(sparse)


@pytest.mark.parametrize("layers", [
    {"lines": _layer("breaklines")},
    {"cells": _frame(), "faces": _frame()},
    {"cells": _frame()},
])
def test_an_older_freestiler_is_not_passed_them(monkeypatch, tmp_path, layers):
    fake = _install(monkeypatch, _Fake(new=False))
    applied = vector_tiles.run_freestiler(tmp_path / "o.pmtiles", layers, 0, 14)
    assert "deduplicate" not in fake.kwargs and "sort_values" not in fake.kwargs
    assert "deduplicate" not in applied and "sort_values" not in applied


def test_compaction_can_be_turned_off(monkeypatch, tmp_path):
    fake = _install(monkeypatch, _Fake(new=True))
    monkeypatch.setenv(vector_tiles.COMPACT_ENV, "0")
    applied = vector_tiles.run_freestiler(tmp_path / "o.pmtiles", {"l": _layer("breaklines")}, 0, 14)
    assert fake.kwargs["deduplicate"] is False and fake.kwargs["sort_values"] is False
    assert "deduplicate" not in applied
    assert vector_tiles.compaction_summary() == {
        "deduplicate": False, "sortValues": False, "reason": "turned off",
    }
    with vector_tiles.vector_tile_options(compact=True):
        assert vector_tiles.compact_enabled()
        assert vector_tiles.compaction_summary() == {
            "deduplicate": True, "sortValues": True,
            "sortValuesScope": "tilesets without mesh_cells or mesh_faces layers",
        }
    monkeypatch.setenv(vector_tiles.COMPACT_ENV, "maybe")
    with pytest.raises(ValueError, match=vector_tiles.COMPACT_ENV):
        vector_tiles.compact_enabled()


def test_summary_says_why_an_older_build_does_not_compact(monkeypatch):
    _install(monkeypatch, _Fake(new=False))
    summary = vector_tiles.compaction_summary()
    assert summary["deduplicate"] is False and "no deduplicate" in summary["reason"]


def test_manifest_archive_record(monkeypatch):
    _install(monkeypatch, _Fake(new=True))
    monkeypatch.setenv("RAS2CNG_VECTOR_TILER", "freestiler")
    record = maplibre._vector_archive_summary()
    assert record == {
        "deduplicate": True, "sortValues": True,
        "sortValuesScope": "tilesets without mesh_cells or mesh_faces layers",
        "archiveOnlyColumns": ["Terrain_Profiles"],
    }
    monkeypatch.setenv("RAS2CNG_VECTOR_TILER", "tippecanoe")
    record = maplibre._vector_archive_summary()
    assert record["deduplicate"] is False and "tippecanoe" in record["reason"]


# ---------------------------------------------------------------------------
# line_simplify
# ---------------------------------------------------------------------------


def test_line_setting_default_env_and_options(monkeypatch):
    assert simplify.tile_line_pixels_setting() == simplify.DEFAULT_TILE_LINE_SIMPLIFY_PX == 1.0
    assert simplify.tile_line_pixels("breaklines") == 1.0
    monkeypatch.setenv(simplify.TILE_LINE_SIMPLIFY_ENV, "0.5")
    assert simplify.tile_line_pixels("bc_lines") == 0.5
    monkeypatch.setenv(simplify.TILE_LINE_SIMPLIFY_ENV, "off")
    assert simplify.tile_line_pixels("bc_lines") is None
    monkeypatch.delenv(simplify.TILE_LINE_SIMPLIFY_ENV)
    with simplify.simplify_options(tile_line_pixels=2.0):
        assert simplify.tile_line_pixels("breaklines") == 2.0
    with simplify.simplify_options(tile_line_pixels_enabled=False):
        assert simplify.tile_line_pixels("breaklines") is None
        assert simplify.tile_polygon_pixels("land_cover") == 0.5, "only the line option is off"
    with simplify.simplify_options(enabled=False):  # --no-simplify
        assert simplify.tile_line_pixels("breaklines") is None
    monkeypatch.setenv(simplify.TILE_LINE_SIMPLIFY_ENV, "65")
    with pytest.raises(ValueError, match="tile pixels"):
        simplify.tile_line_pixels_setting()


@pytest.mark.parametrize("kind", ["mesh_faces", "mesh_cells", None, ""])
def test_mesh_layers_never_get_line_simplify(kind):
    assert simplify.tile_line_pixels(kind) is None
    with pytest.raises(ValueError, match="line_simplify cannot be set"):
        _layer(kind, line_simplify=1.0)


def test_only_layers_that_carry_it_are_passed_it(monkeypatch, tmp_path):
    fake = _Fake(new=True)
    layers = {
        "breaklines": _layer("breaklines", line_simplify=1.0),
        "bc": _layer("bc_lines", line_simplify=1.0),
        "faces": _layer("mesh_faces"),
        "cells": _layer("mesh_cells"),
        "plain": _layer("reference_lines"),
    }
    vector_tiles._run_freestiler_arrow(fake, tmp_path / "o.pmtiles", layers, {}, {})
    assert {name: layer["line_simplify"] for name, layer in fake.layers.items()} == {
        "breaklines": 1.0, "bc": 1.0, "faces": None, "cells": None, "plain": None,
    }
    assert fake.layers["breaklines"]["keep_short_lines"] is True, "keep_short_lines still applies"
    assert fake.layers["faces"]["keep_short_lines"] is True


def test_an_older_freestiler_is_not_passed_line_simplify(tmp_path):
    fake = _Fake(new=False)
    vector_tiles._run_freestiler_arrow(
        fake, tmp_path / "o.pmtiles", {"breaklines": _layer("breaklines", line_simplify=1.0)}, {}, {}
    )
    assert fake.layers["breaklines"] == {"keep_short_lines": True}


def test_delivery_source_sets_it_on_non_mesh_line_layers_only(monkeypatch, tmp_path):
    monkeypatch.setattr(vector_tiles, "line_simplify_available", lambda: True)
    monkeypatch.setattr(vector_tiles, "polygon_simplify_available", lambda: True)
    lines = gpd.GeoDataFrame({"n": [1]}, geometry=[LineString([(-95, 30), (-94.9, 30.1)])], crs="EPSG:4326")
    polygons = gpd.GeoDataFrame({"n": [1]}, geometry=[box(-95, 30, -94.9, 30.1)], crs="EPSG:4326")
    points = gpd.GeoDataFrame({"n": [1]}, geometry=[Point(-95, 30)], crs="EPSG:4326")

    def deliver(frame, kind):
        return maplibre._write_delivery_source(frame, tmp_path / f"{kind}.ndgeojson", True, kind=kind)[0]

    assert deliver(lines, "breaklines").line_simplify == 1.0
    assert deliver(lines, "pipe_conduits").line_simplify == 1.0
    assert deliver(lines, "mesh_faces").line_simplify is None
    assert deliver(polygons, "land_cover").line_simplify is None
    assert deliver(polygons, "land_cover").polygon_simplify == 0.5
    assert deliver(points, "pipe_nodes").line_simplify is None
    with simplify.simplify_options(enabled=False):
        assert deliver(lines, "breaklines").line_simplify is None
    monkeypatch.setattr(vector_tiles, "line_simplify_available", lambda: False)
    assert deliver(lines, "breaklines").line_simplify is None


def test_results_joined_to_lines_follow_the_same_rule(monkeypatch):
    monkeypatch.setattr(vector_tiles, "line_simplify_available", lambda: True)
    assert maplibre._joined_line_pixels("reference_lines", ["LineString"]) == 1.0
    assert maplibre._joined_line_pixels("mesh_faces", ["LineString"]) is None
    assert maplibre._joined_line_pixels("mesh_cells", ["Polygon"]) is None
    assert maplibre._joined_line_pixels("reference_points", ["Point"]) is None
    monkeypatch.setattr(vector_tiles, "line_simplify_available", lambda: False)
    assert maplibre._joined_line_pixels("reference_lines", ["LineString"]) is None


def test_manifest_line_summary_records_layers_or_the_reason(monkeypatch):
    monkeypatch.setattr(vector_tiles, "line_simplify_available", lambda: True)
    sources = [
        ("bl", _layer("breaklines", line_simplify=1.0)),
        ("faces", _layer("mesh_faces")),
    ]
    summary = maplibre._line_simplify_summary(sources)
    assert summary["enabled"] is True and summary["layers"] == ["bl"]
    assert summary["tilePixels"] == 1.0 and "Douglas-Peucker" in summary["method"]
    assert maplibre._vector_simplification_summary(sources)["lines"] == summary

    assert "mesh_faces" in maplibre._line_simplify_summary([("faces", _layer("mesh_faces"))])["reason"]
    assert "Arrow" in maplibre._line_simplify_summary([("bl", Path("bl.ndgeojson"))])["reason"]
    with simplify.simplify_options(tile_line_pixels_enabled=False):
        assert maplibre._line_simplify_summary(sources[1:]) == {"enabled": False, "reason": "turned off"}
    monkeypatch.setattr(vector_tiles, "line_simplify_available", lambda: False)
    assert "no line_simplify" in maplibre._line_simplify_summary(sources[1:])["reason"]


def test_cli_passes_the_line_and_compact_options(monkeypatch, tmp_path):
    from ras2cng.cli import app

    seen = {}

    def fake_package(archive_dir, output, **kwargs):
        seen.update(
            line=simplify.tile_line_pixels_setting(), polygon=simplify.tile_pixels_setting(),
            compact=vector_tiles.compact_enabled(),
        )
        return {"manifest": str(tmp_path / "viewer.json")}

    monkeypatch.setattr(maplibre, "package_maplibre_viewer", fake_package)
    hdf = tmp_path / "g.hdf"
    hdf.write_bytes(b"")
    base = ["maplibre", str(tmp_path), str(tmp_path / "out"), "--geometry-hdf", f"g01={hdf}"]
    CliRunner().invoke(app, base)
    assert seen == {"line": 1.0, "polygon": 0.5, "compact": True}
    CliRunner().invoke(app, [*base, "--tile-line-simplify", "0.5", "--no-compact-tiles"])
    assert seen == {"line": 0.5, "polygon": 0.5, "compact": False}
    CliRunner().invoke(app, [*base, "--no-tile-line-simplify"])
    assert seen["line"] is None and seen["polygon"] == 0.5
    CliRunner().invoke(app, [*base, "--no-simplify"])
    assert seen["line"] is None and seen["polygon"] is None


# ---------------------------------------------------------------------------
# Columns kept out of tiles; the tippecanoe drop rate
# ---------------------------------------------------------------------------


def test_terrain_profiles_stay_in_the_archive(tmp_path):
    profile = np.array([np.array([0.0, 51.48], dtype="float32"), np.array([21.3, 51.44], dtype="float32")],
                       dtype=object)
    conduits = gpd.GeoDataFrame(
        {"conduit_id": [7], "Name": ["C7"], "Terrain_Profiles": [profile], "layer": ["pipe_conduits"]},
        geometry=[LineString([(-121.7, 38.5), (-121.69, 38.51)])], crs="EPSG:4326",
    )
    assert "Terrain_Profiles" in maplibre.ARCHIVE_ONLY_COLUMNS
    cleaned = maplibre._drop_internal_columns(conduits)
    assert list(cleaned.columns) == ["conduit_id", "Name", "geometry"]

    count, _, _ = maplibre._write_ndgeojson(conduits, tmp_path / "c.ndgeojson")
    text = (tmp_path / "c.ndgeojson").read_text(encoding="utf-8")
    assert count == 1 and "Terrain_Profiles" not in text and "array(" not in text

    layer, *_ = maplibre._write_delivery_source(conduits, tmp_path / "c2.ndgeojson", True, kind="pipe_conduits")
    assert layer.load().column_names == ["conduit_id", "Name", "geometry"]


def test_pmtiles_cli_tippecanoe_keeps_every_point(monkeypatch, tmp_path):
    from ras2cng import pmtiles

    commands = []

    def fake_run(command, **kwargs):
        commands.append(command)
        Path(command[command.index("-o") + 1]).write_bytes(b"pmtiles")
        return type("Done", (), {"stderr": "", "stdout": "", "returncode": 0})()

    monkeypatch.setenv("RAS2CNG_VECTOR_TILER", "tippecanoe")
    monkeypatch.setattr(pmtiles, "_require_cli", lambda command: None)
    monkeypatch.setattr(pmtiles.subprocess, "run", fake_run)
    source = tmp_path / "points.parquet"
    gpd.GeoDataFrame({"n": [1, 2]}, geometry=[Point(-95, 30), Point(-94.9, 30.1)], crs="EPSG:4326").to_parquet(source)
    pmtiles.generate_vector_pmtiles(source, tmp_path / "points.pmtiles", layer_name="nodes")
    assert "--drop-rate=1" in commands[0]


# ---------------------------------------------------------------------------
# The real engine
# ---------------------------------------------------------------------------


def _real_capability(name: str) -> bool:
    try:
        import freestiler

        return bool(vector_tiles.freestiler_capabilities(freestiler)[name])
    except Exception:
        return False


def _varint(data: bytes, pos: int) -> tuple[int, int]:
    value = shift = 0
    while True:
        byte = data[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, pos
        shift += 7


def _fields(data: bytes):
    """``(field number, wire type, value)`` of one protobuf message."""
    import struct

    pos = 0
    while pos < len(data):
        key, pos = _varint(data, pos)
        number, wire = key >> 3, key & 7
        if wire == 0:
            value, pos = _varint(data, pos)
        elif wire == 2:
            size, pos = _varint(data, pos)
            value, pos = data[pos:pos + size], pos + size
        elif wire == 1:
            value, pos = struct.unpack_from("<d", data, pos)[0], pos + 8
        elif wire == 5:
            value, pos = struct.unpack_from("<f", data, pos)[0], pos + 4
        else:  # pragma: no cover - not used by MVT
            raise ValueError(f"wire type {wire}")
        yield number, wire, value


def _mvt_value(data: bytes):
    for number, _, value in _fields(data):
        if number == 1:
            return value.decode("utf-8")
        if number == 6:  # sint64, zigzag
            return (value >> 1) ^ -(value & 1)
        return value
    return None


def _packed(data: bytes) -> list[int]:
    out, pos = [], 0
    while pos < len(data):
        value, pos = _varint(data, pos)
        out.append(value)
    return out


def _tile_layers(path: Path) -> dict:
    """``{(z, x, y): {layer name: [feature properties]}}`` of a PMTiles archive.

    A minimal Mapbox Vector Tile reader, so the test needs no decoder package.
    """
    import gzip

    from pmtiles.reader import MmapSource, Reader, all_tiles

    out = {}
    with path.open("rb") as handle:
        for key, data in all_tiles(Reader(MmapSource(handle)).get_bytes):
            tile = gzip.decompress(data) if data[:2] == b"\x1f\x8b" else data
            layers = {}
            for number, _, layer in _fields(tile):
                if number != 3:
                    continue
                name, keys, values, features = "", [], [], []
                for field, _, value in _fields(layer):
                    if field == 1:
                        name = value.decode("utf-8")
                    elif field == 2:
                        features.append(value)
                    elif field == 3:
                        keys.append(value.decode("utf-8"))
                    elif field == 4:
                        values.append(_mvt_value(value))
                decoded = []
                for feature in features:
                    tags = next((_packed(v) for f, _, v in _fields(feature) if f == 2), [])
                    decoded.append({keys[tags[i]]: values[tags[i + 1]] for i in range(0, len(tags), 2)})
                layers[name] = decoded
            out[tuple(key)] = layers
    return out


@pytest.mark.skipif(
    not (_real_capability("line_simplify") and _real_capability("layers_deduplicate")),
    reason="needs a freestiler with deduplicate, sort_values and line_simplify",
)
def test_real_sparse_tileset_is_smaller_and_keeps_every_breakline(tmp_path, monkeypatch):
    import shapely

    monkeypatch.setenv("RAS2CNG_VECTOR_TILER", "freestiler")
    rng = np.random.default_rng(7)
    lines = []
    for index in range(40):
        x0, y0 = -95.0 + 0.004 * (index % 8), 30.0 + 0.004 * (index // 8)
        steps = rng.normal(0, 2e-6, size=(400, 2)).cumsum(axis=0)
        ramp = np.column_stack([np.linspace(0, 0.003, 400), np.linspace(0, 0.001, 400)])
        lines.append(LineString(np.array([x0, y0]) + ramp + steps))
    extent = box(-95.01, 29.99, -94.96, 30.03)

    def table(geometries, **columns):
        wkb = pa.array(shapely.to_wkb(np.array(geometries, dtype=object)), type=pa.binary())
        return pa.table({**columns, "geometry": wkb})

    def layers(line_px):
        return {
            "breaklines": vector_tiles.ArrowLayer(
                load=lambda: table(lines, bl_id=list(range(40))), crs=vector_tiles.WGS84,
                kind="breaklines", line_simplify=line_px,
            ),
            "extent": vector_tiles.ArrowLayer(
                load=lambda: table([extent], Name=["model"]), crs=vector_tiles.WGS84,
                kind="model_extents", polygon_simplify=0.5,
            ),
        }

    with vector_tiles.vector_tile_options(compact=False, feature_ids=True):
        vector_tiles.run_freestiler(tmp_path / "before.pmtiles", layers(None), 0, 15)
    with vector_tiles.vector_tile_options(feature_ids=True):
        applied = vector_tiles.run_freestiler(tmp_path / "after.pmtiles", layers(1.0), 0, 15)
    assert applied["deduplicate"] is True and applied["sort_values"] is True
    before, after = (tmp_path / "before.pmtiles").stat().st_size, (tmp_path / "after.pmtiles").stat().st_size
    assert after < before, (before, after)

    old, new = _tile_layers(tmp_path / "before.pmtiles"), _tile_layers(tmp_path / "after.pmtiles")
    assert set(new) == set(old), "every tile is still addressed"

    def ids_by_zoom(tiles):
        found: dict[int, set] = {}
        for (zoom, *_), decoded in tiles.items():
            features = decoded.get("breaklines") or []
            found.setdefault(zoom, set()).update(feature["bl_id"] for feature in features)
        return found

    kept = ids_by_zoom(new)
    assert kept == ids_by_zoom(old)
    assert all(kept[zoom] == set(range(40)) for zoom in range(16)), "every breakline at every zoom"
