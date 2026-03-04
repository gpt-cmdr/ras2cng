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
