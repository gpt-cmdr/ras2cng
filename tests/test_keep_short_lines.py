"""The tiler's keep_short_lines: which layers get it, the off switches, older freestiler."""

from __future__ import annotations

import inspect
from pathlib import Path

import pyarrow as pa
import pytest
from typer.testing import CliRunner

from ras2cng import maplibre, vector_tiles


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv(vector_tiles.KEEP_SHORT_LINES_ENV, raising=False)


def _table() -> pa.Table:
    import shapely

    wkb = shapely.to_wkb(shapely.LineString([(-95, 30), (-94.9, 30.1)]))
    return pa.table({"geometry": pa.array([wkb], type=pa.binary()), "Name": ["a"]})


def _layer(kind):
    return vector_tiles.ArrowLayer(load=_table, crs=vector_tiles.WGS84, kind=kind)


class _Fake:
    """The parts of the freestiler API the Arrow path uses, recording each layer's options."""

    def __init__(self, *, has_option: bool):
        self.seen: dict[str, dict] = {}

        def layer_with(input, crs=None, geometry_column="geometry", polygon_simplify=None, keep_short_lines=False):
            return {"keep_short_lines": keep_short_lines}

        def layer_without(input, crs=None, geometry_column="geometry", polygon_simplify=None):
            return {}

        self.FreestileLayer = layer_with if has_option else layer_without

    def freestile(self, *args, **kwargs):  # pragma: no cover - only inspected
        raise AssertionError

    def freestile_layers(self, pairs, output, **kwargs):
        for name, layer in pairs:
            self.seen[name] = layer
        Path(output).write_bytes(b"x")


def _run(fake, tmp_path, kinds):
    layers = {str(kind): _layer(kind) for kind in kinds}
    vector_tiles._run_freestiler_arrow(fake, tmp_path / "out.pmtiles", layers, {}, {})
    return fake.seen


def test_the_set_is_faces_and_breaklines():
    assert vector_tiles.KEEP_SHORT_LINES_KINDS == {"mesh_faces", "breaklines"}


def test_only_faces_and_breaklines_get_it(tmp_path):
    seen = _run(
        _Fake(has_option=True),
        tmp_path,
        ["mesh_faces", "breaklines", "mesh_cells", "pipe_conduits", "bc_lines", "reference_lines", None],
    )
    assert {name for name, layer in seen.items() if layer.get("keep_short_lines")} == {"mesh_faces", "breaklines"}


def test_older_freestiler_is_not_passed_the_option(tmp_path):
    fake = _Fake(has_option=False)
    assert vector_tiles.freestiler_capabilities(fake)["keep_short_lines"] is False
    assert _run(fake, tmp_path, ["mesh_faces", "breaklines"]) == {"mesh_faces": {}, "breaklines": {}}


def test_capability_check_matches_the_signature():
    fake = _Fake(has_option=True)
    assert vector_tiles.freestiler_capabilities(fake)["keep_short_lines"] is True
    assert "keep_short_lines" in inspect.signature(fake.FreestileLayer).parameters


@pytest.mark.parametrize("value", ["0", "off", "false", "no"])
def test_env_turns_it_off(monkeypatch, tmp_path, value):
    monkeypatch.setenv(vector_tiles.KEEP_SHORT_LINES_ENV, value)
    assert _run(_Fake(has_option=True), tmp_path, ["mesh_faces"])["mesh_faces"]["keep_short_lines"] is False


def test_env_value_is_checked(monkeypatch):
    monkeypatch.setenv(vector_tiles.KEEP_SHORT_LINES_ENV, "maybe")
    with pytest.raises(ValueError, match=vector_tiles.KEEP_SHORT_LINES_ENV):
        vector_tiles.keep_short_lines_enabled()


def test_context_overrides_env(monkeypatch):
    monkeypatch.setenv(vector_tiles.KEEP_SHORT_LINES_ENV, "0")
    with vector_tiles.vector_tile_options(keep_short_lines=True):
        assert vector_tiles.keeps_short_lines("mesh_faces")
    monkeypatch.setenv(vector_tiles.KEEP_SHORT_LINES_ENV, "1")
    with vector_tiles.vector_tile_options(keep_short_lines=False):
        assert not vector_tiles.keeps_short_lines("breaklines")
    assert vector_tiles.keeps_short_lines("breaklines")
    assert not vector_tiles.keeps_short_lines("pipe_conduits")
    assert not vector_tiles.keeps_short_lines(None)


def test_summary_records_layers_or_the_reason(monkeypatch):
    monkeypatch.setattr(vector_tiles, "keep_short_lines_available", lambda: True)
    sources = [("faces", _layer("mesh_faces")), ("cells", _layer("mesh_cells")), ("bl", _layer("breaklines"))]
    summary = maplibre._short_lines_summary(sources)
    assert summary["enabled"] is True and summary["layers"] == ["faces", "bl"]
    assert summary["method"] == "freestiler keep_short_lines"

    assert maplibre._short_lines_summary([("cells", _layer("mesh_cells"))])["enabled"] is False
    assert "Arrow" in maplibre._short_lines_summary([("faces", Path("faces.ndgeojson"))])["reason"]
    with vector_tiles.vector_tile_options(keep_short_lines=False):
        assert maplibre._short_lines_summary(sources) == {"enabled": False, "reason": "turned off"}

    monkeypatch.setattr(vector_tiles, "keep_short_lines_available", lambda: False)
    assert "no keep_short_lines" in maplibre._short_lines_summary(sources)["reason"]


def test_dense_arrow_layer_carries_its_kind(tmp_path):
    """A mesh layer with no results reaches the tiler knowing it is mesh_faces."""

    import geopandas as gpd
    from shapely.geometry import LineString

    frame = gpd.GeoDataFrame(
        {"layer": ["mesh_faces"], "mesh_name": ["a"], "face_id": [1]},
        geometry=[LineString([(0, 0), (1, 1)])],
        crs="EPSG:4326",
    )
    path = tmp_path / "g.parquet"
    frame.to_parquet(path)
    layer, *_ = maplibre._dense_layer_arrow(path, "mesh_faces", "mesh_faces", None)
    assert layer.kind == "mesh_faces"


@pytest.mark.parametrize("flag,expected", [("--no-keep-short-lines", False), ("--keep-short-lines", True)])
def test_cli_flag_reaches_the_options(monkeypatch, tmp_path, flag, expected):
    from ras2cng import cli

    seen = {}

    def fake_package(*args, **kwargs):
        seen["value"] = vector_tiles.keep_short_lines_enabled()
        return {}

    monkeypatch.setenv(vector_tiles.KEEP_SHORT_LINES_ENV, "0" if expected else "1")
    monkeypatch.setattr("ras2cng.maplibre.package_maplibre_viewer", fake_package)
    result = CliRunner().invoke(
        cli.app, ["maplibre", str(tmp_path), str(tmp_path / "o"), "--geometry-hdf", "g01=g.hdf", flag]
    )
    assert "value" in seen, result.output
    assert seen["value"] is expected
