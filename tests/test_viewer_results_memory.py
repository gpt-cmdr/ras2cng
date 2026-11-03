"""The low-memory result join delivers exactly what the GeoDataFrame join delivers."""

from __future__ import annotations

import json
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from shapely.geometry import LineString, Point, Polygon, box

from ras2cng import geoparquet, maplibre, vector_tiles


CRS = "EPSG:2278"
X0, Y0 = 3_100_000.0, 13_800_000.0


def _cell(index: int) -> Polygon:
    x, y = X0 + index * 100.0, Y0 + (index % 7) * 100.0
    return Polygon([(x, y, 1.0), (x + 90, y, 2.0), (x + 90, y + 90, 3.0), (x, y + 90, float("nan")), (x, y, 1.0)])


def _geometry_frame() -> gpd.GeoDataFrame:
    """Two meshes sharing cell IDs, faces, a null and an empty geometry, and a sparse layer."""

    rows = [
        {"layer": "mesh_cells", "mesh_name": "A" if i < 20 else "B", "cell_id": i % 20, "geometry": _cell(i)}
        for i in range(30)
    ]
    rows += [{"layer": "mesh_cells", "mesh_name": "B", "cell_id": 90, "geometry": None}]
    rows += [{"layer": "mesh_cells", "mesh_name": "B", "cell_id": 91, "geometry": Polygon()}]
    rows += [
        {
            "layer": "mesh_faces",
            "mesh_name": "A",
            "face_id": i,
            "geometry": LineString([(X0 + i, Y0), (X0 + i + 50, Y0 + 50)]),
        }
        for i in range(25)
    ]
    rows += [{"layer": "bc_lines", "Name": "BC", "geometry": LineString([(X0, Y0), (X0 + 500, Y0)])}]
    frame = gpd.GeoDataFrame(rows, geometry="geometry", crs=CRS)
    frame["hilbert_index"] = np.arange(len(frame), dtype="uint64")
    return frame


def _plan_frame() -> gpd.GeoDataFrame:
    """A consolidated plan file: rows of every variable, each with its own columns."""

    time = pd.Timestamp("2020-01-01 12:30:00")
    frames = []
    # Rows out of geometry order, one cell without a result (cell 3 of both meshes).
    cells = [i for i in reversed(range(20)) if i != 3]
    frames.append(
        pd.DataFrame(
            {
                "layer": "maximum_water_surface",
                "mesh_name": "A",
                "cell_id": cells,
                "maximum_water_surface": np.array(cells, dtype="float32") + 0.5,
                "maximum_water_surface_time": [time + pd.Timedelta(minutes=i) for i in cells],
                "is_ghost_cell": [i == 5 for i in cells],
                "hilbert_index": cells,
            }
        )
    )
    frames.append(
        pd.DataFrame(
            {
                "layer": "cell_last_iteration",
                "mesh_name": "A",
                "cell_id": cells,
                # int64 in this layer's rows, but with nulls across the file.
                "cell_last_iteration": cells,
                "hilbert_index": cells,
            }
        )
    )
    frames.append(
        pd.DataFrame(
            {
                "layer": "maximum_face_velocity",
                "mesh_name": "A",
                "face_id": list(range(25)),
                "maximum_face_velocity": [float("nan") if i == 2 else i * 0.25 for i in range(25)],
                "hilbert_index": list(range(25)),
            }
        )
    )
    frame = pd.concat(frames, ignore_index=True)
    # Nullable through the union, as in an archive.
    for column in ("cell_last_iteration", "cell_id", "face_id"):
        frame[column] = frame[column].astype("Int64")
    frame = frame.astype({"cell_last_iteration": "float64", "cell_id": "float64", "face_id": "float64"})
    geometry = [Point(X0 + i, Y0) for i in range(len(frame))]
    return gpd.GeoDataFrame(frame, geometry=geometry, crs=CRS)


def _integers_nullable(path: Path, columns: dict[str, pa.DataType]) -> None:
    """Rewrite float64 columns as nullable integers, as the archive writer stores them."""

    table = pq.read_table(path)
    for name, data_type in columns.items():
        index = table.schema.get_field_index(name)
        table = table.set_column(index, pa.field(name, data_type), table.column(index).cast(data_type))
    pq.write_table(table, path, row_group_size=16)


@pytest.fixture(params=["archive-writer", "to_parquet"])
def archive(request, tmp_path: Path) -> tuple[Path, Path]:
    geometry_path, plan_path = tmp_path / "model.g01.parquet", tmp_path / "model.p01.parquet"
    if request.param == "archive-writer":
        geoparquet.write_geoparquet(_geometry_frame(), geometry_path, engine="python", row_group_rows=16)
        geoparquet.write_geoparquet(_plan_frame(), plan_path, engine="python", row_group_rows=16)
    else:
        _geometry_frame().to_parquet(geometry_path)
        _plan_frame().to_parquet(plan_path)
    _integers_nullable(plan_path, {"cell_last_iteration": pa.int64(), "cell_id": pa.int64(), "face_id": pa.int64()})
    return geometry_path, plan_path


def _spill(geometry_path: Path, kind: str, tmp_path: Path, **kwargs) -> maplibre._DenseGeometry:
    dense = maplibre._spill_dense_geometry(geometry_path, kind, kind, CRS, tmp_path / "spill", kind, **kwargs)
    assert dense is not None
    return dense


def _same_schema(table: pa.Table, expected: pa.Table) -> None:
    """Field names and types; pandas strings are large_string, which tile the same."""

    def types(schema: pa.Schema) -> list[tuple[str, pa.DataType]]:
        return [(f.name, pa.string() if pa.types.is_large_string(f.type) else f.type) for f in schema]

    assert types(table.schema) == types(expected.schema)


def _pandas_table(geometry_path: Path, plan_path: Path, kind: str, index: str, variable: str) -> pa.Table:
    """What the viewer delivered through pandas: read the whole file, filter, merge, shape."""

    frame = maplibre._to_wgs84(maplibre._read_layer(geometry_path, kind), geometry_path, CRS)
    plan = pd.read_parquet(plan_path)
    plan = plan.loc[plan["layer"] == variable].drop(columns=["geometry"])
    joined = frame.merge(plan, on=index, how="inner", suffixes=("", "_result"))
    joined = gpd.GeoDataFrame(joined, geometry="geometry", crs=frame.crs)
    return vector_tiles.frame_table(maplibre._drop_internal_columns(joined))


@pytest.mark.parametrize(
    "kind, index, variable",
    [
        ("mesh_cells", "cell_id", "maximum_water_surface"),
        ("mesh_cells", "cell_id", "cell_last_iteration"),
        ("mesh_faces", "face_id", "maximum_face_velocity"),
    ],
)
def test_join_delivers_the_same_table_as_the_pandas_join(archive, tmp_path, kind, index, variable):
    geometry_path, plan_path = archive
    expected = _pandas_table(geometry_path, plan_path, kind, index, variable)
    dense = _spill(geometry_path, kind, tmp_path)

    joined = maplibre._join_dense_result(
        plan_path, dense, index, {"layer": variable}, tmp_path / "spill" / "joined.arrow"
    )
    assert joined is not None
    layer, count, types, bounds = joined
    table = layer.load()

    _same_schema(table, expected)
    assert table.to_pylist() == expected.to_pylist()
    assert count == expected.num_rows
    frame = gpd.GeoDataFrame(geometry=gpd.GeoSeries.from_wkb(expected.column("geometry").to_pylist()), crs="EPSG:4326")
    assert bounds == pytest.approx(frame.total_bounds.tolist(), abs=0)
    assert types == sorted(set(frame.geom_type))
    assert layer.crs == vector_tiles.WGS84 and layer.kind == kind
    assert layer.polygon_simplify is None


def test_integer_results_stay_doubles_when_the_file_has_nulls_there(archive, tmp_path):
    geometry_path, plan_path = archive
    joined = maplibre._join_dense_result(
        plan_path,
        _spill(geometry_path, "mesh_cells", tmp_path),
        "cell_id",
        {"layer": "cell_last_iteration"},
        tmp_path / "joined.arrow",
    )
    table = joined[0].load()
    assert table.schema.field("cell_last_iteration").type == pa.float64()
    # The name suffixes come from the geometry's columns, delivered or not.
    assert not {"mesh_name_result", "layer_result", "hilbert_index_result"} & set(table.column_names)
    assert "bbox" not in table.column_names and "face_id_result" not in table.column_names


@pytest.mark.parametrize("variable", ["maximum_water_surface", "cell_last_iteration", "maximum_face_velocity"])
def test_read_result_table_equals_the_whole_file_read(archive, variable):
    _, plan_path = archive
    expected = pd.read_parquet(plan_path)
    expected = expected.loc[expected["layer"] == variable].drop(columns=["geometry"])
    frame = maplibre._read_result_table(plan_path, {"layer": variable}).to_pandas()
    assert list(frame.columns) == list(expected.columns)
    for name in frame.columns:
        if pd.api.types.is_numeric_dtype(expected[name]) and expected[name].dtype != bool:
            assert frame[name].dtype == expected[name].dtype, name
        got, want = frame[name].tolist(), expected[name].tolist()
        assert [None if pd.isna(v) else v for v in map(_plain, got)] == [
            None if pd.isna(v) else v for v in map(_plain, want)
        ], name


def _plain(value):
    return json.dumps(value, sort_keys=True) if isinstance(value, dict) else value


def test_read_result_table_refuses_an_absent_filter_column(tmp_path):
    path = tmp_path / "plan.parquet"
    pd.DataFrame({"cell_id": [1]}).to_parquet(path)
    with pytest.raises(ValueError, match="'layer' is absent"):
        maplibre._read_result_table(path, {"layer": "x"})


def test_join_raw_result_does_not_read_the_whole_plan_file(archive, monkeypatch):
    geometry_path, plan_path = archive
    frame = maplibre._to_wgs84(maplibre._read_layer(geometry_path, "mesh_cells"), geometry_path, CRS)

    def refuse(*_args, **_kwargs):
        raise AssertionError("whole-file read")

    monkeypatch.setattr(maplibre.pd, "read_parquet", refuse)
    joined = maplibre._join_raw_result(plan_path, frame, "cell_id", filters={"layer": "cell_last_iteration"})
    assert len(joined) and "cell_last_iteration" in joined


def test_spill_equals_the_frame_table_and_skips_null_and_empty_geometry(archive, tmp_path):
    geometry_path, _ = archive
    frame = maplibre._to_wgs84(maplibre._read_layer(geometry_path, "mesh_cells"), geometry_path, CRS)
    expected = vector_tiles.frame_table(maplibre._drop_internal_columns(frame))

    dense = _spill(geometry_path, "mesh_cells", tmp_path, batch_size=7)
    table = dense.table()

    assert dense.count == len(frame) == 30
    _same_schema(table, expected)
    assert table.to_pylist() == expected.to_pylist()
    assert list(dense.bounds) == frame.total_bounds.tolist()
    assert list(dense.geometry_types) == sorted(set(frame.geom_type))
    assert list(dense.columns) == list(frame.columns)


def test_spill_takes_the_same_feature_size_sample_across_batches(archive, tmp_path):
    geometry_path, _ = archive
    frame = maplibre._to_wgs84(maplibre._read_layer(geometry_path, "mesh_faces"), geometry_path, CRS)
    sizes: list[np.ndarray] = []
    _spill(geometry_path, "mesh_faces", tmp_path, batch_size=7, pixel_sizes=sizes)
    assert np.concatenate(sizes).tolist() == maplibre._feature_pixel_sizes(frame.geometry.values[::10]).tolist()


def test_spill_of_a_layer_with_no_usable_geometry_has_no_features(tmp_path):
    path = tmp_path / "g.parquet"
    gpd.GeoDataFrame(
        {"layer": ["mesh_cells"], "cell_id": [1]}, geometry=[Polygon()], crs=CRS
    ).to_parquet(path)
    assert maplibre._spill_dense_geometry(path, "mesh_cells", "mesh_cells", CRS, tmp_path, "x").count == 0


def test_spill_refuses_a_layer_with_no_crs_as_the_frame_path_does(tmp_path):
    path = tmp_path / "g.parquet"
    gpd.GeoDataFrame({"layer": ["mesh_cells"], "cell_id": [1]}, geometry=[box(0, 0, 1, 1)]).to_parquet(path)
    with pytest.raises(ValueError, match="no CRS"):
        maplibre._spill_dense_geometry(path, "mesh_cells", "mesh_cells", None, tmp_path, "x")


def test_a_column_arrow_and_pandas_could_deliver_differently_stays_on_pandas(archive, tmp_path):
    geometry_path, plan_path = archive
    dense = _spill(geometry_path, "mesh_faces", tmp_path)
    table = pq.read_table(plan_path)
    values = pa.array([[1, 2]] * table.num_rows, type=pa.list_(pa.int64()))
    pq.write_table(table.append_column("listed", values), tmp_path / "nested.parquet")
    assert (
        maplibre._join_dense_result(
            tmp_path / "nested.parquet", dense, "face_id", {"layer": "maximum_face_velocity"}, tmp_path / "x.arrow"
        )
        is None
    )
    # All null there: it delivers no property on either path, so the join can go on.
    pq.write_table(
        table.append_column("listed", pa.nulls(table.num_rows, type=pa.list_(pa.int64()))), tmp_path / "null.parquet"
    )
    joined = maplibre._join_dense_result(
        tmp_path / "null.parquet", dense, "face_id", {"layer": "maximum_face_velocity"}, tmp_path / "y.arrow"
    )
    assert joined is not None and "listed" not in joined[0].load().column_names


def test_stored_pandas_index_or_extension_types_stay_on_pandas(tmp_path):
    frame = pd.DataFrame({"cell_id": [1, 2], "value": pd.array([1, None], dtype="Int64")})
    frame.to_parquet(tmp_path / "plain.parquet")
    assert not maplibre._pandas_metadata_is_plain(pq.read_schema(tmp_path / "plain.parquet"))
    pd.DataFrame({"cell_id": [1, 2]}, index=pd.Index([5, 6], name="stored")).to_parquet(tmp_path / "index.parquet")
    assert not maplibre._pandas_metadata_is_plain(pq.read_schema(tmp_path / "index.parquet"))
    pd.DataFrame({"cell_id": [1, 2]}).to_parquet(tmp_path / "range.parquet")
    assert maplibre._pandas_metadata_is_plain(pq.read_schema(tmp_path / "range.parquet"))


def test_a_missing_index_column_is_refused(archive, tmp_path):
    geometry_path, plan_path = archive
    dense = _spill(geometry_path, "mesh_cells", tmp_path)
    with pytest.raises(ValueError, match="index 'nope' is not present"):
        maplibre._join_dense_result(plan_path, dense, "nope", {"layer": "cell_last_iteration"}, tmp_path / "x.arrow")


def test_an_empty_join_has_no_layer(archive, tmp_path):
    geometry_path, plan_path = archive
    dense = _spill(geometry_path, "mesh_cells", tmp_path)
    layer, count, *_ = maplibre._join_dense_result(plan_path, dense, "cell_id", {"layer": "nope"}, tmp_path / "x.arrow")
    assert layer is None and count == 0


def _viewer_archive(tmp_path: Path, geometry_path: Path, plan_path: Path, *, join_columns=None) -> tuple[Path, Path]:
    variables = [
        {
            "variable": variable,
            "filter_value": variable,
            "parquet": "",
            "geometry_filter": kind,
            "index_column": "" if join_columns else index,
            "join_columns": join_columns or {},
        }
        for variable, kind, index in (
            ("maximum_water_surface", "mesh_cells", "cell_id"),
            ("cell_last_iteration", "mesh_cells", "cell_id"),
            ("maximum_face_velocity", "mesh_faces", "face_id"),
        )
    ]
    layers = ["mesh_cells", "mesh_faces", "bc_lines"]
    manifest = {
        "project": {"name": "Model"},
        "geometry": [
            {
                "geom_id": "g01",
                "parquet": geometry_path.name,
                "layers": [{"layer": layer, "filter_value": layer} for layer in layers],
            }
        ],
        "results": [{"plan_id": "p01", "geom_id": "g01", "parquet": plan_path.name, "variables": variables}],
    }
    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    hdf = tmp_path / "model.g01.hdf"
    hdf.touch()
    return tmp_path, hdf


def _package(monkeypatch, archive_dir: Path, hdf: Path, output: Path):
    extent = gpd.GeoDataFrame(geometry=[box(X0, Y0, X0 + 3000, Y0 + 700)], crs=CRS).to_crs("EPSG:4326")
    monkeypatch.setattr(maplibre, "_extent_from_hdf", lambda *_: extent)
    return maplibre.package_maplibre_viewer(
        archive_dir, output, geometry_hdfs={"g01": hdf}, include_vector_results=True, crs=CRS
    )


def _without_timings(value):
    if isinstance(value, dict):
        return {key: _without_timings(item) for key, item in value.items() if key != "seconds"}
    if isinstance(value, list):
        return [_without_timings(item) for item in value]
    return value


def test_viewer_tiles_are_byte_identical_to_the_pandas_path(archive, tmp_path, monkeypatch):
    pytest.importorskip("freestiler")
    monkeypatch.setenv("RAS2CNG_VECTOR_TILER", "freestiler")
    if not vector_tiles.arrow_input_enabled():
        pytest.skip("needs a freestiler with Arrow input")
    geometry_path, plan_path = archive
    archive_dir, hdf = _viewer_archive(geometry_path.parent, geometry_path, plan_path)

    calls: list[str] = []
    original = maplibre._join_raw_result
    monkeypatch.setattr(
        maplibre, "_join_raw_result", lambda *a, **k: calls.append(str(a[0])) or original(*a, **k)
    )
    fast = _package(monkeypatch, archive_dir, hdf, tmp_path / "fast")
    assert not calls, "every mesh result should take the Arrow join"

    # The pandas path for the same archive: no spill, so no fast join.
    monkeypatch.setattr(maplibre, "_spill_dense_geometry", lambda *a, **k: None)
    slow = _package(monkeypatch, archive_dir, hdf, tmp_path / "slow")
    assert len(calls) == 3

    assert fast.result_detail_pmtiles.read_bytes() == slow.result_detail_pmtiles.read_bytes()
    assert (tmp_path / "fast/tiles/geometry-detail.pmtiles").read_bytes() == (
        tmp_path / "slow/tiles/geometry-detail.pmtiles"
    ).read_bytes()
    assert _without_timings(json.loads(fast.manifest_path.read_text("utf-8"))) == _without_timings(
        json.loads(slow.manifest_path.read_text("utf-8"))
    )


def test_a_mesh_result_on_composite_keys_goes_through_the_checked_join(archive, tmp_path, monkeypatch):
    pytest.importorskip("freestiler")
    monkeypatch.setenv("RAS2CNG_VECTOR_TILER", "freestiler")
    if not vector_tiles.arrow_input_enabled():
        pytest.skip("needs a freestiler with Arrow input")
    geometry_path, plan_path = archive
    archive_dir, hdf = _viewer_archive(
        geometry_path.parent, geometry_path, plan_path, join_columns={"mesh_name": "mesh_name"}
    )
    # Every result row of a mesh has the same mesh name: not a unique key.
    with pytest.raises(ValueError, match="composite result keys are not unique"):
        _package(monkeypatch, archive_dir, hdf, tmp_path / "viewer")


def test_join_of_a_file_per_variable_and_of_a_profile_matches_pandas(tmp_path):
    geometry_path = tmp_path / "g.parquet"
    geoparquet.write_geoparquet(_geometry_frame(), geometry_path, engine="python")
    dense = _spill(geometry_path, "mesh_cells", tmp_path)
    frame = maplibre._to_wgs84(maplibre._read_layer(geometry_path, "mesh_cells"), geometry_path, CRS)
    values = pd.DataFrame(
        {
            "cell_id": [1, 2, 1, 2],
            "profile": ["PF 1", "PF 1", "PF 2", "PF 2"],
            "wse": [1.0, 2.0, 3.0, 4.0],
        }
    )
    path = tmp_path / "variable.parquet"
    values.to_parquet(path)

    for filters, rows in ((None, values), ({"profile": "PF 2"}, values[values["profile"] == "PF 2"])):
        joined = maplibre._join_dense_result(path, dense, "cell_id", filters, tmp_path / "joined.arrow")
        expected = vector_tiles.frame_table(
            maplibre._drop_internal_columns(
                gpd.GeoDataFrame(frame.merge(rows, on="cell_id", how="inner"), geometry="geometry", crs=frame.crs)
            )
        )
        table = joined[0].load()
        _same_schema(table, expected)
        assert table.to_pylist() == expected.to_pylist()
        assert joined[1] == expected.num_rows
        # Only some cells have a value, so the extent is theirs, not the layer's.
        taken = gpd.GeoSeries.from_wkb(expected.column("geometry").to_pylist())
        assert joined[3] == taken.total_bounds.tolist()
