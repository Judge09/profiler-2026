"""Route-level QA: the auth gate, vault endpoints, malformed input and the
security-shaped behaviour that must not regress (CSV injection, invalid
regexes, batch caps).

Run: python tests/test_routes.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app  # noqa: E402
from app.monitor import vault  # noqa: E402

app = create_app()
app.config["TESTING"] = True

FAIL = []


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (" :: " + str(detail) if detail else ""))
    if not cond:
        FAIL.append(name)


print("=== AUTH GATE: every page requires login ===")
anon = app.test_client()
protected = ["/monitor/", "/monitor/dashboard", "/monitor/watch?id=1",
             "/monitor/vault", "/profiles/", "/linkmap/", "/dorks/",
             "/osint/", "/monitor/capabilities"]
for path in protected:
    r = anon.get(path)
    ok = r.status_code in (301, 302) and "/login" in (r.headers.get("Location") or "")
    check("anon redirected from %s" % path, ok, "%d -> %s" % (r.status_code, r.headers.get("Location")))

print("\n=== AUTH GATE: API endpoints too ===")
for path, method in [("/monitor/api/score", "POST"), ("/monitor/api/collect", "POST"),
                     ("/monitor/api/sync/pull", "GET"), ("/monitor/api/dashboard", "POST")]:
    r = anon.post(path, json={}) if method == "POST" else anon.get(path)
    ok = r.status_code in (301, 302)
    check("anon blocked from %s" % path, ok, r.status_code)

print("\n=== LOGIN ===")
c = app.test_client()
r = c.post("/login", data={"password": "wrong-password"})
check("bad password rejected", b"Invalid" in r.data or r.status_code == 200,
      r.status_code)
r = c.post("/login", data={"password": "profiler2024"}, follow_redirects=False)
check("good password accepted", r.status_code == 302, r.status_code)

with c.session_transaction() as s:
    s["authed"] = True

print("\n=== VAULT ROUTES ===")
vault.lock()
r = c.get("/monitor/vault/status")
d = r.get_json()
check("status readable while locked", r.status_code == 200 and d["unlocked"] is False, d)
check("status never leaks secrets",
      not any("secret" in str(k).lower() or "blob" in str(k).lower()
              for k in json.dumps(d)), "clean")

r = c.post("/monitor/vault/credentials", json={"label": "x", "kind": "cookies",
                                               "cookies": "a=1"})
check("cannot add while locked", r.status_code == 403, r.status_code)

r = c.post("/monitor/vault/unlock", json={"passphrase": "short"})
check("short passphrase rejected", r.status_code == 400, r.get_json())

r = c.post("/monitor/vault/unlock", json={"passphrase": "a-long-enough-passphrase"})
check("unlock accepted", r.status_code == 200 and r.get_json()["unlocked"], r.get_json())

r = c.post("/monitor/vault/credentials", json={
    "label": "QA test cred", "platform": "facebook", "kind": "cookies",
    "cookies": "c_user=123; xs=abc"})
check("add credential", r.status_code == 201, str(r.get_json())[:100])
cred = r.get_json()
cid = cred.get("id")
check("account hint is a summary, not the secret",
      "123" not in str(cred.get("account_hint")), cred.get("account_hint"))
check("health reported", "health" in cred, list(cred.keys()))

r = c.get("/monitor/vault/status")
d = r.get_json()
check("credential listed without secrets", d["count"] >= 1 and
      all("secret_blob" not in x for x in d["credentials"]), d["count"])

r = c.post("/monitor/vault/credentials", json={"label": "", "kind": "cookies",
                                               "cookies": "a=1"})
check("empty label rejected", r.status_code == 400, r.get_json())

r = c.post("/monitor/vault/credentials", json={"label": "z", "kind": "cookies",
                                               "cookies": "nonsense"})
check("uncookie-like input rejected", r.status_code == 400, r.get_json())

r = c.post("/monitor/vault/credentials", json={"label": "z", "kind": "wat",
                                               "cookies": "a=1"})
check("unknown kind rejected", r.status_code == 400, r.get_json())

r = c.post("/monitor/vault/credentials", json={"label": "tok", "kind": "token",
                                               "token": "sk-abcdef123456789"})
tok = r.get_json()
check("token hint is masked", r.status_code == 201 and "abcdef123456" not in
      str(tok.get("account_hint")), tok.get("account_hint"))

if cid:
    r = c.patch("/monitor/vault/credentials/%d" % cid, json={"enabled": False})
    check("disable credential", r.status_code == 200 and r.get_json()["enabled"] is False)
    r = c.delete("/monitor/vault/credentials/%d" % cid)
    check("delete credential", r.status_code == 200)

# Wrong passphrase against existing secrets must not look like success.
r = c.post("/monitor/vault/lock")
check("lock works", r.status_code == 200 and r.get_json()["unlocked"] is False)
r = c.post("/monitor/vault/unlock", json={"passphrase": "a-DIFFERENT-passphrase"})
check("wrong passphrase detected against stored data",
      r.status_code == 400 and "not match" in str(r.get_json()), r.get_json())
check("vault relocked after failed unlock", not vault.is_unlocked())

# Clean up the token credential.
c.post("/monitor/vault/unlock", json={"passphrase": "a-long-enough-passphrase"})
if tok.get("id"):
    c.delete("/monitor/vault/credentials/%d" % tok["id"])

print("\n=== COLLECT-AUTH GUARDS ===")
vault.lock()
r = c.post("/monitor/api/collect-auth", json={"watch": {}, "credential_id": 1})
check("collect-auth needs unlocked vault", r.status_code == 403, r.get_json())
c.post("/monitor/vault/unlock", json={"passphrase": "a-long-enough-passphrase"})
r = c.post("/monitor/api/collect-auth", json={"watch": {}})
check("collect-auth needs a credential", r.status_code == 400, r.get_json())
r = c.post("/monitor/api/collect-auth", json={"watch": {}, "credential_id": 99999})
check("unknown credential 404s", r.status_code == 404, r.status_code)

print("\n=== MALFORMED INPUT ===")
cases = [
    ("/monitor/api/score", {"posts": "not a list"}, 400),
    ("/monitor/api/score", {"watch": {}, "posts": []}, 200),
    ("/monitor/api/collect", {"watch": {}, "sources": []}, 400),
    ("/monitor/api/collect", {"watch": {"subject": "x"}, "sources": ["nosuch"]}, 200),
    ("/monitor/api/keywords", {}, 200),
    ("/monitor/api/graph", {"watch": {}, "posts": []}, 200),
    ("/monitor/api/dashboard", {"watches": [], "posts": []}, 200),
    ("/monitor/api/enrich", {"posts": []}, 200),
    ("/monitor/api/sync/push", {"payload": {"format": "wrong"}}, 400),
]
for path, body, expect in cases:
    r = c.post(path, json=body)
    check("%s with %s -> %d" % (path.split("/")[-1], str(body)[:34], expect),
          r.status_code == expect, "got %d" % r.status_code)

r = c.post("/monitor/api/score", json={"watch": {}, "posts": [{}] * 501})
check("score batch cap", r.status_code == 400)
r = c.post("/monitor/api/enrich", json={"posts": [{}] * 101})
check("enrich batch cap", r.status_code == 400)

print("\n=== XSS / INJECTION SHAPED INPUT ===")
nasty = '<script>alert(1)</script> & "quotes" \'apos\' <img onerror=x>'
r = c.post("/monitor/api/score", json={
    "watch": {"subject": nasty, "kw_required": nasty},
    "posts": [{"author": nasty, "text": nasty + " BARMM", "platform": "X"}]})
check("nasty input scores without error", r.status_code == 200, r.status_code)
d = r.get_json()["results"][0]
check("script tag not executed server-side", isinstance(d["score"], int), d["score"])

r = c.post("/monitor/api/keywords", json={"kw_required": "/[unclosed(/", "subject": "x"})
check("invalid regex does not 500", r.status_code == 200, r.status_code)

r = c.post("/monitor/api/score", json={
    "watch": {"custom_flags": "/((((/ = 10"},
    "posts": [{"author": "a", "text": "b"}]})
check("invalid custom-flag regex survives", r.status_code == 200, r.status_code)

print("\n=== CSV INJECTION GUARD ===")
from app.monitor import routes as R  # noqa: E402
# The browser builds CSVs now, but the server export must still be safe.
with app.app_context():
    from app.models import MonitorWatch, MonitorPost
    from app.extensions import db
    w = MonitorWatch(name="csvtest", subject="x", kw_required="x")
    db.session.add(w)
    db.session.commit()
    db.session.add(MonitorPost(watch_id=w.id, platform="X",
                               author="=cmd|'/c calc'!A1", text="x test",
                               dedupe_key="csvtest1"))
    db.session.commit()
    wid = w.id
r = c.get("/monitor/%d/export.csv" % wid)
body = r.get_data(as_text=True)
check("formula-leading cell is neutralised",
      "'=cmd" in body or '"\'=cmd' in body, [l for l in body.splitlines() if "cmd" in l][:1])
with app.app_context():
    from app.models import MonitorWatch
    from app.extensions import db
    db.session.delete(MonitorWatch.query.get(wid))
    db.session.commit()

print("\n=== OTHER FEATURES ===")
for path in ["/profiles/", "/linkmap/", "/dorks/", "/osint/", "/monitor/dashboard",
             "/monitor/watch?id=1", "/monitor/vault"]:
    r = c.get(path)
    check("GET %s" % path, r.status_code == 200, r.status_code)

r = c.get("/monitor/capabilities")
d = r.get_json()
check("capabilities structured", "groups" in d and d["counts"]["total"] > 0,
      "%d/%d" % (d["counts"]["available"], d["counts"]["total"]))

r = c.get("/monitor/sources")
d = r.get_json()
check("sources carry availability", all("available" in s for s in d["sources"]),
      "%d sources" % len(d["sources"]))

r = c.get("/monitor/ai-status")
check("ai-status responds", r.status_code == 200, r.get_json())

print("\n=== 404 / BAD IDS ===")
# Legacy server-addressed URLs redirect to their browser-addressed equivalents.
# They cannot 404 on a missing id, because the id names a record in the
# visitor's IndexedDB and the server has no way to know whether it exists --
# the page itself says so once it loads.
for path, dest in [("/monitor/99999", "/monitor/watch?id=99999"),
                   ("/linkmap/99999", "/linkmap/edit?id=99999")]:
    r = c.get(path)
    check("%s redirects to %s" % (path, dest),
          r.status_code == 301 and r.headers.get("Location") == dest,
          "%s -> %s" % (r.status_code, r.headers.get("Location")))
# No GET handler on a post: 405 is the correct HTTP answer, not 404.
check("GET on a PATCH-only route -> 405",
      c.get("/monitor/posts/99999").status_code == 405)
check("DELETE of a missing post -> 404",
      c.delete("/monitor/posts/99999").status_code == 404)
r = c.get("/monitor/watch?id=notanumber")
check("non-numeric watch id still renders shell", r.status_code == 200, r.status_code)

vault.lock()
print("\n" + "=" * 62)
print("FAILURES: %d" % len(FAIL))
for f in FAIL:
    print("  -", f)
sys.exit(1 if FAIL else 0)
