from __future__ import annotations

from collections import defaultdict
from typing import Any, Hashable

import networkx as nx

from src.models.network import NetworkState


class NetworkModel:
    def __init__(self, state: NetworkState, metrics: list[dict[str, Any]]) -> None:
        self.state = state
        self.metrics = metrics
        self.graph = nx.Graph()
        self._device_to_vlans = self._build_device_vlan_index()
        self._build_graph()

    def _build_device_vlan_index(self) -> dict[Hashable, set[int]]:
        mapping: dict[Hashable, set[int]] = defaultdict(set)
        for assignment in self.state.assignments:
            mapping[assignment.device_id].add(assignment.vlan_id)
        return mapping

    def _extract_metric_endpoints(
        self,
        metric: dict[str, Any],
    ) -> tuple[Hashable | None, Hashable | None]:
        src = metric.get("src_device_id", metric.get("source_device_id", metric.get("device_a")))
        dst = metric.get("dst_device_id", metric.get("destination_device_id", metric.get("device_b")))
        if src is None or dst is None:
            return None, None
        return src, dst

    def _build_graph(self) -> None:
        for device in self.state.devices:
            self.graph.add_node(device.id)

        for metric in self.metrics:
            src, dst = self._extract_metric_endpoints(metric)
            if src is None or dst is None:
                continue
            weight = float(metric.get("bytes_total", 0.0))
            vlan_id = metric.get("vlan_id")
            if self.graph.has_edge(src, dst):
                self.graph[src][dst]["weight"] += weight
            else:
                self.graph.add_edge(src, dst, weight=weight, vlan_id=vlan_id)

    def get_intra_segment_traffic(self, vlan_id: int) -> float:
        total = 0.0
        for src, dst, attrs in self.graph.edges(data=True):
            src_in = vlan_id in self._device_to_vlans.get(src, set())
            dst_in = vlan_id in self._device_to_vlans.get(dst, set())
            if src_in and dst_in:
                total += float(attrs.get("weight", 0.0))
        return total

    def get_inter_segment_traffic(self, vlan_a: int, vlan_b: int) -> float:
        total = 0.0
        for src, dst, attrs in self.graph.edges(data=True):
            src_vlans = self._device_to_vlans.get(src, set())
            dst_vlans = self._device_to_vlans.get(dst, set())
            is_a_to_b = vlan_a in src_vlans and vlan_b in dst_vlans
            is_b_to_a = vlan_b in src_vlans and vlan_a in dst_vlans
            if is_a_to_b or is_b_to_a:
                total += float(attrs.get("weight", 0.0))
        return total

    def get_segment_size(self, vlan_id: int) -> int:
        return len({a.device_id for a in self.state.assignments if a.vlan_id == vlan_id})

    def evaluate(self, segmentation: dict[Hashable, int]) -> float:
        intra_traffic = 0.0
        inter_traffic = 0.0

        for src, dst, attrs in self.graph.edges(data=True):
            src_vlan = segmentation.get(src)
            dst_vlan = segmentation.get(dst)
            weight = float(attrs.get("weight", 0.0))
            if src_vlan is None or dst_vlan is None:
                continue
            if src_vlan == dst_vlan:
                intra_traffic += weight
            else:
                inter_traffic += weight

        return intra_traffic - 0.5 * inter_traffic

    def get_anomalous_vlans(self, metrics: list[dict[str, Any]], anomaly_threshold: float) -> list[int]:
        anomalous: set[int] = set()
        for metric in metrics:
            score = float(metric.get("anomaly_score", 0.0))
            vlan_id = metric.get("vlan_id")
            if vlan_id is None:
                continue
            if score > anomaly_threshold:
                anomalous.add(int(vlan_id))
        return sorted(anomalous)
