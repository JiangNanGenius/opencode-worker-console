/* Task display helpers and a single-selection, visibility-aware detail poller. */
(function (root) {
  'use strict';
  const numeric = value => typeof value === 'number' && Number.isFinite(value) && value >= 0;
  const fields = ['total', 'input', 'output', 'reasoning', 'cache_read', 'cache_write'];
  function usageOf(task) {
    if (task.usage) return task.usage;
    const tokens = task.tokens || {};
    return { total: numeric(tokens.total) ? tokens.total : null,
      input: tokens.input, output: tokens.output, reasoning: tokens.reasoning,
      cache_read: tokens.cache?.read, cache_write: tokens.cache?.write, source: 'saved' };
  }
  function aggregate(tasks, overrides = new Map()) {
    const result = { total: null, known: 0, count: tasks.length };
    for (const task of tasks) {
      const usage = overrides.get(task.id)?.usage || usageOf(task);
      if (numeric(usage.total)) { result.total = (result.total || 0) + usage.total; result.known++; }
    }
    return result;
  }
  function usageHTML(usage, { tr, esc, number }) {
    usage = usage || {};
    const values = fields.map(key => `<div><dt>${esc(tr('usage.' + key))}</dt><dd>${numeric(usage[key]) ? esc(number(usage[key])) : '—'}</dd></div>`).join('');
    const partial = usage.complete === false ? `<p class="muted">${esc(tr('usage.partial'))}</p>` : '';
    return `<section class="task-usage" aria-label="${esc(tr('usage.heading'))}"><h3>${esc(tr('usage.heading'))}</h3><dl class="usage-breakdown">${values}</dl>${partial}<p class="muted">${esc(tr('usage.note'))}</p></section>`;
  }
  function activityHTML(activity, { tr, esc, clock }) {
    activity = activity || {};
    const events = Array.isArray(activity.events) ? activity.events : [];
    const rows = events.map(event => {
      const labelKeys = {assistant:'activity.message',user:'activity.user',guidance:'activity.guidance',status:'activity.state'};
      const label = labelKeys[event.label] ? tr(labelKeys[event.label]) : (event.label || tr('activity.' + (event.type === 'tool' ? 'tool' : 'message')));
      const state = ['pending', 'running', 'completed', 'error'].includes(event.status) ? event.status : 'info';
      return `<li data-event-id="${esc(event.id || '')}"><div class="activity-event-heading"><span class="activity-event-state ${state}">${esc(tr('activity.' + state))}</span><strong>${esc(label)}</strong><time>${esc(clock(event.time))}</time></div>${event.text ? `<pre>${esc(event.text)}</pre>` : ''}</li>`;
    }).join('');
    const source = activity.source === 'live' ? 'activity.live' : 'activity.saved';
    return `<section class="task-activity" aria-label="${esc(tr('activity.heading'))}"><div class="activity-heading"><h3>${esc(tr('activity.heading'))}</h3><span class="muted">${esc(tr(source))}${activity.sampled_at ? ' · ' + esc(clock(activity.sampled_at)) : ''}</span></div>${activity.stale ? `<p class="activity-warning" role="status">${esc(tr('activity.stale'))}${activity.error ? ' ' + esc(activity.error) : ''}</p>` : ''}<div class="activity-scroll" tabindex="0" role="region" aria-label="${esc(tr('activity.heading'))}">${rows ? `<ol class="activity-events">${rows}</ol>` : `<p class="muted activity-empty">${esc(tr('activity.empty'))}</p>`}</div>${activity.has_more ? `<p class="muted">${esc(tr('activity.recentOnly'))}</p>` : ''}</section>`;
  }
  function createController({ request, changed, visible = () => true }) {
    let selected = null, generation = 0, activeRequest = null;
    const cache = new Map();
    let error = null, pending = false;
    function state() { return { selected, detail: cache.get(selected) || null, error, pending }; }
    function notify() { changed(state()); }
    function close() {
      generation++; activeRequest?.abort(); activeRequest = null;
      selected = null; pending = false; error = null; notify();
    }
    async function refresh(force = false) {
      if (!selected || pending || !visible()) return;
      const terminal = ['completed', 'failed', 'cancelled', 'needs_attention', 'timed_out'];
      if (!force && !error && terminal.includes(cache.get(selected)?.task?.status)) return;
      const id = selected, version = generation, abort = new AbortController();
      activeRequest = abort; pending = true; notify();
      try {
        const detail = await request('/console-api/task/' + encodeURIComponent(id), { signal: abort.signal });
        if (version !== generation || selected !== id) return;
        cache.set(id, detail); error = null;
      } catch (failure) {
        if (version !== generation || selected !== id || failure.name === 'AbortError') return;
        error = failure.message || String(failure);
      } finally {
        if (version === generation && selected === id) {
          pending = false; activeRequest = null; notify();
        }
      }
    }
    function open(id) {
      if (selected === id) { close(); return; }
      generation++; activeRequest?.abort(); activeRequest = null;
      selected = id; pending = false; error = null; notify(); refresh(true);
    }
    return { open, close, refresh, state, cache };
  }
  const api = { numeric, usageOf, aggregate, usageHTML, activityHTML, createController };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  root.TaskView = api;
})(typeof window !== 'undefined' ? window : globalThis);
