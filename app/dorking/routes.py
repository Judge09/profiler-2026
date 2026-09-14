import webbrowser
from urllib.parse import quote_plus
from flask import (Blueprint, render_template, request, jsonify)
from ..extensions import db
from ..models import DorkTemplate, DorkHistory, DorkFavorite, Profile
from ..auth.routes import login_required

dorking = Blueprint("dorking", __name__, url_prefix="/dorks")


@dorking.route("/")
@login_required
def index():
    profiles = Profile.query.order_by(Profile.codename).all()
    return render_template("dorking/index.html", profiles=profiles)


@dorking.route("/templates")
@login_required
def get_templates():
    templates = DorkTemplate.query.order_by(DorkTemplate.category, DorkTemplate.name).all()
    grouped = {}
    for t in templates:
        grouped.setdefault(t.category, []).append(t.to_dict())
    return jsonify(grouped)


@dorking.route("/search", methods=["POST"])
@login_required
def search():
    data = request.json
    query = (data.get("query") or "").strip()
    template_id = data.get("template_id")
    profile_id = data.get("profile_id")

    if not query:
        return jsonify({"error": "Query required"}), 400

    # Save to history
    hist = DorkHistory(
        query=query,
        template_id=template_id or None,
        profile_id=profile_id or None,
    )
    db.session.add(hist)
    db.session.commit()

    # Open in browser
    url = f"https://www.google.com/search?q={quote_plus(query)}"
    try:
        webbrowser.open(url)
    except Exception:
        pass

    return jsonify({"ok": True, "url": url})


@dorking.route("/history")
@login_required
def history():
    items = DorkHistory.query.order_by(DorkHistory.used_at.desc()).limit(100).all()
    return jsonify([i.to_dict() for i in items])


@dorking.route("/favorites", methods=["GET"])
@login_required
def list_favorites():
    favs = DorkFavorite.query.order_by(DorkFavorite.saved_at.desc()).all()
    return jsonify([f.to_dict() for f in favs])


@dorking.route("/favorites", methods=["POST"])
@login_required
def save_favorite():
    data = request.json
    query = (data.get("query") or "").strip()
    label = (data.get("label") or "").strip()
    if not query:
        return jsonify({"error": "Query required"}), 400
    fav = DorkFavorite(query=query, label=label or None)
    db.session.add(fav)
    db.session.commit()
    return jsonify(fav.to_dict()), 201


@dorking.route("/favorites/<int:fav_id>", methods=["DELETE"])
@login_required
def delete_favorite(fav_id):
    fav = DorkFavorite.query.get_or_404(fav_id)
    db.session.delete(fav)
    db.session.commit()
    return jsonify({"ok": True})


@dorking.route("/custom", methods=["POST"])
@login_required
def create_custom():
    data = request.json
    name = (data.get("name") or "").strip()
    template = (data.get("template") or "").strip()
    category = (data.get("category") or "Custom").strip()
    description = (data.get("description") or "").strip()
    if not name or not template:
        return jsonify({"error": "Name and template required"}), 400
    t = DorkTemplate(name=name, template=template, category=category,
                     description=description, is_builtin=False)
    db.session.add(t)
    db.session.commit()
    return jsonify(t.to_dict()), 201


@dorking.route("/custom/<int:tmpl_id>", methods=["DELETE"])
@login_required
def delete_custom(tmpl_id):
    t = DorkTemplate.query.get_or_404(tmpl_id)
    if t.is_builtin:
        return jsonify({"error": "Cannot delete built-in templates"}), 403
    db.session.delete(t)
    db.session.commit()
    return jsonify({"ok": True})
