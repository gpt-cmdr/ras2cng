"""
Unit tests for geometry layers, export_all_hdf_layers, and merge_all_layers.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import geopandas as gpd
import pytest
from shapely.geometry import LineString, Point, Polygon

from ras_commander.geom import GeomParser, GeomStorage

from ras2cng.geometry import (
    ALL_HDF_LAYERS,
    ALL_TEXT_LAYERS,
    HDF_LAYERS,
    export_all_hdf_layers,
    export_all_text_layers,
    merge_all_layers,
    _extract_hdf_layer,
    _hilbert_sort,
)


def _fake_gdf(geom_type="LineString", n=3):
    """Return a minimal GeoDataFrame for testing."""
    if geom_type == "Polygon":
        geoms = [Polygon([(i, 0), (i + 1, 0), (i + 1, 1), (i, 0)]) for i in range(n)]
    elif geom_type == "Point":
        geoms = [Point(i, i) for i in range(n)]
    else:
        geoms = [LineString([(i, 0), (i + 1, 1)]) for i in range(n)]
    return gpd.GeoDataFrame({"id": list(range(n))}, geometry=geoms, crs="EPSG:4326")


# ---------------------------------------------------------------------------
# HDF_LAYERS dict completeness
# ---------------------------------------------------------------------------

def test_hdf_layers_dict_includes_all_expected():
    expected = {
        "mesh_cells", "mesh_faces", "mesh_areas", "cross_sections",
        "centerlines", "bank_lines", "bc_lines", "breaklines",
        "refinement_regions", "reference_lines", "reference_points",
        "structures", "pipe_conduits", "pipe_nodes", "river_reaches",
        "edge_lines", "storage_areas", "pump_stations",
        "mannings_n_regions", "infiltration_regions",
    }
    assert expected == set(HDF_LAYERS.keys()), f"Missing: {expected - set(HDF_LAYERS.keys())}"


def test_all_hdf_layers_list_matches_dict():
    assert set(ALL_HDF_LAYERS) == set(HDF_LAYERS.keys())


def test_all_text_layers_contains_expected():
    assert "cross_sections" in ALL_TEXT_LAYERS
    assert "centerlines" in ALL_TEXT_LAYERS
    assert "storage_areas" in ALL_TEXT_LAYERS


# ---------------------------------------------------------------------------
# _extract_hdf_layer — invalid layer name
# ---------------------------------------------------------------------------

def test_extract_hdf_layer_unknown_name_raises(tmp_path):
    with pytest.raises(ValueError, match="Unknown HDF layer"):
        _extract_hdf_layer(tmp_path / "fake.g01.hdf", "nonexistent_layer")


def test_extract_mesh_faces_uses_native_face_ids(tmp_path):
    hdf_path = tmp_path / "fake.g01.hdf"
    hdf_path.touch()
    faces = gpd.GeoDataFrame(
        {
            "mesh_name": ["m1", "m1"],
            "face_id": [0, 1],
        },
        geometry=[LineString([(0, 0), (1, 0)]), LineString([(1, 0), (1, 1)])],
        crs="EPSG:4326",
    )

    with patch("ras2cng.geometry.HdfMesh.get_mesh_cell_faces", return_value=faces):
        result = _extract_hdf_layer(hdf_path, "mesh_faces")

    assert result is not None
    assert "face_id" in result.columns
    assert list(result["face_id"]) == [0, 1]


def test_extract_pipe_conduits_preserves_hdf_crs_and_normalizes_geometry(tmp_path):
    hdf_path = tmp_path / "pipes.g01.hdf"
    hdf_path.touch()
    conduits = _fake_gdf("LineString", n=2).set_crs("EPSG:2871", allow_override=True)
    conduits = conduits.rename_geometry("Polyline")

    with (
        patch("ras2cng.geometry.HdfBase.get_projection", return_value="EPSG:2871"),
        patch("ras2cng.geometry.HdfPipe.get_pipe_conduits", return_value=conduits) as extract,
    ):
        result = _extract_hdf_layer(hdf_path, "pipe_conduits")

    extract.assert_called_once_with(hdf_path, crs="EPSG:2871")
    assert result is not None
    assert result.crs == "EPSG:2871"
    assert result.geometry.name == "geometry"


@pytest.mark.parametrize(
    ("layer", "extractor"),
    [
        ("river_reaches", "HdfXsec.get_river_reaches"),
        ("edge_lines", "HdfXsec.get_river_edge_lines"),
        ("storage_areas", "HdfStruc.get_storage_area_polygons"),
        ("pump_stations", "HdfPump.get_pump_stations"),
        ("mannings_n_regions", "HdfLandCover.get_mannings_region_polygons"),
        ("infiltration_regions", "HdfInfiltration.get_infiltration_region_polygons"),
    ],
)
def test_extract_extended_hdf_geometry_layers(tmp_path, layer, extractor):
    hdf_path = tmp_path / "extended.g01.hdf"
    hdf_path.touch()
    expected = _fake_gdf("Polygon" if "regions" in layer or layer == "storage_areas" else "LineString")

    with patch(f"ras2cng.geometry.{extractor}", return_value=expected) as extract:
        result = _extract_hdf_layer(hdf_path, layer)

    extract.assert_called_once_with(hdf_path)
    assert result is expected


# ---------------------------------------------------------------------------
# export_all_hdf_layers
# ---------------------------------------------------------------------------

def test_export_all_hdf_layers_returns_dict(tmp_path):
    """export_all_hdf_layers should return a dict of layer_name -> Path."""
    hdf_path = tmp_path / "model.g01.hdf"
    hdf_path.touch()
    out_dir = tmp_path / "geometry" / "g01"

    # Mock _extract_hdf_layer to return a GDF for mesh_cells only
    def fake_extract(path, layer):
        if layer == "mesh_cells":
            return _fake_gdf("Polygon")
        return None

    with patch("ras2cng.geometry._extract_hdf_layer", side_effect=fake_extract):
        result = export_all_hdf_layers(hdf_path, out_dir)

    assert isinstance(result, dict)
    assert "mesh_cells" in result
    assert result["mesh_cells"].suffix == ".parquet"
    assert result["mesh_cells"].exists()


def test_export_all_hdf_layers_skips_empty(tmp_path):
    """Layers returning None should be skipped (not in result dict)."""
    hdf_path = tmp_path / "model.g01.hdf"
    hdf_path.touch()

    with patch("ras2cng.geometry._extract_hdf_layer", return_value=None):
        result = export_all_hdf_layers(hdf_path, tmp_path / "out")

    assert result == {}


def test_export_all_hdf_layers_handles_extraction_error_gracefully(tmp_path):
    """Extraction errors should be caught and skipped (not raised)."""
    hdf_path = tmp_path / "model.g01.hdf"
    hdf_path.touch()

    call_count = {"n": 0}

    def raising_extract(path, layer):
        call_count["n"] += 1
        raise RuntimeError("HDF read error")

    with patch("ras2cng.geometry._extract_hdf_layer", side_effect=raising_extract):
        result = export_all_hdf_layers(hdf_path, tmp_path / "out", skip_empty=True)

    assert result == {}  # all failed, none written


# ---------------------------------------------------------------------------
# export_all_text_layers
# ---------------------------------------------------------------------------

def test_export_all_text_layers_returns_dict(tmp_path):
    """export_all_text_layers should write available layers."""
    text_path = tmp_path / "model.g01"
    text_path.write_text("Cross Section\n")

    def fake_xs(path):
        return _fake_gdf("LineString")

    def fake_cl(path):
        return _fake_gdf("LineString", n=1)

    def fake_sa(path):
        return gpd.GeoDataFrame()  # empty — should be skipped

    with (
        patch.object(GeomParser, "get_xs_cut_lines", side_effect=fake_xs),
        patch.object(GeomParser, "get_river_centerlines", side_effect=fake_cl),
        patch.object(GeomStorage, "get_storage_areas", side_effect=fake_sa),
    ):
        result = export_all_text_layers(text_path, tmp_path / "out")

    assert "cross_sections" in result
    assert "centerlines" in result
    assert "storage_areas" not in result  # was empty


def test_export_all_text_layers_skips_errors(tmp_path):
    """Errors in text extraction should be caught and skipped."""
    text_path = tmp_path / "model.g01"
    text_path.write_text("")

    with (
        patch.object(GeomParser, "get_xs_cut_lines", side_effect=Exception("parse error")),
        patch.object(GeomParser, "get_river_centerlines", side_effect=Exception("parse error")),
        patch.object(GeomStorage, "get_storage_areas", side_effect=Exception("parse error")),
    ):
        result = export_all_text_layers(text_path, tmp_path / "out")

    assert result == {}


# ---------------------------------------------------------------------------
# merge_all_layers
# ---------------------------------------------------------------------------

def test_merge_all_layers_hdf_only(tmp_path):
    """merge_all_layers with HDF only should return GDF with layer column."""
    hdf_path = tmp_path / "model.g01.hdf"
    hdf_path.touch()

    def fake_extract(path, layer):
        if layer == "mesh_cells":
            return _fake_gdf("Polygon", n=5)
        if layer == "bc_lines":
            return _fake_gdf("LineString", n=2)
        return None

    with patch("ras2cng.geometry._extract_hdf_layer", side_effect=fake_extract):
        result = merge_all_layers(hdf_path=hdf_path, sort=False)

    assert result is not None
    assert "layer" in result.columns
    assert set(result["layer"].unique()) == {"mesh_cells", "bc_lines"}
    assert len(result[result["layer"] == "mesh_cells"]) == 5
    assert len(result[result["layer"] == "bc_lines"]) == 2


def test_merge_all_layers_hdf_and_text(tmp_path):
    """merge_all_layers with both HDF and text should use _text suffix."""
    hdf_path = tmp_path / "model.g01.hdf"
    hdf_path.touch()
    text_path = tmp_path / "model.g01"
    text_path.write_text("data")

    def fake_extract(path, layer):
        if layer == "mesh_cells":
            return _fake_gdf("Polygon", n=3)
        return None

    def fake_xs(path):
        return _fake_gdf("LineString", n=2)

    def fake_cl(path):
        return gpd.GeoDataFrame()  # empty

    def fake_sa(path):
        return gpd.GeoDataFrame()  # empty

    with (
        patch("ras2cng.geometry._extract_hdf_layer", side_effect=fake_extract),
        patch.object(GeomParser, "get_xs_cut_lines", side_effect=fake_xs),
        patch.object(GeomParser, "get_river_centerlines", side_effect=fake_cl),
        patch.object(GeomStorage, "get_storage_areas", side_effect=fake_sa),
    ):
        result = merge_all_layers(hdf_path=hdf_path, text_path=text_path, sort=False)

    assert result is not None
    layers = set(result["layer"].unique())
    assert "mesh_cells" in layers
    assert "cross_sections_text" in layers
    # Text layers should have _text suffix
    for lyr in layers:
        if lyr not in set(ALL_HDF_LAYERS):
            assert lyr.endswith("_text")


def test_merge_all_layers_returns_none_when_empty(tmp_path):
    """merge_all_layers returns None when no layers extracted."""
    hdf_path = tmp_path / "model.g01.hdf"
    hdf_path.touch()

    with patch("ras2cng.geometry._extract_hdf_layer", return_value=None):
        result = merge_all_layers(hdf_path=hdf_path)

    assert result is None


def test_merge_all_layers_none_inputs():
    """merge_all_layers with no paths returns None."""
    result = merge_all_layers(hdf_path=None, text_path=None)
    assert result is None


# ---------------------------------------------------------------------------
# _hilbert_sort
# ---------------------------------------------------------------------------

def test_hilbert_sort_preserves_data():
    """Hilbert sort should return same rows, possibly reordered."""
    gdf = _fake_gdf("Point", n=10)
    sorted_gdf = _hilbert_sort(gdf)
    assert len(sorted_gdf) == len(gdf)
    assert set(sorted_gdf["id"].tolist()) == set(gdf["id"].tolist())


def test_hilbert_sort_single_row():
    """Single-row GDF should be returned as-is."""
    gdf = _fake_gdf("Point", n=1)
    result = _hilbert_sort(gdf)
    assert len(result) == 1


def test_hilbert_sort_empty():
    """Empty GDF should be returned as-is."""
    gdf = gpd.GeoDataFrame({"id": []}, geometry=[], crs="EPSG:4326")
    result = _hilbert_sort(gdf)
    assert len(result) == 0
