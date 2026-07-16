from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import geopandas as gpd
import pytest
from shapely.geometry import box

from ras2cng.catalog import Manifest, ManifestPlanEntry
from ras2cng.stored_maps import import_rasprocess_stored_maps


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
    for map_type in ("Depth", "WSE", "Velocity"):
        (plan_maps / f"{map_type} (Max).Terrain_cog.tif").write_bytes(map_type.encode())
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
    assert summary.raster_count == 3
    assert summary.vector_count == 1
    assert len(raster_calls) == 3
    assert raster_calls[0][2]["geometry"] == "g02"
    assert vector_calls[0][2]["geometry"] == "g02"
    assert (archive / "stored-maps/p03/depth-max.cog.tif").is_file()
    assert (archive / "stored-maps/p03/inundation-boundary-max.parquet").is_file()
    loaded = Manifest.load(archive / "manifest.json")
    assert len(loaded.maps) == 1
    assert len(loaded.maps[0]["rasters"]) == 3
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
