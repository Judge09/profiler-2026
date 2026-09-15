"""Shared plumbing for the command-line collectors.

The web app keeps its data in the browser, which is fine while someone is
sitting in front of it and useless for anything scheduled. These scripts are
the other half: they run the same collectors and the same scoring engine from a
terminal or a cron job, and write JSON that the app imports without
translation.

Nothing here talks to the app's database. A watch is read from a JSON file, and
results are written to a JSON file, so a scheduled run needs no server, no
session and no network beyond the sources it is collecting from.

The output format is the one `Import JSON` on a watch page already accepts, and
also what `--push` sends to `/monitor/api/sync/push`.
"""

import argparse
import json
import os
import sys
from datetime import datetime, timedelta

# The project root must be importable before `app` and `config` resolve. These
# scripts are run directly ("python scripts/collect.py"), so Python puts
# `scripts/` on the path, not the repository root.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app.monitor import collectors, engine  # noqa: E402
from app.monitor.keywords import rules_hash, spec_for  # noqa: E402


class Failure(Exception):
    """A problem worth reporting plainly and exiting on."""


def log(message, quiet=False):
    """Progress goes to stderr so stdout stays pure JSON when piped."""
    if not quiet:
        print(message, file=sys.stderr, flush=True)


# -- Watch definitions -------------------------------------------------------

# Every field the engine reads, with the same defaults the browser uses when it
# creates a watch. Listing them here means a hand-written watch file can be two
# lines long and still score identically to one built in the UI.
WATCH_DEFAULTS = {
    "id": 0,
    "name": "Untitled watch",
    "subject": "",
    "keywords": "",
    "kw_required": "",
    "kw_optional": "",
    "kw_excluded": "",
    "kw_match_mode": "any",
    "mode_release": False,
    "mode_hunter": False,
    "reference_text": "",
    "official_accounts": "",
    "official_domains": "",
    "custom_flags": "",
    "weights": {},
    "threshold_review": 30,
    "threshold_high": 60,
    "profile_id": None,
    "profiles": [],
}


def load_watch(path, name=None):
    """Read a watch definition from JSON.

    Accepts three shapes, because all three are things people actually have to
    hand: a bare watch object, a `{"watch": {...}}` wrapper, and a full
    Profiler backup exported from the app. From a backup the watch named by
    `name` is taken, or the only one when there is exactly one -- picking
    arbitrarily from several would silently monitor the wrong thing.
    """
    if not os.path.exists(path):
        raise Failure("No such file: %s" % path)
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except ValueError as e:
        raise Failure("%s is not valid JSON: %s" % (path, e))
    except OSError as e:
        raise Failure("Could not read %s: %s" % (path, e))

    if not isinstance(data, dict):
        raise Failure("A watch file must contain a JSON object.")

    if data.get("format") == "profiler-backup":
        store = data.get("data") or {}
        watches = store.get("watches") or []
        if not watches:
            raise Failure("That backup contains no watches.")
        if name:
            wanted = str(name).strip().lower()
            found = [w for w in watches
                     if str(w.get("name", "")).strip().lower() == wanted]
            if not found:
                raise Failure(
                    "No watch named %r in that backup. It has: %s"
                    % (name, ", ".join(repr(w.get("name")) for w in watches)))
            chosen = found[0]
        elif len(watches) == 1:
            chosen = watches[0]
        else:
            raise Failure(
                "That backup holds %d watches; name one with --watch-name. "
                "It has: %s" % (len(watches),
                                ", ".join(repr(w.get("name")) for w in watches)))
        chosen = dict(chosen)
        # Hunter mode correlates against profiles, which the backup carries.
        if store.get("profiles"):
            chosen.setdefault("profiles", store["profiles"])
        return chosen

    if isinstance(data.get("watch"), dict):
        return data["watch"]
    return data


def normalise_watch(raw):
    """Fill in defaults and validate enough to fail early with a clear reason."""
    watch = dict(WATCH_DEFAULTS)
    profiles = raw.pop("_profiles", None)
    watch.update({k: v for k, v in (raw or {}).items() if v is not None})
    if profiles and not watch.get("profiles"):
        watch["profiles"] = profiles

    spec = spec_for(watch)
    query = spec.build_query() or (watch.get("subject") or "").strip()
    if not query:
        raise Failure(
            "This watch has no keywords and no subject, so there is nothing to "
            "search for. Add `subject`, `kw_required` or `keywords` to it.")
    return watch, spec, query


def build_config(watch):
    """The engine config for a watch dict, including Hunter-mode profiles."""
    class _Link:
        def __init__(self, d):
            self.username = d.get("username")
            self.platform = d.get("platform")

    class _Profile:
        def __init__(self, d):
            self.id = d.get("id")
            self.codename = d.get("codename") or ""
            self.real_name = d.get("real_name") or ""
            self.known_aliases = d.get("known_aliases") or []
            self.social_links = [_Link(s) for s in (d.get("social_links") or [])]

    objs = ([_Profile(p) for p in (watch.get("profiles") or [])]
            if watch.get("mode_hunter") else [])
    return engine.build_config(watch, objs)


# -- Post assembly -----------------------------------------------------------

def dedupe_key(watch_id, platform, author, text, url=""):
    """The same key the server and the browser compute, so nothing re-imports.

    This must stay byte-identical to `routes._dedupe_key`; a post collected by
    a script and one collected in the UI have to collide, or every scheduled
    run would silently duplicate its own previous results.
    """
    import hashlib
    raw = "|".join([str(watch_id), (platform or "").lower(),
                    (author or "").lower(), (url or "").lower(),
                    (text or "").strip().lower()[:400]])
    return hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()


def score_posts(raw_posts, watch, cfg, known_keys=(), strict=True):
    """Turn collector output into scored, deduplicated post records.

    Returns (kept, stats). The records carry every field the browser store
    expects, so importing them needs no further work.
    """
    known = set(known_keys or ())
    watch_id = watch.get("id") or 0
    now = datetime.utcnow().isoformat(timespec="seconds")
    digest = rules_hash(watch, cfg.get("spec"))

    kept = []
    stats = {"seen": len(raw_posts), "added": 0, "duplicate": 0,
             "off_topic": 0, "flagged": 0, "empty": 0, "comments": 0}

    for raw in raw_posts:
        author = str(raw.get("author") or "").strip()
        text = str(raw.get("text") or "").strip()
        if not author or not text:
            stats["empty"] += 1
            continue

        platform = str(raw.get("platform") or "Other").strip()
        url = str(raw.get("url") or "").strip()
        key = dedupe_key(watch_id, platform, author, text, url)
        if key in known:
            stats["duplicate"] += 1
            continue
        known.add(key)

        post = {
            "watch_id": watch_id,
            "platform": platform,
            "author": author[:200],
            "handle": str(raw.get("handle") or "").lstrip("@")[:120],
            "verified": bool(raw.get("verified")),
            "text": text,
            "url": url,
            "source": raw.get("source") or "script",
            "source_url": str(raw.get("source_url") or "")[:500],
            "link_kind": raw.get("link_kind") or "direct",
            "posted_at": raw.get("posted_at"),
            "posted_ts": raw.get("posted_at") or now,
            "collected_at": now,
            "dedupe_key": key,
            "status": "new",
            "pinned": False,
            "manual_score": None,
            "manual_verdict": None,
            "analyst_note": "",
            "profile_id": None,
            "kind": raw.get("kind") or "post",
            "parent_url": str(raw.get("parent_url") or "")[:500],
            "parent_author": str(raw.get("parent_author") or "")[:200],
            "engagement": raw.get("engagement") or {},
        }

        analysis = engine.analyze(post, cfg)
        if strict and not analysis["relevant"]:
            stats["off_topic"] += 1
            continue

        post.update({
            "score": analysis["score"],
            "verdict": analysis["verdict"],
            "verdict_label": analysis["verdict_label"],
            "types": analysis["types"],
            "relevant": analysis["relevant"],
            "relevance": analysis.get("relevance", 0),
            "analysis": analysis,
            "rules_hash": digest,
        })
        if analysis["verdict"] != "ok":
            stats["flagged"] += 1
        if post["kind"] == "comment":
            stats["comments"] += 1
        kept.append(post)

    stats["added"] = len(kept)
    return kept, stats


# -- Output ------------------------------------------------------------------

def load_known_keys(path):
    """Dedupe keys from a previous run's output file, when one exists.

    This is what makes a cron job idempotent: without it, every run re-collects
    and re-emits the same posts, and the analyst imports fifty copies of the
    same week.
    """
    if not path or not os.path.exists(path):
        return set()
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (ValueError, OSError):
        return set()
    posts = data.get("posts") if isinstance(data, dict) else data
    if not isinstance(posts, list):
        return set()
    return {p.get("dedupe_key") for p in posts if isinstance(p, dict)
            and p.get("dedupe_key")}


def write_output(path, payload, append_known=None, quiet=False):
    """Write the run's result, merging with a previous file when appending."""
    if append_known:
        previous = []
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    old = json.load(f)
                previous = (old.get("posts") if isinstance(old, dict) else old) or []
            except (ValueError, OSError):
                previous = []
        payload["posts"] = previous + payload["posts"]
        payload["total"] = len(payload["posts"])

    if path == "-":
        json.dump(payload, sys.stdout, indent=2, default=str)
        sys.stdout.write("\n")
        return

    directory = os.path.dirname(os.path.abspath(path))
    try:
        os.makedirs(directory, exist_ok=True)
        # Write to a temporary file and move it into place, so a crashed or
        # killed run never leaves a truncated file where the next one will read
        # its dedupe keys from.
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, default=str)
        os.replace(tmp, path)
    except OSError as e:
        raise Failure("Could not write %s: %s" % (path, e))
    log("Wrote %d post(s) to %s" % (len(payload["posts"]), path), quiet)


def push_to_server(payload, base_url, password, quiet=False):
    """Send results to a running Profiler instance.

    Optional, and deliberately kept simple: it logs in with the app password
    and posts to the sync endpoint the browser already uses.
    """
    import requests

    base = base_url.rstrip("/")
    session = requests.Session()
    try:
        # The auth blueprint has no URL prefix, so the endpoint is /login.
        resp = session.post(base + "/login", data={"password": password},
                            timeout=20, allow_redirects=True)
        if resp.status_code >= 400:
            raise Failure("Login failed (%d). Check --password." % resp.status_code)
        # A wrong password re-renders the login form with 200 rather than
        # redirecting, so the status code alone does not prove we are in.
        if "Invalid password" in resp.text:
            raise Failure("The password was rejected. Check --password.")

        backup = {
            "format": "profiler-backup",
            "version": 3,
            "exported_at": datetime.utcnow().isoformat(timespec="seconds"),
            "data": {"posts": payload["posts"], "watches": [payload["watch"]]},
        }
        resp = session.post(base + "/monitor/api/sync/push",
                            json={"payload": backup, "replace": False},
                            timeout=60)
        if resp.status_code >= 400:
            raise Failure("Push failed (%d): %s" % (resp.status_code, resp.text[:200]))
    except requests.exceptions.RequestException as e:
        raise Failure("Could not reach %s: %s" % (base, type(e).__name__))
    log("Pushed %d post(s) to %s" % (len(payload["posts"]), base), quiet)
    return resp.json() if resp.content else {}


def since_from_days(days):
    """A naive UTC cut-off from a day count, or None for 'everything'."""
    if not days:
        return None
    try:
        n = int(days)
    except (TypeError, ValueError):
        raise Failure("--days takes a whole number of days.")
    if n <= 0:
        return None
    return datetime.utcnow() - timedelta(days=min(n, 3650))


def report_lines(report):
    """Per-source outcome, formatted for a terminal."""
    lines = []
    for row in report.get("sources", []):
        mark = "ok  " if row["ok"] else ("blk " if row.get("blocked") else "fail")
        lines.append("  [%s] %-14s %3d post(s) %5.1fs  %s"
                     % (mark, row["source"], row["count"], row.get("elapsed", 0),
                        (row.get("note") or "")[:110]))
    return lines


def add_common_args(parser):
    """Arguments every collector script shares."""
    parser.add_argument("--watch", required=True, metavar="FILE",
                        help="JSON file holding the watch definition, or a "
                             "Profiler backup exported from the app.")
    parser.add_argument("--watch-name", metavar="NAME",
                        help="Which watch to use, when --watch is a backup "
                             "holding more than one.")
    parser.add_argument("--out", default="-", metavar="FILE",
                        help="Where to write results; '-' for stdout (default).")
    parser.add_argument("--append", action="store_true",
                        help="Merge into --out instead of replacing it, skipping "
                             "posts already in that file.")
    parser.add_argument("--days", type=int, default=0, metavar="N",
                        help="Only collect posts from the last N days.")
    parser.add_argument("--limit", type=int, default=25, metavar="N",
                        help="Maximum posts per source (default 25).")
    parser.add_argument("--loose", action="store_true",
                        help="Keep posts that fail the watch's relevance rules.")
    parser.add_argument("--push-to", metavar="URL",
                        help="Also push results to a running Profiler, "
                             "e.g. http://127.0.0.1:5000")
    parser.add_argument("--password", default=os.environ.get("PROFILER_PASSWORD", ""),
                        help="Password for --push-to. Defaults to $PROFILER_PASSWORD.")
    parser.add_argument("--quiet", action="store_true",
                        help="Suppress progress output on stderr.")
    return parser


def finish(args, watch, posts, stats, report, extra=None):
    """Assemble the payload, write it, optionally push, and summarise."""
    payload = {
        "format": "profiler-collection",
        "generated_at": datetime.utcnow().isoformat(timespec="seconds"),
        "watch": watch,
        "posts": posts,
        "total": len(posts),
        "stats": stats,
        "report": report,
    }
    if extra:
        payload.update(extra)

    write_output(args.out, payload, append_known=args.append, quiet=args.quiet)

    if args.push_to:
        if not args.password:
            raise Failure("--push-to needs --password (or $PROFILER_PASSWORD).")
        push_to_server(payload, args.push_to, args.password, args.quiet)

    log("", args.quiet)
    log("  %d collected, %d new, %d duplicate, %d off-topic, %d flagged"
        % (stats["seen"], stats["added"], stats["duplicate"],
           stats["off_topic"], stats["flagged"]), args.quiet)
    return payload


def run(main_fn):
    """Entrypoint wrapper: turn a Failure into a clean message and exit code."""
    try:
        return main_fn() or 0
    except Failure as e:
        print("error: %s" % e, file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
