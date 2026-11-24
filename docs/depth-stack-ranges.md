# Depth stack range index (`ras2cng-depth-stack-ranges/v1`)

`ras2cng.depth_stack_ranges` lets a browser read selected profiles of a
`ras2cng-depth-stack/v1` NetCDF4 file with HTTP 206 range requests, without
HDF5 and without downloading the whole object. The NetCDF file stays an
ordinary, standard NetCDF4/CF-1.8 file; the index is a small JSON companion.

## API

| Function | Purpose |
|---|---|
| `copy_depth_stack_indexed(source, target) -> dict` | Create a NEW indexed copy of an existing depth stack and return its index. |
| `inspect_depth_stack_ranges(path) -> dict` | Re-inspect an indexed copy and return its index (reads only metadata and the 36-byte witness). |
| `validate_range_index(index) -> None` | Check that an index is closed, self-consistent and bounded. |
| `dumps_range_index(index) -> str` | Validate and serialize as compact JSON (`separators=(",", ":")`). |

`copy_depth_stack_indexed`:

1. Validates the source (rules below) and records its private SHA-256.
2. Copies the bytes to a `.partial` sibling of `target` and checks the copy hash.
3. Appends one contiguous, unfiltered NetCDF character variable
   `range_object_id(range_object_id_length=36)` holding a random canonical
   UUID4 (attributes `long_name`, `comment`, `range_index_schema`). Global
   attributes, `depth`, coordinates, CRS and profile variables are unchanged.
4. Verifies every compressed `depth` chunk is byte-identical to the source chunk
   (`read_direct_chunk`), so every stored pixel is preserved exactly. Nothing is
   rescaled, rewarped, rounded or recompressed.
5. Links the copy to `target` without replacing an existing file
   (`FileExistsError`), re-hashes the source (must be unchanged), and re-inspects
   the final file after close: offsets, lengths, length and identity must equal
   the returned index (chunk offsets also equal the source's). If any check
   after placement fails, the file this call created is removed. Malformed
   input, including a missing required attribute, raises `ValueError`.

Hashes are used only privately during the copy; neither the NetCDF file nor the
index contains source paths, source hashes or content digests. The source of a
copy must not carry `source_sha256_json` (write it with
`include_source_hashes=False`); such files are rejected instead of copied,
because deleting an HDF5 attribute does not scrub its old bytes.

## Accepted input (anything else raises `ValueError`)

- Globals exactly `Conventions=CF-1.8`, `schema=ras2cng-depth-stack/v1`,
  `role=display_derivative`, `provenance_json` (JSON string map, at most 64 KiB),
  `_NCProperties`. Variables exactly `flow, profile, x, y, crs, depth` (plus
  `range_object_id, range_object_id_length` on an indexed copy).
- `depth(flow,y,x)`: little-endian float32, `_FillValue` NaN, attributes exactly
  the writer set (no `scale_factor`, `add_offset`, `missing_value`, `valid_*`),
  `units` `ft` or `m`, `grid_mapping=crs`, `coordinates=profile`.
- Chunked layout `(1, min(256,height), min(256,width))`, every chunk allocated,
  `filter_mask` 0 on every chunk, no overlapping or out-of-file chunk.
- Filter pipeline exactly: HDF5 shuffle (id 2, element size 4) then deflate
  (id 1, level 1-9). Fletcher32, scale-offset, szip, n-bit and any other filter
  are rejected.
- `flow`: either `profile_index` (int32 `0..n-1`, units `1`, no `flow_units`) or
  `discharge` (finite, strictly monotonic float64, `flow_units` cfs/cms with CF
  units `ft3 s-1`/`m3 s-1`). Profile labels unique, nonblank, at most 256 bytes.
- `crs.epsg_code=EPSG:3857`; finite north-up `GeoTransform`; `x`/`y` float64
  centres exactly equal `origin + (i + 0.5) * spacing`.

## Bounds

| Constant | Value |
|---|---|
| `MAX_PROFILES` | 1024 |
| `MAX_AXIS` (width or height) | 32768 |
| `MAX_CHUNKS` | 65536 |
| `MAX_INDEX_BYTES` (compact JSON) | 1048576 |
| Per-chunk stored length | `0 < length <= decoded_length + 4096` |

For the measured 4608x4608x86 stack the index has 27,864 chunks (about 0.5 MB);
the 512x512x86 benchmark index is 5,702 bytes.

## Index fields (closed: no other fields at any level)

```json
{
 "schema": "ras2cng-depth-stack-ranges/v1",
 "object": {"object_id": "<UUID4>", "byte_length": 58126,
            "witness": {"offset": 56001, "length": 36, "encoding": "ascii"}},
 "variable": {"name": "depth", "dimensions": ["flow","y","x"], "dtype": "float32",
              "byteorder": "little", "shape": [P,H,W], "chunk_shape": [1,CY,CX],
              "fill_value": "NaN", "units": "ft|m"},
 "filters": [{"id": 2, "name": "shuffle", "element_size": 4},
             {"id": 1, "name": "deflate", "level": 4, "format": "zlib"}],
 "grid": {"crs": "EPSG:3857", "width": W, "height": H,
          "geotransform": [x0, dx, 0, y0, 0, dy], "x_first_center": x0+dx/2,
          "y_first_center": y0+dy/2},
 "profiles": {"labels": ["..."], "axis_semantics": "profile_index|discharge",
              "flows": null, "flow_units": null},
 "chunks": {"grid": [P,R,C], "order": "C", "decoded_length": CY*CX*4,
            "max_length": 12894, "filter_mask": 0,
            "offsets": [...], "lengths": [...]}
}
```

- `geotransform` is GDAL order; `dy < 0`. It describes the native source grid;
  any display resampling is a separate reader concern and must not relabel it.
- `profiles.flows` is `null` with `profile_index` (unknown discharge; the axis is
  dimensionless). With `discharge` it lists the values and `flow_units`.
- `chunks.offsets[i]`/`lengths[i]` are absolute byte offset and stored length in
  the NetCDF object for chunk `i = (p * R + r) * C + c`, covering pixels rows
  `r*CY .. min(H,(r+1)*CY)-1` and columns `c*CX .. min(W,(c+1)*CX)-1` of
  profile `p` (the index of `profiles.labels`).

## Decoding a chunk

1. Fetch `bytes = object[offset : offset + length]`.
2. Undo filters in reverse order: inflate as a **zlib (RFC 1950)** stream
   (browser `DecompressionStream("deflate")`), refusing output longer than
   `decoded_length`; the result must be exactly `decoded_length` bytes.
3. Unshuffle with element size 4: with `N = decoded_length / 4`,
   `out[4*i + k] = in[k*N + i]`.
4. Interpret as little-endian float32 `CY x CX`, row-major. Edge chunks are
   stored at full size; keep only the valid `h x w` top-left part.

Values: NaN is nodata, `0` is valid dry, values `<= 0` may be rendered
transparent by policy but are stored unchanged; no scale/offset applies.

## Identity and freshness protocol

The index binds to one exact object: `object_id` lives in the file at
`witness.offset` as 36 ASCII bytes. A reader must:

1. Fetch the witness range and require HTTP 206, the exact `Content-Range`
   (`bytes off-end/byte_length`, total equal to `object.byte_length`), the exact
   length, identity encoding (no `Content-Encoding`), and bytes equal to
   `object_id`. Capture the strong `ETag` (reject weak `W/` or missing).
2. Send every later chunk request with `If-Match: <ETag>`; reject anything other
   than 206 with the same total length and ETag (412 means stale: stop).

A copy rebuilt from the same source gets a new UUID, so a stale index cannot
silently pair with a new file. The UUID is public asset identity, not a digest.

## Coalescing

Chunks of one profile are usually adjacent in the file. A reader may merge the
ranges of selected chunks into one request when the merged span stays within its
response bound (for example 1 MiB, 2 concurrent requests). Every chunk is still
sliced by its exact offset/length, inflated with the same bound, and any gap
bytes between chunks are discarded and counted against the response budget.

## Wiring

The copy route reuses completed depth stacks without rerunning conversions:
call `copy_depth_stack_indexed(existing_nc, new_nc)` and publish `new_nc` with
`dumps_range_index(index)` as a small `ranges.json` beside it. The original
remains untouched; the indexed copy is the published large object.

## Metadata trust and qualification

The copy API preserves caller-supplied profile labels and `provenance_json`.
A supported NetCDF layout is not a privacy guarantee. The caller must qualify
those strings for publication, including the source file metadata, with its
unchanged public-content policy before copying or releasing. Private source
hash attributes and unrecognized attributes are rejected, not stripped: an
HDF5 edit cannot prove removed bytes no longer exist in the file. Use only
writer-produced public stacks; arbitrary edited HDF5 files with hidden or freed
private payloads are outside the accepted trust boundary.

The inspector now requires exact writer dimension bindings, closed variable
attribute sets, canonical writer EPSG:3857 WKT and projected coordinate
metadata. The JSON validator separately checks variable identity, axis labels
and semantics, unit declarations, transform centres, strict numeric types,
shape/byte budgets and non-overlapping chunk and identity witness ranges.
Malformed JSON values raise `ValueError`. Serialization is limited by UTF-8
bytes. None of these checks constitutes hydraulic approval.
