from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pyogrio
import pytest
import rasterio
from rasterio.features import rasterize
from rasterio.transform import from_origin

from ras2cng.boundary import (
    DERIVED_BOUNDARY_SCHEMA,
    REQUIRED_SHAPEFILE_SUFFIXES,
    BoundaryEdgeLimitError,
    derive_inundation_boundary,
    normalize_depth_units,
)
from ras2cng.catalog import Manifest, ManifestPlanEntry
from ras2cng.stored_maps import (
    DERIVED_BOUNDARY_MAP_KEY,
    _discover_plan_maps,
    derived_boundary_provenance_errors,
    import_rasprocess_stored_maps,
)


def _write_depth(
    path: Path,
    values: np.ndarray,
    *,
    nodata: float | None = -9999.0,
    scale: float = 1.0,
    offset: float = 0.0,
    resolution: float = 1.0,
    units: str = "ft",
) -> Path:
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        width=values.shape[1],
        height=values.shape[0],
        count=1,
        dtype="float32",
        crs="EPSG:26915",
        transform=from_origin(100, 200, resolution, resolution),
        nodata=nodata,
    ) as destination:
        destination.write(values.astype("float32"), 1)
        destination.scales = (scale,)
        destination.offsets = (offset,)
        destination.update_tags(1, units=units)
    return path


def _rasterized_boundary(
    shapefile: Path,
    *,
    shape: tuple[int, int],
    transform,
) -> np.ndarray:
    frame = pyogrio.read_dataframe(shapefile)
    return rasterize(
        [(geometry, 1) for geometry in frame.geometry],
        out_shape=shape,
        transform=transform,
        fill=0,
        dtype="uint8",
    )


def _edge_count(mask: np.ndarray) -> int:
    wet = mask.astype(bool)
    padded = np.pad(wet, 1, constant_values=False)
    return int(
        np.count_nonzero(wet & ~padded[:-2, 1:-1])
        + np.count_nonzero(wet & ~padded[2:, 1:-1])
        + np.count_nonzero(wet & ~padded[1:-1, :-2])
        + np.count_nonzero(wet & ~padded[1:-1, 2:])
    )


@pytest.mark.parametrize(
    ("spelling", "canonical"),
    [
        ("ft", "ft"),
        ("Feet", "ft"),
        ("US Survey Feet", "ft"),
        ("English Units", "ft"),
        ("m", "m"),
        ("meters", "m"),
        ("metres", "m"),
        ("SI Units", "m"),
    ],
)
def test_normalize_depth_units_common_spellings(spelling: str, canonical: str):
    assert normalize_depth_units(spelling) == canonical


def test_native_mask_semantics_scale_offset_and_portable_provenance(tmp_path: Path):
    values = np.array(
        [
            [-9999.0, np.nan, np.inf, -np.inf],
            [-2.0, -1.0, 0.0, 1.0],
            [1.5, 2.0, 3.0, 0.5],
        ],
        dtype="float32",
    )
    depth = _write_depth(
        tmp_path / "Depth (Max).tif",
        values,
        scale=2.0,
        offset=-1.0,
    )
    output = tmp_path / "boundary.shp"

    result = derive_inundation_boundary(
        depth,
        output,
        threshold=1.0,
        block_size=2,
    )

    # scaled depth == threshold is dry; invalid, nonfinite, and negative
    # samples are dry. Only finite scaled depth strictly above 1 is wet.
    expected = np.isfinite(values) & (values != -9999.0)
    expected &= values * 2.0 - 1.0 > 1.0
    actual = _rasterized_boundary(
        output,
        shape=values.shape,
        transform=from_origin(100, 200, 1, 1),
    )
    np.testing.assert_array_equal(actual, expected.astype("uint8"))
    assert result.edge_count == _edge_count(expected)
    assert result.source_resolution == (1.0, 1.0)
    assert result.output_resolution == (1.0, 1.0)
    assert result.resampling == "none"

    for suffix in REQUIRED_SHAPEFILE_SUFFIXES:
        assert output.with_suffix(suffix).is_file()
    assert result.provenance_path.name == "boundary.raster-derived.provenance.json"
    provenance_text = result.provenance_path.read_text(encoding="utf-8")
    provenance = json.loads(provenance_text)
    assert provenance["schema"] == DERIVED_BOUNDARY_SCHEMA
    assert provenance["sourceKind"] == "calculated"
    assert provenance["source"] == "RASMapper/RasProcess Depth Stored Map"
    assert provenance["sourceMapType"] == "Depth"
    assert provenance["interpolationAuthority"] == "RASMapper/RasProcess source raster"
    assert provenance["derivationAuthority"] == "ras2cng"
    assert provenance["nativeRasMapperStoredPolygon"] is False
    assert provenance["comparison"] == "depth > threshold"
    assert provenance["threshold"] == 1.0
    assert provenance["units"] == "ft"
    assert provenance["profile"] == "Max"
    assert provenance["connectivity"] == 4
    assert provenance["sourceResolution"] == {"x": 1.0, "y": 1.0}
    assert provenance["outputResolution"] == {"x": 1.0, "y": 1.0}
    assert provenance["resampling"] == "none"
    assert provenance["nodata"]["sourceValue"] == -9999.0
    assert provenance["edgeCount"] == result.edge_count
    assert provenance["edgeLimit"] == result.edge_limit
    assert provenance["sourceRaster"] == depth.name
    assert str(tmp_path.resolve()) not in provenance_text


def test_holes_disconnected_cells_and_four_connectivity_are_exact(
    monkeypatch,
    tmp_path: Path,
):
    values = np.zeros((7, 7), dtype="float32")
    values[1:6, 1:6] = 1.0
    values[3, 3] = 0.0  # hole
    values[0, 6] = 1.0  # only diagonally adjacent to the main component
    depth = _write_depth(tmp_path / "depth.tif", values)
    output = tmp_path / "topology.shp"

    calls: list[bool] = []
    write_dataframe = pyogrio.write_dataframe

    def recording_write(*args, **kwargs):
        calls.append(bool(kwargs.get("append")))
        return write_dataframe(*args, **kwargs)

    monkeypatch.setattr(pyogrio, "write_dataframe", recording_write)
    result = derive_inundation_boundary(
        depth,
        output,
        batch_size=1,
        block_size=3,
    )

    actual = _rasterized_boundary(
        output,
        shape=values.shape,
        transform=from_origin(100, 200, 1, 1),
    )
    np.testing.assert_array_equal(actual, (values > 0).astype("uint8"))
    frame = pyogrio.read_dataframe(output)
    assert result.feature_count == len(frame) == 2
    assert sorted(len(geometry.interiors) for geometry in frame.geometry) == [0, 1]
    assert calls == [False, True]


def test_empty_mask_publishes_complete_empty_family(tmp_path: Path):
    values = np.zeros((3, 4), dtype="float32")
    depth = _write_depth(tmp_path / "depth.tif", values)
    output = tmp_path / "empty.shp"

    result = derive_inundation_boundary(depth, output)

    assert result.feature_count == 0
    assert result.wet_pixel_count == 0
    assert result.edge_count == 0
    assert pyogrio.read_dataframe(output).empty
    for suffix in REQUIRED_SHAPEFILE_SUFFIXES:
        assert output.with_suffix(suffix).is_file()
    assert result.provenance_path.is_file()


def test_resolution_rejects_upsampling_without_outputs(tmp_path: Path):
    depth = _write_depth(
        tmp_path / "depth.tif",
        np.ones((2, 2), dtype="float32"),
        resolution=2.0,
    )
    output = tmp_path / "upsampled.shp"

    with pytest.raises(ValueError, match="upsample"):
        derive_inundation_boundary(depth, output, resolution=1.0)

    assert not any(output.with_suffix(suffix).exists() for suffix in REQUIRED_SHAPEFILE_SUFFIXES)
    assert not (tmp_path / "upsampled.raster-derived.provenance.json").exists()


def test_max_resampling_preserves_any_wet_source_cell(tmp_path: Path):
    values = np.array(
        [
            [0, 1, 0, 0],
            [0, 0, 0, 0],
            [0, 0, 0, 1],
            [0, 0, 0, 0],
        ],
        dtype="float32",
    )
    depth = _write_depth(tmp_path / "depth.tif", values)
    output = tmp_path / "coarse.shp"

    result = derive_inundation_boundary(depth, output, resolution=2.0)

    actual = _rasterized_boundary(
        output,
        shape=(2, 2),
        transform=from_origin(100, 200, 2, 2),
    )
    np.testing.assert_array_equal(actual, np.array([[1, 0], [0, 1]], dtype="uint8"))
    assert result.output_resolution == (2.0, 2.0)
    assert result.resampling == "max"
    provenance = json.loads(result.provenance_path.read_text(encoding="utf-8"))
    assert provenance["resampling"] == "max"
    assert provenance["outputResolution"] == {"x": 2.0, "y": 2.0}


def test_edge_cap_runs_before_polygonization(monkeypatch, tmp_path: Path):
    values = (np.indices((8, 8)).sum(axis=0) % 2).astype("float32")
    depth = _write_depth(tmp_path / "depth.tif", values)
    output = tmp_path / "complex.shp"

    def forbidden_polygonize(*args, **kwargs):
        raise AssertionError("polygonization started before edge preflight")

    monkeypatch.setattr(
        "ras2cng.boundary._polygonize_mask",
        forbidden_polygonize,
    )
    with pytest.raises(BoundaryEdgeLimitError, match="before polygonization"):
        derive_inundation_boundary(
            depth,
            output,
            max_edges=10,
            block_size=3,
        )

    assert not any(output.with_suffix(suffix).exists() for suffix in REQUIRED_SHAPEFILE_SUFFIXES)


def test_portable_relative_source_identifier(tmp_path: Path):
    depth = _write_depth(
        tmp_path / "depth.tif",
        np.ones((1, 1), dtype="float32"),
    )
    output = tmp_path / "portable.shp"
    result = derive_inundation_boundary(
        depth,
        output,
        source_identifier="plans/p01/Depth (Max).tif",
    )
    provenance = json.loads(result.provenance_path.read_text(encoding="utf-8"))
    assert provenance["sourceRaster"] == "plans/p01/Depth (Max).tif"

    with pytest.raises(ValueError, match="portable relative"):
        derive_inundation_boundary(
            depth,
            tmp_path / "absolute.shp",
            source_identifier=str(depth.resolve()),
        )


def test_publish_failure_leaves_no_partial_destination(monkeypatch, tmp_path: Path):
    depth = _write_depth(
        tmp_path / "depth.tif",
        np.ones((2, 2), dtype="float32"),
    )
    output = tmp_path / "atomic.shp"
    destination_dbf = output.with_suffix(".dbf")
    replace = Path.replace

    def fail_dbf_move(self: Path, target: Path):
        if self.suffix == ".dbf" and Path(target) == destination_dbf:
            raise OSError("simulated family publication failure")
        return replace(self, target)

    monkeypatch.setattr(Path, "replace", fail_dbf_move)
    with pytest.raises(OSError, match="publication failure"):
        derive_inundation_boundary(depth, output)

    assert not any(output.with_suffix(suffix).exists() for suffix in REQUIRED_SHAPEFILE_SUFFIXES)
    assert not (tmp_path / "atomic.raster-derived.provenance.json").exists()


def test_derived_family_is_discovered_and_imported_as_calculated_vector(
    monkeypatch,
    tmp_path: Path,
):
    maps_dir = tmp_path / "maps"
    plan_dir = maps_dir / "p03"
    plan_dir.mkdir(parents=True)
    depth = _write_depth(
        plan_dir / "Depth (Max)_cog.tif",
        np.array([[0, 1], [1, 0]], dtype="float32"),
        resolution=5.0,
    )
    boundary = plan_dir / "Inundation Boundary (Max).raster-derived.shp"
    derived = derive_inundation_boundary(
        depth,
        boundary,
        profile="Max",
        units="US Survey Feet",
    )
    provenance = json.loads(derived.provenance_path.read_text(encoding="utf-8"))

    assert provenance["units"] == "ft"
    assert provenance["sourceResolution"] == {"x": 5.0, "y": 5.0}
    assert derived_boundary_provenance_errors(provenance, profile="Max") == []
    assert _discover_plan_maps(plan_dir)[DERIVED_BOUNDARY_MAP_KEY] == (
        boundary,
        "Max",
    )

    archive_dir = tmp_path / "archive"
    viewer_dir = tmp_path / "viewer"
    archive_dir.mkdir()
    viewer_dir.mkdir()
    viewer_dir.joinpath("manifest.json").write_text(
        json.dumps({"tilesets": [], "groups": []}),
        encoding="utf-8",
    )
    manifest = Manifest.create(
        "Model",
        tmp_path / "Model.prj",
        tmp_path,
        archive_dir,
        crs="EPSG:26915",
    )
    manifest.add_plan_entry(
        ManifestPlanEntry(
            plan_id="p03",
            plan_title="Plan 03",
            geom_id="g01",
            flow_id="u01",
            hdf_exists=True,
            completed=True,
            layout="variable",
            geometry_mode="none",
        )
    )
    manifest.write(archive_dir / "manifest.json")
    package_calls = []
    monkeypatch.setattr(
        "ras2cng.stored_maps.package_maplibre_calculated_vector",
        lambda source, viewer, **kwargs: package_calls.append(
            (source, viewer, kwargs)
        ),
    )

    summary = import_rasprocess_stored_maps(
        maps_dir,
        archive_dir,
        viewer_dir,
        require_all=False,
    )

    imported = archive_dir / "calculated/p03/inundation-boundary-max.parquet"
    assert summary.raster_count == 1
    assert summary.vector_count == 1
    assert imported.is_file()
    assert imported.with_suffix(".provenance.json").is_file()
    assert package_calls[0][0] == imported
    assert package_calls[0][2]["provenance"] == provenance
