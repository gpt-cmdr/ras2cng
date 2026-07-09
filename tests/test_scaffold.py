"""
Unit tests for scaffold.py — barebones project synthesis from a plan HDF.

All tests use tiny synthetic HDF5 files built with h5py; terrain creation via
RasProcess.exe is mocked. No real HEC-RAS files or installs needed.
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch

import h5py
import pytest

from ras2cng.scaffold import (
    SCAFFOLD_MARKER,
    PlanHdfMetadata,
    build_scaffold,
    read_plan_hdf_metadata,
    terrain_sidecar_files,
    _ras_date,
)


WKT = (
    'PROJCS["NAD_1983_StatePlane_Pennsylvania_North_FIPS_3701_Feet",'
    'GEOGCS["GCS_North_American_1983",DATUM["D_North_American_1983",'
    'SPHEROID["GRS_1980",6378137.0,298.257222101]]]'
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_plan_hdf(
    path: Path,
    *,
    file_type: str = "HEC-RAS Results",
    projection: str | None = WKT,
    units: str = "US Customary",
    plan_filename: str = "TestModel.p07",
    with_results: bool = True,
    with_plan_info: bool = True,
    plan_info_overrides: dict | None = None,
) -> Path:
    """Create a minimal synthetic plan results HDF."""
    with h5py.File(path, "w") as f:
        f.attrs["File Type"] = file_type.encode()
        f.attrs["File Version"] = b"HEC-RAS 6.6 September 2024"
        f.attrs["Units System"] = units.encode()
        if projection is not None:
            f.attrs["Projection"] = projection.encode()
        if with_results:
            f.create_group("Results")
        f.create_group("Geometry")
        if with_plan_info:
            info = f.create_group("Plan Data/Plan Information")
            attrs = {
                "Plan Filename": plan_filename,
                "Plan Title": "Test Plan Seven",
                "Plan ShortID": "TP-07",
                "Project Title": "Test Project",
                "Geometry Filename": "TestModel.g04",
                "Flow Filename": "TestModel.u02",
                "Simulation Start Time": "01Jan1999 12:00:00",
                "Simulation End Time": "04Jan1999 12:00:00",
            }
            attrs.update(plan_info_overrides or {})
            for k, v in attrs.items():
                info.attrs[k] = str(v).encode()
    return path


def make_terrain_sidecar(terrain_dir: Path, name: str = "Terrain50", tiles=("dem1", "dem2")):
    """Create a synthetic terrain HDF + .vrt + tile TIFF placeholder set."""
    terrain_dir.mkdir(parents=True, exist_ok=True)
    hdf_path = terrain_dir / f"{name}.hdf"
    with h5py.File(hdf_path, "w") as f:
        grp = f.create_group("Terrain")
        for tile in tiles:
            layer = grp.create_group(f"{name}.{tile}")
            layer.attrs["File"] = f"{name}.{tile}.tif".encode()
    (terrain_dir / f"{name}.vrt").write_text("<VRTDataset/>")
    for tile in tiles:
        (terrain_dir / f"{name}.{tile}.tif").write_bytes(b"TIFF")
    return hdf_path


# ---------------------------------------------------------------------------
# read_plan_hdf_metadata
# ---------------------------------------------------------------------------

def test_read_metadata_happy_path(tmp_path):
    hdf = make_plan_hdf(tmp_path / "results.hdf")
    meta = read_plan_hdf_metadata(hdf)
    assert meta.project_name == "TestModel"
    assert meta.plan_number == "07"
    assert meta.plan_short_id == "TP-07"
    assert meta.geom_ext == "g04"
    assert meta.flow_ext == "u02"
    assert meta.units == "US Customary"
    assert meta.projection_wkt == WKT
    assert meta.sim_start == "01Jan1999 12:00:00"


def test_read_metadata_foreign_absolute_plan_filename(tmp_path):
    hdf = make_plan_hdf(
        tmp_path / "results.hdf",
        plan_filename=r"C:\RasRemote\job_abc123\BigModel.p13",
    )
    meta = read_plan_hdf_metadata(hdf)
    assert meta.project_name == "BigModel"
    assert meta.plan_number == "13"


def test_read_metadata_missing_projection(tmp_path):
    hdf = make_plan_hdf(tmp_path / "results.hdf", projection=None)
    meta = read_plan_hdf_metadata(hdf)
    assert meta.projection_wkt is None


def test_read_metadata_unparseable_falls_back_to_filename(tmp_path):
    hdf = make_plan_hdf(
        tmp_path / "MyModel.p03.hdf",
        plan_info_overrides={"Plan Filename": "garbage"},
    )
    meta = read_plan_hdf_metadata(hdf)
    assert meta.project_name == "MyModel"
    assert meta.plan_number == "03"


def test_read_metadata_unparseable_and_bad_filename_raises(tmp_path):
    hdf = make_plan_hdf(
        tmp_path / "results.hdf",
        plan_info_overrides={"Plan Filename": "garbage"},
    )
    with pytest.raises(ValueError, match="cannot determine project name"):
        read_plan_hdf_metadata(hdf)


def test_read_metadata_rejects_non_results_hdf(tmp_path):
    hdf = make_plan_hdf(tmp_path / "geom.hdf", file_type="HEC-RAS Geometry")
    with pytest.raises(ValueError, match="not a HEC-RAS results HDF"):
        read_plan_hdf_metadata(hdf)


def test_read_metadata_rejects_uncomputed_plan(tmp_path):
    hdf = make_plan_hdf(tmp_path / "results.hdf", with_results=False)
    with pytest.raises(ValueError, match="not been computed"):
        read_plan_hdf_metadata(hdf)


def test_ras_date_format():
    assert _ras_date("01Jan1999 12:00:00") == "01JAN1999,1200"
    assert _ras_date("04Jan1999 00:30:00") == "04JAN1999,0030"


# ---------------------------------------------------------------------------
# build_scaffold — file synthesis (terrain mocked)
# ---------------------------------------------------------------------------

@pytest.fixture
def plan_hdf(tmp_path):
    return make_plan_hdf(tmp_path / "arbitrary_name.hdf")


def _mock_create_terrain(terrain_dir_holder):
    """Patch RasTerrain.create_terrain_from_rasters to fabricate Terrain.hdf."""
    def fake_create(rasters, out_dir, terrain_name="Terrain", **kwargs):
        out = Path(out_dir) / f"{terrain_name}.hdf"
        with h5py.File(out, "w") as f:
            f.create_group("Terrain")
        terrain_dir_holder.append(out)
        return out
    return fake_create


def test_build_scaffold_from_tifs(tmp_path, plan_hdf):
    workdir = tmp_path / "scaffold"
    tif = tmp_path / "dem.tif"
    tif.write_bytes(b"TIFF")

    created = []
    with patch("ras_commander.RasTerrain") as mock_terrain:
        mock_terrain.create_terrain_from_rasters.side_effect = _mock_create_terrain(created)
        info = build_scaffold(plan_hdf, workdir, terrain_tifs=[tif])

    # Canonical plan HDF name derived from attrs, not the input filename
    assert info.plan_hdf.name == "TestModel.p07.hdf"
    assert info.plan_hdf.exists()
    assert info.prj_file.name == "TestModel.prj"

    prj_text = info.prj_file.read_text()
    assert "Proj Title=Test Project" in prj_text
    assert "Current Plan=p07" in prj_text
    assert "English Units" in prj_text
    assert "Plan File=p07" in prj_text

    plan_text = (workdir / "TestModel.p07").read_text()
    assert "Short Identifier=TP-07" in plan_text
    assert "Simulation Date=01JAN1999,1200,04JAN1999,1200" in plan_text
    assert "Geom File=g04" in plan_text
    assert "Flow File=u02" in plan_text

    # Flow/geom stubs exist to quiet RasPrj log noise
    assert (workdir / "TestModel.u02").exists()
    assert (workdir / "TestModel.g04").exists()

    # Projection lives inside Terrain\ (avoids .prj collision)
    assert (workdir / "Terrain" / "Projection.prj").read_text() == WKT
    assert not (workdir / "Projection.prj").exists()

    # CreateTerrain called with generate_prj=False and correct units
    _, kwargs = mock_terrain.create_terrain_from_rasters.call_args
    assert kwargs["generate_prj"] is False
    assert kwargs["units"] == "Feet"

    assert (workdir / SCAFFOLD_MARKER).exists()


def test_build_scaffold_rasmap_structure(tmp_path, plan_hdf):
    workdir = tmp_path / "scaffold"
    tif = tmp_path / "dem.tif"
    tif.write_bytes(b"TIFF")

    with patch("ras_commander.RasTerrain") as mock_terrain:
        mock_terrain.create_terrain_from_rasters.side_effect = _mock_create_terrain([])
        build_scaffold(plan_hdf, workdir, terrain_tifs=[tif], render_mode="horizontal")

    root = ET.parse(workdir / "TestModel.rasmap").getroot()
    assert root.tag == "RASMapper"
    assert root.find("Version").text == "2.0.0"
    assert root.find("RASProjectionFilename").get("Filename") == ".\\Terrain\\Projection.prj"
    assert root.find("Results") is not None  # empty; store_maps populates it
    terrain_layer = root.find("Terrains/Layer")
    assert terrain_layer.get("Type") == "TerrainLayer"
    assert terrain_layer.get("Filename") == ".\\Terrain\\Terrain.hdf"
    assert root.find("Units").text == "US Customary"
    assert root.find("RenderMode").text == "horizontal"

    # Referenced files resolve relative to the scaffold
    assert (workdir / "Terrain" / "Terrain.hdf").exists()


def test_build_scaffold_si_units(tmp_path):
    hdf = make_plan_hdf(tmp_path / "m.hdf", units="SI Units")
    tif = tmp_path / "dem.tif"
    tif.write_bytes(b"TIFF")

    with patch("ras_commander.RasTerrain") as mock_terrain:
        mock_terrain.create_terrain_from_rasters.side_effect = _mock_create_terrain([])
        build_scaffold(hdf, tmp_path / "scaffold", terrain_tifs=[tif])

    _, kwargs = mock_terrain.create_terrain_from_rasters.call_args
    assert kwargs["units"] == "Meters"
    assert "SI Units" in (tmp_path / "scaffold" / "TestModel.prj").read_text()


def test_build_scaffold_copy_fallback_when_hardlink_fails(tmp_path, plan_hdf):
    workdir = tmp_path / "scaffold"
    tif = tmp_path / "dem.tif"
    tif.write_bytes(b"TIFF")

    with patch("ras_commander.RasTerrain") as mock_terrain, \
         patch("ras2cng.scaffold.os.link", side_effect=OSError("cross-device")):
        mock_terrain.create_terrain_from_rasters.side_effect = _mock_create_terrain([])
        info = build_scaffold(plan_hdf, workdir, terrain_tifs=[tif])

    assert info.plan_hdf.exists()
    assert info.plan_hdf.stat().st_size == plan_hdf.stat().st_size


def test_build_scaffold_missing_projection_requires_override(tmp_path):
    hdf = make_plan_hdf(tmp_path / "noproj.hdf", projection=None)
    tif = tmp_path / "dem.tif"
    tif.write_bytes(b"TIFF")

    with pytest.raises(ValueError, match="no Projection attribute"):
        build_scaffold(hdf, tmp_path / "scaffold", terrain_tifs=[tif])

    # With an explicit projection file it succeeds
    prj_file = tmp_path / "override.prj"
    prj_file.write_text(WKT)
    with patch("ras_commander.RasTerrain") as mock_terrain:
        mock_terrain.create_terrain_from_rasters.side_effect = _mock_create_terrain([])
        info = build_scaffold(
            hdf, tmp_path / "scaffold2", terrain_tifs=[tif], projection_file=prj_file
        )
    assert (info.project_dir / "Terrain" / "Projection.prj").read_text() == WKT


def test_build_scaffold_terrain_xor_enforced(tmp_path, plan_hdf):
    with pytest.raises(ValueError, match="exactly one"):
        build_scaffold(plan_hdf, tmp_path / "s", terrain_tifs=None, terrain_hdf=None)
    with pytest.raises(ValueError, match="exactly one"):
        build_scaffold(
            plan_hdf, tmp_path / "s",
            terrain_tifs=[tmp_path / "a.tif"], terrain_hdf=tmp_path / "t.hdf",
        )


def test_build_scaffold_rejects_foreign_nonempty_workdir(tmp_path, plan_hdf):
    workdir = tmp_path / "occupied"
    workdir.mkdir()
    (workdir / "somefile.txt").write_text("hello")

    with pytest.raises(ValueError, match="not a previous"):
        build_scaffold(plan_hdf, workdir, terrain_tifs=[tmp_path / "dem.tif"])


def test_build_scaffold_reuses_terrain_on_rerun(tmp_path, plan_hdf):
    workdir = tmp_path / "scaffold"
    tif = tmp_path / "dem.tif"
    tif.write_bytes(b"TIFF")

    with patch("ras_commander.RasTerrain") as mock_terrain:
        mock_terrain.create_terrain_from_rasters.side_effect = _mock_create_terrain([])
        build_scaffold(plan_hdf, workdir, terrain_tifs=[tif])
        assert mock_terrain.create_terrain_from_rasters.call_count == 1
        build_scaffold(plan_hdf, workdir, terrain_tifs=[tif])
        # Terrain build skipped on rerun with unchanged inputs
        assert mock_terrain.create_terrain_from_rasters.call_count == 1


def test_build_scaffold_hdf_change_preserves_terrain(tmp_path, plan_hdf):
    """A recomputed plan HDF must not discard an unchanged terrain build."""
    workdir = tmp_path / "scaffold"
    tif = tmp_path / "dem.tif"
    tif.write_bytes(b"TIFF")

    with patch("ras_commander.RasTerrain") as mock_terrain:
        mock_terrain.create_terrain_from_rasters.side_effect = _mock_create_terrain([])
        build_scaffold(plan_hdf, workdir, terrain_tifs=[tif])

        # Tamper with the marker to simulate a recomputed plan HDF
        marker = json.loads((workdir / SCAFFOLD_MARKER).read_text())
        marker["hdf_sig"][1] = 0  # mtime component
        (workdir / SCAFFOLD_MARKER).write_text(json.dumps(marker))

        info = build_scaffold(plan_hdf, workdir, terrain_tifs=[tif])
        # Terrain untouched; plan HDF and stubs regenerated
        assert mock_terrain.create_terrain_from_rasters.call_count == 1
        assert info.plan_hdf.exists()
        assert (workdir / "TestModel.prj").exists()


def test_build_scaffold_terrain_change_rebuilds_terrain(tmp_path, plan_hdf):
    """A different terrain input must invalidate the cached terrain build."""
    workdir = tmp_path / "scaffold"
    tif_a = tmp_path / "dem_2019.tif"
    tif_a.write_bytes(b"TIFF-A")
    tif_b = tmp_path / "dem_2024.tif"
    tif_b.write_bytes(b"TIFF-B")

    with patch("ras_commander.RasTerrain") as mock_terrain:
        mock_terrain.create_terrain_from_rasters.side_effect = _mock_create_terrain([])
        build_scaffold(plan_hdf, workdir, terrain_tifs=[tif_a])
        assert mock_terrain.create_terrain_from_rasters.call_count == 1

        build_scaffold(plan_hdf, workdir, terrain_tifs=[tif_b])
        assert mock_terrain.create_terrain_from_rasters.call_count == 2


def test_build_scaffold_recovers_from_failed_terrain_build(tmp_path, plan_hdf):
    """A failed CreateTerrain must not poison the workdir for reruns."""
    workdir = tmp_path / "scaffold"
    tif = tmp_path / "dem.tif"
    tif.write_bytes(b"TIFF")

    with patch("ras_commander.RasTerrain") as mock_terrain:
        mock_terrain.create_terrain_from_rasters.side_effect = RuntimeError("CreateTerrain failed")
        with pytest.raises(RuntimeError):
            build_scaffold(plan_hdf, workdir, terrain_tifs=[tif])

    # Rerun with a working terrain build succeeds (no 'not a previous
    # ras2cng scaffold' error from the partial dir)
    with patch("ras_commander.RasTerrain") as mock_terrain:
        mock_terrain.create_terrain_from_rasters.side_effect = _mock_create_terrain([])
        info = build_scaffold(plan_hdf, workdir, terrain_tifs=[tif])
    assert info.terrain_hdf.exists()


# ---------------------------------------------------------------------------
# Terrain sidecar handling
# ---------------------------------------------------------------------------

def test_terrain_sidecar_files_complete(tmp_path):
    hdf = make_terrain_sidecar(tmp_path / "terrain")
    files = terrain_sidecar_files(hdf)
    names = {p.name for p in files}
    assert names == {"Terrain50.vrt", "Terrain50.dem1.tif", "Terrain50.dem2.tif"}


def test_terrain_sidecar_files_missing_tile(tmp_path):
    hdf = make_terrain_sidecar(tmp_path / "terrain")
    (tmp_path / "terrain" / "Terrain50.dem2.tif").unlink()
    with pytest.raises(FileNotFoundError, match="Terrain50.dem2.tif"):
        terrain_sidecar_files(hdf)


def test_terrain_sidecar_files_rejects_non_terrain_hdf(tmp_path):
    bogus = tmp_path / "bogus.hdf"
    with h5py.File(bogus, "w") as f:
        f.create_group("NotTerrain")
    with pytest.raises(ValueError, match="not a HEC-RAS terrain HDF"):
        terrain_sidecar_files(bogus)


def test_build_scaffold_with_terrain_sidecar(tmp_path, plan_hdf):
    terrain_hdf = make_terrain_sidecar(tmp_path / "prebuilt")
    workdir = tmp_path / "scaffold"

    info = build_scaffold(plan_hdf, workdir, terrain_hdf=terrain_hdf)

    tdir = workdir / "Terrain"
    assert (tdir / "Terrain50.hdf").exists()
    assert (tdir / "Terrain50.vrt").exists()
    assert (tdir / "Terrain50.dem1.tif").exists()
    assert (tdir / "Terrain50.dem2.tif").exists()
    assert info.terrain_hdf == tdir / "Terrain50.hdf"

    # rasmap points at the imported terrain name
    root = ET.parse(workdir / "TestModel.rasmap").getroot()
    assert root.find("Terrains/Layer").get("Filename") == ".\\Terrain\\Terrain50.hdf"
