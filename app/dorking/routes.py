"""Dork engine.

Builds search-engine queries. Templates are static reference data and stay on
the server; history, favourites and custom templates are the analyst's own
records and live in the browser like everything else.

The old `/search` endpoint called `webbrowser.open()`, which opens a browser on
the *server* -- fine on a laptop, useless (or worse) on anything remote. The
client opens the URL itself now, so this just builds it.
"""

from urllib.parse import quote_plus

from flask import Blueprint, jsonify, render_template, request

from ..auth.routes import login_required
from ..models import DorkTemplate

dorking = Blueprint("dorking", __name__, url_prefix="/dorks")

# Where a built query can be run. Adding an engine here adds it to the UI.
ENGINES = {
    "google": "https://www.google.com/search?q=",
    "bing": "https://www.bing.com/search?q=",
    "duckduckgo": "https://duckduckgo.com/?q=",
    "yandex": "https://yandex.com/search/?text=",
    "startpage": "https://www.startpage.com/sp/search?query=",
}


@dorking.route("/")
@login_required
def index():
    return render_template("dorking/index.html")


@dorking.route("/templates")
@login_required
def get_templates():
    """Built-in templates, grouped by category.

    These are reference data shipped with the app, not user records, so they
    are served from the database rather than stored per browser.
    """
    templates = DorkTemplate.query.order_by(DorkTemplate.category,
                                            DorkTemplate.name).all()
    grouped = {}
    for t in templates:
        grouped.setdefault(t.category, []).append(t.to_dict())
    return jsonify(grouped)


@dorking.route("/build", methods=["POST"])
@login_required
def build():
    """Turn a query into runnable search URLs. Stores nothing."""
    data = request.json or {}
    query = (data.get("query") or "").strip()
    if not query:
        return jsonify({"error": "Enter a query first."}), 400

    encoded = quote_plus(query)
    return jsonify({
        "ok": True,
        "query": query,
        "urls": [{"engine": name, "url": base + encoded}
                 for name, base in ENGINES.items()],
        # The primary link the UI opens on a plain "Search".
        "url": ENGINES["google"] + encoded,
    })
