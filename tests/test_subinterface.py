from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest

from src.config import settings
from src.db.network_state import get_device_vlans, get_network_state, get_router_device_id, get_router_subinterfaces
from src.models.network import Device, DeviceType, NetworkState, PortMode, VlanAssignment, VlanInfo
from src.models.operations import ActionType, BatchCriticality
from src.segmenters.interface import SegmentationResult
from src.strategies.interface import StrategyDecision, VlanDecision
from src.task_builder.builder import TaskBuilder


class _Conn:
    def __init__(self, rows=None, row=None, exc: Exception | None = None):
        self._rows = rows or []
        self._row = row
        self._exc = exc

    async def fetch(self, query, *params):
        if self._exc:
            raise self._exc
        return self._rows

    async def fetchrow(self, query, *params):
        if self._exc:
            raise self._exc
        return self._row


class _Pool:
    def __init__(self, conn: _Conn):
        self._conn = conn

    def acquire(self):
        conn = self._conn

        class _Ctx:
            async def __aenter__(self_inner):
                return conn

            async def __aexit__(self_inner, exc_type, exc, tb):
                return None

        return _Ctx()


def _device(name: str, ip: str, dtype: DeviceType = DeviceType.SWITCH):
    return Device(id=uuid4(), hostname=name, mgmt_ip=ip, device_type=dtype, status="ACTIVE")


def _state_with_vlan(
    vlan_ids: list[int],
    assignments: list[VlanAssignment],
    devices: list[Device] | None = None,
    device_vlans: set[tuple[object, int]] | None = None,
):
    devices = devices or [_device("sw-1", "10.0.0.1")]
    if device_vlans is None:
        default_device_vlans = {(a.device_id, a.vlan_id) for a in assignments}
    else:
        default_device_vlans = {(d, int(v)) for d, v in device_vlans}
    return NetworkState(
        devices=devices,
        vlans=[VlanInfo(vlan_id=v, name=f"v{v}", admin_status="ACTIVE", oper_status="UP") for v in vlan_ids],
        assignments=assignments,
        device_vlans=default_device_vlans,
        timestamp=datetime.now(timezone.utc),
    )


def _decision(segmentation, decisions):
    return StrategyDecision(
        decisions=decisions,
        segmentation_result=SegmentationResult(
            segmentation=segmentation,
            objective_value=0.0,
            iterations=1,
            algorithm_name="t",
            duration_seconds=0.1,
        ),
    )


def test_create_subinterface_added_after_add_vlan(monkeypatch):
    sw = _device("sw-1", "10.0.0.1")
    state = _state_with_vlan(
        [20],
        [VlanAssignment(device_id=sw.id, port="Gi0/1", vlan_id=20, mode=PortMode.ACCESS)],
        devices=[sw],
    )
    builder = TaskBuilder(
        router_device_id_getter=lambda: sw.id,
        router_subinterfaces_getter=lambda: {},
    )

    decision = _decision(
        {sw.id: 30},
        [VlanDecision(vlan_id=20, decision_type="REBALANCE", criticality=BatchCriticality.NORMAL)],
    )
    task = builder.build(decision, state, "test")

    actions = task.batches[0].actions
    assert actions[0].action_type == ActionType.ADD_VLAN
    assert actions[1].action_type == ActionType.CREATE_SUBINTERFACE
    assert actions[1].params["parent_interface"] == settings.subinterface_parent
    assert actions[1].params["vlan_id"] == 30
    assert actions[1].params["ip_address"] == "192.168.30.1/24"


def test_ip_generated_from_template(monkeypatch):
    sw = _device("sw-1", "10.0.0.1")
    state = _state_with_vlan(
        [20],
        [VlanAssignment(device_id=sw.id, port="Gi0/1", vlan_id=20, mode=PortMode.ACCESS)],
        devices=[sw],
    )
    monkeypatch.setattr(settings, "subinterface_subnet_template", "192.168.{vlan_id}.1/24")
    builder = TaskBuilder(router_device_id_getter=lambda: sw.id, router_subinterfaces_getter=lambda: {})
    decision = _decision(
        {sw.id: 50},
        [VlanDecision(vlan_id=20, decision_type="REBALANCE", criticality=BatchCriticality.NORMAL)],
    )

    task = builder.build(decision, state, "test")
    create = next(a for a in task.batches[0].actions if a.action_type == ActionType.CREATE_SUBINTERFACE)
    assert create.params["ip_address"] == "192.168.50.1/24"


def test_create_subinterface_skipped_when_disabled(monkeypatch):
    sw = _device("sw-1", "10.0.0.1")
    state = _state_with_vlan(
        [20],
        [VlanAssignment(device_id=sw.id, port="Gi0/1", vlan_id=20, mode=PortMode.ACCESS)],
        devices=[sw],
    )
    monkeypatch.setattr(settings, "subinterface_enabled", False)
    builder = TaskBuilder(router_device_id_getter=lambda: sw.id, router_subinterfaces_getter=lambda: {})
    decision = _decision(
        {sw.id: 30},
        [VlanDecision(vlan_id=20, decision_type="REBALANCE", criticality=BatchCriticality.NORMAL)],
    )

    task = builder.build(decision, state, "test")
    assert [a.action_type for a in task.batches[0].actions] == [ActionType.ADD_VLAN, ActionType.SET_ACCESS]


def test_create_subinterface_skipped_when_router_not_found(caplog):
    sw = _device("sw-1", "10.0.0.1")
    state = _state_with_vlan(
        [20],
        [VlanAssignment(device_id=sw.id, port="Gi0/1", vlan_id=20, mode=PortMode.ACCESS)],
        devices=[sw],
    )
    builder = TaskBuilder(router_device_id_getter=lambda: None, router_subinterfaces_getter=lambda: {})
    decision = _decision(
        {sw.id: 30},
        [VlanDecision(vlan_id=20, decision_type="REBALANCE", criticality=BatchCriticality.NORMAL)],
    )

    with caplog.at_level("WARNING"):
        task = builder.build(decision, state, "test")
    assert [a.action_type for a in task.batches[0].actions] == [ActionType.ADD_VLAN, ActionType.SET_ACCESS]
    assert "Router not found" in caplog.text


def test_delete_subinterface_before_delete_vlan():
    router_id = uuid4()
    sw = _device("sw-1", "10.0.0.1")
    state = _state_with_vlan(
        [20, 30],
        [VlanAssignment(device_id=sw.id, port="Gi0/1", vlan_id=30, mode=PortMode.ACCESS)],
        devices=[sw],
        device_vlans={(sw.id, 30), (router_id, 30)},
    )
    builder = TaskBuilder(
        router_device_id_getter=lambda: router_id,
        router_subinterfaces_getter=lambda: {30: {"name": "GigabitEthernet0/0/1.30"}},
    )
    decision = _decision(
        {sw.id: 20},
        [VlanDecision(vlan_id=30, decision_type="MERGE", criticality=BatchCriticality.NORMAL)],
    )

    task = builder.build(decision, state, "test")
    final_batch = task.batches[-1]
    assert [a.action_type for a in final_batch.actions] == [
        ActionType.DELETE_SUBINTERFACE,
        ActionType.DELETE_VLAN,
    ]


def test_delete_subinterface_skipped_when_not_found(caplog):
    router_id = uuid4()
    sw = _device("sw-1", "10.0.0.1")
    state = _state_with_vlan(
        [20, 30],
        [VlanAssignment(device_id=sw.id, port="Gi0/1", vlan_id=30, mode=PortMode.ACCESS)],
        devices=[sw],
        device_vlans={(sw.id, 30), (router_id, 30)},
    )
    builder = TaskBuilder(
        router_device_id_getter=lambda: router_id,
        router_subinterfaces_getter=lambda: {},
    )
    decision = _decision(
        {sw.id: 20},
        [VlanDecision(vlan_id=30, decision_type="MERGE", criticality=BatchCriticality.NORMAL)],
    )

    with caplog.at_level("WARNING"):
        task = builder.build(decision, state, "test")
    final_batch = task.batches[-1]
    assert [a.action_type for a in final_batch.actions] == [ActionType.DELETE_VLAN]
    assert "Subinterface for vlan_id=30 not found" in caplog.text


def test_merge_includes_delete_subinterface_before_delete_vlan():
    router_id = uuid4()
    sw = _device("sw-1", "10.0.0.1")
    state = _state_with_vlan(
        [20, 30],
        [VlanAssignment(device_id=sw.id, port="Gi0/2", vlan_id=30, mode=PortMode.ACCESS)],
        devices=[sw],
        device_vlans={(sw.id, 30), (router_id, 30)},
    )
    builder = TaskBuilder(
        router_device_id_getter=lambda: router_id,
        router_subinterfaces_getter=lambda: {30: {"name": "GigabitEthernet0/0/1.30"}},
    )
    decision = _decision(
        {sw.id: 20},
        [VlanDecision(vlan_id=30, decision_type="MERGE", criticality=BatchCriticality.NORMAL)],
    )

    task = builder.build(decision, state, "test")
    final_actions = task.batches[-1].actions
    assert final_actions[0].action_type == ActionType.DELETE_SUBINTERFACE
    assert final_actions[1].action_type == ActionType.DELETE_VLAN


@pytest.mark.asyncio
async def test_get_router_subinterfaces_returns_empty_on_failure():
    pool = _Pool(_Conn(exc=RuntimeError("table missing")))
    result = await get_router_subinterfaces(pool)
    assert result == {}


@pytest.mark.asyncio
async def test_get_router_device_id_returns_none_when_missing():
    pool = _Pool(_Conn(row=None))
    result = await get_router_device_id(pool)
    assert result is None


@pytest.mark.asyncio
async def test_get_device_vlans_returns_empty_on_failure(caplog):
    pool = _Pool(_Conn(exc=RuntimeError("device_vlan missing")))
    with caplog.at_level("WARNING"):
        result = await get_device_vlans(pool)
    assert result == set()
    assert "device_vlan unavailable" in caplog.text


@pytest.mark.asyncio
async def test_device_vlans_loaded_into_network_state():
    d1 = uuid4()

    class _ConnByQuery:
        async def fetch(self, query, *params):
            if "SELECT id, hostname, mgmt_ip, device_type, status" in query:
                return [{"id": d1, "hostname": "sw-1", "mgmt_ip": "10.0.0.1", "device_type": "SWITCH", "status": "ACTIVE"}]
            if "iv.mode = 'ACCESS'" in query:
                return [{"device_id": d1, "port": "Gi0/1", "vlan_id": 10, "mode": "ACCESS"}]
            if "iv.mode = 'TRUNK'" in query:
                return []
            if "FROM vlan" in query and "trunk_allowed_vlan" not in query:
                return [{"vlan_id": 10, "name": "v10", "admin_status": "ACTIVE", "oper_status": "UP"}]
            if "FROM device_vlan" in query:
                return [{"device_id": d1, "vlan_id": 10}, {"device_id": d1, "vlan_id": 20}]
            return []

    state = await get_network_state(_ConnByQuery())  # type: ignore[arg-type]
    assert state.device_vlans == {(d1, 10), (d1, 20)}
