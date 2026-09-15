#!/usr/bin/env python
"""Run a watch's collection from the command line.

This is the scheduled-job half of the Signal Monitor. It takes the same watch
definition the app uses, sweeps the sources named on the command line, scores
everything with the same engine, and writes JSON the app can import.

Examples
--------
Collect the last week from the news indexes, writing a file you can import:

    python scripts/collect.py --watch watches/barmm.json \\
        --sources google_news,bing_news,reddit --days 7 --out out/barmm.json

Read a Facebook Page, including the comments under each post:

    python scripts/collect.py --watch watches/barmm.json \\
        --sources facebook --page NAMFREL --with-comments \\
        --out out/barmm.json --append

Every hour, on any machine with cron (`crontab -e`):

    0 * * * * cd /srv/profiler && python scripts/collect.py \\
        --watch watches/barmm.json --sources google_news,facebook \\
        --page NAMFREL --days 1 --out out/barmm.json --append --quiet

`--append` is what makes a repeating job safe: posts already in the output file
are skipped, so the file grows with what is genuinely new instead of
accumulating copies of the same week.

Run `--list-sources` to see everything available, including which optional
libraries are installed on this machine.
"""

import argparse
import sys

import _common as C


def list_sources():
    """Print the source table, with availability resolved on this machine."""
    rows = C.collectors.source_meta()
    width = max(len(r["key"]) for r in rows)
    group = None
    for row in sorted(rows, key=lambda r: (r.get("group") or "", r["key"])):
        if row.get("group") != group:
            group = row.get("group")
            print("\n%s" % (group or "Other"))
        mark = " " if row.get("available", True) else "!"
        needs = ""
        if not row.get("available", True):
            needs = "  -- unavailable: %s" % (row.get("cap_reason") or "not installed")
        elif row.get("needs_url"):
            needs = "  -- needs --url"
        print(" %s %-*s  %s%s" % (mark, width, row["key"],
                                  (row.get("desc") or "")[:88], needs))
    print("\n! = an optional library is missing; see requirements-extra.txt")
    return 0


def main():
    parser = argparse.ArgumentParser(
        description="Collect and score posts for a watch, without a browser.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Run with --list-sources to see what can be collected from.")
    parser.add_argument("--list-sources", action="store_true",
                        help="Print every available source and exit.")
    parser.add_argument("--sources", default="google_news,bing_news",
                        help="Comma-separated source keys (default: "
                             "google_news,bing_news).")
    parser.add_argument("--query", default="",
                        help="Override the search built from the watch's keywords.")
    parser.add_argument("--url", default="",
                        help="Target URL, for sources that need one (rss, page, "
                             "article, fb_comments).")
    parser.add_argument("--page", default="",
                        help="Facebook Page name or URL, for the facebook source.")
    parser.add_argument("--with-comments", action="store_true",
                        help="For --sources facebook: also read the comments "
                             "under each post found.")
    parser.add_argument("--comment-limit", type=int, default=50, metavar="N",
                        help="Maximum comments per post (default 50).")

    # --list-sources is informational and should not demand a watch file, so
    # the shared required arguments are only added once it is ruled out.
    if "--list-sources" in sys.argv:
        parser.parse_known_args()
        return list_sources()

    C.add_common_args(parser)
    args = parser.parse_args()

    raw = C.load_watch(args.watch, args.watch_name)
    watch, spec, default_query = C.normalise_watch(raw)
    cfg = C.build_config(watch)

    # Quoting habits vary; strip stray quotes so --sources "'rss'" is not read
    # as a source literally named "'rss'".
    keys = [k.strip().strip("'\"") for k in args.sources.split(",")]
    keys = [k for k in keys if k]
    if not keys:
        raise C.Failure("Name at least one source with --sources, "
                        "e.g. --sources google_news,facebook")
    unknown = [k for k in keys if k not in C.collectors.COLLECTORS]
    if unknown:
        raise C.Failure("Unknown source(s): %s. Run --list-sources to see them all."
                        % ", ".join(unknown))

    query = args.query.strip() or default_query
    since = C.since_from_days(args.days)

    options = {"limit": max(1, args.limit)}
    if args.url:
        options["url"] = args.url
    if args.page:
        options["page"] = args.page
    if args.with_comments:
        options["with_comments"] = True
        options["comment_limit"] = max(1, args.comment_limit)

    C.log("Watch:   %s" % watch.get("name"), args.quiet)
    C.log("Query:   %s" % query, args.quiet)
    C.log("Sources: %s" % ", ".join(keys), args.quiet)
    if since:
        C.log("Since:   %s" % since.isoformat(timespec="seconds"), args.quiet)
    C.log("", args.quiet)

    raw_posts, report = C.collectors.collect(keys, query, options, since=since)
    for line in C.report_lines(report):
        C.log(line, args.quiet)

    known = C.load_known_keys(args.out) if args.append else set()
    posts, stats = C.score_posts(raw_posts, watch, cfg, known_keys=known,
                                 strict=not args.loose)
    C.finish(args, watch, posts, stats, report)
    return 0


if __name__ == "__main__":
    sys.exit(C.run(main))
