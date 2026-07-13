import argparse
import base64
import datetime
import json
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from 获取mac import get_current_server_id


def create_and_sign_license(server_id, duration_days, private_key_path="private.pem", filename="license.lic"):
    private_path = Path(private_key_path)
    if not private_path.exists():
        raise FileNotFoundError(f"未找到私钥文件: {private_key_path}")

    issue_date = datetime.datetime.now()
    expiry_date = issue_date + datetime.timedelta(days=duration_days)

    raw_data = {
        "server_id": server_id,
        "issue_date": issue_date.strftime("%Y-%m-%d"),
        "expiry_date": expiry_date.strftime("%Y-%m-%d"),
        "version": "1.0",
    }

    data_bytes = json.dumps(raw_data, sort_keys=True, ensure_ascii=False).encode("utf-8")

    with open(private_key_path, "rb") as f:
        private_key = serialization.load_pem_private_key(f.read(), password=None)

    signature = private_key.sign(
        data_bytes,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.MAX_LENGTH),
        hashes.SHA256(),
    )

    final_license = {
        "data": base64.b64encode(data_bytes).decode("utf-8"),
        "signature": base64.b64encode(signature).decode("utf-8"),
    }

    with open(filename, "w", encoding="utf-8") as f:
        json.dump(final_license, f, indent=4, ensure_ascii=False)

    return raw_data


def main():
    parser = argparse.ArgumentParser(description="生成许可证文件（license.lic）")
    parser.add_argument("--server-id", help="服务器ID/MAC（默认当前机器MAC）", default=None)
    parser.add_argument("--days", type=int, default=365, help="有效期天数，默认365")
    parser.add_argument("--private-key", default="private.pem", help="私钥路径，默认 private.pem")
    parser.add_argument("--out", default="license.lic", help="输出 lic 文件路径，默认 license.lic")
    args = parser.parse_args()

    server_id = args.server_id or get_current_server_id()
    raw_data = create_and_sign_license(
        server_id=server_id,
        duration_days=args.days,
        private_key_path=args.private_key,
        filename=args.out,
    )

    print(f"✅ 成功生成 License 文件: {args.out}")
    print(f"   - 服务器 ID: {raw_data['server_id']}")
    print(f"   - 签发日期: {raw_data['issue_date']}")
    print(f"   - 到期日期: {raw_data['expiry_date']}")


if __name__ == "__main__":
    main()
