import re
import uuid


def get_current_server_id():
    """获取当前机器 MAC 地址并格式化为 AA-BB-CC-DD-EE-FF"""
    mac_int = uuid.getnode()
    mac_hex = f"{mac_int:012x}"
    mac_address = "-".join(re.findall("..", mac_hex)).upper()
    return mac_address


if __name__ == "__main__":
    print(get_current_server_id())
