/* Ripplet UI controller: views, inspector, palette, explorer, history. */
(function () {
  'use strict';
  const $ = (s, r = document) => r.querySelector(s);
  const el = (tag, attrs = {}, ...kids) => {
    const e = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (k === 'class') e.className = v;
      else if (k === 'style') e.style.cssText = v;
      else if (k.startsWith('on')) e.addEventListener(k.slice(2), v);
      else if (v !== undefined && v !== null && v !== false) e.setAttribute(k, v === true ? '' : v);
    }
    for (const k of kids.flat()) if (k !== null && k !== undefined && k !== false) e.append(k.nodeType ? k : document.createTextNode(String(k)));
    return e;
  };
  const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

  const TYPE_COLORS = {
    function: '#7c9cff', method: '#56c2e6', class: '#f5b971', interface: '#c79bff', struct: '#f0d264', type: '#f0d264',
    trait: '#c79bff', enum: '#e8a76c', test: '#5fd4a8', object: '#ff9b73', route: '#e879d6', constant: '#93a4c0',
    variable: '#93a4c0', record: '#f0d264', file: '#8790a3', dir: '#7c9cff', external: '#5b6273', external_symbol: '#5b6273', module: '#8790a3',
  };
  const GROUP_COLORS = ['#7c9cff', '#5fd4a8', '#f5b971', '#e879d6', '#56c2e6', '#ff8a7a', '#c79bff', '#f0d264', '#7ad37a', '#ff9bc3', '#6fe0d0', '#b3a2ff'];
  const groupColor = new Map();
  function colorOfGroup(key) {
    if (!groupColor.has(key)) groupColor.set(key, GROUP_COLORS[groupColor.size % GROUP_COLORS.length]);
    return groupColor.get(key);
  }
  const groupKey = (path) => { const p = (path || '').split('/'); return p.length > 2 ? p.slice(0, 2).join('/') : p[0]; };
  const LANG_COLORS = {
    python: '#3572A5', typescript: '#3178c6', tsx: '#3178c6', javascript: '#f1e05a', java: '#b07219', kotlin: '#A97BFF',
    go: '#00ADD8', rust: '#dea584', c_sharp: '#178600', csharp: '#178600', php: '#4F5D95', ruby: '#701516', swift: '#F05138',
    cpp: '#f34b7d', c: '#8b8f96', lua: '#6b7ff0', dart: '#00B4AB', gdscript: '#478cbf', scala: '#c22d40',
  };
  const VIEWS = ['map', 'explore', 'impact', 'changes', 'http', 'path', 'routes', 'data', 'health'];
  const VIEW_TITLES = { map: 'Map', explore: 'Explore', impact: 'Impact', changes: 'Changes', http: 'API links', path: 'Path',
    routes: 'Routes', data: 'Data', health: 'Index health' };
  // how a link was established, in words (edge attribute `via`)
  const HOW = {
    'inferred-model': 'guessed by the learned model', 'ruby-unique-name': 'only method with this name (guess)',
    'swift-unique-name': 'only method with this name (guess)', 'kotlin-unique-name': 'only method with this name (guess)',
    'param-type': 'receiver type known', field: 'through a field / property type', chain: 'through a call chain',
    self: 'same class', static: 'static call', extension: 'extension method',
  };
  const ICONS = {
    explore: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="3"/><circle cx="4.5" cy="6" r="1.8"/><circle cx="19.5" cy="6" r="1.8"/><circle cx="4.5" cy="18" r="1.8"/><circle cx="19.5" cy="18" r="1.8"/><path d="M9.5 10.5L6 7M14.5 10.5L18 7M9.5 13.5L6 17M14.5 13.5L18 17"/></svg>',
    impact: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="2.5"/><circle cx="12" cy="12" r="6"/><circle cx="12" cy="12" r="9.5" stroke-dasharray="2 3"/></svg>',
    changes: '<svg viewBox="0 0 24 24"><circle cx="6" cy="6" r="2.2"/><circle cx="6" cy="18" r="2.2"/><circle cx="18" cy="12" r="2.2"/><path d="M6 8.2v7.6M8 6.5c6 0 8 2 8.6 3.6"/></svg>',
    http: '<svg viewBox="0 0 24 24"><rect x="2.5" y="5" width="7" height="14" rx="2"/><rect x="14.5" y="5" width="7" height="14" rx="2"/><path d="M9.5 9h5M9.5 15h5"/></svg>',
    path: '<svg viewBox="0 0 24 24"><circle cx="4.5" cy="12" r="2.3"/><circle cx="19.5" cy="12" r="2.3"/><path d="M7 12c3-5 7 5 10 0"/></svg>',
    warn: '<svg viewBox="0 0 24 24"><path d="M12 4l9 16H3z"/><path d="M12 10v4M12 17v.1"/></svg>',
  };
  const HEAT = ['#ff7a85', '#ff9e6b', '#f5c96a', '#a6dc7f', '#56c2e6', '#7c9cff', '#8790a3'];

  const state = {
    meta: null, view: 'map', focus: null, depth: 1, dir: 'both', mapLevel: 'auto',
    offTypes: new Set(), offRels: new Set(['imports']), showTests: true, showExternal: false, colorLang: false,
    pathFrom: null, pathTo: null, changesMode: 'working', changesBase: 'main', impactDepth: 3,
    history: [], hpos: -1, lastData: null, current: null,
    listMode: false, cluster: 'auto', tableSel: null, routeFilter: '',
  };
  const CLUSTER_AT = 80;     // above this many nodes a graph is grouped by file

  // ------------------------------------------------------------- api
  const cache = new Map();
  async function api(path, params = {}, useCache = false) {
    const qs = new URLSearchParams(Object.entries(params).filter(([, v]) => v !== undefined && v !== null && v !== '')).toString();
    const url = `/api/${path}${qs ? '?' + qs : ''}`;
    if (useCache && cache.has(url)) return cache.get(url);
    busy(true);
    try {
      const r = await fetch(url);
      const j = await r.json();
      if (useCache && r.ok) cache.set(url, j);
      if (!r.ok) throw new Error(j.error || r.statusText);
      return j;
    } finally { busy(false); }
  }
  let busyN = 0;
  function busy(on) { busyN += on ? 1 : -1; const b = $('#busy'); b.classList.toggle('show', busyN > 0); b.firstChild.textContent = 'Loading…'; }

  // ------------------------------------------------------------ graph
  const gv = new window.RippletView($('#graph'), $('#minimap'));
  const nodeColor = (d) => {
    if (d.heat !== undefined) return HEAT[Math.min(d.heat, HEAT.length - 1)];
    if (state.colorLang && d.lang && LANG_COLORS[d.lang]) return LANG_COLORS[d.lang];
    if (d.path) return colorOfGroup(groupKey(d.path));
    if (d.external) return TYPE_COLORS.external;
    return TYPE_COLORS[d.type] || '#8a94a8';
  };
  const isTestish = (d) => d.type === 'test' || /(^|\/)(tests?|spec|__tests__)\/|[._-](test|spec)\.|Tests?\.\w+$/.test(d.file || '');
  gv.style = {
    color: nodeColor,
    radius: (d, deg) => d.size ? Math.max(5, Math.min(34, 3 + Math.sqrt(d.size) * 1.6)) : Math.max(4, Math.min(20, 4 + Math.sqrt(deg || 0) * 1.6 + (d.hop === 0 ? 4 : 0))),
    visible: (d) => {
      if (d.id === state.focus || d.hop === 0 || d.changed) return true;
      if (!state.showExternal && d.external) return false;
      if (!state.showTests && isTestish(d)) return false;
      if (state.offTypes.has(d.external ? 'external' : d.type)) return false;
      return true;
    },
    edgeVisible: (e) => !state.offRels.has(e.rel),
  };

  gv.on('select', (d) => {
    if (d.members) inspectCluster(d);
    else if (d.type === 'dir' || d.type === 'file' && d.path) inspectGroup(d);
    else inspect(d.id);
  });
  gv.on('dblclick', (d) => {
    if (d.members) { openCluster(d.file); return; }
    if (state.view === 'map') { if (d.path) drillMap(d); return; }
    if (state.view === 'explore') expand(d.id);
    else go({ view: 'explore', focus: d.id });
  });
  gv.on('background', () => { gv.select(null); if (!['changes', 'http', 'path', 'routes', 'data', 'health'].includes(state.view)) closeInspector(); });
  gv.on('hover', (d, p) => tooltip(d, p));
  gv.on('context', (d, p) => contextMenu(d, p));

  function tooltip(d, p) {
    const t = $('#tooltip');
    if (!d) { t.style.display = 'none'; return; }
    const loc = d.file ? `${d.file}${d.line ? ':' + d.line : ''}` : (d.path || '');
    const meta = [d.type === 'dir' ? 'folder' : d.type, d.lang, d.size ? `${d.size} symbols` : '',
      d.hop !== undefined && state.view !== 'explore' && state.view !== 'map' ? `distance ${d.hop}` : ''].filter(Boolean).join(' · ');
    t.innerHTML = `<div class="t">${esc(d.label || d.id)}</div>`
      + (d.qn && d.qn !== d.label ? `<div class="q">${esc(d.qn)}</div>` : '')
      + `<div class="m">${esc(meta)}</div>` + (loc ? `<div class="q">${esc(loc)}</div>` : '');
    t.style.display = 'block';
    const W = $('#canvasWrap').clientWidth;
    t.style.left = Math.min(p.x + 16, W - 390) + 'px'; t.style.top = (p.y + 16) + 'px';
  }

  function contextMenu(d, p) {
    const m = $('#ctx');
    m.innerHTML = '';
    const item = (label, fn) => m.append(el('button', { onclick: () => { m.style.display = 'none'; fn(); } }, label));
    if (d.path) {
      item('Open in map', () => drillMap(d));
    } else {
      item('Explore neighbourhood', () => go({ view: 'explore', focus: d.id }));
      item('Impact: what depends on this', () => go({ view: 'impact', focus: d.id }));
      m.append(el('hr'));
      item('Path from here…', () => { state.pathFrom = d; go({ view: 'path' }); });
      item('Path to here…', () => { state.pathTo = d; go({ view: 'path' }); });
      m.append(el('hr'));
      if (state.view === 'explore') item('Expand neighbours', () => expand(d.id));
      item('Hide this node', () => { const n = gv.byId.get(d.id); if (n) { n.d.hidden = true; gv.style.visible = wrapHidden(gv.style.visible); gv.draw(); } });
      item('Copy qualified name', () => navigator.clipboard && navigator.clipboard.writeText(d.qn || d.id));
      if (d.file) item('Copy file:line', () => navigator.clipboard && navigator.clipboard.writeText(`${d.file}:${d.line || 1}`));
    }
    m.style.display = 'block';
    const W = $('#canvasWrap').clientWidth, H = $('#canvasWrap').clientHeight;
    m.style.left = Math.min(p.x, W - 200) + 'px'; m.style.top = Math.min(p.y, H - 260) + 'px';
  }
  let hiddenWrapped = false;
  function wrapHidden(fn) { if (hiddenWrapped) return fn; hiddenWrapped = true; return (d) => !d.hidden && fn(d); }
  document.addEventListener('click', (e) => { if (!e.target.closest('#ctx')) $('#ctx').style.display = 'none'; });

  // ----------------------------------------------------------- views
  function go(patch, push = true) {
    if ('focus' in patch && patch.focus !== state.focus) state.openClusters = new Set();
    Object.assign(state, patch);
    if (push) {
      state.history = state.history.slice(0, state.hpos + 1);
      state.history.push({ view: state.view, focus: state.focus });
      state.hpos = state.history.length - 1;
    }
    writeHash();
    render();
  }
  function writeHash() {
    const p = new URLSearchParams({ v: state.view });
    if (state.focus) p.set('id', state.focus);
    if (state.listMode && (state.view === 'explore' || state.view === 'impact')) p.set('list', '1');
    history.replaceState(null, '', '#' + p.toString());
  }
  function readHash() {
    const p = new URLSearchParams(location.hash.slice(1));
    const v = p.get('v');
    const h = { view: VIEWS.includes(v) ? v : 'map', focus: p.get('id') || null };
    if (p.has('list')) h.listMode = p.get('list') === '1';
    return h;
  }
  function navButtons() {
    $('#back').disabled = state.hpos <= 0;
    $('#fwd').disabled = state.hpos >= state.history.length - 1;
  }

  async function render() {
    navButtons();
    document.querySelectorAll('#views button').forEach((b) => b.classList.toggle('active', b.dataset.view === state.view));
    $('#viewName').textContent = VIEW_TITLES[state.view];
    showEmpty(null);
    page(null);
    try {
      if (state.view === 'map') await viewMap();
      else if (state.view === 'explore') await viewExplore();
      else if (state.view === 'impact') await viewImpact();
      else if (state.view === 'changes') await viewChanges();
      else if (state.view === 'http') await viewHttp();
      else if (state.view === 'path') await viewPath();
      else if (state.view === 'routes') await viewRoutes();
      else if (state.view === 'data') await viewData();
      else if (state.view === 'health') await viewHealth();
    } catch (e) {
      showEmpty('Something went wrong', String(e.message || e));
    }
    legend();
  }

  function showEmpty(title, body, icon) {
    const e = $('#empty');
    if (!title) { e.classList.remove('show'); e.innerHTML = ''; return; }
    e.innerHTML = `<div class="card"><div class="ico">${ICONS[icon || state.view] || ICONS.explore}</div><h2>${esc(title)}</h2><p>${body || ''}</p></div>`;
    e.classList.add('show');
  }

  function toolbar(...items) { const t = $('#toolbar'); t.innerHTML = ''; t.append(...items.filter(Boolean)); }
  function sel(label, value, options, onchange) {
    if (options.length <= 4) {   // few options: a segmented control
      const seg = el('div', { class: 'seg', title: label });
      for (const [v, l] of options) {
        seg.append(el('button', { class: String(v) === String(value) ? 'on' : '', onclick: () => onchange(String(v)) }, l));
      }
      return el('span', { class: 'field' }, label, seg);
    }
    const s = el('select', { onchange: (e) => onchange(e.target.value) }, ...options.map(([v, l]) => el('option', { value: v, selected: String(v) === String(value) }, l)));
    return el('label', { class: 'field' }, label, s);
  }
  function focusPicker(placeholder, onpick, current) {
    return el('button', { class: 'pick' + (current ? ' set' : ''), onclick: () => openPalette(onpick) },
      current ? el('b', {}, current) : placeholder, current ? el('span', { class: 'hint' }, 'change') : null);
  }
  const hint = (t) => el('span', { class: 'hint' }, t);
  function status(parts) {
    const base = $('#status').dataset.base;
    $('#status').innerHTML = parts.filter(Boolean).map((p) => `<span>${p}</span>`).join('')
      ;
    if (base) $('#project').title = base;
  }

  // ---- map
  async function viewMap() {
    toolbar(
            sel('Group by', state.mapLevel, [['auto', 'automatic'], [1, 'top folders'], [2, 'folders (2 levels)'], [3, 'folders (3 levels)'], [4, 'folders (4 levels)'], [5, '5 levels'], [6, '6 levels'], [0, 'files']],
        (v) => { state.mapLevel = v === 'auto' ? 'auto' : +v; render(); }),
      hint( 'Double-click a group to open it · edges = calls, instantiation, inheritance, HTTP'),
    );
    const data = await api('map', { level: state.mapLevel, limit: state.mapLevel === 0 ? 700 : 400 }, true);
    // disambiguate equal folder names (api/.../Features vs web/.../features)
    const seen = {};
    for (const n of data.nodes) { const k = n.label.toLowerCase(); seen[k] = (seen[k] || 0) + 1; }
    const nodes = data.nodes.map((n) => {
      const parts = n.path.split('/');
      const label = seen[n.label.toLowerCase()] > 1 && parts.length > 1 ? parts.slice(-2).join('/') : n.label;
      return Object.assign({}, n, { label, file: n.path });
    });
    gv.setData(nodes, data.edges, { layout: 'force' });
    state.lastData = data;
    state.mapResolved = data.level;
    status([`<b>${data.nodes.length}</b> of ${data.groups_total} groups (depth ${data.level})`, `<b>${data.edges.length}</b> dependency links`]);
    if (!data.nodes.length) showEmpty('No symbols indexed', 'Run <code>ripplet index</code> in this project first.');
  }
  function drillMap(d) {
    if (d.type === 'file' || state.mapResolved === 0) { openFile(d.path); return; }
    state.mapLevel = Math.min(8, (state.mapResolved || 1) + 1);
    render().then(() => {
      const prefix = d.path + '/';
      const hit = gv.nodes.filter((n) => n.d.path === d.path || (n.d.path || '').startsWith(prefix));
      if (hit.length) { gv.setMarks(new Map(hit.map((n) => [n.id, '#6c8cff']))); gv.centerOn(hit[0].id, 1.2); }
    });
  }

  // ---- explore
  async function viewExplore() {
    if (!state.focus) {
      toolbar(focusPicker('Pick a symbol to explore…', (id) => go({ focus: id })));
      const top = await api('search', { q: '', limit: 60 }, true);
      gv.setData(top.results.map((r) => Object.assign({}, r)), [], { layout: 'force' });
      showEmpty('Pick a symbol to explore', 'Most connected symbols are shown. Press <kbd>Ctrl K</kbd> to search, or click a node.');
      status([`<b>${top.results.length}</b> most connected symbols`]);
      return;
    }
    toolbar(
      focusPicker('Pick a symbol…', (id) => go({ focus: id }), gv.byId.get(state.focus)?.d.label || state.focusLabel),
      showToggle(),
      state.listMode ? null : sel('Depth', state.depth, [[1, '1 hop'], [2, '2 hops'], [3, '3 hops']], (v) => { state.depth = +v; render(); }),
      sel('Direction', state.dir, [['both', 'callers + callees'], ['in', 'callers only'], ['out', 'callees only']], (v) => { state.dir = v; render(); }),
      state.listMode ? null : clusterToggle(),
      hint(state.listMode ? 'Every call site, grouped by file' : 'Double-click any node (or a file group) to expand it'),
    );
    if (state.listMode) {
      const node = await api('node', { id: state.focus }, true);
      state.focusLabel = node.node.label;
      gv.setData([], []);
      inspect(state.focus);
      page(siteList(node, state.dir));
      status([`<b>${node.callers_total}</b> callers`, `<b>${node.callees_total}</b> callees`,
        node.unverified && node.unverified.total ? `<b>${node.unverified.total}</b> possible (unverified)` : '']);
      return;
    }
    const data = await api('neighborhood', { id: state.focus, depth: state.depth, dir: state.dir, externals: state.showExternal ? 1 : '' });
    state.focusLabel = (data.nodes.find((n) => n.id === state.focus) || {}).label;
    state.lastNodes = data.nodes;
    const [nodes, edges, clustered] = maybeCluster(data.nodes, data.edges, state.focus);
    gv.setData(nodes, edges, { layout: 'force', center: state.focus, select: state.focus });
    gv.setMarks(new Map([[state.focus, '#6c8cff']]));
    inspect(state.focus);
    status([`<b>${data.nodes.length}</b> nodes`, `<b>${data.edges.length}</b> edges`,
      clustered ? `grouped into <b>${clustered}</b> files — double-click a group to open it` : '',
      data.truncated ? 'truncated at 450 nodes — use filters or a smaller depth' : '']);
  }

  // ---- graph / list, clustering
  function showToggle() {
    return sel('Show', state.listMode ? 'list' : 'graph', [['graph', 'Graph'], ['list', 'List']],
      (v) => { state.listMode = v === 'list'; render(); });
  }
  function clusterToggle() {
    return sel('Group by file', state.cluster, [['auto', 'auto'], ['on', 'on'], ['off', 'off']],
      (v) => { state.cluster = v; render(); });
  }
  // Large neighbourhoods: every file's nodes collapse into one group node
  // (the focus and its own file stay expanded); edges are summed.
  function maybeCluster(nodes, edges, focus) {
    const on = state.cluster === 'on' || (state.cluster === 'auto' && nodes.length > CLUSTER_AT);
    if (!on) return [nodes, edges, 0];
    const focusFile = (nodes.find((n) => n.id === focus) || {}).file;
    const groupOf = new Map();
    const groups = new Map();
    for (const n of nodes) {
      if (n.id === focus || !n.file || n.file === focusFile || state.openClusters?.has(n.file)) continue;
      const g = 'cluster:' + n.file;
      groupOf.set(n.id, g);
      if (!groups.has(g)) groups.set(g, { id: g, label: n.file.split('/').pop(), file: n.file, path: n.file, type: 'file', size: 0, hop: n.hop, members: [] });
      const gg = groups.get(g); gg.size++; gg.members.push(n.id); gg.hop = Math.min(gg.hop ?? 9, n.hop ?? 9);
    }
    // a file with a single node stays a node
    for (const [g, gg] of [...groups]) if (gg.size < 2) { groups.delete(g); groupOf.delete(gg.members[0]); }
    const keep = nodes.filter((n) => !groupOf.has(n.id));
    const merged = new Map();
    for (const e of edges) {
      const s = groupOf.get(e.s) || e.s, t = groupOf.get(e.t) || e.t;
      if (s === t) continue;
      const k = `${s}|${t}|${e.rel}`;
      if (!merged.has(k)) merged.set(k, Object.assign({}, e, { s, t, w: 0 }));
      merged.get(k).w += e.w || 1;
    }
    return [keep.concat([...groups.values()].map((g) => Object.assign({}, g, { label: `${g.label} (${g.size})` }))), [...merged.values()], groups.size];
  }

  // the list mode body: callers / callees grouped by file, with line and code
  function siteList(node, dir) {
    const box = el('div', { class: 'pagebody' });
    const filter = el('input', { class: 'input', placeholder: 'Filter by file, name or code…', autocomplete: 'off' });
    const body = el('div', {});
    const draw = () => {
      const f = filter.value.trim().toLowerCase();
      body.innerHTML = '';
      const sections = [];
      if (dir !== 'out') sections.push(['Called by', node.callers]);
      if (dir !== 'in') sections.push(['Calls', node.callees]);
      for (const [title, items] of sections) {
        const rows = items.filter((e) => !f || `${e.file} ${e.node.label} ${e.code || ''}`.toLowerCase().includes(f));
        body.append(el('h3', {}, `${title} `, el('span', { class: 'n' }, String(rows.length))));
        body.append(siteTable(rows));
      }
      if (dir !== 'out' && node.unverified && node.unverified.total) body.append(unverifiedBlock(node.unverified, f));
    };
    filter.addEventListener('input', draw);
    draw();
    box.append(el('div', { class: 'pagehead' }, el('h2', {}, node.node.label), el('span', { class: 'ns' }, node.node.qn), filter), body);
    return box;
  }
  function siteTable(rows) {
    const t = el('div', { class: 'sites' });
    const byFile = new Map();
    for (const e of rows) { const k = e.file || e.node.file || '(no file)'; if (!byFile.has(k)) byFile.set(k, []); byFile.get(k).push(e); }
    for (const [file, list] of [...byFile].sort()) {
      t.append(el('div', { class: 'file', onclick: () => openFile(file) }, file, el('span', { class: 'n' }, String(list.length))));
      for (const e of list.sort((a, b) => a.line - b.line)) {
        t.append(el('div', { class: 'site', onclick: () => inspect(e.node.id) },
          e.op !== undefined ? el('span', { class: `op op-${(e.op || 'other').toLowerCase()}` }, e.op || '—') : null,
          el('span', { class: 'ln' }, String(e.line || '')),
          el('span', { class: 'who' }, e.node.label),
          el('code', {}, e.code || ''),
          e.lines && e.lines.length > 1 ? el('span', { class: 'more', title: e.lines.join(', ') }, `+${e.lines.length - 1} lines`) : null,
          confBadge(e)));
      }
    }
    if (!rows.length) t.append(el('div', { class: 'note' }, 'Nothing here.'));
    return t;
  }
  function confBadge(e) {
    if (e.conf === 'inferred') return el('span', { class: 'badge inf', title: HOW[e.how] || e.how || 'not proven by types' }, 'inferred');
    if (e.conf === 'ambiguous') return el('span', { class: 'badge amb', title: 'overload not decidable from syntax' }, 'ambiguous');
    return null;
  }
  function unverifiedBlock(u, f) {
    const sites = u.sites.filter((s) => !f || `${s.file} ${s.code}`.toLowerCase().includes(f));
    const box = el('div', { class: 'unverified' },
      el('h3', {}, 'Possible callers (unverified) ', el('span', { class: 'n' }, String(u.total))),
      el('p', { class: 'note' }, 'Calls of the same name on a receiver whose type Ripplet could not establish. Each may or may not be this symbol: open the line to check. Agents are told to treat them as callers unless the code shows otherwise.'));
    const t = el('div', { class: 'sites' });
    for (const s of sites) {
      t.append(el('div', { class: 'site', onclick: () => openFile(s.file, s.line) },
        el('span', { class: 'ln' }, s.line ? String(s.line) : ''), el('span', { class: 'who' }, s.file),
        el('code', {}, s.code || ''), el('span', { class: 'badge unv' }, 'check')));
    }
    if (u.total > u.sites.length) t.append(el('div', { class: 'note' }, `… ${u.total - u.sites.length} more`));
    box.append(t);
    return box;
  }

  function impactList(data, node) {
    const box = el('div', { class: 'pagebody' });
    const filter = el('input', { class: 'input', placeholder: 'Filter by file, name or code…', autocomplete: 'off' });
    const body = el('div', {});
    const via = new Map(data.edges.map((e) => [e.s, e]));
    const draw = () => {
      const f = filter.value.trim().toLowerCase();
      body.innerHTML = '';
      const byHop = new Map();
      for (const n of data.nodes) {
        if (!n.hop) continue;
        const e = via.get(n.id) || {};
        const row = { node: n, file: e.file || n.file, line: e.line || n.line, code: e.code || '', conf: e.conf };
        if (f && !`${row.file} ${n.label} ${row.code}`.toLowerCase().includes(f)) continue;
        if (!byHop.has(n.hop)) byHop.set(n.hop, []);
        byHop.get(n.hop).push(row);
      }
      for (const [hop, rows] of [...byHop].sort((a, b) => a[0] - b[0])) {
        body.append(el('h3', {}, hop === 1 ? 'Direct dependents ' : `Depth ${hop} `, el('span', { class: 'n' }, String(rows.length))));
        body.append(siteTable(rows));
      }
      if (node.unverified && node.unverified.total) body.append(unverifiedBlock(node.unverified, f));
      body.append(el('h3', {}, 'Tests that reach it ', el('span', { class: 'n' }, String(data.tests_total))));
      const tl = el('div', { class: 'list' });
      for (const t of data.tests.filter((t) => !f || `${t.file} ${t.label}`.toLowerCase().includes(f))) tl.append(itemRow(t));
      body.append(tl);
    };
    filter.addEventListener('input', draw);
    draw();
    box.append(el('div', { class: 'pagehead' }, el('h2', {}, `What depends on ${node.node.label}`), el('span', { class: 'ns' }, node.node.qn), filter), body);
    return box;
  }

  function openCluster(file) {
    state.openClusters = state.openClusters || new Set();
    state.openClusters.add(file);
    render();
  }
  async function inspectCluster(d) {
    const p = $('#inspector'); p.innerHTML = '';
    const list = el('div', { class: 'list tabbody' }, el('div', { class: 'group' }, `${d.size} symbols in this file`));
    for (const id of d.members) {
      const n = (state.lastNodes || []).find((x) => x.id === id) || { id, label: id.split(':').pop() };
      list.append(itemRow(n));
    }
    p.append(el('div', { class: 'insp-head' }, el('div', { class: 'kind' }, 'file group'), el('h2', {}, d.file.split('/').pop()),
      el('div', { class: 'ns' }, d.file),
      el('div', { class: 'actions' },
        el('button', { class: 'btn primary', onclick: () => openCluster(d.file) }, 'Expand group'),
        el('button', { class: 'btn', onclick: () => openFile(d.file) }, 'Open file'))), list);
  }

  // full-width page over the canvas (list views, routes, data, health)
  function page(content) {
    const p = $('#page');
    if (!content) { p.hidden = true; p.innerHTML = ''; return; }
    p.innerHTML = ''; p.append(content); p.hidden = false;
  }
  async function expand(id) {
    const data = await api('neighborhood', { id, depth: 1, dir: 'both', externals: state.showExternal ? 1 : '' });
    gv.merge(data.nodes.map((n) => Object.assign({}, n, { hop: undefined })), data.edges, id);
    status([`<b>${gv.nodes.length}</b> nodes`, `<b>${gv.edges.length}</b> edges`, `expanded ${esc(data.nodes.find((n) => n.id === id)?.label || '')}`]);
  }

  // ---- impact
  async function viewImpact() {
    if (!state.focus) {
      toolbar(focusPicker('Pick a symbol…', (id) => go({ focus: id })));
      gv.setData([], []);
      showEmpty('What breaks if this changes?', 'Pick a symbol: every symbol that depends on it, ring by ring, plus the tests that reach it.');
      return;
    }
    toolbar(
      focusPicker('Pick a symbol…', (id) => go({ focus: id }), state.focusLabel),
      showToggle(),
      sel('Depth', state.impactDepth, [[1, '1'], [2, '2'], [3, '3'], [4, '4'], [5, '5']], (v) => { state.impactDepth = +v; render(); }),
      state.listMode ? null : clusterToggle(),
      hint(state.listMode ? 'Dependents by distance and file, with the call that links them' : 'Rings = distance from the changed symbol (red = direct dependents)'),
    );
    const data = await api('impact', { id: state.focus, depth: state.impactDepth });
    const nodes = data.nodes.map((n) => Object.assign({}, n, { heat: n.hop }));
    state.focusLabel = (data.nodes.find((n) => n.id === state.focus) || {}).label;
    state.lastNodes = data.nodes;
    if (state.listMode) {
      gv.setData([], []);
      const node = await api('node', { id: state.focus }, true);
      page(impactList(data, node));
    } else {
      const [cn, ce, clustered] = maybeCluster(nodes, data.edges, state.focus);
      gv.setData(cn.map((n) => Object.assign({ heat: n.hop }, n)), ce, { layout: 'radial', center: state.focus, select: state.focus });
      if (clustered) data.clustered = clustered;
    }
    inspect(state.focus, { impact: data });
    status([`<b>${data.nodes.length - 1}</b> dependents`, ...Object.entries(data.by_depth).filter(([k]) => k !== '0').map(([k, v]) => `depth ${k}: <b>${v}</b>`),
      `<b>${data.files_total}</b> files`, `<b>${data.tests_total}</b> related tests`]);
  }

  // ---- changes
  async function viewChanges() {
    const base = el('input', { type: 'text', value: state.changesBase, size: 12, title: 'Base branch or commit' });
    toolbar(
      sel('Compare', state.changesMode, [['working', 'uncommitted changes'], ['diff', 'branch vs base']], (v) => { state.changesMode = v; render(); }),
      state.changesMode === 'diff' ? el('label', { class: 'field' }, 'base', base) : null,
      state.changesMode === 'diff' ? el('button', { class: 'btn', onclick: () => { state.changesBase = base.value.trim() || 'main'; render(); } }, 'Apply') : null,
      sel('Impact depth', state.impactDepth, [[1, '1'], [2, '2'], [3, '3'], [4, '4']], (v) => { state.impactDepth = +v; render(); }),
      el('button', { class: 'btn', onclick: () => { cache.clear(); render(); } }, 'Refresh'),
    );
    const data = await api('changes', { mode: state.changesMode, base: state.changesBase, depth: state.impactDepth });
    const changed = new Set(data.changed.map((c) => c.id));
    const nodes = data.impact.nodes.map((n) => Object.assign({}, n, { heat: n.hop, changed: changed.has(n.id) }));
    gv.setData(nodes, data.impact.edges, { layout: 'radial' });
    gv.setMarks(new Map([...changed].map((id) => [id, '#ef6b73'])));
    renderChangesPanel(data);
    const files = data.repos.reduce((a, r) => a + (r.files ? r.files.length : 0), 0);
    status([`<b>${files}</b> changed files`, `<b>${data.changed.length}</b> changed symbols`, `<b>${data.impact.total - data.changed.length}</b> affected`, `<b>${data.tests.length}</b> tests to run`]);
    if (!data.changed.length) {
      const noGit = data.repos.every((r) => r.git === false);
      showEmpty(noGit ? 'Not a git repository' : 'No changed symbols',
        noGit ? 'Change tracking needs git.' : (files ? 'Changed files have no indexed symbols (docs, config, or not re-indexed yet).' : 'Working tree is clean. Edit code, or compare a branch against its base.'));
    }
  }
  function renderChangesPanel(data) {
    const p = $('#inspector'); p.innerHTML = '';
    const head = el('div', { class: 'insp-head' }, el('div', { class: 'kind' }, 'change set'), el('h2', {}, `${data.changed.length} changed symbols`));
    const list = el('div', { class: 'list tabbody' });
    for (const r of data.repos) {
      if (!r.files) continue;
      list.append(el('div', { class: 'group' }, `Files — ${r.root.split(/[\\/]/).pop()}`));
      for (const f of r.files) list.append(el('div', { class: 'item', onclick: () => openFile(f.path) }, el('span', { class: 'badge ' + f.status }, f.status), el('span', { class: 'tx' }, el('span', { class: 'nm' }, f.path.split('/').pop()), el('span', { class: 'sub' }, f.path))));
    }
    list.append(el('div', { class: 'group' }, 'Changed symbols'));
    for (const c of data.changed) list.append(itemRow(c, el('span', { class: 'badge ' + (c.status || 'modified') }, c.status || c.change || 'modified')));
    list.append(el('div', { class: 'group' }, `Tests that reach the change (${data.tests.length})`));
    for (const t of data.tests) list.append(itemRow(t));
    if (!data.tests.length) list.append(el('div', { class: 'note' }, 'No test reaches these symbols through the graph.'));
    p.append(head, list);
  }

  // ---- http
  async function viewHttp() {
    toolbar(hint( 'Left: client call sites · Right: route handlers · edge label = method + URL'));
    const data = await api('http', {}, true);
    const targets = new Set(data.edges.map((e) => e.t));
    gv.setData(data.nodes, data.edges, { layout: 'bipartite', side: (d) => targets.has(d.id) ? 1 : 0 });
    state.lastData = data;
    const p = $('#inspector'); p.innerHTML = '';
    const list = el('div', { class: 'list tabbody' });
    const byNode = Object.fromEntries(data.nodes.map((n) => [n.id, n]));
    for (const e of data.edges.slice().sort((a, b) => (a.http || '').localeCompare(b.http || ''))) {
      list.append(el('div', { class: 'item', onclick: () => { gv.select(e.t); gv.centerOn(e.t); inspect(e.t); } },
        el('span', { class: 'tx' }, el('span', { class: 'nm' }, `${byNode[e.s]?.label || e.s} → ${byNode[e.t]?.label || e.t}`),
          el('span', { class: 'sub' }, e.http || 'HTTP')),
        e.cross_repo ? el('span', { class: 'badge' }, 'cross-repo') : null));
    }
    p.append(el('div', { class: 'insp-head' }, el('div', { class: 'kind' }, 'http links'), el('h2', {}, `${data.edges.length} API calls`), el('div', { class: 'ns' }, 'frontend call site → backend handler')), list);
    status([`<b>${data.edges.length}</b> HTTP links`, `<b>${targets.size}</b> handlers`, `<b>${data.nodes.length - targets.size}</b> callers`]);
    if (!data.edges.length) showEmpty('No HTTP links', 'No frontend call matched a backend route in this index.');
  }

  // ---- path
  async function viewPath() {
    const fromBtn = focusPicker('From…', async (id) => { state.pathFrom = await brief(id); render(); }, state.pathFrom && state.pathFrom.label);
    const toBtn = focusPicker('To…', async (id) => { state.pathTo = await brief(id); render(); }, state.pathTo && state.pathTo.label);
    toolbar(fromBtn, el('span', { class: 'hint' }, '→'), toBtn,
      el('button', { class: 'btn', onclick: () => { const t = state.pathFrom; state.pathFrom = state.pathTo; state.pathTo = t; render(); } }, 'Swap'));
    if (!state.pathFrom || !state.pathTo) {
      gv.setData([], []);
      showEmpty('How do two symbols connect?', 'Pick a start and an end (or right-click nodes in any view: “Path from / to here”).');
      return;
    }
    const data = await api('path', { from: state.pathFrom.id, to: state.pathTo.id });
    if (!data.found) { gv.setData([], []); showEmpty('No connection', 'These symbols are not connected through calls, instantiation, inheritance, imports or HTTP.'); return; }
    const order = [state.pathFrom.id, ...data.edges.map((e) => (e.s === state.pathFrom.id ? e.t : e.s))];
    const seq = [state.pathFrom.id];
    for (const e of data.edges) { const last = seq[seq.length - 1]; seq.push(e.s === last ? e.t : e.s); }
    gv.setData(data.nodes, data.edges, { layout: 'layered', order: seq.length ? seq : order });
    gv.setMarks(new Map([[state.pathFrom.id, '#47c299'], [state.pathTo.id, '#ef6b73']]));
    status([`<b>${data.edges.length}</b> hops`]);
    const p = $('#inspector'); p.innerHTML = '';
    const list = el('div', { class: 'list tabbody' });
    const byId = Object.fromEntries(data.nodes.map((n) => [n.id, n]));
    data.edges.forEach((e, i) => {
      list.append(itemRow(byId[seq[i]]));
      list.append(el('div', { class: 'note' }, `${byId[e.s]?.label} —${e.rel}→ ${byId[e.t]?.label}${e.site && e.site !== ':0' ? '  @ ' + e.site : ''}`));
    });
    list.append(itemRow(byId[seq[seq.length - 1]]));
    p.append(el('div', { class: 'insp-head' }, el('div', { class: 'kind' }, 'path'), el('h2', {}, `${data.edges.length} hops`)), list);
  }
  async function brief(id) { const n = await api('node', { id }); return n.node; }

  // ---- routes
  async function viewRoutes() {
    toolbar(hint('Every HTTP route the index found and the handler it runs · click a handler to explore it'));
    gv.setData([], []);
    const data = await api('routes', {}, true);
    const filter = el('input', { class: 'input', placeholder: 'Filter by path, method or handler…', value: state.routeFilter, autocomplete: 'off' });
    const body = el('div', { class: 'rtable' });
    const draw = () => {
      const f = filter.value.trim().toLowerCase();
      state.routeFilter = filter.value;
      body.innerHTML = '';
      body.append(el('div', { class: 'rrow head' }, el('span', {}, 'Method'), el('span', {}, 'Path'), el('span', {}, 'Handler'), el('span', {}, 'Declared at')));
      let shown = 0;
      for (const r of data.routes) {
        const h = r.handler;
        if (f && !`${r.method} ${r.path} ${h ? h.qn : r.handler_text}`.toLowerCase().includes(f)) continue;
        shown++;
        body.append(el('div', { class: 'rrow' },
          el('span', {}, el('span', { class: `method m-${(r.method || 'any').toLowerCase()}` }, r.method || 'ANY')),
          el('span', { class: 'path' }, r.path),
          h ? el('a', { class: 'handler', title: h.qn, onclick: () => go({ view: 'explore', focus: h.id }) }, h.qn.split('\\').pop())
            : el('span', { class: 'handler missing', title: 'The handler named in the route could not be found in the index' }, r.handler_text || '—'),
          el('a', { class: 'decl', onclick: () => openFile(r.file, r.line) }, `${r.file}:${r.line}`),
          r.callers.length ? el('span', { class: 'badge http', title: r.callers.map((c) => `${c.node.label} @ ${c.file}:${c.line}`).join('\n') }, `${r.callers.length} client call${r.callers.length > 1 ? 's' : ''}`) : null));
      }
      if (!shown) body.append(el('div', { class: 'note' }, data.total ? 'No route matches the filter.' : 'No HTTP routes in this index.'));
    };
    filter.addEventListener('input', draw);
    draw();
    page(el('div', { class: 'pagebody' }, el('div', { class: 'pagehead' }, el('h2', {}, 'Routes'),
      el('span', { class: 'ns' }, `${data.total} routes · ${data.unbound} without a resolved handler`), filter), body));
    status([`<b>${data.total}</b> routes`, data.unbound ? `<b>${data.unbound}</b> handler not found` : 'every handler resolved']);
  }

  // ---- data (database tables)
  async function viewData() {
    toolbar(hint('Tables from migrations / schema files and the code whose SQL reads or writes them'));
    const data = await api('tables', {}, true);
    if (!data.total) { gv.setData([], []); showEmpty('No database tables', 'No CREATE TABLE statements (migrations, schema files) were found in this index.', 'warn'); return; }
    const cur = state.tableSel && data.tables.find((t) => t.id === state.tableSel) ? state.tableSel : data.tables[0].id;
    state.tableSel = cur;
    const detail = await api('tables', { id: cur }, true);
    const side = el('div', { class: 'tlist' });
    const tf = el('input', { class: 'input', placeholder: 'Filter tables…', autocomplete: 'off' });
    const drawSide = () => {
      const f = tf.value.trim().toLowerCase();
      side.querySelectorAll('.titem').forEach((x) => x.remove());
      for (const t of data.tables) {
        if (f && !t.name.toLowerCase().includes(f)) continue;
        side.append(el('div', { class: 'titem' + (t.id === cur ? ' on' : ''), onclick: () => { state.tableSel = t.id; render(); } },
          el('span', { class: 'nm' }, t.name), el('span', { class: 'n', title: `${t.columns} columns · ${t.users} code locations` }, String(t.users))));
      }
    };
    side.append(tf); tf.addEventListener('input', drawSide); drawSide();
    const ops = {};
    for (const u of detail.users) ops[u.op || 'other'] = (ops[u.op || 'other'] || 0) + 1;
    const main = el('div', { class: 'tmain' },
      el('div', { class: 'pagehead' }, el('h2', {}, detail.table.label),
        el('a', { class: 'ns', onclick: () => openFile(detail.table.file, detail.table.line) }, `${detail.table.file}:${detail.table.line}`),
        el('span', { class: 'ops' }, ...Object.entries(ops).sort((a, b) => b[1] - a[1]).map(([o, n]) => el('span', { class: `op op-${o.toLowerCase()}` }, `${o} ${n}`)))),
      el('h3', {}, 'Columns ', el('span', { class: 'n' }, String(detail.columns.length))),
      el('div', { class: 'cols' }, ...detail.columns.map((c) => el('span', { class: 'col', title: `${c.users} code locations name this column` }, c.name, c.users ? el('b', {}, String(c.users)) : null))),
      el('h3', {}, 'Used by ', el('span', { class: 'n' }, String(detail.users_total))),
      siteTable(detail.users.map((u) => Object.assign({}, u, { conf: '' }))));
    gv.setData([], []);
    page(el('div', { class: 'pagebody split' }, side, main));
    status([`<b>${data.total}</b> tables`, `<b>${detail.users_total}</b> uses of ${esc(detail.table.label)}`]);
  }

  // ---- health
  async function viewHealth() {
    toolbar(hint('How much of the code Ripplet can answer for, per language · freshness · agent setup'),
      el('button', { class: 'btn', onclick: () => { cache.clear(); render(); } }, 'Refresh'));
    gv.setData([], []);
    const h = await api('health', {});
    const age = h.indexed_at ? Math.round((Date.now() / 1000 - h.indexed_at) / 60) : null;
    const cards = el('div', { class: 'hcards' },
      hcard('Indexed', age === null ? '—' : age < 60 ? `${age} min ago` : age < 1440 ? `${Math.round(age / 60)} h ago` : `${Math.round(age / 1440)} days ago`,
        h.stale_total ? `${h.stale_total} file${h.stale_total > 1 ? 's' : ''} edited since — run ripplet update` : 'up to date', h.stale_total ? 'warn' : 'ok'),
      hcard('Graph', `${h.nodes.toLocaleString()} nodes`, `${h.edges.toLocaleString()} edges`),
      ...h.agents.map((a) => hcard('Agents', a.mcp && a.claude_rules ? (a.strict ? 'strict mode' : 'connected') : 'not set up',
        [a.mcp ? 'MCP ✓' : 'MCP ✗', a.claude_rules ? 'CLAUDE.md ✓' : 'CLAUDE.md ✗', a.agents_rules ? 'AGENTS.md ✓' : 'AGENTS.md ✗', a.claude_hooks ? 'hooks ✓' : 'hooks ✗'].join(' · '),
        a.mcp && a.claude_rules ? 'ok' : 'warn')));
    const rows = el('div', { class: 'ltable' },
      el('div', { class: 'lrow head' }, el('span', {}, 'Language'), el('span', {}, 'Files'), el('span', {}, 'Parse'), el('span', {}, 'Symbols'),
        el('span', { title: 'Of the calls that name a project function or method: the share Ripplet proved' }, 'Project calls proven'),
        el('span', { title: 'Calls proven only by name (one method with that name), marked inferred' }, 'Inferred'),
        el('span', { title: 'Calls into libraries / the runtime (not project code)' }, 'Library calls')));
    const other = h.languages.filter((L) => L.files && !L.symbols && !L.calls);
    for (const L of h.languages) {
      if (!L.files || (!L.symbols && !L.calls)) continue;
      const share = L.bound_share;
      rows.append(el('div', { class: 'lrow' },
        el('span', { class: 'lang' }, el('i', { style: `background:${LANG_COLORS[L.language] || '#8a94a8'}` }), L.language),
        el('span', {}, String(L.files)),
        el('span', { class: L.partial ? 'warn' : 'ok', title: L.partial_files.join('\n') }, L.partial ? `${L.partial} partial` : 'ok'),
        el('span', {}, String(L.symbols)),
        share === null ? el('span', { class: 'dim' }, '—') : el('span', { class: 'meter' }, el('span', { class: 'bar', style: `background:linear-gradient(to right, ${share > 0.9 ? 'var(--ok)' : share > 0.75 ? 'var(--warn)' : 'var(--bad)'} ${Math.round(share * 100)}%, var(--surface2) ${Math.round(share * 100)}%)` }),
          el('b', {}, `${Math.round(share * 100)}%`), el('span', { class: 'dim' }, ` ${L.calls}/${L.calls + L.possible}`)),
        el('span', { class: L.inferred ? 'warn' : 'dim' }, String(L.inferred || 0)),
        el('span', { class: 'dim' }, String(L.library ?? '—'))));
    }
    const stale = h.stale_total ? el('div', {}, el('h3', {}, 'Edited since the last index ', el('span', { class: 'n' }, String(h.stale_total))),
      el('div', { class: 'list' }, ...h.stale_files.map((f) => el('div', { class: 'item', onclick: () => openFile(f) }, el('span', { class: 'tx' }, el('span', { class: 'nm' }, f.split('/').pop()), el('span', { class: 'sub' }, f)))))) : null;
    page(el('div', { class: 'pagebody' }, el('div', { class: 'pagehead' }, el('h2', {}, 'Index health')), cards,
      el('h3', {}, 'Languages'), rows,
      other.length ? el('p', { class: 'note' }, 'Also indexed (no code symbols): ' + other.map((L) => `${L.language} ${L.files}`).join(' · ')) : null,
      el('p', { class: 'note' }, '“Project calls proven”: of the calls whose name matches a function or method of this project, the share Ripplet bound with types or imports. The rest are listed to agents as unverified, never guessed silently.'),
      stale));
    status([`<b>${h.languages.filter((l) => l.files).length}</b> languages`, h.stale_total ? `<b>${h.stale_total}</b> stale files` : 'index is fresh']);
  }
  function hcard(title, big, small, tone) {
    return el('div', { class: 'hcard ' + (tone || '') }, el('span', { class: 't' }, title), el('b', {}, big), el('span', { class: 's' }, small));
  }

  // -------------------------------------------------------- inspector
  function itemRow(n, extra) {
    if (!n) return el('div');
    return el('div', { class: 'item', title: n.qn || '', onclick: () => { if (gv.byId.has(n.id)) { gv.select(n.id); gv.centerOn(n.id); } inspect(n.id); } },
      el('i', { style: `background:${nodeColor(n)}` }),
      el('span', { class: 'tx' }, el('span', { class: 'nm' }, n.label || n.id),
        el('span', { class: 'sub' }, n.file ? `${n.file}:${n.line || ''}` : (n.qn || ''))),
      extra || null);
  }

  let inspectSeq = 0;
  async function inspect(id, extra = {}) {
    const seq = ++inspectSeq;
    const data = await api('node', { id }, true);
    if (seq !== inspectSeq) return;
    state.current = data;
    const n = data.node, p = $('#inspector');
    p.innerHTML = '';
    const head = el('div', { class: 'insp-head' },
      el('div', { class: 'kind' }, el('i', { style: `background:${nodeColor(n)}` }), n.type, n.lang ? ` · ${n.lang}` : '', n.external ? ' · external' : ''),
      el('h2', {}, n.label),
      n.qn !== n.label ? el('div', { class: 'ns' }, n.qn.endsWith('.' + n.label) ? n.qn.slice(0, -n.label.length - 1) : n.qn) : null,
      n.file ? el('a', { class: 'loc', title: 'Open file', onclick: () => openFile(n.file, n.line) }, `${n.file}:${n.line}${data.end && data.end !== n.line ? '–' + data.end : ''}`) : null,
      data.parents && data.parents.length ? el('div', { class: 'crumbs' }, ...data.parents.flatMap((c, i) => [i ? el('span', {}, '›') : null, el('a', { onclick: () => inspect(c.id) }, c.label)])) : null,
      el('div', { class: 'actions' },
        el('button', { class: 'btn primary', onclick: () => go({ view: 'explore', focus: n.id }) }, 'Explore'),
        el('button', { class: 'btn', onclick: () => go({ view: 'impact', focus: n.id }) }, 'Impact'),
        el('button', { class: 'btn', onclick: () => { state.pathFrom = n; go({ view: 'path' }); } }, 'Path from'),
        el('button', { class: 'btn', onclick: () => { state.pathTo = n; go({ view: 'path' }); } }, 'Path to'),
        ['function', 'method', 'class', 'interface', 'struct', 'trait', 'type', 'enum'].includes(n.type) && !n.external
          ? el('button', { class: 'btn', title: 'Preview a rename: the lines Ripplet would change and the ones to check', onclick: () => renamePrompt(n) }, 'Rename…') : null));
    const loc = data.source && data.source.length ? data.source.length : 0;
    const stats = el('div', { class: 'stats' },
      stat(data.callers_total, 'used by'), stat(data.callees_total, 'uses'), stat(data.tests.length, 'tests'), stat(loc || '—', 'lines'));
    const unv = data.unverified || { sites: [], total: 0 };
    const tabs = [
      ['Source', loc, () => sourceView(data)],
      ['Used by', data.callers_total, () => edgeList(data.callers, 'No callers in the index.')],
      unv.total ? ['Possible', unv.total, () => unverifiedBlock(unv, '')] : null,
      ['Uses', data.callees_total, () => edgeList(data.callees, 'Calls nothing the index could resolve.', data.unresolved)],
      ['Tests', data.tests.length, () => listOf(data.tests, 'No test reaches this symbol through the graph.')],
      ['Members', data.children.length, () => listOf(data.children, 'No members.')],
    ].filter(Boolean);
    if (extra.impact) tabs.unshift(['Impact', extra.impact.nodes.length - 1, () => impactView(extra.impact)]);
    const tabbar = el('div', { class: 'tabs' }), body = el('div', { class: 'tabbody' });
    const show = (i) => { [...tabbar.children].forEach((b, j) => b.classList.toggle('active', i === j)); body.innerHTML = ''; body.append(tabs[i][2]()); state.tab = tabs[i][0]; };
    tabs.forEach(([name, count], i) => tabbar.append(el('button', { onclick: () => show(i) }, name, el('span', { class: 'n' }, count))));
    p.append(head, stats, tabbar, body);
    const want = tabs.findIndex((t) => t[0] === state.tab);
    show(want >= 0 ? want : 0);
  }
  const stat = (v, l) => el('div', { class: 'stat' }, el('b', {}, String(v)), el('span', {}, l));

  function edgeList(items, empty, unresolved) {
    const box = el('div', { class: 'list' });
    if (!items.length) box.append(el('div', { class: 'note' }, empty));
    const groups = {};
    for (const e of items) (groups[e.rel] = groups[e.rel] || []).push(e);
    for (const [rel, list] of Object.entries(groups)) {
      const inf = list.filter((e) => e.conf === 'inferred').length;
      box.append(el('div', { class: 'group' }, `${rel} (${list.length})`, inf ? el('span', { class: 'gnote' }, ` · ${inf} inferred`) : null));
      for (const e of list) {
        const where = e.file ? `${e.file}:${e.line}` + (e.lines && e.lines.length > 1 ? ` (+${e.lines.length - 1})` : '') : (e.node.file || '');
        box.append(el('div', { class: 'item' + (e.conf === 'inferred' ? ' inferred' : ''), title: (e.node.qn || '') + (e.lines && e.lines.length > 1 ? `\nlines ${e.lines.join(', ')}` : ''),
          onclick: () => { if (gv.byId.has(e.node.id)) { gv.select(e.node.id); gv.centerOn(e.node.id); } inspect(e.node.id); } },
          el('i', { style: `background:${nodeColor(e.node)}` }),
          el('span', { class: 'tx' }, el('span', { class: 'nm' }, e.node.label),
            el('span', { class: 'sub' }, (e.via ? `via ${e.via} · ` : '') + where),
            e.code ? el('span', { class: 'code1' }, e.code) : null),
          e.http ? el('span', { class: 'badge http' }, e.http) : null,
          confBadge(e)));
      }
    }
    if (unresolved && unresolved.length) {
      box.append(el('div', { class: 'group' }, `unresolved (${unresolved.length})`));
      box.append(el('div', { class: 'note' }, unresolved.join(', ')));
    }
    return box;
  }
  // ---- rename preview (dry run of rename_symbol: nothing is written)
  async function renamePrompt(n) {
    const p = $('#inspector'); p.innerHTML = '';
    const input = el('input', { class: 'input', value: n.label, autocomplete: 'off' });
    const out = el('div', { class: 'tabbody' });
    const run = async () => {
      const to = input.value.trim();
      if (!to || to === n.label) return;
      out.innerHTML = '';
      const r = await api('rename', { id: n.id, to });
      out.append(renameResult(r));
    };
    input.addEventListener('keydown', (e) => { if (e.key === 'Enter') run(); });
    p.append(el('div', { class: 'insp-head' }, el('div', { class: 'kind' }, 'rename preview'), el('h2', {}, n.label), el('div', { class: 'ns' }, n.qn),
      el('div', { class: 'rename-in' }, input, el('button', { class: 'btn primary', onclick: run }, 'Preview')),
      el('div', { class: 'note' }, 'Shows what the rename_symbol tool would change. Nothing is written from the UI.'),
      el('div', { class: 'actions' }, el('button', { class: 'btn', onclick: () => inspect(n.id) }, '‹ Back'))), out);
    setTimeout(() => { input.focus(); input.select(); }, 20);
  }
  function renameResult(r) {
    const box = el('div', { class: 'list' });
    if (r.error) { box.append(el('div', { class: 'note' }, r.error)); return box; }
    let section = 'change';
    for (const raw of r.text.split('\n')) {
      const line = raw.trimEnd();
      if (!line) continue;
      if (line.startsWith('# ')) continue;
      if (line.startsWith('===')) {
        section = 'check';
        box.append(el('div', { class: 'group warn' }, line.replace(/^=+\s*/, '').split(' - ')[0]),
          el('div', { class: 'note' }, 'Not renamed: same-name calls on receivers of unknown type. Check each one.'));
        continue;
      }
      const mFile = line.match(/^--\s+(.+?):\s+([\d, ]+)$/);
      if (mFile) {
        const [, file, lines] = mFile;
        box.append(el('div', { class: 'item', onclick: () => openFile(file, +lines.split(',')[0]) },
          el('span', { class: 'badge modified' }, 'edit'),
          el('span', { class: 'tx' }, el('span', { class: 'nm' }, file.split('/').pop()), el('span', { class: 'sub' }, `${file} · lines ${lines}`))));
        continue;
      }
      const mSite = line.match(/^\s+(\S+?):(\d+)\s+(.*)$/);
      if (section === 'check' && mSite) {
        const [, file, ln, code] = mSite;
        box.append(el('div', { class: 'item', onclick: () => openFile(file, +ln) },
          el('span', { class: 'badge unv' }, 'check'),
          el('span', { class: 'tx' }, el('span', { class: 'nm' }, `${file.split('/').pop()}:${ln}`), el('span', { class: 'code1' }, code))));
        continue;
      }
      box.append(el('div', { class: 'note' }, line));
    }
    return box;
  }

  function listOf(items, empty) {
    const box = el('div', { class: 'list' });
    if (!items.length) box.append(el('div', { class: 'note' }, empty));
    for (const n of items) box.append(itemRow(n));
    return box;
  }
  function impactView(data) {
    const box = el('div', {});
    const max = Math.max(1, ...Object.entries(data.by_depth).filter(([k]) => k !== '0').map(([, v]) => v));
    const bars = el('div', { class: 'bars' });
    for (const [d, v] of Object.entries(data.by_depth)) {
      if (d === '0') continue;
      bars.append(el('div', { class: 'bar' }, d === '1' ? 'direct' : `depth ${d}`, el('div', {}, el('span', { style: `width:${(v / max) * 100}%;background:${HEAT[Math.min(+d, HEAT.length - 1)]}` })), el('b', {}, String(v))));
    }
    box.append(bars);
    const list = el('div', { class: 'list' });
    const byHop = data.nodes.filter((n) => n.hop > 0).sort((a, b) => a.hop - b.hop);
    let cur = -1;
    for (const n of byHop.slice(0, 400)) {
      if (n.hop !== cur) { cur = n.hop; list.append(el('div', { class: 'group' }, cur === 1 ? 'direct dependents' : `depth ${cur}`)); }
      list.append(itemRow(n));
    }
    list.append(el('div', { class: 'group' }, `files (${data.files_total})`));
    for (const f of data.files.slice(0, 200)) list.append(el('div', { class: 'item', onclick: () => openFile(f) }, el('span', { class: 'tx' }, el('span', { class: 'nm' }, f.split('/').pop()), el('span', { class: 'sub' }, f))));
    list.append(el('div', { class: 'group' }, `tests (${data.tests_total})`));
    for (const t of data.tests) list.append(itemRow(t));
    box.append(list);
    return box;
  }

  // ------------------------------------------------------ source view
  const KW = /\b(abstract|async|await|break|case|catch|class|const|continue|def|default|defer|do|else|enum|export|extends|false|final|finally|fn|for|from|func|function|go|if|impl|implements|import|in|interface|internal|is|let|match|mod|module|namespace|new|nil|none|null|object|open|override|package|private|protected|pub|public|raise|readonly|return|sealed|self|static|struct|super|switch|this|throw|trait|true|try|type|typeof|use|using|val|var|void|when|where|while|with|yield)\b/g;
  function highlight(line) {
    const out = [];
    const re = /("(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'|`(?:[^`\\]|\\.)*`)|(\/\/.*$|#(?![\[{]).*$|--.*$)|(\b\d+(?:\.\d+)?\b)/g;
    let last = 0, m;
    while ((m = re.exec(line))) {
      out.push(esc(line.slice(last, m.index)).replace(KW, '<span class="k">$1</span>'));
      const cls = m[1] ? 's' : m[2] ? 'c' : 'nu';
      out.push(`<span class="${cls}">${esc(m[0])}</span>`);
      last = m.index + m[0].length;
    }
    out.push(esc(line.slice(last)).replace(KW, '<span class="k">$1</span>'));
    return out.join('');
  }
  function codeBlock(lines, hl) {
    const box = el('div', { class: 'code' });
    box.innerHTML = lines.map((l) => `<div class="ln${hl && hl.has(l.n) ? ' hl' : ''}"><span class="no">${l.n}</span><span>${highlight(l.t)}</span></div>`).join('');
    return box;
  }
  function sourceView(data) {
    if (!data.source || !data.source.length) return el('div', { class: 'note' }, data.node.external ? 'External symbol: no source in this workspace.' : 'Source not available (file moved or not readable).');
    const hl = new Set(data.callees.filter((e) => e.file === data.node.file).map((e) => e.line));
    return codeBlock(data.source, hl);
  }

  async function openFile(path, line) {
    const data = await api('file', { path }, true);
    const p = $('#inspector'); p.innerHTML = '';
    p.append(el('div', { class: 'insp-head' }, el('div', { class: 'kind' }, 'file'), el('h2', {}, path.split('/').pop()), el('div', { class: 'ns' }, path),
      el('div', { class: 'actions' }, el('button', { class: 'btn', onclick: () => revealInTree(path) }, 'Show in explorer'))));
    const outline = el('div', { class: 'list tabbody' }, el('div', { class: 'group' }, `symbols (${data.symbols.length})`));
    for (const s of data.symbols) outline.append(itemRow(s));
    p.append(outline);
    if (data.source) {
      const lines = data.source.map((t, i) => ({ n: i + 1, t }));
      const cb = codeBlock(lines, line ? new Set([line]) : null);
      p.append(el('div', { class: 'tabbody' }, cb));
      if (line) setTimeout(() => { const r = cb.children[line - 1]; if (r) r.scrollIntoView({ block: 'center' }); }, 30);
    }
  }
  function inspectGroup(d) {
    const p = $('#inspector'); p.innerHTML = '';
    const ins = state.lastData && state.lastData.edges ? state.lastData.edges.filter((e) => e.t === d.id) : [];
    const outs = state.lastData && state.lastData.edges ? state.lastData.edges.filter((e) => e.s === d.id) : [];
    const lab = (id) => (gv.byId.get(id)?.d.path || id);
    const list = el('div', { class: 'list tabbody' });
    list.append(el('div', { class: 'group' }, `depends on (${outs.length})`));
    for (const e of outs.sort((a, b) => b.w - a.w)) list.append(el('div', { class: 'item', onclick: () => { gv.select(e.t); gv.centerOn(e.t); inspectGroup(gv.byId.get(e.t).d); } }, el('i', { style: `background:${colorOfGroup(groupKey(lab(e.t)))}` }), el('span', { class: 'tx' }, el('span', { class: 'nm' }, lab(e.t)), el('span', { class: 'sub' }, `${e.w} links · ${Object.keys(e.kinds || {}).join(', ')}`))));
    list.append(el('div', { class: 'group' }, `used by (${ins.length})`));
    for (const e of ins.sort((a, b) => b.w - a.w)) list.append(el('div', { class: 'item', onclick: () => { gv.select(e.s); gv.centerOn(e.s); inspectGroup(gv.byId.get(e.s).d); } }, el('i', { style: `background:${colorOfGroup(groupKey(lab(e.s)))}` }), el('span', { class: 'tx' }, el('span', { class: 'nm' }, lab(e.s)), el('span', { class: 'sub' }, `${e.w} links · ${Object.keys(e.kinds || {}).join(', ')}`))));
    p.append(el('div', { class: 'insp-head' }, el('div', { class: 'kind' }, d.type === 'file' ? 'file' : 'folder', d.lang ? ` · ${d.lang}` : ''), el('h2', {}, d.label), el('div', { class: 'ns' }, d.path),
      el('div', { class: 'actions' },
        el('button', { class: 'btn primary', onclick: () => drillMap(d) }, d.type === 'file' ? 'Open file' : 'Open'),
        el('button', { class: 'btn', onclick: () => revealInTree(d.path) }, 'Show in explorer'))),
      el('div', { class: 'stats' }, stat(d.size, 'symbols'), stat(outs.length, 'depends on'), stat(ins.length, 'used by'), stat(d.lang || '—', 'language')), list);
  }

  // ------------------------------------------------------------ legend
  function legend() {
    const L = $('#legend'); L.innerHTML = '';
    if (state.view === 'impact' || state.view === 'changes') {
      ['changed / focus', 'direct', 'depth 2', 'depth 3', 'depth 4+'].forEach((t, i) => L.append(el('span', {}, el('i', { style: `background:${HEAT[i]}` }), t)));
    } else if (state.view === 'map') {
      const groups = [...new Set(gv.nodes.map((n) => groupKey(n.d.path)))].slice(0, 10);
      for (const g of groups) L.append(el('span', {}, el('i', { style: `background:${colorOfGroup(g)}` }), g));
    } else {
      const types = new Set(gv.nodes.map((n) => (n.d.external ? 'external' : n.d.type)));
      for (const t of types) L.append(el('span', {}, el('i', { style: `background:${state.colorLang ? '#888' : (TYPE_COLORS[t] || '#8a94a8')}` }), t));
    }
    const rels = new Set(gv.edges.map((e) => e.d.rel));
    for (const r of rels) L.append(el('span', {}, el('b', { style: `border-color:${window.RIPPLET_REL_COLORS[r] || '#8a94a8'}` }), r));
    if (gv.edges.some((e) => e.d.conf === 'ambiguous')) L.append(el('span', {}, el('b', { style: 'border-top-style:dashed;border-color:#8a94a8' }), 'ambiguous'));
    if (gv.edges.some((e) => e.d.conf === 'inferred')) L.append(el('span', { title: 'Linked by name only (one method with that name), not proven by types' }, el('b', { style: 'border-top-style:dotted;border-color:#f5b971' }), 'inferred'));
  }

  // ----------------------------------------------------------- palette
  let palettePick = null, paletteSel = 0, paletteItems = [];
  function openPalette(onpick) {
    palettePick = onpick || null;
    $('#palette').hidden = false;
    const i = $('#paletteInput'); i.value = ''; i.focus();
    runSearch('');
  }
  function closePalette() { $('#palette').hidden = true; palettePick = null; }
  let searchTimer = null;
  async function runSearch(q) {
    const data = await api('search', { q, limit: 40 }, true);
    paletteItems = data.results; paletteSel = 0;
    const box = $('#paletteResults'); box.innerHTML = '';
    paletteItems.forEach((r, i) => {
      box.append(el('div', { class: 'item' + (i === 0 ? ' sel' : ''), onmousemove: () => markSel(i), onclick: (e) => choose(i, e.shiftKey) },
        el('i', { style: `background:${nodeColor(r)}` }),
        el('span', { class: 'tx' }, el('span', { class: 'nm' }, r.label), el('span', { class: 'qn' }, r.type === 'file' ? r.file : (r.file ? `${r.file}:${r.line}` : r.qn))),
        el('span', { class: 't' }, `${r.type}${r.degree ? ' · ' + r.degree + ' links' : ''}`)));
    });
    if (!paletteItems.length) box.append(el('div', { class: 'note' }, q ? 'No matches.' : 'Type to search.'));
  }
  function markSel(i) { paletteSel = i; [...$('#paletteResults').children].forEach((c, j) => c.classList.toggle('sel', j === i)); }
  function choose(i, impact) {
    const r = paletteItems[i]; if (!r) return;
    const pick = palettePick; closePalette();
    if (r.type === 'file') { openFile(r.file); return; }
    if (pick) pick(r.id);
    else go({ view: impact ? 'impact' : 'explore', focus: r.id });
  }
  $('#paletteInput').addEventListener('input', (e) => { clearTimeout(searchTimer); searchTimer = setTimeout(() => runSearch(e.target.value), 120); });
  $('#paletteInput').addEventListener('keydown', (e) => {
    if (e.key === 'ArrowDown') { e.preventDefault(); markSel(Math.min(paletteItems.length - 1, paletteSel + 1)); $('#paletteResults').children[paletteSel]?.scrollIntoView({ block: 'nearest' }); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); markSel(Math.max(0, paletteSel - 1)); $('#paletteResults').children[paletteSel]?.scrollIntoView({ block: 'nearest' }); }
    else if (e.key === 'Enter') { e.preventDefault(); choose(paletteSel, e.shiftKey); }
    else if (e.key === 'Escape') closePalette();
  });
  $('#palette').addEventListener('mousedown', (e) => { if (e.target.id === 'palette') closePalette(); });
  $('#helpDlg').addEventListener('mousedown', (e) => { if (e.target.id === 'helpDlg') $('#helpDlg').hidden = true; });

  // -------------------------------------------------------------- tree
  let treeData = null;
  const openDirs = new Set(['']);
  async function loadTree() { treeData = await api('tree', {}, true); drawTree(); }
  function drawTree() {
    const box = $('#tree'); box.innerHTML = '';
    const f = $('#treeFilter').value.trim().toLowerCase();
    const walk = (n, depth) => {
      for (const k of n.children) {
        if (f && !matchTree(k, f)) continue;
        const open = f ? true : openDirs.has(k.path);
        const row = el('div', { class: 'row', style: `padding-left:${4 + depth * 12}px`, 'data-path': k.path,
          onclick: () => { if (k.file) openFile(k.path); else { open ? openDirs.delete(k.path) : openDirs.add(k.path); drawTree(); } } },
          el('span', { class: 'tw' }, k.file ? '' : (open ? '▾' : '▸')), depth === 0 ? el('span', { class: 'dotc', style: `background:${colorOfGroup(groupKey(k.path))}` }) : null, el('span', { class: 'nm' }, k.name), el('span', { class: 'ct' }, k.count));
        box.append(row);
        if (!k.file && open) walk(k, depth + 1);
      }
    };
    if (treeData) walk(treeData, 0);
  }
  function matchTree(n, f) { return n.path.toLowerCase().includes(f) || n.children.some((c) => matchTree(c, f)); }
  function revealInTree(path) {
    const parts = path.split('/');
    for (let i = 1; i < parts.length; i++) openDirs.add(parts.slice(0, i).join('/'));
    $('#treeFilter').value = ''; drawTree();
    const row = [...document.querySelectorAll('#tree .row')].find((r) => r.dataset.path === path);
    if (row) { document.querySelectorAll('#tree .row.sel').forEach((r) => r.classList.remove('sel')); row.classList.add('sel'); row.scrollIntoView({ block: 'center' }); }
  }
  $('#treeFilter').addEventListener('input', () => drawTree());

  // ------------------------------------------------------------ filters
  function filters() {
    const m = state.meta;
    const tf = $('#typeFilters'); tf.innerHTML = '';
    const types = Object.entries(m.types).filter(([t]) => m.symbol_types.includes(t)).sort((a, b) => b[1] - a[1]);
    types.push(['external', m.types.external_symbol || 0]);
    for (const [t, n] of types) {
      const c = el('span', { class: 'chip' + (state.offTypes.has(t) ? ' off' : ''), onclick: () => { state.offTypes.has(t) ? state.offTypes.delete(t) : state.offTypes.add(t); c.classList.toggle('off'); gv.draw(); } },
        el('span', { class: 'dot', style: `background:${TYPE_COLORS[t] || '#8a94a8'}` }), t, el('span', { class: 'n' }, n));
      tf.append(c);
    }
    const rf = $('#relFilters'); rf.innerHTML = '';
    const rels = Object.entries(m.relations).filter(([r]) => m.dep_relations.includes(r)).sort((a, b) => b[1] - a[1]);
    for (const [r, n] of rels) {
      const c = el('span', { class: 'chip' + (state.offRels.has(r) ? ' off' : ''), onclick: () => { state.offRels.has(r) ? state.offRels.delete(r) : state.offRels.add(r); c.classList.toggle('off'); gv.draw(); } },
        el('span', { class: 'dot', style: `background:${window.RIPPLET_REL_COLORS[r] || '#8a94a8'}` }), r, el('span', { class: 'n' }, n));
      rf.append(c);
    }
  }
  $('#optTests').addEventListener('change', (e) => { state.showTests = e.target.checked; gv.draw(); });
  $('#optExternal').addEventListener('change', (e) => { state.showExternal = e.target.checked; if (state.view === 'explore') render(); else gv.draw(); });
  $('#optLabels').addEventListener('change', (e) => { gv.opts.labels = e.target.checked; gv.draw(); });
  $('#optArrows').addEventListener('change', (e) => { gv.opts.arrows = e.target.checked; gv.draw(); });
  $('#optColorLang').addEventListener('change', (e) => { state.colorLang = e.target.checked; gv.draw(); legend(); });

  // ------------------------------------------------------------ chrome
  document.querySelectorAll('#views button').forEach((b) => b.addEventListener('click', () => go({ view: b.dataset.view })));
  $('#openPalette').addEventListener('click', () => openPalette());
  $('#zoomIn').addEventListener('click', () => gv.zoomBy(1.3));
  $('#zoomOut').addEventListener('click', () => gv.zoomBy(1 / 1.3));
  $('#fit').addEventListener('click', () => gv.fit());
  $('#pause').addEventListener('click', () => { const r = gv.togglePause(); $('#pause').textContent = r ? '❚❚' : '▶'; });
  $('#png').addEventListener('click', () => gv.toPNG());
  $('#back').addEventListener('click', () => histStep(-1));
  $('#fwd').addEventListener('click', () => histStep(1));
  $('#help').addEventListener('click', () => { $('#helpDlg').hidden = false; });
  function histStep(d) {
    const i = state.hpos + d;
    if (i < 0 || i >= state.history.length) return;
    state.hpos = i; Object.assign(state, state.history[i]); writeHash(); render();
  }
  function setTheme(t) {
    document.documentElement.dataset.theme = t;
    try { localStorage.setItem('ripplet-theme', t); } catch (e) { /* storage off */ }
    gv.draw();
  }
  $('#theme').addEventListener('click', () => setTheme(document.documentElement.dataset.theme === 'light' ? 'dark' : 'light'));

  document.addEventListener('keydown', (e) => {
    const typing = /INPUT|SELECT|TEXTAREA/.test(document.activeElement.tagName);
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') { e.preventDefault(); openPalette(); return; }
    if (e.key === 'Escape') {
      const dlg = !$('#palette').hidden || !$('#helpDlg').hidden;
      closePalette(); $('#helpDlg').hidden = true; $('#ctx').style.display = 'none';
      if (!dlg) closeInspector();
      return;
    }
    if (typing) return;
    if (e.altKey && e.key === 'ArrowLeft') { histStep(-1); return; }
    if (e.altKey && e.key === 'ArrowRight') { histStep(1); return; }
    if (e.key === '/') { e.preventDefault(); openPalette(); }
    else if (e.key >= '1' && e.key <= '9') go({ view: VIEWS[+e.key - 1] });
    else if ((e.key === 'l' || e.key === 'L') && (state.view === 'explore' || state.view === 'impact')) { state.listMode = !state.listMode; render(); }
    else if (e.key === 'f' || e.key === 'F') gv.fit();
    else if (e.key === ' ') { e.preventDefault(); $('#pause').click(); }
    else if (e.key === '+' || e.key === '=') gv.zoomBy(1.3);
    else if (e.key === '-') gv.zoomBy(1 / 1.3);
    else if (e.key === '?') $('#helpDlg').hidden = false;
    else if ((e.key === 'i' || e.key === 'I') && gv.selected && !gv.selected.startsWith('dir:')) go({ view: 'impact', focus: gv.selected });
    else if ((e.key === 'e' || e.key === 'E') && gv.selected && !gv.selected.startsWith('dir:')) go({ view: 'explore', focus: gv.selected });
  });
  window.addEventListener('hashchange', () => { const h = readHash(); if (h.view !== state.view || h.focus !== state.focus) go(h); });

  // ------------------------------------------------------ panels
  const insp = $('#inspector');
  new MutationObserver(() => {
    const open = !!insp.querySelector('.insp-head');
    insp.classList.toggle('open', open);
    $('#inspClose').classList.toggle('show', open);
  }).observe(insp, { childList: true });
  function closeInspector() { insp.innerHTML = ''; gv.select(null); }
  $('#inspClose').addEventListener('click', closeInspector);
  function setSidebar(open) {
    $('#sidebar').classList.toggle('hidden', !open);
    $('#toggleSidebar').classList.toggle('on', open);
    try { localStorage.setItem('ripplet-sidebar', open ? '1' : '0'); } catch (e) { /* storage off */ }
  }
  $('#toggleSidebar').addEventListener('click', () => setSidebar($('#sidebar').classList.contains('hidden')));
  try { setSidebar(localStorage.getItem('ripplet-sidebar') === '1'); } catch (e) { setSidebar(false); }

  // -------------------------------------------------------------- boot
  (async function boot() {
    let theme = 'dark';
    try { theme = localStorage.getItem('ripplet-theme') || (matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark'); } catch (e) { /* default */ }
    document.documentElement.dataset.theme = theme;
    state.meta = await api('meta');
    const m = state.meta;
    $('#project').textContent = m.project;
    document.title = `${m.project} · Ripplet`;
    filters();
    loadTree();
    const symbols = Object.entries(m.types).filter(([t]) => m.symbol_types.includes(t)).reduce((a, [, n]) => a + n, 0);
    const langs = Object.entries(m.languages).sort((a, b) => b[1] - a[1]).slice(0, 4).map(([l]) => l).join(', ');
    $('#status').dataset.base = `${symbols} symbols · ${m.edges} edges · ${langs}`;
    const h = readHash();
    go(h);
  })();
})();
