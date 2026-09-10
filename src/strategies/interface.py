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
    topology: "Topology | None" = None


@dataclass(slots=True)
class VlanDecision:
    vlan_id: int
    decision_type: str  # "ISOLATE", "REBALANCE", "MERGE", "SPLIT", "NO_ACTION"
    criticality: BatchCriticality
    # Для MERGE: целевой VLAN (куда сливаем)
    target_vlan_id: int | None = None
    # Для SPLIT: новые VLAN ID (на которые разбиваем)
    child_vlan_ids: tuple[int, int] | None = None


@dataclass(slots=True)
class StrategyDecision:
    decisions: list[VlanDecision] = field(default_factory=list)
    segmentation_result: SegmentationResult | None = None
    batches: list[Batch] = field(default_factory=list)


class IDecisionStrategy(Protocol):
    def decide(self, ctx: NetworkContext) -> StrategyDecision:
        ...

    def get_name(self) -> str:
        ...

    def get_version(self) -> str:
        ...