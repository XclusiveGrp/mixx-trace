/* TuneTrace interface. Talks to the local engine over a small JSON API. */
(function () {
  'use strict';
  const raw = window.__API_BASE__ || '';
  const API = raw.indexOf('__PORT_') === 0 ? '' : raw.replace(/\/$/, '');
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const css = (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
  const MINUS = '\u2212';
  const sgn = (v, d) => (v < 0 ? MINUS : '+') + Math.abs(v).toFixed(d === undefined ? 1 : d);
  const fmtHz = window.fmtHz;

  const S = { view: 'feedback', frame: null, st: null, ver: -1, fail: 0, connected: true, devices: null,
    chFilter: 'all', rtaTop: -20, magMid: null, html: {}, chRows: 0, startedOnce: false };
  const C = {};
  function loadColors() {
    ['--trace-live', '--trace-best', '--trace-pred', '--trace-target', '--trace-coh', '--color-crit', '--color-warn', '--color-faint',
      '--trace-1', '--trace-2', '--trace-3', '--trace-4', '--trace-5', '--trace-6'].forEach((n) => { C[n] = css(n); });
  }
  loadColors();
  const traceColor = (i) => C['--trace-' + ((i % 6) + 1)];

  // ---------------------------------------------------------------- transport
  async function api(path) {
    const r = await fetch(API + path, { cache: 'no-store', credentials: 'same-origin' });
    if (!r.ok) throw new Error('HTTP ' + r.status);
    return r;
  }
  async function cmd(name, body) {
    try {
      const r = await fetch(API + '/api/cmd', { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(Object.assign({ cmd: name }, body || {})) });
      const j = await r.json();
      if (!j.ok) toast(j.msg || 'That did not work.', true);
      else if (j.msg) toast(j.msg);
      return j;
    } catch (e) {
      toast('Lost contact with the TuneTrace engine.', true);
      return { ok: false };
    }
  }
  let toastT = 0;
  function toast(msg, err) {
    const t = $('toast');
    t.textContent = msg; t.className = 'toast' + (err ? ' err' : ''); t.hidden = false;
    clearTimeout(toastT); toastT = setTimeout(() => { t.hidden = true; }, err ? 6000 : 3500);
  }
  function setHTML(id, html) {
    if (S.html[id] === html) return;
    S.html[id] = html; $(id).innerHTML = html;
  }

  // -------------------------------------------------------------------- plots
  const plots = {
    rta: new window.TTPlot($('plot-rta'), { yMin: -90, yMax: -20, yStep: 10, unit: 'dBFS' }),
    mag: new window.TTPlot($('plot-mag'), { yMin: -18, yMax: 18, yStep: 6, unit: 'dB', xLabels: false }),
    coh: new window.TTPlot($('plot-coh'), { yMin: 0, yMax: 1, yStep: 0.5, xLabels: false, edgeLabels: true,
      fmt: (v) => Math.round(v * 100) + '%', fmtVal: (v) => Math.round(v * 100) + '%' }),
    phase: new window.TTPlot($('plot-phase'), { yMin: -180, yMax: 180, yStep: 90, unit: '°', wrap: true, zero: 0, decimals: 0, edgeLabels: true }),
  };

  // --------------------------------------------------------------------- loop
  async function tick() {
    try {
      const fr = await (await api('/api/frame?view=' + S.view)).json();
      S.frame = fr; S.fail = 0;
      if (!S.connected) { S.connected = true; S.ver = -1; }
      if (fr.ver !== S.ver) {
        S.st = await (await api('/api/static')).json();
        S.ver = S.st.ver;
        renderStatic();
      }
      renderFrame();
    } catch (e) {
      S.fail++;
      if (S.fail >= 3 && S.connected) { S.connected = false; renderDisconnected(); }
    }
    setTimeout(tick, document.hidden ? 1000 : (S.connected ? 90 : 1500));
  }

  function renderDisconnected() {
    $('status-dot').className = 'dot err';
    $('status-text').textContent = 'Engine not reachable';
    const n = $('notice');
    n.className = 'notice err'; n.hidden = false;
    n.textContent = 'TuneTrace cannot reach its audio engine. If you closed the TuneTrace window in Terminal, start it again; this page reconnects by itself.';
    $('alert').hidden = true; $('meters').hidden = true;
  }

  // ------------------------------------------------------------------- header
  function meter(el, valEl, lv) {
    const pct = Math.max(0, Math.min(100, (lv.peak + 60) / 60 * 100));
    el.style.width = pct + '%';
    el.className = lv.clip ? 'clip' : (lv.peak > -6 ? 'hot' : '');
    valEl.textContent = lv.peak <= -99 ? MINUS + '\u221e' : lv.peak.toFixed(0).replace('-', MINUS);
  }
  function renderHeader(fr) {
    const s = fr.status, run = s.mode !== 'stopped';
    $('status-dot').className = 'dot' + (s.error ? ' err' : run ? ' on' : '');
    let txt = 'Stopped';
    if (run) {
      txt = (s.mode === 'demo' ? 'Demo signal' : s.device) + ' \u00b7 ' + (s.sr / 1000).toFixed(s.sr % 1000 ? 1 : 0) + ' kHz \u00b7 ' + s.nch + ' ch';
      if (s.dropped > 0) txt += ' \u00b7 ' + s.dropped + ' blocks dropped';
    }
    $('status-text').textContent = txt;
    $('btn-stop').hidden = !run;
    $('btn-start').hidden = run || !s.live;
    $('btn-demo').hidden = s.mode === 'demo';
    $('btn-demo').className = 'btn' + (!s.live && !run ? ' primary' : '');
    $('meters').hidden = !run || !fr.levels;
    if (run && fr.levels) { meter($('m-meas'), $('m-meas-val'), fr.levels.meas); meter($('m-ref'), $('m-ref-val'), fr.levels.ref); }

    const n = $('notice');
    if (s.error) { n.className = 'notice err'; n.textContent = s.error; n.hidden = false; }
    else if (s.demo_only) { n.className = 'notice'; n.hidden = false;
      n.textContent = 'Hosted preview: this is a simulated show, so you can try every tool. To measure a real system, run TuneTrace on your own computer with your interface connected.'; }
    else n.hidden = true;

    const a = $('alert');
    if (run && fr.alert) {
      const al = fr.alert, fb = al.status === 'feedback';
      const where = al.src.length ? ' on ' + esc(al.src[0].name) + ' (input ' + al.src[0].ch + ')' : '';
      setHTML('alert', '<span class="tag">' + (fb ? 'FEEDBACK' : 'RINGING') + '</span><span class="f num">' + fmtHz(al.f) + '</span>' +
        '<span class="fix">Cut <span class="num">' + sgn(al.notch.gain, 0) + ' dB, Q ' + al.notch.q + '</span>' + where +
        (al.growing ? ' \u00b7 level is rising' : '') + (al.n > 1 ? ' \u00b7 ' + (al.n - 1) + ' more' : '') + '</span>');
      a.className = 'alert' + (fb ? '' : ' ring'); a.hidden = false;
    } else if (run) {
      setHTML('alert', '<span class="tag">CLEAR</span><span class="fix">Listening on ' + esc(fr.levels ? fr.levels.meas.name : 'the measurement mic') + '. No sustained tones right now.' +
        (fr.counts && fr.counts.events ? ' ' + fr.counts.events + ' logged earlier.' : '') + '</span>');
      a.className = 'alert calm'; a.hidden = false;
    } else a.hidden = true;

    const cnt = fr.counts || { events: 0, active: 0, crit: 0, warn: 0 };
    const fbc = $('tab-fb-count');
    fbc.hidden = !cnt.events; fbc.textContent = cnt.events; fbc.className = 'tab-count' + (cnt.active ? ' crit' : '');
    const chc = $('tab-ch-count');
    chc.hidden = !(cnt.crit + cnt.warn); chc.textContent = cnt.crit + cnt.warn; chc.className = 'tab-count' + (cnt.crit ? ' crit' : ' warn');
  }

  function emptyState(kind) {
    const s = S.frame.status;
    if (kind === 'stopped') {
      return '<h3>No input running</h3><p>' + (s.live
        ? 'Start your audio interface to listen to the room, or load the demo signal to see how TuneTrace works.'
        : 'Load the demo signal to see how TuneTrace works.') + '</p><div class="row">' +
        (s.live ? '<button class="btn primary" data-act="start">Start input</button>' : '') +
        '<button class="btn' + (s.live ? '' : ' primary') + '" data-act="demo">Demo signal</button></div>';
    }
    if (kind === 'same') return '<h3>One input selected for both jobs</h3><p>The transfer function compares the measurement mic with a reference. Pick two different inputs in Setup.</p><div class="row"><button class="btn" data-act="setup">Open Setup</button></div>';
    return '';
  }

  // ----------------------------------------------------------------- feedback
  function ageText(a) { return 'stopped ' + (a < 90 ? Math.max(1, Math.round(a)) + ' s ago' : Math.round(a / 60) + ' min ago'); }
  function renderFeedback(fr) {
    const run = fr.status.mode !== 'stopped';
    setHTML('rta-empty', run ? '' : emptyState('stopped'));
    $('rta-src').textContent = run && fr.levels ? fr.levels.meas.name + ' (input ' + fr.levels.meas.ch + ')' : '';
    if (!run || !fr.rta) {
      plots.rta.set(S.st.grid, [], []);
      setHTML('events', '<div class="blank"><b>Nothing to report</b>Problem frequencies appear here once the input is running.</div>');
      setHTML('buildups', ''); $('buildups-title').hidden = true;
      return;
    }
    let mx = -200;
    for (const v of fr.rta.peak) if (v !== null && v > mx) mx = v;
    if (mx > S.rtaTop - 4 || mx < S.rtaTop - 30) S.rtaTop = Math.max(-50, Math.min(0, Math.ceil((mx + 8) / 10) * 10));
    plots.rta.setRange(S.rtaTop - 70, S.rtaTop, 10);
    const marks = fr.events.map((e) => ({ f: e.f, color: e.status === 'feedback' ? C['--color-crit'] : C['--color-warn'],
      alpha: e.active ? 1 : 0.45, dash: e.active ? [] : [4, 4], width: e.active ? 2 : 1, label: fmtHz(e.f).replace(' ', '') }));
    plots.rta.set(S.st.grid, [
      { data: fr.rta.peak, color: C['--trace-live'], fill: true, fillOnly: true, fillAlpha: 0.13 },
      { data: fr.rta.avg, color: C['--trace-live'], width: 1.6, name: 'Average' },
      { data: fr.rta.peak, color: C['--color-faint'], width: 1, name: 'Peak', alpha: 0.9 },
    ], marks);

    let h = '';
    for (const e of fr.events) {
      h += '<article class="ev ' + e.status + (e.active ? ' active' : '') + '" data-testid="event">' +
        '<div class="ev-top"><span class="ev-f num">' + fmtHz(e.f) + '</span>' +
        '<span class="badge ' + e.status + '"><b>' + (e.status === 'feedback' ? 'Feedback' : 'Ringing') + '</b></span>' +
        '<span class="ev-meta num">' + (e.active ? (e.growing ? 'sounding, rising' : 'sounding now') : ageText(e.age)) + '</span></div>' +
        '<div class="ev-note"><span class="num">peak ' + e.max.toFixed(0).replace('-', MINUS) + ' dBFS' + (e.hits > 1 ? ' \u00b7 \u00d7' + e.hits : '') + '</span> \u00b7 note ' + esc(e.note.replace('-', MINUS)) + ' \u00b7 1/3-oct band ' + fmtHz(e.iso) + '</div>' +
        '<div class="ev-fix"><span class="k">Cut</span><span class="num">Bell ' + fmtHz(e.notch.f) + ' \u00b7 ' + sgn(e.notch.gain, 0) + ' dB \u00b7 Q ' + e.notch.q + '</span></div>' +
        '<div class="ev-where">' + esc(e.where) + '</div></article>';
    }
    setHTML('events', h || '<div class="blank"><b>All clear</b>Listening. A tone that stands out and holds steady shows up here with a suggested cut and the input it came from.</div>');
    let b = '';
    for (const u of fr.buildups) {
      b += '<article class="ev"><div class="ev-top"><span class="ev-f num">' + fmtHz(u.f) + '</span><span class="ev-meta num">+' + u.excess.toFixed(0) + ' dB over its neighbours</span></div>' +
        '<div class="ev-fix"><span class="k">Try</span><span class="num">Bell ' + fmtHz(u.iso) + ' \u00b7 ' + sgn(u.cut) + ' dB \u00b7 Q 3</span></div>' +
        '<div class="ev-where">A wide hump in the room spectrum, not a ringing tone. Confirm it against the transfer function before cutting.</div></article>';
    }
    $('buildups-title').hidden = !b;
    setHTML('buildups', b);
  }

  // --------------------------------------------------------------------- tune
  function renderTune(fr) {
    const st = S.st, run = fr.status.mode !== 'stopped';
    const tf = run && fr.tf && fr.tf.mag ? fr.tf : null;
    const haveStored = st.traces.length > 0;
    setHTML('tf-empty', !run && !haveStored ? emptyState('stopped') : (run && fr.tf_ok === false && !haveStored ? emptyState('same') : ''));
    $('tf-src').textContent = run && fr.levels && fr.tf_ok ? fr.levels.meas.name + ' vs ' + fr.levels.ref.name : '';

    const off = fr.target_off;
    let mid = off;
    if (mid === null || mid === undefined) {
      const src = tf ? tf.mag : (st.best ? st.best.mag : null);
      if (src) { const v = src.filter((x, i) => x !== null && st.grid[i] > 200 && st.grid[i] < 4000).sort((a, b) => a - b); if (v.length) mid = v[v.length >> 1]; }
    }
    if (mid !== null && mid !== undefined && (S.magMid === null || Math.abs(mid - S.magMid) > 4)) S.magMid = Math.round(mid / 3) * 3;
    const m0 = S.magMid === null ? 0 : S.magMid;
    plots.mag.setRange(m0 - 21, m0 + 21, 6);

    const series = [], legend = [];
    if (off !== null && off !== undefined) {
      series.push({ data: st.target, offset: off, color: C['--trace-target'], dash: [6, 5], width: 1.5, name: 'Target' });
      legend.push(['Target', C['--trace-target'], true]);
    }
    for (const t of st.traces) if (t.visible) {
      series.push({ data: t.mag, color: traceColor(t.color), width: 1, alpha: 0.85, name: t.name, mask: t.coh });
      legend.push([t.name, traceColor(t.color)]);
    }
    if (tf) { series.push({ data: tf.mag, color: C['--trace-live'], width: 1.6, name: 'Live', mask: tf.coh }); legend.push(['Live', C['--trace-live']]); }
    if (st.best && st.traces.length) { series.push({ data: st.best.mag, color: C['--trace-best'], width: 2.2, name: 'Best trace' }); legend.push(['Best trace', C['--trace-best']]); }
    if (st.eq) { series.push({ data: st.eq.predicted, color: C['--trace-pred'], width: 2, name: 'With EQ' }); legend.push(['Predicted with EQ', C['--trace-pred']]); }
    plots.mag.set(st.grid, series, []);
    setHTML('tf-legend', legend.map((l) => '<span style="color:' + l[1] + '"><i' + (l[2] ? ' class="dash"' : '') + '></i><span style="color:var(--color-muted)">' + esc(l[0]) + '</span></span>').join(''));

    const cs = [];
    if (st.best && st.traces.length) cs.push({ data: st.best.coh, color: C['--trace-best'], width: 1, alpha: 0.7, name: 'Best trace' });
    if (tf) cs.push({ data: tf.coh, color: C['--trace-coh'], width: 1.4, fill: true, fillAlpha: 0.16, name: 'Live' });
    plots.coh.set(st.grid, cs, []);
    plots.phase.set(st.grid, tf ? [{ data: tf.phase, color: C['--trace-live'], width: 1.3, name: 'Phase', mask: tf.coh }] : [], []);

    // delay
    const d = fr.delay, dn = $('delay-note');
    $('delay-ms').textContent = d ? d.ms.toFixed(2) : '0.00';
    if (!run) { dn.textContent = 'Start the input, then find the delay with signal playing.'; dn.className = 'step-note'; }
    else if (d.pending) { dn.textContent = 'Listening for the arrival\u2026'; dn.className = 'step-note'; }
    else if (d.result && d.result.ok) {
      const inv = d.result.polarity < 0;
      dn.textContent = 'Found ' + d.ms.toFixed(2) + ' ms, about ' + d.m.toFixed(1) + ' m of path. ' + (inv ? 'Polarity looks inverted: check the mic cable or the output polarity.' : 'Polarity normal.');
      dn.className = 'step-note ' + (inv ? 'bad' : 'good');
    } else if (d.result) { dn.textContent = d.result.msg; dn.className = 'step-note bad'; }
    else { dn.textContent = d.samples ? 'Delay set to ' + d.ms.toFixed(2) + ' ms.' : 'Not aligned yet. Play signal and press Find delay.'; dn.className = 'step-note' + (d.samples ? '' : ' bad'); }

    // quality
    const qb = $('q-bar'), qn = $('q-note');
    if (!tf) {
      qb.style.width = '0%'; $('q-val').textContent = '\u2013'; qn.className = 'step-note';
      qn.textContent = !run ? 'Play pink noise or music through the system.' : (fr.tf_ok === false ? 'Pick different measurement and reference inputs in Setup.' : 'Waiting for signal\u2026');
    } else {
      const q = fr.tf.quality, cls = q >= 0.85 ? 'good' : q >= 0.6 ? 'fair' : 'poor';
      qb.style.width = Math.round(q * 100) + '%'; qb.className = cls;
      $('q-val').textContent = Math.round(q * 100) + '%';
      qn.className = 'step-note ' + (cls === 'good' ? 'good' : 'bad');
      qn.textContent = fr.tf.frames < 8 ? 'Averaging\u2026' : cls === 'good' ? 'Good. Coherence is high, so the trace can be trusted. Capture when it has settled.'
        : cls === 'fair' ? 'Usable. For a cleaner trace raise the test signal or move the mic away from reflective surfaces.'
          : 'Poor. Check the delay, the reference routing, and that the test signal sits well above the room noise.';
      if (fr.tf.frames < 8) qn.className = 'step-note';
    }
    $('btn-demo-move').hidden = fr.status.mode !== 'demo';
    if (fr.status.demo_pos) $('btn-demo-move').textContent = 'Move mic (demo, position ' + fr.status.demo_pos + ' of 3)';
  }

  function renderTraces() {
    const st = S.st;
    let h = '';
    for (const t of st.traces) {
      h += '<li data-id="' + t.id + '"><span class="sw" style="color:' + traceColor(t.color) + '"></span>' +
        '<input type="text" value="' + esc(t.name) + '" aria-label="Trace name" data-f="name">' +
        '<span class="q num" title="Average coherence">' + Math.round(t.quality * 100) + '%</span>' +
        '<label class="chk" title="Include in the best trace"><input type="checkbox" data-f="in_avg"' + (t.in_avg ? ' checked' : '') + '>Use</label>' +
        '<span class="row" style="gap:0"><label class="chk" title="Show on the plot"><input type="checkbox" data-f="visible"' + (t.visible ? ' checked' : '') + '>Show</label>' +
        '<button class="icon-btn" data-f="del" aria-label="Delete ' + esc(t.name) + '" title="Delete">\u2715</button></span></li>';
    }
    if (st.best && st.traces.length) {
      h += '<li class="best-row"><span class="sw" style="color:' + C['--trace-best'] + '"></span><span>Best trace</span><span class="q" style="grid-column: 3 / -1; text-align:right">average of ' + st.best.n + ' capture' + (st.best.n === 1 ? '' : 's') + '</span></li>';
      if (st.best.n < 3) h += '<li><span></span><span class="q" style="grid-column: 2 / -1">Capture ' + (3 - st.best.n) + ' more position' + (3 - st.best.n === 1 ? '' : 's') + ' for a trustworthy average.</span></li>';
    }
    setHTML('traces', h);
  }

  function renderEq() {
    const st = S.st, e = st.eq, con = st.consoles[st.settings.console];
    $('btn-eq-clear').hidden = !e;
    if (!e) { setHTML('eq-result', '<p class="step-note">Fits up to the chosen number of bell filters so the trace follows the target. Boosts stay small and wide; narrow dips are left alone because they are usually cancellations that EQ cannot fill.</p>'); return; }
    let h = '<table class="eq-table"><thead><tr><th>Band</th><th>Freq</th><th>Gain</th><th>Q</th></tr></thead><tbody>';
    e.bands.forEach((b, i) => {
      h += '<tr><td>' + (i + 1) + ' Bell</td><td class="num">' + fmtHz(b.f) + '</td><td class="num ' + (b.gain < 0 ? 'cut' : 'boost') + '">' + sgn(b.gain) + ' dB</td><td class="num">' + b.q.toFixed(1) + '</td></tr>';
    });
    h += '</tbody></table>';
    if (!e.bands.length) h = '<p class="step-note good">Already within 1 dB of the target. No EQ needed.</p>';
    h += '<p class="eq-sum">Fitted to ' + esc(e.source) + ' at ' + esc(e.time) + ', ' + fmtHz(e.f_lo) + ' to ' + fmtHz(e.f_hi) + '. Deviation from target: <span class="num">' + e.before.toFixed(1) + ' dB</span> now, <b class="num">' + e.after.toFixed(1) + ' dB</b> predicted.</p>' +
      '<p class="eq-sum">Enter these on the output or matrix EQ feeding this system. ' + esc(con.out_note) + (e.bands.length > con.out_bands ? ' This fit uses more bands than that.' : '') + ' Re-measure after applying.</p>' +
      '<div class="row" style="margin-top:var(--space-3)"><button class="btn small" data-act="copy-eq">Copy table</button><button class="btn small" data-act="report">Full report</button></div>';
    setHTML('eq-result', h);
  }

  // ----------------------------------------------------------------- channels
  const SEV = { crit: 'Fix', warn: 'Check', info: 'Note', ok: 'OK' };
  function buildChannelRows(list) {
    const types = Object.entries(S.st.types).map(([k, v]) => '<option value="' + k + '">' + esc(v) + '</option>').join('');
    let h = '';
    for (const c of list) {
      h += '<div class="ch" id="ch-' + c.i + '" data-i="' + c.i + '" data-testid="channel">' +
        '<div class="ch-n num">' + (c.i + 1) + '</div>' +
        '<input type="text" class="ch-name" aria-label="Name of input ' + (c.i + 1) + '" maxlength="32">' +
        '<select class="ch-type" aria-label="Source type of input ' + (c.i + 1) + '">' + types + '</select>' +
        '<div class="ch-meter"><span class="meter-bar"><i></i></span><span class="vals num"><span class="pk"></span><span class="rm"></span></span></div>' +
        '<div class="ch-body"></div></div>';
    }
    $('channels').innerHTML = h;
    S.chRows = list.length;
    S.html.chBody = {};
    syncChannelMeta();
  }
  function syncChannelMeta() {
    for (const c of S.st.channels) {
      const row = $('ch-' + c.i);
      if (!row) continue;
      const nm = row.querySelector('.ch-name'), ty = row.querySelector('.ch-type');
      if (document.activeElement !== nm) nm.value = c.name;
      if (document.activeElement !== ty) ty.value = c.type;
    }
  }
  function renderChannels(fr) {
    const run = fr.status.mode !== 'stopped', list = fr.channels || [];
    if (!run || !list.length) {
      if (S.chRows !== 0 || !S.html.chEmpty) { $('channels').innerHTML = '<div class="blank"><b>No inputs to scan</b>Start the input to see every channel of the interface here, with a level check and suggested fixes for each one.</div>'; S.chRows = 0; S.html.chEmpty = true; }
      $('ch-sub').textContent = '';
      return;
    }
    S.html.chEmpty = false;
    if (S.chRows !== list.length || !$('ch-' + list[0].i)) buildChannelRows(list);
    let shown = 0, need = 0;
    for (const c of list) {
      const row = $('ch-' + c.i);
      const problem = c.active && c.worst <= 1;
      if (problem) need++;
      const vis = S.chFilter === 'all' || problem;
      row.hidden = !vis;
      if (!vis) continue;
      shown++;
      row.className = 'ch s' + c.worst + (c.active ? '' : ' idle');
      const bar = row.querySelector('.meter-bar i');
      bar.style.width = Math.max(0, Math.min(100, (c.peak + 60) / 60 * 100)) + '%';
      bar.className = c.clip ? 'clip' : (c.peak > -6 ? 'hot' : '');
      row.querySelector('.pk').textContent = c.active ? 'pk ' + c.peak.toFixed(0).replace('-', MINUS) : '';
      row.querySelector('.rm').textContent = c.active ? 'rms ' + c.rms.toFixed(0).replace('-', MINUS) : '';
      let h = '';
      if (c.role) h += '<div class="finding"><span class="badge role"><b>' + (c.role === 'meas' ? 'Measurement mic' : 'Reference') + '</b></span></div>';
      for (const f of c.findings) {
        h += '<div class="finding"><span class="badge ' + f.sev + '"><b>' + SEV[f.sev] + '</b></span><span class="t">' + esc(f.title) + '</span><span class="d">' + esc(f.detail) + '</span></div>';
      }
      const s = c.strip, chips = [];
      if (s.gain) chips.push('<span class="chip num"><span class="k">GAIN</span>' + esc(s.gain.replace('-', MINUS)) + '</span>');
      if (s.hpf) chips.push('<span class="chip num"><span class="k">HPF</span>' + fmtHz(s.hpf) + '</span>');
      for (const b of s.bands) chips.push('<span class="chip num"><span class="k">' + esc(b.band.toUpperCase()) + '</span>' + fmtHz(b.f) + ' \u00b7 ' + sgn(b.gain) + ' dB \u00b7 Q ' + b.q.toFixed(1) + (b.dyn ? ' \u00b7 dynamic' : '') + '<span class="y">' + esc(b.why) + '</span></span>');
      if (s.dyn) chips.push('<span class="chip"><span class="k">DYN</span>' + esc(s.dyn) + '</span>');
      if (chips.length) h += '<div class="strip">' + chips.join('') + '</div>';
      if (S.html.chBody[c.i] !== h) { S.html.chBody[c.i] = h; row.querySelector('.ch-body').innerHTML = h; }
    }
    $('ch-sub').textContent = need ? need + ' of ' + list.length + ' need attention' : 'all ' + list.length + ' look fine';
    let none = $('ch-none');
    if (!shown && !none) { none = document.createElement('div'); none.id = 'ch-none'; none.className = 'blank'; none.innerHTML = '<b>Nothing needs attention</b>Every active input is within normal range.'; $('channels').appendChild(none); }
    if (none) none.hidden = shown > 0;
  }

  // -------------------------------------------------------------------- setup
  function opt(v, label, sel) { return '<option value="' + esc(v) + '"' + (sel ? ' selected' : '') + '>' + esc(label) + '</option>'; }
  function renderDevices() {
    const d = S.devices, st = S.st;
    if (!d || !st) return;
    const s = st.settings;
    const off = $('live-off');
    if (st.demo_only) {
      off.hidden = false; off.innerHTML = 'This hosted preview runs the built-in demo only. Live input, the signal generator and saved sessions work when TuneTrace runs on your own computer.';
    } else if (d.error) {
      off.hidden = false; off.innerHTML = 'Live audio is unavailable on this computer: ' + esc(d.error) + '<br>Install it with <code>python3 -m pip install sounddevice</code> and start TuneTrace again.';
    } else off.hidden = true;
    const dis = st.demo_only || !!d.error;
    ['dev-in', 'dev-sr', 'dev-ch', 'btn-start-2', 'btn-refresh', 'gen-dev', 'gen-ch', 'gen-type', 'gen-freq', 'gen-level', 'btn-gen'].forEach((id) => { $(id).disabled = dis; });
    let h = opt('default', dis ? 'No interface available' : 'System default input', s.device === null);
    for (const i of d.inputs) h += opt(i.id, i.name + ' \u2014 ' + i.ch + ' in (' + i.api + ')', s.device === i.id);
    $('dev-in').innerHTML = h;
    $('dev-sr').value = String(s.sr || 0);
    fillChannelCount();
    let g = opt('default', dis ? 'No interface available' : 'System default output', st.gen.device === null);
    for (const o of d.outputs) g += opt(o.id, o.name + ' \u2014 ' + o.ch + ' out (' + o.api + ')', st.gen.device === o.id);
    $('gen-dev').innerHTML = g;
    fillGenChannels();
  }
  function fillChannelCount() {
    const d = S.devices, id = $('dev-in').value;
    const dev = d.inputs.find((i) => String(i.id) === id) || d.inputs.find((i) => i.default);
    const max = dev ? dev.ch : 2, cur = S.st.settings.channels || 0;
    let h = opt(0, 'All' + (dev ? ' (' + Math.min(max, 64) + ')' : ''), !cur);
    for (const n of [2, 4, 8, 16, 24, 32, 48]) if (n < max) h += opt(n, 'First ' + n, cur === n);
    $('dev-ch').innerHTML = h;
  }
  function fillGenChannels() {
    const d = S.devices, id = $('gen-dev').value;
    const dev = d.outputs.find((o) => String(o.id) === id) || d.outputs.find((o) => o.default);
    const max = Math.min(dev ? dev.ch : 2, 64), cur = (S.st.gen.channels || [1])[0];
    let h = '';
    for (let n = 1; n <= max; n++) h += opt(n, 'Output ' + n, n === cur);
    $('gen-ch').innerHTML = h;
  }
  function renderRoles() {
    const st = S.st, list = st.channels.length ? st.channels : null;
    for (const [id, key] of [['role-meas', 'meas'], ['role-ref', 'ref']]) {
      let h = '';
      const n = list ? list.length : Math.max(2, st.roles[key] + 1, 8);
      for (let i = 0; i < n; i++) h += opt(i, (i + 1) + (list ? ' \u00b7 ' + list[i].name : ''), st.roles[key] === i);
      if (document.activeElement !== $(id)) $(id).innerHTML = h;
    }
  }

  // ------------------------------------------------------------- static render
  function seg(id, val) { $(id).querySelectorAll('button').forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.v === val))); }
  function setVal(id, v) { const el = $(id); if (document.activeElement !== el) el.value = String(v); }
  function renderStatic() {
    const st = S.st, s = st.settings;
    $('foot-ver').textContent = 'TuneTrace ' + st.version;
    setVal('rta-smooth', s.rta_smooth); setVal('tf-smooth', s.tf_smooth); setVal('tf-avg', s.avg);
    seg('sens', s.sens);
    // target
    let h = '';
    for (const [k, p] of Object.entries(st.presets)) h += opt(k, p.label, s.target.preset === k);
    h += opt('custom', 'Custom', s.target.preset === 'custom');
    if (document.activeElement !== $('target-preset')) $('target-preset').innerHTML = h;
    setVal('t-low', s.target.low); setVal('t-tilt', s.target.tilt);
    $('t-low-val').textContent = sgn(s.target.low) + ' dB';
    $('t-tilt-val').textContent = sgn(s.target.tilt, 2) + ' dB/oct';
    // eq options
    const con = st.consoles[s.console];
    h = '';
    for (let n = 3; n <= 12; n++) h += opt(n, n + (n === con.out_bands ? ' (console output EQ)' : ''), s.eq.bands === n);
    if (document.activeElement !== $('eq-bands')) $('eq-bands').innerHTML = h;
    setVal('eq-boost', s.eq.boost >= 6 ? 6 : s.eq.boost >= 3 ? 3 : 0);
    h = opt('auto', 'Best trace if captured, else live', s.eq.source === 'auto') + opt('live', 'Live measurement', s.eq.source === 'live');
    for (const t of st.traces) h += opt(t.id, t.name, s.eq.source === t.id);
    if (document.activeElement !== $('eq-source')) $('eq-source').innerHTML = h;
    renderTraces(); renderEq();
    // setup
    h = '';
    for (const [k, c] of Object.entries(st.consoles)) h += opt(k, c.label, s.console === k);
    if (document.activeElement !== $('console')) $('console').innerHTML = h;
    $('console-note').textContent = 'Input strip: HPF plus ' + con.in_bands + ' EQ bands, \u00b1' + con.gain + ' dB, Q up to ' + con.q_max + '. ' + con.out_note + ' USB recording interface: ' + con.usb_ch + ' channels.';
    $('cal-state').textContent = st.cal ? 'Loaded: ' + st.cal.name + '. Its response is subtracted from the spectrum and the transfer function.' : "No calibration file loaded. Without one, the mic's own response is part of the trace.";
    $('btn-cal-clear').hidden = !st.cal;
    const gb = $('btn-gen');
    gb.setAttribute('aria-pressed', String(!!st.gen.on)); gb.textContent = st.gen.on ? 'Stop generator' : 'Start generator';
    setVal('gen-type', st.gen.type); setVal('gen-freq', st.gen.freq); setVal('gen-level', st.gen.level);
    $('gen-level-val').textContent = String(st.gen.level).replace('-', MINUS) + ' dBFS';
    $('gen-freq-wrap').hidden = $('gen-type').value !== 'sine';
    renderRoles(); renderDevices(); syncChannelMeta();
  }

  function renderFrame() {
    const fr = S.frame;
    renderHeader(fr);
    if (S.view === 'feedback') renderFeedback(fr);
    else if (S.view === 'tune') renderTune(fr);
    else if (S.view === 'channels') renderChannels(fr);
  }

  // ------------------------------------------------------------------ actions
  function show(view) {
    S.view = view;
    document.querySelectorAll('.tab').forEach((t) => t.setAttribute('aria-selected', String(t.dataset.view === view)));
    document.querySelectorAll('.view').forEach((v) => { v.hidden = v.id !== 'view-' + view; });
    Object.values(plots).forEach((p) => p.resize());
    if (S.frame && S.st) renderHeader(S.frame);
    if (view === 'setup' && !S.devices) loadDevices(false);
  }
  async function loadDevices(refresh) {
    try { S.devices = await (await api('/api/devices' + (refresh ? '?refresh=1' : ''))).json(); renderDevices(); if (refresh) toast('Device list updated.'); }
    catch (e) { /* the loop reports connection loss */ }
  }
  async function startLive() {
    const dev = $('dev-in').value || 'default';
    const r = await cmd('start', { device: dev, sr: +$('dev-sr').value || 0, channels: +$('dev-ch').value || 0 });
    if (!r.ok) show('setup');
  }
  async function startDemo() { await cmd('start_demo'); S.magMid = null; }
  const debounce = (fn, ms) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; };

  function copyText(text) {
    const done = () => toast('Copied.');
    const fallback = () => {
      const ta = document.createElement('textarea'); ta.value = text; ta.style.position = 'fixed'; ta.style.opacity = '0';
      document.body.appendChild(ta); ta.select();
      let ok = false; try { ok = document.execCommand('copy'); } catch (e) { ok = false; }
      ta.remove(); ok ? done() : toast('Copy is blocked here. Select the text and copy it by hand.', true);
    };
    if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(text).then(done, fallback); else fallback();
  }
  function download(name, text, type) {
    const a = document.createElement('a');
    a.href = URL.createObjectURL(new Blob([text], { type: type || 'text/plain' }));
    a.download = name; document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 2000);
  }
  function stamp() { const d = new Date(), p = (n) => String(n).padStart(2, '0'); return d.getFullYear() + p(d.getMonth() + 1) + p(d.getDate()) + '-' + p(d.getHours()) + p(d.getMinutes()); }
  async function openReport() {
    try {
      const txt = await (await api('/api/report.txt')).text();
      $('dlg-body').textContent = txt;
      const dlg = $('dlg');
      if (dlg.showModal) dlg.showModal(); else dlg.setAttribute('open', '');
    } catch (e) { toast('Could not build the report.', true); }
  }
  function eqText() {
    const e = S.st.eq; if (!e) return '';
    const L = ['System EQ from TuneTrace, fitted to ' + e.source];
    e.bands.forEach((b, i) => L.push('Band ' + (i + 1) + '\tBell\t' + fmtHz(b.f) + '\t' + (b.gain > 0 ? '+' : '') + b.gain.toFixed(1) + ' dB\tQ ' + b.q.toFixed(1)));
    return L.join('\n');
  }

  function bind() {
    document.querySelectorAll('.tab').forEach((t) => t.addEventListener('click', () => show(t.dataset.view)));
    document.querySelector('.tabs').addEventListener('keydown', (e) => {
      if (e.key !== 'ArrowRight' && e.key !== 'ArrowLeft') return;
      const tabs = Array.from(document.querySelectorAll('.tab')), i = tabs.indexOf(document.activeElement);
      if (i < 0) return;
      const n = tabs[(i + (e.key === 'ArrowRight' ? 1 : tabs.length - 1)) % tabs.length];
      n.focus(); show(n.dataset.view);
    });
    $('btn-start').addEventListener('click', startLive);
    $('btn-start-2').addEventListener('click', startLive);
    $('btn-demo').addEventListener('click', startDemo);
    $('btn-stop').addEventListener('click', () => cmd('stop'));
    document.addEventListener('click', (e) => {
      const b = e.target.closest('[data-act]'); if (!b) return;
      const a = b.dataset.act;
      if (a === 'start') startLive(); else if (a === 'demo') startDemo(); else if (a === 'setup') show('setup');
      else if (a === 'copy-eq') copyText(eqText()); else if (a === 'report') openReport();
    });
    // feedback
    $('rta-smooth').addEventListener('change', (e) => cmd('set', { rta_smooth: +e.target.value }));
    $('sens').addEventListener('click', (e) => { const b = e.target.closest('button'); if (b) cmd('set', { sens: b.dataset.v }); });
    $('btn-clear-events').addEventListener('click', () => cmd('clear_events'));
    // tune
    $('tf-smooth').addEventListener('change', (e) => cmd('set', { tf_smooth: +e.target.value }));
    $('tf-avg').addEventListener('change', (e) => cmd('set', { avg: e.target.value }));
    $('btn-reset-avg').addEventListener('click', () => cmd('reset_avg'));
    $('btn-delay').addEventListener('click', () => cmd('find_delay'));
    $('btn-capture').addEventListener('click', async () => { const r = await cmd('capture'); if (r.ok && !r.msg) toast('Trace captured.'); });
    $('btn-demo-move').addEventListener('click', async () => {
      const r = await cmd('demo_move');
      if (r.ok) { toast('Mic moved to position ' + r.pos + '. Finding the new delay.'); setTimeout(() => cmd('find_delay'), 2200); }
    });
    $('traces').addEventListener('change', (e) => {
      const li = e.target.closest('li[data-id]'); if (!li) return;
      const f = e.target.dataset.f, body = { id: +li.dataset.id };
      body[f] = e.target.type === 'checkbox' ? e.target.checked : e.target.value;
      cmd('trace', body);
    });
    $('traces').addEventListener('click', (e) => {
      const b = e.target.closest('[data-f="del"]'); if (!b) return;
      cmd('trace_delete', { id: +b.closest('li').dataset.id });
    });
    $('target-preset').addEventListener('change', (e) => {
      if (e.target.value === 'custom') cmd('set', { target: { low: +$('t-low').value, tilt: +$('t-tilt').value } });
      else cmd('set', { target: { preset: e.target.value } });
    });
    const sendTarget = debounce(() => cmd('set', { target: { low: +$('t-low').value, tilt: +$('t-tilt').value } }), 120);
    $('t-low').addEventListener('input', (e) => { $('t-low-val').textContent = sgn(+e.target.value) + ' dB'; sendTarget(); });
    $('t-tilt').addEventListener('input', (e) => { $('t-tilt-val').textContent = sgn(+e.target.value, 2) + ' dB/oct'; sendTarget(); });
    $('eq-bands').addEventListener('change', (e) => cmd('set', { eq: { bands: +e.target.value } }));
    $('eq-boost').addEventListener('change', (e) => cmd('set', { eq: { boost: +e.target.value } }));
    $('eq-source').addEventListener('change', (e) => cmd('set', { eq: { source: /^\d+$/.test(e.target.value) ? +e.target.value : e.target.value } }));
    $('btn-eq').addEventListener('click', async () => { const r = await cmd('auto_eq'); if (r.ok) toast(r.n ? r.n + ' EQ band' + (r.n === 1 ? '' : 's') + ' suggested.' : 'Already on target.'); });
    $('btn-eq-clear').addEventListener('click', () => cmd('eq_clear'));
    // channels
    $('ch-filter').addEventListener('click', (e) => { const b = e.target.closest('button'); if (!b) return; S.chFilter = b.dataset.v; seg('ch-filter', S.chFilter); if (S.frame) renderFrame(); });
    $('btn-report').addEventListener('click', openReport);
    $('channels').addEventListener('change', (e) => {
      const row = e.target.closest('.ch'); if (!row) return;
      if (e.target.classList.contains('ch-name')) cmd('channel', { i: +row.dataset.i, name: e.target.value });
      else if (e.target.classList.contains('ch-type')) cmd('channel', { i: +row.dataset.i, type: e.target.value });
    });
    $('channels').addEventListener('keydown', (e) => { if (e.key === 'Enter' && e.target.classList.contains('ch-name')) e.target.blur(); });
    // setup
    $('dev-in').addEventListener('change', fillChannelCount);
    $('btn-refresh').addEventListener('click', () => loadDevices(true));
    $('role-meas').addEventListener('change', (e) => cmd('roles', { meas: +e.target.value }));
    $('role-ref').addEventListener('change', (e) => cmd('roles', { ref: +e.target.value }));
    $('console').addEventListener('change', (e) => cmd('set', { console: e.target.value }));
    $('gen-dev').addEventListener('change', fillGenChannels);
    $('gen-type').addEventListener('change', (e) => { $('gen-freq-wrap').hidden = e.target.value !== 'sine'; });
    $('gen-level').addEventListener('input', (e) => { $('gen-level-val').textContent = e.target.value.replace('-', MINUS) + ' dBFS'; if (S.st.gen.on) cmd('gen_level', { level: +e.target.value }); });
    $('btn-gen').addEventListener('click', () => {
      if (S.st.gen.on) cmd('gen', { on: false });
      else cmd('gen', { on: true, device: $('gen-dev').value, channels: [+$('gen-ch').value || 1], type: $('gen-type').value, freq: +$('gen-freq').value || 1000, level: +$('gen-level').value });
    });
    $('cal-file').addEventListener('change', (e) => {
      const f = e.target.files[0]; if (!f) return;
      const rd = new FileReader();
      rd.onload = async () => { const r = await cmd('cal_load', { name: f.name, text: String(rd.result) }); if (r.ok) toast('Calibration loaded.'); };
      rd.readAsText(f); e.target.value = '';
    });
    $('btn-cal-clear').addEventListener('click', () => cmd('cal_clear'));
    $('btn-export').addEventListener('click', async () => {
      try { download('tunetrace-session-' + stamp() + '.json', await (await api('/api/session.json')).text(), 'application/json'); }
      catch (e) { toast('Could not export the session.', true); }
    });
    $('session-file').addEventListener('change', (e) => {
      const f = e.target.files[0]; if (!f) return;
      const rd = new FileReader();
      rd.onload = async () => {
        let data; try { data = JSON.parse(String(rd.result)); } catch (err) { toast('That file is not a TuneTrace session.', true); return; }
        const r = await cmd('session_import', { data }); if (r.ok) toast('Session imported.');
      };
      rd.readAsText(f); e.target.value = '';
    });
    // dialog
    $('dlg-close').addEventListener('click', () => $('dlg').close());
    $('dlg-copy').addEventListener('click', () => copyText($('dlg-body').textContent));
    $('dlg-download').addEventListener('click', () => download('tunetrace-report-' + stamp() + '.txt', $('dlg-body').textContent));
    $('dlg').addEventListener('click', (e) => { if (e.target === $('dlg')) $('dlg').close(); });
  }

  bind();
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(() => Object.values(plots).forEach((p) => { p.readColors(); p.draw(); }));
  loadDevices(false);
  tick();
})();
