"""Rule-based, topology-driven strategy (groups 1-2).

Unlike :class:`ThresholdHeuristic`, which optimises a traffic objective, this
strategy enforces *deterministic graph invariants* on the trunk fabric with
provable properties. It owns no objective function; it reconciles the actual
``allowed_vlan`` configuration of inter-switch trunks against the canonical
"correct placement" defined by :func:`required_links` (the single source of
truth, a Steiner tree over a VLAN's members).

Two invariants are enforced:

Group 1 — trunk symmetry (necessary condition of 802.1Q forwarding).
    For a physical link ``L = (a, b)`` and VLAN ``V``, the configuration is
    *consistent* iff ``V in allowed(a) <=> V in allowed(b)``. An 802.1Q frame
    tagged ``V`` is forwarded across a trunk only when ``V`` is in the allowed
    list on *both* ends; an asymmetric link (``V`` on one end only) is therefore
    a black hole / one-way reachability defect. The repair aligns both ends; the
    direction of alignment is taken from :func:`required_links` — if ``L`` is on
    the Steiner tree of ``V`` then ``V`` must be present on both ends, otherwise
    it is removed from both.

Group 2 — Steiner minimality (least privilege).
    For a VLAN ``V``, any trunk that carries ``V`` but does not belong to
    ``required_links(V, members(V))`` is *excess reachability*: ``V`` is present
    in a segment that contains none of its members, which is needless attack
    surface. Formally the correction is the symmetric difference between the
    actual allowed-set (expressed as links) and the Steiner-required set; the
    rule prunes ``V`` from the excess links (paired on both ends). This minimises
    each VLAN's footprint down to exactly its members — the principle of least
    privilege.

Both invariants are realised by a single per-interface reconciliation: for every
inter-switch trunk interface the *desired* allowed-set is
``{ V : link(interface) in required_links(V, members(V)) }`` and a SET_TRUNK is
emitted whenever the desired set differs from the actual one. Because the desired
set is derived per link, the two ends of any link always converge to the same
membership (group 1), and a VLAN is kept only on links it needs (group 2).
"""

from __future__ import annotations

import logging
from collections import defaultdict

from src.models.operations import Batch, BatchCriticality
from src.strategies.interface import NetworkContext, StrategyDecision, VlanDecision
from src.task_builder.dependencies import batch_by_dependencies
from src.topology.graph import TRUNK, Topology, _norm_link, build_trunk_action, required_links

logger = logging.getLogger(__name__)


class RuleBasedStrategy:
    def __init__(self, topology_getter=None) -> None:
        self._topology_getter = topology_getter

    def get_name(self) -> str:
        return "RuleBased"

    def get_version(self) -> str:
        return "1.0.0"

    def _resolve_topology(self, ctx: NetworkContext) -> Topology | None:
        if ctx.topology is not None:
            return ctx.topology
        if self._topology_getter is not None:
            return self._topology_getter()
        return None

    def decide(self, ctx: NetworkContext) -> StrategyDecision:
        topology = self._resolve_topology(ctx)
        if topology is None:
            return StrategyDecision(decisions=[])

        inter_switch: set = set()
        for link in topology.links:
            for interface_id in link:
                info = topology.interfaces.get(interface_id)
                if info is not None and info.mode == TRUNK:
                    inter_switch.add(interface_id)

        if not inter_switch:
            return StrategyDecision(decisions=[])

        protected = topology.protected_vlans
        if protected:
            logger.debug("skip protected vlans=%s in RuleBased", sorted(protected))

        candidate_vlans: set[int] = set(topology.device_vlan_members) - protected
        for interface_id in inter_switch:
            candidate_vlans |= topology.interfaces[interface_id].allowed_vlans - protected

        needed_by_vlan = {
            vlan_id: required_links(
                topology, vlan_id, topology.device_vlan_members.get(vlan_id, set())
            )
            for vlan_id in candidate_vlans
        }

        desired: dict = defaultdict(set)
        for link in topology.links:
            norm = _norm_link(*link)
            for vlan_id in candidate_vlans:
                if norm in needed_by_vlan[vlan_id]:
                    for interface_id in link:
                        info = topology.interfaces.get(interface_id)
                        if info is not None and info.mode == TRUNK:
                            desired[interface_id].add(vlan_id)

        symmetry_actions = []  
        prune_actions = []     
        added_vlans: set[int] = set()
        removed_vlans: set[int] = set()

        for interface_id in inter_switch:
            info = topology.interfaces[interface_id]
            actual = info.allowed_vlans
            want = set(desired.get(interface_id, set())) | (actual & protected)
            if actual == want:
                continue
            to_add = want - actual
            to_remove = actual - want
            action = build_trunk_action(info, want)
            if to_add:
                symmetry_actions.append(action)
                added_vlans |= to_add
                removed_vlans |= to_remove
            else:
                prune_actions.append(action)
                removed_vlans |= to_remove

        batches: list[Batch] = []
        batches.extend(batch_by_dependencies(symmetry_actions, topology, BatchCriticality.NORMAL))
        batches.extend(batch_by_dependencies(prune_actions, topology, BatchCriticality.OPTIONAL))

        if not batches:
            return StrategyDecision(decisions=[])

        decisions: list[VlanDecision] = []
        for vlan_id in sorted(added_vlans):
            decisions.append(
                VlanDecision(vlan_id=vlan_id, decision_type="TRUNK_SYNC", criticality=BatchCriticality.NORMAL)
            )
        for vlan_id in sorted(removed_vlans - added_vlans):
            decisions.append(
                VlanDecision(vlan_id=vlan_id, decision_type="TRUNK_PRUNE", criticality=BatchCriticality.OPTIONAL)
            )

        logger.info(
            "RuleBased: trunk_sync_vlans=%s trunk_prune_vlans=%s actions=%d",
            sorted(added_vlans),
            sorted(removed_vlans - added_vlans),
            sum(len(b.actions) for b in batches),
        )
        return StrategyDecision(decisions=decisions, batches=batches)
