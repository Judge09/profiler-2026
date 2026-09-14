"""Post scoring engine.

Ported from the standalone Media Release Threat Monitor prototype and
generalised: the release-comparison rules are now one optional mode among
several rather than the whole engine.

Modes
-----
standard  Always on. Keyword relevance, scam/urgency language, link analysis.
release   Media Release Threat mode. Adds fidelity-to-reference scoring,
          impersonation of official accounts, contradiction detection and the
          account-compromise rule.
hunter    Digital Hunter mode. Adds identity correlation against stored
          Profiles (codename, aliases, social handles) plus doxxing and
          threat-language rules.

Every numeric weight below can be overridden per-watch through the rule
editor, so an analyst can retune scoring without touching this file.
"""

import re
from urllib.parse import urlparse

BASE_SCORE = 25
THRESHOLD_REVIEW = 30
THRESHOLD_HIGH = 60

VERDICT_LABELS = {
    "bad": "High risk",
    "warn": "Needs review",
    "ok": "Likely authentic",
}

STOP = set(
    "the and for with from this that its are was were been will has have our "
    "their your into about more than which who what can not but all also via "
    "get got any you they them there here when where how why some such only "
    "just being had did does do".split()
)

# Characters swapped in to disguise a look-alike name.
GLYPH = {"0": "o", "1": "l", "3": "e", "4": "a", "5": "s", "7": "t",
         "@": "a", "$": "s", "|": "l"}
FILLER = {"official", "page", "team", "real", "the", "ph", "inc", "hq",
          "admin", "account", "org", "net"}

SHORTENERS = {"bit.ly", "tinyurl.com", "t.co", "cutt.ly", "rb.gy", "is.gd",
              "goo.gl", "s.id", "tiny.cc", "shorturl.at", "ow.ly"}

# Path segments that genuinely indicate a credential or sign-up flow. Compared
# against whole segments, never as substrings -- "updates" in an article slug
# is not "update" the account action.
CREDENTIAL_SEGMENTS = {
    "login", "log-in", "signin", "sign-in", "signup", "sign-up", "verify",
    "verification", "register", "registration", "claim", "voucher", "reward",
    "rewards", "otp", "account", "accounts", "password", "reset", "confirm",
    "auth", "authenticate", "wallet", "payment", "billing",
}

# A search results page carries the user's own words, so its path and query
# say nothing about the destination's intent.
SEARCH_PATHS = {"search", "results", "s"}

# Newsroom labels. Capitalised by house style, not by sensationalism.
NEWS_LABELS = re.compile(
    r"\b(LIVE UPDATES?|LIVE|UPDATES?|DEVELOPING|EXCLUSIVE|WATCH|READ|LOOK|"
    r"IN PHOTOS|IN NUMBERS|EXPLAINER|ANALYSIS|OPINION|EDITORIAL|FULL TEXT|"
    r"TIMELINE|RECAP|FACT CHECK|SPECIAL COVERAGE|JUST IN|BREAKING NEWS)\b:?")
RISKY_TLD = re.compile(
    r"\.(xyz|top|click|shop|live|online|site|icu|buzz|loan|win|rest|cyou|"
    r"monster|info|zip|mov)$", re.I)

LINK_PATTERN = (
    r"(?:https?://|www\.)[^\s<>\"]+"
    r"|\b(?:bit\.ly|tinyurl\.com|t\.co|cutt\.ly|rb\.gy|is\.gd|goo\.gl|s\.id|tiny\.cc)/[^\s<>\"]+"
)
LINK_RE = re.compile(LINK_PATTERN, re.I)

SCAM_RULES = [
    # "free" only counts as bait when it is offering something. "feel free to
    # ask" and "free and fair elections" are ordinary language, and the latter
    # is unavoidable vocabulary in election monitoring.
    (re.compile(r"(?<!feel )\bfree\b(?!\s+(and fair|speech|press|will|trade|"
                r"market|of charge\b(?! )))", re.I), "free"),
    (re.compile(r"\bgiv(e|ing) ?aways?\b", re.I), "giveaway"),
    (re.compile(r"\bvouchers?\b", re.I), "voucher"),
    (re.compile(r"\bclaim\b", re.I), "claim"),
    (re.compile(r"\b(prizes?|winners?|raffle|rewards?)\b", re.I), "prize"),
    (re.compile(r"\b(limited slots?|first \d+( only)?)\b", re.I), "scarcity"),
    (re.compile(r"\b(gcash|maya|paymaya|bitcoin|crypto wallet)\b", re.I), "e-wallet"),
    (re.compile(r"\b(verify|log ?in|password|otp|one[- ]time pin)\b", re.I), "credential request"),
    (re.compile(r"\bdeactivat\w*", re.I), "deactivation threat"),
    (re.compile(r"\b(dm|pm) (us|me)\b", re.I), "move to private messages"),
    (re.compile(r"\bregister\b", re.I), "registration link"),
]
# A payment *request*, not any mention of a sum. Reporting on a P2.2 billion
# audit finding is not a scam signal, so an amount alone must not match.
MONEY_RE = re.compile(
    r"\b(send|pay|transfer|remit|deposit|load)\b[^.!?\n]{0,40}"
    r"(₱|\bphp\b|\bpesos?\b|\$|\bgcash\b|\bmaya\b|\bpaymaya\b|\bP\d)"
    r"|(₱|\bphp\s?|\$)\s?\d[\d,]*(\.\d+)?[^.!?\n]{0,30}\b"
    r"(via|through|to)\b[^.!?\n]{0,30}\b(gcash|maya|paymaya|bank|account|number)\b"
    r"|\b(processing|registration|application|accreditation)\s+fee\b"
    r"|\bfee\s+of\s+(₱|php|\$)", re.I)
URGENCY_RE = re.compile(
    r"\b(immediately|urgent(ly)?|asap|hurry|last chance|today only|act now"
    r"|within \d+ ?(hours?|hrs?|minutes?|mins?))\b", re.I)
CLAIM_RULES = [
    (re.compile(r"\bcancel+(ed|s|ation)?\b", re.I), "cancelled"),
    (re.compile(r"\bpostpone[ds]?\b", re.I), "postponed"),
    (re.compile(r"\bsuspen(d|ded|ds|sion)\b", re.I), "suspended"),
    (re.compile(r"\bterminat(ed|es|ion)\b", re.I), "terminated"),
    (re.compile(r"\bshut(ting)? down\b", re.I), "shut down"),
    (re.compile(r"\bhack(ed|s)?\b", re.I), "hacked"),
    (re.compile(r"\bbreach(ed|es)?\b", re.I), "breach"),
    (re.compile(r"\bleak(ed|s)?\b", re.I), "leaked"),
    (re.compile(r"\bfake\b", re.I), "fake"),
    (re.compile(r"\bscam\b", re.I), "scam"),
    (re.compile(r"\barrest(ed)?\b", re.I), "arrested"),
    (re.compile(r"\bresign(ed|s)?\b", re.I), "resigned"),
]

# Digital Hunter: language suggesting a person is being targeted or exposed.
DOXX_RULES = [
    (re.compile(r"\b(home )?address\b", re.I), "address mention"),
    (re.compile(r"\b(phone|mobile|contact) ?(number|no\.?)\b", re.I), "phone number"),
    (re.compile(r"\bwhere (he|she|they) (lives?|works?|studies)\b", re.I), "location exposure"),
    (re.compile(r"\b(expose|exposing|exposed)\b", re.I), "exposure framing"),
    (re.compile(r"\b(find|locate|track) (him|her|them|this (guy|girl|person))\b", re.I), "locate request"),
    (re.compile(r"\b(real name|true identity|who (he|she|they) really (is|are))\b", re.I), "identity reveal"),
    (re.compile(r"\b(school|workplace|dorm|barangay) (of|where)\b", re.I), "affiliation exposure"),
]
THREAT_RULES = [
    (re.compile(r"\b(kill|murder|shoot|stab|beat up|hurt)\b", re.I), "violence"),
    (re.compile(r"\bthreat(en(ing|ed)?)?\b", re.I), "threat language"),
    (re.compile(r"\b(revenge|payback|get even)\b", re.I), "revenge framing"),
    (re.compile(r"\b(watch (your|his|her) back|you'?re dead)\b", re.I), "intimidation"),
]

# Default weights. A watch may override any of these by key.
DEFAULT_WEIGHTS = {
    "base": BASE_SCORE,
    "official_account": -30,
    "verified_not_official": -5,
    "impersonation": 30,
    "claims_official": 10,
    "profile_match": 10,
    "official_link": -10,
    "risky_link_cap": 45,
    "scam_each": 8,
    "scam_cap": 30,
    "money": 15,
    "urgency_each": 8,
    "urgency_cap": 16,
    "breaking": 8,
    "sensational": 6,
    "doxx_each": 10,
    "doxx_cap": 30,
    "threat_each": 12,
    "threat_cap": 35,
    "contradiction": 30,
    "compromise": 30,
    "fidelity_high": -15,
    "fidelity_mid": -6,
    "off_message": 6,
    "custom_flag": 12,
}


# -- Text utilities ----------------------------------------------------------

def stem(w):
    """Crude suffix stripper. Mirrors the prototype's stemmer exactly."""
    if len(w) > 5 and w.endswith("ing"):
        w = w[:-3]
    elif len(w) > 4 and w.endswith("ied"):
        w = w[:-3] + "y"
    elif len(w) > 4 and w.endswith("ed"):
        w = w[:-2]
    elif len(w) > 4 and w.endswith("ies"):
        w = w[:-3] + "y"
    elif len(w) > 4 and re.search(r"(s|x|z|ch|sh)es$", w):
        w = w[:-2]
    elif len(w) > 3 and w.endswith("s") and not re.search(r"(ss|us|is)$", w):
        w = w[:-1]
    if len(w) > 4 and w.endswith("e"):
        w = w[:-1]
    return w


def tokens(text):
    cleaned = LINK_RE.sub(" ", str(text or "").lower())
    return [stem(w) for w in re.findall(r"[a-z0-9]+", cleaned)
            if len(w) > 2 and w not in STOP]


def norm_name(s):
    """Normalise a display name or handle for look-alike comparison."""
    s = str(s or "").lstrip("@").lower()
    s = "".join(GLYPH.get(c, c) for c in s)
    return "".join(w for w in re.split(r"[^a-z0-9]+", s) if w and w not in FILLER)


def levenshtein(a, b):
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev_row = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        row = [i]
        for j, cb in enumerate(b, 1):
            row.append(min(row[j - 1] + 1, prev_row[j] + 1,
                           prev_row[j - 1] + (ca != cb)))
        prev_row = row
    return prev_row[len(b)]


def similarity(a, b):
    if not a or not b:
        return 0.0
    return 1 - levenshtein(a, b) / max(len(a), len(b))


def clamp(v, lo=0, hi=100):
    return max(lo, min(hi, v))


# Platforms whose "author" is a publication, not an account someone controls.
NEWS_PLATFORMS = {"news site", "web page", "rss", "hackernews", "news"}

# Words that describe a category rather than a subject. On their own they place
# a post in no particular topic -- "election" matches Manila and Missouri alike
# -- so they broaden a watch but can never anchor it.
GENERIC_TERMS = {
    "election", "elections", "poll", "polls", "vote", "votes", "voting",
    "voter", "voters", "ballot", "ballots", "campaign", "candidate",
    "candidates", "politics", "political", "government", "news", "update",
    "updates", "report", "security", "cyber", "cybersecurity", "scam",
    "fraud", "protest", "rally", "parliament", "parliamentary", "senate",
    "congress", "official", "announcement", "statement",
}


def is_generic_term(term):
    """True when a term names a category rather than a subject.

    Multi-word phrases count as generic when every word is generic, so
    "parliamentary elections" cannot anchor a watch (it matches Russia and
    1919 Italy alike) while "BARMM elections" still can.
    """
    words = [w for w in re.split(r"[^a-z0-9]+", str(term or "").lower()) if w]
    if not words:
        return True
    return all(w in GENERIC_TERMS for w in words)


def handle_looks_social(handle):
    """True when a handle names an account rather than a website host."""
    h = str(handle or "").strip().lstrip("@")
    if not h:
        return False
    # Feed collectors put the publisher's domain in `handle`; a real social
    # handle has no dots-with-TLD shape.
    return not re.search(r"\.[a-z]{2,}(\.[a-z]{2,})?$", h, re.I)


# Phrasing that puts a claim in the past or attributes it elsewhere, rather
# than asserting it about the current situation.
_HISTORICAL_CUES = re.compile(
    # Perfect/passive constructions that report a completed event:
    # "after being postponed", "having been postponed", "was postponed in 2022".
    r"\b((after|having|despite|since) (been|being|it was)"
    r"|had (been|previously)"
    r"|(was|were) \w+ (in|from|to|until) (19|20)\d{2}"
    r"|previously|originally|earlier|formerly|used to|no longer"
    r"|since (19|20)\d{2}|in (19|20)\d{2}|from (19|20)\d{2}|back in"
    r"|last (year|month|week)|history|historical(ly)?"
    r"|\d+ (times|years? ago)|first in|then in)\b", re.I)
_ATTRIBUTION_CUES = re.compile(
    r"\b(rumou?rs?|claims?|alleged(ly)?|falsely|hoax|debunk\w*|fact.?check\w*|"
    r"denied|denies|misinformation|disinformation|not true|untrue|"
    r"speculation|conspiracy)\b", re.I)


def _is_historical(text, pattern):
    """True when a matched claim reads as past record, not a present assertion.

    "postponed at least four times: first in May 2022" states history; "the
    election is postponed" asserts it. Looking at the words immediately around
    the match separates the two without needing a parser.
    """
    for m in pattern.finditer(text):
        window = text[max(0, m.start() - 90):m.end() + 90]
        if _HISTORICAL_CUES.search(window) or _ATTRIBUTION_CUES.search(window):
            continue
        return False  # at least one occurrence is asserted in the present
    return True


def _collect_words(text, pattern, sink):
    """Collect the individual words a regex matched, for highlighting."""
    for m in pattern.finditer(text):
        for w in re.split(r"[^A-Za-z0-9]+", m.group(0).lower()):
            if w:
                sink.add(w)


def compile_custom_flags(raw):
    """Parse the analyst's custom flag list.

    One rule per line. Accepted forms:
        keyword
        keyword = 15            (explicit weight)
        /regex/ = 20            (regex, slashes)
        keyword = 15 | label    (custom label)
    """
    rules = []
    for line in (raw or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        label = ""
        if "|" in line:
            line, label = line.split("|", 1)
            line, label = line.strip(), label.strip()
        weight = None
        if "=" in line:
            line, w = line.rsplit("=", 1)
            line = line.strip()
            try:
                weight = int(float(w.strip()))
            except ValueError:
                weight = None
        if not line:
            continue
        is_regex = len(line) > 2 and line.startswith("/") and line.endswith("/")
        body = line[1:-1] if is_regex else re.escape(line)
        pattern = body if is_regex else rf"\b{body}\b"
        try:
            rx = re.compile(pattern, re.I)
        except re.error:
            continue
        rules.append({
            "rx": rx,
            "label": label or line,
            "weight": DEFAULT_WEIGHTS["custom_flag"] if weight is None else weight,
            "source": line,
        })
    return rules


# -- Link analysis -----------------------------------------------------------

def analyze_link(raw, cfg):
    display = re.sub(r"[),.!?;:'\"]+$", "", str(raw or ""))
    if not display:
        return None
    candidate = display if re.match(r"^https?://", display, re.I) else "https://" + display
    try:
        parsed = urlparse(candidate)
        host = (parsed.hostname or "").lower()
    except ValueError:
        return None
    if not host:
        return None
    host = re.sub(r"^www\.", "", host)

    official = any(host == d or host.endswith("." + d) for d in cfg["domains"])
    # A link the organisation itself published in the reference is vouched for,
    # even when it is a shortener or an unfamiliar host.
    cited = display.lower() in (cfg.get("reference") or "").lower()
    flags = []
    if not official and not cited:
        if re.match(r"^http://", display, re.I):
            flags.append({"label": host + " uses unencrypted http", "w": 10})
        if host in SHORTENERS:
            flags.append({"label": host + " is a shortener that hides the real destination", "w": 15})
        if re.match(r"^\d{1,3}(\.\d{1,3}){3}$", host):
            flags.append({"label": "Link points to a raw IP address", "w": 20})
        if "xn--" in host:
            flags.append({"label": host + " uses punycode, a sign of look-alike characters", "w": 20})
        if RISKY_TLD.search(host):
            flags.append({"label": host + " uses a domain ending common in throwaway sites", "w": 10})
        flat = re.sub(r"[^a-z0-9]", "", host)
        subject_n = cfg.get("subject_norm") or ""
        subject_first = cfg.get("subject_first") or ""
        if (len(subject_n) >= 5 and subject_n in flat) or (
                len(subject_first) >= 3 and subject_first in re.split(r"[.-]", host)):
            flags.append({"label": host + " borrows the subject name but isn't an official domain",
                          "w": 25, "phish": True})
        # Credential-seeking paths. Matched on whole path segments only: an
        # article slug like /barmm-updates-voting-results contains "update"
        # but is not a login page. Search engines carry the user's words in
        # the query string, so that is excluded too.
        segments = [s for s in re.split(r"[/_.\-]+", parsed.path or "") if s]
        if not SEARCH_PATHS.intersection(segments):
            if CREDENTIAL_SEGMENTS.intersection(s.lower() for s in segments):
                flags.append({"label": "The path " + (parsed.path or "/")
                                       + " asks for sign-up or credentials",
                              "w": 10, "phish": True})
    return {"display": display, "host": host, "official": official,
            "cited": cited, "flags": flags}


# -- Config ------------------------------------------------------------------

def build_config(watch, profiles=None):
    """Turn a MonitorWatch row (or a dict) into the analyser's config."""
    if isinstance(watch, dict):
        def get(k, d=None):
            return watch.get(k, d)
    else:
        def get(k, d=None):
            return getattr(watch, k, d)

    subject = (get("subject") or "").strip()
    accounts = set()
    handles = []
    for line in (get("official_accounts") or "").splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) == 1:
            platform, handle = "", parts[0].lstrip("@")
        else:
            platform, handle = parts[0], "".join(parts[1:]).lstrip("@")
        if not handle:
            continue
        accounts.add(platform.lower() + ":" + handle.lower())
        accounts.add(":" + handle.lower())  # platform-agnostic match
        handles.append({"n": norm_name(handle), "raw": "@" + handle})

    domains = []
    for d in re.split(r"[,\s]+", get("official_domains") or ""):
        d = d.strip().lower()
        if not d:
            continue
        d = re.sub(r"^https?://", "", d)
        d = re.sub(r"^www\.", "", d)
        d = re.sub(r"/.*$", "", d)
        if d:
            domains.append(d)

    # Keywords split into required (marked with a leading +) and optional.
    # Requiring at least one anchor term keeps a watch on its topic instead of
    # matching anything that mentions any single keyword.
    raw_keywords = [k.strip() for k in (get("keywords") or "").split(",") if k.strip()]
    required_kw = [k.lstrip("+").strip().lower() for k in raw_keywords if k.startswith("+")]
    optional_kw = [k.lower() for k in raw_keywords if not k.startswith("+")]
    keywords = [k.lstrip("+").strip().lower() for k in raw_keywords]

    # Terms too generic to establish a topic on their own. Matching only these
    # is what pulled foreign coverage into a local-election watch.
    generic_kw = {k for k in optional_kw if is_generic_term(k)}
    if not required_kw:
        # No explicit anchor: everything specific becomes an anchor, and a post
        # must hit at least one of them. Generic terms only ever broaden.
        subj = (subject or "").strip().lower()
        specific = [k for k in optional_kw if k not in generic_kw]
        required_kw = ([subj] if subj else []) + [k for k in specific if k != subj]
        optional_kw = sorted(generic_kw)

    reference = get("reference_text") or ""

    weights = dict(DEFAULT_WEIGHTS)
    overrides = get("weights") or {}
    if isinstance(overrides, dict):
        for k, v in overrides.items():
            if k in weights:
                try:
                    weights[k] = int(float(v))
                except (TypeError, ValueError):
                    pass

    first = re.findall(r"[a-z0-9]+", subject.lower())
    return {
        "subject": subject,
        "subject_norm": norm_name(subject),
        "subject_first": first[0] if first else "",
        "reference": reference,
        "ref_set": set(tokens(reference)),
        "accounts": accounts,
        "handles": handles,
        "domains": domains,
        "keywords": keywords,
        "required_keywords": required_kw,
        "optional_keywords": optional_kw,
        "mode_release": bool(get("mode_release")),
        "mode_hunter": bool(get("mode_hunter")),
        "custom_flags": compile_custom_flags(get("custom_flags")),
        "weights": weights,
        "threshold_review": int(get("threshold_review") or THRESHOLD_REVIEW),
        "threshold_high": int(get("threshold_high") or THRESHOLD_HIGH),
        "profiles": profiles or [],
    }


def profile_targets(profiles):
    """Flatten Profile rows into comparable identity targets for Hunter mode."""
    targets = []
    for p in profiles:
        entries = [(p.codename, "codename")]
        if p.real_name:
            entries.append((p.real_name, "real name"))
        for alias in (p.known_aliases or []):
            entries.append((alias, "alias"))
        for link in getattr(p, "social_links", []) or []:
            if link.username:
                entries.append((link.username, (link.platform or "social") + " handle"))
        for raw, kind in entries:
            n = norm_name(raw)
            if len(n) >= 3:
                targets.append({"profile_id": p.id, "codename": p.codename,
                                "raw": raw, "kind": kind, "n": n})
    return targets


# -- Main analyser -----------------------------------------------------------

def analyze(post, cfg):
    """Score one post. `post` is a MonitorPost row or a plain dict."""
    if isinstance(post, dict):
        def get(k, d=None):
            return post.get(k, d)
    else:
        def get(k, d=None):
            return getattr(post, k, d)

    text = get("text") or ""
    platform = get("platform") or ""
    author = get("author") or ""
    handle = str(get("handle") or "").lstrip("@")
    verified = bool(get("verified"))
    url = get("url") or ""
    W = cfg["weights"]

    signals = []
    types = set()
    flag_words = set()
    matches = []

    def add(label, detail, w):
        if w:
            signals.append({"label": label, "detail": detail, "w": int(w)})

    official = (platform.lower() + ":" + handle.lower() in cfg["accounts"]
                or (bool(handle) and ":" + handle.lower() in cfg["accounts"]))

    # Relevance: a post must be on the watch's topic, not merely mention one of
    # its keywords. An anchor term (or the subject) has to appear; the optional
    # keywords then broaden coverage *within* that topic.
    def _mentions(term):
        return bool(re.search(r"\b" + re.escape(term) + r"\b", text, re.I))

    kw_hits = [k for k in cfg["keywords"] if _mentions(k)]
    required = cfg.get("required_keywords") or []
    optional_hits = [k for k in (cfg.get("optional_keywords") or []) if _mentions(k)]

    subject_n = cfg["subject_norm"]
    subject_hit = bool(subject_n and subject_n in norm_name(text))
    anchor_hit = subject_hit or any(_mentions(k) for k in required)

    if required:
        # On topic when any anchor appears, or the post came from an official
        # account (which is about the subject by definition). Generic keywords
        # alone are not enough -- "election" is not a topic.
        relevant = bool(official or anchor_hit)
    else:
        relevant = bool(official or kw_hits or subject_hit)

    off_topic_reason = ""
    if not relevant and optional_hits:
        off_topic_reason = ("Mentions %s, but none of %s"
                            % (", ".join(optional_hits[:3]),
                               ", ".join(required[:4]) or "the subject"))

    # Identity: impersonation (release mode)
    if cfg["mode_release"]:
        if official:
            add("Official account", platform + " @" + handle + " is on the official list",
                W["official_account"])
        else:
            if verified:
                add("Verified by the platform",
                    "Has a verified badge, but isn't one of the official accounts",
                    W["verified_not_official"])
            name_n, handle_n = norm_name(author), norm_name(handle)
            best, target = 0.0, ""
            candidates = [{"n": subject_n, "raw": cfg["subject"]}] + cfg["handles"]
            for t in candidates:
                if not t["n"]:
                    continue
                s = max(similarity(name_n, t["n"]), similarity(handle_n, t["n"]))
                if len(t["n"]) >= 5 and (t["n"] in name_n or t["n"] in handle_n):
                    s = max(s, 0.9)
                if s > best:
                    best, target = s, t["raw"]
            # Impersonation is a claim about an *account* pretending to be the
            # subject. A news outlet or aggregator reporting on the subject is
            # not impersonating it, so exclude posts that carry a byline from a
            # publication rather than a social handle.
            if platform.lower() in NEWS_PLATFORMS and not handle_looks_social(handle):
                best = 0.0
            if best >= 0.8:
                add("Imitates the subject name",
                    '"' + author + '" is ' + str(round(best * 100)) + "% similar to "
                    + target + " but isn't on the official list", W["impersonation"])
                types.add("Impersonation")
            elif re.search(r"official", author + " " + handle, re.I):
                add("Claims to be official",
                    'Uses "official" without being on the list', W["claims_official"])
                types.add("Impersonation")

    # Identity: profile correlation (hunter mode)
    if cfg["mode_hunter"] and cfg["profiles"]:
        name_n, handle_n = norm_name(author), norm_name(handle)
        text_n = norm_name(text)
        seen_profiles = {}
        for t in profile_targets(cfg["profiles"]):
            score = max(similarity(name_n, t["n"]), similarity(handle_n, t["n"]))
            via = "author"
            if len(t["n"]) >= 4 and t["n"] in text_n and score < 0.85:
                score, via = 0.85, "post text"
            if len(t["n"]) >= 4 and (t["n"] in name_n or t["n"] in handle_n):
                score, via = max(score, 0.92), "author"
            if score >= 0.8:
                prev = seen_profiles.get(t["profile_id"])
                if not prev or score > prev["confidence"]:
                    seen_profiles[t["profile_id"]] = {
                        "profile_id": t["profile_id"], "codename": t["codename"],
                        "matched": t["raw"], "kind": t["kind"],
                        "via": via, "confidence": round(score, 3),
                    }
        matches = sorted(seen_profiles.values(),
                         key=lambda m: m["confidence"], reverse=True)
        if matches:
            top = matches[0]
            detail = (top["kind"].capitalize() + ' "' + top["matched"] + '" matches '
                      + top["codename"] + " (" + str(round(top["confidence"] * 100))
                      + "% via " + top["via"] + ")")
            if len(matches) > 1:
                detail += ", plus " + str(len(matches) - 1) + " other profile"
                detail += "s" if len(matches) > 2 else ""
            add("Matches a tracked profile", detail, W["profile_match"])
            types.add("Profile hit")
            relevant = True

    # Links
    raws = re.findall(LINK_PATTERN, text, re.I)
    raws = [r if isinstance(r, str) else r[0] for r in raws]
    # `url` is the post's own location. When the collector could not recover a
    # real article URL it stores a search link instead; that is our own
    # navigation aid, not something the author published, so it is not evidence.
    if url and get("link_kind") != "search":
        raws.append(url)
    links, seen = [], set()
    for r in raws:
        L = analyze_link(r, cfg)
        if L and L["display"] not in seen:
            seen.add(L["display"])
            links.append(L)
    bad_flags = [f for l in links for f in l["flags"]]
    if any(l["official"] for l in links):
        add("Links to an official domain", "Points readers to an official source",
            W["official_link"])
    if bad_flags:
        add("Risky link", "; ".join(f["label"] for f in bad_flags),
            min(sum(f["w"] for f in bad_flags), W["risky_link_cap"]))
        if any(f.get("phish") for f in bad_flags):
            types.add("Phishing")

    # Scam language
    scam_hits = [(rx, lbl) for rx, lbl in SCAM_RULES if rx.search(text)]
    if scam_hits:
        for rx, _ in scam_hits:
            _collect_words(text, rx, flag_words)
        labels = ", ".join(lbl for _, lbl in scam_hits)
        add("Scam language", labels[:1].upper() + labels[1:],
            min(len(scam_hits) * W["scam_each"], W["scam_cap"]))
        types.add("Scam")
    money_hit = bool(MONEY_RE.search(text))
    if money_hit:
        _collect_words(text, MONEY_RE, flag_words)
        add("Asks for money", "Requests a payment or transfer", W["money"])
        types.add("Scam")

    # Urgency and sensationalism
    urg = sorted({m.group(0).lower() for m in URGENCY_RE.finditer(text)})
    if urg:
        _collect_words(text, URGENCY_RE, flag_words)
        add("Pressure to act fast", ", ".join('"' + u + '"' for u in urg),
            min(len(urg) * W["urgency_each"], W["urgency_cap"]))
    if re.search(r"\bbreaking\b", text, re.I):
        flag_words.add("breaking")
        add("Breaking-news framing", "Presents an unconfirmed claim as breaking news",
            W["breaking"])
    # Standard newsroom labels are capitalised by convention, and acronyms are
    # unavoidable in this domain (BARMM, COMELEC). Strip both before judging
    # whether the *writing* is shouty.
    body = NEWS_LABELS.sub(" ", text)
    body = re.sub(r"\b[A-Z]{2,}\b", " ", body)  # acronyms
    letters = re.sub(r"[^A-Za-z]", "", body)
    caps = len(re.findall(r"[A-Z]", body))
    bangs = text.count("!")
    if bangs >= 2 or (len(letters) > 40 and caps / len(letters) > 0.4):
        add("Sensational formatting",
            (str(bangs) + " exclamation marks") if bangs >= 2 else "Mostly capital letters",
            W["sensational"])

    # Hunter: targeting language
    if cfg["mode_hunter"]:
        doxx = [(rx, lbl) for rx, lbl in DOXX_RULES if rx.search(text)]
        if doxx:
            for rx, _ in doxx:
                _collect_words(text, rx, flag_words)
            add("Personal-information exposure", ", ".join(lbl for _, lbl in doxx),
                min(len(doxx) * W["doxx_each"], W["doxx_cap"]))
            types.add("Doxxing")
        threat = [(rx, lbl) for rx, lbl in THREAT_RULES if rx.search(text)]
        if threat:
            for rx, _ in threat:
                _collect_words(text, rx, flag_words)
            add("Threatening language", ", ".join(lbl for _, lbl in threat),
                min(len(threat) * W["threat_each"], W["threat_cap"]))
            types.add("Threat")

    # Analyst's own flag list
    custom_hits = []
    for rule in cfg["custom_flags"]:
        if rule["rx"].search(text):
            _collect_words(text, rule["rx"], flag_words)
            custom_hits.append(rule)
    if custom_hits:
        add("Custom flags", ", ".join(r["label"] for r in custom_hits),
            sum(r["weight"] for r in custom_hits))
        types.add("Custom flag")

    # Release mode: contradiction and fidelity
    claims = []
    coverage = 0.0
    if cfg["mode_release"]:
        claims = [(rx, lbl) for rx, lbl in CLAIM_RULES
                  if rx.search(text) and not rx.search(cfg["reference"])
                  and not _is_historical(text, rx)]
        if claims:
            for rx, _ in claims:
                _collect_words(text, rx, flag_words)
            add("Contradicts the reference",
                "Says " + ", ".join('"' + lbl + '"' for _, lbl in claims)
                + ", which the reference doesn't mention",
                min(W["contradiction"] + (len(claims) - 1) * 5, 40))
            types.add("Misinformation")

        # Compromise is a serious call, so require behaviour an official account
        # would not plausibly show: a payment request, a credential-phishing
        # link, contradicted claims, or several scam signals at once. A single
        # shortener is not enough -- organisations use them routinely.
        phishy = any(f.get("phish") for f in bad_flags)
        if official and (claims or money_hit or phishy or len(scam_hits) >= 2):
            add("Possible account compromise",
                "An official account is posting content that breaks from the reference",
                W["compromise"])
            types.add("Account compromise")

        pt = list(dict.fromkeys(tokens(text)))
        coverage = (len([t for t in pt if t in cfg["ref_set"]]) / len(pt)) if pt else 0.0
        if relevant and not claims and cfg["ref_set"]:
            pct = round(coverage * 100)
            if coverage >= 0.75:
                add("Matches the reference", str(pct) + "% of its key words appear in the reference",
                    W["fidelity_high"])
            elif coverage >= 0.45:
                add("Paraphrases the reference", str(pct) + "% of its key words appear in the reference",
                    W["fidelity_mid"])
            elif len(pt) >= 6 and coverage < 0.2:
                add("Off-message",
                    "Mentions the subject, but only " + str(pct)
                    + "% of its key words appear in the reference", W["off_message"])

    raw_total = W["base"] + sum(s["w"] for s in signals)
    score = round(clamp(raw_total))
    hi, mid = cfg["threshold_high"], cfg["threshold_review"]
    verdict = "bad" if score >= hi else "warn" if score >= mid else "ok"

    if not relevant:
        types.add("Off-topic")
    if verdict == "ok" and relevant and not types:
        types.add("Official" if official else
                  "Faithful echo" if coverage >= 0.45 else "Relevant mention")

    return {
        "score": score,
        "raw_total": raw_total,
        "verdict": verdict,
        "verdict_label": VERDICT_LABELS[verdict],
        "signals": signals,
        "types": sorted(types),
        "links": links,
        "flag_words": sorted(flag_words),
        "relevant": relevant,
        "official": official,
        "coverage": round(coverage, 3),
        "keyword_hits": kw_hits,
        "off_topic_reason": off_topic_reason,
        "profile_matches": matches,
        "custom_hits": [r["label"] for r in custom_hits],
        "recommendation": recommend(types, verdict, platform),
        "base": W["base"],
        "thresholds": {"review": mid, "high": hi},
    }


def recommend(types, verdict, platform):
    if "Account compromise" in types:
        return ("Secure the official account now: change the password, end other "
                "active sessions, then remove the post and explain what happened.")
    if "Threat" in types:
        return ("Preserve evidence (screenshot with URL and timestamp), report to the "
                "platform, and escalate to the appropriate authority.")
    if verdict == "bad":
        parts = []
        if "Impersonation" in types:
            parts.append("report the account to " + (platform or "the platform")
                         + " for impersonation")
        if "Phishing" in types or "Scam" in types:
            parts.append("warn followers not to click, register, or pay")
        if "Misinformation" in types:
            parts.append("publish a correction that links the official reference")
        if "Doxxing" in types:
            parts.append("request takedown of the exposed personal information")
        if not parts:
            parts.append("review manually and consider reporting")
        s = ", ".join(parts)
        return s[:1].upper() + s[1:] + "."
    if verdict == "warn":
        return "Confirm with your team before responding. Nothing here is conclusive on its own."
    return "No action needed."


def analyze_batch(posts, cfg):
    out = {}
    for p in posts:
        pid = p.get("id") if isinstance(p, dict) else p.id
        out[pid] = analyze(p, cfg)
    return out
