"""How a profile is named and identified once it reaches the Link Mapper.

The label stays the codename -- short on a crowded canvas, and it keeps real
names out of an exported PNG -- so everything that actually identifies the
person rides along in the tooltip and in fields on the node.

The second half is the part that matters most: a profile carries its id onto
the map, so the same person merges correctly even after a rename. Matching on
the label alone meant a renamed profile silently became a second node and the
map double-counted one person.

Needs Playwright. Run: python tests/test_browser_profile_map.py
"""
import os,sys,threading,time,json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from werkzeug.serving import make_server
from app import create_app
from playwright.sync_api import sync_playwright
PORT=5088; BASE="http://127.0.0.1:%d"%PORT
app=create_app(); app.config["TESTING"]=True
srv=make_server("127.0.0.1",PORT,app,threaded=True)
threading.Thread(target=srv.serve_forever,daemon=True).start(); time.sleep(1)
FAIL=0
def check(n,c,d=""):
    global FAIL
    try: print(("  PASS  " if c else "  FAIL  ")+n+((" :: "+str(d)) if d else ""))
    except UnicodeEncodeError: print(("  PASS  " if c else "  FAIL  ")+n)
    FAIL+=(not c)
with sync_playwright() as pw:
    b=pw.chromium.launch(headless=True); page=b.new_context().new_page()
    errs=[]; page.on("pageerror", lambda e: errs.append(str(e)))
    page.goto(BASE+"/login"); page.fill("input[type=password]","profiler2024")
    page.click("button[type=submit], input[type=submit]"); page.wait_for_load_state("networkidle")
    page.goto(BASE+"/monitor/"); page.wait_for_function("() => window.Store && window.Data")
    pid=page.evaluate("""async () => { await Store.wipe();
      return await Store.put('profiles',{codename:'FALCON-1',real_name:'Juan Dela Cruz',
        known_aliases:['JDC','Juancho'], occupation:'Organiser', nationality:'Filipino',
        social_links:[{platform:'Facebook',username:'juan.dc',url:'https://facebook.com/juan.dc'}],
        tag_ids:[], radar:{labels:['Academics','Physical','Social','Influence','Threat','Digital'],
        scores:[0,0,0,0,8,0]}, created_at:new Date().toISOString()}); }""")
    page.goto(BASE+"/profiles/%d"%pid); page.wait_for_selector("#btnLinkmap",timeout=10000)
    page.click("#btnLinkmap"); page.wait_for_selector("#lmAddModal.show",timeout=8000)
    page.wait_for_function("() => /Would draw|Adds/.test(document.querySelector('#lmAddBody').textContent)",timeout=10000)
    page.click("#lmAddGo")
    page.wait_for_function("async () => (await Store.all('graphs')).length === 1",timeout=12000)
    g=page.evaluate("async () => (await Store.all('graphs'))[0]")
    graph=json.loads(g["graph_json"])
    person=[n for n in graph["nodes"] if n["type"]=="person"][0]
    print("  node:", json.dumps({k:person.get(k) for k in ('label','title','profile_id','real_name','threat')}, ensure_ascii=False))
    check("label is the codename", person["label"]=="FALCON-1", person["label"])
    check("tooltip carries the real name", "Juan Dela Cruz" in person["title"])
    check("tooltip carries aliases", "JDC" in person["title"])
    check("tooltip carries the threat score", "Threat 8/10" in person["title"])
    check("node keeps profile_id", person.get("profile_id")==pid)
    acct=[n for n in graph["nodes"] if n["type"]=="username"]
    check("account keeps its handle for merging", acct and acct[0].get("handle")=="juan.dc",
          acct[0] if acct else None)

    # Rename the profile, add again -> must merge, not duplicate.
    page.evaluate("""async (pid) => { const p = await Store.get('profiles', pid);
        p.codename='RAVEN-9'; await Store.put('profiles', p); }""", pid)
    page.goto(BASE+"/profiles/%d"%pid); page.wait_for_selector("#btnLinkmap",timeout=10000)
    page.click("#btnLinkmap"); page.wait_for_selector("#lmAddModal.show",timeout=8000)
    page.wait_for_timeout(900)
    page.select_option("#lmAddTarget", str(g["id"]))
    page.wait_for_timeout(900)
    page.click("#lmAddGo"); page.wait_for_timeout(2500)
    rows=page.evaluate("async () => await Store.all('graphs')")
    g2=json.loads(rows[0]["graph_json"])
    people=[n for n in g2["nodes"] if n["type"]=="person"]
    check("a renamed profile does not duplicate", len(people)==1,
          [p["label"] for p in people])
    check("only one map exists", len(rows)==1, len(rows))
    check("no JS errors", not errs, errs[:2])
    b.close()
srv.shutdown()
print("FAILURES:",FAIL)
sys.exit(1 if FAIL else 0)
