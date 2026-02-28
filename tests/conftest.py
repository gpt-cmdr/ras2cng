"""
Fixtures for rascmdr-parquet-cli integration tests.

Provides:
- Model discovery across RasExamples, HCFCD M3, and LWI Region 4 sources
- Output directory management on I: drive
- JSON report collection for pass/fail/error/skip tracking
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import pytest


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

OUTPUT_BASE = Path("I:/rascmdr-parquet-testing")
MODELS_DIR = OUTPUT_BASE / "models"
OUTPUTS_DIR = OUTPUT_BASE / "outputs"
REPORTS_DIR = OUTPUT_BASE / "reports"

EXAMPLE_PROJECTS_DIR = MODELS_DIR / "example_projects"
M3_MODELS_DIR = MODELS_DIR / "m3_models"

LWI_REGION4_ROOT = Path("L:/Region_4")
LWI_SUBBASINS = [
    "Lower_Calcasieu",
    "Sabine_Lake",
    "Upper_Calcasieu",
    "West_Fork",
    "Whisky_Chitto",
]

# M3 model IDs (22 watersheds)
M3_MODEL_IDS = list("ABCDEFGHIJKLMNOPQRSTUW")


# ---------------------------------------------------------------------------
# Report collector
# ---------------------------------------------------------------------------

@dataclass
class ReportCollector:
    """Collects per-file, per-layer, per-variable test results."""

    entries: list = field(default_factory=list)
    start_time: float = field(default_factory=time.time)

    def record(
        self,
        status: str,
        source_file: Path,
        layer_or_var: str,
        detail: str = "",
        source: str = "",
    ):
        self.entries.append(
            {
                "status": status,
                "source": source,
                "file": str(source_file),
                "layer_or_variable": layer_or_var,
                "detail": detail,
                "timestamp": time.time(),
            }
        )

    def write_report(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        summary = {
            "total": len(self.entries),
            "pass": sum(1 for e in self.entries if e["status"] == "PASS"),
            "error": sum(1 for e in self.entries if e["status"] == "ERROR"),
            "skip": sum(1 for e in self.entries if e["status"] == "SKIP"),
            "duration_s": round(time.time() - self.start_time, 1),
        }
        report = {"summary": summary, "results": self.entries}
        path.write_text(json.dumps(report, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def output_base():
    """Root output directory on I: drive."""
    OUTPUT_BASE.mkdir(parents=True, exist_ok=True)
    OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
    return OUTPUT_BASE


@pytest.fixture(scope="session")
def report_collector(output_base):
    """Session-scoped report collector; writes JSON on teardown."""
    collector = ReportCollector()
    yield collector
    collector.write_report(REPORTS_DIR / "extraction_report.json")


@pytest.fixture(scope="session")
def ensure_ras_examples():
    """Download and extract all RasExamples projects (cached on I:).

    Returns list of project directory paths.
    """
    from ras_commander import RasExamples

    EXAMPLE_PROJECTS_DIR.mkdir(parents=True, exist_ok=True)

    # Trigger download of the zip if not already present
    RasExamples.get_example_projects()

    project_names = RasExamples.list_projects()
    if not project_names:
        pytest.skip("No RasExamples projects available")

    extracted = []
    for name in project_names:
        try:
            path = RasExamples.extract_project(name, output_path=EXAMPLE_PROJECTS_DIR)
            extracted.append(path)
        except Exception as exc:
            print(f"Warning: Could not extract RasExamples project '{name}': {exc}")

    return extracted


@pytest.fixture(scope="session")
def ensure_m3_models():
    """Download and extract all HCFCD M3 models (cached on I:).

    M3 models are downloaded as zip archives per channel. This fixture also
    unzips the individual HEC-RAS channel zips so that .g01/.p01 files are
    discoverable on disk.

    Returns list of extracted model directory paths.
    """
    import zipfile

    from ras_commander import M3Model

    M3_MODELS_DIR.mkdir(parents=True, exist_ok=True)

    extracted = []
    for model_id in M3_MODEL_IDS:
        try:
            path = M3Model.extract_model(model_id, output_path=M3_MODELS_DIR)
            extracted.append(path)
        except Exception as exc:
            print(f"Warning: Could not extract M3 model '{model_id}': {exc}")

    # Unzip individual channel zips (HEC-RAS/*.zip) so .g01/.p01 are on disk
    for model_dir in extracted:
        ras_dir = model_dir / "HEC-RAS"
        if not ras_dir.exists():
            continue
        for zf in ras_dir.glob("*.zip"):
            target = ras_dir / zf.stem
            if target.exists():
                continue  # already unzipped
            try:
                with zipfile.ZipFile(zf, "r") as z:
                    z.extractall(ras_dir)
            except Exception as exc:
                print(f"Warning: Could not unzip {zf}: {exc}")

    return extracted


# ---------------------------------------------------------------------------
# File discovery helpers (module-level for parametrize)
# ---------------------------------------------------------------------------

# Cache file list on I: to avoid slow rescans of L: drive (~6 min per scan).
# Delete this file to force a rescan.
_FILE_CACHE = OUTPUT_BASE / "reports" / "file_list_cache.json"
_MAX_WALK_DEPTH = 4  # limit recursion depth below each root


def _walk_files(root: Path, max_depth: int = _MAX_WALK_DEPTH) -> list[Path]:
    """Walk directory tree with depth limit, returning all files."""
    import os

    results = []
    if not root.exists():
        return results
    for dirpath, dirnames, filenames in os.walk(root):
        depth = len(Path(dirpath).relative_to(root).parts)
        if depth >= max_depth:
            dirnames.clear()
            continue
        for f in filenames:
            results.append(Path(dirpath) / f)
    return results


def _scan_all_files() -> dict[str, list[str]]:
    """Scan all model sources and return categorised file lists.

    Returns dict with keys: geometry_hdf, text_geometry, plan_hdf.
    Each value is a sorted list of absolute path strings.
    """
    all_files: list[Path] = []

    # Fast sources on I:
    for root in [EXAMPLE_PROJECTS_DIR, M3_MODELS_DIR]:
        all_files.extend(_walk_files(root))

    # Slow source on L: — depth-limited
    if LWI_REGION4_ROOT.exists():
        for sub in LWI_SUBBASINS:
            all_files.extend(_walk_files(LWI_REGION4_ROOT / sub))

    geom_hdf = sorted(str(f) for f in all_files if _is_geometry_hdf(f))
    text_geom = sorted(str(f) for f in all_files if _is_text_geometry(f))
    plan_hdf = sorted(str(f) for f in all_files if _is_plan_hdf(f))

    return {
        "geometry_hdf": geom_hdf,
        "text_geometry": text_geom,
        "plan_hdf": plan_hdf,
    }


def _load_file_cache() -> dict[str, list[str]]:
    """Load cached file lists, rescanning if cache is missing."""
    if _FILE_CACHE.exists():
        data = json.loads(_FILE_CACHE.read_text(encoding="utf-8"))
        # Validate cache has expected keys
        if all(k in data for k in ("geometry_hdf", "text_geometry", "plan_hdf")):
            return data

    # Cache miss — scan (slow on first run)
    data = _scan_all_files()
    _FILE_CACHE.parent.mkdir(parents=True, exist_ok=True)
    _FILE_CACHE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return data


def _is_geometry_hdf(p: Path) -> bool:
    """Match *.g??.hdf files."""
    suffixes = [s.lower() for s in p.suffixes]
    return len(suffixes) >= 2 and suffixes[-1] == ".hdf" and suffixes[-2].startswith(".g")


def _is_text_geometry(p: Path) -> bool:
    """Match *.g[0-9][0-9] (not HDF) files."""
    s = p.suffix.lower()
    return bool(re.match(r"\.g\d{2}$", s)) and not _is_geometry_hdf(p)


def _is_plan_hdf(p: Path) -> bool:
    """Match *.p??.hdf files (excluding .tmp.hdf)."""
    suffixes = [s.lower() for s in p.suffixes]
    if ".tmp" in suffixes:
        return False
    return len(suffixes) >= 2 and suffixes[-1] == ".hdf" and suffixes[-2].startswith(".p")


def _source_label(p: Path) -> str:
    """Derive a source label from a file path for reporting."""
    s = str(p).replace("\\", "/")
    if "example_projects" in s:
        return "ras_examples"
    if "m3_models" in s:
        return "m3_models"
    if "Region_4" in s:
        return "lwi_region4"
    return "unknown"


def _output_subdir(p: Path) -> str:
    """Build a relative output subdirectory from the source label and project name."""
    label = _source_label(p)
    if label == "ras_examples":
        parts = p.parts
        try:
            idx = [x.lower() for x in parts].index("example_projects")
            project = parts[idx + 1]
            return f"ras_examples/{project}"
        except (ValueError, IndexError):
            return f"ras_examples/{p.stem}"
    elif label == "m3_models":
        parts = p.parts
        try:
            idx = [x.lower() for x in parts].index("m3_models")
            project = parts[idx + 1]
            return f"m3_models/{project}"
        except (ValueError, IndexError):
            return f"m3_models/{p.stem}"
    elif label == "lwi_region4":
        parts = p.parts
        try:
            idx = [x.lower() for x in parts].index("region_4")
            subbasin = parts[idx + 1]
            return f"lwi_region4/{subbasin}"
        except (ValueError, IndexError):
            return f"lwi_region4/{p.stem}"
    return f"other/{p.stem}"


# -- Cached file lists for parametrize --

_cached = _load_file_cache()


def discover_geometry_hdf_files() -> list[Path]:
    return [Path(p) for p in _cached["geometry_hdf"]]


def discover_text_geometry_files() -> list[Path]:
    return [Path(p) for p in _cached["text_geometry"]]


def discover_plan_hdf_files() -> list[Path]:
    return [Path(p) for p in _cached["plan_hdf"]]


def _file_id(p: Path) -> str:
    """Short test ID from file path: source/project/filename."""
    label = _source_label(p)
    subdir = _output_subdir(p).split("/", 1)[-1] if "/" in _output_subdir(p) else p.parent.name
    return f"{label}/{subdir}/{p.name}"
