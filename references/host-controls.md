# Host overview and structured controls

The authenticated console's default home is the **Host overview**: live host
metrics, active Codex/OpenCode work, provider quotas and service health, plus
progressive drawers of fixed, validated controls. There is deliberately **no
terminal and no file browser** in this surface.

## Architecture

- `scripts/host_telemetry.py` — one shared background collector owned by the
  console process. Host metrics every **2 s**, task activity every **3 s**,
  provider quotas on an independent **300 s** cadence. GET handlers only ever
  read its in-memory cache; slow acquisition never happens inside an HTTP
  request.
- Storage: `STATE/host/telemetry.sqlite` (owner-only). Raw 2-second samples
  are kept for **1 hour**; one-minute aggregates for **7 days**. Row caps are
  enforced on every prune. Chart ranges: `15m` (raw), `1h` (raw downsampled),
  `24h` (minute aggregates). Missing samples stay gaps — timestamps are never
  zero-filled.
- Physical board: the same snapshot is published atomically to
  `STATE/host-board.json` (mode `0600`) using the existing board **v2**
  payload (`schema_version: 2`, `unix_time`, `utc_offset_seconds`, `system`,
  `tasks: {codex, opencode}`, `quotas`). The `system` block carries the
  original `status_agent` aliases (`host`, `cpu`, `cpu_temp_c`, `gpu_temp_c`,
  `memory_percent`, `memory_used_gb`, `memory_total_gb`, `disks`,
  `uptime_hours`) alongside the richer new names, so existing firmware keeps
  working. Board consumers read the file; their presence or absence never
  affects the collector. Quota IDs on this board-only payload retain the
  firmware identifiers `codex`, `kimi`, `ark`, `deepseek`; the web snapshot
  retains full provider IDs such as `volcengine-agent-plan`.
- `scripts/host_controls.py` — fixed action dispatch with validated
  parameters, metadata-only audit (`STATE/host/audit.log`, size-bounded), and
  capability probes.
- `scripts/host_helper.py` + `scripts/install_host_helper.py` — the narrow
  privileged helper used for power modes and sleep/restart/shutdown.

Sensor sources (all bounded, read-only; failures isolate to `null` fields):

| Metric | Source |
| --- | --- |
| CPU, memory, load | `ps` / `vm_stat`+`sysctl` (macOS), `/proc` (Linux) |
| GPU utilization | `macmon` structured counters (Apple Silicon; `null` when absent) |
| Memory pressure | `kern.memorystatus_vm_pressure_level` normal/warning/critical state (macOS), PSI `/proc/pressure/memory` (Linux) — the kernel pressure signal, not derived from free bytes |
| Temperatures, fans, thermal pressure | fan daemon Unix socket (`/var/run/fancontrold.sock`), structured JSON |
| Network rate | `netstat -ib` / `/proc/net/dev` cumulative deltas |
| Disk I/O | `IOBlockStorageDriver` byte counters via `ioreg` (macOS); `/proc/diskstats` (Linux). Both provide read/write counters; missing counters remain `null` |
| Disk capacity | internal SSD + mounted external volumes (diskutil/`df` discovery; ejected volumes disappear and stale cache entries are pruned) |

Quotas: worker providers are read from the delegate daemon's cache
(`STATE/quota.json`), so the collector never issues provider network requests
or influences routing economics. The Codex adapter (CodexBar CLI, 35 s bound)
runs only on the background quota thread; a failed sample keeps the previous
windows (stale, not empty).

## Console endpoints (session + Origin gated, like every console API)

- `GET /console-api/host/snapshot` — cached snapshot: `system`, `tasks`,
  `quotas`, `services`, `service_health`, `processes`, `caffeinate`,
  `operations`, `affected_jobs`, `stale`.
- `GET /console-api/host/history?range=15m|1h|24h` — `{series: {metric: [{t, v}]}}`.
- `GET /console-api/host/capabilities` — what this host supports and what is writable.
- `GET /console-api/host/operations` — persisted power-operation log, reconciled
  against observed boot evidence.
- `POST /console-api/host/action` — `{action, params}`; validation failures are
  400, unavailable privileged helpers are 503.

Legacy compatibility: `/console-api/state` still returns the
`system_status`-shaped `host` block; existing pages and routes are unchanged.

## Controls and their guards

- **Fans** — daemon IPC writes are limited to `setControlMode`, `setTarget`
  and `applyProfile`. Profiles are the built-in `Silent`, `Balanced`, `Cool`,
  `Max`; fixed RPM is validated against the daemon-reported per-fan bounds.
  Re-sending the identical fixed RPM is an idempotent no-op. The daemon safety
  floor (`setSafety`, profile editing) is never touched.
- **Power modes** — only modes listed by `pmset -g cap` (e.g. `lowpowermode`,
  `highpowermode`). Applied through the helper; the new value is parsed back
  from `pmset -g custom` before the action reports success.
- **Keep-awake** — one console-managed `caffeinate -i`: `off`, `timed`
  (1–720 minutes) or `while_tasks` (released 10 s after the last active
  bridge/Codex task). Existing sleep settings are never rewritten.
- **Processes** — top CPU/RSS list is own-user only, name-only (no argv).
  Terminate sends TERM; KILL is a separate explicit action. PID **and** start
  time must match what was listed, so a recycled PID is never signalled.
  PID 1, the console lineage, and the managed services are protected.
- **Services** — restart is allowlisted to `server`, `pool`, `console`.
  `server`/`pool` refuse while any task is queued or running. Console restart
  respawns the process after the response is sent.
- **Sleep / restart / shutdown** — require `confirm: true`, the exact current
  affected-job list (active/queued bridge tasks **plus** active Codex
  conversations), and a fresh password check against the local account
  verifier behind the standard per-peer login throttle. The password is never
  persisted or logged. The request is deduplicated for 60 s under a file lock,
  persisted with `accepted → dispatched/failed` status, and restart/shutdown
  reconcile to `completed` only when observed boot evidence proves a reboot.
  Accepted is never reported as completed.

## Privileged helper

The console never runs as root. Disruptive commands execute via
`/opt/opencode-host/bin/opencode-host-helper` (dedicated root-owned directory,
helper `root:wheel 0755`, fixed `#!/usr/bin/python3 -I` shebang — no
caller-controlled PATH/PYTHONPATH, absolute `/usr/bin/pmset` and
`/sbin/shutdown` only, fixed command grammar, no secrets on argv).

Install (requires sudo, preserves any previous helper/sudoers byte-for-byte on
failure):

```sh
sudo python3 scripts/install_host_helper.py
```

Verify (as the console user):

```sh
sudo -n /opt/opencode-host/bin/opencode-host-helper --verify
```

Until the helper is installed **and** the verify check succeeds, power modes
and power operations report unavailable; everything else works without it.

## Configuration and state paths

| Path | Purpose |
| --- | --- |
| `STATE/host/telemetry.sqlite` | bounded raw + minute history |
| `STATE/host-board.json` | board v2 payload, atomic, `0600` |
| `STATE/host/audit.log` | metadata-only control audit, size-bounded |
| `STATE/host/caffeinate.json` | managed keep-awake state |
| `STATE/host/operations.json` | persisted power operations (50 latest) |
| `/opt/opencode-host/bin/opencode-host-helper` | privileged helper |
| `/etc/sudoers.d/opencode-host-helper` | narrow NOPASSWD allowlist |

## Rollback

```sh
sudo rm -f /etc/sudoers.d/opencode-host-helper
sudo rm -rf /opt/opencode-host
rm -rf "$HOME/.local/state/delegate-opencode/host"
rm -f "$HOME/.local/state/delegate-opencode/host-board.json"
```

Deleting the web view is one revert of this repository; legacy pages and APIs
are untouched by the feature.

## Limitations

- GPU utilization requires `macmon` (Apple Silicon); other hosts report `null`.
- Disk throughput uses differences between real byte counters; missing samples
  leave gaps instead of inferred throughput.
- Sleep operations have no observable readback; they remain `dispatched` (not
  `completed`) until restart evidence exists or an operator treats the machine
  coming back as proof.
- Fan control requires the local fan daemon; without its socket all fan
  fields are `null` and the drawer reports unavailable.
- Thermal performance impact of fan/power changes is not modelled or claimed;
  controls report observed state only.
