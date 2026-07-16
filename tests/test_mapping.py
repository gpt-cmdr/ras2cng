"""
Unit tests for mapping.py — result raster generation via RasProcess.

All tests are fully mocked -- no real HEC-RAS files or RasProcess.exe needed.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from ras2cng.mapping import (
    MapResult,
    MAP_TYPE_VARIABLES,
    generate_result_maps,
    _build_requested_types,
    _configure_rasprocess,
    _convert_to_cog,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_fake_ras(tmp_path, plan_count=1, plan_hdf_exists=True):
    """Build a fake RasPrj-like object with minimal DataFrames."""
    project_dir = tmp_path / "project"
    project_dir.mkdir(exist_ok=True)
    prj = project_dir / "TestModel.prj"
    prj.write_text("Proj Title=Test\nEnglish Units\n")

    plan_rows = []
    for i in range(1, plan_count + 1):
        plan_num = str(i).zfill(2)
        plan_rows.append({
            "plan_number": plan_num,
            "geometry_number": "01",
            "Plan Title": f"Plan {i}",
        })
        if plan_hdf_exists:
            (project_dir / f"TestModel.p{plan_num}.hdf").touch()

    ras = MagicMock()
    ras.project_name = "TestModel"
    ras.project_folder = str(project_dir)
    ras.plan_df = pd.DataFrame(plan_rows) if plan_rows else pd.DataFrame()
    ras.geom_df = pd.DataFrame()
    ras.results_df = pd.DataFrame()

    return ras, project_dir, prj


# ---------------------------------------------------------------------------
# _build_requested_types
# ---------------------------------------------------------------------------

def test_build_requested_types_defaults():
    result = _build_requested_types(
        wse=True, depth=True, velocity=True,
        froude=False, shear_stress=False,
        depth_x_velocity=False,
        depth_x_velocity_sq=False,
        inundation_boundary=False,
        arrival_time=False, duration=False,
        recession=False,
    )
    assert result == ["wse", "depth", "velocity"]


def test_build_requested_types_all_enabled():
    result = _build_requested_types(
        wse=True, depth=True, velocity=True,
        froude=True, shear_stress=True,
        depth_x_velocity=True,
        depth_x_velocity_sq=True,
        inundation_boundary=True,
        arrival_time=True, duration=True,
        recession=True,
    )
    assert len(result) == 11


def test_build_requested_types_none_enabled():
    result = _build_requested_types(
        wse=False, depth=False, velocity=False,
        froude=False, shear_stress=False,
        depth_x_velocity=False,
        depth_x_velocity_sq=False,
        inundation_boundary=False,
        arrival_time=False, duration=False,
        recession=False,
    )
    assert result == []


# ---------------------------------------------------------------------------
# MAP_TYPE_VARIABLES
# ---------------------------------------------------------------------------

def test_map_type_variables_keys():
    expected = {
        "wse", "depth", "velocity", "froude", "shear_stress",
        "depth_x_velocity", "depth_x_velocity_sq", "inundation_boundary",
        "arrival_time", "duration", "recession", "percent_inundated",
    }
    assert set(MAP_TYPE_VARIABLES.keys()) == expected


# ---------------------------------------------------------------------------
# ADR: capability detection, rasmap injection shim, recession derivation
# ---------------------------------------------------------------------------

def test_store_maps_native_adr_detection():
    from ras2cng.mapping import _store_maps_supports_native_adr

    def native_signature(plan_number, arrival_time=False, arrival_depth=0.0, **kw):
        pass

    def legacy_signature(plan_number, wse=True, depth=True, **kw):
        pass

    with patch("ras2cng.mapping.RasProcess") as mock_rp:
        mock_rp.store_maps = native_signature
        assert _store_maps_supports_native_adr() is True
        mock_rp.store_maps = legacy_signature
        assert _store_maps_supports_native_adr() is False


def test_inject_adr_stored_maps_xml(tmp_path):
    import xml.etree.ElementTree as ET
    from ras2cng.mapping import _inject_adr_stored_maps

    rasmap = tmp_path / "Model.rasmap"
    rasmap.write_text(
        '<RASMapper><Results Checked="True" /></RASMapper>', encoding="utf-8"
    )

    _inject_adr_stored_maps(
        rasmap,
        "Model.p01.hdf",
        {"arrival_time": True, "duration": True, "percent_inundated": False},
        arrival_depth=0.25,
    )

    root = ET.parse(rasmap).getroot()
    plan_layers = root.findall(".//Results/Layer")
    assert len(plan_layers) == 1
    assert plan_layers[0].get("Filename") == ".\\Model.p01.hdf"

    params = root.findall(".//MapParameters")
    map_types = {p.get("MapType") for p in params}
    assert map_types == {"arrival time", "duration"}
    assert all(p.get("ArrivalDepth") == "0.25" for p in params)
    assert all(p.get("ProfileIndex") == "2147483647" for p in params)
    assert all(p.get("OutputMode") == "Stored Current Terrain" for p in params)


def test_inject_adr_reuses_existing_plan_layer(tmp_path):
    import xml.etree.ElementTree as ET
    from ras2cng.mapping import _inject_adr_stored_maps

    rasmap = tmp_path / "Model.rasmap"
    rasmap.write_text(
        '<RASMapper><Results Checked="True">'
        '<Layer Name="P1" Type="RASResults" Filename=".\\Model.p01.hdf" />'
        "</Results></RASMapper>",
        encoding="utf-8",
    )

    _inject_adr_stored_maps(
        rasmap, "Model.p01.hdf", {"arrival_time": True}, arrival_depth=0.0
    )

    root = ET.parse(rasmap).getroot()
    assert len(root.findall(".//Results/Layer")) == 1  # no duplicate plan layer
    assert len(root.findall(".//MapParameters")) == 1


def test_generate_plan_maps_shim_injects_and_restores(tmp_path):
    """Older ras-commander (no native ADR kwargs): rasmap pre-injection shim."""
    import xml.etree.ElementTree as ET
    from ras2cng.mapping import _generate_plan_maps

    ras, project_dir, _ = _make_fake_ras(tmp_path)
    ras.project_folder = project_dir  # Path for the shim's rasmap resolution
    rasmap = project_dir / "TestModel.rasmap"
    original_xml = '<RASMapper><Results Checked="True" /></RASMapper>'
    rasmap.write_text(original_xml, encoding="utf-8")
    output_dir = tmp_path / "out"
    output_dir.mkdir()

    captured = {}

    def legacy_store_maps(plan_number, output_path=None, **kwargs):
        # Signature has no arrival_time -> shim path expected
        captured["rasmap_at_call"] = rasmap.read_text(encoding="utf-8")
        captured["kwargs"] = kwargs
        # Simulate store_maps' move-loop relocating the ADR outputs
        (output_dir / "Arrival Time (0.5ft hrs).TileA.tif").write_text("a")
        (output_dir / "Duration (0.5ft hrs).TileA.tif").write_text("d")
        return {"wse": []}

    with patch("ras2cng.mapping.RasProcess") as mock_rp:
        mock_rp.store_maps = legacy_store_maps
        mock_rp._remove_stored_maps_from_rasmap = MagicMock(return_value=0)
        result = _generate_plan_maps(
            ras=ras,
            plan_number="01",
            profile="Max",
            output_dir=output_dir,
            arrival_depth=0.5,
            wse=False, depth=False, velocity=False,
            arrival_time=True, duration=True,
        )

    # Injection was visible to store_maps
    root = ET.fromstring(captured["rasmap_at_call"])
    map_types = {p.get("MapType") for p in root.findall(".//MapParameters")}
    assert map_types == {"arrival time", "duration"}
    assert captured["kwargs"]["clear_existing"] is False
    mock_rp._remove_stored_maps_from_rasmap.assert_called_once()

    # rasmap restored afterwards and backup removed
    assert rasmap.read_text(encoding="utf-8") == original_xml
    assert not rasmap.with_suffix(".rasmap.adrbak").exists()

    # ADR outputs collected by glob
    assert [p.name for p in result["arrival_time"]] == [
        "Arrival Time (0.5ft hrs).TileA.tif"
    ]
    assert [p.name for p in result["duration"]] == [
        "Duration (0.5ft hrs).TileA.tif"
    ]


def test_generate_plan_maps_binds_every_requested_map_to_named_terrain(tmp_path):
    import xml.etree.ElementTree as ET
    from ras2cng.mapping import _generate_plan_maps

    ras, project_dir, _ = _make_fake_ras(tmp_path)
    ras.project_folder = project_dir
    rasmap = project_dir / "TestModel.rasmap"
    original_xml = (
        '<RASMapper><Results Checked="True" />'
        '<Terrains><Layer Name="Terrain" /><Layer Name="TerrainWithChannel" />'
        "</Terrains></RASMapper>"
    )
    rasmap.write_text(original_xml, encoding="utf-8")
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    captured = {}

    def legacy_store_maps(plan_number, output_path=None, **kwargs):
        captured["rasmap_at_call"] = rasmap.read_text(encoding="utf-8")
        captured["kwargs"] = kwargs
        (output_dir / "WSE (Max).TerrainWithChannel.tif").write_text("wse")
        (output_dir / "D _ V (Max).TerrainWithChannel.tif").write_text("dv")
        (output_dir / "Arrival Time (0.1ft hrs).TerrainWithChannel.tif").write_text(
            "arrival"
        )
        return {}

    with patch("ras2cng.mapping.RasProcess") as mock_rp:
        mock_rp.store_maps = legacy_store_maps
        mock_rp._remove_stored_maps_from_rasmap = MagicMock(return_value=0)
        result = _generate_plan_maps(
            ras=ras,
            plan_number="01",
            profile="Max",
            output_dir=output_dir,
            terrain_name="TerrainWithChannel",
            arrival_depth=0.1,
            wse=True,
            depth=False,
            velocity=False,
            depth_x_velocity=True,
            arrival_time=True,
        )

    root = ET.fromstring(captured["rasmap_at_call"])
    params = root.findall(".//MapParameters")
    assert {item.get("MapType") for item in params} == {
        "elevation",
        "depth and velocity",
        "arrival time",
    }
    assert all(item.get("Terrain") == "TerrainWithChannel" for item in params)
    assert all(item.get("OutputMode") == "Stored Specified Terrain" for item in params)
    assert captured["kwargs"]["wse"] is False
    assert captured["kwargs"]["depth_x_velocity"] is False
    assert captured["kwargs"]["clear_existing"] is False
    assert [path.name for path in result["wse"]] == ["WSE (Max).TerrainWithChannel.tif"]
    assert [path.name for path in result["depth_x_velocity"]] == [
        "D _ V (Max).TerrainWithChannel.tif"
    ]
    assert [path.name for path in result["arrival_time"]] == [
        "Arrival Time (0.1ft hrs).TerrainWithChannel.tif"
    ]
    assert rasmap.read_text(encoding="utf-8") == original_xml


def test_generate_plan_maps_stale_adrbak_never_restored(tmp_path):
    """A leftover .adrbak from a killed prior run must not clobber the rasmap."""
    from ras2cng.mapping import _generate_plan_maps

    ras, project_dir, _ = _make_fake_ras(tmp_path)
    ras.project_folder = project_dir
    rasmap = project_dir / "TestModel.rasmap"
    current_xml = '<RASMapper><Results Checked="True"><!-- user edits --></Results></RASMapper>'
    rasmap.write_text(current_xml, encoding="utf-8")

    stale_backup = project_dir / "TestModel.rasmap.adrbak"
    stale_backup.write_text("<RASMapper><!-- months old --></RASMapper>", encoding="utf-8")

    output_dir = tmp_path / "out"
    output_dir.mkdir()

    with patch("ras2cng.mapping.RasProcess") as mock_rp:
        mock_rp.store_maps = lambda **kwargs: {"wse": []}
        _generate_plan_maps(
            ras=ras, plan_number="01", profile="Max", output_dir=output_dir,
            wse=True, depth=False, velocity=False,
        )

    # Current rasmap untouched, stale backup discarded
    assert rasmap.read_text(encoding="utf-8") == current_xml
    assert not stale_backup.exists()


def test_generate_plan_maps_shim_ignores_stale_adr_outputs(tmp_path):
    """ADR glob must not claim rasters from a previous run at another threshold."""
    import os as _os
    from ras2cng.mapping import _generate_plan_maps

    ras, project_dir, _ = _make_fake_ras(tmp_path)
    ras.project_folder = project_dir
    (project_dir / "TestModel.rasmap").write_text(
        '<RASMapper><Results Checked="True" /></RASMapper>', encoding="utf-8"
    )
    output_dir = tmp_path / "out"
    output_dir.mkdir()

    # Stale raster from a previous run at a different threshold
    stale = output_dir / "Arrival Time (0.1ft hrs).TileA.tif"
    stale.write_text("old")
    old_time = 946684800  # year 2000
    _os.utime(stale, (old_time, old_time))

    def legacy_store_maps(plan_number, output_path=None, **kwargs):
        (output_dir / "Arrival Time (0.5ft hrs).TileA.tif").write_text("new")
        return {}

    with patch("ras2cng.mapping.RasProcess") as mock_rp:
        mock_rp.store_maps = legacy_store_maps
        mock_rp._remove_stored_maps_from_rasmap = MagicMock(return_value=0)
        result = _generate_plan_maps(
            ras=ras, plan_number="01", profile="Max", output_dir=output_dir,
            arrival_depth=0.5,
            wse=False, depth=False, velocity=False, arrival_time=True,
        )

    assert [p.name for p in result["arrival_time"]] == [
        "Arrival Time (0.5ft hrs).TileA.tif"
    ]


def test_generate_plan_maps_native_passthrough(tmp_path):
    """Newer ras-commander: ADR kwargs passed straight through, no injection."""
    from ras2cng.mapping import _generate_plan_maps

    ras, project_dir, _ = _make_fake_ras(tmp_path)
    ras.project_folder = project_dir
    rasmap = project_dir / "TestModel.rasmap"
    original_xml = '<RASMapper><Results Checked="True" /></RASMapper>'
    rasmap.write_text(original_xml, encoding="utf-8")
    output_dir = tmp_path / "out"
    output_dir.mkdir()

    captured = {}

    def native_store_maps(plan_number, output_path=None, arrival_time=False,
                          duration=False, percent_inundated=False,
                          arrival_depth=0.0, **kwargs):
        captured["rasmap_at_call"] = rasmap.read_text(encoding="utf-8")
        captured["arrival_time"] = arrival_time
        captured["arrival_depth"] = arrival_depth
        return {
            "arrival_time": [output_dir / "Arrival Time (0.5ft hrs).TileA.tif"],
        }

    (output_dir / "Arrival Time (0.5ft hrs).TileA.tif").write_text("a")

    with patch("ras2cng.mapping.RasProcess") as mock_rp:
        mock_rp.store_maps = native_store_maps
        result = _generate_plan_maps(
            ras=ras,
            plan_number="01",
            profile="Max",
            output_dir=output_dir,
            arrival_depth=0.5,
            wse=False, depth=False, velocity=False,
            arrival_time=True,
        )

    assert captured["arrival_time"] is True
    assert captured["arrival_depth"] == 0.5
    # No injection happened — rasmap untouched at call time
    assert captured["rasmap_at_call"] == original_xml
    assert [p.name for p in result["arrival_time"]] == [
        "Arrival Time (0.5ft hrs).TileA.tif"
    ]


# ---------------------------------------------------------------------------
# _configure_rasprocess (mocked)
# ---------------------------------------------------------------------------

@patch("ras2cng.mapping.RasProcess")
def test_configure_rasprocess_with_explicit_path(mock_rp):
    exe_path = Path("/usr/bin/RasProcess.exe")
    # The wine branch only runs on Linux; pin the platform so the test is
    # deterministic regardless of host OS.
    with patch("ras2cng.mapping.platform.system", return_value="Linux"):
        _configure_rasprocess(rasprocess_path=exe_path)
    # configure_wine receives the directory containing RasProcess.exe
    mock_rp.configure_wine.assert_called_once_with(ras_install_dir=str(exe_path.parent))


@patch("ras2cng.mapping.RasProcess")
def test_configure_rasprocess_with_version(mock_rp):
    _configure_rasprocess(ras_version="6.6")
    mock_rp.find_rasprocess.assert_called_once_with(version="6.6")


@patch("ras2cng.mapping.RasProcess")
def test_configure_rasprocess_no_args(mock_rp):
    _configure_rasprocess()
    mock_rp.configure_wine.assert_not_called()
    mock_rp.find_rasprocess.assert_not_called()


# ---------------------------------------------------------------------------
# generate_result_maps (mocked)
# ---------------------------------------------------------------------------

@patch("ras2cng.mapping._generate_plan_maps")
@patch("ras2cng.mapping._configure_rasprocess")
@patch("ras2cng.mapping.init_ras_project")
def test_generate_result_maps_basic(mock_init, mock_config, mock_gen, tmp_path):
    """generate_result_maps should process all plans and return MapResult list."""
    ras, project_dir, prj = _make_fake_ras(tmp_path, plan_count=1)
    mock_init.return_value = ras
    output_dir = tmp_path / "maps"

    # Mock plan map generation to return a dict of map type -> TIF paths
    fake_tif = output_dir / "p01" / "depth.tif"
    fake_tif.parent.mkdir(parents=True, exist_ok=True)
    fake_tif.write_bytes(b"fake tif")
    mock_gen.return_value = {"depth": [fake_tif]}

    results = generate_result_maps(
        project_dir,
        output_dir,
        wse=False, depth=True, velocity=False,
    )

    assert len(results) == 1
    assert results[0].plan_id == "p01"
    assert "depth" in results[0].map_types
    assert len(results[0].map_types["depth"]) == 1


@patch("ras2cng.mapping._generate_plan_maps")
@patch("ras2cng.mapping._configure_rasprocess")
@patch("ras2cng.mapping.init_ras_project")
def test_generate_result_maps_plan_filtering(mock_init, mock_config, mock_gen, tmp_path):
    """generate_result_maps should filter to specified plans."""
    ras, project_dir, prj = _make_fake_ras(tmp_path, plan_count=3)
    mock_init.return_value = ras
    output_dir = tmp_path / "maps"

    mock_gen.return_value = {}

    results = generate_result_maps(
        project_dir, output_dir,
        plans=["p02"],
        wse=False, depth=True, velocity=False,
    )

    # Should only process p02
    assert len(results) == 1
    assert results[0].plan_id == "p02"


@patch("ras2cng.mapping._generate_plan_maps")
@patch("ras2cng.mapping._configure_rasprocess")
@patch("ras2cng.mapping.init_ras_project")
def test_generate_result_maps_skips_missing_hdf(mock_init, mock_config, mock_gen, tmp_path):
    """Plans without HDF files should be skipped."""
    ras, project_dir, prj = _make_fake_ras(tmp_path, plan_count=1, plan_hdf_exists=False)
    mock_init.return_value = ras
    output_dir = tmp_path / "maps"

    results = generate_result_maps(project_dir, output_dir, depth=True)

    assert len(results) == 0
    mock_gen.assert_not_called()


@patch("ras2cng.mapping._generate_plan_maps")
@patch("ras2cng.mapping._configure_rasprocess")
@patch("ras2cng.mapping.init_ras_project")
def test_generate_result_maps_error_handling(mock_init, mock_config, mock_gen, tmp_path):
    """Errors should be recorded but not raised when skip_errors=True."""
    ras, project_dir, prj = _make_fake_ras(tmp_path, plan_count=1)
    mock_init.return_value = ras
    output_dir = tmp_path / "maps"

    mock_gen.side_effect = RuntimeError("RasProcess failed")

    results = generate_result_maps(
        project_dir, output_dir,
        wse=False, depth=True, velocity=False,
        skip_errors=True,
    )

    assert len(results) == 1
    assert len(results[0].errors) == 1
    assert "RasProcess failed" in results[0].errors[0]


@patch("ras2cng.mapping._generate_plan_maps")
@patch("ras2cng.mapping._configure_rasprocess")
@patch("ras2cng.mapping.init_ras_project")
def test_generate_result_maps_error_raises_when_fail_fast(mock_init, mock_config, mock_gen, tmp_path):
    """Errors should raise when skip_errors=False."""
    ras, project_dir, prj = _make_fake_ras(tmp_path, plan_count=1)
    mock_init.return_value = ras
    output_dir = tmp_path / "maps"

    mock_gen.side_effect = RuntimeError("RasProcess failed")

    with pytest.raises(RuntimeError, match="RasProcess failed"):
        generate_result_maps(
            project_dir, output_dir,
            wse=False, depth=True, velocity=False,
            skip_errors=False,
        )


@patch("ras2cng.mapping._generate_plan_maps")
@patch("ras2cng.mapping._configure_rasprocess")
@patch("ras2cng.mapping.init_ras_project")
def test_generate_result_maps_no_types_selected(mock_init, mock_config, mock_gen, tmp_path):
    """Should return empty list when no map types are selected."""
    ras, project_dir, prj = _make_fake_ras(tmp_path, plan_count=1)
    mock_init.return_value = ras
    output_dir = tmp_path / "maps"

    results = generate_result_maps(
        project_dir, output_dir,
        wse=False, depth=False, velocity=False,
    )

    assert results == []
    mock_gen.assert_not_called()


@patch("ras2cng.mapping._generate_plan_maps")
@patch("ras2cng.mapping._configure_rasprocess")
@patch("ras2cng.mapping.init_ras_project")
def test_generate_result_maps_passes_render_mode(mock_init, mock_config, mock_gen, tmp_path):
    """render_mode should be forwarded to _generate_plan_maps."""
    ras, project_dir, prj = _make_fake_ras(tmp_path, plan_count=1)
    mock_init.return_value = ras
    mock_gen.return_value = {}

    generate_result_maps(
        project_dir, tmp_path / "maps",
        wse=False, depth=True, velocity=False,
        render_mode="slopingPretty",
    )

    _, kwargs = mock_gen.call_args
    assert kwargs.get("render_mode") == "slopingPretty"


@patch("ras2cng.mapping._generate_plan_maps")
@patch("ras2cng.mapping._configure_rasprocess")
@patch("ras2cng.mapping.init_ras_project")
def test_generate_result_maps_empty_project(mock_init, mock_config, mock_gen, tmp_path):
    """Should handle project with no plans gracefully."""
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    prj = project_dir / "Empty.prj"
    prj.write_text("Proj Title=Empty\n")

    ras = MagicMock()
    ras.project_name = "Empty"
    ras.plan_df = None
    mock_init.return_value = ras

    results = generate_result_maps(project_dir, tmp_path / "maps", depth=True)
    assert results == []


# ---------------------------------------------------------------------------
# MapResult dataclass
# ---------------------------------------------------------------------------

def test_map_result_defaults():
    mr = MapResult(plan_id="p01", plan_number="01")
    assert mr.map_types == {}
    assert mr.errors == []


def test_map_result_with_data(tmp_path):
    tif = tmp_path / "depth.tif"
    tif.touch()
    mr = MapResult(
        plan_id="p01",
        plan_number="01",
        map_types={"depth": [tif]},
        output_dir=tmp_path,
    )
    assert len(mr.map_types["depth"]) == 1
    assert mr.map_types["depth"][0] == tif


def test_convert_to_cog_uses_bundled_gdal_and_builds_overviews(tmp_path):
    """COG conversion must not depend on the system gdal_translate."""
    import numpy as np
    import rasterio
    from rasterio.transform import from_origin

    source_path = tmp_path / "Depth (Max).tif"
    with rasterio.open(
        source_path,
        "w",
        driver="GTiff",
        width=1536,
        height=1024,
        count=1,
        dtype="float32",
        crs="EPSG:2965",
        transform=from_origin(500000, 500000, 5, 5),
        nodata=-9999.0,
    ) as destination:
        destination.write(np.ones((1024, 1536), dtype="float32"), 1)

    [output_path] = _convert_to_cog([source_path])

    assert output_path.name == "Depth (Max)_cog.tif"
    assert source_path.is_file()
    with rasterio.open(output_path) as source:
        assert source.is_tiled
        assert source.overviews(1)
        assert source.nodata == pytest.approx(-9999.0)
        assert source.compression.name == "zstd"


def test_convert_to_cog_rejects_missing_source(tmp_path):
    with pytest.raises(FileNotFoundError, match="Result raster does not exist"):
        _convert_to_cog([tmp_path / "missing.tif"])


def test_convert_to_cog_uses_rasmapper_vrt_for_multiple_terrain_sources(tmp_path):
    import numpy as np
    import rasterio
    from rasterio.transform import from_origin

    left = tmp_path / "Depth (Max).TerrainWithChannel.ChannelOnly.tif"
    right = tmp_path / "Depth (Max).TerrainWithChannel.base.tif"
    for path, x_origin, value in ((left, 0, 1.0), (right, 600, 2.0)):
        with rasterio.open(
            path,
            "w",
            driver="GTiff",
            width=600,
            height=600,
            count=1,
            dtype="float32",
            crs="EPSG:2965",
            transform=from_origin(x_origin, 600, 1, 1),
            nodata=-9999.0,
        ) as destination:
            destination.write(np.full((600, 600), value, dtype="float32"), 1)

    vrt = tmp_path / "Depth (Max).vrt"
    vrt.write_text(
        f"""<VRTDataset rasterXSize="1200" rasterYSize="600">
  <SRS>EPSG:2965</SRS>
  <GeoTransform>0, 1, 0, 600, 0, -1</GeoTransform>
  <VRTRasterBand dataType="Float32" band="1">
    <NoDataValue>-9999</NoDataValue>
    <SimpleSource>
      <SourceFilename relativeToVRT="1">{left.name}</SourceFilename>
      <SourceBand>1</SourceBand>
      <SrcRect xOff="0" yOff="0" xSize="600" ySize="600"/>
      <DstRect xOff="0" yOff="0" xSize="600" ySize="600"/>
      <NODATA>-9999</NODATA>
    </SimpleSource>
    <SimpleSource>
      <SourceFilename relativeToVRT="1">{right.name}</SourceFilename>
      <SourceBand>1</SourceBand>
      <SrcRect xOff="0" yOff="0" xSize="600" ySize="600"/>
      <DstRect xOff="600" yOff="0" xSize="600" ySize="600"/>
      <NODATA>-9999</NODATA>
    </SimpleSource>
  </VRTRasterBand>
</VRTDataset>""",
        encoding="ascii",
    )

    [output] = _convert_to_cog([left, right])

    assert output.name == "Depth (Max)_cog.tif"
    with rasterio.open(output) as source:
        assert source.width == 1200
        assert source.height == 600
        assert source.read(1, window=((300, 301), (300, 301))).item() == pytest.approx(
            1.0
        )
        assert source.read(1, window=((300, 301), (900, 901))).item() == pytest.approx(
            2.0
        )
        assert source.overviews(1)
