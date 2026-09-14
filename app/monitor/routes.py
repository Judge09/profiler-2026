"""Signal Monitor routes.

A watch is a saved monitoring case. Posts are collected into it (live or by
hand), scored by the engine, and optionally correlated back to Profiles.
"""

import csv
import hashlib
import io
import json
import os
import re
from datetime import datetime, timedelta

import requests
from flask import (Blueprint, Response, jsonify, redirect, render_template,
                   request, url_for)

from ..auth.routes import login_required
from ..extensions import db
from ..models import (Graph, IntelNote, MonitorCredential, MonitorFeed,
                      MonitorPost, MonitorWatch, Profile)
from . import authfetch, collectors, engine, vault

monitor = Blueprint("monitor", __name__, url_prefix="/monitor")

STATUSES = ("new", "reviewed", "escalated", "dismissed")


# -- Helpers -----------------------------------------------------------------

def _dedupe_key(watch_id, platform, author, text, url=""):
    raw = "|".join([str(watch_id), (platform or "").lower(), (author or "").lower(),
                    (url or "").lower(), (text or "").strip().lower()[:400]])
    return hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()


def _watch_config(watch):
    """Build the engine config, loading profiles only when Hunter mode is on."""
    profiles = Profile.query.all() if watch.mode_hunter else []
    cfg = engine.build_config(watch, profiles)
    return cfg


def _analyze_watch(watch):
    cfg = _watch_config(watch)
    results = {}
    for p in watch.posts:
        a = engine.analyze(p, cfg)
        # An analyst override replaces the computed score but keeps the evidence.
        if p.manual_score is not None:
            a["auto_score"] = a["score"]
            a["auto_verdict"] = a["verdict"]
            a["score"] = int(p.manual_score)
            a["verdict"] = (p.manual_verdict
                            or ("bad" if a["score"] >= cfg["threshold_high"]
                                else "warn" if a["score"] >= cfg["threshold_review"]
                                else "ok"))
            a["verdict_label"] = engine.VERDICT_LABELS.get(a["verdict"], a["verdict"])
            a["overridden"] = True
        elif p.manual_verdict:
            a["auto_verdict"] = a["verdict"]
            a["verdict"] = p.manual_verdict
            a["verdict_label"] = engine.VERDICT_LABELS.get(p.manual_verdict, p.manual_verdict)
            a["overridden"] = True
        else:
            a["overridden"] = False
        results[p.id] = a
    return cfg, results


def _add_posts(watch, raw_posts, source="manual", drop_off_topic=False):
    """Insert posts, skipping duplicates.

    Returns (added, skipped, off_topic). With `drop_off_topic`, posts that fail
    the watch's relevance test are rejected rather than stored -- collectors
    return whatever the search engine matched, which is broader than the topic.
    """
    existing = {p.dedupe_key for p in watch.posts}
    cfg = _watch_config(watch) if drop_off_topic else None
    added = skipped = off_topic = 0
    for raw in raw_posts:
        author = str(raw.get("author") or "").strip()
        text = str(raw.get("text") or "").strip()
        if not author or not text:
            skipped += 1
            continue
        if cfg is not None:
            probe = dict(raw, author=author, text=text,
                         platform=raw.get("platform") or "Other")
            if not engine.analyze(probe, cfg)["relevant"]:
                off_topic += 1
                continue
        platform = str(raw.get("platform") or "Other").strip()
        url = str(raw.get("url") or "").strip()
        key = _dedupe_key(watch.id, platform, author, text, url)
        if key in existing:
            skipped += 1
            continue
        existing.add(key)
        db.session.add(MonitorPost(
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
            posted_at=raw.get("posted_at"),
            dedupe_key=key,
        ))
        added += 1
    if added:
        watch.updated_at = datetime.utcnow()
    db.session.commit()
    return added, skipped, off_topic


def _quote(term):
    return '"%s"' % term if " " in term else term


def _query_for(watch):
    """Build a topic-scoped search string.

    A bare OR of every keyword matches anything mentioning any one of them,
    which is how US and Indian election coverage ended up in a BARMM watch.
    Anchors (the specific terms) are ORed with each other but ANDed against the
    generic terms, so a result must name something specific to this topic:

        (NAMFREL OR BARMM OR COMELEC OR Bangsamoro) AND (election OR parliamentary)

    Terms marked with a leading + are forced to be anchors. Otherwise anything
    not in engine.GENERIC_TERMS anchors, and generic words only broaden.
    """
    anchors, generic = _split_keywords(watch)
    if not anchors and not generic:
        return (watch.subject or watch.name or "").strip()
    if not anchors:
        # Only generic words given: nothing scopes the topic, so fall back to
        # the subject rather than searching for "election" worldwide.
        subject = (watch.subject or watch.name or "").strip()
        return ("%s %s" % (_quote(subject), " OR ".join(generic[:6]))).strip()

    core = " OR ".join(_quote(t) for t in anchors[:8])
    if len(anchors) > 1:
        core = "(%s)" % core
    if generic:
        group = " OR ".join(_quote(t) for t in generic[:6])
        return "%s AND (%s)" % (core, group)
    return core


def _split_keywords(watch):
    """Return (anchors, generic) keyword lists for a watch.

    Anchors identify the topic; generic terms merely describe its category.
    """
    raw = [k.strip() for k in (watch.keywords or "").split(",") if k.strip()]
    forced = [k.lstrip("+").strip() for k in raw if k.startswith("+")]
    rest = [k for k in raw if not k.startswith("+")]

    if forced:
        return forced, [k for k in rest if k.lstrip("+").strip() not in forced]

    anchors, generic = [], []
    subject = (watch.subject or "").strip()
    if subject:
        anchors.append(subject)
    for k in rest:
        if engine.is_generic_term(k):
            generic.append(k)
        elif k.lower() != subject.lower():
            anchors.append(k)
    return anchors, generic


# -- Watch CRUD --------------------------------------------------------------

@monitor.route("/")
@login_required
def list_watches():
    watches = MonitorWatch.query.order_by(MonitorWatch.updated_at.desc()).all()
    summary = []
    for w in watches:
        _, results = _analyze_watch(w)
        counts = {"bad": 0, "warn": 0, "ok": 0}
        for a in results.values():
            counts[a["verdict"]] += 1
        summary.append({"watch": w, "counts": counts, "total": len(results)})
    profiles = Profile.query.order_by(Profile.codename).all()
    return render_template("monitor/list.html", summary=summary, profiles=profiles)


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
    """
    days = max(1, min(180, int(request.args.get("days", 30))))
    cutoff = datetime.utcnow() - timedelta(days=days)

    watches = MonitorWatch.query.order_by(MonitorWatch.updated_at.desc()).all()
    totals = {"bad": 0, "warn": 0, "ok": 0}
    pending = 0
    escalated = 0
    by_day = {}
    by_platform = {}
    type_counts = {}
    recent_flagged = []
    watch_rows = []
    stale = []

    for w in watches:
        _, results = _analyze_watch(w)
        counts = {"bad": 0, "warn": 0, "ok": 0}
        w_pending = 0
        newest = None

        for p in w.posts:
            a = results[p.id]
            counts[a["verdict"]] += 1
            totals[a["verdict"]] += 1
            if a["verdict"] != "ok" and (p.status or "new") == "new":
                pending += 1
                w_pending += 1
            if p.status == "escalated":
                escalated += 1
            for t in a["types"]:
                type_counts[t] = type_counts.get(t, 0) + 1

            stamp = p.posted_at or (p.collected_at.isoformat() if p.collected_at else None)
            if stamp:
                day = str(stamp)[:10]
                if newest is None or str(stamp) > str(newest):
                    newest = stamp
                try:
                    when = datetime.fromisoformat(str(stamp)[:19])
                except ValueError:
                    when = None
                if when and when >= cutoff:
                    d = by_day.setdefault(day, {"date": day, "total": 0,
                                                "bad": 0, "warn": 0, "ok": 0})
                    d["total"] += 1
                    d[a["verdict"]] += 1

            plat = p.platform or "Other"
            ps = by_platform.setdefault(plat, {"platform": plat, "total": 0,
                                               "flagged": 0})
            ps["total"] += 1
            if a["verdict"] != "ok":
                ps["flagged"] += 1

            if a["verdict"] != "ok":
                recent_flagged.append({
                    "post_id": p.id, "watch_id": w.id, "watch": w.name,
                    "author": p.author, "platform": p.platform,
                    "score": a["score"], "verdict": a["verdict"],
                    "verdict_label": a["verdict_label"], "types": a["types"],
                    "status": p.status, "url": p.url,
                    "link_kind": p.link_kind or "direct",
                    "when": str(stamp or ""),
                    "excerpt": (p.text or "")[:180],
                })

        watch_rows.append({
            "id": w.id, "name": w.name, "subject": w.subject,
            "mode_label": w.mode_label,
            "mode_release": w.mode_release, "mode_hunter": w.mode_hunter,
            "total": len(w.posts), "counts": counts, "pending": w_pending,
            "newest": str(newest or ""),
            "updated_at": w.updated_at.strftime("%Y-%m-%d %H:%M") if w.updated_at else "",
        })

        # A watch nobody has fed in a while is probably going stale.
        if w.posts and newest:
            try:
                if datetime.fromisoformat(str(newest)[:19]) < datetime.utcnow() - timedelta(days=7):
                    stale.append({"id": w.id, "name": w.name, "newest": str(newest)[:10]})
            except ValueError:
                pass
        elif not w.posts:
            stale.append({"id": w.id, "name": w.name, "newest": ""})

    recent_flagged.sort(key=lambda r: (r["when"] or ""), reverse=True)
    timeline = [by_day[k] for k in sorted(by_day)]

    # Source health: what the saved feeds last reported.
    feeds = MonitorFeed.query.order_by(MonitorFeed.last_run.desc()).all()
    sources = [{
        "source": f.source, "watch_id": f.watch_id,
        "enabled": f.enabled,
        "last_run": f.last_run.strftime("%Y-%m-%d %H:%M") if f.last_run else "",
        "last_note": f.last_note or "",
        # "Fetched 0" ran without erroring but returned nothing, which is a
        # problem worth surfacing rather than a green tick.
        "ok": ("Fetched" in (f.last_note or "")
               and not re.search(r"\b(Fetched|Extracted) 0\b", f.last_note or "")),
    } for f in feeds[:12]]

    creds = MonitorCredential.query.all()
    cred_summary = {
        "total": len(creds),
        "enabled": sum(1 for c in creds if c.enabled),
        "failing": sum(1 for c in creds if c.last_ok is False),
        "unlocked": vault.is_unlocked(),
    }

    return jsonify({
        "generated_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M"),
        "days": days,
        "watch_count": len(watches),
        "totals": totals,
        "post_total": sum(totals.values()),
        "pending": pending,
        "escalated": escalated,
        "timeline": timeline,
        "watches": watch_rows,
        "platforms": sorted(by_platform.values(), key=lambda p: -p["total"]),
        "types": sorted(({"type": k, "count": v} for k, v in type_counts.items()),
                        key=lambda x: -x["count"])[:10],
        "recent": recent_flagged[:12],
        "queue": sorted([r for r in recent_flagged if r["status"] == "new"],
                        key=lambda r: -r["score"])[:10],
        "sources": sources,
        "credentials": cred_summary,
        "stale": stale,
        "profiles": Profile.query.count(),
    })


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
        mode_release=bool(data.get("mode_release")),
        mode_hunter=bool(data.get("mode_hunter")),
        profile_id=int(data["profile_id"]) if data.get("profile_id") else None,
    )
    if w.mode_release and not w.reference_text:
        w.reference_text = (data.get("reference_text") or "").strip()
    db.session.add(w)
    db.session.commit()
    return jsonify(w.to_dict()), 201


@monitor.route("/<int:watch_id>")
@login_required
def watch_detail(watch_id):
    w = MonitorWatch.query.get_or_404(watch_id)
    cfg, results = _analyze_watch(w)
    posts = sorted(w.posts, key=lambda p: (not p.pinned, -results[p.id]["score"]))
    counts = {"bad": 0, "warn": 0, "ok": 0}
    for a in results.values():
        counts[a["verdict"]] += 1
    return render_template(
        "monitor/watch.html",
        w=w,
        posts=posts,
        results=results,
        counts=counts,
        profiles=Profile.query.order_by(Profile.codename).all(),
        feeds=MonitorFeed.query.filter_by(watch_id=w.id).all(),
        sources=collectors.SOURCE_META,
        weights=cfg["weights"],
        default_weights=engine.DEFAULT_WEIGHTS,
        statuses=STATUSES,
        suggested=collectors.load_sources().get("suggested_feeds", []),
    )


@monitor.route("/<int:watch_id>/update", methods=["POST"])
@login_required
def update_watch(watch_id):
    w = MonitorWatch.query.get_or_404(watch_id)
    data = request.json or {}
    for field in ("name", "subject", "keywords", "reference_text",
                  "official_accounts", "official_domains", "custom_flags"):
        if field in data:
            setattr(w, field, (data.get(field) or "").strip())
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
    """Rescore every post. Used after a settings or weight change."""
    w = MonitorWatch.query.get_or_404(watch_id)
    _, res = _analyze_watch(w)
    counts = {"bad": 0, "warn": 0, "ok": 0}
    for a in res.values():
        counts[a["verdict"]] += 1
    return jsonify({
        "counts": counts,
        "total": len(res),
        "posts": [dict(p.to_dict(), analysis=res[p.id]) for p in w.posts],
    })


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
    added, skipped, _off = _add_posts(w, [data], source="manual")
    if not added:
        return jsonify({"error": "Needs an author and post text, or it duplicates an existing post"}), 400
    return jsonify({"ok": True, "added": added, "skipped": skipped}), 201


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
    added, skipped, _off = _add_posts(w, arr, source="import")
    return jsonify({"ok": True, "added": added, "skipped": skipped})


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
    a = engine.analyze(p, cfg)
    return jsonify({"ok": True, "post": p.to_dict(), "analysis": a})


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
    raw_posts, report = collectors.collect(keys, query, options)
    added, skipped, off_topic = _add_posts(
        w, raw_posts, source="live",
        drop_off_topic=data.get("strict", True))

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

    return jsonify({"ok": True, "added": added, "skipped": skipped,
                    "off_topic": off_topic, "query": query, "report": report})


@monitor.route("/<int:watch_id>/topic")
@login_required
def topic_scope(watch_id):
    """Show how the current keywords resolve into a topic.

    Lets the analyst see which terms actually scope the watch before running a
    collection, instead of discovering the drift afterwards.
    """
    w = MonitorWatch.query.get_or_404(watch_id)
    # Allow previewing unsaved edits from the sidebar.
    probe = w
    if request.args.get("keywords") is not None:
        probe = MonitorWatch(
            name=w.name,
            subject=request.args.get("subject", w.subject or ""),
            keywords=request.args.get("keywords", ""),
        )
    anchors, generic = _split_keywords(probe)
    return jsonify({
        "anchors": anchors,
        "generic": generic,
        "query": _query_for(probe),
        "warning": ("No specific terms, so nothing scopes this watch to a topic."
                    if not anchors else ""),
    })


@monitor.route("/<int:watch_id>/dorks")
@login_required
def dorks(watch_id):
    w = MonitorWatch.query.get_or_404(watch_id)
    query = request.args.get("q", "").strip() or _query_for(w)
    return jsonify({"query": query, "urls": collectors.dork_urls(query)})


@monitor.route("/sources")
@login_required
def sources():
    return jsonify({"sources": collectors.SOURCE_META,
                    "suggested": collectors.load_sources().get("suggested_feeds", [])})


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
    """Build a link map from the watch: subject in the middle, accounts around it."""
    w = MonitorWatch.query.get_or_404(watch_id)
    data = request.json or {}
    min_score = int(data.get("min_score") or 30)
    _, results = _analyze_watch(w)

    nodes = [{"id": 1, "label": w.subject or w.name, "type": "person",
              "title": "Watch subject"}]
    edges = []
    nid = 2
    seen = {}
    for p in w.posts:
        a = results[p.id]
        if a["score"] < min_score:
            continue
        key = (p.platform or "").lower() + ":" + (p.handle or p.author or "").lower()
        if key in seen:
            continue
        label = ("@" + p.handle) if p.handle else p.author
        seen[key] = nid
        nodes.append({
            "id": nid,
            "label": label,
            "type": "account",
            "title": "%s - risk %d/100 - %s" % (p.platform, a["score"], a["verdict_label"]),
        })
        edges.append({"from": 1, "to": nid,
                      "label": a["verdict_label"],
                      "title": ", ".join(a["types"]) or "signal"})
        nid += 1

    graph = Graph(
        title=w.name + " - Signal Map",
        profile_id=w.profile_id,
        graph_json=json.dumps({"nodes": nodes, "edges": edges}),
    )
    db.session.add(graph)
    db.session.commit()
    return jsonify({"ok": True, "graph_id": graph.id,
                    "url": url_for("linkmap.edit_map", graph_id=graph.id),
                    "nodes": len(nodes)})


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


@monitor.route("/<int:watch_id>/collect-auth", methods=["POST"])
@login_required
def collect_authenticated_route(watch_id):
    """Collect into a watch using a stored credential."""
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

    added, skipped, off_topic = _add_posts(
        w, result["posts"], source="auth:" + c.platform,
        drop_off_topic=data.get("strict", True))
    db.session.commit()

    return jsonify({
        "ok": result["ok"], "added": added, "skipped": skipped,
        "off_topic": off_topic,
        "note": result["note"], "blocked": result["blocked"],
        "manual_url": result["manual_url"], "strategy": result.get("strategy"),
        "query": query,
    })


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
