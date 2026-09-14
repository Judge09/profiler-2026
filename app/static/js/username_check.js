/**
 * Username OSINT — SSE consumer + live results table
 */

(function () {
  const form        = document.getElementById('usernameForm');
  const input       = document.getElementById('usernameInput');
  const resultsWrap = document.getElementById('resultsWrap');
  const resultsBody = document.getElementById('resultsBody');
  const progressBar = document.getElementById('osintProgress');
  const progressTxt = document.getElementById('osintProgressTxt');
  const statsEl     = document.getElementById('osintStats');
  const saveBtn     = document.getElementById('saveToProfBtn');
  const exportBtn   = document.getElementById('exportCsvBtn');
  const cancelBtn   = document.getElementById('cancelBtn');
  const filterBtns  = document.querySelectorAll('.osint-filter-btn');

  let currentRunId     = null;
  let currentUsername  = '';
  let totalPlatforms   = 0;
  let doneCount        = 0;
  let foundCount       = 0;
  let activeFilter     = 'all';
  let allResults       = [];
  let es               = null;

  if (form) {
    form.addEventListener('submit', (e) => {
      e.preventDefault();
      const username = input.value.trim();
      if (!username) return;
      startCheck(username);
    });
  }

  filterBtns.forEach(btn => {
    btn.addEventListener('click', () => {
      filterBtns.forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      activeFilter = btn.dataset.filter;
      renderResults();
    });
  });

  function startCheck(username) {
    if (es) es.close();
    allResults    = [];
    doneCount     = 0;
    foundCount    = 0;
    currentRunId  = null;
    currentUsername = username;
    resultsBody.innerHTML = '';
    resultsWrap.style.display = 'block';
    setProgress(0, '');
    statsEl.textContent = '';
    saveBtn.style.display   = 'none';
    exportBtn.style.display = 'none';
    cancelBtn.style.display = 'inline-flex';

    const searchBtn = document.getElementById('searchBtn');
    searchBtn.disabled = true;
    searchBtn.innerHTML = '<span class="spinner"></span> Scanning…';

    es = new EventSource(`/osint/check?username=${encodeURIComponent(username)}`);

    es.onmessage = (event) => {
      const data = JSON.parse(event.data);

      if (data.type === 'start') {
        totalPlatforms = data.total;
        currentRunId   = data.run_id;
        return;
      }

      if (data.type === 'done') {
        finishScan(searchBtn);
        return;
      }

      doneCount++;
      if (data.status === 'found') foundCount++;
      allResults.push(data);

      const pct = totalPlatforms ? Math.round((doneCount / totalPlatforms) * 100) : 0;
      setProgress(pct, `${doneCount} / ${totalPlatforms}`);
      statsEl.innerHTML = `<span class="text-green">${foundCount} found</span> · <span class="text-dim">${doneCount} checked</span>`;

      appendResultRow(data);
    };

    es.onerror = () => finishScan(searchBtn);
  }

  function finishScan(searchBtn) {
    if (es) { es.close(); es = null; }
    if (searchBtn) {
      searchBtn.disabled = false;
      searchBtn.innerHTML = '<i class="fa fa-magnifying-glass"></i> Search';
    }
    cancelBtn.style.display = 'none';
    setProgress(100, 'Complete');
    statsEl.innerHTML = `
      <span class="text-green"><i class="fa fa-check-circle me-1"></i>${foundCount} found</span>
      &nbsp;·&nbsp;
      <span class="text-dim">${totalPlatforms} platforms checked</span>
    `;
    if (currentRunId && foundCount > 0) saveBtn.style.display = 'inline-flex';
    if (allResults.length > 0) exportBtn.style.display = 'inline-flex';
  }

  // ── Cancel ────────────────────────────────────────────────────────────────
  window.cancelScan = function () {
    if (es) { es.close(); es = null; }
    const searchBtn = document.getElementById('searchBtn');
    finishScan(searchBtn);
    setProgress(doneCount && totalPlatforms ? Math.round((doneCount / totalPlatforms) * 100) : 0, 'Cancelled');
    showToast('Scan cancelled', 'warning');
  };

  function setProgress(pct, label) {
    if (progressBar) progressBar.style.width = `${pct}%`;
    if (progressTxt) progressTxt.textContent = label;
  }

  function appendResultRow(data) {
    if (activeFilter !== 'all' && data.status !== activeFilter) return;

    const tr = document.createElement('tr');
    tr.dataset.status   = data.status;
    tr.dataset.platform = data.platform.toLowerCase();
    const copyCell = data.url && data.status === 'found'
      ? `<td><button class="copy-btn" onclick="copyToClipboard('${escHtml(data.url)}', this)" title="Copy URL"><i class="fa fa-copy"></i></button></td>`
      : '<td></td>';

    tr.innerHTML = `
      <td style="font-weight:600; color:var(--text);">${escHtml(data.platform)}</td>
      <td class="status-${data.status}">${statusLabel(data.status)}</td>
      <td style="font-family:var(--font-mono); font-size:11px; color:var(--text-faint);">${data.http_code || '—'}</td>
      <td>
        ${data.url && data.status === 'found'
          ? `<a href="${escHtml(data.url)}" target="_blank" rel="noopener" class="text-accent" style="font-size:11px; font-family:var(--font-mono);">${escHtml(data.url)}</a>`
          : `<span style="color:var(--text-faint); font-size:11px;">${escHtml(data.url || '—')}</span>`
        }
      </td>
      ${copyCell}
    `;
    resultsBody.appendChild(tr);
  }

  function renderResults() {
    resultsBody.innerHTML = '';
    const filtered = activeFilter === 'all' ? allResults : allResults.filter(r => r.status === activeFilter);
    filtered.forEach(appendResultRow);
  }

  function statusLabel(status) {
    const map = {
      found:     '<i class="fa fa-check-circle me-1"></i>FOUND',
      not_found: '<i class="fa fa-circle-xmark me-1"></i>Not found',
      error:     '<i class="fa fa-triangle-exclamation me-1"></i>Error',
      timeout:   '<i class="fa fa-clock me-1"></i>Timeout',
    };
    return map[status] || status;
  }

  // ── CSV Export ────────────────────────────────────────────────────────────
  window.exportCSV = function () {
    const rows = [['Platform', 'Status', 'URL', 'HTTP Code']];
    allResults.forEach(r => {
      rows.push([r.platform, r.status, r.url || '', r.http_code || '']);
    });
    const csv = rows.map(r => r.map(v => `"${String(v).replace(/"/g, '""')}"`).join(',')).join('\r\n');
    const blob = new Blob([csv], { type: 'text/csv;charset=utf-8;' });
    const url  = URL.createObjectURL(blob);
    const a    = document.createElement('a');
    a.href     = url;
    a.download = `osint_${currentUsername}_${Date.now()}.csv`;
    a.click();
    URL.revokeObjectURL(url);
    showToast('CSV exported', 'success');
  };

  // ── Save to profile ───────────────────────────────────────────────────────
  if (saveBtn) {
    saveBtn.addEventListener('click', async () => {
      const profileId = document.getElementById('saveProfileSelect')?.value;
      if (!profileId) { showToast('Select a profile first', 'warning'); return; }

      saveBtn.disabled = true;
      const res = await fetch('/osint/save', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ run_id: currentRunId, profile_id: profileId })
      });
      if (res.ok) {
        const d = await res.json();
        saveBtn.innerHTML = `<i class="fa fa-check"></i> Saved (${d.updated})`;
        showToast(`${d.updated} results linked to profile`, 'success');
      } else {
        saveBtn.disabled = false;
        showToast('Save failed', 'danger');
      }
    });
  }

  function escHtml(str) {
    if (!str) return '';
    return String(str).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
  }
})();
