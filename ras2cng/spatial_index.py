"""Spatial post-processing for ras2cng archives.

The archive writer extracts data first, then this module adds persisted spatial
ordering/index columns. Geometry parquet files get a ``hilbert_index`` column:
DuckDB spatial ``ST_Hilbert`` of each row's bbox centre over its layer's
extent, the key the archive writer (and terrio's native engine) orders rows
by, so for a freshly written archive the re-sort is a no-op. Layers keep their
first-appearance order and a row group ends at every layer boundary.
Geometryless result tables get a ``join_index`` column and, when possible,
inherit ``hilbert_index`` from the matching mesh geometry.

Both bbox covering forms are read: the GeoParquet 1.1 ``bbox`` struct (schema
2.7+) and the flat ``bbox_xmin`` ... columns of older archives. A file is
re-written in the form it already has; a file with no covering gets the struct.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from ras2cng.catalog import Manifest
from ras2cng.geoparquet import (
    LEGACY_BBOX_COLUMNS,
    ROW_GROUP_ROWS,
    _layer_slices,
    _write_python,
    bbox_covering,
    bbox_sql_columns,
    geo_metadata_with_covering,
    hilbert_keys,
    read_bounds,
)


# Flat covering columns of archives written before manifest schema 2.7.
BBOX_COLUMNS = LEGACY_BBOX_COLUMNS
HILBERT_COLUMN = "hilbert_index"
JOIN_INDEX_COLUMN = "join_index"
INDEX_COLUMNS = {HILBERT_COLUMN, JOIN_INDEX_COLUMN}


def _sql_literal(value: str | Path) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _quote_identifier(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _parquet_columns(path: Path) -> list[str]:
    return list(pq.read_schema(path).names)


def _select_columns(columns: list[str], *, alias: str = "", exclude: set[str] | None = None) -> str:
    excluded = exclude or set()
    prefix = f"{_quote_identifier(alias)}." if alias else ""
    selected = [prefix + _quote_identifier(col) for col in columns if col not in excluded]
    if not selected:
        raise ValueError("No columns available for parquet SELECT")
    return ", ".join(selected)


def _duckdb_copy(query: str, output_path: Path) -> Path:
    try:
        import duckdb
    except ImportError as exc:  # pragma: no cover - depends on optional extra
        raise RuntimeError("DuckDB is required for archive spatial post-processing") from exc

    tmp_path = output_path.with_name(output_path.name + ".tmp")
    if tmp_path.exists():
        tmp_path.unlink()

    con = duckdb.connect()
    try:
        con.execute("PRAGMA preserve_insertion_order=false")
        try:
            con.execute(f"PRAGMA temp_directory={_sql_literal(output_path.parent)}")
        except Exception:
            pass
        con.execute(
            f"COPY ({query}) TO {_sql_literal(tmp_path)} "
            "(FORMAT PARQUET, COMPRESSION ZSTD)"
        )
    finally:
        con.close()

    return tmp_path


def _hilbert_index_column(bounds: np.ndarray, slices) -> tuple[pa.Array, str]:
    """Per-layer ``ST_Hilbert`` keys as uint64; null where there is no bbox."""

    keys = np.zeros(len(bounds), dtype=np.uint64)
    valid = np.zeros(len(bounds), dtype=bool)
    method = ""
    for _, rows in slices:
        if not len(rows):
            continue
        layer_keys, layer_valid, method = hilbert_keys(bounds[rows])
        keys[rows] = layer_keys
        valid[rows] = layer_valid
    return pa.array(keys, type=pa.uint64(), mask=~valid), method


def _sorted_by_layer_and_key(table: pa.Table, slices, keys: pa.Array) -> tuple[pa.Table, list]:
    """Stable order: layer (first appearance), key (nulls last), input row."""

    key_values = keys.to_numpy(zero_copy_only=False)
    valid = keys.is_valid().to_numpy(zero_copy_only=False)
    sort_keys = np.where(valid, np.nan_to_num(key_values).astype(np.uint64), np.iinfo(np.uint64).max)
    parts, out_slices, offset = [], [], 0
    for name, rows in slices:
        ordered = rows[np.argsort(sort_keys[rows], kind="stable")]
        parts.append(ordered)
        out_slices.append((name, np.arange(offset, offset + len(rows))))
        offset += len(rows)
    order = np.concatenate(parts) if parts else np.arange(0)
    table = table.append_column(HILBERT_COLUMN, keys)
    if not np.array_equal(order, np.arange(len(order))):
        table = table.take(pa.array(order))
    return table, out_slices


def _write_geoparquet_fallback(path: Path, *, hilbert_level: int) -> dict[str, Any]:
    """Index a GeoParquet that has no bbox covering: add the ``bbox`` struct."""

    from ras2cng.geoparquet import write_geoparquet

    import geopandas as gpd

    gdf = gpd.read_parquet(path)
    gdf = gdf.drop(columns=[c for c in INDEX_COLUMNS if c in gdf.columns])
    layer_column = "layer" if "layer" in gdf.columns else None
    write_geoparquet(gdf, path, layer_column=layer_column, hilbert=True, engine="python")
    result = postprocess_geoparquet(path, hilbert_level=hilbert_level)
    result["method"] = "geopandas+" + result.get("method", "")
    return result


def postprocess_geoparquet(
    path: Path, *, hilbert_level: int = 16, written_indexed: bool = False
) -> dict[str, Any]:
    """Add ``hilbert_index`` to a GeoParquet and order rows by it within layers.

    ``hilbert_level`` is kept for API compatibility; the key is always
    ``ST_Hilbert``'s 16 bits per axis. ``written_indexed`` means the archive
    writer already ordered the rows and stored the key (its manifest record
    says ``hilbert_column``); the file is then left as written.
    """
    path = Path(path)
    schema = pq.read_schema(path)
    form = bbox_covering(schema)
    if form is None:
        return _write_geoparquet_fallback(path, hilbert_level=hilbert_level)
    if written_indexed and HILBERT_COLUMN in schema.names:
        metadata = pq.ParquetFile(path).metadata
        return {
            "path": path.name,
            "rows": int(metadata.num_rows),
            "row_groups": int(metadata.num_row_groups),
            "hilbert_index": HILBERT_COLUMN,
            "bbox_covering": form,
            "method": "written_by_archive_writer",
        }

    source_meta = schema.metadata or {}
    table = pq.read_table(path)
    keep_columns = [col for col in table.column_names if col not in INDEX_COLUMNS]
    table = table.select(keep_columns)

    if "layer" in table.column_names:
        import pandas as pd

        slices = _layer_slices(pd.DataFrame({"layer": table["layer"].to_pandas()}), "layer")
    else:
        slices = [(None, np.arange(table.num_rows))]
    bounds = read_bounds(table)
    keys, method = _hilbert_index_column(bounds, slices)
    table, slices = _sorted_by_layer_and_key(table, slices, keys)

    metadata = dict(table.schema.metadata or {})
    if b"geo" in source_meta:
        metadata[b"geo"] = geo_metadata_with_covering(source_meta[b"geo"], form)
    table = table.replace_schema_metadata(metadata)
    row_groups = _write_python(table, slices, path, row_group_rows=ROW_GROUP_ROWS, compression_level=None)

    return {
        "path": path.name,
        "rows": int(table.num_rows),
        "row_groups": int(row_groups),
        "hilbert_index": HILBERT_COLUMN,
        "bbox_covering": form,
        "method": f"st_hilbert_{method or 'none'}",
    }


def _can_spatial_join(geometry_path: Path, *, geometry_filter: str, key_column: str) -> bool:
    if not geometry_path or not geometry_path.exists():
        return False
    columns = set(_parquet_columns(geometry_path))
    required = {"mesh_name", "layer", key_column, HILBERT_COLUMN}
    return bool(geometry_filter) and required.issubset(columns)


def postprocess_result_table(
    path: Path,
    *,
    key_column: str = "",
    join_columns: Mapping[str, str] | None = None,
    geometry_path: Path | None = None,
    geometry_filter: str = "",
) -> dict[str, Any]:
    """Sort/index one geometryless result parquet table."""
    path = Path(path)
    columns = _parquet_columns(path)
    join_columns = dict(join_columns or {})
    if not key_column and join_columns:
        result_keys = list(join_columns.values())
        missing_result = [column for column in result_keys if column not in columns]
        geometry_columns = set(_parquet_columns(geometry_path)) if geometry_path and geometry_path.exists() else set()
        missing_geometry = [column for column in join_columns if column not in geometry_columns]
        if missing_result:
            return {
                "path": path.name,
                "status": "skipped",
                "reason": "missing result composite key: " + ", ".join(missing_result),
            }

        def normalized(alias: str, column: str) -> str:
            return (
                f"trim(CAST({_quote_identifier(alias)}.{_quote_identifier(column)} AS VARCHAR))"
            )

        result_order = ", ".join(normalized("r", column) for column in result_keys)
        result_columns = _select_columns(columns, alias="r", exclude=INDEX_COLUMNS)
        join_index_expr = (
            f"row_number() OVER (ORDER BY {result_order}) - 1 "
            f"AS {_quote_identifier(JOIN_INDEX_COLUMN)}"
        )
        can_spatial_join = (
            bool(geometry_filter)
            and geometry_path is not None
            and geometry_path.exists()
            and not missing_geometry
            and {"layer", HILBERT_COLUMN}.issubset(geometry_columns)
        )
        if can_spatial_join:
            geometry_key_select = ", ".join(
                f"{normalized('source', geometry_column)} AS _join_key_{index}"
                for index, geometry_column in enumerate(join_columns)
            )
            geometry_key_names = ", ".join(
                f"_join_key_{index}" for index in range(len(join_columns))
            )
            join_predicate = " AND ".join(
                f"{normalized('r', result_column)} = {_quote_identifier('g')}._join_key_{index}"
                for index, result_column in enumerate(result_keys)
            )
            query = (
                f"SELECT {result_columns}, {join_index_expr}, "
                f"{_quote_identifier('g')}.{_quote_identifier(HILBERT_COLUMN)} "
                f"AS {_quote_identifier(HILBERT_COLUMN)} "
                f"FROM read_parquet({_sql_literal(path)}) AS {_quote_identifier('r')} "
                "LEFT JOIN ("
                f"SELECT {geometry_key_select}, "
                f"min({_quote_identifier(HILBERT_COLUMN)}) AS {_quote_identifier(HILBERT_COLUMN)} "
                f"FROM read_parquet({_sql_literal(geometry_path)}) AS {_quote_identifier('source')} "
                f"WHERE {_quote_identifier('layer')} = {_sql_literal(geometry_filter)} "
                f"GROUP BY {geometry_key_names}"
                f") AS {_quote_identifier('g')} ON {join_predicate} "
                f"ORDER BY {_quote_identifier('g')}.{_quote_identifier(HILBERT_COLUMN)} NULLS LAST, "
                f"{result_order}"
            )
            status = "spatial_join"
            sort_order = HILBERT_COLUMN
        else:
            query = (
                f"SELECT {result_columns}, {join_index_expr} "
                f"FROM read_parquet({_sql_literal(path)}) AS {_quote_identifier('r')} "
                f"ORDER BY {result_order}"
            )
            status = "join_key"
            sort_order = ",".join(result_keys)

        tmp_path = _duckdb_copy(query, path)
        tmp_path.replace(path)
        metadata = pq.ParquetFile(path).metadata
        return {
            "path": path.name,
            "rows": int(metadata.num_rows),
            "row_groups": int(metadata.num_row_groups),
            "status": status,
            "sort_order": sort_order,
            "hilbert_index": HILBERT_COLUMN if status == "spatial_join" else "",
            "join_index": JOIN_INDEX_COLUMN,
        }

    if "mesh_name" not in columns or key_column not in columns:
        return {
            "path": path.name,
            "status": "skipped",
            "reason": f"missing mesh_name/{key_column}",
        }

    result_columns = _select_columns(columns, alias="r", exclude=INDEX_COLUMNS)
    key_expr = f"TRY_CAST({_quote_identifier('r')}.{_quote_identifier(key_column)} AS BIGINT)"
    join_index_expr = (
        "row_number() OVER (ORDER BY "
        f"{_quote_identifier('r')}.{_quote_identifier('mesh_name')}, {key_expr}"
        f") - 1 AS {_quote_identifier(JOIN_INDEX_COLUMN)}"
    )

    if geometry_path and _can_spatial_join(geometry_path, geometry_filter=geometry_filter, key_column=key_column):
        geom_key_expr = f"TRY_CAST({_quote_identifier(key_column)} AS BIGINT)"
        query = (
            f"SELECT {result_columns}, {join_index_expr}, "
            f"{_quote_identifier('g')}.{_quote_identifier(HILBERT_COLUMN)} AS {_quote_identifier(HILBERT_COLUMN)} "
            f"FROM read_parquet({_sql_literal(path)}) AS {_quote_identifier('r')} "
            "LEFT JOIN ("
            f"SELECT {_quote_identifier('mesh_name')}, {geom_key_expr} AS _join_key, "
            f"min({_quote_identifier(HILBERT_COLUMN)}) AS {_quote_identifier(HILBERT_COLUMN)} "
            f"FROM read_parquet({_sql_literal(geometry_path)}) "
            f"WHERE {_quote_identifier('layer')} = {_sql_literal(geometry_filter)} "
            f"AND {_quote_identifier(key_column)} IS NOT NULL "
            f"AND {_quote_identifier(HILBERT_COLUMN)} IS NOT NULL "
            f"GROUP BY {_quote_identifier('mesh_name')}, _join_key"
            f") AS {_quote_identifier('g')} "
            f"ON {_quote_identifier('r')}.{_quote_identifier('mesh_name')} = {_quote_identifier('g')}.{_quote_identifier('mesh_name')} "
            f"AND {key_expr} = {_quote_identifier('g')}._join_key "
            f"ORDER BY {_quote_identifier('g')}.{_quote_identifier(HILBERT_COLUMN)} NULLS LAST, "
            f"{_quote_identifier('r')}.{_quote_identifier('mesh_name')}, {key_expr}"
        )
        status = "spatial_join"
        sort_order = HILBERT_COLUMN
    else:
        query = (
            f"SELECT {result_columns}, {join_index_expr} "
            f"FROM read_parquet({_sql_literal(path)}) AS {_quote_identifier('r')} "
            f"ORDER BY {_quote_identifier('r')}.{_quote_identifier('mesh_name')}, {key_expr}"
        )
        status = "join_key"
        sort_order = f"mesh_name,{key_column}"

    tmp_path = _duckdb_copy(query, path)
    tmp_path.replace(path)
    metadata = pq.ParquetFile(path).metadata
    return {
        "path": path.name,
        "rows": int(metadata.num_rows),
        "row_groups": int(metadata.num_row_groups),
        "status": status,
        "sort_order": sort_order,
        "hilbert_index": HILBERT_COLUMN if status == "spatial_join" else "",
        "join_index": JOIN_INDEX_COLUMN,
    }


def _archive_path(archive_dir: Path, rel_path: str | Path | None) -> Path | None:
    if not rel_path:
        return None
    return archive_dir / Path(rel_path)


def _written_indexed(entry: Mapping[str, Any]) -> bool:
    record = entry.get("geoparquet") or {}
    return record.get("hilbert_column") == HILBERT_COLUMN


def postprocess_archive(
    archive_dir: Path,
    *,
    manifest: Manifest | None = None,
    write_manifest: bool = True,
    hilbert_level: int = 16,
    skip_errors: bool = True,
) -> dict[str, Any]:
    """Hilbert-sort and index parquet files in a ras2cng archive."""
    archive_dir = Path(archive_dir)
    manifest_path = archive_dir / "manifest.json"
    if manifest is None:
        manifest = Manifest.load(manifest_path)

    summary: dict[str, Any] = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "hilbert_column": HILBERT_COLUMN,
        "join_index_column": JOIN_INDEX_COLUMN,
        "hilbert_level": hilbert_level,
        "geometry_files": [],
        "result_files": [],
        "errors": [],
    }

    geom_by_id = {entry.get("geom_id"): entry for entry in manifest.geometry}

    for entry in manifest.geometry:
        parquet_path = _archive_path(archive_dir, entry.get("parquet"))
        if parquet_path is None or not parquet_path.exists():
            continue
        try:
            result = postprocess_geoparquet(
                parquet_path,
                hilbert_level=hilbert_level,
                written_indexed=_written_indexed(entry),
            )
            entry["size_bytes"] = parquet_path.stat().st_size
            for layer in entry.get("layers", []):
                layer["hilbert_index"] = HILBERT_COLUMN
                layer["sort_order"] = "layer,hilbert_index"
                # SQL expressions for xmin, ymin, xmax, ymax in either form.
                layer["bbox_columns"] = bbox_sql_columns(result.get("bbox_covering"))
                layer["bbox_covering"] = result.get("bbox_covering", "")
            summary["geometry_files"].append(result)
        except Exception as exc:
            error = {"path": str(parquet_path), "error": str(exc)}
            summary["errors"].append(error)
            if not skip_errors:
                raise

    for plan in manifest.results:
        plan_parquet = _archive_path(archive_dir, plan.get("parquet"))
        geometry_mode = plan.get("geometry_mode", "polygon")
        if plan_parquet is not None and plan_parquet.exists() and geometry_mode != "none":
            try:
                result = postprocess_geoparquet(
                    plan_parquet,
                    hilbert_level=hilbert_level,
                    written_indexed=_written_indexed(plan),
                )
                plan["size_bytes"] = plan_parquet.stat().st_size
                for variable in plan.get("variables", []):
                    variable["hilbert_index"] = HILBERT_COLUMN
                    variable["sort_order"] = "layer,hilbert_index"
                    variable["index_status"] = "spatial"
                summary["result_files"].append(result)
            except Exception as exc:
                error = {"path": str(plan_parquet), "error": str(exc)}
                summary["errors"].append(error)
                if not skip_errors:
                    raise

        geom_entry = geom_by_id.get(plan.get("geom_id"))
        geom_path = _archive_path(archive_dir, geom_entry.get("parquet")) if geom_entry else None
        for variable in plan.get("variables", []):
            variable_path = _archive_path(archive_dir, variable.get("parquet"))
            if variable_path is None or not variable_path.exists():
                continue
            key_column = variable.get("index_column") or ""
            join_columns = variable.get("join_columns") or {}
            if not key_column and not join_columns:
                variable["index_status"] = "skipped"
                continue
            try:
                result = postprocess_result_table(
                    variable_path,
                    key_column=key_column,
                    join_columns=join_columns,
                    geometry_path=geom_path,
                    geometry_filter=variable.get("geometry_filter") or "",
                )
                variable["hilbert_index"] = result.get("hilbert_index", "")
                variable["join_index"] = result.get("join_index", "")
                variable["sort_order"] = result.get("sort_order", "")
                variable["index_status"] = result.get("status", "")
                variable["size_bytes"] = variable_path.stat().st_size
                summary["result_files"].append(result)
            except Exception as exc:
                variable["index_status"] = "error"
                error = {"path": str(variable_path), "error": str(exc)}
                summary["errors"].append(error)
                if not skip_errors:
                    raise

    summary["geometry_file_count"] = len(summary["geometry_files"])
    summary["result_file_count"] = len(summary["result_files"])
    summary["error_count"] = len(summary["errors"])
    manifest.postprocessing["spatial_index"] = summary

    if write_manifest:
        manifest.write(manifest_path)
    return summary
