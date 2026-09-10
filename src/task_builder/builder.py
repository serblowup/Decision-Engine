from __future__ import annotations

import asyncio
import logging
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any, Callable
from uuid import UUID

from src.config import settings
from src.db.network_state import get_router_device_id, get_router_subinterfaces
from src.models.network import NetworkState, PortMode, VlanAssignment
from src.models.operations import (
    Action,
    ActionType,
    Batch,
    BatchCriticality,
    ReconfigurationTask,
)
from src.strategies.interface import StrategyDecision
from src.topology.graph import (
    ACCESS,
    TRUNK,
    Topology,
    is_link_required,
    required_links,
    trunk_actions_for_link,
    vlans_blocking_trunk_to_access,
)
from src.task_builder.dependencies import batch_by_dependencies

# Импорт IPAM-модуля
from src.ipam.ipam_service import IPAMService

logger = logging.getLogger(__name__)


class TaskBuilder:
    def __init__(
        self,
        db_pool: Any | None = None,
        router_device_id_getter: Callable[[], UUID | None] | None = None,
        router_subinterfaces_getter: Callable[[], dict[int, dict[str, Any]]] | None = None,
        topology_getter: Callable[[], Topology | None] | None = None,
        ipam_service: IPAMService | None = None,
    ) -> None:
        self._db_pool = db_pool
        self._router_device_id_getter = router_device_id_getter
        self._router_subinterfaces_getter = router_subinterfaces_getter
        self._topology_getter = topology_getter
        self._bootstrap_marker_enabled: bool = settings.kafka_bootstrap_marker_enabled
        self._ipam_service = ipam_service or IPAMService(db_pool=db_pool)

    def _get_topology(self) -> Topology | None:
        if self._topology_getter is not None:
            return self._topology_getter()
        return None

    def expand_add_vlan(
        self,
        device_id: UUID,
        vlan_id: int,
        access_ports: list[str] | None = None,
        topology: Topology | None = None,
        criticality: BatchCriticality = BatchCriticality.NORMAL,
    ) -> list[Batch]:
        """Expand a logical ADD_VLAN into VLAN-DB + paired trunk + access actions."""
        topology = topology if topology is not None else self._get_topology()
        access_ports = list(access_ports or [])

        add_actions = [
            Action(
                device_id=device_id,
                action_type=ActionType.ADD_VLAN,
                params={"vlan_id": vlan_id},
            )
        ]

        trunk_actions: list[Action] = []
        if topology is not None:
            members_after = set(topology.device_vlan_members.get(vlan_id, set())) | {device_id}
            seen: set = set()
            for link in required_links(topology, vlan_id, members_after):
                for action in trunk_actions_for_link(topology, link, vlan_id, present=True):
                    key = (action.device_id, action.params.get("port") or action.params.get("parent_interface"), action.action_type)
                    if key in seen:
                        continue
                    seen.add(key)
                    trunk_actions.append(action)

        access_actions = [
            Action(
                device_id=device_id,
                action_type=ActionType.SET_ACCESS,
                params={"port": port, "vlan_id": vlan_id},
            )
            for port in access_ports
        ]

        flat = [*add_actions, *trunk_actions, *access_actions]
        return batch_by_dependencies(flat, topology, criticality)

    def expand_delete_vlan(
        self,
        device_id: UUID,
        vlan_id: int,
        topology: Topology | None = None,
        criticality: BatchCriticality = BatchCriticality.NORMAL,
    ) -> list[Batch]:
        """Expand a logical DELETE_VLAN (NOT symmetric to ADD)."""
        topology = topology if topology is not None else self._get_topology()
        fallback = settings.fallback_vlan_id

        if topology is not None and vlan_id in topology.protected_vlans:
            logger.warning("DELETE_VLAN blocked: vlan_id=%s is protected", vlan_id)
            return []

        if topology is None:
            return [
                Batch(
                    actions=[
                        Action(
                            device_id=device_id,
                            action_type=ActionType.DELETE_VLAN,
                            params={"vlan_id": vlan_id},
                        )
                    ],
                    criticality=criticality,
                )
            ]

        # 1. evacuate access ports on D in V, always, first
        evac_actions: list[Action] = [
            Action(
                device_id=device_id,
                action_type=ActionType.SET_ACCESS,
                params={"port": info.name, "vlan_id": fallback},
                previous_state={"mode": "ACCESS", "vlan_id": vlan_id},
                target_state={"mode": "ACCESS", "vlan_id": fallback},
            )
            for info in topology.interfaces_of(device_id)
            if info.mode == ACCESS and info.access_vlan_id == vlan_id
        ]

        members_after = set(topology.device_vlan_members.get(vlan_id, set())) - {device_id}
        needed_links = topology.steiner_links(members_after)
        transit_devices = topology.devices_on_links(needed_links)

        if device_id in transit_devices:
            logger.warning(
                "DELETE_VLAN: device %s is transit for VLAN %s, keeping VLAN, evacuating access only",
                device_id,
                vlan_id,
            )
            return batch_by_dependencies(evac_actions, topology, criticality) if evac_actions else []

        # D is a leaf / isolated for V -> prune trunks (both ends), then delete from DB.
        trunk_actions: list[Action] = []
        seen: set = set()
        for info in topology.interfaces_of(device_id):
            if info.mode != TRUNK or vlan_id not in info.allowed_vlans:
                continue
            peer_id = topology.peer_interface(info.interface_id)
            link = (info.interface_id, peer_id)
            for action in trunk_actions_for_link(topology, link, vlan_id, present=False):
                key = (action.device_id, action.params.get("port") or action.params.get("parent_interface"), action.action_type)
                if key in seen:
                    continue
                seen.add(key)
                trunk_actions.append(action)

        delete_actions = [
            Action(
                device_id=device_id,
                action_type=ActionType.DELETE_VLAN,
                params={"vlan_id": vlan_id},
            )
        ]

        flat = [*evac_actions, *trunk_actions, *delete_actions]
        return batch_by_dependencies(flat, topology, criticality)

    def edit_trunk_remove(
        self,
        interface_id,
        vlan_id: int,
        members_after=None,
        topology: Topology | None = None,
        criticality: BatchCriticality = BatchCriticality.NORMAL,
    ) -> list[Batch]:
        """Standalone EDIT_TRUNK(-V): remove a VLAN from a single trunk link."""
        topology = topology if topology is not None else self._get_topology()
        if topology is None:
            return []
        if vlan_id in topology.protected_vlans:
            logger.warning("EDIT_TRUNK(-%s) blocked: vlan is protected", vlan_id)
            return []
        peer_id = topology.peer_interface(interface_id)
        link = (interface_id, peer_id)
        if members_after is None:
            members_after = topology.device_vlan_members.get(vlan_id, set())
        if is_link_required(topology, vlan_id, members_after, link):
            logger.warning(
                "EDIT_TRUNK(-%s) skipped: link still required by the VLAN (neighbour keeps it)",
                vlan_id,
            )
            return []
        actions = trunk_actions_for_link(topology, link, vlan_id, present=False)
        return [Batch(actions=actions, criticality=criticality)] if actions else []

    def convert_port_to_access(
        self,
        interface_id,
        target_vlan: int,
        topology: Topology | None = None,
        criticality: BatchCriticality = BatchCriticality.NORMAL,
    ) -> list[Batch]:
        """Convert an inter-switch trunk port to ACCESS, guarded by path recompute."""
        topology = topology if topology is not None else self._get_topology()
        if topology is None:
            return []
        info = topology.interfaces.get(interface_id)
        if info is None:
            return []
        blocking = vlans_blocking_trunk_to_access(topology, interface_id)
        if blocking:
            logger.warning(
                "SET_ACCESS conversion blocked on port %s: link still needed by vlans=%s",
                info.name,
                sorted(blocking),
            )
            return []
        action = Action(
            device_id=info.device_id,
            action_type=ActionType.SET_ACCESS,
            params={"port": info.name, "vlan_id": target_vlan},
            previous_state={"mode": "TRUNK", "allowed_vlans": sorted(info.allowed_vlans)},
            target_state={"mode": "ACCESS", "vlan_id": target_vlan},
        )
        return [Batch(actions=[action], criticality=criticality)]

    def build(self, decision: StrategyDecision, state: NetworkState, initiated_by: str) -> ReconfigurationTask:
        # --- Обработка IPAM-решений (SPLIT / MERGE) ---
        ipam_decisions = [d for d in decision.decisions if d.decision_type in {"SPLIT", "MERGE"}]
        if ipam_decisions:
            ipam_batches = self._build_ipam_batches(ipam_decisions, state)
            if ipam_batches:
                return ReconfigurationTask(
                    batches=ipam_batches,
                    initiated_by=initiated_by,
                    created_at=datetime.now(timezone.utc),
                )

        # --- Существующая логика для структурных решений ---
        if decision.segmentation_result is None:
            if decision.batches:
                return ReconfigurationTask(
                    batches=list(decision.batches),
                    initiated_by=initiated_by,
                    created_at=datetime.now(timezone.utc),
                )
            logger.warning("No segmentation result received from strategy; returning empty task")
            return self._empty_task(initiated_by)

        target_segmentation = dict(decision.segmentation_result.segmentation)
        if not target_segmentation:
            return self._empty_task(initiated_by)

        decision_by_vlan = {item.vlan_id: item for item in decision.decisions}
        target_segmentation = self._apply_merge_decisions(target_segmentation, decision, state)

        assignments_by_device: dict[UUID, list[VlanAssignment]] = defaultdict(list)
        assignments_by_port: dict[tuple[UUID, str], list[VlanAssignment]] = defaultdict(list)
        for assignment in state.assignments:
            assignments_by_device[assignment.device_id].append(assignment)
            assignments_by_port[(assignment.device_id, assignment.port)].append(assignment)

        protected_vlans: set[int] = set(state.protected_vlans)
        port_protected_map: dict[tuple[UUID, str], set[int]] = {}
        for port_key, port_assignments in assignments_by_port.items():
            on_trunk = {
                int(a.vlan_id) for a in port_assignments if a.mode == PortMode.TRUNK
            }
            collision = on_trunk & protected_vlans
            if collision:
                port_protected_map[port_key] = collision

        actions_by_device: dict[UUID, list[Action]] = defaultdict(list)
        criticalities_by_device: dict[UUID, list[BatchCriticality]] = defaultdict(list)

        for device_id, assignments in assignments_by_device.items():
            target_vlan = target_segmentation.get(device_id)
            if target_vlan is None:
                continue

            needs_add_vlan = any(
                target_vlan != assignment.vlan_id for assignment in assignments
            ) and not self._vlan_exists_on_device(device_id, target_vlan, state)

            if needs_add_vlan:
                actions_by_device[device_id].append(
                    Action(
                        device_id=device_id,
                        action_type=ActionType.ADD_VLAN,
                        params={"vlan_id": target_vlan},
                    )
                )
                self._append_create_subinterface(actions_by_device[device_id], target_vlan)

            isolate_on_device = False
            for _a in assignments:
                _vd = decision_by_vlan.get(_a.vlan_id)
                if _vd is not None and _vd.decision_type == "ISOLATE":
                    isolate_on_device = True
                    break

            quarantine_blocked = False
            quarantine_vlan = int(settings.quarantine_vlan)
            if isolate_on_device:
                if quarantine_vlan in state.protected_vlans:
                    logger.warning(
                        "ISOLATE skipped on device=%s: quarantine_vlan=%s is protected",
                        device_id,
                        quarantine_vlan,
                    )
                    quarantine_blocked = True
                elif quarantine_vlan != target_vlan and not self._vlan_exists_on_device(
                    device_id, quarantine_vlan, state
                ):
                    actions_by_device[device_id].append(
                        Action(
                            device_id=device_id,
                            action_type=ActionType.ADD_VLAN,
                            params={"vlan_id": quarantine_vlan},
                        )
                    )
                    self._append_create_subinterface(
                        actions_by_device[device_id], quarantine_vlan
                    )

            seen_actions: set[tuple[str, int]] = set()
            for assignment in assignments:
                decision_type = None
                vlan_decision = decision_by_vlan.get(assignment.vlan_id)
                if vlan_decision is not None:
                    decision_type = vlan_decision.decision_type

                if decision_type == "ISOLATE":
                    if quarantine_blocked:
                        continue
                    effective_target = quarantine_vlan
                else:
                    effective_target = target_vlan

                if assignment.vlan_id == effective_target:
                    continue

                key = (assignment.port, effective_target)
                if key in seen_actions:
                    continue
                seen_actions.add(key)

                will_emit_set_access = (
                    assignment.mode == PortMode.TRUNK
                    and decision_type not in {"ISOLATE", "MERGE"}
                )
                if will_emit_set_access:
                    protected_on_port = port_protected_map.get(
                        (device_id, assignment.port), set()
                    )
                    if protected_on_port:
                        logger.warning(
                            "SET_ACCESS blocked: device=%s port=%s carries protected vlans=%s",
                            device_id,
                            assignment.port,
                            sorted(protected_on_port),
                        )
                        continue

                action = self._build_port_action(
                    device_id=device_id,
                    assignment=assignment,
                    target_vlan=effective_target,
                    port_assignments=assignments_by_port[(assignment.device_id, assignment.port)],
                    decision_type=decision_type,
                )
                actions_by_device[device_id].append(action)

                if vlan_decision is not None:
                    criticalities_by_device[device_id].append(vlan_decision.criticality)
                elif effective_target == settings.quarantine_vlan:
                    criticalities_by_device[device_id].append(BatchCriticality.CRITICAL)
                else:
                    criticalities_by_device[device_id].append(BatchCriticality.NORMAL)

        batches: list[Batch] = []
        for device_id, actions in actions_by_device.items():
            if not actions:
                continue
            batch_criticality = self._max_criticality(criticalities_by_device.get(device_id, []))
            batches.append(Batch(actions=actions, criticality=batch_criticality))

        merge_source_vlans = [d.vlan_id for d in decision.decisions if d.decision_type == "MERGE"]
        if merge_source_vlans and state.devices:
            final_actions: list[Action] = []
            target_vlans_after_merge = set(target_segmentation.values())
            protected = set(getattr(state, "protected_vlans", set()) or set())
            for vlan_id in sorted(set(merge_source_vlans)):
                if vlan_id in protected:
                    logger.warning("DELETE_VLAN blocked: vlan_id=%s is protected", vlan_id)
                    continue
                if not any(a.vlan_id == vlan_id for a in state.assignments):
                    logger.warning(
                        "MERGE: source vlan_id=%s not found in assignments, skip DELETE_VLAN",
                        vlan_id,
                    )
                    continue
                if vlan_id in target_vlans_after_merge:
                    logger.warning(
                        "MERGE: vlan_id=%s is target after merge, skipping DELETE_VLAN",
                        vlan_id,
                    )
                    continue
                router_device_id = self._get_router_device_id()
                if router_device_id is not None and not self._vlan_exists_on_device(
                    router_device_id, vlan_id, state
                ):
                    logger.warning(
                        "MERGE: vlan_id=%s not found on router device, skipping DELETE_VLAN",
                        vlan_id,
                    )
                    continue
                self._append_delete_subinterface(final_actions, vlan_id)
                final_actions.append(
                    Action(
                        device_id=state.devices[0].id,
                        action_type=ActionType.DELETE_VLAN,
                        params={"vlan_id": vlan_id},
                    )
                )
            if final_actions:
                batches.append(Batch(actions=final_actions, criticality=BatchCriticality.NORMAL))

        return ReconfigurationTask(
            batches=batches,
            initiated_by=initiated_by,
            created_at=datetime.now(timezone.utc),
        )

    def _build_ipam_batches(
        self,
        ipam_decisions: list[VlanDecision],
        state: NetworkState,
    ) -> list[Batch]:
        """Создать батчи для IPAM-решений SPLIT и MERGE."""
        actions: list[Action] = []
        router_device_id = self._get_router_device_id()

        for decision in ipam_decisions:
            if decision.decision_type == "SPLIT":
                actions.extend(self._build_split_actions(decision, state, router_device_id))
            elif decision.decision_type == "MERGE":
                actions.extend(self._build_merge_actions(decision, state, router_device_id))

        if not actions:
            return []

        # Группируем по критичности
        critical_actions = [a for a in actions if a.params.get("criticality") == BatchCriticality.CRITICAL]
        normal_actions = [a for a in actions if a.params.get("criticality") != BatchCriticality.CRITICAL]

        batches = []
        if critical_actions:
            batches.append(Batch(actions=critical_actions, criticality=BatchCriticality.CRITICAL))
        if normal_actions:
            batches.append(Batch(actions=normal_actions, criticality=BatchCriticality.NORMAL))

        return batches

    def _build_split_actions(
        self,
        decision: VlanDecision,
        state: NetworkState,
        router_device_id: UUID | None,
    ) -> list[Action]:
        """Создать actions для SPLIT."""
        actions = []

        if router_device_id is None:
            logger.warning("Router not found, skip SPLIT for vlan_id=%s", decision.vlan_id)
            return actions

        # Получаем текущий префикс VLAN
        prefix = self._get_vlan_prefix(state, decision.vlan_id)
        if prefix is None:
            logger.warning("VLAN %s has no IP prefix, skip SPLIT", decision.vlan_id)
            return actions

        try:
            # Выполняем разбиение
            child1, child2 = self._ipam_service.calculator.split(prefix)
            gateway1 = self._ipam_service.calculator.get_gateway(child1)
            gateway2 = self._ipam_service.calculator.get_gateway(child2)

            child_vlan_ids = decision.child_vlan_ids
            if child_vlan_ids is None:
                logger.warning("No child VLAN IDs for SPLIT of vlan_id=%s", decision.vlan_id)
                return actions

            child1_id, child2_id = child_vlan_ids

            # 1. DELETE_SUBINTERFACE для старого VLAN
            actions.append(
                Action(
                    device_id=router_device_id,
                    action_type=ActionType.DELETE_SUBINTERFACE,
                    params={
                        "parent_interface": settings.subinterface_parent,
                        "vlan_id": decision.vlan_id,
                    },
                    previous_state={"vlan_id": decision.vlan_id},
                    target_state={},
                )
            )

            # 2. CREATE_SUBINTERFACE для первого дочернего VLAN
            actions.append(
                Action(
                    device_id=router_device_id,
                    action_type=ActionType.CREATE_SUBINTERFACE,
                    params={
                        "parent_interface": settings.subinterface_parent,
                        "vlan_id": child1_id,
                        "ip_address": gateway1,
                    },
                    previous_state={},
                    target_state={
                        "vlan_id": child1_id,
                        "ip_prefix": child1,
                        "gateway": gateway1,
                    },
                )
            )

            # 3. CREATE_SUBINTERFACE для второго дочернего VLAN
            actions.append(
                Action(
                    device_id=router_device_id,
                    action_type=ActionType.CREATE_SUBINTERFACE,
                    params={
                        "parent_interface": settings.subinterface_parent,
                        "vlan_id": child2_id,
                        "ip_address": gateway2,
                    },
                    previous_state={},
                    target_state={
                        "vlan_id": child2_id,
                        "ip_prefix": child2,
                        "gateway": gateway2,
                    },
                )
            )

            # 4. UPDATE_VLAN_PREFIX для обновления в БД
            actions.append(
                Action(
                    device_id=router_device_id,
                    action_type=ActionType.UPDATE_VLAN_PREFIX,
                    params={
                        "parent_vlan_id": decision.vlan_id,
                        "child1_vlan_id": child1_id,
                        "child1_prefix": child1,
                        "child1_gateway": gateway1,
                        "child2_vlan_id": child2_id,
                        "child2_prefix": child2,
                        "child2_gateway": gateway2,
                    },
                    previous_state={"ip_prefix": prefix},
                    target_state={
                        "child1_ip_prefix": child1,
                        "child2_ip_prefix": child2,
                    },
                )
            )

            logger.info(
                "SPLIT: %s -> %s (VLAN %d) + %s (VLAN %d)",
                prefix, child1, child1_id, child2, child2_id
            )

        except Exception as e:
            logger.error("SPLIT failed for vlan_id=%s: %s", decision.vlan_id, e)

        return actions

    def _build_merge_actions(
        self,
        decision: VlanDecision,
        state: NetworkState,
        router_device_id: UUID | None,
    ) -> list[Action]:
        """Создать actions для MERGE."""
        actions = []

        if router_device_id is None:
            logger.warning("Router not found, skip MERGE for vlan_id=%s", decision.vlan_id)
            return actions

        source_vlan = decision.vlan_id
        target_vlan = decision.target_vlan_id

        if target_vlan is None:
            logger.warning("No target VLAN for MERGE of vlan_id=%s", source_vlan)
            return actions

        # Получаем префиксы
        prefix1 = self._get_vlan_prefix(state, source_vlan)
        prefix2 = self._get_vlan_prefix(state, target_vlan)

        if prefix1 is None:
            logger.warning("VLAN %s has no IP prefix, skip MERGE", source_vlan)
            return actions
        if prefix2 is None:
            logger.warning("VLAN %s has no IP prefix, skip MERGE", target_vlan)
            return actions

        try:
            # Выполняем слияние
            merged = self._ipam_service.calculator.merge(prefix1, prefix2)
            gateway = self._ipam_service.calculator.get_gateway(merged)

            # 1. DELETE_SUBINTERFACE для source VLAN
            actions.append(
                Action(
                    device_id=router_device_id,
                    action_type=ActionType.DELETE_SUBINTERFACE,
                    params={
                        "parent_interface": settings.subinterface_parent,
                        "vlan_id": source_vlan,
                    },
                    previous_state={"vlan_id": source_vlan},
                    target_state={},
                )
            )

            # 2. DELETE_SUBINTERFACE для target VLAN
            actions.append(
                Action(
                    device_id=router_device_id,
                    action_type=ActionType.DELETE_SUBINTERFACE,
                    params={
                        "parent_interface": settings.subinterface_parent,
                        "vlan_id": target_vlan,
                    },
                    previous_state={"vlan_id": target_vlan},
                    target_state={},
                )
            )

            # 3. CREATE_SUBINTERFACE для слитого VLAN
            actions.append(
                Action(
                    device_id=router_device_id,
                    action_type=ActionType.CREATE_SUBINTERFACE,
                    params={
                        "parent_interface": settings.subinterface_parent,
                        "vlan_id": target_vlan,
                        "ip_address": gateway,
                    },
                    previous_state={},
                    target_state={
                        "vlan_id": target_vlan,
                        "ip_prefix": merged,
                        "gateway": gateway,
                    },
                )
            )

            # 4. UPDATE_VLAN_PREFIX для обновления в БД
            actions.append(
                Action(
                    device_id=router_device_id,
                    action_type=ActionType.UPDATE_VLAN_PREFIX,
                    params={
                        "source_vlan_id": source_vlan,
                        "target_vlan_id": target_vlan,
                        "merged_prefix": merged,
                        "merged_gateway": gateway,
                    },
                    previous_state={
                        "source_ip_prefix": prefix1,
                        "target_ip_prefix": prefix2,
                    },
                    target_state={
                        "merged_ip_prefix": merged,
                        "merged_gateway": gateway,
                    },
                )
            )

            logger.info(
                "MERGE: %s + %s -> %s (VLAN %d)",
                prefix1, prefix2, merged, target_vlan
            )

        except Exception as e:
            logger.error("MERGE failed for vlan_id=%s: %s", source_vlan, e)

        return actions

    def _get_vlan_prefix(self, state: NetworkState, vlan_id: int) -> str | None:
        """Получить IP-префикс VLAN из состояния."""
        for vlan in state.vlans:
            if vlan.vlan_id == vlan_id:
                if hasattr(vlan, "ip_prefix") and vlan.ip_prefix:
                    return vlan.ip_prefix
                break
        return None

    def _append_create_subinterface(self, actions: list[Action], vlan_id: int) -> None:
        if not settings.subinterface_enabled:
            return

        router_device_id = self._get_router_device_id()
        if router_device_id is None:
            logger.warning("Router not found, skip CREATE_SUBINTERFACE for vlan_id=%s", vlan_id)
            return

        ip = settings.subinterface_subnet_template.format(vlan_id=vlan_id)
        actions.append(
            Action(
                device_id=router_device_id,
                action_type=ActionType.CREATE_SUBINTERFACE,
                params={
                    "parent_interface": settings.subinterface_parent,
                    "vlan_id": vlan_id,
                    "ip_address": ip,
                },
                previous_state={},
                target_state={},
            )
        )

    def _append_delete_subinterface(self, actions: list[Action], vlan_id: int) -> None:
        if not settings.subinterface_enabled:
            return

        sub_map = self._get_router_subinterfaces()
        if vlan_id not in sub_map:
            logger.warning("Subinterface for vlan_id=%s not found, skip DELETE_SUBINTERFACE", vlan_id)
            return

        router_device_id = self._get_router_device_id()
        if router_device_id is None:
            logger.warning("Router not found, skip DELETE_SUBINTERFACE for vlan_id=%s", vlan_id)
            return

        actions.append(
            Action(
                device_id=router_device_id,
                action_type=ActionType.DELETE_SUBINTERFACE,
                params={
                    "parent_interface": settings.subinterface_parent,
                    "vlan_id": vlan_id,
                },
                previous_state={},
                target_state={},
            )
        )

    def _get_router_device_id(self) -> UUID | None:
        if self._router_device_id_getter is not None:
            return self._router_device_id_getter()
        if self._db_pool is None:
            return None
        return self._run_async(lambda: get_router_device_id(self._db_pool))

    def _get_router_subinterfaces(self) -> dict[int, dict[str, Any]]:
        if self._router_subinterfaces_getter is not None:
            return self._router_subinterfaces_getter()
        if self._db_pool is None:
            return {}
        return self._run_async(lambda: get_router_subinterfaces(self._db_pool)) or {}

    def _run_async(self, factory: Callable[[], Any]) -> Any:
        try:
            asyncio.get_running_loop()
            return None
        except RuntimeError:
            return asyncio.run(factory())

    def _build_port_action(
        self,
        device_id: UUID,
        assignment: VlanAssignment,
        target_vlan: int,
        port_assignments: list[VlanAssignment],
        decision_type: str | None,
    ) -> Action:
        if decision_type in {"ISOLATE", "MERGE"}:
            return Action(
                device_id=device_id,
                action_type=ActionType.SWITCH_VLAN,
                params={"port": assignment.port, "target_vlan_id": target_vlan},
                previous_state={"vlan_id": assignment.vlan_id},
                target_state={"vlan_id": target_vlan},
            )

        current_mode = str(assignment.mode)
        if assignment.mode == PortMode.TRUNK:
            allowed = sorted({int(a.vlan_id) for a in port_assignments if a.mode == PortMode.TRUNK})
        else:
            allowed = []

        return Action(
            device_id=device_id,
            action_type=ActionType.SET_ACCESS,
            params={"port": assignment.port, "vlan_id": target_vlan},
            previous_state={"mode": current_mode, "allowed_vlans": allowed},
            target_state={"mode": PortMode.ACCESS, "vlan_id": target_vlan},
        )

    def _vlan_exists_on_device(
        self,
        device_id: UUID,
        vlan_id: int,
        state: NetworkState,
    ) -> bool:
        return (device_id, vlan_id) in state.device_vlans

    def _apply_merge_decisions(
        self,
        segmentation: dict[UUID, int],
        decision: StrategyDecision,
        state: NetworkState,
    ) -> dict[UUID, int]:
        result = dict(segmentation)

        for item in decision.decisions:
            if item.decision_type != "MERGE":
                continue

            source_vlan = item.vlan_id
            if not any(a.vlan_id == source_vlan for a in state.assignments):
                logger.warning(
                    "MERGE: source vlan_id=%s not found in assignments, skipping",
                    source_vlan,
                )
                continue

            vlan_port_counts: dict[int, int] = {}
            for assignment in state.assignments:
                if assignment.vlan_id != source_vlan:
                    vlan_port_counts[assignment.vlan_id] = vlan_port_counts.get(assignment.vlan_id, 0) + 1

            if not vlan_port_counts:
                logger.warning(
                    "MERGE: no target VLAN found for source vlan_id=%s, skipping",
                    source_vlan,
                )
                continue

            target_vlan = max(vlan_port_counts, key=lambda v: vlan_port_counts[v])
            logger.info("MERGE: source_vlan=%s -> target_vlan=%s", source_vlan, target_vlan)
            for device_id, vlan_id in list(result.items()):
                if vlan_id == source_vlan:
                    result[device_id] = target_vlan

        return result

    def _max_criticality(self, criticalities: list[BatchCriticality]) -> BatchCriticality:
        if not criticalities:
            return BatchCriticality.NORMAL
        ranking = {
            BatchCriticality.OPTIONAL: 0,
            BatchCriticality.NORMAL: 1,
            BatchCriticality.CRITICAL: 2,
        }
        return Counter(criticalities).most_common(1)[0][0] if len(set(criticalities)) == 1 else max(
            criticalities,
            key=lambda item: ranking[item],
        )

    def _empty_task(self, initiated_by: str) -> ReconfigurationTask:
        return ReconfigurationTask(
            batches=[],
            initiated_by=initiated_by,
            created_at=datetime.now(timezone.utc),
        )