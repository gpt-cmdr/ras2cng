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
  river reaches or cross sections. Pipe networks are permitted.
* **Storage areas: opt-in** (fast-map 0.12 #18, ras-commander 0.113 #757;
  engine capability ``storage-area-level-pool/1``,
  :func:`storage_areas_supported`). Standalone storage areas are drawn as RAS
  Mapper level pools in WSE/Depth (every render mode) and arrival time /
  percent inundated: 0 mask and 0 float32 bit differences against native
  HEC-RAS 7.0.1 on Bald Eagle p07 and p04. **Not a default** (user decision
  2026-10-31) until it is faster than RAS Mapper: on p07, WSE + Depth took
  18.7 s against RAS Mapper's 12.2 s. By default a plan with storage areas
  goes wholly to RAS Mapper; ``fast_map_storage_areas=True``
  (``--fast-map-storage-areas``, ``RAS2CNG_FAST_MAP_STORAGE_AREAS=1``) maps
  it in Rust, which is how to map such plans without HEC-RAS (Linux, FIM
  batches). Velocity, the derived products, duration and the boundary are
  not qualified with storage areas, so even then they stay on RAS Mapper.
  Without the capability the whole plan goes to RAS Mapper.
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
  (``Arrival Time (0.5ft hrs).tif``). **Duration** joins them when the engine
  advertises ``duration_rule`` ``rasmapper-backward-window-recession/1``
  (fast-map #20): 0 differences on Davis p02 (4,879 px) and bridges p05
  (959,043 px); older engines keep it on RAS Mapper.
  ``fast_map_whole_simulation=False`` keeps all three on RAS Mapper.
* **Velocity, Max** (fast-map #13, #20; ras-commander #758). With an engine
  advertising ``velocity_depth`` (:func:`velocity_render_modes`) velocity is
  mapped by default in plain sloping and in horizontal (clipped with the
  horizontal depth, as RAS Mapper does): 0 value and 0 mask differences on
  bridges p05 Max in both (973,902 / 973,532 px). The earlier ~2,000 px mask
  difference was a reference artifact. **slopingPretty** velocity is a
  default too when the engine advertises ``velocity_with_faces``
  (:data:`WITH_FACES_METHOD`, fast-map #30) and ras-commander passes the
  options through (#819): the project's ``.rasmap`` flags (shallow reduction,
  depth-weighted faces) are passed unchanged, because RAS Mapper clips
  velocity with the depth rendered with the same options. 0 mask and 0 bit
  differences on bridges p05 and Davis p02 in all four option combinations.
  The earlier bridges slopingPretty difference (75 + 28 px) compared a plain
  run with a reference made with shallow reduction on. Without that record
  slopingPretty velocity stays opt-in (``fast_map_velocity=True``) and plain
  only. On older engines velocity is opt-in and plain sloping only.
* **D x V, D x V², Froude, Max** (fast-map #20 ``reduce_max/1``,
  ``RasFim.map_derived_max``). RAS Mapper's Max of these is the per-time
  product's pixelwise maximum over every output time, not Max d x Max v; the
  engine reduces all times in one pass. 0 differences on bridges p05 over 433
  times (sloping and horizontal); Davis within 2 ulp on <= 5 px. Plain
  sloping and horizontal by default (``fast_map_derived``), and slopingPretty
  under the same ``velocity_with_faces`` record as velocity (0 / 0 on bridges
  p05 and Davis p02, four option combinations); otherwise slopingPretty stays
  on RAS Mapper.
* **Shear stress, Max** (fast-map #31 ``rasmapper-face-shear-max/1``,
  ras-commander #820 ``RasFim.supports_shear_stress``; :func:`shear_plan`,
  :func:`map_shear`). Its own engine call, like velocity, in plain sloping
  and horizontal (clipped by the horizontal depth): 0 mask and 0 bit
  differences against RAS Mapper 7.0.1 on bridges p05 (973,902 / 973,532 px)
  and Davis p02 (22,041 / 20,560 px). slopingPretty and storage-area plans
  stay on RAS Mapper, as does every engine without the method.
* **Inundation boundary, Max profile** (``RasFim.map_inundation_boundaries``,
  ras-commander #755, superseding #709; fast-map #5). Not with storage areas. RAS Mapper's boundary export is
  very slow; on Davis the Rust batch took 3.5 s against 85 s for two native
  calls. The default is a different, deterministic method
  (``deterministic-zero-depth-contours/1``), not vector parity: polygons
  differ slightly from RAS Mapper's, whose own polygons vary between
  identical exports. Horizontal and plain sloping only, and it needs the
  original **single-source, unmodified** terrain TIFF plus its terrain HDF.
  ``fast_map_boundary_method="native-serial"`` (opt-in, fast-map 0.15 /
  ras-commander 0.116; :data:`NATIVE_BOUNDARY_METHOD`) selects the engine's
  native-serial contract instead: plain sloping, original terrain sources
  (more than one is accepted), and storage areas when they are opted in. The
  engine and ras-commander still label it unqualified; ras-commander's
  mapping guide reports exact geometry and unrounded coordinates against RAS
  Mapper's **serial** boundary export on named fixtures only (Windows x64).
  See :func:`map_boundary`.
  ``fast_map_boundary=False`` keeps boundaries on RAS Mapper. When the engine
  cannot name the terrain's EPSG code and reads its WKT differently from
  rasterio (Davis), it refuses unless the caller pins the engine's WKT hash:
  ``fast_map_terrain_crs_fnv1a64`` (``--fast-map-terrain-crs-hash``). ras2cng
  never guesses it; the refusal message prints the hash the engine reports
  (``ras2fim-batch terrain-info``) so the user can check and supply it.
* **Terrain** (:class:`TerrainResolver`, :func:`with_terrain`). A supplied
  ``fast_map_terrain`` (a float32 export of the registered terrain, with
  modifications) is used as is. Otherwise, when ras-commander has
  ``RasFim.top_priority_terrain_source`` (fast-map #11), the products are
  mapped on the terrain's unmodified **priority-1 source TIFF** with its
  terrain-HDF footprint mask, skipping the RAS Mapper terrain export (about
  190-240 s on Bald Eagle). The engine refuses when a wet cell would draw
  outside that footprint; a CRS refusal retries on the source's
  CRS-canonical copy. Any other refusal next tries the **native effective
  terrain** (fast-map #19, ``RasFim.build_effective_terrain``,
  :func:`effective_terrain_supported`): RAS Mapper's multi-source mosaic
  rebuilt in Rust from the source TIFFs, masks and stitch TIN, without
  HEC-RAS (Bald Eagle Terrain50: 0 mask differences, 8 of 197 M values differ
  in stitch-TIN corners where RAS Mapper's own exports are unstable; ~7 s
  against ~190 s). Any other refusal falls back to the RAS Mapper export,
  which needs HEC-RAS (Windows or Wine). Each is built at most once per run.
  **Modified terrains** (:meth:`TerrainResolver.modified_route`): when
  :func:`ras2cng.terrain.terrain_modifications` finds any modification in
  the terrain HDF or the ``.rasmap``, the unmodified source TIFF is skipped
  and Rust boundaries are refused (RAS Mapper draws them). The terrain is
  built by the native effective terrain *with modifications* only when the
  engine advertises ``rasmapper-effective-terrain/2`` (Fast-Map 0.18+) with a
  ``modifications`` record, ras-commander's modification reader
  (``RasFim.read_terrain_modifications``, 0.119+, else the private
  ``_terrain_mods.read_layers``; recorded as ``modification_reader``)
  stages every layer and every staged form is in the engine's qualified
  list; otherwise RAS Mapper's export builds it, as before, and the reason
  is recorded in ``MapResult.fast_map_terrain_route`` and
  :data:`TERRAIN_ROUTE_MANIFEST`. With Fast-Map 0.17.0 (``/1`` only) every
  modified terrain still goes to the export.
* **Invalid cell polygons** (ras-commander 0.110.2+). ``invalid_cells``
  (:data:`INVALID_CELL_POLICIES`) sets pixel ownership for cells whose face
  polylines cross; the engine default ``repair`` maps such meshes instead of
  refusing them. The engine's ``fim-invalid-cells/1`` record is returned on
  :class:`FastMapProducts` and kept in every manifest written here.
* **Native GeoTIFF delivery** (fast-map 0.11, ras-commander 0.110.3).
  :data:`PRODUCT_WRITERS`: sloping WSE/Depth written by the engine (terrio
  LZW) instead of rasterio; pixel-identical, same layout. Velocity requests
  keep rasterio.
* **Not used: retained mapping state** (``RasFim.prepare_sloping_model``,
  ``map_catalog``). ras2cng maps one Max request per plan, and prepared
  models refuse ``terrain_hdf`` and velocity, so there is nothing to retain.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

RASMAPPER = "rasmapper"
FAST_MAP = "fast-map"
MAP_ENGINES = (RASMAPPER, FAST_MAP)
MAP_ENGINE_ENV = "RAS2CNG_MAP_ENGINE"

# Map types the Rust engine produces. Everything else stays on RAS Mapper.
FAST_MAP_RASTER_TYPES = ("wse", "depth")
FAST_MAP_BOUNDARY = "inundation_boundary"
FAST_MAP_TYPES = FAST_MAP_RASTER_TYPES + (FAST_MAP_BOUNDARY,)
# Whole-simulation products qualified against RAS Mapper (fast-map #12).
# Duration joins them only on an engine with the qualified recession rule
# (fast-map #20, DURATION_RULE); older engines mark it unqualified.
FAST_MAP_WHOLE_SIMULATION_TYPES = ("arrival_time", "percent_inundated")
FAST_MAP_DURATION = "duration"
DURATION_RULE = "rasmapper-backward-window-recession/1"
FAST_MAP_WHOLE_SIMULATION_RENDER_MODES = ("horizontal", "sloping")
# Velocity (fast-map #13, #20): see velocity_render_modes().
FAST_MAP_VELOCITY = "velocity"
# Render modes where velocity is mapped without being asked for: qualified
# 0/0 against RAS Mapper on bridges p05 Max. slopingPretty is opt-in.
FAST_MAP_VELOCITY_DEFAULT_MODES = ("horizontal", "sloping")
# Max D x V, D x V^2 and Froude (fast-map #20 reduce_max/1): ras2cng map type
# -> engine product name, and the RAS Mapper file label.
FAST_MAP_DERIVED_TYPES = {
    "depth_x_velocity": "depth_x_velocity",
    "depth_x_velocity_sq": "depth_x_velocity_squared",
    "froude": "froude",
}
FAST_MAP_DERIVED_LABELS = {
    "depth_x_velocity": "D _ V",
    "depth_x_velocity_sq": "D _ V^2",
    "froude": "Froude",
}
FAST_MAP_DERIVED_RENDER_MODES = ("horizontal", "sloping")
DERIVED_MAX_METHOD = "reduce_max/1"
# slopingPretty velocity and derived Max (fast-map #30, ras-commander #819):
# the engine's sloping record names this method in ``velocity_with_faces`` and
# lists the slopingPretty options it is qualified with in
# ``velocity_with_faces_options``. See with_faces_options().
WITH_FACES_METHOD = "rasmapper-with-faces-velocity-derived/1"
SLOPING_PRETTY_OPTIONS = ("reduce_shallow_to_horizontal", "use_depth_weighted_faces")
# Max shear stress (fast-map #31, ras-commander #820): see shear_plan().
FAST_MAP_SHEAR = "shear_stress"
SHEAR_STRESS_METHOD = "rasmapper-face-shear-max/1"
FAST_MAP_SHEAR_RENDER_MODES = ("horizontal", "sloping")
# Output times rendered concurrently in the Max derived pass. On bridges p05
# (433 times, 7 threads) 1 took 213 s / 1,448 MiB and 3 took 147 s / 2,592 MiB
# (RAS Mapper: 206 s / 2,316 MiB), with identical output. The engine still
# admits each in-flight time against max_working_mib.
DERIVED_MAX_INFLIGHT = 3
STORAGE_AREA_METHOD = "storage-area-level-pool/1"
# Storage-area level pools are opt-in (user decision 2026-10-31): p07 WSE +
# Depth took 18.7 s in Rust against 12.2 s in RAS Mapper. See
# resolve_storage_areas().
STORAGE_AREAS_ENV = "RAS2CNG_FAST_MAP_STORAGE_AREAS"
STORAGE_AREAS_DEFAULT = False
FAST_MAP_WHOLE_SIMULATION_MANIFEST = "fast_map_whole_simulation_manifest.json"
# How fast-map got its terrain, recorded per product in MapResult.fast_map_terrain.
TERRAIN_SUPPLIED = "supplied"
TERRAIN_SOURCE = "source"
TERRAIN_SOURCE_CANONICAL = "source-canonical"
TERRAIN_EFFECTIVE = "effective"
TERRAIN_EXPORT = "export"
# The export's CRS-canonical copy (same pixels, EPSG GeoTIFF keys), tried only
# after the engine refuses the export's CRS identity (Davis, where the source
# terrain is refused outside plain sloping).
TERRAIN_EXPORT_CANONICAL = "export-canonical"
# Modified terrains (fast-map 0.18 / ras-commander 0.119): the native effective
# terrain with modifications needs this method and request schema; see
# TerrainResolver.modified_route(). Fast-Map 0.17.0 advertises only /1.
EFFECTIVE_TERRAIN_V2 = "rasmapper-effective-terrain/2"
EFFECTIVE_TERRAIN_SCHEMA_V2 = "ras-commander-effective-terrain-request/2"
# Routing decision for a modified terrain, kept in MapResult.fast_map_terrain_route
# and in TERRAIN_ROUTE_MANIFEST beside the terrain work files.
MODIFIED_ROUTE_NATIVE = "native-effective"
MODIFIED_ROUTE_RASMAPPER = "rasmapper-export"
TERRAIN_ROUTE_MANIFEST = "fast_map_terrain_route.json"
# Commander's modification reader recorded on the route (``modification_reader``):
# the public API (ras-commander 0.119+), else the private module it wraps.
MODIFICATION_READER_PUBLIC = "RasFim.read_terrain_modifications"
MODIFICATION_READER_PRIVATE = "ras_commander.fim._terrain_mods.read_layers"
# Reader refusal code for a polygon with more than one ring (holes or separate
# parts; ras-commander #837, Fast-Map #42): always the RAS Mapper export.
MULTI_RING_POLYGON = "multi_ring_polygon"
# HEC-RAS releases whose RAS Mapper terrain exports the engine's modification
# rules reproduce (Fast-Map 0.18.0 is qualified against 6.6 and 7.0.1 exports).
# The native route and the export use one resolved release (resolve_hecras_runtime);
# a modified terrain whose export runs on any other release stays on the export.
QUALIFIED_MODIFICATION_RUNTIMES = ("6.6", "7.0.1")
# A terrain whose HDF stores modification layers that RAS Mapper does not apply
# (no .rasmap group named exactly "Modifications"; ras-commander 0.119) is mapped
# as unmodified; its route record says so (route MODIFIED_ROUTE_UNAPPLIED).
MODIFIED_ROUTE_UNAPPLIED = "unmodified"
RASMAP_UNAPPLIED_CODES = ("modification_group_misnamed", "modification_group_missing")
FAST_MAP_RENDER_MODES = ("horizontal", "sloping", "slopingPretty")
# Deterministic contours support horizontal and plain cell-corner sloping only.
FAST_MAP_BOUNDARY_RENDER_MODES = ("horizontal", "sloping")
FAST_MAP_MANIFEST = "fast_map_manifest.json"
FAST_MAP_VELOCITY_MANIFEST = "fast_map_velocity_manifest.json"
FAST_MAP_DERIVED_MANIFEST = "fast_map_derived_manifest.json"
FAST_MAP_BOUNDARY_MANIFEST = "fast_map_boundary_manifest.json"
FAST_MAP_SHEAR_MANIFEST = "fast_map_shear_manifest.json"
# Boundary methods. ``deterministic`` is the default contour method;
# ``native-serial`` is the engine's opt-in native-serial contract (fast-map
# 0.15, ras-commander 0.116), which the engine itself labels unqualified.
BOUNDARY_DETERMINISTIC = "deterministic"
BOUNDARY_NATIVE_SERIAL = "native-serial"
BOUNDARY_METHODS = (BOUNDARY_DETERMINISTIC, BOUNDARY_NATIVE_SERIAL)
BOUNDARY_METHOD_ENV = "RAS2CNG_FAST_MAP_BOUNDARY_METHOD"
DETERMINISTIC_BOUNDARY_METHOD = "deterministic-zero-depth-contours/1"
NATIVE_BOUNDARY_METHOD = "rasmapper-serial-zero-depth-contours/1"
# The native-serial checks in ras-commander's mapping guide use plain sloping.
FAST_MAP_NATIVE_BOUNDARY_RENDER_MODES = ("sloping",)

_COMPLETE_STATUSES = {"complete", "no_flooded_pixels"}

# Pixel ownership for physical cells whose face polylines do not form a valid
# polygon (ras-commander 0.110.2+, ``invalid_cells``). ``None`` leaves the
# engine default ("repair"). The record is ``fim-invalid-cells/1``.
INVALID_CELL_POLICIES = ("repair", "hull", "omit", "raise")
INVALID_CELLS_SCHEMA = "fim-invalid-cells/1"

# How fast-map delivers sloping WSE/Depth GeoTIFFs. ``native`` has the Rust
# engine write the LZW TIFFs through terrio (fast-map 0.11
# ``native_geotiff_outputs``, ras-commander 0.110.3 ``product_writer``)
# instead of piping float32 arrays back to Python for rasterio. On Bald Eagle
# bridges p05 both are pixel-identical with the same strip/LZW layout; native
# files are ~4% smaller and peak memory ~18% lower. ``auto`` uses native when
# the engine advertises it and the request allows it (no velocity).
PRODUCT_WRITERS = ("auto", "native", "rasterio")
PRODUCT_WRITER_ENV = "RAS2CNG_FAST_MAP_WRITER"


class FastMapProducts(dict):
    """``{map_type: [paths]}`` plus the engine's ``invalid_cells`` record.

    ``invalid_cells`` is the ``fim-invalid-cells/1`` record ras-commander
    attaches to the product manifest (``None`` on ras-commander < 0.110.2).
    ``product_writer`` is the delivery path actually used.
    """

    invalid_cells: Optional[dict] = None
    product_writer: Optional[str] = None


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


_CRS_FNV1A64 = re.compile(r"^[0-9a-f]{16}$")


def normalize_crs_fnv1a64(value: Optional[str]) -> Optional[str]:
    """Validate a user-supplied engine WKT hash (16 hex digits, as reported
    by ``ras2fim-batch terrain-info`` in ``crs_wkt_fnv1a64``)."""
    if value is None:
        return None
    text = str(value).strip().lower()
    if text.startswith("0x"):
        text = text[2:]
    if not _CRS_FNV1A64.match(text):
        raise ValueError(
            "terrain CRS hash must be the 16 hex digits the engine reports as "
            f"crs_wkt_fnv1a64 (ras2fim-batch terrain-info <terrain.tif>); got {value!r}"
        )
    return text


def terrain_crs_hint(terrain_tif: Path) -> Optional[str]:
    """How to pin a terrain CRS the engine refused, with its reported hash.

    Runs ``ras2fim-batch terrain-info`` and returns a message naming the
    engine's ``crs_wkt_fnv1a64`` and the start of its WKT, so the user can
    verify the CRS and pass ``--fast-map-terrain-crs-hash``. The value is only
    shown, never used. ``None`` when the engine cannot be asked.
    """
    try:
        from ras_commander import RasFim
        from ras_commander.fim import _engine

        engine = RasFim.find_engine()
        info = _engine._json_stdout(
            _engine.run_engine([str(engine), "terrain-info", str(terrain_tif)], "terrain-info"),
            "ras2fim-batch terrain-info",
        )
    except Exception:
        return None
    fnv = info.get("crs_wkt_fnv1a64") if isinstance(info, dict) else None
    if not fnv:
        return None
    crs = info.get("crs")
    wkt = crs.get("wkt") if isinstance(crs, dict) else None
    shown = f" (engine WKT: {wkt[:160]}...)" if isinstance(wkt, str) and wkt else ""
    return (
        f"the engine reports crs_wkt_fnv1a64={fnv} for {Path(terrain_tif).name}{shown}. "
        "If that WKT is the terrain's CRS, rerun with "
        f"--fast-map-terrain-crs-hash {fnv} (Python: fast_map_terrain_crs_fnv1a64); "
        f'check with: ras2fim-batch terrain-info "{terrain_tif}"'
    )


def _crs_refusal(message: str) -> bool:
    """True for the engine/rasterio terrain CRS identity refusals."""
    return "terrain_crs_fnv1a64" in message or "engine terrain CRS differs" in message


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


def engine_method(kind: str) -> dict:
    """The installed engine's ``ras2fim-batch method`` record for ``kind``.

    ``{}`` when RasFim, the engine or the record is missing; never raises.
    """
    try:
        from ras_commander import RasFim
        from ras_commander.fim import _engine
    except ImportError:
        return {}
    try:
        record = _engine.engine_method(RasFim.find_engine()).get(kind)
    except Exception:
        return {}
    return record if isinstance(record, dict) else {}


def velocity_render_modes() -> tuple[str, ...]:
    """Render modes in which the installed RasFim + engine map velocity.

    Plain ``sloping`` whenever RasFim takes ``velocity`` (fast-map #13).
    ``horizontal`` when the engine's sloping method advertises
    ``velocity_depth`` ``horizontal`` and RasFim takes ``velocity_depth``
    (fast-map #20, ras-commander #758); ``slopingPretty`` when it is also in
    ``velocity_render_modes``.
    """
    if not velocity_supported():
        return ()
    from ras_commander import RasFim

    modes = ["sloping"]
    record = engine_method("sloping")
    if _has_kwargs(RasFim.map_sloping_profiles, "velocity_depth") and "horizontal" in (
        record.get("velocity_depth") or ()
    ):
        modes.append("horizontal")
        if "slopingPretty" in (record.get("velocity_render_modes") or ()):
            modes.append("slopingPretty")
    return tuple(modes)


def velocity_default_on() -> bool:
    """Velocity is mapped unasked only on engines with the #20 velocity rules."""
    return "horizontal" in velocity_render_modes()


def with_faces_options() -> Optional[tuple[str, ...]]:
    """slopingPretty options velocity and derived Max are qualified with.

    ``None`` unless the engine's sloping record advertises
    ``velocity_with_faces`` = :data:`WITH_FACES_METHOD` (fast-map #30) **and**
    the installed ras-commander passes the options through
    (``RasFim.map_derived_max`` takes them, ras-commander #819). Then the
    names in ``velocity_with_faces_options``; ``()`` would mean plain
    slopingPretty only.
    """
    try:
        from ras_commander import RasFim
    except ImportError:
        return None
    if not _has_kwargs(
        getattr(RasFim, "map_derived_max", None), *SLOPING_PRETTY_OPTIONS
    ):
        return None
    record = engine_method("sloping")
    if record.get("velocity_with_faces") != WITH_FACES_METHOD:
        return None
    return tuple(str(name) for name in record.get("velocity_with_faces_options") or ())


def _pretty_options(render: dict[str, Any]) -> tuple[str, ...]:
    """The slopingPretty options the project's ``.rasmap`` turns on."""
    if render.get("mode") != "slopingPretty":
        return ()
    return tuple(name for name in SLOPING_PRETTY_OPTIONS if render.get(name))


def sloping_pretty_products_reason(render: dict[str, Any]) -> Optional[str]:
    """``None`` when slopingPretty velocity / derived Max are qualified here.

    Otherwise why not: the engine has no :data:`WITH_FACES_METHOD` record (or
    ras-commander cannot pass the options), or the project uses an option the
    record does not list.
    """
    qualified = with_faces_options()
    if qualified is None:
        return (
            f"the installed fast-map / ras-commander do not advertise {WITH_FACES_METHOD}"
        )
    missing = [name for name in _pretty_options(render) if name not in qualified]
    if missing:
        return f"the engine does not qualify slopingPretty with {', '.join(missing)}"
    return None


def shear_supported() -> bool:
    """True when RasFim + engine map RAS Mapper's Max shear stress (fast-map #31)."""
    try:
        from ras_commander import RasFim
    except ImportError:
        return False
    check = getattr(RasFim, "supports_shear_stress", None)
    if not callable(check) or not _has_kwargs(
        RasFim.map_sloping_profiles, "shear_stress", "shear_depth"
    ):
        return False
    try:
        return check() is True
    except Exception:
        return False


def resolve_boundary_method(value: Optional[str] = None) -> str:
    """``deterministic`` or ``native-serial``; ``None`` reads the env var."""
    raw = value if value is not None else os.environ.get(BOUNDARY_METHOD_ENV, "")
    text = str(raw).strip().lower().replace("_", "-") or BOUNDARY_DETERMINISTIC
    if text not in BOUNDARY_METHODS:
        raise ValueError(
            f"fast-map boundary method must be one of {', '.join(BOUNDARY_METHODS)}; "
            f"got {raw!r}"
        )
    return text


def native_boundary_supported() -> bool:
    """True when RasFim + engine have the native-serial boundary contract.

    Needs ``method`` and ``allow_unqualified`` on
    ``RasFim.map_inundation_boundaries`` (ras-commander 0.116) and an engine
    whose ``native_boundary`` record is :data:`NATIVE_BOUNDARY_METHOD`
    (fast-map 0.15).
    """
    try:
        from ras_commander import RasFim
    except ImportError:
        return False
    if not _has_kwargs(
        getattr(RasFim, "map_inundation_boundaries", None), "method", "allow_unqualified"
    ):
        return False
    return engine_method("native_boundary").get("method_id") == NATIVE_BOUNDARY_METHOD


def derived_max_supported() -> bool:
    """True when RasFim maps Max D x V / D x V^2 / Froude (fast-map #20)."""
    try:
        from ras_commander import RasFim
    except ImportError:
        return False
    if not callable(getattr(RasFim, "map_derived_max", None)):
        return False
    record = engine_method("sloping")
    return record.get("derived_max") == DERIVED_MAX_METHOD and bool(record.get("derived_products"))


def duration_supported() -> bool:
    """True when the engine's duration follows RAS Mapper's recession rule."""
    return engine_method("whole_simulation").get("duration_rule") == DURATION_RULE


def resolve_storage_areas(value: Optional[bool] = None) -> bool:
    """Whether fast-map may draw storage areas; ``None`` reads the env var.

    ``RAS2CNG_FAST_MAP_STORAGE_AREAS`` accepts 1/true/yes/on and
    0/false/no/off. Unset means :data:`STORAGE_AREAS_DEFAULT` (off).
    """
    if value is not None:
        return bool(value)
    raw = os.environ.get(STORAGE_AREAS_ENV)
    if raw is None or not raw.strip():
        return STORAGE_AREAS_DEFAULT
    text = raw.strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off"):
        return False
    raise ValueError(
        f"{STORAGE_AREAS_ENV} must be 1/true/yes/on or 0/false/no/off; got {raw!r}"
    )


def _render_storage_areas(render: Optional[dict[str, Any]]) -> bool:
    """The storage-area opt-in recorded by :func:`preflight`, else the env var."""
    if isinstance(render, dict) and "storage_areas_enabled" in render:
        return bool(render["storage_areas_enabled"])
    return resolve_storage_areas(None)


def storage_areas_supported() -> bool:
    """True when RasFim + engine draw storage-area level pools (fast-map #18)."""
    try:
        from ras_commander import RasFim
    except ImportError:
        return False
    check = getattr(RasFim, "supports_storage_areas", None)
    if not callable(check) or not _has_kwargs(RasFim.map_sloping_profiles, "storage_areas"):
        return False
    try:
        return check() is True
    except Exception:
        return False


def effective_terrain_supported() -> bool:
    """True when RasFim builds RAS Mapper's effective terrain natively (fast-map #19)."""
    try:
        from ras_commander import RasFim
    except ImportError:
        return False
    check = getattr(RasFim, "supports_effective_terrain", None)
    if not callable(check) or not callable(getattr(RasFim, "build_effective_terrain", None)):
        return False
    try:
        return check() is True
    except Exception:
        return False


def modified_terrain_capability() -> tuple[Optional[dict], Optional[str]]:
    """The engine's terrain-modification record, or ``(None, reason)``.

    A modified terrain may be built natively only by an engine whose
    ``effective_terrain`` record advertises :data:`EFFECTIVE_TERRAIN_V2` (as
    its ``method_id`` or a ``compatible_method_ids`` entry) with a
    ``modifications`` record, and, when it lists ``request_schemas``,
    :data:`EFFECTIVE_TERRAIN_SCHEMA_V2`. Fast-Map 0.17.0 and older advertise
    only ``/1``. Never raises.
    """
    record = engine_method("effective_terrain")
    if not record:
        return None, "the engine advertises no effective_terrain method"
    ids = {record.get("method_id"), *(record.get("compatible_method_ids") or ())}
    ids.discard(None)
    if EFFECTIVE_TERRAIN_V2 not in ids:
        shown = ", ".join(sorted(str(i) for i in ids)) or "no method ID"
        return None, (
            f"the engine advertises {shown}; modified terrains need {EFFECTIVE_TERRAIN_V2}"
        )
    schemas = record.get("request_schemas")
    if schemas is not None and EFFECTIVE_TERRAIN_SCHEMA_V2 not in schemas:
        return None, f"the engine does not accept {EFFECTIVE_TERRAIN_SCHEMA_V2}"
    modifications = record.get("modifications")
    if not isinstance(modifications, dict) or not isinstance(
        modifications.get("supported"), dict
    ):
        return None, (
            f"the engine's {EFFECTIVE_TERRAIN_V2} record lists no qualified modification forms"
        )
    return modifications, None


def unqualified_modification_forms(layers: list, record: dict) -> list[str]:
    """Forms in Commander's staged ``layers`` the engine does not qualify.

    ``layers`` is the output of Commander's modification reader (the
    engine's ``modification_layers`` request field); ``record`` is the
    engine's ``effective_terrain.modifications`` record. A layer kind, ground
    line subtype or elevation type missing from ``record["supported"]`` is
    unqualified, as are the forms the engine lists as refused and refuses
    when it validates a request: shape-Z polygons without boundary
    elevations and multi-part polygons that need a TIN. ``[]`` means every
    form is qualified. Pure: reads nothing.
    """
    supported = record.get("supported") if isinstance(record, dict) else None
    supported = supported if isinstance(supported, dict) else {}
    problems: list[str] = []
    for layer in layers:
        kind = layer.get("kind")
        name = layer.get("name", "?")
        form = supported.get(kind)
        if not isinstance(form, dict):
            problems.append(f"layer {name!r}: {kind!r} layers are not qualified by the engine")
            continue
        subtypes = form.get("subtypes")
        subtype = layer.get("subtype")
        if subtypes is not None and subtype is not None and subtype not in subtypes:
            problems.append(f"layer {name!r}: {kind} subtype {subtype!r} is not qualified")
        elevation_types = form.get("elevation_types")
        for feature in layer.get("features") or ():
            et = feature.get("elevation_type")
            if elevation_types is not None and et not in elevation_types:
                problems.append(f"layer {name!r}: {kind} elevation type {et!r} is not qualified")
                break
            if kind == "polygon":
                if feature.get("use_shape_z") and not feature.get("generate_boundary"):
                    problems.append(f"layer {name!r}: shape-Z polygons are refused by the engine")
                    break
                needs_tin = feature.get("generate_boundary") or layer.get("control_points")
                if needs_tin and len(feature.get("rings") or ()) > 1:
                    problems.append(
                        f"layer {name!r}: multi-part polygons needing a TIN are refused by the engine"
                    )
                    break
    return problems


def _public_modification_reader():
    """``RasFim.read_terrain_modifications`` (ras-commander 0.119+), or None."""
    try:
        from ras_commander.fim import RasFim
    except ImportError:
        return None
    read = getattr(RasFim, "read_terrain_modifications", None)
    return read if callable(read) else None


def _private_modification_reader():
    """The private ``ras_commander.fim._terrain_mods`` module, or None.

    Fallback for a ras-commander that stages modifications but has no public
    reader yet.
    """
    try:
        from ras_commander.fim import _terrain_mods
    except ImportError:
        return None
    needed = ("read_layers", "project_length_units", "project_hecras_version", "hecras_version_for")
    if not all(callable(getattr(_terrain_mods, name, None)) for name in needed):
        return None
    return _terrain_mods


@dataclass(frozen=True)
class ModificationReader:
    """Commander's terrain-modification reader, public or private.

    ``read(terrain_hdf, rasmap_path, ras_version)`` returns a record shaped
    like ``RasFim.read_terrain_modifications``: ``status`` (``staged``,
    ``none`` or ``refused``), ``refusal``, ``hecras_version``,
    ``hecras_version_basis``, ``length_units`` (``None`` when the version or
    units could not be resolved) and ``layers``.
    """

    name: str
    read: Callable[[Path, Optional[Path], Optional[str]], dict]


def modification_refusal_reason(code: Any, message: Optional[str]) -> Optional[str]:
    """Stable reason code of a modification-reader refusal, or None.

    Uses the code Commander attaches (``FimUnsupportedError.reason``, or
    ``refusal_reason`` on the public record) when present; for an older
    Commander without codes, recognises a multi-ring polygon from the message.
    """
    if code:
        return str(code)
    text = (message or "").lower()
    if MULTI_RING_POLYGON in text or "multi-ring polygon" in text:
        return MULTI_RING_POLYGON
    return None


def _read_public(read: Callable) -> Callable:
    def run(hdf: Path, rasmap_path: Optional[Path], ras_version: Optional[str]) -> dict:
        record = dict(read(hdf, rasmap_path=rasmap_path, hecras_version=ras_version))
        if ras_version is not None and record.get("hecras_version_basis") == "argument":
            record["hecras_version_basis"] = f"ras_version {ras_version}"
        return record

    return run


def _read_private(module: Any) -> Callable:
    def run(hdf: Path, rasmap_path: Optional[Path], ras_version: Optional[str]) -> dict:
        record: dict[str, Any] = {
            "status": "refused", "refusal": None, "hecras_version": None,
            "hecras_version_basis": None, "length_units": None, "layers": [],
        }
        try:
            if ras_version is not None:
                version = module.hecras_version_for(ras_version)
                basis = f"ras_version {ras_version}"
            else:
                version, basis = module.project_hecras_version(hdf, rasmap_path, None)
            record.update(hecras_version=version, hecras_version_basis=basis)
            units = module.project_length_units(hdf, rasmap_path, None)
        except Exception as exc:
            record["refusal"] = str(exc)
            return record
        record["length_units"] = units
        try:
            layers = module.read_layers(hdf, length_units=units, hecras_version=version)
        except Exception as exc:  # FimUnsupportedError: an unqualified form
            record["refusal"] = str(exc)
            record["refusal_reason"] = getattr(exc, "reason", None)
            return record
        record.update(status="staged" if layers else "none", layers=layers)
        return record

    return run


def _modification_reader() -> Optional[ModificationReader]:
    """Commander's modification reader: the public API, else the private module.

    ``None`` when the installed ras-commander has neither (before 0.119);
    such terrains stay on the RAS Mapper export.
    """
    read = _public_modification_reader()
    if read is not None:
        return ModificationReader(MODIFICATION_READER_PUBLIC, _read_public(read))
    module = _private_modification_reader()
    if module is not None:
        return ModificationReader(MODIFICATION_READER_PRIVATE, _read_private(module))
    return None


@dataclass(frozen=True)
class HecRasRuntime:
    """The one HEC-RAS release a terrain's export and native route both use.

    ``label`` is what ras2cng passes as ``hecras_version`` to the RAS Mapper
    terrain export: ``--ras-version`` when given, else the version
    ras-commander detects from the project's plan files (``init_ras_project``,
    exactly as the export detects it). ``canonical`` is the export's
    canonical release (``"7.0.1"``, ``"6.6"``), or ``None`` when the version
    could not be resolved or the export does not support it, with the reason
    in ``problem``. The native route stages the modifications for
    ``canonical`` and builds with it, so both terrains follow one release.
    """

    label: Optional[str]
    basis: str
    canonical: Optional[str]
    problem: Optional[str] = None


def _runtime_release(label: str) -> Optional[str]:
    """``"7.0.1"`` / ``"6.6"`` from a version label, without ras-commander's table."""
    match = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", str(label))
    if match is None:
        return None
    major, minor, patch = int(match.group(1)), int(match.group(2)), int(match.group(3) or 0)
    return f"{major}.{minor}" if patch == 0 else f"{major}.{minor}.{patch}"


def resolve_hecras_runtime(prj_file: Path, ras_version: Optional[str]) -> HecRasRuntime:
    """Resolve the HEC-RAS release the RAS Mapper terrain export will use.

    ``ras_version`` (``--ras-version``) wins; otherwise the release
    ras-commander's ``init_ras_project`` detects from the project's plan files,
    which is what :func:`ras2cng.terrain.export_modified_terrain` uses when it
    is given no version. The release is then checked as the export checks it
    (``resolve_supported_hecras_version``: 6.4.1, 6.5, 6.6, 7.0.1, 7.0.2,
    7.1). Never raises.
    """
    if ras_version:
        label, basis = str(ras_version), f"--ras-version {ras_version}"
    else:
        try:
            from ras_commander import init_ras_project

            ras = init_ras_project(Path(prj_file), ras_object="new", load_results_summary=False)
            detected = getattr(ras, "ras_version", None)
        except Exception as exc:
            return HecRasRuntime(
                None, "project plan files", None,
                f"could not open the project to detect its HEC-RAS version: {exc}",
            )
        if not detected:
            return HecRasRuntime(
                None, "project plan files", None,
                "no installed HEC-RAS release matches the project's plan files "
                "(pass --ras-version)",
            )
        label = str(detected)
        basis = f"detected from the project's plan files ({label})"
    try:
        from ras_commander.native.terrain_export_host import resolve_supported_hecras_version
    except ImportError:
        return HecRasRuntime(label, basis, _runtime_release(label))
    try:
        canonical = resolve_supported_hecras_version(label, None)
    except (ValueError, TypeError) as exc:
        return HecRasRuntime(label, basis, None, str(exc))
    return HecRasRuntime(label, basis, str(canonical))


def rasmap_unapplied_modifications(
    terrain_hdf: Path, rasmap_path: Optional[Path]
) -> Optional[list[dict]]:
    """Commander's warnings when RAS Mapper applies none of a terrain's HDF layers.

    RAS Mapper (HEC-RAS 6.6, 7.0.1) applies modifications only from a
    ``.rasmap`` ``ElevationModificationGroup`` named exactly
    ``Modifications``; with none (missing, or only a misnamed group) it
    applies none of the layers stored in the terrain HDF. ras-commander 0.119
    reports this from ``RasFim.top_priority_terrain_source(...,
    rasmap_path=...)`` as ``modified: False`` with ``rasmap_warnings``
    (:data:`RASMAP_UNAPPLIED_CODES`). Returns those warnings, or ``None``
    when the layers are applied, there is no rasmap, or Commander cannot
    tell (an older Commander, an unreadable terrain): such a terrain stays
    modified. Never raises.
    """
    if rasmap_path is None:
        return None
    try:
        from ras_commander import RasFim
    except ImportError:
        return None
    check = getattr(RasFim, "top_priority_terrain_source", None)
    if not callable(check):
        return None
    try:
        info = check(Path(terrain_hdf), rasmap_path=Path(rasmap_path))
    except Exception:
        return None
    if (
        not isinstance(info, dict)
        or info.get("modified") is not False
        or not info.get("rasmap_checked")
    ):
        return None
    warnings = [
        dict(warning) for warning in info.get("rasmap_warnings") or ()
        if isinstance(warning, dict) and warning.get("code") in RASMAP_UNAPPLIED_CODES
    ]
    return warnings or None


def normalize_invalid_cells(policy: Optional[str]) -> Optional[str]:
    """Validate an ``invalid_cells`` policy; ``None`` keeps the engine default."""
    if policy is None:
        return None
    value = str(policy).strip().lower()
    if value not in INVALID_CELL_POLICIES:
        raise ValueError(
            f"invalid_cells must be one of {', '.join(INVALID_CELL_POLICIES)}; got {policy!r}"
        )
    return value


def invalid_cells_supported() -> bool:
    """True when RasFim applies an ``invalid_cells`` policy (ras-commander 0.110.2+)."""
    try:
        from ras_commander import RasFim
    except ImportError:
        return False
    return _has_kwargs(RasFim.map_sloping_profiles, "invalid_cells")


class InvalidCellsUnsupported(RuntimeError):
    """The installed ras-commander cannot apply the requested policy.

    Deliberately not :class:`FastMapUnavailable`: that is a terrain refusal
    and would move :func:`with_terrain` on to the next (exported) terrain.
    """


def _invalid_cells_kwargs(function: Any, policy: Optional[str]) -> dict[str, str]:
    """``{"invalid_cells": policy}`` when ``function`` takes it, else ``{}``.

    Before ras-commander 0.110.2 every mapping call assembled cell polygons
    strictly, i.e. behaved as ``"raise"``; any other explicit policy cannot be
    honoured there and is refused (the product group falls back to RAS Mapper).
    """
    policy = normalize_invalid_cells(policy)
    if policy is None:
        return {}
    if _has_kwargs(function, "invalid_cells"):
        return {"invalid_cells": policy}
    if policy == "raise":
        return {}
    raise InvalidCellsUnsupported(
        f"the installed ras-commander cannot apply invalid_cells={policy!r} "
        "(needs ras-commander>=0.110.2)"
    )


def _invalid_cells_record(manifest: Any) -> Optional[dict]:
    """The engine's ``fim-invalid-cells/1`` record from a manifest's attrs."""
    record = getattr(manifest, "attrs", {}).get("invalid_cells")
    return record if isinstance(record, dict) else None


def resolve_product_writer(writer: Optional[str] = None) -> str:
    """Return ``auto``/``native``/``rasterio``; ``None`` reads the env var."""
    value = writer if writer is not None else os.environ.get(PRODUCT_WRITER_ENV, "auto")
    normalized = str(value).strip().lower()
    if normalized not in PRODUCT_WRITERS:
        raise ValueError(
            f"fast-map product writer must be one of {', '.join(PRODUCT_WRITERS)}; got {value!r}"
        )
    return normalized


def native_writer_supported() -> bool:
    """True when RasFim and the engine can write sloping GeoTIFFs natively.

    Needs ``product_writer`` on ``RasFim.map_sloping_profiles``
    (ras-commander 0.110.3) and an engine whose sloping method advertises
    ``native_geotiff_outputs`` (fast-map 0.11). Checked up front: a refusal
    inside the engine call would otherwise look like a terrain refusal.
    """
    try:
        from ras_commander import RasFim
        from ras_commander.fim import _engine
    except ImportError:
        return False
    if not _has_kwargs(RasFim.map_sloping_profiles, "product_writer"):
        return False
    try:
        method = _engine.engine_method(RasFim.find_engine())
    except Exception:
        return False
    return bool((method.get("sloping") or {}).get("native_geotiff_outputs"))


def _select_product_writer(writer: Optional[str], *, velocity: bool) -> Optional[str]:
    """The ``product_writer`` to pass to sloping mapping, or ``None`` to omit it.

    Native delivery excludes velocity (ras-commander refuses the combination),
    so a velocity request uses rasterio. ``native`` is strict: it raises when
    the installation cannot honour it.
    """
    choice = resolve_product_writer(writer)
    if choice == "rasterio":
        return "rasterio" if _product_writer_kwarg() else None
    if velocity:
        if choice == "native":
            raise RuntimeError(
                "fast-map native GeoTIFF delivery excludes velocity; use "
                "--fast-map-writer auto or rasterio with --fast-map-velocity"
            )
        return "rasterio" if _product_writer_kwarg() else None
    if native_writer_supported():
        return "native"
    if choice == "native":
        raise RuntimeError(
            "fast-map native GeoTIFF delivery needs ras-commander>=0.110.3 and an "
            "engine advertising native_geotiff_outputs (ras-commander-fast-map>=0.11)"
        )
    return "rasterio" if _product_writer_kwarg() else None


def _product_writer_kwarg() -> bool:
    try:
        from ras_commander import RasFim
    except ImportError:
        return False
    return _has_kwargs(RasFim.map_sloping_profiles, "product_writer")


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


def _check_scope(RasFim: Any, plan_hdf: Path, storage_areas_enabled: bool = False) -> int:
    """Return the plan's storage-area count; raise when out of fast-map scope.

    Raises :class:`FastMapUnavailable` for 1D elements, a missing engine,
    storage areas without the opt-in (``storage_areas_enabled``, see
    :func:`resolve_storage_areas`), or storage areas on an installation
    without level-pool surfaces (:func:`storage_areas_supported`).
    """
    from ras_commander.fim.errors import FimEngineNotFoundError, FimUnsupportedError

    try:
        scope = RasFim.validate_2d_model(plan_hdf)
        RasFim.find_engine()
    except (FimUnsupportedError, FimEngineNotFoundError) as exc:
        raise FastMapUnavailable(str(exc)) from exc
    storage_areas = int(scope.get("storage_areas") or 0) if isinstance(scope, dict) else 0
    if storage_areas and not storage_areas_enabled:
        raise FastMapUnavailable(
            f"model has {storage_areas} storage area(s); fast-map storage-area mapping is "
            "opt-in until it is faster than RAS Mapper (--fast-map-storage-areas or "
            f"{STORAGE_AREAS_ENV}=1 maps them in Rust as level pools)"
        )
    if storage_areas and not storage_areas_supported():
        raise FastMapUnavailable(
            f"model has {storage_areas} storage area(s); the installed fast-map does not "
            f"render storage-area surfaces ({STORAGE_AREA_METHOD}), so they would be "
            "missing from the map"
        )
    return storage_areas


def preflight(
    plan_hdf: Path,
    ras: Any,
    render_mode: Optional[str],
    storage_areas: Optional[bool] = None,
) -> dict[str, Any]:
    """Check a plan is fast-map's before any terrain work; return its render.

    Raises :class:`FastMapUnavailable` when RasFim or the engine is missing,
    the model has 1D elements, storage areas without the opt-in
    (``storage_areas``; ``None`` reads ``RAS2CNG_FAST_MAP_STORAGE_AREAS``,
    default off) or that the engine cannot draw, or no render mode is set.
    These are cheap HDF/XML reads, so an out-of-scope plan never triggers a
    terrain export before it goes to RAS Mapper. The returned render also
    carries ``storage_areas`` (the count the engine will draw as level pools),
    which :func:`velocity_plan`, :func:`derived_plan` and
    :func:`whole_simulation_types` use to route products before terrain work.
    """
    try:
        from ras_commander import RasFim
    except ImportError as exc:
        raise FastMapUnavailable(f"ras-commander has no RasFim: {exc}") from exc
    enabled = resolve_storage_areas(storage_areas)
    count = _check_scope(RasFim, Path(plan_hdf), enabled)
    render = render_settings(ras, render_mode)
    render["storage_areas_enabled"] = enabled
    if count:
        render["storage_areas"] = count
    return render


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
        effective: bool = True,
        threads: Optional[int] = None,
        max_working_mib: Optional[int] = None,
    ) -> None:
        self.prj_file = Path(prj_file)
        #: Try the native effective terrain before the RAS Mapper export.
        self.effective = bool(effective)
        self.threads = threads
        self.max_working_mib = max_working_mib
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
        # One resolver per run: a route manifest an earlier run left in this work
        # directory does not describe this run (e.g. its terrain is now unmodified
        # and no route is written). Routes for this run's plans are rewritten as
        # they are decided (_write_routes).
        try:
            (self.work_dir / TERRAIN_ROUTE_MANIFEST).unlink(missing_ok=True)
        except OSError as exc:
            self.notes.append(f"could not remove the previous {TERRAIN_ROUTE_MANIFEST}: {exc}")

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
        # The release the native route also uses (hecras_runtime); without
        # --ras-version it is the one the export would detect itself.
        runtime = self.hecras_runtime()
        try:
            exported = export_modified_terrain(
                self.prj_file, target, terrain_name=name, overwrite=True,
                hecras_version=runtime.label,
            )
        except Exception as exc:
            raise FastMapUnavailable(
                f"could not export the registered terrain {name or '(default)'!r} "
                f"for fast-map: {exc}. Supply a float32 export with --fast-map-terrain."
            ) from exc
        self._exported[name] = Path(exported)
        return self._exported[name]

    def hecras_runtime(self) -> HecRasRuntime:
        """The HEC-RAS release of this run's terrain export and native route.

        Resolved once (:func:`resolve_hecras_runtime`): ``--ras-version``,
        else what the export detects from the project. The RAS Mapper export,
        Commander's modification reader and the native effective terrain all
        take it, so a modified terrain follows one release whichever route
        builds it.
        """
        key = ("runtime", None)
        if key not in self._sources:
            self._sources[key] = resolve_hecras_runtime(self.prj_file, self.ras_version)
        return self._sources[key]

    def candidates(self, plan_hdf: Path, *, use_source: bool = True):
        """Yield ``(kind, terrain_tif, terrain_hdf_or_None)`` cheapest first.

        A supplied terrain is the only candidate. Otherwise, when RasFim can
        map on the priority-1 source TIFF: that TIFF, then its CRS-canonical
        copy (the engine refuses some original CRS definitions, e.g. Davis).
        The copy is only tried after a CRS refusal (``last_refusal``). Then
        the native effective terrain when the engine builds it (for a
        modified terrain only when :meth:`modified_route` chose the native
        route; the source TIFF is never used for one), and last the RAS
        Mapper export, with
        its own CRS-canonical copy after a CRS refusal (the same pixels with
        EPSG GeoTIFF keys; ``RasFim.canonicalize_terrain_crs`` verifies every
        pixel bit). Each later candidate is only built when the caller asks
        for it, i.e. after the previous one was refused.
        """
        if self.terrain_path is not None:
            yield TERRAIN_SUPPLIED, self.for_plan(plan_hdf), None
            return
        if self.modifications(plan_hdf):
            # A modified terrain is built by RAS Mapper's export unless every
            # gate in modified_route() passes (engine /2, every modification
            # form qualified, effective terrain enabled and supported); then
            # the native effective terrain with modifications is tried first.
            # The unmodified source TIFF is never used for a modified terrain.
            route = self.modified_route(plan_hdf)
            if route is not None and route.get("route") == MODIFIED_ROUTE_NATIVE:
                effective = self._effective(plan_hdf)
                if effective is not None:
                    yield TERRAIN_EFFECTIVE, effective, None
            yield from self._export_candidates(plan_hdf)
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
        if self.effective and effective_terrain_supported():
            effective = self._effective(plan_hdf)
            if effective is not None:
                yield TERRAIN_EFFECTIVE, effective, None
        yield from self._export_candidates(plan_hdf)

    def _export_candidates(self, plan_hdf: Path):
        """The RAS Mapper export, then its CRS-canonical copy after a CRS refusal."""
        export = self.for_plan(plan_hdf)
        self.last_refusal = None
        yield TERRAIN_EXPORT, export, None
        if "crs" in (self.last_refusal or "").lower():
            canonical = self._canonical(export)
            if canonical is not None:
                yield TERRAIN_EXPORT_CANONICAL, canonical, None

    def modifications(self, plan_hdf: Path) -> list[str]:
        """The plan terrain's modifications (cached), ``[]`` when unmodified.

        Any modification skips the unmodified source TIFF and refuses Rust
        boundaries. The terrain goes to RAS Mapper's export unless
        :meth:`modified_route` qualifies the native effective terrain with
        modifications. A terrain whose HDF cannot be found or read counts as
        unknown, which also keeps it on RAS Mapper. A terrain whose HDF layers
        RAS Mapper does not apply (no ``.rasmap`` group named exactly
        ``Modifications``, :func:`rasmap_unapplied_modifications`) is
        unmodified, as in ras-commander 0.119; its route record
        (:data:`MODIFIED_ROUTE_UNAPPLIED`) keeps Commander's warning.
        """
        try:
            hdf = self._terrain_hdf_for(plan_hdf)
        except Exception:  # no terrain HDF: no Rust terrain step can run either
            hdf = None
        key = ("modifications", str(hdf))
        if key not in self._sources:
            if hdf is None:
                found: list[str] = []
            else:
                from ras2cng.terrain import terrain_modifications

                rasmap = self.prj_file.with_suffix(".rasmap")
                try:
                    found = terrain_modifications(
                        hdf, rasmap if rasmap.is_file() else None
                    )
                except Exception as exc:  # unreadable: do not assume unmodified
                    found = [f"unreadable terrain HDF ({exc})"]
                unapplied = (
                    rasmap_unapplied_modifications(hdf, rasmap if rasmap.is_file() else None)
                    if found else None
                )
                if unapplied:
                    # RAS Mapper applies none of these layers (no .rasmap group
                    # named "Modifications"): an unmodified terrain, as in
                    # ras-commander 0.119. Recorded, with Commander's warning.
                    message = "; ".join(str(w.get("message") or w.get("code")) for w in unapplied)
                    self.notes.append(
                        f"terrain {hdf.name} stores {len(found)} modification layer(s) that "
                        f"RAS Mapper does not apply; mapped as unmodified ({message})"
                    )
                    self._sources[("route", str(hdf))] = {
                        "terrain_hdf": str(hdf),
                        "modifications": list(found),
                        "route": MODIFIED_ROUTE_UNAPPLIED,
                        "reason": f"RAS Mapper applies none of the terrain's modifications: {message}",
                        "rasmap_warnings": unapplied,
                    }
                    self._write_routes()
                    found = []
            if found:
                shown = ", ".join(found[:5]) + (" ..." if len(found) > 5 else "")
                self.notes.append(
                    f"terrain {hdf.name if hdf else '?'} has {len(found)} modification(s) "
                    f"({shown})"
                )
            self._sources[key] = found
        return self._sources[key]

    def modified_route(self, plan_hdf: Path) -> Optional[dict[str, Any]]:
        """Decide, once per terrain HDF, how a modified terrain is built.

        ``None`` for an unmodified terrain, or the
        :data:`MODIFIED_ROUTE_UNAPPLIED` record when its HDF layers are not
        applied by RAS Mapper (see :meth:`modifications`). Otherwise a record
        with ``route`` :data:`MODIFIED_ROUTE_NATIVE` (the native effective
        terrain with modifications, ``rasmapper-effective-terrain/2``) or
        :data:`MODIFIED_ROUTE_RASMAPPER` (RAS Mapper's export, as before) and
        the ``reason`` for the RAS Mapper route. The native route needs all
        of: the modification names readable and stored in the terrain HDF;
        the effective terrain enabled and built by RasFim; an engine that
        advertises ``/2`` with a ``modifications`` record
        (:func:`modified_terrain_capability`); Commander's modification
        reader (ras-commander 0.119+), which refuses the forms it knows the
        engine refuses; the RAS Mapper export's own HEC-RAS release
        (:meth:`hecras_runtime`, recorded as ``hecras_runtime``) resolved and
        in :data:`QUALIFIED_MODIFICATION_RUNTIMES`, the release both routes
        then use; and every staged layer form in the engine's qualified
        list (:func:`unqualified_modification_forms`). An engine refusal at
        build or mapping time still falls back to the export and is added to
        the record as ``fallback``. Every record is written to
        :data:`TERRAIN_ROUTE_MANIFEST` in the work directory. ``None`` too
        when the caller supplied the terrain (``--fast-map-terrain``).
        """
        if self.terrain_path is not None:
            return None
        found = self.modifications(plan_hdf)
        try:
            hdf = self._terrain_hdf_for(plan_hdf)
        except Exception:
            hdf = None
        key = ("route", str(hdf))
        if not found:
            # Unmodified; the record of layers RAS Mapper does not apply, if any.
            return self._sources.get(key)
        if key in self._sources:
            return self._sources[key]
        record: dict[str, Any] = {
            "terrain_hdf": str(hdf) if hdf is not None else None,
            "modifications": list(found),
        }
        reason, details = self._modified_route_reason(hdf, found)
        record.update(details)
        if reason is None:
            record.update(route=MODIFIED_ROUTE_NATIVE, reason=None)
            self.notes.append(
                f"modified terrain qualified for the native effective terrain "
                f"({EFFECTIVE_TERRAIN_V2}; {len(details.get('layers') or [])} layer(s))"
            )
        else:
            record.update(route=MODIFIED_ROUTE_RASMAPPER, reason=reason)
            self.notes.append(f"modified terrain goes to the RAS Mapper export: {reason}")
        self._sources[key] = record
        self._write_routes()
        return record

    def _modified_route_reason(
        self, hdf: Optional[Path], found: list[str]
    ) -> tuple[Optional[str], dict[str, Any]]:
        """``(reason for the RAS Mapper route or None, details)``."""
        if hdf is None:
            return "the terrain HDF was not found", {}
        unreadable = [
            name for name in found
            if not (name.startswith("hdf:") or name.startswith("rasmap:"))
            or name.startswith("hdf:/")
        ]
        if unreadable:
            return f"the terrain's modifications could not be read ({unreadable[0]})", {}
        stored = {name[4:] for name in found if name.startswith("hdf:")}
        rasmap_only = sorted(
            name[7:] for name in found
            if name.startswith("rasmap:") and name[7:] not in stored
        )
        if rasmap_only:
            return (
                f"rasmap modification layer(s) {rasmap_only} are not stored in {hdf.name}", {}
            )
        if not self.effective:
            return "the native effective terrain is turned off (--no-fast-map-effective-terrain)", {}
        if not effective_terrain_supported():
            return "RasFim or the engine cannot build the native effective terrain", {}
        record, reason = modified_terrain_capability()
        if record is None:
            return reason, {}
        reader = _modification_reader()
        if reader is None:
            return (
                "ras-commander has no terrain-modification reader (needs ras-commander 0.119.0 "
                "or later)", {"method_id": EFFECTIVE_TERRAIN_V2, "modification_reader": None},
            )
        rasmap = self.prj_file.with_suffix(".rasmap")
        rasmap_path = rasmap if rasmap.is_file() else None
        details: dict[str, Any] = {
            "method_id": EFFECTIVE_TERRAIN_V2, "modification_reader": reader.name,
        }
        # One release for both routes: the one the RAS Mapper export uses.
        runtime = self.hecras_runtime()
        details.update(
            hecras_runtime=runtime.canonical or runtime.label,
            hecras_runtime_basis=runtime.basis,
        )
        if runtime.problem is not None or runtime.canonical is None:
            return (
                "the HEC-RAS release of the RAS Mapper export could not be resolved, so the "
                f"native terrain cannot follow it: {runtime.problem}", details,
            )
        if runtime.canonical not in QUALIFIED_MODIFICATION_RUNTIMES:
            return (
                f"the RAS Mapper export runs on HEC-RAS {runtime.canonical}; the engine's "
                "modification rules are qualified against HEC-RAS "
                f"{' and '.join(QUALIFIED_MODIFICATION_RUNTIMES)} exports only", details,
            )
        try:
            staged = reader.read(hdf, rasmap_path, runtime.canonical)
        except Exception as exc:
            return f"Commander's modification reader failed: {exc}", details
        version = staged.get("hecras_version")
        units = staged.get("length_units")
        if version is None or units is None:
            return (
                "could not resolve the project's HEC-RAS version or units: "
                f"{staged.get('refusal')}", details,
            )
        details.update(
            hecras_version=version,
            hecras_version_basis=runtime.basis,
            length_units=units,
        )
        if version != "7.0.1" and "hecras_version" not in record:
            return (
                f"the engine does not advertise hecras_version; HEC-RAS {version} "
                "modification rules are not available", details,
            )
        if staged.get("status") == "refused":
            code = modification_refusal_reason(staged.get("refusal_reason"), staged.get("refusal"))
            if code is not None:
                details["refusal_reason"] = code
            return (
                f"Commander's modification reader refused the terrain: {staged.get('refusal')}",
                details,
            )
        layers = list(staged.get("layers") or ())
        details["layers"] = [
            {
                "name": layer.get("name"),
                "kind": layer.get("kind"),
                "subtype": layer.get("subtype"),
                "priority": layer.get("priority"),
                "features": len(layer.get("features") or ()),
                "elevation_types": sorted(
                    {str(f.get("elevation_type")) for f in layer.get("features") or ()}
                ),
            }
            for layer in layers
        ]
        problems = unqualified_modification_forms(layers, record)
        if problems:
            details["unqualified"] = problems
            return "; ".join(problems), details
        return None, details

    def record_product_group(
        self, plan_hdf: Path, label: str, kind: str
    ) -> Optional[dict[str, Any]]:
        """Snapshot the route of a product group that mapped on terrain ``kind``.

        Returns an independent copy of :meth:`modified_route`'s record (``None``
        for an unmodified terrain without a record) with ``product_group`` and
        ``terrain`` set, for ``MapResult.fast_map_terrain_route``. A later group's
        refusal appends to the shared record's ``fallback``; the copy keeps what
        held when this group mapped. The same snapshot (terrain kind and the
        ``fallback`` entries so far) is kept under the shared record's
        ``product_groups[<plan HDF name>][label]`` in the route manifest.
        """
        import copy

        route = self.modified_route(plan_hdf)
        if route is None:
            return None
        fallback = list(route.get("fallback") or ())
        route.setdefault("product_groups", {}).setdefault(Path(plan_hdf).name, {})[label] = {
            "terrain": kind, "fallback": fallback,
        }
        self._write_routes()
        snapshot = copy.deepcopy({k: v for k, v in route.items() if k != "product_groups"})
        snapshot.update(product_group=label, terrain=kind)
        return snapshot

    def _record_fallback(self, plan_hdf: Path, message: str) -> None:
        """Note on a native-routed modified terrain that the engine refused it."""
        try:
            hdf = self._terrain_hdf_for(plan_hdf)
        except Exception:
            return
        record = self._sources.get(("route", str(hdf)))
        if isinstance(record, dict) and record.get("route") == MODIFIED_ROUTE_NATIVE:
            record.setdefault("fallback", []).append(message)
            self._write_routes()

    def _write_routes(self) -> None:
        routes = [v for k, v in self._sources.items() if k[0] == "route"]
        try:
            self.work_dir.mkdir(parents=True, exist_ok=True)
            (self.work_dir / TERRAIN_ROUTE_MANIFEST).write_text(
                json.dumps({"schema": "ras2cng-terrain-route/1", "routes": routes},
                           indent=2, default=str),
                encoding="utf-8",
            )
        except OSError as exc:
            self.notes.append(f"could not write {TERRAIN_ROUTE_MANIFEST}: {exc}")

    def _effective(self, plan_hdf: Path) -> Optional[Path]:
        """RAS Mapper's effective terrain built in Rust, or None (with a note).

        Built once per terrain HDF per run. For a modified terrain it is
        only called when :meth:`modified_route` chose the native route; the
        engine then applies the modifications (``/2``). A refusal leaves the
        terrain to the RAS Mapper export and is recorded on the route.
        """
        hdf = self._terrain_hdf_for(plan_hdf)
        if hdf is None:
            return None
        key = ("effective", str(hdf))
        if key not in self._sources:
            from ras_commander import RasFim

            rasmap = self.prj_file.with_suffix(".rasmap")
            target = self.work_dir / f"{_slug(hdf.stem)}.effective.tif"
            kwargs: dict[str, Any] = {}
            if self.modifications(plan_hdf) and _has_kwargs(
                RasFim.build_effective_terrain, "hecras_version"
            ):
                # The release the RAS Mapper export uses (hecras_runtime), with
                # or without --ras-version; never Commander's own guess.
                runtime = self.hecras_runtime()
                if runtime.canonical is not None:
                    kwargs["hecras_version"] = runtime.canonical
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                RasFim.build_effective_terrain(
                    hdf, target,
                    rasmap_path=rasmap if rasmap.is_file() else None,
                    threads=default_threads() if self.threads is None else int(self.threads),
                    max_working_mib=(
                        default_working_mib() if self.max_working_mib is None
                        else int(self.max_working_mib)
                    ),
                    **kwargs,
                )
                self._sources[key] = target
            except Exception as exc:
                self.notes.append(f"native effective terrain unavailable: {exc}")
                self._record_fallback(plan_hdf, f"effective terrain build refused: {exc}")
                self._sources[key] = None
        return self._sources[key]

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

    def boundary_terrain(
        self, plan_hdf: Path, method: str = BOUNDARY_DETERMINISTIC
    ) -> tuple[Path, Path]:
        """Return the original ``(terrain TIFF, terrain HDF)`` for boundaries.

        Boundaries need the registered terrain's own masks, perimeters and
        stitch TIN, so the flattened raster export cannot be used. The
        deterministic method refuses a terrain with more than one source
        TIFF. The native-serial method takes every original source and is
        given the first one in the terrain HDF's priority order
        (:func:`native_primary_source`); the engine checks that choice and
        refuses a wrong one. Terrain modifications are refused for both.
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
        from ras2cng.terrain import terrain_modifications

        rasmap = self.prj_file.with_suffix(".rasmap")
        try:
            mods = terrain_modifications(Path(hdf), rasmap if rasmap.is_file() else None)
        except Exception as exc:
            mods = [f"unreadable terrain HDF ({exc})"]
        if mods:
            raise FastMapUnavailable(
                f"terrain {Path(hdf).name} has {len(mods)} modification(s); "
                "Rust boundaries are only used on unmodified terrains"
            )
        tifs = _discover_tifs_for_hdf(Path(hdf))
        if method == BOUNDARY_NATIVE_SERIAL and len(tifs) > 1:
            return native_primary_source(Path(hdf)), Path(hdf)
        if len(tifs) != 1:
            raise FastMapUnavailable(
                f"Rust inundation boundaries need a single-source terrain; "
                f"{Path(hdf).name} has {len(tifs)} source TIFF(s)"
                + (
                    "" if method == BOUNDARY_NATIVE_SERIAL or not tifs else
                    " (--fast-map-boundary-method native-serial takes every source)"
                )
            )
        return tifs[0], Path(hdf)


def native_primary_source(terrain_hdf: Path) -> Path:
    """The first original source TIFF of a terrain HDF, by stored priority.

    Sources with a complete set of stored ``Priority`` values are taken in
    ascending order; otherwise the HDF's own order is kept. The engine
    verifies the choice against the terrain HDF and refuses a mismatch, so a
    wrong answer here cannot produce a boundary. Raises
    :class:`FastMapUnavailable` when the sources cannot be read.
    """
    import h5py

    terrain_hdf = Path(terrain_hdf)
    records: list[tuple[int, str]] = []
    try:
        with h5py.File(terrain_hdf, "r") as hdf:
            group = hdf.get("Terrain")
            if not isinstance(group, h5py.Group):
                raise KeyError("no Terrain group")
            for name in group:
                source = group[name]
                if not isinstance(source, h5py.Group):
                    continue
                filename = source.attrs.get("File")
                if filename is None:
                    continue
                if isinstance(filename, bytes):
                    filename = filename.decode("utf-8", errors="replace")
                priority = source.attrs.get("Priority", -1)
                records.append((int(priority), Path(str(filename).replace("\\", "/")).name))
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise FastMapUnavailable(
            f"could not read the terrain sources of {terrain_hdf.name}: {exc}"
        ) from exc
    if not records:
        raise FastMapUnavailable(f"{terrain_hdf.name} names no source TIFFs")
    if all(priority != -1 for priority, _ in records):
        records.sort(key=lambda record: record[0])
    primary = terrain_hdf.parent / records[0][1]
    if not primary.is_file():
        raise FastMapUnavailable(f"terrain source TIFF not found: {primary}")
    return primary


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
            if kind == TERRAIN_EFFECTIVE:
                resolver._record_fallback(plan_hdf, f"effective terrain refused when mapping: {exc}")
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
    invalid_cells: Optional[str] = None,
    product_writer: Optional[str] = None,
) -> FastMapProducts:
    """Map one plan's Max WSE/Depth with the Rust engine into ``output_dir``.

    Products are named like RAS Mapper's (``WSE (Max).tif``,
    ``Depth (Max).tif``) so downstream discovery is unchanged. The engine's
    product manifest is kept as ``fast_map_manifest.json``. Raises
    :class:`FastMapUnavailable` for anything outside fast-map's scope, and lets
    engine failures propagate.

    ``invalid_cells`` (``repair``/``hull``/``omit``/``raise``; ``None`` = the
    engine default, ``repair``) sets pixel ownership for cells whose face
    polylines do not form a valid polygon. The engine's record of every such
    cell is returned as ``result.invalid_cells`` and kept in the manifest.
    ``product_writer`` (``auto``/``native``/``rasterio``; ``None`` reads
    ``RAS2CNG_FAST_MAP_WRITER``) selects sloping GeoTIFF delivery; see
    :data:`PRODUCT_WRITERS`.
    """
    requested = [name for name in map_types if name in FAST_MAP_RASTER_TYPES]
    # ``velocity=True`` here renders velocity in the same plain-sloping call
    # (the original opt-in). ``ras2cng map`` uses :func:`map_velocity`
    # instead, so a velocity refusal never costs WSE/Depth.
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
    storage_areas = _check_scope(RasFim, plan_hdf, _render_storage_areas(render))
    if storage_areas and want_velocity:
        # Velocity is refused with storage-area surfaces; leave it to RAS Mapper.
        requested.remove(FAST_MAP_VELOCITY)
        want_velocity = False
        if not requested:
            return FastMapProducts()

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
        writer: Optional[str] = None
        try:
            if render["mode"] == "horizontal":
                # The cell table fixes the policy; mapping must be given the same one.
                cells = RasFim.get_cells_from_plan_hdf(
                    plan_hdf, mesh_name="*", profile_name="wsel_max",
                    **_invalid_cells_kwargs(RasFim.get_cells_from_plan_hdf, invalid_cells),
                )
                scope = {"terrain_hdf": terrain_hdf} if terrain_hdf is not None else {}
                if storage_areas:
                    # Level pools at the storage areas' Maximum Water Surface.
                    scope["storage_areas"] = {"wsel_max": "Max"}
                manifest = RasFim.map_horizontal_profiles(
                    cells, terrain_tif, work, profiles=["wsel_max"],
                    hdf_path=plan_hdf, **scope, **common,
                    **_invalid_cells_kwargs(RasFim.map_horizontal_profiles, invalid_cells),
                )
            else:
                pretty = render["mode"] == "slopingPretty"
                writer = _select_product_writer(product_writer, velocity=want_velocity)
                manifest = RasFim.map_sloping_profiles(
                    plan_hdf, terrain_tif, work,
                    profiles={"Max": "Max"},
                    render_mode=render["mode"],
                    reduce_shallow_to_horizontal=pretty and render["reduce_shallow_to_horizontal"],
                    use_depth_weighted_faces=pretty and render["use_depth_weighted_faces"],
                    **({"terrain_hdf": terrain_hdf} if terrain_hdf is not None else {}),
                    **({"velocity": True} if want_velocity else {}),
                    **({"storage_areas": True} if storage_areas else {}),
                    **({"product_writer": writer} if writer is not None else {}),
                    **_invalid_cells_kwargs(RasFim.map_sloping_profiles, invalid_cells),
                    **common,
                )
        except FimUnsupportedError as exc:
            raise FastMapUnavailable(str(exc)) from exc
        record = _invalid_cells_record(manifest)

        row = manifest.iloc[0]
        if row["status"] not in _COMPLETE_STATUSES:
            raise RuntimeError(f"fast-map {row['status']}: {row.get('error')}")

        products = {
            "wse": row.get("wse_tif"),
            "depth": row.get("depth_tif"),
            FAST_MAP_VELOCITY: row.get("velocity_tif"),
        }
        labels = {"wse": "WSE", "depth": "Depth", FAST_MAP_VELOCITY: "Velocity"}
        result = FastMapProducts()
        result.invalid_cells = record
        result.product_writer = writer or (None if render["mode"] == "horizontal" else "rasterio")
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
            # ras-commander 0.110.2+ writes the record itself; keep the attrs
            # copy when it did not (it is the same record).
            if record is not None:
                details.setdefault("invalid_cells", record)
            details["ras2cng"] = {
                "plan_hdf": str(plan_hdf),
                "terrain": str(terrain_tif),
                "terrain_hdf": str(terrain_hdf) if terrain_hdf is not None else None,
                "terrain_kind": terrain_kind,
                "render": render,
                "storage_areas": storage_areas,
                "storage_areas_opt_in": _render_storage_areas(render),
                "storage_area_method": STORAGE_AREA_METHOD if storage_areas else None,
                "invalid_cells_policy": normalize_invalid_cells(invalid_cells),
                "invalid_cells_count": (record or {}).get("count"),
                "product_writer": result.product_writer,
                "products": {k: [str(p) for p in v] for k, v in result.items()},
            }
            (output_dir / FAST_MAP_MANIFEST).write_text(
                json.dumps(details, indent=2, default=str), encoding="utf-8"
            )
        return result
    finally:
        shutil.rmtree(work, ignore_errors=True)


def velocity_plan(
    render: dict[str, Any], requested: Optional[bool] = None
) -> tuple[Optional[str], Optional[str]]:
    """Decide whether fast-map maps Max velocity for this plan.

    Returns ``(velocity_depth, None)`` to map it (``"rendered"`` or
    ``"horizontal"``), or ``(None, reason)`` to leave it to RAS Mapper.
    ``requested``: ``None`` = the default policy (sloping and horizontal on
    engines with the fast-map #20 velocity rules, and slopingPretty with the
    project's options on engines advertising :data:`WITH_FACES_METHOD`),
    ``True`` = also plain slopingPretty / older engines where supported,
    ``False`` = never. Checked before any terrain work, so a policy refusal
    never triggers an export.
    """
    if requested is False:
        return None, "disabled (--no-fast-map-velocity)"
    if render.get("storage_areas"):
        return None, "velocity is not qualified with storage-area surfaces"
    mode = render["mode"]
    modes = velocity_render_modes()
    if mode not in modes:
        return None, f"the installed fast-map does not map velocity in {mode!r} mode"
    if mode == "slopingPretty":
        reason = sloping_pretty_products_reason(render)
        if reason is None:
            # Qualified with exactly these options: a default.
            return "rendered", None
        if _pretty_options(render):
            return None, f"slopingPretty velocity with the project's options: {reason}"
        if requested is None:
            return None, (
                f"slopingPretty velocity is opt-in on this installation "
                f"(--fast-map-velocity): {reason}"
            )
        return "rendered", None
    if requested is None and not velocity_default_on():
        return None, "velocity is opt-in on this fast-map (--fast-map-velocity)"
    return ("horizontal" if mode == "horizontal" else "rendered"), None


def map_velocity(
    plan_hdf: Path,
    terrain_tif: Path,
    output_dir: Path,
    *,
    render: dict[str, Any],
    velocity_depth: str = "rendered",
    profile: str = "Max",
    threads: Optional[int] = None,
    writer_threads: int = 2,
    timeout: Optional[float] = None,
    max_working_mib: Optional[int] = None,
    terrain_hdf: Optional[Path] = None,
    terrain_kind: Optional[str] = None,
    invalid_cells: Optional[str] = None,
) -> FastMapProducts:
    """Map one plan's Max velocity with the Rust engine (``Velocity (Max).tif``).

    RAS Mapper's velocity values do not depend on the render mode; it clips
    them with the active mode's depth. Sloping modes render with that mode;
    horizontal renders plain sloping with ``velocity_depth="horizontal"``
    (the horizontal cell-constant depth clips it). In slopingPretty the
    project's shallow-reduction and depth-weighting flags are passed
    unchanged, because that depth is the mask; :func:`velocity_plan` only
    returns a plan for options the engine is qualified with. WSE/Depth from
    this call are discarded: they come from :func:`map_plan`, so a velocity
    refusal never costs them. Decide with :func:`velocity_plan` first.
    """
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
    if _check_scope(RasFim, plan_hdf, _render_storage_areas(render)):
        raise FastMapUnavailable("velocity is not qualified with storage-area surfaces")
    horizontal = velocity_depth == "horizontal"
    mode = "sloping" if horizontal else render["mode"]
    pretty = mode == "slopingPretty"
    extra: dict[str, Any] = {}
    if velocity_depth != "rendered" or _has_kwargs(RasFim.map_sloping_profiles, "velocity_depth"):
        extra["velocity_depth"] = velocity_depth
    if _product_writer_kwarg():
        extra["product_writer"] = "rasterio"  # native delivery excludes velocity
    work = Path(tempfile.mkdtemp(prefix=".fast-map-velocity-", dir=output_dir))
    try:
        try:
            manifest = RasFim.map_sloping_profiles(
                plan_hdf, terrain_tif, work,
                profiles={"Max": "Max"},
                render_mode=mode,
                reduce_shallow_to_horizontal=bool(
                    pretty and render.get("reduce_shallow_to_horizontal")
                ),
                use_depth_weighted_faces=bool(
                    pretty and render.get("use_depth_weighted_faces")
                ),
                velocity=True,
                **({"terrain_hdf": terrain_hdf} if terrain_hdf is not None else {}),
                **extra,
                **_invalid_cells_kwargs(RasFim.map_sloping_profiles, invalid_cells),
                max_working_mib=(
                    default_working_mib() if max_working_mib is None else int(max_working_mib)
                ),
                threads=default_threads() if threads is None else int(threads),
                writer_threads=writer_threads,
                timeout=timeout,
                write_floodplain=False,
            )
        except FimUnsupportedError as exc:
            raise FastMapUnavailable(str(exc)) from exc
        record = _invalid_cells_record(manifest)
        row = manifest.iloc[0]
        if row["status"] not in _COMPLETE_STATUSES:
            raise RuntimeError(f"fast-map velocity {row['status']}: {row.get('error')}")
        source = row.get("velocity_tif")
        if not source or not Path(source).is_file():
            raise RuntimeError("fast-map reported no velocity product")
        target = output_dir / f"Velocity ({profile}).tif"
        os.replace(source, target)
        result = FastMapProducts({FAST_MAP_VELOCITY: [target]})
        result.invalid_cells = record
        result.product_writer = "rasterio"
        engine_manifest = work / "fim_manifest.json"
        details = (
            json.loads(engine_manifest.read_text(encoding="utf-8"))
            if engine_manifest.is_file() else {}
        )
        if record is not None:
            details.setdefault("invalid_cells", record)
        details["ras2cng"] = {
            "plan_hdf": str(plan_hdf),
            "terrain": str(terrain_tif),
            "terrain_hdf": str(terrain_hdf) if terrain_hdf is not None else None,
            "terrain_kind": terrain_kind,
            "render": render,
            "velocity_depth": velocity_depth,
            "engine_render_mode": mode,
            "sloping_pretty_options": list(_pretty_options(render)) if pretty else None,
            "with_faces_method": WITH_FACES_METHOD if pretty and with_faces_options() is not None else None,
            "invalid_cells_policy": normalize_invalid_cells(invalid_cells),
            "products": {FAST_MAP_VELOCITY: [str(target)]},
        }
        (output_dir / FAST_MAP_VELOCITY_MANIFEST).write_text(
            json.dumps(details, indent=2, default=str), encoding="utf-8"
        )
        return result
    finally:
        shutil.rmtree(work, ignore_errors=True)


def derived_plan(render: dict[str, Any], enabled: bool = True) -> Optional[str]:
    """``None`` when fast-map maps Max D x V / D x V^2 / Froude, else the reason."""
    if not enabled:
        return "disabled (--no-fast-map-derived)"
    if render.get("storage_areas"):
        return "D x V / Froude need velocity, which is not qualified with storage areas"
    if not derived_max_supported():
        return (
            "the installed fast-map has no Max derived products "
            f"({DERIVED_MAX_METHOD}, RasFim.map_derived_max)"
        )
    if render["mode"] == "slopingPretty":
        reason = sloping_pretty_products_reason(render)
        if reason is not None:
            return f"Max D x V / Froude in slopingPretty: {reason}"
        return None
    if render["mode"] not in FAST_MAP_DERIVED_RENDER_MODES:
        return (
            f"Max D x V / Froude are qualified in horizontal, plain sloping and "
            f"slopingPretty, not {render['mode']!r}"
        )
    return None


def map_derived(
    plan_hdf: Path,
    terrain_tif: Path,
    output_dir: Path,
    *,
    map_types: list[str],
    render: dict[str, Any],
    profile: str = "Max",
    threads: Optional[int] = None,
    timeout: Optional[float] = None,
    max_working_mib: Optional[int] = None,
    terrain_hdf: Optional[Path] = None,
    terrain_kind: Optional[str] = None,
    invalid_cells: Optional[str] = None,
) -> FastMapProducts:
    """Map RAS Mapper's Max D x V, D x V^2 and Froude in one Rust time-series pass.

    RAS Mapper's Max of these is, per pixel, the maximum over every output
    time of that time's product (``RasFim.map_derived_max``, engine
    ``reduce_max/1``), not Max depth x Max velocity; its valid area is the
    union over times. Named as RAS Mapper names them (``D _ V (Max).tif``,
    ``D _ V^2 (Max).tif``, ``Froude (Max).tif``). Decide with
    :func:`derived_plan` first.
    """
    requested = [t for t in FAST_MAP_DERIVED_TYPES if t in map_types]
    if not requested:
        return FastMapProducts()
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
    if _check_scope(RasFim, plan_hdf, _render_storage_areas(render)):
        raise FastMapUnavailable("D x V / Froude are not qualified with storage-area surfaces")
    velocity_depth = "horizontal" if render["mode"] == "horizontal" else "rendered"
    pretty = render["mode"] == "slopingPretty"
    # slopingPretty: the project's options are the mask (see map_velocity).
    pretty_kwargs: dict[str, Any] = {}
    if pretty:
        if not _has_kwargs(RasFim.map_derived_max, *SLOPING_PRETTY_OPTIONS):
            raise FastMapUnavailable(
                "the installed RasFim.map_derived_max does not take slopingPretty options"
            )
        pretty_kwargs = {
            name: bool(render.get(name)) for name in SLOPING_PRETTY_OPTIONS
        }
    budget = default_working_mib() if max_working_mib is None else int(max_working_mib)
    work = Path(tempfile.mkdtemp(prefix=".fast-map-derived-", dir=output_dir))
    try:
        try:
            frame = RasFim.map_derived_max(
                plan_hdf, terrain_tif, work,
                products=tuple(FAST_MAP_DERIVED_TYPES[t] for t in requested),
                render_mode="slopingPretty" if pretty else "sloping",
                velocity_depth=velocity_depth,
                **pretty_kwargs,
                threads=default_threads() if threads is None else int(threads),
                max_working_mib=budget,
                timeout=timeout,
                **(
                    {"max_inflight_profiles": DERIVED_MAX_INFLIGHT}
                    if _has_kwargs(RasFim.map_derived_max, "max_inflight_profiles") else {}
                ),
                **({"terrain_hdf": terrain_hdf} if terrain_hdf is not None else {}),
                **_invalid_cells_kwargs(RasFim.map_derived_max, invalid_cells),
            )
        except FimUnsupportedError as exc:
            raise FastMapUnavailable(str(exc)) from exc
        record = _invalid_cells_record(frame)
        rows = {str(r["product"]): r for _, r in frame.iterrows()}
        result = FastMapProducts()
        result.invalid_cells = record
        for name in requested:
            row = rows.get(FAST_MAP_DERIVED_TYPES[name])
            source = None if row is None else row.get("tif")
            if not source or not Path(source).is_file():
                raise RuntimeError(f"fast-map reported no Max {name} product")
            target = output_dir / f"{FAST_MAP_DERIVED_LABELS[name]} ({profile}).tif"
            os.replace(source, target)
            result[name] = [target]

        def _plain(value):
            return value.item() if hasattr(value, "item") else value

        details = {
            "method": (
                "fast-map Max derived products (reduce_max/1), qualified against "
                "RAS Mapper stored maps"
            ),
            "plan_hdf": str(plan_hdf),
            "terrain": str(terrain_tif),
            "terrain_hdf": str(terrain_hdf) if terrain_hdf is not None else None,
            "terrain_kind": terrain_kind,
            "render": render,
            "velocity_depth": velocity_depth,
            "engine_render_mode": "slopingPretty" if pretty else "sloping",
            "sloping_pretty_options": list(_pretty_options(render)) if pretty else None,
            "with_faces_method": WITH_FACES_METHOD if pretty else None,
            "max_derived": frame.attrs.get("max_derived"),
            "invalid_cells_policy": normalize_invalid_cells(invalid_cells),
            "invalid_cells": record,
            "products": {
                name: {
                    "path": str(result[name][0]),
                    "valid_pixels": _plain(rows[FAST_MAP_DERIVED_TYPES[name]].get("valid_pixels")),
                }
                for name in requested
            },
        }
        (output_dir / FAST_MAP_DERIVED_MANIFEST).write_text(
            json.dumps(details, indent=2, default=str), encoding="utf-8"
        )
        return result
    finally:
        shutil.rmtree(work, ignore_errors=True)


def shear_plan(
    render: dict[str, Any], enabled: bool = True
) -> tuple[Optional[str], Optional[str]]:
    """Decide whether fast-map maps Max shear stress for this plan.

    Returns ``(shear_depth, None)`` to map it (``"rendered"`` in plain
    sloping, ``"horizontal"`` in horizontal mode), or ``(None, reason)`` to
    leave it to RAS Mapper. Checked before any terrain work.
    """
    if not enabled:
        return None, "disabled (--no-fast-map-shear)"
    if render.get("storage_areas"):
        return None, "shear stress is not qualified with storage-area surfaces"
    mode = render["mode"]
    if mode not in FAST_MAP_SHEAR_RENDER_MODES:
        return None, (
            f"Max shear stress is qualified in horizontal and plain sloping, not {mode!r}"
        )
    if not shear_supported():
        return None, (
            f"the installed fast-map has no Max shear stress ({SHEAR_STRESS_METHOD}, "
            "RasFim.supports_shear_stress)"
        )
    depth = "horizontal" if mode == "horizontal" else "rendered"
    if depth not in (engine_method("sloping").get("shear_stress_depth") or ()):
        return None, f"the installed fast-map does not clip shear stress by the {depth} depth"
    return depth, None


def map_shear(
    plan_hdf: Path,
    terrain_tif: Path,
    output_dir: Path,
    *,
    render: dict[str, Any],
    shear_depth: str = "rendered",
    profile: str = "Max",
    threads: Optional[int] = None,
    writer_threads: int = 2,
    timeout: Optional[float] = None,
    max_working_mib: Optional[int] = None,
    terrain_hdf: Optional[Path] = None,
    terrain_kind: Optional[str] = None,
    invalid_cells: Optional[str] = None,
) -> FastMapProducts:
    """Map one plan's Max shear stress with the Rust engine.

    Writes ``Shear Stress (Max).tif``, as RAS Mapper names it, and
    ``fast_map_shear_manifest.json``. RAS Mapper's Max shear stress is a face
    quantity: each face keeps its largest shear over every stored output
    time, and those face values are interpolated like plain sloping whatever
    the render mode; the active mode's Max depth is the mask. So both modes
    render plain sloping here, and horizontal clips with
    ``shear_depth="horizontal"``. Qualified against RAS Mapper 7.0.1 stored
    maps on bridges p05 and Davis p02 in both modes (0 mask and 0 bit
    differences). It is its own engine call because the engine refuses shear
    together with native GeoTIFF delivery, and so that a shear refusal never
    costs WSE/Depth, whose rasters from this call are discarded. Decide with
    :func:`shear_plan` first.
    """
    if profile != "Max":
        raise FastMapUnavailable(
            f"fast-map maps the Max profile only through ras2cng (got {profile!r})"
        )
    try:
        from ras_commander import RasFim
        from ras_commander.fim.errors import FimUnsupportedError
    except ImportError as exc:
        raise FastMapUnavailable(f"ras-commander has no RasFim: {exc}") from exc
    if not shear_supported():
        raise FastMapUnavailable(
            f"the installed fast-map has no Max shear stress ({SHEAR_STRESS_METHOD})"
        )
    plan_hdf = Path(plan_hdf)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    if _check_scope(RasFim, plan_hdf, _render_storage_areas(render)):
        raise FastMapUnavailable("shear stress is not qualified with storage-area surfaces")
    work = Path(tempfile.mkdtemp(prefix=".fast-map-shear-", dir=output_dir))
    try:
        try:
            manifest = RasFim.map_sloping_profiles(
                plan_hdf, terrain_tif, work,
                profiles={"Max": "Max"},
                render_mode="sloping",
                reduce_shallow_to_horizontal=False,
                use_depth_weighted_faces=False,
                shear_stress=True,
                shear_depth=shear_depth,
                # The engine refuses shear with native GeoTIFF delivery.
                product_writer="rasterio",
                **({"terrain_hdf": terrain_hdf} if terrain_hdf is not None else {}),
                **_invalid_cells_kwargs(RasFim.map_sloping_profiles, invalid_cells),
                max_working_mib=(
                    default_working_mib() if max_working_mib is None else int(max_working_mib)
                ),
                threads=default_threads() if threads is None else int(threads),
                writer_threads=writer_threads,
                timeout=timeout,
                write_floodplain=False,
            )
        except FimUnsupportedError as exc:
            raise FastMapUnavailable(str(exc)) from exc
        record = _invalid_cells_record(manifest)
        row = manifest.iloc[0]
        if row["status"] not in _COMPLETE_STATUSES:
            raise RuntimeError(f"fast-map shear stress {row['status']}: {row.get('error')}")
        source = row.get("shear_stress_tif")
        if not source or not Path(source).is_file():
            raise RuntimeError("fast-map reported no shear stress product")
        target = output_dir / f"Shear Stress ({profile}).tif"
        os.replace(source, target)
        result = FastMapProducts({FAST_MAP_SHEAR: [target]})
        result.invalid_cells = record
        result.product_writer = "rasterio"
        engine_manifest = work / "fim_manifest.json"
        details = (
            json.loads(engine_manifest.read_text(encoding="utf-8"))
            if engine_manifest.is_file() else {}
        )
        if record is not None:
            details.setdefault("invalid_cells", record)
        details["ras2cng"] = {
            "method": (
                f"fast-map Max shear stress ({SHEAR_STRESS_METHOD}), qualified against "
                "RAS Mapper stored maps"
            ),
            "plan_hdf": str(plan_hdf),
            "terrain": str(terrain_tif),
            "terrain_hdf": str(terrain_hdf) if terrain_hdf is not None else None,
            "terrain_kind": terrain_kind,
            "render": render,
            "shear_depth": shear_depth,
            "engine_render_mode": "sloping",
            "shear_stress": getattr(manifest, "attrs", {}).get("shear_stress"),
            "invalid_cells_policy": normalize_invalid_cells(invalid_cells),
            "products": {FAST_MAP_SHEAR: [str(target)]},
        }
        (output_dir / FAST_MAP_SHEAR_MANIFEST).write_text(
            json.dumps(details, indent=2, default=str), encoding="utf-8"
        )
        return result
    finally:
        shutil.rmtree(work, ignore_errors=True)


def whole_simulation_types(map_types: list[str], render: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Split whole-simulation types into ``(fast-map, reasons for the rest)``.

    Arrival time and percent inundated always qualify (with storage areas
    too, on an engine that draws them). Duration needs the engine's
    qualified recession rule (:func:`duration_supported`) and no storage
    areas (the engine refuses that combination).
    """
    types = [t for t in map_types if t in FAST_MAP_WHOLE_SIMULATION_TYPES]
    notes: list[str] = []
    if render["mode"] not in FAST_MAP_WHOLE_SIMULATION_RENDER_MODES:
        # Decided here: raised inside with_terrain it would read as a terrain
        # refusal and walk on to the effective terrain and the export.
        wanted = types + ([FAST_MAP_DURATION] if FAST_MAP_DURATION in map_types else [])
        if wanted:
            notes.append(
                f"{', '.join(wanted)}: qualified for horizontal and plain sloping, "
                f"not {render['mode']!r}"
            )
        return [], notes
    if FAST_MAP_DURATION in map_types:
        if render.get("storage_areas"):
            notes.append("duration is not implemented with storage-area surfaces")
        elif not duration_supported():
            notes.append(
                f"the installed fast-map does not qualify duration ({DURATION_RULE})"
            )
        else:
            types.append(FAST_MAP_DURATION)
    return types, notes


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
    invalid_cells: Optional[str] = None,
) -> FastMapProducts:
    """Map arrival time and percent time inundated with the Rust engine.

    Qualified against RAS Mapper 7.0.1 stored maps (fast-map #12, ras-commander
    #724): identical values and masks on Davis p02 and Bald Eagle bridges p05
    at a 0.5 ft threshold, in horizontal and plain sloping (RAS Mapper draws
    the same products in both). Wet means depth > ``threshold`` (model
    vertical units; ras2cng's ``arrival_depth``); arrival time is in hours
    since the simulation start. Products are named as RAS Mapper names them
    (``Arrival Time (0.5ft hrs).tif``, ``Percent Time Inundated (0.5ft).tif``).
    Duration (``Duration (0.5ft hrs).tif``) is produced only when the engine
    advertises RAS Mapper's recession rule (:data:`DURATION_RULE`, fast-map
    #20: 0 differences on Davis p02 and bridges p05) and the plan has no
    storage areas; otherwise it is left out (RAS Mapper maps it). Storage
    areas are drawn as level pools when the engine supports them.
    ``invalid_cells`` is applied and recorded as in :func:`map_plan`.
    """
    requested = [t for t in map_types if t in FAST_MAP_WHOLE_SIMULATION_TYPES]
    want_duration = FAST_MAP_DURATION in map_types and duration_supported()
    if want_duration:
        requested.append(FAST_MAP_DURATION)
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
    storage_areas = _check_scope(RasFim, plan_hdf, _render_storage_areas(render))
    if storage_areas and want_duration:
        # The engine refuses duration with level pools; RAS Mapper maps it.
        requested.remove(FAST_MAP_DURATION)
        if not requested:
            return {}
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
                **({"storage_areas": True} if storage_areas else {}),
                **_invalid_cells_kwargs(RasFim.map_whole_simulation, invalid_cells),
            )
        except FimUnsupportedError as exc:
            raise FastMapUnavailable(str(exc)) from exc
        record = _invalid_cells_record(manifest)

        unit = plan_units(plan_hdf)
        label = f"{float(threshold):g}{unit}"
        names = {
            "arrival_time": f"Arrival Time ({label} hrs).tif",
            "percent_inundated": f"Percent Time Inundated ({label}).tif",
            FAST_MAP_DURATION: f"Duration ({label} hrs).tif",
        }
        rows = {str(r["product"]): r for _, r in manifest.iterrows()}
        result = FastMapProducts()
        result.invalid_cells = record
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
            "storage_areas": storage_areas,
            "storage_areas_opt_in": _render_storage_areas(render),
            "duration_rule": DURATION_RULE if FAST_MAP_DURATION in requested else None,
            "invalid_cells_policy": normalize_invalid_cells(invalid_cells),
            "invalid_cells": record,
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
    invalid_cells: Optional[str] = None,
    terrain_crs_fnv1a64: Optional[str] = None,
    method: Optional[str] = None,
) -> FastMapProducts:
    """Map one plan's Max inundation boundary with the Rust engine.

    Writes ``Inundation Boundary (Max).geojson`` (a MultiPolygon with holes
    in the model CRS) and ``fast_map_boundary_manifest.json``. A plan with no
    flooded area returns an empty list. Raises :class:`FastMapUnavailable`
    for anything outside the boundary method's scope. ``invalid_cells`` is
    passed and recorded when the installed boundary API takes it.

    ``method`` (``None`` reads ``RAS2CNG_FAST_MAP_BOUNDARY_METHOD``):

    * ``"deterministic"`` (default): ``deterministic-zero-depth-contours/1``.
      Byte-identical across runs, platforms and worker counts; close to, not
      identical with, RAS Mapper's polygons. Horizontal or plain sloping,
      single-source terrain, no storage areas.
    * ``"native-serial"`` (opt-in): the engine's
      ``rasmapper-serial-zero-depth-contours/1`` contract, requested with
      ``allow_unqualified=True`` because the engine and ras-commander keep it
      labelled unqualified. Plain sloping only here; ``terrain_tif`` is the
      first original terrain source and every source is used. Storage areas
      are included when the caller opted in to them. What ras-commander's
      mapping guide reports for it, and all that may be claimed: exact valid
      geometry and unrounded coordinates (ring start, direction and part
      order aside) against RAS Mapper 6.6 / 7.0.1 boundaries exported with
      RAS Mapper's serial polygon loop, on Windows x64, on the named fixtures
      (Baxter steady profiles, Davis Max and one saved time on 7.0.1, Bald
      Eagle Max and one saved time with two terrain sources and storage,
      Reese mixed-model Max). RAS Mapper's default export is not serial and
      its polygons vary between identical runs, so equality with an ordinary
      RAS Mapper shapefile is not claimed, nor is any other model, profile,
      platform or a modified terrain.

    ``terrain_crs_fnv1a64`` is the engine's WKT hash of ``terrain_tif``,
    supplied and verified by the user (see :func:`terrain_crs_hint`). The
    engine accepts it only when it cannot name an EPSG code itself and the
    hash matches exactly; it is ignored when the engine reports an EPSG code.
    Without it, a CRS identity refusal raises :class:`FastMapUnavailable`
    whose message gives the hash the engine reports.
    """
    terrain_crs_fnv1a64 = normalize_crs_fnv1a64(terrain_crs_fnv1a64)
    method = resolve_boundary_method(method)
    native = method == BOUNDARY_NATIVE_SERIAL
    if profile != "Max":
        raise FastMapUnavailable(
            f"fast-map maps the Max profile only through ras2cng (got {profile!r})"
        )
    if native and render["mode"] not in FAST_MAP_NATIVE_BOUNDARY_RENDER_MODES:
        raise FastMapUnavailable(
            "native-serial inundation boundaries are checked for plain sloping only, "
            f"not {render['mode']!r}"
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
            "(ras-commander #755 with ras-commander-fast-map #5)"
        )

    plan_hdf = Path(plan_hdf)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    if native and not native_boundary_supported():
        raise FastMapUnavailable(
            "native-serial inundation boundaries need ras-commander>=0.116 and "
            f"ras-commander-fast-map>=0.15 ({NATIVE_BOUNDARY_METHOD})"
        )
    storage_areas = _check_scope(RasFim, plan_hdf, _render_storage_areas(render))
    if storage_areas and not native:
        # The contour method draws 2D cells only; a level pool would be missing.
        raise FastMapUnavailable(
            "deterministic Rust inundation boundaries do not include storage-area "
            "surfaces (--fast-map-boundary-method native-serial does)"
        )
    method_kwargs: dict[str, Any] = (
        {"method": "native_serial", "allow_unqualified": True} if native else {}
    )
    budget = default_working_mib() if max_working_mib is None else int(max_working_mib)
    crs_kwargs: dict[str, Any] = {}
    if terrain_crs_fnv1a64 is not None:
        if not _has_kwargs(RasFim.map_inundation_boundaries, "terrain_crs_fnv1a64"):
            raise FastMapUnavailable(
                "the installed RasFim.map_inundation_boundaries does not take "
                "terrain_crs_fnv1a64; upgrade ras-commander"
            )
        crs_kwargs["terrain_crs_fnv1a64"] = terrain_crs_fnv1a64

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
                **_invalid_cells_kwargs(RasFim.map_inundation_boundaries, invalid_cells),
                **crs_kwargs,
                **method_kwargs,
            )
        except FimUnsupportedError as exc:
            message = str(exc)
            if terrain_crs_fnv1a64 is None and _crs_refusal(message):
                hint = terrain_crs_hint(Path(terrain_tif))
                if hint:
                    message = f"{message}; {hint}"
            raise FastMapUnavailable(message) from exc
        record = _invalid_cells_record(manifest)

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
            "boundary_method": method,
            "storage_areas": storage_areas,
            "method": (
                f"{NATIVE_BOUNDARY_METHOD} (opt-in; labelled unqualified by the engine; "
                "exact against RAS Mapper's serial boundary export only on the fixtures "
                "in the ras-commander mapping guide)"
                if native else
                f"{DETERMINISTIC_BOUNDARY_METHOD} (not RAS Mapper vector parity)"
            ),
            "statistics": {
                key: _plain(row.get(key))
                for key in ("polygons", "holes", "vertices", "area", "crs")
            },
            "products": [str(path) for path in products],
            "invalid_cells_policy": normalize_invalid_cells(invalid_cells),
            # User-supplied engine WKT hash (None: not supplied).
            "terrain_crs_fnv1a64": terrain_crs_fnv1a64,
        }
        if record is not None:
            details.setdefault("invalid_cells", record)
        (output_dir / FAST_MAP_BOUNDARY_MANIFEST).write_text(
            json.dumps(details, indent=2, default=str), encoding="utf-8"
        )
        result = FastMapProducts({FAST_MAP_BOUNDARY: products})
        result.invalid_cells = record
        return result
    finally:
        shutil.rmtree(work, ignore_errors=True)
