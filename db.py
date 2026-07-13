# db.py 示例修改
import oracledb as cx_Oracle

class OracleDBManager:
    def __init__(self):
        self.pool = None
#         self.dsn = """
# (DESCRIPTION =
#     (ADDRESS = (PROTOCOL = TCP)(HOST = 10.27.3.150)(PORT = 1521))
#     (CONNECT_DATA =
#       (SERVER = DEDICATED)
#       (SERVICE_NAME = sjzx)
#     )
# )
# """
#         self.user = "sjcj"
#         self.password = "sjcj"

        self.dsn = """
        (DESCRIPTION =
            (ADDRESS = (PROTOCOL = TCP)(HOST = 192.168.1.10)(PORT = 1521))
            (CONNECT_DATA =
              (SERVER = DEDICATED)
              (SERVICE_NAME = orcl)
            )
        )
        """
        self.user ="system"
        self.password = "Founder123"

    def init_pool(self):
        """应用启动时初始化全局连接池"""
        if self.pool is None:
            self.pool = cx_Oracle.SessionPool(
                user=self.user,
                password=self.password,
                dsn=self.dsn,
                min=2,  # 池中最小连接数
                max=20,  # 池中最大连接数
                increment=1,
                threaded=True,  # 必须为 True 以支持多线程 Flask
                encoding="UTF-8"
            )

    def get_connection(self):
        """从池中获取一个连接"""
        return self.pool.acquire()

    def release_connection(self, conn):
        """将连接归还给池"""
        self.pool.release(conn)
