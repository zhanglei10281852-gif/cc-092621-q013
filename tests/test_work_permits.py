from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

import pytest

from app.core.clock import FrozenClock, to_storage
from app.database import get_connection
from app.temple.operations import TempleRestorationService
from app.temple.service import TempleSafetyService
from app.temple.work_permits import WorkPermitService
from app.temple.work_permits_catalog import RISK_CATALOG, catalog_snapshot

BASE = datetime(2026, 9, 26, 8, 0, tzinfo=UTC)
VALID_FROM = "2026-09-26T00:00:00+00:00"
VALID_UNTIL = "2026-09-27T03:00:00+00:00"
DAY_SHIFT = ("day", "2026-09-26T01:00:00+00:00", "2026-09-26T10:00:00+00:00")
NIGHT_SHIFT = ("night", "2026-09-26T22:00:00+00:00", "2026-09-27T02:00:00+00:00")


@pytest.fixture(autouse=True)
def isolated_database(tmp_path, monkeypatch):
    monkeypatch.setenv("TEMPLE_DATABASE_PATH", str(tmp_path / "work-permits.db"))
    from app.database import close_connection, init_db
    close_connection()
    init_db()
    yield
    close_connection()


def service(at: datetime = BASE) -> WorkPermitService:
    return WorkPermitService(get_connection(), FrozenClock(at))


def safety(at: datetime = BASE) -> TempleSafetyService:
    return TempleSafetyService(get_connection(), FrozenClock(at))


def shifts_payload() -> list[dict]:
    return [
        {"shift_code": DAY_SHIFT[0], "starts_at": DAY_SHIFT[1], "ends_at": DAY_SHIFT[2]},
        {"shift_code": NIGHT_SHIFT[0], "starts_at": NIGHT_SHIFT[1], "ends_at": NIGHT_SHIFT[2]},
    ]


def permit_payload(**overrides) -> dict:
    payload = {
        "temple_code": "shanmen-temple",
        "code": "hot-work-beam-001",
        "title": "大雄宝殿梁架焊补",
        "work_type": "hot_work",
        "hall_codes": ["main-hall"],
        "shifts": shifts_payload(),
        "responsibles": ["foreman-zhang"],
        "valid_from": VALID_FROM,
        "valid_until": VALID_UNTIL,
        "actor": "clerk-li",
    }
    payload.update(overrides)
    return payload


def prepare_temple() -> None:
    svc = safety()
    svc.create_temple({
        "code": "shanmen-temple", "name": "山门古寺", "temple_type": "heritage",
        "timezone": "Asia/Shanghai", "max_concurrent_mitigation_sessions": 50, "ventilation_capacity": 1000,
    })
    svc.add_hall("shanmen-temple", {
        "code": "main-hall", "name": "大雄宝殿", "visit_order": 1,
        "expected_visit_seconds": 900, "ventilation_capacity": 400,
    })
    svc.add_hall("shanmen-temple", {
        "code": "side-hall", "name": "偏殿", "visit_order": 2,
        "expected_visit_seconds": 600, "ventilation_capacity": 200,
    })


def create_hot_work_permit(code: str = "hot-work-beam-001", halls=("main-hall",)) -> dict:
    return service().create_permit(permit_payload(code=code, hall_codes=list(halls)))


def open_incident(hall_code: str = "main-hall", severity_pm25: float = 500.0) -> int:
    svc = safety()
    svc.create_incense_profile({
        "incense_code": "festival-incense", "name": "节庆香火", "activity_type": "festival",
        "pm25_target": 100, "co_target": 0.01, "min_supply_airflow": 8, "min_exhaust_airflow": 4,
        "default_risk_priority": 70,
    })
    result = svc.ingest_observation({
        "observation_key": f"obs-{hall_code}-{severity_pm25}",
        "temple_code": "shanmen-temple",
        "hall_code": hall_code,
        "incense_code": "festival-incense",
        "steward_hash": "steward-incident-0001",
        "sensor_class": "ceiling-sensor",
        "visitor_density": 300,
        "pm25_ugm3": severity_pm25,
        "co_ppm": 0.2,
        "supply_airflow": 1.0,
        "exhaust_airflow": 0.2,
        "observed_at": "2026-09-26T07:30:00+00:00",
    })
    assert result["safety_incident_id"] is not None
    return result["safety_incident_id"]


def satisfy_signoffs_and_attendants(permit_id: int) -> None:
    svc = service()
    svc.add_signoff(permit_id, "safety_officer", {"signer": "officer-wang", "actor": "officer-wang"})
    svc.add_signoff(permit_id, "fire_warden", {"signer": "warden-zhao", "actor": "warden-zhao"})
    svc.checkin_attendant(permit_id, {"person": "monitor-chen", "attendant_role": "safety_monitor", "actor": "clerk-li"})
    svc.checkin_attendant(permit_id, {"person": "watch-sun", "attendant_role": "fire_watch", "actor": "clerk-li"})


def issued_active_permit(code: str = "hot-work-beam-001") -> dict:
    permit = create_hot_work_permit(code)
    satisfy_signoffs_and_attendants(permit["id"])
    svc = service()
    svc.issue_permit(permit["id"], "officer-wang")
    return svc.start_work(permit["id"], "foreman-zhang", "班前条件确认，开始动火")


# ------------------------------------------------------------------ 冻结清单

def test_permit_freezes_risk_catalog_by_work_type():
    prepare_temple()
    hot = create_hot_work_permit("hot-work-freeze")
    snapshot = catalog_snapshot("hot_work")
    assert hot["catalog_version"] == snapshot["catalog_version"]
    assert hot["risk_items"] == snapshot["risk_items"]
    assert hot["required_attendant_roles"] == ["safety_monitor", "fire_watch"]
    assert hot["required_signoff_roles"] == ["safety_officer", "fire_warden"]
    # 运行时目录变化不影响已冻结的许可快照
    RISK_CATALOG["hot_work"]["risk_items"].append({"code": "future-risk", "name": "未来新增风险", "severity": "minor"})
    try:
        stored = service().permit_detail(hot["id"])
        assert all(item["code"] != "future-risk" for item in stored["risk_items"])
        timber = service().create_permit(permit_payload(
            code="timber-repair-001", work_type="timber_repair",
            title="木构件修补", hall_codes=["side-hall"],
        ))
        assert {item["code"] for item in timber["risk_items"]} == {item["code"] for item in catalog_snapshot("timber_repair")["risk_items"]}
    finally:
        RISK_CATALOG["hot_work"]["risk_items"].pop()


def test_permit_persists_halls_shifts_and_responsibles():
    prepare_temple()
    permit = create_hot_work_permit("hot-work-scope", halls=("main-hall", "side-hall"))
    assert [hall["code"] for hall in permit["halls"]] == ["main-hall", "side-hall"]
    assert [shift["shift_code"] for shift in permit["shifts"]] == ["day", "night"]
    assert permit["responsibles"] == ["foreman-zhang"]


def test_shift_outside_validity_rejected_even_across_day_boundary():
    prepare_temple()
    bad_shifts = shifts_payload() + [{"shift_code": "late", "starts_at": "2026-09-27T02:00:00+00:00", "ends_at": "2026-09-27T04:00:00+00:00"}]
    with pytest.raises(Exception) as exc:
        service().create_permit(permit_payload(code="hot-work-bad-shift", shifts=bad_shifts))
    assert getattr(exc.value, "code", "") == "validation_error"


# ------------------------------------------------------------------ 签发核验

def test_issue_requires_multi_role_signoffs():
    prepare_temple()
    permit = create_hot_work_permit("hot-work-signoff")
    svc = service()
    with pytest.raises(Exception) as exc:
        svc.issue_permit(permit["id"], "officer-wang")
    assert exc.value.context["missing_signoff_roles"] == ["safety_officer", "fire_warden"]
    with pytest.raises(Exception) as exc:
        svc.add_signoff(permit["id"], "electrician", {"signer": "spark-li", "actor": "spark-li"})
    assert exc.value.code == "conflict"
    svc.add_signoff(permit["id"], "safety_officer", {"signer": "officer-wang", "actor": "officer-wang"})
    # 同一签署人重复签署不产生重复记录
    svc.add_signoff(permit["id"], "safety_officer", {"signer": "officer-wang", "actor": "officer-wang"})
    with pytest.raises(Exception) as exc:
        svc.issue_permit(permit["id"], "officer-wang")
    assert exc.value.context["missing_signoff_roles"] == ["fire_warden"]
    svc.add_signoff(permit["id"], "fire_warden", {"signer": "warden-zhao", "actor": "warden-zhao"})
    # 签署齐备但监护未到岗仍拒绝签发
    with pytest.raises(Exception) as exc:
        svc.issue_permit(permit["id"], "officer-wang")
    reason_codes = {item["code"] for item in exc.value.context["reasons"]}
    assert reason_codes == {"attendant_missing"}


def test_issue_checks_closure_ceremony_hall_and_open_incidents():
    prepare_temple()
    permit = create_hot_work_permit("hot-work-preconditions")
    satisfy_signoffs_and_attendants(permit["id"])
    svc = service()

    closure_svc = TempleRestorationService(get_connection(), FrozenClock(BASE))
    closure_svc.create_closure({
        "temple_code": "shanmen-temple", "hall_code": "main-hall", "code": "beam-closure",
        "reason": "梁架检查封闭", "starts_at": "2026-09-26T06:00:00+00:00",
        "ends_at": "2026-09-26T09:00:00+00:00", "drain_mode": "block_new", "actor": "operator",
    })
    with pytest.raises(Exception) as exc:
        svc.issue_permit(permit["id"], "officer-wang")
    assert {item["code"] for item in exc.value.context["reasons"]} == {"hall_closure"}
    get_connection().execute("UPDATE hall_closure_windows SET state='cancelled' WHERE code='beam-closure'")

    svc.create_ceremony_schedule({
        "temple_code": "shanmen-temple", "hall_code": "main-hall", "code": "morning-puja",
        "name": "早课法会", "starts_at": "2026-09-26T07:00:00+00:00",
        "ends_at": "2026-09-26T09:30:00+00:00", "actor": "monke",
    })
    with pytest.raises(Exception) as exc:
        svc.issue_permit(permit["id"], "officer-wang")
    assert any(item["context"].get("ceremony_code") == "morning-puja" for item in exc.value.context["reasons"])
    get_connection().execute("UPDATE ceremony_schedules SET state='cancelled' WHERE code='morning-puja'")

    get_connection().execute("UPDATE worship_halls SET status='closure' WHERE code='main-hall'")
    with pytest.raises(Exception) as exc:
        svc.issue_permit(permit["id"], "officer-wang")
    assert any(item["code"] == "hall_not_open" for item in exc.value.context["reasons"])
    get_connection().execute("UPDATE worship_halls SET status='active' WHERE code='main-hall'")

    incident_id = open_incident()
    with pytest.raises(Exception) as exc:
        svc.issue_permit(permit["id"], "officer-wang")
    incident_reason = next(item for item in exc.value.context["reasons"] if item["code"] == "open_safety_incident")
    assert incident_reason["context"]["safety_incident_id"] == incident_id
    get_connection().execute("UPDATE safety_incidents SET state='resolved',resolved_at=? WHERE id=?", (to_storage(BASE), incident_id))

    issued = svc.issue_permit(permit["id"], "officer-wang")
    assert issued["state"] == "issued"
    assert issued["issued_by"] == "officer-wang"


# ------------------------------------------------------------------ 生命周期

def test_full_lifecycle_with_pause_resume_and_complete():
    prepare_temple()
    permit = issued_active_permit("hot-work-lifecycle")
    svc = service()
    assert permit["state"] == "active"

    # 固定时钟下重复开工得到确定的冲突结果
    with pytest.raises(Exception) as exc:
        svc.start_work(permit["id"], "foreman-zhang", "重复开工")
    assert exc.value.code == "conflict"

    paused = svc.pause_work(permit["id"], "foreman-zhang", "午间暂停动火")
    assert paused["state"] == "suspended"
    assert paused["suspend_kind"] == "manual"
    with pytest.raises(Exception):
        svc.pause_work(permit["id"], "foreman-zhang", "重复暂停")

    # 白班结束、进入班次间隙，复工被拒绝并指明下一个跨日班次
    gap_svc = service(BASE.replace(hour=10, minute=30))
    with pytest.raises(Exception) as exc:
        gap_svc.resume_work(permit["id"], "foreman-zhang", "尝试复工")
    assert any(item["code"] == "outside_shift" for item in exc.value.context["reasons"])
    next_shift = next(item for item in exc.value.context["reasons"] if item["code"] == "outside_shift")["context"]["next_shift"]
    assert next_shift["shift_code"] == "night"

    # 跨日夜班内复工成功
    night_svc = service(BASE.replace(hour=23))
    resumed = night_svc.resume_work(permit["id"], "foreman-zhang", "夜班复工")
    assert resumed["state"] == "active"
    completed = night_svc.complete_work(permit["id"], "foreman-zhang", "动火完成，火种熄灭")
    assert completed["state"] == "completed"
    event_types = [event["event_type"] for event in completed["events"]]
    assert event_types == [
        "created", "signoff", "signoff", "issued", "work_started", "paused", "resumed", "work_completed",
    ]
    # 已完成工作与责任记录仍然保留
    assert completed["responsibles"] == ["foreman-zhang"]
    assert len(completed["signoffs"]) == 2
    with pytest.raises(Exception):
        night_svc.complete_work(permit["id"], "foreman-zhang", "重复收工")


def test_start_rejected_outside_shift_and_allowed_inside_cross_day_shift():
    prepare_temple()
    permit = create_hot_work_permit("hot-work-crossday")
    satisfy_signoffs_and_attendants(permit["id"])
    svc = service(BASE.replace(hour=10, minute=30))
    svc.issue_permit(permit["id"], "officer-wang")
    with pytest.raises(Exception) as exc:
        svc.start_work(permit["id"], "foreman-zhang", "班次间隙开工")
    assert any(item["code"] == "outside_shift" for item in exc.value.context["reasons"])

    night = service(datetime(2026, 9, 27, 0, 30, tzinfo=UTC))
    active = night.start_work(permit["id"], "foreman-zhang", "跨日夜班开工")
    assert active["state"] == "active"
    assert active["decision"]["in_shift"] is True

    # 跨日班次与许可有效期结束后，扫描器自动挂起且不可复工
    expired = service(datetime(2026, 9, 27, 3, 30, tzinfo=UTC))
    evaluation = expired.evaluate_permits("supervisor")
    assert evaluation["suspended"][0]["work_permit_id"] == permit["id"]
    assert any(item["code"] == "outside_validity" for item in evaluation["suspended"][0]["reasons"])
    with pytest.raises(Exception) as exc:
        expired.resume_work(permit["id"], "foreman-zhang", "许可过期后复工")
    assert any(item["code"] == "outside_validity" for item in exc.value.context["reasons"])


# ------------------------------------------------------------------ 自动挂起

def test_auto_suspension_on_ceremony_conflict_preserves_history():
    prepare_temple()
    permit = issued_active_permit("hot-work-auto")
    # 香火开放时段附近临时安排法会
    service().create_ceremony_schedule({
        "temple_code": "shanmen-temple", "hall_code": "main-hall", "code": "emergency-puja",
        "name": "临时祈福法会", "starts_at": "2026-09-26T07:30:00+00:00",
        "ends_at": "2026-09-26T09:00:00+00:00", "actor": "monke",
    })
    result = service().evaluate_permits("supervisor")
    assert result["suspended"][0]["work_permit_id"] == permit["id"]
    assert any(item["code"] == "conflicting_ceremony" for item in result["suspended"][0]["reasons"])
    # 固定时钟下重复扫描是幂等的
    assert service().evaluate_permits("supervisor")["suspended"] == []

    detail = service().permit_detail(permit["id"])
    assert detail["state"] == "suspended"
    assert detail["suspend_kind"] == "auto"
    assert detail["decision"]["construction_allowed"] is False
    assert detail["decision"]["action_required"] == "resolve_preconditions"
    # 挂起期间不能收工抹掉工作，也不能复工
    with pytest.raises(Exception):
        service().complete_work(permit["id"], "foreman-zhang", "试图收工")
    with pytest.raises(Exception):
        service().resume_work(permit["id"], "foreman-zhang", "法会未结束就复工")

    # 法会取消后，复工恢复施工，已完成时间线完整保留
    get_connection().execute("UPDATE ceremony_schedules SET state='cancelled' WHERE code='emergency-puja'")
    resumed = service().resume_work(permit["id"], "foreman-zhang", "法会结束复工")
    assert resumed["state"] == "active"
    event_types = [event["event_type"] for event in resumed["events"]]
    assert event_types == ["created", "signoff", "signoff", "issued", "work_started", "auto_suspended", "resumed"]
    assert len(resumed["halls"]) == 1 and resumed["halls"][0]["code"] == "main-hall"


def test_auto_suspension_when_required_attendant_leaves():
    prepare_temple()
    permit = issued_active_permit("hot-work-attendant")
    service().checkout_attendant(permit["id"], {"person": "watch-sun", "attendant_role": "fire_watch"})
    result = service().evaluate_permits("supervisor")
    assert any(item["code"] == "attendant_missing" for item in result["suspended"][0]["reasons"])
    detail = service().permit_detail(permit["id"])
    assert detail["decision"]["construction_allowed"] is False
    service().checkin_attendant(permit["id"], {"person": "watch-sun", "attendant_role": "fire_watch", "actor": "clerk-li"})
    resumed = service().resume_work(permit["id"], "foreman-zhang", "监护人回到岗位")
    assert resumed["decision"]["construction_allowed"] is True


# ------------------------------------------------------------------ 紧急撤销

def test_emergency_revoke_is_idempotent_and_blocks_further_work():
    prepare_temple()
    permit = issued_active_permit("hot-work-revoke")
    svc = service()
    revoked = svc.revoke_permit(permit["id"], "abbot", "发现火情隐患，紧急撤销动火许可")
    assert revoked["state"] == "revoked"
    revoke_events_before = [event for event in service().permit_detail(permit["id"])["events"] if event["event_type"] == "revoked"]
    assert len(revoke_events_before) == 1
    # 固定时钟下重复撤销：状态确定、不追加事件
    again = svc.revoke_permit(permit["id"], "abbot", "重复撤销")
    assert again["state"] == "revoked"
    revoke_events_after = [event for event in service().permit_detail(permit["id"])["events"] if event["event_type"] == "revoked"]
    assert len(revoke_events_after) == 1
    for action in ("start", "pause", "resume", "complete"):
        with pytest.raises(Exception):
            getattr(svc, f"{action}_work")(permit["id"], "foreman-zhang", "撤销后动作")
    # 已收工许可不可撤销
    finished = issued_active_permit("hot-work-finished-then-revoke")
    service().complete_work(finished["id"], "foreman-zhang", "正常收工")
    with pytest.raises(Exception):
        service().revoke_permit(finished["id"], "abbot", "收工后撤销")


# ------------------------------------------------------------------ 不可变时间线

def test_event_timeline_is_immutable_and_sequential():
    prepare_temple()
    permit = issued_active_permit("hot-work-immutable")
    service().complete_work(permit["id"], "foreman-zhang", "收工")
    connection = get_connection()
    event_id = connection.execute("SELECT id FROM work_permit_events WHERE work_permit_id=? ORDER BY id LIMIT 1", (permit["id"],)).fetchone()["id"]
    with pytest.raises(sqlite3.Error):
        connection.execute("UPDATE work_permit_events SET actor='tampered' WHERE id=?", (event_id,))
    with pytest.raises(sqlite3.Error):
        connection.execute("DELETE FROM work_permit_events WHERE id=?", (event_id,))
    seqs = [row["seq"] for row in connection.execute("SELECT seq FROM work_permit_events WHERE work_permit_id=? ORDER BY seq", (permit["id"],)).fetchall()]
    assert seqs == list(range(1, len(seqs) + 1))
    with pytest.raises(sqlite3.Error):
        connection.execute(
            "INSERT INTO work_permit_events(work_permit_id,seq,event_type,actor,reason,detail_json,created_at) VALUES(?,?,?,?,?,?,?)",
            (permit["id"], 1, "revoked", "x", "", "{}", to_storage(BASE)),
        )


# ------------------------------------------------------------------ 查询解释

def test_decision_query_explains_why_construction_allowed_or_denied():
    prepare_temple()
    draft = create_hot_work_permit("hot-work-decision")
    decision = service().permit_detail(draft["id"])["decision"]
    assert decision["construction_allowed"] is False
    assert decision["action_required"] == "signoff"
    assert any(item["code"] == "not_issued" for item in decision["reasons"])

    satisfy_signoffs_and_attendants(draft["id"])
    issued = service().issue_permit(draft["id"], "officer-wang")
    decision = issued["decision"]
    assert decision["construction_allowed"] is False
    assert decision["action_required"] == "start"
    assert any(item["code"] == "not_started" for item in decision["reasons"])

    active = service().start_work(draft["id"], "foreman-zhang", "开工")
    decision = active["decision"]
    assert decision["construction_allowed"] is True
    assert decision["reasons"] == []
    assert decision["in_shift"] is True
    assert decision["current_shift"]["shift_code"] == "day"


# ------------------------------------------------------------------ HTTP 冒烟

def test_work_permit_api_end_to_end(client):
    create = client.post("/api/temple/temples", json={
        "code": "api-temple", "name": "接口古寺", "temple_type": "urban", "timezone": "Asia/Shanghai",
        "max_concurrent_mitigation_sessions": 10, "ventilation_capacity": 500,
    })
    assert create.status_code == 201, create.text
    assert client.post("/api/temple/temples/api-temple/halls", json={
        "code": "main-hall", "name": "大殿", "visit_order": 1, "expected_visit_seconds": 600, "ventilation_capacity": 200,
    }).status_code == 201
    permit_payload_api = {
        "temple_code": "api-temple",
        "code": "api-temp-power-001",
        "title": "临时用电接入",
        "work_type": "temporary_power",
        "hall_codes": ["main-hall"],
        "shifts": [{"shift_code": "window", "starts_at": "2020-01-01T00:00:00Z", "ends_at": "2030-01-03T00:00:00Z"}],
        "responsibles": ["electrician-zhou"],
        "valid_from": "2020-01-01T00:00:00Z",
        "valid_until": "2030-01-03T00:00:00Z",
        "actor": "clerk-li",
    }
    created = client.post("/api/temple/work-permits", json=permit_payload_api)
    assert created.status_code == 201, created.text
    permit_id = created.json()["id"]
    assert created.json()["required_attendant_roles"] == ["safety_monitor", "electrician"]
    for role, signer in (("safety_officer", "officer-wang"), ("electrician", "electrician-zhou")):
        response = client.post(f"/api/temple/work-permits/{permit_id}/signoffs/{role}", json={"signer": signer, "actor": signer})
        assert response.status_code == 200, response.text
    for role, person in (("safety_monitor", "monitor-chen"), ("electrician", "electrician-zhou")):
        response = client.post(f"/api/temple/work-permits/{permit_id}/attendants/checkin", json={"person": person, "attendant_role": role, "actor": "clerk-li"})
        assert response.status_code == 200, response.text
    assert client.post(f"/api/temple/work-permits/{permit_id}/issue", json={"actor": "officer-wang"}).status_code == 200
    assert client.post(f"/api/temple/work-permits/{permit_id}/start", json={"actor": "foreman-zhang", "reason": "按窗口开工"}).status_code == 200
    detail = client.get(f"/api/temple/work-permits/{permit_id}")
    assert detail.status_code == 200
    assert detail.json()["decision"]["construction_allowed"] is True
    listing = client.get("/api/temple/work-permits", params={"temple_code": "api-temple", "state": "active"})
    assert listing.status_code == 200 and len(listing.json()["items"]) == 1
    evaluated = client.post("/api/temple/work-permits/evaluate")
    assert evaluated.status_code == 200 and evaluated.json()["suspended"] == []
    assert client.post(f"/api/temple/work-permits/{permit_id}/complete", json={"actor": "foreman-zhang", "reason": "用电作业完成"}).status_code == 200
