"""Signal Monitor routes.

A watch is a saved monitoring case. Posts are collected into it (live or by
hand), scored by the engine, and optionally correlated back to Profiles.

Scoring is cached on the post row (see `scoring.py`), so list, dashboard and
pagination queries read indexed columns instead of re-running the engine.
"""

import csv
import hashlib
import io
import json
import os
import re
from datetime import datetime, timedelta

import requests
from flask import (Blueprint, Response, current_app, jsonify, redirect,
                   render_template, request, url_for)

from ..auth.routes import login_required
from ..extensions import db
from ..models import (Graph, IntelNote, MonitorCredential, MonitorFeed,
                      MonitorPost, MonitorWatch, Profile)
from . import (authfetch, capabilities, collectors, engine, graphbuild,
               netintel, scoring, vault)
from .keywords import (alias_hint, rules_hash, spec_for,
                       to_legacy_string)
from .keywords import rules_hash as keywords_rules_hash

monitor = Blueprint("monitor", __name__, url_prefix="/monitor")

STATUSES = ("new", "reviewed", "escalated", "dismissed")
SORTS = ("risk", "recent", "oldest", "platform", "status", "relevance")


# -- Helpers -----------------------------------------------------------------

def _dedupe_key(watch_id, platform, author, text, url=""):
    raw = "|".join([str(watch_id), (platform or "").lower(), (author or "").lower(),
                    (url or "").lower(), (text or "").strip().lower()[:400]])
    return hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()


def _watch_config(watch):
    """Build the engine config, loading profiles only when Hunter mode is on."""
    return scoring.build_config(watch)


def _analyze_watch(watch, force=False):
    """Cached scoring for a whole watch. Kept for callers that need every result."""
    return scoring.refresh_watch(watch, force=force)


def _parse_since(value, default_days=None):
    """Turn a `days` or ISO date parameter into a naive UTC datetime."""
    if value in (None, "", "all"):
        return (datetime.utcnow() - timedelta(days=default_days)
                if default_days else None)
    try:
        return datetime.utcnow() - timedelta(days=max(1, min(3650, int(value))))
    except (TypeError, ValueError):
        pass
    try:
        return datetime.fromisoformat(str(value)[:19])
    except ValueError:
        return None


def _add_posts(watch, raw_posts, source="manual", drop_off_topic=False,
               enrich=False):
    """Insert posts, skipping duplicates.

    Returns a dict of counters. With `drop_off_topic`, posts that fail the
    watch's relevance test are rejected rather than stored -- collectors return
    whatever the search engine matched, which is broader than the topic.
    """
    existing = {p.dedupe_key for p in watch.posts}
    cfg = _watch_config(watch) if (drop_off_topic or enrich) else None
    digest = rules_hash(watch, cfg.get("spec") if cfg else None)
    stats = {"added": 0, "skipped": 0, "off_topic": 0, "flagged": 0}
    fresh = []

    for raw in raw_posts:
        author = str(raw.get("author") or "").strip()
        text = str(raw.get("text") or "").strip()
        if not author or not text:
            stats["skipped"] += 1
            continue

        platform = str(raw.get("platform") or "Other").strip()
        url = str(raw.get("url") or "").strip()
        analysis = None

        if cfg is not None:
            probe = dict(raw, author=author, text=text, platform=platform)
            analysis = engine.analyze(probe, cfg)
            if drop_off_topic and not analysis["relevant"]:
                stats["off_topic"] += 1
                continue

        key = _dedupe_key(watch.id, platform, author, text, url)
        if key in existing:
            stats["skipped"] += 1
            continue
        existing.add(key)

        stamp = raw.get("posted_at")
        try:
            posted_ts = datetime.fromisoformat(str(stamp)[:19]) if stamp else None
        except (ValueError, TypeError):
            posted_ts = None

        post = MonitorPost(
            watch_id=watch.id,
            platform=platform,
            author=author[:200],
            handle=str(raw.get("handle") or "").lstrip("@")[:120],
            verified=bool(raw.get("verified")),
            text=text,
            url=url,
            source=raw.get("source") or source,
            source_url=str(raw.get("source_url") or "")[:500],
            link_kind=raw.get("link_kind") or "direct",
            posted_at=stamp,
            posted_ts=posted_ts or datetime.utcnow(),
            dedupe_key=key,
        )
        # Score on the way in, so the post is never stored unscored.
        if analysis is not None:
            scoring._store(post, analysis, digest)
            if analysis["verdict"] != "ok":
                stats["flagged"] += 1
        db.session.add(post)
        fresh.append(post)
        stats["added"] += 1

    if stats["added"]:
        watch.updated_at = datetime.utcnow()
    db.session.commit()
    return stats


# -- Keyword handling --------------------------------------------------------

def _keyword_payload(watch, overrides=None):
    """The structured keyword view of a watch, for the UI."""
    src = dict(watch.to_dict())
    if overrides:
        src.update({k: v for k, v in overrides.items() if v is not None})
    spec = spec_for(src)
    d = spec.describe()
    return {
        "required": d["required"],
        "optional": d["optional"],
        "excluded": d["excluded"],
        "match_mode": d["match_mode"],
        "subject": d["subject"],
        "demoted": d["demoted"],
        "generic_required": d["generic_required"],
        "query": spec.build_query(),
        "has_anchor": spec.has_anchor,
        "warning": _keyword_warning(spec),
        # Search engines expand acronyms; the filter cannot guess the long
        # form, so it asks rather than silently dropping the results.
        "alias_hints": alias_hint([t.raw for t in spec.required]),
    }


def _keyword_warning(spec):
    if not spec.has_anchor:
        return ("Nothing anchors this watch to a topic, so collection will pull "
                "anything matching the broad terms. Add at least one specific "
                "required keyword.")
    if spec.demoted:
        return ("These terms are too generic to anchor a topic on their own and "
                "were moved to optional: " + ", ".join(spec.demoted))
    if spec.match_mode == "all" and len(spec.required) > 4:
        return ("%d required terms in ALL mode means a post must contain every "
                "one of them, which will match very little."
                % len(spec.required))
    return ""


def _query_for(watch):
    """The search string for live collection, derived from the keyword spec."""
    spec = spec_for(watch)
    q = spec.build_query()
    return q or (watch.subject or watch.name or "").strip()


# -- Watch CRUD --------------------------------------------------------------

@monitor.route("/")
@login_required
def list_watches():
    """All watches with their verdict spread.

    Counts come from the cached score columns via SQL, so this page costs one
    query per watch rather than a full rescore of every post in the system.
    """
    watches = MonitorWatch.query.order_by(MonitorWatch.updated_at.desc()).all()
    summary = []
    for w in watches:
        counts = scoring.counts_for(w)
        total = sum(counts.values())
        pending = MonitorPost.query.filter(
            MonitorPost.watch_id == w.id,
            MonitorPost.status == "new",
            MonitorPost.cached_verdict.in_(("bad", "warn")),
        ).count()
        summary.append({"watch": w, "counts": counts, "total": total,
                        "pending": pending})
    profiles = Profile.query.order_by(Profile.codename).all()
    return render_template("monitor/list.html", summary=summary,
                           profiles=profiles)


@monitor.route("/dashboard")
@login_required
def dashboard_page():
    return render_template("monitor/dashboard.html",
                           profiles=Profile.query.order_by(Profile.codename).all())


@monitor.route("/dashboard/data")
@login_required
def dashboard_data():
    """Operational picture across every watch.

    Built for the question "what should I look at, and what changed?" rather
    than for browsing: activity by day, risk trend, the untriaged queue, which
    collectors are healthy, and where set-up is incomplete.

    Every figure here is aggregated in SQL over the cached score columns. The
    only scoring done is for posts whose rules changed since they were last
    seen, so a dashboard load on unchanged data does no regex work at all.
    """
    days = max(1, min(365, int(request.args.get("days", 30) or 30)))
    cutoff = datetime.utcnow() - timedelta(days=days)

    watches = MonitorWatch.query.order_by(MonitorWatch.updated_at.desc()).all()
    rescored = 0
    for w in watches:
        _, n = scoring.ensure_fresh(w, commit=False)
        rescored += n
    if rescored:
        db.session.commit()

    watch_names = {w.id: w.name for w in watches}
    verdict_col = db.func.coalesce(MonitorPost.manual_verdict,
                                   MonitorPost.cached_verdict)
    score_col = db.func.coalesce(MonitorPost.manual_score,
                                 MonitorPost.cached_score)

    # -- totals ----------------------------------------------------------
    totals = {"bad": 0, "warn": 0, "ok": 0}
    for verdict, n in db.session.query(verdict_col, db.func.count(
            MonitorPost.id)).group_by(verdict_col).all():
        if verdict in totals:
            totals[verdict] = n

    pending = MonitorPost.query.filter(
        MonitorPost.status == "new", verdict_col.in_(("bad", "warn"))).count()
    escalated = MonitorPost.query.filter_by(status="escalated").count()

    # -- per-watch rows --------------------------------------------------
    per_watch = {}
    for wid, verdict, n in db.session.query(
            MonitorPost.watch_id, verdict_col,
            db.func.count(MonitorPost.id)).group_by(
            MonitorPost.watch_id, verdict_col).all():
        row = per_watch.setdefault(wid, {"bad": 0, "warn": 0, "ok": 0})
        if verdict in row:
            row[verdict] = n

    pending_by_watch = dict(db.session.query(
        MonitorPost.watch_id, db.func.count(MonitorPost.id)).filter(
        MonitorPost.status == "new", verdict_col.in_(("bad", "warn"))).group_by(
        MonitorPost.watch_id).all())

    newest_by_watch = dict(db.session.query(
        MonitorPost.watch_id, db.func.max(MonitorPost.posted_ts)).group_by(
        MonitorPost.watch_id).all())

    watch_rows, stale = [], []
    for w in watches:
        counts = per_watch.get(w.id, {"bad": 0, "warn": 0, "ok": 0})
        newest = newest_by_watch.get(w.id)
        watch_rows.append({
            "id": w.id, "name": w.name, "subject": w.subject,
            "mode_label": w.mode_label,
            "mode_release": w.mode_release, "mode_hunter": w.mode_hunter,
            "total": sum(counts.values()), "counts": counts,
            "pending": pending_by_watch.get(w.id, 0),
            "newest": newest.isoformat() if newest else "",
            "updated_at": w.updated_at.strftime("%Y-%m-%d %H:%M") if w.updated_at else "",
        })
        # A watch nobody has fed in a while is probably going stale.
        if not sum(counts.values()):
            stale.append({"id": w.id, "name": w.name, "newest": "",
                          "why": "No posts collected yet"})
        elif newest and newest < datetime.utcnow() - timedelta(days=7):
            stale.append({"id": w.id, "name": w.name,
                          "newest": newest.strftime("%Y-%m-%d"),
                          "why": "Nothing new in over a week"})

    # -- activity by day -------------------------------------------------
    day = db.func.substr(db.func.coalesce(
        db.func.strftime("%Y-%m-%d", MonitorPost.posted_ts), ""), 1, 10)
    timeline_rows = db.session.query(
        day, verdict_col, db.func.count(MonitorPost.id)).filter(
        MonitorPost.posted_ts >= cutoff).group_by(day, verdict_col).all()
    by_day = {}
    for d, verdict, n in timeline_rows:
        if not d:
            continue
        e = by_day.setdefault(d, {"date": d, "total": 0, "bad": 0, "warn": 0, "ok": 0})
        e["total"] += n
        if verdict in e:
            e[verdict] += n
    timeline = [by_day[k] for k in sorted(by_day)]

    # -- platforms and threat types --------------------------------------
    platforms = []
    plat_rows = db.session.query(
        MonitorPost.platform, db.func.count(MonitorPost.id),
        db.func.sum(db.case((verdict_col != "ok", 1), else_=0)),
        db.func.avg(score_col)).group_by(MonitorPost.platform).all()
    for plat, total, flagged, avg in plat_rows:
        platforms.append({"platform": plat or "Other", "total": total,
                          "flagged": int(flagged or 0),
                          "avg": round(float(avg or 0))})
    platforms.sort(key=lambda p: -p["total"])

    type_counts = {}
    for (types,) in db.session.query(MonitorPost.cached_types).filter(
            MonitorPost.cached_types != "").all():
        for t in (types or "").split(","):
            t = t.strip()
            if t:
                type_counts[t] = type_counts.get(t, 0) + 1

    # -- flagged feed ----------------------------------------------------
    flagged_rows = MonitorPost.query.filter(
        verdict_col.in_(("bad", "warn"))).order_by(
        MonitorPost.posted_ts.desc().nullslast()).limit(40).all()

    def row_of(p):
        return {
            "post_id": p.id, "watch_id": p.watch_id,
            "watch": watch_names.get(p.watch_id, ""),
            "author": p.author, "platform": p.platform,
            "score": p.manual_score if p.manual_score is not None else (p.cached_score or 0),
            "verdict": p.manual_verdict or p.cached_verdict or "ok",
            "verdict_label": engine.VERDICT_LABELS.get(
                p.manual_verdict or p.cached_verdict or "ok", ""),
            "types": [t for t in (p.cached_types or "").split(",") if t],
            "status": p.status, "url": p.url,
            "link_kind": p.link_kind or "direct",
            "when": p.posted_ts.isoformat() if p.posted_ts else "",
            "excerpt": (p.text or "")[:180],
        }

    recent = [row_of(p) for p in flagged_rows]

    queue_rows = MonitorPost.query.filter(
        verdict_col.in_(("bad", "warn")),
        MonitorPost.status == "new").order_by(score_col.desc()).limit(10).all()
    queue = [row_of(p) for p in queue_rows]

    # -- source health ---------------------------------------------------
    feeds = MonitorFeed.query.order_by(MonitorFeed.last_run.desc()).limit(14).all()
    sources = [{
        "source": f.source, "watch_id": f.watch_id,
        "watch": watch_names.get(f.watch_id, ""),
        "enabled": f.enabled,
        "last_run": f.last_run.strftime("%Y-%m-%d %H:%M") if f.last_run else "",
        "last_note": f.last_note or "",
        # "Fetched 0" ran without erroring but returned nothing, which is a
        # problem worth surfacing rather than a green tick.
        "ok": ("Fetched" in (f.last_note or "")
               and not re.search(r"\b(Fetched|Extracted) 0\b", f.last_note or "")),
    } for f in feeds]

    creds = MonitorCredential.query.all()
    cred_summary = {
        "total": len(creds),
        "enabled": sum(1 for c in creds if c.enabled),
        "failing": sum(1 for c in creds if c.last_ok is False),
        "unlocked": vault.is_unlocked(),
    }

    caps = capabilities.report()

    return jsonify({
        "generated_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M"),
        "days": days,
        "watch_count": len(watches),
        "totals": totals,
        "post_total": sum(totals.values()),
        "pending": pending,
        "escalated": escalated,
        "rescored": rescored,
        "timeline": timeline,
        "watches": watch_rows,
        "platforms": platforms,
        "types": sorted(({"type": k, "count": v} for k, v in type_counts.items()),
                        key=lambda x: -x["count"])[:10],
        "recent": recent[:12],
        "queue": queue,
        "sources": sources,
        "credentials": cred_summary,
        "stale": stale,
        "profiles": Profile.query.count(),
        "capabilities": {
            "available": caps["counts"]["available"],
            "total": caps["counts"]["total"],
            "serverless": caps["serverless"],
            "missing": caps["missing"][:8],
        },
        "storage": {
            "ephemeral": bool(current_app.config.get("DB_IS_EPHEMERAL")),
            "backend": db.engine.url.get_backend_name(),
        },
    })


@monitor.route("/dashboard/geo")
@login_required
def dashboard_geo():
    """Where the infrastructure behind flagged posts actually lives.

    Resolves the hosts linked from flagged posts and groups them by country.
    Lookups are cached, so repeated loads cost nothing; the first run over a
    large watch is the slow one, which is why it is a separate endpoint the
    page fetches after the main dashboard has already painted.
    """
    limit = max(1, min(200, int(request.args.get("limit", 60) or 60)))
    watch_id = request.args.get("watch_id")

    verdict_col = db.func.coalesce(MonitorPost.manual_verdict,
                                   MonitorPost.cached_verdict)
    q = MonitorPost.query.filter(verdict_col.in_(("bad", "warn")))
    if watch_id:
        try:
            q = q.filter(MonitorPost.watch_id == int(watch_id))
        except ValueError:
            pass
    posts = q.order_by(MonitorPost.posted_ts.desc().nullslast()).limit(limit).all()

    countries = {}
    hosts = {}
    flags = []
    checked = 0

    for p in posts:
        ind = netintel.extract_indicators(p.text or "", p.url or "")
        for u in ind["urls"][:4]:
            host = u["host"]
            if not host or host in hosts:
                continue
            checked += 1
            geo = netintel.geolocate(host)
            entry = {
                "host": host, "domain": u["domain"], "ok": bool(geo.get("ok")),
                "country": geo.get("country") or "", "city": geo.get("city") or "",
                "country_code": geo.get("country_code") or "",
                "lat": geo.get("lat"), "lon": geo.get("lon"),
                "isp": geo.get("isp") or "", "asn": geo.get("asn") or "",
                "hosting": bool(geo.get("hosting")), "proxy": bool(geo.get("proxy")),
                "note": geo.get("note") or "",
                "post_id": p.id, "watch_id": p.watch_id,
            }
            hosts[host] = entry
            if geo.get("ok") and geo.get("country"):
                c = countries.setdefault(geo["country"], {
                    "country": geo["country"],
                    "country_code": geo.get("country_code") or "",
                    "hosts": 0, "hosting": 0, "proxy": 0,
                    "lat": geo.get("lat"), "lon": geo.get("lon")})
                c["hosts"] += 1
                if geo.get("hosting"):
                    c["hosting"] += 1
                if geo.get("proxy"):
                    c["proxy"] += 1

        for ip in ind["ips"][:3]:
            if not ip["routable"]:
                flags.append({
                    "kind": "non-routable", "value": ip["ip"], "post_id": p.id,
                    "detail": "%s address, unreachable from the public internet"
                              % ip["kind"]})

    for h in hosts.values():
        if h["hosting"]:
            flags.append({"kind": "datacenter", "value": h["host"],
                          "post_id": h["post_id"],
                          "detail": "Hosted by %s, not a consumer connection"
                                    % (h["isp"] or "a datacenter")})
        if h["proxy"]:
            flags.append({"kind": "proxy", "value": h["host"],
                          "post_id": h["post_id"],
                          "detail": "Resolves through a proxy or VPN"})

    return jsonify({
        "generated_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M"),
        "posts_examined": len(posts),
        "hosts_checked": checked,
        "countries": sorted(countries.values(), key=lambda c: -c["hosts"]),
        "hosts": sorted(hosts.values(),
                        key=lambda h: (not h["hosting"], h["country"] or "zz")),
        "flags": flags[:30],
        "note": ("Locations are where the server answering for a domain sits. "
                 "A CDN or cloud host says where the infrastructure is, not "
                 "where the author is."),
    })


@monitor.route("/dashboard/phishing")
@login_required
def dashboard_phishing():
    """Every risky link across flagged posts, checked against open feeds."""
    limit = max(1, min(200, int(request.args.get("limit", 60) or 60)))
    watch_id = request.args.get("watch_id")
    use_feeds = (request.args.get("feeds") or "1").lower() not in ("0", "false")

    verdict_col = db.func.coalesce(MonitorPost.manual_verdict,
                                   MonitorPost.cached_verdict)
    q = MonitorPost.query.filter(verdict_col.in_(("bad", "warn")))
    if watch_id:
        try:
            q = q.filter(MonitorPost.watch_id == int(watch_id))
        except ValueError:
            pass
    posts = q.order_by(MonitorPost.posted_ts.desc().nullslast()).limit(limit).all()

    findings, seen = [], set()
    for p in posts:
        ind = netintel.extract_indicators(p.text or "", p.url or "")
        for u in ind["urls"][:5]:
            if u["url"].lower() in seen:
                continue
            seen.add(u["url"].lower())
            check = netintel.check_phishing(u["url"], use_feeds=use_feeds)
            if check["verdict"] == "clean":
                continue
            findings.append(dict(
                check, post_id=p.id, watch_id=p.watch_id,
                author=p.author, platform=p.platform))

    findings.sort(key=lambda f: -f["score"])
    by_verdict = {"malicious": 0, "suspicious": 0}
    for f in findings:
        by_verdict[f["verdict"]] = by_verdict.get(f["verdict"], 0) + 1

    return jsonify({
        "generated_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M"),
        "posts_examined": len(posts),
        "urls_checked": len(seen),
        "counts": by_verdict,
        "findings": findings[:40],
        "feeds": netintel.feed_status(),
    })


@monitor.route("/capabilities")
@login_required
def capabilities_report():
    """What this deployment can and cannot do, and why."""
    return jsonify(capabilities.report())


@monitor.route("/new", methods=["POST"])
@login_required
def create_watch():
    data = request.json or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Name required"}), 400
    w = MonitorWatch(
        name=name,
        subject=(data.get("subject") or "").strip(),
        keywords=(data.get("keywords") or "").strip(),
        kw_required=(data.get("kw_required") or "").strip(),
        kw_optional=(data.get("kw_optional") or "").strip(),
        kw_excluded=(data.get("kw_excluded") or "").strip(),
        kw_match_mode=("any" if (data.get("kw_match_mode") or "all") == "any"
                       else "all"),
        mode_release=bool(data.get("mode_release")),
        mode_hunter=bool(data.get("mode_hunter")),
        profile_id=int(data["profile_id"]) if data.get("profile_id") else None,
    )
    # Keep the legacy comma field in step with the structured one.
    if w.kw_required or w.kw_optional:
        w.keywords = to_legacy_string(spec_for(w))
    if w.mode_release and not w.reference_text:
        w.reference_text = (data.get("reference_text") or "").strip()
    db.session.add(w)
    db.session.commit()
    return jsonify(w.to_dict()), 201


@monitor.route("/watch")
@login_required
def watch_page():
    """The watch workspace, rendered as a shell.

    The watch itself lives in IndexedDB, so the server has nothing to look up
    here -- it serves the chrome and the client fills it from the store. The
    id travels as a query parameter because it is a browser-side id, not a
    database key.
    """
    return render_template(
        "monitor/watch.html",
        profiles=[],
        sources=collectors.source_meta(),
        default_weights=engine.DEFAULT_WEIGHTS,
        weights=engine.DEFAULT_WEIGHTS,
        statuses=STATUSES,
        page_size=current_app.config.get("PAGE_SIZE", 25),
        suggested=collectors.load_sources().get("suggested_feeds", []),
    )


@monitor.route("/<int:watch_id>")
@login_required
def watch_detail(watch_id):
    """Legacy watch URL, kept so old links and bookmarks still work.

    Watches live in the browser now and are addressed by a query parameter, so
    `/monitor/1` cannot identify one. It used to render the workspace anyway,
    with the id resolving to 0 -- every control drawn but bound to nothing,
    which looks like a broken page rather than a stale URL. Redirecting says
    what actually happened.
    """
    return redirect(url_for("monitor.watch_page", id=watch_id), code=301)


@monitor.route("/<int:watch_id>/update", methods=["POST"])
@login_required
def update_watch(watch_id):
    w = MonitorWatch.query.get_or_404(watch_id)
    data = request.json or {}
    for field in ("name", "subject", "keywords", "reference_text",
                  "official_accounts", "official_domains", "custom_flags",
                  "kw_required", "kw_optional", "kw_excluded"):
        if field in data:
            setattr(w, field, (data.get(field) or "").strip())
    if "kw_match_mode" in data:
        w.kw_match_mode = "any" if data.get("kw_match_mode") == "any" else "all"
    # The structured fields are authoritative once set; mirror them back into
    # the legacy comma column so exports and older callers still read sensibly.
    if any(k in data for k in ("kw_required", "kw_optional", "kw_excluded",
                               "kw_match_mode")):
        w.keywords = to_legacy_string(spec_for(w))
    if "mode_release" in data:
        w.mode_release = bool(data["mode_release"])
    if "mode_hunter" in data:
        w.mode_hunter = bool(data["mode_hunter"])
    if "profile_id" in data:
        w.profile_id = int(data["profile_id"]) if data["profile_id"] else None
    for field in ("threshold_review", "threshold_high"):
        if field in data:
            try:
                setattr(w, field, max(0, min(100, int(data[field]))))
            except (TypeError, ValueError):
                pass
    if "weights" in data and isinstance(data["weights"], dict):
        clean = {}
        for k, v in data["weights"].items():
            if k in engine.DEFAULT_WEIGHTS:
                try:
                    clean[k] = int(float(v))
                except (TypeError, ValueError):
                    continue
        w.weights = clean
    w.updated_at = datetime.utcnow()
    db.session.commit()
    return jsonify(w.to_dict())


@monitor.route("/<int:watch_id>/delete", methods=["POST"])
@login_required
def delete_watch(watch_id):
    w = MonitorWatch.query.get_or_404(watch_id)
    MonitorFeed.query.filter_by(watch_id=w.id).delete()
    db.session.delete(w)
    db.session.commit()
    return jsonify({"ok": True})


# -- Scoring -----------------------------------------------------------------

@monitor.route("/<int:watch_id>/results")
@login_required
def results(watch_id):
    """One page of scored posts.

    Filtering, searching and sorting all happen in SQL against the cached
    score columns, so the cost is bounded by the page size rather than by how
    many posts the watch holds.

    Query parameters: page, per_page, verdict, status, platform, q, sort,
    types, days, pinned, profile.
    """
    w = MonitorWatch.query.get_or_404(watch_id)
    scoring.ensure_fresh(w)

    page = max(1, int(request.args.get("page", 1) or 1))
    default_size = current_app.config.get("PAGE_SIZE", 25)
    per_page = max(1, min(200, int(request.args.get("per_page", default_size) or default_size)))

    # The effective verdict honours an analyst override.
    verdict_col = db.func.coalesce(MonitorPost.manual_verdict,
                                   MonitorPost.cached_verdict)
    score_col = db.func.coalesce(MonitorPost.manual_score,
                                 MonitorPost.cached_score)

    q = MonitorPost.query.filter(MonitorPost.watch_id == w.id)

    verdict = (request.args.get("verdict") or "all").strip()
    if verdict in ("bad", "warn", "ok"):
        q = q.filter(verdict_col == verdict)

    status = (request.args.get("status") or "").strip()
    if status in STATUSES:
        q = q.filter(MonitorPost.status == status)

    platform = (request.args.get("platform") or "").strip()
    if platform:
        q = q.filter(MonitorPost.platform == platform)

    if (request.args.get("pinned") or "").lower() in ("1", "true"):
        q = q.filter(MonitorPost.pinned.is_(True))

    profile_id = request.args.get("profile")
    if profile_id:
        try:
            q = q.filter(MonitorPost.profile_id == int(profile_id))
        except ValueError:
            pass

    threat = (request.args.get("types") or "").strip()
    if threat:
        q = q.filter(MonitorPost.cached_types.ilike("%" + threat + "%"))

    since = _parse_since(request.args.get("days"))
    if since:
        q = q.filter(MonitorPost.posted_ts >= since)

    term = (request.args.get("q") or "").strip()
    if term:
        like = "%" + term + "%"
        q = q.filter(db.or_(MonitorPost.text.ilike(like),
                            MonitorPost.author.ilike(like),
                            MonitorPost.handle.ilike(like),
                            MonitorPost.platform.ilike(like),
                            MonitorPost.cached_types.ilike(like)))

    sort = (request.args.get("sort") or "risk").strip()
    if sort not in SORTS:
        sort = "risk"
    order = {
        "risk": (score_col.desc(),),
        "recent": (MonitorPost.posted_ts.desc().nullslast(),),
        "oldest": (MonitorPost.posted_ts.asc().nullsfirst(),),
        "platform": (MonitorPost.platform.asc(), score_col.desc()),
        "status": (MonitorPost.status.asc(), score_col.desc()),
        "relevance": (MonitorPost.cached_relevant.desc(), score_col.desc()),
    }[sort]
    # Pinned posts stay on top wherever the analyst is looking.
    q = q.order_by(MonitorPost.pinned.desc(), *order, MonitorPost.id.desc())

    total_matching = q.count()
    rows = q.limit(per_page).offset((page - 1) * per_page).all()

    cfg = _watch_config(w)
    digest = rules_hash(w, cfg.get("spec"))
    posts = []
    for p in rows:
        analysis = scoring.analyze_post(p, cfg, digest)
        posts.append(dict(p.to_dict(), analysis=analysis))
    db.session.commit()

    counts = scoring.counts_for(w, ensure=False)
    pages = max(1, -(-total_matching // per_page))
    return jsonify({
        "counts": counts,
        "total": sum(counts.values()),
        "matching": total_matching,
        "page": page,
        "pages": pages,
        "per_page": per_page,
        "has_prev": page > 1,
        "has_next": page < pages,
        "posts": posts,
        "platforms": _platform_options(w.id),
        "sort": sort,
        "verdict": verdict,
    })


def _platform_options(watch_id):
    """Distinct platforms present in a watch, for the filter dropdown."""
    rows = db.session.query(MonitorPost.platform,
                            db.func.count(MonitorPost.id)).filter(
        MonitorPost.watch_id == watch_id).group_by(
        MonitorPost.platform).order_by(db.func.count(MonitorPost.id).desc()).all()
    return [{"platform": p or "Other", "count": n} for p, n in rows]


@monitor.route("/<int:watch_id>/briefing")
@login_required
def briefing(watch_id):
    """A situational summary of the watch, so the picture is clear up front.

    Answers the questions an analyst asks before reading any single post: what
    is driving risk right now, which narratives are spreading, who is pushing
    them, what is new since last time, and what needs a decision.
    """
    w = MonitorWatch.query.get_or_404(watch_id)
    cfg, results = _analyze_watch(w)
    posts = list(w.posts)
    total = len(posts)

    counts = {"bad": 0, "warn": 0, "ok": 0}
    status_counts = {s: 0 for s in STATUSES}
    type_counts = {}
    signal_counts = {}
    platform_stats = {}
    author_stats = {}
    domain_stats = {}

    for p in posts:
        a = results[p.id]
        counts[a["verdict"]] += 1
        status_counts[p.status or "new"] = status_counts.get(p.status or "new", 0) + 1

        for t in a["types"]:
            type_counts[t] = type_counts.get(t, 0) + 1
        for s in a["signals"]:
            if s["w"] > 0:
                e = signal_counts.setdefault(s["label"], {"label": s["label"],
                                                          "count": 0, "weight": 0})
                e["count"] += 1
                e["weight"] += s["w"]

        plat = p.platform or "Other"
        ps = platform_stats.setdefault(plat, {"platform": plat, "total": 0,
                                              "bad": 0, "warn": 0, "ok": 0, "score": 0})
        ps["total"] += 1
        ps[a["verdict"]] += 1
        ps["score"] += a["score"]

        # Group by the account, not the feed host -- every article from one
        # aggregator shares a handle, which would merge unrelated outlets.
        key = ((p.author or "") + "|" + (p.platform or "")).lower() or "?"
        aus = author_stats.setdefault(key, {
            "author": p.author, "handle": p.handle, "platform": plat,
            "posts": 0, "flagged": 0, "top_score": 0, "types": set(),
            "post_id": p.id, "url": p.url})
        aus["posts"] += 1
        if a["verdict"] != "ok":
            aus["flagged"] += 1
        if a["score"] > aus["top_score"]:
            aus["top_score"] = a["score"]
            aus["post_id"] = p.id
            aus["url"] = p.url
        aus["types"].update(a["types"])

        for l in a["links"]:
            if l["official"] or not l["host"]:
                continue
            ds = domain_stats.setdefault(l["host"], {
                "host": l["host"], "count": 0, "risky": bool(l["flags"]),
                "reasons": set()})
            ds["count"] += 1
            if l["flags"]:
                ds["risky"] = True
                ds["reasons"].update(f["label"] for f in l["flags"])

    for ps in platform_stats.values():
        ps["avg"] = round(ps["score"] / ps["total"]) if ps["total"] else 0
        del ps["score"]

    # Repeat actors matter more than one-off posters.
    actors = [dict(a, types=sorted(a["types"]))
              for a in author_stats.values() if a["flagged"]]
    actors.sort(key=lambda a: (-a["flagged"], -a["top_score"]))

    domains = [dict(d, reasons=sorted(d["reasons"])) for d in domain_stats.values()]
    domains.sort(key=lambda d: (not d["risky"], -d["count"]))

    drivers = sorted(signal_counts.values(), key=lambda s: -s["weight"])

    ranked = sorted(posts, key=lambda p: -results[p.id]["score"])
    top = [{
        "id": p.id, "author": p.author, "handle": p.handle, "platform": p.platform,
        "score": results[p.id]["score"], "verdict": results[p.id]["verdict"],
        "verdict_label": results[p.id]["verdict_label"],
        "types": results[p.id]["types"], "url": p.url,
        "link_kind": p.link_kind or "direct",
        "status": p.status, "excerpt": (p.text or "")[:220],
        "recommendation": results[p.id]["recommendation"],
    } for p in ranked[:8] if results[p.id]["verdict"] != "ok"]

    # Anything flagged but not yet triaged is what actually needs a decision.
    pending = [p for p in posts
               if results[p.id]["verdict"] != "ok" and (p.status or "new") == "new"]

    coverage = [results[p.id]["coverage"] for p in posts if results[p.id]["relevant"]]
    dates = sorted(p.posted_at for p in posts if p.posted_at)

    gaps = []
    if not w.keywords:
        gaps.append("No keywords set, so relevance cannot be judged and live "
                    "collection has nothing to search for.")
    if w.mode_release and not (w.reference_text or "").strip():
        gaps.append("Media Release Threat mode is on but no reference text is set, "
                    "so fidelity and contradiction rules cannot run.")
    if w.mode_release and not w.official_accounts:
        gaps.append("No official accounts listed, so impersonation cannot be "
                    "distinguished from the real account.")
    if w.mode_release and not w.official_domains:
        gaps.append("No official domains listed, so legitimate links score as "
                    "external.")
    if w.mode_hunter and not Profile.query.count():
        gaps.append("Digital Hunter mode is on but there are no profiles to "
                    "correlate against.")
    if not total:
        gaps.append("No posts collected yet.")

    return jsonify({
        "watch": w.to_dict(),
        "generated_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M"),
        "total": total,
        "counts": counts,
        "status_counts": status_counts,
        "pending_review": len(pending),
        "flagged": counts["bad"] + counts["warn"],
        "types": sorted(({"type": k, "count": v} for k, v in type_counts.items()),
                        key=lambda x: -x["count"]),
        "drivers": drivers[:8],
        "platforms": sorted(platform_stats.values(), key=lambda p: -p["total"]),
        "actors": actors[:8],
        "domains": domains[:8],
        "top": top,
        "avg_coverage": round(sum(coverage) / len(coverage), 2) if coverage else None,
        "date_range": {"first": dates[0], "last": dates[-1]} if dates else None,
        "gaps": gaps,
        "headline": _headline(counts, len(pending), total),
    })


def _headline(counts, pending, total):
    if not total:
        return "Nothing collected yet."
    if counts["bad"]:
        s = "%d post%s need%s action now" % (counts["bad"],
                                             "" if counts["bad"] == 1 else "s",
                                             "s" if counts["bad"] == 1 else "")
    elif counts["warn"]:
        s = "%d post%s need%s a closer look" % (counts["warn"],
                                                "" if counts["warn"] == 1 else "s",
                                                "s" if counts["warn"] == 1 else "")
    else:
        return "Nothing flagged across %d post%s." % (total, "" if total == 1 else "s")
    if pending:
        s += ", %d still untriaged" % pending
    return s + "."


@monitor.route("/preview", methods=["POST"])
@login_required
def preview():
    """Score arbitrary text against a watch without saving it.

    Powers the scoring sandbox: paste a post, tweak weights, see the ledger.
    """
    data = request.json or {}
    watch_id = data.get("watch_id")
    if watch_id:
        w = MonitorWatch.query.get_or_404(int(watch_id))
        overrides = data.get("watch") or {}
        base = w.to_dict()
        base.update({k: v for k, v in overrides.items() if v is not None})
        base["weights"] = overrides.get("weights", w.weights)
        profiles = Profile.query.all() if base.get("mode_hunter") else []
        cfg = engine.build_config(base, profiles)
    else:
        cfg = engine.build_config(data.get("watch") or {},
                                  Profile.query.all()
                                  if (data.get("watch") or {}).get("mode_hunter") else [])
    post = data.get("post") or {}
    return jsonify(engine.analyze(post, cfg))


# -- Post management ---------------------------------------------------------

@monitor.route("/<int:watch_id>/posts", methods=["POST"])
@login_required
def add_post(watch_id):
    w = MonitorWatch.query.get_or_404(watch_id)
    data = request.json or {}
    stats = _add_posts(w, [data], source="manual", enrich=True)
    if not stats["added"]:
        return jsonify({"error": "Needs an author and post text, or it "
                                 "duplicates an existing post"}), 400
    return jsonify(dict(stats, ok=True, counts=scoring.counts_for(w))), 201


@monitor.route("/<int:watch_id>/import", methods=["POST"])
@login_required
def import_posts(watch_id):
    w = MonitorWatch.query.get_or_404(watch_id)
    data = request.json or {}
    payload = data.get("payload")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            return jsonify({"error": "That isn't valid JSON. Check for missing quotes or commas."}), 400
    arr = payload if isinstance(payload, list) else (payload or {}).get("posts")
    if not isinstance(arr, list):
        return jsonify({"error": "Expected an array of posts, or an object with a posts array."}), 400
    if data.get("replace"):
        MonitorPost.query.filter_by(watch_id=w.id).delete()
        db.session.commit()
    stats = _add_posts(w, arr, source="import", enrich=True,
                       drop_off_topic=bool(data.get("strict")))
    return jsonify(dict(stats, ok=True, counts=scoring.counts_for(w)))


@monitor.route("/posts/<int:post_id>", methods=["PATCH"])
@login_required
def update_post(post_id):
    p = MonitorPost.query.get_or_404(post_id)
    data = request.json or {}

    if "manual_score" in data:
        v = data["manual_score"]
        if v in (None, ""):
            p.manual_score = None
        else:
            try:
                p.manual_score = max(0, min(100, int(v)))
            except (TypeError, ValueError):
                return jsonify({"error": "Score must be a number from 0 to 100"}), 400
    if "manual_verdict" in data:
        v = (data["manual_verdict"] or "").strip()
        if v and v not in engine.VERDICT_LABELS:
            return jsonify({"error": "Unknown verdict"}), 400
        p.manual_verdict = v or None
    if "status" in data:
        v = (data["status"] or "new").strip()
        if v not in STATUSES:
            return jsonify({"error": "Unknown status"}), 400
        p.status = v
    if "analyst_note" in data:
        p.analyst_note = (data["analyst_note"] or "").strip()
    if "pinned" in data:
        p.pinned = bool(data["pinned"])
    if "profile_id" in data:
        p.profile_id = int(data["profile_id"]) if data["profile_id"] else None

    db.session.commit()

    w = p.watch
    cfg = _watch_config(w)
    # Re-score rather than reuse the cache: the analyst may have just edited
    # the very fields the verdict depends on.
    a = scoring.analyze_post(p, cfg, rules_hash(w, cfg.get("spec")), force=True)
    db.session.commit()
    return jsonify({"ok": True, "post": p.to_dict(), "analysis": a,
                    "counts": scoring.counts_for(w, ensure=False)})


@monitor.route("/posts/<int:post_id>", methods=["DELETE"])
@login_required
def delete_post(post_id):
    p = MonitorPost.query.get_or_404(post_id)
    db.session.delete(p)
    db.session.commit()
    return jsonify({"ok": True})


@monitor.route("/<int:watch_id>/posts/bulk", methods=["POST"])
@login_required
def bulk_posts(watch_id):
    """Apply one action to many posts at once."""
    w = MonitorWatch.query.get_or_404(watch_id)
    data = request.json or {}
    ids = [int(i) for i in (data.get("ids") or [])]
    action = (data.get("action") or "").strip()
    if not ids:
        return jsonify({"error": "No posts selected"}), 400
    q = MonitorPost.query.filter(MonitorPost.id.in_(ids),
                                 MonitorPost.watch_id == w.id)
    posts = q.all()

    if action == "delete":
        for p in posts:
            db.session.delete(p)
    elif action == "status":
        v = (data.get("value") or "new").strip()
        if v not in STATUSES:
            return jsonify({"error": "Unknown status"}), 400
        for p in posts:
            p.status = v
    elif action == "link_profile":
        pid = int(data["value"]) if data.get("value") else None
        if pid:
            Profile.query.get_or_404(pid)
        for p in posts:
            p.profile_id = pid
    elif action == "clear_override":
        for p in posts:
            p.manual_score = None
            p.manual_verdict = None
    else:
        return jsonify({"error": "Unknown action"}), 400

    db.session.commit()
    return jsonify({"ok": True, "affected": len(posts)})


# -- Live collection ---------------------------------------------------------

@monitor.route("/<int:watch_id>/collect", methods=["POST"])
@login_required
def collect_now(watch_id):
    w = MonitorWatch.query.get_or_404(watch_id)
    data = request.json or {}
    keys = data.get("sources") or []
    if not keys:
        return jsonify({"error": "Pick at least one source"}), 400
    query = (data.get("query") or "").strip() or _query_for(w)
    if not query:
        return jsonify({"error": "Add keywords or a subject to search for"}), 400

    options = data.get("options") or {}
    since = _parse_since(data.get("days"))
    raw_posts, report = collectors.collect(keys, query, options, since=since)
    stats = _add_posts(w, raw_posts, source="live",
                       drop_off_topic=data.get("strict", True), enrich=True)

    # Remember which sources ran, so the watch can be re-collected later.
    for r in report["sources"]:
        if not r["ok"]:
            continue
        feed = MonitorFeed.query.filter_by(watch_id=w.id, source=r["source"]).first()
        if not feed:
            feed = MonitorFeed(watch_id=w.id, source=r["source"])
            db.session.add(feed)
        feed.options = options.get(r["source"], {}) if isinstance(options, dict) else {}
        feed.last_run = datetime.utcnow()
        feed.last_note = r["note"]
    db.session.commit()

    return jsonify(dict(stats, ok=True, query=query, report=report,
                        days=data.get("days"),
                        counts=scoring.counts_for(w)))


@monitor.route("/<int:watch_id>/topic")
@login_required
def topic_scope(watch_id):
    """Show how the current keywords resolve into a topic.

    Lets the analyst see which terms actually scope the watch before running a
    collection, instead of discovering the drift afterwards.
    """
    w = MonitorWatch.query.get_or_404(watch_id)
    # Allow previewing unsaved edits from the sidebar without saving them.
    overrides = {}
    for field in ("kw_required", "kw_optional", "kw_excluded",
                  "kw_match_mode", "subject", "keywords"):
        if request.args.get(field) is not None:
            overrides[field] = request.args.get(field)
    return jsonify(_keyword_payload(w, overrides or None))


@monitor.route("/<int:watch_id>/dorks")
@login_required
def dorks(watch_id):
    w = MonitorWatch.query.get_or_404(watch_id)
    query = request.args.get("q", "").strip() or _query_for(w)
    return jsonify({"query": query, "urls": collectors.dork_urls(query)})


@monitor.route("/sources")
@login_required
def sources():
    return jsonify({
        "sources": collectors.source_meta(),
        "suggested": collectors.load_sources().get("suggested_feeds", []),
        "capabilities": capabilities.report(),
    })


# -- Profile integration -----------------------------------------------------

@monitor.route("/<int:watch_id>/suggest-profiles")
@login_required
def suggest_profiles(watch_id):
    """Digital Hunter: which stored profiles do these posts point at?"""
    w = MonitorWatch.query.get_or_404(watch_id)
    profiles = Profile.query.all()
    cfg = engine.build_config(w, profiles)
    cfg["mode_hunter"] = True  # suggestions work even if the mode is off

    agg = {}
    for p in w.posts:
        a = engine.analyze(p, cfg)
        for m in a["profile_matches"]:
            entry = agg.setdefault(m["profile_id"], {
                "profile_id": m["profile_id"], "codename": m["codename"],
                "posts": [], "best": 0.0})
            entry["best"] = max(entry["best"], m["confidence"])
            entry["posts"].append({
                "post_id": p.id, "author": p.author, "confidence": m["confidence"],
                "matched": m["matched"], "kind": m["kind"], "via": m["via"],
                "score": a["score"], "linked": p.profile_id == m["profile_id"],
            })
    out = sorted(agg.values(), key=lambda e: e["best"], reverse=True)
    for e in out:
        e["posts"].sort(key=lambda x: x["confidence"], reverse=True)
        e["count"] = len(e["posts"])
    return jsonify({"suggestions": out})


@monitor.route("/posts/<int:post_id>/push-note", methods=["POST"])
@login_required
def push_note(post_id):
    """Write a post's findings onto a Profile as an IntelNote."""
    p = MonitorPost.query.get_or_404(post_id)
    data = request.json or {}
    pid = int(data.get("profile_id") or p.profile_id or 0)
    if not pid:
        return jsonify({"error": "Link the post to a profile first"}), 400
    profile = Profile.query.get_or_404(pid)

    cfg = _watch_config(p.watch)
    a = engine.analyze(p, cfg)
    score = p.manual_score if p.manual_score is not None else a["score"]

    lines = [
        "[Signal Monitor] %s on %s" % (p.author, p.platform),
        "Risk %s/100 - %s%s" % (score, a["verdict_label"],
                                (" - " + ", ".join(a["types"])) if a["types"] else ""),
        "",
        p.text[:1500],
    ]
    if p.url:
        lines += ["", "Link: " + p.url]
    if p.analyst_note:
        lines += ["", "Analyst note: " + p.analyst_note]

    note = IntelNote(profile_id=profile.id, content="\n".join(lines),
                     source="Signal Monitor / " + p.watch.name)
    db.session.add(note)
    if not p.profile_id:
        p.profile_id = profile.id
    db.session.commit()
    return jsonify({"ok": True, "note": note.to_dict()})


@monitor.route("/<int:watch_id>/to-linkmap", methods=["POST"])
@login_required
def to_linkmap(watch_id):
    """Build an entity graph from the watch and open it in the Link Mapper.

    Unlike the old star layout, this draws accounts, the domains they push,
    the locations those resolve to and any correlated Profiles, so shared
    infrastructure shows up as shared nodes.

    Pass `graph_id` to merge into an existing map instead of creating one.
    """
    w = MonitorWatch.query.get_or_404(watch_id)
    data = request.json or {}
    scoring.ensure_fresh(w)

    payload, stats = graphbuild.build(
        w,
        min_score=int(data.get("min_score") or 30),
        include_domains=data.get("domains", True),
        include_profiles=data.get("profiles", True),
        include_geo=bool(data.get("geo")),
        verdicts=tuple(data.get("verdicts") or ("bad", "warn")),
    )

    if not payload["nodes"] or len(payload["nodes"]) <= 1:
        return jsonify({"error": "No posts met the threshold, so there is "
                                 "nothing to map. Lower the minimum score or "
                                 "collect more posts."}), 400

    merged_stats = None
    graph_id = data.get("graph_id")
    if graph_id:
        graph = Graph.query.get_or_404(int(graph_id))
        combined, merged_stats = graphbuild.merge(graph.graph_json, payload)
        graph.graph_json = json.dumps(combined)
        graph.updated_at = datetime.utcnow()
    else:
        graph = Graph(
            title=w.name + " - Signal Map",
            profile_id=w.profile_id,
            graph_json=json.dumps(payload),
        )
        db.session.add(graph)
    db.session.commit()

    return jsonify({
        "ok": True, "graph_id": graph.id,
        "url": url_for("linkmap.edit_map", graph_id=graph.id),
        "nodes": stats["nodes"], "edges": stats["edges"],
        "stats": stats, "merged": merged_stats,
        "title": graph.title,
    })


@monitor.route("/<int:watch_id>/linkmap-preview")
@login_required
def linkmap_preview(watch_id):
    """What a map would contain, before committing to building it."""
    w = MonitorWatch.query.get_or_404(watch_id)
    scoring.ensure_fresh(w)
    _, stats = graphbuild.build(
        w, min_score=int(request.args.get("min_score") or 30),
        include_domains=request.args.get("domains", "1") != "0",
        include_profiles=request.args.get("profiles", "1") != "0",
        include_geo=False)
    existing = [{"id": g.id, "title": g.title,
                 "updated_at": g.updated_at.strftime("%Y-%m-%d %H:%M")
                 if g.updated_at else ""}
                for g in Graph.query.order_by(Graph.updated_at.desc()).limit(20)]
    return jsonify({"stats": stats, "graphs": existing})


@monitor.route("/<int:watch_id>/network")
@login_required
def network_analysis(watch_id):
    """Rank the accounts and domains by their position in the network.

    Degree alone says who posted most. Betweenness says who connects otherwise
    separate clusters, which is a better description of who matters in a
    coordinated push. Needs NetworkX; without it the degree counts are still
    returned, so the endpoint stays useful.
    """
    w = MonitorWatch.query.get_or_404(watch_id)
    scoring.ensure_fresh(w)
    payload, stats = graphbuild.build(
        w, min_score=int(request.args.get("min_score") or 0),
        include_domains=True, include_profiles=True,
        verdicts=tuple(request.args.get("verdicts", "bad,warn").split(",")))

    by_id = {n["id"]: n for n in payload["nodes"]}
    degree = {}
    for e in payload["edges"]:
        degree[e["from"]] = degree.get(e["from"], 0) + 1
        degree[e["to"]] = degree.get(e["to"], 0) + 1

    nx = capabilities.load("networkx")
    metrics = {}
    communities = {}
    if nx is not None and payload["edges"]:
        G = nx.Graph()
        for n in payload["nodes"]:
            G.add_node(n["id"])
        for e in payload["edges"]:
            G.add_edge(e["from"], e["to"])
        try:
            between = nx.betweenness_centrality(G)
            degree_c = nx.degree_centrality(G)
            metrics = {nid: {"betweenness": round(between.get(nid, 0), 4),
                             "degree_centrality": round(degree_c.get(nid, 0), 4)}
                       for nid in G.nodes}
            for i, group in enumerate(
                    nx.algorithms.community.greedy_modularity_communities(G)):
                for nid in group:
                    communities[nid] = i
        except Exception:
            metrics = {}

    ranked = []
    for nid, node in by_id.items():
        if node["type"] == "organization":
            continue  # the subject is connected to everything by construction
        m = metrics.get(nid, {})
        ranked.append({
            "id": nid, "label": node["label"], "type": node["type"],
            "degree": degree.get(nid, 0),
            "betweenness": m.get("betweenness"),
            "degree_centrality": m.get("degree_centrality"),
            "community": communities.get(nid),
            "score": node.get("score"), "verdict": node.get("verdict"),
            "post_id": node.get("post_id"), "url": node.get("url", ""),
        })
    ranked.sort(key=lambda r: (-(r["betweenness"] or 0), -r["degree"]))

    return jsonify({
        "available": nx is not None,
        "reason": "" if nx is not None else capabilities.probe("networkx")["reason"],
        "stats": stats,
        "communities": len(set(communities.values())) if communities else 0,
        "nodes": ranked[:40],
        "note": ("Betweenness highlights accounts that bridge otherwise "
                 "separate clusters -- often the coordinators rather than the "
                 "loudest posters."),
    })


# -- Export ------------------------------------------------------------------

@monitor.route("/<int:watch_id>/export.csv")
@login_required
def export_csv(watch_id):
    w = MonitorWatch.query.get_or_404(watch_id)
    _, results = _analyze_watch(w)

    def safe(v):
        s = "" if v is None else str(v)
        return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["platform", "author", "handle", "verified", "risk_score",
                     "verdict", "overridden", "threat_types", "signals",
                     "links", "profile", "status", "analyst_note", "url",
                     "posted_at", "source", "text"])
    for p in sorted(w.posts, key=lambda x: -results[x.id]["score"]):
        a = results[p.id]
        writer.writerow([safe(x) for x in [
            p.platform, p.author, p.handle, p.verified, a["score"],
            a["verdict_label"], a["overridden"], "; ".join(a["types"]),
            "; ".join("%s (%+d)" % (s["label"], s["w"]) for s in a["signals"]),
            " ".join(l["display"] for l in a["links"]),
            p.linked_profile.codename if p.linked_profile else "",
            p.status, p.analyst_note, p.url, p.posted_at or "", p.source, p.text,
        ]])

    filename = "signal-monitor-%d-%s.csv" % (w.id, datetime.utcnow().strftime("%Y%m%d"))
    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": 'attachment; filename="%s"' % filename},
    )


@monitor.route("/<int:watch_id>/export.json")
@login_required
def export_json(watch_id):
    w = MonitorWatch.query.get_or_404(watch_id)
    _, results = _analyze_watch(w)
    payload = {
        "watch": w.to_dict(),
        "exported_at": datetime.utcnow().isoformat(timespec="seconds"),
        "posts": [dict(p.to_dict(), analysis=results[p.id]) for p in w.posts],
    }
    filename = "signal-monitor-%d.json" % w.id
    return Response(
        json.dumps(payload, indent=2),
        mimetype="application/json",
        headers={"Content-Disposition": 'attachment; filename="%s"' % filename},
    )


# -- Credential vault --------------------------------------------------------

@monitor.route("/vault")
@login_required
def vault_page():
    creds = MonitorCredential.query.order_by(MonitorCredential.platform,
                                             MonitorCredential.label).all()
    rows = []
    for c in creds:
        cookies = None
        if vault.is_unlocked() and c.kind == "cookies":
            data = vault.decrypt(c.secret_blob) or {}
            cookies = data.get("cookies")
        rows.append(c.to_dict(health=vault.health(c, cookies)))
    return render_template(
        "monitor/vault.html",
        credentials=rows,
        unlocked=vault.is_unlocked(),
        guidance=vault.PLATFORM_GUIDANCE,
        playwright=authfetch.playwright_available(),
    )


@monitor.route("/vault/status")
@login_required
def vault_status():
    """Vault state plus the credential list, for pickers on other pages.

    Never includes secrets -- only labels and health, so it is safe to call
    from any page.
    """
    creds = MonitorCredential.query.order_by(MonitorCredential.label).all()
    return jsonify({
        "unlocked": vault.is_unlocked(),
        "count": len(creds),
        "playwright": authfetch.playwright_available(),
        "credentials": [{
            "id": c.id, "label": c.label, "platform": c.platform,
            "kind": c.kind, "enabled": c.enabled, "last_ok": c.last_ok,
        } for c in creds],
    })


@monitor.route("/vault/unlock", methods=["POST"])
@login_required
def vault_unlock():
    data = request.json or {}
    passphrase = data.get("passphrase") or ""
    if len(passphrase) < 8:
        return jsonify({"error": "Use a passphrase of at least 8 characters."}), 400
    vault.unlock(passphrase)

    # If secrets already exist, a wrong passphrase must not look like success.
    existing = MonitorCredential.query.filter(
        MonitorCredential.secret_blob != "").first()
    if existing and not vault.verify(existing.secret_blob):
        vault.lock()
        return jsonify({"error": "That passphrase does not match the stored "
                                 "credentials."}), 400
    return jsonify({"ok": True, "unlocked": True})


@monitor.route("/vault/lock", methods=["POST"])
@login_required
def vault_lock():
    vault.lock()
    return jsonify({"ok": True, "unlocked": False})


@monitor.route("/vault/credentials", methods=["POST"])
@login_required
def vault_add():
    if not vault.is_unlocked():
        return jsonify({"error": "Unlock the vault first."}), 403
    data = request.json or {}
    label = (data.get("label") or "").strip()
    platform = (data.get("platform") or "generic").strip().lower()
    kind = (data.get("kind") or "cookies").strip()
    if not label:
        return jsonify({"error": "Give the credential a label."}), 400
    if kind not in ("cookies", "token", "password"):
        return jsonify({"error": "Unknown credential kind."}), 400

    payload, hint = {}, ""
    if kind == "cookies":
        cookies = vault.parse_cookies(data.get("cookies") or "")
        if not cookies:
            return jsonify({"error": "No cookies could be read from that. Paste a "
                                     "Cookie header, cookies.txt, or a JSON export."}), 400
        payload = {"cookies": cookies}
        hint = "%d cookie(s)" % len(cookies)
    elif kind == "token":
        token = (data.get("token") or "").strip()
        if not token:
            return jsonify({"error": "Paste the API token."}), 400
        payload = {"token": token}
        hint = token[:4] + "…" + token[-4:] if len(token) > 12 else "token"
    else:
        username = (data.get("username") or "").strip()
        password = data.get("password") or ""
        if not username or not password:
            return jsonify({"error": "Username and password are both required."}), 400
        payload = {"username": username, "password": password}
        hint = username[:3] + "…" if len(username) > 3 else username

    c = MonitorCredential(
        label=label, platform=platform, kind=kind,
        secret_blob=vault.encrypt(payload),
        account_hint=hint,
        expires_at=(data.get("expires_at") or "").strip() or None,
    )
    db.session.add(c)
    db.session.commit()

    cookies = payload.get("cookies")
    return jsonify(c.to_dict(health=vault.health(c, cookies))), 201


@monitor.route("/vault/credentials/<int:cred_id>", methods=["PATCH", "DELETE"])
@login_required
def vault_edit(cred_id):
    c = MonitorCredential.query.get_or_404(cred_id)
    if request.method == "DELETE":
        db.session.delete(c)
        db.session.commit()
        return jsonify({"ok": True})
    data = request.json or {}
    if "enabled" in data:
        c.enabled = bool(data["enabled"])
    if "label" in data:
        c.label = (data["label"] or c.label).strip()
    if "expires_at" in data:
        c.expires_at = (data["expires_at"] or "").strip() or None
    db.session.commit()
    return jsonify(c.to_dict())


@monitor.route("/vault/credentials/<int:cred_id>/test", methods=["POST"])
@login_required
def vault_test(cred_id):
    """Try the credential against a light request and record the outcome."""
    if not vault.is_unlocked():
        return jsonify({"error": "Unlock the vault first."}), 403
    c = MonitorCredential.query.get_or_404(cred_id)
    data = request.json or {}
    secret = vault.decrypt(c.secret_blob)
    if secret is None:
        return jsonify({"error": "This credential cannot be decrypted with the "
                                 "current passphrase."}), 400

    result = authfetch.collect_authenticated(
        c, secret.get("cookies") or [], data.get("query") or "test",
        limit=3, url=data.get("url") or None,
        use_browser=bool(data.get("use_browser")))

    c.last_used = datetime.utcnow()
    c.last_ok = bool(result["ok"])
    c.last_error = "" if result["ok"] else (result["note"] or "")[:400]
    c.use_count = (c.use_count or 0) + 1
    db.session.commit()

    return jsonify({
        "ok": result["ok"], "note": result["note"], "blocked": result["blocked"],
        "manual_url": result["manual_url"], "found": len(result["posts"]),
        "strategy": result.get("strategy"),
        "sample": [p["text"][:160] for p in result["posts"][:2]],
    })


@monitor.route("/api/collect-auth", methods=["POST"])
@login_required
def api_collect_auth():
    """Collect using a stored credential, without storing the result.

    The credential stays in the server-side vault -- secrets never reach the
    browser -- but the posts it fetches are handed back for the browser to
    store, like every other collector.
    """
    if not vault.is_unlocked():
        return jsonify({"error": "Unlock the credential vault first."}), 403
    data = request.json or {}
    watch = data.get("watch") or {}
    cfg = _watch_from_payload(watch)
    spec = cfg["spec"]

    cred_id = data.get("credential_id")
    if not cred_id:
        return jsonify({"error": "Pick a credential."}), 400
    c = MonitorCredential.query.get_or_404(int(cred_id))
    if not c.enabled:
        return jsonify({"error": "That credential is disabled."}), 400
    secret = vault.decrypt(c.secret_blob)
    if secret is None:
        return jsonify({"error": "This credential cannot be decrypted."}), 400

    query = (data.get("query") or "").strip() or spec.build_query() or         (watch.get("subject") or "").strip()
    result = authfetch.collect_authenticated(
        c, secret.get("cookies") or [], query,
        limit=int(data.get("limit") or 25), url=data.get("url") or None,
        use_browser=bool(data.get("use_browser")))

    c.last_used = datetime.utcnow()
    c.last_ok = bool(result["ok"])
    c.last_error = "" if result["ok"] else (result["note"] or "")[:400]
    c.use_count = (c.use_count or 0) + 1
    db.session.commit()

    known = set(data.get("known_keys") or [])
    strict = data.get("strict", True)
    wid = watch.get("id") or 0
    kept, off_topic, duplicate, flagged = [], 0, 0, 0
    for raw in result["posts"]:
        author = str(raw.get("author") or "").strip()
        text = str(raw.get("text") or "").strip()
        if not author or not text:
            continue
        platform = str(raw.get("platform") or "Other").strip()
        url = str(raw.get("url") or "").strip()
        key = _dedupe_key(wid, platform, author, text, url)
        if key in known:
            duplicate += 1
            continue
        known.add(key)
        post = {
            "watch_id": wid, "platform": platform, "author": author[:200],
            "handle": str(raw.get("handle") or "").lstrip("@")[:120],
            "verified": bool(raw.get("verified")), "text": text, "url": url,
            "source": "auth:" + c.platform,
            "source_url": str(raw.get("source_url") or "")[:500],
            "link_kind": raw.get("link_kind") or "direct",
            "posted_at": raw.get("posted_at"),
            "posted_ts": raw.get("posted_at") or datetime.utcnow().isoformat(
                timespec="seconds"),
            "collected_at": datetime.utcnow().isoformat(timespec="seconds"),
            "dedupe_key": key, "status": "new", "pinned": False,
            "manual_score": None, "manual_verdict": None,
            "analyst_note": "", "profile_id": None,
        }
        a = engine.analyze(post, cfg)
        if strict and not a["relevant"]:
            off_topic += 1
            continue
        post.update({"score": a["score"], "verdict": a["verdict"],
                     "verdict_label": a["verdict_label"], "types": a["types"],
                     "relevant": a["relevant"],
                     "relevance": a.get("relevance", 0), "analysis": a})
        if a["verdict"] != "ok":
            flagged += 1
        kept.append(post)

    return jsonify({
        "ok": result["ok"], "posts": kept, "added": len(kept),
        "skipped": duplicate, "off_topic": off_topic, "flagged": flagged,
        "note": result["note"], "blocked": result["blocked"],
        "manual_url": result["manual_url"], "strategy": result.get("strategy"),
        "query": query,
        "rules_hash": keywords_rules_hash(watch, spec),
    })


@monitor.route("/<int:watch_id>/collect-auth", methods=["POST"])
@login_required
def collect_authenticated_route(watch_id):
    """Collect into a stored watch using a stored credential (server-side)."""
    if not vault.is_unlocked():
        return jsonify({"error": "Unlock the credential vault first."}), 403
    w = MonitorWatch.query.get_or_404(watch_id)
    data = request.json or {}
    cred_id = data.get("credential_id")
    if not cred_id:
        return jsonify({"error": "Pick a credential."}), 400
    c = MonitorCredential.query.get_or_404(int(cred_id))
    if not c.enabled:
        return jsonify({"error": "That credential is disabled."}), 400

    secret = vault.decrypt(c.secret_blob)
    if secret is None:
        return jsonify({"error": "This credential cannot be decrypted."}), 400

    query = (data.get("query") or "").strip() or _query_for(w)
    result = authfetch.collect_authenticated(
        c, secret.get("cookies") or [], query,
        limit=int(data.get("limit") or 25), url=data.get("url") or None,
        use_browser=bool(data.get("use_browser")))

    c.last_used = datetime.utcnow()
    c.last_ok = bool(result["ok"])
    c.last_error = "" if result["ok"] else (result["note"] or "")[:400]
    c.use_count = (c.use_count or 0) + 1

    stats = _add_posts(w, result["posts"], source="auth:" + c.platform,
                       drop_off_topic=data.get("strict", True), enrich=True)
    db.session.commit()

    return jsonify(dict(
        stats,
        ok=result["ok"], note=result["note"], blocked=result["blocked"],
        manual_url=result["manual_url"], strategy=result.get("strategy"),
        query=query, counts=scoring.counts_for(w)))


# -- Optional AI second opinion ----------------------------------------------

@monitor.route("/ai-status")
@login_required
def ai_status():
    return jsonify({"enabled": bool(os.environ.get("ANTHROPIC_API_KEY"))})


@monitor.route("/posts/<int:post_id>/second-opinion", methods=["POST"])
@login_required
def second_opinion(post_id):
    """Proxy to the Anthropic API so the key never reaches the browser."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return jsonify({"error": "No ANTHROPIC_API_KEY set on the server. "
                                 "Set it and restart to enable second opinions."}), 503

    p = MonitorPost.query.get_or_404(post_id)
    w = p.watch
    cfg = _watch_config(w)
    a = engine.analyze(p, cfg)

    modes = []
    if w.mode_release:
        modes.append("media release threat verification")
    if w.mode_hunter:
        modes.append("identity/digital hunting")
    mode_line = " and ".join(modes) if modes else "general keyword monitoring"

    prompt = (
        "You are a trust-and-safety analyst reviewing a social media post.\n\n"
        "Monitoring case: %s\nSubject: %s\nMode: %s\nKeywords: %s\n\n"
        % (w.name, w.subject or "not set", mode_line, w.keywords or "none")
    )
    if w.mode_release and w.reference_text:
        prompt += 'Official reference text:\n"""%s"""\n\n' % w.reference_text[:2000]
        prompt += ("Official accounts: %s\nOfficial domains: %s\n\n"
                   % (w.official_accounts or "none", w.official_domains or "none"))
    prompt += (
        'Post on %s by "%s" (@%s, %s):\n"""%s"""\n\n'
        % (p.platform, p.author, p.handle or "unknown",
           "platform-verified" if p.verified else "not verified", p.text[:3000])
    )
    if p.url:
        prompt += "Linked URL: %s\n\n" % p.url
    prompt += (
        'A rule-based scanner rated it "%s" at %d/100 based on: %s.\n\n'
        "Give an independent judgment. Respond ONLY with a JSON object, no markdown:\n"
        '{"verdict":"likely_authentic"|"needs_review"|"high_risk",'
        '"threat_type":"short label or empty string",'
        '"rationale":"max 45 words, plain language",'
        '"disagrees":true|false}'
        % (a["verdict_label"], a["score"],
           "; ".join(s["label"] for s in a["signals"]) or "no signals")
    )

    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": api_key,
                     "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": "claude-sonnet-5", "max_tokens": 1000,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=45,
        )
        if resp.status_code != 200:
            return jsonify({"error": "Anthropic API returned %d" % resp.status_code}), 502
        body = resp.json()
        txt = "".join(b.get("text", "") for b in body.get("content", []))
        txt = txt.replace("```json", "").replace("```", "").strip()
        return jsonify({"ok": True, "data": json.loads(txt), "scanner_score": a["score"]})
    except requests.exceptions.RequestException as e:
        return jsonify({"error": "Could not reach Anthropic: %s" % type(e).__name__}), 502
    except ValueError:
        return jsonify({"error": "Claude's reply could not be parsed as JSON"}), 502


# -- Record-based helpers ----------------------------------------------------
#
# These work over plain post dicts, so the same logic serves browser-held data
# and stored rows without being written twice.

def _rec_verdict(p):
    a = p.get("analysis") or {}
    return p.get("manual_verdict") or p.get("verdict") or a.get("verdict") or "ok"


def _rec_score(p):
    a = p.get("analysis") or {}
    s = p.get("manual_score")
    return (p.get("score", a.get("score", 0)) or 0) if s is None else s


def _rank_network(payload, stats):
    """Rank graph nodes by their position in the network.

    Degree alone says who posted most. Betweenness says who connects otherwise
    separate clusters, which describes a coordinator better. Needs NetworkX;
    without it the degree counts still come back.
    """
    by_id = {n["id"]: n for n in payload["nodes"]}
    degree = {}
    for e in payload["edges"]:
        degree[e["from"]] = degree.get(e["from"], 0) + 1
        degree[e["to"]] = degree.get(e["to"], 0) + 1

    nx = capabilities.load("networkx")
    metrics, communities = {}, {}
    if nx is not None and payload["edges"]:
        G = nx.Graph()
        for n in payload["nodes"]:
            G.add_node(n["id"])
        for e in payload["edges"]:
            G.add_edge(e["from"], e["to"])
        try:
            between = nx.betweenness_centrality(G)
            degree_c = nx.degree_centrality(G)
            metrics = {nid: {"betweenness": round(between.get(nid, 0), 4),
                             "degree_centrality": round(degree_c.get(nid, 0), 4)}
                       for nid in G.nodes}
            for i, group in enumerate(
                    nx.algorithms.community.greedy_modularity_communities(G)):
                for nid in group:
                    communities[nid] = i
        except Exception:
            metrics = {}

    ranked = []
    for nid, node in by_id.items():
        if node["type"] == "organization":
            continue  # the subject connects to everything by construction
        m = metrics.get(nid, {})
        ranked.append({
            "id": nid, "label": node["label"], "type": node["type"],
            "degree": degree.get(nid, 0),
            "betweenness": m.get("betweenness"),
            "degree_centrality": m.get("degree_centrality"),
            "community": communities.get(nid),
            "score": node.get("score"), "verdict": node.get("verdict"),
            "post_id": node.get("post_id"), "url": node.get("url", ""),
        })
    ranked.sort(key=lambda r: (-(r["betweenness"] or 0), -r["degree"]))

    return {
        "available": nx is not None,
        "reason": "" if nx is not None else capabilities.probe("networkx")["reason"],
        "stats": stats,
        "communities": len(set(communities.values())) if communities else 0,
        "nodes": ranked[:40],
        "note": ("Betweenness highlights accounts that bridge otherwise "
                 "separate clusters -- often the coordinators rather than the "
                 "loudest posters."),
    }


def _briefing_from_records(watch, posts):
    """The situational summary, over post records."""
    total = len(posts)
    counts = {"bad": 0, "warn": 0, "ok": 0}
    status_counts = {s: 0 for s in STATUSES}
    type_counts, signal_counts = {}, {}
    platform_stats, author_stats, domain_stats = {}, {}, {}

    for p in posts:
        a = p.get("analysis") or {}
        verdict = _rec_verdict(p)
        score = _rec_score(p)
        if verdict in counts:
            counts[verdict] += 1
        st = p.get("status") or "new"
        status_counts[st] = status_counts.get(st, 0) + 1

        for t in (p.get("types") or a.get("types") or []):
            type_counts[t] = type_counts.get(t, 0) + 1
        for s in (a.get("signals") or []):
            if s.get("w", 0) > 0:
                e = signal_counts.setdefault(s["label"], {"label": s["label"],
                                                          "count": 0, "weight": 0})
                e["count"] += 1
                e["weight"] += s["w"]

        plat = p.get("platform") or "Other"
        ps = platform_stats.setdefault(plat, {"platform": plat, "total": 0,
                                              "bad": 0, "warn": 0, "ok": 0,
                                              "score": 0})
        ps["total"] += 1
        if verdict in ps:
            ps[verdict] += 1
        ps["score"] += score

        # Group by the account, not the feed host -- every article from one
        # aggregator shares a handle, which would merge unrelated outlets.
        key = ((p.get("author") or "") + "|" + plat).lower() or "?"
        aus = author_stats.setdefault(key, {
            "author": p.get("author"), "handle": p.get("handle"),
            "platform": plat, "posts": 0, "flagged": 0, "top_score": 0,
            "types": set(), "post_id": p.get("id"), "url": p.get("url")})
        aus["posts"] += 1
        if verdict != "ok":
            aus["flagged"] += 1
        if score > aus["top_score"]:
            aus["top_score"] = score
            aus["post_id"] = p.get("id")
            aus["url"] = p.get("url")
        aus["types"].update(p.get("types") or a.get("types") or [])

        for l in (a.get("links") or []):
            if l.get("official") or not l.get("host"):
                continue
            ds = domain_stats.setdefault(l["host"], {
                "host": l["host"], "count": 0, "risky": bool(l.get("flags")),
                "reasons": set()})
            ds["count"] += 1
            if l.get("flags"):
                ds["risky"] = True
                ds["reasons"].update(f["label"] for f in l["flags"])

    for ps in platform_stats.values():
        ps["avg"] = round(ps["score"] / ps["total"]) if ps["total"] else 0
        del ps["score"]

    actors = [dict(a, types=sorted(a["types"]))
              for a in author_stats.values() if a["flagged"]]
    actors.sort(key=lambda a: (-a["flagged"], -a["top_score"]))

    domains = [dict(d, reasons=sorted(d["reasons"])) for d in domain_stats.values()]
    domains.sort(key=lambda d: (not d["risky"], -d["count"]))

    drivers = sorted(signal_counts.values(), key=lambda s: -s["weight"])

    ranked = sorted(posts, key=lambda p: -_rec_score(p))
    top = [{
        "id": p.get("id"), "author": p.get("author"), "handle": p.get("handle"),
        "platform": p.get("platform"), "score": _rec_score(p),
        "verdict": _rec_verdict(p),
        "verdict_label": engine.VERDICT_LABELS.get(_rec_verdict(p), ""),
        "types": p.get("types") or [], "url": p.get("url"),
        "link_kind": p.get("link_kind") or "direct",
        "status": p.get("status"), "excerpt": (p.get("text") or "")[:220],
        "recommendation": (p.get("analysis") or {}).get("recommendation", ""),
    } for p in ranked[:8] if _rec_verdict(p) != "ok"]

    pending = [p for p in posts
               if _rec_verdict(p) != "ok" and (p.get("status") or "new") == "new"]
    coverage = [(p.get("analysis") or {}).get("coverage", 0) for p in posts
                if (p.get("analysis") or {}).get("relevant")]
    dates = sorted(str(p.get("posted_at") or "") for p in posts if p.get("posted_at"))

    gaps = []
    get = watch.get if isinstance(watch, dict) else (lambda k, d=None: getattr(watch, k, d))
    spec = spec_for(watch)
    if not spec.has_anchor:
        gaps.append("No required keywords set, so relevance cannot be judged and "
                    "live collection has nothing specific to search for.")
    if get("mode_release") and not (get("reference_text") or "").strip():
        gaps.append("Media Release Threat mode is on but no reference text is set, "
                    "so fidelity and contradiction rules cannot run.")
    if get("mode_release") and not get("official_accounts"):
        gaps.append("No official accounts listed, so impersonation cannot be "
                    "distinguished from the real account.")
    if get("mode_release") and not get("official_domains"):
        gaps.append("No official domains listed, so legitimate links score as "
                    "external.")
    if not total:
        gaps.append("No posts collected yet.")

    return {
        "watch": watch if isinstance(watch, dict) else watch.to_dict(),
        "generated_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M"),
        "total": total,
        "counts": counts,
        "status_counts": status_counts,
        "pending_review": len(pending),
        "flagged": counts["bad"] + counts["warn"],
        "types": sorted(({"type": k, "count": v} for k, v in type_counts.items()),
                        key=lambda x: -x["count"]),
        "drivers": drivers[:8],
        "platforms": sorted(platform_stats.values(), key=lambda p: -p["total"]),
        "actors": actors[:8],
        "domains": domains[:8],
        "top": top,
        "avg_coverage": round(sum(coverage) / len(coverage), 2) if coverage else None,
        "date_range": {"first": dates[0], "last": dates[-1]} if dates else None,
        "gaps": gaps,
        "headline": _headline(counts, len(pending), total),
    }


# ── Stateless API ───────────────────────────────────────────────────────────
#
# The browser is the system of record (app/static/js/store.js). These endpoints
# hold no state: they take a watch definition and some posts as JSON, do the
# work, and hand the result straight back. Nothing is written to the database.
#
# This is what lets the same scoring engine serve browser-stored data without
# duplicating 850 lines of rules in JavaScript.


def _watch_from_payload(data):
    """Build an engine config from a watch definition sent by the browser."""
    watch = dict(data or {})
    watch.setdefault("weights", watch.get("weights") or {})
    profiles = watch.get("profiles") or []

    # Hunter mode correlates against Profile-shaped objects. The browser holds
    # them, so they arrive in the payload; adapt them to what the engine reads.
    class _P:
        def __init__(self, d):
            self.id = d.get("id")
            self.codename = d.get("codename") or ""
            self.real_name = d.get("real_name") or ""
            self.known_aliases = d.get("known_aliases") or []
            self.social_links = [
                type("L", (), {"username": s.get("username"),
                               "platform": s.get("platform")})()
                for s in (d.get("social_links") or [])
            ]

    objs = [_P(p) for p in profiles] if watch.get("mode_hunter") else []
    return engine.build_config(watch, objs)


@monitor.route("/api/score", methods=["POST"])
@login_required
def api_score():
    """Score a batch of posts against a watch definition. Stores nothing.

    Body: {"watch": {...}, "posts": [{...}, ...]}
    Returns one analysis per post, in the order given.
    """
    data = request.json or {}
    posts = data.get("posts") or []
    if not isinstance(posts, list):
        return jsonify({"error": "posts must be a list"}), 400
    if len(posts) > 500:
        return jsonify({"error": "Score at most 500 posts per request. "
                                 "Send them in batches."}), 400

    cfg = _watch_from_payload(data.get("watch") or {})
    spec = cfg["spec"]
    out = []
    for p in posts:
        a = engine.analyze(p, cfg)
        # Flatten the fields the browser indexes on, so it never has to parse
        # the whole analysis just to filter a list.
        out.append({
            "score": a["score"],
            "verdict": a["verdict"],
            "verdict_label": a["verdict_label"],
            "types": a["types"],
            "relevant": a["relevant"],
            "relevance": a.get("relevance", 0),
            "analysis": a,
        })
    return jsonify({
        "results": out,
        "rules_hash": keywords_rules_hash(data.get("watch") or {}, spec),
        "count": len(out),
    })


@monitor.route("/api/collect", methods=["POST"])
@login_required
def api_collect():
    """Fetch from live sources and score, without storing anything.

    Body: {"watch": {...}, "sources": [...], "query": "...", "days": 7,
           "options": {...}, "strict": true, "known_keys": [...]}

    `known_keys` are dedupe keys the browser already holds, so duplicates are
    dropped here rather than shipped across and discarded there.
    """
    data = request.json or {}
    keys = data.get("sources") or []
    if not keys:
        return jsonify({"error": "Pick at least one source"}), 400

    watch = data.get("watch") or {}
    cfg = _watch_from_payload(watch)
    spec = cfg["spec"]
    query = (data.get("query") or "").strip() or spec.build_query() or \
        (watch.get("subject") or "").strip()
    if not query:
        return jsonify({"error": "Add keywords or a subject to search for"}), 400

    since = _parse_since(data.get("days"))
    raw_posts, report = collectors.collect(keys, query, data.get("options") or {},
                                           since=since)

    known = set(data.get("known_keys") or [])
    strict = data.get("strict", True)
    watch_id = watch.get("id") or 0

    kept, off_topic, duplicate, flagged = [], 0, 0, 0
    seen = set()
    for raw in raw_posts:
        author = str(raw.get("author") or "").strip()
        text = str(raw.get("text") or "").strip()
        if not author or not text:
            continue
        platform = str(raw.get("platform") or "Other").strip()
        url = str(raw.get("url") or "").strip()

        key = _dedupe_key(watch_id, platform, author, text, url)
        if key in known or key in seen:
            duplicate += 1
            continue
        seen.add(key)

        post = {
            "watch_id": watch_id,
            "platform": platform,
            "author": author[:200],
            "handle": str(raw.get("handle") or "").lstrip("@")[:120],
            "verified": bool(raw.get("verified")),
            "text": text,
            "url": url,
            "source": raw.get("source") or "live",
            "source_url": str(raw.get("source_url") or "")[:500],
            "link_kind": raw.get("link_kind") or "direct",
            "posted_at": raw.get("posted_at"),
            "posted_ts": raw.get("posted_at") or datetime.utcnow().isoformat(
                timespec="seconds"),
            "collected_at": datetime.utcnow().isoformat(timespec="seconds"),
            "dedupe_key": key,
            "status": "new",
            "pinned": False,
            "manual_score": None,
            "manual_verdict": None,
            "analyst_note": "",
            "profile_id": None,
        }

        a = engine.analyze(post, cfg)
        if strict and not a["relevant"]:
            off_topic += 1
            continue
        post.update({
            "score": a["score"], "verdict": a["verdict"],
            "verdict_label": a["verdict_label"], "types": a["types"],
            "relevant": a["relevant"], "relevance": a.get("relevance", 0),
            "analysis": a,
        })
        if a["verdict"] != "ok":
            flagged += 1
        kept.append(post)

    return jsonify({
        "ok": True,
        "posts": kept,
        "added": len(kept),
        "skipped": duplicate,
        "off_topic": off_topic,
        "flagged": flagged,
        "query": query,
        "report": report,
        "rules_hash": keywords_rules_hash(watch, spec),
    })


@monitor.route("/api/enrich", methods=["POST"])
@login_required
def api_enrich():
    """Network intelligence for a batch of posts: links, IPs, geo, phishing.

    Body: {"posts": [{"id":…, "text":…, "url":…}], "geo": true, "phishing": true}
    """
    data = request.json or {}
    posts = data.get("posts") or []
    if len(posts) > 100:
        return jsonify({"error": "Enrich at most 100 posts per request."}), 400

    do_geo = data.get("geo", True)
    do_phish = data.get("phishing", True)
    out = []
    for p in posts:
        findings = netintel.enrich_post(p.get("text") or "", p.get("url") or "",
                                        do_geo=do_geo, do_phish=do_phish)
        out.append({"id": p.get("id"), "findings": findings})
    return jsonify({"results": out, "feeds": netintel.feed_status()})


@monitor.route("/api/keywords", methods=["POST"])
@login_required
def api_keywords():
    """Resolve a keyword definition into its query and relevance rules."""
    data = request.json or {}
    spec = spec_for(data)
    d = spec.describe()
    sample = (data.get("sample") or "").strip()
    result = spec.evaluate(sample) if sample else None
    return jsonify({
        "required": d["required"], "optional": d["optional"],
        "excluded": d["excluded"], "match_mode": d["match_mode"],
        "subject": d["subject"], "demoted": d["demoted"],
        "generic_required": d["generic_required"],
        "query": spec.build_query(),
        "has_anchor": spec.has_anchor,
        "warning": _keyword_warning(spec),
        "alias_hints": alias_hint([t.raw for t in spec.required]),
        "sample_result": result,
    })


@monitor.route("/api/graph", methods=["POST"])
@login_required
def api_graph():
    """Build a link-map graph from browser-held posts.

    Body: {"watch": {...}, "posts": [...], "min_score": 30, "geo": false,
           "existing": "<graph_json>"}
    """
    data = request.json or {}
    payload, stats = graphbuild.build_from_records(
        data.get("watch") or {},
        data.get("posts") or [],
        min_score=int(data.get("min_score") or 30),
        include_domains=data.get("domains", True),
        include_profiles=data.get("profiles", True),
        include_geo=bool(data.get("geo")),
        verdicts=tuple(data.get("verdicts") or ("bad", "warn")),
        profiles=data.get("profile_records") or [],
    )
    merged_stats = None
    if data.get("existing"):
        payload, merged_stats = graphbuild.merge(data["existing"], payload)
    return jsonify({"graph": payload, "stats": stats, "merged": merged_stats})


@monitor.route("/api/network", methods=["POST"])
@login_required
def api_network():
    """Centrality ranking over a graph built from browser-held posts."""
    data = request.json or {}
    payload, stats = graphbuild.build_from_records(
        data.get("watch") or {}, data.get("posts") or [],
        min_score=int(data.get("min_score") or 0),
        include_domains=True, include_profiles=True,
        verdicts=tuple(data.get("verdicts") or ("bad", "warn")),
        profiles=data.get("profile_records") or [])
    return jsonify(_rank_network(payload, stats))


@monitor.route("/api/dashboard", methods=["POST"])
@login_required
def api_dashboard():
    """Aggregate browser-held posts into the dashboard picture.

    The browser could compute most of this itself, but doing it here keeps one
    implementation of the rollup logic and keeps the main thread free.
    """
    data = request.json or {}
    watches = data.get("watches") or []
    posts = data.get("posts") or []
    days = max(1, min(365, int(data.get("days") or 30)))
    cutoff = datetime.utcnow() - timedelta(days=days)

    names = {w.get("id"): w.get("name") for w in watches}
    totals = {"bad": 0, "warn": 0, "ok": 0}
    pending = escalated = 0
    by_day, by_platform, type_counts, per_watch = {}, {}, {}, {}
    flagged = []

    for p in posts:
        verdict = p.get("manual_verdict") or p.get("verdict") or "ok"
        score = p.get("manual_score")
        score = p.get("score", 0) if score is None else score
        status = p.get("status") or "new"
        wid = p.get("watch_id")

        if verdict in totals:
            totals[verdict] += 1
        row = per_watch.setdefault(wid, {"bad": 0, "warn": 0, "ok": 0, "pending": 0})
        if verdict in row:
            row[verdict] += 1
        if verdict != "ok" and status == "new":
            pending += 1
            row["pending"] += 1
        if status == "escalated":
            escalated += 1

        for t in (p.get("types") or []):
            type_counts[t] = type_counts.get(t, 0) + 1

        plat = p.get("platform") or "Other"
        ps = by_platform.setdefault(plat, {"platform": plat, "total": 0,
                                           "flagged": 0, "score": 0})
        ps["total"] += 1
        ps["score"] += score
        if verdict != "ok":
            ps["flagged"] += 1

        stamp = p.get("posted_ts") or p.get("posted_at") or p.get("collected_at")
        when = None
        if stamp:
            try:
                when = datetime.fromisoformat(str(stamp)[:19])
            except ValueError:
                when = None
        if when and when >= cutoff:
            d = by_day.setdefault(str(stamp)[:10], {
                "date": str(stamp)[:10], "total": 0, "bad": 0, "warn": 0, "ok": 0})
            d["total"] += 1
            if verdict in d:
                d[verdict] += 1

        if verdict != "ok":
            flagged.append({
                "post_id": p.get("id"), "watch_id": wid,
                "watch": names.get(wid, ""),
                "author": p.get("author"), "platform": plat,
                "score": score, "verdict": verdict,
                "verdict_label": engine.VERDICT_LABELS.get(verdict, ""),
                "types": p.get("types") or [], "status": status,
                "url": p.get("url"), "link_kind": p.get("link_kind") or "direct",
                "when": str(stamp or ""), "excerpt": (p.get("text") or "")[:180],
            })

    for ps in by_platform.values():
        ps["avg"] = round(ps["score"] / ps["total"]) if ps["total"] else 0
        del ps["score"]

    flagged.sort(key=lambda r: r["when"] or "", reverse=True)

    watch_rows, stale = [], []
    for w in watches:
        wid = w.get("id")
        counts = per_watch.get(wid, {"bad": 0, "warn": 0, "ok": 0, "pending": 0})
        total = counts["bad"] + counts["warn"] + counts["ok"]
        newest = max((str(p.get("posted_ts") or "") for p in posts
                      if p.get("watch_id") == wid), default="")
        watch_rows.append({
            "id": wid, "name": w.get("name"), "subject": w.get("subject"),
            "mode_label": _mode_label(w),
            "mode_release": bool(w.get("mode_release")),
            "mode_hunter": bool(w.get("mode_hunter")),
            "total": total,
            "counts": {k: counts[k] for k in ("bad", "warn", "ok")},
            "pending": counts["pending"], "newest": newest,
            "updated_at": w.get("updated_at") or "",
        })
        if not total:
            stale.append({"id": wid, "name": w.get("name"), "newest": "",
                          "why": "No posts collected yet"})
        elif newest and newest[:19] < (datetime.utcnow() - timedelta(days=7)).isoformat():
            stale.append({"id": wid, "name": w.get("name"), "newest": newest[:10],
                          "why": "Nothing new in over a week"})

    caps = capabilities.report()
    return jsonify({
        "generated_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M"),
        "days": days,
        "watch_count": len(watches),
        "totals": totals,
        "post_total": sum(totals.values()),
        "pending": pending, "escalated": escalated,
        "timeline": [by_day[k] for k in sorted(by_day)],
        "watches": watch_rows,
        "platforms": sorted(by_platform.values(), key=lambda x: -x["total"]),
        "types": sorted(({"type": k, "count": v} for k, v in type_counts.items()),
                        key=lambda x: -x["count"])[:10],
        "recent": flagged[:12],
        "queue": sorted([r for r in flagged if r["status"] == "new"],
                        key=lambda r: -r["score"])[:10],
        "stale": stale,
        "sources": data.get("sources") or [],
        "credentials": {"total": 0, "enabled": 0, "failing": 0,
                        "unlocked": vault.is_unlocked()},
        "profiles": len(data.get("profiles") or []),
        "capabilities": {
            "available": caps["counts"]["available"],
            "total": caps["counts"]["total"],
            "serverless": caps["serverless"],
            "missing": caps["missing"][:8],
        },
        "storage": {"mode": "browser", "ephemeral": False},
    })


def _mode_label(w):
    modes = []
    if w.get("mode_release"):
        modes.append("Media Release Threat")
    if w.get("mode_hunter"):
        modes.append("Digital Hunter")
    return " + ".join(modes) if modes else "Standard"


@monitor.route("/api/briefing", methods=["POST"])
@login_required
def api_briefing():
    """The situational summary for one watch, from browser-held posts."""
    data = request.json or {}
    watch = data.get("watch") or {}
    posts = data.get("posts") or []
    return jsonify(_briefing_from_records(watch, posts))


# -- Optional server sync ----------------------------------------------------
#
# The browser is authoritative, but a copy on the server is useful for moving
# between machines or keeping an off-browser backup. Both directions are
# explicit: nothing syncs on its own.

@monitor.route("/api/sync/push", methods=["POST"])
@login_required
def api_sync_push():
    """Replace the server's copy with the browser's data."""
    data = request.json or {}
    payload = data.get("payload") or {}
    if payload.get("format") != "profiler-backup":
        return jsonify({"error": "Expected a Profiler backup payload."}), 400

    stores = payload.get("data") or {}
    counts = {}

    if data.get("replace", True):
        MonitorPost.query.delete()
        MonitorFeed.query.delete()
        MonitorWatch.query.delete()
        db.session.commit()

    id_map = {}
    for w in stores.get("watches", []):
        row = MonitorWatch(
            name=w.get("name") or "Untitled",
            subject=w.get("subject") or "",
            keywords=w.get("keywords") or "",
            kw_required=w.get("kw_required") or "",
            kw_optional=w.get("kw_optional") or "",
            kw_excluded=w.get("kw_excluded") or "",
            kw_match_mode=w.get("kw_match_mode") or "all",
            mode_release=bool(w.get("mode_release")),
            mode_hunter=bool(w.get("mode_hunter")),
            reference_text=w.get("reference_text") or "",
            official_accounts=w.get("official_accounts") or "",
            official_domains=w.get("official_domains") or "",
            custom_flags=w.get("custom_flags") or "",
            threshold_review=int(w.get("threshold_review") or 30),
            threshold_high=int(w.get("threshold_high") or 60),
            weights_json=json.dumps(w.get("weights") or {}),
        )
        db.session.add(row)
        db.session.flush()
        id_map[w.get("id")] = row.id
    counts["watches"] = len(id_map)

    n = 0
    for p in stores.get("posts", []):
        wid = id_map.get(p.get("watch_id"))
        if not wid:
            continue
        stamp = p.get("posted_ts") or p.get("posted_at")
        try:
            posted_ts = datetime.fromisoformat(str(stamp)[:19]) if stamp else None
        except (ValueError, TypeError):
            posted_ts = None
        db.session.add(MonitorPost(
            watch_id=wid, platform=p.get("platform") or "Other",
            author=(p.get("author") or "")[:200],
            handle=(p.get("handle") or "")[:120],
            verified=bool(p.get("verified")),
            text=p.get("text") or "", url=p.get("url") or "",
            source=p.get("source") or "browser",
            source_url=(p.get("source_url") or "")[:500],
            link_kind=p.get("link_kind") or "direct",
            posted_at=p.get("posted_at"), posted_ts=posted_ts,
            dedupe_key=p.get("dedupe_key"),
            status=p.get("status") or "new",
            pinned=bool(p.get("pinned")),
            manual_score=p.get("manual_score"),
            manual_verdict=p.get("manual_verdict"),
            analyst_note=p.get("analyst_note") or "",
            cached_score=p.get("score"), cached_verdict=p.get("verdict"),
            cached_types=",".join(p.get("types") or []),
            cached_relevant=bool(p.get("relevant", True)),
            cached_json=json.dumps(p.get("analysis") or {}),
        ))
        n += 1
    counts["posts"] = n
    db.session.commit()
    return jsonify({"ok": True, "counts": counts,
                    "note": "The server copy now matches this browser."})


@monitor.route("/api/sync/pull")
@login_required
def api_sync_pull():
    """Hand the server's copy back in browser-store shape."""
    watches, posts = [], []
    for w in MonitorWatch.query.all():
        watches.append({
            "id": w.id, "name": w.name, "subject": w.subject or "",
            "keywords": w.keywords or "",
            "kw_required": w.kw_required or "", "kw_optional": w.kw_optional or "",
            "kw_excluded": w.kw_excluded or "",
            "kw_match_mode": w.kw_match_mode or "all",
            "mode_release": w.mode_release, "mode_hunter": w.mode_hunter,
            "reference_text": w.reference_text or "",
            "official_accounts": w.official_accounts or "",
            "official_domains": w.official_domains or "",
            "custom_flags": w.custom_flags or "",
            "threshold_review": w.threshold_review,
            "threshold_high": w.threshold_high,
            "weights": w.weights,
            "updated_at": w.updated_at.isoformat() if w.updated_at else "",
        })
    for p in MonitorPost.query.all():
        try:
            analysis = json.loads(p.cached_json) if p.cached_json else {}
        except (ValueError, TypeError):
            analysis = {}
        posts.append({
            "id": p.id, "watch_id": p.watch_id, "platform": p.platform,
            "author": p.author, "handle": p.handle, "verified": p.verified,
            "text": p.text, "url": p.url, "source": p.source,
            "source_url": p.source_url, "link_kind": p.link_kind,
            "posted_at": p.posted_at,
            "posted_ts": p.posted_ts.isoformat() if p.posted_ts else "",
            "collected_at": p.collected_at.isoformat() if p.collected_at else "",
            "dedupe_key": p.dedupe_key, "status": p.status, "pinned": p.pinned,
            "manual_score": p.manual_score, "manual_verdict": p.manual_verdict,
            "analyst_note": p.analyst_note, "profile_id": p.profile_id,
            "score": p.cached_score or 0, "verdict": p.cached_verdict or "ok",
            "verdict_label": engine.VERDICT_LABELS.get(p.cached_verdict or "ok", ""),
            "types": [t for t in (p.cached_types or "").split(",") if t],
            "relevant": bool(p.cached_relevant),
            "relevance": analysis.get("relevance", 0),
            "analysis": analysis,
        })

    profiles = [{
        "id": pr.id, "codename": pr.codename, "real_name": pr.real_name,
        "known_aliases": pr.known_aliases,
        "social_links": [{"platform": s.platform, "username": s.username,
                          "url": s.url} for s in pr.social_links],
    } for pr in Profile.query.all()]

    graphs = [{"id": g.id, "title": g.title, "profile_id": g.profile_id,
               "graph_json": g.graph_json,
               "updated_at": g.updated_at.isoformat() if g.updated_at else ""}
              for g in Graph.query.all()]

    return jsonify({
        "format": "profiler-backup",
        "version": 1,
        "exported_at": datetime.utcnow().isoformat(timespec="seconds"),
        "data": {"watches": watches, "posts": posts, "profiles": profiles,
                 "graphs": graphs, "notes": [], "feeds": [], "settings": []},
        "counts": {"watches": len(watches), "posts": len(posts),
                   "profiles": len(profiles), "graphs": len(graphs)},
    })
