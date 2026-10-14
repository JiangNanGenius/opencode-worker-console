# OpenViking project memory

OpenViking is the Worker Desk memory backend. It runs as a loopback-only HTTP
service owned by this user; every vector, file and session-derived record stays
in `~/.local/state/delegate-opencode/openviking` on the internal disk.

- **Server:** published `openviking` wheel (AGPL-3.0, unmodified) in
  `~/.local/state/delegate-opencode/runtime/openviking/venv`, managed by
  `scripts/openviking_service.py`.
- **Model configuration:** Ark plan embeddings (`doubao-embedding-vision`,
  2048 dims, `https://ark.cn-beijing.volces.com/api/plan/v3`) and the
  DeepSeek Flash VLM (`deepseek-flash`) for session memory extraction.
- **Config:** `~/.openviking/ov.conf` (owner-only) keeps only environment
  references (`${OPENVIKING_ARK_API_KEY}`, `${OPENVIKING_DEEPSEEK_API_KEY}`,
  `${OPENVIKING_ROOT_API_KEY}`); the bridge injects real values through the
  child environment at start.
- **Authentication:** `server.auth_mode = api_key` on loopback only. The
  owner-only root key (`state/credentials/openviking-root-key`) is used solely
  for administration; a generated user key (`state/credentials/openviking-api-key`)
  authenticates all memory CRUD/search and is shared with the plugin through
  `OPENVIKING_API_KEY`. Unauthenticated data requests return 401; `/health` and
  `/ready` stay public. CORS allows explicit loopback origins only.
- **Client config:** `~/.openviking/ovcli.conf` (owner-only) stores the URL and
  an `${OPENVIKING_API_KEY}` reference the loader expands; no key is persisted.

## Components

| Piece | Path | Role |
| --- | --- | --- |
| Service lifecycle | `scripts/openviking_service.py` | config materialization, start/stop/status, doctor, health/ready probes |
| Bridge backend | `scripts/openviking_backend.py` | CRUD, search, projects, stats, pins, the SQLite journal, rolling retention |
| Migration | `scripts/openviking_migrate.py` | consistent backup, bounded resumable import, delta, deletion reconciliation, verification |
| OpenCode plugin | `@openviking/opencode-plugin` | automatic recall/capture inside worker sessions |
| Console | `web/memory.js` + `/console-api/memory/*` | browse, inspect, semantic search, CRUD/pin, capacity preview and confirmed maintenance |

## Memory mapping

Legacy `opencode-mem` records become memory files under
`viking://user/default/memories/<category>/<slug>-<hash>.md`. Every file carries
a `MEMORY_FIELDS` footer that preserves provenance: `legacy_id`, `legacy_type`,
`project` (the legacy container tag), `project_path`, `project_name`,
`session_id`, `display_name`, `created_at`, `updated_at`, `pinned`.

Management reads use the authenticated raw-content API because ordinary model
reads intentionally hide that footer. Body edits use OpenViking's native
body-only replacement, which preserves its existing metadata. Live pin state and
edit timestamps are authoritative in the bridge journal; pin tags stay available
to retrieval. Re-sending a metadata trailer during replacement would duplicate
it and is deliberately avoided.

Retrieval tags on each file: `project=<container-tag>`, `origin=legacy-migration`
(or `bridge`), `legacy_type=<type>`, plus the original legacy tags as `tag=<x>`.
Project scoping uses the tag filter, so project association survives when an
external volume is unmounted.

The journal at `~/.local/state/delegate-opencode/openviking/memory-state.db`
stores `legacy-id -> viking-uri`, pin state, content hashes and tombstones. A
deleted record is tombstoned by both URI and legacy id, so later migration
passes never resurrect it.

## Console management surface

The console memory page extends the previous flat list; it does not replace the
bridge API. All routes stay behind the console session and resolve the service
key server-side only.

| Route | Purpose |
| --- | --- |
| `GET /console-api/memory/browse` | paginated records with real category/folder/project facets; `q` switches to native semantic `find` scoped to the selected project |
| `GET /console-api/memory/detail?id=` | one record (or one real folder) with sanitized body, provenance and L0/L1/L2 levels |
| `GET /console-api/memory/activity` | native task queue, recent session commits, registered sessions and index counts |
| `GET /console-api/memory/capacity` | logical estimate, measured workspace, rolling policy and a non-mutating reclaim preview |
| `POST /console-api/memory` | existing CRUD/pin/bulk-delete actions plus `maintenance_run` |

Semantics that the page must keep:

- **Projects lead navigation.** The browser remembers the last explicitly selected
  project and restores it only if it still exists. Switching projects clears the
  previous detail; full content opens only after a record is selected.
- **Lists are metadata-first.** One depth-aware native tree listing with per-file tags builds a
  short-lived navigation index; only the visible page reads bounded, server-
  sanitized previews. Full bodies are read (raw, server-parsed) only for the
  record selected in the inspector.
- **Summaries are never invented.** OpenViking stores L0/L1 per folder, and the
  native endpoint falls back to the parent folder for a file. The inspector shows
  the folder summary labelled as such, marks aggregate folder summaries when a
  project filter is active, and shows an explicit absent state when a summary is
  missing or not ready. It also exposes the native `source_extraction_id` as
  provenance; there is no supported reverse link from an extraction id to a
  session, so no session association is fabricated.
- **Session links require verification.** A migrated `session_id` becomes a link
  only when it matches a real OpenCode session; otherwise it is shown as plain
  text.
- **Capacity separates measures.** The logical estimate (`content + float32
  vector + bounded row overhead` per record) and the measured bytes under the
  live `openviking/workspace` are displayed as different numbers; migration
  backups and the legacy store are excluded and are never counted as live memory.
  The reclaim preview only lists the oldest unpinned candidates and never
  mutates; the confirmed `maintenance_run` is the console reclaim action.
  Scheduled rolling retention remains active under the configured policy.
- **Pins are the union of both owners.** Bridge journal pins and native
  `pinned=true` retrieval tags are both protected from rolling retention. When
  the native pinned-tag listing cannot be verified, maintenance fails closed and
  reclaims nothing for that run.
- **URIs are validated before any action.** Management URIs must be descendants
  of `viking://user/default/memories`; traversal, percent-escapes, query
  fragments and other namespaces are rejected.
- **Legacy backends stay flat.** With `memory.backend = "opencode-mem"` the page
  keeps a paginated flat list and CRUD; navigation, inspector, activity and
  capacity are capability-gated and report that they need the OpenViking backend.

## Commands

```bash
delegate-opencode memory status                 # backend, readiness, counts, migration
delegate-opencode memory projects
delegate-opencode memory list --directory "$PWD"
delegate-opencode memory search --directory "$PWD" 'topic'
delegate-opencode memory add 'verified fact' --directory "$PWD"
delegate-opencode memory update URI 'new text'
delegate-opencode memory pin URI [--off]
delegate-opencode memory delete URI --yes
delegate-opencode memory migration              # journal and failure status
delegate-opencode memory migrate --plan         # pending count
delegate-opencode memory migrate --backup       # consistent snapshot incl. WAL
delegate-opencode memory migrate --apply --batch 50
delegate-opencode memory migrate --verify       # counts plus content-hash samples
delegate-opencode memory maintain               # capacity retention now
delegate-opencode memory cutover --apply        # delta + verify + flip backend (idle boundary)
delegate-opencode memory cutover --rollback     # flip back to the legacy backend
```

## Migration protocol

1. `--backup` snapshots every legacy SQLite database with the SQLite backup API
   (WAL included) under `openviking/migration/backup-<timestamp>`, plus a manifest.
2. `--apply` imports live records only, writes a journal row after each chunk and
   can resume after interruption; failed records fall back to single writes and
   are reported, never fabricated.
3. `--verify` compares legacy live counts with the journal, samples
   representative bodies by content hash and reports per-project coverage.
4. After capture cutover, rerun `--apply` as a delta pass (bounded and
   idempotent), then `--verify`. `reconcile_deletions()` removes records deleted
   in the legacy store while migration ran.

## Cutover and rollback

Cutover is one configuration value: `memory.backend` in
`~/.config/opencode/delegate-pool.json` (`opencode-mem` or `openviking`). The
runtime overlay loads exactly one capture plugin, so the two backends never
capture the same session twice. `memory cutover --apply` runs the delta import,
verification and the atomic flip; `delegate-opencode service restart` (idle-safe,
refuses while work is active) then applies the plugin and environment change.
Run `memory migrate --apply --verify --offline` once more after the restart to
import the final legacy-plugin disposal captures. `--offline` explicitly reads
the old SQLite shards and their WAL without the retired HTTP service; it fails
on an unreadable shard rather than treating missing data as deletion. It is not
an automatic fallback for an API or authentication error. The legacy writer must
be stopped before this final pass. `memory cutover --rollback` flips back if
validation fails.

Rollback: set `memory.backend` back to `opencode-mem` and restart. The legacy
store is untouched by migration (backup plus original files), and OpenViking
data remains on disk for inspection.

## Retention and accounting

Rolling retention keeps the configured ceiling (`max_storage_mb: 1024` (1 GB),
`target_percent: 90`, `max_memories: 100000`) and never reclaims pinned records.
It runs from the pool daemon on the configured interval, so a machine that was
off for a month still runs maintenance at the next service start. Embedding use
continues through the existing Ark reserve settings; the VLM is DeepSeek Flash
with text and tool-extraction requests.

## Runtime files

| Item | Location |
| --- | --- |
| Server workspace and data | `~/.local/state/delegate-opencode/openviking/workspace` |
| Bridge journal | `~/.local/state/delegate-opencode/openviking/memory-state.db` |
| Migration backups | `~/.local/state/delegate-opencode/openviking/migration/` |
| Server log | `~/.local/state/delegate-opencode/openviking/logs/openviking.log` |
| Plugin log | `~/.config/opencode/openviking/openviking-memory.log` |

Diagnose with `delegate-opencode memory status` (health + readiness + counts) and
the official `openviking-server doctor`. Health alone is not readiness: `/ready`
must report `ready` with `embedding: ok` before imports or capture are trusted.
