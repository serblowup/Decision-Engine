from __future__ import annotations

import pytest

from src.victoriametrics.client import VictoriaMetricsClient


class DummyVM(VictoriaMetricsClient):
    def __init__(self):
        super().__init__(base_url="http://vm", session=None)
        self.responses: dict[str, list[dict]] = {}

    async def query(self, promql: str):
        return self.responses.get(promql, [])


@pytest.mark.asyncio
async def test_level1_vlan_id_tag():
    client = DummyVM()
    client.responses = {
        'sum by (vlan_id) (rate(netflow_bytes{vlan_id!="0", direction="0"}[5m]))': [
            {"metric": {"vlan_id": "20"}, "value": [1, "100"]}
        ],
        'sum by (vlan_id, vlan_dst) (rate(netflow_bytes{vlan_id!="0", vlan_dst!="0", direction="0"}[5m]))': [
            {"metric": {"vlan_id": "20", "vlan_dst": "10"}, "value": [1, "40"]}
        ],
        'sum by (vlan_id) (rate(netflow_flow_count{vlan_id!="0", direction="0"}[5m]))': [
            {"metric": {"vlan_id": "20"}, "value": [1, "10"]}
        ],
        'sum by (vlan_id) (rate(netflow_packets{vlan_id!="0", direction="0"}[5m]))': [
            {"metric": {"vlan_id": "20"}, "value": [1, "30"]}
        ],
    }

    result = await client.get_latest_metrics()
    assert result and result[0]["vlan_id"] == 20


@pytest.mark.asyncio
async def test_level2_empty_result_returns_empty():
    client = DummyVM()
    client.responses = {
        'sum by (vlan_id) (rate(netflow_bytes{vlan_id!="0", direction="0"}[5m]))': []
    }

    result = await client.get_latest_metrics()
    assert result == []
