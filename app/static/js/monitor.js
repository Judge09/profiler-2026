/* Signal Monitor — watch workspace.
 *
 * Scoring happens server-side; this file renders results, handles triage,
 * and drives collection. State lives in `state`, and render() is the single
 * path that paints the list.
 */
(function () {
  'use strict';

  const WATCH = window.MONITOR.watchId;
  const PROFILES = window.MONITOR.profiles;
  const STATUSES = window.MONITOR.statuses;
  const DEFAULT_WEIGHTS = window.MONITOR.defaultWeights;
  const API = '/monitor';

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

  const state = {
    posts: [], counts: { bad: 0, warn: 0, ok: 0 },
    filter: 'all', sort: 'risk', q: '',
    open: new Set(), selected: new Set(), ai: {},
  };

  async function api(path, opts) {
    const res = await fetch(API + path, Object.assign({
      headers: { 'Content-Type': 'application/json' },
    }, opts || {}));
    let data = {};
    try { data = await res.json(); } catch (e) { /* non-JSON error page */ }
    if (!res.ok) throw new Error(data.error || ('Request failed (' + res.status + ')'));
    return data;
  }

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
      keywords: $('#f-keywords').value,
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
      await api('/' + WATCH + '/update', {
        method: 'POST', body: JSON.stringify(settingsPayload()),
      });
      await refresh();
      if (!quiet) showToast('Rescored with the new settings', 'success');
    } catch (e) {
      showToast(e.message, 'danger');
    }
  }

  /* ── Data ─────────────────────────────────────────────────────────────── */

  async function refresh() {
    const data = await api('/' + WATCH + '/results');
    state.posts = data.posts;
    state.counts = data.counts;
    render();
    renderBriefing();
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
    const t = Date.parse(iso.length <= 10 ? iso + 'T00:00:00' : iso);
    if (isNaN(t)) return iso;
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
    const c = state.counts, total = state.posts.length;

    $('#sumTitle').textContent = !total ? 'No posts yet'
      : c.bad ? c.bad + (c.bad === 1 ? ' post needs' : ' posts need') + ' action now'
      : c.warn ? c.warn + (c.warn === 1 ? ' post needs' : ' posts need') + ' a closer look'
      : 'Nothing flagged';
    $('#scanMeta').textContent = total
      ? 'Scored ' + total + ' post' + (total === 1 ? '' : 's') + ' at ' + new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) + '.'
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
    ].map(([k, l, n, col]) => '<button data-filter="' + k + '" aria-pressed="' + (state.filter === k) + '">' +
      (col ? '<i style="background:' + col + '"></i>' : '') + l + ' <b>' + n + '</b></button>').join('');

    const q = state.q.toLowerCase();
    let list = state.posts.filter((p) => {
      const okFilter = state.filter === 'all' || p.analysis.verdict === state.filter;
      // Search covers threat types and status too, so briefing chips can filter.
      const hay = (p.author + ' ' + p.handle + ' ' + p.text + ' ' + p.platform + ' ' +
        (p.analysis.types || []).join(' ') + ' ' + (p.status || '')).toLowerCase();
      return okFilter && (!q || hay.indexOf(q) !== -1);
    });

    const bySort = {
      risk: (x, y) => y.analysis.score - x.analysis.score,
      recent: (x, y) => String(y.posted_at || y.collected_at).localeCompare(String(x.posted_at || x.collected_at)),
      platform: (x, y) => x.platform.localeCompare(y.platform) || y.analysis.score - x.analysis.score,
      status: (x, y) => String(x.status).localeCompare(String(y.status)) || y.analysis.score - x.analysis.score,
    };
    list.sort((x, y) => (y.pinned - x.pinned) || bySort[state.sort](x, y));

    $('#list').innerHTML = list.length ? list.map(card).join('')
      : '<div class="mon-empty">' + (total ? 'No posts match this view.' : 'No posts yet. Collect some above.') + '</div>';

    $('#selCount').textContent = state.selected.size;
    $('#bulkBar').style.display = state.selected.size ? '' : 'none';
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
    try { b = await api('/' + WATCH + '/briefing'); }
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
      state.q = tf.dataset.typeFilter;
      $('#q').value = state.q;
      state.filter = 'all';
      render();
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
      await api('/posts/' + id, { method: 'PATCH', body: JSON.stringify(body) });
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
      try { await api('/posts/' + id, { method: 'DELETE' }); state.open.delete(id); state.selected.delete(id); await refresh(); }
      catch (err) { showToast(err.message, 'danger'); }
    } else if (act === 'pin') {
      const p = state.posts.find((x) => x.id === id);
      try { await api('/posts/' + id, { method: 'PATCH', body: JSON.stringify({ pinned: !p.pinned }) }); await refresh(); }
      catch (err) { showToast(err.message, 'danger'); }
    } else if (act === 'ai') {
      askClaude(id);
    } else if (act === 'push') {
      const p = state.posts.find((x) => x.id === id);
      if (!p.profile_id) { showToast('Link this post to a profile first', 'warning'); return; }
      try {
        await api('/posts/' + id + '/push-note', { method: 'POST', body: JSON.stringify({ profile_id: p.profile_id }) });
        showToast('Added to ' + p.profile_codename + "'s intel notes", 'success');
      } catch (err) { showToast(err.message, 'danger'); }
    }
  });

  /* ── Bulk actions ─────────────────────────────────────────────────────── */

  async function bulk(action, value) {
    try {
      const data = await api('/' + WATCH + '/posts/bulk', {
        method: 'POST',
        body: JSON.stringify({ ids: Array.from(state.selected), action, value }),
      });
      showToast(data.affected + ' post(s) updated', 'success');
      if (action === 'delete') state.selected.clear();
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
  $('#bulkDeselect').addEventListener('click', () => { state.selected.clear(); render(); });

  /* ── Filters ──────────────────────────────────────────────────────────── */

  $('#legend').addEventListener('click', (e) => {
    const b = e.target.closest('[data-filter]');
    if (b) { state.filter = b.dataset.filter; render(); }
  });
  $('#q').addEventListener('input', debounce((e) => { state.q = e.target.value; render(); }, 150));
  $('#sortSel').addEventListener('change', (e) => { state.sort = e.target.value; render(); });

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

  // Show what the keywords actually scope to, as they are typed.
  const previewTopic = debounce(async () => {
    const el = $('#topicPreview');
    if (!el) return;
    try {
      const t = await api('/' + WATCH + '/topic?keywords=' +
        encodeURIComponent($('#f-keywords').value) +
        '&subject=' + encodeURIComponent($('#f-subject').value));
      if (t.warning) {
        el.innerHTML = '<span style="color:var(--yellow)"><i class="fa fa-triangle-exclamation me-1"></i>' +
          esc(t.warning) + '</span>';
        return;
      }
      el.innerHTML = 'On-topic if it mentions ' +
        t.anchors.map((a) => '<span class="mon-kbd" style="color:var(--accent)">' + esc(a) + '</span>').join(' or ') +
        (t.generic.length ? ' · broadened by ' +
          t.generic.map((g) => '<span class="mon-kbd">' + esc(g) + '</span>').join(' ') : '');
    } catch (e) { el.textContent = ''; }
  }, 400);
  ['f-keywords', 'f-subject'].forEach((id) => {
    const el = $('#' + id);
    if (el) el.addEventListener('input', previewTopic);
  });
  previewTopic();

  $('#btnApply').addEventListener('click', () => applySettings(false));
  $('#weightGrid').addEventListener('input', markChangedWeights);
  $('#btnResetWeights').addEventListener('click', () => {
    $$('[data-weight]').forEach((el) => { el.value = el.dataset.default; });
    markChangedWeights();
    applySettings(false);
  });
  markChangedWeights();

  $('#btnDeleteWatch').addEventListener('click', async () => {
    if (!await confirmModal('Delete this watch and every post in it?', 'Delete watch')) return;
    await api('/' + WATCH + '/delete', { method: 'POST' });
    window.location = API + '/';
  });

  /* ── Collection ───────────────────────────────────────────────────────── */

  function selectedSources() {
    return $$('.mon-src-item.on').map((el) => el.dataset.src);
  }

  $('#srcGrid').addEventListener('click', (e) => {
    const item = e.target.closest('.mon-src-item');
    if (!item) return;
    item.classList.toggle('on');
    const needsUrl = selectedSources().some((k) => {
      const el = document.querySelector('.mon-src-item[data-src="' + k + '"]');
      return el && el.dataset.needsurl === '1';
    });
    $('#urlFields').style.display = needsUrl ? '' : 'none';
  });

  $$('.sugg').forEach((a) => a.addEventListener('click', (e) => {
    e.preventDefault();
    const kw = $('#f-keywords').value.split(',')[0].trim() || $('#f-subject').value.trim();
    $('#c-url').value = a.dataset.url.replace('{query}', encodeURIComponent(kw));
  }));

  $('#btnCollect').addEventListener('click', async () => {
    const sources = selectedSources();
    if (!sources.length) { showToast('Pick at least one source', 'warning'); return; }
    const btn = $('#btnCollect');
    btn.disabled = true;
    btn.innerHTML = '<i class="fa fa-spinner fa-spin me-1"></i> Collecting…';

    const limit = parseInt($('#c-limit').value, 10) || 25;
    const url = $('#c-url') ? $('#c-url').value.trim() : '';
    const options = { limit };
    sources.forEach((k) => { options[k] = { limit, url }; });

    try {
      const data = await api('/' + WATCH + '/collect', {
        method: 'POST',
        body: JSON.stringify({ sources, query: $('#c-query').value.trim(), options }),
      });
      const rep = $('#collectReport');
      rep.style.display = '';
      rep.innerHTML = data.report.sources.map((r) => {
        const cls = r.ok ? 's-ok' : (r.blocked ? 's-blocked' : 's-bad');
        const icon = r.ok ? '✓' : (r.blocked ? '⚠' : '✕');
        return '<div><span class="' + cls + '">' + icon + '</span>' +
          '<span class="src">' + esc(r.source) + '</span>' +
          '<span style="flex:1">' + esc(r.note) + '</span>' +
          (r.manual_url ? '<a href="' + esc(r.manual_url) + '" target="_blank" rel="noopener noreferrer">open search</a>' : '') +
          '</div>';
      }).join('') + '<div style="border:0"><span class="s-ok">→</span><span style="flex:1">' +
        data.added + ' added, ' + data.skipped + ' duplicate' +
        (data.off_topic ? ', <span class="s-blocked">' + data.off_topic + ' off-topic dropped</span>' : '') +
        ' · query: ' + esc(data.query) + ' · ' + data.report.elapsed + 's</span></div>';
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
    const data = await api('/' + WATCH + '/dorks?q=' + encodeURIComponent($('#c-query').value.trim()));
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
      const a = await api('/preview', {
        method: 'POST',
        body: JSON.stringify({ watch_id: WATCH, watch: settingsPayload(), post }),
      });
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
      await api('/' + WATCH + '/posts', { method: 'POST', body: JSON.stringify(post) });
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
      const data = await api('/' + WATCH + '/import', {
        method: 'POST',
        body: JSON.stringify({ payload: $('#imp-text').value, replace: $('#imp-replace').checked }),
      });
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
      const a = await api('/preview', {
        method: 'POST',
        body: JSON.stringify({
          watch_id: WATCH, watch: settingsPayload(),
          post: {
            platform: $('#sb-platform').value, author: $('#sb-author').value,
            handle: $('#sb-handle').value, text,
          },
        }),
      });
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
        const data = await api('/' + WATCH + '/suggest-profiles');
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
        await api('/' + WATCH + '/posts/bulk', {
          method: 'POST',
          body: JSON.stringify({ ids, action: 'link_profile', value: btn.dataset.linkProfile }),
        });
        showToast('Linked ' + ids.length + ' post(s)', 'success');
        await refresh();
      } catch (err) { showToast(err.message, 'danger'); }
    });
  }

  /* ── Link map ─────────────────────────────────────────────────────────── */

  $('#btnLinkmap').addEventListener('click', async () => {
    try {
      const data = await api('/' + WATCH + '/to-linkmap', {
        method: 'POST', body: JSON.stringify({ min_score: 30 }),
      });
      showToast('Built a map with ' + data.nodes + ' nodes', 'success');
      window.open(data.url, '_blank');
    } catch (e) { showToast(e.message, 'danger'); }
  });

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
        const data = await api('/' + WATCH + '/collect-auth', {
          method: 'POST',
          body: JSON.stringify({
            credential_id: cid,
            query: $('#c-query').value.trim(),
            url: $('#authUrl').value.trim(),
            use_browser: $('#authBrowser').checked,
            limit: parseInt($('#c-limit').value, 10) || 25,
          }),
        });
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

  refresh();
})();
