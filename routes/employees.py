import base64
import json

from flask import Blueprint, g, request
from werkzeug.security import generate_password_hash

from auth import token_required
from extensions import get_db
from log import setup_logger
from responses import make_response
from utils.db import rows_to_dicts

logger_system = setup_logger("logger_system")

bp = Blueprint("employees", __name__)


def build_menu_tree(rows, allowed_menu_ids=None):
    nodes = {}
    children_by_parent = {}
    for row in rows:
        menu_id = str(row.get("menu_id") or "").strip()
        if not menu_id:
            continue
        parent_id = str(row.get("parent_id") or "").strip()
        node = {
            "menu_id": menu_id,
            "name": str(row.get("menu_name") or "").strip(),
            "children": [],
        }
        nodes[menu_id] = node
        children_by_parent.setdefault(parent_id, []).append(node)

    for menu_id, node in nodes.items():
        node["children"] = children_by_parent.get(menu_id, [])

    def filter_allowed(node):
        children = [
            child
            for child in (filter_allowed(child) for child in node.get("children", []))
            if child is not None
        ]
        is_allowed = allowed_menu_ids is None or node["menu_id"] in allowed_menu_ids
        if not is_allowed and not children:
            return None
        return {"menu_id": node["menu_id"], "name": node["name"], "children": children}

    roots = children_by_parent.get("", [])
    return [
        node
        for node in (filter_allowed(root) for root in roots)
        if node is not None and (node.get("name") or node.get("children"))
    ]


@bp.route("/employees", methods=["GET"])
@token_required
def list_employees():
    """Query employee records from mdt_a_employee_mi."""
    _db, cursor = get_db()
    keyword = (request.args.get("keyword") or "").strip()
    page = max(int(request.args.get("page", 1)), 1)
    page_size = min(max(int(request.args.get("page_size", 500)), 1), 1000)
    offset = (page - 1) * page_size

    where_sql = ""
    filter_params = {}
    if keyword:
        where_sql = """
            where user_id like :keyword
               or user_name like :keyword
               or dept_name like :keyword
               or role_name like :keyword
               or mobile like :keyword
        """
        filter_params["keyword"] = f"%{keyword}%"

    try:
        count_sql = f"select count(1) from mdt_a_employee_mi {where_sql}"
        cursor.execute(count_sql, filter_params)
        total = cursor.fetchone()[0]

        query = f"""
            select user_id,
                   user_name,
                   dept_code,
                   dept_name,
                   role_code,
                   role_name,
                   role_name as role,
                   mobile,
                   flag,
                   comment_
              from mdt_a_employee_mi
              {where_sql}
             order by user_id
             offset :offset rows fetch next :limit rows only
        """
        query_params = dict(filter_params, offset=offset, limit=page_size)
        cursor.execute(query, query_params)
        columns = [
            "user_id",
            "user_name",
            "dept_code",
            "dept_name",
            "role_code",
            "role_name",
            "role",
            "mobile",
            "flag",
            "comment_",
        ]
        items = [dict(zip(columns, row)) for row in cursor.fetchall()]
        return make_response(
            res_code="ok",
            res_message="查询成功",
            output={
                "items": items,
                "total": total,
                "page": page,
                "page_size": page_size,
            },
            status_code=200,
        )
    except Exception as exc:
        logger_system.error(f"Database error during employee query: {str(exc)}")
        return make_response(
            res_code="error",
            res_message="员工查询失败",
            output=str(exc),
            status_code=500,
        )


@bp.route("/departments", methods=["GET"])
@token_required
def list_departments():
    """Query department dictionary from mdt_zd_unit_code."""
    _db, cursor = get_db()
    try:
        cursor.execute(
            """
            select code, name
              from mdt_zd_unit_code
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
        logger_system.error(f"Database error during department query: {str(exc)}")
        return make_response(
            res_code="error",
            res_message="部门查询失败",
            output=str(exc),
            status_code=500,
        )


@bp.route("/employee-roles", methods=["GET"])
@token_required
def list_employee_roles():
    """Query employee role dictionary from mdt_zd_role_employee."""
    _db, cursor = get_db()
    try:
        cursor.execute(
            """
            select code, name
              from mdt_zd_role_employee
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
        logger_system.error(f"Database error during employee role query: {str(exc)}")
        return make_response(
            res_code="error",
            res_message="角色查询失败",
            output=str(exc),
            status_code=500,
        )


@bp.route("/menus", methods=["GET"])
@token_required
def list_menus():
    """Query system menu tree from mdt_sys_menu."""
    _db, cursor = get_db()
    try:
        cursor.execute(
            """
            select menu_id,
                   parent_id,
                   menu_name,
                   sort_no
              from mdt_sys_menu
             where nvl(enabled_flag, '1') = '1'
             order by nvl(sort_no, 0), menu_name
            """
        )
        rows = rows_to_dicts(cursor, cursor.fetchall())
        tree = build_menu_tree(rows)
        return make_response(
            res_code="ok",
            res_message="查询成功",
            output={"items": tree},
            status_code=200,
        )
    except Exception as exc:
        logger_system.error(f"Database error during menu query: {str(exc)}")
        return make_response(
            res_code="error",
            res_message="目录查询失败",
            output=str(exc),
            status_code=500,
        )


@bp.route("/my-menus", methods=["GET"])
@token_required
def list_my_menus():
    """Query menu tree allowed for the current login user."""
    _db, cursor = get_db()
    user_id = g.user_id
    try:
        cursor.execute(
            """
            select role_code
              from mdt_a_employee_mi
             where user_id = :user_id
            """,
            {"user_id": user_id},
        )
        row = cursor.fetchone()
        role_code = row[0] if row else ""
        if not role_code:
            return make_response(
                res_code="ok",
                res_message="查询成功",
                output={"items": []},
                status_code=200,
            )

        cursor.execute(
            """
            select menu_id
              from mdt_role_menu_permission
             where role_code = :role_code
               and can_view = '1'
            """,
            {"role_code": role_code},
        )
        allowed_menu_ids = {str(row[0]) for row in cursor.fetchall() if row[0]}
        if not allowed_menu_ids:
            return make_response(
                res_code="ok",
                res_message="查询成功",
                output={"items": []},
                status_code=200,
            )

        cursor.execute(
            """
            select menu_id,
                   parent_id,
                   menu_name,
                   sort_no
              from mdt_sys_menu
             where nvl(enabled_flag, '1') = '1'
             order by nvl(sort_no, 0), menu_name
            """
        )
        rows = rows_to_dicts(cursor, cursor.fetchall())
        tree = build_menu_tree(rows, allowed_menu_ids)
        return make_response(
            res_code="ok",
            res_message="查询成功",
            output={"items": tree, "role_code": role_code},
            status_code=200,
        )
    except Exception as exc:
        logger_system.error(f"Database error during current user menu query: {str(exc)}")
        return make_response(
            res_code="error",
            res_message="当前用户目录查询失败",
            output=str(exc),
            status_code=500,
        )


@bp.route("/role-menu-permissions", methods=["GET"])
@token_required
def get_role_menu_permissions():
    """Query menu permissions for one employee role."""
    _db, cursor = get_db()
    role_code = (request.args.get("role_code") or "").strip()
    if not role_code:
        return make_response(
            res_code="error",
            res_message="参数错误",
            output="Missing role_code",
            status_code=400,
        )

    try:
        cursor.execute(
            """
            select menu_id
              from mdt_role_menu_permission
             where role_code = :role_code
               and can_view = '1'
             order by menu_id
            """,
            {"role_code": role_code},
        )
        menu_ids = [row[0] for row in cursor.fetchall()]
        return make_response(
            res_code="ok",
            res_message="查询成功",
            output={"role_code": role_code, "menu_ids": menu_ids},
            status_code=200,
        )
    except Exception as exc:
        logger_system.error(f"Database error during role menu query: {str(exc)}")
        return make_response(
            res_code="error",
            res_message="目录权限查询失败",
            output=str(exc),
            status_code=500,
        )


@bp.route("/role-menu-permissions", methods=["POST"])
@token_required
def save_role_menu_permissions():
    """Save menu permissions for one employee role."""
    db, cursor = get_db()
    data = request.get_json(silent=True) or {}
    role_code = (data.get("role_code") or "").strip()
    menu_ids = data.get("menu_ids") or []

    if not role_code:
        return make_response(
            res_code="error",
            res_message="参数错误",
            output="Missing role_code",
            status_code=400,
        )
    if not isinstance(menu_ids, list):
        return make_response(
            res_code="error",
            res_message="参数错误",
            output="menu_ids must be a list",
            status_code=400,
        )

    unique_menu_ids = []
    seen_menu_ids = set()
    for menu_id in menu_ids:
        menu_id = str(menu_id).strip()
        if menu_id and menu_id not in seen_menu_ids:
            unique_menu_ids.append(menu_id)
            seen_menu_ids.add(menu_id)

    try:
        cursor.execute(
            "delete from mdt_role_menu_permission where role_code = :role_code",
            {"role_code": role_code},
        )
        for menu_id in unique_menu_ids:
            cursor.execute(
                """
                insert into mdt_role_menu_permission
                    (role_code, menu_id, can_view, created_at, updated_at)
                values
                    (:role_code, :menu_id, '1', sysdate, sysdate)
                """,
                {"role_code": role_code, "menu_id": menu_id},
            )
        db.commit()
        return make_response(
            res_code="ok",
            res_message="目录权限保存成功",
            output={"role_code": role_code, "menu_count": len(unique_menu_ids)},
            status_code=200,
        )
    except Exception as exc:
        db.rollback()
        logger_system.error(f"Database error during role menu save: {str(exc)}")
        return make_response(
            res_code="error",
            res_message="目录权限保存失败",
            output=str(exc),
            status_code=500,
        )


@bp.route("/users", methods=["GET"])
@token_required
def list_users():
    """Fuzzy query enabled users from mdt_user."""
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
            select user_id, user_name
              from (
                    select user_id, user_name
                      from mdt_user
                     where (user_id like :keyword or user_name like :keyword)
                       and nvl(open_flag, '1') in ('1', '已启用')
                     order by user_id
                   )
             where rownum <= :limit
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
        logger_system.error(f"Database error during user query: {str(exc)}")
        return make_response(
            res_code="error",
            res_message="成员查询失败",
            output=str(exc),
            status_code=500,
        )




@bp.route("/employee-face-binding", methods=["GET"])
@token_required
def get_employee_face_binding_status():
    """Return whether a doctor already has bound face data."""
    _db, cursor = get_db()
    doctor_id = str(request.args.get("doctor_id") or g.user_id or "").strip()
    if not doctor_id:
        return make_response(
            res_code="error",
            res_message="参数错误",
            output="Missing doctor_id",
            status_code=400,
        )

    try:
        cursor.execute(
            """
            select user_id,
                   dbms_lob.getlength(face_token) as face_token_length,
                   dbms_lob.getlength(avatar_base64) as avatar_size,
                   avatar_base64
              from mdt_a_employee_mi_image
             where user_id = :user_id
               and face_token is not null
               and avatar_base64 is not null
            """,
            {"user_id": doctor_id},
        )
        row = cursor.fetchone()
        if not row:
            return make_response(
                res_code="ok",
                res_message="查询成功",
                output={"doctor_id": doctor_id, "bound": False},
                status_code=200,
            )
        avatar_blob = row[3]
        avatar_bytes = avatar_blob.read() if hasattr(avatar_blob, "read") else avatar_blob
        avatar_base64 = base64.b64encode(avatar_bytes or b"").decode("ascii")
        return make_response(
            res_code="ok",
            res_message="查询成功",
            output={
                "doctor_id": row[0],
                "bound": True,
                "face_token_length": row[1],
                "avatar_size": row[2],
                "avatar_base64": "data:image/jpeg;base64," + avatar_base64,
            },
            status_code=200,
        )
    except Exception as exc:
        logger_system.error(f"Database error during face binding status query: {str(exc)}")
        return make_response(
            res_code="error",
            res_message="人脸绑定状态查询失败",
            output=str(exc),
            status_code=500,
        )


@bp.route("/employee-face-binding", methods=["POST"])
@token_required
def save_employee_face_binding():
    """Save face token and small avatar for doctor check-in."""
    db, cursor = get_db()
    data = request.get_json(silent=True) or {}

    doctor_id = str(data.get("doctor_id") or data.get("user_id") or "").strip()
    face_token = data.get("face_token")
    avatar_base64 = str(data.get("avatar_base64") or "").strip()

    if not doctor_id:
        return make_response(
            res_code="error",
            res_message="参数错误",
            output="Missing doctor_id",
            status_code=400,
        )
    if not isinstance(face_token, list) or len(face_token) != 128:
        return make_response(
            res_code="error",
            res_message="参数错误",
            output="face_token must be a 128-dimension list",
            status_code=400,
        )
    try:
        normalized_token = [float(value) for value in face_token]
    except (TypeError, ValueError) as exc:
        return make_response(
            res_code="error",
            res_message="参数错误",
            output=f"face_token contains non-numeric value: {exc}",
            status_code=400,
        )

    if "," in avatar_base64:
        avatar_base64 = avatar_base64.split(",", 1)[1]
    try:
        avatar_bytes = base64.b64decode(avatar_base64, validate=True)
    except Exception as exc:
        return make_response(
            res_code="error",
            res_message="参数错误",
            output=f"avatar_base64 is invalid: {exc}",
            status_code=400,
        )
    if len(avatar_bytes) > 10 * 1024:
        return make_response(
            res_code="error",
            res_message="参数错误",
            output="avatar_base64 must be compressed to 10KB or less",
            status_code=400,
        )

    try:
        cursor.execute(
            "select count(1) from mdt_a_employee_mi where user_id = :user_id",
            {"user_id": doctor_id},
        )
        if cursor.fetchone()[0] == 0:
            return make_response(
                res_code="error",
                res_message="用户不存在",
                output=f"doctor_id {doctor_id} does not exist",
                status_code=404,
            )

        token_text = json.dumps(normalized_token, ensure_ascii=False, separators=(",", ":"))
        cursor.execute(
            "delete from mdt_a_employee_mi_image where user_id = :user_id",
            {"user_id": doctor_id},
        )
        cursor.execute(
            """
            insert into mdt_a_employee_mi_image
                (user_id, face_token, avatar_base64)
            values
                (:user_id, :face_token, :avatar_base64)
            """,
            {
                "user_id": doctor_id,
                "face_token": token_text,
                "avatar_base64": avatar_bytes,
            },
        )
        db.commit()
        return make_response(
            res_code="ok",
            res_message="人脸绑定保存成功",
            output={"doctor_id": doctor_id, "avatar_size": len(avatar_bytes)},
            status_code=200,
        )
    except Exception as exc:
        db.rollback()
        logger_system.error(f"Database error during face binding save: {str(exc)}")
        return make_response(
            res_code="error",
            res_message="人脸绑定保存失败",
            output=str(exc),
            status_code=500,
        )


@bp.route("/employees", methods=["POST"])
@token_required
def create_employee():
    """Create an employee and login account."""
    db, cursor = get_db()
    data = request.get_json(silent=True) or {}

    user_id = (data.get("user_id") or "").strip()
    user_name = (data.get("user_name") or "").strip()
    password = (data.get("password") or "").strip()
    dept_code = (data.get("dept_code") or "").strip()
    dept_name = (data.get("dept_name") or "").strip()
    role_code = (data.get("role_code") or "").strip()
    role_name = (data.get("role_name") or data.get("role") or "").strip()
    mobile = (data.get("mobile") or "").strip()
    email = (data.get("email") or "").strip()

    required_fields = {
        "user_id": user_id,
        "user_name": user_name,
        "password": password,
        "dept_code": dept_code,
        "dept_name": dept_name,
        "role_code": role_code,
        "role_name": role_name,
    }
    missing_fields = [key for key, value in required_fields.items() if not value]
    if missing_fields:
        return make_response(
            res_code="error",
            res_message="参数错误",
            output=f"Missing required fields: {', '.join(missing_fields)}",
            status_code=400,
        )

    try:
        cursor.execute(
            "select count(1) from mdt_a_employee_mi where user_id = :user_id",
            {"user_id": user_id},
        )
        if cursor.fetchone()[0] > 0:
            return make_response(
                res_code="error",
                res_message="用户已存在",
                output=f"user_id {user_id} already exists",
                status_code=409,
            )

        cursor.execute(
            """
            insert into mdt_a_employee_mi
                (user_id, user_name, dept_code, dept_name,
                 role_code, role_name, mobile, flag, comment_)
            values
                (:user_id, :user_name, :dept_code, :dept_name,
                 :role_code, :role_name, :mobile, :flag, :comment_)
            """,
            {
                "user_id": user_id,
                "user_name": user_name,
                "dept_code": dept_code,
                "dept_name": dept_name,
                "role_code": role_code,
                "role_name": role_name,
                "mobile": mobile,
                "flag": "1",
                "comment_": email,
            },
        )

        cursor.execute(
            "select count(1) from mdt_user where user_id = :user_id",
            {"user_id": user_id},
        )
        if cursor.fetchone()[0] == 0:
            cursor.execute(
                """
                insert into mdt_user (user_id, user_name, password, open_flag)
                values (:user_id, :user_name, :password, :open_flag)
                """,
                {
                    "user_id": user_id,
                    "user_name": user_name,
                    "password": generate_password_hash(password),
                    "open_flag": "已启用",
                },
            )

        db.commit()
        return make_response(
            res_code="ok",
            res_message="新增用户成功",
            output={"user_id": user_id},
            status_code=201,
        )
    except Exception as exc:
        db.rollback()
        logger_system.error(f"Database error during employee create: {str(exc)}")
        return make_response(
            res_code="error",
            res_message="新增用户失败",
            output=str(exc),
            status_code=500,
        )


@bp.route("/employees/<user_id>", methods=["PUT"])
@token_required
def update_employee(user_id):
    """Update employee profile and optionally reset password."""
    db, cursor = get_db()
    data = request.get_json(silent=True) or {}

    user_id = (user_id or "").strip()
    user_name = (data.get("user_name") or "").strip()
    dept_code = (data.get("dept_code") or "").strip()
    dept_name = (data.get("dept_name") or "").strip()
    role_code = (data.get("role_code") or "").strip()
    role_name = (data.get("role_name") or data.get("role") or "").strip()
    mobile = (data.get("mobile") or "").strip()
    email = (data.get("email") or "").strip()
    password = (data.get("password") or "").strip()

    required_fields = {
        "user_id": user_id,
        "user_name": user_name,
        "dept_name": dept_name,
        "role_name": role_name,
    }
    missing_fields = [key for key, value in required_fields.items() if not value]
    if missing_fields:
        return make_response(
            res_code="error",
            res_message="参数错误",
            output=f"Missing required fields: {', '.join(missing_fields)}",
            status_code=400,
        )

    try:
        cursor.execute(
            "select count(1) from mdt_a_employee_mi where user_id = :user_id",
            {"user_id": user_id},
        )
        if cursor.fetchone()[0] == 0:
            return make_response(
                res_code="error",
                res_message="用户不存在",
                output=f"user_id {user_id} does not exist",
                status_code=404,
            )

        cursor.execute(
            """
            update mdt_a_employee_mi
               set user_name = :user_name,
                   dept_code = nvl(:dept_code, dept_code),
                   dept_name = :dept_name,
                   role_code = nvl(:role_code, role_code),
                   role_name = :role_name,
                   mobile = :mobile,
                   comment_ = :comment_
             where user_id = :user_id
            """,
            {
                "user_id": user_id,
                "user_name": user_name,
                "dept_code": dept_code,
                "dept_name": dept_name,
                "role_code": role_code,
                "role_name": role_name,
                "mobile": mobile,
                "comment_": email,
            },
        )

        cursor.execute(
            "select count(1) from mdt_user where user_id = :user_id",
            {"user_id": user_id},
        )
        if cursor.fetchone()[0] == 0:
            cursor.execute(
                """
                insert into mdt_user (user_id, user_name, password, open_flag)
                values (:user_id, :user_name, :password, :open_flag)
                """,
                {
                    "user_id": user_id,
                    "user_name": user_name,
                    "password": generate_password_hash(password or user_id),
                    "open_flag": "1",
                },
            )
        else:
            if password:
                cursor.execute(
                    """
                    update mdt_user
                       set user_name = :user_name,
                           password = :password
                     where user_id = :user_id
                    """,
                    {
                        "user_id": user_id,
                        "user_name": user_name,
                        "password": generate_password_hash(password),
                    },
                )
            else:
                cursor.execute(
                    """
                    update mdt_user
                       set user_name = :user_name
                     where user_id = :user_id
                    """,
                    {"user_id": user_id, "user_name": user_name},
                )

        db.commit()
        return make_response(
            res_code="ok",
            res_message="编辑用户成功",
            output={"user_id": user_id},
            status_code=200,
        )
    except Exception as exc:
        db.rollback()
        logger_system.error(f"Database error during employee update: {str(exc)}")
        return make_response(
            res_code="error",
            res_message="编辑用户失败",
            output=str(exc),
            status_code=500,
        )
