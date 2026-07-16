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
import os
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
from ras2cng.viewer_manifest import apply_manifest_v2


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
    "river_reaches": ("river_id", "River Name", "Reach Name", "River", "Reach"),
    "edge_lines": ("edge_id", "bank_side", "River", "Reach"),
    "cross_sections": ("River", "Reach", "RS"),
    "structures": ("Name", "Type", "Connection", "SA-2D", "River", "Reach", "RS"),
    "pipe_conduits": ("conduit_id", "Name", "System Name", "US Node", "DS Node", "Shape", "Rise", "Span", "Manning's n"),
    "pipe_nodes": ("node_id", "Name", "System Name", "Node Type", "Invert Elevation", "Terrain Elevation", "Depth"),
    "pump_stations": ("station_id", "Name", "Pump Station"),
    "mannings_n_regions": ("region_id", "Name", "2D_Area_Name"),
    "infiltration_regions": ("region_id", "Name"),
}

_GEOMETRY_LABELS = {
    "model_extents": "Model Extents",
    "mesh_areas": "2D Flow Areas",
    "mesh_cells": "2D Mesh Cells",
    "mesh_faces": "2D Mesh Faces",
    "centerlines": "River Centerlines",
    "river_reaches": "Rivers / Reaches",
    "edge_lines": "River Edge Lines",
    "cross_sections": "Cross Sections",
    "structures": "Hydraulic Structures",
    "pipe_conduits": "Pipe Conduits",
    "pipe_nodes": "Pipe Nodes",
    "bc_lines": "Boundary Conditions",
    "breaklines": "Breaklines",
    "refinement_regions": "Refinement Regions",
    "reference_lines": "Reference Lines",
    "reference_points": "Reference Points",
    "storage_areas": "Storage Areas",
    "pump_stations": "Pump Stations",
    "mannings_n_regions": "Manning's n Regions",
    "infiltration_regions": "Infiltration Regions",
    "terrain_modification_lines": "Modification Lines",
    "terrain_modification_polygons": "Modification Polygons",
    "terrain_modification_control_points": "Elevation Control Points",
    "terrain_source_footprints": "Source Raster Footprints",
}

_GEOMETRY_STYLES = {
    "model_extents": {"fill": "#f59e0b", "fillOpacity": 0.08, "line": "#ea580c", "lineWidth": 2.0},
    "mesh_areas": {"fill": "#60a5fa", "fillOpacity": 0.10, "line": "#1d4ed8", "lineWidth": 1.0},
    "mesh_cells": {"fill": "#93c5fd", "fillOpacity": 0.14, "line": "#2563eb", "lineWidth": 0.35, "minzoom": 13},
    "mesh_faces": {"fill": "#2563eb", "fillOpacity": 0.0, "line": "#2563eb", "lineWidth": 0.65, "minzoom": 14},
    "centerlines": {"fill": "#0f766e", "fillOpacity": 0.0, "line": "#0f766e", "lineWidth": 1.2},
    "river_reaches": {"fill": "#0891b2", "fillOpacity": 0.0, "line": "#0891b2", "lineWidth": 1.5},
    "edge_lines": {"fill": "#64748b", "fillOpacity": 0.0, "line": "#64748b", "lineWidth": 0.9},
    "cross_sections": {"fill": "#0f766e", "fillOpacity": 0.0, "line": "#0f766e", "lineWidth": 1.0},
    "structures": {"fill": "#dc2626", "fillOpacity": 0.0, "line": "#dc2626", "lineWidth": 1.4},
    "pipe_conduits": {"fill": "#0891b2", "fillOpacity": 0.0, "line": "#0891b2", "lineWidth": 1.6},
    "pipe_nodes": {"fill": "#facc15", "fillOpacity": 0.72, "line": "#a16207", "lineWidth": 0.9},
    "bc_lines": {"fill": "#7c3aed", "fillOpacity": 0.0, "line": "#7c3aed", "lineWidth": 1.0},
    "breaklines": {"fill": "#a16207", "fillOpacity": 0.0, "line": "#a16207", "lineWidth": 1.0},
    "refinement_regions": {"fill": "#c4b5fd", "fillOpacity": 0.12, "line": "#7c3aed", "lineWidth": 1.0},
    "reference_lines": {"fill": "#0f766e", "fillOpacity": 0.0, "line": "#0f766e", "lineWidth": 1.0},
    "reference_points": {"fill": "#facc15", "fillOpacity": 0.55, "line": "#a16207", "lineWidth": 0.75},
    "storage_areas": {"fill": "#38bdf8", "fillOpacity": 0.10, "line": "#0284c7", "lineWidth": 1.0},
    "pump_stations": {"fill": "#f97316", "fillOpacity": 0.82, "line": "#9a3412", "lineWidth": 1.0},
    "mannings_n_regions": {"fill": "#84cc16", "fillOpacity": 0.12, "line": "#4d7c0f", "lineWidth": 1.0},
    "infiltration_regions": {"fill": "#22c55e", "fillOpacity": 0.10, "line": "#15803d", "lineWidth": 1.0},
    "terrain_modification_lines": {"fill": "#f97316", "fillOpacity": 0.0, "line": "#ea580c", "lineWidth": 2.0},
    "terrain_modification_polygons": {"fill": "#facc15", "fillOpacity": 0.18, "line": "#ca8a04", "lineWidth": 1.4},
    "terrain_modification_control_points": {"fill": "#ef4444", "fillOpacity": 0.9, "line": "#7f1d1d", "lineWidth": 1.0},
    "terrain_source_footprints": {"fill": "#14b8a6", "fillOpacity": 0.08, "line": "#0f766e", "lineWidth": 1.2},
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


@dataclass(frozen=True)
class TerrainPackageSummary:
    """Browser terrain artifact produced from an archived terrain COG."""

    manifest_path: Path
    pmtiles_path: Path
    source_cog: Path
    raster_stats: dict[str, float]
    max_zoom: int


@dataclass(frozen=True)
class RasterPackageSummary:
    """Browser display derivative paired with an authoritative numeric COG."""

    manifest_path: Path
    pmtiles_path: Path
    source_cog: Path
    raster_stats: dict[str, float]
    max_zoom: int
    layer_id: str


@dataclass(frozen=True)
class VectorResultPackageSummary:
    """Queryable vector Stored Map packaged for browser delivery."""

    manifest_path: Path
    pmtiles_path: Path
    source_vector: Path
    feature_count: int
    layer_id: str


# RASMapper's standard terrain palette. The input elevation range is stretched
# across this palette so each model retains usable local relief without
# resampling its source elevation values.
_RAS_TERRAIN_COLORS: tuple[tuple[int, int, int, int, int], ...] = (
    (466, 105, 210, 179, 255),
    (795, 68, 214, 74, 255),
    (1044, 200, 238, 47, 255),
    (1243, 242, 212, 58, 255),
    (1436, 240, 138, 36, 255),
    (1621, 200, 30, 30, 255),
    (1836, 127, 0, 0, 255),
    (2300, 217, 217, 217, 255),
    (2542, 255, 255, 255, 255),
)

_RESULT_COLOR_RAMPS: dict[str, tuple[tuple[int, int, int, int], ...]] = {
    "depth": (
        (239, 246, 255, 210),
        (147, 197, 253, 225),
        (37, 99, 235, 235),
        (30, 58, 138, 245),
    ),
    "velocity": (
        (254, 249, 195, 220),
        (250, 204, 21, 230),
        (249, 115, 22, 235),
        (220, 38, 38, 240),
        (126, 34, 206, 245),
    ),
    "water_surface_elevation": (
        (34, 197, 94, 225),
        (250, 204, 21, 230),
        (249, 115, 22, 235),
        (220, 38, 38, 240),
        (248, 250, 252, 245),
    ),
    "inundation": (
        (96, 165, 250, 215),
        (29, 78, 216, 235),
    ),
    "froude": (
        (30, 64, 175, 225),
        (56, 189, 248, 225),
        (74, 222, 128, 230),
        (250, 204, 21, 235),
        (220, 38, 38, 245),
    ),
    "shear_stress": (
        (254, 249, 195, 215),
        (251, 146, 60, 230),
        (220, 38, 38, 240),
        (126, 34, 206, 245),
    ),
    "arrival_time": (
        (220, 38, 38, 240),
        (249, 115, 22, 235),
        (250, 204, 21, 230),
        (34, 197, 94, 225),
        (37, 99, 235, 240),
    ),
    "duration": (
        (239, 246, 255, 210),
        (147, 197, 253, 225),
        (59, 130, 246, 235),
        (67, 56, 202, 240),
        (88, 28, 135, 245),
    ),
    "percent_inundated": (
        (239, 246, 255, 210),
        (147, 197, 253, 225),
        (59, 130, 246, 235),
        (30, 64, 175, 245),
    ),
    "difference": (
        (30, 64, 175, 240),
        (147, 197, 253, 225),
        (248, 250, 252, 210),
        (252, 165, 165, 225),
        (185, 28, 28, 240),
    ),
    "depth_velocity": (
        (254, 249, 195, 215),
        (250, 204, 21, 225),
        (249, 115, 22, 235),
        (190, 24, 93, 240),
        (88, 28, 135, 245),
    ),
    "hazard_class": (
        (214, 244, 210, 235),
        (166, 217, 106, 235),
        (255, 237, 111, 235),
        (253, 174, 97, 240),
        (239, 91, 82, 245),
        (165, 0, 38, 250),
    ),
    "threshold": (
        (0, 0, 0, 0),
        (37, 99, 235, 235),
    ),
}


def _categorical_legend_entries(map_type: str) -> list[dict[str, Any]]:
    """Return fixed, query-safe labels for controlled categorical recipes."""

    normalized = _slug(map_type).replace("-", "_")
    if normalized == "hazard_class":
        values = range(1, 7)
        labels = [f"H{value}" for value in values]
        palette = _RESULT_COLOR_RAMPS["hazard_class"]
    elif normalized == "inundation_threshold":
        values = (0, 1)
        labels = ["Below threshold", "At or above threshold"]
        palette = _RESULT_COLOR_RAMPS["threshold"]
    else:
        return []
    return [
        {
            "value": value,
            "label": label,
            "color": f"#{red:02x}{green:02x}{blue:02x}",
        }
        for value, label, (red, green, blue, _alpha) in zip(values, labels, palette)
    ]


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def _tippecanoe_command() -> str:
    """Return the executable used for vector tile generation.

    ``tippecanoe`` is the portable default. Windows hosts that bridge to a
    Linux Tippecanoe installation can provide the full wrapper path through
    ``RAS2CNG_TIPPECANOE``.
    """

    return os.environ.get("RAS2CNG_TIPPECANOE", "tippecanoe")


def _pmtiles_command() -> str:
    """Return the PMTiles conversion executable for vector tile delivery."""

    return os.environ.get("RAS2CNG_PMTILES", "pmtiles")


def _gdal_command(name: str) -> str:
    """Return a GDAL executable, allowing workers to use explicit wrappers."""

    key = f"RAS2CNG_{name.upper().replace('-', '_')}"
    return os.environ.get(key, name)


def _gdalinfo_command() -> str:
    return _gdal_command("gdalinfo")


def _gdaldem_command() -> str:
    return _gdal_command("gdaldem")


def _gdalwarp_command() -> str:
    return _gdal_command("gdalwarp")


def _gdal_translate_command() -> str:
    return _gdal_command("gdal_translate")


def _gdaladdo_command() -> str:
    return _gdal_command("gdaladdo")


def _gdal_thread_count() -> str:
    """Return the bounded raster-processing thread count for shared workers."""

    return os.environ.get("RAS2CNG_GDAL_THREADS", "4")


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
    mbtiles_path = output.with_suffix(".mbtiles")
    command = [
        _tippecanoe_command(),
        "--force",
        "--read-parallel",
        "--no-tile-size-limit",
        "--no-feature-limit",
        "--extend-zooms-if-still-dropping",
        f"--minimum-zoom={min_zoom}",
        f"--maximum-zoom={max_zoom}",
        "--output",
        str(mbtiles_path),
    ]
    if temporary_directory is not None:
        temporary_directory.mkdir(parents=True, exist_ok=True)
        command.extend(["--temporary-directory", str(temporary_directory)])
    for source_layer, source_path in layers:
        command.extend(["-L", f"{source_layer}:{source_path}"])
    try:
        subprocess.run(command, check=True, capture_output=True, text=True)
        subprocess.run(
            [_pmtiles_command(), "convert", str(mbtiles_path), str(output)],
            check=True,
            capture_output=True,
            text=True,
        )
    finally:
        mbtiles_path.unlink(missing_ok=True)


def _gdalinfo(path: Path) -> dict[str, Any]:
    """Read GDAL JSON metadata, calculating band statistics when needed."""

    completed = subprocess.run(
        [_gdalinfo_command(), "-json", "-stats", str(path)],
        check=True,
        capture_output=True,
        text=True,
    )
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"GDAL did not return JSON metadata for {path}") from error


def _raster_stats(info: Mapping[str, Any]) -> dict[str, float]:
    """Return finite first-band statistics from ``gdalinfo -json -stats``."""

    bands = info.get("bands") or []
    if not bands:
        raise ValueError("Numeric COG has no raster bands.")
    band = bands[0]
    values: dict[str, float] = {}
    for output_key, keys in {
        "minimum": ("minimum", "computedMin"),
        "maximum": ("maximum", "computedMax"),
        "mean": ("mean", "computedMean"),
        "stddev": ("stdDev", "computedStdDev"),
    }.items():
        value = next((band.get(key) for key in keys if band.get(key) is not None), None)
        if value is not None:
            try:
                numeric = float(value)
            except (TypeError, ValueError):
                continue
            if numeric == numeric and numeric not in (float("inf"), float("-inf")):
                values[output_key] = numeric
    if "minimum" not in values or "maximum" not in values:
        raise ValueError("Numeric COG does not have finite minimum and maximum statistics.")
    if values["maximum"] < values["minimum"]:
        raise ValueError("Numeric COG maximum is less than its minimum.")
    return values


def _expanded_raster_domain(minimum: float, maximum: float) -> tuple[float, float]:
    """Return a GDAL-color-relief-safe domain while preserving constant stats."""

    if maximum > minimum:
        return minimum, maximum
    epsilon = max(abs(minimum) * 1e-9, 1e-9)
    return minimum - epsilon, maximum + epsilon


def _native_raster_zoom_from_resolution(resolution: float) -> int:
    """Calculate a no-upsample Web Mercator zoom from a native cell size."""

    import math

    if resolution <= 0:
        raise ValueError("Terrain cell resolution must be positive.")
    return max(0, int(math.floor(math.log2(156543.03392804097 / resolution))))


def _terrain_color_ramp(stats: Mapping[str, float], path: Path) -> None:
    """Write a stretched RAS terrain palette with transparent no-data cells."""

    minimum, maximum = _expanded_raster_domain(
        float(stats["minimum"]),
        float(stats["maximum"]),
    )
    source_minimum = _RAS_TERRAIN_COLORS[0][0]
    source_maximum = _RAS_TERRAIN_COLORS[-1][0]
    span = source_maximum - source_minimum
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for elevation, red, green, blue, alpha in _RAS_TERRAIN_COLORS:
        fraction = (elevation - source_minimum) / span
        value = minimum + (maximum - minimum) * fraction
        lines.append(f"{value:.9f} {red} {green} {blue} {alpha}")
    # HEC-RAS terrains conventionally use -9999 outside the valid terrain
    # footprint. Without this palette entry, gdaldem assigns those cells the
    # first terrain color instead of preserving the raster's no-data mask.
    lines.append("nv 0 0 0 0")
    path.write_text("\n".join(lines) + "\n", encoding="ascii")


def _result_color_ramp(
    stats: Mapping[str, float],
    map_type: str,
    path: Path,
) -> tuple[str, list[str]]:
    """Write a transparent-nodata result ramp and return its preset/colors."""

    normalized = _slug(map_type).replace("-", "_")
    aliases = {
        "wse": "water_surface_elevation",
        "water_surface": "water_surface_elevation",
        "water_surface_elevation": "water_surface_elevation",
        "inundation_boundary": "inundation",
        "floodplain_boundary": "inundation",
        "max_depth": "depth",
        "maximum_depth": "depth",
        "max_velocity": "velocity",
        "maximum_velocity": "velocity",
        "froude_number": "froude",
        "depth_x_velocity": "depth_velocity",
        "depth_x_velocity_squared": "depth_velocity",
        "percent_time_inundated": "percent_inundated",
        "compare_wse": "difference",
        "compare_depth": "difference",
        "compare_velocity": "difference",
        "terrain_mod_delta": "difference",
        "depth_velocity": "depth_velocity",
        "depth_velocity_squared": "depth_velocity",
        "hazard_class": "hazard_class",
        "inundation_threshold": "threshold",
    }
    palette_key = aliases.get(normalized, normalized)
    palette = _RESULT_COLOR_RAMPS.get(palette_key, _RESULT_COLOR_RAMPS["depth"])
    minimum, maximum = _expanded_raster_domain(
        float(stats["minimum"]),
        float(stats["maximum"]),
    )
    span = maximum - minimum
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for index, (red, green, blue, alpha) in enumerate(palette):
        fraction = index / max(1, len(palette) - 1)
        lines.append(f"{minimum + span * fraction:.9f} {red} {green} {blue} {alpha}")
    lines.append("nv 0 0 0 0")
    path.write_text("\n".join(lines) + "\n", encoding="ascii")
    preset = {
        "froude": "rascommander.froude",
        "shear_stress": "rascommander.shear-stress",
        "arrival_time": "rascommander.arrival-time",
        "duration": "rascommander.duration",
        "percent_inundated": "rascommander.percent-inundated",
        "difference": "rascommander.difference",
        "depth_velocity": "rascommander.depth-velocity",
        "hazard_class": "rascommander.hazard-aidr-2017",
        "threshold": "rascommander.threshold",
    }.get(palette_key, f"rasmapper.{palette_key.replace('_', '-')}")
    return (
        preset,
        [f"#{red:02x}{green:02x}{blue:02x}" for red, green, blue, _ in palette],
    )


def _relative_href(path: Path, base_dir: Path) -> str:
    """Return a browser-safe relative href from a viewer directory."""

    return Path(os.path.relpath(path.resolve(), base_dir.resolve())).as_posix()


def _raster_source_metadata(path: Path) -> dict[str, Any]:
    """Read CRS, native bounds, WGS84 bounds, dtype, and nodata from a COG."""

    import math
    import rasterio
    from rasterio.warp import transform_bounds

    with rasterio.open(path) as source:
        if source.crs is None:
            raise ValueError(f"Numeric raster has no CRS and cannot be published: {path}")
        # RASMapper GeoTIFF WKT frequently omits the projected CRS authority even
        # when PROJ can identify it at lower confidence. The browser still needs
        # an explicit Proj4 definition because proj4js does not bundle EPSG data.
        epsg = source.crs.to_epsg(confidence_threshold=25)
        crs = f"EPSG:{epsg}" if epsg else source.crs.to_string()
        proj4 = source.crs.to_proj4() or None
        if proj4:
            proj4 = " ".join(
                token[:-5] if token.endswith("=True") else token
                for token in proj4.split()
            )
        wgs84_bounds = transform_bounds(
            source.crs,
            "EPSG:4326",
            *source.bounds,
            densify_pts=21,
        )
        nodata = source.nodata
        if nodata is not None and not math.isfinite(float(nodata)):
            nodata = None
        return {
            "sourceCrs": crs,
            "sourceProj4": proj4,
            "sourceBounds": [float(value) for value in source.bounds],
            "bounds": [float(value) for value in wgs84_bounds],
            "dtype": str(source.dtypes[0]),
            "nodata": None if nodata is None else float(nodata),
        }


def _render_raster_pmtiles(
    cog_path: Path,
    output: Path,
    *,
    ramp_writer,
    max_zoom: int | None,
    scratch_dir: Path | None,
    prefix: str,
) -> int:
    """Render a numeric COG to transparent PNG PMTiles with bounded GDAL work."""

    with tempfile.TemporaryDirectory(
        prefix=f"ras2cng-{prefix}-",
        dir=str(scratch_dir) if scratch_dir is not None else None,
    ) as temporary:
        work_dir = Path(temporary)
        ramp = work_dir / "ramp.txt"
        colorized = work_dir / "colorized.tif"
        web_mercator = work_dir / "display-3857.tif"
        mbtiles = work_dir / "display.mbtiles"
        ramp_writer(ramp)
        subprocess.run(
            [_gdaldem_command(), "color-relief", str(cog_path), str(ramp), str(colorized), "-alpha"],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            [
                _gdalwarp_command(),
                "-t_srs", "EPSG:3857",
                "-r", "bilinear",
                "-srcalpha",
                "-dstalpha",
                "-multi",
                "-wo", f"NUM_THREADS={_gdal_thread_count()}",
                "-wm", "512",
                str(colorized),
                str(web_mercator),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        transformed_info = _gdalinfo(web_mercator)
        transform = transformed_info.get("geoTransform") or []
        if len(transform) < 6:
            raise ValueError("Reprojected raster is missing a GDAL geotransform.")
        native_resolution = max(abs(float(transform[1])), abs(float(transform[5])))
        native_max_zoom = _native_raster_zoom_from_resolution(native_resolution)
        selected_max_zoom = native_max_zoom if max_zoom is None else min(max_zoom, native_max_zoom)
        subprocess.run(
            [
                _gdal_translate_command(),
                "-of", "MBTiles",
                "-co", "TILE_FORMAT=PNG",
                "-co", "ZOOM_LEVEL_STRATEGY=LOWER",
                str(web_mercator),
                str(mbtiles),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            [_gdaladdo_command(), "-r", "average", str(mbtiles), "2", "4", "8", "16", "32", "64", "128"],
            check=True,
            capture_output=True,
            text=True,
        )
        output.unlink(missing_ok=True)
        subprocess.run(
            [_pmtiles_command(), "convert", str(mbtiles), str(output)],
            check=True,
            capture_output=True,
            text=True,
        )
    return selected_max_zoom


def package_maplibre_terrain(
    cog_path: Path,
    viewer_dir: Path,
    *,
    name: str = "Terrain",
    source_cog: str | None = None,
    units: str = "ft",
    max_zoom: int | None = None,
    scratch_dir: Path | None = None,
    overwrite: bool = False,
) -> TerrainPackageSummary:
    """Add a RAS-styled, queryable terrain PMTiles layer to a viewer bundle.

    The source COG remains the numerical source for identify queries. The
    PMTiles overlay is a colorized Web Mercator representation used only for
    display. Its highest zoom is capped at source cell resolution so no
    terrain detail is invented in the browser.
    """

    cog_path = Path(cog_path)
    viewer_dir = Path(viewer_dir)
    manifest_path = viewer_dir / "manifest.json"
    if not cog_path.is_file():
        raise FileNotFoundError(f"Terrain COG does not exist: {cog_path}")
    if not manifest_path.is_file():
        raise FileNotFoundError(f"MapLibre viewer manifest does not exist: {manifest_path}")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    tilesets = manifest.setdefault("tilesets", [])
    existing = next((item for item in tilesets if item.get("id") == "terrain"), None)
    if existing and not overwrite:
        raise FileExistsError("Viewer already has a terrain tileset; pass overwrite=True to replace it.")

    for executable in (
        _gdalinfo_command(),
        _gdaldem_command(),
        _gdalwarp_command(),
        _gdal_translate_command(),
        _gdaladdo_command(),
        _pmtiles_command(),
    ):
        _require_cli(executable)

    if scratch_dir is not None:
        scratch_dir = Path(scratch_dir).resolve()
        scratch_dir.mkdir(parents=True, exist_ok=True)
        if not scratch_dir.is_dir():
            raise ValueError(f"Terrain scratch directory is not a directory: {scratch_dir}")

    stats = _raster_stats(_gdalinfo(cog_path))
    raster_metadata = _raster_source_metadata(cog_path)
    tiles_dir = viewer_dir / "tiles"
    tiles_dir.mkdir(parents=True, exist_ok=True)
    output = tiles_dir / "terrain.pmtiles"
    if output.exists() and not overwrite:
        raise FileExistsError(f"Terrain PMTiles already exists: {output}")
    source_href = source_cog or _relative_href(cog_path, viewer_dir)

    selected_max_zoom = _render_raster_pmtiles(
        cog_path,
        output,
        ramp_writer=lambda path: _terrain_color_ramp(stats, path),
        max_zoom=max_zoom,
        scratch_dir=scratch_dir,
        prefix="terrain",
    )

    terrain_tileset = {
        "id": "terrain",
        "name": name,
        "type": "raster",
        "href": "tiles/terrain.pmtiles",
        "sourceCog": source_href,
        "bytes": output.stat().st_size,
        "tileSize": 256,
        "groupId": "ras-terrains",
        "visible": True,
        "opacity": 1.0,
        "maxzoom": selected_max_zoom,
        "rasterStats": stats,
        "ramp": "stretched",
        "domainPolicy": "fixed",
        "sourceKind": "terrain",
        "legend": {
            "type": "continuous",
            "mode": "stretched",
            "preset": "rasmapper.terrain",
            "domainPolicy": "fixed",
            "colors": [
                f"#{red:02x}{green:02x}{blue:02x}"
                for _, red, green, blue, _ in _RAS_TERRAIN_COLORS
            ],
        },
        "queryable": True,
        "units": units,
        "storedMap": {
            "mapType": "terrain",
            "source": "HEC-RAS terrain GeoTIFF",
            "cogBytes": cog_path.stat().st_size,
        },
        **raster_metadata,
    }
    if existing:
        tilesets[tilesets.index(existing)] = terrain_tileset
    else:
        tilesets.append(terrain_tileset)
    groups = manifest.setdefault("groups", [])
    if not any(group.get("id") == "ras-terrains" for group in groups):
        groups.append({"id": "ras-terrains", "name": "Terrain", "visible": True})
    apply_manifest_v2(manifest)
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return TerrainPackageSummary(
        manifest_path=manifest_path,
        pmtiles_path=output,
        source_cog=cog_path,
        raster_stats=stats,
        max_zoom=selected_max_zoom,
    )


def package_maplibre_stored_map(
    cog_path: Path,
    viewer_dir: Path,
    *,
    plan: str,
    map_type: str,
    name: str | None = None,
    profile: str | None = None,
    geometry: str | None = None,
    layer_id: str | None = None,
    source_cog: str | None = None,
    units: str = "ft",
    visible: bool = False,
    domain_policy: str = "fixed",
    max_zoom: int | None = None,
    scratch_dir: Path | None = None,
    overwrite: bool = False,
) -> RasterPackageSummary:
    """Publish one RASMapper Stored Map as display PMTiles plus numeric COG.

    The numeric COG remains authoritative for Identify and future view-local
    styling. The PMTiles derivative is a fast default visualization and is
    explicitly recorded as RASMapper/RasProcess interpolation, not raw HDF
    computation-element data.
    """

    provenance: dict[str, Any] = {
        "mapType": map_type,
        "source": "RASMapper/RasProcess Stored Map",
        "interpolationAuthority": "RASMapper/RasProcess",
    }
    return _package_maplibre_numeric_raster(
        cog_path,
        viewer_dir,
        plan=plan,
        map_type=map_type,
        name=name,
        profile=profile,
        geometry=geometry,
        layer_id=layer_id,
        source_cog=source_cog,
        units=units,
        visible=visible,
        domain_policy=domain_policy,
        max_zoom=max_zoom,
        scratch_dir=scratch_dir,
        overwrite=overwrite,
        source_kind="stored-map",
        provenance=provenance,
        result_kind="rasmapper_stored_map",
        legend_type="continuous",
        legend_mode="stretched",
    )


def package_maplibre_stored_vector(
    vector_path: Path,
    viewer_dir: Path,
    *,
    plan: str,
    map_type: str,
    name: str | None = None,
    profile: str | None = None,
    geometry: str | None = None,
    layer_id: str | None = None,
    crs: str | None = None,
    visible: bool = False,
    min_zoom: int = 0,
    max_zoom: int = 17,
    scratch_dir: Path | None = None,
    overwrite: bool = False,
) -> VectorResultPackageSummary:
    """Publish a RASMapper vector Stored Map as queryable PMTiles."""

    vector_path = Path(vector_path)
    viewer_dir = Path(viewer_dir)
    manifest_path = viewer_dir / "manifest.json"
    if not vector_path.is_file():
        raise FileNotFoundError(f"Stored Map vector does not exist: {vector_path}")
    if not manifest_path.is_file():
        raise FileNotFoundError(f"MapLibre viewer manifest does not exist: {manifest_path}")

    plan_id = _slug(plan)
    if plan_id.isdigit():
        plan_id = f"p{plan_id.zfill(2)}"
    elif plan_id.startswith("p") and plan_id[1:].isdigit():
        plan_id = f"p{plan_id[1:].zfill(2)}"
    if not plan_id:
        raise ValueError("A plan identifier is required for every Stored Map vector")
    map_slug = _slug(map_type)
    profile_slug = _slug(profile or "")
    layer_id = layer_id or "-".join(
        value for value in (plan_id, map_slug, profile_slug) if value
    )
    if not layer_id:
        raise ValueError("Could not derive a Stored Map vector layer identifier")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    tilesets = manifest.setdefault("tilesets", [])
    existing = next((item for item in tilesets if item.get("id") == layer_id), None)
    if existing and not overwrite:
        raise FileExistsError(
            f"Viewer already has vector layer {layer_id}; pass overwrite=True to replace it."
        )

    _require_cli(_tippecanoe_command())
    _require_cli(_pmtiles_command())
    if vector_path.suffix.lower() in {".parquet", ".geoparquet"}:
        frame = gpd.read_parquet(vector_path)
    else:
        frame = gpd.read_file(vector_path)
    frame = _to_wgs84(frame, vector_path, crs)
    if frame.empty:
        raise ValueError(f"Stored Map vector has no publishable features: {vector_path}")

    scratch_parent = Path(scratch_dir).resolve() if scratch_dir else None
    if scratch_parent:
        scratch_parent.mkdir(parents=True, exist_ok=True)
    output = viewer_dir / "tiles" / f"{layer_id}.pmtiles"
    if output.exists() and not overwrite:
        raise FileExistsError(f"Vector PMTiles already exists: {output}")
    with tempfile.TemporaryDirectory(
        prefix=f"ras2cng-{layer_id}-",
        dir=str(scratch_parent) if scratch_parent else None,
    ) as temporary:
        work_dir = Path(temporary)
        ndgeojson = work_dir / f"{layer_id}.ndgeojson"
        feature_count, geometry_types, bounds = _write_ndgeojson(frame, ndgeojson)
        _run_tippecanoe(
            output,
            [(layer_id, ndgeojson)],
            min_zoom,
            max_zoom,
            work_dir / "tippecanoe",
        )

    provenance: dict[str, Any] = {
        "source": "RASMapper/RasProcess Stored Map",
        "interpolationAuthority": "RASMapper/RasProcess",
        "mapType": map_type,
        "plan": plan_id,
        "sourceVector": vector_path.name,
    }
    if profile:
        provenance["profile"] = profile
    if geometry:
        provenance["geometry"] = geometry
    layer = {
        "id": layer_id,
        "name": name or " ".join(value for value in (map_type, profile) if value),
        "sourceLayer": layer_id,
        "groupId": f"ras-results-{plan_id}",
        "geometryId": geometry,
        "visible": visible,
        "kind": map_slug.replace("-", "_"),
        "sourceKind": "stored-map",
        "style": {
            "fill": "#2563eb",
            "fillOpacity": 0.12,
            "line": "#1d4ed8",
            "lineWidth": 1.6,
        },
        "featureCount": feature_count,
        "geometryTypes": geometry_types,
        "bounds": list(bounds),
        "sort": 90,
        "queryable": True,
        "provenance": provenance,
    }
    tileset = {
        "id": layer_id,
        "type": "vector",
        "href": f"tiles/{layer_id}.pmtiles",
        "bytes": output.stat().st_size,
        "layers": [layer],
        "groupId": f"ras-results-{plan_id}",
        "resultKind": "stored_map",
    }
    if existing:
        tilesets[tilesets.index(existing)] = tileset
    else:
        tilesets.append(tileset)
    groups = manifest.setdefault("groups", [])
    group_id = f"ras-results-{plan_id}"
    if not any(group.get("id") == group_id for group in groups):
        groups.append(
            {
                "id": group_id,
                "name": f"Plan {plan_id}",
                "visible": False,
                "resultKind": "stored_map",
            }
        )
    apply_manifest_v2(manifest)
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return VectorResultPackageSummary(
        manifest_path=manifest_path,
        pmtiles_path=output,
        source_vector=vector_path,
        feature_count=feature_count,
        layer_id=layer_id,
    )


def package_maplibre_calculated_map(
    cog_path: Path,
    viewer_dir: Path,
    *,
    plan: str,
    recipe_id: str,
    name: str | None = None,
    profile: str | None = None,
    geometry: str | None = None,
    layer_id: str | None = None,
    source_cog: str | None = None,
    units: str | None = None,
    provenance_path: Path | None = None,
    visible: bool = False,
    domain_policy: str = "fixed",
    max_zoom: int | None = None,
    scratch_dir: Path | None = None,
    overwrite: bool = False,
) -> RasterPackageSummary:
    """Publish a controlled recipe output under a plan's Calculated Layers."""

    from ras2cng.raster_recipes import get_raster_recipe

    cog_path = Path(cog_path)
    recipe = get_raster_recipe(recipe_id)
    provenance_path = Path(provenance_path) if provenance_path else cog_path.with_suffix(".provenance.json")
    recipe_run: dict[str, Any] = {}
    if provenance_path.is_file():
        recipe_run = json.loads(provenance_path.read_text(encoding="utf-8"))
        recorded_id = (recipe_run.get("recipe") or {}).get("recipe_id")
        if recorded_id and recorded_id != recipe_id:
            raise ValueError(
                f"Calculated raster provenance records recipe {recorded_id!r}, not {recipe_id!r}"
            )
    profile = profile or recipe_run.get("profile")
    units = units or (recipe_run.get("output") or {}).get("units")
    if not units:
        raise ValueError("Calculated raster units are required or must exist in its provenance sidecar")
    if recipe.categorical and domain_policy != "fixed":
        raise ValueError("Categorical calculated rasters require domain_policy='fixed'")
    provenance: dict[str, Any] = {
        "mapType": recipe.recipe_id,
        "recipeId": recipe.recipe_id,
        "recipeVersion": recipe.version,
        "source": "ras2cng controlled raster recipe",
        "arithmeticAuthority": "ras2cng",
        "interpolationAuthority": "RASMapper/RasProcess source rasters",
        "parameters": recipe_run.get("parameters", dict(recipe.parameter_defaults)),
        "inputs": recipe_run.get("inputs", {}),
    }
    return _package_maplibre_numeric_raster(
        cog_path,
        viewer_dir,
        plan=plan,
        map_type=recipe.recipe_id,
        name=name or recipe.name,
        profile=profile,
        geometry=geometry,
        layer_id=layer_id,
        source_cog=source_cog,
        units=units,
        visible=visible,
        domain_policy=domain_policy,
        max_zoom=max_zoom,
        scratch_dir=scratch_dir,
        overwrite=overwrite,
        source_kind="calculated",
        provenance=provenance,
        result_kind="calculated_raster",
        legend_type="categorical" if recipe.categorical else "continuous",
        legend_mode="discrete" if recipe.categorical else "stretched",
    )


def _package_maplibre_numeric_raster(
    cog_path: Path,
    viewer_dir: Path,
    *,
    plan: str,
    map_type: str,
    name: str | None,
    profile: str | None,
    geometry: str | None,
    layer_id: str | None,
    source_cog: str | None,
    units: str,
    visible: bool,
    domain_policy: str,
    max_zoom: int | None,
    scratch_dir: Path | None,
    overwrite: bool,
    source_kind: str,
    provenance: Mapping[str, Any],
    result_kind: str,
    legend_type: str,
    legend_mode: str,
) -> RasterPackageSummary:
    """Shared numeric COG plus display PMTiles publication implementation."""

    cog_path = Path(cog_path)
    viewer_dir = Path(viewer_dir)
    manifest_path = viewer_dir / "manifest.json"
    if not cog_path.is_file():
        raise FileNotFoundError(f"Numeric COG does not exist: {cog_path}")
    if not manifest_path.is_file():
        raise FileNotFoundError(f"MapLibre viewer manifest does not exist: {manifest_path}")
    if domain_policy not in {"fixed", "current-view"}:
        raise ValueError("domain_policy must be 'fixed' or 'current-view'")

    plan_id = _slug(plan)
    if plan_id.isdigit():
        plan_id = f"p{plan_id.zfill(2)}"
    elif plan_id.startswith("p") and plan_id[1:].isdigit():
        plan_id = f"p{plan_id[1:].zfill(2)}"
    if not plan_id:
        raise ValueError("A plan identifier is required for every result raster")
    map_slug = _slug(map_type)
    profile_slug = _slug(profile or "")
    layer_id = layer_id or "-".join(
        value for value in (plan_id, map_slug, profile_slug) if value
    )
    if not layer_id:
        raise ValueError("Could not derive a result raster layer identifier")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    tilesets = manifest.setdefault("tilesets", [])
    existing = next((item for item in tilesets if item.get("id") == layer_id), None)
    if existing and not overwrite:
        raise FileExistsError(
            f"Viewer already has raster layer {layer_id}; pass overwrite=True to replace it."
        )

    for executable in (
        _gdalinfo_command(),
        _gdaldem_command(),
        _gdalwarp_command(),
        _gdal_translate_command(),
        _gdaladdo_command(),
        _pmtiles_command(),
    ):
        _require_cli(executable)

    if scratch_dir is not None:
        scratch_dir = Path(scratch_dir).resolve()
        scratch_dir.mkdir(parents=True, exist_ok=True)
        if not scratch_dir.is_dir():
            raise ValueError(f"Raster scratch directory is not a directory: {scratch_dir}")

    source_info = _gdalinfo(cog_path)
    stats = _raster_stats(source_info)
    raster_metadata = _raster_source_metadata(cog_path)
    tiles_dir = viewer_dir / "tiles"
    tiles_dir.mkdir(parents=True, exist_ok=True)
    output = tiles_dir / f"{layer_id}.pmtiles"
    if output.exists() and not overwrite:
        raise FileExistsError(f"Raster PMTiles already exists: {output}")
    source_href = source_cog or _relative_href(cog_path, viewer_dir)
    legend: dict[str, Any] = {}

    def write_ramp(path: Path) -> None:
        preset, colors = _result_color_ramp(stats, map_type, path)
        legend["preset"] = preset
        legend["colors"] = colors

    selected_max_zoom = _render_raster_pmtiles(
        cog_path,
        output,
        ramp_writer=write_ramp,
        max_zoom=max_zoom,
        scratch_dir=scratch_dir,
        prefix=layer_id,
    )

    stored_map: dict[str, Any] = {
        **dict(provenance),
        "mapType": map_type,
        "plan": plan_id,
        "cogBytes": cog_path.stat().st_size,
    }
    if profile:
        stored_map["profile"] = profile
    if geometry:
        stored_map["geometry"] = geometry
    legend_record: dict[str, Any] = {
        "type": legend_type,
        "mode": legend_mode,
        "preset": legend["preset"],
        "domainPolicy": domain_policy,
        "colors": legend["colors"],
    }
    categories = _categorical_legend_entries(map_type)
    if categories:
        legend_record["categories"] = categories

    raster_tileset: dict[str, Any] = {
        "id": layer_id,
        "name": name or " ".join(value for value in (map_type, profile) if value),
        "type": "raster",
        "href": f"tiles/{layer_id}.pmtiles",
        "sourceCog": source_href,
        "bytes": output.stat().st_size,
        "tileSize": 256,
        "groupId": f"ras-results-{plan_id}",
        "visible": visible,
        "opacity": 0.82,
        "maxzoom": selected_max_zoom,
        "rasterStats": stats,
        "ramp": legend_mode,
        "domainPolicy": domain_policy,
        "sourceKind": source_kind,
        "legend": legend_record,
        "queryable": True,
        "units": units,
        "storedMap": stored_map,
        **raster_metadata,
    }
    if existing:
        # The bounded raster service is attached after the first fixed-style
        # packaging pass. Preserve its allowlist identity when a publisher
        # subsequently enables current-view styling with --overwrite.
        for key in ("serviceAsset", "serviceRevision"):
            if existing.get(key):
                raster_tileset[key] = existing[key]
    if geometry:
        raster_tileset["geometryId"] = geometry
    if existing:
        tilesets[tilesets.index(existing)] = raster_tileset
    else:
        tilesets.append(raster_tileset)
    groups = manifest.setdefault("groups", [])
    group_id = f"ras-results-{plan_id}"
    if not any(group.get("id") == group_id for group in groups):
        groups.append(
            {
                "id": group_id,
                "name": f"Plan {plan_id}",
                "visible": False,
                "resultKind": result_kind,
            }
        )
    apply_manifest_v2(manifest)
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return RasterPackageSummary(
        manifest_path=manifest_path,
        pmtiles_path=output,
        source_cog=cog_path,
        raster_stats=stats,
        max_zoom=selected_max_zoom,
        layer_id=layer_id,
    )


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
    primary_geometry: str | None = None,
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
    _require_cli(_tippecanoe_command())
    _require_cli(_pmtiles_command())
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

        for terrain_entry in archive.get("terrain_sources", []):
            terrain_name = str(terrain_entry.get("terrain_name") or "Terrain")
            terrain_slug = _slug(terrain_name) or "terrain"
            group_id = f"ras-terrain-sources-{terrain_slug}"
            groups.append(
                {
                    "id": group_id,
                    "name": f"{terrain_name} Sources",
                    "visible": False,
                }
            )
            for layer in terrain_entry.get("layers", []):
                kind = str(layer.get("layer") or "")
                parquet_href = layer.get("parquet")
                if not kind or not parquet_href:
                    continue
                source_path_archive = archive_dir / parquet_href
                gdf = _to_wgs84(
                    gpd.read_parquet(source_path_archive),
                    source_path_archive,
                    project_crs,
                )
                if gdf.empty:
                    continue
                source_layer = f"{group_id}-{_slug(kind)}"
                source_path = work_dir / "terrain-sources" / f"{source_layer}.ndgeojson"
                count, geometry_types, bounds = _write_ndgeojson(gdf, source_path)
                geometry_overview_sources.append((source_layer, source_path))
                all_bounds.append(bounds)
                viewer_layer = {
                    "id": source_layer,
                    "name": _display_name(kind),
                    "sourceLayer": source_layer,
                    "groupId": group_id,
                    "visible": False,
                    "kind": kind,
                    "sourceKind": "terrain-source",
                    "style": _GEOMETRY_STYLES[kind].copy(),
                    "featureCount": count,
                    "geometryTypes": geometry_types,
                    "bounds": bounds,
                    "sort": _geometry_sort(kind),
                    "queryable": True,
                    "provenance": {
                        "source": "Native HEC-RAS terrain TIFF members",
                        "terrain": terrain_name,
                    },
                }
                geometry_overview_layers.append(viewer_layer)
                geometry_layers.append(viewer_layer)

        for terrain_entry in archive.get("terrain_modifications", []):
            terrain_name = str(terrain_entry.get("terrain_name") or "Terrain")
            terrain_slug = _slug(terrain_name) or "terrain"
            group_id = f"ras-terrain-modifications-{terrain_slug}"
            groups.append(
                {
                    "id": group_id,
                    "name": f"{terrain_name} Modifications",
                    "visible": False,
                }
            )
            for layer in terrain_entry.get("layers", []):
                kind = str(layer.get("layer") or "")
                parquet_href = layer.get("parquet")
                if not kind or not parquet_href:
                    continue
                source_path_archive = archive_dir / parquet_href
                gdf = _to_wgs84(
                    gpd.read_parquet(source_path_archive),
                    source_path_archive,
                    project_crs,
                )
                if gdf.empty:
                    continue
                source_layer = f"{group_id}-{_slug(kind)}"
                source_path = work_dir / "terrain-modifications" / f"{source_layer}.ndgeojson"
                count, geometry_types, bounds = _write_ndgeojson(gdf, source_path)
                geometry_overview_sources.append((source_layer, source_path))
                all_bounds.append(bounds)
                viewer_layer = {
                    "id": source_layer,
                    "name": _display_name(kind),
                    "sourceLayer": source_layer,
                    "groupId": group_id,
                    "visible": False,
                    "kind": kind,
                    "sourceKind": "terrain-modification",
                    "style": _GEOMETRY_STYLES[kind].copy(),
                    "featureCount": count,
                    "geometryTypes": geometry_types,
                    "bounds": bounds,
                    "sort": _geometry_sort(kind),
                    "queryable": True,
                    "provenance": {
                        "source": "HEC-RAS terrain modification HDF",
                        "terrain": terrain_name,
                        "sourceHdf": Path(str(terrain_entry.get("source_hdf") or "")).name,
                    },
                }
                geometry_overview_layers.append(viewer_layer)
                geometry_layers.append(viewer_layer)

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
                    variable_path = variable.get("parquet") or plan.get("parquet")
                    variable_filter = (
                        variable.get("filter_value")
                        if not variable.get("parquet") and plan.get("parquet")
                        else None
                    )
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
                        profile_columns = [profile_column]
                        if variable_filter:
                            profile_columns.append("layer")
                        profile_frame = pd.read_parquet(raw_path, columns=profile_columns)
                        if variable_filter:
                            profile_frame = profile_frame.loc[
                                profile_frame["layer"] == variable_filter
                            ]
                        profile_values = profile_frame[profile_column]
                        profiles = list(pd.unique(profile_values.dropna()))
                    for profile_index, profile in enumerate(profiles):
                        filters: dict[str, Any] = {}
                        if variable_filter:
                            filters["layer"] = variable_filter
                        if profile_column:
                            filters[profile_column] = profile
                        joined = _join_raw_result(
                            raw_path,
                            geometry,
                            index_column,
                            join_columns=join_columns,
                            filters=filters or None,
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
                        if variable_filter:
                            raw_result["archiveFilter"] = {
                                "column": "layer",
                                "value": variable_filter,
                            }
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
        "schema": "rascommander.maplibre.project/1",
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
    primary_geometry_group_id = None
    if primary_geometry:
        normalized_primary = _slug(primary_geometry)
        primary_geometry_group_id = (
            normalized_primary
            if normalized_primary.startswith("ras-geometry-")
            else f"ras-geometry-{normalized_primary}"
        )
    apply_maplibre_default_visibility(
        manifest,
        primary_geometry_group_id=(
            primary_geometry_group_id or _preferred_result_geometry_group_id(archive)
        ),
    )
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
    apply_manifest_v2(manifest, archive=archive)

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
        "pipe_conduits": 65,
        "pipe_nodes": 66,
        "cross_sections": 70,
        "bc_lines": 80,
        "terrain_modification_polygons": 200,
        "terrain_modification_lines": 210,
        "terrain_modification_control_points": 220,
        "terrain_source_footprints": 190,
    }
    return order.get(kind, 90)


def _is_detail_geometry(kind: str) -> bool:
    """Dense mesh delivery belongs in a high-zoom source, never the overview."""

    return kind in {"mesh_cells", "mesh_faces"}


def _preferred_result_geometry_group_id(archive: Mapping[str, Any]) -> str | None:
    """Choose the geometry associated with the first publishable result plan."""

    for plan in archive.get("results", []):
        geometry_id = str(plan.get("geom_id") or "").strip().lower()
        if geometry_id and plan.get("variables"):
            return f"ras-geometry-{geometry_id}"
    return None


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
    default_kinds = {"model_extents", "pipe_conduits", "pipe_nodes"}
    if is_2d:
        default_kinds.update({"mesh_areas", "mesh_cells", "breaklines", "refinement_regions"})
    else:
        default_kinds.update({"centerlines", "river_centerlines"})

    for layer in primary_layers:
        if layer.get("kind") in default_kinds:
            layer["visible"] = True
