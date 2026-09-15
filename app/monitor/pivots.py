"""Pivot links: "check this everywhere else".

An analyst holding a username, an email address, a photo or a domain wants the
same next step every time -- run it through the tools that cover ground this
app does not. Those tools cannot be automated: Google Lens, TinEye and Yandex
all block scripted access, and pretending otherwise would mean shipping a
feature that silently returns nothing.

What *is* honest and genuinely useful is generating the exact queries, so the
pivot is one click instead of a copy-paste into six tabs. That is what this
module does. It is the same approach the dork builder already takes, extended
to the identity and image tools an OSINT workflow actually leans on.

Nothing here makes a network request. Every function returns a list of
{label, url, note} and the browser opens what the analyst chooses.
"""

import re
from urllib.parse import quote, quote_plus


def _clean_username(value):
    """A bare handle from whatever was pasted: @name, a URL, or the name."""
    raw = str(value or "").strip()
    if not raw:
        return ""
    if "://" in raw or raw.count("/") >= 2:
        # Take the last meaningful path segment of a profile URL.
        parts = [p for p in re.sub(r"^[a-z]+://", "", raw).split("/") if p]
        raw = parts[-1] if len(parts) > 1 else raw
    raw = raw.split("?")[0].lstrip("@").strip()
    return raw


def username_pivots(username):
    """Where else this handle might exist.

    WhatsMyName, Sherlock and Maigret are the three the field actually uses;
    the first is a website, the other two are tools you run locally, so those
    come back as the command to run rather than a link that cannot work.
    """
    name = _clean_username(username)
    if not name:
        return []
    q = quote(name, safe="")
    return [
        {"label": "WhatsMyName",
         "url": "https://whatsmyname.app/?q=" + q,
         "note": "600+ sites, checked live in the browser."},
        {"label": "Google — exact handle",
         "url": "https://www.google.com/search?q=" + quote_plus('"%s"' % name),
         "note": "Quoted, so near-misses are excluded."},
        {"label": "Google — profile pages",
         "url": "https://www.google.com/search?q=" + quote_plus(
             '"%s" (site:facebook.com OR site:x.com OR site:instagram.com '
             'OR site:tiktok.com OR site:linkedin.com)' % name),
         "note": "The handle on the major platforms."},
        {"label": "Sherlock (local)",
         "url": "https://github.com/sherlock-project/sherlock",
         "note": "Run: sherlock %s" % name,
         "command": "sherlock %s" % name},
        {"label": "Maigret (local)",
         "url": "https://github.com/soxoj/maigret",
         "note": "Run: maigret %s" % name,
         "command": "maigret %s" % name},
        {"label": "GitHub users",
         "url": "https://github.com/search?type=users&q=" + q,
         "note": "Code and commits often carry a real name."},
        {"label": "Reddit user",
         "url": "https://www.reddit.com/user/%s" % q,
         "note": "Post history is usually public."},
    ]


def email_pivots(email):
    """What to do with an address, beyond the breach check."""
    address = str(email or "").strip()
    if "@" not in address:
        return []
    q = quote(address, safe="")
    local, _, domain = address.partition("@")
    return [
        {"label": "Have I Been Pwned",
         "url": "https://haveibeenpwned.com/account/" + q,
         "note": "Breach exposure, checked by hand on their site."},
        {"label": "EmailRep",
         "url": "https://emailrep.io/" + q,
         "note": "Reputation and risk signals."},
        {"label": "Epieos",
         "url": "https://epieos.com/?q=" + q,
         "note": "Linked Google and other accounts."},
        {"label": "Google — the address",
         "url": "https://www.google.com/search?q=" + quote_plus('"%s"' % address),
         "note": "Where it has been published."},
        {"label": "Google — the handle part",
         "url": "https://www.google.com/search?q=" + quote_plus('"%s"' % local),
         "note": "People reuse the local part as a username."},
        {"label": "Breach history of %s" % domain,
         "url": "/monitor/osint/breach?domain=" + quote(domain, safe=""),
         "note": "The provider's own breach record, checked here.",
         "internal": True},
    ]


def image_pivots(image_url):
    """Reverse-image search, across the engines that each see different things.

    Worth using more than one: Yandex is markedly better at faces, TinEye at
    finding the earliest copy, Google Lens at objects and places.
    """
    raw = str(image_url or "").strip()
    if not raw:
        return []
    q = quote(raw, safe="")
    return [
        {"label": "Google Lens",
         "url": "https://lens.google.com/uploadbyurl?url=" + q,
         "note": "Objects, places and visually similar images."},
        {"label": "Yandex Images",
         "url": "https://yandex.com/images/search?rpt=imageview&url=" + q,
         "note": "The strongest of these for faces."},
        {"label": "TinEye",
         "url": "https://tineye.com/search?url=" + q,
         "note": "Finds the earliest copy, which dates a photo."},
        {"label": "Bing Visual Search",
         "url": "https://www.bing.com/images/search?view=detailv2&iss=sbi&q=imgurl:" + q,
         "note": "Another index; catches what the others miss."},
        {"label": "InVID / WeVerify",
         "url": "https://www.invid-project.eu/tools-and-services/invid-verification-plugin/",
         "note": "Video and deepfake verification. Browser plugin."},
    ]


def domain_pivots(domain):
    """Infrastructure and history for a domain."""
    host = re.sub(r"^[a-z]+://", "", str(domain or "").strip().lower()
                  ).split("/")[0].split(":")[0].strip(".")
    if not host:
        return []
    q = quote(host, safe="")
    return [
        {"label": "Breach history",
         "url": "/monitor/osint/breach?domain=" + q,
         "note": "Known breaches of this domain, checked here.",
         "internal": True},
        {"label": "Wayback Machine",
         "url": "https://web.archive.org/web/*/%s*" % q,
         "note": "What the site used to say."},
        {"label": "crt.sh certificates",
         "url": "https://crt.sh/?q=%25." + q,
         "note": "Certificate log: reveals subdomains."},
        {"label": "urlscan.io",
         "url": "https://urlscan.io/domain/" + q,
         "note": "Past scans, screenshots and the requests it makes."},
        {"label": "VirusTotal",
         "url": "https://www.virustotal.com/gui/domain/" + q,
         "note": "Reputation across many engines."},
        {"label": "WHOIS",
         "url": "https://who.is/whois/" + q,
         "note": "Registration date and registrar."},
    ]


def for_profile(profile):
    """Every pivot that applies to a stored profile, grouped for the UI."""
    groups = []
    seen_names = set()

    for link in (profile.get("social_links") or []):
        handle = _clean_username(link.get("username") or link.get("url"))
        if handle and handle.lower() not in seen_names:
            seen_names.add(handle.lower())
            groups.append({"kind": "username", "subject": handle,
                           "pivots": username_pivots(handle)})

    codename = str(profile.get("codename") or "").strip()
    if codename and codename.lower() not in seen_names:
        groups.append({"kind": "username", "subject": codename,
                       "pivots": username_pivots(codename)})

    real = str(profile.get("real_name") or "").strip()
    if real:
        groups.append({"kind": "name", "subject": real,
                       "pivots": name_pivots(real)})

    return groups


def name_pivots(name):
    """Searches for a real name, which needs different handling from a handle."""
    person = str(name or "").strip()
    if len(person) < 3:
        return []
    quoted = quote_plus('"%s"' % person)
    return [
        {"label": "Google — exact name",
         "url": "https://www.google.com/search?q=" + quoted,
         "note": "Quoted, so the words stay together."},
        {"label": "Google — news",
         "url": "https://www.google.com/search?tbm=nws&q=" + quoted,
         "note": "Coverage mentioning them."},
        {"label": "Facebook people search",
         "url": "https://www.facebook.com/search/people?q=" + quote_plus(person),
         "note": "Needs a logged-in session to load."},
        {"label": "LinkedIn",
         "url": "https://www.google.com/search?q=" + quote_plus(
             'site:linkedin.com/in "%s"' % person),
         "note": "Via Google; LinkedIn's own search is walled."},
        {"label": "Philippine court records",
         "url": "https://www.google.com/search?q=" + quote_plus(
             '"%s" (site:sc.judiciary.gov.ph OR site:lawphil.net)' % person),
         "note": "Supreme Court and LawPhil decisions."},
    ]


def all_kinds():
    """What the UI can offer, for building menus."""
    return [
        {"kind": "username", "label": "Username", "fn": "username_pivots"},
        {"kind": "email", "label": "Email address", "fn": "email_pivots"},
        {"kind": "image", "label": "Image URL", "fn": "image_pivots"},
        {"kind": "domain", "label": "Domain", "fn": "domain_pivots"},
        {"kind": "name", "label": "Real name", "fn": "name_pivots"},
    ]


BUILDERS = {
    "username": username_pivots,
    "email": email_pivots,
    "image": image_pivots,
    "domain": domain_pivots,
    "name": name_pivots,
}


def build(kind, value):
    """Pivots of one kind, or [] when the kind is unknown."""
    fn = BUILDERS.get(str(kind or "").lower())
    return fn(value) if fn else []
