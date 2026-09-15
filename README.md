# profiler-2026

An OSINT and SOCMINT workbench: build profiles, monitor what is said about a
subject, score it for scams, impersonation and threats, and map how the
accounts behind it connect.

**Your data stays in your browser.** Watches, posts, profiles and link maps
live in IndexedDB on your machine; the server fetches from public sources and
runs the scoring engine, but stores nothing of yours. See
[Where your data lives](docs/SIGNAL_MONITOR.md#where-your-data-lives) — the
tradeoffs are real and worth reading before you rely on it.

---

## Running it

```bash
pip install -r requirements.txt
python run.py
```

Then open <http://127.0.0.1:5000>. The default password is `profiler2024` —
**change it before putting this anywhere reachable:**

```bash
export PROFILER_PASSWORD="something long"
export SECRET_KEY="something else long"
```

Optional extras (browser automation, heavier analysis, extra API clients) live
in `requirements-extra.txt`. Nothing breaks without them; the app's capability
report says what is installed and what each missing piece would add.

---

## What is in it

| Area | What it does |
|---|---|
| **Profiles** | Subjects, aliases, accounts, photos, notes and a radar chart. |
| **Signal Monitor** | Collect posts and comments from live sources, score and triage them. |
| **Link Mapper** | Entity graph of accounts, domains, locations and profiles. |
| **Dork builder** | Search templates for finding what is not indexed conveniently. |
| **Identity OSINT** | A username across 121 platforms, breach history for any domain, password exposure checks, and pivot links into the tools this app cannot automate. |
| **Vault** | Encrypted storage for the sessions and API keys some sources need. |

---

## Documentation

- **[Signal Monitor](docs/SIGNAL_MONITOR.md)** — keywords, sources, scoring,
  Facebook and comment collection, the link map, and where data lives.
- **[Automation](docs/AUTOMATION.md)** — running collection from the command
  line and on a schedule.

---

## Scheduled collection

The browser is the system of record, which is right while you are using the app
and useless for anything that runs overnight. `scripts/` is the other half:

```bash
# see what can be collected from
python scripts/collect.py --list-sources

# sweep the news indexes for a watch
python scripts/collect.py --watch watches/example.json \
    --sources google_news,bing_news --days 7 --out out/barmm.json

# read a Facebook Page's posts and the comments under them
python scripts/comments.py --watch watches/example.json \
    --page NAMFREL --posts 10 --linkmap out/map.json \
    --out out/comments.json --append
```

The scripts use the same collectors, the same scoring engine and the same
dedupe keys as the app, so results import cleanly and repeated runs do not
duplicate anything. Full details in [docs/AUTOMATION.md](docs/AUTOMATION.md).

---

## Tests

```bash
python -m pytest tests/test_monitor.py tests/test_facebook.py tests/test_osint.py -v
python -m pytest tests/test_security.py -v
python tests/test_api.py
python tests/test_auth.py
python tests/test_routes.py
```

Most tests are offline. `tests/test_security.py` exercises the real login
throttle so it takes about a minute, and `tests/test_browser_osint.py` reads
the live breach catalogue, so that one needs internet.

---

## Deploying

Runs anywhere Flask runs. For Vercel, `api/index.py` is the entrypoint and
`vercel.json` is already configured; read
[the serverless notes](docs/SIGNAL_MONITOR.md#serverless-vercel) first, because
a function has no durable disk and the credential vault needs one.
