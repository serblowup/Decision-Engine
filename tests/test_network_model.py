from src.network.model import NetworkModel


def test_graph_builds_with_correct_node_count(mock_network_state):
    d1 = mock_network_state.devices[0].id
    d2 = mock_network_state.devices[1].id
    metrics = [
        {"src_device_id": d1, "dst_device_id": d2, "bytes_total": 1000, "vlan_id": 20},
    ]
    model = NetworkModel(mock_network_state, metrics)

    assert model.graph.number_of_nodes() == 2


def test_get_intra_segment_traffic_returns_correct_sum(mock_network_state):
    d1 = mock_network_state.devices[0].id
    d2 = mock_network_state.devices[1].id
    metrics = [
        {"src_device_id": d1, "dst_device_id": d2, "bytes_total": 1200, "vlan_id": 20},
        {"src_device_id": d1, "dst_device_id": d2, "bytes_total": 300, "vlan_id": 10},
    ]
    model = NetworkModel(mock_network_state, metrics)

    assert model.get_intra_segment_traffic(20) == 1500


def test_evaluate_prefers_better_segmentation(mock_network_state):
    d1 = mock_network_state.devices[0].id
    d2 = mock_network_state.devices[1].id
    metrics = [
        {"src_device_id": d1, "dst_device_id": d2, "bytes_total": 2000, "vlan_id": 20},
    ]
    model = NetworkModel(mock_network_state, metrics)

    better = model.evaluate({d1: 20, d2: 20})
    worse = model.evaluate({d1: 10, d2: 30})

    assert better > worse
