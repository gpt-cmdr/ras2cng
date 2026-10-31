"""fast-map storage areas, velocity, Max derived products, duration and the
native effective terrain (fast-map #18-#20, ras-commander #755/#757/#758).

RasFim and the engine are mocked; every capability is detected from the
engine's method record or RasFim's signature, so the fakes below advertise
exactly what each test needs. No HEC-RAS model or engine run is needed.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from ras2cng import fast_map
from ras2cng.mapping import generate_result_maps

_SLOPING = {"mode": "sloping", "reduce_shallow_to_horizontal": False,
            "use_depth_weighted_faces": False}
_HORIZONTAL = {**_SLOPING, "mode": "horizontal"}
_PRETTY = {**_SLOPING, "mode": "slopingPretty"}

_ENGINE_SLOPING_T08 = {
    "velocity": "rasmapper-sloping-velocity/0",
    "velocity_depth": ["rendered", "horizontal"],
    "velocity_render_modes": ["horizontal", "sloping", "slopingPretty"],
    "derived_products": ["depth_x_velocity", "depth_x_velocity_squared", "froude"],
    "derived_max": "reduce_max/1",
    "storage_area_surfaces": "storage-area-level-pool/1",
}
_ENGINE_WHOLE_T08 = {"duration_rule": fast_map.DURATION_RULE}


@pytest.fixture
def new_engine(monkeypatch):
    """An engine with every fast-map #18-#20 capability."""
    records = {"sloping": _ENGINE_SLOPING_T08, "whole_simulation": _ENGINE_WHOLE_T08}
    monkeypatch.setattr(fast_map, "engine_method", lambda kind: records.get(kind, {}))
    monkeypatch.setattr(fast_map, "storage_areas_supported", lambda: True)
    monkeypatch.setattr(fast_map, "velocity_supported", lambda: True)
    monkeypatch.setattr(fast_map, "_has_kwargs", lambda function, *names: True)
    return records


def _manifest(folder: Path, *, velocity=False) -> pd.DataFrame:
    depth, wse = folder / "09_Max_depth.tif", folder / "10_Max_wsel.tif"
    depth.write_bytes(b"depth")
    wse.write_bytes(b"wse")
    row = {"profile": "Max", "status": "complete", "depth_tif": str(depth),
           "wse_tif": str(wse), "error": None}
    if velocity:
        vel = folder / "12_Max_velocity.tif"
        vel.write_bytes(b"vel")
        row["velocity_tif"] = str(vel)
    (folder / "fim_manifest.json").write_text(json.dumps({"status": "complete"}))
    return pd.DataFrame([row])


# ---------------------------------------------------------------------------
# Capability detection
# ---------------------------------------------------------------------------


def test_velocity_render_modes_follow_the_engine_record(monkeypatch):
    monkeypatch.setattr(fast_map, "velocity_supported", lambda: True)
    monkeypatch.setattr(fast_map, "_has_kwargs", lambda function, *names: True)
    monkeypatch.setattr(fast_map, "engine_method", lambda kind: {})
    assert fast_map.velocity_render_modes() == ("sloping",)
    assert fast_map.velocity_default_on() is False
    monkeypatch.setattr(fast_map, "engine_method", lambda kind: _ENGINE_SLOPING_T08)
    assert fast_map.velocity_render_modes() == ("sloping", "horizontal", "slopingPretty")
    assert fast_map.velocity_default_on() is True
    monkeypatch.setattr(fast_map, "velocity_supported", lambda: False)
    assert fast_map.velocity_render_modes() == ()


def test_duration_and_derived_capabilities(monkeypatch):
    monkeypatch.setattr(fast_map, "engine_method", lambda kind: {})
    assert fast_map.duration_supported() is False
    assert fast_map.derived_max_supported() is False
    records = {"sloping": _ENGINE_SLOPING_T08, "whole_simulation": _ENGINE_WHOLE_T08}
    monkeypatch.setattr(fast_map, "engine_method", lambda kind: records.get(kind, {}))
    assert fast_map.duration_supported() is True
    with patch("ras_commander.RasFim") as fim:
        fim.map_derived_max = MagicMock()
        assert fast_map.derived_max_supported() is True
        del fim.map_derived_max
        assert fast_map.derived_max_supported() is False


@patch("ras_commander.RasFim")
def test_storage_area_capability_needs_a_true_answer(fim, monkeypatch):
    monkeypatch.undo()  # the real capability check
    monkeypatch.setattr(fast_map, "_has_kwargs", lambda function, *names: True)
    fim.supports_storage_areas.return_value = MagicMock()  # truthy, not True
    assert fast_map.storage_areas_supported() is False
    fim.supports_storage_areas.return_value = True
    assert fast_map.storage_areas_supported() is True
    fim.supports_storage_areas.side_effect = RuntimeError("engine missing")
    assert fast_map.storage_areas_supported() is False


@patch("ras_commander.RasFim")
def test_effective_terrain_capability(fim, monkeypatch):
    monkeypatch.undo()
    fim.supports_effective_terrain.return_value = True
    assert fast_map.effective_terrain_supported() is True
    fim.supports_effective_terrain.return_value = False
    assert fast_map.effective_terrain_supported() is False
    del fim.build_effective_terrain
    fim.supports_effective_terrain.return_value = True
    assert fast_map.effective_terrain_supported() is False


# ---------------------------------------------------------------------------
# Storage areas
# ---------------------------------------------------------------------------


@patch("ras_commander.RasFim")
def test_scope_accepts_storage_areas_only_with_the_capability(fim, tmp_path, monkeypatch):
    fim.validate_2d_model.return_value = {"storage_areas": 2}
    with pytest.raises(fast_map.FastMapUnavailable, match="storage-area-level-pool/1"):
        fast_map._check_scope(fim, tmp_path / "x.p01.hdf")
    monkeypatch.setattr(fast_map, "storage_areas_supported", lambda: True)
    assert fast_map._check_scope(fim, tmp_path / "x.p01.hdf") == 2


@patch("ras_commander.RasFim")
def test_preflight_carries_the_storage_area_count(fim, tmp_path, monkeypatch):
    monkeypatch.setattr(fast_map, "storage_areas_supported", lambda: True)
    monkeypatch.setattr(fast_map, "render_settings", lambda ras, mode: dict(_SLOPING))
    fim.validate_2d_model.return_value = {"storage_areas": 1}
    assert fast_map.preflight(tmp_path / "x.p01.hdf", MagicMock(), None)["storage_areas"] == 1
    fim.validate_2d_model.return_value = {"storage_areas": 0}
    assert "storage_areas" not in fast_map.preflight(tmp_path / "x.p01.hdf", MagicMock(), None)


@patch("ras_commander.RasFim")
def test_sloping_storage_area_plan_draws_level_pools(fim, tmp_path, new_engine):
    fim.validate_2d_model.return_value = {"storage_areas": 1}
    fim.map_sloping_profiles.side_effect = lambda hdf, t, folder, **kw: _manifest(Path(folder))
    out = tmp_path / "p07"
    result = fast_map.map_plan(
        tmp_path / "x.p07.hdf", tmp_path / "t.tif", out,
        map_types=["wse", "depth", "velocity"], render=_SLOPING, velocity=True,
        product_writer="rasterio",
    )
    assert set(result) == {"wse", "depth"}, "velocity is refused with storage areas"
    kwargs = fim.map_sloping_profiles.call_args.kwargs
    assert kwargs["storage_areas"] is True and "velocity" not in kwargs
    details = json.loads((out / fast_map.FAST_MAP_MANIFEST).read_text())["ras2cng"]
    assert details["storage_areas"] == 1
    assert details["storage_area_method"] == "storage-area-level-pool/1"


@patch("ras_commander.RasFim")
def test_horizontal_storage_area_plan_names_the_pool_profile(fim, tmp_path, new_engine):
    fim.validate_2d_model.return_value = {"storage_areas": 1}
    fim.map_horizontal_profiles.side_effect = lambda c, t, folder, **kw: _manifest(Path(folder))
    fast_map.map_plan(
        tmp_path / "x.p07.hdf", tmp_path / "t.tif", tmp_path / "out",
        map_types=["depth"], render=_HORIZONTAL,
    )
    assert fim.map_horizontal_profiles.call_args.kwargs["storage_areas"] == {"wsel_max": "Max"}


@patch("ras_commander.RasFim")
def test_plan_without_storage_areas_passes_nothing_new(fim, tmp_path, new_engine):
    fim.validate_2d_model.return_value = {"storage_areas": 0}
    fim.map_sloping_profiles.side_effect = lambda hdf, t, folder, **kw: _manifest(Path(folder))
    fast_map.map_plan(
        tmp_path / "x.p01.hdf", tmp_path / "t.tif", tmp_path / "out",
        map_types=["depth"], render=_SLOPING, product_writer="rasterio",
    )
    assert "storage_areas" not in fim.map_sloping_profiles.call_args.kwargs


@patch("ras_commander.RasFim")
def test_boundary_refuses_storage_areas(fim, tmp_path, new_engine):
    fim.validate_2d_model.return_value = {"storage_areas": 1}
    with pytest.raises(fast_map.FastMapUnavailable, match="storage-area"):
        fast_map.map_boundary(
            tmp_path / "x.p07.hdf", tmp_path / "t.tif", tmp_path / "t.hdf", tmp_path / "out",
            render=_SLOPING,
        )
    fim.map_inundation_boundaries.assert_not_called()


# ---------------------------------------------------------------------------
# Velocity
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("render", "requested", "expected"),
    [
        (_SLOPING, None, "rendered"),
        (_HORIZONTAL, None, "horizontal"),
        (_PRETTY, None, None),          # opt-in
        (_PRETTY, True, "rendered"),
        ({**_PRETTY, "reduce_shallow_to_horizontal": True}, True, None),
        (_SLOPING, False, None),
        ({**_SLOPING, "storage_areas": 1}, True, None),
    ],
)
def test_velocity_plan(new_engine, render, requested, expected):
    depth, reason = fast_map.velocity_plan(render, requested)
    assert depth == expected
    assert (reason is None) == (expected is not None)


def test_velocity_stays_opt_in_on_older_engines(monkeypatch):
    monkeypatch.setattr(fast_map, "velocity_supported", lambda: True)
    monkeypatch.setattr(fast_map, "engine_method", lambda kind: {})
    assert fast_map.velocity_plan(_SLOPING, None) == (
        None, "velocity is opt-in on this fast-map (--fast-map-velocity)"
    )
    assert fast_map.velocity_plan(_SLOPING, True) == ("rendered", None)
    assert fast_map.velocity_plan(_HORIZONTAL, True)[0] is None


@patch("ras_commander.RasFim")
def test_horizontal_velocity_renders_sloping_clipped_by_horizontal_depth(fim, tmp_path, new_engine):
    fim.validate_2d_model.return_value = {"storage_areas": 0}
    fim.map_sloping_profiles.side_effect = (
        lambda hdf, t, folder, **kw: _manifest(Path(folder), velocity=True)
    )
    out = tmp_path / "p01"
    result = fast_map.map_velocity(
        tmp_path / "x.p01.hdf", tmp_path / "t.tif", out, render=_HORIZONTAL,
        velocity_depth="horizontal", terrain_hdf=tmp_path / "T.hdf", terrain_kind="source",
    )
    assert result == {"velocity": [out / "Velocity (Max).tif"]}
    assert (out / "Velocity (Max).tif").read_bytes() == b"vel"
    assert not (out / "Depth (Max).tif").exists(), "WSE/Depth of this call are discarded"
    kwargs = fim.map_sloping_profiles.call_args.kwargs
    assert kwargs["render_mode"] == "sloping" and kwargs["velocity"] is True
    assert kwargs["velocity_depth"] == "horizontal" and kwargs["product_writer"] == "rasterio"
    assert kwargs["terrain_hdf"] == tmp_path / "T.hdf"
    details = json.loads((out / fast_map.FAST_MAP_VELOCITY_MANIFEST).read_text())["ras2cng"]
    assert details["velocity_depth"] == "horizontal" and details["terrain_kind"] == "source"
    assert not list(out.glob(".fast-map-velocity-*"))


@patch("ras_commander.RasFim")
def test_map_velocity_refuses_storage_areas(fim, tmp_path, new_engine):
    fim.validate_2d_model.return_value = {"storage_areas": 1}
    with pytest.raises(fast_map.FastMapUnavailable, match="storage-area"):
        fast_map.map_velocity(
            tmp_path / "x.p07.hdf", tmp_path / "t.tif", tmp_path / "out", render=_SLOPING,
        )


# ---------------------------------------------------------------------------
# Max D x V, D x V^2, Froude
# ---------------------------------------------------------------------------


def _derived_frame(folder: Path, products) -> pd.DataFrame:
    rows = []
    for product in products:
        tif = folder / f"14_max_{product}.tif"
        tif.write_bytes(product.encode())
        rows.append({"product": product, "tif": str(tif), "valid_pixels": 973956})
    frame = pd.DataFrame(rows)
    frame.attrs["max_derived"] = {"method": "reduce_max/1", "times": 433}
    return frame


@pytest.mark.parametrize(("render", "velocity_depth"), [(_SLOPING, "rendered"), (_HORIZONTAL, "horizontal")])
@patch("ras_commander.RasFim")
def test_map_derived_names_products_like_rasmapper(fim, tmp_path, new_engine, render, velocity_depth):
    fim.validate_2d_model.return_value = {"storage_areas": 0}
    fim.map_derived_max.side_effect = (
        lambda hdf, t, folder, **kw: _derived_frame(Path(folder), kw["products"])
    )
    out = tmp_path / "p05"
    result = fast_map.map_derived(
        tmp_path / "x.p05.hdf", tmp_path / "t.tif", out,
        map_types=["froude", "depth_x_velocity_sq", "depth_x_velocity", "wse"], render=render,
    )
    assert result == {
        "depth_x_velocity": [out / "D _ V (Max).tif"],
        "depth_x_velocity_sq": [out / "D _ V^2 (Max).tif"],
        "froude": [out / "Froude (Max).tif"],
    }
    kwargs = fim.map_derived_max.call_args.kwargs
    assert kwargs["products"] == ("depth_x_velocity", "depth_x_velocity_squared", "froude")
    assert kwargs["render_mode"] == "sloping" and kwargs["velocity_depth"] == velocity_depth
    assert kwargs["max_inflight_profiles"] == fast_map.DERIVED_MAX_INFLIGHT
    details = json.loads((out / fast_map.FAST_MAP_DERIVED_MANIFEST).read_text())
    assert details["max_derived"]["method"] == "reduce_max/1"
    assert details["products"]["froude"]["valid_pixels"] == 973956


@pytest.mark.parametrize(
    ("render", "enabled", "ok"),
    [(_SLOPING, True, True), (_HORIZONTAL, True, True), (_PRETTY, True, False),
     (_SLOPING, False, False), ({**_SLOPING, "storage_areas": 1}, True, False)],
)
def test_derived_plan(new_engine, render, enabled, ok):
    with patch("ras_commander.RasFim") as fim:
        fim.map_derived_max = MagicMock()
        assert (fast_map.derived_plan(render, enabled) is None) is ok


# ---------------------------------------------------------------------------
# Duration and storage areas in whole-simulation products
# ---------------------------------------------------------------------------


def test_whole_simulation_types(new_engine, monkeypatch):
    types, notes = fast_map.whole_simulation_types(
        ["wse", "arrival_time", "duration", "percent_inundated"], _SLOPING
    )
    assert types == ["arrival_time", "percent_inundated", "duration"] and notes == []
    types, notes = fast_map.whole_simulation_types(
        ["arrival_time", "duration"], {**_SLOPING, "storage_areas": 1}
    )
    assert types == ["arrival_time"] and "storage-area" in notes[0]
    monkeypatch.setattr(fast_map, "engine_method", lambda kind: {})
    types, notes = fast_map.whole_simulation_types(["duration"], _SLOPING)
    assert types == [] and "duration" in notes[0]


def _whole_manifest(folder: Path, products) -> pd.DataFrame:
    rows = []
    for product in products:
        tif = folder / f"{product}.tif"
        tif.write_bytes(product.encode())
        rows.append({"product": product, "status": "complete", "tif": str(tif), "units": "hours",
                     "output_times": 433, "wet_pixels": 10, "qualified": True})
    return pd.DataFrame(rows)


@patch("ras_commander.RasFim")
def test_whole_simulation_maps_duration_and_level_pools(fim, tmp_path, new_engine, monkeypatch):
    monkeypatch.setattr(fast_map, "plan_units", lambda hdf: "ft")
    fim.map_whole_simulation.side_effect = (
        lambda hdf, t, folder, **kw: _whole_manifest(Path(folder), kw["products"])
    )
    fim.validate_2d_model.return_value = {"storage_areas": 0}
    out = tmp_path / "p05"
    result = fast_map.map_whole_simulation(
        tmp_path / "x.p05.hdf", tmp_path / "t.tif", out,
        map_types=["arrival_time", "duration"], threshold=0.5, render=_SLOPING,
    )
    assert result["duration"] == [out / "Duration (0.5ft hrs).tif"]
    assert fim.map_whole_simulation.call_args.kwargs["products"] == ("arrival_time", "duration")
    assert "storage_areas" not in fim.map_whole_simulation.call_args.kwargs
    details = json.loads((out / fast_map.FAST_MAP_WHOLE_SIMULATION_MANIFEST).read_text())
    assert details["duration_rule"] == fast_map.DURATION_RULE

    fim.validate_2d_model.return_value = {"storage_areas": 1}
    out = tmp_path / "p07"
    result = fast_map.map_whole_simulation(
        tmp_path / "x.p07.hdf", tmp_path / "t.tif", out,
        map_types=["arrival_time", "duration"], threshold=0.5, render=_SLOPING,
    )
    assert set(result) == {"arrival_time"}, "duration is refused with level pools"
    kwargs = fim.map_whole_simulation.call_args.kwargs
    assert kwargs["storage_areas"] is True and kwargs["products"] == ("arrival_time",)


# ---------------------------------------------------------------------------
# Native effective terrain
# ---------------------------------------------------------------------------


def _resolver(tmp_path, monkeypatch, *, effective=True, source=True):
    resolver = fast_map.TerrainResolver(
        tmp_path / "a.prj", tmp_path / "w", terrain_name="T", effective=effective
    )
    monkeypatch.setattr(fast_map, "source_terrain_supported", lambda: source)
    monkeypatch.setattr(fast_map, "effective_terrain_supported", lambda: True)
    monkeypatch.setattr(resolver, "_source", lambda plan: (tmp_path / "src.tif", tmp_path / "T.hdf"))
    monkeypatch.setattr(resolver, "_canonical", lambda tif: None)
    monkeypatch.setattr(resolver, "_terrain_hdf_for", lambda plan: tmp_path / "T.hdf")
    monkeypatch.setattr(resolver, "for_plan", lambda plan: tmp_path / "export.tif")
    # An unmodified terrain HDF: modified (or unreadable) terrains never reach Rust.
    import h5py

    with h5py.File(tmp_path / "T.hdf", "w") as handle:
        handle.create_group("Terrain")
    return resolver


@patch("ras_commander.RasFim")
def test_effective_terrain_comes_after_the_source_and_before_the_export(fim, tmp_path, monkeypatch):
    resolver = _resolver(tmp_path, monkeypatch)
    fim.build_effective_terrain.side_effect = lambda hdf, target, **kw: {"output": str(target)}
    kinds = []

    def run(tif, hdf, kind):
        kinds.append((kind, tif, hdf))
        if kind == fast_map.TERRAIN_SOURCE:
            raise fast_map.FastMapUnavailable("28 storage-area pixels lie outside the footprint")
        return "ok"

    assert fast_map.with_terrain(resolver, tmp_path / "a.p07.hdf", run)[1] == fast_map.TERRAIN_EFFECTIVE
    assert [k for k, _, _ in kinds] == ["source", "effective"]
    assert kinds[1][1] == tmp_path / "w" / "T.effective.tif" and kinds[1][2] is None
    # Built once per terrain HDF per run.
    fast_map.with_terrain(resolver, tmp_path / "a.p08.hdf", run)
    fim.build_effective_terrain.assert_called_once()
    assert fim.build_effective_terrain.call_args.args[0] == tmp_path / "T.hdf"


@patch("ras_commander.RasFim")
def test_a_refused_effective_terrain_falls_back_to_the_export(fim, tmp_path, monkeypatch):
    resolver = _resolver(tmp_path, monkeypatch, source=False)
    fim.build_effective_terrain.side_effect = RuntimeError("terrain has modifications ['hdf:Channel']")
    result, kind = fast_map.with_terrain(resolver, tmp_path / "a.p01.hdf", lambda t, h, k: k)
    assert kind == fast_map.TERRAIN_EXPORT
    assert any("modifications" in note for note in resolver.notes)


@patch("ras_commander.RasFim")
def test_effective_terrain_can_be_disabled(fim, tmp_path, monkeypatch):
    resolver = _resolver(tmp_path, monkeypatch, effective=False, source=False)
    assert fast_map.with_terrain(resolver, tmp_path / "a.p01.hdf", lambda t, h, k: k)[1] == "export"
    fim.build_effective_terrain.assert_not_called()


# ---------------------------------------------------------------------------
# generate_result_maps routing
# ---------------------------------------------------------------------------


def _fake_ras(tmp_path):
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    (project_dir / "TestModel.prj").write_text("Proj Title=Test\nEnglish Units\n")
    (project_dir / "TestModel.p01.hdf").touch()
    ras = MagicMock()
    ras.project_name = "TestModel"
    ras.project_folder = str(project_dir)
    ras.plan_df = pd.DataFrame([{"plan_number": "01", "geometry_number": "01"}])
    return ras, project_dir


def _route(tmp_path, monkeypatch, render, **kwargs):
    """Run generate_result_maps with every fast-map group mocked; return calls."""
    ras, project_dir = _fake_ras(tmp_path)
    terrain = tmp_path / "dem.tif"
    terrain.write_bytes(b"x")
    out = tmp_path / "maps" / "p01"
    calls = {}

    def group(name, products):
        def fake(*args, **kw):
            calls[name] = kw
            return fast_map.FastMapProducts({p: [out / f"{p}.tif"] for p in products(kw)})
        return fake

    monkeypatch.setattr(fast_map, "preflight", lambda plan, r, mode: dict(render))
    monkeypatch.setattr(fast_map, "map_plan", group(
        "plan", lambda kw: [t for t in kw["map_types"] if t in ("wse", "depth")]))
    monkeypatch.setattr(fast_map, "map_velocity", group("velocity", lambda kw: ["velocity"]))
    monkeypatch.setattr(fast_map, "map_derived", group("derived", lambda kw: kw["map_types"]))
    monkeypatch.setattr(fast_map, "map_whole_simulation", group("whole", lambda kw: kw["map_types"]))
    with patch("ras2cng.mapping.init_ras_project", return_value=ras), \
            patch("ras2cng.mapping._configure_rasprocess"), \
            patch("ras2cng.mapping._generate_plan_maps", return_value={}) as gen:
        results = generate_result_maps(
            project_dir, tmp_path / "maps", map_engine="fast-map", fast_map_terrain=terrain,
            **kwargs,
        )
    flags = gen.call_args.kwargs if gen.called else {}
    return results[0], calls, {k for k, v in flags.items() if v is True and k in fast_map_types()}


def fast_map_types():
    from ras2cng.mapping import MAP_TYPE_VARIABLES

    return set(MAP_TYPE_VARIABLES)


@pytest.mark.parametrize("render", [_SLOPING, _HORIZONTAL])
def test_default_routing_on_a_new_engine(tmp_path, monkeypatch, new_engine, render):
    with patch("ras_commander.RasFim") as fim:
        fim.map_derived_max = MagicMock()
        result, calls, rasmapper = _route(
            tmp_path, monkeypatch, render, froude=True, depth_x_velocity=True,
            duration=True, arrival_time=True, shear_stress=True,
        )
    assert rasmapper == {"shear_stress"}
    assert result.engines["velocity"] == "fast-map"
    assert calls["velocity"]["velocity_depth"] == (
        "horizontal" if render["mode"] == "horizontal" else "rendered"
    )
    assert calls["derived"]["map_types"] == ["depth_x_velocity", "froude"]
    assert calls["whole"]["map_types"] == ["arrival_time", "duration"]
    assert "velocity" not in calls["plan"], "WSE/Depth never carry velocity"


def test_storage_area_plan_keeps_unqualified_products_on_rasmapper(tmp_path, monkeypatch, new_engine):
    with patch("ras_commander.RasFim") as fim:
        fim.map_derived_max = MagicMock()
        result, calls, rasmapper = _route(
            tmp_path, monkeypatch, {**_SLOPING, "storage_areas": 1},
            froude=True, duration=True, arrival_time=True,
        )
    assert rasmapper == {"velocity", "froude", "duration"}
    assert result.engines["wse"] == result.engines["arrival_time"] == "fast-map"
    assert "velocity" not in calls and "derived" not in calls
    assert calls["whole"]["map_types"] == ["arrival_time"]


def test_sloping_pretty_velocity_is_opt_in(tmp_path, monkeypatch, new_engine):
    _, calls, rasmapper = _route(tmp_path, monkeypatch, _PRETTY)
    assert "velocity" not in calls and rasmapper == {"velocity"}
    (tmp_path / "b").mkdir()
    _, calls, rasmapper = _route(tmp_path / "b", monkeypatch, _PRETTY, fast_map_velocity=True)
    assert calls["velocity"]["velocity_depth"] == "rendered" and rasmapper == set()


def test_no_fast_map_velocity_and_derived(tmp_path, monkeypatch, new_engine):
    _, calls, rasmapper = _route(
        tmp_path, monkeypatch, _SLOPING, froude=True,
        fast_map_velocity=False, fast_map_derived=False,
    )
    assert "velocity" not in calls and "derived" not in calls
    assert rasmapper == {"velocity", "froude"}


def test_non_max_profile_does_no_terrain_work(tmp_path, monkeypatch, new_engine):
    (tmp_path / "x").mkdir()
    result, calls, rasmapper = _route(
        tmp_path / "x", monkeypatch, _SLOPING, profile="02JAN1999 13:00:00", arrival_time=True,
    )
    assert set(calls) == {"whole"}, "only whole-simulation products ignore the profile"
    assert rasmapper == {"wse", "depth", "velocity"}


def test_sloping_pretty_whole_simulation_is_decided_before_terrain_work(tmp_path, monkeypatch, new_engine):
    types, notes = fast_map.whole_simulation_types(["arrival_time", "duration"], _PRETTY)
    assert types == [] and "slopingPretty" in notes[0]
    _, calls, rasmapper = _route(tmp_path, monkeypatch, _PRETTY, arrival_time=True, velocity=False)
    assert "whole" not in calls and rasmapper == {"arrival_time"}
