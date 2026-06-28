from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest

from src.models.network import Device, DeviceType, NetworkState, PortMode, VlanAssignment, VlanInfo
from src.models.operations import ActionType, BatchCriticality
from src.segmenters.interface import SegmentationResult
from src.strategies.interface import StrategyDecision, VlanDecision
from src.task_builder.builder import TaskBuilder
from src.victoriametrics.client import VictoriaMetricsClient
from main import _classify_batch_statuses


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    def raise_for_status(self):
        return None

    async def json(self):
        return self._payload


class _FakeSession:
    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.calls = []

    def get(self, url, params=None):
        self.calls.append((url, params))
        payload = self.payloads.pop(0) if self.payloads else {"data": {"result": []}}
        return _FakeResponse(payload)

    async def close(self):
        return None


def _device(name: str, ip: str):
    return Device(id=uuid4(), hostname=name, mgmt_ip=ip, device_type=DeviceType.SWITCH, status="ACTIVE")


def _segmentation(state: NetworkState, target_vlan: int):
    return {device.id: target_vlan for device in state.devices}


@pytest.mark.asyncio
async def test_isolate_add_vlan_then_switch():
    sw1 = _device("sw-1", "10.0.0.1")
    state = NetworkState(
        devices=[sw1],
        vlans=[VlanInfo(vlan_id=20, name="v20", admin_status="ACTIVE", oper_status="UP")],
        assignments=[VlanAssignment(device_id=sw1.id, port="Gi0/1", vlan_id=20, mode=PortMode.ACCESS)],
        timestamp=datetime.now(timezone.utc),
    )
    decision = StrategyDecision(
        decisions=[VlanDecision(vlan_id=20, decision_type="ISOLATE", criticality=BatchCriticality.CRITICAL)],
        segmentation_result=SegmentationResult(
            segmentation=_segmentation(state, 999),
            objective_value=0,
            iterations=1,
            algorithm_name="t",
            duration_seconds=0.1,
        ),
    )

    task = TaskBuilder().build(decision, state, "test")
    assert len(task.batches) == 1
    actions = task.batches[0].actions
    assert [a.action_type for a in actions] == [ActionType.ADD_VLAN, ActionType.SWITCH_VLAN]
    assert actions[0].params == {"vlan_id": 999}
    assert actions[1].params == {"port": "Gi0/1", "target_vlan_id": 999}


@pytest.mark.asyncio
async def test_isolate_when_quarantine_exists_only_switch():
    sw1 = _device("sw-1", "10.0.0.1")
    state = NetworkState(
        devices=[sw1],
        vlans=[
            VlanInfo(vlan_id=20, name="v20", admin_status="ACTIVE", oper_status="UP"),
            VlanInfo(vlan_id=999, name="q", admin_status="ACTIVE", oper_status="UP"),
        ],
        assignments=[VlanAssignment(device_id=sw1.id, port="Gi0/1", vlan_id=20, mode=PortMode.ACCESS)],
        device_vlans={(sw1.id, 20), (sw1.id, 999)},
        timestamp=datetime.now(timezone.utc),
    )
    decision = StrategyDecision(
        decisions=[VlanDecision(vlan_id=20, decision_type="ISOLATE", criticality=BatchCriticality.CRITICAL)],
        segmentation_result=SegmentationResult(
            segmentation=_segmentation(state, 999), objective_value=0, iterations=1, algorithm_name="t", duration_seconds=0.1
        ),
    )

    task = TaskBuilder().build(decision, state, "test")
    actions = task.batches[0].actions
    assert len(actions) == 1
    assert actions[0].action_type == ActionType.SWITCH_VLAN


@pytest.mark.asyncio
async def test_isolate_multi_device_separate_batches():
    sw1 = _device("sw-1", "10.0.0.1")
    sw2 = _device("sw-2", "10.0.0.2")
    state = NetworkState(
        devices=[sw1, sw2],
        vlans=[VlanInfo(vlan_id=20, name="v20", admin_status="ACTIVE", oper_status="UP")],
        assignments=[
            VlanAssignment(device_id=sw1.id, port="Gi0/1", vlan_id=20, mode=PortMode.ACCESS),
            VlanAssignment(device_id=sw2.id, port="Gi0/3", vlan_id=20, mode=PortMode.ACCESS),
        ],
        timestamp=datetime.now(timezone.utc),
    )
    decision = StrategyDecision(
        decisions=[VlanDecision(vlan_id=20, decision_type="ISOLATE", criticality=BatchCriticality.CRITICAL)],
        segmentation_result=SegmentationResult(
            segmentation=_segmentation(state, 999), objective_value=0, iterations=1, algorithm_name="t", duration_seconds=0.1
        ),
    )

    task = TaskBuilder().build(decision, state, "test")
    assert len(task.batches) == 2
    for batch in task.batches:
        ids = {a.device_id for a in batch.actions}
        assert len(ids) == 1


@pytest.mark.asyncio
async def test_merge_switch_and_delete_vlan():
    sw1 = _device("sw-1", "10.0.0.1")
    state = NetworkState(
        devices=[sw1],
        vlans=[
            VlanInfo(vlan_id=20, name="v20", admin_status="ACTIVE", oper_status="UP"),
            VlanInfo(vlan_id=30, name="v30", admin_status="ACTIVE", oper_status="UP"),
        ],
        assignments=[
            VlanAssignment(device_id=sw1.id, port="Gi0/2", vlan_id=30, mode=PortMode.ACCESS),
            VlanAssignment(device_id=sw1.id, port="Gi0/5", vlan_id=30, mode=PortMode.ACCESS),
        ],
        timestamp=datetime.now(timezone.utc),
    )
    decision = StrategyDecision(
        decisions=[VlanDecision(vlan_id=30, decision_type="MERGE", criticality=BatchCriticality.NORMAL)],
        segmentation_result=SegmentationResult(
            segmentation={sw1.id: 20}, objective_value=0, iterations=1, algorithm_name="t", duration_seconds=0.1
        ),
    )

    task = TaskBuilder().build(decision, state, "test")
    assert len(task.batches) == 2
    switch_batch = task.batches[0]
    assert len([a for a in switch_batch.actions if a.action_type == ActionType.SWITCH_VLAN]) == 2
    delete_batch = task.batches[1]
    assert delete_batch.actions[0].action_type == ActionType.DELETE_VLAN
    assert delete_batch.actions[0].params == {"vlan_id": 30}


@pytest.mark.asyncio
async def test_set_access_previous_state_from_trunk_mode():
    sw1 = _device("sw-1", "10.0.0.1")
    state = NetworkState(
        devices=[sw1],
        vlans=[
            VlanInfo(vlan_id=10, name="v10", admin_status="ACTIVE", oper_status="UP"),
            VlanInfo(vlan_id=20, name="v20", admin_status="ACTIVE", oper_status="UP"),
            VlanInfo(vlan_id=30, name="v30", admin_status="ACTIVE", oper_status="UP"),
        ],
        assignments=[
            VlanAssignment(device_id=sw1.id, port="Gi0/1", vlan_id=10, mode=PortMode.TRUNK),
            VlanAssignment(device_id=sw1.id, port="Gi0/1", vlan_id=20, mode=PortMode.TRUNK),
        ],
        timestamp=datetime.now(timezone.utc),
    )
    decision = StrategyDecision(
        decisions=[VlanDecision(vlan_id=10, decision_type="REBALANCE", criticality=BatchCriticality.NORMAL)],
        segmentation_result=SegmentationResult(
            segmentation={sw1.id: 30}, objective_value=0, iterations=1, algorithm_name="t", duration_seconds=0.1
        ),
    )

    task = TaskBuilder().build(decision, state, "test")
    action = next(a for a in task.batches[0].actions if a.action_type == ActionType.SET_ACCESS)
    assert action.previous_state == {"mode": "TRUNK", "allowed_vlans": [10, 20]}


@pytest.mark.asyncio
async def test_set_access_previous_state_when_access_mode():
    sw1 = _device("sw-1", "10.0.0.1")
    state = NetworkState(
        devices=[sw1],
        vlans=[
            VlanInfo(vlan_id=10, name="v10", admin_status="ACTIVE", oper_status="UP"),
            VlanInfo(vlan_id=20, name="v20", admin_status="ACTIVE", oper_status="UP"),
        ],
        assignments=[VlanAssignment(device_id=sw1.id, port="Gi0/1", vlan_id=10, mode=PortMode.ACCESS)],
        timestamp=datetime.now(timezone.utc),
    )
    decision = StrategyDecision(
        decisions=[VlanDecision(vlan_id=10, decision_type="REBALANCE", criticality=BatchCriticality.NORMAL)],
        segmentation_result=SegmentationResult(
            segmentation={sw1.id: 20}, objective_value=0, iterations=1, algorithm_name="t", duration_seconds=0.1
        ),
    )

    task = TaskBuilder().build(decision, state, "test")
    action = next(a for a in task.batches[0].actions if a.action_type == ActionType.SET_ACCESS)
    assert action.previous_state == {"mode": "ACCESS", "allowed_vlans": []}


@pytest.mark.asyncio
async def test_vm_client_uses_prometheus_query_url():
    fake = _FakeSession(
        [
            {"data": {"result": []}},
        ]
    )
    client = VictoriaMetricsClient("http://vm:8428", session=fake)  # type: ignore[arg-type]
    await client.query("sum(up)")
    assert fake.calls
    assert fake.calls[0][0].endswith("/prometheus/api/v1/query")


@pytest.mark.asyncio
async def test_vm_client_parses_vlan_id_tag():
    fake = _FakeSession(
        [
            {"data": {"result": [{"metric": {"vlan_id": "20"}, "value": [1, "100"]}]}},
            {"data": {"result": []}},
            {"data": {"result": [{"metric": {"vlan_id": "20"}, "value": [1, "10"]}]}},
            {"data": {"result": [{"metric": {"vlan_id": "20"}, "value": [1, "5"]}]}},
        ]
    )
    client = VictoriaMetricsClient("http://vm:8428", session=fake)  # type: ignore[arg-type]
    result = await client.get_latest_metrics()
    assert result and result[0]["vlan_id"] == 20


@pytest.mark.asyncio
async def test_vm_client_skips_vlan_zero():
    fake = _FakeSession(
        [
            {"data": {"result": [{"metric": {"vlan_id": "0"}, "value": [1, "100"]}]}},
        ]
    )
    client = VictoriaMetricsClient("http://vm:8428", session=fake)  # type: ignore[arg-type]
    result = await client.get_latest_metrics()
    assert result == []


def test_builder_returns_empty_when_no_segmentation_result(mock_network_state):
    builder = TaskBuilder()
    decision = StrategyDecision(decisions=[], segmentation_result=None)
    task = builder.build(decision, mock_network_state, "test")
    assert task.batches == []


def test_builder_returns_empty_when_empty_segmentation(mock_network_state):
    builder = TaskBuilder()
    decision = StrategyDecision(
        decisions=[],
        segmentation_result=SegmentationResult(
            segmentation={}, objective_value=0, iterations=0, algorithm_name="t", duration_seconds=0.0
        ),
    )
    task = builder.build(decision, mock_network_state, "test")
    assert task.batches == []


def test_builder_skip_device_when_target_vlan_missing():
    sw1 = _device("sw-1", "10.0.0.1")
    sw2 = _device("sw-2", "10.0.0.2")
    state = NetworkState(
        devices=[sw1, sw2],
        vlans=[VlanInfo(vlan_id=20, name="v20", admin_status="ACTIVE", oper_status="UP")],
        assignments=[
            VlanAssignment(device_id=sw1.id, port="Gi0/1", vlan_id=20, mode=PortMode.ACCESS),
            VlanAssignment(device_id=sw2.id, port="Gi0/1", vlan_id=20, mode=PortMode.ACCESS),
        ],
        timestamp=datetime.now(timezone.utc),
    )
    decision = StrategyDecision(
        decisions=[VlanDecision(vlan_id=20, decision_type="REBALANCE", criticality=BatchCriticality.NORMAL)],
        segmentation_result=SegmentationResult(
            segmentation={sw1.id: 30}, objective_value=0, iterations=1, algorithm_name="t", duration_seconds=0.1
        ),
    )
    task = TaskBuilder().build(decision, state, "test")
    assert len(task.batches) == 1
    assert all(a.device_id == sw1.id for a in task.batches[0].actions)


def test_merge_vlan_not_in_state_no_changes_and_no_delete():
    sw1 = _device("sw-1", "10.0.0.1")
    state = NetworkState(
        devices=[sw1],
        vlans=[VlanInfo(vlan_id=20, name="v20", admin_status="ACTIVE", oper_status="UP")],
        assignments=[VlanAssignment(device_id=sw1.id, port="Gi0/1", vlan_id=20, mode=PortMode.ACCESS)],
        timestamp=datetime.now(timezone.utc),
    )
    decision = StrategyDecision(
        decisions=[VlanDecision(vlan_id=777, decision_type="MERGE", criticality=BatchCriticality.NORMAL)],
        segmentation_result=SegmentationResult(
            segmentation={sw1.id: 20}, objective_value=0, iterations=1, algorithm_name="t", duration_seconds=0.1
        ),
    )
    task = TaskBuilder().build(decision, state, "test")
    assert task.batches == []


def test_merge_with_only_one_vlan_candidate_path():
    sw1 = _device("sw-1", "10.0.0.1")
    state = NetworkState(
        devices=[sw1],
        vlans=[VlanInfo(vlan_id=30, name="v30", admin_status="ACTIVE", oper_status="UP")],
        assignments=[VlanAssignment(device_id=sw1.id, port="Gi0/1", vlan_id=30, mode=PortMode.ACCESS)],
        timestamp=datetime.now(timezone.utc),
    )
    decision = StrategyDecision(
        decisions=[VlanDecision(vlan_id=30, decision_type="MERGE", criticality=BatchCriticality.NORMAL)],
        segmentation_result=SegmentationResult(
            segmentation={sw1.id: 30}, objective_value=0, iterations=1, algorithm_name="t", duration_seconds=0.1
        ),
    )
    task = TaskBuilder().build(decision, state, "test")
    assert task.batches == []


def test_merge_deletes_only_source_vlan():
    sw1 = _device("sw-1", "10.0.0.1")
    sw2 = _device("sw-2", "10.0.0.2")
    state = NetworkState(
        devices=[sw1, sw2],
        vlans=[
            VlanInfo(vlan_id=10, name="v10", admin_status="ACTIVE", oper_status="UP"),
            VlanInfo(vlan_id=20, name="v20", admin_status="ACTIVE", oper_status="UP"),
        ],
        assignments=[
            VlanAssignment(device_id=sw1.id, port="Fa0/1", vlan_id=10, mode=PortMode.ACCESS),
            VlanAssignment(device_id=sw1.id, port="Fa0/2", vlan_id=20, mode=PortMode.ACCESS),
            VlanAssignment(device_id=sw1.id, port="Fa0/3", vlan_id=20, mode=PortMode.ACCESS),
            VlanAssignment(device_id=sw2.id, port="Fa0/1", vlan_id=20, mode=PortMode.ACCESS),
        ],
        timestamp=datetime.now(timezone.utc),
    )
    decision = StrategyDecision(
        decisions=[VlanDecision(vlan_id=10, decision_type="MERGE", criticality=BatchCriticality.NORMAL)],
        segmentation_result=SegmentationResult(
            segmentation={sw1.id: 10, sw2.id: 20},
            objective_value=0,
            iterations=1,
            algorithm_name="t",
            duration_seconds=0.1,
        ),
    )
    task = TaskBuilder().build(decision, state, "test")
    all_actions = [a for b in task.batches for a in b.actions]
    assert any(a.action_type == ActionType.SWITCH_VLAN and a.params.get("target_vlan_id") == 20 for a in all_actions)
    assert any(a.action_type == ActionType.DELETE_VLAN and a.params == {"vlan_id": 10} for a in all_actions)
    assert not any(a.action_type == ActionType.DELETE_VLAN and a.params == {"vlan_id": 20} for a in all_actions)


def test_merge_no_target_vlan_logs_warning(caplog):
    sw1 = _device("sw-1", "10.0.0.1")
    state = NetworkState(
        devices=[sw1],
        vlans=[VlanInfo(vlan_id=10, name="v10", admin_status="ACTIVE", oper_status="UP")],
        assignments=[VlanAssignment(device_id=sw1.id, port="Fa0/1", vlan_id=10, mode=PortMode.ACCESS)],
        timestamp=datetime.now(timezone.utc),
    )
    decision = StrategyDecision(
        decisions=[VlanDecision(vlan_id=10, decision_type="MERGE", criticality=BatchCriticality.NORMAL)],
        segmentation_result=SegmentationResult(
            segmentation={sw1.id: 10}, objective_value=0, iterations=1, algorithm_name="t", duration_seconds=0.1
        ),
    )
    with caplog.at_level("WARNING"):
        task = TaskBuilder().build(decision, state, "test")
    assert task.batches == []
    assert "no target VLAN found" in caplog.text


def test_merge_skip_delete_when_source_becomes_target(caplog):
    sw1 = _device("sw-1", "10.0.0.1")
    sw2 = _device("sw-2", "10.0.0.2")
    state = NetworkState(
        devices=[sw1, sw2],
        vlans=[
            VlanInfo(vlan_id=10, name="v10", admin_status="ACTIVE", oper_status="UP"),
            VlanInfo(vlan_id=20, name="v20", admin_status="ACTIVE", oper_status="UP"),
        ],
        assignments=[
            VlanAssignment(device_id=sw1.id, port="Fa0/1", vlan_id=10, mode=PortMode.ACCESS),
            VlanAssignment(device_id=sw2.id, port="Fa0/1", vlan_id=20, mode=PortMode.ACCESS),
            VlanAssignment(device_id=sw2.id, port="Fa0/2", vlan_id=20, mode=PortMode.ACCESS),
        ],
        timestamp=datetime.now(timezone.utc),
    )
    decision = StrategyDecision(
        decisions=[
            VlanDecision(vlan_id=10, decision_type="MERGE", criticality=BatchCriticality.NORMAL),
            VlanDecision(vlan_id=20, decision_type="MERGE", criticality=BatchCriticality.NORMAL),
        ],
        segmentation_result=SegmentationResult(
            segmentation={sw1.id: 10, sw2.id: 20},
            objective_value=0,
            iterations=1,
            algorithm_name="t",
            duration_seconds=0.1,
        ),
    )
    with caplog.at_level("WARNING"):
        task = TaskBuilder().build(decision, state, "test")
    delete_actions = [a for b in task.batches for a in b.actions if a.action_type == ActionType.DELETE_VLAN]
    assert all(a.params != {"vlan_id": 10} for a in delete_actions)
    assert "is target after merge, skipping DELETE_VLAN" in caplog.text


def test_deduplicates_same_port_target():
    sw1 = _device("sw-1", "10.0.0.1")
    state = NetworkState(
        devices=[sw1],
        vlans=[
            VlanInfo(vlan_id=10, name="v10", admin_status="ACTIVE", oper_status="UP"),
            VlanInfo(vlan_id=20, name="v20", admin_status="ACTIVE", oper_status="UP"),
        ],
        assignments=[
            VlanAssignment(device_id=sw1.id, port="Gi0/1", vlan_id=10, mode=PortMode.TRUNK),
            VlanAssignment(device_id=sw1.id, port="Gi0/1", vlan_id=20, mode=PortMode.TRUNK),
        ],
        device_vlans={(sw1.id, 10), (sw1.id, 20)},
        timestamp=datetime.now(timezone.utc),
    )
    decision = StrategyDecision(
        decisions=[VlanDecision(vlan_id=10, decision_type="ISOLATE", criticality=BatchCriticality.CRITICAL)],
        segmentation_result=SegmentationResult(
            segmentation={sw1.id: 999}, objective_value=0, iterations=1, algorithm_name="t", duration_seconds=0.1
        ),
    )
    task = TaskBuilder().build(decision, state, "test")
    switch_actions = [a for a in task.batches[0].actions if a.action_type == ActionType.SWITCH_VLAN]
    assert len(switch_actions) == 1


def test_cancel_status_is_terminal_and_classified_as_cancel():
    outcome = _classify_batch_statuses(["IN_PROGRESS", "CANCEL"])
    assert outcome == "CANCEL"


def test_add_vlan_added_when_missing_on_device():
    sw1 = _device("sw-1", "10.0.0.1")
    state = NetworkState(
        devices=[sw1],
        vlans=[
            VlanInfo(vlan_id=10, name="v10", admin_status="ACTIVE", oper_status="UP"),
            VlanInfo(vlan_id=20, name="v20", admin_status="ACTIVE", oper_status="UP"),
        ],
        assignments=[VlanAssignment(device_id=sw1.id, port="Fa0/1", vlan_id=20, mode=PortMode.ACCESS)],
        device_vlans={(sw1.id, 20)},
        timestamp=datetime.now(timezone.utc),
    )
    decision = StrategyDecision(
        decisions=[VlanDecision(vlan_id=20, decision_type="REBALANCE", criticality=BatchCriticality.NORMAL)],
        segmentation_result=SegmentationResult(
            segmentation={sw1.id: 10}, objective_value=0, iterations=1, algorithm_name="t", duration_seconds=0.1
        ),
    )
    task = TaskBuilder().build(decision, state, "test")
    assert [a.action_type for a in task.batches[0].actions][:2] == [ActionType.ADD_VLAN, ActionType.SET_ACCESS]


def test_add_vlan_not_added_when_exists_on_device():
    sw1 = _device("sw-1", "10.0.0.1")
    state = NetworkState(
        devices=[sw1],
        vlans=[
            VlanInfo(vlan_id=10, name="v10", admin_status="ACTIVE", oper_status="UP"),
            VlanInfo(vlan_id=20, name="v20", admin_status="ACTIVE", oper_status="UP"),
        ],
        assignments=[VlanAssignment(device_id=sw1.id, port="Fa0/1", vlan_id=20, mode=PortMode.ACCESS)],
        device_vlans={(sw1.id, 10), (sw1.id, 20)},
        timestamp=datetime.now(timezone.utc),
    )
    decision = StrategyDecision(
        decisions=[VlanDecision(vlan_id=20, decision_type="REBALANCE", criticality=BatchCriticality.NORMAL)],
        segmentation_result=SegmentationResult(
            segmentation={sw1.id: 10}, objective_value=0, iterations=1, algorithm_name="t", duration_seconds=0.1
        ),
    )
    task = TaskBuilder().build(decision, state, "test")
    assert all(a.action_type != ActionType.ADD_VLAN for a in task.batches[0].actions)


def test_add_vlan_only_on_missing_device():
    sw1 = _device("sw-1", "10.0.0.1")
    sw2 = _device("sw-2", "10.0.0.2")
    state = NetworkState(
        devices=[sw1, sw2],
        vlans=[
            VlanInfo(vlan_id=10, name="v10", admin_status="ACTIVE", oper_status="UP"),
            VlanInfo(vlan_id=20, name="v20", admin_status="ACTIVE", oper_status="UP"),
        ],
        assignments=[
            VlanAssignment(device_id=sw1.id, port="Fa0/1", vlan_id=20, mode=PortMode.ACCESS),
            VlanAssignment(device_id=sw2.id, port="Fa0/1", vlan_id=20, mode=PortMode.ACCESS),
        ],
        device_vlans={(sw1.id, 20), (sw2.id, 10), (sw2.id, 20)},
        timestamp=datetime.now(timezone.utc),
    )
    decision = StrategyDecision(
        decisions=[VlanDecision(vlan_id=20, decision_type="REBALANCE", criticality=BatchCriticality.NORMAL)],
        segmentation_result=SegmentationResult(
            segmentation={sw1.id: 10, sw2.id: 10},
            objective_value=0,
            iterations=1,
            algorithm_name="t",
            duration_seconds=0.1,
        ),
    )
    task = TaskBuilder().build(decision, state, "test")
    by_device = {b.actions[0].device_id: [a.action_type for a in b.actions] for b in task.batches}
    assert by_device[sw1.id][0] == ActionType.ADD_VLAN
    assert ActionType.ADD_VLAN not in by_device[sw2.id]
