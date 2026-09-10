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
        # Параметры для SPLIT
        split_min_devices: int = 50,
        split_max_devices_per_vlan: int = 100,
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
        self.split_min_devices = split_min_devices
        self.split_max_devices_per_vlan = split_max_devices_per_vlan
        self._prev_active_src: dict[int, int] = {}

    def get_name(self) -> str:
        return "ThresholdHeuristic"

    def get_version(self) -> str:
        return "3.2.0"  # + IPAM support

    def decide(self, ctx: NetworkContext) -> StrategyDecision:
        decision_by_vlan: dict[int, VlanDecision] = {}
        protected = set(getattr(ctx.state, "protected_vlans", set()) or set())

        # Сбор статистики по VLAN
        vlan_stats: dict[int, dict] = self._collect_vlan_stats(ctx)

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

            # --- ISOLATE: аномалии и ICMP ---
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

            # --- SPLIT: если VLAN слишком большой ---
            device_count = vlan_stats.get(vlan_id, {}).get("device_count", 0)
            if device_count > self.split_min_devices:
                # Находим свободные VLAN ID для дочерних сегментов
                child1, child2 = self._find_free_vlan_ids(ctx.state, vlan_id)
                if child1 is not None and child2 is not None:
                    # Проверяем, что IP-префикс можно разбить
                    prefix = vlan_stats.get(vlan_id, {}).get("ip_prefix")
                    if prefix and self._can_split_prefix(prefix):
                        decision_by_vlan[vlan_id] = VlanDecision(
                            vlan_id=vlan_id,
                            decision_type="SPLIT",
                            criticality=BatchCriticality.NORMAL,
                            child_vlan_ids=(child1, child2),
                        )
                        logger.info(
                            "DECISION vlan_id=%s decision_type=SPLIT devices=%d child_vlans=(%d,%d)",
                            vlan_id,
                            device_count,
                            child1,
                            child2,
                        )
                        continue

            # --- MERGE: high inter-vlan ratio ---
            if inter_vlan_ratio > settings.inter_vlan_ratio_threshold:
                if vlan_id in protected:
                    logger.warning("merge skipped: source vlan_id=%s is protected", vlan_id)
                elif inter_vlan_bytes_per_sec < self.merge_min_inter_vlan_bytes_per_sec:
                    logger.info(
                        "MERGE_SUPPRESSED vlan_id=%s inter_vlan_ratio=%.4f "
                        "inter_vlan_bytes_per_sec=%.1f floor=%.1f",
                        vlan_id,
                        inter_vlan_ratio,
                        inter_vlan_bytes_per_sec,
                        self.merge_min_inter_vlan_bytes_per_sec,
                    )
                else:
                    # Находим целевой VLAN для слияния
                    target_vlan = self._find_merge_target(ctx.state, vlan_id)
                    if target_vlan is not None:
                        decision_by_vlan[vlan_id] = VlanDecision(
                            vlan_id=vlan_id,
                            decision_type="MERGE",
                            criticality=BatchCriticality.NORMAL,
                            target_vlan_id=target_vlan,
                        )
                        logger.info(
                            "DECISION vlan_id=%s decision_type=MERGE target_vlan=%s triggered_by=%s",
                            vlan_id,
                            target_vlan,
                            triggered_reasons,
                        )
                        continue

            # --- REBALANCE: высокая утилизация ---
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

            # --- ISOLATE: большой поток ---
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

        # Обновляем историю
        for metric in ctx.metrics:
            if metric.get("vlan_id") is None:
                continue
            vlan_id = int(metric["vlan_id"])
            self._prev_active_src[vlan_id] = int(metric.get("active_src_ips", 0))

        decisions = list(decision_by_vlan.values())
        if not decisions:
            return StrategyDecision(decisions=[])

        # Запускаем сегментатор для REBALANCE/ISOLATE (не для SPLIT/MERGE)
        has_structural_decision = any(
            d.decision_type in {"REBALANCE", "ISOLATE"} for d in decisions
        )
        segmentation_result = None
        if has_structural_decision:
            current_segmentation = self._build_current_segmentation(ctx)
            model = NetworkModel(ctx.state, ctx.metrics)
            segmentation_result = self.segmenter.optimize(
                model=model,
                constraints=self.constraints,
                current=current_segmentation,
            )

        return StrategyDecision(decisions=decisions, segmentation_result=segmentation_result)

    def _collect_vlan_stats(self, ctx: NetworkContext) -> dict[int, dict]:
        """Сбор статистики по VLAN: количество устройств, IP-префиксы."""
        stats: dict[int, dict] = defaultdict(lambda: {"device_count": 0, "ip_prefix": None})

        for assignment in ctx.state.assignments:
            stats[assignment.vlan_id]["device_count"] += 1

        # Получаем IP-префиксы из VLAN
        for vlan in ctx.state.vlans:
            if hasattr(vlan, "ip_prefix") and vlan.ip_prefix:
                stats[vlan.vlan_id]["ip_prefix"] = vlan.ip_prefix

        return dict(stats)

    def _find_free_vlan_ids(self, state: NetworkState, source_vlan_id: int) -> tuple[int | None, int | None]:
        """Найти два свободных VLAN ID для SPLIT."""
        existing_vlans = {v.vlan_id for v in state.vlans}
        used_vlans = existing_vlans | {source_vlan_id}

        # Ищем два свободных VLAN начиная с 100
        candidates = []
        for vlan_id in range(100, 4000):
            if vlan_id not in used_vlans:
                candidates.append(vlan_id)
                if len(candidates) == 2:
                    break

        if len(candidates) < 2:
            return None, None
        return candidates[0], candidates[1]

    def _can_split_prefix(self, prefix: str) -> bool:
        """Проверить, можно ли разбить префикс (маска <= 30)."""
        try:
            import ipaddress
            network = ipaddress.IPv4Network(prefix, strict=False)
            return network.prefixlen <= 30
        except Exception:
            return False

    def _find_merge_target(self, state: NetworkState, source_vlan_id: int) -> int | None:
        """Найти целевой VLAN для MERGE (с наибольшим числом портов)."""
        vlan_port_counts: dict[int, int] = defaultdict(int)
        for assignment in state.assignments:
            if assignment.vlan_id != source_vlan_id:
                vlan_port_counts[assignment.vlan_id] += 1

        if not vlan_port_counts:
            return None

        # Выбираем VLAN с наибольшим числом портов
        return max(vlan_port_counts, key=lambda v: vlan_port_counts[v])

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