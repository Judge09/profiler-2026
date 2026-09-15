"""The OSINT toolkit in a real browser: breach history, pivots, passwords.

These are network-dependent (they read the public breach catalogue), so they
are slower than the rest of the suite and will fail without internet. That is
the honest trade: the value of this panel is what the live catalogue says, and
a mocked version would prove nothing about it.

Needs Playwright. Run: python tests/test_browser_osint.py
"""
import os,sys,threading,time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from werkzeug.serving import make_server
from app import create_app
from playwright.sync_api import sync_playwright
PORT=5086; BASE="http://127.0.0.1:%d"%PORT
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
    page.goto(BASE+"/osint/"); page.wait_for_selector("#toolTabs",timeout=10000)

    print("=== BREACH HISTORY ===")
    page.fill("#breachInput","comelec.gov.ph"); page.click("#breachBtn")
    # "Checking the breach catalogue" also contains "breach", so wait for the
    # rendered card rather than for a word the spinner shares.
    page.wait_for_selector("#breachOut .card", timeout=25000)
    t=page.eval_on_selector("#breachOut","el=>el.textContent")
    check("COMELEC breach shown", "COMELEC" in t, t[:70].strip())
    check("record count shown", "228,605" in t)
    check("severe classes flagged", "biometric" in t.lower())

    print("=== PH BREACHES ===")
    page.click("#breachPhBtn")
    page.wait_for_function(
        "() => /\.ph domains/.test(document.querySelector('#breachOut').textContent)",
        timeout=25000)
    check("PH list rendered", "Wendy" in page.eval_on_selector("#breachOut","el=>el.textContent"))

    print("=== PIVOTS ===")
    page.click("[data-tool='pivot']"); page.wait_for_timeout(200)
    check("pivot tab shown", page.is_visible("#pivotInput"))
    page.select_option("#pivotKind","image")
    page.fill("#pivotInput","https://example.com/face.jpg"); page.click("#pivotBtn")
    page.wait_for_function("() => document.querySelector('#pivotOut').textContent.includes('TinEye')",timeout=15000)
    t=page.eval_on_selector("#pivotOut","el=>el.textContent")
    for tool in ["Google Lens","Yandex","TinEye","InVID"]:
        check("%s link built" % tool, tool in t)
    href=page.eval_on_selector("#pivotOut a","el=>el.href")
    check("links are real URLs", href.startswith("https://lens.google.com"), href[:58])

    print("=== PASSWORD CHECK ===")
    page.click("[data-tool='password']"); page.wait_for_timeout(200)
    page.fill("#pwInput","password123"); page.click("#pwBtn")
    page.wait_for_function("() => /breach/i.test(document.querySelector('#pwOut').textContent)",timeout=20000)
    t=page.eval_on_selector("#pwOut","el=>el.textContent")
    check("breached password flagged", "2,266,543" in t or "appears in" in t, t[:70].strip())
    check("privacy note shown", "never left this machine" in t)
    check("field cleared after check", page.eval_on_selector("#pwInput","el=>el.value")=="")

    check("no JS errors", not errs, errs[:2])
    b.close()
srv.shutdown()
print("FAILURES:",FAIL)
sys.exit(1 if FAIL else 0)
