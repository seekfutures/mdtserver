import base64
import hashlib
import hmac
import json
import os
from datetime import datetime

from 获取mac import get_current_server_id

# 本地状态文件（用于防止通过回拨系统时间延长许可）
STATE_FILE = "license_state.json"
DATE_FMT = "%Y-%m-%d"

# 项目内固定盐，结合机器 MAC 派生签名密钥
_APP_SALT = "mdtserver-license-heartbeat-v1"


def _derive_key(server_id):
    material = f"{server_id}|{_APP_SALT}".encode("utf-8")
    return hashlib.sha256(material).digest()


def _sign_payload(payload_bytes, key_bytes):
    return hmac.new(key_bytes, payload_bytes, hashlib.sha256).hexdigest()


def save_encrypted_time(filename=STATE_FILE):
    """保存当前日期到本地状态文件（带 HMAC 签名）"""
    server_id = get_current_server_id()
    current_date_str = datetime.now().strftime(DATE_FMT)

    payload = {
        "timestamp": current_date_str,
        "server_id": server_id,
    }
    payload_bytes = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    signature = _sign_payload(payload_bytes, _derive_key(server_id))

    state = {
        "payload": base64.b64encode(payload_bytes).decode("utf-8"),
        "signature": signature,
    }

    with open(filename, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)


def load_and_decrypt_time(filename=STATE_FILE):
    """
    读取并校验状态文件，返回 (True, 'YYYY-MM-DD') 或 (False, 错误信息)
    """
    if not os.path.exists(filename):
        return False, "时间戳文件不存在"

    try:
        with open(filename, "r", encoding="utf-8") as f:
            state = json.load(f)

        payload_b64 = state.get("payload")
        signature = state.get("signature")
        if not payload_b64 or not signature:
            return False, "时间戳文件结构无效"

        payload_bytes = base64.b64decode(payload_b64)
        payload = json.loads(payload_bytes.decode("utf-8"))

        server_id = payload.get("server_id")
        timestamp = payload.get("timestamp")
        if not server_id or not timestamp:
            return False, "时间戳内容不完整"

        current_server_id = get_current_server_id()
        if server_id != current_server_id:
            return False, "服务器标识不匹配"

        expected_sig = _sign_payload(payload_bytes, _derive_key(server_id))
        if not hmac.compare_digest(signature, expected_sig):
            return False, "时间戳签名校验失败，文件可能被篡改"

        datetime.strptime(timestamp, DATE_FMT)
        return True, timestamp

    except Exception as exc:
        return False, f"读取或解析时间戳文件失败: {exc}"
