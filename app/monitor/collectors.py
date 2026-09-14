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
"""

import html
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from urllib.parse import parse_qs, quote_plus, unquote, urlparse
from xml.etree import ElementTree

import requests

_SOURCES_PATH = os.path.join(os.path.dirname(__file__), "sources.json")

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/124.0.0.0 Safari/537.36"),
    "Accept-Language": "en-US,en;q=0.9",
}
TIMEOUT = 12
MAX_PER_SOURCE = 40


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


def _parse_date(value):
    if not value:
        return None
    value = str(value).strip()
    fmts = ("%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S %Z",
            "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%d %H:%M:%S", "%Y-%m-%d")
    for fmt in fmts:
        try:
            dt = datetime.strptime(value.replace("GMT", "+0000"), fmt)
            if dt.tzinfo:
                dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
            return _iso(dt)
        except ValueError:
            continue
    return None


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


def _result(ok=True, posts=None, note="", blocked=False, manual_url=""):
    return {"ok": ok, "posts": posts or [], "note": note,
            "blocked": blocked, "manual_url": manual_url}


# -- RSS / Atom --------------------------------------------------------------

def fetch_feed(url, platform=None, limit=MAX_PER_SOURCE):
    """Parse any RSS 2.0 or Atom feed into posts."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
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
    for item in items[:limit]:
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
        published = _parse_date(text_of("pubDate", "atom:published", "atom:updated",
                                        "dc:date"))
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
    return _result(True, posts, note)


# -- Reddit ------------------------------------------------------------------

def fetch_reddit(query, limit=MAX_PER_SOURCE, subreddit=None, sort="new"):
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
        r = fetch_feed(rss, platform="Reddit", limit=limit)
        if r["ok"] and r["posts"]:
            r["note"] = "Fetched %d via Reddit RSS (%s)" % (len(r["posts"]), reason)
            return r
        # Reddit blocks datacenter IPs on both endpoints; try the mirrors next.
        mirrored = fetch_via_mirrors(query, "teddit", limit)
        if mirrored["ok"] and mirrored["posts"]:
            return mirrored
        return _result(
            False, blocked=True,
            note=("Reddit blocked both the JSON API (%s) and RSS. This is an IP-level "
                  "block, not a bad query -- open the search and collect manually." % reason),
            manual_url=search_url)

    try:
        resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
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
        posts.append({
            "platform": "Reddit",
            "author": d.get("author") or "unknown",
            "handle": d.get("author") or "",
            "verified": False,
            "text": combined[:4000],
            "url": "https://www.reddit.com" + (d.get("permalink") or ""),
            "posted_at": _iso(datetime.utcfromtimestamp(created)) if created else None,
            "source_url": url,
            "extra": {"subreddit": d.get("subreddit"), "score": d.get("score"),
                      "comments": d.get("num_comments")},
        })
    return _result(True, posts, "Fetched %d Reddit post(s)" % len(posts))


# -- Hacker News -------------------------------------------------------------

def fetch_hackernews(query, limit=MAX_PER_SOURCE):
    url = ("https://hn.algolia.com/api/v1/search_by_date?query=%s&tags=(story,comment)&hitsPerPage=%d"
           % (quote_plus(query), limit))
    try:
        resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
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

def fetch_google_news(query, limit=MAX_PER_SOURCE):
    url = ("https://news.google.com/rss/search?q=%s&hl=en-US&gl=US&ceid=US:en"
           % quote_plus(query))
    r = fetch_feed(url, platform="News site", limit=limit)
    if r["ok"]:
        r["note"] = "Fetched %d Google News result(s)" % len(r["posts"])
    return r


def fetch_bing_news(query, limit=MAX_PER_SOURCE):
    url = "https://www.bing.com/news/search?q=%s&format=RSS" % quote_plus(query)
    r = fetch_feed(url, platform="News site", limit=limit)
    if r["ok"]:
        r["note"] = "Fetched %d Bing News result(s)" % len(r["posts"])
    return r


def fetch_youtube(query, limit=MAX_PER_SOURCE, channel_id=None):
    """YouTube exposes per-channel Atom feeds without a key. Search needs one."""
    if channel_id:
        url = "https://www.youtube.com/feeds/videos.xml?channel_id=%s" % quote_plus(channel_id)
        return fetch_feed(url, platform="YouTube", limit=limit)
    return _result(
        False, blocked=True,
        note=("YouTube keyword search needs an API key. Per-channel feeds work: "
              "supply a channel ID, or collect manually."),
        manual_url="https://www.youtube.com/results?search_query=%s" % quote_plus(query))


# -- Mirror-based social collection ------------------------------------------

def fetch_via_mirrors(query, kind, limit=MAX_PER_SOURCE):
    """Try each configured mirror in turn until one answers.

    Nitter (X) and Teddit (Reddit) mirrors are volunteer-run and frequently
    offline; we try them in order and report honestly when all fail.
    """
    sources = load_sources()
    mirrors = (sources.get("mirrors") or {}).get(kind) or []
    tried = []
    for tmpl in mirrors:
        url = tmpl.replace("{query}", quote_plus(query))
        r = fetch_feed(url, platform="X" if kind == "nitter" else "Reddit", limit=limit)
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
        resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
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


# -- Dispatch ----------------------------------------------------------------

COLLECTORS = {
    "google_news": lambda q, o: fetch_google_news(q, o.get("limit", MAX_PER_SOURCE)),
    "bing_news": lambda q, o: fetch_bing_news(q, o.get("limit", MAX_PER_SOURCE)),
    "reddit": lambda q, o: fetch_reddit(q, o.get("limit", MAX_PER_SOURCE),
                                        o.get("subreddit"), o.get("sort", "new")),
    "hackernews": lambda q, o: fetch_hackernews(q, o.get("limit", MAX_PER_SOURCE)),
    "youtube": lambda q, o: fetch_youtube(q, o.get("limit", MAX_PER_SOURCE),
                                          o.get("channel_id")),
    "rss": lambda q, o: fetch_feed(o.get("url") or q, o.get("platform"),
                                   o.get("limit", MAX_PER_SOURCE)),
    "page": lambda q, o: fetch_page(o.get("url") or q, o.get("limit", MAX_PER_SOURCE)),
    "nitter": lambda q, o: fetch_via_mirrors(q, "nitter", o.get("limit", MAX_PER_SOURCE)),
    "teddit": lambda q, o: fetch_via_mirrors(q, "teddit", o.get("limit", MAX_PER_SOURCE)),
    "facebook": lambda q, o: manual_only("facebook", q),
    "instagram": lambda q, o: manual_only("instagram", q),
    "tiktok": lambda q, o: manual_only("tiktok", q),
    "x": lambda q, o: manual_only("x", q),
    "linkedin": lambda q, o: manual_only("linkedin", q),
}

SOURCE_META = [
    {"key": "google_news", "name": "Google News", "live": True,
     "desc": "Keyword search across indexed news outlets."},
    {"key": "bing_news", "name": "Bing News", "live": True,
     "desc": "Second news index; catches what Google misses."},
    {"key": "reddit", "name": "Reddit", "live": True,
     "desc": "Public JSON search. Optional subreddit filter."},
    {"key": "hackernews", "name": "Hacker News", "live": True,
     "desc": "Stories and comments via the Algolia index."},
    {"key": "rss", "name": "RSS / Atom feed", "live": True, "needs_url": True,
     "desc": "Any feed URL, including per-site and per-channel feeds."},
    {"key": "page", "name": "Web page", "live": True, "needs_url": True,
     "desc": "Pull readable text blocks from a public page."},
    {"key": "youtube", "name": "YouTube channel", "live": True,
     "desc": "Per-channel Atom feed. Needs a channel ID."},
    {"key": "nitter", "name": "X via Nitter mirror", "live": False,
     "desc": "Tries volunteer mirrors; often all offline."},
    {"key": "teddit", "name": "Reddit via Teddit mirror", "live": False,
     "desc": "Fallback when Reddit rate-limits."},
    {"key": "x", "name": "X (Twitter)", "live": False,
     "desc": "Login-walled. Opens the search for manual collection."},
    {"key": "facebook", "name": "Facebook", "live": False,
     "desc": "Login-walled. Opens the search for manual collection."},
    {"key": "instagram", "name": "Instagram", "live": False,
     "desc": "Login-walled. Opens the tag page for manual collection."},
    {"key": "tiktok", "name": "TikTok", "live": False,
     "desc": "Login-walled. Opens the search for manual collection."},
    {"key": "linkedin", "name": "LinkedIn", "live": False,
     "desc": "Login-walled. Opens the search for manual collection."},
]


def collect(source_keys, query, options=None, max_workers=6):
    """Run several collectors in parallel. Returns (posts, per-source report)."""
    options = options or {}
    report, posts = [], []
    started = time.time()

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {}
        for key in source_keys:
            fn = COLLECTORS.get(key)
            if not fn:
                report.append({"source": key, "ok": False, "count": 0,
                               "note": "Unknown source", "blocked": False,
                               "manual_url": ""})
                continue
            futures[pool.submit(fn, query, options.get(key, options))] = key

        for future in as_completed(futures):
            key = futures[future]
            try:
                r = future.result()
            except Exception as e:  # a collector should never take the run down
                r = _result(False, note="Collector failed: %s" % type(e).__name__)
            for p in r["posts"]:
                p.setdefault("source", key)
            posts.extend(r["posts"])
            report.append({"source": key, "ok": r["ok"], "count": len(r["posts"]),
                           "note": r["note"], "blocked": r["blocked"],
                           "manual_url": r["manual_url"]})

    report.sort(key=lambda r: (not r["ok"], r["source"]))
    return posts, {"sources": report, "elapsed": round(time.time() - started, 2),
                   "total": len(posts)}
