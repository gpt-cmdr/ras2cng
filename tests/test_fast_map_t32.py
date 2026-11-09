"""fast-map slopingPretty velocity / derived Max, Max shear stress and the
native-serial boundary method (fast-map #30, #31 and 0.15; ras-commander
#819, #820 and 0.116).

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
_PRETTY_RS = {**_PRETTY, "reduce_shallow_to_horizontal": True}
_PRETTY_DW = {**_PRETTY, "use_depth_weighted_faces": True}
_PRETTY_BOTH = {**_PRETTY_RS, "use_depth_weighted_faces": True}

_BASE_SLOPING = {
    "velocity": "rasmapper-sloping-velocity/0",
    "velocity_depth": ["rendered", "horizontal"],
    "velocity_render_modes": ["horizontal", "sloping", "slopingPretty"],
    "derived_products": ["depth_x_velocity", "depth_x_velocity_squared", "froude"],
    "derived_max": "reduce_max/1",
}
_FACES = {
    "velocity_with_faces": fast_map.WITH_FACES_METHOD,
    "velocity_with_faces_options": list(fast_map.SLOPING_PRETTY_OPTIONS),
}
_SHEAR = {
    "shear_stress": fast_map.SHEAR_STRESS_METHOD,
    "shear_stress_depth": ["rendered", "horizontal"],
}


def _engine(monkeypatch, sloping, **other):
    records = {"sloping": sloping, **other}
    monkeypatch.setattr(fast_map, "engine_method", lambda kind: records.get(kind, {}))
    monkeypatch.setattr(fast_map, "velocity_supported", lambda: True)
    monkeypatch.setattr(fast_map, "_has_kwargs", lambda function, *names: True)
    return records


@pytest.fixture
def released_engine(monkeypatch):
    """fast-map 0.15.0 / ras-commander 0.116.0: no slopingPretty record, no shear."""
    return _engine(monkeypatch, dict(_BASE_SLOPING))


@pytest.fixture
def faces_engine(monkeypatch):
    """An engine advertising slopingPretty velocity / derived Max and Max shear."""
    monkeypatch.setattr(fast_map, "shear_supported", lambda: True)
    return _engine(monkeypatch, {**_BASE_SLOPING, **_FACES, **_SHEAR})


def _manifest(folder: Path, **extra) -> pd.DataFrame:
    depth, wse = folder / "09_Max_depth.tif", folder / "10_Max_wsel.tif"
    depth.write_bytes(b"depth")
    wse.write_bytes(b"wse")
    row = {"profile": "Max", "status": "complete", "depth_tif": str(depth),
           "wse_tif": str(wse), "error": None}
    for column, (name, payload) in extra.items():
        path = folder / name
        path.write_bytes(payload)
        row[column] = str(path)
    (folder / "fim_manifest.json").write_text(json.dumps({"status": "complete"}))
    return pd.DataFrame([row])


# ---------------------------------------------------------------------------
# slopingPretty velocity and derived Max
# ---------------------------------------------------------------------------


def test_with_faces_options_needs_the_engine_record_and_the_rasfim_kwargs(monkeypatch):
    _engine(monkeypatch, dict(_BASE_SLOPING))
    assert fast_map.with_faces_options() is None
    _engine(monkeypatch, {**_BASE_SLOPING, **_FACES})
    assert fast_map.with_faces_options() == fast_map.SLOPING_PRETTY_OPTIONS
    # An engine with the record, a ras-commander that cannot pass the options.
    monkeypatch.setattr(fast_map, "_has_kwargs", lambda function, *names: False)
    assert fast_map.with_faces_options() is None
    # A record naming another method is not the qualified one.
    _engine(monkeypatch, {**_BASE_SLOPING, **_FACES, "velocity_with_faces": "something/2"})
    assert fast_map.with_faces_options() is None


@pytest.mark.parametrize("render", [_PRETTY, _PRETTY_RS, _PRETTY_DW, _PRETTY_BOTH])
def test_sloping_pretty_velocity_and_derived_are_defaults_with_the_record(faces_engine, render):
    assert fast_map.velocity_plan(render, None) == ("rendered", None)
    with patch("ras_commander.RasFim") as fim:
        fim.map_derived_max = MagicMock()
        assert fast_map.derived_plan(render, True) is None


def test_sloping_pretty_needs_every_project_option_in_the_record(monkeypatch):
    _engine(monkeypatch, {
        **_BASE_SLOPING, **_FACES,
        "velocity_with_faces_options": ["reduce_shallow_to_horizontal"],
    })
    assert fast_map.velocity_plan(_PRETTY_RS, None) == ("rendered", None)
    depth, reason = fast_map.velocity_plan(_PRETTY_DW, True)
    assert depth is None and "use_depth_weighted_faces" in reason
    with patch("ras_commander.RasFim") as fim:
        fim.map_derived_max = MagicMock()
        assert "use_depth_weighted_faces" in fast_map.derived_plan(_PRETTY_BOTH, True)


def test_without_the_record_sloping_pretty_keeps_the_released_behaviour(released_engine):
    depth, reason = fast_map.velocity_plan(_PRETTY, None)
    assert depth is None and "opt-in" in reason
    assert fast_map.velocity_plan(_PRETTY, True) == ("rendered", None)
    # Options were never qualified without the record, asked for or not.
    assert fast_map.velocity_plan(_PRETTY_RS, True)[0] is None
    with patch("ras_commander.RasFim") as fim:
        fim.map_derived_max = MagicMock()
        assert fast_map.derived_plan(_PRETTY, True) is not None
        assert fast_map.derived_plan(_SLOPING, True) is None


@pytest.mark.parametrize("render", [_PRETTY, _PRETTY_RS, _PRETTY_DW, _PRETTY_BOTH])
@patch("ras_commander.RasFim")
def test_map_velocity_passes_the_project_options_unchanged(fim, tmp_path, faces_engine, render):
    fim.validate_2d_model.return_value = {"storage_areas": 0}
    fim.map_sloping_profiles.side_effect = lambda hdf, t, folder, **kw: _manifest(
        Path(folder), velocity_tif=("12_Max_velocity.tif", b"vel"))
    out = tmp_path / "p05"
    result = fast_map.map_velocity(tmp_path / "x.p05.hdf", tmp_path / "t.tif", out, render=render)
    assert result == {"velocity": [out / "Velocity (Max).tif"]}
    kwargs = fim.map_sloping_profiles.call_args.kwargs
    assert kwargs["render_mode"] == "slopingPretty"
    assert kwargs["reduce_shallow_to_horizontal"] is render["reduce_shallow_to_horizontal"]
    assert kwargs["use_depth_weighted_faces"] is render["use_depth_weighted_faces"]
    details = json.loads((out / fast_map.FAST_MAP_VELOCITY_MANIFEST).read_text())["ras2cng"]
    assert details["with_faces_method"] == fast_map.WITH_FACES_METHOD
    assert details["sloping_pretty_options"] == [
        name for name in fast_map.SLOPING_PRETTY_OPTIONS if render[name]
    ]


@patch("ras_commander.RasFim")
def test_horizontal_velocity_never_carries_sloping_pretty_options(fim, tmp_path, faces_engine):
    fim.validate_2d_model.return_value = {"storage_areas": 0}
    fim.map_sloping_profiles.side_effect = lambda hdf, t, folder, **kw: _manifest(
        Path(folder), velocity_tif=("12_Max_velocity.tif", b"vel"))
    render = {**_HORIZONTAL, "reduce_shallow_to_horizontal": True}
    fast_map.map_velocity(
        tmp_path / "x.p05.hdf", tmp_path / "t.tif", tmp_path / "out", render=render,
        velocity_depth="horizontal",
    )
    kwargs = fim.map_sloping_profiles.call_args.kwargs
    assert kwargs["render_mode"] == "sloping"
    assert kwargs["reduce_shallow_to_horizontal"] is False
    assert kwargs["use_depth_weighted_faces"] is False


def _derived_frame(folder: Path, products) -> pd.DataFrame:
    rows = []
    for product in products:
        tif = folder / f"14_max_{product}.tif"
        tif.write_bytes(product.encode())
        rows.append({"product": product, "tif": str(tif), "valid_pixels": 974110})
    frame = pd.DataFrame(rows)
    frame.attrs["max_derived"] = {"method": "reduce_max/1", "times": 433}
    return frame


@pytest.mark.parametrize("render", [_PRETTY, _PRETTY_BOTH])
@patch("ras_commander.RasFim")
def test_map_derived_renders_sloping_pretty_with_the_project_options(
    fim, tmp_path, faces_engine, render
):
    fim.validate_2d_model.return_value = {"storage_areas": 0}
    fim.map_derived_max.side_effect = (
        lambda hdf, t, folder, **kw: _derived_frame(Path(folder), kw["products"])
    )
    out = tmp_path / "p05"
    result = fast_map.map_derived(
        tmp_path / "x.p05.hdf", tmp_path / "t.tif", out, map_types=["froude"], render=render,
    )
    assert result == {"froude": [out / "Froude (Max).tif"]}
    kwargs = fim.map_derived_max.call_args.kwargs
    assert kwargs["render_mode"] == "slopingPretty" and kwargs["velocity_depth"] == "rendered"
    assert kwargs["reduce_shallow_to_horizontal"] is render["reduce_shallow_to_horizontal"]
    assert kwargs["use_depth_weighted_faces"] is render["use_depth_weighted_faces"]
    details = json.loads((out / fast_map.FAST_MAP_DERIVED_MANIFEST).read_text())
    assert details["engine_render_mode"] == "slopingPretty"


@patch("ras_commander.RasFim")
def test_plain_sloping_derived_passes_no_sloping_pretty_options(fim, tmp_path, faces_engine):
    fim.validate_2d_model.return_value = {"storage_areas": 0}
    fim.map_derived_max.side_effect = (
        lambda hdf, t, folder, **kw: _derived_frame(Path(folder), kw["products"])
    )
    fast_map.map_derived(
        tmp_path / "x.p05.hdf", tmp_path / "t.tif", tmp_path / "out", map_types=["froude"],
        render=_SLOPING,
    )
    kwargs = fim.map_derived_max.call_args.kwargs
    assert kwargs["render_mode"] == "sloping"
    assert "reduce_shallow_to_horizontal" not in kwargs
    assert "use_depth_weighted_faces" not in kwargs


# ---------------------------------------------------------------------------
# Max shear stress
# ---------------------------------------------------------------------------


@patch("ras_commander.RasFim")
def test_shear_capability_needs_a_true_answer(fim, monkeypatch):
    monkeypatch.setattr(fast_map, "_has_kwargs", lambda function, *names: True)
    fim.supports_shear_stress.return_value = MagicMock()  # truthy, not True
    assert fast_map.shear_supported() is False
    fim.supports_shear_stress.return_value = True
    assert fast_map.shear_supported() is True
    fim.supports_shear_stress.side_effect = RuntimeError("engine missing")
    assert fast_map.shear_supported() is False
    fim.supports_shear_stress.side_effect = None
    monkeypatch.setattr(fast_map, "_has_kwargs", lambda function, *names: False)
    assert fast_map.shear_supported() is False, "ras-commander without shear_stress="


@pytest.mark.parametrize(
    ("render", "enabled", "expected"),
    [
        (_SLOPING, True, "rendered"),
        (_HORIZONTAL, True, "horizontal"),
        (_PRETTY, True, None),
        (_SLOPING, False, None),
        ({**_SLOPING, "storage_areas": 1}, True, None),
    ],
)
def test_shear_plan(faces_engine, render, enabled, expected):
    depth, reason = fast_map.shear_plan(render, enabled)
    assert depth == expected
    assert (reason is None) == (expected is not None)


def test_shear_plan_without_the_method_leaves_it_to_rasmapper(released_engine, monkeypatch):
    monkeypatch.setattr(fast_map, "shear_supported", lambda: False)
    depth, reason = fast_map.shear_plan(_SLOPING)
    assert depth is None and fast_map.SHEAR_STRESS_METHOD in reason


def test_shear_plan_needs_the_clipping_depth(monkeypatch):
    monkeypatch.setattr(fast_map, "shear_supported", lambda: True)
    _engine(monkeypatch, {**_BASE_SLOPING, **_SHEAR, "shear_stress_depth": ["rendered"]})
    assert fast_map.shear_plan(_SLOPING) == ("rendered", None)
    assert fast_map.shear_plan(_HORIZONTAL)[0] is None


@pytest.mark.parametrize(("render", "depth"), [(_SLOPING, "rendered"), (_HORIZONTAL, "horizontal")])
@patch("ras_commander.RasFim")
def test_map_shear_names_the_product_like_rasmapper(fim, tmp_path, faces_engine, render, depth):
    fim.validate_2d_model.return_value = {"storage_areas": 0}

    def fake(hdf, terrain, folder, **kw):
        frame = _manifest(Path(folder), shear_stress_tif=("15_Max_shear_stress.tif", b"shear"))
        frame.attrs["shear_stress"] = {"method": fast_map.SHEAR_STRESS_METHOD, "times": 433}
        return frame

    fim.map_sloping_profiles.side_effect = fake
    out = tmp_path / "p05"
    result = fast_map.map_shear(
        tmp_path / "x.p05.hdf", tmp_path / "t.tif", out, render=render, shear_depth=depth,
        terrain_hdf=tmp_path / "T.hdf", terrain_kind="source",
    )
    assert result == {"shear_stress": [out / "Shear Stress (Max).tif"]}
    assert (out / "Shear Stress (Max).tif").read_bytes() == b"shear"
    assert not (out / "Depth (Max).tif").exists(), "WSE/Depth of this call are discarded"
    kwargs = fim.map_sloping_profiles.call_args.kwargs
    assert kwargs["shear_stress"] is True and kwargs["shear_depth"] == depth
    assert kwargs["render_mode"] == "sloping", "both modes render plain sloping"
    assert kwargs["product_writer"] == "rasterio", "the engine refuses shear with native delivery"
    assert kwargs["terrain_hdf"] == tmp_path / "T.hdf"
    assert "velocity" not in kwargs
    details = json.loads((out / fast_map.FAST_MAP_SHEAR_MANIFEST).read_text())["ras2cng"]
    assert details["shear_depth"] == depth and details["terrain_kind"] == "source"
    assert details["shear_stress"]["times"] == 433
    assert not list(out.glob(".fast-map-shear-*"))


@patch("ras_commander.RasFim")
def test_map_shear_refuses_storage_areas_and_other_profiles(fim, tmp_path, faces_engine, monkeypatch):
    monkeypatch.setenv(fast_map.STORAGE_AREAS_ENV, "1")
    monkeypatch.setattr(fast_map, "storage_areas_supported", lambda: True)
    fim.validate_2d_model.return_value = {"storage_areas": 1}
    with pytest.raises(fast_map.FastMapUnavailable, match="storage-area"):
        fast_map.map_shear(tmp_path / "x.p07.hdf", tmp_path / "t.tif", tmp_path / "o", render=_SLOPING)
    with pytest.raises(fast_map.FastMapUnavailable, match="Max profile"):
        fast_map.map_shear(
            tmp_path / "x.p07.hdf", tmp_path / "t.tif", tmp_path / "o", render=_SLOPING,
            profile="02JAN1999 13:00:00",
        )
    fim.map_sloping_profiles.assert_not_called()


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
    """generate_result_maps with every fast-map group mocked; what ran where."""
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

    monkeypatch.setattr(fast_map, "preflight", lambda plan, r, mode, **kw: dict(render))
    monkeypatch.setattr(fast_map, "map_plan", group(
        "plan", lambda kw: [t for t in kw["map_types"] if t in ("wse", "depth")]))
    monkeypatch.setattr(fast_map, "map_velocity", group("velocity", lambda kw: ["velocity"]))
    monkeypatch.setattr(fast_map, "map_derived", group("derived", lambda kw: kw["map_types"]))
    monkeypatch.setattr(fast_map, "map_shear", group("shear", lambda kw: ["shear_stress"]))
    with patch("ras2cng.mapping.init_ras_project", return_value=ras), \
            patch("ras2cng.mapping._configure_rasprocess"), \
            patch("ras2cng.mapping._generate_plan_maps", return_value={}) as gen:
        results = generate_result_maps(
            project_dir, tmp_path / "maps", map_engine="fast-map", fast_map_terrain=terrain,
            **kwargs,
        )
    from ras2cng.mapping import MAP_TYPE_VARIABLES

    flags = gen.call_args.kwargs if gen.called else {}
    return results[0], calls, {k for k, v in flags.items() if v is True and k in MAP_TYPE_VARIABLES}


@pytest.mark.parametrize("render", [_SLOPING, _HORIZONTAL])
def test_shear_is_routed_to_fast_map_as_its_own_call(tmp_path, monkeypatch, faces_engine, render):
    with patch("ras_commander.RasFim") as fim:
        fim.map_derived_max = MagicMock()
        result, calls, rasmapper = _route(tmp_path, monkeypatch, render, shear_stress=True)
    assert rasmapper == set()
    assert result.engines["shear_stress"] == "fast-map"
    assert calls["shear"]["shear_depth"] == (
        "horizontal" if render["mode"] == "horizontal" else "rendered"
    )
    assert "velocity" in calls and "plan" in calls, "WSE/Depth and velocity keep their own calls"


def test_shear_stays_on_rasmapper_without_the_capability(tmp_path, monkeypatch, released_engine):
    monkeypatch.setattr(fast_map, "shear_supported", lambda: False)
    result, calls, rasmapper = _route(tmp_path, monkeypatch, _SLOPING, shear_stress=True)
    assert "shear" not in calls and rasmapper == {"shear_stress"}
    assert result.engines["shear_stress"] == "rasmapper"
    assert fast_map.SHEAR_STRESS_METHOD in result.fast_map_skipped["shear_stress"]


def test_sloping_pretty_shear_and_no_fast_map_shear_use_rasmapper(tmp_path, monkeypatch, faces_engine):
    with patch("ras_commander.RasFim") as fim:
        fim.map_derived_max = MagicMock()
        result, calls, rasmapper = _route(tmp_path, monkeypatch, _PRETTY, shear_stress=True)
        assert "shear" not in calls and rasmapper == {"shear_stress"}
        assert "velocity" in calls, "slopingPretty velocity is a default with the record"
        (tmp_path / "b").mkdir()
        _, calls, rasmapper = _route(
            tmp_path / "b", monkeypatch, _SLOPING, shear_stress=True, fast_map_shear=False,
        )
    assert "shear" not in calls and rasmapper == {"shear_stress"}


def test_a_shear_failure_falls_back_without_costing_the_other_products(
    tmp_path, monkeypatch, faces_engine
):
    def boom(*args, **kw):
        raise RuntimeError("engine failed")

    with patch("ras_commander.RasFim") as fim:
        fim.map_derived_max = MagicMock()
        ras, project_dir = _fake_ras(tmp_path)
        terrain = tmp_path / "dem.tif"
        terrain.write_bytes(b"x")
        out = tmp_path / "maps" / "p01"
        monkeypatch.setattr(fast_map, "preflight", lambda plan, r, mode, **kw: dict(_SLOPING))
        monkeypatch.setattr(fast_map, "map_plan", lambda *a, **kw: fast_map.FastMapProducts(
            {"wse": [out / "WSE (Max).tif"], "depth": [out / "Depth (Max).tif"]}))
        monkeypatch.setattr(fast_map, "map_velocity", lambda *a, **kw: fast_map.FastMapProducts(
            {"velocity": [out / "Velocity (Max).tif"]}))
        monkeypatch.setattr(fast_map, "map_shear", boom)
        with patch("ras2cng.mapping.init_ras_project", return_value=ras), \
                patch("ras2cng.mapping._configure_rasprocess"), \
                patch("ras2cng.mapping._generate_plan_maps", return_value={}) as gen:
            result = generate_result_maps(
                project_dir, tmp_path / "maps", map_engine="fast-map", fast_map_terrain=terrain,
                shear_stress=True,
            )[0]
    assert gen.call_args.kwargs["shear_stress"] is True
    assert gen.call_args.kwargs["wse"] is False and gen.call_args.kwargs["velocity"] is False
    assert result.engines == {
        "wse": "fast-map", "depth": "fast-map", "velocity": "fast-map",
        "shear_stress": "rasmapper",
    }
    assert "engine failed" in result.fast_map_skipped["shear_stress"]


# ---------------------------------------------------------------------------
# Inundation boundary methods
# ---------------------------------------------------------------------------


def test_resolve_boundary_method(monkeypatch):
    monkeypatch.delenv(fast_map.BOUNDARY_METHOD_ENV, raising=False)
    assert fast_map.resolve_boundary_method() == "deterministic"
    assert fast_map.resolve_boundary_method("native_serial") == "native-serial"
    monkeypatch.setenv(fast_map.BOUNDARY_METHOD_ENV, "Native-Serial")
    assert fast_map.resolve_boundary_method() == "native-serial"
    assert fast_map.resolve_boundary_method("deterministic") == "deterministic"
    with pytest.raises(ValueError, match="boundary method"):
        fast_map.resolve_boundary_method("exact")


@patch("ras_commander.RasFim")
def test_native_boundary_capability(fim, monkeypatch):
    monkeypatch.setattr(fast_map, "_has_kwargs", lambda function, *names: True)
    monkeypatch.setattr(fast_map, "engine_method", lambda kind: {})
    assert fast_map.native_boundary_supported() is False
    monkeypatch.setattr(fast_map, "engine_method", lambda kind: (
        {"method_id": fast_map.NATIVE_BOUNDARY_METHOD} if kind == "native_boundary" else {}))
    assert fast_map.native_boundary_supported() is True
    monkeypatch.setattr(fast_map, "_has_kwargs", lambda function, *names: False)
    assert fast_map.native_boundary_supported() is False


def _boundary_manifest(folder: Path) -> pd.DataFrame:
    geojson = folder / "Max.geojson"
    geojson.write_text('{"type": "FeatureCollection", "features": []}')
    (folder / "boundary_manifest.json").write_text(json.dumps({"status": "complete"}))
    return pd.DataFrame([{
        "profile": "Max", "status": "complete", "boundary_geojson": str(geojson),
        "polygons": 3, "holes": 65, "vertices": 1000, "area": 12.5, "crs": "EPSG:2227",
    }])


@patch("ras_commander.RasFim")
def test_deterministic_boundary_passes_no_method(fim, tmp_path, monkeypatch):
    monkeypatch.delenv(fast_map.BOUNDARY_METHOD_ENV, raising=False)
    fim.validate_2d_model.return_value = {"storage_areas": 0}
    fim.map_inundation_boundaries.side_effect = (
        lambda hdf, t, folder, **kw: _boundary_manifest(Path(folder))
    )
    out = tmp_path / "p02"
    fast_map.map_boundary(
        tmp_path / "x.p02.hdf", tmp_path / "t.tif", tmp_path / "t.hdf", out, render=_SLOPING,
    )
    kwargs = fim.map_inundation_boundaries.call_args.kwargs
    assert "method" not in kwargs and "allow_unqualified" not in kwargs
    details = json.loads((out / fast_map.FAST_MAP_BOUNDARY_MANIFEST).read_text())["ras2cng"]
    assert details["boundary_method"] == "deterministic"
    assert details["method"].startswith(fast_map.DETERMINISTIC_BOUNDARY_METHOD)
    assert "not RAS Mapper vector parity" in details["method"]


@patch("ras_commander.RasFim")
def test_native_serial_boundary_is_an_explicit_unqualified_request(fim, tmp_path, monkeypatch):
    monkeypatch.setattr(fast_map, "native_boundary_supported", lambda: True)
    monkeypatch.setenv(fast_map.STORAGE_AREAS_ENV, "1")
    monkeypatch.setattr(fast_map, "storage_areas_supported", lambda: True)
    fim.validate_2d_model.return_value = {"storage_areas": 1}
    fim.map_inundation_boundaries.side_effect = (
        lambda hdf, t, folder, **kw: _boundary_manifest(Path(folder))
    )
    out = tmp_path / "p07"
    result = fast_map.map_boundary(
        tmp_path / "x.p07.hdf", tmp_path / "t.tif", tmp_path / "t.hdf", out, render=_SLOPING,
        method="native-serial",
    )
    assert result == {"inundation_boundary": [out / "Inundation Boundary (Max).geojson"]}
    kwargs = fim.map_inundation_boundaries.call_args.kwargs
    assert kwargs["method"] == "native_serial" and kwargs["allow_unqualified"] is True
    assert kwargs["interpolation"] == "sloping"
    details = json.loads((out / fast_map.FAST_MAP_BOUNDARY_MANIFEST).read_text())["ras2cng"]
    assert details["boundary_method"] == "native-serial" and details["storage_areas"] == 1
    assert fast_map.NATIVE_BOUNDARY_METHOD in details["method"]
    assert "unqualified" in details["method"]


@patch("ras_commander.RasFim")
def test_native_serial_boundary_refusals(fim, tmp_path, monkeypatch):
    fim.validate_2d_model.return_value = {"storage_areas": 0}
    args = (tmp_path / "x.p02.hdf", tmp_path / "t.tif", tmp_path / "t.hdf", tmp_path / "out")
    monkeypatch.setattr(fast_map, "native_boundary_supported", lambda: True)
    with pytest.raises(fast_map.FastMapUnavailable, match="plain sloping only"):
        fast_map.map_boundary(*args, render=_HORIZONTAL, method="native-serial")
    monkeypatch.setattr(fast_map, "native_boundary_supported", lambda: False)
    with pytest.raises(fast_map.FastMapUnavailable, match="ras-commander>=0.116"):
        fast_map.map_boundary(*args, render=_SLOPING, method="native-serial")
    fim.map_inundation_boundaries.assert_not_called()


def _terrain_h5(path: Path, sources: dict[str, tuple[str, int]]) -> None:
    import h5py
    import numpy as np

    with h5py.File(path, "w") as hdf:
        terrain = hdf.create_group("Terrain")
        for name, (filename, priority) in sources.items():
            group = terrain.create_group(name)
            group.attrs["File"] = np.bytes_(filename)
            if priority is not None:
                group.attrs["Priority"] = np.int32(priority)


def test_native_primary_source_follows_the_stored_priority(tmp_path):
    hdf = tmp_path / "Terrain50.hdf"
    for name in ("Terrain50.a_dem.tif", "Terrain50.z_dtm.tif"):
        (tmp_path / name).write_bytes(b"x")
    _terrain_h5(hdf, {"a_dem": ("Terrain50.a_dem.tif", 1), "z_dtm": ("Terrain50.z_dtm.tif", 0)})
    assert fast_map.native_primary_source(hdf) == tmp_path / "Terrain50.z_dtm.tif"
    # Without a complete set of priorities the HDF's own order is kept.
    _terrain_h5(hdf, {"a_dem": ("Terrain50.a_dem.tif", None), "z_dtm": ("Terrain50.z_dtm.tif", 0)})
    assert fast_map.native_primary_source(hdf) == tmp_path / "Terrain50.a_dem.tif"
    _terrain_h5(hdf, {"a_dem": ("missing.tif", 0)})
    with pytest.raises(fast_map.FastMapUnavailable, match="not found"):
        fast_map.native_primary_source(hdf)


def test_boundary_terrain_takes_every_source_only_for_native_serial(tmp_path):
    project = tmp_path / "project"
    (project / "Terrain").mkdir(parents=True)
    prj = project / "Model.prj"
    prj.write_text("Proj Title=Model\n")
    hdf = project / "Terrain" / "Terrain50.hdf"
    for name in ("Terrain50.a_dem.tif", "Terrain50.z_dtm.tif"):
        (hdf.parent / name).write_bytes(b"x")
    _terrain_h5(hdf, {"a_dem": ("Terrain50.a_dem.tif", 1), "z_dtm": ("Terrain50.z_dtm.tif", 0)})
    resolver = fast_map.TerrainResolver(prj, tmp_path / "work", terrain_hdf=hdf)
    with pytest.raises(fast_map.FastMapUnavailable, match="single-source"):
        resolver.boundary_terrain(tmp_path / "x.p05.hdf")
    tif, terrain_hdf = resolver.boundary_terrain(tmp_path / "x.p05.hdf", "native-serial")
    assert (tif, terrain_hdf) == (hdf.parent / "Terrain50.z_dtm.tif", hdf)


def test_generate_result_maps_validates_the_boundary_method(tmp_path):
    with pytest.raises(ValueError, match="boundary method"):
        generate_result_maps(tmp_path, tmp_path / "out", fast_map_boundary_method="exact")


# ---------------------------------------------------------------------------
# Terrain: the export's CRS-canonical copy
# ---------------------------------------------------------------------------


def _export_resolver(tmp_path, monkeypatch, *, canonical=True):
    resolver = fast_map.TerrainResolver(tmp_path / "a.prj", tmp_path / "w", terrain_name="T")
    monkeypatch.setattr(fast_map, "source_terrain_supported", lambda: True)
    monkeypatch.setattr(fast_map, "effective_terrain_supported", lambda: False)
    monkeypatch.setattr(resolver, "modifications", lambda plan: [])
    monkeypatch.setattr(resolver, "_source", lambda plan: (tmp_path / "src.tif", tmp_path / "T.hdf"))
    monkeypatch.setattr(resolver, "for_plan", lambda plan: tmp_path / "export.tif")
    monkeypatch.setattr(
        resolver, "_canonical",
        lambda tif: tmp_path / f"{tif.stem}.canonical.tif" if canonical else None,
    )
    return resolver


def test_a_crs_refusal_of_the_export_tries_its_canonical_copy(tmp_path, monkeypatch):
    """Davis in slopingPretty: the source terrain is refused for the render mode
    and the export for its CRS identity; the export's canonical copy maps."""
    resolver = _export_resolver(tmp_path, monkeypatch)
    seen = []

    def run(tif, hdf, kind):
        seen.append((kind, tif, hdf))
        if kind == fast_map.TERRAIN_SOURCE:
            raise fast_map.FastMapUnavailable("terrain_hdf is qualified for plain sloping only")
        if kind == fast_map.TERRAIN_EXPORT:
            raise fast_map.FastMapUnavailable("engine terrain CRS differs from rasterio CRS EPSG:2871")
        return "ok"

    result, kind = fast_map.with_terrain(resolver, tmp_path / "a.p02.hdf", run)
    assert (result, kind) == ("ok", fast_map.TERRAIN_EXPORT_CANONICAL)
    assert [k for k, _, _ in seen] == ["source", "export", "export-canonical"]
    assert seen[2][1:] == (tmp_path / "export.canonical.tif", None)


def test_other_export_refusals_do_not_try_the_canonical_copy(tmp_path, monkeypatch):
    resolver = _export_resolver(tmp_path, monkeypatch)
    seen = []

    def run(tif, hdf, kind):
        seen.append(kind)
        if kind == fast_map.TERRAIN_SOURCE:
            # A CRS refusal of the source must not count against the export.
            raise fast_map.FastMapUnavailable("engine terrain CRS differs")
        raise fast_map.FastMapUnavailable("velocity needs face normals")

    with pytest.raises(fast_map.FastMapUnavailable, match="face normals"):
        fast_map.with_terrain(resolver, tmp_path / "a.p02.hdf", run)
    assert seen == ["source", "source-canonical", "export"]


def test_a_modified_terrain_can_still_use_the_exports_canonical_copy(tmp_path, monkeypatch):
    resolver = _export_resolver(tmp_path, monkeypatch)
    monkeypatch.setattr(resolver, "modifications", lambda plan: ["hdf:Channel"])
    seen = []

    def run(tif, hdf, kind):
        seen.append(kind)
        if kind == fast_map.TERRAIN_EXPORT:
            raise fast_map.FastMapUnavailable("engine terrain CRS differs")
        return "ok"

    assert fast_map.with_terrain(resolver, tmp_path / "a.p02.hdf", run)[1] == "export-canonical"
    assert seen == ["export", "export-canonical"], "no Rust terrain step on a modified terrain"


def test_terrain_refusals_are_printed_when_every_candidate_fails(tmp_path, monkeypatch, capsys):
    ras, project_dir = _fake_ras(tmp_path)
    monkeypatch.setattr(fast_map, "preflight", lambda plan, r, mode, **kw: dict(_PRETTY))
    monkeypatch.setattr(fast_map, "source_terrain_supported", lambda: False)
    monkeypatch.setattr(fast_map, "effective_terrain_supported", lambda: False)
    monkeypatch.setattr(fast_map.TerrainResolver, "modifications", lambda self, plan: [])
    monkeypatch.setattr(fast_map.TerrainResolver, "for_plan", lambda self, plan: tmp_path / "export.tif")
    monkeypatch.setattr(fast_map.TerrainResolver, "_canonical", lambda self, tif: None)

    def refuse(*args, **kw):
        raise fast_map.FastMapUnavailable("engine terrain CRS differs from rasterio CRS EPSG:2871")

    monkeypatch.setattr(fast_map, "map_plan", refuse)
    with patch("ras2cng.mapping.init_ras_project", return_value=ras), \
            patch("ras2cng.mapping._configure_rasprocess"), \
            patch("ras2cng.mapping._generate_plan_maps", return_value={}):
        generate_result_maps(project_dir, tmp_path / "maps", map_engine="fast-map", velocity=False)
    output = capsys.readouterr().out.replace("\n", " ")
    assert "export terrain refused" in " ".join(output.split())
