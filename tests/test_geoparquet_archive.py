"""Archive GeoParquet: Hilbert order, bbox struct covering, engines, readers."""

from __future__ import annotations

import json
import warnings
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from shapely.geometry import LineString, Point, Polygon, box

from ras2cng import geoparquet
from ras2cng.geometry import _hilbert_sort


# ---------------------------------------------------------------------------
# Reference Hilbert encoder, transcribed from the Rust encoder (terrio::geoparquet)
# (crates/terrio/src/geoparquet.rs::hilbert_encode_16), scalar and independent of
# ras2cng's vectorised port.
# ---------------------------------------------------------------------------


def _ref_interleave(x: int) -> int:
    x = (x | (x << 8)) & 0x00FF00FF
    x = (x | (x << 4)) & 0x0F0F0F0F
    x = (x | (x << 2)) & 0x33333333
    x = (x | (x << 1)) & 0x55555555
    return x


def _ref_encode(x: int, y: int) -> int:
    x, y = x & 0xFFFF, y & 0xFFFF
    a = x ^ y
    b = 0xFFFF ^ a
    c = 0xFFFF ^ (x | y)
    d = x & (y ^ 0xFFFF)
    A, B = a | (b >> 1), (a >> 1) ^ a
    C = ((c >> 1) ^ (b & (d >> 1))) ^ c
    D = ((a & (c >> 1)) ^ (d >> 1)) ^ d
    for shift in (2, 4):
        a, b, c, d = A, B, C, D
        A = (a & (a >> shift)) ^ (b & (b >> shift))
        B = (a & (b >> shift)) ^ (b & ((a ^ b) >> shift))
        C ^= (a & (c >> shift)) ^ (b & (d >> shift))
        D ^= (b & (c >> shift)) ^ ((a ^ b) & (d >> shift))
    a, b, c, d = A, B, C, D
    C ^= (a & (c >> 8)) ^ (b & (d >> 8))
    D ^= (b & (c >> 8)) ^ ((a ^ b) & (d >> 8))
    a, b = C ^ (C >> 1), D ^ (D >> 1)
    i0 = x ^ y
    i1 = b | (0xFFFF ^ (i0 | a))
    return ((_ref_interleave(i1) << 1) | _ref_interleave(i0)) & 0xFFFFFFFF


def _ref_cell(v: float, lo: float, hi: float) -> int:
    width = hi - lo
    return int((v - lo) * (65535.0 / width)) if width > 0 else 0


def _ref_order(geoms) -> list[int]:
    bounds = [g.bounds for g in geoms]
    ext = (
        min(b[0] for b in bounds),
        min(b[1] for b in bounds),
        max(b[2] for b in bounds),
        max(b[3] for b in bounds),
    )
    keys = [
        _ref_encode(
            _ref_cell((b[0] + b[2]) / 2, ext[0], ext[2]),
            _ref_cell((b[1] + b[3]) / 2, ext[1], ext[3]),
        )
        for b in bounds
    ]
    return sorted(range(len(keys)), key=lambda i: (keys[i], i))


def _duckdb_spatial() -> bool:
    try:
        geoparquet._duckdb_spatial_connection()
        return True
    except Exception:
        return False


def _scrambled_points(n: int = 64, seed: int = 7) -> gpd.GeoDataFrame:
    rng = np.random.default_rng(seed)
    xy = rng.uniform(0, 1000, size=(n, 2))
    return gpd.GeoDataFrame(
        {"row": np.arange(n)}, geometry=[Point(x, y) for x, y in xy], crs="EPSG:26918"
    )


# ---------------------------------------------------------------------------
# Hilbert sort (task: hilbert_encode did not exist; archives were unsorted)
# ---------------------------------------------------------------------------


def test_hilbert_sort_actually_reorders_rows_by_st_hilbert():
    """Fails on the pre-0.13 code, which silently returned input order."""
    gdf = _scrambled_points()
    result = _hilbert_sort(gdf)
    expected = _ref_order(list(gdf.geometry))
    assert expected != list(range(len(gdf)))  # the input really is unsorted
    assert result["row"].tolist() == expected
    assert result.attrs["hilbert_sort"] in {"duckdb", "numpy"}


@pytest.mark.skipif(not _duckdb_spatial(), reason="DuckDB spatial extension unavailable")
def test_numpy_port_is_bit_identical_to_duckdb_st_hilbert():
    rng = np.random.default_rng(3)
    lo = rng.uniform(-1e6, 1e6, size=(500, 2))
    size = rng.uniform(0, 500, size=(500, 2))
    bounds = np.column_stack([lo, lo + size])
    bounds[5] = np.nan  # empty geometry
    keys_np, valid_np = geoparquet.hilbert_keys_numpy(bounds)
    keys_db, valid_db = geoparquet.hilbert_keys_duckdb(bounds)
    assert np.array_equal(valid_np, valid_db)
    assert np.array_equal(keys_np[valid_np], keys_db[valid_db])

    # A degenerate axis (every centre on one line) maps that axis to cell 0.
    flat = np.column_stack([lo[:, 0], np.full(500, 2.0), lo[:, 0] + 1, np.full(500, 2.0)])
    assert np.array_equal(geoparquet.hilbert_keys_numpy(flat)[0], geoparquet.hilbert_keys_duckdb(flat)[0])


def test_hilbert_sort_warns_and_records_when_duckdb_is_unavailable(monkeypatch):
    def boom(_bounds):
        raise RuntimeError("no spatial extension")

    monkeypatch.setattr(geoparquet, "hilbert_keys_duckdb", boom)
    gdf = _scrambled_points()
    with pytest.warns(RuntimeWarning, match="NumPy port"):
        result = _hilbert_sort(gdf)
    assert result.attrs["hilbert_sort"] == "numpy"
    assert result["row"].tolist() == _ref_order(list(gdf.geometry))


def test_hilbert_sort_puts_empty_geometry_last_and_keeps_ties_stable():
    gdf = gpd.GeoDataFrame(
        {"row": [0, 1, 2, 3]},
        geometry=[Point(5, 5), None, Point(0, 0), Point(5, 5)],
        crs="EPSG:26918",
    )
    result = _hilbert_sort(gdf)
    assert result["row"].tolist()[-1] == 1
    assert result["row"].tolist().index(0) < result["row"].tolist().index(3)


# ---------------------------------------------------------------------------
# Writer layout
# ---------------------------------------------------------------------------


def _mesh_layers(n: int = 30) -> gpd.GeoDataFrame:
    rng = np.random.default_rng(11)
    cells = gpd.GeoDataFrame(
        {
            "mesh_name": ["Main"] * n,
            "cell_id": np.arange(n, dtype="int64"),
            "min_elev": rng.uniform(500, 600, n),
            "layer": "mesh_cells",
        },
        geometry=[box(x, y, x + 10, y + 10) for x, y in rng.uniform(0, 1000, size=(n, 2))],
        crs="EPSG:26918",
    )
    faces = gpd.GeoDataFrame(
        {
            "mesh_name": ["Main"] * (n + 3),
            "face_id": np.arange(n + 3, dtype="int64"),
            "layer": "mesh_faces",
        },
        geometry=[
            LineString([(x, y), (x + 10, y)]) for x, y in rng.uniform(0, 1000, size=(n + 3, 2))
        ],
        crs="EPSG:26918",
    )
    area = gpd.GeoDataFrame(
        {"mesh_name": ["Main"], "layer": "mesh_areas"},
        geometry=[box(0, 0, 1010, 1010)],
        crs="EPSG:26918",
    )
    return geoparquet.concat_layers([cells, faces, area])


def test_concat_layers_keeps_integer_keys_integer():
    merged = _mesh_layers()
    assert str(merged["cell_id"].dtype) == "Int64"
    assert str(merged["face_id"].dtype) == "Int64"
    assert merged["cell_id"].isna().sum() == 34


def test_writer_uses_bbox_struct_covering_and_layer_row_groups(tmp_path: Path):
    merged = _mesh_layers()
    merged.loc[3, "geometry"] = Polygon()  # empty -> null bbox
    path = tmp_path / "M.g01.parquet"
    info = geoparquet.write_geoparquet(merged, path, hilbert=True, row_group_rows=16, engine="python")

    assert info["engine"] == "python" and info["bbox_covering"] == "struct"
    schema = pq.read_schema(path)
    assert schema.names[-2:] == ["layer", "bbox"]
    assert schema.field("bbox").type.names == list(geoparquet.BBOX_FIELDS)
    assert not set(geoparquet.LEGACY_BBOX_COLUMNS) & set(schema.names)
    assert not schema.field("layer").nullable

    geo = json.loads(pq.ParquetFile(path).metadata.metadata[b"geo"])
    assert geo["version"] == "1.1.0"
    assert geo["columns"]["geometry"]["covering"]["bbox"]["xmin"] == ["bbox", "xmin"]

    meta = pq.ParquetFile(path).metadata
    assert [meta.row_group(i).num_rows for i in range(meta.num_row_groups)] == [16, 14, 16, 16, 1, 1]
    assert info["row_groups"] == 6

    table = pq.read_table(path)
    assert table["layer"].to_pylist()[:30] == ["mesh_cells"] * 30
    bboxes = table["bbox"].to_pylist()
    assert bboxes[-1] == {"xmin": 0.0, "ymin": 0.0, "xmax": 1010.0, "ymax": 1010.0}
    # The empty cell sorts last in its layer and has a null bbox.
    assert bboxes[29] is None and table["cell_id"][29].as_py() == 3

    cells = merged[merged["layer"] == "mesh_cells"].drop(index=3).reset_index(drop=True)
    expected = cells.iloc[_ref_order(list(cells.geometry))]["cell_id"].astype(int).tolist()
    assert table["cell_id"].to_pylist()[:29] == expected

    # GeoPandas and DuckDB read the new file; a bbox struct filter works.
    assert len(gpd.read_parquet(path)) == len(merged)
    duckdb = pytest.importorskip("duckdb")
    count = duckdb.sql(
        f"SELECT count(*) FROM read_parquet('{path.as_posix()}') "
        "WHERE bbox.xmin <= 500 AND bbox.xmax >= 0"
    ).fetchone()[0]
    assert 0 < count < len(merged)


def test_writer_can_still_write_legacy_flat_columns(tmp_path: Path):
    path = tmp_path / "legacy.parquet"
    geoparquet.write_geoparquet(_mesh_layers(), path, covering="columns", engine="python")
    schema = pq.read_schema(path)
    assert set(geoparquet.LEGACY_BBOX_COLUMNS) <= set(schema.names)
    assert geoparquet.bbox_covering(schema) == "columns"


def test_python_engine_records_why_terrio_was_not_used(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(
        geoparquet, "native_writer_reason", lambda: "terrio is not installed"
    )
    monkeypatch.setattr(geoparquet, "_NATIVE_PROBE", {})
    monkeypatch.delenv(geoparquet.WRITER_ENV, raising=False)
    # auto (the default) prefers terrio: it falls back, says why, warns once.
    with pytest.warns(RuntimeWarning, match="not installed"):
        info = geoparquet.write_geoparquet(_mesh_layers(), tmp_path / "auto.parquet")
    assert info["engine"] == "python" and "not installed" in info["fallback_reason"]
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        info = geoparquet.write_geoparquet(_mesh_layers(), tmp_path / "a.parquet", engine="prefer-native")
    assert info["engine"] == "python"
    assert "not installed" in info["fallback_reason"]
    info = geoparquet.write_geoparquet(_mesh_layers(), tmp_path / "py.parquet", engine="python")
    assert info["engine"] == "python" and info["fallback_reason"] == ""

    monkeypatch.setenv(geoparquet.WRITER_ENV, "native")
    with pytest.raises(RuntimeError, match="cannot write"):
        geoparquet.write_geoparquet(_mesh_layers(), tmp_path / "b.parquet")
    monkeypatch.setenv(geoparquet.WRITER_ENV, "bogus")
    with pytest.raises(ValueError, match=geoparquet.WRITER_ENV):
        geoparquet.write_geoparquet(_mesh_layers(), tmp_path / "c.parquet")


needs_native = pytest.mark.skipif(
    not geoparquet.native_writer_available(),
    reason=f"terrio archive writer unusable: {geoparquet.native_writer_reason()}",
)


@needs_native
def test_native_and_python_engines_write_identical_files(tmp_path: Path):
    merged = _mesh_layers(200)
    native_path, python_path = tmp_path / "native.parquet", tmp_path / "python.parquet"
    native = geoparquet.write_geoparquet(merged, native_path, hilbert=True, row_group_rows=64, engine="native")
    python = geoparquet.write_geoparquet(merged, python_path, hilbert=True, row_group_rows=64, engine="python")
    assert native["engine"] == "terrio" and python["engine"] == "python"

    a, b = pq.read_table(python_path), pq.read_table(native_path)
    assert a.schema.equals(b.schema, check_metadata=False)
    assert all(x.equals(y, check_metadata=True) for x, y in zip(a.schema, b.schema))
    assert a.equals(b)  # rows and order
    geo = [json.loads(pq.ParquetFile(p).metadata.metadata[b"geo"]) for p in (python_path, native_path)]
    for meta in geo:
        # A set in the spec; both engines write it sorted.
        meta["columns"]["geometry"]["geometry_types"].sort()
    assert geo[0] == geo[1]
    groups = [
        [m.row_group(i).num_rows for i in range(m.num_row_groups)]
        for m in (pq.ParquetFile(python_path).metadata, pq.ParquetFile(native_path).metadata)
    ]
    assert groups[0] == groups[1]


@needs_native
def test_native_engine_writes_float32_and_nested_columns(tmp_path: Path):
    merged = _mesh_layers(120)
    merged["weir_coef"] = np.float32(2.6)
    merged["stations"] = [[float(i), float(i) + 1.0] for i in range(len(merged))]
    native, python = tmp_path / "n.parquet", tmp_path / "p.parquet"
    info = geoparquet.write_geoparquet(merged, native, hilbert=True, hilbert_column="hilbert_index", engine="native")
    assert info["engine"] == "terrio" and info["hilbert_column"] == "hilbert_index"
    geoparquet.write_geoparquet(
        merged, python, hilbert=True, hilbert_column="hilbert_index", engine="python"
    )
    a, b = pq.read_table(python), pq.read_table(native)
    assert all(x.equals(y, check_metadata=True) for x, y in zip(a.schema, b.schema))
    assert a.equals(b)
    assert b.schema.field("weir_coef").type == pa.float32()


def _assert_same_file(python_path: Path, native_path: Path) -> None:
    a, b = pq.read_table(python_path), pq.read_table(native_path)
    assert all(x.equals(y, check_metadata=True) for x, y in zip(a.schema, b.schema))
    assert a.equals(b)
    geo = [json.loads(pq.ParquetFile(p).metadata.metadata[b"geo"]) for p in (python_path, native_path)]
    assert geo[0] == geo[1]
    groups = [
        [m.row_group(i).num_rows for i in range(m.num_row_groups)]
        for m in (pq.ParquetFile(python_path).metadata, pq.ParquetFile(native_path).metadata)
    ]
    assert groups[0] == groups[1]


@needs_native
def test_native_engine_writes_z_coordinates_like_pyarrow(tmp_path: Path):
    from shapely import force_3d

    merged = _mesh_layers()
    merged.geometry = force_3d(merged.geometry.values)
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        info = geoparquet.write_geoparquet(merged, tmp_path / "n.parquet", hilbert=True, engine="native")
    assert info["engine"] == "terrio" and info["fallback_reason"] == ""
    geoparquet.write_geoparquet(merged, tmp_path / "p.parquet", hilbert=True, engine="python")
    _assert_same_file(tmp_path / "p.parquet", tmp_path / "n.parquet")


@needs_native
@pytest.mark.parametrize("covering", ["struct", "columns"])
def test_native_engine_matches_pyarrow_without_layer_column_or_order(tmp_path: Path, covering):
    single = _mesh_layers(50)
    single = single[single["layer"] == "mesh_cells"].drop(columns=["layer"])
    for name, kwargs in [("single", dict(layer_column=None)), ("input", dict(hilbert=False))]:
        frame = single if name == "single" else _mesh_layers(50)
        n, p = tmp_path / f"{name}_n.parquet", tmp_path / f"{name}_p.parquet"
        info = geoparquet.write_geoparquet(frame, n, covering=covering, engine="native", **kwargs)
        assert info["engine"] == "terrio"
        geoparquet.write_geoparquet(frame, p, covering=covering, engine="python", **kwargs)
        _assert_same_file(p, n)


@needs_native
def test_native_engine_matches_pyarrow_on_wider_types(tmp_path: Path):
    """Types the retired fast-hdf contract refused: large lists, tz-aware time."""
    merged = _mesh_layers(40)
    merged["when"] = pd.Timestamp("2026-10-29T12:00:00Z")
    n, p = tmp_path / "n.parquet", tmp_path / "p.parquet"
    info = geoparquet.write_geoparquet(merged, n, hilbert=True, engine="native")
    assert info["engine"] == "terrio"
    geoparquet.write_geoparquet(merged, p, hilbert=True, engine="python")
    _assert_same_file(p, n)


def test_native_failure_falls_back_or_raises(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(geoparquet, "native_writer_reason", lambda: None)
    monkeypatch.setattr(geoparquet, "_NATIVE_PROBE", {})

    def refuse(*args, **kwargs):
        raise ValueError("boom")

    monkeypatch.setattr(geoparquet, "_write_native", refuse)
    with pytest.warns(RuntimeWarning, match="terrio refused the file"):
        info = geoparquet.write_geoparquet(_mesh_layers(), tmp_path / "a.parquet", engine="prefer-native")
    assert info["engine"] == "python" and "boom" in info["fallback_reason"]
    with pytest.raises(ValueError, match="boom"):
        geoparquet.write_geoparquet(_mesh_layers(), tmp_path / "b.parquet", engine="native")


# ---------------------------------------------------------------------------
# Readers: both covering forms
# ---------------------------------------------------------------------------


def _legacy_file(path: Path) -> None:
    """A schema-2.6 style file: flat bbox columns, alphabetical layer order."""
    merged = _mesh_layers()
    geoparquet.write_geoparquet(merged, path, covering="columns", engine="python")


def test_spatial_index_keeps_each_files_covering_form(tmp_path: Path):
    from ras2cng.spatial_index import HILBERT_COLUMN, postprocess_geoparquet

    legacy, current = tmp_path / "legacy.parquet", tmp_path / "current.parquet"
    _legacy_file(legacy)
    geoparquet.write_geoparquet(_mesh_layers(), current, hilbert=True, engine="python")
    before = pq.read_table(current)

    assert postprocess_geoparquet(legacy)["bbox_covering"] == "columns"
    assert geoparquet.bbox_covering(pq.read_schema(legacy)) == "columns"

    result = postprocess_geoparquet(current)
    assert result["bbox_covering"] == "struct"
    after = pq.read_table(current)
    # Already in writer order: the index pass only appends the key column.
    assert after.drop_columns([HILBERT_COLUMN]).equals(before)
    assert after["layer"].to_pylist() == before["layer"].to_pylist()
    meta = pq.ParquetFile(current).metadata
    assert meta.num_row_groups == 3  # one per layer, not one for the file
    keys = after[HILBERT_COLUMN].to_numpy()[:30]
    assert (np.diff(keys.astype("int64")) >= 0).all()


def test_readers_drop_or_flatten_the_bbox_struct(tmp_path: Path):
    path = tmp_path / "a.parquet"
    geoparquet.write_geoparquet(_mesh_layers(), path, engine="python")
    frame = pd.read_parquet(path)
    assert "bbox" in frame.columns
    flat = geoparquet.flatten_bbox_struct(frame)
    assert "bbox" not in flat.columns
    assert flat["bbox_xmax"].dtype == "float64"
    first = frame["bbox"].iloc[0]
    assert flat["bbox_xmin"].iloc[0] == first["xmin"]
    assert "bbox" not in geoparquet.drop_covering_columns(frame).columns

    from ras2cng import maplibre

    assert {"bbox", "bbox_xmin"} <= maplibre._INTERNAL_COLUMNS


def test_duckdb_session_queries_both_covering_forms(tmp_path: Path):
    pytest.importorskip("duckdb")
    from ras2cng.duckdb_session import query_parquet

    legacy, current = tmp_path / "legacy.parquet", tmp_path / "current.parquet"
    _legacy_file(legacy)
    geoparquet.write_geoparquet(_mesh_layers(), current, engine="python")
    old = query_parquet(legacy, "SELECT count(*) AS n FROM _ WHERE bbox_xmin < 500")
    new = query_parquet(current, "SELECT count(*) AS n FROM _ WHERE bbox.xmin < 500")
    assert int(old["n"].iloc[0]) == int(new["n"].iloc[0]) > 0


# ---------------------------------------------------------------------------
# Archive layout: dictionaries on strings only, key written by the writer
# ---------------------------------------------------------------------------


def test_dictionary_encoding_is_limited_to_string_columns(tmp_path: Path):
    path = tmp_path / "a.parquet"
    geoparquet.write_geoparquet(_mesh_layers(200), path, hilbert=True, engine="python")
    group = pq.ParquetFile(path).metadata.row_group(0)
    by_name = {group.column(i).path_in_schema: group.column(i) for i in range(group.num_columns)}
    assert by_name["layer"].has_dictionary_page
    assert by_name["mesh_name"].has_dictionary_page
    for numeric in ("cell_id", "min_elev", "bbox.xmin", "geometry"):
        assert not by_name[numeric].has_dictionary_page, numeric
    assert by_name["cell_id"].compression == "ZSTD"


def test_writer_stores_hilbert_index_and_post_pass_keeps_the_file(tmp_path: Path):
    from ras2cng.catalog import Manifest, ManifestGeomEntry
    from ras2cng.spatial_index import HILBERT_COLUMN, postprocess_archive

    archive = tmp_path / "archive"
    path = archive / "M.g01.parquet"
    info = geoparquet.write_geoparquet(
        _mesh_layers(), path, hilbert=True, hilbert_column=HILBERT_COLUMN, engine="python"
    )
    assert info["hilbert_column"] == HILBERT_COLUMN
    written = pq.read_table(path)
    assert written.schema.names[-2:] == ["bbox", HILBERT_COLUMN]
    keys = written[HILBERT_COLUMN].to_numpy(zero_copy_only=False)[:30]
    assert (np.diff(keys.astype("int64")) >= 0).all()

    manifest = Manifest.create("M", tmp_path / "M.prj", tmp_path, archive)
    entry = ManifestGeomEntry(geom_id="g01", source_file="M.g01.hdf", file_type="hdf", parquet=path.name)
    entry.geoparquet = info
    manifest.add_geom_entry(entry)
    before = path.stat().st_mtime_ns
    summary = postprocess_archive(archive, manifest=manifest, write_manifest=False)
    assert summary["geometry_files"][0]["method"] == "written_by_archive_writer"
    assert path.stat().st_mtime_ns == before

    # The post-pass computes the same key when it does rewrite.
    from ras2cng.spatial_index import postprocess_geoparquet

    postprocess_geoparquet(path)
    assert pq.read_table(path).equals(written)


def test_auto_engine_default_is_one_switch(monkeypatch):
    monkeypatch.delenv(geoparquet.WRITER_ENV, raising=False)
    assert geoparquet._requested_engine(None) == geoparquet.PREFER_NATIVE
    assert geoparquet._requested_engine("prefer-native") == geoparquet.PREFER_NATIVE
    monkeypatch.setattr(geoparquet, "AUTO_PREFERS_NATIVE", False)
    assert geoparquet._requested_engine(None) == "python"
    assert geoparquet._requested_engine("prefer-native") == geoparquet.PREFER_NATIVE
    assert geoparquet._requested_engine("rust") == "native"
    assert geoparquet._requested_engine("terrio") == "native"
    assert geoparquet._requested_engine("fast-hdf") == "native"  # old setting
