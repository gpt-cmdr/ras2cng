"""
Geometry export functions for rascmdr-parquet

Supports:
- HEC-RAS geometry HDF: *.g??.hdf
- HEC-RAS text geometry: *.g??
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Optional

import numpy as np
from ras_commander.hdf import HdfMesh, HdfXsec
from ras_commander.geom import GeomParser


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
    # e.g. model.g01.hdf -> ['.g01', '.hdf']
    return len(suf) >= 2 and suf[-1] == ".hdf" and suf[-2].startswith(".g")


def _is_text_geometry(path: Path) -> bool:
    # e.g. model.g01 -> suffix '.g01'
    return path.suffix.lower().startswith(".g") and not _is_hdf_geometry(path)


def export_geometry_layers(
    geom_input: Path,
    output: Path,
    layer: Optional[str] = None,
):
    """Export HEC-RAS geometry layers to GeoParquet.

    Args:
        geom_input: Path to *.g?? or *.g??.hdf
        output: Output GeoParquet path
        layer: One of: mesh_cells, cross_sections, centerlines
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
    """Export geometry from geometry HDF (*.g??.hdf)."""

    layers = {}

    if layer is None or layer == "mesh_cells":
        # Try full polygons first, then fall back to cell points.
        try:
            mesh_cells = HdfMesh.get_mesh_cell_polygons(hdf_path)
            if len(mesh_cells) > 0:
                layers["mesh_cells"] = mesh_cells
        except Exception as e:
            print(f"Warning: Could not extract mesh cell polygons: {e}")
            try:
                mesh_points = HdfMesh.get_mesh_cell_points(hdf_path)
                if len(mesh_points) > 0:
                    layers["mesh_cells"] = mesh_points
            except Exception as e2:
                print(f"Warning: Could not extract mesh cell points: {e2}")

    if layer is None or layer == "cross_sections":
        try:
            xs_sections = HdfXsec.get_cross_sections(hdf_path)
            if len(xs_sections) > 0:
                layers["cross_sections"] = xs_sections
        except Exception as e:
            print(f"Warning: Could not extract cross sections: {e}")

    if layer is None or layer == "centerlines":
        try:
            centerlines = HdfXsec.get_river_centerlines(hdf_path)
            if len(centerlines) > 0:
                layers["centerlines"] = centerlines
        except Exception as e:
            print(f"Warning: Could not extract centerlines: {e}")

    if not layers:
        raise ValueError("No geometry layers could be extracted from HDF geometry file")

    if layer is not None:
        if layer not in layers:
            raise ValueError(
                f"Requested layer '{layer}' not available. Available: {list(layers.keys())}"
            )
        _prepare_for_parquet(layers[layer]).to_parquet(output, compression="snappy", index=False)
        return

    # If no layer requested, prefer mesh_cells.
    if "mesh_cells" in layers:
        _prepare_for_parquet(layers["mesh_cells"]).to_parquet(output, compression="snappy", index=False)
        return

    # Otherwise, export the first available.
    first = next(iter(layers.values()))
    _prepare_for_parquet(first).to_parquet(output, compression="snappy", index=False)


def export_text_geometry(geom_path: Path, output: Path, layer: Optional[str] = None):
    """Export geometry from plain text *.g??."""

    safe_path = _ensure_utf8_readable(geom_path)

    layers = {}

    if layer is None or layer == "cross_sections":
        try:
            xs_cutlines = GeomParser.get_xs_cut_lines(safe_path)
            if len(xs_cutlines) > 0:
                layers["cross_sections"] = xs_cutlines
        except Exception as e:
            print(f"Warning: Could not extract XS cut lines: {e}")

    if layer is None or layer == "centerlines":
        try:
            centerlines = GeomParser.get_river_centerlines(safe_path)
            if len(centerlines) > 0:
                layers["centerlines"] = centerlines
        except Exception as e:
            print(f"Warning: Could not extract river centerlines: {e}")

    if not layers:
        raise ValueError("No geometry layers could be extracted from text geometry file")

    if layer is not None:
        if layer not in layers:
            raise ValueError(
                f"Requested layer '{layer}' not available. Available: {list(layers.keys())}"
            )
        _prepare_for_parquet(layers[layer]).to_parquet(output, compression="snappy", index=False)
        return

    if "cross_sections" in layers:
        _prepare_for_parquet(layers["cross_sections"]).to_parquet(output, compression="snappy", index=False)
        return

    first = next(iter(layers.values()))
    _prepare_for_parquet(first).to_parquet(output, compression="snappy", index=False)
