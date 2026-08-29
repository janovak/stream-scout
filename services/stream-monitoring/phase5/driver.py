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
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
# `make_twitch` / `top_logins` are Phase 0's, reused rather than rewritten so
# both phases authenticate and rank identically.
sys.path.insert(0, str(HERE.parent / "phase0"))

import redis  # noqa: E402
from common import make_twitch, top_logins  # noqa: E402

from reconciler import (  # noqa: E402
    DESIRED_GENERATION_KEY,
    DESIRED_IDS_KEY,
    DESIRED_KEY,
    Reconciler,
    ReconcilerConfig,
    resolve_reconciler_config,
)
from eventsub_pool import EventSubPoolTransport  # noqa: E402

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
        self.messages = 0
        self.lost_events = 0
        self.pass_sizes = []
        self.transport = None
        self.reconciler = None

    async def _on_message(self, event):
        # Counted and dropped. Nothing reaches Kafka from this process.
        self.messages += 1

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

    async def stop(self, delete_subscriptions=True):
        if self.reconciler is not None:
            self.reconciler.stop()
            self.task.cancel()
            try:
                await self.task
            except (asyncio.CancelledError, Exception):
                pass
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
    last_count, stable_since = -1, None
    while time.monotonic() - t0 < deadline:
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
        await asyncio.sleep(0.5)
    return hit, harness.reconciler.subscription_count, round(time.monotonic() - t0, 2)


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
    hit, final, elapsed = await sample_until(harness, len(desired), RAMP_TIMEOUT_SECONDS, marks)
    record("T041_cold_start", target=len(desired), reached=final,
           seconds_to_plateau=round(elapsed + startup, 2),
           marks={m: round(s + startup, 2) for m, s in hit.items()},
           transport_start_seconds=startup)

    # T044: hold it and watch the count against the desired set.
    deadline = time.monotonic() + SUSTAIN_MINUTES * 60
    samples = []
    while time.monotonic() < deadline:
        await asyncio.sleep(60)
        count = harness.reconciler.subscription_count
        want = harness.client.zcard(DESIRED_KEY)
        samples.append((count, want))
        record("sustain_sample", subscriptions=count, desired=want,
               deviation=round((count - want) / want * 100, 3) if want else None,
               occupancy=harness.transport.occupancy(), messages=harness.messages,
               lost_events=harness.lost_events)

    deviations = [abs(c - w) / w * 100 for c, w in samples if w]
    record("T044_sustain", samples=len(samples), minutes=SUSTAIN_MINUTES,
           subscriptions_min=min(c for c, _ in samples), subscriptions_max=max(c for c, _ in samples),
           desired=samples[0][1] if samples else None,
           deviation_max_pct=round(max(deviations), 3) if deviations else None,
           deviation_mean_pct=round(statistics.mean(deviations), 3) if deviations else None,
           messages_received=harness.messages, lost_events=harness.lost_events)


async def mode_restart(harness, channels):
    """T045: converge, then confirm no broadcaster holds two live subscriptions.

    The kill itself is external (see run_restart below). This mode is what runs
    on the SECOND start, against subscriptions the first process left behind.
    """
    write_desired(harness.client, channels)
    config = resolve_reconciler_config()
    await harness.start(config)
    hit, final, elapsed = await sample_until(harness, len(channels), RAMP_TIMEOUT_SECONDS,
                                             [0.9, 0.99, 1.0])
    per_broadcaster = {}
    async for sub in harness.transport.list():
        per_broadcaster.setdefault(sub.broadcaster_id, []).append(sub.status)
    duplicates = {b: s for b, s in per_broadcaster.items() if len(s) > 1}
    record("T045_convergence", reached=final, seconds=elapsed, marks=hit,
           live_broadcasters=len(per_broadcaster), duplicates=len(duplicates),
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
    full = {login: r for r, (login, _) in enumerate(set_a, 1)}
    ids = {login: int(bid) for login, bid in channels}

    # Warm the connection so the first call does not pay for the TCP setup.
    write(stub, full, ids)

    def commands_processed():
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
    env = dict(l.strip().split("=", 1) for l in open(HERE.parents[2] / ".env") if "=" in l)
    os.environ.setdefault("TWITCH_CLIENT_ID", env["TWITCH_CLIENT_ID"])
    os.environ.setdefault("TWITCH_CLIENT_SECRET", env["TWITCH_CLIENT_SECRET"])

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
    stopping = asyncio.Event()

    def _sigterm(*_):
        stopping.set()

    signal.signal(signal.SIGTERM, _sigterm)
    try:
        if mode == "ramp":
            await mode_ramp(harness, channels)
        elif mode == "restart":
            await mode_restart(harness, channels)
        else:
            raise SystemExit(f"unknown mode {mode}")
    finally:
        await harness.stop(delete_subscriptions=True)
        await twitch.close()


if __name__ == "__main__":
    asyncio.run(main())
