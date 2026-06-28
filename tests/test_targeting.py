from src.models.operations import ActionType, BatchCriticality
from src.segmenters.interface import SegmentationResult
from src.strategies.interface import StrategyDecision, VlanDecision
from src.task_builder.builder import TaskBuilder


def _segmentation_for_state(state, vlan_id: int) -> dict:
    return {device.id: vlan_id for device in state.devices}


def test_targeting_creates_separate_batches_per_device(mock_network_state):
    builder = TaskBuilder()
    decision = StrategyDecision(
        decisions=[
            VlanDecision(vlan_id=20, decision_type="ISOLATE", criticality=BatchCriticality.CRITICAL)
        ],
        segmentation_result=SegmentationResult(
            segmentation=_segmentation_for_state(mock_network_state, 999),
            objective_value=0.0,
            iterations=2,
            algorithm_name="dummy",
            duration_seconds=0.1,
        ),
    )

    task = builder.build(decision=decision, state=mock_network_state, initiated_by="test")

    assert len(task.batches) == 2
    expected_ids = sorted(device.id for device in mock_network_state.devices)
    device_ids = sorted({batch.actions[-1].device_id for batch in task.batches})
    assert device_ids == expected_ids


def test_actions_contain_correct_device_port_previous_and_target(mock_network_state):
    builder = TaskBuilder()
    expected_ids = {device.id for device in mock_network_state.devices}
    decision = StrategyDecision(
        decisions=[
            VlanDecision(vlan_id=20, decision_type="ISOLATE", criticality=BatchCriticality.CRITICAL)
        ],
        segmentation_result=SegmentationResult(
            segmentation=_segmentation_for_state(mock_network_state, 999),
            objective_value=0.0,
            iterations=2,
            algorithm_name="dummy",
            duration_seconds=0.1,
        ),
    )

    task = builder.build(decision=decision, state=mock_network_state, initiated_by="test")

    for batch in task.batches:
        switch_actions = [
            a
            for a in batch.actions
            if a.action_type in {ActionType.SET_ACCESS, ActionType.SET_TRUNK, ActionType.SWITCH_VLAN}
        ]
        assert len(switch_actions) == 2
        for action in switch_actions:
            assert action.device_id in expected_ids
            assert action.params["port"] in {"Gi0/1", "Gi0/2"}
            if action.action_type == ActionType.SET_ACCESS:
                assert action.params["vlan_id"] == 999
                assert action.previous_state["mode"] == "ACCESS"
                assert action.target_state["mode"] == "ACCESS"
                assert action.target_state["vlan_id"] == 999
            elif action.action_type == ActionType.SET_TRUNK:
                assert action.params["allowed_vlans"] == [999]
                assert action.target_state["mode"] == "TRUNK"
                assert action.target_state["allowed_vlans"] == [999]
            else:
                assert action.previous_state["vlan_id"] in {10, 20, 30}
                assert action.target_state["vlan_id"] == 999


def test_empty_targets_return_empty_batches(single_vlan_state):
    builder = TaskBuilder()
    decision = StrategyDecision(
        decisions=[
            VlanDecision(vlan_id=777, decision_type="ISOLATE", criticality=BatchCriticality.CRITICAL)
        ],
        segmentation_result=SegmentationResult(
            segmentation=_segmentation_for_state(single_vlan_state, 20),
            objective_value=0.0,
            iterations=1,
            algorithm_name="dummy",
            duration_seconds=0.1,
        ),
    )

    task = builder.build(decision=decision, state=single_vlan_state, initiated_by="test")

    assert task.batches == []


def test_add_vlan_is_first_action_before_switch(mock_network_state):
    builder = TaskBuilder()
    decision = StrategyDecision(
        decisions=[
            VlanDecision(vlan_id=20, decision_type="ISOLATE", criticality=BatchCriticality.CRITICAL)
        ],
        segmentation_result=SegmentationResult(
            segmentation=_segmentation_for_state(mock_network_state, 999),
            objective_value=0.0,
            iterations=2,
            algorithm_name="dummy",
            duration_seconds=0.1,
        ),
    )

    task = builder.build(decision=decision, state=mock_network_state, initiated_by="test")

    for batch in task.batches:
        assert batch.actions[0].action_type == ActionType.ADD_VLAN
        assert any(
            a.action_type in {ActionType.SET_ACCESS, ActionType.SET_TRUNK, ActionType.SWITCH_VLAN}
            for a in batch.actions[1:]
        )
