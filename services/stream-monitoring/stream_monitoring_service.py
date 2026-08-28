#!/usr/bin/env python3
"""
Stream Monitoring Service

Ranks the top Twitch streams and writes the set that chat must cover to Redis.
Uses Redis for online streamer state management with TTL-based expiration.

The poll job decides intent and returns. It makes no chat connections and no
subscriptions. `reconciler.py` reads the intent and does all network fan-out,
in parallel, on its own task. The two only meet at the Redis keys that
`reconciler.py` documents.
"""

import asyncio
import json
import logging
import math
import os
import signal
import sys
import time
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional, Set

import psycopg2
import psycopg2.pool
import redis
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from confluent_kafka import Producer
from prometheus_client import Counter, Gauge, start_http_server
from pythonjsonlogger import jsonlogger
from twitchAPI.chat import Chat, ChatMessage, EventData
from twitchAPI.twitch import Twitch
from twitchAPI.type import AuthScope, ChatEvent

from eventsub_pool import EventSubPoolTransport, map_chat_message
from reconciler import (
    DESIRED_GENERATION_KEY,
    DESIRED_IDS_KEY,
    DESIRED_KEY,
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
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

# Token-file scope strings -> pyTwitchAPI enums. One place, so adding a scope
# is one line here and one line in seed_twitch_tokens.py REQUIRED_SCOPES.
#
# `user:read:chat` is the EventSub chat scope (T017). `chat:read` is IRC's and
# goes away with IRC in Phase 3.
# A broadcaster marked clipping-disabled re-enters the ranking once the mark is
# this old, for one more attempt. Same interval as the reconciler's refusal
# re-check (reconciler.REFUSAL_RECHECK_DAYS), because they are the same rule
# applied to the two skip lists (spec 004 D5).
CLIPPING_RECHECK_DAYS = 7

SCOPE_MAP = {
    "chat:read": AuthScope.CHAT_READ,
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
        # pair would make every joined channel instantly leave-eligible --
        # thrashing chat connections once per poll and destroying Flink's
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
#
# The chat-side hang (Chat.leave_room waiting forever on a PART confirmation
# that a reconnect threw away) is fixed in the library itself -- see
# patches/twitchapi_leave_room_timeout.py -- not with a wrapper here.
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

# pyTwitchAPI's Chat.is_connected() also reads False for the entire span of a
# still-in-progress reconnect (it only reassigns the connection on success),
# not just after the library has permanently given up. Its own retry budget
# (reconnect_delay_steps) sums to 255s. Requiring the connection to look dead
# across this many consecutive 120s polls (240s+ of continuous non-connected
# readings) keeps us from tearing down a client that's still mid-recovery in
# the common case, without adding a wall-clock timer of our own.
DEAD_CHAT_CONFIRMATION_POLLS = 3

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
active_stream_count = Gauge("active_stream_count", "Number of currently monitored streams")
chat_messages_total = Counter("chat_messages_total", "Total chat messages processed", ["broadcaster_id"])
twitch_api_errors_total = Counter("twitch_api_errors_total", "Total Twitch API errors", ["error_type"])
kafka_messages_produced = Counter("kafka_messages_produced", "Total Kafka messages produced", ["topic"])


class StreamMonitoringService:
    """Main service class for monitoring Twitch streams."""

    def __init__(self):
        self.twitch: Optional[Twitch] = None
        self.chat: Optional[Chat] = None
        self.scheduler: Optional[AsyncIOScheduler] = None
        self.kafka_producer: Optional[Producer] = None
        self.db_pool: Optional[psycopg2.pool.ThreadedConnectionPool] = None
        self.redis_client: Optional[redis.Redis] = None
        self.credentials: Optional[TwitchCredentials] = None
        self.running = True
        self.joined_channels: Set[str] = set()
        # login -> id. Still read by the IRC message handler, which Phase 3
        # deletes. The reconciler does NOT read this: it takes the same map
        # from Redis, because intent must cross the seam through Redis and not
        # through a shared attribute.
        self.broadcaster_ids: Dict[str, int] = {}
        self._consecutive_dead_chat_polls = 0
        self.reconciler: Optional[Reconciler] = None
        self.transport: Optional[EventSubPoolTransport] = None
        # Set from the token file in initialize(). False means every chat
        # subscription is going to refuse, for one reason that has nothing to
        # do with the broadcasters.
        self.has_chat_scope = False
        self._reconciler_task: Optional[asyncio.Task] = None

    async def _on_token_refresh(self, access_token: str, refresh_token: str):
        """Callback invoked when tokens are refreshed by pyTwitchAPI."""
        logger.info("Twitch tokens refreshed, persisting to file")
        if self.credentials:
            self.credentials.persist(access_token, refresh_token)

    async def initialize(self):
        """Initialize all connections and services."""
        logger.info("Initializing Stream Monitoring Service")

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
            self.has_chat_scope = AuthScope.USER_READ_CHAT in auth_scopes
            if not self.has_chat_scope:
                logger.error(
                    "Token is missing user:read:chat -- EventSub chat subscriptions will refuse. "
                    "Re-seed with 'python seed_twitch_tokens.py' (spec 004 T017)",
                    extra={"scopes": record.scopes},
                )

            # Set user authentication with loaded tokens
            await self.twitch.set_user_authentication(
                record.access_token,
                auth_scopes,
                record.refresh_token
            )

            # Register callback for token refresh
            self.twitch.user_auth_refresh_callback = self._on_token_refresh

            logger.info("User authentication configured with pre-seeded tokens", extra={
                "scopes": record.scopes
            })

        except FileNotFoundError as e:
            logger.warning("Token file not found, running without user auth (chat will not work)", extra={
                "error": str(e)
            })
            # Fall back to app-only auth for streams API
            await self.twitch.authenticate_app([])

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
        logger.info("Redis connection initialized")

        # Build the reconciler. It reads the desired set this service writes
        # and owns every network call that used to happen inside the poll tick.
        self.transport = await self._build_transport()
        self.reconciler = Reconciler(
            transport=self.transport,
            redis_client=self.redis_client,
            config=resolve_reconciler_config(),
            # active_stream_count used to count IRC rooms. joined_channels is
            # no longer maintained, so the gauge follows the reconciler's
            # actual set -- the subscriptions that really exist.
            on_pass_complete=active_stream_count.set,
            refusal_store=self._build_refusal_store(),
        )

        # Initialize scheduler
        self.scheduler = AsyncIOScheduler()
        self.scheduler.add_job(
            self.poll_top_streams,
            "interval",
            seconds=POLL_INTERVAL_SECONDS,
            id="poll_streams",
            next_run_time=datetime.now(timezone.utc)
        )

        # Start Prometheus metrics server
        start_http_server(PROMETHEUS_PORT)
        logger.info("Prometheus metrics server started", extra={"port": PROMETHEUS_PORT})

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
            self.reconciler.invalidate_actual_set()

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
        await self.initialize()
        self.scheduler.start()

        # The reconciler is a task in this process, beside the poll job. It is
        # not a separate container: it shares this process's /health endpoint
        # and this logger.
        self._reconciler_task = asyncio.create_task(self.reconciler.run())

        logger.info("Stream Monitoring Service started")

        # Keep the service running
        while self.running:
            await asyncio.sleep(1)

    async def stop(self):
        """Gracefully stop the service."""
        logger.info("Stopping Stream Monitoring Service")
        self.running = False

        if self.scheduler:
            self.scheduler.shutdown(wait=True)

        # Stop the reconciler before Redis closes underneath it. Ask first,
        # then cancel, so a pass that is already running can finish its
        # current operation instead of leaving a half-made subscription.
        if self.reconciler is not None:
            self.reconciler.stop()
        if self._reconciler_task is not None:
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

        if self.chat is not None:
            try:
                # Chat.stop() is synchronous -- it blocks internally via
                # run_coroutine_threadsafe(...).result() until its teardown
                # coroutine finishes on the chat's own thread. `await`ing its
                # (None) return value used to raise a TypeError that this
                # except swallowed silently after the real stop had already
                # completed.
                self.chat.stop()
            except Exception as e:
                logger.warning("Error stopping chat client", extra={"error": str(e)})

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
        """Poll Twitch API for top streams and manage chat connections."""
        try:
            logger.info("Polling for top streams")

            # Fetch more than LEAVE_THRESHOLD raw streams (by viewer rank) and filter
            # out streamers with clipping disabled *before* assigning rank -- so a
            # disabled streamer near the top can't eat a rank slot it can never use.
            # See CLIPPING_DISABLED_FETCH_BUFFER above for why padding is needed.
            fetch_count = LEAVE_THRESHOLD + CLIPPING_DISABLED_FETCH_BUFFER

            async def _fetch_top_streams():
                collected = []
                # first= is the PAGE size, capped by Helix at 100. The
                # generator pages on its own, so the loop below -- not first=
                # -- is what bounds the total.
                async for stream in self.twitch.get_streams(first=HELIX_MAX_PAGE_SIZE):
                    collected.append(stream)
                    if len(collected) >= fetch_count:
                        break
                return collected

            pages, fetch_timeout = fetch_budget(fetch_count)

            try:
                raw_streams = await asyncio.wait_for(_fetch_top_streams(), timeout=fetch_timeout)
            except asyncio.TimeoutError:
                logger.error("Timed out fetching top streams from Twitch API", extra={
                    "timeout_seconds": fetch_timeout,
                    "pages": pages,
                    "fetch_count": fetch_count
                })
                twitch_api_errors_total.labels(error_type="get_streams_timeout").inc()
                return

            # Broadcasters we've already learned don't allow clip creation (via a
            # 403 from the clip-detector job) -- no point spending a chat
            # connection on a streamer we can never successfully clip.
            disabled_ids = self._get_clipping_disabled_ids([int(s.user_id) for s in raw_streams])
            disabled_logins = {s.user_login.lower() for s in raw_streams if int(s.user_id) in disabled_ids}

            # Rank only among clip-eligible streams, so JOIN_THRESHOLD/LEAVE_THRESHOLD
            # reflect real candidates rather than raw Twitch viewer position.
            streams = [s for s in raw_streams if int(s.user_id) not in disabled_ids][:LEAVE_THRESHOLD]

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

            # The membership of the last desired set IS the hysteresis state.
            # It is read back from Redis rather than held in memory, so a
            # restart keeps the retained band instead of collapsing coverage
            # to the top JOIN_THRESHOLD.
            previous_desired = self._read_previous_desired()

            for login in disabled_logins:
                if login in previous_desired:
                    logger.info("Streamer has clipping disabled, dropping from the desired set", extra={
                        "broadcaster_login": login
                    })

            ranked_logins = []
            broadcaster_ids = {}

            for rank, stream in enumerate(streams, 1):
                broadcaster_login = stream.user_login.lower()
                broadcaster_id = int(stream.user_id)
                ranked_logins.append(broadcaster_login)
                broadcaster_ids[broadcaster_login] = broadcaster_id
                self.broadcaster_ids[broadcaster_login] = broadcaster_id

                # Update Redis with TTL
                redis_key = f"streamer:online:{broadcaster_login}"
                is_new = not self.redis_client.exists(redis_key)
                self.redis_client.setex(redis_key, REDIS_STREAMER_TTL, broadcaster_id)

                # Update Postgres
                self._upsert_streamer(broadcaster_id, broadcaster_login)

                # Publish lifecycle event if new (only for top JOIN_THRESHOLD)
                if is_new and rank <= JOIN_THRESHOLD:
                    self._publish_lifecycle_event("online", broadcaster_id, broadcaster_login, rank)
                    logger.info("Streamer online", extra={
                        "broadcaster_login": broadcaster_login,
                        "broadcaster_id": broadcaster_id,
                        "rank": rank
                    })

            desired = compute_desired_set(
                ranked_logins, previous_desired, JOIN_THRESHOLD, LEAVE_THRESHOLD
            )

            # Streamers that went offline. A login leaves `desired` only when
            # it drops out of the top LEAVE_THRESHOLD, so this is the same
            # condition the join loop used to apply against joined_channels.
            for login in previous_desired:
                if login not in desired:
                    redis_key = f"streamer:online:{login}"
                    if not self.redis_client.exists(redis_key):
                        broadcaster_id = self.broadcaster_ids.get(login, 0)
                        self._publish_lifecycle_event("offline", broadcaster_id, login, 0)
                        logger.info("Streamer offline", extra={
                            "broadcaster_login": login,
                            "broadcaster_id": broadcaster_id
                        })

            # Hand the intent to the reconciler and return. No joins, no
            # subscribes, no waiting on a rate-limited bucket. This is the
            # whole point of the split: the poll must always finish well
            # inside POLL_INTERVAL_SECONDS, because APScheduler runs it with
            # max_instances=1 and a skipped poll stops refreshing the online
            # keys that expire at REDIS_STREAMER_TTL.
            self._write_desired_set(desired, broadcaster_ids)

            logger.info("Poll complete", extra={
                "ranked": len(ranked_logins),
                "desired": len(desired),
                "entered": len(set(desired) - previous_desired),
                "left": len(previous_desired - set(desired)),
            })

        except Exception as e:
            logger.error("Error polling streams", extra={"error": str(e)})
            twitch_api_errors_total.labels(error_type="poll_streams").inc()

    def _read_previous_desired(self) -> Set[str]:
        """Return the logins the last poll asked for. This is the hysteresis state."""
        return {
            login.decode("utf-8") if isinstance(login, bytes) else login
            for login in self.redis_client.zrange(DESIRED_KEY, 0, -1)
        }

    def _write_desired_set(self, desired: Dict[str, int], broadcaster_ids: Dict[str, int]):
        """Publish the desired set for the reconciler, in one transaction.

        One round trip whatever changed, so the cost of this write follows the
        SIZE of the desired set and never the size of the CHANGE (FR-003).

        DEL then ZADD, not the rank trim that data-model.md describes: a member
        that leaves the set keeps its old score, and that score is also a low
        rank, so a ZREMRANGEBYRANK would keep the stale member and evict a
        wanted one instead. The pipeline is a MULTI/EXEC, so the reconciler
        never reads the gap between the delete and the write, and the set, the
        id map, and the generation can never disagree.
        """
        pipe = self.redis_client.pipeline()
        pipe.delete(DESIRED_KEY, DESIRED_IDS_KEY)
        if desired:
            pipe.zadd(DESIRED_KEY, desired)
            pipe.hset(
                DESIRED_IDS_KEY,
                mapping={login: broadcaster_ids[login] for login in desired},
            )
        pipe.incr(DESIRED_GENERATION_KEY)
        pipe.execute()

        # The reconciler shares this event loop, so this is the fast path. Its
        # idle timeout is the backstop if the signal is ever missed.
        if self.reconciler is not None:
            self.reconciler.notify_desired_changed()

    async def _manage_chat_connections(self, join_eligible: Set[str], leave_eligible: Set[str],
                                        disabled_logins: Optional[Set[str]] = None):
        """
        Manage chat room connections with hysteresis.

        DEAD CODE ON THE POLL PATH. `poll_top_streams` no longer calls this.
        Chat membership is now intent in Redis plus `reconciler.py`. The
        function and its tests stay until Phase 3 removes IRC outright
        (tasks T029-T031), because deleting the transport is a separate step
        from moving the work off the poll tick.

        Args:
            join_eligible: Streamers in top JOIN_THRESHOLD (should join if not already joined)
            leave_eligible: Streamers in top LEAVE_THRESHOLD (should NOT leave yet)
            disabled_logins: Streamers known to have clipping disabled (for leave-reason logging)

        Hysteresis logic:
        - Join chat when streamer enters top JOIN_THRESHOLD
        - Leave chat only when streamer drops out of top LEAVE_THRESHOLD
        - This prevents thrashing and preserves Flink baseline data
        """
        disabled_logins = disabled_logins or set()

        # pyTwitchAPI's reconnect logic gives up for good after exhausting a
        # bounded backoff list (~4.25 minutes total) and never retries again.
        # From that point, join_room()/leave_room() keep failing forever with
        # "Cannot write to closing transport" against the dead socket, while
        # everything else about the process (health check, scheduler, DB)
        # still looks healthy. is_connected() reports this, but also reads
        # False during a still-in-progress reconnect the library would have
        # completed on its own -- see DEAD_CHAT_CONFIRMATION_POLLS -- so we
        # only act once it has looked dead for several consecutive polls.
        if self.chat is not None and not self.chat.is_connected():
            self._consecutive_dead_chat_polls += 1
        else:
            self._consecutive_dead_chat_polls = 0

        if self.chat is not None and self._consecutive_dead_chat_polls >= DEAD_CHAT_CONFIRMATION_POLLS:
            logger.warning("Chat connection is dead, recreating client", extra={
                "consecutive_dead_polls": self._consecutive_dead_chat_polls
            })
            try:
                self.chat.stop()
            except Exception as e:
                logger.warning("Error stopping dead chat client", extra={"error": str(e)})
            self.chat = None
            self._consecutive_dead_chat_polls = 0
            # The dead socket already dropped every room it held. Rejoin
            # everything hysteresis says should still be joined -- the
            # surviving 16-30 band too, not just newly-JOIN_THRESHOLD-eligible
            # channels -- or recovery would silently narrow coverage from
            # top-LEAVE_THRESHOLD down to top-JOIN_THRESHOLD on every outage.
            join_eligible = join_eligible | (self.joined_channels & leave_eligible)
            self.joined_channels = set()

        # Join channels for streamers who entered top JOIN_THRESHOLD
        channels_to_join = join_eligible - self.joined_channels

        # Only leave channels for streamers who dropped out of top LEAVE_THRESHOLD
        # (i.e., they're currently joined but NOT in leave_eligible set)
        channels_to_leave = self.joined_channels - leave_eligible

        # Initialize chat if needed
        if channels_to_join and not self.chat:
            try:
                # Default no_message_reset_time is 10 minutes -- far too long a
                # blind spot for a dead connection given we track 15-30 channels
                # that together produce many messages/sec; 30s gives huge margin
                # over any observed lull while catching a dead socket fast.
                self.chat = await Chat(self.twitch, no_message_reset_time=0.5)
                self.chat.register_event(ChatEvent.READY, self._on_chat_ready)
                self.chat.register_event(ChatEvent.MESSAGE, self._on_chat_message)
                self.chat.start()
                logger.info("Chat client started")
            except Exception as e:
                logger.error("Failed to start chat client", extra={"error": str(e)})
                return

        # Join new channels (entered top JOIN_THRESHOLD)
        #
        # RAMP WARNING -- this loop is the expected first bottleneck as
        # JOIN_THRESHOLD grows, and it is not a Twitch failure, it is a
        # scheduling one. join_room() waits on pyTwitchAPI's channel_join
        # bucket, which is 20 joins per 10s PER ACCOUNT and blocks rather than
        # failing. So a cold start joining N channels parks here for roughly
        # N/2 seconds, inside the poll job itself.
        #
        # Once that exceeds POLL_INTERVAL_SECONDS (120), APScheduler's default
        # max_instances=1 starts skipping the next poll entirely: "Execution of
        # job skipped: maximum number of running instances reached". Skipped
        # polls stop refreshing the Redis online keys, whose TTL is 180s, so
        # streamers begin expiring as offline and churning lifecycle events
        # while the join storm is still going.
        #
        # Rough thresholds at the 20/10s rate: ~240 channels of cold joining
        # fills one poll interval. Steady state is fine at far higher numbers,
        # because hysteresis means only a handful of channels change per poll
        # -- it is the cold start that hurts.
        #
        # DONE, spec 004 Phase 1: the poller writes the desired set to Redis
        # and `reconciler.py` converges toward it on its own task. Nothing
        # reaches this loop from the poll path any more. The paragraphs above
        # describe the failure that motivated the split; keep them until
        # Phase 3 deletes the IRC client with them.
        for channel in channels_to_join:
            try:
                await self.chat.join_room(channel)
                self.joined_channels.add(channel)
                logger.info("Joined chat room", extra={
                    "channel": channel,
                    "reason": f"entered top {JOIN_THRESHOLD}"
                })
            except Exception as e:
                logger.error("Failed to join chat room", extra={"channel": channel, "error": str(e)})

        # Leave channels (dropped out of top LEAVE_THRESHOLD, or clipping disabled)
        for channel in channels_to_leave:
            try:
                await self.chat.leave_room(channel)
                self.joined_channels.discard(channel)
                reason = "clipping disabled" if channel in disabled_logins else f"exited top {LEAVE_THRESHOLD}"
                logger.info("Left chat room", extra={
                    "channel": channel,
                    "reason": reason
                })
            except Exception as e:
                logger.error("Failed to leave chat room", extra={"channel": channel, "error": str(e)})

    async def _on_chat_ready(self, ready_event: EventData):
        """Handle chat ready event."""
        logger.info("Chat client ready")

    async def _on_chat_message(self, msg: ChatMessage):
        """Handle incoming chat messages."""
        try:
            broadcaster_login = msg.room.name.lower()
            broadcaster_id = self.broadcaster_ids.get(broadcaster_login)

            if not broadcaster_id:
                return

            # Build message payload
            message_payload = {
                "broadcaster_id": broadcaster_id,
                "timestamp": int(time.time() * 1000),   # ingestion clock, unchanged
                "sent_at": msg.sent_timestamp,           # Twitch server clock, from tmi-sent-ts
                "message_id": str(uuid.uuid4()),
                "text": msg.text,
                "user_id": int(msg.user.id) if msg.user.id else 0,
                "user_name": msg.user.name,
                "metadata": {
                    "emotes": {},
                    "badges": dict(msg.user.badges) if msg.user.badges else {},
                    "is_subscriber": msg.user.subscriber,
                    "is_mod": msg.user.mod
                }
            }

            # Publish to Kafka
            self._publish_chat_message(broadcaster_id, message_payload)

            # Update metrics
            chat_messages_total.labels(broadcaster_id=str(broadcaster_id)).inc()

        except Exception as e:
            logger.error("Error processing chat message", extra={"error": str(e)})

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

    def _upsert_streamer(self, streamer_id: int, streamer_login: str):
        """Insert or update streamer in Postgres."""
        conn = None
        try:
            conn = self.db_pool.getconn()
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO streamers (streamer_id, streamer_login, last_seen_at)
                    VALUES (%s, %s, NOW())
                    ON CONFLICT (streamer_id) DO UPDATE
                    SET streamer_login = EXCLUDED.streamer_login,
                        last_seen_at = NOW()
                """, (streamer_id, streamer_login))
                conn.commit()
        except Exception as e:
            logger.error("Failed to upsert streamer", extra={
                "streamer_id": streamer_id,
                "error": str(e)
            })
            if conn:
                conn.rollback()
        finally:
            if conn:
                self.db_pool.putconn(conn)

    def _get_clipping_disabled_ids(self, streamer_ids: List[int]) -> Set[int]:
        """Return the subset of streamer_ids to drop from the ranking.

        `allows_clipping = FALSE` used to be permanent: a broadcaster who
        turned clip creation off once was never looked at again, even after
        turning it back on. A mark older than CLIPPING_RECHECK_DAYS is now
        treated as stale and left OUT of this set, so the broadcaster
        re-enters the ranking and the Flink job gets one more real attempt.
        That attempt either succeeds -- `mark_clipping_allowed` clears the
        flag -- or refuses again and resets the timestamp (FR-013, D5).

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

    def signal_handler():
        logger.info("Received shutdown signal")
        asyncio.create_task(service.stop())

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, signal_handler)

    # Start health check server
    await run_health_check_server()

    # Start main service
    try:
        await service.start()
    except Exception as e:
        logger.error("Service error", extra={"error": str(e)})
        await service.stop()
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
