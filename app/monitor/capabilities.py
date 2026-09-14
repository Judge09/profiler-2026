"""Optional-dependency registry.

Every advanced collector and enrichment in this app leans on a third-party
library that may or may not be installed, and several cannot work at all in a
serverless deployment. Rather than let that surface as an ImportError at
request time -- or worse, as a source that silently returns nothing -- each
capability is declared here with:

    * how to detect it
    * what it is for
    * what it needs (a key, a browser binary, nothing)
    * where it will and will not run

The UI reads this to grey out what is unavailable and say why, so an analyst
is never left wondering whether a source found nothing or never ran.

Nothing here imports a heavy library at module load. `probe()` imports lazily
and caches the result, so the cost is paid once and only for what is asked
about.
"""

import importlib
import os
import shutil
import threading

# True when we are running somewhere with no writable disk and no browser --
# Vercel and friends. Anything needing a browser or a large model download is
# unavailable there, and saying so up front is better than timing out.
SERVERLESS = bool(os.environ.get("VERCEL") or os.environ.get("AWS_LAMBDA_FUNCTION_NAME"))

# name -> spec. `module` is what we try to import; `needs` is what the analyst
# must supply beyond `pip install`.
REGISTRY = {
    # -- parsing / extraction ------------------------------------------
    "beautifulsoup4": {
        "module": "bs4", "label": "BeautifulSoup4",
        "role": "HTML parsing",
        "use": "Extracts structured data from public pages far more reliably "
               "than regex.",
        "needs": None, "serverless_ok": True, "group": "parsing",
    },
    "trafilatura": {
        "module": "trafilatura", "label": "trafilatura",
        "role": "Article extraction",
        "use": "Pulls the clean article body out of a news page, dropping nav, "
               "ads and comment furniture.",
        "needs": None, "serverless_ok": True, "group": "parsing",
    },
    "httpx": {
        "module": "httpx", "label": "httpx",
        "role": "HTTP/2 client",
        "use": "HTTP/2 and async fetching; some CDNs treat it more kindly than "
               "requests.",
        "needs": None, "serverless_ok": True, "group": "parsing",
    },

    # -- platform clients ----------------------------------------------
    "praw": {
        "module": "praw", "label": "PRAW",
        "role": "Reddit API",
        "use": "Authenticated Reddit access: full search, subreddit and user "
               "history, without the datacenter-IP blocks.",
        "needs": "reddit_client_id + reddit_client_secret",
        "serverless_ok": True, "group": "platform",
    },
    "mastodon": {
        "module": "mastodon", "label": "Mastodon.py",
        "role": "Mastodon API",
        "use": "Authenticated fediverse search across an instance, including "
               "full-text where the instance enables it.",
        "needs": "mastodon_token (optional for public timelines)",
        "serverless_ok": True, "group": "platform",
    },
    "telethon": {
        "module": "telethon", "label": "Telethon",
        "role": "Telegram API",
        "use": "Public channel and group history. Telegram is where a great "
               "deal of coordinated messaging actually lives.",
        "needs": "telegram_api_id + telegram_api_hash",
        "serverless_ok": False, "group": "platform",
    },
    "tweepy": {
        "module": "tweepy", "label": "Tweepy",
        "role": "X / Twitter API",
        "use": "Official API access to posts, users and timelines.",
        "needs": "twitter_bearer_token (paid X API tier)",
        "serverless_ok": True, "group": "platform",
    },
    "instaloader": {
        "module": "instaloader", "label": "Instaloader",
        "role": "Instagram collection",
        "use": "Public profile posts and metadata. Rate-limited hard, and "
               "Instagram bans aggressively.",
        "needs": "an Instagram session (vault credential)",
        "serverless_ok": False, "group": "platform",
    },
    "yt_dlp": {
        "module": "yt_dlp", "label": "yt-dlp",
        "role": "Video metadata",
        "use": "Titles, descriptions, upload dates and channel data for "
               "YouTube, TikTok and many other video hosts, without a key.",
        "needs": None, "serverless_ok": True, "group": "platform",
    },
    "snscrape": {
        "module": "snscrape", "label": "snscrape",
        "role": "Social scraping (legacy)",
        "use": "Historical social posts without an API key.",
        "needs": None, "serverless_ok": False, "group": "platform",
        # Stated plainly because the alternative is an analyst assuming an
        # empty result means "nothing was posted".
        "caveat": "Unmaintained since X closed its public endpoints. X support "
                  "is broken in practice; treat any empty result as a failure, "
                  "not as an absence of posts.",
    },

    # -- browser automation --------------------------------------------
    "playwright": {
        "module": "playwright", "label": "Playwright",
        "role": "Browser automation",
        "use": "Renders JavaScript-heavy pages and reuses a logged-in session "
               "from the vault.",
        "needs": "python -m playwright install chromium",
        "serverless_ok": False, "group": "browser",
    },
    "selenium": {
        "module": "selenium", "label": "Selenium",
        "role": "Browser automation (fallback)",
        "use": "Alternative driver for sites that detect Playwright.",
        "needs": "a matching browser driver on PATH",
        "serverless_ok": False, "group": "browser",
    },
    "scrapy": {
        "module": "scrapy", "label": "Scrapy",
        "role": "Crawling framework",
        "use": "Multi-page crawls of a site rather than single-page fetches.",
        "needs": None, "serverless_ok": False, "group": "browser",
    },

    # -- analysis ------------------------------------------------------
    "spacy": {
        "module": "spacy", "label": "spaCy",
        "role": "Entity extraction",
        "use": "Pulls people, organisations and places out of post text, so "
               "identities surface without anyone reading every post.",
        "needs": "python -m spacy download en_core_web_sm",
        "serverless_ok": False, "group": "analysis",
    },
    "transformers": {
        "module": "transformers", "label": "transformers",
        "role": "Classification / sentiment",
        "use": "Model-based sentiment and stance scoring on collected posts.",
        "needs": "a downloaded model (hundreds of MB)",
        "serverless_ok": False, "group": "analysis",
    },
    "networkx": {
        "module": "networkx", "label": "NetworkX",
        "role": "Graph analysis",
        "use": "Centrality, clustering and community detection over the link "
               "map, so the important accounts are ranked rather than guessed.",
        "needs": None, "serverless_ok": True, "group": "analysis",
    },
    "pandas": {
        "module": "pandas", "label": "pandas",
        "role": "Tabular analysis",
        "use": "Trend and account aggregation over collected posts.",
        "needs": None, "serverless_ok": True, "group": "analysis",
    },

    # -- media / enrichment --------------------------------------------
    "exifread": {
        "module": "exifread", "label": "exifread",
        "role": "Image metadata",
        "use": "EXIF from uploaded images: camera, timestamps and GPS, which "
               "often survive on images reposted from a phone.",
        "needs": None, "serverless_ok": True, "group": "media",
    },
    "pillow": {
        "module": "PIL", "label": "Pillow",
        "role": "Image processing",
        "use": "Thumbnails, format conversion and perceptual hashing for "
               "matching reposted images.",
        "needs": None, "serverless_ok": True, "group": "media",
    },
    "geopy": {
        "module": "geopy", "label": "geopy",
        "role": "Geocoding",
        "use": "Turns a place name or GPS pair into a located point for the "
               "link map.",
        "needs": "a Nominatim user agent (free)",
        "serverless_ok": True, "group": "media",
    },
}

_probe_cache = {}
_lock = threading.Lock()


def probe(name):
    """Is this capability usable here? Cached after the first check."""
    with _lock:
        if name in _probe_cache:
            return _probe_cache[name]

    spec = REGISTRY.get(name)
    if not spec:
        result = {"name": name, "available": False, "installed": False,
                  "reason": "Unknown capability"}
    else:
        installed = importlib.util.find_spec(spec["module"]) is not None
        blocked = SERVERLESS and not spec["serverless_ok"]
        reason = ""
        if not installed:
            reason = "Not installed (pip install %s)" % name.replace("_", "-")
        elif blocked:
            reason = ("Needs a writable disk or a browser, which this "
                      "serverless deployment does not have")
        result = {
            "name": name,
            "label": spec["label"],
            "role": spec["role"],
            "use": spec["use"],
            "group": spec["group"],
            "installed": installed,
            "available": installed and not blocked,
            "needs": spec.get("needs"),
            "caveat": spec.get("caveat"),
            "serverless_ok": spec["serverless_ok"],
            "reason": reason,
        }

    with _lock:
        _probe_cache[name] = result
    return result


def available(name):
    return probe(name)["available"]


def load(name):
    """Import and return the module, or None when unavailable.

    Always guard the return value; never assume a capability is present.
    """
    if not available(name):
        return None
    try:
        return importlib.import_module(REGISTRY[name]["module"])
    except Exception:
        # An installed-but-broken package (a missing binary wheel, a model that
        # was never downloaded) must not take a collection run down.
        with _lock:
            _probe_cache[name] = dict(_probe_cache.get(name, {}),
                                      available=False,
                                      reason="Installed but failed to import")
        return None


def report():
    """The full capability picture, for the UI and for /monitor/capabilities."""
    rows = [probe(n) for n in REGISTRY]
    groups = {}
    for r in rows:
        groups.setdefault(r["group"], []).append(r)
    return {
        "serverless": SERVERLESS,
        "groups": groups,
        "available": sorted(r["name"] for r in rows if r["available"]),
        "missing": sorted(r["name"] for r in rows if not r["installed"]),
        "blocked": sorted(r["name"] for r in rows
                          if r["installed"] and not r["available"]),
        "counts": {
            "total": len(rows),
            "available": sum(1 for r in rows if r["available"]),
        },
    }


def reset():
    """Forget probe results. Used by tests and after an install."""
    with _lock:
        _probe_cache.clear()
