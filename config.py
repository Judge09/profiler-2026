"""Application configuration.

Everything here can be overridden from the environment, so the same code runs
locally with a persistent SQLite file and in a container or serverless host
without editing the file.
"""

import os
import secrets
import hashlib

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _load_dotenv():
    """Read a local .env file into the environment, if one exists.

    Deliberately hand-rolled: this is the only thing python-dotenv would be
    pulled in for, and a dependency that ships in every deployment to parse
    `KEY=value` is not worth it.

    Real environment variables always win, so a platform like Vercel -- which
    injects its own and has no .env file -- is unaffected.
    """
    path = os.path.join(BASE_DIR, ".env")
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return

    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        # `export FOO=bar` is a habit people carry over from shell scripts.
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not key:
            continue
        # Strip one layer of matching quotes, which people add out of habit.
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


_load_dotenv()

# True when running on a platform with an ephemeral, mostly read-only disk.
SERVERLESS = bool(os.environ.get("VERCEL") or os.environ.get("AWS_LAMBDA_FUNCTION_NAME"))

# -- Secrets -----------------------------------------------------------------

# Change this password before use, or set PROFILER_PASSWORD in the environment.
PASSWORD = os.environ.get("PROFILER_PASSWORD", "profiler2024")

# Keep sessions stable across Vercel restarts even when SECRET_KEY is omitted.
# An explicitly configured key remains preferred; the password-derived fallback
# means the deployment only needs one required secret.
SECRET_KEY = os.environ.get("SECRET_KEY") or hashlib.sha256(
    ("profiler-session:" + PASSWORD).encode("utf-8")).hexdigest()

# -- Database ----------------------------------------------------------------
#
# User data lives in the browser (IndexedDB), not here. This database exists
# for two much smaller jobs:
#
#   * the built-in dork templates, which are reference data shipped with the app
#   * the optional server sync target, if someone chooses to push a copy
#
# That is why a serverless deployment is no longer a data-loss risk: losing
# /tmp on a cold start costs nothing that was not either regenerated at startup
# or explicitly pushed. The credential vault is the one exception -- its
# secrets are deliberately server-side so they never reach JavaScript -- and
# those do need a persistent disk to survive a restart.

INSTANCE_DIR = os.environ.get("PROFILER_INSTANCE_DIR") or (
    "/tmp/profiler-instance" if SERVERLESS else os.path.join(BASE_DIR, "instance"))

_env_db = os.environ.get("DATABASE_URL") or os.environ.get("PROFILER_DATABASE_URI")
if _env_db:
    # Heroku-style postgres:// is not a SQLAlchemy dialect name.
    DATABASE_URI = _env_db.replace("postgres://", "postgresql://", 1)
    DB_IS_EPHEMERAL = False
else:
    # POSIX separators: SQLAlchemy URLs are URLs, not OS paths, and a
    # backslash from os.path.join on Windows would land inside the URL.
    DATABASE_URI = "sqlite:///" + os.path.join(
        INSTANCE_DIR, "profiler.db").replace(os.sep, "/")
    DB_IS_EPHEMERAL = SERVERLESS

# -- Uploads -----------------------------------------------------------------

UPLOAD_FOLDER = os.environ.get("PROFILER_UPLOAD_DIR") or (
    os.path.join("/tmp", "profiler-uploads") if SERVERLESS
    else os.path.join(BASE_DIR, "app", "static", "uploads", "profile_pics"))
MAX_CONTENT_LENGTH = 16 * 1024 * 1024  # 16MB max upload

# -- App ---------------------------------------------------------------------

DEBUG = os.environ.get("FLASK_DEBUG", "").lower() in ("1", "true", "yes")
HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "5000"))

# Seconds a collection run may take before the client gives up. Serverless
# platforms cap the request anyway; keeping our own limit under theirs means a
# partial result comes back instead of a gateway error.
COLLECT_TIMEOUT = int(os.environ.get("COLLECT_TIMEOUT", "45" if SERVERLESS else "120"))

# Page size for the post list.
PAGE_SIZE = int(os.environ.get("PAGE_SIZE", "25"))
