"""Independent geometric budget checks for named mining scenarios."""
import pytest

pytest.importorskip("shapely", minversion="2.1")
from shapely.geometry import Polygon, box

from vkm_world.geometry_build.material_partition import (
    MaterialInterval, PartitionSolid, audit_partition, partition_material_state,
)


def test_rock_is_removed_and_backfill_replaces_void_with_exact_budget():
    domain = Polygon([(0, 0), (10, 0), (10, 8), (0, 8)], holes=[[(1, 1), (1, 2), (2, 2), (2, 1)]])
    rooms = [box(3, 0, 4, 8), box(6, 0, 7, 8)]
    layers = [MaterialInterval("seam", 0, 6), MaterialInterval("floor_extension_scenario", 6, 6.4)]
    states = [partition_material_state(domain, rooms, layers, state=state, scenario_id="test",
                                       **({"backfill_fraction": .5} if state == "backfilled" else {}))
              for state in ("before", "excavated", "backfilled")]
    expected = 79*6.4
    for result in states:
        assert result["audit"]["valid_partition"]
        assert result["physical_date"] is None
        assert result["audit"]["overlap_volume_m3"] == pytest.approx(0)
        assert sum(result["audit"]["volume_by_role_m3"].values()) == pytest.approx(expected)
    assert states[0]["audit"]["volume_by_role_m3"]["MATERIAL"] == pytest.approx(expected)
    assert states[1]["audit"]["volume_by_role_m3"]["VOID"] == pytest.approx(16*6.4)
    assert states[2]["audit"]["volume_by_role_m3"]["BACKFILL"] == pytest.approx(16*3.2)
    assert states[2]["audit"]["volume_by_role_m3"]["VOID"] == pytest.approx(16*3.2)


def test_independent_audit_catches_balanced_overlap_and_gap():
    # Equal total volume alone would pass: half the block is double-filled and
    # the other half missing. The slab coverage audit must reject it.
    solids = [PartitionSolid("rock", "MATERIAL", box(0, 0, 1, 1), 0, 2),
              PartitionSolid("void", "VOID", box(0, 0, 1, 1), 0, 2)]
    result = audit_partition(box(0, 0, 2, 1), solids, 2)
    assert result["volume_balance_error_m3"] == 0
    assert result["overlap_volume_m3"] == 2
    assert result["missing_volume_m3"] == 2
    assert not result["valid_partition"]


def test_unknown_height_overlapping_rooms_and_implicit_fill_fail_closed():
    domain = box(0, 0, 10, 10)
    layers = [MaterialInterval("seam", 0, 6)]
    with pytest.raises(ValueError, match="fraction"):
        partition_material_state(domain, [box(1, 0, 2, 10)], layers, state="backfilled", scenario_id="test")
    with pytest.raises(ValueError, match="overlapping"):
        partition_material_state(domain, [box(1, 0, 3, 10), box(2, 0, 4, 10)], layers,
                                 state="excavated", scenario_id="test")
    with pytest.raises(ValueError, match="contiguous"):
        partition_material_state(domain, [box(1, 0, 2, 10)],
                                 [MaterialInterval("seam", 0, 6), MaterialInterval("extra", 6.1, 6.4)],
                                 state="excavated", scenario_id="test")


def test_full_backfill_has_no_zero_thickness_void():
    result = partition_material_state(box(0, 0, 10, 10), [box(1, 0, 2, 10)],
                                      [MaterialInterval("seam", 0, 6)], state="backfilled",
                                      scenario_id="full_fill", backfill_fraction=1.)
    assert all(s.role != "VOID" for s in result["solids"])
    assert result["audit"]["volume_by_role_m3"]["BACKFILL"] == 60
