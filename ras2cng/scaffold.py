"""
Barebones HEC-RAS project synthesis from a computed plan HDF.

Enables map generation from minimal inputs (plan HDF + terrain) without the
original HEC-RAS project. The plan HDF carries everything a project scaffold
needs: projection WKT, units, plan/project titles, plan ShortID, and a full
geometry copy. RASMapper's stored-map engine only consumes a .rasmap file and
the plan HDF, so an ~11-line synthesized project satisfies ras-commander's
``init_ras_project`` and the full ``generate_result_maps()`` pipeline.

Provides:
- read_plan_hdf_metadata(): Extract scaffold metadata from a plan HDF
- build_scaffold(): Synthesize a barebones project around a plan HDF, building
  the HEC-RAS terrain from raw TIFFs (RasProcess.exe CreateTerrain) or reusing
  a pre-built terrain HDF sidecar set
"""

from __future__ import annotations

import json
import os
import re
import shutil
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from rich.console import Console

console = Console()

SCAFFOLD_MARKER = ".ras2cng-scaffold"


@dataclass
class PlanHdfMetadata:
    """Metadata extracted from a computed plan HDF, sufficient to scaffold a project."""
    project_name: str          # "Muncie" (from Plan Filename attr)
    plan_number: str           # "01"
    plan_title: str
    plan_short_id: str         # RASMapper output folder name
    project_title: str
    geom_ext: str              # "g04" (referenced file need not exist)
    flow_ext: str              # "u01"
    units: str                 # "US Customary" | "SI Units"
    projection_wkt: Optional[str]
    sim_start: str             # "01Jan1999 12:00:00"
    sim_end: str
    file_version: str


@dataclass
class ScaffoldInfo:
    """Result of build_scaffold()."""
    project_dir: Path
    prj_file: Path
    plan_hdf: Path             # canonical path inside the scaffold
    terrain_hdf: Path
    meta: PlanHdfMetadata


def read_plan_hdf_metadata(plan_hdf: Path) -> PlanHdfMetadata:
    """Extract scaffold metadata from a computed plan HDF.

    Args:
        plan_hdf: Path to a HEC-RAS results HDF (*.pNN.hdf)

    Returns:
        PlanHdfMetadata

    Raises:
        ValueError: If the file is not a computed plan results HDF or required
            attributes are missing/unparseable.
    """
    import h5py

    plan_hdf = Path(plan_hdf)

    def _dec(v) -> str:
        return v.decode() if isinstance(v, bytes) else str(v)

    with h5py.File(plan_hdf, "r") as f:
        file_type = _dec(f.attrs.get("File Type", b""))
        if file_type != "HEC-RAS Results":
            raise ValueError(
                f"{plan_hdf.name}: not a HEC-RAS results HDF "
                f"(File Type={file_type!r}). Pass a computed plan HDF (*.pNN.hdf)."
            )
        if "Results" not in f:
            raise ValueError(
                f"{plan_hdf.name}: no Results group — plan has not been computed."
            )
        if "Plan Data/Plan Information" not in f:
            raise ValueError(f"{plan_hdf.name}: missing Plan Data/Plan Information group.")

        attrs = {k: _dec(v) for k, v in f["Plan Data/Plan Information"].attrs.items()}
        units = _dec(f.attrs.get("Units System", b"US Customary"))
        projection = f.attrs.get("Projection")
        projection_wkt = _dec(projection) if projection is not None else None
        file_version = _dec(f.attrs.get("File Version", b""))

    # "Plan Filename" may carry a foreign absolute path from the compute machine
    plan_file_attr = attrs.get("Plan Filename", "")
    stem = Path(plan_file_attr.replace("\\", "/")).name
    m = re.match(r"(.+)\.p(\d{2})$", stem)
    if not m:
        # Fall back to the input filename if it follows the convention
        m = re.match(r"(.+)\.p(\d{2})\.hdf$", plan_hdf.name, re.IGNORECASE)
        if not m:
            raise ValueError(
                f"{plan_hdf.name}: cannot determine project name / plan number "
                f"(Plan Filename attr={plan_file_attr!r}). Rename the file to "
                f"<Project>.pNN.hdf or supply a properly attributed HDF."
            )
    project_name, plan_number = m.group(1), m.group(2)

    def _ext(key: str, default: str) -> str:
        value = attrs.get(key, "")
        suffix = Path(value.replace("\\", "/")).name.split(".")[-1] if value else ""
        return suffix if re.match(r"[gu]\d{2}$", suffix) else default

    return PlanHdfMetadata(
        project_name=project_name,
        plan_number=plan_number,
        plan_title=attrs.get("Plan Title", project_name),
        plan_short_id=attrs.get("Plan ShortID", f"Plan_{plan_number}"),
        project_title=attrs.get("Project Title", project_name),
        geom_ext=_ext("Geometry Filename", "g01"),
        flow_ext=_ext("Flow Filename", "u01"),
        units=units,
        projection_wkt=projection_wkt,
        sim_start=attrs.get("Simulation Start Time", "01Jan2000 00:00:00"),
        sim_end=attrs.get("Simulation End Time", "02Jan2000 00:00:00"),
        file_version=file_version,
    )


def build_scaffold(
    plan_hdf: Path,
    workdir: Path,
    *,
    terrain_tifs: Optional[list[Path]] = None,
    terrain_hdf: Optional[Path] = None,
    projection_file: Optional[Path] = None,
    render_mode: str = "sloping",
    ras_version: str = "6.6",
) -> ScaffoldInfo:
    """Synthesize a barebones HEC-RAS project around a plan HDF.

    Exactly one of terrain_tifs / terrain_hdf must be provided. Raw TIFFs are
    converted to a HEC-RAS terrain via RasProcess.exe CreateTerrain; a pre-built
    terrain HDF is copied in along with its .vrt and tile TIFF sidecars.

    A previously built scaffold (identified by its marker file) is reused when
    the source plan HDF is unchanged, skipping the expensive terrain build.

    Args:
        plan_hdf: Computed plan results HDF (any filename)
        workdir: Scaffold directory (created; must be empty or a prior scaffold)
        terrain_tifs: Raw terrain GeoTIFF(s) to build into a HEC-RAS terrain
        terrain_hdf: Pre-built HEC-RAS terrain HDF (sidecars must sit beside it)
        projection_file: ESRI .prj overriding the plan HDF projection WKT
        render_mode: Initial rasmap render mode (horizontal/sloping/slopingPretty)
        ras_version: HEC-RAS version for CreateTerrain (default "6.6")

    Returns:
        ScaffoldInfo with project_dir, prj_file, canonical plan_hdf, terrain_hdf
    """
    plan_hdf = Path(plan_hdf)
    workdir = Path(workdir)

    if (terrain_tifs is None) == (terrain_hdf is None):
        raise ValueError("Provide exactly one of terrain_tifs or terrain_hdf.")
    if not plan_hdf.exists():
        raise FileNotFoundError(f"Plan HDF not found: {plan_hdf}")

    meta = read_plan_hdf_metadata(plan_hdf)
    name, num = meta.project_name, meta.plan_number

    marker_path = workdir / SCAFFOLD_MARKER
    if (
        workdir.exists()
        and any(workdir.iterdir())
        and not marker_path.exists()
    ):
        raise ValueError(
            f"Scaffold workdir {workdir} is not empty and is not a previous "
            f"ras2cng scaffold. Choose an empty directory or pass --workdir."
        )

    terrain_sources = [Path(t) for t in (terrain_tifs if terrain_tifs is not None else [terrain_hdf])]
    missing_terrain = [p for p in terrain_sources if not p.exists()]
    if missing_terrain:
        raise FileNotFoundError(
            "Terrain input(s) not found: " + ", ".join(str(p) for p in missing_terrain)
        )

    # Per-input signatures decide what a rerun may reuse: the plan HDF and the
    # terrain are invalidated independently, so recomputing a plan does not
    # discard an expensive terrain build (and vice versa).
    hdf_sig = _file_sig(plan_hdf)
    terrain_sig = [_file_sig(p) for p in terrain_sources]

    if workdir.exists() and marker_path.exists():
        try:
            prior = json.loads(marker_path.read_text())
        except (json.JSONDecodeError, OSError):
            prior = {}

        terrain_same = prior.get("terrain_sig") == terrain_sig
        hdf_same = prior.get("hdf_sig") == hdf_sig
        if not terrain_same:
            shutil.rmtree(workdir / "Terrain", ignore_errors=True)
        if not hdf_same:
            # Project name may differ with a new HDF — clear everything except
            # a still-valid terrain (stubs and rasmap are rewritten below).
            for item in workdir.iterdir():
                if item.name == SCAFFOLD_MARKER:
                    continue
                if item.name == "Terrain" and terrain_same:
                    continue
                if item.is_dir():
                    shutil.rmtree(item, ignore_errors=True)
                else:
                    item.unlink(missing_ok=True)

    terrain_dir = workdir / "Terrain"
    terrain_dir.mkdir(parents=True, exist_ok=True)

    # Marker is written BEFORE the expensive steps so a failed/interrupted
    # build leaves a recognizable scaffold that a rerun can safely resume or
    # rebuild (existence guards below decide what actually needs redoing).
    marker_path.write_text(json.dumps({
        "source_hdf": str(plan_hdf.resolve()),
        "hdf_sig": hdf_sig,
        "terrain_sig": terrain_sig,
        "terrain_inputs": [str(t) for t in terrain_sources],
        "ras2cng": _package_version(),
    }, indent=2))

    # --- Plan HDF under canonical name (hardlink if possible, else copy) ---
    canonical_hdf = workdir / f"{name}.p{num}.hdf"
    if not canonical_hdf.exists():
        _link_or_copy(plan_hdf, canonical_hdf, describe="plan HDF")

    # --- Projection (inside Terrain\ to avoid .prj collision with the project file) ---
    projection_prj = terrain_dir / "Projection.prj"
    if projection_file is not None:
        shutil.copy2(projection_file, projection_prj)
    elif meta.projection_wkt:
        projection_prj.write_text(meta.projection_wkt)
    else:
        raise ValueError(
            f"{plan_hdf.name} has no Projection attribute; pass projection_file "
            f"(--projection) with the ESRI WKT .prj for this model."
        )

    # --- Terrain ---
    terrain_units = "Feet" if meta.units == "US Customary" else "Meters"
    if terrain_hdf is not None:
        scaffold_terrain_hdf = _import_terrain_sidecar(Path(terrain_hdf), terrain_dir)
    else:
        scaffold_terrain_hdf = terrain_dir / "Terrain.hdf"
        if not scaffold_terrain_hdf.exists():
            from ras_commander import RasTerrain

            console.print(f"  Building HEC-RAS terrain from {len(terrain_tifs)} raster(s)...")
            result = RasTerrain.create_terrain_from_rasters(
                [str(t) for t in terrain_tifs],
                str(terrain_dir),
                terrain_name="Terrain",
                units=terrain_units,
                stitch=True,
                hecras_version=ras_version,
                generate_prj=False,  # we already wrote Projection.prj from the plan HDF
            )
            scaffold_terrain_hdf = Path(result)
        else:
            console.print("  Reusing terrain from previous scaffold")

    # --- Project + plan text stubs (content entirely from plan HDF attrs) ---
    units_line = "English Units" if meta.units == "US Customary" else "SI Units"
    (workdir / f"{name}.prj").write_text(
        f"Proj Title={meta.project_title}\n"
        f"Current Plan=p{num}\n"
        f"{units_line}\n"
        f"Geom File={meta.geom_ext}\n"
        f"Unsteady File={meta.flow_ext}\n"
        f"Plan File=p{num}\n"
    )
    (workdir / f"{name}.p{num}").write_text(
        f"Plan Title={meta.plan_title}\n"
        f"Short Identifier={meta.plan_short_id}\n"
        f"Simulation Date={_ras_date(meta.sim_start)},{_ras_date(meta.sim_end)}\n"
        f"Geom File={meta.geom_ext}\n"
        f"Flow File={meta.flow_ext}\n"
    )
    # 1-line flow/geometry stubs: silence harmless-but-noisy RasPrj missing-file errors
    (workdir / f"{name}.{meta.flow_ext}").write_text(f"Flow Title={meta.plan_title}\n")
    (workdir / f"{name}.{meta.geom_ext}").write_text(f"Geom Title={meta.plan_title}\n")

    _write_rasmap(
        workdir / f"{name}.rasmap",
        terrain_hdf_name=scaffold_terrain_hdf.name,
        units=meta.units,
        render_mode=render_mode,
    )

    return ScaffoldInfo(
        project_dir=workdir,
        prj_file=workdir / f"{name}.prj",
        plan_hdf=canonical_hdf,
        terrain_hdf=scaffold_terrain_hdf,
        meta=meta,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _ras_date(timestamp: str) -> str:
    """'01Jan1999 12:00:00' -> '01JAN1999,1200' (HEC-RAS Simulation Date format)."""
    date_part, _, time_part = timestamp.partition(" ")
    return f"{date_part.upper()},{time_part.replace(':', '')[:4] or '0000'}"


def _package_version() -> str:
    try:
        from ras2cng import __version__
        return __version__
    except ImportError:
        return "unknown"


def _file_sig(path: Path) -> list:
    """Identity signature for scaffold-reuse decisions: path + mtime + size.

    Stored as a list (not tuple) so it round-trips through the JSON marker.
    """
    stat = path.stat()
    return [str(path.resolve()), stat.st_mtime_ns, stat.st_size]


def _link_or_copy(src: Path, dst: Path, describe: str = "file") -> None:
    """Hardlink src to dst, falling back to a copy across volumes."""
    try:
        os.link(src, dst)
    except OSError:
        console.print(f"  Copying {describe} ({src.stat().st_size / 1e6:.0f} MB)...")
        shutil.copy2(src, dst)


def terrain_sidecar_files(terrain_hdf: Path) -> list[Path]:
    """List the full sidecar file set a HEC-RAS terrain HDF depends on.

    The terrain HDF's /Terrain children each carry a ``File`` attribute naming
    their tile TIFF (bare filename, same directory); a sibling .vrt mosaics them.

    Raises:
        FileNotFoundError: Listing every missing member, if any.
    """
    import h5py

    terrain_hdf = Path(terrain_hdf)
    required = [terrain_hdf.with_suffix(".vrt")]
    with h5py.File(terrain_hdf, "r") as f:
        terrain_group = f.get("Terrain")
        if terrain_group is None:
            raise ValueError(f"{terrain_hdf.name}: no /Terrain group — not a HEC-RAS terrain HDF.")
        for layer_name in terrain_group:
            file_attr = terrain_group[layer_name].attrs.get("File")
            if file_attr is not None:
                tile = file_attr.decode() if isinstance(file_attr, bytes) else str(file_attr)
                required.append(terrain_hdf.parent / tile)

    missing = [p for p in required if not p.exists()]
    if missing:
        raise FileNotFoundError(
            f"Terrain sidecar set incomplete next to {terrain_hdf.name}; missing: "
            + ", ".join(p.name for p in missing)
        )
    return required


def _import_terrain_sidecar(terrain_hdf: Path, terrain_dir: Path) -> Path:
    """Copy/hardlink a pre-built terrain HDF and its sidecar set into the scaffold."""
    if not terrain_hdf.exists():
        raise FileNotFoundError(f"Terrain HDF not found: {terrain_hdf}")

    members = [terrain_hdf] + terrain_sidecar_files(terrain_hdf)
    for src in members:
        dst = terrain_dir / src.name
        if dst.exists():
            continue
        _link_or_copy(src, dst, describe=f"terrain {src.name}")
    return terrain_dir / terrain_hdf.name


def _write_rasmap(
    rasmap_path: Path,
    *,
    terrain_hdf_name: str,
    units: str,
    render_mode: str,
) -> None:
    """Write a minimal .rasmap sufficient for StoreAllMaps.

    The Results element is left empty; RasProcess.store_maps() populates it
    with stored-map entries per requested map type. Rasmap-relative paths use
    backslashes (RASMapper convention), so they are assembled from plain
    strings — never round-tripped through Path, whose POSIX flavor treats
    backslashes as name characters.
    """
    terrain_name = terrain_hdf_name.rsplit(".", 1)[0]

    root = ET.Element("RASMapper")
    ET.SubElement(root, "Version").text = "2.0.0"
    ET.SubElement(root, "RASProjectionFilename",
                  {"Filename": ".\\Terrain\\Projection.prj"})
    ET.SubElement(root, "Results", {"Checked": "True", "Expanded": "True"})
    terrains = ET.SubElement(root, "Terrains", {"Checked": "True", "Expanded": "True"})
    layer = ET.SubElement(terrains, "Layer", {
        "Name": terrain_name,
        "Type": "TerrainLayer",
        "Checked": "True",
        "Filename": f".\\Terrain\\{terrain_hdf_name}",
    })
    ET.SubElement(layer, "ResampleMethod").text = "near"
    ET.SubElement(layer, "Surface", {"On": "True"})
    ET.SubElement(root, "Units").text = units
    ET.SubElement(root, "RenderMode").text = render_mode

    ET.indent(root)
    tree = ET.ElementTree(root)
    tree.write(rasmap_path, encoding="utf-8", xml_declaration=True)
