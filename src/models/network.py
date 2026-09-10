from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, Field


class DeviceType(StrEnum):
    SWITCH = "SWITCH"
    ROUTER = "ROUTER"


class PortMode(StrEnum):
    ACCESS = "ACCESS"
    TRUNK = "TRUNK"


class Device(BaseModel):
    id: UUID
    hostname: str
    mgmt_ip: str
    device_type: str
    status: str


class VlanInfo(BaseModel):
    vlan_id: int
    name: str | None
    admin_status: str
    oper_status: str | None
    is_protected: bool | None = None
    # IPAM поля
    ip_prefix: str | None = None  # CIDR формат, например "192.168.10.0/24"
    gateway_ip: str | None = None
    dhcp_scope_start: str | None = None
    dhcp_scope_end: str | None = None


class VlanAssignment(BaseModel):
    device_id: UUID
    port: str
    vlan_id: int
    mode: str


class NetworkState(BaseModel):
    devices: list[Device]
    vlans: list[VlanInfo]
    assignments: list[VlanAssignment]
    device_vlans: set[tuple[UUID, int]] = Field(default_factory=set)
    protected_vlans: set[int] = Field(default_factory=set)
    timestamp: datetime