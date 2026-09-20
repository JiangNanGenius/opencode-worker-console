#!/usr/bin/env node
/* Headless behavior tests for the Worker Desk setup wizard core (web/setup.js).

   Runs with plain Node (no extra runtime dependencies):
       node tests/web_setup.test.js

   The wizard core is pure; every network effect goes through the injected
   request function, which lets these tests prove that navigation and cancel
   never mutate the server and that only an explicit Apply sends one POST. */
'use strict';

const assert = require('node:assert/strict');
const path = require('node:path');

const REPO = path.join(__dirname, '..');
const Setup = require(path.join(REPO, 'web', 'setup.js'));

let passed = 0;
const failures = [];
function test(name, fn) {
  Promise.resolve()
    .then(fn)
    .then(() => { passed += 1; console.log('ok - ' + name); })
    .catch((err) => { failures.push({ name, err }); console.error('not ok - ' + name + '\n  ' + (err && err.stack || err)); });
}

function catalogFixture() {
  return {
    providers: [
      { id: 'deepseek', name: 'DeepSeek', connected: true, models: [
        { id: 'deepseek-flash', name: 'DeepSeek V4.1 Flash', variants: ['max', 'high', 'low'] }] },
      { id: 'kimi-for-coding', name: 'Kimi', connected: true, models: [
        { id: 'kimi-for-coding', name: 'Kimi K2.8', variants: ['max', 'high'] },
        { id: 'k3', name: 'Kimi K3', variants: ['max'] }] },
      { id: 'acme', name: 'Acme', connected: false, models: [
        { id: 'worker', name: 'Acme Worker', variants: [] }] }
    ]
  };
}

function settingsFixture() {
  return {
    revision: 3,
    auto_approve: true,
    max_parallel_per_owner: 4,
    kimi_reserve_percent: 20,
    profiles: {
      'fast-code': { model: 'deepseek/deepseek-flash', label: 'Flash', variant: 'high', enabled: true },
      'senior-code': { model: 'kimi-for-coding/kimi-for-coding', label: 'Kimi', variant: 'high', enabled: true },
      'deep-research': { model: 'kimi-for-coding/k3', label: 'K3', enabled: true },
      'my-custom': { model: 'acme/worker', label: 'Mine', enabled: false }
    },
    routing: { fast: 'fast-code', background: 'senior-code', deep: 'deep-research' },
    cleanup: { enabled: true, min_free_gb: 5, target_free_gb: 10, min_age_days: 30, keep_recent: 20, interval_seconds: 3600 },
    kimi_monthly_reset: { enabled: true, day: 19, time: '08:30', timezone: 'Asia/Shanghai' }
  };
}

function requestSpy(handler) {
  const calls = [];
  const fn = (path, options) => {
    calls.push({ path, options });
    return handler ? handler(path, options) : Promise.resolve({});
  };
  fn.calls = calls;
  return fn;
}

// -- navigation and cancel never touch the network --------------------------

test('next/back/goTo/cancel make zero network calls and keep the draft', async () => {
  const spy = requestSpy(() => { throw new Error('network must not be called'); });
  const draft = Setup.createDraft(settingsFixture(), catalogFixture());
  assert.equal(draft.step, 0);
  assert.equal(Setup.next(draft), true);   // providers -> models
  assert.equal(Setup.next(draft), true);   // models -> preferences
  Setup.setMaxParallelPerOwner(draft, 6);
  assert.equal(Setup.next(draft), true);   // preferences -> review
  assert.equal(draft.step, 3);
  assert.equal(Setup.next(draft), false);  // already on the last step
  assert.equal(Setup.back(draft), true);
  assert.equal(draft.step, 2);
  assert.equal(draft.maxParallelPerOwner, 6, 'back preserves the draft value');
  assert.equal(Setup.goTo(draft, 3), true);
  assert.equal(draft.step, 3);
  assert.equal(draft.maxParallelPerOwner, 6, 'forward navigation preserves the draft value');
  Setup.cancel(draft);
  assert.equal(draft.status, 'cancelled');
  assert.equal(spy.calls.length, 0, 'navigation and cancel never call request');
});

test('provider gate blocks Next until a provider is connected, without network', () => {
  const spy = requestSpy(() => { throw new Error('network must not be called'); });
  const catalog = catalogFixture();
  catalog.providers.forEach((p) => { p.connected = false; });
  const draft = Setup.createDraft(settingsFixture(), catalog);
  assert.equal(Setup.anyConnected(draft), false);
  assert.equal(Setup.next(draft), false);
  assert.equal(draft.step, 0);
  assert.deepEqual(draft.errors.map((e) => e.key), ['setup.providers.needOne']);
  assert.equal(spy.calls.length, 0);
});

// -- payload preservation ----------------------------------------------------

test('keep payload preserves every untouched setting byte-for-byte', () => {
  const settings = settingsFixture();
  const draft = Setup.createDraft(settings, catalogFixture());
  const payload = Setup.buildPayload(draft);
  assert.equal(payload.revision, 3);
  assert.equal(payload.auto_approve, true);
  assert.equal(payload.max_parallel_per_owner, 4);
  assert.equal(payload.kimi_reserve_percent, 20);
  assert.deepEqual(payload.profiles, {
    'fast-code': { model: 'deepseek/deepseek-flash', label: 'Flash', enabled: true, variant: 'high' },
    'senior-code': { model: 'kimi-for-coding/kimi-for-coding', label: 'Kimi', enabled: true, variant: 'high' },
    'deep-research': { model: 'kimi-for-coding/k3', label: 'K3', enabled: true },
    'my-custom': { model: 'acme/worker', label: 'Mine', enabled: false }
  });
  assert.deepEqual(payload.routing, settings.routing);
  assert.deepEqual(payload.cleanup, settings.cleanup);
  assert.deepEqual(payload.kimi_monthly_reset, settings.kimi_monthly_reset);
});

test('preset replaces only the three known profiles and preserves custom ones', () => {
  const draft = Setup.createDraft(settingsFixture(), catalogFixture());
  assert.equal(Setup.presetAvailable(draft), true);
  assert.equal(Setup.setChoice(draft, 'preset'), true);
  const payload = Setup.buildPayload(draft);
  assert.deepEqual(payload.profiles['fast-code'],
    { model: 'deepseek/deepseek-flash', label: 'DeepSeek V4.1 Flash', enabled: true, variant: 'max' });
  assert.deepEqual(payload.profiles['senior-code'],
    { model: 'kimi-for-coding/kimi-for-coding', label: 'Kimi K2.8 Preview', enabled: true, variant: 'max' });
  assert.deepEqual(payload.profiles['deep-research'],
    { model: 'kimi-for-coding/k3', label: 'Kimi K3', enabled: true, variant: 'max' });
  assert.deepEqual(payload.profiles['my-custom'], { model: 'acme/worker', label: 'Mine', enabled: false },
    'untouched custom profiles survive the preset');
  assert.deepEqual(payload.cleanup, settingsFixture().cleanup);
  assert.deepEqual(payload.routing, { fast: 'fast-code', background: 'senior-code', deep: 'deep-research' });
});

test('preset keeps max only when the catalog confirms it or lists no variants', () => {
  // Catalog lists variants without max: the preset must not invent it.
  const noMax = catalogFixture();
  noMax.providers[0].models[0].variants = ['high', 'low'];
  let draft = Setup.createDraft(settingsFixture(), noMax);
  Setup.setChoice(draft, 'preset');
  assert.equal(draft.profiles['fast-code'].variant, undefined, 'no nonexistent max variant');
  assert.equal(draft.profiles['senior-code'].variant, 'max', 'kimi catalog still lists max');
  // Catalog lists no variant information at all: the documented preset default stays.
  const noInfo = catalogFixture();
  noInfo.providers.forEach((p) => p.models.forEach((m) => { m.variants = []; }));
  draft = Setup.createDraft(settingsFixture(), noInfo);
  Setup.setChoice(draft, 'preset');
  assert.equal(draft.profiles['fast-code'].variant, 'max');
  assert.equal(draft.profiles['deep-research'].variant, 'max');
});

test('custom mapping only offers catalog variants and never invents max', () => {
  const draft = Setup.createDraft(settingsFixture(), catalogFixture());
  Setup.setChoice(draft, 'custom');
  Setup.setModel(draft, 'fast-code', 'acme/worker');
  assert.equal(draft.profiles['fast-code'].variant, undefined, 'variant resets on model change');
  assert.equal(Setup.setVariant(draft, 'fast-code', 'max'), false, 'acme lists no variants; max is rejected');
  Setup.setModel(draft, 'fast-code', 'kimi-for-coding/k3');
  assert.equal(Setup.setVariant(draft, 'fast-code', 'high'), false, 'k3 does not list high');
  assert.equal(Setup.setVariant(draft, 'fast-code', 'max'), true, 'k3 lists max');
  assert.equal(draft.profiles['fast-code'].variant, 'max');
  assert.equal(Setup.setVariant(draft, 'fast-code', ''), true, 'empty selects the provider default');
  assert.equal(draft.profiles['fast-code'].variant, undefined);
});

test('custom choice creates missing slots and preserves edits when reselected', () => {
  const settings = settingsFixture();
  delete settings.profiles['fast-code'];
  const draft = Setup.createDraft(settings, catalogFixture());
  Setup.setChoice(draft, 'custom');
  assert.equal(draft.profiles['fast-code'].model, '');
  assert.ok(Setup.stepErrors(draft, 1).some((e) => e.key === 'setup.error.modelRequired'));
  Setup.setModel(draft, 'fast-code', 'kimi-for-coding/k3');
  Setup.setRouting(draft, 'fast', 'senior-code');
  Setup.setChoice(draft, 'custom');
  assert.equal(draft.profiles['fast-code'].model, 'kimi-for-coding/k3');
  assert.equal(draft.routing.fast, 'senior-code');
  assert.equal(draft.touched.profiles['deep-research'], undefined);
});

test('preset is refused when its models are absent from the catalog', () => {
  const catalog = catalogFixture();
  catalog.providers = catalog.providers.filter((p) => p.id !== 'kimi-for-coding');
  const draft = Setup.createDraft(settingsFixture(), catalog);
  assert.equal(Setup.presetAvailable(draft), false);
  assert.equal(Setup.setChoice(draft, 'preset'), false);
  assert.equal(draft.choice, 'keep');
});

// -- retained configuration: warnings, not errors ----------------------------

test('retained unknown models warn but never block; touched unknown models error', () => {
  const settings = settingsFixture();
  settings.profiles['legacy-x'] = { model: 'ghost/provider', label: 'Legacy', enabled: true };
  const draft = Setup.createDraft(settings, catalogFixture());
  assert.deepEqual(Setup.stepErrors(draft, 1), [], 'retained profile is not a blocking error');
  const warnings = Setup.profileWarnings(draft);
  assert.equal(warnings.length, 1);
  assert.equal(warnings[0].key, 'setup.warn.modelUnverified');
  assert.equal(warnings[0].vars.model, 'ghost/provider');
  // Preferences can change and the payload still builds with the legacy profile intact.
  Setup.setMaxParallelPerOwner(draft, 8);
  const payload = Setup.buildPayload(draft);
  assert.equal(payload.max_parallel_per_owner, 8);
  assert.deepEqual(payload.profiles['legacy-x'], { model: 'ghost/provider', label: 'Legacy', enabled: true });
  // Editing the legacy profile in the wizard turns the same gap into an error.
  Setup.setChoice(draft, 'custom');
  Setup.setModel(draft, 'legacy-x', 'ghost/provider');
  const errors = Setup.stepErrors(draft, 1);
  assert.ok(errors.some((e) => e.key === 'setup.error.modelUnknown'), 'wizard-selected unknown model is an error');
});

test('routing to a missing or disabled profile is invalid', () => {
  const draft = Setup.createDraft(settingsFixture(), catalogFixture());
  Setup.setRouting(draft, 'deep', 'my-custom'); // disabled profile
  assert.ok(Setup.stepErrors(draft, 1).some((e) => e.key === 'setup.error.routingInvalid'));
  Setup.setRouting(draft, 'deep', 'ghost');
  assert.ok(Setup.stepErrors(draft, 1).some((e) => e.key === 'setup.error.routingInvalid'));
  Setup.setRouting(draft, 'deep', 'deep-research');
  assert.deepEqual(Setup.stepErrors(draft, 1), []);
});

// -- invalid input ------------------------------------------------------------

test('concurrency and monthly reset input are validated with i18n keys', () => {
  const draft = Setup.createDraft(settingsFixture(), catalogFixture());
  for (const bad of [0, 17, 'abc', '', 2.5, true, null]) {
    Setup.setMaxParallelPerOwner(draft, bad);
    assert.ok(Setup.stepErrors(draft, 2).some((e) => e.key === 'setup.error.concurrencyRange'), 'rejects ' + JSON.stringify(bad));
  }
  Setup.setMaxParallelPerOwner(draft, 16);
  assert.deepEqual(Setup.stepErrors(draft, 2), []);
  Setup.setMonthlyReset(draft, { enabled: true, day: 32 });
  assert.ok(Setup.stepErrors(draft, 2).some((e) => e.key === 'setup.error.monthlyDay'));
  Setup.setMonthlyReset(draft, { day: 15, time: '24:00' });
  assert.ok(Setup.stepErrors(draft, 2).some((e) => e.key === 'setup.error.monthlyTime'));
  Setup.setMonthlyReset(draft, { time: '23:59', timezone: '  ' });
  assert.ok(Setup.stepErrors(draft, 2).some((e) => e.key === 'setup.error.monthlyZone'));
  Setup.setMonthlyReset(draft, { timezone: 'UTC' });
  assert.deepEqual(Setup.stepErrors(draft, 2), []);
  // Monthly fields are only validated when the schedule is enabled.
  Setup.setMonthlyReset(draft, { enabled: false, day: 99, time: 'junk', timezone: '' });
  assert.deepEqual(Setup.stepErrors(draft, 2), []);
});

// -- apply: success, failure, retry, stale recovery ---------------------------

test('successful apply sends exactly one POST with the built payload', async () => {
  const draft = Setup.createDraft(settingsFixture(), catalogFixture());
  const saved = Object.assign({}, settingsFixture(), { revision: 4 });
  const spy = requestSpy(() => Promise.resolve(saved));
  const result = await Setup.apply(draft, spy);
  assert.equal(result.ok, true);
  assert.equal(draft.status, 'applied');
  assert.equal(spy.calls.length, 1);
  assert.equal(spy.calls[0].path, '/console-api/settings');
  assert.equal(spy.calls[0].options.method, 'POST');
  const body = JSON.parse(spy.calls[0].options.body);
  assert.deepEqual(body, Setup.buildPayload(draft));
  assert.equal(body.revision, 3);
});

test('failed apply keeps the draft recoverable and a retry succeeds', async () => {
  const draft = Setup.createDraft(settingsFixture(), catalogFixture());
  Setup.setChoice(draft, 'custom');
  Setup.setModel(draft, 'fast-code', 'kimi-for-coding/k3');
  Setup.goTo(draft, 3);
  let fail = true;
  const spy = requestSpy(() => fail
    ? Promise.reject(new Error('Refusing to change settings while pool tasks are queued or active'))
    : Promise.resolve({ revision: 4 }));
  const first = await Setup.apply(draft, spy);
  assert.equal(first.ok, false);
  assert.equal(first.stale, false);
  assert.equal(draft.status, 'error');
  assert.match(draft.error, /queued or active/);
  assert.equal(draft.step, 3, 'stay on review for a recoverable retry');
  assert.equal(draft.profiles['fast-code'].model, 'kimi-for-coding/k3', 'draft edits survive the failure');
  fail = false;
  const second = await Setup.apply(draft, spy);
  assert.equal(second.ok, true);
  assert.equal(draft.status, 'applied');
  assert.equal(spy.calls.length, 2, 'only explicit Apply clicks send requests');
});

test('stale revision flags reload and merge keeps edits but adopts untouched fields', async () => {
  const draft = Setup.createDraft(settingsFixture(), catalogFixture());
  Setup.setAutoApprove(draft, false);                       // user edit: keep
  Setup.setChoice(draft, 'custom');
  Setup.setModel(draft, 'fast-code', 'kimi-for-coding/k3'); // user edit: keep
  const spy = requestSpy(() => Promise.reject(new Error('Settings changed elsewhere; reload before saving')));
  const result = await Setup.apply(draft, spy);
  assert.equal(result.ok, false);
  assert.equal(result.stale, true);
  assert.equal(draft.stale, true);
  const fresh = settingsFixture();
  fresh.revision = 9;
  fresh.auto_approve = true;
  fresh.max_parallel_per_owner = 9;                          // external change: adopt
  fresh.kimi_reserve_percent = 35;                           // external change: adopt
  fresh.profiles['extern'] = { model: 'acme/worker', label: 'External', enabled: true }; // external add: adopt
  delete fresh.profiles['deep-research'];                    // external delete, untouched by user: adopt
  fresh.routing.deep = 'senior-code';                       // external configuration remains valid
  Setup.mergeFreshSettings(draft, fresh);
  assert.equal(draft.revision, 9);
  assert.equal(draft.stale, false);
  assert.equal(draft.status, 'editing');
  assert.equal(draft.autoApprove, false, 'user autoApprove edit survives the merge');
  assert.equal(draft.maxParallelPerOwner, 9, 'untouched concurrency adopts the fresh value');
  assert.equal(draft.kimiReservePercent, 35, 'reserve is never wizard-edited: always fresh');
  assert.equal(draft.profiles['fast-code'].model, 'kimi-for-coding/k3', 'user model edit survives');
  assert.ok(draft.profiles['extern'], 'externally added profiles are preserved');
  assert.equal(draft.profiles['deep-research'], undefined, 'untouched external deletion is honored');
  const payload = Setup.buildPayload(draft);
  assert.equal(payload.revision, 9);
  assert.equal(payload.auto_approve, false);
  assert.equal(payload.max_parallel_per_owner, 9);
});

test('keep choice plus stale reload adopts external profile additions and removals', () => {
  const draft = Setup.createDraft(settingsFixture(), catalogFixture());
  assert.equal(draft.choice, 'keep');
  const fresh = settingsFixture();
  fresh.revision = 7;
  fresh.profiles['extern'] = { model: 'acme/worker', label: 'External', enabled: true };
  delete fresh.profiles['my-custom'];
  Setup.mergeFreshSettings(draft, fresh);
  assert.ok(draft.profiles['extern'], 'keep must not discard externally added profiles');
  assert.equal(draft.profiles['my-custom'], undefined, 'keep honors external deletions');
  assert.equal(Setup.buildPayload(draft).revision, 7);
});

test('apply during apply is a no-op (no double POST)', async () => {
  const draft = Setup.createDraft(settingsFixture(), catalogFixture());
  let release;
  const spy = requestSpy(() => new Promise((resolve) => { release = resolve; }));
  const first = Setup.apply(draft, spy);
  assert.equal(draft.status, 'applying');
  const second = await Setup.apply(draft, spy);
  assert.deepEqual(second, { ok: false, busy: true });
  assert.equal(spy.calls.length, 1, 'a second Apply while applying sends nothing');
  release({ revision: 4 });
  const result = await first;
  assert.equal(result.ok, true);
});

test('validation failure inside apply never reaches the network', async () => {
  const draft = Setup.createDraft(settingsFixture(), catalogFixture());
  Setup.setMaxParallelPerOwner(draft, 0);
  const spy = requestSpy(() => { throw new Error('network must not be called'); });
  const result = await Setup.apply(draft, spy);
  assert.equal(result.ok, false);
  assert.equal(result.validation, true);
  assert.ok(result.errors.some((e) => e.key === 'setup.error.concurrencyRange'));
  assert.equal(spy.calls.length, 0);
});

// -- truthful provider status -------------------------------------------------

test('provider status separates configured, connected and quota telemetry', () => {
  const draft = Setup.createDraft(settingsFixture(), catalogFixture());
  Setup.setChoice(draft, 'custom');
  Setup.setModel(draft, 'fast-code', 'ghost/provider'); // touched but unknown: unconfigured
  const quota = {
    deepseek: { available: true, state: 'ok' },
    'kimi-for-coding': { available: false, state: 'billing_blocked', monthly_plan_exhausted: true,
                         billing: { reason: 'monthly_usage_limit' } }
  };
  const rows = Setup.providerStatus(draft, quota);
  const byId = Object.fromEntries(rows.map((r) => [r.provider, r]));
  assert.equal(byId['ghost'].configured, false);
  assert.equal(byId['ghost'].connected, false);
  assert.equal(byId['ghost'].telemetry, 'unknown');
  assert.equal(byId['kimi-for-coding'].configured, true);
  assert.equal(byId['kimi-for-coding'].connected, true);
  assert.equal(byId['kimi-for-coding'].telemetry, 'monthly');
  assert.equal(byId['deepseek'], undefined, 'unused providers are not listed');
  // Quota availability maps to the quota-telemetry label, never to inference success.
  assert.equal(Setup.telemetryKey('available'), 'setup.request.quotaAvailable');
  assert.equal(Setup.telemetryKey('unknown'), 'setup.request.untested');
  assert.equal(Setup.telemetryKey('monthly'), 'setup.request.monthly');
  const deep = Setup.createDraft(settingsFixture(), catalogFixture());
  const okRows = Setup.providerStatus(deep, { deepseek: { available: true, state: 'ok' } });
  assert.equal(Object.fromEntries(okRows.map((r) => [r.provider, r]))['deepseek'].telemetry, 'available');
});

// -- language change keeps the draft -------------------------------------------

test('switching language mid-wizard keeps every selection and error keys stay stable', () => {
  // Load the real translation bundle with a minimal window/document stub.
  const stub = {
    navigator: { languages: ['en'] },
    localStorage: { getItem: () => null, setItem: () => {} },
    document: {
      documentElement: {},
      querySelectorAll: () => [],
      dispatchEvent: () => true
    }
  };
  global.window = stub;
  require(path.join(REPO, 'web', 'i18n.js'));
  const I18n = stub.I18n;
  try {
    const draft = Setup.createDraft(settingsFixture(), catalogFixture());
    Setup.setChoice(draft, 'custom');
    Setup.setModel(draft, 'fast-code', 'kimi-for-coding/k3');
    Setup.setVariant(draft, 'fast-code', 'max');
    Setup.setAutoApprove(draft, false);
    Setup.setMaxParallelPerOwner(draft, 0); // force a validation error
    const beforeErrors = Setup.stepErrors(draft, 2).map((e) => e.key);
    assert.equal(I18n.getLocale(), 'en');
    I18n.setLocale('zh-CN');
    assert.equal(I18n.getLocale(), 'zh-CN');
    assert.equal(draft.profiles['fast-code'].model, 'kimi-for-coding/k3', 'locale change keeps model selection');
    assert.equal(draft.profiles['fast-code'].variant, 'max', 'locale change keeps variant selection');
    assert.equal(draft.autoApprove, false, 'locale change keeps preferences');
    assert.deepEqual(Setup.stepErrors(draft, 2).map((e) => e.key), beforeErrors, 'error keys are locale-independent');
    assert.notEqual(I18n.t('setup.step.review'), 'setup.step.review', 'zh-CN translation exists');
    assert.notEqual(I18n.t('setup.review.title'), 'setup.review.title', 'zh-CN review title exists');
    assert.notEqual(I18n.t(beforeErrors[0]), beforeErrors[0], 'zh-CN error translation exists');
    I18n.setLocale('en');
    assert.equal(draft.step, 0, 'wizard state untouched by locale switches');
  } finally {
    delete global.window;
  }
});

test('ordered routing survives setup and refreshed settings', () => {
  const settings = settingsFixture();
  settings.routing_policy = { background: [[{ profile: 'senior-code', weight: 1 }], [{ profile: 'fast-code', weight: 1 }]] };
  const draft = Setup.createDraft(settings, catalogFixture());
  assert.deepEqual(Setup.buildPayload(draft).routing_policy, settings.routing_policy);
  const fresh = settingsFixture();
  fresh.revision += 1;
  fresh.routing_policy = { deep: [[{ profile: 'deep-research', weight: 2 }, { profile: 'senior-code', weight: 1 }]] };
  Setup.mergeFreshSettings(draft, fresh);
  assert.deepEqual(Setup.buildPayload(draft).routing_policy, fresh.routing_policy);
});

// -- summary ------------------------------------------------------------------

process.on('exit', () => {
  if (failures.length) {
    console.error(failures.length + ' of ' + (passed + failures.length) + ' wizard behavior tests failed');
    process.exitCode = 1;
  } else {
    console.log(passed + ' wizard behavior tests passed');
  }
});
