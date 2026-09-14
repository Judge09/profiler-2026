import base64
import json
from datetime import datetime
from flask import (Blueprint, render_template, request, jsonify,
                   send_file, Response)
import io
from ..extensions import db
from ..models import Graph, Profile
from ..auth.routes import login_required

linkmap = Blueprint("linkmap", __name__, url_prefix="/linkmap")


@linkmap.route("/")
@login_required
def list_maps():
    graphs = Graph.query.order_by(Graph.updated_at.desc()).all()
    return render_template("linkmap/list.html", graphs=graphs)


@linkmap.route("/new")
@login_required
def new_map():
    profiles = Profile.query.order_by(Profile.codename).all()
    return render_template("linkmap/editor.html", graph=None, profiles=profiles)


@linkmap.route("/<int:graph_id>")
@login_required
def edit_map(graph_id):
    graph = Graph.query.get_or_404(graph_id)
    profiles = Profile.query.order_by(Profile.codename).all()
    return render_template("linkmap/editor.html", graph=graph, profiles=profiles)


@linkmap.route("/profile/<int:pid>")
@login_required
def profile_map(pid):
    profile = Profile.query.get_or_404(pid)
    # Find or create a graph for this profile
    graph = Graph.query.filter_by(profile_id=pid).first()
    if not graph:
        # Bootstrap with a Person node for this profile
        node_label = profile.codename
        initial_json = json.dumps({
            "nodes": [{"id": 1, "label": node_label, "type": "person", "title": profile.real_name or ""}],
            "edges": []
        })
        graph = Graph(
            title=f"{profile.codename} — Link Map",
            profile_id=pid,
            graph_json=initial_json,
        )
        db.session.add(graph)
        db.session.commit()

    profiles = Profile.query.order_by(Profile.codename).all()
    return render_template("linkmap/editor.html", graph=graph, profiles=profiles)


@linkmap.route("/save", methods=["POST"])
@login_required
def save_map():
    data = request.json
    graph_id = data.get("id")
    title = (data.get("title") or "Untitled Map").strip()
    graph_json = data.get("graph_json", '{"nodes":[],"edges":[]}')
    profile_id = data.get("profile_id") or None

    if graph_id:
        graph = Graph.query.get_or_404(int(graph_id))
        graph.title = title
        graph.graph_json = graph_json
        graph.profile_id = int(profile_id) if profile_id else None
        graph.updated_at = datetime.utcnow()
    else:
        graph = Graph(
            title=title,
            graph_json=graph_json,
            profile_id=int(profile_id) if profile_id else None,
        )
        db.session.add(graph)

    db.session.commit()
    return jsonify({"ok": True, "id": graph.id, "title": graph.title})


@linkmap.route("/<int:graph_id>/delete", methods=["POST"])
@login_required
def delete_map(graph_id):
    graph = Graph.query.get_or_404(graph_id)
    db.session.delete(graph)
    db.session.commit()
    return jsonify({"ok": True})


@linkmap.route("/<int:graph_id>/export", methods=["POST"])
@login_required
def export_map(graph_id):
    """Receive base64 PNG from client, return as file download."""
    data = request.json
    data_url = data.get("data_url", "")

    if not data_url.startswith("data:image/"):
        return jsonify({"error": "Invalid image data"}), 400

    # Strip the data URL prefix
    try:
        header, b64data = data_url.split(",", 1)
        img_bytes = base64.b64decode(b64data)
    except Exception:
        return jsonify({"error": "Failed to decode image"}), 400

    graph = Graph.query.get_or_404(graph_id)
    filename = f"{graph.title.replace(' ', '_')}.png"

    return send_file(
        io.BytesIO(img_bytes),
        mimetype="image/png",
        as_attachment=True,
        download_name=filename,
    )
