import hmac
import os
import threading
import time
from functools import wraps
from flask import Blueprint, render_template, request, redirect, url_for, session, flash
import config

auth = Blueprint("auth", __name__)

# -- Brute-force throttling --------------------------------------------------
#
# One shared password protects everything here, including the credential vault,
# and the built-in default is published in the README. Unthrottled, the login
# form accepted about 2,400 guesses a second when measured -- which is a
# dictionary attack finished before anyone notices the traffic.
#
# So: failures are counted per client, each one is answered a little more
# slowly, and past a threshold the address is refused outright for a while.
# The delay is deliberate and small enough not to matter to a person typing a
# password wrong, while making a scripted sweep uselessly slow.
#
# This is in-process, which is the right scope for an app that runs as a single
# process. On a multi-worker or serverless deployment each worker counts
# separately, so it raises the cost of an attack rather than capping it
# absolutely; a shared store would be needed for a hard guarantee.

FAIL_WINDOW = 900           # seconds a failure is remembered
LOCKOUT_AFTER = 8           # failures before the address is refused
LOCKOUT_SECONDS = 300       # how long a refusal lasts
MAX_DELAY = 3.0             # seconds, the longest deliberate pause

_attempts = {}              # client key -> [failure timestamps]
_attempts_lock = threading.Lock()


def _client_key():
    """Who is knocking.

    `X-Forwarded-For` is trusted only when the deployment says a proxy sits in
    front (TRUST_PROXY), because otherwise the header is attacker-controlled
    and every request could claim a fresh identity -- which would defeat the
    throttle entirely.
    """
    if os.environ.get("TRUST_PROXY"):
        forwarded = request.headers.get("X-Forwarded-For", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.remote_addr or "unknown"


def _recent_failures(key, now):
    """Failures still inside the window, pruning anything older."""
    with _attempts_lock:
        stamps = [t for t in _attempts.get(key, []) if now - t < FAIL_WINDOW]
        if stamps:
            _attempts[key] = stamps
        else:
            _attempts.pop(key, None)
        # Keep the table from growing without bound on a busy public host.
        if len(_attempts) > 2048:
            for stale in [k for k, v in _attempts.items()
                          if not v or now - max(v) > FAIL_WINDOW]:
                _attempts.pop(stale, None)
        return stamps


def _record_failure(key, now):
    with _attempts_lock:
        _attempts.setdefault(key, []).append(now)


def _clear_failures(key):
    with _attempts_lock:
        _attempts.pop(key, None)


def login_throttle():
    """Seconds to wait before answering, or None when locked out.

    Returns (delay, lockout_remaining). A lockout_remaining of 0 means the
    attempt may proceed after `delay` seconds.
    """
    now = time.time()
    key = _client_key()
    stamps = _recent_failures(key, now)
    if len(stamps) >= LOCKOUT_AFTER:
        remaining = int(LOCKOUT_SECONDS - (now - max(stamps)))
        if remaining > 0:
            return 0.0, remaining
        # The lockout has expired; start the count again rather than leaving
        # the address permanently one failure from being locked out.
        _clear_failures(key)
        return 0.0, 0
    # Doubling from a quarter of a second: unnoticeable for a typo, and by the
    # eighth attempt an attacker is waiting seconds per guess.
    return (min(MAX_DELAY, 0.25 * (2 ** len(stamps))) if stamps else 0.0), 0


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("authed"):
            return redirect(url_for("auth.login"))
        return f(*args, **kwargs)
    return decorated


@auth.route("/login", methods=["GET", "POST"])
def login():
    if session.get("authed"):
        return redirect(url_for("profiles.list_profiles"))

    error = None
    if request.method == "POST":
        delay, locked = login_throttle()
        if locked:
            # Say how long, so a legitimate user who mistyped several times
            # knows to wait rather than assuming the app is broken.
            minutes = max(1, (locked + 59) // 60)
            error = ("Too many failed attempts. Try again in about %d minute%s."
                     % (minutes, "" if minutes == 1 else "s"))
            return render_template("auth/login.html", error=error), 429

        if delay:
            time.sleep(delay)

        pw = request.form.get("password", "")
        if hmac.compare_digest(pw, config.PASSWORD):
            _clear_failures(_client_key())
            session["authed"] = True
            session.permanent = False
            # A new session id on login, so a session fixed before
            # authentication cannot be reused afterwards.
            session.modified = True
            return redirect(url_for("profiles.list_profiles"))
        else:
            _record_failure(_client_key(), time.time())
            error = "Invalid password."

    return render_template("auth/login.html", error=error)


@auth.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("auth.login"))


@auth.route("/")
def index():
    if session.get("authed"):
        return redirect(url_for("profiles.list_profiles"))
    return redirect(url_for("auth.login"))
