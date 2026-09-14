import os
from flask import Flask
from sqlalchemy import text
from .extensions import db


def create_app():
    app = Flask(__name__, instance_relative_config=False)

    # Config
    import config as cfg
    app.config["SECRET_KEY"] = cfg.SECRET_KEY
    app.config["SQLALCHEMY_DATABASE_URI"] = cfg.DATABASE_URI
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    app.config["UPLOAD_FOLDER"] = cfg.UPLOAD_FOLDER
    app.config["MAX_CONTENT_LENGTH"] = cfg.MAX_CONTENT_LENGTH

    # Extensions
    db.init_app(app)

    with app.app_context():
        # Import models so SQLAlchemy knows about them
        from . import models  # noqa: F401

        # Create tables
        db.create_all()

        # Enable WAL mode for concurrent SSE + writes
        with db.engine.connect() as conn:
            conn.execute(text("PRAGMA journal_mode=WAL"))
            conn.commit()

        # Seed builtin dork templates
        _seed_dorks()

        # Register blueprints
        from .auth.routes import auth
        from .profiles.routes import profiles
        from .dorking.routes import dorking
        from .username_osint.routes import osint
        from .linkmap.routes import linkmap
        from .monitor.routes import monitor

        app.register_blueprint(auth)
        app.register_blueprint(profiles)
        app.register_blueprint(dorking)
        app.register_blueprint(osint)
        app.register_blueprint(linkmap)
        app.register_blueprint(monitor)

        # Unlock the credential vault from the environment when configured.
        from .monitor import vault
        vault.auto_unlock()

    return app


def _seed_dorks():
    from .models import DorkTemplate
    from .dorking.templates_data import BUILTIN_TEMPLATES

    if DorkTemplate.query.filter_by(is_builtin=True).count() == 0:
        for t in BUILTIN_TEMPLATES:
            db.session.add(DorkTemplate(**t, is_builtin=True))
        db.session.commit()
