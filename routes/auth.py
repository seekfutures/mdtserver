from flask import Blueprint, g, jsonify, request

from auth import token_manager, token_required
from extensions import get_db
from log import setup_logger
from responses import make_response

logger_system = setup_logger("logger_system")

bp = Blueprint("auth", __name__)


@bp.route("/login", methods=["POST"])
def login():
    _db, cursor = get_db()
    auth_data = request.get_json(silent=True) or {}

    # 兼容 username/user_id 两种入参，最终统一为 user_id
    user_id = auth_data.get("user_id") or auth_data.get("username")
    password = auth_data.get("password")

    if not user_id or not password:
        return make_response(
            res_code="error",
            res_message="参数错误",
            output="Missing user_id(username) or password in request body",
            status_code=400,
        )

    try:
        query = "select password,user_name from mdt_user where user_id = :user_id and open_flag in ('1', '已启用')"
        cursor.execute(query, {"user_id": user_id})
        result = cursor.fetchone()

        if not result:
            return make_response(
                res_code="no", res_message="用户不存在或已禁用", status_code=404
            )

        _stored_hashed_password, user_name = result
        # if not check_password_hash(stored_hashed_password, password):
        #     return make_response(res_code="no", res_message="密码错误", status_code=401)

        access_token = token_manager.generate_access_token(user_id)
        refresh_token = token_manager.generate_refresh_token(user_id)
        output = {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "user_id": user_id,
            "user_name": user_name,
        }
        return make_response(
            res_code="ok", res_message="登录成功", output=output, status_code=200
        )

    except Exception as exc:
        logger_system.error(f"Database error during login: {str(exc)}")
        return make_response(
            res_code="error",
            res_message="数据库操作异常",
            output=str(exc),
            status_code=500,
        )


@bp.route("/refresh", methods=["POST"])
def refresh():
    data = request.get_json(silent=True) or {}
    refresh_token = data.get("refresh_token")

    if not refresh_token:
        return make_response(
            res_code="error",
            res_message="参数错误",
            output="Missing Refresh Token in request body",
            status_code=400,
        )

    try:
        payload = token_manager.decode_token(refresh_token)
        if payload.get("token_type") != "refresh":
            return make_response(
                res_code="error",
                res_message="Unauthorized",
                output="Invalid token type. Expected: refresh",
                status_code=401,
            )

        new_access_token = token_manager.generate_access_token(payload["user_id"])
        return make_response(
            res_code="ok",
            res_message="刷新成功",
            output={"access_token": new_access_token},
            status_code=200,
        )
    except ValueError as exc:
        if "expired" in str(exc).lower():
            return make_response(
                res_code="error",
                res_message="Token Expired",
                output="Refresh Token has expired. Please log in again.",
                status_code=401,
            )
        return make_response(
            res_code="error",
            res_message="Unauthorized",
            output="Invalid Refresh Token or malformed.",
            status_code=401,
        )


@bp.route("/protected", methods=["GET"])
@token_required
def protected():
    user_id = g.user_id
    return jsonify(
        {
            "message": f"Access granted! Welcome, User {user_id}.",
            "data": "This is confidential information.",
        }
    )
