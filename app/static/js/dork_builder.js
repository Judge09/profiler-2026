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
    const res = await fetch('/dorks/templates');
    allTemplates = await res.json();
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
  if (runBtn) {
    runBtn.addEventListener('click', async () => {
      const query = getFinalQuery();
      if (!query || query.includes('{')) {
        alert('Fill in all variables before searching.');
        return;
      }

      const profileId = document.getElementById('dorkProfileSelect')?.value || null;
      runBtn.disabled = true;
      runBtn.innerHTML = '<span class="spinner"></span> Searching…';

      await fetch('/dorks/search', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
          query,
          template_id: currentTemplate?.id || null,
          profile_id: profileId || null,
        })
      });

      runBtn.disabled = false;
      runBtn.innerHTML = '<i class="fa fa-search"></i> Search Google';

      // Refresh history
      loadHistory();
    });
  }

  // ── Favorites ─────────────────────────────────────────────────────────────
  if (favBtn) {
    favBtn.addEventListener('click', async () => {
      const query = getFinalQuery();
      if (!query) return;
      const label = prompt('Label for this favorite (optional):', currentTemplate?.name || '');
      if (label === null) return;

      await fetch('/dorks/favorites', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({query, label})
      });
      loadFavorites();
    });
  }

  function getFinalQuery() {
    if (customInput && customInput.value.trim()) return customInput.value.trim();
    return previewEl?.textContent?.trim() || '';
  }

  // ── History ───────────────────────────────────────────────────────────────
  async function loadHistory() {
    const res = await fetch('/dorks/history');
    const items = await res.json();
    const container = document.getElementById('dorkHistory');
    if (!container) return;

    if (!items.length) {
      container.innerHTML = '<div style="color:var(--text-faint); font-size:12px;">No history yet.</div>';
      return;
    }

    container.innerHTML = items.map(h => `
      <div class="history-item" onclick="useHistoryQuery(${JSON.stringify(escHtml(h.query))})">
        <span class="history-query">${escHtml(h.query)}</span>
        <span class="history-time">${h.used_at}</span>
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
    const res = await fetch('/dorks/favorites');
    const items = await res.json();
    const container = document.getElementById('dorkFavorites');
    if (!container) return;

    if (!items.length) {
      container.innerHTML = '<div style="color:var(--text-faint); font-size:12px;">No saved favorites.</div>';
      return;
    }

    container.innerHTML = items.map(f => `
      <div class="history-item">
        <span class="history-query" onclick="useHistoryQuery(${JSON.stringify(escHtml(f.query))})">${escHtml(f.label || f.query)}</span>
        <button class="btn btn-xs btn-danger" onclick="deleteFav(${f.id})"><i class="fa fa-trash"></i></button>
      </div>
    `).join('');
  }

  window.deleteFav = async function (id) {
    await fetch(`/dorks/favorites/${id}`, {method: 'DELETE'});
    loadFavorites();
  };

  // ── Custom template save ──────────────────────────────────────────────────
  window.saveCustomTemplate = async function () {
    const name = document.getElementById('customTmplName')?.value.trim();
    const template = document.getElementById('customTmplQuery')?.value.trim();
    const category = document.getElementById('customTmplCategory')?.value.trim() || 'Custom';
    const description = document.getElementById('customTmplDesc')?.value.trim();

    if (!name || !template) { alert('Name and query are required.'); return; }

    const res = await fetch('/dorks/custom', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({name, template, category, description})
    });

    if (res.ok) {
      const data = await res.json();
      allTemplates[data.category] = allTemplates[data.category] || [];
      allTemplates[data.category].push(data);
      renderTemplateList(activeCategory);
      document.getElementById('customTmplName').value = '';
      document.getElementById('customTmplQuery').value = '';
      document.getElementById('customTmplDesc').value = '';
    }
  };

  window.deleteCustomTemplate = async function (id, e) {
    e.stopPropagation();
    if (!confirm('Delete this custom template?')) return;
    const res = await fetch(`/dorks/custom/${id}`, {method: 'DELETE'});
    if (res.ok) {
      Object.keys(allTemplates).forEach(cat => {
        allTemplates[cat] = allTemplates[cat].filter(t => t.id !== id);
      });
      renderTemplateList(activeCategory);
    }
  };

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
