import json
import uuid
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import (Blueprint, render_template, request, Response,
                   stream_with_context, jsonify, current_app)
import requests as req_lib
from ..extensions import db
from ..models import OsintResult, Profile
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
    profiles = Profile.query.order_by(Profile.codename).all()
    return render_template("username_osint/index.html", profiles=profiles,
                           platform_count=len(PLATFORMS))


@osint.route("/check")
@login_required
def check():
    username = request.args.get("username", "").strip()
    if not username:
        return jsonify({"error": "Username required"}), 400

    run_id = str(uuid.uuid4())

    def generate():
        # Send run_id first so client can save it
        yield f"data: {json.dumps({'type': 'start', 'run_id': run_id, 'total': len(PLATFORMS)})}\n\n"

        with current_app.app_context():
            with ThreadPoolExecutor(max_workers=20) as pool:
                futures = {
                    pool.submit(_check_platform, username, name, data): name
                    for name, data in PLATFORMS.items()
                }
                done = 0
                for future in as_completed(futures):
                    done += 1
                    try:
                        result = future.result()
                    except Exception:
                        result = {
                            "platform": futures[future],
                            "url": "",
                            "status": "error",
                            "http_code": None,
                            "username": username,
                        }

                    # Persist to DB
                    try:
                        r = OsintResult(
                            run_id=run_id,
                            username=username,
                            platform=result["platform"],
                            url=result.get("url", ""),
                            status=result["status"],
                            http_code=result.get("http_code"),
                        )
                        db.session.add(r)
                        db.session.commit()
                        result["id"] = r.id
                    except Exception:
                        db.session.rollback()

                    result["done"] = done
                    yield f"data: {json.dumps(result)}\n\n"

        yield f"data: {json.dumps({'type': 'done', 'run_id': run_id})}\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "X-Accel-Buffering": "no",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )


@osint.route("/save", methods=["POST"])
@login_required
def save_to_profile():
    data = request.json
    run_id = data.get("run_id")
    profile_id = data.get("profile_id")

    if not run_id or not profile_id:
        return jsonify({"error": "run_id and profile_id required"}), 400

    Profile.query.get_or_404(int(profile_id))

    updated = OsintResult.query.filter_by(run_id=run_id).update(
        {"profile_id": int(profile_id)}
    )
    db.session.commit()
    return jsonify({"ok": True, "updated": updated})


@osint.route("/history")
@login_required
def history():
    # Group by run_id, return latest 20 runs
    from sqlalchemy import func
    runs = (
        db.session.query(
            OsintResult.run_id,
            OsintResult.username,
            func.count(OsintResult.id).label("total"),
            func.sum(db.case((OsintResult.status == "found", 1), else_=0)).label("found"),
            func.max(OsintResult.checked_at).label("checked_at"),
        )
        .group_by(OsintResult.run_id, OsintResult.username)
        .order_by(func.max(OsintResult.checked_at).desc())
        .limit(20)
        .all()
    )
    return jsonify([
        {
            "run_id": r.run_id,
            "username": r.username,
            "total": r.total,
            "found": r.found,
            "checked_at": r.checked_at.strftime("%Y-%m-%d %H:%M") if r.checked_at else "",
        }
        for r in runs
    ])
