from __future__ import annotations

from fastapi import APIRouter, Query

from app.temple.permits import WorkPermitService
from app.temple.permit_schemas import (
    DharmaServiceCancel,
    DharmaServiceCreate,
    WorkPermitAction,
    WorkPermitCreate,
    WorkPermitIssue,
    WorkPermitRevoke,
)

router = APIRouter(prefix="/api/temple/operations", tags=["修缮工序施工许可"])


def service() -> WorkPermitService:
    return WorkPermitService()


@router.get("/work_permit_templates")
def list_work_permit_templates():
    return {"items": service().list_templates()}


@router.post("/dharma_services", status_code=201)
def create_dharma_service(payload: DharmaServiceCreate):
    return service().create_dharma_service(payload.model_dump())


@router.get("/dharma_services")
def list_dharma_services(temple_code: str | None = None):
    return {"items": service().list_dharma_services(temple_code)}


@router.get("/dharma_services/{dharma_service_id}")
def dharma_service_detail(dharma_service_id: int):
    return service().dharma_service_detail(dharma_service_id)


@router.post("/dharma_services/{dharma_service_id}/cancel")
def cancel_dharma_service(dharma_service_id: int, payload: DharmaServiceCancel):
    return service().cancel_dharma_service(dharma_service_id, payload.actor)


@router.post("/work_permits", status_code=201)
def create_work_permit(payload: WorkPermitCreate):
    return service().create_permit(payload.model_dump())


@router.get("/work_permits")
def list_work_permits(temple_code: str | None = None, state: str | None = None):
    return {"items": service().list_permits(temple_code, state)}


@router.get("/work_permits/{permit_id}")
def work_permit_detail(permit_id: int):
    return service().permit_detail(permit_id)


@router.get("/work_permits/{permit_id}/explanation")
def work_permit_explanation(permit_id: int):
    return service().permit_explanation(permit_id)


@router.post("/work_permits/{permit_id}/issue")
def issue_work_permit(permit_id: int, payload: WorkPermitIssue):
    return service().issue_permit(permit_id, payload.model_dump())


@router.post("/work_permits/{permit_id}/start")
def start_work_permit(permit_id: int, payload: WorkPermitAction):
    return service().act_permit(permit_id, "start", payload.model_dump())


@router.post("/work_permits/{permit_id}/suspend")
def suspend_work_permit(permit_id: int, payload: WorkPermitAction):
    return service().act_permit(permit_id, "suspend", payload.model_dump())


@router.post("/work_permits/{permit_id}/resume")
def resume_work_permit(permit_id: int, payload: WorkPermitAction):
    return service().act_permit(permit_id, "resume", payload.model_dump())


@router.post("/work_permits/{permit_id}/finish")
def finish_work_permit(permit_id: int, payload: WorkPermitAction):
    return service().act_permit(permit_id, "finish", payload.model_dump())


@router.post("/work_permits/{permit_id}/revoke")
def revoke_work_permit(permit_id: int, payload: WorkPermitRevoke):
    return service().revoke_permit(permit_id, payload.model_dump())


@router.post("/work_permits/refresh")
def refresh_work_permits(actor: str = Query(default="permit-guardian", min_length=1)):
    return service().refresh_permits(actor)
