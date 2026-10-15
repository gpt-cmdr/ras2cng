"""Display-only elevation derivatives: a small inspect / plan / write / validate API.

This module produces a *web display* copy of a terrain elevation raster.  It is
never an engineering terrain, never a simulation input, and the result must not
feed a hydraulic or scientific calculation.  The source raster is only read.

Four decisions here are not the obvious implementation:

**Grid.**  The display grid is an *integer multiple of the source grid*,
anchored at the source's own origin, so every display cell is exactly an
``n x n`` block of source cells.  The factor is chosen to land nearest a target
of about 10 ft (3.048 m) inside a window (default 7.5-12.5 ft).  A source that
is already coarser than the window keeps factor 1 -- a display derivative never
upsamples.  A source with no integer multiple inside the window is *rejected*
rather than silently fitted to a different grid.  A source whose width or
height is not divisible by the factor is *rejected* too: a partial edge block
would publish coverage the source does not have, and cropping or shrinking the
cell would break alignment.

**Footprint.**  A coarse cell is valid or not; it cannot reproduce every fine
mask boundary.  The write result reports exactly how many source cells were
added to or dropped from the footprint, and an optional caller gate can refuse a
mismatch.  ``min_valid_fraction`` is a display policy applied to *every*
resampler; no value of it is inherently area-neutral.

**Precision.**  Valid samples are rounded to the nearest 0.01 stored vertical
unit *after* resampling, in the base level and in every embedded overview level
(through ``cog.area_matched_cog(overview_quantum=...)``).  That is decimal
quantization (error <= 0.005, plus a float32 representation term), and it is
deliberately **not** LERC ``MAX_Z_ERROR``: the encoder is always lossless over
the already-rounded values, so no second lossy tolerance is layered on top.
``cog``'s lossy LERC default is explicitly switched off for every write.

**Units are two facts.**  The horizontal unit comes from the CRS; the vertical
unit is declared (or read from the band unit) and is never inferred from the
horizontal one.  Bare ``ft`` is ambiguous between the international and the US
survey foot (2 parts per million apart), so it is refused; say which foot.  A
declared vertical datum is recorded verbatim: it neither overrides nor verifies
any embedded vertical CRS, and the assurance level is written to the output.

COG encoding is delegated to :func:`ras2cng.cog.area_matched_cog`.  No second
encoder exists here.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
import tempfile
from dataclasses import asdict, dataclass, field, fields, replace
from pathlib import Path
from typing import Any, Iterator

from ras2cng import cog

POLICY_ID = "ras2cng.terrain_display/2"

METRE = "metre"
INTERNATIONAL_FOOT = "international_foot"
US_SURVEY_FOOT = "us_survey_foot"

_UNIT_METRES: dict[str, float] = {
    METRE: 1.0,
    INTERNATIONAL_FOOT: 0.3048,
    US_SURVEY_FOOT: 1200.0 / 3937.0,
}

_UNIT_ALIASES: dict[str, str] = {
    "m": METRE,
    "meter": METRE,
    "meters": METRE,
    "metre": METRE,
    "metres": METRE,
    "international_foot": INTERNATIONAL_FOOT,
    "international_feet": INTERNATIONAL_FOOT,
    "intl_foot": INTERNATIONAL_FOOT,
    "us_survey_foot": US_SURVEY_FOOT,
    "us_survey_feet": US_SURVEY_FOOT,
    "us_foot": US_SURVEY_FOOT,
    "foot_us": US_SURVEY_FOOT,
    "ft_us": US_SURVEY_FOOT,
    "us_ft": US_SURVEY_FOOT,
    "usft": US_SURVEY_FOOT,
}

#: Spellings that name a foot without saying which one.
_AMBIGUOUS_FOOT = {"ft", "foot", "feet"}

#: float32 carries a 24-bit significand.  Requiring the half-ulp to stay below
#: this fraction of the quantum keeps the stored value on the intended 0.01 step.
_REPRESENTATION_FRACTION = 0.1

_GRID_TOLERANCE = 1e-9
_LERC_PREDICTOR = "NO"
_FLOAT32_EPS = 2.0**-24
_BLOCK = cog.DEFAULT_BLOCKSIZE
_STRIP_CELLS = 2_000_000
_RATIO_TOLERANCE = 2e-6


class DisplayTerrainError(ValueError):
    """Base class for every refusal this module issues."""


class UnitsError(DisplayTerrainError):
    """A required unit is missing, ambiguous or contradictory."""


class GridError(DisplayTerrainError):
    """The source grid cannot produce a legitimate display grid."""


class NodataError(DisplayTerrainError):
    """The validity mask is missing, corrupt or collides with real values."""


class RepresentationError(DisplayTerrainError):
    """Values cannot be stored to the requested precision in float32."""


class AliasError(DisplayTerrainError):
    """The destination is, or links to, the source file."""


class FootprintError(DisplayTerrainError):
    """The display footprint deviates from the source by more than the caller allows."""


# --------------------------------------------------------------------------
# Records
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class SourceGridInfo:
    """Facts about a source raster, read once and never guessed."""

    crs_wkt: str
    width: int
    height: int
    #: Affine ``(a, b, c, d, e, f)``.
    transform: tuple[float, float, float, float, float, float]
    dtype: str
    nodata: float | None
    horizontal_unit: str
    horizontal_unit_metres: float
    vertical_unit: str
    vertical_datum: str | None
    scale: float = 1.0
    offset: float = 0.0
    #: The caller's vertical-unit declaration and what the raster itself says,
    #: each canonicalised; kept apart so the assurance can be stated truthfully
    #: and so a later re-inspection compares like with like.
    vertical_unit_declared: str | None = None
    vertical_unit_embedded: str | None = None
    #: Name of a vertical component of an embedded compound CRS, if any.  It is
    #: reported, never compared with ``vertical_datum``.
    embedded_vertical_crs: str | None = None
    source_label: str | None = None
    size_bytes: int | None = None
    sha256: str | None = None

    @property
    def pixel_size(self) -> tuple[float, float]:
        return abs(self.transform[0]), abs(self.transform[4])

    @property
    def pixel_size_metres(self) -> float:
        return self.pixel_size[0] * self.horizontal_unit_metres

    @property
    def vertical_unit_assurance(self) -> str:
        if self.vertical_unit_declared and self.vertical_unit_embedded:
            return "declared_and_embedded_agree"
        if self.vertical_unit_declared:
            return "declared_only"
        if self.vertical_unit_embedded:
            return "embedded_only"
        return "none"

    @property
    def vertical_datum_assurance(self) -> str:
        """``declared_unverified`` -- recorded verbatim, never cross-checked."""

        return "declared_unverified" if self.vertical_datum else "unknown"


@dataclass(frozen=True)
class CodecPolicy:
    """A *lossless* encoding of the already-rounded values.

    There is no tolerance field: a lossy tolerance on top of decimal rounding
    would stack two errors.  LERC variants are always written with
    ``MAX_Z_ERROR=0``.
    """

    name: str
    compress: str
    level: int | None = None
    predictor: str = "YES"

    def __post_init__(self) -> None:
        compress = self.compress.upper()
        if compress not in {"DEFLATE", "ZSTD", "LERC_DEFLATE", "LERC_ZSTD"}:
            raise DisplayTerrainError(f"unsupported display codec {self.compress!r}")
        object.__setattr__(self, "compress", compress)
        predictor = str(self.predictor).upper()
        if compress.startswith("LERC"):
            if predictor != _LERC_PREDICTOR:
                raise DisplayTerrainError("LERC does its own encoding; use predictor='NO'")
        elif predictor not in {"YES", "NO", "2", "3"}:
            raise DisplayTerrainError(f"predictor must be YES, NO, 2 or 3, got {self.predictor!r}")
        object.__setattr__(self, "predictor", predictor)
        if self.level is not None:
            ceiling = 12 if compress.endswith("DEFLATE") else 22
            if not 1 <= int(self.level) <= ceiling:
                raise DisplayTerrainError(f"{compress} level must be 1..{ceiling}")

    @property
    def lossless_tolerance(self) -> float | None:
        """``0.0`` for LERC variants (written with MAX_Z_ERROR=0), else ``None``."""

        return 0.0 if self.compress.startswith("LERC") else None


DEFLATE_FLOAT = CodecPolicy("deflate-l9-float-predictor", "DEFLATE", 9, "YES")
ZSTD_HIGH = CodecPolicy("zstd-l19-float-predictor", "ZSTD", 19, "YES")
LERC_ZSTD_LOSSLESS = CodecPolicy("lerc-zstd-l19-zero-error", "LERC_ZSTD", 19, "NO")
DEFLATE_INTEGER_DIFF = CodecPolicy("deflate-l9-predictor2", "DEFLATE", 9, "2")
ZSTD_INTEGER_DIFF = CodecPolicy("zstd-l19-predictor2", "ZSTD", 19, "2")

#: Candidates for :func:`benchmark_codecs`, in tie-break order.  Predictor 2 is
#: included on purpose: which predictor wins depends on the surface (it won on a
#: smooth synthetic ramp, against ``cog``'s float-surface guidance), so a
#: benchmark offering only predictor 3 would be an unfair baseline.
CANDIDATE_CODECS: tuple[CodecPolicy, ...] = (
    DEFLATE_FLOAT,
    DEFLATE_INTEGER_DIFF,
    ZSTD_HIGH,
    ZSTD_INTEGER_DIFF,
    LERC_ZSTD_LOSSLESS,
)


@dataclass(frozen=True)
class DisplayPolicy:
    """Everything a caller can decide.  Defaults describe the display profile."""

    target_spacing_m: float = 10.0 * 0.3048
    min_spacing_m: float = 7.5 * 0.3048
    max_spacing_m: float = 12.5 * 0.3048
    #: Decimal quantum in the *stored vertical unit*.  ``1 / increment`` must be
    #: an integer so the rounded value is ``k / inverse``.
    increment: float = 0.01
    #: ``average`` = mean of valid source cells in the block; ``nearest`` = the
    #: block's centre cell (``n // 2`` offset: for an even factor, the later of
    #: the two middle indices on each axis, i.e. the lower-right of the four
    #: central cells in row/column array order).
    resampling: str = "average"
    #: A display cell is valid only when at least this share of its source cells
    #: are valid -- for *both* resamplers; ``nearest`` additionally needs its
    #: centre cell valid.  A display policy, not a claim of area neutrality: a
    #: coarse mask cannot reproduce every fine boundary.
    min_valid_fraction: float = 0.5
    output_nodata: float = -9999.0
    #: Optional caller gate on ``(added + dropped) / source_valid`` source
    #: cells.  ``None`` reports the deviation without refusing.
    max_footprint_mismatch_fraction: float | None = None
    codec: CodecPolicy = DEFLATE_FLOAT

    def __post_init__(self) -> None:
        if not 0.0 < self.min_spacing_m <= self.target_spacing_m <= self.max_spacing_m:
            raise DisplayTerrainError("require 0 < min <= target <= max spacing")
        inverse = 1.0 / self.increment if self.increment > 0 else 0.0
        if not self.increment > 0 or not math.isclose(inverse, round(inverse), rel_tol=1e-9):
            raise DisplayTerrainError("increment must be 1/k for a positive integer k")
        if self.resampling not in {"average", "nearest"}:
            raise DisplayTerrainError("resampling must be 'average' or 'nearest'")
        if not 0.0 < self.min_valid_fraction <= 1.0:
            raise DisplayTerrainError("min_valid_fraction must be in (0, 1]")
        gate = self.max_footprint_mismatch_fraction
        if gate is not None and not (math.isfinite(gate) and gate >= 0.0):
            raise DisplayTerrainError("max_footprint_mismatch_fraction must be finite and >= 0")
        import numpy as np

        if not math.isfinite(self.output_nodata) or float(np.float32(self.output_nodata)) != self.output_nodata:
            raise DisplayTerrainError("output_nodata must be finite and exactly representable in float32")

    @property
    def inverse(self) -> int:
        return int(round(1.0 / self.increment))


@dataclass(frozen=True)
class DisplayPlan:
    """What would be written, with every assumption stated."""

    policy_id: str
    source: SourceGridInfo
    policy: DisplayPolicy
    factor: int
    #: ``integer_multiple`` | ``native_grid`` | ``native_coarser_than_window``
    disposition: str
    transform: tuple[float, float, float, float, float, float]
    width: int
    height: int
    pixel_size_m: float
    #: Always unqualified here: browser decode is proved separately, per codec.
    decoder_qualification: str = "unqualified"

    @property
    def origin(self) -> tuple[float, float]:
        return self.transform[2], self.transform[5]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class DisplayWriteResult:
    destination: Path
    bytes: int
    source_valid_cells: int
    display_valid_cells: int
    #: ``display_valid * factor^2 / source_valid`` -- footprint area carried over.
    footprint_area_ratio: float
    min_value: float
    max_value: float
    #: Largest |rounded - resampled| actually observed; <= increment / 2.
    observed_quantization_error: float
    #: Declared bound: ``increment / 2`` plus the float32 representation term.
    quantization_error_bound: float
    overview: dict[str, Any]
    tags: dict[str, str]
    #: Source cells that fall in a valid display cell but are themselves
    #: invalid (coverage added), and valid source cells lost to an invalid
    #: display cell.  ``(added + dropped) / source_valid`` is the mismatch.
    footprint_added_source_cells: int = 0
    footprint_dropped_source_cells: int = 0
    footprint_mismatch_fraction: float = 0.0


@dataclass(frozen=True)
class DisplayValidation:
    path: Path
    valid: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    display_valid_cells: int | None = None
    #: True only when at least one embedded overview level was read back and
    #: every level's valid samples are on the quantum.  False for a raster that
    #: fits one block (no levels -- the per-level check is vacuous there).
    overview_levels_quantized: bool = False
    overview_levels_checked: list[int] = field(default_factory=list)


@dataclass(frozen=True)
class CodecBenchmark:
    codec: CodecPolicy
    bytes: int | None
    #: Base level read back equals the rounded raster exactly.
    lossless: bool | None
    error: str | None = None
    #: Byte size is *not* decoder acceptance.
    decoder_qualified: bool = False


# --------------------------------------------------------------------------
# Units
# --------------------------------------------------------------------------


def _canonical_vertical_unit(text: str | None) -> tuple[str | None, bool]:
    """Return ``(label, ambiguous)``; ``label`` is ``None`` when unusable."""

    if text is None or not str(text).strip():
        return None, False
    key = re.sub(r"[\s\-.]+", "_", str(text).strip().lower()).strip("_")
    if key in _AMBIGUOUS_FOOT:
        return None, True
    return _UNIT_ALIASES.get(key), False


def _horizontal_unit_label(factor: float) -> str:
    for label, metres in _UNIT_METRES.items():
        if math.isclose(factor, metres, rel_tol=1e-8):
            return label
    return f"other({factor:.10g}m)"


def resolve_vertical_unit(declared: str | None, embedded: str | None = None) -> str:
    """Resolve the stored vertical unit from a declaration and an embedded tag.

    The two must agree when both are usable.  An ambiguous bare ``ft`` never
    resolves on its own, and is never read from the horizontal unit.
    """

    declared_label, declared_ambiguous = _canonical_vertical_unit(declared)
    embedded_label, embedded_ambiguous = _canonical_vertical_unit(embedded)
    if declared is not None and declared_label is None:
        hint = " (say international_foot or us_survey_foot)" if declared_ambiguous else ""
        raise UnitsError(f"vertical unit {declared!r} is not recognised{hint}")
    if declared_label and embedded_label and declared_label != embedded_label:
        raise UnitsError(
            f"vertical unit contradiction: declared {declared_label!r}, raster says {embedded_label!r}"
        )
    resolved = declared_label or embedded_label
    if resolved is None:
        hint = (
            " The raster's band unit is a bare foot spelling; declare which foot."
            if embedded_ambiguous
            else ""
        )
        raise UnitsError("vertical unit is missing; declare it explicitly." + hint)
    return resolved


# --------------------------------------------------------------------------
# Inspect
# --------------------------------------------------------------------------


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _embedded_vertical_crs(crs) -> str | None:
    try:
        for part in crs.sub_crs_list or ():
            if part.is_vertical:
                return str(part.name)
    except Exception:
        return None
    return None


def inspect_source(
    path: Path,
    *,
    vertical_unit: str | None = None,
    vertical_datum: str | None = None,
    source_label: str | None = None,
    compute_sha256: bool = False,
) -> SourceGridInfo:
    """Read grid, CRS, unit and nodata facts without reading any pixel data.

    ``vertical_unit`` is required unless the band carries an unambiguous unit;
    when both exist they must agree, and the info records which were present.
    ``vertical_datum`` is recorded verbatim and never converted or verified
    against an embedded vertical CRS (that name is reported separately);
    ``None`` stays ``None`` (unknown), it is not defaulted.
    """

    import rasterio
    from pyproj import CRS

    path = Path(path)
    with rasterio.open(path) as src:
        if src.count != 1:
            raise GridError(f"expected one elevation band, found {src.count}")
        if src.crs is None:
            raise GridError("source has no CRS")
        crs = CRS.from_wkt(src.crs.to_wkt())
        if not crs.is_projected:
            raise GridError("geographic or unprojected CRS is rejected; a metre/foot grid is required")
        factor = crs.axis_info[0].unit_conversion_factor
        if not factor or factor <= 0:
            raise GridError("horizontal linear unit could not be determined from the CRS")
        embedded_unit = (src.units[0] if src.units else None) or None
        unit = resolve_vertical_unit(vertical_unit, embedded_unit)
        info = SourceGridInfo(
            crs_wkt=crs.to_wkt(),
            width=src.width,
            height=src.height,
            transform=tuple(float(v) for v in tuple(src.transform)[:6]),
            dtype=src.dtypes[0],
            nodata=None if src.nodata is None else float(src.nodata),
            horizontal_unit=_horizontal_unit_label(float(factor)),
            horizontal_unit_metres=float(factor),
            vertical_unit=unit,
            vertical_datum=(vertical_datum.strip() or None) if vertical_datum else None,
            scale=float(src.scales[0]),
            offset=float(src.offsets[0]),
            vertical_unit_declared=_canonical_vertical_unit(vertical_unit)[0],
            vertical_unit_embedded=_canonical_vertical_unit(embedded_unit)[0],
            embedded_vertical_crs=_embedded_vertical_crs(crs),
            source_label=source_label,
            size_bytes=path.stat().st_size,
            sha256=_sha256(path) if compute_sha256 else None,
        )
    return info


# --------------------------------------------------------------------------
# Plan
# --------------------------------------------------------------------------


def _check_source(source: SourceGridInfo) -> None:
    a, b, _, d, e, _ = source.transform
    scale = max(abs(a), abs(e))
    if min(source.width, source.height) <= 0:
        raise GridError("source has no cells")
    if not (math.isfinite(a) and math.isfinite(e)) or a == 0 or e == 0:
        raise GridError("source pixel size is not finite and positive")
    if abs(b) > 1e-9 * scale or abs(d) > 1e-9 * scale:
        raise GridError("rotated or sheared source grids are rejected")
    if not math.isclose(abs(a), abs(e), rel_tol=1e-6):
        raise GridError(f"non-square source pixels ({abs(a)} x {abs(e)}) are rejected")
    if source.nodata is None:
        raise NodataError("source has no nodata value; the valid footprint cannot be established")
    if source.scale != 1.0 or source.offset != 0.0:
        raise GridError(
            f"source carries scale/offset ({source.scale}, {source.offset}); "
            "unscale it explicitly rather than have it guessed"
        )
    if source.vertical_unit not in _UNIT_METRES:
        raise UnitsError(f"vertical unit {source.vertical_unit!r} is not a known unit")
    if not source.horizontal_unit_metres > 0:
        raise UnitsError("horizontal unit has no metre factor")
    import numpy as np

    if np.dtype(source.dtype).kind not in "fiu":
        raise GridError(f"unsupported elevation dtype {source.dtype!r}")


def choose_factor(native_metres: float, policy: DisplayPolicy) -> tuple[int, str]:
    """Pick the integer block factor and say why.

    Never upsamples.  Raises :class:`GridError` when the native cell is finer
    than the window but no integer multiple of it falls inside the window.
    """

    eps = 1e-9
    if native_metres > policy.max_spacing_m * (1 + eps):
        return 1, "native_coarser_than_window"
    low = max(1, math.ceil(policy.min_spacing_m / native_metres - eps))
    high = math.floor(policy.max_spacing_m / native_metres + eps)
    if low > high:
        raise GridError(
            f"no integer multiple of the {native_metres:.6g} m native cell falls in "
            f"{policy.min_spacing_m:.4g}-{policy.max_spacing_m:.4g} m; widen the window "
            "explicitly or resample the source upstream"
        )
    best = min(
        range(low, high + 1),
        key=lambda n: (abs(n * native_metres - policy.target_spacing_m), n),
    )
    return best, ("native_grid" if best == 1 else "integer_multiple")


def plan_display_terrain(
    source: SourceGridInfo, policy: DisplayPolicy | None = None
) -> DisplayPlan:
    """Build the display grid plan for one authoritative source raster.

    Fails closed when the factor does not divide both dimensions: padding would
    add valid coverage the source never had, cropping would drop real data, and
    shrinking the cell would break alignment.  A caller that needs edge handling
    must say so with an explicit edge/strict-footprint policy, which this
    revision does not provide.

    Multi-source mosaics are not planned here: they need an explicit
    controlling grid, overlap and resampling decision made upstream, and one
    authoritative raster handed to this function.
    """

    policy = policy or DisplayPolicy()
    _check_source(source)
    factor, disposition = choose_factor(source.pixel_size_metres, policy)
    if source.width % factor or source.height % factor:
        raise GridError(
            f"source {source.width} x {source.height} is not divisible by the display factor "
            f"{factor}; non-divisible grids are refused rather than padded, cropped or "
            "re-gridded (an explicit edge policy is not available in this revision)"
        )
    a, b, c, d, e, f = source.transform
    return DisplayPlan(
        policy_id=POLICY_ID,
        source=source,
        policy=policy,
        factor=factor,
        disposition=disposition,
        transform=(a * factor, b, c, d, e * factor, f),
        width=source.width // factor,
        height=source.height // factor,
        pixel_size_m=source.pixel_size_metres * factor,
    )


# --------------------------------------------------------------------------
# Pure array steps
# --------------------------------------------------------------------------


def block_valid_counts(valid, factor: int):
    """Valid-cell count of every ``factor x factor`` block (edge blocks partial)."""

    import numpy as np

    height, width = valid.shape
    out_h, out_w = -(-height // factor), -(-width // factor)
    padded = np.zeros((out_h * factor, out_w * factor), dtype=np.int64)
    padded[:height, :width] = valid
    return padded.reshape(out_h, factor, out_w, factor).sum(axis=(1, 3))


def reduce_to_display_grid(values, valid, factor: int, *, resampling: str, min_valid_fraction: float):
    """Collapse ``factor x factor`` blocks under one validity rule.

    A block is valid only when ``valid_count >= min_valid_fraction * cells`` --
    for ``average`` *and* ``nearest``.  ``average`` then takes the mean of the
    valid cells; ``nearest`` takes the block's centre cell (offset
    ``factor // 2``, clamped on a partial edge block) and is additionally
    invalid when that centre cell is.  Returns ``(values, valid)`` of shape
    ``ceil(h/f) x ceil(w/f)``; invalid cells hold 0.0.  The display planner
    never produces partial edge blocks; they are handled here so the function
    stays total.
    """

    import numpy as np

    height, width = values.shape
    out_h, out_w = -(-height // factor), -(-width // factor)
    if factor == 1:
        return np.where(valid, values, 0.0), valid.copy()

    counts = block_valid_counts(valid, factor)
    cells = block_valid_counts(np.ones((height, width), dtype=bool), factor)
    threshold_ok = (counts > 0) & (counts >= min_valid_fraction * cells)

    if resampling == "nearest":
        rows = np.minimum(np.arange(out_h) * factor + factor // 2, height - 1)
        cols = np.minimum(np.arange(out_w) * factor + factor // 2, width - 1)
        grid = np.ix_(rows, cols)
        kept = valid[grid] & threshold_ok
        return np.where(kept, np.where(valid, values, 0.0)[grid], 0.0), kept

    pad_h, pad_w = out_h * factor, out_w * factor
    padded = np.zeros((pad_h, pad_w), dtype="float64")
    padded[:height, :width] = np.where(valid, values, 0.0)
    sums = padded.reshape(out_h, factor, out_w, factor).sum(axis=(1, 3))
    means = np.where(threshold_ok, sums / np.maximum(counts, 1), 0.0)
    return means, threshold_ok


def quantize_elevation(values, valid, increment: float = 0.01):
    """Round valid samples to the nearest ``increment``; leave invalid untouched.

    Rounding is ``numpy.rint`` (half to even) on ``value * (1/increment)`` in
    float64, then division by the same integer.  A decimal tie such as 0.125 is
    exactly representable and goes to the even step (0.12); a decimal that is
    not exactly representable (0.135) rounds according to its binary value.
    Error against the input is <= increment / 2.
    """

    import numpy as np

    inverse = round(1.0 / increment)
    out = np.array(values, dtype="float64", copy=True)
    out[valid] = np.rint(out[valid] * inverse) / inverse
    return out


def quantization_error_bound(max_abs_value: float, increment: float = 0.01) -> float:
    """``increment / 2`` plus half a float32 ulp at ``max_abs_value``."""

    return increment / 2.0 + abs(max_abs_value) * _FLOAT32_EPS


def _representation_limit(increment: float) -> float:
    return increment * _REPRESENTATION_FRACTION / _FLOAT32_EPS


# --------------------------------------------------------------------------
# Write
# --------------------------------------------------------------------------


def _source_mismatches(planned: SourceGridInfo, current: SourceGridInfo) -> list[str]:
    """Names of every material source fact that differs from the plan."""

    from pyproj import CRS

    problems: list[str] = []
    tol = _GRID_TOLERANCE * max(current.pixel_size) * 1e3
    for item in fields(SourceGridInfo):
        name = item.name
        a, b = getattr(planned, name), getattr(current, name)
        if name in {"size_bytes", "sha256"} and a is None:
            continue  # identity the plan never declared
        if name == "crs_wkt":
            same = CRS.from_wkt(a).equals(CRS.from_wkt(b), ignore_axis_order=True)
        elif name == "transform":
            same = all(math.isclose(x, y, rel_tol=1e-12, abs_tol=tol) for x, y in zip(a, b))
        elif name == "nodata":
            same = a == b or (a is not None and b is not None and math.isnan(a) and math.isnan(b))
        elif isinstance(a, float) and isinstance(b, float):
            same = math.isclose(a, b, rel_tol=1e-12, abs_tol=0.0)
        else:
            same = a == b
        if not same:
            problems.append(name)
    return problems


def _display_tags(plan: DisplayPlan) -> dict[str, str]:
    """The provenance projection written to the file and re-checked on validate."""

    source, policy = plan.source, plan.policy
    nodata = "none" if source.nodata is None else f"{source.nodata:.17g}"
    tags = {
        "display_policy": plan.policy_id,
        "display_use": "web_display_only_not_for_calculation",
        "display_factor": str(plan.factor),
        "display_disposition": plan.disposition,
        "display_source_pixel_size": f"{source.pixel_size[0]:.10g}",
        "display_source_shape": f"{source.width}x{source.height}",
        "display_source_dtype": source.dtype,
        "display_source_nodata": nodata,
        "display_source_origin": f"{source.transform[2]:.17g},{source.transform[5]:.17g}",
        "display_resampling": policy.resampling,
        "display_min_valid_fraction": f"{policy.min_valid_fraction:g}",
        "display_footprint_policy": (
            f"fail_closed_nondivisible;coarse_mask_min_valid_fraction="
            f"{policy.min_valid_fraction:g};resampling={policy.resampling}"
        ),
        "display_quantum": f"{policy.increment:g}",
        "display_quantum_unit": source.vertical_unit,
        "display_quantum_rounding": "half_even_float64_after_resampling",
        "display_quantization_scope": "base_and_all_overviews",
        "display_overview_quantization": f"{policy.increment:g}",
        "display_codec": policy.codec.name,
        "display_decoder_qualification": plan.decoder_qualification,
        "vertical_unit": source.vertical_unit,
        "vertical_unit_assurance": source.vertical_unit_assurance,
        "vertical_datum": source.vertical_datum or "unknown",
        "vertical_datum_assurance": source.vertical_datum_assurance,
        "horizontal_unit": source.horizontal_unit,
    }
    optional = (
        ("display_embedded_vertical_crs", source.embedded_vertical_crs),
        ("display_max_footprint_mismatch", policy.max_footprint_mismatch_fraction),
        ("display_source_label", source.source_label),
        ("display_source_size_bytes", source.size_bytes),
        ("display_source_sha256", source.sha256),
    )
    for key, value in optional:
        if value is not None:
            tags[key] = f"{value:g}" if isinstance(value, float) else str(value)
    return tags


def _refuse_alias(source: Path, destination: Path, *, overwrite: bool) -> None:
    """Refuse any destination that is, or links to, the source -- before output.

    Covers an identical resolved path (relative spellings, ``..``, a symlink in
    either role) and an existing file that is the same file on disk (a hard
    link).  An existing, unrelated destination is replaced only with
    ``overwrite=True``.
    """

    resolved_source = source.resolve(strict=True)
    if destination.resolve() == resolved_source:
        raise AliasError("destination resolves to the source file; refusing to overwrite the source")
    if destination.is_dir():
        raise AliasError(f"destination is a directory: {destination}")
    if destination.exists():
        if os.path.samefile(resolved_source, destination):
            raise AliasError("destination is a hard link or alias of the source file")
        if not overwrite:
            raise FileExistsError(
                f"destination exists: {destination}; pass overwrite=True to replace it"
            )


def _render_display_raster(plan: DisplayPlan, source_path: Path, destination: Path) -> dict[str, Any]:
    """Resample, quantize and write the float32 display raster (a GeoTIFF).

    Streams in strips aligned to the block factor, so the source is never held
    whole.  Source pixels are only read.  The source is re-inspected first and
    every material fact compared with the plan.
    """

    import numpy as np
    import rasterio
    from rasterio.windows import Window

    planned = plan.source
    declared = planned.vertical_unit_declared
    if declared is None and planned.vertical_unit_embedded is None:
        declared = planned.vertical_unit  # a hand-built info: its unit was declared
    try:
        current = inspect_source(
            source_path,
            vertical_unit=declared,
            vertical_datum=planned.vertical_datum,
            source_label=planned.source_label,
            compute_sha256=planned.sha256 is not None,
        )
        _check_source(current)
    except DisplayTerrainError as error:
        raise GridError(f"source no longer matches the plan ({error}); re-inspect and re-plan") from error
    problems = _source_mismatches(planned, current)
    if problems:
        raise GridError(f"source no longer matches the plan ({', '.join(problems)}); re-inspect and re-plan")

    policy, factor = plan.policy, plan.factor
    nodata = np.float32(policy.output_nodata)
    limit = _representation_limit(policy.increment)
    out_rows = max(1, _STRIP_CELLS // max(1, plan.source.width * factor))
    stats = {
        "source_valid": 0,
        "display_valid": 0,
        "added": 0,
        "dropped": 0,
        "min": math.inf,
        "max": -math.inf,
        "error": 0.0,
    }

    profile = {
        "driver": "GTiff",
        "width": plan.width,
        "height": plan.height,
        "count": 1,
        "dtype": "float32",
        "nodata": float(nodata),
        "crs": rasterio.crs.CRS.from_wkt(plan.source.crs_wkt),
        "transform": rasterio.Affine(*plan.transform),
        "tiled": True,
        "blockxsize": 256,
        "blockysize": 256,
        "compress": "DEFLATE",
        "BIGTIFF": "IF_SAFER",
    }
    cells_per_block = factor * factor
    with rasterio.open(source_path) as src, rasterio.open(destination, "w", **profile) as dst:
        for out_row in range(0, plan.height, out_rows):
            rows = min(out_rows, plan.height - out_row)
            src_row = out_row * factor
            window = Window(0, src_row, plan.source.width, rows * factor)
            data = src.read(1, window=window, masked=True)
            valid = ~np.ma.getmaskarray(data)
            values = np.ma.filled(data, 0).astype("float64", copy=False)
            if not np.isfinite(values[valid]).all():
                raise NodataError("non-finite value inside the valid footprint (mask error)")
            stats["source_valid"] += int(valid.sum())

            reduced, kept = reduce_to_display_grid(
                values, valid, factor,
                resampling=policy.resampling, min_valid_fraction=policy.min_valid_fraction,
            )
            counts = block_valid_counts(valid, factor)
            stats["added"] += int((cells_per_block - counts)[kept].sum())
            stats["dropped"] += int(counts[~kept].sum())

            rounded = quantize_elevation(reduced, kept, policy.increment)
            if kept.any():
                chosen = rounded[kept]
                peak = float(np.abs(chosen).max())
                if peak > limit:
                    raise RepresentationError(
                        f"|value| {peak:.6g} exceeds {limit:.6g} {plan.source.vertical_unit}: "
                        f"float32 cannot hold {policy.increment:g} steps at that magnitude"
                    )
                if (chosen.astype("float32") == nodata).any():
                    raise NodataError(
                        f"a valid elevation equals the output nodata value {float(nodata)}; "
                        "choose a different output_nodata"
                    )
                stats["display_valid"] += int(kept.sum())
                stats["min"] = min(stats["min"], float(chosen.min()))
                stats["max"] = max(stats["max"], float(chosen.max()))
                stats["error"] = max(stats["error"], float(np.abs(chosen - reduced[kept]).max()))
            block = np.where(kept, rounded, float(nodata)).astype("float32")
            dst.write(block, 1, window=Window(0, out_row, plan.width, rows))
        dst.update_tags(**_display_tags(plan))
        dst.units = (plan.source.vertical_unit,)
        dst.set_band_description(1, "elevation")

    if stats["display_valid"] == 0:
        raise NodataError("display raster has no valid cells")
    mismatch = (stats["added"] + stats["dropped"]) / max(1, stats["source_valid"])
    gate = policy.max_footprint_mismatch_fraction
    if gate is not None and mismatch > gate:
        raise FootprintError(
            f"footprint mismatch {mismatch:.6g} (added {stats['added']}, dropped "
            f"{stats['dropped']} of {stats['source_valid']} source cells) exceeds the "
            f"allowed {gate:g}"
        )
    return stats


def _encode_cog(plan: DisplayPlan, display_raster: Path, destination: Path, scratch: Path):
    """Hand the rounded raster to the shared COG writer with an explicit policy.

    ``lerc_max_z_error=None`` overrides the module's lossy LERC default;
    ``compress`` and ``predictor`` are explicit so the ``RAS2CNG_COG_*``
    environment cannot change an artifact.  ``overview_quantum`` makes every
    embedded overview level obey the same quantum as the base; with it the
    writer refuses the GDAL fallback and any stacked lossy tolerance.
    """

    codec = plan.policy.codec
    options: dict[str, Any] = dict(cog.TERRAIN_CREATION_OPTIONS)
    if codec.level is not None:
        options["LEVEL"] = codec.level
    if codec.lossless_tolerance is not None:
        options["MAX_Z_ERROR"] = codec.lossless_tolerance
    return cog.area_matched_cog(
        display_raster,
        destination,
        categorical=False,
        compress=codec.compress,
        predictor=codec.predictor,
        blocksize=_BLOCK,
        scratch_dir=scratch,
        require_area_matched=True,
        lerc_max_z_error=None,
        creation_options=options,
        overview_quantum=plan.policy.increment,
    )


def write_display_terrain(
    plan: DisplayPlan,
    source_path: Path,
    destination: Path,
    *,
    scratch_dir: Path | None = None,
    overwrite: bool = False,
) -> DisplayWriteResult:
    """Write the display COG for ``plan``.  The source file is never modified.

    The destination must not be, or link to, the source (checked before any
    output operation), and an existing unrelated destination is replaced only
    with ``overwrite=True``; replacement itself is atomic.
    """

    source_path, destination = Path(source_path), Path(destination)
    _refuse_alias(source_path, destination, overwrite=overwrite)
    with tempfile.TemporaryDirectory(prefix="ras2cng-display-", dir=scratch_dir) as temporary:
        work = Path(temporary)
        base = work / "display_base.tif"
        stats = _render_display_raster(plan, source_path, base)
        report = _encode_cog(plan, base, destination, work)
    return DisplayWriteResult(
        destination=destination,
        bytes=destination.stat().st_size,
        source_valid_cells=stats["source_valid"],
        display_valid_cells=stats["display_valid"],
        footprint_area_ratio=stats["display_valid"] * plan.factor**2 / stats["source_valid"],
        min_value=stats["min"],
        max_value=stats["max"],
        observed_quantization_error=stats["error"],
        quantization_error_bound=quantization_error_bound(
            max(abs(stats["min"]), abs(stats["max"])), plan.policy.increment
        ),
        overview=asdict(report),
        tags=_display_tags(plan),
        footprint_added_source_cells=stats["added"],
        footprint_dropped_source_cells=stats["dropped"],
        footprint_mismatch_fraction=(stats["added"] + stats["dropped"]) / stats["source_valid"],
    )


# --------------------------------------------------------------------------
# Validate
# --------------------------------------------------------------------------


def _strips(height: int, rows: int) -> Iterator[tuple[int, int]]:
    for start in range(0, height, rows):
        yield start, min(rows, height - start)


def validate_display_terrain(
    plan: DisplayPlan, path: Path, *, expected: DisplayWriteResult | None = None
) -> DisplayValidation:
    """Check a written derivative against its plan.  Never raises on a bad file.

    Establishes: COG layout, CRS, grid origin/pixel size/shape, float32 +
    nodata, 512 blocks, overview levels, codec, no stacked lossy tolerance,
    **every** provenance tag of the plan's tag projection (a missing or
    different tag fails), and that every valid sample of the base level *and of
    each embedded overview level* is already on the quantum, with each level's
    valid area matching the writer's recorded area ratio.  A raster that fits
    one block has no levels; that is reported as a warning and the per-level
    check is vacuous.  This is not browser-decoder acceptance.
    """

    import numpy as np
    import rasterio
    from pyproj import CRS
    from rasterio.windows import Window

    path = Path(path)
    errors: list[str] = []
    warnings: list[str] = []
    levels_checked: list[int] = []

    layout = cog.validate_cog(path, require_mask=True)
    errors.extend(layout.errors)
    warnings.extend(layout.warnings)
    if not layout.layout_checked:
        errors.append("cloud-optimized layout was not verified (rio-cogeo missing)")

    display_valid: int | None = None
    levels_on_quantum = True
    try:
        with rasterio.open(path) as src:
            if src.crs is None or not CRS.from_wkt(plan.source.crs_wkt).equals(
                CRS.from_wkt(src.crs.to_wkt()), ignore_axis_order=True
            ):
                errors.append("CRS differs from the plan")
            if (src.width, src.height) != (plan.width, plan.height):
                errors.append(f"shape {(src.width, src.height)} differs from plan {(plan.width, plan.height)}")
            tol = 1e-6 * plan.source.pixel_size[0]
            if any(not math.isclose(x, y, rel_tol=1e-12, abs_tol=tol)
                   for x, y in zip(tuple(src.transform)[:6], plan.transform)):
                errors.append("transform (origin / pixel size) differs from the plan")
            if src.count != 1 or src.dtypes[0] != "float32":
                errors.append(f"expected one float32 band, found {src.count} x {src.dtypes[0]}")
            if src.nodata is None or float(src.nodata) != plan.policy.output_nodata:
                errors.append(f"nodata {src.nodata!r} differs from plan {plan.policy.output_nodata}")
            if src.profile.get("blockxsize") != _BLOCK or src.profile.get("blockysize") != _BLOCK:
                errors.append(f"internal blocks are not {_BLOCK}")
            factors = list(src.overviews(1))
            if factors != cog.overview_factors(src.width, src.height, blocksize=_BLOCK):
                errors.append("overview levels do not continue to a single block")

            structure = src.tags(ns="IMAGE_STRUCTURE")
            if structure.get("COMPRESSION", "").upper() != plan.policy.codec.compress:
                errors.append(
                    f"compression {structure.get('COMPRESSION')!r} differs from plan "
                    f"{plan.policy.codec.compress!r}"
                )
            reported = structure.get("PREDICTOR")
            if reported is not None and not plan.policy.codec.compress.startswith("LERC"):
                wanted = {"YES": "3", "NO": "1", "2": "2", "3": "3"}[plan.policy.codec.predictor]
                if reported != wanted:
                    errors.append(f"predictor {reported} differs from plan ({wanted})")
            if (src.units[0] if src.units else None) != plan.source.vertical_unit:
                errors.append(f"band unit {src.units[0] if src.units else None!r} differs from plan")

            tags = src.tags()
            for key, want in _display_tags(plan).items():
                got = tags.get(key)
                if got is None:
                    errors.append(f"missing provenance tag {key}")
                elif got != want:
                    errors.append(f"provenance tag {key}={got!r} differs from plan {want!r}")
            if "compression_lossy" in tags or "compression_max_z_error" in tags:
                errors.append("a lossy codec tolerance is recorded; it would stack on decimal rounding")
            if tags.get("overview_value_quantum") != f"{plan.policy.increment:g}":
                errors.append("writer did not record overview_value_quantum for the plan's quantum")
            if factors and tags.get("overview_method") != "area_matched_mean":
                errors.append(f"overview_method {tags.get('overview_method')!r} is not area_matched_mean")

            nodata = np.float32(plan.policy.output_nodata)
            inverse = plan.policy.inverse

            def off_grid_count(block, valid) -> int:
                chosen = block[valid].astype("float64")
                again = (np.rint(chosen * inverse) / inverse).astype("float32")
                return int((again != block[valid]).sum())

            off_grid = 0
            display_valid = 0
            rows = max(1, _STRIP_CELLS // max(1, src.width))
            for start, count in _strips(src.height, rows):
                block = src.read(1, window=Window(0, start, src.width, count))
                valid = block != nodata
                if np.isnan(block).any():
                    errors.append("NaN present in base level")
                    break
                display_valid += int(valid.sum())
                off_grid += off_grid_count(block, valid)
            if off_grid:
                errors.append(f"{off_grid} valid base-level samples are not on the {plan.policy.increment:g} quantum")

            ratios = [float(x) for x in tags.get("overview_area_ratios", "").split(",") if x]
            if factors and len(ratios) != len(factors):
                errors.append("overview_area_ratios does not cover every embedded level")
            for index, factor in enumerate(factors):
                with rasterio.open(path, overview_level=index) as level:
                    expect_shape = (-(-src.height // factor), -(-src.width // factor))
                    if (level.height, level.width) != expect_shape:
                        errors.append(f"overview level {factor} has shape {(level.height, level.width)}")
                        levels_on_quantum = False
                        continue
                    level_valid = 0
                    level_off = 0
                    level_rows = max(1, _STRIP_CELLS // max(1, level.width))
                    for start, count in _strips(level.height, level_rows):
                        block = level.read(1, window=Window(0, start, level.width, count))
                        if not np.isfinite(block).all():
                            errors.append(f"overview level {factor} holds a non-finite value")
                            break
                        valid = block != nodata
                        level_valid += int(valid.sum())
                        level_off += off_grid_count(block, valid)
                    levels_checked.append(factor)
                    if level_off:
                        levels_on_quantum = False
                        errors.append(
                            f"overview level {factor}: {level_off} valid samples are not on the "
                            f"{plan.policy.increment:g} quantum"
                        )
                    if index < len(ratios) and display_valid:
                        observed = level_valid * factor * factor / display_valid
                        if abs(observed - ratios[index]) > _RATIO_TOLERANCE + 1e-9:
                            errors.append(
                                f"overview level {factor}: valid area ratio {observed:.6f} differs "
                                f"from the recorded {ratios[index]:.6f}"
                            )
            if not factors:
                warnings.append(
                    "raster fits one block: it has no overview levels, so the per-level "
                    "quantization check is vacuous"
                )
    except Exception as error:  # surfaced as a validation failure, not raised
        errors.append(f"could not be read: {error}")
        levels_on_quantum = False

    if expected is not None and display_valid is not None and display_valid != expected.display_valid_cells:
        errors.append(
            f"valid footprint {display_valid} differs from written {expected.display_valid_cells}"
        )
    return DisplayValidation(
        path=path,
        valid=not errors,
        errors=errors,
        warnings=warnings,
        display_valid_cells=display_valid,
        overview_levels_quantized=bool(levels_checked) and levels_on_quantum,
        overview_levels_checked=levels_checked,
    )


# --------------------------------------------------------------------------
# Codec benchmark (tiny samples only)
# --------------------------------------------------------------------------


def benchmark_codecs(
    plan: DisplayPlan,
    source_path: Path,
    scratch_dir: Path,
    candidates: tuple[CodecPolicy, ...] = CANDIDATE_CODECS,
) -> list[CodecBenchmark]:
    """Encode one rounded raster with each candidate and report measured bytes.

    Intended for a *tiny* sample.  A candidate the local GDAL cannot write is
    reported with ``error``, never raised.  ``lossless`` is a base-level
    equality check; ``decoder_qualified`` stays False because acceptance by the
    browser decoder is a separate proof.
    """

    import numpy as np
    import rasterio
    from rasterio.windows import Window

    scratch_dir = Path(scratch_dir)
    scratch_dir.mkdir(parents=True, exist_ok=True)
    results: list[CodecBenchmark] = []
    with tempfile.TemporaryDirectory(prefix="ras2cng-display-bench-", dir=scratch_dir) as temporary:
        work = Path(temporary)
        base = work / "display_base.tif"
        _render_display_raster(plan, Path(source_path), base)
        for codec in candidates:
            candidate_plan = replace(plan, policy=replace(plan.policy, codec=codec))
            out = work / f"{codec.name}.tif"
            try:
                _encode_cog(candidate_plan, base, out, work)
                same = True
                with rasterio.open(base) as want, rasterio.open(out) as got:
                    for start, count in _strips(want.height, 256):
                        window = Window(0, start, want.width, count)
                        same &= bool(np.array_equal(want.read(1, window=window), got.read(1, window=window)))
                results.append(CodecBenchmark(codec, out.stat().st_size, same))
            except Exception as error:
                results.append(CodecBenchmark(codec, None, None, error=f"{type(error).__name__}: {error}"))
    return results


def select_smallest(benchmarks: list[CodecBenchmark]) -> CodecBenchmark | None:
    """Smallest candidate that wrote and read back exactly; ties keep list order.

    A byte decision only -- the choice still needs browser-decoder proof.
    """

    usable = [b for b in benchmarks if b.bytes is not None and b.lossless]
    return min(usable, key=lambda b: b.bytes) if usable else None


__all__ = [
    "AliasError",
    "CANDIDATE_CODECS",
    "CodecBenchmark",
    "CodecPolicy",
    "DEFLATE_FLOAT",
    "DEFLATE_INTEGER_DIFF",
    "DisplayPlan",
    "DisplayPolicy",
    "DisplayTerrainError",
    "DisplayValidation",
    "DisplayWriteResult",
    "FootprintError",
    "GridError",
    "INTERNATIONAL_FOOT",
    "LERC_ZSTD_LOSSLESS",
    "METRE",
    "NodataError",
    "RepresentationError",
    "SourceGridInfo",
    "US_SURVEY_FOOT",
    "UnitsError",
    "ZSTD_HIGH",
    "ZSTD_INTEGER_DIFF",
    "benchmark_codecs",
    "block_valid_counts",
    "choose_factor",
    "inspect_source",
    "plan_display_terrain",
    "quantization_error_bound",
    "quantize_elevation",
    "reduce_to_display_grid",
    "resolve_vertical_unit",
    "select_smallest",
    "validate_display_terrain",
    "write_display_terrain",
]
