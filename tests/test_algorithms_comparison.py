from __future__ import annotations

import json
import random
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from src.models.network import Device, DeviceType, NetworkState, PortMode, VlanAssignment, VlanInfo
from src.network.model import NetworkModel
from src.segmenters.greedy import GreedySegmenter
from src.segmenters.interface import ConstraintSet
from src.segmenters.simulated_annealing import SimulatedAnnealingSegmenter
from src.segmenters.spectral import SpectralSegmenter


def _build_state_and_metrics(
    sizes: list[int],
    intra_weight: float,
    inter_weight: float,
    noise_edges: int,
    seed: int,
) -> tuple[NetworkState, list[dict], dict[object, int], dict[object, int]]:
    rng = random.Random(seed)

    total_devices = sum(sizes)
    vlan_ids = [10 + idx * 10 for idx in range(len(sizes))]

    devices: list[Device] = []
    assignments: list[VlanAssignment] = []
    vlans = [VlanInfo(vlan_id=vlan_id, name=f"vlan-{vlan_id}", admin_status="ACTIVE", oper_status="UP") for vlan_id in vlan_ids]

    ordered_ids: list[object] = []
    true_segmentation: dict[object, int] = {}
    device_num = 1
    for vlan_id, size in zip(vlan_ids, sizes):
        for _ in range(size):
            d_id = uuid4()
            ordered_ids.append(d_id)
            devices.append(
                Device(
                    id=d_id,
                    hostname=f"sw-{device_num}",
                    mgmt_ip=f"10.0.0.{device_num}",
                    device_type=DeviceType.SWITCH,
                    status="ACTIVE",
                )
            )
            assignments.append(
                VlanAssignment(
                    device_id=d_id,
                    port="Gi0/1",
                    vlan_id=vlan_id,
                    mode=PortMode.ACCESS,
                )
            )
            true_segmentation[d_id] = vlan_id
            device_num += 1

    metrics: list[dict] = []
    for i, src in enumerate(ordered_ids):
        for dst in ordered_ids[i + 1:]:
            same_vlan = true_segmentation[src] == true_segmentation[dst]
            base = intra_weight if same_vlan else inter_weight
            weight = max(0.0, base + rng.uniform(-0.15 * base, 0.15 * base))
            metrics.append(
                {
                    "src_device_id": src,
                    "dst_device_id": dst,
                    "bytes_total": weight,
                    "vlan_id": true_segmentation[src],
                }
            )

    for _ in range(noise_edges):
        src = rng.choice(ordered_ids)
        dst = rng.choice(ordered_ids)
        if src == dst:
            continue
        metrics.append(
            {
                "src_device_id": src,
                "dst_device_id": dst,
                "bytes_total": intra_weight * rng.uniform(0.3, 0.7),
                "vlan_id": true_segmentation[src],
            }
        )

    initial = dict(true_segmentation)
    shuffled_devices = list(initial.keys())
    rng.shuffle(shuffled_devices)
    swaps = max(1, total_devices // 5)
    for idx in range(swaps):
        d_id = shuffled_devices[idx]
        current_vlan = initial[d_id]
        alternatives = [v for v in vlan_ids if v != current_vlan]
        initial[d_id] = rng.choice(alternatives)

    state = NetworkState(
        devices=devices,
        vlans=vlans,
        assignments=assignments,
        timestamp=datetime.now(timezone.utc),
    )
    return state, metrics, initial, true_segmentation


def _run_algorithms(name: str, state: NetworkState, metrics: list[dict], initial: dict[object, int]):
    model = NetworkModel(state, metrics)
    constraints = ConstraintSet(min_vlan_size=1, max_vlan_size=100)
    baseline = model.evaluate(initial)

    algorithms = {
        "greedy": GreedySegmenter(max_iterations=80, lambda_coeff=0.5),
        "spectral": SpectralSegmenter(n_clusters=len(set(initial.values())), random_state=42),
        "sa": SimulatedAnnealingSegmenter(
            initial_temperature=80.0,
            cooling_rate=0.96,
            max_iterations=600,
            random_seed=42,
        ),
    }

    results: dict[str, dict] = {}
    for algo_name, algo in algorithms.items():
        result = algo.optimize(model=model, constraints=constraints, current=initial)
        violations = constraints.validate(result.segmentation)
        results[algo_name] = {
            "objective_value": result.objective_value,
            "duration_seconds": result.duration_seconds,
            "iterations": result.iterations,
            "violations_count": len(violations),
        }

    return baseline, results


def test_algorithms_comparison_generates_report():
    small_state, small_metrics, small_initial, _ = _build_state_and_metrics(
        sizes=[5, 5, 5, 5],
        intra_weight=1000.0,
        inter_weight=100.0,
        noise_edges=20,
        seed=1,
    )
    medium_state, medium_metrics, medium_initial, _ = _build_state_and_metrics(
        sizes=[5, 7, 8, 9, 9, 12],
        intra_weight=900.0,
        inter_weight=220.0,
        noise_edges=120,
        seed=2,
    )
    large_state, large_metrics, large_initial, _ = _build_state_and_metrics(
        sizes=[12, 12, 12, 12, 13, 13, 13, 13],
        intra_weight=500.0,
        inter_weight=420.0,
        noise_edges=240,
        seed=3,
    )

    small_baseline, small_results = _run_algorithms("small", small_state, small_metrics, small_initial)
    medium_baseline, medium_results = _run_algorithms("medium", medium_state, medium_metrics, medium_initial)
    large_baseline, large_results = _run_algorithms("large", large_state, large_metrics, large_initial)

    output = {
        "small_topology": {
            "size": 20,
            "baseline_objective": small_baseline,
            "results": small_results,
        },
        "medium_topology": {
            "size": 50,
            "baseline_objective": medium_baseline,
            "results": medium_results,
        },
        "large_topology": {
            "size": 100,
            "baseline_objective": large_baseline,
            "results": large_results,
        },
    }

    result_path = Path(__file__).resolve().parent / "comparison_results.json"
    result_path.write_text(json.dumps(output, indent=2), encoding="utf-8")

    for algo_name, values in small_results.items():
        assert values["objective_value"] > small_baseline, f"{algo_name} did not improve small topology"

    for values in medium_results.values():
        assert values["duration_seconds"] <= 30.0

    for topology in (small_results, medium_results, large_results):
        for values in topology.values():
            assert values["violations_count"] == 0
