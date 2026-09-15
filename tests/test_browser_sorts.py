"""Browser tests for the time filters and sort controls.

The toolbar's newer controls -- hour windows, a custom From/To range, the
post-vs-collected choice and the sort-direction toggle -- all run against
IndexedDB in `store.js` and are retained through the URL. None of that can be
checked without a real browser.

Needs Playwright. Run: python tests/test_browser_sorts.py
"""
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from werkzeug.serving import make_server

from app import create_app
from playwright.sync_api import sync_playwright

PORT = 5093
BASE = "http://127.0.0.1:%d" % PORT
app = create_app()
app.config["TESTING"] = True
srv = make_server("127.0.0.1", PORT, app, threaded=True)
threading.Thread(target=srv.serve_forever, daemon=True).start()
time.sleep(1.0)

FAIL = []


def check(name, cond, detail=""):
    line = ("  PASS  " if cond else "  FAIL  ") + name +         (" :: " + str(detail) if detail else "")
    # The Windows console is cp1252 and cannot print the arrow the range chip
    # uses; a test must not die on its own output.
    try:
        print(line)
    except UnicodeEncodeError:
        print(line.encode("ascii", "replace").decode("ascii"))
    if not cond:
        FAIL.append(name)


# Four posts spread across time, with distinct scores, authors and engagement
# so every ordering has something to distinguish. All were collected moments
# ago, which is what separates "posted" from "collected".
SEED = """async () => {
    await Store.wipe();
    const id = await Store.put('watches', {
        name:'Sort test watch', subject:'NAMFREL',
        kw_required:'', kw_optional:'', kw_excluded:'',
        kw_match_mode:'any', weights:{},
        threshold_review:30, threshold_high:60,
        updated_at:new Date().toISOString(),
    });
    const mins = (n) => new Date(Date.now() - n*60000).toISOString().slice(0,19);
    const rows = [
      {label:'just-now',   postedMin:20,   score:10, eng:3,   author:'zeta'},
      {label:'two-hours',  postedMin:120,  score:90, eng:900, author:'alpha'},
      {label:'ten-hours',  postedMin:600,  score:50, eng:40,  author:'mid'},
      {label:'three-days', postedMin:4320, score:70, eng:5,   author:'beta'},
    ].map((r, i) => ({
        watch_id:id, author:r.author, handle:r.author,
        text:r.label + ' BARMM post', platform:'Facebook',
        verdict:'warn', score:r.score, manual_score:r.score,
        types:[], status:'new',
        kind:'post', posted_ts:mins(r.postedMin), collected_at:mins(i),
        engagement:{reactions:r.eng}, dedupe_key:r.label, pinned:false,
    }));
    await Store.putMany('posts', rows);
    return id;
}"""

with sync_playwright() as pw:
    browser = pw.chromium.launch(headless=True)
    page = browser.new_context().new_page()
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

    labels = lambda: page.eval_on_selector_all(
        "#list .mon-post .mon-text",
        "els => els.map(e => e.textContent.trim().split(' ')[0])")
    count = lambda: len(labels())

    def wait_for_count(n):
        page.wait_for_function(
            "(n) => document.querySelectorAll('#list .mon-post').length === n",
            arg=n, timeout=8000)

    check("all four posts render", count() == 4, labels())

    print("\n=== HOUR WINDOWS ===")
    page.select_option("#daysSel", "1h")
    wait_for_count(1)
    check("last hour keeps only the 20-minute post", labels() == ["just-now"], labels())

    page.select_option("#daysSel", "3h")
    wait_for_count(2)
    check("last 3 hours adds the 2-hour post",
          set(labels()) == {"just-now", "two-hours"}, labels())

    page.select_option("#daysSel", "12h")
    wait_for_count(3)
    check("last 12 hours adds the 10-hour post", count() == 3, labels())

    page.select_option("#daysSel", "7")
    wait_for_count(4)
    check("last 7 days returns everything", count() == 4, labels())

    print("\n=== THE WINDOW SURVIVES A RELOAD ===")
    page.select_option("#daysSel", "3h")
    wait_for_count(2)
    page.reload()
    page.wait_for_selector("#list .mon-post", timeout=15000)
    check("an hour window is retained",
          page.eval_on_selector("#daysSel", "el => el.value") == "3h" and count() == 2,
          labels())

    print("\n=== POST DATE VS COLLECTED DATE ===")
    # Everything was collected in the last few minutes, so the same 1h window
    # means "one post" by post date and "all four" by collected date.
    page.select_option("#daysSel", "1h")
    wait_for_count(1)
    page.select_option("#dateFieldSel", "collected")
    wait_for_count(4)
    check("by collected date, the same window returns all four",
          count() == 4, labels())
    page.select_option("#dateFieldSel", "")
    wait_for_count(1)
    check("switching back narrows again", count() == 1, labels())

    print("\n=== CUSTOM RANGE ===")
    page.select_option("#daysSel", "custom")
    page.wait_for_selector("#rangeRow", state="visible", timeout=8000)
    check("the range row appears", page.is_visible("#rangeFrom"))

    stamp = page.evaluate("""(h) => {
        const d = new Date(Date.now() - h*3600000);
        const p = (n) => String(n).padStart(2,'0');
        return d.getFullYear()+'-'+p(d.getMonth()+1)+'-'+p(d.getDate())+
               'T'+p(d.getHours())+':'+p(d.getMinutes());
    }""", 11)
    stamp_hi = page.evaluate("""(h) => {
        const d = new Date(Date.now() - h*3600000);
        const p = (n) => String(n).padStart(2,'0');
        return d.getFullYear()+'-'+p(d.getMonth()+1)+'-'+p(d.getDate())+
               'T'+p(d.getHours())+':'+p(d.getMinutes());
    }""", 1)

    page.fill("#rangeFrom", stamp)
    page.dispatch_event("#rangeFrom", "change")
    page.fill("#rangeTo", stamp_hi)
    page.dispatch_event("#rangeTo", "change")
    wait_for_count(2)
    check("the range isolates the middle window",
          set(labels()) == {"two-hours", "ten-hours"}, labels())

    chips = page.eval_on_selector_all(".mon-fchip", "els => els.map(e => e.textContent.trim())")
    check("the range shows as one chip with an arrow",
          any("→" in c for c in chips), chips)

    # Removing that chip must clear the window and both bounds together.
    page.click(".mon-fchip[data-drop-filter='days']")
    wait_for_count(4)
    check("dropping the range chip restores everything", count() == 4, labels())
    check("the range row hides again", not page.is_visible("#rangeRow"))

    print("\n=== SORTS AND THE DIRECTION TOGGLE ===")
    def sort_by(key):
        page.select_option("#sortSel", key)
        page.wait_for_timeout(450)
        return labels()

    check("risk starts highest-first", sort_by("risk")[0] == "two-hours", labels())
    page.click("#sortDirBtn")
    page.wait_for_timeout(450)
    check("the toggle reverses risk", labels()[0] == "just-now", labels())
    check("the arrow icon flips",
          page.eval_on_selector("#sortDirBtn", "el => el.dataset.dir") == "asc")
    page.click("#sortDirBtn")
    page.wait_for_timeout(450)
    check("toggling back restores it", labels()[0] == "two-hours", labels())

    check("post date sorts newest first", sort_by("posted")[0] == "just-now", labels())
    check("collected date sorts newest first",
          sort_by("collected")[0] == "just-now", labels())
    check("engagement sorts highest first",
          sort_by("engagement")[0] == "two-hours", labels())

    # Author is name-like, so choosing it should open A-Z, not Z-A.
    author_order = sort_by("author")
    check("author opens A-Z", author_order[0] == "two-hours", author_order)
    check("author's default direction is ascending",
          page.eval_on_selector("#sortDirBtn", "el => el.dataset.dir") == "asc")
    page.click("#sortDirBtn")
    page.wait_for_timeout(450)
    check("reversing author gives Z-A", labels()[0] == "just-now", labels())

    print("\n=== SORT STATE IS RETAINED ===")
    page.reload()
    page.wait_for_selector("#list .mon-post", timeout=15000)
    check("sort and direction survive a reload",
          page.eval_on_selector("#sortSel", "el => el.value") == "author" and
          page.eval_on_selector("#sortDirBtn", "el => el.dataset.dir") == "desc",
          labels())

    print("\n=== SORT IS NOT COUNTED AS A FILTER ===")
    page.click("#btnResetFilters")
    page.wait_for_timeout(500)
    page.select_option("#sortSel", "engagement")
    page.wait_for_timeout(450)
    badge = page.eval_on_selector(
        "#filterCount", "el => el.style.display === 'none' ? '' : el.textContent")
    check("choosing a sort leaves the filter badge empty", not badge.strip(),
          repr(badge))

    real = [e for e in errors if "favicon" not in e.lower()]
    check("no uncaught JS errors", not real, real[:3])

    print("\n=== TIMESTAMPS ARE READ AS UTC, IN ANY TIMEZONE ===")
    # Stored stamps are UTC with the zone stripped, and `Date.parse` reads a
    # zoneless string as LOCAL. Left uncorrected, every post is shifted by the
    # viewer's offset: in Manila a post collected five minutes ago showed as
    # "8 h ago" and an hour window returned nothing at all.
    TZ_SEED = """async () => {
        await Store.wipe();
        const id = await Store.put('watches', {
            name:'TZ', subject:'X', kw_match_mode:'any', weights:{},
            updated_at:new Date().toISOString() });
        const mins = (n) => new Date(Date.now()-n*60000).toISOString().slice(0,19);
        await Store.putMany('posts', [
          {watch_id:id, author:'a', handle:'a', text:'five-min post',
           platform:'X', verdict:'ok', score:5, manual_score:5, types:[],
           status:'new', kind:'post', posted_ts:mins(5), collected_at:mins(5),
           dedupe_key:'tz-a'},
          {watch_id:id, author:'b', handle:'b', text:'two-hour post',
           platform:'X', verdict:'ok', score:5, manual_score:5, types:[],
           status:'new', kind:'post', posted_ts:mins(120),
           collected_at:mins(120), dedupe_key:'tz-b'},
        ]);
        return id;
    }"""

    for tz in ("Asia/Manila", "UTC", "America/New_York"):
        # A fresh context carries no session cookie, so it has to log in
        # before it can reach the watch.
        ctx = browser.new_context(timezone_id=tz)
        tzp = ctx.new_page()
        tzp.goto(BASE + "/login")
        tzp.fill("input[type=password]", "profiler2024")
        tzp.click("button[type=submit], input[type=submit]")
        tzp.wait_for_load_state("networkidle")
        tzp.goto(BASE + "/monitor/")
        tzp.wait_for_function("() => window.Store && window.Data")
        tzwid = tzp.evaluate(TZ_SEED)
        tzp.goto(BASE + "/monitor/watch?id=%d" % tzwid)
        tzp.wait_for_selector("#list .mon-post", timeout=15000)

        meta = tzp.eval_on_selector_all(
            "#list .mon-post .mon-meta", "els => els.map(e => e.textContent)")
        check("%s shows the 5-minute post as minutes old" % tz,
              any("5 min ago" in m for m in meta), meta)
        recent = tzp.evaluate(
            "async (w) => (await Store.queryPosts(w, {days:'1h', per_page:50})).matching",
            tzwid)
        check("%s: an hour window returns exactly one post" % tz, recent == 1,
              recent)
        ctx.close()

    browser.close()

srv.shutdown()
print("\n" + "=" * 60)
print("FAILURES: %d" % len(FAIL))
for f in FAIL:
    print("  - " + f)
sys.exit(1 if FAIL else 0)
