"""Drive the real browser stack: IndexedDB store + datalayer + server API.

Runs the app on a local port and exercises Store/Data from inside Chromium,
because IndexedDB behaviour (transactions, indexes, quota, persistence across
reloads) cannot be meaningfully faked.

Needs Playwright:  pip install playwright && python -m playwright install chromium
Run:               python tests/test_browser.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json
import threading
import time
import sys

from werkzeug.serving import make_server

from app import create_app

PORT = 5099
BASE = "http://127.0.0.1:%d" % PORT

app = create_app()
app.config["TESTING"] = True
server = make_server("127.0.0.1", PORT, app, threaded=True)
t = threading.Thread(target=server.serve_forever, daemon=True)
t.start()
time.sleep(1.0)

FAIL = []


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (" :: " + str(detail) if detail else ""))
    if not cond:
        FAIL.append(name)


from playwright.sync_api import sync_playwright  # noqa: E402

with sync_playwright() as pw:
    browser = pw.chromium.launch(headless=True)
    ctx = browser.new_context()
    page = ctx.new_page()

    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("console", lambda m: errors.append("console.error: " + m.text)
            if m.type == "error" else None)

    # Authenticate, then load a page that pulls in store.js + datalayer.js.
    page.goto(BASE + "/login")
    page.fill("input[type=password]", "profiler2024")
    page.click("button[type=submit], input[type=submit]")
    page.wait_for_load_state("networkidle")
    check("logged in", "/login" not in page.url, page.url)

    page.goto(BASE + "/monitor/")
    page.wait_for_load_state("networkidle")

    has_store = page.evaluate("typeof window.Store !== 'undefined'")
    has_data = page.evaluate("typeof window.Data !== 'undefined'")
    check("store.js loaded", has_store)
    check("datalayer.js loaded", has_data)

    if not (has_store and has_data):
        print("\nScripts not on the page; cannot continue browser tests.")
        browser.close()
        server.shutdown()
        sys.exit(1)

    print("\n=== IndexedDB basics ===")
    r = page.evaluate("""async () => {
        await Store.wipe();
        const id = await Store.put('watches', {name:'T', subject:'ACME', updated_at:'2026-01-01'});
        const got = await Store.get('watches', id);
        return {id, name: got.name};
    }""")
    check("put/get round-trip", r["name"] == "T", r)

    r = page.evaluate("""async () => {
        const rows = [];
        for (let i=0;i<500;i++) rows.push({
            watch_id:1, author:'a'+i, handle:'h'+i, text:'ACME post '+i,
            platform: i%2 ? 'X':'News site', verdict: i%5===0?'bad':(i%3===0?'warn':'ok'),
            score: i%100, types: i%5===0?['Scam']:[], status:'new',
            posted_ts:'2026-09-'+String((i%27)+1).padStart(2,'0')+'T'+String(i%24).padStart(2,'0')+':00:00',
            dedupe_key:'k'+i, pinned: false,
        });
        const t0 = performance.now();
        await Store.putMany('posts', rows);
        const write = performance.now()-t0;
        const t1 = performance.now();
        const counts = await Store.countsFor(1);
        const rows2 = await Store.byIndex('posts','watch_id', IDBKeyRange.only(1));
        rows2[7].pinned = true; await Store.put('posts', rows2[7]);
        const q = await Store.queryPosts(1, {page:1, per_page:25, sort:'risk'});
        const read = performance.now()-t1;
        return {counts, matching:q.matching, pages:q.pages, n:q.posts.length,
                write:Math.round(write), read:Math.round(read),
                firstPinned: q.posts[0].pinned === true};
    }""")
    check("bulk write 500 posts", r["write"] < 3000, "%d ms" % r["write"])
    check("counts by verdict", sum(r["counts"].values()) == 500, r["counts"])
    check("pagination", r["pages"] == 20 and r["n"] == 25,
          "pages=%d n=%d" % (r["pages"], r["n"]))
    check("query speed", r["read"] < 1500, "%d ms" % r["read"])
    check("pinned floats to top", r["firstPinned"])

    print("\n=== Filtering and sorting ===")
    r = page.evaluate("""async () => {
        const bad = await Store.queryPosts(1, {verdict:'bad', per_page:1000});
        const plat = await Store.queryPosts(1, {platform:'X', per_page:1000});
        const term = await Store.queryPosts(1, {q:'post 42', per_page:1000});
        // Pinned posts intentionally float above every ordering, so check the
        // date ordering among the unpinned ones.
        const recentAll = await Store.queryPosts(1, {sort:'recent', per_page:8});
        const oldestAll = await Store.queryPosts(1, {sort:'oldest', per_page:8});
        const recent = {posts: recentAll.posts.filter(p=>!p.pinned)};
        const oldest = {posts: oldestAll.posts.filter(p=>!p.pinned)};
        const pinnedTop = recentAll.posts[0].pinned === true;
        return {
          bad: bad.matching, allBad: bad.posts.every(p=>p.verdict==='bad'),
          plat: plat.matching, allX: plat.posts.every(p=>p.platform==='X'),
          term: term.matching,
          recentOrdered: recent.posts.every((p,i,arr)=>i===0||
              Date.parse(arr[i-1].posted_ts)>=Date.parse(p.posted_ts)),
          oldestOrdered: oldest.posts.every((p,i,arr)=>i===0||
              Date.parse(arr[i-1].posted_ts)<=Date.parse(p.posted_ts)),
          newestFirst: Date.parse(recent.posts[0].posted_ts) > Date.parse(oldest.posts[0].posted_ts),
          pinnedTop,
        };
    }""")
    check("verdict filter", r["allBad"] and r["bad"] == 100, "n=%d" % r["bad"])
    check("platform filter", r["allX"] and r["plat"] == 250, "n=%d" % r["plat"])
    check("text search", r["term"] >= 1, "n=%d" % r["term"])
    check("sort recent (descending)", r["recentOrdered"] and r["newestFirst"])
    check("sort oldest (ascending)", r["oldestOrdered"])
    check("pinned overrides date sort", r["pinnedTop"])

    print("\n=== Manual override wins ===")
    r = page.evaluate("""async () => {
        const q = await Store.queryPosts(1, {verdict:'ok', per_page:1});
        const p = q.posts[0];
        p.manual_verdict = 'bad'; p.manual_score = 95;
        await Store.put('posts', p);
        const counts = await Store.countsFor(1);
        const back = await Store.get('posts', p.id);
        return {counts, eff: Store.effVerdict(back), score: Store.effScore(back)};
    }""")
    check("override changes counts", r["counts"]["bad"] == 101, r["counts"])
    check("effective verdict/score", r["eff"] == "bad" and r["score"] == 95, r)

    print("\n=== Data layer against the server ===")
    r = page.evaluate("""async () => {
        await Store.wipe();
        const w = await Data.createWatch({
          name:'BARMM', subject:'NAMFREL',
          kw_required:'BARMM\\nNAMFREL', kw_optional:'election',
          kw_excluded:'cricket', kw_match_mode:'any'});
        const stats = await Data.addPosts(w, [
          {author:'News', platform:'News site', text:'BARMM election proceeds calmly'},
          {author:'Scammer', platform:'X', handle:'namfrel_ph',
           text:'FREE BARMM voucher! claim http://namfrel-claim.xyz/login send GCash'},
          {author:'Off', platform:'X', text:'Cricket scores from Mumbai'},
        ], {strict:true});
        const counts = await Store.countsFor(w.id);
        return {wid:w.id, stats, counts};
    }""")
    check("addPosts scores via server", r["stats"]["added"] == 2,
          "added=%d off_topic=%d" % (r["stats"]["added"], r["stats"]["off_topic"]))
    check("off-topic dropped", r["stats"]["off_topic"] == 1, r["stats"])
    check("scam flagged", r["counts"]["bad"] >= 1, r["counts"])
    WID = r["wid"]

    print("\n=== Rules-hash invalidation ===")
    r = page.evaluate("""async (wid) => {
        const w = await Store.get('watches', wid);
        const before = await Data.ensureFresh(w);      // nothing stale
        w.kw_excluded = 'voucher';                     // changes the rules
        await Data.saveWatch(w);
        const after = await Data.ensureFresh(w);       // everything stale
        const rows = await Store.byIndex('posts','watch_id', IDBKeyRange.only(wid));
        return {before, after, n: rows.length,
                hashes: [...new Set(rows.map(p=>p.rules_hash))].length};
    }""", WID)
    check("fresh watch rescores nothing", r["before"] == 0, "n=%d" % r["before"])
    check("rule change rescores all", r["after"] == r["n"],
          "rescored=%d of %d" % (r["after"], r["n"]))
    check("one hash after rescore", r["hashes"] == 1, "distinct=%d" % r["hashes"])

    print("\n=== Dedupe ===")
    r = page.evaluate("""async (wid) => {
        const w = await Store.get('watches', wid);
        const again = await Data.addPosts(w, [
          {author:'News', platform:'News site', text:'BARMM election proceeds calmly'},
        ], {strict:true});
        return again;
    }""", WID)
    check("duplicate rejected", r["added"] == 0 and r["skipped"] == 1, r)

    print("\n=== Live collection into IndexedDB ===")
    r = page.evaluate("""async (wid) => {
        const w = await Store.get('watches', wid);
        const res = await Data.collect(w, ['google_news'], {days:30, options:{limit:5}});
        const counts = await Store.countsFor(wid);
        const rows = await Store.byIndex('posts','watch_id', IDBKeyRange.only(wid));
        return {added:res.added, query:res.query, stored:rows.length, counts,
                allScored: rows.every(p=>typeof p.score === 'number')};
    }""", WID)
    check("collect stores to IndexedDB", r["stored"] > 2,
          "added=%d stored=%d" % (r["added"], r["stored"]))
    check("every stored post is scored", r["allScored"], r["counts"])
    check("query was topic-scoped", "BARMM" in (r["query"] or ""), r["query"])

    print("\n=== Derived views ===")
    r = page.evaluate("""async (wid) => {
        const w = await Store.get('watches', wid);
        const b = await Data.briefing(w);
        const d = await Data.dashboard(30);
        const g = await Data.graph(w, {min_score:0});
        return {headline:b.headline, total:b.total,
                dashTotal:d.post_total, storage:d.storage.mode,
                nodes:g.stats.nodes};
    }""", WID)
    check("briefing from store", r["total"] > 0, r["headline"])
    check("dashboard from store", r["dashTotal"] > 0, "posts=%d" % r["dashTotal"])
    check("dashboard says browser", r["storage"] == "browser", r["storage"])
    check("graph builds", r["nodes"] >= 1, "nodes=%d" % r["nodes"])

    print("\n=== Backup / restore ===")
    r = page.evaluate("""async () => {
        const backup = await Store.exportAll();
        const before = backup.counts;
        await Store.wipe();
        const empty = await Store.stats();
        await Store.importAll(backup, {replace:true});
        const after = await Store.stats();
        return {before, empty, after};
    }""")
    check("export captures data", r["before"]["posts"] > 0, r["before"])
    check("wipe empties", r["empty"]["posts"] == 0, r["empty"])
    check("import restores", r["after"]["posts"] == r["before"]["posts"],
          "restored %d posts, %d watches" % (r["after"]["posts"], r["after"]["watches"]))

    print("\n=== Persistence across reload ===")
    stats_before = page.evaluate("async () => await Store.stats()")
    page.reload()
    page.wait_for_load_state("networkidle")
    stats_after = page.evaluate("async () => await Store.stats()")
    check("data survives reload",
          stats_after["posts"] == stats_before["posts"] and stats_after["posts"] > 0,
          "%d posts before, %d after" % (stats_before["posts"], stats_after["posts"]))

    print("\n=== Storage health ===")
    r = page.evaluate("async () => await Store.usage()")
    check("usage reported", r.get("supported") is True,
          "%.1f KB used of %.0f MB" % (r.get("usage", 0) / 1024,
                                       r.get("quota", 0) / 1048576))

    print("\n=== Server sync round-trip ===")
    r = page.evaluate("""async () => {
        const push = await Data.pushToServer();
        await Store.wipe();
        const emptied = await Store.stats();
        await Data.pullFromServer(true);
        const back = await Store.stats();
        return {push:push.counts, emptied, back};
    }""")
    check("push to server", r["push"]["posts"] > 0, r["push"])
    check("pull restores from server", r["back"]["posts"] == r["push"]["posts"],
          "pushed %d, pulled back %d" % (r["push"]["posts"], r["back"]["posts"]))

    real_errors = [e for e in errors if "favicon" not in e.lower()]
    check("no uncaught JS errors", not real_errors, "; ".join(real_errors[:2]))

    browser.close()

server.shutdown()
print("\n" + "=" * 62)
print("FAILURES: %d" % len(FAIL))
for f in FAIL:
    print("  -", f)
sys.exit(1 if FAIL else 0)
