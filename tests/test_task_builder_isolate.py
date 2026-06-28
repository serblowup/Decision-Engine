"""Regression tests for TaskBuilder ISOLATE routing and the quarantine_vlan
configuration validator (Defect 2).

Three independent checks:

1. ISOLATE must override the segmenter's target: outputs go to
   ``settings.quarantine_vlan`` even when the SegmentationResult points
   elsewhere.
2. A misconfigured quarantine VLAN (out of [2, 4094]) must fail Settings
   instantiation rather than propagate downstream.
3. MERGE must still respect the merge-computed target — the ISOLATE fix
   must not regress MERGE behaviour.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from src.config import Settings, settings
from src.models.network import Device, NetworkState, PortMode, VlanAssignment, VlanInfo
from src.models.operations import ActionType, BatchCriticality
from src.segmenters.interface import SegmentationResult
from src.strategies.interface import StrategyDecision, VlanDecision
from src.task_builder.builder import TaskBuilder


def _device(name: str = "sw") -> Device:
    return Device(
        id=uuid4(),
        hostname=name,
        mgmt_ip="10.0.0.1",
        device_type="SWITCH",
        status="ACTIVE",
    )


def _vlans(ids: list[int]) -> list[VlanInfo]:
    return [
        VlanInfo(vlan_id=v, name=f"v{v}", admin_status="ACTIVE", oper_status="UP")
        for v in ids
    ]


def test_isolate_target_overrides_segmentation_to_quarantine_vlan():
    """Segmenter says target=10 for the device, but the decision is ISOLATE on
    vlan 20. All SWITCH_VLAN actions emitted for ports previously on 20 must
    target settings.quarantine_vlan (999), not the segmenter's 10.
    """
    d = _device("sw-1")
    state = NetworkState(
        devices=[d],
        vlans=_vlans([20, 10]),
        assignments=[
            VlanAssignment(device_id=d.id, port="Gi0/1", vlan_id=20, mode=PortMode.ACCESS),
            VlanAssignment(device_id=d.id, port="Gi0/2", vlan_id=20, mode=PortMode.ACCESS),
        ],
        device_vlans={(d.id, 20), (d.id, 10)},
        protected_vlans=set(),
        timestamp=datetime.now(UTC),
    )
    # Note: segmenter sends device to vlan 10, NOT to the quarantine.
    decision = StrategyDecision(
        decisions=[
            VlanDecision(
                vlan_id=20,
                decision_type="ISOLATE",
                criticality=BatchCriticality.CRITICAL,
            )
        ],
        segmentation_result=SegmentationResult(
            segmentation={d.id: 10},
            objective_value=0.0,
            iterations=1,
            algorithm_name="dummy",
            duration_seconds=0.0,
        ),
    )

    task = TaskBuilder().build(decision=decision, state=state, initiated_by="test")

    quarantine = int(settings.quarantine_vlan)
    switches_from_20 = [
        a
        for batch in task.batches
        for a in batch.actions
        if a.action_type == ActionType.SWITCH_VLAN
        and a.previous_state.get("vlan_id") == 20
    ]
    assert switches_from_20, "expected SWITCH_VLAN actions for ports previously on vlan 20"
    for action in switches_from_20:
        target = action.params.get("target_vlan_id")
        assert target == quarantine, (
            f"ISOLATE routed to {target} instead of quarantine={quarantine}"
        )
        assert action.target_state.get("vlan_id") == quarantine


def test_quarantine_vlan_out_of_range_fails_settings_validation():
    """Defect 2 hardening: an invalid QUARANTINE_VLAN must fail at startup,
    not silently propagate as a bogus SWITCH_VLAN target.
    """
    with pytest.raises(ValueError, match="QUARANTINE_VLAN"):
        Settings(quarantine_vlan=0)
    with pytest.raises(ValueError, match="QUARANTINE_VLAN"):
        Settings(quarantine_vlan=4095)


def test_merge_keeps_merge_target_not_quarantine():
    """MERGE must continue to use the merge-computed target VLAN; the ISOLATE
    routing must NOT regress MERGE actions to quarantine.
    """
    d = _device("sw-2")
    # Pre-state: two devices' worth of ports on the source vlan 20 and an
    # existing fleet on vlan 30; MERGE will dissolve 20 -> 30 because 30 is
    # the most-represented non-source VLAN.
    state = NetworkState(
        devices=[d],
        vlans=_vlans([20, 30]),
        assignments=[
            VlanAssignment(device_id=d.id, port="Gi0/1", vlan_id=20, mode=PortMode.ACCESS),
            VlanAssignment(device_id=d.id, port="Gi0/2", vlan_id=30, mode=PortMode.ACCESS),
            VlanAssignment(device_id=d.id, port="Gi0/3", vlan_id=30, mode=PortMode.ACCESS),
        ],
        device_vlans={(d.id, 20), (d.id, 30)},
        protected_vlans=set(),
        timestamp=datetime.now(UTC),
    )
    decision = StrategyDecision(
        decisions=[
            VlanDecision(
                vlan_id=20,
                decision_type="MERGE",
                criticality=BatchCriticality.NORMAL,
            )
        ],
        segmentation_result=SegmentationResult(
            segmentation={d.id: 20},  # _apply_merge_decisions rewrites 20 -> 30
            objective_value=0.0,
            iterations=1,
            algorithm_name="dummy",
            duration_seconds=0.0,
        ),
    )

    task = TaskBuilder().build(decision=decision, state=state, initiated_by="test")

    quarantine = int(settings.quarantine_vlan)
    switches = [
        a
        for batch in task.batches
        for a in batch.actions
        if a.action_type == ActionType.SWITCH_VLAN
        and a.previous_state.get("vlan_id") == 20
    ]
    assert switches, "expected at least one SWITCH_VLAN to materialise the MERGE"
    for action in switches:
        target = action.params.get("target_vlan_id")
        assert target == 30, f"MERGE target was {target}; expected 30 (the merge result)"
        assert target != quarantine, "MERGE must not route to quarantine"
