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
    const models = Array.isArray(usage.by_model) && usage.by_model.length > 1
      ? `<div class="usage-models"><h4>${esc(tr('usage.byModel'))}</h4>${usage.by_model.map(item=>`<div><span>${esc(item.model||tr('usage.unknownModel'))}</span><strong>${numeric(item.total)?esc(number(item.total)):'—'}</strong></div>`).join('')}</div>` : '';
    const partial = usage.complete === false ? `<p class="muted">${esc(tr('usage.partial'))}</p>` : '';
    return `<section class="task-usage" aria-label="${esc(tr('usage.heading'))}"><h3>${esc(tr('usage.heading'))}</h3><dl class="usage-breakdown">${values}</dl>${models}${partial}<p class="muted">${esc(tr('usage.note'))}</p></section>`;
  }
  function activityHTML(activity, { tr, esc, clock, taskId = '', fullMessages = new Map(), expandedMessages = new Set() }) {
    activity = activity || {};
    const events = Array.isArray(activity.events) ? activity.events.slice().reverse() : [];
    const rows = events.map(event => {
      const labelKeys = {assistant:'activity.message',user:'activity.user',guidance:'activity.guidance',status:'activity.state'};
      const label = labelKeys[event.label] ? tr(labelKeys[event.label]) : (event.label || tr('activity.' + (event.type === 'tool' ? 'tool' : 'message')));
      const state = ['pending', 'running', 'completed', 'error'].includes(event.status) ? event.status : 'info';
      const eventId=event.id||'',messageKey=taskId+':'+eventId,full=fullMessages.get(messageKey),expandable=event.truncated===true||typeof full==='string',expanded=expandable&&expandedMessages.has(messageKey),text=typeof full==='string'?full:event.text;
      const message=text?`<div class="activity-message ${expanded?'expanded':'collapsed'}" data-message-key="${esc(messageKey)}"><pre>${esc(text)}</pre>${expandable?`<button type="button" class="activity-expand" data-expand-activity="${esc(eventId)}" aria-expanded="${expanded}">${esc(tr(expanded?'activity.collapse':'activity.expand'))}</button>`:''}</div>`:'';
      return `<li data-event-id="${esc(eventId)}"><div class="activity-event-heading"><span class="activity-event-state ${state}">${esc(tr('activity.' + state))}</span><strong>${esc(label)}</strong><time>${esc(clock(event.time))}</time></div>${message}</li>`;
    }).join('');
    const source = activity.source === 'live' ? 'activity.live' : 'activity.saved';
    return `<section class="task-activity" aria-label="${esc(tr('activity.heading'))}"><div class="activity-heading"><h3>${esc(tr('activity.heading'))}</h3><span class="muted">${esc(tr(source))}${activity.sampled_at ? ' · ' + esc(clock(activity.sampled_at)) : ''}</span></div>${activity.stale ? `<p class="activity-warning" role="status">${esc(tr('activity.stale'))}${activity.error ? ' ' + esc(activity.error) : ''}</p>` : ''}<div class="activity-scroll" tabindex="0" role="region" aria-label="${esc(tr('activity.heading'))}">${rows ? `<ol class="activity-events">${rows}</ol>` : `<p class="muted activity-empty">${esc(tr('activity.empty'))}</p>`}</div>${activity.has_more ? `<p class="muted">${esc(tr('activity.recentOnly'))}</p>` : ''}</section>`;
  }
  function createController({ request, changed, visible = () => true, timeoutMs = 8000,
    translate = key => key }) {
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
      const timeout = setTimeout(() => abort.abort(), timeoutMs);
      activeRequest = abort; pending = true; notify();
      try {
        const detail = await request('/console-api/task/' + encodeURIComponent(id), { signal: abort.signal });
        if (version !== generation || selected !== id) return;
        cache.set(id, detail); error = null;
      } catch (failure) {
        if (version !== generation || selected !== id) return;
        error = failure.name === 'AbortError' ? translate('detail.slow') :
          (failure.message || String(failure));
      } finally {
        clearTimeout(timeout);
        if (version === generation && selected === id) {
          pending = false; activeRequest = null; notify();
        }
      }
    }
    function open(id, initial = null) {
      if (selected === id) { close(); return; }
      generation++; activeRequest?.abort(); activeRequest = null;
      selected = id; pending = false; error = null;
      if (initial && initial.task?.id === id) cache.set(id, initial);
      notify(); refresh(true);
    }
    return { open, close, refresh, state, cache };
  }
  const api = { numeric, usageOf, aggregate, usageHTML, activityHTML, createController };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  root.TaskView = api;
})(typeof window !== 'undefined' ? window : globalThis);
