"""Regression tests for the protected-VLAN guard on TRUNK ports (Defect 3).

The guard refuses to emit SET_ACCESS on any TRUNK port whose allowed list
contains at least one protected VLAN, because SET_ACCESS would silently
strip every other VLAN — including the protected ones — from the allowed
list when the port is collapsed into ACCESS mode. SWITCH_VLAN (ISOLATE /
MERGE) is exempt by design: it changes the port's primary VLAN without
touching the trunk-allowed list.
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


def _device() -> Device:
    return Device(
        id=uuid4(),
        hostname="sw",
        mgmt_ip="10.0.0.1",
        device_type="SWITCH",
        status="ACTIVE",
    )


def _trunk_state(protected: set[int]) -> tuple[Device, NetworkState]:
    d = _device()
    state = NetworkState(
        devices=[d],
        vlans=[
            VlanInfo(
                vlan_id=v,
                name=f"v{v}",
                admin_status="ACTIVE",
                oper_status="UP",
                is_protected=v in protected,
            )
            for v in (20, 90, 998)
        ],
        assignments=[
            VlanAssignment(device_id=d.id, port="Gi0/1", vlan_id=20, mode=PortMode.TRUNK),
            VlanAssignment(device_id=d.id, port="Gi0/1", vlan_id=90, mode=PortMode.TRUNK),
            VlanAssignment(device_id=d.id, port="Gi0/1", vlan_id=998, mode=PortMode.TRUNK),
        ],
        device_vlans={(d.id, 20), (d.id, 90), (d.id, 998)},
        protected_vlans=protected,
        timestamp=datetime.now(UTC),
    )
    return d, state


def _seg(d: Device, target: int) -> SegmentationResult:
    return SegmentationResult(
        segmentation={d.id: target},
        objective_value=0.0,
        iterations=1,
        algorithm_name="dummy",
        duration_seconds=0.0,
    )


def test_rebalance_blocks_set_access_on_trunk_with_protected(caplog):
    """Trunk allowed=[20, 90, 998], 998 protected. REBALANCE on vlan 20 must
    NOT produce a SET_ACCESS on Gi0/1; a WARNING ``SET_ACCESS blocked`` must
    appear instead.
    """
    d, state = _trunk_state(protected={998})
    decision = StrategyDecision(
        decisions=[
            VlanDecision(
                vlan_id=20,
                decision_type="REBALANCE",
                criticality=BatchCriticality.NORMAL,
            )
        ],
        segmentation_result=_seg(d, target=20),
    )

    with caplog.at_level(logging.WARNING, logger="src.task_builder.builder"):
        task = TaskBuilder().build(decision=decision, state=state, initiated_by="t")

    set_access_on_port = [
        a
        for batch in task.batches
        for a in batch.actions
        if a.action_type == ActionType.SET_ACCESS
        and a.device_id == d.id
        and a.params.get("port") == "Gi0/1"
    ]
    assert set_access_on_port == [], "SET_ACCESS must be blocked when the trunk carries 998"

    messages = [r.getMessage() for r in caplog.records]
    assert any(
        "SET_ACCESS blocked" in m and "Gi0/1" in m and "998" in m for m in messages
    ), f"missing 'SET_ACCESS blocked' warning; got: {messages}"


def test_isolate_on_protected_trunk_emits_switch_vlan_to_quarantine():
    """ISOLATE goes through SWITCH_VLAN, which does NOT touch the trunk-allowed
    list, so the protected guard must not block the ISOLATE action itself. The
    SWITCH_VLAN must target settings.quarantine_vlan. (Non-ISOLATE assignments
    on the same port may still hit the SET_ACCESS protected guard — that is
    correct behaviour and is verified by test 1 above.)
    """
    d, state = _trunk_state(protected={998})
    decision = StrategyDecision(
        decisions=[
            VlanDecision(
                vlan_id=20,
                decision_type="ISOLATE",
                criticality=BatchCriticality.CRITICAL,
            )
        ],
        segmentation_result=_seg(d, target=20),
    )

    task = TaskBuilder().build(decision=decision, state=state, initiated_by="t")

    quarantine = int(settings.quarantine_vlan)
    switch_on_port = [
        a
        for batch in task.batches
        for a in batch.actions
        if a.action_type == ActionType.SWITCH_VLAN
        and a.device_id == d.id
        and a.params.get("port") == "Gi0/1"
    ]
    assert len(switch_on_port) == 1, (
        f"expected exactly one SWITCH_VLAN; got {switch_on_port}"
    )
    sw = switch_on_port[0]
    assert sw.params.get("target_vlan_id") == quarantine
    assert sw.previous_state.get("vlan_id") == 20
    assert sw.target_state.get("vlan_id") == quarantine


def test_rebalance_without_protected_emits_set_access():
    """Positive case: same trunk with no protected VLAN -> REBALANCE generates
    SET_ACCESS as before. Guard does not over-block.
    """
    d, state = _trunk_state(protected=set())
    decision = StrategyDecision(
        decisions=[
            VlanDecision(
                vlan_id=20,
                decision_type="REBALANCE",
                criticality=BatchCriticality.NORMAL,
            )
        ],
        segmentation_result=_seg(d, target=20),
    )

    task = TaskBuilder().build(decision=decision, state=state, initiated_by="t")

    set_access_on_port = [
        a
        for batch in task.batches
        for a in batch.actions
        if a.action_type == ActionType.SET_ACCESS
        and a.device_id == d.id
        and a.params.get("port") == "Gi0/1"
    ]
    assert set_access_on_port, (
        "expected SET_ACCESS to be emitted when no protected VLAN is on the trunk"
    )
