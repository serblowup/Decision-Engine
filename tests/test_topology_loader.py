"""Unit / integration tests for load_topology() (no live DB).

`_fetch_rows` is monkeypatched to return canned rows per query, so the loader
is exercised end-to-end against a mocked PostgreSQL.

Mock stand (tree):

    R --- S1 --- S2
"""

from __future__ import annotations

import logging
from uuid import uuid4

import src.db.network_state as ns
from src.models.operations import ActionType
from src.task_builder.builder import TaskBuilder

R, S1, S2 = uuid4(), uuid4(), uuid4()
# interfaces
R_G0 = uuid4()
S1_UP, S1_DN, S1_ACC = uuid4(), uuid4(), uuid4()
S2_UP, S2_ACC = uuid4(), uuid4()


def _rows(empty_links: bool = False, s1_up_mode: str = "TRUNK"):
    links = [] if empty_links else [
        {"interface_a_id": R_G0, "interface_b_id": S1_UP},
        {"interface_a_id": S1_DN, "interface_b_id": S2_UP},
    ]
    interfaces = [
        {"id": R_G0, "device_id": R, "name": "Gi0/0", "parent_interface_id": None, "ip_address": None, "dot1q_vlan_id": None},
        {"id": S1_UP, "device_id": S1, "name": "Gi0/1", "parent_interface_id": None, "ip_address": None, "dot1q_vlan_id": None},
        {"id": S1_DN, "device_id": S1, "name": "Gi0/2", "parent_interface_id": None, "ip_address": None, "dot1q_vlan_id": None},
        {"id": S1_ACC, "device_id": S1, "name": "Gi0/9", "parent_interface_id": None, "ip_address": None, "dot1q_vlan_id": None},
        {"id": S2_UP, "device_id": S2, "name": "Gi0/1", "parent_interface_id": None, "ip_address": None, "dot1q_vlan_id": None},
        {"id": S2_ACC, "device_id": S2, "name": "Gi0/10", "parent_interface_id": None, "ip_address": None, "dot1q_vlan_id": None},
    ]
    modes = [
        {"interface_id": R_G0, "mode": "TRUNK", "access_vlan_id": None},
        {"interface_id": S1_UP, "mode": s1_up_mode, "access_vlan_id": None},
        {"interface_id": S1_DN, "mode": "TRUNK", "access_vlan_id": None},
        {"interface_id": S1_ACC, "mode": "ACCESS", "access_vlan_id": 10},
        {"interface_id": S2_UP, "mode": "TRUNK", "access_vlan_id": None},
        {"interface_id": S2_ACC, "mode": "ACCESS", "access_vlan_id": 10},
    ]
    allowed = [
        {"interface_id": R_G0, "vlan_id": 10},
        {"interface_id": S1_UP, "vlan_id": 10},
    ]
    device_vlan = [
        {"device_id": R, "vlan_id": 10},
        {"device_id": S1, "vlan_id": 10},
    ]
    endpoints = [{"network_interface_id": S2_ACC}]
    return links, interfaces, modes, allowed, device_vlan, endpoints


def _install_fetch(monkeypatch, **kwargs):
    links, interfaces, modes, allowed, device_vlan, endpoints = _rows(**kwargs)

    async def fake_fetch(_pool, query, *params):
        if "FROM link" in query:
            return links
        if "endpoint_network_attachment" in query:
            return endpoints
        if "FROM device_interface" in query:
            return interfaces
        if "FROM interface_vlan" in query:
            return modes
        if "FROM trunk_allowed_vlan" in query:
            return allowed
        if "FROM device_vlan" in query:
            return device_vlan
        return []

    monkeypatch.setattr(ns, "_fetch_rows", fake_fetch)


async def test_load_topology_builds_graph_modes_members_endpoints(monkeypatch):
    _install_fetch(monkeypatch)
    topo = await ns.load_topology(object())

    assert topo is not None
    # devices and links
    assert topo.graph.number_of_nodes() == 3
    assert topo.graph.has_edge(R, S1)
    assert topo.graph.has_edge(S1, S2)
    # interface modes
    assert topo.interfaces[S1_UP].mode == "TRUNK"
    assert topo.interfaces[S2_ACC].mode == "ACCESS"
    assert topo.interfaces[S2_ACC].access_vlan_id == 10
    # current allowed lists
    assert topo.interfaces[R_G0].allowed_vlans == {10}
    # VLAN members
    assert topo.device_vlan_members[10] == {R, S1}
    # endpoint ports
    assert topo.interfaces[S2_ACC].is_endpoint is True
    assert topo.interfaces[S1_ACC].is_endpoint is False
    # peering
    assert topo.peer_interface(R_G0) == S1_UP


async def test_load_topology_drives_real_expansion(monkeypatch):
    _install_fetch(monkeypatch)
    topo = await ns.load_topology(object())

    builder = TaskBuilder()
    batches = builder.expand_add_vlan(S2, 10, access_ports=["Gi0/10"], topology=topo)
    flat = [a for b in batches for a in b.actions]
    types = [a.action_type for a in flat]
    # Real (expanded) behaviour, not legacy: trunks get provisioned along the path.
    assert ActionType.EDIT_TRUNK in types  # existing trunks -> EDIT_TRUNK
    trunk_ports = {(a.device_id, a.params["port"]) for a in flat if a.action_type == ActionType.EDIT_TRUNK}
    assert (S1, "Gi0/2") in trunk_ports  # s1_dn toward S2
    assert (S2, "Gi0/1") in trunk_ports  # s2_up


async def test_load_topology_empty_links_falls_back_to_none(monkeypatch, caplog):
    _install_fetch(monkeypatch, empty_links=True)
    with caplog.at_level(logging.WARNING):
        topo = await ns.load_topology(object())
    assert topo is None
    assert any("falling back to legacy" in r.message for r in caplog.records)

    # And TaskBuilder with this (None) topology emits legacy single actions.
    builder = TaskBuilder()
    add = builder.expand_add_vlan(S2, 10, topology=topo)
    delete = builder.expand_delete_vlan(S2, 10, topology=topo)
    assert [a.action_type for b in add for a in b.actions] == [ActionType.ADD_VLAN]
    assert [a.action_type for b in delete for a in b.actions] == [ActionType.DELETE_VLAN]


async def test_load_topology_link_table_error_falls_back(monkeypatch, caplog):
    async def boom(_pool, query, *params):
        raise RuntimeError("relation \"link\" does not exist")

    monkeypatch.setattr(ns, "_fetch_rows", boom)
    with caplog.at_level(logging.WARNING):
        topo = await ns.load_topology(object())
    assert topo is None
    assert any("link table unavailable" in r.message for r in caplog.records)


async def test_load_topology_partial_load_error_falls_back(monkeypatch, caplog):
    # link query succeeds, a later query fails -> graceful None
    async def fetch(_pool, query, *params):
        if "FROM link" in query:
            return [{"interface_a_id": R_G0, "interface_b_id": S1_UP}]
        raise RuntimeError("column missing")

    monkeypatch.setattr(ns, "_fetch_rows", fetch)
    with caplog.at_level(logging.WARNING):
        topo = await ns.load_topology(object())
    assert topo is None
    assert any("topology load failed" in r.message for r in caplog.records)
