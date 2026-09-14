"""End-to-end walkthrough: click through the app the way a user would.

Starts from a completely empty store and creates a watch, adds posts, builds a
profile, writes a note, checks the dashboard, round-trips a backup and opens a
link map -- all through the real UI in a real browser.

This is the suite that catches integration bugs the unit tests cannot see: it
found both the "off-topic posts were stored on manual add" and the "new
profiles could not be saved at all" defects.

Needs Playwright. Run: python tests/test_walkthrough.py
"""
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from werkzeug.serving import make_server  # noqa: E402

from app import create_app  # noqa: E402

PORT = 5093
BASE = "http://127.0.0.1:%d" % PORT

app = create_app()
app.config["TESTING"] = True
server = make_server("127.0.0.1", PORT, app, threaded=True)
threading.Thread(target=server.serve_forever, daemon=True).start()
time.sleep(1.0)

FAIL = []


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (" :: " + str(detail) if detail else ""))
    if not cond:
        FAIL.append(name)


from playwright.sync_api import sync_playwright  # noqa: E402

with sync_playwright() as pw:
    browser = pw.chromium.launch(headless=True)
    page = browser.new_context().new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))

    print("=== LOGIN ===")
    page.goto(BASE + "/login")
    page.fill("input[type=password]", "profiler2024")
    page.click("button[type=submit], input[type=submit]")
    page.wait_for_load_state("networkidle")
    check("logged in", "/login" not in page.url, page.url)

    page.goto(BASE + "/monitor/")
    page.wait_for_load_state("networkidle")
    page.evaluate("async () => { await Store.wipe(); }")

    print("\n=== EMPTY STATE ===")
    for path, marker in [("/monitor/", "No watches yet"),
                         ("/profiles/", "No profiles yet"),
                         ("/linkmap/", "No maps yet")]:
        errors.clear()
        page.goto(BASE + path)
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(400)
        body = page.inner_text("body")
        check("empty state on %s" % path, marker in body,
              body[:70].replace("\n", " "))
        real = [e for e in errors if "favicon" not in e.lower()]
        check("no errors on %s" % path, not real, "; ".join(real[:1]))

    print("\n=== CREATE A WATCH THROUGH THE UI ===")
    page.goto(BASE + "/monitor/")
    page.wait_for_load_state("networkidle")
    page.click("#btnNewWatch")
    page.wait_for_timeout(300)
    page.fill("#w-name", "BARMM QA")
    page.fill("#w-subject", "NAMFREL")
    page.fill("#w-required", "BARMM, NAMFREL")
    page.fill("#w-optional", "election")
    page.fill("#w-excluded", "cricket")
    page.click("#w-save")
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(700)
    check("redirected to the new watch", "/monitor/watch?id=" in page.url, page.url)

    name = page.inner_text("#watchName")
    check("watch name rendered from the store", "BARMM QA" in name, name)
    WID = int(page.url.split("id=")[-1])

    chips = page.evaluate("""() => ({
        req: Array.from(document.querySelectorAll('#kwReq .mon-chip .t')).map(e=>e.textContent),
        opt: Array.from(document.querySelectorAll('#kwOpt .mon-chip .t')).map(e=>e.textContent),
        exc: Array.from(document.querySelectorAll('#kwExc .mon-chip .t')).map(e=>e.textContent),
    })""")
    check("keyword chips populated",
          chips["req"] == ["BARMM", "NAMFREL"] and chips["exc"] == ["cricket"], chips)

    page.wait_for_timeout(700)
    preview = page.inner_text("#topicPreview")
    check("topic preview shows the query", "BARMM" in preview,
          preview[:80].replace("\n", " "))

    print("\n=== ADD A POST THROUGH THE UI ===")
    page.click("#btnAddPost")
    page.wait_for_timeout(400)
    page.fill("#np-author", "Fake NAMFREL")
    page.fill("#np-handle", "namfrel_ph")
    page.fill("#np-text",
              "FREE BARMM voucher! Claim now at http://namfrel-claim.xyz/login "
              "and send GCash to 09171234567")
    page.click("#np-save")
    page.wait_for_timeout(1500)

    r = page.evaluate("""async () => {
        const w = (await Data.watches())[0];
        const counts = await Store.countsFor(w.id);
        const rows = await Store.byIndex('posts','watch_id', IDBKeyRange.only(w.id));
        return {counts, n: rows.length, score: rows[0] && rows[0].score,
                types: rows[0] && rows[0].types};
    }""")
    check("post stored and scored", r["n"] == 1 and r["score"] > 50, r)
    check("scam signals detected", "Scam" in (r["types"] or []) or
          "Phishing" in (r["types"] or []), r["types"])

    rendered = page.inner_text("#list")
    check("post rendered in the list", "Fake NAMFREL" in rendered,
          rendered[:70].replace("\n", " "))

    print("\n=== SOURCE PICKER ===")
    # The picker used to render 26 tiles inline, which pushed the query box and
    # the Collect button below the fold on a normal laptop screen -- the thing
    # you came to click was never actually on screen.
    r = page.evaluate("""() => {
        const y = (s) => { const e = document.querySelector(s);
                           return e ? Math.round(e.getBoundingClientRect().top) : null; };
        return {btn: y('#btnCollect'), query: y('#c-query'), vh: window.innerHeight,
                hidden: document.getElementById('srcBody').hidden};
    }""")
    # With posts collected the briefing legitimately fills the top of the
    # page, so this only asserts the controls exist. The empty-watch case --
    # where collection is the only useful action -- is checked further down.
    check("collect controls present",
          r["btn"] is not None and r["query"] is not None, r)
    check("source list starts collapsed", r["hidden"], r)

    summary = page.inner_text("#srcToggle")
    check("summary names the selection", "selected" in summary,
          summary.replace("\n", " ")[:60])

    page.click("#srcToggle")
    page.wait_for_timeout(300)
    check("picker expands",
          not page.evaluate("() => document.getElementById('srcBody').hidden"))

    page.click("[data-preset='none']")
    page.wait_for_timeout(200)
    n = page.evaluate("() => document.querySelectorAll('.mon-src-item.on').length")
    check("Clear preset deselects everything", n == 0, n)

    page.click(".mon-src-item[data-src='google_news']")
    page.wait_for_timeout(200)
    check("clicking a tile selects it",
          page.evaluate("() => document.querySelectorAll('.mon-src-item.on').length") == 1)
    check("summary updates on selection", "1 selected" in page.inner_text("#srcToggle"),
          page.inner_text("#srcToggle").replace("\n", " ")[:60])

    page.click("[data-preset='open']")
    page.wait_for_timeout(250)
    n = page.evaluate("() => document.querySelectorAll('.mon-src-item.on').length")
    check("'Everything open' selects the keyless sources", n > 5, n)

    # Nothing selected plus Collect should reveal the picker, not merely
    # complain about a panel the user cannot see.
    page.click("[data-preset='none']")
    page.wait_for_timeout(150)
    page.click("#srcToggle")
    page.wait_for_timeout(200)
    page.click("#btnCollect")
    page.wait_for_timeout(800)
    check("empty collect reopens the picker",
          not page.evaluate("() => document.getElementById('srcBody').hidden"))

    # Restore a working selection for the rest of the walkthrough.
    page.click("[data-preset='none']")
    page.wait_for_timeout(150)
    page.click(".mon-src-item[data-src='google_news']")
    page.wait_for_timeout(150)

    print("\n=== EMPTY WATCH PUTS COLLECTION FIRST ===")
    # A watch with no posts has nothing to brief on, and collection is the only
    # useful action -- so it has to be on screen without scrolling. The briefing
    # panel used to reserve 345px of zeroes here and push it off a 720px screen.
    newId = page.evaluate("""async () => {
        const w = await Data.createWatch({name: 'Fold check', kw_required: 'ACME'});
        return w.id;
    }""")
    page.goto(BASE + "/monitor/watch?id=" + str(newId))
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(1400)
    r = page.evaluate("""() => {
        const y = (s) => { const e = document.querySelector(s);
                           return e ? Math.round(e.getBoundingClientRect().top) : null; };
        return {btn: y('#btnCollect'), query: y('#c-query'), src: y('#srcToggle'),
                vh: window.innerHeight};
    }""")
    check("empty watch: collect controls above the fold",
          r["btn"] < r["vh"] and r["query"] < r["vh"] and r["src"] < r["vh"], r)

    # Remove the throwaway watch and return to the real one, so the steps that
    # follow see the state they expect.
    page.evaluate("async (id) => { await Data.deleteWatch(id); }", newId)
    page.goto(BASE + "/monitor/watch?id=" + str(WID))
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(1200)


    print("\n=== NO NESTED SCROLL TRAP ===")
    # Two scroll containers side by side stole the wheel from the page, so
    # putting the cursor over the settings column scrolled the column instead.
    r = page.evaluate("""() => {
        const side = document.querySelector('.mon-side');
        return {nested: side.scrollHeight > side.clientHeight,
                pos: getComputedStyle(side).position};
    }""")
    check("settings column does not scroll separately", not r["nested"], r)


    print("\n=== OFF-TOPIC POST IS REJECTED ===")
    page.click("#btnAddPost")
    page.wait_for_timeout(400)
    page.fill("#np-author", "Sports desk")
    page.fill("#np-text", "Cricket scores from the Mumbai match yesterday")
    page.click("#np-save")
    page.wait_for_timeout(1200)
    r = page.evaluate("""async () => {
        const w = (await Data.watches())[0];
        return (await Store.byIndex('posts','watch_id', IDBKeyRange.only(w.id))).length;
    }""")
    check("excluded post not stored", r == 1, "%d posts" % r)

    print("\n=== CREATE A PROFILE THROUGH THE UI ===")
    page.goto(BASE + "/profiles/new")
    page.wait_for_load_state("networkidle")
    page.fill("#f-codename", "NIGHTJAR")
    page.fill("#f-real_name", "Juan Dela Cruz")
    page.fill("#f-aliases", "jdc, nightjar_ph")
    page.click("#saveBtn")
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(800)
    check("redirected to the profile", "/profiles/" in page.url and
          "new" not in page.url, page.url)
    body = page.inner_text("body")
    check("profile detail renders", "NIGHTJAR" in body, body[:70].replace("\n", " "))

    print("\n=== ADD AN INTEL NOTE ===")
    page.fill("#noteText", "Observed coordinating voucher posts")
    page.fill("#noteSource", "Signal Monitor")
    page.click("#addNote")
    page.wait_for_timeout(700)
    notes = page.inner_text("#pNotes")
    check("note appears", "coordinating voucher" in notes,
          notes[:70].replace("\n", " "))

    print("\n=== PROFILE APPEARS IN THE LIST ===")
    page.goto(BASE + "/profiles/")
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(600)
    body = page.inner_text("body")
    check("profile listed", "NIGHTJAR" in body)
    check("count reflects the store", "1 subject" in body,
          [l for l in body.split("\n") if "subject" in l][:1])

    print("\n=== DASHBOARD AGGREGATES BROWSER DATA ===")
    errors.clear()
    page.goto(BASE + "/monitor/dashboard")
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(2500)
    body = page.inner_text("body")
    check("dashboard shows the watch", "BARMM QA" in body,
          body[:90].replace("\n", " "))
    check("storage bar present", "browser" in body.lower())
    real = [e for e in errors if "favicon" not in e.lower()]
    check("no dashboard JS errors", not real, "; ".join(real[:1]))

    print("\n=== BACKUP ROUND-TRIP THROUGH THE STORE ===")
    r = page.evaluate("""async () => {
        const backup = await Store.exportAll();
        await Store.wipe();
        const empty = await Store.stats();
        await Store.importAll(backup, {replace:true});
        const after = await Store.stats();
        return {counts: backup.counts, empty, after};
    }""")
    check("backup captured everything",
          r["counts"]["watches"] == 1 and r["counts"]["posts"] == 1 and
          r["counts"]["profiles"] == 1 and r["counts"]["notes"] == 1, r["counts"])
    check("restore returns it all",
          r["after"]["watches"] == 1 and r["after"]["profiles"] == 1, r["after"])

    print("\n=== DORK ENGINE ===")
    errors.clear()
    page.goto(BASE + "/dorks/")
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(900)
    r = page.evaluate("""() => ({
        cats: document.querySelectorAll('#dorkCategoryBar .dork-pill').length,
        profiles: document.querySelectorAll('#dorkProfileSelect option').length,
    })""")
    check("dork categories loaded", r["cats"] > 3, r)
    check("profile picker filled from the store", r["profiles"] >= 2, r)
    real = [e for e in errors if "favicon" not in e.lower()]
    check("no dork JS errors", not real, "; ".join(real[:1]))

    print("\n=== USERNAME OSINT ===")
    errors.clear()
    page.goto(BASE + "/osint/")
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(700)
    r = page.evaluate("""() => document.querySelectorAll('#saveProfileSelect option').length""")
    check("osint profile picker filled", r >= 2, r)
    real = [e for e in errors if "favicon" not in e.lower()]
    check("no osint JS errors", not real, "; ".join(real[:1]))

    print("\n=== LINK MAP FROM THE WATCH ===")
    r = page.evaluate("""async () => {
        const w = (await Data.watches())[0];
        const res = await Data.graph(w, {min_score: 0});
        const id = await Store.put('graphs', {
            title: 'QA map', profile_id: null,
            graph_json: JSON.stringify(res.graph),
            updated_at: new Date().toISOString().slice(0,19)});
        return {nodes: res.stats.nodes, id};
    }""")
    check("graph built from stored posts", r["nodes"] >= 2, r)

    errors.clear()
    page.goto(BASE + "/linkmap/edit?id=" + str(r["id"]))
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(1800)
    title = page.input_value("#graphTitle")
    check("map opens from the store", title == "QA map", title)
    count = page.inner_text("#graphCountBadge")
    check("map renders its nodes", "node" in count.lower(), count)
    real = [e for e in errors if "favicon" not in e.lower()]
    check("no linkmap JS errors", not real, "; ".join(real[:1]))

    print("\n=== VAULT PAGE ===")
    errors.clear()
    page.goto(BASE + "/monitor/vault")
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(600)
    real = [e for e in errors if "favicon" not in e.lower()]
    check("no vault JS errors", not real, "; ".join(real[:1]))

    browser.close()

server.shutdown()
print("\n" + "=" * 62)
print("FAILURES: %d" % len(FAIL))
for f in FAIL:
    print("  -", f)
sys.exit(1 if FAIL else 0)
