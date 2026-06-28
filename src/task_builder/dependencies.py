"""Action dependency model + batching under CM parallelism.

CM executes batches in parallel (``max_parallel_batches=4``), so order BETWEEN
batches is NOT guaranteed. Therefore actions with a hard ordering dependency must
land in the SAME batch (executed sequentially), and independent action sets go to
separate batches (free to run in parallel).

This module is the single dependency-builder: it (1) injects missing prerequisites
once with de-duplication, (2) builds a dependency graph from a flat action list,
(3) splits it into weakly-connected components — one ordered batch each.

Dependency rules (A before B):
* ADD_VLAN(dev, V)            -> any action on dev that provisions V
                                 (SET_ACCESS V, SWITCH_VLAN->V, SET_TRUNK/EDIT_TRUNK +V)
* CREATE_SUBINTERFACE(rtr, V) -> EDIT_TRUNK/SET_TRUNK +V on the link to that router
* delete is mirrored:        evacuation (SET_ACCESS/SWITCH_VLAN off V)
                             -> EDIT_TRUNK(-V) -> DELETE_SUBINTERFACE(V) -> DELETE_VLAN(V)
"""

from __future__ import annotations

import networkx as nx

from src.models.operations import Action, ActionType, Batch, BatchCriticality

# Tie-break rank for a stable, intuitive order among otherwise-unordered actions.
_PHASE_RANK = {
    ActionType.ADD_VLAN: 0,
    ActionType.CREATE_SUBINTERFACE: 0,
    ActionType.SET_TRUNK: 1,
    ActionType.EDIT_TRUNK: 1,
    ActionType.SET_ACCESS: 2,
    ActionType.SWITCH_VLAN: 2,
    ActionType.DELETE_SUBINTERFACE: 3,
    ActionType.DELETE_VLAN: 4,
}


def _allowed(state: dict) -> set[int]:
    return {int(v) for v in state.get("allowed_vlans", [])}


def _added_removed(action: Action) -> tuple[set[int], set[int]]:
    """For trunk actions: VLANs added / removed relative to previous_state."""
    if action.action_type in (ActionType.SET_TRUNK, ActionType.EDIT_TRUNK):
        prev = _allowed(action.previous_state)
        tgt = _allowed(action.target_state)
        return tgt - prev, prev - tgt
    return set(), set()


def _provisions(action: Action) -> set[int]:
    """VLANs this action makes reachable on its port/device."""
    t = action.action_type
    if t in (ActionType.SET_TRUNK, ActionType.EDIT_TRUNK):
        return _added_removed(action)[0]
    if t == ActionType.SET_ACCESS:
        vid = action.params.get("vlan_id")
        return {int(vid)} if vid is not None else set()
    if t == ActionType.SWITCH_VLAN:
        vid = action.params.get("target_vlan_id")
        return {int(vid)} if vid is not None else set()
    if t == ActionType.CREATE_SUBINTERFACE:
        vid = action.params.get("vlan_id")
        return {int(vid)} if vid is not None else set()
    return set()


def _removes(action: Action) -> set[int]:
    """VLANs this action takes away from its port/device."""
    t = action.action_type
    if t in (ActionType.SET_TRUNK, ActionType.EDIT_TRUNK):
        return _added_removed(action)[1]
    if t in (ActionType.SET_ACCESS, ActionType.SWITCH_VLAN):
        # evacuation: previous_state carries the VLAN being left
        vid = action.previous_state.get("vlan_id")
        return {int(vid)} if vid is not None else set()
    if t == ActionType.DELETE_SUBINTERFACE:
        vid = action.params.get("vlan_id")
        return {int(vid)} if vid is not None else set()
    return set()


def _peer_device(topology, action: Action):
    if topology is None:
        return None
    port = action.params.get("port")
    if port is None:
        return None
    iface = topology.interface_by_device_port(action.device_id, port)
    if iface is None:
        return None
    peer = topology.peer_interface(iface.interface_id)
    return topology.device_of(peer) if peer is not None else None


def _inject_subinterface_prereqs(actions: list[Action], topology) -> list[Action]:
    """Ensure a CREATE_SUBINTERFACE prereq exists for every +V trunk-add toward an
    L3 routed router uplink that lacks a subinterface for V. De-duplicated: if the
    prereq is already present (e.g. added by ADD_VLAN expansion) it is not repeated.
    """
    if topology is None:
        return list(actions)
    from src.config import settings
    from src.topology.graph import L3_ROUTED, create_subinterface_action

    result = list(actions)
    existing = {
        (a.device_id, a.params.get("parent_interface"), int(a.params.get("vlan_id")))
        for a in actions
        if a.action_type == ActionType.CREATE_SUBINTERFACE
    }
    if not settings.subinterface_enabled:
        return result

    for action in actions:
        if action.action_type not in (ActionType.SET_TRUNK, ActionType.EDIT_TRUNK):
            continue
        added, _ = _added_removed(action)
        if not added:
            continue
        port = action.params.get("port")
        iface = topology.interface_by_device_port(action.device_id, port) if port else None
        if iface is None:
            continue
        peer = topology.peer_interface(iface.interface_id)
        if peer is None or topology.interface_kind(peer) != L3_ROUTED:
            continue
        peer_info = topology.interfaces.get(peer)
        for vlan_id in added:
            if topology.has_subinterface_for(peer, vlan_id):
                continue
            key = (peer_info.device_id, peer_info.name, int(vlan_id))
            if key in existing:
                continue
            existing.add(key)
            result.append(create_subinterface_action(peer_info, vlan_id))
    return result


def batch_by_dependencies(
    actions: list[Action],
    topology=None,
    criticality: BatchCriticality = BatchCriticality.NORMAL,
) -> list[Batch]:
    """Group actions into batches by dependency: one weakly-connected component per
    batch, topologically ordered inside. Independent components -> separate batches.
    """
    actions = _inject_subinterface_prereqs(actions, topology)
    if not actions:
        return []

    g = nx.DiGraph()
    for idx in range(len(actions)):
        g.add_node(idx)

    add_prereq: dict[tuple, int] = {}
    create_sub_prereq: dict[tuple, int] = {}
    for idx, a in enumerate(actions):
        if a.action_type == ActionType.ADD_VLAN:
            add_prereq[(a.device_id, int(a.params["vlan_id"]))] = idx
        elif a.action_type == ActionType.CREATE_SUBINTERFACE:
            create_sub_prereq[(a.device_id, int(a.params["vlan_id"]))] = idx

    for idx, a in enumerate(actions):
        # ADD_VLAN(dev,V) -> provisioning actions on dev for V
        for v in _provisions(a):
            src = add_prereq.get((a.device_id, v))
            if src is not None and src != idx:
                g.add_edge(src, idx)
            # CREATE_SUBINTERFACE(rtr,V) -> trunk +V on link to rtr
            if a.action_type in (ActionType.SET_TRUNK, ActionType.EDIT_TRUNK):
                rtr = _peer_device(topology, a)
                if rtr is not None:
                    csrc = create_sub_prereq.get((rtr, v))
                    if csrc is not None and csrc != idx:
                        g.add_edge(csrc, idx)

        # delete ordering for V removed by this action
        for v in _removes(a):
            for jdx, b in enumerate(actions):
                if jdx == idx:
                    continue
                if b.action_type == ActionType.DELETE_VLAN and int(b.params.get("vlan_id", -1)) == v:
                    g.add_edge(idx, jdx)
                if (
                    a.action_type in (ActionType.SET_ACCESS, ActionType.SWITCH_VLAN)
                    and b.action_type in (ActionType.EDIT_TRUNK, ActionType.SET_TRUNK)
                    and v in _removes(b)
                ):
                    # evacuation precedes trunk removal of the same VLAN
                    g.add_edge(idx, jdx)
                if (
                    a.action_type in (ActionType.EDIT_TRUNK, ActionType.SET_TRUNK)
                    and b.action_type == ActionType.DELETE_SUBINTERFACE
                    and int(b.params.get("vlan_id", -1)) == v
                ):
                    g.add_edge(idx, jdx)

    undirected = g.to_undirected()
    batches: list[Batch] = []
    for component in nx.connected_components(undirected):
        sub = g.subgraph(component)
        order = list(nx.lexicographical_topological_sort(sub, key=lambda n: _PHASE_RANK.get(actions[n].action_type, 9)))
        batches.append(Batch(actions=[actions[i] for i in order], criticality=criticality))
    return batches
