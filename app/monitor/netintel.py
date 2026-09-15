"""Network intelligence: IP flagging, geolocation and phishing-link checks.

Three related jobs that all start from the same place -- the URLs and IP
addresses sitting inside collected posts:

    extract_indicators()  Pull URLs, IPs and domains out of post text.
    expand()              Resolve a shortener to its real destination.
    check_phishing()      Test a URL against open threat feeds + heuristics.
    geolocate()           Place a host or IP on the map.

Boundaries, deliberately drawn
------------------------------
This module *inspects and reports*. It never fetches a suspected phishing
page's body, never submits anything to a form, and never tries to look like a
victim's browser. Shortener expansion uses HEAD with redirects capped, so the
destination is revealed without loading the payload. That is enough to warn an
analyst, which is the entire point.

Everything external is optional and cached. With no network and no local
database, the heuristics still run and the module still returns useful answers
-- it just says so rather than silently degrading.
"""

import ipaddress
import json
import os
import re
import socket
import threading
import time
from datetime import datetime
from urllib.parse import urlparse

import requests

from . import capabilities

# -- Caches ------------------------------------------------------------------
# Every lookup here is either rate-limited or slow, and the same handful of
# hosts recur across a whole watch, so caching is what makes this usable.

_lock = threading.Lock()
_geo_cache = {}
_expand_cache = {}
_dns_cache = {}
_feed_cache = {"data": None, "fetched": 0}

GEO_TTL = 86400        # a host's country does not move often
EXPAND_TTL = 3600
FEED_TTL = 3600        # phishing feeds update continuously
REQUEST_TIMEOUT = 8

# Free, no-key geolocation. Rate limited to ~45 requests/minute, which the
# cache keeps us well under.
GEO_ENDPOINT = "http://ip-api.com/json/{ip}?fields=status,country,countryCode,region,regionName,city,lat,lon,isp,org,as,proxy,hosting,query"

# Open phishing/malware feeds. All free, all plain text or JSON.
PHISH_FEEDS = {
    "openphish": "https://openphish.com/feed.txt",
    "urlhaus": "https://urlhaus.abuse.ch/downloads/text_recent/",
}

SHORTENERS = {
    "bit.ly", "tinyurl.com", "t.co", "cutt.ly", "rb.gy", "is.gd", "goo.gl",
    "s.id", "tiny.cc", "shorturl.at", "ow.ly", "buff.ly", "adf.ly", "bit.do",
    "rebrand.ly", "shorte.st", "t.ly", "lnkd.in", "trib.al", "dlvr.it",
}

# TLDs that dominate throwaway phishing infrastructure.
RISKY_TLD = re.compile(
    r"\.(xyz|top|click|shop|live|online|site|icu|buzz|loan|win|rest|cyou|"
    r"monster|zip|mov|gq|cf|tk|ml|ga|work|fit|beauty|sbs|lol|quest)$", re.I)

CREDENTIAL_SEGMENTS = {
    "login", "log-in", "signin", "sign-in", "signup", "sign-up", "verify",
    "verification", "register", "registration", "claim", "voucher", "reward",
    "rewards", "otp", "account", "accounts", "password", "reset", "confirm",
    "auth", "authenticate", "wallet", "payment", "billing", "secure",
    "unlock", "recover", "validate", "update-info",
}

URL_RE = re.compile(
    r"(?:https?://|www\.)[^\s<>\"'\)\]]+"
    r"|\b(?:" + "|".join(re.escape(s) for s in sorted(SHORTENERS)) + r")/[^\s<>\"'\)\]]+",
    re.I)

# An IPv4 that is not part of a version string or a decimal number.
IPV4_RE = re.compile(
    r"(?<![\w.])((?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)"
    r"(?:\.(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)){3})(?![\w.])")
IPV6_RE = re.compile(r"(?<![\w:])((?:[A-Fa-f0-9]{1,4}:){2,7}[A-Fa-f0-9]{1,4})(?![\w:])")
EMAIL_RE = re.compile(r"\b[\w.+-]+@([\w-]+\.[\w.-]+)\b")


def _cache_get(store, key, ttl):
    with _lock:
        hit = store.get(key)
    if not hit:
        return None
    ts, value = hit
    if time.time() - ts > ttl:
        with _lock:
            store.pop(key, None)
        return None
    return value


def _cache_put(store, key, value, cap=2048):
    with _lock:
        if len(store) >= cap:
            for k in sorted(store, key=lambda k: store[k][0])[:cap // 4]:
                store.pop(k, None)
        store[key] = (time.time(), value)


def host_of(url):
    try:
        candidate = url if re.match(r"^https?://", url, re.I) else "https://" + url
        return (urlparse(candidate).hostname or "").lower().lstrip(".")
    except ValueError:
        return ""


def registrable(host):
    """The registrable part of a hostname, near enough for grouping.

    Not a full public-suffix implementation -- it handles the common two-level
    country suffixes (co.uk, com.ph) and otherwise takes the last two labels.
    """
    parts = [p for p in (host or "").split(".") if p]
    if len(parts) < 2:
        return host or ""
    two_level = {"co", "com", "net", "org", "gov", "edu", "ac", "mil"}
    if len(parts) >= 3 and parts[-2] in two_level and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


# -- Indicator extraction ----------------------------------------------------

def classify_ip(raw):
    """Describe an IP address: is it routable, private, reserved, special?"""
    try:
        ip = ipaddress.ip_address(raw)
    except ValueError:
        return None
    kind = "public"
    if ip.is_private:
        kind = "private"
    elif ip.is_loopback:
        kind = "loopback"
    elif ip.is_link_local:
        kind = "link-local"
    elif ip.is_multicast:
        kind = "multicast"
    elif ip.is_reserved or ip.is_unspecified:
        kind = "reserved"
    return {
        "ip": str(ip),
        "version": ip.version,
        "kind": kind,
        "routable": kind == "public",
    }


def extract_indicators(text, url=""):
    """Pull every URL, IP address and email domain out of a post.

    Returns deduplicated lists, each entry carrying enough context for the UI
    to explain why it is interesting.
    """
    blob = (text or "") + ("\n" + url if url else "")

    urls, seen_u = [], set()
    for raw in URL_RE.findall(blob):
        clean = re.sub(r"[),.!?;:'\"]+$", "", raw)
        if not clean or clean.lower() in seen_u:
            continue
        seen_u.add(clean.lower())
        h = host_of(clean)
        urls.append({
            "url": clean,
            "host": h,
            "domain": registrable(h),
            "shortened": h in SHORTENERS,
            "scheme": "http" if clean.lower().startswith("http://") else "https",
        })

    ips, seen_i = [], set()
    for raw in IPV4_RE.findall(blob) + IPV6_RE.findall(blob):
        info = classify_ip(raw)
        if not info or info["ip"] in seen_i:
            continue
        seen_i.add(info["ip"])
        ips.append(info)

    # An IP used as a URL host is the strongest form of this signal, so make
    # sure it is captured even when the regex above skipped it.
    for u in urls:
        info = classify_ip(u["host"])
        if info and info["ip"] not in seen_i:
            seen_i.add(info["ip"])
            info["from_url"] = u["url"]
            ips.append(info)

    domains = sorted({registrable(d) for d in EMAIL_RE.findall(blob) if d})

    return {"urls": urls, "ips": ips, "email_domains": domains}


# -- Shortener expansion -----------------------------------------------------

def expand(url, max_hops=5):
    """Reveal a shortener's destination without loading the page.

    HEAD only, redirects followed manually and capped. If the destination is a
    phishing page, we learn its address and never touch its body.
    """
    cached = _cache_get(_expand_cache, url, EXPAND_TTL)
    if cached is not None:
        return cached

    chain, current = [], url
    result = {"final": url, "chain": [], "expanded": False, "note": ""}
    try:
        for _ in range(max_hops):
            resp = requests.head(
                current, allow_redirects=False, timeout=REQUEST_TIMEOUT,
                headers={"User-Agent": "Mozilla/5.0 (compatible; OSINT-monitor)"})
            if resp.status_code in (301, 302, 303, 307, 308):
                nxt = resp.headers.get("Location")
                if not nxt:
                    break
                if nxt.startswith("/"):
                    p = urlparse(current)
                    nxt = "%s://%s%s" % (p.scheme, p.netloc, nxt)
                chain.append(nxt)
                current = nxt
                continue
            break
        result = {
            "final": current,
            "chain": chain,
            "expanded": bool(chain),
            "note": ("Resolved through %d redirect(s)" % len(chain)) if chain
                    else "No redirect",
        }
    except requests.exceptions.RequestException as e:
        result = {"final": url, "chain": chain, "expanded": bool(chain),
                  "note": "Could not resolve: %s" % type(e).__name__}

    _cache_put(_expand_cache, url, result)
    return result


def resolve_host(host):
    """Forward-resolve a hostname to an IP, cached."""
    if not host:
        return None
    cached = _cache_get(_dns_cache, host, GEO_TTL)
    if cached is not None:
        return cached
    try:
        ip = socket.gethostbyname(host)
    except (socket.gaierror, socket.herror, OSError, UnicodeError):
        ip = None
    _cache_put(_dns_cache, host, ip)
    return ip


# -- Phishing feeds ----------------------------------------------------------

def _load_feeds(force=False):
    """Fetch and index the open phishing feeds.

    Indexed by full URL and by host, because feeds list specific URLs but an
    attacker rotates the path far more often than the domain.
    """
    with _lock:
        fresh = (_feed_cache["data"] is not None
                 and time.time() - _feed_cache["fetched"] < FEED_TTL)
    if fresh and not force:
        return _feed_cache["data"]

    urls, hosts, sources = set(), {}, {}
    for name, endpoint in PHISH_FEEDS.items():
        try:
            resp = requests.get(endpoint, timeout=REQUEST_TIMEOUT,
                                headers={"User-Agent": "OSINT-monitor/1.0"})
            resp.raise_for_status()
        except requests.exceptions.RequestException:
            sources[name] = "unreachable"
            continue
        count = 0
        for line in resp.text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            urls.add(line.lower())
            h = host_of(line)
            if h:
                hosts.setdefault(h, set()).add(name)
            count += 1
        sources[name] = "%d entries" % count

    data = {"urls": urls, "hosts": hosts, "sources": sources,
            "fetched_at": datetime.utcnow().isoformat(timespec="seconds")}
    with _lock:
        _feed_cache["data"] = data
        _feed_cache["fetched"] = time.time()
    return data


def feed_status():
    with _lock:
        data = _feed_cache["data"]
        age = time.time() - _feed_cache["fetched"] if data else None
    if not data:
        return {"loaded": False, "sources": {}, "note": "Feeds not fetched yet"}
    return {
        "loaded": True,
        "sources": data["sources"],
        "hosts": len(data["hosts"]),
        "urls": len(data["urls"]),
        "fetched_at": data["fetched_at"],
        "age_seconds": int(age or 0),
    }


# -- Phishing assessment -----------------------------------------------------

def check_phishing(url, use_feeds=True, do_expand=True):
    """Assess one URL. Returns a verdict, a score and the reasons behind it.

    The score is advisory and always shown alongside its reasons -- an analyst
    should be able to disagree with it on sight.
    """
    original = url
    reasons = []
    score = 0

    exp = {"final": url, "chain": [], "expanded": False, "note": ""}
    host = host_of(url)
    if do_expand and host in SHORTENERS:
        exp = expand(url)
        if exp["expanded"]:
            reasons.append({
                "label": "%s hides its destination (%s)" % (host, host_of(exp["final"])),
                "weight": 15, "kind": "shortener"})
            score += 15
            url = exp["final"]
            host = host_of(url)
        else:
            reasons.append({"label": "%s is a shortener that would not resolve" % host,
                            "weight": 10, "kind": "shortener"})
            score += 10

    if not host:
        return {"url": original, "verdict": "unknown", "score": 0,
                "reasons": [{"label": "No hostname could be read from this URL",
                             "weight": 0, "kind": "parse"}],
                "expansion": exp, "feeds": [], "ip": None}

    try:
        parsed = urlparse(url if re.match(r"^https?://", url, re.I) else "https://" + url)
    except ValueError:
        parsed = None

    # -- feed hits: the strongest signal available -----------------------
    feed_hits = []
    if use_feeds:
        data = _load_feeds()
        if url.lower() in data["urls"]:
            feed_hits = sorted(data["hosts"].get(host, {"feed"}))
            reasons.append({"label": "Listed as a live phishing URL by %s"
                                     % ", ".join(feed_hits),
                            "weight": 60, "kind": "feed"})
            score += 60
        elif host in data["hosts"]:
            feed_hits = sorted(data["hosts"][host])
            reasons.append({"label": "Host is listed in %s for other URLs"
                                     % ", ".join(feed_hits),
                            "weight": 40, "kind": "feed"})
            score += 40

    # -- heuristics -------------------------------------------------------
    ip_info = classify_ip(host)
    if ip_info:
        reasons.append({"label": "Uses a raw IP address instead of a domain name",
                        "weight": 25, "kind": "ip"})
        score += 25
        if not ip_info["routable"]:
            reasons.append({"label": "That address is %s and is not reachable "
                                     "from the public internet" % ip_info["kind"],
                            "weight": 5, "kind": "ip"})
            score += 5

    if url.lower().startswith("http://"):
        reasons.append({"label": "Unencrypted http, so anything typed in is sent "
                                 "in the clear", "weight": 10, "kind": "transport"})
        score += 10

    if "xn--" in host:
        reasons.append({"label": "Punycode in the hostname, used to build "
                                 "look-alike domains", "weight": 25, "kind": "homograph"})
        score += 25

    if RISKY_TLD.search(host):
        reasons.append({"label": "%s uses a TLD common in throwaway "
                                 "infrastructure" % registrable(host),
                        "weight": 12, "kind": "tld"})
        score += 12

    if host.count(".") >= 4:
        reasons.append({"label": "Unusually deep subdomain chain, often used to "
                                 "bury a real domain in the URL",
                        "weight": 10, "kind": "structure"})
        score += 10

    if parsed:
        segments = [s.lower() for s in re.split(r"[/_.\-]+", parsed.path or "") if s]
        hits = CREDENTIAL_SEGMENTS.intersection(segments)
        if hits:
            reasons.append({"label": "Path asks for credentials or a sign-up (%s)"
                                     % ", ".join(sorted(hits)[:3]),
                            "weight": 18, "kind": "credential"})
            score += 18
        if re.search(r"@", parsed.netloc or ""):
            reasons.append({"label": "Userinfo before the host, a classic trick to "
                                     "make a URL read as a trusted domain",
                            "weight": 25, "kind": "structure"})
            score += 25

    score = max(0, min(100, score))
    verdict = ("malicious" if score >= 60 else
               "suspicious" if score >= 30 else "clean")
    return {
        "url": original,
        "final_url": url,
        "host": host,
        "domain": registrable(host),
        "verdict": verdict,
        "score": score,
        "reasons": reasons,
        "expansion": exp,
        "feeds": feed_hits,
        "ip": ip_info,
    }


# -- Geolocation -------------------------------------------------------------

def _geo_local(ip):
    """Look up a local MaxMind GeoLite2 database, when one is installed.

    Preferred over the network service: no rate limit, no third party sees the
    addresses being investigated, and it works offline.
    """
    db_path = os.environ.get("GEOIP_DB") or os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "instance", "GeoLite2-City.mmdb")
    if not os.path.exists(db_path):
        return None
    reader_mod = capabilities.load("geoip2") if "geoip2" in capabilities.REGISTRY else None
    if reader_mod is None:
        try:
            import geoip2.database as reader_mod  # noqa
        except ImportError:
            return None
    try:
        import geoip2.database
        with geoip2.database.Reader(db_path) as reader:
            r = reader.city(ip)
            return {
                "ip": ip, "ok": True, "source": "local GeoLite2",
                "country": r.country.name or "",
                "country_code": r.country.iso_code or "",
                "region": (r.subdivisions.most_specific.name
                           if r.subdivisions else ""),
                "city": r.city.name or "",
                "lat": r.location.latitude, "lon": r.location.longitude,
                "isp": "", "org": "", "asn": "",
                "proxy": False, "hosting": False,
            }
    except Exception:
        return None


def geolocate(target):
    """Locate a hostname or IP. Cached; returns ok=False rather than raising."""
    if not target:
        return {"ok": False, "note": "Nothing to locate"}

    ip = target if classify_ip(target) else resolve_host(target)
    if not ip:
        return {"ok": False, "target": target,
                "note": "Hostname did not resolve to an address"}

    info = classify_ip(ip)
    if info and not info["routable"]:
        return {"ok": False, "ip": ip, "target": target, "kind": info["kind"],
                "note": "%s addresses have no geographic location" % info["kind"].capitalize()}

    cached = _cache_get(_geo_cache, ip, GEO_TTL)
    if cached is not None:
        return dict(cached, target=target)

    local = _geo_local(ip)
    if local:
        _cache_put(_geo_cache, ip, local)
        return dict(local, target=target)

    try:
        resp = requests.get(GEO_ENDPOINT.format(ip=ip), timeout=REQUEST_TIMEOUT,
                            headers={"User-Agent": "OSINT-monitor/1.0"})
        resp.raise_for_status()
        d = resp.json()
    except requests.exceptions.RequestException as e:
        return {"ok": False, "ip": ip, "target": target,
                "note": "Geolocation service unreachable: %s" % type(e).__name__}
    except ValueError:
        return {"ok": False, "ip": ip, "target": target,
                "note": "Geolocation service returned an unreadable response"}

    if d.get("status") != "success":
        out = {"ok": False, "ip": ip, "target": target,
               "note": "No location on record for this address"}
        _cache_put(_geo_cache, ip, out)
        return out

    out = {
        "ok": True, "ip": ip, "source": "ip-api.com",
        "country": d.get("country") or "", "country_code": d.get("countryCode") or "",
        "region": d.get("regionName") or "", "city": d.get("city") or "",
        "lat": d.get("lat"), "lon": d.get("lon"),
        "isp": d.get("isp") or "", "org": d.get("org") or "",
        "asn": d.get("as") or "",
        # A datacenter or proxy origin matters: content claiming to be local
        # grassroots posting that resolves to a hosting provider is worth a look.
        "proxy": bool(d.get("proxy")), "hosting": bool(d.get("hosting")),
    }
    _cache_put(_geo_cache, ip, out)
    return dict(out, target=target)


def enrich_post(text, url="", do_geo=True, do_phish=True, do_breach=True):
    """Everything this module knows about one post, in one call."""
    ind = extract_indicators(text, url)
    findings = {"indicators": ind, "phishing": [], "geo": [], "flags": [],
                "breaches": []}

    hosts_seen = set()
    for u in ind["urls"][:12]:
        if do_phish:
            check = check_phishing(u["url"])
            if check["verdict"] != "clean":
                findings["phishing"].append(check)
        h = host_of(u.get("url") or "")
        if do_geo and h and h not in hosts_seen:
            hosts_seen.add(h)
            g = geolocate(h)
            if g.get("ok"):
                findings["geo"].append(dict(g, via="link", host=h))

    for ip in ind["ips"][:6]:
        if not ip["routable"]:
            findings["flags"].append(
                "%s is a %s address and cannot be reached publicly"
                % (ip["ip"], ip["kind"]))
            continue
        if do_geo:
            g = geolocate(ip["ip"])
            if g.get("ok"):
                findings["geo"].append(dict(g, via="ip in text"))

    # Breach history of the domains a post links to. A link to an
    # organisation whose credentials are already in circulation reads
    # differently from a link to one that has never been breached -- and it is
    # free to check, because the breach catalogue is public.
    if do_breach:
        from . import breachintel
        catalogue = breachintel.catalogue()
        if catalogue:
            for domain in list(dict.fromkeys(
                    u.get("domain") for u in ind["urls"][:8] if u.get("domain")))[:5]:
                hit = breachintel.check_domain(domain, breaches=catalogue)
                if hit["ok"] and hit["breaches"]:
                    findings["breaches"].append({
                        "domain": hit["domain"],
                        "count": len(hit["breaches"]),
                        "records": hit["total_records"],
                        "latest": hit["breaches"][0]["breach_date"],
                        "severe": sorted({s for b in hit["breaches"]
                                          for s in b["severe"]}),
                    })
                    findings["flags"].append(
                        "%s has been breached (%s records exposed)"
                        % (hit["domain"], "{:,}".format(hit["total_records"])))

    for g in findings["geo"]:
        if g.get("hosting"):
            findings["flags"].append(
                "%s is hosted in a datacenter (%s), not a consumer connection"
                % (g.get("host") or g["ip"], g.get("isp") or "unknown provider"))
        if g.get("proxy"):
            findings["flags"].append(
                "%s resolves through a proxy or VPN" % (g.get("host") or g["ip"]))

    return findings
