"""PMTiles generation for the ``ras2cng pmtiles`` CLI surface.

Vector tiles:
- GeoParquet -> reproject to EPSG:4326 -> newline-delimited GeoJSON ->
  tippecanoe -> PMTiles, or GeoParquet -> freestiler -> PMTiles when
  ``RAS2CNG_VECTOR_TILER`` resolves to freestiler (see
  :mod:`ras2cng.vector_tiles`)

Raster tiles:
- delegated to :mod:`ras2cng.maplibre`, which colorizes at native resolution,
  warps to an exact Web Mercator zoom, and builds the tile pyramid.

Note: tippecanoe, gdal, and pmtiles are command-line tools and must be
installed separately and available on PATH.  freestiler is the optional
``ras2cng[freestiler]`` extra.

:mod:`ras2cng.maplibre` is the maintained implementation of both jobs and
streams its inputs; this module is the older single-file CLI surface kept for
``ras2cng pmtiles``.
"""

from __future__ import annotations

import logging
from pathlib import Path
import subprocess
import tempfile
from typing import Optional

import geopandas as gpd

LOGGER = logging.getLogger(__name__)


def _require_cli(exe: str):
    try:
        subprocess.run([exe, "--version"], check=False, capture_output=True, text=True)
    except FileNotFoundError:
        raise RuntimeError(
            f"Required CLI '{exe}' not found on PATH. Install it and retry."
        )


def _log_tool_output(tool: str, stderr: str | None) -> None:
    """Surface a tool's diagnostics rather than discarding them.

    Tippecanoe reports dropped features and oversized tiles on stderr even when
    it exits zero.  Swallowing that turns silent data loss into an invisible
    failure.
    """

    for line in (stderr or "").splitlines():
        text = line.strip()
        if not text:
            continue
        if any(token in text.lower() for token in ("dropp", "exceed", "too large", "warning")):
            LOGGER.warning("%s: %s", tool, text)
        else:
            LOGGER.debug("%s: %s", tool, text)


def generate_pmtiles_from_input(
    input_file: Path,
    output: Path,
    layer_name: str = "layer",
    min_zoom: Optional[int] = None,
    max_zoom: Optional[int] = None,
    *,
    mesh_names: Optional[list[str]] = None,
    source_crs: Optional[str] = None,
    threads: Optional[int] = None,
):
    input_path = Path(input_file)
    suf = input_path.suffix.lower()

    if suf == ".hdf":
        generate_mesh_pmtiles(
            input_path, output, layer_name=layer_name,
            min_zoom=10 if min_zoom is None else min_zoom,
            max_zoom=14 if max_zoom is None else max_zoom,
            mesh_names=mesh_names, source_crs=source_crs, threads=threads,
        )
    elif mesh_names is not None or source_crs is not None or threads is not None:
        raise ValueError("Mesh selection, source CRS override and threads apply only to HDF input")
    elif suf in [".parquet", ".gpq"]:
        generate_vector_pmtiles(input_path, output, layer_name, min_zoom, max_zoom)
    elif suf in [".tif", ".tiff"]:
        generate_raster_pmtiles(input_path, output, min_zoom, max_zoom)
    else:
        raise ValueError(f"Unsupported input format: {input_path.suffix}")


def generate_mesh_pmtiles(
    input_file: Path,
    output: Path,
    *,
    layer_name: str = "mesh_cells",
    mesh_names: Optional[list[str]] = None,
    properties: Optional[dict] = None,
    min_zoom: int = 10,
    max_zoom: int = 14,
    source_crs: Optional[str] = None,
    threads: Optional[int] = None,
) -> Path:
    """Tile HDF mesh cells through Rust, using pyproj for bulk reprojection.

    Requires the separately built ras-commander-fast-hdf extension. The source
    may be geometry HDF or a computed plan containing geometry. Properties are
    column arrays keyed by mesh name, indexed by original cell ID. All selected
    meshes share a source layer, with mesh_name/cell_id properties preserving
    identity. This explicit HDF route does not use the GeoParquet tiler selector.
    """
    from ras_commander.fast_hdf import export_mesh_pmtiles
    from ras2cng.cog import atomic_output

    input_file = Path(input_file)
    output = Path(output)
    if input_file.resolve() == output.resolve():
        raise ValueError("Output must not replace the source HDF")
    with atomic_output(output, suffix=".pmtiles") as staged:
        export_mesh_pmtiles(
            input_file, staged, layer_name=layer_name, mesh_names=mesh_names,
            properties=properties, min_zoom=min_zoom, max_zoom=max_zoom,
            source_crs=source_crs, threads=threads, backend="rust",
        )
    return output


def _write_ndgeojson(gdf: gpd.GeoDataFrame, path: Path) -> int:
    """Write features one line at a time as newline-delimited GeoJSON.

    ``GeoDataFrame.to_json()`` builds the entire layer as a single Python string
    before anything is written, which a RAS mesh of millions of cells will not
    survive.
    """

    import json

    count = 0
    with path.open("w", encoding="utf-8") as handle:
        for feature in gdf.iterfeatures(drop_id=True, na="null", show_bbox=False):
            json.dump(feature, handle, default=str, separators=(",", ":"))
            handle.write("\n")
            count += 1
    return count


def _to_wgs84(gdf: gpd.GeoDataFrame, source: Path) -> gpd.GeoDataFrame:
    """Reproject to EPSG:4326, which is the only CRS tippecanoe accepts.

    Tippecanoe reads RFC 7946 GeoJSON, i.e. WGS84 lon/lat, and does not
    reproject.  Archive GeoParquet from this repo is written in the model's
    native CRS -- Texas Centric Albers or a State Plane zone in feet for these
    projects.  Feeding State Plane feet to tippecanoe yields coordinates in the
    millions, which are clamped or wrapped into a meaningless tileset with no
    error at all: a valid, wrong ``.pmtiles``.
    """

    if gdf.crs is None:
        raise ValueError(
            f"GeoParquet layer has no CRS, so it cannot be reprojected for tiling: {source}. "
            "Set a CRS on the source before generating tiles."
        )
    if gdf.crs.to_epsg() == 4326:
        return gdf
    return gdf.to_crs("EPSG:4326")


# Zoom range when the caller gives none and the backend cannot guess one
# (freestiler has no equivalent of tippecanoe's ``-zg``).
_DEFAULT_MIN_ZOOM = 0
_DEFAULT_MAX_ZOOM = 14


def generate_vector_pmtiles(
    input_file: Path,
    output: Path,
    layer_name: str = "layer",
    min_zoom: Optional[int] = None,
    max_zoom: Optional[int] = None,
):
    from ras2cng import vector_tiles

    backend = vector_tiles.vector_tiler_backend(
        required_commands=(vector_tiles.tippecanoe_command(),)
    )
    if backend == vector_tiles.FREESTILER:
        vector_tiles.require_freestiler()
    else:
        _require_cli(vector_tiles.tippecanoe_command())

    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)

    from ras2cng.geoparquet import drop_covering_columns

    # Archive bbox covering columns (struct or flat) are index data, not
    # feature attributes; keep them out of the tiles.
    gdf = _to_wgs84(drop_covering_columns(gpd.read_parquet(input_file)), Path(input_file))

    if backend == vector_tiles.FREESTILER:
        if gdf.empty:
            raise ValueError(f"No features to tile in {input_file}")
        vector_tiles.run_freestiler(
            output,
            {layer_name: gdf},
            _DEFAULT_MIN_ZOOM if min_zoom is None else min_zoom,
            _DEFAULT_MAX_ZOOM if max_zoom is None else max_zoom,
        )
        return

    from ras2cng.cog import atomic_output

    with tempfile.TemporaryDirectory(prefix="ras2cng-pmtiles-") as scratch:
        geojson_path = Path(scratch) / "layer.ndgeojson"
        feature_count = _write_ndgeojson(gdf, geojson_path)
        if not feature_count:
            raise ValueError(f"No features to tile in {input_file}")

        # Write to a staged sibling so a failure part-way through cannot leave a
        # truncated tileset at the published path.
        with atomic_output(output) as staged:
            cmd = [
                vector_tiles.tippecanoe_command(),
                "-o",
                str(staged),
                "--layer",
                layer_name,
                "-zg",
                "--force",
                # Adjacent RAS mesh cells share edges; simplified independently
                # those edges diverge and open slivers between cells at low zoom.
                "--no-simplification-of-shared-nodes",
            ]

            if min_zoom is not None:
                cmd.extend(["-Z", str(min_zoom)])
            if max_zoom is not None:
                cmd.extend(["-z", str(max_zoom)])

            cmd.append(str(geojson_path))

            completed = subprocess.run(cmd, check=True, capture_output=True, text=True)
            _log_tool_output("tippecanoe", completed.stderr)


def generate_raster_pmtiles(
    input_file: Path,
    output: Path,
    min_zoom: Optional[int] = None,
    max_zoom: Optional[int] = None,
    *,
    map_type: str = "depth",
):
    """Render a numeric raster to PNG PMTiles via the maplibre pipeline.

    This used to run ``gdal_translate -of MBTiles -co TILE_FORMAT=PNG`` directly
    on the source.  Against a float32 depth or WSE COG that produces an 8-bit
    image with no color ramp and no alpha -- nodata renders as opaque black --
    and it built no overviews at all, so the ``MINZOOM`` creation option had
    nothing to apply to.

    :func:`ras2cng.maplibre._render_raster_pmtiles` is the correct
    implementation of this job: it applies the color ramp at native resolution
    before any downsampling, warps to an exact Web Mercator zoom resolution, and
    builds the pyramid.
    """

    from ras2cng import maplibre

    input_path = Path(input_file)
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)

    backend = maplibre._raster_backend_for(input_path)

    info = maplibre._gdalinfo(input_path)
    bands = info.get("bands") or []
    if not bands:
        raise ValueError(f"Raster has no readable bands: {input_path}")
    band = bands[0]
    stats = {
        "minimum": float(band.get("minimum", 0.0)),
        "maximum": float(band.get("maximum", 1.0)),
    }

    def ramp_writer(path: Path) -> None:
        maplibre._result_color_ramp(stats, map_type, path)

    with tempfile.TemporaryDirectory(prefix="ras2cng-raster-pmtiles-") as scratch:
        maplibre._render_raster_pmtiles(
            input_path,
            output,
            ramp_writer=ramp_writer,
            max_zoom=max_zoom,
            scratch_dir=Path(scratch),
            prefix="cli-raster",
            backend=backend,
        )
