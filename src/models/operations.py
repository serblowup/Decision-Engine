from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


class ActionType(StrEnum):
    ADD_VLAN = "ADD_VLAN"
    DELETE_VLAN = "DELETE_VLAN"
    SET_ACCESS = "SET_ACCESS"
    SET_TRUNK = "SET_TRUNK"
    EDIT_TRUNK = "EDIT_TRUNK"
    SWITCH_VLAN = "SWITCH_VLAN"
    CREATE_SUBINTERFACE = "CREATE_SUBINTERFACE"
    DELETE_SUBINTERFACE = "DELETE_SUBINTERFACE"
    # IPAM actions
    SPLIT_VLAN = "SPLIT_VLAN"              # Логическое действие: разбить VLAN на два
    MERGE_VLAN = "MERGE_VLAN"              # Логическое действие: слить два VLAN
    UPDATE_VLAN_PREFIX = "UPDATE_VLAN_PREFIX"  # Обновить префикс VLAN в БД


class ActionStatus(StrEnum):
    PENDING = "PENDING"
    EXECUTING = "EXECUTING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    ROLLED_BACK = "ROLLED_BACK"


class BatchCriticality(StrEnum):
    CRITICAL = "CRITICAL"
    NORMAL = "NORMAL"
    OPTIONAL = "OPTIONAL"


class TaskStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    ROLLED_BACK = "ROLLED_BACK"


class Action(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    device_id: UUID
    action_type: ActionType
    params: dict
    previous_state: dict = Field(default_factory=dict)
    target_state: dict = Field(default_factory=dict)
    status: ActionStatus = ActionStatus.PENDING


class Batch(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    actions: list[Action]
    criticality: BatchCriticality
    status: TaskStatus = TaskStatus.PENDING


class ReconfigurationTask(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    batches: list[Batch]
    initiated_by: str
    created_at: datetime
    status: TaskStatus = TaskStatus.PENDING