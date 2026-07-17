"""Bounded derivation of inundation polygons from stored depth rasters."""

from __future__ import annotations

import json
import math
import re
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from pathlib import PurePosixPath
from typing import Any, Iterator


DERIVED_BOUNDARY_SCHEMA = "ras2cng.derived-inundation-boundary/v1"
DERIVED_BOUNDARY_SOURCE = "RASMapper/RasProcess Depth Stored Map"
DERIVED_BOUNDARY_INTERPOLATION_AUTHORITY = "RASMapper/RasProcess source raster"
DERIVED_BOUNDARY_DERIVATION_AUTHORITY = "ras2cng"
DERIVED_BOUNDARY_COMPARISON = "depth > threshold"
DEFAULT_BOUNDARY_MAX_EDGES = 5_000_000
REQUIRED_SHAPEFILE_SUFFIXES = (".shp", ".shx", ".dbf", ".prj", ".cpg")
_OPTIONAL_SHAPEFILE_SUFFIXES = (".qix", ".sbn", ".sbx", ".fix")


@dataclass(frozen=True)
class DerivedBoundaryResult:
    """Published raster-derived inundation boundary and processing metrics."""

    output_path: Path
    provenance_path: Path
    feature_count: int
    wet_pixel_count: int
    edge_count: int
    edge_limit: int
    source_resolution: tuple[float, float]
    output_resolution: tuple[float, float]
    resampling: str

    @property
    def shapefile_path(self) -> Path:
        """Alias that makes the vector output type explicit to callers."""

        return self.output_path

    @property
    def output_shp(self) -> Path:
        """Backward-friendly alias matching the CLI argument name."""

        return self.output_path


class BoundaryEdgeLimitError(ValueError):
    """Raised before polygonization when the mask is too complex."""


def derive_inundation_boundary(
    depth_cog: Path,
    output_shp: Path,
    *,
    threshold: float = 0.0,
    resolution: float | None = None,
    max_edges: int = DEFAULT_BOUNDARY_MAX_EDGES,
    profile: str | None = None,
    units: str | None = None,
    source_identifier: str | None = None,
    block_size: int = 1024,
    batch_size: int = 2048,
) -> DerivedBoundaryResult:
    """Derive a 4-connected inundation polygon from a stored depth raster.

    Valid source samples are explicitly scaled using the raster band's scale
    and offset, then compared with the strict expression ``depth > threshold``.
    The native binary mask is built in fixed-size windows. If a coarser target
    resolution is requested, it is resampled with ``max`` so any wet source
    cell contributing to an output cell keeps that output cell wet.

    Polygonization does not begin until a separate pass has counted every
    wet/dry or wet/exterior edge and confirmed the configured limit.
    """

    import numpy as np
    import rasterio
    from rasterio.transform import from_origin
    from rasterio.warp import Resampling, reproject

    depth_cog = Path(depth_cog)
    output_shp = Path(output_shp)
    threshold = float(threshold)

    if not depth_cog.is_file():
        raise FileNotFoundError(f"Depth raster does not exist: {depth_cog}")
    if output_shp.suffix.lower() != ".shp":
        raise ValueError(f"Boundary output must be a .shp file: {output_shp}")
    if not math.isfinite(threshold):
        raise ValueError("Boundary threshold must be finite")
    if resolution is not None:
        resolution = float(resolution)
        if not math.isfinite(resolution) or resolution <= 0:
            raise ValueError("Boundary resolution must be a positive finite value")
    if not isinstance(max_edges, int) or isinstance(max_edges, bool) or max_edges <= 0:
        raise ValueError("Boundary max edges must be a positive integer")
    if block_size <= 0 or batch_size <= 0:
        raise ValueError("Boundary block and batch sizes must be positive")

    output_shp.parent.mkdir(parents=True, exist_ok=True)
    provenance_path = _provenance_path(output_shp)

    with rasterio.open(depth_cog) as source:
        if source.count < 1:
            raise ValueError(f"Depth raster has no bands: {depth_cog}")
        if source.crs is None:
            raise ValueError("Depth raster requires a CRS to create a .prj sidecar")

        source_resolution = tuple(float(value) for value in source.res)
        if any(not math.isfinite(value) or value <= 0 for value in source_resolution):
            raise ValueError(f"Depth raster has invalid resolution: {source_resolution}")

        if resolution is not None and any(
            resolution < value and not math.isclose(resolution, value)
            for value in source_resolution
        ):
            raise ValueError(
                "Boundary resolution would upsample the depth raster: "
                f"requested {resolution:g}, native "
                f"{source_resolution[0]:g} x {source_resolution[1]:g}"
            )

        scale = float(source.scales[0] if source.scales else 1.0)
        offset = float(source.offsets[0] if source.offsets else 0.0)
        source_nodata = source.nodata
        resolved_profile = profile or _infer_profile(depth_cog)
        resolved_units = normalize_depth_units(units or _infer_units(source))
        portable_source = _portable_source_identifier(
            depth_cog,
            source_identifier,
        )

        with tempfile.TemporaryDirectory(
            dir=output_shp.parent,
            prefix=f".{output_shp.stem}.raster-derived-",
        ) as temporary_name:
            temporary_dir = Path(temporary_name)
            native_mask_path = temporary_dir / "native-mask.tif"
            mask_profile = {
                "driver": "GTiff",
                "width": source.width,
                "height": source.height,
                "count": 1,
                "dtype": "uint8",
                "crs": source.crs,
                "transform": source.transform,
                "nodata": 0,
                "compress": "DEFLATE",
                "BIGTIFF": "IF_SAFER",
                "SPARSE_OK": "TRUE",
            }

            masked_pixel_count = 0
            nonfinite_pixel_count = 0
            with rasterio.open(native_mask_path, "w", **mask_profile) as destination:
                for window in _iter_windows(source.width, source.height, block_size):
                    values = source.read(1, window=window, masked=True)
                    raw = np.asarray(values.data, dtype="float64")
                    source_mask = np.ma.getmaskarray(values)
                    with np.errstate(over="ignore", invalid="ignore"):
                        scaled = raw * scale + offset
                    finite = np.isfinite(raw) & np.isfinite(scaled)
                    valid = ~source_mask & finite
                    wet = valid & (scaled > threshold)

                    masked_pixel_count += int(np.count_nonzero(source_mask))
                    nonfinite_pixel_count += int(
                        np.count_nonzero(~source_mask & ~finite)
                    )
                    destination.write(wet.astype("uint8"), 1, window=window)

            final_mask_path = native_mask_path
            output_resolution = source_resolution
            resampling = "none"
            output_width = source.width
            output_height = source.height
            output_transform = source.transform

            should_resample = resolution is not None and not all(
                math.isclose(resolution, value) for value in source_resolution
            )
            if should_resample:
                assert resolution is not None
                if source.transform.b != 0 or source.transform.d != 0:
                    raise ValueError(
                        "Boundary resolution override does not support rotated rasters"
                    )

                bounds = source.bounds
                output_width = max(1, math.ceil((bounds.right - bounds.left) / resolution))
                output_height = max(1, math.ceil((bounds.top - bounds.bottom) / resolution))
                output_transform = from_origin(
                    bounds.left,
                    bounds.top,
                    resolution,
                    resolution,
                )
                output_resolution = (resolution, resolution)
                resampling = "max"
                final_mask_path = temporary_dir / "resampled-mask.tif"
                resampled_profile = {
                    **mask_profile,
                    "width": output_width,
                    "height": output_height,
                    "transform": output_transform,
                }
                with rasterio.open(native_mask_path) as native_mask:
                    with rasterio.open(
                        final_mask_path, "w", **resampled_profile
                    ) as resampled_mask:
                        reproject(
                            source=rasterio.band(native_mask, 1),
                            destination=rasterio.band(resampled_mask, 1),
                            src_transform=native_mask.transform,
                            src_crs=native_mask.crs,
                            src_nodata=0,
                            dst_transform=output_transform,
                            dst_crs=native_mask.crs,
                            dst_nodata=0,
                            resampling=Resampling.max,
                            init_dest_nodata=True,
                            warp_mem_limit=64,
                        )

            with rasterio.open(final_mask_path) as final_mask:
                edge_count, wet_pixel_count = _count_mask_edges(
                    final_mask,
                    max_edges=max_edges,
                    block_size=block_size,
                )

            staged_shp = temporary_dir / output_shp.name
            feature_count = _polygonize_mask(
                final_mask_path,
                staged_shp,
                batch_size=batch_size,
            )
            _ensure_required_sidecars(staged_shp, source.crs)

            provenance = {
                "schema": DERIVED_BOUNDARY_SCHEMA,
                "generatedAt": datetime.now(timezone.utc).isoformat(),
                "sourceKind": "calculated",
                "source": DERIVED_BOUNDARY_SOURCE,
                "sourceMapType": "Depth",
                "interpolationAuthority": DERIVED_BOUNDARY_INTERPOLATION_AUTHORITY,
                "derivationAuthority": DERIVED_BOUNDARY_DERIVATION_AUTHORITY,
                "nativeRasMapperStoredPolygon": False,
                "comparison": DERIVED_BOUNDARY_COMPARISON,
                "threshold": threshold,
                "units": resolved_units,
                "profile": resolved_profile,
                "connectivity": 4,
                "sourceRaster": portable_source,
                "outputShapefile": output_shp.name,
                "sourceResolution": {
                    "x": source_resolution[0],
                    "y": source_resolution[1],
                },
                "outputResolution": {
                    "x": output_resolution[0],
                    "y": output_resolution[1],
                },
                "resampling": resampling,
                "scaleOffset": {"scale": scale, "offset": offset},
                "nodata": {
                    "sourceValue": _json_scalar(source_nodata),
                    "datasetMaskApplied": True,
                    "nonFiniteExcluded": True,
                    "maskedPixelCount": masked_pixel_count,
                    "nonFinitePixelCount": nonfinite_pixel_count,
                },
                "outputGrid": {
                    "width": output_width,
                    "height": output_height,
                    "transform": list(output_transform)[:6],
                },
                "wetPixelCount": wet_pixel_count,
                "featureCount": feature_count,
                "edgeCount": edge_count,
                "edgeLimit": max_edges,
            }
            _publish_family(staged_shp, output_shp, provenance_path, provenance)

    return DerivedBoundaryResult(
        output_path=output_shp,
        provenance_path=provenance_path,
        feature_count=feature_count,
        wet_pixel_count=wet_pixel_count,
        edge_count=edge_count,
        edge_limit=max_edges,
        source_resolution=source_resolution,
        output_resolution=output_resolution,
        resampling=resampling,
    )


def _iter_windows(width: int, height: int, block_size: int) -> Iterator[Any]:
    from rasterio.windows import Window

    for row_off in range(0, height, block_size):
        window_height = min(block_size, height - row_off)
        for col_off in range(0, width, block_size):
            yield Window(
                col_off,
                row_off,
                min(block_size, width - col_off),
                window_height,
            )


def _count_mask_edges(dataset, *, max_edges: int, block_size: int) -> tuple[int, int]:
    """Count wet-cell exterior edges without materializing the full mask."""

    import numpy as np
    from rasterio.windows import Window

    edge_count = 0
    wet_pixel_count = 0
    for window in _iter_windows(dataset.width, dataset.height, block_size):
        col_off = int(window.col_off)
        row_off = int(window.row_off)
        width = int(window.width)
        height = int(window.height)
        has_right_halo = col_off + width < dataset.width
        has_bottom_halo = row_off + height < dataset.height
        halo = dataset.read(
            1,
            window=Window(
                col_off,
                row_off,
                width + int(has_right_halo),
                height + int(has_bottom_halo),
            ),
        ).astype(bool, copy=False)
        core = halo[:height, :width]
        wet_pixel_count += int(np.count_nonzero(core))

        if has_right_halo:
            edge_count += int(np.count_nonzero(core != halo[:height, 1 : width + 1]))
        elif width > 1:
            edge_count += int(np.count_nonzero(core[:, :-1] != core[:, 1:]))

        if has_bottom_halo:
            edge_count += int(np.count_nonzero(core != halo[1 : height + 1, :width]))
        elif height > 1:
            edge_count += int(np.count_nonzero(core[:-1, :] != core[1:, :]))

        if row_off == 0:
            edge_count += int(np.count_nonzero(core[0, :]))
        if row_off + height == dataset.height:
            edge_count += int(np.count_nonzero(core[-1, :]))
        if col_off == 0:
            edge_count += int(np.count_nonzero(core[:, 0]))
        if col_off + width == dataset.width:
            edge_count += int(np.count_nonzero(core[:, -1]))

        if edge_count > max_edges:
            raise BoundaryEdgeLimitError(
                f"Boundary edge count exceeds limit {max_edges:,} before "
                f"polygonization (at least {edge_count:,} edges)"
            )

    return edge_count, wet_pixel_count


def _polygonize_mask(mask_path: Path, staged_shp: Path, *, batch_size: int) -> int:
    """Polygonize wet cells and append features to a shapefile in batches."""

    import geopandas as gpd
    import pyogrio
    import rasterio
    from rasterio.features import shapes
    from shapely.geometry import shape

    geometries = []
    feature_count = 0
    wrote_batch = False

    def write_batch() -> None:
        nonlocal wrote_batch
        frame = gpd.GeoDataFrame(
            {"wet": [1] * len(geometries)},
            geometry=geometries,
            crs=mask.crs,
        )
        pyogrio.write_dataframe(
            frame,
            staged_shp,
            driver="ESRI Shapefile",
            encoding="UTF-8",
            geometry_type="Polygon",
            append=wrote_batch,
            use_arrow=True,
        )
        wrote_batch = True

    with rasterio.open(mask_path) as mask:
        band = rasterio.band(mask, 1)
        for geometry, value in shapes(
            band,
            mask=band,
            connectivity=4,
            transform=mask.transform,
        ):
            if int(value) != 1:
                continue
            geometries.append(shape(geometry))
            feature_count += 1
            if len(geometries) >= batch_size:
                write_batch()
                geometries.clear()

        if geometries or not wrote_batch:
            write_batch()

    return feature_count


def _ensure_required_sidecars(staged_shp: Path, crs) -> None:
    cpg_path = staged_shp.with_suffix(".cpg")
    if not cpg_path.exists():
        cpg_path.write_text("UTF-8\n", encoding="ascii")

    prj_path = staged_shp.with_suffix(".prj")
    if not prj_path.exists():
        prj_path.write_text(crs.to_wkt(version="WKT1_ESRI"), encoding="ascii")

    missing = [
        staged_shp.with_suffix(suffix).name
        for suffix in REQUIRED_SHAPEFILE_SUFFIXES
        if not staged_shp.with_suffix(suffix).is_file()
    ]
    if missing:
        raise RuntimeError(
            "Derived boundary shapefile is incomplete: " + ", ".join(missing)
        )


def _publish_family(
    staged_shp: Path,
    output_shp: Path,
    provenance_path: Path,
    provenance: dict[str, Any],
) -> None:
    """Replace a shapefile family with rollback; place provenance last."""

    backup_dir = staged_shp.parent / "previous-family"
    backup_dir.mkdir()
    suffixes = REQUIRED_SHAPEFILE_SUFFIXES + _OPTIONAL_SHAPEFILE_SUFFIXES
    targets = [output_shp.with_suffix(suffix) for suffix in suffixes]
    targets.append(provenance_path)
    published: list[Path] = []
    backed_up: list[tuple[Path, Path]] = []
    provenance_temp = staged_shp.parent / (
        f".{provenance_path.name}.{uuid.uuid4().hex}.tmp"
    )

    try:
        for target in targets:
            if target.exists():
                backup = backup_dir / target.name
                target.replace(backup)
                backed_up.append((target, backup))

        for suffix in REQUIRED_SHAPEFILE_SUFFIXES:
            staged = staged_shp.with_suffix(suffix)
            target = output_shp.with_suffix(suffix)
            staged.replace(target)
            published.append(target)

        provenance_temp.write_text(
            json.dumps(provenance, indent=2, allow_nan=False) + "\n",
            encoding="utf-8",
        )
        provenance_temp.replace(provenance_path)
        published.append(provenance_path)
    except Exception:
        provenance_temp.unlink(missing_ok=True)
        for target in reversed(published):
            target.unlink(missing_ok=True)
        for target, backup in backed_up:
            backup.replace(target)
        raise


def _provenance_path(output_shp: Path) -> Path:
    if output_shp.stem.endswith(".raster-derived"):
        return output_shp.with_suffix(".provenance.json")
    return output_shp.with_name(
        f"{output_shp.stem}.raster-derived.provenance.json"
    )


def _infer_profile(depth_cog: Path) -> str:
    match = re.match(r"Depth \(([^)]*)\)", depth_cog.name, flags=re.IGNORECASE)
    return match.group(1) if match else "unspecified"


def _infer_units(source) -> str:
    candidates = []
    if source.units:
        candidates.extend(source.units)
    band_tags = source.tags(1)
    dataset_tags = source.tags()
    for tags in (band_tags, dataset_tags):
        for key in ("units", "Units", "UNITTYPE", "unit"):
            candidates.append(tags.get(key))
    for candidate in candidates:
        if candidate is not None and str(candidate).strip():
            return str(candidate).strip()
    return "unknown"


def _portable_source_identifier(
    depth_cog: Path,
    source_identifier: str | None,
) -> str:
    if source_identifier is None:
        return depth_cog.name

    normalized = str(source_identifier).strip()
    portable = PurePosixPath(normalized)
    if (
        not normalized
        or "\\" in normalized
        or "://" in normalized
        or normalized.startswith("~")
        or portable.is_absolute()
        or portable.drive
        or ".." in portable.parts
        or re.match(r"^[A-Za-z]:", normalized)
    ):
        raise ValueError(
            "Boundary source identifier must be a portable relative path"
        )
    return portable.as_posix()


def normalize_depth_units(value: str) -> str:
    """Normalize common depth-unit spellings to the provenance contract."""

    normalized = re.sub(r"[^a-z]+", " ", value.casefold()).strip()
    if normalized in {
        "ft",
        "feet",
        "foot",
        "us survey feet",
        "us customary",
        "english units",
    }:
        return "ft"
    if normalized in {
        "m",
        "meter",
        "meters",
        "metre",
        "metres",
        "si units",
        "metric units",
    }:
        return "m"
    raise ValueError(
        "Depth units must resolve to 'ft' or 'm'; supply units explicitly"
    )


def _json_scalar(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (int, str, bool)):
        return value
    numeric = float(value)
    if math.isnan(numeric):
        return "NaN"
    if math.isinf(numeric):
        return "Infinity" if numeric > 0 else "-Infinity"
    return numeric
