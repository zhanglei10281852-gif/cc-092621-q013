from __future__ import annotations

from typing import Any, Literal

WorkType = Literal["timber_repair", "temporary_power", "hot_work"]
SignoffRole = Literal["safety_officer", "fire_warden", "electrician"]
AttendantRole = Literal["safety_monitor", "fire_watch", "electrician"]

WORK_TYPES: tuple[str, ...] = ("timber_repair", "temporary_power", "hot_work")
SIGNOFF_ROLES: tuple[str, ...] = ("safety_officer", "fire_warden", "electrician")
ATTENDANT_ROLES: tuple[str, ...] = ("safety_monitor", "fire_watch", "electrician")

# 与作业类型绑定的风险清单目录：许可创建时按 work_type 冻结快照，
# 之后目录如何调整都不影响已签发的许可证。
RISK_CATALOG_VERSION = "2026-09-01"

RISK_CATALOG: dict[str, dict[str, Any]] = {
    "timber_repair": {
        "label": "木构修补",
        "risk_items": [
            {"code": "timber-dust", "name": "木屑粉尘积聚", "severity": "minor"},
            {"code": "structural-support", "name": "梁架构件拆卸失稳", "severity": "major"},
            {"code": "incense-ember", "name": "香火余烬引燃木料", "severity": "major"},
            {"code": "falling-object", "name": "高处坠物伤及信众", "severity": "major"},
        ],
        "required_attendant_roles": ["safety_monitor"],
        "required_signoff_roles": ["safety_officer"],
        "blocking_incident_severities": ["major", "critical"],
    },
    "temporary_power": {
        "risk_items": [
            {"code": "cable-damage", "name": "临时线缆破损漏电", "severity": "major"},
            {"code": "overload", "name": "配电回路过载", "severity": "major"},
            {"code": "wet-environment", "name": "潮湿环境触电", "severity": "critical"},
        ],
        "required_attendant_roles": ["safety_monitor", "electrician"],
        "required_signoff_roles": ["safety_officer", "electrician"],
        "blocking_incident_severities": ["major", "critical"],
    },
    "hot_work": {
        "risk_items": [
            {"code": "spark-ignition", "name": "焊割火花引燃木构与帷幔", "severity": "critical"},
            {"code": "residual-fire", "name": "收工后暗火复燃", "severity": "critical"},
            {"code": "gas-cylinder", "name": "气瓶受热或倾倒", "severity": "major"},
            {"code": "smoke-ventilation", "name": "烟气积聚影响疏散", "severity": "major"},
        ],
        "required_attendant_roles": ["safety_monitor", "fire_watch"],
        "required_signoff_roles": ["safety_officer", "fire_warden"],
        "blocking_incident_severities": ["minor", "major", "critical"],
    },
}


def catalog_snapshot(work_type: str) -> dict[str, Any]:
    if work_type not in RISK_CATALOG:
        raise ValueError(f"未知作业类型：{work_type}")
    source = RISK_CATALOG[work_type]
    return {
        "catalog_version": RISK_CATALOG_VERSION,
        "risk_items": [dict(item) for item in source["risk_items"]],
        "required_attendant_roles": list(source["required_attendant_roles"]),
        "required_signoff_roles": list(source["required_signoff_roles"]),
        "blocking_incident_severities": list(source["blocking_incident_severities"]),
    }
