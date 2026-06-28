"""RuleBasedStrategy: trunk symmetry (group 1) and Steiner minimality (group 2),
plus the single-source-of-truth contract with the SET_TRUNK expansion.

Tree:  R --- S1 --- S2
"""

from __future__ import annotations

from uuid import uuid4

from src.models.operations import ActionType
from src.strategies.interface import NetworkContext
from src.strategies.rule_based import RuleBasedStrategy
from src.task_builder.builder import TaskBuilder
from src.topology.graph import Topology, _norm_link, required_links

R, S1, S2 = uuid4(), uuid4(), uuid4()
R_G0, S1_UP, S1_DN, S2_UP = "r_g0", "s1_up", "s1_dn", "s2_up"


def _topo(allowed, members):
    interfaces = [
        {"interface_id": R_G0, "device_id": R, "name": "Gi0/0", "mode": "TRUNK"},
        {"interface_id": S1_UP, "device_id": S1, "name": "Gi0/1", "mode": "TRUNK"},
        {"interface_id": S1_DN, "device_id": S1, "name": "Gi0/2", "mode": "TRUNK"},
        {"interface_id": S2_UP, "device_id": S2, "name": "Gi0/1", "mode": "TRUNK"},
    ]
    links = [(R_G0, S1_UP), (S1_DN, S2_UP)]
    trunk_allowed = [(iface, v) for iface, vlans in allowed.items() for v in vlans]
    device_vlan = [(d, v) for v, devs in members.items() for d in devs]
    return Topology.build(interfaces=interfaces, links=links, trunk_allowed=trunk_allowed, device_vlan=device_vlan)


def _ctx(topo):
    return NetworkContext(state=None, metrics=[], timestamp="t", topology=topo)


def _set_trunks(decision):
    out = {}
    for b in decision.batches:
        for a in b.actions:
            if a.action_type in (ActionType.SET_TRUNK, ActionType.EDIT_TRUNK):
                out[(a.device_id, a.params["port"])] = set(a.params["allowed_vlans"])
    return out


def test_required_links_is_single_source_of_truth():
    topo = _topo(allowed={R_G0: [10], S1_UP: [10]}, members={10: [R, S1]})
    rl = required_links(topo, 10, topo.device_vlan_members[10])
    assert rl == topo.steiner_links({R, S1})
    assert rl == {_norm_link(R_G0, S1_UP)}


def test_group1_asymmetric_trunk_on_needed_link_is_aligned():
    # VLAN 10 needed on R-S1 (members R,S1) but present only on R end -> black hole.
    topo = _topo(allowed={R_G0: [10]}, members={10: [R, S1]})
    decision = RuleBasedStrategy().decide(_ctx(topo))
    trunks = _set_trunks(decision)
    # the missing end gains 10; both ends of the needed link now carry 10
    assert 10 in trunks[(S1, "Gi0/1")]
    # idempotent: the already-correct end is not re-emitted
    assert (R, "Gi0/0") not in trunks


def test_group2_prunes_excess_keeps_needed():
    # 10 needed only on R-S1; it has leaked onto the S1-S2 link (no members on S2).
    topo = _topo(
        allowed={R_G0: [10], S1_UP: [10], S1_DN: [10], S2_UP: [10]},
        members={10: [R, S1]},
    )
    decision = RuleBasedStrategy().decide(_ctx(topo))
    trunks = _set_trunks(decision)
    # excess pruned on the S1-S2 link (both ends)
    assert trunks[(S1, "Gi0/2")] == set()   # s1_dn
    assert trunks[(S2, "Gi0/1")] == set()   # s2_up
    # needed link untouched (no SET_TRUNK emitted for already-correct interfaces)
    assert (R, "Gi0/0") not in trunks
    assert (S1, "Gi0/1") not in trunks


def test_group2_vlan_with_no_members_is_fully_removed():
    # VLAN 20 sits on a trunk but no device is a member -> pure excess everywhere.
    topo = _topo(allowed={R_G0: [10, 20], S1_UP: [10, 20]}, members={10: [R, S1]})
    decision = RuleBasedStrategy().decide(_ctx(topo))
    trunks = _set_trunks(decision)
    # 20 removed, 10 retained on both ends of the needed link
    assert trunks[(R, "Gi0/0")] == {10}
    assert trunks[(S1, "Gi0/1")] == {10}


def test_consistent_with_expansion_provisioning():
    # The interfaces RuleBased keeps a VLAN on must equal what expand_add provisions,
    # because both derive placement from required_links.
    topo = _topo(allowed={R_G0: [10]}, members={10: [R, S1]})
    rb = RuleBasedStrategy().decide(_ctx(topo))
    rb_trunks_with_10 = {k for k, v in _set_trunks(rb).items() if 10 in v}
    # expand_add of 10 toward S1 provisions the same needed link interfaces
    add = TaskBuilder().expand_add_vlan(S1, 10, topology=topo)
    add_trunks = {
        (a.device_id, a.params["port"])
        for b in add for a in b.actions
        if a.action_type in (ActionType.SET_TRUNK, ActionType.EDIT_TRUNK)
    }
    # RuleBased adds 10 to s1_up; expand_add would add 10 where missing on the path.
    assert (S1, "Gi0/1") in rb_trunks_with_10
    assert (S1, "Gi0/1") in add_trunks


def test_no_topology_returns_empty():
    decision = RuleBasedStrategy().decide(NetworkContext(state=None, metrics=[], timestamp="t", topology=None))
    assert decision.decisions == []
    assert decision.batches == []


def test_clean_fabric_no_actions():
    topo = _topo(allowed={R_G0: [10], S1_UP: [10]}, members={10: [R, S1]})
    decision = RuleBasedStrategy().decide(_ctx(topo))
    assert decision.decisions == []
    assert decision.batches == []
