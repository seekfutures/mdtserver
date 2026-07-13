# server.py
import base64
from flask import Flask, request, jsonify
import jwt
import datetime
from functools import wraps
import ssl
import os


class JWTTokenManager:
    """JWT Token管理器"""

    def __init__(self, secret_key):
        self.secret_key = secret_key
        self.access_token_lifetime = datetime.timedelta(minutes=1)
        self.refresh_token_lifetime = datetime.timedelta(minutes=2)

    def generate_access_token(self, user_id):
        """生成Access Token"""
        payload = {
            'exp': datetime.datetime.utcnow() + self.access_token_lifetime,
            'iat': datetime.datetime.utcnow(),
            'user_id': user_id,
            'token_type': 'access'
        }
        token = jwt.encode(payload, self.secret_key, algorithm="HS256")
        return base64.b64encode(token).decode('utf-8')

    def generate_refresh_token(self, user_id):
        """生成Refresh Token"""
        payload = {
            'exp': datetime.datetime.utcnow() + self.refresh_token_lifetime,
            'iat': datetime.datetime.utcnow(),
            'user_id': user_id,
            'token_type': 'refresh'
        }
        token = jwt.encode(payload, self.secret_key, algorithm="HS256")
        return base64.b64encode(token).decode('utf-8')

    def decode_token(self, encoded_token):
        """解码并验证Token"""
        try:
            token = base64.b64decode(encoded_token)
            payload = jwt.decode(token, self.secret_key, algorithms=["HS256"])
            return payload
        except jwt.ExpiredSignatureError:
            raise ValueError("Token has expired")
        except (jwt.InvalidTokenError, base64.binascii.Error):
            raise ValueError("Invalid token")


class AuthServer:
    """认证服务器"""

    def __init__(self,
                 secret_key='a_very_secret_key_123',
                 port=443,
                 ssl_cert=None,
                 ssl_key=None,
                 ssl_ca=None):
        self.app = Flask(__name__)
        self.app.config['SECRET_KEY'] = secret_key
        self.port = port

        # SSL/TLS配置
        self.ssl_cert = ssl_cert or 'server-cert.pem'
        self.ssl_key = ssl_key or 'server-key.pem'
        self.ssl_ca = ssl_ca

        # 检查证书文件是否存在
        self._check_ssl_files()

        # 初始化Token管理器
        self.token_manager = JWTTokenManager(secret_key)

        # 模拟用户数据库
        self.users = {
            '50010': {
                'password': '50010',
                'user_id': 50010,
                'user_name': 50010
            }
        }

        # 注册路由
        self._setup_routes()

    def _check_ssl_files(self):
        """检查SSL证书文件是否存在"""
        if not os.path.exists(self.ssl_cert):
            print(f"警告: SSL证书文件 '{self.ssl_cert}' 不存在")
            print("请提供有效的PEM格式证书文件")
            self.ssl_cert = None
            self.ssl_key = None

        if self.ssl_key and not os.path.exists(self.ssl_key):
            print(f"警告: SSL私钥文件 '{self.ssl_key}' 不存在")
            self.ssl_key = None

    def _create_ssl_context(self):
        """创建SSL上下文"""
        if not self.ssl_cert or not self.ssl_key:
            print("警告: 未提供有效的SSL证书，将使用非加密HTTP")
            return None

        try:
            # 创建SSL上下文
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.verify_mode = ssl.CERT_NONE  # 客户端验证可选

            # 加载服务器证书和私钥
            context.load_cert_chain(certfile=self.ssl_cert, keyfile=self.ssl_key)

            # 如果有CA证书，设置验证
            if self.ssl_ca and os.path.exists(self.ssl_ca):
                context.load_verify_locations(cafile=self.ssl_ca)
                context.verify_mode = ssl.CERT_REQUIRED

            print(f"SSL证书加载成功: {self.ssl_cert}")
            return context
        except Exception as e:
            print(f"SSL上下文创建失败: {str(e)}")
            return None

    def _setup_routes(self):
        """设置路由"""

        # Token验证装饰器
        def token_required(f):
            @wraps(f)
            def decorated(*args, **kwargs):
                token = None
                if 'Authorization' in request.headers:
                    token = request.headers['Authorization'].split(" ")[1]

                if not token:
                    return jsonify({'message': 'Access Token is missing!'}), 401

                try:
                    payload = self.token_manager.decode_token(token)
                    if payload.get('token_type') != 'access':
                        return jsonify({'message': 'Invalid token type!'}), 401

                    request.user_id = payload['user_id']
                    return f(*args, **kwargs)
                except ValueError as e:
                    if "expired" in str(e):
                        return jsonify({'message': 'Access Token has expired!'}), 401
                    return jsonify({'message': 'Access Token is invalid!'}), 401

            return decorated

        # 登录接口
        @self.app.route('/login', methods=['POST'])
        def login():
            auth = request.get_json()
            if not auth:
                return jsonify({'message': 'Missing credentials'}), 400

            username = auth.get('username')
            password = auth.get('password')

            user = self.users.get(username)
            if not user or user['password'] != password:
                return jsonify({'message': 'Invalid credentials'}), 401

            try:
                access_token = self.token_manager.generate_access_token(user['user_id'])
                refresh_token = self.token_manager.generate_refresh_token(user['user_id'])

                return jsonify({
                    'access_token': access_token,
                    'refresh_token': refresh_token,
                    'user_id': user['user_id'],
                    'user_name': user['user_name']
                })
            except Exception as e:
                return jsonify({'message': f'Token generation failed: {str(e)}'}), 500

        # Refresh接口
        @self.app.route('/refresh', methods=['POST'])
        def refresh():
            data = request.get_json()
            refresh_token = data.get('refresh_token')

            if not refresh_token:
                return jsonify({'message': 'Refresh Token is missing!'}), 401

            try:
                payload = self.token_manager.decode_token(refresh_token)

                if payload.get('token_type') != 'refresh':
                    return jsonify({'message': 'Invalid token type'}), 401

                new_access_token = self.token_manager.generate_access_token(payload['user_id'])

                return jsonify({'access_token': new_access_token})
            except ValueError as e:
                if "expired" in str(e):
                    return jsonify({'message': 'Refresh Token has expired'}), 401
                return jsonify({'message': 'Invalid Refresh Token'}), 401

        # 受保护资源接口
        @self.app.route('/protected', methods=['GET'])
        @token_required
        def protected():
            user_id = request.user_id
            return jsonify({
                'message': f'Access granted! Welcome, User {user_id}.',
                'data': 'This is confidential information.'
            })

        # 服务器状态检查
        @self.app.route('/health', methods=['GET'])
        def health_check():
            return jsonify({'status': 'healthy', 'service': 'auth_server'})

    def run(self, debug=False, use_ssl=True):
        """启动服务器"""
        protocol = "https" if use_ssl else "http"

        if use_ssl:
            ssl_context = self._create_ssl_context()
            if ssl_context:
                print(f"✅ 服务器运行在 {protocol}://0.0.0.0:{self.port} (TLS加密)")
                print(f"   证书: {self.ssl_cert}")
                print(f"   私钥: {self.ssl_key}")
                if self.ssl_ca:
                    print(f"   CA证书: {self.ssl_ca}")

                self.app.run(
                    host='127.0.0.1',
                    port=self.port,
                    ssl_context=ssl_context,
                    debug=debug
                )
            else:
                print("⚠️ SSL不可用，回退到HTTP")
                print(f"服务器运行在 http://10.11.2.32:5000")
                self.app.run(port=5000, debug=debug)
        else:
            print(f"服务器运行在 {protocol}://10.11.2.32:{self.port}")
            self.app.run(port=self.port, debug=debug)


if __name__ == '__main__':
    # 创建并启动服务器
    server = AuthServer(
        port=443,  # HTTPS默认端口
        ssl_cert='server_crt/server.crt',  # 你的证书文件
        ssl_key='server_crt/server.key',  # 你的私钥文件
        ssl_ca='ca.crt'  # 可选：CA证书
    )
    server.run(debug=False, use_ssl=True)
