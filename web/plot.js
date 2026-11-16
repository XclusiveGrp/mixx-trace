/* SPDX-License-Identifier: MIT
   Copyright (c) 2026 Xclusive Audio Visual Group (XAVGROUP) */
/* Canvas plot on a log-frequency axis. No dependencies. */
(function () {
  'use strict';
  const F_MIN = 20, F_MAX = 20000;
  const F_LINES = [20, 30, 40, 50, 60, 70, 80, 90, 100, 200, 300, 400, 500, 600, 700, 800, 900, 1000,
    2000, 3000, 4000, 5000, 6000, 7000, 8000, 9000, 10000, 20000];
  const F_LABELS = { 20: '20', 50: '50', 100: '100', 200: '200', 500: '500', 1000: '1k', 2000: '2k', 5000: '5k', 10000: '10k', 20000: '20k' };

  function fmtHz(f) {
    if (f >= 10000) return (f / 1000).toFixed(1) + ' kHz';
    if (f >= 1000) return (f / 1000).toFixed(2) + ' kHz';
    if (f >= 100) return f.toFixed(0) + ' Hz';
    return f.toFixed(1) + ' Hz';
  }

  class Plot {
    constructor(canvas, opt) {
      this.c = canvas;
      this.ctx = canvas.getContext('2d');
      this.o = Object.assign({ yMin: -90, yMax: 0, yStep: 10, unit: 'dB', xLabels: true, wrap: false, decimals: 1 }, opt);
      this.series = [];
      this.markers = [];
      this.grid = null;
      this.hover = null;
      this.w = 0; this.h = 0;
      this.colors = {};
      this.readColors();
      const ro = new ResizeObserver(() => this.resize());
      ro.observe(canvas.parentElement);
      canvas.addEventListener('pointermove', (e) => { const r = canvas.getBoundingClientRect(); this.hover = { x: e.clientX - r.left, y: e.clientY - r.top }; this.draw(); });
      canvas.addEventListener('pointerleave', () => { this.hover = null; this.draw(); });
      this.resize();
    }
    readColors() {
      const cs = getComputedStyle(document.documentElement);
      const g = (n) => cs.getPropertyValue(n).trim();
      this.colors = { grid: g('--plot-grid'), strong: g('--plot-grid-strong'), text: g('--color-muted'), fg: g('--color-text'), bg: g('--plot-bg'), box: g('--color-surface-3') };
      this.font = (cs.getPropertyValue('--font-mono') || 'monospace').trim();
    }
    resize() {
      const r = this.c.parentElement.getBoundingClientRect();
      const dpr = Math.min(window.devicePixelRatio || 1, 2.5);
      if (r.width < 10 || r.height < 10) return;
      this.w = r.width; this.h = r.height;
      this.c.width = Math.round(r.width * dpr);
      this.c.height = Math.round(r.height * dpr);
      this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      this.draw();
    }
    get pad() { return { l: this.w < 520 ? 34 : 44, r: 10, t: 8, b: this.o.xLabels ? 22 : 6 }; }
    x(f) { const p = this.pad; return p.l + (Math.log(f / F_MIN) / Math.log(F_MAX / F_MIN)) * (this.w - p.l - p.r); }
    fAt(x) { const p = this.pad; return F_MIN * Math.pow(F_MAX / F_MIN, (x - p.l) / (this.w - p.l - p.r)); }
    y(v) { const p = this.pad; return p.t + (this.o.yMax - v) / (this.o.yMax - this.o.yMin) * (this.h - p.t - p.b); }
    set(grid, series, markers) { this.grid = grid; this.series = series || []; this.markers = markers || []; this.draw(); }
    setRange(yMin, yMax, yStep) { this.o.yMin = yMin; this.o.yMax = yMax; if (yStep) this.o.yStep = yStep; }

    draw() {
      const { ctx, w, h, o } = this;
      if (!w) return;
      const p = this.pad, col = this.colors;
      ctx.clearRect(0, 0, w, h);
      ctx.font = '11px ' + this.font;
      ctx.lineWidth = 1;
      // frequency grid
      for (const f of F_LINES) {
        const x = Math.round(this.x(f)) + 0.5;
        ctx.strokeStyle = F_LABELS[f] ? col.strong : col.grid;
        ctx.beginPath(); ctx.moveTo(x, p.t); ctx.lineTo(x, h - p.b); ctx.stroke();
        if (o.xLabels && F_LABELS[f] && !(w < 520 && (f === 50 || f === 500 || f === 5000))) {
          ctx.fillStyle = col.text; ctx.textAlign = f === 20000 ? 'right' : 'center'; ctx.textBaseline = 'top';
          ctx.fillText(F_LABELS[f], f === 20000 ? x + 4 : x, h - p.b + 6);
        }
      }
      // value grid
      ctx.textAlign = 'right'; ctx.textBaseline = 'middle';
      const first = Math.ceil(o.yMin / o.yStep) * o.yStep;
      for (let v = first; v <= o.yMax + 1e-9; v += o.yStep) {
        const y = Math.round(this.y(v)) + 0.5;
        ctx.strokeStyle = (o.zero !== undefined && Math.abs(v - o.zero) < 1e-9) ? col.strong : col.grid;
        ctx.beginPath(); ctx.moveTo(p.l, y); ctx.lineTo(w - p.r, y); ctx.stroke();
        if (y > p.t + 5 && y < h - p.b - 3 || o.edgeLabels) { ctx.fillStyle = col.text; ctx.fillText(o.fmt ? o.fmt(v) : String(Math.round(v)), p.l - 6, y); }
      }
      if (!this.grid) return;
      ctx.save();
      ctx.beginPath(); ctx.rect(p.l, p.t, w - p.l - p.r, h - p.t - p.b); ctx.clip();
      // markers under the traces
      for (const m of this.markers) {
        const x = Math.round(this.x(m.f)) + 0.5;
        ctx.strokeStyle = m.color; ctx.globalAlpha = m.alpha || 1; ctx.lineWidth = m.width || 1.5;
        ctx.setLineDash(m.dash || []);
        ctx.beginPath(); ctx.moveTo(x, p.t); ctx.lineTo(x, h - p.b); ctx.stroke();
        ctx.setLineDash([]); ctx.globalAlpha = 1;
      }
      for (const s of this.series) this.drawSeries(s);
      ctx.restore();
      // marker labels
      let lastX = -999;
      ctx.textBaseline = 'top'; ctx.font = '600 11px ' + this.font;
      for (const m of this.markers.filter((k) => k.label).sort((a, b) => a.f - b.f)) {
        const x = this.x(m.f), tw = ctx.measureText(m.label).width + 10;
        if (x - lastX < tw + 4) continue;
        lastX = x;
        const bx = Math.min(Math.max(x - tw / 2, p.l + 2), w - p.r - tw - 2);
        if (m.alpha && m.alpha < 1) {             // logged, not sounding now: outlined label
          ctx.fillStyle = col.bg; ctx.fillRect(bx, p.t + 3, tw, 17);
          ctx.strokeStyle = m.color; ctx.lineWidth = 1; ctx.strokeRect(bx + 0.5, p.t + 3.5, tw - 1, 16);
          ctx.fillStyle = m.color;
        } else {
          ctx.fillStyle = m.color; ctx.fillRect(bx, p.t + 3, tw, 17);
          ctx.fillStyle = '#14161a';
        }
        ctx.textAlign = 'center';
        ctx.fillText(m.label, bx + tw / 2, p.t + 6);
      }
      this.drawHover();
    }

    drawSeries(s) {
      const { ctx } = this, g = this.grid, d = s.data;
      if (!d) return;
      const off = s.offset || 0;
      ctx.lineJoin = 'round'; ctx.lineWidth = s.width || 1.5; ctx.strokeStyle = s.color;
      ctx.setLineDash(s.dash || []);
      const base = this.y(this.o.yMin);
      if (s.fill) {
        ctx.globalAlpha = s.fillAlpha || 0.14; ctx.fillStyle = s.color;
        let open = false, x0 = 0, xl = 0;
        ctx.beginPath();
        for (let i = 0; i < g.length; i++) {
          const v = d[i];
          if (v === null || v === undefined) { if (open) { ctx.lineTo(xl, base); ctx.lineTo(x0, base); ctx.closePath(); open = false; } continue; }
          const x = this.x(g[i]), y = this.y(v + off);
          if (!open) { ctx.moveTo(x, y); x0 = x; open = true; } else ctx.lineTo(x, y);
          xl = x;
        }
        if (open) { ctx.lineTo(xl, base); ctx.lineTo(x0, base); ctx.closePath(); }
        ctx.fill(); ctx.globalAlpha = 1;
        if (s.fillOnly) { ctx.setLineDash([]); return; }
      }
      // two passes when a mask dims unreliable parts
      const passes = s.mask ? [[true, s.alpha || 1], [false, 0.22]] : [[null, s.alpha || 1]];
      for (const [want, alpha] of passes) {
        ctx.globalAlpha = alpha;
        ctx.beginPath();
        let pen = false, prev = null;
        for (let i = 0; i < g.length; i++) {
          const v = d[i];
          let ok = v !== null && v !== undefined;
          if (ok && want !== null) {
            const m = s.mask[i] !== null && s.mask[i] >= (s.maskMin || 0.3);
            ok = want ? m : (!m || (i > 0 && !(s.mask[i - 1] >= (s.maskMin || 0.3))) || (i < g.length - 1 && !(s.mask[i + 1] >= (s.maskMin || 0.3))));
          }
          if (ok && this.o.wrap && prev !== null && Math.abs(v - prev) > 180) pen = false;
          if (!ok) { pen = false; prev = null; continue; }
          const x = this.x(g[i]), y = this.y(v + off);
          if (!pen) { ctx.moveTo(x, y); pen = true; } else ctx.lineTo(x, y);
          prev = v;
        }
        ctx.stroke();
      }
      ctx.globalAlpha = 1; ctx.setLineDash([]);
    }

    drawHover() {
      if (!this.hover || !this.grid) return;
      const { ctx, w, h } = this, p = this.pad, col = this.colors;
      const hx = this.hover.x;
      if (hx < p.l || hx > w - p.r) return;
      const f = this.fAt(hx), g = this.grid;
      let i = Math.round(Math.log2(f / g[0]) * (g.length - 1) / Math.log2(g[g.length - 1] / g[0]));
      i = Math.max(0, Math.min(g.length - 1, i));
      const x = Math.round(this.x(g[i])) + 0.5;
      ctx.strokeStyle = col.fg; ctx.globalAlpha = 0.5; ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(x, p.t); ctx.lineTo(x, h - p.b); ctx.stroke(); ctx.globalAlpha = 1;
      const rows = [[fmtHz(g[i]), col.fg]];
      for (const s of this.series) {
        if (!s.name || !s.data || s.data[i] === null || s.data[i] === undefined) continue;
        const v = s.data[i] + (s.offset || 0);
        rows.push([s.name + '  ' + (this.o.fmtVal ? this.o.fmtVal(v) : v.toFixed(this.o.decimals) + ' ' + this.o.unit), s.color]);
        ctx.fillStyle = s.color; ctx.beginPath(); ctx.arc(x, this.y(v), 3, 0, 6.3); ctx.fill();
      }
      ctx.font = '11px ' + this.font;
      const bw = Math.max(...rows.map((r) => ctx.measureText(r[0]).width)) + 16, bh = rows.length * 15 + 10;
      if (bh > h - 4) rows.length = Math.max(1, Math.floor((h - 14) / 15));
      const bh2 = rows.length * 15 + 10;
      const bx = x + 10 + bw > w - p.r ? x - 10 - bw : x + 10;
      const by = Math.max(2, Math.min(h - bh2 - 2, p.t + 26));
      ctx.fillStyle = col.box; ctx.globalAlpha = 0.96; ctx.fillRect(bx, by, bw, bh2); ctx.globalAlpha = 1;
      ctx.textAlign = 'left'; ctx.textBaseline = 'top';
      rows.forEach((r, k) => { ctx.fillStyle = r[1]; ctx.fillText(r[0], bx + 8, by + 6 + k * 15); });
    }
  }
  window.TTPlot = Plot;
  window.fmtHz = fmtHz;
})();
