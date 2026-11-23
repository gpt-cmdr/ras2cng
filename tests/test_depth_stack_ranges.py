"""Real-function range index tests: fixture decode, copy route, rejections."""

import copy
import hashlib
import json
import shutil
import sys
import uuid
import zlib
from pathlib import Path

import numpy as np
import pytest
import rasterio
from netCDF4 import Dataset
from rasterio.transform import from_origin

from ras2cng import depth_stack_ranges as ranges
from ras2cng.depth_stack import write_depth_stack
from ras2cng.depth_stack_ranges import (
    copy_depth_stack_indexed,
    dumps_range_index,
    inspect_depth_stack_ranges,
    validate_range_index,
)

FIXTURE = Path(__file__).parent / "fixtures" / "ranges"
sys.path.insert(0, str(FIXTURE))
from generate import expected_plane


def decode_plane(raw: bytes, index: dict, profile: int) -> np.ndarray:
    """Independent reader: zlib inflate + unshuffle from raw byte ranges only."""
    _, rows, cols = index["chunks"]["grid"]
    _, cy, cx = index["variable"]["chunk_shape"]
    _, height, width = index["variable"]["shape"]
    plane = np.empty((height, width), "<f4")
    for r in range(rows):
        for c in range(cols):
            i = (profile * rows + r) * cols + c
            start, length = index["chunks"]["offsets"][i], index["chunks"]["lengths"][i]
            inflater = zlib.decompressobj()
            data = inflater.decompress(raw[start : start + length], cy * cx * 4 + 1)
            assert inflater.eof and len(data) == index["chunks"]["decoded_length"]
            block = np.frombuffer(data, "u1").reshape(4, -1).T.copy().view("<f4")
            block = block.reshape(cy, cx)
            h, w = min(cy, height - r * cy), min(cx, width - c * cx)
            plane[r * cy : r * cy + h, c * cx : c * cx + w] = block[:h, :w]
    return plane


def stack(tmp_path, name="stack.nc", *, planes=None, **kwargs):
    planes = planes if planes is not None else [expected_plane(k) for k in range(2)]
    sources = []
    for k, plane in enumerate(planes):
        path = tmp_path / f"{name}.{k}.tif"
        with rasterio.open(
            path, "w", driver="GTiff", width=plane.shape[1], height=plane.shape[0],
            count=1, dtype="float32", crs="EPSG:3857",
            transform=from_origin(-1000.0, 2000.0, 2.0, 2.0), nodata=float("nan"),
        ) as dst:  # fmt: skip
            dst.write(plane, 1)
        sources.append(path)
    kwargs.setdefault("include_source_hashes", False)
    return write_depth_stack(
        sources,
        tmp_path / name,
        profiles=kwargs.pop("profiles", [f"p{k}" for k in range(len(planes))]),
        vertical_unit="m",
        **kwargs,
    )


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def test_committed_fixture_matches_manifest_index_and_every_pixel():
    manifest = json.loads((FIXTURE / "MANIFEST.json").read_text())
    for name, entry in manifest.items():
        assert digest(FIXTURE / name) == entry["sha256"]
        assert (FIXTURE / name).stat().st_size == entry["bytes"]
    index = json.loads((FIXTURE / "tiny-depth-stack.ranges.json").read_text())
    assert inspect_depth_stack_ranges(FIXTURE / "tiny-depth-stack.nc") == index
    expected = json.loads((FIXTURE / "expected.json").read_text())
    raw = (FIXTURE / "tiny-depth-stack.nc").read_bytes()
    witness = index["object"]["witness"]
    start = witness["offset"]
    assert raw[start : start + 36] == index["object"]["object_id"].encode()
    assert index["chunks"]["grid"] == [3, 2, 2]
    assert index["profiles"] == {
        "labels": expected["profiles"],
        "axis_semantics": "profile_index",
        "flows": None,
        "flow_units": None,
    }
    for k in range(3):
        plane = decode_plane(raw, index, k)
        assert plane.tobytes() == expected_plane(k).tobytes()
        assert (
            hashlib.sha256(plane.tobytes()).hexdigest()
            == (expected["plane_sha256_float32le"][k])
        )
        assert int(np.isnan(plane).sum()) == expected["nan_count"][k] > 0
        assert int((plane == 0).sum()) == expected["zero_count"][k] >= 200
        assert int((plane < 0).sum()) == expected["negative_count"][k] > 0
    for sample in expected["samples"]:
        value = decode_plane(raw, index, sample["profile"])[
            sample["row"], sample["col"]
        ]
        assert (None if np.isnan(value) else float(value)) == sample["value"]
    with Dataset(FIXTURE / "tiny-depth-stack.nc") as nc:
        assert nc.schema == "ras2cng-depth-stack/v1"
        assert nc["profile"][:].tolist() == expected["profiles"]
        assert "source_sha256_json" not in nc.ncattrs()


def test_copy_preserves_all_pixels_source_and_never_overwrites(tmp_path):
    source = stack(tmp_path, provenance={"project": "public-slug"})
    before = digest(source)
    target = tmp_path / "out" / "indexed.nc"
    index = copy_depth_stack_indexed(source, target)
    assert digest(source) == before
    assert not list(target.parent.glob("*.partial"))
    assert index == inspect_depth_stack_ranges(target)
    assert index["object"]["byte_length"] == target.stat().st_size
    assert uuid.UUID(index["object"]["object_id"]).version == 4
    raw = target.read_bytes()
    with Dataset(source) as old, Dataset(target) as new:
        assert old.__dict__ == new.__dict__
        for name in ["flow", "profile", "x", "y"]:
            assert old[name][:].tolist() == new[name][:].tolist()
        assert old["crs"].__dict__ == new["crs"].__dict__
        for k in range(2):
            a = old["depth"][k].filled(np.nan).astype("<f4")
            b = new["depth"][k].filled(np.nan).astype("<f4")
            assert a.tobytes() == b.tobytes() == decode_plane(raw, index, k).tobytes()
        assert (
            "".join(new["range_object_id"][:].astype(str).tolist())
            == index["object"]["object_id"]
        )
    text = dumps_range_index(index)
    for forbidden in [str(tmp_path), before, "sha256", "\\\\", "C:"]:
        assert forbidden not in text and forbidden.encode() not in raw[-4096:]
    with pytest.raises(FileExistsError):
        copy_depth_stack_indexed(source, target)
    with pytest.raises(FileExistsError):
        copy_depth_stack_indexed(source, source)
    second = copy_depth_stack_indexed(source, tmp_path / "again.nc")
    assert second["object"]["object_id"] != index["object"]["object_id"]
    assert second["chunks"]["lengths"] == index["chunks"]["lengths"]
    assert digest(target) == hashlib.sha256(raw).hexdigest()


def test_discharge_axis_and_single_chunk(tmp_path):
    planes = [np.array([[0, -1.5], [np.nan, 2.25]], "f4")] * 2
    source = stack(tmp_path, planes=planes, flows=[10.0, 20.0], flow_units="cfs")
    index = copy_depth_stack_indexed(source, tmp_path / "q.nc")
    assert index["profiles"]["flows"] == [10.0, 20.0]
    assert index["profiles"]["flow_units"] == "cfs"
    assert index["chunks"]["grid"] == [2, 1, 1]
    assert index["variable"]["chunk_shape"] == [1, 2, 2]
    raw = (tmp_path / "q.nc").read_bytes()
    assert decode_plane(raw, index, 1).tobytes() == planes[1].tobytes()


def test_benchmark_shape_is_supported_when_available():
    benchmark = Path(__file__).resolve().parents[2] / "inputs" / "smallest-benchmark.nc"
    if not benchmark.exists():
        pytest.skip("private 900KB benchmark is not part of the repository")
    index = ranges._inspect(benchmark, indexed=False)
    assert index["chunks"]["grid"] == [86, 2, 2]
    assert index["profiles"]["axis_semantics"] == "profile_index"


def rebuild(source, target, **depth_overrides):
    """Copy a writer stack variable-by-variable with altered depth storage."""
    with Dataset(source) as old, Dataset(target, "w", format="NETCDF4") as new:
        new.setncatts(old.__dict__)
        for name, dim in old.dimensions.items():
            new.createDimension(name, len(dim))
        for name, var in old.variables.items():
            options = {}
            if name == "depth":
                options = {
                    "fill_value": np.float32(np.nan), "zlib": True, "complevel": 4,
                    "shuffle": True, "chunksizes": var.chunking(),
                }  # fmt: skip
                options.update(depth_overrides)
            out = new.createVariable(name, var.datatype, var.dimensions, **options)
            out.setncatts({k: v for k, v in var.__dict__.items() if k != "_FillValue"})
            out[:] = var[:]
    return target


def mutate(source, target, action):
    shutil.copyfile(source, target)
    with Dataset(target, "a") as nc:
        action(nc)
    return target


@pytest.mark.parametrize(
    "case,match",
    [
        ("fletcher32", "filter pipeline"),
        ("no_shuffle", "filter pipeline"),
        ("chunks", "chunking"),
        ("scale", "depth.*attributes"),
        ("grid", "centers disagree"),
        ("profile", "profile labels"),
        ("global", "global attributes"),
        ("crs", "EPSG:3857"),
        ("hashes", "private source identities"),
        ("indexed", "dimension declarations"),
    ],
)
def test_unsupported_sources_are_rejected_without_output(tmp_path, case, match):
    base = stack(tmp_path)
    bad = tmp_path / "bad.nc"
    if case == "fletcher32":
        rebuild(base, bad, fletcher32=True)
    elif case == "no_shuffle":
        rebuild(base, bad, shuffle=False)
    elif case == "chunks":
        rebuild(base, bad, chunksizes=(1, 128, 128))
    elif case == "scale":
        mutate(base, bad, lambda nc: nc["depth"].setncattr("scale_factor", 0.5))
    elif case == "grid":
        mutate(base, bad, lambda nc: nc["x"].__setitem__(0, 0.0))
    elif case == "profile":
        mutate(base, bad, lambda nc: nc["profile"].__setitem__(1, "p0"))
    elif case == "global":
        mutate(base, bad, lambda nc: nc.setncattr("extra", "x"))
    elif case == "crs":
        mutate(base, bad, lambda nc: nc["crs"].setncattr("epsg_code", "EPSG:4326"))
    elif case == "hashes":
        bad = stack(tmp_path, "hashed.nc", include_source_hashes=True)
    else:
        copy_depth_stack_indexed(base, bad)
    before = digest(bad)
    target = tmp_path / "never.nc"
    with pytest.raises(ValueError, match=match):
        copy_depth_stack_indexed(bad, target)
    assert not target.exists() and not list(tmp_path.glob("*.partial"))
    assert digest(bad) == before


def test_identity_corruption_is_rejected(tmp_path):
    target = tmp_path / "indexed.nc"
    index = copy_depth_stack_indexed(stack(tmp_path), target)
    data = bytearray(target.read_bytes())
    start = index["object"]["witness"]["offset"]
    data[start] = ord("0") if data[start] != ord("0") else ord("1")
    target.write_bytes(bytes(data))
    assert inspect_depth_stack_ranges(target)["object"] != index["object"]
    data[start] = ord("g")
    target.write_bytes(bytes(data))
    with pytest.raises(ValueError, match="UUID4"):
        inspect_depth_stack_ranges(target)


@pytest.mark.parametrize(
    "edit,match",
    [
        (lambda i: i.update(extra=1), "top-level"),
        (lambda i: i["object"].update(etag="x"), "object"),
        (lambda i: i["chunks"]["offsets"].__setitem__(0, 10**12), "outside"),
        (lambda i: i["chunks"].update(filter_mask=1), "layout"),
        (lambda i: i["filters"].reverse(), "filter"),
        (lambda i: i["filters"][1].update(level=0), "filter"),
        (lambda i: i["variable"].update(byteorder="big"), "variable"),
        (lambda i: i["object"].update(object_id="not-a-uuid"), "UUID4"),
        (lambda i: i["chunks"]["lengths"].pop(), "chunk arrays"),
    ],
)
def test_validate_rejects_corrupt_index(edit, match):
    index = json.loads((FIXTURE / "tiny-depth-stack.ranges.json").read_text())
    validate_range_index(copy.deepcopy(index))
    edit(index)
    with pytest.raises(ValueError, match=match):
        validate_range_index(index)


def test_index_byte_cap(monkeypatch):
    index = json.loads((FIXTURE / "tiny-depth-stack.ranges.json").read_text())
    monkeypatch.setattr(ranges, "MAX_INDEX_BYTES", 512)
    with pytest.raises(ValueError, match="MAX_INDEX_BYTES"):
        dumps_range_index(index)


def test_missing_required_attribute_is_value_error(tmp_path):
    bad = mutate(
        stack(tmp_path), tmp_path / "bad.nc", lambda nc: nc["flow"].delncattr("units")
    )
    with pytest.raises(ValueError, match="flow.*attributes"):
        copy_depth_stack_indexed(bad, tmp_path / "never.nc")
    assert not (tmp_path / "never.nc").exists()


@pytest.mark.parametrize(
    "edit",
    [
        lambda i: i["chunks"]["offsets"].__setitem__(1, i["chunks"]["offsets"][0]),
        lambda i: i["object"]["witness"].update(offset=i["chunks"]["offsets"][0]),
        lambda i: i["variable"].update(name="wsel"),
        lambda i: i["variable"].update(dimensions=["flow", "x", "y"]),
        lambda i: i["variable"].update(fill_value=0),
        lambda i: i["variable"].update(units="ftUS"),
        lambda i: i["grid"].update(crs="EPSG:4326"),
        lambda i: i["grid"].update(x_first_center=0),
        lambda i: i["grid"]["geotransform"].__setitem__(1, float("nan")),
        lambda i: i["profiles"].update(axis_semantics="unknown"),
        lambda i: i["profiles"].update(flows=[1, 2, 3]),
        lambda i: i["profiles"]["labels"].__setitem__(0, i["profiles"]["labels"][1]),
        lambda i: i["profiles"]["labels"].__setitem__(0, "x" * 257),
        lambda i: i["profiles"]["labels"].__setitem__(0, "\ud800"),
        lambda i: i["variable"].update(shape=[True, 260, 270]),
        lambda i: i["variable"].update(shape=[3, 0, 270]),
        lambda i: i["variable"].update(shape=[3, 32769, 270]),
        lambda i: i["variable"].update(shape=[3]),
        lambda i: i["chunks"].update(decoded_length=True),
        lambda i: i["chunks"].update(offsets=None),
        lambda i: i["filters"].clear(),
        lambda i: i["filters"][1].update(level=True),
        lambda i: i["object"].update(byte_length=True),
        lambda i: i["object"]["witness"].update(offset=0.5),
    ],
)
def test_main_malformed_and_binding_regressions(edit):
    index = json.loads((FIXTURE / "tiny-depth-stack.ranges.json").read_text())
    edit(index)
    with pytest.raises(ValueError):
        validate_range_index(index)


@pytest.mark.parametrize(
    "variable,key,value",
    [
        ("flow", "private_note", "internal-source"),
        ("profile", "private_note", "internal-source"),
        ("x", "private_note", "internal-source"),
        ("y", "units", "ft"),
        ("crs", "private_note", "internal-source"),
        ("crs", "crs_wkt", "EPSG:4326"),
        ("crs", "spatial_ref", "EPSG:4326"),
        ("crs", "earth_radius", 1.0),
        ("depth", "long_name", "private-source"),
    ],
)
def test_main_metadata_tamper_rejected_without_source_mutation(
    tmp_path, variable, key, value
):
    source = stack(tmp_path)
    bad = mutate(
        source, tmp_path / "bad.nc", lambda nc: nc[variable].setncattr(key, value)
    )
    before = digest(bad)
    with pytest.raises(ValueError):
        copy_depth_stack_indexed(bad, tmp_path / "never.nc")
    assert digest(bad) == before
    assert not (tmp_path / "never.nc").exists()
