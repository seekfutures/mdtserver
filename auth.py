import base64
import datetime
import os
from functools import wraps

import jwt
from flask import g, request

from responses import make_response


SECRET_KEY = os.getenv("JWT_SECRET_KEY", "a_very_secret_key_123_dev_only")
ACCESS_TOKEN_MINUTES = int(os.getenv("ACCESS_TOKEN_MINUTES", "30"))
REFRESH_TOKEN_MINUTES = int(os.getenv("REFRESH_TOKEN_MINUTES", "120"))


class JWTTokenManager:
    """JWT Token管理器"""

    def __init__(self, secret_key, access_token_minutes=30, refresh_token_minutes=120):
        self.secret_key = secret_key
        self.access_token_lifetime = datetime.timedelta(minutes=access_token_minutes)
        self.refresh_token_lifetime = datetime.timedelta(minutes=refresh_token_minutes)

    def generate_access_token(self, user_id):
        payload = {
            "exp": datetime.datetime.utcnow() + self.access_token_lifetime,
            "iat": datetime.datetime.utcnow(),
            "user_id": user_id,
            "token_type": "access",
        }
        token = jwt.encode(payload, self.secret_key, algorithm="HS256")
        token_bytes = token.encode("utf-8") if isinstance(token, str) else token
        return base64.b64encode(token_bytes).decode("utf-8")

    def generate_refresh_token(self, user_id):
        payload = {
            "exp": datetime.datetime.utcnow() + self.refresh_token_lifetime,
            "iat": datetime.datetime.utcnow(),
            "user_id": user_id,
            "token_type": "refresh",
        }
        token = jwt.encode(payload, self.secret_key, algorithm="HS256")
        token_bytes = token.encode("utf-8") if isinstance(token, str) else token
        return base64.b64encode(token_bytes).decode("utf-8")

    def decode_token(self, encoded_token):
        try:
            token = base64.b64decode(encoded_token)
            return jwt.decode(token, self.secret_key, algorithms=["HS256"])
        except jwt.ExpiredSignatureError as exc:
            raise ValueError("Token has expired") from exc
        except (jwt.InvalidTokenError, ValueError, TypeError) as exc:
            raise ValueError("Invalid token") from exc


token_manager = JWTTokenManager(
    SECRET_KEY,
    access_token_minutes=ACCESS_TOKEN_MINUTES,
    refresh_token_minutes=REFRESH_TOKEN_MINUTES,
)


def token_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth_header = request.headers.get("Authorization")
        if not auth_header:
            return make_response(
                res_code="error",
                res_message="Unauthorized",
                output="Authorization header is missing",
                status_code=401,
            )

        try:
            scheme, token_value = auth_header.split(" ", 1)
            if scheme.lower() != "bearer":
                return make_response(
                    res_code="error",
                    res_message="Unauthorized",
                    output="Invalid authorization scheme (must be Bearer)",
                    status_code=401,
                )
        except ValueError:
            return make_response(
                res_code="error",
                res_message="Unauthorized",
                output="Invalid Authorization header format",
                status_code=401,
            )

        try:
            payload = token_manager.decode_token(token_value)
            if payload.get("token_type") != "access":
                return make_response(
                    res_code="error",
                    res_message="Unauthorized",
                    output="Invalid token type! Expected: access",
                    status_code=401,
                )

            g.user_id = payload["user_id"]
            return f(*args, **kwargs)
        except ValueError as exc:
            if "expired" in str(exc).lower():
                return make_response(
                    res_code="error",
                    res_message="Token Expired",
                    output="Access Token has expired. Please refresh.",
                    status_code=401,
                )
            return make_response(
                res_code="error",
                res_message="Unauthorized",
                output="Access Token is invalid or malformed.",
                status_code=401,
            )

    return decorated
