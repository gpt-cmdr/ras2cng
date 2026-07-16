"""Controlled, unit-aware raster calculations for RASMapper-derived COGs."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import tempfile
from typing import Any, Mapping
import uuid


@dataclass(frozen=True)
class RasterRecipe:
    """Versioned definition of one allowlisted raster calculation."""

    recipe_id: str
    name: str
    input_roles: tuple[str, ...]
    input_quantities: Mapping[str, str]
    output_quantity: str
    output_dtype: str = "float32"
    output_nodata: float = -9999.0
    default_ramp: str = "rascommander.difference"
    categorical: bool = False
    requires_synchronized_profile: bool = False
    version: str = "1.0"
    description: str = ""
    parameter_defaults: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RasterRecipeResult:
    """Files and statistics produced by :func:`run_raster_recipe`."""

    output_path: Path
    provenance_path: Path
    recipe_id: str
    recipe_version: str
    units: str
    statistics: Mapping[str, float | int]


RECIPES: dict[str, RasterRecipe] = {
    "compare_wse": RasterRecipe(
        "compare_wse",
        "Compare Water Surface Elevation",
        ("baseline", "candidate"),
        {"baseline": "length", "candidate": "length"},
        "length",
        description="Candidate WSE minus baseline WSE.",
    ),
    "compare_depth": RasterRecipe(
        "compare_depth",
        "Compare Depth",
        ("baseline", "candidate"),
        {"baseline": "length", "candidate": "length"},
        "length",
        description="Candidate depth minus baseline depth.",
    ),
    "compare_velocity": RasterRecipe(
        "compare_velocity",
        "Compare Velocity",
        ("baseline", "candidate"),
        {"baseline": "velocity", "candidate": "velocity"},
        "velocity",
        description="Candidate velocity minus baseline velocity.",
    ),
    "depth_velocity": RasterRecipe(
        "depth_velocity",
        "Depth x Velocity",
        ("depth", "velocity"),
        {"depth": "length", "velocity": "velocity"},
        "depth_velocity",
        default_ramp="rascommander.depth-velocity",
        requires_synchronized_profile=True,
        description="Depth multiplied by velocity at one synchronized profile/time.",
    ),
    "depth_velocity_squared": RasterRecipe(
        "depth_velocity_squared",
        "Depth x Velocity Squared",
        ("depth", "velocity"),
        {"depth": "length", "velocity": "velocity"},
        "depth_velocity_squared",
        default_ramp="rascommander.depth-velocity",
        requires_synchronized_profile=True,
        description="Depth multiplied by velocity squared at one synchronized profile/time.",
    ),
    "hazard_class": RasterRecipe(
        "hazard_class",
        "Flood Hazard Class",
        ("depth", "velocity"),
        {"depth": "length", "velocity": "velocity"},
        "hazard_class",
        output_dtype="uint8",
        output_nodata=0,
        default_ramp="rascommander.hazard-aidr-2017",
        categorical=True,
        requires_synchronized_profile=True,
        description="AIDR Guideline 7-3 H1-H6 combined depth/velocity hazard class.",
        parameter_defaults={"standard": "aidr-2017"},
    ),
    "inundation_threshold": RasterRecipe(
        "inundation_threshold",
        "Inundation Threshold",
        ("depth",),
        {"depth": "length"},
        "boolean",
        output_dtype="uint8",
        output_nodata=255,
        default_ramp="rascommander.threshold",
        categorical=True,
        description="Boolean mask where depth exceeds a declared threshold.",
        parameter_defaults={"threshold": 0.0},
    ),
    "terrain_mod_delta": RasterRecipe(
        "terrain_mod_delta",
        "Modified Terrain Delta",
        ("base", "modified"),
        {"base": "length", "modified": "length"},
        "length",
        description="Modified terrain elevation minus base terrain elevation.",
    ),
}


_UNIT_ALIASES = {
    "ft": ("length", "imperial", "ft", 0.3048),
    "foot": ("length", "imperial", "ft", 0.3048),
    "feet": ("length", "imperial", "ft", 0.3048),
    "us survey foot": ("length", "imperial", "ft", 1200.0 / 3937.0),
    "us survey feet": ("length", "imperial", "ft", 1200.0 / 3937.0),
    "m": ("length", "metric", "m", 1.0),
    "meter": ("length", "metric", "m", 1.0),
    "meters": ("length", "metric", "m", 1.0),
    "metre": ("length", "metric", "m", 1.0),
    "metres": ("length", "metric", "m", 1.0),
    "ft/s": ("velocity", "imperial", "ft/s", 0.3048),
    "fps": ("velocity", "imperial", "ft/s", 0.3048),
    "feet/second": ("velocity", "imperial", "ft/s", 0.3048),
    "m/s": ("velocity", "metric", "m/s", 1.0),
    "mps": ("velocity", "metric", "m/s", 1.0),
    "meters/second": ("velocity", "metric", "m/s", 1.0),
}


def list_raster_recipes() -> tuple[RasterRecipe, ...]:
    """Return the stable allowlist of supported calculation recipes."""

    return tuple(RECIPES.values())


def get_raster_recipe(recipe_id: str) -> RasterRecipe:
    """Return one recipe or raise with the complete allowlist."""

    try:
        return RECIPES[recipe_id]
    except KeyError as error:
        raise ValueError(
            f"Unknown raster recipe {recipe_id!r}. Available: {', '.join(RECIPES)}"
        ) from error


def run_raster_recipe(
    recipe_id: str,
    inputs: Mapping[str, Path],
    output_path: Path,
    *,
    input_units: Mapping[str, str] | None = None,
    parameters: Mapping[str, Any] | None = None,
    plan: str | None = None,
    profile: str | None = None,
    scratch_dir: Path | None = None,
    block_size: int = 512,
    hash_assets: bool = False,
    overwrite: bool = False,
) -> RasterRecipeResult:
    """Execute an allowlisted recipe over aligned numeric rasters.

    Inputs must already share a CRS, transform, dimensions, and pixel grid. This
    function intentionally does not reproject or interpolate hydraulic results.
    Source surfaces should first be generated by RASMapper/RasProcess.
    """

    import numpy as np
    import rasterio
    from rasterio.enums import Resampling
    from rasterio.shutil import copy as copy_raster
    from rasterio.windows import Window

    recipe = get_raster_recipe(recipe_id)
    supplied_roles = set(inputs)
    required_roles = set(recipe.input_roles)
    if supplied_roles != required_roles:
        raise ValueError(
            f"Recipe {recipe_id} requires inputs {sorted(required_roles)}; "
            f"received {sorted(supplied_roles)}"
        )
    if block_size < 64 or block_size > 4096:
        raise ValueError("block_size must be between 64 and 4096 pixels")
    if recipe.requires_synchronized_profile:
        normalized_profile = str(profile or "").strip().lower()
        if not normalized_profile or normalized_profile in {"max", "maximum", "min", "minimum"}:
            raise ValueError(
                f"Recipe {recipe_id} requires one synchronized timestep/profile; "
                "independent Max/Min rasters are not valid inputs"
            )

    output_path = Path(output_path)
    if output_path.suffix.lower() not in {".tif", ".tiff"}:
        raise ValueError("Raster recipe output must be a .tif or .tiff")
    if output_path.exists() and not overwrite:
        raise FileExistsError(f"Raster recipe output already exists: {output_path}")
    output_path.parent.mkdir(parents=True, exist_ok=True)

    input_paths = {role: Path(inputs[role]).resolve() for role in recipe.input_roles}
    missing = [str(path) for path in input_paths.values() if not path.is_file()]
    if missing:
        raise FileNotFoundError(f"Raster recipe inputs do not exist: {', '.join(missing)}")

    sources = {role: rasterio.open(path) for role, path in input_paths.items()}
    try:
        reference = sources[recipe.input_roles[0]]
        _validate_grids(sources, reference)
        units = _resolve_units(recipe, sources, input_units or {})
        effective_parameters = dict(recipe.parameter_defaults)
        effective_parameters.update(parameters or {})
        _validate_parameters(recipe, effective_parameters)
        output_units = _output_units(recipe, units)

        scratch_parent = Path(scratch_dir).resolve() if scratch_dir else output_path.parent
        scratch_parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="ras2cng-recipe-", dir=scratch_parent) as temp_name:
            temporary = Path(temp_name) / "calculated.tif"
            profile_options = reference.profile.copy()
            profile_options.update(
                driver="GTiff",
                count=1,
                dtype=recipe.output_dtype,
                nodata=recipe.output_nodata,
                tiled=True,
                blockxsize=_valid_tiff_block(block_size),
                blockysize=_valid_tiff_block(block_size),
                compress="ZSTD",
                predictor=2 if recipe.categorical else 3,
                BIGTIFF="IF_SAFER",
            )
            aggregate = _StatisticsAccumulator()
            with rasterio.open(temporary, "w", **profile_options) as destination:
                destination.update_tags(
                    recipe_id=recipe.recipe_id,
                    recipe_version=recipe.version,
                    units=output_units,
                    interpolation_authority="RASMapper/RasProcess source rasters",
                )
                destination.update_tags(1, units=output_units)
                for row in range(0, reference.height, block_size):
                    height = min(block_size, reference.height - row)
                    for col in range(0, reference.width, block_size):
                        width = min(block_size, reference.width - col)
                        window = Window(col, row, width, height)
                        arrays = {
                            role: source.read(1, window=window, masked=True, out_dtype="float64")
                            for role, source in sources.items()
                        }
                        values, valid = _calculate_window(
                            recipe,
                            arrays,
                            units,
                            effective_parameters,
                        )
                        aggregate.update(values[valid])
                        filled = np.full(values.shape, recipe.output_nodata, dtype=recipe.output_dtype)
                        filled[valid] = values[valid].astype(recipe.output_dtype, copy=False)
                        destination.write(filled, 1, window=window)

                factors = _overview_factors(reference.width, reference.height)
                if factors:
                    overview_resampling = Resampling.nearest if recipe.categorical else Resampling.average
                    destination.build_overviews(factors, overview_resampling)
                    destination.update_tags(
                        ns="rio_overview",
                        resampling=overview_resampling.name,
                    )

            staged_output = output_path.with_name(
                f".{output_path.name}.{uuid.uuid4().hex}.tmp"
            )
            try:
                copy_raster(
                    temporary,
                    staged_output,
                    driver="COG",
                    compress="ZSTD",
                    blocksize=_valid_tiff_block(block_size),
                    overview_resampling="nearest" if recipe.categorical else "average",
                    BIGTIFF="IF_SAFER",
                )
                staged_output.replace(output_path)
            finally:
                staged_output.unlink(missing_ok=True)

        statistics = aggregate.to_dict()
        provenance_path = output_path.with_suffix(".provenance.json")
        provenance = {
            "schema": "ras2cng.raster-recipe/v1",
            "recipe": asdict(recipe),
            "generatedAt": datetime.now(timezone.utc).isoformat(),
            "authority": "ras2cng controlled raster arithmetic",
            "interpolationAuthority": "RASMapper/RasProcess source rasters",
            "plan": plan,
            "profile": profile,
            "inputs": {
                role: {
                    "file": path.name,
                    "sizeBytes": path.stat().st_size,
                    "modifiedNs": path.stat().st_mtime_ns,
                    "units": units[role][2],
                    **({"sha256": _sha256(path)} if hash_assets else {}),
                }
                for role, path in input_paths.items()
            },
            "parameters": effective_parameters,
            "output": {
                "file": output_path.name,
                "sizeBytes": output_path.stat().st_size,
                "modifiedNs": output_path.stat().st_mtime_ns,
                **({"sha256": _sha256(output_path)} if hash_assets else {}),
                "units": output_units,
                "dtype": recipe.output_dtype,
                "nodata": recipe.output_nodata,
                "statistics": statistics,
                "crs": reference.crs.to_string(),
                "transform": list(reference.transform)[:6],
                "width": reference.width,
                "height": reference.height,
            },
        }
        provenance_path.write_text(json.dumps(provenance, indent=2) + "\n", encoding="utf-8")
        return RasterRecipeResult(
            output_path=output_path,
            provenance_path=provenance_path,
            recipe_id=recipe.recipe_id,
            recipe_version=recipe.version,
            units=output_units,
            statistics=statistics,
        )
    finally:
        for source in sources.values():
            source.close()


def _validate_grids(sources, reference) -> None:
    if reference.crs is None:
        raise ValueError("Raster recipe inputs require a CRS")
    for role, source in sources.items():
        if source.count < 1:
            raise ValueError(f"Raster input {role} has no bands")
        if source.crs != reference.crs:
            raise ValueError(f"Raster input {role} has a different CRS")
        if source.width != reference.width or source.height != reference.height:
            raise ValueError(f"Raster input {role} has different dimensions")
        if not source.transform.almost_equals(reference.transform):
            raise ValueError(f"Raster input {role} is not aligned to the reference pixel grid")


def _resolve_units(recipe, sources, supplied: Mapping[str, str]):
    resolved = {}
    for role in recipe.input_roles:
        raw = supplied.get(role) or _raster_unit_tag(sources[role])
        key = str(raw or "").strip().lower()
        if key not in _UNIT_ALIASES:
            raise ValueError(
                f"Input unit for {role!r} is required and must be one of: "
                f"{', '.join(sorted(_UNIT_ALIASES))}"
            )
        definition = _UNIT_ALIASES[key]
        expected = recipe.input_quantities[role]
        if definition[0] != expected:
            raise ValueError(f"Input {role!r} requires {expected} units, not {raw!r}")
        resolved[role] = definition

    if recipe.recipe_id.startswith("compare_") or recipe.recipe_id == "terrain_mod_delta":
        canonical = {definition[2] for definition in resolved.values()}
        if len(canonical) != 1:
            raise ValueError(f"Recipe {recipe.recipe_id} requires matching input units")
    if recipe.output_quantity.startswith("depth_velocity"):
        systems = {definition[1] for definition in resolved.values()}
        if len(systems) != 1:
            raise ValueError(f"Recipe {recipe.recipe_id} requires one consistent unit system")
    return resolved


def _raster_unit_tag(source) -> str | None:
    band_tags = source.tags(1)
    dataset_tags = source.tags()
    for tags in (band_tags, dataset_tags):
        for key in ("units", "Units", "UNITTYPE", "unit"):
            if tags.get(key):
                return tags[key]
    return None


def _validate_parameters(recipe: RasterRecipe, parameters: Mapping[str, Any]) -> None:
    allowed = set(recipe.parameter_defaults)
    unknown = set(parameters) - allowed
    if unknown:
        raise ValueError(f"Unsupported parameters for {recipe.recipe_id}: {sorted(unknown)}")
    if recipe.recipe_id == "hazard_class" and parameters.get("standard") != "aidr-2017":
        raise ValueError("hazard_class currently supports only standard='aidr-2017'")
    if recipe.recipe_id == "inundation_threshold":
        threshold = float(parameters["threshold"])
        if not math.isfinite(threshold) or threshold < 0:
            raise ValueError("inundation_threshold requires a finite, non-negative threshold")


def _output_units(recipe: RasterRecipe, units) -> str:
    if recipe.output_quantity in {"length", "velocity"}:
        return units[recipe.input_roles[0]][2]
    if recipe.output_quantity == "depth_velocity":
        return "ft2/s" if units["depth"][1] == "imperial" else "m2/s"
    if recipe.output_quantity == "depth_velocity_squared":
        return "ft3/s2" if units["depth"][1] == "imperial" else "m3/s2"
    if recipe.output_quantity == "hazard_class":
        return "H1-H6"
    return "boolean"


def _calculate_window(recipe, arrays, units, parameters):
    import numpy as np

    masks = [np.ma.getmaskarray(arrays[role]) for role in recipe.input_roles]
    valid = ~np.logical_or.reduce(masks)
    values = {role: np.asarray(arrays[role].data, dtype="float64") for role in recipe.input_roles}
    valid &= np.logical_and.reduce([np.isfinite(values[role]) for role in recipe.input_roles])

    if recipe.recipe_id.startswith("compare_"):
        output = values["candidate"] - values["baseline"]
    elif recipe.recipe_id == "terrain_mod_delta":
        output = values["modified"] - values["base"]
    elif recipe.recipe_id == "depth_velocity":
        valid &= (values["depth"] >= 0) & (values["velocity"] >= 0)
        output = values["depth"] * values["velocity"]
    elif recipe.recipe_id == "depth_velocity_squared":
        valid &= (values["depth"] >= 0) & (values["velocity"] >= 0)
        output = values["depth"] * np.square(values["velocity"])
    elif recipe.recipe_id == "inundation_threshold":
        valid &= values["depth"] >= 0
        output = (values["depth"] > float(parameters["threshold"])).astype("uint8")
    elif recipe.recipe_id == "hazard_class":
        depth = values["depth"] * units["depth"][3]
        velocity = values["velocity"] * units["velocity"][3]
        valid &= (depth >= 0) & (velocity >= 0)
        product = depth * velocity
        output = np.full(depth.shape, 6, dtype="uint8")
        remaining = valid.copy()
        for class_id, product_limit, depth_limit, velocity_limit in (
            (1, 0.3, 0.3, 2.0),
            (2, 0.6, 0.5, 2.0),
            (3, 0.6, 1.2, 2.0),
            (4, 1.0, 2.0, 2.0),
            (5, 4.0, 4.0, 4.0),
        ):
            selected = remaining & (product <= product_limit) & (depth <= depth_limit) & (velocity <= velocity_limit)
            output[selected] = class_id
            remaining[selected] = False
    else:  # pragma: no cover - guarded by the recipe allowlist
        raise ValueError(f"No implementation for raster recipe {recipe.recipe_id}")
    valid &= np.isfinite(output)
    return output, valid


@dataclass
class _StatisticsAccumulator:
    count: int = 0
    minimum: float = math.inf
    maximum: float = -math.inf
    total: float = 0.0
    total_squares: float = 0.0

    def update(self, values) -> None:
        if values.size == 0:
            return
        self.count += int(values.size)
        self.minimum = min(self.minimum, float(values.min()))
        self.maximum = max(self.maximum, float(values.max()))
        self.total += float(values.sum(dtype="float64"))
        self.total_squares += float((values.astype("float64") ** 2).sum(dtype="float64"))

    def to_dict(self) -> dict[str, float | int]:
        if self.count == 0:
            raise ValueError("Raster recipe produced no valid output pixels")
        mean = self.total / self.count
        variance = max(0.0, self.total_squares / self.count - mean * mean)
        return {
            "minimum": self.minimum,
            "maximum": self.maximum,
            "mean": mean,
            "stddev": math.sqrt(variance),
            "valid_pixels": self.count,
        }


def _overview_factors(width: int, height: int) -> list[int]:
    factors = []
    factor = 2
    while max(width, height) / factor >= 256 and factor <= 128:
        factors.append(factor)
        factor *= 2
    return factors


def _valid_tiff_block(value: int) -> int:
    return max(64, min(4096, int(math.ceil(value / 16.0) * 16)))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
