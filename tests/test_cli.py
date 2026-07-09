from pathlib import Path

from typer.testing import CliRunner

from ras2cng.cli import app


runner = CliRunner()


def test_results_all_routes_to_export_all(monkeypatch, tmp_path: Path):
    called = {"n": 0}

    def fake_export_all(plan_hdf, out_dir, geom_file=None):
        called["n"] += 1
        assert Path(out_dir) == tmp_path
        return ["a", "b"]

    monkeypatch.setattr("ras2cng.results.export_all_variables", fake_export_all)

    result = runner.invoke(app, ["results", "model.p01.hdf", str(tmp_path), "--all"])
    assert result.exit_code == 0
    assert called["n"] == 1


# ---------------------------------------------------------------------------
# map-hdf
# ---------------------------------------------------------------------------

def test_map_hdf_requires_exactly_one_terrain_source(tmp_path: Path):
    hdf = tmp_path / "model.p01.hdf"
    hdf.touch()

    # Neither terrain option
    result = runner.invoke(app, ["map-hdf", str(hdf), str(tmp_path / "out")])
    assert result.exit_code == 1
    assert "exactly one" in result.output

    # Both terrain options
    result = runner.invoke(app, [
        "map-hdf", str(hdf), str(tmp_path / "out"),
        "--terrain", str(tmp_path / "dem.tif"),
        "--terrain-hdf", str(tmp_path / "Terrain.hdf"),
    ])
    assert result.exit_code == 1
    assert "exactly one" in result.output


def test_map_hdf_scaffolds_then_generates(monkeypatch, tmp_path: Path):
    from ras2cng.scaffold import PlanHdfMetadata, ScaffoldInfo

    hdf = tmp_path / "model.p01.hdf"
    hdf.touch()
    dem = tmp_path / "dem.tif"
    dem.touch()
    outdir = tmp_path / "out"

    scaffold_calls = {}
    maps_calls = {}

    meta = PlanHdfMetadata(
        project_name="Model", plan_number="01", plan_title="T",
        plan_short_id="S", project_title="P", geom_ext="g01", flow_ext="u01",
        units="US Customary", projection_wkt="WKT",
        sim_start="01Jan2000 00:00:00", sim_end="02Jan2000 00:00:00",
        file_version="HEC-RAS 6.6",
    )

    def fake_build_scaffold(plan_hdf, workdir, **kwargs):
        scaffold_calls.update(kwargs, plan_hdf=plan_hdf, workdir=workdir)
        return ScaffoldInfo(
            project_dir=Path(workdir),
            prj_file=Path(workdir) / "Model.prj",
            plan_hdf=Path(workdir) / "Model.p01.hdf",
            terrain_hdf=Path(workdir) / "Terrain" / "Terrain.hdf",
            meta=meta,
        )

    def fake_generate(prj_file, output, **kwargs):
        maps_calls.update(kwargs, prj_file=prj_file, output=output)
        return []

    monkeypatch.setattr("ras2cng.scaffold.build_scaffold", fake_build_scaffold)
    monkeypatch.setattr("ras2cng.mapping.generate_result_maps", fake_generate)

    result = runner.invoke(app, [
        "map-hdf", str(hdf), str(outdir),
        "--terrain", str(dem),
        "--profile", "Max", "--froude", "--ras-version", "6.6",
    ])
    assert result.exit_code == 0, result.output

    # Scaffold call: default workdir under output, terrain passthrough
    assert scaffold_calls["plan_hdf"] == hdf
    assert scaffold_calls["workdir"] == outdir / "_scaffold"
    assert scaffold_calls["terrain_tifs"] == [dem]
    assert scaffold_calls["terrain_hdf"] is None
    assert scaffold_calls["ras_version"] == "6.6"

    # Map call: specific .prj file, single-plan filter, flags pass through
    assert maps_calls["prj_file"].name == "Model.prj"
    assert maps_calls["plans"] == ["p01"]
    assert maps_calls["froude"] is True
    assert maps_calls["skip_errors"] is False


def test_map_hdf_rm_scaffold_cleans_up(monkeypatch, tmp_path: Path):
    from ras2cng.scaffold import PlanHdfMetadata, ScaffoldInfo

    hdf = tmp_path / "model.p01.hdf"
    hdf.touch()
    dem = tmp_path / "dem.tif"
    dem.touch()
    outdir = tmp_path / "out"

    meta = PlanHdfMetadata(
        project_name="Model", plan_number="01", plan_title="T",
        plan_short_id="S", project_title="P", geom_ext="g01", flow_ext="u01",
        units="US Customary", projection_wkt="WKT",
        sim_start="01Jan2000 00:00:00", sim_end="02Jan2000 00:00:00",
        file_version="HEC-RAS 6.6",
    )

    def fake_build_scaffold(plan_hdf, workdir, **kwargs):
        from ras2cng.scaffold import SCAFFOLD_MARKER

        Path(workdir).mkdir(parents=True, exist_ok=True)
        (Path(workdir) / "Model.prj").write_text("stub")
        (Path(workdir) / SCAFFOLD_MARKER).write_text("{}")
        return ScaffoldInfo(
            project_dir=Path(workdir),
            prj_file=Path(workdir) / "Model.prj",
            plan_hdf=Path(workdir) / "Model.p01.hdf",
            terrain_hdf=Path(workdir) / "Terrain" / "Terrain.hdf",
            meta=meta,
        )

    monkeypatch.setattr("ras2cng.scaffold.build_scaffold", fake_build_scaffold)
    monkeypatch.setattr(
        "ras2cng.mapping.generate_result_maps", lambda *a, **k: []
    )

    result = runner.invoke(app, [
        "map-hdf", str(hdf), str(outdir), "--terrain", str(dem), "--rm-scaffold",
    ])
    assert result.exit_code == 0, result.output
    assert not (outdir / "_scaffold").exists()


def test_map_hdf_rm_scaffold_never_deletes_foreign_workdir(monkeypatch, tmp_path: Path):
    """--rm-scaffold must not delete a user directory build_scaffold rejected."""
    hdf = tmp_path / "model.p01.hdf"
    hdf.touch()
    dem = tmp_path / "dem.tif"
    dem.touch()

    user_dir = tmp_path / "precious_data"
    user_dir.mkdir()
    (user_dir / "keep_me.txt").write_text("important")

    def fake_build_scaffold(plan_hdf, workdir, **kwargs):
        raise ValueError("not a previous ras2cng scaffold")

    monkeypatch.setattr("ras2cng.scaffold.build_scaffold", fake_build_scaffold)

    result = runner.invoke(app, [
        "map-hdf", str(hdf), str(tmp_path / "out"),
        "--terrain", str(dem),
        "--workdir", str(user_dir), "--rm-scaffold",
    ])
    assert result.exit_code == 1
    assert user_dir.exists()
    assert (user_dir / "keep_me.txt").read_text() == "important"
