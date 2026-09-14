"""Authenticated collection using vaulted credentials.

Two strategies, tried in order:

1. `requests` with the stored session cookies. Fast and dependency-free. Works
   on sites that render posts server-side (mbasic.facebook.com, old.reddit.com,
   most forums).
2. Playwright, when installed. Runs a real browser so JavaScript-rendered feeds
   load. Optional -- everything degrades gracefully without it.

Honest expectations: Meta and LinkedIn detect automation well, and a scripted
session on a throwaway account is typically challenged within hours or days.
When that happens the fetcher reports the challenge plainly rather than
returning empty results that look like "no posts found".
"""

import re
from urllib.parse import urlparse

import requests

from . import collectors
from .collectors import _clean, _host, _result

# Signals that we were challenged rather than simply finding nothing.
#
# These are split by where they may appear, because the two places carry very
# different weight. A word in the *URL* is the platform telling us where it sent
# us. The same word in the *body* is usually just an article that happens to use
# it -- a news story containing "suspended" or "challenge" is not a lockout, and
# treating it as one throws away real results while sending the analyst to chase
# an account problem that does not exist.
#
# So: URL markers match on path segments, and body markers must be whole
# challenge-page phrases, not bare words.

_URL_MARKERS = [
    ("checkpoint", "The account hit a Meta checkpoint and needs manual review."),
    ("login_attempt", "The session was rejected and a fresh login was demanded."),
    ("captcha", "A CAPTCHA page was served."),
    ("challenge", "The platform issued a challenge page."),
    ("denied", "The platform denied the request."),
]

# Paths that mean "you are not logged in", as whole segments so that a search
# for the word "login" does not trip them.
_LOGIN_PATHS = ("/login", "/log-in", "/signin", "/sign-in", "/accounts/login",
                "/auth/login", "/session/new", "/checkpoint")

_BODY_MARKERS = [
    (re.compile(r"\b(your account has been|we(?:'ve| have) )?(temporarily )?"
                r"(suspended|disabled|locked|restricted) your account\b", re.I),
     "The platform says the account is suspended or locked."),
    (re.compile(r"\bconfirm your identity\b", re.I),
     "Identity confirmation was requested."),
    (re.compile(r"\b(complete|solve) (the |this )?(security )?(check|captcha)\b", re.I),
     "A CAPTCHA or security check was served."),
    (re.compile(r"\byou(?:'re| are) temporarily blocked\b", re.I),
     "The account is temporarily blocked."),
    (re.compile(r"\b(log ?in|sign ?in) to continue\b", re.I),
     "The page demands a fresh login -- the session cookies are dead."),
    (re.compile(r"\bsuspicious (login |activity)\b", re.I),
     "The platform flagged the session as suspicious."),
    (re.compile(r"\bunusual (traffic|activity) from your computer\b", re.I),
     "The platform flagged the request as automated traffic."),
    (re.compile(r"\benable javascript (and cookies )?to continue\b", re.I),
     "A bot-check interstitial was served; this feed needs the browser strategy."),
]

# Lightweight endpoints that render without JavaScript, per platform.
_ENDPOINTS = {
    "facebook": "https://mbasic.facebook.com/search/posts/?q={q}",
    "instagram": "https://www.instagram.com/explore/tags/{q}/",
    "x": "https://x.com/search?q={q}&f=live",
    "tiktok": "https://www.tiktok.com/search?q={q}",
    "linkedin": "https://www.linkedin.com/search/results/content/?keywords={q}",
    "reddit": "https://old.reddit.com/search?q={q}&sort=new",
}


def _detect_challenge(url, body):
    """Say why the platform refused us, or None when the page looks real.

    Ordered by confidence: where the platform *sent* us is stronger evidence
    than what the page happens to say, and both are checked more narrowly than
    a substring match so ordinary articles are not mistaken for lockouts.
    """
    target = (url or "").lower()
    try:
        path = urlparse(target).path or ""
    except ValueError:
        path = target

    # A redirect to a login/checkpoint path is unambiguous.
    for p in _LOGIN_PATHS:
        if path == p or path.startswith(p + "/") or path.startswith(p + "?"):
            return ("Redirected to %s -- the session cookies are dead or the "
                    "platform wants a fresh login." % p)

    for marker, message in _URL_MARKERS:
        # Match on a path segment or query key, not anywhere in the string, so
        # a search for "captcha" in the query does not trip this.
        if re.search(r"(?:^|[/?&=._-])" + re.escape(marker) + r"(?:[/?&=._-]|$)",
                     path):
            return message

    # Body phrases are matched whole. Only the head of the document is examined:
    # a challenge page says so immediately, while a long article might mention
    # any of these words halfway down.
    head = (body or "")[:8000]
    for rx, message in _BODY_MARKERS:
        if rx.search(head):
            return message
    return None


def _session_for(cookies, target_url=None):
    """Build a session with the stored cookies attached.

    Two details matter here, and getting either wrong makes every authenticated
    fetch fail with a misleading "the session is dead" message:

    * `requests` rejects `domain=None` outright, so a cookie pasted as a plain
      `Cookie:` header -- which carries no domain, and is the most common way
      people copy one -- must be given a domain explicitly. We use the target
      host, which is what the browser would have sent it to anyway.
    * A domainless cookie must still be scoped to *something*; sending it to
      every host would leak the session to unrelated sites.
    """
    s = requests.Session()
    s.headers.update(collectors.HEADERS)

    fallback = ""
    if target_url:
        host = _host(target_url)
        # A leading dot makes the cookie valid for subdomains too, matching how
        # platforms actually set their session cookies.
        fallback = ("." + host) if host and not host.startswith(".") else host

    for c in cookies or []:
        name = (c.get("name") or "").strip()
        if not name:
            continue
        domain = (c.get("domain") or "").strip() or fallback
        try:
            if domain:
                s.cookies.set(name, str(c.get("value", "")),
                              domain=domain, path=c.get("path") or "/")
            else:
                # No domain anywhere: let the jar default it at request time
                # rather than dropping the cookie.
                s.cookies.set(name, str(c.get("value", "")))
        except (AttributeError, TypeError, ValueError):
            # A malformed entry should cost that one cookie, not the session.
            continue
    return s


# -- Extraction --------------------------------------------------------------

def _extract_mbasic(body, base="https://mbasic.facebook.com"):
    """Pull post-shaped blocks out of Facebook's no-JS interface."""
    posts = []
    # mbasic wraps each story in a table/div with an author link then text.
    for chunk in re.split(r"(?i)<div[^>]+class=\"[^\"]*\bstory_body_container\b", body)[1:]:
        author = ""
        m = re.search(r"(?is)<h3[^>]*>.*?<a[^>]*>(.*?)</a>", chunk)
        if m:
            author = _clean(m.group(1))
        text = ""
        m = re.search(r"(?is)<div[^>]*>(?:<span[^>]*>)?(.{40,3000}?)</div>", chunk)
        if m:
            text = _clean(m.group(1))
        link = ""
        m = re.search(r'(?i)href="(/story\.php[^"]+|/permalink\.php[^"]+)"', chunk)
        if m:
            link = base + m.group(1).replace("&amp;", "&")
        if author and len(text) > 30:
            posts.append({"author": author, "text": text, "url": link})
    return posts


def _extract_generic(body, url):
    """Fallback: treat substantial text blocks as posts."""
    blocks = re.findall(
        r"(?is)<(?:p|li|blockquote|article|div)[^>]*>(.*?)</(?:p|li|blockquote|article|div)>",
        body)
    seen, out = set(), []
    for b in blocks:
        t = _clean(b)
        if len(t) >= 80 and t not in seen:
            seen.add(t)
            out.append({"author": _host(url) or "Page", "text": t, "url": url})
        if len(out) >= 40:
            break
    return out


def _extract_old_reddit(body):
    posts = []
    for chunk in re.split(r'(?i)<div[^>]+class="[^"]*\bthing\b', body)[1:]:
        m = re.search(r'(?i)data-author="([^"]+)"', chunk)
        author = m.group(1) if m else ""
        m = re.search(r'(?is)<a[^>]+class="[^"]*\btitle\b[^"]*"[^>]*>(.*?)</a>', chunk)
        title = _clean(m.group(1)) if m else ""
        m = re.search(r'(?i)data-permalink="([^"]+)"', chunk)
        link = "https://old.reddit.com" + m.group(1) if m else ""
        if author and title:
            posts.append({"author": "/u/" + author, "text": title, "url": link})
    return posts


# -- Fetchers ----------------------------------------------------------------

def fetch_with_cookies(platform, query, cookies, limit=25, url=None):
    """Strategy 1: plain HTTP with the session cookies attached."""
    platform = (platform or "generic").lower()
    target = url or _ENDPOINTS.get(platform)
    if not target:
        return _result(False, note="No known endpoint for %s. Supply a URL." % platform)
    target = target.replace("{q}", requests.utils.quote(query or ""))

    session = _session_for(cookies, target)
    try:
        resp = session.get(target, timeout=20, allow_redirects=True)
    except requests.exceptions.Timeout:
        return _result(False, note="Timed out reaching %s" % _host(target))
    except requests.exceptions.RequestException as e:
        return _result(False, note="Could not reach %s: %s" % (_host(target), type(e).__name__))

    challenge = _detect_challenge(resp.url, resp.text)
    if challenge:
        return _result(False, blocked=True, note=challenge, manual_url=target)

    if platform == "facebook":
        raw = _extract_mbasic(resp.text)
    elif platform == "reddit":
        raw = _extract_old_reddit(resp.text)
    else:
        raw = _extract_generic(resp.text, resp.url)

    if not raw:
        return _result(False, blocked=True,
                       note=("Authenticated but nothing post-shaped was found. The "
                             "layout likely changed, or the feed needs JavaScript."),
                       manual_url=target)

    posts = [{
        "platform": collectors._MANUAL.get(platform, (platform.title(), ""))[0],
        "author": p["author"][:200],
        "handle": "",
        "verified": False,
        "text": p["text"][:4000],
        "url": p["url"] or target,
        "link_kind": "direct" if p["url"] else "search",
        "posted_at": None,
        "source_url": target,
    } for p in raw[:limit]]
    return _result(True, posts, "Fetched %d post(s) with a stored session" % len(posts))


def playwright_available():
    try:
        import playwright  # noqa: F401
        return True
    except ImportError:
        return False


def fetch_with_browser(platform, query, cookies, limit=25, url=None):
    """Strategy 2: a real browser, for feeds that need JavaScript."""
    if not playwright_available():
        return _result(
            False,
            note=("Playwright is not installed. Run 'pip install playwright' then "
                  "'playwright install chromium' to enable browser-based collection."))

    from playwright.sync_api import sync_playwright  # imported lazily

    platform = (platform or "generic").lower()
    target = url or _ENDPOINTS.get(platform)
    if not target:
        return _result(False, note="No known endpoint for %s. Supply a URL." % platform)
    target = target.replace("{q}", requests.utils.quote(query or ""))

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            ctx = browser.new_context(user_agent=collectors.HEADERS["User-Agent"],
                                      locale="en-US")
            valid = []
            for c in cookies or []:
                name = (c.get("name") or "").strip()
                if not name:
                    continue
                valid.append({
                    "name": name, "value": str(c.get("value", "")),
                    "domain": (c.get("domain") or "").strip() or "." + _host(target),
                    "path": c.get("path") or "/",
                })
            if valid:
                # Playwright rejects the whole batch if any single cookie is
                # malformed, so fall back to adding them one at a time rather
                # than losing an otherwise good session to one bad entry.
                try:
                    ctx.add_cookies(valid)
                except Exception:
                    for one in valid:
                        try:
                            ctx.add_cookies([one])
                        except Exception:
                            continue
            page = ctx.new_page()
            page.goto(target, timeout=35000, wait_until="domcontentloaded")
            page.wait_for_timeout(3500)  # let the feed hydrate
            final_url, body = page.url, page.content()
            browser.close()
    except Exception as e:
        return _result(False, note="Browser fetch failed: %s" % type(e).__name__)

    challenge = _detect_challenge(final_url, body)
    if challenge:
        return _result(False, blocked=True, note=challenge, manual_url=target)

    raw = _extract_generic(body, final_url)
    if not raw:
        return _result(False, blocked=True,
                       note="Page loaded but no post-shaped text was found.",
                       manual_url=target)

    posts = [{
        "platform": collectors._MANUAL.get(platform, (platform.title(), ""))[0],
        "author": p["author"][:200], "handle": "", "verified": False,
        "text": p["text"][:4000], "url": p["url"] or final_url,
        "link_kind": "direct", "posted_at": None, "source_url": target,
    } for p in raw[:limit]]
    return _result(True, posts, "Fetched %d post(s) via browser session" % len(posts))


def collect_authenticated(credential, cookies, query, limit=25, url=None,
                          use_browser=False):
    """Run an authenticated collection and report what happened."""
    fn = fetch_with_browser if use_browser else fetch_with_cookies
    result = fn(credential.platform, query, cookies, limit=limit, url=url)
    result["used_credential"] = credential.label
    result["strategy"] = "browser" if use_browser else "http"
    # Fall back to the browser automatically when the simple path finds nothing.
    if (not result["ok"] and not use_browser and playwright_available()
            and not result.get("blocked")):
        alt = fetch_with_browser(credential.platform, query, cookies,
                                 limit=limit, url=url)
        alt["used_credential"] = credential.label
        alt["strategy"] = "browser (fallback)"
        if alt["ok"]:
            return alt
    return result
