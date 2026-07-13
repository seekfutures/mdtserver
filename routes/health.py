from flask import Blueprint

from responses import make_response

bp = Blueprint("health", __name__)


@bp.route("/health", methods=["GET"])
def health_check():
    return make_response(
        res_code="ok", res_message="healthy", output="healthy", status_code=200
    )
