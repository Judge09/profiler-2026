import json
import uuid
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import (Blueprint, Response, jsonify, render_template, request,
                   stream_with_context)
import requests as req_lib
from ..auth.routes import login_required

osint = Blueprint("osint", __name__, url_prefix="/osint")

# Load platforms once at module level
_PLATFORMS_PATH = os.path.join(os.path.dirname(__file__), "platforms.json")
with open(_PLATFORMS_PATH, encoding="utf-8") as f:
    PLATFORMS = json.load(f)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


def _check_platform(username, name, data):
    """Check a single platform. Returns dict with result."""
    url_pattern = data["url"]
    url = url_pattern.replace("{}", username)
    error_type = data.get("errorType", "status_code")
    error_msg = data.get("errorMsg", "")

    result = {
        "platform": name,
        "url": url,
        "status": "not_found",
        "http_code": None,
        "username": username,
    }

    try:
        resp = req_lib.get(
            url,
            headers=_HEADERS,
            timeout=10,
            allow_redirects=True,
        )
        result["http_code"] = resp.status_code

        if error_type == "status_code":
            result["status"] = "found" if resp.status_code == 200 else "not_found"
        elif error_type == "message":
            if error_msg and error_msg.lower() in resp.text.lower():
                result["status"] = "not_found"
            elif resp.status_code == 200:
                result["status"] = "found"
            else:
                result["status"] = "not_found"

        # Some platforms answer 200 whether or not the account exists: they
        # serve a JavaScript shell or a bot-check page whose bytes are
        # identical either way, so no HTTP check can distinguish them. Left
        # alone they produced roughly a third of all "found" results falsely,
        # which is worse than no answer -- an analyst cannot tell the real
        # hits from the invented ones. A "not found" from them is still
        # meaningful, so only the positive is downgraded.
        if result["status"] == "found" and data.get("verifiable") is False:
            result["status"] = "unverifiable"

    except req_lib.exceptions.Timeout:
        result["status"] = "timeout"
    except req_lib.exceptions.ConnectionError:
        result["status"] = "error"
    except Exception:
        result["status"] = "error"

    return result


@osint.route("/")
@login_required
def index():
    """Username search. Results are streamed and stored by the browser."""
    return render_template("username_osint/index.html",
                           platform_count=len(PLATFORMS))


@osint.route("/check")
@login_required
def check():
    """Stream a username check across every configured platform.

    Nothing is written here: results go to the client as they arrive and the
    browser stores them. That also removes the per-platform database write that
    used to happen inside the stream, which was the slowest part of a run.
    """
    username = request.args.get("username", "").strip()
    if not username:
        return jsonify({"error": "Username required"}), 400
    if len(username) > 100:
        return jsonify({"error": "That username is too long."}), 400

    run_id = str(uuid.uuid4())

    def generate():
        yield "data: %s\n\n" % json.dumps(
            {"type": "start", "run_id": run_id, "total": len(PLATFORMS)})

        found = 0
        with ThreadPoolExecutor(max_workers=20) as pool:
            futures = {pool.submit(_check_platform, username, name, data): name
                       for name, data in PLATFORMS.items()}
            done = 0
            for future in as_completed(futures):
                done += 1
                try:
                    result = future.result()
                except Exception:
                    result = {"platform": futures[future], "url": "",
                              "status": "error", "http_code": None,
                              "username": username}
                if result["status"] == "found":
                    found += 1
                result["done"] = done
                yield "data: %s\n\n" % json.dumps(result)

        yield "data: %s\n\n" % json.dumps(
            {"type": "done", "run_id": run_id, "found": found,
             "total": len(PLATFORMS)})

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "X-Accel-Buffering": "no",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )
