"""Breach intelligence: which organisations have been breached, and what leaked.

Three capabilities, deliberately separated by what they actually cost:

1. **The breach catalogue** -- Have I Been Pwned publishes every breach it
   tracks with no key at all: 1,000+ entries with dates, record counts and the
   classes of data exposed. That supports the pivot that matters most in
   practice: *has this domain ever been breached, when, and what leaked?*
   A domain appearing in a scam post is read differently once you know it was
   breached and its password hashes are in circulation.

2. **Pwned Passwords** -- also keyless, and privacy-preserving by design.
   Only the first five characters of the SHA-1 hash are sent; the service
   returns every suffix sharing that prefix and the match happens locally. The
   password never leaves this machine, and neither does its full hash.

3. **Per-account lookup** -- "has this email address appeared in a breach?" is
   the one that needs a paid key ($3.95/mo at the time of writing). The code
   path exists and reads a key from the vault; without one it says so plainly
   rather than returning an empty result, because "no breaches found" and "I
   did not look" are very different answers and only one of them is safe to
   act on.

Everything is cached: the catalogue changes a few times a week, so re-fetching
it per lookup would be rude to a free service and slow for no reason.
"""

import hashlib
import re
import threading
import time
from datetime import datetime

import requests

HIBP_API = "https://haveibeenpwned.com/api/v3"
PWNED_PASSWORDS_API = "https://api.pwnedpasswords.com/range/"
USER_AGENT = "profiler-2026-osint"

# The catalogue is public data that changes a few times a week.
CATALOGUE_TTL = 6 * 3600
_catalogue = {"fetched": 0.0, "breaches": [], "error": ""}
_lock = threading.Lock()

# Data classes worth calling out: these change what an exposure means.
SEVERE_CLASSES = {
    "passwords": "passwords",
    "password hints": "password hints",
    "security questions and answers": "security answers",
    "credit cards": "card numbers",
    "bank account numbers": "bank accounts",
    "government issued ids": "government IDs",
    "social security numbers": "national ID numbers",
    "biometric data": "biometric data",
    "partial credit card data": "partial card data",
    "auth tokens": "auth tokens",
    "private messages": "private messages",
    "physical addresses": "home addresses",
}


def _get(url, headers=None, timeout=20):
    """One GET against a breach service, through the SSRF guard."""
    from . import safefetch
    safefetch.check_url(url)
    head = {"User-Agent": USER_AGENT}
    if headers:
        head.update(headers)
    return requests.get(url, headers=head, timeout=timeout)


def _api_key():
    """The HIBP key from the vault, or "" when none is stored.

    The key lives in the vault rather than the environment so it is encrypted
    at rest alongside the session cookies, and never reaches the browser.
    """
    try:
        from . import vault
        from ..models import MonitorCredential
        if not vault.is_unlocked():
            return ""
        rows = MonitorCredential.query.filter_by(platform="hibp",
                                                 enabled=True).all()
        for row in rows:
            secret = vault.decrypt(row.secret_blob) or {}
            for field in ("api_key", "key", "token"):
                if secret.get(field):
                    return str(secret[field])
    except Exception:
        return ""
    return ""


# -- The catalogue -----------------------------------------------------------

def catalogue(force=False):
    """Every breach HIBP tracks. Keyless, cached, and safe to call often."""
    now = time.time()
    with _lock:
        fresh = (now - _catalogue["fetched"]) < CATALOGUE_TTL
        if _catalogue["breaches"] and fresh and not force:
            return list(_catalogue["breaches"])

    try:
        resp = _get(HIBP_API + "/breaches", timeout=30)
        if resp.status_code != 200:
            raise RuntimeError("HTTP %d" % resp.status_code)
        data = resp.json()
        if not isinstance(data, list):
            raise RuntimeError("unexpected payload")
    except Exception as e:
        with _lock:
            _catalogue["error"] = "Could not reach the breach catalogue: %s" % e
            # Serve a stale copy rather than nothing: week-old breach data is
            # still accurate about breaches that happened years ago.
            return list(_catalogue["breaches"])

    with _lock:
        _catalogue.update({"fetched": now, "breaches": data, "error": ""})
    return list(data)


def _norm_domain(value):
    """Reduce whatever was typed to a bare hostname."""
    raw = str(value or "").strip().lower()
    if not raw:
        return ""
    raw = re.sub(r"^[a-z]+://", "", raw)
    raw = raw.split("/")[0].split("?")[0].split("@")[-1]
    raw = raw.split(":")[0]
    return raw.strip(".")


def _summarise(breach):
    """One breach, in the shape the UI and the link map want."""
    classes = [str(c) for c in (breach.get("DataClasses") or [])]
    severe = [SEVERE_CLASSES[c.lower()] for c in classes
              if c.lower() in SEVERE_CLASSES]
    return {
        "name": breach.get("Name") or "",
        "title": breach.get("Title") or breach.get("Name") or "",
        "domain": breach.get("Domain") or "",
        "breach_date": breach.get("BreachDate") or "",
        "added_date": (breach.get("AddedDate") or "")[:10],
        "count": int(breach.get("PwnCount") or 0),
        "data_classes": classes,
        "severe": severe,
        "verified": bool(breach.get("IsVerified")),
        "fabricated": bool(breach.get("IsFabricated")),
        "sensitive": bool(breach.get("IsSensitive")),
        "malware": bool(breach.get("IsStealerLog")) or bool(breach.get("IsMalware")),
        "spam_list": bool(breach.get("IsSpamList")),
        "description": re.sub(r"<[^>]+>", "", breach.get("Description") or "")[:600],
    }


def check_domain(domain, breaches=None):
    """Every known breach of a domain, most recent first.

    Matches the domain itself and its subdomains, so `mail.example.com`
    finds a breach recorded against `example.com`.
    """
    host = _norm_domain(domain)
    if not host:
        return {"ok": False, "domain": "", "breaches": [],
                "note": "Enter a domain or a URL."}

    rows = breaches if breaches is not None else catalogue()
    if not rows:
        return {"ok": False, "domain": host, "breaches": [],
                "note": (_catalogue.get("error")
                         or "The breach catalogue is unavailable.")}

    hits = []
    for breach in rows:
        target = _norm_domain(breach.get("Domain"))
        if not target:
            continue
        if host == target or host.endswith("." + target) or target.endswith("." + host):
            hits.append(_summarise(breach))

    hits.sort(key=lambda b: b["breach_date"], reverse=True)
    total = sum(b["count"] for b in hits)
    return {
        "ok": True,
        "domain": host,
        "breaches": hits,
        "total_records": total,
        "note": _domain_note(host, hits, total),
    }


def _domain_note(host, hits, total):
    if not hits:
        return ("No breach of %s is in the catalogue. That is not proof it was "
                "never breached -- only that HIBP does not track one." % host)
    severe = sorted({s for b in hits for s in b["severe"]})
    parts = ["%s appears in %d breach%s (%s records)"
             % (host, len(hits), "" if len(hits) == 1 else "es",
                "{:,}".format(total))]
    if severe:
        parts.append("exposing " + ", ".join(severe[:4]))
    return ". ".join(parts) + "."


def search(term, limit=25):
    """Find breaches by name, title or domain -- the free-text pivot."""
    needle = str(term or "").strip().lower()
    if len(needle) < 2:
        return {"ok": False, "breaches": [],
                "note": "Enter at least two characters."}
    rows = catalogue()
    hits = [_summarise(b) for b in rows
            if needle in (b.get("Name") or "").lower()
            or needle in (b.get("Title") or "").lower()
            or needle in (b.get("Domain") or "").lower()]
    hits.sort(key=lambda b: b["count"], reverse=True)
    return {"ok": True, "breaches": hits[:limit], "matched": len(hits),
            "note": "%d breach(es) match %r" % (len(hits), term)}


def by_country_tld(tld=".ph", limit=50):
    """Breaches of domains under a country TLD.

    Local breaches are the ones an analyst here is most likely to need, and
    the catalogue has no country facet of its own.
    """
    suffix = tld if tld.startswith(".") else "." + tld
    rows = catalogue()
    hits = [_summarise(b) for b in rows
            if (b.get("Domain") or "").lower().endswith(suffix)]
    hits.sort(key=lambda b: b["count"], reverse=True)
    return {"ok": True, "tld": suffix, "breaches": hits[:limit],
            "matched": len(hits)}


# -- Pwned Passwords ---------------------------------------------------------

def check_password(password):
    """How many breaches a password appears in, without disclosing it.

    k-anonymity: the SHA-1 is computed locally, only the first five characters
    are sent, and the service answers with every suffix sharing that prefix.
    Neither the password nor its full hash leaves this machine.
    """
    if not password:
        return {"ok": False, "note": "Enter a password to check."}

    digest = hashlib.sha1(password.encode("utf-8")).hexdigest().upper()
    prefix, suffix = digest[:5], digest[5:]

    try:
        resp = _get(PWNED_PASSWORDS_API + prefix,
                    headers={"Add-Padding": "true"})
        if resp.status_code != 200:
            return {"ok": False,
                    "note": "The password service answered HTTP %d."
                            % resp.status_code}
    except Exception as e:
        return {"ok": False,
                "note": "Could not reach the password service: %s"
                        % type(e).__name__}

    count = 0
    for line in resp.text.splitlines():
        parts = line.strip().split(":")
        if len(parts) == 2 and parts[0].upper() == suffix:
            try:
                count = int(parts[1])
            except ValueError:
                count = 0
            break

    return {
        "ok": True,
        "pwned": count > 0,
        "count": count,
        "checked": len(resp.text.splitlines()),
        "note": (("This password appears in %s known breaches. Do not use it "
                  "anywhere." % "{:,}".format(count)) if count else
                 "This password does not appear in any known breach."),
        "privacy": ("Only the first 5 characters of the hash were sent; the "
                    "password itself never left this machine."),
    }


# -- Per-account lookup (needs a paid key) -----------------------------------

def check_account(email):
    """Breaches an email address appears in. Requires a HIBP API key.

    Without a key this reports that it could not look, rather than returning
    an empty list -- "no breaches found" and "I did not check" would otherwise
    be indistinguishable, and one of them is dangerously reassuring.
    """
    address = str(email or "").strip()
    if "@" not in address:
        return {"ok": False, "available": True, "breaches": [],
                "note": "Enter an email address."}

    key = _api_key()
    if not key:
        return {
            "ok": False,
            "available": False,
            "breaches": [],
            "note": ("Per-address lookup needs a Have I Been Pwned API key, "
                     "which is a paid subscription. Add one to the vault "
                     "(platform 'hibp') to enable this. The domain check and "
                     "password check below need no key and still work."),
        }

    url = "%s/breachedaccount/%s?truncateResponse=false" % (
        HIBP_API, requests.utils.quote(address))
    try:
        resp = _get(url, headers={"hibp-api-key": key})
    except Exception as e:
        return {"ok": False, "available": True, "breaches": [],
                "note": "Could not reach HIBP: %s" % type(e).__name__}

    if resp.status_code == 404:
        return {"ok": True, "available": True, "breaches": [],
                "note": "%s does not appear in any tracked breach." % address}
    if resp.status_code == 401:
        return {"ok": False, "available": False, "breaches": [],
                "note": "The stored HIBP key was rejected. Check it in the vault."}
    if resp.status_code == 429:
        return {"ok": False, "available": True, "breaches": [],
                "note": "HIBP is rate-limiting this key. Wait a moment."}
    if resp.status_code != 200:
        return {"ok": False, "available": True, "breaches": [],
                "note": "HIBP answered HTTP %d." % resp.status_code}

    try:
        rows = resp.json()
    except ValueError:
        return {"ok": False, "available": True, "breaches": [],
                "note": "HIBP returned something unreadable."}

    hits = [_summarise(b) for b in rows]
    hits.sort(key=lambda b: b["breach_date"], reverse=True)
    severe = sorted({s for b in hits for s in b["severe"]})
    return {
        "ok": True,
        "available": True,
        "breaches": hits,
        "note": ("%s appears in %d breach(es)%s."
                 % (address, len(hits),
                    (", exposing " + ", ".join(severe[:4])) if severe else "")),
    }


def status():
    """What this module can do right now, for the capability report."""
    rows = catalogue()
    return {
        "catalogue_size": len(rows),
        "catalogue_error": _catalogue.get("error", ""),
        "fetched_at": (datetime.utcfromtimestamp(_catalogue["fetched"])
                       .strftime("%Y-%m-%d %H:%M") if _catalogue["fetched"] else ""),
        "account_lookup": bool(_api_key()),
        "password_check": True,
        "domain_check": bool(rows),
    }
