from datetime import datetime, timezone

from src.models.operations import BatchCriticality
from src.segmenters.interface import ConstraintSet, SegmentationResult
from src.strategies.interface import NetworkContext
from src.strategies.threshold import ThresholdHeuristic


class DummySegmenter:
    def __init__(self) -> None:
        self.called = 0

    def optimize(self, model, constraints, current):
        self.called += 1
        return SegmentationResult(
            segmentation=dict(current),
            objective_value=model.evaluate(current),
            iterations=1,
            algorithm_name="dummy",
            duration_seconds=0.0,
        )

    def get_name(self) -> str:
        return "dummy"

    def get_complexity(self) -> str:
        return "O(1)"


def test_decide_returns_isolate_for_anomalous_vlan(mock_network_state):
    segmenter = DummySegmenter()
    strategy = ThresholdHeuristic(
        anomaly_threshold=3.0,
        bandwidth_threshold=0.85,
        segmenter=segmenter,
        constraints=ConstraintSet(),
    )
    ctx = NetworkContext(
        state=mock_network_state,
        metrics=[{"vlan_id": 20, "anomaly_score": 3.5, "utilization": 0.4}],
        timestamp=datetime.now(timezone.utc).isoformat(),
    )

    decision = strategy.decide(ctx)

    assert len(decision.decisions) == 1
    assert decision.segmentation_result is not None
    vlan_decision = decision.decisions[0]
    assert vlan_decision.vlan_id == 20
    assert vlan_decision.decision_type == "ISOLATE"
    assert vlan_decision.criticality == BatchCriticality.CRITICAL
    assert segmenter.called == 1


def test_decide_returns_empty_when_thresholds_not_exceeded(mock_network_state):
    segmenter = DummySegmenter()
    strategy = ThresholdHeuristic(
        anomaly_threshold=3.0,
        bandwidth_threshold=0.85,
        segmenter=segmenter,
    )
    ctx = NetworkContext(
        state=mock_network_state,
        metrics=[{"vlan_id": 20, "anomaly_score": 2.1, "utilization": 0.2}],
        timestamp=datetime.now(timezone.utc).isoformat(),
    )

    decision = strategy.decide(ctx)

    assert decision.decisions == []
    assert decision.segmentation_result is None
    assert segmenter.called == 0


def test_decide_returns_two_anomalous_vlans(mock_network_state):
    segmenter = DummySegmenter()
    strategy = ThresholdHeuristic(
        anomaly_threshold=3.0,
        bandwidth_threshold=0.85,
        segmenter=segmenter,
    )
    ctx = NetworkContext(
        state=mock_network_state,
        metrics=[
            {"vlan_id": 10, "anomaly_score": 3.2, "utilization": 0.1},
            {"vlan_id": 30, "anomaly_score": 4.1, "utilization": 0.7},
        ],
        timestamp=datetime.now(timezone.utc).isoformat(),
    )

    decision = strategy.decide(ctx)

    vlan_ids = sorted(item.vlan_id for item in decision.decisions)
    assert vlan_ids == [10, 30]
    assert decision.segmentation_result is not None
    assert segmenter.called == 1
