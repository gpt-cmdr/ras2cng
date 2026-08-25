"""Shared Cloud Optimized GeoTIFF policy for ras2cng.

Every COG this repository writes goes through the helpers here so the overview
method, creation options, validation gate, and atomic replacement are decided in
exactly one place.  Downstream pipelines (RBFS ``clb_tx_webmap``, the LWI
webmap) consume ras2cng output, so a default fixed here is a default they
inherit instead of re-deriving.

Three things in this module are not the obvious implementation, and the reasons
matter:

``build_area_matched_overviews``
    A flood result raster is a continuous value *plus* an implicit wet/dry mask
    carried by nodata.  GDAL's ``average`` decimation marks a coarse cell valid
    when *any* contributing sub-cell is valid, so the wet mask grows at every
    overview level and the published inundation extent is larger than the model
    produced.  ``nearest`` preserves neither area nor value.  The builder here
    keeps the level-0 wet **area** by solving for a per-level coverage threshold
    and writes the **mean of wet contributors** for every cell it keeps wet.

``area_matched_cog``
    GDAL will not let a caller supply overview pixels directly, and the osgeo
    Python bindings are not a dependency.  The levels are therefore written as
    sidecar GeoTIFFs, attached to a VRT wrapper through ``<Overview>`` elements,
    and embedded by the COG driver with ``OVERVIEWS=FORCE_USE_EXISTING``.

``resolve_crs_authority``
    ``to_epsg(confidence_threshold=25)`` will hand back a code for a CRS that
    merely resembles a registered one.  A Texas Centric Albers definition in US
    survey feet has no exact EPSG match and the "closest" code, EPSG:3083, is
    the **metre** variant -- roughly 25,000 km of displacement.  Authority codes
    are only returned here when the linear unit and ellipsoid round-trip.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
import math
import os
from pathlib import Path
import tempfile
from typing import Any, Iterator, Mapping, Sequence
import uuid
import warnings
import xml.etree.ElementTree as ET

# --------------------------------------------------------------------------
# Policy defaults
# --------------------------------------------------------------------------

#: Internal tile size for every COG this repo writes.  Overview pyramids are
#: built until the top level fits inside a single block of this size.
DEFAULT_BLOCKSIZE = 512

#: ZSTD-in-TIFF needs a GDAL built with libzstd.  That is common but not
#: universal across tile servers, QGIS builds, and older client GDAL, and a
#: reader without it fails hard rather than degrading -- an artifact that will
#: not open is worse than one that is slightly larger.  Measured on 3000x3000
#: float32 rasters, DEFLATE costs 2% (terrain), 6% (depth) and 8% (WSE) against
#: ZSTD at the same predictor, with no measurable difference in windowed read
#: time.  Set ``RAS2CNG_COG_COMPRESS=ZSTD`` for internal archives that never
#: leave a known-good GDAL.
DEFAULT_COMPRESS = os.environ.get("RAS2CNG_COG_COMPRESS", "DEFLATE").upper()

#: Predictor 3 (floating point) is the textbook choice, and it is what every
#: float raster here gets -- ``YES`` is GDAL's "pick for me", which resolves to
#: 3 for Float32/Float64 and 2 for integer types.  Writing the literal ``3``
#: instead is a trap: GDAL *hard-fails* an integer raster with "PREDICTOR=3 is
#: only supported with Float32 or Float64", so a class or count raster on the
#: continuous path would abort the conversion rather than fall back.
#:
#: The RBFS stack pairs DEFLATE with predictor 2 throughout; do not copy that
#: pairing without measuring, because predictor 2 was 16-25% larger than
#: predictor 3 on every surface benchmarked here, including the smooth WSE case
#: where it is reported to win.  Override with ``RAS2CNG_COG_PREDICTOR=2``.
DEFAULT_PREDICTOR = os.environ.get("RAS2CNG_COG_PREDICTOR", "YES")

#: Ceiling on the level-0 pixel count the in-memory area-matched builder will
#: accept.  Above this the raster falls back to GDAL resampling and the reason
#: is recorded in the output's tags rather than silently ignored.
#:
#: Level 0 is streamed and never held whole, but the level-1 accumulators are
#: quarter-size and hold a float64 sum plus an int64 weight per cell -- roughly
#: ``4 bytes x pixel_count`` of resident memory, so the default ceiling costs
#: about 1.6 GB at its limit (a 20000x20000 raster).  Lower it on a memory-tight
#: host; raising it trades RAM for correct wet-area overviews on bigger rasters.
MAX_AREA_MATCHED_PIXELS = int(
    os.environ.get("RAS2CNG_AREA_MATCHED_MAX_PIXELS", 400_000_000)
)

#: PROJ's own default authority-match confidence.  Anything lower will match a
#: CRS to a registered code that differs in linear unit or datum.
MIN_CRS_CONFIDENCE = 70

#: Extra COG creation options for large numeric terrain rasters.  ``SPARSE_OK``
#: matters because a HEC-RAS terrain is usually a small footprint inside a large
#: bounding box.
TERRAIN_CREATION_OPTIONS: dict[str, Any] = {"LEVEL": 9, "SPARSE_OK": "YES"}


class CogValidationUnavailable(RuntimeError):
    """Raised when ``rio-cogeo`` is not installed, so layout cannot be checked."""


class CogValidationError(ValueError):
    """Raised when a written artifact is not a valid Cloud Optimized GeoTIFF."""


# --------------------------------------------------------------------------
# Atomic replacement (M6)
# --------------------------------------------------------------------------


@contextmanager
def atomic_output(path: Path, *, suffix: str = "") -> Iterator[Path]:
    """Yield a staging path in ``path``'s directory; replace ``path`` on success.

    The partial is a hidden sibling in the *destination* directory so the
    rename stays on one filesystem and is therefore atomic, and it is
    PID/UUID-namespaced so concurrent writers cannot collide.  The previous
    good artifact is never removed until the replacement exists.
    """

    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    staged = destination.with_name(
        f".{destination.name}.partial-{os.getpid()}-{uuid.uuid4().hex[:8]}{suffix}"
    )
    try:
        yield staged
        if not staged.exists():
            raise FileNotFoundError(
                f"Staged output was never written and {destination} was left untouched: {staged}"
            )
        os.replace(staged, destination)
    finally:
        staged.unlink(missing_ok=True)


# --------------------------------------------------------------------------
# Overview level policy (I6)
# --------------------------------------------------------------------------


def overview_factors(
    width: int,
    height: int,
    *,
    blocksize: int = DEFAULT_BLOCKSIZE,
    max_levels: int = 30,
) -> list[int]:
    """Return decimation factors that continue until a level fits one block.

    Stopping earlier -- at 256 px, or capping the factor at 64 -- leaves a large
    raster without a cheap top level, so a zoomed-out read pulls many blocks
    from a mid-level overview instead of one block from the top.
    """

    if width <= 0 or height <= 0:
        return []
    factors: list[int] = []
    factor = 2
    while len(factors) < max_levels:
        previous = math.ceil(max(width, height) / (factor // 2 or 1))
        if previous <= blocksize:
            break
        factors.append(factor)
        factor *= 2
    return factors


# --------------------------------------------------------------------------
# CRS authority resolution (M3)
# --------------------------------------------------------------------------


def _linear_unit_factor(crs) -> float | None:
    try:
        axis = crs.axis_info[0]
    except (AttributeError, IndexError):
        return None
    factor = getattr(axis, "unit_conversion_factor", None)
    return float(factor) if factor else None


def _ellipsoid_matches(source, candidate) -> bool:
    left, right = source.ellipsoid, candidate.ellipsoid
    if left is None or right is None:
        return False
    for attribute in ("semi_major_metre", "inverse_flattening"):
        a = getattr(left, attribute, None)
        b = getattr(right, attribute, None)
        if a is None or b is None:
            # An unspecified inverse flattening (a sphere) is only comparable
            # when both sides agree it is unspecified.
            if a is not b:
                return False
            continue
        if not math.isclose(float(a), float(b), rel_tol=1e-9):
            return False
    return True


def resolve_crs_authority(
    crs: Any,
    *,
    min_confidence: int = MIN_CRS_CONFIDENCE,
) -> tuple[str, str] | None:
    """Return ``(authority, code)`` only when it is safe to publish as one.

    A match is rejected unless the registered definition round-trips the
    source's linear unit and ellipsoid.  That is precisely the check that
    separates a US-survey-foot Albers definition from the metre-based EPSG:3083
    a permissive threshold would happily return.
    """

    if crs is None:
        return None
    try:
        from pyproj import CRS as PyprojCRS

        if hasattr(crs, "to_wkt"):
            source = PyprojCRS.from_wkt(crs.to_wkt())
        else:
            source = PyprojCRS.from_user_input(crs)
        match = source.to_authority(min_confidence=min_confidence)
        if not match:
            return None
        candidate = PyprojCRS.from_authority(*match)
    except Exception:
        return None

    source_unit = _linear_unit_factor(source)
    candidate_unit = _linear_unit_factor(candidate)
    if source_unit is None or candidate_unit is None:
        return None
    if not math.isclose(source_unit, candidate_unit, rel_tol=1e-9):
        return None
    if not _ellipsoid_matches(source, candidate):
        return None
    return str(match[0]), str(match[1])


def describe_crs(crs: Any) -> dict[str, Any]:
    """Describe a CRS for a manifest without ever guessing an authority code.

    ``crs`` is advisory -- it is an authority string only when the code was
    verified, and the verbatim source definition otherwise.  ``proj4`` is the
    authoritative browser-side definition because proj4js bundles no EPSG data.
    """

    if crs is None:
        return {"crs": None, "authority": None, "proj4": None, "wkt": None}

    authority = resolve_crs_authority(crs)
    try:
        wkt = crs.to_wkt() if hasattr(crs, "to_wkt") else str(crs)
    except Exception:
        wkt = None
    try:
        proj4 = crs.to_proj4() if hasattr(crs, "to_proj4") else None
    except Exception:
        proj4 = None
    if proj4:
        # PROJ emits "+type=crs" style booleans as "=True"; strip so proj4js,
        # which does not understand the value form, still parses the string.
        proj4 = " ".join(
            token[:-5] if token.endswith("=True") else token for token in proj4.split()
        )
    if authority:
        label: str | None = f"{authority[0]}:{authority[1]}"
    else:
        try:
            label = (crs.to_string() if hasattr(crs, "to_string") else str(crs)) or None
        except Exception:
            label = wkt
    return {
        "crs": label,
        "authority": f"{authority[0]}:{authority[1]}" if authority else None,
        "proj4": proj4,
        "wkt": wkt,
    }


def assert_plausible_wgs84_bounds(bounds: Sequence[float], source: Any = None) -> None:
    """Fail fast when derived lon/lat bounds cannot be real.

    A wrong CRS almost always shows up here first: the transform succeeds and
    produces coordinates that are out of range, degenerate, or non-finite.
    """

    west, south, east, north = (float(value) for value in bounds)
    label = f" for {source}" if source else ""
    if not all(math.isfinite(value) for value in (west, south, east, north)):
        raise ValueError(f"Derived WGS84 bounds are not finite{label}: {bounds}")
    if not (-180.0 <= west <= 180.0 and -180.0 <= east <= 180.0):
        raise ValueError(f"Derived WGS84 longitude is out of range{label}: {bounds}")
    if not (-90.0 <= south <= 90.0 and -90.0 <= north <= 90.0):
        raise ValueError(f"Derived WGS84 latitude is out of range{label}: {bounds}")
    if east <= west or north <= south:
        raise ValueError(f"Derived WGS84 bounds are degenerate{label}: {bounds}")


# --------------------------------------------------------------------------
# Validation (M5)
# --------------------------------------------------------------------------


@dataclass
class CogValidation:
    """Outcome of the layout and display checks for one written raster."""

    path: Path
    valid: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    layout_checked: bool = False


def validate_cog(path: Path, *, require_mask: bool = True) -> CogValidation:
    """Validate cloud-optimized *layout* and transparent-display readiness.

    ``rio_cogeo.cog_validate`` is the only check that establishes layout: a
    plain tiled GeoTIFF with a ``.ovr`` sidecar, or one whose IFDs are ordered
    wrongly, passes every hand-rolled structural test and is not a COG.

    The ``rio cogeo validate`` **CLI always exits 0**, including on an invalid
    file, so any gate that shells out and trusts ``$?`` is a no-op.  This gates
    on the returned boolean.
    """

    import rasterio
    from rasterio.enums import MaskFlags

    target = Path(path)
    result = CogValidation(path=target, valid=True)

    try:
        from rio_cogeo.cogeo import cog_validate
    except ImportError:
        result.warnings.append(
            "rio-cogeo is not installed, so cloud-optimized layout was not verified. "
            "Install ras2cng[pmtiles] to enable the layout gate."
        )
    else:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            is_valid, errors, cog_warnings = cog_validate(str(target), quiet=True)
        result.layout_checked = True
        result.warnings.extend(str(item) for item in cog_warnings or ())
        if not is_valid:
            result.valid = False
            result.errors.extend(str(item) for item in errors or ())
            if not errors:
                result.errors.append("rio-cogeo reported an invalid COG layout.")

    # cog_validate does not cover nodata/mask, and that is the property that
    # makes a flood raster display transparently outside the wet area.
    if require_mask:
        try:
            with rasterio.open(target) as source:
                flags = set(source.mask_flag_enums[0]) if source.mask_flag_enums else set()
                has_mask = (
                    source.nodata is not None
                    or MaskFlags.alpha in flags
                    or MaskFlags.per_dataset in flags
                )
                if not has_mask:
                    result.valid = False
                    result.errors.append("COG has no nodata value or validity mask.")
                if source.crs is None:
                    result.valid = False
                    result.errors.append("COG has no embedded CRS.")
        except Exception as error:  # pragma: no cover - surfaced to the caller
            result.valid = False
            result.errors.append(f"COG could not be opened: {error}")

    return result


def assert_valid_cog(path: Path, *, require_mask: bool = True) -> CogValidation:
    """Validate and raise :class:`CogValidationError` on failure."""

    result = validate_cog(path, require_mask=require_mask)
    if not result.valid:
        raise CogValidationError(f"{path} is not a valid COG: {'; '.join(result.errors)}")
    return result


# --------------------------------------------------------------------------
# Creation options (M2)
# --------------------------------------------------------------------------


def numeric_predictor(dtype: Any, *, categorical: bool = False) -> int:
    """Return the best TIFF predictor that is legal for ``dtype``.

    The plain **GTiff** driver only accepts the numeric form (1/2/3); ``YES`` is
    a **COG** driver option and is rejected outright by GTiff.  Predictor 3 is
    additionally float-only -- GDAL hard-fails an integer raster rather than
    degrading -- so intermediates written with the GTiff driver have to resolve
    this themselves.

    Predictor 3 is preferred for continuous float surfaces, where it measured
    16-25% smaller than predictor 2.  That preference **reverses on class data**:
    on a float32 class raster predictor 3 was 0.475 MB against 0.195 MB for
    predictor 2, i.e. 2.4x larger.  Class values are a handful of repeated
    magnitudes, which horizontal differencing collapses and the floating-point
    predictor's byte shuffle does not -- so ``categorical`` keeps 2 whatever the
    storage type.
    """

    import numpy as np

    if categorical:
        return 2
    return 3 if np.dtype(dtype).kind == "f" else 2


def resolve_compression(
    compress: str,
    *,
    lerc_max_z_error: float | None,
    categorical: bool = False,
) -> tuple[str, float | None]:
    """Resolve the compression name, applying LERC only when asked for explicitly.

    LERC is **lossy**.  It is not a default and never will be: it is a per-product
    engineering decision, and passing a tolerance is how that decision is
    expressed.  Measured on a smooth WSE surface, ``MAX_Z_ERROR=0.01`` ft gave an
    84% size reduction with the error bound honoured exactly -- but the same
    setting must never be applied to a raster whose values feed a downstream
    calculation, and never to a class raster, where "within tolerance" is
    meaningless.
    """

    if lerc_max_z_error is None:
        return compress, None
    if categorical:
        raise ValueError(
            "LERC is lossy and cannot be applied to a categorical raster: "
            "a class value that is 'within tolerance' is a different class"
        )
    tolerance = float(lerc_max_z_error)
    if not (tolerance > 0.0) or not math.isfinite(tolerance):
        raise ValueError(
            f"lerc_max_z_error must be a positive tolerance, got {lerc_max_z_error!r}; "
            "omit it for lossless compression"
        )
    base = compress if compress in {"DEFLATE", "ZSTD"} else "DEFLATE"
    return f"LERC_{base}", tolerance


def cog_creation_options(
    *,
    compress: str = DEFAULT_COMPRESS,
    predictor: str = "YES",
    blocksize: int = DEFAULT_BLOCKSIZE,
    overview_resampling: str = "AVERAGE",
    reuse_overviews: bool = False,
    level: int | None = None,
    sparse_ok: bool = True,
    lerc_max_z_error: float | None = None,
) -> list[str]:
    """Return ``-co`` arguments for a ``gdal_translate -of COG`` invocation.

    ``OVERVIEWS`` is always passed explicitly.  Its default, ``AUTO``, means
    *"reuse the source pyramid verbatim if it has one"* -- and HEC-RAS terrain
    TIFFs routinely ship internal overviews or ``.ovr`` sidecars, so reuse is
    the normal path, not an edge case.  Under ``AUTO`` every
    ``OVERVIEW_RESAMPLING`` setting is silently discarded.
    """

    compress, tolerance = resolve_compression(compress, lerc_max_z_error=lerc_max_z_error)
    options = [
        "-co", f"COMPRESS={compress}",
        "-co", f"PREDICTOR={predictor}",
        "-co", f"OVERVIEW_COMPRESS={compress}",
        "-co", f"OVERVIEW_PREDICTOR={predictor}",
        "-co", f"OVERVIEW_RESAMPLING={overview_resampling}",
        # FORCE_USE_EXISTING makes a deliberately pre-built pyramid explicit and
        # fails loudly when it is missing; IGNORE_EXISTING makes the declared
        # resampling above actually run.
        "-co", f"OVERVIEWS={'FORCE_USE_EXISTING' if reuse_overviews else 'IGNORE_EXISTING'}",
        "-co", f"BLOCKSIZE={blocksize}",
        "-co", "BIGTIFF=IF_SAFER",
        "-co", "NUM_THREADS=ALL_CPUS",
    ]
    if level is not None:
        options.extend(["-co", f"LEVEL={level}"])
    if sparse_ok:
        options.extend(["-co", "SPARSE_OK=YES"])
    if tolerance is not None:
        options.extend(["-co", f"MAX_Z_ERROR={tolerance:g}"])
    return options


# --------------------------------------------------------------------------
# Area-matched overview construction (M1 / M4)
# --------------------------------------------------------------------------


@dataclass
class OverviewReport:
    """What the overview builder actually did, for tags and the manifest."""

    method: str
    factors: list[int] = field(default_factory=list)
    #: Level-0 wet cell count, then the wet area each level reproduces,
    #: expressed as a fraction of the level-0 area.
    base_valid_pixels: int = 0
    area_ratios: list[float] = field(default_factory=list)
    fallback_reason: str | None = None
    compression: str = ""
    #: Set only for LERC.  Its presence is the record that the artifact is
    #: lossy and by how much.
    lossy_max_z_error: float | None = None

    def as_tags(self) -> dict[str, str]:
        tags = {
            "overview_method": self.method,
            "overview_factors": ",".join(str(f) for f in self.factors),
        }
        if self.compression:
            tags["compression"] = self.compression
        if self.lossy_max_z_error is not None:
            # A lossy artifact that does not say so is indistinguishable from a
            # lossless one, and the tolerance is the whole engineering decision.
            tags["compression_lossy"] = "true"
            tags["compression_max_z_error"] = f"{self.lossy_max_z_error:g}"
        if self.area_ratios:
            tags["overview_area_ratios"] = ",".join(f"{r:.6f}" for r in self.area_ratios)
        if self.base_valid_pixels:
            tags["overview_base_valid_pixels"] = str(self.base_valid_pixels)
        if self.fallback_reason:
            tags["overview_fallback_reason"] = self.fallback_reason
        return tags


def _dither(indices, salt: int):
    """Deterministic per-cell tie-break value.

    Stability across runs matters: an unstable tie-break makes two builds of
    the same model produce different published wet masks.
    """

    import numpy as np

    x = indices.astype(np.uint64) ^ np.uint64(salt & 0xFFFFFFFFFFFFFFFF)
    x = (x * np.uint64(0x9E3779B97F4A7C15)) & np.uint64(0xFFFFFFFFFFFFFFFF)
    x ^= x >> np.uint64(29)
    x = (x * np.uint64(0xBF58476D1CE4E5B9)) & np.uint64(0xFFFFFFFFFFFFFFFF)
    x ^= x >> np.uint64(32)
    return x


def _select_area_matched(weights, factor: int, base_valid: int):
    """Choose which coarse cells stay wet so the level reproduces level-0 area.

    ``weights`` holds, per coarse cell, how many level-0 valid cells it covers.
    The threshold is solved for from the coverage histogram rather than
    hardcoded at 0.5, and the cells sitting exactly on the threshold are broken
    by a coherence rank -- preferring cells beside already-wet cells -- so the
    margin stays connected instead of speckling.
    """

    import numpy as np

    capacity = factor * factor
    target = int(round(base_valid / capacity))
    wet = weights > 0
    if target <= 0:
        return np.zeros_like(wet)
    if not wet.any():
        return wet
    if target >= int(wet.sum()):
        return wet

    counts = np.bincount(weights.ravel(), minlength=capacity + 1)
    accumulated = 0
    threshold = 0
    for level in range(capacity, 0, -1):
        if accumulated + int(counts[level]) >= target:
            threshold = level
            break
        accumulated += int(counts[level])
    else:  # pragma: no cover - guarded by the target >= wet.sum() check above
        return wet

    keep = weights > threshold
    remaining = target - accumulated
    if remaining <= 0:
        return keep

    tie = weights == threshold
    tie_indices = np.flatnonzero(tie.ravel())
    if remaining >= tie_indices.size:
        return keep | tie

    coherence = np.zeros(weights.shape, dtype=np.int16)
    coherence[1:, :] += keep[:-1, :]
    coherence[:-1, :] += keep[1:, :]
    coherence[:, 1:] += keep[:, :-1]
    coherence[:, :-1] += keep[:, 1:]

    tie_coherence = coherence.ravel()[tie_indices]
    tie_dither = _dither(tie_indices, salt=factor * 0x2545F491)
    # lexsort's last key is primary: coherence first, dither as the stable
    # deterministic tie-break beneath it.
    order = np.lexsort((tie_dither, tie_coherence))[::-1]
    chosen = tie_indices[order[:remaining]]
    flat = keep.ravel()
    flat[chosen] = True
    return flat.reshape(weights.shape)


def _reduce_sum(array):
    """Sum 2x2 blocks, zero-padding an odd trailing row or column."""

    import numpy as np

    height, width = array.shape
    padded_h = height + (height % 2)
    padded_w = width + (width % 2)
    if (padded_h, padded_w) != (height, width):
        padded = np.zeros((padded_h, padded_w), dtype=array.dtype)
        padded[:height, :width] = array
        array = padded
    return array.reshape(padded_h // 2, 2, padded_w // 2, 2).sum(axis=(1, 3))


def _reduce_mode(values, valid):
    """Return the modal valid value of each 2x2 block, lowest value breaking ties.

    ``nearest`` on a class raster preserves class membership but not class
    *proportion*; averaging one invents classes that were never in the source.
    """

    import numpy as np

    height, width = values.shape
    padded_h = height + (height % 2)
    padded_w = width + (width % 2)
    if (padded_h, padded_w) != (height, width):
        padded_values = np.zeros((padded_h, padded_w), dtype="float64")
        padded_valid = np.zeros((padded_h, padded_w), dtype=bool)
        padded_values[:height, :width] = values
        padded_valid[:height, :width] = valid
        values, valid = padded_values, padded_valid

    blocks = (
        values.reshape(padded_h // 2, 2, padded_w // 2, 2)
        .transpose(0, 2, 1, 3)
        .reshape(padded_h // 2, padded_w // 2, 4)
    )
    mask = (
        valid.reshape(padded_h // 2, 2, padded_w // 2, 2)
        .transpose(0, 2, 1, 3)
        .reshape(padded_h // 2, padded_w // 2, 4)
    )
    equal = (
        (blocks[..., :, None] == blocks[..., None, :])
        & mask[..., None, :]
        & mask[..., :, None]
    )
    counts = np.where(mask, equal.sum(axis=-1), -1)
    best = counts.max(axis=-1, keepdims=True)
    candidates = np.where(counts == best, blocks, np.inf)
    return candidates.min(axis=-1), best[..., 0] > 0


def _accumulate_first_level(source, band: int, categorical: bool):
    """Stream level 0 into the level-1 accumulators without materializing it.

    Level 0 is never held whole: the peak allocation is the quarter-size
    accumulator pair, not the source band.
    """

    import numpy as np
    from rasterio.windows import Window

    height, width = source.height, source.width
    out_h, out_w = math.ceil(height / 2), math.ceil(width / 2)
    sums = np.zeros((out_h, out_w), dtype="float64")
    weights = np.zeros((out_h, out_w), dtype="int64")
    modes = np.zeros((out_h, out_w), dtype="float64") if categorical else None
    mode_valid = np.zeros((out_h, out_w), dtype=bool) if categorical else None
    base_valid = 0

    strip = max(2, min(height + (height % 2), 2 * max(1, 4_000_000 // max(1, width))))
    strip += strip % 2
    for row in range(0, height, strip):
        rows = min(strip, height - row)
        window = Window(0, row, width, rows)
        data = source.read(band, window=window, masked=True)
        valid = ~np.ma.getmaskarray(data)
        values = np.ma.filled(data, 0).astype("float64", copy=False)
        base_valid += int(valid.sum())

        target = slice(row // 2, row // 2 + math.ceil(rows / 2))
        sums[target, :] = _reduce_sum(np.where(valid, values, 0.0))
        weights[target, :] = _reduce_sum(valid.astype("int64"))
        if categorical:
            block_modes, block_valid = _reduce_mode(values, valid)
            modes[target, :] = block_modes
            mode_valid[target, :] = block_valid

    return sums, weights, modes, mode_valid, base_valid


def build_area_matched_overviews(
    source_path: Path,
    work_dir: Path,
    *,
    factors: Sequence[int],
    categorical: bool = False,
    band: int = 1,
) -> tuple[list[Path], OverviewReport]:
    """Write one sidecar GeoTIFF per overview level, preserving wet area.

    Returns the level files in ascending-factor order along with a report of
    what was built, including the wet area each level actually reproduced.
    """

    import numpy as np
    import rasterio
    from rasterio import Affine

    source_path = Path(source_path)
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    method = "area_matched_mode" if categorical else "area_matched_mean"
    report = OverviewReport(method=method, factors=list(factors))

    with rasterio.open(source_path) as source:
        if source.nodata is None:
            raise ValueError(
                "Area-matched overviews need an explicit nodata value to define the wet mask"
            )
        if source.count != 1:
            raise ValueError(
                f"Area-matched overviews support single-band rasters; {source_path} has {source.count}"
            )
        if source.width * source.height > MAX_AREA_MATCHED_PIXELS:
            raise ValueError(
                f"Raster exceeds RAS2CNG_AREA_MATCHED_MAX_PIXELS "
                f"({source.width * source.height} > {MAX_AREA_MATCHED_PIXELS})"
            )

        nodata = float(source.nodata)
        dtype = source.dtypes[band - 1]
        base_transform = source.transform
        crs = source.crs

        sums, weights, modes, mode_valid, base_valid = _accumulate_first_level(
            source, band, categorical
        )

    report.base_valid_pixels = base_valid
    if base_valid == 0:
        raise ValueError(f"Raster has no valid pixels: {source_path}")

    integral = np.dtype(dtype).kind in "iu"
    level_paths: list[Path] = []

    for index, factor in enumerate(factors):
        if index > 0:
            sums = _reduce_sum(sums)
            weights = _reduce_sum(weights)
            if categorical:
                modes, mode_valid = _reduce_mode(modes, mode_valid)

        keep = _select_area_matched(weights, factor, base_valid)
        kept = int(keep.sum())
        report.area_ratios.append((kept * factor * factor) / base_valid)

        if categorical:
            values = np.where(keep & mode_valid, modes, nodata)
        else:
            with np.errstate(invalid="ignore", divide="ignore"):
                means = np.where(weights > 0, sums / np.maximum(weights, 1), nodata)
            values = np.where(keep, means, nodata)
        if integral:
            values = np.rint(values)

        level_height, level_width = keep.shape
        level_path = work_dir / f"overview_{factor}.tif"
        profile = {
            "driver": "GTiff",
            "width": level_width,
            "height": level_height,
            "count": 1,
            "dtype": dtype,
            "nodata": nodata,
            "crs": crs,
            "transform": base_transform * Affine.scale(float(factor)),
            "tiled": True,
            "blockxsize": _block_dim(level_width),
            "blockysize": _block_dim(level_height),
            "compress": "DEFLATE",
            "BIGTIFF": "IF_SAFER",
        }
        with rasterio.open(level_path, "w", **profile) as destination:
            destination.write(values.astype(dtype, copy=False), 1)
            destination.update_tags(ns="rio_overview", resampling=method)
        level_paths.append(level_path)

        if max(level_width, level_height) <= 1:
            break

    return level_paths, report


def _block_dim(size: int) -> int:
    """Round a level dimension up to a legal 16-multiple TIFF block edge."""

    return max(16, min(DEFAULT_BLOCKSIZE, int(math.ceil(size / 16.0) * 16)))


def _vrt_with_overviews(
    source_path: Path,
    level_paths: Sequence[Path],
    vrt_path: Path,
    *,
    tags: Mapping[str, str] | None = None,
) -> Path:
    """Wrap ``source_path`` in a VRT that declares ``level_paths`` as overviews.

    GDAL exposes VRT ``<Overview>`` elements through the normal overview API,
    so the COG driver will embed them verbatim under
    ``OVERVIEWS=FORCE_USE_EXISTING``.  This is the only route to caller-supplied
    overview pixels without the osgeo Python bindings.
    """

    from rasterio.shutil import copy as copy_raster

    copy_raster(str(source_path), str(vrt_path), driver="VRT")
    tree = ET.parse(vrt_path)
    root = tree.getroot()

    for band_element in root.findall("VRTRasterBand"):
        if band_element.get("band") != "1":
            continue
        for level_path in level_paths:
            overview = ET.SubElement(band_element, "Overview")
            filename = ET.SubElement(overview, "SourceFilename")
            filename.set("relativeToVRT", "0")
            filename.text = str(Path(level_path).resolve())
            ET.SubElement(overview, "SourceBand").text = "1"

    if tags:
        # Only the default metadata domain survives GDAL's GTiff/COG
        # CreateCopy -- a namespaced domain such as rasterio's ``rio_overview``
        # is silently dropped -- so the provenance is written as plain keys any
        # GDAL client can read back.
        metadata = ET.SubElement(root, "Metadata")
        for key, value in tags.items():
            entry = ET.SubElement(metadata, "MDI")
            entry.set("key", key)
            entry.text = str(value)

    tree.write(vrt_path, encoding="utf-8", xml_declaration=False)
    return vrt_path


def area_matched_cog(
    source_path: Path,
    destination: Path,
    *,
    categorical: bool = False,
    compress: str = DEFAULT_COMPRESS,
    predictor: int | str | None = None,
    blocksize: int = DEFAULT_BLOCKSIZE,
    scratch_dir: Path | None = None,
    fallback_resampling: str = "average",
    require_area_matched: bool = False,
    lerc_max_z_error: float | None = None,
    creation_options: Mapping[str, Any] | None = None,
) -> OverviewReport:
    """Write ``source_path`` to ``destination`` as a COG with correct overviews.

    Continuous rasters get area-matched coverage-mask overviews carrying the
    mean of their wet contributors; categorical rasters get the same mask with
    a modal value.  When the area-matched builder cannot run -- no nodata, more
    than one band, or a raster past the pixel ceiling -- the raster falls back
    to GDAL resampling with ``OVERVIEWS=IGNORE_EXISTING`` and the reason is
    recorded in the output's tags.

    ``lerc_max_z_error`` opts into lossy LERC at that tolerance; the tolerance is
    written into the artifact's tags so the loss travels with the file.  Never
    set it on a raster whose values feed a downstream calculation.
    """

    from rasterio.shutil import copy as copy_raster

    source_path = Path(source_path)
    destination = Path(destination)
    if predictor is None:
        # Continuous float -> 3 (measured 16-25% better); class data -> 2
        # (measured 2.4x better on float32, and the only legal choice on int).
        predictor = 2 if categorical else DEFAULT_PREDICTOR
    compress, tolerance = resolve_compression(
        compress, lerc_max_z_error=lerc_max_z_error, categorical=categorical
    )
    extra_options = dict(creation_options or {})
    if tolerance is not None:
        extra_options["MAX_Z_ERROR"] = tolerance
        # LERC does its own encoding; a byte predictor on top is meaningless.
        predictor = None

    import rasterio

    with rasterio.open(source_path) as probe:
        factors = overview_factors(probe.width, probe.height, blocksize=blocksize)

    with tempfile.TemporaryDirectory(
        prefix="ras2cng-cog-",
        dir=str(scratch_dir) if scratch_dir is not None else None,
    ) as temporary:
        work_dir = Path(temporary)
        report: OverviewReport
        level_paths: list[Path] = []

        if not factors:
            report = OverviewReport(
                method="none", factors=[], fallback_reason="raster fits a single block"
            )
        else:
            try:
                level_paths, report = build_area_matched_overviews(
                    source_path, work_dir, factors=factors, categorical=categorical
                )
            except (ValueError, MemoryError) as error:
                if require_area_matched:
                    raise
                report = OverviewReport(
                    method=fallback_resampling,
                    factors=list(factors),
                    fallback_reason=str(error),
                )

        report.compression = compress
        report.lossy_max_z_error = tolerance

        # Both paths go through a VRT wrapper so the overview provenance is
        # recorded on the artifact either way -- a fallback that is not labelled
        # is indistinguishable from the fix having worked.
        vrt_path = work_dir / "source_with_overviews.vrt"
        _vrt_with_overviews(source_path, level_paths, vrt_path, tags=report.as_tags())

        if predictor is not None:
            extra_options["predictor"] = predictor

        with atomic_output(destination) as staged:
            copy_raster(
                str(vrt_path),
                str(staged),
                driver="COG",
                compress=compress,
                blocksize=blocksize,
                **extra_options,
                # FORCE_USE_EXISTING embeds the levels built above verbatim.
                # IGNORE_EXISTING makes the fallback's declared resampling
                # actually run instead of silently inheriting a source pyramid.
                **(
                    {"OVERVIEWS": "FORCE_USE_EXISTING"}
                    if level_paths
                    else {
                        "OVERVIEWS": "IGNORE_EXISTING",
                        "overview_resampling": fallback_resampling,
                    }
                ),
                BIGTIFF="IF_SAFER",
                NUM_THREADS="ALL_CPUS",
            )

    return report
