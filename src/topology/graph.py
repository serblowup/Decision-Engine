"""Topology graph — the structural model of the network.

This module is owned by the *strategy / domain* layer, not by the action
(task_builder) layer. The reasoning: an action is the *result* of a decision,
so the structural model of the network (the topology graph plus the canonical
definition of "where a VLAN must be") is the property of the layer that makes
decisions. ``RuleBasedStrategy`` is the primary owner of this structural
semantics; the ``SET_TRUNK`` / ``ADD_VLAN`` / ``DELETE_VLAN`` expansion in
``task_builder`` is a downstream *consumer* that imports from here.

There is intentionally no dependency from this module back to ``task_builder``.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Hashable, Iterable, Mapping

import networkx as nx

from src.config import settings
from src.models.operations import Action, ActionType

logger = logging.getLogger(__name__)

TRUNK = "TRUNK"
ACCESS = "ACCESS"

# Interface capability (per-interface, NOT per device_type).
L2_SWITCHPORT = "L2_SWITCHPORT"
L3_ROUTED = "L3_ROUTED"

# An undirected physical link, identified by the two interfaces at its ends.
Link = tuple[Hashable, Hashable]


def _norm_link(a: Hashable, b: Hashable) -> Link:
    """Order-independent key for an undirected link between two interfaces."""
    return (a, b) if str(a) <= str(b) else (b, a)


@dataclass(slots=True)
class InterfaceInfo:
    interface_id: Hashable
    device_id: Hashable
    name: str
    mode: str | None  # "ACCESS" | "TRUNK" for switchports; None for L3 routed
    access_vlan_id: int | None = None
    allowed_vlans: set[int] = field(default_factory=set)
    is_endpoint: bool = False  # faces a host (endpoint_network_attachment)
    # L3 attributes (routed interfaces / dot1q subinterfaces)
    parent_interface_id: Hashable | None = None
    ip_address: str | None = None
    dot1q_vlan_id: int | None = None


class Topology:
    """L2 topology graph.

    Nodes are devices, edges are physical links. Each edge keeps the pair of
    interfaces that form the link, so a device-level path can be mapped back to
    the concrete trunk ports that must carry a VLAN.
    """

    def __init__(
        self,
        interfaces: Mapping[Hashable, InterfaceInfo],
        links: Iterable[Link],
        device_vlan_members: Mapping[int, set[Hashable]],
        protected_vlans: Iterable[int] = (),
    ) -> None:
        # VLANs flagged vlan.is_protected = TRUE (managed by Gateway). Invariant:
        # DE produces no destructive action for them (no DELETE_VLAN, no trunk
        # removal) and does not change their placement. Single source of truth.
        self.protected_vlans: set[int] = {int(v) for v in protected_vlans}
        self.interfaces: dict[Hashable, InterfaceInfo] = dict(interfaces)
        self.links: list[Link] = [tuple(link) for link in links]
        self.device_vlan_members: dict[int, set[Hashable]] = {
            int(vlan_id): set(devices) for vlan_id, devices in device_vlan_members.items()
        }

        self._peer: dict[Hashable, Hashable] = {}
        self._by_device: dict[Hashable, list[InterfaceInfo]] = defaultdict(list)
        self._children_by_parent: dict[Hashable, list[Hashable]] = defaultdict(list)
        for iface in self.interfaces.values():
            self._by_device[iface.device_id].append(iface)
            if iface.parent_interface_id is not None:
                self._children_by_parent[iface.parent_interface_id].append(iface.interface_id)

        self.graph = nx.Graph()
        for iface in self.interfaces.values():
            self.graph.add_node(iface.device_id)

        for a, b in self.links:
            self._peer[a] = b
            self._peer[b] = a
            device_a = self.device_of(a)
            device_b = self.device_of(b)
            if device_a is None or device_b is None or device_a == device_b:
                continue
            if self.graph.has_edge(device_a, device_b):
                self.graph[device_a][device_b]["links"].append(_norm_link(a, b))
            else:
                self.graph.add_edge(device_a, device_b, links=[_norm_link(a, b)])

    # ---- builders --------------------------------------------------------
    @classmethod
    def build(
        cls,
        *,
        interfaces: Iterable[Mapping[str, object]],
        links: Iterable[Link],
        trunk_allowed: Iterable[tuple[Hashable, int]] = (),
        device_vlan: Iterable[tuple[Hashable, int]] = (),
        endpoints: Iterable[Hashable] = (),
        protected_vlans: Iterable[int] = (),
    ) -> "Topology":
        allowed_by_iface: dict[Hashable, set[int]] = defaultdict(set)
        for interface_id, vlan_id in trunk_allowed:
            allowed_by_iface[interface_id].add(int(vlan_id))

        endpoint_set = set(endpoints)
        iface_map: dict[Hashable, InterfaceInfo] = {}
        for item in interfaces:
            interface_id = item["interface_id"]
            access_vlan = item.get("access_vlan_id")
            mode = item.get("mode")
            dot1q = item.get("dot1q_vlan_id")
            iface_map[interface_id] = InterfaceInfo(
                interface_id=interface_id,
                device_id=item["device_id"],
                name=str(item["name"]),
                mode=str(mode) if mode is not None else None,
                access_vlan_id=int(access_vlan) if access_vlan is not None else None,
                allowed_vlans=set(allowed_by_iface.get(interface_id, set())),
                is_endpoint=interface_id in endpoint_set,
                parent_interface_id=item.get("parent_interface_id"),
                ip_address=item.get("ip_address"),
                dot1q_vlan_id=int(dot1q) if dot1q is not None else None,
            )

        members: dict[int, set[Hashable]] = defaultdict(set)
        for device_id, vlan_id in device_vlan:
            members[int(vlan_id)].add(device_id)

        return cls(iface_map, list(links), members, protected_vlans=protected_vlans)

    # ---- helpers ---------------------------------------------------------
    def device_of(self, interface_id: Hashable) -> Hashable | None:
        info = self.interfaces.get(interface_id)
        return info.device_id if info is not None else None

    def peer_interface(self, interface_id: Hashable) -> Hashable | None:
        return self._peer.get(interface_id)

    def interfaces_of(self, device_id: Hashable) -> list[InterfaceInfo]:
        return list(self._by_device.get(device_id, []))

    def trunk_interfaces(self) -> list[InterfaceInfo]:
        return [iface for iface in self.interfaces.values() if iface.mode == TRUNK]

    def allowed_on(self, interface_id: Hashable) -> set[int]:
        info = self.interfaces.get(interface_id)
        return set(info.allowed_vlans) if info is not None else set()

    def mode_of(self, interface_id: Hashable) -> str | None:
        info = self.interfaces.get(interface_id)
        return info.mode if info is not None else None

    def children_subinterfaces(self, interface_id: Hashable) -> list[Hashable]:
        return list(self._children_by_parent.get(interface_id, []))

    def interface_kind(self, interface_id: Hashable) -> str:
        """Classify an interface by capability, NOT by device_type.

        L2_SWITCHPORT: has a switchport mode (ACCESS/TRUNK).
        L3_ROUTED:     a dot1q subinterface (parent set), OR a physical uplink that
                       has subinterface children, OR a routed port with an IP and
                       no switchport mode.

        A mixed device (router with switchports, L3 switch) is handled correctly
        because the decision is per interface.
        """
        info = self.interfaces.get(interface_id)
        if info is None:
            return L2_SWITCHPORT
        if info.mode in (ACCESS, TRUNK):
            return L2_SWITCHPORT
        if info.parent_interface_id is not None:
            return L3_ROUTED
        if self._children_by_parent.get(interface_id):
            return L3_ROUTED
        if info.ip_address is not None:
            return L3_ROUTED
        return L2_SWITCHPORT

    def has_subinterface_for(self, interface_id: Hashable, vlan_id: int) -> bool:
        for child_id in self._children_by_parent.get(interface_id, []):
            child = self.interfaces.get(child_id)
            if child is not None and child.dot1q_vlan_id == vlan_id:
                return True
        return False

    def interface_by_device_port(self, device_id: Hashable, name: str) -> InterfaceInfo | None:
        for iface in self._by_device.get(device_id, []):
            if iface.name == name:
                return iface
        return None

    def shortest_path_links(self, device_x: Hashable, device_y: Hashable) -> list[Link]:
        """Return the links (interface pairs) on a shortest device path x->y."""
        if device_x == device_y:
            return []
        if device_x not in self.graph or device_y not in self.graph:
            return []
        try:
            node_path = nx.shortest_path(self.graph, device_x, device_y)
        except nx.NetworkXNoPath:
            return []

        result: list[Link] = []
        for u, v in zip(node_path, node_path[1:]):
            # one representative physical link per device edge
            result.append(self.graph[u][v]["links"][0])
        return result

    def steiner_links(self, members: Iterable[Hashable]) -> set[Link]:
        """Union of shortest paths connecting all member devices.

        Exact minimal tree on a tree topology; a correct (superset) approximation
        on a mesh. Returns the set of links that must carry the VLAN so that every
        member stays connected.
        """
        member_nodes = [m for m in dict.fromkeys(members) if m in self.graph]
        if len(member_nodes) <= 1:
            return set()
        root = member_nodes[0]
        needed: set[Link] = set()
        for other in member_nodes[1:]:
            for link in self.shortest_path_links(root, other):
                needed.add(_norm_link(*link))
        return needed

    def devices_on_links(self, links: Iterable[Link]) -> set[Hashable]:
        devices: set[Hashable] = set()
        for a, b in links:
            for interface_id in (a, b):
                device = self.device_of(interface_id)
                if device is not None:
                    devices.add(device)
        return devices


# --- Canonical "where a VLAN must be" --------------------------------------

def required_links(
    topology: Topology,
    vlan_id: int,
    members: Iterable[Hashable],
) -> set[Link]:
    """Minimal set of trunk links for full connectivity of a VLAN's members.

    Steiner tree over ``members`` on the topology graph (exact on a tree). This
    is the SINGLE SOURCE OF TRUTH for "the correct placement of a VLAN": both
    ``RuleBasedStrategy`` (group 2 minimality) and the ``SET_TRUNK`` expansion in
    ``task_builder`` take the required placement of ``vlan_id`` only from here, so
    they cannot disagree.

    ``vlan_id`` is part of the signature for call-site clarity and future
    per-VLAN policy; the connectivity requirement itself is a pure function of
    the member set and the graph.
    """
    return topology.steiner_links(members)


def build_trunk_action(info: InterfaceInfo, new_allowed: set[int]) -> Action:
    """Build one idempotent trunk action carrying the full target allowed list.

    The action_type is chosen from the interface's CURRENT mode:

    * already TRUNK -> ``EDIT_TRUNK`` (only the allowed-list changes, mode kept);
    * ACCESS        -> ``SET_TRUNK`` (conversion ACCESS -> TRUNK).

    One port yields exactly one action with an unambiguous meaning. The on-the-wire
    params (port + allowed_vlans) are identical for both, so Configurator can reuse
    its parser; previous_state/target_state carry the mode for rollback.
    """
    target = sorted(new_allowed)
    if info.mode == TRUNK:
        return Action(
            device_id=info.device_id,
            action_type=ActionType.EDIT_TRUNK,
            params={"port": info.name, "allowed_vlans": target},
            # EDIT_TRUNK never changes the port mode (already TRUNK on both ends),
            # so the states carry only the allowed-list.
            previous_state={"allowed_vlans": sorted(info.allowed_vlans)},
            target_state={"allowed_vlans": target},
        )
    # ACCESS -> TRUNK conversion
    return Action(
        device_id=info.device_id,
        action_type=ActionType.SET_TRUNK,
        params={"port": info.name, "allowed_vlans": target},
        previous_state={"mode": "ACCESS", "vlan_id": info.access_vlan_id},
        target_state={"mode": "TRUNK", "allowed_vlans": target},
    )


# Backwards-compatible alias (the builder is now mode-aware, not always SET_TRUNK).
set_trunk_action = build_trunk_action


def create_subinterface_action(info: InterfaceInfo, vlan_id: int) -> Action:
    """CREATE_SUBINTERFACE on an L3 routed uplink to carry a VLAN (dot1q)."""
    ip = settings.subinterface_subnet_template.format(vlan_id=vlan_id)
    return Action(
        device_id=info.device_id,
        action_type=ActionType.CREATE_SUBINTERFACE,
        params={"parent_interface": info.name, "vlan_id": vlan_id, "ip_address": ip},
        previous_state={},
        target_state={},
    )


def delete_subinterface_action(info: InterfaceInfo, vlan_id: int) -> Action:
    """DELETE_SUBINTERFACE on an L3 routed uplink."""
    return Action(
        device_id=info.device_id,
        action_type=ActionType.DELETE_SUBINTERFACE,
        params={"parent_interface": info.name, "vlan_id": vlan_id},
        previous_state={},
        target_state={},
    )


def trunk_actions_for_link(
    topology: Topology,
    link: Link,
    vlan_id: int,
    present: bool,
) -> list[Action]:
    """Paired SET_TRUNK on both ends of a link: add (present=True) or remove
    (present=False) ``vlan_id``.

    Only ports with ``mode == TRUNK`` are touched; an inter-switch port found in
    ACCESS mode is skipped with a WARNING (topology desync, not auto-repaired).
    Idempotent: never adds a VLAN already present, never removes one absent.
    Shared by both the strategy layer and ``task_builder`` so the two-ended logic
    is not duplicated.
    """
    actions: list[Action] = []
    for interface_id in link:
        info = topology.interfaces.get(interface_id)
        if info is None:
            continue
        if not present and vlan_id in topology.protected_vlans:
            # Protected VLAN: never removed / never deprovisioned (defense-in-depth).
            logger.warning(
                "trunk removal blocked: vlan_id=%s is protected (port %s)",
                vlan_id,
                info.name,
            )
            continue

        kind = topology.interface_kind(interface_id)

        # --- L3 routed uplink: carry/remove the VLAN via a dot1q subinterface ---
        if kind == L3_ROUTED:
            if not settings.subinterface_enabled:
                continue
            if present:
                if not topology.has_subinterface_for(interface_id, vlan_id):
                    actions.append(create_subinterface_action(info, vlan_id))
            else:
                if topology.has_subinterface_for(interface_id, vlan_id):
                    actions.append(delete_subinterface_action(info, vlan_id))
            continue

        # --- L2 switchport: trunk allowed-list (NEVER subinterface actions) ---
        if info.mode == TRUNK:
            has_vlan = vlan_id in info.allowed_vlans
            if present and has_vlan:
                continue
            if not present and not has_vlan:
                continue
            new_allowed = (
                info.allowed_vlans | {vlan_id} if present else info.allowed_vlans - {vlan_id}
            )
            actions.append(build_trunk_action(info, new_allowed))  # EDIT_TRUNK
        elif info.mode == ACCESS:
            if not present:
                logger.warning(
                    "inter-switch port %s is ACCESS, nothing to remove for vlan_id=%s",
                    info.name,
                    vlan_id,
                )
                continue
            new_allowed = info.allowed_vlans | {vlan_id}
            actions.append(build_trunk_action(info, new_allowed))  # SET_TRUNK conversion
        else:
            logger.warning(
                "port %s on device %s has unclassifiable mode %s, skipping",
                info.name,
                info.device_id,
                info.mode,
            )
            continue
    return actions


# --- Path recompute on removal (Part 3) ------------------------------------

def is_link_required(
    topology: "Topology",
    vlan_id: int,
    members_after: Iterable[Hashable],
    link: Link,
) -> bool:
    """True if ``link`` is on the Steiner tree of ``vlan_id`` for ``members_after``.

    Single source of truth for "does V still need this link". Used to refuse a
    removal that would split the VLAN for a neighbour.
    """
    return _norm_link(*link) in required_links(topology, vlan_id, members_after)


def vlans_blocking_trunk_to_access(topology: "Topology", interface_id: Hashable) -> set[int]:
    """VLANs that forbid converting an inter-switch trunk port to access.

    Converting a trunk to access drops every VLAN it carried. For each carried
    VLAN whose link is still on its Steiner tree, the conversion would break the
    VLAN for the neighbour, so it must be blocked. Returns that blocking set.
    """
    info = topology.interfaces.get(interface_id)
    if info is None or info.mode != TRUNK:
        return set()
    peer = topology.peer_interface(interface_id)
    if peer is None:
        return set()  # not an inter-switch link
    link = (interface_id, peer)
    blocking: set[int] = set()
    for vlan_id in info.allowed_vlans:
        members = topology.device_vlan_members.get(vlan_id, set())
        if _norm_link(*link) in required_links(topology, vlan_id, members):
            blocking.add(vlan_id)
    return blocking
