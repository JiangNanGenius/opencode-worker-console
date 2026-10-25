"""Dynamic Kimi weekly reserve release and bounded native-Ultra demand evidence.

The static reserve (``quota.kimi_weekly_reserve_percent``, base 30%) protects the
final weekly slice for the reserved ``k3.1-pro`` Ultra model. This module owns the
two facts that a static percentage cannot express:

* the release curve inside the second half of one verified seven-day weekly
  window, and
* the bounded demand snapshot that pauses that release while Ultra work is
  queued, running, or was observed on the raw native gate in the last 30 minutes.

The curve is a pure function of the configured base, the 5% emergency floor and
the remaining fraction of the release window. Demand evidence is compact,
account/credential-scoped metadata published atomically under one short
cross-process lock. It never contains request text, task IDs, identities or
secrets. Missing, stale, corrupt, future-dated or other-account evidence is
reported as unknown and is never treated as fabricated quiet demand, so it always
keeps the base reserve.
"""
import math
import secrets
import time

import common
import routing

KIMI_PROVIDER = 'kimi-for-coding'
WEEK_SECONDS = 7 * 86400
# The full base reserve holds for the first half of a verified weekly window;
# the curve releases linearly over the second half down to the emergency floor.
RELEASE_WINDOW_SECONDS = WEEK_SECONDS / 2.0
EMERGENCY_FLOOR_PERCENT = 5.0
WEEKLY_DURATION_MINUTES = 10080.0
# The dynamic release needs a genuinely fresh authoritative weekly sample: the
# same 15-minute horizon quota.view() uses for live telemetry. A missing sample
# time, a future-dated reading or an aged one never releases the reserve.
WEEKLY_SAMPLE_MAX_AGE_SECONDS = 900.0
NATIVE_COOLDOWN_SECONDS = 30 * 60
# The pool daemon republishes every scheduler pass (about two seconds). A
# snapshot older than this is not trustworthy demand evidence, and a publish
# gap longer than this breaks the continuous-quiet observation: the safe answer
# is unknown, which keeps the base reserve instead of inventing zero demand.
SNAPSHOT_MAX_AGE_SECONDS = 90.0
CLOCK_TOLERANCE_SECONDS = 60.0
DEMAND_FILE = 'kimi-demand.json'
DEMAND_LOCK = 'kimi-demand'
MAX_SNAPSHOT_BYTES = 64 * 1024
MAX_COUNT = 100000
ACTIVE_STATUSES = ('starting', 'running', 'stopping', 'uncertain')
# One token per pool process: a restart is a new observation epoch even if the
# on-disk snapshot is recent, so old silence is never inherited across restarts.
_INSTANCE = secrets.token_hex(8)
# Process-local native-Ultra guard. It is set only by the loopback gate that
# actually carried a native Ultra request (or failed to book it), using a
# monotonic clock, so it survives wall-clock rollback and never leaks into the
# pool or console processes. It exists because a failed metadata write must not
# let a later Gate admission mistake failed bookkeeping for quiet demand.
_LOCAL = {'activity': None, 'uncertain': None}


def note_local_native_activity(now=None):
    """Mark this process as having just carried native Ultra inference."""
    _LOCAL['activity'] = time.monotonic() if now is None else float(now)


def note_local_uncertainty(now=None):
    """Mark this process's native Ultra bookkeeping as unverified."""
    _LOCAL['uncertain'] = time.monotonic() if now is None else float(now)


def local_demand_state(now=None):
    """'ultra_demand', 'unknown' or None from this process's own guard."""
    mono = time.monotonic() if now is None else float(now)
    for key, state in (('uncertain', 'unknown'), ('activity', 'ultra_demand')):
        stamp = _number(_LOCAL.get(key))
        if stamp is not None and 0 <= mono - stamp < NATIVE_COOLDOWN_SECONDS:
            return state
    return None


def reset_local_guard():
    """Clear the process-local guard (tests and explicit housekeeping only)."""
    _LOCAL['activity'] = None
    _LOCAL['uncertain'] = None


def dynamic_enabled(c):
    """Operator switch for the release curve; defaults on, only False disables."""
    return c.get('kimi_weekly_reserve_dynamic', True) is not False \
        if isinstance(c, dict) else True


def emergency_percent(base):
    """The expiry floor: never lower than the configured base, at most 5%."""
    return min(float(base), EMERGENCY_FLOOR_PERCENT)


def curve_percent(base, remaining_seconds):
    """Released threshold for one remaining span of the weekly window.

    ``emergency + (base - emergency) * clamp(remaining / (7 days / 2), 0, 1)`` so
    base 30 yields 30 at 84h, 17.5 at 42h, 12.14 at 24h and 5 at expiry. A base
    below the 5% floor keeps its own value: the curve never releases protection
    that was never configured.
    """
    base = float(base)
    emergency = emergency_percent(base)
    span = max(0.0, min(1.0, float(remaining_seconds) / RELEASE_WINDOW_SECONDS))
    return _round4(emergency + (base - emergency) * span)


def compute_reserve_state(base, dynamic_on, weekly, demand, now):
    """Pure reserve contract; every caller shares exactly this evaluation.

    ``weekly`` is ``quota.kimi_weekly_state`` output and ``demand`` is
    :func:`read_demand` output (or None when the weekly reading is not known).
    ``release_state`` is a stable display enum: fixed, not_applicable,
    weekly_unknown, reset_unknown, first_half, ultra_demand, demand_unknown,
    releasing. Only ``releasing`` lowers the effective threshold; every other
    state keeps the configured base, and a verified plan without a weekly pool
    reports no applicable protection instead of inventing one.
    """
    base = max(0.0, min(100.0, float(base)))
    if not isinstance(weekly, dict):
        weekly = {'status': 'unknown'}
    state = {
        'reserve_percent': base,
        'status': weekly.get('status'),
        'remaining_percent': weekly.get('remaining_percent'),
        'source': weekly.get('source'),
        'resets_at': weekly.get('resets_at'),
        'effective_reserve_percent': base,
        'released_percent': 0.0,
        'emergency_reserve_percent': _round4(emergency_percent(base)),
        'dynamic_enabled': bool(dynamic_on),
        'release_state': 'fixed',
        'remaining_hours': None,
        'demand': _demand_metadata(demand),
    }
    status = weekly.get('status')
    if status == 'not_applicable':
        # The authenticated plan has no weekly pool at all: nothing is reserved
        # and no threshold is simulated from the monthly total. This is a property
        # of the account, independent of the operator's dynamic-release toggle.
        state.update(release_state='not_applicable',
                     effective_reserve_percent=0.0, released_percent=0.0)
        return state
    if not dynamic_on:
        return state
    if status != 'known':
        state['release_state'] = 'weekly_unknown'
        return state
    if not _fresh_weekly_sample(weekly.get('as_of'), now):
        state['release_state'] = 'weekly_unknown'
        return state
    window = weekly.get('window') if isinstance(weekly.get('window'), dict) else {}
    reset = routing._iso_seconds(weekly.get('resets_at'))
    remaining = None if reset is None else reset - now
    duration = _number(window.get('duration_minutes'))
    # Trustworthy reset only: a real future epoch, at most one seven-day window
    # ahead, and an explicit weekly duration when the window declares one. A
    # five-hour, monthly or fabricated expiry can never act as the weekly reset.
    if reset is None or remaining <= 0 or \
            remaining > WEEK_SECONDS + CLOCK_TOLERANCE_SECONDS or \
            (duration is not None and abs(duration - WEEKLY_DURATION_MINUTES) > 0.5):
        state['release_state'] = 'reset_unknown'
        return state
    state['remaining_hours'] = _round2(max(0.0, remaining) / 3600.0)
    if remaining > RELEASE_WINDOW_SECONDS:
        state['release_state'] = 'first_half'
        return state
    kind = demand.get('state') if isinstance(demand, dict) else 'unknown'
    if kind != 'quiet':
        state['release_state'] = 'ultra_demand' if kind == 'ultra_demand' else 'demand_unknown'
        return state
    effective = curve_percent(base, remaining)
    state['effective_reserve_percent'] = effective
    state['released_percent'] = _round4(base - effective)
    state['release_state'] = 'releasing'
    return state


def task_demand_counts(all_tasks, c):
    """Bounded (queued, active) Ultra demand from the pool's already-read tasks.

    A task counts by its actual configured tier or profile model, never by title
    text. Only queued or active work participates; completed history is ignored.
    """
    profiles = c.get('profiles') if isinstance(c, dict) else None
    profiles = profiles if isinstance(profiles, dict) else {}
    queued = active = 0
    for task in all_tasks if isinstance(all_tasks, list) else []:
        if not isinstance(task, dict) or not _ultra_task(task, profiles):
            continue
        status = task.get('status')
        if status == 'queued':
            queued += 1
        elif status in ACTIVE_STATUSES:
            active += 1
    return min(queued, MAX_COUNT), min(active, MAX_COUNT)


def publish_task_demand(c, all_tasks, identity, now=None, instance=None):
    """Publish one compact demand snapshot from the pool's all_tasks listing.

    ``instance`` identifies the publishing pool process. A restart, a long
    sampling gap or an account rotation breaks the continuous-quiet streak, so
    ``quiet_since`` starts again instead of inheriting stale silence.
    """
    now = time.time() if now is None else float(now)
    counts = task_demand_counts(all_tasks, c)
    _merge_write(identity if _valid_identity(identity) else None, now, counts=counts,
                 instance=instance or _INSTANCE)
    return {'queued_ultra': counts[0], 'active_ultra': counts[1]}


def record_native_activity(identity, now=None):
    """Record one accepted raw native ``k3.1-pro`` request for the cooldown.

    A heartbeat from an already-admitted stream keeps the credential captured at
    admission: after an account rotation it is ignored instead of overwriting the
    new account's demand state.
    """
    now = time.time() if now is None else float(now)
    return _merge_write(identity if _valid_identity(identity) else None, now, native=now)


def current_identity():
    """Credential identity for demand scoping; None stays conservatively unknown."""
    try:
        return common.credential_identity(KIMI_PROVIDER)
    except Exception:
        return None


def uses_kimi(c):
    """True when any configured profile references a native Kimi model."""
    profiles = c.get('profiles') if isinstance(c, dict) else None
    if not isinstance(profiles, dict):
        return False
    for profile in profiles.values():
        model = profile.get('model') if isinstance(profile, dict) else None
        if isinstance(model, str) and model.split('/', 1)[0] == KIMI_PROVIDER and '/' in model:
            return True
    return False


def read_demand(identity, now):
    """Bounded demand verdict for this account at ``now``; never raises.

    Quiet is reported only for a fresh snapshot that belongs to the current
    credential, carries in-range counts, and provably observed no Ultra activity
    for the whole cooldown. Fresh counts alone are never enough: a new file, a
    pool restart, a sampling gap, an account rotation or a clock rollback must
    hold the base reserve until a full continuous-quiet streak exists. Everything
    else is unknown.
    """
    unknown = {'state': 'unknown', 'reason': 'missing', 'queued_ultra': None,
               'active_ultra': None, 'native_last_at': None,
               'tasks_updated_at': None, 'quiet_since': None, 'age_seconds': None}
    local = local_demand_state()
    if local is not None:
        # The Gate's own admitted/failed native Ultra evidence wins over a file
        # that may still look quiet because a metadata write failed.
        return dict(unknown, state=local, reason='local_guard')
    snapshot = _read_snapshot()
    if snapshot is None:
        return unknown
    snapshot_identity = snapshot.get('identity')
    if not _valid_identity(identity) or snapshot_identity != identity:
        return dict(unknown, reason='identity_mismatch' if _valid_identity(snapshot_identity)
                    else 'missing')
    stamp = _number(snapshot.get('tasks_updated_at'))
    if stamp is None:
        return dict(unknown, reason='missing')
    if stamp > now + CLOCK_TOLERANCE_SECONDS:
        return dict(unknown, reason='future_stamp')
    age = now - stamp
    if age > SNAPSHOT_MAX_AGE_SECONDS:
        return dict(unknown, reason='stale')
    queued = _integer(snapshot.get('queued_ultra'))
    active = _integer(snapshot.get('active_ultra'))
    if queued is None or active is None:
        return dict(unknown, reason='corrupt')
    native = None
    if snapshot.get('native_last_at') is not None:
        native = _number(snapshot.get('native_last_at'))
        if native is None:
            return dict(unknown, reason='corrupt')
        if native > now + CLOCK_TOLERANCE_SECONDS:
            # A future native stamp is clock skew or spoofing, never evidence of
            # quiet: keep the base reserve instead of trusting it.
            return dict(unknown, reason='future_stamp')
    quiet = None
    if snapshot.get('quiet_since') is not None:
        quiet = _number(snapshot.get('quiet_since'))
        if quiet is None or quiet > now + CLOCK_TOLERANCE_SECONDS or now - quiet > WEEK_SECONDS:
            return dict(unknown, reason='corrupt' if quiet is None else 'future_stamp')
    evidence = {'queued_ultra': queued, 'active_ultra': active,
                'native_last_at': native, 'tasks_updated_at': stamp,
                'quiet_since': quiet, 'age_seconds': _round2(max(0.0, age))}
    if native is not None and now - native < NATIVE_COOLDOWN_SECONDS:
        return dict(evidence, state='ultra_demand', reason='native_activity')
    if queued or active:
        return dict(evidence, state='ultra_demand', reason='task_backlog')
    if quiet is None or now - quiet < NATIVE_COOLDOWN_SECONDS:
        # Quiet right now is not enough: the release requires a continuous
        # observed-quiet streak covering the whole cooldown.
        return dict(evidence, state='unknown', reason='quiet_unobserved')
    return dict(evidence, state='quiet', reason='quiet')


def _merge_write(identity, now, counts=None, native=None, instance=None):
    """One short locked read-merge-write; the pool and gate never lose fields.

    ``native_last_at`` only advances (max), while task counts, their sample stamp
    and the continuous-quiet start are owned by the pool's publish. A restart
    (new ``instance``), a publish gap, an account rotation or a detected clock
    rollback starts a new quiet observation instead of inheriting old silence.
    The supplied identity is rechecked against the current inference credential
    inside this short lock, so a suspended writer from a previous account can
    never reset or overwrite the new account's evidence.
    """
    with common.locked(DEMAND_LOCK):
        current = current_identity()
        if not _valid_identity(identity) or not _valid_identity(current) or current != identity:
            return False
        previous = _read_snapshot() or {}
        same_owner = previous.get('identity') == identity
        previous_stamp = _number(previous.get('tasks_updated_at'))
        previous_native = _number(previous.get('native_last_at')) if same_owner else None
        rollback = (previous_stamp is not None and
                    previous_stamp > now + CLOCK_TOLERANCE_SECONDS) or \
                   (previous_native is not None and
                    previous_native > now + CLOCK_TOLERANCE_SECONDS)
        if previous_native is not None and previous_native > now + CLOCK_TOLERANCE_SECONDS:
            # Never carry a future-dated native stamp forward; the rollback flag
            # above already forced a fresh quiet observation for pool publishes.
            previous_native = None
        if counts is None and rollback:
            # A wall clock that moved backwards makes the stored evidence
            # unreadable. A heartbeat must neither overwrite the snapshot nor
            # pretend the old silence still holds; readers keep the base until
            # the pool republishes a fresh observation streak.
            return False
        merged = {'version': 1, 'identity': identity}
        previous_instance = previous.get('instance') if isinstance(previous.get('instance'), str) else None
        if counts is not None:
            merged['queued_ultra'], merged['active_ultra'] = int(counts[0]), int(counts[1])
            merged['tasks_updated_at'] = now
            merged['instance'] = instance
            previous_quiet = _number(previous.get('quiet_since'))
            continuous = (same_owner and previous_instance is not None and
                          instance == previous_instance and previous_stamp is not None and
                          0 <= now - previous_stamp <= SNAPSHOT_MAX_AGE_SECONDS and not rollback)
            if counts[0] or counts[1]:
                quiet_since = None
            elif rollback:
                # Clock rollback invalidates old quiet evidence outright: start a
                # fresh full observation window instead of reusing it.
                quiet_since = now
            elif previous_native is not None and now - previous_native < NATIVE_COOLDOWN_SECONDS:
                # Recent native Ultra activity starts the quiet clock at the
                # activity itself, so the release resumes exactly after cooldown.
                quiet_since = previous_native
            elif continuous and previous_quiet is not None and \
                    previous_quiet <= now + CLOCK_TOLERANCE_SECONDS and \
                    now - previous_quiet <= WEEK_SECONDS:
                quiet_since = previous_quiet
            else:
                quiet_since = now
            if quiet_since is not None:
                merged['quiet_since'] = quiet_since
        elif same_owner:
            queued = _integer(previous.get('queued_ultra'))
            active = _integer(previous.get('active_ultra'))
            if queued is not None and active is not None:
                merged['queued_ultra'], merged['active_ultra'] = queued, active
            if previous_stamp is not None:
                merged['tasks_updated_at'] = previous_stamp
            if previous_instance is not None:
                merged['instance'] = previous_instance
            quiet = _number(previous.get('quiet_since'))
            if quiet is not None and quiet <= now + CLOCK_TOLERANCE_SECONDS and \
                    now - quiet <= WEEK_SECONDS:
                merged['quiet_since'] = quiet
        values = [value for value in (previous_native, _number(native)) if value is not None]
        if values:
            merged['native_last_at'] = max(values)
        common.write_json(common.STATE / DEMAND_FILE, merged)
        return True


def _read_snapshot():
    """Bounded private snapshot read; None when absent, oversized or unreadable."""
    path = common.STATE / DEMAND_FILE
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_SNAPSHOT_BYTES:
            return None
        value = common.read_json(path)
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _ultra_task(task, profiles):
    if task.get('tier') == 'ultra':
        return True
    for key in ('profile', 'requested_profile'):
        name = task.get(key)
        profile = profiles.get(name) if isinstance(name, str) else None
        model = profile.get('model') if isinstance(profile, dict) else None
        if model == routing.KIMI_RESERVED_MODEL:
            return True
    return False


def _demand_metadata(demand):
    """Bounded, secret-free demand metadata for display; counts only when real."""
    if not isinstance(demand, dict):
        return None
    out = {'state': demand.get('state'), 'reason': demand.get('reason')}
    for key in ('queued_ultra', 'active_ultra'):
        value = demand.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            out[key] = value
    for key in ('native_last_at', 'tasks_updated_at', 'quiet_since', 'age_seconds'):
        value = _number(demand.get(key))
        if value is not None:
            out[key] = value
    return out


def _fresh_weekly_sample(value, now):
    """A weekly sample time is required, must not be future, and must be fresh.

    The dynamic release needs an actual authoritative sample time within the same
    15-minute horizon live telemetry uses. A missing, aged or future-dated reading
    is not a release input; base admission keeps its existing weekly rules.
    """
    stamp = _number(value)
    if stamp is None or stamp > now + CLOCK_TOLERANCE_SECONDS:
        return False
    return now - stamp <= WEEKLY_SAMPLE_MAX_AGE_SECONDS


def _valid_identity(value):
    return isinstance(value, str) and 0 < len(value) <= 128 and \
        all(ord(ch) >= 32 and ord(ch) != 127 for ch in value)


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _integer(value):
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 0 <= value <= MAX_COUNT else None


def _round2(value):
    return round(float(value), 2)


def _round4(value):
    return round(float(value), 4)
