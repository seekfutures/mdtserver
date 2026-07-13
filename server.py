# server.py
import base64
from flask import Flask, request, jsonify
import jwt
import datetime
from functools import wraps


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
    def __init__(self, secret_key='a_very_secret_key_123', port=5000):
        self.app = Flask(__name__)
        self.app.config['SECRET_KEY'] = secret_key
        self.port = port

        # 初始化Token管理器
        self.token_manager = JWTTokenManager(secret_key)

        # 模拟用户数据库
        self.users = {
            'user123': {
                'password': 'pass',
                'user_id': 1001
            }
        }

        # 注册路由
        self._setup_routes()

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
                    'user_id': user['user_id']
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

    def run(self, debug=False):
        """启动服务器"""
        print(f"Server running on http://127.0.0.1:{self.port}")
        self.app.run(port=self.port, debug=debug)


if __name__ == '__main__':
    # 创建并启动服务器
    server = AuthServer(port=5000)
    server.run(debug=False)