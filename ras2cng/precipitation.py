"""Gridded precipitation raster export from HEC-RAS HDF files."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal, Sequence

import h5py
import numpy as np


PRECIPITATION_GROUP = "Event Conditions/Meteorology/Precipitation"
PROCESSED_VALUES = f"{PRECIPITATION_GROUP}/Values"
IMPORTED_VALUES = f"{PRECIPITATION_GROUP}/Imported Raster Data/Values"
TIMESTAMP_DATASET = f"{PRECIPITATION_GROUP}/Timestamp"

PrecipitationSource = Literal["auto", "processed", "imported"]


@dataclass(frozen=True)
class PrecipitationGridInfo:
    """Metadata for a gridded precipitation dataset inside a HEC-RAS HDF file."""

    hdf_path: Path
    source: str
    values_path: str
    timestamps: list[str]
    rows: int
    cols: int
    cellsize: float
    left: float
    top: float
    projection: str | None = None
    units: str | None = None
    data_type: str | None = None
    nodata: float | None = None
    source_is_cumulative: bool = False


@dataclass
class PrecipitationExportResult:
    """Files written by :func:`export_precipitation_rasters`."""

    source_hdf: Path
    source: str
    values_path: str
    output_dir: Path
    units: str | None
    rows: int
    cols: int
    timestamps: list[str] = field(default_factory=list)
    incremental: list[Path] = field(default_factory=list)
    cumulative: list[Path] = field(default_factory=list)


def list_precipitation_timestamps(
    hdf_path: Path,
    *,
    source: PrecipitationSource = "auto",
) -> list[str]:
    """List gridded precipitation timestamps in a HEC-RAS HDF file.

    Args:
        hdf_path: Plan HDF (``*.p??.hdf``) or unsteady-flow HDF
            (``*.u??.hdf``) containing gridded precipitation.
        source: Which precipitation dataset to inspect. ``"processed"`` uses
            ``Precipitation/Values`` from completed plan HDF files. ``"imported"``
            uses ``Imported Raster Data/Values``. ``"auto"`` prefers processed
            values and falls back to imported values.

    Returns:
        Timestamp labels as stored in the HDF.
    """

    return read_precipitation_grid_info(hdf_path, source=source).timestamps


def read_precipitation_grid_info(
    hdf_path: Path,
    *,
    source: PrecipitationSource = "auto",
) -> PrecipitationGridInfo:
    """Read gridded precipitation raster metadata from a HEC-RAS HDF file.

    The HDF must include raster georeferencing attributes: ``Raster Rows``,
    ``Raster Cols``, ``Raster Cellsize``, ``Raster Left``, and ``Raster Top``.
    Projection WKT and units are preserved when present.
    """

    hdf_path = Path(hdf_path)
    with h5py.File(hdf_path, "r") as hdf:
        if PRECIPITATION_GROUP not in hdf:
            raise ValueError(f"No gridded precipitation group found: {PRECIPITATION_GROUP}")

        values_path, resolved_source = _resolve_values_path(hdf, source)
        group = hdf[PRECIPITATION_GROUP]
        dataset = hdf[values_path]
        attrs = _merged_attrs(group.attrs, dataset.attrs)

        rows = _required_int_attr(attrs, "Raster Rows")
        cols = _required_int_attr(attrs, "Raster Cols")
        expected = rows * cols
        if len(dataset.shape) < 2 or int(dataset.shape[-1]) != expected:
            raise ValueError(
                f"Precipitation values at {values_path!r} have shape {dataset.shape}; "
                f"expected the final dimension to equal Raster Rows x Raster Cols ({expected})."
            )

        timestamps = _read_timestamps(hdf, attrs, int(dataset.shape[0]))

        return PrecipitationGridInfo(
            hdf_path=hdf_path,
            source=resolved_source,
            values_path=values_path,
            timestamps=timestamps,
            rows=rows,
            cols=cols,
            cellsize=_required_float_attr(attrs, "Raster Cellsize"),
            left=_required_float_attr(attrs, "Raster Left"),
            top=_required_float_attr(attrs, "Raster Top"),
            projection=_optional_str_attr(attrs, "Projection"),
            units=_optional_str_attr(attrs, "Units"),
            data_type=_optional_str_attr(attrs, "Data Type"),
            nodata=_optional_float_attr(attrs, "NoData"),
            source_is_cumulative=_is_cumulative_source(attrs),
        )


def export_precipitation_rasters(
    hdf_path: Path,
    output_dir: Path,
    *,
    source: PrecipitationSource = "auto",
    timestamps: Sequence[str | int] | None = None,
    export_incremental: bool = True,
    export_cumulative: bool = True,
    prefix: str | None = None,
    overwrite: bool = True,
    compress: str = "deflate",
) -> PrecipitationExportResult:
    """Export gridded precipitation GeoTIFF rasters from a HEC-RAS HDF file.

    Processed plan-HDF precipitation values are stored as timestep amounts in
    the HDF precipitation units. Imported raster data may be cumulative source
    data; in that case incremental rasters are derived by differencing adjacent
    cumulative grids.

    Args:
        hdf_path: Plan HDF (``*.p??.hdf``) or unsteady-flow HDF.
        output_dir: Directory where GeoTIFFs will be written.
        source: ``"auto"`` prefers processed plan results and falls back to
            imported raster data. Use ``"processed"`` or ``"imported"`` to
            require a specific dataset.
        timestamps: Optional timestamp labels or zero-based indices to export.
            ``None`` exports every timestep.
        export_incremental: Write per-timestep precipitation amount rasters.
        export_cumulative: Write cumulative-through-timestep rasters.
        prefix: Optional filename prefix. Defaults to the HDF stem.
        overwrite: If False, raise when an output file already exists.
        compress: GeoTIFF compression setting passed to rasterio.

    Returns:
        A :class:`PrecipitationExportResult` describing the written files.
    """

    if not export_incremental and not export_cumulative:
        raise ValueError("At least one of export_incremental or export_cumulative must be True")

    try:
        import rasterio
        from rasterio.crs import CRS
        from rasterio.transform import from_origin
    except ImportError as exc:  # pragma: no cover - exercised only without rasterio installed
        raise ImportError(
            "export_precipitation_rasters() requires rasterio. "
            'Install ras2cng with the "all" or "pmtiles" extra, or install rasterio separately.'
        ) from exc

    hdf_path = Path(hdf_path)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    info = read_precipitation_grid_info(hdf_path, source=source)
    selected_indices = _select_indices(info.timestamps, timestamps)
    file_prefix = _safe_filename_part(prefix or hdf_path.stem)

    with h5py.File(hdf_path, "r") as hdf:
        raw = np.asarray(hdf[info.values_path][:], dtype="float32")

    grids = _reshape_values(raw, info)
    incremental, cumulative = _incremental_and_cumulative(grids, info)

    transform = from_origin(info.left, info.top, info.cellsize, info.cellsize)
    crs = _rasterio_crs(CRS, info.projection)

    profile = {
        "driver": "GTiff",
        "height": info.rows,
        "width": info.cols,
        "count": 1,
        "dtype": "float32",
        "transform": transform,
        "compress": compress,
    }
    if crs is not None:
        profile["crs"] = crs
    if info.nodata is not None:
        profile["nodata"] = info.nodata

    result = PrecipitationExportResult(
        source_hdf=hdf_path,
        source=info.source,
        values_path=info.values_path,
        output_dir=output_dir,
        units=info.units,
        rows=info.rows,
        cols=info.cols,
    )

    for index in selected_indices:
        timestamp = info.timestamps[index]
        stamp = _safe_timestamp_part(timestamp)
        result.timestamps.append(timestamp)

        if export_incremental:
            out = output_dir / f"{file_prefix}_precip_{index:04d}_{stamp}.tif"
            _write_geotiff(
                rasterio,
                out,
                incremental[index],
                profile,
                info,
                timestamp,
                "incremental",
                overwrite=overwrite,
            )
            result.incremental.append(out)

        if export_cumulative:
            out = output_dir / f"{file_prefix}_precip_cumulative_{index:04d}_{stamp}.tif"
            _write_geotiff(
                rasterio,
                out,
                cumulative[index],
                profile,
                info,
                timestamp,
                "cumulative",
                overwrite=overwrite,
            )
            result.cumulative.append(out)

    return result


def _resolve_values_path(hdf: h5py.File, source: PrecipitationSource) -> tuple[str, str]:
    if source not in {"auto", "processed", "imported"}:
        raise ValueError(f"Invalid precipitation source: {source}")

    processed_exists = PROCESSED_VALUES in hdf
    imported_exists = IMPORTED_VALUES in hdf

    if source == "processed":
        if not processed_exists:
            raise ValueError(f"Processed precipitation values not found: {PROCESSED_VALUES}")
        return PROCESSED_VALUES, "processed"

    if source == "imported":
        if not imported_exists:
            raise ValueError(f"Imported precipitation raster values not found: {IMPORTED_VALUES}")
        return IMPORTED_VALUES, "imported"

    if processed_exists:
        return PROCESSED_VALUES, "processed"
    if imported_exists:
        return IMPORTED_VALUES, "imported"

    raise ValueError(
        f"No gridded precipitation values found at {PROCESSED_VALUES!r} or {IMPORTED_VALUES!r}"
    )


def _merged_attrs(*attr_sets) -> dict[str, object]:
    attrs: dict[str, object] = {}
    for attr_set in attr_sets:
        for key, value in attr_set.items():
            attrs[str(key)] = value
    return attrs


def _decode(value: object) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace").strip()
    if isinstance(value, np.bytes_):
        return value.decode("utf-8", errors="replace").strip()
    if isinstance(value, np.ndarray):
        return " ".join(_decode(v) for v in value.tolist()).strip()
    return str(value).strip()


def _required_int_attr(attrs: dict[str, object], name: str) -> int:
    value = _optional_str_attr(attrs, name)
    if value is None:
        raise ValueError(f"Missing precipitation raster attribute: {name}")
    return int(float(value))


def _required_float_attr(attrs: dict[str, object], name: str) -> float:
    value = _optional_str_attr(attrs, name)
    if value is None:
        raise ValueError(f"Missing precipitation raster attribute: {name}")
    return float(value)


def _optional_float_attr(attrs: dict[str, object], name: str) -> float | None:
    value = _optional_str_attr(attrs, name)
    if value in (None, ""):
        return None
    return float(value)


def _optional_str_attr(attrs: dict[str, object], name: str) -> str | None:
    if name not in attrs:
        return None
    value = _decode(attrs[name])
    return value if value else None


def _is_cumulative_source(attrs: dict[str, object]) -> bool:
    data_type = (_optional_str_attr(attrs, "Data Type") or "").strip().lower()
    if not data_type or data_type == "per-cum":
        return False
    return data_type.startswith("cumul")


def _read_timestamps(hdf: h5py.File, attrs: dict[str, object], count: int) -> list[str]:
    if TIMESTAMP_DATASET in hdf:
        timestamps = [_decode(v) for v in hdf[TIMESTAMP_DATASET][:]]
    else:
        timestamps = _parse_times_attr(attrs.get("Times"))

    if not timestamps:
        timestamps = [str(i) for i in range(count)]

    if len(timestamps) != count:
        raise ValueError(
            f"Precipitation timestamp count ({len(timestamps)}) does not match "
            f"value timestep count ({count})."
        )

    return timestamps


def _parse_times_attr(value: object | None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, np.ndarray)):
        return [_decode(v) for v in value]

    text = _decode(value)
    quoted = re.findall(r"b?'([^']+)'", text)
    if quoted:
        return quoted

    parts = [part.strip() for part in re.split(r"[,;\n]+", text) if part.strip()]
    return parts


def _select_indices(
    timestamps: Sequence[str],
    requested: Sequence[str | int] | None,
) -> list[int]:
    if requested is None:
        return list(range(len(timestamps)))

    by_label = {label: i for i, label in enumerate(timestamps)}
    by_safe = {_safe_timestamp_part(label): i for i, label in enumerate(timestamps)}

    indices: list[int] = []
    for item in requested:
        if isinstance(item, int) or (isinstance(item, str) and item.strip().isdigit()):
            idx = int(item)
        else:
            token = str(item).strip()
            if token in by_label:
                idx = by_label[token]
            elif token in by_safe:
                idx = by_safe[token]
            else:
                raise ValueError(f"Timestamp not found in precipitation HDF: {token}")

        if idx < 0 or idx >= len(timestamps):
            raise IndexError(f"Precipitation timestep index out of range: {idx}")
        if idx not in indices:
            indices.append(idx)

    return indices


def _reshape_values(raw: np.ndarray, info: PrecipitationGridInfo) -> np.ndarray:
    if raw.ndim == 2:
        return raw.reshape((raw.shape[0], info.rows, info.cols))
    if raw.ndim == 3 and raw.shape[1:] == (info.rows, info.cols):
        return raw
    raise ValueError(f"Unsupported precipitation values shape: {raw.shape}")


def _incremental_and_cumulative(
    grids: np.ndarray,
    info: PrecipitationGridInfo,
) -> tuple[np.ndarray, np.ndarray]:
    working = grids.astype("float64", copy=False)
    if info.nodata is not None:
        working = np.where(np.isclose(working, info.nodata), np.nan, working)

    if info.source_is_cumulative:
        cumulative = working
        zeros = np.zeros_like(cumulative[:1])
        previous = np.concatenate([zeros, cumulative[:-1]], axis=0)
        incremental = cumulative - previous
    else:
        incremental = working
        valid = ~np.isnan(incremental)
        cumulative = np.cumsum(np.where(valid, incremental, 0.0), axis=0)
        static_nodata = ~np.any(valid, axis=0)
        if np.any(static_nodata):
            cumulative[:, static_nodata] = np.nan

    return incremental.astype("float32"), cumulative.astype("float32")


def _rasterio_crs(crs_cls, projection: str | None):
    if not projection:
        return None
    try:
        return crs_cls.from_wkt(projection)
    except Exception:
        try:
            return crs_cls.from_string(projection)
        except Exception:
            return None


def _write_geotiff(
    rasterio,
    path: Path,
    array: np.ndarray,
    profile: dict,
    info: PrecipitationGridInfo,
    timestamp: str,
    raster_kind: str,
    *,
    overwrite: bool,
) -> None:
    if path.exists() and not overwrite:
        raise FileExistsError(path)

    output = np.asarray(array, dtype="float32")
    if info.nodata is not None:
        output = np.where(np.isnan(output), info.nodata, output).astype("float32")

    tags = {
        "ras2cng_variable": "precipitation",
        "ras2cng_precipitation_kind": raster_kind,
        "ras2cng_precipitation_source": info.source,
        "source_hdf": str(info.hdf_path),
        "source_dataset": info.values_path,
        "timestamp": timestamp,
    }
    if info.units:
        tags["units"] = info.units
    if info.data_type:
        tags["ras_data_type"] = info.data_type

    with rasterio.open(path, "w", **profile) as dst:
        dst.write(output, 1)
        dst.update_tags(**tags)
        label = "Cumulative precipitation" if raster_kind == "cumulative" else "Precipitation"
        if info.units:
            label = f"{label} ({info.units})"
        dst.set_band_description(1, label)


def _safe_filename_part(text: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", text).strip("._")
    return cleaned.replace(".", "_") or "precipitation"


def _safe_timestamp_part(timestamp: str) -> str:
    text = timestamp.strip()
    for fmt in (
        "%d%b%Y %H:%M:%S.%f",
        "%d%b%Y %H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
    ):
        try:
            return datetime.strptime(text, fmt).strftime("%Y%m%d_%H%M%S")
        except ValueError:
            continue

    cleaned = re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_")
    return cleaned[:80] or "timestamp"
