"""Part 1 (interface capability gate), Part 2/2b (dependency batching + dedup),
Part 3 (path recompute) — integration tests.

Stand:  S1(switch) --- R(router L3 uplink)   and   S2(switch) --- R
Switch ports are TRUNK switchports; the router uplink is a routed L3 interface
that carries VLANs via dot1q subinterfaces.
"""

from __future__ import annotations

from uuid import uuid4

from src.models.operations import ActionType, BatchCriticality
from src.task_builder.builder import TaskBuilder
from src.task_builder.dependencies import batch_by_dependencies
from src.topology.graph import (
    L2_SWITCHPORT,
    L3_ROUTED,
    Topology,
    trunk_actions_for_link,
)

S1, S2, R = uuid4(), uuid4(), uuid4()


def _topo(*, allowed=(), members=(), subif_vlans=()):
    interfaces = [
        {"interface_id": "s1_up", "device_id": S1, "name": "Gi0/1", "mode": "TRUNK"},
        {"interface_id": "s1_acc", "device_id": S1, "name": "Gi0/2", "mode": "ACCESS", "access_vlan_id": None},
        {"interface_id": "s2_up", "device_id": S2, "name": "Gi0/1", "mode": "TRUNK"},
        {"interface_id": "r_up", "device_id": R, "name": "Gi0/0", "mode": None, "ip_address": "10.0.0.1"},
        {"interface_id": "r_up2", "device_id": R, "name": "Gi0/3", "mode": None, "ip_address": "10.0.1.1"},
    ]
    for v in subif_vlans:
        interfaces.append({
            "interface_id": f"r_sub_{v}", "device_id": R, "name": f"Gi0/0.{v}",
            "mode": None, "parent_interface_id": "r_up", "dot1q_vlan_id": v,
        })
    links = [("s1_up", "r_up"), ("s2_up", "r_up2")]
    trunk_allowed = [(i, v) for i, vlans in dict(allowed).items() for v in vlans]
    device_vlan = [(d, v) for v, devs in dict(members).items() for d in devs]
    return Topology.build(interfaces=interfaces, links=links, trunk_allowed=trunk_allowed, device_vlan=device_vlan)


# ---- Part 1: interface_kind --------------------------------------------------

def test_interface_kind_switchport_vs_routed_and_mixed():
    topo = _topo(subif_vlans=(10,))
    assert topo.interface_kind("s1_up") == L2_SWITCHPORT       # switchport trunk
    assert topo.interface_kind("s1_acc") == L2_SWITCHPORT      # switchport access
    assert topo.interface_kind("r_up") == L3_ROUTED            # routed uplink (has child + ip)
    assert topo.interface_kind("r_sub_10") == L3_ROUTED        # dot1q subinterface
    # R is a mixed-capable device: routed uplink here, but could host switchports too.
    kinds = {topo.interface_kind(i.interface_id) for i in topo.interfaces_of(R)}
    assert L3_ROUTED in kinds


# ---- Part 1: capability gate -------------------------------------------------

def test_gate_l2_trunk_gets_trunk_action_l3_gets_subinterface():
    topo = _topo()  # no subinterface for 10 yet
    actions = trunk_actions_for_link(topo, ("s1_up", "r_up"), 10, present=True)
    by_dev = {a.device_id: a.action_type for a in actions}
    assert by_dev[S1] == ActionType.EDIT_TRUNK            # L2 switchport -> trunk action
    assert by_dev[R] == ActionType.CREATE_SUBINTERFACE    # L3 routed -> subinterface action
    # never the wrong kind:
    assert all(not (a.device_id == R and a.action_type in (ActionType.SET_TRUNK, ActionType.EDIT_TRUNK)) for a in actions)
    assert all(not (a.device_id == S1 and a.action_type in (ActionType.CREATE_SUBINTERFACE, ActionType.DELETE_SUBINTERFACE)) for a in actions)


def test_gate_delete_l3_emits_delete_subinterface():
    topo = _topo(allowed={"s1_up": [10]}, subif_vlans=(10,))
    actions = trunk_actions_for_link(topo, ("s1_up", "r_up"), 10, present=False)
    by_dev = {a.device_id: a.action_type for a in actions}
    assert by_dev[S1] == ActionType.EDIT_TRUNK
    assert by_dev[R] == ActionType.DELETE_SUBINTERFACE


# ---- Part 2: dependency ordering + components --------------------------------

def _ordered_types(batch):
    return [a.action_type for a in batch.actions]


def test_create_subinterface_ordered_before_edit_trunk_to_router():
    topo = _topo(members={10: [R]})
    batches = TaskBuilder().expand_add_vlan(S1, 10, topology=topo)
    target = None
    for b in batches:
        types = _ordered_types(b)
        if ActionType.CREATE_SUBINTERFACE in types and ActionType.EDIT_TRUNK in types:
            target = b
            break
    assert target is not None, "create-subif and edit-trunk must share one batch"
    types = _ordered_types(target)
    assert types.index(ActionType.CREATE_SUBINTERFACE) < types.index(ActionType.EDIT_TRUNK)


def test_add_vlan_precedes_dependents_in_same_batch():
    topo = _topo(members={10: [R]})
    batches = TaskBuilder().expand_add_vlan(S1, 10, access_ports=["Gi0/2"], topology=topo)
    for b in batches:
        types = _ordered_types(b)
        if ActionType.ADD_VLAN in types:
            ai = types.index(ActionType.ADD_VLAN)
            assert all(ai < j for j, t in enumerate(types) if t in (ActionType.SET_ACCESS,))
            assert ActionType.SET_ACCESS in types


def test_delete_reverse_order_in_one_batch():
    topo = _topo(allowed={"s1_up": [10]}, members={10: [S1]}, subif_vlans=(10,))
    for info in topo.interfaces_of(S1):
        if info.mode == "ACCESS":
            info.access_vlan_id = 10
    batches = TaskBuilder().expand_delete_vlan(S1, 10, topology=topo)
    chain = None
    for b in batches:
        if ActionType.DELETE_VLAN in _ordered_types(b):
            chain = b
            break
    assert chain is not None
    types = _ordered_types(chain)
    assert types.index(ActionType.SET_ACCESS) < types.index(ActionType.EDIT_TRUNK)
    assert types.index(ActionType.EDIT_TRUNK) < types.index(ActionType.DELETE_SUBINTERFACE)
    assert types.index(ActionType.DELETE_SUBINTERFACE) < types.index(ActionType.DELETE_VLAN)


def test_independent_actions_go_to_separate_batches_no_cross_deps():
    from src.models.operations import Action
    a1 = Action(device_id=S1, action_type=ActionType.ADD_VLAN, params={"vlan_id": 10})
    a2 = Action(device_id=S2, action_type=ActionType.ADD_VLAN, params={"vlan_id": 20})
    batches = batch_by_dependencies([a1, a2], topology=None)
    assert len(batches) == 2
    assert all(len(b.actions) == 1 for b in batches)


# ---- Part 2b: prereq dedup ---------------------------------------------------

def test_standalone_edit_trunk_pulls_subinterface_prereq_once():
    from src.models.operations import Action
    topo = _topo(members={10: [R]})
    edit = Action(
        device_id=S1, action_type=ActionType.EDIT_TRUNK,
        params={"port": "Gi0/1", "allowed_vlans": [10]},
        previous_state={"allowed_vlans": []}, target_state={"allowed_vlans": [10]},
    )
    batches = batch_by_dependencies([edit], topology=topo)
    flat = [a for b in batches for a in b.actions]
    subif = [a for a in flat if a.action_type == ActionType.CREATE_SUBINTERFACE and int(a.params["vlan_id"]) == 10]
    assert len(subif) == 1
    for b in batches:
        types = [a.action_type for a in b.actions]
        if ActionType.CREATE_SUBINTERFACE in types and ActionType.EDIT_TRUNK in types:
            assert types.index(ActionType.CREATE_SUBINTERFACE) < types.index(ActionType.EDIT_TRUNK)


def test_expansion_does_not_duplicate_subinterface_prereq():
    topo = _topo(members={10: [R]})
    batches = TaskBuilder().expand_add_vlan(S1, 10, topology=topo)
    flat = [a for b in batches for a in b.actions]
    subif = [a for a in flat if a.action_type == ActionType.CREATE_SUBINTERFACE and int(a.params["vlan_id"]) == 10]
    assert len(subif) == 1


# ---- Part 3: path recompute --------------------------------------------------

def test_edit_trunk_remove_refuses_needed_link():
    topo = _topo(allowed={"s1_up": [10]}, members={10: [S1, R]})
    batches = TaskBuilder().edit_trunk_remove("s1_up", 10, members_after={S1, R}, topology=topo)
    assert batches == []


def test_edit_trunk_remove_allows_unneeded_link():
    topo = _topo(allowed={"s1_up": [10]}, members={10: []})
    batches = TaskBuilder().edit_trunk_remove("s1_up", 10, members_after=set(), topology=topo)
    flat = [a for b in batches for a in b.actions]
    assert any(a.action_type == ActionType.EDIT_TRUNK for a in flat)


def test_convert_trunk_to_access_blocked_when_link_needed():
    topo = _topo(allowed={"s1_up": [10]}, members={10: [S1, R]})
    batches = TaskBuilder().convert_port_to_access("s1_up", 1, topology=topo)
    assert batches == []


def test_convert_trunk_to_access_allowed_when_link_unneeded():
    topo = _topo(allowed={"s1_up": [10]}, members={10: []})
    batches = TaskBuilder().convert_port_to_access("s1_up", 1, topology=topo)
    flat = [a for b in batches for a in b.actions]
    assert any(a.action_type == ActionType.SET_ACCESS for a in flat)
