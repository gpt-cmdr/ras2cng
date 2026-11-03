"""A result row attaches to its own mesh's element: IDs restart in every 2D flow area."""

from __future__ import annotations

from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from shapely.geometry import LineString

from ras2cng import geoparquet, maplibre, vector_tiles

CRS = "EPSG:2278"
X0, Y0 = 3_100_000.0, 13_800_000.0


def _line(i: int) -> LineString:
    return LineString([(X0 + i * 100.0, Y0), (X0 + i * 100.0 + 50.0, Y0 + 50.0)])


def _faces() -> gpd.GeoDataFrame:
    """Davis's shape: a 3-face mesh and a 5-face mesh with the same IDs from 0."""

    mesh = ["small"] * 3 + ["large"] * 5
    ids = [0, 1, 2, 0, 1, 2, 3, 4]
    frame = gpd.GeoDataFrame(
        {"layer": "mesh_faces", "mesh_name": mesh, "face_id": ids},
        geometry=[_line(i) for i in range(8)],
        crs=CRS,
    )
    frame["hilbert_index"] = np.arange(len(frame), dtype="uint64")
    return frame


def _plan(rows: pd.DataFrame | None = None) -> gpd.GeoDataFrame:
    if rows is None:
        # Each value names its element, so a wrong attachment cannot pass by chance.
        rows = pd.DataFrame(
            {
                "mesh_name": ["large"] * 5 + ["small"] * 3,
                "face_id": [4, 3, 2, 1, 0, 2, 1, 0],
                "velocity": [104.0, 103.0, 102.0, 101.0, 100.0, 2.0, 1.0, 0.0],
            }
        )
    rows = rows.assign(layer="velocity", hilbert_index=np.arange(len(rows)))
    return gpd.GeoDataFrame(rows, geometry=[_line(i) for i in range(len(rows))], crs=CRS)


def _write(tmp_path: Path, plan: gpd.GeoDataFrame | None = None) -> tuple[Path, Path]:
    geometry_path, plan_path = tmp_path / "m.g01.parquet", tmp_path / "m.p01.parquet"
    geoparquet.write_geoparquet(_faces(), geometry_path, engine="python")
    geoparquet.write_geoparquet(_plan() if plan is None else plan, plan_path, engine="python")
    return geometry_path, plan_path


def _dense(geometry_path: Path, tmp_path: Path) -> maplibre._DenseGeometry:
    dense = maplibre._spill_dense_geometry(geometry_path, "mesh_faces", "mesh_faces", CRS, tmp_path / "s", "faces")
    assert dense is not None
    return dense


def _attach(table_or_frame) -> dict[tuple[str, int], float]:
    frame = table_or_frame.to_pandas() if isinstance(table_or_frame, pa.Table) else table_or_frame
    assert len(frame) == 8
    return {(row.mesh_name, int(row.face_id)): row.velocity for row in frame.itertuples()}


EXPECTED = {
    ("small", 0): 0.0,
    ("small", 1): 1.0,
    ("small", 2): 2.0,
    ("large", 0): 100.0,
    ("large", 1): 101.0,
    ("large", 2): 102.0,
    ("large", 3): 103.0,
    ("large", 4): 104.0,
}


def test_id_alone_is_the_old_defect():
    """Pinned so this test file says what went wrong: 8 faces became 14 features."""

    faces, plan = _faces(), pd.DataFrame(_plan().drop(columns="geometry"))
    assert len(faces.merge(plan, on="face_id", suffixes=("", "_result"))) == 14


def test_both_joins_attach_each_value_to_its_own_mesh(tmp_path):
    geometry_path, plan_path = _write(tmp_path)
    frame = maplibre._to_wgs84(maplibre._read_layer(geometry_path, "mesh_faces"), geometry_path, CRS)

    pandas_join = maplibre._join_raw_result(plan_path, frame, "face_id", filters={"layer": "velocity"})
    assert _attach(pandas_join) == EXPECTED

    joined = maplibre._join_dense_result(
        plan_path, _dense(geometry_path, tmp_path), "face_id", {"layer": "velocity"}, tmp_path / "j.arrow"
    )
    assert joined is not None and joined[1] == 8
    table = joined[0].load()
    assert _attach(table) == EXPECTED

    # Same rows, order, geometry and values through either path.
    expected = vector_tiles.frame_table(maplibre._drop_internal_columns(pandas_join))
    assert table.select(expected.column_names).to_pylist() == expected.to_pylist()
    assert table.column(table.schema.get_field_index("geometry")).to_pylist() == expected.column("geometry").to_pylist()


def test_the_key_is_the_id_alone_without_mesh_name_on_both_sides():
    assert maplibre._index_join_keys("cell_id", ["mesh_name", "cell_id"], ["cell_id"]) == ["cell_id"]
    assert maplibre._index_join_keys("cell_id", ["cell_id"], ["mesh_name", "cell_id"]) == ["cell_id"]
    assert maplibre._index_join_keys("cell_id", ["mesh_name", "cell_id"], ["mesh_name", "cell_id"]) == [
        "mesh_name",
        "cell_id",
    ]
    assert maplibre._index_join_keys("station_id", ["station_id"], ["station_id"]) == ["station_id"]


def test_a_result_with_no_mesh_name_on_a_multi_mesh_geometry_is_refused(tmp_path):
    """No mesh name cannot tell the meshes apart: refuse, never guess."""

    geometry_path, _ = _write(tmp_path)
    rows = pd.DataFrame({"face_id": [0, 1, 2, 3, 4], "velocity": [1.0, 2.0, 3.0, 4.0, 5.0]})
    plan_path = tmp_path / "variable.parquet"
    rows.to_parquet(plan_path)
    frame = maplibre._to_wgs84(maplibre._read_layer(geometry_path, "mesh_faces"), geometry_path, CRS)
    with pytest.raises(ValueError, match="not unique"):
        maplibre._join_raw_result(plan_path, frame, "face_id")
    # The fast join leaves it to that check.
    assert (
        maplibre._join_dense_result(plan_path, _dense(geometry_path, tmp_path), "face_id", None, tmp_path / "j.arrow")
        is None
    )


def test_repeated_result_keys_are_refused(tmp_path):
    rows = _plan(
        pd.DataFrame({"mesh_name": ["small", "small"], "face_id": [0, 0], "velocity": [1.0, 2.0]})
    )
    geometry_path, plan_path = _write(tmp_path, rows)
    frame = maplibre._to_wgs84(maplibre._read_layer(geometry_path, "mesh_faces"), geometry_path, CRS)
    with pytest.raises(ValueError, match="not unique"):
        maplibre._join_raw_result(plan_path, frame, "face_id", filters={"layer": "velocity"})
    assert (
        maplibre._join_dense_result(
            plan_path, _dense(geometry_path, tmp_path), "face_id", {"layer": "velocity"}, tmp_path / "j.arrow"
        )
        is None
    )


def test_unpaired_rows_and_features_are_reported(tmp_path):
    rows = _plan(
        pd.DataFrame(
            {
                "mesh_name": ["small", "small", "elsewhere"],
                "face_id": [0, 1, 0],
                "velocity": [0.0, 1.0, 9.0],
            }
        )
    )
    geometry_path, plan_path = _write(tmp_path, rows)
    frame = maplibre._to_wgs84(maplibre._read_layer(geometry_path, "mesh_faces"), geometry_path, CRS)
    message = "1 of 3 result rows have no feature and 6 of 8 features"
    with pytest.warns(RuntimeWarning, match=message):
        joined = maplibre._join_raw_result(plan_path, frame, "face_id", filters={"layer": "velocity"})
    assert len(joined) == 2
    with pytest.warns(RuntimeWarning, match=message):
        fast = maplibre._join_dense_result(
            plan_path, _dense(geometry_path, tmp_path), "face_id", {"layer": "velocity"}, tmp_path / "j.arrow"
        )
    assert fast[1] == 2


def test_a_complete_join_does_not_warn(tmp_path, recwarn):
    geometry_path, plan_path = _write(tmp_path)
    maplibre._join_dense_result(
        plan_path, _dense(geometry_path, tmp_path), "face_id", {"layer": "velocity"}, tmp_path / "j.arrow"
    )
    assert not [w for w in recwarn if issubclass(w.category, RuntimeWarning) and "Raw result join" in str(w.message)]


def test_delivered_columns_have_one_mesh_name_and_no_join_copies(tmp_path):
    geometry_path, plan_path = _write(tmp_path)
    table = maplibre._join_dense_result(
        plan_path, _dense(geometry_path, tmp_path), "face_id", {"layer": "velocity"}, tmp_path / "j.arrow"
    )[0].load()
    assert table.column_names.count("mesh_name") == 1
    assert not {"mesh_name_result", "layer_result", "hilbert_index_result"} & set(table.column_names)
    assert pq.read_schema(plan_path).names.count("mesh_name") == 1
