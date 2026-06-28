from src.models.operations import BatchCriticality
from src.segmenters.interface import SegmentationResult
from src.strategies.interface import StrategyDecision, VlanDecision
from src.task_builder.builder import TaskBuilder


def _segmentation_from_values(state, values: list[int]) -> dict:
    return {device.id: values[idx] for idx, device in enumerate(state.devices)}


def test_two_devices_in_vlan_create_two_batches(mock_network_state):
    builder = TaskBuilder()
    decision = StrategyDecision(
        decisions=[
            VlanDecision(vlan_id=20, decision_type="REBALANCE", criticality=BatchCriticality.NORMAL)
        ],
        segmentation_result=SegmentationResult(
            segmentation=_segmentation_from_values(mock_network_state, [30, 10]),
            objective_value=0.0,
            iterations=2,
            algorithm_name="dummy",
            duration_seconds=0.1,
        ),
    )

    task = builder.build(decision=decision, state=mock_network_state, initiated_by="test")

    assert len(task.batches) == 2


def test_empty_vlan_yields_empty_task(single_vlan_state):
    builder = TaskBuilder()
    decision = StrategyDecision(
        decisions=[
            VlanDecision(vlan_id=555, decision_type="REBALANCE", criticality=BatchCriticality.NORMAL)
        ],
        segmentation_result=SegmentationResult(
            segmentation=_segmentation_from_values(single_vlan_state, [20, 20]),
            objective_value=0.0,
            iterations=1,
            algorithm_name="dummy",
            duration_seconds=0.1,
        ),
    )

    task = builder.build(decision=decision, state=single_vlan_state, initiated_by="test")

    assert task.batches == []


def test_batch_criticality_transferred_from_strategy_decision(mock_network_state):
    builder = TaskBuilder()
    decision = StrategyDecision(
        decisions=[
            VlanDecision(vlan_id=20, decision_type="REBALANCE", criticality=BatchCriticality.OPTIONAL),
            VlanDecision(vlan_id=10, decision_type="REBALANCE", criticality=BatchCriticality.OPTIONAL),
            VlanDecision(vlan_id=30, decision_type="REBALANCE", criticality=BatchCriticality.OPTIONAL),
        ],
        segmentation_result=SegmentationResult(
            segmentation=_segmentation_from_values(mock_network_state, [30, 10]),
            objective_value=0.0,
            iterations=1,
            algorithm_name="dummy",
            duration_seconds=0.1,
        ),
    )

    task = builder.build(decision=decision, state=mock_network_state, initiated_by="test")

    assert task.batches
    assert all(batch.criticality == BatchCriticality.OPTIONAL for batch in task.batches)
