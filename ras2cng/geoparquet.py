"""Archive GeoParquet writer: bbox covering, Hilbert order, layer row groups.

One writer serves every consolidated archive file (``Model.g01.parquet``,
``Model.p01.parquet``). It has two engines that produce the same rows, row
order, Arrow schema, GeoParquet metadata and row-group layout:

``fast-hdf``
    ``ras_commander_fast_hdf.export_geoparquet_ipc_files`` (fast-hdf PR #36),
    used when the installed extension has it. It is a strict, contract-bound
    profile: every layer needs a stable row identity, contract types (no
    float32, no list columns), finite floats and 2D WKB. A file that does not
    fit falls back to the Python engine as a whole, and the reason is
    returned so the archive manifest can record it.

``python``
    pyarrow, always available.

``RAS2CNG_GEOPARQUET_WRITER`` = ``auto`` (default) | ``native`` | ``prefer-native`` | ``python``.
``auto`` currently selects ``python``: on Bald Eagle p07 the native engine wrote
identical files but was not faster end to end (p07: 7.7-8.6 s vs ~5 s, most of
it ras2cng-side input staging) and 4.4% larger (Rust Parquet page indexes).
``native`` requires fast-hdf and raises when a file does not fit its contract;
``prefer-native`` tries it and falls back to ``python`` with a warning and the
reason recorded in the manifest.

Layout (both engines):

- rows grouped by layer in first-appearance order; with ``hilbert=True``
  each layer is ordered by the Hilbert index of each row's bbox centre over
  the layer's extent, exactly DuckDB spatial's ``ST_Hilbert(x, y, box)``
  (16 bits per axis). Ties keep input order; null/empty geometry sorts last.
- a GeoParquet 1.1 ``bbox`` struct (``xmin, ymin, xmax, ymax``; null for
  null/empty geometry) with ``covering`` metadata. Archives written by
  ras2cng 0.12 and earlier carry four flat ``bbox_xmin`` ... ``bbox_ymax``
  columns instead; :func:`bbox_covering` and :func:`read_bounds` read both.
- ZSTD; a row group ends every ``row_group_rows`` rows and at every layer
  boundary, so a ``layer`` filter or a bbox filter can skip row groups.
"""

from __future__ import annotations

import json
import os
import threading
import warnings
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

BBOX_COLUMN = "bbox"
BBOX_FIELDS = ("xmin", "ymin", "xmax", "ymax")
LEGACY_BBOX_COLUMNS = ("bbox_xmin", "bbox_ymin", "bbox_xmax", "bbox_ymax")
COVERING_STRUCT = "struct"
COVERING_COLUMNS = "columns"
ROW_GROUP_ROWS = 65536
# ZSTD level for archive files. With dictionary encoding limited to string
# columns (below), level 3 makes Bald Eagle p07 14.7 MB against 19.9 MB at
# level 1 with dictionaries everywhere, and writes faster.
ZSTD_LEVEL = 3
GEOPARQUET_VERSION = "1.1.0"

WRITER_ENV = "RAS2CNG_GEOPARQUET_WRITER"
ENGINE_NATIVE = "fast-hdf"
ENGINE_PYTHON = "python"

# Stable row identities of the layers ras2cng writes, for fast-hdf's export
# contract (Utf8 namespace, optional Int64 local id). Layers not listed here
# get a discovered identity, or send the file to the Python engine.
KNOWN_IDENTITY: dict[str, tuple[str, Optional[str]]] = {
    "mesh_cells": ("mesh_name", "cell_id"),
    "mesh_faces": ("mesh_name", "face_id"),
    "mesh_areas": ("mesh_name", None),
    "bc_lines": ("Name", "bc_line_id"),
    "breaklines": ("Name", "bl_id"),
    "storage_areas": ("Name", None),
    "mannings_n_regions": ("Name", "region_id"),
}


# ---------------------------------------------------------------------------
# Hilbert order (DuckDB spatial ST_Hilbert semantics)
# ---------------------------------------------------------------------------


def _interleave16(x: np.ndarray) -> np.ndarray:
    x = (x | (x << 8)) & 0x00FF00FF
    x = (x | (x << 4)) & 0x0F0F0F0F
    x = (x | (x << 2)) & 0x33333333
    x = (x | (x << 1)) & 0x55555555
    return x


def _hilbert_encode_16(x: np.ndarray, y: np.ndarray) -> np.ndarray:
    """Branch-free 16-bit Hilbert encoding, the form DuckDB spatial uses."""

    x = np.asarray(x, dtype=np.uint32) & np.uint32(0xFFFF)
    y = np.asarray(y, dtype=np.uint32) & np.uint32(0xFFFF)
    m = np.uint32(0xFFFF)
    a = x ^ y
    b = m ^ a
    c = m ^ (x | y)
    d = x & (y ^ m)
    big_a = a | (b >> 1)
    big_b = (a >> 1) ^ a
    big_c = ((c >> 1) ^ (b & (d >> 1))) ^ c
    big_d = ((a & (c >> 1)) ^ (d >> 1)) ^ d
    for shift in (2, 4):
        a, b, c, d = big_a, big_b, big_c, big_d
        big_a = (a & (a >> shift)) ^ (b & (b >> shift))
        big_b = (a & (b >> shift)) ^ (b & ((a ^ b) >> shift))
        big_c = big_c ^ ((a & (c >> shift)) ^ (b & (d >> shift)))
        big_d = big_d ^ ((b & (c >> shift)) ^ ((a ^ b) & (d >> shift)))
    a, b, c, d = big_a, big_b, big_c, big_d
    big_c = big_c ^ ((a & (c >> 8)) ^ (b & (d >> 8)))
    big_d = big_d ^ ((b & (c >> 8)) ^ ((a ^ b) & (d >> 8)))
    a = big_c ^ (big_c >> 1)
    b = big_d ^ (big_d >> 1)
    i0 = x ^ y
    i1 = b | (m ^ (i0 | a))
    return ((_interleave16(i1) << 1) | _interleave16(i0)).astype(np.uint32)


def _hilbert_cell(values: np.ndarray, lo: float, hi: float) -> np.ndarray:
    width = hi - lo
    if not width > 0:
        return np.zeros(len(values), dtype=np.uint32)
    scaled = (values - lo) * (65535.0 / width)
    # Rust/C++ float -> u32 casts truncate toward zero and saturate.
    return np.clip(scaled, 0, 4294967295).astype(np.uint32)


def _as_bounds(bounds) -> np.ndarray:
    array = np.asarray(bounds, dtype="float64")
    if array.ndim != 2 or array.shape[1] != 4:
        raise ValueError("bounds must be an (n, 4) array of xmin, ymin, xmax, ymax")
    return array


def _extent_and_centres(bounds: np.ndarray):
    valid = np.isfinite(bounds).all(axis=1)
    if not valid.any():
        return valid, None, None, None
    b = bounds[valid]
    extent = (b[:, 0].min(), b[:, 1].min(), b[:, 2].max(), b[:, 3].max())
    cx = (b[:, 0] + b[:, 2]) / 2.0
    cy = (b[:, 1] + b[:, 3]) / 2.0
    return valid, extent, cx, cy


def hilbert_keys_numpy(bounds) -> tuple[np.ndarray, np.ndarray]:
    """Hilbert index of each bbox centre over the union of the bboxes.

    Returns ``(keys, valid)``; ``keys`` is uint32 and meaningless where
    ``valid`` is False (null or empty geometry). Bit-identical to DuckDB
    spatial ``ST_Hilbert(x, y, extent)`` and to fast-hdf's archive writer.
    """

    bounds = _as_bounds(bounds)
    keys = np.zeros(len(bounds), dtype=np.uint32)
    valid, extent, cx, cy = _extent_and_centres(bounds)
    if extent is not None:
        keys[valid] = _hilbert_encode_16(
            _hilbert_cell(cx, extent[0], extent[2]),
            _hilbert_cell(cy, extent[1], extent[3]),
        )
    return keys, valid


_DUCKDB = {"con": None}
_DUCKDB_LOCK = threading.Lock()


def _duckdb_spatial_connection():
    """A process-wide DuckDB connection with spatial loaded.

    ``LOAD spatial`` costs about 0.25 s, which dominated archive writing when
    it was paid once per layer; callers hold ``_DUCKDB_LOCK`` while using it.
    """

    if _DUCKDB["con"] is None:
        import duckdb

        con = duckdb.connect()
        try:
            con.execute("LOAD spatial")
        except Exception:
            con.execute("INSTALL spatial")
            con.execute("LOAD spatial")
        _DUCKDB["con"] = con
    return _DUCKDB["con"]


def hilbert_keys_duckdb(bounds) -> tuple[np.ndarray, np.ndarray]:
    """Same as :func:`hilbert_keys_numpy`, computed by DuckDB ``ST_Hilbert``."""

    bounds = _as_bounds(bounds)
    keys = np.zeros(len(bounds), dtype=np.uint32)
    valid, extent, cx, cy = _extent_and_centres(bounds)
    if extent is None:
        return keys, valid
    points = pa.table({"_i": np.arange(len(cx), dtype=np.int64), "_x": cx, "_y": cy})
    with _DUCKDB_LOCK:
        con = _duckdb_spatial_connection()
        con.register("_ras2cng_points", points)
        try:
            result = con.execute(
                "SELECT ST_Hilbert(_x, _y, {'min_x': $1, 'min_y': $2, 'max_x': $3, 'max_y': $4}::BOX_2D) "
                "AS h FROM _ras2cng_points ORDER BY _i",
                [float(v) for v in extent],
            ).fetchnumpy()["h"]
        finally:
            con.unregister("_ras2cng_points")
    keys[valid] = np.asarray(result, dtype=np.uint32)
    return keys, valid


def hilbert_keys(bounds, *, engine: str = "auto") -> tuple[np.ndarray, np.ndarray, str]:
    """Hilbert keys by DuckDB ``ST_Hilbert``, or the bit-identical NumPy port.

    ``engine`` is ``auto`` (DuckDB, warning and falling back to NumPy if the
    DuckDB spatial extension cannot be loaded), ``duckdb`` or ``numpy``.
    Returns ``(keys, valid, method)``.
    """

    if engine == "numpy":
        return (*hilbert_keys_numpy(bounds), "numpy")
    try:
        return (*hilbert_keys_duckdb(bounds), "duckdb")
    except Exception as exc:
        if engine == "duckdb":
            raise
        warnings.warn(
            "DuckDB spatial ST_Hilbert is unavailable "
            f"({type(exc).__name__}: {exc}); using ras2cng's bit-identical NumPy port.",
            RuntimeWarning,
            stacklevel=2,
        )
        return (*hilbert_keys_numpy(bounds), "numpy")


def hilbert_permutation(keys: np.ndarray, valid: np.ndarray) -> np.ndarray:
    """Stable order by key; rows without a bbox go last in input order."""

    sort_keys = np.where(valid, np.asarray(keys, dtype=np.uint64), np.uint64(np.iinfo(np.uint64).max))
    return np.argsort(sort_keys, kind="stable")


# ---------------------------------------------------------------------------
# Bounding boxes and covering metadata
# ---------------------------------------------------------------------------


def geometry_bounds(geometry) -> np.ndarray:
    """``(n, 4)`` bounds over every coordinate; NaN rows for null/empty."""

    import shapely

    values = getattr(geometry, "values", geometry)
    return np.asarray(shapely.bounds(np.asarray(values, dtype=object)), dtype="float64").reshape(-1, 4)


def bbox_struct_array(bounds) -> pa.StructArray:
    bounds = _as_bounds(bounds)
    invalid = ~np.isfinite(bounds).all(axis=1)
    children = [pa.array(bounds[:, i], type=pa.float64(), mask=invalid) for i in range(4)]
    return pa.StructArray.from_arrays(
        children, names=list(BBOX_FIELDS), mask=pa.array(invalid, type=pa.bool_())
    )


def covering_metadata(form: str) -> dict:
    if form == COVERING_STRUCT:
        return {"bbox": {name: [BBOX_COLUMN, name] for name in BBOX_FIELDS}}
    if form == COVERING_COLUMNS:
        return {
            "bbox": {name: [column] for name, column in zip(BBOX_FIELDS, LEGACY_BBOX_COLUMNS)}
        }
    raise ValueError(f"bbox covering must be {COVERING_STRUCT!r} or {COVERING_COLUMNS!r}; got {form!r}")


def bbox_covering(schema: pa.Schema) -> Optional[str]:
    """Return ``"struct"``, ``"columns"`` or None for a GeoParquet schema."""

    names = set(schema.names)
    if BBOX_COLUMN in names and pa.types.is_struct(schema.field(BBOX_COLUMN).type):
        child_names = {schema.field(BBOX_COLUMN).type.field(i).name for i in range(schema.field(BBOX_COLUMN).type.num_fields)}
        if set(BBOX_FIELDS).issubset(child_names):
            return COVERING_STRUCT
    if set(LEGACY_BBOX_COLUMNS).issubset(names):
        return COVERING_COLUMNS
    return None


def bbox_sql_columns(form: Optional[str]) -> list[str]:
    """SQL expressions for xmin, ymin, xmax, ymax in either covering form."""

    if form == COVERING_STRUCT:
        return [f"{BBOX_COLUMN}.{name}" for name in BBOX_FIELDS]
    if form == COVERING_COLUMNS:
        return list(LEGACY_BBOX_COLUMNS)
    return []


def read_bounds(table: pa.Table) -> Optional[np.ndarray]:
    """``(n, 4)`` bounds from a table's covering columns (either form)."""

    form = bbox_covering(table.schema)
    if form == COVERING_STRUCT:
        struct = table[BBOX_COLUMN].combine_chunks()
        if isinstance(struct, pa.ChunkedArray):  # pragma: no cover - old pyarrow
            struct = pa.concat_arrays(struct.chunks)
        validity = struct.is_valid().to_numpy(zero_copy_only=False)
        columns = []
        for name in BBOX_FIELDS:
            child = struct.field(name).to_numpy(zero_copy_only=False).astype("float64")
            columns.append(np.where(validity, child, np.nan))
        return np.column_stack(columns) if columns else None
    if form == COVERING_COLUMNS:
        return np.column_stack(
            [
                np.asarray(table[name].to_numpy(), dtype="float64")
                for name in LEGACY_BBOX_COLUMNS
            ]
        )
    return None


def covering_column_names(columns: Iterable[str]) -> list[str]:
    """Names among ``columns`` that are bbox covering columns of either form."""

    wanted = {BBOX_COLUMN, *LEGACY_BBOX_COLUMNS}
    return [name for name in columns if name in wanted]


def drop_covering_columns(frame):
    """Drop bbox covering columns (both forms) from a (Geo)DataFrame."""

    names = covering_column_names(frame.columns)
    return frame.drop(columns=names) if names else frame


def flatten_bbox_struct(frame):
    """Replace a ``bbox`` struct column with the four flat float columns.

    Used where a consumer cannot store a struct (PostGIS via GeoPandas), so
    the table keeps the same shape for archives of either covering form.
    """

    if BBOX_COLUMN not in frame.columns:
        return frame
    values = frame[BBOX_COLUMN]
    frame = frame.drop(columns=[BBOX_COLUMN])
    for name, column in zip(BBOX_FIELDS, LEGACY_BBOX_COLUMNS):
        if column in frame.columns:
            continue
        frame[column] = [
            (value or {}).get(name) if isinstance(value, dict) or value is None else np.nan
            for value in values
        ]
        frame[column] = pd.to_numeric(frame[column], errors="coerce").astype("float64")
    return frame


def geo_metadata_with_covering(raw_geo: bytes | str, form: str, *, file_bbox=None) -> bytes:
    geo_meta = json.loads(raw_geo.decode("utf-8") if isinstance(raw_geo, bytes) else raw_geo)
    geo_meta["version"] = GEOPARQUET_VERSION
    geom_col = geo_meta.get("primary_column", "geometry")
    col_meta = geo_meta.setdefault("columns", {}).setdefault(geom_col, {})
    col_meta["covering"] = covering_metadata(form)
    if file_bbox is not None:
        col_meta["bbox"] = [float(v) for v in file_bbox]
    return json.dumps(geo_meta).encode("utf-8")


# ---------------------------------------------------------------------------
# DataFrame normalisation shared by both engines
# ---------------------------------------------------------------------------


def concat_layers(frames: Sequence[pd.DataFrame], *, geometry: Optional[str] = "geometry"):
    """Concatenate per-layer frames without widening integer columns to float.

    ``pd.concat`` turns an integer column that is absent from some layers
    into float64 (``cell_id`` 3 -> 3.0). Columns that are integer (or bool) in
    every layer that has them become pandas nullable ``Int64`` (``boolean``)
    instead, so the archive keeps the type the source produced.
    """

    import geopandas as gpd

    integer: dict[str, bool] = {}
    boolean: dict[str, bool] = {}
    for frame in frames:
        for column in frame.columns:
            dtype = frame[column].dtype
            is_int = pd.api.types.is_integer_dtype(dtype) and not pd.api.types.is_bool_dtype(dtype)
            integer[column] = integer.get(column, True) and is_int
            boolean[column] = boolean.get(column, True) and pd.api.types.is_bool_dtype(dtype)
    frames = list(frames)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", FutureWarning)
        merged = pd.concat(frames, ignore_index=True)
    targets = [c for c in merged.columns if integer.get(c) or boolean.get(c)]
    if targets:
        # Assemble each nullable column from the per-layer pieces in NumPy and
        # swap them in with one block operation; per-column astype on the
        # merged frame cost ~0.5 s per geometry file.
        lengths = [len(frame) for frame in frames]
        starts = np.concatenate([[0], np.cumsum(lengths)[:-1]]).astype(int)
        converted = {}
        for column in targets:
            is_bool = bool(boolean.get(column))
            unsigned = not is_bool and all(
                pd.api.types.is_unsigned_integer_dtype(frame[column].dtype)
                for frame in frames
                if column in frame.columns
            )
            dtype = np.bool_ if is_bool else (np.uint64 if unsigned else np.int64)
            values = np.zeros(len(merged), dtype=dtype)
            mask = np.ones(len(merged), dtype=bool)
            for frame, start, length in zip(frames, starts, lengths):
                if column not in frame.columns or not length:
                    continue
                piece = frame[column]
                piece_mask = piece.isna().to_numpy()
                values[start : start + length] = piece.to_numpy(dtype=dtype, na_value=0)
                mask[start : start + length] = piece_mask
            if is_bool:
                converted[column] = pd.arrays.BooleanArray(values, mask)
            else:
                converted[column] = pd.arrays.IntegerArray(values, mask)
        order = list(merged.columns)
        rest = merged.drop(columns=targets)
        merged = pd.concat([rest, pd.DataFrame(converted, index=rest.index)], axis=1)[order]
    if geometry and geometry in merged.columns:
        return gpd.GeoDataFrame(merged, geometry=geometry)
    return merged


def _layer_slices(frame, layer_column: Optional[str]) -> list[tuple[Optional[str], np.ndarray]]:
    if not layer_column or layer_column not in frame.columns:
        return [(None, np.arange(len(frame)))]
    codes, uniques = pd.factorize(frame[layer_column], sort=False)
    if (codes < 0).any():
        raise ValueError(f"The {layer_column!r} column must not contain nulls.")
    order = np.argsort(codes, kind="stable")
    counts = np.bincount(codes, minlength=len(uniques))
    out, start = [], 0
    for name, count in zip(uniques, counts):
        out.append((str(name), order[start : start + count]))
        start += count
    return out


def _arrow_table(frame) -> pa.Table:
    """GeoParquet Arrow table (WKB + ``geo`` metadata) with stable types."""

    from geopandas.io.arrow import _geopandas_to_arrow

    table = _geopandas_to_arrow(frame, index=False)
    geometry_name = frame.geometry.name
    fields = []
    for field in table.schema:
        if field.name == geometry_name:
            # As fast-hdf writes it: the extension name only; the CRS lives in
            # the GeoParquet ``geo`` metadata.
            field = field.with_type(pa.binary()).with_metadata(
                {"ARROW:extension:name": "geoarrow.wkb"}
            )
        elif pa.types.is_large_string(field.type):
            field = field.with_type(pa.string())
        elif pa.types.is_large_binary(field.type):
            field = field.with_type(pa.binary())
        fields.append(field)
    metadata = {k: v for k, v in (table.schema.metadata or {}).items() if k == b"geo"}
    return table.cast(pa.schema(fields)).replace_schema_metadata(metadata)


# ---------------------------------------------------------------------------
# Engines
# ---------------------------------------------------------------------------


_NATIVE_LAYOUT_OPTIONS = {"dictionary": "utf8"}
_NATIVE_PROBE: dict[str, Optional[str]] = {}


def native_writer_reason() -> Optional[str]:
    """None when fast-hdf can write the archive layout, else why not.

    Besides ``export_geoparquet_ipc_files`` the build must accept the layout
    options (dictionary encoding on Utf8 columns only); a build without them
    would write a different, larger file. Probed once per process with an
    empty input list, which writes nothing.
    """

    if "reason" in _NATIVE_PROBE:
        return _NATIVE_PROBE["reason"]
    try:
        import ras_commander_fast_hdf as native
    except Exception:
        native = None
    function = getattr(native, "export_geoparquet_ipc_files", None)
    if not callable(function):
        reason = "ras_commander_fast_hdf.export_geoparquet_ipc_files is not installed"
    else:
        reason = None
        try:
            function([], os.devnull, json.dumps(_NATIVE_LAYOUT_OPTIONS))
        except Exception as exc:
            if "unknown field" in str(exc):
                reason = (
                    "the installed fast-hdf export_geoparquet_ipc_files lacks the "
                    f"archive layout options {sorted(_NATIVE_LAYOUT_OPTIONS)} ({exc})"
                )
    _NATIVE_PROBE["reason"] = reason
    return reason


def _native_accepts(option: str) -> bool:
    key = f"accepts:{option}"
    if key not in _NATIVE_PROBE:
        import ras_commander_fast_hdf as native

        try:
            native.export_geoparquet_ipc_files([], os.devnull, json.dumps({option: "probe"}))
            accepted = True
        except Exception as exc:
            accepted = "unknown field" not in str(exc)
        _NATIVE_PROBE[key] = "yes" if accepted else ""
    return bool(_NATIVE_PROBE[key])


def native_writer_available() -> bool:
    return native_writer_reason() is None


def _requested_engine(engine: Optional[str]) -> str:
    choice = (engine or os.environ.get(WRITER_ENV, "") or "auto").strip().lower()
    aliases = {"fast-hdf": "native", "rust": "native", "pyarrow": "python"}
    choice = aliases.get(choice, choice)
    if choice not in {"auto", "native", "prefer-native", "python"}:
        raise ValueError(f"{WRITER_ENV} must be one of auto, native, prefer-native, python; got {choice!r}")
    return choice


_CONTRACT_SCALARS = (
    pa.types.is_int64,
    pa.types.is_uint64,
    pa.types.is_float64,
    pa.types.is_float32,
    pa.types.is_boolean,
    pa.types.is_string,
    pa.types.is_binary,
)


def _contract_type(dtype: pa.DataType, *, top_level: bool = True) -> bool:
    """Types fast-hdf's archive profile passes through.

    Scalars above, timestamps (ms/us/ns, any tz), all-null columns at the top
    level, and list / large_list / fixed_size_list / non-empty struct nested
    to any depth over those scalars and timestamps.
    """

    if any(check(dtype) for check in _CONTRACT_SCALARS):
        return True
    if pa.types.is_timestamp(dtype):
        return dtype.unit in ("ms", "us", "ns")
    if pa.types.is_null(dtype):
        return top_level
    if pa.types.is_list(dtype) or pa.types.is_large_list(dtype) or pa.types.is_fixed_size_list(dtype):
        return _contract_type(dtype.value_type, top_level=False)
    if pa.types.is_struct(dtype):
        return dtype.num_fields > 0 and all(
            _contract_type(dtype.field(i).type, top_level=False) for i in range(dtype.num_fields)
        )
    return False



def _crs_evidence(frame) -> dict:
    crs = frame.crs
    if crs is None:
        raise ValueError("no CRS")
    projjson = json.loads(crs.to_json())
    units = crs.axis_info[0].unit_name if crs.axis_info else ""
    if not units:
        raise ValueError("CRS has no axis units")
    return {
        "wkt": crs.to_wkt(),
        "projjson": projjson,
        "units": units,
        "axis_order": "xy",
        "resolved": True,
        "geographic_wgs84": bool(crs.is_geographic and crs.to_epsg() == 4326),
    }


def _unique_identity(table: pa.Table, namespace: str, local: Optional[str]) -> bool:
    names = table.schema.names
    if namespace not in names or (local is not None and local not in names):
        return False
    import pyarrow.compute as pc

    ns = table[namespace]
    if not pa.types.is_string(ns.type) or ns.null_count:
        return False
    if pc.any(pc.equal(ns, "")).as_py():
        return False
    rows = table.num_rows
    distinct_ns = pc.count_distinct(ns).as_py()
    if local is None:
        return distinct_ns == rows
    lo = table[local]
    if not pa.types.is_int64(lo.type) or lo.null_count:
        return False
    if rows and pc.min(lo).as_py() < 0:
        return False
    if distinct_ns == 1:
        return pc.count_distinct(lo).as_py() == rows
    return pa.table({"n": ns, "l": lo}).group_by(["n", "l"]).aggregate([]).num_rows == rows


def _layer_identity(name: Optional[str], table: pa.Table) -> Optional[tuple[str, Optional[str]]]:
    known = KNOWN_IDENTITY.get((name or "").removesuffix("_text"))
    if known and _unique_identity(table, *known):
        return known
    # Result layers (maximum_depth, ...) carry mesh keys; try those first.
    for candidate in (("mesh_name", "cell_id"), ("mesh_name", "face_id")):
        if candidate != known and _unique_identity(table, *candidate):
            return candidate
    strings = [
        f.name
        for f in table.schema
        if pa.types.is_string(f.type) and f.name != "geometry" and not table[f.name].null_count
    ]
    ints = [f.name for f in table.schema if pa.types.is_int64(f.type) and not table[f.name].null_count]
    for namespace in strings:
        if _unique_identity(table, namespace, None):
            return namespace, None
    for namespace in strings:
        for local in ints:
            if _unique_identity(table, namespace, local):
                return namespace, local
    return None


def _layer_identities(table: pa.Table, slices, layer_column) -> list:
    """Row identity of each layer (or None), from the table without its layer column."""

    base = table.drop_columns([layer_column]) if layer_column and layer_column in table.schema.names else table
    if len(slices) == 1:
        return [_layer_identity(slices[0][0], base)]
    return [_layer_identity(name, base.take(pa.array(rows))) for name, rows in slices]


def _required_fields(identities: list) -> set[str]:
    """Fields fast-hdf writes as non-nullable: an identity in every layer.

    The native writer unifies per-layer schemas whose identity fields are
    non-nullable; the Python engine applies the same rule so both write the
    same schema.
    """

    if not identities or any(identity is None for identity in identities):
        return set()
    sets = [{name for name in identity if name} for identity in identities]
    return set.intersection(*sets)


def _native_ineligible_reason(frame, table: pa.Table, identities, slices, layer_column) -> Optional[str]:
    import shapely

    try:
        _crs_evidence(frame)
    except Exception as exc:
        return f"CRS not usable for the export contract ({exc})"
    for field in table.schema:
        if field.name == layer_column:
            continue
        if not _contract_type(field.type):
            return f"column {field.name!r} has type {field.type}, outside the export contract"
        if field.name.lower() in {"fid", "_ras_fid"}:
            return f"column {field.name!r} is a reserved name"
        if pa.types.is_float64(field.type):
            values = table[field.name].to_numpy(zero_copy_only=False)
            if np.isinf(values[~np.isnan(values)]).any():
                return f"column {field.name!r} has infinite values"
    geoms = np.asarray(frame.geometry.values, dtype=object)
    present = ~(shapely.is_missing(geoms))
    if shapely.has_z(geoms[present]).any():
        return "geometry has Z coordinates"
    if 7 in set(shapely.get_type_id(geoms[present]).tolist()):
        return "geometry collections are outside the export contract"
    for (name, _), identity in zip(slices, identities):
        if identity is None:
            label = name or "<single>"
            return f"layer {label!r} has no unique (Utf8, Int64) row identity"
    return None


def _write_python(
    table: pa.Table,
    slices,
    output: Path,
    *,
    row_group_rows: int,
    compression_level: Optional[int],
) -> int:
    """Write ``table`` with a row group ending every ``row_group_rows`` rows
    and at every layer boundary.

    Dictionary encoding is applied to string columns only: unique numeric
    columns (ids, values, bbox, Hilbert keys) fall back from dictionary inside
    every row group, which cost 12-15% of the file on Bald Eagle.
    """
    from ras2cng.cog import atomic_output

    level = ZSTD_LEVEL if compression_level is None else compression_level
    dictionary = [
        f.name
        for f in table.schema
        if pa.types.is_string(f.type) or pa.types.is_large_string(f.type)
    ]
    row_groups = 0
    with atomic_output(output, suffix=".parquet") as staged:
        with pq.ParquetWriter(
            staged,
            table.schema,
            compression="zstd",
            compression_level=level,
            use_dictionary=dictionary or False,
            # Keep Arrow's list item name ("item"), as fast-hdf writes it,
            # instead of renaming it to "element".
            use_compliant_nested_type=False,
        ) as writer:
            offset = 0
            for _, rows in slices:
                count = len(rows)
                for start in range(0, count, row_group_rows):
                    length = min(row_group_rows, count - start)
                    writer.write_table(table.slice(offset + start, length), row_group_size=row_group_rows)
                    row_groups += 1
                offset += count
    return row_groups


def _write_native(
    frame,
    table: pa.Table,
    identities,
    slices,
    output: Path,
    *,
    layer_column: Optional[str],
    covering: str,
    hilbert: bool,
    row_group_rows: int,
    compression_level: Optional[int],
    hilbert_column: Optional[str] = None,
) -> dict:
    import tempfile

    import pyarrow.ipc as ipc
    import ras_commander_fast_hdf as native

    from ras2cng.cog import atomic_output

    evidence = _crs_evidence(frame)
    base = table.drop_columns([layer_column]) if layer_column and layer_column in table.schema.names else table
    identity_options = {}
    with tempfile.TemporaryDirectory(prefix=".ras2cng-gpq-", dir=output.parent) as scratch:
        inputs = []
        for index, ((name, rows), (namespace, local)) in enumerate(zip(slices, identities)):
            layer = base if len(slices) == 1 else base.take(pa.array(rows))
            identity_options[name or ""] = {"namespace": namespace, "local": local}
            fields = []
            for field in layer.schema:
                if field.name == "geometry":
                    field = pa.field(
                        "geometry",
                        pa.binary(),
                        nullable=True,
                        metadata={
                            "ARROW:extension:name": "geoarrow.wkb",
                            "ARROW:extension:metadata": json.dumps(
                                {"crs": evidence["projjson"], "crs_type": "projjson"}
                            ),
                        },
                    )
                else:
                    field = field.remove_metadata().with_nullable(field.name not in (namespace, local))
                fields.append(field)
            schema = pa.schema(fields, metadata={"ras:export:v1": json.dumps(evidence)})
            layer = pa.Table.from_arrays(layer.columns, schema=schema)
            path = Path(scratch) / f"layer{index}.arrow"
            with pa.OSFile(str(path), "wb") as stream, ipc.new_file(stream, schema) as sink:
                for batch in layer.to_batches(max_chunksize=ROW_GROUP_ROWS):
                    sink.write_batch(batch)
            del layer
            inputs.append((name, str(path)))
        options: dict[str, Any] = {
            "compression": "zstd",
            "row_group_rows": int(row_group_rows),
            "bbox_covering": covering,
            "order": "hilbert" if hilbert else "input",
            "identity": identity_options,
        }
        options["compression_level"] = int(ZSTD_LEVEL if compression_level is None else compression_level)
        if layer_column and slices[0][0] is not None:
            options["layer_column"] = layer_column
        # Layout options newer fast-hdf builds accept (requested from
        # fh-fixes); an older build refuses unknown keys, which sends the file
        # to the Python engine rather than writing a different layout.
        options.update(_NATIVE_LAYOUT_OPTIONS)
        if hilbert_column:
            options["hilbert_column"] = hilbert_column
        with atomic_output(output, suffix=".parquet") as staged:
            metrics = json.loads(
                native.export_geoparquet_ipc_files(inputs, str(staged), json.dumps(options))
            )
    return metrics


def write_geoparquet(
    gdf,
    output_path: Path,
    *,
    layer_column: Optional[str] = "layer",
    hilbert: bool = False,
    covering: str = COVERING_STRUCT,
    row_group_rows: int = ROW_GROUP_ROWS,
    compression_level: Optional[int] = None,
    engine: Optional[str] = None,
    hilbert_column: Optional[str] = None,
) -> dict:
    """Write an archive GeoParquet; return how it was written.

    The result dict carries ``engine`` (``fast-hdf`` or ``python``),
    ``fallback_reason`` (why fast-hdf was not used, empty otherwise),
    ``hilbert`` (``fast-hdf``, ``duckdb``, ``numpy`` or empty when not
    ordered), ``bbox_covering``, ``rows`` and ``row_groups``.
    """

    covering_metadata(covering)  # validate
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    requested = _requested_engine(engine)

    frame = drop_covering_columns(gdf)
    if layer_column and layer_column in frame.columns:
        columns = [c for c in frame.columns if c != layer_column] + [layer_column]
        frame = frame[columns]
    else:
        layer_column = None
    slices = _layer_slices(frame, layer_column)
    table = _arrow_table(frame)
    identities = _layer_identities(table, slices, layer_column) if len(frame) else []

    info: dict[str, Any] = {
        "engine": ENGINE_PYTHON,
        "fallback_reason": "",
        "hilbert": "",
        "hilbert_column": "",
        "bbox_covering": covering,
        "rows": int(len(frame)),
        "row_groups": 0,
    }

    if requested in ("native", "prefer-native"):
        reason = native_writer_reason()
        available = reason is None
        if available and len(frame) == 0:
            reason = "empty table"
        elif available:
            reason = _native_ineligible_reason(frame, table, identities, slices, layer_column)
        if reason is None:
            # Builds without the hilbert_column option still write the file;
            # the spatial-index pass then adds the key.
            native_column = (
                hilbert_column if hilbert and hilbert_column and _native_accepts("hilbert_column") else None
            )
            try:
                metrics = _write_native(
                    frame,
                    table,
                    identities,
                    slices,
                    output_path,
                    layer_column=layer_column,
                    covering=covering,
                    hilbert=hilbert,
                    row_group_rows=row_group_rows,
                    compression_level=compression_level,
                    hilbert_column=native_column,
                )
                info.update(
                    engine=ENGINE_NATIVE,
                    hilbert=ENGINE_NATIVE if hilbert else "",
                    row_groups=int(metrics.get("row_groups", 0)),
                    hilbert_column=native_column or "",
                )
                return info
            except Exception as exc:
                if requested == "native":
                    raise
                reason = f"fast-hdf refused the file ({exc})"
        if requested == "native":
            raise RuntimeError(
                f"{WRITER_ENV}=native, but the fast-hdf writer cannot write {output_path.name}: {reason}"
            )
        if available or (
            "lacks the archive layout" in (reason or "") and not _NATIVE_PROBE.get("warned")
        ):
            _NATIVE_PROBE["warned"] = "yes"
            warnings.warn(
                f"{output_path.name}: written by the Python GeoParquet engine; {reason}.",
                RuntimeWarning,
                stacklevel=2,
            )
        info["fallback_reason"] = reason

    bounds = geometry_bounds(frame.geometry) if len(frame) else np.zeros((0, 4))
    order_parts = []
    method = ""
    key_column = np.zeros(len(frame), dtype=np.uint64)
    key_valid = np.zeros(len(frame), dtype=bool)
    for _, rows in slices:
        if hilbert and len(rows):
            keys, valid, method = hilbert_keys(bounds[rows])
            key_column[rows] = keys
            key_valid[rows] = valid
            rows = rows[hilbert_permutation(keys, valid)]
        order_parts.append(rows)
    order = np.concatenate(order_parts) if order_parts else np.arange(0)
    if hilbert:
        info["hilbert"] = method or "numpy"
    if not np.array_equal(order, np.arange(len(frame))):
        table = table.take(pa.array(order))
        bounds = bounds[order]
        key_column, key_valid = key_column[order], key_valid[order]

    required = _required_fields(identities)
    if layer_column:
        required.add(layer_column)
    if required:
        fields = [f.with_nullable(False) if f.name in required else f for f in table.schema]
        table = table.cast(pa.schema(fields, metadata=table.schema.metadata))

    valid = np.isfinite(bounds).all(axis=1)
    file_bbox = None
    if valid.any():
        b = bounds[valid]
        file_bbox = (b[:, 0].min(), b[:, 1].min(), b[:, 2].max(), b[:, 3].max())
    if covering == COVERING_STRUCT:
        struct = bbox_struct_array(bounds)
        table = table.append_column(pa.field(BBOX_COLUMN, struct.type), struct)
    else:
        for i, column in enumerate(LEGACY_BBOX_COLUMNS):
            table = table.append_column(column, pa.array(bounds[:, i], type=pa.float64(), mask=~valid))
    if hilbert and hilbert_column:
        # The spatial-index key, so the post-pass need not rewrite the file.
        table = table.append_column(
            hilbert_column, pa.array(key_column, type=pa.uint64(), mask=~key_valid)
        )
        info["hilbert_column"] = hilbert_column

    metadata = dict(table.schema.metadata or {})
    if b"geo" in metadata:
        geo = json.loads(geo_metadata_with_covering(metadata[b"geo"], covering, file_bbox=file_bbox))
        geo.pop("creator", None)  # identical to the fast-hdf engine's metadata
        # geometry_types stays sorted (GeoPandas); fast-hdf's order varies run to
        # run, so the two engines agree on it as a set.
        metadata[b"geo"] = json.dumps(geo).encode("utf-8")
    table = table.replace_schema_metadata(metadata)
    info["row_groups"] = _write_python(
        table,
        slices,
        output_path,
        row_group_rows=row_group_rows,
        compression_level=compression_level,
    )
    return info
