from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.core.clock import FrozenClock, to_storage
from app.database import get_connection
from app.temple.operations import TempleRestorationService
from app.temple.permits import WorkPermitService
from app.temple.rules import DEFAULT_RULES

FROZEN_NOW = datetime(2026, 9, 26, 10, 0, tzinfo=UTC)  # 北京时间 18:00，晚班窗口内


def _service(clock: FrozenClock | None = None) -> WorkPermitService:
    return WorkPermitService(get_connection(), clock or FrozenClock(FROZEN_NOW))


def prepare_temple(client) -> dict:
    client.post(
        "/api/temple/temples",
        json={"code": "baoxiang-temple", "name": "宝相古寺", "temple_type": "heritage", "timezone": "Asia/Shanghai", "max_concurrent_mitigation_sessions": 100, "ventilation_capacity": 1000},
    )
    for sequence, code in enumerate(("main", "side"), start=1):
        client.post(
            "/api/temple/temples/baoxiang-temple/halls",
            json={"code": code, "name": f"{code}-hall", "visit_order": sequence, "expected_visit_seconds": 600, "ventilation_capacity": 400},
        )
    policy = client.post(
        "/api/temple/temples/baoxiang-temple/policies", json={"rules": DEFAULT_RULES, "actor": "tests"},
    ).json()
    client.post(
        f"/api/temple/policies/{policy['id']}/publish",
        json={"actor": "tests", "effective_from": "2026-09-01T00:00:00Z"},
    )
    return policy


def evening_window() -> tuple[str, str]:
    # 2026-09-26 晚班（北京 16:00-24:00）
    return "2026-09-26T08:00:00+00:00", "2026-09-27T00:00:00+00:00"


def make_closure(clock: FrozenClock, code: str = "main-evening-closure", hall_code: str = "main", start=None, end=None) -> dict:
    shift_start, shift_end = evening_window()
    return TempleRestorationService(get_connection(), clock).create_closure({
        "temple_code": "baoxiang-temple",
        "hall_code": hall_code,
        "code": code,
        "reason": "梁架修补封闭",
        "starts_at": start or shift_start,
        "ends_at": end or shift_end,
        "drain_mode": "block_new",
        "actor": "operator",
    })


def hot_work_permit_payload(**overrides) -> dict:
    payload = {
        "temple_code": "baoxiang-temple",
        "code": "hot-work-0001",
        "name": "大雄宝殿动火修补",
        "work_type": "hot_work",
        "hall_codes": ["main"],
        "shift_date": "2026-09-26",
        "shift_label": "evening",
        "timezone": "Asia/Shanghai",
        "responsible_party": "foreman-zhao",
        "monitors": [
            {"role": "safety_officer", "name": "steward-sun"},
            {"role": "fire_warden", "name": "steward-qian"},
        ],
        "risk_items_acknowledged": [
            "明火引燃木构与织物",
            "焊渣飞溅复燃",
            "烟气聚集",
            "灭火器材不足",
        ],
        "actor": "foreman-zhao",
    }
    payload.update(overrides)
    return payload


def test_templates_freeze_risk_list_and_monitors_by_work_type(client):
    prepare_temple(client)
    templates = {item["work_type"]: item for item in _service().list_templates()}
    assert set(templates) == {"timber_repair", "temporary_power", "hot_work"}
    assert templates["hot_work"]["required_monitors"] == ["safety_officer", "fire_warden"]
    assert templates["hot_work"]["blocks_incense_hours"] is True
    assert templates["timber_repair"]["required_monitors"] == ["safety_officer"]
    # 风险清单被作业类型冻结：增删条目不允许
    payload = hot_work_permit_payload(risk_items_acknowledged=["明火引燃木构与织物"])
    with pytest.raises(Exception) as exc:
        _service().create_permit(payload)
    assert "冻结" in str(exc.value)
    # 缺少作业类型要求的监护角色不允许建许可
    payload = hot_work_permit_payload(code="hot-work-0002", monitors=[{"role": "safety_officer", "name": "steward-sun"}])
    with pytest.raises(Exception) as exc:
        _service().create_permit(payload)
    assert "fire_warden" in str(exc.value)


def test_issue_verifies_closure_dharma_monitoring_and_incidents(client):
    prepare_temple(client)
    service = _service()
    permit = service.create_permit(hot_work_permit_payload())
    # 未封闭：解释接口明确给出禁止原因
    explanation = service.permit_explanation(permit["id"])
    assert explanation["verdict"] == "禁止施工"
    assert {item["code"] for item in explanation["checks"] if not item["satisfied"]} >= {"hall_closure", "incense_isolation"}
    # 前两个必需角色会签：许可仍是草稿，不触发终验
    make_closure(FrozenClock(FROZEN_NOW))
    partial = service.issue_permit(permit["id"], {"actor": "boss-li", "role": "issuer"})
    assert partial["state"] == "draft"
    assert {item["role"] for item in partial["signoffs"]} == {"issuer"}
    partial = service.issue_permit(permit["id"], {"actor": "steward-sun", "role": "safety_officer"})
    assert partial["state"] == "draft"
    # 排定冲突法会：最后一个角色齐签时必须拒绝
    dharma = service.create_dharma_service({
        "temple_code": "baoxiang-temple", "hall_code": "main", "code": "evening-ceremony",
        "name": "晚课法会", "starts_at": "2026-09-26T11:00:00+00:00", "ends_at": "2026-09-26T12:00:00+00:00", "actor": "office",
    })
    with pytest.raises(Exception) as exc:
        service.issue_permit(permit["id"], {"actor": "steward-qian", "role": "fire_warden"})
    failed = {item["code"] for item in exc.value.context["failed_checks"]}
    assert failed == {"dharma_conflict"}
    # 已完成的签署没有被抹掉
    assert len(service.permit_detail(permit["id"])["signoffs"]) == 3
    # 取消法会后同一人重复签署幂等通过，许可签发
    service.cancel_dharma_service(dharma["id"], "office")
    issued = service.issue_permit(permit["id"], {"actor": "steward-qian", "role": "fire_warden"})
    assert issued["state"] == "issued"
    assert issued["risk_items"] == [
        "明火引燃木构与织物", "焊渣飞溅复燃", "烟气聚集", "灭火器材不足",
    ]


def test_unresolved_incident_blocks_issue(client):
    prepare_temple(client)
    clock = FrozenClock(FROZEN_NOW)
    make_closure(clock)
    client.post(
        "/api/temple/authorizations",
        json={"steward_hash": "steward-xxxxxxxx", "temple_code": "baoxiang-temple", "authorization_code": "duty", "valid_from": "2026-09-01T00:00:00Z", "valid_until": "2026-10-01T00:00:00Z", "source_approval_id": "approval-1"},
    )
    client.post(
        "/api/temple/incense_profiles",
        json={"incense_code": "daily-incense", "name": "日常上香", "activity_type": "daily", "pm25_target": 100, "co_target": 0.01, "min_supply_airflow": 8, "min_exhaust_airflow": 4, "default_risk_priority": 60},
    )
    observation = client.post(
        "/api/temple/observations",
        json={"observation_key": "obs-incident-1", "temple_code": "baoxiang-temple", "hall_code": "main", "incense_code": "daily-incense", "steward_hash": "steward-xxxxxxxx", "sensor_class": "ceiling-sensor", "visitor_density": 0, "pm25_ugm3": 500, "co_ppm": 0.2, "supply_airflow": 1, "exhaust_airflow": 0.2, "observed_at": to_storage(FROZEN_NOW)},
    ).json()
    assert observation["safety_incident_id"] is not None
    service = _service(clock)
    permit = service.create_permit(hot_work_permit_payload(code="hot-work-incident"))
    service.issue_permit(permit["id"], {"actor": "boss-li", "role": "issuer"})
    service.issue_permit(permit["id"], {"actor": "steward-sun", "role": "safety_officer"})
    with pytest.raises(Exception) as exc:
        service.issue_permit(permit["id"], {"actor": "steward-qian", "role": "fire_warden"})
    assert "open_incidents" in {item["code"] for item in exc.value.context["failed_checks"]}
    # 隐患解决后可以签发
    connection = get_connection()
    connection.execute("UPDATE safety_incidents SET state='resolved',resolved_at=? WHERE id=?", (to_storage(FROZEN_NOW), observation["safety_incident_id"]))
    connection.commit()
    issued = service.issue_permit(permit["id"], {"actor": "steward-qian", "role": "fire_warden"})
    assert issued["state"] == "issued"


def test_lifecycle_timeline_and_duplicate_actions_are_idempotent(client):
    prepare_temple(client)
    clock = FrozenClock(FROZEN_NOW)
    make_closure(clock)
    service = _service(clock)
    permit = service.create_permit(hot_work_permit_payload(code="hot-work-life"))
    for actor, role in (("boss-li", "issuer"), ("steward-sun", "safety_officer"), ("steward-qian", "fire_warden")):
        service.issue_permit(permit["id"], {"actor": actor, "role": role})
    started = service.act_permit(permit["id"], "start", {"actor": "foreman-zhao", "reason": "开始动火"})
    assert started["state"] == "in_progress"
    # 固定时钟下重复开工：返回同一状态，时间线不追加
    duplicate_start = service.act_permit(permit["id"], "start", {"actor": "foreman-zhao", "reason": "重复开工"})
    assert duplicate_start["state"] == "in_progress"
    suspended = service.act_permit(permit["id"], "suspend", {"actor": "foreman-zhao", "reason": "监护人临时离场"})
    assert suspended["state"] == "suspended"
    duplicate_suspend = service.act_permit(permit["id"], "suspend", {"actor": "foreman-zhao", "reason": "重复暂停"})
    assert duplicate_suspend["state"] == "suspended"
    resumed = service.act_permit(permit["id"], "resume", {"actor": "foreman-zhao", "reason": "监护归位"})
    assert resumed["state"] == "in_progress"
    finished = service.act_permit(permit["id"], "finish", {"actor": "foreman-zhao", "reason": "作业完成"})
    assert finished["state"] == "completed"
    duplicate_finish = service.act_permit(permit["id"], "finish", {"actor": "foreman-zhao", "reason": "重复收工"})
    assert duplicate_finish["state"] == "completed"
    event_types = [event["event_type"] for event in finished["events"]]
    assert event_types == ["created", "signoff_collected", "signoff_collected", "signoff_collected", "issued", "started", "suspended", "resumed", "completed"]
    # 时间线不可变：事件记录只增不改，重复动作没有产生重复事件
    assert len(finished["events"]) == len(duplicate_finish["events"])


def test_cross_day_shift_rejects_start_outside_window(client):
    prepare_temple(client)
    # 2026-09-26 夜班（北京 00:00-08:00 → UTC 前一日 16:00 至当日 00:00）
    night_clock = FrozenClock(datetime(2026, 9, 25, 18, 0, tzinfo=UTC))
    TempleRestorationService(get_connection(), night_clock).create_closure({
        "temple_code": "baoxiang-temple", "hall_code": "main", "code": "main-night-closure", "reason": "夜间木构修补",
        "starts_at": "2026-09-25T16:00:00+00:00", "ends_at": "2026-09-26T00:00:00+00:00", "drain_mode": "block_new", "actor": "operator",
    })
    service = _service(night_clock)
    permit = service.create_permit({
        "temple_code": "baoxiang-temple", "code": "timber-night-1", "name": "夜班木构修补", "work_type": "timber_repair",
        "hall_codes": ["main"], "shift_date": "2026-09-26", "shift_label": "night", "timezone": "Asia/Shanghai",
        "responsible_party": "foreman-zhao",
        "monitors": [{"role": "safety_officer", "name": "steward-sun"}],
        "actor": "foreman-zhao",
    })
    for actor, role in (("boss-li", "issuer"), ("steward-sun", "safety_officer")):
        service.issue_permit(permit["id"], {"actor": actor, "role": role})
    started = service.act_permit(permit["id"], "start", {"actor": "foreman-zhao", "reason": "夜班开工"})
    assert started["state"] == "in_progress"
    # 时钟跨过班次结束（北京 08:00 后）：开工动作被确定性拒绝
    after_clock = FrozenClock(datetime(2026, 9, 26, 2, 0, tzinfo=UTC))
    service_after = _service(after_clock)
    finished = service_after.act_permit(permit["id"], "finish", {"actor": "foreman-zhao", "reason": "跨日收工"})
    assert finished["state"] == "completed"
    with pytest.raises(Exception) as exc:
        service_after.act_permit(permit["id"], "start", {"actor": "foreman-zhao", "reason": "跨日后试图重新开工"})
    assert exc.value.code == "conflict"


def test_precondition_failure_auto_suspends_without_erasing_work(client):
    prepare_temple(client)
    clock = FrozenClock(FROZEN_NOW)
    make_closure(clock)
    service = _service(clock)
    permit = service.create_permit(hot_work_permit_payload(code="hot-work-suspend"))
    for actor, role in (("boss-li", "issuer"), ("steward-sun", "safety_officer"), ("steward-qian", "fire_warden")):
        service.issue_permit(permit["id"], {"actor": actor, "role": role})
    service.act_permit(permit["id"], "start", {"actor": "foreman-zhao", "reason": "开工"})
    # 作业进行中突然排定冲突法会：许可自动挂起
    dharma = service.create_dharma_service({
        "temple_code": "baoxiang-temple", "hall_code": "main", "code": "sudden-ceremony",
        "name": "临时法会", "starts_at": "2026-09-26T10:30:00+00:00", "ends_at": "2026-09-26T11:30:00+00:00", "actor": "office",
    })
    detail = service.permit_detail(permit["id"])
    assert detail["state"] == "auto_suspended"
    assert "dharma_conflict" in detail["suspension_reason"]
    # 已完成工作（开工事件）仍然保留
    assert "started" in [event["event_type"] for event in detail["events"]]
    # 条件未恢复前复工被拒
    with pytest.raises(Exception) as exc:
        service.act_permit(permit["id"], "resume", {"actor": "foreman-zhao", "reason": "试图复工"})
    assert "dharma_conflict" in {item["code"] for item in exc.value.context["failed_checks"]}
    # 法会取消：自动挂起不自动解除，必须人工复工
    service.cancel_dharma_service(dharma["id"], "office")
    assert service.permit_detail(permit["id"])["state"] == "auto_suspended"
    resumed = service.act_permit(permit["id"], "resume", {"actor": "foreman-zhao", "reason": "法会取消后人工复工"})
    assert resumed["state"] == "in_progress"
    assert "auto_suspended" in [event["event_type"] for event in resumed["events"]]


def test_emergency_revoke_is_terminal_and_preserves_timeline(client):
    prepare_temple(client)
    clock = FrozenClock(FROZEN_NOW)
    make_closure(clock)
    service = _service(clock)
    permit = service.create_permit(hot_work_permit_payload(code="hot-work-revoke"))
    for actor, role in (("boss-li", "issuer"), ("steward-sun", "safety_officer"), ("steward-qian", "fire_warden")):
        service.issue_permit(permit["id"], {"actor": actor, "role": role})
    service.act_permit(permit["id"], "start", {"actor": "foreman-zhao", "reason": "开工"})
    revoked = service.revoke_permit(permit["id"], {"actor": "abbot", "reason": "山火预警，紧急撤销"})
    assert revoked["state"] == "revoked"
    assert revoked["events"][-1]["event_type"] == "revoked"
    assert revoked["events"][-1]["detail"]["emergency"] is True
    # 已完成工作未被抹除
    assert "started" in [event["event_type"] for event in revoked["events"]]
    # 终态动作有确定结果：重复撤销冲突、任何施工动作冲突
    with pytest.raises(Exception) as exc:
        service.revoke_permit(permit["id"], {"actor": "abbot", "reason": "再次撤销"})
    assert exc.value.code == "conflict"
    for action in ("start", "resume", "finish"):
        with pytest.raises(Exception):
            service.act_permit(permit["id"], action, {"actor": "foreman-zhao", "reason": "终态后动作"})


def test_duplicate_signoff_by_different_person_rejected(client):
    prepare_temple(client)
    make_closure(FrozenClock(FROZEN_NOW))
    service = _service()
    permit = service.create_permit(hot_work_permit_payload(code="hot-work-signoff"))
    service.issue_permit(permit["id"], {"actor": "boss-li", "role": "issuer"})
    service.issue_permit(permit["id"], {"actor": "steward-sun", "role": "safety_officer"})
    # 同一角色换人签署：确定拒绝
    with pytest.raises(Exception) as exc:
        service.issue_permit(permit["id"], {"actor": "intruder", "role": "safety_officer"})
    assert exc.value.code == "conflict"
    # 同一人重复签署：幂等忽略
    again = service.issue_permit(permit["id"], {"actor": "boss-li", "role": "issuer"})
    assert len(again["signoffs"]) == 2
    signoff_events = [event for event in again["events"] if event["event_type"] == "signoff_collected"]
    assert len(signoff_events) == 2


def test_explanation_reports_why_work_is_allowed_or_forbidden(client):
    prepare_temple(client)
    clock = FrozenClock(FROZEN_NOW)
    service = _service(clock)
    permit = service.create_permit(hot_work_permit_payload(code="hot-work-explain"))
    before = service.permit_explanation(permit["id"])
    assert before["verdict"] == "禁止施工"
    assert before["can_work_now"] is False
    assert any("封闭" in reason for reason in before["reasons"])
    make_closure(clock)
    for actor, role in (("boss-li", "issuer"), ("steward-sun", "safety_officer"), ("steward-qian", "fire_warden")):
        service.issue_permit(permit["id"], {"actor": actor, "role": role})
    ready = service.permit_explanation(permit["id"])
    assert ready["verdict"] == "允许施工"
    assert ready["within_shift"] is True
    assert "start" in ready["allowed_actions"]
    service.act_permit(permit["id"], "start", {"actor": "foreman-zhao", "reason": "开工"})
    assert service.permit_explanation(permit["id"])["can_work_now"] is True
    # 封闭窗口提前结束：前置条件失效，自动挂起且解释中给出原因
    connection = get_connection()
    connection.execute("UPDATE hall_closure_windows SET ends_at=? WHERE code='main-evening-closure'", ("2026-09-26T09:30:00+00:00",))
    connection.commit()
    result = service.refresh_permits("guardian")
    assert permit["id"] in result["auto_suspended"]
    blocked = service.permit_explanation(permit["id"])
    assert blocked["state"] == "auto_suspended"
    assert blocked["verdict"] == "禁止施工"
    assert any("封闭" in reason for reason in blocked["reasons"])


def test_work_permit_api_routing(client):
    prepare_temple(client)
    created = client.post("/api/temple/operations/work_permits", json=hot_work_permit_payload(code="hot-work-api-1"))
    assert created.status_code == 201, created.text
    permit_id = created.json()["id"]
    missing = client.post(f"/api/temple/operations/work_permits/{permit_id}/issue", json={"actor": "boss-li", "role": "issuer"})
    assert missing.status_code == 200
    assert missing.json()["state"] == "draft"
    explanation = client.get(f"/api/temple/operations/work_permits/{permit_id}/explanation")
    assert explanation.status_code == 200
    assert explanation.json()["verdict"] == "禁止施工"
    templates = client.get("/api/temple/operations/work_permit_templates")
    assert templates.status_code == 200
    assert len(templates.json()["items"]) == 3
