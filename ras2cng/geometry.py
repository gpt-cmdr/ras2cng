"""
Geometry export functions for ras2cng.

Supports:
- HEC-RAS geometry HDF: *.g??.hdf  (mesh, BCs, structures, XS, centerlines)
- HEC-RAS text geometry: *.g??     (XS cut lines, centerlines, storage areas)
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Optional

import geopandas as gpd
import numpy as np
import pandas as pd
from ras_commander.hdf import HdfMesh, HdfXsec, HdfBndry, HdfStruc
from ras_commander.geom import GeomParser
from ras_commander.geom import GeomStorage


# ---------------------------------------------------------------------------
# Layer dispatch table for HDF geometry files
# Maps layer_name -> (class, method_name)
# Each method signature: cls.method(hdf_path) -> GeoDataFrame
# ---------------------------------------------------------------------------

HDF_LAYERS: dict[str, tuple] = {
    "mesh_cells":           (None, None),           # handled specially (polygon/point fallback)
    "mesh_areas":           (HdfMesh, "get_mesh_areas"),
    "cross_sections":       (HdfXsec, "get_cross_sections"),
    "centerlines":          (HdfXsec, "get_river_centerlines"),
    "bc_lines":             (HdfBndry, "get_bc_lines"),
    "breaklines":           (HdfBndry, "get_breaklines"),
    "refinement_regions":   (HdfBndry, "get_refinement_regions"),
    "reference_lines":      (HdfBndry, "get_reference_lines"),
    "reference_points":     (HdfBndry, "get_reference_points"),
    "structures":           (HdfStruc, "get_structures"),
}

# All known layer names (for validation / documentation)
ALL_HDF_LAYERS = list(HDF_LAYERS.keys())
ALL_TEXT_LAYERS = ["cross_sections", "centerlines", "storage_areas"]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _prepare_for_parquet(gdf):
    """Convert ndarray columns to lists so pyarrow can serialize them."""
    for col in gdf.columns:
        if col == "geometry":
            continue
        sample = gdf[col].dropna().iloc[0] if len(gdf[col].dropna()) > 0 else None
        if isinstance(sample, np.ndarray):
            gdf[col] = gdf[col].apply(
                lambda x: x.tolist() if isinstance(x, np.ndarray) else x
            )
    return gdf


def _ensure_utf8_readable(geom_path: Path) -> Path:
    """If file isn't valid UTF-8, transcode from latin-1 to a temp copy."""
    try:
        geom_path.read_text(encoding="utf-8")
        return geom_path
    except UnicodeDecodeError:
        tmp = Path(tempfile.mkdtemp()) / geom_path.name
        content = geom_path.read_text(encoding="latin-1")
        tmp.write_text(content, encoding="utf-8")
        return tmp


def _is_hdf_geometry(path: Path) -> bool:
    suf = [s.lower() for s in path.suffixes]
    return len(suf) >= 2 and suf[-1] == ".hdf" and suf[-2].startswith(".g")


def _is_text_geometry(path: Path) -> bool:
    return path.suffix.lower().startswith(".g") and not _is_hdf_geometry(path)


def _extract_mesh_cells(hdf_path: Path) -> Optional[object]:
    """Extract mesh cells as polygons, falling back to points. Returns GDF or None."""
    try:
        gdf = HdfMesh.get_mesh_cell_polygons(hdf_path)
        if len(gdf) > 0:
            return gdf
    except Exception as e:
        print(f"Warning: Could not extract mesh cell polygons: {e}")
    try:
        gdf = HdfMesh.get_mesh_cell_points(hdf_path)
        if len(gdf) > 0:
            return gdf
    except Exception as e2:
        print(f"Warning: Could not extract mesh cell points: {e2}")
    return None


def _extract_hdf_layer(hdf_path: Path, layer: str) -> Optional[object]:
    """Extract a single named layer from an HDF geometry file. Returns GDF or None."""
    if layer == "mesh_cells":
        return _extract_mesh_cells(hdf_path)

    if layer not in HDF_LAYERS:
        raise ValueError(f"Unknown HDF layer '{layer}'. Available: {ALL_HDF_LAYERS}")

    cls, method_name = HDF_LAYERS[layer]
    try:
        gdf = getattr(cls, method_name)(hdf_path)
        if len(gdf) > 0:
            return gdf
    except Exception as e:
        print(f"Warning: Could not extract '{layer}': {e}")
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def export_geometry_layers(
    geom_input: Path,
    output: Path,
    layer: Optional[str] = None,
):
    """Export HEC-RAS geometry layers to GeoParquet.

    Args:
        geom_input: Path to *.g?? or *.g??.hdf
        output: Output GeoParquet path
        layer: Layer name (mesh_cells, cross_sections, centerlines, bc_lines,
               breaklines, refinement_regions, reference_lines, reference_points,
               structures, mesh_areas, storage_areas). None = auto-select best.
    """
    geom_path = Path(geom_input)

    if _is_hdf_geometry(geom_path):
        export_hdf_geometry(geom_path, output, layer=layer)
    elif _is_text_geometry(geom_path):
        export_text_geometry(geom_path, output, layer=layer)
    else:
        raise ValueError(
            f"Unsupported geometry file format: {geom_path.name} (suffixes={geom_path.suffixes})"
        )


def export_hdf_geometry(hdf_path: Path, output: Path, layer: Optional[str] = None):
    """Export geometry from a HDF geometry file (*.g??.hdf).

    When layer is None, prefers mesh_cells then falls back to first available.
    """
    if layer is not None:
        gdf = _extract_hdf_layer(hdf_path, layer)
        if gdf is None:
            raise ValueError(f"Layer '{layer}' could not be extracted from {hdf_path.name}")
        output.parent.mkdir(parents=True, exist_ok=True)
        _prepare_for_parquet(gdf).to_parquet(output, compression="snappy", index=False)
        return

    # Auto-select: try all layers, prefer mesh_cells
    layers = {}
    for lname in ALL_HDF_LAYERS:
        gdf = _extract_hdf_layer(hdf_path, lname)
        if gdf is not None:
            layers[lname] = gdf

    if not layers:
        raise ValueError("No geometry layers could be extracted from HDF geometry file")

    preferred = "mesh_cells" if "mesh_cells" in layers else next(iter(layers))
    output.parent.mkdir(parents=True, exist_ok=True)
    _prepare_for_parquet(layers[preferred]).to_parquet(output, compression="snappy", index=False)


def export_all_hdf_layers(
    hdf_path: Path,
    output_dir: Path,
    skip_empty: bool = True,
) -> dict[str, Path]:
    """Export all available geometry layers from a single HDF file.

    Args:
        hdf_path: Path to *.g??.hdf geometry file
        output_dir: Directory to write individual layer parquet files
        skip_empty: If True, silently skip layers that return no data

    Returns:
        Dict of {layer_name: parquet_path} for successfully written layers
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}

    for layer_name in ALL_HDF_LAYERS:
        out_path = output_dir / f"{layer_name}.parquet"
        try:
            gdf = _extract_hdf_layer(hdf_path, layer_name)
        except Exception as e:
            print(f"Warning: Could not extract '{layer_name}' from {hdf_path.name}: {e}")
            continue
        if gdf is None:
            if not skip_empty:
                print(f"Info: Layer '{layer_name}' not available in {hdf_path.name}")
            continue
        _prepare_for_parquet(gdf).to_parquet(out_path, compression="snappy", index=False)
        written[layer_name] = out_path

    return written


def export_text_geometry(geom_path: Path, output: Path, layer: Optional[str] = None):
    """Export geometry from a plain text geometry file (*.g??).

    Layers available from text files: cross_sections, centerlines, storage_areas
    """
    safe_path = _ensure_utf8_readable(geom_path)
    layers = {}

    if layer is None or layer == "cross_sections":
        try:
            gdf = GeomParser.get_xs_cut_lines(safe_path)
            if len(gdf) > 0:
                layers["cross_sections"] = gdf
        except Exception as e:
            print(f"Warning: Could not extract XS cut lines: {e}")

    if layer is None or layer == "centerlines":
        try:
            gdf = GeomParser.get_river_centerlines(safe_path)
            if len(gdf) > 0:
                layers["centerlines"] = gdf
        except Exception as e:
            print(f"Warning: Could not extract river centerlines: {e}")

    if layer is None or layer == "storage_areas":
        try:
            gdf = GeomStorage.get_storage_areas(safe_path)
            if len(gdf) > 0:
                layers["storage_areas"] = gdf
        except Exception as e:
            print(f"Warning: Could not extract storage areas: {e}")

    if not layers:
        raise ValueError("No geometry layers could be extracted from text geometry file")

    if layer is not None:
        if layer not in layers:
            raise ValueError(
                f"Layer '{layer}' not available. Available: {list(layers.keys())}"
            )
        output.parent.mkdir(parents=True, exist_ok=True)
        _prepare_for_parquet(layers[layer]).to_parquet(output, compression="snappy", index=False)
        return

    preferred = "cross_sections" if "cross_sections" in layers else next(iter(layers))
    output.parent.mkdir(parents=True, exist_ok=True)
    _prepare_for_parquet(layers[preferred]).to_parquet(output, compression="snappy", index=False)


def export_all_text_layers(
    geom_path: Path,
    output_dir: Path,
) -> dict[str, Path]:
    """Export all available geometry layers from a text geometry file.

    Args:
        geom_path: Path to *.g?? text geometry file
        output_dir: Directory to write individual layer parquet files

    Returns:
        Dict of {layer_name: parquet_path} for successfully written layers
    """
    safe_path = _ensure_utf8_readable(geom_path)
    output_dir.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}

    extractors = {
        "cross_sections": lambda p: GeomParser.get_xs_cut_lines(p),
        "centerlines":    lambda p: GeomParser.get_river_centerlines(p),
        "storage_areas":  lambda p: GeomStorage.get_storage_areas(p),
    }

    for layer_name, extractor in extractors.items():
        try:
            gdf = extractor(safe_path)
            if len(gdf) == 0:
                continue
            out_path = output_dir / f"{layer_name}.parquet"
            _prepare_for_parquet(gdf).to_parquet(out_path, compression="snappy", index=False)
            written[layer_name] = out_path
        except Exception as e:
            print(f"Warning: Could not extract '{layer_name}' from {geom_path.name}: {e}")

    return written


# ---------------------------------------------------------------------------
# Consolidated merge functions (v2 archive format)
# ---------------------------------------------------------------------------

def _hilbert_sort(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Sort GeoDataFrame rows by Hilbert curve index of geometry centroid.

    Uses DuckDB's hilbert_encode(). Falls back to original order if DuckDB
    is not installed or sorting fails.
    """
    if len(gdf) <= 1:
        return gdf

    try:
        import duckdb
    except ImportError:
        return gdf

    try:
        centroids = gdf.geometry.centroid
        pts = pd.DataFrame({
            "_row_idx": range(len(gdf)),
            "_x": centroids.x.values,
            "_y": centroids.y.values,
        })
        con = duckdb.connect()
        order = con.execute(
            "SELECT _row_idx FROM pts ORDER BY hilbert_encode([_x, _y]::DOUBLE[2])"
        ).fetchdf()["_row_idx"].values
        con.close()
        return gdf.iloc[order].reset_index(drop=True)
    except Exception:
        return gdf


def merge_all_layers(
    hdf_path: Optional[Path] = None,
    text_path: Optional[Path] = None,
    *,
    sort: bool = True,
) -> Optional[gpd.GeoDataFrame]:
    """Extract and merge all geometry layers into a single GeoDataFrame.

    HDF layers use their base names (e.g. ``mesh_cells``). Text layers get
    a ``_text`` suffix (e.g. ``cross_sections_text``). All layers are
    distinguished by a ``layer`` column.

    Args:
        hdf_path: Path to ``*.g??.hdf`` geometry file (or None)
        text_path: Path to ``*.g??`` text geometry file (or None)
        sort: If True, apply Hilbert spatial sort within each layer

    Returns:
        A merged GeoDataFrame with ``layer`` column, or None if nothing extracted
    """
    all_gdfs: list[gpd.GeoDataFrame] = []

    # --- HDF layers ---
    if hdf_path is not None:
        for layer_name in ALL_HDF_LAYERS:
            try:
                gdf = _extract_hdf_layer(hdf_path, layer_name)
            except Exception as e:
                print(f"Warning: Could not extract '{layer_name}' from {hdf_path.name}: {e}")
                continue
            if gdf is None or len(gdf) == 0:
                continue
            gdf = _prepare_for_parquet(gdf)
            gdf["layer"] = layer_name
            if sort:
                gdf = _hilbert_sort(gdf)
            all_gdfs.append(gdf)

    # --- Text layers ---
    if text_path is not None:
        safe_path = _ensure_utf8_readable(text_path)
        text_extractors = {
            "cross_sections": lambda p: GeomParser.get_xs_cut_lines(p),
            "centerlines":    lambda p: GeomParser.get_river_centerlines(p),
            "storage_areas":  lambda p: GeomStorage.get_storage_areas(p),
        }
        for layer_name, extractor in text_extractors.items():
            try:
                gdf = extractor(safe_path)
                if len(gdf) == 0:
                    continue
            except Exception as e:
                print(f"Warning: Could not extract '{layer_name}' from {text_path.name}: {e}")
                continue
            gdf = _prepare_for_parquet(gdf)
            gdf["layer"] = f"{layer_name}_text"
            if sort:
                gdf = _hilbert_sort(gdf)
            all_gdfs.append(gdf)

    if not all_gdfs:
        return None

    merged = pd.concat(all_gdfs, ignore_index=True)
    return gpd.GeoDataFrame(merged, geometry="geometry")
