"""Credential vault, cookie parsing, session handling and authfetch.

Two bugs these tests exist to keep fixed:

* A cookie with no domain -- which is exactly what pasting a plain `Cookie:`
  header produces -- used to be silently dropped, so every authenticated fetch
  failed with a misleading "the session is dead" message.
* Challenge detection used substring matching, so an article containing the
  word "suspended" or "challenge" was reported as a locked account while its
  real results were thrown away.

Run: python tests/test_auth.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

FAIL = []


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (" :: " + str(detail) if detail else ""))
    if not cond:
        FAIL.append(name)


from app.monitor import authfetch, vault  # noqa: E402

print("=== VAULT: lock/unlock lifecycle ===")
vault.lock()
check("starts locked", not vault.is_unlocked())
try:
    vault.encrypt({"a": 1})
    check("encrypt refuses while locked", False, "no exception raised")
except vault.VaultLocked:
    check("encrypt refuses while locked", True)
try:
    vault.decrypt("x")
    check("decrypt refuses while locked", False, "no exception raised")
except vault.VaultLocked:
    check("decrypt refuses while locked", True)

vault.unlock("correct horse battery staple")
check("unlock works", vault.is_unlocked())
blob = vault.encrypt({"cookies": [{"name": "sessionid", "value": "abc"}]})
check("encrypt produces ciphertext", isinstance(blob, str) and "sessionid" not in blob,
      "plaintext leaked!" if "sessionid" in blob else "opaque")
check("round-trips", vault.decrypt(blob)["cookies"][0]["value"] == "abc")

print("\n=== VAULT: wrong passphrase ===")
vault.lock()
vault.unlock("WRONG passphrase entirely")
check("wrong key returns None, not garbage", vault.decrypt(blob) is None)
check("verify() rejects wrong key", vault.verify(blob) is False)
vault.lock()
vault.unlock("correct horse battery staple")
check("right key still works after a wrong attempt", vault.decrypt(blob) is not None)

print("\n=== VAULT: lock actually clears the key ===")
vault.lock()
check("locked after lock()", not vault.is_unlocked())
try:
    vault.decrypt(blob)
    check("secret unreadable after lock", False, "decrypted while locked")
except vault.VaultLocked:
    check("secret unreadable after lock", True)

print("\n=== VAULT: tampering ===")
vault.unlock("correct horse battery staple")
tampered = blob[:-6] + "AAAAAA"
check("tampered ciphertext rejected", vault.decrypt(tampered) is None)
check("garbage rejected", vault.decrypt("not-even-base64!!") is None)
check("empty blob returns None", vault.decrypt("") is None)

print("\n=== VAULT: auto_unlock ===")
vault.lock()
os.environ["MONITOR_VAULT_KEY"] = "env-provided-passphrase"
check("auto_unlock from env", vault.auto_unlock() is True and vault.is_unlocked())
vault.lock()
os.environ.pop("MONITOR_VAULT_KEY", None)
check("auto_unlock no-ops without env", vault.auto_unlock() is False)

print("\n=== COOKIE PARSING ===")
vault.unlock("test-pass-phrase")

hdr = vault.parse_cookies("c_user=123; xs=abc%3Adef; datr=zzz")
check("header form", len(hdr) == 3 and hdr[0]["name"] == "c_user", hdr[0])
check("header preserves encoded values", hdr[1]["value"] == "abc%3Adef", hdr[1]["value"])

js = vault.parse_cookies(
    '[{"name":"sessionid","value":"v1","domain":".instagram.com","path":"/"},'
    ' {"name":"ds_user_id","value":"42"}]')
check("json export form", len(js) == 2 and js[0]["domain"] == ".instagram.com", js)

capitalised = vault.parse_cookies('[{"Name":"li_at","Value":"tok","Domain":".linkedin.com"}]')
check("json with capitalised keys", len(capitalised) == 1 and capitalised[0]["name"] == "li_at",
      capitalised)

netscape = vault.parse_cookies(
    "# Netscape HTTP Cookie File\n"
    ".reddit.com\tTRUE\t/\tTRUE\t1799999999\treddit_session\tsess-value\n")
check("netscape cookies.txt", len(netscape) == 1 and netscape[0]["name"] == "reddit_session",
      netscape)

check("empty input", vault.parse_cookies("") == [])
check("whitespace input", vault.parse_cookies("   \n  ") == [])
check("malformed json degrades to []", vault.parse_cookies('[{"name":') == [])
check("junk header yields nothing", vault.parse_cookies("no equals signs here") == [])

# A value containing '=' (base64 padding) must survive.
eq = vault.parse_cookies("token=YWJjZGVm==; other=1")
check("value containing '=' kept whole", eq[0]["value"] == "YWJjZGVm==", eq[0]["value"])

print("\n=== HEALTH CHECKS ===")


class Cred:
    def __init__(self, **kw):
        self.platform = kw.get("platform", "generic")
        self.kind = kw.get("kind", "cookies")
        self.label = kw.get("label", "test")
        self.expires_at = kw.get("expires_at")
        self.last_error = kw.get("last_error", "")


h = vault.health(Cred(platform="facebook"), [{"name": "c_user"}])
check("missing cookie flagged", any("xs" in i for i in h["issues"]), h["issues"])

h = vault.health(Cred(platform="facebook"), [{"name": "c_user"}, {"name": "xs"}])
check("complete cookies clean", not h["issues"], h["issues"])

h = vault.health(Cred(platform="facebook", kind="password"))
check("password warns", any("password" in i.lower() for i in h["issues"]), h["issues"])

h = vault.health(Cred(platform="x", expires_at="2020-01-01T00:00:00"))
check("expired flagged", any("expiry" in i for i in h["issues"]), h["issues"])

h = vault.health(Cred(platform="x", expires_at="not a date"))
check("bad expiry does not crash", isinstance(h["issues"], list))

h = vault.health(Cred(platform="reddit", last_error="429 Too Many Requests"))
check("last error surfaced", any("429" in i for i in h["issues"]), h["issues"])

check("reliability reported", vault.guidance("linkedin")["reliability"] == "low")
check("unknown platform falls back", vault.guidance("wat")["name"] == "Other site")

print("\n=== AUTHFETCH: challenge detection (true positives) ===")
real = [
    ("https://m.facebook.com/checkpoint/1234", "<html>ok</html>", "checkpoint path"),
    ("https://www.instagram.com/accounts/login/?next=/", "<html></html>", "login redirect"),
    ("https://x.com/login", "<html></html>", "bare login path"),
    ("https://site.com/feed", "<div>Please confirm your identity</div>", "identity check"),
    ("https://site.com/feed", "<div>You are temporarily blocked</div>", "temp block"),
    ("https://site.com/f", "<p>Your account has been suspended your account</p>", "suspension"),
    ("https://site.com/f", "<p>unusual traffic from your computer</p>", "bot detection"),
    ("https://site.com/f", "<p>Please enable JavaScript and cookies to continue</p>", "interstitial"),
    ("https://site.com/f", "<p>Log in to continue</p>", "login wall"),
    ("https://site.com/f", "<p>Complete the security check</p>", "security check"),
]
for url, body, label in real:
    got = authfetch._detect_challenge(url, body)
    check("detects %s" % label, bool(got), got)

print("\n=== AUTHFETCH: challenge detection (no false positives) ===")
# Ordinary content that merely uses the words. Flagging any of these would
# discard real results and send an analyst chasing an account problem that is
# not there.
benign = [
    ("https://old.reddit.com/search?q=login+page+design",
     "<div>Discussion about login page design</div>", "thread about logins"),
    ("https://news.site/article",
     "<p>The company suspended operations in March.</p>", "article: suspended"),
    ("https://news.site/a",
     "<p>Officials face a challenge in the coming months.</p>", "article: challenge"),
    ("https://site.com/feed", "<p>Our captcha-free signup is live</p>", "captcha-free"),
    ("https://old.reddit.com/search?q=captcha",
     "<div class='thing' data-author='bob'>captcha research</div>", "search for captcha"),
    ("https://news.site/x",
     "<p>The election was challenged in court.</p>", "article: challenged"),
]
for url, body, label in benign:
    got = authfetch._detect_challenge(url, body)
    check("ignores %s" % label, got is None, got)

print("\n=== AUTHFETCH: cookie -> session ===")
s = authfetch._session_for([
    {"name": "a", "value": "1", "domain": ".example.com", "path": "/"},
    {"name": "b", "value": "2"},                 # no domain key at all
    {"name": "c", "value": "3", "domain": ""},   # empty domain
    {"name": "", "value": "skipme"},
    {"value": "no name"},
], "https://mbasic.facebook.com/search/posts/?q=x")
names = {c.name for c in s.cookies}
check("domainless cookies are kept", {"a", "b", "c"} <= names, names)
check("nameless cookies skipped", "" not in names and len(names) == 3, names)
check("session carries browser headers", "User-Agent" in s.headers)

# The fallback domain must be the target host, not a wildcard: a session cookie
# sent to every host would leak the account to unrelated sites.
by_name = {c.name: c for c in s.cookies}
check("domainless cookie scoped to target host",
      "facebook.com" in by_name["b"].domain, by_name["b"].domain)
check("explicit domain preserved", by_name["a"].domain == ".example.com",
      by_name["a"].domain)

# With no target URL there is nothing to scope to, but the cookie must still
# not be dropped and must not raise.
s2 = authfetch._session_for([{"name": "x", "value": "1"}])
check("no target url still attaches", "x" in {c.name for c in s2.cookies},
      [c.name for c in s2.cookies])

s3 = authfetch._session_for(
    [{"name": "ok", "value": "1"}, {"name": "bad", "value": object()}],
    "https://example.com")
check("one malformed cookie does not kill the session",
      "ok" in {c.name for c in s3.cookies})

print("\n=== AUTHFETCH: extraction ===")
reddit_html = '''
<div class="thing link" data-author="alice" data-permalink="/r/x/comments/1/a/">
  <a class="title may-blank" href="#">BARMM election monitoring begins</a></div>
<div class="thing link" data-author="bob" data-permalink="/r/x/comments/2/b/">
  <a class="title may-blank" href="#">Second headline here</a></div>'''
posts = authfetch._extract_old_reddit(reddit_html)
check("old.reddit extraction", len(posts) == 2 and posts[0]["author"] == "/u/alice", posts[:1])
check("permalink absolute", posts[0]["url"].startswith("https://old.reddit.com/r/"),
      posts[0]["url"])

generic = authfetch._extract_generic(
    "<p>" + ("long enough block of text " * 5) + "</p><p>short</p><p>" +
    ("another sufficiently long block " * 4) + "</p>", "https://example.com/x")
check("generic extraction keeps long blocks", len(generic) == 2, len(generic))
check("generic drops short blocks", all(len(p["text"]) >= 80 for p in generic))

dupes = authfetch._extract_generic(
    ("<p>" + ("repeated identical block of text " * 4) + "</p>") * 3,
    "https://example.com/x")
check("generic deduplicates", len(dupes) == 1, len(dupes))

print("\n=== AUTHFETCH: no endpoint / bad platform ===")
r = authfetch.fetch_with_cookies("nosuchplatform", "query", [])
check("unknown platform reports clearly", not r["ok"] and "endpoint" in r["note"].lower(),
      r["note"])

print("\n=== AUTHFETCH: playwright detection ===")
check("playwright_available returns a bool", isinstance(authfetch.playwright_available(), bool),
      authfetch.playwright_available())

print("\n=== AUTHFETCH: live request to a benign endpoint ===")
r = authfetch.fetch_with_cookies("generic", "", [], limit=3,
                                 url="https://example.com/")
check("http strategy runs end to end", isinstance(r, dict) and "ok" in r,
      "ok=%s note=%s" % (r["ok"], r["note"][:70]))

print("\n=== AUTHFETCH: collect_authenticated shape ===")


class C:
    platform = "generic"
    label = "test-cred"


r = authfetch.collect_authenticated(C(), [], "test", limit=2,
                                    url="https://example.com/")
for k in ("ok", "posts", "note", "blocked", "manual_url", "used_credential", "strategy"):
    check("result carries %s" % k, k in r)
check("credential label echoed", r["used_credential"] == "test-cred")

vault.lock()
print("\n" + "=" * 62)
print("FAILURES: %d" % len(FAIL))
for f in FAIL:
    print("  -", f)
sys.exit(1 if FAIL else 0)
