from datetime import datetime, timezone

from src.models.operations import BatchCriticality
from src.segmenters.interface import ConstraintSet, SegmentationResult
from src.strategies.interface import NetworkContext
from src.strategies.threshold import ThresholdHeuristic


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


def _ctx(state, metrics):
    return NetworkContext(state=state, metrics=metrics, timestamp=datetime.now(timezone.utc).isoformat())


def test_strategy_high_anomaly_gives_critical_isolate(mock_network_state):
    strategy = ThresholdHeuristic(3.0, 0.85, DummySegmenter(), ConstraintSet())
    decision = strategy.decide(_ctx(mock_network_state, [{"vlan_id": 20, "anomaly_score": 3.8}]))
    assert decision.decisions[0].decision_type == "ISOLATE"
    assert decision.decisions[0].criticality == BatchCriticality.CRITICAL


def test_strategy_high_inter_vlan_ratio_gives_merge(mock_network_state):
    strategy = ThresholdHeuristic(3.0, 0.85, DummySegmenter(), ConstraintSet())
    # High ratio AND meaningful absolute cross-segment traffic -> MERGE.
    decision = strategy.decide(
        _ctx(
            mock_network_state,
            [{"vlan_id": 20, "inter_vlan_ratio": 0.8, "bytes_per_sec": 50_000_000.0}],
        )
    )
    assert decision.decisions[0].decision_type == "MERGE"
    assert decision.decisions[0].criticality == BatchCriticality.NORMAL


def test_strategy_high_ratio_low_traffic_suppresses_merge(mock_network_state):
    strategy = ThresholdHeuristic(3.0, 0.85, DummySegmenter(), ConstraintSet())
    # High ratio but negligible absolute traffic must NOT merge (auto-apply loop guard).
    decision = strategy.decide(
        _ctx(mock_network_state, [{"vlan_id": 20, "inter_vlan_ratio": 0.8, "bytes_per_sec": 10.0}])
    )
    assert decision.decisions == []


def test_strategy_high_util_low_inter_gives_rebalance(mock_network_state):
    strategy = ThresholdHeuristic(3.0, 0.85, DummySegmenter(), ConstraintSet())
    decision = strategy.decide(_ctx(mock_network_state, [{"vlan_id": 20, "utilization": 0.9, "inter_vlan_ratio": 0.1}]))
    assert decision.decisions[0].decision_type == "REBALANCE"


def test_strategy_active_src_spike_gives_isolate(mock_network_state):
    strategy = ThresholdHeuristic(3.0, 0.85, DummySegmenter(), ConstraintSet())
    strategy.decide(_ctx(mock_network_state, [{"vlan_id": 20, "active_src_ips": 2}]))
    decision = strategy.decide(_ctx(mock_network_state, [{"vlan_id": 20, "active_src_ips": 40}]))
    assert decision.decisions[0].decision_type == "ISOLATE"
