from __future__ import annotations

import logging
from collections import Counter, defaultdict

from src.config import settings
from src.models.operations import BatchCriticality
from src.network.model import NetworkModel
from src.segmenters.interface import ConstraintSet, Segmenter
from src.strategies.interface import NetworkContext, StrategyDecision, VlanDecision

logger = logging.getLogger(__name__)


class ThresholdHeuristic:
    def __init__(
        self,
        anomaly_threshold: float,
        bandwidth_threshold: float,
        segmenter: Segmenter,
        constraints: ConstraintSet | None = None,
        merge_min_inter_vlan_bytes_per_sec: float | None = None,
    ) -> None:
        self.anomaly_threshold = anomaly_threshold
        self.bandwidth_threshold = bandwidth_threshold
        self.segmenter = segmenter
        self.constraints = constraints or ConstraintSet()
        self.merge_min_inter_vlan_bytes_per_sec = (
            merge_min_inter_vlan_bytes_per_sec
            if merge_min_inter_vlan_bytes_per_sec is not None
            else settings.merge_min_inter_vlan_bytes_per_sec
        )
        self._prev_active_src: dict[int, int] = {}

    def get_name(self) -> str:
        return "ThresholdHeuristic"

    def get_version(self) -> str:
        return "3.1.0"

    def decide(self, ctx: NetworkContext) -> StrategyDecision:
        decision_by_vlan: dict[int, VlanDecision] = {}
        # Protected VLANs (vlan.is_protected, managed by Gateway) must never be the
        # source of a MERGE: that would generate a destructive DELETE_VLAN for them.
        protected = set(getattr(ctx.state, "protected_vlans", set()) or set())

        for metric in ctx.metrics:
            vlan_id_raw = metric.get("vlan_id")
            if vlan_id_raw is None:
                continue

            vlan_id = int(vlan_id_raw)
            anomaly_score = float(metric.get("anomaly_score", 0.0))
            utilization = float(metric.get("utilization", 0.0))
            icmp_per_sec = float(metric.get("icmp_per_sec", 0.0))
            active_src_ips = int(metric.get("active_src_ips", 0))
            inter_vlan_ratio = float(metric.get("inter_vlan_ratio", 0.0))
            max_flow_bytes = float(metric.get("max_flow_bytes", 0.0))
            bytes_per_sec = float(metric.get("bytes_per_sec", 0.0))
            inter_vlan_bytes_per_sec = bytes_per_sec * inter_vlan_ratio

            logger.info(
                "METRICS vlan_id=%s utilization=%.4f anomaly_score=%.4f "
                "icmp_per_sec=%.1f active_src_ips=%d inter_vlan_ratio=%.4f max_flow_bytes=%.0f",
                vlan_id,
                utilization,
                anomaly_score,
                icmp_per_sec,
                active_src_ips,
                inter_vlan_ratio,
                max_flow_bytes,
            )

            prev_active = self._prev_active_src.get(vlan_id)
            is_new_sources_spike = (
                prev_active is not None
                and abs(active_src_ips - prev_active) > settings.new_sources_threshold
            )
            new_sources_delta = abs(active_src_ips - prev_active) if prev_active is not None else 0

            triggered_reasons: list[str] = []
            if anomaly_score > self.anomaly_threshold:
                triggered_reasons.append("anomaly")
                logger.warning(
                    "TRIGGER vlan_id=%s reason=anomaly value=%.4f threshold=%.4f",
                    vlan_id,
                    anomaly_score,
                    self.anomaly_threshold,
                )
            if icmp_per_sec > settings.icmp_threshold:
                triggered_reasons.append("icmp")
                logger.warning(
                    "TRIGGER vlan_id=%s reason=icmp value=%.1f threshold=%.1f",
                    vlan_id,
                    icmp_per_sec,
                    settings.icmp_threshold,
                )
            if is_new_sources_spike:
                triggered_reasons.append("new_sources")
                logger.warning(
                    "TRIGGER vlan_id=%s reason=new_sources value=%d threshold=%d",
                    vlan_id,
                    new_sources_delta,
                    settings.new_sources_threshold,
                )
            if utilization > self.bandwidth_threshold:
                triggered_reasons.append("bandwidth")
                logger.warning(
                    "TRIGGER vlan_id=%s reason=bandwidth value=%.4f threshold=%.4f",
                    vlan_id,
                    utilization,
                    self.bandwidth_threshold,
                )
            if inter_vlan_ratio > settings.inter_vlan_ratio_threshold:
                triggered_reasons.append("inter_vlan_ratio")
                logger.warning(
                    "TRIGGER vlan_id=%s reason=inter_vlan_ratio value=%.4f threshold=%.4f",
                    vlan_id,
                    inter_vlan_ratio,
                    settings.inter_vlan_ratio_threshold,
                )
            if max_flow_bytes > settings.max_flow_bytes_threshold:
                triggered_reasons.append("max_flow_bytes")
                logger.warning(
                    "TRIGGER vlan_id=%s reason=max_flow_bytes value=%.0f threshold=%.0f",
                    vlan_id,
                    max_flow_bytes,
                    settings.max_flow_bytes_threshold,
                )

            if anomaly_score > self.anomaly_threshold or icmp_per_sec > settings.icmp_threshold:
                decision_by_vlan[vlan_id] = VlanDecision(
                    vlan_id=vlan_id,
                    decision_type="ISOLATE",
                    criticality=BatchCriticality.CRITICAL,
                )
                logger.info(
                    "DECISION vlan_id=%s decision_type=%s triggered_by=%s",
                    vlan_id,
                    "ISOLATE",
                    triggered_reasons,
                )
                continue

            if is_new_sources_spike:
                decision_by_vlan[vlan_id] = VlanDecision(
                    vlan_id=vlan_id,
                    decision_type="ISOLATE",
                    criticality=BatchCriticality.CRITICAL,
                )
                logger.info(
                    "DECISION vlan_id=%s decision_type=%s triggered_by=%s",
                    vlan_id,
                    "ISOLATE",
                    triggered_reasons,
                )
                continue

            if utilization > self.bandwidth_threshold and inter_vlan_ratio < 0.2:
                decision_by_vlan[vlan_id] = VlanDecision(
                    vlan_id=vlan_id,
                    decision_type="REBALANCE",
                    criticality=BatchCriticality.NORMAL,
                )
                logger.info(
                    "DECISION vlan_id=%s decision_type=%s triggered_by=%s",
                    vlan_id,
                    "REBALANCE",
                    triggered_reasons,
                )
                continue

            if inter_vlan_ratio > settings.inter_vlan_ratio_threshold:
                if vlan_id in protected:
                    logger.warning("merge skipped: source vlan_id=%s is protected", vlan_id)
                elif inter_vlan_bytes_per_sec < self.merge_min_inter_vlan_bytes_per_sec:
                    # Guard against the auto-apply MERGE loop: a high inter_vlan_ratio
                    # on a near-idle VLAN is noise. Only consolidate VLANs that carry
                    # a meaningful absolute amount of cross-segment traffic.
                    logger.info(
                        "MERGE_SUPPRESSED vlan_id=%s inter_vlan_ratio=%.4f "
                        "inter_vlan_bytes_per_sec=%.1f floor=%.1f",
                        vlan_id,
                        inter_vlan_ratio,
                        inter_vlan_bytes_per_sec,
                        self.merge_min_inter_vlan_bytes_per_sec,
                    )
                else:
                    decision_by_vlan[vlan_id] = VlanDecision(
                        vlan_id=vlan_id,
                        decision_type="MERGE",
                        criticality=BatchCriticality.NORMAL,
                    )
                    logger.info(
                        "DECISION vlan_id=%s decision_type=%s triggered_by=%s",
                        vlan_id,
                        "MERGE",
                        triggered_reasons,
                    )
                    continue

            if max_flow_bytes > settings.max_flow_bytes_threshold:
                decision_by_vlan[vlan_id] = VlanDecision(
                    vlan_id=vlan_id,
                    decision_type="ISOLATE",
                    criticality=BatchCriticality.NORMAL,
                )
                logger.info(
                    "DECISION vlan_id=%s decision_type=%s triggered_by=%s",
                    vlan_id,
                    "ISOLATE",
                    triggered_reasons,
                )
            else:
                logger.info(
                    "DECISION vlan_id=%s decision_type=%s triggered_by=%s",
                    vlan_id,
                    "NO_ACTION",
                    triggered_reasons,
                )

        for metric in ctx.metrics:
            if metric.get("vlan_id") is None:
                continue
            vlan_id = int(metric["vlan_id"])
            self._prev_active_src[vlan_id] = int(metric.get("active_src_ips", 0))

        decisions = list(decision_by_vlan.values())
        if not decisions:
            return StrategyDecision(decisions=[])

        current_segmentation = self._build_current_segmentation(ctx)
        model = NetworkModel(ctx.state, ctx.metrics)
        segmentation_result = self.segmenter.optimize(
            model=model,
            constraints=self.constraints,
            current=current_segmentation,
        )

        return StrategyDecision(decisions=decisions, segmentation_result=segmentation_result)

    def _build_current_segmentation(self, ctx: NetworkContext) -> dict[int, int]:
        device_vlan_votes: dict[int, list[int]] = defaultdict(list)
        for assignment in ctx.state.assignments:
            device_vlan_votes[assignment.device_id].append(assignment.vlan_id)

        segmentation: dict[int, int] = {}
        for device in ctx.state.devices:
            votes = device_vlan_votes.get(device.id)
            if votes:
                segmentation[device.id] = Counter(votes).most_common(1)[0][0]
                continue

            if ctx.state.vlans:
                segmentation[device.id] = ctx.state.vlans[0].vlan_id
        return segmentation
