"""Browser tests for the watch toolbar: filter retention, saved views, export.

Filters are the analyst's working state, so they are expected to survive a
reload, a trip to another page and back, and to decide what an export contains.
None of that can be checked without a real browser: retention runs through the
URL, IndexedDB and the History API.

Needs Playwright. Run: python tests/test_browser_filters.py
"""
import os
import sys
import json
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from werkzeug.serving import make_server

from app import create_app
from playwright.sync_api import sync_playwright

PORT = 5096
BASE = "http://127.0.0.1:%d" % PORT
app = create_app(); app.config["TESTING"] = True
srv = make_server("127.0.0.1", PORT, app, threaded=True)
threading.Thread(target=srv.serve_forever, daemon=True).start()
time.sleep(1.0)

FAIL = []
def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (" :: " + str(detail) if detail else ""))
    if not cond: FAIL.append(name)

SEED = """async () => {
    await Store.wipe();
    const id = await Store.put('watches', {
        name:'UI test watch', subject:'ACME',
        keywords:{required:[],optional:[],excluded:[],match_mode:'all'},
        weights:{}, updated_at:new Date().toISOString(),
    });
    const rows = [];
    for (let i=0;i<40;i++) rows.push({
        watch_id:id, author:'a'+i, handle:'h'+i, text:'ACME post '+i,
        platform: i%2 ? 'X' : 'News site',
        verdict: i%4===0?'bad':(i%3===0?'warn':'ok'), score: i%100,
        types: i%4===0?['Scam']:[], status: i%5===0?'escalated':'new',
        posted_ts: new Date(Date.now()-(i%20)*86400000).toISOString(),
        dedupe_key:'u'+i, pinned: i===7,
    });
    await Store.putMany('posts', rows);
    return id;
}"""

with sync_playwright() as pw:
    b = pw.chromium.launch(headless=True)
    page = b.new_context(accept_downloads=True).new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("console", lambda m: errors.append("console.error: " + m.text)
            if m.type == "error" else None)

    page.goto(BASE + "/login")
    page.fill("input[type=password]", "profiler2024")
    page.click("button[type=submit], input[type=submit]")
    page.wait_for_load_state("networkidle")

    page.goto(BASE + "/monitor/")
    page.wait_for_function("() => window.Store && window.Data")
    wid = page.evaluate(SEED)

    W = BASE + "/monitor/watch?id=%d" % wid
    page.goto(W)
    page.wait_for_selector("#list .mon-post", timeout=15000)
    count = lambda: page.eval_on_selector_all("#list .mon-post", "els => els.length")
    total = count()
    check("watch page renders posts", total > 0, "n=%d" % total)

    # --- Filter, then reload: the view must come back. ---
    page.select_option("#platformSel", "X")
    page.wait_for_timeout(700)
    n_x = count()
    check("platform filter applies", 0 < n_x < total, "n=%d of %d" % (n_x, total))
    check("filter written to URL", "platform=X" in page.url, page.url)

    page.reload()
    page.wait_for_selector("#list .mon-post", timeout=15000)
    after, sel = count(), page.input_value("#platformSel")
    check("filter survives reload", after == n_x and sel == "X",
          "n=%d sel=%s" % (after, sel))

    # --- Retention with no query string at all. ---
    page.goto(BASE + "/monitor/")
    page.wait_for_load_state("networkidle")
    page.goto(W)
    page.wait_for_selector("#list .mon-post", timeout=15000)
    back, sel2 = count(), page.input_value("#platformSel")
    check("filter retained without URL", back == n_x and sel2 == "X",
          "n=%d sel=%s" % (back, sel2))

    # --- Active-filter chip removes just that filter. ---
    page.click('[data-drop-filter="platform"]')
    page.wait_for_timeout(800)
    check("chip clears its filter", count() == total, "n=%d" % count())

    # --- The date window (the filter that used to be silently ignored). ---
    d = page.evaluate("""async (wid) => {
        const all = await Store.queryPosts(wid, {per_page:1000});
        const d7  = await Store.queryPosts(wid, {days:'7', per_page:1000});
        return {all: all.matching, d7: d7.matching};
    }""", wid)
    page.select_option("#daysSel", "7")
    page.wait_for_timeout(800)
    check("date filter narrows the list", d["d7"] < d["all"] and count() <= d["d7"],
          "d7=%d all=%d shown=%d" % (d["d7"], d["all"], count()))

    # --- Saved view: save, reset, re-apply. ---
    page.select_option("#statusSel", "escalated")
    page.wait_for_timeout(800)
    narrowed = count()
    page.evaluate("() => { window.prompt = () => 'My view'; }")
    page.click("#btnSavePreset")
    page.wait_for_timeout(900)
    n_presets = page.eval_on_selector_all("#presetList .mon-preset", "els => els.length")
    check("preset saved and listed", n_presets == 1, "n=%d" % n_presets)

    page.click("#btnResetFilters")
    page.wait_for_timeout(800)
    check("reset clears filters", count() == total, "n=%d" % count())

    page.click("#presetList .mon-preset > button")
    page.wait_for_timeout(900)
    re_n, st = count(), page.input_value("#statusSel")
    check("preset re-applies its filters", re_n == narrowed and st == "escalated",
          "n=%d status=%s (saved view had %d)" % (re_n, st, narrowed))
    on = page.eval_on_selector_all("#presetList .mon-preset.on", "els => els.length")
    check("active preset highlighted", on == 1, "n=%d" % on)

    # --- Export follows the filters. ---
    exp = page.evaluate("""async (wid) => {
        const q = await Store.queryPosts(wid, {status:'escalated', days:'7', per_page:100000});
        const all = await Store.queryPosts(wid, {per_page:100000});
        return {filtered: q.matching, all: all.matching};
    }""", wid)

    with page.expect_download() as dl:
        page.click("#btnCsv")
    body = open(dl.value.path(), encoding='utf-8').read().strip().split("\n")
    rows_out = len(body) - 1
    check("CSV exports the filtered view, not everything",
          rows_out == exp["filtered"] and rows_out < exp["all"],
          "csv=%d filtered=%d all=%d" % (rows_out, exp["filtered"], exp["all"]))
    check("CSV filename reflects the filters",
          "escalated" in dl.value.suggested_filename, dl.value.suggested_filename)

    with page.expect_download() as dl2:
        page.click("#btnJson")
    data = json.load(open(dl2.value.path(), encoding='utf-8'))
    check("JSON records the scope it was exported under",
          data["scope"] == "current filtered view" and data["count"] == exp["filtered"],
          {"scope": data["scope"], "count": data["count"], "filters": data["filters"]})

    with page.expect_download() as dl3:
        page.click("#btnJson", modifiers=["Shift"])
    full = json.load(open(dl3.value.path(), encoding='utf-8'))
    check("Shift-click exports every post", full["count"] == exp["all"],
          "n=%d of %d" % (full["count"], exp["all"]))

    check("no uncaught JS errors", not errors, errors[:4])
    b.close()

srv.shutdown()
print("\n" + "=" * 58)
print("FAILURES: %d" % len(FAIL) + ("  -> " + ", ".join(FAIL) if FAIL else ""))
sys.exit(1 if FAIL else 0)
