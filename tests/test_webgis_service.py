from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient
from morecantile import tms
import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

import ras2cng.webgis_service as webgis_service
from ras2cng.webgis_service import (
    RASTER_ASSET_SCHEMA,
    RasterAsset,
    RasterAssetCatalog,
    ReleaseRasterCatalogStore,
    RasterServiceSettings,
    bounded_view_dimensions,
    build_raster_asset_catalog,
    compute_view_statistics,
    create_release_raster_app,
    create_raster_app,
    parse_byte_range,
    render_styled_tile,
    sample_raster_at_point,
    STYLE_PRESETS,
)
from ras2cng.viewer_manifest import apply_manifest_v2


def _write_cog(path: Path, *, value_offset: float = 0.0) -> Path:
    values = (
        np.linspace(0, 20, 256 * 256, dtype="float32").reshape((256, 256))
        + value_offset
    )
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


def _write_release(
    root: Path,
    release_id: str,
    *,
    value_offset: float = 0.0,
) -> tuple[Path, RasterAsset]:
    release_root = root / "releases" / release_id
    release_root.mkdir(parents=True)
    asset = _asset(
        _write_cog(release_root / "depth.tif", value_offset=value_offset)
    )
    (release_root / "raster-assets.json").write_text(
        json.dumps(
            {
                "schema": RASTER_ASSET_SCHEMA,
                "releaseId": release_id,
                "assets": {
                    asset.asset_id: {
                        "path": asset.path.name,
                        "revision": asset.revision,
                        "preset": asset.preset,
                        "units": asset.units,
                        "minimum": asset.minimum,
                        "maximum": asset.maximum,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    return release_root, asset


def test_all_continuous_stored_map_presets_are_allowlisted() -> None:
    assert {
        "rascommander.froude",
        "rascommander.shear-stress",
        "rascommander.depth-velocity",
        "rascommander.arrival-time",
        "rascommander.duration",
        "rascommander.percent-inundated",
    } <= set(STYLE_PRESETS)


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
    assert manifest["services"]["numericRaster"]["samplePath"] == "/sample"
    assert manifest["services"]["numericRaster"]["cogPath"] == "/cog"
    assert (
        manifest["services"]["numericRaster"]["cogRangeTransport"] == "header-or-query"
    )
    assert manifest["services"]["numericRaster"]["cogRangeParameter"] == "range"
    tileset = manifest["tilesets"][0]
    assert tileset["serviceAsset"] == resource["serviceAsset"]

    apply_manifest_v2(manifest)
    regenerated = manifest["resources"]["p03-depth-max-numeric"]
    assert regenerated["serviceAsset"] == resource["serviceAsset"]
    assert regenerated["serviceRevision"] == resource["serviceRevision"]


def test_release_catalog_store_loads_self_contained_catalogs(tmp_path: Path):
    root = tmp_path / "hec-ras-7.0"
    first_root, first_asset = _write_release(root, "release-one")
    second_root, _ = _write_release(root, "release-two")
    store = ReleaseRasterCatalogStore(root, max_entries=2)

    first = store.get("release-one")
    second = store.get("release-two")

    assert first.release_id == "release-one"
    assert first.get(first_asset.asset_id).path == first_root / "depth.tif"
    assert second.release_id == "release-two"
    assert second.data_root == second_root
    assert store.get("release-one") is first


def test_release_catalog_store_rejects_identity_mismatch(tmp_path: Path):
    root = tmp_path / "hec-ras-7.0"
    release_root, _ = _write_release(root, "release-one")
    catalog_path = release_root / "raster-assets.json"
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    catalog["releaseId"] = "other-release"
    catalog_path.write_text(json.dumps(catalog), encoding="utf-8")

    with pytest.raises(ValueError, match="does not match"):
        ReleaseRasterCatalogStore(root).get("release-one")


@pytest.mark.parametrize(
    "release_id",
    ("../escape", "nested/release", ".hidden", "", "white space"),
)
def test_release_catalog_store_rejects_unsafe_release_ids(
    tmp_path: Path,
    release_id: str,
):
    with pytest.raises(ValueError, match="Release ID"):
        ReleaseRasterCatalogStore(tmp_path).get(release_id)


def test_release_raster_app_discovers_catalog_without_restart(tmp_path: Path):
    root = tmp_path / "hec-ras-7.0"
    _, asset = _write_release(root, "release-one")
    client = TestClient(create_release_raster_app(root))
    _, second_asset = _write_release(root, "release-two")

    health = client.get("/ras-raster/health")
    ready = client.get("/ras-raster/releases/release-one/ready")
    sample = client.get(
        "/ras-raster/releases/release-one/sample",
        params={
            "asset": asset.asset_id,
            "lng": -84.98,
            "lat": 40.05,
            "revision": asset.revision,
        },
    )
    second_ready = client.get("/ras-raster/releases/release-two/ready")
    second_sample = client.get(
        "/ras-raster/releases/release-two/sample",
        params={
            "asset": second_asset.asset_id,
            "lng": -84.98,
            "lat": 40.05,
            "revision": second_asset.revision,
        },
    )
    missing = client.get("/ras-raster/releases/missing/ready")

    assert health.status_code == 200
    assert health.json()["catalogMode"] == "immutable-releases"
    assert ready.status_code == 200
    assert ready.json()["releaseId"] == "release-one"
    assert ready.json()["assets"] == 1
    assert sample.status_code == 200
    assert sample.json()["state"] == "value"
    assert second_ready.status_code == 200
    assert second_ready.json()["releaseId"] == "release-two"
    assert second_sample.status_code == 200
    assert missing.status_code == 404
    assert missing.headers["cache-control"] == "no-store"


def test_release_raster_app_reports_missing_assets_as_not_ready(tmp_path: Path):
    root = tmp_path / "hec-ras-7.0"
    release_root, _ = _write_release(root, "release-missing")
    (release_root / "depth.tif").unlink()
    client = TestClient(create_release_raster_app(root))

    response = client.get("/ras-raster/releases/release-missing/ready")

    assert response.status_code == 503
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["detail"] == {
        "status": "not-ready",
        "releaseId": "release-missing",
        "missingAssets": 1,
        "missingAssetExamples": ["muncie/p03-depth"],
    }


def test_legacy_routes_follow_current_without_cross_release_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    root = tmp_path / "hec-ras-7.0"
    _, first_asset = _write_release(root, "release-one")
    _, second_asset = _write_release(root, "release-two", value_offset=100)
    app = create_release_raster_app(root)
    selected_release = ["release-one"]
    monkeypatch.setattr(
        app.state.catalog_store,
        "current_release_id",
        lambda: selected_release[0],
    )
    client = TestClient(app)
    parameters = {
        "asset": first_asset.asset_id,
        "lng": -84.98,
        "lat": 40.05,
        "revision": first_asset.revision,
    }
    assert first_asset.revision == second_asset.revision

    first = client.get("/ras-raster/sample", params=parameters)
    ready = client.get("/ras-raster/ready")
    selected_release[0] = "release-two"
    second = client.get(
        "/ras-raster/sample",
        params={**parameters, "revision": second_asset.revision},
    )

    assert first.status_code == 200
    assert ready.status_code == 200
    assert ready.json()["releaseId"] == "release-one"
    assert second.status_code == 200
    assert first.json()["value"] < 25
    assert second.json()["value"] > 100


def test_legacy_routes_fail_closed_without_current_pointer(tmp_path: Path):
    root = tmp_path / "hec-ras-7.0"
    _, asset = _write_release(root, "release-one")
    client = TestClient(create_release_raster_app(root))

    response = client.get(
        "/ras-raster/sample",
        headers={"Origin": "https://rascommander.info"},
        params={
            "asset": asset.asset_id,
            "lng": -84.98,
            "lat": 40.05,
            "revision": asset.revision,
        },
    )

    assert response.status_code == 503
    assert response.headers["cache-control"] == "no-store"
    assert (
        response.headers["access-control-allow-origin"]
        == "https://rascommander.info"
    )
    assert response.json()["detail"] == "Current raster release unavailable"


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
    assert bounded_view_dimensions(
        4000, 3000, max_pixels=1_000_000, max_dimension=4096
    ) == (1154, 866)


def test_point_sample_reads_zstd_cog_and_reports_nodata(tmp_path: Path):
    asset = _asset(_write_cog(tmp_path / "depth.tif"))

    value = sample_raster_at_point(asset, -84.98, 40.05)
    nodata = sample_raster_at_point(asset, -85.099, 40.199)
    outside = sample_raster_at_point(asset, -86.0, 41.0)

    assert value["state"] == "value"
    assert 0 <= value["value"] <= 20
    assert value["units"] == "ft"
    assert nodata["state"] == "nodata"
    assert outside["state"] == "outside"


@pytest.mark.parametrize(
    ("value", "size", "expected"),
    [
        ("bytes=0-0", 100, (0, 0)),
        ("bytes=10-19", 100, (10, 19)),
        ("bytes=90-", 100, (90, 99)),
        ("bytes=-10", 100, (90, 99)),
        ("bytes=-200", 100, (0, 99)),
    ],
)
def test_parse_byte_range(value: str, size: int, expected: tuple[int, int]):
    assert parse_byte_range(value, size, max_bytes=256) == expected


@pytest.mark.parametrize(
    "value",
    [None, "", "items=0-1", "bytes=0-1,4-5", "bytes=100-101", "bytes=10-9"],
)
def test_parse_byte_range_rejects_invalid_or_multiple_ranges(value: str | None):
    with pytest.raises(ValueError):
        parse_byte_range(value, 100, max_bytes=256)


def test_parse_byte_range_enforces_response_size_limit():
    with pytest.raises(ValueError, match="service limit"):
        parse_byte_range("bytes=0-16", 100, max_bytes=16)


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
                "releaseId": "release-20260725",
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
        settings=RasterServiceSettings(
            allowed_origins=("https://rascommander.info",),
            max_cog_range_bytes=16,
        ),
    )
    client = TestClient(app)

    health = client.get("/ras-raster/health")
    assert health.status_code == 200
    assert health.json()["assets"] == 1
    assert health.json()["releaseId"] == "release-20260725"
    assert health.json()["catalogRevision"]
    assert health.headers["cache-control"] == "no-store"

    ready = client.get("/ras-raster/ready")
    assert ready.status_code == 200
    assert ready.json()["status"] == "ready"
    assert ready.headers["cache-control"] == "no-store"

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

    sample = client.get(
        "/ras-raster/sample",
        params={
            "asset": asset.asset_id,
            "lng": -84.98,
            "lat": 40.05,
            "revision": asset.revision,
        },
    )
    assert sample.status_code == 200, sample.text
    assert sample.json()["state"] == "value"
    assert sample.json()["units"] == "ft"
    assert sample.headers["cache-control"].endswith("immutable")

    cog_head = client.head(
        "/ras-raster/cog",
        params={"asset": asset.asset_id, "revision": asset.revision},
    )
    assert cog_head.status_code == 200
    assert int(cog_head.headers["content-length"]) == cog.stat().st_size
    assert cog_head.headers["accept-ranges"] == "bytes"
    assert cog_head.content == b""

    cog_range = client.get(
        "/ras-raster/cog",
        params={"asset": asset.asset_id, "revision": asset.revision},
        headers={"Range": "bytes=8-15"},
    )
    assert cog_range.status_code == 206
    assert cog_range.content == cog.read_bytes()[8:16]
    assert cog_range.headers["content-range"] == f"bytes 8-15/{cog.stat().st_size}"
    assert cog_range.headers["content-length"] == "8"
    assert cog_range.headers["accept-ranges"] == "bytes"
    assert cog_range.headers["cache-control"].endswith("immutable, no-transform")

    query_range = client.get(
        "/ras-raster/cog",
        params={
            "asset": asset.asset_id,
            "revision": asset.revision,
            "range": "bytes=16-23",
        },
    )
    assert query_range.status_code == 206
    assert query_range.content == cog.read_bytes()[16:24]
    assert query_range.headers["content-range"] == (f"bytes 16-23/{cog.stat().st_size}")

    suffix_query_range = client.get(
        "/ras-raster/cog",
        params={
            "asset": asset.asset_id,
            "revision": asset.revision,
            "range": "bytes=-9",
        },
    )
    assert suffix_query_range.status_code == 206
    assert suffix_query_range.content == cog.read_bytes()[-9:]

    open_ended_query_range = client.get(
        "/ras-raster/cog",
        params={
            "asset": asset.asset_id,
            "revision": asset.revision,
            "range": "bytes=0-",
        },
    )
    assert open_ended_query_range.status_code == 416
    assert "service limit" in open_ended_query_range.json()["detail"]
    assert open_ended_query_range.headers["cache-control"] == "no-store"

    conflicting_range = client.get(
        "/ras-raster/cog",
        params={
            "asset": asset.asset_id,
            "revision": asset.revision,
            "range": "bytes=16-23",
        },
        headers={"Range": "bytes=8-15"},
    )
    assert conflicting_range.status_code == 416

    missing_range = client.get(
        "/ras-raster/cog",
        params={"asset": asset.asset_id, "revision": asset.revision},
    )
    assert missing_range.status_code == 416
    assert missing_range.headers["content-range"] == f"bytes */{cog.stat().st_size}"

    oversized_range = client.get(
        "/ras-raster/cog",
        params={"asset": asset.asset_id, "revision": asset.revision},
        headers={"Range": "bytes=0-16"},
    )
    assert oversized_range.status_code == 416

    stale_range = client.get(
        "/ras-raster/cog",
        params={"asset": asset.asset_id, "revision": "stale"},
        headers={"Range": "bytes=0-0"},
    )
    assert stale_range.status_code == 422
    assert stale_range.headers["cache-control"] == "no-store"

    missing_asset = client.get(
        "/ras-raster/cog",
        params={"asset": "missing", "range": "bytes=0-0"},
    )
    assert missing_asset.status_code == 404
    assert missing_asset.headers["cache-control"] == "no-store"

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
        params={
            "asset": asset.asset_id,
            "preset": "viridis",
            "minimum": 0,
            "maximum": 20,
        },
    )
    assert rejected.status_code == 422
    assert rejected.headers["cache-control"] == "no-store"


def test_readiness_detects_missing_assets_after_startup(tmp_path: Path):
    root = tmp_path / "data"
    root.mkdir()
    cog = _write_cog(root / "depth.tif")
    asset = _asset(cog)
    catalog_path = tmp_path / "catalog.json"
    catalog_path.write_text(
        json.dumps(
            {
                "schema": RASTER_ASSET_SCHEMA,
                "releaseId": "release-missing",
                "assets": {
                    asset.asset_id: {
                        "path": cog.name,
                        "revision": asset.revision,
                        "preset": asset.preset,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    client = TestClient(create_raster_app(catalog_path, root))
    cog.unlink()

    ready = client.get("/ras-raster/ready")

    assert ready.status_code == 503
    assert ready.json()["status"] == "not-ready"
    assert ready.json()["missingAssets"] == 1
    assert ready.headers["cache-control"] == "no-store"


def test_unexpected_raster_errors_are_not_cacheable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    root = tmp_path / "data"
    root.mkdir()
    cog = _write_cog(root / "depth.tif")
    asset = _asset(cog)
    catalog_path = tmp_path / "catalog.json"
    catalog_path.write_text(
        json.dumps(
            {
                "schema": RASTER_ASSET_SCHEMA,
                "assets": {
                    asset.asset_id: {
                        "path": cog.name,
                        "revision": asset.revision,
                        "preset": asset.preset,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        webgis_service,
        "compute_view_statistics",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("GDAL failed")),
    )
    client = TestClient(
        create_raster_app(catalog_path, root),
        raise_server_exceptions=False,
    )

    response = client.get(
        "/ras-raster/stats",
        params={
            "asset": asset.asset_id,
            "bbox": "-85.09,39.96,-84.86,40.19",
            "revision": asset.revision,
        },
    )

    assert response.status_code == 500
    assert response.headers["cache-control"] == "no-store"
    assert "GDAL failed" not in response.text


def test_concurrency_limit_returns_non_cacheable_503(
    tmp_path: Path,
):
    root = tmp_path / "data"
    root.mkdir()
    cog = _write_cog(root / "depth.tif")
    asset = _asset(cog)
    catalog_path = tmp_path / "catalog.json"
    catalog_path.write_text(
        json.dumps(
            {
                "schema": RASTER_ASSET_SCHEMA,
                "assets": {
                    asset.asset_id: {
                        "path": cog.name,
                        "revision": asset.revision,
                        "preset": asset.preset,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    app = create_raster_app(
        catalog_path,
        root,
        settings=RasterServiceSettings(max_concurrent_operations=1),
    )
    client = TestClient(app)

    with app.state.operation_limiter.slot():
        response = client.get(
            "/ras-raster/sample",
            params={
                "asset": asset.asset_id,
                "lng": -84.98,
                "lat": 40.05,
                "revision": asset.revision,
            },
        )

    assert response.status_code == 503
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["retry-after"] == "1"
