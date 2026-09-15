"""Facebook post and comment collection.

Everything here targets `mbasic.facebook.com` -- the no-JavaScript interface
Facebook still serves for feature phones. It matters because it is the only
Facebook surface that renders complete posts, permalinks, timestamps and
comment threads as plain server-side HTML. The modern site renders nothing
without executing a large JavaScript bundle, so parsing it from `requests` is
not possible at all.

What this module can and cannot reach, measured rather than assumed:

    ANY request without a session      no -- Facebook answers HTTP 400 with an
                                       "Error" page for every logged-out mbasic
                                       request, Pages and permalinks alike
    public Page timeline, session      yes
    permalink for a single post        yes, when the post itself is public
    comments under a public post       yes, including paging through
                                       "View more comments"
    groups, private pages, profiles    only what that account can see
    search                             no -- mbasic search is login-walled

So a vaulted session is now a requirement, not an upgrade. The unauthenticated
path is kept because the failure has to be *explained*: an empty result is
reported as "Facebook refused the request, add a session", never as "no
comments found", which reads as "this post has no comments" and sends the
analyst to check entirely the wrong thing.

Sessions are short-lived -- cookies expire in days and scripted sessions get
challenged -- so a rejected one is reported distinctly from a missing one.

Parsing approach
----------------
mbasic's markup is old-style, shallow and remarkably stable -- it has to be,
because the devices it targets cannot handle anything else. It is still HTML
though, so we parse with BeautifulSoup when it is installed (it is, in
requirements.txt) and fall back to regex when it is not. The fallback exists
because a missing optional dependency should cost accuracy, not the feature.

Nothing here writes to the database. Collectors return post dicts in the same
shape as every other source, and the caller decides what to keep.
"""

import os
import re
import time
from datetime import datetime, timedelta
from urllib.parse import parse_qs, quote, urlencode, urlparse, urlunparse

import requests

from .collectors import _clean, _host, _result, http_get

MBASIC = "https://mbasic.facebook.com"

# Facebook serves a login wall from several paths; hitting any of them means
# the fetch produced a page about logging in rather than the content asked for.
_LOGIN_MARKERS = ("/login", "/login.php", "/checkpoint", "/recover",
                  "/r.php", "/reg/")

# Segments in a Facebook URL that are plumbing rather than an account name.
_RESERVED = {
    "profile.php", "permalink.php", "story.php", "photo.php", "video.php",
    "groups", "pages", "watch", "events", "marketplace", "search", "hashtag",
    "home.php", "login.php", "sharer.php", "help", "policies", "privacy",
    "terms", "about", "settings", "messages", "notifications", "bookmarks",
    "friends", "pg", "people", "posts", "photos", "videos", "reel", "share",
}

# Reaction/comment/share counters, as mbasic words them.
_REACT_RE = re.compile(r"(\d[\d,.]*)\s*(?:people|person)?\s*"
                       r"(?:reacted|likes?|reactions?)", re.I)
_COMMENT_RE = re.compile(r"(\d[\d,.]*)\s*comments?", re.I)
_SHARE_RE = re.compile(r"(\d[\d,.]*)\s*shares?", re.I)

# Relative timestamps mbasic prints instead of dates ("2 hrs", "Yesterday").
_REL_RE = re.compile(
    r"^\s*(?:(\d+)\s*(m|min|mins|minute|minutes|h|hr|hrs|hour|hours|"
    r"d|day|days|w|wk|wks|week|weeks)|(just now|yesterday))\b", re.I)

_UNIT_MINUTES = {
    "m": 1, "min": 1, "mins": 1, "minute": 1, "minutes": 1,
    "h": 60, "hr": 60, "hrs": 60, "hour": 60, "hours": 60,
    "d": 1440, "day": 1440, "days": 1440,
    "w": 10080, "wk": 10080, "wks": 10080, "week": 10080, "weeks": 10080,
}


def _soup(markup):
    """Parse with BeautifulSoup, or return None when it is unavailable.

    Callers fall back to regex extraction on None, so a missing optional
    dependency degrades accuracy instead of removing the feature.
    """
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return None
    try:
        return BeautifulSoup(markup or "", "html.parser")
    except Exception:
        return None


def page_name(value):
    """Normalise whatever the analyst typed into a Page identifier.

    Accepts a bare name, a full URL, an `m.`/`web.` host, a `/pg/` path or a
    numeric `profile.php?id=`. Returns "" when nothing usable is in there,
    because fetching `mbasic.facebook.com/` with an empty name would quietly
    return the login page and look like a scraping failure.
    """
    raw = str(value or "").strip()
    if not raw:
        return ""
    if "facebook.com" in raw.lower() or raw.startswith("http"):
        try:
            parsed = urlparse(raw if "://" in raw else "https://" + raw)
        except ValueError:
            return ""
        # profile.php?id=100... is a numeric account, and the id is the name.
        if parsed.path.rstrip("/").endswith("profile.php"):
            ids = parse_qs(parsed.query or "").get("id")
            return ("profile.php?id=" + ids[0]) if ids else ""
        parts = [p for p in (parsed.path or "").split("/") if p]
        if parts and parts[0] == "pg" and len(parts) > 1:
            parts = parts[1:]
        if parts and parts[0] == "groups" and len(parts) > 1:
            return "groups/" + parts[1]
        raw = parts[0] if parts else ""
    raw = raw.lstrip("@").strip("/")
    if not raw or raw.lower() in _RESERVED:
        return ""
    # A Page name is letters, digits, dots and dashes. Anything else is a
    # fragment of a URL we failed to parse, and fetching it wastes a request.
    if not re.match(r"^[A-Za-z0-9.\-_]{2,80}$", raw):
        return ""
    return raw


def _abs(href):
    """Make an mbasic-relative link absolute, dropping tracking noise."""
    if not href:
        return ""
    href = href.replace("&amp;", "&")
    if href.startswith("//"):
        href = "https:" + href
    elif href.startswith("/"):
        href = MBASIC + href
    elif not href.startswith("http"):
        return ""
    try:
        parsed = urlparse(href)
    except ValueError:
        return href
    # `refid`, `__tn__`, `eav` and friends are per-session tracking values.
    # Left in, they make the same post look different on every fetch, which
    # would defeat deduplication entirely.
    keep = {k: v for k, v in parse_qs(parsed.query).items()
            if k in ("id", "story_fbid", "fbid", "comment_id", "p", "v")}
    return urlunparse(parsed._replace(
        netloc=parsed.netloc.replace("mbasic.", "www.").replace("m.", "www."),
        query=urlencode(keep, doseq=True), fragment=""))


def _looks_like_login(url, body):
    """True when Facebook answered with a login wall rather than content."""
    path = ""
    try:
        path = (urlparse(url or "").path or "").lower()
    except ValueError:
        path = (url or "").lower()
    if any(path == m or path.startswith(m) for m in _LOGIN_MARKERS):
        return True
    head = (body or "")[:4000].lower()
    # Two independent signals, because either alone shows up on real pages:
    # a password field is decisive, and the phrasing only appears on the wall.
    if 'type="password"' in head:
        return True
    return ("log in to continue" in head
            or "you must log in to continue" in head)


def _parse_stamp(text, now=None):
    """Turn an mbasic timestamp into an ISO string, or None.

    mbasic prints absolute dates for old posts ("14 September at 09:12") and
    relative ones for recent ones ("2 hrs"). Both are worth recovering: without
    a timestamp every collected post sorts as "now", and the date filter --
    which is how an analyst narrows a sweep -- silently does nothing.
    """
    raw = _clean(text or "")
    if not raw:
        return None
    now = now or datetime.utcnow()

    m = _REL_RE.match(raw)
    if m:
        if m.group(3):
            word = m.group(3).lower()
            when = now - timedelta(days=1) if word == "yesterday" else now
            return when.replace(microsecond=0).isoformat()
        minutes = _UNIT_MINUTES.get((m.group(2) or "").lower())
        if minutes:
            return (now - timedelta(minutes=int(m.group(1)) * minutes)
                    ).replace(microsecond=0).isoformat()

    # Absolute forms. `%-d` is not portable, so the day is matched loosely and
    # each candidate format is tried in turn.
    cleaned = re.sub(r"\bat\b", "", raw, flags=re.I)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ,")
    # "at" has already been removed above, so every format here is the
    # stripped form -- listing "%d %B at %H:%M" would never match anything.
    # mbasic omits the year only for the current one. Rather than lean on
    # strptime's 1900 default -- which Python 3.15 will stop allowing for
    # year-less dates -- the current year is appended to both the value and
    # the format, and a result in the future is rolled back a year ("31
    # December" read on 2 January is not eleven months away).
    for fmt in ("%d %B %Y %H:%M", "%d %B %H:%M", "%d %B %Y", "%d %B",
                "%B %d, %Y %H:%M", "%B %d %Y %H:%M", "%B %d, %Y",
                "%B %d %H:%M", "%B %d", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        yearless = "%Y" not in fmt
        value = ("%s %d" % (cleaned, now.year)) if yearless else cleaned
        try:
            dt = datetime.strptime(value, (fmt + " %Y") if yearless else fmt)
        except ValueError:
            continue
        if yearless and dt > now + timedelta(days=1):
            try:
                dt = dt.replace(year=now.year - 1)
            except ValueError:  # 29 February in a non-leap previous year
                dt = dt.replace(month=2, day=28, year=now.year - 1)
        return dt.replace(microsecond=0).isoformat()
    return None


def _counter(pattern, text):
    m = pattern.search(text or "")
    if not m:
        return 0
    try:
        return int(re.sub(r"[,.]", "", m.group(1)))
    except (TypeError, ValueError):
        return 0


def _engagement(block_text):
    """Reactions, comments and shares as integers, 0 when not shown."""
    return {
        "reactions": _counter(_REACT_RE, block_text),
        "comments": _counter(_COMMENT_RE, block_text),
        "shares": _counter(_SHARE_RE, block_text),
    }


def _permalink(node):
    """The canonical link for one post block.

    mbasic puts several links in a story footer; only the story/permalink one
    identifies the post. Without it a post has no stable URL, comments cannot
    be fetched for it, and deduplication falls back to text alone.
    """
    for a in node.find_all("a", href=True):
        href = a["href"]
        if re.search(r"(story\.php|permalink\.php|/posts/|/photos/|/videos/|"
                     r"story_fbid=|/photo\.php)", href):
            return _abs(href)
    return ""


def _post_id(url):
    """A stable id for a post, used to fetch its comments later."""
    if not url:
        return ""
    try:
        q = parse_qs(urlparse(url).query or "")
    except ValueError:
        return ""
    for key in ("story_fbid", "fbid", "id"):
        if q.get(key):
            return q[key][0]
    m = re.search(r"/(?:posts|videos|photos)/(?:[^/]+/)?(\d{5,})", url)
    return m.group(1) if m else ""


def _handle_from_href(href):
    """The account a comment's author link points at, or "".

    Two shapes matter: a vanity name (`/juan.delacruz.9`) and a numeric account
    (`/profile.php?id=100078888`). Numeric ones are common -- most people never
    set a vanity URL -- and dropping them left half a thread's commenters with
    no identity to group or correlate on.
    """
    raw = str(href or "")
    if not raw:
        return ""
    m = re.search(r"profile\.php\?(?:[^\"'&]*&)?id=(\d{5,})", raw)
    if m:
        return "profile.php?id=" + m.group(1)

    # Take the first path segment, never the host: matching "/name" loosely
    # against a full URL happily returns "www.facebook.com" from the "//" in
    # "https://".
    if raw.startswith("//"):          # protocol-relative: //host/name
        raw = "https:" + raw
    try:
        path = urlparse(raw if "://" in raw else "https://x" + (
            raw if raw.startswith("/") else "/" + raw)).path or ""
    except ValueError:
        return ""
    segment = next((s for s in path.split("/") if s), "")
    if not segment or segment.lower() in _RESERVED:
        return ""
    return segment if re.match(r"^[A-Za-z0-9.\-]{3,60}$", segment) else ""


def _story_nodes(soup):
    """Every element that looks like one story on an mbasic timeline.

    mbasic marks stories with `data-ft` (a JSON blob of feed metadata) or with
    an `article` element, depending on which variant is served. Both are
    checked, and nested matches are dropped so one post is not counted twice.
    """
    nodes = soup.find_all(attrs={"data-ft": True}) + soup.find_all("article")
    out = []
    for n in nodes:
        # A story nested inside another matched story is a shared-post preview,
        # not a separate post.
        if any(n is not o and o in n.parents for o in nodes):
            continue
        if n not in out:
            out.append(n)
    return out


def _story_text(node):
    """The post's own words, without the chrome around them.

    Everything mbasic wraps a story in -- the author line, the timestamp, the
    reaction counters, the Like/Comment/Share links -- is text too, and left in
    it drowns the post. So those parts are removed from a copy of the node
    before the remaining text is read.
    """
    try:
        import copy
        work = copy.copy(node)
    except Exception:
        work = node

    for tag in work.find_all(["h3", "h4", "abbr", "footer", "script", "style"]):
        tag.decompose()
    # Action links and counters live in anchors; the post body does not.
    for a in work.find_all("a"):
        label = _clean(a.get_text(" "))
        if re.match(r"(?i)^(like|comment|share|full story|view more|"
                    r"\d[\d,.]*\s*(comments?|shares?|reactions?))", label):
            a.decompose()
    text = _clean(work.get_text(" "))
    # A leading "Page Name" repeated from the header, and trailing counters.
    text = re.sub(r"\s*·\s*$", "", text)
    return text.strip()


def _author_of(node, fallback=""):
    """Who posted this story."""
    for tag in ("h3", "h4"):
        h = node.find(tag)
        if h:
            name = _clean(h.get_text(" "))
            # The header often reads "Page Name shared a post"; the name is the
            # part before the verb.
            name = re.split(r"\s+(?:shared|added|posted|updated|is |was |"
                            r"replied|commented)\b", name)[0].strip()
            if 1 < len(name) <= 120:
                return name
    return fallback


def _timestamp_of(node):
    """The story's own timestamp element, when mbasic printed one."""
    abbr = node.find("abbr")
    if abbr:
        stamp = _parse_stamp(abbr.get_text(" "))
        if stamp:
            return stamp
    # Some variants use a plain span with the date as its whole content.
    for span in node.find_all("span"):
        text = _clean(span.get_text(" "))
        if 3 <= len(text) <= 40:
            stamp = _parse_stamp(text)
            if stamp:
                return stamp
    return None


def parse_timeline(markup, page="", limit=25):
    """Extract posts from an mbasic Page timeline.

    Returns a list of post dicts. Falls back to a regex pass when
    BeautifulSoup is unavailable, which recovers text but not engagement.
    """
    soup = _soup(markup)
    if soup is None:
        return _parse_timeline_regex(markup, page, limit)

    posts = []
    for node in _story_nodes(soup):
        text = _story_text(node)
        if len(text) < 15:
            continue
        url = _permalink(node)
        raw = _clean(node.get_text(" "))
        posts.append({
            "platform": "Facebook",
            "author": _author_of(node, page or "Facebook")[:120],
            "handle": page,
            "verified": False,
            "text": text[:4000],
            "url": url or (MBASIC + "/" + page if page else ""),
            "link_kind": "direct" if url else "search",
            "posted_at": _timestamp_of(node),
            "source_url": MBASIC + "/" + page if page else MBASIC,
            "kind": "post",
            "post_ref": _post_id(url),
            "engagement": _engagement(raw),
        })
        if len(posts) >= limit:
            break
    return posts


def _parse_timeline_regex(markup, page="", limit=25):
    """Regex fallback for when BeautifulSoup is not installed."""
    posts = []
    chunks = re.split(r'(?i)<div[^>]+data-ft=', markup or "")[1:]
    for chunk in chunks:
        # The split lands mid-tag, so the chunk opens with the rest of the
        # `data-ft` value and the closing `>`. Dropping everything up to that
        # `>` is what keeps `"1"> ` out of the post text.
        chunk = chunk.partition(">")[2] or chunk
        # The author sits in the header link; remove the header so the name is
        # not repeated at the front of every post body.
        body = re.sub(r"(?is)<h[34][^>]*>.*?</h[34]>", " ", chunk)
        text = _clean(body)
        # Counters are read from the raw chunk below, so they are noise here.
        text = re.sub(r"(?i)\b\d[\d,.]*\s*"
                      r"(?:people\s+)?(?:reacted|reactions?|likes?|comments?|shares?)\b",
                      " ", text)
        # Strip the interface words the tag-stripper leaves behind.
        text = re.sub(r"(?i)\b(like|comment|share|full story)\b", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        if len(text) < 40:
            continue
        m = re.search(r'(?i)href="(/story\.php[^"]+|/permalink\.php[^"]+)"', chunk)
        url = _abs(m.group(1)) if m else ""
        posts.append({
            "platform": "Facebook",
            "author": (page or "Facebook")[:120],
            "handle": page,
            "verified": False,
            "text": text[:4000],
            "url": url or (MBASIC + "/" + page if page else ""),
            "link_kind": "direct" if url else "search",
            "posted_at": None,
            "source_url": MBASIC + "/" + page if page else MBASIC,
            "kind": "post",
            "post_ref": _post_id(url),
            # Read from the raw chunk: the counters were deliberately stripped
            # out of `text` above, so parsing them from it would find nothing.
            "engagement": _engagement(_clean(chunk)),
        })
        if len(posts) >= limit:
            break
    return posts


# -- Comments ----------------------------------------------------------------

def parse_comments(markup, parent_url="", parent_author="", limit=100):
    """Extract comments from an mbasic permalink page.

    Comment blocks carry an `id` of the raw comment id, which is what
    distinguishes them from the post itself and from the surrounding chrome.
    Each becomes a post-shaped dict with `kind="comment"` so it can be scored,
    filtered and mapped exactly like a post.
    """
    soup = _soup(markup)
    if soup is None:
        return _parse_comments_regex(markup, parent_url, limit)

    out, seen = [], set()
    for node in soup.find_all(id=re.compile(r"^\d{6,}$")):
        cid = node.get("id")
        if cid in seen:
            continue

        # The author is the first link pointing at an account; the body is what
        # remains once that link and the action row are removed.
        author, author_href = "", ""
        for a in node.find_all("a", href=True):
            label = _clean(a.get_text(" "))
            if not label or len(label) > 120:
                continue
            if re.match(r"(?i)^(like|reply|more|\d)", label):
                continue
            author, author_href = label, a["href"]
            break
        if not author:
            continue

        try:
            import copy
            work = copy.copy(node)
        except Exception:
            work = node
        for a in work.find_all("a"):
            label = _clean(a.get_text(" "))
            if label == author or re.match(
                    r"(?i)^(like|reply|more|hide|\d[\d,.]*\s*(likes?|replies))", label):
                a.decompose()
        for tag in work.find_all(["abbr", "script", "style"]):
            tag.decompose()

        text = _clean(work.get_text(" "))
        # mbasic puts the like counter in a bare <span> in the action row, so
        # stripping links and <abbr> leaves a stray number glued to the end of
        # every comment ("...do not share it. 12"). It is not part of what the
        # person wrote, and it corrupts both the text and the dedupe key.
        text = re.sub(r"\s*\d[\d,.]*\s*$", "", text).strip()
        if len(text) < 2:
            continue
        seen.add(cid)

        handle = _handle_from_href(_abs(author_href) or author_href)

        stamp = None
        abbr = node.find("abbr")
        if abbr:
            stamp = _parse_stamp(abbr.get_text(" "))

        out.append({
            "platform": "Facebook",
            "author": author[:120],
            "handle": handle,
            "verified": False,
            "text": text[:4000],
            "url": (parent_url + ("&" if "?" in parent_url else "?")
                    + "comment_id=" + cid) if parent_url else "",
            "link_kind": "direct" if parent_url else "search",
            "posted_at": stamp,
            "source_url": parent_url,
            "kind": "comment",
            "parent_url": parent_url,
            "parent_author": parent_author,
            "comment_ref": cid,
        })
        if len(out) >= limit:
            break
    return out


def _parse_comments_regex(markup, parent_url="", limit=100):
    """Regex fallback for comment extraction."""
    out = []
    for m in re.finditer(r'(?is)<div[^>]+id="(\d{6,})"[^>]*>(.{20,2000}?)</div>',
                         markup or ""):
        cid, chunk = m.group(1), m.group(2)
        a = re.search(r"(?is)<a[^>]*>(.*?)</a>", chunk)
        author = _clean(a.group(1)) if a else ""
        text = _clean(re.sub(r"(?is)<a[^>]*>.*?</a>", " ", chunk))
        if not author or len(text) < 2:
            continue
        out.append({
            "platform": "Facebook", "author": author[:120], "handle": "",
            "verified": False, "text": text[:4000],
            "url": (parent_url + ("&" if "?" in parent_url else "?")
                    + "comment_id=" + cid) if parent_url else "",
            "link_kind": "direct" if parent_url else "search",
            "posted_at": None, "source_url": parent_url,
            "kind": "comment", "parent_url": parent_url, "comment_ref": cid,
        })
        if len(out) >= limit:
            break
    return out


def _next_comment_page(markup):
    """The href of mbasic's "View more comments" link, when there is one."""
    soup = _soup(markup)
    if soup is not None:
        for a in soup.find_all("a", href=True):
            if re.search(r"(?i)view (more|previous) comments", a.get_text(" ")):
                return _raw_href(a["href"])
        return ""
    m = re.search(r'(?is)<a[^>]+href="([^"]+)"[^>]*>[^<]*view (?:more|previous) '
                  r'comments', markup or "")
    return _raw_href(m.group(1)) if m else ""


def _raw_href(href):
    """Absolute mbasic URL, keeping the paging parameters intact.

    `_abs` rewrites the host to www and strips query keys, which is right for a
    permalink an analyst will click but wrong for a paging link we must follow:
    mbasic's paging cursors live in exactly the parameters `_abs` drops.
    """
    href = (href or "").replace("&amp;", "&")
    if href.startswith("/"):
        return MBASIC + href
    if href.startswith("http"):
        return href
    return ""


# -- Fetchers ----------------------------------------------------------------

def _default_budget():
    """Seconds a comment sweep may take before returning what it has.

    Kept under the platform's own request cap so a partial result comes back
    rather than a gateway error. Read from the environment at call time, so a
    deployment can raise it without touching the code, and so a test can too.
    """
    serverless = bool(os.environ.get("VERCEL")
                      or os.environ.get("AWS_LAMBDA_FUNCTION_NAME"))
    try:
        configured = int(os.environ.get("COLLECT_TIMEOUT")
                         or (45 if serverless else 120))
    except (TypeError, ValueError):
        configured = 45 if serverless else 120
    # Leave room for the page fetch, the scoring pass and the response.
    return max(10, int(configured * 0.6))


def _session_advice(session):
    """What to do next, depending on whether a session was already used."""
    if session is None:
        return ("Facebook no longer serves this content to logged-out "
                "requests. Add a Facebook session to the vault (Monitor > "
                "Vault > paste your cookies), then collect again.")
    return ("The stored session was rejected -- its cookies have most likely "
            "expired, or the account hit a checkpoint. Re-paste fresh cookies "
            "in the vault.")


def _refusal(status, session, what="page"):
    """Explain a non-200 from Facebook in terms of what to do about it.

    Facebook answers 400 with an "Error" page for *any* unauthenticated
    request to mbasic now -- Pages, permalinks and comment threads alike. That
    used to be reported as "no comments found", which reads as "this post has
    no comments" and sends people looking in the wrong place.
    """
    if status in (400, 401, 403):
        return ("Facebook refused the request (HTTP %d) without serving the "
                "%s. %s" % (status, what, _session_advice(session)))
    if status == 404:
        return ("Facebook says that %s does not exist. It may have been "
                "deleted, or it may be private." % what)
    if status == 429:
        return ("Facebook is rate-limiting this address (HTTP 429). Wait a "
                "few minutes before collecting again.")
    if 500 <= status < 600:
        return ("Facebook returned a server error (HTTP %d). This is usually "
                "temporary -- try again shortly." % status)
    return ("Facebook answered HTTP %d instead of the %s. %s"
            % (status, what, _session_advice(session)))


def _get(url, session=None, timeout=20):
    """One GET, through a vault session when there is one."""
    if session is not None:
        return session.get(url, timeout=timeout, allow_redirects=True)
    return http_get(url, timeout=timeout, use_cache=False)


def collect_page(page, limit=25, session=None, since=None, with_comments=False,
                 comment_limit=50, budget=None):
    """Fetch recent posts from a public Facebook Page.

    `session` is an authenticated `requests.Session` (built by authfetch from a
    vaulted credential) and is optional -- without one this still works on the
    Pages Facebook serves anonymously, and reports the login wall when it does
    not. With `with_comments`, each post's comment thread is fetched too and
    returned in the same list, tagged `kind="comment"`.
    """
    name = page_name(page)
    if not name:
        return _result(False, note="That does not look like a Facebook Page "
                                   "name or URL.")

    url = "%s/%s" % (MBASIC, quote(name, safe="/?=&."))
    try:
        resp = _get(url, session)
    except requests.exceptions.Timeout:
        return _result(False, note="Timed out reaching mbasic.facebook.com")
    except requests.exceptions.RequestException as e:
        return _result(False, note="Could not reach Facebook: %s" % type(e).__name__)

    if resp.status_code == 404:
        return _result(False, note="No Facebook Page called '%s'." % name,
                       manual_url="https://www.facebook.com/" + name)
    if resp.status_code != 200:
        return _result(False, blocked=True,
                       note=_refusal(resp.status_code, session),
                       manual_url="https://www.facebook.com/" + name)

    if _looks_like_login(getattr(resp, "url", url), resp.text):
        return _result(
            False, blocked=True,
            note=("Facebook served a login wall for that Page. %s"
                  % _session_advice(session)),
            manual_url="https://www.facebook.com/" + name)

    posts = parse_timeline(resp.text, page=name, limit=limit)
    if not posts:
        return _result(
            False, blocked=True,
            note=("The Page loaded but no posts were found in it. It may be "
                  "empty, or restricted to logged-in viewers."),
            manual_url="https://www.facebook.com/" + name)

    posts = _apply_since(posts, since)
    note = "Read %d post(s) from the %s Page" % (len(posts), name)

    comments = []
    if with_comments:
        # Reading every thread is many sequential fetches, and a serverless
        # host will kill the whole request part-way through. A wall-clock
        # budget returns what was gathered so far instead of losing all of it
        # to a gateway timeout, and says how far it got.
        started = time.time()
        cap = budget if budget is not None else _default_budget()
        stopped_early = 0

        # Only posts with a real permalink can have their thread fetched.
        threads = [x for x in posts
                   if x.get("url") and x.get("link_kind") == "direct"]
        for i, p in enumerate(threads):
            if cap and (time.time() - started) > cap:
                stopped_early = len(threads) - i
                break
            got = collect_comments(p["url"], limit=comment_limit, session=session,
                                   parent_author=p.get("author") or name)
            if got["ok"]:
                comments.extend(got["posts"])
        if comments:
            note += " and %d comment(s)" % len(comments)
        if stopped_early:
            note += (" (stopped after %ds with %d thread(s) unread -- collect "
                     "again or raise COLLECT_TIMEOUT)" % (cap, stopped_early))

    if session is not None:
        note += " using a stored session"
    return _result(True, posts + comments, note)


def collect_comments(post_url, limit=50, session=None, parent_author="",
                     max_pages=5):
    """Fetch the comment thread under one public Facebook post.

    Follows "View more comments" up to `max_pages` times, because mbasic shows
    only a handful at a time and the interesting replies are rarely the first
    few. Paging stops early once `limit` is reached.
    """
    url = _mbasic_permalink(post_url)
    if not url:
        return _result(False, note="That is not a Facebook post URL.")

    collected, seen, pages, note_extra = [], set(), 0, ""
    refused = ""      # why Facebook would not serve the page, when it would not
    while url and pages < max_pages and len(collected) < limit:
        pages += 1
        try:
            resp = _get(url, session)
        except requests.exceptions.Timeout:
            note_extra = " (timed out while paging)"
            break
        except requests.exceptions.RequestException as e:
            note_extra = " (stopped: %s)" % type(e).__name__
            break
        if resp.status_code != 200:
            if collected:
                note_extra = " (Facebook answered %d and paging stopped)" % resp.status_code
            else:
                refused = _refusal(resp.status_code, session, "post")
            break
        if _looks_like_login(getattr(resp, "url", url), resp.text):
            if collected:
                note_extra = " (a login wall stopped further paging)"
                break
            return _result(
                False, blocked=True,
                note=("Facebook served a login wall for that post. %s"
                      % _session_advice(session)),
                manual_url=post_url)

        batch = parse_comments(resp.text, parent_url=_abs(post_url),
                               parent_author=parent_author,
                               limit=limit - len(collected))
        fresh = [c for c in batch if c.get("comment_ref") not in seen]
        for c in fresh:
            seen.add(c.get("comment_ref"))
        collected.extend(fresh)
        # No new comments on this page means paging is going in circles.
        if not fresh:
            break
        url = _next_comment_page(resp.text)

    if not collected:
        if refused:
            # Being refused is not the same as there being nothing to read, and
            # saying "no comments found" sends the analyst to check the wrong
            # thing entirely.
            return _result(False, blocked=True, note=refused, manual_url=post_url)
        return _result(
            False,
            note=("That post loaded but no comments were readable%s. It may "
                  "have none, comments may be limited, or the layout changed."
                  % note_extra),
            manual_url=post_url)
    return _result(True, collected[:limit],
                   "Read %d comment(s) across %d page(s)%s"
                   % (len(collected[:limit]), pages, note_extra))


def _mbasic_permalink(url):
    """Rewrite any Facebook post URL to its mbasic equivalent."""
    raw = str(url or "").strip()
    if not raw:
        return ""
    if not raw.startswith("http"):
        raw = "https://" + raw.lstrip("/")
    try:
        parsed = urlparse(raw)
    except ValueError:
        return ""
    host = (parsed.hostname or "").lower()
    if not (host.endswith("facebook.com") or host.endswith("fb.com")):
        return ""
    return urlunparse(parsed._replace(scheme="https", netloc="mbasic.facebook.com",
                                      fragment=""))


def _apply_since(posts, since):
    """Drop posts older than `since`, keeping undated ones.

    An undated post is not evidence that it is old, and discarding it would
    silently throw away everything mbasic declined to timestamp.
    """
    if not since:
        return posts
    out = []
    for p in posts:
        stamp = p.get("posted_at")
        if not stamp:
            out.append(p)
            continue
        try:
            if datetime.fromisoformat(str(stamp)[:19]) >= since:
                out.append(p)
        except (ValueError, TypeError):
            out.append(p)
    return out
