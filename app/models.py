import json
from datetime import datetime
from .extensions import db

# Many-to-many join table
profile_tag = db.Table(
    "profile_tag",
    db.Column("profile_id", db.Integer, db.ForeignKey("profile.id", ondelete="CASCADE"), primary_key=True),
    db.Column("tag_id", db.Integer, db.ForeignKey("tag.id", ondelete="CASCADE"), primary_key=True),
)


class Profile(db.Model):
    __tablename__ = "profile"

    id = db.Column(db.Integer, primary_key=True)
    codename = db.Column(db.Text, nullable=False, unique=True)
    real_name = db.Column(db.Text)
    dob = db.Column(db.Text)
    nationality = db.Column(db.Text)
    occupation = db.Column(db.Text)
    physical_desc = db.Column(db.Text)
    _known_aliases = db.Column("known_aliases", db.Text, default="[]")
    photo_path = db.Column(db.Text)
    bio_notes = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    social_links = db.relationship("SocialLink", backref="profile", cascade="all, delete-orphan", lazy=True)
    tags = db.relationship("Tag", secondary=profile_tag, backref="profiles", lazy=True)
    radar_config = db.relationship("RadarConfig", backref="profile", cascade="all, delete-orphan", uselist=False, lazy=True)
    intel_notes = db.relationship("IntelNote", backref="profile", cascade="all, delete-orphan", order_by="IntelNote.created_at.desc()", lazy=True)
    osint_results = db.relationship("OsintResult", backref="profile", lazy=True)
    graphs = db.relationship("Graph", backref="profile", lazy=True)

    @property
    def known_aliases(self):
        try:
            return json.loads(self._known_aliases or "[]")
        except (json.JSONDecodeError, TypeError):
            return []

    @known_aliases.setter
    def known_aliases(self, value):
        if isinstance(value, list):
            self._known_aliases = json.dumps(value)
        else:
            self._known_aliases = json.dumps([])

    def to_dict(self):
        return {
            "id": self.id,
            "codename": self.codename,
            "real_name": self.real_name,
            "photo_path": self.photo_path,
            "tags": [{"id": t.id, "name": t.name, "color": t.color} for t in self.tags],
            "threat_score": self.radar_config.axis5_score if self.radar_config else 0,
        }


class SocialLink(db.Model):
    __tablename__ = "social_link"

    id = db.Column(db.Integer, primary_key=True)
    profile_id = db.Column(db.Integer, db.ForeignKey("profile.id", ondelete="CASCADE"), nullable=False)
    platform = db.Column(db.Text, nullable=False)
    url = db.Column(db.Text, nullable=False)
    username = db.Column(db.Text)


class Tag(db.Model):
    __tablename__ = "tag"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.Text, nullable=False, unique=True)
    color = db.Column(db.Text, default="#00ff99")

    def to_dict(self):
        return {"id": self.id, "name": self.name, "color": self.color}


class RadarConfig(db.Model):
    __tablename__ = "radar_config"

    id = db.Column(db.Integer, primary_key=True)
    profile_id = db.Column(db.Integer, db.ForeignKey("profile.id", ondelete="CASCADE"), nullable=False, unique=True)

    axis1_label = db.Column(db.Text, default="Academics")
    axis1_score = db.Column(db.Float, default=0.0)
    axis2_label = db.Column(db.Text, default="Physical")
    axis2_score = db.Column(db.Float, default=0.0)
    axis3_label = db.Column(db.Text, default="Social")
    axis3_score = db.Column(db.Float, default=0.0)
    axis4_label = db.Column(db.Text, default="Influence")
    axis4_score = db.Column(db.Float, default=0.0)
    axis5_label = db.Column(db.Text, default="Threat")
    axis5_score = db.Column(db.Float, default=0.0)
    axis6_label = db.Column(db.Text, default="Digital")
    axis6_score = db.Column(db.Float, default=0.0)

    def to_dict(self):
        return {
            "labels": [
                self.axis1_label, self.axis2_label, self.axis3_label,
                self.axis4_label, self.axis5_label, self.axis6_label,
            ],
            "scores": [
                self.axis1_score, self.axis2_score, self.axis3_score,
                self.axis4_score, self.axis5_score, self.axis6_score,
            ],
        }


class IntelNote(db.Model):
    __tablename__ = "intel_note"

    id = db.Column(db.Integer, primary_key=True)
    profile_id = db.Column(db.Integer, db.ForeignKey("profile.id", ondelete="CASCADE"), nullable=False)
    content = db.Column(db.Text, nullable=False)
    source = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            "id": self.id,
            "content": self.content,
            "source": self.source,
            "created_at": self.created_at.strftime("%Y-%m-%d %H:%M"),
        }


class DorkTemplate(db.Model):
    __tablename__ = "dork_template"

    id = db.Column(db.Integer, primary_key=True)
    category = db.Column(db.Text, nullable=False)
    name = db.Column(db.Text, nullable=False)
    template = db.Column(db.Text, nullable=False)
    description = db.Column(db.Text)
    is_builtin = db.Column(db.Boolean, default=False)

    def to_dict(self):
        return {
            "id": self.id,
            "category": self.category,
            "name": self.name,
            "template": self.template,
            "description": self.description,
            "is_builtin": self.is_builtin,
        }


class DorkHistory(db.Model):
    __tablename__ = "dork_history"

    id = db.Column(db.Integer, primary_key=True)
    query = db.Column(db.Text, nullable=False)
    template_id = db.Column(db.Integer, db.ForeignKey("dork_template.id"), nullable=True)
    profile_id = db.Column(db.Integer, db.ForeignKey("profile.id"), nullable=True)
    used_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            "id": self.id,
            "query": self.query,
            "used_at": self.used_at.strftime("%Y-%m-%d %H:%M"),
        }


class DorkFavorite(db.Model):
    __tablename__ = "dork_favorite"

    id = db.Column(db.Integer, primary_key=True)
    query = db.Column(db.Text, nullable=False)
    label = db.Column(db.Text)
    saved_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {"id": self.id, "query": self.query, "label": self.label}


class OsintResult(db.Model):
    __tablename__ = "osint_result"

    id = db.Column(db.Integer, primary_key=True)
    run_id = db.Column(db.Text, nullable=False, index=True)
    username = db.Column(db.Text, nullable=False)
    platform = db.Column(db.Text, nullable=False)
    url = db.Column(db.Text)
    status = db.Column(db.Text, nullable=False)  # found / not_found / error / timeout
    http_code = db.Column(db.Integer)
    checked_at = db.Column(db.DateTime, default=datetime.utcnow)
    profile_id = db.Column(db.Integer, db.ForeignKey("profile.id"), nullable=True)

    def to_dict(self):
        return {
            "id": self.id,
            "run_id": self.run_id,
            "username": self.username,
            "platform": self.platform,
            "url": self.url,
            "status": self.status,
            "http_code": self.http_code,
        }


class Graph(db.Model):
    __tablename__ = "graph"

    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.Text, nullable=False)
    profile_id = db.Column(db.Integer, db.ForeignKey("profile.id"), nullable=True)
    graph_json = db.Column(db.Text, nullable=False, default='{"nodes":[],"edges":[]}')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def to_dict(self):
        return {
            "id": self.id,
            "title": self.title,
            "profile_id": self.profile_id,
            "graph_json": self.graph_json,
            "updated_at": self.updated_at.strftime("%Y-%m-%d %H:%M") if self.updated_at else "",
        }


class MonitorWatch(db.Model):
    """A saved monitoring case: what to look for and how to score it."""
    __tablename__ = "monitor_watch"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.Text, nullable=False)
    subject = db.Column(db.Text)

    # Keywords. `keywords` stays the canonical comma string for backward
    # compatibility; the three columns below are the structured form the UI
    # edits and the engine prefers when present.
    keywords = db.Column(db.Text, default="")
    kw_required = db.Column(db.Text, default="")   # newline separated, all must match
    kw_optional = db.Column(db.Text, default="")   # newline separated, broaden only
    kw_excluded = db.Column(db.Text, default="")   # newline separated, reject on match
    kw_match_mode = db.Column(db.Text, default="all")  # all / any -- for required terms

    # Media Release Threat mode
    mode_release = db.Column(db.Boolean, default=False)
    reference_text = db.Column(db.Text, default="")
    official_accounts = db.Column(db.Text, default="")
    official_domains = db.Column(db.Text, default="")

    # Digital Hunter mode
    mode_hunter = db.Column(db.Boolean, default=False)

    # Manual scoring controls
    custom_flags = db.Column(db.Text, default="")
    weights_json = db.Column(db.Text, default="{}")
    threshold_review = db.Column(db.Integer, default=30)
    threshold_high = db.Column(db.Integer, default=60)

    profile_id = db.Column(db.Integer, db.ForeignKey("profile.id"), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    posts = db.relationship("MonitorPost", backref="watch",
                            cascade="all, delete-orphan", lazy=True)

    @property
    def weights(self):
        try:
            return json.loads(self.weights_json or "{}")
        except (json.JSONDecodeError, TypeError):
            return {}

    @weights.setter
    def weights(self, value):
        self.weights_json = json.dumps(value if isinstance(value, dict) else {})

    @property
    def mode_label(self):
        modes = []
        if self.mode_release:
            modes.append("Media Release Threat")
        if self.mode_hunter:
            modes.append("Digital Hunter")
        return " + ".join(modes) if modes else "Standard"

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "subject": self.subject,
            "keywords": self.keywords,
            "kw_required": self.kw_required or "",
            "kw_optional": self.kw_optional or "",
            "kw_excluded": self.kw_excluded or "",
            "kw_match_mode": self.kw_match_mode or "all",
            "mode_release": self.mode_release,
            "mode_hunter": self.mode_hunter,
            "mode_label": self.mode_label,
            "reference_text": self.reference_text,
            "official_accounts": self.official_accounts,
            "official_domains": self.official_domains,
            "custom_flags": self.custom_flags,
            "weights": self.weights,
            "threshold_review": self.threshold_review,
            "threshold_high": self.threshold_high,
            "profile_id": self.profile_id,
            "post_count": len(self.posts),
            "updated_at": self.updated_at.strftime("%Y-%m-%d %H:%M") if self.updated_at else "",
        }


class MonitorPost(db.Model):
    """One collected post inside a watch."""
    __tablename__ = "monitor_post"

    id = db.Column(db.Integer, primary_key=True)
    watch_id = db.Column(db.Integer, db.ForeignKey("monitor_watch.id", ondelete="CASCADE"),
                         nullable=False, index=True)
    platform = db.Column(db.Text, default="Other")
    author = db.Column(db.Text, nullable=False)
    handle = db.Column(db.Text, default="")
    verified = db.Column(db.Boolean, default=False)
    text = db.Column(db.Text, nullable=False)
    url = db.Column(db.Text, default="")

    source = db.Column(db.Text, default="manual")
    source_url = db.Column(db.Text, default="")
    link_kind = db.Column(db.Text, default="direct")  # direct / search

    # Comments are stored as posts with a parent, so they inherit the whole
    # triage, filtering, export and link-map pipeline instead of duplicating
    # it. `kind` is "post" or "comment"; the parent columns are empty on posts.
    kind = db.Column(db.Text, default="post", index=True)
    parent_url = db.Column(db.Text, default="")
    parent_author = db.Column(db.Text, default="")
    engagement_json = db.Column(db.Text, default="{}")
    posted_at = db.Column(db.Text)
    collected_at = db.Column(db.DateTime, default=datetime.utcnow)
    dedupe_key = db.Column(db.Text, index=True)

    # Analyst overrides and triage state
    profile_id = db.Column(db.Integer, db.ForeignKey("profile.id"), nullable=True)
    manual_score = db.Column(db.Integer, nullable=True)
    manual_verdict = db.Column(db.Text)
    analyst_note = db.Column(db.Text, default="")
    status = db.Column(db.Text, default="new")  # new / reviewed / escalated / dismissed
    pinned = db.Column(db.Boolean, default=False)

    # Cached scoring. Filled by the engine and reused until the watch's rules
    # change (tracked by `cache_rules_hash`) or the post itself is edited.
    # Keeping the verdict in a column is what lets the dashboard, the watch
    # list and pagination run as plain SQL instead of rescoring every post.
    cached_score = db.Column(db.Integer, index=True)
    cached_verdict = db.Column(db.Text, index=True)
    cached_types = db.Column(db.Text, default="")
    cached_relevant = db.Column(db.Boolean, default=True)
    cached_json = db.Column(db.Text)
    cache_rules_hash = db.Column(db.Text, index=True)
    posted_ts = db.Column(db.DateTime, index=True)  # parsed posted_at, for sorting

    linked_profile = db.relationship("Profile", foreign_keys=[profile_id], lazy=True)

    @property
    def engagement(self):
        try:
            return json.loads(self.engagement_json or "{}")
        except (json.JSONDecodeError, TypeError):
            return {}

    @engagement.setter
    def engagement(self, value):
        self.engagement_json = json.dumps(value if isinstance(value, dict) else {})

    def to_dict(self):
        return {
            "id": self.id,
            "watch_id": self.watch_id,
            "platform": self.platform,
            "author": self.author,
            "handle": self.handle,
            "verified": self.verified,
            "text": self.text,
            "url": self.url,
            "source": self.source,
            "source_url": self.source_url,
            "link_kind": self.link_kind or "direct",
            "kind": self.kind or "post",
            "parent_url": self.parent_url or "",
            "parent_author": self.parent_author or "",
            "engagement": self.engagement,
            "posted_at": self.posted_at,
            "collected_at": self.collected_at.strftime("%Y-%m-%d %H:%M") if self.collected_at else "",
            "profile_id": self.profile_id,
            "profile_codename": self.linked_profile.codename if self.linked_profile else None,
            "manual_score": self.manual_score,
            "manual_verdict": self.manual_verdict,
            "analyst_note": self.analyst_note,
            "status": self.status,
            "pinned": self.pinned,
        }


class MonitorFeed(db.Model):
    """A saved live source attached to a watch, for repeat collection runs."""
    __tablename__ = "monitor_feed"

    id = db.Column(db.Integer, primary_key=True)
    watch_id = db.Column(db.Integer, db.ForeignKey("monitor_watch.id", ondelete="CASCADE"),
                         nullable=False, index=True)
    source = db.Column(db.Text, nullable=False)
    url = db.Column(db.Text, default="")
    options_json = db.Column(db.Text, default="{}")
    enabled = db.Column(db.Boolean, default=True)
    last_run = db.Column(db.DateTime)
    last_note = db.Column(db.Text, default="")

    @property
    def options(self):
        try:
            return json.loads(self.options_json or "{}")
        except (json.JSONDecodeError, TypeError):
            return {}

    @options.setter
    def options(self, value):
        self.options_json = json.dumps(value if isinstance(value, dict) else {})

    def to_dict(self):
        return {
            "id": self.id,
            "source": self.source,
            "url": self.url,
            "options": self.options,
            "enabled": self.enabled,
            "last_run": self.last_run.strftime("%Y-%m-%d %H:%M") if self.last_run else "",
            "last_note": self.last_note,
        }


class MonitorCredential(db.Model):
    """An encrypted secret for reaching an authenticated source.

    The secret itself lives in `secret_blob`, encrypted by app.monitor.vault.
    Nothing sensitive is stored in the clear; the plain columns exist so the
    list can be shown while the vault is locked.
    """
    __tablename__ = "monitor_credential"

    id = db.Column(db.Integer, primary_key=True)
    label = db.Column(db.Text, nullable=False)
    platform = db.Column(db.Text, nullable=False, default="generic")
    kind = db.Column(db.Text, nullable=False, default="cookies")  # cookies/token/password
    secret_blob = db.Column(db.Text, nullable=False, default="")

    account_hint = db.Column(db.Text, default="")   # e.g. a masked handle
    expires_at = db.Column(db.Text)
    enabled = db.Column(db.Boolean, default=True)

    last_used = db.Column(db.DateTime)
    last_ok = db.Column(db.Boolean)
    last_error = db.Column(db.Text, default="")
    use_count = db.Column(db.Integer, default=0)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def to_dict(self, health=None):
        return {
            "id": self.id,
            "label": self.label,
            "platform": self.platform,
            "kind": self.kind,
            "account_hint": self.account_hint,
            "expires_at": self.expires_at,
            "enabled": self.enabled,
            "last_used": self.last_used.strftime("%Y-%m-%d %H:%M") if self.last_used else "",
            "last_ok": self.last_ok,
            "last_error": self.last_error,
            "use_count": self.use_count or 0,
            "health": health or {},
        }
