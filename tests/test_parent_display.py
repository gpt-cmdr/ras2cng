"""Synthetic tests for parent-display publication (no HEC-RAS files, no tiler binaries)."""

import geopandas as gpd
import pyarrow.parquet as pq
import pytest
from shapely.geometry import LineString, Point, box

from ras2cng import parent_display as pdisp
from ras2cng.parent_display import PARENT_LAYERS, PublishRefused, publish_parent_layer

CRS = "EPSG:2278"  # US survey feet; native units must survive publication


def _frame(geoms, crs=CRS):
    return gpd.GeoDataFrame({"id": range(len(geoms))}, geometry=geoms, crs=crs)


class RecordingTiler:
    def __init__(self):
        self.calls = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        kwargs["output"].write_bytes(b"pmtiles")


def test_domain_is_geoparquet_only_and_keeps_native_crs(tmp_path):
    tiler = RecordingTiler()
    frame = _frame([box(1.8e6, 1.39e7, 1.9e6, 1.4e7)])
    record = publish_parent_layer(frame, "parent_domain", tmp_path, tile_writer=tiler)
    assert record["geoparquet"] == "parent_domain.geoparquet"
    assert "pmtiles" not in record and tiler.calls == []
    table = pq.read_table(tmp_path / "parent_domain.geoparquet")
    assert b"2278" in table.schema.metadata[b"geo"]  # CRS authority kept, not 4326
    assert {"bbox_xmin", "bbox_ymax"} <= set(table.column_names)


def test_mesh_is_tiled_from_the_detail_floor_and_keeps_every_feature(tmp_path):
    tiler = RecordingTiler()
    cells = _frame([box(i, 0, i + 1, 1) for i in range(50)])
    record = publish_parent_layer(cells, "parent_mesh", tmp_path, tile_writer=tiler)
    assert record["feature_count"] == 50
    call = tiler.calls[0]
    assert call["min_zoom"] == pdisp.DETAIL_MIN_ZOOM == 13
    assert len(call["frame"]) == 50  # nothing dropped before the tiler
    assert (tmp_path / "parent_mesh.pmtiles").exists()
    assert record["pmtiles"] == "parent_mesh.pmtiles"


def test_freestiler_is_preferred_when_installed(tmp_path, monkeypatch):
    from ras2cng import vector_tiles

    monkeypatch.setattr(vector_tiles, "freestiler_available", lambda: True)
    monkeypatch.setenv(
        "RAS2CNG_VECTOR_TILER", "tippecanoe"
    )  # freestiler still wins by request
    record = publish_parent_layer(
        _frame([LineString([(0, 0), (1, 1)])]),
        "parent_breaklines",
        tmp_path,
        tile_writer=RecordingTiler(),
    )
    assert record["tiler"] == {
        "backend": "freestiler",
        "preferred": "freestiler",
        "fallback_reason": None,
    }


def test_fallback_is_recorded_when_freestiler_is_missing(tmp_path, monkeypatch):
    from ras2cng import vector_tiles

    monkeypatch.setattr(vector_tiles, "freestiler_available", lambda: False)
    monkeypatch.setenv("RAS2CNG_VECTOR_TILER", "tippecanoe")
    record = publish_parent_layer(
        _frame([LineString([(0, 0), (1, 1)])]),
        "parent_breaklines",
        tmp_path,
        tile_writer=RecordingTiler(),
    )
    assert record["tiler"]["backend"] == "tippecanoe"
    assert (
        record["tiler"]["fallback_reason"] == "freestiler unavailable; using tippecanoe"
    )


def test_empty_layer_is_refused_not_published_as_zero(tmp_path):
    # An empty structures table must surface as LAYER_EMPTY, never as a file.
    with pytest.raises(PublishRefused) as err:
        publish_parent_layer(_frame([]), "parent_structures", tmp_path)
    assert err.value.reason_code == "LAYER_EMPTY"
    assert list(tmp_path.iterdir()) == []


def test_layer_without_crs_is_refused_before_any_write(tmp_path):
    with pytest.raises(PublishRefused) as err:
        publish_parent_layer(
            _frame([box(0, 0, 1, 1)], crs=None), "parent_domain", tmp_path
        )
    assert err.value.reason_code == "CRS_MISSING"
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    ("layer", "geoms", "code"),
    [
        ("parent_domain", [Point(0, 0)], "GEOMETRY_TYPE_MISMATCH"),
        ("parent_breaklines", [box(0, 0, 1, 1)], "GEOMETRY_TYPE_MISMATCH"),
        ("parent_domain", [box(0, 0, 1, 1), None], "GEOMETRY_NULL"),
    ],
)
def test_wrong_or_null_geometry_is_refused(tmp_path, layer, geoms, code):
    with pytest.raises(PublishRefused) as err:
        publish_parent_layer(_frame(geoms), layer, tmp_path)
    assert err.value.reason_code == code


def test_unknown_layer_is_refused(tmp_path):
    with pytest.raises(PublishRefused) as err:
        publish_parent_layer(_frame([box(0, 0, 1, 1)]), "plan_depth", tmp_path)
    assert err.value.reason_code == "UNKNOWN_LAYER"


def test_refusal_codes_and_layer_table_are_closed():
    assert set(pdisp.REFUSAL_CODES) == {
        "UNKNOWN_LAYER",
        "LAYER_EMPTY",
        "CRS_MISSING",
        "GEOMETRY_NULL",
        "GEOMETRY_TYPE_MISMATCH",
        "OUTPUT_EXISTS",
    }
    assert set(PARENT_LAYERS) == {
        "parent_domain",
        "parent_mesh",
        "parent_breaklines",
        "parent_refinement",
        "parent_structures",
        "parent_connections",
    }


# --- remediation: no overwrite, no false success -----------------------------------


class ExplodingTiler:
    def __init__(self, write_first=False):
        self.write_first = write_first

    def __call__(self, **kwargs):
        if self.write_first:  # a tiler that leaves a partial archive behind
            kwargs["output"].write_bytes(b"partial")
        raise RuntimeError("tiler exploded")


def _snapshot(directory):
    return {p.name: p.read_bytes() for p in sorted(directory.iterdir())}


@pytest.mark.parametrize("existing", ["parent_mesh.geoparquet", "parent_mesh.pmtiles"])
def test_existing_completed_artifact_is_never_overwritten(tmp_path, existing):
    (tmp_path / existing).write_bytes(b"COMPLETED PRIOR ARTIFACT")
    before = _snapshot(tmp_path)
    tiler = RecordingTiler()
    cells = _frame([box(i, 0, i + 1, 1) for i in range(3)])
    with pytest.raises(PublishRefused) as err:
        publish_parent_layer(cells, "parent_mesh", tmp_path, tile_writer=tiler)
    assert err.value.reason_code == "OUTPUT_EXISTS"
    assert tiler.calls == [] and _snapshot(tmp_path) == before


def test_existing_geoparquet_blocks_a_geoparquet_only_layer(tmp_path):
    (tmp_path / "parent_domain.geoparquet").write_bytes(b"PRIOR")
    with pytest.raises(PublishRefused) as err:
        publish_parent_layer(_frame([box(0, 0, 1, 1)]), "parent_domain", tmp_path)
    assert err.value.reason_code == "OUTPUT_EXISTS"
    assert (tmp_path / "parent_domain.geoparquet").read_bytes() == b"PRIOR"


def test_publishing_twice_into_one_directory_refuses_the_second(tmp_path):
    frame = _frame([box(0, 0, 1, 1)])
    publish_parent_layer(frame, "parent_domain", tmp_path)
    first = _snapshot(tmp_path)
    with pytest.raises(PublishRefused) as err:
        publish_parent_layer(frame, "parent_domain", tmp_path)
    assert err.value.reason_code == "OUTPUT_EXISTS" and _snapshot(tmp_path) == first


@pytest.mark.parametrize("write_first", [False, True])
def test_tiler_failure_never_reports_success_and_leaves_no_final_names(
    tmp_path, write_first
):
    cells = _frame([box(i, 0, i + 1, 1) for i in range(3)])
    with pytest.raises(RuntimeError, match="tiler exploded"):
        publish_parent_layer(
            cells,
            "parent_mesh",
            tmp_path,
            tile_writer=ExplodingTiler(write_first=write_first),
        )
    assert (
        list(tmp_path.iterdir()) == []
    )  # neither the GeoParquet nor a partial PMTiles


def test_tiler_failure_does_not_touch_other_completed_layers(tmp_path):
    publish_parent_layer(_frame([box(0, 0, 1, 1)]), "parent_domain", tmp_path)
    before = _snapshot(tmp_path)
    cells = _frame([box(i, 0, i + 1, 1) for i in range(3)])
    with pytest.raises(RuntimeError):
        publish_parent_layer(
            cells, "parent_mesh", tmp_path, tile_writer=ExplodingTiler(True)
        )
    assert _snapshot(tmp_path) == before


def test_failed_attempt_can_be_retried_in_a_fresh_directory_only_after_cleanup(
    tmp_path,
):
    cells = _frame([box(i, 0, i + 1, 1) for i in range(3)])
    with pytest.raises(RuntimeError):
        publish_parent_layer(
            cells, "parent_mesh", tmp_path, tile_writer=ExplodingTiler(True)
        )
    record = publish_parent_layer(
        cells, "parent_mesh", tmp_path, tile_writer=RecordingTiler()
    )
    assert (
        record["pmtiles"] == "parent_mesh.pmtiles"
    )  # a clean failure leaves a clean slate


def test_unresolvable_tiler_fails_before_any_file_is_written(tmp_path, monkeypatch):
    monkeypatch.setenv("RAS2CNG_VECTOR_TILER", "not-a-tiler")
    from ras2cng import vector_tiles

    monkeypatch.setattr(vector_tiles, "freestiler_available", lambda: False)
    cells = _frame([box(0, 0, 1, 1)])
    with pytest.raises(ValueError, match="RAS2CNG_VECTOR_TILER"):
        publish_parent_layer(
            cells, "parent_mesh", tmp_path, tile_writer=RecordingTiler()
        )
    assert list(tmp_path.iterdir()) == []
