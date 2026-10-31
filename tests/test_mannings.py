"""`ras2cng mannings`: RAS Mapper's Final n Values in Rust, estimate fallback.

User decision 2026-10-31: the Rust raster (ras-commander 0.113
``HdfLandCover.export_final_mannings_raster``, fast-hdf 0.3) is the default.
Geometries the engine refuses fall back to the Python estimate with a warning,
and the method is recorded in the GeoTIFF tags. HdfLandCover is mocked; the
rasters are real small GeoTIFFs so the tags and summary are exercised.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pytest

rasterio = pytest.importorskip("rasterio")
from rasterio.transform import from_origin  # noqa: E402

from ras2cng import terrain  # noqa: E402


def _write_tif(path, value=0.06):
    data = np.full((4, 5), value, dtype="float32")
    data[0, 0] = -9999.0
    with rasterio.open(
        path, "w", driver="GTiff", width=5, height=4, count=1, dtype="float32",
        crs="EPSG:26918", transform=from_origin(0, 40, 10, 10), nodata=-9999.0,
    ) as dst:
        dst.write(data, 1)
    return data


class _Refused(Exception):
    """Stands in for ras_commander_fast_hdf.UnsupportedError."""


@pytest.fixture
def project(tmp_path):
    (tmp_path / "Model.prj").write_text("Proj Title=Model\n")
    (tmp_path / "Model.g01.hdf").write_bytes(b"")
    (tmp_path / "Model.g02.hdf").write_bytes(b"")
    return tmp_path


def _run(project, out, landcover, **kwargs):
    ras = SimpleNamespace(project_name="Model")
    fake_fh = SimpleNamespace(UnsupportedError=_Refused, TopologyError=type("T", (Exception,), {}))
    with patch("ras_commander.init_ras_project", return_value=ras), \
            patch("ras_commander.hdf.HdfLandCover", landcover), \
            patch.dict("sys.modules", {"ras_commander_fast_hdf": fake_fh}):
        return terrain.export_mannings_raster(project, out, **kwargs)


class _LandCover:
    """HdfLandCover fake: records calls, writes rasters."""

    def __init__(self, *, available=True, refuse=None, has_rust=True):
        self.calls = []
        self._available = available
        self._refuse = refuse
        if has_rust:
            self.export_final_mannings_raster = self._export
            self.rasmapper_final_mannings_available = lambda: self._available

    def _export(self, hdf, output, ras_object=None):
        self.calls.append(("rust", Path(hdf).name))
        if self._refuse is not None:
            raise self._refuse
        _write_tif(output, 0.08)
        return Path(output)

    def estimate_final_mannings_raster(self, hdf_path, output_tif_path, ras_object=None):
        self.calls.append(("estimate", Path(hdf_path).name))
        return _write_tif(output_tif_path, 0.12)


def _tags(path):
    with rasterio.open(path) as src:
        return src.tags(), src.read(1)


def test_rust_raster_is_the_default(project, tmp_path):
    lc = _LandCover()
    out = _run(project, tmp_path / "n.tif", lc)
    assert lc.calls == [("rust", "Model.g01.hdf")]
    tags, data = _tags(out)
    assert tags["ras2cng_mannings_method"] == terrain.MANNINGS_RASMAPPER
    assert "ras2cng_mannings_fallback_reason" not in tags
    assert float(data[1, 1]) == pytest.approx(0.08)


def test_refused_geometry_falls_back_to_the_estimate_with_a_warning(project, tmp_path, capsys):
    lc = _LandCover(refuse=_Refused("geometry has 192 cross sections (1D)"))
    out = _run(project, tmp_path / "n.tif", lc, geometry="g02")
    assert lc.calls == [("rust", "Model.g02.hdf"), ("estimate", "Model.g02.hdf")]
    tags, data = _tags(out)
    assert tags["ras2cng_mannings_method"] == terrain.MANNINGS_ESTIMATE
    assert "1D" in tags["ras2cng_mannings_fallback_reason"]
    assert float(data[1, 1]) == pytest.approx(0.12)
    printed = capsys.readouterr().out
    assert "WARNING" in printed and "estimate" in printed


def test_rasmapper_method_never_falls_back(project, tmp_path):
    lc = _LandCover(refuse=_Refused("pipe network"))
    with pytest.raises(RuntimeError, match="pipe network"):
        _run(project, tmp_path / "n.tif", lc, method="rasmapper")
    assert lc.calls == [("rust", "Model.g01.hdf")]


def test_estimate_method_keeps_the_old_behaviour(project, tmp_path):
    lc = _LandCover()
    out = _run(project, tmp_path / "n.tif", lc, method="estimate")
    assert lc.calls == [("estimate", "Model.g01.hdf")]
    assert _tags(out)[0]["ras2cng_mannings_method"] == terrain.MANNINGS_ESTIMATE


def test_missing_engine_falls_back(project, tmp_path):
    lc = _LandCover(available=False)
    out = _run(project, tmp_path / "n.tif", lc)
    assert lc.calls == [("estimate", "Model.g01.hdf")]
    assert "FinalManningsModel" in _tags(out)[0]["ras2cng_mannings_fallback_reason"]
    lc = _LandCover(has_rust=False)
    out = _run(project, tmp_path / "m.tif", lc)
    assert "0.113.0" in _tags(out)[0]["ras2cng_mannings_fallback_reason"]


def test_engine_errors_that_are_not_refusals_propagate(project, tmp_path):
    lc = _LandCover(refuse=OSError("disk full"))
    with pytest.raises(OSError, match="disk full"):
        _run(project, tmp_path / "n.tif", lc)
    assert lc.calls == [("rust", "Model.g01.hdf")], "no silent fallback on real failures"


def test_unknown_method_is_rejected(project, tmp_path):
    with pytest.raises(ValueError, match="auto, rasmapper, estimate"):
        _run(project, tmp_path / "n.tif", _LandCover(), method="fast")
