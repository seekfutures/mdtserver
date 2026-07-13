"""Generic MDT admission rule engine based on business_rules.

The disease group decides which rules to run through database JSON.
This module owns how those rules are converted and executed.
"""

from __future__ import annotations

import re
from datetime import datetime

try:
    from business_rules import run_all
    from business_rules.actions import BaseActions, rule_action
    from business_rules.fields import FIELD_TEXT
    from business_rules.variables import (
        BaseVariables,
        numeric_rule_variable,
        string_rule_variable,
    )
except ImportError:  # pragma: no cover - deployment dependency guard
    run_all = None

    class BaseActions:
        pass

    class BaseVariables:
        pass

    FIELD_TEXT = "text"

    def rule_action(*args, **kwargs):
        def decorator(func):
            return func
        return decorator

    def numeric_rule_variable(*args, **kwargs):
        def decorator(func):
            return func
        return decorator

    def string_rule_variable(*args, **kwargs):
        def decorator(func):
            return func
        return decorator


DEFAULT_LARGE_DAYS = 999
DEFAULT_LARGE_NUMBER = 999.0


class MedicalQualityVariables(BaseVariables):
    """The same variable surface used by the client-side rule prototype."""

    def __init__(self, patient_data):
        self.data = dict(patient_data or {})

    @numeric_rule_variable(label="是否有胸部CT")
    def has_ct(self):
        return as_bool_int(self.data.get("has_ct"))

    @numeric_rule_variable(label="CT层厚mm")
    def ct_slice_thickness(self):
        return as_float(self.data.get("ct_slice_thickness"), DEFAULT_LARGE_NUMBER)

    @numeric_rule_variable(label="CT距今天数")
    def ct_days_ago(self):
        return as_int(self.data.get("ct_days_ago"), DEFAULT_LARGE_DAYS)

    @numeric_rule_variable(label="肺结节大小mm")
    def nodule_size(self):
        return as_float(self.data.get("nodule_size"), 0)

    @numeric_rule_variable(label="是否有肿瘤标志物")
    def has_tumor_markers(self):
        return as_bool_int(self.data.get("has_tumor_markers"))

    @numeric_rule_variable(label="CEA")
    def cea_level(self):
        return as_float(self.data.get("cea_level"), 0)

    @numeric_rule_variable(label="CYFRA21-1")
    def cyfra211_level(self):
        return as_float(self.data.get("cyfra211_level"), 0)

    @numeric_rule_variable(label="检验距今天数")
    def lab_days_ago(self):
        return as_int(self.data.get("lab_days_ago"), DEFAULT_LARGE_DAYS)

    @numeric_rule_variable(label="是否有历史CT")
    def has_history_ct(self):
        return as_bool_int(self.data.get("has_history_ct"))

    @numeric_rule_variable(label="是否有3个月内薄层CT")
    def has_recent_hrct(self):
        if "has_recent_hrct" in self.data:
            return as_bool_int(self.data.get("has_recent_hrct"))
        return 1 if (
            self.has_ct()
            and self.ct_slice_thickness() <= 1.25
            and self.ct_days_ago() <= 90
        ) else 0

    @numeric_rule_variable(label="是否有1个月内肿瘤标志物报告")
    def has_recent_tumor_markers(self):
        if "has_recent_tumor_markers" in self.data:
            return as_bool_int(self.data.get("has_recent_tumor_markers"))
        return 1 if self.has_tumor_markers() and self.lab_days_ago() <= 30 else 0

    @numeric_rule_variable(label="是否有历史影像对比")
    def has_history_compare(self):
        if "has_history_compare" in self.data:
            return as_bool_int(self.data.get("has_history_compare"))
        return self.has_history_ct()

    @string_rule_variable(label="诊断文本")
    def diagnosis_text(self):
        return str(self.data.get("diagnosis_text") or "")

    @string_rule_variable(label="结节密度")
    def nodule_density(self):
        return str(self.data.get("nodule_density") or "")


class QualityControlActions(BaseActions):
    """Collect REJECT/WARN actions triggered by business_rules."""

    def __init__(self, feedback_container):
        self.feedback = feedback_container
        self.feedback["action_type"] = "PASS"
        self.feedback["messages"] = []

    @rule_action(params={"error_msg": FIELD_TEXT})
    def trigger_reject(self, error_msg):
        self.feedback["action_type"] = "REJECT"
        self.feedback["messages"].append(error_msg)

    @rule_action(params={"warn_msg": FIELD_TEXT})
    def trigger_warning(self, warn_msg):
        if self.feedback["action_type"] != "REJECT":
            self.feedback["action_type"] = "WARN"
        self.feedback["messages"].append(warn_msg)


def build_patient_rule_facts(patient, diagnoses, pacs_reports, lis_items):
    """Build the variable dictionary consumed by engine_conditions."""
    facts = {
        "has_ct": 0,
        "ct_slice_thickness": DEFAULT_LARGE_NUMBER,
        "ct_days_ago": DEFAULT_LARGE_DAYS,
        "has_tumor_markers": 0,
        "lab_days_ago": DEFAULT_LARGE_DAYS,
        "has_history_ct": 0,
    }

    ct_reports = []
    for report in pacs_reports or []:
        text = " ".join(
            str(report.get(key) or "")
            for key in ("exam_type", "exam_item", "finding", "conclusion")
        ).upper()
        if "CT" not in text and "断层" not in text:
            continue
        ct_reports.append(report)
        facts["has_ct"] = 1
        facts["ct_days_ago"] = min(
            facts["ct_days_ago"], days_ago(report.get("exam_time"))
        )
        thickness = extract_ct_slice_thickness(text)
        if thickness is not None:
            facts["ct_slice_thickness"] = min(facts["ct_slice_thickness"], thickness)

    if len(ct_reports) > 1:
        facts["has_history_ct"] = 1

    for item in lis_items or []:
        item_name = str(item.get("item_name") or "").upper()
        if any(token in item_name for token in ("CEA", "CYFRA", "NSE", "SCC", "CA125", "肿瘤")):
            facts["has_tumor_markers"] = 1
            facts["lab_days_ago"] = min(
                facts["lab_days_ago"], days_ago(item.get("report_time"))
            )

    facts["has_recent_hrct"] = 1 if (
        facts["has_ct"]
        and facts["ct_slice_thickness"] <= 1.25
        and facts["ct_days_ago"] <= 90
    ) else 0
    facts["has_recent_tumor_markers"] = 1 if (
        facts["has_tumor_markers"] and facts["lab_days_ago"] <= 30
    ) else 0
    facts["has_history_compare"] = facts["has_history_ct"]
    facts["diagnosis_text"] = build_diagnosis_text(patient, diagnoses)
    return facts


def evaluate_admission_rules(rules, facts):
    """Evaluate database-driven rules with business_rules."""
    if run_all is None:
        raise RuntimeError("缺少 business-rules 依赖，请先安装 requirements.txt")
    normalized_facts = dict(facts or {})
    print("给规则引擎的数据",normalized_facts)
    business_rules = build_business_rule_list(rules)
    result = {
        "action_type": "PASS",
        "messages": [],
        "matched_rules": [],
        "facts": normalized_facts,
    }
    run_all(
        rule_list=business_rules,
        defined_variables=MedicalQualityVariables(normalized_facts),
        defined_actions=QualityControlActions(result),
    )
    triggered_messages = set(result["messages"])
    print("triggered_messages",triggered_messages)
    for rule in rules or []:
        message = rule_action_message(rule)
        if message in triggered_messages:
            result["matched_rules"].append(
                {
                    "rule_id": rule.get("rule_id"),
                    "rule_name": rule.get("rule_name") or "",
                    "action_type": rule_action_type(rule),
                    "message": message,
                }
            )
    print("规则引擎返回结果",result)
    return result


def build_business_rule_list(rules):
    """Convert database rule rows to the JSON format required by business_rules."""
    rule_list = []
    for rule in rules or []:
        if "conditions" in rule and "actions" in rule:
            rule_list.append(rule)
            continue

        action_type = str(rule.get("action_type") or "REJECT").upper()
        action_name = "trigger_warning" if action_type == "WARN" else "trigger_reject"
        param_name = "warn_msg" if action_type == "WARN" else "error_msg"
        message = rule_action_message(rule)
        rule_list.append(
            {
                "conditions": rule.get("engine_conditions") or {},
                "actions": [
                    {
                        "name": action_name,
                        "params": {param_name: message},
                    }
                ],
            }
        )
    return rule_list


def rule_action_message(rule):
    if rule.get("actions"):
        params = (rule.get("actions")[0] or {}).get("params") or {}
        return str(
            params.get("error_msg")
            or params.get("warn_msg")
            or rule.get("rule_name")
            or "规则校验未通过"
        )
    return str(rule.get("action_message") or rule.get("rule_name") or "规则校验未通过")


def rule_action_type(rule):
    if rule.get("actions"):
        action_name = str((rule.get("actions")[0] or {}).get("name") or "")
        return "WARN" if action_name == "trigger_warning" else "REJECT"
    return str(rule.get("action_type") or "REJECT").upper()


def as_bool_int(value):
    if isinstance(value, str):
        return 1 if value.strip().lower() in {"1", "true", "yes", "有", "是"} else 0
    return 1 if value else 0


def as_int(value, default):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def as_float(value, default):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def days_ago(value):
    parsed = parse_datetime(value)
    if parsed is None:
        return DEFAULT_LARGE_DAYS
    return max(0, (datetime.now() - parsed).days)


def parse_datetime(value):
    if isinstance(value, datetime):
        return value
    if value is None:
        return None
    text = str(value).strip().replace("T", " ")
    candidates = (text[:19], text[:10], text)
    for candidate in candidates:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d %H:%M:%S", "%Y/%m/%d"):
            try:
                return datetime.strptime(candidate, fmt)
            except ValueError:
                continue
    return None


def extract_ct_slice_thickness(text):
    patterns = [
        r"层厚\s*[:：]?\s*(\d+(?:\.\d+)?)\s*MM",
        r"(\d+(?:\.\d+)?)\s*MM\s*层厚",
        r"THICKNESS\s*[:：]?\s*(\d+(?:\.\d+)?)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return float(match.group(1))
    return None


def build_diagnosis_text(patient, diagnoses):
    parts = [
        patient.get("main_diagnosis_code") or "",
        patient.get("main_diagnosis") or "",
    ]
    for diagnosis in diagnoses or []:
        parts.extend(
            [
                diagnosis.get("diagnosis_code") or "",
                diagnosis.get("diagnosis_name") or "",
            ]
        )
    return " ".join(parts).upper()
