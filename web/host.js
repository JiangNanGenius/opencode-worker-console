/* Host overview: charts, active work, quotas and structured controls.
   Pure helpers are exported for node tests; browser wiring boots only when the
   view exists. Missing telemetry renders as unavailable (—), never as zero,
   and progress appears only when a real report exists. */
(function (root) {
  'use strict';

  const esc = (x) => String(x ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  function coercible(value) {
    return value !== null && value !== undefined && value !== '' && typeof value !== 'boolean' && Number.isFinite(Number(value));
  }

  function formatBytes(value) {
    if (!coercible(value)) return '—';
    const n = Number(value);
    const units = [[1 << 30, 'GB'], [1 << 20, 'MB'], [1 << 10, 'KB']];
    for (const [size, label] of units) if (n >= size) return (n / size >= 100 ? Math.round(n / size) : (n / size).toFixed(1)) + ' ' + label;
    return Math.round(n) + ' B';
  }

  function formatRate(bps) {
    if (!coercible(bps)) return '—';
    const n = Number(bps);
    if (n >= 1e6) return (n / 1e6 >= 100 ? Math.round(n / 1e6) : (n / 1e6).toFixed(1)) + ' MB/s';
    if (n >= 1e3) return Math.round(n / 1e3) + ' KB/s';
    return Math.round(n) + ' B/s';
  }

  function formatTemp(value) {
    if (!coercible(value)) return '—';
    return Number(value).toFixed(1) + ' °C';
  }

  function formatPercent(value) {
    if (!coercible(value)) return '—';
    return Math.round(Number(value)) + '%';
  }

  function formatDuration(seconds) {
    if (!coercible(seconds) || Number(seconds) < 0) return '—';
    const n = Number(seconds);
    const s = Math.round(n);
    if (s < 60) return s + ' s';
    if (s < 3600) return Math.floor(s / 60) + ' m ' + (s % 60) + ' s';
    return Math.floor(s / 3600) + ' h ' + Math.floor(s % 3600 / 60) + ' m';
  }

  const GAP_FACTOR = 2.5;

  function splitGaps(points, intervalSeconds) {
    const gap = Math.max(1, Number(intervalSeconds) || 2) * GAP_FACTOR;
    const segments = [];
    let current = [];
    for (const point of points || []) {
      const last = current[current.length - 1];
      if (last && point.t - last.t > gap) { segments.push(current); current = []; }
      current.push(point);
    }
    if (current.length) segments.push(current);
    return segments;
  }

  function chartSVG(points, options) {
    const opts = options || {};
    const width = 320, height = 96, pad = { left: 6, right: 6, top: 8, bottom: 14 };
    if (!points || !points.length) return '<svg class="hostov-svg" viewBox="0 0 ' + width + ' ' + height + '" role="img" aria-label="' + esc(opts.label || '') + '"></svg>';
    const segments = splitGaps(points, opts.interval);
    const t0 = points[0].t, t1 = Math.max(points[points.length - 1].t, t0 + 1);
    const maxValue = Math.max(Number(opts.floor) || 0, ...points.map(p => Number(p.v) || 0)) * 1.08 || 1;
    const x = t => pad.left + (t - t0) / (t1 - t0) * (width - pad.left - pad.right);
    const y = v => pad.top + (1 - Math.max(0, Math.min(1, (Number(v) || 0) / maxValue))) * (height - pad.top - pad.bottom);
    const paths = segments.map(seg => '<path class="hostov-line" d="' + seg.map((p, i) => (i ? 'L' : 'M') + x(p.t).toFixed(1) + ' ' + y(p.v).toFixed(1)).join(' ') + '"/>').join('');
    const last = points[points.length - 1];
    return '<svg class="hostov-svg" viewBox="0 0 ' + width + ' ' + height + '" role="img" aria-label="' + esc(opts.label || '') + '">' +
      '<line class="hostov-gridline" x1="' + pad.left + '" y1="' + y(maxValue / 1.08).toFixed(1) + '" x2="' + (width - pad.right) + '" y2="' + y(maxValue / 1.08).toFixed(1) + '"/>' +
      paths + '<circle class="hostov-dot" cx="' + x(last.t).toFixed(1) + '" cy="' + y(last.v).toFixed(1) + '" r="2.6"/></svg>';
  }

  function taskRowHTML(item, now) {
    const ref = now || Date.now() / 1000;
    const elapsed = item.started_at ? formatDuration(ref - item.started_at) : '—';
    const progress = item.progress || null;
    const hasRealProgress = Boolean(progress && (progress.percent != null || progress.github_run || progress.phase));
    const run = progress && typeof progress.github_run === 'string' && /^https?:\/\/[^\s]+$/i.test(progress.github_run) ? progress.github_run : '';
    const ci = run ? '<a class="hostov-ci" href="' + esc(run) + '" target="_blank" rel="noopener">CI ↗</a>' : '';
    const bar = hasRealProgress && progress.percent != null ? '<span class="hostov-taskbar"><i style="transform:scaleX(' + Math.max(0, Math.min(100, progress.percent)) / 100 + ')"></i></span>' : '';
    return '<article class="hostov-task ' + esc(item.status) + '">' +
      '<div class="hostov-task-main">' + (item.source === 'opencode' ? '<button class="hostov-task-link" data-host-task="'+esc(item.id)+'">'+esc(item.title)+'</button>' : '<strong>'+esc(item.title)+'</strong>') +
      '<span class="hostov-task-meta">' + esc(item.project || '') + ' · ' + esc(item.source === 'codex' ? 'Codex' : 'OpenCode') + '</span></div>' +
      '<div class="hostov-task-side"><span class="status ' + esc(item.status) + '"><i></i>' + esc(item.status) + '</span>' +
      '<span class="hostov-task-model">' + esc(item.model || '—') + (item.effort ? ' · ' + esc(item.effort) : '') + '</span>' +
      '<span class="hostov-task-elapsed">' + esc(elapsed) + '</span>' + bar + ci + '</div></article>';
  }

  function tasksHTML(tasks, now) {
    const items = [...((tasks && tasks.codex && tasks.codex.items) || []).map(t => ({ ...t, source: 'codex' })),
                   ...((tasks && tasks.opencode && tasks.opencode.items) || []).map(t => ({ ...t, source: 'opencode' }))];
    if (!items.length) return '<p class="muted">—</p>';
    return items.map(item => taskRowHTML(item, now)).join('');
  }

  function quotaRowHTML(quota) {
    const windows = (quota.windows || []).filter(w => coercible(w.remaining_percent));
    const worst = windows.reduce((best, w) => !best || Number(w.remaining_percent) < Number(best.remaining_percent) ? w : best, null);
    const balances = (quota.balances || []).map(b => (b.currency === 'CNY' ? '¥' : b.currency === 'USD' ? '$' : (b.currency || '') + ' ') + Number(b.remaining).toFixed(2)).join(' / ');
    const tone = quota.state === 'ok' ? (worst && worst.remaining_percent <= 5 ? 'critical' : 'healthy') : quota.state === 'unavailable' ? '' : 'tight';
    const value = balances || (worst ? Math.round(worst.remaining_percent) + '%' : '—');
    const reset = worst && coercible(worst.resets_at) && Number(worst.resets_at) > 0 ? new Date(Number(worst.resets_at) * 1000).toLocaleString(undefined, { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit', timeZoneName: 'short' }) : '';
    const track = worst ? '<span class="hostov-track"><i class="' + (worst.remaining_percent <= 5 ? 'critical' : worst.remaining_percent <= 25 ? 'tight' : '') + '" style="width:' + Math.max(0, Math.min(100, worst.remaining_percent)) + '%"></i></span>' : '';
    return '<div class="hostov-quota ' + esc(tone) + '"><span class="hostov-quota-name">' + esc(quota.name || quota.id) + '</span>' +
      '<strong>' + esc(value) + '</strong>' + track + '<small>' + esc(reset ? '↻ ' + reset : quota.state === 'ok' ? '' : quota.state) + '</small></div>';
  }

  function serviceRowHTML(service) {
    return '<div class="hostov-service ' + (service.alive ? 'alive' : 'down') + '"><i></i><span>' + esc(service.name) + '</span><small>' + (service.alive ? 'pid ' + esc(service.pid) : 'stopped') + '</small></div>';
  }

  function diskRowHTML(disk) {
    const used = Number(disk.used_percent);
    return '<div class="hostov-disk"><div class="hostov-disk-head"><strong>' + esc(disk.name) + '</strong><span>' + esc(disk.mountpoint) + '</span></div>' +
      '<div class="hostov-disk-bar"><i class="' + (used >= 90 ? 'critical' : used >= 75 ? 'tight' : '') + '" style="width:' + Math.max(0, Math.min(100, used)) + '%"></i></div>' +
      '<small>' + esc(Number.isFinite(used) ? Math.round(used) + '% · ' : '') + esc(formatBytes((disk.free_gb || 0) * (1 << 30))) + ' free / ' + esc(formatBytes((disk.total_gb || 0) * (1 << 30))) + (disk.internal ? '' : ' · external') + '</small></div>';
  }

  function processRowHTML(proc, canTerminate) {
    const term = canTerminate ? '<button class="text-button" data-proc-term="' + proc.pid + '" data-proc-started="' + proc.started_at + '">TERM</button>' +
      '<button class="text-button danger-text" data-proc-kill="' + proc.pid + '" data-proc-started="' + proc.started_at + '">KILL</button>' : '';
    return '<div class="hostov-proc"><span class="hostov-proc-name">' + esc(proc.name) + '</span>' +
      '<span>' + esc(formatPercent(proc.cpu_percent)) + '</span><span>' + esc(formatPercent(proc.mem_percent)) + '</span>' +
      '<span>' + esc(formatBytes(proc.rss_bytes)) + '</span><span class="hostov-proc-actions">' + term + '</span></div>';
  }

  function availabilityNote(enabled, label) {
    return enabled ? '' : '<p class="hostov-note">' + esc(label) + '</p>';
  }

  function opStatusLabel(op) {
    return op.status + (op.status === 'accepted' || op.status === 'dispatched' ? '…' : '');
  }

  const api = { esc, formatBytes, formatRate, formatTemp, formatPercent, formatDuration, splitGaps, chartSVG, taskRowHTML, tasksHTML, quotaRowHTML, serviceRowHTML, diskRowHTML, processRowHTML, availabilityNote, opStatusLabel };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  root.HostView = api;

  // --- browser wiring -------------------------------------------------------
  if (typeof document === 'undefined' || !document.getElementById || !document.getElementById('view-host')) return;
  if (typeof window === 'undefined') return;

  const $ = id => document.getElementById(id);
  const tr = (key, vars) => (root.I18n ? root.I18n.t(key, vars) : key);
  const CHARTS = {
    cpu: { label: 'hostov.cpu', metric: 'cpu_percent', format: formatPercent, floor: 100 },
    memory: { label: 'hostov.memory', metric: 'memory_percent', format: formatPercent, floor: 100 },
    gpu: { label: 'hostov.gpu', metric: 'gpu_percent', format: formatPercent, floor: 100 },
    pressure: { label: 'hostov.diskRead', metric: 'disk_read_bps', format: formatRate },
    net: { label: 'hostov.network', metrics: ['net_in_bps', 'net_out_bps'], format: formatRate },
    diskio: { label: 'hostov.diskio', metric: 'disk_total_bps', format: formatRate },
    temp: { label: 'hostov.temperature', metrics: ['temp_cpu_c', 'temp_gpu_c'], format: formatTemp },
    fans: { label: 'hostov.fans', metric: 'fans_rpm', format: v => Number.isFinite(Number(v)) ? Math.round(v) + ' RPM' : '—' }
  };
  let snapshot = null, capabilities = null, history = null, currentRange = '15m', pollTimer = null;

  async function request(path, options) {
    const response = await fetch(path, options);
    if (response.status === 401) { window.location.replace('/console-login'); throw new Error(tr('auth.expired')); }
    if (!response.ok) {
      let body = {};
      try { body = await response.json(); } catch {}
      throw new Error(body.error || tr('error.unavailable'));
    }
    return response.json();
  }

  async function action(name, params) {
    return request('/console-api/host/action', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ action: name, params }) });
  }

  function renderStrip() {
    const el = $('hostov-status');
    if (!el) return;
    if (!snapshot || !snapshot.system) { el.textContent = tr('hostov.noData'); return; }
    const sys = snapshot.system;
    const parts = [tr('hostov.sampled', { time: sys.sampled_at ? new Date(sys.sampled_at * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' }) : '—' })];
    if (Number.isFinite(Number(sys.uptime_hours))) parts.push(tr('hostov.uptime', { value: formatDuration(sys.uptime_hours * 3600) }));
    if (sys.thermal_pressure) parts.push(tr('hostov.thermal', { value: sys.thermal_pressure }));
    el.textContent = parts.join(' · ');
    el.classList.toggle('hostov-stale', snapshot.stale === true);
  }

  function renderCharts() {
    const series = (history && history.series) || {};
    const interval = (history && history.interval_seconds) || 2;
    document.querySelectorAll('#hostov-charts [data-chart]').forEach(card => {
      const key = card.dataset.chart, cfg = CHARTS[key];
      const box = card.querySelector('.hostov-chart'), value = card.querySelector('.hostov-chart-value');
      if (!box || !cfg) return;
      const label = tr(cfg.label);
      let points = [];
      if (cfg.metrics) points = (series[cfg.metrics[0]] || []).length >= (series[cfg.metrics[1]] || []).length ? (series[cfg.metrics[0]] || []) : (series[cfg.metrics[1]] || []);
      else points = series[cfg.metric] || [];
      box.innerHTML = chartSVG(points, { label, interval, floor: cfg.floor });
      const last = points[points.length - 1];
      value.textContent = last ? cfg.format(last.v) : '—';
    });
  }

  function renderSnapshot() {
    renderStrip();
    const sys = (snapshot && snapshot.system) || null;
    const strip = $('hostov-strip');
    if (strip) {
      strip.innerHTML = [
        ['CPU', sys ? formatPercent(sys.cpu_percent) : '—'],
        [tr('hostov.memory'), sys ? formatPercent(sys.memory_percent) : '—'],
        [tr('hostov.pressure'), sys ? (sys.memory_pressure_state ? tr('hostov.pressure.' + sys.memory_pressure_state) : formatPercent(sys.memory_pressure_percent)) : '—'],
        [tr('hostov.network'), sys ? formatRate(sys.net_in_bps) + ' ↓ / ' + formatRate(sys.net_out_bps) + ' ↑' : '—'],
        [tr('hostov.diskio'), sys ? formatRate(sys.disk_total_bps) : '—'],
        [tr('hostov.temperature'), sys ? formatTemp(sys.temp_cpu_c) : '—'],
        [tr('hostov.fans'), sys && sys.fans ? sys.fans.map(f => Math.round(f.rpm) + ' RPM').join(' · ') : '—']
      ].map(([k, v]) => '<div class="hostov-kpi"><span>' + esc(k) + '</span><strong>' + esc(v) + '</strong></div>').join('');
    }
    const tasks = $('hostov-tasks');
    if (tasks) {
      const active = Object.values(snapshot.tasks || {}).reduce((n, group) => n + (group.active || 0), 0);
      $('hostov-tasks-count').textContent = tr('hostov.tasksCount', { n: active });
      tasks.innerHTML = tasksHTML(snapshot.tasks);
    }
    const quotas = $('hostov-quotas');
    if (quotas) quotas.innerHTML = (snapshot.quotas || []).map(quotaRowHTML).join('') || '<p class="muted">—</p>';
    const services = $('hostov-services');
    if (services) services.innerHTML = (snapshot.services || []).map(serviceRowHTML).join('') || '<p class="muted">—</p>';
    const disks = $('hostov-disks');
    if (disks) disks.innerHTML = (sys && sys.disks ? sys.disks.map(diskRowHTML).join('') : '<p class="muted">—</p>');
    renderCharts();
    if (!$('hostov-drawers')?.contains(document.activeElement)) renderDrawers();
  }

  function renderDrawers() {
    const caps = capabilities || {};
    const sys = (snapshot && snapshot.system) || {};
    const fans = $('hostov-fans');
    if (fans) {
      const fanCaps = caps.fans || {};
      const enabled = fanCaps.writable === true;
      const current = sys.fan_active_profile || '';
      const mode = sys.fan_control_mode || '';
      fans.innerHTML = availabilityNote(enabled, tr('hostov.fansUnavailable')) +
        '<div class="hostov-fan-state">' + esc(tr('hostov.fanMode', { mode: mode || '—', profile: current || '—' })) +
        (sys.fan_safety_engaged ? ' · ' + esc(tr('hostov.fanSafety')) : '') + '</div>' +
        '<div class="hostov-fan-buttons">' + (fanCaps.profiles || []).map(name =>
          '<button class="secondary-button' + (name === current ? ' selected' : '') + '" data-fan-profile="' + esc(name) + '"' + (enabled ? '' : ' disabled') + '>' + esc(name) + '</button>').join('') +
        '<button class="secondary-button' + (mode === 'appleAuto' ? ' selected' : '') + '" data-fan-auto="1"' + (enabled ? '' : ' disabled') + '>' + esc(tr('hostov.fanAuto')) + '</button></div>' +
        '<div class="hostov-fan-rpm"><label>' + esc(tr('hostov.fanRpm')) + '<input id="hostov-rpm-input" type="number" min="' + esc(sys.fans && sys.fans[0] ? sys.fans[0].min_rpm : 0) + '" max="' + esc(sys.fans && sys.fans[0] ? sys.fans[0].max_rpm : '') + '" step="50"></label>' +
        '<button class="secondary-button" data-fan-rpm="1"' + (enabled ? '' : ' disabled') + '>' + esc(tr('hostov.fanRpmApply')) + '</button></div>';
    }
    const power = $('hostov-power');
    if (power) {
      const pc = caps.power_modes || {};
      const supported = pc.supported || [];
      const current = pc.current || {};
      const enabled = pc.writable === true;
      power.innerHTML = availabilityNote(enabled, tr('hostov.powerUnavailable')) +
        (supported.length ? supported.map(mode =>
          '<label class="check-label"><input type="checkbox" data-power-mode="' + esc(mode) + '"' +
          (current[mode] ? ' checked' : '') + (enabled ? '' : ' disabled') + '><span>' + esc(tr('hostov.powerMode.' + mode)) + '</span></label>').join('')
          : '<p class="muted">' + esc(tr('hostov.powerNone')) + '</p>');
    }
    const caf = $('hostov-caffeinate');
    if (caf) {
      const cc = caps.caffeinate || {};
      const status = (snapshot && snapshot.caffeinate) || { active: false, mode: 'off' };
      const enabled = cc.writable === true;
      caf.innerHTML = availabilityNote(enabled, tr('hostov.caffeinateUnavailable')) +
        '<p class="hostov-fan-state">' + esc(status.active ? tr('hostov.caffeinateActive', { mode: status.mode, until: status.until ? new Date(status.until * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : '—' }) : tr('hostov.caffeinateOff')) + '</p>' +
        '<div class="hostov-fan-buttons"><button class="secondary-button" data-caffeinate="off"' + (enabled ? '' : ' disabled') + '>' + esc(tr('hostov.caffeinateOff')) + '</button>' +
        '<button class="secondary-button" data-caffeinate="timed"' + (enabled ? '' : ' disabled') + '>' + esc(tr('hostov.caffeinateTimed')) + '</button>' +
        '<button class="secondary-button" data-caffeinate="while_tasks"' + (enabled ? '' : ' disabled') + '>' + esc(tr('hostov.caffeinateTasks')) + '</button></div>' +
        '<label class="hostov-inline-label">' + esc(tr('hostov.caffeinateMinutes')) + '<input id="hostov-caffeinate-minutes" type="number" min="1" max="720" value="30"></label>';
    }
    const procs = $('hostov-processes');
    if (procs) {
      procs.innerHTML = '<div class="hostov-proc hostov-proc-head"><span>' + esc(tr('hostov.procName')) + '</span><span>CPU</span><span>' + esc(tr('hostov.memory')) + '</span><span>RSS</span><span></span></div>' +
        ((snapshot && snapshot.processes) || []).map(p => processRowHTML(p, true)).join('') || '<p class="muted">—</p>';
    }
    const svc = $('hostov-svc');
    if (svc) {
      svc.innerHTML = ((snapshot && snapshot.services) || []).map(s =>
        '<div class="hostov-service-row"><span class="hostov-service ' + (s.alive ? 'alive' : 'down') + '"><i></i>' + esc(s.name) + '</span>' +
        '<button class="text-button danger-text" data-service-restart="' + esc(s.name) + '">' + esc(tr('hostov.serviceRestart')) + '</button></div>').join('');
    }
    const ops = $('hostov-powerops');
    if (ops) {
      const pw = caps.power || {};
      const enabled = pw.available === true;
      ops.innerHTML = availabilityNote(enabled, tr('hostov.powerOpsUnavailable')) +
        '<div class="hostov-fan-buttons">' + (pw.operations || []).map(op =>
          '<button class="secondary-button danger-text" data-power-op="' + esc(op) + '"' + (enabled ? '' : ' disabled') + '>' + esc(tr('hostov.powerOp.' + op)) + '</button>').join('') + '</div>' +
        '<div class="hostov-ops">' + ((snapshot && snapshot.operations) || []).map(op =>
          '<div class="hostov-op"><span>' + esc(tr('hostov.powerOp.' + op.op)) + '</span><small>' + esc(new Date(op.requested_at * 1000).toLocaleString()) + '</small><strong class="' + esc(op.status) + '">' + esc(opStatusLabel(op)) + '</strong></div>').join('') + '</div>';
    }
  }

  async function loadHistory() {
    history = await request('/console-api/host/history?range=' + encodeURIComponent(currentRange));
    renderCharts();
  }

  async function load() {
    const [snap, caps] = await Promise.all([
      request('/console-api/host/snapshot'),
      request('/console-api/host/capabilities')
    ]);
    snapshot = snap.snapshot || {};
    capabilities = caps;
    renderSnapshot();
    await loadHistory();

  }

  function startPolling() {
    stopPolling();
    pollTimer = setInterval(() => { if (!document.hidden) load().catch(() => {}); }, 4000);
  }
  function stopPolling() { if (pollTimer) clearInterval(pollTimer); pollTimer = null; }

  document.addEventListener('visibilitychange', () => { if (!document.hidden && $('view-host') && !$('view-host').hidden) load().catch(() => {}); });

  document.querySelector('.hostov-ranges')?.addEventListener('click', event => {
    const button = event.target.closest('[data-range]');
    if (!button) return;
    currentRange = button.dataset.range;
    document.querySelectorAll('.hostov-ranges [data-range]').forEach(b => b.classList.toggle('selected', b === button));
    loadHistory().catch(() => {});
  });

  $('hostov-drawers')?.addEventListener('click', async event => {
    const profile = event.target.closest('[data-fan-profile]');
    const taskLink = event.target.closest('[data-host-task]');
    if (taskLink) { document.querySelector('[data-view=tasks]')?.click(); if(typeof details === 'function') details(taskLink.dataset.hostTask); return; }
    const auto = event.target.closest('[data-fan-auto]');
    const rpm = event.target.closest('[data-fan-rpm]');
    const caffeinate = event.target.closest('[data-caffeinate]');
    const term = event.target.closest('[data-proc-term]');
    const kill = event.target.closest('[data-proc-kill]');
    const restart = event.target.closest('[data-service-restart]');
    const powerOp = event.target.closest('[data-power-op]');
    const showError = message => { const box = $('hostov-error'); if (box) { box.textContent = message; box.hidden = false; } };
    try {
      if (profile) await action('fan.profile', { name: profile.dataset.fanProfile });
      else if (auto) await action('fan.auto', {});
      else if (rpm) await action('fan.rpm', { rpm: Number(($('hostov-rpm-input') || {}).value) });
      else if (caffeinate) {
        const minutes = Number(($('hostov-caffeinate-minutes') || {}).value) || 30;
        await action('caffeinate.set', { mode: caffeinate.dataset.caffeinate, minutes });
      } else if (term) await action('process.terminate', { pid: Number(term.dataset.procTerm), started_at: Number(term.dataset.procStarted), signal: 'term' });
      else if (kill) await confirmProcessEnd(kill, 'kill');
      else if (restart) await confirmServiceRestart(restart.dataset.serviceRestart);
      else if (powerOp) await confirmPowerOp(powerOp.dataset.powerOp);
      else return;
      await load();
    } catch (error) {
      showError(error.message);
    }
  });

  $('hostov-power')?.addEventListener('change', async event => {
    const input = event.target.closest('[data-power-mode]');
    if (!input) return;
    try {
      await action('power-mode.set', { mode: input.dataset.powerMode, enabled: input.checked });
      await load();
    } catch (error) {
      const box = $('hostov-error');
      if (box) { box.textContent = error.message; box.hidden = false; }
      input.checked = !input.checked;
    }
  });

  function confirmProcessEnd(button, signal) {
    const pid = Number(button.dataset.procKill || button.dataset.procTerm);
    const started = Number(button.dataset.procStarted);
    const name = (button.closest('.hostov-proc')?.querySelector('.hostov-proc-name')?.textContent) || String(pid);
    openAction(tr('hostov.procEndTitle'), '<p>' + esc(tr('hostov.procEndHint', { name, signal: signal.toUpperCase() })) + '</p>', async () => {
      await action('process.terminate', { pid, started_at: started, signal });
      await load();
    });
  }

  function confirmServiceRestart(name) {
    openAction(tr('hostov.serviceRestartTitle'), '<p>' + esc(tr('hostov.serviceRestartHint', { name })) + '</p>', async () => {
      await action('service.restart', { name });
      await load();
    });
  }

  function confirmPowerOp(op) {
    const jobs = (snapshot && snapshot.affected_jobs) || [];
    const jobList = jobs.length ? '<ul>' + jobs.map(j => '<li>' + esc(j) + '</li>').join('') + '</ul>' : '<p>' + esc(tr('hostov.powerOpNoJobs')) + '</p>';
    openAction(tr('hostov.powerOp.' + op),
      '<p>' + esc(tr('hostov.powerOpHint', { op: tr('hostov.powerOp.' + op), n: jobs.length })) + '</p>' + jobList +
      '<label>' + esc(tr('hostov.powerOpPassword')) + '<input name="password" type="password" required autocomplete="off"></label>',
      async value => {
        await action('power.request', { op, confirm: true, affected_jobs: jobs, password: value.password });
        await load();
      });
  }

  root.HostView.load = load;
  root.HostView.startPolling = startPolling;
  root.HostView.stopPolling = stopPolling;
})(typeof window !== 'undefined' ? window : globalThis);
