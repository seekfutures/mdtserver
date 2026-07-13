import json

from flask import Blueprint, request

from auth import token_required
from extensions import get_db
from log import setup_logger
from responses import make_response
from utils.db import generate_id, rows_to_dicts

logger_system = setup_logger("logger_system")

bp = Blueprint("disease_groups", __name__)


def read_lob(value):
    """Return plain text for Oracle LOB values."""
    return value.read() if hasattr(value, "read") else value


def load_json_value(value, default=None):
    """Parse JSON stored in CLOB/VARCHAR columns."""
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
    """Serialize rule engine structures for database storage."""
    return json.dumps(value or {}, ensure_ascii=False)


@bp.route("/group-roles", methods=["GET"])
@token_required
def list_group_roles():
    """Query disease-group member role dictionary."""
    _db, cursor = get_db()
    try:
        cursor.execute(
            """
            select code, name
              from mdt_zd_role_groups
             order by code
            """
        )
        return make_response(
            res_code="ok",
            res_message="查询成功",
            output={"items": rows_to_dicts(cursor, cursor.fetchall())},
            status_code=200,
        )
    except Exception as exc:
        logger_system.error(f"Database error during group role query: {str(exc)}")
        return make_response(
            res_code="error",
            res_message="组内角色查询失败",
            output=str(exc),
            status_code=500,
        )


@bp.route("/icd-codes", methods=["GET"])
@bp.route("/icd_codes", methods=["GET"])
@token_required
def search_icd_codes():
    """Fuzzy query ICD codes by code or name."""
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
            select code, name
              from (
                    select code, name
                      from mdt_zd_icd_code
                     where upper(code) like upper(:keyword)
                        or upper(name) like upper(:keyword)
                     order by code
                   )
             where rownum <= :limit
            """,
            {"keyword": f"%{keyword}%", "limit": limit},
        )
        items = [
            {
                "code": row["code"],
                "name": row["name"],
                "icd_code": row["code"],
                "icd_name": row["name"],
            }
            for row in rows_to_dicts(cursor, cursor.fetchall())
        ]
        return make_response(
            res_code="ok",
            res_message="查询成功",
            output={"items": items},
            status_code=200,
        )
    except Exception as exc:
        logger_system.error(f"Database error during ICD code query: {str(exc)}")
        return make_response(
            res_code="error",
            res_message="ICD编码查询失败",
            output=str(exc),
            status_code=500,
        )


@bp.route("/disease-groups", methods=["GET"])
@bp.route("/disease_groups", methods=["GET"])
@token_required
def list_disease_groups():
    """Query disease groups with rules, materials and members."""
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
        groups = rows_to_dicts(cursor, cursor.fetchall())
        group_ids = [group["group_id"] for group in groups]

        keywords_by_group = {group_id: [] for group_id in group_ids}
        rules_by_group = {group_id: [] for group_id in group_ids}
        members_by_group = {group_id: [] for group_id in group_ids}

        if group_ids:
            bind_names = [f":id_{index}" for index in range(len(group_ids))]
            bind_params = {
                f"id_{index}": group_id for index, group_id in enumerate(group_ids)
            }
            in_clause = ", ".join(bind_names)

            cursor.execute(
                f"""
                select group_id, icd_code, icd_name, keyword_type, sort_no
                  from mdt_disease_group_keyword
                 where group_id in ({in_clause})
                 order by group_id, sort_no, icd_code
                """,
                bind_params,
            )
            for row in rows_to_dicts(cursor, cursor.fetchall()):
                if row.get("keyword_type") in (None, "DISEASE"):
                    keywords_by_group.setdefault(row["group_id"], []).append(
                        {
                            "icd_code": row.get("icd_code") or "",
                            "icd_name": row.get("icd_name")
                            or row.get("icd_code")
                            or "",
                        }
                    )

            cursor.execute(
                f"""
                select rule_id,
                       group_id,
                       rule_name,
                       logic_type,
                       engine_conditions,
                       action_type,
                       action_message,
                       enabled_flag,
                       sort_no
                  from mdt_disease_admission_rule
                 where group_id in ({in_clause})
                   and nvl(enabled_flag, '1') = '1'
                 order by group_id, sort_no, rule_id
                """,
                bind_params,
            )
            rule_rows = rows_to_dicts(cursor, cursor.fetchall())

            for row in rule_rows:
                rules_by_group.setdefault(row["group_id"], []).append(
                    {
                        "rule_id": row["rule_id"],
                        "rule_name": row.get("rule_name") or "",
                        "logic": row.get("logic_type") or "AND",
                        "engine_conditions": load_json_value(
                            row.get("engine_conditions"), {}
                        ),
                        "action_type": row.get("action_type") or "REJECT",
                        "action_message": read_lob(row.get("action_message")) or "",
                        "enabled_flag": row.get("enabled_flag") or "1",
                        "sort_no": row.get("sort_no") or 0,
                    }
                )

            cursor.execute(
                f"""
                select m.member_id,
                       m.group_id,
                       m.user_id,
                       u.user_name,
                       m.member_role,
                       m.duty_title,
                       m.sort_no,
                       m.enabled_flag
                  from mdt_disease_group_member m
                  left join mdt_user u on u.user_id = m.user_id
                 where m.group_id in ({in_clause})
                   and nvl(m.enabled_flag, '1') = '1'
                 order by m.group_id, m.sort_no, m.member_id
                """,
                bind_params,
            )
            for row in rows_to_dicts(cursor, cursor.fetchall()):
                members_by_group.setdefault(row["group_id"], []).append(row)

        items = []
        for group in groups:
            group_id = group["group_id"]
            items.append(
                {
                    "discipline_id": group["discipline_id"],
                    "discipline": group["discipline_name"],
                    "group_id": group_id,
                    "group_name": group["group_name"],
                    "rule_enabled": group.get("rule_enabled") or "1",
                    "related_diseases": keywords_by_group.get(group_id, []),
                    "rules": rules_by_group.get(group_id, []),
                    "members": members_by_group.get(group_id, []),
                    "sort_no": group.get("sort_no") or 0,
                }
            )

        return make_response(
            res_code="ok",
            res_message="查询成功",
            output={"items": items},
            status_code=200,
        )
    except Exception as exc:
        logger_system.error(f"Database error during disease group query: {str(exc)}")
        return make_response(
            res_code="error",
            res_message="病种分组查询失败",
            output=str(exc),
            status_code=500,
        )


@bp.route("/disease-groups/<group_id>/members", methods=["GET"])
@bp.route("/disease_groups/<group_id>/members", methods=["GET"])
@token_required
def list_disease_group_members(group_id):
    """Query active members for one disease group from employee master data."""
    _db, cursor = get_db()
    group_id = (group_id or "").strip()
    if not group_id:
        return make_response(
            res_code="error",
            res_message="参数错误",
            output="Missing group_id",
            status_code=400,
        )

    try:
        cursor.execute(
            """
            select m.member_id,
                   m.group_id,
                   m.user_id,
                   e.user_name,
                   m.member_role,
                   r.name as member_role_name,
                   m.duty_title,
                   m.sort_no,
                   m.enabled_flag
              from mdt_disease_group_member m
              left join mdt_a_employee_mi e on e.user_id = m.user_id
              left join mdt_zd_role_groups r on r.code = m.member_role
             where m.group_id = :group_id
               and nvl(m.enabled_flag, '1') = '1'
             order by m.sort_no, m.member_id
            """,
            {"group_id": group_id},
        )
        rows = rows_to_dicts(cursor, cursor.fetchall())
        return make_response(
            res_code="ok",
            res_message="查询成功",
            output={"items": rows},
            status_code=200,
        )
    except Exception as exc:
        logger_system.error(f"Database error during group member query: {str(exc)}")
        return make_response(
            res_code="error",
            res_message="病种组成员查询失败",
            output=str(exc),
            status_code=500,
        )


@bp.route("/disease-groups", methods=["POST", "PUT"])
@bp.route("/disease_groups", methods=["POST", "PUT"])
@token_required
def save_disease_group():
    """Create or update disease group configuration."""
    db, cursor = get_db()
    data = request.get_json(silent=True) or {}

    discipline_id = (data.get("discipline_id") or "").strip()
    discipline_name = (
        data.get("discipline") or data.get("discipline_name") or ""
    ).strip()
    group_id = (data.get("group_id") or "").strip()
    group_name = (data.get("name") or data.get("group_name") or "").strip()

    if not discipline_name or not group_name:
        return make_response(
            res_code="error",
            res_message="参数错误",
            output="Missing discipline or group name",
            status_code=400,
        )

    try:
        if not discipline_id:
            cursor.execute(
                "select discipline_id from mdt_discipline_group where discipline_name = :name",
                {"name": discipline_name},
            )
            row = cursor.fetchone()
            discipline_id = row[0] if row else generate_id("DISC")

        cursor.execute(
            "select count(1) from mdt_discipline_group where discipline_id = :discipline_id",
            {"discipline_id": discipline_id},
        )
        if cursor.fetchone()[0] == 0:
            cursor.execute(
                """
                insert into mdt_discipline_group
                    (discipline_id, discipline_name, enabled_flag, created_at, updated_at)
                values
                    (:discipline_id, :discipline_name, '1', sysdate, sysdate)
                """,
                {"discipline_id": discipline_id, "discipline_name": discipline_name},
            )
        else:
            cursor.execute(
                """
                update mdt_discipline_group
                   set discipline_name = :discipline_name,
                       updated_at = sysdate
                 where discipline_id = :discipline_id
                """,
                {"discipline_id": discipline_id, "discipline_name": discipline_name},
            )

        if not group_id:
            cursor.execute(
                """
                select group_id
                  from mdt_disease_group
                 where discipline_id = :discipline_id
                   and group_name = :group_name
                """,
                {"discipline_id": discipline_id, "group_name": group_name},
            )
            row = cursor.fetchone()
            group_id = row[0] if row else generate_id("DG")

        group_params = {
            "group_id": group_id,
            "discipline_id": discipline_id,
            "group_name": group_name,
            "rule_enabled": "1"
            if data.get("rule_enabled") in (True, "1", "true", "开", "启用")
            else "0",
        }
        cursor.execute(
            "select count(1) from mdt_disease_group where group_id = :group_id",
            {"group_id": group_id},
        )
        if cursor.fetchone()[0] == 0:
            cursor.execute(
                """
                insert into mdt_disease_group
                    (group_id, discipline_id, group_name, rule_enabled,
                     enabled_flag, created_at, updated_at)
                values
                    (:group_id, :discipline_id, :group_name, :rule_enabled,
                     '1', sysdate, sysdate)
                """,
                group_params,
            )
        else:
            cursor.execute(
                """
                update mdt_disease_group
                   set discipline_id = :discipline_id,
                       group_name = :group_name,
                       rule_enabled = :rule_enabled,
                       updated_at = sysdate
                 where group_id = :group_id
                """,
                group_params,
            )

        cursor.execute(
            "delete from mdt_disease_group_keyword where group_id = :group_id",
            {"group_id": group_id},
        )
        for index, keyword in enumerate(data.get("related_diseases") or [], start=1):
            if isinstance(keyword, dict):
                icd_code = (
                    keyword.get("icd_code")
                    or keyword.get("code")
                    or keyword.get("icd_name")
                    or keyword.get("name")
                    or ""
                )
                icd_name = (
                    keyword.get("icd_name")
                    or keyword.get("name")
                    or keyword.get("keyword")
                    or icd_code
                )
                keyword_type = keyword.get("keyword_type") or "DISEASE"
            else:
                icd_name = str(keyword).strip()
                icd_code = icd_name
                keyword_type = "DISEASE"
            icd_code = str(icd_code).strip()[:20]
            icd_name = str(icd_name).strip()
            if not icd_code or not icd_name:
                continue
            cursor.execute(
                """
                insert into mdt_disease_group_keyword
                    (icd_code, group_id, icd_name, keyword_type, sort_no)
                values
                    (:icd_code, :group_id, :icd_name, :keyword_type, :sort_no)
                """,
                {
                    "icd_code": icd_code,
                    "group_id": group_id,
                    "icd_name": icd_name,
                    "keyword_type": keyword_type,
                    "sort_no": index,
                },
            )

        cursor.execute(
            "delete from mdt_disease_admission_rule where group_id = :group_id",
            {"group_id": group_id},
        )
        for index, rule in enumerate(data.get("rules") or [], start=1):
            rule_id = rule.get("rule_id") or generate_id("RULE")
            logic_type = rule.get("logic") or rule.get("logic_type") or "AND"
            engine_conditions = (
                rule.get("engine_conditions")
                or rule.get("conditions")
                or {"all" if logic_type == "AND" else "any": []}
            )
            cursor.execute(
                """
                insert into mdt_disease_admission_rule
                    (rule_id, group_id, rule_name, logic_type, engine_conditions,
                     action_type, action_message, enabled_flag, sort_no,
                     created_at, updated_at)
                values
                    (:rule_id, :group_id, :rule_name, :logic_type, :engine_conditions,
                     :action_type, :action_message, :enabled_flag, :sort_no,
                     sysdate, sysdate)
                """,
                {
                    "rule_id": rule_id,
                    "group_id": group_id,
                    "rule_name": rule.get("rule_name") or "",
                    "logic_type": logic_type,
                    "engine_conditions": dump_json_value(engine_conditions),
                    "action_type": rule.get("action_type") or "REJECT",
                    "action_message": rule.get("action_message") or "",
                    "enabled_flag": rule.get("enabled_flag") or "1",
                    "sort_no": rule.get("sort_no") or index,
                },
            )

        cursor.execute(
            "delete from mdt_disease_group_member where group_id = :group_id",
            {"group_id": group_id},
        )
        for index, member in enumerate(data.get("members") or [], start=1):
            user_id = (member.get("user_id") or "").strip()
            if not user_id:
                continue
            cursor.execute(
                """
                insert into mdt_disease_group_member
                    (member_id, group_id, user_id, member_role, duty_title,
                     sort_no, enabled_flag, created_at, updated_at)
                values
                    (:member_id, :group_id, :user_id, :member_role, :duty_title,
                     :sort_no, :enabled_flag, sysdate, sysdate)
                """,
                {
                    "member_id": f"{group_id}_{user_id}",
                    "group_id": group_id,
                    "user_id": user_id,
                    "member_role": member.get("member_role") or "MEMBER",
                    "duty_title": member.get("duty_title") or "",
                    "sort_no": member.get("sort_no") or index,
                    "enabled_flag": member.get("enabled_flag") or "1",
                },
            )

        db.commit()
        return make_response(
            res_code="ok",
            res_message="保存成功",
            output={"discipline_id": discipline_id, "group_id": group_id},
            status_code=200,
        )
    except Exception as exc:
        db.rollback()
        logger_system.error(f"Database error during disease group save: {str(exc)}")
        return make_response(
            res_code="error",
            res_message="病种分组保存失败",
            output=str(exc),
            status_code=500,
        )


@bp.route("/disease-groups/<group_id>", methods=["DELETE"])
@bp.route("/disease_groups/<group_id>", methods=["DELETE"])
@token_required
def delete_disease_group(group_id):
    """Delete one disease group and related configuration."""
    db, cursor = get_db()
    group_id = (group_id or "").strip()
    if not group_id:
        return make_response(
            res_code="error",
            res_message="参数错误",
            output="Missing group_id",
            status_code=400,
        )

    try:
        cursor.execute(
            "select count(1) from mdt_disease_group where group_id = :group_id",
            {"group_id": group_id},
        )
        if cursor.fetchone()[0] == 0:
            return make_response(
                res_code="error",
                res_message="病种分组不存在",
                output=f"group_id {group_id} does not exist",
                status_code=404,
            )

        cursor.execute(
            "delete from mdt_disease_admission_rule where group_id = :group_id",
            {"group_id": group_id},
        )
        cursor.execute(
            "delete from mdt_disease_group_keyword where group_id = :group_id",
            {"group_id": group_id},
        )
        cursor.execute(
            "delete from mdt_disease_group_member where group_id = :group_id",
            {"group_id": group_id},
        )
        cursor.execute(
            "delete from mdt_disease_group where group_id = :group_id",
            {"group_id": group_id},
        )
        db.commit()
        return make_response(
            res_code="ok",
            res_message="删除成功",
            output={"group_id": group_id},
            status_code=200,
        )
    except Exception as exc:
        db.rollback()
        logger_system.error(f"Database error during disease group delete: {str(exc)}")
        return make_response(
            res_code="error",
            res_message="病种分组删除失败",
            output=str(exc),
            status_code=500,
        )
