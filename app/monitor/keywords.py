"""Keyword parsing, matching and query building.

This module owns the single question the rest of the monitor keeps asking:
*is this post actually about the thing we are watching?*

The old answer -- "it mentions any one of the keywords" -- is what pulled US
and Indian election coverage into a BARMM watch. A term like "election" places
a post in no particular topic, so on its own it can never establish relevance.

The model here has three buckets:

    required   Every one of these must appear (or any one, in "any" mode).
               These anchor the topic.
    optional   Broaden coverage and raise a relevance score, but can never
               make an off-topic post relevant on their own.
    excluded   Any match rejects the post outright, whatever else it says.

Each term may be:

    plain word        barmm
    "quoted phrase"   "media release"
    an OR group       comelec|election commission|poll body
    a regex           /barmm\\s+parliament/

Terms are matched on word boundaries against a normalised copy of the text, so
"poll" does not match "pollution" and accents/curly quotes do not break a hit.
"""

import hashlib
import json
import re
import unicodedata

# Words that describe a category rather than a subject. On their own they place
# a post in no particular topic -- "election" matches Manila and Missouri alike
# -- so they broaden a watch but can never anchor it.
GENERIC_TERMS = {
    "election", "elections", "poll", "polls", "vote", "votes", "voting",
    "voter", "voters", "ballot", "ballots", "campaign", "candidate",
    "candidates", "politics", "political", "government", "news", "update",
    "updates", "report", "reports", "security", "cyber", "cybersecurity",
    "scam", "scams", "fraud", "protest", "rally", "parliament",
    "parliamentary", "senate", "congress", "official", "announcement",
    "statement", "issue", "issues", "people", "public", "national", "local",
    "today", "breaking", "latest", "social", "media", "online", "post",
    "posts", "video", "photo", "story", "stories",
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


def normalise(text):
    """Fold accents and smart punctuation so matching is not defeated by them."""
    t = unicodedata.normalize("NFKD", str(text or ""))
    t = "".join(c for c in t if not unicodedata.combining(c))
    return (t.replace("’", "'").replace("‘", "'")
             .replace("“", '"').replace("”", '"')
             .replace("–", "-").replace("—", "-"))


def _acronym_of(phrase):
    """Initials of a multi-word phrase, when they look like a real acronym."""
    # Split on whitespace only: "e-mail" is one hyphenated word, not two
    # words whose initials mean anything.
    words = [w for w in re.split(r"\s+", str(phrase or "").strip()) if w]
    if len(words) < 2:
        return ""
    small = {"of", "the", "and", "in", "for", "a", "an", "de", "del", "da"}
    letters = "".join(w[0] for w in words if w.lower() not in small)
    return letters.upper() if 2 <= len(letters) <= 8 else ""


def expand_aliases(raw):
    """Alternative spellings a search engine will return for this term.

    Search engines expand acronyms and synonyms; a keyword filter that matches
    only the literal string then throws away exactly what the query asked for.
    Searching "BARMM" returns posts saying "Bangsamoro" -- fetched, paid for,
    and silently dropped.

    Rather than ship a dictionary of world knowledge, this handles the two
    mechanical cases that cause most of the damage:

      * a multi-word phrase and its initials ("Bangsamoro Autonomous Region"
        <-> "BARMM"), in either direction
      * punctuation and spacing variants ("COMELEC" / "Comelec", "e-mail" /
        "email")

    Anything beyond that is a judgement call, so the UI suggests it and the
    analyst decides -- see `alias_hint`. Writing `BARMM|Bangsamoro` by hand has
    always worked and still does.
    """
    term = str(raw or "").strip()
    if not term or term.startswith("/"):     # a regex means what it says
        return []

    body = term.strip('"')
    out = []

    acronym = _acronym_of(body)
    if acronym and acronym.lower() != body.lower():
        out.append(acronym)

    # Hyphens and periods inside a word: "e-mail" also appears as "email",
    # "U.S." as "US".
    flat = re.sub(r"[.\-]", "", body)
    if flat and flat.lower() != body.lower() and len(flat) >= 2:
        out.append(flat)

    seen, uniq = {body.lower()}, []
    for a in out:
        if a.lower() not in seen:
            seen.add(a.lower())
            uniq.append(a)
    return uniq


def alias_hint(terms):
    """Suggest synonyms worth adding, for the UI to offer.

    Deliberately advisory: the app cannot know that BARMM means Bangsamoro, but
    it can notice that a bare acronym probably has a long form and ask.
    """
    hints = []
    for t in terms:
        body = str(t or "").strip().strip('"')
        if not body or "|" in body or body.startswith("/"):
            continue      # already an OR-group, or a regex
        if body.isupper() and 3 <= len(body) <= 8 and " " not in body:
            hints.append({
                "term": body,
                "why": ("%s is an acronym. Searches return its long form too, "
                        "and those posts are currently dropped as off-topic."
                        % body),
                "suggest": "%s|<long form>" % body,
            })
    return hints


class Term:
    """One keyword term, compiled to a matcher.

    `raw` is kept verbatim so the UI can round-trip exactly what was typed.
    """

    __slots__ = ("raw", "kind", "rx", "alternatives", "generic")

    def __init__(self, raw):
        self.raw = str(raw or "").strip()
        self.kind = "word"
        self.alternatives = [self.raw]
        self.rx = None
        self.generic = True
        self._compile()

    def _compile(self):
        body = self.raw
        if not body:
            self.rx = None
            return

        # /regex/
        if len(body) > 2 and body.startswith("/") and body.endswith("/"):
            self.kind = "regex"
            self.alternatives = [body]
            try:
                self.rx = re.compile(body[1:-1], re.I)
            except re.error:
                self.rx = None
            self.generic = False
            return

        # "quoted phrase" -- strip the quotes, match the phrase as a unit
        if len(body) > 1 and body[0] == '"' and body[-1] == '"':
            self.kind = "phrase"
            body = body[1:-1].strip()
            self.alternatives = [body] if body else []
        # a|b|c -- any alternative counts as a hit
        elif "|" in body:
            self.kind = "group"
            self.alternatives = [a.strip().strip('"') for a in body.split("|")
                                 if a.strip().strip('"')]
        else:
            self.alternatives = [body]

        # A term matches its own mechanical variants too, so a search that
        # returns "U.S." for "US" is not then discarded by the filter.
        for alias in expand_aliases(self.raw):
            if alias.lower() not in {a.lower() for a in self.alternatives}:
                self.alternatives.append(alias)

        parts = []
        for alt in self.alternatives:
            # Inside a phrase, runs of whitespace match any whitespace, and
            # punctuation in the source is matched loosely so "COMELEC's" still
            # hits "comelec".
            esc = r"\s+".join(re.escape(w) for w in alt.split())
            if esc:
                parts.append(esc)
        if not parts:
            self.rx = None
            return
        self.rx = re.compile(r"(?<![\w])(?:" + "|".join(parts) + r")(?![\w])", re.I)
        self.generic = all(is_generic_term(a) for a in self.alternatives)

    def matches(self, text):
        return bool(self.rx and self.rx.search(text))

    def __bool__(self):
        return self.rx is not None


def _split_lines(raw):
    """Split a textarea value into terms, one per line or comma."""
    out = []
    for line in str(raw or "").replace("\r", "").split("\n"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # A line may still hold a comma list, but never split inside quotes.
        if "," in line and '"' not in line and not line.startswith("/"):
            out.extend(p.strip() for p in line.split(",") if p.strip())
        else:
            out.append(line)
    return out


def _legacy_split(raw_keywords, subject):
    """Interpret the old single comma string as required/optional.

    Terms prefixed with + were already "forced anchors". Everything else that
    is specific becomes required-any; generic words become optional. This keeps
    existing watches behaving sensibly without anyone re-entering keywords.
    """
    raw = [k.strip() for k in str(raw_keywords or "").split(",") if k.strip()]
    forced = [k.lstrip("+").strip() for k in raw if k.startswith("+")]
    rest = [k for k in raw if not k.startswith("+")]

    if forced:
        return forced, [k for k in rest if k not in forced], "any"

    required, optional = [], []
    subj = (subject or "").strip()
    if subj:
        required.append(subj)
    for k in rest:
        if is_generic_term(k):
            optional.append(k)
        elif k.lower() != subj.lower():
            required.append(k)
    # Legacy behaviour was "any anchor counts", so preserve that.
    return required, optional, "any"


class KeywordSpec:
    """The compiled keyword rules for one watch."""

    def __init__(self, required, optional, excluded, match_mode="all",
                 subject=""):
        self.subject = (subject or "").strip()
        self.match_mode = "any" if str(match_mode).lower() == "any" else "all"
        self.required = [t for t in (Term(r) for r in required) if t]
        self.optional = [t for t in (Term(r) for r in optional) if t]
        self.excluded = [t for t in (Term(r) for r in excluded) if t]

        # A required term that is purely generic cannot anchor anything, so it
        # is demoted rather than silently making every post "on topic".
        demoted = [t for t in self.required if t.generic]
        if demoted and len(demoted) < len(self.required):
            self.required = [t for t in self.required if not t.generic]
            self.optional.extend(demoted)
        self.demoted = [t.raw for t in demoted] if len(demoted) < len(
            self.required) + len(demoted) else []

    # -- introspection, for the UI --------------------------------------

    @property
    def has_anchor(self):
        return bool(self.required) or bool(self.subject)

    def describe(self):
        return {
            "required": [t.raw for t in self.required],
            "optional": [t.raw for t in self.optional],
            "excluded": [t.raw for t in self.excluded],
            "match_mode": self.match_mode,
            "subject": self.subject,
            "demoted": self.demoted,
            "generic_required": [t.raw for t in self.required if t.generic],
        }

    def fingerprint(self):
        return json.dumps(self.describe(), sort_keys=True)

    # -- matching --------------------------------------------------------

    def evaluate(self, text, subject_hit=False):
        """Decide relevance for one piece of text.

        Returns a dict with the verdict and the evidence behind it, so the UI
        can explain *why* something was kept or dropped rather than just
        asserting it.
        """
        t = normalise(text)

        excluded_hits = [term.raw for term in self.excluded if term.matches(t)]
        if excluded_hits:
            return {
                "relevant": False,
                "reason": "Excluded by %s" % ", ".join(excluded_hits[:3]),
                "required_hits": [], "optional_hits": [],
                "excluded_hits": excluded_hits, "missing": [],
                "relevance": 0.0,
            }

        required_hits = [term.raw for term in self.required if term.matches(t)]
        optional_hits = [term.raw for term in self.optional if term.matches(t)]
        missing = [term.raw for term in self.required if term.raw not in required_hits]

        if not self.required:
            # Nothing anchors the watch: fall back to the subject, then to any
            # optional hit. This is the weakest configuration and the UI warns.
            relevant = bool(subject_hit or optional_hits)
            reason = ("" if relevant else
                      "No required keywords set and nothing matched the subject")
        elif self.match_mode == "all":
            relevant = bool(required_hits) and not missing
            reason = ("" if relevant
                      else "Missing required: " + ", ".join(missing[:4]))
        else:
            relevant = bool(required_hits) or subject_hit
            reason = ("" if relevant else
                      "None of the required terms appear: "
                      + ", ".join(t.raw for t in self.required[:4]))

        # A relevance score, separate from the risk score: how strongly this
        # post sits inside the topic. Used for ranking and for the "weak match"
        # badge, never for risk.
        denom = (len(self.required) or 1) + (len(self.optional) * 0.5)
        got = len(required_hits) + len(optional_hits) * 0.5
        if subject_hit:
            got += 1
            denom += 1
        relevance = round(min(got / denom, 1.0), 3) if denom else 0.0

        return {
            "relevant": relevant,
            "reason": reason,
            "required_hits": required_hits,
            "optional_hits": optional_hits,
            "excluded_hits": [],
            "missing": missing,
            "relevance": relevance,
        }

    # -- search-query construction ---------------------------------------

    def _quote(self, term):
        """Render one term for a search engine."""
        body = term.raw
        if body.startswith("/") and body.endswith("/"):
            # A regex has no search-engine equivalent; use its literal words.
            body = re.sub(r"[^\w\s]", " ", body[1:-1])
            body = " ".join(body.split())
        body = body.strip('"')
        if "|" in body:
            alts = [a.strip() for a in body.split("|") if a.strip()]
            return "(" + " OR ".join(
                ('"%s"' % a if " " in a else a) for a in alts) + ")"
        return '"%s"' % body if " " in body else body

    def build_query(self, max_terms=8):
        """A topic-scoped search string.

        A bare OR of every keyword matches anything mentioning any one of them.
        Required terms are ANDed (or ORed in "any" mode) and the optional terms
        are ORed into a single broadening group, so a result has to name
        something specific to this topic:

            ("NAMFREL" OR "BARMM") AND (election OR parliamentary) -site:...
        """
        req = [self._quote(t) for t in self.required[:max_terms] if t.raw]
        opt = [self._quote(t) for t in self.optional[:6] if t.raw]
        exc = [self._quote(t) for t in self.excluded[:6] if t.raw]

        if not req:
            subject = self.subject
            if subject:
                req = ['"%s"' % subject if " " in subject else subject]
            elif opt:
                # Only generic words to go on -- searching for them worldwide
                # is exactly the drift we are trying to avoid, so keep it as a
                # single AND group rather than an OR.
                req, opt = opt, []

        if not req:
            return ""

        joiner = " AND " if self.match_mode == "all" else " OR "
        core = joiner.join(req)
        if len(req) > 1:
            core = "(%s)" % core

        parts = [core]
        if opt:
            parts.append("(%s)" % " OR ".join(opt))
        query = " AND ".join(parts)

        for e in exc:
            query += " -" + e
        return query


def spec_for(watch, subject=None):
    """Build a KeywordSpec from a MonitorWatch row or a plain dict."""
    if isinstance(watch, dict):
        def get(k, d=""):
            return watch.get(k, d)
    else:
        def get(k, d=""):
            return getattr(watch, k, d)

    subj = subject if subject is not None else (get("subject") or "")
    required = _split_lines(get("kw_required"))
    optional = _split_lines(get("kw_optional"))
    excluded = _split_lines(get("kw_excluded"))
    mode = get("kw_match_mode") or "all"

    # Nothing structured stored yet: interpret the legacy comma string.
    if not required and not optional and not excluded:
        required, optional, mode = _legacy_split(get("keywords"), subj)

    return KeywordSpec(required, optional, excluded, mode, subj)


def to_legacy_string(spec):
    """Render a spec back into the comma `keywords` column.

    Kept in sync so exports, older code paths and the search box all still see
    something meaningful.
    """
    parts = ["+" + t.raw for t in spec.required] + [t.raw for t in spec.optional]
    return ", ".join(parts)


def rules_hash(watch, spec=None):
    """A stable fingerprint of everything that affects a post's score.

    When this changes, every cached analysis for the watch is stale. Including
    the keyword spec, the modes, the weights and the thresholds means editing
    any of them invalidates exactly what it should.
    """
    if isinstance(watch, dict):
        def get(k, d=None):
            return watch.get(k, d)
    else:
        def get(k, d=None):
            return getattr(watch, k, d)

    spec = spec or spec_for(watch)
    payload = json.dumps({
        "kw": spec.describe(),
        "subject": get("subject") or "",
        "release": bool(get("mode_release")),
        "hunter": bool(get("mode_hunter")),
        "reference": get("reference_text") or "",
        "accounts": get("official_accounts") or "",
        "domains": get("official_domains") or "",
        "flags": get("custom_flags") or "",
        "weights": get("weights") if isinstance(get("weights"), dict) else {},
        "t_review": get("threshold_review"),
        "t_high": get("threshold_high"),
    }, sort_keys=True, default=str)
    return hashlib.sha1(payload.encode("utf-8", "replace")).hexdigest()[:16]
