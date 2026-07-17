from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import geopandas as gpd
import pytest
from shapely.geometry import box

from ras2cng.catalog import Manifest, ManifestPlanEntry
from ras2cng.stored_maps import _discover_plan_maps, import_rasprocess_stored_maps


RASTER_NAMES = (
    "Depth (Max).Terrain_cog.tif",
    "WSE (Max).Terrain_cog.tif",
    "Velocity (Max).Terrain_cog.tif",
    "Froude (Max).Terrain_cog.tif",
    "Shear Stress (Max).Terrain_cog.tif",
    "Depth x Velocity (Max).Terrain_cog.tif",
    "Depth x Velocity² (Max).Terrain_cog.tif",
    "Arrival Time (0.1ft hrs).Terrain_cog.tif",
    "Duration (0.1ft hrs).Terrain_cog.tif",
    "Percent Time Inundated (0.1ft).Terrain_cog.tif",
)


def _bundle(tmp_path: Path):
    archive = tmp_path / "archive"
    viewer = tmp_path / "viewer"
    maps = tmp_path / "maps"
    archive.mkdir()
    viewer.mkdir()
    plan_maps = maps / "p03"
    plan_maps.mkdir(parents=True)
    manifest = Manifest.create(
        "Muncie",
        tmp_path / "Muncie.prj",
        tmp_path,
        archive,
        crs="EPSG:2965",
    )
    manifest.add_plan_entry(
        ManifestPlanEntry(
            plan_id="p03",
            plan_title="2D 50ft Grid",
            geom_id="g02",
            flow_id="u01",
            hdf_exists=True,
            completed=True,
            layout="variable",
            geometry_mode="none",
        )
    )
    manifest.write(archive / "manifest.json")
    (viewer / "manifest.json").write_text(json.dumps({"tilesets": [], "groups": []}))
    for name in RASTER_NAMES:
        (plan_maps / name).write_bytes(name.encode())
    gpd.GeoDataFrame(
        {"Name": ["Max"], "geometry": [box(500000, 500000, 501000, 501000)]},
        crs="EPSG:2965",
    ).to_file(plan_maps / "Inundation Boundary (Max Value_0).shp")
    return maps, archive, viewer


def test_import_stored_maps_registers_archive_and_packages_layers(monkeypatch, tmp_path: Path):
    maps, archive, viewer = _bundle(tmp_path)
    raster_calls = []
    vector_calls = []

    def fake_raster(cog, viewer_dir, **kwargs):
        raster_calls.append((cog, viewer_dir, kwargs))
        return SimpleNamespace(layer_id=kwargs["layer_id"])

    def fake_vector(source, viewer_dir, **kwargs):
        vector_calls.append((source, viewer_dir, kwargs))
        return SimpleNamespace(layer_id=kwargs["layer_id"])

    monkeypatch.setattr("ras2cng.stored_maps.package_maplibre_stored_map", fake_raster)
    monkeypatch.setattr("ras2cng.stored_maps.package_maplibre_stored_vector", fake_vector)

    summary = import_rasprocess_stored_maps(
        maps,
        archive,
        viewer,
        scratch_dir=tmp_path / "scratch",
    )

    assert summary.plan_count == 1
    assert summary.raster_count == 10
    assert summary.vector_count == 1
    assert len(raster_calls) == 10
    assert raster_calls[0][2]["geometry"] == "g02"
    assert vector_calls[0][2]["geometry"] == "g02"
    assert (archive / "stored-maps/p03/depth-max.cog.tif").is_file()
    assert (archive / "stored-maps/p03/inundation-boundary-max.parquet").is_file()
    loaded = Manifest.load(archive / "manifest.json")
    assert len(loaded.maps) == 1
    assert len(loaded.maps[0]["rasters"]) == 10
    assert len(loaded.maps[0]["vectors"]) == 1


def test_import_stored_maps_rejects_incomplete_plan_before_copy(monkeypatch, tmp_path: Path):
    maps, archive, viewer = _bundle(tmp_path)
    (maps / "p03" / "Velocity (Max).Terrain_cog.tif").unlink()
    monkeypatch.setattr(
        "ras2cng.stored_maps.package_maplibre_stored_map",
        lambda *_args, **_kwargs: pytest.fail("packaging should not start"),
    )

    with pytest.raises(ValueError, match="p03: missing velocity"):
        import_rasprocess_stored_maps(maps, archive, viewer)

    assert not (archive / "stored-maps").exists()


def test_import_stored_maps_rejects_missing_extended_raster_family(
    monkeypatch, tmp_path: Path
):
    maps, archive, viewer = _bundle(tmp_path)
    (maps / "p03" / "Froude (Max).Terrain_cog.tif").unlink()
    monkeypatch.setattr(
        "ras2cng.stored_maps.package_maplibre_stored_map",
        lambda *_args, **_kwargs: pytest.fail("packaging should not start"),
    )

    with pytest.raises(ValueError, match="p03: missing froude"):
        import_rasprocess_stored_maps(maps, archive, viewer)

    assert not (archive / "stored-maps").exists()


def test_import_stored_maps_discovers_all_supported_raster_families(
    monkeypatch, tmp_path: Path
):
    maps, archive, viewer = _bundle(tmp_path)
    raster_calls = []
    monkeypatch.setattr(
        "ras2cng.stored_maps.package_maplibre_stored_map",
        lambda cog, _viewer, **kwargs: raster_calls.append((cog, kwargs)),
    )
    monkeypatch.setattr(
        "ras2cng.stored_maps.package_maplibre_stored_vector",
        lambda *_args, **_kwargs: None,
    )

    summary = import_rasprocess_stored_maps(maps, archive, viewer)

    assert summary.raster_count == 10
    assert {call[1]["map_type"] for call in raster_calls} == {
        "Depth",
        "WSE",
        "Velocity",
        "Froude Number",
        "Shear Stress",
        "Depth x Velocity",
        "Depth x Velocity Squared",
        "Arrival Time",
        "Duration",
        "Percent Time Inundated",
    }
    assert (archive / "stored-maps/p03/arrival_time-0-1ft-hrs.cog.tif").is_file()
    assert (archive / "stored-maps/p03/depth_x_velocity_sq-max.cog.tif").is_file()


def test_discover_plan_maps_prefers_complete_vrt_mosaic_cog(tmp_path: Path):
    plan = tmp_path / "p03"
    plan.mkdir()
    component = plan / "Depth (Max).TerrainWithChannel.base_cog.tif"
    mosaic = plan / "Depth (Max)_cog.tif"
    component.touch()
    mosaic.touch()

    discovered = _discover_plan_maps(plan)

    assert discovered["depth"] == (mosaic, "Max")
