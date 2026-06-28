from __future__ import annotations

from dataclasses import dataclass, field
from time import perf_counter
from typing import Protocol

from src.network.model import NetworkModel

Segmentation = dict[int, int]


@dataclass(slots=True)
class SegmentationResult:
    segmentation: Segmentation
    objective_value: float
    iterations: int
    algorithm_name: str
    duration_seconds: float


@dataclass(slots=True)
class ConstraintSet:
    min_vlan_size: int = 1
    max_vlan_size: int = 100
    max_vlans_per_device: int = 4096
    forbidden_pairs: list[tuple[int, int]] = field(default_factory=list)
    required_pairs: list[tuple[int, int]] = field(default_factory=list)

    def validate(self, segmentation: Segmentation) -> list[str]:
        violations: list[str] = []
        if not segmentation:
            return violations

        vlan_to_devices: dict[int, list[int]] = {}
        for device_id, vlan_id in segmentation.items():
            vlan_to_devices.setdefault(vlan_id, []).append(device_id)

        for vlan_id, devices in vlan_to_devices.items():
            if len(devices) < self.min_vlan_size:
                violations.append(
                    f"VLAN {vlan_id} size {len(devices)} is below min_vlan_size={self.min_vlan_size}"
                )
            if len(devices) > self.max_vlan_size:
                violations.append(
                    f"VLAN {vlan_id} size {len(devices)} exceeds max_vlan_size={self.max_vlan_size}"
                )

        if self.max_vlans_per_device < 1:
            violations.append("max_vlans_per_device must be >= 1")

        for left, right in self.forbidden_pairs:
            left_vlan = segmentation.get(left)
            right_vlan = segmentation.get(right)
            if left_vlan is not None and right_vlan is not None and left_vlan == right_vlan:
                violations.append(
                    f"Forbidden pair ({left}, {right}) is in the same VLAN {left_vlan}"
                )

        for left, right in self.required_pairs:
            left_vlan = segmentation.get(left)
            right_vlan = segmentation.get(right)
            if left_vlan is not None and right_vlan is not None and left_vlan != right_vlan:
                violations.append(
                    f"Required pair ({left}, {right}) is split across VLANs {left_vlan} and {right_vlan}"
                )

        return violations


class Segmenter(Protocol):
    def optimize(
        self,
        model: NetworkModel,
        constraints: ConstraintSet,
        current: Segmentation,
    ) -> SegmentationResult:
        ...

    def get_name(self) -> str:
        ...

    def get_complexity(self) -> str:
        ...


def build_passthrough_result(name: str, model: NetworkModel, current: Segmentation) -> SegmentationResult:
    start = perf_counter()
    value = model.evaluate(current)
    return SegmentationResult(
        segmentation=dict(current),
        objective_value=value,
        iterations=0,
        algorithm_name=name,
        duration_seconds=perf_counter() - start,
    )
