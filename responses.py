from flask import jsonify


def make_response(res_code="ok", res_message="操作成功", output=None, status_code=200):
    return jsonify(
        {"res_code": res_code, "res_message": res_message, "output": output}
    ), status_code
