#!/usr/bin/env python3
"""On-demand speech for the Worker Desk notification skill.

The default route is cloud-first through the existing Volcengine Ark Agent Plan
speech model (``seed-tts-2.0``), with an automatic fallback to the installed
offline Qwen3-TTS model. Preferences come from
``~/.config/qwen-voice-notify/settings.json``; an explicit CLI value always
overrides them. Missing preferences keep the legacy local default ``Vivian`` and
the default speech rate ``1.1``.

The model process never runs at the same time as playback: local synthesis exits
first, a pitch-preserving ``ffmpeg atempo`` pass applies a non-1.0 local rate,
and only then is the temporary WAV played. Cloud audio is decoded to WAV before
playback and its native ``speech_rate`` is never applied twice. Raw audio and
credentials are never written to logs or last-run metadata.
"""
from __future__ import annotations

import argparse
import base64
import binascii
import fcntl
import http.client
import json
import math
import os
from pathlib import Path
import re
import signal
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import uuid

HOME = Path.home()
ROOT = HOME / '.local/share/qwen-voice-notify'
MODEL = ROOT / 'model'
SCRIPT = Path(__file__).resolve()
STATE = ROOT / 'active.json'
LAST = ROOT / 'last-run.json'
SETTINGS = HOME / '.config/qwen-voice-notify/settings.json'
SETTINGS_LIMIT = 4096

LEGACY_DEFAULT_VOICE = 'Vivian'
DEFAULT_LOCAL_VOICE = LEGACY_DEFAULT_VOICE
DEFAULT_CLOUD_VOICE = 'zh_female_vv_uranus_bigtts'
DEFAULT_BACKEND = 'auto'
DEFAULT_RATE = 1.1
RATE_MIN = 0.5
RATE_MAX = 2.0
BACKENDS = ('auto', 'cloud', 'local')

MODEL_FILES = ('config.json', 'model.safetensors', 'tokenizer_config.json',
               'speech_tokenizer/config.json', 'speech_tokenizer/model.safetensors')
VOICE_ID = re.compile(r'[A-Za-z][A-Za-z0-9_]{0,31}\Z')
CLOUD_VOICE_ID = re.compile(r'[A-Za-z][A-Za-z0-9_]{0,63}\Z')

# Official Ark Agent Plan single-directional TTS endpoint. The /plan/ segment is
# part of the Agent Plan contract; the generic pay-as-you-go endpoint must not be
# substituted because the dedicated Agent Plan key would not be accepted there.
CLOUD_PROVIDER = 'volcengine-agent-plan'
CLOUD_HOST = 'openspeech.bytedance.com'
CLOUD_PATH = '/api/v3/plan/tts/unidirectional'
CLOUD_ENDPOINT = 'https://' + CLOUD_HOST + CLOUD_PATH
CLOUD_RESOURCE = 'seed-tts-2.0'
CLOUD_TERMINAL = 20000000
CLOUD_SAMPLE_RATE = 24000
CLOUD_FORMAT = 'mp3'
CLOUD_CONNECT_TIMEOUT = 5.0
CLOUD_TOTAL_TIMEOUT = 15.0
CLOUD_MAX_BYTES = 8 * 1024 * 1024
CLOUD_MAX_LINE = 4 * 1024 * 1024
CLOUD_VOICES = (
    ('zh_female_vv_uranus_bigtts', 'Vivi 2.0'),
    ('zh_female_xiaohe_uranus_bigtts', '小何 2.0'),
    ('zh_male_m191_uranus_bigtts', '云舟 2.0'),
    ('zh_male_taocheng_uranus_bigtts', '小天 2.0'),
    ('zh_male_liufei_uranus_bigtts', '刘飞 2.0'),
    ('zh_female_sophie_uranus_bigtts', '魅力苏菲 2.0'),
    ('zh_female_qingxinnvsheng_uranus_bigtts', '清新女声 2.0'),
    ('zh_female_cancan_uranus_bigtts', '知性灿灿 2.0'),
    ('zh_female_gaolengyujie_uranus_bigtts', '高冷御姐 2.0'),
    ('zh_female_linjianvhai_uranus_bigtts', '邻家女孩 2.0'),
    ('zh_female_tianmeitaozi_uranus_bigtts', '甜美桃子 2.0'),
    ('zh_female_wenroushunv_uranus_bigtts', '温柔淑女 2.0'),
    ('zh_male_ruyaqingnian_uranus_bigtts', '儒雅青年 2.0'),
    ('zh_male_yangguangqingnian_uranus_bigtts', '阳光青年 2.0'),
    ('zh_female_yingyujiaoxue_uranus_bigtts', 'Tina老师 2.0'),
    ('zh_female_kefunvsheng_uranus_bigtts', '暖阳女声 2.0'),
    ('zh_female_xiaoxue_uranus_bigtts', '儿童绘本 2.0'),
)
CLOUD_VOICE_IDS = tuple(item[0] for item in CLOUD_VOICES)
FFMPEG = Path('/opt/homebrew/bin/ffmpeg')


class SpeechCancelled(RuntimeError):
    """Do not use InterruptedError: subprocess may retry that internally."""


class CloudError(RuntimeError):
    """A bounded, user-actionable cloud failure that may trigger local fallback."""

    def __init__(self, reason, message, code=None, request_id=None):
        self.reason = reason
        self.message = message
        self.code = code
        self.request_id = request_id
        super().__init__(message)


def emit(value):
    print(json.dumps(value, ensure_ascii=False), flush=True)


def read_json_bounded(path, limit):
    try:
        path = Path(path)
        if not path.is_file() or path.stat().st_size > limit:
            return None
        return json.loads(path.read_text())
    except (OSError, ValueError, UnicodeError):
        return None


def sanitize_error(value, limit=500):
    """Strip local paths and accidental secret-shaped fragments from metadata."""
    text = str(value)
    for prefix, replacement in ((str(ROOT), '~/.local/share/qwen-voice-notify'),
                                (str(SCRIPT.parent.parent), '~/.codex/skills/qwen-voice-notify'),
                                (str(HOME), '~')):
        if prefix:
            text = text.replace(prefix, replacement)
    text = re.sub(r'(?i)(x-api-key|authorization)\s*[:=]\s*\S+', r'\1: <redacted>', text)
    text = re.sub(r'\b(?:sk-[A-Za-z0-9_-]{12,}|Bearer\s+[A-Za-z0-9._-]{12,})', '<redacted>', text)
    return text[:limit]


def provider_auth_path():
    base = os.environ.get('XDG_DATA_HOME')
    if base:
        return Path(base).expanduser() / 'opencode' / 'auth.json'
    return HOME / '.local/share/opencode/auth.json'


def plan_key():
    """Resolve the dedicated Agent Plan key from the OpenCode provider store.

    This mirrors ``common.auth_key('volcengine-agent-plan')`` and
    ``ark_harness_mcp.resolve_plan_key``: only the local OpenCode auth store is
    read, nothing is persisted, and the value is never printed or passed to a
    child process. The installed skill cannot import the console repository
    portably, so it reads the same provider entry directly.
    """
    data = read_json_bounded(provider_auth_path(), 262144)
    entry = data.get(CLOUD_PROVIDER) if isinstance(data, dict) else None
    if isinstance(entry, dict) and entry.get('type') == 'api':
        key = entry.get('key')
        if isinstance(key, str) and len(key) >= 8:
            return key
    raise CloudError(
        'missing_key',
        '云端语音未配置 Agent Plan 密钥；请在 OpenCode 中配置 '
        'volcengine-agent-plan，或改用本机离线语音。')


def cloud_key_configured():
    try:
        return bool(plan_key())
    except Exception:
        return False


def local_voice_catalog():
    configuration = read_json_bounded(MODEL / 'config.json', 1_048_576)
    if not isinstance(configuration, dict):
        return None
    talker = configuration.get('talker_config')
    speakers = talker.get('spk_id') if isinstance(talker, dict) else None
    if not isinstance(speakers, dict) or not speakers:
        return None
    catalog = set()
    for candidate in speakers:
        normalized = str(candidate).strip().lower()
        if VOICE_ID.fullmatch(normalized):
            catalog.add(normalized)
    return catalog or None


def local_ready():
    return all((MODEL / name).is_file() for name in MODEL_FILES)


def normalize_rate(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number) or number < RATE_MIN or number > RATE_MAX:
        return None
    return round(number, 2)


def normalize_voice(value, catalog=None, require_catalog=False):
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    if not VOICE_ID.fullmatch(normalized):
        return None
    if require_catalog or catalog:
        if not catalog or normalized not in catalog:
            return None
    return normalized


def normalize_cloud_voice(value):
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    if not CLOUD_VOICE_ID.fullmatch(normalized):
        return None
    return normalized if normalized in CLOUD_VOICE_IDS else None


def load_preferences():
    """Read bounded non-secret preferences with legacy and partial-file support."""
    preferences = {
        'voice': DEFAULT_LOCAL_VOICE,
        'cloud_voice': DEFAULT_CLOUD_VOICE,
        'backend': DEFAULT_BACKEND,
        'rate': DEFAULT_RATE,
    }
    warnings = []
    meta = {'settings': 'legacy', 'voice': 'legacy'}
    try:
        present = SETTINGS.exists()
    except OSError:
        present = False
    if not present:
        return preferences, warnings, meta
    data = read_json_bounded(SETTINGS, SETTINGS_LIMIT)
    if not isinstance(data, dict):
        warnings.append('invalid_saved_settings')
        return preferences, warnings, meta
    meta['settings'] = 'saved'
    catalog = local_voice_catalog()

    voice = data.get('voice')
    if voice is not None:
        normalized = normalize_voice(voice, catalog, require_catalog=True)
        if normalized is None:
            warnings.append('invalid_saved_voice')
        else:
            preferences['voice'] = normalized
            meta['voice'] = 'saved'

    cloud_voice = data.get('cloud_voice')
    if cloud_voice is not None:
        normalized = normalize_cloud_voice(cloud_voice)
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
        normalized = normalize_rate(rate)
        if normalized is None:
            warnings.append('invalid_saved_rate')
        else:
            preferences['rate'] = normalized

    return preferences, warnings, meta


def saved_default_voice():
    """Console-saved local fallback voice; kept for compatibility and status."""
    return load_preferences()[0]['voice']


def resolve_settings(args):
    """Apply saved preferences and validate explicit CLI overrides."""
    preferences, warnings, _meta = load_preferences()
    catalog = local_voice_catalog()

    backend = getattr(args, 'backend', None)
    if backend is None:
        backend = preferences['backend']
    if backend not in BACKENDS:
        raise ValueError('未知的播报后端')

    rate = getattr(args, 'rate', None)
    if rate is None:
        rate = preferences['rate']
    normalized_rate = normalize_rate(rate)
    if normalized_rate is None:
        raise ValueError('语速必须在 0.5–2.0 之间')

    voice = getattr(args, 'voice', None)
    if voice is None:
        voice = preferences['voice']
    else:
        if backend == 'local' and not catalog:
            raise ValueError('本机离线语音不可用：未找到可用的本地音色目录。')
        # The local catalog is required only when a local voice can actually be
        # used. In cloud mode, and in auto mode while the local model is absent,
        # an explicit --voice is just a syntactically safe fallback value: the
        # cloud attempt must not be blocked by a missing local model.
        normalized_voice = normalize_voice(
            voice, catalog, require_catalog=bool(catalog) and backend in ('auto', 'local'))
        if normalized_voice is None:
            raise ValueError('不支持的本地音色')
        voice = normalized_voice

    cloud_voice = getattr(args, 'cloud_voice', None)
    if cloud_voice is None:
        cloud_voice = preferences['cloud_voice']
    else:
        normalized_cloud = normalize_cloud_voice(cloud_voice)
        if normalized_cloud is None:
            raise ValueError('不支持的云端音色')
        cloud_voice = normalized_cloud

    args.backend = backend
    args.rate = normalized_rate
    args.voice = voice
    args.cloud_voice = cloud_voice
    args.settings_warnings = list(warnings)
    return args


def cloud_voice_catalog():
    return [{'id': voice_id, 'label': label} for voice_id, label in CLOUD_VOICES]


# --- local Qwen synthesis and playback -----------------------------------


def speaker_device():
    import sounddevice as sd
    for i, d in enumerate(sd.query_devices()):
        name = d['name'].replace(' ', '').lower()
        if d['max_output_channels'] > 0 and name in ('macmini扬声器', 'macminispeakers', 'macminispeaker'):
            return i, d
    raise RuntimeError('Mac mini 内置扬声器不可用；未更改系统默认输出。')


def identity(pid):
    p = subprocess.run(['/bin/ps', '-p', str(pid), '-o', 'uid=,lstart=,args='], capture_output=True, text=True)
    return p.stdout.strip()


def live_state():
    try:
        data = json.loads(STATE.read_text())
        current = identity(data['pid'])
        if current and current == data['identity'] and str(SCRIPT) in current:
            return data
    except (OSError, ValueError, KeyError):
        pass
    return None


def synth(args):
    import resource
    start = time.monotonic()
    import mlx.core as mx
    import numpy as np
    import soundfile as sf
    from mlx_audio.tts.utils import load_model
    model = load_model(str(MODEL))
    mx.eval(model.parameters())
    loaded = time.monotonic()
    parts = []
    sample_rate = None
    for result in model.generate_custom_voice(
        text=sys.stdin.read(), speaker=args.voice, language=args.language,
        instruct='用自然、清晰、平静的语气说话。', max_tokens=2048,
    ):
        parts.append(np.asarray(result.audio))
        sample_rate = result.sample_rate
    if not parts:
        raise RuntimeError('千问没有生成音频')
    audio = np.concatenate(parts)
    if not np.isfinite(audio).all() or len(audio) == 0:
        raise RuntimeError('生成音频为空或包含无效数值')
    sf.write(args.wav, audio, sample_rate)
    metrics = {
        'load_seconds': round(loaded - start, 3),
        'synthesis_seconds': round(time.monotonic() - loaded, 3),
        'audio_seconds': round(len(audio) / sample_rate, 3),
        'peak_rss_mib': round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024**2, 1),
        'mlx_peak_mib': round(mx.get_peak_memory() / 1024**2, 1),
    }
    Path(args.metrics).write_text(json.dumps(metrics))


def play(args):
    import sounddevice as sd
    import soundfile as sf
    index, _ = speaker_device()
    audio, rate = sf.read(args.wav, dtype='float32', always_2d=True)
    sd.play(audio, rate, device=index, blocking=True)


def audio_seconds(path):
    """Read only the container duration; never loads or plays audio."""
    try:
        import soundfile as sf
        value = float(sf.info(str(path)).duration)
    except Exception:
        return None
    if not math.isfinite(value) or value <= 0:
        return None
    return round(value, 3)


def ffmpeg_to_wav_command(source, dest):
    return [str(FFMPEG), '-nostdin', '-hide_banner', '-loglevel', 'error', '-y',
            '-i', str(source), '-c:a', 'pcm_s16le', str(dest)]


def ffmpeg_atempo_command(source, dest, rate):
    normalized = normalize_rate(rate)
    if normalized is None:
        raise ValueError('语速必须在 0.5–2.0 之间')
    if abs(normalized - 1.0) < 1e-9:
        raise ValueError('1.0x 不需要变速处理')
    return [str(FFMPEG), '-nostdin', '-hide_banner', '-loglevel', 'error', '-y',
            '-i', str(source), '-filter:a', 'atempo={:.2f}'.format(normalized),
            '-c:a', 'pcm_s16le', str(dest)]


def run_ffmpeg(command, timeout=60):
    """Standalone bounded ffmpeg run used by verification and tests."""
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RuntimeError('ffmpeg 超时') from None
    if result.returncode:
        raise RuntimeError('ffmpeg 失败：' + sanitize_error(result.stderr[-800:] or result.stdout[-400:]))
    return result


def run_stage(command, state, stdin_text, timeout, env, phase, old_handlers, label):
    """Run one child in its own process group and always reap it.

    A stop signal raises SpeechCancelled in the parent; the child group is
    killed before the exception continues, so an interrupted synthesis, tempo
    pass or playback never survives the skill process.
    """
    state['phase'] = phase
    STATE.write_text(json.dumps(state, ensure_ascii=False))
    child = None
    try:
        child = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True, env=env,
                                 start_new_session=True)
        try:
            out, err = child.communicate(stdin_text, timeout=timeout)
        except BaseException:
            for sig in old_handlers:
                signal.signal(sig, signal.SIG_IGN)
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            child.communicate()
            raise
        if child.returncode:
            raise RuntimeError('{}: {}'.format(label, err[-3000:] or out[-1000:]))
        return out, err
    finally:
        if child is not None and child.poll() is None:
            child.kill()
            child.wait()


# --- cloud Agent Plan speech ---------------------------------------------


def _cloud_message(reason, code=None):
    base = {
        'missing_key': '云端语音未配置 Agent Plan 密钥；请在 OpenCode 中配置 '
                       'volcengine-agent-plan，或改用本机离线语音。',
        'auth': '云端语音鉴权失败；请检查 Agent Plan 密钥或套餐权限。',
        'quota': '云端语音额度不足或已限流；请检查套餐额度。',
        'network': '云端语音网络不可用。',
        'timeout': '云端语音响应超时。',
        'provider': '云端语音服务暂不可用。',
        'protocol': '云端语音返回格式异常。',
        'empty': '云端语音未返回音频。',
        'truncated': '云端语音返回不完整。',
        'oversized': '云端语音返回内容超过上限。',
        'decode': '云端语音音频无法解码。',
        'invalid': '云端语音请求参数无效。',
    }.get(reason, '云端语音暂不可用。')
    if code is not None:
        base += '（状态 {}）'.format(code)
    return base


def _classify_http(status, detail=''):
    text = (detail or '').lower()
    if status in (401, 403) or any(word in text for word in
                                   ('unauthor', 'forbidden', 'api key', 'apikey', 'permission')):
        return 'auth'
    if status in (402, 408, 429) or any(word in text for word in
                                        ('quota', 'limit', 'balance', 'insufficient', 'exceed')):
        return 'quota' if status != 408 else 'timeout'
    if 500 <= status < 600:
        return 'provider'
    return 'provider'


def _classify_code(code, detail=''):
    text = (detail or '').lower()
    if any(word in text for word in ('unauthor', 'forbidden', 'api key', 'apikey', 'permission')):
        return 'auth'
    if any(word in text for word in ('quota', 'limit', 'balance', 'insufficient', 'exceed', 'too many')):
        return 'quota'
    return 'provider'


def _cloud_request(text, speaker, rate, key, connection_factory=None):
    """One bounded Agent Plan request; returns (audio_bytes, metadata)."""
    speech_rate = max(-50, min(100, int(round((rate - 1) * 100))))
    request_id = uuid.uuid4().hex
    payload = {
        'req_params': {
            'text': text,
            'speaker': speaker,
            'audio_params': {
                'format': CLOUD_FORMAT,
                'sample_rate': CLOUD_SAMPLE_RATE,
                'speech_rate': speech_rate,
            },
        }
    }
    headers = {
        'Content-Type': 'application/json',
        'Accept': 'application/x-ndjson',
        'X-Api-Key': key,
        'X-Api-Resource-Id': CLOUD_RESOURCE,
        'X-Api-Request-Id': request_id,
    }
    deadline = time.monotonic() + CLOUD_TOTAL_TIMEOUT
    connection = None
    holder = {'sock': None, 'fired': False}

    def expire():
        # A socket timeout is an inactivity limit, not a wall-clock limit: a
        # provider that drips one byte just before every timeout could keep a
        # readline alive forever. This watchdog shuts the socket down at the
        # declared total deadline, so the read unblocks and is classified as a
        # timeout even for a slow-drip response.
        holder['fired'] = True
        sock = holder.get('sock')
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    watchdog = threading.Timer(CLOUD_TOTAL_TIMEOUT, expire)
    watchdog.daemon = True
    watchdog.start()
    try:
        if connection_factory is None:
            connection = http.client.HTTPSConnection(
                CLOUD_HOST, timeout=CLOUD_CONNECT_TIMEOUT,
                context=ssl.create_default_context())
        else:
            connection = connection_factory(
                CLOUD_HOST, timeout=CLOUD_CONNECT_TIMEOUT,
                context=ssl.create_default_context())
        holder['sock'] = getattr(connection, 'sock', None)
        if holder['fired']:
            raise CloudError('timeout', _cloud_message('timeout'),
                             request_id=request_id)
        try:
            connection.request('POST', CLOUD_PATH,
                               body=json.dumps(payload).encode('utf-8'), headers=headers)
            # request() performs connect() and installs connection.sock; publish
            # it before the blocking getresponse/readline so the watchdog can
            # shut down a provider that stalls after sending the request.
            holder['sock'] = getattr(connection, 'sock', None)
            if holder['fired']:
                raise CloudError('timeout', _cloud_message('timeout'),
                                 request_id=request_id)
            response = connection.getresponse()
        except (socket.timeout, TimeoutError):
            raise CloudError('timeout', _cloud_message('timeout'),
                             request_id=request_id) from None
        except (OSError, http.client.HTTPException):
            if holder['fired']:
                raise CloudError('timeout', _cloud_message('timeout'),
                                 request_id=request_id) from None
            raise CloudError('network', _cloud_message('network'),
                             request_id=request_id) from None
        if holder['fired']:
            raise CloudError('timeout', _cloud_message('timeout'),
                             request_id=request_id)

        status = getattr(response, 'status', None)
        if status != 200:
            body = b''
            try:
                body = response.read(4096) or b''
            except Exception:
                pass
            if isinstance(body, bytes):
                detail = body.decode('utf-8', 'replace')
            else:
                detail = str(body)
            reason = _classify_http(status, detail)
            raise CloudError(reason, _cloud_message(reason, status),
                             code=status, request_id=request_id)

        chunks = []
        total = 0
        frames = 0
        terminal = False
        while True:
            if holder['fired']:
                raise CloudError('timeout', _cloud_message('timeout'),
                                 request_id=request_id)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CloudError('timeout', _cloud_message('timeout'),
                                 request_id=request_id)
            sock = getattr(connection, 'sock', None)
            if sock is not None:
                try:
                    sock.settimeout(max(0.1, min(CLOUD_CONNECT_TIMEOUT, remaining)))
                except OSError:
                    pass
            try:
                line = response.readline(CLOUD_MAX_LINE + 1)
            except (socket.timeout, TimeoutError):
                raise CloudError('timeout', _cloud_message('timeout'),
                                 request_id=request_id) from None
            except OSError:
                if holder['fired']:
                    raise CloudError('timeout', _cloud_message('timeout'),
                                     request_id=request_id) from None
                raise CloudError('network', _cloud_message('network'),
                                 request_id=request_id) from None
            if holder['fired']:
                raise CloudError('timeout', _cloud_message('timeout'),
                                 request_id=request_id)
            if not line:
                break
            if isinstance(line, str):
                line = line.encode('utf-8')
            if len(line) > CLOUD_MAX_LINE and not line.endswith(b'\n'):
                raise CloudError('oversized', _cloud_message('oversized'),
                                 request_id=request_id)
            if not line.strip():
                continue
            frames += 1
            try:
                frame = json.loads(line)
            except (ValueError, UnicodeError):
                raise CloudError('protocol', _cloud_message('protocol'),
                                 request_id=request_id) from None
            if not isinstance(frame, dict):
                raise CloudError('protocol', _cloud_message('protocol'),
                                 request_id=request_id)
            code = frame.get('code')
            if isinstance(code, str) and code.isdigit():
                code = int(code)
            if isinstance(code, bool) or not isinstance(code, int):
                raise CloudError('protocol', _cloud_message('protocol'),
                                 request_id=request_id)
            message = frame.get('message') or frame.get('msg') or ''
            if not isinstance(message, str):
                message = ''
            if code == CLOUD_TERMINAL:
                terminal = True
                break
            if code != 0:
                reason = _classify_code(code, message)
                raise CloudError(reason, _cloud_message(reason, code),
                                 code=code, request_id=request_id)
            data = frame.get('data')
            if data is None:
                continue
            if not isinstance(data, str):
                raise CloudError('protocol', _cloud_message('protocol'),
                                 request_id=request_id)
            if not data:
                continue
            try:
                chunk = base64.b64decode(data, validate=True)
            except (ValueError, binascii.Error):
                raise CloudError('protocol', _cloud_message('protocol'),
                                 request_id=request_id) from None
            if not chunk:
                continue
            total += len(chunk)
            if total > CLOUD_MAX_BYTES:
                raise CloudError('oversized', _cloud_message('oversized'),
                                 request_id=request_id)
            chunks.append(chunk)
        if holder['fired']:
            raise CloudError('timeout', _cloud_message('timeout'),
                             request_id=request_id)
        if not terminal:
            raise CloudError('truncated', _cloud_message('truncated'),
                             request_id=request_id)
        if not chunks:
            raise CloudError('empty', _cloud_message('empty'),
                             request_id=request_id)
        return b''.join(chunks), {
            'model': CLOUD_RESOURCE,
            'code': CLOUD_TERMINAL,
            'request_id': request_id,
            'frames': frames,
            'bytes': total,
            'format': CLOUD_FORMAT,
        }
    finally:
        watchdog.cancel()
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass


def cloud_speech(text, speaker, rate, key=None, connection_factory=None):
    """Validate one short cloud synthesis and return raw MP3 bytes plus metadata."""
    if not isinstance(text, str) or not text or len(text) > 200:
        raise ValueError('播报内容须为 1–200 字符；长文本请分段。')
    if normalize_cloud_voice(speaker) is None:
        raise ValueError('不支持的云端音色')
    normalized_rate = normalize_rate(rate)
    if normalized_rate is None:
        raise ValueError('语速必须在 0.5–2.0 之间')
    key = key or plan_key()
    try:
        return _cloud_request(text, speaker, normalized_rate, key, connection_factory)
    except SpeechCancelled:
        raise
    except CloudError:
        raise
    except (OSError, http.client.HTTPException):
        raise CloudError('network', _cloud_message('network')) from None


# --- routing and reporting ------------------------------------------------


def _apply_local(args, state, text, tmp_path, env, old_handlers, report):
    """Synthesize locally, exit the model process, then apply optional tempo."""
    if not local_ready():
        raise RuntimeError('本机 Qwen3-TTS 模型不完整。')
    if abs(args.rate - 1.0) > 1e-9 and not FFMPEG.is_file():
        raise RuntimeError('本机语速处理需要 ffmpeg，但未找到 /opt/homebrew/bin/ffmpeg。')

    local_wav = tmp_path / 'local.wav'
    metrics_path = tmp_path / 'metrics.json'
    state['backend'] = 'local'
    command = [sys.executable, str(SCRIPT), '_synth', '--wav', str(local_wav),
               '--metrics', str(metrics_path), '--voice', args.voice,
               '--language', args.language]
    run_stage(command, state, text, args.timeout, env, 'synthesis', old_handlers, '_synth')
    metrics = read_json_bounded(metrics_path, 65536) or {}
    for key in ('load_seconds', 'synthesis_seconds', 'peak_rss_mib', 'mlx_peak_mib'):
        if key in metrics:
            report[key] = metrics.get(key)
    if 'audio_seconds' in metrics:
        report['synthesis_audio_seconds'] = metrics.get('audio_seconds')
    report['inference_exited_before_playback'] = True

    playback_wav = local_wav
    if abs(args.rate - 1.0) > 1e-9:
        tempo_wav = tmp_path / 'tempo.wav'
        run_stage(ffmpeg_atempo_command(local_wav, tempo_wav, args.rate),
                  state, '', 60, env, 'tempo', old_handlers, 'ffmpeg')
        report['tempo_applied'] = True
        playback_wav = tempo_wav
    else:
        report['tempo_applied'] = False
    return playback_wav


def speak(args):
    text = sys.stdin.read().strip()
    if not text or len(text) > 200:
        raise ValueError('播报内容须为 1–200 字符；长文本请分段。')

    with (ROOT / 'speech.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            emit({'status': 'busy', 'message': '已有播报正在进行，本次未排队。'})
            return 3
        # Check the fixed output device before any network or model work.
        _, device = speaker_device()
        state = {
            'pid': os.getpid(),
            'identity': identity(os.getpid()),
            'started_at': time.time(),
            'backend': args.backend,
            'rate': args.rate,
        }
        STATE.write_text(json.dumps(state, ensure_ascii=False))
        started = time.monotonic()
        env = dict(os.environ, HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1',
                   TOKENIZERS_PARALLELISM='false')

        def cancelled(signum, frame):
            raise SpeechCancelled('播报已停止')

        old_handlers = {sig: signal.signal(sig, cancelled)
                        for sig in (signal.SIGTERM, signal.SIGINT)}
        report = {
            'status': 'failed',
            'device': device['name'],
            'requested_backend': args.backend,
            'backend': args.backend,
            'rate': args.rate,
            'voice': args.voice if args.backend == 'local' else args.cloud_voice,
            'local_voice': args.voice,
            'cloud_voice': args.cloud_voice,
            'fallback': False,
            'fallback_reason': None,
            'fallback_detail': None,
            'cloud': None,
            'cloud_status': None,
            'audio_seconds': None,
        }
        warnings = getattr(args, 'settings_warnings', None)
        if warnings:
            report['settings_warnings'] = list(warnings)

        try:
            with tempfile.TemporaryDirectory(prefix='audio-', dir=ROOT) as tmp:
                tmp_path = Path(tmp)
                playback_wav = None
                cloud_error = None

                if args.backend in ('auto', 'cloud'):
                    state['backend'] = 'cloud'
                    state['phase'] = 'cloud'
                    STATE.write_text(json.dumps(state, ensure_ascii=False))
                    try:
                        audio, cloud_meta = cloud_speech(
                            text, args.cloud_voice, args.rate)
                        mp3_path = tmp_path / 'cloud.mp3'
                        mp3_path.write_bytes(audio)
                        if not FFMPEG.is_file():
                            raise CloudError(
                                'decode',
                                '云端语音解码需要 ffmpeg，但未找到 /opt/homebrew/bin/ffmpeg。',
                                request_id=cloud_meta.get('request_id'))
                        cloud_wav = tmp_path / 'cloud.wav'
                        run_stage(ffmpeg_to_wav_command(mp3_path, cloud_wav),
                                  state, '', 60, env, 'cloud_decode', old_handlers, 'ffmpeg')
                        seconds = audio_seconds(cloud_wav)
                        if not seconds:
                            raise CloudError('decode', _cloud_message('decode'),
                                             request_id=cloud_meta.get('request_id'))
                        cloud_meta['audio_seconds'] = seconds
                        report['cloud'] = cloud_meta
                        report['cloud_status'] = {
                            'model': CLOUD_RESOURCE,
                            'code': cloud_meta.get('code'),
                            'request_id': cloud_meta.get('request_id'),
                            'status': 'ok',
                        }
                        playback_wav = cloud_wav
                        report['backend'] = 'cloud'
                        report['voice'] = args.cloud_voice
                    except SpeechCancelled:
                        raise
                    except CloudError as exc:
                        cloud_error = exc
                        report['cloud_status'] = {
                            'model': CLOUD_RESOURCE,
                            'reason': exc.reason,
                            'code': exc.code,
                            'request_id': exc.request_id,
                        }
                        if args.backend == 'cloud':
                            raise RuntimeError('云端语音不可用：' + exc.message) from None
                        report['fallback_reason'] = exc.reason
                        report['fallback_detail'] = exc.message
                        report['fallback'] = True
                    except Exception as exc:
                        cloud_error = CloudError('decode', _cloud_message('decode'))
                        report['cloud_status'] = {
                            'model': CLOUD_RESOURCE,
                            'reason': cloud_error.reason,
                        }
                        if args.backend == 'cloud':
                            raise RuntimeError('云端语音不可用：' + sanitize_error(exc)) from None
                        report['fallback_reason'] = cloud_error.reason
                        report['fallback_detail'] = cloud_error.message
                        report['fallback'] = True

                if playback_wav is None:
                    report['backend'] = 'local'
                    report['voice'] = args.voice
                    try:
                        playback_wav = _apply_local(
                            args, state, text, tmp_path, env, old_handlers, report)
                    except SpeechCancelled:
                        raise
                    except Exception as exc:
                        if cloud_error is not None:
                            raise RuntimeError(
                                '云端语音不可用（{}）；本机离线语音也失败：{}'.format(
                                    cloud_error.message, sanitize_error(exc))) from None
                        raise

                seconds = audio_seconds(playback_wav)
                if not seconds:
                    raise RuntimeError('播报音频无法解码或时长无效。')
                report['audio_seconds'] = seconds
                run_stage([sys.executable, str(SCRIPT), '_play', '--wav', str(playback_wav)],
                          state, '', args.timeout, env, 'playback', old_handlers, '_play')
                report['status'] = 'played'
                return 0
        except (Exception, KeyboardInterrupt) as exc:
            report['error'] = sanitize_error(exc)
            return 1
        finally:
            report['total_seconds'] = round(time.monotonic() - started, 3)
            report['finished_at'] = time.time()
            try:
                LAST.write_text(json.dumps(report, ensure_ascii=False, indent=2))
            except OSError:
                pass
            try:
                STATE.unlink(missing_ok=True)
            except OSError:
                pass
            for sig, handler in old_handlers.items():
                signal.signal(sig, handler)
            emit(report)


def status_payload():
    device = None
    speaker_error = None
    try:
        _, info = speaker_device()
        device = info['name']
    except Exception as exc:
        speaker_error = sanitize_error(exc)
    missing = [name for name in MODEL_FILES if not (MODEL / name).is_file()]
    preferences, warnings, meta = load_preferences()
    return {
        'status': 'ready' if not missing else 'model_incomplete',
        'model': str(MODEL),
        'missing_files': missing,
        'device': device,
        'speaker_error': speaker_error,
        'default_voice': preferences['voice'],
        'cloud_voice': preferences['cloud_voice'],
        'backend': preferences['backend'],
        'rate': preferences['rate'],
        'cloud_model': CLOUD_RESOURCE,
        'cloud_key_configured': cloud_key_configured(),
        'cloud_voices': cloud_voice_catalog(),
        'settings_source': meta['settings'],
        'voice_source': meta['voice'],
        'settings_warnings': warnings,
        'active': live_state(),
        'last_run': read_json_bounded(LAST, 65536),
    }


def rate_argument(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError('语速必须在 0.5–2.0 之间') from None
    normalized = normalize_rate(number)
    if normalized is None:
        raise argparse.ArgumentTypeError('语速必须在 0.5–2.0 之间')
    return normalized


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)

    speak_parser = sub.add_parser('speak')
    speak_parser.add_argument('--voice', default=None)
    speak_parser.add_argument('--cloud-voice', default=None)
    speak_parser.add_argument('--backend', choices=BACKENDS, default=None)
    speak_parser.add_argument('--rate', type=rate_argument, default=None)
    speak_parser.add_argument('--language', default='Chinese')
    speak_parser.add_argument('--timeout', type=float, default=180)

    synth_parser = sub.add_parser('_synth')
    synth_parser.add_argument('--voice', default=LEGACY_DEFAULT_VOICE)
    synth_parser.add_argument('--language', default='Chinese')
    synth_parser.add_argument('--wav', required=True)
    synth_parser.add_argument('--metrics', required=True)

    play_parser = sub.add_parser('_play')
    play_parser.add_argument('--wav', required=True)
    sub.add_parser('status')
    sub.add_parser('stop')

    args = parser.parse_args(argv)
    if args.command == '_synth':
        synth(args)
    elif args.command == '_play':
        play(args)
    elif args.command == 'speak':
        if args.timeout <= 0:
            raise ValueError('timeout 必须大于零')
        args = resolve_settings(args)
        return speak(args)
    elif args.command == 'stop':
        state = live_state()
        if state:
            os.kill(state['pid'], signal.SIGTERM)
        emit({'status': 'stop_requested' if state else 'idle'})
    else:
        emit(status_payload())
    return 0


def run(argv=None):
    """CLI entry point that turns validation failures into one JSON error."""
    try:
        return main(argv)
    except Exception as exc:
        emit({'status': 'error', 'error': sanitize_error(exc)})
        return 1


if __name__ == '__main__':
    sys.exit(run())
