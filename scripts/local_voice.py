"""Cloud-first speech settings, catalog, persisted defaults and bounded preview.

The console is a thin, authenticated control surface over the installed
``qwen-voice-notify`` skill. This module owns the consumer-side contract:

* ``~/.config/qwen-voice-notify/settings.json`` stores only non-secret
  preferences: the local fallback voice, cloud voice, backend (``auto`` by
  default, cloud first) and speech rate (``1.1`` by default). Writes are atomic
  merges, so an older client that knows only ``voice`` cannot reset the newer
  fields, and a missing rate always means ``1.1``.
* The local voice catalog comes from the installed model's own ``config.json``
  (``talker_config.spk_id`` / ``spk_is_dialect``), so only voices the local
  weights actually expose can be selected. Cloud voice IDs are the documented
  Ark Agent Plan ``seed-tts-2.0`` catalog checked in below.
* Preview uses one fixed argv, one fixed short Chinese sentence and a fixed
  offline environment. Saved/form values are validated and passed as separate
  argv entries; nothing from the request is interpolated into a shell string.
* A GET never synthesizes, plays audio or performs a network request. It may
  read only metadata (including whether the Agent Plan key is configured).
* Stop only terminates a preview this module started; another client's playback
  is left untouched.

The controller never changes system volume or the default output device; the
skill's own Mac mini built-in speaker selection stays authoritative.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import tempfile
import threading
import time

HOME = Path.home()
SKILL_DIR = HOME / '.codex/skills/qwen-voice-notify'
NOTIFY = SKILL_DIR / 'scripts/notify.py'
RUNTIME = HOME / '.local/share/qwen-voice-notify'
VENV_PYTHON = RUNTIME / 'venv/bin/python'
MODEL = RUNTIME / 'model'
MODEL_FILES = ('config.json', 'model.safetensors', 'tokenizer_config.json',
               'speech_tokenizer/config.json', 'speech_tokenizer/model.safetensors')
ACTIVE = RUNTIME / 'active.json'
LAST_RUN = RUNTIME / 'last-run.json'
SETTINGS_DIR = HOME / '.config/qwen-voice-notify'
SETTINGS = SETTINGS_DIR / 'settings.json'
OWNED = SETTINGS_DIR / 'preview.json'
PREVIEW_RESULT = SETTINGS_DIR / 'preview-result.json'

LEGACY_DEFAULT = 'Vivian'
DEFAULT_CLOUD = 'zh_female_vv_uranus_bigtts'
DEFAULT_BACKEND = 'auto'
DEFAULT_RATE = 1.1
RATE_MIN = 0.5
RATE_MAX = 2.0
BACKENDS = ('auto', 'cloud', 'local')
CLOUD_MODEL = 'seed-tts-2.0'
PREVIEW_TEXT = '您好，这是千问语音提醒试听。'
VOICE_ID = re.compile(r'[A-Za-z][A-Za-z0-9_]{0,31}\Z')
CLOUD_VOICE_ID = re.compile(r'[A-Za-z][A-Za-z0-9_]{0,63}\Z')
SETTINGS_LIMIT = 4096
PROBE_TTL = 30.0
PREVIEW_TIMEOUT = '120'

# Documented Ark Agent Plan seed-tts-2.0 voices. The console cannot enumerate
# the cloud catalog without a network request, so this checked-in subset is the
# selectable list; every ID is an official 2.0 (uranus) voice.
CLOUD_VOICES = (
    {'id': 'zh_female_vv_uranus_bigtts', 'label': 'Vivi 2.0'},
    {'id': 'zh_female_xiaohe_uranus_bigtts', 'label': '小何 2.0'},
    {'id': 'zh_male_m191_uranus_bigtts', 'label': '云舟 2.0'},
    {'id': 'zh_male_taocheng_uranus_bigtts', 'label': '小天 2.0'},
    {'id': 'zh_male_liufei_uranus_bigtts', 'label': '刘飞 2.0'},
    {'id': 'zh_female_sophie_uranus_bigtts', 'label': '魅力苏菲 2.0'},
    {'id': 'zh_female_qingxinnvsheng_uranus_bigtts', 'label': '清新女声 2.0'},
    {'id': 'zh_female_cancan_uranus_bigtts', 'label': '知性灿灿 2.0'},
    {'id': 'zh_female_gaolengyujie_uranus_bigtts', 'label': '高冷御姐 2.0'},
    {'id': 'zh_female_linjianvhai_uranus_bigtts', 'label': '邻家女孩 2.0'},
    {'id': 'zh_female_tianmeitaozi_uranus_bigtts', 'label': '甜美桃子 2.0'},
    {'id': 'zh_female_wenroushunv_uranus_bigtts', 'label': '温柔淑女 2.0'},
    {'id': 'zh_male_ruyaqingnian_uranus_bigtts', 'label': '儒雅青年 2.0'},
    {'id': 'zh_male_yangguangqingnian_uranus_bigtts', 'label': '阳光青年 2.0'},
    {'id': 'zh_female_yingyujiaoxue_uranus_bigtts', 'label': 'Tina老师 2.0'},
    {'id': 'zh_female_kefunvsheng_uranus_bigtts', 'label': '暖阳女声 2.0'},
    {'id': 'zh_female_xiaoxue_uranus_bigtts', 'label': '儿童绘本 2.0'},
)
CLOUD_IDS = tuple(voice['id'] for voice in CLOUD_VOICES)

_CONTROL = threading.Lock()
_PROBE = {'at': None, 'value': None}


def _read_json(path, limit=65536):
    """Small, size-bounded JSON read; returns None for absent/invalid files."""
    try:
        path = Path(path)
        if not path.is_file() or path.stat().st_size > limit:
            return None
        return json.loads(path.read_text())
    except (OSError, ValueError, UnicodeError):
        return None


def _write_json_atomic(path, payload):
    """Owner-only atomic JSON write (temp file, fsync, os.replace)."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    data = (json.dumps(payload, ensure_ascii=False, indent=2) + '\n').encode()
    handle, temporary = tempfile.mkstemp(prefix='.' + path.name + '.', dir=str(path.parent))
    try:
        with os.fdopen(handle, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        try:
            directory = os.open(str(path.parent), os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except OSError:
            pass
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _label(voice_id):
    return ' '.join(part.capitalize() for part in voice_id.split('_'))


def _local_catalog():
    """Return (voices, reason, detail) from the installed model configuration."""
    configuration = _read_json(MODEL / 'config.json', limit=1_048_576)
    if not isinstance(configuration, dict):
        return [], 'model_missing', 'Installed model configuration is unavailable'
    talker = configuration.get('talker_config')
    speakers = talker.get('spk_id') if isinstance(talker, dict) else None
    if not isinstance(speakers, dict) or not speakers:
        return [], 'catalog_error', 'Installed model exposes no named voices'
    dialects = talker.get('spk_is_dialect') if isinstance(talker, dict) else None
    voices = []
    for voice_id in speakers:
        normalized = str(voice_id).strip().lower()
        if not VOICE_ID.fullmatch(normalized):
            continue
        dialect = None
        if isinstance(dialects, dict):
            value = dialects.get(voice_id)
            if isinstance(value, str) and value.strip():
                dialect = value.strip().lower()
        voices.append({'id': normalized, 'label': _label(normalized), 'dialect': dialect})
    if not voices:
        return [], 'catalog_error', 'Installed model exposes no selectable voices'
    return voices, None, None


# Public alias kept for the existing catalog tests and callers.
catalog = _local_catalog


def _model_ready():
    return all((MODEL / name).is_file() for name in MODEL_FILES)


def runtime_state():
    """Local runtime availability (no subprocess, no network)."""
    voices, reason, detail = catalog()
    if not NOTIFY.is_file():
        return False, 'runtime_missing', 'Installed notify.py entry point is unavailable', voices
    if not VENV_PYTHON.is_file():
        return False, 'runtime_missing', 'Installed voice virtual environment is unavailable', voices
    if not _model_ready():
        return False, 'model_missing', 'Installed Qwen3-TTS model is incomplete', voices
    if reason:
        return False, reason, detail, voices
    return True, None, None, voices


def _auth_store():
    base = os.environ.get('XDG_DATA_HOME')
    if base:
        return Path(base).expanduser() / 'opencode' / 'auth.json'
    return HOME / '.local/share/opencode/auth.json'


def _cloud_key_configured():
    """Whether the Agent Plan provider entry exists; never returns the value."""
    data = _read_json(_auth_store(), limit=262144)
    entry = data.get('volcengine-agent-plan') if isinstance(data, dict) else None
    return bool(isinstance(entry, dict) and entry.get('type') == 'api' and
                isinstance(entry.get('key'), str) and len(entry.get('key')) >= 8)


def _normalize_voice(value, voices, require_catalog=False):
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    if not VOICE_ID.fullmatch(normalized):
        return None
    if require_catalog:
        if not voices or normalized not in {voice['id'] for voice in voices}:
            return None
    return normalized


def _normalize_cloud_voice(value):
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    if not CLOUD_VOICE_ID.fullmatch(normalized):
        return None
    return normalized if normalized in CLOUD_IDS else None


def _normalize_rate(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number) or number < RATE_MIN or number > RATE_MAX:
        return None
    return round(number, 2)


def _default_preferences():
    return {'voice': LEGACY_DEFAULT, 'cloud_voice': DEFAULT_CLOUD,
            'backend': DEFAULT_BACKEND, 'rate': DEFAULT_RATE}


def _normalize_settings(data, voices, allow_unvalidated_voice=False):
    """Normalize a settings mapping, preserving defaults for invalid fields."""
    preferences = _default_preferences()
    warnings = []
    meta = {'settings': 'saved', 'voice': 'legacy'}
    if not isinstance(data, dict):
        return preferences, ['invalid_saved_settings'], {'settings': 'legacy', 'voice': 'legacy'}

    voice = data.get('voice')
    if voice is not None:
        if voices:
            normalized = _normalize_voice(voice, voices, require_catalog=True)
        elif allow_unvalidated_voice:
            normalized = _normalize_voice(voice, None, require_catalog=False)
        else:
            normalized = None
        if normalized is None:
            warnings.append('invalid_saved_voice')
        else:
            preferences['voice'] = normalized
            meta['voice'] = 'saved'

    cloud_voice = data.get('cloud_voice')
    if cloud_voice is not None:
        normalized = _normalize_cloud_voice(cloud_voice)
        if normalized is None:
            warnings.append('invalid_saved_cloud_voice')
        else:
            preferences['cloud_voice'] = normalized

    backend = data.get('backend')
    if backend is not None:
        if backend not in BACKENDS:
            warnings.append('invalid_saved_backend')
        else:
            preferences['backend'] = backend

    rate = data.get('rate')
    if rate is not None:
        normalized = _normalize_rate(rate)
        if normalized is None:
            warnings.append('invalid_saved_rate')
        else:
            preferences['rate'] = normalized

    return preferences, warnings, meta


def read_preferences(voices):
    """Return (preferences, warnings, meta) from the bounded settings file."""
    preferences = _default_preferences()
    try:
        present = SETTINGS.exists()
    except OSError:
        present = False
    if not present:
        return preferences, [], {'settings': 'legacy', 'voice': 'legacy'}
    data = _read_json(SETTINGS, limit=SETTINGS_LIMIT)
    if not isinstance(data, dict):
        return preferences, ['invalid_saved_settings'], {'settings': 'legacy', 'voice': 'legacy'}
    return _normalize_settings(data, voices)


def read_settings(voices):
    """Compatibility view of the local default: (voice, source, warning)."""
    preferences, warnings, meta = read_preferences(voices)
    voice_warning = 'invalid_saved_voice' if any(
        warning in ('invalid_saved_voice', 'invalid_saved_settings') for warning in warnings) else None
    return preferences['voice'], meta['voice'], voice_warning


def save(options):
    """Validate and atomically merge non-secret speech preferences.

    ``options`` is a mapping with any of ``voice``, ``cloud_voice``,
    ``backend`` and ``rate``. A plain string is accepted as a legacy
    ``voice``-only save so an older console keeps working; the merge preserves
    every field it did not explicitly update.
    """
    if isinstance(options, str):
        options = {'voice': options}
    if not isinstance(options, dict):
        raise ValueError('Voice settings object required')

    updates = {}
    if options.get('voice') is not None:
        available, _reason, detail, voices = runtime_state()
        if not available:
            raise RuntimeError(detail or 'Local voice runtime is unavailable')
        normalized = _normalize_voice(options.get('voice'), voices, require_catalog=True)
        if normalized is None:
            raise ValueError('Unsupported local voice')
        updates['voice'] = normalized

    if options.get('cloud_voice') is not None:
        normalized = _normalize_cloud_voice(options.get('cloud_voice'))
        if normalized is None:
            raise ValueError('Unsupported cloud voice')
        updates['cloud_voice'] = normalized

    if options.get('backend') is not None:
        backend = options.get('backend')
        if backend not in BACKENDS:
            raise ValueError('Unsupported voice backend')
        updates['backend'] = backend

    if options.get('rate') is not None:
        normalized = _normalize_rate(options.get('rate'))
        if normalized is None:
            raise ValueError('Speech rate must be between 0.5 and 2.0')
        updates['rate'] = normalized

    if not updates:
        raise ValueError('No voice settings to save')

    existing = _read_json(SETTINGS, limit=SETTINGS_LIMIT)
    if not isinstance(existing, dict):
        existing = {}
    merged = dict(existing)
    merged.update(updates)

    voices = catalog()[0]
    preferences, warnings, _meta = _normalize_settings(
        merged, voices, allow_unvalidated_voice=True)
    updated_at = time.time()
    payload = dict(merged)
    payload.update(preferences)
    payload['schema'] = 2
    payload['updated_at'] = updated_at
    _write_json_atomic(SETTINGS, payload)
    return {
        'voice': preferences['voice'],
        'cloud_voice': preferences['cloud_voice'],
        'backend': preferences['backend'],
        'rate': preferences['rate'],
        'updated_at': updated_at,
        'warnings': warnings,
    }


def _identity(pid):
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return ''
    try:
        result = subprocess.run(['/bin/ps', '-p', str(pid), '-o', 'uid=,lstart=,args='],
                                capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.SubprocessError):
        return ''
    return result.stdout.strip()


def _process_start(pid):
    """Stable process start token; unlike argv it survives a launcher re-exec."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return ''
    try:
        result = subprocess.run(['/bin/ps', '-p', str(pid), '-o', 'lstart='],
                                capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.SubprocessError):
        return ''
    return result.stdout.strip()


def live_active():
    """The skill's verified active playback record, if its process is still alive."""
    data = _read_json(ACTIVE)
    if not isinstance(data, dict):
        return None
    current = _identity(data.get('pid'))
    if current and current == data.get('identity') and str(NOTIFY) in current:
        return data
    return None


def owned_preview():
    """The console-owned preview record, only while its exact process is alive.

    Ownership is PID plus the process start token (not the argv snapshot, which
    macOS launchers can change on re-exec). The skill's own active record must
    also verify that same PID, so a recycled PID cannot inherit ownership.
    """
    record = _read_json(OWNED, limit=SETTINGS_LIMIT)
    if not isinstance(record, dict):
        return None
    active = live_active()
    if not active or active.get('pid') != record.get('pid'):
        return None
    started = _process_start(record.get('pid'))
    if not started or record.get('started') != started:
        return None
    return {'pid': active.get('pid'), 'identity': active.get('identity'),
            'voice': record.get('voice'), 'cloud_voice': record.get('cloud_voice'),
            'backend': record.get('backend'), 'rate': record.get('rate'),
            'started_at': record.get('started_at')}


def pending_owned():
    """Owned preview that is alive before the skill published active.json.

    The HTTP handler can start a second preview only after this check fails; a
    verified PID + start token + current notify.py argv is enough to treat the
    child as ours during startup, so OWNED cannot be overwritten by a duplicate.
    """
    record = _read_json(OWNED, limit=SETTINGS_LIMIT)
    if not isinstance(record, dict):
        return None
    pid = record.get('pid')
    started = _process_start(pid)
    if not started or record.get('started') != started:
        return None
    current = _identity(pid)
    if not current or str(NOTIFY) not in current:
        return None
    return {'pid': pid, 'identity': current, 'voice': record.get('voice'),
            'cloud_voice': record.get('cloud_voice'), 'backend': record.get('backend'),
            'rate': record.get('rate'), 'started_at': record.get('started_at')}


def _clear_owned(pid=None):
    record = _read_json(OWNED, limit=SETTINGS_LIMIT)
    if pid is not None and isinstance(record, dict) and record.get('pid') != pid:
        return
    try:
        OWNED.unlink(missing_ok=True)
    except OSError:
        pass


def _sanitize(value, limit=300):
    text = str(value)
    for prefix, replacement in ((str(RUNTIME), '~/.local/share/qwen-voice-notify'),
                                (str(SKILL_DIR), '~/.codex/skills/qwen-voice-notify'),
                                (str(HOME), '~')):
        text = text.replace(prefix, replacement)
    text = text.replace('\n', ' ').replace('\r', ' ')
    return text[:limit]


def _text(value, limit=300):
    if not isinstance(value, (str, int, float)):
        return None
    text = _sanitize(value, limit)
    return text or None


def _number(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number < 0:
        return None
    return round(number, 3)


def _cloud_status(value):
    if not isinstance(value, dict):
        return None
    result = {}
    for key in ('model', 'status', 'reason', 'request_id', 'code', 'detail'):
        if key not in value:
            continue
        item = value.get(key)
        if key == 'code' and isinstance(item, int) and not isinstance(item, bool):
            result[key] = item
        elif key != 'code':
            text = _text(item, 300 if key == 'detail' else 120)
            if text:
                result[key] = text
    return result or None


def _last_payload(record):
    if not isinstance(record, dict):
        return None
    return {
        'status': _text(record.get('status'), 32) or 'unknown',
        'voice': _text(record.get('voice'), 64),
        'backend': _text(record.get('backend'), 16),
        'requested_backend': _text(record.get('requested_backend'), 16),
        'rate': _number(record.get('rate')),
        'cloud_voice': _text(record.get('cloud_voice'), 64),
        'fallback': record.get('fallback') is True,
        'fallback_reason': _text(record.get('fallback_reason'), 32),
        'fallback_detail': _text(record.get('fallback_detail'), 300),
        'cloud_status': _cloud_status(record.get('cloud_status')),
        'audio_seconds': _number(record.get('audio_seconds')),
        'total_seconds': _number(record.get('total_seconds')),
        'error': _text(record.get('error'), 300),
        'finished_at': _number(record.get('finished_at')),
    }


def _probe():
    """Cached `notify.py status` probe; only used for the speaker name."""
    now = time.monotonic()
    if _PROBE['at'] is not None and now - _PROBE['at'] < PROBE_TTL:
        return _PROBE['value']
    value = None
    if VENV_PYTHON.is_file() and NOTIFY.is_file():
        try:
            result = subprocess.run([str(VENV_PYTHON), str(NOTIFY), 'status'],
                                    capture_output=True, text=True, timeout=4)
            lines = [line for line in (result.stdout or '').splitlines() if line.strip()]
            if lines:
                parsed = json.loads(lines[-1])
                if isinstance(parsed, dict):
                    value = {'device': parsed.get('device'), 'status': parsed.get('status')}
        except (OSError, ValueError, subprocess.SubprocessError):
            value = None
    _PROBE['at'] = now
    _PROBE['value'] = value
    return value


def view():
    """Console-safe status payload: availability, catalogs, defaults and playback."""
    available, reason, detail, voices = runtime_state()
    preferences, warnings, meta = read_preferences(voices)
    cloud_key = _cloud_key_configured()
    active = live_active()
    owned = owned_preview()
    pending = None if active else pending_owned()
    own = owned or pending
    last = _last_payload(_read_json(LAST_RUN))
    own_result = _last_payload(_read_json(PREVIEW_RESULT, limit=SETTINGS_LIMIT))
    if own_result is not None:
        try:
            newer = last is None or float(own_result.get('finished_at') or 0) >= \
                float(last.get('finished_at') or 0)
        except (TypeError, ValueError):
            newer = last is None
        if newer and str(own_result.get('status') or '') != 'played':
            # Only non-success console outcomes must mask an older last-run;
            # a successful preview keeps the skill's measured timing metrics.
            last = own_result
    probe = _probe()
    info = active or pending
    preview = {'running': bool(info), 'owned': bool(own), 'pending': bool(pending)}
    if pending:
        preview['phase'] = 'starting'
    if info:
        preview['phase'] = preview.get('phase') or str(info.get('phase') or '')[:32] or None
        preview['voice'] = _text(info.get('voice') or (own or {}).get('voice'), 64) or None
        preview['backend'] = _text(info.get('backend') or (own or {}).get('backend'), 16) or None
        preview['rate'] = _number(info.get('rate') if info.get('rate') is not None
                                  else (own or {}).get('rate'))
        preview['started_at'] = info.get('started_at')
    cloud_voices = [dict(voice) for voice in CLOUD_VOICES]
    legacy_warning = 'invalid_saved_voice' if any(
        warning in ('invalid_saved_voice', 'invalid_saved_settings') for warning in warnings) else None
    return {
        'available': available,
        'reason': reason,
        'detail': _sanitize(detail) if detail else None,
        'voices': voices,
        'default_voice': preferences['voice'],
        'default_source': meta['voice'],
        'legacy_default': LEGACY_DEFAULT,
        'warning': legacy_warning,
        'warnings': warnings,
        'settings_source': meta['settings'],
        'backend': preferences['backend'],
        'rate': preferences['rate'],
        'cloud_voice': preferences['cloud_voice'],
        'cloud_voices': cloud_voices,
        'cloud_default': DEFAULT_CLOUD,
        'cloud_key_configured': cloud_key,
        'cloud_ready': bool(cloud_key),
        'cloud_model': CLOUD_MODEL,
        'cloud': {
            'voices': cloud_voices,
            'default_voice': DEFAULT_CLOUD,
            'selected_voice': preferences['cloud_voice'],
            'key_configured': bool(cloud_key),
            'ready': bool(cloud_key),
            'model': CLOUD_MODEL,
        },
        'defaults': {
            'voice': LEGACY_DEFAULT,
            'cloud_voice': DEFAULT_CLOUD,
            'backend': DEFAULT_BACKEND,
            'rate': DEFAULT_RATE,
        },
        'preview': preview,
        'last': last,
        'speaker': {'name': probe.get('device') if probe else None},
    }


def _preview_options(options, voices, preferences):
    data = {} if options is None else options
    if isinstance(data, str):
        data = {'voice': data}
    if not isinstance(data, dict):
        raise ValueError('Voice preview options required')

    backend = data.get('backend') if data.get('backend') is not None else preferences['backend']
    if backend not in BACKENDS:
        raise ValueError('Unsupported voice backend')

    rate = data.get('rate') if data.get('rate') is not None else preferences['rate']
    normalized_rate = _normalize_rate(rate)
    if normalized_rate is None:
        raise ValueError('Speech rate must be between 0.5 and 2.0')

    voice = data.get('voice') if data.get('voice') is not None else preferences['voice']
    if data.get('voice') is not None:
        # Syntax only here: catalog membership is required later only when a
        # local voice can actually be used (local backend, or auto with the
        # local runtime available). A cloud-only preview must work even when
        # the local model is absent.
        normalized_voice = _normalize_voice(data.get('voice'), None, require_catalog=False)
        if normalized_voice is None:
            raise ValueError('Unsupported local voice')
        voice = normalized_voice

    cloud_voice = data.get('cloud_voice') if data.get('cloud_voice') is not None \
        else preferences['cloud_voice']
    if data.get('cloud_voice') is not None:
        normalized_cloud = _normalize_cloud_voice(data.get('cloud_voice'))
        if normalized_cloud is None:
            raise ValueError('Unsupported cloud voice')
        cloud_voice = normalized_cloud

    return backend, normalized_rate, voice, cloud_voice


def preview(options=None):
    """Start one bounded, console-owned preview with a fixed argv and sentence."""
    with _CONTROL:
        available, reason, detail, voices = runtime_state()
        preferences, _warnings, _meta = read_preferences(voices)
        backend, rate, voice, cloud_voice = _preview_options(options, voices, preferences)
        cloud_key = _cloud_key_configured()

        if backend == 'local' and not available:
            raise RuntimeError(detail or 'Local voice runtime is unavailable')
        if backend == 'cloud' and not cloud_key:
            raise RuntimeError('云端密钥未配置；仅云端模式不会回退到本机语音。')
        if backend == 'auto' and not cloud_key and not available:
            raise RuntimeError('云端密钥未配置，本机离线语音也不可用。')
        if backend == 'local' or (backend == 'auto' and available):
            normalized_voice = _normalize_voice(voice, voices, require_catalog=True)
        else:
            normalized_voice = _normalize_voice(voice, None, require_catalog=False)
        if normalized_voice is None:
            raise ValueError('Unsupported local voice')
        voice = normalized_voice

        active = live_active()
        if active:
            owned = owned_preview()
            if owned and owned.get('pid') == active.get('pid'):
                return {'status': 'already_running', 'voice': owned.get('voice'),
                        'backend': owned.get('backend'), 'rate': owned.get('rate')}
            return {'status': 'busy'}
        pending = pending_owned()
        if pending:
            return {'status': 'already_running', 'voice': pending.get('voice'),
                    'backend': pending.get('backend'), 'rate': pending.get('rate')}

        argv = [str(VENV_PYTHON), str(NOTIFY), 'speak',
                '--backend', backend,
                '--rate', '{:.2f}'.format(rate),
                '--voice', voice,
                '--cloud-voice', cloud_voice,
                '--timeout', PREVIEW_TIMEOUT]
        environment = dict(os.environ, HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1',
                           TOKENIZERS_PARALLELISM='false')
        try:
            process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, text=True, env=environment,
                                       start_new_session=True)
        except OSError:
            raise RuntimeError('Could not start the voice process') from None
        try:
            process.stdin.write(PREVIEW_TEXT)
            process.stdin.flush()
        except OSError:
            pass
        try:
            process.stdin.close()
        except OSError:
            pass
        # communicate() in the reaper must not flush an already-closed stdin.
        process.stdin = None
        started = _process_start(process.pid)
        for _ in range(4):
            if started:
                break
            time.sleep(0.02)
            started = _process_start(process.pid)
        record = {'pid': process.pid, 'identity': _identity(process.pid),
                  'started': started, 'voice': voice, 'cloud_voice': cloud_voice,
                  'backend': backend, 'rate': rate, 'started_at': time.time()}
        try:
            _write_json_atomic(OWNED, record)
        except OSError:
            try:
                process.kill()
            except OSError:
                pass
            raise RuntimeError('Could not record the voice preview') from None
        threading.Thread(target=_reap, args=(process, record['pid'], voice, backend,
                                             rate, cloud_voice), daemon=True).start()
        return {'status': 'started', 'voice': voice, 'backend': backend,
                'rate': rate, 'cloud_voice': cloud_voice, 'pid': process.pid}


def _reap(process, pid, voice, backend, rate, cloud_voice):
    """Drain, record the exact child status and reap a console-owned preview.

    The skill can emit ``busy`` without writing last-run.json, so the console
    keeps its own bounded result (never raw stdout) instead of trusting an older
    last-run that may claim a successful playback for this preview.
    """
    parsed = None
    detail = ''
    try:
        if process.stdin is not None:
            try:
                process.stdin.close()
            except (OSError, ValueError):
                pass
            process.stdin = None
        out, err = process.communicate()
        for line in reversed((out or '').splitlines()):
            if not line.strip():
                continue
            try:
                candidate = json.loads(line)
            except ValueError:
                break
            if isinstance(candidate, dict):
                parsed = candidate
            break
        if parsed is None and err:
            detail = err[-500:]
    except (OSError, ValueError):
        parsed = None
        try:
            process.wait(timeout=5)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            pass
    finally:
        result = _last_payload(parsed) or {'status': 'unknown', 'voice': None,
                                           'backend': None, 'rate': None,
                                           'cloud_voice': None, 'fallback': False,
                                           'fallback_reason': None, 'fallback_detail': None,
                                           'cloud_status': None, 'audio_seconds': None,
                                           'total_seconds': None, 'error': None,
                                           'finished_at': None}
        result['voice'] = result.get('voice') or voice
        result['backend'] = result.get('backend') or backend
        result['requested_backend'] = result.get('requested_backend') or backend
        result['rate'] = result.get('rate') if result.get('rate') is not None else rate
        result['cloud_voice'] = result.get('cloud_voice') or cloud_voice
        if result.get('status') == 'unknown' and detail:
            result['error'] = _sanitize(detail, 300)
        result['pid'] = pid
        result['finished_at'] = time.time()
        try:
            _write_json_atomic(PREVIEW_RESULT, result)
        except OSError:
            pass
        _clear_owned(pid)


def stop():
    """Stop only a preview launched by this console, revalidated by pid+identity.

    The console never delegates to the skill's generic ``stop`` command: that
    command re-reads ``active.json`` and could cancel a newer playback started by
    another client between the ownership check and the signal. Instead the exact
    owned PID is signalled; the skill's own SIGTERM handler still performs the
    graceful child/process-group cleanup and metrics write.
    """
    with _CONTROL:
        owned = owned_preview()
        target = owned or pending_owned()
        if not target:
            _clear_owned()
            return {'status': 'idle'}
        if owned:
            active = live_active()
            if not active or active.get('pid') != owned.get('pid') or \
                    active.get('identity') != owned.get('identity'):
                _clear_owned()
                return {'status': 'idle'}
        try:
            pid = int(target['pid'])
        except (KeyError, TypeError, ValueError):
            _clear_owned()
            return {'status': 'idle'}
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            _clear_owned(pid)
            return {'status': 'idle'}
        except OSError:
            raise RuntimeError('Could not stop the voice preview') from None
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and _process_start(pid):
            time.sleep(0.05)
        if not _process_start(pid):
            _clear_owned(pid)
        return {'status': 'stop_requested'}
