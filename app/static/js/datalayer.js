/* The bridge between browser storage and the stateless server.
 *
 * Store (IndexedDB) is the system of record. The server fetches from external
 * sources and runs the scoring engine, but keeps nothing — so every write goes
 * to IndexedDB, and the server is only ever asked to *compute*.
 *
 * The one rule worth internalising: a post's score belongs to a set of watch
 * rules. `rules_hash` travels with each post, and when the watch's rules change
 * the affected posts are re-sent for scoring. That is the same invalidation
 * idea the server-side cache used, moved to the client.
 */
(function (global) {
  'use strict';

  const API = '/monitor/api';
  const SCORE_BATCH = 200;   // server caps at 500; stay well under
  const ENRICH_BATCH = 50;

  async function post(path, body) {
    const res = await fetch(API + path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {}),
    });
    let data = {};
    try { data = await res.json(); } catch (e) { /* not JSON */ }
    if (!res.ok) throw new Error(data.error || ('Request failed (' + res.status + ')'));
    return data;
  }

  async function get(path) {
    const res = await fetch(API + path);
    let data = {};
    try { data = await res.json(); } catch (e) { /* not JSON */ }
    if (!res.ok) throw new Error(data.error || ('Request failed (' + res.status + ')'));
    return data;
  }

  const nowISO = () => new Date().toISOString().slice(0, 19);

  /* ── Watches ─────────────────────────────────────────────────────────── */

  const Data = {
    async watches() {
      const rows = await Store.all('watches');
      return rows.sort((a, b) => String(b.updated_at).localeCompare(String(a.updated_at)));
    },

    watch(id) { return Store.get('watches', id); },

    async createWatch(fields) {
      const w = Object.assign({
        name: 'Untitled watch', subject: '',
        kw_required: '', kw_optional: '', kw_excluded: '', kw_match_mode: 'any',
        mode_release: false, mode_hunter: false,
        reference_text: '', official_accounts: '', official_domains: '',
        custom_flags: '', threshold_review: 30, threshold_high: 60,
        weights: {}, profile_id: null,
        created_at: nowISO(), updated_at: nowISO(),
      }, fields || {});
      w.id = await Store.put('watches', w);
      return w;
    },

    async saveWatch(watch) {
      watch.updated_at = nowISO();
      await Store.put('watches', watch);
      return watch;
    },

    async deleteWatch(id) {
      await Store.deletePostsFor(id);
      const feeds = await Store.byIndex('feeds', 'watch_id', IDBKeyRange.only(id));
      await Store.removeMany('feeds', feeds.map((f) => f.id));
      return Store.remove('watches', id);
    },

    /* ── Scoring ───────────────────────────────────────────────────────── */

    // Hunter mode correlates against profiles, which live in the browser, so
    // they must travel with the watch definition.
    async watchPayload(watch) {
      const payload = Object.assign({}, watch);
      if (watch.mode_hunter) payload.profiles = await Store.all('profiles');
      return payload;
    },

    async scorePosts(watch, posts) {
      if (!posts.length) return [];
      const wp = await Data.watchPayload(watch);
      const out = [];
      for (let i = 0; i < posts.length; i += SCORE_BATCH) {
        const slice = posts.slice(i, i + SCORE_BATCH);
        const res = await post('/score', { watch: wp, posts: slice });
        slice.forEach((p, j) => {
          const r = res.results[j];
          out.push(Object.assign(p, {
            score: r.score, verdict: r.verdict, verdict_label: r.verdict_label,
            types: r.types, relevant: r.relevant, relevance: r.relevance,
            analysis: r.analysis, rules_hash: res.rules_hash,
          }));
        });
      }
      return out;
    },

    /** Rescore only the posts whose stored hash no longer matches the rules.
     *  Returns how many were recomputed. */
    async ensureFresh(watch, onProgress) {
      const wp = await Data.watchPayload(watch);
      const rows = await Store.byIndex('posts', 'watch_id', IDBKeyRange.only(watch.id));
      if (!rows.length) return 0;

      // Score one post to learn the current rules hash, then rescore only the
      // posts whose stored hash differs from it.
      const one = await post('/score', { watch: wp, posts: [rows[0]] });
      const current = one.rules_hash;
      const stale = rows.filter((p) => p.rules_hash !== current);
      if (!stale.length) return 0;

      let done = 0;
      for (let i = 0; i < stale.length; i += SCORE_BATCH) {
        const slice = stale.slice(i, i + SCORE_BATCH);
        const res = await post('/score', { watch: wp, posts: slice });
        slice.forEach((p, j) => {
          const r = res.results[j];
          Object.assign(p, {
            score: r.score, verdict: r.verdict, verdict_label: r.verdict_label,
            types: r.types, relevant: r.relevant, relevance: r.relevance,
            analysis: r.analysis, rules_hash: res.rules_hash,
          });
        });
        await Store.putMany('posts', slice);
        done += slice.length;
        if (onProgress) onProgress(done, stale.length);
      }
      return done;
    },

    /* ── Posts ─────────────────────────────────────────────────────────── */

    async results(watch, query) {
      await Data.ensureFresh(watch);
      const page = await Store.queryPosts(watch.id, query);
      const [counts, platforms] = await Promise.all([
        Store.countsFor(watch.id), Store.platformsFor(watch.id),
      ]);
      return Object.assign(page, {
        counts,
        total: counts.bad + counts.warn + counts.ok,
        platforms,
      });
    },

    // Every post matching a query, unpaged -- what an export of "the current
    // view" needs. Paging is a reading convenience; it should not silently
    // decide what lands in the file.
    async allMatching(watch, query) {
      await Data.ensureFresh(watch);
      const q = Object.assign({}, query, { page: 1, per_page: 1000000 });
      const page = await Store.queryPosts(watch.id, q);
      return page.posts;
    },

    async addPosts(watch, raw, opts) {
      // Strict by default, matching collection: a post that fails the watch's
      // own relevance rules does not belong in it, however it arrived.
      const o = Object.assign({ strict: true }, opts || {});
      const known = await Store.existingKeys(watch.id);
      const stats = { added: 0, skipped: 0, off_topic: 0, flagged: 0 };

      const candidates = [];
      raw.forEach((r) => {
        const author = String(r.author || '').trim();
        const text = String(r.text || '').trim();
        if (!author || !text) { stats.skipped += 1; return; }
        const key = r.dedupe_key || Data.dedupeKey(watch.id, r.platform, author, text, r.url);
        if (known.has(key)) { stats.skipped += 1; return; }
        known.add(key);
        candidates.push(Object.assign({
          watch_id: watch.id, platform: 'Other', handle: '', verified: false,
          url: '', source: o.source || 'manual', source_url: '',
          link_kind: 'direct', posted_at: null, status: 'new', pinned: false,
          manual_score: null, manual_verdict: null, analyst_note: '',
          profile_id: null,
        }, r, {
          author, text, dedupe_key: key,
          collected_at: nowISO(),
          posted_ts: r.posted_at || nowISO(),
        }));
      });

      if (!candidates.length) return stats;

      // Posts arriving from /api/collect are already scored; anything else
      // needs a scoring pass before it can be stored.
      const unscored = candidates.filter((p) => p.score === undefined);
      if (unscored.length) await Data.scorePosts(watch, unscored);

      const keep = candidates.filter((p) => {
        if (o.strict && p.relevant === false) { stats.off_topic += 1; return false; }
        if (p.verdict && p.verdict !== 'ok') stats.flagged += 1;
        return true;
      });

      await Store.putMany('posts', keep);
      stats.added = keep.length;
      if (keep.length) await Data.saveWatch(watch);
      return stats;
    },

    async updatePost(id, patch) {
      const p = await Store.get('posts', id);
      if (!p) throw new Error('That post is no longer in this browser.');
      Object.assign(p, patch);
      await Store.put('posts', p);
      return p;
    },

    deletePost(id) { return Store.remove('posts', id); },

    async bulk(watchId, ids, action, value) {
      if (action === 'delete') {
        await Store.removeMany('posts', ids);
        return ids.length;
      }
      const rows = [];
      for (const id of ids) {
        const p = await Store.get('posts', id);
        if (!p) continue;
        if (action === 'status') p.status = value;
        else if (action === 'link_profile') p.profile_id = value ? Number(value) : null;
        else if (action === 'clear_override') { p.manual_score = null; p.manual_verdict = null; }
        rows.push(p);
      }
      await Store.putMany('posts', rows);
      return rows.length;
    },

    // Mirrors the server's key so a post collected either way dedupes the same.
    dedupeKey(watchId, platform, author, text, url) {
      const raw = [watchId, (platform || '').toLowerCase(), (author || '').toLowerCase(),
        (url || '').toLowerCase(), (text || '').trim().toLowerCase().slice(0, 400)].join('|');
      // A non-cryptographic hash is enough for deduplication, and avoids
      // pulling in SubtleCrypto's async API on every candidate post.
      let h1 = 0x811c9dc5, h2 = 0x01000193;
      for (let i = 0; i < raw.length; i++) {
        const c = raw.charCodeAt(i);
        h1 = Math.imul(h1 ^ c, 0x01000193) >>> 0;
        h2 = Math.imul(h2 + c, 0x85ebca6b) >>> 0;
      }
      return (h1.toString(16).padStart(8, '0') + h2.toString(16).padStart(8, '0'));
    },

    /* ── Collection ────────────────────────────────────────────────────── */

    async collect(watch, sources, opts) {
      const o = opts || {};
      const known = Array.from(await Store.existingKeys(watch.id));
      const wp = await Data.watchPayload(watch);
      const res = await post('/collect', {
        watch: wp, sources, query: o.query || '', days: o.days || '',
        options: o.options || {}, strict: o.strict !== false,
        known_keys: known,
      });
      // The server already scored and de-duplicated; store the survivors.
      const rows = res.posts.map((p) => Object.assign(p, {
        watch_id: watch.id, rules_hash: res.rules_hash,
      }));
      await Store.putMany('posts', rows);
      if (rows.length) await Data.saveWatch(watch);
      return res;
    },

    /* ── Derived views ─────────────────────────────────────────────────── */

    async briefing(watch) {
      const posts = await Store.byIndex('posts', 'watch_id', IDBKeyRange.only(watch.id));
      return post('/briefing', { watch, posts });
    },

    async dashboard(days) {
      const [watches, posts, profiles] = await Promise.all([
        Store.all('watches'), Store.all('posts'), Store.all('profiles'),
      ]);
      return post('/dashboard', { watches, posts, profiles, days: days || 30 });
    },

    async keywords(watch, sample) {
      return post('/keywords', Object.assign({}, watch, { sample: sample || '' }));
    },

    async graph(watch, opts) {
      const o = opts || {};
      const posts = await Store.byIndex('posts', 'watch_id', IDBKeyRange.only(watch.id));
      const profiles = await Store.all('profiles');
      return post('/graph', {
        watch, posts, profile_records: profiles,
        min_score: o.min_score != null ? o.min_score : 30,
        domains: o.domains !== false, profiles: o.profiles !== false,
        geo: !!o.geo, existing: o.existing || null,
        verdicts: o.verdicts || ['bad', 'warn'],
      });
    },

    async network(watch, opts) {
      const o = opts || {};
      const posts = await Store.byIndex('posts', 'watch_id', IDBKeyRange.only(watch.id));
      const profiles = await Store.all('profiles');
      return post('/network', {
        watch, posts, profile_records: profiles,
        min_score: o.min_score || 0, verdicts: o.verdicts || ['bad', 'warn'],
      });
    },

    /** Network enrichment for flagged posts. Results are written back onto the
     *  posts so the work is not repeated on the next page load. */
    async enrich(watchId, opts) {
      const o = opts || {};
      let rows = await Store.byIndex('posts', 'watch_id', IDBKeyRange.only(watchId));
      rows = rows.filter((p) => Store.effVerdict(p) !== 'ok')
        .filter((p) => o.force || !p.netintel)
        .slice(0, o.limit || 60);
      if (!rows.length) return { checked: 0, results: [] };

      const results = [];
      for (let i = 0; i < rows.length; i += ENRICH_BATCH) {
        const slice = rows.slice(i, i + ENRICH_BATCH);
        const res = await post('/enrich', {
          posts: slice.map((p) => ({ id: p.id, text: p.text, url: p.url })),
          geo: o.geo !== false, phishing: o.phishing !== false,
        });
        res.results.forEach((r, j) => {
          slice[j].netintel = r.findings;
          results.push(Object.assign({ post: slice[j] }, r));
        });
        await Store.putMany('posts', slice);
      }
      return { checked: rows.length, results };
    },

    /* ── Backup and sync ───────────────────────────────────────────────── */

    exportAll() { return Store.exportAll(); },

    async downloadBackup() {
      const data = await Store.exportAll();
      const blob = new Blob([JSON.stringify(data, null, 2)],
        { type: 'application/json' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = 'profiler-backup-' + new Date().toISOString().slice(0, 10) + '.json';
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
      return data.counts;
    },

    importBackup(payload, replace) {
      return Store.importAll(payload, { replace });
    },

    async pushToServer() {
      const payload = await Store.exportAll();
      return post('/sync/push', { payload, replace: true });
    },

    async pullFromServer(replace) {
      const payload = await get('/sync/pull');
      return Store.importAll(payload, { replace: replace !== false });
    },

    storage() { return Store.usage(); },
    requestPersistence() { return Store.requestPersistence(); },
    stats() { return Store.stats(); },
  };


  /* ── Profiles ─────────────────────────────────────────────────────────
     Moved into the browser alongside everything else. Digital Hunter
     correlates watches against these, so leaving them on the server would
     have meant a browser-stored watch depending on server-stored identities
     — exactly the split this storage model exists to avoid. */

  Object.assign(Data, {
    async profiles() {
      const rows = await Store.all('profiles');
      return rows.sort((a, b) => String(a.codename).localeCompare(String(b.codename)));
    },

    profile(id) { return Store.get('profiles', id); },

    async saveProfile(fields) {
      const p = Object.assign({
        codename: '', real_name: '', dob: '', nationality: '', occupation: '',
        physical_desc: '', known_aliases: [], photo_id: null, bio_notes: '',
        tag_ids: [], social_links: [],
        radar: {
          labels: ['Academics', 'Physical', 'Social', 'Influence', 'Threat', 'Digital'],
          scores: [0, 0, 0, 0, 0, 0],
        },
        created_at: nowISO(),
      }, fields || {});

      // An autoincrement store generates the key only when the property is
      // absent. An explicit `undefined` is a value, and IndexedDB rejects it.
      if (p.id === undefined || p.id === null) delete p.id;
      if (!p.created_at) p.created_at = nowISO();
      p.updated_at = nowISO();
      if (!String(p.codename).trim()) throw new Error('A codename is required.');

      // Codenames identify a profile across watches, so a duplicate would make
      // Hunter matches ambiguous.
      const clash = (await Store.all('profiles')).find((x) =>
        x.id !== p.id &&
        String(x.codename).toLowerCase() === String(p.codename).toLowerCase());
      if (clash) throw new Error('A profile with that codename already exists.');

      p.id = await Store.put('profiles', p);
      return p;
    },

    async deleteProfile(id) {
      const p = await Store.get('profiles', id);
      if (p && p.photo_id) await Store.deletePhoto(p.photo_id);

      // Notes belong to the profile and go with it.
      const notes = await Store.byIndex('notes', 'profile_id', IDBKeyRange.only(id));
      await Store.removeMany('notes', notes.map((n) => n.id));

      // Posts keep their text but lose the link, rather than being deleted:
      // the evidence is still evidence.
      const posts = (await Store.all('posts')).filter((x) => x.profile_id === id);
      if (posts.length) {
        posts.forEach((x) => { x.profile_id = null; });
        await Store.putMany('posts', posts);
      }
      return Store.remove('profiles', id);
    },

    async setProfilePhoto(profileId, file) {
      if (!file) return null;
      if (!/^image\//.test(file.type || '')) {
        throw new Error('That file is not an image.');
      }
      if (file.size > 8 * 1024 * 1024) {
        throw new Error('Images must be under 8 MB.');
      }
      const p = await Store.get('profiles', profileId);
      if (!p) throw new Error('Profile not found in this browser.');
      if (p.photo_id) await Store.deletePhoto(p.photo_id);
      p.photo_id = await Store.savePhoto(file);
      p.updated_at = nowISO();
      await Store.put('profiles', p);
      return p.photo_id;
    },

    photoUrl(id) { return Store.photoUrl(id); },

    /* ── Intel notes ───────────────────────────────────────────────────── */

    async notes(profileId) {
      const rows = await Store.byIndex('notes', 'profile_id', IDBKeyRange.only(profileId));
      // Notes written in the same second tie on timestamp; id is insertion order.
      return rows.sort((a, b) =>
        String(b.created_at).localeCompare(String(a.created_at)) || (b.id - a.id));
    },

    async addNote(profileId, content, source) {
      if (!String(content || '').trim()) throw new Error('The note is empty.');
      const id = await Store.put('notes', {
        profile_id: profileId, content: String(content).trim(),
        source: source || '', created_at: nowISO(),
      });
      return Store.get('notes', id);
    },

    deleteNote(id) { return Store.remove('notes', id); },

    /* ── Tags ──────────────────────────────────────────────────────────── */

    tags() { return Store.all('tags'); },

    async addTag(name, color) {
      name = String(name || '').trim();
      if (!name) throw new Error('The tag needs a name.');
      const existing = (await Store.all('tags')).find((t) =>
        String(t.name).toLowerCase() === name.toLowerCase());
      if (existing) return existing;
      const id = await Store.put('tags', { name, color: color || '#00ff99' });
      return Store.get('tags', id);
    },

    async deleteTag(id) {
      // Drop the tag from every profile that carries it, so no profile is left
      // pointing at an id that no longer resolves.
      const profiles = await Store.all('profiles');
      const touched = profiles.filter((p) => (p.tag_ids || []).includes(id));
      touched.forEach((p) => { p.tag_ids = p.tag_ids.filter((t) => t !== id); });
      if (touched.length) await Store.putMany('profiles', touched);
      return Store.remove('tags', id);
    },

    /* ── Dork engine ───────────────────────────────────────────────────── */

    async dorkHistory(limit) {
      const rows = await Store.all('dork_history');
      // Timestamps are second-resolution, so two searches a moment apart tie.
      // The autoincrement id is the real insertion order, and breaks the tie.
      rows.sort((a, b) =>
        String(b.used_at).localeCompare(String(a.used_at)) || (b.id - a.id));
      return rows.slice(0, limit || 50);
    },

    async recordDork(query, templateId, profileId) {
      await Store.put('dork_history', {
        query, template_id: templateId || null, profile_id: profileId || null,
        used_at: nowISO(),
      });
      // History is a convenience, not a record; keep it from growing forever.
      const rows = await Store.all('dork_history');
      if (rows.length > 300) {
        rows.sort((a, b) => String(a.used_at).localeCompare(String(b.used_at)));
        await Store.removeMany('dork_history',
          rows.slice(0, rows.length - 300).map((r) => r.id));
      }
    },

    dorkFavorites() { return Store.all('dork_favorites'); },

    async saveDorkFavorite(query, label) {
      if (!String(query || '').trim()) throw new Error('Nothing to save.');
      const id = await Store.put('dork_favorites', {
        query, label: label || '', saved_at: nowISO(),
      });
      return Store.get('dork_favorites', id);
    },

    deleteDorkFavorite(id) { return Store.remove('dork_favorites', id); },

    customDorks() { return Store.all('dork_custom'); },

    async saveCustomDork(fields) {
      const t = Object.assign({ category: 'Custom', name: '', template: '',
                                description: '' }, fields || {});
      if (!t.name || !t.template) throw new Error('Name and template are required.');
      t.id = await Store.put('dork_custom', t);
      return t;
    },

    deleteCustomDork(id) { return Store.remove('dork_custom', id); },

    /* ── Username OSINT ────────────────────────────────────────────────── */

    async saveOsintRun(runId, username, results) {
      const rows = results.map((r) => Object.assign({}, r, {
        run_id: runId, username, checked_at: nowISO(),
      }));
      await Store.putMany('osint_runs', rows);
      return rows.length;
    },

    async osintHistory(limit) {
      const rows = await Store.all('osint_runs');
      // Group by run so the history reads as "searches", not raw rows.
      const runs = {};
      rows.forEach((r) => {
        const e = runs[r.run_id] || (runs[r.run_id] = {
          run_id: r.run_id, username: r.username, checked_at: r.checked_at,
          found: 0, total: 0,
        });
        e.total += 1;
        if (r.status === 'found') e.found += 1;
        if (String(r.checked_at) > String(e.checked_at)) e.checked_at = r.checked_at;
      });
      return Object.values(runs)
        .sort((a, b) => String(b.checked_at).localeCompare(String(a.checked_at)))
        .slice(0, limit || 25);
    },

    osintRun(runId) {
      return Store.byIndex('osint_runs', 'run_id', IDBKeyRange.only(runId));
    },

    /** Attach found usernames to a profile as social links. */
    async osintToProfile(profileId, results) {
      const p = await Store.get('profiles', profileId);
      if (!p) throw new Error('Profile not found in this browser.');
      const have = new Set((p.social_links || []).map((l) =>
        (l.platform + '|' + l.username).toLowerCase()));
      let added = 0;
      (results || []).filter((r) => r.status === 'found').forEach((r) => {
        const key = (r.platform + '|' + r.username).toLowerCase();
        if (have.has(key)) return;
        have.add(key);
        (p.social_links = p.social_links || []).push({
          platform: r.platform, username: r.username, url: r.url,
        });
        added += 1;
      });
      if (added) {
        p.updated_at = nowISO();
        await Store.put('profiles', p);
      }
      return added;
    },
  });

  global.Data = Data;
})(window);
