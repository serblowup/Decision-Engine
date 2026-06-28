"""SET_TRUNK vs EDIT_TRUNK split.

SET_TRUNK  = ACCESS -> TRUNK conversion (mode change).
EDIT_TRUNK = allowed-list change on an already-trunk port (mode unchanged).
The choice is made per port from its CURRENT mode in Topology.
"""

from __future__ import annotations

from uuid import uuid4

from src.models.operations import ActionType
from src.topology.graph import Topology, build_trunk_action, trunk_actions_for_link

DEV = uuid4()


def _iface(topo, iface_id):
    return topo.interfaces[iface_id]


def _topo(a_mode="TRUNK", a_allowed=(10,), a_access=None):
    interfaces = [
        {"interface_id": "ifa", "device_id": DEV, "name": "Gi0/1", "mode": a_mode, "access_vlan_id": a_access},
        {"interface_id": "ifb", "device_id": uuid4(), "name": "Gi0/1", "mode": "TRUNK"},
    ]
    links = [("ifa", "ifb")]
    trunk_allowed = [("ifa", v) for v in a_allowed]
    return Topology.build(interfaces=interfaces, links=links, trunk_allowed=trunk_allowed)


def test_trunk_port_allowed_change_emits_edit_trunk():
    topo = _topo(a_mode="TRUNK", a_allowed=(10,))
    action = build_trunk_action(_iface(topo, "ifa"), {10, 20})
    assert action.action_type == ActionType.EDIT_TRUNK
    assert action.previous_state == {"allowed_vlans": [10]}
    assert action.target_state == {"allowed_vlans": [10, 20]}
    assert action.params == {"port": "Gi0/1", "allowed_vlans": [10, 20]}


def test_access_port_becomes_trunk_emits_set_trunk():
    topo = _topo(a_mode="ACCESS", a_allowed=(), a_access=99)
    action = build_trunk_action(_iface(topo, "ifa"), {10})
    assert action.action_type == ActionType.SET_TRUNK
    assert action.previous_state == {"mode": "ACCESS", "vlan_id": 99}
    assert action.target_state == {"mode": "TRUNK", "allowed_vlans": [10]}


def test_set_trunk_never_emitted_for_already_trunk():
    topo = _topo(a_mode="TRUNK", a_allowed=(10,))
    actions = trunk_actions_for_link(topo, ("ifa", "ifb"), 20, present=True)
    assert actions
    assert all(a.action_type == ActionType.EDIT_TRUNK for a in actions)
    assert all(a.action_type != ActionType.SET_TRUNK for a in actions)


def test_idempotent_no_action_when_target_equals_current():
    topo = _topo(a_mode="TRUNK", a_allowed=(10,))
    actions = trunk_actions_for_link(topo, ("ifa", "ifb"), 10, present=True)
    ifa_actions = [a for a in actions if a.params["port"] == "Gi0/1" and a.device_id == DEV]
    assert ifa_actions == []


def test_access_inter_switch_port_converted_on_add():
    topo = _topo(a_mode="ACCESS", a_allowed=(), a_access=5)
    actions = trunk_actions_for_link(topo, ("ifa", "ifb"), 10, present=True)
    ifa = [a for a in actions if a.device_id == DEV][0]
    assert ifa.action_type == ActionType.SET_TRUNK
    assert ifa.target_state["mode"] == "TRUNK"
    assert 10 in ifa.params["allowed_vlans"]
