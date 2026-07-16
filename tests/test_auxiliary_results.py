from __future__ import annotations

from pathlib import Path

import h5py
import numpy as np
import pandas as pd

from ras2cng.results import (
    _summarize_time_location_dataset,
    extract_auxiliary_result_tables,
    extract_pipe_network_summaries,
    extract_sa2d_structure_summary,
)


def test_chunked_dataset_summary_tracks_extrema_and_time_index(tmp_path: Path):
    path = tmp_path / "summary.p01.hdf"
    values = np.array(
        [
            [1.0, 9.0],
            [5.0, np.nan],
            [3.0, 12.0],
            [-2.0, 4.0],
        ]
    )
    with h5py.File(path, "w") as hdf:
        dataset = hdf.create_dataset("values", data=values)
        summary = _summarize_time_location_dataset(dataset, chunk_rows=2)

    assert summary["maximum"].tolist() == [5.0, 12.0]
    assert summary["minimum"].tolist() == [-2.0, 4.0]
    assert summary["time_index"].tolist() == [1, 2]


def test_pipe_network_summary_discovers_all_named_networks(tmp_path: Path):
    path = tmp_path / "pipes.p01.hdf"
    base = (
        "Results/Unsteady/Output/Output Blocks/DSS Hydrograph Output/"
        "Unsteady Time Series/Pipe Networks"
    )
    with h5py.File(path, "w") as hdf:
        network = hdf.require_group(f"{base}/Davis")
        network.create_dataset(
            "Pipes/Pipe Flow DS",
            data=np.array([[1.0, 2.0], [4.0, 3.0], [-2.0, 8.0]]),
        )
        network.create_dataset(
            "Pipes/Vel US",
            data=np.array([[0.5, 1.0], [2.5, 1.5], [1.5, 4.0]]),
        )
        network.create_dataset(
            "Nodes/Depth",
            data=np.array([[0.0, 0.2, 0.5], [1.0, 0.8, 0.7]]),
        )

    summaries = extract_pipe_network_summaries(path, chunk_rows=2)

    conduits = summaries["pipe_conduits"]
    nodes = summaries["pipe_nodes"]
    assert conduits["network_name"].unique().tolist() == ["Davis"]
    assert conduits["conduit_id"].tolist() == [0, 1]
    assert conduits["maximum_pipe_flow_ds"].tolist() == [4.0, 8.0]
    assert conduits["maximum_vel_us"].tolist() == [2.5, 4.0]
    assert nodes["node_id"].tolist() == [0, 1, 2]
    assert nodes["maximum_depth"].tolist() == [1.0, 0.8, 0.7]


def test_structure_summary_reads_in_bounded_chunks(tmp_path: Path):
    path = tmp_path / "structures.p01.hdf"
    base = (
        "Results/Unsteady/Output/Output Blocks/Base Output/Unsteady Time Series/"
        "SA 2D Area Conn"
    )
    with h5py.File(path, "w") as hdf:
        hdf.create_dataset(
            f"{base}/Dam A/Structure Variables",
            data=np.array(
                [
                    [10.0, 3.0, 101.0, 99.0],
                    [20.0, 5.0, 102.0, 100.0],
                    [-4.0, 1.0, 100.0, 98.0],
                ]
            ),
        )

    summary = extract_sa2d_structure_summary(path, chunk_rows=1)

    assert summary.loc[0, "structure_name"] == "Dam A"
    assert summary.loc[0, "maximum_total_flow"] == 20.0
    assert summary.loc[0, "minimum_total_flow"] == -4.0
    assert summary.loc[0, "maximum_hw"] == 102.0
    assert summary.loc[0, "maximum_total_flow_time_index"] == 1


def test_structure_summary_joins_geometry_connection_identifier(monkeypatch, tmp_path: Path):
    path = tmp_path / "structures.p01.hdf"
    path.touch()
    monkeypatch.setattr(
        "ras2cng.results.HdfResultsPlan.get_reference_summary",
        lambda *_args, **_kwargs: pd.DataFrame(),
    )
    monkeypatch.setattr(
        "ras2cng.results.extract_sa2d_structure_summary",
        lambda *_args, **_kwargs: pd.DataFrame({"structure_name": ["162"]}),
    )
    monkeypatch.setattr(
        "ras2cng.results.HdfPump.get_pump_station_summary",
        lambda *_args, **_kwargs: pd.DataFrame(),
    )
    monkeypatch.setattr(
        "ras2cng.results.extract_pipe_network_summaries",
        lambda *_args, **_kwargs: {"pipe_conduits": pd.DataFrame(), "pipe_nodes": pd.DataFrame()},
    )
    monkeypatch.setattr(
        "ras2cng.results.extract_1d_structure_summary",
        lambda *_args, **_kwargs: pd.DataFrame(),
    )

    tables = extract_auxiliary_result_tables(path)

    structure = next(table for table in tables if table.variable == "sa2d_structure_summary")
    assert structure.join_columns == {"Connection": "structure_name"}
