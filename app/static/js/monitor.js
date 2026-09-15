/* Signal Monitor — watch workspace.
 *
 * Scoring happens server-side and is cached there; this file renders results,
 * handles triage, and drives collection.
 *
 * Filtering, searching, sorting and paging are all server-side too: `state`
 * holds the query, `fetchPage()` asks for one page, and `render()` paints it.
 * That keeps a watch with thousands of posts as responsive as an empty one.
 */
(function () {
  'use strict';

  const WATCH = window.MONITOR.watchId;
  const STATUSES = window.MONITOR.statuses;
  const DEFAULT_WEIGHTS = window.MONITOR.defaultWeights;

  // Filled from IndexedDB during boot(). Nothing renders before that.
  let watch = null;
  let PROFILES = [];

  const $ = (s) => document.querySelector(s);
  const $$ = (s) => Array.from(document.querySelectorAll(s));
  const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g,
    (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const clamp = (v, lo = 0, hi = 100) => Math.max(lo, Math.min(hi, v));
  const debounce = (fn, ms) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; };

  const VERD = {
    bad: { label: 'High risk', color: 'var(--red)' },
    warn: { label: 'Needs review', color: 'var(--yellow)' },
    ok: { label: 'Likely authentic', color: 'var(--green)' },
  };

  /* ── Filter model ──────────────────────────────────────────────────────
     One place defines what a filter *is*, so retention, the URL, presets and
     export all agree on the same vocabulary. Anything not in DEFAULTS is not
     a filter and is never persisted. */

  // Sort keys name a field only; direction is separate, so every one of these
  // works both ways. `recent` and `oldest` are the old fused sort+direction
  // keys, kept so saved presets and shared links from before the toggle keep
  // working -- normalizeQuery rewrites them into a key plus a direction.
  const SORTS = ['risk', 'posted', 'collected', 'engagement', 'author',
                 'relevance', 'platform', 'status'];
  const LEGACY_SORTS = { recent: ['posted', 'desc'], oldest: ['posted', 'asc'] };
  // The direction each sort gets when it is first chosen. Scores and dates
  // read highest/newest first; names read A-Z.
  const NATURAL_DIR = {
    risk: 'desc', posted: 'desc', collected: 'desc', engagement: 'desc',
    relevance: 'desc', author: 'asc', platform: 'asc', status: 'asc',
  };
  // Preset time windows the date menu offers. Hours carry an 'h'; bare numbers
  // are days. 'custom' hands over to the From/To inputs.
  const DAY_CHOICES = ['1h', '3h', '6h', '12h', '1', '3', '7', '30', '90'];
  const DEFAULT_PER_PAGE = window.MONITOR.pageSize || 25;
  // The configured PAGE_SIZE is accepted even when it is not one of the menu
  // choices, so a deployment that sets its own default is not normalised away.
  const PER_PAGE_CHOICES = [10, 25, 50, 100].concat(
    [10, 25, 50, 100].includes(DEFAULT_PER_PAGE) ? [] : [DEFAULT_PER_PAGE]);
  const FILTER_DEFAULTS = {
    verdict: 'all',
    status: '',
    // '' is both, 'post' or 'comment' narrows to one. Listing it here is what
    // carries it through the URL, saved presets and Reset with no extra code.
    kind: '',
    platform: '',
    days: '',
    // An explicit window, used when `days` is 'custom'. Both ends are
    // optional: one alone is an open-ended range.
    from: '',
    to: '',
    // Which timestamp the window applies to: '' is the post's own date,
    // 'collected' is when this browser collected it.
    date_field: '',
    q: '',
    types: '',
    pinned: false,
    sort: 'risk',
    dir: 'desc',
    per_page: DEFAULT_PER_PAGE,
  };
  // Page is positional, not a filter: it is retained in the URL so a reload
  // lands where you were, but it is never part of a saved preset.
  const FILTER_KEYS = Object.keys(FILTER_DEFAULTS);
  const LAST_FILTER_KEY = 'monitor:filters:' + WATCH;

  function defaultQuery() {
    return Object.assign({ page: 1 }, FILTER_DEFAULTS);
  }

  /* Normalise a `datetime-local` value, or '' when it is not one.
   *
   * These are local wall-clock strings with no zone ('2026-09-14T22:30'),
   * which is what the analyst typed and what the store compares against. They
   * are validated rather than trusted because they also arrive from the URL,
   * where anything at all can be typed. Seconds are accepted (some browsers
   * add them) and kept, so a range is not silently widened to the minute.
   */
  function localStamp(value) {
    const raw = String(value == null ? '' : value).trim();
    if (!raw) return '';
    if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?$/.test(raw)) return '';
    return Number.isNaN(Date.parse(raw)) ? '' : raw;
  }

  // "14 Sep 22:30" — a range bound in the chips and preset descriptions,
  // where the full ISO string is noise.
  function shortStamp(value) {
    const t = Date.parse(value || '');
    if (Number.isNaN(t)) return String(value || '');
    const d = new Date(t);
    return d.toLocaleString(undefined, {
      day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit',
    });
  }

  // How the date menu's options read, for chips and preset descriptions.
  function windowLabel(v) {
    const m = String(v || '').match(/^(\d+)h$/);
    if (m) return m[1] === '1' ? 'Last hour' : 'Last ' + m[1] + ' hours';
    if (v === '1') return 'Last 24 hours';
    return 'Last ' + v + ' days';
  }

  // Coerce anything (URL string, stored JSON, preset) into a valid query.
  // Unknown keys and out-of-range values are dropped rather than trusted.
  function normalizeQuery(raw) {
    const o = raw || {};
    const out = defaultQuery();
    if (['all', 'bad', 'warn', 'ok'].includes(o.verdict)) out.verdict = o.verdict;
    if (STATUSES.includes(o.status)) out.status = o.status;
    if (['post', 'comment'].includes(o.kind)) out.kind = o.kind;
    if (o.platform) out.platform = String(o.platform);
    if (DAY_CHOICES.includes(String(o.days))) out.days = String(o.days);
    if (String(o.days) === 'custom') out.days = 'custom';
    if (o.date_field === 'collected') out.date_field = 'collected';

    // A custom range is only meaningful with at least one bound, and only
    // while 'custom' is selected -- otherwise a stale range left in a URL
    // would keep narrowing a preset window invisibly.
    if (out.days === 'custom') {
      out.from = localStamp(o.from);
      out.to = localStamp(o.to);
      // Reversed bounds are a slip, not an empty result: swap them rather
      // than showing nothing and leaving the analyst to work out why.
      if (out.from && out.to && out.from > out.to) {
        const swap = out.from; out.from = out.to; out.to = swap;
      }
      // 'custom' with no bounds yet is a legitimate state -- it is what
      // picking "Custom range…" means before anything has been typed, and it
      // is what keeps the From/To row on screen. It filters nothing until a
      // bound is set, which `activeFilters` reflects by not listing it.
    }

    if (o.q) out.q = String(o.q).slice(0, 200);
    if (o.types) out.types = String(o.types).slice(0, 80);
    out.pinned = o.pinned === true || o.pinned === 'true' || o.pinned === '1';

    if (SORTS.includes(o.sort)) {
      out.sort = o.sort;
      if (o.dir === 'asc' || o.dir === 'desc') out.dir = o.dir;
    } else if (LEGACY_SORTS[o.sort]) {
      // An old link or preset: unpack the fused key into key + direction.
      [out.sort, out.dir] = LEGACY_SORTS[o.sort];
    }
    const pp = parseInt(o.per_page, 10);
    if (PER_PAGE_CHOICES.includes(pp)) out.per_page = pp;
    const pg = parseInt(o.page, 10);
    out.page = pg > 0 ? pg : 1;
    return out;
  }

  // Only non-default values travel, so a shared link stays readable and a
  // preset does not silently pin defaults that later change.
  function activeFilters(q) {
    const out = {};
    FILTER_KEYS.forEach((k) => {
      if (q[k] !== FILTER_DEFAULTS[k] && q[k] !== '' && q[k] !== false) out[k] = q[k];
    });
    // "Custom range" with neither end set narrows nothing yet -- it is just
    // the row being open. Listing it would put an empty chip on screen and
    // make the filter badge claim a filter that is not filtering.
    if (out.days === 'custom' && !out.from && !out.to) delete out.days;
    return out;
  }

  // Sort, direction and page size shape the view; they narrow nothing, so the
  // "N filters" badge must ignore them or it never reads zero. `date_field`
  // likewise only says which timestamp the window means -- the window itself
  // is `days`/`from`/`to`, which do count.
  const VIEW_KEYS = ['sort', 'dir', 'per_page', 'date_field'];

  function filterCount(q) {
    return Object.keys(activeFilters(q)).filter(
      (k) => !VIEW_KEYS.includes(k)).length;
  }

  function queryFromUrl() {
    const p = new URLSearchParams(window.location.search);
    if (![...p.keys()].some((k) => FILTER_KEYS.includes(k) || k === 'page')) return null;
    const raw = {};
    FILTER_KEYS.concat(['page']).forEach((k) => {
      if (p.has(k)) raw[k] = p.get(k);
    });
    return normalizeQuery(raw);
  }

  // The URL is the source of truth a reload or a shared link reads back, so it
  // is rewritten (without a history entry) on every filter change. The watch
  // id travels in the same query string and must survive the rewrite -- drop
  // it and the page has no watch to show.
  function syncUrl() {
    const p = new URLSearchParams();
    const id = new URLSearchParams(window.location.search).get('id');
    if (id) p.set('id', id);
    Object.entries(activeFilters(state.query)).forEach(([k, v]) => p.set(k, v));
    if (state.query.page > 1) p.set('page', state.query.page);
    const qs = p.toString();
    const url = window.location.pathname + (qs ? '?' + qs : '');
    window.history.replaceState({ q: state.query }, '', url);
  }

  function persistFilters() {
    // Best-effort: a failed write must never block rendering.
    Store.saveSetting(LAST_FILTER_KEY, state.query).catch(() => {});
  }

  // `query` is exactly what goes to the server; everything else is local UI.
  const state = {
    posts: [],
    counts: window.MONITOR.counts || { bad: 0, warn: 0, ok: 0 },
    // Replaced in boot() by the URL or the retained filters; defaults here so
    // an event arriving before boot finishes still has a valid query to read.
    query: defaultQuery(),
    meta: { pages: 1, matching: 0, total: 0, platforms: [] },
    open: new Set(),
    selected: new Set(),
    ai: {},
    keywords: window.MONITOR.keywords || {
      required: [], optional: [], excluded: [], match_mode: 'all',
    },
    loading: false,
  };

  // Only used for the few endpoints that are genuinely server-side concerns
  // (capability report, AI second opinion, vault status).
  async function api(path, opts) {
    const res = await fetch('/monitor' + path, Object.assign({
      headers: { 'Content-Type': 'application/json' },
    }, opts || {}));
    let data = {};
    try { data = await res.json(); } catch (e) { /* non-JSON error page */ }
    if (!res.ok) throw new Error(data.error || ('Request failed (' + res.status + ')'));
    return data;
  }

  /* ── Keyword editor ───────────────────────────────────────────────────── */

  const BUCKETS = {
    required: { key: 'required', el: 'kwReq', input: 'kwReqInput' },
    optional: { key: 'optional', el: 'kwOpt', input: 'kwOptInput' },
    excluded: { key: 'excluded', el: 'kwExc', input: 'kwExcInput' },
  };

  function renderChips() {
    Object.values(BUCKETS).forEach((b) => {
      const host = $('#' + b.el);
      if (!host) return;
      const terms = state.keywords[b.key] || [];
      host.innerHTML = terms.length ? terms.map((t, i) =>
        '<span class="mon-chip" data-bucket="' + b.key + '" data-i="' + i + '">' +
        '<span class="t">' + esc(t) + '</span>' +
        '<button type="button" class="x" title="Remove" aria-label="Remove ' + esc(t) + '">&times;</button>' +
        '</span>').join('')
        : '<span class="mon-chip-empty">none</span>';
    });
    const mode = state.keywords.match_mode || 'all';
    $$('.mon-kw-mode button').forEach((b) =>
      b.classList.toggle('on', b.dataset.mode === mode));
    const hint = $('#kwReqHint');
    if (hint) {
      hint.textContent = mode === 'all'
        ? 'every one must appear'
        : 'at least one must appear';
    }
  }

  function addTerm(bucket, raw) {
    const term = String(raw || '').trim().replace(/,+$/, '');
    if (!term) return false;
    const list = state.keywords[bucket] || (state.keywords[bucket] = []);
    if (list.some((t) => t.toLowerCase() === term.toLowerCase())) return false;
    list.push(term);
    return true;
  }

  function wireKeywordEditor() {
    if (!$('#kwEditor')) return;

    Object.values(BUCKETS).forEach((b) => {
      const input = $('#' + b.input);
      if (!input) return;
      input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' || e.key === ',') {
          e.preventDefault();
          // Paste of a comma list adds every term at once.
          const added = input.value.split(',').map((t) => addTerm(b.key, t))
            .some(Boolean);
          if (added) { input.value = ''; renderChips(); previewTopic(); }
        } else if (e.key === 'Backspace' && !input.value) {
          const list = state.keywords[b.key] || [];
          if (list.length) { list.pop(); renderChips(); previewTopic(); }
        }
      });
      // Losing focus should not silently discard what was typed.
      input.addEventListener('blur', () => {
        if (input.value.trim() && addTerm(b.key, input.value)) {
          input.value = '';
          renderChips();
          previewTopic();
        }
      });
    });

    $('#kwEditor').addEventListener('click', (e) => {
      const x = e.target.closest('.mon-chip .x');
      if (x) {
        const chip = x.closest('.mon-chip');
        const list = state.keywords[chip.dataset.bucket] || [];
        list.splice(parseInt(chip.dataset.i, 10), 1);
        renderChips();
        previewTopic();
        return;
      }
      const mode = e.target.closest('.mon-kw-mode button');
      if (mode) {
        state.keywords.match_mode = mode.dataset.mode;
        renderChips();
        previewTopic();
      }
    });

    renderChips();
  }

  const previewTopic = debounce(async () => {
    const el = $('#topicPreview');
    if (!el) return;
    try {
      const t = await Data.keywords({
        kw_required: (state.keywords.required || []).join('\n'),
        kw_optional: (state.keywords.optional || []).join('\n'),
        kw_excluded: (state.keywords.excluded || []).join('\n'),
        kw_match_mode: state.keywords.match_mode || 'all',
        subject: $('#f-subject') ? $('#f-subject').value : '',
      });
      let html = '';
      if (t.warning) {
        html += '<div style="color:var(--yellow)"><i class="fa fa-triangle-exclamation me-1"></i>' +
          esc(t.warning) + '</div>';
      }
      if (t.required.length) {
        const joiner = t.match_mode === 'all' ? ' and ' : ' or ';
        html += '<div>On-topic when it mentions ' +
          t.required.map((a) => '<span class="mon-kbd" style="color:var(--accent)">' + esc(a) + '</span>').join(joiner) +
          '</div>';
      }
      if (t.excluded.length) {
        html += '<div>Rejected if it mentions ' +
          t.excluded.map((a) => '<span class="mon-kbd" style="color:var(--red)">' + esc(a) + '</span>').join(' or ') +
          '</div>';
      }
      if (t.query) {
        html += '<div class="mt-1" style="opacity:.75">Search: <code style="font-size:11px">' +
          esc(t.query) + '</code></div>';
      }
      // A bare acronym silently loses every post that spells it out. The app
      // cannot know the long form, so it asks for it here rather than dropping
      // those posts without saying anything.
      (t.alias_hints || []).forEach((h) => {
        html += '<div class="mon-alias-hint">' +
          '<i class="fa fa-lightbulb"></i><span>' + esc(h.why) +
          ' Try <span class="mon-kbd">' + esc(h.suggest) + '</span>' +
          '</span></div>';
      });
      el.innerHTML = html;
    } catch (e) { el.textContent = ''; }
  }, 350);

  /* ── Settings ─────────────────────────────────────────────────────────── */

  function collectWeights() {
    const out = {};
    $$('[data-weight]').forEach((el) => {
      const v = parseInt(el.value, 10);
      if (!isNaN(v)) out[el.dataset.weight] = v;
    });
    return out;
  }

  function markChangedWeights() {
    $$('[data-weight]').forEach((el) => {
      const changed = parseInt(el.value, 10) !== parseInt(el.dataset.default, 10);
      el.classList.toggle('changed', changed);
      const label = document.querySelector('[data-wlabel="' + el.dataset.weight + '"]');
      if (label) label.classList.toggle('changed', changed);
    });
  }

  function settingsPayload() {
    return {
      subject: $('#f-subject').value,
      kw_required: (state.keywords.required || []).join('\n'),
      kw_optional: (state.keywords.optional || []).join('\n'),
      kw_excluded: (state.keywords.excluded || []).join('\n'),
      kw_match_mode: state.keywords.match_mode || 'all',
      profile_id: $('#f-profile').value || null,
      mode_release: $('#m-release').checked,
      mode_hunter: $('#m-hunter').checked,
      reference_text: $('#f-reference') ? $('#f-reference').value : '',
      official_accounts: $('#f-accounts') ? $('#f-accounts').value : '',
      official_domains: $('#f-domains') ? $('#f-domains').value : '',
      custom_flags: $('#f-flags').value,
      threshold_review: parseInt($('#f-t-review').value, 10) || 30,
      threshold_high: parseInt($('#f-t-high').value, 10) || 60,
      weights: collectWeights(),
    };
  }

  async function applySettings(quiet) {
    try {
      Object.assign(watch, settingsPayload());
      await Data.saveWatch(watch);
      state.query.page = 1;
      // Changing the rules invalidates every score; ensureFresh (inside
      // refresh) recomputes exactly the posts affected.
      await refresh();
      if (!quiet) showToast('Rescored with the new settings', 'success');
    } catch (e) {
      showToast(e.message, 'danger');
    }
  }

  /* ── Data ─────────────────────────────────────────────────────────────── */

  async function refresh() {
    state.loading = true;
    paintLoading();
    try {
      const data = await Data.results(watch, state.query);
      state.posts = data.posts;
      state.counts = data.counts;
      state.meta = {
        pages: data.pages, matching: data.matching, total: data.total,
        page: data.page, per_page: data.per_page,
        has_next: data.has_next, has_prev: data.has_prev,
        platforms: data.platforms || [],
      };
      state.query.page = data.page;
    } catch (e) {
      showToast(e.message, 'danger');
    } finally {
      state.loading = false;
    }
    render();
    renderBriefing();
  }

  function paintLoading() {
    const list = $('#list');
    if (list && !state.posts.length) {
      list.innerHTML = '<div class="mon-empty"><i class="fa fa-spinner fa-spin me-2"></i>Loading…</div>';
    }
  }

  // Jump to page 1 whenever a filter changes, since the current page number
  // is meaningless against a different result set. Every change is mirrored to
  // the URL and to storage, so the view survives a reload or a trip to the
  // link map and back.
  function setFilter(patch) {
    Object.assign(state.query, patch, { page: 1 });
    state.selected.clear();
    syncUrl();
    persistFilters();
    // Some controls reflect state rather than only setting it -- the range row
    // opens and closes with the window, and the direction arrow has to point
    // the way the list now runs. Without this they only updated on a reload.
    syncControls();
    refresh();
  }

  // Push the query back onto the controls. Used on boot and after applying a
  // preset, so the toolbar never disagrees with the results below it.
  function syncControls() {
    const q = state.query;
    // Never write back into the control the analyst is currently using. The
    // search box is debounced, so overwriting a focused field would drop
    // whatever was typed between the keystroke and the query landing.
    const set = (id, v) => {
      const el = $('#' + id);
      if (el && el !== document.activeElement) el.value = v;
    };
    set('q', q.q);
    set('statusSel', q.status);
    set('kindSel', q.kind);
    set('platformSel', q.platform);
    set('daysSel', q.days);
    set('dateFieldSel', q.date_field);
    set('sortSel', q.sort);
    set('perPageSel', String(q.per_page));
    const pin = $('#pinnedOnly');
    if (pin) pin.checked = q.pinned;
    syncSortDir();
    syncRangeRow();
  }

  // The direction button carries its own state, and the icon has to say which
  // way the list currently runs -- an arrow that never changes is worse than
  // no arrow at all.
  function syncSortDir() {
    const btn = $('#sortDirBtn');
    if (!btn) return;
    const asc = state.query.dir === 'asc';
    btn.dataset.dir = asc ? 'asc' : 'desc';
    btn.innerHTML = '<i class="fa ' +
      (asc ? 'fa-arrow-up-short-wide' : 'fa-arrow-down-wide-short') + '"></i>';
    const opt = $('#sortSel') &&
      $('#sortSel').querySelector('option[value="' + state.query.sort + '"]');
    const name = opt ? opt.textContent.toLowerCase() : state.query.sort;
    btn.title = asc
      ? 'Lowest ' + name + ' first — click for highest first'
      : 'Highest ' + name + ' first — click for lowest first';
  }

  // The range inputs only exist while "Custom range…" is the chosen window.
  function syncRangeRow() {
    const row = $('#rangeRow');
    if (!row) return;
    const on = state.query.days === 'custom';
    row.style.display = on ? '' : 'none';
    if (!on) return;
    const from = $('#rangeFrom');
    const to = $('#rangeTo');
    if (from) from.value = state.query.from || '';
    if (to) to.value = state.query.to || '';
    const note = $('#rangeNote');
    if (note) {
      note.textContent = (!state.query.from && !state.query.to)
        ? 'Set either end, or both.'
        : '';
    }
  }

  /* ── Rendering ────────────────────────────────────────────────────────── */

  function highlight(text, a) {
    const flags = new Set(a.flag_words || []);
    const linkSet = (a.links || []);
    // Split into links, words, and everything else so we can wrap each kind.
    const re = /((?:https?:\/\/|www\.)[^\s<>"]+)|([A-Za-z0-9À-ɏ']+)|([^A-Za-z0-9À-ɏ']+)/g;
    let out = '', m;
    while ((m = re.exec(text))) {
      if (m[1]) {
        const L = linkSet.find((l) => m[1].startsWith(l.display) || l.display.startsWith(m[1]));
        const cls = L ? (L.official ? 'ok' : (L.flags.length ? 'bad' : '')) : '';
        out += '<span class="mon-lnk ' + cls + '">' + esc(m[1]) + '</span>';
      } else if (m[2]) {
        const w = m[2].toLowerCase().replace(/'/g, '');
        if (flags.has(w)) out += '<mark class="hl-flag">' + esc(m[2]) + '</mark>';
        else out += esc(m[2]);
      } else {
        out += esc(m[3]);
      }
    }
    return out;
  }

  function ledger(a) {
    const pos = (s, e) => {
      const lo = clamp(Math.min(s, e));
      return 'left:' + lo + '%;width:' + Math.max(clamp(Math.max(s, e)) - lo, 0.8) + '%';
    };
    let run = 0, html = '';
    const rows = [{ label: 'Starting point', detail: 'Every post begins here', w: a.base, base: true }]
      .concat(a.signals);
    rows.forEach((r) => {
      const s = run, e = run + r.w; run = e;
      const dir = r.base ? 'base' : (r.w > 0 ? 'up' : 'down');
      html += '<div class="mon-row"><div><span class="lbl">' + esc(r.label) + '</span>' +
        '<span class="det">' + esc(r.detail) + '</span></div>' +
        '<div class="mon-track"><span class="mon-seg ' + dir + '" style="' + pos(s, e) + '"></span></div>' +
        '<div class="mon-w ' + (r.base ? '' : dir) + '">' +
        (r.w > 0 && !r.base ? '+' : '') + r.w + '</div></div>';
    });
    const capped = run !== a.raw_total || a.raw_total !== a.score;
    html += '<div class="mon-row total"><div><span class="lbl">Risk score</span>' +
      '<span class="det">' + (capped ? 'Raw total ' + a.raw_total + ', capped to 0–100' : 'Sum of the rows above') + '</span></div>' +
      '<div class="mon-track"><span class="mon-seg final" style="' + pos(0, a.score) + '"></span></div>' +
      '<div class="mon-w" style="color:var(--c)">' + a.score + '</div></div>';
    return html;
  }

  function profileOptions(sel) {
    let html = '<option value="">— no profile —</option>';
    PROFILES.forEach((p) => {
      html += '<option value="' + p.id + '"' + (sel === p.id ? ' selected' : '') + '>' + esc(p.codename) + '</option>';
    });
    return html;
  }

  function evidence(p, a) {
    // Risky links stay un-clickable on purpose; safe ones are worth opening.
    const links = (a.links || []).length ? '<div><h6>Links</h6><ul class="mon-links">' +
      a.links.map((l) => {
        const cls = l.official ? 'ok' : (l.flags.length ? 'bad' : '');
        const why = l.official ? 'Official domain'
          : l.cited ? 'Cited in the reference'
          : l.flags.length ? esc(l.flags.map((f) => f.label).join('; '))
          : 'External, no red flags';
        const host = l.flags.length
          ? '<span class="mon-lnk bad" title="Not clickable — flagged as risky">' + esc(l.host) + '</span>'
          : '<a class="mon-lnk ' + cls + '" href="' + esc(l.display) + '" target="_blank" rel="noopener noreferrer nofollow">' +
            esc(l.host) + ' <i class="fa fa-arrow-up-right-from-square" style="font-size:8px"></i></a>';
        return '<li>' + host + '<small>' + why + '</small></li>';
      }).join('') +
      '</ul><p class="mon-note mt-2 mb-0">Flagged links are shown as text only and are not clickable.</p></div>' : '';

    const matches = (a.profile_matches || []).length ? '<div><h6>Profile matches</h6>' +
      a.profile_matches.map((m) => '<div class="mon-row" style="grid-template-columns:1fr auto">' +
        '<div><span class="lbl">' + esc(m.codename) + '</span><span class="det">' +
        esc(m.kind) + ' "' + esc(m.matched) + '" via ' + esc(m.via) + '</span></div>' +
        '<div class="mon-w">' + Math.round(m.confidence * 100) + '%</div></div>').join('') + '</div>' : '';

    const aiState = state.ai[p.id];
    let aiHtml = '<button class="btn btn-ghost btn-xs" data-act="ai">Second opinion from Claude</button>';
    if (aiState && aiState.loading) aiHtml = '<span class="mon-note">Asking Claude…</span>';
    else if (aiState && aiState.error) aiHtml = '<span class="mon-note" style="color:var(--red)">' + esc(aiState.error) + '</span>';
    else if (aiState && aiState.data) {
      const map = { high_risk: 'bad', needs_review: 'warn', likely_authentic: 'ok' };
      const v = VERD[map[aiState.data.verdict]] || VERD.warn;
      aiHtml = '<div class="mon-action" style="--cbg:var(--bg-card2)"><b style="color:' + v.color + '">Claude: ' + v.label + '</b>' +
        (aiState.data.threat_type ? ' — ' + esc(aiState.data.threat_type) : '') +
        '<div class="mon-note mt-1 mb-0">' + esc(aiState.data.rationale || '') + '</div></div>';
    }

    return '<div class="mon-evidence">' +
      '<div class="mon-triage">' +
        '<label>Manual score</label>' +
        '<input type="number" min="0" max="100" data-field="manual_score" value="' + (p.manual_score == null ? '' : p.manual_score) + '" placeholder="' + a.score + '">' +
        '<label>Verdict</label>' +
        '<select data-field="manual_verdict">' +
          '<option value="">auto</option>' +
          ['bad', 'warn', 'ok'].map((k) => '<option value="' + k + '"' + (p.manual_verdict === k ? ' selected' : '') + '>' + VERD[k].label + '</option>').join('') +
        '</select>' +
        '<label>Status</label>' +
        '<select data-field="status">' +
          STATUSES.map((s) => '<option value="' + s + '"' + (p.status === s ? ' selected' : '') + '>' + s + '</option>').join('') +
        '</select>' +
        '<label>Profile</label>' +
        '<select data-field="profile_id">' + profileOptions(p.profile_id) + '</select>' +
        '<input class="grow" data-field="analyst_note" placeholder="Analyst note" value="' + esc(p.analyst_note || '') + '">' +
        '<button class="btn btn-primary btn-xs" data-act="save">Save</button>' +
      '</div>' +
      (matches || '') + links +
      '<div><h6>How the score was built</h6>' +
      '<p class="mon-note">Red bars add risk, green bars count as evidence it is genuine. Ticks mark the review and high-risk thresholds.</p>' +
      ledger(a) + '</div>' +
      '<div class="mon-action"><b>Recommended:</b> ' + esc(a.recommendation) + '</div>' +
      '<div class="d-flex gap-2 flex-wrap align-items-center">' + aiHtml +
        '<button class="btn btn-ghost btn-xs" data-act="push">Push to profile notes</button>' +
        '<button class="btn btn-ghost btn-xs" data-act="pin">' + (p.pinned ? 'Unpin' : 'Pin') + '</button>' +
        '<button class="btn btn-danger btn-xs ms-auto" data-act="remove">Remove post</button>' +
      '</div></div>';
  }

  // "3 h ago" reads faster than a timestamp when triaging a long list.
  function ago(iso) {
    if (!iso) return '';
    // Stored stamps are UTC with the zone stripped, so they must be parsed as
    // UTC. Reading them as local time made everything look hours older (or
    // newer) than it was, by exactly the viewer's offset.
    const t = iso.length <= 10
      ? Date.parse(iso + 'T00:00:00Z')
      : Store.parseStored(iso);
    if (!t || isNaN(t)) return iso;
    const mins = Math.round((Date.now() - t) / 60000);
    if (mins < 1) return 'just now';
    if (mins < 60) return mins + ' min ago';
    const h = Math.round(mins / 60);
    if (h < 24) return h + ' h ago';
    const d = Math.round(h / 24);
    return d < 30 ? d + ' d ago' : new Date(t).toISOString().slice(0, 10);
  }

  function openLink(p, label) {
    if (!p.url) return '';
    const search = p.link_kind === 'search';
    return '<a class="mon-tag" href="' + esc(p.url) + '" target="_blank" rel="noopener noreferrer" ' +
      'title="' + (search ? 'Aggregator hid the real URL — opens a search for this headline'
                          : esc(p.url)) + '">' +
      '<i class="fa fa-arrow-up-right-from-square"></i> ' +
      (label || (search ? 'find article' : 'open')) + '</a>';
  }

  function card(p) {
    const a = p.analysis;
    const open = state.open.has(p.id);
    const sel = state.selected.has(p.id);
    const when = ago(p.posted_at || p.collected_at);
    // The author links out when we know where the post lives.
    const who = p.url
      ? '<a class="mon-author mon-out" href="' + esc(p.url) + '" target="_blank" rel="noopener noreferrer">' + esc(p.author) + '</a>'
      : '<span class="mon-author">' + esc(p.author) + '</span>';
    return '<article class="mon-post v-' + a.verdict + (p.pinned ? ' is-pinned' : '') + (sel ? ' sel' : '') + '" data-id="' + p.id + '">' +
      '<div class="mon-top">' +
        '<div style="min-width:0;display:flex;gap:10px;align-items:flex-start">' +
          '<input type="checkbox" data-act="select"' + (sel ? ' checked' : '') + ' style="margin-top:4px">' +
          '<div style="min-width:0">' +
            who +
            (p.verified ? ' <i class="fa fa-circle-check" style="color:var(--accent);font-size:11px" title="Verified on platform"></i>' : '') +
            // A reply reads very differently from a post, so say which it is
            // and under whose thread it sits.
            (p.kind === 'comment'
              ? ' <span class="mon-tag comment-tag" title="A reply' +
                (p.parent_author ? ' under ' + esc(p.parent_author) + "'s post" : '') +
                '"><i class="fa fa-reply"></i> comment' +
                (p.parent_author ? ' · ' + esc(p.parent_author) : '') + '</span>'
              : '') +
            '<span class="mon-meta">' + (p.handle ? '@' + esc(p.handle) + ' · ' : '') + esc(p.platform) +
            (when ? ' · ' + esc(when) : '') +
            (p.source && p.source !== 'manual' ? ' · via ' + esc(p.source) : '') + '</span>' +
          '</div>' +
        '</div>' +
        '<div class="mon-score' + (a.overridden ? ' overridden' : '') + '" ' +
          'title="' + (a.overridden ? 'Set by an analyst. Rules scored ' + (a.auto_score != null ? a.auto_score : '-') : 'Scored by the rules') + '">' +
          '<b>' + a.score + '</b><span>risk / 100</span></div>' +
      '</div>' +
      '<p class="mon-text ' + (open ? '' : 'clamp') + '">' + highlight(p.text, a) + '</p>' +
      '<div class="mon-foot">' +
        '<span class="mon-verdict">' + a.verdict_label + '</span>' +
        (a.types || []).map((t) => '<span class="mon-tag">' + esc(t) + '</span>').join('') +
        (p.profile_codename ? '<a class="mon-tag profile" href="/profiles/' + p.profile_id + '" target="_blank" rel="noopener"><i class="fa fa-id-card"></i> ' + esc(p.profile_codename) + '</a>' : '') +
        (p.status && p.status !== 'new' ? '<span class="mon-tag status-' + p.status + '">' + esc(p.status) + '</span>' : '') +
        openLink(p) +
        '<button class="mon-linkbtn" data-act="toggle">' + (open ? 'Hide evidence' : 'Show evidence') + '</button>' +
      '</div>' +
      (open ? evidence(p, a) : '') +
      '</article>';
  }

  function render() {
    const c = state.counts;
    const total = state.meta.total || 0;
    const m = state.meta;

    $('#sumTitle').textContent = !total ? 'No posts yet'
      : c.bad ? c.bad + (c.bad === 1 ? ' post needs' : ' posts need') + ' action now'
      : c.warn ? c.warn + (c.warn === 1 ? ' post needs' : ' posts need') + ' a closer look'
      : 'Nothing flagged';
    $('#scanMeta').textContent = total
      ? 'Scored ' + total + ' post' + (total === 1 ? '' : 's') + ' at ' +
        new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) + '.'
      : 'Collect posts live, add one by hand, or import a batch.';

    const dist = $('#dist');
    requestAnimationFrame(() => {
      dist.querySelector('.d-bad').style.flexGrow = c.bad;
      dist.querySelector('.d-warn').style.flexGrow = c.warn;
      dist.querySelector('.d-ok').style.flexGrow = c.ok;
    });

    $('#legend').innerHTML = [
      ['all', 'All', total, null], ['bad', VERD.bad.label, c.bad, VERD.bad.color],
      ['warn', VERD.warn.label, c.warn, VERD.warn.color], ['ok', VERD.ok.label, c.ok, VERD.ok.color],
    ].map(([k, l, n, col]) => '<button data-filter="' + k + '" aria-pressed="' + (state.query.verdict === k) + '">' +
      (col ? '<i style="background:' + col + '"></i>' : '') + l + ' <b>' + n + '</b></button>').join('');

    // Platform dropdown reflects what is actually in this watch.
    const psel = $('#platformSel');
    if (psel && m.platforms) {
      const cur = state.query.platform;
      psel.innerHTML = '<option value="">All platforms</option>' +
        m.platforms.map((p) => '<option value="' + esc(p.platform) + '"' +
          (cur === p.platform ? ' selected' : '') + '>' + esc(p.platform) +
          ' (' + p.count + ')</option>').join('');
    }

    // An empty list has three quite different causes; say which one it is,
    // and offer the way out of the one that is recoverable.
    let empty;
    if (!total) {
      empty = 'No posts yet. Collect some above.';
    } else if (filterCount(state.query)) {
      empty = 'None of the <b>' + total + '</b> posts in this watch match ' +
        describeFilters(activeFilters(state.query)) + '. ' +
        '<button class="btn btn-ghost btn-xs" id="btnClearInline">Clear filters</button>';
    } else {
      empty = 'No posts on this page. ' +
        '<button class="btn btn-ghost btn-xs" id="btnClearInline">Back to the start</button>';
    }
    $('#list').innerHTML = state.posts.length
      ? state.posts.map(card).join('')
      : '<div class="mon-empty">' + empty + '</div>';

    renderMeta();
    renderPager();
    renderPresets();

    $('#selCount').textContent = state.selected.size;
    $('#bulkBar').style.display = state.selected.size ? '' : 'none';
  }

  // Labels for the removable chips below the toolbar. Every narrowing filter
  // appears here -- including `types`, which has no control of its own and
  // would otherwise stay applied invisibly after a reload.
  const CHIP_LABELS = {
    verdict: (v) => VERD[v].label,
    status: (v) => 'Status: ' + v,
    platform: (v) => v,
    // A custom range reads as one chip covering both ends: two chips that only
    // make sense together are two chips you can break by removing one.
    days: (v, f) => {
      if (v !== 'custom') return windowLabel(v);
      if (f.from && f.to) return shortStamp(f.from) + ' → ' + shortStamp(f.to);
      if (f.from) return 'Since ' + shortStamp(f.from);
      return 'Up to ' + shortStamp(f.to);
    },
    date_field: () => 'By collected date',
    types: (v) => 'Type: ' + v,
    pinned: () => 'Pinned only',
    q: (v) => '“' + v + '”',
  };

  // Removing a chip clears everything that chip stood for.
  const CHIP_CLEARS = {
    days: { days: '', from: '', to: '' },
  };

  function renderChipsForFilters() {
    const f = activeFilters(state.query);
    return Object.keys(CHIP_LABELS)
      .filter((k) => k in f)
      .map((k) => '<button class="mon-fchip" data-drop-filter="' + k + '" ' +
        'title="Remove this filter">' + esc(CHIP_LABELS[k](f[k], f)) +
        ' <i class="fa fa-xmark"></i></button>').join('');
  }

  function renderMeta() {
    const m = state.meta;
    const el = $('#resultMeta');
    if (!el) return;
    const chips = renderChipsForFilters();
    const chipRow = chips
      ? '<div class="mon-fchips">' + chips +
        '<button class="mon-fchip clear" data-drop-filter="*">Clear all</button></div>'
      : '';
    if (!m.matching) {
      // Say why the list is empty: an empty watch and an over-narrow filter
      // look identical otherwise.
      el.innerHTML = chipRow;
      return;
    }
    const from = (m.page - 1) * m.per_page + 1;
    const to = Math.min(m.page * m.per_page, m.matching);
    const filtered = m.matching !== m.total;
    el.innerHTML = chipRow +
      '<div>Showing <b>' + from + '–' + to + '</b> of <b>' + m.matching + '</b>' +
      (filtered ? ' matching (of ' + m.total + ' total)' : ' posts') +
      ' · page ' + m.page + ' of ' + m.pages + '</div>';
  }

  // Removing one chip re-runs the query with just that filter dropped.
  document.addEventListener('click', (e) => {
    const chip = e.target.closest('[data-drop-filter]');
    if (!chip) return;
    const k = chip.dataset.dropFilter;
    if (k === '*') { resetFilters(); return; }
    // Some chips stand for more than one key (a custom range is days + both
    // bounds); clearing only the named key would leave the rest applied.
    setFilter(CHIP_CLEARS[k] || { [k]: FILTER_DEFAULTS[k] });
    syncControls();
  });

  // A windowed pager: first, last, and a few either side of the current page.
  function renderPager() {
    const el = $('#pager');
    if (!el) return;
    const { page, pages } = state.meta;
    if (!pages || pages <= 1) { el.innerHTML = ''; return; }

    const nums = new Set([1, pages, page, page - 1, page + 1, page - 2, page + 2]);
    const list = Array.from(nums).filter((n) => n >= 1 && n <= pages).sort((a, b) => a - b);

    let html = '<button data-page="' + (page - 1) + '"' + (page <= 1 ? ' disabled' : '') +
      ' aria-label="Previous page"><i class="fa fa-chevron-left"></i></button>';
    let prev = 0;
    list.forEach((n) => {
      if (prev && n - prev > 1) html += '<span class="gap">…</span>';
      html += '<button data-page="' + n + '"' + (n === page ? ' class="on" aria-current="page"' : '') +
        '>' + n + '</button>';
      prev = n;
    });
    html += '<button data-page="' + (page + 1) + '"' + (page >= pages ? ' disabled' : '') +
      ' aria-label="Next page"><i class="fa fa-chevron-right"></i></button>';
    el.innerHTML = html;
  }

  function goToPage(n) {
    const p = Math.max(1, Math.min(state.meta.pages || 1, n));
    if (p === state.query.page) return;
    state.query.page = p;
    syncUrl();
    persistFilters();
    refresh().then(() => {
      const top = document.getElementById('list');
      if (top) top.scrollIntoView({ behavior: 'smooth', block: 'start' });
    });
  }

  document.addEventListener('click', (e) => {
    const pg = e.target.closest('#pager [data-page]');
    if (pg && !pg.disabled) {
      goToPage(parseInt(pg.dataset.page, 10));
      return;
    }
    if (e.target.closest('#btnClearInline')) {
      resetFilters();
    }
  });

  // Left/right arrows page through results when not typing in a field.
  document.addEventListener('keydown', (e) => {
    if (/^(INPUT|TEXTAREA|SELECT)$/.test((e.target.tagName || '')) ||
        e.ctrlKey || e.metaKey || e.altKey) return;
    if (e.key === 'ArrowRight' && state.meta.has_next) goToPage(state.query.page + 1);
    if (e.key === 'ArrowLeft' && state.meta.has_prev) goToPage(state.query.page - 1);
  });

  // Clears narrowing filters but keeps sort and page size, which are how the
  // analyst likes to read results rather than part of the question being asked.
  function resetFilters() {
    const keep = { sort: state.query.sort, per_page: state.query.per_page };
    state.query = Object.assign(defaultQuery(), keep);
    syncControls();
    state.selected.clear();
    syncUrl();
    persistFilters();
    refresh();
  }

  /* ── Briefing ─────────────────────────────────────────────────────────── */

  function kv(k, v, cls, sub) {
    return '<div class="mon-kv"><span class="k">' + k +
      (sub ? '<small>' + sub + '</small>' : '') + '</span>' +
      '<span class="v ' + (cls || '') + '">' + v + '</span></div>';
  }

  async function renderBriefing() {
    const el = $('#briefing');
    if (!el) return;
    let b;
    try { b = await Data.briefing(watch); }
    catch (e) { el.innerHTML = ''; return; }
    state.briefing = b;

    const cells = [];

    cells.push('<div class="mon-brief-cell"><h6>Where it stands</h6>' +
      '<div class="mon-bignum">' +
      '<div class="n-bad"><b>' + b.counts.bad + '</b><span>High</span></div>' +
      '<div class="n-warn"><b>' + b.counts.warn + '</b><span>Review</span></div>' +
      '<div class="n-ok"><b>' + b.counts.ok + '</b><span>Clear</span></div>' +
      '<div class="n-pend"><b>' + b.pending_review + '</b><span>Untriaged</span></div>' +
      '</div>' +
      (b.date_range ? '<p class="mon-note mt-2 mb-0">Posts from ' +
        esc(ago(b.date_range.first)) + ' to ' + esc(ago(b.date_range.last)) + '.</p>' : '') +
      '</div>');

    if (b.drivers.length) {
      cells.push('<div class="mon-brief-cell"><h6>What is driving risk</h6>' +
        b.drivers.slice(0, 5).map((d) =>
          kv(esc(d.label), '+' + d.weight, 'up', d.count + ' post' + (d.count === 1 ? '' : 's'))
        ).join('') + '</div>');
    }

    if (b.types.length) {
      cells.push('<div class="mon-brief-cell"><h6>Threat types seen</h6>' +
        b.types.slice(0, 6).map((t) =>
          '<div class="mon-kv"><span class="k"><a href="#" class="mon-out" data-type-filter="' +
          esc(t.type) + '">' + esc(t.type) + '</a></span><span class="v dim">' + t.count + '</span></div>'
        ).join('') + '</div>');
    }

    if (b.platforms.length) {
      cells.push('<div class="mon-brief-cell"><h6>By platform</h6>' +
        b.platforms.slice(0, 5).map((p) =>
          kv(esc(p.platform), p.total, p.bad ? 'up' : 'dim',
             'avg risk ' + p.avg + (p.bad ? ' · ' + p.bad + ' high' : ''))
        ).join('') + '</div>');
    }

    if (b.actors.length) {
      cells.push('<div class="mon-brief-cell"><h6>Repeat accounts</h6>' +
        b.actors.slice(0, 5).map((a) =>
          kv((a.url ? '<a class="mon-out" href="' + esc(a.url) + '" target="_blank" rel="noopener noreferrer">' + esc(a.author || a.handle) + '</a>'
                    : esc(a.author || a.handle)),
             a.flagged + '/' + a.posts, a.top_score >= 60 ? 'up' : 'dim',
             'top risk ' + a.top_score + (a.types.length ? ' · ' + esc(a.types.slice(0, 2).join(', ')) : ''))
        ).join('') + '</div>');
    }

    const risky = b.domains.filter((d) => d.risky);
    if (risky.length) {
      cells.push('<div class="mon-brief-cell"><h6>Risky domains</h6>' +
        risky.slice(0, 5).map((d) =>
          kv('<span class="mon-lnk bad">' + esc(d.host) + '</span>', d.count, 'up',
             esc(d.reasons.slice(0, 1).join('')))
        ).join('') + '</div>');
    }

    if (b.top.length) {
      cells.push('<div class="mon-brief-cell wide"><h6>Needs a decision first</h6>' +
        '<div class="mon-queue">' + b.top.slice(0, 5).map((t) =>
          '<a class="v-' + t.verdict + '" href="#post-' + t.id + '" data-jump="' + t.id + '">' +
          '<span class="sc">' + t.score + '</span><span class="bd">' +
          '<span class="who">' + esc(t.author) + '</span> ' +
          '<span class="mon-tag">' + esc(t.platform) + '</span> ' +
          (t.types.length ? '<span class="mon-tag">' + esc(t.types[0]) + '</span>' : '') +
          '<span class="ex">' + esc(t.recommendation) + '</span></span></a>'
        ).join('') + '</div></div>');
    }

    if (b.gaps.length) {
      cells.push('<div class="mon-brief-cell wide"><h6>Set-up gaps weakening these results</h6>' +
        b.gaps.map((g) => '<div class="mon-gap"><i class="fa fa-triangle-exclamation"></i><span>' +
          esc(g) + '</span></div>').join('') + '</div>');
    }

    // An empty watch has nothing to brief on, and a 345px panel of zeroes
    // pushes the collection controls off a 720px screen -- which is the first
    // thing someone needs on a watch with no posts. Collapse to one line until
    // there is something to say.
    if (!b.total) {
      el.innerHTML = '<div class="mon-brief empty"><div class="mon-brief-head">' +
        '<h3>Nothing collected yet</h3><span class="meta">' +
        'Pick your sources below and collect, or add a post by hand.' +
        '</span></div></div>';
      return;
    }

    el.innerHTML = '<div class="mon-brief">' +
      '<div class="mon-brief-head">' +
        '<h3>' + esc(b.headline) + '</h3>' +
        '<span class="meta">' + b.total + ' post' + (b.total === 1 ? '' : 's') +
        ' · ' + esc(b.watch.mode_label) + ' · updated ' + esc(b.generated_at) + '</span>' +
      '</div>' +
      '<div class="mon-brief-body">' + cells.join('') + '</div></div>';
  }

  // Jumping from the briefing to the post it names.
  document.addEventListener('click', (e) => {
    const jump = e.target.closest('[data-jump]');
    if (jump) {
      e.preventDefault();
      const id = parseInt(jump.dataset.jump, 10);
      state.filter = 'all';
      state.open.add(id);
      render();
      const el = document.querySelector('.mon-post[data-id="' + id + '"]');
      if (el) {
        el.scrollIntoView({ behavior: 'smooth', block: 'center' });
        el.style.transition = 'box-shadow .4s';
        el.style.boxShadow = '0 0 0 2px var(--accent)';
        setTimeout(() => { el.style.boxShadow = ''; }, 1600);
      }
      return;
    }
    const tf = e.target.closest('[data-type-filter]');
    if (tf) {
      e.preventDefault();
      // Filter by threat type properly, rather than by dropping the label into
      // the search box: `types` matches the classification, not the post text.
      setFilter({ types: tf.dataset.typeFilter, verdict: 'all' });
      $('#list').scrollIntoView({ behavior: 'smooth', block: 'start' });
    }
  });

  /* ── Post actions ─────────────────────────────────────────────────────── */

  async function savePost(id, article) {
    const body = {};
    article.querySelectorAll('[data-field]').forEach((el) => {
      const f = el.dataset.field;
      body[f] = (f === 'manual_score' && el.value === '') ? null
        : (f === 'profile_id' && el.value === '') ? null : el.value;
    });
    try {
      // Blank number fields mean "no override", not zero.
      if (body.manual_score != null && body.manual_score !== '') {
        body.manual_score = Math.max(0, Math.min(100, parseInt(body.manual_score, 10) || 0));
      }
      await Data.updatePost(id, body);
      await refresh();
      showToast('Saved', 'success', 1500);
    } catch (e) { showToast(e.message, 'danger'); }
  }

  async function askClaude(id) {
    state.ai[id] = { loading: true }; render();
    try {
      const data = await api('/posts/' + id + '/second-opinion', { method: 'POST', body: '{}' });
      state.ai[id] = { data: data.data };
    } catch (e) {
      state.ai[id] = { error: e.message };
    }
    render();
  }

  $('#list').addEventListener('click', async (e) => {
    const btn = e.target.closest('[data-act]');
    if (!btn) return;
    const article = btn.closest('.mon-post');
    const id = parseInt(article.dataset.id, 10);
    const act = btn.dataset.act;

    if (act === 'toggle') {
      state.open.has(id) ? state.open.delete(id) : state.open.add(id);
      render();
    } else if (act === 'select') {
      state.selected.has(id) ? state.selected.delete(id) : state.selected.add(id);
      render();
    } else if (act === 'save') {
      savePost(id, article);
    } else if (act === 'remove') {
      if (!await confirmModal('Remove this post from the watch?', 'Remove')) return;
      try {
        await Data.deletePost(id);
        state.open.delete(id); state.selected.delete(id);
        await refresh();
      } catch (err) { showToast(err.message, 'danger'); }
    } else if (act === 'pin') {
      const p = state.posts.find((x) => x.id === id);
      try { await Data.updatePost(id, { pinned: !p.pinned }); await refresh(); }
      catch (err) { showToast(err.message, 'danger'); }
    } else if (act === 'ai') {
      askClaude(id);
    } else if (act === 'push') {
      const p = state.posts.find((x) => x.id === id);
      if (!p.profile_id) { showToast('Link this post to a profile first', 'warning'); return; }
      try {
        const prof = await Store.get('profiles', p.profile_id);
        const a = p.analysis || {};
        const score = p.manual_score != null ? p.manual_score : p.score;
        const lines = [
          '[Signal Monitor] ' + p.author + ' on ' + p.platform,
          'Risk ' + score + '/100 - ' + (p.verdict_label || '') +
            ((p.types || []).length ? ' - ' + p.types.join(', ') : ''),
          '', (p.text || '').slice(0, 1500),
        ];
        if (p.url) lines.push('', 'Link: ' + p.url);
        if (p.analyst_note) lines.push('', 'Analyst note: ' + p.analyst_note);
        await Store.put('notes', {
          profile_id: p.profile_id, content: lines.join('\n'),
          source: 'Signal Monitor / ' + watch.name,
          created_at: new Date().toISOString().slice(0, 19),
        });
        showToast('Added to ' + ((prof && prof.codename) || 'the profile') +
                  "'s intel notes", 'success');
      } catch (err) { showToast(err.message, 'danger'); }
    }
  });

  /* ── Bulk actions ─────────────────────────────────────────────────────── */

  async function bulk(action, value) {
    try {
      const ids = Array.from(state.selected);
      const affected = await Data.bulk(WATCH, ids, action, value);
      const data = { affected };
      showToast(affected + ' post(s) updated', 'success');
      if (action === 'delete') {
        state.selected.clear();
        // Deleting the last row of a page would otherwise leave it empty.
        if (state.posts.length === data.affected && state.query.page > 1) {
          state.query.page -= 1;
          syncUrl();
        }
      }
      await refresh();
    } catch (e) { showToast(e.message, 'danger'); }
  }

  $('#bulkStatus').addEventListener('change', (e) => { if (e.target.value) { bulk('status', e.target.value); e.target.value = ''; } });
  $('#bulkProfile').addEventListener('change', (e) => { if (e.target.value) { bulk('link_profile', e.target.value); e.target.value = ''; } });
  $('#bulkClear').addEventListener('click', () => bulk('clear_override'));
  $('#bulkDelete').addEventListener('click', async () => {
    if (!await confirmModal('Delete ' + state.selected.size + ' selected post(s)?', 'Delete')) return;
    bulk('delete');
  });
  // Selected posts onto a map. The selection is the analyst's own choice, so
  // it goes across whole -- no verdict or score filter is applied to it.
  const bulkMap = $('#bulkLinkmap');
  if (bulkMap) {
    bulkMap.addEventListener('click', async () => {
      const ids = new Set(state.selected);
      if (!ids.size) { showToast('Select some posts first', 'warning'); return; }
      try {
        // Read the full rows from the store: `state.posts` holds only the
        // page on screen, and a selection can span several pages.
        const rows = [];
        for (const id of ids) {
          const row = await Store.get('posts', id);
          if (row) rows.push(row);
        }
        if (!rows.length) {
          showToast('Those posts are no longer in this browser', 'warning');
          return;
        }
        await LinkMapAdd.openAndShow({ kind: 'posts', watch, posts: rows });
      } catch (e) { showToast(e.message, 'danger'); }
    });
  }

  $('#bulkDeselect').addEventListener('click', () => { state.selected.clear(); render(); });
  const bulkPage = $('#bulkSelectPage');
  if (bulkPage) {
    bulkPage.addEventListener('click', () => {
      state.posts.forEach((p) => state.selected.add(p.id));
      render();
    });
  }

  /* ── Filters ──────────────────────────────────────────────────────────── */

  $('#legend').addEventListener('click', (e) => {
    const b = e.target.closest('[data-filter]');
    if (b) setFilter({ verdict: b.dataset.filter });
  });
  $('#q').addEventListener('input', debounce((e) => setFilter({ q: e.target.value }), 300));
  // Choosing a sort resets its direction to the one that reads naturally for
  // that field: worst-first for scores and newest-first for dates, but A-Z for
  // names -- nobody picks "Author" wanting Z first. The toggle still overrides.
  $('#sortSel').addEventListener('change', (e) => {
    const sort = e.target.value;
    setFilter({ sort, dir: NATURAL_DIR[sort] || 'desc' });
  });
  $('#platformSel').addEventListener('change', (e) => setFilter({ platform: e.target.value }));
  $('#statusSel').addEventListener('change', (e) => setFilter({ status: e.target.value }));
  const kindSel = $('#kindSel');
  if (kindSel) kindSel.addEventListener('change', (e) => setFilter({ kind: e.target.value }));
  // Switching to a preset window clears any range that was set, and away from
  // 'custom' clears it too -- a range left behind would keep narrowing the
  // preset with no visible sign of it.
  $('#daysSel').addEventListener('change', (e) => {
    const days = e.target.value;
    if (days === 'custom') {
      setFilter({ days, from: state.query.from, to: state.query.to });
      const from = $('#rangeFrom');
      if (from) from.focus();
    } else {
      setFilter({ days, from: '', to: '' });
    }
  });

  const dateField = $('#dateFieldSel');
  if (dateField) {
    dateField.addEventListener('change', (e) =>
      setFilter({ date_field: e.target.value }));
  }

  // Typing a date fires `change` on blur and on each spinner step; `input`
  // would re-query on every keystroke of a half-typed year.
  ['rangeFrom', 'rangeTo'].forEach((id) => {
    const el = $('#' + id);
    if (!el) return;
    el.addEventListener('change', () => {
      setFilter({
        days: 'custom',
        from: $('#rangeFrom') ? $('#rangeFrom').value : '',
        to: $('#rangeTo') ? $('#rangeTo').value : '',
      });
    });
  });

  const rangeClear = $('#rangeClear');
  if (rangeClear) {
    rangeClear.addEventListener('click', () =>
      setFilter({ days: '', from: '', to: '' }));
  }

  const sortDirBtn = $('#sortDirBtn');
  if (sortDirBtn) {
    sortDirBtn.addEventListener('click', () =>
      setFilter({ dir: state.query.dir === 'asc' ? 'desc' : 'asc' }));
  }

  $('#perPageSel').addEventListener('change', (e) =>
    setFilter({ per_page: parseInt(e.target.value, 10) || 25 }));
  $('#btnResetFilters').addEventListener('click', resetFilters);
  const pinnedOnly = $('#pinnedOnly');
  if (pinnedOnly) {
    pinnedOnly.addEventListener('change', (e) => setFilter({ pinned: e.target.checked }));
  }

  /* ── Saved filters ─────────────────────────────────────────────────────
     A preset is a named set of non-default filters. Presets are stored per
     watch, except those that name nothing watch-specific (no platform), which
     are saved against watch 0 and offered everywhere. */

  let FILTER_PRESETS = [];

  function presetIsPortable(f) {
    // A platform only exists within the watch that collected it; everything
    // else (verdict, status, date window, text) means the same anywhere.
    return !f.platform;
  }

  // A human-readable account of what a preset actually does, so the list is
  // readable without having to apply each one to find out.
  function describeFilters(f) {
    const bits = [];
    if (f.verdict && f.verdict !== 'all') bits.push(VERD[f.verdict].label);
    if (f.status) bits.push('status: ' + f.status);
    if (f.platform) bits.push(f.platform);
    if (f.days === 'custom') {
      bits.push(f.from && f.to ? shortStamp(f.from) + ' to ' + shortStamp(f.to)
        : f.from ? 'since ' + shortStamp(f.from) : 'up to ' + shortStamp(f.to));
    } else if (f.days) {
      bits.push(windowLabel(f.days).toLowerCase());
    }
    if (f.date_field) bits.push('by collected date');
    if (f.types) bits.push('type: ' + f.types);
    if (f.pinned) bits.push('pinned only');
    if (f.q) bits.push('“' + f.q + '”');
    if ((f.sort && f.sort !== FILTER_DEFAULTS.sort) ||
        (f.dir && f.dir !== FILTER_DEFAULTS.dir)) {
      const key = f.sort || FILTER_DEFAULTS.sort;
      const opt = $('#sortSel') && $('#sortSel').querySelector('option[value="' + key + '"]');
      bits.push('sorted by ' + (opt ? opt.textContent.toLowerCase() : key) +
        ((f.dir || FILTER_DEFAULTS.dir) === 'asc' ? ' (ascending)' : ''));
    }
    return bits.length ? bits.join(' · ') : 'No filters (everything)';
  }

  async function loadPresets() {
    try {
      const rows = await Store.all('saved_filters');
      FILTER_PRESETS = rows.filter((r) => r.watch_id === WATCH || r.watch_id === 0)
        .sort((a, b) => String(a.name).localeCompare(String(b.name)));
    } catch (e) {
      FILTER_PRESETS = [];
    }
    renderPresets();
  }

  // Two presets match when they narrow by the same things, so the active one
  // can be highlighted after a reload without storing which was last clicked.
  function sameFilters(a, b) {
    const ka = Object.keys(a), kb = Object.keys(b);
    return ka.length === kb.length && ka.every((k) => String(a[k]) === String(b[k]));
  }

  function renderPresets() {
    const el = $('#presetList');
    if (!el) return;
    const cur = activeFilters(state.query);
    if (!FILTER_PRESETS.length) {
      el.innerHTML = '<span class="mon-note mb-0">No saved filters yet. ' +
        'Set up a view, then save it.</span>';
    } else {
      el.innerHTML = FILTER_PRESETS.map((f) => {
        const on = sameFilters(cur, f.filters || {});
        return '<span class="mon-preset' + (on ? ' on' : '') + '">' +
          '<button type="button" data-preset="' + f.id + '" title="' +
            esc(describeFilters(f.filters || {})) + '">' +
            esc(f.name) + (f.watch_id === 0 ? ' <i class="fa fa-globe" title="Available in every watch"></i>' : '') +
          '</button>' +
          '<button type="button" class="x" data-preset-del="' + f.id +
            '" title="Delete this saved filter" aria-label="Delete ' + esc(f.name) + '">&times;</button>' +
          '</span>';
      }).join('');
    }
    const badge = $('#filterCount');
    if (badge) {
      const n = filterCount(state.query);
      badge.textContent = n ? n + ' active' : '';
      badge.style.display = n ? '' : 'none';
    }
  }

  async function savePreset() {
    const filters = activeFilters(state.query);
    if (!Object.keys(filters).length) {
      showToast('Set at least one filter before saving a view', 'info');
      return;
    }
    const name = (window.prompt('Name this filter', suggestPresetName(filters)) || '').trim();
    if (!name) return;

    const scope = presetIsPortable(filters) ? 0 : WATCH;
    // Saving over an existing name updates it, rather than quietly growing a
    // list of near-duplicates.
    const existing = FILTER_PRESETS.find(
      (f) => f.name.toLowerCase() === name.toLowerCase() && f.watch_id === scope);
    try {
      await Store.put('saved_filters', Object.assign({}, existing, {
        watch_id: scope, name, filters, updated_at: new Date().toISOString(),
      }));
      await loadPresets();
      showToast(existing ? 'Updated “' + name + '”' : 'Saved “' + name + '”', 'success');
    } catch (e) {
      showToast(e.message, 'danger');
    }
  }

  function suggestPresetName(f) {
    return describeFilters(f).replace(/ · /g, ', ').slice(0, 40);
  }

  async function applyPreset(id) {
    const f = FILTER_PRESETS.find((x) => String(x.id) === String(id));
    if (!f) return;
    // Keep the reading preferences unless the preset names its own.
    const base = { sort: state.query.sort, per_page: state.query.per_page };
    state.query = normalizeQuery(Object.assign(base, f.filters || {}));
    syncControls();
    state.selected.clear();
    syncUrl();
    persistFilters();
    await refresh();
  }

  async function deletePreset(id) {
    const f = FILTER_PRESETS.find((x) => String(x.id) === String(id));
    if (!f || !window.confirm('Delete the saved filter “' + f.name + '”?')) return;
    try {
      await Store.remove('saved_filters', f.id);
      await loadPresets();
      showToast('Deleted “' + f.name + '”', 'info');
    } catch (e) {
      showToast(e.message, 'danger');
    }
  }

  const btnSavePreset = $('#btnSavePreset');
  if (btnSavePreset) btnSavePreset.addEventListener('click', savePreset);

  const presetList = $('#presetList');
  if (presetList) {
    presetList.addEventListener('click', (e) => {
      const del = e.target.closest('[data-preset-del]');
      if (del) { deletePreset(del.dataset.presetDel); return; }
      const use = e.target.closest('[data-preset]');
      if (use) applyPreset(use.dataset.preset);
    });
  }

  // The back/forward buttons should move between filter views, not leave the
  // page, since the URL is what encodes the view.
  window.addEventListener('popstate', (e) => {
    const q = (e.state && e.state.q) || queryFromUrl() || defaultQuery();
    state.query = normalizeQuery(q);
    syncControls();
    state.selected.clear();
    refresh();
  });

  /* ── Mode toggles and settings ────────────────────────────────────────── */

  function wireToggle(cbId, togId) {
    const cb = $('#' + cbId), tog = $('#' + togId);
    cb.addEventListener('change', () => {
      tog.classList.toggle('on', cb.checked);
      if (cbId === 'm-release') $('#release-panel').style.display = cb.checked ? '' : 'none';
      applySettings(true).then(() => showToast(
        (cbId === 'm-release' ? 'Media Release Threat' : 'Digital Hunter') + ' mode ' + (cb.checked ? 'on' : 'off'),
        cb.checked ? 'success' : 'info'));
    });
  }
  wireToggle('m-release', 'tog-release');
  wireToggle('m-hunter', 'tog-hunter');

  // The keyword editor owns its own preview; wire the subject into it too.
  // The initial paint happens in boot(), once the watch has been loaded.
  wireKeywordEditor();
  if ($('#f-subject')) $('#f-subject').addEventListener('input', previewTopic);

  $('#btnApply').addEventListener('click', () => applySettings(false));
  $('#weightGrid').addEventListener('input', markChangedWeights);
  $('#btnResetWeights').addEventListener('click', () => {
    $$('[data-weight]').forEach((el) => { el.value = el.dataset.default; });
    markChangedWeights();
    applySettings(false);
  });
  $('#btnDeleteWatch').addEventListener('click', async () => {
    if (!await confirmModal('Delete this watch and every post in it? This ' +
        'removes them from this browser and cannot be undone.', 'Delete watch')) return;
    await Data.deleteWatch(WATCH);
    window.location = '/monitor/';
  });

  /* ── Collection ───────────────────────────────────────────────────────── */

  function selectedSources() {
    return $$('.mon-src-item.on').map((el) => el.dataset.src);
  }

  function syncUrlField() {
    const chosen = selectedSources();
    const needsUrl = chosen.some((k) => {
      const el = document.querySelector('.mon-src-item[data-src="' + k + '"]');
      return el && el.dataset.needsurl === '1';
    });
    $('#urlFields').style.display = needsUrl ? '' : 'none';

    // The Page name and comment options only mean anything to the Facebook
    // collector, so they stay out of the way until it is selected.
    const fb = $('#fbFields');
    if (fb) fb.style.display = chosen.includes('facebook') ? '' : 'none';
  }

  const cComments = $('#c-comments');
  if (cComments) {
    cComments.addEventListener('change', () => {
      const opts = $('#fbCommentOpts');
      if (opts) opts.style.display = cComments.checked ? '' : 'none';
    });
  }

  // The collapsed summary has to say what is selected, or it just hides the
  // setting rather than tidying it.
  function syncSourceSummary() {
    const on = $$('.mon-src-item.on');
    const count = $('#srcCount');
    const names = $('#srcNames');
    const summary = $('#srcToggle');
    if (!count || !names || !summary) return;

    count.textContent = on.length + ' selected';
    summary.classList.toggle('none-selected', on.length === 0);

    if (!on.length) {
      names.textContent = 'none — pick at least one to collect';
    } else {
      const labels = on.map((el) => el.dataset.name || el.dataset.src);
      names.textContent = labels.slice(0, 4).join(', ') +
        (labels.length > 4 ? ' +' + (labels.length - 4) + ' more' : '');
    }
    syncUrlField();
  }

  function setSource(el, on) {
    el.classList.toggle('on', on);
    el.setAttribute('aria-checked', on ? 'true' : 'false');
  }

  const PRESETS = {
    news: (el) => el.dataset.group === 'News' && el.dataset.available === '1',
    social: (el) => el.dataset.group === 'Social' && el.dataset.available === '1',
    // Everything that fetches without a key or a login.
    open: (el) => el.dataset.live === '1' && el.dataset.available === '1' &&
                  el.dataset.needsurl !== '1',
    none: () => false,
  };

  const srcToggle = $('#srcToggle');
  if (srcToggle) {
    srcToggle.addEventListener('click', () => {
      const body = $('#srcBody');
      const open = body.hidden;
      body.hidden = !open;
      srcToggle.setAttribute('aria-expanded', open ? 'true' : 'false');
    });
  }

  // Selecting sources. An unavailable source stays selectable: it reports why
  // it could not run, which is more useful than an inert tile.
  document.addEventListener('click', (e) => {
    const preset = e.target.closest('[data-preset]');
    if (preset) {
      e.preventDefault();
      const test = PRESETS[preset.dataset.preset];
      $$('.mon-src-item').forEach((el) => setSource(el, !!test && test(el)));
      syncSourceSummary();
      return;
    }

    const all = e.target.closest('[data-group-all]');
    const none = e.target.closest('[data-group-none]');
    if (all || none) {
      e.preventDefault();
      const host = (all || none).closest('.mon-src-group');
      host.querySelectorAll('.mon-src-item').forEach((el) => {
        setSource(el, Boolean(all) && el.dataset.available === '1');
      });
      syncSourceSummary();
      return;
    }

    const item = e.target.closest('.mon-src-item');
    if (item) {
      setSource(item, !item.classList.contains('on'));
      syncSourceSummary();
    }
  });

  // Tiles act as checkboxes, so Space and Enter must work on them too.
  document.addEventListener('keydown', (e) => {
    if (e.key !== ' ' && e.key !== 'Enter') return;
    const item = e.target.closest && e.target.closest('.mon-src-item');
    if (!item) return;
    e.preventDefault();
    setSource(item, !item.classList.contains('on'));
    syncSourceSummary();
  });

  syncSourceSummary();

  $$('.sugg').forEach((a) => a.addEventListener('click', (e) => {
    e.preventDefault();
    // Use the first anchor term, falling back to the subject.
    const kw = (state.keywords.required || [])[0] ||
      (state.keywords.optional || [])[0] ||
      ($('#f-subject') ? $('#f-subject').value.trim() : '');
    $('#c-url').value = a.dataset.url.replace('{query}', encodeURIComponent(kw));
  }));

  $('#btnCollect').addEventListener('click', async () => {
    const sources = selectedSources();
    if (!sources.length) {
      // Open the picker rather than just complaining about it.
      const body = $('#srcBody');
      if (body && body.hidden) {
        body.hidden = false;
        $('#srcToggle').setAttribute('aria-expanded', 'true');
        body.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
      }
      showToast('Pick at least one source to collect from', 'warning');
      return;
    }
    const btn = $('#btnCollect');
    btn.disabled = true;
    btn.innerHTML = '<i class="fa fa-spinner fa-spin me-1"></i> Collecting…';

    const limit = parseInt($('#c-limit').value, 10) || 25;
    const url = $('#c-url') ? $('#c-url').value.trim() : '';
    const days = $('#c-days') ? $('#c-days').value : '';
    const options = { limit };
    sources.forEach((k) => { options[k] = { limit, url }; });

    // Facebook takes a Page name and, optionally, the comment threads under
    // each post it finds there.
    if (sources.includes('facebook')) {
      const page = $('#c-page') ? $('#c-page').value.trim() : '';
      const wantComments = $('#c-comments') ? $('#c-comments').checked : false;
      const climit = parseInt($('#c-comment-limit') &&
        $('#c-comment-limit').value, 10) || 50;
      options.facebook = Object.assign(options.facebook || {}, {
        limit, url, page,
        with_comments: wantComments,
        comment_limit: climit,
      });
    }

    try {
      const data = await Data.collect(watch, sources, {
        query: $('#c-query').value.trim(), options, days,
        strict: true,
      });
      const rep = $('#collectReport');
      rep.style.display = '';
      rep.innerHTML = data.report.sources.map((r) => {
        const cls = r.ok ? 's-ok' : (r.blocked ? 's-blocked' : 's-bad');
        const icon = r.ok ? '✓' : (r.blocked ? '⚠' : '✕');
        // Facebook and X return targeted follow-up searches, because what the
        // index holds is only part of what is on the platform.
        const dorks = (r.dorks || []).length
          ? '<div class="mon-dorks">' +
            '<span class="lbl">Search directly:</span>' +
            r.dorks.map((d) => '<a href="' + esc(d.url) + '" target="_blank" ' +
              'rel="noopener noreferrer">' + esc(d.label) + '</a>').join('') +
            '</div>'
          : '';
        return '<div><span class="' + cls + '">' + icon + '</span>' +
          '<span class="src">' + esc(r.source) + '</span>' +
          '<span style="flex:1">' + esc(r.note) + '</span>' +
          (r.elapsed ? '<span class="ms">' + r.elapsed + 's</span>' : '') +
          (r.manual_url && !dorks ? '<a href="' + esc(r.manual_url) + '" target="_blank" rel="noopener noreferrer">open search</a>' : '') +
          '</div>' + dorks;
      }).join('') + '<div style="border:0"><span class="s-ok">→</span><span style="flex:1">' +
        '<b>' + data.added + '</b> added, ' + data.skipped + ' duplicate' +
        (data.off_topic ? ', <span class="s-blocked">' + data.off_topic + ' off-topic dropped</span>' : '') +
        (data.flagged ? ', <b style="color:var(--yellow)">' + data.flagged + ' flagged</b>' : '') +
        ' · ' + data.report.ok_sources + '/' +
        (data.report.ok_sources + data.report.failed_sources) + ' sources · ' +
        data.report.elapsed + 's</span></div>' +
        '<div style="border:0;opacity:.7"><span></span><span style="flex:1">Query: <code>' +
        esc(data.query) + '</code></span></div>';
      state.query.page = 1;
      syncUrl();
      await refresh();
      showToast(data.added + ' post(s) collected', data.added ? 'success' : 'info');
    } catch (e) {
      showToast(e.message, 'danger');
    } finally {
      btn.disabled = false;
      btn.innerHTML = '<i class="fa fa-satellite-dish me-1"></i> Collect now';
    }
  });

  $('#btnDorks').addEventListener('click', async () => {
    const kw = await Data.keywords(watch);
    const q = $('#c-query').value.trim() || kw.query;
    const sites = ['facebook.com', 'x.com', 'twitter.com', 'instagram.com',
      'tiktok.com', 'reddit.com', 'linkedin.com'];
    const data = { urls: [{ label: 'All sites',
      url: 'https://www.google.com/search?q=' + encodeURIComponent(q) }].concat(
      sites.map((site) => ({ label: site,
        url: 'https://www.google.com/search?q=' + encodeURIComponent('site:' + site + ' ' + q) }))) };
    $('#dorkList').innerHTML = data.urls.map((u) =>
      '<a class="btn btn-outline btn-sm text-start" href="' + esc(u.url) + '" target="_blank" rel="noopener noreferrer">' +
      '<i class="fa fa-arrow-up-right-from-square me-2"></i>' + esc(u.label) + '</a>').join('');
    new bootstrap.Modal('#dorksModal').show();
  });

  /* ── Add / import ─────────────────────────────────────────────────────── */

  function newPostBody() {
    return {
      platform: $('#np-platform').value,
      author: $('#np-author').value.trim(),
      handle: $('#np-handle').value.trim(),
      verified: $('#np-verified').checked,
      text: $('#np-text').value.trim(),
      url: $('#np-url').value.trim(),
    };
  }

  const addModal = new bootstrap.Modal('#addModal');
  $('#btnAddPost').addEventListener('click', () => {
    $('#np-err').textContent = ''; $('#np-preview').innerHTML = '';
    addModal.show();
  });

  $('#np-test').addEventListener('click', async () => {
    const post = newPostBody();
    if (!post.text) { $('#np-err').textContent = 'Add some post text to score.'; return; }
    try {
      const probe = Object.assign({}, watch, settingsPayload());
      const scored = await Data.scorePosts(probe, [Object.assign({}, post)]);
      const a = scored[0].analysis;
      $('#np-preview').innerHTML = '<div class="mon-post v-' + a.verdict + '" style="margin:0">' +
        '<div class="mon-top"><div><span class="mon-author">Preview</span></div>' +
        '<div class="mon-score"><b>' + a.score + '</b><span>risk / 100</span></div></div>' +
        '<div class="mon-foot"><span class="mon-verdict">' + a.verdict_label + '</span>' +
        a.types.map((t) => '<span class="mon-tag">' + esc(t) + '</span>').join('') + '</div>' +
        '<div class="mon-evidence" style="margin-top:10px">' + ledger(a) + '</div></div>';
    } catch (e) { $('#np-err').textContent = e.message; }
  });

  $('#np-save').addEventListener('click', async () => {
    const post = newPostBody();
    if (!post.author || !post.text) { $('#np-err').textContent = 'Add a display name and the post text.'; return; }
    try {
      // Manual adds honour the watch's relevance rules, so say which rule
      // rejected the post rather than just refusing it.
      const stats = await Data.addPosts(watch, [post], { source: 'manual' });
      if (!stats.added) {
        if (stats.off_topic) {
          const kw = await Data.keywords(watch, post.text);
          const why = (kw.sample_result && kw.sample_result.reason) || '';
          $('#np-err').innerHTML = 'This post is off-topic for the watch' +
            (why ? ': ' + esc(why) : '') +
            '. <button type="button" class="btn btn-ghost btn-xs" id="np-force">' +
            'Add it anyway</button>';
          const force = $('#np-force');
          if (force) {
            force.addEventListener('click', async () => {
              const forced = await Data.addPosts(watch, [post],
                { source: 'manual', strict: false });
              if (forced.added) {
                addModal.hide();
                await refresh();
                showToast('Post added despite being off-topic', 'info');
              }
            });
          }
        } else {
          $('#np-err').textContent = 'That post is already in this watch.';
        }
        return;
      }
      ['np-author', 'np-handle', 'np-url', 'np-text'].forEach((i) => { $('#' + i).value = ''; });
      $('#np-verified').checked = false;
      $('#np-preview').innerHTML = '';
      addModal.hide();
      await refresh();
      showToast('Post added and scored', 'success');
    } catch (e) { $('#np-err').textContent = e.message; }
  });

  const impModal = new bootstrap.Modal('#importModal');
  $('#btnImport').addEventListener('click', () => { $('#imp-err').textContent = ''; impModal.show(); });
  $('#imp-save').addEventListener('click', async () => {
    try {
      let payload;
      try { payload = JSON.parse($('#imp-text').value); }
      catch (err) { throw new Error("That isn't valid JSON. Check for missing quotes or commas."); }
      const arr = Array.isArray(payload) ? payload : (payload && payload.posts);
      if (!Array.isArray(arr)) {
        throw new Error('Expected an array of posts, or an object with a posts array.');
      }
      if ($('#imp-replace').checked) await Store.deletePostsFor(WATCH);
      const data = await Data.addPosts(watch, arr, { source: 'import' });
      impModal.hide();
      $('#imp-text').value = '';
      await refresh();
      showToast(data.added + ' imported, ' + data.skipped + ' skipped', 'success');
    } catch (e) { $('#imp-err').textContent = e.message; }
  });

  /* ── Sandbox ──────────────────────────────────────────────────────────── */

  const sbModal = new bootstrap.Modal('#sandboxModal');
  $('#btnSandbox').addEventListener('click', () => sbModal.show());

  const runSandbox = debounce(async () => {
    const text = $('#sb-text').value.trim();
    if (!text) { $('#sb-out').innerHTML = ''; return; }
    try {
      const probe = Object.assign({}, watch, settingsPayload());
      const scored = await Data.scorePosts(probe, [{
        platform: $('#sb-platform').value, author: $('#sb-author').value,
        handle: $('#sb-handle').value, text,
      }]);
      const a = scored[0].analysis;
      $('#sb-out').innerHTML = '<div class="mon-post v-' + a.verdict + '" style="margin:0">' +
        '<div class="mon-top"><div><span class="mon-author">' + a.verdict_label + '</span>' +
        '<span class="mon-meta">' + (a.types.join(', ') || 'no threat types') + '</span></div>' +
        '<div class="mon-score"><b>' + a.score + '</b><span>risk / 100</span></div></div>' +
        '<div class="mon-evidence" style="margin-top:10px">' + ledger(a) +
        '<div class="mon-action"><b>Recommended:</b> ' + esc(a.recommendation) + '</div></div></div>';
    } catch (e) { $('#sb-out').innerHTML = '<p class="text-danger" style="font-size:12px">' + esc(e.message) + '</p>'; }
  }, 350);
  ['sb-text', 'sb-author', 'sb-handle', 'sb-platform'].forEach((id) =>
    $('#' + id).addEventListener('input', runSandbox));
  $('#sb-platform').addEventListener('change', runSandbox);

  /* ── Hunter suggestions ───────────────────────────────────────────────── */

  const btnSuggest = $('#btnSuggest');
  if (btnSuggest) {
    btnSuggest.addEventListener('click', async () => {
      const body = $('#suggestBody');
      body.innerHTML = '<p class="mon-note mb-0">Scanning…</p>';
      try {
        const data = await suggestProfiles();
        if (!data.suggestions.length) {
          body.innerHTML = '<p class="mon-note mb-0">No profile matches found in the current posts.</p>';
          return;
        }
        body.innerHTML = data.suggestions.map((s) =>
          '<div class="mon-row" style="grid-template-columns:1fr auto auto;gap:10px">' +
          '<div><span class="lbl">' + esc(s.codename) + '</span>' +
          '<span class="det">' + s.count + ' post(s), best match ' + Math.round(s.best * 100) + '% — ' +
          esc(s.posts[0].kind) + ' "' + esc(s.posts[0].matched) + '"</span></div>' +
          '<div class="mon-w">' + Math.round(s.best * 100) + '%</div>' +
          '<button class="btn btn-outline btn-xs" data-link-profile="' + s.profile_id + '" ' +
          'data-posts="' + s.posts.map((p) => p.post_id).join(',') + '">Link all</button></div>').join('');
      } catch (e) {
        body.innerHTML = '<p class="text-danger mb-0" style="font-size:12px">' + esc(e.message) + '</p>';
      }
    });

    $('#suggestBody').addEventListener('click', async (e) => {
      const btn = e.target.closest('[data-link-profile]');
      if (!btn) return;
      const ids = btn.dataset.posts.split(',').map(Number);
      try {
        await Data.bulk(WATCH, ids, 'link_profile', btn.dataset.linkProfile);
        showToast('Linked ' + ids.length + ' post(s)', 'success');
        await refresh();
      } catch (err) { showToast(err.message, 'danger'); }
    });
  }

  /* ── Link map ─────────────────────────────────────────────────────────── */
  //
  // The dialog, the merge behaviour and the preview all live in
  // linkmap_integrate.js, so the watch page, the post list and the profile
  // page reach exactly the same flow rather than three drifting copies.

  $('#btnLinkmap').addEventListener('click', () => {
    LinkMapAdd.openAndShow({ kind: 'watch', watch });
  });

  /* ── Capabilities ─────────────────────────────────────────────────────── */

  const btnCaps = $('#btnCaps');
  if (btnCaps) {
    btnCaps.addEventListener('click', async (e) => {
      e.preventDefault();
      const modal = new bootstrap.Modal('#capsModal');
      modal.show();
      const body = $('#capsBody');
      try {
        const d = await api('/capabilities');
        const groups = Object.entries(d.groups || {});
        body.innerHTML =
          '<p class="mon-note">' + d.counts.available + ' of ' + d.counts.total +
          ' optional libraries are usable here' +
          (d.serverless ? ', and this is a serverless deployment, so anything ' +
            'needing a browser or a writable disk cannot run' : '') + '.</p>' +
          groups.map(([name, rows]) =>
            '<h6 class="mt-3" style="text-transform:capitalize">' + esc(name) + '</h6>' +
            '<div class="mon-caps">' + rows.map((r) =>
              '<div class="mon-cap ' + (r.available ? 'on' : 'off') + '">' +
              '<div class="t"><i class="fa ' + (r.available ? 'fa-circle-check' : 'fa-circle-xmark') +
              '"></i> ' + esc(r.label) + '<span class="role">' + esc(r.role) + '</span></div>' +
              '<div class="u">' + esc(r.use) + '</div>' +
              (r.available ? '' : '<div class="why">' + esc(r.reason) + '</div>') +
              (r.needs ? '<div class="why">Needs: ' + esc(r.needs) + '</div>' : '') +
              (r.caveat ? '<div class="why warn">' + esc(r.caveat) + '</div>' : '') +
              '</div>').join('') + '</div>').join('');
      } catch (err) {
        body.innerHTML = '<p class="text-danger" style="font-size:12px">' + esc(err.message) + '</p>';
      }
    });
  }

  /* ── Authenticated collection ─────────────────────────────────────────── */

  async function initAuth() {
    const row = $('#authRow');
    if (!row) return;
    let status;
    try { status = await fetch('/monitor/vault/status').then((r) => r.json()); }
    catch (e) { return; }
    if (!status.count) return;  // nothing stored, keep the UI simple

    row.style.display = '';
    const sel = $('#authCred');
    if (!status.unlocked) {
      sel.innerHTML = '<option>— vault locked —</option>';
      sel.disabled = true;
      $('#btnAuthCollect').disabled = true;
      $('#authHint').innerHTML = '<a class="mon-out" href="/monitor/vault">Unlock the vault</a> ' +
        'to collect from logged-in sources.';
      return;
    }
    const usable = (status.credentials || []).filter((c) => c.enabled);
    if (!usable.length) { row.style.display = 'none'; return; }
    sel.innerHTML = usable.map((c) =>
      '<option value="' + c.id + '">' + esc(c.label) + ' (' + esc(c.platform) + ')</option>').join('');
    $('#authHint').innerHTML = 'Uses a stored session. If the platform challenges it, ' +
      'the report says so — collect manually in that case.' +
      (status.playwright ? '' : ' Browser mode needs Playwright installed.');
  }

  const btnAuth = $('#btnAuthCollect');
  if (btnAuth) {
    btnAuth.addEventListener('click', async () => {
      const cid = $('#authCred').value;
      if (!cid) { showToast('Pick a credential', 'warning'); return; }
      btnAuth.disabled = true;
      btnAuth.innerHTML = '<i class="fa fa-spinner fa-spin me-1"></i> Collecting…';
      try {
        const known = Array.from(await Store.existingKeys(WATCH));
        const data = await api('/api/collect-auth', {
          method: 'POST',
          body: JSON.stringify({
            watch, credential_id: cid,
            query: $('#c-query').value.trim(),
            url: $('#authUrl').value.trim(),
            use_browser: $('#authBrowser').checked,
            limit: parseInt($('#c-limit').value, 10) || 25,
            known_keys: known, strict: true,
          }),
        });
        // Secrets stayed on the server; the posts come back here to be stored.
        if (data.posts && data.posts.length) {
          await Store.putMany('posts', data.posts.map((p) => Object.assign(p, {
            watch_id: WATCH, rules_hash: data.rules_hash,
          })));
          await Data.saveWatch(watch);
        }
        const rep = $('#collectReport');
        rep.style.display = '';
        const cls = data.ok ? 's-ok' : (data.blocked ? 's-blocked' : 's-bad');
        const icon = data.ok ? '✓' : (data.blocked ? '⚠' : '✕');
        rep.innerHTML = '<div><span class="' + cls + '">' + icon + '</span>' +
          '<span class="src">logged-in</span><span style="flex:1">' + esc(data.note) +
          (data.strategy ? ' [' + esc(data.strategy) + ']' : '') + '</span>' +
          (data.manual_url ? '<a href="' + esc(data.manual_url) + '" target="_blank" rel="noopener noreferrer">open search</a>' : '') +
          '</div><div style="border:0"><span class="s-ok">→</span><span style="flex:1">' +
          data.added + ' added, ' + data.skipped + ' duplicate/skipped</span></div>';
        await refresh();
        showToast(data.added + ' post(s) collected', data.added ? 'success' : 'info');
      } catch (e) {
        showToast(e.message, 'danger');
      } finally {
        btnAuth.disabled = false;
        btnAuth.innerHTML = '<i class="fa fa-user-lock me-1"></i> Collect logged-in';
      }
    });
  }
  initAuth();

  /* ── Hide the AI button when no key is configured ─────────────────────── */

  api('/ai-status').then((s) => {
    if (!s.enabled) {
      const style = document.createElement('style');
      style.textContent = '[data-act="ai"]{display:none}';
      document.head.appendChild(style);
    }
  }).catch(() => {});

  /* ── Profile suggestions (Hunter) ─────────────────────────────────────
     The correlation itself is done by the scoring engine; this just groups
     the matches it already attached to each post. */

  async function suggestProfiles() {
    const probe = Object.assign({}, watch, { mode_hunter: true });
    const rows = await Store.byIndex('posts', 'watch_id', IDBKeyRange.only(WATCH));
    const scored = await Data.scorePosts(probe, rows.map((r) => Object.assign({}, r)));
    const agg = {};
    scored.forEach((p, i) => {
      ((p.analysis || {}).profile_matches || []).forEach((m) => {
        const e = agg[m.profile_id] || (agg[m.profile_id] = {
          profile_id: m.profile_id, codename: m.codename, posts: [], best: 0,
        });
        e.best = Math.max(e.best, m.confidence);
        e.posts.push({
          post_id: rows[i].id, author: p.author, confidence: m.confidence,
          matched: m.matched, kind: m.kind, via: m.via, score: p.score,
          linked: rows[i].profile_id === m.profile_id,
        });
      });
    });
    const out = Object.values(agg).sort((a, b) => b.best - a.best);
    out.forEach((e) => {
      e.posts.sort((a, b) => b.confidence - a.confidence);
      e.count = e.posts.length;
    });
    return { suggestions: out };
  }

  /* ── Export ───────────────────────────────────────────────────────────
     Exports are built here rather than server-side, because the posts only
     exist in this browser. */

  function download(name, text, mime) {
    const blob = new Blob([text], { type: mime });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = name;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  // A leading =, +, - or @ makes a spreadsheet treat a cell as a formula.
  function csvCell(v) {
    let str = v == null ? '' : String(v);
    if (/^[=+\-@\t\r]/.test(str)) str = "'" + str;
    return '"' + str.replace(/"/g, '""') + '"';
  }

  // Exports follow the view: whatever the filters select is what lands in the
  // file, in the same order as on screen -- a filtered workspace and its export
  // should never disagree. Shift-click exports the whole watch instead, for a
  // full backup.
  async function exportRows(all) {
    if (all) {
      const rows = await Store.byIndex('posts', 'watch_id', IDBKeyRange.only(WATCH));
      rows.sort((a, b) => Store.effScore(b) - Store.effScore(a));
      return rows;
    }
    return Data.allMatching(watch, state.query);
  }

  // A filename that says which slice this is, so several exports from one watch
  // do not overwrite each other or get mixed up later.
  function exportName(ext, all) {
    const bits = ['signal-monitor', String(WATCH)];
    if (!all) {
      const f = activeFilters(state.query);
      ['verdict', 'status', 'platform'].forEach((k) => {
        if (f[k]) bits.push(String(f[k]).toLowerCase().replace(/[^a-z0-9]+/g, '-'));
      });
      // A window in the filename, so two exports taken minutes apart over
      // different ranges do not overwrite each other.
      if (f.days === 'custom') bits.push('range');
      else if (f.days) bits.push(/h$/.test(f.days) ? f.days : f.days + 'd');
      if (f.pinned) bits.push('pinned');
      if (f.q || f.types) bits.push('search');
    }
    bits.push(new Date().toISOString().slice(0, 10));
    return bits.join('-') + '.' + ext;
  }

  function exportNote(rows, all) {
    if (all) return 'Exported all ' + rows.length + ' post(s) in this watch';
    return 'Exported ' + rows.length + ' post(s)' +
      (filterCount(state.query) ? ' matching the current view' : '');
  }

  async function exportCsv(e) {
    const all = !!(e && e.shiftKey);
    const rows = await exportRows(all);
    if (!rows.length) {
      showToast('Nothing to export in this view', 'info');
      return;
    }
    const head = ['platform', 'author', 'handle', 'verified', 'risk_score',
      'verdict', 'overridden', 'threat_types', 'signals', 'links', 'profile',
      'status', 'analyst_note', 'url', 'posted_at', 'source', 'text'];
    const profiles = await Store.all('profiles');
    const byId = Object.fromEntries(profiles.map((x) => [x.id, x.codename]));
    const lines = [head.join(',')];
    rows.forEach((p) => {
      const a = p.analysis || {};
      lines.push([
        p.platform, p.author, p.handle, p.verified, Store.effScore(p),
        p.verdict_label || '', p.manual_score != null || !!p.manual_verdict,
        (p.types || []).join('; '),
        (a.signals || []).map((x) => x.label + ' (' + (x.w > 0 ? '+' : '') + x.w + ')').join('; '),
        (a.links || []).map((l) => l.display).join(' '),
        byId[p.profile_id] || '', p.status, p.analyst_note, p.url,
        p.posted_at || '', p.source, p.text,
      ].map(csvCell).join(','));
    });
    download(exportName('csv', all), lines.join('\n'), 'text/csv');
    showToast(exportNote(rows, all), 'success');
  }

  async function exportJson(e) {
    const all = !!(e && e.shiftKey);
    const rows = await exportRows(all);
    if (!rows.length) {
      showToast('Nothing to export in this view', 'info');
      return;
    }
    download(exportName('json', all), JSON.stringify({
      watch,
      exported_at: new Date().toISOString(),
      // Recording the filters makes the file self-describing: a reader can see
      // that it is a slice, and which one, rather than assuming it is the lot.
      scope: all ? 'all posts in watch' : 'current filtered view',
      filters: all ? {} : activeFilters(state.query),
      filters_described: all ? 'none' : describeFilters(activeFilters(state.query)),
      count: rows.length,
      posts: rows,
    }, null, 2), 'application/json');
    showToast(exportNote(rows, all), 'success');
  }

  /* ── Boot ─────────────────────────────────────────────────────────────
     Everything above assumes `watch` and the form fields are populated, so
     nothing runs until the watch has been read out of IndexedDB. */

  function fillForm() {
    $('#watchName').textContent = watch.name;
    document.title = watch.name + ' — Signal Monitor';

    const chips = [];
    if (watch.mode_release) {
      chips.push('<span class="mon-mode-chip release"><i class="fa fa-bullhorn"></i> Media Release Threat</span>');
    }
    if (watch.mode_hunter) {
      chips.push('<span class="mon-mode-chip hunter"><i class="fa fa-crosshairs"></i> Digital Hunter</span>');
    }
    if (!chips.length) chips.push('<span class="mon-mode-chip std">Standard</span>');
    if (watch.subject) {
      chips.push('<span class="page-sub mb-0" style="font-size:12px">Subject: ' +
        esc(watch.subject) + '</span>');
    }
    $('#watchChips').innerHTML = chips.join('');

    $('#f-subject').value = watch.subject || '';
    $('#m-release').checked = !!watch.mode_release;
    $('#m-hunter').checked = !!watch.mode_hunter;
    $('#tog-release').classList.toggle('on', !!watch.mode_release);
    $('#tog-hunter').classList.toggle('on', !!watch.mode_hunter);
    $('#release-panel').style.display = watch.mode_release ? '' : 'none';
    const hunter = $('#hunterPanel');
    if (hunter) hunter.style.display = watch.mode_hunter ? '' : 'none';

    if ($('#f-reference')) $('#f-reference').value = watch.reference_text || '';
    if ($('#f-accounts')) $('#f-accounts').value = watch.official_accounts || '';
    if ($('#f-domains')) $('#f-domains').value = watch.official_domains || '';
    $('#f-flags').value = watch.custom_flags || '';
    $('#f-t-review').value = watch.threshold_review != null ? watch.threshold_review : 30;
    $('#f-t-high').value = watch.threshold_high != null ? watch.threshold_high : 60;

    const weights = watch.weights || {};
    $$('[data-weight]').forEach((el) => {
      const k = el.dataset.weight;
      el.value = weights[k] != null ? weights[k] : el.dataset.default;
    });

    // Keyword buckets are stored newline-separated.
    const split = (v) => String(v || '').split('\n').map((t) => t.trim()).filter(Boolean);
    state.keywords = {
      required: split(watch.kw_required),
      optional: split(watch.kw_optional),
      excluded: split(watch.kw_excluded),
      match_mode: watch.kw_match_mode || 'all',
    };

    const opts = '<option value="">— no profile —</option>' + PROFILES.map((p) =>
      '<option value="' + p.id + '">' + esc(p.codename) + '</option>').join('');
    const sel = $('#f-profile');
    if (sel) {
      sel.innerHTML = opts;
      sel.value = watch.profile_id || '';
    }
    const bulkProf = $('#bulkProfile');
    if (bulkProf) {
      bulkProf.innerHTML = '<option value="">Link to profile…</option>' +
        PROFILES.map((p) => '<option value="' + p.id + '">' + esc(p.codename) + '</option>').join('');
    }
  }

  async function boot() {
    if (!WATCH) {
      $('#list').innerHTML = '<div class="mon-empty">No watch id in the URL. ' +
        '<a class="mon-out" href="/monitor/">Back to all watches</a>.</div>';
      return;
    }
    watch = await Store.get('watches', WATCH);
    if (!watch) {
      $('#list').innerHTML = '<div class="mon-empty">' +
        'That watch is not in this browser. It may have been created in another ' +
        'browser or profile, or the site data was cleared. ' +
        '<a class="mon-out" href="/monitor/">Back to all watches</a>.</div>';
      $('#watchName').textContent = 'Watch not found';
      return;
    }
    PROFILES = await Store.all('profiles');

    // Filter precedence: an explicit URL (a shared or bookmarked link) wins,
    // then whatever this analyst last had open on this watch, then defaults.
    let restored = queryFromUrl();
    let fromUrl = !!restored;
    if (!restored) {
      try { restored = await Store.setting(LAST_FILTER_KEY, null); } catch (e) { /* first run */ }
    }
    state.query = normalizeQuery(restored || {});
    syncControls();
    if (!fromUrl) syncUrl();

    await loadPresets();

    fillForm();
    renderChips();
    markChangedWeights();
    previewTopic();
    await refresh();

    if (!fromUrl && filterCount(state.query)) {
      showToast('Restored your last filters · ' + describeFilters(activeFilters(state.query)), 'info');
    }
  }

  $('#btnCsv').addEventListener('click', exportCsv);
  $('#btnJson').addEventListener('click', exportJson);

  boot().catch((e) => {
    const list = $('#list');
    if (list) {
      list.innerHTML = '<div class="mon-empty">Could not read this browser&#39;s ' +
        'storage. ' + esc(e.message) + '</div>';
    }
    const name = $('#watchName');
    if (name) name.textContent = 'Storage unavailable';
  });
})();

