from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import SimpleNamespace
from types import ModuleType

import pytest
from typer.testing import CliRunner

from ras2cng.cli import app
from ras2cng.precipitation import PrecipitationExportResult


runner = CliRunner()

COMMANDS = [
    "inspect",
    "archive",
    "geometry",
    "results",
    "query",
    "pmtiles",
    "sync",
    "terrain",
    "boundary-from-depth",
    "map",
    "terrain-mod",
    "mannings",
]


class DummyFrame:
    def __init__(self, rows: int = 3):
        self.rows = rows
        self.head_limit = None
        self.csv_write = None
        self.parquet_write = None

    def __len__(self):
        return self.rows

    def head(self, limit: int):
        self.head_limit = limit
        return self

    def to_string(self):
        return "col\n1\n2\n3"

    def to_csv(self, output, index: bool = False):
        self.csv_write = {"output": Path(output), "index": index}

    def to_parquet(self, output, index: bool = False):
        self.parquet_write = {"output": Path(output), "index": index}


def install_fake_module(monkeypatch, module_name: str, **attrs):
    module = ModuleType(module_name)
    for name, value in attrs.items():
        setattr(module, name, value)

    monkeypatch.setitem(sys.modules, module_name, module)
    parent_name, attr_name = module_name.rsplit(".", 1)
    parent = importlib.import_module(parent_name)
    monkeypatch.setattr(parent, attr_name, module, raising=False)
    return module


def call_recorder(calls, name: str, return_value=None, fail: bool = False):
    def _record(*args, **kwargs):
        if fail:
            raise RuntimeError(f"{name} failed")
        calls.append({"name": name, "args": args, "kwargs": kwargs})
        return return_value

    return _record


def install_command_backend(monkeypatch, command: str, calls, fail: bool = False):
    if command == "inspect":
        info = object()
        install_fake_module(
            monkeypatch,
            "ras2cng.project",
            inspect_project=call_recorder(calls, "inspect_project", info, fail=fail),
            print_project_info=call_recorder(calls, "print_project_info"),
        )
    elif command == "archive":
        install_fake_module(
            monkeypatch,
            "ras2cng.project",
            archive_project=call_recorder(calls, "archive_project", fail=fail),
        )
    elif command == "geometry":
        install_fake_module(
            monkeypatch,
            "ras2cng.geometry",
            export_geometry_layers=call_recorder(
                calls, "export_geometry_layers", fail=fail
            ),
        )
    elif command == "results":
        install_fake_module(
            monkeypatch,
            "ras2cng.results",
            export_results_layer=call_recorder(calls, "export_results_layer", fail=fail),
            export_all_variables=call_recorder(
                calls, "export_all_variables", ["depth.parquet"], fail=fail
            ),
        )
    elif command == "query":
        install_fake_module(
            monkeypatch,
            "ras2cng.duckdb_session",
            query_parquet=call_recorder(calls, "query_parquet", DummyFrame(), fail=fail),
        )
    elif command == "pmtiles":
        install_fake_module(
            monkeypatch,
            "ras2cng.pmtiles",
            generate_pmtiles_from_input=call_recorder(
                calls, "generate_pmtiles_from_input", fail=fail
            ),
        )
    elif command == "sync":
        install_fake_module(
            monkeypatch,
            "ras2cng.postgis_sync",
            sync_to_postgres=call_recorder(calls, "sync_to_postgres", fail=fail),
        )
    elif command == "terrain":
        install_fake_module(
            monkeypatch,
            "ras2cng.terrain",
            consolidate_terrain=call_recorder(
                calls, "consolidate_terrain", Path("terrain.hdf"), fail=fail
            ),
        )
    elif command == "map":
        install_fake_module(
            monkeypatch,
            "ras2cng.mapping",
            generate_result_maps=call_recorder(calls, "generate_result_maps", fail=fail),
        )
    elif command == "terrain-mod":
        install_fake_module(
            monkeypatch,
            "ras2cng.terrain",
            export_modified_terrain=call_recorder(
                calls, "export_modified_terrain", fail=fail
            ),
        )
    elif command == "mannings":
        install_fake_module(
            monkeypatch,
            "ras2cng.terrain",
            export_mannings_raster=call_recorder(
                calls, "export_mannings_raster", fail=fail
            ),
        )
    else:
        raise AssertionError(f"Unhandled command fixture: {command}")


ERROR_CASES = [
    ("inspect", ["inspect", "model.prj"]),
    ("archive", ["archive", "model.prj", "archive-out"]),
    ("geometry", ["geometry", "model.g01.hdf", "geometry.parquet"]),
    ("results", ["results", "model.p01.hdf", "depth.parquet"]),
    ("query", ["query", "geometry.parquet", "select * from _"]),
    ("pmtiles", ["pmtiles", "geometry.parquet", "geometry.pmtiles"]),
    ("sync", ["sync", "geometry.parquet", "postgresql://host/db", "ras.geom"]),
    ("terrain", ["terrain", "model.prj", "terrain-out"]),
    ("map", ["map", "model.prj", "map-out"]),
    ("terrain-mod", ["terrain-mod", "model.prj", "modified.tif"]),
    ("mannings", ["mannings", "model.prj", "mannings.tif"]),
]


@pytest.mark.parametrize("command", COMMANDS)
def test_subcommand_help_text_generates(command):
    result = runner.invoke(app, [command, "--help"])

    assert result.exit_code == 0, result.output
    assert "Usage:" in result.output
    assert command in result.output


def test_root_help_lists_all_commands():
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0, result.output
    for command in COMMANDS:
        assert command in result.output


@pytest.mark.parametrize("command", COMMANDS)
def test_command_missing_required_arguments_returns_usage_error(command):
    result = runner.invoke(app, [command])

    assert result.exit_code == 2, result.output
    assert "Missing argument" in result.output


@pytest.mark.parametrize(
    ("argv", "bad_value"),
    [
        (["pmtiles", "input.parquet", "output.pmtiles", "--min-zoom", "bad"], "bad"),
        (["terrain", "model.prj", "terrain-out", "--downsample", "bad"], "bad"),
        (["map", "model.prj", "map-out", "--timeout", "bad"], "bad"),
        (["map", "model.prj", "map-out", "--min-depth", "bad"], "bad"),
        (["map", "model.prj", "map-out", "--boundary-method", "bad"], "bad"),
    ],
)
def test_invalid_typed_options_return_usage_error(argv, bad_value):
    result = runner.invoke(app, argv)

    assert result.exit_code == 2, result.output
    assert bad_value in result.output


@pytest.mark.parametrize(("command", "argv"), ERROR_CASES)
def test_backend_exceptions_return_exit_code_one(monkeypatch, command, argv):
    calls = []
    install_command_backend(monkeypatch, command, calls, fail=True)

    result = runner.invoke(app, argv)

    assert result.exit_code == 1, result.output
    assert "failed" in result.output


def test_inspect_passes_project_and_json_flag(monkeypatch):
    calls = []
    install_command_backend(monkeypatch, "inspect", calls)

    result = runner.invoke(app, ["inspect", "model.prj", "--json"])

    assert result.exit_code == 0, result.output
    assert calls[0]["name"] == "inspect_project"
    assert calls[0]["args"] == (Path("model.prj"),)
    assert calls[1]["name"] == "print_project_info"
    assert calls[1]["kwargs"] == {"as_json": True}


def test_archive_defaults(monkeypatch, tmp_path: Path):
    calls = []
    install_command_backend(monkeypatch, "archive", calls)

    result = runner.invoke(app, ["archive", "model.prj", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert calls == [
        {
            "name": "archive_project",
            "args": (Path("model.prj"), tmp_path),
            "kwargs": {
                "include_results": False,
                "include_terrain": False,
                "include_plan_geometry": False,
                "plans": None,
                "result_variables": None,
                "results_layout": "plan",
                "results_geometry": "polygon",
                "include_auxiliary_results": True,
                "skip_errors": True,
                "sort": True,
                "map_results": False,
                "consolidate_terrain": False,
                "terrain_target_resolutions": None,
                "render_mode": None,
                "ras_version": None,
                "rasprocess_path": None,
                "crs": None,
            },
        }
    ]


def test_archive_options_and_flag_pairs(monkeypatch, tmp_path: Path):
    calls = []
    install_command_backend(monkeypatch, "archive", calls)

    result = runner.invoke(
        app,
        [
            "archive",
            "model.prj",
            str(tmp_path),
            "--results",
            "--terrain",
            "--plan-geometry",
            "--plans",
            "p01, p02",
            "--fail-fast",
            "--no-sort",
            "--map",
            "--consolidate-terrain",
            "--terrain-resolution",
            "Existing Terrain=5",
            "--mesh-results-only",
            "--render-mode",
            "slopingPretty",
            "--ras-version",
            "6.6",
            "--rasprocess",
            "C:/RAS",
            "--crs",
            "EPSG:2249",
        ],
    )

    assert result.exit_code == 0, result.output
    kwargs = calls[0]["kwargs"]
    assert kwargs["include_results"] is True
    assert kwargs["include_terrain"] is True
    assert kwargs["include_plan_geometry"] is True
    assert kwargs["plans"] == ["p01", "p02"]
    assert kwargs["skip_errors"] is False
    assert kwargs["sort"] is False
    assert kwargs["map_results"] is True
    assert kwargs["consolidate_terrain"] is True
    assert kwargs["terrain_target_resolutions"] == {"Existing Terrain": 5.0}
    assert kwargs["include_auxiliary_results"] is False
    assert kwargs["render_mode"] == "slopingPretty"
    assert kwargs["ras_version"] == "6.6"
    assert kwargs["rasprocess_path"] == Path("C:/RAS")
    assert kwargs["crs"] == "EPSG:2249"


def test_archive_no_results_flag_overrides_results(monkeypatch, tmp_path: Path):
    calls = []
    install_command_backend(monkeypatch, "archive", calls)

    result = runner.invoke(
        app,
        ["archive", "model.prj", str(tmp_path), "--results", "--no-results"],
    )

    assert result.exit_code == 0, result.output
    assert calls[0]["kwargs"]["include_results"] is False


def test_geometry_passes_layer(monkeypatch, tmp_path: Path):
    calls = []
    install_command_backend(monkeypatch, "geometry", calls)
    output = tmp_path / "geometry.parquet"

    result = runner.invoke(
        app,
        ["geometry", "model.g01.hdf", str(output), "--layer", "mesh_cells"],
    )

    assert result.exit_code == 0, result.output
    assert calls == [
        {
            "name": "export_geometry_layers",
            "args": (Path("model.g01.hdf"), output),
            "kwargs": {"layer": "mesh_cells", "out_crs": "EPSG:4326"},
        }
    ]


def test_results_defaults_to_single_variable_export(monkeypatch, tmp_path: Path):
    calls = []
    install_command_backend(monkeypatch, "results", calls)
    output = tmp_path / "depth.parquet"

    result = runner.invoke(app, ["results", "model.p01.hdf", str(output)])

    assert result.exit_code == 0, result.output
    assert calls == [
        {
            "name": "export_results_layer",
            "args": (Path("model.p01.hdf"), output),
            "kwargs": {"variable": "Maximum Depth", "geom_file": None},
        }
    ]


def test_results_passes_geometry_and_variable(monkeypatch, tmp_path: Path):
    calls = []
    install_command_backend(monkeypatch, "results", calls)
    output = tmp_path / "velocity.parquet"

    result = runner.invoke(
        app,
        [
            "results",
            "model.p01.hdf",
            str(output),
            "--geometry",
            "geometry.parquet",
            "--var",
            "Velocity",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls[0]["name"] == "export_results_layer"
    assert calls[0]["kwargs"] == {
        "variable": "Velocity",
        "geom_file": Path("geometry.parquet"),
    }


def test_results_all_routes_to_export_all(monkeypatch, tmp_path: Path):
    calls = []
    install_command_backend(monkeypatch, "results", calls)

    result = runner.invoke(app, ["results", "model.p01.hdf", str(tmp_path), "--all"])

    assert result.exit_code == 0, result.output
    assert calls == [
        {
            "name": "export_all_variables",
            "args": (Path("model.p01.hdf"), tmp_path),
            "kwargs": {"geom_file": None},
        }
    ]


def test_query_prints_rows_when_no_output_is_requested(monkeypatch):
    frame = DummyFrame(rows=5)

    def fake_query(input_file, sql):
        return frame

    install_fake_module(
        monkeypatch, "ras2cng.duckdb_session", query_parquet=fake_query
    )

    result = runner.invoke(app, ["query", "geometry.parquet", "select * from _"])

    assert result.exit_code == 0, result.output
    assert frame.head_limit == 20
    assert "Query returned 5 rows" in result.output
    assert "col" in result.output


def test_query_writes_csv_output(monkeypatch, tmp_path: Path):
    frame = DummyFrame(rows=2)

    def fake_query(input_file, sql):
        assert input_file == Path("geometry.parquet")
        assert sql == "select * from _"
        return frame

    install_fake_module(
        monkeypatch, "ras2cng.duckdb_session", query_parquet=fake_query
    )
    output = tmp_path / "query.csv"

    result = runner.invoke(
        app,
        ["query", "geometry.parquet", "select * from _", "--output", str(output)],
    )

    assert result.exit_code == 0, result.output
    assert frame.csv_write == {"output": output, "index": False}
    assert frame.parquet_write is None


def test_query_writes_parquet_output_for_non_csv_suffix(monkeypatch, tmp_path: Path):
    frame = DummyFrame(rows=2)

    install_fake_module(
        monkeypatch,
        "ras2cng.duckdb_session",
        query_parquet=lambda input_file, sql: frame,
    )
    output = tmp_path / "query.parquet"

    result = runner.invoke(
        app,
        ["query", "geometry.parquet", "select * from _", "-o", str(output)],
    )

    assert result.exit_code == 0, result.output
    assert frame.parquet_write == {"output": output, "index": False}
    assert frame.csv_write is None


def test_pmtiles_passes_vector_options(monkeypatch, tmp_path: Path):
    calls = []
    install_command_backend(monkeypatch, "pmtiles", calls)
    output = tmp_path / "tiles.pmtiles"

    result = runner.invoke(
        app,
        [
            "pmtiles",
            "geometry.parquet",
            str(output),
            "--layer",
            "flood",
            "--min-zoom",
            "4",
            "--max-zoom",
            "12",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls == [
        {
            "name": "generate_pmtiles_from_input",
            "args": (Path("geometry.parquet"), output),
            "kwargs": {"layer_name": "flood", "min_zoom": 4, "max_zoom": 12},
        }
    ]


def test_sync_passes_schema_and_if_exists(monkeypatch):
    calls = []
    install_command_backend(monkeypatch, "sync", calls)

    result = runner.invoke(
        app,
        [
            "sync",
            "geometry.parquet",
            "postgresql://host/db",
            "mesh_cells",
            "--schema",
            "hydraulics",
            "--if-exists",
            "append",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls == [
        {
            "name": "sync_to_postgres",
            "args": (Path("geometry.parquet"), "postgresql://host/db", "mesh_cells"),
            "kwargs": {"schema": "hydraulics", "if_exists": "append"},
        }
    ]


def test_terrain_passes_options_and_inverted_flags(monkeypatch, tmp_path: Path):
    calls = []
    install_command_backend(monkeypatch, "terrain", calls)

    result = runner.invoke(
        app,
        [
            "terrain",
            "model.prj",
            str(tmp_path),
            "--name",
            "Merged",
            "--downsample",
            "2.5",
            "--resolution",
            "10",
            "--terrains",
            "Terrain A, Terrain B",
            "--units",
            "Meters",
            "--ras-version",
            "6.5",
            "--tiff-only",
            "--no-register",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls == [
        {
            "name": "consolidate_terrain",
            "args": (Path("model.prj"), tmp_path),
            "kwargs": {
                "terrain_name": "Merged",
                "downsample_factor": 2.5,
                "target_resolution": 10.0,
                "terrain_names": ["Terrain A", "Terrain B"],
                "units": "Meters",
                "ras_version": "6.5",
                "create_hdf": False,
                "register_rasmap": False,
            },
        }
    ]


def test_map_defaults(monkeypatch, tmp_path: Path):
    calls = []
    install_command_backend(monkeypatch, "map", calls)

    result = runner.invoke(app, ["map", "model.prj", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert calls == [
        {
            "name": "generate_result_maps",
            "args": (Path("model.prj"), tmp_path),
            "kwargs": {
                "plans": None,
                "profile": "Max",
                "wse": True,
                "depth": True,
                "velocity": True,
                "froude": False,
                "shear_stress": False,
                "depth_x_velocity": False,
                "depth_x_velocity_sq": False,
                "inundation_boundary": False,
                "boundary_method": "rasmapper",
                "boundary_threshold": 0.0,
                "boundary_resolution": None,
                "boundary_max_edges": 5_000_000,
                "arrival_time": False,
                "duration": False,
                "recession": False,
                "percent_inundated": False,
                "arrival_depth": 0.0,
                "terrain_name": None,
                "ras_version": None,
                "rasprocess_path": None,
                "render_mode": None,
                "min_depth": 0.0,
                "reproject_wgs84": False,
                "convert_cog": False,
                "timeout": 10800,
                "skip_errors": True,
                "keep_postprocessing": False,
            },
        }
    ]


def test_map_options_and_flag_pairs(monkeypatch, tmp_path: Path):
    calls = []
    install_command_backend(monkeypatch, "map", calls)

    result = runner.invoke(
        app,
        [
            "map",
            "model.prj",
            str(tmp_path),
            "--plans",
            "p01, p03",
            "--profile",
            "Min",
            "--no-wse",
            "--no-depth",
            "--no-velocity",
            "--froude",
            "--shear-stress",
            "--dv",
            "--dv-sq",
            "--inundation-boundary",
            "--arrival-time",
            "--duration",
            "--recession",
            "--terrain",
            "Terrain A",
            "--render-mode",
            "horizontal",
            "--ras-version",
            "6.6",
            "--rasprocess",
            "C:/RAS",
            "--min-depth",
            "0.2",
            "--wgs84",
            "--cog",
            "--timeout",
            "45",
            "--fail-fast",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls == [
        {
            "name": "generate_result_maps",
            "args": (Path("model.prj"), tmp_path),
            "kwargs": {
                "plans": ["p01", "p03"],
                "profile": "Min",
                "wse": False,
                "depth": False,
                "velocity": False,
                "froude": True,
                "shear_stress": True,
                "depth_x_velocity": True,
                "depth_x_velocity_sq": True,
                "inundation_boundary": True,
                "boundary_method": "rasmapper",
                "boundary_threshold": 0.0,
                "boundary_resolution": None,
                "boundary_max_edges": 5_000_000,
                "arrival_time": True,
                "duration": True,
                "recession": True,
                "percent_inundated": False,
                "arrival_depth": 0.0,
                "terrain_name": "Terrain A",
                "ras_version": "6.6",
                "rasprocess_path": Path("C:/RAS"),
                "render_mode": "horizontal",
                "min_depth": 0.2,
                "reproject_wgs84": True,
                "convert_cog": True,
                "timeout": 45,
                "skip_errors": False,
                "keep_postprocessing": False,
            },
        }
    ]


def test_terrain_mod_passes_geometry_and_terrain(monkeypatch, tmp_path: Path):
    calls = []
    install_command_backend(monkeypatch, "terrain-mod", calls)
    output = tmp_path / "modified.tif"

    result = runner.invoke(
        app,
        [
            "terrain-mod",
            "model.prj",
            str(output),
            "--geometry",
            "g02",
            "--terrain",
            "Terrain A",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls == [
        {
            "name": "export_modified_terrain",
            "args": (Path("model.prj"), output),
            "kwargs": {"geometry": "g02", "terrain_name": "Terrain A"},
        }
    ]


def test_mannings_passes_geometry(monkeypatch, tmp_path: Path):
    calls = []
    install_command_backend(monkeypatch, "mannings", calls)
    output = tmp_path / "mannings.tif"

    result = runner.invoke(
        app, ["mannings", "model.prj", str(output), "--geometry", "g02"]
    )

    assert result.exit_code == 0, result.output
    assert calls == [
        {
            "name": "export_mannings_raster",
            "args": (Path("model.prj"), output),
            "kwargs": {"geometry": "g02"},
        }
    ]


def test_raster_calculate_parses_controlled_inputs(monkeypatch, tmp_path: Path):
    calls = []
    output = tmp_path / "dv.tif"
    install_fake_module(
        monkeypatch,
        "ras2cng.raster_recipes",
        run_raster_recipe=call_recorder(
            calls,
            "run_raster_recipe",
            SimpleNamespace(output_path=output, provenance_path=tmp_path / "dv.provenance.json"),
        ),
    )

    result = runner.invoke(
        app,
        [
            "raster-calculate",
            "depth_velocity",
            str(output),
            "--input", "depth=depth.tif",
            "--input", "velocity=velocity.tif",
            "--input-unit", "depth=ft",
            "--input-unit", "velocity=ft/s",
            "--parameter", "threshold=0.5",
            "--plan", "p03",
            "--profile", "01JAN2026 12:00:00",
            "--block-size", "1024",
            "--hash-assets",
            "--overwrite",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls == [
        {
            "name": "run_raster_recipe",
            "args": (
                "depth_velocity",
                {"depth": Path("depth.tif"), "velocity": Path("velocity.tif")},
                output,
            ),
            "kwargs": {
                "input_units": {"depth": "ft", "velocity": "ft/s"},
                "parameters": {"threshold": 0.5},
                "plan": "p03",
                "profile": "01JAN2026 12:00:00",
                "scratch_dir": None,
                "block_size": 1024,
                "hash_assets": True,
                "overwrite": True,
            },
        }
    ]


def test_boundary_from_depth_passes_bounded_options(monkeypatch, tmp_path: Path):
    calls = []
    output = tmp_path / "boundary.shp"
    provenance = tmp_path / "boundary.raster-derived.provenance.json"
    install_fake_module(
        monkeypatch,
        "ras2cng.boundary",
        derive_inundation_boundary=call_recorder(
            calls,
            "derive_inundation_boundary",
            SimpleNamespace(output_path=output, provenance_path=provenance),
        ),
    )

    result = runner.invoke(
        app,
        [
            "boundary-from-depth",
            str(tmp_path / "Depth (Max)_cog.tif"),
            str(output),
            "--threshold",
            "0.25",
            "--resolution",
            "10",
            "--max-edges",
            "1234",
            "--profile",
            "Max",
            "--units",
            "ft",
            "--source-id",
            "plans/p01/Depth.tif",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls == [
        {
            "name": "derive_inundation_boundary",
            "args": (tmp_path / "Depth (Max)_cog.tif", output),
            "kwargs": {
                "threshold": 0.25,
                "resolution": 10.0,
                "max_edges": 1234,
                "profile": "Max",
                "units": "ft",
                "source_identifier": "plans/p01/Depth.tif",
            },
        }
    ]


def test_map_depth_raster_boundary_options(monkeypatch, tmp_path: Path):
    calls = []
    install_command_backend(monkeypatch, "map", calls)

    result = runner.invoke(
        app,
        [
            "map",
            "model.prj",
            str(tmp_path),
            "--inundation-boundary",
            "--boundary-method",
            "depth-raster",
            "--boundary-threshold",
            "0.2",
            "--boundary-resolution",
            "10",
            "--boundary-max-edges",
            "4321",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls[0]["kwargs"]["boundary_method"] == "depth-raster"
    assert calls[0]["kwargs"]["boundary_threshold"] == 0.2
    assert calls[0]["kwargs"]["boundary_resolution"] == 10.0
    assert calls[0]["kwargs"]["boundary_max_edges"] == 4321


def test_map_depth_raster_boundary_requires_depth(monkeypatch, tmp_path: Path):
    calls = []
    install_command_backend(monkeypatch, "map", calls)

    result = runner.invoke(
        app,
        [
            "map",
            "model.prj",
            str(tmp_path),
            "--inundation-boundary",
            "--boundary-method",
            "depth-raster",
            "--no-depth",
        ],
    )

    assert result.exit_code == 1
    assert "Depth must be enabled" in result.output
    assert calls == []


def test_raster_calculate_rejects_duplicate_roles(tmp_path: Path):
    result = runner.invoke(
        app,
        [
            "raster-calculate",
            "compare_depth",
            str(tmp_path / "out.tif"),
            "--input", "baseline=a.tif",
            "--input", "baseline=b.tif",
        ],
    )
    assert result.exit_code == 1
    assert "more than once" in result.output


def test_maplibre_calculated_map_passes_recipe_provenance(monkeypatch, tmp_path: Path):
    calls = []
    output = tmp_path / "viewer" / "tiles" / "p03-hazard.pmtiles"
    monkeypatch.setattr(
        "ras2cng.maplibre.package_maplibre_calculated_map",
        call_recorder(
            calls,
            "package_maplibre_calculated_map",
            SimpleNamespace(pmtiles_path=output, layer_id="p03-hazard"),
        ),
    )

    result = runner.invoke(
        app,
        [
            "maplibre-calculated-map",
            str(tmp_path / "hazard.tif"),
            str(tmp_path / "viewer"),
            "--plan", "p03",
            "--recipe", "hazard_class",
            "--profile", "01JAN2026 12:00:00",
            "--geometry", "g03",
            "--provenance", str(tmp_path / "hazard.provenance.json"),
            "--overwrite",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls[0]["args"] == (tmp_path / "hazard.tif", tmp_path / "viewer")
    assert calls[0]["kwargs"]["recipe_id"] == "hazard_class"
    assert calls[0]["kwargs"]["provenance_path"] == tmp_path / "hazard.provenance.json"
    assert calls[0]["kwargs"]["overwrite"] is True


def test_raster_service_catalog_attaches_manifests(monkeypatch, tmp_path: Path):
    calls = []
    output = tmp_path / "raster-assets.json"
    monkeypatch.setattr(
        "ras2cng.webgis_service.build_raster_asset_catalog",
        call_recorder(calls, "build_raster_asset_catalog", output),
    )

    result = runner.invoke(
        app,
        [
            "raster-service-catalog",
            str(tmp_path / "data"),
            str(output),
            "--manifest", str(tmp_path / "viewer" / "manifest.json"),
            "--service-base-url", "/data/ras-raster",
            "--attach-manifests",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls[0]["kwargs"]["manifest_paths"] == [tmp_path / "viewer" / "manifest.json"]
    assert calls[0]["kwargs"]["service_base_url"] == "/data/ras-raster"
    assert calls[0]["kwargs"]["attach_manifests"] is True


def test_raster_service_rejects_non_loopback_listener(tmp_path: Path):
    result = runner.invoke(
        app,
        [
            "raster-service",
            str(tmp_path / "catalog.json"),
            str(tmp_path / "data"),
            "--host", "0.0.0.0",
        ],
    )

    assert result.exit_code == 1
    assert "loopback" in result.output


def test_precip_routes_to_export_precipitation(monkeypatch, tmp_path: Path):
    called = {"kwargs": None}

    def fake_export_precipitation(hdf_file, output, **kwargs):
        called["kwargs"] = kwargs
        assert Path(hdf_file) == Path("model.p01.hdf")
        assert Path(output) == tmp_path
        return PrecipitationExportResult(
            source_hdf=Path(hdf_file),
            source="processed",
            values_path="Event Conditions/Meteorology/Precipitation/Values",
            output_dir=Path(output),
            units="in",
            rows=2,
            cols=2,
            timestamps=["0", "2"],
            incremental=[tmp_path / "a.tif", tmp_path / "b.tif"],
            cumulative=[],
        )

    monkeypatch.setattr(
        "ras2cng.precipitation.export_precipitation_rasters",
        fake_export_precipitation,
    )

    result = runner.invoke(
        app,
        [
            "precip",
            "model.p01.hdf",
            str(tmp_path),
            "--source",
            "processed",
            "--timestamps",
            "0,2",
            "--no-cumulative",
            "--prefix",
            "rain",
        ],
    )

    assert result.exit_code == 0
    assert called["kwargs"] == {
        "source": "processed",
        "timestamps": ["0", "2"],
        "units": "native",
        "export_incremental": True,
        "export_cumulative": False,
        "prefix": "rain",
        "overwrite": True,
    }



# ---------------------------------------------------------------------------
# map-hdf
# ---------------------------------------------------------------------------

def test_map_hdf_requires_exactly_one_terrain_source(tmp_path: Path):
    hdf = tmp_path / "model.p01.hdf"
    hdf.touch()

    # Neither terrain option
    result = runner.invoke(app, ["map-hdf", str(hdf), str(tmp_path / "out")])
    assert result.exit_code == 1
    assert "exactly one" in result.output

    # Both terrain options
    result = runner.invoke(app, [
        "map-hdf", str(hdf), str(tmp_path / "out"),
        "--terrain", str(tmp_path / "dem.tif"),
        "--terrain-hdf", str(tmp_path / "Terrain.hdf"),
    ])
    assert result.exit_code == 1
    assert "exactly one" in result.output


def test_map_hdf_scaffolds_then_generates(monkeypatch, tmp_path: Path):
    from ras2cng.scaffold import PlanHdfMetadata, ScaffoldInfo

    hdf = tmp_path / "model.p01.hdf"
    hdf.touch()
    dem = tmp_path / "dem.tif"
    dem.touch()
    outdir = tmp_path / "out"

    scaffold_calls = {}
    maps_calls = {}

    meta = PlanHdfMetadata(
        project_name="Model", plan_number="01", plan_title="T",
        plan_short_id="S", project_title="P", geom_ext="g01", flow_ext="u01",
        units="US Customary", projection_wkt="WKT",
        sim_start="01Jan2000 00:00:00", sim_end="02Jan2000 00:00:00",
        file_version="HEC-RAS 6.6",
    )

    def fake_build_scaffold(plan_hdf, workdir, **kwargs):
        scaffold_calls.update(kwargs, plan_hdf=plan_hdf, workdir=workdir)
        return ScaffoldInfo(
            project_dir=Path(workdir),
            prj_file=Path(workdir) / "Model.prj",
            plan_hdf=Path(workdir) / "Model.p01.hdf",
            terrain_hdf=Path(workdir) / "Terrain" / "Terrain.hdf",
            meta=meta,
        )

    def fake_generate(prj_file, output, **kwargs):
        maps_calls.update(kwargs, prj_file=prj_file, output=output)
        return []

    monkeypatch.setattr("ras2cng.scaffold.build_scaffold", fake_build_scaffold)
    monkeypatch.setattr("ras2cng.mapping.generate_result_maps", fake_generate)

    result = runner.invoke(app, [
        "map-hdf", str(hdf), str(outdir),
        "--terrain", str(dem),
        "--profile", "Max", "--froude", "--ras-version", "6.6",
        "--inundation-boundary", "--boundary-method", "depth-raster",
        "--boundary-threshold", "0.3", "--boundary-resolution", "20",
        "--boundary-max-edges", "999",
    ])
    assert result.exit_code == 0, result.output

    # Scaffold call: default workdir under output, terrain passthrough
    assert scaffold_calls["plan_hdf"] == hdf
    assert scaffold_calls["workdir"] == outdir / "_scaffold"
    assert scaffold_calls["terrain_tifs"] == [dem]
    assert scaffold_calls["terrain_hdf"] is None
    assert scaffold_calls["ras_version"] == "6.6"

    # Map call: specific .prj file, single-plan filter, flags pass through
    assert maps_calls["prj_file"].name == "Model.prj"
    assert maps_calls["plans"] == ["p01"]
    assert maps_calls["froude"] is True
    assert maps_calls["boundary_method"] == "depth-raster"
    assert maps_calls["boundary_threshold"] == 0.3
    assert maps_calls["boundary_resolution"] == 20.0
    assert maps_calls["boundary_max_edges"] == 999
    assert maps_calls["skip_errors"] is False


def test_map_hdf_depth_raster_boundary_requires_depth(tmp_path: Path):
    hdf = tmp_path / "model.p01.hdf"
    hdf.touch()
    dem = tmp_path / "dem.tif"
    dem.touch()

    result = runner.invoke(
        app,
        [
            "map-hdf",
            str(hdf),
            str(tmp_path / "out"),
            "--terrain",
            str(dem),
            "--inundation-boundary",
            "--boundary-method",
            "depth-raster",
            "--no-depth",
        ],
    )

    assert result.exit_code == 1
    assert "Depth must be enabled" in result.output


def test_map_hdf_rm_scaffold_cleans_up(monkeypatch, tmp_path: Path):
    from ras2cng.scaffold import PlanHdfMetadata, ScaffoldInfo

    hdf = tmp_path / "model.p01.hdf"
    hdf.touch()
    dem = tmp_path / "dem.tif"
    dem.touch()
    outdir = tmp_path / "out"

    meta = PlanHdfMetadata(
        project_name="Model", plan_number="01", plan_title="T",
        plan_short_id="S", project_title="P", geom_ext="g01", flow_ext="u01",
        units="US Customary", projection_wkt="WKT",
        sim_start="01Jan2000 00:00:00", sim_end="02Jan2000 00:00:00",
        file_version="HEC-RAS 6.6",
    )

    def fake_build_scaffold(plan_hdf, workdir, **kwargs):
        from ras2cng.scaffold import SCAFFOLD_MARKER

        Path(workdir).mkdir(parents=True, exist_ok=True)
        (Path(workdir) / "Model.prj").write_text("stub")
        (Path(workdir) / SCAFFOLD_MARKER).write_text("{}")
        return ScaffoldInfo(
            project_dir=Path(workdir),
            prj_file=Path(workdir) / "Model.prj",
            plan_hdf=Path(workdir) / "Model.p01.hdf",
            terrain_hdf=Path(workdir) / "Terrain" / "Terrain.hdf",
            meta=meta,
        )

    monkeypatch.setattr("ras2cng.scaffold.build_scaffold", fake_build_scaffold)
    monkeypatch.setattr(
        "ras2cng.mapping.generate_result_maps", lambda *a, **k: []
    )

    result = runner.invoke(app, [
        "map-hdf", str(hdf), str(outdir), "--terrain", str(dem), "--rm-scaffold",
    ])
    assert result.exit_code == 0, result.output
    assert not (outdir / "_scaffold").exists()


def test_map_hdf_rm_scaffold_never_deletes_foreign_workdir(monkeypatch, tmp_path: Path):
    """--rm-scaffold must not delete a user directory build_scaffold rejected."""
    hdf = tmp_path / "model.p01.hdf"
    hdf.touch()
    dem = tmp_path / "dem.tif"
    dem.touch()

    user_dir = tmp_path / "precious_data"
    user_dir.mkdir()
    (user_dir / "keep_me.txt").write_text("important")

    def fake_build_scaffold(plan_hdf, workdir, **kwargs):
        raise ValueError("not a previous ras2cng scaffold")

    monkeypatch.setattr("ras2cng.scaffold.build_scaffold", fake_build_scaffold)

    result = runner.invoke(app, [
        "map-hdf", str(hdf), str(tmp_path / "out"),
        "--terrain", str(dem),
        "--workdir", str(user_dir), "--rm-scaffold",
    ])
    assert result.exit_code == 1
    assert user_dir.exists()
    assert (user_dir / "keep_me.txt").read_text() == "important"
