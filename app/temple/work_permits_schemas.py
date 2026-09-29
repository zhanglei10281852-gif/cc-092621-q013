from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

WorkTypeLiteral = Literal["timber_repair", "temporary_power", "hot_work"]


class WorkShift(BaseModel):
    shift_code: str = Field(min_length=1, max_length=64)
    starts_at: str
    ends_at: str


class WorkPermitCreate(BaseModel):
    temple_code: str = Field(min_length=2, max_length=64)
    code: str = Field(min_length=3, max_length=80, pattern=r"^[a-z0-9][a-z0-9._-]+$")
    title: str = Field(min_length=2, max_length=160)
    work_type: WorkTypeLiteral
    hall_codes: list[str] = Field(min_length=1, max_length=100)
    shifts: list[WorkShift] = Field(min_length=1, max_length=30)
    responsibles: list[str] = Field(min_length=1, max_length=50)
    restoration_campaign_id: int | None = Field(default=None, gt=0)
    valid_from: str
    valid_until: str
    actor: str = Field(min_length=1, max_length=120)

    @model_validator(mode="after")
    def validate_permit(self) -> "WorkPermitCreate":
        if len(self.hall_codes) != len(set(self.hall_codes)):
            raise ValueError("适用殿堂不能重复")
        if len(self.responsibles) != len(set(self.responsibles)):
            raise ValueError("责任人不能重复")
        codes = [shift.shift_code for shift in self.shifts]
        if len(codes) != len(set(codes)):
            raise ValueError("班次编码不能重复")
        return self


class WorkPermitSignoff(BaseModel):
    signer: str = Field(min_length=1, max_length=120)
    actor: str = Field(min_length=1, max_length=120)


class WorkPermitAction(BaseModel):
    actor: str = Field(min_length=1, max_length=120)
    reason: str = Field(min_length=2, max_length=500)


class WorkPermitActor(BaseModel):
    actor: str = Field(min_length=1, max_length=120)


class WorkPermitAttendantCheckin(BaseModel):
    person: str = Field(min_length=1, max_length=120)
    attendant_role: Literal["safety_monitor", "fire_watch", "electrician"]
    actor: str = Field(min_length=1, max_length=120)


class WorkPermitAttendantCheckout(BaseModel):
    person: str = Field(min_length=1, max_length=120)
    attendant_role: Literal["safety_monitor", "fire_watch", "electrician"]


class CeremonyScheduleCreate(BaseModel):
    temple_code: str = Field(min_length=2, max_length=64)
    hall_code: str | None = Field(default=None, max_length=64)
    code: str = Field(min_length=3, max_length=80, pattern=r"^[a-z0-9][a-z0-9._-]+$")
    name: str = Field(min_length=2, max_length=160)
    starts_at: str
    ends_at: str
    actor: str = Field(min_length=1, max_length=120)
