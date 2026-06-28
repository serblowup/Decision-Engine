from datetime import datetime, timezone
from uuid import uuid4

import pytest

from src.models.network import Device, DeviceType, NetworkState, PortMode, VlanAssignment, VlanInfo


@pytest.fixture
def mock_network_state() -> NetworkState:
    d1 = uuid4()
    d2 = uuid4()
    return NetworkState(
        devices=[
            Device(
                id=d1,
                hostname="sw-1",
                mgmt_ip="10.0.0.1",
                device_type=DeviceType.SWITCH,
                status="ACTIVE",
            ),
            Device(
                id=d2,
                hostname="sw-2",
                mgmt_ip="10.0.0.2",
                device_type=DeviceType.SWITCH,
                status="ACTIVE",
            ),
        ],
        vlans=[
            VlanInfo(vlan_id=10, name="users", admin_status="ACTIVE", oper_status="UP"),
            VlanInfo(vlan_id=20, name="servers", admin_status="ACTIVE", oper_status="UP"),
            VlanInfo(vlan_id=30, name="guests", admin_status="ACTIVE", oper_status="UP"),
        ],
        assignments=[
            VlanAssignment(device_id=d1, port="Gi0/1", vlan_id=20, mode=PortMode.ACCESS),
            VlanAssignment(device_id=d1, port="Gi0/2", vlan_id=10, mode=PortMode.ACCESS),
            VlanAssignment(device_id=d2, port="Gi0/1", vlan_id=20, mode=PortMode.ACCESS),
            VlanAssignment(device_id=d2, port="Gi0/2", vlan_id=30, mode=PortMode.ACCESS),
        ],
        device_vlans={(d1, 10), (d1, 20), (d2, 20), (d2, 30)},
        timestamp=datetime.now(timezone.utc),
    )


@pytest.fixture
def single_vlan_state() -> NetworkState:
    d1 = uuid4()
    d2 = uuid4()
    return NetworkState(
        devices=[
            Device(
                id=d1,
                hostname="sw-1",
                mgmt_ip="10.0.0.1",
                device_type=DeviceType.SWITCH,
                status="ACTIVE",
            ),
            Device(
                id=d2,
                hostname="sw-2",
                mgmt_ip="10.0.0.2",
                device_type=DeviceType.SWITCH,
                status="ACTIVE",
            ),
        ],
        vlans=[
            VlanInfo(vlan_id=20, name="servers", admin_status="ACTIVE", oper_status="UP"),
        ],
        assignments=[
            VlanAssignment(device_id=d1, port="Gi0/1", vlan_id=20, mode=PortMode.ACCESS),
            VlanAssignment(device_id=d2, port="Gi0/1", vlan_id=20, mode=PortMode.ACCESS),
        ],
        device_vlans={(d1, 20), (d2, 20)},
        timestamp=datetime.now(timezone.utc),
    )
