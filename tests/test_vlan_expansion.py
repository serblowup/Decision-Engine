"""Topology-aware expansion of ADD_VLAN / DELETE_VLAN.

Mock stand: a tree rooted at a router.

    R --- S1 --- S2
           |
           S3

Inter-switch ports are TRUNK, so allowed-list changes emit EDIT_TRUNK. An ACCESS
inter-switch port that must carry a VLAN is converted with SET_TRUNK.
"""

from __future__ import annotations

from uuid import uuid4

from src.models.operations import ActionType
from src.task_builder.builder import TaskBuilder
from src.topology.graph import Topology

R, S1, S2, S3 = uuid4(), uuid4(), uuid4(), uuid4()
TRUNK_ACTIONS = (ActionType.SET_TRUNK, ActionType.EDIT_TRUNK)


def _topology(trunk_allowed=(), device_vlan=(), s2_access=("Gi0/10",), s1_access=("Gi0/9",),
              s1_up_mode="TRUNK"):
    interfaces = [
        {"interface_id": "r_g0", "device_id": R, "name": "Gi0/0", "mode": "TRUNK"},
        {"interface_id": "s1_up", "device_id": S1, "name": "Gi0/1", "mode": s1_up_mode},
        {"interface_id": "s1_d2", "device_id": S1, "name": "Gi0/2", "mode": "TRUNK"},
        {"interface_id": "s1_d3", "device_id": S1, "name": "Gi0/3", "mode": "TRUNK"},
        {"interface_id": "s2_up", "device_id": S2, "name": "Gi0/1", "mode": "TRUNK"},
        {"interface_id": "s3_up", "device_id": S3, "name": "Gi0/1", "mode": "TRUNK"},
    ]
    for i, p in enumerate(s1_access):
        interfaces.append({"interface_id": f"s1_a{i}", "device_id": S1, "name": p, "mode": "ACCESS", "access_vlan_id": None})
    for i, p in enumerate(s2_access):
        interfaces.append({"interface_id": f"s2_a{i}", "device_id": S2, "name": p, "mode": "ACCESS", "access_vlan_id": None})
    links = [("r_g0", "s1_up"), ("s1_d2", "s2_up"), ("s1_d3", "s3_up")]
    return Topology.build(interfaces=interfaces, links=links, trunk_allowed=trunk_allowed, device_vlan=device_vlan)


def _flatten(batches):
    return [a for b in batches for a in b.actions]


def _types(batches):
    return [a.action_type for a in _flatten(batches)]


def test_add_vlan_no_other_members_only_adds_and_access():
    topo = _topology(device_vlan=[])
    batches = TaskBuilder().expand_add_vlan(S2, 50, access_ports=["Gi0/10"], topology=topo)
    types = _types(batches)
    assert ActionType.ADD_VLAN in types
    assert ActionType.SET_ACCESS in types
    assert ActionType.EDIT_TRUNK not in types and ActionType.SET_TRUNK not in types
    assert types.index(ActionType.ADD_VLAN) < types.index(ActionType.SET_ACCESS)


def test_add_vlan_leaf_sets_paired_trunks_along_path():
    # VLAN 10 already lives on R; add it to leaf S2. Path R-S1-S2. All trunks exist.
    topo = _topology(device_vlan=[(R, 10)])
    batches = TaskBuilder().expand_add_vlan(S2, 10, access_ports=["Gi0/10"], topology=topo)
    trunk_ports = {(a.device_id, a.params["port"]) for a in _flatten(batches) if a.action_type in TRUNK_ACTIONS}
    assert (R, "Gi0/0") in trunk_ports
    assert (S1, "Gi0/1") in trunk_ports
    assert (S1, "Gi0/2") in trunk_ports
    assert (S2, "Gi0/1") in trunk_ports
    assert len(trunk_ports) == 4
    # existing trunks -> EDIT_TRUNK with full allowed list including 10
    for a in _flatten(batches):
        if a.action_type in TRUNK_ACTIONS:
            assert a.action_type == ActionType.EDIT_TRUNK
            assert 10 in a.params["allowed_vlans"]
            # EDIT_TRUNK states carry only allowed_vlans (no mode)
            assert "mode" not in a.previous_state
            assert "mode" not in a.target_state
    types = _types(batches)
    assert types.index(ActionType.ADD_VLAN) < types.index(ActionType.EDIT_TRUNK) < types.index(ActionType.SET_ACCESS)


def test_add_vlan_idempotent_when_already_allowed():
    allowed = [("r_g0", 10), ("s1_up", 10), ("s1_d2", 10), ("s2_up", 10)]
    topo = _topology(trunk_allowed=allowed, device_vlan=[(R, 10)])
    batches = TaskBuilder().expand_add_vlan(S2, 10, access_ports=[], topology=topo)
    assert ActionType.EDIT_TRUNK not in _types(batches)
    assert ActionType.SET_TRUNK not in _types(batches)
    assert _types(batches) == [ActionType.ADD_VLAN]


def test_add_vlan_converts_access_inter_switch_port():
    # s1_up is an ACCESS inter-switch port that must carry the VLAN -> SET_TRUNK conversion.
    topo = _topology(device_vlan=[(R, 10)], s1_up_mode="ACCESS")
    batches = TaskBuilder().expand_add_vlan(S2, 10, topology=topo)
    by_port = {(a.device_id, a.params["port"]): a for a in _flatten(batches) if a.action_type in TRUNK_ACTIONS}
    conv = by_port[(S1, "Gi0/1")]  # the ACCESS end
    assert conv.action_type == ActionType.SET_TRUNK
    assert conv.previous_state["mode"] == "ACCESS"
    assert conv.target_state["mode"] == "TRUNK"
    assert 10 in conv.params["allowed_vlans"]
    # the genuine trunk ends are EDIT_TRUNK
    assert by_port[(R, "Gi0/0")].action_type == ActionType.EDIT_TRUNK


def test_delete_vlan_evacuates_all_access_first_then_prune_then_delete():
    topo = _topology(
        trunk_allowed=[("s2_up", 10), ("s1_d2", 10), ("s1_up", 10), ("r_g0", 10)],
        device_vlan=[(R, 10), (S2, 10)],
        s2_access=("Gi0/10", "Gi0/11", "Gi0/12"),
    )
    for info in topo.interfaces_of(S2):
        if info.mode == "ACCESS":
            info.access_vlan_id = 10
    batches = TaskBuilder().expand_delete_vlan(S2, 10, topology=topo)
    flat = _flatten(batches)
    types = [a.action_type for a in flat]
    access_idx = [i for i, t in enumerate(types) if t == ActionType.SET_ACCESS]
    assert len(access_idx) == 3
    assert all(flat[i].params["vlan_id"] == 1 for i in access_idx)
    assert max(access_idx) < types.index(ActionType.DELETE_VLAN)
    if ActionType.EDIT_TRUNK in types:
        assert max(access_idx) < types.index(ActionType.EDIT_TRUNK)
        assert types.index(ActionType.EDIT_TRUNK) < types.index(ActionType.DELETE_VLAN)
    assert types[-1] == ActionType.DELETE_VLAN


def test_delete_vlan_leaf_prunes_both_ends():
    topo = _topology(
        trunk_allowed=[("s2_up", 10), ("s1_d2", 10), ("s1_up", 10), ("r_g0", 10)],
        device_vlan=[(R, 10), (S2, 10)],
    )
    batches = TaskBuilder().expand_delete_vlan(S2, 10, topology=topo)
    pruned = {(a.device_id, a.params["port"]) for a in _flatten(batches) if a.action_type in TRUNK_ACTIONS}
    assert (S2, "Gi0/1") in pruned   # s2_up
    assert (S1, "Gi0/2") in pruned   # s1_d2 peer
    for a in _flatten(batches):
        if a.action_type in TRUNK_ACTIONS:
            assert a.action_type == ActionType.EDIT_TRUNK   # existing trunks
            assert 10 not in a.params["allowed_vlans"]
    assert _types(batches)[-1] == ActionType.DELETE_VLAN


def test_topology_getter_injection_is_used():
    topo = _topology(device_vlan=[(R, 10)])
    builder = TaskBuilder(topology_getter=lambda: topo)
    batches = builder.expand_add_vlan(S2, 10)
    assert ActionType.EDIT_TRUNK in _types(batches)
