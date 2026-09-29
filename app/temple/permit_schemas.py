from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

WorkType = Literal["timber_repair", "temporary_power", "hot_work"]
ShiftLabel = Literal["day", "evening", "night"]


class MonitorAssignee(BaseModel):
    role: str = Field(min_length=2, max_length=40)
    name: str = Field(min_length=1, max_length=120)


class WorkPermitCreate(BaseModel):
    temple_code: str = Field(min_length=2, max_length=64)
    code: str = Field(min_length=3, max_length=80, pattern=r"^[a-z0-9][a-z0-9._-]+$")
    name: str = Field(min_length=2, max_length=160)
    work_type: WorkType
    hall_codes: list[str] = Field(min_length=1, max_length=100)
    shift_date: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    shift_label: ShiftLabel = "day"
    timezone: str | None = Field(default=None, max_length=64)
    responsible_party: str = Field(min_length=1, max_length=120)
    monitors: list[MonitorAssignee] = Field(min_length=1, max_length=20)
    risk_items_acknowledged: list[str] | None = None
    actor: str = Field(min_length=1, max_length=120)


class WorkPermitIssue(BaseModel):
    actor: str = Field(min_length=1, max_length=120)
    role: Literal["issuer", "safety_officer", "fire_warden"] = "issuer"


class WorkPermitAction(BaseModel):
    actor: str = Field(min_length=1, max_length=120)
    reason: str = Field(default="", max_length=500)


class WorkPermitRevoke(BaseModel):
    actor: str = Field(min_length=1, max_length=120)
    reason: str = Field(min_length=2, max_length=500)


class DharmaServiceCreate(BaseModel):
    temple_code: str = Field(min_length=2, max_length=64)
    hall_code: str | None = Field(default=None, max_length=64)
    code: str = Field(min_length=3, max_length=80, pattern=r"^[a-z0-9][a-z0-9._-]+$")
    name: str = Field(min_length=2, max_length=160)
    starts_at: str
    ends_at: str
    actor: str = Field(min_length=1, max_length=120)


class DharmaServiceCancel(BaseModel):
    actor: str = Field(min_length=1, max_length=120)
