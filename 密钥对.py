from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives import hashes
import json
import base64
from datetime import datetime, timedelta


def generate_keys():
    """生成 RSA 2048 位密钥对 (私钥用于签名, 公钥用于验证)"""
    print("--- 正在生成密钥对（请安全保存 Private Key） ---")

    # 1. 生成私钥
    private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048
    )

    # 2. 导出私钥 (PEM 格式, 不加密)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption()
    )

    # 3. 导出公钥 (PEM 格式)
    public_key = private_key.public_key()
    public_pem = public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo
    )

    # 写入文件
    with open('private.pem', 'wb') as f:
        f.write(private_pem)
    with open('public.pem', 'wb') as f:
        f.write(public_pem)

    print("✅ 密钥生成完成：")
    print("   - private.pem (私钥) -> 用于签名 License")
    print("   - public.pem (公钥)  -> 用于服务器端验证")

    return private_pem, public_pem

# 运行一次生成密钥：
PRIVATE_KEY, PUBLIC_KEY = generate_keys()