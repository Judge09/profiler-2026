"""Exercise the stateless API that the browser store talks to.

These endpoints hold no state: they take a watch definition and posts as JSON,
score or fetch, and hand the result straight back. Some tests reach the live
internet (collection), so they are slower than tests/test_monitor.py.

Run: python tests/test_api.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json
import time

from app import create_app

app = create_app()
app.config["TESTING"] = True
c = app.test_client()
with c.session_transaction() as s:
    s["authed"] = True

FAIL = []


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (" :: " + detail if detail else ""))
    if not cond:
        FAIL.append(name)


WATCH = {
    "id": 1, "name": "BARMM watch", "subject": "NAMFREL",
    "kw_required": "BARMM\nNAMFREL", "kw_optional": "election",
    "kw_excluded": "cricket", "kw_match_mode": "any",
    "mode_release": False, "mode_hunter": False,
    "threshold_review": 30, "threshold_high": 60, "weights": {},
}

print("\n=== /api/keywords ===")
r = c.post("/monitor/api/keywords", json=dict(WATCH, sample="BARMM election result"))
d = r.get_json()
check("resolves buckets", d["required"] == ["BARMM", "NAMFREL"] and d["excluded"] == ["cricket"],
      "req=%s exc=%s" % (d["required"], d["excluded"]))
check("builds query", "-cricket" in d["query"], d["query"])
check("evaluates sample", d["sample_result"]["relevant"] is True)

r = c.post("/monitor/api/keywords", json=dict(WATCH, sample="BARMM cricket match"))
check("sample exclusion works", r.get_json()["sample_result"]["relevant"] is False)

print("\n=== /api/score ===")
posts = [
    {"author": "News", "platform": "News site", "text": "BARMM election proceeds calmly"},
    {"author": "Fake NAMFREL", "platform": "X", "handle": "namfrel_ph",
     "text": "FREE BARMM voucher! Claim now http://namfrel-claim.xyz/login, send GCash 09171234567"},
    {"author": "Off", "platform": "X", "text": "Cricket scores from Mumbai"},
]
r = c.post("/monitor/api/score", json={"watch": WATCH, "posts": posts})
d = r.get_json()
check("scores a batch", len(d["results"]) == 3, "n=%d" % len(d["results"]))
check("clean post low", d["results"][0]["score"] < 40, "score=%d" % d["results"][0]["score"])
check("scam post high", d["results"][1]["score"] >= 60,
      "score=%d types=%s" % (d["results"][1]["score"], d["results"][1]["types"]))
check("off-topic flagged irrelevant", d["results"][2]["relevant"] is False)
check("returns rules_hash", bool(d.get("rules_hash")), d.get("rules_hash"))
check("flattened fields present",
      all(k in d["results"][0] for k in ("score", "verdict", "types", "relevance", "analysis")))

r = c.post("/monitor/api/score", json={"watch": WATCH, "posts": [{}] * 501})
check("batch cap enforced", r.status_code == 400, str(r.get_json())[:60])

print("\n=== /api/score with hunter profiles ===")
hw = dict(WATCH, mode_hunter=True, profiles=[
    {"id": 7, "codename": "GHOSTWIRE", "real_name": "Juan Dela Cruz",
     "known_aliases": ["jdc"], "social_links": [{"platform": "X", "username": "ghostwire"}]}])
r = c.post("/monitor/api/score", json={"watch": hw, "posts": [
    {"author": "ghostwire", "handle": "ghostwire", "platform": "X",
     "text": "BARMM post from a tracked account"}]})
d = r.get_json()["results"][0]
check("hunter correlates profile", len(d["analysis"].get("profile_matches") or []) > 0,
      str(d["analysis"].get("profile_matches"))[:110])

print("\n=== /api/collect (live) ===")
t = time.time()
r = c.post("/monitor/api/collect", json={
    "watch": WATCH, "sources": ["google_news", "wikipedia"],
    "options": {"limit": 5}, "days": 30, "strict": True, "known_keys": []})
d = r.get_json()
el = time.time() - t
check("collects and scores", r.status_code == 200, "%d posts in %.1fs" % (d.get("added", 0), el))
check("posts carry scores", all("score" in p and "dedupe_key" in p for p in d["posts"]),
      "sample keys ok" if d["posts"] else "no posts")
check("report per source", len(d["report"]["sources"]) == 2,
      str([(s["source"], s["count"]) for s in d["report"]["sources"]]))

if d["posts"]:
    keys = [p["dedupe_key"] for p in d["posts"]]
    r2 = c.post("/monitor/api/collect", json={
        "watch": WATCH, "sources": ["google_news"], "options": {"limit": 5},
        "days": 30, "known_keys": keys})
    d2 = r2.get_json()
    check("known_keys dedupes", d2["skipped"] > 0 or d2["added"] == 0,
          "added=%d skipped=%d" % (d2["added"], d2["skipped"]))

print("\n=== /api/dashboard ===")
recs = [dict(p, id=i + 1, watch_id=1, status="new",
             posted_ts="2026-09-%02dT10:00:00" % ((i % 27) + 1))
        for i, p in enumerate(d["posts"][:20])]
r = c.post("/monitor/api/dashboard", json={
    "watches": [WATCH], "posts": recs, "days": 30})
dd = r.get_json()
check("aggregates totals", dd["post_total"] == len(recs),
      "total=%d %s" % (dd["post_total"], dd["totals"]))
check("timeline built", isinstance(dd["timeline"], list), "%d days" % len(dd["timeline"]))
check("watch rows", len(dd["watches"]) == 1 and dd["watches"][0]["total"] == len(recs))
check("storage says browser", dd["storage"]["mode"] == "browser", str(dd["storage"]))

print("\n=== /api/briefing ===")
r = c.post("/monitor/api/briefing", json={"watch": WATCH, "posts": recs})
b = r.get_json()
check("briefing builds", r.status_code == 200 and b["total"] == len(recs),
      "headline=%s" % b.get("headline"))
check("has gaps analysis", isinstance(b.get("gaps"), list))

print("\n=== /api/graph + /api/network ===")
graph_posts = [
    {"id": 1, "platform": "X", "author": "A", "handle": "a1",
     "text": "see http://evil.xyz/login", "score": 70, "verdict": "bad", "types": ["Phishing"]},
    {"id": 2, "platform": "X", "author": "B", "handle": "b1",
     "text": "also http://evil.xyz/claim", "score": 65, "verdict": "bad", "types": ["Scam"]},
]
r = c.post("/monitor/api/graph", json={"watch": WATCH, "posts": graph_posts, "min_score": 30})
g = r.get_json()
check("graph builds", g["stats"]["nodes"] == 4, str(g["stats"]))
check("shared domain merges", g["stats"]["domains"] == 1, "domains=%d" % g["stats"]["domains"])

r = c.post("/monitor/api/graph", json={
    "watch": WATCH, "posts": graph_posts, "min_score": 30,
    "existing": json.dumps(g["graph"])})
check("merge dedupes", r.get_json()["merged"]["nodes_added"] == 0,
      str(r.get_json()["merged"]))

r = c.post("/monitor/api/network", json={"watch": WATCH, "posts": graph_posts})
n = r.get_json()
check("network ranks", n["available"] and len(n["nodes"]) == 3,
      "available=%s nodes=%d" % (n["available"], len(n["nodes"])))

print("\n=== /api/enrich ===")
r = c.post("/monitor/api/enrich", json={"posts": [
    {"id": 1, "text": "claim at http://barmm-verify.xyz/account/login or 192.168.1.5"}],
    "geo": False, "phishing": True})
e = r.get_json()["results"][0]["findings"]
check("enrich finds phishing", len(e["phishing"]) > 0,
      "verdict=%s" % (e["phishing"][0]["verdict"] if e["phishing"] else "none"))
check("enrich flags private ip", any("private" in f for f in e["flags"]), str(e["flags"])[:80])

print("\n=== /api/sync ===")
backup = {"format": "profiler-backup", "version": 1,
          "data": {"watches": [WATCH], "posts": recs[:5]}}
r = c.post("/monitor/api/sync/push", json={"payload": backup, "replace": True})
check("sync push", r.status_code == 200, str(r.get_json().get("counts")))

r = c.get("/monitor/api/sync/pull")
pulled = r.get_json()
check("sync pull round-trips", pulled["counts"]["watches"] == 1 and pulled["counts"]["posts"] == 5,
      str(pulled["counts"]))
check("pulled shape matches store", all(
    k in pulled["data"]["posts"][0] for k in ("score", "verdict", "types", "dedupe_key")))

print("\n" + "=" * 62)
print("FAILURES: %d" % len(FAIL))
for f in FAIL:
    print("  -", f)
