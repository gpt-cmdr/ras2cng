"""Optional Rust 2D inundation mapping (ras-commander-fast-map) for ras2cng.

``ras2cng map`` renders result rasters through RAS Mapper
(``RasStoreMapHelper.exe``) by default. ``map_engine="fast-map"`` instead maps
WSE and Depth through ``RasFim`` and the native Rust ``ras2fim-batch`` engine
from ``ras-commander-fast-map``. On 2D-only models this is faster than RAS
Mapper. ras-commander's comparisons against native RAS Mapper 7.0.1 found
identical float32 values and validity masks on five eBFE / example models,
1.2-3.6x faster for horizontal and plain sloping, and 1.8-7.3x for
depth-weighted sloping faces. See
https://rascommander.info/ras/development/fim-mapping-performance/.

Scope. Everything outside it falls back to RAS Mapper unless fallback is
disabled:

* **Pure 2D models.** ``RasFim.validate_2d_model`` rejects any model with 1D
  river reaches or cross sections. ras2cng also sends models with storage
  areas to RAS Mapper: fast-map does not render storage-area surfaces, so its
  map would leave them blank. (Checked on a Bald Eagle dam-break plan: inside
  the 2D mesh every pixel matched RAS Mapper bit for bit, and the only
  difference was the 466,912 storage-area pixels RAS Mapper drew.) Pipe
  networks are permitted.
* **WSE and Depth, Max profile.** Velocity, Froude, arrival time, and the
  other map types always go through RAS Mapper.
* **Inundation boundary, Max profile** (``RasFim.map_inundation_boundaries``,
  ras-commander PR #709 / fast-map PR #5). RAS Mapper's boundary export is
  very slow; on Davis the Rust batch took 3.5 s against 85 s for two native
  calls. It is a different, deterministic method
  (``deterministic-zero-depth-contours/1``), not vector parity: polygons
  differ slightly from RAS Mapper's, whose own polygons vary between
  identical exports. Horizontal and plain sloping only, and it needs the
  original **single-source, unmodified** terrain TIFF plus its terrain HDF
  (not the export used for rasters). Multi-source terrains fall back.
  ``fast_map_boundary=False`` keeps boundaries on RAS Mapper.
* **Render modes.** ``horizontal`` (cell-constant), ``sloping`` (cell
  corners), and ``slopingPretty`` (cell faces, with the project's
  shallow-to-horizontal and depth-weighting flags). Depth weighting needs
  stored corner ground elevations; without them the plan falls back.
* **Terrain.** One float32 GeoTIFF of the exact registered terrain, with
  modifications applied. Supply ``fast_map_terrain`` to reuse an export.
  Otherwise ras2cng exports the plan's terrain once per run through RAS
  Mapper (``RasTerrain.export_rasmapper_terrain``), so this step still needs
  HEC-RAS (Windows or Wine). The published speed-ups exclude that export.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any, Optional

RASMAPPER = "rasmapper"
FAST_MAP = "fast-map"
MAP_ENGINES = (RASMAPPER, FAST_MAP)
MAP_ENGINE_ENV = "RAS2CNG_MAP_ENGINE"

# Map types the Rust engine produces. Everything else stays on RAS Mapper.
FAST_MAP_RASTER_TYPES = ("wse", "depth")
FAST_MAP_BOUNDARY = "inundation_boundary"
FAST_MAP_TYPES = FAST_MAP_RASTER_TYPES + (FAST_MAP_BOUNDARY,)
FAST_MAP_RENDER_MODES = ("horizontal", "sloping", "slopingPretty")
# Deterministic contours support horizontal and plain cell-corner sloping only.
FAST_MAP_BOUNDARY_RENDER_MODES = ("horizontal", "sloping")
FAST_MAP_MANIFEST = "fast_map_manifest.json"
FAST_MAP_BOUNDARY_MANIFEST = "fast_map_boundary_manifest.json"

_COMPLETE_STATUSES = {"complete", "no_flooded_pixels"}


class FastMapUnavailable(RuntimeError):
    """The plan, profile, terrain or installation is outside fast-map's scope."""


def resolve_map_engine(engine: Optional[str] = None) -> str:
    """Return the selected engine; ``None`` reads ``RAS2CNG_MAP_ENGINE``."""
    value = engine if engine is not None else os.environ.get(MAP_ENGINE_ENV, RASMAPPER)
    normalized = str(value).strip().lower().replace("_", "-")
    if normalized in ("fastmap", "rust"):
        normalized = FAST_MAP
    if normalized not in MAP_ENGINES:
        raise ValueError(
            f"map engine must be one of {', '.join(MAP_ENGINES)}; got {value!r}"
        )
    return normalized


def default_threads() -> int:
    """Rust worker count: all but one logical CPU, within the engine's 1-64."""
    return max(1, min((os.cpu_count() or 2) - 1, 64))


def default_working_mib() -> int:
    """Engine memory admission: a quarter of physical RAM, 1-16 GiB.

    ras-commander's own default (1 GiB) refuses ordinary eBFE-sized windows.
    This is planned admission, not a hard RSS cap.
    """
    try:
        import psutil

        total_mib = psutil.virtual_memory().total // (1024 * 1024)
    except Exception:
        return 4096
    return int(max(1024, min(total_mib // 4, 16384)))


def fast_map_status() -> dict[str, Any]:
    """Report whether ``RasFim`` and the ``ras2fim-batch`` engine are usable."""
    try:
        from ras_commander import RasFim
    except ImportError as exc:
        return {"available": False, "reason": f"ras-commander has no RasFim: {exc}"}
    try:
        engine = RasFim.find_engine()
    except Exception as exc:  # FimEngineNotFoundError and platform failures
        return {
            "available": False,
            "reason": (
                f"{exc}. Install it with `pip install ras-commander-fast-map` "
                "(Windows x64 and Linux x64 wheels)."
            ),
        }
    info: dict[str, Any] = {"available": True, "engine_path": str(engine)}
    try:
        info["engine"] = RasFim.get_engine_info(engine)
    except Exception:
        pass
    return info


def plan_terrain_name(plan_hdf: Path) -> Optional[str]:
    """Return the terrain layer the plan's geometry was computed against."""
    import h5py

    try:
        with h5py.File(plan_hdf, "r") as hdf:
            if "Geometry" not in hdf:
                return None
            value = hdf["Geometry"].attrs.get("Terrain Layername")
    except OSError:
        return None
    if value is None:
        return None
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    if hasattr(value, "item"):
        value = value.item()
        if isinstance(value, bytes):
            value = value.decode("utf-8", errors="replace")
    text = str(value).strip()
    return text or None


def render_settings(ras: Any, render_mode: Optional[str]) -> dict[str, Any]:
    """Resolve the water-surface render mode and slopingPretty flags.

    An explicit ``render_mode`` overrides the mode. The slopingPretty flags
    always come from the project's ``.rasmap``, as they do for RAS Mapper,
    which keeps them when ``RasProcess`` sets only the mode.
    """
    stored: Optional[dict] = None
    try:
        from ras_commander import RasMap

        stored = RasMap.get_water_surface_render_mode(ras_object=ras)
    except Exception:
        stored = None
    mode = render_mode or (stored or {}).get("mode") or "horizontal"
    if mode not in FAST_MAP_RENDER_MODES:
        raise FastMapUnavailable(f"fast-map has no {mode!r} render mode")
    pretty = mode == "slopingPretty"
    return {
        "mode": mode,
        "reduce_shallow_to_horizontal": bool(
            pretty and (stored or {}).get("reduce_shallow_to_horizontal")
        ),
        "use_depth_weighted_faces": bool(
            pretty and (stored or {}).get("use_depth_weighted_faces")
        ),
    }


def boundary_supported() -> bool:
    """True when the installed ras-commander has Rust inundation boundaries."""
    try:
        from ras_commander import RasFim
    except ImportError:
        return False
    return callable(getattr(RasFim, "map_inundation_boundaries", None))


def plan_terrain_hdf(plan_hdf: Path, project_dir: Path) -> Optional[Path]:
    """Return the terrain HDF the plan's geometry was computed against."""
    import h5py

    try:
        with h5py.File(plan_hdf, "r") as hdf:
            value = hdf["Geometry"].attrs.get("Terrain Filename") if "Geometry" in hdf else None
    except OSError:
        return None
    if value is None:
        return None
    if hasattr(value, "item"):
        value = value.item()
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    text = str(value).strip().replace("\\", "/")
    if not text:
        return None
    path = Path(text)
    return path if path.is_absolute() else (Path(project_dir) / path).resolve()


def _check_scope(RasFim: Any, plan_hdf: Path) -> None:
    """Raise FastMapUnavailable for 1D elements, storage areas or no engine."""
    from ras_commander.fim.errors import FimEngineNotFoundError, FimUnsupportedError

    try:
        scope = RasFim.validate_2d_model(plan_hdf)
        RasFim.find_engine()
    except (FimUnsupportedError, FimEngineNotFoundError) as exc:
        raise FastMapUnavailable(str(exc)) from exc
    storage_areas = int(scope.get("storage_areas") or 0) if isinstance(scope, dict) else 0
    if storage_areas:
        raise FastMapUnavailable(
            f"model has {storage_areas} storage area(s); fast-map does not render "
            "storage-area surfaces, so they would be missing from the map"
        )


def _slug(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("_") or "terrain"


class TerrainResolver:
    """Resolve, and export at most once per run, the terrain for each plan."""

    def __init__(
        self,
        prj_file: Path,
        work_dir: Path,
        *,
        terrain_path: Optional[Path] = None,
        terrain_name: Optional[str] = None,
        ras_version: Optional[str] = None,
        terrain_hdf: Optional[Path] = None,
    ) -> None:
        self.prj_file = Path(prj_file)
        self.ras_version = ras_version
        self.terrain_hdf = Path(terrain_hdf) if terrain_hdf is not None else None
        self.work_dir = Path(work_dir)
        self.terrain_path = Path(terrain_path) if terrain_path is not None else None
        self.terrain_name = terrain_name
        self._exported: dict[Optional[str], Path] = {}

    def for_plan(self, plan_hdf: Path) -> Path:
        if self.terrain_path is not None:
            if not self.terrain_path.is_file():
                raise FastMapUnavailable(f"fast-map terrain not found: {self.terrain_path}")
            return self.terrain_path
        name = self.terrain_name or plan_terrain_name(plan_hdf)
        if name in self._exported:
            return self._exported[name]
        from ras2cng.terrain import export_modified_terrain

        target = self.work_dir / f"{_slug(name or 'terrain')}.tif"
        try:
            exported = export_modified_terrain(
                self.prj_file, target, terrain_name=name, overwrite=True,
                hecras_version=self.ras_version,
            )
        except Exception as exc:
            raise FastMapUnavailable(
                f"could not export the registered terrain {name or '(default)'!r} "
                f"for fast-map: {exc}. Supply a float32 export with --fast-map-terrain."
            ) from exc
        self._exported[name] = Path(exported)
        return self._exported[name]

    def boundary_terrain(self, plan_hdf: Path) -> tuple[Path, Path]:
        """Return the original single-source ``(terrain TIFF, terrain HDF)``.

        Boundaries need the registered terrain's own masks, perimeters and
        stitch TIN, so the flattened raster export cannot be used. A terrain
        with more than one source TIFF is refused; fast-map also refuses
        terrain modifications.
        """
        from ras2cng.terrain import _discover_tifs_for_hdf, discover_terrains

        hdf = self.terrain_hdf
        if hdf is None and self.terrain_name:
            for info in discover_terrains(self.prj_file.parent):
                if info.name == self.terrain_name and info.hdf_path is not None:
                    hdf = Path(info.hdf_path)
                    break
        if hdf is None:
            hdf = plan_terrain_hdf(plan_hdf, self.prj_file.parent)
        if hdf is None or not Path(hdf).is_file():
            raise FastMapUnavailable(
                f"terrain HDF for {Path(plan_hdf).name} not found ({hdf}); "
                "supply it with --fast-map-terrain-hdf"
            )
        tifs = _discover_tifs_for_hdf(Path(hdf))
        if len(tifs) != 1:
            raise FastMapUnavailable(
                f"Rust inundation boundaries need a single-source terrain; "
                f"{Path(hdf).name} has {len(tifs)} source TIFF(s)"
            )
        return tifs[0], Path(hdf)


def map_plan(
    plan_hdf: Path,
    terrain_tif: Path,
    output_dir: Path,
    *,
    map_types: list[str],
    profile: str = "Max",
    render: dict[str, Any],
    threads: Optional[int] = None,
    writer_threads: int = 2,
    timeout: Optional[float] = None,
    max_working_mib: Optional[int] = None,
) -> dict[str, list[Path]]:
    """Map one plan's Max WSE/Depth with the Rust engine into ``output_dir``.

    Products are named like RAS Mapper's (``WSE (Max).tif``,
    ``Depth (Max).tif``) so downstream discovery is unchanged. The engine's
    product manifest is kept as ``fast_map_manifest.json``. Raises
    :class:`FastMapUnavailable` for anything outside fast-map's scope, and lets
    engine failures propagate.
    """
    requested = [name for name in map_types if name in FAST_MAP_RASTER_TYPES]
    if not requested:
        return {}
    if profile != "Max":
        raise FastMapUnavailable(
            f"fast-map maps the Max profile only through ras2cng (got {profile!r})"
        )
    try:
        from ras_commander import RasFim
        from ras_commander.fim.errors import FimUnsupportedError
    except ImportError as exc:
        raise FastMapUnavailable(f"ras-commander has no RasFim: {exc}") from exc

    plan_hdf = Path(plan_hdf)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    threads = default_threads() if threads is None else int(threads)
    _check_scope(RasFim, plan_hdf)

    work = Path(tempfile.mkdtemp(prefix=".fast-map-", dir=output_dir))
    try:
        common = dict(
            max_working_mib=(
                default_working_mib() if max_working_mib is None else int(max_working_mib)
            ),
            threads=threads,
            writer_threads=writer_threads,
            timeout=timeout,
            write_floodplain=False,
        )
        try:
            if render["mode"] == "horizontal":
                cells = RasFim.get_cells_from_plan_hdf(
                    plan_hdf, mesh_name="*", profile_name="wsel_max",
                )
                manifest = RasFim.map_horizontal_profiles(
                    cells, terrain_tif, work, profiles=["wsel_max"],
                    hdf_path=plan_hdf, **common,
                )
            else:
                pretty = render["mode"] == "slopingPretty"
                manifest = RasFim.map_sloping_profiles(
                    plan_hdf, terrain_tif, work,
                    profiles={"Max": "Max"},
                    render_mode=render["mode"],
                    reduce_shallow_to_horizontal=pretty and render["reduce_shallow_to_horizontal"],
                    use_depth_weighted_faces=pretty and render["use_depth_weighted_faces"],
                    **common,
                )
        except FimUnsupportedError as exc:
            raise FastMapUnavailable(str(exc)) from exc

        row = manifest.iloc[0]
        if row["status"] not in _COMPLETE_STATUSES:
            raise RuntimeError(f"fast-map {row['status']}: {row.get('error')}")

        products = {"wse": row.get("wse_tif"), "depth": row.get("depth_tif")}
        labels = {"wse": "WSE", "depth": "Depth"}
        result: dict[str, list[Path]] = {}
        for name in requested:
            source = products.get(name)
            if not source or not Path(source).is_file():
                raise RuntimeError(f"fast-map reported no {name} product")
            target = output_dir / f"{labels[name]} ({profile}).tif"
            os.replace(source, target)
            result[name] = [target]

        engine_manifest = work / "fim_manifest.json"
        if engine_manifest.is_file():
            details = json.loads(engine_manifest.read_text(encoding="utf-8"))
            details["ras2cng"] = {
                "plan_hdf": str(plan_hdf),
                "terrain": str(terrain_tif),
                "render": render,
                "products": {k: [str(p) for p in v] for k, v in result.items()},
            }
            (output_dir / FAST_MAP_MANIFEST).write_text(
                json.dumps(details, indent=2, default=str), encoding="utf-8"
            )
        return result
    finally:
        shutil.rmtree(work, ignore_errors=True)


def map_boundary(
    plan_hdf: Path,
    terrain_tif: Path,
    terrain_hdf: Path,
    output_dir: Path,
    *,
    profile: str = "Max",
    render: dict[str, Any],
    threads: Optional[int] = None,
    timeout: Optional[float] = None,
    max_working_mib: Optional[int] = None,
) -> dict[str, list[Path]]:
    """Map one plan's Max inundation boundary with the Rust engine.

    Writes ``Inundation Boundary (Max).geojson`` (a MultiPolygon with holes
    in the model CRS) and ``fast_map_boundary_manifest.json``. A plan with no
    flooded area returns an empty list. Raises :class:`FastMapUnavailable`
    for anything outside the boundary method's scope.
    """
    if profile != "Max":
        raise FastMapUnavailable(
            f"fast-map maps the Max profile only through ras2cng (got {profile!r})"
        )
    if render["mode"] not in FAST_MAP_BOUNDARY_RENDER_MODES:
        raise FastMapUnavailable(
            "Rust inundation boundaries support horizontal and plain sloping, "
            f"not {render['mode']!r}"
        )
    try:
        from ras_commander import RasFim
        from ras_commander.fim.errors import FimUnsupportedError
    except ImportError as exc:
        raise FastMapUnavailable(f"ras-commander has no RasFim: {exc}") from exc
    if not boundary_supported():
        raise FastMapUnavailable(
            "the installed ras-commander has no RasFim.map_inundation_boundaries "
            "(ras-commander PR #709 with ras-commander-fast-map PR #5)"
        )

    plan_hdf = Path(plan_hdf)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    _check_scope(RasFim, plan_hdf)
    budget = default_working_mib() if max_working_mib is None else int(max_working_mib)

    work = Path(tempfile.mkdtemp(prefix=".fast-map-boundary-", dir=output_dir))
    try:
        try:
            manifest = RasFim.map_inundation_boundaries(
                plan_hdf, terrain_tif, work,
                terrain_hdf=terrain_hdf,
                profiles={"Max": "Max"},
                interpolation=render["mode"],
                threads=default_threads() if threads is None else int(threads),
                max_working_mib=budget,
                max_prepared_mib=budget,
                timeout=timeout,
            )
        except FimUnsupportedError as exc:
            raise FastMapUnavailable(str(exc)) from exc

        row = manifest.iloc[0]
        if row["status"] == "empty":
            products: list[Path] = []
        elif row["status"] == "complete":
            source = row.get("boundary_geojson")
            if not source or not Path(source).is_file():
                raise RuntimeError("fast-map reported no inundation boundary product")
            target = output_dir / f"Inundation Boundary ({profile}).geojson"
            os.replace(source, target)
            products = [target]
        else:
            raise RuntimeError(f"fast-map boundary {row['status']}: {row.get('error')}")

        engine_manifest = work / "boundary_manifest.json"
        details = (
            json.loads(engine_manifest.read_text(encoding="utf-8"))
            if engine_manifest.is_file() else {}
        )

        def _plain(value):
            return value.item() if hasattr(value, "item") else value

        details["ras2cng"] = {
            "plan_hdf": str(plan_hdf),
            "terrain": str(terrain_tif),
            "terrain_hdf": str(terrain_hdf),
            "interpolation": render["mode"],
            "method": "deterministic-zero-depth-contours/1 (not RAS Mapper vector parity)",
            "statistics": {
                key: _plain(row.get(key))
                for key in ("polygons", "holes", "vertices", "area", "crs")
            },
            "products": [str(path) for path in products],
        }
        (output_dir / FAST_MAP_BOUNDARY_MANIFEST).write_text(
            json.dumps(details, indent=2, default=str), encoding="utf-8"
        )
        return {FAST_MAP_BOUNDARY: products}
    finally:
        shutil.rmtree(work, ignore_errors=True)
