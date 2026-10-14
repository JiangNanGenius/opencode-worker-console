'use strict';
/* Memory view integration with a minimal DOM stub: pagination requests, the
   inspector level tabs and latest-response race protection across two detail
   loads. The stub only implements what the view actually touches. */
const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');

function makeElement(id) {
  const el = {
    id, innerHTML: '', textContent: '', hidden: false, disabled: false, value: '', checked: false,
    dataset: {}, attrs: {}, listeners: {}, classes: new Set()
  };
  el.classList = {
    toggle(name, on) { if (on === undefined) on = !el.classes.has(name); on ? el.classes.add(name) : el.classes.delete(name); },
    add(name) { el.classes.add(name); },
    remove(name) { el.classes.delete(name); },
    contains(name) { return el.classes.has(name); }
  };
  el.focus = () => {};
  el.setAttribute = (key, value) => { el.attrs[key] = value; };
  el.getAttribute = (key) => (key in el.attrs ? el.attrs[key] : null);
  el.addEventListener = (type, fn) => { (el.listeners[type] = el.listeners[type] || []).push(fn); };
  el.dispatch = (type, event) => (el.listeners[type] || []).forEach((fn) => fn(event));
  return el;
}

function setupDom() {
  const elements = {};
  global.document = {
    hidden: false,
    querySelector: () => null,
    getElementById: (id) => elements[id] || (elements[id] = makeElement(id))
  };
  delete global.matchMedia;
  return elements;
}

function withView(browsePayloads, detailQueue, overrides = {}) {
  const elements = setupDom();
  const calls = [];
  const pending = { detail: [] };
  const request = async (urlPath, options) => {
    calls.push(urlPath);
    if (urlPath === '/console-api/memory/status') {
      return { enabled: true, backend: 'openviking', healthy: true, embedding_model: 'doubao-embedding-vision' };
    }
    if (urlPath === '/console-api/memory/projects') {
      return [{ tag: 'opencode_project_x', projectPath: '/tmp/proj', projectName: 'Project X' }];
    }
    if (urlPath === '/console-api/memory/capacity') {
      return { enabled: true, logical: { bytes: 1000, records: 3 }, physical: { bytes: 2000, files: 4 },
               capacity_bytes: 1048576, over_capacity: false, policy: { max_storage_mb: 1024, max_memories: 100000, target_percent: 90 },
               preview: { candidates: 0, candidate_bytes: 0, pinned: 0 }, last: null, last_checked_at: null, next_due_at: null };
    }
    if (urlPath === '/console-api/memory/activity') {
      return { available: true, queue: { pending: 1, running: 0, cancelling: 0, failed: 0, completed: 2, total: 3 },
               recent: [{ id: 't1', status: 'completed', sessionId: 'ses_1', memories: 2, updatedAt: 10 }],
               sessions: { registered: 3 }, indexed: { total_memories: 9 } };
    }
    if (urlPath.startsWith('/console-api/memory/browse')) {
      const page = Number(new URL('http://x' + urlPath.slice(urlPath.indexOf('?'))).searchParams.get('page') || 1);
      return browsePayloads(page, urlPath);
    }
    if (urlPath.startsWith('/console-api/memory/detail')) {
      return new Promise((resolve) => pending.detail.push({ urlPath, resolve }));
    }
    throw new Error('unexpected path ' + urlPath);
  };
  const View = require(path.join(__dirname, '..', 'web', 'memory.js'));
  const controller = View.mount({
    $: (id) => elements[id] || (elements[id] = makeElement(id)),
    request,
    mutate: async () => ({}),
    tr: (key) => key,
    esc: (value) => String(value === null || value === undefined ? '' : value),
    openAction: () => {},
    showToast: () => {},
    notice: () => {},
    number: (value) => String(value),
    formatTime: (value) => String(value || '—'),
    visible: () => true,
    ...overrides
  });
  controller.bind();
  return { elements, calls, pending, controller };
}

const browsePages = (page) => ({
  items: page === 2
    ? [{ id: 'viking://user/default/memories/experiences/c.md', uri: 'viking://user/default/memories/experiences/c.md',
        preview: 'third record', updatedAt: 3, memoryType: 'experiences' }]
    : [{ id: 'viking://user/default/memories/experiences/a.md', uri: 'viking://user/default/memories/experiences/a.md',
        preview: 'first record', updatedAt: 2, memoryType: 'experiences' },
       { id: 'viking://user/default/memories/experiences/b.md', uri: 'viking://user/default/memories/experiences/b.md',
        preview: 'second record', updatedAt: 1, memoryType: 'experiences' }],
  total: 3, page, pageSize: 2, totalPages: 2, totalRecords: 3,
  categories: [{ name: 'experiences', count: 3 }], directories: [], projects: [{ tag: 'opencode_project_x', count: 3, name: 'Project X' }],
  scope: null
});

const flush = async (rounds = 8) => { for (let i = 0; i < rounds; i++) await new Promise((r) => setImmediate(r)); };

test('summary shows fetched totals and capacity immediately, including an empty refresh', async () => {
  let payload = browsePages(1);
  const view = withView(() => payload, []);
  await view.controller.load();
  assert.match(view.elements['memory-strip'].innerHTML, /<strong>3<\/strong> memory.total/);
  assert.match(view.elements['memory-strip'].innerHTML, /value="1000" max="1048576"/);
  payload = { ...payload, items: [], total: 0, totalRecords: 0, totalPages: 1 };
  await view.controller.load();
  assert.match(view.elements['memory-strip'].innerHTML, /<strong>0<\/strong> memory.total/);
});

test('memory view loads status, rows and facets, then paginates', async (t) => {
  const view = withView(browsePages, []);
  t.after(() => view.controller.stop());
  view.controller.start();
  await flush();
  const rows = view.elements['memory-list'].innerHTML;
  assert.match(rows, /first record/);
  assert.match(rows, /second record/);
  assert.match(view.elements['memory-nav'].innerHTML, /memory.navCategories/);
  assert.match(view.elements['memory-strip'].innerHTML, /memory.ready/);
  assert.equal(view.elements['memory-status'].textContent, 'memory.stripReady');
  assert.match(view.elements['memory-count'].innerHTML + view.elements['memory-count'].textContent, /memory.range/);
  assert.equal(view.elements['memory-pager'].innerHTML.includes('data-memory-page="2"'), true);
  const click = (target) => view.elements['memory-pager'].dispatch('click', { target });
  click({ closest: (selector) => (selector === '[data-memory-page]' ? { dataset: { memoryPage: '2' }, disabled: false } : null) });
  await flush();
  assert.match(view.elements['memory-list'].innerHTML, /third record/);
  assert.ok(view.calls.some((call) => call.includes('page=2')));
  view.controller.stop();
});

test('selecting a row opens the inspector and level tabs switch content', async (t) => {
  const view = withView(browsePages, []);
  t.after(() => view.controller.stop());
  view.controller.start();
  await flush();
  const uri = 'viking://user/default/memories/experiences/a.md';
  view.elements['memory-list'].dispatch('click', {
    target: { closest: (selector) => (selector === '[data-memory-open]' ? { dataset: { memoryOpen: uri } } : null) }
  });
  await flush();
  assert.equal(view.pending.detail.length, 1);
  view.pending.detail[0].resolve({
    id: uri, uri, kind: 'file', origin: 'openviking', content: 'full body', displayName: 'a.md',
    project: { tag: 'opencode_project_x', name: 'Project X' }, category: 'experiences',
    levels: { l0: { available: false, reason: 'not ready' }, l1: { available: true, text: 'Overview text', uri: 'viking://user/default/memories/experiences' },
              l2: { available: true, chars: 9 } }
  });
  await flush();
  assert.equal(view.elements['memory-layout'].classes.has('inspector-open'), true);
  assert.match(view.elements['memory-inspector-body'].innerHTML, /full body/);
  // The L1 tab shows the real native overview, not an invented summary.
  view.elements['memory-inspector'].dispatch('click', {
    target: { closest: (selector) => (selector === '[data-memory-tab]' ? { dataset: { memoryTab: 'l1' } } : null) }
  });
  assert.match(view.elements['memory-inspector-body'].innerHTML, /Overview text/);
  // The L0 tab reports the explicit absent state.
  view.elements['memory-inspector'].dispatch('click', {
    target: { closest: (selector) => (selector === '[data-memory-tab]' ? { dataset: { memoryTab: 'l0' } } : null) }
  });
  assert.match(view.elements['memory-inspector-body'].innerHTML, /memory.level.notReady/);
  view.controller.stop();
});

test('a superseded detail response never overwrites the latest selection', async (t) => {
  const view = withView(browsePages, []);
  t.after(() => view.controller.stop());
  view.controller.start();
  await flush();
  const first = 'viking://user/default/memories/experiences/a.md';
  const second = 'viking://user/default/memories/experiences/b.md';
  view.controller.select(first);
  await flush(4);
  view.controller.select(second);
  await flush(4);
  assert.equal(view.pending.detail.length, 2);
  // The first (older) request answers last and must be discarded.
  view.pending.detail[1].resolve({
    id: second, uri: second, kind: 'file', content: 'newer body', displayName: 'b.md',
    levels: { l0: { available: false }, l1: { available: false }, l2: { available: true, chars: 10 } }
  });
  await flush();
  view.pending.detail[0].resolve({
    id: first, uri: first, kind: 'file', content: 'stale body', displayName: 'a.md',
    levels: { l0: { available: false }, l1: { available: false }, l2: { available: true, chars: 10 } }
  });
  await flush();
  const html = view.elements['memory-inspector-body'].innerHTML;
  assert.match(html, /newer body/);
  assert.ok(!html.includes('stale body'));
  view.controller.stop();
});

test('semantic search sends q and renders results without pagination', async (t) => {
  const view = withView((page, urlPath) => {
    if (urlPath.includes('q=needle')) {
      return { items: [{ id: 'x', uri: 'x', preview: 'semantic hit' }], total: 1, page: 1, pageSize: 25,
               totalPages: 1, semantic: true, categories: [], directories: [], projects: [] };
    }
    return browsePages(page);
  }, []);
  t.after(() => view.controller.stop());
  view.controller.start();
  await flush();
  view.elements['memory-search'].value = 'needle';
  view.elements['memory-search'].dispatch('change', {});
  await flush();
  assert.ok(view.calls.some((call) => call.includes('q=needle')));
  assert.match(view.elements['memory-list'].innerHTML, /semantic hit/);
  assert.match(view.elements['memory-count'].textContent, /memory.semanticCount/);
  assert.equal(view.elements['memory-pager'].innerHTML, '');
  view.controller.stop();
});


test('locale refresh translates open detail without losing selection or level or fetching again', async () => {
  let locale = 'zh';
  const view = withView(browsePages, [], { tr: key => locale + ':' + key });
  await view.controller.load();
  const uri = 'viking://user/default/memories/experiences/a.md';
  const loading = view.controller.select(uri);
  await flush();
  view.pending.detail[0].resolve({ id: uri, uri, kind: 'file', content: 'full body',
    levels: { l0: { available: false, reason: 'not ready' }, l1: { available: false } } });
  await loading;
  view.elements['memory-inspector'].dispatch('click', {
    target: { closest: selector => selector === '[data-memory-tab]' ? { dataset: { memoryTab: 'l0' } } : null }
  });
  const requests = view.calls.length;
  locale = 'en';
  view.controller.refreshLocale();
  const html = view.elements['memory-inspector-body'].innerHTML;
  assert.match(html, /en:action.edit/);
  assert.match(html, /en:memory.provenance/);
  assert.match(html, /en:memory.level.notReady/);
  assert.doesNotMatch(html, /zh:/);
  assert.match(view.elements['memory-count'].textContent, /en:memory.range/);
  assert.match(view.elements['memory-pager'].innerHTML, /en:memory.pagerNext/);
  assert.equal(view.controller.state.selected, uri);
  assert.equal(view.controller.state.tab, 'l0');
  assert.equal(view.calls.length, requests);
});

for (const deleted of [0, 2]) test('maintenance failure remains visible with ' + deleted + ' successful deletions', async () => {
  const messages = [];
  let submit;
  const result = { enabled: true, deleted, failed: 3, errors: [{ error: 'permission denied' }] };
  const view = withView(browsePages, [], {
    mutate: async () => result,
    openAction: (title, html, callback) => { submit = callback; },
    showToast: (...args) => messages.push(args),
    tr: (key, values) => key + (values ? JSON.stringify(values) : '')
  });
  await view.controller.load();
  view.elements['memory-capacity-body'].dispatch('click', {
    target: { closest: selector => selector === '#memory-maintenance-run' ? {} : null }
  });
  await submit();
  assert.equal(messages[0][2], 'error');
  assert.equal(messages[0][3], true);
  assert.match(messages[0][1], /maintenancePartial/);
  assert.match(messages[0][1], /permission denied/);
  assert.match(messages[0][1], new RegExp('"deleted":"' + deleted + '"'));
  view.controller.state.capacity.last = result;
  view.controller.refreshLocale();
  assert.match(view.elements['memory-capacity-body'].innerHTML, /maintenancePartial/);
  assert.match(view.elements['memory-capacity-body'].innerHTML, /permission denied/);
});

for (const saved of ['opencode_project_x', 'removed-project']) test('restores only an existing remembered project: ' + saved, async t => {
  global.localStorage = { getItem: () => saved, setItem: () => {} };
  t.after(() => { delete global.localStorage; });
  const view = withView(browsePages, []);
  await view.controller.load();
  assert.equal(view.controller.state.project, saved === 'opencode_project_x' ? saved : '');
  const browse = view.calls.find(path => path.startsWith('/console-api/memory/browse'));
  assert.equal(browse.includes('project=opencode_project_x'), saved === 'opencode_project_x');
  assert.equal(view.pending.detail.length, 0);
  const nav = view.elements['memory-nav'].innerHTML;
  assert.ok(nav.indexOf('memory.navProjects') < nav.indexOf('memory.navCategories'));
});

test('changing projects remembers scope and discards the old in-flight inspector', async t => {
  const saved = [];
  global.localStorage = { getItem: () => '', setItem: (key, value) => saved.push(value) };
  t.after(() => { delete global.localStorage; });
  const view = withView(browsePages, []);
  await view.controller.load();
  const loading = view.controller.select('viking://user/default/memories/experiences/a.md');
  await flush();
  view.elements['memory-nav'].dispatch('click', {
    target: { closest: selector => selector === '[data-nav-kind]' ? { dataset: { navKind: 'project', navValue: 'opencode_project_x' } } : null }
  });
  view.pending.detail[0].resolve({ kind: 'file', content: 'old project detail' });
  await loading;
  await flush();
  assert.equal(view.controller.state.project, 'opencode_project_x');
  assert.equal(view.controller.state.selected, null);
  assert.equal(view.controller.state.detail, null);
  assert.equal(view.elements['memory-layout'].classes.has('inspector-open'), false);
  assert.deepEqual(saved, ['opencode_project_x']);
  assert.doesNotMatch(view.elements['memory-inspector-body'].innerHTML, /old project detail/);
});
