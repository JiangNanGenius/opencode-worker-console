'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const Voice = require('../web/voice-settings.js');

test('voice controls start even while model settings and Kimi quota are pending', async () => {
  const source = require('node:fs').readFileSync(require('node:path').join(__dirname, '../web/manage.js'), 'utf8');
  const definition = source.match(/^async function loadModelControls\(\).*$/m)[0];
  const calls = [];
  let finishSettings, finishQuota;
  const context = require('node:vm').createContext({
    loadSettings: () => { calls.push('settings'); return new Promise(resolve => { finishSettings = resolve; }); },
    loadKimiAccount: () => { calls.push('quota'); return new Promise(resolve => { finishQuota = resolve; }); },
    voiceController: { start: async () => { calls.push('voice'); } }
  });
  require('node:vm').runInContext(definition, context);
  const loading = context.loadModelControls();
  assert.deepEqual(calls, ['settings', 'quota', 'voice']);
  finishSettings();
  finishQuota();
  await loading;
});

const messages = {
  'localVoice.dialect.sichuan_dialect': 'Sichuan dialect',
  'localVoice.dialect.beijing_dialect': 'Beijing dialect',
  'localVoice.available': 'Ready · {speaker}',
  'localVoice.availableUnknownSpeaker': 'Runtime ready; speaker status unknown',
  'localVoice.unavailable.model_missing': 'Model missing. {detail}',
  'localVoice.unavailable.unknown': 'Unavailable. {detail}',
  'localVoice.warning.invalidSavedVoice': 'Saved voice unsupported',
  'localVoice.last': 'Last run: {detail}',
  'localVoice.lastNone': 'No preview or notification has run yet.',
  'localVoice.lastStatus.played': 'played',
  'localVoice.lastStatus.failed': 'failed',
  'localVoice.audioSeconds': '{seconds}s audio',
  'localVoice.totalSeconds': '{seconds}s total',
  'localVoice.backend.auto': 'Automatic (cloud first)',
  'localVoice.backend.cloud': 'Cloud only',
  'localVoice.backend.local': 'Local only',
  'localVoice.availableCloud': 'Cloud-first ready · {speaker}',
  'localVoice.availableCloudUnknownSpeaker': 'Cloud-first ready; speaker status unknown',
  'localVoice.readyLocalOnly': 'Cloud key not configured; automatic mode uses the local fallback · {speaker}',
  'localVoice.readyLocalOnlyUnknownSpeaker': 'Cloud key not configured; automatic mode uses the local fallback; speaker status unknown',
  'localVoice.cloudOnly': 'Cloud ready; local fallback unavailable. {detail}',
  'localVoice.lastBackend.cloud': 'cloud',
  'localVoice.lastBackend.local': 'local',
  'localVoice.lastBackend.auto': 'auto',
  'localVoice.cloudStatus.ok': 'cloud OK',
  'localVoice.fallbackDetail': 'cloud unavailable, used local fallback: {reason}',
  'localVoice.unknownReason': 'unknown reason',
  'localVoice.currentSummary': 'Current: {backend} · cloud {cloud} · local {local} · rate {rate}',
  'localVoice.saved': 'Saved: {detail}',
  'localVoice.previewStarted': 'Synthesizing…',
  'localVoice.previewDone': 'Preview finished: {detail}',
  'localVoice.previewFailed': 'Preview failed: {detail}',
  'localVoice.pollStopped': 'poll timeout',
  'localVoice.pollFailed': 'poll failed: {message}',
  'localVoice.stopRequested': 'stop requested',
  'localVoice.idle': 'idle',
  'localVoice.alreadyRunning': 'already running',
  'localVoice.busy': 'busy',
  'localVoice.localUnavailableOption': 'Local voice unavailable',
  'localVoice.warning.invalidSavedSettings': 'Saved settings invalid',
  'localVoice.warning.invalidSavedRate': 'Saved rate invalid',
  'localVoice.warning.invalidSavedBackend': 'Saved backend invalid',
  'localVoice.warning.invalidSavedCloudVoice': 'Saved cloud voice invalid'
};
const tr = (key, vars) => {
  let value = messages[key] ?? key;
  if (vars) value = value.replace(/\{(\w+)\}/g, (match, name) => (name in vars ? String(vars[name]) : match));
  return value;
};
const voices = [
  { id: 'serena', label: 'Serena', dialect: null },
  { id: 'vivian', label: 'Vivian', dialect: null },
  { id: 'uncle_fu', label: 'Uncle Fu', dialect: null },
  { id: 'eric', label: 'Eric', dialect: 'sichuan_dialect' },
  { id: 'dylan', label: 'Dylan', dialect: 'beijing_dialect' }
];

test('voice labels use installed ids with only verified dialects', () => {
  assert.equal(Voice.humanizeVoiceId('uncle_fu'), 'Uncle Fu');
  assert.equal(Voice.humanizeVoiceId('ono_anna'), 'Ono Anna');
  assert.equal(Voice.voiceLabel(voices[3], tr), 'Eric · Sichuan dialect');
  assert.equal(Voice.voiceLabel(voices[4], tr), 'Dylan · Beijing dialect');
  assert.equal(Voice.voiceLabel(voices[0], tr), 'Serena');
  assert.equal(Voice.voiceLabelFor('uncLe_fu', voices, tr), 'Uncle Fu');
  assert.equal(Voice.voiceLabelFor('vivian', voices, tr), 'Vivian');
  assert.equal(Voice.voiceLabelFor('', voices, tr), '—');
});

test('view state maps availability, default source and bounded controls', () => {
  const mapped = Voice.viewState({
    available: true, default_voice: 'serena', default_source: 'saved',
    voices, speaker: { name: 'Mac mini扬声器' }, preview: { running: false, owned: false }
  }, tr);
  assert.equal(mapped.available, true);
  assert.equal(mapped.defaultLabel, 'Serena');
  assert.equal(mapped.defaultSource, 'saved');
  assert.equal(mapped.canSave, true);
  assert.equal(mapped.canPreview, true);
  assert.equal(mapped.canStop, false);
  assert.equal(mapped.availability, 'Ready · Mac mini扬声器');

  const playing = Voice.viewState({
    available: true, default_voice: 'vivian', voices,
    preview: { running: true, owned: true }
  }, tr);
  assert.equal(playing.running, true);
  assert.equal(playing.canPreview, false);
  assert.equal(playing.canStop, true);

  const foreign = Voice.viewState({
    available: true, default_voice: 'vivian', voices,
    preview: { running: true, owned: false }
  }, tr);
  assert.equal(foreign.canStop, false, 'never offer stop for another client\'s playback');
  assert.equal(foreign.canPreview, false);

  const unavailable = Voice.viewState({
    available: false, reason: 'model_missing', detail: 'incomplete', voices: [],
    preview: { running: false }
  }, tr);
  assert.equal(unavailable.canSave, false);
  assert.equal(unavailable.canPreview, false);
  assert.equal(unavailable.canStop, false);
  assert.match(unavailable.availability, /Model missing/);

  const legacy = Voice.viewState({ available: true, default_voice: 'Vivian', voices: [],
    warning: 'invalid_saved_voice', preview: {} }, tr);
  assert.equal(legacy.defaultSource, 'legacy');
  assert.equal(legacy.warning, 'Saved voice unsupported');
  assert.equal(legacy.defaultLabel, 'Vivian');
});

test('last run text distinguishes played and failed without hiding errors', () => {
  const played = Voice.lastRunText({ status: 'played', voice: 'vivian', audio_seconds: 4.24, total_seconds: 9.575 }, voices, tr);
  assert.match(played, /played/);
  assert.match(played, /Vivian/);
  assert.match(played, /4\.2s audio/);
  assert.match(played, /9\.6s total/);
  const failed = Voice.lastRunText({ status: 'failed', voice: 'eric', error: '播报已停止' }, voices, tr);
  assert.match(failed, /failed/);
  assert.match(failed, /Eric/);
  assert.match(failed, /播报已停止/);
  assert.equal(Voice.lastRunText(null, voices, tr), 'No preview or notification has run yet.');
});

test('selection survives status polls and only a save resets it', () => {
  assert.equal(Voice.nextSelection('dylan', 'vivian', voices, false), 'dylan');
  assert.equal(Voice.nextSelection('dylan', 'vivian', voices, true), 'vivian');
  assert.equal(Voice.nextSelection('removed_voice', 'serena', voices, false), 'serena');
  assert.equal(Voice.nextSelection('', 'Vivian', voices, false), 'vivian');
  assert.equal(Voice.nextSelection('', 'vivian', [], false), 'vivian');
});

const cloudVoices = [
  { id: 'zh_female_vv_uranus_bigtts', label: 'Vivi 2.0' },
  { id: 'zh_female_gaolengyujie_uranus_bigtts', label: '高冷御姐 2.0' }
];

test('rate normalization is bounded and formatted', () => {
  assert.equal(Voice.normalizeRate(undefined, 1.1), 1.1);
  assert.equal(Voice.normalizeRate('', 1.1), 1.1);
  assert.equal(Voice.normalizeRate(0.2, 1.1), 0.5);
  assert.equal(Voice.normalizeRate(2.5, 1.1), 2.0);
  assert.equal(Voice.normalizeRate(1.15, 1.1), 1.15);
  assert.equal(Voice.rateLabel(1.1), '×1.10');
  assert.equal(Voice.rateLabel(0.5), '×0.50');
  assert.equal(Voice.backendLabel('cloud', tr), 'Cloud only');
  assert.equal(Voice.effectiveBackendLabel('cloud', tr), 'cloud');
});

test('cloud-aware view state exposes both voices and honest fallback controls', () => {
  const view = {
    available: true, voices, cloud_voices: cloudVoices,
    cloud_key_configured: true, cloud_default: 'zh_female_vv_uranus_bigtts',
    cloud_voice: 'zh_female_gaolengyujie_uranus_bigtts',
    default_voice: 'serena', backend: 'auto', rate: 1.2,
    speaker: { name: 'Mac mini扬声器' }, preview: { running: false, owned: false }
  };
  const mapped = Voice.viewState(view, tr);
  assert.equal(mapped.hasCloud, true);
  assert.equal(mapped.cloudAvailable, true);
  assert.equal(mapped.keyConfigured, true);
  assert.equal(mapped.canSave, true);
  assert.equal(mapped.canPreview, true);
  assert.equal(mapped.backend, 'auto');
  assert.equal(mapped.rate, 1.2);
  assert.equal(mapped.rateLabel, '×1.20');
  assert.equal(mapped.cloudLabel, '高冷御姐 2.0');
  assert.equal(mapped.availability, 'Cloud-first ready · Mac mini扬声器');

  const cloudOnly = Voice.viewState({ ...view, available: false, voices: [], detail: 'model missing' }, tr, 'cloud');
  assert.equal(cloudOnly.canPreview, true);
  assert.equal(cloudOnly.canSave, true);
  assert.match(cloudOnly.availability, /Cloud ready/);
  const cloudOnlyLocal = Voice.viewState({ ...view, available: false, voices: [] }, tr, 'local');
  assert.equal(cloudOnlyLocal.canPreview, false);

  const noKey = Voice.viewState({ ...view, cloud_key_configured: false }, tr, 'cloud');
  assert.equal(noKey.cloudAvailable, false);
  assert.equal(noKey.canPreview, false);
  const noKeyAuto = Voice.viewState({ ...view, cloud_key_configured: false }, tr, 'auto');
  assert.equal(noKeyAuto.canPreview, true);
  assert.match(noKeyAuto.availability, /automatic mode uses the local fallback/);
  const bothMissing = Voice.viewState({ ...view, available: false, voices: [], cloud_key_configured: false }, tr);
  assert.equal(bothMissing.canPreview, false);
  const nothing = Voice.viewState({ available: false, voices: [], cloud_voices: [], preview: {} }, tr);
  assert.equal(nothing.canSave, false);
});

test('last run text shows effective backend, rate and cloud fallback', () => {
  const fallback = Voice.lastRunText({
    status: 'played', backend: 'local', voice: 'vivian', rate: 1.1,
    audio_seconds: 2.5, total_seconds: 4.2, fallback: true,
    fallback_detail: 'response timeout'
  }, voices, tr, cloudVoices);
  assert.match(fallback, /local/);
  assert.match(fallback, /Vivian/);
  assert.match(fallback, /×1\.10/);
  assert.match(fallback, /used local fallback: response timeout/);
  const cloud = Voice.lastRunText({
    status: 'played', backend: 'cloud',
    voice: 'zh_female_vv_uranus_bigtts', rate: 1.2,
    cloud_status: { status: 'ok' }
  }, voices, tr, cloudVoices);
  assert.match(cloud, /cloud/);
  assert.match(cloud, /Vivi 2\.0/);
  assert.match(cloud, /×1\.20/);
  assert.match(cloud, /cloud OK/);
});

test('cloud voice catalog and selection follow either payload shape', () => {
  assert.equal(Voice.cloudVoices({ cloud_voices: cloudVoices }).length, 2);
  assert.equal(Voice.cloudVoices({ cloud: { voices: cloudVoices } }).length, 2);
  assert.equal(Voice.cloudVoices({}).length, 0);
  assert.equal(Voice.nextSelection('zh_female_gaolengyujie_uranus_bigtts',
    'zh_female_vv_uranus_bigtts', cloudVoices, false),
  'zh_female_gaolengyujie_uranus_bigtts');
  assert.equal(Voice.nextSelection('zh_female_gaolengyujie_uranus_bigtts',
    'zh_female_vv_uranus_bigtts', cloudVoices, true),
  'zh_female_vv_uranus_bigtts');
});

test('new settings warnings are surfaced together', () => {
  const mapped = Voice.viewState({
    available: true, voices, cloud_voices: cloudVoices, cloud_key_configured: true,
    warnings: ['invalid_saved_rate', 'invalid_saved_backend'], preview: {}
  }, tr);
  assert.match(mapped.warning, /Saved rate invalid/);
  assert.match(mapped.warning, /Saved backend invalid/);
});

class FakeElement {
  constructor(tag) {
    this.tagName = tag;
    this.children = [];
    this.value = '';
    this.textContent = '';
    this.hidden = false;
    this.disabled = false;
    this.listeners = {};
    this._innerHTML = '';
    this.classList = { toggle() {} };
  }
  appendChild(child) { this.children.push(child); return child; }
  set innerHTML(value) { this._innerHTML = value; if (value === '') this.children = []; }
  get innerHTML() { return this._innerHTML; }
  addEventListener(name, handler) { this.listeners[name] = handler; }
}

test('mounted card renders cloud and local controls and posts validated settings', async () => {
  const ids = ['local-voice-select', 'local-voice-state', 'local-voice-availability',
    'local-voice-current', 'local-voice-save', 'local-voice-preview', 'local-voice-stop',
    'local-voice-message', 'local-voice-last', 'local-voice-backend', 'local-voice-rate',
    'local-voice-rate-value', 'local-voice-cloud-voice', 'local-voice-backend-field',
    'local-voice-rate-field', 'local-voice-cloud-field'];
  const nodes = {};
  ids.forEach((id) => {
    const tag = id === 'local-voice-rate' ? 'input'
      : id === 'local-voice-rate-value' ? 'output'
        : id.indexOf('-field') >= 0 || id === 'local-voice-current' ? 'div' : 'select';
    nodes[id] = new FakeElement(tag);
  });
  const document = {
    createElement: (tag) => new FakeElement(tag),
    getElementById: (id) => nodes[id] || null
  };
  const globalDocument = global.document;
  global.document = document;
  const posts = [];
  const view = {
    available: true, voices, cloud_voices: cloudVoices, cloud_key_configured: true,
    cloud_default: 'zh_female_vv_uranus_bigtts',
    cloud_voice: 'zh_female_gaolengyujie_uranus_bigtts',
    default_voice: 'serena', backend: 'cloud', rate: 1.2,
    speaker: { name: 'Mac mini扬声器' }, preview: { running: false, owned: false },
    last: null, warnings: []
  };
  const request = async (path, options) => {
    if (options && options.method === 'POST') {
      posts.push(JSON.parse(options.body));
      return { voice: 'serena', cloud_voice: cloudVoices[1].id, backend: 'local',
        rate: 1.35, warnings: [] };
    }
    return view;
  };
  try {
    const controller = Voice.mount({ $: (id) => nodes[id] || null, request, tr,
      showToast: () => {} });
    assert.ok(controller);
    controller.bind();
    await controller.start();
    assert.equal(nodes['local-voice-backend'].value, 'cloud');
    assert.equal(nodes['local-voice-rate'].value, '1.2');
    assert.equal(nodes['local-voice-rate-value'].textContent, '×1.20');
    assert.equal(nodes['local-voice-cloud-voice'].value, 'zh_female_gaolengyujie_uranus_bigtts');
    assert.equal(nodes['local-voice-select'].value, 'serena');
    assert.equal(nodes['local-voice-save'].disabled, false);
    assert.equal(nodes['local-voice-preview'].disabled, false);
    assert.match(nodes['local-voice-availability'].textContent, /Cloud-first ready/);
    assert.match(nodes['local-voice-current'].textContent, /Cloud only/);
    assert.match(nodes['local-voice-current'].textContent, /高冷御姐 2\.0/);
    assert.match(nodes['local-voice-current'].textContent, /×1\.20/);

    nodes['local-voice-rate'].value = '1.35';
    nodes['local-voice-rate'].listeners.input();
    assert.equal(nodes['local-voice-rate-value'].textContent, '×1.35');
    nodes['local-voice-backend'].value = 'local';
    nodes['local-voice-backend'].listeners.change();
    assert.match(nodes['local-voice-current'].textContent, /Local only/);
    assert.equal(nodes['local-voice-preview'].disabled, false);

    nodes['local-voice-save'].listeners.click();
    for (let i = 0; i < 4; i += 1) await new Promise((resolve) => setTimeout(resolve, 0));
    assert.equal(posts.length, 1);
    assert.deepEqual(posts[0], {
      action: 'save', backend: 'local', rate: 1.35,
      cloud_voice: 'zh_female_gaolengyujie_uranus_bigtts', voice: 'serena'
    });
    assert.match(nodes['local-voice-message'].textContent, /Saved:/);
  } finally {
    if (globalDocument === undefined) delete global.document;
    else global.document = globalDocument;
  }
});

test('mount stays inert without the card markup', () => {
  assert.equal(Voice.mount({ $: () => null, request: () => {}, tr, showToast: () => {} }), null);
});
