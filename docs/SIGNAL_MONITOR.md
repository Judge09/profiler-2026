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

### Facebook

**A session in the vault is now required.** Facebook answers HTTP 400 to every
logged-out request — Pages, permalinks and comment threads alike. It no longer
serves public content anonymously at all, so without a stored session this
source returns nothing and says so.

To set one up: **Monitor → Vault**, unlock it, and paste the cookies from a
browser you are logged into. Collection then reads `mbasic.facebook.com`, the
no-JavaScript interface, which is the only Facebook surface that renders whole
posts, permalinks, timestamps and comment threads as plain server-side HTML.

**Name a Page** in the collection panel (`NAMFREL`, or paste its URL) and you
get, per post:

- the full post text, not a search-result snippet
- the permalink, with tracking parameters stripped so the same post dedupes
  across runs instead of looking new every time
- the timestamp, including relative forms like `2 hrs` and `Yesterday`
- reaction, comment and share counts

What to expect, measured rather than assumed:

| Target | Logged out | With a vault session |
|---|---|---|
| Public Page timeline | **no** — HTTP 400 | yes |
| A single public post | **no** — HTTP 400 | yes, when the post is public |
| Comments on a public post | **no** — HTTP 400 | yes |
| Groups, private pages, profiles | no | only what that account can see |
| Search | no | no — mbasic search is login-walled |

Without a Page name there is nothing to fetch directly, so the collector falls
back to the search index and the dork links, which still work logged out.

Sessions do not last. Cookies expire in days, and a scripted session on a
throwaway account is often challenged sooner. When that happens the run says
the session was rejected rather than reporting an empty result — re-paste fresh
cookies.

### Comments

Two ways in:

- **Facebook Page** source, with *"Also read the comments under each post"*
  ticked — reads the Page's recent posts and every thread under them.
- **Facebook comments** source, with one post's permalink in the URL field.
  Open the post on Facebook and copy the address bar; a Page URL will not do,
  it has to point at a single post.

Both need a vault session, for the reason above. If a run comes back empty,
read the note beside the source in the collection report: it distinguishes
"refused, no session" from "no comments on that post" from "everything fell
outside your date window", which are three quite different problems.

Comments matter because they are usually where a coordinated push is most
visible: the post is bland and deniable while the replies carry the scam link,
the threat, or one talking point repeated by a dozen new accounts. The
collector pages through *View more comments* rather than taking only the first
handful, since the interesting replies are rarely at the top.

Comments are stored as posts with a parent, so they inherit the whole
pipeline — the same scoring, filters, triage, export and link map. They are
marked with a **comment** tag naming the thread they sit under, and the
toolbar's **Posts and comments** menu narrows the list to one or the other.

One deliberate difference in scoring: **a comment inherits its parent post's
relevance.** A reply saying *"this is fake, don't share"* names nothing the
keyword rules look for, yet it is exactly the reply worth reading — judging it
on its own words alone would discard it. An **excluded** term still rejects a
comment, because that is an explicit "never this".

On a serverless host the comment sweep runs under a wall-clock budget (about
60% of `COLLECT_TIMEOUT`). When it runs out, the run returns what it gathered
and says how many threads went unread, rather than losing everything to a
gateway timeout. Collect again to continue, or raise `COLLECT_TIMEOUT`.

**When nothing comes back**, the note says which of these it was:

| Note | What it means |
|---|---|
| *Facebook refused the request (HTTP 400)…* | No usable session. Add or refresh one in the vault. |
| *…the stored session was rejected* | The cookies expired or the account hit a checkpoint. Re-paste them. |
| *That post loaded but no comments were readable* | Genuinely none, comments are limited, or the layout changed. |
| *…all of them fall outside the date window* | They were read and then filtered out. Widen the window. |
| *Facebook says that post does not exist* | Deleted, or the URL points at a private post. |

### X (Twitter)

X is login-walled and blocks scripted requests, so it cannot be read directly.
Instead:

1. **Collect what the search index already holds.** Google News honours a
   `site:` restriction and indexes a useful amount of X content, including post
   text and timestamps. Results are filtered back to the platform's own
   domains, so an index that quietly drops the restriction cannot leak
   unrelated pages into a watch.
2. **Recover the author where it is knowable** — from the post URL, or an
   `@handle` in the text. Newsroom banners like `LOOK:` or `BARMM ELECTIONS |`
   are deliberately *not* treated as accounts: a fabricated author would
   silently corrupt the repeat-actor analysis.
3. **Offer targeted follow-up searches.** Nine per platform — latest, top,
   media-only, verified-only, hashtag, groups, pages, and date-restricted
   Google dorks — rather than one generic link.

A stored session in the **Vault** beats all of this, and an X API token beats
that. Both are used automatically when present. Nitter mirrors now serve an
anti-bot challenge page and Bing's RSS silently ignores `site:`; both are still
attempted, but they are not where results come from.

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

### Threats are flagged in every mode

Threatening language is scored whether or not Digital Hunter is on. A comment
reading *"we know where the canvassers live"* is worth surfacing in any watch,
and it used to be invisible unless someone had remembered to switch the mode
on. The patterns cover veiled threats — retribution, location threats, pursuit
— and Filipino phrasing, and are written narrowly enough that election and
crime reporting, which uses violent vocabulary constantly, does not trip them.

*Doxxing* detection stays inside Hunter mode: words like "address" and "expose"
are ordinary in reporting, and only make sense as signals when a watch is
specifically hunting for targeting.

### Modes

- **Media Release Threat** — compares posts against an official release.
  Detects impersonation, contradictions and compromised official accounts.
  Needs the reference text, official accounts and official domains to be set.
- **Digital Hunter** — correlates post authors against stored Profiles by
  codename, alias and social handle. Adds doxxing and threat-language rules.

---

## Reading results: filters, saved views and export

The toolbar above the results narrows what you are looking at: verdict (the
legend buttons), platform, status, posts-or-comments, time window, threat type,
pinned-only, free text, plus sort order and page size. Filtering, sorting and
paging all run against the store, so a watch holding thousands of posts stays as
responsive as an empty one.

### Time windows

The window menu runs from **Last hour** through 3, 6 and 12 hours, then 24
hours, 3, 7, 30 and 90 days. Hour-scale windows are what a live incident needs:
during one, "what has landed since I last looked" is the only question, and a
day-scale filter cannot express it.

**Custom range…** opens From and To fields for an exact window — the night of
the 14th, say, or the two hours around a rally. Either end alone works:
`From` only is everything since, `To` only is everything up to. Bounds entered
backwards are swapped rather than returning nothing. The range shows as a single
chip; removing it clears both ends together.

Beside the menu, **By post date / By collected date** chooses what the window
applies to. The default is when the post was published. *By collected date* asks
a different and often more useful question: what did the last sweep bring in,
regardless of how old the posts themselves are — which is exactly what you want
after a collection run that reached back weeks.

### Sorting

Sort by **risk, post date, collected date, engagement, author, relevance,
platform** or **status**, and use the arrow beside the menu to reverse any of
them. Direction is separate from the field, so every sort works both ways.

- **Engagement** totals reactions, comments and shares — what actually spread,
  as opposed to what merely scored. Only sources that report counts contribute;
  the rest sort as zero.
- **Author** groups repeat actors into blocks instead of scattering them, which
  is how a handful of accounts posting constantly becomes visible.
- **Collected date** separates when you found something from when it was said.

Choosing a sort sets the direction that reads naturally for it — highest risk,
newest date, but A–Z for names — and the arrow overrides that. Pinned posts stay
on top of every ordering. Sort and direction are view preferences, not filters,
so they are not counted in the filter badge and a saved view does not force them
on you.

**Filters are retained.** They are mirrored into the URL and saved per watch, so
a reload, a trip to the link map and back, or reopening the watch tomorrow all
land on the view you left. Precedence is: an explicit URL first (which is what
makes a filtered view shareable and bookmarkable), then your last view on that
watch, then defaults. Back and forward move between filter views rather than
leaving the page.

Everything currently narrowing the list appears as a removable chip under the
toolbar, with a count beside the saved views. Nothing filters invisibly — which
matters most for threat type, set by clicking a type in the briefing rather than
from a control of its own.

**Saved views** name a set of filters worth returning to ("High risk, last
week"). Saving over an existing name updates it. A view that names nothing
watch-specific is offered in every watch, marked with a globe; one naming a
platform belongs to the watch that collected it. Applying a view keeps your
current sort and page size, since those are how you read results rather than
part of the question being asked. `Clear filters` likewise keeps them.

**Exports follow the view.** CSV and JSON contain exactly what the filters
select — all of it, not just the page on screen — in the same order. The
filename records the slice (`signal-monitor-3-bad-x-7d-2026-09-14.csv`) and the
JSON carries the filters it was exported under, so a file is never mistaken for
the complete set later. Hold **Shift** while clicking CSV or JSON to export
every post in the watch instead, for a full backup.

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

### Adding to a map from anywhere

There is one **Add to link map** dialog, reached from three places:

| From | Button | What goes on the map |
|---|---|---|
| A watch | **Link map**, above the post list | every flagged post in the watch |
| Selected posts | **Add to map**, in the selection bar | exactly what you ticked |
| A profile | **Link map**, on the profile page | the profile and its accounts |

Each opens the same dialog, which asks the one question that matters: **a new
map, or the one you are already building?** Merging is the default, because
"add this to the map I am working on" is the common case — a brand-new map per
action is what made the old flow tedious.

Entities are matched on label and type, so adding the same thing twice never
duplicates nodes, and a profile merged into a post-built map joins at the node
that already stands for it. A live count tells you what would be drawn before
you commit, so nobody adds four hundred nodes by accident.

A selection you ticked by hand is added **whole** — no verdict or score filter
is applied on top of it, since you already chose those posts.

### Comments on the map

Commenters attach to the account whose thread they replied to, not to the
subject directly. That is the point of collecting comments: twenty fresh
accounts converging on one post is a shape you can see, and it is invisible in
a list. When the thread belongs to the watched subject itself, the commenters
attach straight to the subject node rather than to a duplicate of it.

### Importing a map built by a script

`scripts/comments.py --linkmap FILE` writes a map file. **Import map** on the
Link Maps page loads it, which is how a scheduled sweep puts its graph in front
of an analyst without the script needing any access to the browser's storage.

### Network analysis

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
python tests/test_browser_filters.py    # Chromium: filter retention, saved views, export
```

The browser suites need Playwright:

```bash
pip install playwright && python -m playwright install chromium
```
