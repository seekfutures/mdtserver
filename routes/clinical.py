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
    """Submit MDT application after server-side rule validation."""
    db, cursor = get_db()
    data = request.get_json(silent=True) or {}
    patient_id = (data.get("patient_id") or "").strip()
    visit_no = (data.get("visit_no") or "").strip()
    group_id = (data.get("group_id") or "").strip()
    apply_reason = (data.get("apply_reason") or "").strip()
    schedule_id = (data.get("schedule_id") or "").strip()
    appointment_type = (data.get("appointment_type") or "REGULAR").strip().upper()
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
        if not validation["passed"]:
            return make_response(
                res_code="error",
                res_message="规则校验未通过，不能提交MDT申请",
                output=validation,
                status_code=400,
            )

        basic = validation["patient"]
        group = validation["group"]
        application_id = generate_id("MDTA")
        cursor.execute(
            """
            insert into mdt_clinical_application
                (application_id, patient_id, visit_no, patient_name,
                 group_id, group_name, applicant_id, apply_reason,
                 schedule_id, appointment_type, status, created_at, updated_at)
            values
                (:application_id, :patient_id, :visit_no, :patient_name,
                 :group_id, :group_name, :applicant_id, :apply_reason,
                 :schedule_id, :appointment_type, :status, sysdate, sysdate)
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
            },
        )
        db.commit()
        return make_response(
            res_code="ok",
            res_message="MDT申请提交成功",
            output={"application_id": application_id},
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
    """Query current doctor's MDT applications for workflow board."""
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

    where = ["applicant_id = :applicant_id"]
    params = {"applicant_id": getattr(g, "user_id", "")}
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
                   null as waitlist_no,
                   null as waitlist_message,
                   to_char(created_at, 'YYYY-MM-DD HH24:MI') as created_at,
                   to_char(updated_at, 'YYYY-MM-DD HH24:MI') as updated_at
              from mdt_clinical_application
             where {' and '.join(where)}
             order by updated_at desc, created_at desc
            """,
            params,
        )
        return make_response(
            res_code="ok",
            res_message="查询成功",
            output={"items": rows_to_dicts(cursor, cursor.fetchall())},
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
            "warnings": [],
            "patient": {},
            "group": {},
            "diagnoses": [],
            "experts": [],
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
            "warnings": [],
            "patient": patient,
            "group": {},
            "diagnoses": [],
            "experts": [],
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
    experts = query_list(
        cursor,
        """
        select m.user_id,
               e.user_name,
               m.member_role,
               r.name as member_role_name,
               m.duty_title
          from mdt_disease_group_member m
          left join mdt_a_employee_mi e on e.user_id = m.user_id
          left join mdt_zd_role_groups r on r.code = m.member_role
         where m.group_id = :group_id
           and nvl(m.enabled_flag, '1') = '1'
         order by m.sort_no, m.member_id
        """,
        {"group_id": group_id},
    )

    if str(group.get("rule_enabled") or "1") != "1":
        return {
            "passed": True,
            "message": "病种组未启用规则校验，允许提交",
            "errors": [],
            "warnings": [],
            "patient": patient,
            "group": group,
            "diagnoses": diagnoses,
            "matched_keywords": [],
            "rules": rules,
            "experts": experts,
        }

    errors = []
    warnings = []
    matched_keywords = match_diagnosis_keywords(diagnoses, patient, keywords)
    if keywords and not matched_keywords:
        errors.append("患者诊断未命中该病种组的关联病种")
    if not keywords:
        errors.append("当前病种组未配置关联病种，不能通过规则校验")
    rule_facts = (
        patient_data
        if isinstance(patient_data, dict)
        else build_patient_rule_facts(patient, diagnoses, pacs_reports, lis_items)
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
        "warnings": warnings,
        "patient": patient,
        "group": group,
        "diagnoses": diagnoses,
        "matched_keywords": matched_keywords,
        "rules": rules,
        "rule_result": rule_result,
        "rule_facts": rule_facts,
        "experts": experts,
    }



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
        select 检验项目 || '-' ||报告日期 as report_id,
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
