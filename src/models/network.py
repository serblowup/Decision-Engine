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
    # Set by Gateway via vlan.is_protected. A protected VLAN must never be the
    # target of a destructive DE action (DELETE_VLAN, trunk removal) nor have its
    # placement changed. NULL is treated as not protected.
    is_protected: bool | None = None


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
    # VLANs flagged vlan.is_protected = TRUE. Single source of truth shared with
    # the strategies and task_builder; refreshed every decision cycle.
    protected_vlans: set[int] = Field(default_factory=set)
    timestamp: datetime
