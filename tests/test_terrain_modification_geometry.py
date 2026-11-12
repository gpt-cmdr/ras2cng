"""Terrain-modification geometry read from synthetic terrain HDFs.

The fixtures use RAS Mapper's on-disk layout (RasMapperLib ``HDF5Storage``):
``Polyline/Polygon Info`` rows ``[point start, point count, part start, part
count]``, ``Parts`` rows ``[start within the feature, count]``, one
``Attributes`` row per feature, and the affine shape datasets ``Piers`` (15
columns), ``Rectangles``, ``Circles`` (5) and ``Triangles`` (9).
"""

from __future__ import annotations

import math

import h5py
import numpy as np
import pandas as pd
import pytest
from shapely.geometry import Point

from ras2cng.terrain import extract_terrain_modification_layers

CRS = "EPSG:2271"

PIER_COLUMNS = (
    "Body X, Body Y, Body Width, Body Height, TopCap X, TopCap Y, TopCap RadiusX, "
    "TopCap RadiusY, TopCap Type, BottomCap X, BottomCap Y, BottomCap RadiusX, "
    "BottomCap RadiusY, BottomCap Type, AngleRotation"
)


def _ground_line_attributes(rows):
    dtype = np.dtype([
        ("System Name", "S128"), ("Name", "S128"), ("Elevation Type", "S16"),
        ("Top Elevation", "<f4"), ("Top Width", "<f4"), ("Left Slope", "<f4"),
        ("Right Slope", "<f4"), ("Max Reach", "<f4"),
    ])
    return np.array(rows, dtype=dtype)


def _shape_attributes(names, values):
    dtype = np.dtype([("Name", "S64"), ("Elevation Value", "<f4"), ("Elevation Type", "S16")])
    return np.array(
        [(n.encode(), v, b"Replace") for n, v in zip(names, values)], dtype=dtype
    )


def _layers(path):
    return extract_terrain_modification_layers(path, crs=CRS)


def test_channel_layer_with_two_features_keeps_each_width(tmp_path):
    path = tmp_path / "Terrain.hdf"
    with h5py.File(path, "w") as hdf:
        group = hdf.create_group("Modifications/Channels")
        group.attrs["Type"] = np.bytes_("Levee")
        group.attrs["Subtype"] = np.bytes_("Channel")
        group.create_dataset("Polyline Info", data=np.array(
            [[0, 2, 0, 1], [2, 3, 1, 1]], dtype="int32"))
        group.create_dataset("Polyline Parts", data=np.array([[0, 2], [0, 3]], dtype="int32"))
        group.create_dataset("Polyline Points", data=np.array(
            [[0.0, 0.0], [100.0, 0.0],
             [0.0, 500.0], [50.0, 500.0], [50.0, 600.0]]))
        group.create_dataset("Attributes", data=_ground_line_attributes([
            (b"Channel 1", b"Channel 1", b"Replace", 10.0, 40.0, 3.0, -3.0, 60.0),
            (b"Channel 2", b"Channel 2", b"Replace", 12.0, 20.0, 2.0, -2.0, np.nan),
        ]))

    layers = _layers(path)
    lines = layers["terrain_modification_lines"]
    assert list(lines["feature_name"]) == ["Channel 1", "Channel 2"]
    assert list(lines["feature_index"]) == [0, 1]
    # Before: both features' points were read as one LineString of 5 points.
    assert [len(g.coords) for g in lines.geometry] == [2, 3]
    assert list(lines["width"]) == [40.0, 20.0]
    assert lines.iloc[0]["max_extent"] == 60.0
    assert pd.isna(lines.iloc[1]["max_extent"])
    assert lines.iloc[0]["left_slope"] == 3.0

    footprints = layers["terrain_modification_footprints"].set_index("feature_name")
    first = footprints.loc["Channel 1"]
    assert first["footprint_basis"] == "max_extent"
    assert first["footprint_half_width"] == 60.0
    # Flat-capped offset polygon: 100 ft long, 2 x 60 ft wide.
    assert first.geometry.area == pytest.approx(100.0 * 120.0)
    assert first.geometry.contains(Point(50.0, 59.0))
    assert not first.geometry.contains(Point(50.0, 61.0))
    second = footprints.loc["Channel 2"]
    assert second["footprint_basis"] == "top_width"
    assert second["footprint_half_width"] == 10.0
    assert second.geometry.contains(Point(25.0, 509.0))


def test_pier_layers_produce_outlines(tmp_path):
    path = tmp_path / "Terrain.hdf"
    with h5py.File(path, "w") as hdf:
        round_piers = hdf.create_group("Modifications/Round_Piers")
        round_piers.attrs["Type"] = np.bytes_("Pier")
        # Body 4 ft wide x 20 ft long, round noses of radius 3 ft on both ends,
        # rotated 90 degrees clockwise: the pier axis runs along +x.
        data = np.zeros((2, 15))
        data[0, :] = [1000.0, 2000.0, 4.0, 20.0, 1000.0, 2010.0, 2.0, 3.0, 0,
                      1000.0, 1990.0, 2.0, 3.0, 0, 90.0]
        # Unrotated body only.
        data[1, :] = [1100.0, 2000.0, 6.0, 10.0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0.0]
        dataset = round_piers.create_dataset("Piers", data=data)
        dataset.attrs["Column Ordering"] = np.bytes_(PIER_COLUMNS)
        round_piers.create_dataset(
            "Attributes", data=_shape_attributes(["P1", "P2"], [5.0, 6.0]))

        sharp = hdf.create_group("Modifications/Elongated_Piers")
        sharp.attrs["Type"] = np.bytes_("Pier")
        # No body: one sharp nose on the centre (base 4 ft, height 2 x 3 ft).
        sharp.create_dataset("Piers", data=np.array(
            [[0, 0, 0, 0, 500.0, 500.0, 2.0, 3.0, 1, 0, 0, 0, 0, 0, 0.0]]))

    layers = _layers(path)
    polygons = layers["terrain_modification_polygons"]
    assert set(polygons["shape_type"]) == {"pier"}
    # Before: pier layers produced no geometry at all.
    assert len(polygons) == 3
    by_name = polygons.set_index("feature_name")

    p1 = by_name.loc["P1"].geometry
    half_ellipse = math.pi * 2.0 * 3.0 / 2.0
    assert p1.area == pytest.approx(4.0 * 20.0 + 2 * half_ellipse, rel=0.01)
    minx, miny, maxx, maxy = p1.bounds
    # Rotated 90 deg clockwise: 10 ft body + 3 ft nose either side along x.
    assert minx == pytest.approx(1000.0 - 13.0, abs=1e-6)
    assert maxx == pytest.approx(1000.0 + 13.0, abs=1e-6)
    assert miny == pytest.approx(2000.0 - 2.0, abs=1e-3)
    assert maxy == pytest.approx(2000.0 + 2.0, abs=1e-3)
    assert by_name.loc["P1"]["elevation_value"] == 5.0
    assert by_name.loc["P1"]["elevation_type"] == "Replace"

    p2 = by_name.loc["P2"].geometry
    assert p2.bounds == pytest.approx((1097.0, 1995.0, 1103.0, 2005.0))

    nose = by_name.loc["Elongated_Piers"].geometry
    assert nose.area == pytest.approx(0.5 * 4.0 * 6.0)
    assert nose.bounds == pytest.approx((498.0, 497.0, 502.0, 503.0))

    footprints = layers["terrain_modification_footprints"]
    assert set(footprints["footprint_basis"]) == {"pier"}
    assert len(footprints) == 3


def test_rectangle_ellipse_and_triangle_layers(tmp_path):
    path = tmp_path / "Terrain.hdf"
    with h5py.File(path, "w") as hdf:
        mods = hdf.create_group("Modifications")
        mods.create_group("Boxes").create_dataset(
            "Rectangles", data=np.array([[10.0, 10.0, 4.0, 2.0, 90.0]]))
        mods.create_group("Mounds").create_dataset(
            "Circles", data=np.array([[50.0, 50.0, 5.0, 5.0, 0.0]]))
        mods.create_group("Wedges").create_dataset(
            "Triangles", data=np.array([[0.0, 0.0, -1.0, 0.0, 1.0, 0.0, 0.0, 3.0, 0.0]]))

    polygons = _layers(path)["terrain_modification_polygons"].set_index("name")
    assert polygons.loc["Boxes"]["shape_type"] == "rectangle"
    # 4 ft x 2 ft rotated 90 degrees: 2 ft along x, 4 ft along y.
    assert polygons.loc["Boxes"].geometry.bounds == pytest.approx((9.0, 8.0, 11.0, 12.0))
    assert polygons.loc["Mounds"].geometry.area == pytest.approx(math.pi * 25.0, rel=0.01)
    assert polygons.loc["Wedges"].geometry.area == pytest.approx(3.0)


def test_polygon_layer_with_two_features_and_a_hole(tmp_path):
    path = tmp_path / "Terrain.hdf"
    with h5py.File(path, "w") as hdf:
        group = hdf.create_group("Modifications/Fills")
        group.attrs["Type"] = np.bytes_("Polygon")
        outer = [[0, 0], [10, 0], [10, 10], [0, 10]]
        hole = [[4, 4], [6, 4], [6, 6], [4, 6]]
        other = [[20, 0], [30, 0], [30, 10]]
        group.create_dataset("Polygon Info", data=np.array(
            [[0, 8, 0, 2], [8, 3, 2, 1]], dtype="int32"))
        group.create_dataset("Polygon Parts", data=np.array(
            [[0, 4], [4, 4], [0, 3]], dtype="int32"))
        group.create_dataset(
            "Polygon Points", data=np.array(outer + hole + other, dtype="float64"))

    polygons = _layers(path)["terrain_modification_polygons"]
    assert list(polygons["feature_index"]) == [0, 1]
    assert polygons.geometry.iloc[0].area == pytest.approx(100.0 - 4.0)
    assert polygons.geometry.iloc[1].area == pytest.approx(50.0)


def _collect(group, metadata):
    from ras2cng.terrain import TERRAIN_MODIFICATION_LAYERS, _collect_modification_group

    rows = {name: [] for name in TERRAIN_MODIFICATION_LAYERS}
    _collect_modification_group("Layer", group, metadata, rows)
    return rows


def test_feature_rows_without_a_field_keep_the_layer_value(tmp_path):
    # An Attributes row with a name but no Max Reach / Top Width field must not
    # drop the layer-level values (and with them the footprint).
    with h5py.File(tmp_path / "t.hdf", "w") as hdf:
        group = hdf.create_group("Modifications/Layer")
        group.create_dataset("Polyline Points", data=np.array([[0.0, 0.0], [100.0, 0.0]]))
        group.create_dataset("Attributes", data=np.array(
            [(b"Line 1",)], dtype=np.dtype([("Name", "S32")])))
        rows = _collect(group, {"width": 30.0, "max_extent": 45.0})
    (line,) = rows["terrain_modification_lines"]
    assert line["feature_name"] == "Line 1"
    assert line["width"] == 30.0 and line["max_extent"] == 45.0
    (footprint,) = rows["terrain_modification_footprints"]
    assert footprint["footprint_basis"] == "max_extent"


def test_feature_field_present_but_invalid_is_none(tmp_path):
    with h5py.File(tmp_path / "t.hdf", "w") as hdf:
        group = hdf.create_group("Modifications/Layer")
        group.create_dataset("Polyline Points", data=np.array([[0.0, 0.0], [100.0, 0.0]]))
        group.create_dataset("Attributes", data=_ground_line_attributes([
            (b"L", b"L", b"Replace", 10.0, 40.0, 3.0, -3.0, np.nan),
        ]))
        rows = _collect(group, {"max_extent": 45.0})
    (line,) = rows["terrain_modification_lines"]
    assert line["max_extent"] is None
    assert rows["terrain_modification_footprints"][0]["footprint_basis"] == "top_width"


def test_boundary_elevations_are_per_polygon_feature(tmp_path):
    with h5py.File(tmp_path / "t.hdf", "w") as hdf:
        group = hdf.create_group("Modifications/Layer")
        group.create_dataset("Polygon Info", data=np.array(
            [[0, 3, 0, 1], [3, 3, 1, 1]], dtype="int32"))
        group.create_dataset("Polygon Parts", data=np.array([[0, 3], [0, 3]], dtype="int32"))
        group.create_dataset("Polygon Points", data=np.array(
            [[0, 0], [10, 0], [0, 10], [100, 100], [110, 100], [100, 110]], dtype="float64"))
        group.create_dataset("Boundary Elevations", data=np.array(
            [1.0, 2.0, 3.0, 10.0, 20.0, 30.0]))
        rows = _collect(group, {})
    polygons = rows["terrain_modification_polygons"]
    assert [(p["boundary_elevation_min"], p["boundary_elevation_max"]) for p in polygons] == [
        (1.0, 3.0), (10.0, 30.0)
    ]


def test_repaired_ring_keeps_only_polygonal_parts():
    from shapely.geometry import GeometryCollection, LineString, MultiPolygon, Polygon

    from ras2cng.terrain import _polygonal_part

    square = Polygon([(0, 0), (1, 0), (1, 1), (0, 1)])
    mixed = GeometryCollection([square, LineString([(2, 2), (3, 3)])])
    assert _polygonal_part(mixed).equals(square)
    assert _polygonal_part(GeometryCollection([LineString([(0, 0), (1, 1)])])) is None
    two = GeometryCollection([square, Polygon([(5, 5), (6, 5), (6, 6)])])
    assert isinstance(_polygonal_part(two), MultiPolygon)
