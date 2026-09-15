/* Browser-side system of record.
 *
 * All user data — watches, posts, profiles, link maps, settings — lives in
 * IndexedDB in this browser. The server is a stateless service: it fetches
 * from external sources and it scores text, but it keeps nothing.
 *
 * Why IndexedDB and not localStorage: posts run about 1.8 KB each once their
 * analysis is attached, so a few thousand of them blow past localStorage's
 * 5–10 MB cap. IndexedDB has room and, unlike localStorage, does not block the
 * main thread on every write.
 *
 * What this means for durability — say it plainly, because it bites people:
 *
 *   * Data is tied to THIS browser profile on THIS machine. A different
 *     browser, a different device, or a guest profile sees nothing.
 *   * "Clear browsing data" / "Clear site data" deletes all of it.
 *   * Private and incognito windows usually discard it when the window closes.
 *   * Browsers may evict storage under disk pressure unless the origin has
 *     been granted persistence, which `requestPersistence()` asks for.
 *
 * So: export regularly, and use the optional server sync if the data matters.
 * The UI surfaces all of this rather than letting anyone assume otherwise.
 */
(function (global) {
  'use strict';

  const DB_NAME = 'profiler';
  const DB_VERSION = 3;

  // store -> { keyPath, autoIncrement, indexes: [[name, keyPath, opts]] }
  const SCHEMA = {
    watches: {
      keyPath: 'id', autoIncrement: true,
      indexes: [['updated_at', 'updated_at', {}]],
    },
    posts: {
      keyPath: 'id', autoIncrement: true,
      indexes: [
        ['watch_id', 'watch_id', {}],
        // Compound indexes are what keep filtering and sorting off the main
        // thread: the cursor walks an index instead of scanning every row.
        ['watch_verdict', ['watch_id', 'verdict'], {}],
        ['watch_score', ['watch_id', 'score'], {}],
        ['watch_posted', ['watch_id', 'posted_ts'], {}],
        ['watch_status', ['watch_id', 'status'], {}],
        ['dedupe', 'dedupe_key', { unique: false }],
      ],
    },
    profiles: { keyPath: 'id', autoIncrement: true, indexes: [['codename', 'codename', {}]] },
    graphs: { keyPath: 'id', autoIncrement: true, indexes: [['updated_at', 'updated_at', {}]] },
    notes: { keyPath: 'id', autoIncrement: true, indexes: [['profile_id', 'profile_id', {}]] },
    feeds: { keyPath: 'id', autoIncrement: true, indexes: [['watch_id', 'watch_id', {}]] },
    // Profile-adjacent records, moved off the server so that nothing
    // user-generated lives outside this browser.
    tags: { keyPath: 'id', autoIncrement: true, indexes: [['name', 'name', { unique: false }]] },
    // Photos are held as Blobs. IndexedDB stores them natively, so there is no
    // base64 inflation and no file on any disk but this one.
    photos: { keyPath: 'id', autoIncrement: true, indexes: [] },
    dork_history: { keyPath: 'id', autoIncrement: true, indexes: [['used_at', 'used_at', {}]] },
    dork_favorites: { keyPath: 'id', autoIncrement: true, indexes: [] },
    dork_custom: { keyPath: 'id', autoIncrement: true, indexes: [['category', 'category', {}]] },
    osint_runs: { keyPath: 'id', autoIncrement: true, indexes: [
      ['run_id', 'run_id', {}], ['username', 'username', {}]] },
    settings: { keyPath: 'key', autoIncrement: false, indexes: [] },
    // Named filter presets, scoped to a watch. `watch_id` 0 means "any watch",
    // which is how a preset that only names a verdict or a date window can be
    // reused across watches.
    saved_filters: { keyPath: 'id', autoIncrement: true, indexes: [
      ['watch_id', 'watch_id', {}]] },
  };

  let dbPromise = null;

  /* Surface a storage failure on the page.
   *
   * A rejected open() used to fail silently into a console message, which left
   * every control rendered but inert -- indistinguishable from "the UI is
   * broken". A schema upgrade blocked by another tab is the common cause, and
   * it is entirely recoverable, so say so where the user will actually see it.
   */
  function banner(message, recoverable) {
    try {
      let el = document.getElementById('storeBanner');
      if (!el) {
        el = document.createElement('div');
        el.id = 'storeBanner';
        el.setAttribute('role', 'alert');
        el.style.cssText =
          'position:fixed;top:0;left:0;right:0;z-index:100000;padding:11px 16px;' +
          'background:#2a0d14;border-bottom:1px solid #ff4060;color:#ffd7de;' +
          'font:13px/1.5 Inter,system-ui,sans-serif;display:flex;gap:12px;' +
          'align-items:center;flex-wrap:wrap';
        (document.body || document.documentElement).appendChild(el);
      }
      el.innerHTML =
        '<span style="flex:1;min-width:220px"><b>Storage unavailable.</b> ' +
        message + '</span>' +
        (recoverable
          ? '<button type="button" id="storeRetry" style="background:#ff4060;' +
            'border:0;color:#fff;padding:5px 12px;border-radius:5px;' +
            'cursor:pointer;font-weight:600">Retry</button>'
          : '');
      const retry = document.getElementById('storeRetry');
      if (retry) retry.onclick = () => location.reload();
    } catch (e) { /* pre-DOM: the console message is all we have */ }
  }

  function open() {
    if (dbPromise) return dbPromise;
    dbPromise = new Promise((resolve, reject) => {
      if (!global.indexedDB) {
        const msg = 'This browser has no IndexedDB, so nothing can be saved. ' +
          'Private windows in some browsers block it.';
        banner(msg, false);
        reject(new Error(msg));
        return;
      }

      const req = indexedDB.open(DB_NAME, DB_VERSION);
      let blockedTimer = null;

      req.onupgradeneeded = (e) => {
        const db = req.result;
        Object.entries(SCHEMA).forEach(([name, spec]) => {
          let store;
          if (!db.objectStoreNames.contains(name)) {
            store = db.createObjectStore(name, {
              keyPath: spec.keyPath,
              autoIncrement: spec.autoIncrement,
            });
          } else {
            store = e.target.transaction.objectStore(name);
          }
          spec.indexes.forEach(([iname, keyPath, opts]) => {
            if (!store.indexNames.contains(iname)) store.createIndex(iname, keyPath, opts);
          });
        });
      };

      req.onsuccess = () => {
        if (blockedTimer) clearTimeout(blockedTimer);
        const el = document.getElementById('storeBanner');
        if (el) el.remove();
        // Another tab upgrading later must not find us holding the old version
        // open -- that is the deadlock this whole path exists to avoid.
        req.result.onversionchange = () => {
          req.result.close();
          dbPromise = null;
          banner('This app was updated in another tab. Reload to continue.', true);
        };
        resolve(req.result);
      };

      req.onerror = () => {
        const msg = (req.error && req.error.name === 'QuotaExceededError')
          ? 'The browser is out of storage for this site. Export a backup, then ' +
            'delete some posts or clear space.'
          : 'The database could not be opened' +
            (req.error ? ' (' + req.error.name + ')' : '') + '.';
        banner(msg, true);
        reject(req.error || new Error(msg));
      };

      // `blocked` is not fatal: the other tab may close at any moment and the
      // upgrade will then proceed on its own. So keep waiting, and only warn.
      req.onblocked = () => {
        blockedTimer = setTimeout(() => {
          banner('Another tab has an older version of this app open, which is ' +
                 'blocking an update. Close the other tabs, then retry.', true);
        }, 1200);
      };
    });
    return dbPromise;
  }

  function tx(stores, mode, fn) {
    return open().then((db) => new Promise((resolve, reject) => {
      const t = db.transaction(stores, mode);
      let result;
      t.oncomplete = () => resolve(result);
      t.onerror = () => reject(t.error);
      t.onabort = () => reject(t.error || new Error('Transaction aborted'));
      try {
        result = fn(t);
        // `fn` may return a promise-like for a single request's value.
        if (result && typeof result.then === 'function') {
          result.then((v) => { result = v; }, reject);
        }
      } catch (err) {
        try { t.abort(); } catch (e) { /* already aborting */ }
        reject(err);
      }
    }));
  }

  const wrap = (req) => new Promise((resolve, reject) => {
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });

  /* ── Generic CRUD ────────────────────────────────────────────────────── */

  const api = {
    async get(store, key) {
      const db = await open();
      return wrap(db.transaction(store, 'readonly').objectStore(store).get(key));
    },

    async all(store) {
      const db = await open();
      return wrap(db.transaction(store, 'readonly').objectStore(store).getAll());
    },

    async put(store, value) {
      const db = await open();
      const t = db.transaction(store, 'readwrite');
      const key = await wrap(t.objectStore(store).put(value));
      return new Promise((resolve, reject) => {
        t.oncomplete = () => resolve(key);
        t.onerror = () => reject(t.error);
      });
    },

    async putMany(store, values) {
      if (!values.length) return [];
      const db = await open();
      const t = db.transaction(store, 'readwrite');
      const os = t.objectStore(store);
      const keys = [];
      values.forEach((v) => {
        const r = os.put(v);
        r.onsuccess = () => keys.push(r.result);
      });
      return new Promise((resolve, reject) => {
        t.oncomplete = () => resolve(keys);
        t.onerror = () => reject(t.error);
      });
    },

    async remove(store, key) {
      const db = await open();
      const t = db.transaction(store, 'readwrite');
      t.objectStore(store).delete(key);
      return new Promise((resolve, reject) => {
        t.oncomplete = () => resolve(true);
        t.onerror = () => reject(t.error);
      });
    },

    async removeMany(store, keys) {
      if (!keys.length) return 0;
      const db = await open();
      const t = db.transaction(store, 'readwrite');
      const os = t.objectStore(store);
      keys.forEach((k) => os.delete(k));
      return new Promise((resolve, reject) => {
        t.oncomplete = () => resolve(keys.length);
        t.onerror = () => reject(t.error);
      });
    },

    async clear(store) {
      const db = await open();
      const t = db.transaction(store, 'readwrite');
      t.objectStore(store).clear();
      return new Promise((resolve, reject) => {
        t.oncomplete = () => resolve(true);
        t.onerror = () => reject(t.error);
      });
    },

    async byIndex(store, index, query) {
      const db = await open();
      return wrap(db.transaction(store, 'readonly')
        .objectStore(store).index(index).getAll(query));
    },

    async countBy(store, index, query) {
      const db = await open();
      return wrap(db.transaction(store, 'readonly')
        .objectStore(store).index(index).count(query));
    },

    /* ── Posts: the only store big enough to need care ──────────────────
       Filtering happens while walking a cursor, so a watch with tens of
       thousands of posts never materialises them all at once. */

    async queryPosts(watchId, opts) {
      const o = opts || {};
      const db = await open();
      const t = db.transaction('posts', 'readonly');
      const index = t.objectStore('posts').index('watch_id');

      const term = (o.q || '').toLowerCase();
      const wantTypes = (o.types || '').toLowerCase();

      // The time window, in whichever form the caller used:
      //
      //   days           a preset span, in hours ('1h', '6h') or days ('7')
      //   from / to      the toolbar's explicit range
      //   since / until  the same bounds, under the names other callers use
      //
      // All of it collapses to a pair of timestamps here, so the matcher only
      // ever deals with numbers. (`days` used to be dropped on the floor,
      // which made the date filter look applied but do nothing.)
      //
      // Bounds are LOCAL wall-clock: the analyst typed "22:30" meaning half
      // past ten where they are sitting, so they are parsed as local. Stored
      // post timestamps are the opposite -- UTC with the zone stripped -- and
      // go through `parseStored`. Confusing the two silently shifts everything
      // by the viewer's UTC offset.
      let since = Date.parse(o.since || o.from || '');
      let until = Date.parse(o.until || o.to || '');
      if (Number.isNaN(since)) since = null;
      if (Number.isNaN(until)) until = null;
      if (since === null && o.days !== 'custom') {
        const span = windowMs(o.days);
        if (span) since = Date.now() - span;
      }
      // A bound typed to the minute means that whole minute. Without this, a
      // range ending 22:30 drops a post published at 22:30:45, which reads as
      // the filter losing posts.
      if (until !== null && !/:\d{2}:\d{2}/.test(String(o.until || o.to || ''))) {
        until += 59999;
      }

      const match = (p) => {
        if (o.verdict && o.verdict !== 'all' && effVerdict(p) !== o.verdict) return false;
        if (o.status && p.status !== o.status) return false;
        // Posts collected before comments existed carry no `kind`; they are
        // posts, so treat a missing value as one rather than hiding them.
        if (o.kind && (p.kind || 'post') !== o.kind) return false;
        if (o.platform && p.platform !== o.platform) return false;
        if (o.pinned && !p.pinned) return false;
        if (o.profile_id && p.profile_id !== o.profile_id) return false;
        if (wantTypes && !(p.types || []).join(',').toLowerCase().includes(wantTypes)) return false;
        if (since || until) {
          // `date_field` chooses what the window applies to: when the post was
          // published (the default, which is what "last hour" usually means),
          // or when it was collected -- the right question after a sweep, when
          // you want what the last run brought in regardless of post age.
          const ts = dateOf(p, o.date_field === 'collected' ? 'collected' : 'posted');
          if (!ts) return false;
          if (since && ts < since) return false;
          if (until && ts > until) return false;
        }
        if (term) {
          const hay = (p.author + ' ' + p.handle + ' ' + p.text + ' ' +
            p.platform + ' ' + (p.types || []).join(' ') + ' ' + (p.status || '')).toLowerCase();
          if (!hay.includes(term)) return false;
        }
        return true;
      };

      const rows = [];
      await new Promise((resolve, reject) => {
        const req = index.openCursor(IDBKeyRange.only(watchId));
        req.onsuccess = () => {
          const cur = req.result;
          if (!cur) { resolve(); return; }
          if (match(cur.value)) rows.push(cur.value);
          cur.continue();
        };
        req.onerror = () => reject(req.error);
      });

      rows.sort(sorter(o.sort || 'risk', o.dir));

      const perPage = Math.max(1, o.per_page || 25);
      const pages = Math.max(1, Math.ceil(rows.length / perPage));
      const page = Math.min(Math.max(1, o.page || 1), pages);
      return {
        posts: rows.slice((page - 1) * perPage, page * perPage),
        matching: rows.length,
        page, pages, per_page: perPage,
        has_prev: page > 1, has_next: page < pages,
      };
    },

    async countsFor(watchId) {
      const db = await open();
      const t = db.transaction('posts', 'readonly');
      const counts = { bad: 0, warn: 0, ok: 0 };
      await new Promise((resolve, reject) => {
        const req = t.objectStore('posts').index('watch_id')
          .openCursor(IDBKeyRange.only(watchId));
        req.onsuccess = () => {
          const cur = req.result;
          if (!cur) { resolve(); return; }
          const v = effVerdict(cur.value);
          if (v in counts) counts[v] += 1;
          cur.continue();
        };
        req.onerror = () => reject(req.error);
      });
      return counts;
    },

    async platformsFor(watchId) {
      const rows = await api.byIndex('posts', 'watch_id', IDBKeyRange.only(watchId));
      const seen = {};
      rows.forEach((p) => {
        const k = p.platform || 'Other';
        seen[k] = (seen[k] || 0) + 1;
      });
      return Object.entries(seen)
        .map(([platform, count]) => ({ platform, count }))
        .sort((a, b) => b.count - a.count);
    },

    async deletePostsFor(watchId) {
      const db = await open();
      const t = db.transaction('posts', 'readwrite');
      const idx = t.objectStore('posts').index('watch_id');
      let n = 0;
      await new Promise((resolve, reject) => {
        const req = idx.openCursor(IDBKeyRange.only(watchId));
        req.onsuccess = () => {
          const cur = req.result;
          if (!cur) { resolve(); return; }
          cur.delete();
          n += 1;
          cur.continue();
        };
        req.onerror = () => reject(req.error);
      });
      return n;
    },

    async existingKeys(watchId) {
      const rows = await api.byIndex('posts', 'watch_id', IDBKeyRange.only(watchId));
      return new Set(rows.map((p) => p.dedupe_key).filter(Boolean));
    },

    /* ── Settings ──────────────────────────────────────────────────────── */

    async setting(key, fallback) {
      const row = await api.get('settings', key);
      return row === undefined ? fallback : row.value;
    },

    saveSetting(key, value) {
      return api.put('settings', { key, value });
    },

    /* ── Backup and restore ────────────────────────────────────────────── */

    async exportAll() {
      const out = {
        format: 'profiler-backup',
        version: DB_VERSION,
        exported_at: new Date().toISOString(),
        data: {},
      };
      for (const name of Object.keys(SCHEMA)) {
        out.data[name] = await api.all(name);
      }
      out.counts = Object.fromEntries(
        Object.entries(out.data).map(([k, v]) => [k, v.length]));
      return out;
    },

    async importAll(payload, opts) {
      const o = opts || {};
      if (!payload || payload.format !== 'profiler-backup') {
        throw new Error('That file is not a Profiler backup.');
      }
      const stats = {};
      for (const [name, rows] of Object.entries(payload.data || {})) {
        if (!SCHEMA[name] || !Array.isArray(rows)) continue;
        if (o.replace) await api.clear(name);
        // Ids are preserved so that watch_id and profile_id references inside
        // the payload keep pointing at the right rows.
        await api.putMany(name, rows);
        stats[name] = rows.length;
      }
      return stats;
    },

    /* ── Storage health ────────────────────────────────────────────────── */

    async usage() {
      if (!navigator.storage || !navigator.storage.estimate) {
        return { supported: false };
      }
      const est = await navigator.storage.estimate();
      const persisted = navigator.storage.persisted
        ? await navigator.storage.persisted() : false;
      return {
        supported: true,
        usage: est.usage || 0,
        quota: est.quota || 0,
        percent: est.quota ? (est.usage / est.quota) * 100 : 0,
        persisted,
      };
    },

    // Ask the browser not to evict this origin under disk pressure. Chrome
    // grants it silently on engaged sites; Firefox prompts. A refusal is not
    // an error — it just means eviction stays possible, which the UI says.
    async requestPersistence() {
      if (!navigator.storage || !navigator.storage.persist) return false;
      try {
        if (await navigator.storage.persisted()) return true;
        return await navigator.storage.persist();
      } catch (e) {
        return false;
      }
    },

    async stats() {
      const [watches, posts, profiles, graphs] = await Promise.all([
        api.all('watches'), api.all('posts'),
        api.all('profiles'), api.all('graphs'),
      ]);
      return {
        watches: watches.length, posts: posts.length,
        profiles: profiles.length, graphs: graphs.length,
      };
    },

    async wipe() {
      for (const name of Object.keys(SCHEMA)) await api.clear(name);
      return true;
    },

    /* ── Photos ────────────────────────────────────────────────────────
       Kept in their own store so a profile row stays small and cheap to
       query; the row holds only the photo id. */

    async savePhoto(blob) {
      return api.put('photos', { blob, type: blob.type || 'image/jpeg',
                                 size: blob.size,
                                 saved_at: new Date().toISOString().slice(0, 19) });
    },

    async photoUrl(id) {
      if (!id) return '';
      const row = await api.get('photos', id);
      if (!row || !row.blob) return '';
      return URL.createObjectURL(row.blob);
    },

    async deletePhoto(id) {
      if (!id) return false;
      return api.remove('photos', id);
    },

    _schema: SCHEMA,
    _open: open,
  };

  /* ── Helpers ─────────────────────────────────────────────────────────── */

  // An analyst's override wins over whatever the engine computed.
  function effVerdict(p) {
    return p.manual_verdict || p.verdict || 'ok';
  }
  function effScore(p) {
    return p.manual_score != null ? p.manual_score : (p.score || 0);
  }

  /* How long a preset time window is, in milliseconds.
   *
   * Accepts hours ('1h', '12h') and days ('7', '30', or '7d'). Returns 0 for
   * anything unrecognised, which the caller reads as "no window" -- a filter
   * that silently matched nothing would look like an empty watch.
   */
  function windowMs(value) {
    const raw = String(value == null ? '' : value).trim().toLowerCase();
    if (!raw) return 0;
    const m = raw.match(/^(\d+(?:\.\d+)?)\s*([hd]?)$/);
    if (!m) return 0;
    const n = parseFloat(m[1]);
    if (!(n > 0)) return 0;
    return m[2] === 'h' ? n * 3600000 : n * 86400000;
  }

  // Total engagement on a post, for the engagement sort. Absent counters are
  // zero rather than missing, so posts from sources that report none sink to
  // the bottom instead of scattering unpredictably.
  function engagementOf(p) {
    const e = p.engagement || {};
    return (Number(e.reactions) || 0) + (Number(e.comments) || 0) +
      (Number(e.shares) || 0);
  }

  // The name a post is grouped under when sorting by author.
  function authorKey(p) {
    return String(p.handle || p.author || '').toLowerCase();
  }

  /* Comparators written as strict DESCENDING order: highest score, newest
   * date, Z to A. `dir: 'asc'` reverses them.
   *
   * Writing every field the same way is what makes the direction toggle mean
   * one consistent thing. Whether a sort *starts* ascending or descending is a
   * separate question, and the toolbar decides it (see NATURAL_DIR) -- picking
   * "Author" opens A-Z, but "descending" here still means Z-A.
   */
  const SORT_FIELDS = {
    risk: (a, b) => effScore(b) - effScore(a),
    posted: (a, b) => dateOf(b, 'posted') - dateOf(a, 'posted'),
    collected: (a, b) => dateOf(b, 'collected') - dateOf(a, 'collected'),
    relevance: (a, b) => (b.relevance || 0) - (a.relevance || 0),
    engagement: (a, b) => engagementOf(b) - engagementOf(a),
    platform: (a, b) => String(b.platform || '').localeCompare(String(a.platform || '')),
    status: (a, b) => String(b.status || '').localeCompare(String(a.status || '')),
    author: (a, b) => authorKey(b).localeCompare(authorKey(a)),
  };

  /* Parse a stored timestamp, correctly, as UTC.
   *
   * Both sides write UTC with the zone stripped: the browser via
   * `toISOString().slice(0, 19)` and the server via `utcnow().isoformat()`.
   * `Date.parse` reads a zoneless date-time as LOCAL time, so every stored
   * stamp was being shifted by the viewer's UTC offset -- eight hours in
   * Manila. At day scale that was invisible; an "in the last hour" filter it
   * breaks outright, hiding posts that were collected minutes ago.
   *
   * So a bare `YYYY-MM-DDTHH:MM:SS` gets a 'Z'. Anything that already names a
   * zone, or is a date only, is left alone.
   */
  function parseStored(value) {
    const raw = String(value == null ? '' : value).trim();
    if (!raw) return 0;
    const bare = /^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?$/.test(raw);
    return Date.parse(bare ? raw.replace(' ', 'T') + 'Z' : raw) || 0;
  }

  function dateOf(p, field) {
    return field === 'collected'
      ? (parseStored(p.collected_at) || parseStored(p.posted_ts))
      : (parseStored(p.posted_ts) || parseStored(p.collected_at));
  }

  /* Build the comparator for a sort key and direction.
   *
   * `dir` is 'desc' (the key's natural order) or 'asc' (reversed). The legacy
   * keys `recent` and `oldest` still work: they were a sort and a direction
   * fused together, and saved presets and shared links from before the
   * direction toggle existed still carry them.
   */
  function sorter(kind, dir) {
    let key = kind;
    let direction = dir === 'asc' ? 'asc' : 'desc';
    if (kind === 'recent') { key = 'posted'; direction = dir ? direction : 'desc'; }
    if (kind === 'oldest') { key = 'posted'; direction = dir ? direction : 'asc'; }

    const base = SORT_FIELDS[key] || SORT_FIELDS.risk;
    const sign = direction === 'asc' ? -1 : 1;
    // Risk breaks every tie, so equal dates or shared authors still read
    // worst-first instead of in arbitrary insertion order.
    const tie = SORT_FIELDS.risk;
    const inner = (a, b) => (sign * base(a, b)) || (key === 'risk' ? 0 : tie(a, b));

    // Pinned posts stay on top of every ordering.
    return (a, b) => (Number(b.pinned || 0) - Number(a.pinned || 0)) || inner(a, b);
  }

  api.effVerdict = effVerdict;
  api.effScore = effScore;
  // Shared so the UI reads stored timestamps the same way the queries do --
  // two parsers would drift, and this one is the difference between "5 min
  // ago" and "8 h ago" for anyone not sitting on UTC.
  api.parseStored = parseStored;

  global.Store = api;
})(window);
