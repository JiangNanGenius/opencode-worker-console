"""Speech settings and routing tests.

Covers the console catalog/persistence/ownership contract and the installed
skill's cloud-first routing with local fallback. No test plays audio: cloud
connections are faked, playback children are stubbed and the only real external
binary used is ffmpeg for file-level tempo/decode verification.
"""
from __future__ import annotations

import argparse
import contextlib
import http.client
import importlib.util
import io
import json
import math
import os
from pathlib import Path
import signal
import stat
import struct
import sys
import tempfile
import threading
import time
import unittest
import wave
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import common
import console
import console_auth
import local_voice


VOICES = {'serena': 3066, 'vivian': 3065, 'uncle_fu': 3010, 'ryan': 3061,
          'aiden': 2861, 'ono_anna': 2873, 'sohee': 2864, 'eric': 2875, 'dylan': 2878}
DIALECTS = {'serena': False, 'vivian': False, 'uncle_fu': False, 'ryan': False,
            'aiden': False, 'ono_anna': False, 'sohee': False,
            'eric': 'sichuan_dialect', 'dylan': 'beijing_dialect'}
REFERENCE_NOTIFY = ROOT / 'references' / 'qwen-voice-notify' / 'notify.py'
CLOUD_VOICE = 'zh_female_vv_uranus_bigtts'
CLOUD_VOICE_ALT = 'zh_female_gaolengyujie_uranus_bigtts'
FAKE_PLAN_KEY = 'fake-plan-key-0123456789abcdef'


def write_wav(path, seconds=0.2, rate=24000, freq=440.0, amplitude=12000):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    frames = max(1, int(round(rate * seconds)))
    with wave.open(str(path), 'wb') as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        data = bytearray()
        for index in range(frames):
            value = int(amplitude * math.sin(2 * math.pi * freq * index / rate))
            data += struct.pack('<h', value)
        handle.writeframes(bytes(data))
    return path


def read_wav(path):
    with wave.open(str(path), 'rb') as handle:
        channels = handle.getnchannels()
        rate = handle.getframerate()
        width = handle.getsampwidth()
        raw = handle.readframes(handle.getnframes())
    if width != 2:
        raise AssertionError('test WAV must be 16-bit')
    samples = list(struct.unpack('<{}h'.format(len(raw) // 2), raw))
    return rate, channels, samples


def wav_frequency(path):
    rate, channels, samples = read_wav(path)
    mono = samples[::channels]
    duration = len(mono) / float(rate)
    if duration <= 0:
        return 0.0
    crossings = 0
    previous = mono[0]
    for sample in mono[1:]:
        if (previous < 0 <= sample) or (previous >= 0 > sample):
            crossings += 1
        previous = sample
    return crossings / 2.0 / duration


def load_notify(home):
    name = 'qwen_notify_test_' + str(abs(hash(str(home))))
    spec = importlib.util.spec_from_file_location(name, REFERENCE_NOTIFY)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    with patch.dict(os.environ, {'HOME': str(home)}):
        spec.loader.exec_module(module)
    return module


class FakeStdin:
    def __init__(self):
        self.value = ''

    def write(self, text):
        self.value += text

    def flush(self):
        pass

    def close(self):
        pass


class FakeProcess:
    def __init__(self, argv, **kwargs):
        self.argv = argv
        self.kwargs = kwargs
        self.pid = 4321
        self.stdin = FakeStdin()
        self.stdout = io.StringIO()
        self.stderr = io.StringIO()
        self.communicated = False
        self.killed = False

    def communicate(self, input=None):
        self.communicated = True
        return '', ''

    def kill(self):
        self.killed = True


class FakeThread:
    def __init__(self, target=None, args=(), daemon=None):
        self.target = target
        self.args = args

    def start(self):
        pass


class LocalVoiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.skill = self.root / 'skill'
        self.runtime = self.root / 'runtime'
        self.config = self.root / 'config'
        self.auth = self.root / 'auth.json'
        (self.skill / 'scripts').mkdir(parents=True)
        (self.runtime / 'model').mkdir(parents=True)
        (self.runtime / 'venv' / 'bin').mkdir(parents=True)
        (self.runtime / 'venv' / 'bin' / 'python').write_text('#!/bin/sh\nexit 0\n')
        (self.skill / 'scripts' / 'notify.py').write_text(
            '#!/usr/bin/env python3\nimport json,sys\nif len(sys.argv)>1 and sys.argv[1]=="status":\n'
            '    print(json.dumps({"status":"ready","device":"Mac mini扬声器"}))\n')
        model = self.runtime / 'model'
        for name in local_voice.MODEL_FILES:
            (model / name).parent.mkdir(parents=True, exist_ok=True)
            (model / name).write_text('{}' if name.endswith('.json') else '')
        (model / 'config.json').write_text(json.dumps(
            {'talker_config': {'spk_id': VOICES, 'spk_is_dialect': DIALECTS}}))
        self.write_auth(True)
        self.patchers = [
            patch.object(local_voice, 'HOME', self.root),
            patch.object(local_voice, 'SKILL_DIR', self.skill),
            patch.object(local_voice, 'NOTIFY', self.skill / 'scripts' / 'notify.py'),
            patch.object(local_voice, 'RUNTIME', self.runtime),
            patch.object(local_voice, 'VENV_PYTHON', self.runtime / 'venv' / 'bin' / 'python'),
            patch.object(local_voice, 'MODEL', model),
            patch.object(local_voice, 'ACTIVE', self.runtime / 'active.json'),
            patch.object(local_voice, 'LAST_RUN', self.runtime / 'last-run.json'),
            patch.object(local_voice, 'SETTINGS_DIR', self.config),
            patch.object(local_voice, 'SETTINGS', self.config / 'settings.json'),
            patch.object(local_voice, 'OWNED', self.config / 'preview.json'),
            patch.object(local_voice, 'PREVIEW_RESULT', self.config / 'preview-result.json'),
            patch.object(local_voice, '_auth_store', return_value=self.auth),
        ]
        for patcher in self.patchers:
            patcher.start()
        local_voice._PROBE.update({'at': None, 'value': None})

    def tearDown(self):
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.tmp.cleanup()

    def write_auth(self, present):
        if present:
            self.auth.write_text(json.dumps({
                'volcengine-agent-plan': {'type': 'api', 'key': FAKE_PLAN_KEY},
            }))
        else:
            self.auth.unlink(missing_ok=True)

    def write_settings(self, data):
        self.config.mkdir(mode=0o700, parents=True, exist_ok=True)
        (self.config / 'settings.json').write_text(
            data if isinstance(data, str) else json.dumps(data))

    def read_settings_file(self):
        return json.loads((self.config / 'settings.json').read_text())

    def write_active(self, pid=999, identity='x', backend='local', rate=1.1):
        (self.runtime / 'active.json').write_text(json.dumps(
            {'pid': pid, 'identity': identity, 'phase': 'playback',
             'backend': backend, 'rate': rate, 'started_at': 1.0}))

    def write_owned(self, pid=999, identity='x', voice='vivian', started='start-token',
                    backend='local', rate=1.1, cloud_voice=CLOUD_VOICE):
        self.config.mkdir(mode=0o700, parents=True, exist_ok=True)
        (self.config / 'preview.json').write_text(json.dumps(
            {'pid': pid, 'identity': identity, 'started': started, 'voice': voice,
             'cloud_voice': cloud_voice, 'backend': backend, 'rate': rate,
             'started_at': 1.0}))

    def test_catalog_comes_from_installed_voice_metadata(self):
        voices, reason, detail = local_voice.catalog()
        self.assertIsNone(reason)
        self.assertIsNone(detail)
        self.assertEqual([voice['id'] for voice in voices], list(VOICES))
        labels = {voice['id']: voice['label'] for voice in voices}
        self.assertEqual(labels['uncle_fu'], 'Uncle Fu')
        self.assertEqual(labels['ono_anna'], 'Ono Anna')
        self.assertEqual(labels['vivian'], 'Vivian')
        dialects = {voice['id']: voice['dialect'] for voice in voices}
        self.assertEqual(dialects['eric'], 'sichuan_dialect')
        self.assertEqual(dialects['dylan'], 'beijing_dialect')
        self.assertIsNone(dialects['vivian'])
        for voice in voices:
            self.assertEqual(set(voice), {'id', 'label', 'dialect'})

    def test_missing_model_or_runtime_is_reported_not_faked(self):
        (self.runtime / 'model' / 'config.json').unlink()
        self.assertEqual(local_voice.catalog()[1], 'model_missing')
        available, reason, _, _ = local_voice.runtime_state()
        self.assertFalse(available)
        self.assertEqual(reason, 'model_missing')
        (self.runtime / 'model' / 'config.json').write_text(
            json.dumps({'talker_config': {'spk_id': VOICES, 'spk_is_dialect': DIALECTS}}))
        (self.runtime / 'model' / 'model.safetensors').unlink()
        self.assertEqual(local_voice.runtime_state()[1], 'model_missing')
        (self.runtime / 'model' / 'model.safetensors').write_text('')
        (self.skill / 'scripts' / 'notify.py').unlink()
        self.assertEqual(local_voice.runtime_state()[1], 'runtime_missing')

    def test_save_is_validated_atomic_and_owner_only(self):
        saved = local_voice.save('SERENA')
        self.assertEqual(saved['voice'], 'serena')
        self.assertEqual(saved['cloud_voice'], CLOUD_VOICE)
        self.assertEqual(saved['backend'], 'auto')
        self.assertEqual(saved['rate'], 1.1)
        payload = self.read_settings_file()
        self.assertEqual(payload['schema'], 2)
        self.assertEqual(payload['voice'], 'serena')
        self.assertEqual(payload['cloud_voice'], CLOUD_VOICE)
        self.assertEqual(payload['backend'], 'auto')
        self.assertEqual(payload['rate'], 1.1)
        self.assertEqual(stat.S_IMODE((self.config / 'settings.json').stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.config.stat().st_mode), 0o700)
        self.assertEqual(sorted(path.name for path in self.config.iterdir()), ['settings.json'])
        self.assertEqual(local_voice.read_settings(local_voice.catalog()[0]),
                         ('serena', 'saved', None))
        self.assertEqual(local_voice.save(' vivian ')['voice'], 'vivian')

    def test_save_rejects_unknown_voices_and_injection(self):
        for value in ('../../bin/sh', 'vivian; rm -rf /', 'vivian && echo', 'k3', '', None,
                      'A' * 40, 'uncle-fu', 'vivian/../x'):
            with self.assertRaises(ValueError):
                local_voice.save(value)
        with self.assertRaises(ValueError):
            local_voice.save({'backend': 'speech'})
        with self.assertRaises(ValueError):
            local_voice.save({'cloud_voice': '../../bin/sh'})
        for rate in (0.49, 2.01, '1.1', True, None, float('nan')):
            with self.assertRaises(ValueError):
                local_voice.save({'rate': rate})
        with self.assertRaises(ValueError):
            local_voice.save({})
        self.assertFalse((self.config / 'settings.json').exists())

    def test_settings_merge_preserves_new_fields_and_unknown_keys(self):
        self.write_settings({'schema': 1, 'voice': 'serena', 'cloud_voice': CLOUD_VOICE_ALT,
                             'backend': 'cloud', 'rate': 1.35, 'future': 'keep'})
        saved = local_voice.save('vivian')
        self.assertEqual(saved['voice'], 'vivian')
        self.assertEqual(saved['cloud_voice'], CLOUD_VOICE_ALT)
        self.assertEqual(saved['backend'], 'cloud')
        self.assertEqual(saved['rate'], 1.35)
        payload = self.read_settings_file()
        self.assertEqual(payload['future'], 'keep')
        self.assertEqual(payload['schema'], 2)

        local_voice.save({'rate': 1.5})
        payload = self.read_settings_file()
        self.assertEqual(payload['voice'], 'vivian')
        self.assertEqual(payload['cloud_voice'], CLOUD_VOICE_ALT)
        self.assertEqual(payload['backend'], 'cloud')
        self.assertEqual(payload['rate'], 1.5)

        local_voice.save({'backend': 'auto'})
        payload = self.read_settings_file()
        self.assertEqual(payload['voice'], 'vivian')
        self.assertEqual(payload['cloud_voice'], CLOUD_VOICE_ALT)
        self.assertEqual(payload['backend'], 'auto')
        self.assertEqual(payload['rate'], 1.5)

    def test_missing_rate_defaults_and_old_save_cannot_reset_new_fields(self):
        self.write_settings({'schema': 1, 'voice': 'serena'})
        preferences, warnings, meta = local_voice.read_preferences(local_voice.catalog()[0])
        self.assertEqual(preferences['rate'], 1.1)
        self.assertEqual(preferences['backend'], 'auto')
        self.assertEqual(preferences['cloud_voice'], CLOUD_VOICE)
        self.assertEqual(preferences['voice'], 'serena')
        self.assertEqual(warnings, [])
        self.assertEqual(meta['settings'], 'saved')
        self.assertEqual(meta['voice'], 'saved')

        local_voice.save('vivian')
        payload = self.read_settings_file()
        self.assertEqual(payload['rate'], 1.1)
        self.assertEqual(payload['backend'], 'auto')
        self.assertEqual(payload['cloud_voice'], CLOUD_VOICE)

        self.write_settings({'cloud_voice': CLOUD_VOICE_ALT, 'backend': 'local', 'rate': 1.2})
        local_voice.save('serena')
        payload = self.read_settings_file()
        self.assertEqual(payload['voice'], 'serena')
        self.assertEqual(payload['cloud_voice'], CLOUD_VOICE_ALT)
        self.assertEqual(payload['backend'], 'local')
        self.assertEqual(payload['rate'], 1.2)

    def test_settings_fall_back_to_legacy_vivian(self):
        voices, _, _ = local_voice.catalog()
        self.assertEqual(local_voice.read_settings(voices), ('Vivian', 'legacy', None))
        self.config.mkdir(parents=True, exist_ok=True)
        (self.config / 'settings.json').write_text('{not json')
        self.assertEqual(local_voice.read_settings(voices),
                         ('Vivian', 'legacy', 'invalid_saved_voice'))
        (self.config / 'settings.json').write_text(json.dumps({'voice': 'not_a_voice'}))
        self.assertEqual(local_voice.read_settings(voices),
                         ('Vivian', 'legacy', 'invalid_saved_voice'))

    def test_invalid_new_fields_fall_back_with_warnings(self):
        self.write_settings({'voice': 'serena', 'cloud_voice': 'not_a_cloud_voice',
                             'backend': 'speech', 'rate': 9})
        preferences, warnings, _meta = local_voice.read_preferences(local_voice.catalog()[0])
        self.assertEqual(preferences['voice'], 'serena')
        self.assertEqual(preferences['cloud_voice'], CLOUD_VOICE)
        self.assertEqual(preferences['backend'], 'auto')
        self.assertEqual(preferences['rate'], 1.1)
        self.assertIn('invalid_saved_cloud_voice', warnings)
        self.assertIn('invalid_saved_backend', warnings)
        self.assertIn('invalid_saved_rate', warnings)

    def test_cloud_settings_save_without_local_catalog_preserves_voice(self):
        self.write_settings({'voice': 'serena', 'cloud_voice': CLOUD_VOICE})
        (self.runtime / 'model' / 'config.json').unlink()
        saved = local_voice.save({'cloud_voice': CLOUD_VOICE_ALT, 'backend': 'cloud',
                                  'rate': 1.4})
        self.assertEqual(saved['voice'], 'serena')
        self.assertEqual(saved['cloud_voice'], CLOUD_VOICE_ALT)
        payload = self.read_settings_file()
        self.assertEqual(payload['voice'], 'serena')
        self.assertEqual(payload['backend'], 'cloud')
        self.assertEqual(payload['rate'], 1.4)

    def test_view_reports_cloud_catalog_and_key_metadata(self):
        self.write_settings({'voice': 'serena', 'cloud_voice': CLOUD_VOICE_ALT,
                             'backend': 'cloud', 'rate': 1.25})
        with patch.object(local_voice, '_probe', return_value={'device': 'Mac mini扬声器',
                                                               'status': 'ready'}):
            result = local_voice.view()
        self.assertTrue(result['available'])
        self.assertTrue(result['cloud_key_configured'])
        self.assertTrue(result['cloud_ready'])
        self.assertEqual(result['backend'], 'cloud')
        self.assertEqual(result['rate'], 1.25)
        self.assertEqual(result['cloud_voice'], CLOUD_VOICE_ALT)
        self.assertEqual(result['cloud_default'], CLOUD_VOICE)
        self.assertEqual(len(result['cloud_voices']), len(local_voice.CLOUD_VOICES))
        self.assertEqual(result['cloud']['key_configured'], True)
        self.assertEqual(result['cloud']['model'], 'seed-tts-2.0')
        self.assertEqual(result['default_voice'], 'serena')
        self.assertEqual(result['speaker'], {'name': 'Mac mini扬声器'})
        dumped = json.dumps(result, ensure_ascii=False)
        self.assertNotIn(FAKE_PLAN_KEY, dumped)

        self.write_auth(False)
        with patch.object(local_voice, '_probe', return_value=None):
            missing = local_voice.view()
        self.assertFalse(missing['cloud_key_configured'])
        self.assertFalse(missing['cloud_ready'])

    def test_preview_uses_fixed_argv_and_fixed_sentence(self):
        process = MagicMock()
        process.pid = 4321
        fake_stdin = FakeStdin()
        process.stdin = fake_stdin
        with patch.object(local_voice.subprocess, 'Popen', return_value=process) as popen, \
                patch.object(local_voice, '_identity', return_value='fake identity'), \
                patch.object(local_voice, '_process_start', return_value='start-token'), \
                patch.object(local_voice.threading, 'Thread', FakeThread):
            result = local_voice.preview({
                'backend': 'cloud', 'rate': 1.25,
                'cloud_voice': CLOUD_VOICE_ALT, 'voice': 'vivian'})
        self.assertEqual(result['status'], 'started')
        self.assertEqual(result['voice'], 'vivian')
        self.assertEqual(result['backend'], 'cloud')
        self.assertEqual(result['rate'], 1.25)
        argv, kwargs = popen.call_args
        self.assertEqual(argv[0], [str(self.runtime / 'venv' / 'bin' / 'python'),
                                   str(self.skill / 'scripts' / 'notify.py'),
                                   'speak', '--backend', 'cloud', '--rate', '1.25',
                                   '--voice', 'vivian', '--cloud-voice', CLOUD_VOICE_ALT,
                                   '--timeout', '120'])
        self.assertTrue(kwargs['text'])
        self.assertNotIn('shell', kwargs)
        self.assertEqual(kwargs['env']['HF_HUB_OFFLINE'], '1')
        self.assertEqual(fake_stdin.value, local_voice.PREVIEW_TEXT)
        record = json.loads((self.config / 'preview.json').read_text())
        self.assertEqual(record['pid'], 4321)
        self.assertEqual(record['voice'], 'vivian')
        self.assertEqual(record['cloud_voice'], CLOUD_VOICE_ALT)
        self.assertEqual(record['backend'], 'cloud')
        self.assertEqual(record['rate'], 1.25)

    def test_preview_rejects_injection_and_unavailable_runtime(self):
        with self.assertRaises(ValueError):
            local_voice.preview({'voice': '../../bin/sh'})
        with self.assertRaises(ValueError):
            local_voice.preview({'voice': 'vivian --timeout 0'})
        with self.assertRaises(ValueError):
            local_voice.preview({'backend': 'speech'})
        with self.assertRaises(ValueError):
            local_voice.preview({'cloud_voice': '../../bin/sh'})
        for rate in (0.49, 2.01, '1.1', True):
            with self.assertRaises(ValueError):
                local_voice.preview({'rate': rate})
        (self.runtime / 'model' / 'model.safetensors').unlink()
        with self.assertRaisesRegex(RuntimeError, 'incomplete'):
            local_voice.preview({'backend': 'local', 'voice': 'vivian'})
        with patch.object(local_voice.subprocess, 'Popen') as popen, \
                patch.object(local_voice, '_identity', return_value='fake identity'), \
                patch.object(local_voice, '_process_start', return_value='start-token'), \
                patch.object(local_voice.threading, 'Thread', FakeThread):
            process = MagicMock()
            process.pid = 4321
            process.stdin = FakeStdin()
            popen.return_value = process
            result = local_voice.preview({'backend': 'cloud', 'voice': 'vivian'})
        self.assertEqual(result['status'], 'started')

    def test_preview_auto_without_key_uses_local_but_cloud_only_requires_key(self):
        self.write_auth(False)
        with patch.object(local_voice.subprocess, 'Popen') as popen, \
                patch.object(local_voice, '_identity', return_value='fake identity'), \
                patch.object(local_voice, '_process_start', return_value='start-token'), \
                patch.object(local_voice.threading, 'Thread', FakeThread):
            process = MagicMock()
            process.pid = 4321
            process.stdin = FakeStdin()
            popen.return_value = process
            result = local_voice.preview({'backend': 'auto', 'voice': 'vivian'})
        self.assertEqual(result['status'], 'started')
        with self.assertRaisesRegex(RuntimeError, '云端密钥未配置'):
            local_voice.preview({'backend': 'cloud', 'voice': 'vivian'})
        (self.runtime / 'model' / 'config.json').unlink()
        with self.assertRaisesRegex(RuntimeError, '本机离线语音也不可用'):
            local_voice.preview({'backend': 'auto'})

    def test_preview_auto_and_cloud_start_without_local_model(self):
        (self.runtime / 'model' / 'config.json').unlink()
        with patch.object(local_voice.subprocess, 'Popen') as popen, \
                patch.object(local_voice, '_identity', return_value='fake identity'), \
                patch.object(local_voice, '_process_start', return_value='start-token'), \
                patch.object(local_voice.threading, 'Thread', FakeThread):
            process = MagicMock()
            process.pid = 4321
            process.stdin = FakeStdin()
            popen.return_value = process
            auto = local_voice.preview({'backend': 'auto', 'rate': 1.1})
        self.assertEqual(auto['status'], 'started')
        argv = popen.call_args[0][0]
        self.assertIn('auto', argv)
        self.assertIn('vivian', argv)
        local_voice._clear_owned()

        with patch.object(local_voice.subprocess, 'Popen') as popen, \
                patch.object(local_voice, '_identity', return_value='fake identity'), \
                patch.object(local_voice, '_process_start', return_value='start-token'), \
                patch.object(local_voice.threading, 'Thread', FakeThread):
            process = MagicMock()
            process.pid = 4321
            process.stdin = FakeStdin()
            popen.return_value = process
            cloud = local_voice.preview({'backend': 'cloud', 'rate': 1.1,
                                         'voice': 'vivian', 'cloud_voice': CLOUD_VOICE})
        self.assertEqual(cloud['status'], 'started')
        local_voice._clear_owned()
        with self.assertRaisesRegex(RuntimeError, 'incomplete'):
            local_voice.preview({'backend': 'local', 'voice': 'vivian'})

    def test_preview_reports_busy_and_never_steals_other_playback(self):
        identity = '501 Sun Oct 25 2026 ' + str(self.skill / 'scripts' / 'notify.py')
        self.write_active(pid=777, identity=identity)
        with patch.object(local_voice, '_identity', return_value=identity), \
                patch.object(local_voice.subprocess, 'Popen') as popen:
            self.assertEqual(local_voice.preview({'voice': 'vivian'}), {'status': 'busy'})
        popen.assert_not_called()

    def test_pending_owned_blocks_duplicate_preview_before_active_exists(self):
        identity = '501 Sun Oct 25 2026 ' + str(self.skill / 'scripts' / 'notify.py')
        self.write_owned(pid=4321, identity=identity, voice='vivian')
        with patch.object(local_voice, '_process_start', return_value='start-token'), \
                patch.object(local_voice, '_identity', return_value=identity), \
                patch.object(local_voice.subprocess, 'Popen') as popen, \
                patch.object(local_voice, '_probe', return_value={'device': 'Mac mini扬声器'}):
            self.assertEqual(local_voice.preview({'voice': 'serena'}),
                             {'status': 'already_running', 'voice': 'vivian',
                              'backend': 'local', 'rate': 1.1})
            view = local_voice.view()
        popen.assert_not_called()
        self.assertTrue(view['preview']['running'])
        self.assertTrue(view['preview']['owned'])
        self.assertTrue(view['preview']['pending'])
        self.assertEqual(view['preview']['phase'], 'starting')

    def test_stop_can_end_verified_pending_preview(self):
        identity = '501 Sun Oct 25 2026 ' + str(self.skill / 'scripts' / 'notify.py')
        self.write_owned(pid=4321, identity=identity, voice='serena')
        with patch.object(local_voice, '_process_start',
                          side_effect=['start-token', '', '']), \
                patch.object(local_voice, '_identity', return_value=identity), \
                patch.object(local_voice.os, 'kill') as kill:
            self.assertEqual(local_voice.stop(), {'status': 'stop_requested'})
        kill.assert_called_once_with(4321, signal.SIGTERM)
        self.assertFalse((self.config / 'preview.json').exists())

    def test_busy_child_status_overrides_older_played_last_run(self):
        stub = self.skill / 'scripts' / 'notify.py'
        stub.write_text("import json, sys\n"
                        "print(json.dumps({'status': 'busy'}), flush=True)\n"
                        "sys.exit(3)\n")
        (self.runtime / 'last-run.json').write_text(json.dumps({
            'status': 'played', 'voice': 'Vivian', 'audio_seconds': 4.24,
            'total_seconds': 9.5, 'finished_at': 100.0}))
        with patch.object(local_voice, 'VENV_PYTHON', Path(sys.executable)):
            result = local_voice.preview({'backend': 'local', 'voice': 'vivian'})
            self.assertEqual(result['status'], 'started')
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and (self.config / 'preview.json').exists():
                time.sleep(0.02)
            self.assertFalse((self.config / 'preview.json').exists())
            result_deadline = time.monotonic() + 3
            while time.monotonic() < result_deadline and not (self.config / 'preview-result.json').exists():
                time.sleep(0.02)
        recorded = json.loads((self.config / 'preview-result.json').read_text())
        self.assertEqual(recorded['status'], 'busy')
        with patch.object(local_voice, '_probe', return_value={'device': 'Mac mini扬声器'}):
            view = local_voice.view()
        self.assertEqual(view['last']['status'], 'busy')
        self.assertGreater(view['last']['finished_at'], 100.0)
        self.assertNotIn('played', json.dumps(view['last']))

    def test_stop_only_targets_console_owned_preview(self):
        with patch.object(local_voice.subprocess, 'run') as run:
            self.assertEqual(local_voice.stop(), {'status': 'idle'})
            run.assert_not_called()
        identity = '501 Sun Oct 25 2026 ' + str(self.skill / 'scripts' / 'notify.py')
        self.write_active(pid=777, identity=identity)
        with patch.object(local_voice, '_identity', return_value=identity), \
                patch.object(local_voice.subprocess, 'run') as run:
            self.assertEqual(local_voice.stop(), {'status': 'idle'})
            run.assert_not_called()
        self.assertTrue((self.runtime / 'active.json').exists())

        self.write_active(pid=4321, identity=identity)
        self.write_owned(pid=4321, identity=identity, voice='serena')
        with patch.object(local_voice, '_identity', side_effect=[identity, identity]), \
                patch.object(local_voice, '_process_start',
                             side_effect=['start-token', '', '']), \
                patch.object(local_voice.os, 'kill') as kill, \
                patch.object(local_voice.subprocess, 'run') as run:
            self.assertEqual(local_voice.stop(), {'status': 'stop_requested'})
        kill.assert_called_once_with(4321, signal.SIGTERM)
        run.assert_not_called()
        self.assertFalse((self.config / 'preview.json').exists())

    def test_stop_revalidates_and_never_signals_a_new_foreign_playback(self):
        identity = '501 Sun Oct 25 2026 ' + str(self.skill / 'scripts' / 'notify.py')
        other = '501 Sun Oct 25 2026 ' + str(self.skill / 'scripts' / 'notify.py') + ' --other'
        self.write_active(pid=4321, identity=identity)
        self.write_owned(pid=4321, identity=identity, voice='serena')
        with patch.object(local_voice, '_identity', side_effect=[identity, other]), \
                patch.object(local_voice, '_process_start', return_value='start-token'), \
                patch.object(local_voice.os, 'kill') as kill:
            self.assertEqual(local_voice.stop(), {'status': 'idle'})
        kill.assert_not_called()
        self.assertFalse((self.config / 'preview.json').exists())

    def test_real_preview_subprocess_keeps_ownership_until_reaped(self):
        stub = self.skill / 'scripts' / 'notify.py'
        stub.write_text(
            "import json, os, subprocess, time\n"
            "from pathlib import Path\n"
            "runtime = Path(os.environ['STUB_RUNTIME'])\n"
            "pid = os.getpid()\n"
            "identity = subprocess.run(['/bin/ps', '-p', str(pid), '-o', 'uid=,lstart=,args='],\n"
            "                         capture_output=True, text=True).stdout.strip()\n"
            "state = {'pid': pid, 'identity': identity, 'phase': 'synthesis', 'started_at': time.time()}\n"
            "(runtime / 'active.json').write_text(json.dumps(state))\n"
            "time.sleep(0.8)\n"
            "(runtime / 'active.json').unlink(missing_ok=True)\n")
        with patch.object(local_voice, 'VENV_PYTHON', Path(sys.executable)), \
                patch.dict(os.environ, {'STUB_RUNTIME': str(self.runtime)}):
            result = local_voice.preview({'backend': 'local', 'voice': 'vivian'})
            self.assertEqual(result['status'], 'started')
            again = local_voice.preview({'backend': 'local', 'voice': 'serena'})
            self.assertEqual(again['status'], 'already_running',
                             'a second request must not overwrite ownership during startup')
            self.assertEqual(again['voice'], 'vivian')
            registered = time.monotonic() + 2
            while time.monotonic() < registered and local_voice.owned_preview() is None:
                time.sleep(0.02)
            self.assertIsNotNone(local_voice.owned_preview(),
                                 'ownership must survive until the child actually exits')
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and (self.config / 'preview.json').exists():
                time.sleep(0.05)
            self.assertFalse((self.config / 'preview.json').exists(),
                             'the reaper must drain and clear its own record')
            self.assertEqual(local_voice._identity(result['pid']), '',
                             'the child must be reaped, not left running')

    def test_speaker_probe_is_cached_and_never_trusts_clock_origin(self):
        result = MagicMock(returncode=0,
                           stdout='{"status": "ready", "device": "Mac mini扬声器"}\n',
                           stderr='')
        local_voice._PROBE.update({'at': None, 'value': None})
        with patch.object(local_voice.subprocess, 'run', return_value=result) as run:
            self.assertEqual(local_voice._probe(),
                             {'device': 'Mac mini扬声器', 'status': 'ready'})
            self.assertEqual(local_voice._probe(),
                             {'device': 'Mac mini扬声器', 'status': 'ready'})
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args[0][0][1:], [str(self.skill / 'scripts' / 'notify.py'),
                                                    'status'])

    def test_view_last_run_reports_effective_backend_rate_and_fallback(self):
        self.write_settings({'backend': 'auto', 'rate': 1.1, 'voice': 'vivian'})
        (self.runtime / 'last-run.json').write_text(json.dumps({
            'status': 'failed', 'voice': 'vivian', 'backend': 'local',
            'requested_backend': 'auto', 'rate': 1.1, 'cloud_voice': CLOUD_VOICE,
            'fallback': True, 'fallback_reason': 'timeout',
            'fallback_detail': '云端语音响应超时。',
            'cloud_status': {'model': 'seed-tts-2.0', 'reason': 'timeout',
                             'request_id': 'req-123'},
            'error': str(self.root) + '/private failure',
            'audio_seconds': 4.2, 'total_seconds': 9.5, 'finished_at': 1.0}))
        with patch.object(local_voice, '_probe', return_value={'device': 'Mac mini扬声器'}):
            result = local_voice.view()
        last = result['last']
        self.assertEqual(last['status'], 'failed')
        self.assertEqual(last['backend'], 'local')
        self.assertEqual(last['requested_backend'], 'auto')
        self.assertEqual(last['rate'], 1.1)
        self.assertTrue(last['fallback'])
        self.assertEqual(last['fallback_reason'], 'timeout')
        self.assertEqual(last['fallback_detail'], '云端语音响应超时。')
        self.assertEqual(last['cloud_status']['reason'], 'timeout')
        self.assertEqual(last['cloud_status']['request_id'], 'req-123')
        self.assertEqual(last['error'], '~/private failure')
        dumped = json.dumps(result, ensure_ascii=False)
        self.assertNotIn(str(self.root), dumped)
        self.assertNotIn(FAKE_PLAN_KEY, dumped)

        (self.runtime / 'model' / 'config.json').unlink()
        with patch.object(local_voice, '_probe', return_value=None):
            missing = local_voice.view()
        self.assertFalse(missing['available'])
        self.assertEqual(missing['reason'], 'model_missing')
        self.assertEqual(missing['voices'], [])

    def test_reap_records_cloud_fallback_result(self):
        stub = self.skill / 'scripts' / 'notify.py'
        stub.write_text(
            "import json, sys\n"
            "print(json.dumps({'status':'played','voice':'vivian','backend':'local',\n"
            "  'requested_backend':'auto','rate':1.1,\n"
            "  'cloud_voice':'" + CLOUD_VOICE + "','fallback':True,\n"
            "  'fallback_reason':'timeout','fallback_detail':'云端语音响应超时。',\n"
            "  'cloud_status':{'model':'seed-tts-2.0','reason':'timeout','request_id':'req-123'},\n"
            "  'audio_seconds':2.5,'total_seconds':4.2}), flush=True)\n")
        with patch.object(local_voice, 'VENV_PYTHON', Path(sys.executable)):
            result = local_voice.preview({'backend': 'local', 'voice': 'vivian'})
            self.assertEqual(result['status'], 'started')
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and not (self.config / 'preview-result.json').exists():
                time.sleep(0.02)
        recorded = json.loads((self.config / 'preview-result.json').read_text())
        self.assertEqual(recorded['status'], 'played')
        self.assertEqual(recorded['backend'], 'local')
        self.assertEqual(recorded['requested_backend'], 'auto')
        self.assertEqual(recorded['rate'], 1.1)
        self.assertTrue(recorded['fallback'])
        self.assertEqual(recorded['fallback_reason'], 'timeout')
        self.assertEqual(recorded['fallback_detail'], '云端语音响应超时。')
        self.assertEqual(recorded['cloud_status']['request_id'], 'req-123')
        self.assertEqual(recorded['audio_seconds'], 2.5)

    def test_cloud_catalog_matches_reference_script(self):
        module = load_notify(self.root / 'reference-home')
        expected = [(voice['id'], voice['label']) for voice in local_voice.CLOUD_VOICES]
        self.assertEqual(list(module.CLOUD_VOICES), expected)


class NotifyReferenceDefaultTests(unittest.TestCase):
    """The portable reference notify.py shares settings and honors overrides."""

    def write_home(self, home, settings=None, model=True):
        if model:
            model_dir = home / '.local/share/qwen-voice-notify/model'
            model_dir.mkdir(parents=True)
            (model_dir / 'config.json').write_text(
                json.dumps({'talker_config': {'spk_id': VOICES}}))
            for name in ('model.safetensors', 'tokenizer_config.json',
                         'speech_tokenizer/config.json',
                         'speech_tokenizer/model.safetensors'):
                (model_dir / name).parent.mkdir(parents=True, exist_ok=True)
                (model_dir / name).write_text('{}' if name.endswith('.json') else '')
        if settings is not None:
            path = home / '.config/qwen-voice-notify/settings.json'
            path.parent.mkdir(parents=True)
            path.write_text(settings if isinstance(settings, str) else json.dumps(settings))

    def write_auth(self, home):
        path = home / '.local/share/opencode/auth.json'
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({
            'volcengine-agent-plan': {'type': 'api', 'key': FAKE_PLAN_KEY},
        }))

    def test_saved_default_falls_back_to_vivian_and_stays_validated(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            self.write_home(home)
            module = load_notify(home)
            self.assertEqual(module.saved_default_voice(), 'Vivian')
            settings = home / '.config/qwen-voice-notify/settings.json'
            settings.parent.mkdir(parents=True)
            settings.write_text(json.dumps({'voice': 'serena'}))
            self.assertEqual(module.saved_default_voice(), 'serena')
            settings.write_text(json.dumps({'voice': 'Serena'}))
            self.assertEqual(module.saved_default_voice(), 'serena')
            settings.write_text(json.dumps({'voice': 'no_such_voice'}))
            self.assertEqual(module.saved_default_voice(), 'Vivian')
            settings.write_text(json.dumps({'voice': '../../bin/sh'}))
            self.assertEqual(module.saved_default_voice(), 'Vivian')
            settings.write_text('{broken')
            self.assertEqual(module.saved_default_voice(), 'Vivian')
            settings.write_text(json.dumps({'voice': 'serena'}) + ' ' * 5000)
            self.assertEqual(module.saved_default_voice(), 'Vivian')
            self.assertNotIn('mlx', sys.modules)
            self.assertNotIn('mlx_audio', sys.modules)

    def test_saved_default_requires_the_installed_catalog(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            self.write_home(home, settings={'voice': 'serena'}, model=False)
            module = load_notify(home)
            self.assertEqual(module.saved_default_voice(), 'Vivian')

    def test_main_uses_saved_default_unless_voice_is_explicit(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            self.write_home(home, settings={'voice': 'dylan'})
            module = load_notify(home)
            seen = {}

            def fake_speak(args):
                seen.update({'voice': args.voice, 'backend': args.backend,
                             'rate': args.rate, 'cloud_voice': args.cloud_voice})
                return 0

            with patch.object(module, 'speak', fake_speak):
                with patch.object(sys, 'argv', ['notify.py', 'speak']):
                    self.assertEqual(module.main(), 0)
                self.assertEqual(seen['voice'], 'dylan')
                self.assertEqual(seen['backend'], 'auto')
                self.assertEqual(seen['rate'], 1.1)
                self.assertEqual(seen['cloud_voice'], module.DEFAULT_CLOUD_VOICE)
                with patch.object(sys, 'argv', ['notify.py', 'speak', '--voice', 'Serena',
                                                '--rate', '1.3', '--backend', 'local']):
                    self.assertEqual(module.main(), 0)
                self.assertEqual(seen['voice'], 'serena')
                self.assertEqual(seen['backend'], 'local')
                self.assertEqual(seen['rate'], 1.3)
                with patch.object(sys, 'argv', ['notify.py', 'speak',
                                                '--cloud-voice', CLOUD_VOICE_ALT]):
                    self.assertEqual(module.main(), 0)
                self.assertEqual(seen['cloud_voice'], CLOUD_VOICE_ALT)

    def test_cli_rejects_out_of_range_rate_and_unknown_backend(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            self.write_home(home)
            module = load_notify(home)
            with patch.object(sys, 'argv', ['notify.py', 'speak', '--rate', '2.5']), \
                    contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    module.main()
            with patch.object(sys, 'argv', ['notify.py', 'speak', '--backend', 'speech']), \
                    contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    module.main()
            with patch.object(sys, 'argv', ['notify.py', 'speak', '--voice', 'no_such_voice']):
                with contextlib.redirect_stdout(io.StringIO()) as output, \
                        patch.object(module, 'speak') as speak:
                    self.assertEqual(module.run(), 1)
                speak.assert_not_called()
                self.assertEqual(json.loads(output.getvalue().strip())['status'], 'error')

    def test_resolve_settings_allows_cloud_without_local_catalog(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            self.write_home(home, model=False)
            module = load_notify(home)
            for backend in ('auto', 'cloud'):
                args = argparse.Namespace(backend=backend, rate=1.1, voice='Vivian',
                                          cloud_voice=None)
                resolved = module.resolve_settings(args)
                self.assertEqual(resolved.voice, 'vivian')
                self.assertEqual(resolved.backend, backend)
            args = argparse.Namespace(backend='local', rate=1.1, voice='vivian',
                                      cloud_voice=None)
            with self.assertRaisesRegex(ValueError, '本机离线语音不可用'):
                module.resolve_settings(args)

    def test_preferences_defaults_and_invalid_new_fields(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            self.write_home(home, settings={'schema': 1, 'voice': 'serena'})
            module = load_notify(home)
            preferences, warnings, meta = module.load_preferences()
            self.assertEqual(preferences['voice'], 'serena')
            self.assertEqual(preferences['backend'], 'auto')
            self.assertEqual(preferences['rate'], 1.1)
            self.assertEqual(preferences['cloud_voice'], CLOUD_VOICE)
            self.assertEqual(warnings, [])
            self.assertEqual(meta, {'settings': 'saved', 'voice': 'saved'})

            settings = home / '.config/qwen-voice-notify/settings.json'
            settings.write_text(json.dumps({
                'voice': 'serena', 'cloud_voice': 'no_such_voice',
                'backend': 'speech', 'rate': 8}))
            preferences, warnings, _meta = module.load_preferences()
            self.assertEqual(preferences['cloud_voice'], CLOUD_VOICE)
            self.assertEqual(preferences['backend'], 'auto')
            self.assertEqual(preferences['rate'], 1.1)
            self.assertIn('invalid_saved_cloud_voice', warnings)
            self.assertIn('invalid_saved_backend', warnings)
            self.assertIn('invalid_saved_rate', warnings)

    def test_status_payload_reports_cloud_metadata_without_synthesis(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            self.write_home(home, settings={'backend': 'cloud', 'rate': 1.2,
                                            'cloud_voice': CLOUD_VOICE_ALT})
            self.write_auth(home)
            module = load_notify(home)
            with patch.object(module, 'speaker_device',
                              return_value=(0, {'name': 'Mac mini扬声器'})), \
                    patch.object(module, 'cloud_speech',
                                 side_effect=AssertionError('status must not synthesize')):
                payload = module.status_payload()
            self.assertEqual(payload['status'], 'ready')
            self.assertEqual(payload['backend'], 'cloud')
            self.assertEqual(payload['rate'], 1.2)
            self.assertEqual(payload['cloud_voice'], CLOUD_VOICE_ALT)
            self.assertTrue(payload['cloud_key_configured'])
            self.assertEqual(payload['cloud_model'], 'seed-tts-2.0')
            self.assertEqual(payload['device'], 'Mac mini扬声器')
            self.assertNotIn(FAKE_PLAN_KEY, json.dumps(payload, ensure_ascii=False))


class FakeSock:
    def __init__(self, event=None):
        self.timeout = None
        self.event = event
        self.shutdown_called = False

    def settimeout(self, value):
        self.timeout = value

    def shutdown(self, how):
        self.shutdown_called = True
        if self.event is not None:
            self.event.set()


class FakeResponse:
    def __init__(self, lines, status=200):
        self.lines = list(lines)
        self.status = status
        self.read_called = 0

    def readline(self, limit=-1):
        if not self.lines:
            return b''
        return self.lines.pop(0)

    def read(self, limit=-1):
        self.read_called += 1
        return b'{"error":"upstream"}'

    def close(self):
        pass


class BlockingResponse(FakeResponse):
    def __init__(self, event, status=200):
        super().__init__([], status=status)
        self.event = event

    def readline(self, limit=-1):
        self.event.wait(5)
        return b''


class FakeConnection:
    def __init__(self, response, sock=None):
        self.response = response
        self.sock = sock or FakeSock()
        self.path = None
        self.payload = None
        self.headers = None
        self.closed = False
        self.fail = None

    def request(self, method, path, body=None, headers=None):
        if self.fail is not None:
            raise self.fail
        self.path = path
        self.headers = headers
        self.payload = json.loads(body)

    def getresponse(self):
        return self.response

    def close(self):
        self.closed = True


class NotifyCloudTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.module = load_notify(self.home)
        self.tmp_wav = self.home / 'out.wav'
        self.tmp_mp3 = self.home / 'out.mp3'

    def tearDown(self):
        self.tmp.cleanup()

    def cloud_lines(self, audio=b'fake-mp3-bytes', terminal=True):
        lines = [json.dumps({'code': 0,
                             'data': __import__('base64').b64encode(audio).decode()}).encode() + b'\n']
        if terminal:
            lines.append(json.dumps({'code': 20000000, 'message': 'ok'}).encode() + b'\n')
        return lines

    def test_payload_nests_speech_rate_inside_audio_params(self):
        connection = FakeConnection(FakeResponse(self.cloud_lines()))
        audio, meta = self.module.cloud_speech(
            '您好，这是测试。', CLOUD_VOICE, 1.1, key=FAKE_PLAN_KEY,
            connection_factory=lambda *args, **kwargs: connection)
        self.assertEqual(audio, b'fake-mp3-bytes')
        self.assertEqual(connection.path, '/api/v3/plan/tts/unidirectional')
        self.assertEqual(connection.headers['X-Api-Key'], FAKE_PLAN_KEY)
        self.assertEqual(connection.headers['X-Api-Resource-Id'], 'seed-tts-2.0')
        self.assertTrue(connection.headers.get('X-Api-Request-Id'))
        self.assertNotIn('X-Api-App-Id', connection.headers)
        self.assertNotIn('X-Api-Access-Key', connection.headers)
        req = connection.payload['req_params']
        self.assertEqual(req['text'], '您好，这是测试。')
        self.assertEqual(req['speaker'], CLOUD_VOICE)
        self.assertEqual(req['audio_params']['format'], 'mp3')
        self.assertEqual(req['audio_params']['sample_rate'], 24000)
        self.assertEqual(req['audio_params']['speech_rate'], 10)
        self.assertNotIn('speech_rate', req)
        self.assertEqual(meta['model'], 'seed-tts-2.0')
        self.assertEqual(meta['code'], 20000000)
        self.assertNotIn(FAKE_PLAN_KEY, json.dumps(meta))
        self.assertNotIn('fake-mp3-bytes', json.dumps(meta))

    def test_rate_mapping_uses_native_range(self):
        for rate, expected in ((0.5, -50), (1.0, 0), (1.1, 10), (1.5, 50), (2.0, 100)):
            connection = FakeConnection(FakeResponse(self.cloud_lines()))
            self.module.cloud_speech('测试。', CLOUD_VOICE, rate, key=FAKE_PLAN_KEY,
                                     connection_factory=lambda *args, **kwargs: connection)
            self.assertEqual(connection.payload['req_params']['audio_params']['speech_rate'],
                             expected)

    def test_cloud_stream_accepts_chunks_and_terminal(self):
        connection = FakeConnection(FakeResponse(self.cloud_lines(b'abc')))
        audio, meta = self.module.cloud_speech(
            '测试。', CLOUD_VOICE, 1.1, key=FAKE_PLAN_KEY,
            connection_factory=lambda *args, **kwargs: connection)
        self.assertEqual(audio, b'abc')
        self.assertEqual(meta['frames'], 2)
        self.assertTrue(connection.closed)

    def test_cloud_stream_rejects_positive_error(self):
        for code in (45000001, 50000000):
            connection = FakeConnection(FakeResponse([
                json.dumps({'code': code, 'message': 'quota exhausted'}).encode() + b'\n']))
            with self.assertRaises(self.module.CloudError) as caught:
                self.module.cloud_speech('测试。', CLOUD_VOICE, 1.1, key=FAKE_PLAN_KEY,
                                         connection_factory=lambda *args, **kwargs: connection)
            self.assertEqual(caught.exception.reason, 'quota')
            self.assertNotIn(FAKE_PLAN_KEY, str(caught.exception))

    def test_cloud_stream_rejects_truncated_malformed_empty_oversized(self):
        cases = (
            ('truncated', self.cloud_lines(terminal=False), 'truncated'),
            ('malformed', [b'not-json\n'], 'protocol'),
            ('non_object', [b'[]\n'], 'protocol'),
            ('empty', [json.dumps({'code': 20000000}).encode() + b'\n'], 'empty'),
            ('bad_base64', [json.dumps({'code': 0, 'data': '***'}).encode() + b'\n'], 'protocol'),
            ('missing_code', [json.dumps({'data': 'YWJj'}).encode() + b'\n'], 'protocol'),
        )
        for name, lines, reason in cases:
            with self.subTest(name=name):
                connection = FakeConnection(FakeResponse(lines))
                with self.assertRaises(self.module.CloudError) as caught:
                    self.module.cloud_speech('测试。', CLOUD_VOICE, 1.1, key=FAKE_PLAN_KEY,
                                             connection_factory=lambda *args, **kwargs: connection)
                self.assertEqual(caught.exception.reason, reason)

    def test_cloud_stream_rejects_oversized_audio_and_line(self):
        connection = FakeConnection(FakeResponse(self.cloud_lines(b'12345678')))
        old = self.module.CLOUD_MAX_BYTES
        self.module.CLOUD_MAX_BYTES = 4
        try:
            with self.assertRaises(self.module.CloudError) as caught:
                self.module.cloud_speech('测试。', CLOUD_VOICE, 1.1, key=FAKE_PLAN_KEY,
                                         connection_factory=lambda *args, **kwargs: connection)
            self.assertEqual(caught.exception.reason, 'oversized')
        finally:
            self.module.CLOUD_MAX_BYTES = old

        connection = FakeConnection(FakeResponse([b'x' * 20]))
        old = self.module.CLOUD_MAX_LINE
        self.module.CLOUD_MAX_LINE = 8
        try:
            with self.assertRaises(self.module.CloudError) as caught:
                self.module.cloud_speech('测试。', CLOUD_VOICE, 1.1, key=FAKE_PLAN_KEY,
                                         connection_factory=lambda *args, **kwargs: connection)
            self.assertEqual(caught.exception.reason, 'oversized')
        finally:
            self.module.CLOUD_MAX_LINE = old

    def test_cloud_http_auth_quota_and_provider_errors(self):
        for status, reason in ((401, 'auth'), (403, 'auth'), (429, 'quota'),
                               (402, 'quota'), (503, 'provider'), (400, 'provider')):
            with self.subTest(status=status):
                connection = FakeConnection(FakeResponse([], status=status))
                with self.assertRaises(self.module.CloudError) as caught:
                    self.module.cloud_speech('测试。', CLOUD_VOICE, 1.1, key=FAKE_PLAN_KEY,
                                             connection_factory=lambda *args, **kwargs: connection)
                self.assertEqual(caught.exception.reason, reason)
                self.assertNotIn(FAKE_PLAN_KEY, str(caught.exception))

    def test_cloud_network_and_socket_timeout(self):
        for error, reason in ((__import__('socket').timeout('slow'), 'timeout'),
                              (OSError('down'), 'network'),
                              (http.client.RemoteDisconnected('closed'), 'network')):
            with self.subTest(error=type(error).__name__):
                connection = FakeConnection(FakeResponse([]))
                connection.fail = error
                with self.assertRaises(self.module.CloudError) as caught:
                    self.module.cloud_speech('测试。', CLOUD_VOICE, 1.1, key=FAKE_PLAN_KEY,
                                             connection_factory=lambda *args, **kwargs: connection)
                self.assertEqual(caught.exception.reason, reason)

    def test_cloud_hard_deadline_unblocks_a_slow_read(self):
        event = threading.Event()
        connection = FakeConnection(BlockingResponse(event), sock=FakeSock(event))
        old = self.module.CLOUD_TOTAL_TIMEOUT
        self.module.CLOUD_TOTAL_TIMEOUT = 0.2
        started = time.monotonic()
        try:
            with self.assertRaises(self.module.CloudError) as caught:
                self.module.cloud_speech('测试。', CLOUD_VOICE, 1.1, key=FAKE_PLAN_KEY,
                                         connection_factory=lambda *args, **kwargs: connection)
            elapsed = time.monotonic() - started
        finally:
            self.module.CLOUD_TOTAL_TIMEOUT = old
        self.assertEqual(caught.exception.reason, 'timeout')
        self.assertLess(elapsed, 1.5)
        self.assertTrue(event.is_set(), 'the watchdog must shut the socket down')

    def test_cloud_cancellation_propagates_without_retry(self):
        module = self.module
        class CancellingResponse(FakeResponse):
            def readline(self, limit=-1):
                raise module.SpeechCancelled('播报已停止')

        connection = FakeConnection(CancellingResponse([]))
        with self.assertRaises(self.module.SpeechCancelled):
            self.module.cloud_speech('测试。', CLOUD_VOICE, 1.1, key=FAKE_PLAN_KEY,
                                     connection_factory=lambda *args, **kwargs: connection)

    def test_cloud_missing_key_is_actionable_and_does_not_leak(self):
        with patch.object(self.module, 'provider_auth_path',
                          return_value=self.home / 'missing-auth.json'):
            with self.assertRaises(self.module.CloudError) as caught:
                self.module.cloud_speech('测试。', CLOUD_VOICE, 1.1)
        self.assertEqual(caught.exception.reason, 'missing_key')
        self.assertIn('Agent Plan', str(caught.exception))
        self.assertNotIn(FAKE_PLAN_KEY, str(caught.exception))

    def test_cloud_invalid_voice_and_rate_rejected_before_network(self):
        with patch.object(self.module, 'connection_factory', create=True) as unused:
            with self.assertRaises(ValueError):
                self.module.cloud_speech('测试。', 'no_such_voice', 1.1, key=FAKE_PLAN_KEY)
            with self.assertRaises(ValueError):
                self.module.cloud_speech('测试。', CLOUD_VOICE, 2.5, key=FAKE_PLAN_KEY)
            with self.assertRaises(ValueError):
                self.module.cloud_speech('', CLOUD_VOICE, 1.1, key=FAKE_PLAN_KEY)
            unused.assert_not_called()


class NotifyFfmpegTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.module = load_notify(self.home)
        self.source = write_wav(self.home / 'tone.wav', seconds=1.0, freq=240.0)

    def tearDown(self):
        self.tmp.cleanup()

    @unittest.skipUnless(Path('/opt/homebrew/bin/ffmpeg').is_file(), 'ffmpeg unavailable')
    def test_atempo_preserves_pitch_and_shortens_duration(self):
        dest = self.home / 'fast.wav'
        self.module.run_ffmpeg(self.module.ffmpeg_atempo_command(self.source, dest, 1.5))
        rate, _channels, samples = read_wav(dest)
        self.assertEqual(rate, 24000)
        duration = len(samples) / float(rate)
        self.assertAlmostEqual(duration, 1.0 / 1.5, delta=0.04)
        self.assertAlmostEqual(wav_frequency(dest), 240.0, delta=12.0)

        slow = self.home / 'slow.wav'
        self.module.run_ffmpeg(self.module.ffmpeg_atempo_command(self.source, slow, 0.75))
        _rate, _channels, samples = read_wav(slow)
        duration = len(samples) / float(_rate)
        self.assertAlmostEqual(duration, 1.0 / 0.75, delta=0.05)
        self.assertAlmostEqual(wav_frequency(slow), 240.0, delta=12.0)

    def test_one_x_is_rejected_as_unnecessary(self):
        with self.assertRaises(ValueError):
            self.module.ffmpeg_atempo_command(self.source, self.home / 'same.wav', 1.0)

    @unittest.skipUnless(Path('/opt/homebrew/bin/ffmpeg').is_file(), 'ffmpeg unavailable')
    def test_mp3_to_wav_decode_preserves_duration(self):
        mp3 = self.home / 'tone.mp3'
        self.module.run_ffmpeg([str(self.module.FFMPEG), '-nostdin', '-hide_banner',
                                '-loglevel', 'error', '-y', '-i', str(self.source),
                                '-codec:a', 'libmp3lame', '-q:a', '4', str(mp3)])
        decoded = self.home / 'decoded.wav'
        self.module.run_ffmpeg(self.module.ffmpeg_to_wav_command(mp3, decoded))
        rate, _channels, samples = read_wav(decoded)
        self.assertEqual(rate, 24000)
        self.assertAlmostEqual(len(samples) / float(rate), 1.0, delta=0.1)


class NotifyRoutingTests(unittest.TestCase):
    """speak() routing: cloud success, automatic fallback and no-fallback edges."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.module = load_notify(self.home)
        self.root = self.home / '.local/share/qwen-voice-notify'
        self.root.mkdir(parents=True)
        model = self.root / 'model'
        model.mkdir()
        for name in self.module.MODEL_FILES:
            (model / name).parent.mkdir(parents=True, exist_ok=True)
            (model / name).write_text('{}' if name.endswith('.json') else '')
        (model / 'config.json').write_text(json.dumps({'talker_config': {'spk_id': VOICES}}))
        self.settings = self.home / '.config/qwen-voice-notify/settings.json'
        self.calls = []
        self.cloud_calls = []
        self.ffmpeg_error = False
        self.play_error = False
        self.synth_error = False
        self.patchers = [
            patch.object(self.module, 'speaker_device',
                         return_value=(0, {'name': 'Mac mini扬声器'})),
            patch.object(self.module, 'identity', return_value='fake identity'),
            patch.object(self.module, 'audio_seconds', return_value=1.0),
            patch.object(self.module, 'local_ready', return_value=True),
            patch.object(self.module, 'run_stage', side_effect=self.fake_run_stage),
        ]
        for patcher in self.patchers:
            patcher.start()

    def tearDown(self):
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.tmp.cleanup()

    def fake_run_stage(self, command, state, stdin_text, timeout, env, phase, old_handlers, label):
        self.calls.append((phase, label, list(command)))
        if label == '_synth':
            if self.synth_error:
                raise self.module.SpeechCancelled('播报已停止')
            wav = Path(command[command.index('--wav') + 1])
            write_wav(wav)
            metrics = Path(command[command.index('--metrics') + 1])
            metrics.write_text(json.dumps({'audio_seconds': 0.2, 'load_seconds': 0.1,
                                           'synthesis_seconds': 0.2}))
        elif label == 'ffmpeg':
            if self.ffmpeg_error:
                raise RuntimeError('ffmpeg failed')
            source = Path(command[command.index('-i') + 1])
            dest = Path(command[-1])
            if source.exists():
                dest.write_bytes(source.read_bytes())
            else:
                write_wav(dest)
        elif label == '_play':
            if self.play_error:
                raise RuntimeError('_play: speaker failed')
        return '', ''

    def fake_cloud(self, audio=b'fake-mp3-bytes', meta=None, error=None):
        def run(text, speaker, rate):
            self.cloud_calls.append({'text': text, 'speaker': speaker, 'rate': rate})
            if error is not None:
                raise error
            return audio, meta or {'model': 'seed-tts-2.0', 'code': 20000000,
                                   'request_id': 'req-123'}
        return run

    def run_speak(self, backend='auto', rate=1.1, cloud_speech=None,
                  text='测试播报。'):
        args = argparse.Namespace(backend=backend, rate=rate, voice='vivian',
                                  cloud_voice=CLOUD_VOICE, language='Chinese',
                                  timeout=5, settings_warnings=[])
        if cloud_speech is None:
            cloud_speech = self.fake_cloud()
        with patch.object(self.module, 'cloud_speech', side_effect=cloud_speech), \
                patch.object(sys, 'stdin', io.StringIO(text)), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            code = self.module.speak(args)
        lines = output.getvalue().strip().splitlines()
        report = json.loads(lines[-1]) if lines else {}
        return code, report

    def last_report(self):
        return json.loads((self.root / 'last-run.json').read_text())

    def labels(self):
        return [label for _phase, label, _command in self.calls]

    def test_auto_falls_back_to_local_after_cloud_timeout(self):
        code, report = self.run_speak(
            cloud_speech=self.fake_cloud(error=self.module.CloudError(
                'timeout', '云端语音响应超时。')))
        self.assertEqual(code, 0)
        self.assertEqual(report['status'], 'played')
        self.assertEqual(report['requested_backend'], 'auto')
        self.assertEqual(report['backend'], 'local')
        self.assertTrue(report['fallback'])
        self.assertEqual(report['fallback_reason'], 'timeout')
        self.assertEqual(report['fallback_detail'], '云端语音响应超时。')
        self.assertEqual(report['voice'], 'vivian')
        self.assertEqual(report['cloud_voice'], CLOUD_VOICE)
        self.assertEqual(report['rate'], 1.1)
        self.assertEqual(report['audio_seconds'], 1.0)
        self.assertIn('_synth', self.labels())
        tempo = [command for _phase, label, command in self.calls
                 if label == 'ffmpeg' and any('atempo=' in item for item in command)]
        self.assertEqual(len(tempo), 1)
        self.assertEqual(self.labels()[-1], '_play')
        self.assertFalse((self.root / 'active.json').exists())
        self.assertEqual(self.last_report()['backend'], 'local')
        self.assertTrue(self.last_report()['fallback'])

    def test_cloud_success_never_double_applies_rate(self):
        code, report = self.run_speak(rate=1.25, cloud_speech=self.fake_cloud())
        self.assertEqual(code, 0)
        self.assertEqual(report['backend'], 'cloud')
        self.assertEqual(report['voice'], CLOUD_VOICE)
        self.assertFalse(report['fallback'])
        self.assertEqual(report['cloud_status']['code'], 20000000)
        self.assertEqual(report['cloud_status']['request_id'], 'req-123')
        self.assertEqual(report['audio_seconds'], 1.0)
        self.assertEqual(self.cloud_calls[0]['rate'], 1.25)
        self.assertNotIn('_synth', self.labels())
        for phase, label, command in self.calls:
            if label == 'ffmpeg':
                self.assertFalse(any('atempo=' in item for item in command),
                                 'cloud native speech_rate must not be applied twice')
        self.assertEqual(self.labels()[-1], '_play')

    def test_cloud_success_without_local_model_after_resolve(self):
        (self.root / 'model' / 'config.json').unlink()
        args = argparse.Namespace(backend='auto', rate=1.1, voice='Vivian',
                                  cloud_voice=None, language='Chinese', timeout=5,
                                  settings_warnings=[])
        with patch.object(self.module, 'local_ready', return_value=False), \
                patch.object(self.module, 'cloud_speech', side_effect=self.fake_cloud()), \
                patch.object(sys, 'stdin', io.StringIO('测试播报。')), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            resolved = self.module.resolve_settings(args)
            code = self.module.speak(resolved)
        report = json.loads(output.getvalue().strip().splitlines()[-1])
        self.assertEqual(code, 0)
        self.assertEqual(report['backend'], 'cloud')
        self.assertFalse(report['fallback'])
        self.assertNotIn('_synth', self.labels())

    def test_explicit_cloud_failure_does_not_fall_back(self):
        code, report = self.run_speak(
            backend='cloud',
            cloud_speech=self.fake_cloud(error=self.module.CloudError(
                'auth', '云端语音鉴权失败；请检查 Agent Plan 密钥或套餐权限。')))
        self.assertEqual(code, 1)
        self.assertEqual(report['status'], 'failed')
        self.assertEqual(report['backend'], 'cloud')
        self.assertFalse(report['fallback'])
        self.assertIn('云端语音不可用', report['error'])
        self.assertNotIn('_synth', self.labels())

    def test_playback_failure_after_cloud_does_not_fall_back(self):
        self.play_error = True
        code, report = self.run_speak(cloud_speech=self.fake_cloud())
        self.assertEqual(code, 1)
        self.assertEqual(report['status'], 'failed')
        self.assertFalse(report['fallback'])
        self.assertNotIn('_synth', self.labels())
        self.assertIn('_play', report['error'])

    def test_cancellation_during_cloud_does_not_fall_back(self):
        code, report = self.run_speak(
            cloud_speech=self.fake_cloud(error=self.module.SpeechCancelled('播报已停止')))
        self.assertEqual(code, 1)
        self.assertEqual(report['status'], 'failed')
        self.assertFalse(report['fallback'])
        self.assertEqual(report['error'], '播报已停止')
        self.assertNotIn('_synth', self.labels())

    def test_cancellation_during_local_synth_does_not_fall_back(self):
        self.synth_error = True
        code, report = self.run_speak(backend='local')
        self.assertEqual(code, 1)
        self.assertEqual(report['status'], 'failed')
        self.assertFalse(report['fallback'])
        self.assertEqual(report['error'], '播报已停止')
        self.assertNotIn('_play', self.labels())
        self.assertFalse(any(any('atempo=' in item for item in command)
                             for _phase, label, command in self.calls))

    def test_local_rate_one_is_passthrough_without_ffmpeg(self):
        code, report = self.run_speak(backend='local', rate=1.0)
        self.assertEqual(code, 0)
        self.assertEqual(report['backend'], 'local')
        self.assertFalse(report['tempo_applied'])
        self.assertEqual(self.labels(), ['_synth', '_play'])
        self.assertEqual(report['audio_seconds'], 1.0)

    def test_auto_missing_ffmpeg_still_falls_back_to_local_rate_one(self):
        missing = self.home / 'no-ffmpeg'
        with patch.object(self.module, 'FFMPEG', missing):
            code, report = self.run_speak(rate=1.0, cloud_speech=self.fake_cloud())
        self.assertEqual(code, 0)
        self.assertEqual(report['backend'], 'local')
        self.assertTrue(report['fallback'])
        self.assertEqual(report['fallback_reason'], 'decode')
        self.assertIn('ffmpeg', report['fallback_detail'])
        self.assertNotIn('ffmpeg', self.labels())
        self.assertEqual(self.labels(), ['_synth', '_play'])

    def test_local_rate_requires_ffmpeg_and_reports_actionable_error(self):
        missing = self.home / 'no-ffmpeg'
        with patch.object(self.module, 'FFMPEG', missing):
            code, report = self.run_speak(backend='local', rate=1.1)
        self.assertEqual(code, 1)
        self.assertEqual(report['status'], 'failed')
        self.assertIn('ffmpeg', report['error'])
        self.assertNotIn('_synth', self.labels())
        self.assertNotIn('_play', self.labels())

    def test_report_contains_no_audio_or_key(self):
        code, report = self.run_speak(cloud_speech=self.fake_cloud(
            audio=b'raw-audio-must-not-appear', meta={'model': 'seed-tts-2.0',
                                                      'code': 20000000,
                                                      'request_id': 'req-123'}))
        self.assertEqual(code, 0)
        dumped = json.dumps(report, ensure_ascii=False)
        self.assertNotIn('raw-audio-must-not-appear', dumped)
        self.assertNotIn(FAKE_PLAN_KEY, dumped)
        self.assertEqual(set(report['cloud']), {'model', 'code', 'request_id', 'audio_seconds'})

    def test_last_run_is_cleared_and_records_only_metadata(self):
        code, report = self.run_speak(backend='local', rate=1.0)
        self.assertEqual(code, 0)
        stored = self.last_report()
        self.assertEqual(stored['backend'], 'local')
        self.assertEqual(stored['rate'], 1.0)
        self.assertEqual(stored['status'], 'played')
        self.assertNotIn('text', stored)
        self.assertNotIn('stdout', stored)


class LocalVoiceApiTests(unittest.TestCase):
    """Real console server: session/origin guards, allowlist and actions."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name).resolve()
        cls.state = root / 'state'
        cls.state.mkdir()
        cls.server = console.ThreadingHTTPServer(('127.0.0.1', 0), console.Handler)
        cls.port = cls.server.server_address[1]
        cls.server.daemon_threads = True
        cls.server.auth_root = cls.state
        cls.server.cookie_name = 'delegate_console_session_' + str(cls.port)
        config_path = root / 'config.json'
        config_path.write_text(json.dumps({
            'profiles': {}, 'server_url': 'http://127.0.0.1:1',
            'console_url': 'http://127.0.0.1:{}'.format(cls.port),
            'max_parallel_per_owner': 4}))
        console_auth.set_user('admin', 'api-password', cls.state)
        cls.config_patcher = patch.object(common, 'CONFIG', config_path)
        cls.state_patcher = patch.object(common, 'STATE', cls.state)

        def fake_save(options):
            if not isinstance(options, dict):
                raise ValueError('Voice settings object required')
            voice = options.get('voice')
            if voice is not None and voice not in ('vivian', 'serena'):
                raise ValueError('Unsupported local voice')
            backend = options.get('backend')
            if backend is not None and backend not in ('auto', 'cloud', 'local'):
                raise ValueError('Unsupported voice backend')
            return {'voice': voice or 'vivian', 'cloud_voice': CLOUD_VOICE,
                    'backend': backend or 'auto', 'rate': options.get('rate', 1.1)}

        def fake_preview(options):
            if not isinstance(options, dict):
                raise ValueError('Voice preview options required')
            if options.get('voice') not in (None, 'vivian', 'serena'):
                raise ValueError('Unsupported local voice')
            return {'status': 'busy'}

        cls.voice_patchers = [
            patch.object(local_voice, 'view', return_value={
                'available': True, 'reason': None, 'detail': None,
                'voices': [{'id': 'serena', 'label': 'Serena', 'dialect': None},
                           {'id': 'vivian', 'label': 'Vivian', 'dialect': None}],
                'default_voice': 'vivian', 'default_source': 'saved',
                'legacy_default': 'Vivian', 'warning': None, 'warnings': [],
                'settings_source': 'saved', 'backend': 'auto', 'rate': 1.1,
                'cloud_voice': CLOUD_VOICE, 'cloud_default': CLOUD_VOICE,
                'cloud_key_configured': True, 'cloud_ready': True,
                'cloud_model': 'seed-tts-2.0',
                'cloud_voices': [{'id': CLOUD_VOICE, 'label': 'Vivi 2.0'}],
                'cloud': {'voices': [{'id': CLOUD_VOICE, 'label': 'Vivi 2.0'}],
                          'default_voice': CLOUD_VOICE, 'selected_voice': CLOUD_VOICE,
                          'key_configured': True, 'ready': True, 'model': 'seed-tts-2.0'},
                'defaults': {'voice': 'Vivian', 'cloud_voice': CLOUD_VOICE,
                             'backend': 'auto', 'rate': 1.1},
                'preview': {'running': False, 'owned': False},
                'last': None, 'speaker': {'name': 'Mac mini扬声器'}}),
            patch.object(local_voice, 'save', side_effect=fake_save),
            patch.object(local_voice, 'preview', side_effect=fake_preview),
            patch.object(local_voice, 'stop', return_value={'status': 'idle'}),
        ]
        cls.config_patcher.start()
        cls.state_patcher.start()
        for patcher in cls.voice_patchers:
            patcher.start()
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        for patcher in reversed(cls.voice_patchers):
            patcher.stop()
        cls.state_patcher.stop()
        cls.config_patcher.stop()
        cls.tmp.cleanup()

    def _headers(self, token=None, origin=None):
        headers = {'Content-Type': 'application/json'}
        if token:
            headers['Cookie'] = 'delegate_console_session_{}={}'.format(self.port, token)
        if origin:
            headers['Origin'] = origin
        return headers

    def _login(self):
        import http.client
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=10)
        conn.request('POST', '/console-api/auth/login',
                     json.dumps({'username': 'admin', 'password': 'api-password'}),
                     {'Content-Type': 'application/json'})
        response = conn.getresponse()
        self.assertEqual(response.status, 200)
        cookie = response.getheader('Set-Cookie').split(';')[0]
        conn.close()
        return cookie.split('=', 1)[1]

    def _request(self, method, path, body=None, token=None, origin=None):
        import http.client
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=10)
        conn.request(method, path, json.dumps(body) if body is not None else None,
                     self._headers(token, origin))
        response = conn.getresponse()
        raw = response.read()
        status = response.status
        headers = dict(response.getheaders())
        conn.close()
        try:
            body = json.loads(raw) if raw else {}
        except ValueError:
            body = raw
        return status, body, headers

    def test_voice_endpoints_require_session_and_same_origin(self):
        status, _, _ = self._request('GET', '/console-api/local-voice')
        self.assertEqual(status, 401)
        status, _, _ = self._request('GET', '/console-assets/voice-settings.js')
        self.assertNotEqual(status, 200)
        token = self._login()
        status, body, _ = self._request('GET', '/console-api/local-voice', token=token)
        self.assertEqual(status, 200)
        self.assertIn('available', body)
        self.assertTrue(body['cloud_key_configured'])
        status, body, _ = self._request(
            'POST', '/console-api/local-voice', {'action': 'save', 'voice': 'vivian'},
            token=token, origin='http://evil.example')
        self.assertEqual(status, 403)
        status, _, headers = self._request('GET', '/console-assets/voice-settings.js', token=token)
        self.assertEqual(status, 200)
        self.assertIn('javascript', headers.get('Content-Type', ''))

    def test_voice_actions_are_allowlisted_and_bounded(self):
        token = self._login()
        status, body, _ = self._request(
            'POST', '/console-api/local-voice', {'action': 'stop'}, token=token)
        self.assertEqual(status, 200)
        self.assertEqual(body, {'status': 'idle'})
        status, body, _ = self._request(
            'POST', '/console-api/local-voice',
            {'action': 'preview', 'voice': 'vivian', 'backend': 'cloud', 'rate': 1.25,
             'cloud_voice': CLOUD_VOICE}, token=token)
        self.assertEqual(status, 200)
        self.assertEqual(body, {'status': 'busy'})
        status, body, _ = self._request(
            'POST', '/console-api/local-voice',
            {'action': 'save', 'voice': 'vivian', 'cloud_voice': CLOUD_VOICE,
             'backend': 'auto', 'rate': 1.1}, token=token)
        self.assertEqual(status, 200)
        self.assertEqual(body['backend'], 'auto')
        self.assertEqual(body['rate'], 1.1)
        status, body, _ = self._request(
            'POST', '/console-api/local-voice', {'action': 'delete', 'voice': 'vivian'}, token=token)
        self.assertEqual(status, 400)
        status, _, _ = self._request(
            'POST', '/console-api/local-voice', {'action': 'save', 'voice': '../../bin/sh'}, token=token)
        self.assertEqual(status, 400)
        status, _, _ = self._request(
            'POST', '/console-api/local-voice', {'action': 'save', 'backend': 'speech'}, token=token)
        self.assertEqual(status, 400)
        status, _, _ = self._request(
            'GET', '/console-api/local-voice', token=token)
        self.assertEqual(status, 200)

    def test_preview_receives_backend_rate_and_cloud_voice(self):
        token = self._login()
        with patch.object(local_voice, 'preview', return_value={'status': 'busy'}) as preview:
            status, body, _ = self._request(
                'POST', '/console-api/local-voice',
                {'action': 'preview', 'voice': 'serena', 'backend': 'cloud',
                 'rate': 1.35, 'cloud_voice': CLOUD_VOICE}, token=token)
        self.assertEqual(status, 200)
        self.assertEqual(body, {'status': 'busy'})
        preview.assert_called_once_with({'action': 'preview', 'voice': 'serena',
                                         'backend': 'cloud', 'rate': 1.35,
                                         'cloud_voice': CLOUD_VOICE})

    def test_voice_view_is_redacted(self):
        token = self._login()
        status, body, _ = self._request('GET', '/console-api/local-voice', token=token)
        self.assertEqual(status, 200)
        self.assertEqual(body['default_voice'], 'vivian')
        self.assertEqual([voice['id'] for voice in body['voices']], ['serena', 'vivian'])
        self.assertTrue(body['available'])
        self.assertEqual(body['rate'], 1.1)
        self.assertEqual(body['cloud_voice'], CLOUD_VOICE)


if __name__ == '__main__':
    unittest.main()
