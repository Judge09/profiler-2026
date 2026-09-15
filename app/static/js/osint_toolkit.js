/* The lookup half of the OSINT page: breach history, pivots, password checks.
 *
 * These are all single lookups rather than sweeps, so they share one panel
 * with tabs instead of each getting a page of its own. Everything talks to
 * /monitor/osint/*, which does the fetching server-side -- the browser cannot
 * call these APIs directly because of CORS.
 */
(function () {
  'use strict';

  const $ = (sel) => document.querySelector(sel);
  const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g,
    (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  const tabs = $('#toolTabs');
  if (!tabs) return;             // not on this page

  const num = (n) => Number(n || 0).toLocaleString();

  /* ── Tabs ───────────────────────────────────────────────────────────── */

  tabs.addEventListener('click', (e) => {
    const btn = e.target.closest('[data-tool]');
    if (!btn) return;
    tabs.querySelectorAll('[data-tool]').forEach((b) => {
      b.classList.toggle('active', b === btn);
      const panel = document.getElementById('tool-' + b.dataset.tool);
      if (panel) panel.style.display = (b === btn) ? '' : 'none';
    });
  });

  async function getJSON(url, options) {
    const res = await fetch(url, options);
    let data = {};
    try { data = await res.json(); } catch (e) { /* not JSON */ }
    if (!res.ok && data.error) throw new Error(data.error);
    return data;
  }

  function busy(el, message) {
    el.innerHTML = '<p class="mon-note mb-0"><i class="fa fa-spinner fa-spin me-2"></i>' +
      esc(message) + '</p>';
  }

  function failed(el, err) {
    el.innerHTML = '<p class="text-danger mb-0" style="font-size:12px">' +
      esc(err.message || String(err)) + '</p>';
  }

  /* ── Breach history ─────────────────────────────────────────────────── */

  function breachCard(b) {
    // What leaked matters more than that something leaked: passwords and
    // biometrics are a different problem from an email list.
    const severe = (b.severe || []).length
      ? '<div style="font-size:11px;color:var(--red);margin-top:4px">' +
        '<i class="fa fa-triangle-exclamation me-1"></i>' +
        esc(b.severe.join(', ')) + '</div>'
      : '';
    const caveats = [];
    if (b.fabricated) caveats.push('reported fabricated');
    if (b.spam_list) caveats.push('spam list');
    if (b.malware) caveats.push('from malware logs');
    if (!b.verified) caveats.push('unverified');

    return '<div class="card mb-2" style="padding:12px">' +
      '<div class="d-flex justify-content-between align-items-start gap-2 flex-wrap">' +
      '<div style="min-width:0">' +
      '<div style="font-weight:700;color:#fff">' + esc(b.title) + '</div>' +
      '<div style="font-size:11px;color:var(--text-dim)">' +
      esc(b.domain || '—') + ' · breached ' + esc(b.breach_date || '?') + '</div>' +
      '</div>' +
      '<div style="text-align:right;white-space:nowrap">' +
      '<div style="font-weight:700;color:var(--yellow)">' + num(b.count) + '</div>' +
      '<div style="font-size:10px;color:var(--text-faint)">records</div>' +
      '</div></div>' +
      severe +
      (b.data_classes && b.data_classes.length
        ? '<div style="font-size:11px;color:var(--text-dim);margin-top:6px">' +
          esc(b.data_classes.slice(0, 8).join(' · ')) + '</div>' : '') +
      (caveats.length
        ? '<div style="font-size:10px;color:var(--text-faint);margin-top:4px">' +
          esc(caveats.join(' · ')) + '</div>' : '') +
      '</div>';
  }

  function renderBreaches(out, data) {
    const rows = data.breaches || [];
    if (!rows.length) {
      out.innerHTML = '<p class="mon-note mb-0">' +
        esc(data.note || 'Nothing found.') + '</p>';
      return;
    }
    out.innerHTML =
      '<p class="mon-note">' + esc(data.note || '') + '</p>' +
      rows.map(breachCard).join('');
  }

  async function runBreach() {
    const out = $('#breachOut');
    const value = $('#breachInput').value.trim();
    if (!value) { out.innerHTML = ''; return; }
    busy(out, 'Checking the breach catalogue…');
    try {
      // A dotted value is a domain; anything else is a name to search for.
      const isDomain = /\./.test(value) && !/\s/.test(value);
      const data = await getJSON('/monitor/osint/breach?' +
        (isDomain ? 'domain=' : 'q=') + encodeURIComponent(value));
      renderBreaches(out, data);
    } catch (e) { failed(out, e); }
  }

  const breachBtn = $('#breachBtn');
  if (breachBtn) breachBtn.addEventListener('click', runBreach);
  const breachInput = $('#breachInput');
  if (breachInput) {
    breachInput.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') { e.preventDefault(); runBreach(); }
    });
  }

  const phBtn = $('#breachPhBtn');
  if (phBtn) {
    phBtn.addEventListener('click', async () => {
      const out = $('#breachOut');
      busy(out, 'Listing breaches of .ph domains…');
      try {
        const data = await getJSON('/monitor/osint/breach?tld=.ph');
        renderBreaches(out, Object.assign({}, data, {
          note: (data.breaches || []).length +
            ' breach(es) of .ph domains in the catalogue, largest first.',
        }));
      } catch (e) { failed(out, e); }
    });
  }

  /* ── Pivot links ────────────────────────────────────────────────────── */

  const pivotBtn = $('#pivotBtn');
  if (pivotBtn) {
    pivotBtn.addEventListener('click', async () => {
      const out = $('#pivotOut');
      const kind = $('#pivotKind').value;
      const value = $('#pivotInput').value.trim();
      if (!value) { out.innerHTML = ''; return; }
      busy(out, 'Building links…');
      try {
        const data = await getJSON('/monitor/osint/pivots?kind=' +
          encodeURIComponent(kind) + '&value=' + encodeURIComponent(value));
        out.innerHTML = '<div class="mon-dorks" style="display:block">' +
          (data.pivots || []).map((p) => {
            // A local tool gets its command shown, because a link to its
            // GitHub page is not what the analyst needs at that moment.
            const cmd = p.command
              ? '<code style="margin-left:8px;font-size:11px">' +
                esc(p.command) + '</code>' : '';
            return '<div style="padding:6px 0;border-bottom:1px solid var(--border)">' +
              '<a href="' + esc(p.url) + '"' +
              (p.internal ? '' : ' target="_blank" rel="noopener noreferrer"') +
              ' style="font-weight:600">' + esc(p.label) + '</a>' + cmd +
              '<div style="font-size:11px;color:var(--text-dim)">' +
              esc(p.note || '') + '</div></div>';
          }).join('') + '</div>';
      } catch (e) { failed(out, e); }
    });
  }

  /* ── Password check ─────────────────────────────────────────────────── */

  const pwBtn = $('#pwBtn');
  if (pwBtn) {
    pwBtn.addEventListener('click', async () => {
      const out = $('#pwOut');
      const input = $('#pwInput');
      const value = input.value;
      if (!value) { out.innerHTML = ''; return; }
      busy(out, 'Checking…');
      try {
        const data = await getJSON('/monitor/osint/password', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ password: value }),
        });
        // Clear it as soon as the answer is back: there is no reason for a
        // password to sit in a field after it has been checked.
        input.value = '';
        const bad = data.pwned;
        out.innerHTML =
          '<div class="card" style="padding:12px;border-color:' +
          (bad ? 'var(--red)' : 'var(--green)') + '">' +
          '<div style="font-weight:700;color:' +
          (bad ? 'var(--red)' : 'var(--green)') + '">' +
          '<i class="fa ' + (bad ? 'fa-triangle-exclamation' : 'fa-circle-check') +
          ' me-2"></i>' + esc(data.note || '') + '</div>' +
          (data.privacy ? '<div style="font-size:11px;color:var(--text-faint);' +
            'margin-top:6px"><i class="fa fa-lock me-1"></i>' +
            esc(data.privacy) + '</div>' : '') +
          '</div>';
      } catch (e) { failed(out, e); }
    });
  }

  const pwInput = $('#pwInput');
  if (pwInput) {
    pwInput.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') { e.preventDefault(); pwBtn.click(); }
    });
  }
})();
