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
    app.config["PAGE_SIZE"] = cfg.PAGE_SIZE
    app.config["DB_IS_EPHEMERAL"] = cfg.DB_IS_EPHEMERAL
    app.config["SERVERLESS"] = cfg.SERVERLESS

    # Pool settings matter once the app serves more than one request at a time.
    # SQLite ignores most of these; Postgres does not.
    app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
        "pool_pre_ping": True,
        "pool_recycle": 280,
    }

    # Session cookie hardening. SameSite=Lax blocks the cookie on cross-site
    # POSTs, which is the CSRF vector that matters here; Secure is set only
    # where the deployment is actually served over HTTPS, because setting it
    # on plain http would break local development entirely.
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["SESSION_COOKIE_SECURE"] = bool(
        cfg.SERVERLESS or os.environ.get("FORCE_HTTPS"))

    _warn_on_weak_config(cfg)

    # The database and upload directories must exist before anything writes to
    # them. On serverless hosts these live under /tmp and are recreated on each
    # cold start.
    for path in (cfg.INSTANCE_DIR, cfg.UPLOAD_FOLDER):
        try:
            os.makedirs(path, exist_ok=True)
        except OSError:
            pass  # read-only filesystem; SQLAlchemy will report it properly

    # Extensions
    db.init_app(app)

    with app.app_context():
        # Import models so SQLAlchemy knows about them
        from . import models  # noqa: F401

        # Create tables
        db.create_all()

        # Add any columns and indexes introduced since this database was made.
        from . import migrations
        migrations.run()
        migrations.backfill_posted_ts()

        # WAL mode lets reads continue while a collection run writes. It only
        # applies to SQLite, and only where the filesystem supports it.
        if db.engine.url.get_backend_name() == "sqlite":
            try:
                with db.engine.connect() as conn:
                    conn.execute(text("PRAGMA journal_mode=WAL"))
                    conn.execute(text("PRAGMA synchronous=NORMAL"))
                    conn.execute(text("PRAGMA busy_timeout=5000"))
                    conn.commit()
            except Exception:
                pass

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


def _warn_on_weak_config(cfg):
    """Say something loudly when a deployment is reachable but unhardened.

    The defaults are chosen for a local run. Carrying them onto a public host
    means anyone who finds the URL is one guessed password from the vault, so
    this prints rather than failing silently.
    """
    import sys

    problems = []
    if cfg.PASSWORD == "profiler2024":
        problems.append("PROFILER_PASSWORD is still the built-in default.")
    if not os.environ.get("SECRET_KEY"):
        problems.append("SECRET_KEY is generated per process, so sessions drop "
                        "on restart and across workers.")
    if not problems:
        return

    where = "a public deployment" if cfg.SERVERLESS else "this host"
    print("\n  Configuration warning for %s:" % where, file=sys.stderr)
    for p in problems:
        print("    - " + p, file=sys.stderr)
    print("  Set these in the environment before exposing the app.\n",
          file=sys.stderr)


def _seed_dorks():
    from .models import DorkTemplate
    from .dorking.templates_data import BUILTIN_TEMPLATES

    if DorkTemplate.query.filter_by(is_builtin=True).count() == 0:
        for t in BUILTIN_TEMPLATES:
            db.session.add(DorkTemplate(**t, is_builtin=True))
        db.session.commit()
