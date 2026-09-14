/**
 * Dork Builder — Dynamic variable detection + live preview
 */

(function () {
  const templateSelect  = document.getElementById('templateSelect');
  const varsContainer   = document.getElementById('dorkVarsContainer');
  const previewEl       = document.getElementById('dorkPreview');
  const runBtn          = document.getElementById('dorkRunBtn');
  const favBtn          = document.getElementById('dorkFavBtn');
  const categoryPills   = document.querySelectorAll('.dork-cat-pill');
  const templateList    = document.getElementById('dorkTemplateList');

  let allTemplates = {};
  let currentTemplate = null;
  let activeCategory   = 'all';

  // ── Load templates on page load ──────────────────────────────────────────
  async function loadTemplates() {
    // Built-in templates are reference data and come from the server; the
    // analyst's own templates live in this browser and are merged in here.
    const res = await fetch('/dorks/templates');
    allTemplates = await res.json();
    try {
      (await Data.customDorks()).forEach((t) => {
        const cat = t.category || 'Custom';
        allTemplates[cat] = allTemplates[cat] || [];
        allTemplates[cat].push(Object.assign({ is_builtin: false }, t));
      });
    } catch (e) { /* an empty store is not an error */ }
    renderCategories();
    renderTemplateList(activeCategory);
  }

  function renderCategories() {
    const bar = document.getElementById('dorkCategoryBar');
    if (!bar) return;

    const categories = Object.keys(allTemplates);
    categories.forEach(cat => {
      const pill = document.createElement('span');
      pill.className = 'dork-pill';
      pill.dataset.cat = cat;
      pill.textContent = cat;
      pill.onclick = () => selectCategory(cat, pill);
      bar.appendChild(pill);
    });
  }

  function selectCategory(cat, pillEl) {
    activeCategory = cat;
    document.querySelectorAll('#dorkCategoryBar .dork-pill').forEach(p => p.classList.remove('active'));
    document.querySelector('#dorkCategoryBar .dork-pill[data-cat="all"]')?.classList.remove('active');
    pillEl?.classList.add('active');
    renderTemplateList(cat);
  }

  function renderTemplateList(cat) {
    if (!templateList) return;
    templateList.innerHTML = '';

    let items = [];
    if (cat === 'all') {
      Object.values(allTemplates).forEach(arr => items.push(...arr));
    } else {
      items = allTemplates[cat] || [];
    }

    if (!items.length) {
      templateList.innerHTML = '<div style="color:var(--text-faint); font-size:12px; padding:12px;">No templates in this category.</div>';
      return;
    }

    items.forEach(t => {
      const div = document.createElement('div');
      div.className = 'dork-template-item';
      div.innerHTML = `
        <div class="dork-template-name">${escHtml(t.name)}</div>
        ${t.description ? `<div class="dork-template-desc">${escHtml(t.description)}</div>` : ''}
        <div class="dork-template-query">${escHtml(t.template)}</div>
        ${!t.is_builtin ? `<button class="btn btn-xs btn-danger mt-1" onclick="deleteCustomTemplate(${t.id}, event)"><i class="fa fa-trash"></i></button>` : ''}
      `;
      div.onclick = (e) => {
        if (e.target.closest('button')) return;
        selectTemplate(t);
        document.querySelectorAll('.dork-template-item').forEach(el => el.style.borderColor = '');
        div.style.borderColor = 'var(--accent)';
      };
      templateList.appendChild(div);
    });
  }

  function selectTemplate(t) {
    currentTemplate = t;
    buildVarFields(t.template);
    updatePreview();
  }

  // ── Variable extraction & field builder ──────────────────────────────────
  function extractVars(template) {
    const matches = [...template.matchAll(/\{(\w+)\}/g)];
    const seen = new Set();
    return matches.map(m => m[1]).filter(v => { if (seen.has(v)) return false; seen.add(v); return true; });
  }

  function buildVarFields(template) {
    if (!varsContainer) return;
    const vars = extractVars(template);
    const hint = document.getElementById('dorkVarHint');

    if (!vars.length) {
      varsContainer.innerHTML = '<div style="color:var(--text-dim); font-size:12px;">No variables — template is ready to search.</div>';
      if (hint) hint.style.display = 'none';
      updatePreview();
      return;
    }

    if (hint) hint.style.display = 'block';
    varsContainer.innerHTML = vars.map(v => `
      <div class="mb-2">
        <label class="form-label">${v.replace(/_/g, ' ').toUpperCase()}</label>
        <input type="text" class="form-control dork-var-input" data-var="${escHtml(v)}"
               placeholder="${escHtml(v)}" oninput="dorkBuildPreview()">
      </div>
    `).join('');
  }

  // ── Preview builder ───────────────────────────────────────────────────────
  window.dorkBuildPreview = function () {
    updatePreview();
  };

  function updatePreview() {
    if (!previewEl) return;
    if (!currentTemplate) {
      previewEl.textContent = 'Select a template or type a custom query…';
      return;
    }

    let result = currentTemplate.template;
    document.querySelectorAll('.dork-var-input').forEach(input => {
      const varName = input.dataset.var;
      const val = input.value.trim() || `{${varName}}`;
      result = result.replaceAll(`{${varName}}`, val);
    });

    previewEl.textContent = result;
    if (previewEl.tagName === 'INPUT' || previewEl.tagName === 'TEXTAREA') {
      previewEl.value = result;
    }
  }

  // ── Custom query direct input ─────────────────────────────────────────────
  const customInput = document.getElementById('customDorkInput');
  if (customInput) {
    customInput.addEventListener('input', () => {
      if (previewEl) previewEl.textContent = customInput.value.trim() || 'Type a query above…';
    });
  }

  // ── Run / Search ──────────────────────────────────────────────────────────
  // The server builds the URL; this tab opens it. The previous version called
  // webbrowser.open() on the *server*, which opens a browser on the machine
  // running Flask -- fine on a laptop, useless on anything remote.
  if (runBtn) {
    runBtn.addEventListener('click', async () => {
      const query = getFinalQuery();
      if (!query || query.includes('{')) {
        showToast('Fill in every variable before searching.', 'warning');
        return;
      }

      const profileId = document.getElementById('dorkProfileSelect')?.value || null;
      runBtn.disabled = true;
      runBtn.innerHTML = '<span class="spinner"></span> Opening…';

      try {
        const res = await fetch('/dorks/build', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ query }),
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || 'Could not build that query');

        await Data.recordDork(query, currentTemplate?.id || null,
                              profileId ? Number(profileId) : null);
        window.open(data.url, '_blank', 'noopener');
        loadHistory();
      } catch (e) {
        showToast(e.message, 'danger');
      } finally {
        runBtn.disabled = false;
        runBtn.innerHTML = '<i class="fa fa-search"></i> Search Google';
      }
    });
  }

  // ── Favorites ─────────────────────────────────────────────────────────────
  if (favBtn) {
    favBtn.addEventListener('click', async () => {
      const query = getFinalQuery();
      if (!query) return;
      const label = prompt('Label for this favorite (optional):', currentTemplate?.name || '');
      if (label === null) return;
      try {
        await Data.saveDorkFavorite(query, label);
        loadFavorites();
        showToast('Saved to favorites', 'success');
      } catch (e) { showToast(e.message, 'danger'); }
    });
  }

  function getFinalQuery() {
    if (customInput && customInput.value.trim()) return customInput.value.trim();
    return previewEl?.textContent?.trim() || '';
  }

  // ── History ───────────────────────────────────────────────────────────────
  // History, favourites and custom templates are the analyst's own records, so
  // they live in this browser rather than on the server.
  async function loadHistory() {
    const items = await Data.dorkHistory(25);
    const container = document.getElementById('dorkHistory');
    if (!container) return;

    if (!items.length) {
      container.innerHTML = '<div style="color:var(--text-faint); font-size:12px;">No history yet.</div>';
      return;
    }

    container.innerHTML = items.map((h) => `
      <div class="history-item" data-use="${escHtml(h.query)}">
        <span class="history-query">${escHtml(h.query)}</span>
        <span class="history-time">${escHtml((h.used_at || '').replace('T', ' ').slice(0, 16))}</span>
      </div>
    `).join('');
  }

  window.useHistoryQuery = function (query) {
    if (customInput) customInput.value = query;
    if (previewEl) previewEl.textContent = query;
    currentTemplate = null;
    if (varsContainer) varsContainer.innerHTML = '';
  };

  // ── Favorites loader ──────────────────────────────────────────────────────
  async function loadFavorites() {
    const items = await Data.dorkFavorites();
    const container = document.getElementById('dorkFavorites');
    if (!container) return;

    if (!items.length) {
      container.innerHTML = '<div style="color:var(--text-faint); font-size:12px;">No saved favorites.</div>';
      return;
    }

    container.innerHTML = items.map((f) => `
      <div class="history-item">
        <span class="history-query" data-use="${escHtml(f.query)}">${escHtml(f.label || f.query)}</span>
        <button class="btn btn-xs btn-danger" data-delfav="${f.id}"><i class="fa fa-trash"></i></button>
      </div>
    `).join('');
  }

  // One delegated listener rather than inline onclick handlers, so a query
  // containing quotes cannot break out of an attribute.
  document.addEventListener('click', async (e) => {
    const use = e.target.closest('[data-use]');
    if (use) { window.useHistoryQuery(use.dataset.use); return; }

    const del = e.target.closest('[data-delfav]');
    if (del) {
      await Data.deleteDorkFavorite(Number(del.dataset.delfav));
      loadFavorites();
    }
  });

  // ── Custom template save ──────────────────────────────────────────────────
  window.saveCustomTemplate = async function () {
    const name = document.getElementById('customTmplName')?.value.trim();
    const template = document.getElementById('customTmplQuery')?.value.trim();
    const category = document.getElementById('customTmplCategory')?.value.trim() || 'Custom';
    const description = document.getElementById('customTmplDesc')?.value.trim();

    if (!name || !template) {
      showToast('Name and query are both required.', 'warning');
      return;
    }
    try {
      const saved = await Data.saveCustomDork({ name, template, category, description });
      saved.is_builtin = false;
      allTemplates[category] = allTemplates[category] || [];
      allTemplates[category].push(saved);
      renderTemplateList(activeCategory);
      ['customTmplName', 'customTmplQuery', 'customTmplDesc'].forEach((id) => {
        const el = document.getElementById(id);
        if (el) el.value = '';
      });
      showToast('Template saved', 'success');
    } catch (e) { showToast(e.message, 'danger'); }
  };

  window.deleteCustomTemplate = async function (id, e) {
    if (e) e.stopPropagation();
    if (!await confirmModal('Delete this custom template?', 'Delete')) return;
    await Data.deleteCustomDork(id);
    Object.keys(allTemplates).forEach((cat) => {
      allTemplates[cat] = allTemplates[cat].filter((t) => t.id !== id);
    });
    renderTemplateList(activeCategory);
  };

  // Profile picker, filled from the browser store.
  (async function fillProfiles() {
    const sel = document.getElementById('dorkProfileSelect');
    if (!sel) return;
    try {
      const profiles = await Data.profiles();
      sel.innerHTML = '<option value="">— no profile —</option>' +
        profiles.map((p) => `<option value="${p.id}">${escHtml(p.codename)}</option>`).join('');
    } catch (e) { /* leave the picker as-is */ }
  })();

  // ── Util ──────────────────────────────────────────────────────────────────
  function escHtml(str) {
    if (!str) return '';
    return String(str)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  // ── Tabs ──────────────────────────────────────────────────────────────────
  window.switchTab = function (tabName) {
    document.querySelectorAll('.tab-content').forEach(el => el.classList.remove('active'));
    document.querySelectorAll('.tab-btn').forEach(el => el.classList.remove('active'));
    document.getElementById(`tab-${tabName}`)?.classList.add('active');
    document.querySelector(`.tab-btn[data-tab="${tabName}"]`)?.classList.add('active');
    if (tabName === 'history') loadHistory();
    if (tabName === 'favorites') loadFavorites();
  };

  // ── Copy query ────────────────────────────────────────────────────────────
  window.copyDorkQuery = function () {
    const query = getFinalQuery();
    if (!query || query === 'Select a template or type a custom query…') {
      showToast('Nothing to copy yet', 'warning');
      return;
    }
    const btn = document.getElementById('dorkCopyBtn');
    copyToClipboard(query, btn);
  };

  // ── Init ─────────────────────────────────────────────────────────────────
  loadTemplates();
  loadHistory();
  loadFavorites();
})();
