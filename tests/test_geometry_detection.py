from pathlib import Path

import rascmdr_parquet.geometry as geom


def test_is_hdf_geometry():
    assert geom._is_hdf_geometry(Path("model.g01.hdf"))
    assert geom._is_hdf_geometry(Path("C:/x/y/model.g99.hdf"))
    assert not geom._is_hdf_geometry(Path("model.g01"))


def test_is_text_geometry():
    assert geom._is_text_geometry(Path("model.g01"))
    assert not geom._is_text_geometry(Path("model.g01.hdf"))
