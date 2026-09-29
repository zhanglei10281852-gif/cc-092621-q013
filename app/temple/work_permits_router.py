from __future__ import annotations

from fastapi import APIRouter, Query

from app.temple.work_permits import WorkPermitService
from app.temple.work_permits_schemas import (
    CeremonyScheduleCreate,
    WorkPermitAction,
    WorkPermitActor,
    WorkPermitAttendantCheckin,
    WorkPermitAttendantCheckout,
    WorkPermitCreate,
    WorkPermitSignoff,
)

router = APIRouter(prefix="/api/temple/work-permits", tags=["修缮工序施工许可"])


def service() -> WorkPermitService:
    return WorkPermitService()


@router.post("/ceremonies", status_code=201)
def create_ceremony(payload: CeremonyScheduleCreate):
    return service().create_ceremony_schedule(payload.model_dump())


@router.get("/ceremonies")
def list_ceremonies(temple_code: str | None = None):
    return {"items": service().list_ceremony_schedules(temple_code)}


@router.post("", status_code=201)
def create_permit(payload: WorkPermitCreate):
    return service().create_permit(payload.model_dump())


@router.get("")
def list_permits(temple_code: str | None = None, state: str | None = None):
    return {"items": service().list_permits(temple_code, state)}


@router.post("/evaluate")
def evaluate_permits(actor: str = Query(default="work-permit-supervisor", min_length=1)):
    return service().evaluate_permits(actor)


@router.get("/{work_permit_id}")
def permit_detail(work_permit_id: int):
    return service().permit_detail(work_permit_id)


@router.post("/{work_permit_id}/signoffs/{signoff_role}")
def add_signoff(work_permit_id: int, signoff_role: str, payload: WorkPermitSignoff):
    return service().add_signoff(work_permit_id, signoff_role, payload.model_dump())


@router.post("/{work_permit_id}/attendants/checkin")
def checkin_attendant(work_permit_id: int, payload: WorkPermitAttendantCheckin):
    return service().checkin_attendant(work_permit_id, payload.model_dump())


@router.post("/{work_permit_id}/attendants/checkout")
def checkout_attendant(work_permit_id: int, payload: WorkPermitAttendantCheckout):
    return service().checkout_attendant(work_permit_id, payload.model_dump())


@router.post("/{work_permit_id}/issue")
def issue_permit(work_permit_id: int, payload: WorkPermitActor):
    return service().issue_permit(work_permit_id, payload.actor)


@router.post("/{work_permit_id}/start")
def start_work(work_permit_id: int, payload: WorkPermitAction):
    return service().start_work(work_permit_id, payload.actor, payload.reason)


@router.post("/{work_permit_id}/pause")
def pause_work(work_permit_id: int, payload: WorkPermitAction):
    return service().pause_work(work_permit_id, payload.actor, payload.reason)


@router.post("/{work_permit_id}/resume")
def resume_work(work_permit_id: int, payload: WorkPermitAction):
    return service().resume_work(work_permit_id, payload.actor, payload.reason)


@router.post("/{work_permit_id}/complete")
def complete_work(work_permit_id: int, payload: WorkPermitAction):
    return service().complete_work(work_permit_id, payload.actor, payload.reason)


@router.post("/{work_permit_id}/revoke")
def revoke_permit(work_permit_id: int, payload: WorkPermitAction):
    return service().revoke_permit(work_permit_id, payload.actor, payload.reason)
