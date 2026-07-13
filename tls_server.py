import base64
import json
import os
import ssl
import sys
from datetime import datetime
from socketserver import ThreadingMixIn
from wsgiref.simple_server import WSGIServer, WSGIRequestHandler

from PyQt6.QtCore import QThread, pyqtSignal, Qt
from PyQt6.QtWidgets import QApplication, QVBoxLayout, QWidget, QLabel, QHBoxLayout, QMainWindow, QSpinBox
from qfluentwidgets import (
    PrimaryPushButton,
    PushButton,
    StrongBodyLabel,
    BodyLabel,
    CardWidget,
    InfoBar,
    InfoBarPosition,
    setThemeColor,
)

from log import setup_logger
from tls_app import app

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives import hashes

from 获取mac import get_current_server_id
from 心跳检测 import save_encrypted_time, load_and_decrypt_time
from 生成lic文件 import create_and_sign_license

logger_system = setup_logger("system")


class SSLWSGIServer(ThreadingMixIn, WSGIServer):
    """支持双向TLS认证的自定义WSGI服务器"""

    def __init__(self, *args, ssl_context=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.ssl_context = ssl_context

    def get_request(self):
        client_socket, client_addr = super().get_request()
        if self.ssl_context:
            try:
                client_socket = self.ssl_context.wrap_socket(
                    client_socket,
                    server_side=True,
                    do_handshake_on_connect=True,
                )
                cert = client_socket.getpeercert()
                if not cert:
                    logger_system.warning(f"客户端 {client_addr} 未提供证书，拒绝连接")
                    client_socket.close()
                    raise ConnectionAbortedError("客户端证书缺失")
            except ssl.SSLError as e:
                logger_system.error(f"SSL握手失败: {str(e)}")
                client_socket.close()
                raise
        return client_socket, client_addr


class ServerThread(QThread):
    """Web服务器运行线程"""

    error_signal = pyqtSignal(str)
    status_signal = pyqtSignal(str)

    def __init__(self, app_obj, host="127.0.0.1", port=8443, ssl_cert=None, ssl_key=None, ca_cert=None):
        super().__init__()
        self.app = app_obj
        self.host = host
        self.port = port
        self.ssl_cert = ssl_cert
        self.ssl_key = ssl_key
        self.ca_cert = ca_cert
        self.server = None

    def run(self):
        try:
            ssl_context = None
            if self.ssl_cert and self.ssl_key:
                ssl_context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
                ssl_context.load_cert_chain(certfile=self.ssl_cert, keyfile=self.ssl_key)

                if self.ca_cert and os.path.exists(self.ca_cert):
                    ssl_context.verify_mode = ssl.CERT_REQUIRED
                    ssl_context.load_verify_locations(cafile=self.ca_cert)
                    logger_system.info("已启用客户端证书验证")

            self.server = SSLWSGIServer((self.host, self.port), WSGIRequestHandler, ssl_context=ssl_context)
            self.server.set_app(self.app)

            protocol = "HTTPS" if ssl_context else "HTTP"
            self.status_signal.emit(f"服务运行中 ({protocol}://{self.host}:{self.port})")
            self.server.serve_forever()

        except Exception as e:
            logger_system.error(f"服务器错误: {str(e)}")
            self.error_signal.emit(str(e))

    def stop(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
        self.quit()


class HeartbeatThread(QThread):
    """心跳检测线程：定时验证许可证效期"""

    expired_signal = pyqtSignal(str)

    def __init__(self, check_func, interval_secs=3600):
        super().__init__()
        self.check_func = check_func
        self.interval = interval_secs
        self.running = True

    def run(self):
        logger_system.info("心跳检测线程已启动")
        while self.running:
            success, message = self.check_func()
            if not success:
                logger_system.warning(f"心跳检测失败: {message}")
                self.expired_signal.emit(message)
                break
            # 每次验证成功后刷新可信时间戳，防止通过回拨系统时间延长授权
            save_encrypted_time()
            self.msleep(self.interval * 1000)

    def stop(self):
        self.running = False


class LicenseVerifier:
    """许可证验证逻辑"""

    @staticmethod
    def verify_license_only():
        try:
            with open("public.pem", "rb") as f:
                public_key = serialization.load_pem_public_key(f.read(), backend=None)
            with open("license.lic", "r", encoding="utf-8") as f:
                signed_license = json.load(f)

            encoded_data = signed_license["data"]
            signature = base64.b64decode(signed_license["signature"])
            data_bytes = base64.b64decode(encoded_data)
            data = json.loads(data_bytes.decode("utf-8"))

            public_key.verify(
                signature,
                data_bytes,
                padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.MAX_LENGTH),
                hashes.SHA256(),
            )

            if data.get("server_id") != get_current_server_id():
                return False, "服务器MAC不匹配"

            expiry_date_str = data.get("expiry_date")
            expiry_date = datetime.strptime(expiry_date_str, "%Y-%m-%d")
            today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)

            if today > expiry_date:
                return False, f"许可证已过期 ({expiry_date_str})"
            return True, f"有效期至: {expiry_date_str}"
        except Exception as e:
            return False, f"证书验证异常: {str(e)}"

    @staticmethod
    def verify():
        success, message = load_and_decrypt_time()
        if not success:
            # 严格模式：不允许自动重建时间戳文件
            # 时间戳文件必须在首次激活时由受控流程写入
            return False, f"严格模式：时间戳校验失败（{message}）"

        try:
            last_time_dt = datetime.strptime(message, "%Y-%m-%d")
            today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
            if last_time_dt > today:
                return False, "禁止通过修改服务器时间延续注册"
        except Exception:
            return False, "时间戳格式无效"

        return LicenseVerifier.verify_license_only()


class ControlWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.flask_app = app
        self.server_thread = None
        self.heartbeat_thread = None
        self.is_running = False

        self.ssl_cert = "./server_crt/server.crt"
        self.ssl_key = "./server_crt/server.key"
        self.ca_cert = "./server_crt/ca.crt"
        self.listen_host = os.getenv("TLS_HOST", "127.0.0.1")
        self.listen_port = int(os.getenv("TLS_PORT", "8443"))

        self._init_ui()
        self._check_certificates()

    def _init_ui(self):
        self.setWindowTitle("安全服务控制台")
        self.resize(680, 420)

        root = QWidget(self)
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(16)

        title = StrongBodyLabel("TLS 服务控制中心", self)
        subtitle = BodyLabel("管理服务生命周期、证书注册状态与许可证心跳检测", self)
        subtitle.setStyleSheet("color: rgb(120,120,120);")
        layout.addWidget(title)
        layout.addWidget(subtitle)

        status_card = CardWidget(self)
        status_layout = QVBoxLayout(status_card)
        status_layout.setContentsMargins(20, 16, 20, 16)
        status_layout.setSpacing(10)

        self.status_label = QLabel("服务状态：未运行", self)
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.status_label.setStyleSheet("font-size: 14px; font-weight: 600;")

        self.register_label = QLabel("注册信息：待检测", self)
        self.register_label.setStyleSheet("font-size: 13px;")

        status_layout.addWidget(self.status_label)
        status_layout.addWidget(self.register_label)
        layout.addWidget(status_card)

        action_card = CardWidget(self)
        action_layout = QHBoxLayout(action_card)
        action_layout.setContentsMargins(20, 14, 20, 14)
        action_layout.setSpacing(12)

        self.start_btn = PrimaryPushButton("启动服务", self)
        self.start_btn.clicked.connect(self.start_service)
        self.start_btn.setEnabled(False)

        self.stop_btn = PushButton("停止服务", self)
        self.stop_btn.clicked.connect(self.stop_service)
        self.stop_btn.setEnabled(False)

        self.register_btn = PushButton("注册/验证", self)
        self.register_btn.clicked.connect(self.register_service)

        action_layout.addWidget(self.start_btn)
        action_layout.addWidget(self.stop_btn)
        action_layout.addWidget(self.register_btn)
        action_layout.addStretch(1)
        layout.addWidget(action_card)

        license_card = CardWidget(self)
        license_layout = QHBoxLayout(license_card)
        license_layout.setContentsMargins(20, 14, 20, 14)
        license_layout.setSpacing(12)

        self.server_id_label = BodyLabel(f"当前MAC: {get_current_server_id()}", self)
        self.days_spin = QSpinBox(self)
        self.days_spin.setRange(1, 3650)
        self.days_spin.setValue(365)
        self.days_spin.setPrefix("有效期(天): ")

        self.generate_license_btn = PushButton("生成本机License", self)
        self.generate_license_btn.clicked.connect(self.generate_license)

        license_layout.addWidget(self.server_id_label)
        license_layout.addWidget(self.days_spin)
        license_layout.addWidget(self.generate_license_btn)
        license_layout.addStretch(1)
        layout.addWidget(license_card)

        layout.addStretch(1)

    def _show_info(self, title, content, level="info"):
        creator = {
            "info": InfoBar.info,
            "success": InfoBar.success,
            "warning": InfoBar.warning,
            "error": InfoBar.error,
        }.get(level, InfoBar.info)

        creator(
            title=title,
            content=content,
            orient=Qt.Orientation.Horizontal,
            isClosable=True,
            position=InfoBarPosition.TOP,
            duration=3500,
            parent=self,
        )

    def _check_certificates(self):
        missing = [f for f in [self.ssl_cert, self.ssl_key, self.ca_cert] if not os.path.exists(f)]
        if missing:
            self._show_info("证书提示", f"缺失文件: {', '.join(missing)}，将尝试回退到非SSL模式", "warning")

    def register_service(self):
        success, message = LicenseVerifier.verify()
        # 严格模式下若仅缺失时间戳文件，允许通过“证书预检”放行首次激活
        if (not success) and ("时间戳文件不存在" in message):
            success, message = LicenseVerifier.verify_license_only()
            if success:
                message = f"{message}（待首次激活写入时间戳）"
        self.register_label.setText(f"注册信息：{message}")
        if success:
            self.start_btn.setEnabled(True)
            self._show_info("验证通过", "许可证验证成功，可以启动服务", "success")
        else:
            self.start_btn.setEnabled(False)
            self._show_info("验证失败", message, "error")

    def generate_license(self):
        try:
            server_id = get_current_server_id()
            days = self.days_spin.value()
            result = create_and_sign_license(
                server_id=server_id,
                duration_days=days,
                private_key_path="private.pem",
                filename="license.lic",
            )
            self.register_label.setText(f"注册信息：已生成，{result['expiry_date']} 到期")
            self._show_info(
                "生成成功",
                f"license.lic 已生成（MAC: {server_id}，到期: {result['expiry_date']}）",
                "success",
            )
        except Exception as exc:
            self._show_info("生成失败", f"License 生成失败: {exc}", "error")

    def start_service(self):
        # 首次激活写入：仅在时间戳文件不存在时初始化
        # 严格模式下，不做“缺失即通过”容错；只有写入成功后才允许启动
        state_exists, state_msg = load_and_decrypt_time()
        if not state_exists:
            if "不存在" in str(state_msg):
                try:
                    save_encrypted_time()
                    state_exists, state_msg = load_and_decrypt_time()
                except Exception as exc:
                    self._show_info("启动失败", f"首次激活写入时间戳失败: {exc}", "error")
                    return
            if not state_exists:
                self._show_info("启动失败", f"严格模式：时间戳不可用（{state_msg}）", "error")
                return

        # 每次启动前再次做完整许可证校验
        success, message = LicenseVerifier.verify()
        if not success:
            self.register_label.setText(f"注册信息：{message}")
            self._show_info("启动失败", message, "error")
            return

        self.server_thread = ServerThread(
            self.flask_app,
            host=self.listen_host,
            port=self.listen_port,
            ssl_cert=self.ssl_cert,
            ssl_key=self.ssl_key,
            ca_cert=self.ca_cert,
        )
        self.server_thread.error_signal.connect(self.on_server_error)
        self.server_thread.status_signal.connect(self.status_label.setText)
        self.server_thread.start()

        self.heartbeat_thread = HeartbeatThread(LicenseVerifier.verify, interval_secs=3600)
        self.heartbeat_thread.expired_signal.connect(self.handle_license_expired)
        self.heartbeat_thread.start()

        self.is_running = True
        self.update_ui_state()

    def stop_service(self):
        if self.server_thread:
            self.server_thread.stop()
        if self.heartbeat_thread:
            self.heartbeat_thread.stop()

        self.is_running = False
        self.update_ui_state()
        self.status_label.setText("服务状态：已停止")

    def handle_license_expired(self, reason):
        self.stop_service()
        self.register_label.setText(f"注册信息：{reason}")
        self._show_info("安全警告", f"服务已强制停止，原因：{reason}", "error")

    def update_ui_state(self):
        self.start_btn.setEnabled(not self.is_running)
        self.stop_btn.setEnabled(self.is_running)
        self.register_btn.setEnabled(not self.is_running)

    def on_server_error(self, error_msg):
        self._show_info("服务器错误", error_msg, "error")
        self.stop_service()

    def closeEvent(self, event):
        self.stop_service()
        event.accept()


if __name__ == "__main__":
    app_qt = QApplication(sys.argv)
    setThemeColor("#0078D4")
    win = ControlWindow()
    win.show()
    sys.exit(app_qt.exec())
