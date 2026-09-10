"""Интеграционный тест IPAM-модуля с Decision Engine и Kafka."""

from __future__ import annotations

import asyncio
import json
import logging
import pytest
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from src.config import settings
from src.db.network_state import NetworkState
from src.ipam.ipam_service import IPAMService, SubnetCalculator
from src.kafka.producer import TaskProducer
from src.models.network import Device, DeviceType, NetworkState, PortMode, VlanAssignment, VlanInfo
from src.models.operations import ActionType, BatchCriticality, ReconfigurationTask
from src.segmenters.interface import ConstraintSet, SegmentationResult
from src.strategies.interface import NetworkContext, StrategyDecision, VlanDecision
from src.strategies.threshold import ThresholdHeuristic
from src.task_builder.builder import TaskBuilder

logger = logging.getLogger(__name__)

class MockKafkaProducer:
    """Mock Kafka producer для тестирования."""
    def __init__(self):
        self.published_tasks: list[ReconfigurationTask] = []
        self.started = False
        self._bootstrap_marker_enabled = False

    async def start(self):
        self.started = True

    async def stop(self):
        pass

    async def publish(self, task: ReconfigurationTask):
        self.published_tasks.append(task)
        logger.info(f"Published task {task.id} with {len(task.batches)} batches")

class DummySegmenter:
    def optimize(self, model, constraints, current):
        return SegmentationResult(
            segmentation=dict(current),
            objective_value=model.evaluate(current),
            iterations=1,
            algorithm_name="dummy",
            duration_seconds=0.0,
        )
    def get_name(self):
        return "dummy"
    def get_complexity(self):
        return "O(1)"

def create_test_state(
    device_count: int = 60,
    vlan_id: int = 10,
    with_ip_prefix: str | None = "192.168.10.0/24",
) -> NetworkState:
    """Создать тестовое состояние с VLAN."""
    devices = []
    assignments = []
    device_vlans = set()

    for i in range(device_count):
        d_id = uuid4()
        devices.append(
            Device(
                id=d_id,
                hostname=f"sw-{i+1}",
                mgmt_ip=f"10.0.0.{i+1}",
                device_type=DeviceType.SWITCH,
                status="ACTIVE",
            )
        )
        assignments.append(
            VlanAssignment(
                device_id=d_id,
                port=f"Gi0/{i+1}",
                vlan_id=vlan_id,
                mode=PortMode.ACCESS,
            )
        )
        device_vlans.add((d_id, vlan_id))

    # Создаём VLAN с IP-префиксом
    vlan = VlanInfo(
        vlan_id=vlan_id,
        name="test-vlan",
        admin_status="ACTIVE",
        oper_status="UP",
        ip_prefix=with_ip_prefix,
    )

    return NetworkState(
        devices=devices,
        vlans=[vlan],
        assignments=assignments,
        device_vlans=device_vlans,
        protected_vlans=set(),
        timestamp=datetime.now(timezone.utc),
    )

def create_router_state() -> tuple[NetworkState, UUID]:
    """Создать состояние с роутером."""
    router_id = uuid4()
    device = Device(
        id=router_id,
        hostname="router-1",
        mgmt_ip="10.0.0.254",
        device_type=DeviceType.ROUTER,
        status="ACTIVE",
    )
    state = NetworkState(
        devices=[device],
        vlans=[],
        assignments=[],
        device_vlans=set(),
        protected_vlans=set(),
        timestamp=datetime.now(timezone.utc),
    )
    return state, router_id

class TestIPAMIntegration:
    """Интеграционные тесты IPAM-модуля."""

    @pytest.mark.asyncio
    async def test_split_full_integration(self):
        """Полный интеграционный тест SPLIT."""
        print("ТЕСТ: SPLIT FULL INTEGRATION")
        
        # 1. Создаём состояние с большим VLAN
        print("\n1. Создание тестового состояния:")
        print("   VLAN 10 с префиксом 192.168.10.0/24")
        print("   60 устройств в VLAN 10")
        
        state = create_test_state(device_count=60, vlan_id=10, with_ip_prefix="192.168.10.0/24")
        router_state, router_id = create_router_state()
        
        print(f"   Роутер: {router_id}")
        
        # Объединяем состояния
        all_devices = state.devices + router_state.devices
        all_vlans = state.vlans
        all_assignments = state.assignments
        all_device_vlans = state.device_vlans | {(router_id, 10)}
        
        full_state = NetworkState(
            devices=all_devices,
            vlans=all_vlans,
            assignments=all_assignments,
            device_vlans=all_device_vlans,
            protected_vlans=set(),
            timestamp=datetime.now(timezone.utc),
        )

        # 2. Создаём SPLIT решение
        print("\n2. Создание SPLIT решения:")
        print("   VLAN 10 -> VLAN 20, VLAN 30")
        
        split_decision = StrategyDecision(
            decisions=[
                VlanDecision(
                    vlan_id=10,
                    decision_type="SPLIT",
                    criticality=BatchCriticality.NORMAL,
                    child_vlan_ids=(20, 30),
                )
            ],
            segmentation_result=None,
        )

        # 3. Создаём IPAM сервис и TaskBuilder
        print("\n3. Создание IPAMService и TaskBuilder")
        ipam_service = IPAMService(db_pool=None)
        task_builder = TaskBuilder(
            router_device_id_getter=lambda: router_id,
            router_subinterfaces_getter=lambda: {},
            ipam_service=ipam_service,
        )

        # 4. Собираем задачу
        print("\n4. Сборка задачи...")
        task = task_builder.build(split_decision, full_state, "integration-test-split")
        print(f"   Создано {len(task.batches)} батчей")

        # 5. Публикуем в Kafka
        print("\n5. Публикация в Kafka (mock)...")
        producer = MockKafkaProducer()
        await producer.start()
        await producer.publish(task)
        
        print(f"   Опубликовано задач: {len(producer.published_tasks)}")

        # 6. Проверяем результат
        print("\n6. Проверка результата:")
        assert len(producer.published_tasks) == 1
        published_task = producer.published_tasks[0]

        # Проверяем действия
        all_actions = [a for batch in published_task.batches for a in batch.actions]
        action_types = [a.action_type for a in all_actions]
        
        print(f"   Всего действий: {len(all_actions)}")
        print(f"   Типы действий: {set(action_types)}")
        
        assert ActionType.DELETE_SUBINTERFACE in action_types
        assert ActionType.CREATE_SUBINTERFACE in action_types
        assert action_types.count(ActionType.CREATE_SUBINTERFACE) == 2
        assert ActionType.UPDATE_VLAN_PREFIX in action_types

        # Проверяем IP-адреса
        create_actions = [a for a in all_actions if a.action_type == ActionType.CREATE_SUBINTERFACE]
        print("\n   CREATE_SUBINTERFACE действия:")
        for action in create_actions:
            ip = action.params.get("ip_address")
            print(f"     VLAN {action.params.get('vlan_id')}: {ip}")
            assert ip is not None
            assert ip.startswith("192.168.10.")

        print("\nSPLIT интеграционный тест пройден!")

    @pytest.mark.asyncio
    async def test_merge_full_integration(self):
        """Полный интеграционный тест MERGE."""
        print("🧪 ТЕСТ: MERGE FULL INTEGRATION")
        
        # 1. Создаём состояние с двумя VLAN
        print("\n1. Создание тестового состояния:")
        print("   VLAN 10 с префиксом 192.168.10.0/25 (10 устройств)")
        print("   VLAN 20 с префиксом 192.168.10.128/25 (15 устройств)")
        
        state1 = create_test_state(device_count=10, vlan_id=10, with_ip_prefix="192.168.10.0/25")
        state2 = create_test_state(device_count=15, vlan_id=20, with_ip_prefix="192.168.10.128/25")
        router_state, router_id = create_router_state()

        # Объединяем состояния
        all_devices = state1.devices + state2.devices + router_state.devices
        all_vlans = state1.vlans + state2.vlans
        all_assignments = state1.assignments + state2.assignments
        all_device_vlans = state1.device_vlans | state2.device_vlans | {(router_id, 10), (router_id, 20)}

        full_state = NetworkState(
            devices=all_devices,
            vlans=all_vlans,
            assignments=all_assignments,
            device_vlans=all_device_vlans,
            protected_vlans=set(),
            timestamp=datetime.now(timezone.utc),
        )

        # 2. Создаём MERGE решение
        print("\n2. Создание MERGE решения:")
        print("   VLAN 10 + VLAN 20 -> VLAN 20")
        
        merge_decision = StrategyDecision(
            decisions=[
                VlanDecision(
                    vlan_id=10,
                    decision_type="MERGE",
                    criticality=BatchCriticality.NORMAL,
                    target_vlan_id=20,
                )
            ],
            segmentation_result=None,
        )

        # 3. Создаём IPAM сервис и TaskBuilder
        print("\n3. Создание IPAMService и TaskBuilder")
        ipam_service = IPAMService(db_pool=None)
        task_builder = TaskBuilder(
            router_device_id_getter=lambda: router_id,
            router_subinterfaces_getter=lambda: {},
            ipam_service=ipam_service,
        )

        # 4. Собираем задачу
        print("\n4. Сборка задачи...")
        task = task_builder.build(merge_decision, full_state, "integration-test-merge")
        print(f"   Создано {len(task.batches)} батчей")

        # 5. Публикуем в Kafka (mock)
        print("\n5. Публикация в Kafka (mock)...")
        producer = MockKafkaProducer()
        await producer.start()
        await producer.publish(task)
        
        print(f"   Опубликовано задач: {len(producer.published_tasks)}")

        # 6. Проверяем результат
        print("\n6. Проверка результата:")
        assert len(producer.published_tasks) == 1
        published_task = producer.published_tasks[0]

        # Проверяем действия
        all_actions = [a for batch in published_task.batches for a in batch.actions]
        action_types = [a.action_type for a in all_actions]

        print(f"   Всего действий: {len(all_actions)}")
        print(f"   Типы действий: {set(action_types)}")
        
        # Должны быть: 2×DELETE_SUBINTERFACE + CREATE_SUBINTERFACE + UPDATE_VLAN_PREFIX
        assert action_types.count(ActionType.DELETE_SUBINTERFACE) == 2
        assert ActionType.CREATE_SUBINTERFACE in action_types
        assert ActionType.UPDATE_VLAN_PREFIX in action_types

        # Проверяем, что IP-адрес изменился на /24
        create_actions = [a for a in all_actions if a.action_type == ActionType.CREATE_SUBINTERFACE]
        print("\n   CREATE_SUBINTERFACE действия:")
        for action in create_actions:
            ip = action.params.get("ip_address")
            print(f"     VLAN {action.params.get('vlan_id')}: {ip}")
            assert ip is not None
            assert ip == "192.168.10.1/24" or ip.startswith("192.168.10.1")

        print("\nMERGE интеграционный тест пройден!")

@pytest.mark.asyncio
async def test_ipam_with_threshold_strategy():
    """Тест интеграции IPAM с Threshold стратегией."""
    print("🧪 ТЕСТ: IPAM + THRESHOLD STRATEGY")
    
    # 1. Создаём состояние с большим VLAN и метриками
    print("\n1. Создание тестового состояния:")
    print("   VLAN 10 с префиксом 192.168.10.0/24 (60 устройств)")
    
    state = create_test_state(device_count=60, vlan_id=10, with_ip_prefix="192.168.10.0/24")
    router_state, router_id = create_router_state()
    
    full_state = NetworkState(
        devices=state.devices + router_state.devices,
        vlans=state.vlans,
        assignments=state.assignments,
        device_vlans=state.device_vlans | {(router_id, 10)},
        protected_vlans=set(),
        timestamp=datetime.now(timezone.utc),
    )
    
    # 2. Создаём метрики
    print("\n2. Создание метрик:")
    metrics = [
        {
            "vlan_id": 10,
            "anomaly_score": 0.5,
            "utilization": 0.6,
            "icmp_per_sec": 10.0,
            "active_src_ips": 25,
            "inter_vlan_ratio": 0.1,
            "max_flow_bytes": 10_000_000.0,
            "bytes_per_sec": 50_000_000.0,
        }
    ]
    print(f"   VLAN 10: utilization=0.6, active_src_ips=25")

    # 3. Создаём стратегию с низким порогом для SPLIT
    segmenter = DummySegmenter()
    strategy = ThresholdHeuristic(
        anomaly_threshold=3.0,
        bandwidth_threshold=0.85,
        segmenter=segmenter,
        constraints=ConstraintSet(),
        split_min_devices=50,  # SPLIT при >50 устройств
    )

    print("\n3. Запуск Threshold стратегии...")
    print("   split_min_devices=50")

    # 4. Применяем стратегию
    ctx = NetworkContext(
        state=full_state,
        metrics=metrics,
        timestamp=datetime.now(timezone.utc).isoformat(),
    )
    decision = strategy.decide(ctx)

    # 5. Проверяем, что принято решение SPLIT
    print("\n4. Проверка решения стратегии:")
    split_decisions = [d for d in decision.decisions if d.decision_type == "SPLIT"]
    assert len(split_decisions) == 1, "SPLIT decision not generated"
    
    split = split_decisions[0]
    print(f"   Решение: SPLIT VLAN {split.vlan_id} -> {split.child_vlan_ids}")
    
    assert split.vlan_id == 10
    assert split.child_vlan_ids is not None
    assert len(split.child_vlan_ids) == 2

    # 6. Собираем задачу через TaskBuilder
    print("\n5. Сборка задачи через TaskBuilder...")
    ipam_service = IPAMService(db_pool=None)
    task_builder = TaskBuilder(
        router_device_id_getter=lambda: router_id,
        ipam_service=ipam_service,
    )

    task = task_builder.build(decision, full_state, "threshold-ipam-test")
    print(f"   Создано {len(task.batches)} батчей")

    # 7. Проверяем результат
    print("\n6. Проверка результата:")
    all_actions = [a for batch in task.batches for a in batch.actions]
    action_types = [a.action_type for a in all_actions]

    print(f"   Всего действий: {len(all_actions)}")
    print(f"   Типы действий: {set(action_types)}")

    assert ActionType.DELETE_SUBINTERFACE in action_types
    assert action_types.count(ActionType.CREATE_SUBINTERFACE) == 2
    assert ActionType.UPDATE_VLAN_PREFIX in action_types

    print("\nIPAM + Threshold стратегия интеграционный тест пройден!")

if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])