/* Voice reminder card: cloud-first delivery, separate cloud and local voices,
   persisted rate, explicit preview and ownership-checked stop. Mounted by
   manage.js when the Models & routing view opens; it never autoplays and never
   changes volume or the output device. */
(function (root) {
  'use strict';

  var PREVIEW_POLL_MS = 2500;
  var PREVIEW_POLL_LIMIT = 60;
  var RATE_MIN = 0.5;
  var RATE_MAX = 2.0;
  var DEFAULTS = {
    backend: 'auto',
    rate: 1.1,
    cloud_voice: 'zh_female_vv_uranus_bigtts',
    voice: 'Vivian'
  };

  function humanizeVoiceId(id) {
    return String(id || '').split('_').filter(Boolean)
      .map(function (part) { return part.charAt(0).toUpperCase() + part.slice(1); })
      .join(' ');
  }

  function voiceLabel(voice, tr) {
    var label = (voice && voice.label) || humanizeVoiceId(voice && voice.id);
    if (voice && voice.dialect) label += ' · ' + tr('localVoice.dialect.' + voice.dialect);
    return label;
  }

  function voiceLabelFor(id, voices, tr) {
    var normalized = String(id || '').toLowerCase();
    for (var i = 0; i < (voices || []).length; i += 1) {
      if (voices[i].id === normalized) return voiceLabel(voices[i], tr);
    }
    return id ? humanizeVoiceId(id) : '—';
  }

  function normalizeRate(value, fallback) {
    if (value === '' || value == null) return fallback;
    var number = Number(value);
    if (!Number.isFinite(number)) return fallback;
    if (number < RATE_MIN) return RATE_MIN;
    if (number > RATE_MAX) return RATE_MAX;
    return Math.round(number * 100) / 100;
  }

  function rateLabel(rate) {
    return '×' + normalizeRate(rate, DEFAULTS.rate).toFixed(2);
  }

  function backendLabel(backend, tr) {
    var key = backend === 'local' || backend === 'cloud' ? backend : 'auto';
    return tr('localVoice.backend.' + key);
  }

  function effectiveBackendLabel(backend, tr) {
    var key = backend === 'local' || backend === 'cloud' ? backend : 'auto';
    return tr('localVoice.lastBackend.' + key);
  }

  function cloudVoices(view) {
    view = view || {};
    if (Array.isArray(view.cloud_voices)) return view.cloud_voices;
    if (view.cloud && Array.isArray(view.cloud.voices)) return view.cloud.voices;
    return [];
  }

  function viewState(view, tr, backendOverride) {
    view = view || {};
    var preview = view.preview || {};
    var running = preview.running === true;
    var localVoices = Array.isArray(view.voices) ? view.voices : [];
    var clouds = cloudVoices(view);
    var hasCloud = clouds.length > 0;
    var backend = backendOverride || view.backend;
    if (backend !== 'cloud' && backend !== 'local') backend = DEFAULTS.backend;
    var rate = normalizeRate(view.rate, DEFAULTS.rate);
    var keyConfigured = view.cloud_key_configured === true ||
      (view.cloud && view.cloud.key_configured === true) === true;
    keyConfigured = keyConfigured === true;
    var localAvailable = view.available === true && localVoices.length > 0;
    var cloudAvailable = hasCloud && keyConfigured;
    var canSave = hasCloud || localAvailable;
    var canPreview = canSave && !running;
    if (backend === 'local') canPreview = canPreview && localAvailable;
    else if (backend === 'cloud') canPreview = canPreview && cloudAvailable;
    else canPreview = canPreview && (localAvailable || cloudAvailable);
    var selectedCloud = view.cloud_voice || view.cloud_default || DEFAULTS.cloud_voice;
    var selectedLocal = view.default_voice || DEFAULTS.voice;
    var speaker = (view.speaker && view.speaker.name) || null;
    var state = {
      available: localAvailable,
      localAvailable: localAvailable,
      cloudAvailable: cloudAvailable,
      keyConfigured: keyConfigured,
      hasCloud: hasCloud,
      running: running,
      owned: preview.owned === true,
      canSave: canSave,
      canPreview: canPreview,
      canStop: running && preview.owned === true,
      backend: backend,
      rate: rate,
      rateLabel: rateLabel(rate),
      defaultLabel: voiceLabelFor(selectedLocal, localVoices, tr),
      cloudLabel: voiceLabelFor(selectedCloud, clouds, tr),
      defaultSource: view.default_source === 'saved' ? 'saved' : 'legacy',
      speaker: speaker,
      selectedCloud: selectedCloud,
      selectedLocal: selectedLocal
    };
    if (!hasCloud) {
      if (!localAvailable) state.availability = tr('localVoice.unavailable.' + (view.reason || 'unknown'), {detail: view.detail || ''});
      else if (speaker) state.availability = tr('localVoice.available', {speaker: speaker});
      else state.availability = tr('localVoice.availableUnknownSpeaker');
    } else if (localAvailable && cloudAvailable) {
      state.availability = speaker ? tr('localVoice.availableCloud', {speaker: speaker})
        : tr('localVoice.availableCloudUnknownSpeaker');
    } else if (localAvailable && !cloudAvailable) {
      state.availability = speaker ? tr('localVoice.readyLocalOnly', {speaker: speaker})
        : tr('localVoice.readyLocalOnlyUnknownSpeaker');
    } else if (!localAvailable && cloudAvailable) {
      state.availability = tr('localVoice.cloudOnly', {detail: view.detail || ''});
    } else if (hasCloud && !cloudAvailable) {
      state.availability = tr('localVoice.bothUnavailable', {detail: view.detail || ''});
    } else {
      state.availability = tr('localVoice.unavailable.' + (view.reason || 'unknown'), {detail: view.detail || ''});
    }
    var warningList = Array.isArray(view.warnings) ? view.warnings.slice() : [];
    if (!warningList.length && view.warning) warningList.push(view.warning);
    var warnings = [];
    warningList.forEach(function (warning) {
      var key = warning === 'invalid_saved_voice' ? 'localVoice.warning.invalidSavedVoice'
        : warning === 'invalid_saved_settings' ? 'localVoice.warning.invalidSavedSettings'
          : warning === 'invalid_saved_rate' ? 'localVoice.warning.invalidSavedRate'
            : warning === 'invalid_saved_backend' ? 'localVoice.warning.invalidSavedBackend'
              : warning === 'invalid_saved_cloud_voice' ? 'localVoice.warning.invalidSavedCloudVoice' : null;
      if (!key) return;
      var text = tr(key);
      if (text && text !== key && warnings.indexOf(text) === -1) warnings.push(text);
    });
    state.warning = warnings.length ? warnings.join(' ') : null;
    return state;
  }

  function lastRunText(last, voices, tr, clouds) {
    if (!last) return tr('localVoice.lastNone');
    var all = (voices || []).concat(clouds || []);
    var statusKey = 'localVoice.lastStatus.' + String(last.status || 'unknown');
    var parts = [tr(statusKey)];
    if (last.backend) parts.push(effectiveBackendLabel(last.backend, tr));
    if (last.voice) parts.push(voiceLabelFor(last.voice, all, tr));
    if (last.rate != null) parts.push(rateLabel(last.rate));
    if (Number.isFinite(Number(last.audio_seconds))) parts.push(tr('localVoice.audioSeconds', {seconds: Number(last.audio_seconds).toFixed(1)}));
    if (Number.isFinite(Number(last.total_seconds))) parts.push(tr('localVoice.totalSeconds', {seconds: Number(last.total_seconds).toFixed(1)}));
    if (last.cloud_status && last.cloud_status.status === 'ok') parts.push(tr('localVoice.cloudStatus.ok'));
    if (last.fallback) parts.push(tr('localVoice.fallbackDetail', {reason: last.fallback_detail || tr('localVoice.unknownReason')}));
    if (last.error) parts.push(String(last.error));
    return tr('localVoice.last', {detail: parts.filter(Boolean).join(' · ')});
  }

  function nextSelection(previous, defaultVoice, voices, reset) {
    var fallback = String(defaultVoice || '').toLowerCase();
    if (!reset && previous) {
      for (var i = 0; i < (voices || []).length; i += 1) if (voices[i].id === previous) return previous;
    }
    for (var j = 0; j < (voices || []).length; j += 1) if (voices[j].id === fallback) return fallback;
    return fallback;
  }

  function mount(api) {
    var $ = api.$;
    var request = api.request;
    var tr = api.tr;
    var showToast = api.showToast;

    var select = $('local-voice-select');
    var stateNode = $('local-voice-state');
    var availability = $('local-voice-availability');
    if (!select || !stateNode || !availability) return null;
    var current = $('local-voice-current');
    var save = $('local-voice-save');
    var preview = $('local-voice-preview');
    var stop = $('local-voice-stop');
    var message = $('local-voice-message');
    var last = $('local-voice-last');
    var backend = $('local-voice-backend');
    var rate = $('local-voice-rate');
    var rateValue = $('local-voice-rate-value');
    var cloudSelect = $('local-voice-cloud-voice');
    var backendField = $('local-voice-backend-field');
    var rateField = $('local-voice-rate-field');
    var cloudField = $('local-voice-cloud-field');

    var state = {
      view: null, timer: null, reloadTimer: null, polls: 0,
      active: false, bound: false, busy: false, initialized: false
    };

    function setMessage(text, tone) {
      if (!message) return;
      message.textContent = text || '';
      message.classList.toggle('danger-text', tone === 'error');
    }

    function setRateDisplay(value) {
      if (rateValue) rateValue.textContent = rateLabel(value);
    }

    function populateBackend() {
      if (!backend) return;
      backend.innerHTML = '';
      ['auto', 'cloud', 'local'].forEach(function (value) {
        var option = document.createElement('option');
        option.value = value;
        option.textContent = tr('localVoice.backend.' + value);
        backend.appendChild(option);
      });
      backend.value = '';
    }

    function allCloudVoices() {
      return cloudVoices(state.view);
    }

    function payload(mapped) {
      var body = {};
      if (mapped.hasCloud) {
        body.backend = backend && backend.value ? backend.value : mapped.backend;
        body.rate = normalizeRate(rate ? rate.value : mapped.rate, mapped.rate);
        if (cloudSelect && cloudSelect.value) body.cloud_voice = cloudSelect.value;
      }
      if (select && select.value) body.voice = select.value;
      return body;
    }

    function summary(result) {
      var clouds = allCloudVoices();
      var localVoices = (state.view && state.view.voices) || [];
      return tr('localVoice.currentSummary', {
        backend: backendLabel(result.backend, tr),
        cloud: voiceLabelFor(result.cloud_voice, clouds, tr),
        local: voiceLabelFor(result.voice, localVoices, tr),
        rate: rateLabel(result.rate)
      });
    }

    function render(options) {
      var view = state.view;
      if (!view) return;
      var clouds = allCloudVoices();
      var localVoices = Array.isArray(view.voices) ? view.voices : [];

      if (backend && ((options && options.resetSelection) || !backend.value)) {
        var savedBackend = view.backend;
        backend.value = savedBackend === 'cloud' || savedBackend === 'local' ? savedBackend : DEFAULTS.backend;
      }
      var mapped = viewState(view, tr, backend ? backend.value : null);
      var ready = mapped.hasCloud ? (mapped.localAvailable || mapped.cloudAvailable) : mapped.available;
      stateNode.textContent = ready
        ? (mapped.running ? tr('localVoice.state.playing') : tr('localVoice.state.ready'))
        : tr('localVoice.state.unavailable');
      stateNode.classList.toggle('danger-text', !ready);
      availability.textContent = mapped.availability || '';

      if (backend) {
        var wantedBackend = backend.value;
        if (wantedBackend !== 'auto' && wantedBackend !== 'cloud' && wantedBackend !== 'local') {
          wantedBackend = mapped.backend;
        }
        backend.value = wantedBackend;
        backend.disabled = !mapped.hasCloud || state.busy;
        if (backendField) backendField.hidden = !mapped.hasCloud;
      }

      if (rate) {
        if ((options && options.resetSelection) || !state.initialized || !rate.value) {
          rate.value = String(mapped.rate);
        }
        var wantedRate = normalizeRate(rate.value, mapped.rate);
        setRateDisplay(wantedRate);
        rate.disabled = !mapped.hasCloud || state.busy;
        if (rateField) rateField.hidden = !mapped.hasCloud;
      }

      if (cloudSelect) {
        var wantedCloud = nextSelection(cloudSelect.value, mapped.selectedCloud, clouds,
                                        options && options.resetSelection);
        cloudSelect.innerHTML = '';
        clouds.forEach(function (voice) {
          var option = document.createElement('option');
          option.value = voice.id;
          option.textContent = voiceLabel(voice, tr);
          if (String(voice.id) === wantedCloud) option.selected = true;
          cloudSelect.appendChild(option);
        });
        cloudSelect.value = clouds.length ? wantedCloud : '';
        cloudSelect.disabled = !mapped.hasCloud || state.busy;
        if (cloudField) cloudField.hidden = !mapped.hasCloud;
      }

      var wantedLocal = nextSelection(select.value, mapped.selectedLocal, localVoices,
                                      options && options.resetSelection);
      select.innerHTML = '';
      if (!localVoices.length) {
        var unavailable = document.createElement('option');
        unavailable.value = '';
        unavailable.textContent = tr('localVoice.localUnavailableOption');
        select.appendChild(unavailable);
        select.value = '';
      } else {
        localVoices.forEach(function (voice) {
          var option = document.createElement('option');
          option.value = voice.id;
          option.textContent = voiceLabel(voice, tr);
          if (String(voice.id) === wantedLocal) option.selected = true;
          select.appendChild(option);
        });
        select.value = wantedLocal;
      }
      select.disabled = !mapped.localAvailable || state.busy;

      var cloudLabel = voiceLabelFor(cloudSelect && cloudSelect.value ? cloudSelect.value : mapped.selectedCloud, clouds, tr);
      var localLabel = voiceLabelFor(select && select.value ? select.value : mapped.selectedLocal, localVoices, tr);
      if (current) {
        current.textContent = mapped.hasCloud
          ? tr('localVoice.currentSummary', {
            backend: backendLabel(backend && backend.value ? backend.value : mapped.backend, tr),
            cloud: cloudLabel,
            local: localLabel,
            rate: rateLabel(rate ? rate.value : mapped.rate)
          })
          : mapped.defaultLabel;
      }

      if (save) save.disabled = !mapped.canSave || state.busy;
      if (preview) preview.disabled = !mapped.canPreview || state.busy;
      if (stop) stop.disabled = !mapped.canStop || state.busy;
      if (last) last.textContent = lastRunText(view.last, localVoices, tr, clouds);
      if (mapped.warning) setMessage(mapped.warning, 'error');
      else if (!ready) setMessage('', null);
    }

    async function load(options) {
      var view = await request('/console-api/local-voice');
      state.view = view;
      render(options);
      state.initialized = true;
      return view;
    }

    function stopPolling() {
      if (state.timer) clearTimeout(state.timer);
      if (state.reloadTimer) clearTimeout(state.reloadTimer);
      state.timer = null;
      state.reloadTimer = null;
      state.polls = 0;
    }

    function schedulePoll() {
      if (state.timer || !state.active || state.stopRequested) return;
      state.timer = setTimeout(poll, PREVIEW_POLL_MS);
    }

    function previewOutcome(result) {
      if (!result) return tr('localVoice.previewDone', {detail: tr('localVoice.unknownReason')});
      if (result.status === 'played') {
        var all = ((state.view && state.view.voices) || []).concat(allCloudVoices());
        var detail = [effectiveBackendLabel(result.backend, tr)];
        if (result.voice) detail.push(voiceLabelFor(result.voice, all, tr));
        if (result.rate != null) detail.push(rateLabel(result.rate));
        if (result.fallback) {
          detail.push(tr('localVoice.fallbackDetail', {
            reason: result.fallback_detail || tr('localVoice.unknownReason')
          }));
        }
        return tr('localVoice.previewDone', {detail: detail.filter(Boolean).join(' · ')});
      }
      if (result.status === 'busy') return tr('localVoice.busy');
      return tr('localVoice.previewFailed', {detail: result.error || tr('localVoice.unknownError')});
    }

    async function poll() {
      state.timer = null;
      if (!state.active) return;
      state.polls += 1;
      try {
        var view = await load();
        if (state.stopRequested) return;
        if (!view.preview || !view.preview.running) {
          setMessage(previewOutcome(view.last), view.last && view.last.status === 'played' ? null : 'error');
          return;
        }
      } catch (error) {
        if (state.stopRequested) return;
        if (state.polls >= 3) {
          setMessage(tr('localVoice.pollFailed', {message: error.message || String(error)}), 'error');
          return;
        }
      }
      if (state.polls >= PREVIEW_POLL_LIMIT) {
        setMessage(tr('localVoice.pollStopped'), 'error');
        return;
      }
      schedulePoll();
    }

    async function act(button, body, success) {
      if (state.busy) return;
      state.busy = true;
      [save, preview, stop, backend, rate, cloudSelect, select]
        .forEach(function (node) { if (node) node.disabled = true; });
      try {
        var result = await request('/console-api/local-voice', {
          method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)
        });
        await success(result);
      } catch (error) {
        setMessage(error.message || String(error), 'error');
      } finally {
        state.busy = false;
        render();
      }
    }

    function bind() {
      if (state.bound) return;
      state.bound = true;
      if (save) save.addEventListener('click', function () {
        var mapped = viewState(state.view, tr, backend ? backend.value : null);
        act(save, Object.assign({action: 'save'}, payload(mapped)), async function (result) {
          await load({resetSelection: true});
          var detail = summary(result);
          setMessage(tr('localVoice.saved', {detail: detail}));
          if (showToast) showToast(tr('localVoice.heading'), tr('localVoice.saved', {detail: detail}));
        });
      });
      if (preview) preview.addEventListener('click', function () {
        var mapped = viewState(state.view, tr, backend ? backend.value : null);
        state.stopRequested = false;
        act(preview, Object.assign({action: 'preview'}, payload(mapped)), async function (result) {
          if (result.status === 'busy') {
            setMessage(tr('localVoice.busy'));
            return;
          }
          if (result.status === 'already_running') setMessage(tr('localVoice.alreadyRunning'));
          else setMessage(tr('localVoice.previewStarted', {seconds: 37}));
          await load();
          state.polls = 0;
          schedulePoll();
        });
      });
      if (stop) stop.addEventListener('click', function () {
        state.stopRequested = true;
        stopPolling();
        act(stop, {action: 'stop'}, async function (result) {
          setMessage(result.status === 'stop_requested' ? tr('localVoice.stopRequested') : tr('localVoice.idle'));
          if (state.view && state.view.preview) state.view.preview.running = false;
          render();
          state.reloadTimer = setTimeout(function () {
            state.reloadTimer = null;
            load().catch(function (error) { setMessage(error.message, 'error'); });
          }, 1200);
        });
      });
      if (rate) rate.addEventListener('input', function () {
        setRateDisplay(rate.value);
        render();
      });
      if (backend) backend.addEventListener('change', function () { render(); });
      if (cloudSelect) cloudSelect.addEventListener('change', function () { render(); });
      select.addEventListener('change', function () { render(); });
    }

    async function start() {
      state.active = true;
      state.stopRequested = false;
      state.initialized = false;
      setMessage('');
      populateBackend();
      try {
        await load({resetSelection: true});
      } catch (error) {
        setMessage(tr('localVoice.unavailable.unknown', {detail: error.message || String(error)}), 'error');
      }
    }

    function stopPollingView() {
      state.active = false;
      stopPolling();
    }

    return {bind: bind, start: start, stop: stopPollingView, load: load};
  }

  var api = {mount: mount, humanizeVoiceId: humanizeVoiceId, voiceLabel: voiceLabel,
             voiceLabelFor: voiceLabelFor, viewState: viewState, lastRunText: lastRunText,
             nextSelection: nextSelection, normalizeRate: normalizeRate,
             rateLabel: rateLabel, backendLabel: backendLabel,
             effectiveBackendLabel: effectiveBackendLabel, cloudVoices: cloudVoices};
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  root.VoiceSettings = api;
})(typeof window !== 'undefined' ? window : globalThis);
