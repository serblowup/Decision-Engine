from __future__ import annotations

import math
import random
from time import perf_counter

from src.network.model import NetworkModel
from src.segmenters.interface import (
    ConstraintSet,
    Segmentation,
    SegmentationResult,
    build_passthrough_result,
)


class SimulatedAnnealingSegmenter:
    """Simulated annealing optimizer for VLAN segmentation.

    The algorithm follows physical annealing analogy:

    - Kirkpatrick S., Gelatt C.D., Vecchi M.P. "Optimization by Simulated Annealing",
      Science, 1983.

    Theoretical convergence: with logarithmic cooling T(t)=C/log(1+t), the
    process converges to global optimum with probability 1
    (Geman S., Geman D., 1984). In practice we use geometric cooling:

        T(t) = T0 * alpha^t,  alpha in [0.85, 0.99]

    balancing runtime and solution quality.

    Acceptance probability for worsening move (delta_E > 0 in energy terms):

        P(delta_E) = exp(-delta_E / T)

    In objective-maximization form with delta_F = F(new)-F(current), worsening
    moves (delta_F <= 0) are accepted with probability exp(delta_F / T).

    High temperature encourages exploration; low temperature favors local
    exploitation around the best-found solution.
    """

    def __init__(
        self,
        initial_temperature: float = 100.0,
        cooling_rate: float = 0.95,
        min_temperature: float = 0.01,
        max_iterations: int = 1000,
        random_seed: int = 42,
    ) -> None:
        self.initial_temperature = initial_temperature
        self.cooling_rate = cooling_rate
        self.min_temperature = min_temperature
        self.max_iterations = max_iterations
        self.random_seed = random_seed

    def get_name(self) -> str:
        return "sa"

    def get_complexity(self) -> str:
        return "O(iterations * eval_cost)"

    def optimize(
        self,
        model: NetworkModel,
        constraints: ConstraintSet,
        current: Segmentation,
    ) -> SegmentationResult:
        start = perf_counter()
        if len(current) <= 1 or len(set(current.values())) <= 1 or model.graph.number_of_edges() == 0:
            return build_passthrough_result(self.get_name(), model, current)

        rng = random.Random(self.random_seed)
        current_seg = dict(current)
        current_value = model.evaluate(current_seg)
        best_seg = dict(current_seg)
        best_value = current_value

        temperature = self.initial_temperature
        iterations = 0

        devices = list(current_seg.keys())
        vlan_pool = sorted(set(current_seg.values()))
        if len(vlan_pool) < 2:
            return build_passthrough_result(self.get_name(), model, current)

        while temperature > self.min_temperature and iterations < self.max_iterations:
            iterations += 1
            candidate = dict(current_seg)

            device_id = rng.choice(devices)
            current_vlan = candidate[device_id]
            alternatives = [vlan for vlan in vlan_pool if vlan != current_vlan]
            if not alternatives:
                temperature *= self.cooling_rate
                continue

            candidate[device_id] = rng.choice(alternatives)
            if constraints.validate(candidate):
                temperature *= self.cooling_rate
                continue

            candidate_value = model.evaluate(candidate)
            delta = candidate_value - current_value

            accept = False
            if delta > 0:
                accept = True
            else:
                probability = math.exp(delta / max(temperature, 1e-9))
                if rng.random() < probability:
                    accept = True

            if accept:
                current_seg = candidate
                current_value = candidate_value
                if current_value > best_value:
                    best_value = current_value
                    best_seg = dict(current_seg)

            temperature *= self.cooling_rate

        return SegmentationResult(
            segmentation=best_seg,
            objective_value=best_value,
            iterations=iterations,
            algorithm_name=self.get_name(),
            duration_seconds=perf_counter() - start,
        )

