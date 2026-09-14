"""Live post collection.

Each collector takes a query plus options and returns a list of raw post
dicts: platform, author, handle, verified, text, url, posted_at, source_url.

Reality check on scraping: Facebook, Instagram, X and TikTok all require an
authenticated session and actively block scripted requests from datacenter
and residential IPs alike. Rather than pretend otherwise or silently return
nothing, those collectors report `blocked` with a dork URL the analyst can
open and collect from by hand. The collectors that do work without keys --
RSS/Atom, Reddit, Hacker News, news aggregator feeds, Nitter/Teddit mirrors
and plain web pages -- are fully implemented.

Mirror hosts move and die constantly, so they live in `sources.json` and can
be edited without touching this file.

Fetching notes
--------------
* One pooled `requests.Session` per thread, with keep-alive and a retry policy
  for the transient failures (429, 502-504) that would otherwise show up as a
  dead source.
* Responses are cached briefly in-process, so re-running a collection or
  hitting several watches that share a feed does not re-fetch the same bytes.
* User-Agent is rotated per host, because a single fixed UA is the easiest
  possible fingerprint to block on.
"""

import hashlib
import html
import json
import os
import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, quote_plus, unquote, urlparse
from xml.etree import ElementTree

import requests
from requests.adapters import HTTPAdapter

from . import capabilities

try:  # urllib3 v2 and v1 keep Retry in different places
    from urllib3.util.retry import Retry
except ImportError:  # pragma: no cover
    Retry = None

_SOURCES_PATH = os.path.join(os.path.dirname(__file__), "sources.json")

USER_AGENTS = [
    ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
     "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
     "(KHTML, like Gecko) Version/17.4 Safari/605.1.15"),
    ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
     "Chrome/123.0.0.0 Safari/537.36"),
    ("Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 "
     "Firefox/125.0"),
]

HEADERS = {
    "User-Agent": USER_AGENTS[0],
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}
TIMEOUT = 12
MAX_PER_SOURCE = 40
CACHE_TTL = 120        # seconds a fetched body stays reusable
CACHE_MAX = 256


def _headers_for(url, extra=None):
    """Per-host headers. The UA is stable per host but varies between hosts."""
    host = _host(url)
    ua = USER_AGENTS[int(hashlib.md5(host.encode()).hexdigest(), 16) % len(USER_AGENTS)]
    h = dict(HEADERS, **{"User-Agent": ua})
    if host:
        h["Referer"] = "https://" + host + "/"
    if extra:
        h.update(extra)
    return h


# -- HTTP session pool -------------------------------------------------------

_local = threading.local()


def session():
    """A pooled session for the calling thread.

    Connection reuse is the single biggest win when a collection run pulls
    several feeds from the same host, and the retry policy turns transient
    429/5xx responses into a successful fetch instead of a dead source.
    """
    s = getattr(_local, "session", None)
    if s is not None:
        return s
    s = requests.Session()
    if Retry is not None:
        retry = Retry(
            total=2, connect=2, read=2, backoff_factor=0.6,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset(["GET", "HEAD"]),
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry, pool_connections=16,
                              pool_maxsize=16)
        s.mount("https://", adapter)
        s.mount("http://", adapter)
    _local.session = s
    return s


# -- Response cache ----------------------------------------------------------

_cache = {}
_cache_lock = threading.Lock()


def _cache_get(key):
    with _cache_lock:
        hit = _cache.get(key)
        if not hit:
            return None
        ts, value = hit
        if time.time() - ts > CACHE_TTL:
            _cache.pop(key, None)
            return None
        return value


def _cache_put(key, value):
    with _cache_lock:
        if len(_cache) >= CACHE_MAX:
            # Drop the oldest quarter rather than clearing everything, so a
            # busy run does not repeatedly lose its whole cache.
            for k in sorted(_cache, key=lambda k: _cache[k][0])[:CACHE_MAX // 4]:
                _cache.pop(k, None)
        _cache[key] = (time.time(), value)


def cache_clear():
    with _cache_lock:
        _cache.clear()


def http_get(url, timeout=TIMEOUT, headers=None, use_cache=True, **kw):
    """GET with pooling, retries and a short response cache."""
    key = "GET:" + url
    if use_cache:
        cached = _cache_get(key)
        if cached is not None:
            return cached
    resp = session().get(url, headers=_headers_for(url, headers),
                         timeout=timeout, **kw)
    if use_cache and resp.status_code == 200:
        _cache_put(key, resp)
    return resp


def load_sources():
    try:
        with open(_SOURCES_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"feeds": [], "mirrors": {}}


def _fix_mojibake(text):
    """Repair UTF-8 that was decoded as latin-1 somewhere upstream.

    Bing's news feed double-encodes: curly quotes arrive as 'â€œ' and friends.
    Round-tripping through latin-1 restores the original bytes. Only applied
    when the telltale sequences are present, so correct text is left alone.
    """
    if "â€" not in text and "Ã" not in text:
        return text
    try:
        return text.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text


def _clean(text):
    """Strip tags and collapse whitespace from feed HTML."""
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", text or "")
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = html.unescape(text)
    text = _fix_mojibake(text)
    return re.sub(r"\s+", " ", text).strip()


def _iso(dt):
    return dt.replace(microsecond=0).isoformat()


def parse_dt(value):
    """Parse a feed date into a naive UTC datetime, or None."""
    if not value:
        return None
    value = str(value).strip()
    # Strip a trailing named zone that strptime cannot read alongside %z.
    value = re.sub(r"\s+\((?:[A-Z]{2,5})\)$", "", value)
    fmts = ("%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S %Z",
            "%a, %d %b %Y %H:%M %z", "%d %b %Y %H:%M:%S %z",
            "%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z",
            "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d")
    for fmt in fmts:
        try:
            dt = datetime.strptime(value.replace("GMT", "+0000").replace("UTC", "+0000"), fmt)
            if dt.tzinfo:
                dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
            return dt.replace(microsecond=0)
        except ValueError:
            continue
    return None


def _parse_date(value):
    dt = parse_dt(value)
    return _iso(dt) if dt else None


def _unwrap(url):
    """Return the real destination behind an aggregator redirect.

    Bing wraps every result in http://bing.com/news/apiclick.aspx?...&url=<real>,
    and Google News uses news.google.com/rss/articles/... Left alone, the
    wrapper's own http scheme and 'apiclick' path trip the link rules on every
    article, so unwrap before anything else looks at the URL.
    """
    if not url:
        return url
    try:
        parsed = urlparse(url)
    except ValueError:
        return url
    if not parsed.query:
        return url
    params = parse_qs(parsed.query)
    for key in ("url", "u", "q", "target"):
        for candidate in params.get(key, []):
            if candidate.startswith(("http://", "https://")):
                return unquote(candidate)
    return url


def _is_opaque(url):
    """True for aggregator links that don't name the destination.

    Google News encrypts its article ids and resolves them with JavaScript, so
    a server-side fetch can never recover the publisher's URL. Rather than hand
    the analyst a dead link, we mark these and fall back to a title search.
    """
    host = _host(url)
    return host.endswith("news.google.com") or "/rss/articles/" in (url or "")


def _search_link(title, publisher=""):
    """A link that reliably reaches the article when the real URL is unknown."""
    terms = re.sub(r"\s+-\s+[^-]{2,45}$", "", title or "").strip()
    if publisher and publisher.lower() not in terms.lower():
        terms = "%s %s" % (terms, publisher)
    return "https://www.google.com/search?q=" + quote_plus(terms[:200])


def _publisher(title, link):
    """Recover the outlet name from an aggregator headline.

    Google News and Bing append the publisher to the headline as
    "Headline - Publisher". Falling back to the link host keeps the author
    field pointing at who published, never at the search query.
    """
    if title and " - " in title:
        tail = title.rsplit(" - ", 1)[-1].strip()
        # A publisher name is short and is not a sentence fragment.
        if 2 < len(tail) <= 45 and not tail.endswith((".", "?", "!")):
            return tail
    return _host(link)


def _host(url):
    try:
        return (urlparse(url).hostname or "").replace("www.", "")
    except ValueError:
        return ""


def _result(ok=True, posts=None, note="", blocked=False, manual_url="",
            dorks=None):
    """One collector's outcome.

    `dorks` carries targeted follow-up searches for the login-walled platforms,
    where what the app can fetch is only part of the picture and the analyst
    needs precise queries rather than a single generic link.
    """
    return {"ok": ok, "posts": posts or [], "note": note,
            "blocked": blocked, "manual_url": manual_url,
            "dorks": dorks or []}


# -- RSS / Atom --------------------------------------------------------------

def fetch_feed(url, platform=None, limit=MAX_PER_SOURCE, since=None,
               use_cache=True):
    """Parse any RSS 2.0 or Atom feed into posts.

    `since` is a naive UTC datetime; items older than it are dropped at the
    source, so an analyst asking for "the last 7 days" does not pay to store
    and score a year of archive.
    """
    try:
        # A retry after a rate-limited refusal must not replay the cached
        # failure, or the backoff accomplishes nothing.
        resp = http_get(url, use_cache=use_cache)
        resp.raise_for_status()
        # Some feeds (Bing) declare no charset and default to latin-1, which
        # mojibakes any non-ASCII text. Trust the XML declaration instead.
        raw = resp.content
        if resp.encoding and resp.encoding.lower() == "iso-8859-1":
            resp.encoding = resp.apparent_encoding or "utf-8"
        root = ElementTree.fromstring(raw)
    except requests.exceptions.Timeout:
        return _result(False, note="Timed out after %ds" % TIMEOUT)
    except requests.exceptions.RequestException as e:
        return _result(False, note="Could not reach the feed: %s" % type(e).__name__)
    except ElementTree.ParseError:
        return _result(False, note="The response was not valid RSS or Atom XML")

    ns = {"atom": "http://www.w3.org/2005/Atom",
          "dc": "http://purl.org/dc/elements/1.1/",
          "media": "http://search.yahoo.com/mrss/",
          "content": "http://purl.org/rss/1.0/modules/content/"}
    items = root.findall(".//item") or root.findall(".//atom:entry", ns)
    feed_title = ""
    for path in ("./channel/title", "atom:title"):
        el = root.find(path, ns)
        if el is not None and el.text:
            feed_title = el.text.strip()
            break

    posts = []
    stale = 0
    for item in items[:max(limit * 3, limit)]:
        if len(posts) >= limit:
            break

        def text_of(*paths):
            for p in paths:
                try:
                    el = item.find(p, ns)
                except SyntaxError:
                    continue  # feed uses a namespace prefix we don't declare
                if el is not None:
                    if el.text and el.text.strip():
                        return el.text.strip()
                    href = el.get("href")
                    if href:
                        return href.strip()
            return ""

        title = _clean(text_of("title", "atom:title"))
        body = _clean(text_of("description", "content:encoded", "atom:summary",
                              "atom:content", "media:description"))
        link = _unwrap(text_of("link", "atom:link", "guid"))
        # Prefer a real byline, then the publication. Aggregator feed titles
        # echo the search query ("NAMFREL BARMM - Google News"), which would
        # otherwise look like the monitored subject impersonating itself.
        author = (_clean(text_of("author", "dc:creator", "atom:author/atom:name"))
                  or _publisher(title, link) or _host(link) or feed_title or "Feed")
        when = parse_dt(text_of("pubDate", "atom:published", "atom:updated",
                                "dc:date"))
        published = _iso(when) if when else None
        if since and when and when < since:
            stale += 1
            continue
        combined = title if not body else (title + " — " + body if title else body)
        if not combined:
            continue
        opaque = _is_opaque(link)
        posts.append({
            "platform": platform or "News site",
            "author": author[:120],
            "handle": "" if opaque else _host(link),
            "verified": False,
            "text": combined[:4000],
            # An opaque aggregator id is useless to click, so point at a search
            # that finds the article and keep the original for reference.
            "url": _search_link(title, author) if opaque else link,
            "link_kind": "search" if opaque else "direct",
            "posted_at": published,
            "source_url": url,
        })
    note = "Fetched %d item%s" % (len(posts), "" if len(posts) == 1 else "s")
    if stale:
        note += " (%d older than the date filter)" % stale
    return _result(True, posts, note)


# -- Reddit ------------------------------------------------------------------

def fetch_reddit(query, limit=MAX_PER_SOURCE, subreddit=None, sort="new",
                 since=None):
    """Reddit search.

    The JSON API 403s from datacenter IPs, so we try it first and fall back to
    the RSS endpoint, which is served without the same restriction.
    """
    if subreddit:
        url = ("https://www.reddit.com/r/%s/search.json?q=%s&restrict_sr=1&sort=%s&limit=%d"
               % (quote_plus(subreddit), quote_plus(query), sort, limit))
        rss = ("https://www.reddit.com/r/%s/search.rss?q=%s&restrict_sr=1&sort=%s&limit=%d"
               % (quote_plus(subreddit), quote_plus(query), sort, limit))
    else:
        url = ("https://www.reddit.com/search.json?q=%s&sort=%s&limit=%d"
               % (quote_plus(query), sort, limit))
        rss = ("https://www.reddit.com/search.rss?q=%s&sort=%s&limit=%d"
               % (quote_plus(query), sort, limit))

    search_url = "https://www.reddit.com/search/?q=%s&sort=%s" % (quote_plus(query), sort)

    def _rss_fallback(reason):
        # Reddit rate-limits rather than blocks: measured from one IP, the RSS
        # endpoint refuses roughly every other request and then serves the next
        # one fine. Treating the first refusal as a permanent block threw away
        # results that were one short retry away, so back off and try again.
        attempts = 3
        for attempt in range(attempts):
            if attempt:
                time.sleep(1.2 * attempt)
            r = fetch_feed(rss, platform="Reddit", limit=limit, since=since,
                           use_cache=False)
            if r["ok"] and r["posts"]:
                r["note"] = ("Fetched %d via Reddit RSS (%s%s)"
                             % (len(r["posts"]), reason,
                                ", retry %d" % (attempt + 1) if attempt else ""))
                return r

        # Still nothing: the mirrors are the last open route.
        mirrored = fetch_via_mirrors(query, "teddit", limit, since=since)
        if mirrored["ok"] and mirrored["posts"]:
            return mirrored
        return _result(
            False, blocked=True,
            note=("Reddit refused the JSON API (%s) and RSS after %d attempts. "
                  "This is rate limiting on the source IP, not a bad query -- "
                  "wait a minute and retry, or open the search and collect by "
                  "hand." % (reason, attempts)),
            manual_url=search_url)

    try:
        resp = http_get(url, headers={"Accept": "application/json"})
        if resp.status_code in (403, 429):
            return _rss_fallback("JSON API returned %d" % resp.status_code)
        resp.raise_for_status()
        data = resp.json()
    except requests.exceptions.Timeout:
        return _result(False, note="Reddit timed out after %ds" % TIMEOUT)
    except requests.exceptions.RequestException as e:
        return _rss_fallback(type(e).__name__)
    except ValueError:
        return _rss_fallback("non-JSON response")

    posts = []
    for child in (data.get("data", {}) or {}).get("children", [])[:limit]:
        d = child.get("data", {}) or {}
        title = d.get("title") or ""
        body = d.get("selftext") or ""
        combined = (title + " — " + body).strip(" —") if body else title
        if not combined:
            continue
        created = d.get("created_utc")
        when = datetime.fromtimestamp(created, timezone.utc).replace(tzinfo=None) if created else None
        if since and when and when < since:
            continue
        posts.append({
            "platform": "Reddit",
            "author": d.get("author") or "unknown",
            "handle": d.get("author") or "",
            "verified": False,
            "text": combined[:4000],
            "url": "https://www.reddit.com" + (d.get("permalink") or ""),
            "posted_at": _iso(when) if when else None,
            "source_url": url,
            "extra": {"subreddit": d.get("subreddit"), "score": d.get("score"),
                      "comments": d.get("num_comments")},
        })
    return _result(True, posts, "Fetched %d Reddit post(s)" % len(posts))


# -- Hacker News -------------------------------------------------------------

def fetch_hackernews(query, limit=MAX_PER_SOURCE, since=None):
    url = ("https://hn.algolia.com/api/v1/search_by_date?query=%s&tags=(story,comment)&hitsPerPage=%d"
           % (quote_plus(query), limit))
    if since:
        url += "&numericFilters=created_at_i>%d" % int(since.replace(
            tzinfo=timezone.utc).timestamp())
    try:
        resp = http_get(url, headers={"Accept": "application/json"})
        resp.raise_for_status()
        data = resp.json()
    except requests.exceptions.RequestException as e:
        return _result(False, note="Could not reach Hacker News: %s" % type(e).__name__)
    except ValueError:
        return _result(False, note="Hacker News returned a non-JSON response")

    posts = []
    for hit in data.get("hits", [])[:limit]:
        body = _clean(hit.get("comment_text") or "") or (hit.get("title") or "")
        if not body:
            continue
        posts.append({
            "platform": "HackerNews",
            "author": hit.get("author") or "unknown",
            "handle": hit.get("author") or "",
            "verified": False,
            "text": body[:4000],
            "url": hit.get("url") or ("https://news.ycombinator.com/item?id=%s"
                                      % hit.get("objectID")),
            "posted_at": _parse_date(hit.get("created_at")),
            "source_url": url,
        })
    return _result(True, posts, "Fetched %d Hacker News item(s)" % len(posts))


# -- News aggregators (RSS-backed, no key) -----------------------------------

def fetch_google_news(query, limit=MAX_PER_SOURCE, since=None, region=None):
    # Google News accepts a relative recency operator directly in the query,
    # which narrows at the source instead of fetching a year and discarding it.
    q = query
    if since:
        days = max(1, (datetime.utcnow() - since).days)
        q = "%s when:%dd" % (query, min(days, 365))
    hl, gl, ceid = (region or "en-US", (region or "en-US").split("-")[-1],
                    "%s:%s" % ((region or "en-US").split("-")[-1],
                               (region or "en-US").split("-")[0]))
    url = ("https://news.google.com/rss/search?q=%s&hl=%s&gl=%s&ceid=%s"
           % (quote_plus(q), hl, gl, ceid))
    r = fetch_feed(url, platform="News site", limit=limit, since=since)
    if r["ok"]:
        r["note"] = "Fetched %d Google News result(s)" % len(r["posts"])
    return r


def fetch_bing_news(query, limit=MAX_PER_SOURCE, since=None):
    url = "https://www.bing.com/news/search?q=%s&format=RSS" % quote_plus(query)
    if since:
        days = (datetime.utcnow() - since).days
        # Bing exposes only coarse buckets, so map to the nearest it supports.
        url += "&qft=" + quote_plus(
            "+filterui:age-lt%s" % ("1440" if days <= 1 else
                                    "10080" if days <= 7 else "43200"))
    r = fetch_feed(url, platform="News site", limit=limit, since=since)
    if r["ok"]:
        r["note"] = "Fetched %d Bing News result(s)" % len(r["posts"])
    return r


def fetch_youtube(query, limit=MAX_PER_SOURCE, channel_id=None, since=None):
    """YouTube exposes per-channel Atom feeds without a key. Search needs one."""
    if channel_id:
        url = "https://www.youtube.com/feeds/videos.xml?channel_id=%s" % quote_plus(channel_id)
        return fetch_feed(url, platform="YouTube", limit=limit, since=since)
    return _result(
        False, blocked=True,
        note=("YouTube keyword search needs an API key. Per-channel feeds work: "
              "supply a channel ID, or collect manually."),
        manual_url="https://www.youtube.com/results?search_query=%s" % quote_plus(query))


# -- Mirror-based social collection ------------------------------------------

def fetch_via_mirrors(query, kind, limit=MAX_PER_SOURCE, since=None):
    """Try each configured mirror in turn until one answers.

    Nitter (X) and Teddit (Reddit) mirrors are volunteer-run and frequently
    offline; we try them in order and report honestly when all fail.
    """
    sources = load_sources()
    mirrors = (sources.get("mirrors") or {}).get(kind) or []
    tried = []
    for tmpl in mirrors:
        url = tmpl.replace("{query}", quote_plus(query))
        r = fetch_feed(url, platform="X" if kind == "nitter" else "Reddit",
                       limit=limit, since=since)
        if r["ok"] and r["posts"]:
            r["note"] = "Fetched %d via mirror %s" % (len(r["posts"]), _host(url))
            return r
        tried.append(_host(url))
    return _result(
        False, blocked=True,
        note=("No working %s mirror. Tried: %s"
              % (kind, ", ".join(tried) if tried else "none configured")),
        manual_url=("https://x.com/search?q=%s" if kind == "nitter"
                    else "https://www.reddit.com/search/?q=%s") % quote_plus(query))


# -- Generic page scrape -----------------------------------------------------

def fetch_page(url, limit=MAX_PER_SOURCE):
    """Pull visible text blocks from an arbitrary public page.

    Deliberately simple: it extracts paragraph-level text so the analyst can
    scan a forum thread or article body. It is not a JS-rendering browser.
    """
    if not re.match(r"^https?://", url or "", re.I):
        return _result(False, note="Enter a full http:// or https:// URL")
    try:
        resp = http_get(url)
        resp.raise_for_status()
    except requests.exceptions.RequestException as e:
        return _result(False, note="Could not fetch the page: %s" % type(e).__name__)

    body = resp.text
    if "<rss" in body[:600].lower() or "<feed" in body[:600].lower():
        return fetch_feed(url, limit=limit)

    title = ""
    m = re.search(r"(?is)<title[^>]*>(.*?)</title>", body)
    if m:
        title = _clean(m.group(1))
    blocks = re.findall(r"(?is)<(?:p|li|blockquote|h[1-3])[^>]*>(.*?)</(?:p|li|blockquote|h[1-3])>", body)
    chunks = []
    for b in blocks:
        t = _clean(b)
        if len(t) >= 60:
            chunks.append(t)
    if not chunks:
        stripped = _clean(re.sub(r"(?is)<(script|style|nav|footer|header)[^>]*>.*?</\1>", " ", body))
        if len(stripped) >= 60:
            chunks = [stripped[:4000]]
    host = _host(url)
    posts = [{
        "platform": "Web page",
        "author": title or host or "Page",
        "handle": host,
        "verified": False,
        "text": c[:4000],
        "url": url,
        "posted_at": None,
        "source_url": url,
    } for c in chunks[:limit]]
    if not posts:
        return _result(False, note="No readable text blocks found on that page")
    return _result(True, posts, "Extracted %d text block(s)" % len(posts))


# -- Additional open sources (threat hunting / SOCMINT) ----------------------

def fetch_mastodon(query, limit=MAX_PER_SOURCE, instance=None, since=None):
    """Mastodon's public hashtag timeline needs no key.

    The fediverse is where a lot of coordinated messaging moves once it is
    pushed off the mainstream platforms, so it is worth having.
    """
    host = (instance or "mastodon.social").replace("https://", "").strip("/")
    words = [w for w in re.split(r"[^A-Za-z0-9]+", query) if w]
    tag = words[0] if words else ""
    if not tag:
        return _result(False, note="Mastodon needs a word to use as a hashtag")
    url = ("https://%s/api/v1/timelines/tag/%s?limit=%d"
           % (host, quote_plus(tag), min(limit, 40)))
    try:
        resp = http_get(url, headers={"Accept": "application/json"})
        resp.raise_for_status()
        data = resp.json()
    except requests.exceptions.RequestException as e:
        return _result(False, note="Could not reach %s: %s" % (host, type(e).__name__))
    except ValueError:
        return _result(False, note="%s returned a non-JSON response" % host)

    posts = []
    for item in (data if isinstance(data, list) else [])[:limit]:
        body = _clean(item.get("content") or "")
        if not body:
            continue
        when = parse_dt(item.get("created_at"))
        if since and when and when < since:
            continue
        acct = item.get("account") or {}
        posts.append({
            "platform": "Mastodon",
            "author": acct.get("display_name") or acct.get("username") or "unknown",
            "handle": acct.get("acct") or "",
            "verified": False,
            "text": body[:4000],
            "url": item.get("url") or item.get("uri") or "",
            "posted_at": _iso(when) if when else None,
            "source_url": url,
            "extra": {"boosts": item.get("reblogs_count"),
                      "replies": item.get("replies_count")},
        })
    return _result(True, posts,
                   "Fetched %d Mastodon post(s) from #%s" % (len(posts), tag))


def fetch_lemmy(query, limit=MAX_PER_SOURCE, instance=None, since=None):
    """Lemmy is the fediverse's Reddit, and its search API is open."""
    host = (instance or "lemmy.world").replace("https://", "").strip("/")
    url = ("https://%s/api/v3/search?q=%s&type_=Posts&sort=New&limit=%d"
           % (host, quote_plus(query), min(limit, 50)))
    try:
        resp = http_get(url, headers={"Accept": "application/json"})
        resp.raise_for_status()
        data = resp.json()
    except requests.exceptions.RequestException as e:
        return _result(False, note="Could not reach %s: %s" % (host, type(e).__name__))
    except ValueError:
        return _result(False, note="%s returned a non-JSON response" % host)

    posts = []
    for entry in (data.get("posts") or [])[:limit]:
        post = entry.get("post") or {}
        creator = entry.get("creator") or {}
        body = ((post.get("name") or "") + " - " + (post.get("body") or "")).strip(" -")
        if not body:
            continue
        when = parse_dt(post.get("published"))
        if since and when and when < since:
            continue
        posts.append({
            "platform": "Lemmy",
            "author": creator.get("display_name") or creator.get("name") or "unknown",
            "handle": creator.get("name") or "",
            "verified": False,
            "text": body[:4000],
            "url": post.get("ap_id") or post.get("url") or "",
            "posted_at": _iso(when) if when else None,
            "source_url": url,
        })
    return _result(True, posts, "Fetched %d Lemmy post(s)" % len(posts))


def fetch_wikipedia(query, limit=MAX_PER_SOURCE, since=None):
    """Articles mentioning the subject, most recently edited first.

    Narrative work often shows up as article edits before it shows up
    anywhere else, and the API is open.
    """
    url = ("https://en.wikipedia.org/w/api.php?action=query&list=search"
           "&srsearch=%s&srsort=last_edit_desc&srlimit=%d&format=json"
           % (quote_plus(query), min(limit, 50)))
    try:
        resp = http_get(url, headers={"Accept": "application/json"})
        resp.raise_for_status()
        data = resp.json()
    except requests.exceptions.RequestException as e:
        return _result(False, note="Could not reach Wikipedia: %s" % type(e).__name__)
    except ValueError:
        return _result(False, note="Wikipedia returned a non-JSON response")

    posts = []
    for hit in ((data.get("query") or {}).get("search") or [])[:limit]:
        when = parse_dt(hit.get("timestamp"))
        if since and when and when < since:
            continue
        title = hit.get("title") or ""
        snippet = _clean(hit.get("snippet") or "")
        posts.append({
            "platform": "Wikipedia",
            "author": title or "Wikipedia",
            "handle": "en.wikipedia.org",
            "verified": False,
            "text": (title + " - " + snippet)[:4000],
            "url": "https://en.wikipedia.org/wiki/" + quote_plus(title.replace(" ", "_")),
            "posted_at": _iso(when) if when else None,
            "source_url": url,
        })
    return _result(True, posts, "Fetched %d Wikipedia article(s)" % len(posts))


def fetch_paste_dorks(query, limit=MAX_PER_SOURCE, since=None):
    """Paste sites are where leaked personal data usually surfaces first.

    Their own search is gated, so this returns a targeted dork rather than
    pretending to scrape. Real signal for a doxxing watch, honestly labelled.
    """
    sites = ["pastebin.com", "ghostbin.com", "controlc.com", "justpaste.it",
             "rentry.co", "telegra.ph"]
    dork = "(%s) (%s)" % (query, " OR ".join("site:" + s for s in sites))
    return _result(
        False, blocked=True,
        note=("Paste sites block automated search. This dork finds exposed data "
              "for the subject -- open it and import anything relevant."),
        manual_url="https://www.google.com/search?q=" + quote_plus(dork))


def fetch_all_feeds(query, limit=MAX_PER_SOURCE, since=None, feeds=None):
    """Fan out across every fixed feed in sources.json at once.

    One action that actually sweeps the configured newsrooms, instead of making
    the analyst add each feed by hand. Query filtering happens later, in the
    relevance pass, because a newsroom feed carries everything it published.
    """
    configured = feeds or [f["url"] for f in load_sources().get("suggested_feeds", [])
                           if "{query}" not in (f.get("url") or "")]
    if not configured:
        return _result(False, note="No fixed feeds are configured in sources.json")

    posts, ok, notes = [], 0, []
    with ThreadPoolExecutor(max_workers=min(8, len(configured))) as pool:
        futures = {pool.submit(fetch_feed, u, None, limit, since): u
                   for u in configured}
        for fut in as_completed(futures):
            try:
                r = fut.result()
            except Exception as e:
                notes.append("%s: %s" % (_host(futures[fut]), type(e).__name__))
                continue
            if r["ok"]:
                ok += 1
                posts.extend(r["posts"])
            else:
                notes.append("%s: %s" % (_host(futures[fut]), r["note"]))

    note = "Fetched %d item(s) from %d of %d feeds" % (len(posts), ok, len(configured))
    if notes:
        note += " - failures: " + "; ".join(notes[:3])
    return _result(True, posts[:limit * 4], note)


# -- Library-backed collectors -----------------------------------------------
#
# Each of these needs an optional dependency and, in some cases, a credential.
# They all follow the same contract: if the library is missing or the key is
# absent, say so plainly and hand back a manual URL. A source that cannot run
# must never look like a source that found nothing.

def _needs(cap, query, manual=None):
    """The standard 'this capability is not usable here' result."""
    info = capabilities.probe(cap)
    note = "%s is unavailable: %s" % (info.get("label", cap),
                                      info.get("reason") or "not configured")
    if info.get("needs"):
        note += ". Needs %s" % info["needs"]
    if info.get("caveat"):
        note += ". " + info["caveat"]
    return _result(False, blocked=True, note=note,
                   manual_url=manual or ("https://www.google.com/search?q="
                                         + quote_plus(query)))


def _cred(platform, key):
    """Read a secret from the vault, or None.

    Credentials live in the encrypted vault rather than in environment
    variables, so nothing sensitive sits in the process environment or in a
    deployment config.
    """
    try:
        from . import vault
        from ..models import MonitorCredential
        if not vault.is_unlocked():
            return None
        rows = MonitorCredential.query.filter_by(platform=platform,
                                                 enabled=True).all()
        for c in rows:
            data = vault.decrypt(c.secret_blob) or {}
            if key in data:
                return data[key]
            if data.get("token") and key.endswith("token"):
                return data["token"]
    except Exception:
        return None
    return None


def fetch_praw(query, limit=MAX_PER_SOURCE, since=None, subreddit=None):
    """Authenticated Reddit via PRAW.

    The public JSON endpoint is blocked from most datacenter IPs; an app-only
    OAuth token is not, so this is the reliable path when credentials exist.
    """
    praw = capabilities.load("praw")
    if praw is None:
        return _needs("praw", query,
                      "https://www.reddit.com/search/?q=" + quote_plus(query))

    cid = _cred("reddit", "client_id") or os.environ.get("REDDIT_CLIENT_ID")
    secret = _cred("reddit", "client_secret") or os.environ.get("REDDIT_CLIENT_SECRET")
    if not cid or not secret:
        return _result(False, blocked=True,
                       note=("PRAW is installed but has no Reddit app credentials. "
                             "Add a vault credential with platform 'reddit' holding "
                             "client_id and client_secret, or set REDDIT_CLIENT_ID "
                             "and REDDIT_CLIENT_SECRET."),
                       manual_url="https://www.reddit.com/search/?q=" + quote_plus(query))

    try:
        reddit = praw.Reddit(client_id=cid, client_secret=secret,
                             user_agent="profiler-signal-monitor/1.0",
                             check_for_async=False)
        target = reddit.subreddit(subreddit or "all")
        posts = []
        for sub in target.search(query, sort="new", limit=min(limit, 100)):
            when = datetime.fromtimestamp(sub.created_utc, timezone.utc).replace(tzinfo=None) if sub.created_utc else None
            if since and when and when < since:
                continue
            body = (sub.title or "")
            if getattr(sub, "selftext", ""):
                body += " - " + sub.selftext
            posts.append({
                "platform": "Reddit",
                "author": str(sub.author) if sub.author else "[deleted]",
                "handle": str(sub.author) if sub.author else "",
                "verified": False,
                "text": body[:4000],
                "url": "https://www.reddit.com" + sub.permalink,
                "posted_at": _iso(when) if when else None,
                "source_url": "praw://" + (subreddit or "all"),
                "extra": {"score": sub.score, "comments": sub.num_comments,
                          "subreddit": str(sub.subreddit)},
            })
        return _result(True, posts, "Fetched %d Reddit post(s) via the API" % len(posts))
    except Exception as e:
        return _result(False, note="PRAW failed: %s" % type(e).__name__,
                       manual_url="https://www.reddit.com/search/?q=" + quote_plus(query))


def fetch_telegram(query, limit=MAX_PER_SOURCE, since=None, channel=None):
    """Public Telegram channel history via Telethon.

    Telegram carries a large share of coordinated messaging, and public
    channels are readable with an API id. Without one, fall back to the
    web preview, which serves public channels without auth.
    """
    if not channel:
        return _result(False, blocked=True,
                       note=("Telegram needs a public channel name (for example "
                             "'somechannel'), not a keyword. Add it as the source URL."),
                       manual_url="https://t.me/s/" + quote_plus(query.split()[0] if query.split() else ""))

    telethon = capabilities.load("telethon")
    api_id = _cred("telegram", "api_id") or os.environ.get("TELEGRAM_API_ID")
    api_hash = _cred("telegram", "api_hash") or os.environ.get("TELEGRAM_API_HASH")

    # Web-preview fallback: t.me/s/<channel> renders public posts as plain HTML
    # and needs no credentials at all.
    if telethon is None or not api_id or not api_hash:
        name = re.sub(r"^https?://t\.me/(s/)?", "", channel).strip("/")
        page = fetch_page("https://t.me/s/" + quote_plus(name), limit=limit)
        if page["ok"] and page["posts"]:
            for p in page["posts"]:
                p["platform"] = "Telegram"
                p["handle"] = "@" + name
                p["author"] = name
            page["note"] = ("Fetched %d post(s) from the public web preview "
                            "(no API credentials configured)" % len(page["posts"]))
            return page
        return _result(False, blocked=True,
                       note=("Telegram API credentials are not configured and the "
                             "public web preview returned nothing. Set "
                             "TELEGRAM_API_ID and TELEGRAM_API_HASH, or check the "
                             "channel is public."),
                       manual_url="https://t.me/s/" + quote_plus(name))

    try:
        from telethon.sync import TelegramClient
        from telethon.sessions import StringSession
        sess = _cred("telegram", "session") or ""
        posts = []
        with TelegramClient(StringSession(sess), int(api_id), api_hash) as client:
            for msg in client.iter_messages(channel, limit=min(limit, 100)):
                if not msg.text:
                    continue
                when = msg.date.replace(tzinfo=None) if msg.date else None
                if since and when and when < since:
                    break
                posts.append({
                    "platform": "Telegram",
                    "author": getattr(msg.sender, "username", None) or channel,
                    "handle": "@" + str(channel).lstrip("@"),
                    "verified": False,
                    "text": msg.text[:4000],
                    "url": "https://t.me/%s/%d" % (str(channel).lstrip("@"), msg.id),
                    "posted_at": _iso(when) if when else None,
                    "source_url": "telegram://" + str(channel),
                    "extra": {"views": getattr(msg, "views", None),
                              "forwards": getattr(msg, "forwards", None)},
                })
        return _result(True, posts, "Fetched %d Telegram message(s)" % len(posts))
    except Exception as e:
        return _result(False, note="Telethon failed: %s" % type(e).__name__,
                       manual_url="https://t.me/s/" + quote_plus(str(channel).lstrip("@")))


def fetch_tweepy(query, limit=MAX_PER_SOURCE, since=None):
    """X / Twitter via the official API.

    Requires a paid API tier; there is no free search endpoint any more. Said
    plainly rather than discovered through an empty result.
    """
    tweepy = capabilities.load("tweepy")
    manual = "https://x.com/search?q=%s&f=live" % quote_plus(query)
    if tweepy is None:
        return _needs("tweepy", query, manual)

    token = _cred("twitter", "bearer_token") or os.environ.get("TWITTER_BEARER_TOKEN")
    if not token:
        return _result(False, blocked=True,
                       note=("Tweepy is installed but no bearer token is configured. "
                             "X search requires a paid API tier. Add a vault "
                             "credential with platform 'twitter', or set "
                             "TWITTER_BEARER_TOKEN."),
                       manual_url=manual)
    try:
        client = tweepy.Client(bearer_token=token)
        resp = client.search_recent_tweets(
            query=query[:512], max_results=min(max(limit, 10), 100),
            tweet_fields=["created_at", "public_metrics", "author_id"],
            expansions=["author_id"], user_fields=["username", "name", "verified"])
        users = {u.id: u for u in (resp.includes or {}).get("users", [])}
        posts = []
        for tw in (resp.data or []):
            when = tw.created_at.replace(tzinfo=None) if tw.created_at else None
            if since and when and when < since:
                continue
            u = users.get(tw.author_id)
            posts.append({
                "platform": "X",
                "author": (u.name if u else "") or "unknown",
                "handle": (u.username if u else "") or "",
                "verified": bool(getattr(u, "verified", False)),
                "text": (tw.text or "")[:4000],
                "url": "https://x.com/%s/status/%s" % (
                    (u.username if u else "i"), tw.id),
                "posted_at": _iso(when) if when else None,
                "source_url": "tweepy://search",
                "extra": dict(tw.public_metrics or {}),
            })
        return _result(True, posts, "Fetched %d post(s) from the X API" % len(posts))
    except Exception as e:
        return _result(False, note="X API call failed: %s" % type(e).__name__,
                       manual_url=manual)


def fetch_instagram(query, limit=MAX_PER_SOURCE, since=None, profile=None):
    """Public Instagram profile posts via Instaloader."""
    manual = "https://www.instagram.com/explore/tags/%s/" % quote_plus(
        re.sub(r"[^A-Za-z0-9]", "", query))
    il = capabilities.load("instaloader")
    if il is None:
        return _needs("instaloader", query, manual)
    target = (profile or "").strip().lstrip("@")
    if not target:
        return _result(False, blocked=True,
                       note=("Instaloader reads a named public profile, not a keyword "
                             "search. Put the username in the source URL field."),
                       manual_url=manual)
    try:
        L = il.Instaloader(quiet=True, download_pictures=False,
                           download_videos=False, download_comments=False,
                           save_metadata=False)
        prof = il.Profile.from_username(L.context, target)
        posts = []
        for post in prof.get_posts():
            if len(posts) >= limit:
                break
            when = post.date_utc
            if since and when and when < since:
                break
            posts.append({
                "platform": "Instagram",
                "author": prof.full_name or target,
                "handle": target,
                "verified": bool(prof.is_verified),
                "text": (post.caption or "")[:4000],
                "url": "https://www.instagram.com/p/%s/" % post.shortcode,
                "posted_at": _iso(when) if when else None,
                "source_url": "instaloader://" + target,
                "extra": {"likes": post.likes, "comments": post.comments},
            })
        return _result(True, posts, "Fetched %d Instagram post(s) from @%s"
                       % (len(posts), target))
    except Exception as e:
        return _result(False, blocked=True,
                       note=("Instagram refused the request (%s). It rate-limits "
                             "hard and blocks unauthenticated collection."
                             % type(e).__name__),
                       manual_url=manual)


def fetch_video(query, limit=MAX_PER_SOURCE, since=None, url=None):
    """Public video metadata via yt-dlp.

    Works across YouTube, TikTok and many other hosts without a key, and never
    downloads the media itself -- metadata only.
    """
    ytdlp = capabilities.load("yt_dlp")
    manual = "https://www.youtube.com/results?search_query=" + quote_plus(query)
    if ytdlp is None:
        return _needs("yt_dlp", query, manual)

    target = url or ("ytsearch%d:%s" % (min(limit, 25), query))
    opts = {"quiet": True, "skip_download": True, "extract_flat": "in_playlist",
            "noplaylist": False, "ignoreerrors": True, "socket_timeout": TIMEOUT}
    try:
        with ytdlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(target, download=False)
    except Exception as e:
        return _result(False, note="yt-dlp failed: %s" % type(e).__name__,
                       manual_url=manual)

    entries = info.get("entries") if isinstance(info, dict) else None
    if entries is None:
        entries = [info] if info else []

    posts = []
    for e in entries[:limit]:
        if not e:
            continue
        stamp = e.get("upload_date") or ""
        when = None
        if len(stamp) == 8 and stamp.isdigit():
            try:
                when = datetime.strptime(stamp, "%Y%m%d")
            except ValueError:
                when = None
        if since and when and when < since:
            continue
        body = (e.get("title") or "")
        if e.get("description"):
            body += " - " + e["description"]
        if not body.strip():
            continue
        posts.append({
            "platform": "YouTube" if "youtube" in (e.get("webpage_url") or target)
                        else "Video",
            "author": e.get("uploader") or e.get("channel") or "unknown",
            "handle": e.get("uploader_id") or e.get("channel_id") or "",
            "verified": False,
            "text": body[:4000],
            "url": e.get("webpage_url") or e.get("url") or "",
            "posted_at": _iso(when) if when else None,
            "source_url": "yt-dlp://" + str(target)[:80],
            "extra": {"views": e.get("view_count"), "duration": e.get("duration")},
        })
    return _result(True, posts, "Fetched metadata for %d video(s)" % len(posts))


def fetch_article(query, limit=MAX_PER_SOURCE, since=None, url=None):
    """Clean article text via trafilatura, falling back to BeautifulSoup.

    `fetch_page` grabs paragraph blocks with regex, which drags in navigation
    and cookie banners. When trafilatura is installed this returns the actual
    article body instead.
    """
    target = url or query
    if not re.match(r"^https?://", target or "", re.I):
        return _result(False, note="Enter a full http:// or https:// URL")

    traf = capabilities.load("trafilatura")
    if traf is not None:
        try:
            downloaded = traf.fetch_url(target)
            if downloaded:
                text = traf.extract(downloaded, include_comments=False,
                                    include_tables=False, no_fallback=False)
                meta = traf.extract_metadata(downloaded)
                if text:
                    when = parse_dt(getattr(meta, "date", None)) if meta else None
                    return _result(True, [{
                        "platform": "News site",
                        "author": (getattr(meta, "author", None)
                                   or getattr(meta, "sitename", None)
                                   or _host(target)),
                        "handle": _host(target),
                        "verified": False,
                        "text": ((getattr(meta, "title", "") or "") + " - " + text)[:4000],
                        "url": target,
                        "posted_at": _iso(when) if when else None,
                        "source_url": target,
                    }], "Extracted the article body with trafilatura")
        except Exception:
            pass  # fall through to the parser below

    bs4 = capabilities.load("beautifulsoup4")
    if bs4 is not None:
        try:
            resp = http_get(target)
            resp.raise_for_status()
            soup = bs4.BeautifulSoup(resp.text, "html.parser")
            for tag in soup(["script", "style", "nav", "footer", "header",
                             "aside", "form"]):
                tag.decompose()
            title = (soup.title.string or "").strip() if soup.title else ""
            body = " ".join(p.get_text(" ", strip=True)
                            for p in soup.find_all(["p", "li", "blockquote"]))
            body = re.sub(r"\s+", " ", body).strip()
            if len(body) >= 60:
                return _result(True, [{
                    "platform": "Web page",
                    "author": title or _host(target),
                    "handle": _host(target),
                    "verified": False,
                    "text": ((title + " - ") if title else "") + body[:4000],
                    "url": target, "posted_at": None, "source_url": target,
                }], "Extracted the page text with BeautifulSoup")
        except Exception:
            pass

    return fetch_page(target, limit=limit)


def fetch_browser(query, limit=MAX_PER_SOURCE, since=None, url=None):
    """Render a JavaScript-heavy page with Playwright and read the result.

    This is the last resort for sites that render nothing server-side. It is
    unavailable in serverless deployments, which the capability layer reports.
    """
    manual = url or ("https://www.google.com/search?q=" + quote_plus(query))
    if not url:
        return _result(False, note="Browser rendering needs a URL to open",
                       blocked=True, manual_url=manual)
    if not capabilities.available("playwright"):
        return _needs("playwright", query, manual)
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page(user_agent=USER_AGENTS[0])
            page.goto(url, timeout=TIMEOUT * 1000, wait_until="domcontentloaded")
            page.wait_for_timeout(1200)
            title = page.title()
            body = page.inner_text("body")
            browser.close()
    except Exception as e:
        return _result(False, note="Browser render failed: %s" % type(e).__name__,
                       blocked=True, manual_url=manual)

    chunks = [c.strip() for c in re.split(r"\n{2,}", body or "") if len(c.strip()) >= 60]
    posts = [{
        "platform": "Web page", "author": title or _host(url), "handle": _host(url),
        "verified": False, "text": c[:4000], "url": url,
        "posted_at": None, "source_url": url,
    } for c in chunks[:limit]]
    if not posts:
        return _result(False, note="The page rendered but held no readable text")
    return _result(True, posts, "Rendered the page and read %d block(s)" % len(posts))


# -- Login-walled platforms --------------------------------------------------

_MANUAL = {
    "facebook": ("Facebook", "https://www.facebook.com/search/posts?q=%s"),
    "instagram": ("Instagram", "https://www.instagram.com/explore/tags/%s/"),
    "tiktok": ("TikTok", "https://www.tiktok.com/search?q=%s"),
    "x": ("X", "https://x.com/search?q=%s&f=live"),
    "linkedin": ("LinkedIn", "https://www.linkedin.com/search/results/content/?keywords=%s"),
}


def manual_only(kind, query):
    name, tmpl = _MANUAL.get(kind, ("That platform", "https://www.google.com/search?q=%s"))
    return _result(
        False, blocked=True,
        note=("%s requires a logged-in session and blocks automated requests. "
              "Open the search, then paste results into Add post or Import." % name),
        manual_url=tmpl % quote_plus(query))


def dork_urls(query, domains=None):
    """Search-engine queries that surface social posts without scraping."""
    sites = domains or ["facebook.com", "x.com", "twitter.com", "instagram.com",
                        "tiktok.com", "reddit.com", "linkedin.com"]
    out = [{"label": "All sites",
            "url": "https://www.google.com/search?q=" + quote_plus(query)}]
    for s in sites:
        out.append({"label": s,
                    "url": "https://www.google.com/search?q="
                           + quote_plus("site:%s %s" % (s, query))})
    return out


# -- Facebook and X ----------------------------------------------------------
#
# Both platforms are login-walled and block scripted requests hard. What used
# to happen here was a single "open this search yourself" link, which is barely
# a feature. What actually works, measured rather than assumed:
#
#   Google News with a site: restriction   WORKS -- returns real post text,
#                                          author handles and timestamps
#   Bing RSS with site:                    silently IGNORES the restriction and
#                                          returns unrelated web results
#   Nitter mirrors                         serve an anti-bot challenge page
#   mbasic.facebook.com                    400s without a session
#   syndication.twitter.com                429s
#
# So the strategy is: pull what the search indexes already hold, verify it
# really came from the platform, and hand back precise dorks for the rest
# instead of one generic link. A stored session (see authfetch.py) still beats
# all of this when one is available.

# Hosts that count as "really from this platform", so an index that quietly
# drops the site: restriction cannot smuggle unrelated results into a watch.
_PLATFORM_HOSTS = {
    "facebook": ("facebook.com", "fb.com", "fb.watch", "m.facebook.com",
                 "web.facebook.com", "mbasic.facebook.com"),
    "x": ("x.com", "twitter.com", "mobile.twitter.com", "nitter.net"),
}

# Where a post's author lives in each platform's URL.
_HANDLE_PATTERNS = {
    "x": [re.compile(r"(?:x|twitter)\.com/([A-Za-z0-9_]{1,15})(?:/status/\d+)?", re.I)],
    "facebook": [
        re.compile(r"facebook\.com/([A-Za-z0-9.\-]{3,60})/(?:posts|videos|photos)/", re.I),
        re.compile(r"facebook\.com/(?:pg/)?([A-Za-z0-9.\-]{3,60})/?(?:\?|$)", re.I),
    ],
}

# Newsroom section labels that lead a post but name no account.
_SECTION_LABELS = {
    "look", "watch", "read", "breaking", "just in", "update", "updates",
    "live", "developing", "exclusive", "opinion", "editorial", "analysis",
    "explainer", "in photos", "in numbers", "timeline", "recap", "fact check",
    "alert", "advisory", "news", "video", "photo", "story", "special report",
}

# URL segments that are Facebook/X plumbing, never an account name.
_NOT_A_HANDLE = {
    "search", "hashtag", "i", "home", "explore", "notifications", "messages",
    "settings", "login", "share", "intent", "watch", "events", "groups",
    "marketplace", "pages", "profile.php", "permalink.php", "story.php",
    "photo.php", "video.php", "help", "policies", "privacy", "terms", "about",
    "status", "statuses", "media", "likes", "with_replies",
}


def _platform_handle(url, kind):
    """Pull the author's handle out of a post URL, or '' when it is not one."""
    for rx in _HANDLE_PATTERNS.get(kind, []):
        m = rx.search(url or "")
        if not m:
            continue
        handle = (m.group(1) or "").strip(".")
        if handle.lower() in _NOT_A_HANDLE or not handle:
            continue
        return handle
    return ""


def _is_platform_url(url, kind):
    host = _host(url)
    return any(host == h or host.endswith("." + h)
               for h in _PLATFORM_HOSTS.get(kind, ()))


def _social_via_index(kind, query, limit, since, extra_terms=None):
    """Search the news index for posts the platform itself will not serve.

    Google News indexes a surprising amount of X and Facebook content, and --
    unlike Bing -- it honours `site:`. Results are filtered back down to the
    platform's own hosts so a silently-dropped restriction cannot leak
    unrelated pages into the watch.
    """
    sites = _PLATFORM_HOSTS[kind][:2]  # the two canonical domains
    site_clause = " OR ".join("site:" + s for s in sites)
    q = "(%s) %s" % (site_clause, query)
    if extra_terms:
        q += " " + extra_terms

    url = ("https://news.google.com/rss/search?q=%s&hl=en-US&gl=US&ceid=US:en"
           % quote_plus(q))
    r = fetch_feed(url, platform=("X" if kind == "x" else "Facebook"),
                   limit=limit * 3, since=since)
    if not r["ok"]:
        return [], r["note"]

    label = "X" if kind == "x" else "Facebook"
    hosts = _PLATFORM_HOSTS[kind]
    posts, seen = [], set()
    for p in r["posts"]:
        # Google News puts the publisher in `author` -- literally "x.com" or
        # "facebook.com" for these. That is the origin claim we can check,
        # because the " - x.com" suffix has already been stripped from the
        # text by the feed parser. A direct platform URL counts too.
        claimed = (p.get("author") or "").strip().lower() in hosts
        direct = _is_platform_url(p.get("url") or "", kind)
        if not claimed and not direct:
            continue

        text = p.get("text") or ""
        # The aggregator tags the origin onto the end, sometimes after a dash
        # and sometimes not, so strip it either way rather than leaving
        # "facebook.com" dangling on the end of a post.
        body = re.sub(r"(?:\s[-–]\s|\s+)"
                      r"(?:x|twitter|facebook|fb)\.com\s*$",
                      "", text.strip(), flags=re.I)
        # Aggregator titles repeat the body as "headline — body"; keep the
        # longer half rather than storing the sentence twice.
        if " — " in body:
            head, _, tail = body.partition(" — ")
            if tail.startswith(head[:40]):
                body = tail
        body = body.strip()
        if len(body) < 25:
            continue

        key = body[:180].lower()
        if key in seen:
            continue
        seen.add(key)

        handle = _platform_handle(p.get("url") or "", kind)

        # With an opaque aggregator link there is no URL to read a handle from,
        # but posts routinely carry the account in the text: an "@handle", a
        # "Page Name:" prefix, or a shouted "SECTION |" banner. Recovering it
        # is the difference between 40 posts all attributed to "Facebook" and
        # 40 posts you can actually group by who said them.
        if not handle:
            m = re.search(r"(?:^|\s)@([A-Za-z0-9_.]{3,30})", body)
            if m and m.group(1).lower() not in _NOT_A_HANDLE:
                handle = m.group(1)

        # A "Name." or "Name:" prefix is a real byline; an ALL-CAPS banner like
        # "BARMM ELECTIONS |" or "LOOK:" is a headline the outlet shouted, not
        # an account. Guessing wrong is worse than not guessing, because a
        # fabricated author silently corrupts the repeat-actor analysis.
        author = handle
        if not author:
            m = re.match(r"([A-Z][\w.'&-]*(?:\s+[A-Z][\w.'&-]*){0,3})\s*[.:|]\s", body)
            if m:
                cand = m.group(1).strip().rstrip(".")
                # Newsroom banners are section labels, not accounts: an
                # all-caps prefix is always one, and the common ones show up
                # title-cased too ("Watch:", "Look:").
                shouted = cand.upper() == cand
                if (3 <= len(cand) <= 50 and not shouted
                        and cand.lower() not in _SECTION_LABELS):
                    author = cand
        if not author:
            author = label

        posts.append({
            "platform": label,
            "author": author[:120],
            "handle": handle,
            "verified": False,
            "text": body[:4000],
            "url": p.get("url") or "",
            "link_kind": p.get("link_kind") or "search",
            "posted_at": p.get("posted_at"),
            "source_url": url,
            "extra": {"via": "news-index"},
        })
        if len(posts) >= limit:
            break

    return posts, "Found %d %s post(s) in the news index" % (len(posts), label)


def _social_dorks(kind, query, domains=None):
    """Targeted searches for what the index does not reach.

    One generic link is not much help. These are the queries an analyst would
    actually type: recent posts, the discussion around a hashtag, named pages,
    and the platform's own search with a live filter.
    """
    q = quote_plus(query)
    bare = re.sub(r"[^A-Za-z0-9 ]", "", query).strip()
    tag = quote_plus(bare.split()[0]) if bare.split() else q

    if kind == "x":
        return [
            {"label": "X search — latest",
             "url": "https://x.com/search?q=%s&f=live" % q},
            {"label": "X search — top",
             "url": "https://x.com/search?q=%s&f=top" % q},
            {"label": "X — links only",
             "url": "https://x.com/search?q=%s%%20filter%%3Alinks&f=live" % q},
            {"label": "X — media only",
             "url": "https://x.com/search?q=%s%%20filter%%3Amedia&f=live" % q},
            {"label": "X — verified accounts",
             "url": "https://x.com/search?q=%s%%20filter%%3Averified&f=live" % q},
            {"label": "X — replies excluded",
             "url": "https://x.com/search?q=%s%%20-filter%%3Areplies&f=live" % q},
            {"label": "Hashtag #%s" % bare.split()[0] if bare.split() else "Hashtag",
             "url": "https://x.com/hashtag/%s?f=live" % tag},
            {"label": "Google — x.com posts",
             "url": "https://www.google.com/search?q=%s" % quote_plus(
                 "site:x.com OR site:twitter.com " + query)},
            {"label": "Google — last 24h on x.com",
             "url": "https://www.google.com/search?tbs=qdr:d&q=%s" % quote_plus(
                 "site:x.com " + query)},
        ]

    return [
        {"label": "Facebook — posts",
         "url": "https://www.facebook.com/search/posts?q=%s" % q},
        {"label": "Facebook — pages",
         "url": "https://www.facebook.com/search/pages?q=%s" % q},
        {"label": "Facebook — groups",
         "url": "https://www.facebook.com/search/groups?q=%s" % q},
        {"label": "Facebook — people",
         "url": "https://www.facebook.com/search/people?q=%s" % q},
        {"label": "Facebook — videos",
         "url": "https://www.facebook.com/search/videos?q=%s" % q},
        {"label": "Hashtag #%s" % (bare.split()[0] if bare.split() else ""),
         "url": "https://www.facebook.com/hashtag/%s" % tag},
        {"label": "Google — facebook.com",
         "url": "https://www.google.com/search?q=%s" % quote_plus(
             "site:facebook.com " + query)},
        {"label": "Google — last 24h on facebook.com",
         "url": "https://www.google.com/search?tbs=qdr:d&q=%s" % quote_plus(
             "site:facebook.com " + query)},
        {"label": "Google — public group posts",
         "url": "https://www.google.com/search?q=%s" % quote_plus(
             "site:facebook.com/groups " + query)},
    ]


def fetch_x(query, limit=MAX_PER_SOURCE, since=None, handle=None, use_api=True):
    """Collect X posts through whatever route is actually open.

    Order of preference: the official API when a token exists, then the news
    index, then mirrors, then honest dorks. Each fallback explains itself, so
    an empty result never looks like "nothing was posted".
    """
    manual = "https://x.com/search?q=%s&f=live" % quote_plus(query)

    # 1. The official API, if a token is configured.
    if use_api and (_cred("twitter", "bearer_token")
                    or os.environ.get("TWITTER_BEARER_TOKEN")):
        api = fetch_tweepy(query, limit, since)
        if api["ok"] and api["posts"]:
            return api

    notes = []
    posts = []

    # 2. What the news index already holds.
    if handle:
        indexed, note = _social_via_index("x", "from:%s %s" % (handle, query),
                                          limit, since)
    else:
        indexed, note = _social_via_index("x", query, limit, since)
    posts.extend(indexed)
    notes.append(note)

    # 3. Mirrors, which are usually down but cost little to try.
    if len(posts) < limit:
        mirrored = fetch_via_mirrors(query, "nitter", limit - len(posts), since=since)
        if mirrored["ok"] and mirrored["posts"]:
            posts.extend(mirrored["posts"])
            notes.append("plus %d via a Nitter mirror" % len(mirrored["posts"]))

    dorks = _social_dorks("x", query)
    if posts:
        return _result(True, posts[:limit],
                       "%s. X blocks direct scraping, so these came from the "
                       "search index -- open the dork links for the rest."
                       % "; ".join(n for n in notes if n),
                       manual_url=manual, dorks=dorks)

    return _result(
        False, blocked=True,
        note=("X serves no public search and its mirrors are offline. "
              "%s. Use the dork links, a stored session in the vault, or an "
              "API token." % (notes[0] if notes else "Nothing in the index")),
        manual_url=manual, dorks=dorks)


def fetch_facebook(query, limit=MAX_PER_SOURCE, since=None, page=None):
    """Collect Facebook posts through whatever route is actually open.

    Facebook has no public search API and blocks unauthenticated requests.
    Public *pages* do still publish readable content, so a named page is worth
    fetching directly; otherwise fall back to the index and dorks.
    """
    manual = "https://www.facebook.com/search/posts?q=%s" % quote_plus(query)
    notes, posts = [], []

    # 1. A named public page, which sometimes renders without a session.
    if page:
        name = re.sub(r"^https?://(?:www\.|m\.|mbasic\.)?facebook\.com/", "",
                      str(page)).strip("/").split("?")[0]
        for host in ("mbasic.facebook.com", "m.facebook.com"):
            try:
                resp = http_get("https://%s/%s" % (host, quote_plus(name)))
                if resp.status_code == 200 and "login" not in resp.url.lower():
                    blocks = re.findall(
                        r"(?is)<(?:p|div)[^>]*>([^<]{60,1200})</(?:p|div)>", resp.text)
                    for b in blocks[:limit]:
                        text = _clean(b)
                        if len(text) < 60:
                            continue
                        posts.append({
                            "platform": "Facebook", "author": name,
                            "handle": name, "verified": False,
                            "text": text[:4000],
                            "url": "https://www.facebook.com/" + name,
                            "link_kind": "direct", "posted_at": None,
                            "source_url": resp.url,
                        })
                    if posts:
                        notes.append("read %d block(s) from the public page" % len(posts))
                        break
            except requests.exceptions.RequestException:
                continue
        if not posts:
            notes.append("the public page did not render without a session")

    # 2. The news index.
    if len(posts) < limit:
        indexed, note = _social_via_index(
            "facebook", ('"%s" %s' % (page, query)) if page else query,
            limit - len(posts), since)
        posts.extend(indexed)
        notes.append(note)

    dorks = _social_dorks("facebook", query)
    if posts:
        return _result(True, posts[:limit],
                       "%s. Facebook blocks direct scraping, so most of this came "
                       "from the search index -- open the dork links for the rest."
                       % "; ".join(n for n in notes if n),
                       manual_url=manual, dorks=dorks)

    return _result(
        False, blocked=True,
        note=("Facebook has no public search and blocks unauthenticated "
              "requests. %s. Use the dork links, or add a session to the vault."
              % "; ".join(n for n in notes if n)),
        manual_url=manual, dorks=dorks)


# -- Dispatch ----------------------------------------------------------------

def _opt(o, key, default=None):
    """Read a per-source option, falling back to the shared options dict."""
    if not isinstance(o, dict):
        return default
    v = o.get(key)
    return default if v in (None, "") else v


COLLECTORS = {
    # open, no key
    "google_news": lambda q, o: fetch_google_news(q, _opt(o, "limit", MAX_PER_SOURCE),
                                                  _opt(o, "since"), _opt(o, "region")),
    "bing_news": lambda q, o: fetch_bing_news(q, _opt(o, "limit", MAX_PER_SOURCE),
                                              _opt(o, "since")),
    "all_feeds": lambda q, o: fetch_all_feeds(q, _opt(o, "limit", MAX_PER_SOURCE),
                                              _opt(o, "since")),
    "reddit": lambda q, o: fetch_reddit(q, _opt(o, "limit", MAX_PER_SOURCE),
                                        _opt(o, "subreddit"), _opt(o, "sort", "new"),
                                        _opt(o, "since")),
    "hackernews": lambda q, o: fetch_hackernews(q, _opt(o, "limit", MAX_PER_SOURCE),
                                                _opt(o, "since")),
    "mastodon": lambda q, o: fetch_mastodon(q, _opt(o, "limit", MAX_PER_SOURCE),
                                            _opt(o, "instance"), _opt(o, "since")),
    "lemmy": lambda q, o: fetch_lemmy(q, _opt(o, "limit", MAX_PER_SOURCE),
                                      _opt(o, "instance"), _opt(o, "since")),
    "wikipedia": lambda q, o: fetch_wikipedia(q, _opt(o, "limit", MAX_PER_SOURCE),
                                              _opt(o, "since")),
    "rss": lambda q, o: fetch_feed(_opt(o, "url") or q, _opt(o, "platform"),
                                   _opt(o, "limit", MAX_PER_SOURCE), _opt(o, "since")),
    "page": lambda q, o: fetch_page(_opt(o, "url") or q, _opt(o, "limit", MAX_PER_SOURCE)),
    "article": lambda q, o: fetch_article(q, _opt(o, "limit", MAX_PER_SOURCE),
                                          _opt(o, "since"), _opt(o, "url")),
    "youtube": lambda q, o: fetch_youtube(q, _opt(o, "limit", MAX_PER_SOURCE),
                                          _opt(o, "channel_id") or _opt(o, "url"),
                                          _opt(o, "since")),
    "video": lambda q, o: fetch_video(q, _opt(o, "limit", MAX_PER_SOURCE),
                                      _opt(o, "since"), _opt(o, "url")),
    "nitter": lambda q, o: fetch_via_mirrors(q, "nitter", _opt(o, "limit", MAX_PER_SOURCE),
                                             _opt(o, "since")),
    "teddit": lambda q, o: fetch_via_mirrors(q, "teddit", _opt(o, "limit", MAX_PER_SOURCE),
                                             _opt(o, "since")),

    # library-backed, key or session required
    "praw": lambda q, o: fetch_praw(q, _opt(o, "limit", MAX_PER_SOURCE),
                                    _opt(o, "since"), _opt(o, "subreddit")),
    "telegram": lambda q, o: fetch_telegram(q, _opt(o, "limit", MAX_PER_SOURCE),
                                            _opt(o, "since"),
                                            _opt(o, "channel") or _opt(o, "url")),
    "tweepy": lambda q, o: fetch_tweepy(q, _opt(o, "limit", MAX_PER_SOURCE),
                                        _opt(o, "since")),
    "instagram_api": lambda q, o: fetch_instagram(q, _opt(o, "limit", MAX_PER_SOURCE),
                                                  _opt(o, "since"),
                                                  _opt(o, "profile") or _opt(o, "url")),
    "browser": lambda q, o: fetch_browser(q, _opt(o, "limit", MAX_PER_SOURCE),
                                          _opt(o, "since"), _opt(o, "url")),

    # dork-only
    "pastes": lambda q, o: fetch_paste_dorks(q, _opt(o, "limit", MAX_PER_SOURCE),
                                             _opt(o, "since")),
    # Facebook and X now collect what the search index holds and return
    # targeted dorks for the rest, rather than one generic link.
    "facebook": lambda q, o: fetch_facebook(q, _opt(o, "limit", MAX_PER_SOURCE),
                                            _opt(o, "since"),
                                            _opt(o, "page") or _opt(o, "url")),
    "x": lambda q, o: fetch_x(q, _opt(o, "limit", MAX_PER_SOURCE),
                              _opt(o, "since"),
                              _opt(o, "handle") or _opt(o, "url")),
    "instagram": lambda q, o: manual_only("instagram", q),
    "tiktok": lambda q, o: manual_only("tiktok", q),
    "linkedin": lambda q, o: manual_only("linkedin", q),
}

# `live` drives the green/amber dot in the UI. `cap` names the optional
# capability a source needs, so the UI can grey it out and explain why rather
# than letting the analyst select something that cannot run.
SOURCE_META = [
    {"key": "google_news", "name": "Google News", "live": True, "group": "News",
     "desc": "Keyword search across indexed news outlets. Honours the date filter."},
    {"key": "bing_news", "name": "Bing News", "live": True, "group": "News",
     "desc": "Second news index; catches what Google misses."},
    {"key": "all_feeds", "name": "All configured feeds", "live": True, "group": "News",
     "desc": "Sweeps every fixed newsroom feed in sources.json at once."},
    {"key": "rss", "name": "RSS / Atom feed", "live": True, "needs_url": True,
     "group": "News", "desc": "Any feed URL, including per-site and per-channel feeds."},
    {"key": "article", "name": "Article extractor", "live": True, "needs_url": True,
     "group": "News", "cap": "trafilatura",
     "desc": "Pulls the clean article body from a news page, dropping nav and ads."},
    {"key": "page", "name": "Web page", "live": True, "needs_url": True,
     "group": "News", "desc": "Pull readable text blocks from a public page."},

    {"key": "reddit", "name": "Reddit", "live": True, "group": "Social",
     "desc": "Public JSON search with an RSS fallback. Optional subreddit filter."},
    {"key": "praw", "name": "Reddit (API)", "live": True, "group": "Social",
     "cap": "praw",
     "desc": "Authenticated Reddit. Avoids the datacenter-IP blocks entirely."},
    {"key": "hackernews", "name": "Hacker News", "live": True, "group": "Social",
     "desc": "Stories and comments via the Algolia index."},
    {"key": "mastodon", "name": "Mastodon", "live": True, "group": "Social",
     "desc": "Public hashtag timeline from any instance. No key needed."},
    {"key": "lemmy", "name": "Lemmy", "live": True, "group": "Social",
     "desc": "Fediverse link aggregator. Open search API."},
    {"key": "telegram", "name": "Telegram channel", "live": True, "needs_url": True,
     "group": "Social", "cap": "telethon",
     "desc": "Public channel history. Falls back to the web preview without a key."},
    {"key": "tweepy", "name": "X (API)", "live": True, "group": "Social",
     "cap": "tweepy",
     "desc": "Official X API. Requires a paid tier; there is no free search."},
    {"key": "instagram_api", "name": "Instagram profile", "live": True,
     "needs_url": True, "group": "Social", "cap": "instaloader",
     "desc": "Public profile posts. Rate-limited hard by Instagram."},
    {"key": "nitter", "name": "X via Nitter mirror", "live": False, "group": "Social",
     "desc": "Tries volunteer mirrors; often all offline."},
    {"key": "teddit", "name": "Reddit via Teddit mirror", "live": False,
     "group": "Social", "desc": "Fallback when Reddit rate-limits."},

    {"key": "video", "name": "Video metadata", "live": True, "group": "Media",
     "cap": "yt_dlp",
     "desc": "Titles, descriptions and channels from YouTube, TikTok and more."},
    {"key": "youtube", "name": "YouTube channel feed", "live": True, "group": "Media",
     "desc": "Per-channel Atom feed. Needs a channel ID."},
    {"key": "browser", "name": "Rendered page", "live": True, "needs_url": True,
     "group": "Media", "cap": "playwright",
     "desc": "Runs a real browser for pages that render nothing server-side."},

    {"key": "wikipedia", "name": "Wikipedia", "live": True, "group": "Reference",
     "desc": "Articles mentioning the subject, most recently edited first."},
    {"key": "pastes", "name": "Paste sites", "live": False, "group": "Reference",
     "desc": "Dork for leaked data on pastebin and friends. Opens the search."},

    {"key": "x", "name": "X (Twitter)", "live": True, "group": "Social",
     "needs_url": False,
     "desc": "Pulls indexed X posts, then gives targeted dorks. Uses the API "
             "or a vault session when one exists."},
    {"key": "facebook", "name": "Facebook", "live": True, "group": "Social",
     "desc": "Pulls indexed Facebook posts and public pages, then gives "
             "targeted dorks. Add a page name to read it directly."},
    {"key": "instagram", "name": "Instagram", "live": False, "group": "Login-walled",
     "desc": "Login-walled. Opens the tag page for manual collection."},
    {"key": "tiktok", "name": "TikTok", "live": False, "group": "Login-walled",
     "desc": "Login-walled. Opens the search for manual collection."},
    {"key": "linkedin", "name": "LinkedIn", "live": False, "group": "Login-walled",
     "desc": "Login-walled. Opens the search for manual collection."},
]


def source_meta():
    """SOURCE_META with live capability status folded in."""
    out = []
    for s in SOURCE_META:
        row = dict(s)
        cap = s.get("cap")
        if cap:
            info = capabilities.probe(cap)
            row["available"] = info["available"]
            row["cap_label"] = info.get("label", cap)
            row["cap_reason"] = info.get("reason", "")
            row["cap_needs"] = info.get("needs")
            row["cap_caveat"] = info.get("caveat")
        else:
            row["available"] = True
        out.append(row)
    return out


def collect(source_keys, query, options=None, max_workers=8, since=None):
    """Run several collectors in parallel. Returns (posts, per-source report).

    `since` is a naive UTC datetime applied to every collector that supports
    narrowing at the source, so a "last 7 days" run does not pull and discard
    a year of archive.
    """
    options = options or {}
    report, posts = [], []
    started = time.time()

    def opts_for(key):
        base = {k: v for k, v in options.items() if not isinstance(v, dict)}
        base.update(options.get(key) or {})
        if since is not None:
            base.setdefault("since", since)
        return base

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {}
        for key in source_keys:
            fn = COLLECTORS.get(key)
            if not fn:
                report.append({"source": key, "ok": False, "count": 0,
                               "note": "Unknown source", "blocked": False,
                               "manual_url": "", "elapsed": 0})
                continue
            futures[pool.submit(_timed, fn, query, opts_for(key))] = key

        for future in as_completed(futures):
            key = futures[future]
            try:
                r, elapsed = future.result()
            except Exception as e:  # a collector must never take the run down
                r, elapsed = _result(False, note="Collector failed: %s"
                                                 % type(e).__name__), 0
            for p in r["posts"]:
                p.setdefault("source", key)
            posts.extend(r["posts"])
            report.append({"source": key, "ok": r["ok"], "count": len(r["posts"]),
                           "note": r["note"], "blocked": r["blocked"],
                           "manual_url": r["manual_url"],
                           # Targeted follow-up searches for the login-walled
                           # platforms, where what we can fetch is only part
                           # of the picture.
                           "dorks": r.get("dorks") or [],
                           "elapsed": round(elapsed, 2)})

    report.sort(key=lambda r: (not r["ok"], r["source"]))
    return posts, {"sources": report, "elapsed": round(time.time() - started, 2),
                   "total": len(posts),
                   "ok_sources": sum(1 for r in report if r["ok"]),
                   "failed_sources": sum(1 for r in report if not r["ok"])}


def _timed(fn, query, opts):
    """Run one collector, returning its result and how long it took.

    Per-source timing is what tells an analyst which source is slowing a run,
    instead of only seeing one total.
    """
    t0 = time.time()
    try:
        return fn(query, opts), time.time() - t0
    except Exception as e:
        return (_result(False, note="Collector failed: %s" % type(e).__name__),
                time.time() - t0)
