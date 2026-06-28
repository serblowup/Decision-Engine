from __future__ import annotations

import logging
import math
from collections import defaultdict, deque
from datetime import datetime, timezone
from statistics import fmean, pstdev
from typing import Any

import aiohttp

from src.config import settings

logger = logging.getLogger(__name__)


class VictoriaMetricsClient:
    def __init__(self, base_url: str | None = None, session: aiohttp.ClientSession | None = None) -> None:
        self.base_url = (base_url or settings.victoriametrics_url).rstrip("/")
        self._session = session or aiohttp.ClientSession()
        # Sliding window over previous flow rates per VLAN; size matches the
        # cold-start threshold so the deque becomes "warm" exactly when z-score
        # is allowed to fire (see _z_score).
        self._flow_history: dict[int, deque[float]] = defaultdict(
            lambda: deque(maxlen=max(settings.anomaly_min_window_size, 1))
        )

    async def __aenter__(self) -> "VictoriaMetricsClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    async def close(self) -> None:
        await self._session.close()

    async def query(self, promql: str) -> list[dict[str, Any]]:
        url = f"{self.base_url}/prometheus/api/v1/query"
        try:
            async with self._session.get(url, params={"query": promql}) as response:
                response.raise_for_status()
                payload = await response.json()
                return payload.get("data", {}).get("result", [])
        except Exception as exc:
            logger.exception("VictoriaMetrics query failed: %s", exc)
            return []

    async def query_range(
        self,
        promql: str,
        start: int,
        end: int,
        step: str,
    ) -> list[dict[str, Any]]:
        url = f"{self.base_url}/prometheus/api/v1/query_range"
        try:
            async with self._session.get(
                url,
                params={"query": promql, "start": start, "end": end, "step": step},
            ) as response:
                response.raise_for_status()
                payload = await response.json()
                return payload.get("data", {}).get("result", [])
        except Exception as exc:
            logger.exception("VictoriaMetrics query_range failed: %s", exc)
            return []

    async def get_latest_metrics(self) -> list[dict[str, Any]]:
        metrics, raw_results = await self._resolve_level1_vlan_id()
        if metrics:
            return metrics

        logger.debug(
            "VictoriaMetrics query returned %s series, all vlan_ids: %s",
            len(raw_results),
            [r.get("metric", {}).get("vlan_id") for r in raw_results],
        )
        logger.warning(
            "No metrics with vlan_id tag found. Ensure NetFlow v9 is configured on network devices and vlan_id is exported."
        )
        return []

    async def _resolve_level1_vlan_id(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Aggregate latest NetFlow rates per VLAN from VictoriaMetrics.

        Note on ``flows_per_sec`` / ``anomaly_score`` semantics: real exporters
        tag ``netflow_flow_count`` series with ``direction="1"`` (egress) and
        ``vlan_dst="<destination VLAN>"``, leaving ``vlan_id``/``vlan_src`` as
        ``"0"``. The flows query therefore keys by ``vlan_dst`` (the destination
        VLAN) so it lands on the same per-VLAN rows produced by the byte/inter
        queries; consequently ``anomaly_score`` is computed per *destination*
        VLAN. The byte queries are unchanged.

        Note on the flows aggregation: ``netflow_flow_count`` is exported as
        per-flow series (each src/dst/port tuple is a distinct time series with
        a single sample). ``rate(...[5m])`` and ``count_over_time(...[1m])``
        therefore return empty on this exporter, while the instant
        ``count by (vlan_dst) (netflow_flow_count{...})`` returns the active
        flow cardinality per destination VLAN in VM's staleness window. The
        z-score over history is scale-invariant, so this signal is a valid
        anomaly input.
        """
        total_bytes_q = 'sum by (vlan_id) (rate(netflow_bytes{vlan_id!="0", direction="0"}[5m]))'
        inter_q = 'sum by (vlan_id, vlan_dst) (rate(netflow_bytes{vlan_id!="0", vlan_dst!="0", direction="0"}[5m]))'
        # netflow_flow_count is per-flow (one sample per series), so windowed
        # aggregations return empty on this stand. Use the instant count of
        # active series per destination VLAN; z-score detects relative bursts.
        flows_q = 'count by (vlan_dst) (netflow_flow_count{vlan_dst!="0", direction="1"})'
        packets_q = 'sum by (vlan_id) (rate(netflow_packets{vlan_id!="0", direction="0"}[5m]))'

        total_bytes_result = await self.query(total_bytes_q)
        if not total_bytes_result:
            fallback_query = 'sum by (vlan_id) (rate(netflow_bytes{vlan_id!="0"}[5m]))'
            total_bytes_result = await self.query(fallback_query)
            if total_bytes_result:
                logger.info("Using fallback query without direction filter")
            else:
                return [], []

        inter_result = await self.query(inter_q)
        flows_result = await self.query(flows_q)
        packets_result = await self.query(packets_q)

        per_vlan = self._init_rows_from_tag(total_bytes_result, "vlan_id")
        if not per_vlan:
            return [], total_bytes_result

        # Keep flows keyed by vlan_dst to match the egress label scheme of the
        # exporter; packets remain on vlan_id (unchanged byte-side semantics).
        self._fill_metric(per_vlan, flows_result, "vlan_dst", "flows_per_sec")
        self._fill_metric(per_vlan, packets_result, "vlan_id", "packets_per_sec")

        for item in inter_result:
            metric = item.get("metric", {})
            src = metric.get("vlan_id")
            dst = metric.get("vlan_dst")
            if src in (None, "0") or dst in (None, "0"):
                continue
            if src == dst:
                continue
            try:
                vlan_id = int(src)
            except (TypeError, ValueError):
                continue
            if vlan_id not in per_vlan:
                continue
            per_vlan[vlan_id]["inter_bytes"] += self._extract_value(item)

        for vlan_id, row in per_vlan.items():
            total = float(row.get("bytes_per_sec", 0.0))
            inter = float(row.get("inter_bytes", 0.0))
            row["intra_bytes"] = max(total - inter, 0.0)

        return self._finalize_rows(per_vlan), total_bytes_result

    def _empty_row(self, vlan_id: int) -> dict[str, Any]:
        return {
            "vlan_id": vlan_id,
            "bytes_per_sec": 0.0,
            "flows_per_sec": 0.0,
            "packets_per_sec": 0.0,
            "active_src_ips": 0,
            "icmp_per_sec": 0.0,
            "inter_bytes": 0.0,
            "intra_bytes": 0.0,
            "max_flow_bytes": 0.0,
        }

    def _init_rows_from_tag(self, results: list[dict[str, Any]], tag: str) -> dict[int, dict[str, Any]]:
        per_vlan: dict[int, dict[str, Any]] = {}
        for item in results:
            metric = item.get("metric", {})
            raw_vlan = metric.get(tag)
            if raw_vlan is None or str(raw_vlan) == "0":
                continue
            try:
                vlan_id = int(raw_vlan)
            except (TypeError, ValueError):
                continue
            row = self._empty_row(vlan_id)
            row["bytes_per_sec"] = self._extract_value(item)
            per_vlan[vlan_id] = row
        return per_vlan

    def _fill_metric(
        self,
        per_vlan: dict[int, dict[str, Any]],
        results: list[dict[str, Any]],
        tag: str,
        target: str,
    ) -> None:
        for item in results:
            metric = item.get("metric", {})
            raw_vlan = metric.get(tag)
            if raw_vlan is None or str(raw_vlan) == "0":
                continue
            try:
                vlan_id = int(raw_vlan)
            except (TypeError, ValueError):
                continue
            if vlan_id not in per_vlan:
                continue
            per_vlan[vlan_id][target] = self._extract_value(item)

    def _finalize_rows(self, per_vlan: dict[int, dict[str, Any]]) -> list[dict[str, Any]]:
        link_capacity_bps = settings.link_capacity_mbps * 1_000_000
        metrics: list[dict[str, Any]] = []

        for vlan_id, row in sorted(per_vlan.items()):
            inter = float(row.get("inter_bytes", 0.0))
            intra = float(row.get("intra_bytes", 0.0))
            total = inter + intra
            ratio = inter / total if total > 0 else 0.0
            flows = float(row.get("flows_per_sec", 0.0))
            history = self._flow_history[vlan_id]
            anomaly = self._z_score(list(history), flows)
            history.append(flows)

            bytes_per_sec = float(row.get("bytes_per_sec", 0.0))
            metrics.append(
                {
                    "vlan_id": vlan_id,
                    "bytes_per_sec": bytes_per_sec,
                    "flows_per_sec": flows,
                    "packets_per_sec": float(row.get("packets_per_sec", 0.0)),
                    "active_src_ips": int(row.get("active_src_ips", 0)),
                    "icmp_per_sec": float(row.get("icmp_per_sec", 0.0)),
                    "inter_vlan_ratio": ratio,
                    "max_flow_bytes": float(row.get("max_flow_bytes", 0.0)),
                    "utilization": (bytes_per_sec * 8.0 / link_capacity_bps) if link_capacity_bps > 0 else 0.0,
                    "anomaly_score": anomaly,
                    "window_end": datetime.now(timezone.utc).isoformat(),
                }
            )
        return metrics

    def _extract_value(self, item: dict[str, Any]) -> float:
        value = item.get("value")
        if isinstance(value, list) and len(value) > 1:
            try:
                return float(value[1])
            except (TypeError, ValueError):
                return 0.0
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    def _z_score(self, history: list[float], current: float) -> float:
        """Population z-score with three guards against false positives.

        Cold start: until the history contains at least
        ``settings.anomaly_min_window_size`` samples there is no statistically
        meaningful baseline, so we return ``0.0`` (neutral, no trigger).
        Low baseline mean: when the window's mean is below
        ``settings.anomaly_min_baseline_mean`` the metric is in a low-volume
        regime where a tiny absolute deviation produces a disproportionately
        large z-score that is not actionable; we also return ``0.0`` (covers
        the gap between the cold-start and low-std guards).
        Low variance: when the window is essentially constant
        (``pstdev < settings.anomaly_min_std_threshold``) any non-zero
        ``current`` would produce an arbitrarily large score from numerical
        noise; we also return ``0.0`` in that case.
        """
        if len(history) < settings.anomaly_min_window_size:
            logger.debug(
                "anomaly skipped: cold_start, window_size=%d (min=%d)",
                len(history),
                settings.anomaly_min_window_size,
            )
            return 0.0
        avg = fmean(history)
        if avg < settings.anomaly_min_baseline_mean:
            logger.debug(
                "anomaly skipped: low_baseline, mean=%.2f (min=%.2f)",
                avg,
                settings.anomaly_min_baseline_mean,
            )
            return 0.0
        std = pstdev(history)
        if math.isnan(std) or std < settings.anomaly_min_std_threshold:
            logger.debug("anomaly skipped: low_std=%s (min=%s)", std, settings.anomaly_min_std_threshold)
            return 0.0
        return (current - avg) / std
