"""
Manifest catalog for ras2cng project archives.

Tracks what was extracted, from which source files, and where the outputs live.
Written to manifest.json at the root of every archive directory.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


# Schema version — increment when manifest structure changes
SCHEMA_VERSION = "2.5"


@dataclass
class ManifestLayer:
    """Metadata for one geometry layer inside a consolidated GeoParquet."""
    layer: str
    filter_value: str         # Value of the `layer` column to filter on
    rows: int
    geometry_type: str
    crs: Optional[str] = None
    hilbert_index: str = ""   # Column name when Hilbert indexed
    sort_order: str = ""      # Persisted sort order description


@dataclass
class ManifestGeomEntry:
    """One geometry source and its consolidated GeoParquet file."""
    geom_id: str                            # e.g. "g01"
    source_file: str                        # relative or abs path to original
    file_type: str                          # "hdf", "text", "hdf+text"
    parquet: str = ""                       # relative path from archive root
    plans_using: list[str] = field(default_factory=list)
    layers: list[dict] = field(default_factory=list)
    size_bytes: int = 0

    def add_layer(self, layer: ManifestLayer) -> None:
        self.layers.append(asdict(layer))


@dataclass
class ManifestResultVariable:
    """Metadata for one results variable inside a consolidated GeoParquet."""
    variable: str
    filter_value: str         # Value of the `layer` column to filter on
    rows: int
    parquet: str = ""         # Optional variable-level parquet path
    geometry_mode: str = ""   # polygon, point, or none
    index_column: str = ""    # cell_id or face_id when the result can join geometry
    geometry_filter: str = "" # mesh_cells or mesh_faces for viewer-side joins
    join_columns: dict[str, str] = field(default_factory=dict)  # geometry column -> result column
    profile_column: str = ""  # Split raw rows into one browser layer per profile
    source: str = ""          # Clear provenance for browser result identification
    hilbert_index: str = ""   # Column name when spatially indexed
    join_index: str = ""      # Column name when join-key indexed
    sort_order: str = ""      # Persisted sort order description
    index_status: str = ""    # spatial_join, join_key, skipped, or error
    size_bytes: int = 0


@dataclass
class ManifestPlanEntry:
    """One plan's results export record."""
    plan_id: str
    plan_title: str
    geom_id: str
    flow_id: Optional[str]
    hdf_exists: bool
    completed: bool
    parquet: str = ""                       # relative path from archive root
    variables: list[dict] = field(default_factory=list)
    size_bytes: int = 0
    layout: str = "plan"                    # plan or variable
    geometry_mode: str = "polygon"          # polygon, point, or none

    def add_variable(self, var: ManifestResultVariable) -> None:
        self.variables.append(asdict(var))


@dataclass
class ManifestTerrainEntry:
    """One terrain raster file converted to COG."""
    source_file: str
    cog_file: str
    size_bytes: int
    crs: Optional[str]
    terrain_name: str = ""
    source_files: list[str] = field(default_factory=list)
    target_resolution: Optional[float] = None
    horizontal_units: str = ""
    provenance_file: str = ""
    authoritative: bool = False


@dataclass
class ManifestTerrainModificationEntry:
    """Vector construction layers associated with one named terrain."""

    terrain_name: str
    source_hdf: str
    layers: list[dict] = field(default_factory=list)


@dataclass
class ManifestTerrainSourceEntry:
    """Native TIFF source footprints for one named terrain."""

    terrain_name: str
    layers: list[dict] = field(default_factory=list)


@dataclass
class ManifestMapEntry:
    """Result raster generation record for one plan."""
    plan_id: str
    profile: str
    rasters: list[dict] = field(default_factory=list)  # [{type, file, size_bytes}]
    vectors: list[dict] = field(default_factory=list)
    min_depth: float = 0.0
    reprojected_wgs84: bool = False


@dataclass
class Manifest:
    """Full archive manifest. Written as manifest.json at archive root."""

    project: dict
    project_parquet: Optional[str] = None   # relative path to project metadata parquet
    geometry: list[dict] = field(default_factory=list)
    results: list[dict] = field(default_factory=list)
    terrain: list[dict] = field(default_factory=list)
    terrain_modifications: list[dict] = field(default_factory=list)
    terrain_sources: list[dict] = field(default_factory=list)
    maps: list[dict] = field(default_factory=list)
    postprocessing: dict = field(default_factory=dict)
    schema_version: str = SCHEMA_VERSION

    # -----------------------------------------------------------------------
    # Builder helpers
    # -----------------------------------------------------------------------

    @classmethod
    def create(
        cls,
        project_name: str,
        prj_file: Path,
        source_path: Path,
        archive_path: Path,
        crs: Optional[str] = None,
        units: str = "Unknown",
        plan_count: int = 0,
        geom_count: int = 0,
    ) -> "Manifest":
        """Create a new blank manifest for a project."""
        return cls(
            project={
                "name": project_name,
                "prj_file": str(prj_file.name),
                "source_path": str(source_path.resolve()),
                "archive_path": str(archive_path.resolve()),
                "created_at": datetime.now(timezone.utc).isoformat(),
                "crs": crs,
                "units": units,
                "plan_count": plan_count,
                "geom_count": geom_count,
            }
        )

    def add_geom_entry(self, entry: ManifestGeomEntry) -> None:
        self.geometry.append(asdict(entry))

    def add_plan_entry(self, entry: ManifestPlanEntry) -> None:
        self.results.append(asdict(entry))

    def add_terrain_entry(self, entry: ManifestTerrainEntry) -> None:
        self.terrain.append(asdict(entry))

    def add_terrain_modification_entry(self, entry: ManifestTerrainModificationEntry) -> None:
        self.terrain_modifications.append(asdict(entry))

    def add_terrain_source_entry(self, entry: ManifestTerrainSourceEntry) -> None:
        self.terrain_sources.append(asdict(entry))

    def add_map_entry(self, entry: ManifestMapEntry) -> None:
        self.maps.append(asdict(entry))

    # -----------------------------------------------------------------------
    # Serialization
    # -----------------------------------------------------------------------

    def to_dict(self) -> dict:
        d = {
            "schema_version": self.schema_version,
            "project": self.project,
            "project_parquet": self.project_parquet,
            "geometry": self.geometry,
            "results": self.results,
            "terrain": self.terrain,
            "terrain_modifications": self.terrain_modifications,
            "terrain_sources": self.terrain_sources,
        }
        if self.maps:
            d["maps"] = self.maps
        if self.postprocessing:
            d["postprocessing"] = self.postprocessing
        return d

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, default=str)

    def write(self, path: Path) -> None:
        """Write manifest.json to path."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.to_json(), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> "Manifest":
        """Load manifest.json from path."""
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            project=data.get("project", {}),
            project_parquet=data.get("project_parquet"),
            geometry=data.get("geometry", []),
            results=data.get("results", []),
            terrain=data.get("terrain", []),
            terrain_modifications=data.get("terrain_modifications", []),
            terrain_sources=data.get("terrain_sources", []),
            maps=data.get("maps", []),
            postprocessing=data.get("postprocessing", {}),
            schema_version=data.get("schema_version", SCHEMA_VERSION),
        )

    # -----------------------------------------------------------------------
    # Convenience queries
    # -----------------------------------------------------------------------

    @property
    def geom_ids(self) -> list[str]:
        return [g["geom_id"] for g in self.geometry]

    @property
    def plan_ids(self) -> list[str]:
        return [p["plan_id"] for p in self.results]

    def layer_paths(self) -> list[str]:
        """All unique parquet paths for geometry entries."""
        paths: set[str] = set()
        for g in self.geometry:
            p = g.get("parquet")
            if p:
                paths.add(p)
        return sorted(paths)

    def result_paths(self) -> list[str]:
        """All unique parquet paths for results entries."""
        paths: set[str] = set()
        for p in self.results:
            pq = p.get("parquet")
            if pq:
                paths.add(pq)
            for variable in p.get("variables", []):
                variable_pq = variable.get("parquet")
                if variable_pq:
                    paths.add(variable_pq)
        return sorted(paths)
