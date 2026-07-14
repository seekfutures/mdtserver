import json

from flask import Blueprint, g, request

from auth import token_required
from extensions import get_db
from log import setup_logger
from responses import make_response
from utils.db import generate_id, rows_to_dicts
from utils.rule_engine import build_patient_rule_facts, evaluate_admission_rules

logger_system = setup_logger("logger_system")

bp = Blueprint("clinical", __name__)


def read_lob(value):
    return value.read() if hasattr(value, "read") else value


def load_json_value(value, default=None):
    if default is None:
        default = {}
    text = read_lob(value)
    if not text:
        return default
    if isinstance(text, (dict, list)):
        return text
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return default


def dump_json_value(value):
    return json.dumps(value or {}, ensure_ascii=False, default=str)


def clean_text_value(value):
    text = str(value or "").strip()
    return "" if text.lower() in {"", "null", "none", "nan"} else text


def fetch_schedule_capacity_for_update(cursor, schedule_id):
    if not schedule_id:
        return 0
    schedule = query_one(
        cursor,
        """
        select capacity
          from mdt_schedule
         where schedule_id = :schedule_id
         for update
        """,
        {"schedule_id": schedule_id},
    )
    return int(schedule.get("capacity") or 0) if schedule else 0


def next_queue_order(cursor, schedule_id):
    if not schedule_id:
        return None
    row = query_one(
        cursor,
        """
        select nvl(max(queue_order), 0) + 1 as queue_order
          from mdt_clinical_application
         where schedule_id = :schedule_id
        """,
        {"schedule_id": schedule_id},
    )
    return int(row.get("queue_order") or 1)


def rebalance_application_queue(cursor, schedule_id):
    capacity = fetch_schedule_capacity_for_update(cursor, schedule_id)
    if not schedule_id or capacity <= 0:
        return []

    applications = query_list(
        cursor,
        """
        select application_id,
               patient_name,
               group_name,
               applicant_id,
               appointment_type,
               schedule_no,
               status
          from mdt_clinical_application
         where schedule_id = :schedule_id
           and upper(nvl(status, '-')) not in
               ('DRAFT', 'REJECTED', 'CANCELLED', 'WAITLIST_FAILED')
         order by nvl(queue_order, 999999999), created_at, application_id
        """,
        {"schedule_id": schedule_id},
    )
    changed = []
    for index, application in enumerate(applications, start=1):
        is_waitlist = index > capacity
        schedule_no = 100 + index - capacity if is_waitlist else index
        current_status = str(application.get("status") or "").upper()
        new_status = "WAITLIST" if is_waitlist else current_status
        if not is_waitlist and current_status == "WAITLIST":
            new_status = "SUBMITTED"
        cursor.execute(
            """
            update mdt_clinical_application
               set schedule_no = :schedule_no,
                   appointment_type = :appointment_type,
                   status = :status,
                   updated_at = sysdate
             where application_id = :application_id
            """,
            {
                "application_id": application["application_id"],
                "schedule_no": schedule_no,
                "appointment_type": "WAITLIST" if is_waitlist else "REGULAR",
                "status": new_status,
            },
        )
        changed.append(
            {
                "application_id": application["application_id"],
                "patient_name": application.get("patient_name") or "",
                "group_name": application.get("group_name") or "",
                "applicant_id": application.get("applicant_id") or "",
                "old_schedule_no": application.get("schedule_no"),
                "old_appointment_type": application.get("appointment_type") or "",
                "old_status": current_status,
                "schedule_no": schedule_no,
                "status": new_status,
                "appointment_type": "WAITLIST" if is_waitlist else "REGULAR",
            }
        )
    return changed


@bp.route("/clinical/mdt-disease-groups", methods=["GET"])
@token_required
def list_mdt_disease_groups():
    """Query disease groups available for clinical MDT application."""
    _db, cursor = get_db()
    try:
        cursor.execute(
            """
            select d.discipline_id,
                   d.discipline_name,
                   g.group_id,
                   g.group_name,
                   g.rule_enabled,
                   g.sort_no
              from mdt_disease_group g
              join mdt_discipline_group d on d.discipline_id = g.discipline_id
             where nvl(g.enabled_flag, '1') = '1'
               and nvl(d.enabled_flag, '1') = '1'
             order by d.sort_no, g.sort_no, g.group_name
            """
        )
        return make_response(
            res_code="ok",
            res_message="查询成功",
            output={"items": rows_to_dicts(cursor, cursor.fetchall())},
            status_code=200,
        )
    except Exception as exc:
        logger_system.error(f"Database error during MDT group query: {str(exc)}")
        return make_response(
            res_code="error",
            res_message="MDT病种组查询失败",
            output=str(exc),
            status_code=500,
        )


@bp.route("/clinical/patients", methods=["GET"])
@token_required
def search_patients():
    """Fuzzy query patients from Oracle hospital source tables."""
    _db, cursor = get_db()
    keyword = (request.args.get("keyword") or "").strip()
    try:
        limit = int(request.args.get("limit") or 30)
    except ValueError:
        limit = 30
    limit = max(1, min(limit, 100))

    if not keyword:
        return make_response(
            res_code="error",
            res_message="参数错误",
            output="Missing keyword",
            status_code=400,
        )

    try:
        cursor.execute(
            """
            select patient_id,
                   patient_name,
                   sex,
                   age,
                   bed_no,
                   visit_no,
                   medical_record_no,
                   allergy_history,
                   main_diagnosis,
                   main_diagnosis_code,
                   birth_date
              from (
                    select patient_id,
                           patient_name,
                           sex,
                           age,
                           null as bed_no,
                           visit_no,
                           medical_record_no,
                           null as allergy_history,
                           main_diagnosis,
                           main_diagnosis_code,
                           birth_date,
                           row_number() over (
                               partition by patient_id
                               order by visit_time desc nulls last, visit_no desc nulls last
                           ) as rn
                      from (
                            select p.patient_id,
                                   p.name as patient_name,
                                   decode(to_char(p.sex), '1', '男', '2', '女', to_char(p.sex)) as sex,
                                   floor(months_between(sysdate, p.birthday) / 12) as age,
                                   v.visit_number as visit_no,
                                   p.social_no as medical_record_no,
                                   to_char(p.birthday, 'YYYY-MM-DD') as birth_date,
                                   v.visit_time,
                                   d.value_st_txt as main_diagnosis,
                                   d.value_code as main_diagnosis_code
                              from mdt_a_patient_mi p
                              left join (
                                    select patient_id,
                                           visit_number,
                                           visit_date as visit_time
                                      from mdt_mz_visit_table
                                    union all
                                    select patient_id,
                                           visit_number,
                                           dis_date as visit_time
                                      from mdt_zy_actpatient
                              ) v on v.patient_id = p.patient_id
                              left join (
                                    select patient_id,
                                           visit_number,
                                           value_code,
                                           value_st_txt,
                                           row_number() over (
                                               partition by patient_id, visit_number
                                               order by aut_time desc nulls last
                                           ) as rn
                                      from mdt_obs_dx
                              ) d on d.patient_id = v.patient_id
                                 and d.visit_number = v.visit_number
                                 and d.rn = 1
                             where p.patient_id like :keyword
                                or p.name like :keyword
                                or p.social_no like :keyword
                                or v.visit_number like :keyword
                           )
                     order by patient_name, patient_id
                   )
             where rn = 1
               and rownum <= :limit
            """,
            {"keyword": f"%{keyword}%", "limit": limit},
        )
        return make_response(
            res_code="ok",
            res_message="查询成功",
            output={"items": rows_to_dicts(cursor, cursor.fetchall())},
            status_code=200,
        )
    except Exception as exc:
        logger_system.error(f"Database error during patient query: {str(exc)}")
        return make_response(
            res_code="error",
            res_message="患者查询失败",
            output=str(exc),
            status_code=500,
        )


@bp.route("/clinical/patients/<patient_id>/panorama", methods=["GET"])
@token_required
def get_patient_panorama(patient_id):
    """Return patient panorama data from Oracle hospital source tables."""
    _db, cursor = get_db()
    patient_id = (patient_id or "").strip()
    visit_no = (request.args.get("visit_no") or "").strip()
    if not patient_id:
        return make_response(
            res_code="error",
            res_message="参数错误",
            output="Missing patient_id",
            status_code=400,
        )

    try:
        basic = query_patient_basic(cursor, patient_id, visit_no or None)
        if not basic:
            return make_response(
                res_code="error",
                res_message="患者不存在",
                output=f"patient_id {patient_id} does not exist",
                status_code=404,
            )
        visit_no = visit_no or str(basic.get("visit_no") or "")
        visits = query_patient_visits(cursor, patient_id)
        diagnoses = query_patient_diagnoses(cursor, patient_id, visit_no or None)
        pacs_reports = query_patient_pacs(cursor, patient_id, visit_no or None)
        lis_items = query_patient_lis(cursor, patient_id, visit_no or None)
        timeline = build_patient_timeline(visits, diagnoses, pacs_reports, lis_items)

        return make_response(
            res_code="ok",
            res_message="查询成功",
            output={
                "basic": basic,
                "visits": visits,
                "diagnoses": diagnoses,
                "timeline": timeline,
                "emr_records": [],
                "pacs_reports": pacs_reports,
                "lis_items": lis_items,
            },
            status_code=200,
        )
    except Exception as exc:
        logger_system.error(f"Database error during panorama query: {str(exc)}")
        return make_response(
            res_code="error",
            res_message="患者全景病历查询失败",
            output=str(exc),
            status_code=500,
        )


@bp.route("/clinical/mdt-applications/validate", methods=["POST"])
@token_required
def validate_mdt_application_route():
    """Validate whether current patient visit can submit an MDT application."""
    _db, cursor = get_db()
    data = request.get_json(silent=True) or {}
    patient_id = (data.get("patient_id") or "").strip()
    visit_no = (data.get("visit_no") or "").strip()
    group_id = (data.get("group_id") or "").strip()
    patient_data = data.get("patient_data") or data.get("patient_D") or data.get("facts")
    patient_payload = normalize_patient_payload(data, patient_id, visit_no)
    if not patient_id or not visit_no or not group_id:
        return make_response(
            res_code="error",
            res_message="参数错误",
            output="Missing patient_id/visit_no/group_id",
            status_code=400,
        )

    try:
        result = validate_mdt_application(
            cursor,
            patient_id,
            visit_no,
            group_id,
            patient_data=patient_data,
            patient=patient_payload,
        )
        attach_validation_report_materials(cursor, result, patient_data)
        return make_response(
            res_code="ok",
            res_message="校验完成",
            output=result,
            status_code=200,
        )
    except Exception as exc:
        logger_system.error(f"Database error during MDT validation: {str(exc)}")
        return make_response(
            res_code="error",
            res_message="MDT申请校验失败",
            output=str(exc),
            status_code=500,
        )


@bp.route("/clinical/mdt-applications", methods=["POST"])
@token_required
def submit_mdt_application():
    """Submit MDT application, preserving rule validation result as review context."""
    db, cursor = get_db()
    data = request.get_json(silent=True) or {}
    patient_id = (data.get("patient_id") or "").strip()
    visit_no = (data.get("visit_no") or "").strip()
    group_id = (data.get("group_id") or "").strip()
    apply_reason = (data.get("apply_reason") or "").strip()
    schedule_id = (data.get("schedule_id") or "").strip()
    appointment_type = (data.get("appointment_type") or "REGULAR").strip().upper()
    requested_consultation_date = clean_text_value(
        data.get("requested_consultation_date") or data.get("schedule_date")
    )
    patient_data = data.get("patient_data") or data.get("patient_D") or data.get("facts")
    initial_status = "WAITLIST" if appointment_type == "WAITLIST" else "SUBMITTED"

    if not patient_id or not visit_no or not group_id:
        return make_response(
            res_code="error",
            res_message="参数错误",
            output="Missing patient_id/visit_no/group_id",
            status_code=400,
        )

    try:
        validation = validate_mdt_application(
            cursor,
            patient_id,
            visit_no,
            group_id,
            patient_data=patient_data,
            patient=normalize_patient_payload(data, patient_id, visit_no),
        )

        basic = query_patient_basic(cursor, patient_id, visit_no)
        group = query_one(
            cursor,
            """
            select group_id,
                   group_name
              from mdt_disease_group
             where group_id = :group_id
            """,
            {"group_id": group_id},
        )
        if not basic or not group:
            return make_response(
                res_code="error",
                res_message="患者或病种组不存在",
                output=validation,
                status_code=400,
            )
        rule_facts = patient_data if isinstance(patient_data, dict) else {}
        ct_report_sn = clean_text_value(rule_facts.get("ct_report_sn"))
        jy_report_sn = clean_text_value(rule_facts.get("jy_report_sn"))
        application_id = generate_id("MDTA")
        queue_order = None
        if schedule_id:
            capacity = fetch_schedule_capacity_for_update(cursor, schedule_id)
            if capacity <= 0:
                db.rollback()
                return make_response(
                    res_code="error",
                    res_message="排班不存在或容量无效",
                    output={"schedule_id": schedule_id},
                    status_code=400,
                )
            queue_order = next_queue_order(cursor, schedule_id)
            initial_status = "WAITLIST" if queue_order > capacity else "SUBMITTED"
            appointment_type = "WAITLIST" if queue_order > capacity else "REGULAR"
        cursor.execute(
            """
            insert into mdt_clinical_application
                (application_id, patient_id, visit_no, patient_name,
                 group_id, group_name, applicant_id, apply_reason,
                 schedule_id, appointment_type, status,
                 queue_order, schedule_no,
                 requested_consultation_date, scheduled_consultation_date,
                 ct_report_sn, jy_report_sn, rule_facts_json, rule_checked_at,
                 created_at, updated_at)
            values
                (:application_id, :patient_id, :visit_no, :patient_name,
                 :group_id, :group_name, :applicant_id, :apply_reason,
                 :schedule_id, :appointment_type, :status,
                 :queue_order, null,
                 to_date(:requested_consultation_date, 'YYYY-MM-DD'), null,
                 :ct_report_sn, :jy_report_sn, :rule_facts_json, sysdate,
                 sysdate, sysdate)
            """,
            {
                "application_id": application_id,
                "patient_id": patient_id,
                "visit_no": visit_no,
                "patient_name": basic.get("patient_name") or "",
                "group_id": group_id,
                "group_name": group.get("group_name") or "",
                "applicant_id": getattr(g, "user_id", ""),
                "apply_reason": apply_reason,
                "schedule_id": schedule_id,
                "appointment_type": appointment_type,
                "status": initial_status,
                "queue_order": queue_order,
                "requested_consultation_date": requested_consultation_date,
                "ct_report_sn": ct_report_sn,
                "jy_report_sn": jy_report_sn,
                "rule_facts_json": dump_json_value(rule_facts),
            },
        )
        changed_queue = rebalance_application_queue(cursor, schedule_id) if schedule_id else []
        assigned = next(
            (
                item
                for item in changed_queue
                if item.get("application_id") == application_id
            ),
            {},
        )
        db.commit()
        return make_response(
            res_code="ok",
            res_message="MDT申请提交成功",
            output={
                "application_id": application_id,
                "queue_order": queue_order,
                "schedule_no": assigned.get("schedule_no"),
                "status": assigned.get("status") or initial_status,
            },
            status_code=200,
        )
    except Exception as exc:
        db.rollback()
        logger_system.error(f"Database error during MDT submit: {str(exc)}")
        return make_response(
            res_code="error",
            res_message="MDT申请提交失败",
            output=str(exc),
            status_code=500,
        )


@bp.route("/clinical/mdt-applications", methods=["GET"])
@token_required
def list_mdt_applications():
    """Query MDT applications for doctor's workflow board or secretary lobby."""
    _db, cursor = get_db()
    keyword = (request.args.get("keyword") or "").strip()
    group_id = (request.args.get("group_id") or "").strip()
    status = (request.args.get("status") or "").strip().upper()
    statuses = [
        item.strip().upper()
        for item in (request.args.get("statuses") or "").split(",")
        if item.strip()
    ]
    start_date = (request.args.get("start_date") or "").strip()
    end_date = (request.args.get("end_date") or "").strip()
    scope = (request.args.get("scope") or "").strip().lower()

    where = ["1 = 1"]
    params = {}
    if scope not in {"lobby", "secretary", "all"}:
        where.append("applicant_id = :applicant_id")
        params["applicant_id"] = getattr(g, "user_id", "")
    if keyword:
        where.append(
            """
            (patient_name like :keyword
             or patient_id like :keyword
             or visit_no like :keyword
             or application_id like :keyword)
            """
        )
        params["keyword"] = f"%{keyword}%"
    if group_id:
        where.append("group_id = :group_id")
        params["group_id"] = group_id
    if status:
        where.append("upper(status) = :status")
        params["status"] = status
    elif statuses:
        status_binds = []
        for index, value in enumerate(statuses):
            bind = f"status_{index}"
            status_binds.append(f":{bind}")
            params[bind] = value
        where.append(f"upper(status) in ({', '.join(status_binds)})")
    if start_date:
        where.append("created_at >= to_date(:start_date, 'YYYY-MM-DD')")
        params["start_date"] = start_date
    if end_date:
        where.append("created_at < to_date(:end_date, 'YYYY-MM-DD') + 1")
        params["end_date"] = end_date

    try:
        cursor.execute(
            f"""
            select application_id,
                   patient_id,
                   visit_no,
                   patient_name,
                   group_id,
                   group_name,
                   applicant_id,
                   apply_reason,
                   status,
                   schedule_id,
                   appointment_type,
                   queue_order,
                   schedule_no,
                   to_char(requested_consultation_date, 'YYYY-MM-DD') as requested_consultation_date,
                   to_char(scheduled_consultation_date, 'YYYY-MM-DD') as scheduled_consultation_date,
                   ct_report_sn,
                   jy_report_sn,
                   rule_facts_json,
                   to_char(rule_checked_at, 'YYYY-MM-DD HH24:MI') as rule_checked_at,
                   case
                       when schedule_no >= 100 then schedule_no - 100
                       else null
                   end as waitlist_no,
                   null as waitlist_message,
                   to_char(created_at, 'YYYY-MM-DD HH24:MI') as created_at,
                   to_char(updated_at, 'YYYY-MM-DD HH24:MI') as updated_at
              from mdt_clinical_application
             where {' and '.join(where)}
             order by updated_at desc, created_at desc
            """,
            params,
        )
        items = rows_to_dicts(cursor, cursor.fetchall())
        for item in items:
            item["ct_report_sn"] = clean_text_value(item.get("ct_report_sn"))
            item["jy_report_sn"] = clean_text_value(item.get("jy_report_sn"))
            item["rule_facts_json"] = read_lob(item.get("rule_facts_json")) or ""
        if scope in {"lobby", "secretary", "all"}:
            attach_lobby_diagnoses(cursor, items)
            attach_lobby_admission_rules(cursor, items)
            attach_lobby_report_materials(cursor, items)
        return make_response(
            res_code="ok",
            res_message="查询成功",
            output={"items": items},
            status_code=200,
        )
    except Exception as exc:
        logger_system.error(f"Database error during MDT application query: {str(exc)}")
        return make_response(
            res_code="error",
            res_message="MDT申请查询失败",
            output=str(exc),
            status_code=500,
        )


def attach_lobby_admission_rules(cursor, applications):
    group_ids = sorted(
        {
            str(item.get("group_id") or "").strip()
            for item in applications or []
            if str(item.get("group_id") or "").strip()
        }
    )
    if not group_ids:
        return
    bind_names = []
    params = {}
    for index, group_id in enumerate(group_ids):
        bind = f"group_id_{index}"
        bind_names.append(f":{bind}")
        params[bind] = group_id
    cursor.execute(
        f"""
        select group_id,
               rule_id,
               rule_name,
               logic_type,
               action_type,
               action_message,
               sort_no
          from mdt_disease_admission_rule
         where group_id in ({", ".join(bind_names)})
           and nvl(enabled_flag, '1') = '1'
         order by group_id, sort_no, rule_id
        """,
        params,
    )
    rules_by_group = {}
    for row in rows_to_dicts(cursor, cursor.fetchall()):
        row["action_message"] = read_lob(row.get("action_message")) or ""
        rules_by_group.setdefault(row.get("group_id"), []).append(row)
    for item in applications:
        item["admission_rules"] = rules_by_group.get(item.get("group_id"), [])


def attach_lobby_diagnoses(cursor, applications):
    visit_keys = sorted(
        {
            (
                str(item.get("patient_id") or "").strip(),
                str(item.get("visit_no") or "").strip(),
            )
            for item in applications or []
            if str(item.get("patient_id") or "").strip()
            and str(item.get("visit_no") or "").strip()
        }
    )
    if not visit_keys:
        return
    clauses = []
    params = {}
    for index, (patient_id, visit_no) in enumerate(visit_keys):
        patient_bind = f"diag_patient_{index}"
        visit_bind = f"diag_visit_{index}"
        clauses.append(
            f"(patient_id = :{patient_bind} and visit_number = :{visit_bind})"
        )
        params[patient_bind] = patient_id
        params[visit_bind] = visit_no
    cursor.execute(
        f"""
        select patient_id,
               visit_number as visit_no,
               value_code as diagnosis_code,
               value_st_txt as diagnosis_name,
               code as diagnosis_type,
               to_char(aut_time, 'YYYY-MM-DD HH24:MI') as diagnosis_time
          from mdt_obs_dx
         where {" or ".join(clauses)}
         order by patient_id, visit_number, aut_time desc nulls last, code
        """,
        params,
    )
    diagnoses_by_visit = {}
    for row in rows_to_dicts(cursor, cursor.fetchall()):
        key = (row.get("patient_id"), row.get("visit_no"))
        diagnoses_by_visit.setdefault(key, []).append(row)
    for item in applications or []:
        key = (item.get("patient_id"), item.get("visit_no"))
        item["diagnoses"] = diagnoses_by_visit.get(key, [])


def attach_lobby_report_materials(cursor, applications):
    ct_report_sns = sorted(
        {
            item.get("ct_report_sn")
            for item in applications or []
            if item.get("ct_report_sn")
        }
    )
    jy_report_sns = sorted(
        {
            item.get("jy_report_sn")
            for item in applications or []
            if item.get("jy_report_sn")
        }
    )
    ct_reports = query_pacs_reports_by_sn(cursor, ct_report_sns)
    jy_reports = query_lis_reports_by_sn(cursor, jy_report_sns)
    for item in applications or []:
        item["ct_report"] = ct_reports.get(item.get("ct_report_sn")) or {}
        item["jy_report"] = jy_reports.get(item.get("jy_report_sn")) or {}


def attach_validation_report_materials(cursor, result, patient_data):
    if not isinstance(result, dict):
        return
    rule_facts = patient_data if isinstance(patient_data, dict) else {}
    ct_report_sn = clean_text_value(rule_facts.get("ct_report_sn"))
    jy_report_sn = clean_text_value(rule_facts.get("jy_report_sn"))
    result["ct_report_sn"] = ct_report_sn
    result["jy_report_sn"] = jy_report_sn
    result["ct_report"] = (
        query_pacs_reports_by_sn(cursor, [ct_report_sn]).get(ct_report_sn)
        if ct_report_sn
        else {}
    ) or {}
    result["jy_report"] = (
        query_lis_reports_by_sn(cursor, [jy_report_sn]).get(jy_report_sn)
        if jy_report_sn
        else {}
    ) or {}


def queue_position_text(schedule_no):
    try:
        value = int(float(schedule_no))
    except (TypeError, ValueError):
        return "待定"
    if value >= 100:
        return f"候补{value - 100}"
    return f"正班{value}"


def doctor_display_name(value):
    text = clean_text_value(value)
    return text or "申请"


def build_reject_notification(application, reason):
    doctor_name = doctor_display_name(application.get("applicant_id"))
    patient_name = clean_text_value(application.get("patient_name")) or "患者"
    meeting_name = clean_text_value(application.get("group_name")) or "MDT会诊"
    original_order = queue_position_text(application.get("schedule_no"))
    reject_reason = reason or "秘书审核意见"
    message = (
        "【MDT会诊中心】\n"
        f"尊敬的 {doctor_name} 医生，您为患者 {patient_name} 申请的 "
        f"{meeting_name}（原预排第 {original_order} 顺位）因 {reject_reason} 未通过审核，申请已被退回。\n"
        "请您登录 MDT 系统，在“已退回工作台”查看详情并修改补充材料，重新提交后系统将为您重新排队。"
    )
    return {
        "type": "REJECTED",
        "application_id": application.get("application_id"),
        "doctor_id": application.get("applicant_id"),
        "patient_name": patient_name,
        "message": message,
    }


def build_promotion_notification(queue_item):
    doctor_name = doctor_display_name(queue_item.get("applicant_id"))
    patient_name = clean_text_value(queue_item.get("patient_name")) or "患者"
    meeting_name = clean_text_value(queue_item.get("group_name")) or "MDT会诊"
    new_order = queue_position_text(queue_item.get("schedule_no"))
    message = (
        "【MDT会诊中心】\n"
        f"尊敬的 {doctor_name} 医生，告诉您一个好消息：您申请的患者 {patient_name} "
        f"已成功递补进入 {meeting_name} 正班序列（当前为第 {new_order} 顺位）。\n"
        "会议正式排定后系统将为您发送确切讨论时间，请您提前整理好汇报病历，做好会诊准备。"
    )
    return {
        "type": "PROMOTED_TO_REGULAR",
        "application_id": queue_item.get("application_id"),
        "doctor_id": queue_item.get("applicant_id"),
        "patient_name": patient_name,
        "message": message,
    }


def build_queue_change_notifications(rejected_application, reason, changed_queue):
    notifications = [build_reject_notification(rejected_application, reason)]
    for item in changed_queue or []:
        try:
            old_schedule_no = int(float(item.get("old_schedule_no")))
            new_schedule_no = int(float(item.get("schedule_no")))
        except (TypeError, ValueError):
            continue
        if old_schedule_no >= 100 and new_schedule_no < 100:
            notifications.append(build_promotion_notification(item))
    return notifications


def query_pacs_reports_by_sn(cursor, report_sns):
    if not report_sns:
        return {}
    bind_names, params = build_in_binds("ct_report", report_sns)
    cursor.execute(
        f"""
        select reportsn as report_id,
               to_char(examdate, 'YYYY-MM-DD HH24:MI') as exam_time,
               ordertype as exam_type,
               itemname as exam_item,
               examsee as finding,
               examresult as conclusion,
               null as pacs_url,
               null as pacs_command,
               reportdoctor,
               to_char(applydate, 'YYYY-MM-DD HH24:MI') as apply_time,
               applydept,
               applydoctor,
               exammethod,
               orderid
          from mdt_phone_checklist_fs
         where reportsn in ({", ".join(bind_names)})
        """,
        params,
    )
    reports = {}
    for row in rows_to_dicts(cursor, cursor.fetchall()):
        row["finding"] = read_lob(row.get("finding")) or ""
        row["conclusion"] = read_lob(row.get("conclusion")) or ""
        reports[row.get("report_id")] = row
    return reports


def query_lis_reports_by_sn(cursor, report_sns):
    if not report_sns:
        return {}
    bind_names, params = build_in_binds("jy_report", report_sns)
    cursor.execute(
        f"""
        select 报告单号 as report_id,
               报告日期 as report_time,
               检验项目 as item_code,
               检验细目 as item_name,
               结果 as result_value,
               单位 as result_unit,
               参考值 as reference_range,
               decode(提示, 'H', '高', 'L', '低', 'None', '', 提示) as abnormal_flag,
               检验日期 as test_time,
               姓名 as patient_name
          from mdt_phone_testdetails
         where 报告单号 in ({", ".join(bind_names)})
         order by 报告单号, 检验项目, 检验细目
        """,
        params,
    )
    reports = {}
    for row in rows_to_dicts(cursor, cursor.fetchall()):
        report_id = row.get("report_id")
        report = reports.setdefault(
            report_id,
            {
                "report_id": report_id,
                "report_time": row.get("report_time"),
                "test_time": row.get("test_time"),
                "patient_name": row.get("patient_name"),
                "items": [],
            },
        )
        report["items"].append(
            {
                "item_code": row.get("item_code"),
                "item_name": row.get("item_name"),
                "result_value": row.get("result_value"),
                "result_unit": row.get("result_unit"),
                "reference_range": row.get("reference_range"),
                "abnormal_flag": row.get("abnormal_flag"),
            }
        )
    return reports


def build_in_binds(prefix, values):
    bind_names = []
    params = {}
    for index, value in enumerate(values):
        bind = f"{prefix}_{index}"
        bind_names.append(f":{bind}")
        params[bind] = value
    return bind_names, params


@bp.route("/clinical/mdt-applications/drafts", methods=["POST"])
@token_required
def save_mdt_application_draft():
    """Save a half-finished MDT application into doctor's draft box."""
    db, cursor = get_db()
    data = request.get_json(silent=True) or {}
    patient_id = (data.get("patient_id") or "").strip()
    visit_no = (data.get("visit_no") or "").strip()
    group_id = (data.get("group_id") or "").strip()
    apply_reason = (data.get("apply_reason") or "").strip()

    if not patient_id or not visit_no or not group_id:
        return make_response(
            res_code="error",
            res_message="参数错误",
            output="Missing patient_id/visit_no/group_id",
            status_code=400,
        )

    try:
        patient = query_patient_basic(cursor, patient_id, visit_no)
        group = query_one(
            cursor,
            """
            select group_id,
                   group_name
              from mdt_disease_group
             where group_id = :group_id
            """,
            {"group_id": group_id},
        )
        if not patient or not group:
            return make_response(
                res_code="error",
                res_message="患者或病种组不存在",
                output="patient/group not found",
                status_code=400,
            )

        application_id = generate_id("MDTD")
        cursor.execute(
            """
            insert into mdt_clinical_application
                (application_id, patient_id, visit_no, patient_name,
                 group_id, group_name, applicant_id, apply_reason,
                 status, created_at, updated_at)
            values
                (:application_id, :patient_id, :visit_no, :patient_name,
                 :group_id, :group_name, :applicant_id, :apply_reason,
                 'DRAFT', sysdate, sysdate)
            """,
            {
                "application_id": application_id,
                "patient_id": patient_id,
                "visit_no": visit_no,
                "patient_name": patient.get("patient_name") or "",
                "group_id": group_id,
                "group_name": group.get("group_name") or "",
                "applicant_id": getattr(g, "user_id", ""),
                "apply_reason": apply_reason,
            },
        )
        db.commit()
        return make_response(
            res_code="ok",
            res_message="草稿已保存",
            output={"application_id": application_id},
            status_code=200,
        )
    except Exception as exc:
        db.rollback()
        logger_system.error(f"Database error during MDT draft save: {str(exc)}")
        return make_response(
            res_code="error",
            res_message="MDT申请暂存失败",
            output=str(exc),
            status_code=500,
        )


@bp.route("/clinical/mdt-applications/<application_id>/approve", methods=["POST"])
@token_required
def approve_mdt_application(application_id):
    """Approve an MDT application into the unassigned pool."""
    db, cursor = get_db()
    application_id = (application_id or "").strip()
    if not application_id:
        return make_response(
            res_code="error",
            res_message="参数错误",
            output="Missing application_id",
            status_code=400,
        )
    try:
        cursor.execute(
            """
            update mdt_clinical_application
               set status = 'APPROVED',
                   updated_at = sysdate
             where application_id = :application_id
               and upper(nvl(status, '-')) in
                   ('SUBMITTED', 'PENDING_REVIEW', 'WAITLIST')
            """,
            {"application_id": application_id},
        )
        if cursor.rowcount == 0:
            db.rollback()
            return make_response(
                res_code="error",
                res_message="申请状态不允许审核通过或申请不存在",
                output={"application_id": application_id},
                status_code=400,
            )
        db.commit()
        return make_response(
            res_code="ok",
            res_message="审核通过",
            output={"application_id": application_id, "status": "APPROVED"},
            status_code=200,
        )
    except Exception as exc:
        db.rollback()
        logger_system.error(f"Database error during MDT application approve: {str(exc)}")
        return make_response(
            res_code="error",
            res_message="MDT申请审核通过失败",
            output=str(exc),
            status_code=500,
        )


@bp.route("/clinical/mdt-applications/<application_id>/reject", methods=["POST"])
@token_required
def reject_mdt_application(application_id):
    """Reject an MDT application and rebalance the related schedule queue."""
    db, cursor = get_db()
    application_id = (application_id or "").strip()
    data = request.get_json(silent=True) or {}
    reason = clean_text_value(data.get("reason"))
    if not application_id:
        return make_response(
            res_code="error",
            res_message="参数错误",
            output="Missing application_id",
            status_code=400,
        )
    try:
        application = query_one(
            cursor,
            """
            select application_id,
                   schedule_id,
                   patient_name,
                   group_name,
                   applicant_id,
                   appointment_type,
                   schedule_no,
                   status
              from mdt_clinical_application
             where application_id = :application_id
             for update
            """,
            {"application_id": application_id},
        )
        if not application:
            db.rollback()
            return make_response(
                res_code="error",
                res_message="申请不存在",
                output={"application_id": application_id},
                status_code=404,
            )
        current_status = str(application.get("status") or "").upper()
        if current_status in {"DRAFT", "REJECTED", "CANCELLED", "DONE", "FINISHED"}:
            db.rollback()
            return make_response(
                res_code="error",
                res_message="申请状态不允许驳回",
                output={"application_id": application_id, "status": current_status},
                status_code=400,
            )

        cursor.execute(
            """
            update mdt_clinical_application
               set status = 'REJECTED',
                   schedule_no = null,
                   updated_at = sysdate
             where application_id = :application_id
            """,
            {"application_id": application_id},
        )
        schedule_id = clean_text_value(application.get("schedule_id"))
        changed_queue = rebalance_application_queue(cursor, schedule_id) if schedule_id else []
        notifications = build_queue_change_notifications(application, reason, changed_queue)
        db.commit()
        return make_response(
            res_code="ok",
            res_message="申请已驳回，排班队列已重排",
            output={
                "application_id": application_id,
                "status": "REJECTED",
                "reason": reason,
                "schedule_id": schedule_id,
                "queue": changed_queue,
                "notifications": notifications,
            },
            status_code=200,
        )
    except Exception as exc:
        db.rollback()
        logger_system.error(f"Database error during MDT application reject: {str(exc)}")
        return make_response(
            res_code="error",
            res_message="MDT申请驳回失败",
            output=str(exc),
            status_code=500,
        )


@bp.route("/clinical/mdt-applications/<application_id>/opinion", methods=["GET"])
@token_required
def get_mdt_application_opinion(application_id):
    """Return archived MDT opinion document content when available."""
    return make_response(
        res_code="ok",
        res_message="查询成功",
        output={
            "application_id": application_id,
            "content": "",
        },
        status_code=200,
    )


def validate_mdt_application(
    cursor,
    patient_id,
    visit_no,
    group_id,
    patient_data=None,
    patient=None,
):
    patient = normalize_patient_payload(patient or {}, patient_id, visit_no)
    if not has_patient_identity(patient):
        patient = query_patient_basic(cursor, patient_id, visit_no)
    if not patient:
        return {
            "passed": False,
            "message": "未找到当前患者就诊信息",
            "errors": ["未找到当前患者就诊信息"],
            "rules": [],
        }

    group = query_one(
        cursor,
        """
        select group_id,
               group_name,
               rule_enabled
          from mdt_disease_group
         where group_id = :group_id
           and nvl(enabled_flag, '1') = '1'
        """,
        {"group_id": group_id},
    )
    if not group:
        return {
            "passed": False,
            "message": "病种组不存在或已停用",
            "errors": ["病种组不存在或已停用"],
            "rules": [],
        }

    diagnoses = build_payload_diagnoses(patient) or query_patient_diagnoses(
        cursor, patient_id, visit_no
    )
    keywords = query_list(
        cursor,
        """
        select icd_code,
               icd_name
          from mdt_disease_group_keyword
         where group_id = :group_id
           and nvl(keyword_type, 'DISEASE') = 'DISEASE'
         order by sort_no, icd_code
        """,
        {"group_id": group_id},
    )
    pacs_reports = query_patient_pacs(cursor, patient_id, visit_no)
    lis_items = query_patient_lis(cursor, patient_id, visit_no)
    rules = query_list(
        cursor,
        """
        select rule_id,
               rule_name,
               logic_type,
               engine_conditions,
               action_type,
               action_message,
               enabled_flag,
               sort_no
          from mdt_disease_admission_rule
         where group_id = :group_id
           and nvl(enabled_flag, '1') = '1'
         order by sort_no, rule_id
        """,
        {"group_id": group_id},
    )
    for rule in rules:
        rule["logic"] = rule.get("logic_type") or "AND"
        rule["engine_conditions"] = load_json_value(rule.get("engine_conditions"), {})
        rule["action_type"] = rule.get("action_type") or "REJECT"
        rule["action_message"] = read_lob(rule.get("action_message")) or ""
    if str(group.get("rule_enabled") or "1") != "1":
        return {
            "passed": True,
            "message": "病种组未启用规则校验，允许提交",
            "errors": [],
            "rules": compact_validation_rules(rules),
        }

    errors = []
    warnings = []
    matched_keywords = match_diagnosis_keywords(diagnoses, patient, keywords)
    if keywords and not matched_keywords:
        errors.append("患者诊断未命中该病种组的关联病种")
    if not keywords:
        errors.append("当前病种组未配置关联病种，不能通过规则校验")
    rule_facts = build_patient_rule_facts(patient, diagnoses, pacs_reports, lis_items)
    if isinstance(patient_data, dict):
        rule_facts.update(
            {
                key: value
                for key, value in patient_data.items()
                if key
                not in {
                    "has_ct",
                    "ct_days_ago",
                    "ct_report_sn",
                    "has_tumor_markers",
                    "lab_days_ago",
                    "jy_report_sn",
                    "has_recent_hrct",
                    "has_recent_tumor_markers",
                }
            }
        )
    rule_result = evaluate_admission_rules(rules, rule_facts)
    if rule_result["action_type"] == "REJECT":
        errors.extend(rule_result["messages"])
    elif rule_result["action_type"] == "WARN":
        warnings.extend(rule_result["messages"])

    return {
        "passed": not errors,
        "message": validation_message(errors, warnings),
        "errors": errors,
        "rules": compact_validation_rules(rules),
    }


def compact_validation_rules(rules):
    """Return only fields needed by the client rule viewer."""
    compact_rules = []
    for rule in rules or []:
        action_message = rule.get("action_message") or ""
        compact_rules.append(
            {
                "rule_id": rule.get("rule_id") or "",
                "rule_name": rule.get("rule_name") or "",
                "rule_type": rule.get("logic_type") or rule.get("logic") or "AND",
                "indicator_rule": readable_engine_conditions(rule.get("engine_conditions")),
                "engine_conditions": rule.get("engine_conditions") or {},
                "action_type": rule.get("action_type") or "REJECT",
                "action_message": action_message,
                "actions": compact_rule_actions(rule),
            }
        )
    return compact_rules


def compact_rule_actions(rule):
    message = rule.get("action_message") or ""
    action_type = rule.get("action_type") or "REJECT"
    if action_type == "REJECT":
        name = "trigger_reject"
    elif action_type == "WARN":
        name = "trigger_warning"
    else:
        name = action_type.lower()
    return [{"name": name, "params": {"error_msg": message}}]


def readable_engine_conditions(conditions):
    if not conditions:
        return "已启用规则"
    if isinstance(conditions, str):
        return conditions
    if isinstance(conditions, dict):
        if "all" in conditions:
            return "需同时满足：" + "；".join(
                readable_condition_item(item) for item in conditions.get("all") or []
            )
        if "any" in conditions:
            return "满足任一条件：" + "；".join(
                readable_condition_item(item) for item in conditions.get("any") or []
            )
    return str(conditions)


def readable_condition_item(item):
    if not isinstance(item, dict):
        return str(item)
    name = str(item.get("name") or item.get("variable") or item.get("field") or "")
    operator = str(item.get("operator") or item.get("op") or "")
    value = item.get("value")
    labels = {
        "has_ct": "合格薄层CT",
        "ct_days_ago": "薄层CT距今天数",
        "has_tumor_markers": "肿瘤标志物检测",
        "nodule_size": "结节大小",
        "nodule_density": "结节密度",
        "cea_level": "CEA",
        "cyfra211_level": "CYFRA21-1",
    }
    operators = {
        "equal_to": "等于",
        "not_equal_to": "不等于",
        "greater_than": "大于",
        "greater_than_or_equal_to": "大于等于",
        "less_than": "小于",
        "less_than_or_equal_to": "小于等于",
    }
    return f"{labels.get(name, name)} {operators.get(operator, operator)} {value}"



def normalize_patient_payload(data, patient_id="", visit_no=""):
    if not isinstance(data, dict):
        return {}
    patient = dict(data)
    patient["patient_id"] = str(patient.get("patient_id") or patient_id or "").strip()
    patient["visit_no"] = str(
        patient.get("visit_no") or patient.get("visit_number") or visit_no or ""
    ).strip()
    return patient


def has_patient_identity(patient):
    return bool(
        isinstance(patient, dict)
        and patient.get("patient_id")
        and patient.get("visit_no")
    )


def build_payload_diagnoses(patient):
    if not isinstance(patient, dict):
        return []
    diagnosis_name = str(patient.get("main_diagnosis") or "").strip()
    diagnosis_code = str(patient.get("main_diagnosis_code") or "").strip()
    if not diagnosis_name and not diagnosis_code:
        return []
    return [
        {
            "diagnosis_code": diagnosis_code,
            "diagnosis_name": diagnosis_name,
            "diagnosis_type": "主要诊断",
            "diagnosis_time": patient.get("admission_time") or "",
        }
    ]


def query_patient_basic(cursor, patient_id, visit_no=None):
    return query_one(
        cursor,
        """
        select patient_id,
               patient_name,
               sex,
               age,
               bed_no,
               visit_no,
               medical_record_no,
               birth_date,
               allergy_history,
               main_diagnosis,
               main_diagnosis_code,
               admission_time,
               dept_name,
               attending_doctor,
               visit_type
          from (
                select p.patient_id,
                       p.name as patient_name,
                       decode(to_char(p.sex), '1', '男', '2', '女', to_char(p.sex)) as sex,
                       floor(months_between(sysdate, p.birthday) / 12) as age,
                       null as bed_no,
                       v.visit_number as visit_no,
                       p.social_no as medical_record_no,
                       to_char(p.birthday, 'YYYY-MM-DD') as birth_date,
                       null as allergy_history,
                       d.value_st_txt as main_diagnosis,
                       d.value_code as main_diagnosis_code,
                       to_char(v.visit_time, 'YYYY-MM-DD HH24:MI') as admission_time,
                       v.dept_name,
                       v.doctor_name as attending_doctor,
                       v.visit_type,
                       v.visit_time
                  from mdt_a_patient_mi p
                  left join (
                        select patient_id,
                               visit_number,
                               visit_date as visit_time,
                               gh_dept_name as dept_name,
                               doctor_name,
                               nvl(clinic_type, '门诊') as visit_type
                          from mdt_mz_visit_table
                        union all
                        select patient_id,
                               visit_number,
                               dis_date as visit_time,
                               dept_sn_name as dept_name,
                               null as doctor_name,
                               '住院' as visit_type
                          from mdt_zy_actpatient
                  ) v on v.patient_id = p.patient_id
                  left join (
                        select patient_id,
                               visit_number,
                               value_code,
                               value_st_txt,
                               row_number() over (
                                   partition by patient_id, visit_number
                                   order by aut_time desc nulls last
                               ) as rn
                          from mdt_obs_dx
                  ) d on d.patient_id = v.patient_id
                     and d.visit_number = v.visit_number
                     and d.rn = 1
                 where p.patient_id = :patient_id
                   and (:visit_no is null or v.visit_number = :visit_no)
                 order by v.visit_time desc nulls last, v.visit_number desc nulls last
               )
         where rownum = 1
        """,
        {"patient_id": patient_id, "visit_no": visit_no},
    )


def query_patient_visits(cursor, patient_id):
    return query_list(
        cursor,
        """
        select patient_id,
               patient_name,
               sex,
               age,
               bed_no,
               visit_no,
               medical_record_no,
               birth_date,
               visit_type,
               ward_name,
               visit_time,
               admission_time,
               dept_name,
               attending_doctor
          from (
                select p.patient_id,
                       p.name as patient_name,
                       decode(to_char(p.sex), '1', '男', '2', '女', to_char(p.sex)) as sex,
                       floor(months_between(sysdate, p.birthday) / 12) as age,
                       null as bed_no,
                       v.visit_number as visit_no,
                       p.social_no as medical_record_no,
                       to_char(p.birthday, 'YYYY-MM-DD') as birth_date,
                       v.visit_type,
                       v.dept_name as ward_name,
                       to_char(v.visit_time, 'YYYY-MM-DD HH24:MI') as visit_time,
                       to_char(v.visit_time, 'YYYY-MM-DD HH24:MI') as admission_time,
                       v.dept_name,
                       v.doctor_name as attending_doctor,
                       v.visit_time as raw_visit_time
                  from mdt_a_patient_mi p
                  join (
                        select patient_id,
                               visit_number,
                               visit_date as visit_time,
                               gh_dept_name as dept_name,
                               doctor_name,
                               nvl(clinic_type, '门诊') as visit_type
                          from mdt_mz_visit_table
                        union all
                        select patient_id,
                               visit_number,
                               dis_date as visit_time,
                               dept_sn_name as dept_name,
                               null as doctor_name,
                               '住院' as visit_type
                          from mdt_zy_actpatient
                  ) v on v.patient_id = p.patient_id
                 where p.patient_id = :patient_id
                 order by v.visit_time desc nulls last, v.visit_number desc nulls last
               )
        """,
        {"patient_id": patient_id},
    )


def query_patient_diagnoses(cursor, patient_id, visit_no=None):
    return query_list(
        cursor,
        """
        select value_code as diagnosis_code,
               value_st_txt as diagnosis_name,
               code as diagnosis_type,
               to_char(aut_time, 'YYYY-MM-DD HH24:MI') as diagnosis_time
          from mdt_obs_dx
         where patient_id = :patient_id
           and (:visit_no is null or visit_number = :visit_no)
         order by aut_time desc nulls last, code
        """,
        {"patient_id": patient_id, "visit_no": visit_no},
    )


def query_patient_pacs(cursor, patient_id, visit_no=None):
    return query_list(
        cursor,
        """
        select reportsn as report_id,
               to_char(examdate, 'YYYY-MM-DD HH24:MI') as exam_time,
               ordertype as exam_type,
               itemname as exam_item,
               examsee as finding,
               examresult as conclusion,
               null as pacs_url,
               null as pacs_command,
               reportdoctor,
               to_char(applydate, 'YYYY-MM-DD HH24:MI') as apply_time,
               applydept,
               applydoctor,
               exammethod, 
               orderid
          from mdt_phone_checklist_fs
         where patient_id = :patient_id
           and (:visit_no is null or times = :visit_no)
         order by examdate desc nulls last, applydate desc nulls last
        """,
        {"patient_id": patient_id, "visit_no": visit_no},
    )


def query_patient_lis(cursor, patient_id, visit_no=None):
    return query_list(
        cursor,
        """
        select 报告单号 as report_id,
               报告日期 as report_time,
               检验项目 as item_code,
               检验细目 as item_name,
               结果 as result_value,
               单位 as result_unit,
               参考值 as reference_range,
               decode(提示, 'H', '高', 'L', '低', 'None', '', 提示) as abnormal_flag,
               检验日期 as test_time,
               姓名 as patient_name
          from mdt_phone_testdetails
         where 患者登记号 = :patient_id
           and (:visit_no is null or 就诊流水号 = :visit_no)
         order by 报告日期 desc nulls last, 检验项目, 检验细目
        """,
        {"patient_id": patient_id, "visit_no": visit_no},
    )


def build_patient_timeline(visits, diagnoses, pacs_reports, lis_items):
    rows = []
    for visit in visits or []:
        event_time = visit.get("visit_time") or visit.get("admission_time")
        rows.append(
            {
                "event_time": event_time,
                "event_type": visit.get("visit_type") or "就诊",
                "title": visit.get("dept_name") or visit.get("ward_name") or "就诊记录",
                "content": f"就诊流水号：{visit.get('visit_no') or ''}",
            }
        )
    for diagnosis in diagnoses or []:
        rows.append(
            {
                "event_time": diagnosis.get("diagnosis_time"),
                "event_type": "诊断",
                "title": diagnosis.get("diagnosis_name") or "诊断记录",
                "content": diagnosis.get("diagnosis_code") or "",
            }
        )
    for report in pacs_reports or []:
        rows.append(
            {
                "event_time": report.get("exam_time"),
                "event_type": "检查",
                "title": report.get("exam_item") or "检查报告",
                "content": report.get("conclusion") or report.get("finding") or "",
            }
        )
    seen_lis = set()
    for item in lis_items or []:
        key = (item.get("report_id"), item.get("report_time"))
        if key in seen_lis:
            continue
        seen_lis.add(key)
        rows.append(
            {
                "event_time": item.get("report_time"),
                "event_type": "检验",
                "title": item.get("item_code") or item.get("item_name") or "检验报告",
                "content": "检验报告已出具",
            }
        )
    return sorted(rows, key=lambda row: row.get("event_time") or "")

def validation_message(errors, warnings):
    if errors:
        return "规则校验未通过"
    if warnings:
        return "规则校验通过，但存在预警"
    return "规则校验通过"


def match_diagnosis_keywords(diagnoses, patient, keywords):
    diagnosis_text = build_diagnosis_text(diagnoses, patient)
    matches = []
    for keyword in keywords:
        icd_code = str(keyword.get("icd_code") or "").strip().upper()
        icd_name = str(keyword.get("icd_name") or "").strip().upper()
        if icd_code and icd_code in diagnosis_text:
            matches.append(keyword)
        elif icd_name and icd_name in diagnosis_text:
            matches.append(keyword)
    return matches


def build_diagnosis_text(diagnoses, patient):
    parts = [
        patient.get("main_diagnosis_code") or "",
        patient.get("main_diagnosis") or "",
    ]
    for diagnosis in diagnoses:
        parts.extend(
            [
                diagnosis.get("diagnosis_code") or "",
                diagnosis.get("diagnosis_name") or "",
            ]
        )
    return " ".join(str(part).upper() for part in parts if part)


def query_one(cursor, sql, params):
    cursor.execute(sql, params)
    rows = rows_to_dicts(cursor, cursor.fetchall())
    return rows[0] if rows else None


def query_list(cursor, sql, params):
    cursor.execute(sql, params)
    return rows_to_dicts(cursor, cursor.fetchall())
