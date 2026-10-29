from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from flask_cors import CORS
from dotenv import load_dotenv
from urllib.parse import quote_plus
import os

load_dotenv()

db = SQLAlchemy()


def create_app():
    app = Flask(__name__)

    # Allow all origins on all routes — safe for a local/internal tool.
    # Supports preflight OPTIONS requests from the React dev server.
    CORS(
        app,
        resources={r"/*": {"origins": "*"}},
        allow_headers=["Content-Type", "Authorization", "Accept"],
        methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        supports_credentials=False,
    )

    # URL-encode the password so special characters like @ are handled correctly
    db_password = quote_plus(os.getenv("DB_PASSWORD", ""))

    app.config["SQLALCHEMY_DATABASE_URI"] = (
        f"mysql+pymysql://{os.getenv('DB_USER')}:{db_password}"
        f"@{os.getenv('DB_HOST')}:{os.getenv('DB_PORT')}/{os.getenv('DB_NAME')}"
    )
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "dev")

    # Connection pool settings — prevent "Lost connection" errors on idle connections
    app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
        "pool_pre_ping":    True,       # test connection before using it
        "pool_recycle":     280,        # recycle connections every 280s (before MySQL's wait_timeout)
        "pool_size":        10,
        "max_overflow":     20,
        "connect_args": {
            "connect_timeout": 30,
            "read_timeout":    60,
            "write_timeout":   60,
        },
    }

    db.init_app(app)

    from app.routes import cost_bp
    from app.aws_routes import aws_bp
    app.register_blueprint(cost_bp, url_prefix="/api")
    app.register_blueprint(aws_bp, url_prefix="/api")

    with app.app_context():
        db.create_all()

    return app
