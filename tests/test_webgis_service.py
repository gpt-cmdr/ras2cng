from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient
from morecantile import tms
import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

from ras2cng.webgis_service import (
    RASTER_ASSET_SCHEMA,
    RasterAsset,
    RasterAssetCatalog,
    RasterServiceSettings,
    bounded_view_dimensions,
    build_raster_asset_catalog,
    compute_view_statistics,
    create_raster_app,
    render_styled_tile,
)
from ras2cng.viewer_manifest import apply_manifest_v2


def _write_cog(path: Path) -> Path:
    values = np.linspace(0, 20, 256 * 256, dtype="float32").reshape((256, 256))
    values[:16, :16] = -9999
    temporary = path.with_name("source.tif")
    with rasterio.open(
        temporary,
        "w",
        driver="GTiff",
        width=256,
        height=256,
        count=1,
        dtype="float32",
        crs="EPSG:4326",
        transform=from_origin(-85.1, 40.2, 0.001, 0.001),
        nodata=-9999,
        tiled=True,
        blockxsize=128,
        blockysize=128,
    ) as destination:
        destination.write(values, 1)
        destination.update_tags(1, units="ft")
    from rasterio.shutil import copy as copy_raster

    copy_raster(temporary, path, driver="COG", compress="ZSTD", blocksize=128)
    temporary.unlink()
    return path


def _asset(path: Path) -> RasterAsset:
    return RasterAsset(
        asset_id="muncie/p03-depth",
        path=path,
        revision="revision-1",
        preset="rasmapper.depth",
        units="ft",
        minimum=0,
        maximum=20,
    )


def test_catalog_rejects_path_escape(tmp_path: Path):
    root = tmp_path / "data"
    root.mkdir()
    outside = tmp_path / "outside.tif"
    outside.write_bytes(b"tif")
    catalog = tmp_path / "catalog.json"
    catalog.write_text(
        json.dumps(
            {
                "schema": RASTER_ASSET_SCHEMA,
                "assets": {
                    "bad/asset": {
                        "path": "../outside.tif",
                        "preset": "rasmapper.depth",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="escapes"):
        RasterAssetCatalog.load(catalog, root)


def test_catalog_builder_attaches_service_asset_to_manifest(tmp_path: Path):
    root = tmp_path / "data"
    viewer = root / "projects" / "muncie" / "viewer"
    archive = root / "projects" / "muncie" / "archive"
    viewer.mkdir(parents=True)
    archive.mkdir(parents=True)
    _write_cog(archive / "depth.tif")
    manifest_path = viewer / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema": "rascommander.maplibre/v2",
                "sourceProject": "Muncie",
                "resources": {
                    "depth-numeric": {"type": "cog", "href": "../archive/depth.tif"}
                },
                "layers": {
                    "p03-depth-max": {
                        "sourceKind": "stored-map",
                        "role": "Depth",
                        "query": {"numericResource": "depth-numeric"},
                        "style": {
                            "legendRef": "legend-depth",
                            "domainPolicy": "current-view",
                        },
                        "raster": {"units": "ft"},
                    }
                },
                "legends": {
                    "legend-depth": {
                        "preset": "rasmapper.depth",
                        "units": "ft",
                        "domain": {"minimum": 0, "maximum": 20},
                    }
                },
                "tree": [],
                "groups": [
                    {"id": "ras-results-p03", "name": "Plan p03", "visible": False}
                ],
                "tilesets": [
                    {
                        "id": "p03-depth-max",
                        "name": "Depth Max",
                        "type": "raster",
                        "href": "tiles/depth.pmtiles",
                        "sourceCog": "../archive/depth.tif",
                        "groupId": "ras-results-p03",
                        "domainPolicy": "current-view",
                        "queryable": True,
                        "units": "ft",
                        "rasterStats": {"minimum": 0, "maximum": 20},
                        "legend": {
                            "type": "continuous",
                            "preset": "rasmapper.depth",
                            "domainPolicy": "current-view",
                        },
                        "storedMap": {
                            "plan": "p03",
                            "mapType": "depth",
                            "source": "RASMapper/RasProcess Stored Map",
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    output = root / "raster-assets.json"
    build_raster_asset_catalog(
        root,
        output,
        manifest_paths=[manifest_path],
        service_base_url="https://rascommander.info/ras-raster",
        attach_manifests=True,
    )

    catalog = json.loads(output.read_text(encoding="utf-8"))
    assert list(catalog["assets"]) == ["projects/muncie/p03-depth-max"]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    resource = manifest["resources"]["depth-numeric"]
    assert resource["serviceAsset"] == "projects/muncie/p03-depth-max"
    assert resource["serviceRevision"]
    assert manifest["services"]["numericRaster"]["baseUrl"].endswith("/ras-raster")
    tileset = manifest["tilesets"][0]
    assert tileset["serviceAsset"] == resource["serviceAsset"]

    apply_manifest_v2(manifest)
    regenerated = manifest["resources"]["p03-depth-max-numeric"]
    assert regenerated["serviceAsset"] == resource["serviceAsset"]
    assert regenerated["serviceRevision"] == resource["serviceRevision"]


def test_view_statistics_are_pixel_bounded_and_robust(tmp_path: Path):
    asset = _asset(_write_cog(tmp_path / "depth.tif"))
    result = compute_view_statistics(
        asset,
        (-85.09, 39.96, -84.86, 40.19),
        4000,
        3000,
        max_pixels=1_000_000,
        max_dimension=4096,
    )
    assert result["sampleWidth"] * result["sampleHeight"] <= 1_000_000
    assert result["statistics"]["minimum"] <= result["domain"]["minimum"]
    assert result["domain"]["maximum"] <= result["statistics"]["maximum"]
    assert result["statistics"]["validPixels"] > 0
    assert bounded_view_dimensions(4000, 3000, max_pixels=1_000_000, max_dimension=4096) == (1154, 866)


def test_styled_tile_and_fastapi_endpoints(tmp_path: Path):
    root = tmp_path / "data"
    root.mkdir()
    cog = _write_cog(root / "depth.tif")
    asset = _asset(cog)
    tile = tms.get("WebMercatorQuad").tile(-84.98, 40.05, 10)
    content = render_styled_tile(
        asset,
        tile.x,
        tile.y,
        tile.z,
        preset_id="rasmapper.depth",
        minimum=0,
        maximum=20,
    )
    assert content.startswith(b"\x89PNG\r\n\x1a\n")

    catalog_path = tmp_path / "catalog.json"
    catalog_path.write_text(
        json.dumps(
            {
                "schema": RASTER_ASSET_SCHEMA,
                "assets": {
                    asset.asset_id: {
                        "path": "depth.tif",
                        "revision": asset.revision,
                        "preset": asset.preset,
                        "units": asset.units,
                        "minimum": 0,
                        "maximum": 20,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    app = create_raster_app(
        catalog_path,
        root,
        settings=RasterServiceSettings(allowed_origins=("https://rascommander.info",)),
    )
    client = TestClient(app)

    health = client.get("/ras-raster/health")
    assert health.status_code == 200
    assert health.json()["assets"] == 1

    stats = client.get(
        "/ras-raster/stats",
        params={
            "asset": asset.asset_id,
            "bbox": "-85.09,39.96,-84.86,40.19",
            "width": 1200,
            "height": 800,
            "revision": asset.revision,
        },
    )
    assert stats.status_code == 200, stats.text
    assert stats.headers["cache-control"].endswith("immutable")
    domain = stats.json()["domain"]

    image = client.get(
        f"/ras-raster/tiles/{tile.z}/{tile.x}/{tile.y}.png",
        params={
            "asset": asset.asset_id,
            "preset": asset.preset,
            "minimum": domain["minimum"],
            "maximum": domain["maximum"],
            "revision": asset.revision,
        },
    )
    assert image.status_code == 200, image.text
    assert image.headers["content-type"] == "image/png"
    assert image.content.startswith(b"\x89PNG")

    rejected = client.get(
        f"/ras-raster/tiles/{tile.z}/{tile.x}/{tile.y}.png",
        params={"asset": asset.asset_id, "preset": "viridis", "minimum": 0, "maximum": 20},
    )
    assert rejected.status_code == 422
