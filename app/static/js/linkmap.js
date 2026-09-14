/**
 * Maltego-style Link Mapper — vis.js Network
 */

(function () {
  const container = document.getElementById('linkmapCanvas');
  if (!container) return;

  // ── Node type definitions ─────────────────────────────────────────────────
  const NODE_TYPES = {
    person:       { color: '#2979ff', icon: '👤', label: 'Person' },
    email:        { color: '#ffd600', icon: '✉️', label: 'Email' },
    username:     { color: '#e0e0e0', icon: '🔤', label: 'Username' },
    phone:        { color: '#00e676', icon: '📞', label: 'Phone' },
    ip:           { color: '#ff5252', icon: '🌐', label: 'IP Address' },
    website:      { color: '#ce93d8', icon: '🔗', label: 'Website' },
    organization: { color: '#ff9800', icon: '🏢', label: 'Organization' },
    location:     { color: '#00e5ff', icon: '📍', label: 'Location' },
    device:       { color: '#a5d6a7', icon: '💻', label: 'Device' },
    note:         { color: '#90a4ae', icon: '📝', label: 'Note' },
  };

  const EDGE_TYPES = [
    'known associate', 'same IP', 'linked account', 'communicates with',
    'located at', 'owns', 'works at', 'reported by', 'related to', 'custom'
  ];

  // ── Initial data ──────────────────────────────────────────────────────────
  let initialData = { nodes: [], edges: [] };
  const rawEl = document.getElementById('graphDataRaw');
  if (rawEl) {
    try { initialData = JSON.parse(rawEl.textContent); } catch (e) {}
  }

  const nodesDS = new vis.DataSet(initialData.nodes.map(n => enrichNode(n)));
  const edgesDS = new vis.DataSet(initialData.edges.map(e => enrichEdge(e)));

  const options = {
    physics: {
      enabled: true,
      solver: 'forceAtlas2Based',
      forceAtlas2Based: { gravitationalConstant: -50, springLength: 120 },
      stabilization: { iterations: 150 },
    },
    interaction: {
      hover: true,
      tooltipDelay: 200,
      multiselect: true,
    },
    nodes: {
      shape: 'dot',
      size: 18,
      font: { color: '#d0d8ef', size: 13, face: 'Inter, sans-serif' },
      borderWidth: 2,
      borderWidthSelected: 3,
    },
    edges: {
      color: { color: '#2a2a4a', highlight: '#00e5ff', hover: '#4a4a7a' },
      font: { color: '#6b7498', size: 10, align: 'middle', background: '#07070d' },
      arrows: { to: { enabled: true, scaleFactor: 0.5 } },
      smooth: { type: 'continuous', roundness: 0.1 },
      width: 1.5,
    },
    manipulation: { enabled: false }, // We handle manually
    background: { color: '#07070d' },
  };

  const network = new vis.Network(container, { nodes: nodesDS, edges: edgesDS }, options);

  // ── Dirty state + count tracking ─────────────────────────────────────────
  let isDirty = false;

  function markDirty() {
    isDirty = true;
    const dot = document.getElementById('dirtyDot');
    if (dot) dot.style.display = 'inline-block';
  }

  function markClean() {
    isDirty = false;
    const dot = document.getElementById('dirtyDot');
    if (dot) dot.style.display = 'none';
  }

  function updateCountBadge() {
    const badge = document.getElementById('graphCountBadge');
    if (!badge) return;
    const n = nodesDS.length;
    const e = edgesDS.length;
    badge.textContent = `${n} node${n !== 1 ? 's' : ''} · ${e} edge${e !== 1 ? 's' : ''}`;
  }

  nodesDS.on('*', () => { markDirty(); updateCountBadge(); });
  edgesDS.on('*', () => { markDirty(); updateCountBadge(); });

  // Initial count (don't mark dirty on load)
  updateCountBadge();
  // Don't mark initial data as dirty — override the listener's first fire
  setTimeout(markClean, 50);

  // Disable physics after stabilization
  network.on('stabilized', () => {
    network.setOptions({ physics: { enabled: false } });
  });

  // ── Node/edge helpers ─────────────────────────────────────────────────────
  function enrichNode(n) {
    const type = n.type || 'person';
    const def = NODE_TYPES[type] || NODE_TYPES.person;
    return {
      ...n,
      color: {
        background: def.color + '33',
        border: def.color,
        highlight: { background: def.color + '55', border: '#fff' },
        hover: { background: def.color + '44', border: def.color },
      },
      font: { color: '#d0d8ef', size: 13 },
      title: n.title || n.label,
    };
  }

  function enrichEdge(e) {
    return {
      ...e,
      label: e.label || '',
      color: { color: '#2a2a4a', highlight: '#00e5ff' },
    };
  }

  let nodeIdCounter = Math.max(0, ...nodesDS.getIds().map(Number)) + 1;

  // ── Add node ──────────────────────────────────────────────────────────────
  window.addNode = function () {
    const label = document.getElementById('newNodeLabel')?.value.trim();
    const type  = document.getElementById('newNodeType')?.value || 'person';
    const detail = document.getElementById('newNodeDetail')?.value.trim();
    if (!label) { alert('Label is required.'); return; }

    const node = enrichNode({
      id: nodeIdCounter++,
      label,
      type,
      title: detail || label,
    });
    nodesDS.add(node);
    clearNodeForm();
  };

  function clearNodeForm() {
    const l = document.getElementById('newNodeLabel');
    const d = document.getElementById('newNodeDetail');
    if (l) l.value = '';
    if (d) d.value = '';
  }

  // ── Add edge ──────────────────────────────────────────────────────────────
  let addingEdge = false;
  let edgeFromNode = null;

  window.startAddEdge = function () {
    if (addingEdge) {
      cancelAddEdge();
      return;
    }
    addingEdge = true;
    edgeFromNode = null;
    document.getElementById('addEdgeBtn').textContent = '✕ Cancel Edge';
    document.getElementById('edgeStatus').textContent = 'Click the SOURCE node…';
    document.getElementById('edgeStatus').style.display = 'block';
  };

  function cancelAddEdge() {
    addingEdge = false;
    edgeFromNode = null;
    document.getElementById('addEdgeBtn').textContent = '+ Add Edge';
    const st = document.getElementById('edgeStatus');
    if (st) { st.textContent = ''; st.style.display = 'none'; }
  }

  network.on('click', function (params) {
    if (!addingEdge) {
      // Show selection in property panel
      if (params.nodes.length === 1) {
        showNodeProps(params.nodes[0]);
      } else if (params.edges.length === 1 && params.nodes.length === 0) {
        showEdgeProps(params.edges[0]);
      } else {
        clearProps();
      }
      return;
    }

    if (!params.nodes.length) return;
    const clickedId = params.nodes[0];

    if (!edgeFromNode) {
      edgeFromNode = clickedId;
      document.getElementById('edgeStatus').textContent = 'Now click the TARGET node…';
    } else {
      if (edgeFromNode !== clickedId) {
        const label = document.getElementById('newEdgeLabel')?.value.trim() || 'related to';
        edgesDS.add(enrichEdge({
          id: `e${Date.now()}`,
          from: edgeFromNode,
          to: clickedId,
          label,
        }));
      }
      cancelAddEdge();
    }
  });

  // ── Property panel ────────────────────────────────────────────────────────
  function showNodeProps(id) {
    const node = nodesDS.get(id);
    if (!node) return;
    const panel = document.getElementById('propPanel');
    const typeOpts = Object.entries(NODE_TYPES)
      .map(([k, v]) => `<option value="${k}" ${node.type === k ? 'selected' : ''}>${v.label}</option>`)
      .join('');

    panel.innerHTML = `
      <div class="card-title mb-3"><i class="fa fa-circle-dot me-2 text-accent"></i>Node Properties</div>
      <div class="mb-2">
        <label class="form-label">Label</label>
        <input type="text" id="propLabel" class="form-control" value="${escHtml(node.label)}">
      </div>
      <div class="mb-2">
        <label class="form-label">Type</label>
        <select id="propType" class="form-select">${typeOpts}</select>
      </div>
      <div class="mb-3">
        <label class="form-label">Details / Tooltip</label>
        <input type="text" id="propDetail" class="form-control" value="${escHtml(node.title || '')}">
      </div>
      <div class="d-flex gap-2">
        <button class="btn btn-primary btn-sm" onclick="applyNodeProps(${id})">Apply</button>
        <button class="btn btn-danger btn-sm" onclick="deleteNode(${id})">Delete</button>
      </div>
    `;
  }

  function showEdgeProps(id) {
    const edge = edgesDS.get(id);
    if (!edge) return;
    const panel = document.getElementById('propPanel');
    const opts = EDGE_TYPES.map(t => `<option ${edge.label === t ? 'selected' : ''}>${t}</option>`).join('');

    panel.innerHTML = `
      <div class="card-title mb-3"><i class="fa fa-arrow-right-long me-2 text-accent"></i>Edge Properties</div>
      <div class="mb-2">
        <label class="form-label">Relationship Label</label>
        <select id="propEdgeLabel" class="form-select">${opts}</select>
      </div>
      <div class="mb-2">
        <label class="form-label">Custom Label</label>
        <input type="text" id="propEdgeCustom" class="form-control" value="${escHtml(edge.label || '')}" placeholder="Or type a custom label">
      </div>
      <div class="d-flex gap-2">
        <button class="btn btn-primary btn-sm" onclick="applyEdgeProps('${id}')">Apply</button>
        <button class="btn btn-danger btn-sm" onclick="deleteEdge('${id}')">Delete</button>
      </div>
    `;
  }

  function clearProps() {
    const panel = document.getElementById('propPanel');
    if (panel) panel.innerHTML = '<div style="color:var(--text-faint); font-size:12px;">Click a node or edge to edit properties.</div>';
  }

  window.applyNodeProps = function (id) {
    const label  = document.getElementById('propLabel')?.value.trim();
    const type   = document.getElementById('propType')?.value;
    const detail = document.getElementById('propDetail')?.value.trim();
    if (!label) return;
    const updated = enrichNode({ id, label, type, title: detail || label, ...nodesDS.get(id) });
    updated.label = label;
    updated.type = type;
    updated.title = detail || label;
    nodesDS.update(enrichNode(updated));
  };

  window.deleteNode = function (id) {
    if (!confirm('Delete this node?')) return;
    nodesDS.remove(id);
    clearProps();
  };

  window.applyEdgeProps = function (id) {
    const select = document.getElementById('propEdgeLabel')?.value;
    const custom = document.getElementById('propEdgeCustom')?.value.trim();
    const label = custom || select || 'related to';
    edgesDS.update(enrichEdge({ ...edgesDS.get(id), label }));
  };

  window.deleteEdge = function (id) {
    if (!confirm('Delete this edge?')) return;
    edgesDS.remove(id);
    clearProps();
  };

  // ── Save ──────────────────────────────────────────────────────────────────
  window.saveGraph = async function () {
    const title     = document.getElementById('graphTitle')?.value.trim() || 'Untitled Map';
    const profileId = document.getElementById('graphProfileId')?.value || null;
    const graphId   = document.getElementById('graphId')?.value || null;

    const nodes = nodesDS.get();
    const edges = edgesDS.get();
    const graph_json = JSON.stringify({ nodes, edges });

    const saveBtn = document.getElementById('saveGraphBtn');
    saveBtn.disabled = true;
    saveBtn.innerHTML = '<span class="spinner"></span>';

    const res = await fetch('/linkmap/save', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ id: graphId ? parseInt(graphId) : null, title, graph_json, profile_id: profileId })
    });

    const data = await res.json();
    saveBtn.disabled = false;
    saveBtn.innerHTML = '<i class="fa fa-save"></i> Save';

    if (data.ok) {
      if (!graphId) {
        history.replaceState(null, '', `/linkmap/${data.id}`);
        document.getElementById('graphId').value = data.id;
      }
      markClean();
      saveBtn.innerHTML = '<i class="fa fa-check"></i> Saved';
      setTimeout(() => saveBtn.innerHTML = '<i class="fa fa-save"></i> Save', 2000);
      showToast('Map saved', 'success');
    } else {
      showToast('Save failed', 'danger');
    }
  };

  // ── Export PNG ────────────────────────────────────────────────────────────
  window.exportPNG = async function () {
    const graphId = document.getElementById('graphId')?.value;
    if (!graphId) { alert('Save the map first before exporting.'); return; }

    const canvas = container.querySelector('canvas');
    if (!canvas) { alert('Canvas not found.'); return; }

    const dataUrl = canvas.toDataURL('image/png');

    const res = await fetch(`/linkmap/${graphId}/export`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ data_url: dataUrl })
    });

    if (res.ok) {
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `linkmap_${graphId}.png`;
      a.click();
      URL.revokeObjectURL(url);
    }
  };

  // ── Zoom / layout controls ────────────────────────────────────────────────
  window.fitGraph = function () { network.fit({ animation: { duration: 500 } }); };
  window.rerunLayout = function () {
    network.setOptions({ physics: { enabled: true } });
    setTimeout(() => network.setOptions({ physics: { enabled: false } }), 3000);
  };

  // Populate type select
  const typeSelect = document.getElementById('newNodeType');
  if (typeSelect) {
    typeSelect.innerHTML = Object.entries(NODE_TYPES)
      .map(([k, v]) => `<option value="${k}">${v.icon} ${v.label}</option>`)
      .join('');
  }

  // Populate edge label select
  const edgeLabelSelect = document.getElementById('newEdgeLabel');
  if (edgeLabelSelect) {
    edgeLabelSelect.innerHTML = EDGE_TYPES.map(t => `<option>${t}</option>`).join('');
  }

  // Init prop panel
  clearProps();

  function escHtml(str) {
    if (!str) return '';
    return String(str).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
  }
})();
