"""Protected VLANs (vlan.is_protected) honoured across all four layers:
load, RuleBasedStrategy, ThresholdHeuristic (MERGE source), task_builder guard.

Invariant: DE produces no destructive action (DELETE_VLAN, trunk removal) for a
protected VLAN and never changes its placement. Source of truth: vlan.is_protected.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from uuid import uuid4

import src.db.network_state as ns
from src.models.network import Device, DeviceType, NetworkState, PortMode, VlanAssignment, VlanInfo
from src.models.operations import ActionType
from src.strategies.interface import NetworkContext
from src.strategies.rule_based import RuleBasedStrategy
from src.strategies.threshold import ThresholdHeuristic
from src.task_builder.builder import TaskBuilder
from src.topology.graph import Topology

R, S1, S2 = uuid4(), uuid4(), uuid4()
R_G0, S1_UP, S1_DN, S2_UP = uuid4(), uuid4(), uuid4(), uuid4()


def _topo(allowed, members, protected=()):
    interfaces = [
        {"interface_id": R_G0, "device_id": R, "name": "Gi0/0", "mode": "TRUNK"},
        {"interface_id": S1_UP, "device_id": S1, "name": "Gi0/1", "mode": "TRUNK"},
        {"interface_id": S1_DN, "device_id": S1, "name": "Gi0/2", "mode": "TRUNK"},
        {"interface_id": S2_UP, "device_id": S2, "name": "Gi0/1", "mode": "TRUNK"},
    ]
    links = [(R_G0, S1_UP), (S1_DN, S2_UP)]
    trunk_allowed = [(i, v) for i, vlans in allowed.items() for v in vlans]
    device_vlan = [(d, v) for v, devs in members.items() for d in devs]
    return Topology.build(
        interfaces=interfaces, links=links, trunk_allowed=trunk_allowed,
        device_vlan=device_vlan, protected_vlans=protected,
    )


def _ctx(topo, state=None):
    return NetworkContext(state=state, metrics=[], timestamp="t", topology=topo)


def _set_trunks(decision):
    out = {}
    for b in decision.batches:
        for a in b.actions:
            if a.action_type in (ActionType.SET_TRUNK, ActionType.EDIT_TRUNK):
                out[(a.device_id, a.params["port"])] = set(a.params["allowed_vlans"])
    return out


# ---- Layer 1: load ---------------------------------------------------------

async def test_load_populates_protected_set(monkeypatch):
    rows = {
        "vlan": [
            {"vlan_id": 10, "name": "users", "admin_status": "ACTIVE", "oper_status": "UP", "is_protected": False},
            {"vlan_id": 998, "name": "native", "admin_status": "ACTIVE", "oper_status": "UP", "is_protected": True},
            {"vlan_id": 20, "name": "x", "admin_status": "ACTIVE", "oper_status": "UP", "is_protected": None},
        ],
        "device": [{"id": R, "hostname": "r", "mgmt_ip": "1", "device_type": "ROUTER", "status": "ACTIVE"}],
    }

    async def fake_fetch(_pool, query, *params):
        if "AS port" in query or "trunk_allowed_vlan tav" in query:
            return []  # access / trunk assignment joins
        if "hostname" in query:
            return rows["device"]
        if "FROM vlan" in query:
            return rows["vlan"]
        return []

    async def fake_dv(_pool):
        return set()

    monkeypatch.setattr(ns, "_fetch_rows", fake_fetch)
    monkeypatch.setattr(ns, "get_device_vlans", fake_dv)
    state = await ns.get_network_state(object())
    assert state.protected_vlans == {998}  # TRUE only; FALSE and NULL excluded


async def test_load_topology_reads_protected(monkeypatch):
    async def fake_fetch(_pool, query, *params):
        if "FROM link" in query:
            return [{"interface_a_id": R_G0, "interface_b_id": S1_UP}]
        if "WHERE COALESCE(is_protected" in query:
            return [{"vlan_id": 998}]
        if "FROM device_interface" in query:
            return [
                {"id": R_G0, "device_id": R, "name": "Gi0/0", "parent_interface_id": None, "ip_address": None, "dot1q_vlan_id": None},
                {"id": S1_UP, "device_id": S1, "name": "Gi0/1", "parent_interface_id": None, "ip_address": None, "dot1q_vlan_id": None},
            ]
        return []

    monkeypatch.setattr(ns, "_fetch_rows", fake_fetch)
    topo = await ns.load_topology(object())
    assert topo is not None
    assert topo.protected_vlans == {998}


# ---- Layer 2: RuleBased ----------------------------------------------------

def test_group2_protected_memberless_not_pruned():
    # 998 sits on a trunk, has no members -> protected keeps it (not pruned).
    topo = _topo(allowed={R_G0: [998], S1_UP: [998]}, members={}, protected=[998])
    decision = RuleBasedStrategy().decide(_ctx(topo))
    # 998 is protected and memberless -> kept (not pruned)
    assert decision.batches == []
    assert decision.decisions == []


def test_group2_unprotected_memberless_is_pruned():
    # same shape but NOT protected -> 20 (memberless) pruned (regression intact)
    topo = _topo(allowed={R_G0: [20], S1_UP: [20]}, members={})
    decision = RuleBasedStrategy().decide(_ctx(topo))
    trunks = _set_trunks(decision)
    assert trunks[(R, "Gi0/0")] == set()
    assert trunks[(S1, "Gi0/1")] == set()


def test_group1_protected_asymmetry_not_aligned():
    # protected 998 asymmetric on a needed link -> left as-is (hands-off)
    topo = _topo(allowed={R_G0: [998]}, members={998: [R, S1]}, protected=[998])
    decision = RuleBasedStrategy().decide(_ctx(topo))
    assert decision.batches == []  # not aligned


def test_group1_unprotected_asymmetry_is_aligned():
    topo = _topo(allowed={R_G0: [10]}, members={10: [R, S1]})
    decision = RuleBasedStrategy().decide(_ctx(topo))
    trunks = _set_trunks(decision)
    assert 10 in trunks[(S1, "Gi0/1")]


# ---- Layer 3: ThresholdHeuristic (MERGE source) ----------------------------

class _Dummy:
    def optimize(self, model, constraints, current):
        from src.segmenters.interface import SegmentationResult
        return SegmentationResult(dict(current), model.evaluate(current), 1, "dummy", 0.0)
    def get_name(self): return "dummy"
    def get_complexity(self): return "O(1)"


def _state(protected=()):
    d1, d2 = uuid4(), uuid4()
    return NetworkState(
        devices=[
            Device(id=d1, hostname="a", mgmt_ip="1", device_type=DeviceType.SWITCH, status="ACTIVE"),
            Device(id=d2, hostname="b", mgmt_ip="2", device_type=DeviceType.SWITCH, status="ACTIVE"),
        ],
        vlans=[VlanInfo(vlan_id=20, name="x", admin_status="ACTIVE", oper_status="UP")],
        assignments=[
            VlanAssignment(device_id=d1, port="Gi0/1", vlan_id=20, mode=PortMode.ACCESS),
            VlanAssignment(device_id=d2, port="Gi0/2", vlan_id=20, mode=PortMode.ACCESS),
        ],
        device_vlans={(d1, 20), (d2, 20)},
        protected_vlans=set(protected),
        timestamp=datetime.now(timezone.utc),
    )


def test_merge_skipped_for_protected_source(caplog):
    strat = ThresholdHeuristic(3.0, 0.85, _Dummy())
    state = _state(protected={20})
    ctx = NetworkContext(state=state, metrics=[{"vlan_id": 20, "inter_vlan_ratio": 0.8, "bytes_per_sec": 50_000_000.0}], timestamp="t")
    with caplog.at_level(logging.WARNING):
        decision = strat.decide(ctx)
    assert decision.decisions == []  # no MERGE produced
    assert any("merge skipped" in r.message and "protected" in r.message for r in caplog.records)


def test_merge_works_for_unprotected_source():
    strat = ThresholdHeuristic(3.0, 0.85, _Dummy())
    state = _state(protected=set())
    ctx = NetworkContext(state=state, metrics=[{"vlan_id": 20, "inter_vlan_ratio": 0.8, "bytes_per_sec": 50_000_000.0}], timestamp="t")
    decision = strat.decide(ctx)
    assert [d.decision_type for d in decision.decisions] == ["MERGE"]


# ---- Layer 4: task_builder guards ------------------------------------------

def test_expand_delete_blocked_for_protected(caplog):
    topo = _topo(allowed={S2_UP: [998], S1_DN: [998]}, members={998: [R]}, protected=[998])
    with caplog.at_level(logging.WARNING):
        batches = TaskBuilder().expand_delete_vlan(S2, 998, topology=topo)
    assert batches == []
    assert any("DELETE_VLAN blocked" in r.message for r in caplog.records)


def test_expand_delete_works_for_unprotected():
    topo = _topo(allowed={S2_UP: [10], S1_DN: [10]}, members={10: [R, S2]})
    batches = TaskBuilder().expand_delete_vlan(S2, 10, topology=topo)
    types = [a.action_type for b in batches for a in b.actions]
    assert ActionType.DELETE_VLAN in types


def test_trunk_removal_blocked_for_protected(caplog):
    from src.topology.graph import trunk_actions_for_link
    topo = _topo(allowed={S2_UP: [998], S1_DN: [998]}, members={}, protected=[998])
    with caplog.at_level(logging.WARNING):
        actions = trunk_actions_for_link(topo, (S1_DN, S2_UP), 998, present=False)
    assert actions == []
    assert any("trunk removal blocked" in r.message for r in caplog.records)
