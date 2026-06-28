"""Regression tests for the auto-apply MERGE loop.

Background
----------
When the Decision Engine was allowed to apply tasks without confirmation it
entered a perpetual MERGE loop. The trigger was *low inter-segment traffic*:
near-idle VLANs repeatedly exceeded the relative ``inter_vlan_ratio`` threshold
on negligible absolute traffic, so ``ThresholdHeuristic`` kept emitting MERGE
decisions and the task builder collapsed those VLANs into the largest neighbor.
Each applied merge changed the topology, the next poll merged again, and the
network configuration churned / collapsed.

These tests pin down two properties:
1. Strategy layer: a high inter_vlan_ratio on negligible absolute traffic must
   NOT produce a MERGE decision (the actual loop driver), while genuinely heavy
   cross-segment traffic must still merge.
2. Segmenter layer (guardrail): greedy must not collapse cohesive VLANs joined
   only by a trickle of traffic, and must reach a stable fixed point so that
   feeding its output back in yields no further moves.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from src.models.network import Device, DeviceType, NetworkState, PortMode, VlanAssignment, VlanInfo
from src.network.model import NetworkModel
from src.segmenters.greedy import GreedySegmenter
from src.segmenters.interface import ConstraintSet, SegmentationResult
from src.strategies.interface import NetworkContext
from src.strategies.threshold import ThresholdHeuristic


class _DummySegmenter:
    def optimize(self, model, constraints, current):
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


def _ctx(state, metrics):
    return NetworkContext(state=state, metrics=metrics, timestamp="t")


def _single_vlan_state() -> NetworkState:
    d1, d2 = uuid4(), uuid4()
    return NetworkState(
        devices=[
            Device(id=d1, hostname="a", mgmt_ip="10.0.0.1", device_type=DeviceType.SWITCH, status="ACTIVE"),
            Device(id=d2, hostname="b", mgmt_ip="10.0.0.2", device_type=DeviceType.SWITCH, status="ACTIVE"),
        ],
        vlans=[VlanInfo(vlan_id=20, name="x", admin_status="ACTIVE", oper_status="UP")],
        assignments=[
            VlanAssignment(device_id=d1, port="Gi0/1", vlan_id=20, mode=PortMode.ACCESS),
            VlanAssignment(device_id=d2, port="Gi0/2", vlan_id=20, mode=PortMode.ACCESS),
        ],
        device_vlans={(d1, 20), (d2, 20)},
        timestamp=datetime.now(timezone.utc),
    )


def _sparse_topology(n_vlans: int = 6, intra: float = 2.0, inter: float = 1.0):
    """Many small VLANs with little internal traffic, chained by tiny links.

    Mimics the production state after several ISOLATE actions: lots of near-idle
    segments with only a thin cross-VLAN flow between them.
    """
    devices: list[Device] = []
    assignments: list[VlanAssignment] = []
    segmentation: dict[object, int] = {}
    vlan_ids = [10 * (i + 1) for i in range(n_vlans)]
    per_vlan: dict[int, list[object]] = {}

    k = 1
    for vlan_id in vlan_ids:
        per_vlan[vlan_id] = []
        for _ in range(2):
            d_id = uuid4()
            devices.append(
                Device(
                    id=d_id,
                    hostname=f"sw-{k}",
                    mgmt_ip=f"10.0.0.{k}",
                    device_type=DeviceType.SWITCH,
                    status="ACTIVE",
                )
            )
            assignments.append(
                VlanAssignment(device_id=d_id, port="Gi0/1", vlan_id=vlan_id, mode=PortMode.ACCESS)
            )
            segmentation[d_id] = vlan_id
            per_vlan[vlan_id].append(d_id)
            k += 1

    metrics: list[dict] = []
    for vlan_id in vlan_ids:
        a, b = per_vlan[vlan_id]
        metrics.append({"src_device_id": a, "dst_device_id": b, "bytes_total": intra, "vlan_id": vlan_id})
    for i in range(len(vlan_ids) - 1):
        a = per_vlan[vlan_ids[i]][0]
        b = per_vlan[vlan_ids[i + 1]][0]
        metrics.append({"src_device_id": a, "dst_device_id": b, "bytes_total": inter, "vlan_id": vlan_ids[i]})

    state = NetworkState(
        devices=devices,
        vlans=[
            VlanInfo(vlan_id=v, name=str(v), admin_status="ACTIVE", oper_status="UP")
            for v in vlan_ids
        ],
        assignments=assignments,
        timestamp=datetime.now(timezone.utc),
    )
    return state, metrics, segmentation


# --- Strategy layer: the actual production loop driver -----------------------

def test_high_ratio_but_negligible_traffic_does_not_merge():
    """A noisy high inter_vlan_ratio on a near-idle VLAN must NOT trigger MERGE."""
    strategy = ThresholdHeuristic(3.0, 0.85, _DummySegmenter(), ConstraintSet())
    state = _single_vlan_state()
    metrics = [{"vlan_id": 20, "inter_vlan_ratio": 0.8, "bytes_per_sec": 10.0}]

    decision = strategy.decide(_ctx(state, metrics))

    assert [d.decision_type for d in decision.decisions] == [], (
        "MERGE was emitted on negligible absolute inter-segment traffic"
    )


def test_high_ratio_with_real_traffic_still_merges():
    """When inter-segment traffic is genuinely large, MERGE must still fire."""
    strategy = ThresholdHeuristic(3.0, 0.85, _DummySegmenter(), ConstraintSet())
    state = _single_vlan_state()
    metrics = [{"vlan_id": 20, "inter_vlan_ratio": 0.8, "bytes_per_sec": 50_000_000.0}]

    decision = strategy.decide(_ctx(state, metrics))

    assert [d.decision_type for d in decision.decisions] == ["MERGE"]


# --- Segmenter layer: guardrails --------------------------------------------

def test_low_inter_traffic_does_not_collapse_vlans():
    """A trickle of cross-VLAN traffic must not justify merging cohesive segments."""
    state, metrics, segmentation = _sparse_topology()
    model = NetworkModel(state, metrics)

    result = GreedySegmenter(max_iterations=80).optimize(model, ConstraintSet(), dict(segmentation))

    initial_vlans = len(set(segmentation.values()))
    final_vlans = len(set(result.segmentation.values()))
    assert final_vlans == initial_vlans, (
        f"greedy collapsed {initial_vlans} VLANs down to {final_vlans} on negligible "
        "inter-segment traffic"
    )


def test_greedy_reaches_stable_fixed_point():
    """Re-running greedy on its own output must not keep changing the segmentation."""
    state, metrics, segmentation = _sparse_topology()
    model = NetworkModel(state, metrics)
    segmenter = GreedySegmenter(max_iterations=80)

    current = dict(segmentation)
    for _ in range(6):
        result = segmenter.optimize(model, ConstraintSet(), current)
        if result.segmentation == current:
            break
        current = result.segmentation

    rerun = segmenter.optimize(model, ConstraintSet(), current)
    assert rerun.segmentation == current, "greedy did not converge to a stable fixed point"


def test_significant_inter_traffic_still_merges_segmenter():
    """When cross-VLAN traffic genuinely dominates, greedy must still merge."""
    state, metrics, segmentation = _sparse_topology(n_vlans=2, intra=1.0, inter=500.0)
    model = NetworkModel(state, metrics)

    result = GreedySegmenter(max_iterations=80).optimize(model, ConstraintSet(), dict(segmentation))

    assert len(set(result.segmentation.values())) == 1, (
        "greedy refused to merge VLANs dominated by cross-segment traffic"
    )
