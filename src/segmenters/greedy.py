from __future__ import annotations

import itertools
from time import perf_counter

from src.network.model import NetworkModel
from src.segmenters.interface import (
    ConstraintSet,
    Segmentation,
    SegmentationResult,
    build_passthrough_result,
)


class GreedySegmenter:
    """Greedy Merge/Split optimizer for VLAN segmentation.

    This algorithm follows local optimization principles from Kernighan-Lin style
    graph partitioning heuristics:

    - Kernighan B.W., Lin S. "An Efficient Heuristic Procedure for Partitioning
      Graphs", Bell System Technical Journal, 1970.

    We optimize the segmentation objective through two local operators:
    merge of two VLAN segments and split of one VLAN segment. For segments
    S_i and S_j, and traffic weight operator W(., .):

    - delta_merge = 2 * W(S_i, S_j) - lambda * (W(S_i, S_i) + W(S_j, S_j))
    - delta_split = lambda * W(A, B) - W(A, A) - W(B, B)

    where S_i is split into A and B.

    Complexity is O(n^2 log n) (n = number of devices) for practical runs with
    bounded iterations and sorted candidate processing.

    The method guarantees convergence to a local optimum (not global optimum):
    each accepted move strictly improves the objective by at least
    improvement_threshold, and the process stops after finite iterations when no
    improving action remains.
    """

    def __init__(
        self,
        max_iterations: int = 100,
        improvement_threshold: float = 0.001,
        lambda_coeff: float = 0.5,
    ) -> None:
        self.max_iterations = max_iterations
        self.improvement_threshold = improvement_threshold
        self.lambda_coeff = lambda_coeff

    def get_name(self) -> str:
        return "greedy"

    def get_complexity(self) -> str:
        return "O(n^2 log n)"

    def optimize(
        self,
        model: NetworkModel,
        constraints: ConstraintSet,
        current: Segmentation,
    ) -> SegmentationResult:
        start = perf_counter()
        if len(current) <= 1 or len(set(current.values())) <= 1 or model.graph.number_of_edges() == 0:
            return build_passthrough_result(self.get_name(), model, current)

        best = dict(current)
        current_value = model.evaluate(best)
        iterations = 0

        while iterations < self.max_iterations:
            iterations += 1
            best_candidate_seg: Segmentation | None = None
            best_candidate_delta = float("-inf")

            groups = self._groups(best)

            for vlan_a, vlan_b in itertools.combinations(groups.keys(), 2):
                delta = self._delta_merge(model, groups[vlan_a], groups[vlan_b])
                if delta <= self.improvement_threshold or delta <= best_candidate_delta:
                    continue
                candidate = self._merge(best, vlan_a, vlan_b)
                if constraints.validate(candidate):
                    continue
                eval_delta = model.evaluate(candidate) - current_value
                if eval_delta > best_candidate_delta and eval_delta > self.improvement_threshold:
                    best_candidate_delta = eval_delta
                    best_candidate_seg = candidate

            for vlan_id, devices in groups.items():
                if len(devices) < 2:
                    continue
                part_a, part_b = self._split_partition(model, devices)
                if not part_a or not part_b:
                    continue
                delta = self._delta_split(model, part_a, part_b)
                if delta <= self.improvement_threshold or delta <= best_candidate_delta:
                    continue
                candidate = self._split(best, vlan_id, part_a, part_b)
                if constraints.validate(candidate):
                    continue
                eval_delta = model.evaluate(candidate) - current_value
                if eval_delta > best_candidate_delta and eval_delta > self.improvement_threshold:
                    best_candidate_delta = eval_delta
                    best_candidate_seg = candidate

            if best_candidate_seg is None:
                break

            best = best_candidate_seg
            current_value = model.evaluate(best)

        return SegmentationResult(
            segmentation=best,
            objective_value=current_value,
            iterations=iterations,
            algorithm_name=self.get_name(),
            duration_seconds=perf_counter() - start,
        )

    def _groups(self, segmentation: Segmentation) -> dict[int, list[int]]:
        grouped: dict[int, list[int]] = {}
        for device_id, vlan_id in segmentation.items():
            grouped.setdefault(vlan_id, []).append(device_id)
        return grouped

    def _merge(self, segmentation: Segmentation, target_vlan: int, source_vlan: int) -> Segmentation:
        candidate = dict(segmentation)
        for device_id, vlan_id in segmentation.items():
            if vlan_id == source_vlan:
                candidate[device_id] = target_vlan
        return candidate

    def _split(
        self,
        segmentation: Segmentation,
        vlan_id: int,
        part_a: list[int],
        part_b: list[int],
    ) -> Segmentation:
        candidate = dict(segmentation)
        new_vlan = max(segmentation.values()) + 1
        for device_id in part_a:
            candidate[device_id] = vlan_id
        for device_id in part_b:
            candidate[device_id] = new_vlan
        return candidate

    def _traffic(self, model: NetworkModel, left: set[int], right: set[int]) -> float:
        total = 0.0
        for src, dst, attrs in model.graph.edges(data=True):
            if src in left and dst in right:
                total += float(attrs.get("weight", 0.0))
            elif src in right and dst in left:
                total += float(attrs.get("weight", 0.0))
        return total

    def _intra(self, model: NetworkModel, nodes: set[int]) -> float:
        total = 0.0
        for src, dst, attrs in model.graph.edges(data=True):
            if src in nodes and dst in nodes:
                total += float(attrs.get("weight", 0.0))
        return total

    def _delta_merge(self, model: NetworkModel, group_a: list[int], group_b: list[int]) -> float:
        set_a = set(group_a)
        set_b = set(group_b)
        cross = self._traffic(model, set_a, set_b)
        intra_a = self._intra(model, set_a)
        intra_b = self._intra(model, set_b)
        return 2.0 * cross - self.lambda_coeff * (intra_a + intra_b)

    def _delta_split(self, model: NetworkModel, part_a: list[int], part_b: list[int]) -> float:
        set_a = set(part_a)
        set_b = set(part_b)
        cross = self._traffic(model, set_a, set_b)
        intra_a = self._intra(model, set_a)
        intra_b = self._intra(model, set_b)
        return self.lambda_coeff * cross - intra_a - intra_b

    def _split_partition(self, model: NetworkModel, devices: list[int]) -> tuple[list[int], list[int]]:
        scores: list[tuple[int, float]] = []
        node_set = set(devices)
        for device_id in devices:
            intra_weight = 0.0
            for neighbor, attrs in model.graph[device_id].items():
                if neighbor in node_set:
                    intra_weight += float(attrs.get("weight", 0.0))
            scores.append((device_id, intra_weight))

        scores.sort(key=lambda item: item[1], reverse=True)
        part_a: list[int] = []
        part_b: list[int] = []
        for idx, (device_id, _) in enumerate(scores):
            if idx % 2 == 0:
                part_a.append(device_id)
            else:
                part_b.append(device_id)
        return part_a, part_b

