import os
import uuid
from datetime import datetime
from flask import (Blueprint, render_template, request, redirect,
                   url_for, jsonify, current_app, flash)
from PIL import Image, ImageOps
from ..extensions import db
from ..models import Profile, SocialLink, Tag, RadarConfig, IntelNote, profile_tag
from ..auth.routes import login_required

profiles = Blueprint("profiles", __name__, url_prefix="/profiles")

SOCIAL_PLATFORMS = [
    "Twitter/X", "Instagram", "Facebook", "TikTok", "Telegram",
    "Discord", "Reddit", "LinkedIn", "GitHub", "YouTube",
    "Snapchat", "Pinterest", "Twitch", "OnlyFans", "Custom",
]


# ── Helpers ──────────────────────────────────────────────────────────────────

def _save_photo(file_storage, old_path=None):
    """Process and save profile photo. Returns relative path for url_for static."""
    upload_dir = current_app.config["UPLOAD_FOLDER"]
    os.makedirs(upload_dir, exist_ok=True)

    filename = f"{uuid.uuid4().hex}.jpg"
    save_path = os.path.join(upload_dir, filename)

    img = Image.open(file_storage.stream).convert("RGB")
    img = ImageOps.exif_transpose(img)
    img.thumbnail((400, 400))
    img.save(save_path, "JPEG", quality=85)

    # Delete old photo if replacing
    if old_path:
        _delete_photo(old_path)

    return f"uploads/profile_pics/{filename}"


def _delete_photo(relative_path):
    if not relative_path:
        return
    try:
        full = os.path.join(current_app.root_path, "static", relative_path)
        if os.path.isfile(full):
            os.remove(full)
    except Exception:
        pass


def _parse_aliases(raw):
    """Parse comma-separated alias string into a list."""
    if not raw:
        return []
    return [a.strip() for a in raw.split(",") if a.strip()]


def _update_social_links(profile, form):
    """Replace all social links for a profile from form data."""
    SocialLink.query.filter_by(profile_id=profile.id).delete()
    platforms = form.getlist("social_platform")
    urls = form.getlist("social_url")
    usernames = form.getlist("social_username")
    for plat, url, uname in zip(platforms, urls, usernames):
        if plat and url:
            db.session.add(SocialLink(
                profile_id=profile.id,
                platform=plat,
                url=url,
                username=uname or "",
            ))


# ── Routes ───────────────────────────────────────────────────────────────────

@profiles.route("/")
@login_required
def list_profiles():
    q = request.args.get("q", "").strip()
    tag_filter = request.args.get("tag", "").strip()
    sort = request.args.get("sort", "date").strip()

    query = Profile.query

    if q:
        like = f"%{q}%"
        query = query.filter(
            db.or_(Profile.codename.ilike(like), Profile.real_name.ilike(like))
        )

    if tag_filter:
        query = query.join(Profile.tags).filter(Tag.name == tag_filter)

    if sort == "name":
        query = query.order_by(Profile.codename.asc())
    elif sort == "threat":
        query = (
            query
            .outerjoin(RadarConfig, RadarConfig.profile_id == Profile.id)
            .order_by(db.nullslast(RadarConfig.axis5_score.desc()))
        )
    else:
        query = query.order_by(Profile.created_at.desc())

    all_profiles = query.all()
    all_tags = Tag.query.order_by(Tag.name).all()

    return render_template(
        "profiles/list.html",
        profiles=all_profiles,
        tags=all_tags,
        q=q,
        tag_filter=tag_filter,
        sort=sort,
    )


@profiles.route("/new", methods=["GET", "POST"])
@login_required
def new_profile():
    all_tags = Tag.query.order_by(Tag.name).all()

    if request.method == "POST":
        codename = request.form.get("codename", "").strip()
        if not codename:
            return render_template("profiles/form.html", all_tags=all_tags,
                                   error="Codename is required.", platforms=SOCIAL_PLATFORMS)

        if Profile.query.filter_by(codename=codename).first():
            return render_template("profiles/form.html", all_tags=all_tags,
                                   error="Codename already exists.", platforms=SOCIAL_PLATFORMS)

        profile = Profile(
            codename=codename,
            real_name=request.form.get("real_name", "").strip() or None,
            dob=request.form.get("dob", "").strip() or None,
            nationality=request.form.get("nationality", "").strip() or None,
            occupation=request.form.get("occupation", "").strip() or None,
            physical_desc=request.form.get("physical_desc", "").strip() or None,
            bio_notes=request.form.get("bio_notes", "").strip() or None,
        )
        profile.known_aliases = _parse_aliases(request.form.get("known_aliases", ""))

        # Photo
        photo = request.files.get("photo")
        if photo and photo.filename:
            profile.photo_path = _save_photo(photo)

        db.session.add(profile)
        db.session.flush()  # get profile.id

        # Tags
        tag_ids = request.form.getlist("tag_ids")
        for tid in tag_ids:
            tag = Tag.query.get(int(tid))
            if tag:
                profile.tags.append(tag)

        # Social links
        _update_social_links(profile, request.form)

        # Radar config (auto-create with defaults)
        db.session.add(RadarConfig(profile_id=profile.id))

        db.session.commit()
        return redirect(url_for("profiles.detail", profile_id=profile.id))

    return render_template("profiles/form.html", all_tags=all_tags,
                           profile=None, platforms=SOCIAL_PLATFORMS)


@profiles.route("/<int:profile_id>")
@login_required
def detail(profile_id):
    p = Profile.query.get_or_404(profile_id)
    all_tags = Tag.query.order_by(Tag.name).all()
    return render_template("profiles/detail.html", p=p, all_tags=all_tags,
                           platforms=SOCIAL_PLATFORMS)


@profiles.route("/<int:profile_id>/edit", methods=["GET", "POST"])
@login_required
def edit_profile(profile_id):
    p = Profile.query.get_or_404(profile_id)
    all_tags = Tag.query.order_by(Tag.name).all()

    if request.method == "POST":
        codename = request.form.get("codename", "").strip()
        if not codename:
            return render_template("profiles/form.html", profile=p, all_tags=all_tags,
                                   error="Codename is required.", platforms=SOCIAL_PLATFORMS)

        existing = Profile.query.filter_by(codename=codename).first()
        if existing and existing.id != p.id:
            return render_template("profiles/form.html", profile=p, all_tags=all_tags,
                                   error="Codename already in use.", platforms=SOCIAL_PLATFORMS)

        p.codename = codename
        p.real_name = request.form.get("real_name", "").strip() or None
        p.dob = request.form.get("dob", "").strip() or None
        p.nationality = request.form.get("nationality", "").strip() or None
        p.occupation = request.form.get("occupation", "").strip() or None
        p.physical_desc = request.form.get("physical_desc", "").strip() or None
        p.bio_notes = request.form.get("bio_notes", "").strip() or None
        p.known_aliases = _parse_aliases(request.form.get("known_aliases", ""))
        p.updated_at = datetime.utcnow()

        # Photo
        photo = request.files.get("photo")
        if photo and photo.filename:
            p.photo_path = _save_photo(photo, old_path=p.photo_path)

        # Tags
        p.tags.clear()
        tag_ids = request.form.getlist("tag_ids")
        for tid in tag_ids:
            tag = Tag.query.get(int(tid))
            if tag:
                p.tags.append(tag)

        _update_social_links(p, request.form)
        db.session.commit()
        return redirect(url_for("profiles.detail", profile_id=p.id))

    return render_template("profiles/form.html", profile=p, all_tags=all_tags,
                           platforms=SOCIAL_PLATFORMS)


@profiles.route("/<int:profile_id>/delete", methods=["POST"])
@login_required
def delete_profile(profile_id):
    p = Profile.query.get_or_404(profile_id)
    _delete_photo(p.photo_path)
    db.session.delete(p)
    db.session.commit()
    return redirect(url_for("profiles.list_profiles"))


# ── Notes ────────────────────────────────────────────────────────────────────

@profiles.route("/<int:profile_id>/notes", methods=["POST"])
@login_required
def add_note(profile_id):
    p = Profile.query.get_or_404(profile_id)
    content = request.json.get("content", "").strip()
    source = request.json.get("source", "").strip()
    if not content:
        return jsonify({"error": "Content required"}), 400
    note = IntelNote(profile_id=p.id, content=content, source=source or None)
    db.session.add(note)
    db.session.commit()
    return jsonify(note.to_dict())


@profiles.route("/<int:profile_id>/notes/<int:note_id>", methods=["DELETE"])
@login_required
def delete_note(profile_id, note_id):
    note = IntelNote.query.filter_by(id=note_id, profile_id=profile_id).first_or_404()
    db.session.delete(note)
    db.session.commit()
    return jsonify({"ok": True})


# ── Radar ────────────────────────────────────────────────────────────────────

@profiles.route("/<int:profile_id>/radar", methods=["POST"])
@login_required
def update_radar(profile_id):
    p = Profile.query.get_or_404(profile_id)
    data = request.json
    rc = p.radar_config
    if not rc:
        rc = RadarConfig(profile_id=p.id)
        db.session.add(rc)

    for i in range(1, 7):
        label = data.get(f"axis{i}_label")
        score = data.get(f"axis{i}_score")
        if label is not None:
            setattr(rc, f"axis{i}_label", label)
        if score is not None:
            setattr(rc, f"axis{i}_score", float(score))

    db.session.commit()
    return jsonify(rc.to_dict())


# ── Tags ─────────────────────────────────────────────────────────────────────

@profiles.route("/tags", methods=["GET"])
@login_required
def list_tags():
    tags = Tag.query.order_by(Tag.name).all()
    return jsonify([t.to_dict() for t in tags])


@profiles.route("/tags", methods=["POST"])
@login_required
def create_tag():
    data = request.json
    name = (data.get("name") or "").strip()
    color = (data.get("color") or "#00ff99").strip()
    if not name:
        return jsonify({"error": "Name required"}), 400
    if Tag.query.filter_by(name=name).first():
        return jsonify({"error": "Tag already exists"}), 409
    tag = Tag(name=name, color=color)
    db.session.add(tag)
    db.session.commit()
    return jsonify(tag.to_dict()), 201


@profiles.route("/tags/<int:tag_id>", methods=["DELETE"])
@login_required
def delete_tag(tag_id):
    tag = Tag.query.get_or_404(tag_id)
    db.session.delete(tag)
    db.session.commit()
    return jsonify({"ok": True})
