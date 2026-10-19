"""Regenerate the tiny range-index fixture through the real public functions.

Run from the repository root: ``uv run python tests/fixtures/ranges/generate.py``.
The UUID is random, so a rerun produces new bytes; the committed fixture is the
frozen EARLY contract instance and MANIFEST.json records its raw hashes.
"""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
from pathlib import Path

import numpy as np
import rasterio
from rasterio.transform import from_origin

from ras2cng.depth_stack import write_depth_stack
from ras2cng.depth_stack_ranges import copy_depth_stack_indexed, dumps_range_index

HERE = Path(__file__).resolve().parent
HEIGHT, WIDTH = 260, 270
LABELS = ["p03-low", "p01-mid", "p02-high"]
ORIGIN_X, ORIGIN_Y, PIXEL = -10855000.0, 3511600.0, 3.0
FORMULA = (
    "value(k,r,c) = float32((((7*r + 3*c + 11*k) % 41) - 10) * 0.25); then"
    " NaN where (r < 40 and c < 50) or (r + c + k) % 17 == 0; then 0.0 where"
    " 100 <= r < 110 and c < 20"
)


def expected_plane(k: int) -> np.ndarray:
    r, c = np.mgrid[0:HEIGHT, 0:WIDTH]
    plane = ((((7 * r + 3 * c + 11 * k) % 41) - 10) * 0.25).astype("f4")
    plane[((r < 40) & (c < 50)) | ((r + c + k) % 17 == 0)] = np.nan
    plane[100:110, :20] = 0.0
    return plane


def build(directory: Path) -> dict:
    with tempfile.TemporaryDirectory() as scratch:
        sources = []
        for k in range(len(LABELS)):
            path = Path(scratch) / f"profile{k}.tif"
            with rasterio.open(
                path,
                "w",
                driver="GTiff",
                width=WIDTH,
                height=HEIGHT,
                count=1,
                dtype="float32",
                crs="EPSG:3857",
                transform=from_origin(ORIGIN_X, ORIGIN_Y, PIXEL, PIXEL),
                nodata=float("nan"),
            ) as dst:
                dst.write(expected_plane(k), 1)
            sources.append(path)
        stack = write_depth_stack(
            sources,
            Path(scratch) / "stack.nc",
            profiles=LABELS,
            vertical_unit="ft",
            provenance={"fixture": "ras2cng-range-fixture-v1"},
            include_source_hashes=False,
        )
        index = copy_depth_stack_indexed(stack, directory / "tiny-depth-stack.nc")
    (directory / "tiny-depth-stack.ranges.json").write_text(
        dumps_range_index(index) + "\n", encoding="utf-8", newline="\n"
    )
    points = [
        (0, 0), (0, 269), (259, 0), (259, 269), (255, 255), (256, 256),
        (255, 256), (256, 255), (100, 5), (39, 49), (40, 50), (150, 150),
    ]  # fmt: skip
    samples = []
    for k in range(len(LABELS)):
        plane = expected_plane(k)
        for r, c in points:
            value = float(plane[r, c])
            samples.append(
                {"profile": k, "row": r, "col": c,
                 "value": None if np.isnan(value) else value}
            )  # fmt: skip
    expected = {
        "fixture": "tiny-depth-stack.nc",
        "formula": FORMULA,
        "profiles": LABELS,
        "axis_semantics": "profile_index",
        "units": "ft",
        "crs": "EPSG:3857",
        "width": WIDTH,
        "height": HEIGHT,
        "geotransform": [ORIGIN_X, PIXEL, 0.0, ORIGIN_Y, 0.0, -PIXEL],
        "chunk_shape": [1, 256, 256],
        "edge_chunks": "chunk rows/cols 1 hold 4 rows / 14 cols of valid data; stored chunks are full 256x256",
        "nan_count": [int(np.isnan(expected_plane(k)).sum()) for k in range(3)],
        "zero_count": [int((expected_plane(k) == 0).sum()) for k in range(3)],
        "negative_count": [int((expected_plane(k) < 0).sum()) for k in range(3)],
        "plane_sha256_float32le": [
            hashlib.sha256(expected_plane(k).astype("<f4").tobytes()).hexdigest()
            for k in range(3)
        ],
        "samples": samples,
    }
    (directory / "expected.json").write_text(
        json.dumps(expected, indent=1) + "\n", encoding="utf-8", newline="\n"
    )
    names = ["tiny-depth-stack.nc", "tiny-depth-stack.ranges.json", "expected.json"]
    manifest = {
        name: {
            "bytes": (directory / name).stat().st_size,
            "sha256": hashlib.sha256((directory / name).read_bytes()).hexdigest(),
        }
        for name in names
    }
    (directory / "MANIFEST.json").write_text(
        json.dumps(manifest, indent=1) + "\n", encoding="utf-8", newline="\n"
    )
    return manifest


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE
    print(json.dumps(build(target), indent=1))
