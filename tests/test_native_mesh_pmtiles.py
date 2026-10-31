"""Direct HDF routing and transactional publication regression checks."""
from pathlib import Path

import pytest

from ras2cng import pmtiles


def test_hdf_failure_preserves_existing_output(monkeypatch, tmp_path):
    from ras_commander import fast_hdf

    output = tmp_path / "mesh.pmtiles"
    output.write_bytes(b"previous valid archive")

    def fail(source, staged, **kwargs):
        assert kwargs["backend"] == "rust"
        assert kwargs["layer_name"] == "cells"
        Path(staged).write_bytes(b"partial archive")
        raise RuntimeError("native tiler failure")

    monkeypatch.setattr(fast_hdf, "export_mesh_pmtiles", fail)
    with pytest.raises(RuntimeError, match="native tiler failure"):
        pmtiles.generate_mesh_pmtiles(tmp_path / "model.g01.hdf", output, layer_name="cells")
    assert output.read_bytes() == b"previous valid archive"
    assert list(tmp_path.glob(".*partial*")) == []


def test_hdf_route_passes_mesh_options(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(pmtiles, "generate_mesh_pmtiles", lambda *a, **kw: calls.append((a, kw)))
    pmtiles.generate_pmtiles_from_input(
        tmp_path / "model.g01.hdf", tmp_path / "cells.pmtiles",
        layer_name="cells", mesh_names=["LockHaven"], source_crs="EPSG:2271", threads=2,
    )
    assert calls[0][1] == {
        "layer_name": "cells", "min_zoom": 10, "max_zoom": 14,
        "mesh_names": ["LockHaven"], "source_crs": "EPSG:2271", "threads": 2,
    }


def test_hdf_options_are_not_silently_ignored_for_parquet(tmp_path):
    with pytest.raises(ValueError, match="only to HDF"):
        pmtiles.generate_pmtiles_from_input(tmp_path / "mesh.parquet", tmp_path / "out.pmtiles", threads=2)


def test_hdf_cannot_replace_source(tmp_path):
    source = tmp_path / "model.g01.hdf"
    source.write_bytes(b"source")
    with pytest.raises(ValueError, match="source HDF"):
        pmtiles.generate_mesh_pmtiles(source, source)
    assert source.read_bytes() == b"source"
