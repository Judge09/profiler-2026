"""Vercel serverless entrypoint.

Vercel imports `app` from this module and serves it with its WSGI bridge, so
there is no `app.run()` here -- that is only for local development (`run.py`).

Read this before deploying
--------------------------
A serverless function has no durable disk. The SQLite database lives under
/tmp, which means **data does not survive between cold starts**: watches and
collected posts created on one invocation may be gone on the next. The app
detects this (config.SERVERLESS) and the dashboard shows a warning rather than
letting anyone assume their work is being kept.

If you need the data to persist, either:

  * run the app on a host with a real disk (a VM, a container with a volume,
    or just locally), which is the default and needs no configuration; or
  * point DATABASE_URL at an external database, which is the only way a
    serverless deployment can keep anything.

Everything else works on Vercel: collection, scoring, the dashboard, the
link map, phishing checks and geolocation. Only the browser-driven sources
(Playwright, Selenium) are unavailable, and the capability report says so.
"""

import os
import sys

# The project root must be importable before `app` and `config` resolve.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("VERCEL", "1")

from app import create_app  # noqa: E402

app = create_app()

# Vercel's Python runtime looks for `app` or `handler`; provide both so it
# works regardless of which convention the builder expects.
handler = app
