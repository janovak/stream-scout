#!/usr/bin/env python3
"""
EventSub websocket pool -- the real `SubscriptionTransport`.

`reconciler.py` decides WHICH channels must be subscribed. This module decides
WHERE each subscription lives and turns the events back into the Kafka payload
the Flink job already consumes. The reconciler does not know how many sockets
there are, and it must not: everything about connections, the 300-per-socket
cap and routing stays behind `SubscriptionTransport`.

Why a pool at all
-----------------
One `EventSubWebsocket` is one Twitch session, and a session holds at most 300
subscriptions (measured, spec 003 research §1). 500 channels therefore need
two sockets, and the number has to grow on its own or the service breaks
silently the first time the monitored set is raised.

Routing: rendezvous hashing, not modulo (D6)
--------------------------------------------
A channel must land on the same connection across reconciles, so that a socket
death costs only that socket's subscriptions and not a full reshuffle.
`hash(id) % len(connections)` does not give that: growing from one socket to
two moves about half of ALL channels. Rendezvous hashing (highest random
weight) scores every (channel, connection) pair and takes the best-scoring
connection with room. Adding a connection moves only the ~1/N channels whose
score is now highest on the new one, and removing a connection moves only the
channels that were on it. That is exactly the property D6 asks for.

Connection identity is a monotonic counter, never a list index, so retiring a
dead connection does not renumber the survivors and re-route their channels.

Occupancy is local (T019)
-------------------------
The per-connection `enabled` count reported by the library is wrong -- the
Phase 0 spike saw `get_eventsub_subscriptions().total` report 300 while the
pages held 396. Occupancy here is `len(connection.subscription_ids)`,
maintained by `create` and `delete`, and it is never re-read from the library.
`list()` counts pages for the same reason and never reads `total`.

The message path
----------------
`SubscriptionTransport` is create/delete/list only -- it says nothing about
receiving. The pool takes a `message_handler` and calls it once per chat
event. `stream_monitoring_service.py` passes a handler that runs
`map_chat_message` and hands the result to the existing Kafka producer path.
The mapping is a module-level pure function so it can be tested without a
socket, a Twitch client or a producer (T020, T021).

Callbacks run on each socket's own asyncio loop, which is the library default.
Do NOT pass `callback_loop`: the library calls `loop.create_task()` on it from
the socket thread, which is not thread-safe. The handler must therefore be
safe to call from several socket threads at once. Publishing through
confluent-kafka is -- `produce()` and `poll()` are thread-safe, and one
producer is shared exactly as the IRC path shared it.
"""

import asyncio
import hashlib
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Awaitable, Callable, Dict, List, Optional, Set

from twitchAPI.eventsub.websocket import EventSubWebsocket
from twitchAPI.type import (
    AuthType,
    EventSubSubscriptionConflict,
    EventSubSubscriptionError,
    TwitchAPIException,
    TwitchBackendException,
    TwitchResourceNotFound,
)

from reconciler import (
    ExistingSubscription,
    RateLimitedError,
    SubscriptionRefusedError,
    SubscriptionTransport,
    TransportError,
)

logger = logging.getLogger("stream_monitoring")

# Measured cap for one websocket session. Twitch documents 300 for a websocket
# transport and the spike confirmed it.
SUBSCRIPTIONS_PER_CONNECTION = 300

# The only subscription type this pool creates. Also the filter for `list()`,
# so a subscription made by something else never enters the actual set.
CHAT_MESSAGE_SUBSCRIPTION_TYPE = "channel.chat.message"

# How often the supervisor looks for a socket that has stopped receiving.
DEFAULT_SUPERVISE_INTERVAL_SECONDS = 15.0

# How long to wait for a new session to say session_welcome. A healthy connect
# takes well under a second. The library's own retry ladder runs to 255 s and
# it does not fail cleanly at the end of it, so the wait is bounded here.
DEFAULT_CONNECT_TIMEOUT_SECONDS = 30.0
# How long `aclose()` lets in-flight socket callbacks land before returning,
# so the caller's Kafka flush can carry the chat they produced. The library's
# own `_stop()` sleeps 0.25 s for the same reason.
SOCKET_DRAIN_SECONDS = 0.25

# `_subscribe` throws away the HTTP status and keeps only Twitch's message, so
# the kind of failure has to be read back out of the text. Phase 0's throwaway
# harness matched "too many" for a 429 and that held over 976 of them; the
# other spellings are here so a wording change degrades into "retry next pass"
# rather than "this channel is permanently refused".
_REFUSAL_MARKERS = ("missing proper authorization", "not authorized", "forbidden")
_RATE_LIMIT_MARKERS = ("too many", "rate limit", "429", "exceeded")
# A full session answers 400, not 429. Retrying that channel on the same
# socket can never work, so it must not look like a rate limit -- and the
# rate-limit list below matches the very generic "exceeded", which a full
# message could easily contain. These are checked FIRST for that reason.
#
# Nobody has seen Twitch's actual wording at the 301st subscription; the spike
# measured the count, not the message. So the list is deliberately wide. The
# trade changed when `full_at` replaced the old permanent `full` flag: a false
# positive now only skips the connection until deletes bring it below that
# level, while a false negative classifies the error as a 429 and burns the
# reconciler's whole retry budget -- 20 rounds of 10 s backoff -- on a create
# that can never succeed.
#
# `"websocket session"` was in this list and had to come out. It matches
# `EventSubSubscriptionError: websocket session has already disconnected`,
# which `research.md` records the library raising twice from its own
# `_resubscribe` during a 500-channel ramp -- a transient reconnect race, not
# a full session. Marking that connection full was wrong, and at occupancy 0
# it stranded the socket. Every marker here must name a LIMIT.
_SESSION_FULL_MARKERS = (
    "subscription limit",
    "too many subscriptions",
    "maximum number of subscriptions",
    "subscriptions per websocket",
    "session limit",
)


def _score(broadcaster_id: int, connection_id: int) -> int:
    """Rendezvous weight for one (channel, connection) pair.

    A cryptographic digest, not the built-in `hash()`: `hash()` of a str is
    salted per process, so routing would change on every restart and a
    restart would reshuffle every channel across the pool.
    """
    digest = hashlib.blake2b(
        f"{broadcaster_id}:{connection_id}".encode("utf-8"), digest_size=8
    ).digest()
    return int.from_bytes(digest, "big")


def to_epoch_ms(value) -> Optional[int]:
    """Convert the envelope timestamp to epoch milliseconds.

    `sent_at` drives Flink's event time through `SentAtTimestampAssigner`, and
    the contract says it is an int or `null` -- never a string, or the
    assigner silently falls back to record time (contract invariant 2).

    pyTwitchAPI has already parsed `metadata.message_timestamp` into a
    tz-aware `datetime` by the time an event reaches us. The string branch is
    for tests and for any caller holding the raw envelope.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        moment = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        # Truncation, not rounding. Phase 0 T001 measured this quantity against
        # IRC's integer `tmi-sent-ts` over 24,473 messages and found the two
        # within 0-1 ms, so either choice is far inside the 2 s watermark.
        return int(moment.timestamp() * 1000)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        # Twitch sends up to 9 fractional digits. datetime.fromisoformat on
        # Python 3.10 accepts only 3 or 6, so trim the fraction.
        if "." in text:
            head, _, tail = text.partition(".")
            digits = ""
            while tail and tail[0].isdigit():
                digits, tail = digits + tail[0], tail[1:]
            text = f"{head}.{digits[:6]:0<6}{tail}"
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp() * 1000)
    raise TypeError(f"cannot convert {type(value).__name__} to epoch milliseconds")


def map_chat_message(event, *, received_at_ms: Optional[int] = None) -> dict:
    """Map a `ChannelChatMessageEvent` onto the `chat-messages` schema.

    The schema is `contracts/chat-messages.schema.md`, and FR-008 requires it
    to stay byte-compatible with what IRC published, so the Flink job does not
    need a change. `test_stream_monitoring.py` asserts the two mappings give
    the same keys and the same types (T021).

    Two things EventSub makes easier than IRC:

    - `broadcaster_user_id` arrives on the event, so there is no
      login-to-id lookup and no way to drop a message because the map was
      not populated yet.
    - `message_id` is Twitch's own UUID rather than one generated here, so a
      duplicate delivery is recognisable downstream.

    `emotes` stays `{}`. IRC never populated it and starting now would change
    the payload for the Flink job in a feature that promises not to.
    """
    data = event.event
    badges = list(getattr(data, "badges", None) or [])
    chatter_id = getattr(data, "chatter_user_id", None)
    message_id = getattr(data, "message_id", None)

    return {
        "broadcaster_id": int(data.broadcaster_user_id),
        # Ingestion clock, unchanged from the IRC path.
        "timestamp": int(time.time() * 1000) if received_at_ms is None else received_at_ms,
        # Twitch's send clock. T001 proved this is the same instant IRC's
        # `tmi-sent-ts` carried, to the millisecond -- there is no offset to
        # correct here (D3).
        "sent_at": to_epoch_ms(event.metadata.message_timestamp),
        "message_id": message_id or str(uuid.uuid4()),
        "text": data.message.text,
        "user_id": int(chatter_id) if chatter_id else 0,
        "user_name": data.chatter_user_login,
        "metadata": {
            "emotes": {},
            "badges": {badge.set_id: badge.id for badge in badges},
            "is_subscriber": any(badge.set_id == "subscriber" for badge in badges),
            "is_mod": any(badge.set_id == "moderator" for badge in badges),
        },
    }


@dataclass
class _Connection:
    """One websocket session and the subscriptions it holds."""

    connection_id: int
    websocket: Any
    subscription_ids: Set[str] = field(default_factory=set)
    # Slots handed out to in-flight creates. Ten workers can be routing at
    # once, so the cap has to count the creates that have not landed yet or
    # the pool oversubscribes a session and Twitch rejects the overflow.
    reserved: int = 0
    # Set when Twitch says the session is full at a lower number than the cap
    # this module believes in: the occupancy it refused at. Routing skips the
    # connection while it still holds that many, and takes it back once
    # deletes bring it below. This used to be a bool that nothing ever
    # cleared, so one report retired a socket from routing for the life of
    # the process -- under ordinary hysteresis churn the pool then opened
    # fresh sockets while drained ones sat idle and unusable.
    full_at: Optional[int] = None

    @property
    def occupancy(self) -> int:
        return len(self.subscription_ids)

    @property
    def load(self) -> int:
        return len(self.subscription_ids) + self.reserved


@dataclass(frozen=True)
class _Slot:
    """Where one broadcaster's subscription lives."""

    broadcaster_id: int
    connection_id: int
    subscription_id: str
    # The websocket session this subscription was made on. A reconnect gives
    # the connection a NEW session, and everything Twitch held on the old one
    # is gone -- so a slot whose session no longer matches its connection's is
    # stale whatever any local registry says. This is the only check that does
    # not depend on the library telling the truth about what it holds.
    session_id: Optional[str] = None


class EventSubPoolTransport(SubscriptionTransport):
    """A growing pool of `EventSubWebsocket` sessions behind one transport.

    `message_handler` is an async callable invoked once per chat event, with
    the raw `ChannelChatMessageEvent`. It runs on the receiving socket's own
    event loop, so it must not assume the service's loop and must not block.
    """

    def __init__(
        self,
        twitch,
        message_handler: Callable[[Any], Awaitable[None]],
        *,
        user_id: Optional[str] = None,
        cap: int = SUBSCRIPTIONS_PER_CONNECTION,
        connection_factory: Optional[Callable[[], Any]] = None,
        on_subscriptions_lost: Optional[Callable[[int], None]] = None,
        supervise_interval_seconds: float = DEFAULT_SUPERVISE_INTERVAL_SECONDS,
        connect_timeout_seconds: float = DEFAULT_CONNECT_TIMEOUT_SECONDS,
    ):
        self.twitch = twitch
        self.message_handler = message_handler
        self.user_id = user_id
        self.cap = cap
        self._connection_factory = connection_factory or self._default_connection_factory
        self.on_subscriptions_lost = on_subscriptions_lost
        self.supervise_interval_seconds = supervise_interval_seconds
        self.connect_timeout_seconds = connect_timeout_seconds
        # Monotonic deadline after a failed `_grow`, so the rest of a batch
        # fails fast instead of each channel waiting out its own connect.
        self._growth_blocked_until = 0.0

        self._connections: List[_Connection] = []
        self._next_connection_id = 0
        self._slots: Dict[int, _Slot] = {}
        self._by_subscription: Dict[str, _Slot] = {}
        # Serialises routing, reservations and growth. Held only around
        # bookkeeping and the one blocking `start()`, never around a create.
        self._lock = asyncio.Lock()
        self._supervisor: Optional[asyncio.Task] = None
        # The service's event loop. Callbacks arrive on a socket's own loop,
        # on another thread, and must hop back here before touching anything
        # this object owns.
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def _default_connection_factory(self):
        return EventSubWebsocket(self.twitch, revocation_handler=self._on_revocation)

    # -- lifecycle --------------------------------------------------------

    async def start(self):
        """Resolve the auth user and start watching for socket death.

        No connection is opened here. Twitch closes a session that has no
        subscription within ten seconds, so a connection is only opened when
        there is a channel to put on it.
        """
        self._loop = asyncio.get_running_loop()
        if self.user_id is None:
            async for user in self.twitch.get_users():
                self.user_id = user.id
                break
        if self.user_id is None:
            raise TransportError("could not resolve the authenticated user id")
        if self._supervisor is None:
            self._supervisor = asyncio.create_task(self._supervise())
        logger.info(
            "EventSub pool ready",
            extra={"user_id": self.user_id, "cap": self.cap},
        )

    async def aclose(self):
        """Stop the supervisor and close every live session."""
        if self._supervisor is not None:
            self._supervisor.cancel()
            try:
                await self._supervisor
            except asyncio.CancelledError:
                pass
            self._supervisor = None
        # `_retire`, not `websocket.stop()`. `stop()` blocks on a future the
        # socket's own loop has to complete, so a session whose loop is wedged
        # -- half-open TCP, a failed close -- hangs SIGTERM shutdown for ever
        # with this service's event loop frozen. `_retire` schedules the same
        # teardown on the socket's loop without awaiting it, which is why it
        # exists; shutdown has no more reason to block on a dead socket than
        # the supervisor does.
        for connection in list(self._connections):
            try:
                self._retire(connection)
            except Exception as e:
                logger.warning(
                    "Error stopping an EventSub connection",
                    extra={"connection": connection.connection_id, "error": str(e)},
                )
        self._connections = []
        self._slots = {}
        self._by_subscription = {}
        # `_retire` schedules each socket's teardown on its own loop and does
        # not await it, so delivery has not actually stopped when this returns.
        # An event already dispatched runs the message handler -- and its
        # `producer.produce()` -- after the caller's `flush()` has returned,
        # and that record dies with the process. Give those callbacks a moment
        # to land in the producer's queue, so the flush that follows carries
        # them. Bounded, because shutdown must not hang on a wedged socket.
        await asyncio.sleep(SOCKET_DRAIN_SECONDS)

    # -- SubscriptionTransport --------------------------------------------

    async def create(self, broadcaster_id: int) -> str:
        """Subscribe to one channel's chat, on the connection it routes to."""
        existing = self._slots.get(broadcaster_id)
        existing_connection = (
            self._connection_by_id(existing.connection_id) if existing is not None else None
        )
        if existing is not None and existing_connection is not None:
            # A live connection is not proof of a live subscription. When the
            # library's `_resubscribe()` gives up part way through a reconnect
            # the socket stays up while the channels past the failure point no
            # longer exist on Twitch, and `_slots` still maps them to their
            # pre-reconnect ids. Returning the recorded id here made no Twitch
            # call, so the periodic re-adoption would drop the channel from
            # `_actual`, ask for it again, be handed the ghost straight back,
            # and count it as covered for ever. Check the library's registry,
            # which is the only local record of what the socket really holds.
            if self._slot_is_current(existing, existing_connection, broadcaster_id):
                return existing.subscription_id
            logger.warning(
                "Recorded subscription is not on its connection any more, recreating",
                extra={
                    "broadcaster_id": broadcaster_id,
                    "connection": existing_connection.connection_id,
                    "subscription_id": existing.subscription_id,
                },
            )
            self._forget_slot(existing)
            existing = None
        if existing is not None:
            # The slot points at a connection that is gone. Returning its id
            # would report the channel as covered while no socket delivers
            # for it -- dark, and permanently so, because nothing else clears
            # a slot whose connection has already been retired.
            self._forget_slot(existing)

        connection = await self._reserve(broadcaster_id)
        subscription_id = None
        try:
            subscription_id = await connection.websocket.listen_channel_chat_message(
                str(broadcaster_id), self.user_id, self._on_event
            )
        except EventSubSubscriptionConflict:
            # The interface says a duplicate create adopts rather than fails
            # (FR-005). Twitch answers 409 when this exact subscription is
            # already there, which happens whenever the actual set is stale.
            return await self._adopt_conflict(broadcaster_id)
        except EventSubSubscriptionError as e:
            raise self._classify(connection, e)
        except TwitchBackendException as e:
            # Twitch's own 500. The channel keeps its place and the next pass
            # tries again.
            raise TransportError(f"Twitch backend error: {e}") from e
        except TwitchAPIException as e:
            raise TransportError(str(e)) from e
        finally:
            # Only the failure paths release here. On success the reservation
            # is given up in the SAME critical section that records the
            # subscription, below. Releasing it first drops `load` by one
            # before `subscription_ids` grows, and `_release` and the record
            # block take the lock separately -- so another worker routing in
            # that window sees a free slot that is already spoken for and
            # pushes the session one past the 300 cap. Twitch then refuses,
            # and the connection is marked full at the cap it was already at.
            if subscription_id is None:
                await self._release(connection)

        async with self._lock:
            connection.reserved = max(0, connection.reserved - 1)
            if self._connection_by_id(connection.connection_id) is None:
                # The supervisor retired this connection while the create was
                # in flight -- it runs on this loop and the create above is an
                # await. Recording the slot now would re-add an entry
                # `_retire` has already cleared, and every later create would
                # hand back that dead id without ever contacting Twitch.
                logger.warning(
                    "Connection was retired mid-create, discarding the subscription",
                    extra={
                        "broadcaster_id": broadcaster_id,
                        "connection": connection.connection_id,
                        "subscription_id": subscription_id,
                    },
                )
                raise TransportError(
                    f"connection {connection.connection_id} was lost while subscribing "
                    f"broadcaster {broadcaster_id}"
                )
            slot = _Slot(
                broadcaster_id,
                connection.connection_id,
                subscription_id,
                self._session_id(connection),
            )
            connection.subscription_ids.add(subscription_id)
            self._slots[broadcaster_id] = slot
            self._by_subscription[subscription_id] = slot
        return subscription_id

    async def delete(self, subscription_id: str) -> None:
        """Remove a subscription. Already gone is success, not an error (T024).

        After a socket loses its keepalive the library reconnects and
        re-subscribes everything, which gives every subscription on that
        socket a NEW id. The id the reconciler holds is then stale, and
        deleting it would answer "not found" while the real subscription kept
        delivering. So the live id is resolved from the connection before the
        delete, and the stale one is only a fallback.
        """
        slot = self._by_subscription.get(subscription_id)
        if slot is None:
            # An id this pool does not recognise, which is not the same as an
            # id that is not ours. After a reconnect rotates the ids on a
            # socket, `list()` reports the NEW ones and the reconciler asks to
            # delete one of those, while `_by_subscription` still holds the
            # old. Deleting it and stopping there would leave the library's
            # own registry intact -- so the socket re-creates the channel on
            # its next reconnect -- and leave the pool's occupancy counting a
            # subscription that no longer exists.
            await self._delete_one(subscription_id)
            self._forget_unrecognised(subscription_id)
            return

        connection = self._connection_by_id(slot.connection_id)
        targets = self._live_subscription_ids(connection, slot)
        for target in targets:
            await self._delete_one(target)
            if connection is not None:
                self._forget_library_subscription(connection, target)

        async with self._lock:
            if connection is not None:
                connection.subscription_ids.discard(subscription_id)
                for target in targets:
                    connection.subscription_ids.discard(target)
            self._slots.pop(slot.broadcaster_id, None)
            self._by_subscription.pop(subscription_id, None)

    async def list(self) -> AsyncIterator[ExistingSubscription]:
        """Yield the chat subscriptions that live on a session this pool holds.

        Page count, never `total` -- the spike saw `total` report 300 while
        the pages held 396 (D6). The library paginates transparently, so the
        count comes from what actually arrives.

        Subscriptions on any other session are skipped. A websocket session
        dies with the process that opened it, so one this pool does not hold
        can never deliver a message to it: counting it in the actual set would
        leave the channel silently dark. Twitch collects the leftovers itself.
        """
        live_sessions = self._live_session_ids()
        result = await self.twitch.get_eventsub_subscriptions(
            sub_type=CHAT_MESSAGE_SUBSCRIPTION_TYPE, target_token=AuthType.USER
        )

        seen = 0
        yielded = 0
        pages = 1
        cursor = self._cursor_of(result)
        async for subscription in result:
            moved = self._cursor_of(result)
            if moved != cursor:
                pages += 1
                cursor = moved
            seen += 1
            transport = getattr(subscription, "transport", None) or {}
            if transport.get("session_id") not in live_sessions:
                continue
            broadcaster_id = (getattr(subscription, "condition", None) or {}).get(
                "broadcaster_user_id"
            )
            if broadcaster_id is None:
                continue
            yielded += 1
            yield ExistingSubscription(
                subscription_id=subscription.id,
                broadcaster_id=int(broadcaster_id),
                status=subscription.status,
            )

        logger.info(
            "Enumerated EventSub subscriptions",
            extra={
                "pages": pages,
                "seen": seen,
                "on_our_sessions": yielded,
                "connections": len(self._connections),
            },
        )

    def occupancy(self) -> Dict[str, int]:
        """Subscriptions per connection, counted locally (T019).

        Never taken from the library's per-connection view, which the spike
        measured as wrong.
        """
        return {
            str(connection.connection_id): connection.occupancy
            for connection in self._connections
        }

    # -- routing and growth -----------------------------------------------

    def route(self, broadcaster_id: int) -> Optional[_Connection]:
        """The connection this channel belongs on, or None if the pool is full.

        Public because the routing rule is the part worth testing directly
        (T019a): the same broadcaster must come back to the same connection
        across reconciles.
        """
        ordered = sorted(
            self._connections,
            key=lambda connection: _score(broadcaster_id, connection.connection_id),
            reverse=True,
        )
        for connection in ordered:
            if connection.full_at is not None and connection.load >= connection.full_at:
                continue
            if connection.load < self.cap:
                return connection
        return None

    async def _reserve(self, broadcaster_id: int) -> _Connection:
        """Pick the connection for this channel and hold a slot on it."""
        async with self._lock:
            connection = self.route(broadcaster_id)
            if connection is None:
                # Growth runs under the lock, and a connect can take up to
                # `connect_timeout_seconds` to give up. Without the guard below
                # every remaining channel in the batch queued behind its own
                # 30 s attempt, one after another: 200 channels waiting on a
                # hung Twitch handshake froze the reconciler for about an hour,
                # with `reconcile_last_success_timestamp` stopped throughout.
                # One failure now fails the rest of the batch fast, and the
                # next pass tries again.
                if time.monotonic() < self._growth_blocked_until:
                    raise TransportError(
                        "pool growth failed recently, not retrying this pass"
                    )
                try:
                    connection = await self._grow()
                except Exception:
                    self._growth_blocked_until = (
                        time.monotonic() + self.connect_timeout_seconds
                    )
                    raise
            connection.reserved += 1
            return connection

    async def _release(self, connection: _Connection):
        async with self._lock:
            connection.reserved = max(0, connection.reserved - 1)

    async def _grow(self) -> _Connection:
        """Open one more session. Called with the lock held.

        `EventSubWebsocket.start()` blocks the calling thread until the
        session_welcome arrives, so it runs on the default executor rather
        than stalling the service's event loop for the length of a connect.
        """
        websocket = self._connection_factory()
        loop = asyncio.get_running_loop()
        try:
            await asyncio.wait_for(
                loop.run_in_executor(None, websocket.start),
                timeout=self.connect_timeout_seconds,
            )
        except asyncio.TimeoutError as e:
            # `start()` busy-waits on `_startup_complete`, which only
            # `_handle_welcome` ever sets. If the socket thread died on its
            # way up -- `_connect` gives up after a 255 s retry ladder and
            # raises -- that flag is never set and `start()` spins for the
            # life of the process. The future would never resolve, this lock
            # would never be released, and every later create would block on
            # it: one failed connect would freeze the reconciler for good.
            # Setting the flag releases the busy-wait so the worker thread
            # ends instead of spinning.
            #
            # That alone is not enough. `_keep_loop_alive()` runs on the
            # socket's OWN loop and spins on `while not self._closing`, and
            # only `_stop()` ever sets `_closing`. Without the teardown below
            # every timed-out connect left a thread spinning at 10 Hz for the
            # life of the process, holding an open `ClientSession` and its file
            # descriptors -- and invisibly, because the connection is never
            # appended to `self._connections`, so `reap_dead_connections()`
            # could not see it either.
            websocket._startup_complete = True
            websocket._running = False
            self._tear_down_socket(websocket)
            raise TransportError(
                f"EventSub connection did not come up within "
                f"{self.connect_timeout_seconds}s"
            ) from e
        except Exception as e:
            raise TransportError(f"could not open an EventSub connection: {e}") from e

        connection = _Connection(connection_id=self._next_connection_id, websocket=websocket)
        self._next_connection_id += 1
        self._connections.append(connection)
        logger.info(
            "Opened an EventSub connection",
            extra={
                "connection": connection.connection_id,
                "connections": len(self._connections),
                "cap": self.cap,
            },
        )
        return connection

    def _connection_by_id(self, connection_id: int) -> Optional[_Connection]:
        for connection in self._connections:
            if connection.connection_id == connection_id:
                return connection
        return None

    # -- events -----------------------------------------------------------

    async def _on_event(self, event):
        """Hand one chat event to the service. Runs on the socket's own loop."""
        try:
            await self.message_handler(event)
        except Exception as e:
            # A handler that raises would otherwise only surface in the
            # library's done-callback, as an unrelated traceback.
            logger.error(
                "Chat message handler failed",
                extra={"error": str(e), "error_type": type(e).__name__},
            )

    async def _on_revocation(self, payload: dict):
        """Twitch withdrew a subscription. Runs on the socket's own loop.

        A revocation -- the broadcaster removed authorization, or the channel
        went away -- silently stops delivery while every count still says the
        channel is covered. Forget it locally and report the loss, so the
        reconciler re-enumerates and either re-creates the channel or learns
        that it now refuses.

        `asyncio.Event.set()` and dict mutation are not safe from another
        thread, so the work hops to the service's loop rather than running
        here.
        """
        subscription = payload.get("subscription") or {}
        subscription_id = subscription.get("id")
        if not subscription_id or self._loop is None:
            return
        # Take the broadcaster from the payload. The library pops the id out of
        # `_active_subscriptions` and `_callbacks` BEFORE it calls this handler
        # (`_handle_revocation`), so by now no local registry can resolve it --
        # a lookup there is guaranteed to miss. Twitch sends the whole
        # subscription object, condition included, so the channel is right here.
        condition = subscription.get("condition") or {}
        broadcaster_id = condition.get("broadcaster_user_id")
        self._loop.call_soon_threadsafe(
            self._forget_revoked,
            subscription_id,
            subscription.get("status"),
            broadcaster_id,
        )

    def _forget_revoked(
        self,
        subscription_id: str,
        status: Optional[str],
        broadcaster_id: Optional[str] = None,
    ):
        slot = self._by_subscription.pop(subscription_id, None)
        if slot is None:
            # An id this pool does not recognise is NOT an id that is not ours.
            # A reconnect makes the library re-create every subscription on the
            # socket with new ids (`_resubscribe`), and the pool keeps the ids
            # it recorded at create time -- `delete()` resolves the live id
            # lazily for exactly that reason. So a revocation that arrives
            # after a reconnect names an id `_by_subscription` has never seen.
            # Returning here dropped the loss on the floor: nothing discarded
            # the channel, nothing invalidated the reconciler, and every count
            # went on reporting it as covered while no socket delivered for it.
            slot = self._slot_for_broadcaster(broadcaster_id)
        if slot is None:
            logger.error(
                "Subscription revoked by Twitch, but the channel could not be "
                "identified -- invalidating so the next pass re-enumerates",
                extra={"subscription_id": subscription_id, "status": status},
            )
            # The channel is unknown, so the only safe move is to make the
            # reconciler rebuild its view from Twitch.
            if self.on_subscriptions_lost is not None:
                self.on_subscriptions_lost(1)
            return
        self._slots.pop(slot.broadcaster_id, None)
        # Both ids. When the slot came back from the rotated-id lookup, the one
        # Twitch revoked is NOT the one occupancy was counted under, so
        # discarding only the revoked id would leave the channel in the count
        # for ever -- the same dark-but-counted state this method exists to
        # prevent.
        self._by_subscription.pop(slot.subscription_id, None)
        connection = self._connection_by_id(slot.connection_id)
        if connection is not None:
            connection.subscription_ids.discard(subscription_id)
            connection.subscription_ids.discard(slot.subscription_id)
        logger.warning(
            "Subscription revoked by Twitch",
            extra={
                "broadcaster_id": slot.broadcaster_id,
                "subscription_id": subscription_id,
                "status": status,
            },
        )
        if self.on_subscriptions_lost is not None:
            self.on_subscriptions_lost(1)

    # -- errors -----------------------------------------------------------

    def _classify(self, connection: _Connection, error: Exception) -> Exception:
        """Turn Twitch's error text into the exception the reconciler expects.

        `_subscribe` in pyTwitchAPI 4.5.0 keeps only the message, so the HTTP
        status is not available here.
        """
        message = str(error) or ""
        lowered = message.lower()

        if any(marker in lowered for marker in _REFUSAL_MARKERS):
            # 403. The reconciler counts it and T025 makes it durable.
            return SubscriptionRefusedError(message)
        if any(marker in lowered for marker in _SESSION_FULL_MARKERS):
            # Twitch says this session is full below the cap this module
            # believes in. Retrying the same channel would route it straight
            # back here, so take the connection out of routing and let the
            # next pass place the channel elsewhere.
            if connection.occupancy == 0:
                # Full while holding nothing: this session can never carry a
                # channel. Skipping it in `route()` was not enough -- nothing
                # else would ever look at it again, `_is_dead()` cannot flag it
                # because the library still thinks the socket is healthy, and
                # so its thread, event loop and ClientSession leaked for the
                # life of the process while `_grow()` opened a replacement.
                # Retire it properly instead.
                logger.error(
                    "EventSub connection reported full while empty, retiring it",
                    extra={"connection": connection.connection_id, "error": message},
                )
                self._retire(connection)
                return TransportError(f"connection unusable: {message}")
            # Remember the level it refused at, not a permanent flag, so
            # deletes can bring the connection back into routing.
            connection.full_at = connection.occupancy
            logger.error(
                "EventSub connection reported full below the configured cap",
                extra={
                    "connection": connection.connection_id,
                    "occupancy": connection.occupancy,
                    "full_at": connection.full_at,
                    "cap": self.cap,
                    "error": message,
                },
            )
            return TransportError(f"connection full: {message}")
        if any(marker in lowered for marker in _RATE_LIMIT_MARKERS):
            # 429. No Retry-After survives the library, so the reconciler
            # falls back to its configured backoff (D2).
            return RateLimitedError(message)

        # Nothing matched. The marker lists are string matches against wording
        # nobody has seen from a genuinely full websocket session -- the spike
        # measured the 300 count, not the message Twitch sends at 301 -- so a
        # miss here is the likeliest way this classifier is wrong. Log the raw
        # text at WARNING: an unclassified full-session error routes the
        # channel straight back to the same saturated socket every pass, and
        # this line is what turns that into a five-minute diagnosis.
        logger.warning(
            "Unclassified EventSub subscription error, treating it as retryable",
            extra={
                "connection": connection.connection_id,
                "occupancy": connection.occupancy,
                "error": message,
            },
        )
        return TransportError(message)

    async def _adopt_conflict(self, broadcaster_id: int) -> str:
        """Answer a 409 with the id of the subscription that already exists."""
        slot = self._slots.get(broadcaster_id)
        if slot is not None:
            return slot.subscription_id

        live_sessions = self._live_session_ids()
        result = await self.twitch.get_eventsub_subscriptions(
            sub_type=CHAT_MESSAGE_SUBSCRIPTION_TYPE, target_token=AuthType.USER
        )
        async for subscription in result:
            condition = getattr(subscription, "condition", None) or {}
            if condition.get("broadcaster_user_id") != str(broadcaster_id):
                continue
            transport = getattr(subscription, "transport", None) or {}
            session_id = transport.get("session_id")
            if session_id not in live_sessions:
                continue
            connection = self._connection_by_session(session_id)
            if connection is None:
                continue
            slot = _Slot(
                broadcaster_id,
                connection.connection_id,
                subscription.id,
                self._session_id(connection),
            )
            connection.subscription_ids.add(subscription.id)
            self._slots[broadcaster_id] = slot
            self._by_subscription[subscription.id] = slot
            logger.info(
                "Adopted a conflicting subscription",
                extra={
                    "broadcaster_id": broadcaster_id,
                    "subscription_id": subscription.id,
                    "connection": connection.connection_id,
                },
            )
            return subscription.id

        # It exists somewhere this pool cannot receive from. Do not claim it:
        # the channel stays out of the actual set and the next pass retries.
        raise TransportError(
            f"conflict for broadcaster {broadcaster_id}, but no matching "
            "subscription on a session this pool holds"
        )

    # -- deletes ----------------------------------------------------------

    async def _delete_one(self, subscription_id: str) -> None:
        try:
            await self.twitch.delete_eventsub_subscription(
                subscription_id, target_token=AuthType.USER
            )
        except TwitchResourceNotFound:
            # Expected. A subscription whose socket has gone lingers as
            # `websocket_disconnected` and answers "not found" on DELETE,
            # until Twitch collects it. That is success, not failure (T024).
            logger.debug(
                "Subscription was already gone",
                extra={"subscription_id": subscription_id},
            )
        except TwitchAPIException as e:
            raise TransportError(f"could not delete {subscription_id}: {e}") from e

    @staticmethod
    def _tear_down_socket(websocket) -> None:
        """Close one library socket without blocking this service's loop.

        `EventSubWebsocket.stop()` blocks on a future the socket's own loop has
        to complete, so calling it on a session that has already failed -- or
        never came up -- can hang the service. `_stop()` is the coroutine that
        actually closes the aiohttp session and the websocket, so it is
        scheduled on the socket's loop and not awaited.

        `_closing` is set in a `finally`, because it is what `_keep_loop_alive`
        spins on: a teardown that raised on the way would otherwise leave that
        thread looping at 10 Hz for the life of the process. `_stop()` itself
        raises when the connection is already None, which is exactly that case.
        """
        try:
            socket_loop = getattr(websocket, "_socket_loop", None)
            if socket_loop is not None and socket_loop.is_running():
                async def teardown():
                    try:
                        await websocket._stop()
                    except Exception:
                        pass
                    finally:
                        websocket._closing = True

                asyncio.run_coroutine_threadsafe(teardown(), socket_loop)
            else:
                websocket._closing = True
        except Exception as e:  # pragma: no cover -- a test double without them
            logger.debug("Could not tear down a socket", extra={"error": str(e)})

    def _slot_for_broadcaster(self, broadcaster_id) -> Optional[_Slot]:
        """The slot this pool holds for a channel, whatever id it recorded.

        Used when a revocation names an id the pool has never seen, which is
        what a reconnect leaves behind: it rotates every id on the socket while
        the pool keeps the ones it recorded at create time. The broadcaster
        comes from the revocation payload rather than any local registry --
        the library has already emptied those by the time it calls us.
        """
        if broadcaster_id is None:
            return None
        try:
            return self._slots.get(int(broadcaster_id))
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _session_id(connection: _Connection) -> Optional[str]:
        session = getattr(connection.websocket, "active_session", None)
        return getattr(session, "id", None) if session is not None else None

    def _slot_is_current(
        self, slot: _Slot, connection: _Connection, broadcaster_id: int
    ) -> bool:
        """Is this recorded subscription still real on this connection?

        Two checks, because the registry alone is not trustworthy. The
        library's `_resubscribe()` empties `_active_subscriptions`, re-creates
        everything, and on failure restores the OLD map wholesale -- but only
        `if not self._active_subscriptions`, so a failure on the FIRST
        re-subscribe puts every pre-reconnect id back while Twitch holds none
        of them on the new session. The registry then says yes for channels
        that do not exist, `create()` hands back the ghost id with no Twitch
        call, and the periodic re-adopt cannot repair it: `list()` yields
        nothing for them, they land in `to_create`, and `create()` short-
        circuits to the ghost again. Every FR-012 signal reads healthy while
        that socket's channels are dark.

        The session check catches it. A reconnect always means a new session,
        so a slot stamped with the old one is stale no matter what the map
        says -- and that holds for a partial failure too.
        """
        current_session = self._session_id(connection)
        if (
            slot.session_id is not None
            and current_session is not None
            and slot.session_id != current_session
        ):
            return False
        return self._connection_holds(connection, broadcaster_id)

    def _connection_holds(self, connection: _Connection, broadcaster_id: int) -> bool:
        """Does the library still have a subscription for this channel here?

        The registry is private, and after a reconnect it can lie (see
        `_slot_is_current`), so this is the second of two checks rather than
        the only one. A test double without one is taken at its word.
        """
        active = getattr(connection.websocket, "_active_subscriptions", None)
        if not isinstance(active, dict):
            return True
        wanted = str(broadcaster_id)
        return any(
            (subscription.get("condition") or {}).get("broadcaster_user_id") == wanted
            for subscription in active.values()
        )

    def _forget_slot(self, slot: _Slot):
        """Drop one slot from both indexes."""
        self._slots.pop(slot.broadcaster_id, None)
        self._by_subscription.pop(slot.subscription_id, None)
        connection = self._connection_by_id(slot.connection_id)
        if connection is not None:
            connection.subscription_ids.discard(slot.subscription_id)

    def _forget_unrecognised(self, subscription_id: str):
        """Clean up after deleting an id the pool had no slot for.

        Finds the channel behind the id from the library's own registry --
        that is the only place a rotated id appears -- and clears both the
        library's entry and whatever slot the pool still holds for that
        channel.
        """
        for connection in list(self._connections):
            active = getattr(connection.websocket, "_active_subscriptions", None)
            if not isinstance(active, dict) or subscription_id not in active:
                continue
            condition = (active[subscription_id].get("condition") or {})
            broadcaster = condition.get("broadcaster_user_id")
            self._forget_library_subscription(connection, subscription_id)
            connection.subscription_ids.discard(subscription_id)
            if broadcaster is None:
                return
            try:
                broadcaster_id = int(broadcaster)
            except (TypeError, ValueError):
                # The sibling helper already guards this. Raising here would be
                # worse than useless: the Twitch DELETE above has already
                # succeeded, so `_drop_one` would never pop `_actual` and the
                # reconciler would re-issue the same delete every pass for ever.
                return
            slot = self._slots.get(broadcaster_id)
            if slot is not None:
                self._forget_slot(slot)
                self._forget_library_subscription(connection, slot.subscription_id)
            return

    def _live_subscription_ids(
        self, connection: Optional[_Connection], slot: _Slot
    ) -> List[str]:
        """The ids Twitch currently holds for this channel on this connection.

        The library re-subscribes everything after a reconnect and gets fresh
        ids, so the id recorded at create time can be stale. Its own
        `_active_subscriptions` map is the only record of the current one; it
        is private, but the alternative is deleting an id Twitch has already
        collected and leaving the live subscription delivering into nothing.
        """
        if connection is None:
            return [slot.subscription_id]
        active = getattr(connection.websocket, "_active_subscriptions", None)
        if not isinstance(active, dict):
            return [slot.subscription_id]
        matches = [
            subscription_id
            for subscription_id, subscription in active.items()
            if (subscription.get("condition") or {}).get("broadcaster_user_id")
            == str(slot.broadcaster_id)
        ]
        if matches and slot.subscription_id not in matches:
            logger.debug(
                "Subscription id rotated by a reconnect, deleting the live one",
                extra={
                    "broadcaster_id": slot.broadcaster_id,
                    "recorded": slot.subscription_id,
                    "live": matches,
                },
            )
        return matches or [slot.subscription_id]

    @staticmethod
    def _forget_library_subscription(connection: _Connection, subscription_id: str):
        """Drop the subscription from the library's own bookkeeping.

        Without this the socket would re-create it on its next reconnect, and
        a channel the reconciler deliberately dropped would come back.
        """
        for attribute in ("_active_subscriptions", "_callbacks"):
            registry = getattr(connection.websocket, attribute, None)
            if isinstance(registry, dict):
                registry.pop(subscription_id, None)

    # -- socket death (T023) ----------------------------------------------

    async def _supervise(self):
        """Retire connections whose receive loop has stopped.

        The pool does not repair anything itself. It drops the dead
        connection and reports the loss; the reconciler then sees a smaller
        actual set, `eventsub_subscription_count` falls (which is the alert),
        and the next pass re-creates those channels on a surviving or new
        connection. Recovery stays in one place.
        """
        try:
            while True:
                await asyncio.sleep(self.supervise_interval_seconds)
                try:
                    self.reap_dead_connections()
                except Exception as e:
                    logger.error(
                        "Connection supervisor pass failed",
                        extra={"error": str(e), "error_type": type(e).__name__},
                    )
        except asyncio.CancelledError:
            raise

    def reap_dead_connections(self) -> int:
        """Drop every dead connection. Returns how many subscriptions were lost."""
        dead = [
            connection for connection in self._connections if self._is_dead(connection)
        ]
        if not dead:
            return 0

        # Computed once, before any retire: `_retire` removes from
        # `self._connections`, so evaluating this inside the loop counted the
        # survivors down by one on every iteration and printed a different
        # number on each line of the same event.
        remaining = len(self._connections) - len(dead)

        lost = 0
        for connection in dead:
            lost += connection.occupancy
            logger.error(
                "EventSub connection lost, its subscriptions are gone",
                extra={
                    "connection": connection.connection_id,
                    "subscriptions": connection.occupancy,
                    "remaining_connections": remaining,
                },
            )
            self._retire(connection)

        if self.on_subscriptions_lost is not None:
            self.on_subscriptions_lost(lost)
        return lost

    @staticmethod
    def _is_dead(connection: _Connection) -> bool:
        """True once this session can no longer deliver a message.

        The library keeps its thread alive after the receive loop breaks --
        `_keep_loop_alive` only watches a `_closing` flag that nothing sets on
        failure -- so thread liveness alone would never report a loss. The
        receive task finishing is the real signal: `_task_receive` breaks out
        when the connection is lost and cannot be re-established, and
        `_task_reconnect_handler` dies with the exception that ends it.
        """
        websocket = connection.websocket
        if not getattr(websocket, "_running", True):
            return True
        thread = getattr(websocket, "_socket_thread", None)
        if thread is not None and not thread.is_alive():
            return True
        tasks = getattr(websocket, "_tasks", None)
        if not tasks:
            # Still starting up. Not dead yet.
            return False
        return any(task.done() for task in tasks)

    def _retire(self, connection: _Connection):
        """Forget a connection and tear its socket down without blocking.

        `EventSubWebsocket.stop()` blocks on a future the socket's own loop
        has to complete, so calling it on a session that has already failed
        can hang the service. Its `_stop()` coroutine is what actually closes
        the aiohttp session and the websocket, so that is scheduled on the
        socket's loop and not awaited. Without it every socket death leaks a
        `ClientSession`, its file descriptors and a never-closed event loop.

        `_stop()` raises if the connection is already None, and it is the
        thing that sets `_closing`, so the flag is set in a wrapper's `finally`
        -- otherwise a failed teardown would leave `_keep_loop_alive` spinning
        forever on a thread that can no longer do anything.
        """
        websocket = connection.websocket
        websocket._running = False
        self._tear_down_socket(websocket)

        self._connections = [
            live for live in self._connections if live.connection_id != connection.connection_id
        ]
        for subscription_id in list(connection.subscription_ids):
            slot = self._by_subscription.pop(subscription_id, None)
            if slot is not None:
                self._slots.pop(slot.broadcaster_id, None)
        # A slot can outlive its id if a reconnect rotated it; clear anything
        # still pointing at this connection.
        for broadcaster_id, slot in list(self._slots.items()):
            if slot.connection_id == connection.connection_id:
                self._slots.pop(broadcaster_id, None)
                self._by_subscription.pop(slot.subscription_id, None)
        connection.subscription_ids.clear()

    # -- small helpers ----------------------------------------------------

    def _live_session_ids(self) -> Set[str]:
        sessions = set()
        for connection in self._connections:
            session = getattr(connection.websocket, "active_session", None)
            if session is not None and getattr(session, "id", None):
                sessions.add(session.id)
        return sessions

    def _connection_by_session(self, session_id: str) -> Optional[_Connection]:
        for connection in self._connections:
            session = getattr(connection.websocket, "active_session", None)
            if session is not None and getattr(session, "id", None) == session_id:
                return connection
        return None

    @staticmethod
    def _cursor_of(result) -> Optional[str]:
        try:
            return result.current_cursor()
        except Exception:
            return None
