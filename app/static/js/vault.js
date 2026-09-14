/* Credential vault UI. */
(function () {
  'use strict';

  const $ = (s) => document.querySelector(s);
  const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g,
    (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const API = '/monitor/vault';

  let creds = window.VAULT.credentials || [];
  const GUIDE = window.VAULT.guidance || {};

  async function api(path, opts) {
    const res = await fetch(API + path, Object.assign({
      headers: { 'Content-Type': 'application/json' },
    }, opts || {}));
    let data = {};
    try { data = await res.json(); } catch (e) { /* ignore */ }
    if (!res.ok) throw new Error(data.error || 'Request failed');
    return data;
  }

  const RELIABILITY = {
    high: ['var(--green)', 'Usually holds up'],
    medium: ['var(--yellow)', 'Breaks sometimes'],
    low: ['var(--red)', 'Expect frequent lockouts'],
  };

  function card(c) {
    const h = c.health || {};
    const rel = RELIABILITY[h.reliability] || RELIABILITY.medium;
    const status = c.last_ok === true ? ['var(--green)', 'working']
      : c.last_ok === false ? ['var(--red)', 'failing']
      : ['var(--text-dim)', 'untested'];
    return '<div class="card p-3 mb-2" data-id="' + c.id + '">' +
      '<div class="d-flex justify-content-between align-items-start gap-2 flex-wrap">' +
        '<div style="min-width:0">' +
          '<div style="font-weight:700;font-size:14px">' + esc(c.label) +
            (c.enabled ? '' : ' <span class="mon-tag">disabled</span>') + '</div>' +
          '<div class="mon-meta">' + esc((GUIDE[c.platform] || {}).name || c.platform) +
            ' · ' + esc(c.kind) + (c.account_hint ? ' · ' + esc(c.account_hint) : '') +
            (c.expires_at ? ' · expires ' + esc(c.expires_at) : '') + '</div>' +
        '</div>' +
        '<div class="d-flex gap-2 align-items-center flex-wrap">' +
          '<span class="mon-tag" style="border-color:' + rel[0] + ';color:' + rel[0] + '">' + rel[1] + '</span>' +
          '<span class="mon-tag" style="border-color:' + status[0] + ';color:' + status[0] + '">' + status[1] + '</span>' +
          '<button class="btn btn-outline btn-xs" data-act="test">Test</button>' +
          '<button class="btn btn-ghost btn-xs" data-act="toggle">' + (c.enabled ? 'Disable' : 'Enable') + '</button>' +
          '<button class="btn btn-danger btn-xs" data-act="del"><i class="fa fa-trash"></i></button>' +
        '</div>' +
      '</div>' +
      (h.note ? '<p class="mon-note mt-2 mb-0">' + esc(h.note) + '</p>' : '') +
      (h.issues || []).map((i) => '<div class="mon-gap mt-2"><i class="fa fa-triangle-exclamation"></i><span>' +
        esc(i) + '</span></div>').join('') +
      '<div class="mt-2" data-result></div>' +
      (c.last_used ? '<div class="mon-meta mt-2">Last used ' + esc(c.last_used) +
        ' · ' + c.use_count + ' run(s)</div>' : '') +
      '</div>';
  }

  function render() {
    const el = $('#credList');
    if (!creds.length) return;  // server-rendered empty state stands
    el.innerHTML = creds.map(card).join('');
  }

  // -- Unlock ---------------------------------------------------------------

  const btnUnlock = $('#btnUnlock');
  if (btnUnlock) {
    const go = async () => {
      const p = $('#pass').value;
      $('#unlockErr').textContent = '';
      try {
        await api('/unlock', { method: 'POST', body: JSON.stringify({ passphrase: p }) });
        window.location.reload();
      } catch (e) { $('#unlockErr').textContent = e.message; }
    };
    btnUnlock.addEventListener('click', go);
    $('#pass').addEventListener('keydown', (e) => { if (e.key === 'Enter') go(); });
  }

  const btnLock = $('#btnLock');
  if (btnLock) {
    btnLock.addEventListener('click', async () => {
      await api('/lock', { method: 'POST' });
      window.location.reload();
    });
  }

  // -- Add ------------------------------------------------------------------

  const addModalEl = $('#addModal');
  const addModal = addModalEl ? new bootstrap.Modal(addModalEl) : null;
  const btnAdd = $('#btnAdd');
  if (btnAdd) btnAdd.addEventListener('click', () => { $('#addErr').textContent = ''; addModal.show(); });

  function syncPlatform() {
    const g = GUIDE[$('#c-platform').value] || {};
    const rel = RELIABILITY[g.reliability] || RELIABILITY.medium;
    $('#platNote').innerHTML = '<i class="fa fa-circle-info" style="color:' + rel[0] + '"></i>' +
      '<span><b style="color:' + rel[0] + '">' + rel[1] + '.</b> ' + esc(g.note || '') +
      (g.cookie_names && g.cookie_names.length
        ? ' Needs: <span class="mon-kbd">' + g.cookie_names.join('</span> <span class="mon-kbd">') + '</span>'
        : '') + '</span>';
  }
  function syncKind() {
    const k = $('#c-kind').value;
    $('#f-cookies').style.display = k === 'cookies' ? '' : 'none';
    $('#f-token').style.display = k === 'token' ? '' : 'none';
    $('#f-password').style.display = k === 'password' ? '' : 'none';
  }
  if ($('#c-platform')) {
    $('#c-platform').addEventListener('change', syncPlatform);
    $('#c-kind').addEventListener('change', syncKind);
    syncPlatform(); syncKind();
  }

  const save = $('#c-save');
  if (save) {
    save.addEventListener('click', async () => {
      const body = {
        label: $('#c-label').value.trim(),
        platform: $('#c-platform').value,
        kind: $('#c-kind').value,
        cookies: $('#c-cookies').value,
        token: $('#c-token').value,
        username: $('#c-username').value,
        password: $('#c-password').value,
        expires_at: $('#c-expires').value,
      };
      try {
        const c = await api('/credentials', { method: 'POST', body: JSON.stringify(body) });
        creds.push(c);
        ['c-label', 'c-cookies', 'c-token', 'c-username', 'c-password', 'c-expires']
          .forEach((i) => { $('#' + i).value = ''; });
        addModal.hide();
        render();
        showToast('Credential stored, encrypted', 'success');
      } catch (e) { $('#addErr').textContent = e.message; }
    });
  }

  // -- Row actions ----------------------------------------------------------

  $('#credList').addEventListener('click', async (e) => {
    const btn = e.target.closest('[data-act]');
    if (!btn) return;
    const row = btn.closest('[data-id]');
    const id = parseInt(row.dataset.id, 10);
    const c = creds.find((x) => x.id === id);
    const act = btn.dataset.act;

    if (act === 'del') {
      if (!await confirmModal('Delete this credential permanently?', 'Delete')) return;
      await api('/credentials/' + id, { method: 'DELETE' });
      creds = creds.filter((x) => x.id !== id);
      render();
      showToast('Deleted', 'success');
    } else if (act === 'toggle') {
      const r = await api('/credentials/' + id, {
        method: 'PATCH', body: JSON.stringify({ enabled: !c.enabled }),
      });
      c.enabled = r.enabled;
      render();
    } else if (act === 'test') {
      const out = row.querySelector('[data-result]');
      btn.disabled = true;
      out.innerHTML = '<p class="mon-note mb-0"><i class="fa fa-spinner fa-spin me-1"></i>Testing the session…</p>';
      try {
        const r = await api('/credentials/' + id + '/test', {
          method: 'POST', body: JSON.stringify({ query: 'test' }),
        });
        const col = r.ok ? 'var(--green)' : (r.blocked ? 'var(--yellow)' : 'var(--red)');
        out.innerHTML = '<div class="mon-gap" style="border-color:' + col + '33">' +
          '<i class="fa fa-' + (r.ok ? 'circle-check' : 'triangle-exclamation') + '" style="color:' + col + '"></i>' +
          '<span><b style="color:' + col + '">' + (r.ok ? 'Working' : (r.blocked ? 'Challenged' : 'Failed')) + '.</b> ' +
          esc(r.note) + (r.found ? ' Found ' + r.found + ' item(s).' : '') +
          (r.strategy ? ' <span class="mon-kbd">' + esc(r.strategy) + '</span>' : '') +
          (r.manual_url ? ' <a class="mon-out" href="' + esc(r.manual_url) + '" target="_blank" rel="noopener noreferrer">open manually</a>' : '') +
          '</span></div>';
        c.last_ok = r.ok;
        setTimeout(render, 4000);
      } catch (err) {
        out.innerHTML = '<p class="text-danger mb-0" style="font-size:12px">' + esc(err.message) + '</p>';
      }
      btn.disabled = false;
    }
  });

  render();
})();
