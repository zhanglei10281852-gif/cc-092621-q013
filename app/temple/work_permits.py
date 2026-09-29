from __future__ import annotations

import json
import sqlite3
from typing import Any

from app.core.clock import Clock, SystemClock, from_storage, to_storage
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.database import get_connection, transaction
from app.temple.repository import TempleRepository
from app.temple.schema import ensure_temple_schema
from app.temple.work_permits_catalog import catalog_snapshot

_TERMINAL_STATES = {"completed", "revoked"}
_ACTIVE_INCIDENT_STATES = ("open", "mitigating")
_CLOSURE_LIKE_STATES = ("scheduled", "active")


class WorkPermitService:
    """面向修缮工序的施工许可：风险清单冻结、前置条件核验、不可变时间线。"""

    def __init__(self, connection: sqlite3.Connection | None = None, clock: Clock | None = None) -> None:
        self.connection = connection or get_connection()
        ensure_temple_schema(self.connection)
        self.clock = clock or SystemClock()
        self.repository = TempleRepository(self.connection)

    # ------------------------------------------------------------------ 法会窗口

    def create_ceremony_schedule(self, payload: dict[str, Any]) -> dict[str, Any]:
        temple = self._temple(payload["temple_code"])
        hall_id = None
        if payload.get("hall_code"):
            hall = self.repository.hall_by_code(temple["id"], payload["hall_code"])
            if hall is None:
                raise NotFoundError("法会殿堂不存在")
            hall_id = hall["id"]
        starts_at = self._time(payload["starts_at"], "法会开始时间")
        ends_at = self._time(payload["ends_at"], "法会结束时间")
        if ends_at <= starts_at:
            raise ValidationError("法会结束时间必须晚于开始时间")
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            try:
                cursor = connection.execute(
                    "INSERT INTO ceremony_schedules(temple_id,hall_id,code,name,starts_at,ends_at,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (temple["id"], hall_id, payload["code"], payload["name"], starts_at, ends_at, payload["actor"], now, now),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError("法会窗口编码已存在") from exc
            return self.ceremony_detail(cursor.lastrowid, connection)

    def ceremony_detail(self, ceremony_id: int, connection: sqlite3.Connection | None = None) -> dict[str, Any]:
        connection = connection or self.connection
        row = connection.execute(
            "SELECT c.*,n.code AS temple_code,s.code AS hall_code FROM ceremony_schedules c "
            "JOIN temple_sites n ON n.id=c.temple_id LEFT JOIN worship_halls s ON s.id=c.hall_id WHERE c.id=?",
            (ceremony_id,),
        ).fetchone()
        if row is None:
            raise NotFoundError("法会窗口不存在")
        return dict(row)

    def list_ceremony_schedules(self, temple_code: str | None = None) -> list[dict[str, Any]]:
        sql = (
            "SELECT c.*,n.code AS temple_code,s.code AS hall_code FROM ceremony_schedules c "
            "JOIN temple_sites n ON n.id=c.temple_id LEFT JOIN worship_halls s ON s.id=c.hall_id"
        )
        params: list[Any] = []
        if temple_code:
            sql += " WHERE n.code=?"
            params.append(temple_code)
        sql += " ORDER BY c.starts_at,c.id"
        return [dict(row) for row in self.connection.execute(sql, params).fetchall()]

    # ------------------------------------------------------------------ 许可创建

    def create_permit(self, payload: dict[str, Any]) -> dict[str, Any]:
        temple = self._temple(payload["temple_code"])
        hall_rows = self._permit_halls(temple["id"], payload["hall_codes"])
        valid_from = self._time(payload["valid_from"], "许可开始时间")
        valid_until = self._time(payload["valid_until"], "许可结束时间")
        if valid_until <= valid_from:
            raise ValidationError("许可结束时间必须晚于开始时间")
        shifts = self._normalize_shifts(payload["shifts"], valid_from, valid_until)
        snapshot = catalog_snapshot(payload["work_type"])
        if payload.get("restoration_campaign_id") is not None:
            campaign = self.connection.execute(
                "SELECT id FROM restoration_campaigns WHERE id=? AND temple_id=?",
                (payload["restoration_campaign_id"], temple["id"]),
            ).fetchone()
            if campaign is None:
                raise ValidationError("修缮活动不属于目标寺院")
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            try:
                cursor = connection.execute(
                    "INSERT INTO work_permits(temple_id,code,title,work_type,restoration_campaign_id,catalog_version,"
                    "risk_items_json,required_attendant_roles_json,required_signoff_roles_json,blocking_incident_severities_json,"
                    "valid_from,valid_until,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        temple["id"], payload["code"], payload["title"], payload["work_type"],
                        payload.get("restoration_campaign_id"), snapshot["catalog_version"],
                        self._json(snapshot["risk_items"]), self._json(snapshot["required_attendant_roles"]),
                        self._json(snapshot["required_signoff_roles"]), self._json(snapshot["blocking_incident_severities"]),
                        valid_from, valid_until, payload["actor"], now, now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError("施工许可编码已存在") from exc
            permit_id = cursor.lastrowid
            for hall in hall_rows:
                connection.execute("INSERT INTO work_permit_halls(work_permit_id,hall_id) VALUES(?,?)", (permit_id, hall["id"]))
            for seq, shift in enumerate(shifts, start=1):
                connection.execute(
                    "INSERT INTO work_permit_shifts(work_permit_id,seq,shift_code,starts_at,ends_at) VALUES(?,?,?,?,?)",
                    (permit_id, seq, shift["shift_code"], shift["starts_at"], shift["ends_at"]),
                )
            for seq, person in enumerate(dict.fromkeys(payload["responsibles"]), start=1):
                connection.execute(
                    "INSERT INTO work_permit_responsibles(work_permit_id,person,seq) VALUES(?,?,?)",
                    (permit_id, person, seq),
                )
            self._event(connection, permit_id, "created", payload["actor"], "许可草稿已建立", {"work_type": payload["work_type"]}, now)
            return self.permit_detail(permit_id, connection)

    # ------------------------------------------------------------------ 监护与签署

    def checkin_attendant(self, permit_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        permit = self._permit_row(permit_id)
        self._require_live(permit)
        required = self._read_json(permit["required_attendant_roles_json"])
        role = payload["attendant_role"]
        if role not in required:
            raise ConflictError(f"该作业类型不需要 {role} 监护角色", context={"required_roles": required})
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            connection.execute(
                "INSERT INTO work_permit_attendants(work_permit_id,person,attendant_role,checked_in_at,created_at) "
                "VALUES(?,?,?,?,?) ON CONFLICT(work_permit_id,person,attendant_role) "
                "DO UPDATE SET checked_in_at=excluded.checked_in_at,checked_out_at=NULL",
                (permit_id, payload["person"], role, now, now),
            )
            return self.permit_detail(permit_id, connection)

    def checkout_attendant(self, permit_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        permit = self._permit_row(permit_id)
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            cursor = connection.execute(
                "UPDATE work_permit_attendants SET checked_out_at=? "
                "WHERE work_permit_id=? AND person=? AND attendant_role=? AND checked_out_at IS NULL",
                (now, permit_id, payload["person"], payload["attendant_role"]),
            )
            if cursor.rowcount == 0:
                raise ConflictError("没有在岗的该监护人员记录")
            return self.permit_detail(permit_id, connection)

    def add_signoff(self, permit_id: int, signoff_role: str, payload: dict[str, Any]) -> dict[str, Any]:
        permit = self._permit_row(permit_id)
        required = self._read_json(permit["required_signoff_roles_json"])
        if signoff_role not in required:
            raise ConflictError(f"该作业类型不需要 {signoff_role} 签署", context={"required_roles": required})
        if permit["state"] != "draft":
            raise ConflictError("只有草稿许可可以补充签署")
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            existing = connection.execute(
                "SELECT id FROM work_permit_signoffs WHERE work_permit_id=? AND signoff_role=? AND signer=?",
                (permit_id, signoff_role, payload["signer"]),
            ).fetchone()
            if existing is None:
                connection.execute(
                    "INSERT INTO work_permit_signoffs(work_permit_id,signoff_role,signer,signed_at) VALUES(?,?,?,?)",
                    (permit_id, signoff_role, payload["signer"], now),
                )
                self._event(connection, permit_id, "signoff", payload["actor"], f"{signoff_role} 签署完成", {"signoff_role": signoff_role, "signer": payload["signer"]}, now)
            return self.permit_detail(permit_id, connection)

    # ------------------------------------------------------------------ 签发与生命周期

    def issue_permit(self, permit_id: int, actor: str) -> dict[str, Any]:
        permit = self._permit_row(permit_id)
        if permit["state"] != "draft":
            raise ConflictError("只有草稿许可可以签发", context={"state": permit["state"]})
        missing_roles = self._missing_signoff_roles(permit)
        if missing_roles:
            raise ConflictError("所需责任人尚未完成多人签署", context={"missing_signoff_roles": missing_roles})
        now = to_storage(self.clock.now())
        reasons = self._issue_reasons(permit, now)
        if reasons:
            raise ConflictError("签发前置条件未满足", context={"reasons": reasons})
        with transaction(immediate=True) as connection:
            cursor = connection.execute(
                "UPDATE work_permits SET state='issued',issued_by=?,issued_at=?,last_event_at=?,updated_at=?,version=version+1 WHERE id=? AND state='draft'",
                (actor, now, now, now, permit_id),
            )
            if cursor.rowcount != 1:
                raise ConflictError("许可状态已变化，签发未执行")
            self._event(connection, permit_id, "issued", actor, "前置条件核验通过，许可签发", {}, now)
            return self.permit_detail(permit_id, connection)

    def start_work(self, permit_id: int, actor: str, reason: str) -> dict[str, Any]:
        permit = self._permit_row(permit_id)
        if permit["state"] != "issued":
            raise ConflictError("只有已签发且未开工的许可可以开工", context={"state": permit["state"]})
        now = to_storage(self.clock.now())
        reasons = self._runtime_reasons(permit, now)
        if reasons:
            raise ConflictError("开工前置条件未满足", context={"reasons": reasons})
        with transaction(immediate=True) as connection:
            self._transition(connection, permit_id, expected="issued", target="active", now=now)
            self._event(connection, permit_id, "work_started", actor, reason, {}, now)
            return self.permit_detail(permit_id, connection)

    def pause_work(self, permit_id: int, actor: str, reason: str) -> dict[str, Any]:
        permit = self._permit_row(permit_id)
        if permit["state"] != "active":
            raise ConflictError("只有施工中的许可可以暂停", context={"state": permit["state"]})
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            cursor = connection.execute(
                "UPDATE work_permits SET state='suspended',suspend_kind='manual',updated_at=?,version=version+1 WHERE id=? AND state='active'",
                (now, permit_id),
            )
            if cursor.rowcount != 1:
                raise ConflictError("许可状态已变化，暂停未执行")
            self._event(connection, permit_id, "paused", actor, reason, {}, now)
            return self.permit_detail(permit_id, connection)

    def resume_work(self, permit_id: int, actor: str, reason: str) -> dict[str, Any]:
        permit = self._permit_row(permit_id)
        if permit["state"] != "suspended":
            raise ConflictError("只有挂起的许可可以复工", context={"state": permit["state"]})
        now = to_storage(self.clock.now())
        reasons = self._runtime_reasons(permit, now)
        if reasons:
            raise ConflictError("复工前置条件仍未满足", context={"reasons": reasons})
        with transaction(immediate=True) as connection:
            cursor = connection.execute(
                "UPDATE work_permits SET state='active',suspend_kind='',suspend_reasons_json='[]',updated_at=?,version=version+1 "
                "WHERE id=? AND state='suspended'",
                (now, permit_id),
            )
            if cursor.rowcount != 1:
                raise ConflictError("许可状态已变化，复工未执行")
            self._event(connection, permit_id, "resumed", actor, reason, {"previous_suspend_kind": permit["suspend_kind"]}, now)
            return self.permit_detail(permit_id, connection)

    def complete_work(self, permit_id: int, actor: str, reason: str) -> dict[str, Any]:
        permit = self._permit_row(permit_id)
        if permit["state"] != "active":
            raise ConflictError("只有施工中的许可可以收工", context={"state": permit["state"]})
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            self._transition(connection, permit_id, expected="active", target="completed", now=now)
            self._event(connection, permit_id, "work_completed", actor, reason, {}, now)
            return self.permit_detail(permit_id, connection)

    def revoke_permit(self, permit_id: int, actor: str, reason: str) -> dict[str, Any]:
        permit = self._permit_row(permit_id)
        if permit["state"] == "revoked":
            return self.permit_detail(permit_id)
        if permit["state"] == "draft":
            raise ConflictError("草稿许可尚未签发，无需紧急撤销")
        if permit["state"] == "completed":
            raise ConflictError("已收工许可不能紧急撤销")
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            cursor = connection.execute(
                "UPDATE work_permits SET state='revoked',updated_at=?,version=version+1 WHERE id=? AND state NOT IN ('completed','revoked')",
                (now, permit_id),
            )
            if cursor.rowcount != 1:
                raise ConflictError("许可状态已变化，撤销未执行")
            self._event(connection, permit_id, "revoked", actor, reason, {"previous_state": permit["state"]}, now)
            return self.permit_detail(permit_id, connection)

    def evaluate_permits(self, actor: str = "work-permit-supervisor") -> dict[str, Any]:
        """固定时钟下扫描施工中许可；前置条件失效则自动挂起，已完成工作保留。"""
        now = to_storage(self.clock.now())
        suspended: list[dict[str, Any]] = []
        with transaction(immediate=True) as connection:
            rows = connection.execute("SELECT id FROM work_permits WHERE state='active' ORDER BY id").fetchall()
            for row in rows:
                # 事务内重新读取并核验，避免扫描期间前置条件或状态发生变化
                permit = connection.execute("SELECT * FROM work_permits WHERE id=?", (row["id"],)).fetchone()
                if permit is None or permit["state"] != "active":
                    continue
                reasons = self._runtime_reasons(permit, now, connection)
                if not reasons:
                    continue
                connection.execute(
                    "UPDATE work_permits SET state='suspended',suspend_kind='auto',suspend_reasons_json=?,updated_at=?,version=version+1 WHERE id=?",
                    (self._json(reasons), now, permit["id"]),
                )
                self._event(connection, permit["id"], "auto_suspended", actor, "前置条件失效，许可证自动挂起", {"reasons": reasons}, now)
                suspended.append({"work_permit_id": permit["id"], "code": permit["code"], "reasons": reasons})
        return {"suspended": suspended, "checked_at": now}

    # ------------------------------------------------------------------ 查询

    def list_permits(self, temple_code: str | None = None, state: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT p.*,n.code AS temple_code FROM work_permits p JOIN temple_sites n ON n.id=p.temple_id"
        clauses: list[str] = []
        params: list[Any] = []
        if temple_code:
            clauses.append("n.code=?")
            params.append(temple_code)
        if state:
            clauses.append("p.state=?")
            params.append(state)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY p.id DESC"
        return [self._permit_dict(row) for row in self.connection.execute(sql, params).fetchall()]

    def permit_detail(self, permit_id: int, connection: sqlite3.Connection | None = None) -> dict[str, Any]:
        connection = connection or self.connection
        row = connection.execute(
            "SELECT p.*,n.code AS temple_code,n.name AS temple_name FROM work_permits p "
            "JOIN temple_sites n ON n.id=p.temple_id WHERE p.id=?",
            (permit_id,),
        ).fetchone()
        if row is None:
            raise NotFoundError("施工许可不存在")
        result = self._permit_dict(row)
        result["halls"] = [
            {"hall_id": item["hall_id"], "code": item["code"], "name": item["name"], "status": item["status"]}
            for item in connection.execute(
                "SELECT h.id AS hall_id,h.code,h.name,h.status FROM work_permit_halls ph JOIN worship_halls h ON h.id=ph.hall_id "
                "WHERE ph.work_permit_id=? ORDER BY h.visit_order,h.id",
                (permit_id,),
            ).fetchall()
        ]
        result["shifts"] = [dict(item) for item in connection.execute(
            "SELECT seq,shift_code,starts_at,ends_at FROM work_permit_shifts WHERE work_permit_id=? ORDER BY seq",
            (permit_id,),
        ).fetchall()]
        result["responsibles"] = [item["person"] for item in connection.execute(
            "SELECT person FROM work_permit_responsibles WHERE work_permit_id=? ORDER BY seq",
            (permit_id,),
        ).fetchall()]
        result["attendants"] = [dict(item) for item in connection.execute(
            "SELECT person,attendant_role,checked_in_at,checked_out_at FROM work_permit_attendants WHERE work_permit_id=? ORDER BY attendant_role,person",
            (permit_id,),
        ).fetchall()]
        result["signoffs"] = [dict(item) for item in connection.execute(
            "SELECT signoff_role,signer,signed_at FROM work_permit_signoffs WHERE work_permit_id=? ORDER BY signed_at,id",
            (permit_id,),
        ).fetchall()]
        result["events"] = self._events(connection, permit_id)
        result["decision"] = self._decision(row, to_storage(self.clock.now()))
        return result

    # ------------------------------------------------------------------ 前置条件核验

    def _issue_reasons(self, permit: sqlite3.Row, now: str) -> list[dict[str, Any]]:
        reasons: list[dict[str, Any]] = []
        hall_ids = self._permit_hall_ids(permit["id"])
        halls = [self.repository.hall_by_id(hall_id) for hall_id in hall_ids]
        for hall in halls:
            if hall is not None and hall["status"] != "active":
                reasons.append(self._reason("hall_not_open", f"殿堂 {hall['code']} 当前未开放", {"hall_code": hall["code"], "status": hall["status"]}))
        closures = self.connection.execute(
            "SELECT w.code,w.starts_at,w.ends_at,s.code AS hall_code FROM hall_closure_windows w "
            "LEFT JOIN worship_halls s ON s.id=w.hall_id "
            "WHERE w.temple_id=? AND w.state IN ('scheduled','active') AND (w.hall_id IS NULL OR w.hall_id IN (%s)) "
            "AND w.starts_at<? AND w.ends_at>? ORDER BY w.id" % ",".join("?" for _ in hall_ids),
            [permit["temple_id"], *hall_ids, permit["valid_until"], permit["valid_from"]],
        ).fetchall() if hall_ids else []
        for item in closures:
            reasons.append(self._reason(
                "hall_closure",
                f"封闭窗口 {item['code']} 与许可有效期冲突",
                {"closure_code": item["code"], "hall_code": item["hall_code"], "starts_at": item["starts_at"], "ends_at": item["ends_at"]},
            ))
        ceremonies = self.connection.execute(
            "SELECT c.code,c.starts_at,c.ends_at,s.code AS hall_code FROM ceremony_schedules c "
            "LEFT JOIN worship_halls s ON s.id=c.hall_id "
            "WHERE c.temple_id=? AND c.state IN ('scheduled','active') AND (c.hall_id IS NULL OR c.hall_id IN (%s)) "
            "AND c.starts_at<? AND c.ends_at>? ORDER BY c.id" % ",".join("?" for _ in hall_ids),
            [permit["temple_id"], *hall_ids, permit["valid_until"], permit["valid_from"]],
        ).fetchall() if hall_ids else []
        for item in ceremonies:
            reasons.append(self._reason(
                "conflicting_ceremony",
                f"法会 {item['code']} 与许可有效期冲突",
                {"ceremony_code": item["code"], "hall_code": item["hall_code"], "starts_at": item["starts_at"], "ends_at": item["ends_at"]},
            ))
        reasons.extend(self._attendant_reasons(permit))
        reasons.extend(self._incident_reasons(permit, hall_ids))
        return reasons

    def _runtime_reasons(self, permit: sqlite3.Row, now: str, connection: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
        connection = connection or self.connection
        repository = TempleRepository(connection)
        reasons: list[dict[str, Any]] = []
        hall_ids = self._permit_hall_ids(permit["id"], connection)
        if not (permit["valid_from"] <= now < permit["valid_until"]):
            reasons.append(self._reason("outside_validity", "当前时间不在许可有效期内", {"valid_from": permit["valid_from"], "valid_until": permit["valid_until"]}))
        shift = self._current_shift(permit["id"], now, connection)
        if shift is None:
            next_shift = self._next_shift(permit["id"], now, connection)
            reasons.append(self._reason("outside_shift", "当前不在许可的有效班次内", {"next_shift": next_shift}))
        closures = connection.execute(
            "SELECT w.code,s.code AS hall_code FROM hall_closure_windows w LEFT JOIN worship_halls s ON s.id=w.hall_id "
            "WHERE w.temple_id=? AND w.state IN ('scheduled','active') AND (w.hall_id IS NULL OR w.hall_id IN (%s)) "
            "AND w.starts_at<=? AND w.ends_at>? ORDER BY w.id" % ",".join("?" for _ in hall_ids),
            [permit["temple_id"], *hall_ids, now, now],
        ).fetchall() if hall_ids else []
        for item in closures:
            reasons.append(self._reason("hall_closure", f"殿堂封闭窗口 {item['code']} 生效中", {"closure_code": item["code"], "hall_code": item["hall_code"]}))
        ceremonies = connection.execute(
            "SELECT c.code,s.code AS hall_code FROM ceremony_schedules c LEFT JOIN worship_halls s ON s.id=c.hall_id "
            "WHERE c.temple_id=? AND c.state IN ('scheduled','active') AND (c.hall_id IS NULL OR c.hall_id IN (%s)) "
            "AND c.starts_at<=? AND c.ends_at>? ORDER BY c.id" % ",".join("?" for _ in hall_ids),
            [permit["temple_id"], *hall_ids, now, now],
        ).fetchall() if hall_ids else []
        for item in ceremonies:
            reasons.append(self._reason("conflicting_ceremony", f"冲突法会 {item['code']} 正在进行", {"ceremony_code": item["code"], "hall_code": item["hall_code"]}))
        for hall_id in hall_ids:
            hall = repository.hall_by_id(hall_id)
            if hall is not None and hall["status"] != "active":
                reasons.append(self._reason("hall_not_open", f"殿堂 {hall['code']} 当前未开放", {"hall_code": hall["code"], "status": hall["status"]}))
        reasons.extend(self._attendant_reasons(permit, connection))
        reasons.extend(self._incident_reasons(permit, hall_ids, connection))
        return reasons

    def _attendant_reasons(self, permit: sqlite3.Row, connection: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
        connection = connection or self.connection
        required = self._read_json(permit["required_attendant_roles_json"])
        placeholders = ",".join("?" for _ in required) or "''"
        rows = connection.execute(
            f"SELECT attendant_role,COUNT(*) AS total FROM work_permit_attendants WHERE work_permit_id=? "
            f"AND attendant_role IN ({placeholders}) AND checked_in_at IS NOT NULL AND checked_out_at IS NULL GROUP BY attendant_role",
            [permit["id"], *required],
        ).fetchall()
        present = {row["attendant_role"] for row in rows}
        return [
            self._reason("attendant_missing", f"缺少在岗的 {role} 监护", {"attendant_role": role})
            for role in required
            if role not in present
        ]

    def _incident_reasons(self, permit: sqlite3.Row, hall_ids: list[int], connection: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
        connection = connection or self.connection
        repository = TempleRepository(connection)
        severities = self._read_json(permit["blocking_incident_severities_json"])
        if not severities or not hall_ids:
            return []
        placeholders_halls = ",".join("?" for _ in hall_ids)
        placeholders_sev = ",".join("?" for _ in severities)
        rows = connection.execute(
            f"SELECT id,severity,hall_id FROM safety_incidents WHERE temple_id=? AND state IN ('open','mitigating') "
            f"AND severity IN ({placeholders_sev}) AND (hall_id IS NULL OR hall_id IN ({placeholders_halls})) ORDER BY id",
            [permit["temple_id"], *severities, *hall_ids],
        ).fetchall()
        reasons = []
        for row in rows:
            hall = repository.hall_by_id(row["hall_id"]) if row["hall_id"] else None
            reasons.append(self._reason(
                "open_safety_incident",
                f"存在未解决的 {row['severity']} 级安全隐患 #{row['id']}",
                {"safety_incident_id": row["id"], "severity": row["severity"], "hall_code": hall["code"] if hall else None},
            ))
        return reasons

    def _decision(self, permit: sqlite3.Row, now: str) -> dict[str, Any]:
        state = permit["state"]
        current_shift = self._current_shift(permit["id"], now)
        in_validity = permit["valid_from"] <= now < permit["valid_until"]
        runtime_reasons = self._runtime_reasons(permit, now) if state in {"issued", "active", "suspended"} else []

        if state == "draft":
            missing = self._missing_signoff_roles(permit)
            allowed = False
            action_required = "issue" if not missing else "signoff"
            explanations = [self._reason("not_issued", "许可尚未签发", {"missing_signoff_roles": missing})]
            explanations.extend(self._issue_reasons(permit, now))
        elif state == "issued":
            allowed = False
            action_required = "start" if not runtime_reasons else "resolve_preconditions"
            explanations = list(runtime_reasons)
            if not runtime_reasons:
                explanations.append(self._reason("not_started", "许可已签发且前置条件满足，等待开工", {}))
            else:
                explanations.insert(0, self._reason("precondition_failed", "开工前置条件未满足", {}))
        elif state == "active":
            allowed = not runtime_reasons
            action_required = None if allowed else "resolve_preconditions"
            explanations = [] if allowed else [self._reason("precondition_failed", "施工前置条件已失效", {})] + runtime_reasons
        elif state == "suspended":
            allowed = False
            action_required = "resume" if not runtime_reasons else "resolve_preconditions"
            label = "manual_suspension" if permit["suspend_kind"] == "manual" else "auto_suspension"
            message = "许可处于人工暂停状态" if permit["suspend_kind"] == "manual" else "许可因前置条件失效被自动挂起"
            explanations = [self._reason(label, message, {"suspend_reasons": self._read_json(permit["suspend_reasons_json"])})]
            explanations.extend(runtime_reasons)
        elif state == "completed":
            allowed = False
            action_required = None
            explanations = [self._reason("completed", "许可已收工，已完成工作予以保留")]
        else:  # revoked
            allowed = False
            action_required = None
            explanations = [self._reason("revoked", "许可已被紧急撤销")]
        return {
            "construction_allowed": allowed,
            "action_required": action_required,
            "in_validity": in_validity,
            "in_shift": current_shift is not None,
            "current_shift": current_shift,
            "checked_at": now,
            "reasons": explanations,
        }

    # ------------------------------------------------------------------ 辅助

    def _missing_signoff_roles(self, permit: sqlite3.Row) -> list[str]:
        required = self._read_json(permit["required_signoff_roles_json"])
        rows = self.connection.execute(
            "SELECT DISTINCT signoff_role FROM work_permit_signoffs WHERE work_permit_id=?",
            (permit["id"],),
        ).fetchall()
        present = {row["signoff_role"] for row in rows}
        return [role for role in required if role not in present]

    def _current_shift(self, permit_id: int, now: str, connection: sqlite3.Connection | None = None) -> dict[str, Any] | None:
        connection = connection or self.connection
        row = connection.execute(
            "SELECT seq,shift_code,starts_at,ends_at FROM work_permit_shifts "
            "WHERE work_permit_id=? AND starts_at<=? AND ends_at>? ORDER BY seq LIMIT 1",
            (permit_id, now, now),
        ).fetchone()
        return dict(row) if row else None

    def _next_shift(self, permit_id: int, now: str, connection: sqlite3.Connection | None = None) -> dict[str, Any] | None:
        connection = connection or self.connection
        row = connection.execute(
            "SELECT seq,shift_code,starts_at,ends_at FROM work_permit_shifts "
            "WHERE work_permit_id=? AND ends_at>? ORDER BY starts_at,seq LIMIT 1",
            (permit_id, now),
        ).fetchone()
        return dict(row) if row else None

    def _permit_hall_ids(self, permit_id: int, connection: sqlite3.Connection | None = None) -> list[int]:
        connection = connection or self.connection
        return [row["hall_id"] for row in connection.execute(
            "SELECT hall_id FROM work_permit_halls WHERE work_permit_id=? ORDER BY hall_id",
            (permit_id,),
        ).fetchall()]

    def _permit_halls(self, temple_id: int, codes: list[str]) -> list[sqlite3.Row]:
        result = []
        for code in codes:
            hall = self.repository.hall_by_code(temple_id, code)
            if hall is None:
                raise NotFoundError(f"许可适用殿堂不存在：{code}")
            result.append(hall)
        return result

    def _normalize_shifts(self, shifts: list[dict[str, Any]], valid_from: str, valid_until: str) -> list[dict[str, Any]]:
        normalized = []
        for shift in shifts:
            starts_at = self._time(shift["starts_at"], "班次开始时间")
            ends_at = self._time(shift["ends_at"], "班次结束时间")
            if ends_at <= starts_at:
                raise ValidationError(f"班次 {shift['shift_code']} 结束时间必须晚于开始时间")
            if starts_at < valid_from or ends_at > valid_until:
                raise ValidationError(f"班次 {shift['shift_code']} 超出许可有效期（允许跨日班次）")
            normalized.append({"shift_code": shift["shift_code"], "starts_at": starts_at, "ends_at": ends_at})
        normalized.sort(key=lambda item: (item["starts_at"], item["ends_at"]))
        return normalized

    def _permit_row(self, permit_id: int) -> sqlite3.Row:
        row = self.connection.execute("SELECT * FROM work_permits WHERE id=?", (permit_id,)).fetchone()
        if row is None:
            raise NotFoundError("施工许可不存在")
        return row

    @staticmethod
    def _require_live(permit: sqlite3.Row) -> None:
        if permit["state"] in _TERMINAL_STATES:
            raise ConflictError("终态许可不能变更监护安排", context={"state": permit["state"]})

    @staticmethod
    def _transition(connection: sqlite3.Connection, permit_id: int, *, expected: str, target: str, now: str) -> None:
        cursor = connection.execute(
            f"UPDATE work_permits SET state=?,last_event_at=?,updated_at=?,version=version+1 WHERE id=? AND state=?",
            (target, now, now, permit_id, expected),
        )
        if cursor.rowcount != 1:
            row = connection.execute("SELECT state FROM work_permits WHERE id=?", (permit_id,)).fetchone()
            raise ConflictError("许可状态已变化，动作未执行", context={"state": row["state"] if row else None})

    def _temple(self, code: str) -> sqlite3.Row:
        row = self.repository.temple_by_code(code)
        if row is None:
            raise NotFoundError("寺院不存在")
        return row

    @staticmethod
    def _time(value: str, label: str) -> str:
        try:
            return to_storage(from_storage(value))
        except (TypeError, ValueError) as exc:
            raise ValidationError(f"{label}格式不正确") from exc

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)

    def _read_json(self, value: str) -> Any:
        return json.loads(value or "null")

    @staticmethod
    def _reason(code: str, message: str, context: dict[str, Any] | None = None) -> dict[str, Any]:
        return {"code": code, "message": message, "context": context or {}}

    def _permit_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        for field_name in ("risk_items", "required_attendant_roles", "required_signoff_roles", "blocking_incident_severities", "suspend_reasons"):
            column = f"{field_name}_json"
            if column in result:
                result[field_name] = self._read_json(result.pop(column))
        return result

    @staticmethod
    def _event(
        connection: sqlite3.Connection,
        permit_id: int,
        event_type: str,
        actor: str,
        reason: str,
        detail: dict[str, Any],
        now: str,
    ) -> None:
        seq_row = connection.execute("SELECT COALESCE(MAX(seq),0)+1 AS next_seq FROM work_permit_events WHERE work_permit_id=?", (permit_id,)).fetchone()
        connection.execute(
            "INSERT INTO work_permit_events(work_permit_id,seq,event_type,actor,reason,detail_json,created_at) VALUES(?,?,?,?,?,?,?)",
            (permit_id, int(seq_row["next_seq"]), event_type, actor, reason, json.dumps(detail, ensure_ascii=False, sort_keys=True), now),
        )

    @staticmethod
    def _events(connection: sqlite3.Connection, permit_id: int) -> list[dict[str, Any]]:
        result = []
        for row in connection.execute(
            "SELECT seq,event_type,actor,reason,detail_json,created_at FROM work_permit_events WHERE work_permit_id=? ORDER BY seq,id",
            (permit_id,),
        ).fetchall():
            item = dict(row)
            item["detail"] = json.loads(item.pop("detail_json"))
            result.append(item)
        return result
