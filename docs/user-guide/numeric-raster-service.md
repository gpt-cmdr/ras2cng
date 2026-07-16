# Numeric Raster Service

The optional WebGIS service supplies bounded current-view statistics and styled 256-pixel
tiles from authoritative numeric COGs. Precolored raster PMTiles remain the low-latency
dataset view and automatic fallback.

## Catalog And Manifest Attachment

Build an allowlist from published manifest v2 bundles after all COG paths are final:

```bash
ras2cng raster-service-catalog /srv/rascommander/data \
  /srv/rascommander/raster-assets.json \
  --attach-manifests \
  --service-base-url /ras-raster
```

The command resolves every numeric COG beneath the configured data root, records a revision,
and attaches `serviceAsset` and `serviceRevision` to both v2 resources and compatibility
tilesets. A later `apply_manifest_v2()` call therefore preserves the service contract.
Hosted COG URLs can be mapped to the local root with `--public-url-prefix`.

## Runtime

Install the `webgis` extra and run behind the site's reverse proxy:

```bash
pip install 'ras2cng[webgis]'
ras2cng raster-service /srv/rascommander/raster-assets.json \
  /srv/rascommander/data --host 127.0.0.1 --port 8087
```

The CLI rejects non-loopback listeners. The reverse proxy is the only public entry point.
The repository includes example systemd and Nginx configurations under `deploy/webgis/`.

## Request Limits

- Assets are selected by allowlisted IDs, never arbitrary paths or URLs.
- Statistics reads use source overviews and are capped by pixel count and dimensions.
- Styled tiles are fixed at 256 pixels and use only built-in RASMapper/RAS Commander presets.
- Asset revisions reject stale browser requests and make successful responses immutable.
- Categorical legends use fixed domains; only continuous layers support current-view ranges.
- Current-view mode uses the robust 2nd-98th percentile by default. Exact minimum/maximum is
  available for deliberate comparison.
- An in-memory bounded LRU avoids unbounded disk cache growth.

Environment settings include `RAS2CNG_RASTER_CATALOG`, `RAS2CNG_RASTER_DATA_ROOT`,
`RAS2CNG_RASTER_MAX_VIEW_PIXELS`, `RAS2CNG_RASTER_MAX_VIEW_DIMENSION`,
`RAS2CNG_RASTER_CACHE_ENTRIES`, and comma-separated `RAS2CNG_RASTER_ALLOWED_ORIGINS`.

Manifest validation and the Example Library publication gate reject a layer whose preferred
`domainPolicy` is `current-view` unless it has a numeric COG, catalog asset/revision, and the
top-level `services.numericRaster` endpoint contract.
