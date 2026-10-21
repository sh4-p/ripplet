/* Ripplet canvas graph engine: force / radial / layered / bipartite
   layouts, Barnes-Hut repulsion, zoom & pan, hover focus, minimap. No deps. */
(function () {
  'use strict';

  const REL_COLORS = {
    calls: '#8b9bc4', instantiates: '#5fd4a8', calls_http: '#e879d6', inherits: '#f5b971',
    implements: '#c79bff', imports: '#5d6884', references: '#6fb3e0', depends_on: '#8790a3',
    tests: '#a6dc7f', uses: '#6fb3e0',
  };
  const DASHED = { ambiguous: [5, 4], inferred: [2, 3], hypothetical: [2, 3], heuristic: [2, 3] };

  class Quad {
    constructor(x, y, w) { this.x = x; this.y = y; this.w = w; this.m = 0; this.cx = 0; this.cy = 0; this.n = null; this.k = null; }
    insert(p, depth) {
      if (this.m === 0 && !this.k && !this.n) { this.n = p; this.m = 1; this.cx = p.x; this.cy = p.y; return; }
      if (!this.k) {
        if (depth > 18) { this.cx = (this.cx * this.m + p.x) / (this.m + 1); this.cy = (this.cy * this.m + p.y) / (this.m + 1); this.m++; return; }
        const h = this.w / 2;
        this.k = [new Quad(this.x, this.y, h), new Quad(this.x + h, this.y, h), new Quad(this.x, this.y + h, h), new Quad(this.x + h, this.y + h, h)];
        const old = this.n; this.n = null; this.m = 0;
        if (old) this._child(old).insert(old, depth + 1);
        this.m = old ? 1 : 0; this.cx = old ? old.x : 0; this.cy = old ? old.y : 0;
      }
      this._child(p).insert(p, depth + 1);
      this.cx = (this.cx * this.m + p.x) / (this.m + 1); this.cy = (this.cy * this.m + p.y) / (this.m + 1); this.m++;
    }
    _child(p) { const h = this.w / 2; return this.k[(p.x >= this.x + h ? 1 : 0) + (p.y >= this.y + h ? 2 : 0)]; }
    force(p, theta2, strength) {
      if (this.m === 0 || this.n === p && !this.k) return;
      const dx = this.cx - p.x, dy = this.cy - p.y;
      let d2 = dx * dx + dy * dy;
      if (!this.k || (this.w * this.w) / (d2 + 1e-9) < theta2) {
        if (d2 < 1) d2 = 1;
        const f = strength * this.m / d2;
        p.vx -= dx * f; p.vy -= dy * f;
        return;
      }
      for (const c of this.k) c.force(p, theta2, strength);
    }
  }

  class GraphView {
    constructor(canvas, minimap) {
      this.cv = canvas; this.ctx = canvas.getContext('2d');
      this.mm = minimap; this.mctx = minimap ? minimap.getContext('2d') : null;
      this.nodes = []; this.edges = []; this.byId = new Map(); this.adj = new Map();
      this.t = { x: 0, y: 0, k: 1 };
      this.alpha = 0; this.running = true; this.layout = 'force';
      this.hover = null; this.selected = null; this.focusSet = null; this.mark = new Map();
      this.opts = { labels: true, arrows: true };
      this.style = { color: () => '#6c8cff', radius: () => 6, visible: () => true, edgeVisible: () => true };
      this.handlers = {};
      this._bind();
      this._resize();
      new ResizeObserver(() => { this._resize(); this.draw(); }).observe(canvas.parentElement);
      const loop = () => { this._tick(); requestAnimationFrame(loop); };
      requestAnimationFrame(loop);
    }

    on(ev, fn) { this.handlers[ev] = fn; return this; }
    emit(ev, ...a) { const h = this.handlers[ev]; if (h) h(...a); }

    _resize() {
      const r = this.cv.parentElement.getBoundingClientRect();
      const dpr = window.devicePixelRatio || 1;
      this.w = r.width; this.h = r.height; this.dpr = dpr;
      this.cv.width = Math.max(1, r.width * dpr); this.cv.height = Math.max(1, r.height * dpr);
      if (this.mm) { const m = this.mm.getBoundingClientRect(); this.mm.width = m.width * dpr; this.mm.height = m.height * dpr; }
    }

    // --------------------------------------------------------------- data
    setData(nodes, edges, cfg = {}) {
      const prev = cfg.keepPositions ? this.byId : new Map();
      this.layout = cfg.layout || 'force';
      this.center = cfg.center || null;
      this.nodes = nodes.map((d) => {
        const old = prev.get(d.id);
        return { id: d.id, d, x: old ? old.x : (Math.random() - 0.5) * 400, y: old ? old.y : (Math.random() - 0.5) * 400,
          vx: 0, vy: 0, fixed: old ? old.fixed : false, r: 6 };
      });
      this._index(edges);
      this._initLayout(cfg);
      this.selected = cfg.select && this.byId.has(cfg.select) ? cfg.select : (this.selected && this.byId.has(this.selected) ? this.selected : null);
      this.hover = null; this.focusSet = null;
      this.alpha = 1; this.running = true;
      this._settle(cfg.layout === 'force' ? 80 : 40);
      this.fit(true);
      this._autoFit = true;   // once more when the layout has cooled
    }

    merge(nodes, edges, anchor) {
      const a = this.byId.get(anchor);
      for (const d of nodes) {
        if (this.byId.has(d.id)) { this.byId.get(d.id).d = Object.assign({}, this.byId.get(d.id).d, d); continue; }
        const ang = Math.random() * Math.PI * 2;
        const n = { id: d.id, d, x: (a ? a.x : 0) + Math.cos(ang) * 60, y: (a ? a.y : 0) + Math.sin(ang) * 60, vx: 0, vy: 0, fixed: false, r: 6 };
        this.nodes.push(n);
      }
      const all = this.edges.map((e) => e.d).concat(edges);
      const seen = new Set();
      this._index(all.filter((e) => { const k = e.s + '|' + e.t + '|' + e.rel; if (seen.has(k)) return false; seen.add(k); return true; }));
      this.layout = 'force';
      this.alpha = Math.max(this.alpha, 0.6); this.running = true;
    }

    _index(edges) {
      this.byId = new Map(this.nodes.map((n) => [n.id, n]));
      this.edges = [];
      this.adj = new Map(this.nodes.map((n) => [n.id, new Set()]));
      for (const e of edges) {
        const s = this.byId.get(e.s), t = this.byId.get(e.t);
        if (!s || !t || s === t) continue;
        this.edges.push({ s, t, d: e });
        this.adj.get(s.id).add(t.id); this.adj.get(t.id).add(s.id);
      }
      for (const n of this.nodes) n.r = this.style.radius(n.d, this.adj.get(n.id).size);
    }

    restyle() {
      for (const n of this.nodes) n.r = this.style.radius(n.d, (this.adj.get(n.id) || new Set()).size);
      this.draw();
    }

    _initLayout(cfg) {
      const N = this.nodes.length;
      if (this.layout === 'radial') {
        const rings = new Map();
        for (const n of this.nodes) { const h = n.d.hop || 0; if (!rings.has(h)) rings.set(h, []); rings.get(h).push(n); }
        for (const [h, list] of rings) {
          list.forEach((n, i) => {
            const a = (i / list.length) * Math.PI * 2 + h * 0.5;
            const R = h * 170;
            n.x = Math.cos(a) * R; n.y = Math.sin(a) * R;
          });
        }
      } else if (this.layout === 'layered') {
        const order = cfg.order || this.nodes.map((n) => n.id);
        order.forEach((id, i) => { const n = this.byId.get(id); if (n) { n.x = i * 190 - (order.length - 1) * 95; n.y = (i % 2) * 40 - 20; } });
      } else if (this.layout === 'bipartite') {
        const left = this.nodes.filter((n) => cfg.side(n.d) === 0), right = this.nodes.filter((n) => cfg.side(n.d) !== 0);
        const place = (list, x) => list.forEach((n, i) => { n.x = x; n.y = (i - (list.length - 1) / 2) * 34; });
        place(left, -360); place(right, 360);
        this.bipartiteSide = cfg.side;
      } else if (!cfg.keepPositions) {
        // phyllotaxis start: fast, even convergence
        this.nodes.forEach((n, i) => { const r = 12 * Math.sqrt(i + 0.5), a = i * 2.39996; n.x = r * Math.cos(a); n.y = r * Math.sin(a); });
      }
      if (this.center && this.byId.has(this.center)) { const c = this.byId.get(this.center); c.x = 0; c.y = 0; }
      this.repulsion = this.layout === 'bipartite' ? 220 : N > 1500 ? 900 : N > 600 ? 1300 : 1800;
    }

    _settle(n) { for (let i = 0; i < n; i++) this._step(); }

    _step() {
      const N = this.nodes.length;
      if (!N) return;
      const a = this.alpha;
      let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
      for (const n of this.nodes) { if (n.x < minX) minX = n.x; if (n.y < minY) minY = n.y; if (n.x > maxX) maxX = n.x; if (n.y > maxY) maxY = n.y; }
      const w = Math.max(maxX - minX, maxY - minY) + 2;
      const q = new Quad(minX - 1, minY - 1, w + 2);
      for (const n of this.nodes) q.insert(n, 0);
      const strength = this.repulsion * a;
      for (const n of this.nodes) q.force(n, 0.81, strength);
      const L = this.layout === 'layered' ? 150 : N > 800 ? 45 : 70;
      for (const e of this.edges) {
        const s = e.s, t = e.t;
        let dx = t.x - s.x, dy = t.y - s.y;
        const d = Math.sqrt(dx * dx + dy * dy) || 1;
        const len = L + s.r + t.r;
        const k = 0.06 * a * (d - len) / d / Math.max(1, Math.min(this.adj.get(s.id).size, this.adj.get(t.id).size) * 0.5);
        dx *= k; dy *= k;
        t.vx -= dx; t.vy -= dy; s.vx += dx; s.vy += dy;
      }
      for (const n of this.nodes) {
        if (this.layout === 'radial') {
          const R = (n.d.hop || 0) * 170, d = Math.sqrt(n.x * n.x + n.y * n.y) || 1;
          const k = 0.45 * a * (d - R) / d; n.vx -= n.x * k; n.vy -= n.y * k;
        } else if (this.layout === 'bipartite') {
          const X = this.bipartiteSide(n.d) === 0 ? -360 : 360; n.vx += (X - n.x) * 0.3 * a; n.vy -= n.y * 0.002 * a;
        } else if (this.layout === 'layered') {
          n.vy -= n.y * 0.02 * a;
        } else {
          // isolated nodes would drift to the edge and shrink the whole fit
          const g = this.adj.get(n.id).size ? 0.008 : 0.05;
          n.vx -= n.x * g * a; n.vy -= n.y * g * a;
        }
      }
      for (const n of this.nodes) {
        if (n.fixed || n === this.dragNode) { n.vx = n.vy = 0; continue; }
        if (this.layout === 'layered') n.vx = 0;
        n.vx *= 0.55; n.vy *= 0.55;
        const sp = Math.sqrt(n.vx * n.vx + n.vy * n.vy);
        if (sp > 40) { n.vx *= 40 / sp; n.vy *= 40 / sp; }
        n.x += n.vx; n.y += n.vy;
        if (this.layout === 'bipartite') n.x = this.bipartiteSide(n.d) === 0 ? -360 : 360;  // columns are fixed
      }
      this.alpha *= 0.985;
    }

    _tick() {
      if (this.running && this.alpha > 0.004) {
        const steps = this.nodes.length > 1200 ? 1 : 2;
        for (let i = 0; i < steps; i++) this._step();
        if (this._autoFit && this.alpha < 0.12) { this._autoFit = false; this.fit(); }
        this.draw();
      } else if (this._dirty) {
        this.draw();
      }
      this._dirty = false;
    }

    reheat(x = 0.5) { this.alpha = Math.max(this.alpha, x); this.running = true; }
    togglePause() { this.running = !this.running; if (this.running) this.reheat(0.3); return this.running; }
    unpinAll() { for (const n of this.nodes) n.fixed = false; this.reheat(0.4); }

    // ------------------------------------------------------------- view
    fit(instant) {
      const vis = this.nodes.filter((n) => this.style.visible(n.d));
      if (!vis.length) { this.t = { x: this.w / 2, y: this.h / 2, k: 1 }; this.draw(); return; }
      let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
      for (const n of vis) { minX = Math.min(minX, n.x - n.r); minY = Math.min(minY, n.y - n.r); maxX = Math.max(maxX, n.x + n.r); maxY = Math.max(maxY, n.y + n.r); }
      const pad = 60;
      const k = Math.min(2.2, Math.max(0.05, Math.min((this.w - pad * 2) / (maxX - minX + 1), (this.h - pad * 2) / (maxY - minY + 1))));
      const target = { k, x: this.w / 2 - k * (minX + maxX) / 2, y: this.h / 2 - k * (minY + maxY) / 2 };
      this._animateTo(target, instant ? 0 : 350);
    }

    centerOn(id, k) {
      const n = this.byId.get(id);
      if (!n) return;
      const kk = k || Math.max(this.t.k, 1);
      this._animateTo({ k: kk, x: this.w / 2 - kk * n.x, y: this.h / 2 - kk * n.y }, 350);
    }

    _animateTo(target, ms) {
      if (!ms) { this.t = target; this.draw(); return; }
      const from = Object.assign({}, this.t), t0 = performance.now();
      const step = (now) => {
        const p = Math.min(1, (now - t0) / ms), e = 1 - Math.pow(1 - p, 3);
        this.t = { k: from.k + (target.k - from.k) * e, x: from.x + (target.x - from.x) * e, y: from.y + (target.y - from.y) * e };
        this.draw();
        if (p < 1) requestAnimationFrame(step);
      };
      requestAnimationFrame(step);
    }

    zoomBy(f, cx, cy) {
      cx = cx == null ? this.w / 2 : cx; cy = cy == null ? this.h / 2 : cy;
      const k = Math.min(8, Math.max(0.03, this.t.k * f));
      this.t.x = cx - (cx - this.t.x) * (k / this.t.k);
      this.t.y = cy - (cy - this.t.y) * (k / this.t.k);
      this.t.k = k; this.draw();
    }

    toWorld(px, py) { return { x: (px - this.t.x) / this.t.k, y: (py - this.t.y) / this.t.k }; }

    nodeAt(px, py) {
      const p = this.toWorld(px, py);
      let best = null, bd = Infinity;
      for (const n of this.nodes) {
        if (!this.style.visible(n.d)) continue;
        const dx = n.x - p.x, dy = n.y - p.y, d = dx * dx + dy * dy, rr = (n.r + 4 / this.t.k) ** 2;
        if (d < rr && d < bd) { best = n; bd = d; }
      }
      return best;
    }

    select(id) { this.selected = id; this._dirty = true; }
    setMarks(map) { this.mark = map || new Map(); this._dirty = true; }

    // ---------------------------------------------------------- render
    draw() {
      const c = this.ctx, dpr = this.dpr, t = this.t;
      const css = getComputedStyle(document.documentElement);
      const bg = css.getPropertyValue('--bg').trim(), fg = css.getPropertyValue('--fg').trim(), fg2 = css.getPropertyValue('--fg2').trim();
      const grid = css.getPropertyValue('--bg-grid').trim(), accent = css.getPropertyValue('--accent').trim();
      const light = document.documentElement.dataset.theme === 'light';
      c.setTransform(1, 0, 0, 1, 0, 0);
      c.fillStyle = bg; c.fillRect(0, 0, this.cv.width, this.cv.height);
      // dot grid in screen space, following pan/zoom
      const step = 26 * t.k >= 12 ? 26 * t.k : 26 * t.k * Math.ceil(12 / (26 * t.k));
      c.fillStyle = grid;
      const ox = ((t.x % step) + step) % step, oy = ((t.y % step) + step) % step;
      for (let x = ox; x < this.w; x += step) for (let y = oy; y < this.h; y += step) c.fillRect(x * dpr, y * dpr, 1.4 * dpr, 1.4 * dpr);
      c.setTransform(dpr * t.k, 0, 0, dpr * t.k, dpr * t.x, dpr * t.y);
      const focus = this.hover ? this.hover.id : null;
      const near = focus ? this.adj.get(focus) : null;
      const sel = this.selected, selNear = sel ? this.adj.get(sel) : null;
      const vis = (n) => this.style.visible(n.d);
      const arrows = this.opts.arrows && t.k > 0.35;
      const many = this.nodes.length > 600;
      // edges: gentle curves, colour by relation
      for (const e of this.edges) {
        if (!vis(e.s) || !vis(e.t) || !this.style.edgeVisible(e.d)) continue;
        const on = focus ? (e.s.id === focus || e.t.id === focus) : sel ? (e.s.id === sel || e.t.id === sel) : false;
        const dim = (focus && !on) || (!focus && sel && !on && selNear);
        const col = REL_COLORS[e.d.rel] || '#8a94a8';
        c.globalAlpha = dim ? (focus ? 0.05 : 0.18) : on ? 0.95 : (many ? 0.22 : 0.42);
        c.strokeStyle = col;
        c.lineWidth = (on ? 2.2 : Math.min(4, 1 + Math.log2(1 + (e.d.w || 1)) * 0.7)) / t.k;
        c.setLineDash((DASHED[e.d.conf] || []).map((v) => v / t.k));
        const dx = e.t.x - e.s.x, dy = e.t.y - e.s.y, d = Math.sqrt(dx * dx + dy * dy) || 1;
        const bend = this.layout === 'bipartite' || this.layout === 'layered' ? 0.08 : 0.16;
        const mx = (e.s.x + e.t.x) / 2 - dy * bend, my = (e.s.y + e.t.y) / 2 + dx * bend;
        // end point on the target circle, along the curve's last tangent
        const tx = e.t.x - mx, ty = e.t.y - my, tl = Math.sqrt(tx * tx + ty * ty) || 1;
        const ex = e.t.x - tx / tl * (e.t.r + 2.5 / t.k), ey = e.t.y - ty / tl * (e.t.r + 2.5 / t.k);
        c.beginPath(); c.moveTo(e.s.x, e.s.y); c.quadraticCurveTo(mx, my, ex, ey); c.stroke();
        if (arrows && (on || (!focus && !many))) {
          const ah = Math.min(10, 5 + c.lineWidth * t.k * 1.6) / t.k, ang = Math.atan2(ty, tx);
          c.setLineDash([]); c.fillStyle = col;
          c.beginPath(); c.moveTo(ex, ey);
          c.lineTo(ex - ah * Math.cos(ang - 0.4), ey - ah * Math.sin(ang - 0.4));
          c.lineTo(ex - ah * Math.cos(ang + 0.4), ey - ah * Math.sin(ang + 0.4));
          c.closePath(); c.fill();
        }
      }
      c.setLineDash([]); c.globalAlpha = 1;
      // nodes: glow for focus/selection, ring in background colour to separate
      for (const n of this.nodes) {
        if (!vis(n)) continue;
        const dim = (focus && n.id !== focus && !(near && near.has(n.id)));
        const hot = n.id === focus || n.id === sel;
        const col = this.style.color(n.d);
        c.globalAlpha = dim ? 0.14 : 1;
        if (hot || this.mark.has(n.id)) {
          c.shadowColor = this.mark.get(n.id) || col; c.shadowBlur = 18 * dpr;
        }
        c.fillStyle = col;
        c.beginPath(); c.arc(n.x, n.y, n.r, 0, Math.PI * 2); c.fill();
        c.shadowBlur = 0;
        c.lineWidth = Math.max(1.2, n.r * 0.18) / Math.max(t.k, 0.6);
        c.strokeStyle = light ? 'rgba(255,255,255,.9)' : 'rgba(11,13,18,.85)';
        c.stroke();
        if (n.d.external) { c.lineWidth = 1.2 / t.k; c.strokeStyle = fg2; c.setLineDash([2 / t.k, 2 / t.k]); c.beginPath(); c.arc(n.x, n.y, n.r + 1.5 / t.k, 0, Math.PI * 2); c.stroke(); c.setLineDash([]); }
        const m = this.mark.get(n.id);
        if (m) { c.lineWidth = 2.5 / t.k; c.strokeStyle = m; c.beginPath(); c.arc(n.x, n.y, n.r + 4 / t.k, 0, Math.PI * 2); c.stroke(); }
        if (n.id === sel) { c.lineWidth = 2 / t.k; c.strokeStyle = accent; c.beginPath(); c.arc(n.x, n.y, n.r + 7 / t.k, 0, Math.PI * 2); c.stroke(); }
        if (n.fixed) { c.fillStyle = fg; c.beginPath(); c.arc(n.x + n.r * 0.72, n.y - n.r * 0.72, 2.2 / t.k, 0, Math.PI * 2); c.fill(); }
      }
      c.globalAlpha = 1;
      // labels: pills, greedy overlap avoidance in screen space
      if (this.opts.labels) {
        c.setTransform(dpr, 0, 0, dpr, 0, 0);
        c.textBaseline = 'middle';
        const placed = [];
        const cand = this.nodes.filter(vis).map((n) => {
          const imp = (n.id === focus || n.id === sel) ? 1e9 : (near && near.has(n.id)) ? 1e8 : (selNear && selNear.has(n.id)) ? 1e7 : n.r * 1000 + (this.adj.get(n.id).size);
          return { n, imp };
        }).sort((a, b) => b.imp - a.imp);
        const maxLabels = this.nodes.length > 800 ? 110 : 240;
        let count = 0;
        for (const { n, imp } of cand) {
          if (count >= maxLabels) break;
          const sx = n.x * t.k + t.x, sy = n.y * t.k + t.y;
          if (sx < -60 || sy < -20 || sx > this.w + 60 || sy > this.h + 20) continue;
          if (imp < 1e7 && n.r * t.k < 4.5 && this.nodes.length > 80) continue;
          if (focus && imp < 1e8) continue;
          const strong = imp >= 1e8;
          c.font = `${strong ? 600 : 500} ${strong ? 12.5 : 11.5}px Inter, "Segoe UI Variable", "Segoe UI", system-ui, sans-serif`;
          const txt = n.d.label || n.id;
          const tw = c.measureText(txt).width;
          const box = { x: sx + n.r * t.k + 6, y: sy - 10, w: tw + 12, h: 20 };
          if (!strong && placed.some((p) => box.x < p.x + p.w && box.x + box.w > p.x && box.y < p.y + p.h && box.y + box.h > p.y)) continue;
          placed.push(box); count++;
          c.globalAlpha = strong ? 0.95 : 0.78;
          c.fillStyle = light ? 'rgba(255,255,255,.92)' : 'rgba(17,20,28,.86)';
          const r = 7;
          c.beginPath();
          c.moveTo(box.x + r, box.y); c.arcTo(box.x + box.w, box.y, box.x + box.w, box.y + box.h, r);
          c.arcTo(box.x + box.w, box.y + box.h, box.x, box.y + box.h, r); c.arcTo(box.x, box.y + box.h, box.x, box.y, r);
          c.arcTo(box.x, box.y, box.x + box.w, box.y, r); c.closePath(); c.fill();
          if (strong) { c.strokeStyle = this.style.color(n.d); c.lineWidth = 1; c.globalAlpha = 0.6; c.stroke(); }
          c.globalAlpha = 1; c.fillStyle = strong ? fg : fg2;
          c.fillText(txt, box.x + 6, sy + 0.5);
        }
      }
      this._drawMinimap(bg, accent);
    }

    _drawMinimap(bg, accent) {
      if (!this.mctx || !this.nodes.length) return;
      const m = this.mctx, W = this.mm.width, H = this.mm.height;
      m.setTransform(1, 0, 0, 1, 0, 0); m.clearRect(0, 0, W, H);
      let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
      for (const n of this.nodes) { minX = Math.min(minX, n.x); minY = Math.min(minY, n.y); maxX = Math.max(maxX, n.x); maxY = Math.max(maxY, n.y); }
      const pad = 8 * this.dpr, k = Math.min((W - pad * 2) / (maxX - minX + 1), (H - pad * 2) / (maxY - minY + 1));
      const ox = pad - minX * k + ((W - pad * 2) - (maxX - minX) * k) / 2, oy = pad - minY * k + ((H - pad * 2) - (maxY - minY) * k) / 2;
      this.mmT = { k, ox, oy };
      for (const n of this.nodes) {
        if (!this.style.visible(n.d)) continue;
        m.fillStyle = this.style.color(n.d);
        m.fillRect(n.x * k + ox - 1, n.y * k + oy - 1, 2.5, 2.5);
      }
      const a = this.toWorld(0, 0), b = this.toWorld(this.w, this.h);
      m.strokeStyle = accent; m.lineWidth = 1.5;
      m.strokeRect(a.x * k + ox, a.y * k + oy, (b.x - a.x) * k, (b.y - a.y) * k);
    }

    toPNG() {
      const url = this.cv.toDataURL('image/png');
      const a = document.createElement('a'); a.href = url; a.download = 'ripplet.png'; a.click();
    }

    // ---------------------------------------------------------- events
    _bind() {
      const cv = this.cv;
      let down = null, moved = false, lastClick = 0;
      const pos = (e) => { const r = cv.getBoundingClientRect(); return { x: e.clientX - r.left, y: e.clientY - r.top }; };
      cv.addEventListener('mousedown', (e) => {
        if (e.button !== 0) return;
        const p = pos(e); moved = false;
        const n = this.nodeAt(p.x, p.y);
        down = { p, n, t: Object.assign({}, this.t) };
        if (n) { this.dragNode = n; } else cv.classList.add('dragging');
      });
      window.addEventListener('mousemove', (e) => {
        const p = pos(e);
        if (down) {
          const dx = p.x - down.p.x, dy = p.y - down.p.y;
          if (Math.abs(dx) + Math.abs(dy) > 3) moved = true;
          if (this.dragNode) {
            const w = this.toWorld(p.x, p.y); this.dragNode.x = w.x; this.dragNode.y = w.y; this.dragNode.fixed = true;
            this.reheat(0.25);
          } else { this.t.x = down.t.x + dx; this.t.y = down.t.y + dy; }
          this._dirty = true;
          return;
        }
        if (e.target !== cv) return;
        const n = this.nodeAt(p.x, p.y);
        if ((n && n.id) !== (this.hover && this.hover.id)) { this.hover = n; this._dirty = true; this.emit('hover', n ? n.d : null, p); }
        else if (n) this.emit('hover', n.d, p);
      });
      window.addEventListener('mouseup', (e) => {
        if (!down) return;
        cv.classList.remove('dragging');
        const p = pos(e), n = down.n;
        this.dragNode = null;
        if (!moved) {
          const now = performance.now();
          const dbl = now - lastClick < 320; lastClick = now;
          if (n) { if (dbl) this.emit('dblclick', n.d); else { this.selected = n.id; this.emit('select', n.d); } }
          else if (dbl) { this.unpinAll(); } else this.emit('background');
          this._dirty = true;
        }
        down = null;
      });
      cv.addEventListener('mouseleave', () => { if (this.hover) { this.hover = null; this._dirty = true; this.emit('hover', null); } });
      cv.addEventListener('wheel', (e) => {
        e.preventDefault(); const p = pos(e);
        this.zoomBy(Math.exp(-e.deltaY * 0.0015), p.x, p.y);
      }, { passive: false });
      cv.addEventListener('contextmenu', (e) => {
        e.preventDefault(); const p = pos(e); const n = this.nodeAt(p.x, p.y);
        if (n) { this.selected = n.id; this._dirty = true; this.emit('context', n.d, p); }
      });
      if (this.mm) {
        this.mm.addEventListener('click', (e) => {
          if (!this.mmT) return;
          const r = this.mm.getBoundingClientRect(), x = (e.clientX - r.left) * this.dpr, y = (e.clientY - r.top) * this.dpr;
          const wx = (x - this.mmT.ox) / this.mmT.k, wy = (y - this.mmT.oy) / this.mmT.k;
          this._animateTo({ k: this.t.k, x: this.w / 2 - this.t.k * wx, y: this.h / 2 - this.t.k * wy }, 250);
        });
      }
    }
  }

  window.RippletView = GraphView;
  window.RIPPLET_REL_COLORS = REL_COLORS;
})();
