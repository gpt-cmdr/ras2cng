"""Bounded statistics and styled-tile service for allowlisted numeric COGs."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
from threading import Lock
from typing import Any, Iterable, Mapping
from urllib.parse import unquote, urlparse


RASTER_ASSET_SCHEMA = "rascommander.raster-assets/v1"
_ASSET_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")


@dataclass(frozen=True)
class RasterAsset:
    """One local numeric COG approved for dynamic service access."""

    asset_id: str
    path: Path
    revision: str
    preset: str
    units: str = ""
    categorical: bool = False
    minimum: float | None = None
    maximum: float | None = None


@dataclass(frozen=True)
class RasterServiceSettings:
    """Resource and request limits for the isolated WebGIS service."""

    route_prefix: str = "/ras-raster"
    max_view_pixels: int = 2_097_152
    max_view_dimension: int = 4096
    tile_size: int = 256
    cache_entries: int = 512
    allowed_origins: tuple[str, ...] = ("https://rascommander.info",)


@dataclass(frozen=True)
class StylePreset:
    """Allowlisted color ramp; clients cannot submit arbitrary styles."""

    preset_id: str
    colors: tuple[tuple[int, int, int, int], ...]
    categorical_values: tuple[int, ...] = ()

    @property
    def categorical(self) -> bool:
        return bool(self.categorical_values)


STYLE_PRESETS: dict[str, StylePreset] = {
    "rasmapper.terrain": StylePreset(
        "rasmapper.terrain",
        (
            (105, 210, 179, 255),
            (68, 214, 74, 255),
            (200, 238, 47, 255),
            (242, 212, 58, 255),
            (240, 138, 36, 255),
            (200, 30, 30, 255),
            (127, 0, 0, 255),
            (217, 217, 217, 255),
            (255, 255, 255, 255),
        ),
    ),
    "rasmapper.depth": StylePreset(
        "rasmapper.depth",
        ((239, 246, 255, 210), (147, 197, 253, 225), (37, 99, 235, 235), (30, 58, 138, 245)),
    ),
    "rasmapper.velocity": StylePreset(
        "rasmapper.velocity",
        ((254, 249, 195, 220), (250, 204, 21, 230), (249, 115, 22, 235), (220, 38, 38, 240), (126, 34, 206, 245)),
    ),
    "rasmapper.water-surface-elevation": StylePreset(
        "rasmapper.water-surface-elevation",
        ((34, 197, 94, 225), (250, 204, 21, 230), (249, 115, 22, 235), (220, 38, 38, 240), (248, 250, 252, 245)),
    ),
    "rasmapper.inundation": StylePreset(
        "rasmapper.inundation",
        ((96, 165, 250, 215), (29, 78, 216, 235)),
    ),
    "rascommander.froude": StylePreset(
        "rascommander.froude",
        (
            (30, 64, 175, 225),
            (56, 189, 248, 225),
            (74, 222, 128, 230),
            (250, 204, 21, 235),
            (220, 38, 38, 245),
        ),
    ),
    "rascommander.shear-stress": StylePreset(
        "rascommander.shear-stress",
        (
            (254, 249, 195, 215),
            (251, 146, 60, 230),
            (220, 38, 38, 240),
            (126, 34, 206, 245),
        ),
    ),
    "rascommander.arrival-time": StylePreset(
        "rascommander.arrival-time",
        (
            (220, 38, 38, 240),
            (249, 115, 22, 235),
            (250, 204, 21, 230),
            (34, 197, 94, 225),
            (37, 99, 235, 240),
        ),
    ),
    "rascommander.duration": StylePreset(
        "rascommander.duration",
        (
            (239, 246, 255, 210),
            (147, 197, 253, 225),
            (59, 130, 246, 235),
            (67, 56, 202, 240),
            (88, 28, 135, 245),
        ),
    ),
    "rascommander.percent-inundated": StylePreset(
        "rascommander.percent-inundated",
        (
            (239, 246, 255, 210),
            (147, 197, 253, 225),
            (59, 130, 246, 235),
            (30, 64, 175, 245),
        ),
    ),
    "rascommander.difference": StylePreset(
        "rascommander.difference",
        ((30, 64, 175, 240), (147, 197, 253, 225), (248, 250, 252, 210), (252, 165, 165, 225), (185, 28, 28, 240)),
    ),
    "rascommander.depth-velocity": StylePreset(
        "rascommander.depth-velocity",
        ((254, 249, 195, 215), (250, 204, 21, 225), (249, 115, 22, 235), (190, 24, 93, 240), (88, 28, 135, 245)),
    ),
    "rascommander.hazard-aidr-2017": StylePreset(
        "rascommander.hazard-aidr-2017",
        ((214, 244, 210, 235), (166, 217, 106, 235), (255, 237, 111, 235), (253, 174, 97, 240), (239, 91, 82, 245), (165, 0, 38, 250)),
        (1, 2, 3, 4, 5, 6),
    ),
    "rascommander.threshold": StylePreset(
        "rascommander.threshold",
        ((0, 0, 0, 0), (37, 99, 235, 235)),
        (0, 1),
    ),
}


class RasterAssetCatalog:
    """Validated in-memory view of a data-root-relative asset catalog."""

    def __init__(self, data_root: Path, assets: Mapping[str, RasterAsset]):
        self.data_root = Path(data_root).resolve()
        self.assets = dict(assets)

    @classmethod
    def load(cls, catalog_path: Path, data_root: Path) -> "RasterAssetCatalog":
        catalog_path = Path(catalog_path)
        document = json.loads(catalog_path.read_text(encoding="utf-8"))
        if document.get("schema") != RASTER_ASSET_SCHEMA:
            raise ValueError(f"Unsupported raster asset catalog schema: {document.get('schema')!r}")
        root = Path(data_root).resolve()
        assets: dict[str, RasterAsset] = {}
        for asset_id, record in (document.get("assets") or {}).items():
            _validate_asset_id(asset_id)
            relative = Path(str(record.get("path") or ""))
            if relative.is_absolute() or not relative.parts:
                raise ValueError(f"Raster asset {asset_id!r} must use a relative path")
            path = (root / relative).resolve()
            if not path.is_relative_to(root):
                raise ValueError(f"Raster asset {asset_id!r} escapes the configured data root")
            if not path.is_file():
                raise FileNotFoundError(f"Raster asset {asset_id!r} does not exist: {path}")
            preset = str(record.get("preset") or "")
            if preset not in STYLE_PRESETS:
                raise ValueError(f"Raster asset {asset_id!r} uses unsupported preset {preset!r}")
            assets[asset_id] = RasterAsset(
                asset_id=asset_id,
                path=path,
                revision=str(record.get("revision") or _asset_revision(path)),
                preset=preset,
                units=str(record.get("units") or ""),
                categorical=bool(record.get("categorical", STYLE_PRESETS[preset].categorical)),
                minimum=_optional_float(record.get("minimum")),
                maximum=_optional_float(record.get("maximum")),
            )
        return cls(root, assets)

    def get(self, asset_id: str) -> RasterAsset:
        _validate_asset_id(asset_id)
        try:
            return self.assets[asset_id]
        except KeyError as error:
            raise KeyError(f"Unknown raster asset: {asset_id}") from error


def build_raster_asset_catalog(
    data_root: Path,
    output_path: Path,
    *,
    manifest_paths: Iterable[Path] | None = None,
    service_base_url: str = "/ras-raster",
    attach_manifests: bool = False,
    public_url_prefix: str | None = None,
) -> Path:
    """Build an allowlist from manifest v2 numeric resources under a data root."""

    root = Path(data_root).resolve()
    if not root.is_dir():
        raise NotADirectoryError(f"WebGIS data root does not exist: {root}")
    paths = [Path(path).resolve() for path in manifest_paths] if manifest_paths else _discover_manifest_paths(root)
    assets: dict[str, dict[str, Any]] = {}
    for manifest_path in paths:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("schema") != "rascommander.maplibre/v2":
            raise ValueError(f"Raster service catalog requires manifest v2: {manifest_path}")
        project_key = _project_key(root, manifest_path, manifest)
        modified = False
        for layer_id, layer in (manifest.get("layers") or {}).items():
            numeric_id = (layer.get("query") or {}).get("numericResource")
            resource = (manifest.get("resources") or {}).get(numeric_id) if numeric_id else None
            if not resource or resource.get("type") != "cog" or not resource.get("href"):
                continue
            path = _resolve_numeric_href(
                root,
                manifest_path,
                str(resource["href"]),
                public_url_prefix=public_url_prefix,
            )
            if not path.is_file():
                raise FileNotFoundError(f"Numeric COG for {layer_id!r} does not exist: {path}")
            legend_id = (layer.get("style") or {}).get("legendRef")
            legend = (manifest.get("legends") or {}).get(legend_id, {})
            preset = str(legend.get("preset") or _default_preset(layer))
            if preset not in STYLE_PRESETS:
                raise ValueError(f"Layer {layer_id!r} uses unsupported service preset {preset!r}")
            asset_id = f"{project_key}/{_slug(layer_id)}"
            _validate_asset_id(asset_id)
            revision = _asset_revision(path)
            domain = legend.get("domain") or {}
            assets[asset_id] = {
                "path": path.relative_to(root).as_posix(),
                "revision": revision,
                "preset": preset,
                "units": str(legend.get("units") or (layer.get("raster") or {}).get("units") or ""),
                "categorical": legend.get("type") == "categorical" or STYLE_PRESETS[preset].categorical,
                "minimum": _optional_float(domain.get("minimum")),
                "maximum": _optional_float(domain.get("maximum")),
            }
            if attach_manifests:
                resource["serviceAsset"] = asset_id
                resource["serviceRevision"] = revision
                for tileset in manifest.get("tilesets", []):
                    if tileset.get("id") == layer_id and tileset.get("type") == "raster":
                        tileset["serviceAsset"] = asset_id
                        tileset["serviceRevision"] = revision
                        break
                modified = True
        if attach_manifests and modified:
            manifest.setdefault("services", {})["numericRaster"] = {
                "baseUrl": service_base_url.rstrip("/"),
                "statisticsPath": "/stats",
                "samplePath": "/sample",
                "tilePath": "/tiles/{z}/{x}/{y}.png",
                "maxViewPixels": 2_097_152,
            }
            _atomic_json_write(manifest_path, manifest)

    document = {
        "schema": RASTER_ASSET_SCHEMA,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "dataRoot": ".",
        "assets": assets,
    }
    _atomic_json_write(Path(output_path), document)
    return Path(output_path)


def _discover_manifest_paths(root: Path) -> list[Path]:
    """Find viewer manifests without entering hidden transaction directories."""

    manifests: list[Path] = []
    for directory, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(name for name in dirnames if not name.startswith("."))
        if Path(directory).name == "viewer" and "manifest.json" in filenames:
            manifests.append(Path(directory) / "manifest.json")
    return sorted(manifests)


def compute_view_statistics(
    asset: RasterAsset,
    bbox: tuple[float, float, float, float],
    width: int,
    height: int,
    *,
    exact: bool = False,
    max_pixels: int = 2_097_152,
    max_dimension: int = 4096,
) -> dict[str, Any]:
    """Read one overview-bounded viewport and return robust or exact statistics."""

    from rasterio.crs import CRS
    from rio_tiler.io import Reader

    normalized_bbox = normalize_view_bbox(bbox, width, height)
    read_width, read_height = bounded_view_dimensions(
        width,
        height,
        max_pixels=max_pixels,
        max_dimension=max_dimension,
    )
    with Reader(str(asset.path)) as reader:
        image = reader.part(
            normalized_bbox,
            bounds_crs=CRS.from_epsg(4326),
            width=read_width,
            height=read_height,
            resampling_method="nearest",
        )
    statistics = image.statistics(percentiles=[2, 98])
    if not statistics:
        raise ValueError("Viewport contains no raster bands")
    band = next(iter(statistics.values()))
    values = band.model_dump()
    minimum = float(values["min"])
    maximum = float(values["max"])
    robust_minimum = float(values.get("percentile_2", minimum))
    robust_maximum = float(values.get("percentile_98", maximum))
    domain_minimum = minimum if exact else robust_minimum
    domain_maximum = maximum if exact else robust_maximum
    if not all(math.isfinite(value) for value in (minimum, maximum, domain_minimum, domain_maximum)):
        raise ValueError("Viewport statistics are not finite")
    return {
        "asset": asset.asset_id,
        "revision": asset.revision,
        "bbox": list(normalized_bbox),
        "sampleWidth": read_width,
        "sampleHeight": read_height,
        "exact": exact,
        "units": asset.units,
        "statistics": {
            "minimum": minimum,
            "maximum": maximum,
            "mean": float(values["mean"]),
            "stddev": float(values["std"]),
            "validPixels": int(values["valid_pixels"]),
            "maskedPixels": int(values["masked_pixels"]),
            "percentile2": robust_minimum,
            "percentile98": robust_maximum,
        },
        "domain": {"minimum": domain_minimum, "maximum": domain_maximum},
    }


def sample_raster_at_point(
    asset: RasterAsset,
    longitude: float,
    latitude: float,
) -> dict[str, Any]:
    """Read one allowlisted raster cell at a WGS84 point."""

    import numpy as np
    import rasterio
    from rasterio.warp import transform

    longitude = float(longitude)
    latitude = float(latitude)
    if not math.isfinite(longitude) or not math.isfinite(latitude):
        raise ValueError("Sample coordinates must be finite")
    if longitude < -180 or longitude > 180 or latitude < -90 or latitude > 90:
        raise ValueError("Sample coordinates must be valid WGS84 longitude and latitude")

    with rasterio.open(asset.path) as source:
        if source.crs is None:
            raise ValueError("Raster has no coordinate reference system")
        xs, ys = transform("EPSG:4326", source.crs, [longitude], [latitude])
        source_x, source_y = float(xs[0]), float(ys[0])
        bounds = source.bounds
        if (
            source_x < bounds.left
            or source_x >= bounds.right
            or source_y <= bounds.bottom
            or source_y > bounds.top
        ):
            return {
                "asset": asset.asset_id,
                "revision": asset.revision,
                "longitude": longitude,
                "latitude": latitude,
                "state": "outside",
                "units": asset.units,
            }
        row, column = source.index(source_x, source_y)
        sample = next(source.sample([(source_x, source_y)], indexes=1, masked=True))[0]

    base = {
        "asset": asset.asset_id,
        "revision": asset.revision,
        "longitude": longitude,
        "latitude": latitude,
        "sourceX": source_x,
        "sourceY": source_y,
        "row": int(row),
        "column": int(column),
        "units": asset.units,
    }
    if np.ma.is_masked(sample) or not math.isfinite(float(sample)):
        return {**base, "state": "nodata"}
    return {**base, "state": "value", "value": float(sample)}


def render_styled_tile(
    asset: RasterAsset,
    x: int,
    y: int,
    z: int,
    *,
    preset_id: str,
    minimum: float | None = None,
    maximum: float | None = None,
    tile_size: int = 256,
) -> bytes:
    """Render one PNG using an approved ramp and a bounded 256-pixel COG read."""

    from rio_tiler.io import Reader

    preset = get_style_preset(preset_id)
    if tile_size != 256:
        raise ValueError("Only 256-pixel tiles are supported")
    with Reader(str(asset.path)) as reader:
        image = reader.tile(
            x,
            y,
            z,
            tilesize=tile_size,
            resampling_method="nearest" if preset.categorical else "bilinear",
        )
    if preset.categorical:
        colormap = {
            value: color
            for value, color in zip(preset.categorical_values, preset.colors)
        }
    else:
        if minimum is None or maximum is None:
            raise ValueError("Continuous styled tiles require minimum and maximum")
        minimum = float(minimum)
        maximum = float(maximum)
        if not math.isfinite(minimum) or not math.isfinite(maximum) or maximum < minimum:
            raise ValueError("Styled tile range must be finite with maximum >= minimum")
        if maximum == minimum:
            epsilon = max(abs(minimum) * 1e-9, 1e-9)
            minimum -= epsilon
            maximum += epsilon
        image.rescale(in_range=((minimum, maximum),), out_range=((0, 255),))
        colormap = _linear_colormap(preset.colors)
    return image.render(img_format="PNG", colormap=colormap)


def create_raster_app(
    catalog_path: Path | None = None,
    data_root: Path | None = None,
    *,
    settings: RasterServiceSettings | None = None,
):
    """Create the isolated FastAPI application used by CLB-WebGIS."""

    from fastapi import FastAPI, HTTPException, Query, Response
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import JSONResponse

    settings = settings or _settings_from_environment()
    catalog_path = Path(catalog_path or os.environ.get("RAS2CNG_RASTER_CATALOG", "raster-assets.json"))
    data_root = Path(data_root or os.environ.get("RAS2CNG_RASTER_DATA_ROOT", "."))
    catalog = RasterAssetCatalog.load(catalog_path, data_root)
    cache = _LruCache(settings.cache_entries)
    prefix = "/" + settings.route_prefix.strip("/")
    app = FastAPI(title="RAS Commander Numeric Raster Service", version="1")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.allowed_origins),
        allow_methods=["GET"],
        allow_headers=["*"],
    )
    app.state.catalog = catalog

    @app.get(f"{prefix}/health")
    def health():
        return {"status": "ok", "assets": len(catalog.assets), "schema": RASTER_ASSET_SCHEMA}

    @app.get(f"{prefix}/stats")
    def statistics(
        asset: str = Query(..., min_length=1, max_length=300),
        bbox: str = Query(..., min_length=7, max_length=160),
        width: int = Query(1024, ge=1, le=settings.max_view_dimension),
        height: int = Query(768, ge=1, le=settings.max_view_dimension),
        exact: bool = Query(False),
        revision: str | None = Query(None, max_length=80),
    ):
        try:
            record = catalog.get(asset)
            _require_revision(record, revision)
            parsed_bbox = parse_bbox(bbox)
            normalized_bbox = normalize_view_bbox(parsed_bbox, width, height)
            read_width, read_height = bounded_view_dimensions(
                width,
                height,
                max_pixels=settings.max_view_pixels,
                max_dimension=settings.max_view_dimension,
            )
            key = (record.asset_id, record.revision, normalized_bbox, read_width, read_height, exact)
            result = cache.get(key)
            if result is None:
                result = compute_view_statistics(
                    record,
                    normalized_bbox,
                    read_width,
                    read_height,
                    exact=exact,
                    max_pixels=settings.max_view_pixels,
                    max_dimension=settings.max_view_dimension,
                )
                cache.put(key, result)
        except KeyError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return JSONResponse(
            content=result,
            headers=_cache_headers(record, revision, result),
        )

    @app.get(f"{prefix}/sample")
    def sample(
        asset: str = Query(..., min_length=1, max_length=300),
        lng: float = Query(..., ge=-180, le=180),
        lat: float = Query(..., ge=-90, le=90),
        revision: str | None = Query(None, max_length=80),
    ):
        try:
            record = catalog.get(asset)
            _require_revision(record, revision)
            key = ("sample", record.asset_id, record.revision, round(lng, 10), round(lat, 10))
            result = cache.get(key)
            if result is None:
                result = sample_raster_at_point(record, lng, lat)
                cache.put(key, result)
        except KeyError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return JSONResponse(
            content=result,
            headers=_cache_headers(record, revision, result),
        )

    @app.get(
        f"{prefix}/tiles/{{z}}/{{x}}/{{y}}.png",
        responses={200: {"content": {"image/png": {}}, "description": "Styled numeric raster tile"}},
    )
    def tile(
        z: int,
        x: int,
        y: int,
        asset: str = Query(..., min_length=1, max_length=300),
        preset: str | None = Query(None, max_length=100),
        minimum: float | None = Query(None),
        maximum: float | None = Query(None),
        revision: str | None = Query(None, max_length=80),
    ):
        try:
            if z < 0 or z > 24 or x < 0 or y < 0 or x >= 2**z or y >= 2**z:
                raise ValueError("Tile coordinates are outside the supported Web Mercator pyramid")
            record = catalog.get(asset)
            _require_revision(record, revision)
            selected_preset = preset or record.preset
            if selected_preset != record.preset:
                raise ValueError("Requested preset does not match the asset allowlist")
            key = (record.asset_id, record.revision, z, x, y, selected_preset, minimum, maximum)
            content = cache.get(key)
            if content is None:
                content = render_styled_tile(
                    record,
                    x,
                    y,
                    z,
                    preset_id=selected_preset,
                    minimum=minimum,
                    maximum=maximum,
                    tile_size=settings.tile_size,
                )
                cache.put(key, content)
        except KeyError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return Response(
            content,
            media_type="image/png",
            headers=_cache_headers(record, revision, content),
        )

    return app


def parse_bbox(value: str) -> tuple[float, float, float, float]:
    try:
        bounds = tuple(float(item.strip()) for item in value.split(","))
    except ValueError as error:
        raise ValueError("bbox must contain four comma-separated numbers") from error
    if len(bounds) != 4 or not all(math.isfinite(item) for item in bounds):
        raise ValueError("bbox must contain four finite numbers")
    west, south, east, north = bounds
    if west < -180 or east > 180 or south < -90 or north > 90:
        raise ValueError("bbox must use WGS84 longitude/latitude limits")
    if west >= east or south >= north:
        raise ValueError("bbox west/south must be less than east/north")
    return west, south, east, north


def normalize_view_bbox(
    bbox: tuple[float, float, float, float],
    width: int,
    height: int,
) -> tuple[float, float, float, float]:
    west, south, east, north = bbox
    x_step = (east - west) / max(1, width)
    y_step = (north - south) / max(1, height)
    precision = max(5, min(10, int(-math.floor(math.log10(max(x_step, y_step)))) + 1))
    return tuple(round(value, precision) for value in bbox)


def bounded_view_dimensions(
    width: int,
    height: int,
    *,
    max_pixels: int,
    max_dimension: int,
) -> tuple[int, int]:
    if width < 1 or height < 1:
        raise ValueError("Viewport dimensions must be positive")
    scale = min(1.0, max_dimension / width, max_dimension / height)
    if width * height * scale * scale > max_pixels:
        scale = min(scale, math.sqrt(max_pixels / (width * height)))
    return max(1, int(math.floor(width * scale))), max(1, int(math.floor(height * scale)))


def get_style_preset(preset_id: str) -> StylePreset:
    try:
        return STYLE_PRESETS[preset_id]
    except KeyError as error:
        raise ValueError(f"Unsupported raster style preset: {preset_id}") from error


class _LruCache:
    def __init__(self, max_entries: int):
        self.max_entries = max(1, max_entries)
        self._values: OrderedDict[Any, Any] = OrderedDict()
        self._lock = Lock()

    def get(self, key):
        with self._lock:
            if key not in self._values:
                return None
            self._values.move_to_end(key)
            return self._values[key]

    def put(self, key, value) -> None:
        with self._lock:
            self._values[key] = value
            self._values.move_to_end(key)
            while len(self._values) > self.max_entries:
                self._values.popitem(last=False)


def _linear_colormap(colors):
    import numpy as np

    positions = np.linspace(0.0, 1.0, len(colors))
    targets = np.linspace(0.0, 1.0, 256)
    channels = []
    for channel in range(4):
        channels.append(np.interp(targets, positions, [color[channel] for color in colors]))
    rgba = np.stack(channels, axis=1).round().astype("uint8")
    return {index: tuple(int(value) for value in row) for index, row in enumerate(rgba)}


def _resolve_numeric_href(
    root: Path,
    manifest_path: Path,
    href: str,
    *,
    public_url_prefix: str | None,
) -> Path:
    parsed = urlparse(href)
    if parsed.scheme in {"http", "https"}:
        if not public_url_prefix or not href.startswith(public_url_prefix):
            raise ValueError(f"Hosted numeric COG requires a matching public_url_prefix: {href}")
        relative = unquote(href[len(public_url_prefix):].split("?", 1)[0]).lstrip("/")
        path = (root / relative).resolve()
    else:
        path = (manifest_path.parent / unquote(parsed.path)).resolve()
    if not path.is_relative_to(root):
        raise ValueError(f"Numeric COG escapes the configured data root: {href}")
    return path


def _project_key(root: Path, manifest_path: Path, manifest: Mapping[str, Any]) -> str:
    try:
        relative = manifest_path.parent.parent.relative_to(root).as_posix()
        value = relative if relative not in {"", "."} else str(manifest.get("sourceProject") or "project")
    except ValueError:
        value = str(manifest.get("sourceProject") or manifest_path.parent.parent.name or "project")
    return "/".join(_slug(part) for part in value.split("/") if _slug(part))


def _default_preset(layer: Mapping[str, Any]) -> str:
    source_kind = layer.get("sourceKind")
    role = _slug(str(layer.get("role") or ""))
    if source_kind == "terrain":
        return "rasmapper.terrain"
    if "velocity" in role:
        return "rasmapper.velocity"
    if "water-surface" in role or role in {"wse", "compare-wse"}:
        return "rasmapper.water-surface-elevation"
    if role.startswith("compare-") or role == "terrain-mod-delta":
        return "rascommander.difference"
    if role.startswith("depth-velocity"):
        return "rascommander.depth-velocity"
    if role in {"froude", "froude-number"}:
        return "rascommander.froude"
    if role == "shear-stress":
        return "rascommander.shear-stress"
    if role == "arrival-time":
        return "rascommander.arrival-time"
    if role == "duration":
        return "rascommander.duration"
    if role in {"percent-inundated", "percent-time-inundated"}:
        return "rascommander.percent-inundated"
    if role == "hazard-class":
        return "rascommander.hazard-aidr-2017"
    if role == "inundation-threshold":
        return "rascommander.threshold"
    return "rasmapper.depth"


def _asset_revision(path: Path) -> str:
    stat = path.stat()
    value = f"{stat.st_size}:{stat.st_mtime_ns}".encode("ascii")
    return hashlib.sha256(value).hexdigest()[:16]


def _cache_headers(asset: RasterAsset, requested_revision: str | None, content: Any) -> dict[str, str]:
    if isinstance(content, bytes):
        payload = content
    else:
        payload = json.dumps(content, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {
        "Cache-Control": (
            "public, max-age=31536000, immutable"
            if requested_revision == asset.revision
            else "public, max-age=300"
        ),
        "ETag": f'"{hashlib.sha256(payload).hexdigest()[:24]}"',
        "X-Raster-Revision": asset.revision,
    }


def _require_revision(asset: RasterAsset, revision: str | None) -> None:
    if revision is not None and revision != asset.revision:
        raise ValueError("Requested raster revision is stale")


def _settings_from_environment() -> RasterServiceSettings:
    origins = tuple(
        value.strip()
        for value in os.environ.get("RAS2CNG_RASTER_ALLOWED_ORIGINS", "https://rascommander.info").split(",")
        if value.strip()
    )
    return RasterServiceSettings(
        route_prefix=os.environ.get("RAS2CNG_RASTER_ROUTE_PREFIX", "/ras-raster"),
        max_view_pixels=int(os.environ.get("RAS2CNG_RASTER_MAX_VIEW_PIXELS", "2097152")),
        max_view_dimension=int(os.environ.get("RAS2CNG_RASTER_MAX_VIEW_DIMENSION", "4096")),
        cache_entries=int(os.environ.get("RAS2CNG_RASTER_CACHE_ENTRIES", "512")),
        allowed_origins=origins,
    )


def _validate_asset_id(asset_id: str) -> None:
    segments = asset_id.split("/")
    if not _ASSET_ID.fullmatch(asset_id) or any(segment in {"", ".", ".."} for segment in segments):
        raise ValueError(f"Invalid raster asset id: {asset_id!r}")


def _optional_float(value) -> float | None:
    if value is None:
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-") or "item"


def _atomic_json_write(path: Path, document: Mapping[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)
