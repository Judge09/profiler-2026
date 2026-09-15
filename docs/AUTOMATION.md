# Automation

Written for: whoever sets up scheduled collection, and whoever debugs it later.

The web app keeps its data in your browser, which is right while you are
sitting in front of it and useless for anything that has to run at 3am. The
scripts in `scripts/` are the other half: they run the same collectors and the
same scoring engine from a terminal, and write JSON the app imports without
translation.

They need no server, no browser and no database. A watch is read from a JSON
file; results are written to a JSON file.

---

## The two scripts

| Script | What it does |
|---|---|
| `scripts/collect.py` | Sweeps sources for a watch and scores what it finds. |
| `scripts/comments.py` | Reads and scores the comments under Facebook posts, and can draw the link map for them. |

Both share the same options for the watch, the output file, the date window
and pushing to a running app. Run either with `--help` for the full list.

---

## Step 1: a watch file

The scripts do not read the browser's storage, so the watch has to be on disk.
Two ways to get one:

**Export from the app.** On any page, use the backup export, and point
`--watch` at the downloaded file. If the backup holds more than one watch, name
the one you want with `--watch-name "BARMM Election Integrity"`.

**Or write one by hand.** Only the keywords matter; everything else has the
same defaults the app uses. `watches/example.json` is a working starting point:

```json
{
  "id": 1,
  "name": "BARMM Election Integrity",
  "subject": "NAMFREL",
  "kw_required": "BARMM",
  "kw_optional": "election\ncanvassing\nadvisory",
  "kw_excluded": "basketball",
  "kw_match_mode": "any",
  "threshold_review": 30,
  "threshold_high": 60
}
```

Keyword buckets and the ALL/ANY toggle behave exactly as they do in the app —
see [SIGNAL_MONITOR.md](SIGNAL_MONITOR.md). A watch with no `subject` and no
keywords is refused, because there would be nothing to search for.

---

## Step 2: a first run

See what can be collected from on this machine:

```bash
python scripts/collect.py --list-sources
```

Sources marked `!` need an optional library from `requirements-extra.txt`.

Collect the last week of news, writing a file you can import:

```bash
python scripts/collect.py --watch watches/example.json \
    --sources google_news,bing_news,reddit --days 7 \
    --out out/barmm.json
```

Progress goes to stderr and the JSON to stdout, so `--out -` pipes cleanly into
another tool. The summary at the end reads:

```
  48 collected, 31 new, 9 duplicate, 8 off-topic, 6 flagged
```

*Off-topic* posts failed the watch's own relevance rules and were dropped. Pass
`--loose` to keep them, which is worth doing once when tuning keywords.

---

## Step 3: Facebook posts and comments

Read a Page, including the comment thread under each post:

```bash
python scripts/collect.py --watch watches/example.json \
    --sources facebook --page NAMFREL --with-comments \
    --out out/barmm.json --append
```

Or go straight at comments, which is what `comments.py` is for:

```bash
# one post
python scripts/comments.py --watch watches/example.json \
    --post https://www.facebook.com/NAMFREL/posts/1234567890 \
    --out out/comments.json

# the threads under a Page's ten most recent posts, plus the link map
python scripts/comments.py --watch watches/example.json \
    --page NAMFREL --posts 10 \
    --linkmap out/comments-map.json \
    --out out/comments.json --append

# a list of URLs, one per line, '#' for comments
python scripts/comments.py --watch watches/example.json \
    --post-file urls.txt --out out/comments.json
```

`--min-score 40` keeps only replies that actually scored, while the counts
still report everything read. `--linkmap` writes a map file that the **Import
map** button on the Link Maps page loads.

What you can reach without a logged-in session is described in
[SIGNAL_MONITOR.md](SIGNAL_MONITOR.md#facebook). Public Pages usually work;
groups and private content do not.

---

## Step 4: getting results into the app

**Import the file.** On the watch page, *Import JSON*, and pick the output
file. This is the normal route and keeps everything in the browser.

**Or push to a running instance** with `--push-to`:

```bash
python scripts/collect.py --watch watches/example.json \
    --sources google_news --out out/barmm.json \
    --push-to http://127.0.0.1:5000 --password "$PROFILER_PASSWORD"
```

The password defaults to `$PROFILER_PASSWORD`, so it need not appear in the
command or in a crontab. Pushing writes to the *server's* copy, which only
persists if the app has a real database — see the serverless note below.

---

## Step 5: scheduling it

### cron (Linux, macOS)

```cron
# Every hour, news and the NAMFREL Page's posts and comments.
0 * * * * cd /srv/profiler && /usr/bin/python3 scripts/collect.py \
    --watch watches/example.json --sources google_news,facebook \
    --page NAMFREL --with-comments --days 1 \
    --out out/barmm.json --append --quiet >> logs/collect.log 2>&1
```

### Task Scheduler (Windows)

Create a Basic Task, action *Start a program*:

- **Program:** `python`
- **Arguments:** `scripts\collect.py --watch watches\example.json --sources google_news --days 1 --out out\barmm.json --append --quiet`
- **Start in:** the repository folder

### systemd timer

```ini
# /etc/systemd/system/profiler-collect.service
[Service]
Type=oneshot
WorkingDirectory=/srv/profiler
Environment=PROFILER_PASSWORD=...
ExecStart=/usr/bin/python3 scripts/collect.py --watch watches/example.json \
    --sources google_news --days 1 --out out/barmm.json --append --quiet
```

```ini
# /etc/systemd/system/profiler-collect.timer
[Timer]
OnCalendar=hourly
Persistent=true

[Install]
WantedBy=timers.target
```

---

## `--append` is what makes repetition safe

Without it, every run rewrites the output file, and an hourly job produces
twenty-four copies of the same morning.

With it, the script reads the dedupe keys already in the file and skips
anything it has seen. The keys are computed exactly as the app computes them,
so a post collected by a script and the same post collected in the browser
collide rather than duplicating.

Writes are atomic — the file is written alongside and moved into place — so a
run that is killed part-way never leaves a truncated file for the next one to
read its keys from.

---

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Ran. (Zero *new* posts is a normal outcome, not a failure.) |
| `2` | Refused to run: a bad watch file, an unknown source, a failed push. |
| `130` | Interrupted. |

A scheduler that alerts on non-zero will tell you about a broken watch file or
a rejected password, and stay quiet on a normal empty sweep.

---

## Serverless deployments

The scripts are excluded from the Vercel bundle — they run on a machine with a
scheduler, not inside a function that is only alive during a request. That is
deliberate.

If you want scheduled collection *and* a serverless deployment, run the scripts
anywhere with cron and `--push-to` your deployment. That only keeps anything if
`DATABASE_URL` points at a real database; without one, the serverless copy
lives in `/tmp` and is gone on the next cold start. See
[SIGNAL_MONITOR.md](SIGNAL_MONITOR.md#serverless-vercel).

---

## When something looks wrong

**"No new posts" every run.** Usually correct — `--append` is skipping what you
already have. Check the `duplicate` count in the summary; if it matches the
collected count, that is exactly what should happen.

**Everything is dropped as off-topic.** The watch's required keywords are not
in the results. Run once with `--loose` to see what is actually arriving, then
widen or move terms to the optional bucket.

**Facebook returns a login wall.** Expected for some Pages without a session.
Add one to the Vault in the app; the scripts themselves run unauthenticated, so
for walled Pages collect through the app instead.

**A comment sweep stops early.** It hit its time budget; the note says how many
threads went unread. Run it again, or raise `COLLECT_TIMEOUT`.
