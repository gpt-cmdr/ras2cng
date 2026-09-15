"""Tests for the private ras-commander stored-map adapter."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from ras2cng._ras_commander_maps import generate_plan_maps


def _ras(tmp_path: Path):
    plan_hdf = tmp_path / "Model.p01.hdf"
    plan_hdf.touch()
    return SimpleNamespace(project_folder=tmp_path, project_name="Model")


def _steady_summary(
    output: Path,
    profile: str,
    *,
    boundary: bool = False,
) -> dict:
    depth_vrt = output / f"Depth ({profile}).vrt"
    depth_tif = output / f"Depth ({profile}).Terrain.tif"
    depth_vrt.write_text("vrt", encoding="utf-8")
    depth_tif.write_text("tif", encoding="utf-8")
    records = [
        {
            "profile_index": 1,
            "profile_name": profile,
            "map_type": "depth",
            "files": [str(depth_vrt), str(depth_tif)],
        }
    ]
    if boundary:
        boundary_path = output / f"Inundation Boundary ({profile}).shp"
        boundary_path.write_text("shp", encoding="utf-8")
        records.append(
            {
                "profile_index": 1,
                "profile_name": profile,
                "map_type": "inundation_boundary",
                "files": [str(boundary_path)],
            }
        )
    return {
        "success": True,
        "mode": "steady_profiles",
        "plans": {
            "01": {
                "success": True,
                "profiles": [
                    {
                        "profile_index": 1,
                        "profile_name": profile,
                        "products": records,
                    }
                ],
                "stored_maps": records,
            }
        },
    }


def _selected_summary(output: Path, map_type: str, label: str) -> dict:
    path = output / f"{label}.tif"
    path.write_text(map_type, encoding="utf-8")
    return {
        "success": True,
        "plans": {
            "01": {
                "success": True,
                "files_by_type": {map_type: [str(path)]},
            }
        },
    }


def _generate(
    ras,
    output: Path,
    *,
    profile: str,
    map_types: list[str],
) -> dict[str, list[Path]]:
    result = generate_plan_maps(
        ras=ras,
        plan_number="01",
        profile=profile,
        output_dir=output,
        map_types=map_types,
        render_mode="sloping",
        terrain_name="Terrain",
        arrival_depth=0.5,
        ras_version="7.0.1",
        timeout=1200,
        performance=object(),
        optimized_available=True,
    )
    assert result is not None
    return result


def test_named_steady_profile_routes_exactly_without_boundary(tmp_path):
    ras = _ras(tmp_path)
    summary = _steady_summary(tmp_path, "Q100")

    with (
        patch(
            "ras2cng._ras_commander_maps.HdfResultsPlan.is_steady_plan",
            return_value=True,
        ),
        patch(
            "ras2cng._ras_commander_maps.HdfResultsPlan.get_steady_profile_names",
            return_value=["Q10", "Q100"],
        ),
        patch(
            "ras2cng._ras_commander_maps.supports_exact_steady_profiles",
            return_value=True,
        ),
        patch(
            "ras2cng._ras_commander_maps.RasMap.store_all_maps",
            return_value=summary,
        ) as store,
    ):
        result = _generate(
            ras,
            tmp_path,
            profile="Q100",
            map_types=["depth"],
        )

    kwargs = store.call_args.kwargs
    assert kwargs["mode"] == "steady_profiles"
    assert kwargs["profiles"] == ["Q100"]
    assert kwargs["depth"] is True
    assert kwargs["wse"] is False
    assert kwargs["inundation_boundary"] is False
    assert "profile" not in kwargs
    assert "performance" not in kwargs
    assert [path.name for path in result["depth"]] == ["Depth (Q100).Terrain.tif"]
    assert "inundation_boundary" not in result


@pytest.mark.parametrize(
    ("available", "message"),
    [
        (["Q10"], "was not found"),
        (["Q100", "Q100"], "ambiguous"),
    ],
)
def test_invalid_steady_profile_fails_before_store_map_mutation(
    tmp_path,
    available,
    message,
):
    ras = _ras(tmp_path)

    with (
        patch(
            "ras2cng._ras_commander_maps.HdfResultsPlan.is_steady_plan",
            return_value=True,
        ),
        patch(
            "ras2cng._ras_commander_maps.HdfResultsPlan.get_steady_profile_names",
            return_value=available,
        ),
        patch("ras2cng._ras_commander_maps.RasMap.store_all_maps") as store,
        pytest.raises(ValueError, match=message),
    ):
        _generate(
            ras,
            tmp_path,
            profile="Q100",
            map_types=["depth"],
        )

    store.assert_not_called()


@pytest.mark.parametrize("profile", ["Max", "Min"])
def test_explicit_summary_profiles_remain_on_selected_mode(tmp_path, profile):
    ras = _ras(tmp_path)
    summary = _selected_summary(tmp_path, "depth", f"Depth ({profile})")

    with (
        patch("ras2cng._ras_commander_maps.HdfResultsPlan.is_steady_plan") as is_steady,
        patch(
            "ras2cng._ras_commander_maps.RasMap.store_all_maps",
            return_value=summary,
        ) as store,
    ):
        result = _generate(
            ras,
            tmp_path,
            profile=profile,
            map_types=["depth"],
        )

    is_steady.assert_not_called()
    assert store.call_args.kwargs["mode"] == "selected"
    assert store.call_args.kwargs["profile"] == profile
    assert result["depth"]


def test_named_steady_profile_splits_whole_simulation_products(tmp_path):
    ras = _ras(tmp_path)
    steady_summary = _steady_summary(tmp_path, "Q100", boundary=True)
    arrival_summary = _selected_summary(
        tmp_path,
        "arrival_time",
        "Arrival Time (0.5ft hrs)",
    )

    with (
        patch(
            "ras2cng._ras_commander_maps.HdfResultsPlan.is_steady_plan",
            return_value=True,
        ),
        patch(
            "ras2cng._ras_commander_maps.HdfResultsPlan.get_steady_profile_names",
            return_value=["Q10", "Q100"],
        ),
        patch(
            "ras2cng._ras_commander_maps.supports_exact_steady_profiles",
            return_value=True,
        ),
        patch(
            "ras2cng._ras_commander_maps.RasMap.store_all_maps",
            side_effect=[steady_summary, arrival_summary],
        ) as store,
    ):
        result = _generate(
            ras,
            tmp_path,
            profile="Q100",
            map_types=["depth", "inundation_boundary", "arrival_time"],
        )

    steady_call, whole_call = [call.kwargs for call in store.call_args_list]
    assert steady_call["mode"] == "steady_profiles"
    assert steady_call["profiles"] == ["Q100"]
    assert steady_call["inundation_boundary"] is True
    assert whole_call["mode"] == "selected"
    assert whole_call["profile"] == "Max"
    assert whole_call["map_types"] == ["arrival_time"]
    assert set(result) == {"depth", "inundation_boundary", "arrival_time"}


def test_invalid_unsteady_timestamp_fails_before_store_map_mutation(tmp_path):
    ras = _ras(tmp_path)

    with (
        patch(
            "ras2cng._ras_commander_maps.HdfResultsPlan.is_steady_plan",
            return_value=False,
        ),
        patch(
            "ras2cng._ras_commander_maps.RasProcess.get_plan_timestamps",
            return_value=["15SEP2026 12:00:00"],
        ),
        patch("ras2cng._ras_commander_maps.RasMap.store_all_maps") as store,
        pytest.raises(ValueError, match="was not found"),
    ):
        _generate(
            ras,
            tmp_path,
            profile="15SEP2026 13:00:00",
            map_types=["depth"],
        )

    store.assert_not_called()


def test_steady_summary_cannot_substitute_max(tmp_path):
    ras = _ras(tmp_path)
    wrong_summary = _steady_summary(tmp_path, "Max")

    with (
        patch(
            "ras2cng._ras_commander_maps.HdfResultsPlan.is_steady_plan",
            return_value=True,
        ),
        patch(
            "ras2cng._ras_commander_maps.HdfResultsPlan.get_steady_profile_names",
            return_value=["Q100"],
        ),
        patch(
            "ras2cng._ras_commander_maps.supports_exact_steady_profiles",
            return_value=True,
        ),
        patch(
            "ras2cng._ras_commander_maps.RasMap.store_all_maps",
            return_value=wrong_summary,
        ),
        pytest.raises(RuntimeError, match="exact requested profile"),
    ):
        _generate(
            ras,
            tmp_path,
            profile="Q100",
            map_types=["depth"],
        )


def test_whole_simulation_only_does_not_send_irrelevant_named_profile(tmp_path):
    ras = _ras(tmp_path)
    summary = _selected_summary(
        tmp_path,
        "duration",
        "Duration (0.5ft hrs)",
    )

    with (
        patch("ras2cng._ras_commander_maps.HdfResultsPlan.is_steady_plan") as is_steady,
        patch(
            "ras2cng._ras_commander_maps.RasMap.store_all_maps",
            return_value=summary,
        ) as store,
    ):
        result = _generate(
            ras,
            tmp_path,
            profile="Not Used",
            map_types=["duration"],
        )

    is_steady.assert_not_called()
    assert store.call_args.kwargs["profile"] == "Max"
    assert result["duration"]
