import pytest


def test_duckdb_register_parquet_importable():
    duckdb = pytest.importorskip("duckdb")
    assert duckdb is not None
