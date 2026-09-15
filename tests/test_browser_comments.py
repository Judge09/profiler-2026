"""Browser tests for comments in the list and the shared link-map dialog.

These are the two things added most recently that only exist once a real
browser has run the page: the comment tag and kind filter are rendered by
`monitor.js` against IndexedDB rows, and `LinkMapAdd` is a dialog that talks to
the server and writes a graph back into the store. Neither can be checked
without driving the actual UI.

Needs Playwright. Run: python tests/test_browser_comments.py
"""
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from werkzeug.serving import make_server

from app import create_app
from playwright.sync_api import sync_playwright

PORT = 5094
BASE = "http://127.0.0.1:%d" % PORT
app = create_app()
app.config["TESTING"] = True
srv = make_server("127.0.0.1", PORT, app, threaded=True)
threading.Thread(target=srv.serve_forever, daemon=True).start()
time.sleep(1.0)

FAIL = []


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name +
          (" :: " + str(detail) if detail else ""))
    if not cond:
        FAIL.append(name)


# Three posts and four comments, one of them a scam pointing at a shortener --
# the shape a brigaded thread actually has.
SEED = """async () => {
    await Store.wipe();
    const id = await Store.put('watches', {
        name:'Comment test watch', subject:'NAMFREL',
        kw_required:'BARMM', kw_optional:'', kw_excluded:'',
        kw_match_mode:'any', weights:{},
        threshold_review:30, threshold_high:60,
        updated_at:new Date().toISOString(),
    });
    const rows = [];
    for (let i=0;i<3;i++) rows.push({
        watch_id:id, author:'NAMFREL', handle:'namfrel',
        text:'BARMM canvassing update number '+i, platform:'Facebook',
        verdict:'warn', score:40, types:['Relevant mention'], status:'new',
        kind:'post', parent_author:'', parent_url:'',
        posted_ts:new Date().toISOString(), dedupe_key:'p'+i, pinned:false,
    });
    for (let i=0;i<4;i++) rows.push({
        watch_id:id, author:'Commenter '+i, handle:'c'+i,
        text: i===0 ? 'claim your free load at bit.ly/xyz limited slots'
                    : 'BARMM reply number '+i,
        platform:'Facebook',
        verdict: i===0 ? 'bad' : 'warn', score: i===0 ? 80 : 45,
        types: i===0 ? ['Scam'] : ['Relevant mention'], status:'new',
        kind:'comment', parent_author:'NAMFREL',
        parent_url:'https://www.facebook.com/permalink.php?story_fbid=1',
        posted_ts:new Date().toISOString(), dedupe_key:'c'+i, pinned:false,
    });
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
    count = lambda: page.eval_on_selector_all("#list .mon-post", "els => els.length")

    print("\n=== COMMENTS IN THE LIST ===")
    check("all posts and comments render", count() == 7, "n=%d" % count())

    tags = page.eval_on_selector_all(".mon-tag.comment-tag", "els => els.length")
    check("comments are tagged as replies", tags == 4, "tags=%d" % tags)

    labelled = page.eval_on_selector_all(
        ".mon-tag.comment-tag", "els => els.every(e => /NAMFREL/.test(e.textContent))")
    check("the tag names the thread it sits under", labelled)

    print("\n=== THE KIND FILTER ===")
    page.select_option("#kindSel", "comment")
    page.wait_for_function("() => document.querySelectorAll('#list .mon-post').length === 4",
                           timeout=8000)
    check("comments only", count() == 4, "n=%d" % count())

    page.select_option("#kindSel", "post")
    page.wait_for_function("() => document.querySelectorAll('#list .mon-post').length === 3",
                           timeout=8000)
    check("posts only", count() == 3, "n=%d" % count())

    # The filter has to survive a reload like every other one, because it
    # travels in the URL rather than in page-local state.
    page.reload()
    page.wait_for_selector("#list .mon-post", timeout=15000)
    kept = page.eval_on_selector("#kindSel", "el => el.value")
    check("the kind filter survives a reload", kept == "post" and count() == 3,
          "value=%r n=%d" % (kept, count()))

    page.select_option("#kindSel", "")
    page.wait_for_function("() => document.querySelectorAll('#list .mon-post').length === 7",
                           timeout=8000)
    check("clearing shows both again", count() == 7, "n=%d" % count())

    print("\n=== ADD TO LINK MAP: FROM THE WATCH ===")
    page.click("#btnLinkmap")
    page.wait_for_selector("#lmAddModal.show", timeout=8000)
    check("the shared dialog opens", page.is_visible("#lmAddModal"))

    # The preview is a server round-trip, so wait for it to report real counts.
    page.wait_for_function(
        "() => /Would draw/.test(document.querySelector('#lmAddStats').textContent)",
        timeout=12000)
    stats_text = page.eval_on_selector("#lmAddStats", "el => el.textContent")
    check("the preview counts what would be drawn", "node(s)" in stats_text,
          stats_text.strip()[:80])

    page.click("#lmAddGo")
    page.wait_for_function("async () => (await Store.all('graphs')).length === 1",
                           timeout=12000)
    graphs = page.evaluate("async () => await Store.all('graphs')")
    check("a map was written to the store", len(graphs) == 1,
          "n=%d" % len(graphs))

    import json as _json
    graph = _json.loads(graphs[0]["graph_json"])
    labels = [n["label"] for n in graph["nodes"]]
    check("the scam commenter is on the map", any("c0" in l for l in labels), labels)
    check("the shared shortener is on the map", "bit.ly" in labels, labels)
    check("the subject appears exactly once", labels.count("NAMFREL") == 1, labels)

    print("\n=== ADD TO LINK MAP: MERGING, NOT DUPLICATING ===")
    before = len(graph["nodes"])
    page.goto(W)
    page.wait_for_selector("#list .mon-post", timeout=15000)
    page.click("#btnLinkmap")
    page.wait_for_selector("#lmAddModal.show", timeout=8000)
    # Target the map just created rather than a new one.
    page.select_option("#lmAddTarget", str(graphs[0]["id"]))
    page.wait_for_function(
        "() => /Would draw/.test(document.querySelector('#lmAddStats').textContent)",
        timeout=12000)
    page.click("#lmAddGo")
    page.wait_for_selector("#lmAddModal.show", state="hidden", timeout=12000)

    after_rows = page.evaluate("async () => await Store.all('graphs')")
    after = _json.loads(after_rows[0]["graph_json"])
    check("merging did not create a second map", len(after_rows) == 1,
          "maps=%d" % len(after_rows))
    check("merging the same posts added no nodes",
          len(after["nodes"]) == before,
          "%d -> %d" % (before, len(after["nodes"])))

    print("\n=== ADD TO LINK MAP: SELECTED POSTS ===")
    page.goto(W)
    page.wait_for_selector("#list .mon-post", timeout=15000)
    # Each tick re-renders the list, so the checkboxes must be clicked one at a
    # time against the current DOM -- a batch collected up front would leave
    # the second click hitting a detached element.
    page.click("#list .mon-post:nth-of-type(1) input[data-act=select]")
    page.wait_for_function("() => document.querySelector('#selCount').textContent === '1'",
                           timeout=8000)
    page.click("#list .mon-post:nth-of-type(2) input[data-act=select]")
    page.wait_for_function("() => document.querySelector('#selCount').textContent === '2'",
                           timeout=8000)
    page.wait_for_selector("#bulkBar:not([style*='display: none'])", timeout=8000)
    check("the selection bar appears", page.is_visible("#bulkLinkmap"))

    page.click("#bulkLinkmap")
    page.wait_for_selector("#lmAddModal.show", timeout=8000)
    body = page.eval_on_selector("#lmAddBody", "el => el.textContent")
    check("the dialog says how many were selected", "2" in body, body[:90])

    print("\n=== ADD TO LINK MAP: FROM A PROFILE ===")
    pid = page.evaluate("""async () => await Store.put('profiles', {
        codename:'TARGET-1', real_name:'A Person', known_aliases:[],
        social_links:[{platform:'Facebook', username:'target1',
                       url:'https://facebook.com/target1'}],
        tag_ids:[], created_at:new Date().toISOString(),
    })""")
    page.goto(BASE + "/profiles/%d" % pid)
    page.wait_for_selector("#btnLinkmap", timeout=10000)
    page.click("#btnLinkmap")
    page.wait_for_selector("#lmAddModal.show", timeout=8000)
    text = page.eval_on_selector("#lmAddBody", "el => el.textContent")
    check("the profile dialog names the profile", "TARGET-1" in text, text[:90])

    real = [e for e in errors if "favicon" not in e.lower()]
    check("no uncaught JS errors", not real, real[:3])

    browser.close()

srv.shutdown()
print("\n" + "=" * 62)
print("FAILURES: %d" % len(FAIL))
for f in FAIL:
    print("  - " + f)
sys.exit(1 if FAIL else 0)
