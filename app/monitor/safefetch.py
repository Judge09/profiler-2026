"""Guard against fetching internal services on someone else's behalf.

Several collectors take a URL from the analyst and fetch it server-side: the
`page`, `rss` and `article` sources, feed subscriptions, and link expansion.
Without a check, that turns the server into a proxy into its own network. On a
cloud host the damage is concrete and immediate:

    http://169.254.169.254/latest/meta-data/iam/security-credentials/
        -> the instance's cloud credentials

    http://127.0.0.1:8080/admin
        -> whatever internal service is listening, returned as "post text"

Neither needs anything clever; both are just a URL typed into a field. This was
verified against a local service before the guard existed -- it returned the
page contents happily.

What this module enforces
-------------------------
* http and https only. No file://, ftp://, gopher://, data: and so on.
* The hostname must resolve to a public address. Private, loopback,
  link-local, multicast, reserved and unspecified ranges are all refused,
  for every address the name resolves to rather than just the first.
* The check is repeated after every redirect, because an allowed external URL
  may redirect to 127.0.0.1 -- which is the standard way around a naive
  check that only looks at what the user typed.
* No credentials embedded in the URL, and no non-standard ports.

What it deliberately does not do
--------------------------------
It does not try to defeat a determined attacker with DNS rebinding: the name
is resolved for the check and resolved again by the HTTP client, and a record
that changes in between would slip through. Closing that needs the connection
pinned to the validated address, which `requests` does not expose cleanly.
Rebinding needs attacker-controlled DNS and precise timing; typing
`169.254.169.254` into a form needs neither, and that is what this stops.

`ALLOW_PRIVATE_FETCH=1` disables the guard, for someone deliberately running
this against a lab network on a host that is not reachable from anywhere else.
"""

import ipaddress
import os
import socket
from urllib.parse import urlparse

# Ports a browser would use. A URL pointing at 6379 (Redis) or 9200
# (Elasticsearch) is not a web page someone wants collected, it is a probe.
ALLOWED_PORTS = {80, 443, 8080, 8443}

MAX_REDIRECTS = 5


class BlockedURL(ValueError):
    """A URL that must not be fetched, with a reason fit to show an analyst."""


def _guard_disabled():
    return str(os.environ.get("ALLOW_PRIVATE_FETCH", "")).lower() in (
        "1", "true", "yes")


def _addresses_for(host):
    """Every address a hostname resolves to, or [] when it does not resolve.

    All of them are checked, not just the first: a name that returns one public
    and one loopback address would otherwise pass the check and then connect to
    whichever the client picked.
    """
    try:
        infos = socket.getaddrinfo(host, None)
    except (socket.gaierror, UnicodeError, ValueError):
        return []
    out = []
    for info in infos:
        addr = info[4][0]
        try:
            out.append(ipaddress.ip_address(addr))
        except ValueError:
            continue
    return out


def _classify(ip):
    """Why this address is not fetchable, or None when it is fine."""
    if ip.is_loopback:
        return "a loopback address (the server itself)"
    if ip.is_link_local:
        # 169.254.169.254 lives here: the cloud metadata endpoint.
        return "a link-local address (cloud metadata lives here)"
    if ip.is_private:
        return "a private network address"
    if ip.is_multicast:
        return "a multicast address"
    if ip.is_reserved or ip.is_unspecified:
        return "a reserved address"
    # IPv6 can wrap an IPv4 address; unwrap before trusting it.
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        return _classify(mapped)
    sixto4 = getattr(ip, "sixtofour", None)
    if sixto4 is not None:
        return _classify(sixto4)
    return None


def check_url(url):
    """Raise BlockedURL unless this URL is safe to fetch server-side.

    Returns the parsed URL on success, so callers that need the host do not
    parse it a second time.
    """
    raw = str(url or "").strip()
    if not raw:
        raise BlockedURL("No URL was given.")

    try:
        parsed = urlparse(raw)
    except ValueError:
        raise BlockedURL("That URL could not be parsed.")

    if parsed.scheme.lower() not in ("http", "https"):
        raise BlockedURL(
            "Only http:// and https:// URLs can be fetched (got %r)."
            % (parsed.scheme or "no scheme"))

    if parsed.username or parsed.password:
        raise BlockedURL("URLs carrying credentials are not fetched.")

    host = parsed.hostname
    if not host:
        raise BlockedURL("That URL has no hostname.")

    if _guard_disabled():
        return parsed

    port = parsed.port
    if port is not None and port not in ALLOWED_PORTS:
        raise BlockedURL(
            "Port %d is not a web port, so it is not fetched. Allowed: %s."
            % (port, ", ".join(str(p) for p in sorted(ALLOWED_PORTS))))

    # A literal IP needs no resolution; a name does.
    try:
        literal = ipaddress.ip_address(host)
        addresses = [literal]
    except ValueError:
        addresses = _addresses_for(host)
        if not addresses:
            raise BlockedURL("%s does not resolve." % host)

    for ip in addresses:
        problem = _classify(ip)
        if problem:
            raise BlockedURL(
                "%s resolves to %s, which is %s. Fetching it would reach this "
                "server's own network rather than the public internet."
                % (host, ip, problem))
    return parsed


def is_safe(url):
    """True when `url` may be fetched. For callers that want no exception."""
    try:
        check_url(url)
        return True
    except BlockedURL:
        return False


def safe_get(url, fetch, **kwargs):
    """Fetch `url` through `fetch`, validating it and every redirect.

    `fetch` is the underlying getter (`collectors.http_get`, or a session's
    `get`). Redirects are followed one at a time here rather than by the HTTP
    client, because the client would follow a redirect to 127.0.0.1 without
    ever showing it to us -- which is precisely the bypass this closes.
    """
    check_url(url)
    kwargs.pop("allow_redirects", None)

    current = url
    for _ in range(MAX_REDIRECTS):
        resp = fetch(current, allow_redirects=False, **kwargs)
        if resp.status_code not in (301, 302, 303, 307, 308):
            return resp

        location = resp.headers.get("Location")
        if not location:
            return resp

        # A relative redirect stays on a host already validated.
        if location.startswith("/"):
            parsed = urlparse(current)
            location = "%s://%s%s" % (parsed.scheme, parsed.netloc, location)

        check_url(location)
        current = location

    raise BlockedURL("That URL redirected more than %d times." % MAX_REDIRECTS)
