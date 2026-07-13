import datetime

from flask import Blueprint, request

from auth import token_required
from extensions import get_db
from log import setup_logger
from responses import make_response
from utils.db import generate_id, rows_to_dicts

logger_system = setup_logger("logger_system")

bp = Blueprint("schedules", __name__)


def parse_date(value, field_name):
    try:
        return datetime.datetime.strptime(value, "%Y-%m-%d").date()
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be YYYY-MM-DD") from exc


def each_day(start_date, end_date):
    current = start_date
    while current <= end_date:
        yield current
        current += datetime.timedelta(days=1)


def fetch_template(cursor, template_id):
    cursor.execute(
        """
        select template_id,
               group_id,
               group_name,
               template_name,
               start_time,
               end_time,
               capacity
          from mdt_schedule_template
         where template_id = :template_id
        """,
        {"template_id": template_id},
    )
    rows = rows_to_dicts(cursor, cursor.fetchall())
    return rows[0] if rows else None


def fetch_template_experts(cursor, template_id):
    cursor.execute(
        """
        select dept_code,
               dept_name,
               expert_user_id,
               expert_name,
               resident_flag,
               sort_no
          from mdt_schedule_template_expert
         where template_id = :template_id
         order by sort_no, dept_name, expert_name
        """,
        {"template_id": template_id},
    )
    return rows_to_dicts(cursor, cursor.fetchall())


def normalize_experts(experts):
    normalized = []
    for index, expert in enumerate(experts or [], start=1):
        if not isinstance(expert, dict):
            continue
        dept_name = str(expert.get("dept_name") or "").strip()
        expert_name = str(expert.get("expert_name") or "").strip()
        if not dept_name:
            continue
        normalized.append(
            {
                "dept_code": str(expert.get("dept_code") or "").strip(),
                "dept_name": dept_name,
                "expert_user_id": str(expert.get("expert_user_id") or "").strip(),
                "expert_name": expert_name,
                "resident_flag": str(expert.get("resident_flag") or "1").strip(),
                "sort_no": int(expert.get("sort_no") or index),
            }
        )
    return normalized


@bp.route("/schedule-templates", methods=["GET"])
@token_required
def list_schedule_templates():
    _db, cursor = get_db()
    group_id = (request.args.get("group_id") or "").strip()
    params = {}
    where_sql = "where nvl(enabled_flag, '1') = '1'"
    if group_id:
        where_sql += " and group_id = :group_id"
        params["group_id"] = group_id

    try:
        cursor.execute(
            f"""
            select template_id,
                   group_id,
                   group_name,
                   template_name,
                   start_time,
                   end_time,
                   capacity,
                   enabled_flag
              from mdt_schedule_template
              {where_sql}
             order by group_id, start_time, template_name
            """,
            params,
        )
        items = rows_to_dicts(cursor, cursor.fetchall())
        for item in items:
            item["experts"] = fetch_template_experts(cursor, item["template_id"])
        return make_response(
            res_code="ok",
            res_message="查询成功",
            output={"items": items},
            status_code=200,
        )
    except Exception as exc:
        logger_system.error(f"Database error during template query: {str(exc)}")
        return make_response(
            res_code="error",
            res_message="排班模板查询失败",
            output=str(exc),
            status_code=500,
        )


@bp.route("/schedule-templates", methods=["POST"])
@token_required
def save_schedule_template():
    db, cursor = get_db()
    data = request.get_json(silent=True) or {}
    template_id = (data.get("template_id") or "").strip() or generate_id("SCHT")
    group_id = (data.get("group_id") or "").strip()
    group_name = (data.get("group_name") or "").strip()
    template_name = (data.get("template_name") or "").strip()
    start_time = (data.get("start_time") or "").strip()
    end_time = (data.get("end_time") or "").strip()
    capacity = int(data.get("capacity") or 0)
    experts = normalize_experts(data.get("experts") or [])

    if not group_id or not template_name or not start_time or not end_time or capacity <= 0:
        return make_response(
            res_code="error",
            res_message="参数错误",
            output="Missing group_id/template_name/start_time/end_time/capacity",
            status_code=400,
        )

    try:
        cursor.execute(
            "select count(1) from mdt_schedule_template where template_id = :template_id",
            {"template_id": template_id},
        )
        params = {
            "template_id": template_id,
            "group_id": group_id,
            "group_name": group_name,
            "template_name": template_name,
            "start_time": start_time,
            "end_time": end_time,
            "capacity": capacity,
        }
        if cursor.fetchone()[0] == 0:
            cursor.execute(
                """
                insert into mdt_schedule_template
                    (template_id, group_id, group_name, template_name, start_time, end_time,
                     capacity, enabled_flag, created_at, updated_at)
                values
                    (:template_id, :group_id, :group_name, :template_name, :start_time, :end_time,
                     :capacity, '1', sysdate, sysdate)
                """,
                params,
            )
        else:
            cursor.execute(
                """
                update mdt_schedule_template
                   set group_id = :group_id,
                       group_name = :group_name,
                       template_name = :template_name,
                       start_time = :start_time,
                       end_time = :end_time,
                       capacity = :capacity,
                       updated_at = sysdate
                 where template_id = :template_id
                """,
                params,
            )

        cursor.execute(
            "delete from mdt_schedule_template_expert where template_id = :template_id",
            {"template_id": template_id},
        )
        for expert in experts:
            cursor.execute(
                """
                insert into mdt_schedule_template_expert
                    (template_id, dept_code, dept_name, expert_user_id, expert_name,
                     resident_flag, sort_no)
                values
                    (:template_id, :dept_code, :dept_name, :expert_user_id, :expert_name,
                     :resident_flag, :sort_no)
                """,
                {"template_id": template_id, **expert},
            )

        db.commit()
        return make_response(
            res_code="ok",
            res_message="模板保存成功",
            output={"template_id": template_id},
            status_code=200,
        )
    except Exception as exc:
        db.rollback()
        logger_system.error(f"Database error during template save: {str(exc)}")
        return make_response(
            res_code="error",
            res_message="排班模板保存失败",
            output=str(exc),
            status_code=500,
        )


@bp.route("/schedules/generate", methods=["POST"])
@token_required
def generate_schedules():
    db, cursor = get_db()
    data = request.get_json(silent=True) or {}
    group_id = (data.get("group_id") or "").strip()
    group_name = (data.get("group_name") or "").strip()
    template_id = (data.get("template_id") or "").strip()

    try:
        start_date = parse_date(data.get("start_date"), "start_date")
        end_date = parse_date(data.get("end_date"), "end_date")
    except ValueError as exc:
        return make_response(
            res_code="error",
            res_message="参数错误",
            output=str(exc),
            status_code=400,
        )

    if not group_id or not template_id or start_date > end_date:
        return make_response(
            res_code="error",
            res_message="参数错误",
            output="Missing group_id/template_id or invalid date range",
            status_code=400,
        )

    try:
        template = fetch_template(cursor, template_id)
        if not template:
            return make_response(
                res_code="error",
                res_message="模板不存在",
                output=f"template_id {template_id} does not exist",
                status_code=404,
            )
        experts = fetch_template_experts(cursor, template_id)
        generated = []
        skipped = 0

        for day in each_day(start_date, end_date):
            if day.weekday() >= 5:
                skipped += 1
                continue
            cursor.execute(
                """
                select schedule_id
                  from mdt_schedule
                 where group_id = :group_id
                   and template_id = :template_id
                   and trunc(schedule_date) = :schedule_date
                """,
                {
                    "group_id": group_id,
                    "template_id": template_id,
                    "schedule_date": day,
                },
            )
            if cursor.fetchone():
                skipped += 1
                continue
            schedule_id = generate_id("SCH")
            cursor.execute(
                """
                insert into mdt_schedule
                    (schedule_id, group_id, group_name, template_id, schedule_date,
                     shift_name, start_time, end_time, capacity, status,
                     created_at, updated_at)
                values
                    (:schedule_id, :group_id, :group_name, :template_id, :schedule_date,
                     :shift_name, :start_time, :end_time, :capacity, 'OPEN',
                     sysdate, sysdate)
                """,
                {
                    "schedule_id": schedule_id,
                    "group_id": group_id,
                    "group_name": group_name,
                    "template_id": template_id,
                    "schedule_date": day,
                    "shift_name": template["template_name"],
                    "start_time": template["start_time"],
                    "end_time": template["end_time"],
                    "capacity": template["capacity"],
                },
            )
            for expert in experts:
                cursor.execute(
                    """
                    insert into mdt_schedule_expert
                        (schedule_id, dept_code, dept_name, expert_user_id, expert_name,
                         resident_flag, sort_no)
                    values
                        (:schedule_id, :dept_code, :dept_name, :expert_user_id,
                         :expert_name, :resident_flag, :sort_no)
                    """,
                    {"schedule_id": schedule_id, **expert},
                )
            generated.append(schedule_id)

        db.commit()
        return make_response(
            res_code="ok",
            res_message="排班生成成功",
            output={
                "schedule_ids": generated,
                "count": len(generated),
                "skipped": skipped,
            },
            status_code=200,
        )
    except Exception as exc:
        db.rollback()
        logger_system.error(f"Database error during schedule generation: {str(exc)}")
        return make_response(
            res_code="error",
            res_message="排班生成失败",
            output=str(exc),
            status_code=500,
        )


@bp.route("/schedules", methods=["GET"])
@token_required
def list_schedules():
    _db, cursor = get_db()
    group_id = (request.args.get("group_id") or "").strip()
    try:
        start_date = parse_date(request.args.get("start_date"), "start_date")
        end_date = parse_date(request.args.get("end_date"), "end_date")
    except ValueError as exc:
        return make_response(
            res_code="error",
            res_message="参数错误",
            output=str(exc),
            status_code=400,
        )

    try:
        params = {"start_date": start_date, "end_date": end_date}
        where_sql = "where schedule_date between :start_date and :end_date"
        if group_id:
            where_sql += " and group_id = :group_id"
            params["group_id"] = group_id
        cursor.execute(
            f"""
            select schedule_id,
                   group_id,
                   group_name,
                   template_id,
                   to_char(schedule_date, 'YYYY-MM-DD') as schedule_date,
                   shift_name,
                   start_time,
                   end_time,
                   capacity,
                   status
              from mdt_schedule
             {where_sql}
             order by schedule_date, start_time, group_name
            """,
            params,
        )
        items = rows_to_dicts(cursor, cursor.fetchall())
        schedule_ids = [item["schedule_id"] for item in items]
        experts_by_schedule = {schedule_id: [] for schedule_id in schedule_ids}
        counts_by_schedule = {
            schedule_id: {"booked_count": 0, "waitlist_count": 0}
            for schedule_id in schedule_ids
        }
        if schedule_ids:
            bind_names = [f":id_{index}" for index in range(len(schedule_ids))]
            bind_params = {
                f"id_{index}": schedule_id
                for index, schedule_id in enumerate(schedule_ids)
            }
            cursor.execute(
                f"""
                select schedule_id,
                       dept_code,
                       dept_name,
                       expert_user_id,
                       expert_name,
                       resident_flag,
                       sort_no
                  from mdt_schedule_expert
                 where schedule_id in ({", ".join(bind_names)})
                 order by schedule_id, sort_no, dept_name, expert_name
                """,
                bind_params,
            )
            for row in rows_to_dicts(cursor, cursor.fetchall()):
                experts_by_schedule.setdefault(row["schedule_id"], []).append(row)

            cursor.execute(
                f"""
                select schedule_id,
                       sum(
                           case
                               when nvl(appointment_type, 'REGULAR') <> 'WAITLIST'
                                and upper(nvl(status, '-')) not in
                                    ('DRAFT', 'REJECTED', 'CANCELLED',
                                     'WAITLIST', 'WAITLIST_FAILED')
                               then 1
                               else 0
                           end
                       ) as booked_count,
                       sum(
                           case
                               when nvl(appointment_type, 'REGULAR') = 'WAITLIST'
                                and upper(nvl(status, '-')) = 'WAITLIST'
                               then 1
                               else 0
                           end
                       ) as waitlist_count
                  from mdt_clinical_application
                 where schedule_id in ({", ".join(bind_names)})
                 group by schedule_id
                """,
                bind_params,
            )
            for row in rows_to_dicts(cursor, cursor.fetchall()):
                counts_by_schedule[row["schedule_id"]] = {
                    "booked_count": int(row.get("booked_count") or 0),
                    "waitlist_count": int(row.get("waitlist_count") or 0),
                }

        for item in items:
            item["experts"] = experts_by_schedule.get(item["schedule_id"], [])
            counts = counts_by_schedule.get(item["schedule_id"], {})
            item["booked_count"] = counts.get("booked_count", 0)
            item["waitlist_count"] = counts.get("waitlist_count", 0)
        return make_response(
            res_code="ok",
            res_message="查询成功",
            output={"items": items},
            status_code=200,
        )
    except Exception as exc:
        logger_system.error(f"Database error during schedule query: {str(exc)}")
        return make_response(
            res_code="error",
            res_message="排班查询失败",
            output=str(exc),
            status_code=500,
        )


@bp.route("/schedules/<schedule_id>", methods=["PUT"])
@token_required
def update_schedule(schedule_id):
    db, cursor = get_db()
    data = request.get_json(silent=True) or {}
    schedule_id = (schedule_id or "").strip()
    capacity = int(data.get("capacity") or 0)
    status = (data.get("status") or "OPEN").strip()
    experts = normalize_experts(data.get("experts") or [])
    if not schedule_id or capacity <= 0:
        return make_response(
            res_code="error",
            res_message="参数错误",
            output="Missing schedule_id/capacity",
            status_code=400,
        )

    try:
        cursor.execute(
            """
            update mdt_schedule
               set capacity = :capacity,
                   status = :status,
                   updated_at = sysdate
             where schedule_id = :schedule_id
            """,
            {"schedule_id": schedule_id, "capacity": capacity, "status": status},
        )
        if cursor.rowcount == 0:
            return make_response(
                res_code="error",
                res_message="排班不存在",
                output=f"schedule_id {schedule_id} does not exist",
                status_code=404,
            )
        cursor.execute(
            "delete from mdt_schedule_expert where schedule_id = :schedule_id",
            {"schedule_id": schedule_id},
        )
        for expert in experts:
            cursor.execute(
                """
                insert into mdt_schedule_expert
                    (schedule_id, dept_code, dept_name, expert_user_id, expert_name,
                     resident_flag, sort_no)
                values
                    (:schedule_id, :dept_code, :dept_name, :expert_user_id,
                     :expert_name, :resident_flag, :sort_no)
                """,
                {"schedule_id": schedule_id, **expert},
            )
        db.commit()
        return make_response(
            res_code="ok",
            res_message="排班已更新",
            output={"schedule_id": schedule_id},
            status_code=200,
        )
    except Exception as exc:
        db.rollback()
        logger_system.error(f"Database error during schedule update: {str(exc)}")
        return make_response(
            res_code="error",
            res_message="排班更新失败",
            output=str(exc),
            status_code=500,
        )
