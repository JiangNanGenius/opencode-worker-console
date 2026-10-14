/* Project memory view: navigation, paginated rows, progressive inspector and
   truthful capacity/ingestion state. Pure state helpers are exported for node
   tests; the browser controller binds only when mounted from manage.js. */
(function (root) {
  'use strict';

  var PREVIEW_MAX = 360;
  var PAGE_SIZES = [25, 50, 100];
  var SORTS = ['newest', 'oldest', 'name', 'size'];

  function esc(value) {
    return String(value === null || value === undefined ? '' : value)
      .replace(/[&<>"']/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]; });
  }

  function numberValue(value) {
    return value !== null && value !== undefined && value !== '' && typeof value !== 'boolean' && Number.isFinite(Number(value));
  }

  function formatBytes(value) {
    if (!numberValue(value)) return '—';
    var n = Number(value);
    if (n >= 1 << 30) return (n / (1 << 30) >= 100 ? Math.round(n / (1 << 30)) : (n / (1 << 30)).toFixed(2)) + ' GB';
    if (n >= 1 << 20) return (n / (1 << 20) >= 100 ? Math.round(n / (1 << 20)) : (n / (1 << 20)).toFixed(1)) + ' MB';
    if (n >= 1 << 10) return Math.round(n / (1 << 10)) + ' KB';
    return Math.round(n) + ' B';
  }

  function pageCount(total, pageSize) {
    var size = Math.max(1, Math.round(Number(pageSize) || 25));
    var count = Math.max(0, Math.round(Number(total) || 0));
    return Math.max(1, Math.ceil(count / size));
  }

  function clampPage(page, totalPages) {
    var value = Math.round(Number(page) || 1);
    return Math.min(Math.max(1, value), Math.max(1, Math.round(Number(totalPages) || 1)));
  }

  /* Latest-response race protection: every request gets a sequence number and
     a response is only applied when it is still the latest of its kind. */
  function createState() {
    return { pageSeq: 0, detailSeq: 0, listSeq: 0 };
  }

  function beginRequest(state, kind) {
    state[kind + 'Seq'] = (state[kind + 'Seq'] || 0) + 1;
    return state[kind + 'Seq'];
  }

  function acceptResult(state, kind, seq, result) {
    return state[kind + 'Seq'] === seq ? result : null;
  }

  function levelOf(detail, level) {
    var levels = (detail && detail.levels) || {};
    var entry = levels[level] || null;
    if (level === 'l2') {
      if (entry && entry.available) return { state: 'available', text: (detail && detail.content) || '', chars: entry.chars };
      if (entry && entry.reason === 'directory') return { state: 'directory' };
      return { state: 'pending' };
    }
    if (!entry) return { state: 'pending' };
    if (entry.available && typeof entry.text === 'string' && entry.text.trim()) {
      return { state: 'available', text: entry.text, uri: entry.uri || '', aggregate: entry.aggregate === true };
    }
    if (entry.reason === 'not ready') return { state: 'notReady' };
    if (entry.reason === 'directory') return { state: 'directory' };
    if (entry.error) return { state: 'error', error: entry.error };
    return { state: 'missing' };
  }

  function previewOf(item, max) {
    if (!item) return '';
    var text = String(item.preview || '').split(/\s+/).join(' ').trim();
    if (!text && item.content) text = String(item.content).split(/\s+/).join(' ').trim();
    return text.replace(/(?:^|\s)#{1,6}\s*(?:Request|Outcome|Description)\b/g, ' ').trim().slice(0, max || PREVIEW_MAX);
  }

  function titleOf(detail) {
    var lines = String((detail && detail.content) || '').split('\n');
    for (var i = 0; i < lines.length; i++) {
      var line = lines[i].replace(/^\s*#{1,6}\s*/, '').trim();
      if (line && !/^(?:Request|Outcome|Description)$/i.test(line)) return line.slice(0, 120);
    }
    return String((detail && (detail.displayName || detail.uri)) || '').split('/').pop();
  }

  /* Only a server-verified session relation becomes a link, and the target must
     stay a same-origin path; a stale migrated id stays plain text. */
  function sessionURL(detail) {
    var session = detail && detail.session;
    if (!session || session.verified !== true) return null;
    var url = session.url;
    if (typeof url !== 'string' || url.charAt(0) !== '/' || url.charAt(1) === '/' || /[\\\s]/.test(url)) return null;
    return url;
  }

  function folderLabel(uri) {
    if (typeof uri !== 'string') return '';
    var marker = '/memories/';
    var index = uri.indexOf(marker);
    return index === -1 ? uri : '~/' + uri.slice(index + marker.length);
  }

  function capacityState(capacity) {
    if (!capacity) return { state: 'pending' };
    var logical = capacity.logical ? capacity.logical.bytes : null;
    var capacityBytes = capacity.capacity_bytes || null;
    var ratio = numberValue(logical) && numberValue(capacityBytes) && Number(capacityBytes) > 0
      ? Number(logical) / Number(capacityBytes) : null;
    return {
      state: 'ready',
      over: capacity.over_capacity === true,
      logicalBytes: logical,
      capacityBytes: capacityBytes,
      ratio: ratio,
      candidates: capacity.preview ? capacity.preview.candidates : null,
      candidateBytes: capacity.preview ? capacity.preview.candidate_bytes : null
    };
  }

  function queueTone(queue) {
    if (!queue) return 'unknown';
    if (Number(queue.failed) > 0) return 'warning';
    if (Number(queue.pending) > 0 || Number(queue.running) > 0) return 'busy';
    return 'idle';
  }

  var api = {
    esc: esc,
    formatBytes: formatBytes,
    pageCount: pageCount,
    clampPage: clampPage,
    createState: createState,
    beginRequest: beginRequest,
    acceptResult: acceptResult,
    levelOf: levelOf,
    previewOf: previewOf,
    titleOf: titleOf,
    sessionURL: sessionURL,
    folderLabel: folderLabel,
    capacityState: capacityState,
    queueTone: queueTone,
    PAGE_SIZES: PAGE_SIZES,
    SORTS: SORTS
  };

  /* ---------------------------------------------------------------- mount */

  function mount(options) {
    var $ = options.$;
    var request = options.request;
    var mutate = options.mutate;
    var tr = options.tr;
    var openAction = options.openAction;
    var showToast = options.showToast;
    var notice = options.notice;
    var visible = options.visible || function () { return true; };
    var formatTime = options.formatTime || function (value) { return value || '—'; };
    var number = options.number || function (value) { return String(value); };

    var state = {
      category: '', folder: '', project: '', page: 1, pageSize: 25, sort: 'newest', query: '',
      items: [], total: 0, totalPages: 1, totalRecords: 0,
      categories: [], directories: [], projects: [], navProjects: [],
      selected: null, detail: null, tab: 'l2', legacy: false,
      backend: '', ready: false, error: '', loaded: false,
      status: null, capacity: null, activity: null,
      pageSeq: 0, detailSeq: 0, listSeq: 0, statusSeq: 0, capacitySeq: 0, activitySeq: 0
    };
    var selectedIds = new Set();
    var pollTimer = null;
    var controller = {};

    function narrow() {
      return typeof matchMedia === 'function' && !matchMedia('(min-width: 1180px)').matches;
    }

    function memoryRequest(path, timeoutMs) {
      var timer = null;
      var guard;
      if (typeof AbortController === 'function') {
        guard = new AbortController();
        timer = setTimeout(function () { guard.abort(); }, timeoutMs || 20000);
      }
      return request(path, guard ? { signal: guard.signal } : undefined).finally(function () {
        if (timer) clearTimeout(timer);
      });
    }

    function browseQuery(overrides) {
      var params = new URLSearchParams();
      var scoped = overrides || {};
      if (scoped.q || state.query) params.set('q', scoped.q !== undefined ? scoped.q : state.query);
      if (state.category) params.set('category', state.category);
      if (state.folder) params.set('folder', state.folder);
      if (state.project) params.set('project', state.project);
      params.set('page', String(scoped.page || state.page));
      params.set('pageSize', String(state.pageSize));
      params.set('sort', state.sort);
      return params.toString();
    }

    function applyBrowse(result) {
      state.items = Array.isArray(result.items) ? result.items : [];
      state.total = Number(result.total) || 0;
      state.page = Number(result.page) || 1;
      state.pageSize = Number(result.pageSize) || state.pageSize;
      state.totalPages = Number(result.totalPages) || pageCount(state.total, state.pageSize);
      state.page = clampPage(state.page, state.totalPages);
      if (numberValue(result.totalRecords)) state.totalRecords = Number(result.totalRecords);
      state.categories = result.categories || [];
      state.directories = result.directories || [];
      state.projects = result.projects || [];
      state.scope = result.scope || null;
      state.legacy = result.legacy === true || !state.backend || state.backend !== 'openviking';
      state.error = '';
      state.loaded = true;
      var known = new Set(state.items.map(function (item) { return item.id || item.uri; }));
      selectedIds = new Set(Array.from(selectedIds).filter(function (id) { return known.has(id); }));
    }

    async function loadBrowse(page) {
      if (page) state.page = page;
      var seq = api.beginRequest(state, 'page');
      renderList({ loading: true });
      try {
        var result = await memoryRequest('/console-api/memory/browse?' + browseQuery());
        if (!api.acceptResult(state, 'page', seq, result)) return;
        applyBrowse(result);
        renderList({});
        renderNav();
        renderCount();
        renderPager();
        renderStrip();
      } catch (error) {
        if (!api.acceptResult(state, 'page', seq, {})) return;
        state.error = error.message || String(error);
        renderList({ error: state.error });
      }
    }

    async function loadDetail(uri) {
      if (!uri || state.legacy) return;
      var seq = api.beginRequest(state, 'detail');
      state.selected = uri;
      state.lastSelected = uri;
      state.detail = null;
      renderList({});
      renderInspector({ loading: true });
      updateLayout();
      try {
        var params = new URLSearchParams({ id: uri });
        if (state.project) params.set('project', state.project);
        var detail = await memoryRequest('/console-api/memory/detail?' + params.toString());
        if (!api.acceptResult(state, 'detail', seq, detail)) return;
        state.detail = detail;
        if (detail.kind === 'directory' && state.tab === 'l2') state.tab = 'l0';
        renderInspector({});
        focusInspector();
      } catch (error) {
        if (!api.acceptResult(state, 'detail', seq, {})) return;
        renderInspector({ error: error.message || String(error) });
      }
    }

    async function loadStatus() {
      var seq = api.beginRequest(state, 'list');
      try {
        var status = await memoryRequest('/console-api/memory/status');
        if (!api.acceptResult(state, 'list', seq, status)) return;
        state.status = status;
        state.backend = status.backend || '';
        state.ready = status.healthy === true;
        state.legacy = state.backend !== 'openviking';
        renderStrip();
        applyCapabilities();
      } catch (error) {
        if (!api.acceptResult(state, 'list', seq, {})) return;
        state.status = { enabled: true, healthy: false, error: error.message || String(error) };
        state.ready = false;
        renderStrip();
      }
    }

    async function loadProjectsForAdd() {
      try {
        var projects = await memoryRequest('/console-api/memory/projects');
        state.navProjects = Array.isArray(projects) ? projects : [];
      } catch (_error) { /* adding stays disabled with a clear message */ }
    }

    async function loadCapacity() {
      if (state.legacy) return;
      var seq = api.beginRequest(state, 'capacity');
      try {
        var capacity = await memoryRequest('/console-api/memory/capacity');
        if (!api.acceptResult(state, 'capacity', seq, capacity)) return;
        state.capacity = capacity;
        renderCapacity();
        renderStrip();
      } catch (error) {
        if (!api.acceptResult(state, 'capacity', seq, {})) return;
        state.capacity = { error: error.message || String(error) };
        renderCapacity();
        renderStrip();
      }
    }

    async function loadActivity() {
      if (state.legacy) return;
      var seq = api.beginRequest(state, 'activity');
      try {
        var activity = await memoryRequest('/console-api/memory/activity');
        if (!api.acceptResult(state, 'activity', seq, activity)) return;
        state.activity = activity;
        renderActivity();
        renderStrip();
      } catch (error) {
        if (!api.acceptResult(state, 'activity', seq, {})) return;
        state.activity = { available: false, error: error.message || String(error) };
        renderActivity();
        renderStrip();
      }
    }

    function applyCapabilities() {
      var native = state.backend === 'openviking';
      var nav = $('memory-nav');
      if (nav) nav.hidden = !native;
      var capacity = $('memory-capacity');
      if (capacity) capacity.hidden = !native;
      var activity = $('memory-activity');
      if (activity) activity.hidden = !native;
      var sort = $('memory-sort');
      if (sort) sort.disabled = !native;
      var inspector = $('memory-inspector');
      if (inspector && !native) {
        state.selected = null;
        state.detail = null;
        inspector.hidden = true;
      }
      updateLayout();
    }

    /* --------------------------------------------------------- rendering */

    function renderStrip() {
      var status = state.status || {};
      var native = state.backend === 'openviking';
      var queue = (state.activity && state.activity.queue) || null;
      var logical = state.capacity && state.capacity.logical ? state.capacity.logical : null;
      var capacityBytes = state.capacity && state.capacity.capacity_bytes;
      var pieces = [];
      var tone = status.healthy ? 'ok' : (status.enabled === false ? 'off' : 'down');
      pieces.push('<span class="memory-strip-state ' + tone + '"><i></i>' + esc(status.healthy
        ? tr('memory.ready', { model: status.embedding_model || '—' })
        : status.enabled === false ? tr('memory.disabled')
        : (status.error ? tr('memory.unavailableDetail', { error: status.error }) : tr('memory.unavailable'))) + '</span>');
      if (native) {
        pieces.push('<span><strong>' + esc(state.loaded ? number(state.totalRecords) : '—') + '</strong> ' + esc(tr('memory.total')) + '</span>');
        pieces.push('<span><strong>' + esc(queue ? number(queue.pending + queue.running) : '—') + '</strong> ' + esc(tr('memory.stripQueue')) + '</span>');
        pieces.push('<span><strong>' + esc(logical ? formatBytes(logical.bytes) : '—') + '</strong> ' + esc(tr('memory.stripLogical')) + '</span>');
        pieces.push('<span><strong>' + esc(numberValue(capacityBytes) ? formatBytes(capacityBytes) : '—') + '</strong> ' + esc(tr('memory.stripCapacity')) + '</span>');
        if (logical && numberValue(logical.bytes) && numberValue(capacityBytes) && capacityBytes > 0) {
          pieces.push('<progress class="memory-budget-meter" value="' + Math.max(0, Number(logical.bytes)) + '" max="' + Number(capacityBytes) + '" aria-label="' + esc(tr('memory.stripLogical')) + '"></progress>');
        }
      }
      var strip = $('memory-strip');
      if (strip) strip.innerHTML = pieces.join('');
      var label = $('memory-status');
      if (label) label.textContent = status.healthy ? tr('memory.stripReady') : tr('memory.stripAttention');
    }

    function facetCount(entry) {
      return numberValue(entry.count) ? number(entry.count) : '';
    }

    function navItem(kind, active, attrs, label, count) {
      return '<button type="button" class="memory-nav-item' + (active ? ' selected' : '') + '" data-nav-kind="' + kind + '" ' + attrs + '>'
        + '<span class="memory-nav-label">' + esc(label) + '</span>'
        + (count === '' ? '' : '<span class="memory-nav-count">' + esc(count) + '</span>') + '</button>';
    }

    function renderNav() {
      var nav = $('memory-nav');
      if (!nav || state.legacy) return;
      var html = '<div class="memory-nav-group">';
      html += navItem('all', !state.category && !state.folder && !state.project, 'data-nav-value=""',
        tr('memory.allMemories'), numberValue(state.totalRecords) ? number(state.totalRecords) : '');
      html += '</div><div class="memory-nav-group"><div class="memory-nav-heading">' + esc(tr('memory.navCategories')) + '</div>';
      var categories = state.categories || [];
      if (!categories.length) html += '<p class="muted memory-nav-empty">' + esc(tr('memory.navEmpty')) + '</p>';
      for (var i = 0; i < categories.length; i++) {
        var entry = categories[i];
        var active = state.category === entry.name && !state.folder;
        html += navItem('category', active,
          'data-nav-value="' + esc(entry.name) + '"',
          entry.name === 'root' ? tr('memory.rootFiles') : entry.name, facetCount(entry));
      }
      html += '</div>';
      var dirs = (state.directories || []).filter(function (item) { return item.depth >= 1; });
      if (dirs.length) {
        html += '<div class="memory-nav-group"><div class="memory-nav-heading">' + esc(tr('memory.navFolders')) + '</div>';
        for (var d = 0; d < dirs.length; d++) {
          var dir = dirs[d];
          html += navItem('folder', state.folder === dir.rel,
            'data-nav-value="' + esc(dir.rel) + '" data-nav-category="' + esc((dir.rel || '').split('/')[0]) + '"',
            dir.rel, numberValue(dir.count) ? number(dir.count) : '');
        }
        html += '</div>';
      }
      var projects = state.projects || [];
      html += '<div class="memory-nav-group"><div class="memory-nav-heading">' + esc(tr('memory.navProjects')) + '</div>';
      if (!projects.length) html += '<p class="muted memory-nav-empty">' + esc(tr('memory.navEmpty')) + '</p>';
      for (var p = 0; p < projects.length; p++) {
        var project = projects[p];
        html += navItem('project', state.project === project.tag,
          'data-nav-value="' + esc(project.tag) + '"', project.name || project.tag, facetCount(project));
      }
      html += '</div>';
      nav.innerHTML = html;
    }

    function renderCount() {
      var el = $('memory-count');
      if (!el) return;
      if (state.error) { el.textContent = ''; return; }
      if (state.query) {
        el.textContent = tr('memory.semanticCount', { n: number(state.total) });
        return;
      }
      var from = state.total ? (state.page - 1) * state.pageSize + 1 : 0;
      var to = Math.min(state.total, state.page * state.pageSize);
      el.textContent = tr('memory.range', { from: number(from), to: number(to), total: number(state.total) });
    }

    function renderList(flags) {
      var list = $('memory-list');
      if (!list) return;
      if (flags && flags.loading) {
        list.innerHTML = '<p class="muted memory-list-state">' + esc(tr('memory.loading')) + '</p>';
        return;
      }
      if (flags && flags.error) {
        list.innerHTML = '<div class="memory-list-state"><p class="muted">' + esc(tr('memory.loadError', { error: flags.error })) + '</p>'
          + '<button type="button" class="secondary-button" data-memory-retry>' + esc(tr('memory.retry')) + '</button></div>';
        return;
      }
      if (!state.items.length) {
        list.innerHTML = '<div class="memory-list-state"><p class="muted">' + esc(state.query ? tr('memory.searchEmpty') : tr('memory.empty')) + '</p>'
          + (state.query ? '<button type="button" class="secondary-button" data-memory-clear>' + esc(tr('memory.clearSearch')) + '</button>' : '')
          + '</div>';
        return;
      }
      list.innerHTML = state.items.map(function (item) {
        var id = item.id || item.uri;
        var pinned = item.isPinned === true;
        var selected = state.selected === id;
        var preview = api.previewOf(item) || tr('memory.noPreview');
        var meta = [];
        if (item.projectName || item.projectTag) meta.push(item.projectName || item.projectTag);
        if (item.folder) meta.push(item.folder);
        else if (item.category) meta.push(item.category);
        if (item.legacyId) meta.push(tr('memory.source.legacy'));
        return '<article class="memory-row' + (pinned ? ' pinned' : '') + (selected ? ' selected' : '') + '" data-memory-row="' + esc(id) + '">'
          + '<label class="memory-select"><input type="checkbox" data-memory-select="' + esc(id) + '" ' + (selectedIds.has(id) ? 'checked ' : '') + 'aria-label="' + esc(tr('memory.select')) + '"></label>'
          + '<button type="button" class="memory-row-open" data-memory-open="' + esc(id) + '" aria-expanded="' + (selected ? 'true' : 'false') + '">'
          + '<span class="memory-row-head"><span class="memory-row-type">' + esc(item.memoryType || tr('memory.general')) + '</span>'
          + (pinned ? '<span class="memory-pin-badge">' + esc(tr('memory.pinned')) + '</span>' : '')
          + '<time>' + esc(formatTime(item.updatedAt || item.createdAt)) + '</time></span>'
          + '<span class="memory-row-preview">' + esc(preview) + '</span>'
          + '<span class="memory-row-meta">' + esc(meta.join(' · ')) + '</span>'
          + '</button></article>';
      }).join('');
      var deleteButton = $('memory-delete-selected');
      if (deleteButton) deleteButton.disabled = !selectedIds.size;
    }

    function renderPager() {
      var pager = $('memory-pager');
      if (!pager) return;
      if (state.query || state.totalPages <= 1) {
        pager.innerHTML = '';
        return;
      }
      pager.innerHTML = '<button type="button" class="secondary-button" data-memory-page="' + (state.page - 1) + '" ' + (state.page <= 1 ? 'disabled' : '') + '>'
        + esc(tr('memory.pagerPrev')) + '</button>'
        + '<span class="memory-pager-state">' + esc(tr('memory.pagerState', { page: number(state.page), pages: number(state.totalPages) })) + '</span>'
        + '<button type="button" class="secondary-button" data-memory-page="' + (state.page + 1) + '" ' + (state.page >= state.totalPages ? 'disabled' : '') + '>'
        + esc(tr('memory.pagerNext')) + '</button>';
    }

    function updateLayout() {
      var layout = $('memory-layout');
      if (!layout) return;
      var open = Boolean(state.selected && state.backend === 'openviking');
      layout.classList.toggle('inspector-open', open);
      var inspector = $('memory-inspector');
      if (inspector) inspector.hidden = !open;
    }

    function summaryPanel(level, detail) {
      var levelState = api.levelOf(detail, level);
      var title = tr(level === 'l0' ? 'memory.tab.l0' : 'memory.tab.l1');
      if (levelState.state === 'available') {
        var note = '';
        if (detail.kind !== 'directory' && levelState.uri) {
          note = '<p class="memory-level-note">' + esc(tr('memory.folderSummary', { path: api.folderLabel(levelState.uri) })) + '</p>';
          if (state.project) note += '<p class="memory-level-note">' + esc(tr('memory.aggregateNote')) + '</p>';
        } else if (detail.kind === 'directory' && state.project) {
          note = '<p class="memory-level-note">' + esc(tr('memory.aggregateNote')) + '</p>';
        }
        return '<div class="memory-level">' + note + '<pre class="memory-level-text">' + esc(levelState.text) + '</pre></div>';
      }
      var message = levelState.state === 'notReady' ? tr('memory.level.notReady')
        : levelState.state === 'error' ? tr('memory.level.error', { error: levelState.error || '' })
        : levelState.state === 'directory' ? tr('memory.level.directory')
        : tr(detail.kind === 'file' ? 'memory.level.fileMissing' : 'memory.level.missing');
      return '<div class="memory-level memory-level-absent"><p class="muted">' + esc(message) + '</p>'
        + '<p class="memory-level-caption">' + esc(tr('memory.level.caption')) + '</p></div>';
    }

    function provenanceRow(key, value) {
      return value ? '<div><span>' + esc(tr(key)) + '</span><strong>' + esc(value) + '</strong></div>' : '';
    }

    function renderInspector(flags) {
      var body = $('memory-inspector-body');
      var title = $('memory-inspector-title');
      if (!body) return;
      if (flags && flags.loading) {
        if (title) title.textContent = '';
        body.innerHTML = '<p class="muted">' + esc(tr('memory.detailLoading')) + '</p>';
        return;
      }
      if (flags && flags.error) {
        if (title) title.textContent = '';
        body.innerHTML = '<div class="memory-level-absent"><p class="muted">' + esc(tr('memory.detailError', { error: flags.error })) + '</p>'
          + '<button type="button" class="secondary-button" data-memory-detail-retry>' + esc(tr('memory.retry')) + '</button></div>';
        return;
      }
      var detail = state.detail;
      if (!detail) {
        if (title) title.textContent = '';
        body.innerHTML = '<p class="muted">' + esc(tr('memory.inspectorIdle')) + '</p>';
        return;
      }
      if (title) {
        title.textContent = api.titleOf(detail);
        title.title = title.textContent;
      }
      var sourceKey = detail.origin === 'legacy-migration' ? 'memory.source.legacy'
        : detail.origin === 'bridge' ? 'memory.source.bridge' : 'memory.source.openviking';
      var provenance = '<section class="memory-provenance" aria-label="' + esc(tr('memory.provenance')) + '">'
        + provenanceRow('memory.project', detail.project && (detail.project.name || detail.project.tag))
        + provenanceRow('memory.category', detail.category)
        + provenanceRow('memory.updated', formatTime(detail.updatedAt))
        + provenanceRow('memory.created', formatTime(detail.createdAt))
        + provenanceRow('memory.source', tr(sourceKey))
        + provenanceRow('memory.sourceId', detail.sourceId)
        + provenanceRow('memory.legacyId', detail.legacyId)
        + provenanceRow('memory.tags', (detail.tags || []).filter(function (tag) { return tag !== 'pinned=true'; }).join(', '))
        + '</section>';
      var session = '';
      if (detail.sessionId) {
        var url = api.sessionURL(detail);
        session = '<section class="memory-session"><span>' + esc(tr('memory.session')) + '</span>' + (url
          ? '<a href="' + esc(url) + '" target="_blank" rel="noopener">' + esc(detail.sessionId) + ' ↗</a>'
          : '<strong>' + esc(detail.sessionId) + '</strong><small>' + esc(tr('memory.sessionUnverified')) + '</small>') + '</section>';
      }
      var directoryInfo = detail.kind === 'directory'
        ? '<p class="memory-directory-note">' + esc(tr('memory.directoryRecords', { n: number(detail.records || 0) })) + '</p>' : '';
      var actions = detail.kind === 'directory' ? '' :
        '<div class="memory-inspector-actions">'
        + '<button type="button" class="secondary-button" data-memory-action="edit">' + esc(tr('action.edit')) + '</button>'
        + '<button type="button" class="secondary-button" data-memory-action="pin">' + esc(tr(detail.pinned ? 'memory.unpin' : 'memory.pin')) + '</button>'
        + '<button type="button" class="secondary-button danger-text" data-memory-action="delete">' + esc(tr('action.remove')) + '</button>'
        + '</div>';
      var tabs = ['l0', 'l1', 'l2'].map(function (level) {
        var selected = state.tab === level;
        return '<button type="button" role="tab" id="memory-tab-' + level + '" class="memory-tab' + (selected ? ' selected' : '') + '"'
          + ' data-memory-tab="' + level + '" aria-selected="' + (selected ? 'true' : 'false') + '" aria-controls="memory-panel-' + level + '"'
          + ' tabindex="' + (selected ? '0' : '-1') + '">' + esc(tr('memory.tab.' + level)) + '</button>';
      }).join('');
      var content = state.tab === 'l2'
        ? (detail.kind === 'directory'
          ? '<div class="memory-level memory-level-absent"><p class="muted">' + esc(tr('memory.level.directory')) + '</p></div>'
          : '<div class="memory-level"><pre class="memory-level-text memory-level-full">' + esc(detail.content || '') + '</pre></div>')
        : summaryPanel(state.tab, detail);
      body.innerHTML = directoryInfo + '<details class="memory-provenance-details"><summary>' + esc(tr('memory.provenance')) + '</summary>' + provenance + session + '</details>' + actions
        + '<div class="memory-tabs" role="tablist" aria-label="' + esc(tr('memory.levels')) + '">' + tabs + '</div>'
        + '<div class="memory-tab-panel" role="tabpanel" id="memory-panel-' + state.tab + '" aria-labelledby="memory-tab-' + state.tab + '" tabindex="0">' + content + '</div>';
    }

    function maintenanceFailure(result) {
      if (!result) return '';
      var errors = (result.errors || []).map(function (item) { return item.error || ''; }).filter(Boolean);
      if (Number(result.failed) > 0) return tr('memory.maintenancePartial', {
        deleted: number(result.deleted || 0), failed: number(result.failed),
        error: result.error || errors.slice(0, 3).join('; ') || tr('memory.maintenanceRetry')
      });
      return result.error ? tr('memory.maintenanceError', { error: result.error }) : '';
    }

    function renderCapacity() {
      var body = $('memory-capacity-body');
      var stateEl = $('memory-capacity-state');
      if (!body) return;
      var capacity = state.capacity;
      if (!capacity || capacity.error || capacity.enabled === false) {
        if (stateEl) stateEl.textContent = '';
        body.innerHTML = '<div class="memory-list-state"><p class="muted">' + esc(capacity && capacity.error
          ? tr('memory.capacityError', { error: capacity.error }) : tr('memory.capacityLoading')) + '</p>'
          + (capacity && capacity.error ? '<button type="button" class="secondary-button" data-memory-capacity-retry>' + esc(tr('memory.retry')) + '</button>' : '')
          + '</div>';
        return;
      }
      var summary = api.capacityState(capacity);
      if (stateEl) stateEl.textContent = summary.over ? tr('memory.overCapacity') : tr('memory.withinCapacity');
      var policy = capacity.policy || {};
      var logical = capacity.logical || {};
      var physical = capacity.physical || {};
      var preview = capacity.preview || {};
      var last = capacity.last || null;
      var metrics = '<div class="memory-metrics">'
        + '<article><span>' + esc(tr('memory.logical')) + '</span><strong>' + esc(formatBytes(logical.bytes)) + '</strong>'
        + '<small>' + esc(tr('memory.logicalNote', { n: number(logical.records || 0) })) + '</small></article>'
        + '<article><span>' + esc(tr('memory.physical')) + '</span><strong>' + esc(formatBytes(physical.bytes)) + '</strong>'
        + '<small>' + esc(tr('memory.physicalNote', { n: number(physical.files || 0) })) + '</small></article>'
        + '<article><span>' + esc(tr('memory.policy')) + '</span><strong>' + esc(numberValue(policy.max_storage_mb) ? policy.max_storage_mb + ' MB' : '—') + '</strong>'
        + '<small>' + esc(tr('memory.policyNote', { records: number(policy.max_memories || 0), percent: number(policy.target_percent || 0) })) + '</small></article>'
        + '</div>';
      var previewLine = Number(preview.candidates) > 0
        ? tr('memory.previewCandidates', { n: number(preview.candidates), size: formatBytes(preview.candidate_bytes) })
        : tr('memory.previewNone');
      var reclaim = '<div class="memory-preview">'
        + '<p><strong>' + esc(tr('memory.previewHeading')) + '</strong> ' + esc(previewLine) + '</p>'
        + '<p class="memory-panel-note">' + esc(tr('memory.previewPinned', { n: number(preview.pinned || 0) })) + '</p>'
        + '<button type="button" class="secondary-button" id="memory-maintenance-run">' + esc(tr('memory.maintenanceRun')) + '</button>'
        + '</div>';
      var run = '<p class="memory-panel-note">' + esc(tr('memory.lastRun', {
        time: capacity.last_checked_at ? formatTime(capacity.last_checked_at) : tr('memory.never'),
        deleted: last && numberValue(last.deleted) ? number(last.deleted) : 0
      })) + ' · ' + esc(tr('memory.nextDue', { time: capacity.next_due_at ? formatTime(capacity.next_due_at) : tr('memory.never') })) + '</p>'
        + (maintenanceFailure(last) ? '<p class="memory-panel-note danger-text">' + esc(maintenanceFailure(last)) + '</p>' : '');
      body.innerHTML = metrics + reclaim + run;
    }

    function renderActivity() {
      var body = $('memory-activity-body');
      var stateEl = $('memory-activity-state');
      if (!body) return;
      var activity = state.activity;
      if (!activity || activity.available === false) {
        if (stateEl) stateEl.textContent = '';
        body.innerHTML = '<div class="memory-list-state"><p class="muted">' + esc(activity && activity.error
          ? tr('memory.activityError', { error: activity.error }) : tr('memory.activityLoading')) + '</p>'
          + (activity && activity.error ? '<button type="button" class="secondary-button" data-memory-activity-retry>' + esc(tr('memory.retry')) + '</button>' : '')
          + '</div>';
        return;
      }
      var queue = activity.queue || {};
      var tone = api.queueTone(queue);
      if (stateEl) stateEl.textContent = tone === 'busy' ? tr('memory.queueBusy') : tone === 'warning' ? tr('memory.queueFailed') : tr('memory.queueIdle');
      var counts = '<div class="memory-metrics memory-metrics-compact">'
        + '<article><span>' + esc(tr('memory.queuePendingLabel')) + '</span><strong>' + esc(number(queue.pending || 0)) + '</strong><small>' + esc(tr('memory.queuePendingNote')) + '</small></article>'
        + '<article><span>' + esc(tr('memory.queueRunningLabel')) + '</span><strong>' + esc(number(queue.running || 0)) + '</strong><small>' + esc(tr('memory.queueRunningNote')) + '</small></article>'
        + '<article><span>' + esc(tr('memory.queueFailedLabel')) + '</span><strong>' + esc(number(queue.failed || 0)) + '</strong><small>' + esc(tr('memory.queueFailedNote')) + '</small></article>'
        + '<article><span>' + esc(tr('memory.sessionsRegistered')) + '</span><strong>' + esc(number((activity.sessions || {}).registered || 0)) + '</strong><small>' + esc(tr('memory.sessionsRegisteredNote')) + '</small></article>'
        + (activity.indexed ? '<article><span>' + esc(tr('memory.indexedTotal')) + '</span><strong>' + esc(number(activity.indexed.total_memories || 0)) + '</strong><small>' + esc(tr('memory.indexedNote')) + '</small></article>' : '')
        + '</div>';
      var commits = (activity.recent || []).map(function (commit) {
        return '<li class="memory-commit"><span class="memory-commit-session">' + esc(commit.sessionId || commit.id || '') + '</span>'
          + '<span class="memory-commit-meta">' + esc(tr('memory.commitMemories', { n: number(commit.memories || 0) })) + ' · '
          + esc(tr('memory.commit.' + (commit.status || 'unknown'))) + ' · ' + esc(formatTime(commit.updatedAt)) + '</span>'
          + (commit.error ? '<span class="danger-text">' + esc(commit.error) + '</span>' : '') + '</li>';
      }).join('');
      body.innerHTML = counts
        + '<div class="memory-commits"><h4>' + esc(tr('memory.recentCommits')) + '</h4>'
        + (commits ? '<ul>' + commits + '</ul>' : '<p class="muted">' + esc(tr('memory.recentNone')) + '</p>')
        + (activity.truncated ? '<p class="memory-panel-note">' + esc(tr('memory.activityTruncated')) + '</p>' : '')
        + '</div>';
    }

    /* ------------------------------------------------------------ actions */

    function projectEntry() {
      var tag = state.project;
      return (state.navProjects || []).find(function (item) { return String(item.tag || '') === String(tag || ''); }) || null;
    }

    function addMemory() {
      var projects = state.navProjects || [];
      if (!projects.length) { notice(Error(tr('memory.chooseProject'))); return; }
      var options = projects.map(function (item) {
        return '<option value="' + esc(item.tag) + '"' + (state.project === item.tag ? ' selected' : '') + '>'
          + esc(item.projectName || item.projectPath || item.tag) + '</option>';
      }).join('');
      var categories = (state.categories || []).filter(function (item) { return item.name !== 'root'; });
      var categoryOptions = (categories.length ? categories.map(function (item) { return item.name; }) : ['experiences', 'events', 'entities', 'preferences'])
        .map(function (name) { return '<option value="' + esc(name) + '">' + esc(name) + '</option>'; }).join('');
      openAction(tr('memory.add'), '<label>' + esc(tr('memory.project')) + '<select name="project" required>' + options + '</select></label>'
        + '<label>' + esc(tr('memory.content')) + '<textarea name="content" required rows="7" maxlength="100000"></textarea></label>'
        + '<label>' + esc(tr('memory.categoryLabel')) + '<select name="category">' + categoryOptions + '</select></label>'
        + '<label>' + esc(tr('memory.tags')) + '<input name="tags"></label>', async function (value) {
        var project = projects.find(function (item) { return item.tag === value.project; });
        if (!project) throw Error(tr('memory.chooseProject'));
        await mutate('/console-api/memory', {
          action: 'add', directory: project.projectPath, content: value.content,
          type: value.category, tags: value.tags.split(',').map(function (x) { return x.trim(); }).filter(Boolean)
        });
        await refreshAfterMutation();
      });
    }

    function editMemory() {
      var detail = state.detail;
      if (!detail || detail.kind === 'directory') return;
      openAction(tr('memory.edit'), '<label>' + esc(tr('memory.content')) + '<textarea name="content" required rows="10" maxlength="100000">'
        + esc(detail.content || '') + '</textarea></label>', async function (value) {
        await mutate('/console-api/memory', { action: 'update', id: detail.id || detail.uri, content: value.content });
        await refreshAfterMutation();
        await loadDetail(state.selected);
      });
    }

    async function togglePin() {
      var detail = state.detail;
      if (!detail || detail.kind === 'directory') return;
      try {
        await mutate('/console-api/memory', { action: 'pin', id: detail.id || detail.uri, pinned: !detail.pinned });
        await loadBrowse();
        await loadDetail(state.selected);
        await loadCapacity();
      } catch (error) { notice(error); }
    }

    function deleteMemory() {
      var detail = state.detail;
      if (!detail || detail.kind === 'directory') return;
      openAction(tr('memory.delete'), '<p>' + esc(tr('memory.deleteHint')) + '</p>', async function () {
        await mutate('/console-api/memory', { action: 'delete', id: detail.id || detail.uri });
        selectedIds.delete(detail.id || detail.uri);
        state.selected = null; state.detail = null;
        renderList({}); renderInspector({}); updateLayout();
        await refreshAfterMutation();
      });
    }

    function deleteSelected() {
      if (!selectedIds.size) return;
      var ids = Array.from(selectedIds);
      openAction(tr('memory.deleteSelected'), '<p>' + esc(tr('memory.deleteMany', { n: ids.length })) + '</p>', async function () {
        await mutate('/console-api/memory', { action: 'bulk_delete', ids: ids });
        selectedIds.clear();
        if (ids.indexOf(state.selected) !== -1) { state.selected = null; state.detail = null; renderInspector({}); }
        updateLayout();
        await refreshAfterMutation();
      });
    }

    function runMaintenance() {
      openAction(tr('memory.maintenanceRun'), '<p>' + esc(tr('memory.maintenanceConfirm')) + '</p>', async function () {
        var result = await mutate('/console-api/memory', { action: 'maintenance_run' });
        if (result && result.enabled === false) { showToast(tr('memory.maintenanceRun'), tr('memory.maintenanceDisabled'), 'warning'); }
        else if (maintenanceFailure(result)) { showToast(tr('memory.maintenanceRun'), maintenanceFailure(result), 'error', true); }
        else if (result && Number(result.deleted) > 0) { showToast(tr('memory.maintenanceRun'), tr('memory.maintenanceDone', { n: number(result.deleted) }), 'success'); }
        else { showToast(tr('memory.maintenanceRun'), tr('memory.maintenanceNone'), 'success'); }
        await refreshAfterMutation();
      });
    }

    async function refreshAfterMutation() {
      await loadBrowse();
      await loadCapacity();
      await loadActivity();
      await loadStatus();
    }

    function selectNav(kind, value, category) {
      if (kind === 'project') {
        state.project = state.project === value ? '' : value;
      } else if (kind === 'category') {
        state.category = state.category === value ? '' : value;
        state.folder = '';
      } else if (kind === 'folder') {
        if (state.folder === value) { state.folder = ''; }
        else { state.folder = value; state.category = category || (value || '').split('/')[0]; }
      } else {
        state.category = ''; state.folder = ''; state.project = '';
      }
      state.page = 1;
      loadBrowse();
      renderNav();
    }

    function onListClick(event) {
      var open = event.target.closest('[data-memory-open]');
      if (open) { loadDetail(open.dataset.memoryOpen); return; }
      var retry = event.target.closest('[data-memory-retry]');
      if (retry) { loadBrowse(); return; }
      var clear = event.target.closest('[data-memory-clear]');
      if (clear) { state.query = ''; var input = $('memory-search'); if (input) input.value = ''; state.page = 1; loadBrowse(); return; }
      var page = event.target.closest('[data-memory-page]');
      if (page && !page.disabled) { loadBrowse(Number(page.dataset.memoryPage)); return; }
      var nav = event.target.closest('[data-nav-kind]');
      if (nav) { selectNav(nav.dataset.navKind, nav.dataset.navValue, nav.dataset.navCategory); }
    }

    function onListChange(event) {
      var box = event.target.closest('[data-memory-select]');
      if (!box) return;
      if (box.checked) selectedIds.add(box.dataset.memorySelect); else selectedIds.delete(box.dataset.memorySelect);
      renderList({});
    }

    function onInspectorClick(event) {
      var tab = event.target.closest('[data-memory-tab]');
      if (tab) { state.tab = tab.dataset.memoryTab; renderInspector({}); focusTab(); return; }
      var action = event.target.closest('[data-memory-action]');
      if (action) {
        if (action.dataset.memoryAction === 'edit') editMemory();
        else if (action.dataset.memoryAction === 'pin') togglePin();
        else if (action.dataset.memoryAction === 'delete') deleteMemory();
        return;
      }
      var retry = event.target.closest('[data-memory-detail-retry]');
      if (retry) loadDetail(state.selected);
    }

    function onInspectorKeydown(event) {
      if (event.key !== 'ArrowRight' && event.key !== 'ArrowLeft' && event.key !== 'Home' && event.key !== 'End') return;
      var tabs = ['l0', 'l1', 'l2'];
      var index = tabs.indexOf(state.tab);
      if (event.key === 'ArrowRight') index = (index + 1) % tabs.length;
      else if (event.key === 'ArrowLeft') index = (index + tabs.length - 1) % tabs.length;
      else if (event.key === 'Home') index = 0;
      else index = tabs.length - 1;
      state.tab = tabs[index];
      event.preventDefault();
      renderInspector({});
      focusTab();
    }

    function focusTab() {
      var el = $('memory-tab-' + state.tab);
      if (el) el.focus();
    }

    function focusInspector() {
      if (!narrow()) return;
      var inspector = $('memory-inspector');
      if (inspector) inspector.focus({ preventScroll: true });
    }

    function backToList() {
      var previous = state.lastSelected;
      state.selected = null;
      state.detail = null;
      renderList({});
      renderInspector({});
      updateLayout();
      if (previous) {
        var row = document.querySelector('[data-memory-open="' + String(previous).replace(/["\\]/g, '\\$&') + '"]');
        if (row) row.focus({ preventScroll: true });
      }
    }

    function bind() {
      var list = $('memory-list');
      if (list) {
        list.addEventListener('click', onListClick);
        list.addEventListener('change', onListChange);
      }
      var nav = $('memory-nav');
      if (nav) nav.addEventListener('click', onListClick);
      var pager = $('memory-pager');
      if (pager) pager.addEventListener('click', onListClick);
      var inspector = $('memory-inspector');
      if (inspector) {
        inspector.addEventListener('click', onInspectorClick);
        inspector.addEventListener('keydown', onInspectorKeydown);
      }
      var back = $('memory-back');
      if (back) back.addEventListener('click', backToList);
      var close = $('memory-inspector-close');
      if (close) close.addEventListener('click', backToList);
      var refresh = $('memory-refresh');
      if (refresh) refresh.addEventListener('click', function () { controller.load(); });
      var add = $('memory-add');
      if (add) add.addEventListener('click', addMemory);
      var removeSelected = $('memory-delete-selected');
      if (removeSelected) removeSelected.addEventListener('click', deleteSelected);
      var sort = $('memory-sort');
      if (sort) sort.addEventListener('change', function () { state.sort = sort.value; state.page = 1; loadBrowse(); });
      var search = $('memory-search');
      if (search) {
        search.addEventListener('change', function () { state.query = search.value.trim(); state.page = 1; loadBrowse(); });
        search.addEventListener('search', function () { state.query = search.value.trim(); state.page = 1; loadBrowse(); });
      }
      var capacityBody = $('memory-capacity-body');
      if (capacityBody) capacityBody.addEventListener('click', function (event) {
        if (event.target.closest('#memory-maintenance-run')) runMaintenance();
        else if (event.target.closest('[data-memory-capacity-retry]')) loadCapacity();
      });
      var activityBody = $('memory-activity-body');
      if (activityBody) activityBody.addEventListener('click', function (event) {
        if (event.target.closest('[data-memory-activity-retry]')) loadActivity();
      });
    }

    function refreshLocale() {
      var provenance = document.querySelector('.memory-provenance-details');
      var provenanceOpen = provenance && provenance.open;
      renderStrip(); renderNav(); renderList({}); renderCount(); renderPager(); renderInspector({});
      renderCapacity(); renderActivity();
      provenance = document.querySelector('.memory-provenance-details');
      if (provenance) provenance.open = !!provenanceOpen;
    }

    async function load() {
      await loadStatus();
      applyCapabilities();
      if (state.legacy) {
        await loadBrowse();
        return;
      }
      await Promise.all([loadProjectsForAdd(), loadBrowse(), loadCapacity(), loadActivity()]);
      renderNav();
    }

    function start() {
      load().catch(function (error) { notice(error); });
      if (pollTimer) clearInterval(pollTimer);
      pollTimer = setInterval(function () {
        if (!visible()) return;
        loadStatus().catch(function () {});
        loadCapacity().catch(function () {});
        loadActivity().catch(function () {});
      }, 25000);
    }

    function stop() {
      /* Data stays rendered; polling stops until the view is opened again. */
      if (pollTimer) {
        clearInterval(pollTimer);
        pollTimer = null;
      }
    }

    controller = {
      start: start,
      stop: stop,
      load: load,
      refresh: refreshAfterMutation,
      refreshLocale: refreshLocale,
      state: state,
      bind: bind,
      select: loadDetail
    };
    return controller;
  }

  api.mount = mount;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  root.MemoryView = api;
})(typeof window !== 'undefined' ? window : globalThis);
