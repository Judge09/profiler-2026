"""Browser tests for profiles, tags, notes, photos, dorks and OSINT runs.

These cover the records that moved out of SQLite and into IndexedDB, including
the cascade when a profile is deleted and the fact that Digital Hunter now
correlates against browser-stored profiles.

Needs Playwright. Run: python tests/test_browser_profiles.py
"""
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from werkzeug.serving import make_server  # noqa: E402

from app import create_app  # noqa: E402

PORT = 5094
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

    page.goto(BASE + "/login")
    page.fill("input[type=password]", "profiler2024")
    page.click("button[type=submit], input[type=submit]")
    page.wait_for_load_state("networkidle")

    print("=== PAGES LOAD WITHOUT JS ERRORS ===")
    for path in ["/profiles/", "/profiles/new", "/dorks/", "/osint/",
                 "/monitor/", "/monitor/dashboard", "/linkmap/"]:
        errors.clear()
        page.goto(BASE + path)
        page.wait_for_load_state("networkidle")
        real = [e for e in errors if "favicon" not in e.lower()]
        check("no JS errors on %s" % path, not real, "; ".join(real[:2]))

    page.goto(BASE + "/profiles/")
    page.wait_for_load_state("networkidle")

    print("\n=== SCHEMA v2 ===")
    r = page.evaluate("""async () => {
        await Store.wipe();
        const db = await Store._open();
        return Array.from(db.objectStoreNames);
    }""")
    for want in ["profiles", "notes", "tags", "photos", "dork_history",
                 "dork_favorites", "dork_custom", "osint_runs"]:
        check("store '%s' exists" % want, want in r)

    print("\n=== PROFILES CRUD ===")
    r = page.evaluate("""async () => {
        const p = await Data.saveProfile({
            codename:'NIGHTJAR', real_name:'Juan Dela Cruz',
            known_aliases:['jdc','nightjar_ph'],
            social_links:[{platform:'X', username:'nightjar_ph', url:'https://x.com/nightjar_ph'}],
            radar:{labels:['A','B','C','D','Threat','F'], scores:[10,20,30,40,77,60]}});
        const back = await Data.profile(p.id);
        return {id:p.id, codename:back.codename, aliases:back.known_aliases,
                threat:back.radar.scores[4]};
    }""")
    check("profile saved and read back", r["codename"] == "NIGHTJAR", r)
    check("aliases stored", r["aliases"] == ["jdc", "nightjar_ph"], r["aliases"])
    PID = r["id"]

    r = page.evaluate("""async () => {
        try {
            await Data.saveProfile({codename:'NIGHTJAR'});
            return 'allowed';
        } catch (e) { return e.message; }
    }""")
    check("duplicate codename rejected", "already exists" in r, r)

    r = page.evaluate("""async () => {
        try { await Data.saveProfile({codename:'   '}); return 'allowed'; }
        catch (e) { return e.message; }
    }""")
    check("empty codename rejected", "codename" in r.lower(), r)

    print("\n=== TAGS ===")
    r = page.evaluate("""async (pid) => {
        const t1 = await Data.addTag('HVT', '#ff4060');
        const dup = await Data.addTag('hvt', '#00ff99');
        const p = await Data.profile(pid);
        p.tag_ids = [t1.id];
        await Data.saveProfile(p);
        const tags = await Data.tags();
        return {same: t1.id === dup.id, count: tags.length,
                onProfile: (await Data.profile(pid)).tag_ids};
    }""", PID)
    check("tag created", r["count"] == 1, r)
    check("duplicate tag name reuses the tag", r["same"], r)
    check("tag attached to profile", len(r["onProfile"]) == 1, r["onProfile"])

    r = page.evaluate("""async (pid) => {
        const tags = await Data.tags();
        await Data.deleteTag(tags[0].id);
        return (await Data.profile(pid)).tag_ids;
    }""", PID)
    check("deleting a tag detaches it from profiles", r == [], r)

    print("\n=== NOTES ===")
    r = page.evaluate("""async (pid) => {
        await Data.addNote(pid, 'First observation', 'OSINT');
        await Data.addNote(pid, 'Second observation', 'Manual');
        const notes = await Data.notes(pid);
        return {n: notes.length, newestFirst: notes[0].content};
    }""", PID)
    check("notes stored", r["n"] == 2, r)

    r = page.evaluate("""async (pid) => {
        try { await Data.addNote(pid, '   '); return 'allowed'; }
        catch (e) { return e.message; }
    }""", PID)
    check("empty note rejected", "empty" in r.lower(), r)

    print("\n=== PHOTOS ===")
    r = page.evaluate("""async (pid) => {
        // A 1x1 PNG is enough to prove the Blob round-trip.
        const b64 = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==';
        const bin = atob(b64);
        const bytes = new Uint8Array(bin.length);
        for (let i=0;i<bin.length;i++) bytes[i] = bin.charCodeAt(i);
        const file = new File([bytes], 'x.png', {type:'image/png'});
        const photoId = await Data.setProfilePhoto(pid, file);
        const url = await Data.photoUrl(photoId);
        const p = await Data.profile(pid);
        return {photoId, hasUrl: url.startsWith('blob:'), onProfile: p.photo_id};
    }""", PID)
    check("photo stored as a blob", r["hasUrl"], r)
    check("photo linked to profile", r["onProfile"] == r["photoId"], r)

    r = page.evaluate("""async (pid) => {
        const big = new File([new Uint8Array(9*1024*1024)], 'big.png', {type:'image/png'});
        try { await Data.setProfilePhoto(pid, big); return 'allowed'; }
        catch (e) { return e.message; }
    }""", PID)
    check("oversized photo rejected", "8 MB" in r, r)

    r = page.evaluate("""async (pid) => {
        const txt = new File(['hello'], 'x.txt', {type:'text/plain'});
        try { await Data.setProfilePhoto(pid, txt); return 'allowed'; }
        catch (e) { return e.message; }
    }""", PID)
    check("non-image rejected", "not an image" in r, r)

    print("\n=== HUNTER USES BROWSER PROFILES ===")
    r = page.evaluate("""async () => {
        const w = await Data.createWatch({
            name:'Hunter', subject:'ACME', kw_required:'ACME',
            kw_match_mode:'any', mode_hunter:true});
        const scored = await Data.scorePosts(w, [
            {author:'nightjar_ph', handle:'nightjar_ph', platform:'X',
             text:'ACME statement from a tracked account'}]);
        const m = (scored[0].analysis || {}).profile_matches || [];
        return {wid:w.id, matches:m.length, codename:m[0] && m[0].codename};
    }""")
    check("hunter correlates against browser-stored profiles",
          r["matches"] > 0 and r["codename"] == "NIGHTJAR", r)
    WID = r["wid"]

    print("\n=== DORK HISTORY / FAVOURITES ===")
    r = page.evaluate("""async () => {
        await Data.recordDork('site:example.com "acme"', null, null);
        await Data.recordDork('inurl:admin acme', null, null);
        const hist = await Data.dorkHistory(10);
        const fav = await Data.saveDorkFavorite('site:example.com "acme"', 'ACME site');
        const favs = await Data.dorkFavorites();
        return {hist: hist.length, newest: hist[0].query,
                favs: favs.length, label: favs[0].label};
    }""")
    check("dork history recorded", r["hist"] == 2, r)
    check("history is newest-first", "inurl:admin" in r["newest"], r["newest"])
    check("favourite saved", r["favs"] == 1 and r["label"] == "ACME site", r)

    r = page.evaluate("""async () => {
        const t = await Data.saveCustomDork({name:'My dork', template:'site:{domain} {term}',
                                             category:'Custom'});
        const all = await Data.customDorks();
        return {n: all.length, name: all[0].name, id: t.id};
    }""")
    check("custom template saved", r["n"] == 1 and r["name"] == "My dork", r)

    print("\n=== OSINT RUNS ===")
    r = page.evaluate("""async (pid) => {
        const results = [
          {platform:'GitHub', username:'nightjar_ph', url:'https://github.com/nightjar_ph', status:'found'},
          {platform:'Reddit', username:'nightjar_ph', url:'https://reddit.com/u/nightjar_ph', status:'found'},
          {platform:'Nowhere', username:'nightjar_ph', url:'', status:'not_found'},
        ];
        await Data.saveOsintRun('run-1', 'nightjar_ph', results);
        const hist = await Data.osintHistory(5);
        const added = await Data.osintToProfile(pid, results);
        const again = await Data.osintToProfile(pid, results);
        const p = await Data.profile(pid);
        return {runs: hist.length, found: hist[0].found, total: hist[0].total,
                added, again, links: p.social_links.length};
    }""", PID)
    check("osint run stored", r["runs"] == 1 and r["found"] == 2 and r["total"] == 3, r)
    check("found accounts added to profile", r["added"] == 2, r)
    check("re-adding is idempotent", r["again"] == 0, r)
    check("profile now carries the links", r["links"] == 3, r["links"])

    print("\n=== PROFILE DELETE CASCADES ===")
    r = page.evaluate("""async (pid) => {
        const w = (await Data.watches())[0];
        await Data.addPosts(w, [{author:'x', text:'ACME test post'}], {});
        const posts = await Store.all('posts');
        if (posts.length) { posts[0].profile_id = pid; await Store.put('posts', posts[0]); }

        const before = {notes: (await Data.notes(pid)).length,
                        photos: (await Store.all('photos')).length};
        await Data.deleteProfile(pid);
        const after = {profile: await Data.profile(pid),
                       notes: (await Data.notes(pid)).length,
                       photos: (await Store.all('photos')).length,
                       orphanPosts: (await Store.all('posts')).filter(p=>p.profile_id===pid).length,
                       postsKept: (await Store.all('posts')).length};
        return {before, after};
    }""", PID)
    check("profile removed", r["after"]["profile"] is None)
    check("its notes removed", r["after"]["notes"] == 0, r["after"]["notes"])
    check("its photo removed", r["after"]["photos"] == 0, r["after"]["photos"])
    check("linked posts kept but unlinked",
          r["after"]["orphanPosts"] == 0 and r["after"]["postsKept"] > 0, r["after"])

    print("\n=== BACKUP INCLUDES THE NEW STORES ===")
    r = page.evaluate("""async () => {
        await Data.saveProfile({codename:'BACKUPTEST'});
        await Data.addTag('T1', '#fff');
        const backup = await Store.exportAll();
        await Store.wipe();
        const empty = await Store.stats();
        await Store.importAll(backup, {replace:true});
        const after = await Store.stats();
        return {keys: Object.keys(backup.data), counts: backup.counts,
                empty: empty.profiles, after: after.profiles,
                tags: (await Data.tags()).length};
    }""")
    check("backup covers new stores",
          all(k in r["keys"] for k in ["profiles", "tags", "notes", "photos",
                                       "dork_history", "osint_runs"]), r["keys"])
    check("restore brings profiles back", r["empty"] == 0 and r["after"] >= 1, r)
    check("restore brings tags back", r["tags"] >= 1, r["tags"])

    print("\n=== V1 BACKUP STILL IMPORTS ===")
    r = page.evaluate("""async () => {
        const v1 = {format:'profiler-backup', version:1, data:{
            watches:[{id:99, name:'Old watch', kw_required:'x'}],
            posts:[{id:991, watch_id:99, author:'a', text:'t', verdict:'ok', score:5}]}};
        await Store.wipe();
        const stats = await Store.importAll(v1, {replace:true});
        return {stats, watches:(await Store.all('watches')).length,
                posts:(await Store.all('posts')).length};
    }""")
    check("older backup imports cleanly", r["watches"] == 1 and r["posts"] == 1, r)

    real = [e for e in errors if "favicon" not in e.lower()]
    check("no uncaught JS errors overall", not real, "; ".join(real[:2]))

    browser.close()

server.shutdown()
print("\n" + "=" * 62)
print("FAILURES: %d" % len(FAIL))
for f in FAIL:
    print("  -", f)
sys.exit(1 if FAIL else 0)
