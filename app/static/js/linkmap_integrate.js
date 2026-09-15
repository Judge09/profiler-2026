/* "Add to link map" — one button, wherever there is something worth mapping.
 *
 * Building a map used to be a thing you did from the watch page, into a new
 * map, and nowhere else. That is the wrong shape for how the work actually
 * goes: an analyst finds one interesting post, or opens a profile, and wants it
 * on the map they are already building — not a fresh one they then have to
 * merge by hand.
 *
 * So this exposes a single entry point, `LinkMapAdd.open(source)`, that any
 * page can call with whatever it has:
 *
 *   {kind: 'watch',   watch}          every flagged post in a watch
 *   {kind: 'posts',   watch, posts}   a hand-picked selection
 *   {kind: 'profile', profile}        a profile and its accounts
 *
 * It shows one dialog: which map to add to (existing or new), what to include,
 * and a live count of what would be drawn. Merging is the default, because
 * "add to the map I am building" is the common case and a brand-new map for
 * every action is what made the old flow tedious.
 *
 * Merge safety: `graphbuild.merge` on the server matches nodes on label+type
 * and remaps ids, so adding the same thing twice does not duplicate nodes. The
 * existing map's JSON is sent and the merged result comes back whole.
 */
(function (global) {
  'use strict';

  const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g,
    (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  const nowISO = () => new Date().toISOString().slice(0, 19);

  /* The dialog is created once and reused, so repeated use does not pile up
   * detached modals in the DOM. */
  function ensureModal() {
    let el = document.getElementById('lmAddModal');
    if (el) return el;
    el = document.createElement('div');
    el.className = 'modal fade';
    el.id = 'lmAddModal';
    el.tabIndex = -1;
    el.innerHTML =
      '<div class="modal-dialog modal-dialog-centered">' +
      '<div class="modal-content"><div class="modal-header">' +
      '<h5 class="modal-title"><i class="fa fa-diagram-project me-2"></i>' +
      'Add to link map</h5>' +
      '<button type="button" class="btn-close" data-bs-dismiss="modal"></button>' +
      '</div><div class="modal-body" id="lmAddBody"></div>' +
      '<div class="modal-footer">' +
      '<button type="button" class="btn btn-ghost btn-sm" data-bs-dismiss="modal">' +
      'Cancel</button>' +
      '<button type="button" class="btn btn-primary btn-sm" id="lmAddGo">' +
      '<i class="fa fa-diagram-project me-1"></i> Add to map</button>' +
      '</div></div></div>';
    document.body.appendChild(el);
    return el;
  }

  /* Build the graph payload for a source, without storing anything.
   *
   * Everything funnels through the server's /api/graph so there is exactly one
   * implementation of what a graph looks like. A profile has no posts, so it is
   * turned into a tiny graph here rather than being sent through the post
   * pipeline that would have nothing to chew on. */
  async function buildGraph(source, opts) {
    if (source.kind === 'profile') {
      return profileGraph(source.profile, opts);
    }

    const watch = source.watch;
    if (!watch) throw new Error('No watch to build from.');

    if (source.kind === 'posts') {
      const profiles = await Store.all('profiles');
      const res = await fetch('/monitor/api/graph', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          watch, posts: source.posts || [], profile_records: profiles,
          min_score: opts.min_score, domains: opts.domains,
          profiles: opts.profiles, geo: opts.geo,
          existing: opts.existing || null,
          // A hand-picked selection is already the analyst's choice; filtering
          // it again by verdict would silently drop posts they just selected.
          verdicts: null,
        }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || 'Could not build the graph.');
      return data;
    }

    return Data.graph(watch, {
      min_score: opts.min_score, domains: opts.domains,
      profiles: opts.profiles, geo: opts.geo, existing: opts.existing || null,
    });
  }

  /* A profile's own small graph: the person and the accounts attached to them.
   * Merged into an existing map, this is how a profile joins a picture built
   * from posts — the codename node matches the one the post graph already drew
   * for a linked profile, so the two halves join at that node. */
  function profileGraph(profile, opts) {
    const nodes = [{
      id: 1, label: profile.codename, type: 'person',
      title: profile.real_name || 'Tracked profile',
      profile_id: profile.id,
    }];
    const edges = [];
    let nid = 2;

    (profile.social_links || []).forEach((l) => {
      const label = l.username || l.url;
      if (!label) return;
      nodes.push({
        id: nid, label: String(label), type: 'username',
        title: l.platform || 'Account',
      });
      edges.push({ from: 1, to: nid, label: 'linked account',
                   title: l.platform || '' });
      nid += 1;
    });

    if (opts.domains) {
      (profile.social_links || []).forEach((l) => {
        if (!l.url) return;
        let host = '';
        try { host = new URL(l.url).hostname.replace(/^www\./, ''); } catch (e) { return; }
        if (!host) return;
        const existing = nodes.find((n) => n.label === host && n.type === 'website');
        const id = existing ? existing.id : nid;
        if (!existing) { nodes.push({ id: nid, label: host, type: 'website',
                                      title: 'Linked from the profile' }); nid += 1; }
        edges.push({ from: 1, to: id, label: 'links to', title: host });
      });
    }

    const graph = { nodes, edges };
    const stats = { nodes: nodes.length, edges: edges.length,
                    posts: 0, comments: 0, accounts: nodes.length - 1,
                    domains: 0, profiles: 1, locations: 0, skipped: 0 };

    // Merging happens on the server so that one implementation decides what
    // counts as the same entity.
    if (opts.existing) {
      return fetch('/monitor/api/graph/merge', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ existing: opts.existing, addition: graph }),
      }).then(async (res) => {
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || 'Merge failed.');
        return { graph: data.graph, stats, merged: data.merged };
      });
    }
    return Promise.resolve({ graph, stats, merged: null });
  }

  function describe(source) {
    if (source.kind === 'profile') {
      return 'Adds <b>' + esc(source.profile.codename) + '</b> and the accounts ' +
        'attached to it. Merged into an existing map, it joins at the node ' +
        'that already stands for this profile.';
    }
    if (source.kind === 'posts') {
      const n = (source.posts || []).length;
      return 'Adds the <b>' + n + '</b> selected post' + (n === 1 ? '' : 's') +
        ', the accounts behind them and the domains they link to.';
    }
    return 'Adds the flagged posts in this watch: the accounts behind them, ' +
      'the domains they push, and any matching profiles. Accounts sharing a ' +
      'domain become visible as shared nodes.';
  }

  const LinkMapAdd = {
    /** Open the dialog for a source. Resolves to the graph id, or null when
     *  the analyst cancelled. */
    async open(source) {
      if (typeof bootstrap === 'undefined') {
        showToast('The dialog library did not load; reload the page.', 'danger');
        return null;
      }

      const el = ensureModal();
      const body = el.querySelector('#lmAddBody');
      body.innerHTML = '<p class="mon-note mb-0">Preparing…</p>';
      const modal = bootstrap.Modal.getOrCreateInstance(el);
      modal.show();

      let graphs = [];
      try {
        graphs = (await Store.all('graphs'))
          .sort((a, b) => String(b.updated_at).localeCompare(String(a.updated_at)))
          .slice(0, 30);
      } catch (e) {
        body.innerHTML = '<p class="text-danger mb-0" style="font-size:12px">' +
          esc(e.message) + '</p>';
        return null;
      }

      // A map already tied to this profile, or built from this watch, is the
      // one the analyst almost always means — so it is preselected.
      const preferred = graphs.find((g) =>
        (source.kind === 'profile' && g.profile_id === source.profile.id) ||
        (source.watch && g.title === source.watch.name + ' - Signal Map'));

      const isProfile = source.kind === 'profile';
      body.innerHTML =
        '<p class="mon-note">' + describe(source) + '</p>' +
        '<div class="mb-2"><label class="form-label">Add to</label>' +
        '<select class="form-select form-select-sm" id="lmAddTarget">' +
        '<option value="">— a new map —</option>' +
        graphs.map((g) => '<option value="' + g.id + '"' +
          (preferred && preferred.id === g.id ? ' selected' : '') + '>' +
          esc(g.title) + '</option>').join('') +
        '</select></div>' +
        (isProfile ? '' :
          '<div class="mb-2"><label class="form-label">Minimum risk score</label>' +
          '<input type="number" class="form-control form-control-sm" ' +
          'id="lmAddMin" value="' + (source.kind === 'posts' ? 0 : 30) +
          '" min="0" max="100"></div>') +
        '<div class="d-flex gap-3 flex-wrap mb-2" style="font-size:12px">' +
        '<label><input type="checkbox" id="lmAddDomains" checked> Include domains</label>' +
        (isProfile ? '' :
          '<label><input type="checkbox" id="lmAddProfiles" checked> Include profiles</label>' +
          '<label><input type="checkbox" id="lmAddGeo"> Include host locations</label>') +
        '</div>' +
        '<div class="mon-note" id="lmAddStats">Choose the options, then add.</div>';

      // A live preview of what would be drawn, so nobody adds 400 nodes to a
      // map by accident. It is debounced because each run is a server call.
      let timer = null;
      const preview = async () => {
        const stats = body.querySelector('#lmAddStats');
        stats.textContent = 'Counting…';
        try {
          const res = await buildGraph(source, readOpts(body, source));
          const s = res.stats || {};
          stats.innerHTML = 'Would draw <b>' + (s.nodes || 0) + '</b> node(s) and <b>' +
            (s.edges || 0) + '</b> edge(s)' +
            (s.posts ? ' from ' + s.posts + ' post(s)' : '') +
            (s.comments ? ', ' + s.comments + ' of them comments' : '') +
            (s.skipped ? '; ' + s.skipped + ' below the threshold' : '') + '.';
        } catch (e) {
          stats.innerHTML = '<span class="text-danger">' + esc(e.message) + '</span>';
        }
      };
      const schedule = () => { clearTimeout(timer); timer = setTimeout(preview, 350); };
      body.querySelectorAll('input, select').forEach((input) => {
        input.addEventListener('change', schedule);
      });
      schedule();

      return new Promise((resolve) => {
        const go = el.querySelector('#lmAddGo');

        // `onclick` rather than addEventListener: the modal is reused, and a
        // listener added on every open would fire once per previous open.
        go.onclick = async () => {
          go.disabled = true;
          go.innerHTML = '<i class="fa fa-spinner fa-spin me-1"></i> Adding…';
          try {
            const targetId = body.querySelector('#lmAddTarget').value
              ? Number(body.querySelector('#lmAddTarget').value) : null;
            const existing = targetId ? await Store.get('graphs', targetId) : null;
            const opts = readOpts(body, source);
            opts.existing = existing ? existing.graph_json : null;

            const res = await buildGraph(source, opts);
            const row = existing || {
              title: defaultTitle(source),
              profile_id: (isProfile ? source.profile.id
                : (source.watch && source.watch.profile_id) || null),
              created_at: nowISO(),
            };
            row.graph_json = JSON.stringify(res.graph);
            row.updated_at = nowISO();
            const gid = await Store.put('graphs', row);

            modal.hide();
            // showToast writes into innerHTML, and a map title is whatever
            // someone typed, so it is escaped rather than interpolated raw.
            showToast(res.merged
              ? 'Merged ' + res.merged.nodes_added + ' new node(s) into ' +
                esc(row.title)
              : 'Built a map with ' + (res.stats.nodes || 0) + ' node(s)', 'success');
            resolve(gid);
          } catch (e) {
            showToast(e.message, 'danger');
            resolve(null);
          } finally {
            go.disabled = false;
            go.innerHTML = '<i class="fa fa-diagram-project me-1"></i> Add to map';
          }
        };

        el.addEventListener('hidden.bs.modal', () => resolve(null), { once: true });
      });
    },

    /** Open the dialog and, on success, open the map in a new tab. */
    async openAndShow(source) {
      const gid = await LinkMapAdd.open(source);
      if (gid) window.open('/linkmap/edit?id=' + gid, '_blank');
      return gid;
    },
  };

  function readOpts(body, source) {
    const num = (id, fallback) => {
      const el = body.querySelector(id);
      if (!el) return fallback;
      const v = parseInt(el.value, 10);
      return Number.isNaN(v) ? fallback : v;
    };
    const on = (id, fallback) => {
      const el = body.querySelector(id);
      return el ? el.checked : fallback;
    };
    return {
      min_score: num('#lmAddMin', source.kind === 'posts' ? 0 : 30),
      domains: on('#lmAddDomains', true),
      profiles: on('#lmAddProfiles', true),
      geo: on('#lmAddGeo', false),
    };
  }

  function defaultTitle(source) {
    if (source.kind === 'profile') return source.profile.codename + ' — Link Map';
    const name = (source.watch && source.watch.name) || 'Watch';
    return name + ' - Signal Map';
  }

  global.LinkMapAdd = LinkMapAdd;
})(window);
