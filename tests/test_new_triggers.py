from src.triggers.triggers import AnomalyTrigger, ThresholdTrigger


def test_anomaly_trigger_fires_on_high_anomaly_score():
    trigger = AnomalyTrigger(threshold=3.0)
    assert trigger.should_trigger([{"vlan_id": 20, "anomaly_score": 3.5, "icmp_per_sec": 0, "active_src_ips": 5}])


def test_anomaly_trigger_fires_on_high_icmp():
    trigger = AnomalyTrigger(threshold=3.0)
    assert trigger.should_trigger([{"vlan_id": 20, "anomaly_score": 0.5, "icmp_per_sec": 2000, "active_src_ips": 5}])


def test_anomaly_trigger_fires_on_active_sources_spike():
    trigger = AnomalyTrigger(threshold=3.0)
    trigger.should_trigger([{"vlan_id": 20, "anomaly_score": 0.1, "icmp_per_sec": 10, "active_src_ips": 2}])
    assert trigger.should_trigger([{"vlan_id": 20, "anomaly_score": 0.1, "icmp_per_sec": 10, "active_src_ips": 20}])


def test_threshold_trigger_fires_on_high_inter_vlan_ratio():
    trigger = ThresholdTrigger(change_percent=0.2)
    assert trigger.should_trigger([{"utilization": 0.1, "inter_vlan_ratio": 0.7, "max_flow_bytes": 1000}])


def test_threshold_trigger_fires_on_high_max_flow_bytes():
    trigger = ThresholdTrigger(change_percent=0.2)
    assert trigger.should_trigger([{"utilization": 0.2, "inter_vlan_ratio": 0.1, "max_flow_bytes": 120000000}])


def test_no_triggers_fire_on_normal_metrics():
    a_trigger = AnomalyTrigger(threshold=3.0)
    t_trigger = ThresholdTrigger(change_percent=0.2)
    metrics = [{
        "vlan_id": 20,
        "anomaly_score": 0.5,
        "icmp_per_sec": 10,
        "active_src_ips": 5,
        "utilization": 0.3,
        "inter_vlan_ratio": 0.1,
        "max_flow_bytes": 5000000,
    }]
    assert not a_trigger.should_trigger(metrics)
    assert not t_trigger.should_trigger(metrics)
