"""Time filters and sort orders, server-side.

The toolbar offers hour-scale windows ("last hour"), an explicit From/To range,
a choice of which timestamp the window applies to, and eight sort fields that
each work in both directions. This exercises all of it through the real results
endpoint against a seeded watch.

Scores are seeded as `manual_score`, not `cached_score`: the route calls
`ensure_fresh()`, which recomputes cached scores from the watch's rules and
would flatten a fixture that relied on them. An analyst override is preserved,
which is what makes the ordering here predictable.

Run:  python tests/test_filters_sorts.py
"""
import os, sys, json
from datetime import datetime, timedelta
os.environ["PROFILER_DATABASE_URI"]="sqlite:///:memory:"
os.environ["PROFILER_PASSWORD"]="qa"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import create_app
from app.extensions import db
from app.models import MonitorWatch, MonitorPost

app=create_app()
FAIL=0
def check(n,c,d=""):
    global FAIL
    print(("  PASS  " if c else "  FAIL  ")+n+((" :: "+str(d)) if d else "")); FAIL+=(not c)

with app.app_context():
    w=MonitorWatch(name="T", subject="NAMFREL"); db.session.add(w); db.session.flush()
    now=datetime.utcnow()
    # (label, minutes-ago-posted, minutes-ago-collected, score, engagement, author)
    rows=[("just-now",   20,   5, 10, 3,   "zeta"),
          ("two-hours", 120,  10, 90, 900, "alpha"),
          ("ten-hours", 600,  15, 50, 40,  "mid"),
          ("three-days",4320, 20, 70, 5,   "beta")]
    for label, pm, cm, sc, eng, author in rows:
        db.session.add(MonitorPost(
            watch_id=w.id, platform="Facebook", author=author, handle=author,
            text=label+" BARMM", url="", dedupe_key=label,
            posted_ts=now-timedelta(minutes=pm),
            collected_at=now-timedelta(minutes=cm),
            manual_score=sc, cached_score=sc, cached_verdict="warn", cached_relevant=True,
            engagement_json=json.dumps({"reactions":eng}), status="new"))
    db.session.commit()
    wid=w.id

c=app.test_client()
c.post("/login", data={"password":"qa"}, follow_redirects=True)
def get(**kw):
    qs="&".join("%s=%s"%(k,v) for k,v in kw.items())
    r=c.get("/monitor/%d/results?%s"%(wid,qs))
    assert r.status_code==200, r.status_code
    return [p["text"].split()[0] for p in r.get_json()["posts"]]

print("=== HOUR WINDOWS ===")
check("1h returns only the 20-min post", get(days="1h")==["just-now"], get(days="1h"))
check("3h adds the 2-hour post", set(get(days="3h"))=={"just-now","two-hours"}, get(days="3h"))
check("12h adds the 10-hour post", len(get(days="12h"))==3, get(days="12h"))
check("7 days returns all four", len(get(days="7"))==4, get(days="7"))
check("no window returns all four", len(get(days=""))==4)

print("\n=== COLLECTED-DATE WINDOW ===")
# Everything was collected within 20 minutes, so a 1h collected window is all 4
# even though only one was POSTED in the last hour. That is the distinction.
check("1h by collected date returns all four",
      len(get(days="1h", date_field="collected"))==4,
      get(days="1h", date_field="collected"))

print("\n=== CUSTOM RANGE ===")
lo=(datetime.utcnow()-timedelta(hours=11)).strftime("%Y-%m-%dT%H:%M")
hi=(datetime.utcnow()-timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M")
got=get(days="custom", **{"from":lo,"to":hi})
check("range isolates the middle window", set(got)=={"two-hours","ten-hours"}, got)
got=get(days="custom", **{"from":lo})
check("open-ended 'from' works", len(got)==3, got)
got=get(days="custom", **{"to":hi})
check("open-ended 'to' works", set(got)=={"two-hours","ten-hours","three-days"}, got)
got=get(days="custom", **{"from":hi,"to":lo})
check("reversed bounds are swapped, not empty", set(got)=={"two-hours","ten-hours"}, got)

print("\n=== SORTS, BOTH DIRECTIONS ===")
check("risk desc", get(sort="risk",dir="desc")[0]=="two-hours", get(sort="risk",dir="desc"))
check("risk asc",  get(sort="risk",dir="asc")[0]=="just-now",  get(sort="risk",dir="asc"))
check("posted desc", get(sort="posted",dir="desc")[0]=="just-now", get(sort="posted",dir="desc"))
check("posted asc",  get(sort="posted",dir="asc")[0]=="three-days", get(sort="posted",dir="asc"))
check("collected desc", get(sort="collected",dir="desc")[0]=="just-now", get(sort="collected",dir="desc"))
check("collected asc",  get(sort="collected",dir="asc")[0]=="three-days", get(sort="collected",dir="asc"))
check("author asc is A-Z (alpha first)", get(sort="author",dir="asc")[0]=="two-hours", get(sort="author",dir="asc"))
check("author desc is Z-A (zeta first)",  get(sort="author",dir="desc")[0]=="just-now", get(sort="author",dir="desc"))
check("engagement desc", get(sort="engagement",dir="desc")[0]=="two-hours", get(sort="engagement",dir="desc"))
check("engagement asc",  get(sort="engagement",dir="asc")[0]=="just-now",  get(sort="engagement",dir="asc"))

print("\n=== LEGACY KEYS STILL WORK ===")
check("legacy 'recent'", get(sort="recent")[0]=="just-now", get(sort="recent"))
check("legacy 'oldest'", get(sort="oldest")[0]=="three-days", get(sort="oldest"))

print("\n=== BAD INPUT IS IGNORED, NOT FATAL ===")
check("garbage sort falls back to risk", get(sort="nonsense")[0]=="two-hours")
check("garbage dir falls back to desc", get(sort="risk",dir="sideways")[0]=="two-hours")
check("garbage days = no filter", len(get(days="abc"))==4)
check("garbage range bounds ignored", len(get(days="custom", **{"from":"nope"}))==4)

print("\n"+"="*52); print("FAILURES:", FAIL)
sys.exit(1 if FAIL else 0)
