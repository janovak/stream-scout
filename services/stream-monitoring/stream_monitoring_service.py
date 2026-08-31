#!/usr/bin/env python3
"""
Stream Monitoring Service

Ranks the top Twitch streams and writes the set that chat must cover to Redis.
Uses Redis for online streamer state management with TTL-based expiration.

The poll job decides intent and returns. It makes no chat connections and no
subscriptions. `reconciler.py` reads the intent and does all network fan-out,
in parallel, on its own task. The two only meet through the
`DesiredSetStore` interface.
"""

import asyncio
import json
import logging
import math
import os
import signal
import sys
import time
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Dict, List, Optional, Set

import psycopg2
import psycopg2.pool
import redis
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from confluent_kafka import Producer
from prometheus_client import Counter, Gauge, Histogram, start_http_server
from psycopg2.extras import execute_values
from pythonjsonlogger import jsonlogger
from twitchAPI.twitch import Twitch
from twitchAPI.type import InvalidTokenException, MissingScopeException
from twitchAPI.type import AuthScope

from desired_set_store import DesiredSetStore, RedisDesiredSetStore
from eventsub_pool import EventSubPoolTransport, map_chat_message
from reconciler import (
    PostgresRefusalStore,
    Reconciler,
    resolve_reconciler_config,
)
from token_manager import TwitchCredentials, get_credentials


# Configuration
KAFKA_BROKER_URL = os.getenv("KAFKA_BROKER_URL", "localhost:9092")
POSTGRES_URL = os.getenv("POSTGRES_URL", "postgresql://twitch:twitch_password@localhost:5432/twitch")
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
TWITCH_CLIENT_ID = os.getenv("TWITCH_CLIENT_ID", "")
TWITCH_CLIENT_SECRET = os.getenv("TWITCH_CLIENT_SECRET", "")
PROMETHEUS_PORT = int(os.getenv("PROMETHEUS_PORT", "9100"))
HEALTH_CHECK_PORT = int(os.getenv("HEALTH_CHECK_PORT", "8080"))
# Token problems that no restart can fix, so the service degrades to "no chat"
# rather than crash-looping. Anything else -- notably a transient Twitch or
# network failure during the live token validation -- must propagate instead,
# so the container restarts and recovers on its own.
TOKEN_FAILURES = (
    FileNotFoundError,
    json.JSONDecodeError,
    KeyError,
    ValueError,
    InvalidTokenException,
    MissingScopeException,
)
# How long shutdown lets an in-flight reconcile pass finish before cancelling
# it. Long enough for a create or delete already in flight to land; far short
# of a full ramp, which shutdown has no reason to wait for.
RECONCILER_STOP_TIMEOUT_SECONDS = float(os.getenv("RECONCILER_STOP_TIMEOUT_SECONDS", "5"))
# How long shutdown waits for an in-flight `initialize()` to finish before
# tearing down. Bounded, because a Twitch auth that never returns must not
# block SIGTERM for ever.
INITIALIZE_WAIT_SECONDS = float(os.getenv("INITIALIZE_WAIT_SECONDS", "20"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

# Token-file scope strings -> pyTwitchAPI enums. One place, so adding a scope
# is one line here and one line in seed_twitch_tokens.py REQUIRED_SCOPES.
#
# `user:read:chat` is the EventSub chat scope (T017).
# A broadcaster marked clipping-disabled re-enters the ranking once the mark is
# this old, for one more attempt. Same interval as the reconciler's refusal
# re-check (reconciler.REFUSAL_RECHECK_DAYS), because they are the same rule
# applied to the two skip lists (spec 004 D5).
CLIPPING_RECHECK_DAYS = 7

SCOPE_MAP = {
    "user:read:chat": AuthScope.USER_READ_CHAT,
    "clips:edit": AuthScope.CLIPS_EDIT,
}

# Hysteresis thresholds for the monitored set
# A streamer enters the desired set on reaching top JOIN_THRESHOLD
# A streamer leaves it only on exiting top LEAVE_THRESHOLD
# This preserves Flink baseline data during rank fluctuations
#
# Configurable so the monitored set can be ramped empirically (15/30 ->
# 50/100 -> 150/300 ...) to find where the system actually degrades, rather
# than designing for one guessed target. The old warning here was that a
# larger set cost startup time inside the poll tick, at roughly
# LEAVE_THRESHOLD/2 seconds to join from cold. That is no longer true: the
# poll writes intent and returns, and `reconciler.py` does the fan-out in
# parallel outside the tick. See `compute_desired_set` below.
def resolve_thresholds(env=None):
    """Read and validate the hysteresis thresholds from `env`.

    A function, not inline module code, so tests can exercise the validation
    without reloading this module -- module-level start_http_server() and the
    Prometheus collectors below cannot be registered twice in one process.

    Returns (join_threshold, leave_threshold). Raises ValueError on a
    configuration that would misbehave rather than fail loudly at runtime.
    """
    env = os.environ if env is None else env
    join = int(env.get("JOIN_THRESHOLD", "15"))
    leave = int(env.get("LEAVE_THRESHOLD", "30"))

    if join < 1:
        raise ValueError(f"JOIN_THRESHOLD must be >= 1, got {join}")
    if leave < join:
        # Hysteresis requires a band. Equal values give none, and an inverted
        # pair would make every desired channel instantly leave-eligible --
        # thrashing the desired set once per poll and destroying Flink's
        # baseline.
        raise ValueError(
            f"LEAVE_THRESHOLD ({leave}) must be >= JOIN_THRESHOLD "
            f"({join}) to give the hysteresis band a width"
        )
    return join, leave


JOIN_THRESHOLD, LEAVE_THRESHOLD = resolve_thresholds()


def compute_desired_set(ranked_logins, previous_desired, join_threshold, leave_threshold):
    """Return {login: rank} for the channels chat should cover, with hysteresis.

    This is the same band the join loop used to apply, moved to where intent is
    decided instead of where connections are made (FR-011). The rule does not
    change:

    - A login ENTERS when it reaches the top `join_threshold`.
    - A login STAYS while it holds the top `leave_threshold`, even though it
      is no longer in the join band. This is the retained band, ranks 16-30 at
      the shipped 15/30.
    - A login LEAVES only when it drops out of the top `leave_threshold`, or
      off the ranked list altogether.

    In set terms the old loop settled on `(joined | top_join) & top_leave`, and
    that is what the expression below computes. Hysteresis stops a channel near
    the boundary from leaving and rejoining once per poll, which would destroy
    the Flink baseline it has built.

    `ranked_logins` is in rank order, best first. `previous_desired` is the
    membership of the last poll. It is read back from Redis rather than held in
    memory, so a restart keeps the band instead of collapsing it to the join
    threshold.
    """
    desired = {}
    for rank, login in enumerate(ranked_logins, 1):
        if rank > leave_threshold:
            break
        if rank <= join_threshold or login in previous_desired:
            desired[login] = rank
    return desired


# Extra raw streams (by viewer rank) to fetch beyond LEAVE_THRESHOLD so that
# streamers with clipping disabled don't eat a rank slot a real candidate
# could use -- some of Twitch's consistently highest-viewed streamers
# (e.g. kaicenat, ishowspeed) have clipping disabled, so without padding
# they'd permanently shrink the real candidate pool below LEAVE_THRESHOLD.
#
# The default of 20 was sized against LEAVE_THRESHOLD=30. It is a flat pad, so
# it thins out as the threshold grows; scale it with the threshold if the
# "Fewer than LEAVE_THRESHOLD clip-allowed streams" warning starts firing.
CLIPPING_DISABLED_FETCH_BUFFER = int(os.getenv("CLIPPING_DISABLED_FETCH_BUFFER", "20"))

REDIS_STREAMER_TTL = 180  # 3 minutes TTL for streamer online status
POLL_INTERVAL_SECONDS = 120  # Poll every 2 minutes

# Helix caps `first` at 100 per page, so a fetch larger than that must
# paginate. pyTwitchAPI's get_streams() is an auto-paginating async generator:
# pass first=100 (one full page per request) and keep consuming until we have
# what we need. Passing first>100 is rejected by Helix outright, which is what
# the old min(..., 100) guarded against -- at the cost of silently capping the
# monitored set at 100 however high LEAVE_THRESHOLD went.
HELIX_MAX_PAGE_SIZE = 100

# aiohttp already bounds this request (ClientTimeout total=300s), but that is
# well past our 120s poll interval, so a stalled call would silently eat
# several cycles before raising. Measured median for a single page is ~0.1s.
#
# This is now a per-page budget, not a whole-call one: the timeout below scales
# with the number of pages the fetch needs, so raising LEAVE_THRESHOLD does not
# silently start tripping a fixed timeout. 10s per page keeps the ~100x
# headroom the single-page measurement justified.
# Don't go much lower: a skipped poll opens a 240s gap against the 180s
# REDIS_STREAMER_TTL, expiring online keys and churning lifecycle events.
GET_STREAMS_TIMEOUT_SECONDS = 10


def fetch_budget(fetch_count):
    """Return (pages, timeout_seconds) for fetching `fetch_count` streams.

    Scales the budget with the number of Helix pages needed, so raising
    LEAVE_THRESHOLD does not silently start tripping a bound sized for one
    page. Caps it below the poll interval regardless: bounding this call
    exists to recover within one poll, and an unscaled ceiling would let a
    stalled fetch eat several cycles -- the very thing
    GET_STREAMS_TIMEOUT_SECONDS guards against. At the measured ~0.1s per
    page the cap still leaves a wide margin (a 21-page fetch runs in ~2s
    against a 60s cap).
    """
    pages = math.ceil(fetch_count / HELIX_MAX_PAGE_SIZE)
    return pages, min(GET_STREAMS_TIMEOUT_SECONDS * pages, POLL_INTERVAL_SECONDS // 2)

# Logging setup
logger = logging.getLogger("stream_monitoring")
logger.setLevel(getattr(logging, LOG_LEVEL.upper()))
handler = logging.StreamHandler(sys.stdout)
formatter = jsonlogger.JsonFormatter(
    fmt="%(asctime)s %(levelname)s %(name)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S"
)
handler.setFormatter(formatter)
logger.addHandler(handler)

# Prometheus metrics
POLL_OUTCOMES = (
    "success",
    "metadata_failed",
    "ranking_failed",
    "desired_read_failed",
    "online_snapshot_failed",
    "online_refresh_failed",
    "desired_publish_failed",
    "unexpected_failure",
)
POLL_PHASES = (
    "ranking_fetch",
    "metadata_persistence",
    "online_snapshot",
    "online_refresh",
    "lifecycle_publication",
    "desired_set_publication",
)
PHASE_OUTCOMES = ("success", "failure", "empty")

active_stream_count = Gauge("active_stream_count", "Number of currently monitored streams")
chat_messages_total = Counter("chat_messages_total", "Total chat messages processed", ["broadcaster_id"])
twitch_api_errors_total = Counter("twitch_api_errors_total", "Total Twitch API errors", ["error_type"])
kafka_messages_produced = Counter("kafka_messages_produced", "Total Kafka messages produced", ["topic"])
stream_poll_duration_seconds = Histogram(
    "stream_poll_duration_seconds",
    "Stream poll wall-clock duration by final bounded outcome",
    ["outcome"],
)
stream_poll_phase_duration_seconds = Histogram(
    "stream_poll_phase_duration_seconds",
    "Stream poll phase duration by bounded phase and outcome",
    ["phase", "outcome"],
)
stream_metadata_consecutive_failures = Gauge(
    "stream_metadata_consecutive_failures",
    "Consecutive failed non-empty streamer metadata batches",
)


class StreamMonitoringService:
    """Main service class for monitoring Twitch streams."""

    def __init__(self):
        self.twitch: Optional[Twitch] = None
        self.scheduler: Optional[AsyncIOScheduler] = None
        self.kafka_producer: Optional[Producer] = None
        self.db_pool: Optional[psycopg2.pool.ThreadedConnectionPool] = None
        self.redis_client: Optional[redis.Redis] = None
        self.desired_store: Optional[DesiredSetStore] = None
        self.credentials: Optional[TwitchCredentials] = None
        self.running = True
        # The task running `initialize()`, so shutdown can do more than wait on
        # it. A wait alone was not enough: on timeout `stop()` went ahead and
        # tore down, while `initialize()` carried on and built a Kafka
        # producer, a Postgres pool, a Redis client and live websockets
        # afterwards -- and `_stopping` was already True, so nothing would ever
        # close them. Owning the task means it can be cancelled instead. Its
        # completion also replaces the separate "initialized" event: a
        # start-up that RAISES finishes the task just the same, so a shutdown
        # racing a crash-loop does not wait out INITIALIZE_WAIT_SECONDS.
        # `_stopping` makes `stop()` idempotent: a signal and a failing
        # `start()` can both reach it.
        self._init_task: Optional[asyncio.Task] = None
        self._stopping = False
        # `stop()` must not call `shutdown()` on a scheduler that was built but
        # never started -- APScheduler raises there, and that exception used to
        # abandon the whole teardown.
        self._scheduler_started = False
        self.reconciler: Optional[Reconciler] = None
        self.transport: Optional[EventSubPoolTransport] = None
        # Both set from the token file in initialize(). No user auth at all
        # means no chat transport can be built; user auth without
        # user:read:chat means every chat subscription is going to refuse, for
        # one reason that has nothing to do with the broadcasters.
        self.has_user_auth = False
        self.has_chat_scope = False
        self._reconciler_task: Optional[asyncio.Task] = None
        self._metadata_consecutive_failures = 0
        stream_metadata_consecutive_failures.set(0)
        self._poll_observer = None

    async def _on_token_refresh(self, access_token: str, refresh_token: str):
        """Callback invoked when tokens are refreshed by pyTwitchAPI."""
        logger.info("Twitch tokens refreshed, persisting to file")
        if self.credentials:
            self.credentials.persist(access_token, refresh_token)

    async def initialize(self):
        """Initialize all connections and services."""
        logger.info("Initializing Stream Monitoring Service")
        await self._initialize()

    async def _initialize(self):

        # The metrics server comes up FIRST, before the token, Kafka, Postgres,
        # Redis or the transport -- every one of which can fail on a bad day.
        # It used to start last, so any of those failures killed the process
        # with no /metrics at all, taking down the FR-012 gauges an operator is
        # told to check at exactly the moment the service is failing.
        start_http_server(PROMETHEUS_PORT)
        logger.info("Prometheus metrics server started", extra={"port": PROMETHEUS_PORT})

        # Initialize Twitch API with app credentials
        self.twitch = await Twitch(TWITCH_CLIENT_ID, TWITCH_CLIENT_SECRET)

        # Load user tokens from file and set up user authentication
        self.credentials = get_credentials()
        try:
            record = self.credentials.load()

            # Convert scope strings to AuthScope enums
            auth_scopes = [SCOPE_MAP[scope] for scope in record.scopes if scope in SCOPE_MAP]
            unknown_scopes = [scope for scope in record.scopes if scope not in SCOPE_MAP]
            if unknown_scopes:
                # Not fatal -- an extra scope on the token costs nothing. Say
                # so, though: a typo in the token file used to be silent.
                logger.warning("Token file carries scopes this service does not map", extra={
                    "scopes": unknown_scopes
                })
            self.has_user_auth = True
            self.has_chat_scope = AuthScope.USER_READ_CHAT in auth_scopes
            if not self.has_chat_scope:
                logger.error(
                    "Token is missing user:read:chat -- EventSub chat subscriptions will refuse. "
                    "Re-seed with 'python seed_twitch_tokens.py' (spec 004 T017)",
                    extra={"scopes": record.scopes},
                )

            # Register the refresh callback BEFORE authenticating, not after.
            # `set_user_authentication` validates the token and, on a 401,
            # refreshes it internally and invokes this callback (twitch.py:716
            # -721). Assigned afterwards it is still None at that moment, so
            # the rotated refresh token is dropped on the floor and the file
            # keeps the old one -- the exact failure `token_manager`'s
            # `test_refresh_stores_rotated_refresh_token` exists to prevent.
            self.twitch.user_auth_refresh_callback = self._on_token_refresh

            await self.twitch.set_user_authentication(
                record.access_token,
                auth_scopes,
                record.refresh_token
            )

            logger.info("User authentication configured with pre-seeded tokens", extra={
                "scopes": record.scopes
            })

        except TOKEN_FAILURES as e:
            # Every way the TOKEN can fail, not just a missing file: expired
            # and unrefreshable (InvalidTokenException), scope-reduced
            # (MissingScopeException), truncated or hand-edited (raises out of
            # `credentials.load()`). Those are permanent until someone re-seeds
            # the file, so crash-looping the container achieves nothing and the
            # fallback below is the right answer.
            #
            # Deliberately NOT `except Exception`. `set_user_authentication`
            # makes a live validate call, so a network blip or a Twitch 5xx
            # lands here too -- and degrading on those was worse than the crash
            # it replaced: the service would run for the rest of the process
            # lifetime with no chat ingestion at all, while the poll job kept
            # working and /health kept returning OK, and nothing ever retried.
            # A transient failure should propagate and let Docker restart us,
            # which recovers on its own.
            logger.warning(
                "No usable user token, running without user auth (chat will not work)",
                extra={"error": str(e), "error_type": type(e).__name__},
            )
            # Fall back to app-only auth for streams API
            await self.twitch.authenticate_app([])
            self.has_user_auth = False

        # Initialize Kafka producer
        self.kafka_producer = Producer({
            "bootstrap.servers": KAFKA_BROKER_URL,
            "client.id": "stream-monitoring-service",
            "acks": "all",
            "retries": 3,
            "retry.backoff.ms": 1000,
        })
        logger.info("Kafka producer initialized", extra={"broker": KAFKA_BROKER_URL})

        # Initialize Postgres connection pool
        self.db_pool = psycopg2.pool.ThreadedConnectionPool(
            minconn=2,
            maxconn=10,
            dsn=POSTGRES_URL
        )
        logger.info("Postgres connection pool initialized")

        # Initialize Redis
        self.redis_client = redis.from_url(REDIS_URL, decode_responses=True)
        self.redis_client.ping()
        self.desired_store = RedisDesiredSetStore(self.redis_client)
        logger.info("Redis connection initialized")

        # Build the reconciler. It reads the desired set this service writes
        # and owns every network call that used to happen inside the poll tick.
        # Without user auth there is no transport to build: the pool resolves
        # the auth user through get_users(), which an app token cannot do. The
        # warning above promises the service keeps running with chat off, so
        # honour that instead of crash-looping the container on a missing
        # token file.
        if not self.has_user_auth:
            logger.error(
                "No user authentication, so no chat transport and no reconciler. "
                "The poll job still runs and writes the desired set. "
                "Run 'python seed_twitch_tokens.py' and restart to ingest chat"
            )
            self._build_scheduler()
            return

        # NOT wrapped in a degrade-and-continue handler. `EventSubPoolTransport
        # .start()` calls `get_users()`, so the failures here are transient by
        # nature -- a network blip, a Twitch 5xx. Swallowing them left the
        # service running with no transport, no reconciler and zero chat
        # ingestion for the rest of the process lifetime, with /health still
        # green and nothing ever retrying. Letting it propagate restarts the
        # container, which recovers by itself. The metrics server is already up
        # by this point, so the FR-012 gauges are still there to diagnose it --
        # that was the real fix, and the swallow was never load-bearing.
        self.transport = await self._build_transport()

        self.reconciler = Reconciler(
            transport=self.transport,
            desired_store=self.desired_store,
            config=resolve_reconciler_config(),
            # active_stream_count used to count IRC rooms. It now follows the
            # reconciler's actual set -- the subscriptions that really exist.
            on_pass_complete=active_stream_count.set,
            refusal_store=self._build_refusal_store(),
        )

        self._build_scheduler()

    def _build_scheduler(self):
        self.scheduler = AsyncIOScheduler()
        self.scheduler.add_job(
            self.poll_top_streams,
            "interval",
            seconds=POLL_INTERVAL_SECONDS,
            id="poll_streams",
            next_run_time=datetime.now(timezone.utc)
        )

    def _build_refusal_store(self):
        """The durable refusal cache, or None when it would do harm.

        A token without `user:read:chat` makes EVERY channel refuse, for one
        reason that has nothing to do with the broadcasters. Persisting that
        would write `eventsub_refused_at` across the whole monitored set and
        then skip all of it for seven days -- a token mistake turned into a
        week-long outage. Without the store those refusals stay loud, repeated
        and harmless, and they stop the moment the token is re-seeded.
        """
        if not self.has_chat_scope:
            logger.error(
                "Running without the refusal cache: the token cannot subscribe to chat, "
                "so refusals say nothing about the broadcasters and are not persisted"
            )
            return None
        return PostgresRefusalStore(self.db_pool)

    async def _build_transport(self):
        """Return the transport the reconciler drives.

        This is the whole of the Phase 2 swap. The reconciler is
        transport-independent by design, so replacing the Phase 1 stub with
        the real EventSub pool changes nothing else about it (T022).
        `StubTransport` stays in `reconciler.py` for the tests.

        The pool needs one thing the transport interface does not describe:
        somewhere to put the messages it receives. That is
        `_on_eventsub_message` below.
        """
        pool = EventSubPoolTransport(
            self.twitch,
            self._on_eventsub_message,
            on_subscriptions_lost=self._on_subscriptions_lost,
        )
        await pool.start()
        return pool

    def _on_subscriptions_lost(self, lost_subscriptions: int):
        """Subscriptions vanished under us -- a dead socket, or a revocation.

        A websocket that cannot reconnect takes all ~300 of its subscriptions
        with it (T023); a revocation takes one.

        Nothing is repaired here. The reconciler is told its picture of the
        world is stale, re-enumerates on the next pass, finds those channels
        absent and re-creates them on a surviving or new connection. The
        `eventsub_subscription_count` dip in between is the alert (FR-012).
        """
        logger.error("EventSub connection lost", extra={
            "lost_subscriptions": lost_subscriptions
        })
        if self.reconciler is not None:
            # The count goes with it, so the FR-012 gauge can drop now instead
            # of at the end of the next pass -- by which time a successful
            # re-create would have hidden the dip entirely.
            self.reconciler.invalidate_actual_set(lost_subscriptions)

    async def _on_eventsub_message(self, event):
        """Publish one EventSub chat message to Kafka.

        Called by the pool once per `channel.chat.message` event, on the
        receiving socket's own event loop rather than this service's. That is
        safe for what happens here: `_publish_chat_message` goes through
        confluent-kafka, whose `produce()` and `poll()` are thread-safe, and
        it is the same single producer the IRC path used.

        The mapping is `map_chat_message` in `eventsub_pool.py`, kept out of
        this method so the schema can be tested without a socket (T020, T021).
        """
        try:
            payload = map_chat_message(event)
            broadcaster_id = payload["broadcaster_id"]
            self._publish_chat_message(broadcaster_id, payload)
            chat_messages_total.labels(broadcaster_id=str(broadcaster_id)).inc()
        except Exception as e:
            logger.error("Error processing EventSub chat message", extra={
                "error": str(e),
                "error_type": type(e).__name__,
            })

    async def start(self):
        """Start the service."""
        if not self.running:
            # A signal beat us here -- `main()` awaits the health server before
            # this, and `stop()` is a no-op while there is nothing built and no
            # start-up task to wait on. Initializing now would allocate a Kafka
            # producer, a Postgres pool, a Redis client and live websockets
            # that the already-finished `stop()` can never come back to close.
            logger.info("Shutdown signalled before start-up, not initializing")
            return
        # As a task, so `stop()` can cancel it rather than only wait on it.
        self._init_task = asyncio.create_task(self.initialize())
        try:
            await self._init_task
        except asyncio.CancelledError:
            if asyncio.current_task().cancelling() > 0:
                # Aimed at THIS task, not at start-up. `stop()` cancels
                # `_init_task` alone, which leaves our own cancel count at
                # zero; anything that cancelled `start()` itself shows up here.
                # `_stopping` was the wrong discriminator: a cancellation
                # arriving from outside WHILE a shutdown is already running
                # sets it too, and swallowing that one reported clean
                # completion to a caller that had asked us to stop.
                raise
            # `stop()` ran out of patience with start-up. It owns the teardown
            # of whatever got built, and it is awaiting this task, so return
            # normally rather than propagating a cancellation that was never
            # aimed at us.
            logger.info("Start-up cancelled by shutdown")
            return
        if not self.running:
            # A signal arrived while `initialize()` was still building things.
            # Starting the scheduler and the reconciler now would hand
            # `stop()` -- which is waiting for exactly this moment -- more to
            # tear down, and race it while doing so.
            logger.info("Shutdown signalled during start-up, not starting work")
            return
        self.scheduler.start()
        self._scheduler_started = True

        # The reconciler is a task in this process, beside the poll job. It is
        # not a separate container: it shares this process's /health endpoint
        # and this logger.
        if self.reconciler is not None:
            self._reconciler_task = asyncio.create_task(self.reconciler.run())

        logger.info("Stream Monitoring Service started")

        # Keep the service running
        while self.running:
            await asyncio.sleep(1)

    async def stop(self):
        """Gracefully stop the service."""
        if self._stopping:
            return
        self._stopping = True
        logger.info("Stopping Stream Monitoring Service")
        self.running = False

        # Wait for start-up to finish before tearing anything down. A signal
        # can land while `initialize()` is mid-flight -- a `docker compose
        # restart` during a slow Twitch auth does it -- and tearing down then
        # closed the aiohttp session the auth call was still using, while every
        # other branch no-opped on a Kafka producer, DB pool, Redis client and
        # transport that did not exist yet. `initialize()` then built all of
        # them and nothing ever closed them: an unflushed producer, an open
        # Postgres pool and live websockets, after "stopped" had been logged.
        #
        # Bounded, because a Twitch auth that never returns must not block
        # SIGTERM for ever -- and CANCELLED when that bound is hit, which is
        # the part a plain wait was missing. Timing out and tearing down anyway
        # left start-up running, so it went on to build exactly the resources
        # this teardown had already decided did not exist, with `_stopping`
        # set so no later `stop()` could reach them either.
        if self._init_task is not None and not self._init_task.done():
            try:
                await asyncio.wait_for(
                    asyncio.shield(self._init_task), timeout=INITIALIZE_WAIT_SECONDS
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "Start-up did not finish before shutdown, cancelling it and "
                    "tearing down what exists",
                    extra={"timeout_seconds": INITIALIZE_WAIT_SECONDS},
                )
                self._init_task.cancel()
                try:
                    await self._init_task
                except asyncio.CancelledError:
                    # Deliberately swallowed, including a cancellation aimed at
                    # `stop()` itself. This is the teardown; the rule for
                    # everything below is that no single failure may skip the
                    # steps after it, and abandoning the flush and the socket
                    # close because someone cancelled the shutdown is exactly
                    # the truncation this function was rewritten to prevent.
                    # The discrimination two blocks up is a different case:
                    # there the cancellation came from the shielded CHILD, and
                    # continuing was the only correct answer.
                    pass
                except Exception as e:
                    logger.warning(
                        "Start-up failed while being cancelled",
                        extra={"error": str(e)},
                    )
            except Exception as e:
                # `initialize()` raised. Its own handlers have already logged
                # the detail; what matters here is that start-up is over, so
                # the teardown below can run against whatever it managed to
                # build.
                logger.warning("Start-up failed before shutdown", extra={"error": str(e)})
            except asyncio.CancelledError:
                # The start-up task was cancelled by something other than this
                # `stop()` -- an outer runtime unwinding its tasks while a
                # signal-driven shutdown is already in flight. The shield lets
                # that reach us, and letting it propagate abandoned the WHOLE
                # teardown: nothing below here ran, so the producer was never
                # flushed and the websockets were never closed. That is the
                # opposite of what a shutdown racing a cancellation should do.
                # Only a cancellation aimed at `stop()` ITSELF may stop it.
                if asyncio.current_task().cancelling() > 0:
                    raise
                logger.warning(
                    "Start-up was cancelled from elsewhere, tearing down what exists"
                )

        # Only if it was actually started. `start()` can now return before
        # `scheduler.start()` when shutdown is signalled during start-up, and
        # APScheduler's `shutdown()` on a never-started scheduler raises
        # `AttributeError: 'NoneType' object has no attribute
        # 'call_soon_threadsafe'`. That exception used to escape here and
        # abandon EVERY step below it -- the reconciler never stopped, the
        # websockets stayed open, and the Kafka producer was never flushed, so
        # buffered chat was dropped. It also propagated into `main()`, so the
        # process exited on a traceback with "stopped" never logged.
        #
        # Wrapped as well as guarded: no single failure in this teardown may
        # skip the ones after it, which is the property that was missing.
        if self.scheduler is not None and self._scheduler_started:
            try:
                self.scheduler.shutdown(wait=True)
            except Exception as e:
                logger.warning("Error shutting down the scheduler", extra={"error": str(e)})

        # Stop the reconciler before Redis closes underneath it. Ask first,
        # then cancel, so a pass that is already running can finish its current
        # operation instead of leaving a half-made subscription.
        #
        # The wait is what makes "ask first" real. `stop()` only sets a flag
        # and wakes the loop; cancelling in the next statement never let the
        # task run, so the cancellation landed at whatever await the pass was
        # sitting on -- including between `transport.create()` returning and
        # `_actual[bid]` being assigned, which is exactly the half-made
        # subscription this ordering claims to avoid.
        if self.reconciler is not None:
            self.reconciler.stop()
        if self._reconciler_task is not None:
            try:
                await asyncio.wait_for(
                    asyncio.shield(self._reconciler_task),
                    timeout=RECONCILER_STOP_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "Reconciler did not stop in time, cancelling",
                    extra={"timeout_seconds": RECONCILER_STOP_TIMEOUT_SECONDS},
                )
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.warning("Reconciler stopped with an error", extra={"error": str(e)})
            self._reconciler_task.cancel()
            try:
                await self._reconciler_task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.warning("Reconciler stopped with an error", extra={"error": str(e)})

        # After the reconciler, so a pass in flight cannot subscribe on a
        # socket that is being closed underneath it.
        if self.transport is not None:
            try:
                await self.transport.aclose()
            except Exception as e:
                logger.warning("Error closing the EventSub pool", extra={"error": str(e)})

        if self.twitch:
            await self.twitch.close()

        if self.kafka_producer:
            self.kafka_producer.flush(timeout=10)

        if self.db_pool:
            self.db_pool.closeall()

        if self.redis_client:
            self.redis_client.close()

        logger.info("Stream Monitoring Service stopped")

    async def poll_top_streams(self):
        """Poll Twitch API for top streams and write the desired set to Redis."""
        poll_started = time.perf_counter()
        phase_durations = {}
        outcome = "unexpected_failure"
        failed_phase = None
        ranked_count = 0
        desired_count = 0
        entered_count = 0
        left_count = 0
        metadata_input_count = 0
        metadata_unique_count = 0

        def finish_phase(phase, started, phase_outcome):
            duration = time.perf_counter() - started
            phase_durations[phase] = duration
            self._record_poll_phase(phase, phase_outcome, duration)

        try:
            logger.info("Polling for top streams")

            ranking_started = time.perf_counter()
            try:
                fetch_count = LEAVE_THRESHOLD + CLIPPING_DISABLED_FETCH_BUFFER

                async def _fetch_top_streams():
                    collected = []
                    async for stream in self.twitch.get_streams(
                        first=HELIX_MAX_PAGE_SIZE
                    ):
                        collected.append(stream)
                        if len(collected) >= fetch_count:
                            break
                    return collected

                pages, fetch_timeout = fetch_budget(fetch_count)
                raw_streams = await asyncio.wait_for(
                    _fetch_top_streams(), timeout=fetch_timeout
                )
                disabled_ids = self._get_clipping_disabled_ids(
                    [int(stream.user_id) for stream in raw_streams]
                )
                disabled_logins = {
                    stream.user_login.lower()
                    for stream in raw_streams
                    if int(stream.user_id) in disabled_ids
                }
                streams = [
                    stream
                    for stream in raw_streams
                    if int(stream.user_id) not in disabled_ids
                ][:LEAVE_THRESHOLD]
                normalized = [
                    (
                        rank,
                        int(stream.user_id),
                        stream.user_login.lower(),
                    )
                    for rank, stream in enumerate(streams, 1)
                ]
            except asyncio.TimeoutError as error:
                finish_phase("ranking_fetch", ranking_started, "failure")
                failed_phase = "ranking_fetch"
                outcome = "ranking_failed"
                logger.error(
                    "Timed out fetching top streams from Twitch API",
                    extra={
                        "timeout_seconds": fetch_timeout,
                        "pages": pages,
                        "fetch_count": fetch_count,
                        "error": str(error),
                        "error_type": type(error).__name__,
                    },
                )
                twitch_api_errors_total.labels(error_type="get_streams_timeout").inc()
                return outcome
            except Exception as error:
                finish_phase("ranking_fetch", ranking_started, "failure")
                failed_phase = "ranking_fetch"
                outcome = "ranking_failed"
                logger.error(
                    "Failed to fetch or normalize top streams",
                    extra={
                        "error": str(error),
                        "error_type": type(error).__name__,
                    },
                )
                twitch_api_errors_total.labels(error_type="poll_streams").inc()
                return outcome
            else:
                finish_phase(
                    "ranking_fetch",
                    ranking_started,
                    "empty" if not normalized else "success",
                )

            if len(streams) < LEAVE_THRESHOLD:
                logger.warning(
                    "Fewer than LEAVE_THRESHOLD clip-allowed streams found even after padding fetch",
                    extra={
                        "eligible_found": len(streams),
                        "leave_threshold": LEAVE_THRESHOLD,
                        "fetch_count": fetch_count,
                        "raw_streams_found": len(raw_streams)
                    }
                )

            try:
                previous = self.desired_store.read()
            except Exception as error:
                failed_phase = "desired_read"
                outcome = "desired_read_failed"
                logger.error(
                    "Failed to read previous desired set",
                    extra={
                        "phase": failed_phase,
                        "error": str(error),
                        "error_type": type(error).__name__,
                    },
                )
                return outcome

            previous_desired = set(previous.logins)
            previous_ids = previous.ids

            for login in disabled_logins:
                if login in previous_desired:
                    logger.info("Streamer has clipping disabled, dropping from the desired set", extra={
                        "broadcaster_login": login
                    })

            ranked_logins = [login for _, _, login in normalized]
            broadcaster_ids = {}
            metadata_records = []
            for _, broadcaster_id, broadcaster_login in normalized:
                broadcaster_ids[broadcaster_login] = broadcaster_id
                metadata_records.append((broadcaster_id, broadcaster_login))
            metadata_input_count = len(metadata_records)
            metadata_unique_count = len(
                {broadcaster_id for broadcaster_id, _ in metadata_records}
            )

            desired = compute_desired_set(
                ranked_logins, previous_desired, JOIN_THRESHOLD, LEAVE_THRESHOLD
            )
            departed = [
                login for login in previous.logins if login not in desired
            ]

            ranked_count = len(ranked_logins)
            desired_count = len(desired)
            entered_count = len(set(desired) - previous_desired)
            left_count = len(departed)

            metadata_started = time.perf_counter()
            metadata_result = self._upsert_streamer_batch(metadata_records)
            finish_phase(
                "metadata_persistence",
                metadata_started,
                (
                    "empty"
                    if metadata_result is None
                    else "success" if metadata_result else "failure"
                ),
            )
            metadata_failed = metadata_result is False

            snapshot_logins = list(
                dict.fromkeys(ranked_logins + departed)
            )
            snapshot_keys = [
                f"streamer:online:{login}" for login in snapshot_logins
            ]
            snapshot_started = time.perf_counter()
            if snapshot_keys:
                try:
                    raw_snapshot = self.redis_client.mget(snapshot_keys)
                    if len(raw_snapshot) != len(snapshot_keys):
                        raise ValueError(
                            "online snapshot response length "
                            f"{len(raw_snapshot)} did not match "
                            f"request length {len(snapshot_keys)}"
                        )
                except Exception as error:
                    finish_phase(
                        "online_snapshot", snapshot_started, "failure"
                    )
                    failed_phase = "online_snapshot"
                    outcome = "online_snapshot_failed"
                    logger.error(
                        "Online-state snapshot failed",
                        extra={
                            "phase": failed_phase,
                            "requested_key_count": len(snapshot_keys),
                            "error": str(error),
                            "error_type": type(error).__name__,
                        },
                    )
                    return outcome
                snapshot = MappingProxyType(
                    {
                        login: value is not None
                        for login, value in zip(
                            snapshot_logins, raw_snapshot
                        )
                    }
                )
                finish_phase("online_snapshot", snapshot_started, "success")
            else:
                snapshot = MappingProxyType({})
                finish_phase("online_snapshot", snapshot_started, "empty")

            first_current = {}
            for rank, broadcaster_id, login in normalized:
                first_current.setdefault(
                    login, (rank, broadcaster_id)
                )

            lifecycle_candidates = []
            invalid_departures = []
            for login, (rank, broadcaster_id) in first_current.items():
                if not snapshot[login] and rank <= JOIN_THRESHOLD:
                    lifecycle_candidates.append(
                        ("online", broadcaster_id, login, rank)
                    )
            for login in departed:
                if snapshot[login]:
                    continue
                previous_id = previous_ids.get(login)
                try:
                    previous_id = int(previous_id)
                except (TypeError, ValueError):
                    previous_id = None
                if previous_id is None or previous_id <= 0:
                    invalid_departures.append(login)
                    continue
                lifecycle_candidates.append(
                    ("offline", previous_id, login, 0)
                )

            refresh_started = time.perf_counter()
            if normalized:
                try:
                    pipeline = self.redis_client.pipeline(
                        transaction=False
                    )
                    for _, broadcaster_id, login in normalized:
                        pipeline.setex(
                            f"streamer:online:{login}",
                            REDIS_STREAMER_TTL,
                            broadcaster_id,
                        )
                    pipeline.execute(raise_on_error=True)
                except Exception as error:
                    finish_phase(
                        "online_refresh", refresh_started, "failure"
                    )
                    failed_phase = "online_refresh"
                    outcome = "online_refresh_failed"
                    logger.error(
                        "Online-state refresh failed",
                        extra={
                            "phase": failed_phase,
                            "refresh_count": len(normalized),
                            "error": str(error),
                            "error_type": type(error).__name__,
                        },
                    )
                    return outcome
                finish_phase("online_refresh", refresh_started, "success")
            else:
                finish_phase("online_refresh", refresh_started, "empty")

            lifecycle_started = time.perf_counter()
            for login in invalid_departures:
                logger.error(
                    "Offline lifecycle event suppressed: invalid previous id",
                    extra={
                        "broadcaster_login": login,
                        "previous_generation": previous.generation,
                    },
                )
            for event_type, broadcaster_id, login, rank in lifecycle_candidates:
                self._publish_lifecycle_event(
                    event_type, broadcaster_id, login, rank
                )
                logger.info(
                    f"Streamer {event_type}",
                    extra={
                        "broadcaster_login": login,
                        "broadcaster_id": broadcaster_id,
                        "rank": rank,
                    },
                )
            finish_phase(
                "lifecycle_publication",
                lifecycle_started,
                "success" if lifecycle_candidates else "empty",
            )

            desired_publish_started = time.perf_counter()
            try:
                self.desired_store.publish(desired, broadcaster_ids)
            except Exception as error:
                finish_phase(
                    "desired_set_publication",
                    desired_publish_started,
                    "failure",
                )
                failed_phase = "desired_publish"
                outcome = "desired_publish_failed"
                logger.error(
                    "Desired-set publication failed",
                    extra={
                        "phase": failed_phase,
                        "desired_count": len(desired),
                        "error": str(error),
                        "error_type": type(error).__name__,
                    },
                )
                return outcome

            if self.reconciler is not None:
                self.reconciler.notify_desired_changed()
            finish_phase(
                "desired_set_publication",
                desired_publish_started,
                "success",
            )
            outcome = "metadata_failed" if metadata_failed else "success"
            return outcome
        except Exception as error:
            failed_phase = failed_phase or "unexpected"
            outcome = "unexpected_failure"
            logger.error(
                "Unexpected poll failure",
                extra={
                    "phase": failed_phase,
                    "error": str(error),
                    "error_type": type(error).__name__,
                },
            )
            twitch_api_errors_total.labels(error_type="poll_streams").inc()
            return outcome
        finally:
            total_duration = time.perf_counter() - poll_started
            self._record_poll_outcome(outcome, total_duration)
            final_failed_phase = (
                "metadata_persistence"
                if outcome == "metadata_failed"
                else failed_phase
            )
            log = (
                logger.info
                if outcome in {"success", "metadata_failed"}
                else logger.error
            )
            log(
                "Poll finished",
                extra={
                    "outcome": outcome,
                    "failed_phase": final_failed_phase,
                    "duration_seconds": total_duration,
                    "phase_durations_seconds": phase_durations,
                    "ranked": ranked_count,
                    "desired": desired_count,
                    "entered": entered_count,
                    "left": left_count,
                    "metadata_input_count": metadata_input_count,
                    "metadata_unique_count": metadata_unique_count,
                    "metadata_failure_streak": (
                        self._metadata_consecutive_failures
                    ),
                },
            )

    def _record_poll_phase(self, phase, outcome, duration):
        if phase not in POLL_PHASES:
            raise ValueError(f"unbounded poll phase label: {phase}")
        if outcome not in PHASE_OUTCOMES:
            raise ValueError(f"unbounded poll phase outcome label: {outcome}")
        stream_poll_phase_duration_seconds.labels(
            phase=phase, outcome=outcome
        ).observe(duration)
        observer = self._poll_observer
        if observer is not None:
            observer.record_phase(phase, outcome)

    def _record_poll_outcome(self, outcome, duration):
        if outcome not in POLL_OUTCOMES:
            raise ValueError(f"unbounded poll outcome label: {outcome}")
        stream_poll_duration_seconds.labels(outcome=outcome).observe(duration)
        observer = self._poll_observer
        if observer is not None:
            observer.record_outcome(outcome)

    def _publish_chat_message(self, broadcaster_id: int, message: dict):
        """Publish chat message to Kafka."""
        try:
            self.kafka_producer.produce(
                topic="chat-messages",
                key=str(broadcaster_id).encode("utf-8"),
                value=json.dumps(message).encode("utf-8"),
                callback=self._delivery_callback
            )
            self.kafka_producer.poll(0)
            kafka_messages_produced.labels(topic="chat-messages").inc()
        except Exception as e:
            logger.error("Failed to publish chat message", extra={"error": str(e)})

    def _publish_lifecycle_event(self, event_type: str, broadcaster_id: int,
                                  broadcaster_login: str, rank: int):
        """Publish stream lifecycle event to Kafka."""
        try:
            event = {
                "event_type": event_type,
                "broadcaster_id": broadcaster_id,
                "broadcaster_login": broadcaster_login,
                "rank": rank,
                "timestamp": int(time.time())
            }
            self.kafka_producer.produce(
                topic="stream-lifecycle",
                key=str(broadcaster_id).encode("utf-8"),
                value=json.dumps(event).encode("utf-8"),
                callback=self._delivery_callback
            )
            self.kafka_producer.poll(0)
            kafka_messages_produced.labels(topic="stream-lifecycle").inc()
        except Exception as e:
            logger.error("Failed to publish lifecycle event", extra={"error": str(e)})

    def _delivery_callback(self, err, msg):
        """Kafka delivery callback."""
        if err:
            logger.error("Kafka delivery failed", extra={
                "error": str(err),
                "topic": msg.topic()
            })
        else:
            logger.debug("Kafka message delivered", extra={
                "topic": msg.topic(),
                "partition": msg.partition()
            })

    def _upsert_streamer_batch(self, records):
        """Persist normalized ``(streamer_id, login)`` rows atomically.

        ``None`` means no non-empty batch was attempted. For attempted batches,
        ``True`` means the one statement and commit were acknowledged and
        ``False`` means the entire batch must be retried on the next poll.
        """
        ordered_records = list(records)
        if not ordered_records:
            return None

        conn = None
        discard_connection = False
        metadata_by_id = {}
        rows = []
        try:
            for record in ordered_records:
                try:
                    streamer_id, streamer_login = record
                except (TypeError, ValueError) as error:
                    raise ValueError(
                        "metadata records must contain streamer_id and login"
                    ) from error
                metadata_by_id[streamer_id] = streamer_login
            rows = list(metadata_by_id.items())

            conn = self.db_pool.getconn()
            with conn.cursor() as cur:
                execute_values(
                    cur,
                    """
                    INSERT INTO streamers (
                        streamer_id,
                        streamer_login,
                        last_seen_at
                    )
                    VALUES %s
                    ON CONFLICT (streamer_id) DO UPDATE
                    SET streamer_login = EXCLUDED.streamer_login,
                        last_seen_at = EXCLUDED.last_seen_at
                    """,
                    rows,
                    template="(%s, %s, NOW())",
                    page_size=len(rows),
                )
            conn.commit()
        except Exception as error:
            self._metadata_consecutive_failures += 1
            stream_metadata_consecutive_failures.set(
                self._metadata_consecutive_failures
            )
            if conn is not None:
                try:
                    conn.rollback()
                except Exception as rollback_error:
                    discard_connection = True
                    logger.error(
                        "Streamer metadata batch rollback failed",
                        extra={
                            "error": str(rollback_error),
                            "error_type": type(rollback_error).__name__,
                        },
                    )
            logger.error(
                "Streamer metadata batch failed",
                extra={
                    "input_batch_size": len(ordered_records),
                    "unique_batch_size": len(metadata_by_id),
                    "metadata_failure_streak": self._metadata_consecutive_failures,
                    "error": str(error),
                    "error_type": type(error).__name__,
                },
            )
            return False
        else:
            self._metadata_consecutive_failures = 0
            stream_metadata_consecutive_failures.set(0)
            return True
        finally:
            if conn is not None:
                self.db_pool.putconn(conn, close=discard_connection)

    def _get_clipping_disabled_ids(self, streamer_ids: List[int]) -> Set[int]:
        """Return the subset of streamer_ids to drop from the ranking.

        `allows_clipping = FALSE` used to be permanent: a broadcaster who
        turned clip creation off once was never looked at again, even after
        turning it back on. A mark older than CLIPPING_RECHECK_DAYS is now
        treated as stale and left OUT of this set, so the broadcaster
        re-enters the ranking (FR-013, D5).

        What resolves it is a real clip attempt, which needs a spike: success
        clears the flag, a fresh 403 re-stamps the timestamp and starts the
        seven days again. So a stale mark is an un-skip, not a bounded retry
        -- a broadcaster who never trends holds a monitored slot from the
        moment the mark goes stale until they do. That is the rule
        `data-model.md` specifies; it is worth knowing it is not self-limiting
        the way the reconciler's refusal re-check is, where every refusal
        re-stamps.

        A NULL `clipping_disabled_at` beside a FALSE flag means the row
        predates the column and was not caught by the migration backfill.
        Treat it as disabled: no timestamp is no evidence that the mark is
        stale.
        """
        if not streamer_ids:
            return set()
        conn = None
        try:
            conn = self.db_pool.getconn()
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT streamer_id FROM streamers "
                    "WHERE streamer_id = ANY(%s) AND allows_clipping = FALSE "
                    "  AND (clipping_disabled_at IS NULL "
                    "       OR clipping_disabled_at >= NOW() - make_interval(days => %s))",
                    (streamer_ids, CLIPPING_RECHECK_DAYS)
                )
                return {row[0] for row in cur.fetchall()}
        except Exception as e:
            logger.error("Failed to query clipping-disabled streamers", extra={"error": str(e)})
            return set()
        finally:
            if conn:
                self.db_pool.putconn(conn)


async def run_health_check_server():
    """Run a simple HTTP health check server."""
    from aiohttp import web

    async def health_handler(request):
        return web.Response(text="OK", status=200)

    app = web.Application()
    app.router.add_get("/health", health_handler)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", HEALTH_CHECK_PORT)
    await site.start()
    logger.info("Health check server started", extra={"port": HEALTH_CHECK_PORT})


async def main():
    """Main entry point."""
    service = StreamMonitoringService()

    # Set up signal handlers
    loop = asyncio.get_event_loop()
    stop_task = None

    def signal_handler():
        # Held and awaited below. `stop()` sets `running = False` before its
        # first await, so `start()`'s keep-alive loop returns within a second
        # and `main()` would return with the shutdown only part done --
        # `asyncio.run` then cancels it where it stands. Everything after the
        # reconciler wait (transport close, Kafka flush, Postgres, Redis) was
        # being skipped: buffered chat dropped, subscriptions left for Twitch
        # to reap, and "Stream Monitoring Service stopped" never logged.
        nonlocal stop_task
        logger.info("Received shutdown signal")
        if stop_task is None:
            stop_task = asyncio.create_task(service.stop())

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, signal_handler)

    # Start health check server
    await run_health_check_server()

    # Start main service
    try:
        await service.start()
    except Exception as e:
        logger.error("Service error", extra={"error": str(e)})
        # If a signal already started a shutdown, `stop()` returns at once --
        # it is idempotent -- so awaiting that task is what actually waits for
        # the teardown. Without it `sys.exit(1)` raises SystemExit and
        # `asyncio.run` cancels the in-flight stop mid-way, which is the
        # truncated shutdown the await below exists to prevent.
        await service.stop()
        if stop_task is not None:
            try:
                await stop_task
            except Exception:
                logger.exception("Shutdown failed")
        sys.exit(1)

    # Let a shutdown that a signal started actually finish.
    if stop_task is not None:
        await stop_task


if __name__ == "__main__":
    asyncio.run(main())
