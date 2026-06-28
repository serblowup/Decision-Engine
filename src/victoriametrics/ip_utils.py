from __future__ import annotations

import ipaddress
import socket
import struct
from typing import Any

from src.config import settings


def decode_netflow_ip(value: Any) -> ipaddress.IPv4Address | None:
    if value is None:
        return None
    try:
        if settings.netflow_src_encoding == "ip":
            ip = ipaddress.ip_address(str(value))
            if isinstance(ip, ipaddress.IPv4Address) and str(ip) != "0.0.0.0":
                return ip
            return None

        int_value = int(float(value))
        packed = struct.pack("!I", int_value)
        decoded = socket.inet_ntoa(packed)
        if decoded == "0.0.0.0":
            return None
        return ipaddress.IPv4Address(decoded)
    except Exception:
        return None


def find_vlan_by_ip(
    ip: ipaddress.IPv4Address,
    subnet_vlan_map: dict[ipaddress.IPv4Network, int],
) -> int | None:
    matched: list[tuple[int, int]] = []
    for network, vlan_id in subnet_vlan_map.items():
        if ip in network:
            matched.append((network.prefixlen, vlan_id))
    if not matched:
        return None
    matched.sort(key=lambda item: item[0], reverse=True)
    return matched[0][1]
