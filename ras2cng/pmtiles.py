"""PMTiles generation for rascmdr-parquet.

Vector tiles:
- GeoParquet -> GeoJSON -> tippecanoe -> PMTiles

Raster tiles:
- GeoTIFF -> MBTiles (gdal_translate) -> PMTiles (pmtiles convert)

Note: tippecanoe, gdal_translate, and pmtiles are command-line tools and must be installed
separately and available on PATH.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional
import subprocess
import tempfile

import geopandas as gpd


def _require_cli(exe: str):
    try:
        subprocess.run([exe, "--version"], check=False, capture_output=True, text=True)
    except FileNotFoundError:
        raise RuntimeError(
            f"Required CLI '{exe}' not found on PATH. Install it and retry."
        )


def generate_pmtiles_from_input(
    input_file: Path,
    output: Path,
    layer_name: str = "layer",
    min_zoom: Optional[int] = None,
    max_zoom: Optional[int] = None,
):
    input_path = Path(input_file)
    suf = input_path.suffix.lower()

    if suf in [".parquet", ".gpq"]:
        generate_vector_pmtiles(input_path, output, layer_name, min_zoom, max_zoom)
    elif suf in [".tif", ".tiff"]:
        generate_raster_pmtiles(input_path, output, min_zoom, max_zoom)
    else:
        raise ValueError(f"Unsupported input format: {input_path.suffix}")


def generate_vector_pmtiles(
    input_file: Path,
    output: Path,
    layer_name: str = "layer",
    min_zoom: Optional[int] = None,
    max_zoom: Optional[int] = None,
):
    _require_cli("tippecanoe")

    gdf = gpd.read_parquet(input_file)

    # Write GeoJSON without requiring Fiona/GDAL.
    with tempfile.NamedTemporaryFile(suffix=".geojson", delete=False) as tmp:
        geojson_path = Path(tmp.name)
        geojson_path.write_text(gdf.to_json(), encoding="utf-8")

    try:
        cmd = [
            "tippecanoe",
            "-o",
            str(output),
            "--layer",
            layer_name,
            "-zg",
            "--force",
        ]

        if min_zoom is not None:
            cmd.extend(["-Z", str(min_zoom)])
        if max_zoom is not None:
            cmd.extend(["-z", str(max_zoom)])

        cmd.append(str(geojson_path))

        p = subprocess.run(cmd, check=True, capture_output=True, text=True)
        if p.stderr:
            # tippecanoe logs to stderr even on success
            pass
    finally:
        geojson_path.unlink(missing_ok=True)


def generate_raster_pmtiles(
    input_file: Path,
    output: Path,
    min_zoom: Optional[int] = None,
    max_zoom: Optional[int] = None,
):
    _require_cli("gdal_translate")
    _require_cli("pmtiles")

    with tempfile.NamedTemporaryFile(suffix=".mbtiles", delete=False) as tmp:
        mbtiles_path = Path(tmp.name)

    try:
        gdal_cmd = [
            "gdal_translate",
            "-of",
            "MBTiles",
            "-co",
            "TILE_FORMAT=PNG",
        ]

        if min_zoom is not None:
            gdal_cmd.extend(["-co", f"MINZOOM={min_zoom}"])
        if max_zoom is not None:
            gdal_cmd.extend(["-co", f"MAXZOOM={max_zoom}"])

        gdal_cmd.extend([str(input_file), str(mbtiles_path)])
        subprocess.run(gdal_cmd, check=True, capture_output=True, text=True)

        pmtiles_cmd = ["pmtiles", "convert", str(mbtiles_path), str(output)]
        subprocess.run(pmtiles_cmd, check=True, capture_output=True, text=True)
    finally:
        mbtiles_path.unlink(missing_ok=True)
