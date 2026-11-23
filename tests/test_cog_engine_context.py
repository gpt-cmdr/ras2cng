import pytest

from ras2cng.cog import COG_ENGINE_ENV, cog_engine, using_cog_engine


def test_cog_engine_override_is_scoped(monkeypatch):
    monkeypatch.setenv(COG_ENGINE_ENV, "gdal")
    assert cog_engine() == "gdal"
    with using_cog_engine("auto"):
        assert cog_engine() == "auto"
        with pytest.raises(ValueError, match="COG_ENGINE"):
            with using_cog_engine("invalid"):
                pytest.fail("accepted an invalid engine")
        assert cog_engine() == "auto"
    assert cog_engine() == "gdal"
