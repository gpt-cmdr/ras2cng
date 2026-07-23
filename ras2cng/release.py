"""Upgrade existing MapLibre releases with current ras2cng metadata."""

from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
from typing import Any, Iterator

import pandas as pd

from ras2cng.maplibre import _INTERNAL_COLUMNS, build_raw_result_renderer


def _raw_layers(manifest: dict[str, Any]) -> Iterator[dict[str, Any]]:
    if manifest.get("schema") == "rascommander.maplibre/v2":
        yield from (
            layer
            for layer in manifest.get("layers", {}).values()
            if layer.get("sourceKind") == "raw-hdf"
        )
        return
    for tileset in manifest.get("tilesets", []):
        for layer in tileset.get("layers", []):
            if layer.get("sourceKind") == "raw-hdf" or layer.get("groupId") == "ras-results":
                yield layer


def _provenance(layer: dict[str, Any]) -> dict[str, Any]:
    value = layer.get("provenance") or layer.get("rawResult") or {}
    return value if isinstance(value, dict) else {}


def _result_frame(archive_root: Path, provenance: dict[str, Any]) -> pd.DataFrame:
    relative = provenance.get("archiveParquet")
    if not relative:
        raise ValueError("Raw-result layer is missing archiveParquet provenance")
    frame = pd.read_parquet(archive_root / relative)
    archive_filter = provenance.get("archiveFilter") or {}
    if archive_filter.get("column"):
        frame = frame.loc[
            frame[archive_filter["column"]].astype(str) == str(archive_filter.get("value"))
        ]
    if provenance.get("profile") is not None and "profile" in frame.columns:
        frame = frame.loc[frame["profile"].astype(str) == str(provenance["profile"])]

    excluded = (
        set(_INTERNAL_COLUMNS)
        | {str(provenance.get("indexColumn") or "")}
        | set((provenance.get("joinColumns") or {}).values())
        | {str(archive_filter.get("column") or "")}
    )
    frame.attrs["raw_result_value_fields"] = [
        column
        for column in frame.columns
        if column not in excluded
        and pd.api.types.is_numeric_dtype(frame[column])
        and not pd.api.types.is_bool_dtype(frame[column])
    ]
    return frame


def enrich_vector_result_renderers(
    manifest: dict[str, Any],
    *,
    archive_root: Path,
    project_units: str | None = None,
) -> tuple[dict[str, Any], int]:
    """Add renderer and query metadata using existing archive result values."""

    result = copy.deepcopy(manifest)
    count = 0
    for layer in _raw_layers(result):
        provenance = _provenance(layer)
        frame = _result_frame(archive_root, provenance)
        renderer = build_raw_result_renderer(
            frame,
            str(provenance.get("variable") or layer.get("role") or layer.get("kind") or ""),
            str(provenance.get("geometryJoin") or ""),
            project_units,
        )
        if not renderer:
            continue
        layer["renderer"] = renderer
        query = layer.setdefault("query", {})
        query["valueField"] = renderer["valueField"]
        query["units"] = renderer["units"]
        query["fields"] = [
            {
                "field": field["field"],
                "name": field["name"],
                "units": field["units"],
            }
            for field in renderer["availableFields"]
        ]
        count += 1
    return result, count


def enrich_release(
    release_root: Path,
    output_root: Path,
    *,
    project_units: str | None = None,
    project_ids: set[str] | None = None,
) -> int:
    """Write changed manifests into a publication delta with release-relative paths."""

    total = 0
    selected = project_ids or set()
    for project_root in sorted((release_root / "projects").iterdir()):
        if not project_root.is_dir() or selected and project_root.name not in selected:
            continue
        manifest_path = project_root / "viewer" / "manifest.json"
        archive_root = project_root / "archive"
        if not manifest_path.is_file() or not archive_root.is_dir():
            continue
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        enriched, count = enrich_vector_result_renderers(
            manifest,
            archive_root=archive_root,
            project_units=project_units,
        )
        if not count:
            continue
        output = (
            output_root
            / "projects"
            / project_root.name
            / "viewer"
            / "manifest.json"
        )
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(enriched, indent=2) + "\n", encoding="utf-8")
        total += count
        print(f"{project_root.name}: {count} renderer(s)")
    return total


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--release-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--project-units", default="English")
    parser.add_argument("--project-id", action="append", default=[])
    args = parser.parse_args()
    total = enrich_release(
        args.release_root,
        args.output_root,
        project_units=args.project_units,
        project_ids=set(args.project_id),
    )
    print(f"Total renderers: {total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
