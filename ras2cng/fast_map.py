"""Optional Rust 2D inundation mapping (ras-commander-fast-map) for ras2cng.

``ras2cng map`` maps the products below through ``RasFim`` and the native
Rust ``ras2fim-batch`` engine from ``ras-commander-fast-map`` by default
(:data:`DEFAULT_MAP_ENGINE`), and renders everything else through RAS Mapper
(``RasStoreMapHelper.exe``). ``map_engine="rasmapper"`` (or
``RAS2CNG_MAP_ENGINE=rasmapper``) forces RAS Mapper for every product. The
render mode comes from the project's ``.rasmap``; with none stored, the plan
goes to RAS Mapper, which applies its own default (:func:`render_settings`).
:func:`preflight` checks scope, engine and render mode before any terrain
work.

**Scope: pure 2D models, built for FIM library creation, not general use
across all model types.** Every product here is qualified against RAS Mapper
output: pixel conformance against native HEC-RAS 7.0.1 stored maps on the
named models. Everything outside the scope (1D reaches or cross sections,
storage areas, non-Max profiles, unqualified products) falls back to RAS
Mapper per product group, with the reason printed, unless fallback is
disabled (``--fast-map-strict``). ``MapResult.engines`` records which engine
produced each map type and ``MapResult.fast_map_terrain`` the terrain used.

* **Pure 2D models.** ``RasFim.validate_2d_model`` rejects any model with 1D
  river reaches or cross sections. Models with storage areas also go to RAS
  Mapper: fast-map does not render storage-area surfaces. (On a Bald Eagle
  dam-break plan every 2D-mesh pixel matched RAS Mapper and only the 466,912
  storage-area pixels differed.) Pipe networks are permitted.
* **WSE and Depth, Max profile.** ras-commander's comparison found identical
  float32 values and masks on five eBFE / example models, 1.2-7.3x faster.
  Render modes ``horizontal``, ``sloping`` and ``slopingPretty`` (with the
  project's shallow-to-horizontal and depth-weighting flags; depth weighting
  needs stored corner elevations).
* **Arrival time and percent time inundated** (fast-map #12, ras-commander
  #724). Wet means depth > ``arrival_depth``; arrival time is in hours since
  the simulation start. Identical to RAS Mapper on Davis p02 and Bald Eagle
  bridges p05 at 0.5 ft, in horizontal and plain sloping (RAS Mapper draws
  them the same in both). Named as RAS Mapper names them
  (``Arrival Time (0.5ft hrs).tif``). Duration stays on RAS Mapper (the
  engine marks it unqualified). ``fast_map_whole_simulation=False`` keeps
  them on RAS Mapper.
* **Velocity, Max, plain sloping, opt-in** (``fast_map_velocity=True``;
  fast-map #13). Values match RAS Mapper on every common pixel, but on Bald
  Eagle bridges p05 about 2,000 of 973,000 pixels differ in the drawn mask,
  so it is off by default.
* **Inundation boundary, Max profile** (``RasFim.map_inundation_boundaries``,
  ras-commander PR #709 / fast-map PR #5). RAS Mapper's boundary export is
  very slow; on Davis the Rust batch took 3.5 s against 85 s for two native
  calls. It is a different, deterministic method
  (``deterministic-zero-depth-contours/1``), not vector parity: polygons
  differ slightly from RAS Mapper's, whose own polygons vary between
  identical exports. Horizontal and plain sloping only, and it needs the
  original **single-source, unmodified** terrain TIFF plus its terrain HDF.
  ``fast_map_boundary=False`` keeps boundaries on RAS Mapper.
* **Terrain** (:class:`TerrainResolver`, :func:`with_terrain`). A supplied
  ``fast_map_terrain`` (a float32 export of the registered terrain, with
  modifications) is used as is. Otherwise, when ras-commander has
  ``RasFim.top_priority_terrain_source`` (fast-map #11), the products are
  mapped on the terrain's unmodified **priority-1 source TIFF** with its
  terrain-HDF footprint mask, skipping the RAS Mapper terrain export (about
  190-240 s on Bald Eagle). The engine refuses when a wet cell would draw
  outside that footprint; a CRS refusal retries on the source's
  CRS-canonical copy, and any other refusal falls back to the export, which
  needs HEC-RAS (Windows or Wine). The export is made once per run.
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
# Whole-simulation products qualified against RAS Mapper (fast-map #12).
# Duration stays on RAS Mapper: the engine marks it unqualified.
FAST_MAP_WHOLE_SIMULATION_TYPES = ("arrival_time", "percent_inundated")
FAST_MAP_WHOLE_SIMULATION_RENDER_MODES = ("horizontal", "sloping")
# Velocity (fast-map #13) is opt-in: plain sloping only, and on Bald Eagle
# bridges p05 Max its mask differs from RAS Mapper's on ~2,000 of 973,000
# pixels although every common value is identical.
FAST_MAP_VELOCITY = "velocity"
FAST_MAP_WHOLE_SIMULATION_MANIFEST = "fast_map_whole_simulation_manifest.json"
# How fast-map got its terrain, recorded per product in MapResult.fast_map_terrain.
TERRAIN_SUPPLIED = "supplied"
TERRAIN_SOURCE = "source"
TERRAIN_SOURCE_CANONICAL = "source-canonical"
TERRAIN_EXPORT = "export"
FAST_MAP_RENDER_MODES = ("horizontal", "sloping", "slopingPretty")
# Deterministic contours support horizontal and plain cell-corner sloping only.
FAST_MAP_BOUNDARY_RENDER_MODES = ("horizontal", "sloping")
FAST_MAP_MANIFEST = "fast_map_manifest.json"
FAST_MAP_BOUNDARY_MANIFEST = "fast_map_boundary_manifest.json"

_COMPLETE_STATUSES = {"complete", "no_flooded_pixels"}


class FastMapUnavailable(RuntimeError):
    """The plan, profile, terrain or installation is outside fast-map's scope."""


#: The engine ``ras2cng map`` uses when neither ``--engine`` nor
#: ``RAS2CNG_MAP_ENGINE`` says otherwise.  fast-map maps what it is qualified
#: for on pure 2D models and hands everything else to RAS Mapper per product
#: group; ``rasmapper`` forces the native path for every product.
DEFAULT_MAP_ENGINE = FAST_MAP


def resolve_map_engine(engine: Optional[str] = None) -> str:
    """Return the selected engine; ``None`` reads ``RAS2CNG_MAP_ENGINE``.

    The default is :data:`DEFAULT_MAP_ENGINE` (fast-map).
    """
    value = engine if engine is not None else os.environ.get(MAP_ENGINE_ENV, DEFAULT_MAP_ENGINE)
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

    The mode comes from the project's ``.rasmap``, as RAS Mapper takes it; an
    explicit ``render_mode`` overrides it. When neither gives a mode, fast-map
    does not guess: :class:`FastMapUnavailable` sends the plan to RAS Mapper,
    which applies its own default. The slopingPretty flags always come from
    the ``.rasmap``, as they do for RAS Mapper, which keeps them when
    ``RasProcess`` sets only the mode.
    """
    stored: Optional[dict] = None
    try:
        from ras_commander import RasMap

        stored = RasMap.get_water_surface_render_mode(ras_object=ras)
    except Exception:
        stored = None
    mode = render_mode or (stored or {}).get("mode")
    if not mode:
        raise FastMapUnavailable(
            "the project's .rasmap sets no water-surface render mode; RAS Mapper "
            "renders with its own default"
        )
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


def _has_kwargs(function: Any, *names: str) -> bool:
    import inspect

    try:
        parameters = inspect.signature(function).parameters
    except (TypeError, ValueError):
        return False
    return all(name in parameters for name in names)


def source_terrain_supported() -> bool:
    """True when RasFim maps on the priority-1 source TIFF (ras-commander #724).

    That skips the RAS Mapper terrain export (about 190-240 s on Bald Eagle)
    wherever every mapped pixel of a wet cell lies inside that source's
    footprint (fast-map #11); otherwise the engine refuses and ras2cng exports.
    """
    try:
        from ras_commander import RasFim
    except ImportError:
        return False
    return callable(getattr(RasFim, "top_priority_terrain_source", None)) and _has_kwargs(
        RasFim.map_sloping_profiles, "terrain_hdf"
    ) and _has_kwargs(RasFim.map_horizontal_profiles, "terrain_hdf")


def whole_simulation_supported() -> bool:
    """True when RasFim maps arrival time / percent inundated (fast-map #12)."""
    try:
        from ras_commander import RasFim
    except ImportError:
        return False
    return callable(getattr(RasFim, "map_whole_simulation", None))


def velocity_supported() -> bool:
    """True when RasFim renders velocity in sloping requests (fast-map #13)."""
    try:
        from ras_commander import RasFim
    except ImportError:
        return False
    return _has_kwargs(RasFim.map_sloping_profiles, "velocity")


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


def preflight(plan_hdf: Path, ras: Any, render_mode: Optional[str]) -> dict[str, Any]:
    """Check a plan is fast-map's before any terrain work; return its render.

    Raises :class:`FastMapUnavailable` when RasFim or the engine is missing,
    the model has 1D elements or storage areas, or no render mode is set.
    These are cheap HDF/XML reads, so an out-of-scope plan never triggers a
    terrain export before it goes to RAS Mapper.
    """
    try:
        from ras_commander import RasFim
    except ImportError as exc:
        raise FastMapUnavailable(f"ras-commander has no RasFim: {exc}") from exc
    _check_scope(RasFim, Path(plan_hdf))
    return render_settings(ras, render_mode)


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
        self._sources: dict[tuple[str, str], Any] = {}
        #: Why cheaper terrain candidates were skipped, for the console/manifest.
        self.notes: list[str] = []
        #: The engine's last refusal, which decides the next candidate.
        self.last_refusal: Optional[str] = None

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

    def candidates(self, plan_hdf: Path, *, use_source: bool = True):
        """Yield ``(kind, terrain_tif, terrain_hdf_or_None)`` cheapest first.

        A supplied terrain is the only candidate. Otherwise, when RasFim can
        map on the priority-1 source TIFF: that TIFF, then its CRS-canonical
        copy (the engine refuses some original CRS definitions, e.g. Davis),
        then the RAS Mapper export. The copy is only tried after a CRS refusal
        (``last_refusal``). Each later candidate is only built when
        the caller asks for it, i.e. after the previous one was refused.
        """
        if self.terrain_path is not None:
            yield TERRAIN_SUPPLIED, self.for_plan(plan_hdf), None
            return
        if use_source and source_terrain_supported():
            source = self._source(plan_hdf)
            if source is not None:
                tif, hdf = source
                yield TERRAIN_SOURCE, tif, hdf
                # Only a CRS refusal is cured by the canonical copy; any other
                # (e.g. a wet cell drawing outside the source) goes to export.
                if "crs" in (self.last_refusal or "").lower():
                    canonical = self._canonical(tif)
                    if canonical is not None:
                        yield TERRAIN_SOURCE_CANONICAL, canonical, hdf
        yield TERRAIN_EXPORT, self.for_plan(plan_hdf), None

    def _terrain_hdf_for(self, plan_hdf: Path) -> Optional[Path]:
        from ras2cng.terrain import discover_terrains

        hdf = self.terrain_hdf
        if hdf is None and self.terrain_name:
            for info in discover_terrains(self.prj_file.parent):
                if info.name == self.terrain_name and info.hdf_path is not None:
                    hdf = Path(info.hdf_path)
                    break
        if hdf is None:
            hdf = plan_terrain_hdf(plan_hdf, self.prj_file.parent)
        return Path(hdf) if hdf is not None and Path(hdf).is_file() else None

    def _source(self, plan_hdf: Path) -> Optional[tuple[Path, Path]]:
        """The unmodified priority-1 source TIFF and its terrain HDF, or None."""
        hdf = self._terrain_hdf_for(plan_hdf)
        if hdf is None:
            return None
        key = ("source", str(hdf))
        if key not in self._sources:
            from ras_commander import RasFim
            from ras_commander.fim.errors import FimUnsupportedError

            rasmap = self.prj_file.with_suffix(".rasmap")
            try:
                info = RasFim.top_priority_terrain_source(
                    hdf, rasmap_path=rasmap if rasmap.is_file() else None
                )
                usable = not info.get("modified") and Path(info["tif"]).is_file()
                self._sources[key] = (Path(info["tif"]), hdf) if usable else None
            except (FimUnsupportedError, OSError, KeyError) as exc:
                self.notes.append(f"priority-1 terrain source unusable: {exc}")
                self._sources[key] = None
        return self._sources[key]

    def _canonical(self, tif: Path) -> Optional[Path]:
        key = ("canonical", str(tif))
        if key not in self._sources:
            from ras_commander import RasFim

            target = self.work_dir / f"{_slug(tif.stem)}.canonical.tif"
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                self._sources[key] = Path(RasFim.canonicalize_terrain_crs(tif, target))
            except Exception as exc:
                self.notes.append(f"CRS-canonical copy of {tif.name} failed: {exc}")
                self._sources[key] = None
        return self._sources[key]

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


def with_terrain(
    resolver: "TerrainResolver",
    plan_hdf: Path,
    run,
    *,
    use_source: bool = True,
) -> tuple[Any, str]:
    """Call ``run(terrain_tif, terrain_hdf)`` on each terrain candidate in turn.

    Returns ``(run's result, terrain kind)``. A candidate the engine refuses
    (:class:`FastMapUnavailable`) moves on to the next one, ending with the
    RAS Mapper export; the reasons are kept in ``resolver.notes``. Other
    errors propagate.
    """
    resolver.last_refusal = None
    candidates = resolver.candidates(plan_hdf, use_source=use_source)
    last: Optional[FastMapUnavailable] = None
    while True:
        try:
            kind, tif, terrain_hdf = next(candidates)
        except StopIteration:
            break
        except FastMapUnavailable as exc:  # the export itself failed
            last = exc
            break
        try:
            return run(tif, terrain_hdf, kind), kind
        except FastMapUnavailable as exc:
            resolver.notes.append(f"{kind} terrain refused: {exc}")
            resolver.last_refusal = str(exc)
            last = exc
    raise last or FastMapUnavailable("no terrain available for fast-map")


def plan_units(plan_hdf: Path) -> str:
    """``ft`` or ``m``, as RAS Mapper labels whole-simulation products."""
    import h5py

    try:
        with h5py.File(plan_hdf, "r") as hdf:
            value = hdf.attrs.get("Units System", b"US Customary")
    except OSError:
        value = b"US Customary"
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    return "m" if "SI" in str(value) else "ft"


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
    terrain_hdf: Optional[Path] = None,
    velocity: bool = False,
    terrain_kind: Optional[str] = None,
) -> dict[str, list[Path]]:
    """Map one plan's Max WSE/Depth with the Rust engine into ``output_dir``.

    Products are named like RAS Mapper's (``WSE (Max).tif``,
    ``Depth (Max).tif``) so downstream discovery is unchanged. The engine's
    product manifest is kept as ``fast_map_manifest.json``. Raises
    :class:`FastMapUnavailable` for anything outside fast-map's scope, and lets
    engine failures propagate.
    """
    requested = [name for name in map_types if name in FAST_MAP_RASTER_TYPES]
    # Velocity is opt-in and plain sloping only; anything else leaves it to
    # RAS Mapper without affecting WSE/Depth.
    want_velocity = bool(
        velocity and FAST_MAP_VELOCITY in map_types and render["mode"] == "sloping"
        and velocity_supported()
    )
    if want_velocity:
        requested.append(FAST_MAP_VELOCITY)
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
                scope = {"terrain_hdf": terrain_hdf} if terrain_hdf is not None else {}
                manifest = RasFim.map_horizontal_profiles(
                    cells, terrain_tif, work, profiles=["wsel_max"],
                    hdf_path=plan_hdf, **scope, **common,
                )
            else:
                pretty = render["mode"] == "slopingPretty"
                manifest = RasFim.map_sloping_profiles(
                    plan_hdf, terrain_tif, work,
                    profiles={"Max": "Max"},
                    render_mode=render["mode"],
                    reduce_shallow_to_horizontal=pretty and render["reduce_shallow_to_horizontal"],
                    use_depth_weighted_faces=pretty and render["use_depth_weighted_faces"],
                    **({"terrain_hdf": terrain_hdf} if terrain_hdf is not None else {}),
                    **({"velocity": True} if want_velocity else {}),
                    **common,
                )
        except FimUnsupportedError as exc:
            raise FastMapUnavailable(str(exc)) from exc

        row = manifest.iloc[0]
        if row["status"] not in _COMPLETE_STATUSES:
            raise RuntimeError(f"fast-map {row['status']}: {row.get('error')}")

        products = {
            "wse": row.get("wse_tif"),
            "depth": row.get("depth_tif"),
            FAST_MAP_VELOCITY: row.get("velocity_tif"),
        }
        labels = {"wse": "WSE", "depth": "Depth", FAST_MAP_VELOCITY: "Velocity"}
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
                "terrain_hdf": str(terrain_hdf) if terrain_hdf is not None else None,
                "terrain_kind": terrain_kind,
                "render": render,
                "products": {k: [str(p) for p in v] for k, v in result.items()},
            }
            (output_dir / FAST_MAP_MANIFEST).write_text(
                json.dumps(details, indent=2, default=str), encoding="utf-8"
            )
        return result
    finally:
        shutil.rmtree(work, ignore_errors=True)


def map_whole_simulation(
    plan_hdf: Path,
    terrain_tif: Path,
    output_dir: Path,
    *,
    map_types: list[str],
    threshold: float,
    render: dict[str, Any],
    terrain_hdf: Optional[Path] = None,
    threads: Optional[int] = None,
    writer_threads: int = 2,
    timeout: Optional[float] = None,
    max_working_mib: Optional[int] = None,
    terrain_kind: Optional[str] = None,
) -> dict[str, list[Path]]:
    """Map arrival time and percent time inundated with the Rust engine.

    Qualified against RAS Mapper 7.0.1 stored maps (fast-map #12, ras-commander
    #724): identical values and masks on Davis p02 and Bald Eagle bridges p05
    at a 0.5 ft threshold, in horizontal and plain sloping (RAS Mapper draws
    the same products in both). Wet means depth > ``threshold`` (model
    vertical units; ras2cng's ``arrival_depth``); arrival time is in hours
    since the simulation start. Products are named as RAS Mapper names them
    (``Arrival Time (0.5ft hrs).tif``, ``Percent Time Inundated (0.5ft).tif``).
    Duration is not produced here: the engine marks it unqualified.
    """
    requested = [t for t in map_types if t in FAST_MAP_WHOLE_SIMULATION_TYPES]
    if not requested:
        return {}
    if render["mode"] not in FAST_MAP_WHOLE_SIMULATION_RENDER_MODES:
        raise FastMapUnavailable(
            "whole-simulation products are qualified for horizontal and plain "
            f"sloping, not {render['mode']!r}"
        )
    try:
        from ras_commander import RasFim
        from ras_commander.fim.errors import FimUnsupportedError
    except ImportError as exc:
        raise FastMapUnavailable(f"ras-commander has no RasFim: {exc}") from exc
    if not whole_simulation_supported():
        raise FastMapUnavailable(
            "the installed ras-commander has no RasFim.map_whole_simulation "
            "(ras-commander #724 with ras-commander-fast-map #12)"
        )

    plan_hdf = Path(plan_hdf)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    _check_scope(RasFim, plan_hdf)
    work = Path(tempfile.mkdtemp(prefix=".fast-map-adr-", dir=output_dir))
    try:
        try:
            manifest = RasFim.map_whole_simulation(
                plan_hdf, terrain_tif, work,
                products=tuple(requested),
                threshold=float(threshold),
                terrain_hdf=terrain_hdf,
                threads=default_threads() if threads is None else int(threads),
                writer_threads=writer_threads,
                max_working_mib=(
                    default_working_mib() if max_working_mib is None else int(max_working_mib)
                ),
                timeout=timeout,
            )
        except FimUnsupportedError as exc:
            raise FastMapUnavailable(str(exc)) from exc

        unit = plan_units(plan_hdf)
        label = f"{float(threshold):g}{unit}"
        names = {
            "arrival_time": f"Arrival Time ({label} hrs).tif",
            "percent_inundated": f"Percent Time Inundated ({label}).tif",
        }
        rows = {str(r["product"]): r for _, r in manifest.iterrows()}
        result: dict[str, list[Path]] = {}
        for name in requested:
            row = rows.get(name)
            if row is None or row["status"] not in _COMPLETE_STATUSES:
                status = "missing" if row is None else f"{row['status']}: {row.get('error')}"
                raise RuntimeError(f"fast-map whole-simulation {name} {status}")
            source = row.get("tif")
            if not source or not Path(source).is_file():
                raise RuntimeError(f"fast-map reported no {name} product")
            target = output_dir / names[name]
            os.replace(source, target)
            result[name] = [target]

        def _plain(value):
            return value.item() if hasattr(value, "item") else value

        details = {
            "method": "fast-map whole-simulation, qualified against RAS Mapper stored maps",
            "plan_hdf": str(plan_hdf),
            "terrain": str(terrain_tif),
            "terrain_hdf": str(terrain_hdf) if terrain_hdf is not None else None,
            "terrain_kind": terrain_kind,
            "render": render,
            "threshold": float(threshold),
            "threshold_units": unit,
            "products": {
                name: {
                    "path": str(result[name][0]),
                    **{
                        key: _plain(rows[name].get(key))
                        for key in ("units", "output_times", "wet_pixels", "qualified")
                    },
                }
                for name in requested
            },
        }
        (output_dir / FAST_MAP_WHOLE_SIMULATION_MANIFEST).write_text(
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
