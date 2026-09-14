# Signal Monitor

Written for: analysts using the tool, and whoever maintains it next.

A **watch** is a saved monitoring case: what to look for, where to look, and
how to score what turns up. Posts are collected into it (live or by hand),
scored, triaged, and tied back to Profiles and the Link Mapper.

---

## Keywords: the part that decides what you actually collect

This is the single most important setting, and the easiest to get wrong. A
watch whose keywords are too broad fills with noise; one that is too narrow
finds nothing.

Keywords go in three buckets:

| Bucket | Meaning | Example |
|---|---|---|
| **Required** | Anchors the topic. A post must match to be kept. | `BARMM`, `NAMFREL` |
| **Optional** | Broadens coverage. **Never enough on its own.** | `election`, `poll` |
| **Excluded** | Rejects the post outright, whatever else it says. | `cricket`, `satire` |

The **ALL / ANY** toggle controls the required bucket:

- **ANY** — one required term is enough. Start here.
- **ALL** — every required term must appear. Use to narrow a noisy watch.

### Why optional terms can never anchor a watch

A word like *election* places a post in no particular topic — it matches
Manila and Missouri alike. Earlier versions treated every keyword equally,
which is how US and Indian election coverage ended up inside a BARMM watch.

Terms are checked against a list of generic words; if you put a purely generic
term in **Required**, it is moved to **Optional** and the sidebar tells you.
That is deliberate, not a bug. Multi-word phrases only count as generic when
every word is — `parliamentary elections` is generic, `BARMM elections` is not.

### Term syntax

| Form | Matches |
|---|---|
| `barmm` | The word, on word boundaries. `poll` will not match `pollution`. |
| `"media release"` | The exact phrase, with flexible whitespace. |
| `comelec\|election commission` | Any one of the alternatives. |
| `/barmm\s+parl\w*/` | A regular expression. |

Accents and curly quotes are folded before matching, so `COMELEC’s` still hits
`comelec`. Hashtags match the bare term, so `#Bangsamoro` hits `Bangsamoro`.

### Acronyms — the one thing worth knowing

Search engines expand acronyms; the relevance filter matches text. A watch
required on `BARMM` **sends a query for BARMM, gets back posts saying
"Bangsamoro", and then drops them as off-topic** — you pay to fetch results and
silently discard the on-topic ones.

Multi-word phrases and their initials are linked automatically (`Philippine
News Agency` also matches `PNA`), but the app cannot know that BARMM means
Bangsamoro. So when a required term is a bare acronym, the sidebar says so and
suggests the fix:

```
BARMM|Bangsamoro
```

Measured on a real run: 19 posts kept before, 22 after, from the same 24
fetched.

### What gets searched

The required and optional buckets build the query sent to live sources:

```
Required: BARMM, NAMFREL   Optional: election   Excluded: cricket   Mode: ANY
  →  (BARMM OR NAMFREL) AND (election) -cricket
```

The sidebar shows this query live as you type. Check it before collecting.

---

## Collecting

Sources are grouped by kind. A green dot fetches live; amber is login-walled
and opens a search for manual collection; a padlock means an optional library
is not installed.

**Only fetch** narrows at the source, so a "last 7 days" run does not download
and discard a year of archive.

**Strict mode** (on by default) drops posts that fail the relevance test rather
than storing them. The run report says how many were dropped.

### Sources that work with no setup

Google News, Bing News, all configured RSS feeds, Reddit, Hacker News,
Mastodon, Lemmy, Wikipedia, YouTube channel feeds, any RSS/Atom URL, and any
public web page.

### Facebook and X

Both are login-walled and block scripted requests, so neither can be scraped
directly. What they do instead:

1. **Collect what the search index already holds.** Google News honours a
   `site:` restriction and indexes a useful amount of X and Facebook content,
   including post text and timestamps. Results are filtered back to the
   platform's own domains, so an index that quietly drops the restriction
   cannot leak unrelated pages into a watch.
2. **Recover the author where it is knowable** — from the post URL, or an
   `@handle` in the text. Newsroom banners like `LOOK:` or `BARMM ELECTIONS |`
   are deliberately *not* treated as accounts: a fabricated author would
   silently corrupt the repeat-actor analysis.
3. **Offer targeted follow-up searches.** Nine per platform — latest, top,
   media-only, verified-only, hashtag, groups, pages, and date-restricted
   Google dorks — rather than one generic link.

A stored session in the **Vault** beats all of this, and an X API token beats
that. Both are used automatically when present.

Measured, so you know what to expect: Nitter mirrors now serve an anti-bot
challenge page, Bing's RSS silently ignores `site:`, and `mbasic.facebook.com`
returns 400 without a session. Those paths are still attempted, but they are
not where the results come from.

### Sources needing an optional library

Install with `pip install -r requirements-extra.txt`, or individually. The
capability report (the "see what is installed" link) shows what is usable and
what each missing piece needs.

| Source | Library | Also needs |
|---|---|---|
| Reddit (API) | `praw` | Reddit app client id + secret |
| Telegram channel | `telethon` | API id + hash (falls back to the public web preview without them) |
| X (API) | `tweepy` | A **paid** X API tier — there is no free search. Without it, X still collects from the index. |
| Instagram profile | `instaloader` | Nothing, but Instagram rate-limits hard |
| Video metadata | `yt-dlp` | Nothing |
| Article extractor | `trafilatura` | Nothing |
| Rendered page | `playwright` | `python -m playwright install chromium` |

Credentials belong in the **Vault**, not in environment variables.

### A note on snscrape

It is listed but commented out. It has been unmaintained since X closed its
public endpoints, and its X support is broken. An empty result from it means
*failure*, not an absence of posts — which is exactly the kind of silent
wrongness this tool tries to avoid.

---

## Scoring

Every post starts at a base score and accumulates signals: scam language,
payment requests, urgency, risky links, impersonation, doxxing, threats, and
any custom flags you define. The **evidence panel** shows the full ledger —
every rule that fired and what it added.

Thresholds (defaults: review at 30, high risk at 60) and every individual
weight are editable per watch. **Test scoring** lets you paste text and see the
ledger without saving anything.

Scores are stored on the post alongside a fingerprint of the rules that
produced them. Editing keywords, weights or thresholds changes that
fingerprint, and exactly the affected posts are rescored — nothing else.

### Modes

- **Media Release Threat** — compares posts against an official release.
  Detects impersonation, contradictions and compromised official accounts.
  Needs the reference text, official accounts and official domains to be set.
- **Digital Hunter** — correlates post authors against stored Profiles by
  codename, alias and social handle. Adds doxxing and threat-language rules.

---

## Link analysis, phishing and geolocation

Links inside posts are extracted, expanded and checked against the open
OpenPhish and URLhaus feeds, plus heuristics: raw-IP hosts, punycode,
throwaway TLDs, credential-seeking paths, userinfo tricks and deep subdomain
chains.

**Shortener expansion uses `HEAD` with capped redirects.** The destination is
revealed without ever loading the page body. Flagged links are rendered as
plain text and are deliberately not clickable.

The **geo panel** resolves the hosts behind flagged posts and groups them by
country, flagging datacenter and proxy origins — content claiming to be local
grassroots posting that resolves to a hosting provider is worth a look.

Locations describe **where the server sits, not where the author is**. A CDN
tells you about infrastructure, nothing more. Install `geoip2` and drop a
MaxMind `GeoLite2-City.mmdb` into `instance/` for offline lookups with no rate
limit and no third party seeing what you investigate.

---

## Link Mapper integration

**Link map** builds an entity graph, not a star:

```
subject ── account ── domain ── location
              └── profile
```

Two accounts pushing the same domain become two edges into one node, which is
how shared infrastructure becomes visible.

You can **merge into an existing map** — entities are matched on label and
type, so re-running never duplicates nodes.

The **network analysis** ranks accounts by betweenness centrality when
`networkx` is installed: who *bridges* otherwise separate clusters, which
usually describes coordinators better than who posted most.

---

## Where your data lives

**Everything you create is stored in your browser**, in IndexedDB — watches,
posts, profiles, intel notes, photos, link maps, dork history and username
searches. None of it leaves the machine unless you export or sync it.

The server keeps two things only:

- the built-in dork templates, which are reference data shipped with the app
- **credential vault secrets**, which are deliberately server-side and
  encrypted at rest, so session cookies and tokens never reach JavaScript

Everything else the server does is stateless: it fetches from external sources
and runs the scoring engine, then forgets. That is why the scoring rules are
not duplicated in JavaScript — posts go up to be scored and verdicts come back
to be stored locally.

### What this means in practice

| | |
|---|---|
| **Per browser profile** | Data is tied to this browser on this machine. Another browser, another device, or a guest profile sees nothing. |
| **Clearing site data deletes it** | "Clear browsing data" removes everything. There is no server copy unless you made one. |
| **Private windows** | Usually discard the data when the window closes. |
| **Eviction** | Browsers may clear storage under disk pressure. Click **Protect** on the storage bar to request persistence — Chrome usually grants it silently, Firefox prompts. |

The storage bar on the Signal Monitor page shows usage, whether persistence is
granted, and offers **Export backup** / **Restore**. Typical capacity is
hundreds of MB to a few GB; posts run about 1.8 KB each, so ten thousand posts
is roughly 18 MB. Photos are stored as blobs and count against the same quota.

### Backups

- **Export backup** downloads one JSON file containing every store. Do this
  regularly — it is the only copy that survives clearing site data.
- **Restore** replaces the browser's contents from such a file. Backups taken
  before profiles moved into the browser still import cleanly.

### Optional server sync

The SQLite database doubles as a sync target, so you can move between machines
or keep an off-browser copy. Nothing syncs automatically.

- `POST /monitor/api/sync/push` — replace the server copy with this browser's data
- `GET /monitor/api/sync/pull` — fetch the server copy back

---

## Deployment

```bash
pip install -r requirements.txt
python run.py
```

### Before exposing it to a network

The app prints a warning at startup if either of these is unset:

```bash
export PROFILER_PASSWORD="something-long-and-unguessable"
export SECRET_KEY="$(python -c 'import secrets;print(secrets.token_hex(32))')"
```

The default password is in the source. Anyone who finds the URL is one guess
from the credential vault, so change it.

Session cookies are `HttpOnly` and `SameSite=Lax` always, and `Secure` is set
automatically on serverless hosts or when `FORCE_HTTPS=1`.

### Serverless (Vercel)

`vercel.json` and `api/index.py` are included and the deployment is
straightforward now that the browser owns the data: an ephemeral `/tmp` costs
nothing, because the only things stored there are regenerated at startup.

Two caveats:

- **Vault secrets do not survive a cold start** on serverless, since they need
  a persistent disk. Set `DATABASE_URL` to an external database if you rely on
  stored credentials.
- Browser-driven sources (Playwright, Selenium) cannot run there. The
  capability report says so rather than failing silently.

### Configuration

Environment-overridable: `SECRET_KEY`, `PROFILER_PASSWORD`, `DATABASE_URL`,
`MONITOR_VAULT_KEY`, `FORCE_HTTPS`, `PAGE_SIZE`, `PORT`, `GEOIP_DB`.

---

## Tests

```bash
python tests/test_monitor.py            # engine, keywords, netintel, graph merge
python tests/test_auth.py               # vault, cookie parsing, challenge detection
python tests/test_routes.py             # auth gate, vault routes, malformed input
python tests/test_api.py                # the stateless API (reaches the network)
python tests/test_browser.py            # Chromium + IndexedDB: monitor core
python tests/test_browser_profiles.py   # Chromium + IndexedDB: profiles, dorks, OSINT
```

The two browser suites need Playwright:

```bash
pip install playwright && python -m playwright install chromium
```
