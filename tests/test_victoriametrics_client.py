from __future__ import annotations

from collections import deque

import pytest

from src.victoriametrics.client import VictoriaMetricsClient


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    def raise_for_status(self):
        return None

    async def json(self):
        return self._payload


class FakeFailResponse:
    async def __aenter__(self):
        raise RuntimeError("vm down")

    async def __aexit__(self, exc_type, exc, tb):
        return None


class FakeSession:
    def __init__(self, payloads=None, fail=False):
        self.payloads = list(payloads or [])
        self.fail = fail
        self.calls: list[tuple[str, dict | None]] = []

    def get(self, url, params=None):
        self.calls.append((url, params))
        if self.fail:
            return FakeFailResponse()
        if self.payloads:
            return FakeResponse(self.payloads.pop(0))
        return FakeResponse({"data": {"result": []}})

    async def close(self):
        return None


@pytest.mark.asyncio
async def test_get_latest_metrics_parses_vm_result():
    fake_session = FakeSession()
    client = VictoriaMetricsClient("http://vm:8428", session=fake_session)  # type: ignore[arg-type]
    payloads = [
        {"data": {"result": [{"metric": {"vlan_id": "10"}, "value": [1710, "1250000"]}]}},
        {"data": {"result": [{"metric": {"vlan_id": "10", "vlan_dst": "20"}, "value": [1710, "150000"]}]}},
        # flows: real egress-only series keyed by vlan_dst (vlan_id/vlan_src="0").
        {"data": {"result": [{"metric": {"vlan_dst": "10"}, "value": [1710, "350"]}]}},
        {"data": {"result": [{"metric": {"vlan_id": "10"}, "value": [1710, "900"]}]}},
    ]
    fake_session.payloads = payloads

    result = await client.get_latest_metrics()

    assert len(result) == 1
    row = result[0]
    assert row["vlan_id"] == 10
    assert row["bytes_per_sec"] == 1250000.0
    assert row["flows_per_sec"] == 350.0
    assert row["packets_per_sec"] == 900.0
    assert row["inter_vlan_ratio"] == pytest.approx(150000 / 1250000)

    assert fake_session.calls
    assert fake_session.calls[0][0].endswith("/prometheus/api/v1/query")


@pytest.mark.asyncio
async def test_query_returns_empty_on_error():
    client = VictoriaMetricsClient("http://vm:8428", session=FakeSession(fail=True))  # type: ignore[arg-type]

    result = await client.query("sum(up)")

    assert result == []


@pytest.mark.asyncio
async def test_anomaly_score_zero_when_history_short():
    fake_session = FakeSession()
    client = VictoriaMetricsClient("http://vm:8428", session=fake_session)  # type: ignore[arg-type]
    client._flow_history[10] = deque([100.0, 120.0], maxlen=10)
    fake_session.payloads = [
        {"data": {"result": [{"metric": {"vlan_id": "10"}, "value": [1710, "1000"]}]}},
        {"data": {"result": []}},
        # flows: real egress-only series keyed by vlan_dst.
        {"data": {"result": [{"metric": {"vlan_dst": "10"}, "value": [1710, "300"]}]}},
        {"data": {"result": [{"metric": {"vlan_id": "10"}, "value": [1710, "200"]}]}},
    ]

    result = await client.get_latest_metrics()

    assert result[0]["anomaly_score"] == 0.0


@pytest.mark.asyncio
async def test_utilization_computation():
    fake_session = FakeSession()
    client = VictoriaMetricsClient("http://vm:8428", session=fake_session)  # type: ignore[arg-type]
    fake_session.payloads = [
        {"data": {"result": [{"metric": {"vlan_id": "10"}, "value": [1710, "125000000"]}]}},
        {"data": {"result": []}},
        {"data": {"result": []}},
        {"data": {"result": []}},
    ]

    result = await client.get_latest_metrics()

    assert result[0]["utilization"] == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_vlan_id_zero_is_skipped():
    fake_session = FakeSession(
        payloads=[
            {"data": {"result": [{"metric": {"vlan_id": "0"}, "value": [1710, "1000"]}]}},
        ]
    )
    client = VictoriaMetricsClient("http://vm:8428", session=fake_session)  # type: ignore[arg-type]

    result = await client.get_latest_metrics()

    assert result == []


@pytest.mark.asyncio
async def test_query_range_and_context_manager_paths():
    fake_session = FakeSession(payloads=[{"data": {"result": []}}])
    client = VictoriaMetricsClient("http://vm:8428", session=fake_session)  # type: ignore[arg-type]
    async with client as c:
        result = await c.query_range("up", 1, 2, "60s")
    assert result == []
    assert fake_session.calls[0][0].endswith("/prometheus/api/v1/query_range")


@pytest.mark.asyncio
async def test_query_range_returns_empty_on_error():
    client = VictoriaMetricsClient("http://vm:8428", session=FakeSession(fail=True))  # type: ignore[arg-type]
    result = await client.query_range("up", 1, 2, "60s")
    assert result == []


@pytest.mark.asyncio
async def test_inter_segment_skips_missing_vlan_dst_and_equal_vlan():
    fake_session = FakeSession(
        payloads=[
            {"data": {"result": [{"metric": {"vlan_id": "10"}, "value": [1710, "1000"]}]}},
            {"data": {"result": [
                {"metric": {"vlan_id": "10"}, "value": [1710, "50"]},
                {"metric": {"vlan_id": "10", "vlan_dst": "10"}, "value": [1710, "70"]},
            ]}},
            {"data": {"result": []}},
            {"data": {"result": []}},
        ]
    )
    client = VictoriaMetricsClient("http://vm:8428", session=fake_session)  # type: ignore[arg-type]
    result = await client.get_latest_metrics()
    assert result and result[0]["inter_vlan_ratio"] == 0.0


def test_extract_value_and_zscore_error_paths():
    client = VictoriaMetricsClient("http://vm:8428", session=FakeSession())  # type: ignore[arg-type]
    assert client._extract_value({"value": [1, "bad"]}) == 0.0
    assert client._extract_value({"value": object()}) == 0.0
    assert client._z_score([1.0, 1.0, 1.0], 1.0) == 0.0


@pytest.mark.asyncio
async def test_flows_per_sec_matches_real_exporter_labels_and_drives_anomaly():
    """Real exporters emit netflow_flow_count with direction='1', vlan_id='0',
    vlan_src='0', vlan_dst='<dest>'. The flows query must key by vlan_dst so
    the value lands on the per-VLAN row and feeds anomaly_score (z-score over
    history). Once history has >=3 samples and the new value diverges,
    anomaly_score must be non-zero.
    """
    fake_session = FakeSession()
    client = VictoriaMetricsClient("http://vm:8428", session=fake_session)  # type: ignore[arg-type]
    # Prime the deque with >= ANOMALY_MIN_WINDOW_SIZE varied samples so the
    # cold-start / low-std guards in _z_score are cleared and this single
    # divergent sample yields a non-zero score.
    client._flow_history[20] = deque(
        [100.0, 120.0, 80.0, 110.0, 95.0, 130.0, 75.0, 105.0, 90.0, 115.0],
        maxlen=10,
    )
    fake_session.payloads = [
        # total_bytes: per_vlan keyed by vlan_id (byte side is untouched).
        {"data": {"result": [{"metric": {"vlan_id": "20"}, "value": [1710, "5000000"]}]}},
        # inter: empty (irrelevant to this test).
        {"data": {"result": []}},
        # flows: production-shaped labels — vlan_id/vlan_src="0", vlan_dst="20",
        # direction="1". With the fix the query selects this row and keys by
        # vlan_dst, so it lands on per_vlan[20].
        {
            "data": {
                "result": [
                    {
                        "metric": {
                            "direction": "1",
                            "vlan_id": "0",
                            "vlan_src": "0",
                            "vlan_dst": "20",
                        },
                        "value": [1710, "500"],
                    }
                ]
            }
        },
        # packets: empty.
        {"data": {"result": []}},
    ]

    result = await client.get_latest_metrics()

    assert len(result) == 1
    row = result[0]
    assert row["vlan_id"] == 20
    assert row["flows_per_sec"] == 500.0
    # 500 against history mean ~100 with non-zero std must yield a non-zero score.
    assert row["anomaly_score"] != 0.0

    flows_queries = [
        params["query"]
        for (_url, params) in fake_session.calls
        if params and "netflow_flow_count" in params.get("query", "")
    ]
    assert flows_queries, "no netflow_flow_count query was issued"
    q = flows_queries[0]
    assert 'vlan_dst!="0"' in q
    assert 'direction="1"' in q
    # The exporter emits per-flow series with single samples, so rate(...) and
    # count_over_time(...) return empty. We aggregate the instant series count
    # per destination VLAN.
    assert "count by (vlan_dst)" in q
    assert "rate(" not in q
