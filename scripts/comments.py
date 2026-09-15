#!/usr/bin/env python
"""Collect and score the comments under Facebook posts.

Comments are usually where a coordinated push is most visible. The post itself
is often bland and deniable while the replies carry the scam link, the threat,
or the same talking point repeated by a dozen accounts registered last week.
This script reads those threads, scores every comment with the watch's own
rules, and writes the result as posts the app imports directly.

It also builds the link map for what it found (`--linkmap`), which is the point
of connecting comments to the graph: a brigade shows up as many commenter nodes
converging on one post, and that shape is not visible in a list.

Examples
--------
One post's thread:

    python scripts/comments.py --watch watches/barmm.json \\
        --post https://www.facebook.com/NAMFREL/posts/1234567890 \\
        --out out/comments.json

Every post on a Page, with their threads, plus the link map:

    python scripts/comments.py --watch watches/barmm.json \\
        --page NAMFREL --posts 10 --linkmap out/comments-map.json \\
        --out out/comments.json --append

A list of post URLs, one per line:

    python scripts/comments.py --watch watches/barmm.json \\
        --post-file urls.txt --out out/comments.json

Only the flagged replies are worth a human's time, so `--min-score` filters the
output to what actually scored, while the counts still report everything read.
"""

import argparse
import json
import os
import sys
from datetime import datetime

# `_common` puts the repository root on sys.path as a side effect of being
# imported, so it must come before anything reaching into `app`.
import _common as C  # noqa: E402

from app.monitor import facebook as fb  # noqa: E402
from app.monitor import graphbuild  # noqa: E402


def read_post_urls(args):
    """Every post URL to work through, from whichever way it was supplied."""
    urls = []
    if args.post:
        urls.extend(args.post)
    if args.post_file:
        if not os.path.exists(args.post_file):
            raise C.Failure("No such file: %s" % args.post_file)
        try:
            with open(args.post_file, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        urls.append(line)
        except OSError as e:
            raise C.Failure("Could not read %s: %s" % (args.post_file, e))
    # Preserve order while dropping repeats, so a list with duplicates does not
    # fetch the same thread twice.
    seen, out = set(), []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def posts_from_page(page, limit, session, quiet=False):
    """Find recent posts on a Page, to then read each one's comments."""
    C.log("Reading the %s Page for recent posts…" % page, quiet)
    result = fb.collect_page(page, limit=limit, session=session)
    if not result["ok"]:
        raise C.Failure(result["note"])
    found = [p for p in result["posts"]
             if p.get("url") and p.get("link_kind") == "direct"]
    C.log("  found %d post(s) with a usable permalink" % len(found), quiet)
    if not found:
        raise C.Failure(
            "None of the posts on that Page had a permalink to read comments "
            "from. Facebook sometimes withholds them without a session -- try "
            "adding one to the vault, or pass --post with a URL directly.")
    return found


def main():
    parser = argparse.ArgumentParser(
        description="Read and score the comments under Facebook posts.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--post", action="append", metavar="URL",
                        help="A Facebook post URL. Repeat for several.")
    parser.add_argument("--post-file", metavar="FILE",
                        help="File of post URLs, one per line.")
    parser.add_argument("--page", metavar="NAME",
                        help="Read a Page's recent posts and take the comments "
                             "under each.")
    parser.add_argument("--posts", type=int, default=5, metavar="N",
                        help="With --page, how many posts to read comments from "
                             "(default 5).")
    parser.add_argument("--comment-limit", type=int, default=100, metavar="N",
                        help="Maximum comments per post (default 100).")
    parser.add_argument("--pages", type=int, default=5, metavar="N",
                        help="How many 'View more comments' pages to follow "
                             "(default 5).")
    parser.add_argument("--min-score", type=int, default=0, metavar="N",
                        help="Only keep comments scoring at least N.")
    parser.add_argument("--linkmap", metavar="FILE",
                        help="Also write a link map of what was found, ready to "
                             "import on the Link Maps page.")
    parser.add_argument("--linkmap-min-score", type=int, default=30, metavar="N",
                        help="Minimum score for a comment to be drawn on the "
                             "link map (default 30).")
    C.add_common_args(parser)
    args = parser.parse_args()

    if not (args.post or args.post_file or args.page):
        raise C.Failure("Give --post, --post-file or --page to say which "
                        "comments to read.")

    raw = C.load_watch(args.watch, args.watch_name)
    watch, _spec, _query = C.normalise_watch(raw)
    cfg = C.build_config(watch)

    # A vaulted session needs the app's database, which a bare script has no
    # reason to require. Without one this still works on public Pages.
    session = None
    C.log("Watch: %s" % watch.get("name"), args.quiet)

    targets = []
    if args.page:
        targets.extend(posts_from_page(args.page, max(1, args.posts), session,
                                       args.quiet))
    for url in read_post_urls(args):
        targets.append({"url": url, "author": "", "text": ""})

    since = C.since_from_days(args.days)
    known = C.load_known_keys(args.out) if args.append else set()

    all_raw, sources = [], []
    for target in targets:
        url = target["url"]
        C.log("  reading comments: %s" % url[:96], args.quiet)
        result = fb.collect_comments(
            url, limit=max(1, args.comment_limit), session=session,
            parent_author=target.get("author") or "",
            max_pages=max(1, args.pages))
        got = result["posts"] if result["ok"] else []
        if since and got:
            got = fb._apply_since(got, since)
        all_raw.extend(got)
        sources.append({"source": "fb_comments", "ok": result["ok"],
                        "count": len(got), "note": result["note"],
                        "blocked": result.get("blocked", False),
                        "manual_url": result.get("manual_url", ""),
                        "elapsed": 0, "url": url})
        C.log("    %s" % result["note"], args.quiet)

    posts, stats = C.score_posts(all_raw, watch, cfg, known_keys=known,
                                 strict=not args.loose)

    if args.min_score > 0:
        before = len(posts)
        posts = [p for p in posts if p["score"] >= args.min_score]
        stats["below_min_score"] = before - len(posts)
        stats["added"] = len(posts)

    report = {"sources": sources, "total": len(all_raw),
              "ok_sources": sum(1 for s in sources if s["ok"]),
              "failed_sources": sum(1 for s in sources if not s["ok"]),
              "elapsed": 0}

    payload = C.finish(args, watch, posts, stats, report,
                       extra={"threads": len(targets)})

    if args.linkmap:
        write_linkmap(args, watch, posts)
    return 0


def write_linkmap(args, watch, posts):
    """Build the graph for these comments and write it as an importable map.

    The file is a single map in the shape the Link Maps page stores, so it
    imports with the "Import map" button and opens in the editor unchanged.
    """
    graph, stats = graphbuild.build_from_records(
        watch, posts, min_score=args.linkmap_min_score,
        include_domains=True, include_profiles=True,
        verdicts=("bad", "warn"),
        profiles=watch.get("profiles") or [])

    payload = {
        "format": "profiler-linkmap",
        "title": "%s - comments" % (watch.get("name") or "Watch"),
        "profile_id": watch.get("profile_id"),
        "graph_json": json.dumps(graph),
        "stats": stats,
        "created_at": datetime.utcnow().isoformat(timespec="seconds"),
        "updated_at": datetime.utcnow().isoformat(timespec="seconds"),
    }
    try:
        directory = os.path.dirname(os.path.abspath(args.linkmap))
        os.makedirs(directory, exist_ok=True)
        with open(args.linkmap, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, default=str)
    except OSError as e:
        raise C.Failure("Could not write %s: %s" % (args.linkmap, e))

    C.log("Link map: %d node(s), %d edge(s) from %d comment(s) -> %s"
          % (stats["nodes"], stats["edges"], stats.get("comments", 0),
             args.linkmap), args.quiet)


if __name__ == "__main__":
    sys.exit(C.run(main))
