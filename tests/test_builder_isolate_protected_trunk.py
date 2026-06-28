"""Regression tests for TaskBuilder.build() in the segmentation branch:

1. SET_ACCESS must not silently strip a protected VLAN from a TRUNK port.
2. decision_type='ISOLATE' must route to settings.quarantine_vlan
   (not to the segmentation target) and the quarantine VLAN must be
   provisioned on the device first.
3. If the quarantine VLAN already exists on the device, ADD_VLAN is not
   re-emitted.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from uuid import uuid4

from src.config import settings
from src.models.network import Device, NetworkState, PortMode, VlanAssignment, VlanInfo
from src.models.operations import ActionType, BatchCriticality
from src.segmenters.interface import SegmentationResult
from src.strategies.interface import StrategyDecision, VlanDecision
from src.task_builder.builder import TaskBuilder


def _device(hostname: str = "sw") -> Device:
    return Device(
        id=uuid4(),
        hostname=hostname,
        mgmt_ip="10.0.0.1",
        device_type="SWITCH",
        status="ACTIVE",
    )


def _vlans(ids: list[int], protected: set[int]) -> list[VlanInfo]:
    return [
        VlanInfo(
            vlan_id=v,
            name=f"v{v}",
            admin_status="ACTIVE",
            oper_status="UP",
            is_protected=v in protected,
        )
        for v in ids
    ]


def _seg(devices: list[Device], target: int) -> SegmentationResult:
    return SegmentationResult(
        segmentation={d.id: target for d in devices},
        objective_value=0.0,
        iterations=1,
        algorithm_name="dummy",
        duration_seconds=0.0,
    )


# ---------------------------------------------------------------------------
# 1. Protected VLAN on TRUNK must block TRUNK->ACCESS conversion.
# ---------------------------------------------------------------------------

def test_set_access_blocked_when_trunk_carries_protected(caplog):
    d = _device("sw-1")
    assignments = [
        VlanAssignment(device_id=d.id, port="Gi0/1", vlan_id=10, mode=PortMode.TRUNK),
        VlanAssignment(device_id=d.id, port="Gi0/1", vlan_id=20, mode=PortMode.TRUNK),
        VlanAssignment(device_id=d.id, port="Gi0/1", vlan_id=998, mode=PortMode.TRUNK),
    ]
    state = NetworkState(
        devices=[d],
        vlans=_vlans([10, 20, 998], protected={998}),
        assignments=assignments,
        device_vlans={(d.id, 10), (d.id, 20), (d.id, 998)},
        protected_vlans={998},
        timestamp=datetime.now(UTC),
    )
    decision = StrategyDecision(
        decisions=[
            VlanDecision(
                vlan_id=20,
                decision_type="REBALANCE",  # non-ISOLATE / non-MERGE
                criticality=BatchCriticality.NORMAL,
            )
        ],
        segmentation_result=_seg([d], target=20),
    )

    with caplog.at_level(logging.WARNING, logger="src.task_builder.builder"):
        task = TaskBuilder().build(decision=decision, state=state, initiated_by="test")

    set_access_on_port = [
        a
        for batch in task.batches
        for a in batch.actions
        if a.action_type == ActionType.SET_ACCESS
        and a.device_id == d.id
        and a.params.get("port") == "Gi0/1"
    ]
    assert set_access_on_port == [], "SET_ACCESS must not be emitted on a trunk that carries a protected VLAN"

    messages = [r.getMessage() for r in caplog.records]
    assert any(
        "SET_ACCESS blocked" in m
        and "Gi0/1" in m
        and "carries protected vlans=[998]" in m
        for m in messages
    ), f"missing warning about protected trunk; got: {messages}"


# ---------------------------------------------------------------------------
# 2. ISOLATE must route to quarantine_vlan + auto-add it when missing.
# ---------------------------------------------------------------------------

def test_isolate_routes_to_quarantine_vlan():
    d = _device("sw-2")
    state = NetworkState(
        devices=[d],
        vlans=_vlans([20], protected=set()),
        assignments=[
            VlanAssignment(device_id=d.id, port="Gi0/1", vlan_id=20, mode=PortMode.ACCESS),
        ],
        device_vlans={(d.id, 20)},  # quarantine 999 NOT present
        protected_vlans=set(),
        timestamp=datetime.now(UTC),
    )
    decision = StrategyDecision(
        decisions=[
            VlanDecision(
                vlan_id=20,
                decision_type="ISOLATE",
                criticality=BatchCriticality.CRITICAL,
            )
        ],
        # Segmenter pointed somewhere arbitrary (50); ISOLATE must override.
        segmentation_result=_seg([d], target=50),
    )

    task = TaskBuilder().build(decision=decision, state=state, initiated_by="test")

    quarantine = int(settings.quarantine_vlan)
    switches = [
        a
        for batch in task.batches
        for a in batch.actions
        if a.action_type == ActionType.SWITCH_VLAN
        and a.device_id == d.id
        and a.params.get("port") == "Gi0/1"
    ]
    assert len(switches) == 1, (
        f"expected exactly one SWITCH_VLAN for the ISOLATE port; got {switches}"
    )
    sw = switches[0]
    assert sw.params.get("target_vlan_id") == quarantine
    assert sw.target_state.get("vlan_id") == quarantine

    add_vlan_quarantine = [
        a
        for batch in task.batches
        for a in batch.actions
        if a.action_type == ActionType.ADD_VLAN
        and a.device_id == d.id
        and a.params.get("vlan_id") == quarantine
    ]
    assert add_vlan_quarantine, (
        "ADD_VLAN(quarantine) must be emitted when quarantine is not on device"
    )


# ---------------------------------------------------------------------------
# 3. If quarantine is already provisioned, no extra ADD_VLAN.
# ---------------------------------------------------------------------------

def test_isolate_with_existing_quarantine_skips_add_vlan():
    d = _device("sw-3")
    quarantine = int(settings.quarantine_vlan)
    state = NetworkState(
        devices=[d],
        vlans=_vlans([20, quarantine], protected=set()),
        assignments=[
            VlanAssignment(device_id=d.id, port="Gi0/1", vlan_id=20, mode=PortMode.ACCESS),
        ],
        device_vlans={(d.id, 20), (d.id, quarantine)},  # quarantine ALREADY present
        protected_vlans=set(),
        timestamp=datetime.now(UTC),
    )
    decision = StrategyDecision(
        decisions=[
            VlanDecision(
                vlan_id=20,
                decision_type="ISOLATE",
                criticality=BatchCriticality.CRITICAL,
            )
        ],
        segmentation_result=_seg([d], target=50),
    )

    task = TaskBuilder().build(decision=decision, state=state, initiated_by="test")

    add_vlan_quarantine = [
        a
        for batch in task.batches
        for a in batch.actions
        if a.action_type == ActionType.ADD_VLAN
        and a.device_id == d.id
        and a.params.get("vlan_id") == quarantine
    ]
    assert add_vlan_quarantine == [], (
        "ADD_VLAN(quarantine) must be suppressed when already on device"
    )

    # SWITCH_VLAN still goes to quarantine.
    switches = [
        a
        for batch in task.batches
        for a in batch.actions
        if a.action_type == ActionType.SWITCH_VLAN and a.device_id == d.id
    ]
    assert any(a.params.get("target_vlan_id") == quarantine for a in switches)
