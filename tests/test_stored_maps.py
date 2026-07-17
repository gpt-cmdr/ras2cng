from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import geopandas as gpd
import numpy as np
import pandas as pd
import pytest
import rasterio
from rasterio.transform import from_origin
from shapely.geometry import box

from ras2cng.catalog import Manifest, ManifestPlanEntry
from ras2cng.mapping import generate_result_maps
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

DERIVED_BOUNDARY_PROVENANCE = {
    "schema": "ras2cng.derived-inundation-boundary/v1",
    "sourceKind": "calculated",
    "source": "RASMapper/RasProcess Depth Stored Map",
    "sourceRaster": "Depth (Max)_cog.tif",
    "sourceMapType": "Depth",
    "interpolationAuthority": "RASMapper/RasProcess source raster",
    "derivationAuthority": "ras2cng",
    "nativeRasMapperStoredPolygon": False,
    "threshold": 0.0,
    "comparison": "depth > threshold",
    "profile": "Max",
    "units": "ft",
    "connectivity": 4,
    "outputShapefile": "Inundation Boundary (Max).raster-derived.shp",
    "sourceResolution": {"x": 5.0, "y": 5.0},
    "outputResolution": {"x": 5.0, "y": 5.0},
    "resampling": "none",
    "nodata": {
        "sourceValue": -9999.0,
        "datasetMaskApplied": True,
        "nonFiniteExcluded": True,
        "maskedPixelCount": 0,
        "nonFinitePixelCount": 0,
    },
    "edgeCount": 400,
    "edgeLimit": 5_000_000,
}


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


def _add_derived_boundary(plan_maps: Path, *, remove_native: bool = True) -> Path:
    if remove_native:
        for path in plan_maps.glob("Inundation Boundary (Max Value_0).*"):
            path.unlink()
    boundary = plan_maps / "Inundation Boundary (Max).raster-derived.shp"
    gpd.GeoDataFrame(
        {"profile": ["Max"], "geometry": [box(500000, 500000, 501000, 501000)]},
        crs="EPSG:2965",
    ).to_file(boundary)
    boundary.with_suffix(".provenance.json").write_text(
        json.dumps(DERIVED_BOUNDARY_PROVENANCE),
        encoding="utf-8",
    )
    return boundary


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
    assert raster_calls[0][2]["max_zoom"] == 16
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


def test_import_derived_boundary_as_calculated_vector(monkeypatch, tmp_path: Path):
    maps, archive, viewer = _bundle(tmp_path)
    boundary = _add_derived_boundary(maps / "p03")
    vector_calls = []

    monkeypatch.setattr(
        "ras2cng.stored_maps.package_maplibre_stored_map",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "ras2cng.stored_maps.package_maplibre_calculated_vector",
        lambda source, viewer_dir, **kwargs: vector_calls.append(
            (source, viewer_dir, kwargs)
        ),
    )

    summary = import_rasprocess_stored_maps(maps, archive, viewer)

    target = archive / "calculated/p03/inundation-boundary-max.parquet"
    provenance_target = target.with_suffix(".provenance.json")
    assert summary.raster_count == 10
    assert summary.vector_count == 1
    assert target.is_file()
    assert provenance_target.is_file()
    assert not (archive / "stored-maps/p03/inundation-boundary-max.parquet").exists()
    assert vector_calls == [
        (
            target,
            viewer,
            {
                "plan": "p03",
                "map_type": "Inundation Boundary",
                "name": "Inundation Boundary (Max) - Derived from RASMapper Depth",
                "profile": "Max",
                "geometry": "g02",
                "layer_id": "calculated-p03-inundation-boundary-max",
                "crs": "EPSG:2965",
                "provenance": DERIVED_BOUNDARY_PROVENANCE,
                "visible": False,
                "scratch_dir": None,
                "overwrite": False,
            },
        )
    ]
    loaded = Manifest.load(archive / "manifest.json")
    vector = loaded.maps[0]["vectors"][0]
    assert vector["file"] == "calculated/p03/inundation-boundary-max.parquet"
    assert vector["provenance_file"] == (
        "calculated/p03/inundation-boundary-max.provenance.json"
    )
    assert vector["source_kind"] == "calculated"
    assert vector["result_kind"] == "calculated_vector"
    assert vector["provenance"] == DERIVED_BOUNDARY_PROVENANCE
    assert boundary.with_suffix(".provenance.json").is_file()


def test_discover_plan_maps_rejects_partial_derived_family_before_copy(
    monkeypatch, tmp_path: Path
):
    maps, archive, viewer = _bundle(tmp_path)
    boundary = _add_derived_boundary(maps / "p03")
    boundary.with_suffix(".dbf").unlink()
    monkeypatch.setattr(
        "ras2cng.stored_maps.package_maplibre_stored_map",
        lambda *_args, **_kwargs: pytest.fail("packaging should not start"),
    )

    with pytest.raises(ValueError, match="partial derived inundation boundary family"):
        import_rasprocess_stored_maps(maps, archive, viewer)

    assert not (archive / "stored-maps").exists()
    assert not (archive / "calculated").exists()


def test_discover_plan_maps_rejects_native_and_derived_boundary_ambiguity(
    monkeypatch, tmp_path: Path
):
    maps, archive, viewer = _bundle(tmp_path)
    _add_derived_boundary(maps / "p03", remove_native=False)
    monkeypatch.setattr(
        "ras2cng.stored_maps.package_maplibre_stored_map",
        lambda *_args, **_kwargs: pytest.fail("packaging should not start"),
    )

    with pytest.raises(ValueError, match="native and derived inundation boundaries"):
        import_rasprocess_stored_maps(maps, archive, viewer)

    assert not (archive / "stored-maps").exists()
    assert not (archive / "calculated").exists()


def test_discover_plan_maps_rejects_local_path_in_derived_provenance(tmp_path: Path):
    maps, archive, viewer = _bundle(tmp_path)
    boundary = _add_derived_boundary(maps / "p03")
    provenance_path = boundary.with_suffix(".provenance.json")
    provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    provenance["sourceRaster"] = r"C:\\RAS\\scratch\\Depth (Max)_cog.tif"
    provenance_path.write_text(json.dumps(provenance), encoding="utf-8")

    with pytest.raises(ValueError, match="portable relative|processing-host path"):
        import_rasprocess_stored_maps(maps, archive, viewer)

    assert not (archive / "stored-maps").exists()
    assert not (archive / "calculated").exists()


def test_mapping_derive_then_import_uses_canonical_provenance(
    monkeypatch, tmp_path: Path
):
    maps, archive, viewer = _bundle(tmp_path)
    plan_maps = maps / "p03"
    for path in plan_maps.glob("Inundation Boundary (Max Value_0).*"):
        path.unlink()

    project = tmp_path / "project"
    project.mkdir()
    project_file = project / "TestModel.prj"
    project_file.write_text("Proj Title=Test\nEnglish Units\n", encoding="utf-8")
    (project / "TestModel.p03.hdf").touch()
    ras = SimpleNamespace(
        project_name="TestModel",
        project_folder=str(project),
        plan_df=pd.DataFrame(
            [
                {
                    "plan_number": "03",
                    "geometry_number": "02",
                    "Plan Title": "2D 50ft Grid",
                }
            ]
        ),
        geom_df=pd.DataFrame(),
        results_df=pd.DataFrame(),
    )
    monkeypatch.setattr("ras2cng.mapping.init_ras_project", lambda *_args, **_kwargs: ras)
    monkeypatch.setattr("ras2cng.mapping._configure_rasprocess", lambda *_args: None)

    def fake_generate_plan_maps(*, output_dir, **_kwargs):
        depth = output_dir / "Depth (Max)_cog.tif"
        with rasterio.open(
            depth,
            "w",
            driver="GTiff",
            width=3,
            height=3,
            count=1,
            dtype="float32",
            crs="EPSG:2965",
            transform=from_origin(500000, 501000, 5, 5),
            nodata=-9999.0,
        ) as destination:
            destination.write(
                np.array(
                    [
                        [0.0, 1.0, 0.0],
                        [1.0, 1.0, 1.0],
                        [0.0, 1.0, 0.0],
                    ],
                    dtype="float32",
                ),
                1,
            )
        return {"depth": [depth]}

    monkeypatch.setattr(
        "ras2cng.mapping._generate_plan_maps",
        fake_generate_plan_maps,
    )
    [mapping_result] = generate_result_maps(
        project,
        maps,
        wse=False,
        depth=True,
        velocity=False,
        inundation_boundary=True,
        boundary_method="depth-raster",
        boundary_max_edges=1_000,
        skip_errors=False,
    )
    derived = mapping_result.derived_boundary
    assert derived is not None
    expected_provenance = json.loads(
        derived.provenance_path.read_text(encoding="utf-8")
    )
    assert expected_provenance["units"] == "ft"
    vector_calls = []
    monkeypatch.setattr(
        "ras2cng.stored_maps.package_maplibre_stored_map",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "ras2cng.stored_maps.package_maplibre_calculated_vector",
        lambda source, viewer_dir, **kwargs: vector_calls.append(
            (source, viewer_dir, kwargs)
        ),
    )

    discovered = _discover_plan_maps(plan_maps)
    summary = import_rasprocess_stored_maps(maps, archive, viewer)

    assert discovered["derived_inundation_boundary"] == (
        derived.output_path,
        "Max",
    )
    assert summary.raster_count == 10
    assert summary.vector_count == 1
    assert vector_calls[0][2]["provenance"] == expected_provenance
    assert vector_calls[0][2]["provenance"]["sourceResolution"] == {
        "x": 5.0,
        "y": 5.0,
    }
    assert vector_calls[0][2]["provenance"]["sourceRaster"] == (
        "Depth (Max)_cog.tif"
    )
    archived = Manifest.load(archive / "manifest.json").maps[0]["vectors"][0]
    assert archived["provenance"] == expected_provenance
