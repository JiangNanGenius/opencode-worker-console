/* Worker Desk setup wizard: guided first-run flow over the existing
   authenticated settings/models APIs.

   The wizard core (WorkerDeskSetup) is pure: it never performs network or DOM
   work, so Node behavior tests can drive the entire flow headlessly. Only
   apply() touches the injected request function, and only when the user
   explicitly chooses Apply. Next/back/cancel/goTo never mutate the server.

   Validation distinguishes models the user actively selected in this wizard
   (hard errors when absent from the catalog) from retained configuration
   (readiness warnings only), so preferences can be adjusted without forcing
   the replacement of existing custom profiles.

   The browser wiring at the bottom renders the draft, keeps selections across
   steps and language changes, and reuses the app.js helpers (tr/esc/request)
   through the shared global lexical scope of classic scripts. */
(function (global) {
  'use strict';

  var STEPS = ['providers', 'models', 'preferences', 'review'];
  var ROUTING_KEYS = ['fast', 'background', 'deep'];
  var WIZARD_PROFILE_IDS = ['fast-code', 'senior-code', 'deep-research'];
  var MONTHLY_FIELDS = ['enabled', 'day', 'time', 'timezone'];
  var DEFAULT_ROUTING = { fast: 'fast-code', background: 'senior-code', deep: 'deep-research' };
  // Mirrors install.py's documented DeepSeek + Kimi preset. The 'max' variant
  // is kept only when the catalog confirms it or lists no variant information;
  // it is never invented for models whose catalog entry excludes it.
  var PRESET_PROFILES = {
    'fast-code': { model: 'deepseek/deepseek-flash', label: 'DeepSeek V4.1 Flash', variant: 'max' },
    'senior-code': { model: 'kimi-for-coding/kimi-for-coding', label: 'Kimi K2.8 Preview', variant: 'max' },
    'deep-research': { model: 'kimi-for-coding/k3', label: 'Kimi K3', variant: 'max' }
  };

  function clone(value) { return value == null ? value : JSON.parse(JSON.stringify(value)); }

  function isInt(value) { return typeof value === 'number' && Number.isInteger(value); }

  function toInt(value) {
    if (typeof value === 'boolean' || value == null || value === '') return null;
    var n = Number(value);
    return Number.isInteger(n) ? n : null;
  }

  function normalizeCatalog(raw) {
    var list = Array.isArray(raw) ? raw : (raw && Array.isArray(raw.providers) ? raw.providers : []);
    var out = [];
    for (var i = 0; i < list.length; i++) {
      var p = list[i];
      if (!p || typeof p.id !== 'string' || !p.id) continue;
      var models = [];
      var rawModels = Array.isArray(p.models) ? p.models : Object.keys(p.models || {}).map(function (k) { return p.models[k]; });
      for (var j = 0; j < rawModels.length; j++) {
        var m = rawModels[j];
        if (!m || typeof m.id !== 'string' || !m.id) continue;
        var variants = Array.isArray(m.variants) ? m.variants.filter(function (v) { return typeof v === 'string'; }) : [];
        models.push({ id: m.id, name: typeof m.name === 'string' && m.name ? m.name : m.id, variants: variants });
      }
      out.push({ id: p.id, name: typeof p.name === 'string' && p.name ? p.name : p.id, connected: p.connected === true, models: models });
    }
    return out;
  }

  function normalizeMonthly(value) {
    value = value && typeof value === 'object' ? value : {};
    var day = toInt(value.day);
    return {
      enabled: value.enabled === true,
      day: day == null ? 1 : day,
      time: typeof value.time === 'string' && value.time ? value.time : '12:00',
      timezone: typeof value.timezone === 'string' && value.timezone ? value.timezone : 'Asia/Shanghai'
    };
  }

  function modelEntry(catalog, ref) {
    if (typeof ref !== 'string') return null;
    var slash = ref.indexOf('/');
    if (slash <= 0 || slash === ref.length - 1) return null;
    var pid = ref.slice(0, slash), mid = ref.slice(slash + 1);
    for (var i = 0; i < catalog.length; i++) {
      if (catalog[i].id !== pid) continue;
      for (var j = 0; j < catalog[i].models.length; j++)
        if (catalog[i].models[j].id === mid) return { provider: catalog[i], model: catalog[i].models[j] };
      return null;
    }
    return null;
  }

  function emptyTouched() {
    return { profiles: {}, routing: {}, autoApprove: false, maxParallelPerOwner: false, monthly: {} };
  }

  function createDraft(settings, rawCatalog) {
    settings = settings && typeof settings === 'object' ? settings : {};
    var catalog = normalizeCatalog(rawCatalog);
    var reserve = toInt(settings.kimi_reserve_percent);
    var base = {
      profiles: clone(settings.profiles) || {},
      routing: clone(settings.routing) || null,
      routingPolicy: clone(settings.routing_policy) || null,
      cleanup: clone(settings.cleanup) || null,
      kimiReservePercent: reserve == null ? 0 : reserve,
      monthlyReset: normalizeMonthly(settings.kimi_monthly_reset)
    };
    return {
      step: 0,
      status: 'editing',          // editing | applying | applied | error | cancelled
      errors: [],                 // blocking validation errors: [{key, field, vars?}]
      warnings: [],               // non-blocking readiness warnings for retained config
      error: null,                // last server-side apply failure message
      stale: false,               // apply failed because the revision moved on
      revision: isInt(settings.revision) ? settings.revision : 0,
      catalog: catalog,
      base: base,
      touched: emptyTouched(),
      choice: 'keep',             // keep | preset | custom
      profiles: clone(base.profiles),
      routing: clone(base.routing) || clone(DEFAULT_ROUTING),
      autoApprove: settings.auto_approve !== false,
      maxParallelPerOwner: settings.max_parallel_per_owner,
      kimiReservePercent: base.kimiReservePercent,
      monthlyReset: clone(base.monthlyReset),
      saved: null
    };
  }

  function anyConnected(draft) {
    return draft.catalog.some(function (p) { return p.connected; });
  }

  function presetAvailable(draft) {
    return WIZARD_PROFILE_IDS.every(function (id) { return !!modelEntry(draft.catalog, PRESET_PROFILES[id].model); });
  }

  // Keep the documented preset variant only when the catalog confirms it or
  // offers no variant information at all; never force it onto a model whose
  // catalog entry explicitly lists other variants.
  function presetVariant(catalog, ref, wanted) {
    var entry = modelEntry(catalog, ref);
    if (!entry) return null;
    if (!entry.model.variants.length) return wanted;
    return entry.model.variants.indexOf(wanted) >= 0 ? wanted : null;
  }

  function setChoice(draft, choice) {
    if (['keep', 'preset', 'custom'].indexOf(choice) < 0) return false;
    if (choice === 'preset' && !presetAvailable(draft)) return false;
    draft.choice = choice;
    var id, profiles;
    if (choice === 'keep') {
      // Explicit opt-out of replacement: profile/routing edits are discarded
      // and a later stale-reload adopts the external configuration wholesale.
      draft.profiles = clone(draft.base.profiles);
      draft.routing = clone(draft.base.routing) || clone(DEFAULT_ROUTING);
      draft.touched.profiles = {};
      draft.touched.routing = {};
    } else if (choice === 'preset') {
      profiles = clone(draft.base.profiles);
      draft.touched.profiles = {};
      for (var i = 0; i < WIZARD_PROFILE_IDS.length; i++) {
        id = WIZARD_PROFILE_IDS[i];
        var spec = PRESET_PROFILES[id];
        var profile = { model: spec.model, label: spec.label, enabled: true };
        var variant = presetVariant(draft.catalog, spec.model, spec.variant);
        if (variant) profile.variant = variant;
        profiles[id] = profile;
        draft.touched.profiles[id] = true;
      }
      draft.profiles = profiles;
      draft.routing = clone(DEFAULT_ROUTING);
      ROUTING_KEYS.forEach(function (key) { draft.touched.routing[key] = true; });
    } else {
      profiles = clone(draft.profiles);
      for (var j = 0; j < WIZARD_PROFILE_IDS.length; j++) {
        id = WIZARD_PROFILE_IDS[j];
        if (!profiles[id]) {
          profiles[id] = { model: '', label: id, enabled: true };
          draft.touched.profiles[id] = true;
        }
      }
      draft.profiles = profiles;
    }
    return true;
  }

  function setModel(draft, profileId, ref) {
    var profile = draft.profiles[profileId];
    if (!profile) return false;
    profile.model = typeof ref === 'string' ? ref : '';
    // Never carry a variant across models; the user picks from the new model's
    // actual catalog variants (or the provider default when none are listed).
    delete profile.variant;
    var entry = modelEntry(draft.catalog, profile.model);
    profile.label = entry ? entry.model.name : (profile.model || profileId);
    draft.touched.profiles[profileId] = true;
    return true;
  }

  function setVariant(draft, profileId, variant) {
    var profile = draft.profiles[profileId];
    if (!profile) return false;
    if (variant == null || variant === '') {
      delete profile.variant;
      draft.touched.profiles[profileId] = true;
      return true;
    }
    var entry = modelEntry(draft.catalog, profile.model);
    if (!entry || entry.model.variants.indexOf(variant) < 0) return false;
    profile.variant = variant;
    draft.touched.profiles[profileId] = true;
    return true;
  }

  function setRouting(draft, key, profileId) {
    if (ROUTING_KEYS.indexOf(key) < 0) return false;
    draft.routing[key] = profileId;
    draft.touched.routing[key] = true;
    return true;
  }

  function setAutoApprove(draft, value) {
    draft.autoApprove = value === true;
    draft.touched.autoApprove = true;
  }

  function setMaxParallelPerOwner(draft, value) {
    draft.maxParallelPerOwner = value;
    draft.touched.maxParallelPerOwner = true;
  }

  function setMonthlyReset(draft, patch) {
    patch = patch && typeof patch === 'object' ? patch : {};
    MONTHLY_FIELDS.forEach(function (field) {
      if (!(field in patch)) return;
      draft.monthlyReset[field] = field === 'enabled' ? patch[field] === true : patch[field];
      draft.touched.monthly[field] = true;
    });
  }

  function providerErrors(draft) {
    return anyConnected(draft) ? [] : [{ key: 'setup.providers.needOne', field: 'providers' }];
  }

  // Hard errors cover only what the user actively selected in this wizard:
  // preset/custom models must exist in the catalog with a listed variant.
  // Retained configuration is never blocked here; profileWarnings reports it.
  function profileErrors(draft) {
    var errs = [];
    var profiles = draft.profiles || {};
    var ids = Object.keys(profiles);
    if (!ids.length) errs.push({ key: 'setup.error.noProfiles', field: 'profiles' });
    if (draft.choice === 'preset' && !presetAvailable(draft))
      errs.push({ key: 'setup.choice.presetUnavailable', field: 'choice' });
    ids.forEach(function (id) {
      if (!/^[a-z][a-z0-9-]*$/.test(id)) errs.push({ key: 'error.profileId', field: 'profiles' });
      if (!draft.touched.profiles[id]) return;
      var p = profiles[id] || {};
      if (typeof p.model !== 'string' || !p.model.trim()) {
        errs.push({ key: 'setup.error.modelRequired', field: 'model:' + id });
        return;
      }
      var entry = modelEntry(draft.catalog, p.model);
      if (!entry) {
        errs.push({ key: 'setup.error.modelUnknown', field: 'model:' + id, vars: { model: p.model } });
        return;
      }
      if (p.variant && entry.model.variants.length && entry.model.variants.indexOf(p.variant) < 0)
        errs.push({ key: 'setup.error.variantUnknown', field: 'variant:' + id, vars: { variant: p.variant } });
    });
    ROUTING_KEYS.forEach(function (key) {
      var target = draft.routing && draft.routing[key];
      if (!target || !profiles[target] || profiles[target].enabled === false)
        errs.push({ key: 'setup.error.routingInvalid', field: 'routing:' + key });
    });
    return errs;
  }

  // Non-blocking readiness warnings for retained configuration the wizard
  // never touched: kept verbatim in the payload, but flagged as unverifiable.
  function profileWarnings(draft) {
    var warns = [];
    Object.keys(draft.profiles || {}).forEach(function (id) {
      if (draft.touched.profiles[id]) return;
      var p = draft.profiles[id];
      if (!p || p.enabled === false || typeof p.model !== 'string' || !p.model) return;
      var entry = modelEntry(draft.catalog, p.model);
      if (!entry) {
        warns.push({ key: 'setup.warn.modelUnverified', field: 'model:' + id, vars: { id: id, model: p.model } });
      } else if (p.variant && entry.model.variants.length && entry.model.variants.indexOf(p.variant) < 0) {
        warns.push({ key: 'setup.warn.variantUnverified', field: 'variant:' + id, vars: { id: id, variant: p.variant } });
      }
    });
    return warns;
  }

  function preferenceErrors(draft) {
    var errs = [];
    var n = toInt(draft.maxParallelPerOwner);
    if (n == null || n < 1 || n > 16)
      errs.push({ key: 'setup.error.concurrencyRange', field: 'maxParallelPerOwner' });
    var mr = draft.monthlyReset || {};
    if (mr.enabled) {
      var day = toInt(mr.day);
      if (day == null || day < 1 || day > 31)
        errs.push({ key: 'setup.error.monthlyDay', field: 'monthlyDay' });
      if (!/^([01]\d|2[0-3]):[0-5]\d$/.test(String(mr.time || '')))
        errs.push({ key: 'setup.error.monthlyTime', field: 'monthlyTime' });
      if (!mr.timezone || !String(mr.timezone).trim())
        errs.push({ key: 'setup.error.monthlyZone', field: 'monthlyZone' });
    }
    return errs;
  }

  function stepErrors(draft, index) {
    var name = STEPS[index];
    if (name === 'providers') return providerErrors(draft);
    if (name === 'models') return profileErrors(draft);
    if (name === 'preferences') return preferenceErrors(draft);
    if (name === 'review') return profileErrors(draft).concat(preferenceErrors(draft));
    return [];
  }

  function next(draft) {
    var errs = stepErrors(draft, draft.step);
    draft.errors = errs;
    draft.warnings = profileWarnings(draft);
    if (errs.length) return false;
    if (draft.step >= STEPS.length - 1) return false;
    draft.step += 1;
    draft.errors = [];
    return true;
  }

  function back(draft) {
    if (draft.step > 0) draft.step -= 1;
    draft.errors = [];
    return true;
  }

  function goTo(draft, target) {
    if (!isInt(target) || target < 0 || target >= STEPS.length) return false;
    var guard = 0;
    while (draft.step < target && guard++ < STEPS.length) if (!next(draft)) return false;
    while (draft.step > target) back(draft);
    return draft.step === target;
  }

  function cancel(draft) {
    draft.status = 'cancelled';
    draft.errors = [];
    draft.error = null;
  }

  function buildPayload(draft) {
    var errs = stepErrors(draft, STEPS.length - 1);
    if (errs.length) {
      var failure = new Error('Wizard validation failed');
      failure.errors = errs;
      throw failure;
    }
    var profiles = {};
    Object.keys(draft.profiles).forEach(function (id) {
      var p = draft.profiles[id] || {};
      var out = {
        model: String(p.model || '').trim(),
        label: typeof p.label === 'string' && p.label.trim() ? p.label.trim() : id,
        enabled: p.enabled !== false
      };
      if (typeof p.variant === 'string' && p.variant.trim()) out.variant = p.variant.trim();
      profiles[id] = out;
    });
    var mr = draft.monthlyReset || {};
    var payload = {
      revision: draft.revision,
      auto_approve: draft.autoApprove === true,
      profiles: profiles,
      max_parallel_per_owner: toInt(draft.maxParallelPerOwner),
      kimi_reserve_percent: toInt(draft.kimiReservePercent) == null ? 0 : toInt(draft.kimiReservePercent),
      routing: { fast: draft.routing.fast, background: draft.routing.background, deep: draft.routing.deep },
      kimi_monthly_reset: {
        enabled: mr.enabled === true,
        day: toInt(mr.day) == null ? 1 : toInt(mr.day),
        time: String(mr.time || '12:00'),
        timezone: String(mr.timezone || '').trim() || 'Asia/Shanghai'
      }
    };
    // Untouched configuration round-trips exactly as loaded: the cleanup
    // policy is never rebuilt by the wizard.
    if (draft.base.cleanup) payload.cleanup = clone(draft.base.cleanup);
    if (draft.base.routingPolicy) payload.routing_policy = clone(draft.base.routingPolicy);
    return payload;
  }

  // The single network operation of the whole flow, invoked only by the
  // explicit Apply control. Failures stay recoverable: the draft is intact and
  // apply can be called again; a stale revision is flagged for reload.
  function apply(draft, request) {
    if (draft.status === 'applying') return Promise.resolve({ ok: false, busy: true });
    var payload;
    try {
      payload = buildPayload(draft);
    } catch (failure) {
      draft.errors = failure.errors || [];
      draft.status = 'editing';
      return Promise.resolve({ ok: false, validation: true, errors: draft.errors });
    }
    draft.status = 'applying';
    draft.error = null;
    draft.stale = false;
    return request('/console-api/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    }).then(function (saved) {
      draft.status = 'applied';
      draft.saved = saved;
      return { ok: true, saved: saved };
    }, function (err) {
      var message = err && err.message ? err.message : String(err);
      draft.status = 'error';
      draft.error = message;
      draft.stale = /changed elsewhere/i.test(message);
      return { ok: false, error: message, stale: draft.stale };
    });
  }

  // Recovery after a stale revision, field by field: adopt the fresh revision
  // and every setting the user did not touch in this wizard — including
  // profiles added or removed elsewhere — while keeping each explicit edit.
  function mergeFreshSettings(draft, settings) {
    settings = settings && typeof settings === 'object' ? settings : {};
    var reserve = toInt(settings.kimi_reserve_percent);
    draft.revision = isInt(settings.revision) ? settings.revision : draft.revision;
    draft.base = {
      profiles: clone(settings.profiles) || {},
      routing: clone(settings.routing) || null,
      routingPolicy: clone(settings.routing_policy) || null,
      cleanup: clone(settings.cleanup) || null,
      kimiReservePercent: reserve == null ? 0 : reserve,
      monthlyReset: normalizeMonthly(settings.kimi_monthly_reset)
    };
    var merged = clone(draft.base.profiles);
    Object.keys(draft.touched.profiles).forEach(function (id) {
      if (draft.profiles[id]) merged[id] = clone(draft.profiles[id]);
    });
    draft.profiles = merged;
    var routing = clone(draft.base.routing) || clone(DEFAULT_ROUTING);
    ROUTING_KEYS.forEach(function (key) {
      if (!draft.touched.routing[key]) return;
      var target = draft.routing[key];
      if (target && merged[target] && merged[target].enabled !== false) routing[key] = target;
    });
    draft.routing = routing;
    if (!draft.touched.autoApprove) draft.autoApprove = settings.auto_approve !== false;
    if (!draft.touched.maxParallelPerOwner) draft.maxParallelPerOwner = settings.max_parallel_per_owner;
    draft.kimiReservePercent = draft.base.kimiReservePercent;
    var monthly = clone(draft.base.monthlyReset);
    MONTHLY_FIELDS.forEach(function (field) {
      if (draft.touched.monthly[field]) monthly[field] = draft.monthlyReset[field];
    });
    draft.monthlyReset = monthly;
    draft.stale = false;
    draft.error = null;
    draft.status = 'editing';
    draft.warnings = profileWarnings(draft);
    return draft;
  }

  // Truthful per-provider status for review: configured (model exists in the
  // catalog), connected (OpenCode reports the provider), and quota telemetry
  // from the last balance sample. Quota availability is never presented as
  // real inference success: the wizard sends no model requests at all.
  function providerStatus(draft, quota) {
    quota = quota && typeof quota === 'object' ? quota : {};
    var used = {};
    Object.keys(draft.profiles || {}).forEach(function (id) {
      var p = draft.profiles[id];
      if (!p || p.enabled === false || typeof p.model !== 'string' || !p.model) return;
      var pid = p.model.split('/')[0];
      if (!used[pid]) used[pid] = { provider: pid, name: pid, configured: true, connected: false };
      var entry = modelEntry(draft.catalog, p.model);
      if (!entry) used[pid].configured = false;
      else { used[pid].connected = entry.provider.connected; used[pid].name = entry.provider.name; }
    });
    return Object.keys(used).sort().map(function (pid) {
      var row = used[pid];
      var q = quota[pid];
      var telemetry = 'unknown';
      if (q && typeof q === 'object') {
        if (q.monthly_plan_exhausted === true || (q.billing && q.billing.reason === 'monthly_usage_limit')) telemetry = 'monthly';
        else if (q.state === 'auth_error') telemetry = 'authError';
        else if (q.available === true) telemetry = 'available';
        else if (q.available === false) telemetry = 'unavailable';
      }
      return { provider: pid, name: row.name, configured: row.configured, connected: row.connected, telemetry: telemetry };
    });
  }

  function telemetryKey(status) {
    if (status === 'available') return 'setup.request.quotaAvailable';
    if (status === 'unavailable') return 'quota.state.unavailable';
    if (status === 'authError') return 'quota.state.authError';
    if (status === 'monthly') return 'setup.request.monthly';
    return 'setup.request.untested';
  }

  var Setup = {
    STEPS: STEPS,
    ROUTING_KEYS: ROUTING_KEYS,
    WIZARD_PROFILE_IDS: WIZARD_PROFILE_IDS,
    DEFAULT_ROUTING: DEFAULT_ROUTING,
    PRESET_PROFILES: PRESET_PROFILES,
    clone: clone,
    toInt: toInt,
    normalizeCatalog: normalizeCatalog,
    normalizeMonthly: normalizeMonthly,
    modelEntry: modelEntry,
    createDraft: createDraft,
    anyConnected: anyConnected,
    presetAvailable: presetAvailable,
    presetVariant: presetVariant,
    setChoice: setChoice,
    setModel: setModel,
    setVariant: setVariant,
    setRouting: setRouting,
    setAutoApprove: setAutoApprove,
    setMaxParallelPerOwner: setMaxParallelPerOwner,
    setMonthlyReset: setMonthlyReset,
    stepErrors: stepErrors,
    profileWarnings: profileWarnings,
    next: next,
    back: back,
    goTo: goTo,
    cancel: cancel,
    buildPayload: buildPayload,
    apply: apply,
    mergeFreshSettings: mergeFreshSettings,
    providerStatus: providerStatus,
    telemetryKey: telemetryKey
  };

  if (typeof module !== 'undefined' && module.exports) module.exports = Setup;
  global.WorkerDeskSetup = Setup;

  /* ================= browser wiring ================= */
  if (typeof document === 'undefined' || typeof window === 'undefined') return;

  var root = document.getElementById('setup-root');
  if (!root) return;
  var banner = document.getElementById('setup-banner');
  var DISMISS_KEY = 'worker-desk-setup-v1';
  var draft = null;
  var loading = false;
  var loadingError = null;
  var refreshError = null;

  function dismissed() {
    try { return localStorage.getItem(DISMISS_KEY) === 'done'; } catch (e) { return true; }
  }

  function markDone() {
    try { localStorage.setItem(DISMISS_KEY, 'done'); } catch (e) {}
  }

  function quotaData() {
    try { return (typeof data !== 'undefined' && data && data.quota) || {}; } catch (e) { return {}; }
  }

  // The banner is a passive, dismissible hint — never an auto-opening overlay.
  // It appears only after a successful state refresh still shows zero tasks.
  function updateBanner() {
    if (!banner) return;
    var d = null;
    try { d = typeof data !== 'undefined' ? data : null; } catch (e) {}
    banner.hidden = dismissed() || !d || !d.updated_at || !Array.isArray(d.tasks) || d.tasks.length !== 0;
  }

  function switchView(name) {
    var button = document.querySelector('[data-view="' + name + '"]');
    if (button) button.click();
  }

  function loadWizard() {
    if (loading) return;
    loading = true;
    loadingError = null;
    render();
    Promise.all([request('/console-api/settings'), request('/console-api/models')]).then(function (results) {
      loading = false;
      draft = Setup.createDraft(results[0], results[1]);
      render();
    }, function (err) {
      loading = false;
      loadingError = err;
      render();
    });
  }

  function openWizard() {
    if (loading) return;
    if (draft && draft.status !== 'cancelled' && draft.status !== 'applied') {
      render();
      refreshCatalog();
      return;
    }
    loadWizard();
  }

  function refreshCatalog() {
    if (!draft || draft.status === 'applying') return;
    refreshError = null;
    request('/console-api/models').then(function (catalog) {
      if (!draft) return;
      refreshError = null;
      draft.catalog = Setup.normalizeCatalog(catalog);
      render();
    }, function (err) {
      refreshError = err && err.message ? err.message : String(err);
      render();
    });
  }

  function reloadLatest() {
    if (!draft || draft.status === 'applying') return;
    Promise.all([request('/console-api/settings'), request('/console-api/models')]).then(function (results) {
      if (!draft) return;
      Setup.mergeFreshSettings(draft, results[0]);
      draft.catalog = Setup.normalizeCatalog(results[1]);
      render();
    }, function (err) {
      if (!draft) return;
      draft.status = 'error';
      draft.error = err && err.message ? err.message : String(err);
      render();
    });
  }

  function doApply() {
    if (!draft || draft.status === 'applying') return;
    Setup.apply(draft, request).then(function (result) {
      if (result && result.ok) markDone();
      render();
    });
    render();
  }

  function profileSummaryHTML(profiles) {
    var ids = Object.keys(profiles || {});
    if (!ids.length) return '<p class="muted">' + esc(tr('setup.error.noProfiles')) + '</p>';
    return '<ul class="setup-list">' + ids.map(function (id) {
      var p = profiles[id] || {};
      var bits = '<strong>' + esc(p.label || id) + '</strong><span class="muted">' + esc(id) + '</span><span>' + esc(p.model || '—') + '</span>';
      if (p.variant) bits += '<span class="muted">' + esc(p.variant) + '</span>';
      if (p.enabled === false) bits += '<span class="muted">· ' + esc(tr('setup.no')) + '</span>';
      return '<li>' + bits + '</li>';
    }).join('') + '</ul>';
  }

  function routingSummaryHTML() {
    if (draft.base.routingPolicy && Object.keys(draft.base.routingPolicy).length && typeof routingPolicySummary === 'function') {
      return routingPolicySummary(draft.base.routingPolicy, draft.profiles) + '<p class="muted">' + esc(tr('routing.policyPreserved')) + '</p>';
    }
    return '<ul class="setup-list">' + ROUTING_KEYS.map(function (key) {
      var target = draft.routing && draft.routing[key];
      var profile = target && draft.profiles[target];
      return '<li><span>' + esc(tr('routing.' + key)) + '</span><strong>' + esc(profile ? (profile.label || target) : '—') + '</strong></li>';
    }).join('') + '</ul>';
  }

  function choiceHTML(value, titleKey, hintKey, disabled, disabledKey) {
    var selected = draft.choice === value;
    return '<label class="setup-choice' + (selected ? ' selected' : '') + '">' +
      '<input type="radio" name="setup-choice" value="' + value + '"' + (selected ? ' checked' : '') + (disabled ? ' disabled' : '') + '>' +
      '<span><strong>' + esc(tr(titleKey)) + '</strong><small>' + esc(tr(disabled ? disabledKey : hintKey)) + '</small></span></label>';
  }

  function modelOptions(selected) {
    var providers = draft.catalog.slice().sort(function (a, b) { return (b.connected ? 1 : 0) - (a.connected ? 1 : 0); });
    var found = false;
    var html = providers.map(function (p) {
      var label = p.name + (p.connected ? '' : ' · ' + tr('provider.disconnected'));
      var opts = p.models.map(function (m) {
        var ref = p.id + '/' + m.id;
        if (ref === selected) found = true;
        return '<option value="' + esc(ref) + '"' + (ref === selected ? ' selected' : '') + '>' + esc(m.name) + '</option>';
      }).join('');
      return opts ? '<optgroup label="' + esc(label) + '">' + opts + '</optgroup>' : '';
    }).join('');
    if (selected && !found)
      html = '<option value="' + esc(selected) + '" selected>' + esc(tr('setup.modelNotInCatalog', { model: selected })) + '</option>' + html;
    return html;
  }

  function variantFieldHTML(id, profile) {
    var entry = Setup.modelEntry(draft.catalog, profile.model);
    var variants = entry ? entry.model.variants : [];
    if (!profile.model) return '';
    if (!variants.length)
      return '<p class="muted setup-variant-note">' + esc(tr('setup.variant.none')) + '</p>';
    var opts = '<option value="">' + esc(tr('setup.variant.default')) + '</option>' + variants.map(function (v) {
      return '<option value="' + esc(v) + '"' + (profile.variant === v ? ' selected' : '') + '>' + esc(v) + '</option>';
    }).join('');
    if (profile.variant && variants.indexOf(profile.variant) < 0)
      opts = '<option value="' + esc(profile.variant) + '" selected>' + esc(profile.variant) + '</option>' + opts;
    return '<label><span>' + esc(tr('field.variant')) + '</span><select data-profile-variant="' + esc(id) + '">' + opts + '</select></label>';
  }

  function stepProvidersHTML() {
    var rows = draft.catalog.map(function (p) {
      return '<div class="setup-provider"><strong>' + esc(p.name) + '</strong>' +
        '<span class="' + (p.connected ? 'setup-ok' : 'setup-bad') + '">' + esc(tr(p.connected ? 'provider.connected' : 'provider.disconnected')) + '</span>' +
        '<span class="muted">' + esc(tr('provider.models', { n: p.models.length })) + '</span></div>';
    }).join('') || '<p class="muted">' + esc(tr('provider.none')) + '</p>';
    return '<h3 tabindex="-1" data-setup-heading>' + esc(tr('setup.providers.title')) + '</h3>' +
      '<p class="setup-lede">' + esc(tr('setup.providers.text')) + '</p>' +
      '<p><a class="detail-link" href="/" target="_blank" rel="noopener">' + esc(tr('providers.open')) + '</a></p>' +
      '<div class="setup-providers">' + rows + '</div>' +
      '<p><button type="button" class="secondary-button" data-action="refresh-providers">' + esc(tr('setup.providers.recheck')) + '</button></p>';
  }

  function stepModelsHTML() {
    var html = '<h3 tabindex="-1" data-setup-heading>' + esc(tr('setup.models.title')) + '</h3>' +
      '<fieldset class="setup-choices"><legend class="sr-only">' + esc(tr('setup.models.title')) + '</legend>' +
      choiceHTML('keep', 'setup.choice.keep', 'setup.choice.keepHint') +
      choiceHTML('preset', 'setup.choice.preset', 'setup.choice.presetHint', !Setup.presetAvailable(draft), 'setup.choice.presetUnavailable') +
      choiceHTML('custom', 'setup.choice.custom', 'setup.choice.customHint') +
      '</fieldset>';
    if (draft.choice === 'custom') {
      html += WIZARD_PROFILE_IDS.map(function (id, i) {
        var profile = draft.profiles[id] || { model: '', enabled: true };
        return '<div class="setup-profile"><h4 class="setup-subhead">' + esc(tr('routing.' + ROUTING_KEYS[i])) + ' · ' + esc(id) + '</h4>' +
          '<div class="setup-field-grid"><label><span>' + esc(tr('field.providerModel')) + '</span>' +
          '<select data-profile-model="' + esc(id) + '"><option value=""></option>' + modelOptions(profile.model) + '</select></label>' +
          variantFieldHTML(id, profile) + '</div></div>';
      }).join('');
      var enabled = Object.keys(draft.profiles).filter(function (id) { return draft.profiles[id] && draft.profiles[id].enabled !== false; });
      html += '<div class="setup-profile"><h4 class="setup-subhead">' + esc(tr('routing.heading')) + '</h4><div class="setup-field-grid">' +
        ROUTING_KEYS.map(function (key) {
          var opts = enabled.map(function (id) {
            var p = draft.profiles[id];
            return '<option value="' + esc(id) + '"' + (draft.routing[key] === id ? ' selected' : '') + '>' + esc((p.label || id) + ' · ' + id) + '</option>';
          }).join('');
          return '<label><span>' + esc(tr('routing.' + key)) + '</span><select data-routing="' + esc(key) + '">' + opts + '</select></label>';
        }).join('') + '</div></div>';
    } else {
      html += '<div class="setup-review-grid"><div>' + profileSummaryHTML(draft.profiles) + '</div><div><h4 class="setup-subhead">' + esc(tr('routing.heading')) + '</h4>' + routingSummaryHTML() + '</div></div>';
    }
    return html;
  }

  function stepPreferencesHTML() {
    var mr = draft.monthlyReset;
    var disabled = mr.enabled ? '' : ' disabled';
    return '<h3 tabindex="-1" data-setup-heading>' + esc(tr('setup.prefs.title')) + '</h3>' +
      '<div class="setup-profile"><label class="check-label"><input type="checkbox" data-pref="autoApprove"' + (draft.autoApprove ? ' checked' : '') + '><span>' + esc(tr('autoApprove.label')) + '</span></label>' +
      '<p class="muted">' + esc(tr('autoApprove.note')) + '</p></div>' +
      '<div class="setup-profile"><div class="setup-field-grid"><label><span>' + esc(tr('routing.maxParallelPerOwner')) + '</span>' +
      '<input type="number" min="1" max="16" data-pref="maxParallelPerOwner" value="' + esc(draft.maxParallelPerOwner) + '"></label></div>' +
      '<p class="muted">' + esc(tr('routing.maxParallelPerOwnerNote')) + '</p></div>' +
      '<div class="setup-profile"><h4 class="setup-subhead">' + esc(tr('monthlyReset.heading')) + '</h4>' +
      '<label class="check-label"><input type="checkbox" data-monthly="enabled"' + (mr.enabled ? ' checked' : '') + '><span>' + esc(tr('monthlyReset.enabled')) + '</span></label>' +
      '<p class="muted">' + esc(tr('setup.prefs.monthlyOptional')) + '</p>' +
      '<div class="setup-field-grid"><label><span>' + esc(tr('monthlyReset.day')) + '</span><input type="number" min="1" max="31" data-monthly="day" value="' + esc(mr.day) + '"' + disabled + '></label>' +
      '<label><span>' + esc(tr('monthlyReset.time')) + '</span><input type="time" data-monthly="time" value="' + esc(mr.time) + '"' + disabled + '></label>' +
      '<label><span>' + esc(tr('monthlyReset.timezone')) + '</span><input type="text" list="reset-timezones" data-monthly="timezone" value="' + esc(mr.timezone) + '"' + disabled + '></label></div>' +
      '<p class="muted">' + esc(tr('monthlyReset.note')) + '</p></div>';
  }

  function stepReviewHTML() {
    var statuses = Setup.providerStatus(draft, quotaData());
    var mr = draft.monthlyReset;
    var html = '<h3 tabindex="-1" data-setup-heading>' + esc(tr('setup.review.title')) + '</h3>' +
      '<p class="setup-lede">' + esc(tr('setup.review.text')) + '</p>' +
      '<div class="setup-review-grid"><div>' +
      '<h4 class="setup-subhead">' + esc(tr('setup.review.profiles')) + ' · ' + esc(tr('setup.choice.' + draft.choice)) + '</h4>' +
      profileSummaryHTML(draft.profiles) +
      '<h4 class="setup-subhead">' + esc(tr('routing.heading')) + '</h4>' + routingSummaryHTML() +
      '</div><div>' +
      '<h4 class="setup-subhead">' + esc(tr('setup.review.prefs')) + '</h4><ul class="setup-list">' +
      '<li><span>' + esc(draft.autoApprove ? tr('setup.review.autoOn') : tr('setup.review.autoOff')) + '</span></li>' +
      '<li><span>' + esc(tr('setup.review.concurrency', { n: Setup.toInt(draft.maxParallelPerOwner) })) + '</span></li>' +
      '<li><span>' + esc(tr('setup.review.reserve', { n: draft.kimiReservePercent })) + '</span></li>' +
      '<li><span>' + esc(tr('setup.review.monthly')) + ': ' + esc(mr.enabled ? tr('setup.review.monthlyOn', { day: mr.day, time: mr.time, zone: mr.timezone }) : tr('setup.review.monthlyOff')) + '</span></li>' +
      '</ul></div></div>' +
      '<h4 class="setup-subhead">' + esc(tr('setup.review.statusHeading')) + '</h4>';
    html += statuses.length ? '<ul class="setup-list">' + statuses.map(function (row) {
      return '<li><strong>' + esc(row.name) + '</strong>' +
        '<span class="' + (row.configured ? 'setup-ok' : 'setup-bad') + '">' + esc(tr('setup.review.configured')) + ' · ' + esc(tr(row.configured ? 'setup.yes' : 'setup.no')) + '</span>' +
        '<span class="' + (row.connected ? 'setup-ok' : 'setup-bad') + '">' + esc(tr('setup.review.connected')) + ' · ' + esc(tr(row.connected ? 'setup.yes' : 'setup.no')) + '</span>' +
        '<span class="muted">' + esc(tr('setup.review.request')) + ' · ' + esc(tr(Setup.telemetryKey(row.telemetry))) + '</span></li>';
    }).join('') + '</ul>' : '<p class="muted">' + esc(tr('provider.none')) + '</p>';
    html += '<p class="muted">' + esc(tr('setup.request.untested')) + '</p>';
    return html;
  }

  function alertHTML() {
    var html = '';
    if (refreshError)
      html += '<div class="setup-alert" role="alert"><p>' + esc(tr('setup.error.refreshFailed', { message: refreshError })) + '</p>' +
        '<p><button type="button" class="secondary-button" data-action="refresh-providers">' + esc(tr('setup.retry')) + '</button></p></div>';
    if (draft.errors && draft.errors.length)
      html += '<div class="setup-alert" role="alert"><ul>' + draft.errors.map(function (e) {
        return '<li>' + esc(tr(e.key, e.vars)) + '</li>';
      }).join('') + '</ul></div>';
    if (draft.status === 'error' && draft.error) {
      html += '<div class="setup-alert" role="alert"><p>' + esc(tr('setup.error.applyFailed', { message: draft.error })) + '</p>';
      if (draft.stale) html += '<p>' + esc(tr('setup.error.stale')) + '</p>';
      html += '</div>';
    }
    return html;
  }

  function warningsHTML() {
    var warns = draft.warnings && draft.warnings.length ? draft.warnings : Setup.profileWarnings(draft);
    if (!warns.length || draft.status === 'applied') return '';
    return '<div class="setup-warn" role="status"><ul>' + warns.map(function (w) {
      return '<li>' + esc(tr(w.key, w.vars)) + '</li>';
    }).join('') + '</ul></div>';
  }

  function actionsHTML() {
    var applying = draft.status === 'applying';
    var html = '<div class="setup-actions">' +
      '<button type="button" class="text-button" data-action="cancel"' + (applying ? ' disabled' : '') + '>' + esc(tr('setup.cancel')) + '</button>' +
      '<span class="setup-actions-spacer"></span>';
    if (draft.stale && draft.status === 'error')
      html += '<button type="button" class="secondary-button" data-action="reload">' + esc(tr('setup.reloadLatest')) + '</button>';
    if (draft.step > 0)
      html += '<button type="button" class="secondary-button" data-action="back"' + (applying ? ' disabled' : '') + '>' + esc(tr('setup.back')) + '</button>';
    if (draft.step < STEPS.length - 1)
      html += '<button type="button" class="primary-button" data-action="next"' + (applying ? ' disabled' : '') + '>' + esc(tr('setup.next')) + '</button>';
    else
      html += '<button type="button" class="primary-button" data-action="apply"' + (applying ? ' disabled' : '') + '>' + esc(draft.status === 'error' ? tr('setup.retry') : tr('setup.apply')) + '</button>';
    return html + '</div>';
  }

  function doneHTML() {
    return '<div class="setup-card" role="status"><h3>' + esc(tr('setup.done.title')) + '</h3>' +
      '<p class="setup-lede">' + esc(tr('setup.done.text')) + '</p>' +
      '<div class="setup-actions"><button type="button" class="primary-button" data-action="first-task">' + esc(tr('setup.done.firstTask')) + '</button>' +
      '<button type="button" class="secondary-button" data-action="tasks">' + esc(tr('setup.done.backToTasks')) + '</button></div></div>';
  }

  function progressHTML() {
    var applying = draft.status === 'applying';
    var items = STEPS.map(function (name, i) {
      var cls = i === draft.step ? 'current' : (i < draft.step ? 'done' : '');
      var current = i === draft.step ? ' aria-current="step"' : '';
      return '<li class="' + cls + '"><button type="button" data-goto-step="' + i + '"' + current + (applying ? ' disabled' : '') +
        ' aria-label="' + esc(tr('setup.stepLabel', { n: i + 1, name: tr('setup.step.' + name) })) + '">' +
        '<span class="setup-step-num" aria-hidden="true">' + (i + 1) + '</span><span class="setup-step-name">' + esc(tr('setup.step.' + name)) + '</span></button></li>';
    }).join('');
    return '<ol class="setup-progress" aria-label="' + esc(tr('setup.progress')) + '">' + items + '</ol>';
  }

  function render() {
    var html;
    if (loading) {
      html = '<div class="setup-card"><p class="muted">' + esc(tr('setup.loading')) + '</p></div>';
    } else if (loadingError) {
      html = '<div class="setup-card"><div class="setup-alert" role="alert"><p>' + esc(tr('setup.loadError', { message: loadingError.message || String(loadingError) })) + '</p>' +
        '<p><button type="button" class="secondary-button" data-action="retry-load">' + esc(tr('setup.retry')) + '</button></p></div></div>';
    } else if (!draft) {
      html = '';
    } else if (draft.status === 'applied') {
      html = progressHTML() + doneHTML();
    } else {
      var stepBody = [stepProvidersHTML, stepModelsHTML, stepPreferencesHTML, stepReviewHTML][draft.step]();
      var status = draft.status === 'applying' ? '<p class="muted" role="status">' + esc(tr('settings.applying')) + '</p>' : '';
      // Editing is disabled while an apply is in flight; the fieldset mirrors
      // that state to every control inside the step card.
      var disabled = draft.status === 'applying' ? ' disabled' : '';
      html = progressHTML() + alertHTML() + warningsHTML() +
        '<div class="setup-card setup-wizard"><fieldset class="setup-fields"' + disabled + '>' + stepBody + '</fieldset>' + status + '</div>' + actionsHTML();
    }
    root.innerHTML = html;
    var heading = root.querySelector('[data-setup-heading]');
    if (heading) heading.focus();
  }

  root.addEventListener('click', function (event) {
    var button = event.target.closest('[data-action],[data-goto-step]');
    if (!button || !root.contains(button)) return;
    if (button.dataset.gotoStep != null) {
      if (draft && draft.status !== 'applying') { Setup.goTo(draft, Number(button.dataset.gotoStep)); render(); }
      return;
    }
    var action = button.dataset.action;
    if (action === 'retry-load') { loadWizard(); return; }
    if (action === 'cancel') {
      if (draft) Setup.cancel(draft);
      draft = null;
      refreshError = null;
      switchView('tasks');
    } else if (!draft) {
      return;
    } else if (action === 'back') { Setup.back(draft); render(); }
    else if (action === 'next') { Setup.next(draft); render(); }
    else if (action === 'apply') { doApply(); }
    else if (action === 'reload') { reloadLatest(); }
    else if (action === 'refresh-providers') { refreshCatalog(); }
    else if (action === 'first-task') {
      switchView('tasks');
      var newTask = document.getElementById('new-task');
      if (newTask) newTask.click();
    } else if (action === 'tasks') { switchView('tasks'); }
  });

  root.addEventListener('change', function (event) {
    if (!draft || draft.status === 'applying') return;
    var el = event.target;
    if (el.name === 'setup-choice') { Setup.setChoice(draft, el.value); render(); return; }
    var profileModel = el.getAttribute('data-profile-model');
    if (profileModel) { Setup.setModel(draft, profileModel, el.value); render(); return; }
    var profileVariant = el.getAttribute('data-profile-variant');
    if (profileVariant) { Setup.setVariant(draft, profileVariant, el.value); render(); return; }
    var routingKey = el.getAttribute('data-routing');
    if (routingKey) { Setup.setRouting(draft, routingKey, el.value); render(); return; }
    if (el.getAttribute('data-pref') === 'autoApprove') Setup.setAutoApprove(draft, el.checked);
    var monthly = el.getAttribute('data-monthly');
    if (monthly === 'enabled') {
      Setup.setMonthlyReset(draft, { enabled: el.checked });
      root.querySelectorAll('[data-monthly="day"],[data-monthly="time"],[data-monthly="timezone"]').forEach(function (input) {
        input.disabled = !el.checked;
      });
    }
  });

  root.addEventListener('input', function (event) {
    if (!draft || draft.status === 'applying') return;
    var el = event.target;
    if (el.getAttribute('data-pref') === 'maxParallelPerOwner') Setup.setMaxParallelPerOwner(draft, el.value);
    var monthly = el.getAttribute('data-monthly');
    if (monthly === 'day') Setup.setMonthlyReset(draft, { day: el.value });
    else if (monthly === 'time') Setup.setMonthlyReset(draft, { time: el.value });
    else if (monthly === 'timezone') Setup.setMonthlyReset(draft, { timezone: el.value });
  });

  var navButton = document.querySelector('[data-view="setup"]');
  if (navButton) navButton.addEventListener('click', openWizard);

  if (banner) {
    var start = document.getElementById('setup-banner-start');
    var dismiss = document.getElementById('setup-banner-dismiss');
    if (start) start.addEventListener('click', function () { switchView('setup'); });
    if (dismiss) dismiss.addEventListener('click', function () { markDone(); updateBanner(); });
  }

  // Language changes re-render from the same draft: every selection survives.
  document.addEventListener('i18n:change', function () {
    updateBanner();
    var panel = document.getElementById('view-setup');
    if (panel && !panel.hidden && draft) render();
  });

  setInterval(updateBanner, 2000);
  updateBanner();
})(typeof window !== 'undefined' ? window : globalThis);
