"""Vector tiler backend selection shared by every PMTiles writer in ras2cng.

Two backends produce the same delivery contract -- every feature at every zoom
of the tileset, coincident shared edges between mesh cells, one named source
layer per input:

``freestiler``
    In-process Rust tiler from the optional ``freestiler`` extra
    (``pip install ras2cng[freestiler]``). It snaps vertices to the tile pixel
    grid, so shared mesh vertices stay shared, and keeps every feature. A build
    with Arrow input and the ``buffer`` option (gpt-cmdr/freestiler #5) is the
    preferred backend: it needs no GeoJSON, mbtiles or ``pmtiles convert`` step.
    Viewer on Bald Eagle p07 with vector results: 36 s against 1,745 s for
    tippecanoe. Archives with the default feature IDs are 5% larger than
    tippecanoe's and without them 10% smaller; sparse low-zoom layers stay
    larger (``feature_dev_notes/rust_engines/T12.md``). Older builds are 9-12%
    larger on mesh layers.

``tippecanoe``
    External ``tippecanoe`` + ``pmtiles`` CLIs. Not available natively on
    Windows. Select it explicitly with ``RAS2CNG_VECTOR_TILER=tippecanoe``.

``RAS2CNG_VECTOR_TILER`` selects ``tippecanoe``, ``freestiler`` or ``auto``
(the default): freestiler when the installed build has Arrow input and
``buffer``; otherwise tippecanoe when its CLIs are on PATH; otherwise any
installed freestiler.

freestiler input (``RAS2CNG_FREESTILER_INPUT``):

``auto`` (default) or ``arrow``
    When freestiler has Arrow input, every layer is handed over as an Arrow
    table (:class:`ArrowLayer`) and no newline-delimited GeoJSON is written.
    Dense mesh layers come straight from the archive GeoParquet (column
    projection and ``layer`` filter) in the model CRS and are reprojected to
    EPSG:4326 in Rust. Other layers come from their already reprojected
    GeoDataFrames. When freestiler refuses a CRS (NAD83(HARN), NAD27, Hotine,
    ...), layers in that CRS are reprojected in Python with pyproj and passed
    as EPSG:4326 Arrow tables instead. Property columns are shaped as the
    GeoJSON path delivered them (:func:`delivery_table`). ``arrow`` fails when
    the installed freestiler has no Arrow input.
``geojson``
    The previous path: NDGeoJSON sources read back as GeoDataFrames.

freestiler options (ignored by tippecanoe, whose tiles never carry IDs):

``RAS2CNG_VECTOR_TILE_IDS``
    ``1`` (default) keeps an MVT feature ID on every feature; ``0`` omits them
    (``generate_ids=False``). On the 87k-cell Bald Eagle mesh, z10-15, omitting
    IDs saves 1.7 MB (-14%). Nothing in the ras2cng viewer reads feature IDs
    (tippecanoe output never had them), but a consumer using MapLibre
    ``feature-state`` without ``promoteId`` needs them. The CLI flag
    ``--feature-ids/--no-feature-ids`` and :func:`vector_tile_options` override it.

``RAS2CNG_VECTOR_TILE_GZIP_LEVEL``
    Tile gzip level 0-9 when the installed freestiler accepts ``gzip_level``;
    default 6 (-14% on the same mesh, tile contents unchanged). freestiler's
    own default is 1.

The clip buffer is passed explicitly as ``buffer=5`` (:data:`TILE_BUFFER`) when
the installed freestiler accepts it: 5 pixels of a 256-pixel tile, tippecanoe's
default. freestiler's own default (12.8 px) repeats 10-12% more mesh features
per zoom in neighbouring tiles. It only removes repeats in the buffer zone; every
feature is still kept at every zoom.

Multi-layer tilesets go through ``freestiler.freestile_layers`` when it is
available, which loads and tiles one layer at a time (byte-identical output;
10 mesh layers 1.7 GB -> 311 MiB peak), and layers are loaded lazily (from the
archive, an Arrow IPC spill file or a GeoJSON source) so the Python side holds
one layer at a time too.

Neither backend is ever asked to drop features to cap tile size.
Tippecanoe's ``--drop-densest-as-needed`` thins a uniform mesh into visible
stripes, and freestiler's ``drop_rate`` removes features by bounding-box size,
which empties whole zoom levels of a mesh and cuts the refined cells along
breaklines first. Tile weight is controlled by each tileset's minimum zoom
instead.
"""

from __future__ import annotations

import contextlib
import contextvars
import importlib.util
import inspect
import json
import os
import shutil
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Optional, Sequence, Union

import geopandas as gpd
import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.compute as pc

TIPPECANOE = "tippecanoe"
FREESTILER = "freestiler"
_CHOICES = (TIPPECANOE, FREESTILER)

FEATURE_IDS_ENV = "RAS2CNG_VECTOR_TILE_IDS"
GZIP_LEVEL_ENV = "RAS2CNG_VECTOR_TILE_GZIP_LEVEL"
DEFAULT_GZIP_LEVEL = 6
TILE_BUFFER = 5
INPUT_ENV = "RAS2CNG_FREESTILER_INPUT"
ARROW = "arrow"
GEOJSON = "geojson"
WGS84 = "EPSG:4326"

_OPTIONS: contextvars.ContextVar[dict] = contextvars.ContextVar(
    "ras2cng_vector_tile_options", default={}
)
_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


@contextlib.contextmanager
def vector_tile_options(
    *, feature_ids: Optional[bool] = None, gzip_level: Optional[int] = None
) -> Iterator[None]:
    """Override the freestiler tile options for the enclosed calls.

    ``None`` leaves a setting to its environment variable or default.
    """

    current = dict(_OPTIONS.get())
    if feature_ids is not None:
        current["feature_ids"] = bool(feature_ids)
    if gzip_level is not None:
        current["gzip_level"] = _checked_gzip_level(gzip_level, "gzip_level")
    token = _OPTIONS.set(current)
    try:
        yield
    finally:
        _OPTIONS.reset(token)


def feature_ids_enabled() -> bool:
    """Whether vector tiles carry MVT feature IDs (default True)."""

    override = _OPTIONS.get().get("feature_ids")
    if override is not None:
        return override
    raw = os.environ.get(FEATURE_IDS_ENV, "").strip().lower()
    if not raw or raw in _TRUE:
        return True
    if raw in _FALSE:
        return False
    raise ValueError(f"{FEATURE_IDS_ENV} must be 1 or 0 (true/false); got {raw!r}")


def _checked_gzip_level(value, source: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{source} must be an integer from 0 to 9; got {value!r}")
    try:
        level = int(str(value).strip())
    except (TypeError, ValueError):
        level = -1
    if not 0 <= level <= 9:
        raise ValueError(f"{source} must be an integer from 0 to 9; got {value!r}")
    return level


def gzip_level() -> int:
    """Requested tile gzip level (default 6)."""

    override = _OPTIONS.get().get("gzip_level")
    if override is not None:
        return override
    raw = os.environ.get(GZIP_LEVEL_ENV, "").strip()
    return _checked_gzip_level(raw, GZIP_LEVEL_ENV) if raw else DEFAULT_GZIP_LEVEL


def tippecanoe_command() -> str:
    """Return the tippecanoe executable.

    ``tippecanoe`` is the portable default. Windows hosts that bridge to a
    Linux Tippecanoe installation can provide the full wrapper path through
    ``RAS2CNG_TIPPECANOE``.
    """

    return os.environ.get("RAS2CNG_TIPPECANOE", "tippecanoe")


def pmtiles_command() -> str:
    """Return the PMTiles conversion executable used after tippecanoe."""

    return os.environ.get("RAS2CNG_PMTILES", "pmtiles")


def freestiler_available() -> bool:
    return importlib.util.find_spec("freestiler") is not None


def freestiler_preferred() -> bool:
    """Whether the installed freestiler has Arrow input and ``buffer``.

    Such a build matches or beats tippecanoe's archive size on the HEC-RAS
    meshes measured, so ``auto`` prefers it over tippecanoe.
    """

    if not freestiler_available():
        return False
    try:
        capabilities = freestiler_capabilities()
    except Exception:  # pragma: no cover - a broken install is not preferred
        return False
    return capabilities["arrow"] and capabilities["layers_buffer"]


def vector_tiler_backend(required_commands: Sequence[str] | None = None) -> str:
    """Resolve ``RAS2CNG_VECTOR_TILER`` to a concrete backend name.

    ``required_commands`` are the CLIs the tippecanoe path needs at this call
    site. ``auto`` chooses freestiler when it has Arrow input and ``buffer``,
    then tippecanoe when all of its CLIs are on PATH, then any freestiler.
    """

    choice = os.environ.get("RAS2CNG_VECTOR_TILER", "").strip().lower() or "auto"
    if choice in _CHOICES:
        return choice
    if choice != "auto":
        raise ValueError(
            f"RAS2CNG_VECTOR_TILER must be one of auto, tippecanoe, freestiler; got {choice!r}"
        )
    if freestiler_preferred():
        return FREESTILER
    commands = required_commands or (tippecanoe_command(), pmtiles_command())
    if all(shutil.which(command) for command in commands):
        return TIPPECANOE
    if freestiler_available():
        return FREESTILER
    # Neither is usable; report the missing CLI.
    return TIPPECANOE


def arrow_input_enabled() -> bool:
    """Whether freestiler layers go in as Arrow (``RAS2CNG_FREESTILER_INPUT``)."""

    choice = os.environ.get(INPUT_ENV, "").strip().lower() or "auto"
    if choice not in ("auto", ARROW, GEOJSON):
        raise ValueError(f"{INPUT_ENV} must be one of auto, arrow, geojson; got {choice!r}")
    if choice == GEOJSON:
        return False
    supported = freestiler_available() and freestiler_capabilities()["arrow"]
    if choice == ARROW and not supported:
        raise RuntimeError(
            f"{INPUT_ENV}=arrow, but the installed freestiler has no Arrow input "
            "(gpt-cmdr/freestiler #5). Install a build with it, or unset the variable."
        )
    return bool(supported)


def require_freestiler() -> None:
    if not freestiler_available():
        raise RuntimeError(
            "RAS2CNG_VECTOR_TILER selects freestiler, but it is not installed. "
            "Install it with `pip install ras2cng[freestiler]`, or install the "
            "tippecanoe and pmtiles CLIs."
        )


def read_ndgeojson(path: Path) -> gpd.GeoDataFrame:
    """Read a delivery source written by ``_write_ndgeojson`` back as WGS84."""

    frame = gpd.read_file(f"GeoJSONSeq:{path}")
    if frame.crs is None:
        frame = frame.set_crs("EPSG:4326")
    return frame


def _freestiler_frame(frame: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Shape a layer the way tippecanoe would read it from GeoJSON.

    freestiler rejects null geometries and nested property values; tippecanoe
    skips the former and stringifies the latter, so match that.
    """

    frame = frame[frame.geometry.notna() & ~frame.geometry.is_empty]
    nested = [
        column
        for column in frame.columns
        if column != frame.geometry.name
        and frame[column].dtype == object
        and frame[column].map(_is_nested).any()
    ]
    if nested:
        frame = frame.copy()
        for column in nested:
            frame[column] = frame[column].map(
                lambda value: _json_text(value) if _is_nested(value) else value
            )
    return frame


def _is_nested(value: object) -> bool:
    return isinstance(value, (list, tuple, dict, np.ndarray))


def _json_text(value: object) -> str:
    if isinstance(value, np.ndarray):
        value = value.tolist()
    return json.dumps(value, default=str, separators=(",", ":"))


# --------------------------------------------------------------------------
# Arrow input
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ArrowLayer:
    """One freestiler source layer given as Arrow.

    ``load`` returns a ``pyarrow.Table`` with a WKB geometry column; it is
    called only when the tiler reaches the layer, so a tileset holds one layer
    at a time. ``crs`` is the CRS of that column (PROJJSON or any string
    pyproj accepts); ``EPSG:4326`` means it is already reprojected.
    """

    load: Callable[[], pa.Table]
    crs: str
    geometry_column: str = "geometry"


def crs_text(crs: Any) -> str:
    """A CRS as text both pyproj and freestiler read (PROJJSON or ``EPSG:4326``)."""

    from pyproj import CRS

    resolved = CRS.from_user_input(crs)
    if resolved.to_epsg() == 4326 and resolved.is_geographic:
        return WGS84
    return resolved.to_json()


def geoparquet_crs(schema: pa.Schema, fallback_crs: Any = None) -> tuple[str, str]:
    """Return ``(geometry column, CRS text)`` from a GeoParquet schema.

    An absent ``crs`` key means OGC:CRS84 (GeoParquet spec), as GeoPandas
    reads it. ``crs: null`` and a missing ``geo`` key use ``fallback_crs``;
    with no fallback the layer is refused, because tiling projected
    coordinates as degrees yields a valid, silently wrong tileset.
    """

    metadata = schema.metadata or {}
    geo = json.loads(metadata[b"geo"]) if b"geo" in metadata else {}
    column = geo.get("primary_column") or "geometry"
    column_meta = (geo.get("columns") or {}).get(column, {})
    if geo and "crs" not in column_meta:
        return column, WGS84
    crs = column_meta.get("crs")
    if crs is None:
        crs = fallback_crs
    if crs is None:
        raise ValueError("GeoParquet layer has no CRS and cannot be tiled")
    if isinstance(crs, dict):
        if crs.get("id") == {"authority": "EPSG", "code": 4326}:
            return column, WGS84
        return column, json.dumps(crs)
    return column, crs_text(crs)


_GEOMETRY_TYPE_NAMES = {
    0: "Point",
    1: "LineString",
    2: "LinearRing",
    3: "Polygon",
    4: "MultiPoint",
    5: "MultiLineString",
    6: "MultiPolygon",
    7: "GeometryCollection",
}


def geometry_type_names(geometries: np.ndarray) -> set[str]:
    import shapely

    ids = np.unique(shapely.get_type_id(geometries))
    return {_GEOMETRY_TYPE_NAMES[int(i)] for i in ids if int(i) in _GEOMETRY_TYPE_NAMES}


def reproject_table_to_wgs84(table: pa.Table, crs: str, geometry_column: str = "geometry") -> pa.Table:
    """Reproject a WKB geometry column to EPSG:4326 with pyproj.

    The fallback for CRSs freestiler refuses. Z is dropped first, as the
    GeoJSON path did (HEC-RAS writes NaN Z on some rings, which GEOS cannot
    reproject); the transformation is the one ``GeoDataFrame.to_crs`` uses.
    """

    import shapely
    from pyproj import CRS, Transformer

    index = table.schema.get_field_index(geometry_column)
    geometries = shapely.force_2d(
        shapely.from_wkb(table.column(index).to_numpy(zero_copy_only=False))
    )
    transformer = Transformer.from_crs(CRS.from_user_input(crs), WGS84, always_xy=True)

    def to_lonlat(coords: np.ndarray) -> np.ndarray:
        x, y = transformer.transform(coords[:, 0], coords[:, 1])
        return np.column_stack([x, y])

    geometries = shapely.transform(geometries, to_lonlat)
    wkb = pa.array(shapely.to_wkb(geometries), type=pa.binary())
    return table.set_column(index, pa.field(geometry_column, pa.binary()), wkb).replace_schema_metadata(None)


def _timestamp_text(column: pa.ChunkedArray) -> pa.ChunkedArray:
    """Timestamps as ``str(pandas.Timestamp)`` at millisecond precision.

    That is the form the GeoJSON path produced: ``json.dump(default=str)``
    wrote the text and GDAL read it back as a millisecond timestamp, which
    freestiler stringified (``1999-01-01 12:00:00``). Arrow's own formatting
    (``1999-01-01T12:00:00``) would change every time property.
    """

    unit_ms = pa.timestamp("ms", tz=column.type.tz)
    values = pc.cast(column, unit_ms, safe=False).to_pandas()
    codes, uniques = pd.factorize(values)
    text = np.array([str(value) for value in uniques] + [None], dtype=object)
    return pa.chunked_array([pa.array(text[codes], type=pa.string())])


def _nested_text(value: Any, geojson: bool = True) -> Any:
    """A nested value as text, the way the previous input path delivered it.

    Through GeoJSON (``geojson=True``, the viewer), ``json.dump(default=str)``
    wrote NumPy arrays as their ``str()`` text and lists or dicts as JSON,
    which ``_freestiler_frame`` stringified again. A GeoDataFrame handed to
    freestiler directly (``geojson=False``, ``ras2cng pmtiles``) went through
    ``_freestiler_frame`` only: compact JSON.
    """

    if value is None:
        return None
    if not geojson:
        return _json_text(value)
    plain = json.loads(json.dumps(value, default=str))
    return _json_text(plain) if _is_nested(plain) else plain


def _is_nested_type(data_type: pa.DataType) -> bool:
    return (
        pa.types.is_list(data_type)
        or pa.types.is_large_list(data_type)
        or pa.types.is_fixed_size_list(data_type)
        or pa.types.is_struct(data_type)
        or pa.types.is_map(data_type)
    )


def delivery_table(
    table: pa.Table, geometry_column: str = "geometry", *, geojson_nested: bool = True
) -> pa.Table:
    """Shape Arrow property columns as the GeoJSON path delivered them.

    freestiler's Arrow reader formats timestamps itself, writes NaN as a value,
    and skips list, struct and binary columns. The GeoJSON path instead wrote
    NaN as null, timestamps as ``str()`` text and nested values as text, and
    read every integer back as a signed integer, so match that here: tile
    properties do not change with the input path. All-null and binary columns
    carry no property and are dropped. ``geojson_nested``
    selects the nested-value text form (:func:`_nested_text`).
    """

    names: list[str] = []
    columns: list[pa.ChunkedArray] = []
    for field, column in zip(table.schema, table.columns):
        data_type = field.type
        if field.name == geometry_column:
            names.append(field.name)
            columns.append(column)
            continue
        if pa.types.is_dictionary(data_type):
            column = pc.cast(column, data_type.value_type)
            data_type = data_type.value_type
        if pa.types.is_null(data_type) or pa.types.is_binary(data_type) or pa.types.is_large_binary(data_type):
            continue
        if pa.types.is_timestamp(data_type):
            column = _timestamp_text(column)
        elif pa.types.is_floating(data_type):
            column = pc.if_else(pc.is_nan(column), pa.scalar(None, data_type), column)
        elif pa.types.is_integer(data_type) and data_type != pa.int64():
            if not pa.types.is_unsigned_integer(data_type) or (
                (pc.max(column).as_py() or 0) <= np.iinfo(np.int64).max
            ):
                column = pc.cast(column, pa.int64())
        elif _is_nested_type(data_type):
            values = column.to_pandas()
            column = pa.chunked_array(
                [
                    pa.array(
                        [_nested_text(value, geojson_nested) for value in values],
                        type=pa.string(),
                    )
                ]
            )
        # Consolidated archive layers carry the union of every layer's columns;
        # a column with no value in this layer delivers no property at all.
        if column.null_count == len(column):
            continue
        names.append(field.name)
        columns.append(column)
    return pa.table(columns, names=names)


def frame_table(frame: gpd.GeoDataFrame) -> pa.Table:
    """A (WGS84) GeoDataFrame as a :func:`delivery_table` with WKB geometry.

    Object columns are converted the way GeoJSON would carry them: NumPy
    arrays and lists become text (:func:`_nested_text`), and a column Arrow
    cannot type (mixed values) becomes ``str()`` text.
    """

    import shapely

    geometry_name = frame.geometry.name
    names: list[str] = []
    arrays: list[pa.Array] = []
    for name in frame.columns:
        if name == geometry_name:
            continue
        series = frame[name]
        if series.dtype == object and series.map(_is_nested).any():
            series = series.map(lambda value: _nested_text(value) if _is_nested(value) else value)
        try:
            array = pa.array(series, from_pandas=True)
        except (pa.ArrowInvalid, pa.ArrowTypeError, pa.ArrowNotImplementedError):
            array = pa.array(
                [None if _is_missing(value) else str(value) for value in series], type=pa.string()
            )
        names.append(str(name))
        arrays.append(array)
    names.append("geometry")
    arrays.append(pa.array(shapely.to_wkb(np.asarray(frame.geometry.values)), type=pa.binary()))
    return delivery_table(pa.table(arrays, names=names))


def _is_missing(value: Any) -> bool:
    if value is None:
        return True
    try:
        return bool(pd.isna(value))
    except (TypeError, ValueError):
        return False


def write_arrow_spill(table: pa.Table, path: Path) -> ArrowLayer:
    """Spill a WGS84 delivery table to an Arrow IPC file; load it back lazily."""

    path.parent.mkdir(parents=True, exist_ok=True)
    with pa.OSFile(str(path), "wb") as sink, pa.ipc.new_file(sink, table.schema) as writer:
        writer.write_table(table)

    def load(path: Path = path) -> pa.Table:
        with pa.OSFile(str(path), "rb") as source:
            return pa.ipc.open_file(source).read_all()

    return ArrowLayer(load=load, crs=WGS84)


def _accepts(function, name: str) -> bool:
    try:
        return name in inspect.signature(function).parameters
    except (TypeError, ValueError):  # pragma: no cover - builtins without signatures
        return False


def freestiler_capabilities(module=None) -> dict[str, bool]:
    """What the installed freestiler supports, detected from its API.

    ``layers``: ``freestile_layers`` (one layer in memory at a time);
    ``gzip``: ``freestile(gzip_level=...)``; ``layers_gzip``:
    ``freestile_layers(gzip_level=...)``; ``buffer`` / ``layers_buffer``: the
    clip buffer on ``freestile`` / ``freestile_layers``; ``arrow``: Arrow
    stream layers with a per-layer CRS, reprojected in Rust
    (``_LayeredTiler.add_arrow_layer`` and ``FreestileLayer.crs``).
    """

    if module is None:
        import freestiler as module
    layered = getattr(module, "freestile_layers", None)
    layered_ok = callable(layered)
    tiler = getattr(module, "_LayeredTiler", None)
    layer_fields = getattr(getattr(module, "FreestileLayer", None), "__dataclass_fields__", {})
    return {
        "layers": layered_ok,
        "gzip": _accepts(module.freestile, "gzip_level"),
        "layers_gzip": layered_ok and _accepts(layered, "gzip_level"),
        "buffer": _accepts(module.freestile, "buffer"),
        "layers_buffer": layered_ok and _accepts(layered, "buffer"),
        "arrow": layered_ok and hasattr(tiler, "add_arrow_layer") and "crs" in layer_fields,
    }


_WARNED: set[str] = set()


def _warn_once(key: str, message: str) -> None:
    if key not in _WARNED:
        _WARNED.add(key)
        warnings.warn(message, RuntimeWarning, stacklevel=3)


# CRS definitions freestiler refused in this process; layers in them are
# reprojected in Python instead of retrying Rust for every layer.
_RUST_REFUSED_CRS: set[str] = set()

LayerSource = Union[gpd.GeoDataFrame, ArrowLayer, Callable[[], gpd.GeoDataFrame]]


def _load(source: LayerSource) -> gpd.GeoDataFrame:
    if callable(source) and not isinstance(source, gpd.GeoDataFrame):
        return source()
    return source


def _arrow_table(layer: ArrowLayer) -> tuple[pa.Table, str]:
    """Load an Arrow layer, reprojecting in Python if Rust refused its CRS."""

    table = layer.load()
    if layer.crs != WGS84 and layer.crs in _RUST_REFUSED_CRS:
        return reproject_table_to_wgs84(table, layer.crs, layer.geometry_column), WGS84
    return table, layer.crs


def _run_freestiler_arrow(freestiler, output: Path, layers: Mapping[str, LayerSource], kwargs: dict, applied: dict) -> None:
    """Tile Arrow (and GeoDataFrame) layers with ``freestile_layers``.

    A CRS freestiler refuses raises ``ValueError`` before any of that layer is
    tiled. The archive is then rebuilt with that CRS reprojected in Python;
    each distinct CRS is tried in Rust at most once per process.
    """

    from ras2cng.cog import atomic_output

    while True:
        attempt: dict[str, Any] = {"crs": None, "layer": None}
        python_reprojected: list[str] = []

        def pairs():
            for name, source in layers.items():
                attempt["crs"] = attempt["layer"] = None
                if isinstance(source, ArrowLayer):
                    table, crs = _arrow_table(source)
                    if not table.num_rows:
                        continue
                    if crs != source.crs:
                        python_reprojected.append(name)
                    attempt.update(crs=crs, layer=name)
                    yield name, freestiler.FreestileLayer(
                        input=table, crs=crs, geometry_column=source.geometry_column
                    )
                    del table
                else:
                    frame = _freestiler_frame(_load(source))
                    if not frame.empty:
                        yield name, frame
                    del frame
            attempt["crs"] = attempt["layer"] = None

        try:
            with atomic_output(output, suffix=".pmtiles") as staged:
                freestiler.freestile_layers(pairs(), str(staged), **kwargs)
        except ValueError as exc:
            crs = attempt["crs"]
            if "at least one layer" in str(exc):
                raise ValueError("A vector tileset needs at least one non-empty source layer.") from exc
            if crs is None or crs == WGS84 or crs in _RUST_REFUSED_CRS or "no features" in str(exc):
                raise
            _RUST_REFUSED_CRS.add(crs)
            _warn_once(
                f"crs-refused:{crs}",
                f"freestiler cannot reproject layer {attempt['layer']!r} ({exc}); "
                "layers in this CRS are reprojected in Python (pyproj) and passed as "
                "EPSG:4326 Arrow tables instead.",
            )
            continue
        applied["python_reprojected"] = python_reprojected
        return


def run_freestiler(
    output: Path,
    layers: Mapping[str, LayerSource],
    min_zoom: int,
    max_zoom: int,
) -> dict:
    """Write ``layers`` to one PMTiles archive, one source layer per key.

    A layer is an :class:`ArrowLayer`, a GeoDataFrame or a zero-argument
    callable returning one; callables are loaded only when their turn comes.
    Every feature is kept at every zoom (no ``drop_rate``); the output is
    staged beside ``output`` and replaced atomically. Returns the options
    actually applied (``generate_ids``, ``gzip_level``, ``layered``, plus
    ``buffer`` and ``input`` when the installed freestiler has them).
    """

    require_freestiler()
    import freestiler

    from ras2cng.cog import atomic_output

    capabilities = freestiler_capabilities(freestiler)
    applied: dict = {"generate_ids": feature_ids_enabled(), "gzip_level": None, "layered": False}
    kwargs: dict = {
        "min_zoom": int(min_zoom),
        "max_zoom": int(max_zoom),
        "quiet": True,
        "generate_ids": applied["generate_ids"],
    }
    level = gzip_level()
    output = Path(output)
    empty_message = "A vector tileset needs at least one non-empty source layer."
    arrow = any(isinstance(source, ArrowLayer) for source in layers.values())

    if arrow:
        if not capabilities["arrow"]:
            raise RuntimeError(
                "Arrow layers need a freestiler with Arrow input (gpt-cmdr/freestiler #5)."
            )
        applied.update(layered=True, input=ARROW)
        if capabilities["layers_gzip"]:
            kwargs["gzip_level"] = applied["gzip_level"] = level
        if capabilities["layers_buffer"]:
            kwargs["buffer"] = applied["buffer"] = TILE_BUFFER
        _run_freestiler_arrow(freestiler, output, layers, kwargs, applied)
        return applied

    if len(layers) > 1 and capabilities["layers"]:
        applied["layered"] = True
        if capabilities["layers_gzip"]:
            kwargs["gzip_level"] = applied["gzip_level"] = level
        elif capabilities["gzip"]:
            _warn_once(
                "layers_gzip",
                "freestiler.freestile_layers has no gzip_level pass-through in this build; "
                "multi-layer tiles use freestiler's default gzip level instead of "
                f"{GZIP_LEVEL_ENV}={level}.",
            )
        if capabilities["layers_buffer"]:
            kwargs["buffer"] = applied["buffer"] = TILE_BUFFER

        def prepared():
            for name, source in layers.items():
                frame = _freestiler_frame(_load(source))
                if not frame.empty:
                    yield name, frame
                del frame

        with atomic_output(output, suffix=".pmtiles") as staged:
            try:
                freestiler.freestile_layers(prepared(), str(staged), **kwargs)
            except ValueError as exc:
                if "at least one layer" in str(exc):
                    raise ValueError(empty_message) from exc
                raise
        return applied

    frames = {name: _freestiler_frame(_load(source)) for name, source in layers.items()}
    frames = {name: frame for name, frame in frames.items() if not frame.empty}
    if not frames:
        raise ValueError(empty_message)
    if capabilities["gzip"]:
        kwargs["gzip_level"] = applied["gzip_level"] = level
    if capabilities["buffer"]:
        kwargs["buffer"] = applied["buffer"] = TILE_BUFFER
    with atomic_output(output, suffix=".pmtiles") as staged:
        freestiler.freestile(frames, str(staged), **kwargs)
    return applied
