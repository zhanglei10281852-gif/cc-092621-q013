from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from app.core.clock import Clock, SystemClock, from_storage, to_storage
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.database import get_connection, transaction
from app.temple.schema import ensure_temple_schema

WORK_TYPES = ("timber_repair", "temporary_power", "hot_work")
SHIFTS = ("day", "evening", "night")
# 可以在签发环节会签的角色（签发人始终必需）
SIGNOFF_ROLES = ("issuer", "safety_officer", "fire_warden")

# 作业类型在签发时冻结的风险清单与最低监护要求
DEFAULT_TEMPLATES: dict[str, dict[str, Any]] = {
    "timber_repair": {
        "name": "木构修补作业",
        "risk_items": [
            "木构件承重失效风险",
            "木屑粉尘堆积",
            "高处作业坠落",
            "工具机械伤害",
        ],
        "required_monitors": ["safety_officer"],
        "requires_hall_closure": True,
        "blocks_incense_hours": False,
    },
    "temporary_power": {
        "name": "临时用电作业",
        "risk_items": [
            "临时线缆过载与短路",
            "触电伤害",
            "配电箱受潮",
            "线缆绊倒通道",
        ],
        "required_monitors": ["safety_officer", "electrician"],
        "requires_hall_closure": True,
        "blocks_incense_hours": False,
    },
    "hot_work": {
        "name": "动火作业",
        "risk_items": [
            "明火引燃木构与织物",
            "焊渣飞溅复燃",
            "烟气聚集",
            "灭火器材不足",
        ],
        "required_monitors": ["safety_officer", "fire_warden"],
        "requires_hall_closure": True,
        "blocks_incense_hours": True,
    },
}

# 各班次的寺院本地起止时刻（半开区间 [start, end)），三个班次恰好覆盖整日
SHIFT_HOURS: dict[str, tuple[int, int]] = {
    "day": (8, 16),
    "evening": (16, 24),
    "night": (0, 8),
}

# 允许执行动作的许可状态
ACTION_STATES: dict[str, set[str]] = {
    "start": {"issued"},
    "suspend": {"in_progress"},
    "resume": {"suspended", "auto_suspended"},
    "finish": {"in_progress", "suspended", "auto_suspended"},
}
# 动作写入时间线的事件类型
ACTION_EVENTS = {
    "start": "started",
    "suspend": "suspended",
    "resume": "resumed",
    "finish": "completed",
}
# 动作后进入的许可状态
ACTION_RESULTS = {
    "start": "in_progress",
    "suspend": "suspended",
    "resume": "in_progress",
    "finish": "completed",
}


class WorkPermitService:
    """面向修缮工序的施工许可：签发核验、动作时间线与前置条件自动挂起。"""

    def __init__(self, connection: sqlite3.Connection | None = None, clock: Clock | None = None) -> None:
        self.connection = connection or get_connection()
        ensure_temple_schema(self.connection)
        self.clock = clock or SystemClock()
        self._seed_templates()

    # ------------------------------------------------------------------ 模板

    def list_templates(self) -> list[dict[str, Any]]:
        rows = self.connection.execute("SELECT * FROM work_permit_templates ORDER BY work_type").fetchall()
        return [self._template(row) for row in rows]

    def _seed_templates(self) -> None:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            for work_type, config in DEFAULT_TEMPLATES.items():
                existing = connection.execute("SELECT id FROM work_permit_templates WHERE work_type=?", (work_type,)).fetchone()
                if existing is None:
                    connection.execute(
                        "INSERT INTO work_permit_templates(work_type,name,risk_items_json,required_monitors_json,requires_hall_closure,blocks_incense_hours,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                        (
                            work_type,
                            config["name"],
                            json.dumps(config["risk_items"], ensure_ascii=False),
                            json.dumps(config["required_monitors"], ensure_ascii=False),
                            1 if config["requires_hall_closure"] else 0,
                            1 if config["blocks_incense_hours"] else 0,
                            now,
                            now,
                        ),
                    )

    @staticmethod
    def _template(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["risk_items"] = json.loads(result.pop("risk_items_json"))
        result["required_monitors"] = json.loads(result.pop("required_monitors_json"))
        result["requires_hall_closure"] = bool(result["requires_hall_closure"])
        result["blocks_incense_hours"] = bool(result["blocks_incense_hours"])
        return result

    # ------------------------------------------------------------------ 法会

    def create_dharma_service(self, payload: dict[str, Any]) -> dict[str, Any]:
        temple = self._temple(payload["temple_code"])
        hall_id = None
        if payload.get("hall_code"):
            hall_id = self._hall(temple["id"], payload["hall_code"])["id"]
        try:
            starts_at = to_storage(from_storage(payload["starts_at"]))
            ends_at = to_storage(from_storage(payload["ends_at"]))
        except (TypeError, ValueError) as exc:
            raise ValidationError("法会时间格式不正确") from exc
        if ends_at <= starts_at:
            raise ValidationError("法会结束时间必须晚于开始时间")
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            try:
                cursor = connection.execute(
                    "INSERT INTO dharma_services(temple_id,hall_id,code,name,starts_at,ends_at,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (temple["id"], hall_id, payload["code"], payload["name"], starts_at, ends_at, payload["actor"], now, now),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError("法会编码已存在") from exc
            dharma_id = cursor.lastrowid
        # 新法会可能与进行中许可冲突：立即重评并自动挂起
        self.refresh_permits(payload["actor"])
        return self.dharma_service_detail(dharma_id)

    def cancel_dharma_service(self, dharma_service_id: int, actor: str) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM dharma_services WHERE id=?", (dharma_service_id,)).fetchone()
        if row is None:
            raise NotFoundError("法会不存在")
        if row["state"] != "cancelled":
            with transaction(immediate=True) as connection:
                now = to_storage(self.clock.now())
                connection.execute("UPDATE dharma_services SET state='cancelled',updated_at=? WHERE id=?", (now, dharma_service_id))
            # 法会取消可能解除冲突前置条件，重评所有进行中许可（自动挂起不会自行恢复，仍须人工复工）
            self.refresh_permits(actor)
        return self.dharma_service_detail(dharma_service_id)

    def dharma_service_detail(self, dharma_service_id: int) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT d.*,n.code AS temple_code,s.code AS hall_code FROM dharma_services d "
            "JOIN temple_sites n ON n.id=d.temple_id LEFT JOIN worship_halls s ON s.id=d.hall_id WHERE d.id=?",
            (dharma_service_id,),
        ).fetchone()
        if row is None:
            raise NotFoundError("法会不存在")
        return dict(row)

    def list_dharma_services(self, temple_code: str | None = None) -> list[dict[str, Any]]:
        sql = (
            "SELECT d.*,n.code AS temple_code,s.code AS hall_code FROM dharma_services d "
            "JOIN temple_sites n ON n.id=d.temple_id LEFT JOIN worship_halls s ON s.id=d.hall_id"
        )
        params: list[Any] = []
        if temple_code:
            sql += " WHERE n.code=?"
            params.append(temple_code)
        sql += " ORDER BY d.starts_at,d.id"
        return [dict(row) for row in self.connection.execute(sql, params).fetchall()]

    # ------------------------------------------------------------------ 许可

    def create_permit(self, payload: dict[str, Any]) -> dict[str, Any]:
        temple = self._temple(payload["temple_code"])
        work_type = payload["work_type"]
        if work_type not in WORK_TYPES:
            raise ValidationError("不支持的作业类型")
        template = self._template(self.connection.execute(
            "SELECT * FROM work_permit_templates WHERE work_type=?", (work_type,),
        ).fetchone())
        hall_codes = payload.get("hall_codes") or []
        if not hall_codes:
            raise ValidationError("施工许可必须至少指定一个适用殿堂")
        if len(hall_codes) != len(set(hall_codes)):
            raise ValidationError("适用殿堂不能重复")
        hall_ids = [int(self._hall(temple["id"], code)["id"]) for code in hall_codes]
        shift_label = payload.get("shift_label", "day")
        if shift_label not in SHIFT_HOURS:
            raise ValidationError("班次必须是 day、evening 或 night")
        shift_date, shift_start, shift_end, tz_name = self._resolve_shift(
            payload.get("shift_date"), shift_label, payload.get("timezone", temple["timezone"]), self.clock,
        )
        monitors = self._normalize_monitors(payload.get("monitors"), template["required_monitors"])
        responsible_party = str(payload.get("responsible_party", "")).strip()
        if not responsible_party:
            raise ValidationError("必须指定许可责任人")
        # 风险清单按作业类型冻结：只允许逐项确认，不允许增删或改写
        risk_items = list(template["risk_items"])
        acknowledged = payload.get("risk_items_acknowledged")
        if acknowledged is not None and sorted(acknowledged) != sorted(risk_items):
            raise ValidationError("风险清单由作业类型冻结，必须逐项确认且不得增删")
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            try:
                cursor = connection.execute(
                    "INSERT INTO work_permits(temple_id,code,work_type,name,template_id,risk_items_json,required_monitors_json,"
                    "shift_date,shift_label,shift_tz,shift_start,shift_end,responsible_party,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        temple["id"], payload["code"], work_type, payload["name"], template["id"],
                        json.dumps(risk_items, ensure_ascii=False, sort_keys=True),
                        json.dumps(monitors, ensure_ascii=False, sort_keys=True),
                        shift_date, shift_label, tz_name, shift_start, shift_end,
                        responsible_party, now, now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError("施工许可编码已存在") from exc
            permit_id = cursor.lastrowid
            for hall_id in hall_ids:
                connection.execute("INSERT INTO work_permit_halls(work_permit_id,hall_id) VALUES(?,?)", (permit_id, hall_id))
            self._event(connection, permit_id, "created", payload.get("actor", responsible_party), {"work_type": work_type}, now)
        return self.permit_detail(permit_id)

    def issue_permit(self, permit_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        """签发前核验殿堂封闭、冲突法会、所需监护与未解决隐患；多人按角色会签。

        会签逐人落库（不因后续核验失败而抹除）；最后一个必需角色签署时触发统一核验，
        核验通过许可才转为 issued。
        """
        actor = payload["actor"]
        role = payload.get("role", "issuer")
        if role not in SIGNOFF_ROLES:
            raise ValidationError("会签角色必须是 issuer、safety_officer 或 fire_warden")
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            permit = connection.execute("SELECT * FROM work_permits WHERE id=?", (permit_id,)).fetchone()
            if permit is None:
                raise NotFoundError("施工许可不存在")
            if permit["state"] == "issued":
                # 重复签发：确定地返回已签发结果，不再追加签署或事件
                return self.permit_detail(permit_id, connection)
            if permit["state"] != "draft":
                raise ConflictError("只有草稿许可可以签发")
            inserted = self._add_signoff(connection, permit_id, role, actor, now)
            missing_roles = self._missing_signoff_roles(connection, permit)
            if inserted:
                self._event(connection, permit_id, "signoff_collected", actor, {"role": role, "missing": missing_roles}, now)
            if missing_roles:
                return self.permit_detail(permit_id, connection)
        # 会签齐全：在独立事务中核验并签发，核验失败不会抹掉任何已完成签署
        with transaction(immediate=True) as connection:
            permit = connection.execute("SELECT * FROM work_permits WHERE id=?", (permit_id,)).fetchone()
            if permit is None:
                raise NotFoundError("施工许可不存在")
            if self._missing_signoff_roles(connection, permit):
                return self.permit_detail(permit_id, connection)
            evaluation = self._evaluate_conditions(permit, at=now, connection=connection)
            failed = [item for item in evaluation["checks"] if not item["satisfied"]]
            if failed:
                raise ConflictError("签发前置条件未满足", context={"failed_checks": failed, "missing_signatures": []})
            connection.execute("UPDATE work_permits SET state='issued',issued_by=?,issued_at=?,updated_at=? WHERE id=?", (actor, now, now, permit_id))
            self._event(connection, permit_id, "issued", actor, {"checks": evaluation["checks"]}, now)
            return self.permit_detail(permit_id, connection)

    def act_permit(self, permit_id: int, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        """开工、暂停、复工、收工；固定时钟下重复动作返回同一结果，不重复写入时间线。"""
        if action not in ACTION_STATES:
            raise ValidationError("不支持的许可动作")
        actor = payload["actor"]
        reason = str(payload.get("reason", "")).strip()
        now_value = self.clock.now()
        now = to_storage(now_value)
        with transaction(immediate=True) as connection:
            row = connection.execute("SELECT * FROM work_permits WHERE id=?", (permit_id,)).fetchone()
            if row is None:
                raise NotFoundError("施工许可不存在")
            # 目标状态已达成：重复动作幂等返回，时间线不追加
            if row["state"] == ACTION_RESULTS[action]:
                return self.permit_detail(permit_id, connection)
            if row["state"] not in ACTION_STATES[action]:
                raise ConflictError(f"许可当前状态 {row['state']} 不能执行 {action} 动作")
            if action in {"start", "resume"}:
                self._require_within_shift(row, now_value)
                evaluation = self._evaluate_conditions(row, at=now, connection=connection)
                failed = [item for item in evaluation["checks"] if not item["satisfied"]]
                if failed:
                    raise ConflictError("施工前置条件未满足，禁止开工或复工", context={"failed_checks": failed})
            else:
                evaluation = None
            connection.execute("UPDATE work_permits SET suspension_reason='',updated_at=? WHERE id=?", (now, permit_id))
            connection.execute("UPDATE work_permits SET state=?,updated_at=? WHERE id=?", (ACTION_RESULTS[action], now, permit_id))
            detail: dict[str, Any] = {"reason": reason}
            if action in {"start", "resume"}:
                detail["checks"] = evaluation["checks"]
            self._event(connection, permit_id, ACTION_EVENTS[action], actor, detail, now)
            return self.permit_detail(permit_id, connection)

    def revoke_permit(self, permit_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        """紧急撤销：任何非终态许可都可立即撤销，已完成工作只追加事件，不抹除。"""
        reason = str(payload.get("reason", "")).strip()
        if len(reason) < 2:
            raise ValidationError("紧急撤销必须说明原因")
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            row = connection.execute("SELECT * FROM work_permits WHERE id=?", (permit_id,)).fetchone()
            if row is None:
                raise NotFoundError("施工许可不存在")
            if row["state"] in {"revoked", "completed"}:
                raise ConflictError("终态许可不能撤销")
            connection.execute("UPDATE work_permits SET state='revoked',suspension_reason='',updated_at=? WHERE id=?", (now, permit_id))
            self._event(connection, permit_id, "revoked", payload["actor"], {"reason": reason, "emergency": True}, now)
            return self.permit_detail(permit_id, connection)

    def refresh_permits(self, actor: str = "permit-guardian") -> dict[str, Any]:
        """前置条件失效时自动挂起进行中许可；自动挂起只能由人工核验后复工。"""
        now = to_storage(self.clock.now())
        suspended: list[int] = []
        with transaction(immediate=True) as connection:
            rows = connection.execute("SELECT * FROM work_permits WHERE state='in_progress' ORDER BY id").fetchall()
            for row in rows:
                evaluation = self._evaluate_conditions(row, at=now, connection=connection)
                failed = [item for item in evaluation["checks"] if not item["satisfied"]]
                if failed:
                    reasons = ";".join(item["code"] for item in failed)
                    cursor = connection.execute(
                        "UPDATE work_permits SET state='auto_suspended',suspension_reason=?,updated_at=? WHERE id=? AND state='in_progress'",
                        (reasons, now, row["id"]),
                    )
                    if cursor.rowcount:
                        self._event(connection, row["id"], "auto_suspended", actor, {"failed_checks": failed}, now)
                        suspended.append(row["id"])
        return {"auto_suspended": suspended, "checked_at": now}

    def permit_explanation(self, permit_id: int) -> dict[str, Any]:
        """说明当前为何允许或禁止施工。"""
        permit = self._permit_row(permit_id)
        now = to_storage(self.clock.now())
        evaluation = self._evaluate_conditions(permit, at=now)
        failed_codes = [item["code"] for item in evaluation["checks"] if not item["satisfied"]]
        within_shift = permit["shift_start"] <= now < permit["shift_end"]
        actionable = {"start", "resume"} & {
            action for action, states in ACTION_STATES.items() if permit["state"] in states
        }
        can_transition_to_work = bool(actionable) and within_shift and not failed_codes
        can_work_now = permit["state"] == "in_progress" and not failed_codes
        allowed_actions: list[str] = []
        for action, states in ACTION_STATES.items():
            if permit["state"] not in states:
                continue
            if action in {"start", "resume"} and (not within_shift or failed_codes):
                continue
            allowed_actions.append(action)
        if permit["state"] not in {"revoked", "completed"}:
            allowed_actions.append("revoke")
        reasons = [item["message"] for item in evaluation["checks"] if not item["satisfied"]]
        if not reasons and not within_shift and permit["state"] in {"issued", "suspended", "auto_suspended"}:
            reasons.append("当前不在许可有效班次内")
        if not reasons and permit["state"] == "in_progress":
            reasons = ["全部前置条件持续满足"]
        elif not reasons and permit["state"] == "issued":
            reasons = ["前置条件已满足，可在班次内开工"]
        return {
            "permit_id": permit_id,
            "state": permit["state"],
            "suspension_reason": permit["suspension_reason"],
            "checked_at": now,
            "within_shift": within_shift,
            "shift_window": {"start": permit["shift_start"], "end": permit["shift_end"], "label": permit["shift_label"]},
            "can_work_now": bool(can_work_now),
            "can_start_or_resume": bool(can_transition_to_work),
            "verdict": "允许施工" if (can_work_now or can_transition_to_work) else "禁止施工",
            "reasons": reasons,
            "checks": evaluation["checks"],
            "allowed_actions": sorted(set(allowed_actions)),
        }

    def permit_detail(self, permit_id: int, connection: sqlite3.Connection | None = None) -> dict[str, Any]:
        connection = connection or self.connection
        permit = connection.execute(
            "SELECT p.*,n.code AS temple_code,n.name AS temple_name FROM work_permits p "
            "JOIN temple_sites n ON n.id=p.temple_id WHERE p.id=?",
            (permit_id,),
        ).fetchone()
        if permit is None:
            raise NotFoundError("施工许可不存在")
        result = dict(permit)
        result["risk_items"] = json.loads(result.pop("risk_items_json"))
        result["required_monitors"] = json.loads(result.pop("required_monitors_json"))
        result["halls"] = [
            {"hall_id": row["hall_id"], "code": row["code"], "name": row["name"]}
            for row in connection.execute(
                "SELECT x.hall_id,h.code,h.name FROM work_permit_halls x "
                "JOIN worship_halls h ON h.id=x.hall_id WHERE x.work_permit_id=? ORDER BY h.visit_order,h.id",
                (permit_id,),
            ).fetchall()
        ]
        result["signoffs"] = [dict(row) for row in connection.execute(
            "SELECT role,actor,signed_at FROM work_permit_signoffs WHERE work_permit_id=? ORDER BY id",
            (permit_id,),
        ).fetchall()]
        result["events"] = self._events(connection, permit_id)
        return result

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
        sql += " ORDER BY p.shift_start,p.id"
        return [dict(row) for row in self.connection.execute(sql, params).fetchall()]

    # ------------------------------------------------------------- 前置条件

    def _evaluate_conditions(self, permit: sqlite3.Row, *, at: str, connection: sqlite3.Connection | None = None) -> dict[str, Any]:
        connection = connection or self.connection
        hall_ids = [row[0] for row in connection.execute(
            "SELECT hall_id FROM work_permit_halls WHERE work_permit_id=? ORDER BY id", (permit["id"],),
        ).fetchall()]
        template = self._template(connection.execute(
            "SELECT * FROM work_permit_templates WHERE id=?", (permit["template_id"],),
        ).fetchone())
        checks: list[dict[str, Any]] = []

        # 1) 殿堂封闭：作业班次必须被同殿堂（或全寺）的封闭窗口完整覆盖
        closure_satisfied = True
        closure_codes: dict[str, str] = {}
        if template["requires_hall_closure"]:
            for hall_id in hall_ids:
                window = self._covering_closure(connection, permit["temple_id"], hall_id, permit["shift_start"], permit["shift_end"])
                if window is None:
                    closure_satisfied = False
                else:
                    closure_codes[str(hall_id)] = window["code"]
        checks.append({
            "code": "hall_closure",
            "name": "殿堂封闭",
            "satisfied": closure_satisfied,
            "message": "适用殿堂在作业班次内已全部封闭" if closure_satisfied else "存在未被封闭窗口完整覆盖的适用殿堂",
            "closures": closure_codes,
        })

        # 2) 冲突法会：法会时间窗与班次窗不得相交（全寺法会对所有殿堂生效）
        conflicts: list[dict[str, Any]] = []
        seen: set[int] = set()
        for hall_id in hall_ids:
            rows = connection.execute(
                "SELECT d.id,d.code,d.name,d.starts_at,d.ends_at FROM dharma_services d "
                "WHERE d.temple_id=? AND d.state='scheduled' AND (d.hall_id IS ? OR d.hall_id IS NULL) "
                "AND d.starts_at<? AND d.ends_at>?",
                (permit["temple_id"], hall_id, permit["shift_end"], permit["shift_start"]),
            ).fetchall()
            for row in rows:
                if row["id"] in seen:
                    continue
                seen.add(row["id"])
                conflicts.append({
                    "id": row["id"], "code": row["code"], "name": row["name"], "hall_id": hall_id,
                    "starts_at": row["starts_at"], "ends_at": row["ends_at"],
                })
        checks.append({
            "code": "dharma_conflict",
            "name": "冲突法会",
            "satisfied": not conflicts,
            "message": "作业班次与已排法会不冲突" if not conflicts else "作业班次与排定法会时间冲突",
            "conflicts": conflicts,
        })

        # 3) 所需监护：模板冻结的监护角色必须在许可监护清单中全部有人承担
        monitors = json.loads(permit["required_monitors_json"])
        assigned_roles = {item["role"] for item in monitors}
        missing_monitors = [role for role in template["required_monitors"] if role not in assigned_roles]
        monitor_satisfied = not missing_monitors
        checks.append({
            "code": "monitoring",
            "name": "现场监护",
            "satisfied": monitor_satisfied,
            "message": f"所需监护已到位：{', '.join(template['required_monitors'])}" if monitor_satisfied else f"缺少监护角色：{', '.join(missing_monitors)}",
            "required": template["required_monitors"],
            "assigned": monitors,
        })

        # 4) 未解决隐患：适用殿堂上不得存在 open / mitigating 隐患
        placeholders = ",".join("?" for _ in hall_ids)
        incident_rows = connection.execute(
            f"SELECT i.id,i.severity,h.code AS hall_code FROM safety_incidents i "
            f"LEFT JOIN worship_halls h ON h.id=i.hall_id "
            f"WHERE i.temple_id=? AND i.state IN ('open','mitigating') AND i.hall_id IN ({placeholders}) ORDER BY i.id",
            [permit["temple_id"], *hall_ids],
        ).fetchall()
        incidents = [{"id": row["id"], "severity": row["severity"], "hall_code": row["hall_code"]} for row in incident_rows]
        checks.append({
            "code": "open_incidents",
            "name": "未解决隐患",
            "satisfied": not incidents,
            "message": "适用殿堂没有未解决的安全隐患" if not incidents else "适用殿堂存在未解决的安全隐患",
            "incidents": incidents,
        })

        # 5) 香火隔离：动火等作业类型必须凭封闭窗口与香火开放时段隔离
        if template["blocks_incense_hours"]:
            checks.append({
                "code": "incense_isolation",
                "name": "香火隔离",
                "satisfied": closure_satisfied,
                "message": "动火作业殿堂已与香火开放时段隔离" if closure_satisfied else "动火作业必须先封闭殿堂、隔离香火",
            })

        # 6) 有效班次：班次未结束（允许提前签发；进行中跨日到班次结束即失效）
        shift_satisfied = at < permit["shift_end"]
        checks.append({
            "code": "shift_valid",
            "name": "有效班次",
            "satisfied": bool(shift_satisfied),
            "message": "许可仍在有效班次内" if shift_satisfied else "许可班次已经结束",
            "shift": {"start": permit["shift_start"], "end": permit["shift_end"], "label": permit["shift_label"]},
        })

        return {"satisfied": all(item["satisfied"] for item in checks), "checks": checks}

    @staticmethod
    def _covering_closure(connection: sqlite3.Connection, temple_id: int, hall_id: int, starts_at: str, ends_at: str) -> sqlite3.Row | None:
        """返回完整覆盖作业班次的封闭窗口：优先同殿堂，其次全寺。"""
        row = connection.execute(
            "SELECT * FROM hall_closure_windows WHERE temple_id=? AND hall_id=? AND state IN ('scheduled','active') "
            "AND starts_at<=? AND ends_at>=? ORDER BY starts_at,id LIMIT 1",
            (temple_id, hall_id, starts_at, ends_at),
        ).fetchone()
        if row is not None:
            return row
        return connection.execute(
            "SELECT * FROM hall_closure_windows WHERE temple_id=? AND hall_id IS NULL AND state IN ('scheduled','active') "
            "AND starts_at<=? AND ends_at>=? ORDER BY starts_at,id LIMIT 1",
            (temple_id, starts_at, ends_at),
        ).fetchone()

    # ------------------------------------------------------------------ 辅助

    @staticmethod
    def _resolve_shift(shift_date: str | None, shift_label: str, tz_name: str, clock: Clock) -> tuple[str, str, str, str]:
        try:
            tz = ZoneInfo(tz_name)
        except Exception as exc:
            raise ValidationError("时区不受支持") from exc
        if shift_date:
            try:
                day = datetime.strptime(shift_date, "%Y-%m-%d").date()
            except ValueError as exc:
                raise ValidationError("班次日期格式必须为 YYYY-MM-DD") from exc
        else:
            day = clock.now().astimezone(tz).date()
        start_hour, end_hour = SHIFT_HOURS[shift_label]
        start_local = datetime(day.year, day.month, day.day, start_hour, tzinfo=tz)
        duration_hours = (end_hour - start_hour) % 24 or 8
        end_local = start_local + timedelta(hours=duration_hours)
        return day.isoformat(), to_storage(start_local), to_storage(end_local), tz_name

    def _require_within_shift(self, permit: sqlite3.Row, now_value: datetime) -> None:
        now_text = to_storage(now_value)
        if not (permit["shift_start"] <= now_text < permit["shift_end"]):
            raise ConflictError("当前不在许可有效班次内（跨日班次不得开工）", context={
                "shift_start": permit["shift_start"], "shift_end": permit["shift_end"], "checked_at": now_text,
            })

    @staticmethod
    def _normalize_monitors(value: Any, required_roles: list[str]) -> list[dict[str, str]]:
        if not isinstance(value, list) or not value:
            raise ValidationError("必须提供现场监护清单")
        result: list[dict[str, str]] = []
        seen_roles: set[str] = set()
        for item in value:
            if not isinstance(item, dict) or "role" not in item or "name" not in item:
                raise ValidationError("监护条目必须包含 role 与 name")
            role = str(item["role"]).strip()
            name = str(item["name"]).strip()
            if not role or not name:
                raise ValidationError("监护角色与责任人不能为空")
            if role in seen_roles:
                raise ValidationError(f"监护角色不能重复：{role}")
            seen_roles.add(role)
            result.append({"role": role, "name": name})
        missing = [role for role in required_roles if role not in seen_roles]
        if missing:
            raise ValidationError(f"缺少作业类型要求的监护角色：{', '.join(missing)}")
        return result

    @staticmethod
    def _add_signoff(connection: sqlite3.Connection, permit_id: int, role: str, actor: str, now: str) -> bool:
        """记录一次角色会签，返回是否新增。同一人重复签署确定地忽略，换人签署被拒绝。"""
        try:
            connection.execute(
                "INSERT INTO work_permit_signoffs(work_permit_id,role,actor,signed_at) VALUES(?,?,?,?)",
                (permit_id, role, actor, now),
            )
            return True
        except sqlite3.IntegrityError as exc:
            existing = connection.execute(
                "SELECT actor FROM work_permit_signoffs WHERE work_permit_id=? AND role=?", (permit_id, role),
            ).fetchone()
            if existing["actor"] != actor:
                raise ConflictError(f"角色 {role} 已由 {existing['actor']} 签署，不能换人重复签署") from exc
            return False

    def _missing_signoff_roles(self, connection: sqlite3.Connection, permit: sqlite3.Row) -> list[str]:
        template = self._template(connection.execute(
            "SELECT * FROM work_permit_templates WHERE id=?", (permit["template_id"],),
        ).fetchone())
        required = {"issuer", *[role for role in template["required_monitors"] if role in SIGNOFF_ROLES]}
        signed = {row["role"] for row in connection.execute(
            "SELECT role FROM work_permit_signoffs WHERE work_permit_id=?", (permit["id"],),
        ).fetchall()}
        return [role for role in SIGNOFF_ROLES if role in required and role not in signed]

    def _permit_row(self, permit_id: int) -> sqlite3.Row:
        row = self.connection.execute("SELECT * FROM work_permits WHERE id=?", (permit_id,)).fetchone()
        if row is None:
            raise NotFoundError("施工许可不存在")
        return row

    def _temple(self, code: str) -> sqlite3.Row:
        row = self.connection.execute("SELECT * FROM temple_sites WHERE code=?", (code,)).fetchone()
        if row is None:
            raise NotFoundError("寺院不存在")
        return row

    def _hall(self, temple_id: int, code: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM worship_halls WHERE temple_id=? AND code=?", (temple_id, code),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"适用殿堂不存在：{code}")
        return row

    @staticmethod
    def _event(connection: sqlite3.Connection, permit_id: int, event_type: str, actor: str, detail: dict[str, Any], now: str) -> None:
        connection.execute(
            "INSERT INTO work_permit_events(work_permit_id,event_type,actor,detail_json,created_at) VALUES(?,?,?,?,?)",
            (permit_id, event_type, actor, json.dumps(detail, ensure_ascii=False, sort_keys=True), now),
        )

    @staticmethod
    def _events(connection: sqlite3.Connection, permit_id: int) -> list[dict[str, Any]]:
        rows = connection.execute(
            "SELECT id,event_type,actor,detail_json,created_at FROM work_permit_events WHERE work_permit_id=? ORDER BY id",
            (permit_id,),
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["detail"] = json.loads(item.pop("detail_json"))
            result.append(item)
        return result
