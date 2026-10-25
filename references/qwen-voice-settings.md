# Voice reminder settings

Worker Desk exposes a **语音提醒 / Voice reminders** card in **Models & routing**
for the installed `qwen-voice-notify` notification skill. The card selects a
cloud-first delivery path (Volcengine Ark Agent Plan `seed-tts-2.0`) with an
automatic offline Qwen3-TTS fallback, a shared speech rate, and separate cloud
and local voices. It never plays audio on page load, never changes system volume
or the default output device, never downloads a model and never starts a
resident inference server.

## Architecture

| Piece | Location | Responsibility |
| --- | --- | --- |
| `scripts/local_voice.py` | this repository | Catalogs, persisted preferences, bounded preview and ownership-checked stop |
| Console routes | `scripts/console.py` | `GET/POST /console-api/local-voice` behind the normal session, host and CSRF guards |
| `web/voice-settings.js` | this repository | Separate deferred card controller (no `app.js` edits) |
| `notify.py` | installed skill | Cloud request, local fallback, single-flight lock, playback, `last-run.json` |
| Local model/runtime | `~/.local/share/qwen-voice-notify` | Read-only local weights, venv and skill state |

The console starts only the installed entry point with a fixed argv. Saved or
form values are validated and passed as separate argv entries:

```
<venv>/bin/python <skill>/scripts/notify.py speak \
  --backend auto|cloud|local --rate <0.50-2.00> \
  --voice <local-catalog-id> --cloud-voice <cloud-catalog-id> --timeout 120
```

The preview sentence is the fixed short Chinese `您好，这是千问语音提醒试听。`
(sent through stdin). A request body cannot supply text, commands, paths, shell
fragments or environment values. Preview is non-blocking: the API returns
immediately and the card polls bounded status until the skill publishes
`last-run.json` or the console records its own outcome.

## Delivery modes and fallback

- `auto` (default): try cloud first. If cloud speech fails **before playback
  starts**, fall back to the installed local Qwen voice and record the effective
  backend plus `fallback_reason` / `fallback_detail`.
- `cloud`: never fall back. A missing key, auth/quota failure, timeout or bad
  response is reported with an actionable reason.
- `local`: never touches the network. The local model is used directly.

Fallback is allowed for network, timeout, provider, missing-key, auth, quota,
protocol, empty, truncated, oversized and pre-playback decode failures. It is
**never** attempted after audible playback has started or after a cancellation,
and a fallback never reads the text twice. Missing key/auth/quota messages say
what to fix; they do not claim the whole feature is offline. If the local
fallback also fails, the error includes both the cloud and local reasons.

`auto` and `cloud` attempt the cloud even when the local model or catalog is
absent; a missing local model is not a validation failure for those modes.
Only `local` requires the local catalog up front, and `auto` validates and
uses the local voice only if fallback is actually needed. If a cloud attempt
fails while the local model is also missing, the error reports both reasons.

All routing, synthesis and playback stay inside the single `speech.lock`
single-flight section. `active.json` reports `cloud`, `cloud_decode`,
`synthesis`, `tempo` or `playback`; `stop` signals exactly the active PID and the
skill's `SIGTERM` handler reaps the child process group.

## Cloud contract

Exact endpoint:

```
POST https://openspeech.bytedance.com/api/v3/plan/tts/unidirectional
```

The `/plan/` segment is required. The generic pay-as-you-go endpoint is a
different contract and must not be substituted.

Headers:

| Header | Value |
| --- | --- |
| `X-Api-Key` | The existing Agent Plan dedicated key resolved at runtime |
| `X-Api-Resource-Id` | `seed-tts-2.0` |
| `X-Api-Request-Id` | A fresh UUID per request |
| `Content-Type` | `application/json` |

Payload. `speech_rate` is **inside `audio_params`** (the official HTTP contract),
not a sibling of `text`/`speaker`:

```json
{
  "req_params": {
    "text": "您好，这是测试。",
    "speaker": "zh_female_vv_uranus_bigtts",
    "audio_params": {
      "format": "mp3",
      "sample_rate": 24000,
      "speech_rate": 10
    }
  }
}
```

`speech_rate = int(round((rate - 1) * 100))`. The native range is `-50..100`,
which maps exactly to the console's `0.50..2.00` range (`1.10` → `10`). The
cloud applies this rate natively; the returned audio is **not** passed through
`atempo` again, so a cloud preview is never double-slowed or double-sped.

The response is HTTP NDJSON:

- `{"code":0,"data":"<base64 audio>"}` frames contribute MP3 bytes.
- `{"code":20000000,...}` is the successful terminal frame.
- Any other positive code is an error. Empty audio, a missing terminal frame,
  malformed JSON/base64, non-object frames, oversized audio (8 MiB) or an
  oversized line (4 MiB) are rejected.
- A non-200 HTTP status is classified as auth (401/403), quota (402/429),
  timeout (408) or provider (other).

Every accepted cloud response is decoded to a temporary MP3, transcoded to a
temporary WAV with `/opt/homebrew/bin/ffmpeg`, then duration-checked before
playback. Raw MP3/WAV bytes, base64 frames and text are never written to
`last-run.json`, console responses or logs.

### Timeouts

The cloud path uses a 5-second connect/read inactivity timeout and a 15-second
hard total deadline. A watchdog shuts the socket down at the total deadline, so
a provider that drips one byte just before each inactivity timeout still
unblocks and is classified as a timeout. This keeps the local fallback timely.

### Credential resolution

The skill reads the dedicated key from the local OpenCode provider store:

```
~/.local/share/opencode/auth.json  (XDG_DATA_HOME respected)
provider: volcengine-agent-plan  →  {type: "api", key: "…"}
```

This is the same provider entry used by `common.auth_key('volcengine-agent-plan')`
and `ark_harness_mcp.resolve_plan_key`; the installed skill cannot import the
console repository portably, so it reads the same narrow entry directly. The
key is never placed in argv, environment variables, settings, command strings,
logs, console JSON or `last-run.json`, and the console only exposes a boolean
`cloud_key_configured`.

## Local rate and tempo

The local path preserves pitch and sample rate:

1. `_synth` loads Qwen3-TTS, writes a temporary WAV and **exits**.
2. If `rate == 1.0`, the WAV is used as-is (passthrough).
3. If `rate != 1.0`, the parent runs the installed
   `/opt/homebrew/bin/ffmpeg` with the validated fixed filter
   `-filter:a atempo=<0.50-2.00>` and `-c:a pcm_s16le`. No `asetrate` is used,
   so the sample rate and pitch stay unchanged while the duration changes.
4. Only then is `_play` started.

The ffmpeg filter value comes from a validated float and is formatted to two
decimals; it is always one array element and never a shell string. `1.0` is a
strict passthrough, not a near-1.0 filter. Cloud audio is decoded but never
tempo-filtered.

If ffmpeg is missing: local `rate=1.0` still works; local `rate!=1.0` fails with
an actionable error; `auto` cloud decode turns the missing decoder into a
pre-playback fallback reason and still tries the local path.

## Catalogs

The local catalog is read from the installed model's own
`~/.local/share/qwen-voice-notify/model/config.json` →
`talker_config.spk_id` / `spk_is_dialect`. The checked-in model revision exposes
nine voices: `serena`, `vivian`, `uncle_fu`, `ryan`, `aiden`, `ono_anna`,
`sohee`, `eric`, `dylan`. Only configured dialects are annotated (`eric` =
Sichuan, `dylan` = Beijing).

The cloud catalog is a checked-in subset of the documented
`seed-tts-2.0` (uranus) voices; it cannot be enumerated without a network
request, and a GET never performs one. The default is
`zh_female_vv_uranus_bigtts` (Vivi 2.0); the official
`zh_female_gaolengyujie_uranus_bigtts` (高冷御姐 2.0) is included as another
selectable example. The canonical list lives in both `notify.py` and
`scripts/local_voice.py`, and a test asserts they stay identical.

If the local model is missing, the card reports local voice as unavailable.
Cloud-only and automatic previews still run when the Agent Plan key is
configured; an explicit `--voice` is only syntax-checked in that situation, so a
missing local catalog cannot block the cloud. A `local` preview or a real
fallback still requires and validates the local catalog. If the venv or
`notify.py` itself is missing, no preview can start because the skill entry
point cannot run; the card reports that runtime reason. If both cloud and local
paths are unavailable, save/preview are disabled or fail with the composed
reason instead of fabricating availability.

## Persisted preferences

`~/.config/qwen-voice-notify/settings.json` is owner-only and written with a
temporary file + `fsync` + `os.replace`:

```json
{
  "schema": 2,
  "voice": "vivian",
  "cloud_voice": "zh_female_vv_uranus_bigtts",
  "backend": "auto",
  "rate": 1.1,
  "updated_at": 1792900000.0
}
```

Defaults:

| Field | Default |
| --- | --- |
| `voice` (local fallback) | `Vivian` |
| `cloud_voice` | `zh_female_vv_uranus_bigtts` |
| `backend` | `auto` |
| `rate` | `1.1` |

Rules:

- Directory `0700`, file `0600`.
- `save` validates every supplied field and rejects unknown voices, unknown
  backends and rates outside `0.50..2.00` with a 400 response. Values are
  rounded to two decimals.
- `save` is an **atomic merge**: it reads the current file, updates only the
  supplied known fields, preserves unknown keys and rewrites `schema=2`. A
  legacy `save("voice")` therefore cannot reset `cloud_voice`, `backend` or
  `rate`, and a partial write cannot drop them.
- Missing fields always use their defaults, so a legacy file with only `voice`
  gets `rate=1.1`, `backend=auto` and the default cloud voice. A missing `rate`
  never becomes 0 or 2.
- Reading tolerates a missing, corrupt, oversized or invalid file. Invalid new
  fields use safe defaults with warnings (`invalid_saved_rate`,
  `invalid_saved_backend`, `invalid_saved_cloud_voice`); an invalid local voice
  falls back to Vivian with `invalid_saved_voice`. The skill silently uses the
  same fallbacks, so a manual edit cannot produce an out-of-range rate or an
  unknown backend.
- A cloud-only save works even when the local model is unavailable and preserves
  the existing local voice; it does not overwrite it with Vivian merely because
  the catalog could not be read.
- `preview-result.json` in the same directory stores the console's own bounded
  preview outcome, including effective `backend`, `rate`, `fallback`,
  `fallback_reason`, `fallback_detail` and redacted `cloud_status`. It exists so
  a skill `busy` reply — which does not write `last-run.json` — can never be
  shown as an older successful playback. It never contains raw stdout, audio,
  text or credentials.

`notify.py` reads the same file. CLI values always override it:
`--backend`, `--rate`, `--voice` (local) and `--cloud-voice`.

## Console API

Both routes inherit the console session cookie, host/origin checks and CSRF
rejection; there is no separate auth path.

`GET /console-api/local-voice` returns metadata only. It may run the installed
`notify.py status` probe (speaker name/model completeness/settings) and may read
whether the Agent Plan key is configured, but it never synthesizes, plays or
performs a network request.

`POST /console-api/local-voice` accepts one of:

```json
{"action":"save","voice":"vivian","cloud_voice":"zh_female_vv_uranus_bigtts","backend":"auto","rate":1.1}
{"action":"preview","voice":"serena","cloud_voice":"zh_female_vv_uranus_bigtts","backend":"auto","rate":1.1}
{"action":"stop"}
```

`save` returns the effective four fields and any normalization warnings.
`preview` returns `started`, `busy` or `already_running` with the effective
backend/rate/voice. `stop` returns `stop_requested` or `idle`. Invalid actions,
voices, backends or rates are 400; an unavailable local runtime or a cloud-only
preview without a configured key is a bounded 503-style runtime error. The
response is passed through the console's existing redaction.

The GET view includes:

- `available`, `reason`, `detail`, local `voices`, `default_voice`,
  `default_source`, `warnings`, `settings_source`;
- `backend`, `rate`, `cloud_voice`, `cloud_voices`, `cloud_default`,
  `cloud_key_configured`, `cloud_ready`, `cloud_model`, `cloud` metadata and
  `defaults`;
- `preview` (`running`, `owned`, `pending`, `phase`, `voice`, `backend`, `rate`,
  `started_at`) and `speaker`;
- `last`, whose fields are `status`, `voice`, `backend`, `requested_backend`,
  `rate`, `cloud_voice`, `fallback`, `fallback_reason`, `fallback_detail`,
  `cloud_status`, `audio_seconds`, `total_seconds`, `error` and `finished_at`.
  Paths are replaced with `~`; `cloud_status` is limited to model/code/request
  id/reason/status/detail.

## Preview ownership and stop

The console records its preview PID, the process start token (survives a launcher
re-exec) and the exact voice/backend/rate in `preview.json`. It revalidates that
PID against both its own record and the skill's `active.json` before signalling.
Stop never delegates to the skill's generic `stop` command, which re-reads
`active.json` and could cancel a newer playback started by another client.
Another client's playback is reported as `busy` and is never signalled. A
verified startup `pending` state prevents a duplicate preview from overwriting
ownership before the skill publishes `active.json`, and the reaper drains and
clears the record when the child exits.

## Security boundaries

- No secret is stored in `settings.json`, `preview.json`, `preview-result.json`,
  `last-run.json`, argv, environment variables, console responses or logs.
- The cloud key is used only in the in-process HTTP header and is scrubbed from
  any surfaced error; the console exposes only a boolean.
- Provider error text is not copied into user-facing messages; the report keeps
  only a bounded model/code/request-id/reason/status detail.
- A GET never performs a cloud request or synthesis.
- There is no autoplay. The skill only speaks when a caller invokes `speak`; the
  card's preview requires an explicit button. Ordinary task progress,
  completion and confirmation remain silent.

## Syncing the installed skill (backup first)

The portable canonical script is checked in as
[`references/qwen-voice-notify/notify.py`](qwen-voice-notify/notify.py) and the
canonical skill instructions as
[`references/qwen-voice-notify/SKILL.md`](qwen-voice-notify/SKILL.md). They are
byte-identical to the installed copies after a sync:

```bash
skill="$HOME/.codex/skills/qwen-voice-notify"
stamp=$(date +%Y%m%d-%H%M%S)
cp "$skill/scripts/notify.py" "$skill/scripts/notify.py.bak-$stamp"
cp "$skill/SKILL.md" "$skill/SKILL.md.bak-$stamp"
cp references/qwen-voice-notify/notify.py "$skill/scripts/notify.py"
cp references/qwen-voice-notify/SKILL.md "$skill/SKILL.md"
```

No service restart is needed: the next skill invocation reads the new script and
the console serves the changed assets from disk. The running console needs a
console-only reload to pick up the changed Python modules. Do not restart the
worker service, bridge, pool or gateway. Model weights and the venv are
untouched.

## Tests

```bash
python3 -m unittest tests.test_local_voice -v
node --test tests/voice_settings.test.js
python3 -m unittest tests.test_console_auth -v
python3 -m py_compile scripts/local_voice.py scripts/console.py references/qwen-voice-notify/notify.py
node --check web/voice-settings.js && node --check web/i18n.js && node --check web/manage.js
```

The backend suite covers catalog metadata, atomic merge/backcompat, default
1.1/missing-rate behavior, invalid settings, cloud payload nesting
(`req_params.audio_params.speech_rate`), positive/terminal NDJSON handling,
truncated/malformed/empty/oversized responses, auth/quota/network/timeout
classification, the hard total deadline under a slow read, cancellation
propagation, real ffmpeg duration/pitch and MP3 decode checks without playback,
auto fallback, missing-ffmpeg fallback at rate 1.0, no fallback after playback
or cancellation, cloud success with no local model after `resolve_settings`, and
no audio/key leakage in reports. The JS suite covers rate bounds/defaults,
cloud/local view state, fallback wording, catalog shapes and selection
persistence. The ownership startup/reaping tests remain in the backend
suite.

Native playback, hearing and final visual/timbre acceptance stay with the root
operator; the tests above prove file-level audio properties and routing, not
that anyone heard the output.
