"""Build a compact MapLibre project bundle from a ras2cng archive.

The archive remains the authoritative, queryable delivery format. This module
creates a browser delivery companion: one PMTiles archive for geometry and,
when requested, one PMTiles archive for raw HDF result values joined to their
source model elements. It intentionally does not rasterize results; stored-map
COGs are a separate, explicit publication step.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
import subprocess
import tempfile
from typing import Any, Iterable, Mapping, Sequence

import geopandas as gpd
import pandas as pd
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
import shapely

from ras2cng.pmtiles import _require_cli


MAPLIBRE_SCHEMA = "rascommander.maplibre.project/1"
_INTERNAL_COLUMNS = {
    "layer",
    "bbox_xmin",
    "bbox_ymin",
    "bbox_xmax",
    "bbox_ymax",
    "hilbert_index",
    "join_index",
}

_DELIVERY_ATTRIBUTE_COLUMNS = {
    "mesh_areas": ("mesh_name", "Name", "SA-2D"),
    "mesh_cells": ("mesh_name", "cell_id"),
    "mesh_faces": ("mesh_name", "face_id"),
    "bc_lines": ("Name", "SA-2D", "bc_line_id"),
    "breaklines": ("Name", "bl_id"),
    "refinement_regions": ("Name", "rr_id"),
    "reference_lines": ("Name", "refln_id"),
    "reference_points": ("Name",),
    "storage_areas": ("Name",),
    "centerlines": ("River", "Reach"),
    "cross_sections": ("River", "Reach", "RS"),
    "structures": ("Name", "Type", "Connection", "SA-2D", "River", "Reach", "RS"),
}

_GEOMETRY_LABELS = {
    "model_extents": "Model Extents",
    "mesh_areas": "2D Flow Areas",
    "mesh_cells": "2D Mesh Cells",
    "mesh_faces": "2D Mesh Faces",
    "centerlines": "River Centerlines",
    "cross_sections": "Cross Sections",
    "structures": "Hydraulic Structures",
    "bc_lines": "Boundary Conditions",
    "breaklines": "Breaklines",
    "refinement_regions": "Refinement Regions",
    "reference_lines": "Reference Lines",
    "reference_points": "Reference Points",
    "storage_areas": "Storage Areas",
}

_GEOMETRY_STYLES = {
    "model_extents": {"fill": "#f59e0b", "fillOpacity": 0.08, "line": "#ea580c", "lineWidth": 2.0},
    "mesh_areas": {"fill": "#60a5fa", "fillOpacity": 0.10, "line": "#1d4ed8", "lineWidth": 1.0},
    "mesh_cells": {"fill": "#93c5fd", "fillOpacity": 0.14, "line": "#2563eb", "lineWidth": 0.35, "minzoom": 13},
    "mesh_faces": {"fill": "#2563eb", "fillOpacity": 0.0, "line": "#2563eb", "lineWidth": 0.65, "minzoom": 14},
    "centerlines": {"fill": "#0f766e", "fillOpacity": 0.0, "line": "#0f766e", "lineWidth": 1.2},
    "cross_sections": {"fill": "#0f766e", "fillOpacity": 0.0, "line": "#0f766e", "lineWidth": 1.0},
    "structures": {"fill": "#dc2626", "fillOpacity": 0.0, "line": "#dc2626", "lineWidth": 1.4},
    "bc_lines": {"fill": "#7c3aed", "fillOpacity": 0.0, "line": "#7c3aed", "lineWidth": 1.0},
    "breaklines": {"fill": "#a16207", "fillOpacity": 0.0, "line": "#a16207", "lineWidth": 1.0},
    "refinement_regions": {"fill": "#c4b5fd", "fillOpacity": 0.12, "line": "#7c3aed", "lineWidth": 1.0},
    "reference_lines": {"fill": "#0f766e", "fillOpacity": 0.0, "line": "#0f766e", "lineWidth": 1.0},
    "reference_points": {"fill": "#facc15", "fillOpacity": 0.55, "line": "#a16207", "lineWidth": 0.75},
    "storage_areas": {"fill": "#38bdf8", "fillOpacity": 0.10, "line": "#0284c7", "lineWidth": 1.0},
}

_RESULT_STYLES = {
    "depth": {"fill": "#2563eb", "fillOpacity": 0.42, "line": "#1d4ed8", "lineWidth": 0.35},
    "water_surface": {"fill": "#0f766e", "fillOpacity": 0.40, "line": "#0f766e", "lineWidth": 0.35},
    "velocity": {"fill": "#ea580c", "fillOpacity": 0.42, "line": "#c2410c", "lineWidth": 0.35},
}


@dataclass(frozen=True)
class PackageSummary:
    """Files and counts produced by :func:`package_maplibre_viewer`."""

    manifest_path: Path
    geometry_pmtiles: Path
    result_pmtiles: Path | None
    geometry_layer_count: int
    result_layer_count: int
    bounds: tuple[float, float, float, float]


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def _display_name(value: str) -> str:
    return _GEOMETRY_LABELS.get(value, value.replace("_", " ").title())


def _bounds(gdf: gpd.GeoDataFrame) -> list[float]:
    minx, miny, maxx, maxy = gdf.total_bounds
    return [float(minx), float(miny), float(maxx), float(maxy)]


def _merge_bounds(bounds: Iterable[Sequence[float]]) -> tuple[float, float, float, float]:
    values = list(bounds)
    if not values:
        raise ValueError("No spatial features were available for the MapLibre bundle.")
    return (
        min(float(item[0]) for item in values),
        min(float(item[1]) for item in values),
        max(float(item[2]) for item in values),
        max(float(item[3]) for item in values),
    )


def _default_zoom(bounds: Sequence[float]) -> int:
    span = max(float(bounds[2]) - float(bounds[0]), float(bounds[3]) - float(bounds[1]))
    if span <= 0.005:
        return 14
    if span <= 0.025:
        return 12
    if span <= 0.08:
        return 11
    if span <= 0.25:
        return 10
    return 8


def _read_layer(path: Path, filter_value: str) -> gpd.GeoDataFrame:
    """Read one logical layer without expanding the whole project archive."""

    try:
        gdf = gpd.read_parquet(path, filters=[("layer", "==", filter_value)])
    except TypeError:
        # GeoPandas before 0.14 did not forward Arrow predicate filters.
        gdf = gpd.read_parquet(path)
        if "layer" in gdf.columns:
            gdf = gdf[gdf["layer"] == filter_value]
    return gdf


def _to_wgs84(
    gdf: gpd.GeoDataFrame,
    source: Path,
    fallback_crs: str | None = None,
) -> gpd.GeoDataFrame:
    if gdf.crs is None:
        if not fallback_crs:
            raise ValueError(f"GeoParquet layer has no CRS and cannot be published: {source}")
        gdf = gdf.set_crs(fallback_crs)
    gdf = gdf[gdf.geometry.notna() & ~gdf.geometry.is_empty].copy()
    if gdf.empty:
        return gdf
    # HEC-RAS sometimes writes a NaN Z ordinate on otherwise valid polygons.
    # GeoParquet preserves that source value, but GEOS cannot reproject an
    # unclosed 3D ring whose closing NaN does not compare equal. Browser tiles
    # are 2D by definition, so drop Z only in this delivery copy.
    gdf.geometry = shapely.force_2d(gdf.geometry.values)
    return gdf.to_crs("EPSG:4326")


def _drop_internal_columns(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    return gdf.drop(columns=[name for name in _INTERNAL_COLUMNS if name in gdf.columns])


def _write_ndgeojson(gdf: gpd.GeoDataFrame, path: Path) -> tuple[int, list[str], list[float]]:
    path.parent.mkdir(parents=True, exist_ok=True)
    cleaned = _drop_internal_columns(gdf)
    with path.open("w", encoding="utf-8") as handle:
        for feature in cleaned.iterfeatures(drop_id=True, na="null", show_bbox=False):
            json.dump(feature, handle, default=str, separators=(",", ":"))
            handle.write("\n")
    return len(cleaned), sorted(set(cleaned.geom_type.dropna())), _bounds(cleaned)


def _stream_dense_layer_ndgeojson(
    source: Path,
    filter_value: str,
    kind: str,
    path: Path,
    fallback_crs: str | None,
    batch_size: int = 20_000,
) -> tuple[int, list[str], list[float]]:
    """Write a dense delivery layer without loading its full mesh into memory."""

    parquet = pq.ParquetFile(source)
    available_columns = set(parquet.schema_arrow.names)
    columns = ["geometry", "layer"]
    columns.extend(
        column
        for column in _DELIVERY_ATTRIBUTE_COLUMNS.get(kind, ())
        if column in available_columns
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    geometry_types: set[str] = set()
    bounds: list[Sequence[float]] = []

    with path.open("w", encoding="utf-8") as handle:
        for batch in parquet.iter_batches(batch_size=batch_size, columns=columns):
            table = pa.Table.from_batches([batch]).filter(
                pc.equal(batch.column(batch.schema.get_field_index("layer")), filter_value)
            )
            if not table.num_rows:
                continue
            gdf = _to_wgs84(gpd.GeoDataFrame.from_arrow(table), source, fallback_crs)
            if gdf.empty:
                continue
            cleaned = _drop_internal_columns(gdf)
            # Only emit a property in batches where the source layer has a value.
            cleaned = cleaned.drop(
                columns=[
                    column
                    for column in cleaned.columns
                    if column != cleaned.geometry.name and not cleaned[column].notna().any()
                ]
            )
            for feature in cleaned.iterfeatures(drop_id=True, na="null", show_bbox=False):
                json.dump(feature, handle, default=str, separators=(",", ":"))
                handle.write("\n")
            count += len(cleaned)
            geometry_types.update(cleaned.geom_type.dropna())
            bounds.append(_bounds(cleaned))

    if not count:
        raise ValueError(f"No {kind} features were written from {source}")
    return count, sorted(geometry_types), _merge_bounds(bounds)


def _run_tippecanoe(
    output: Path,
    layers: Sequence[tuple[str, Path]],
    min_zoom: int,
    max_zoom: int,
    temporary_directory: Path | None = None,
) -> None:
    if not layers:
        raise ValueError("Tippecanoe needs at least one non-empty source layer.")
    output.parent.mkdir(parents=True, exist_ok=True)
    command = [
        "tippecanoe",
        "--force",
        "--read-parallel",
        "--no-tile-size-limit",
        "--no-feature-limit",
        "--extend-zooms-if-still-dropping",
        f"--minimum-zoom={min_zoom}",
        f"--maximum-zoom={max_zoom}",
        "--output",
        str(output),
    ]
    if temporary_directory is not None:
        temporary_directory.mkdir(parents=True, exist_ok=True)
        command.extend(["--temporary-directory", str(temporary_directory)])
    for source_layer, source_path in layers:
        command.extend(["-L", f"{source_layer}:{source_path}"])
    subprocess.run(command, check=True, capture_output=True, text=True)


def _extent_from_hdf(hdf_path: Path, fallback_crs: str | None = None) -> gpd.GeoDataFrame:
    """Return the authoritative no-buffer footprint from ras-commander."""

    from ras_commander.hdf import HdfProject

    try:
        footprint, _ = HdfProject.get_project_extent(
            hdf_path,
            geometry_type="footprint",
            buffer_percent=0,
        )
    except TypeError as error:
        raise RuntimeError(
            "MapLibre packaging requires the ras-commander footprint API "
            "(HdfProject.get_project_extent(..., geometry_type='footprint')). "
            "Install ras-commander from current main before packaging."
        ) from error
    if footprint.empty:
        raise ValueError(f"No model footprint was returned for {hdf_path}")
    return _to_wgs84(footprint, hdf_path, fallback_crs)


def _result_style(variable: str) -> dict[str, float | str]:
    lowered = variable.lower()
    for key, style in _RESULT_STYLES.items():
        if key in lowered:
            return style.copy()
    return {"fill": "#64748b", "fillOpacity": 0.42, "line": "#475569", "lineWidth": 0.35}


def _join_raw_result(
    result_path: Path,
    geometry: gpd.GeoDataFrame,
    index_column: str = "",
    *,
    join_columns: Mapping[str, str] | None = None,
    filters: Mapping[str, Any] | None = None,
) -> gpd.GeoDataFrame:
    """Join raw HDF values to the feature geometry solely for vector delivery."""

    result = pd.read_parquet(result_path)
    if filters:
        for column, value in filters.items():
            if column not in result.columns:
                raise ValueError(f"Cannot filter raw results from {result_path}: '{column}' is absent.")
            result = result.loc[result[column] == value]
    attributes = result.drop(columns=["geometry"], errors="ignore")
    if index_column:
        if index_column not in attributes.columns or index_column not in geometry.columns:
            raise ValueError(
                f"Cannot join raw results from {result_path}: index '{index_column}' "
                "is not present in both the results and geometry tables."
            )
        joined = geometry.merge(attributes, on=index_column, how="inner", suffixes=("", "_result"))
    elif join_columns:
        geometry_columns = list(join_columns)
        result_columns = list(join_columns.values())
        missing_geometry = [column for column in geometry_columns if column not in geometry.columns]
        missing_result = [column for column in result_columns if column not in attributes.columns]
        if missing_geometry or missing_result:
            raise ValueError(
                f"Cannot join raw results from {result_path}: missing geometry columns "
                f"{missing_geometry} or result columns {missing_result}."
            )
        if attributes.duplicated(subset=result_columns).any():
            raise ValueError(
                f"Cannot join raw results from {result_path}: composite result keys are not unique."
            )
        delivery_geometry = geometry.copy()
        delivery_attributes = attributes.copy()
        for geometry_column, result_column in join_columns.items():
            delivery_geometry[geometry_column] = delivery_geometry[geometry_column].astype("string").str.strip()
            delivery_attributes[result_column] = delivery_attributes[result_column].astype("string").str.strip()
        joined = delivery_geometry.merge(
            delivery_attributes,
            left_on=geometry_columns,
            right_on=result_columns,
            how="inner",
            suffixes=("", "_result"),
        )
        joined = joined.drop(columns=result_columns, errors="ignore")
    else:
        raise ValueError(f"Cannot join raw results from {result_path}: no join key was declared.")
    return gpd.GeoDataFrame(joined, geometry="geometry", crs=geometry.crs)


def _project_metadata(archive_dir: Path) -> dict[str, Any]:
    project_path = archive_dir.parent / "project.json"
    if not project_path.is_file():
        return {}
    return json.loads(project_path.read_text(encoding="utf-8"))


def package_maplibre_viewer(
    archive_dir: Path,
    output_dir: Path,
    *,
    geometry_hdfs: Mapping[str, Path],
    title: str | None = None,
    source_project: str | None = None,
    crs: str | None = None,
    include_vector_results: bool = False,
    min_zoom: int = 0,
    max_zoom: int = 17,
    scratch_dir: Path | None = None,
) -> PackageSummary:
    """Create a MapLibre viewer bundle from a completed ras2cng archive.

    ``geometry_hdfs`` maps archive IDs such as ``g01`` to their original HDF
    geometry files. Requiring that mapping ensures every model footprint in the
    browser bundle is produced by ``HdfProject.get_project_extent`` rather than
    approximated from delivery tiles.
    """

    archive_dir = Path(archive_dir)
    output_dir = Path(output_dir)
    archive_manifest_path = archive_dir / "manifest.json"
    if not archive_manifest_path.is_file():
        raise FileNotFoundError(f"ras2cng archive manifest not found: {archive_manifest_path}")
    if output_dir.exists() and any(output_dir.iterdir()):
        raise FileExistsError(f"MapLibre output directory is not empty: {output_dir}")

    archive = json.loads(archive_manifest_path.read_text(encoding="utf-8"))
    geometry_entries = archive.get("geometry", [])
    if not geometry_entries:
        raise ValueError(f"Archive has no geometry entries: {archive_manifest_path}")

    missing_hdfs = [entry["geom_id"] for entry in geometry_entries if entry["geom_id"] not in geometry_hdfs]
    if missing_hdfs:
        raise ValueError(
            "Missing geometry HDF mapping(s) required for API-derived footprints: "
            + ", ".join(missing_hdfs)
        )
    for geom_id, hdf_path in geometry_hdfs.items():
        if not Path(hdf_path).is_file():
            raise FileNotFoundError(f"Geometry HDF for {geom_id} does not exist: {hdf_path}")

    metadata = _project_metadata(archive_dir)
    project_crs = crs or metadata.get("crs") or archive.get("project", {}).get("crs")
    _require_cli("tippecanoe")
    output_dir.mkdir(parents=True, exist_ok=True)
    tiles_dir = output_dir / "tiles"
    viewer_title = title or metadata.get("title") or archive.get("project", {}).get("name") or archive_dir.name
    source_project = source_project or metadata.get("href") or "../project.json"
    if scratch_dir is not None:
        scratch_dir = Path(scratch_dir).resolve()
        scratch_dir.mkdir(parents=True, exist_ok=True)
        if not scratch_dir.is_dir():
            raise ValueError(f"MapLibre scratch directory is not a directory: {scratch_dir}")

    geometry_cache: dict[tuple[str, str], gpd.GeoDataFrame] = {}
    result_geometry_keys = {
        (str(plan.get("geom_id", "")).lower(), str(variable["geometry_filter"]))
        for plan in archive.get("results", [])
        for variable in plan.get("variables", [])
        if include_vector_results and variable.get("geometry_filter")
    }
    geometry_overview_sources: list[tuple[str, Path]] = []
    geometry_detail_sources: list[tuple[str, Path]] = []
    geometry_overview_layers: list[dict[str, Any]] = []
    geometry_detail_layers: list[dict[str, Any]] = []
    geometry_layers: list[dict[str, Any]] = []
    groups: list[dict[str, Any]] = []
    extent_features: list[dict[str, Any]] = []
    all_bounds: list[Sequence[float]] = []

    with tempfile.TemporaryDirectory(
        prefix="ras2cng-maplibre-",
        dir=str(scratch_dir) if scratch_dir is not None else None,
    ) as temporary:
        work_dir = Path(temporary)
        for geometry_index, entry in enumerate(geometry_entries):
            geom_id = entry["geom_id"].lower()
            group_id = f"ras-geometry-{geom_id}"
            group_layers: list[dict[str, Any]] = []
            groups.append(
                {
                    "id": group_id,
                    "name": f"Geometry {geom_id}",
                    "visible": geometry_index == 0,
                }
            )

            extent = _extent_from_hdf(Path(geometry_hdfs[entry["geom_id"]]), project_crs)
            extent["geometry_id"] = geom_id
            extent_source = f"{group_id}-model-extents"
            extent_path = work_dir / "geometry" / f"{extent_source}.ndgeojson"
            count, geometry_types, bounds = _write_ndgeojson(extent, extent_path)
            geometry_overview_sources.append((extent_source, extent_path))
            all_bounds.append(bounds)
            group_layers.append(
                {
                    "id": extent_source,
                    "name": _display_name("model_extents"),
                    "sourceLayer": extent_source,
                    "groupId": group_id,
                    "visible": False,
                    "kind": "model_extents",
                    "style": _GEOMETRY_STYLES["model_extents"].copy(),
                    "featureCount": count,
                    "geometryTypes": geometry_types,
                    "bounds": bounds,
                    "sort": 0,
                    "queryable": True,
                    "extentSource": "HdfProject.get_project_extent(geometry_type='footprint')",
                }
            )
            extent_features.extend(extent.iterfeatures(drop_id=True, na="null", show_bbox=False))

            archive_geometry_path = archive_dir / entry["parquet"]
            for layer in entry.get("layers", []):
                kind = layer.get("layer") or layer.get("filter_value")
                filter_value = layer.get("filter_value") or kind
                if not kind or not filter_value:
                    continue
                source_layer = f"{group_id}-{_slug(kind)}"
                source_path = work_dir / "geometry" / f"{source_layer}.ndgeojson"
                cache_geometry = (geom_id, kind) in result_geometry_keys
                gdf: gpd.GeoDataFrame | None = None
                if _is_detail_geometry(kind) and not cache_geometry:
                    count, geometry_types, bounds = _stream_dense_layer_ndgeojson(
                        archive_geometry_path,
                        filter_value,
                        kind,
                        source_path,
                        project_crs,
                    )
                else:
                    gdf = _to_wgs84(
                        _read_layer(archive_geometry_path, filter_value),
                        archive_geometry_path,
                        project_crs,
                    )
                    if gdf.empty:
                        continue
                    if cache_geometry:
                        geometry_cache[(geom_id, kind)] = gdf
                    count, geometry_types, bounds = _write_ndgeojson(gdf, source_path)
                source_layers = (
                    geometry_detail_sources if _is_detail_geometry(kind) else geometry_overview_sources
                )
                source_layers.append((source_layer, source_path))
                all_bounds.append(bounds)
                group_layers.append(
                    {
                        "id": source_layer,
                        "name": _display_name(kind),
                        "sourceLayer": source_layer,
                        "groupId": group_id,
                        "visible": False,
                        "kind": kind,
                        "style": _GEOMETRY_STYLES.get(
                            kind,
                            {"fill": "#94a3b8", "fillOpacity": 0.12, "line": "#475569", "lineWidth": 1.0},
                        ).copy(),
                        "featureCount": count,
                        "geometryTypes": geometry_types,
                        "bounds": bounds,
                        "sort": _geometry_sort(kind),
                        "queryable": True,
                    }
                )
                if gdf is not None and not cache_geometry:
                    del gdf

            geometry_layers.extend(group_layers)
            for layer in group_layers:
                target_layers = (
                    geometry_detail_layers
                    if _is_detail_geometry(layer["kind"])
                    else geometry_overview_layers
                )
                target_layers.append(layer)

        geometry_pmtiles = tiles_dir / "geometry.pmtiles"
        _run_tippecanoe(
            geometry_pmtiles,
            geometry_overview_sources,
            min_zoom,
            max_zoom,
            work_dir / "tippecanoe-overview",
        )
        geometry_detail_pmtiles: Path | None = None
        if geometry_detail_sources:
            geometry_detail_pmtiles = tiles_dir / "geometry-detail.pmtiles"
            _run_tippecanoe(
                geometry_detail_pmtiles,
                geometry_detail_sources,
                max(min_zoom, 13),
                max_zoom,
                work_dir / "tippecanoe-detail",
            )

        result_sources: list[tuple[str, Path]] = []
        result_layers: list[dict[str, Any]] = []
        if include_vector_results:
            for plan in archive.get("results", []):
                plan_id = str(plan.get("plan_id", "plan")).lower()
                result_group_id = f"ras-results-{plan_id}"
                plan_layers: list[dict[str, Any]] = []
                for variable in plan.get("variables", []):
                    variable_path = variable.get("parquet")
                    geometry_kind = variable.get("geometry_filter")
                    index_column = str(variable.get("index_column") or "")
                    join_columns = variable.get("join_columns") or {}
                    geom_id = str(plan.get("geom_id", "")).lower()
                    if not variable_path or not geometry_kind or not (index_column or join_columns):
                        continue
                    geometry = geometry_cache.get((geom_id, geometry_kind))
                    if geometry is None:
                        continue
                    raw_path = archive_dir / variable_path
                    variable_name = variable.get("variable") or variable.get("filter_value") or raw_path.stem
                    profile_column = str(variable.get("profile_column") or "")
                    profiles: list[Any] = [None]
                    if profile_column:
                        profile_values = pd.read_parquet(raw_path, columns=[profile_column])[profile_column]
                        profiles = list(pd.unique(profile_values.dropna()))
                    for profile_index, profile in enumerate(profiles):
                        filters = {profile_column: profile} if profile_column else None
                        joined = _join_raw_result(
                            raw_path,
                            geometry,
                            index_column,
                            join_columns=join_columns,
                            filters=filters,
                        )
                        if joined.empty:
                            continue
                        profile_suffix = f"-{_slug(str(profile))}" if profile is not None else ""
                        source_layer = f"{result_group_id}-{_slug(variable_name)}{profile_suffix}"
                        source_path = work_dir / "results" / f"{source_layer}.ndgeojson"
                        count, geometry_types, bounds = _write_ndgeojson(joined, source_path)
                        result_sources.append((source_layer, source_path))
                        all_bounds.append(bounds)
                        layer_name = _display_name(variable_name)
                        if profile is not None:
                            layer_name = f"{layer_name} - {profile}"
                        raw_result = {
                            "source": variable.get("source") or "Raw HEC-RAS HDF summary result values",
                            "plan": plan_id,
                            "variable": variable_name,
                            "geometryJoin": geometry_kind,
                            "archiveParquet": variable_path,
                        }
                        if index_column:
                            raw_result["indexColumn"] = index_column
                        if join_columns:
                            raw_result["joinColumns"] = join_columns
                        if profile is not None:
                            raw_result["profile"] = profile
                        plan_layers.append(
                            {
                                "id": source_layer,
                                "name": layer_name,
                                "sourceLayer": source_layer,
                                "groupId": result_group_id,
                                "visible": False,
                                "kind": f"{plan_id}_{variable_name}{profile_suffix}",
                                "style": _result_style(variable_name),
                                "featureCount": count,
                                "geometryTypes": geometry_types,
                                "bounds": bounds,
                                "sort": 100 + profile_index,
                                "queryable": True,
                                "rawResult": raw_result,
                            }
                        )
                if plan_layers:
                    groups.append(
                        {
                            "id": result_group_id,
                            "name": f"Vector Results {plan_id}",
                            "visible": False,
                            "resultKind": "raw_hdf",
                        }
                    )
                    result_layers.extend(plan_layers)

        result_pmtiles: Path | None = None
        if result_sources:
            result_pmtiles = tiles_dir / "results.pmtiles"
            _run_tippecanoe(
                result_pmtiles,
                result_sources,
                min_zoom,
                max_zoom,
                work_dir / "tippecanoe-results",
            )

    final_bounds = _merge_bounds(all_bounds)
    center = [
        (final_bounds[0] + final_bounds[2]) / 2.0,
        (final_bounds[1] + final_bounds[3]) / 2.0,
    ]
    geometry_tilesets: list[dict[str, Any]] = [
        {
            "id": "geometry",
            "type": "vector",
            "href": "tiles/geometry.pmtiles",
            "bytes": geometry_pmtiles.stat().st_size,
            "layers": geometry_overview_layers,
        }
    ]
    if geometry_detail_pmtiles:
        geometry_tilesets.append(
            {
                "id": "geometry-detail",
                "type": "vector",
                "href": "tiles/geometry-detail.pmtiles",
                "bytes": geometry_detail_pmtiles.stat().st_size,
                "layers": geometry_detail_layers,
                "minzoom": max(min_zoom, 13),
            }
        )

    manifest: dict[str, Any] = {
        "schema": MAPLIBRE_SCHEMA,
        "generatedBy": "ras2cng maplibre",
        "sourceProject": source_project,
        "title": viewer_title,
        "bounds": list(final_bounds),
        "center": center,
        "zoom": _default_zoom(final_bounds),
        "sourceCrs": project_crs,
        "tilesets": geometry_tilesets,
        "groups": groups,
        "notes": (
            "Geometry is delivered as PMTiles. Vector Results are raw HEC-RAS "
            "HDF summary values joined to their source geometry for display; "
            "they are not RASMapper-interpolated raster results."
        ),
    }
    apply_maplibre_default_visibility(manifest)
    if result_pmtiles:
        manifest["tilesets"].append(
            {
                "id": "results",
                "type": "vector",
                "href": "tiles/results.pmtiles",
                "bytes": result_pmtiles.stat().st_size,
                "layers": result_layers,
                "resultKind": "raw_hdf",
            }
        )

    (output_dir / "model_extent.geojson").write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "name": f"{_slug(viewer_title)}-model-extents",
                "features": extent_features,
            },
            indent=2,
            default=str,
        )
        + "\n",
        encoding="utf-8",
    )
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return PackageSummary(
        manifest_path=manifest_path,
        geometry_pmtiles=geometry_pmtiles,
        result_pmtiles=result_pmtiles,
        geometry_layer_count=len(geometry_layers),
        result_layer_count=len(result_layers),
        bounds=final_bounds,
    )


def _geometry_sort(kind: str) -> int:
    order = {
        "mesh_areas": 10,
        "mesh_cells": 20,
        "mesh_faces": 30,
        "breaklines": 40,
        "centerlines": 50,
        "structures": 60,
        "cross_sections": 70,
        "bc_lines": 80,
    }
    return order.get(kind, 90)


def _is_detail_geometry(kind: str) -> bool:
    """Dense mesh delivery belongs in a high-zoom source, never the overview."""

    return kind in {"mesh_cells", "mesh_faces"}


def apply_maplibre_default_visibility(
    manifest: dict[str, Any],
    *,
    primary_geometry_group_id: str | None = None,
) -> None:
    """Apply the standard initial geometry view to a MapLibre manifest.

    The viewer should begin with one geometry configuration, its authoritative
    model-limit footprint, and enough model context to orient a reviewer. A
    1D geometry uses centerlines; a 2D geometry uses mesh context plus mesh
    refinement controls when they are present. Dense faces, cross sections,
    boundary conditions, and structures remain opt-in.
    """

    geometry_groups: dict[str, list[dict[str, Any]]] = {}
    for tileset in manifest.get("tilesets", []):
        if tileset.get("type") != "vector":
            continue
        for layer in tileset.get("layers", []):
            group_id = str(layer.get("groupId") or "")
            if group_id.startswith("ras-geometry-"):
                geometry_groups.setdefault(group_id, []).append(layer)

    if not geometry_groups:
        return

    if primary_geometry_group_id not in geometry_groups:
        configured_groups = [
            str(group.get("id"))
            for group in manifest.get("groups", [])
            if group.get("visible") and str(group.get("id")) in geometry_groups
        ]
        primary_geometry_group_id = configured_groups[0] if configured_groups else next(iter(geometry_groups))

    for group_id, layers in geometry_groups.items():
        for layer in layers:
            layer["visible"] = False
        for group in manifest.get("groups", []):
            if group.get("id") == group_id:
                group["visible"] = group_id == primary_geometry_group_id

    primary_layers = geometry_groups[primary_geometry_group_id]
    kinds = {str(layer.get("kind") or "") for layer in primary_layers}
    is_2d = bool({"mesh_areas", "mesh_cells", "mesh_faces", "breaklines", "refinement_regions"} & kinds)
    default_kinds = {"model_extents"}
    if is_2d:
        default_kinds.update({"mesh_areas", "mesh_cells", "breaklines", "refinement_regions"})
    else:
        default_kinds.update({"centerlines", "river_centerlines"})

    for layer in primary_layers:
        if layer.get("kind") in default_kinds:
            layer["visible"] = True
