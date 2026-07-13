from flask import g

from db  import OracleDBManager


db_manager = OracleDBManager()


def init_extensions(app):
    db_manager.init_pool()
    app.teardown_appcontext(teardown_db)


def get_db():
    if "db" not in g:
        g.db = db_manager.get_connection()
        g.cursor = g.db.cursor()
    return g.db, g.cursor


def teardown_db(exception):
    cursor = g.pop("cursor", None)
    if cursor is not None:
        cursor.close()

    conn = g.pop("db", None)
    if conn is not None:
        db_manager.release_connection(conn)
