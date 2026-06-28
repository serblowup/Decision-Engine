from __future__ import annotations

import argparse
import os
import time
import urllib.request


def post_prometheus_lines(base_url: str, lines: list[str]) -> None:
    payload = ("\n".join(lines) + "\n").encode("utf-8")
    request = urllib.request.Request(
        url=f"{base_url.rstrip('/')}/api/v1/import/prometheus",
        data=payload,
        method="POST",
        headers={"Content-Type": "text/plain; version=0.0.4"},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        response.read()


def build_tag_mode(iteration: int) -> list[str]:
    burst = iteration % 5 == 0
    return [
        f'netflow_in_bytes{{vlan_src="10"}} {15000000}',
        f'netflow_in_bytes{{vlan_src="20"}} {220000000 if burst else 45000000}',
        f'netflow_in_bytes{{vlan_src="30"}} {35000000 + iteration * 10000}',
        f'netflow_flows{{vlan_src="10"}} 900',
        f'netflow_flows{{vlan_src="20"}} {7000 if burst else 1800}',
        f'netflow_flows{{vlan_src="30"}} 1600',
        f'netflow_active_src_ips{{vlan_src="10"}} 8',
        f'netflow_active_src_ips{{vlan_src="20"}} 14',
        f'netflow_active_src_ips{{vlan_src="30"}} 11',
        f'netflow_icmp_packets_total{{vlan_src="10"}} 15',
        f'netflow_icmp_packets_total{{vlan_src="20"}} {1300 if burst else 250}',
        f'netflow_icmp_packets_total{{vlan_src="30"}} 35',
        'netflow_intra_vlan_bytes{vlan_src="10"} 17000000',
        'netflow_intra_vlan_bytes{vlan_src="20"} 21000000',
        'netflow_intra_vlan_bytes{vlan_src="30"} 25000000',
        'netflow_inter_vlan_bytes{vlan_src="10"} 1200000',
        'netflow_inter_vlan_bytes{vlan_src="20"} 1800000',
        'netflow_inter_vlan_bytes{vlan_src="30"} 900000',
        'netflow_max_flow_bytes{vlan_src="10",src_ip="10.0.10.15"} 1500000',
        f'netflow_max_flow_bytes{{vlan_src="20",src_ip="10.0.20.15"}} {140000000 if burst else 45000000}',
        'netflow_max_flow_bytes{vlan_src="30",src_ip="10.0.30.15"} 7000000',
    ]


def build_subnet_mode(iteration: int) -> list[str]:
    burst = iteration % 5 == 0
    return [
        'netflow_src{source="192.168.10.1",flow="1"} 167777295',  # 10.0.20.15
        'netflow_src{source="192.168.10.1",flow="2"} 167772425',  # 10.0.1.9
        f'netflow_in_bytes{{source="192.168.10.1",flow="1",netflow_src="167777295"}} {220000000 if burst else 45000000}',
        'netflow_in_bytes{source="192.168.10.1",flow="2",netflow_src="167772425"} 18000000',
        f'netflow_in_packets{{source="192.168.10.1",flow="1",netflow_src="167777295"}} {900000 if burst else 200000}',
        'netflow_in_packets{source="192.168.10.1",flow="2",netflow_src="167772425"} 70000',
        f'netflow_flows{{source="192.168.10.1",flow="1",netflow_src="167777295"}} {7000 if burst else 1800}',
        'netflow_flows{source="192.168.10.1",flow="2",netflow_src="167772425"} 800',
    ]


def build_snmp_mode(iteration: int) -> list[str]:
    burst = iteration % 5 == 0
    return [
        'netflow_in_snmp{source="192.168.10.1",flow="1"} 3',
        'netflow_in_snmp{source="192.168.10.1",flow="2"} 7',
        f'netflow_in_bytes{{source="192.168.10.1",flow="1",netflow_in_snmp="3"}} {220000000 if burst else 45000000}',
        'netflow_in_bytes{source="192.168.10.1",flow="2",netflow_in_snmp="7"} 17000000',
        f'netflow_in_packets{{source="192.168.10.1",flow="1",netflow_in_snmp="3"}} {850000 if burst else 220000}',
        'netflow_in_packets{source="192.168.10.1",flow="2",netflow_in_snmp="7"} 60000',
        f'netflow_flows{{source="192.168.10.1",flow="1",netflow_in_snmp="3"}} {7000 if burst else 1700}',
        'netflow_flows{source="192.168.10.1",flow="2",netflow_in_snmp="7"} 600',
    ]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["tag", "subnet", "snmp"], default="tag")
    args = parser.parse_args()

    base_url = os.getenv("VICTORIAMETRICS_URL", "http://localhost:8428")
    iteration = 0

    print(f"Writing mock metrics to {base_url} every 60 seconds (mode={args.mode})")
    try:
        while True:
            iteration += 1
            if args.mode == "tag":
                lines = build_tag_mode(iteration)
            elif args.mode == "subnet":
                lines = build_subnet_mode(iteration)
            else:
                lines = build_snmp_mode(iteration)

            try:
                post_prometheus_lines(base_url, lines)
                print(f"iteration={iteration}: wrote {len(lines)} lines")
            except Exception as exc:
                print(f"iteration={iteration}: failed to write metrics: {exc}")

            time.sleep(60)
    except KeyboardInterrupt:
        print("Stopped mock metrics writer")


if __name__ == "__main__":
    main()
