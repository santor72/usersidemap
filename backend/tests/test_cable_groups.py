import pytest
from test_geometry import MemoryStore

from map_sync.cable_groups import assemble_group, vertex_count
from map_sync.geometry import GeometryBuilder


class StyleStub:
    def cable(self, row):
        return {"color": "#000080", "width": 5}


def store(extra=None):
    records = {
        ("node", 1): {"geometry": {"type": "Point", "coordinates": [37, 55]}},
        ("node", 2): {"geometry": {"type": "Point", "coordinates": [37.001, 55]}},
        ("cable", 10): {"code": 10, "uzelcode1": 1, "uzelcode2": 2, "cable_line_type_id": 1},
        ("cable_type", 1): {"is_on_map": 1},
        ("group_member_children", 50): [{"volscode": 10}],
    }
    records.update(extra or {})
    return MemoryStore(records)


def assemble(records):
    return assemble_group(records, GeometryBuilder(records, "lon_lat"), StyleStub(), {"code": 50})


def test_group_visibility_and_empty_group():
    result = assemble(store())
    assert not result["hidden"]
    assert result["geometry"]["type"] == "MultiLineString"
    assert result["zoom"] == 13
    assert vertex_count(result["geometry"]) == 2
    assert assemble(store({("group_member_children", 50): []})) is None
    hidden = assemble(store({("cable_type", 1): {"is_on_map": 0}}))
    assert hidden["hidden"]
    assert hidden["properties"]["cableTypeIds"] == []


@pytest.mark.parametrize(
    "extra,code",
    [
        ({("cable", 10): None}, "missing_group_member"),
        ({("cable_type", 1): None}, "missing_cable_type"),
        ({("node", 2): None}, "missing_node"),
    ],
)
def test_bad_members_are_reported_without_blocking(extra, code):
    records = store(extra)
    issues = []
    result = assemble_group(
        records,
        GeometryBuilder(records, "lon_lat"),
        StyleStub(),
        {"code": 50},
        on_issue=issues.append,
    )
    assert result is None
    assert issues[0].code == code


def chain_records():
    return store(
        {
            ("node", 3): {},
            ("node", 4): {},
            ("node", 5): {"geometry": {"type": "Point", "coordinates": [37.004, 55]}},
            ("node", 6): {"geometry": {"type": "Point", "coordinates": [37.005, 55]}},
            ("cable", 40): {"code": 40, "uzelcode1": 2, "uzelcode2": 3, "cable_line_type_id": 1},
            ("cable", 20): {"code": 20, "uzelcode1": 4, "uzelcode2": 3, "cable_line_type_id": 1},
            ("cable", 30): {"code": 30, "uzelcode1": 4, "uzelcode2": 5, "cable_line_type_id": 1},
            ("cable", 50): {"code": 50, "uzelcode1": 6, "uzelcode2": 5, "cable_line_type_id": 1},
            ("group_member_children", 50): [{"volscode": i} for i in [50, 30, 10, 20, 40]],
        }
    )


@pytest.mark.parametrize("policy", ["bridge", "drop"])
def test_consecutive_failures_follow_topology_not_member_id_order(policy):
    records = chain_records()
    issues = []
    result = assemble_group(
        records,
        GeometryBuilder(records, "lon_lat"),
        StyleStub(),
        {"code": 50},
        error_policy=policy,
        on_issue=issues.append,
    )
    assert len(issues) == 3
    props = result["properties"]
    if policy == "bridge":
        assert props["reconstructedMemberIds"] == [20, 30, 40]
        assert props["droppedMemberIds"] == []
        assert props["geometryComplete"]
        assert result["geometry"]["coordinates"][-1] == [[37.001, 55], [37.004, 55]]
        assert len(result["geometry"]["coordinates"]) == 3
    else:
        assert props["reconstructedMemberIds"] == []
        assert props["droppedMemberIds"] == [20, 30, 40]
        assert not props["geometryComplete"]
        assert len(result["geometry"]["coordinates"]) == 2


def test_bridge_uses_known_endpoints_when_custom_route_is_broken():
    records = store({("place_children", 10): [{"mapcode": 3, "x1": 999, "y1": 55}]})
    result = assemble(records)
    assert result["properties"]["reconstructedMemberIds"] == [10]
    assert result["geometry"]["coordinates"] == [[[37, 55], [37.001, 55]]]


def test_bridge_does_not_invent_a_terminal_endpoint():
    records = chain_records()
    records.records[("group_member_children", 50)] = [{"volscode": 10}, {"volscode": 40}]
    result = assemble(records)
    assert result["properties"]["droppedMemberIds"] == [40]
    assert result["properties"]["bridges"] == []


def test_unknown_branch_is_not_connected_arbitrarily():
    records = chain_records()
    records.records[("cable", 20)]["uzelcode1"] = 6
    records.records[("cable", 30)]["uzelcode1"] = 3
    result = assemble(records)
    assert result["properties"]["bridges"] == []
    assert result["properties"]["droppedMemberIds"] == [20, 30, 40]


def test_zero_ids_cannot_connect_unrelated_failed_cables():
    records = chain_records()
    records.records[("group_member_children", 50)] = [
        {"volscode": 10},
        {"volscode": 40},
        {"volscode": 30},
    ]
    records.records[("cable", 40)]["uzelcode2"] = 0
    records.records[("cable", 30)]["uzelcode1"] = 0
    result = assemble(records)
    assert result["properties"]["bridges"] == []
    assert result["properties"]["droppedMemberIds"] == [30, 40]


@pytest.mark.parametrize("policy", ["bridge", "drop"])
def test_policy_loads_from_env_file(tmp_path, monkeypatch, policy):
    from map_sync.config import Config

    monkeypatch.delenv("US_CABLE_GROUP_ERROR_POLICY", raising=False)
    monkeypatch.setenv("MAP_DB_URL", "postgresql://unused/test")
    env_file = tmp_path / ".env"
    env_file.write_text(f"US_CABLE_GROUP_ERROR_POLICY={policy}\n")
    assert Config.load(env_file, require_source=False).cable_group_error_policy == policy


def test_invalid_policy_is_rejected():
    from map_sync.config import Config, SyncError

    with pytest.raises(SyncError, match="bridge or drop"):
        Config({}, "unused", cable_group_error_policy="typo")
