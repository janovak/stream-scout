#!/usr/bin/env python3
"""
Subscription reconciler -- makes the live subscription set match the poller's
intent.

The poller decides WHAT to watch. The reconciler makes it so. The two only
share Redis, and they never wait for each other.

Why the split
-------------
Chat membership used to happen inside the poll job. A cold start had to join
every channel one at a time, inside the tick, against a rate limit that blocks
instead of failing. The poll then ran longer than POLL_INTERVAL_SECONDS,
APScheduler (max_instances=1) skipped the next poll, the Redis online keys
expired at 180 s, and the service churned lifecycle events while the join storm
was still going.

The poll now writes intent and returns. This module owns all network fan-out
and does it in parallel. The cost of a poll no longer depends on how much the
desired set changed (FR-003).

Redis layout (data-model.md)
----------------------------
| Key                     | Type       | Written by | Read by     |
|-------------------------|------------|------------|-------------|
| `chat:desired`          | Sorted set | Poller     | Reconciler  |
| `chat:desired:ids`      | Hash       | Poller     | Reconciler  |
| `chat:desired:generation` | String   | Poller     | Reconciler  |

- `chat:desired`  -- member = broadcaster login, score = rank, 1 = top. The
  score order lets the reconciler work highest rank first, so the most-watched
  channels come up first during a cold start.
- `chat:desired:ids` -- login -> broadcaster id. EventSub subscribes by
  broadcaster id, but the poller ranks by login, so the map must cross the
  seam. It is written in the same transaction as `chat:desired`, so the two
  can never disagree.
- `chat:desired:generation` -- a counter. The poller increments it each time it
  writes a new desired set. The reconciler uses it to notice that its own view
  went stale while a pass was running.

The poller writes all three in one MULTI/EXEC. Its cost scales with the SIZE of
the desired set, not with the size of the CHANGE, which is what FR-003 asks
for.

Note on the write shape: data-model.md describes the write as one `ZADD` plus a
`ZREMRANGEBYRANK` trim. A rank trim cannot do the job. A member that leaves the
desired set keeps its old score, which is also a low rank, so the trim would
keep the stale member and evict a wanted one instead. The poller does
`DEL` + `ZADD` inside a MULTI, which is still one round trip and is exact. A
reader never sees the empty window, because Redis runs the transaction whole.

Refusals are durable
--------------------
About 1.5% of channels answer a create with `subscription missing proper
authorization`. Retrying one every pass wastes a POST per channel per pass
forever, and skipping it in memory forgets on restart. The refusal is written
to `streamers.eventsub_refused_at` instead, and a mark older than
`REFUSAL_RECHECK_DAYS` is retried once: success clears it, a fresh refusal
resets it (D5, FR-007). `allows_clipping` gets the same self-heal on the
poller side, through `clipping_disabled_at`.

The transport seam
------------------
The reconciler never talks to Twitch. It talks to a `SubscriptionTransport`.
Phase 1 ships `StubTransport`, an in-memory implementation. Phase 2 replaces it
with the real EventSub connection pool (`eventsub_pool.py`) and changes nothing
here. Per-channel routing and per-connection occupancy live behind the
interface, because they are transport concerns.
"""

import asyncio
import logging
import os
import random
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import AsyncIterator, Callable, Dict, Iterable, List, Optional, Set

from prometheus_client import Counter, Gauge, Histogram

logger = logging.getLogger("stream_monitoring")

# Redis keys. Everything that crosses the poller/reconciler seam is here.
DESIRED_KEY = "chat:desired"
DESIRED_IDS_KEY = "chat:desired:ids"
DESIRED_GENERATION_KEY = "chat:desired:generation"

# Only an `enabled` subscription counts as live. Twitch keeps dead ones around
# with statuses such as `authorization_revoked` and `websocket_disconnected`,
# and it collects them itself later. A dead subscription for a channel we still
# want must be treated as absent, so that the next pass creates it again
# (T014). A dead subscription for a channel we no longer want is ignored: it is
# not in the actual set, so it is never in the drop set either.
ADOPTABLE_STATUSES = frozenset({"enabled"})

# How long a refusal stands before the channel is worth one more try (D5).
# The same interval governs `clipping_disabled_at` in the poller, so a channel
# that fixes its settings comes back on both paths at the same rate.
REFUSAL_RECHECK_DAYS = 7

# Prometheus metrics (FR-012). These live here, not in the service module,
# because the reconciler owns the values. The service starts the HTTP server
# and both modules share the default registry.
eventsub_subscription_count = Gauge(
    "eventsub_subscription_count",
    "Live chat subscriptions the reconciler currently holds",
)
reconcile_duration_seconds = Histogram(
    "reconcile_duration_seconds",
    "Wall-clock duration of one reconcile pass",
    # The default buckets stop at 10 s. A cold start to 500 channels takes
    # about 51 s at the default concurrency (measured, research T041), so the
    # interesting range would all land in +Inf. These buckets cover a converged
    # pass (milliseconds) through the 120 s SC-001 ceiling.
    buckets=(0.005, 0.05, 0.5, 1, 2, 5, 10, 20, 30, 60, 120, float("inf")),
)
subscription_create_failures_total = Counter(
    "subscription_create_failures_total",
    "Subscription creations that failed, by reason",
    ["reason"],
)
eventsub_connection_occupancy = Gauge(
    "eventsub_connection_occupancy",
    "Subscriptions held per transport connection",
    ["connection"],
)
reconcile_last_success_timestamp = Gauge(
    "reconcile_last_success_timestamp",
    "Unix time of the last reconcile pass that ran to completion",
)


def resolve_reconciler_config(env=None):
    """Read and validate the reconciler settings from `env`.

    A function, not module-level code, so that tests can exercise the
    validation. The module cannot be reloaded: the Prometheus collectors above
    refuse a second registration in one process. This follows
    `resolve_thresholds` in the service module.

    Returns a `ReconcilerConfig`. Raises ValueError on a setting that would
    misbehave quietly at run time.
    """
    env = os.environ if env is None else env
    concurrency = int(env.get("RECONCILE_CONCURRENCY", "10"))
    idle_timeout = float(env.get("RECONCILE_IDLE_TIMEOUT_SECONDS", "5"))
    backoff = float(env.get("RECONCILE_RATE_LIMIT_BACKOFF_SECONDS", "10"))
    max_rounds = int(env.get("RECONCILE_MAX_RETRY_ROUNDS", "20"))
    readopt_interval = float(env.get("RECONCILE_READOPT_INTERVAL_SECONDS", "300"))
    adopt_retry = float(env.get("RECONCILE_ADOPT_RETRY_SECONDS", "30"))

    if concurrency < 1:
        raise ValueError(f"RECONCILE_CONCURRENCY must be >= 1, got {concurrency}")
    if idle_timeout <= 0:
        raise ValueError(f"RECONCILE_IDLE_TIMEOUT_SECONDS must be > 0, got {idle_timeout}")
    if backoff < 0:
        raise ValueError(f"RECONCILE_RATE_LIMIT_BACKOFF_SECONDS must be >= 0, got {backoff}")
    if max_rounds < 1:
        raise ValueError(f"RECONCILE_MAX_RETRY_ROUNDS must be >= 1, got {max_rounds}")
    if readopt_interval <= 0:
        raise ValueError(
            f"RECONCILE_READOPT_INTERVAL_SECONDS must be > 0, got {readopt_interval}"
        )
    if adopt_retry < 0:
        raise ValueError(
            f"RECONCILE_ADOPT_RETRY_SECONDS must be >= 0, got {adopt_retry}"
        )

    return ReconcilerConfig(
        concurrency=concurrency,
        idle_timeout_seconds=idle_timeout,
        rate_limit_backoff_seconds=backoff,
        max_retry_rounds=max_rounds,
        readopt_interval_seconds=readopt_interval,
        adopt_retry_seconds=adopt_retry,
    )


@dataclass(frozen=True)
class ReconcilerConfig:
    """Tuning for one reconciler.

    `concurrency` defaults to 10. Phase 0 T003 measured zero 429s at
    concurrency 1, 5, 10 and 20 for 250 creates, so the rate limit is a
    per-token burst budget and not a concurrency ceiling. 10 already meets
    SC-001 with margin. A higher value saves about 10 s at cold start and
    leaves more in-flight work to unwind after a restart (research D2).
    """

    concurrency: int = 10
    idle_timeout_seconds: float = 5.0
    rate_limit_backoff_seconds: float = 10.0
    max_retry_rounds: int = 20
    # How long the in-memory actual set may go without being checked against
    # Twitch. Adoption used to run once and then never again unless a socket
    # died or a revocation arrived -- both of which the pool has to observe
    # first. A subscription lost by any route the pool CANNOT observe was
    # therefore permanent, and `eventsub_subscription_count` went on reporting
    # it, so the FR-012 alert could not fire. The known such route is the
    # library's `_resubscribe()` failing part way through a reconnect: it
    # swallows the exception and only restores the old map if nothing at all
    # was re-created, so the channels past the failure point simply do not
    # exist on Twitch any more. One listing every few minutes is cheap
    # insurance against that whole class.
    readopt_interval_seconds: float = 300.0
    # How long to wait before retrying an enumeration that FAILED. Distinct
    # from the interval above, and from the immediate retry a socket loss
    # deserves: a walk that raised will probably raise again in 5 s, and
    # retrying it every pass means a full multi-page Helix enumeration ~12
    # times a minute on the token the clip job shares.
    adopt_retry_seconds: float = 30.0


class TransportError(Exception):
    """A transport operation failed. The channel keeps its place in the set."""


class RateLimitedError(TransportError):
    """Twitch refused the request for now (HTTP 429).

    Phase 0 T003b showed that the burst budget is about 360-420 creates per
    token, and that the first 429 lands after roughly 364 successful creates.
    Past about 400 channels the retry loop is what makes a cold start converge,
    so it is load-bearing and not a safety net.
    """

    def __init__(self, message="rate limited", retry_after: Optional[float] = None):
        super().__init__(message)
        self.retry_after = retry_after


class SubscriptionRefusedError(TransportError):
    """The broadcaster does not permit the subscription (HTTP 403).

    About 1.5% of channels refuse. Retrying one inside the same pass only
    wastes requests, so the reconciler counts it and moves on. Phase 2 (T025)
    makes the refusal durable in `streamers.eventsub_refused_at`.
    """


@dataclass(frozen=True)
class ExistingSubscription:
    """One subscription that already exists, as the transport reports it."""

    subscription_id: str
    broadcaster_id: int
    status: str = "enabled"


class SubscriptionTransport(ABC):
    """What the reconciler needs from a chat transport, and nothing more.

    Phase 1 runs against `StubTransport`. Phase 2 supplies the EventSub
    websocket pool. Connection routing, per-connection occupancy and the 300
    subscriptions-per-socket cap all stay behind this interface, because the
    reconciler must not know how many sockets there are.
    """

    @abstractmethod
    async def create(self, broadcaster_id: int) -> str:
        """Subscribe to `broadcaster_id` and return the subscription id.

        The call MUST be idempotent. If the subscription already exists, return
        the id of the existing one rather than making a second. Twitch answers
        a duplicate create with 409 Conflict, and the implementation adopts
        that subscription instead of treating it as an error. This keeps
        "never duplicate" (FR-005) true even when the reconciler has an
        incomplete view of what exists.

        Raises `RateLimitedError` on 429 and `SubscriptionRefusedError` on a
        403 authorization refusal.
        """

    @abstractmethod
    async def delete(self, subscription_id: str) -> None:
        """Remove a subscription.

        A subscription that is already gone is NOT an error. After a socket
        closes, its subscriptions linger with status `websocket_disconnected`
        and a DELETE against them answers "not found". Twitch collects them
        itself. The implementation logs that case at debug and returns.
        """

    @abstractmethod
    def list(self) -> AsyncIterator[ExistingSubscription]:
        """Yield every subscription that currently exists.

        This is an async iterator, not a coroutine that returns a list, for
        two reasons.

        1. The implementation MUST count pages instead of reading `total`. The
           Phase 0 spike saw `get_eventsub_subscriptions().total` report 300
           while the pages held 396 (research D6).
        2. Enumeration can fail part way. An iterator lets the reconciler keep
           the subscriptions it already saw and mark its view incomplete
           (NFR-003). A coroutine that raises would lose all of them.
        """

    def occupancy(self) -> Dict[str, int]:
        """Report subscriptions held per connection, for the FR-012 gauge.

        The default suits a transport with one connection. The Phase 2 pool
        overrides it with real per-socket counts.
        """
        return {}


class StubTransport(SubscriptionTransport):
    """An in-memory transport for Phase 1 and for tests.

    It can imitate the two behaviours that shape the reconciler: latency, and
    a burst budget that answers 429 once it runs out.
    """

    def __init__(
        self,
        *,
        latency_seconds: float = 0.0,
        burst_budget: Optional[int] = None,
        budget_refill_seconds: float = 0.0,
        list_fails_after: Optional[int] = None,
        refuse: Optional[Set[int]] = None,
        connections: int = 1,
    ):
        self.subscriptions: Dict[int, str] = {}
        self.statuses: Dict[int, str] = {}
        self.latency_seconds = latency_seconds
        self.burst_budget = burst_budget
        self.budget_refill_seconds = budget_refill_seconds
        self.list_fails_after = list_fails_after
        self.refuse = set(refuse or ())
        self.connections = max(1, connections)
        self.create_calls: List[int] = []
        self.delete_calls: List[str] = []
        self.list_calls: List[int] = []
        self._created_in_burst = 0
        self._budget_spent_at: Optional[float] = None
        self._next_id = 0

    def _check_budget(self):
        """Imitate the measured burst-then-throttle shape (T003b)."""
        if self.burst_budget is None:
            return
        now = time.monotonic()
        if (
            self._budget_spent_at is not None
            and now - self._budget_spent_at >= self.budget_refill_seconds
        ):
            self._created_in_burst = 0
            self._budget_spent_at = None
        if self._created_in_burst >= self.burst_budget:
            self._budget_spent_at = now
            raise RateLimitedError(retry_after=self.budget_refill_seconds or None)
        self._created_in_burst += 1

    async def create(self, broadcaster_id: int) -> str:
        if self.latency_seconds:
            await asyncio.sleep(self.latency_seconds)
        self.create_calls.append(broadcaster_id)
        if broadcaster_id in self.refuse:
            raise SubscriptionRefusedError("subscription missing proper authorization")
        self._check_budget()
        # Idempotent, as the interface requires: a repeat create adopts the
        # subscription that is already there. Only a LIVE one, though. A
        # revoked or disconnected entry is dead, and Twitch lets a new
        # subscription take its place, so the stub replaces it.
        existing = self.subscriptions.get(broadcaster_id)
        if existing is not None and self.statuses.get(broadcaster_id) in ADOPTABLE_STATUSES:
            return existing
        self._next_id += 1
        subscription_id = f"sub-{self._next_id}"
        self.subscriptions[broadcaster_id] = subscription_id
        self.statuses[broadcaster_id] = "enabled"
        return subscription_id

    async def delete(self, subscription_id: str) -> None:
        if self.latency_seconds:
            await asyncio.sleep(self.latency_seconds)
        self.delete_calls.append(subscription_id)
        for broadcaster_id, held in list(self.subscriptions.items()):
            if held == subscription_id:
                del self.subscriptions[broadcaster_id]
                self.statuses.pop(broadcaster_id, None)
                return
        # Already gone. Not an error.

    async def list(self) -> AsyncIterator[ExistingSubscription]:
        self.list_calls.append(len(self.subscriptions))
        for index, (broadcaster_id, subscription_id) in enumerate(list(self.subscriptions.items())):
            if self.list_fails_after is not None and index >= self.list_fails_after:
                raise TransportError("simulated pagination failure")
            yield ExistingSubscription(
                subscription_id=subscription_id,
                broadcaster_id=broadcaster_id,
                status=self.statuses.get(broadcaster_id, "enabled"),
            )

    def revoke(self, broadcaster_id: int):
        """Mark a subscription revoked, the way Twitch does. Test helper."""
        if broadcaster_id in self.statuses:
            self.statuses[broadcaster_id] = "authorization_revoked"

    def occupancy(self) -> Dict[str, int]:
        counts = {str(index): 0 for index in range(self.connections)}
        for broadcaster_id in self.subscriptions:
            counts[str(broadcaster_id % self.connections)] += 1
        return counts


class RefusalStore(ABC):
    """Where a `subscription missing proper authorization` is remembered.

    Kept behind an interface for the same reason `SubscriptionTransport` is:
    the reconciler owns the 7-day rule, not the storage. `PostgresRefusalStore`
    is the real one; tests use a fake.
    """

    @abstractmethod
    def refusals(self, broadcaster_ids: List[int]) -> Dict[int, bool]:
        """Of these ids, the ones carrying a refusal, and whether it is stale.

        Maps broadcaster id -> `True` when the mark is older than
        `REFUSAL_RECHECK_DAYS` and the channel is therefore due its one retry,
        `False` when the mark still stands and the channel is skipped. Ids
        with no mark are absent. One call per pass, never one per channel.
        """

    @abstractmethod
    def mark_refused(self, broadcaster_id: int) -> None:
        """Record a refusal at the current time, replacing any older mark."""

    @abstractmethod
    def clear_refusal(self, broadcaster_id: int) -> None:
        """Forget a refusal, because the channel has just accepted one."""


class PostgresRefusalStore(RefusalStore):
    """`streamers.eventsub_refused_at`, through the service's connection pool.

    The calls are synchronous, like the reconciler's Redis calls and the
    poller's own Postgres calls. There is at most one query per pass for the
    read; the two writes happen only on a refusal or on a channel healing,
    which is rare by construction.

    The WRITES swallow their own errors: a refusal that fails to record is
    re-learned next pass, which is harmless. The READ does not, and must not.
    Returning `{}` for a failed read is not a degraded answer, it is a wrong
    one -- indistinguishable from "no channel is refused" -- and it silently
    disabled the caller's own handling: `_drop_refused` has an `except` branch
    and an explicit "Refusal cache unavailable" log for exactly this, and both
    were unreachable for the real store. The visible cost was every refused
    channel being retried every pass, one POST each, with nothing in the log
    to say why.
    """

    def __init__(self, db_pool, recheck_days: int = REFUSAL_RECHECK_DAYS):
        self.db_pool = db_pool
        self.recheck_days = recheck_days

    def refusals(self, broadcaster_ids: List[int]) -> Dict[int, bool]:
        if not broadcaster_ids:
            return {}
        rows = self._run(
            "read",
            reraise=True,
            work=lambda cur: cur.execute(
                "SELECT streamer_id, "
                "       eventsub_refused_at < NOW() - make_interval(days => %s) AS stale "
                "FROM streamers "
                "WHERE streamer_id = ANY(%s) AND eventsub_refused_at IS NOT NULL",
                (self.recheck_days, list(broadcaster_ids)),
            ),
            fetch=True,
        )
        return {} if rows is None else {row[0]: bool(row[1]) for row in rows}


    def mark_refused(self, broadcaster_id: int) -> None:
        self._run(
            "mark",
            lambda cur: cur.execute(
                "UPDATE streamers SET eventsub_refused_at = NOW() WHERE streamer_id = %s",
                (broadcaster_id,),
            ),
        )

    def clear_refusal(self, broadcaster_id: int) -> None:
        self._run(
            "clear",
            lambda cur: cur.execute(
                "UPDATE streamers SET eventsub_refused_at = NULL WHERE streamer_id = %s",
                (broadcaster_id,),
            ),
        )

    def _run(self, operation: str, work, fetch: bool = False, reraise: bool = False):
        conn = None
        try:
            conn = self.db_pool.getconn()
            with conn.cursor() as cur:
                work(cur)
                result = list(cur.fetchall()) if fetch else None
            conn.commit()
            return result
        except Exception as e:
            logger.error(
                "Refusal store operation failed",
                extra={"operation": operation, "error": str(e), "error_type": type(e).__name__},
            )
            if conn is not None:
                try:
                    conn.rollback()
                except Exception:
                    pass
            if reraise:
                raise
            return None
        finally:
            if conn is not None:
                self.db_pool.putconn(conn)


@dataclass(frozen=True)
class DesiredSet:
    """One read of the poller's intent."""

    logins: List[str] = field(default_factory=list)  # rank order, best first
    ids: Dict[str, int] = field(default_factory=dict)
    generation: int = 0

    def broadcaster_ids(self) -> List[int]:
        """Broadcaster ids in rank order, skipping any login with no id."""
        return [self.ids[login] for login in self.logins if login in self.ids]


class Reconciler:
    """Drives the live subscription set toward `chat:desired`.

    It runs as an asyncio task inside the stream-monitoring process, next to
    the APScheduler poll job. It is not a separate container: it shares the
    process `/health` endpoint and the same jsonlogger path.

    The loop wakes when the poller bumps `chat:desired:generation`, or after
    `idle_timeout_seconds`, whichever comes first. One pass never overlaps the
    next.
    """

    def __init__(
        self,
        transport: SubscriptionTransport,
        redis_client,
        config: Optional[ReconcilerConfig] = None,
        on_pass_complete: Optional[Callable[[int], None]] = None,
        refusal_store: Optional[RefusalStore] = None,
    ):
        self.transport = transport
        self.redis_client = redis_client
        self.config = config or resolve_reconciler_config()
        self.on_pass_complete = on_pass_complete
        # Optional: without it every channel is attempted every pass, which is
        # what Phase 1 did. With it, FR-007's skip and 7-day re-check apply.
        self.refusal_store = refusal_store
        self.running = True

        # broadcaster id -> subscription id. This is the actual set. It is
        # rebuilt from the transport at start-up and kept in memory after that.
        self._actual: Dict[int, str] = {}
        # False until one enumeration finishes. While it is False the
        # reconciler knows its view of the world has holes.
        self._adoption_complete = False
        # Bumped by every `invalidate_actual_set()`. `_adopt` reads it before
        # and after its enumeration so a loss that lands mid-walk is not
        # thrown away by the completion that follows it.
        self._invalidations = 0
        # When the actual set was last rebuilt from Twitch, for the periodic
        # re-check. -inf so the first pass always adopts.
        self._last_adopt = float("-inf")
        # Set when an enumeration FAILS, to hold off the retry. Separate from
        # `_last_adopt`, which paces the healthy periodic re-check.
        self._adopt_retry_after = float("-inf")
        # The live desired view. Workers read it, so that a channel which
        # leaves the set part way through a pass is not created (T014).
        self._desired_ids: Set[int] = set()
        self._pass_generation = -1
        self._wake = asyncio.Event()
        # A SEPARATE event from `_wake`, deliberately. They mean different
        # things -- "the poller wrote a new set" and "a socket died" -- and
        # sharing one made each fix for the other break something: clearing it
        # in the mid-pass refresh swallowed a socket loss, and re-setting it
        # there left it set for the rest of the pass, so every remaining
        # channel re-read Redis. Two events, no interaction.
        self._invalidated = asyncio.Event()
        self._refresh_lock = asyncio.Lock()
        # Channels whose refusal has gone stale and that this pass is giving
        # one more try. A create that succeeds for one of these clears the
        # mark; a create that refuses again resets it (D5).
        self._rechecking_refusals: Set[int] = set()

    # -- public surface ---------------------------------------------------

    @property
    def subscription_count(self) -> int:
        return len(self._actual)

    def invalidate_actual_set(self):
        """Rebuild the actual set from the transport on the next pass.

        The transport calls this when it loses a connection (T023). Everything
        that socket held is gone, but only the transport can know that. This
        does not repair anything itself: the next pass re-enumerates, the lost
        subscriptions are simply absent, `eventsub_subscription_count` drops --
        which is the alert path (FR-012) -- and the ordinary diff re-creates
        those channels on a surviving or new connection.

        Drops stay switched off until an enumeration succeeds, so a failure to
        re-enumerate cannot turn into a mass delete.
        """
        self._adoption_complete = False
        self._invalidations += 1
        # Clear any failed-enumeration backoff. Round 7 added that backoff and
        # claimed in a comment that a socket loss "does not come through here"
        # -- it does: the gate in `reconcile_once` covers every adoption path.
        # A failed walk and a dead socket are positively correlated (one blip
        # causes both), so the pairing is likely, and it left up to 300
        # channels dark for the whole backoff: `_actual` still held the dead
        # socket's ids, so they never entered `to_create`. A loss earns one
        # immediate attempt; if THAT walk fails, `_adopt` sets the backoff
        # again, so this cannot become the hot loop the backoff prevents.
        self._adopt_retry_after = float("-inf")
        self._invalidated.set()

    def notify_desired_changed(self):
        """Tell the loop that the poller wrote a new desired set.

        The poller and the reconciler share one event loop, so this is the
        cheap path. The idle timeout is the backstop: even if this signal is
        never sent, a pass still runs within `idle_timeout_seconds` and reads
        the generation from Redis.
        """
        self._wake.set()

    async def run(self):
        """Reconcile until stopped. One pass never overlaps the next."""
        logger.info(
            "Reconciler started",
            extra={
                "concurrency": self.config.concurrency,
                "idle_timeout_seconds": self.config.idle_timeout_seconds,
                "transport": type(self.transport).__name__,
            },
        )
        try:
            while self.running:
                try:
                    await self.reconcile_once()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # `reconcile_once` guards the paths it expects to fail, so
                    # reaching here means something unforeseen. Ending the task
                    # would be silent: `self._reconciler_task` holds a live
                    # reference, so asyncio's "Task exception was never
                    # retrieved" handler never runs and no traceback is ever
                    # printed. The service would go on polling and writing
                    # `chat:desired` while no subscription was created or
                    # dropped again, with a frozen
                    # `reconcile_last_success_timestamp` as the only symptom.
                    logger.exception("Reconcile pass raised, continuing")
                if not self.running:
                    # `stop()` signals through `_wake`, and
                    # `_maybe_refresh_desired` clears that event for its own
                    # purpose -- so the signal can be gone by the time the pass
                    # ends. Re-check the flag directly rather than waiting on
                    # an event that may already have been consumed, or shutdown
                    # sits here for the whole idle timeout and the service's
                    # bounded "ask first" wait always times out instead.
                    break
                await self._wait_for_work()
        except asyncio.CancelledError:
            logger.info("Reconciler cancelled")
            raise
        finally:
            logger.info("Reconciler stopped")

    def stop(self):
        self.running = False
        self._wake.set()

    async def reconcile_once(self):
        """Run one pass: read the intent, diff it, and act on the difference."""
        started = time.monotonic()
        try:
            if (
                not self._adoption_complete or self._readopt_due()
            ) and time.monotonic() >= self._adopt_retry_after:
                await self._adopt()

            desired = self._read_desired()
            self._pass_generation = desired.generation
            self._desired_ids = set(desired.broadcaster_ids())
        except Exception as e:
            # NFR-002. A Redis fault must not take the loop down and must not
            # drop a live subscription. Nothing has been deleted at this point,
            # so returning early leaves the world as it was.
            logger.error(
                "Reconcile pass skipped, could not read the desired set",
                extra={"error": str(e), "error_type": type(e).__name__},
            )
            return

        missing_ids = [login for login in desired.logins if login not in desired.ids]
        if missing_ids:
            # The poller writes the set and the id map in one transaction, so
            # this should not happen. Skip those logins rather than guess.
            logger.warning(
                "Desired logins have no broadcaster id, skipping them",
                extra={"count": len(missing_ids), "sample": missing_ids[:5]},
            )

        to_create = [bid for bid in desired.broadcaster_ids() if bid not in self._actual]
        to_create = self._drop_refused(to_create)
        to_drop = [bid for bid in self._actual if bid not in self._desired_ids]

        if not self._adoption_complete and to_drop:
            # NFR-003. The view has holes, so an "extra" subscription may only
            # look extra. Never delete on an incomplete picture.
            logger.warning(
                "Enumeration is incomplete, holding back drops until it succeeds",
                extra={"would_drop": len(to_drop)},
            )
            to_drop = []

        if to_create or to_drop:
            logger.info(
                "Reconciling",
                extra={
                    "desired": len(self._desired_ids),
                    "actual": len(self._actual),
                    "to_create": len(to_create),
                    "to_drop": len(to_drop),
                    "generation": desired.generation,
                },
            )

        await self._drop_all(to_drop)
        await self._create_all(to_create)

        eventsub_subscription_count.set(len(self._actual))
        self._publish_occupancy()
        reconcile_duration_seconds.observe(time.monotonic() - started)
        # "Ran to completion", not "had no failures". A pass where individual
        # creates refused or errored still reached here, and that is on
        # purpose: at 500 channels one broadcaster refuses on every pass, so a
        # no-failures gate would hold this gauge still for ever and destroy the
        # signal it exists for. What it detects is a reconciler that has
        # stopped completing passes at all, while the poller keeps working.
        # Per-channel failures are `subscription_create_failures_total`.
        reconcile_last_success_timestamp.set(time.time())
        if self.on_pass_complete is not None:
            self.on_pass_complete(len(self._actual))

        self._wake_if_generation_moved()

    # -- the loop ---------------------------------------------------------

    async def _wait_for_work(self):
        """Wait for a generation bump, or for the idle timeout."""
        waiters = [
            asyncio.ensure_future(self._wake.wait()),
            asyncio.ensure_future(self._invalidated.wait()),
        ]
        try:
            await asyncio.wait(
                waiters,
                timeout=self.config.idle_timeout_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            for waiter in waiters:
                waiter.cancel()
            self._wake.clear()
            self._invalidated.clear()

    def _readopt_due(self) -> bool:
        """True when the in-memory actual set is due a check against Twitch."""
        return (
            time.monotonic() - self._last_adopt
            >= self.config.readopt_interval_seconds
        )

    def _wake_if_generation_moved(self):
        """Run again at once if the poller wrote a new set during this pass."""
        try:
            current = self._read_generation()
        except Exception as e:
            logger.warning("Could not re-read the generation", extra={"error": str(e)})
            return
        if current != self._pass_generation:
            self._wake.set()

    # -- Redis ------------------------------------------------------------
    #
    # These calls are synchronous, which matches how the poller already uses
    # Redis in this service. There are three of them per pass and a ZRANGE of
    # 500 members costs well under a millisecond, so the event loop does not
    # notice. Do not add per-channel Redis calls here.

    def _read_desired(self) -> DesiredSet:
        ranked = self.redis_client.zrange(DESIRED_KEY, 0, -1)
        ids = self.redis_client.hgetall(DESIRED_IDS_KEY) or {}
        # Per entry, not a comprehension that raises. One unparseable value
        # used to take out `_read_desired` entirely, so `reconcile_once` hit
        # its "could not read the desired set" early return and created and
        # dropped nothing at all until the next poll rewrote the hash. The
        # poller's own reader of this key already skips bad entries; a single
        # bad member should cost that member, not the pass.
        parsed: Dict[str, int] = {}
        for login, broadcaster_id in ids.items():
            try:
                parsed[self._as_text(login)] = int(broadcaster_id)
            except (TypeError, ValueError):
                logger.warning(
                    "Unparseable broadcaster id in the desired-set map, skipping it",
                    extra={"login": self._as_text(login)},
                )
        return DesiredSet(
            logins=[self._as_text(login) for login in ranked],
            ids=parsed,
            generation=self._read_generation(),
        )

    def _read_generation(self) -> int:
        raw = self.redis_client.get(DESIRED_GENERATION_KEY)
        return int(raw) if raw else 0

    @staticmethod
    def _as_text(value) -> str:
        return value.decode("utf-8") if isinstance(value, bytes) else str(value)

    # -- adoption ---------------------------------------------------------

    async def _adopt(self):
        """Rebuild the actual set from the transport (FR-005).

        Called at start-up, and again each pass until one enumeration finishes.
        Existing subscriptions are adopted, never made a second time. A partial
        enumeration keeps what it saw: the subscriptions it did not reach are
        not treated as absent, and drops stay switched off until a full pass
        succeeds (NFR-003).
        """
        # `transport.list()` is a paginated walk with awaits in it, and the
        # pool's supervisor runs on this same loop. A socket can die *during*
        # the enumeration and call `invalidate_actual_set()`, and marking the
        # adoption complete afterwards would throw that signal away: the
        # enumeration's snapshot still holds the dead session, so its channels
        # are recorded as covered while nothing delivers for them, and nothing
        # re-enumerates until some later, unrelated loss. Count invalidations
        # and re-check at the end.
        invalidations_before = self._invalidations
        # Stamped for every attempt, so a failed walk cannot leave
        # `_readopt_due()` permanently true. This paces the healthy re-check;
        # what bounds a FAILING one is `_adopt_retry_after`, set below.
        self._last_adopt = time.monotonic()
        adopted: Dict[int, str] = {}
        skipped_status = 0
        complete = True
        try:
            async for subscription in self.transport.list():
                if subscription.status in ADOPTABLE_STATUSES:
                    adopted[subscription.broadcaster_id] = subscription.subscription_id
                else:
                    # Revoked or disconnected. Treat it as absent so that the
                    # diff below makes it again if the channel is still wanted.
                    skipped_status += 1
        except Exception as e:
            complete = False
            # Hold off the retry. Stamping `_last_adopt` alone did NOT bound
            # this, which the comment there used to claim: a partial walk also
            # clears `_adoption_complete`, and `reconcile_once` re-adopts on
            # `not self._adoption_complete OR self._readopt_due()`, so the
            # first disjunct forced a full multi-page enumeration on every 5 s
            # pass however recently `_last_adopt` was stamped. A socket loss
            # still retries at once -- it does not come through here.
            self._adopt_retry_after = time.monotonic() + self.config.adopt_retry_seconds
            logger.error(
                "Subscription enumeration failed part way, keeping what was seen",
                extra={
                    "error": str(e),
                    "seen": len(adopted),
                    "retry_after_seconds": self.config.adopt_retry_seconds,
                },
            )

        invalidated_during = self._invalidations != invalidations_before

        if complete and not invalidated_during:
            self._actual = adopted
            self._adoption_complete = True
        elif complete:
            # A connection was lost while this enumeration ran. What it saw is
            # still the freshest view available, so keep it -- but leave the
            # adoption incomplete so the next pass re-enumerates and drops stay
            # held back until one clean walk succeeds (NFR-003).
            self._actual = adopted
            logger.warning(
                "Subscriptions were lost while enumerating, re-enumerating next pass",
                extra={"adopted": len(adopted)},
            )
        else:
            # Keep both views. A subscription seen before is still real, and
            # one seen now is new information.
            merged = dict(self._actual)
            merged.update(adopted)
            self._actual = merged
            # And the view has holes again, so drops go back off until a clean
            # walk succeeds (NFR-003). Before the periodic re-adopt existed
            # this was implicit -- `_adopt` only ran while the flag was already
            # False -- and the periodic path broke that implication by reaching
            # here with it True.
            self._adoption_complete = False

        logger.info(
            "Adopted existing subscriptions",
            extra={
                "adopted": len(adopted),
                "actual": len(self._actual),
                "skipped_not_enabled": skipped_status,
                "complete": complete,
            },
        )

    # -- acting on the diff -----------------------------------------------

    async def _create_all(self, broadcaster_ids: List[int]):
        """Create every missing subscription, highest rank first.

        `broadcaster_ids` arrives in rank order and the worker pool takes from
        the front, so the most-watched channels come up first.

        A 429 does not drop a channel. The failed channels go into the next
        round after a backoff. Anything still unfinished when the rounds run
        out stays in `chat:desired`, so the next pass tries it again.
        """
        pending = list(broadcaster_ids)
        for round_number in range(1, self.config.max_retry_rounds + 1):
            if not pending:
                return
            rate_limited = await self._run_batch(pending, self._create_one, operation="create")
            if not rate_limited:
                return
            pending = [broadcaster_id for broadcaster_id, _ in rate_limited]
            if round_number == self.config.max_retry_rounds:
                break  # No point sleeping, the pass ends here.
            backoff = self._backoff_for(rate_limited)
            logger.warning(
                "Rate limited, backing off and retrying the failed channels",
                extra={
                    "rate_limited": len(pending),
                    "round": round_number,
                    "backoff_seconds": backoff,
                },
            )
            await asyncio.sleep(backoff)

        logger.error(
            "Retry rounds exhausted, carrying the rest to the next pass",
            extra={"remaining": len(pending), "rounds": self.config.max_retry_rounds},
        )

    def _backoff_for(self, rate_limited) -> float:
        """Honour `Ratelimit-Reset` when the transport gives it to us."""
        offered = [error.retry_after for _, error in rate_limited if error.retry_after]
        base = max(offered) if offered else self.config.rate_limit_backoff_seconds
        if not base:
            return 0.0
        # A little jitter, so two passes that restart together do not line up.
        return base + random.uniform(0, min(1.0, base * 0.1))

    async def _drop_all(self, broadcaster_ids: List[int]):
        """Remove subscriptions for channels that left the desired set."""
        await self._run_batch(broadcaster_ids, self._drop_one, operation="drop")

    async def _create_one(self, broadcaster_id: int):
        if broadcaster_id in self._actual:
            return  # Adopted by an earlier round. Never make a second.
        await self._maybe_refresh_desired()
        if broadcaster_id not in self._desired_ids:
            logger.info(
                "Channel left the desired set mid-pass, not creating",
                extra={"broadcaster_id": broadcaster_id},
            )
            return
        subscription_id = await self.transport.create(broadcaster_id)
        self._actual[broadcaster_id] = subscription_id
        if broadcaster_id in self._rechecking_refusals:
            # The channel refused more than REFUSAL_RECHECK_DAYS ago and has
            # just accepted. Clear the mark so it is a normal channel again.
            self._rechecking_refusals.discard(broadcaster_id)
            self._clear_refusal(broadcaster_id)
            logger.info(
                "Stale refusal cleared, channel accepted the subscription",
                extra={"broadcaster_id": broadcaster_id},
            )

    async def _drop_one(self, broadcaster_id: int):
        subscription_id = self._actual.get(broadcaster_id)
        if subscription_id is None:
            return
        await self.transport.delete(subscription_id)
        self._actual.pop(broadcaster_id, None)

    async def _run_batch(self, broadcaster_ids: Iterable[int], handler, operation: str):
        """Run `handler` over the ids on a fixed pool of workers.

        NFR-001. The pool size is the bound: `concurrency` tasks, whatever the
        size of the work. There is no task or thread per channel. This is why
        the work goes through a queue instead of a gather over every item --
        a gather would build 500 task objects for a 500-channel cold start.

        Returns the ids that hit a 429, with the error, for the retry loop.
        """
        queue: asyncio.Queue = asyncio.Queue()
        for broadcaster_id in broadcaster_ids:
            queue.put_nowait(broadcaster_id)
        if queue.empty():
            return []

        rate_limited: List = []

        def count_failure(reason: str):
            # The FR-012 counter is about creates. A failed delete is logged
            # but not counted, or the name would lie.
            if operation == "create":
                subscription_create_failures_total.labels(reason=reason).inc()

        async def worker():
            while True:
                try:
                    broadcaster_id = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                try:
                    await handler(broadcaster_id)
                except RateLimitedError as e:
                    rate_limited.append((broadcaster_id, e))
                    count_failure("rate_limited")
                except SubscriptionRefusedError as e:
                    count_failure("refused")
                    if operation == "create":
                        # Durable, so the next pass skips it instead of
                        # spending a POST on it again (FR-007). A channel
                        # already under a stale mark gets its timestamp reset
                        # by the same UPDATE, which restarts its 7 days.
                        self._rechecking_refusals.discard(broadcaster_id)
                        self._record_refusal(broadcaster_id)
                    logger.warning(
                        "Channel refused the subscription",
                        extra={"broadcaster_id": broadcaster_id, "error": str(e)},
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    count_failure("error")
                    logger.error(
                        "Subscription operation failed",
                        extra={
                            "operation": operation,
                            "broadcaster_id": broadcaster_id,
                            "error": str(e),
                            "error_type": type(e).__name__,
                        },
                    )

        workers = [
            asyncio.create_task(worker())
            for _ in range(min(self.config.concurrency, queue.qsize()))
        ]
        try:
            await asyncio.gather(*workers)
        except asyncio.CancelledError:
            for task in workers:
                task.cancel()
            raise
        return rate_limited

    async def _maybe_refresh_desired(self):
        """Take a fresh desired view if the poller bumped the generation.

        A cold start can run for tens of seconds. A poll that lands part way
        through it can remove a channel that this pass is still about to
        create. The flag check is free, and Redis is read once per generation,
        not once per channel.
        """
        if not self._wake.is_set():
            return
        async with self._refresh_lock:
            if not self._wake.is_set():
                return
            try:
                desired = self._read_desired()
            except Exception as e:
                # Keep the view we have. The next pass corrects it.
                logger.warning("Mid-pass desired refresh failed", extra={"error": str(e)})
                return
            # Only the desired-set signal. A socket loss has its own event, so
            # clearing this one cannot swallow it and there is nothing to put
            # back -- which matters because this runs once per channel, and
            # re-setting the event here left it set for the rest of the pass,
            # turning "read Redis once per generation" into three synchronous
            # round trips per channel.
            self._wake.clear()
            self._desired_ids = set(desired.broadcaster_ids())
            logger.info(
                "Picked up a new desired set mid-pass",
                extra={"generation": desired.generation, "desired": len(self._desired_ids)},
            )

    # -- refusals (FR-007, D5) --------------------------------------------

    def _drop_refused(self, broadcaster_ids: List[int]) -> List[int]:
        """Remove the channels that are still under a refusal.

        One query per pass for the whole candidate list, never one per
        channel. A mark older than `REFUSAL_RECHECK_DAYS` does not come back
        from the store, so those channels stay in the list and get their one
        retry; they are remembered here so that a success can clear the mark.
        """
        self._rechecking_refusals = set()
        if self.refusal_store is None or not broadcaster_ids:
            return broadcaster_ids

        try:
            marks = self.refusal_store.refusals(broadcaster_ids)
        except Exception as e:
            # NFR-002 again. A store that is away must not take the loop down,
            # and must not leave every channel unsubscribed. Attempting a
            # channel that would have been skipped costs one POST; skipping
            # every channel because the database is away costs the coverage.
            logger.error(
                "Refusal cache unavailable, attempting every channel this pass",
                extra={"error": str(e), "error_type": type(e).__name__},
            )
            return broadcaster_ids

        if not marks:
            return broadcaster_ids

        standing = {bid for bid, stale in marks.items() if not stale}
        # A stale mark buys one retry this pass. Remember which, so that a
        # create that now succeeds can clear the mark instead of leaving the
        # channel marked while it is plainly working.
        self._rechecking_refusals = {bid for bid, stale in marks.items() if stale}
        attempting = [bid for bid in broadcaster_ids if bid not in standing]
        logger.info(
            "Applied the refusal cache",
            extra={
                "skipped": len(standing),
                "rechecking": len(self._rechecking_refusals),
                "attempting": len(attempting),
                "recheck_days": REFUSAL_RECHECK_DAYS,
            },
        )
        return attempting

    def _record_refusal(self, broadcaster_id: int):
        if self.refusal_store is None:
            return
        self.refusal_store.mark_refused(broadcaster_id)

    def _clear_refusal(self, broadcaster_id: int):
        if self.refusal_store is None:
            return
        self.refusal_store.clear_refusal(broadcaster_id)

    def _publish_occupancy(self):
        try:
            occupancy = self.transport.occupancy()
            # Drop the previous labels first. A connection that is retired
            # keeps its last value forever otherwise, so a pool that lost a
            # socket goes on reporting the 300 subscriptions it no longer has
            # -- which is precisely the number the FR-012 alert watches.
            eventsub_connection_occupancy.clear()
            for connection, count in occupancy.items():
                eventsub_connection_occupancy.labels(connection=connection).set(count)
        except Exception as e:
            logger.warning("Could not read transport occupancy", extra={"error": str(e)})
