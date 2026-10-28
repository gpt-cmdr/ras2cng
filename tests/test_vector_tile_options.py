"""freestiler options: freestile_layers, gzip_level, optional MVT feature IDs."""

from __future__ import annotations

import sys
import types
from pathlib import Path

import geopandas as gpd
import pytest
from shapely.geometry import box

from ras2cng import vector_tiles


def _frame(n: int = 3) -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(
        {"cell_id": list(range(n))},
        geometry=[box(-95 + i * 0.001, 30, -95 + (i + 1) * 0.001, 30.001) for i in range(n)],
        crs="EPSG:4326",
    )


def _fake_freestiler(monkeypatch, *, layers=True, gzip=True, layers_gzip=True):
    calls: dict = {"freestile": [], "freestile_layers": [], "loaded": []}

    def freestile(frames, output, *, min_zoom, max_zoom, quiet, generate_ids, **extra):
        calls["freestile"].append({"layers": list(frames), "generate_ids": generate_ids, **extra})
        Path(output).write_bytes(b"pmtiles")

    if gzip:
        def freestile_gz(frames, output, *, min_zoom, max_zoom, quiet, generate_ids, gzip_level=None):
            freestile(frames, output, min_zoom=min_zoom, max_zoom=max_zoom, quiet=quiet,
                      generate_ids=generate_ids, gzip_level=gzip_level)
        module_freestile = freestile_gz
    else:
        def module_freestile(frames, output, *, min_zoom, max_zoom, quiet, generate_ids):
            freestile(frames, output, min_zoom=min_zoom, max_zoom=max_zoom, quiet=quiet,
                      generate_ids=generate_ids)

    module = types.ModuleType("freestiler")
    module.freestile = module_freestile

    if layers:
        def record(pairs, output, extra):
            names = []
            for name, frame in pairs:  # consumed one at a time
                calls["loaded"].append(("tiled", name))
                names.append(name)
            calls["freestile_layers"].append({"layers": names, **extra})
            Path(output).write_bytes(b"pmtiles")

        if layers_gzip:
            def freestile_layers(pairs, output, *, min_zoom, max_zoom, quiet, generate_ids, gzip_level=None):
                record(pairs, output, {"generate_ids": generate_ids, "gzip_level": gzip_level})
        else:
            def freestile_layers(pairs, output, *, min_zoom, max_zoom, quiet, generate_ids):
                record(pairs, output, {"generate_ids": generate_ids})
        module.freestile_layers = freestile_layers

    monkeypatch.setitem(sys.modules, "freestiler", module)
    monkeypatch.setattr(vector_tiles, "freestiler_available", lambda: True)
    vector_tiles._WARNED.clear()
    return calls


def _loader(calls, name, frame):
    def load():
        calls["loaded"].append(("load", name))
        return frame
    return load


def test_feature_ids_default_on_env_and_context(monkeypatch):
    monkeypatch.delenv(vector_tiles.FEATURE_IDS_ENV, raising=False)
    assert vector_tiles.feature_ids_enabled() is True
    monkeypatch.setenv(vector_tiles.FEATURE_IDS_ENV, "0")
    assert vector_tiles.feature_ids_enabled() is False
    with vector_tiles.vector_tile_options(feature_ids=True):
        assert vector_tiles.feature_ids_enabled() is True
    assert vector_tiles.feature_ids_enabled() is False
    monkeypatch.setenv(vector_tiles.FEATURE_IDS_ENV, "maybe")
    with pytest.raises(ValueError, match=vector_tiles.FEATURE_IDS_ENV):
        vector_tiles.feature_ids_enabled()


def test_gzip_level_default_env_and_validation(monkeypatch):
    monkeypatch.delenv(vector_tiles.GZIP_LEVEL_ENV, raising=False)
    assert vector_tiles.gzip_level() == 6
    monkeypatch.setenv(vector_tiles.GZIP_LEVEL_ENV, "9")
    assert vector_tiles.gzip_level() == 9
    with vector_tiles.vector_tile_options(gzip_level=1):
        assert vector_tiles.gzip_level() == 1
    for bad in ("10", "-1", "2.5", "x"):
        monkeypatch.setenv(vector_tiles.GZIP_LEVEL_ENV, bad)
        with pytest.raises(ValueError, match="0 to 9"):
            vector_tiles.gzip_level()
    with pytest.raises(ValueError):
        with vector_tiles.vector_tile_options(gzip_level=True):
            pass


def test_multi_layer_uses_freestile_layers_one_layer_at_a_time(monkeypatch, tmp_path):
    monkeypatch.delenv(vector_tiles.GZIP_LEVEL_ENV, raising=False)
    monkeypatch.delenv(vector_tiles.FEATURE_IDS_ENV, raising=False)
    calls = _fake_freestiler(monkeypatch)
    layers = {name: _loader(calls, name, _frame()) for name in ("cells", "faces", "empty")}
    layers["empty"] = _loader(calls, "empty", _frame().iloc[:0])

    applied = vector_tiles.run_freestiler(tmp_path / "out.pmtiles", layers, 10, 14)

    assert applied == {"generate_ids": True, "gzip_level": 6, "layered": True}
    assert calls["freestile"] == []
    assert calls["freestile_layers"] == [
        {"layers": ["cells", "faces"], "generate_ids": True, "gzip_level": 6}
    ]
    # Each layer is loaded only when the tiler asks for it.
    assert calls["loaded"] == [
        ("load", "cells"), ("tiled", "cells"),
        ("load", "faces"), ("tiled", "faces"),
        ("load", "empty"),
    ]
    assert (tmp_path / "out.pmtiles").read_bytes() == b"pmtiles"


def test_feature_ids_can_be_dropped(monkeypatch, tmp_path):
    calls = _fake_freestiler(monkeypatch)
    monkeypatch.setenv(vector_tiles.FEATURE_IDS_ENV, "false")
    vector_tiles.run_freestiler(tmp_path / "a.pmtiles", {"cells": _frame(), "faces": _frame()}, 10, 14)
    vector_tiles.run_freestiler(tmp_path / "b.pmtiles", {"cells": _frame()}, 10, 14)
    assert calls["freestile_layers"][0]["generate_ids"] is False
    assert calls["freestile"][0]["generate_ids"] is False


def test_layered_build_without_gzip_pass_through_warns(monkeypatch, tmp_path):
    """freestiler #3 + #4 merged without wiring gzip_level into freestile_layers."""
    calls = _fake_freestiler(monkeypatch, layers_gzip=False)
    with pytest.warns(RuntimeWarning, match="no gzip_level pass-through"):
        applied = vector_tiles.run_freestiler(
            tmp_path / "a.pmtiles", {"cells": _frame(), "faces": _frame()}, 10, 14
        )
    assert applied["layered"] and applied["gzip_level"] is None
    assert "gzip_level" not in calls["freestile_layers"][0]
    # A single layer still gets gzip_level through freestile().
    applied = vector_tiles.run_freestiler(tmp_path / "b.pmtiles", {"cells": _frame()}, 10, 14)
    assert applied["gzip_level"] == 6 and calls["freestile"][0]["gzip_level"] == 6


def test_stock_freestiler_without_new_api(monkeypatch, tmp_path):
    calls = _fake_freestiler(monkeypatch, layers=False, gzip=False)
    applied = vector_tiles.run_freestiler(
        tmp_path / "a.pmtiles", {"cells": _frame(), "faces": _frame()}, 10, 14
    )
    assert applied == {"generate_ids": True, "gzip_level": None, "layered": False}
    assert calls["freestile"] == [{"layers": ["cells", "faces"], "generate_ids": True}]
    assert vector_tiles.freestiler_capabilities(sys.modules["freestiler"]) == {
        "layers": False, "gzip": False, "layers_gzip": False,
    }


def test_all_empty_layers_are_refused(monkeypatch, tmp_path):
    _fake_freestiler(monkeypatch)
    empty = _frame().iloc[:0]

    def refuse_empty(pairs, output, **kwargs):
        if not list(pairs):
            raise ValueError("at least one layer is required")

    sys.modules["freestiler"].freestile_layers = refuse_empty
    with pytest.raises(ValueError, match="non-empty source layer"):
        vector_tiles.run_freestiler(tmp_path / "a.pmtiles", {"a": empty, "b": empty}, 10, 14)
    assert not (tmp_path / "a.pmtiles").exists()


def test_cli_feature_ids_flag_reaches_the_tiler(monkeypatch, tmp_path):
    from typer.testing import CliRunner

    from ras2cng import pmtiles
    from ras2cng.cli import app

    seen = {}

    def fake_generate(*args, **kwargs):
        seen["ids"] = vector_tiles.feature_ids_enabled()

    monkeypatch.setattr(pmtiles, "generate_pmtiles_from_input", fake_generate)
    monkeypatch.delenv(vector_tiles.FEATURE_IDS_ENV, raising=False)
    source = tmp_path / "in.parquet"
    source.write_bytes(b"")
    result = CliRunner().invoke(app, ["pmtiles", str(source), str(tmp_path / "o.pmtiles"), "--no-feature-ids"])
    assert result.exit_code == 0, result.output
    assert seen["ids"] is False
    result = CliRunner().invoke(app, ["pmtiles", str(source), str(tmp_path / "o.pmtiles")])
    assert result.exit_code == 0 and seen["ids"] is True


def test_real_freestiler_capabilities_are_detected():
    pytest.importorskip("freestiler")
    import freestiler

    caps = vector_tiles.freestiler_capabilities(freestiler)
    assert caps["layers"] == hasattr(freestiler, "freestile_layers")
