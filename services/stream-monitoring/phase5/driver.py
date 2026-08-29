#!/usr/bin/env python3
"""Phase 5 (spec 004) driver for T041, T042, T044 and T045.

Tracked, unlike the phase0/ harnesses: this produced four of spec 004's
success-criteria results, and evidence nobody can re-run is weak evidence.
Its captured Kafka slices are gitignored; this script is not.

Production runs at JOIN_THRESHOLD 15 / LEAVE_THRESHOLD 30, which is 19-21
channels. The operator decided on 2026-08-28 to leave it there (research.md,
"What is NOT met: the 500-channel tail"), so the scale-dependent success
criteria cannot be measured on the production service.

This driver runs the REAL `Reconciler` and the REAL `EventSubPoolTransport`
against a 500-channel desired set, so the code under measurement is the code
that ships. What it deliberately leaves out is everything downstream of the
socket: no Kafka producer, no Flink, no ClipCreator. Received messages are
counted and dropped. A 500-channel set detects far more anomalies than the
clip budget can act on -- that is spec 005's problem, and pulling it in here
would damage production for a verification number.

What it shares with production, and why that is safe
---------------------------------------------------
`secrets/phase0_tokens.json` is the same Twitch user as production
(48754970). Two consequences:

- Subscriptions do NOT collide. `EventSubPoolTransport.list()` yields only
  subscriptions on a session the pool itself holds, so production's
  reconciler never sees this driver's and never drops them, and this driver
  never sees production's.
- The create rate limit IS shared: it is a per-token burst budget of roughly
  360-420 (T003b). A cold ramp here can therefore 429 a production create
  that happens to land in the same window. Production backs off ~10 s and
  retries, and never drops a channel (D2). The operator accepted this on
  2026-08-29.

Redis
-----
Logical db 15, never the production db. Phase 1 used the same convention to
exercise the seam against a real Redis.

Usage
-----
    python phase5/driver.py ramp     # T041 cold start, then T044 sustain
    python phase5/driver.py flatness # T042 poll flatness at 500
    python phase5/driver.py restart  # T045 convergence after a mid-ramp kill
"""

import asyncio
import json
import os
import signal
import statistics
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

import redis  # noqa: E402
# phase5/common.py, not phase0's: `phase0/` is excluded per-clone through
# .git/info/exclude and is in no clone but this machine's.
from common import load_env, make_twitch, top_logins  # noqa: E402

from reconciler import (  # noqa: E402
    DESIRED_GENERATION_KEY,
    DESIRED_IDS_KEY,
    DESIRED_KEY,
    Reconciler,
    ReconcilerConfig,
    resolve_reconciler_config,
)
from eventsub_pool import (  # noqa: E402
    CHAT_MESSAGE_SUBSCRIPTION_TYPE,
    EventSubPoolTransport,
)
from twitchAPI.type import AuthType  # noqa: E402

TARGET_CHANNELS = int(os.environ.get("PHASE5_CHANNELS", "500"))
SUSTAIN_MINUTES = float(os.environ.get("PHASE5_SUSTAIN_MINUTES", "31"))
RAMP_TIMEOUT_SECONDS = float(os.environ.get("PHASE5_RAMP_TIMEOUT", "300"))
STABLE_SECONDS = float(os.environ.get("PHASE5_STABLE_SECONDS", "20"))
REDIS_DB = int(os.environ.get("PHASE5_REDIS_DB", "15"))
OUT = HERE / os.environ.get("PHASE5_OUT", "results.jsonl")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def record(kind: str, **fields):
    """Append one result line and echo it, so a killed run keeps its evidence."""
    row = {"kind": kind, "at": now(), **fields}
    with OUT.open("a") as fh:
        fh.write(json.dumps(row) + "\n")
    print(f"[{row['at']}] {kind}: " + " ".join(f"{k}={v}" for k, v in fields.items()), flush=True)
    return row


def redis_client():
    return redis.Redis(
        host=os.environ.get("REDIS_HOST", "localhost"),
        port=int(os.environ.get("REDIS_PORT", "6379")),
        db=REDIS_DB,
    )


def write_desired(client, channels):
    """Write the desired set the way the poller does (`_write_desired_set`).

    Same MULTI/EXEC, same DEL + ZADD + HSET + INCR shape. Copied rather than
    imported because importing the service module pulls in the Kafka producer
    and the APScheduler wiring, neither of which this driver wants; the
    flatness mode below calls the real method instead.
    """
    desired = {login: rank for rank, (login, _) in enumerate(channels, start=1)}
    ids = {login: bid for login, bid in channels}
    pipe = client.pipeline()
    pipe.delete(DESIRED_KEY, DESIRED_IDS_KEY)
    # The `if desired` guard is the real method's, and it matters: ZADD with an
    # empty mapping raises DataError, so an empty channel list would die
    # pointing at Redis instead of at channel resolution.
    if desired:
        pipe.zadd(DESIRED_KEY, desired)
        pipe.hset(DESIRED_IDS_KEY, mapping={login: ids[login] for login in desired})
    pipe.incr(DESIRED_GENERATION_KEY)
    pipe.execute()
    return desired


class Harness:
    def __init__(self, twitch, user_id, client):
        self.twitch = twitch
        self.user_id = user_id
        self.client = client
        self._messages = 0
        self._message_lock = threading.Lock()
        self.lost_events = 0
        self.pass_sizes = []
        self.transport = None
        self.reconciler = None
        self.task = None

    async def _on_message(self, event):
        # Counted and dropped. Nothing reaches Kafka from this process.
        #
        # Locked, not a bare `+= 1`: callbacks run on each socket's own loop,
        # on its own thread (eventsub_pool's docstring says the handler must be
        # safe to call from several at once), and a load-add-store across two
        # connections drops increments. The count is a reported figure, so it
        # should not quietly undercount. A lock per message is nothing against
        # the socket work already done to deliver it.
        with self._message_lock:
            self._messages += 1

    @property
    def messages(self) -> int:
        with self._message_lock:
            return self._messages

    def _on_lost(self, lost):
        self.lost_events += 1
        record("subscriptions_lost", lost=lost)

    async def start(self, config: ReconcilerConfig):
        self.transport = EventSubPoolTransport(
            self.twitch,
            self._on_message,
            user_id=self.user_id,
            on_subscriptions_lost=self._on_lost,
        )
        await self.transport.start()
        self.reconciler = Reconciler(
            transport=self.transport,
            redis_client=self.client,
            config=config,
            on_pass_complete=self.pass_sizes.append,
            refusal_store=None,  # no Postgres writes from a measurement harness
        )
        self.task = asyncio.create_task(self.reconciler.run())

    def reconciler_failure(self):
        """The exception that killed the reconciler task, or None.

        `Reconciler.run()` re-raises CancelledError but lets anything else
        propagate into the task, where nothing would ever retrieve it. A dead
        reconciler leaves `subscription_count` frozen, and a frozen count is
        exactly what `sample_until` reads as convergence -- so without this a
        crashed ramp records a smaller number as a clean plateau. This script
        produced the SC-001/002/004/006 evidence; that failure mode is the one
        worth refusing to have.
        """
        if self.task is None or not self.task.done():
            return None
        if self.task.cancelled():
            return None
        return self.task.exception()

    async def stop(self, delete_subscriptions=True):
        if self.reconciler is not None:
            failure = self.reconciler_failure()
            if failure is not None:
                record("reconciler_died", error=repr(failure))
            self.reconciler.stop()
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                record("reconciler_died", error=repr(exc))
        if self.transport is not None and delete_subscriptions:
            await self._delete_everything()
        if self.transport is not None:
            await self.transport.aclose()

    async def _delete_everything(self):
        """Give back every subscription this process created."""
        ids = []
        async for sub in self.transport.list():
            ids.append(sub.subscription_id)
        deleted = 0
        for sub_id in ids:
            try:
                await self.transport.delete(sub_id)
                deleted += 1
            except Exception:
                pass
        record("teardown", listed=len(ids), deleted=deleted)


async def sample_until(harness, target, deadline, marks):
    """Poll the actual set until it stops growing. Returns time-to-mark seconds."""
    t0 = time.monotonic()
    hit = {}
    last_count, stable_since, last_growth = -1, None, 0.0
    while time.monotonic() - t0 < deadline:
        failure = harness.reconciler_failure()
        if failure is not None:
            record("reconciler_died", error=repr(failure),
                   at_count=harness.reconciler.subscription_count)
            raise RuntimeError(f"reconciler died mid-ramp: {failure!r}") from failure
        count = harness.reconciler.subscription_count
        elapsed = time.monotonic() - t0
        for mark in marks:
            need = int(target * mark)
            if mark not in hit and count >= need:
                hit[mark] = round(elapsed, 2)
                record("ramp_mark", fraction=mark, need=need, count=count, seconds=hit[mark])
        if count == last_count:
            if stable_since is None:
                stable_since = elapsed
            # No growth for this long: the ramp has converged as far as
            # refusals allow. Must exceed the 429 backoff (10 s) plus a retry
            # round, or a rate-limited ramp is mistaken for a converged one --
            # which is what truncated the first T045 run at 277 of 500.
            elif elapsed - stable_since > STABLE_SECONDS and count > 0:
                break
        else:
            last_count, stable_since = count, None
            last_growth = elapsed
        await asyncio.sleep(0.5)
    # Convergence is the moment the count STOPPED growing, not that moment plus
    # the settle window. Returning the latter inflates SC-001 by
    # STABLE_SECONDS, and when refusals hold the ramp below `target` the 1.0
    # mark never lands, so the inflated figure would be the only number
    # recorded.
    return (hit,
            harness.reconciler.subscription_count,
            round(last_growth, 2),
            round(time.monotonic() - t0, 2))


async def mode_ramp(harness, channels):
    """T041 cold start, then T044 sustain."""
    desired = write_desired(harness.client, channels)
    record("desired_written", channels=len(desired), redis_db=REDIS_DB)

    config = resolve_reconciler_config()
    record("config", concurrency=config.concurrency, backoff=config.rate_limit_backoff_seconds,
           max_rounds=config.max_retry_rounds)

    # The clock starts before the transport does: SC-001 is time to coverage
    # from a cold process, so socket setup belongs inside it.
    ramp_start = time.monotonic()
    await harness.start(config)
    startup = round(time.monotonic() - ramp_start, 2)
    marks = [0.5, 0.9, 0.95, 0.99, 1.0]
    hit, final, converged, watched = await sample_until(
        harness, len(desired), RAMP_TIMEOUT_SECONDS, marks)
    record("T041_cold_start", target=len(desired), reached=final,
           seconds_to_converge=round(converged + startup, 2),
           marks={m: round(s + startup, 2) for m, s in hit.items()},
           settle_window_seconds=STABLE_SECONDS,
           seconds_watched=round(watched + startup, 2),
           transport_start_seconds=startup)

    # T044: hold it and watch the count against the desired set.
    deadline = time.monotonic() + SUSTAIN_MINUTES * 60
    samples = []
    while time.monotonic() < deadline:
        await asyncio.sleep(60)
        # Same hazard sample_until guards, and worse here: a reconciler that
        # dies mid-sustain freezes subscription_count against a static desired
        # set, so every remaining sample records deviation 0.0% -- the
        # strongest possible SC-004 result, produced by a dead process.
        failure = harness.reconciler_failure()
        if failure is not None:
            record("reconciler_died", error=repr(failure),
                   at_count=harness.reconciler.subscription_count,
                   samples_so_far=len(samples))
            raise RuntimeError(f"reconciler died mid-sustain: {failure!r}") from failure
        count = harness.reconciler.subscription_count
        want = harness.client.zcard(DESIRED_KEY)
        samples.append((count, want))
        record("sustain_sample", subscriptions=count, desired=want,
               deviation=round((count - want) / want * 100, 3) if want else None,
               occupancy=harness.transport.occupancy(), messages=harness.messages,
               lost_events=harness.lost_events)

    if not samples:
        # Under ~1 minute of sustain the loop never takes a sample. Say so
        # rather than dying in min() with the T041 result already recorded.
        record("T044_sustain", samples=0, minutes=SUSTAIN_MINUTES,
               note="no samples taken -- sustain shorter than the 60 s sample interval")
        return
    deviations = [abs(c - w) / w * 100 for c, w in samples if w]
    record("T044_sustain", samples=len(samples), minutes=SUSTAIN_MINUTES,
           subscriptions_min=min(c for c, _ in samples),
           subscriptions_max=max(c for c, _ in samples),
           desired=samples[0][1],
           deviation_max_pct=round(max(deviations), 3) if deviations else None,
           deviation_mean_pct=round(statistics.mean(deviations), 3) if deviations else None,
           messages_received=harness.messages, lost_events=harness.lost_events)


async def mode_restart(harness, channels):
    """T045: converge, then confirm no broadcaster holds two live subscriptions.

    The kill itself is external -- this mode is only the SECOND start. The
    sequence is:

        python phase5/driver.py ramp &          # let it climb
        kill -9 <pid>                           # mid-ramp, no teardown
        python phase5/driver.py restart         # this mode

    It runs against the subscriptions the killed process left behind, which
    sit on a session that died with it.
    """
    write_desired(harness.client, channels)
    config = resolve_reconciler_config()
    await harness.start(config)
    hit, final, elapsed, _watched = await sample_until(
        harness, len(channels), RAMP_TIMEOUT_SECONDS, [0.9, 0.99, 1.0])
    # Walk Twitch's own pages, not transport.list().
    #
    # Be precise about what this proves, because an earlier version of this
    # comment overclaimed. SC-006 asks for "no duplicate subscriptions" after
    # a restart, and there are two readings:
    #
    #   (a) no broadcaster has two subscriptions of any kind. Right after a
    #       SIGKILL this is false by definition -- the dead process's
    #       subscriptions linger until Twitch reaps them -- and it is not the
    #       interesting property, because a subscription on a dead session
    #       delivers nothing to anyone.
    #   (b) no broadcaster has two subscriptions that can actually DELIVER.
    #       That is the leak worth forbidding: two live sockets both feeding
    #       one broadcaster's messages into the pipeline would double-count
    #       and corrupt detection.
    #
    # This checks (b), and (b) is the criterion worth checking -- but note
    # that it therefore CANNOT fail on a cross-restart pair, because the
    # stranded half is by definition not deliverable. What it does catch is
    # the pool creating two live subscriptions for one broadcaster in a single
    # run, which is the routing bug that would actually hurt.
    #
    # `enabled_anywhere` below is reported alongside so the excluded
    # population is visible rather than assumed: it counts broadcasters with
    # two or more ENABLED rows across every session on the token. At the
    # operating point that number is dominated by production, which shares
    # this token and whose ~21 channels are a subset of the top 500 -- so a
    # non-zero value there is expected and is not a failure.
    #
    # target_token=USER is mandatory. The library defaults to APP, and an app
    # token cannot see websocket subscriptions at all -- the call succeeds and
    # returns an empty webhook list, which would make every count below zero.
    #
    # One blind spot worth naming: a pool socket mid-reconnect is absent from
    # _live_session_ids(), so its enabled subscriptions read as foreign for
    # that instant.
    ours = harness.transport._live_session_ids()
    enabled_ours = {}
    enabled_anywhere = {}
    enabled_foreign = 0
    not_enabled = 0
    seen = 0
    result = await harness.twitch.get_eventsub_subscriptions(
        sub_type=CHAT_MESSAGE_SUBSCRIPTION_TYPE, target_token=AuthType.USER
    )
    async for sub in result:
        seen += 1
        broadcaster = (getattr(sub, "condition", None) or {}).get("broadcaster_user_id")
        if broadcaster is None:
            continue
        if sub.status != "enabled":
            # Stranded on a dead session; Twitch reaps these.
            not_enabled += 1
            continue
        enabled_anywhere.setdefault(broadcaster, []).append(sub.id)
        session = (getattr(sub, "transport", None) or {}).get("session_id")
        if session in ours:
            enabled_ours.setdefault(broadcaster, []).append(sub.id)
        else:
            enabled_foreign += 1

    # Two deliverable subscriptions for one broadcaster on our own sessions is
    # the leak SC-006 forbids.
    duplicates = {b: ids for b, ids in enabled_ours.items() if len(ids) > 1}
    multi_anywhere = sum(1 for ids in enabled_anywhere.values() if len(ids) > 1)
    record("T045_convergence", reached=final, seconds=elapsed, marks=hit,
           enabled_on_our_sessions=len(enabled_ours),
           enabled_on_other_sessions=enabled_foreign,  # production's, expected
           not_enabled=not_enabled,  # orphans on the killed session
           broadcasters_multi_enabled_anywhere=multi_anywhere,  # incl. production
           subscriptions_seen=seen,
           duplicates=len(duplicates),
           duplicate_detail=dict(list(duplicates.items())[:5]))


def mode_flatness(client, channels):
    """T042: the poller's write cost must follow set SIZE, not change SIZE.

    Calls the real `_write_desired_set` on a stand-in object carrying only the
    two attributes it touches, so this measures production code and not a
    copy of it.
    """
    from stream_monitoring_service import StreamMonitoringService

    class Stub:
        pass

    stub = Stub()
    stub.redis_client = client
    stub.reconciler = None
    write = StreamMonitoringService._write_desired_set

    # Two DISJOINT sets of TARGET_CHANNELS each. Alternating between them is a
    # complete turnover on every write -- 500 members leave and 500 arrive.
    # Permuting one set's ranks would not do: the membership would be
    # unchanged and the comparison would measure nothing.
    set_a = channels[:TARGET_CHANNELS]
    set_b = channels[TARGET_CHANNELS : TARGET_CHANNELS * 2]
    assert not ({l for l, _ in set_a} & {l for l, _ in set_b}), "sets must be disjoint"
    # Same size, or the "change everything" writes are smaller than the
    # "change nothing" ones and the ratio is biased toward the PASS this
    # criterion is testing for. Helix pagination runs short somewhere past
    # 1000, so this is a real possibility, not a theoretical one.
    assert len(set_b) == len(set_a), (
        f"need {TARGET_CHANNELS * 2} live channels for a fair comparison, "
        f"got {len(channels)}"
    )
    full = {login: r for r, (login, _) in enumerate(set_a, 1)}
    ids = {login: int(bid) for login, bid in channels}

    # Warm the connection so the first call does not pay for the TCP setup.
    write(stub, full, ids)

    def commands_processed():
        # Server-wide, not per-database. Sound here because this is the local
        # Redis container, which nothing else uses -- production is a separate
        # instance (REDIS_URL points at the Tailscale host). Any other client
        # on this server would be attributed to the harness, so check
        # `INFO clients` before trusting this number elsewhere.
        return client.info("stats")["total_commands_processed"]

    def timed(desired, label, repeats=15):
        durations = []
        before = commands_processed()
        for _ in range(repeats):
            t0 = time.perf_counter()
            write(stub, desired, ids)
            durations.append((time.perf_counter() - t0) * 1000)
        # One INFO per call is inside this delta; subtract it so the number is
        # the write's own command count.
        used = commands_processed() - before - 1
        return {
            "label": label,
            "members": len(desired),
            "p50_ms": round(statistics.median(durations), 3),
            "max_ms": round(max(durations), 3),
            "redis_commands_per_write": round(used / repeats, 2),
        }

    # Same 500 members every time: the change is nothing.
    unchanged = timed(full, "change_nothing")
    # Alternating disjoint sets: every member changes on every write.
    swapped_a = full
    swapped_b = {login: r for r, (login, _) in enumerate(set_b, 1)}
    durations = []
    before = commands_processed()
    for i in range(15):
        target = swapped_a if i % 2 else swapped_b
        t0 = time.perf_counter()
        write(stub, target, ids)
        durations.append((time.perf_counter() - t0) * 1000)
    used = commands_processed() - before - 1
    changed = {
        "label": "change_everything",
        "members": len(swapped_a),
        "p50_ms": round(statistics.median(durations), 3),
        "max_ms": round(max(durations), 3),
        "redis_commands_per_write": round(used / 15, 2),
    }
    record("T042_poll_flatness", channels=len(channels), unchanged=unchanged, changed=changed,
           ratio=round(changed["p50_ms"] / unchanged["p50_ms"], 3))


async def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "ramp"
    load_env()

    client = redis_client()
    twitch, user_id = await make_twitch()
    # Flatness needs two disjoint 500-channel sets to swap between.
    wanted = TARGET_CHANNELS * 2 if mode == "flatness" else TARGET_CHANNELS
    channels = await top_logins(twitch, wanted)
    record("channels_resolved", requested=wanted, resolved=len(channels), mode=mode)

    if mode == "flatness":
        mode_flatness(client, channels)
        await twitch.close()
        return

    harness = Harness(twitch, user_id, client)

    # SIGTERM must reach the `finally` below, because that is what hands ~500
    # subscriptions back. Cancelling the work task does; a handler that only
    # sets a flag does NOT -- it replaces the default terminate behaviour, so
    # the process ignores SIGTERM outright and a `kill` or `docker stop`
    # mid-sustain leaks every subscription until Twitch reaps the session.
    # SIGKILL still bypasses all of this, which is what T045 relies on.
    work = asyncio.current_task()

    def _request_stop(*_):
        record("signal_received", action="cancelling and tearing down")
        work.cancel()

    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)
    try:
        if mode == "ramp":
            await mode_ramp(harness, channels)
        elif mode == "restart":
            await mode_restart(harness, channels)
        else:
            raise SystemExit(f"unknown mode {mode}")
    except asyncio.CancelledError:
        record("cancelled", note="tearing down before exit")
    finally:
        # Teardown must survive the cancellation that got us here, AND a second
        # one. `await shield(t)` protects the inner task but re-raises in the
        # awaiter as soon as another cancel arrives -- which would skip
        # twitch.close() and let asyncio.run() kill the half-finished delete
        # loop, leaking exactly what this exists to clean up. So absorb the
        # cancellations and keep waiting on the inner task.
        teardown = asyncio.ensure_future(harness.stop(delete_subscriptions=True))
        while True:
            try:
                await asyncio.shield(teardown)
                break
            except asyncio.CancelledError:
                record("cancel_during_teardown", note="ignored, teardown continues")
            except Exception as exc:
                # A network blip mid-teardown must not skip twitch.close(), and
                # must not replace whatever the run itself raised -- that is
                # the message saying why the run failed.
                record("teardown_failed", error=repr(exc))
                break
        await twitch.close()


if __name__ == "__main__":
    asyncio.run(main())
