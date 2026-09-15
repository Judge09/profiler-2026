"""Build link-map graphs from a watch.

The previous version produced a star: the subject in the middle with one
account node per post. That shows who posted but not how anything connects,
which is the only reason to draw a graph at all.

This builds a real entity graph instead:

    subject ── account ── domain
                  │         │
               profile    location

Accounts link to the domains they push, domains to where they are hosted,
and accounts to any tracked Profile they correlate with. Shared infrastructure
then becomes visible as shared nodes -- two accounts pointing at one domain
are drawn as two edges into the same node, which is exactly the pattern an
analyst is looking for.

Node ids and shapes match what `linkmap.js` already understands, so graphs
open in the existing editor with no changes to it.
"""

import json
import re

from .. models import MonitorPost
from . import netintel, scoring

# Node types the link map already knows how to draw.
TYPE_PERSON = "person"
TYPE_ORG = "organization"
TYPE_ACCOUNT = "username"
TYPE_WEBSITE = "website"
TYPE_LOCATION = "location"
TYPE_NOTE = "note"

VERDICT_COLOR = {"bad": "#ff5252", "warn": "#ffd600", "ok": "#00e676"}


class GraphBuilder:
    """Accumulates nodes and edges, deduplicating by a stable key."""

    def __init__(self):
        self.nodes = []
        self.edges = []
        self._ids = {}
        self._edges_seen = set()
        self._next = 1

    def node(self, key, label, ntype, title="", **extra):
        """Add a node, or return the id of the one already standing for `key`."""
        if key in self._ids:
            nid = self._ids[key]
            # A later mention may carry a better title than the first one did.
            if title:
                for n in self.nodes:
                    if n["id"] == nid and not n.get("title"):
                        n["title"] = title
            return nid
        nid = self._next
        self._next += 1
        self._ids[key] = nid
        node = {"id": nid, "label": label[:60], "type": ntype, "title": title}
        node.update(extra)
        self.nodes.append(node)
        return nid

    def edge(self, a, b, label="", title="", **extra):
        if a is None or b is None or a == b:
            return
        key = (a, b, label)
        if key in self._edges_seen:
            return
        self._edges_seen.add(key)
        edge = {"from": a, "to": b, "label": label, "title": title}
        edge.update(extra)
        self.edges.append(edge)

    def payload(self):
        return {"nodes": self.nodes, "edges": self.edges}


def _record(p):
    """Normalise a MonitorPost row or a browser record into one shape.

    The browser is the system of record now, but stored rows still exist for
    the optional server sync. Both funnel through here so the graph looks the
    same whichever side the data came from.
    """
    if isinstance(p, dict):
        analysis = p.get("analysis") or {}
        score = p.get("manual_score")
        if score is None:
            score = p.get("score", analysis.get("score", 0))
        return {
            "id": p.get("id"),
            "platform": p.get("platform") or "",
            "author": p.get("author") or "",
            "handle": p.get("handle") or "",
            "text": p.get("text") or "",
            "url": p.get("url") or "",
            "score": score or 0,
            "verdict": (p.get("manual_verdict") or p.get("verdict")
                        or analysis.get("verdict") or "ok"),
            "types": p.get("types") or analysis.get("types") or [],
            "profile_id": p.get("profile_id"),
            "profile_codename": p.get("profile_codename"),
            "matches": analysis.get("profile_matches") or [],
            "kind": p.get("kind") or "post",
            "parent_url": p.get("parent_url") or "",
            "parent_author": p.get("parent_author") or "",
        }
    analysis = scoring.analysis_of(p) or {}
    score = p.manual_score if p.manual_score is not None else (
        p.cached_score or analysis.get("score") or 0)
    return {
        "id": p.id,
        "platform": p.platform or "",
        "author": p.author or "",
        "handle": p.handle or "",
        "text": p.text or "",
        "url": p.url or "",
        "score": score,
        "verdict": (p.manual_verdict or p.cached_verdict
                    or analysis.get("verdict") or "ok"),
        "types": ([t for t in (p.cached_types or "").split(",") if t]
                  or analysis.get("types") or []),
        "profile_id": p.profile_id,
        "profile_codename": (p.linked_profile.codename
                             if p.linked_profile else None),
        "matches": analysis.get("profile_matches") or [],
        "kind": getattr(p, "kind", "") or "post",
        "parent_url": getattr(p, "parent_url", "") or "",
        "parent_author": getattr(p, "parent_author", "") or "",
    }


def build(watch, min_score=30, include_domains=True, include_profiles=True,
          include_geo=False, max_posts=400, verdicts=("bad", "warn")):
    """Construct the graph for a stored watch."""
    posts = MonitorPost.query.filter(
        MonitorPost.watch_id == watch.id).order_by(
        MonitorPost.cached_score.desc()).limit(max_posts).all()
    return build_from_records(
        {"subject": watch.subject, "name": watch.name}, posts,
        min_score=min_score, include_domains=include_domains,
        include_profiles=include_profiles, include_geo=include_geo,
        verdicts=verdicts)


def build_from_records(watch, posts, min_score=30, include_domains=True,
                       include_profiles=True, include_geo=False,
                       verdicts=("bad", "warn"), profiles=None):
    """The graph core, over post records from either store.

    Returns (payload, stats). Only posts at or above `min_score` with a matching
    verdict are drawn, so the map stays readable.
    """
    get = (watch.get if isinstance(watch, dict)
           else (lambda k, d=None: getattr(watch, k, d)))
    g = GraphBuilder()
    stats = {"posts": 0, "comments": 0, "accounts": 0, "domains": 0,
             "profiles": 0, "locations": 0, "skipped": 0}

    subject_label = get("subject") or get("name") or "Subject"
    root = g.node("subject", subject_label, TYPE_ORG,
                  "Watch subject - %s" % (get("name") or ""))
    # The thread being commented on is very often the subject's own page -- you
    # watch NAMFREL and read NAMFREL's posts. Drawn as a separate node it
    # becomes two "NAMFREL" circles with the commenters hanging off the wrong
    # one, so the name is remembered here and reused as the root instead.
    subject_norm = str(subject_label).strip().lower()

    known = {pr.get("id"): pr.get("codename") for pr in (profiles or [])}

    for raw in posts:
        p = _record(raw)
        score, verdict = p["score"], p["verdict"]
        if score < min_score or (verdicts and verdict not in verdicts):
            stats["skipped"] += 1
            continue
        stats["posts"] += 1
        if p.get("kind") == "comment":
            stats["comments"] += 1
        types = p["types"]

        # -- the account -------------------------------------------------
        handle = p["handle"].strip().lstrip("@")
        akey = "acct:%s:%s" % (p["platform"].lower(),
                               (handle or p["author"]).lower())
        label = ("@" + handle) if handle else (p["author"] or "unknown")
        is_comment = p.get("kind") == "comment"
        anode = g.node(
            akey, label, TYPE_ACCOUNT,
            "%s - risk %d/100 - %s%s" % (
                p["platform"] or "?", score,
                ", ".join(types) or "no threat type",
                " (commenter)" if is_comment else ""),
            color=VERDICT_COLOR.get(verdict),
            score=score, verdict=verdict, platform=p["platform"],
            post_id=p["id"], url=p["url"], kind=p.get("kind") or "post")

        # A commenter is attached to the account it replied to, not straight to
        # the subject. That is the whole point of collecting comments: a dozen
        # fresh accounts converging on one post is a pattern, and it is only
        # visible when they all draw edges into that same post's author.
        if is_comment and p.get("parent_author"):
            author_norm = p["parent_author"].strip().lower()
            if author_norm and author_norm == subject_norm:
                # The thread is the subject's own; commenters attach straight
                # to it rather than to a duplicate of it.
                parent = root
            else:
                pkey = "acct:%s:%s" % (p["platform"].lower(), author_norm)
                parent = g.node(pkey, p["parent_author"][:60], TYPE_ACCOUNT,
                                "Posted the thread these comments reply to",
                                url=p.get("parent_url") or "")
                g.edge(root, parent, "thread", "carries the collected comments")
            g.edge(anode, parent, "commented on",
                   ", ".join(types) or "replied in the thread",
                   dashes=True, color=VERDICT_COLOR.get(verdict))
        else:
            g.edge(root, anode, verdict_label(verdict),
                   ", ".join(types) or "mentions the subject",
                   color=VERDICT_COLOR.get(verdict))

        # -- domains it pushes -------------------------------------------
        if include_domains:
            ind = netintel.extract_indicators(p["text"], p["url"])
            for u in ind["urls"][:4]:
                host = u["host"]
                if not host:
                    continue
                dkey = "dom:" + u["domain"]
                risky = bool(re.search(r"\.(xyz|top|click|icu|buzz|tk|ml|ga|cf)$",
                                       host, re.I))
                dnode = g.node(dkey, u["domain"], TYPE_WEBSITE,
                               "Domain linked from collected posts",
                               color="#ff5252" if risky else None)
                g.edge(anode, dnode, "links to", host)

                if include_geo:
                    geo = netintel.geolocate(host)
                    if geo.get("ok") and geo.get("country"):
                        lkey = "loc:" + geo["country"]
                        lnode = g.node(
                            lkey, geo["country"], TYPE_LOCATION,
                            "%s%s" % (geo.get("isp") or "",
                                      " (datacenter)" if geo.get("hosting") else ""))
                        g.edge(dnode, lnode, "hosted in",
                               geo.get("city") or geo["country"])

        # -- correlated profiles -----------------------------------------
        if include_profiles:
            if p["profile_id"]:
                codename = (p["profile_codename"] or known.get(p["profile_id"])
                            or ("profile %s" % p["profile_id"]))
                pkey = "prof:%s" % p["profile_id"]
                pnode = g.node(pkey, codename, TYPE_PERSON, "Linked profile",
                               profile_id=p["profile_id"])
                g.edge(anode, pnode, "linked to", "Analyst-confirmed link")
            for m in p["matches"][:2]:
                pkey = "prof:%s" % m["profile_id"]
                pnode = g.node(pkey, m["codename"], TYPE_PERSON,
                               "Suggested match - %d%% via %s"
                               % (round(m["confidence"] * 100), m["via"]),
                               profile_id=m["profile_id"])
                g.edge(anode, pnode, "possible match",
                       '%s "%s"' % (m["kind"], m["matched"]),
                       dashes=True)

    # Count unique nodes rather than mentions.
    stats["accounts"] = sum(1 for n in g.nodes if n["type"] == TYPE_ACCOUNT)
    stats["domains"] = sum(1 for n in g.nodes if n["type"] == TYPE_WEBSITE)
    stats["profiles"] = sum(1 for n in g.nodes if n["type"] == TYPE_PERSON)
    stats["locations"] = sum(1 for n in g.nodes if n["type"] == TYPE_LOCATION)
    stats["nodes"] = len(g.nodes)
    stats["edges"] = len(g.edges)
    return g.payload(), stats


def verdict_label(v):
    return {"bad": "high risk", "warn": "needs review", "ok": "clear"}.get(v, v)


def merge(existing_json, addition):
    """Merge a new graph into an existing one without duplicating nodes.

    Nodes are matched on label plus type, which is what a human would call
    "the same entity". Ids in the addition are remapped onto the existing
    graph's numbering so nothing collides.
    """
    try:
        base = json.loads(existing_json or '{"nodes":[],"edges":[]}')
    except (ValueError, TypeError):
        base = {"nodes": [], "edges": []}
    base.setdefault("nodes", [])
    base.setdefault("edges", [])

    index = {(str(n.get("label", "")).lower(), n.get("type")): n.get("id")
             for n in base["nodes"]}
    next_id = max([int(n.get("id", 0)) for n in base["nodes"]] or [0]) + 1

    remap = {}
    added_nodes = 0
    for n in addition.get("nodes", []):
        key = (str(n.get("label", "")).lower(), n.get("type"))
        if key in index:
            remap[n["id"]] = index[key]
            continue
        new = dict(n, id=next_id)
        remap[n["id"]] = next_id
        index[key] = next_id
        next_id += 1
        base["nodes"].append(new)
        added_nodes += 1

    seen = {(e.get("from"), e.get("to"), e.get("label"))
            for e in base["edges"]}
    added_edges = 0
    for e in addition.get("edges", []):
        a, b = remap.get(e.get("from")), remap.get(e.get("to"))
        if a is None or b is None:
            continue
        key = (a, b, e.get("label"))
        if key in seen:
            continue
        seen.add(key)
        base["edges"].append(dict(e, **{"from": a, "to": b}))
        added_edges += 1

    return base, {"nodes_added": added_nodes, "edges_added": added_edges,
                  "nodes_total": len(base["nodes"]),
                  "edges_total": len(base["edges"])}
