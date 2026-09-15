"""Lightweight schema migrations.

`db.create_all()` creates missing tables but never alters existing ones, so a
database created by an earlier version of this app is missing every column
added since. Rather than pull in Alembic for a single-file SQLite app, this
module adds missing columns and indexes idempotently at startup.

Each entry is additive and nullable, which is what makes running it on every
boot safe: adding a column that already exists is detected and skipped, and
nothing is ever dropped or rewritten.
"""

from sqlalchemy import inspect, text

from .extensions import db

# table -> [(column, DDL type)]
COLUMNS = {
    "monitor_watch": [
        ("kw_required", "TEXT DEFAULT ''"),
        ("kw_optional", "TEXT DEFAULT ''"),
        ("kw_excluded", "TEXT DEFAULT ''"),
        ("kw_match_mode", "TEXT DEFAULT 'all'"),
    ],
    "monitor_post": [
        ("cached_score", "INTEGER"),
        ("cached_verdict", "TEXT"),
        ("cached_types", "TEXT DEFAULT ''"),
        ("cached_relevant", "BOOLEAN DEFAULT 1"),
        ("cached_json", "TEXT"),
        ("cache_rules_hash", "TEXT"),
        ("posted_ts", "DATETIME"),
        ("kind", "TEXT DEFAULT 'post'"),
        ("parent_url", "TEXT DEFAULT ''"),
        ("parent_author", "TEXT DEFAULT ''"),
        ("engagement_json", "TEXT DEFAULT '{}'"),
    ],
}

# Indexes that make the cached-score queries cheap. Named so the IF NOT EXISTS
# check is reliable across restarts.
INDEXES = [
    ("ix_post_watch_verdict", "monitor_post", "watch_id, cached_verdict"),
    ("ix_post_watch_score", "monitor_post", "watch_id, cached_score"),
    ("ix_post_watch_posted", "monitor_post", "watch_id, posted_ts"),
    ("ix_post_watch_status", "monitor_post", "watch_id, status"),
    ("ix_post_rules_hash", "monitor_post", "cache_rules_hash"),
    ("ix_post_watch_kind", "monitor_post", "watch_id, kind"),
]


def run():
    """Apply every pending migration. Safe to call on each startup."""
    applied = []
    inspector = inspect(db.engine)
    existing_tables = set(inspector.get_table_names())

    with db.engine.begin() as conn:
        for table, columns in COLUMNS.items():
            if table not in existing_tables:
                continue  # create_all() will have built it with every column
            have = {c["name"] for c in inspector.get_columns(table)}
            for name, ddl in columns:
                if name in have:
                    continue
                conn.execute(text("ALTER TABLE %s ADD COLUMN %s %s"
                                  % (table, name, ddl)))
                applied.append("%s.%s" % (table, name))

        for index, table, cols in INDEXES:
            if table not in existing_tables:
                continue
            try:
                conn.execute(text("CREATE INDEX IF NOT EXISTS %s ON %s (%s)"
                                  % (index, table, cols)))
            except Exception:
                # A backend without IF NOT EXISTS support, or an index that is
                # already there under a different name. Neither is fatal.
                pass

    return applied


def backfill_posted_ts():
    """Populate posted_ts for rows written before the column existed.

    Without this, older posts sort as if they had no date at all.
    """
    from .models import MonitorPost
    from datetime import datetime

    rows = MonitorPost.query.filter(MonitorPost.posted_ts.is_(None)).all()
    if not rows:
        return 0
    for p in rows:
        stamp = p.posted_at or ""
        try:
            p.posted_ts = datetime.fromisoformat(str(stamp)[:19])
        except (ValueError, TypeError):
            p.posted_ts = p.collected_at
    db.session.commit()
    return len(rows)
