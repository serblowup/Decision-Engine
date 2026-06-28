from __future__ import annotations

import time
from typing import Protocol

from src.config import settings


class Trigger(Protocol):
    def should_trigger(self, metrics: list[dict]) -> bool:
        ...


class ThresholdTrigger:
    def __init__(self, change_percent: float) -> None:
        self.change_percent = change_percent

    def should_trigger(self, metrics: list[dict]) -> bool:
        for metric in metrics:
            utilization = float(metric.get("utilization", 0.0))
            inter_ratio = float(metric.get("inter_vlan_ratio", 0.0))
            max_flow = float(metric.get("max_flow_bytes", 0.0))
            if utilization > settings.bandwidth_threshold:
                return True
            if inter_ratio > settings.inter_vlan_ratio_threshold:
                return True
            if max_flow > settings.max_flow_bytes_threshold:
                return True
        return False


class PeriodicTrigger:
    def __init__(self, interval_seconds: int) -> None:
        self.interval_seconds = interval_seconds
        self._last_triggered: float | None = None

    def should_trigger(self, metrics: list[dict]) -> bool:
        now = time.monotonic()
        if self._last_triggered is None:
            self._last_triggered = now
            return True

        if now - self._last_triggered >= self.interval_seconds:
            self._last_triggered = now
            return True
        return False


class AnomalyTrigger:
    def __init__(self, threshold: float) -> None:
        self.threshold = threshold
        self._prev_active_src: dict[int, int] = {}

    def should_trigger(self, metrics: list[dict]) -> bool:
        fired = False
        for metric in metrics:
            vlan_id = int(metric.get("vlan_id", 0))
            anomaly_score = float(metric.get("anomaly_score", 0.0))
            icmp_per_sec = float(metric.get("icmp_per_sec", 0.0))
            active_src_ips = int(metric.get("active_src_ips", 0))

            prev = self._prev_active_src.get(vlan_id)
            if anomaly_score > self.threshold:
                fired = True
            if icmp_per_sec > settings.icmp_threshold:
                fired = True
            if prev is not None and abs(active_src_ips - prev) > settings.new_sources_threshold:
                fired = True

        for metric in metrics:
            vlan_id = int(metric.get("vlan_id", 0))
            self._prev_active_src[vlan_id] = int(metric.get("active_src_ips", 0))

        return fired
