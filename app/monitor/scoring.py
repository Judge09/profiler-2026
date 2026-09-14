"""Cached scoring.

The dashboard and the watch list used to re-run the full regex engine over
every post of every watch on every page load. With a few thousand posts that
is seconds of CPU per request, for an answer that almost never changes.

The fix is to score once and remember it. Each post carries the verdict it was
given plus `cache_rules_hash`, a fingerprint of the watch settings that
produced it. When the fingerprint still matches, the stored verdict is used;
when the analyst edits keywords, weights or thresholds the fingerprint changes
and exactly the affected posts are rescored.

This keeps the expensive path (`engine.analyze`) but stops paying for it
repeatedly, and it lets counts, filters, sorting and pagination run as plain
SQL against indexed columns.
"""

import json
from datetime import datetime

from ..extensions import db
from ..models import MonitorPost, Profile
from . import engine
from .keywords import rules_hash


def build_config(watch, profiles=None):
    """Engine config for a watch, loading profiles only when Hunter mode needs them."""
    if profiles is None:
        profiles = Profile.query.all() if watch.mode_hunter else []
    return engine.build_config(watch, profiles)


def _store(post, analysis, digest):
    """Write one analysis onto the post row."""
    post.cached_score = int(analysis["score"])
    post.cached_verdict = analysis["verdict"]
    post.cached_types = ",".join(analysis.get("types") or [])
    post.cached_relevant = bool(analysis.get("relevant"))
    post.cached_json = json.dumps(analysis, default=str)
    post.cache_rules_hash = digest

    # Denormalise the timestamp so ORDER BY works in SQL. `posted_at` is free
    # text from whatever the source gave us, which cannot be sorted reliably.
    if post.posted_ts is None:
        stamp = post.posted_at or ""
        try:
            post.posted_ts = datetime.fromisoformat(str(stamp)[:19])
        except (ValueError, TypeError):
            post.posted_ts = post.collected_at


def _apply_override(post, analysis, cfg):
    """Layer an analyst's manual score or verdict over the computed one.

    The evidence is kept either way -- an override changes the call, not the
    reasoning that was on display when it was made.
    """
    if post.manual_score is not None:
        analysis["auto_score"] = analysis["score"]
        analysis["auto_verdict"] = analysis["verdict"]
        analysis["score"] = int(post.manual_score)
        analysis["verdict"] = (post.manual_verdict
                               or ("bad" if analysis["score"] >= cfg["threshold_high"]
                                   else "warn" if analysis["score"] >= cfg["threshold_review"]
                                   else "ok"))
        analysis["verdict_label"] = engine.VERDICT_LABELS.get(
            analysis["verdict"], analysis["verdict"])
        analysis["overridden"] = True
    elif post.manual_verdict:
        analysis["auto_verdict"] = analysis["verdict"]
        analysis["verdict"] = post.manual_verdict
        analysis["verdict_label"] = engine.VERDICT_LABELS.get(
            post.manual_verdict, post.manual_verdict)
        analysis["overridden"] = True
    else:
        analysis["overridden"] = False
    return analysis


def analyze_post(post, cfg, digest, force=False):
    """Return the analysis for one post, computing it only when stale."""
    if not force and post.cache_rules_hash == digest and post.cached_json:
        try:
            return _apply_override(post, json.loads(post.cached_json), cfg)
        except (ValueError, TypeError):
            pass  # corrupt cache entry: fall through and recompute
    analysis = engine.analyze(post, cfg)
    _store(post, analysis, digest)
    return _apply_override(post, analysis, cfg)


def refresh_watch(watch, force=False, commit=True):
    """Ensure every post in a watch has a current cached score.

    Returns (config, {post_id: analysis}). Only posts whose fingerprint is
    stale are recomputed, so a page load after no changes does no scoring work
    at all.
    """
    cfg = build_config(watch)
    digest = rules_hash(watch, cfg.get("spec"))
    results = {}
    dirty = 0

    for post in watch.posts:
        stale = force or post.cache_rules_hash != digest or not post.cached_json
        if stale:
            dirty += 1
        results[post.id] = analyze_post(post, cfg, digest, force=force)

    if dirty and commit:
        db.session.commit()
    return cfg, results


def ensure_fresh(watch, commit=True):
    """Rescore only what is stale, without building the full results dict.

    Used by the dashboard and the watch list, which then read the cached
    columns with SQL rather than holding every analysis in memory.
    """
    digest = rules_hash(watch)
    stale = [p for p in watch.posts
             if p.cache_rules_hash != digest or not p.cached_json]
    if not stale:
        return digest, 0

    cfg = build_config(watch)
    for post in stale:
        analysis = engine.analyze(post, cfg)
        _store(post, analysis, digest)
    if commit:
        db.session.commit()
    return digest, len(stale)


def counts_for(watch, ensure=True):
    """Verdict counts for a watch, honouring analyst overrides.

    Runs as a grouped SQL query over the cached columns instead of scoring in
    Python. The override columns are folded in with a CASE so a manually set
    verdict counts where the analyst put it.
    """
    if ensure:
        ensure_fresh(watch)

    rows = db.session.query(
        db.func.coalesce(MonitorPost.manual_verdict, MonitorPost.cached_verdict),
        db.func.count(MonitorPost.id),
    ).filter(MonitorPost.watch_id == watch.id).group_by(
        db.func.coalesce(MonitorPost.manual_verdict, MonitorPost.cached_verdict)
    ).all()

    counts = {"bad": 0, "warn": 0, "ok": 0}
    for verdict, n in rows:
        if verdict in counts:
            counts[verdict] += n
    return counts


def invalidate(watch_id=None):
    """Mark cached scores stale, forcing a recompute on next read.

    Called when something outside the watch's own settings changes the answer
    -- a new Profile in Hunter mode, for instance.
    """
    q = MonitorPost.query
    if watch_id is not None:
        q = q.filter(MonitorPost.watch_id == watch_id)
    q.update({MonitorPost.cache_rules_hash: None}, synchronize_session=False)
    db.session.commit()


def analysis_of(post):
    """Read a post's cached analysis without a watch context.

    Returns None when nothing is cached, so callers can decide whether to
    score on demand.
    """
    if not post.cached_json:
        return None
    try:
        return json.loads(post.cached_json)
    except (ValueError, TypeError):
        return None
