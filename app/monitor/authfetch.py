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
from datetime import datetime

import requests

from . import collectors
from .collectors import _clean, _host, _result

# Text that means "we noticed you are a bot" rather than "no results".
_CHALLENGE_MARKERS = [
    ("checkpoint", "The account hit a Meta checkpoint and needs manual review."),
    ("login_attempt", "The session was rejected and a fresh login was demanded."),
    ("/login", "Redirected to a login page -- the session cookies are dead."),
    ("captcha", "A CAPTCHA was served."),
    ("suspended", "The account appears suspended."),
    ("challenge", "The platform issued a challenge page."),
    ("temporarily blocked", "The account is temporarily blocked."),
    ("confirm your identity", "Identity confirmation was requested."),
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
    low = (body or "")[:6000].lower()
    target = (url or "").lower()
    for marker, message in _CHALLENGE_MARKERS:
        if marker in target or marker in low:
            return message
    return None


def _session_for(cookies):
    s = requests.Session()
    s.headers.update(collectors.HEADERS)
    for c in cookies or []:
        if not c.get("name"):
            continue
        try:
            s.cookies.set(c["name"], str(c.get("value", "")),
                          domain=c.get("domain") or None,
                          path=c.get("path") or "/")
        except Exception:
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

    session = _session_for(cookies)
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
                if not c.get("name"):
                    continue
                valid.append({
                    "name": c["name"], "value": str(c.get("value", "")),
                    "domain": c.get("domain") or "." + _host(target),
                    "path": c.get("path") or "/",
                })
            if valid:
                ctx.add_cookies(valid)
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
