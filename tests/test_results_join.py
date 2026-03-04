from pathlib import Path

import geopandas as gpd
from shapely.geometry import Polygon, Point

from ras2cng.results import export_results_layer


def test_results_join_points_to_polys(monkeypatch, tmp_path: Path):
    # Fake results: points with mesh_name/cell_id
    res = gpd.GeoDataFrame(
        {
            "mesh_name": ["m1", "m1"],
            "cell_id": [1, 2],
            "Maximum Depth": [1.0, 2.0],
        },
        geometry=[Point(0, 0), Point(1, 1)],
        crs="EPSG:4326",
    )

    def fake_get_mesh_summary_output(plan_hdf, variable):
        return res

    monkeypatch.setattr(
        "ras2cng.results.HdfResultsMesh.get_mesh_summary_output",
        fake_get_mesh_summary_output,
    )

    polys = gpd.GeoDataFrame(
        {
            "mesh_name": ["m1", "m1"],
            "cell_id": [1, 2],
        },
        geometry=[
            Polygon([(0, 0), (0, 1), (1, 1), (1, 0)]),
            Polygon([(1, 1), (1, 2), (2, 2), (2, 1)]),
        ],
        crs="EPSG:4326",
    )

    geom_path = tmp_path / "mesh_cells.parquet"
    polys.to_parquet(geom_path, index=False)

    out = tmp_path / "out.parquet"
    export_results_layer(Path("model.p01.hdf"), out, variable="Maximum Depth", geom_file=geom_path)

    joined = gpd.read_parquet(out)
    assert len(joined) == 2
    assert "Maximum Depth" in joined.columns
    assert joined.geometry.geom_type.iloc[0] in ("Polygon", "MultiPolygon")
