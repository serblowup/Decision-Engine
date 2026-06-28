from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

from src.models.network import NetworkState
from src.models.operations import Batch, BatchCriticality
from src.segmenters.interface import SegmentationResult

if TYPE_CHECKING:
    from src.topology.graph import Topology


@dataclass(slots=True)
class NetworkContext:
    state: NetworkState
    metrics: list[dict]
    timestamp: str
    # The structural model of the network for this decision cycle. Owned by the
    # strategy layer; loaded once per cycle and shared by all strategies.
    topology: "Topology | None" = None


@dataclass(slots=True)
class VlanDecision:
    vlan_id: int
    decision_type: str
    criticality: BatchCriticality


@dataclass(slots=True)
class StrategyDecision:
    decisions: list[VlanDecision] = field(default_factory=list)
    segmentation_result: SegmentationResult | None = None
    # Strategies that produce structural (graph-derived) actions directly — e.g.
    # RuleBasedStrategy emitting SET_TRUNK — attach the ready batches here. When
    # set (and segmentation_result is None) TaskBuilder publishes them as-is.
    batches: list[Batch] = field(default_factory=list)


class IDecisionStrategy(Protocol):
    def decide(self, ctx: NetworkContext) -> StrategyDecision:
        ...

    def get_name(self) -> str:
        ...

    def get_version(self) -> str:
        ...
