"""HTTP range index for ``ras2cng-depth-stack/v1`` NetCDF4 depth catalogs.

``copy_depth_stack_indexed`` creates a NEW byte-for-byte copy of an existing
depth stack (written by :func:`ras2cng.depth_stack.write_depth_stack`), adds a
random public object identity in a new contiguous ``range_object_id``
character variable, and returns a closed JSON-serializable chunk
index (schema ``ras2cng-depth-stack-ranges/v1``). Chunk bytes are never
re-encoded: every compressed chunk in the copy is verified identical to the
source chunk, so all stored pixels are preserved exactly. The source is never
modified and an existing target is never replaced.

The index lists, for each HDF5 chunk of ``depth(flow,y,x)``, the absolute byte
offset and stored length inside the NetCDF file, together with the exact filter
pipeline. A reader can then fetch selected profiles with HTTP 206 requests and
decode them without HDF5. See ``docs/depth-stack-ranges.md`` for the contract.
Unsupported layouts, filters, scaling or grids are rejected, never guessed.
"""

from __future__ import annotations

import hashlib
import itertools
import math
import os
import shutil
import tempfile
import uuid
from pathlib import Path

RANGE_INDEX_SCHEMA = "ras2cng-depth-stack-ranges/v1"
STACK_SCHEMA = "ras2cng-depth-stack/v1"
MAX_PROFILES = 1024
MAX_AXIS = 32768
MAX_CHUNKS = 65536
MAX_LABEL_BYTES = 256
MAX_PROVENANCE_BYTES = 65536
MAX_INDEX_BYTES = 1048576
CHUNK_EDGE = 256
CHUNK_SLACK = 4096
IDENTITY_NOTE = (
    "Random UUID4 assigned when this indexed copy was created; public object"
    " identity for HTTP range reads, not a content digest."
)

_GLOBALS = {"Conventions", "schema", "role", "provenance_json", "_NCProperties"}
_ID_VAR, _ID_DIM, _ID_LENGTH = "range_object_id", "range_object_id_length", 36
_ID_ATTRS = {
    "DIMENSION_LIST",
    "_Netcdf4Coordinates",
    "_Netcdf4Dimid",
    "long_name",
    "comment",
    "range_index_schema",
}
_DEPTH_ATTRS = {
    "_Netcdf4Coordinates",
    "_FillValue",
    "units",
    "long_name",
    "grid_mapping",
    "coordinates",
    "DIMENSION_LIST",
    "_Netcdf4Dimid",
}
_VARIABLES = {"flow", "profile", "x", "y", "crs", "depth"}
_SHUFFLE, _DEFLATE = 2, 1
_KEYS = {
    "schema": None,
    "object": {"object_id", "byte_length", "witness"},
    "variable": {
        "name",
        "dimensions",
        "dtype",
        "byteorder",
        "shape",
        "chunk_shape",
        "fill_value",
        "units",
    },
    "filters": None,
    "grid": {
        "crs",
        "width",
        "height",
        "geotransform",
        "x_first_center",
        "y_first_center",
    },
    "profiles": {"labels", "axis_semantics", "flows", "flow_units"},
    "chunks": {
        "grid",
        "order",
        "decoded_length",
        "max_length",
        "filter_mask",
        "offsets",
        "lengths",
    },
}


def _text(value):
    if isinstance(value, bytes):
        return value.decode("utf-8")
    if hasattr(value, "tolist") and not isinstance(value, str):
        value = value.tolist()
        if isinstance(value, list) and len(value) == 1:
            value = value[0]
        return _text(value)
    if not isinstance(value, str):
        raise ValueError("expected a string attribute")  # noqa: TRY004
    return value


def _require_uuid4(text) -> None:
    try:
        valid = str(uuid.UUID(text)) == text and uuid.UUID(text).version == 4
    except (TypeError, ValueError, AttributeError):
        valid = False
    if not valid:
        raise ValueError("range object identity must be a canonical UUID4")


def _check_metadata(path: Path, *, indexed: bool) -> None:
    """Check declared NetCDF dimensions and the writer's closed metadata layout."""
    import netCDF4
    from rasterio.crs import CRS

    with netCDF4.Dataset(path, "r") as nc:
        expected_dims = {"flow", "y", "x"} | ({_ID_DIM} if indexed else set())
        if set(nc.dimensions) != expected_dims or any(
            d.isunlimited() for d in nc.dimensions.values()
        ):
            raise ValueError("unsupported NetCDF dimension declarations")
        dimensions = {
            "flow": ("flow",),
            "profile": ("flow",),
            "x": ("x",),
            "y": ("y",),
            "crs": (),
            "depth": ("flow", "y", "x"),
        }
        attrs = {
            "flow": {"units", "axis_semantics", "long_name"},
            "profile": set(),
            "x": {"standard_name", "units", "axis"},
            "y": {"standard_name", "units", "axis"},
            "crs": {
                "spatial_ref",
                "crs_wkt",
                "epsg_code",
                "grid_mapping_name",
                "longitude_of_projection_origin",
                "standard_parallel",
                "false_easting",
                "false_northing",
                "earth_radius",
                "GeoTransform",
            },
            "depth": {
                "_FillValue",
                "units",
                "long_name",
                "grid_mapping",
                "coordinates",
            },
        }
        if "flow" not in nc.variables:
            raise ValueError("missing flow variable")
        semantics = getattr(nc["flow"], "axis_semantics", None)
        if semantics == "discharge":
            attrs["flow"].add("flow_units")
        if indexed:
            dimensions[_ID_VAR] = (_ID_DIM,)
            attrs[_ID_VAR] = {"long_name", "comment", "range_index_schema"}
            if len(nc.dimensions[_ID_DIM]) != 36:
                raise ValueError("invalid identity dimension")
        for name, dims in dimensions.items():
            if (
                name not in nc.variables
                or nc[name].dimensions != dims
                or set(nc[name].ncattrs()) != attrs[name]
            ):
                raise ValueError(f"unsupported {name} dimensions or attributes")
        if nc["depth"].long_name != "Depth" or nc["flow"].long_name != (
            "Discharge" if semantics == "discharge" else "Ordered profile index"
        ):
            raise ValueError("unexpected variable labels")
        if nc["profile"].datatype is not str and not isinstance(
            nc["profile"].datatype, netCDF4.VLType
        ):
            raise ValueError("profile must be a variable-length string")
        for name in ("x", "y"):
            var = nc[name]
            if (
                var.standard_name != f"projection_{name}_coordinate"
                or var.units != "m"
                or var.axis != name.upper()
            ):
                raise ValueError("coordinate declarations disagree with projected grid")
        crs = nc["crs"]
        canonical = CRS.from_epsg(3857).to_wkt()
        if (
            crs.dtype.str != "<i4"
            or crs.spatial_ref != canonical
            or crs.crs_wkt != canonical
            or crs.grid_mapping_name != "mercator"
            or any(
                getattr(crs, key) != value
                for key, value in {
                    "longitude_of_projection_origin": 0.0,
                    "standard_parallel": 0.0,
                    "false_easting": 0.0,
                    "false_northing": 0.0,
                    "earth_radius": 6378137.0,
                }.items()
            )
        ):
            raise ValueError(
                "CRS declarations disagree with canonical writer EPSG:3857"
            )
        if indexed and nc[_ID_VAR].long_name != "Public range object identity":
            raise ValueError("unexpected identity label")


def _inspect(path: Path, *, indexed: bool) -> dict:
    """Validate a depth stack through h5py and return its range index."""
    try:
        _check_metadata(path, indexed=indexed)
        return _inspect_file(path, indexed=indexed)
    except (KeyError, AttributeError, TypeError, IndexError, UnicodeError) as exc:
        raise ValueError(f"missing required NetCDF item: {exc}") from exc


def _inspect_file(path: Path, *, indexed: bool) -> dict:
    import json

    import h5py
    import numpy as np

    size = path.stat().st_size
    with h5py.File(path, "r") as nc:
        names = set(nc.attrs)
        if "source_sha256_json" in names:
            raise ValueError(
                "source carries private source identities; rewrite it with include_source_hashes=False"
            )
        if names != _GLOBALS:
            raise ValueError(
                f"unsupported global attributes: {sorted(names ^ _GLOBALS)}"
            )
        if (
            _text(nc.attrs["Conventions"]) != "CF-1.8"
            or _text(nc.attrs["schema"]) != STACK_SCHEMA
            or _text(nc.attrs["role"]) != "display_derivative"
        ):
            raise ValueError("not a ras2cng-depth-stack/v1 display derivative")
        provenance = _text(nc.attrs["provenance_json"])
        parsed = json.loads(provenance)
        if (
            len(provenance.encode()) > MAX_PROVENANCE_BYTES
            or not isinstance(parsed, dict)
            or not all(
                isinstance(k, str)
                and k.strip()
                and "\x00" not in k
                and isinstance(v, str)
                and "\x00" not in v
                for k, v in parsed.items()
            )
        ):
            raise ValueError("provenance_json must be a bounded string mapping")
        object_id = witness = None
        variables = _VARIABLES | ({_ID_VAR, _ID_DIM} if indexed else set())
        if set(nc.keys()) != variables:
            raise ValueError(
                f"unsupported variable set: {sorted(set(nc.keys()) ^ variables)}"
            )
        if indexed:
            ident = nc[_ID_VAR]
            offset = ident.id.get_offset()
            if (
                set(ident.attrs) != _ID_ATTRS
                or ident.dtype.str != "|S1"
                or ident.shape != (_ID_LENGTH,)
                or ident.id.get_create_plist().get_layout() != h5py.h5d.CONTIGUOUS
                or ident.id.get_create_plist().get_nfilters() != 0
                or offset is None
                or ident.id.get_storage_size() != _ID_LENGTH
                or offset + _ID_LENGTH > size
                or _text(ident.attrs["range_index_schema"]) != RANGE_INDEX_SCHEMA
                or _text(ident.attrs["comment"]) != IDENTITY_NOTE
            ):
                raise ValueError("invalid range object identity variable")
            object_id = b"".join(ident[()].tolist()).decode("ascii")
            _require_uuid4(object_id)
            witness = {"offset": int(offset), "length": _ID_LENGTH, "encoding": "ascii"}
        depth = nc["depth"]
        if set(depth.attrs) != _DEPTH_ATTRS:
            raise ValueError(
                f"unsupported depth attributes (scaling/packing?): {sorted(set(depth.attrs) ^ _DEPTH_ATTRS)}"
            )
        if depth.dtype.str != "<f4" or depth.ndim != 3:
            raise ValueError("depth must be little-endian float32 (flow,y,x)")
        profiles, height, width = depth.shape
        fill = np.asarray(depth.attrs["_FillValue"]).reshape(-1)
        if (
            fill.size != 1
            or not math.isnan(float(fill[0]))
            or not math.isnan(float(depth.fillvalue))
        ):
            raise ValueError("depth fill must be NaN")
        units = _text(depth.attrs["units"])
        if (
            units not in {"ft", "m"}
            or _text(depth.attrs["grid_mapping"]) != "crs"
            or _text(depth.attrs["coordinates"]) != "profile"
        ):
            raise ValueError("depth units/grid_mapping/coordinates unsupported")
        if not (
            1 <= profiles <= MAX_PROFILES
            and 1 <= height <= MAX_AXIS
            and 1 <= width <= MAX_AXIS
        ):
            raise ValueError("depth shape exceeds bounds")
        chunk = (1, min(CHUNK_EDGE, height), min(CHUNK_EDGE, width))
        plist = depth.id.get_create_plist()
        if plist.get_layout() != h5py.h5d.CHUNKED or depth.chunks != chunk:
            raise ValueError(f"depth chunking must be {chunk}")
        pipeline = [plist.get_filter(i)[:3] for i in range(plist.get_nfilters())]
        if (
            len(pipeline) != 2
            or pipeline[0][0] != _SHUFFLE
            or tuple(pipeline[0][2]) != (4,)
            or pipeline[1][0] != _DEFLATE
            or len(pipeline[1][2]) != 1
            or not 1 <= pipeline[1][2][0] <= 9
        ):
            raise ValueError(f"unsupported filter pipeline: {pipeline}")
        level = int(pipeline[1][2][0])

        flow, label = nc["flow"], nc["profile"]
        if flow.shape != (profiles,) or label.shape != (profiles,):
            raise ValueError("flow/profile length mismatch")
        labels = [_text(v) for v in label[()]]
        if len(set(labels)) != profiles or any(
            not v.strip() or len(v.encode()) > MAX_LABEL_BYTES for v in labels
        ):
            raise ValueError("profile labels must be unique, nonblank and bounded")
        semantics = _text(flow.attrs.get("axis_semantics", b""))
        values = flow[()]
        if semantics == "profile_index":
            if (
                flow.dtype.str != "<i4"
                or _text(flow.attrs["units"]) != "1"
                or "flow_units" in flow.attrs
                or values.tolist() != list(range(profiles))
            ):
                raise ValueError("profile_index axis must be int32 0..n-1 with units 1")
            flows = flow_units = None
        elif semantics == "discharge":
            flow_units = _text(flow.attrs.get("flow_units", b""))
            cf = {"cfs": "ft3 s-1", "cms": "m3 s-1"}.get(flow_units)
            flows = values.tolist()
            steps = np.diff(values)
            if (
                flow.dtype.str != "<f8"
                or cf is None
                or _text(flow.attrs["units"]) != cf
                or not np.isfinite(values).all()
                or not (np.all(steps > 0) or np.all(steps < 0))
            ):
                raise ValueError("discharge axis must be finite monotonic f8 cfs/cms")
        else:
            raise ValueError("unsupported flow axis semantics")

        crs = nc["crs"]
        if _text(crs.attrs.get("epsg_code", b"")) != "EPSG:3857":
            raise ValueError("depth stack grid must be EPSG:3857")
        geo = [float(v) for v in _text(crs.attrs["GeoTransform"]).split()]
        if (
            len(geo) != 6
            or not all(math.isfinite(v) for v in geo)
            or geo[2] != 0
            or geo[4] != 0
            or geo[1] <= 0
            or geo[5] >= 0
        ):
            raise ValueError("grid must be a finite north-up axis-aligned transform")
        for name, length, origin, spacing in (
            ("x", width, geo[0], geo[1]),
            ("y", height, geo[3], geo[5]),
        ):
            coordinate = nc[name][()]
            expected_centers = origin + (np.arange(length) + 0.5) * spacing
            if coordinate.dtype.str != "<f8" or not np.array_equal(
                coordinate, expected_centers
            ):
                raise ValueError(f"{name} centers disagree with GeoTransform")

        rows = -(-height // chunk[1])
        cols = -(-width // chunk[2])
        count = profiles * rows * cols
        if count > MAX_CHUNKS or depth.id.get_num_chunks() != count:
            raise ValueError("all depth chunks must be allocated and bounded")
        decoded = chunk[1] * chunk[2] * 4
        offsets, lengths = [0] * count, [0] * count
        for i in range(count):
            info = depth.id.get_chunk_info(i)
            p, r, c = info.chunk_offset
            slot = (p * rows + r // chunk[1]) * cols + c // chunk[2]
            if info.filter_mask != 0:
                raise ValueError("skipped-filter chunks are unsupported")
            if (
                info.byte_offset is None
                or not 0 < info.size <= decoded + CHUNK_SLACK
                or info.byte_offset + info.size > size
                or lengths[slot]
            ):
                raise ValueError("chunk storage out of bounds")
            offsets[slot], lengths[slot] = int(info.byte_offset), int(info.size)
        spans = sorted(zip(offsets, lengths))
        if any(a + n > b for (a, n), (b, _) in itertools.pairwise(spans)):
            raise ValueError("chunk storage overlaps")

    return {
        "schema": RANGE_INDEX_SCHEMA,
        "object": {"object_id": object_id, "byte_length": size, "witness": witness},
        "variable": {
            "name": "depth",
            "dimensions": ["flow", "y", "x"],
            "dtype": "float32",
            "byteorder": "little",
            "shape": [profiles, height, width],
            "chunk_shape": list(chunk),
            "fill_value": "NaN",
            "units": units,
        },
        "filters": [
            {"id": _SHUFFLE, "name": "shuffle", "element_size": 4},
            {"id": _DEFLATE, "name": "deflate", "level": level, "format": "zlib"},
        ],
        "grid": {
            "crs": "EPSG:3857",
            "width": width,
            "height": height,
            "geotransform": geo,
            "x_first_center": geo[0] + 0.5 * geo[1],
            "y_first_center": geo[3] + 0.5 * geo[5],
        },
        "profiles": {
            "labels": labels,
            "axis_semantics": semantics,
            "flows": flows,
            "flow_units": flow_units,
        },
        "chunks": {
            "grid": [profiles, rows, cols],
            "order": "C",
            "decoded_length": decoded,
            "max_length": max(lengths),
            "filter_mask": 0,
            "offsets": offsets,
            "lengths": lengths,
        },
    }


def _read_witness(path: Path, witness: dict) -> bytes:
    with path.open("rb") as stream:
        stream.seek(witness["offset"])
        return stream.read(witness["length"])


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def inspect_depth_stack_ranges(path: str | Path) -> dict:
    """Re-inspect an indexed copy and return its closed v1 range index.

    Raises ``ValueError`` unless the file is a supported depth stack carrying a
    contiguous ``range_object_id`` whose stored bytes at the witness offset
    equal the ASCII UUID. Only that 36-byte range is read directly.
    """
    target = Path(path)
    index = _inspect(target, indexed=True)
    obj = index["object"]
    if _read_witness(target, obj["witness"]) != obj["object_id"].encode("ascii"):
        raise ValueError("identity witness bytes disagree with range_object_id")
    validate_range_index(index)
    return index


def copy_depth_stack_indexed(source: str | Path, target: str | Path) -> dict:
    """Create a NEW indexed copy of a depth stack and return its range index.

    The source must be a ``ras2cng-depth-stack/v1`` file without private source
    identities. Its bytes are copied to a temporary sibling of ``target``, a
    contiguous ``range_object_id(range_object_id_length=36)`` character
    variable holding a random UUID4 is appended (globals unchanged), every
    compressed chunk is verified identical to the source chunk, and the copy
    is linked into place without replacing an existing file. Offsets, length
    and identity are re-inspected after the final close/placement. The source
    must be byte-identical before and after. No value is rescaled or rewarped.
    """
    import h5py
    import netCDF4
    import numpy as np

    origin, destination = Path(source), Path(target)
    if destination.exists() or destination.resolve() == origin.resolve():
        raise FileExistsError("target exists; indexed copies never overwrite")
    before = _sha256(origin)
    reference = _inspect(origin, indexed=False)
    destination.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".partial", dir=destination.parent
    )
    os.close(handle)
    staged = Path(temporary)
    try:
        shutil.copyfile(origin, staged)
        if _sha256(staged) != before:
            raise ValueError("copy differs from source")
        object_id = str(uuid.uuid4())
        with netCDF4.Dataset(staged, "a") as nc:
            nc.createDimension(_ID_DIM, _ID_LENGTH)
            ident = nc.createVariable(_ID_VAR, "S1", (_ID_DIM,), contiguous=True)
            ident.long_name = "Public range object identity"
            ident.comment = IDENTITY_NOTE
            ident.range_index_schema = RANGE_INDEX_SCHEMA
            ident[:] = np.frombuffer(object_id.encode("ascii"), dtype="S1")
        index = inspect_depth_stack_ranges(staged)
        if index["object"]["object_id"] != object_id or {
            k: v for k, v in index.items() if k != "object"
        } != {k: v for k, v in reference.items() if k != "object"}:
            raise ValueError("indexed copy metadata or chunk offsets differ")
        with h5py.File(origin, "r") as old, h5py.File(staged, "r") as new:
            a, b = old["depth"].id, new["depth"].id
            for i in range(a.get_num_chunks()):
                where = a.get_chunk_info(i).chunk_offset
                if a.read_direct_chunk(where) != b.read_direct_chunk(where):
                    raise ValueError("indexed copy changed stored chunk bytes")
        try:
            os.link(staged, destination)
        except FileExistsError:
            raise
        except OSError:
            if os.name != "nt":
                raise
            os.rename(staged, destination)  # Windows rename never replaces
    finally:
        staged.unlink(missing_ok=True)
    try:
        if _sha256(origin) != before:
            raise ValueError("source changed while copying")
        final = inspect_depth_stack_ranges(destination)
        if final != index:
            raise ValueError("final placement changed offsets or identity")
    except Exception:
        destination.unlink(missing_ok=True)  # only the file this call created
        raise
    return final


def dumps_range_index(index: dict) -> str:
    """Serialize a validated index as compact canonical JSON (bounded size)."""
    import json

    validate_range_index(index)
    return json.dumps(index, separators=(",", ":"), allow_nan=False)


def _validate_range_index(index: dict) -> None:
    """Validate the closed v1 JSON contract; malformed values raise ValueError.

    Offsets are absolute object bytes. Pixel sizes are metres in EPSG:3857;
    depth units are ft or m. This validates metadata, not the remote object's
    bytes: readers must still verify its UUID witness and strong HTTP ETag.
    """
    import json

    def require(condition, message):
        if not condition:
            raise ValueError(message)

    def integer(value, low, high=2**53 - 1):
        return type(value) is int and low <= value <= high

    def finite(value):
        return type(value) in (int, float) and math.isfinite(value)

    def vector(value, size):
        return isinstance(value, list) and len(value) == size

    require(
        isinstance(index, dict) and set(index) == set(_KEYS),
        "range index has unexpected top-level fields",
    )
    require(index["schema"] == RANGE_INDEX_SCHEMA, "unsupported range index schema")
    for key, fields in _KEYS.items():
        if fields is not None:
            require(
                isinstance(index[key], dict) and set(index[key]) == fields,
                f"range index {key} has unexpected fields",
            )
    obj, var, chunks = index["object"], index["variable"], index["chunks"]
    geo, profiles = index["grid"], index["profiles"]
    require(integer(obj["byte_length"], 36), "invalid object byte length")
    witness = obj["witness"]
    require(
        isinstance(witness, dict) and set(witness) == {"offset", "length", "encoding"},
        "invalid object identity witness",
    )
    require(
        witness["encoding"] == "ascii"
        and type(witness["length"]) is int
        and witness["length"] == 36
        and integer(witness["offset"], 0, obj["byte_length"] - 36),
        "invalid object identity witness",
    )
    _require_uuid4(obj["object_id"])
    filters = index["filters"]
    require(
        vector(filters, 2) and all(isinstance(f, dict) for f in filters),
        "unsupported filter pipeline",
    )
    level = filters[1].get("level")
    require(
        integer(level, 1, 9)
        and filters
        == [
            {"id": _SHUFFLE, "name": "shuffle", "element_size": 4},
            {"id": _DEFLATE, "name": "deflate", "level": level, "format": "zlib"},
        ],
        "unsupported filter pipeline",
    )
    require(
        all(type(filters[i]["id"]) is int for i in range(2))
        and type(filters[0]["element_size"]) is int,
        "invalid filter integers",
    )
    require(vector(var["shape"], 3), "invalid depth shape")
    p, h, w = var["shape"]
    require(
        integer(p, 1, MAX_PROFILES)
        and integer(h, 1, MAX_AXIS)
        and integer(w, 1, MAX_AXIS),
        "depth shape exceeds bounds",
    )
    chunk = [1, min(CHUNK_EDGE, h), min(CHUNK_EDGE, w)]
    grid = [p, -(-h // chunk[1]), -(-w // chunk[2])]
    count = math.prod(grid)
    require(
        var["name"] == "depth"
        and var["dimensions"] == ["flow", "y", "x"]
        and var["dtype"] == "float32"
        and var["byteorder"] == "little"
        and var["fill_value"] == "NaN"
        and var["units"] in ("ft", "m"),
        "unsupported depth variable",
    )
    require(
        vector(var["chunk_shape"], 3)
        and all(type(v) is int for v in var["chunk_shape"])
        and var["chunk_shape"] == chunk
        and vector(chunks["grid"], 3)
        and all(type(v) is int for v in chunks["grid"])
        and chunks["grid"] == grid
        and chunks["order"] == "C"
        and type(chunks["filter_mask"]) is int
        and chunks["filter_mask"] == 0
        and type(chunks["decoded_length"]) is int
        and chunks["decoded_length"] == chunk[1] * chunk[2] * 4
        and count <= MAX_CHUNKS,
        "range index layout is inconsistent",
    )
    offsets, lengths = chunks["offsets"], chunks["lengths"]
    require(vector(offsets, count) and vector(lengths, count), "invalid chunk arrays")
    require(
        all(
            integer(o, 0)
            and integer(n, 1, chunks["decoded_length"] + CHUNK_SLACK)
            and o + n <= obj["byte_length"]
            for o, n in zip(offsets, lengths)
        ),
        "chunk ranges fall outside the object",
    )
    require(
        integer(chunks["max_length"], 1) and chunks["max_length"] == max(lengths),
        "invalid maximum chunk length",
    )
    spans = sorted([*zip(offsets, lengths), (witness["offset"], 36)])
    require(
        all(a + n <= b for (a, n), (b, _) in itertools.pairwise(spans)),
        "chunk or witness ranges overlap",
    )
    require(
        geo["crs"] == "EPSG:3857"
        and type(geo["width"]) is int
        and type(geo["height"]) is int
        and geo["width"] == w
        and geo["height"] == h,
        "invalid grid CRS or dimensions",
    )
    gt = geo["geotransform"]
    require(vector(gt, 6) and all(finite(v) for v in gt), "invalid geotransform")
    require(
        gt[1] > 0
        and gt[2] == 0
        and gt[4] == 0
        and gt[5] < 0
        and finite(geo["x_first_center"])
        and finite(geo["y_first_center"])
        and geo["x_first_center"] == gt[0] + gt[1] / 2
        and geo["y_first_center"] == gt[3] + gt[5] / 2,
        "invalid grid centres or orientation",
    )
    labels = profiles["labels"]
    require(
        vector(labels, p)
        and all(
            isinstance(v, str)
            and v.strip()
            and "\x00" not in v
            and len(v.encode("utf-8")) <= MAX_LABEL_BYTES
            for v in labels
        ),
        "invalid profile labels",
    )
    require(len(set(labels)) == p, "duplicate profile labels")
    if profiles["axis_semantics"] == "profile_index":
        require(
            profiles["flows"] is None and profiles["flow_units"] is None,
            "profile index does not imply discharge",
        )
    elif profiles["axis_semantics"] == "discharge":
        values = profiles["flows"]
        require(
            profiles["flow_units"] in ("cfs", "cms")
            and vector(values, p)
            and all(finite(v) for v in values),
            "invalid discharge axis",
        )
        require(
            all(a < b for a, b in itertools.pairwise(values))
            or all(a > b for a, b in itertools.pairwise(values)),
            "discharge axis must be strictly monotonic",
        )
    else:
        raise ValueError("unsupported flow axis semantics")
    try:
        encoded = json.dumps(index, separators=(",", ":"), allow_nan=False).encode(
            "utf-8"
        )
    except (TypeError, ValueError, OverflowError, UnicodeError) as exc:
        raise ValueError("range index is not finite UTF-8 JSON") from exc
    require(len(encoded) <= MAX_INDEX_BYTES, "range index exceeds MAX_INDEX_BYTES")


def validate_range_index(index: dict) -> None:
    """Validate the closed EPSG:3857 range index without reading or mutating files.

    All malformed JSON-compatible values raise ValueError. Public metadata is
    caller-qualified; this does not establish privacy or hydraulic approval.
    """
    try:
        _validate_range_index(index)
    except (TypeError, KeyError, IndexError, OverflowError, UnicodeError) as exc:
        raise ValueError("malformed range index") from exc
