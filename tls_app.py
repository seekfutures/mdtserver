from flask import Flask

from extensions import init_extensions
from log import setup_logger
from responses import make_response
from routes.auth import bp as auth_bp
from routes.clinical import bp as clinical_bp
from routes.disease_groups import bp as disease_groups_bp
from routes.employees import bp as employees_bp
from routes.health import bp as health_bp
from routes.schedules import bp as schedules_bp

logger_system = setup_logger("logger_system")


def register_error_handlers(app):
    @app.errorhandler(404)
    def not_found_error(error):
        logger_system.error(error)
        return make_response(
            res_code="error",
            res_message="Resource not found",
            output=str(error),
            status_code=404,
        )

    @app.errorhandler(400)
    def bad_request_error(error):
        logger_system.error(error)
        return make_response(
            res_code="error",
            res_message="Bad request",
            output=str(error),
            status_code=400,
        )

    @app.errorhandler(500)
    def internal_server_error(error):
        logger_system.error(error)
        return make_response(
            res_code="error",
            res_message="Internal server error",
            output=str(error),
            status_code=500,
        )

    @app.errorhandler(Exception)
    def handle_exception(error):
        logger_system.error(error)
        return make_response(
            res_code="error",
            res_message="An unexpected error occurred",
            output=str(error),
            status_code=500,
        )


def register_blueprints(app):
    app.register_blueprint(auth_bp)
    app.register_blueprint(employees_bp)
    app.register_blueprint(disease_groups_bp)
    app.register_blueprint(schedules_bp)
    app.register_blueprint(clinical_bp)
    app.register_blueprint(health_bp)


def create_app():
    flask_app = Flask(__name__)
    init_extensions(flask_app)
    register_blueprints(flask_app)
    register_error_handlers(flask_app)
    return flask_app


app = create_app()
