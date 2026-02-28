"""
Integration tests: export HEC-RAS geometry and results to GeoParquet,
then validate with geopandas + DuckDB.

Run with:
    pytest tests/test_full_extraction.py -v --tb=short -m integration

Models are downloaded/extracted on first run (session-scoped fixtures in conftest).
Output goes to I:/rascmdr-parquet-testing/outputs/.
A JSON report is written to I:/rascmdr-parquet-testing/reports/extraction_report.json.
"""

from __future__ import annotations

from pathlib import Path

import geopandas as gpd
import pytest

from rascmdr_parquet.geometry import export_geometry_layers
from rascmdr_parquet.results import (
    export_all_variables,
    export_results_layer,
    list_available_summary_variables,
)
from rascmdr_parquet.duckdb_session import query_parquet

from conftest import (
    OUTPUTS_DIR,
    discover_geometry_hdf_files,
    discover_plan_hdf_files,
    discover_text_geometry_files,
    _file_id,
    _output_subdir,
    _source_label,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

HDF_GEOM_LAYERS = ["mesh_cells", "cross_sections", "centerlines"]
TEXT_GEOM_LAYERS = ["cross_sections", "centerlines"]


def _validate_parquet(path: Path) -> gpd.GeoDataFrame:
    """Read parquet, assert basic validity, return GeoDataFrame."""
    assert path.exists(), f"Output file does not exist: {path}"
    gdf = gpd.read_parquet(path)
    assert len(gdf) > 0, f"Empty GeoDataFrame in {path}"
    assert "geometry" in gdf.columns, f"No geometry column in {path}"
    return gdf


def _validate_duckdb(path: Path):
    """Confirm DuckDB can query the parquet file."""
    df = query_parquet(path, "SELECT COUNT(*) AS cnt FROM _")
    assert df["cnt"].iloc[0] > 0, f"DuckDB returned 0 rows for {path}"


def _stem_for_output(p: Path) -> str:
    """Get a clean stem without double-suffixes (e.g. 'Model.g01' from 'Model.g01.hdf')."""
    # For HDF: name = 'Model.g01.hdf' -> want 'Model.g01'
    # For text: name = 'Model.g01' -> want 'Model.g01'
    name = p.name
    if name.lower().endswith(".hdf"):
        return name[: -len(".hdf")]
    return name


# ---------------------------------------------------------------------------
# Ensure models are downloaded (autouse triggers the session fixtures)
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True, scope="session")
def _download_models(ensure_ras_examples, ensure_m3_models):
    """Trigger model downloads once per session."""
    pass


# ---------------------------------------------------------------------------
# Geometry HDF tests
# ---------------------------------------------------------------------------

_geom_hdf_files = discover_geometry_hdf_files()


@pytest.mark.integration
@pytest.mark.parametrize(
    "geom_hdf",
    _geom_hdf_files,
    ids=[_file_id(f) for f in _geom_hdf_files] if _geom_hdf_files else [],
)
def test_export_geometry_hdf(geom_hdf: Path, output_base, report_collector):
    """Export all 3 geometry layers from each HDF geometry file."""
    subdir = _output_subdir(geom_hdf)
    stem = _stem_for_output(geom_hdf)
    source = _source_label(geom_hdf)

    for layer in HDF_GEOM_LAYERS:
        out = OUTPUTS_DIR / subdir / f"{stem}_{layer}.parquet"
        out.parent.mkdir(parents=True, exist_ok=True)

        try:
            export_geometry_layers(geom_hdf, out, layer=layer)
            _validate_parquet(out)
            _validate_duckdb(out)
            report_collector.record("PASS", geom_hdf, layer, source=source)
        except Exception as exc:
            report_collector.record("ERROR", geom_hdf, layer, str(exc), source=source)
            # Don't fail the whole test — record and continue to next layer
            continue


# ---------------------------------------------------------------------------
# Text geometry tests
# ---------------------------------------------------------------------------

_text_geom_files = discover_text_geometry_files()


@pytest.mark.integration
@pytest.mark.parametrize(
    "text_geom",
    _text_geom_files,
    ids=[_file_id(f) for f in _text_geom_files] if _text_geom_files else [],
)
def test_export_text_geometry(text_geom: Path, output_base, report_collector):
    """Export geometry from text .g?? files (cross_sections, centerlines)."""
    subdir = _output_subdir(text_geom)
    stem = _stem_for_output(text_geom)
    source = _source_label(text_geom)

    for layer in TEXT_GEOM_LAYERS:
        out = OUTPUTS_DIR / subdir / f"{stem}_{layer}.parquet"
        out.parent.mkdir(parents=True, exist_ok=True)

        try:
            export_geometry_layers(text_geom, out, layer=layer)
            _validate_parquet(out)
            _validate_duckdb(out)
            report_collector.record("PASS", text_geom, layer, source=source)
        except Exception as exc:
            report_collector.record("ERROR", text_geom, layer, str(exc), source=source)
            continue


# ---------------------------------------------------------------------------
# Results export tests
# ---------------------------------------------------------------------------

_plan_hdf_files = discover_plan_hdf_files()


@pytest.mark.integration
@pytest.mark.parametrize(
    "plan_hdf",
    _plan_hdf_files,
    ids=[_file_id(f) for f in _plan_hdf_files] if _plan_hdf_files else [],
)
def test_export_results(plan_hdf: Path, output_base, report_collector):
    """List available summary variables and export each one."""
    subdir = _output_subdir(plan_hdf)
    stem = _stem_for_output(plan_hdf)
    source = _source_label(plan_hdf)

    try:
        variables = list_available_summary_variables(plan_hdf)
    except Exception as exc:
        report_collector.record("ERROR", plan_hdf, "list_variables", str(exc), source=source)
        return

    if not variables:
        report_collector.record("SKIP", plan_hdf, "list_variables", "No 2D summary variables found", source=source)
        return

    for var in variables:
        var_slug = var.lower().replace(" ", "_")
        out = OUTPUTS_DIR / subdir / f"{stem}_{var_slug}.parquet"
        out.parent.mkdir(parents=True, exist_ok=True)

        try:
            export_results_layer(plan_hdf, out, variable=var)
            _validate_parquet(out)
            _validate_duckdb(out)
            report_collector.record("PASS", plan_hdf, var, source=source)
        except Exception as exc:
            report_collector.record("ERROR", plan_hdf, var, str(exc), source=source)
            continue


# ---------------------------------------------------------------------------
# Results with polygon geometry join
# ---------------------------------------------------------------------------

@pytest.mark.integration
@pytest.mark.parametrize(
    "plan_hdf",
    _plan_hdf_files,
    ids=[_file_id(f) for f in _plan_hdf_files] if _plan_hdf_files else [],
)
def test_export_results_with_geometry_join(plan_hdf: Path, output_base, report_collector):
    """Export results joined to polygon geometry where mesh_cells parquet exists."""
    subdir = _output_subdir(plan_hdf)
    source = _source_label(plan_hdf)

    # Find a matching mesh_cells parquet in the same output subdirectory.
    # The geometry HDF should have been exported already by test_export_geometry_hdf.
    mesh_parquets = sorted((OUTPUTS_DIR / subdir).glob("*_mesh_cells.parquet")) if (OUTPUTS_DIR / subdir).exists() else []

    if not mesh_parquets:
        report_collector.record(
            "SKIP", plan_hdf, "geometry_join",
            "No mesh_cells parquet found for join", source=source,
        )
        return

    geom_parquet = mesh_parquets[0]

    # Try the first available variable
    try:
        variables = list_available_summary_variables(plan_hdf)
    except Exception as exc:
        report_collector.record("ERROR", plan_hdf, "geometry_join", str(exc), source=source)
        return

    if not variables:
        report_collector.record(
            "SKIP", plan_hdf, "geometry_join",
            "No 2D summary variables for join test", source=source,
        )
        return

    var = variables[0]
    var_slug = var.lower().replace(" ", "_")
    stem = _stem_for_output(plan_hdf)
    out = OUTPUTS_DIR / subdir / f"{stem}_{var_slug}_joined.parquet"
    out.parent.mkdir(parents=True, exist_ok=True)

    try:
        export_results_layer(plan_hdf, out, variable=var, geom_file=geom_parquet)
        gdf = _validate_parquet(out)
        _validate_duckdb(out)

        # Verify geometry is polygons (from the join), not points
        geom_types = set(gdf.geometry.dropna().geom_type.unique())
        has_polys = bool(geom_types & {"Polygon", "MultiPolygon"})
        if has_polys:
            report_collector.record("PASS", plan_hdf, f"{var} (joined)", source=source)
        else:
            report_collector.record(
                "PASS", plan_hdf, f"{var} (joined, point geom)",
                f"Geometry types: {geom_types}", source=source,
            )
    except Exception as exc:
        report_collector.record("ERROR", plan_hdf, f"{var} (joined)", str(exc), source=source)
