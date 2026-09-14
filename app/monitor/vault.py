"""Encrypted credential vault.

Stores the secrets needed to reach authenticated sources: session cookies, API
tokens, and (reluctantly) passwords. Everything is encrypted at rest with
Fernet (AES-128-CBC + HMAC) using a key derived from a master passphrase.

Threat model and its limits
---------------------------
This protects the database file. It does NOT protect a running app: once the
vault is unlocked, the key lives in process memory and any code in this app can
read the secrets. It also cannot protect against a compromised machine.

Practical guidance, in order of preference:
  1. Prefer API tokens. Revocable, scoped, and their loss is contained.
  2. Prefer session cookies over passwords. Paste cookies from a browser you
     logged into yourself -- no password ever touches this app, 2FA is already
     satisfied, and the cookie expires on its own.
  3. Passwords last. Storing one means storing full account control, and
     automating a login is what actually triggers platform lockouts.

Never put a personal account in here. Use a throwaway.
"""

import base64
import json
import os
from datetime import datetime

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

# Where the salt lives. The key itself is never written to disk.
_SALT_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "instance", "vault.salt")

_KDF_ROUNDS = 480_000

# Process-local unlocked key. Cleared on lock() and lost on restart by design.
_key = None


class VaultLocked(Exception):
    """Raised when a secret is needed but the vault has not been unlocked."""


def _salt():
    """Read (or create) the KDF salt."""
    os.makedirs(os.path.dirname(_SALT_PATH), exist_ok=True)
    if not os.path.exists(_SALT_PATH):
        with open(_SALT_PATH, "wb") as f:
            f.write(os.urandom(16))
    with open(_SALT_PATH, "rb") as f:
        return f.read()


def derive_key(passphrase):
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=_salt(),
                     iterations=_KDF_ROUNDS)
    return base64.urlsafe_b64encode(kdf.derive(passphrase.encode("utf-8")))


def unlock(passphrase):
    """Derive and hold the key for this process."""
    global _key
    _key = derive_key(passphrase)
    return True


def lock():
    global _key
    _key = None


def is_unlocked():
    return _key is not None


def auto_unlock():
    """Unlock from MONITOR_VAULT_KEY if it is set.

    Lets the app come up ready in a trusted local setup without a manual step.
    """
    secret = os.environ.get("MONITOR_VAULT_KEY")
    if secret and not is_unlocked():
        unlock(secret)
        return True
    return False


def encrypt(payload):
    """Encrypt a JSON-serialisable dict to a storable string."""
    if not is_unlocked():
        raise VaultLocked("The credential vault is locked.")
    raw = json.dumps(payload).encode("utf-8")
    return Fernet(_key).encrypt(raw).decode("ascii")


def decrypt(blob):
    """Decrypt a stored string back to a dict. Returns None if unreadable."""
    if not is_unlocked():
        raise VaultLocked("The credential vault is locked.")
    if not blob:
        return None
    try:
        return json.loads(Fernet(_key).decrypt(blob.encode("ascii")))
    except (InvalidToken, ValueError):
        # Wrong passphrase, or the record predates the current key.
        return None


def verify(blob):
    """True when a stored blob decrypts with the current key."""
    try:
        return decrypt(blob) is not None
    except VaultLocked:
        return False


# -- Cookie parsing ----------------------------------------------------------

def parse_cookies(raw):
    """Accept the several shapes people copy cookies in as.

    Supports: a Cookie: header string (a=1; b=2), Netscape cookies.txt, and the
    JSON array produced by the common "export cookies" extensions.
    """
    raw = (raw or "").strip()
    if not raw:
        return []

    # JSON array from an export extension.
    if raw.startswith("[") or raw.startswith("{"):
        try:
            data = json.loads(raw)
            items = data if isinstance(data, list) else data.get("cookies", [])
            out = []
            for c in items:
                name = c.get("name") or c.get("Name")
                value = c.get("value") or c.get("Value")
                if name and value is not None:
                    out.append({
                        "name": name, "value": value,
                        "domain": c.get("domain") or c.get("Domain") or "",
                        "path": c.get("path") or "/",
                    })
            return out
        except (ValueError, AttributeError):
            return []

    # Netscape cookies.txt: domain, flag, path, secure, expiry, name, value
    if "\t" in raw:
        out = []
        for line in raw.splitlines():
            if line.startswith("#") or not line.strip():
                continue
            parts = line.split("\t")
            if len(parts) >= 7:
                out.append({"domain": parts[0], "path": parts[2],
                            "name": parts[5], "value": parts[6]})
        if out:
            return out

    # Plain "a=1; b=2" header.
    out = []
    for chunk in raw.replace("\n", ";").split(";"):
        if "=" not in chunk:
            continue
        name, _, value = chunk.partition("=")
        name, value = name.strip(), value.strip()
        if name:
            out.append({"name": name, "value": value, "domain": "", "path": "/"})
    return out


# What each platform needs, and how healthy the approach is. Shown in the UI so
# the choice is informed rather than guessed.
PLATFORM_GUIDANCE = {
    "facebook": {
        "name": "Facebook",
        "cookie_names": ["c_user", "xs"],
        "domain": ".facebook.com",
        "reliability": "low",
        "note": ("Meta aggressively detects automation. Expect checkpoints. Paste "
                 "cookies from a throwaway account you logged into by hand; never "
                 "store the password, and never use a personal account."),
    },
    "instagram": {
        "name": "Instagram",
        "cookie_names": ["sessionid", "ds_user_id"],
        "domain": ".instagram.com",
        "reliability": "low",
        "note": ("Same detection as Facebook. Session cookies last days at best "
                 "before a re-login challenge."),
    },
    "x": {
        "name": "X (Twitter)",
        "cookie_names": ["auth_token", "ct0"],
        "domain": ".x.com",
        "reliability": "medium",
        "note": ("auth_token plus ct0 works for reading. The official API is far "
                 "more stable if you have a key."),
    },
    "tiktok": {
        "name": "TikTok",
        "cookie_names": ["sessionid"],
        "domain": ".tiktok.com",
        "reliability": "low",
        "note": "Heavy bot detection and frequent challenge pages.",
    },
    "linkedin": {
        "name": "LinkedIn",
        "cookie_names": ["li_at"],
        "domain": ".linkedin.com",
        "reliability": "low",
        "note": ("LinkedIn bans scraping firmly and li_at is tied to device "
                 "fingerprint. Least likely of any platform here to hold up."),
    },
    "reddit": {
        "name": "Reddit",
        "cookie_names": ["reddit_session"],
        "domain": ".reddit.com",
        "reliability": "high",
        "note": ("Prefer an OAuth app token: it is officially supported, stable, "
                 "and lifts the rate limits that block anonymous access."),
    },
    "generic": {
        "name": "Other site",
        "cookie_names": [],
        "domain": "",
        "reliability": "medium",
        "note": "Any site behind a login. Paste its session cookies.",
    },
}


def guidance(platform):
    return PLATFORM_GUIDANCE.get((platform or "").lower(),
                                 PLATFORM_GUIDANCE["generic"])


def health(credential, cookies=None):
    """Describe how usable a credential looks, before anything is fetched."""
    g = guidance(credential.platform)
    issues = []
    if credential.kind == "password":
        issues.append("Stored passwords trigger login flows, which is what gets "
                      "accounts locked. Session cookies are safer.")
    if credential.kind == "cookies" and cookies is not None:
        names = {c.get("name") for c in cookies}
        missing = [n for n in g["cookie_names"] if n not in names]
        if missing:
            issues.append("Missing expected cookie(s): %s. The session will "
                          "probably not authenticate." % ", ".join(missing))
    if credential.expires_at:
        try:
            if datetime.fromisoformat(credential.expires_at) < datetime.utcnow():
                issues.append("This credential is past its recorded expiry.")
        except ValueError:
            pass
    if credential.last_error:
        issues.append("Last use failed: %s" % credential.last_error)
    return {"reliability": g["reliability"], "note": g["note"], "issues": issues}
