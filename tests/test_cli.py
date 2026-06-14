from pathlib import Path

from typer.testing import CliRunner

from ras2cng.cli import app
from ras2cng.precipitation import PrecipitationExportResult


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


def test_precip_routes_to_export_precipitation(monkeypatch, tmp_path: Path):
    called = {"kwargs": None}

    def fake_export_precipitation(hdf_file, output, **kwargs):
        called["kwargs"] = kwargs
        assert Path(hdf_file) == Path("model.p01.hdf")
        assert Path(output) == tmp_path
        return PrecipitationExportResult(
            source_hdf=Path(hdf_file),
            source="processed",
            values_path="Event Conditions/Meteorology/Precipitation/Values",
            output_dir=Path(output),
            units="in",
            rows=2,
            cols=2,
            timestamps=["0", "2"],
            incremental=[tmp_path / "a.tif", tmp_path / "b.tif"],
            cumulative=[],
        )

    monkeypatch.setattr(
        "ras2cng.precipitation.export_precipitation_rasters",
        fake_export_precipitation,
    )

    result = runner.invoke(
        app,
        [
            "precip",
            "model.p01.hdf",
            str(tmp_path),
            "--source",
            "processed",
            "--timestamps",
            "0,2",
            "--no-cumulative",
            "--prefix",
            "rain",
        ],
    )

    assert result.exit_code == 0
    assert called["kwargs"] == {
        "source": "processed",
        "timestamps": ["0", "2"],
        "export_incremental": True,
        "export_cumulative": False,
        "prefix": "rain",
        "overwrite": True,
    }
