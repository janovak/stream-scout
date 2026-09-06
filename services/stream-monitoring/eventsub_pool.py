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
event, and a `notification_handler` for the auxiliary type.
`stream_monitoring_service.py` passes a handler that runs `map_chat_message`
and hands the result to the existing Kafka producer path. The mapping is a
module-level pure function so it can be tested without a socket, a Twitch
client or a producer (T020, T021).

Two subscriptions per channel (Feature 007)
-------------------------------------------
A monitored channel needs `channel.chat.message` for the chat itself and
`channel.chat.notification` for the gift and raid notices that cause the
bursts the detector has to ignore. They are two independent Twitch
subscriptions with their own ids, their own failures and their own
revocations, so the pool keeps one `_Slot` per (channel, `CoverageType`) and
never infers one from the other.

The unit that follows from that is the one thing to keep straight: a
CONNECTION holds at most 300 SUBSCRIPTIONS, so at most 150 fully covered
CHANNELS; the pool holds at most 450 channels, which is exactly 900
subscriptions across three sessions. `occupancy()` answers in
subscriptions and `coverage_counts()` in channels, and mixing them is how a
session silently goes to 301.

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
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import (
    Any,
    AsyncIterator,
    Awaitable,
    Callable,
    Dict,
    List,
    Optional,
    Set,
    Tuple,
)

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
    ADOPTABLE_STATUSES,
    CHANNEL_COVERAGE_STATES,
    ExistingSubscription,
    PoolCapacityError,
    RateLimitedError,
    SubscriptionRefusedError,
    SubscriptionTransport,
    TransientSessionError,
    TransportError,
)

# `PoolCapacityError` is imported rather than defined here on purpose. It is a
# fact about THIS transport's slots, but the taxonomy the reconciler classifies
# lives in `reconciler.py`, so the class is defined once and re-exported
# through this module: `eventsub_pool.PoolCapacityError` and
# `reconciler.PoolCapacityError` are the SAME object. Two classes sharing one
# name would mean a capacity error raised here is caught by nothing there
# (decision 28, data-model I26).

logger = logging.getLogger("stream_monitoring")

# Measured cap for one websocket session. Twitch documents 300 for a websocket
# transport and the spike confirmed it.
SUBSCRIPTIONS_PER_CONNECTION = 300
# Twitch's other websocket limit, and the one nothing here used to know about:
# "You can create a maximum of 3 WebSockets connections with enabled
# subscriptions", per client-id/user-id pair
# (dev.twitch.tv/docs/eventsub/handling-websocket-events, checked 2026-08-29).
# So the real ceiling for this transport is 3 x 300 = 900 subscriptions, not the
# arbitrary number the growth rule implied. Past it Twitch refuses the
# subscriptions on the fourth socket with wording that matches none of the
# markers below, so the channels routed there would be retried for ever on a
# socket that can never take them, with only a WARNING to show for it. Fail
# loudly at the boundary instead.
MAX_CONNECTIONS = 3
MAX_SUBSCRIPTIONS = SUBSCRIPTIONS_PER_CONNECTION * MAX_CONNECTIONS

# The two subscription types a monitored channel needs, and the filters for
# `list()`, so a subscription made by something else never enters the actual
# set. `channel.chat.message` carries the chat this service was built for;
# `channel.chat.notification` carries the gift/raid notices Feature 007 uses to
# suppress the bursts they cause. They are separate Twitch subscriptions with
# separate ids, separate quotas and separate failure modes.
CHAT_MESSAGE_SUBSCRIPTION_TYPE = "channel.chat.message"
CHAT_NOTIFICATION_SUBSCRIPTION_TYPE = "channel.chat.notification"

# Version of the `suppression-events` payload, carried on every record
# (contracts/suppression-events.schema.md §2). A consumer that does not
# recognise the value ignores the record, which is what makes adding an
# optional field safe against a live topic.
SUPPRESSION_SCHEMA_VERSION = 1

# The only `notice_type` values that ever produce a record (FR-005). Everything
# else -- including the documented categories below, an absent attribute and
# any value Twitch adds later -- produces nothing. This tuple is the topic's
# invariant 3: the consumer's allow-list is defence in depth, not the filter.
SUPPRESSION_TRIGGER_NOTICE_TYPES = ("community_sub_gift", "sub_gift", "raid")

# The documented 4.5.0 categories that are NOT triggers (research §2.2). They
# are enumerated so an ignored notice can be counted under its own name while
# the label set stays bounded: a metric label taken straight from Twitch would
# grow without limit the day a new category ships.
KNOWN_IGNORED_NOTICE_TYPES = frozenset(
    {
        "sub",
        "resub",
        "gift_paid_upgrade",
        "prime_paid_upgrade",
        "unraid",
        "pay_it_forward",
        "announcement",
        "bits_badge_tier",
        "charity_donation",
    }
)

# Where every unrecognised or absent category is counted instead (research R7).
UNKNOWN_NOTICE_TYPE_LABEL = "other"

# How long a channel stays in `degraded_chat_only` after Twitch refuses its
# notification subscription while chat is live (research D2, data-model I17).
#
# The hold-off exists because `streamers.eventsub_refused_at` is per CHANNEL
# and lasts seven days: letting an auxiliary 403 reach the reconciler as a
# refusal would kill that channel's CHAT for a week to protect a suppression
# signal. It is bounded rather than permanent because a permanent pool-local
# refusal trades one coverage hole for another and quietly contradicts FR-001.
AUXILIARY_REFUSAL_RETRY_SECONDS = 3600


def _positive_int_from_env(name: str, default: int) -> int:
    """An operator override, or the checked-in default. Loud on nonsense."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError as e:
        raise ValueError(f"{name} must be a whole number of seconds, got {raw!r}") from e
    if value <= 0:
        raise ValueError(f"{name} must be positive, got {value}")
    return value


class CoverageType(Enum):
    """The closed set of subscription types one monitored channel needs.

    Closed on purpose: every capacity number in `data-model.md` §4 is derived
    from there being exactly two of these (450 channels x 2 = 900
    subscriptions, the full allowance for this token). A third member would change all
    of them, so adding one has to be a deliberate edit here.
    """

    CHAT = "chat"
    NOTIFICATION = "notification"

    @property
    def subscription_type(self) -> str:
        """The Twitch `type` this coverage is made of."""
        return _SUBSCRIPTION_TYPE_BY_COVERAGE[self]

    @classmethod
    def for_subscription_type(cls, subscription_type) -> Optional["CoverageType"]:
        """The coverage a Twitch `type` belongs to, or None if it is not ours.

        None rather than a guess: a subscription type this pool does not create
        must never be resolved to one of ours, or a revocation would forget the
        wrong half of a channel's pair.
        """
        return _COVERAGE_BY_SUBSCRIPTION_TYPE.get(subscription_type)


_SUBSCRIPTION_TYPE_BY_COVERAGE = {
    CoverageType.CHAT: CHAT_MESSAGE_SUBSCRIPTION_TYPE,
    CoverageType.NOTIFICATION: CHAT_NOTIFICATION_SUBSCRIPTION_TYPE,
}
_COVERAGE_BY_SUBSCRIPTION_TYPE = {
    subscription_type: coverage_type
    for coverage_type, subscription_type in _SUBSCRIPTION_TYPE_BY_COVERAGE.items()
}

# The channel-level states derived from the pair. `absent` is deliberately not
# counted: a channel with no slot at all is not in any index, so counting it
# would mean counting every channel that has ever existed. The tuple is the
# reconciler's, imported rather than repeated, because it is also the complete
# label set of `eventsub_channel_coverage` and the two must not drift apart.
COVERAGE_STATES = CHANNEL_COVERAGE_STATES

# The two ORDINARY partial states: one coverage type live, the other missing,
# and no hold-off standing in for it. They are the only states `list()` keeps
# out of the actual set on purpose, which is what makes them repairable -- and
# therefore the only ones the reconciler has to be handed a drop handle for
# once the channel stops being wanted. `degraded_chat_only` is deliberately
# NOT here: it is actual for the length of its hold-off, so the ordinary diff
# already drops it, and reporting it as droppable would evict live chat (I1).
PARTIAL_COVERAGE_STATES = ("chat_only", "notification_only")

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

# How long one capacity-preflight enumeration stays usable, in MILLISECONDS on
# the injected monotonic clock. At exact capacity every blocked channel in a
# batch asks the same question -- "does this already exist?" -- and each answer
# used to cost two type-filtered Helix walks. One bounded snapshot answers the
# whole batch in two walks, and a window this short cannot outlive the pass
# that took it. It is read through `self._monotonic_ms`, so a test moves it
# without sleeping.
CAPACITY_ADOPTION_SNAPSHOT_MS = 1000

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

# A create that raced a reconnect: the session id it carried is already gone.
# The 2026-08-30 ramp saw bursts of these on every cold start (up to ~70 in one
# pass), all recovered on the next pass. Classified so the reconciler counts
# them apart from real failures and logs them at WARNING, not ERROR.
_TRANSIENT_SESSION_MARKERS = (
    "session does not exist",
    "has already disconnected",
    "session has already disconnected",
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
    """Convert the envelope timestamp to epoch milliseconds, or `None`.

    `sent_at` drives Flink's event time through `SentAtTimestampAssigner`, and
    the contract says it is an int or `null` -- never a string, or the
    assigner silently falls back to record time (contract invariant 2).

    pyTwitchAPI has already parsed `metadata.message_timestamp` into a
    tz-aware `datetime` by the time an event reaches us. The string branch is
    for tests and for any caller holding the raw envelope.

    A value this cannot read returns `None` rather than raising. `spec.md`
    Edge Cases require an envelope with a missing or unparseable
    `message_timestamp` to STILL publish, with `sent_at` null so the assigner
    falls back to record time. Raising sent it to `_on_eventsub_message`'s
    handler instead, which logs and returns -- so the whole chat message was
    dropped, against the constitution's no-data-loss rule, to save a field the
    contract already allows to be null.
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
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            logger.warning(
                "Unparseable message_timestamp, publishing with sent_at null",
                extra={"message_timestamp": value},
            )
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp() * 1000)
    logger.warning(
        "Unexpected message_timestamp type, publishing with sent_at null",
        extra={"type": type(value).__name__},
    )
    return None


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
        #
        # `getattr`, because `TwitchObject.__init__` skips any field the
        # payload omits, so an envelope without `message_timestamp` has no such
        # attribute at all rather than a None one. The spec's edge case wants
        # that message published with `sent_at` null, not dropped.
        "sent_at": to_epoch_ms(getattr(event.metadata, "message_timestamp", None)),
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


@dataclass(frozen=True)
class SuppressionMapResult:
    """What one `channel.chat.notification` event turned into.

    Typed rather than an `Optional[dict]` so the three outcomes the producer
    contract distinguishes cannot be confused at the call site: a notice that
    is not a trigger is ORDINARY (`ignored`), while a trigger this cannot be
    made trustworthy is a FAULT (`malformed`). Both produce no record, and
    counting them on the same metric would hide the second behind the first.

    `notice_type` is always a bounded metric label: the real category for a
    trigger or a documented non-trigger, and `other` for anything else.
    `reason` is set only for `malformed`, and is one of `identity` or
    `occurred_at`. `notice_id` is carried so the failure log can name the
    notice without the caller re-reading the raw event -- which is where the
    free user text lives.
    """

    kind: str
    payload: Optional[dict]
    notice_type: str
    reason: Optional[str] = None
    notice_id: Optional[str] = None


def _suppression_notice_id(metadata) -> Optional[str]:
    """Twitch's own `message_id` for this notice, or `None`.

    `getattr` for the same reason `map_chat_message` uses it: pyTwitchAPI
    omits an absent field entirely rather than setting it to `None`. Anything
    that is not a string is dropped, because the contract types the field
    `str | null` and a diagnostic is not worth a type violation.
    """
    notice_id = getattr(metadata, "message_id", None)
    return notice_id if isinstance(notice_id, str) else None


def _suppression_broadcaster_id(value) -> Optional[int]:
    """The channel identity, or `None` when it cannot be trusted.

    Never guessed and never defaulted (contract §2.1): a suppression record
    for the wrong channel would silence clipping on a channel that had no
    burst.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _raid_viewer_count(data) -> Optional[int]:
    """The raid's audience size, when Twitch sent a trustworthy one.

    Diagnostic only (contract §2.2): the raid window is a constant, so this
    number may never reach any consumer logic. It is carried so the tuning
    question "should raid windows scale with audience?" can be revisited from
    the topic instead of from a producer change.
    """
    raid = getattr(data, "raid", None)
    viewer_count = getattr(raid, "viewer_count", None)
    if isinstance(viewer_count, bool) or not isinstance(viewer_count, int):
        return None
    return viewer_count


def map_suppression_event(event, *, received_at_ms: Optional[int] = None) -> SuppressionMapResult:
    """Map a `ChannelChatNotificationEvent` onto the `suppression-events` schema.

    The schema is `contracts/suppression-events.schema.md`, and this function
    is its whole enforcement point: it is the only place a record can be
    created. Pure, so the contract is testable without a socket, a Twitch
    client or a producer (T004/T005).

    What it carries is a NOTICE, never a decision. No window duration and no
    computed deadline appear here -- baking policy into the topic would make
    every retained record wrong the moment a window is retuned (research D7).

    Order matters. The category is classified FIRST, so a notice this feature
    does not care about is ignored without ever being judged on identity or
    time it was never going to need. Counting a `sub` as malformed because
    Twitch omitted a field this feature does not read would turn ordinary
    traffic into a fault signal.

    The one deliberate divergence from the chat path: a trigger whose
    `message_timestamp` cannot be read is DROPPED rather than published with a
    null time. `map_chat_message` publishes `sent_at: null` because the
    assigner may fall back to record time; here the timestamp becomes a
    suppression deadline, and substituting the ingestion clock would fabricate
    one -- which FR-017 forbids (research D9).
    """
    data = event.event
    metadata = getattr(event, "metadata", None)
    notice_id = _suppression_notice_id(metadata)

    notice_type = getattr(data, "notice_type", None)
    if notice_type not in SUPPRESSION_TRIGGER_NOTICE_TYPES:
        return SuppressionMapResult(
            kind="ignored",
            payload=None,
            notice_type=(
                notice_type
                if isinstance(notice_type, str)
                and notice_type in KNOWN_IGNORED_NOTICE_TYPES
                else UNKNOWN_NOTICE_TYPE_LABEL
            ),
            notice_id=notice_id,
        )

    broadcaster_id = _suppression_broadcaster_id(
        getattr(data, "broadcaster_user_id", None)
    )
    if broadcaster_id is None:
        return SuppressionMapResult(
            kind="malformed",
            payload=None,
            notice_type=notice_type,
            reason="identity",
            notice_id=notice_id,
        )

    # The same converter and therefore the same clock as `chat-messages.sent_at`,
    # which is what makes comparing a peak second with a deadline meaningful.
    occurred_at_ms = to_epoch_ms(getattr(metadata, "message_timestamp", None))
    if occurred_at_ms is None:
        return SuppressionMapResult(
            kind="malformed",
            payload=None,
            notice_type=notice_type,
            reason="occurred_at",
            notice_id=notice_id,
        )

    if received_at_ms is None:
        # Read once. The ingestion clock is diagnostic only -- it never stands
        # in for `occurred_at_ms` above.
        received_at_ms = int(time.time() * 1000)

    return SuppressionMapResult(
        kind="mapped",
        payload={
            "schema_version": SUPPRESSION_SCHEMA_VERSION,
            "broadcaster_id": broadcaster_id,
            "notice_type": notice_type,
            "occurred_at_ms": occurred_at_ms,
            "notice_id": notice_id,
            "received_at_ms": int(received_at_ms),
            "viewer_count": (
                _raid_viewer_count(data) if notice_type == "raid" else None
            ),
        },
        notice_type=notice_type,
        notice_id=notice_id,
    )


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
    #
    # It is also not remembered for ever across a SESSION change. `full_at`
    # records what one session refused at, and a reconnect replaces that
    # session with one that has refused nothing; keeping the number would
    # strand slots on a session that never earned it, and with no free
    # reserve those are the very slots the last channel needs (decision 28,
    # data-model I27). `full_at_session` is the session the refusal was
    # observed on, and `_reevaluate_full_at` clears the pair when it moves.
    full_at: Optional[int] = None
    full_at_session: Optional[str] = None

    @property
    def occupancy(self) -> int:
        return len(self.subscription_ids)

    @property
    def load(self) -> int:
        return len(self.subscription_ids) + self.reserved


def _monotonic_ms() -> int:
    """The default clock for the auxiliary hold-off, in milliseconds.

    Monotonic, not wall clock: the hold-off is a duration, and a clock step
    must not shorten or extend it. Injectable, because an hour cannot be
    waited out in a test and sleeping through it would not be a test.
    """
    return int(time.monotonic() * 1000)


async def _discard_notification(event) -> None:
    """The default sink for `channel.chat.notification`.

    Dual coverage is not conditional on anything having somewhere to put the
    notices (FR-001): every monitored channel gets both subscriptions, so the
    pool can be constructed without a notification handler and still create
    the subscription. The service wires `_on_eventsub_notification` in its
    place, which maps and publishes through `map_suppression_event`.
    """
    logger.debug("Chat notification received with no handler wired, discarding")


@dataclass(frozen=True)
class _Slot:
    """Where one broadcaster's subscription of ONE coverage type lives.

    Two slots for the same channel are independent records. Either may exist
    without the other, and neither may be inferred from the other's presence
    or from the other's id (data-model I3).
    """

    broadcaster_id: int
    coverage_type: CoverageType
    connection_id: int
    subscription_id: str
    # The websocket session this subscription was made on. A reconnect gives
    # the connection a NEW session, and everything Twitch held on the old one
    # is gone -- so a slot whose session no longer matches its connection's is
    # stale whatever any local registry says. This is the only check that does
    # not depend on the library telling the truth about what it holds. It is
    # read before each listen call, so the two halves of a pair created either
    # side of a reconnect carry different, honest stamps.
    session_id: Optional[str] = None


@dataclass(frozen=True)
class _AdoptableRow:
    """One Helix row the capacity preflight is allowed to adopt.

    Recorded exactly as it was SEEN: the subscription's own id, the session it
    was on, and the connection that held that session at snapshot time. It is
    never adopted straight from here -- the session is revalidated against the
    connections the pool holds now, because a snapshot is a reading of the
    past and a reconnect makes every row on the old session worthless.
    """

    subscription_id: str
    session_id: str
    connection_id: int


@dataclass(frozen=True)
class ChannelCoverage:
    """The derived, per-channel view over one channel's pair of slots.

    This is the only thing that answers "is this channel covered". The
    reconciler is channel-keyed, so it needs one answer per channel and not
    one per subscription.
    """

    broadcaster_id: int
    chat_slot: Optional[_Slot]
    notification_slot: Optional[_Slot]
    auxiliary_refused_until_ms: Optional[int]
    state: str

    @property
    def is_actual(self) -> bool:
        """May the reconciler count this channel as covered?

        `degraded_chat_only` is the one state that says yes without both
        halves, and only for the length of a bounded hold-off: without it the
        reconciler would drop the channel from `_actual` every pass and
        re-create a subscription Twitch has just refused.
        """
        return self.state in ("complete", "degraded_chat_only")


class EventSubPoolTransport(SubscriptionTransport):
    """A growing pool of `EventSubWebsocket` sessions behind one transport.

    `message_handler` is an async callable invoked once per chat event, with
    the raw `ChannelChatMessageEvent`. `notification_handler` is the same for
    `ChannelChatNotificationEvent`. Both run on the receiving socket's own
    event loop, so neither may assume the service's loop and neither may
    block.

    Every monitored channel holds one subscription of EACH `CoverageType`, so
    a channel costs two of the 300 subscriptions a session can hold. Occupancy
    is therefore counted in subscriptions and coverage in channels, and the
    two units are never mixed (data-model I4).
    """

    def __init__(
        self,
        twitch,
        message_handler: Callable[[Any], Awaitable[None]],
        *,
        notification_handler: Optional[Callable[[Any], Awaitable[None]]] = None,
        user_id: Optional[str] = None,
        cap: int = SUBSCRIPTIONS_PER_CONNECTION,
        connection_factory: Optional[Callable[[], Any]] = None,
        on_subscriptions_lost: Optional[Callable[[int], None]] = None,
        supervise_interval_seconds: float = DEFAULT_SUPERVISE_INTERVAL_SECONDS,
        connect_timeout_seconds: float = DEFAULT_CONNECT_TIMEOUT_SECONDS,
        max_connections: int = MAX_CONNECTIONS,
        monotonic_ms: Optional[Callable[[], int]] = None,
        auxiliary_refusal_retry_seconds: Optional[int] = None,
    ):
        self.twitch = twitch
        self.message_handler = message_handler
        self.notification_handler = notification_handler or _discard_notification
        self.user_id = user_id
        self.cap = cap
        self._connection_factory = connection_factory or self._default_connection_factory
        self.on_subscriptions_lost = on_subscriptions_lost
        self.supervise_interval_seconds = supervise_interval_seconds
        self.connect_timeout_seconds = connect_timeout_seconds
        self.max_connections = max_connections
        self._monotonic_ms = monotonic_ms or _monotonic_ms
        self.auxiliary_refusal_retry_seconds = (
            auxiliary_refusal_retry_seconds
            if auxiliary_refusal_retry_seconds is not None
            else _positive_int_from_env(
                "AUXILIARY_REFUSAL_RETRY_SECONDS", AUXILIARY_REFUSAL_RETRY_SECONDS
            )
        )
        # Monotonic deadline after a failed `_grow`, so the rest of a batch
        # fails fast instead of each channel waiting out its own connect.
        self._growth_blocked_until = 0.0

        self._connections: List[_Connection] = []
        self._next_connection_id = 0
        # One entry per (channel, coverage type), never per channel: the two
        # halves are created, adopted, deleted, revoked and lost separately.
        self._slots: Dict[Tuple[int, CoverageType], _Slot] = {}
        # Each subscription id resolves to its OWN slot, so no id can be
        # mistaken for its sibling's.
        self._by_subscription: Dict[str, _Slot] = {}
        # broadcaster -> monotonic ms at which a refused notification
        # subscription becomes worth trying again (research D2).
        self._auxiliary_refused_until: Dict[int, int] = {}
        # Serialises routing, reservations and growth. Held only around
        # bookkeeping and the one blocking `start()`, never around a create.
        self._lock = asyncio.Lock()
        # The capacity preflight's own lock, deliberately NOT `_lock`: it is
        # held across a Helix walk, and holding the routing lock across a
        # network round trip would stall every other worker's placement. Only
        # one caller populates the snapshot; the rest wait here and read it.
        self._adoption_lock = asyncio.Lock()
        # (broadcaster, coverage type) -> `_AdoptableRow`, the shared bounded
        # preflight snapshot. `None` means "nothing cached"; the types walked
        # so far are tracked separately so a caller missing only one half does
        # not force a second walk of the other.
        self._adoption_rows: Optional[Dict[Tuple[int, CoverageType], _AdoptableRow]] = None
        self._adoption_types: Set[CoverageType] = set()
        self._adoption_expires_ms = 0
        # The connections and sessions the snapshot was taken against. A grow,
        # a retirement or a reconnect changes it, and each of those makes the
        # snapshot a reading of a pool that no longer exists.
        self._adoption_fingerprint: Tuple[Tuple[int, Optional[str]], ...] = ()
        # Bumped by every invalidation, so a walk that was overtaken by a
        # delete is discarded instead of cached.
        self._adoption_generation = 0
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
        self._auxiliary_refused_until = {}
        self._invalidate_adoption_snapshot("transport closed")
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
        """Bring one channel up to full coverage, creating only what is missing.

        A channel needs one `channel.chat.message` subscription and one
        `channel.chat.notification` subscription (FR-001). Both are created
        when the channel is absent; a channel that already holds one of them
        gets only the other, so a repair never duplicates a working
        subscription (FR-002).

        The return value is the CHAT subscription id. The reconciler keys
        `_actual` by channel and hands this id back to `delete()`, so the chat
        half is the channel's handle -- the auxiliary half is always resolved
        from the channel, never from an id.
        """
        self._refresh_slots(broadcaster_id)
        to_create = self._coverage_to_create(broadcaster_id)
        if not to_create:
            return self._handle_for(broadcaster_id)

        try:
            placement = await self._reserve_placement(broadcaster_id, to_create)
        except PoolCapacityError as error:
            # The pool has no slot. Before reporting that, check whether these
            # subscriptions already EXIST -- at exact capacity that is not a
            # remote possibility, it is the ordinary shape of a stale index
            # against a full account. Adoption creates nothing and consumes no
            # slot (decision 27), so it is the one thing that can still
            # succeed here.
            return await self._adopt_at_capacity(broadcaster_id, to_create, error)

        # Each half is placed on its OWN connection -- the same one twice when
        # the pair is co-located, two different ones when it is split -- and
        # `_create_one` consumes exactly one reserved subscription on every
        # path it can take. What is left here is only the types it never got
        # to, each released on the connection its slot was actually held on.
        unattempted = dict(placement)
        try:
            for coverage_type in to_create:
                connection = unattempted.pop(coverage_type)
                try:
                    await self._create_one(broadcaster_id, coverage_type, connection)
                except SubscriptionRefusedError as error:
                    if coverage_type is not CoverageType.NOTIFICATION or not self._holds(
                        broadcaster_id, CoverageType.CHAT
                    ):
                        raise
                    # D2. Refusal is per CHANNEL in Postgres and stands for
                    # seven days, so letting an auxiliary 403 out of here as a
                    # `SubscriptionRefusedError` would evict this channel's
                    # chat for a week to protect a suppression signal. Record a
                    # bounded, pool-local hold-off instead and keep the chat.
                    self._record_auxiliary_refusal(broadcaster_id, error)
        finally:
            # A failure after reservation KEEPS the successful half: a
            # chat-only channel is a convergent state, and at 900 of 900 the
            # slot it would cost to recreate may not be there on the next pass
            # (decision 28). Only the reservations nothing was attempted on
            # are given back, and each on its own connection.
            for connection in unattempted.values():
                await self._release(connection, slots=1)

        return self._handle_for(broadcaster_id)

    async def _reserve_placement(
        self, broadcaster_id: int, to_create: List[CoverageType]
    ) -> Dict[CoverageType, _Connection]:
        """Hold a slot for each type this channel is missing.

        Two missing types are a PAIR and are reserved as one atomic
        all-or-nothing placement; one missing type is a repair and takes the
        ordinary single-slot reservation, which is unchanged.
        """
        if len(to_create) == 2:
            return await self._reserve_pair(broadcaster_id)
        coverage_type = to_create[0]
        return {coverage_type: await self._reserve(broadcaster_id, slots=1)}

    async def _adopt_at_capacity(
        self,
        broadcaster_id: int,
        to_create: List[CoverageType],
        error: PoolCapacityError,
    ) -> str:
        """Last resort when there is no slot: is it already subscribed?

        A bounded preflight adoption, and ONLY on the capacity path -- never
        on an ordinary create, which reaches `_adopt_conflict` through Twitch's
        own 409 after the listen call. Here there is no listen call to make and
        no slot to reserve: a subscription that already exists is already
        counted against the 900, so recording it changes occupancy by nothing.
        That is exactly why decision 27 could release the free reserve.

        The enumeration is SHARED. A full pool blocks every channel in the
        batch, and each blocked channel asking Twitch on its own cost two
        type-filtered Helix walks -- so one saturated account turned a single
        pass into hundreds of walks and spent the rate-limit budget on
        questions with one answer. `_adoption_snapshot` answers them all from
        one bounded reading: at most one walk per `CoverageType` per snapshot
        window, whether the callers arrive together or one after another.

        The rules are unchanged and still applied per row: `enabled`, exact
        type match, and on a session this pool holds RIGHT NOW rather than
        when the snapshot was taken. Nothing foreign is claimed either -- only
        the broadcaster and the types this channel is actually missing are
        ever looked up, so another channel's row is never recorded under this
        one. An enumeration that finds nothing is not an error in its own
        right -- the capacity condition is the answer -- so it is logged and
        the original error is raised.
        """
        try:
            rows = await self._adoption_snapshot(to_create)
        except Exception as enumeration_error:
            # A preflight that cannot enumerate has not found anything, which
            # is the same outcome as an enumeration that found nothing.
            logger.debug(
                "Capacity preflight could not enumerate, reporting capacity",
                extra={
                    "broadcaster_id": broadcaster_id,
                    "error": str(enumeration_error),
                },
            )
            rows = {}

        for coverage_type in to_create:
            if self._holds(broadcaster_id, coverage_type):
                # Already adopted -- by a concurrent caller, or by the direct
                # `_slots` answer the ordinary path gives. Nothing to do, and
                # nothing to enumerate for.
                continue
            row = rows.get((broadcaster_id, coverage_type))
            if row is not None and self._adopt_snapshot_row(
                broadcaster_id, coverage_type, row
            ):
                continue
            logger.debug(
                "Nothing adoptable for a channel the pool has no slot for",
                extra={
                    "broadcaster_id": broadcaster_id,
                    "coverage_type": coverage_type.value,
                    "in_snapshot": row is not None,
                },
            )

        remaining = self._coverage_to_create(broadcaster_id)
        if not remaining:
            # Everything this channel was missing already existed on a session
            # this pool receives from. Nothing was created and the occupancy is
            # exactly what it was.
            return self._handle_for(broadcaster_id)

        logger.warning(
            "No free subscription slot for this channel, and nothing to adopt",
            extra={
                "broadcaster_id": broadcaster_id,
                "missing": [coverage_type.value for coverage_type in remaining],
                "subscriptions": sum(
                    connection.occupancy for connection in self._connections
                ),
                "connections": len(self._connections),
                "free_slots": self._usable_free_slots(),
            },
        )
        raise error

    async def _adoption_snapshot(
        self, coverage_types: List[CoverageType]
    ) -> Dict[Tuple[int, CoverageType], _AdoptableRow]:
        """The shared preflight reading, walking each type at most once.

        One snapshot serves the callers that are blocked together AND the ones
        that arrive immediately afterwards, because both are asking about the
        same full account. `_adoption_lock` is what makes that true: the first
        caller through walks, everyone behind it reads what that walk found.

        The window is short and monotonic (`CAPACITY_ADOPTION_SNAPSHOT_MS`),
        and three things end it early, because each makes the reading a
        description of a pool that no longer exists:

        * a GROW or a RETIREMENT, and a SESSION transition on any connection --
          all three show up in `_session_fingerprint()`, which is compared here
          rather than hooked into every site that could cause one;
        * a DELETE or a revocation, which turns a row into a stale POSITIVE:
          the id is still in the snapshot while Twitch no longer holds it, and
          adopting it would record a slot nothing delivers for. Those bump
          `_adoption_generation`.

        A caller ADOPTING from the snapshot does none of that on purpose. The
        row it took was true, the ones its neighbours need are still true, and
        invalidating there would put the walk back on every blocked channel --
        which is the cost this snapshot exists to remove.

        A create is deliberately not an invalidation either: it can only make
        the snapshot MISS a row (a stale negative), and a miss reports the
        capacity condition that was already true.
        """
        async with self._adoption_lock:
            generation = self._adoption_generation
            now = self._monotonic_ms()
            fingerprint = self._session_fingerprint()
            reusable = (
                self._adoption_rows is not None
                and now < self._adoption_expires_ms
                and fingerprint == self._adoption_fingerprint
            )
            walked = set(self._adoption_types) if reusable else set()
            missing = [
                coverage_type
                for coverage_type in coverage_types
                if coverage_type not in walked
            ]
            if reusable and not missing:
                # Every type this caller needs was walked inside this window,
                # so there is nothing to ask Twitch. That IS the point.
                return self._adoption_rows
            rows = dict(self._adoption_rows) if reusable else {}
            if missing:
                live_sessions = self._live_session_ids()
                for coverage_type in missing:
                    try:
                        await self._collect_adoptable(
                            coverage_type, live_sessions, rows
                        )
                    except Exception as e:
                        # The type is still marked as walked. A Helix that is
                        # refusing must not be asked again by every remaining
                        # blocked channel in the batch -- the window expires on
                        # its own, and until then the answer is "nothing
                        # adoptable", which is the safe one.
                        logger.warning(
                            "Capacity preflight walk failed, treating this type "
                            "as unadoptable for the rest of the window",
                            extra={
                                "coverage_type": coverage_type.value,
                                "error": str(e),
                            },
                        )
                    walked.add(coverage_type)

            if self._adoption_generation != generation:
                # A delete or a revocation landed while this walk was in
                # flight, so some row here may name a subscription Twitch has
                # already collected. Cache nothing and adopt nothing: the
                # caller reports the capacity condition it already had, and
                # the next pass re-enumerates against a settled pool.
                self._adoption_rows = None
                self._adoption_types = set()
                return {}

            self._adoption_rows = rows
            self._adoption_types = walked
            # A fresh snapshot gets its full reuse window AFTER the Helix
            # walks complete. Starting the clock before ten paginated requests
            # made a slow-but-successful snapshot expire before it was stored,
            # putting every blocked channel back on its own pair of walks.
            self._adoption_expires_ms = (
                self._adoption_expires_ms
                if reusable
                else self._monotonic_ms() + CAPACITY_ADOPTION_SNAPSHOT_MS
            )
            self._adoption_fingerprint = fingerprint
            return rows

    async def _collect_adoptable(
        self,
        coverage_type: CoverageType,
        live_sessions: Set[str],
        rows: Dict[Tuple[int, CoverageType], _AdoptableRow],
    ) -> None:
        """One type-filtered Helix walk, keeping only what may be adopted.

        The same filter `_adopt_conflict` applies, row by row: Twitch's own
        `sub_type` filter is not trusted on its own, `enabled` is required, and
        the transport's session has to be one this pool holds. Anything else --
        revoked, disconnected, or living on a session that belongs to somebody
        else's process -- never enters the snapshot at all, so it cannot be
        adopted later by a lookup that has forgotten why it was there.
        """
        result = await self.twitch.get_eventsub_subscriptions(
            sub_type=coverage_type.subscription_type, target_token=AuthType.USER
        )
        async for subscription in result:
            row_type = getattr(subscription, "type", None)
            if row_type is not None and row_type != coverage_type.subscription_type:
                continue
            if getattr(subscription, "status", None) not in ADOPTABLE_STATUSES:
                continue
            transport = getattr(subscription, "transport", None) or {}
            session_id = transport.get("session_id")
            if session_id not in live_sessions:
                continue
            connection = self._connection_by_session(session_id)
            if connection is None:
                continue
            broadcaster_id = (getattr(subscription, "condition", None) or {}).get(
                "broadcaster_user_id"
            )
            if broadcaster_id is None:
                continue
            try:
                key = (int(broadcaster_id), coverage_type)
            except (TypeError, ValueError):
                continue
            # First row wins, so one channel cannot be recorded twice under
            # one key and the snapshot stays the size of the account.
            rows.setdefault(
                key,
                _AdoptableRow(
                    subscription_id=subscription.id,
                    session_id=session_id,
                    connection_id=connection.connection_id,
                ),
            )

    def _adopt_snapshot_row(
        self, broadcaster_id: int, coverage_type: CoverageType, row: _AdoptableRow
    ) -> bool:
        """Record one snapshot row, but only if its session is still current.

        The revalidation is the whole safety of reading a snapshot at all:
        `_connection_by_session` answers from the sessions the pool holds NOW,
        so a row whose session has been retired or replaced by a reconnect
        resolves to nothing and is refused. Recording it instead would count a
        subscription Twitch dropped with the old session, and the channel
        would read as covered while nothing delivered for it.
        """
        connection = self._connection_by_session(row.session_id)
        if connection is None:
            return False
        slot = _Slot(
            broadcaster_id=broadcaster_id,
            coverage_type=coverage_type,
            connection_id=connection.connection_id,
            subscription_id=row.subscription_id,
            session_id=row.session_id,
        )
        connection.subscription_ids.add(row.subscription_id)
        self._record_slot(slot)
        logger.info(
            "Adopted an existing subscription for a channel the pool has no slot for",
            extra={
                "broadcaster_id": broadcaster_id,
                "coverage_type": coverage_type.value,
                "subscription_id": row.subscription_id,
                "connection": connection.connection_id,
            },
        )
        return True

    def _session_fingerprint(self) -> Tuple[Tuple[int, Optional[str]], ...]:
        """The connections the pool holds and the session each is on now."""
        return tuple(
            (connection.connection_id, self._session_id(connection))
            for connection in self._connections
        )

    def _invalidate_adoption_snapshot(self, reason: str) -> None:
        """Drop the preflight snapshot: something made its rows untrue.

        Called where a row can become a stale POSITIVE -- a delete, a
        revocation, a retirement -- and on growth, whose new session no
        snapshot describes. NOT called when a caller adopts from the snapshot:
        that leaves every other row exactly as true as it was, and dropping it
        there would put a Helix walk back on every blocked channel.
        """
        if self._adoption_rows is None and not self._adoption_types:
            # Nothing cached, but a walk may be in flight, and its rows are
            # exactly as stale as the ones this call would have dropped.
            self._adoption_generation += 1
            return
        self._adoption_rows = None
        self._adoption_types = set()
        self._adoption_expires_ms = 0
        self._adoption_generation += 1
        logger.debug(
            "Capacity preflight snapshot invalidated", extra={"reason": reason}
        )

    async def _create_one(
        self, broadcaster_id: int, coverage_type: CoverageType, connection: _Connection
    ) -> str:
        """Create one subscription of one type. Consumes one reservation.

        Every path through this -- success, adoption, refusal, a connection
        retired underneath it, a reconnect that invalidates the result --
        gives up exactly one of the reserved subscriptions, so the caller's
        accounting stays a simple count.
        """
        # Read BEFORE the create, not after, and once per LISTEN CALL rather
        # than once per channel. The library builds the POST's transport from
        # whatever session is current when the request is issued, and its
        # socket thread can complete a reconnect -- and so change the session
        # -- while that request is in flight. Stamping the slot with the
        # session read AFTER the await therefore labelled a subscription made
        # on the OLD session with the NEW one, and `_slot_is_current` would
        # then agree with itself for ever: the session check passes, the
        # library's registry holds the id because `_subscribe` added it, and
        # `create()` hands the ghost back with no Twitch call while nothing
        # delivers for that channel. A single reading shared by both halves of
        # a pair has the same defect, one reconnect narrower.
        session_before = self._session_id(connection)
        subscription_id = None
        adopted = None
        try:
            subscription_id = await self._listen(connection, broadcaster_id, coverage_type)
        except EventSubSubscriptionConflict:
            # The interface says a duplicate create adopts rather than fails
            # (FR-005). Twitch answers 409 when this exact subscription is
            # already there, which happens whenever the actual set is stale.
            # The adoption is per type: a chat conflict may only ever adopt a
            # chat subscription.
            adopted = await self._adopt_conflict(broadcaster_id, coverage_type)
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

        if adopted is not None:
            # The 409 path. The reservation was given up by the `finally`
            # above, and only AFTER `_adopt_conflict` recorded the adopted
            # subscription -- so `load` never dips between the two either.
            return adopted

        async with self._lock:
            if self._connection_by_id(connection.connection_id) is None:
                connection.reserved = max(0, connection.reserved - 1)
                # The supervisor retired this connection while the create was
                # in flight -- it runs on this loop and the create above is an
                # await. Recording the slot now would re-add an entry
                # `_retire` has already cleared, and every later create would
                # hand back that dead id without ever contacting Twitch.
                logger.warning(
                    "Connection was retired mid-create, discarding the subscription",
                    extra={
                        "broadcaster_id": broadcaster_id,
                        "coverage_type": coverage_type.value,
                        "connection": connection.connection_id,
                        "subscription_id": subscription_id,
                    },
                )
                raise TransportError(
                    f"connection {connection.connection_id} was lost while subscribing "
                    f"{coverage_type.value} for broadcaster {broadcaster_id}"
                )
            session_now = self._session_id(connection)
            reconnected = (
                session_before is not None
                and session_now is not None
                and session_before != session_now
            )
            if not reconnected:
                # Decremented in the SAME critical section that records the
                # subscription, so `load` never dips between the two.
                connection.reserved = max(0, connection.reserved - 1)
                slot = _Slot(
                    broadcaster_id=broadcaster_id,
                    coverage_type=coverage_type,
                    connection_id=connection.connection_id,
                    subscription_id=subscription_id,
                    session_id=session_before,
                )
                connection.subscription_ids.add(subscription_id)
                self._record_slot(slot)

        if not reconnected:
            return subscription_id

        # The socket reconnected while this create was in flight, and which
        # session the subscription landed on cannot be known from out here.
        # `_subscribe` reads the session when it builds the POST body, so a
        # reconnect that finished before that moment put it on the NEW session
        # -- live, with a callback -- and one that finished after put it on the
        # old one, where `_resubscribe()` will not restore it because it only
        # re-creates what the registry held when it took its snapshot.
        #
        # Deleting covers both. A subscription on the dead session answers
        # "not found", which `_delete_one` already treats as success; a live
        # one is removed and re-created cleanly on the next pass. Guessing
        # instead was worse in one direction than the other: dropping the
        # library's callback for a subscription that turned out to be LIVE
        # left it delivering into nothing, and the next pass would take
        # Twitch's 409 and adopt it -- `_adopt_conflict` restores the pool's
        # indexes but not the library's callback -- so the channel was counted
        # as covered and dark for good. That is the exact failure this whole
        # check exists to prevent. And the live case is not hypothetical:
        # Twitch's graceful `session_reconnect` changes the session id AND
        # migrates the subscriptions, and the library does not call
        # `_resubscribe()` on that path at all.
        #
        # The delete runs OUTSIDE the lock (it is a Twitch round trip) and
        # BEFORE the registry is cleared. If it fails, the registry entry
        # stays, which is the safe side of that error: on the live-session
        # branch the subscription and its callback are both still intact and
        # the next enumeration simply adopts a working subscription.
        #
        # The RESERVATION is held across that round trip, and released only
        # once it is over. Giving it up with the delete still in flight left
        # the subscription counted in neither `reserved` nor
        # `subscription_ids` while it may well still exist on Twitch, so
        # another worker could route a channel into a slot that was not really
        # free and push the session past its cap.
        #
        # Residual, accepted: if the DELETE itself FAILS, the reservation is
        # still released while a possibly-live subscription remains. The
        # alternatives are worse -- holding the reservation for ever leaks the
        # slot, and counting a subscription with no slot as occupancy leaves
        # something nothing can clear. It needs this reconnect race AND a
        # failed delete AND the session to be at its cap before it costs
        # anything, and what it then costs is one refused create that
        # `_classify` already understands, repaired on the next pass when the
        # 409 is adopted.
        logger.warning(
            "Connection reconnected mid-create, discarding the subscription",
            extra={
                "broadcaster_id": broadcaster_id,
                "coverage_type": coverage_type.value,
                "connection": connection.connection_id,
                "subscription_id": subscription_id,
                "session_at_create": session_before,
                "session_now": session_now,
            },
        )
        try:
            await self._delete_one(subscription_id)
            self._forget_library_subscription(connection, subscription_id)
        finally:
            await self._release(connection)
        raise TransportError(
            f"connection {connection.connection_id} reconnected while "
            f"subscribing {coverage_type.value} for broadcaster {broadcaster_id}"
        )

    async def _listen(
        self, connection: _Connection, broadcaster_id: int, coverage_type: CoverageType
    ) -> str:
        """Issue the listen call for one coverage type, with its own callback.

        Each type gets its OWN handler: a notification delivered to the chat
        publisher would be mapped as a chat message and land on
        `chat-messages`, which the Flink job reads.

        Both use `self.user_id`, the operator account already authenticated --
        `user:read:chat` covers both types, so nothing is reseeded (FR-016).
        The listener is resolved from the websocket at call time rather than
        bound earlier, because a reconnect can replace it.
        """
        if coverage_type is CoverageType.CHAT:
            return await connection.websocket.listen_channel_chat_message(
                str(broadcaster_id), self.user_id, self._on_event
            )
        return await connection.websocket.listen_channel_chat_notification(
            str(broadcaster_id), self.user_id, self._on_notification
        )

    # -- coverage ---------------------------------------------------------

    def channel_coverage(self, broadcaster_id: int) -> ChannelCoverage:
        """What this pool holds for one channel, as a single derived answer."""
        chat = self._slots.get((broadcaster_id, CoverageType.CHAT))
        notification = self._slots.get((broadcaster_id, CoverageType.NOTIFICATION))
        refused_until = self._auxiliary_refused_until_ms(broadcaster_id)

        if chat is not None and notification is not None:
            state = "complete"
        elif chat is not None and refused_until is not None:
            # The only state that reports a channel as actual without both
            # halves, and only until the hold-off runs out.
            state = "degraded_chat_only"
        elif chat is not None:
            state = "chat_only"
        elif notification is not None:
            state = "notification_only"
        else:
            state = "absent"

        return ChannelCoverage(
            broadcaster_id=broadcaster_id,
            chat_slot=chat,
            notification_slot=notification,
            auxiliary_refused_until_ms=refused_until,
            state=state,
        )

    def coverage_counts(self) -> Dict[str, int]:
        """Channels by coverage state -- CHANNELS, not subscriptions.

        The unit is the whole point (data-model I4). `occupancy()` answers in
        subscriptions and is bounded by 300 per connection; this answers in
        channels and is bounded by the 450-channel maximum. Mixing them is how
        a 450-channel pool reads as 900 against a 450 limit, or a 300-
        subscription session reads as full at 150.
        """
        counts = {state: 0 for state in COVERAGE_STATES}
        for broadcaster_id in {key[0] for key in self._slots}:
            state = self.channel_coverage(broadcaster_id).state
            if state in counts:
                counts[state] += 1
        return counts

    def _holds(self, broadcaster_id: int, coverage_type: CoverageType) -> bool:
        return (broadcaster_id, coverage_type) in self._slots

    def partial_channel_handles(self) -> Dict[int, str]:
        """One delete handle per ORDINARY partial channel. Drop-only (T021).

        A channel that holds exactly one of its two coverage types is not in
        the actual set -- `list()` withholds it on purpose, because that is
        what makes the reconciler re-create the missing half. The cost of that
        is that once the channel leaves the desired set it is in nothing the
        reconciler diffs: not in `_actual`, so never dropped; not in `desired`,
        so never created. The surviving subscription would then hold one of the
        300 slots on its session for the life of the process (FR-014, NFR-003).

        This is the reclamation handle for exactly that case, and nothing
        else. It is NOT a second actual set: a channel here is still not
        covered, still not counted, and still repairable by the ordinary
        create path while it is wanted.

        `complete` and active `degraded_chat_only` channels are excluded.
        Both are already actual, so the ordinary diff drops them -- listing
        them here would give the reconciler two routes to the same delete, and
        for a degraded channel it would evict the live chat that its bounded
        hold-off exists to protect (I1, I17).

        The handle is each channel's CURRENT live id, resolved the same way
        `delete()` resolves one, so a reconnect that rotated it cannot hand
        back an id Twitch has already collected.
        """
        handles: Dict[int, str] = {}
        for broadcaster_id in sorted({key[0] for key in self._slots}):
            coverage = self.channel_coverage(broadcaster_id)
            if coverage.state not in PARTIAL_COVERAGE_STATES:
                continue
            slot = coverage.chat_slot or coverage.notification_slot
            if slot is None:  # pragma: no cover -- the state implies one
                continue
            handles[broadcaster_id] = self._live_handle_for(slot)
        return handles

    def _live_handle_for(self, slot: _Slot) -> str:
        """The id a delete should be issued against for this slot right now."""
        connection = self._connection_by_id(slot.connection_id)
        live = self._live_subscription_ids(connection, slot)
        return live[0] if live else slot.subscription_id

    def _handle_for(self, broadcaster_id: int) -> str:
        """The id the reconciler holds for this channel: the chat half."""
        chat = self._slots.get((broadcaster_id, CoverageType.CHAT))
        if chat is None:
            raise TransportError(
                f"broadcaster {broadcaster_id} has no chat subscription after create"
            )
        return chat.subscription_id

    def _record_slot(self, slot: _Slot) -> None:
        """Index one slot under its own key and its own id."""
        self._slots[(slot.broadcaster_id, slot.coverage_type)] = slot
        self._by_subscription[slot.subscription_id] = slot
        if slot.coverage_type is CoverageType.NOTIFICATION:
            # Coverage is complete for the auxiliary type, so whatever refusal
            # put this channel in a hold-off is over (data-model §5.4.1).
            self._clear_auxiliary_refusal(slot.broadcaster_id, "notification covered")

    def _refresh_slots(self, broadcaster_id: int) -> None:
        """Drop the slots that are no longer real, one coverage type at a time.

        A live connection is not proof of a live subscription. When the
        library's `_resubscribe()` gives up part way through a reconnect the
        socket stays up while the channels past the failure point no longer
        exist on Twitch, and `_slots` still maps them to their pre-reconnect
        ids -- and it can give up between the two halves of one channel, so
        each half has to be judged on its own.
        """
        for coverage_type in CoverageType:
            slot = self._slots.get((broadcaster_id, coverage_type))
            if slot is None:
                continue
            connection = self._connection_by_id(slot.connection_id)
            if connection is None:
                # The slot points at a connection that is gone. Keeping it
                # would report the channel as covered while no socket delivers
                # for it -- dark, and permanently so, because nothing else
                # clears a slot whose connection has already been retired.
                self._forget_slot(slot)
                self._clear_auxiliary_refusal(broadcaster_id, "connection retired")
                continue
            if self._session_changed(slot, connection):
                # A reconnect. Twitch holds nothing from the old session, and
                # the refusal that started any hold-off may have been specific
                # to it -- so this is a free opportunity to retest it (I17).
                # The same is true of anything that session reported about its
                # own capacity, so `full_at` is re-evaluated here too, BEFORE
                # this channel is routed anywhere (decision 28, I27).
                self._reevaluate_full_at()
                logger.warning(
                    "Recorded subscription is on a replaced session, recreating",
                    extra={
                        "broadcaster_id": broadcaster_id,
                        "coverage_type": coverage_type.value,
                        "connection": connection.connection_id,
                        "subscription_id": slot.subscription_id,
                    },
                )
                self._forget_slot(slot)
                self._clear_auxiliary_refusal(broadcaster_id, "websocket reconnected")
                continue
            if not self._slot_is_current(slot, connection):
                logger.warning(
                    "Recorded subscription is not on its connection any more, recreating",
                    extra={
                        "broadcaster_id": broadcaster_id,
                        "coverage_type": coverage_type.value,
                        "connection": connection.connection_id,
                        "subscription_id": slot.subscription_id,
                    },
                )
                self._forget_slot(slot)

    def _coverage_to_create(self, broadcaster_id: int) -> List[CoverageType]:
        """The types this channel is missing, minus anything held off.

        Chat first, always: it is the data path, and it is what makes an
        auxiliary refusal an auxiliary one rather than a channel refusal.
        """
        to_create = [
            coverage_type
            for coverage_type in CoverageType
            if not self._holds(broadcaster_id, coverage_type)
        ]
        if (
            CoverageType.NOTIFICATION in to_create
            and self._auxiliary_refused_until_ms(broadcaster_id) is not None
        ):
            # Retrying inside the hold-off is the hot loop D2 exists to stop:
            # the reconciler asks every pass, Twitch refuses every pass, and
            # the create budget goes on a subscription that cannot be made.
            to_create.remove(CoverageType.NOTIFICATION)
        return to_create

    # -- auxiliary refusal (D2, T024) -------------------------------------

    def _auxiliary_refused_until_ms(self, broadcaster_id: int) -> Optional[int]:
        """The live hold-off deadline for this channel, or None.

        An expired deadline is not a hold-off, so it is dropped here rather
        than reported: `degraded_chat_only` has to end on its own, or it would
        be a standing exception to FR-001 instead of a bounded one.
        """
        deadline = self._auxiliary_refused_until.get(broadcaster_id)
        if deadline is None:
            return None
        if deadline <= self._monotonic_ms():
            del self._auxiliary_refused_until[broadcaster_id]
            return None
        return deadline

    def _record_auxiliary_refusal(self, broadcaster_id: int, error: Exception) -> None:
        deadline = (
            self._monotonic_ms() + self.auxiliary_refusal_retry_seconds * 1000
        )
        self._auxiliary_refused_until[broadcaster_id] = deadline
        logger.warning(
            "Twitch refused the chat-notification subscription, keeping chat and "
            "degrading suppression coverage for this channel",
            extra={
                "broadcaster_id": broadcaster_id,
                "retry_after_seconds": self.auxiliary_refusal_retry_seconds,
                "error": str(error),
            },
        )

    def _clear_auxiliary_refusal(self, broadcaster_id: int, reason: str) -> None:
        if self._auxiliary_refused_until.pop(broadcaster_id, None) is None:
            return
        logger.info(
            "Chat-notification hold-off cleared, the channel is repairable again",
            extra={"broadcaster_id": broadcaster_id, "reason": reason},
        )

    async def delete(self, subscription_id: str) -> None:
        """Drop a channel: BOTH of its subscriptions, independently (T024).

        The reconciler is channel-keyed and holds one id per channel, so the
        id it passes identifies the CHANNEL; every coverage type that channel
        still holds is deleted. A subscription that is already gone is
        success, not an error.

        After a socket loses its keepalive the library reconnects and
        re-subscribes everything, which gives every subscription on that
        socket a NEW id -- per type. The id the reconciler holds is then
        stale, so each type's live id is resolved from the connection before
        its own delete, and the recorded one is only a fallback.

        A rotated id is the id the reconciler ACTUALLY holds: `list()` reports
        what Twitch has now, so the very next enumeration replaces the handle
        with the post-reconnect one while `_by_subscription` still holds the
        pre-reconnect pair. Resolving that id has to answer with the CHANNEL,
        not with one subscription of it -- deleting only the half named would
        leave the sibling live on Twitch, in the library's registry and in the
        occupancy count, as a partial orphan nothing ever drops (the channel
        has left the desired set, so no pass creates it, and `list()` does not
        yield a partial channel, so no pass drops it either).
        """
        slot = self._by_subscription.get(subscription_id)
        if slot is not None:
            await self._delete_channel(slot.broadcaster_id)
            return

        # An id this pool does not recognise, which is not the same as an id
        # that is not ours. The library's own registry is the only place a
        # rotated id appears, and it carries the condition AND the type -- so
        # both halves of the channel's identity come back from one lookup.
        resolved = self._resolve_unrecognised(subscription_id)
        if resolved is None:
            # Genuinely unknown, or already gone. The DELETE is still issued
            # so the call stays idempotent, and there is nothing local to
            # clean up because nothing local knows this id.
            await self._delete_one(subscription_id)
            return

        connection, broadcaster_id, coverage_type = resolved
        if broadcaster_id is None or coverage_type is None:
            # The registry has the id but not a usable identity behind it.
            # Delete exactly what was named and clear the library's entry, so
            # the socket does not resubscribe it on its next reconnect.
            # Raising instead would be worse than useless: the DELETE has
            # already succeeded, so `_drop_one` would never pop `_actual` and
            # the reconciler would re-issue the same delete every pass for
            # ever.
            await self._delete_one(subscription_id)
            self._forget_library_subscription(connection, subscription_id)
            async with self._lock:
                connection.subscription_ids.discard(subscription_id)
            return

        await self._delete_channel(
            broadcaster_id, orphans={coverage_type: (connection, subscription_id)}
        )

    async def _delete_channel(
        self,
        broadcaster_id: int,
        orphans: Optional[Dict[CoverageType, Tuple[_Connection, str]]] = None,
    ) -> None:
        """Delete every coverage type this channel still holds, in one pass.

        `orphans` names a live id per coverage type for which the pool has no
        slot at all -- the rotated id a caller handed in when its own slot has
        already been forgotten. Without it that id would be deleted from
        Twitch by nobody and resubscribed by the library on its next
        reconnect.

        Both halves are attempted whatever the other does. The one that failed
        keeps its slot and its place in the occupancy count, so the next pass
        retries exactly it -- aborting early would leave a live subscription
        with no slot to find it by. The first error is re-raised once every
        type has been attempted.
        """
        orphans = orphans or {}
        failure = None
        for coverage_type in CoverageType:
            target = self._slots.get((broadcaster_id, coverage_type))
            try:
                if target is not None:
                    await self._delete_slot(target)
                    continue
                orphan = orphans.get(coverage_type)
                if orphan is None:
                    continue
                await self._delete_orphan(*orphan)
            except TransportError as e:
                failure = failure or e

        if failure is None:
            self._clear_auxiliary_refusal(broadcaster_id, "channel dropped")
        else:
            raise failure

    async def _delete_orphan(
        self, connection: _Connection, subscription_id: str
    ) -> None:
        """Delete a live id of a coverage type the pool holds no slot for."""
        await self._delete_one(subscription_id)
        self._forget_library_subscription(connection, subscription_id)
        async with self._lock:
            connection.subscription_ids.discard(subscription_id)
            self._by_subscription.pop(subscription_id, None)

    async def _delete_slot(self, slot: _Slot) -> None:
        """Delete one coverage type's subscription and clear its indexes."""
        connection = self._connection_by_id(slot.connection_id)
        targets = self._live_subscription_ids(connection, slot)
        for target in targets:
            await self._delete_one(target)
            if connection is not None:
                self._forget_library_subscription(connection, target)

        async with self._lock:
            if connection is not None:
                connection.subscription_ids.discard(slot.subscription_id)
                for target in targets:
                    connection.subscription_ids.discard(target)
            self._slots.pop((slot.broadcaster_id, slot.coverage_type), None)
            self._by_subscription.pop(slot.subscription_id, None)

    async def list(self) -> AsyncIterator[ExistingSubscription]:
        """Yield the channels this pool can actually receive BOTH halves for.

        Two type-filtered Helix walks, joined per channel (R1). A channel is
        yielded only when its chat AND its notification subscription are
        `enabled` on a session this pool holds -- or when it is in an active
        auxiliary-refusal hold-off, which is the one bounded exception (I1).

        The reconciler stays channel-keyed: what it gets back is one entry per
        channel carrying the CHAT subscription id, exactly as before.

        Page count, never `total` -- the spike saw `total` report 300 while
        the pages held 396 (D6). The library paginates transparently, so the
        count comes from what actually arrives.

        Subscriptions on any other session are skipped. A websocket session
        dies with the process that opened it, so one this pool does not hold
        can never deliver a message to it: counting it in the actual set would
        leave the channel silently dark. Twitch collects the leftovers itself.

        Either walk failing propagates. "Clean" has to mean BOTH walks
        finished, or the reconciler would delete live subscriptions it merely
        failed to see -- it holds its drops back until one enumeration
        completes, and this is what tells it one did not.
        """
        live_sessions = self._live_session_ids()
        stats = {
            "chat_seen": 0,
            "chat_pages": 0,
            "notification_seen": 0,
            "notification_pages": 0,
        }

        # The chat walk first and in full: it carries the id the reconciler
        # gets back, so nothing can be yielded before it is known. The session
        # each row is on comes back with it, because that is what says whether
        # a degraded channel's connection has reconnected since its refusal.
        chat_ids: Dict[int, str] = {}
        chat_sessions: Dict[int, Optional[str]] = {}
        async for broadcaster_id, subscription_id, session_id in self._walk(
            CoverageType.CHAT, live_sessions, stats
        ):
            chat_ids[broadcaster_id] = subscription_id
            chat_sessions[broadcaster_id] = session_id

        # The notification walk streams, so a channel is yielded the moment
        # its pair is complete. A walk that dies half way therefore still
        # reports what it saw, and the reconciler merges that with what it
        # already had rather than treating the rest as absent (NFR-003).
        complete: Set[int] = set()
        async for broadcaster_id, _, _ in self._walk(
            CoverageType.NOTIFICATION, live_sessions, stats
        ):
            if broadcaster_id not in chat_ids or broadcaster_id in complete:
                continue
            complete.add(broadcaster_id)
            yield ExistingSubscription(
                subscription_id=chat_ids[broadcaster_id],
                broadcaster_id=broadcaster_id,
                status="enabled",
            )

        degraded = 0
        reconnected = 0
        for broadcaster_id, subscription_id in chat_ids.items():
            if broadcaster_id in complete:
                continue
            if self._auxiliary_refused_until_ms(broadcaster_id) is None:
                # An ordinary partial state. Leaving it out is what makes the
                # reconciler re-create the missing half on the next pass.
                continue
            if self._reconnected_out_of_hold_off(
                broadcaster_id, subscription_id, chat_sessions.get(broadcaster_id)
            ):
                # The hold-off has just been cleared by the reconnect, so this
                # is an ordinary `chat_only` channel again. Withholding it is
                # the repair: it drops out of the actual set and the
                # reconciler's own `to_create` makes the missing half on this
                # same pass (I17).
                reconnected += 1
                continue
            degraded += 1
            yield ExistingSubscription(
                subscription_id=subscription_id,
                broadcaster_id=broadcaster_id,
                status="enabled",
            )

        logger.info(
            "Enumerated EventSub subscriptions",
            extra={
                "pages": stats["chat_pages"] + stats["notification_pages"],
                "seen": stats["chat_seen"] + stats["notification_seen"],
                "chat_on_our_sessions": len(chat_ids),
                "complete_channels": len(complete),
                "degraded_channels": degraded,
                "hold_offs_cleared_by_reconnect": reconnected,
                "connections": len(self._connections),
            },
        )

    async def _walk(
        self,
        coverage_type: CoverageType,
        live_sessions: Set[str],
        stats: Dict[str, int],
    ) -> AsyncIterator[Tuple[int, str, Optional[str]]]:
        """One type-filtered Helix walk, yielding only what this pool can use.

        `enabled` on a session this pool holds is the whole filter, and it has
        to hold for BOTH halves or a channel whose notification subscription
        was revoked would read as covered.

        The session comes back with each row rather than being dropped once it
        has passed the filter: which of this pool's sessions a subscription is
        on is what distinguishes a channel whose connection has reconnected
        since its slot was stamped from one that has simply not been repaired.
        """
        result = await self.twitch.get_eventsub_subscriptions(
            sub_type=coverage_type.subscription_type, target_token=AuthType.USER
        )
        pages = 1
        cursor = self._cursor_of(result)
        async for subscription in result:
            moved = self._cursor_of(result)
            if moved != cursor:
                pages += 1
                cursor = moved
            stats[f"{coverage_type.value}_seen"] += 1
            if getattr(subscription, "status", None) not in ADOPTABLE_STATUSES:
                continue
            transport = getattr(subscription, "transport", None) or {}
            session_id = transport.get("session_id")
            if session_id not in live_sessions:
                continue
            broadcaster_id = (getattr(subscription, "condition", None) or {}).get(
                "broadcaster_user_id"
            )
            if broadcaster_id is None:
                continue
            yield int(broadcaster_id), subscription.id, session_id
        stats[f"{coverage_type.value}_pages"] += pages

    def _reconnected_out_of_hold_off(
        self,
        broadcaster_id: int,
        live_subscription_id: str,
        live_session_id: Optional[str],
    ) -> bool:
        """Has a degraded channel's connection reconnected since its refusal?

        `_refresh_slots()` already answers this -- but it only runs inside
        `create()`, and the reconciler never calls `create()` for a channel it
        already counts as actual. A degraded channel is actual for the whole
        hold-off, precisely so the reconciler does not hot-loop on a refusal
        it cannot fix, so on the deployed path nothing ever noticed the
        reconnect and I17's "immediately on reconnect" meant "in at most an
        hour". The enumeration has to ask the question too.

        Clearing the hold-off HERE, before the channel is judged actual, is
        what makes the repair immediate: the channel is an ordinary
        `chat_only` again, so it is withheld from the actual set and the
        reconciler's own `to_create` diff makes the missing notification half
        on this same pass. No `create()` call is needed to clear it and no
        3600 seconds have to elapse; without a reconnect nothing here fires
        and the hold-off goes on holding.

        The surviving chat subscription is re-stamped against the id and
        session Helix has just reported rather than forgotten. It is live, and
        dropping the slot would make the repair subscribe a SECOND chat
        subscription for the same channel (FR-002, I2).
        """
        slot = self._slots.get((broadcaster_id, CoverageType.CHAT))
        if slot is None:
            # Nothing recorded to compare a session against, so there is no
            # evidence of a reconnect. Helix says a chat subscription is live
            # on one of this pool's sessions; leave the hold-off alone rather
            # than clear it on a guess.
            return False

        connection = (
            self._connection_by_session(live_session_id) if live_session_id else None
        ) or self._connection_by_id(slot.connection_id)
        if connection is None:
            # The connection is gone entirely. A refusal cannot outlive the
            # session it was made on (I17), and the slot points at nothing.
            self._forget_slot(slot)
            self._clear_auxiliary_refusal(broadcaster_id, "connection retired")
            return True

        if not self._session_changed(slot, connection):
            return False

        logger.info(
            "Degraded channel's connection has reconnected, repairing its "
            "notification coverage now rather than at the hold-off's deadline",
            extra={
                "broadcaster_id": broadcaster_id,
                "connection": connection.connection_id,
                "session_at_create": slot.session_id,
                "session_now": self._session_id(connection),
            },
        )
        self._clear_auxiliary_refusal(broadcaster_id, "websocket reconnected")
        self._restamp_slot(slot, connection, live_subscription_id)
        return True

    def _restamp_slot(
        self, slot: _Slot, connection: _Connection, subscription_id: str
    ) -> None:
        """Re-record a surviving slot against the id and session it is on now.

        One slot in, one slot out. The old id leaves every index -- including
        the occupancy count it was held under -- and the new one takes its
        place, so the channel neither loses the coverage type it still has nor
        counts it twice.
        """
        self._slots.pop((slot.broadcaster_id, slot.coverage_type), None)
        self._by_subscription.pop(slot.subscription_id, None)
        previous = self._connection_by_id(slot.connection_id)
        if previous is not None:
            previous.subscription_ids.discard(slot.subscription_id)
        connection.subscription_ids.discard(slot.subscription_id)
        connection.subscription_ids.add(subscription_id)
        self._record_slot(
            _Slot(
                broadcaster_id=slot.broadcaster_id,
                coverage_type=slot.coverage_type,
                connection_id=connection.connection_id,
                subscription_id=subscription_id,
                session_id=self._session_id(connection),
            )
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

    def connection_capacity(self) -> Dict[str, dict]:
        """Per-connection capacity, including full BELOW the cap (I27).

        The same shape and the same keys as `occupancy()`, so the reconciler
        can publish a bounded per-connection gauge from it and the two views
        can never disagree about which connections exist. A retired connection
        disappears from both.

        `free` is USABLE free -- it counts reservations and honours `full_at`,
        because at 900 of 900 the difference between `occupancy` and `load`,
        and between the cap and the level Twitch actually refused at, is the
        entire margin. `full_below_cap` is the stranded-capacity condition:
        slots the capacity model counts on that this connection will not give.
        """
        self._reevaluate_full_at()
        return {
            str(connection.connection_id): {
                "occupancy": connection.occupancy,
                "cap": self.cap,
                "full_at": connection.full_at,
                "free": self._free_slots(connection),
                "full_below_cap": (
                    connection.full_at is not None and connection.full_at < self.cap
                ),
            }
            for connection in self._connections
        }

    # -- routing and growth -----------------------------------------------

    def route(self, broadcaster_id: int, slots: int = 1) -> Optional[_Connection]:
        """The connection `slots` subscriptions for this channel belong on.

        `slots` is a SUBSCRIPTION count -- two for a new channel, one for a
        repair -- because that is the unit the 300-per-session cap is in. A
        connection with room for one is not a connection with room for a pair,
        and treating them as the same is how a session goes to 301.

        Locality first: a channel's pair is kept on one connection when that
        connection has room, so a socket death costs the whole channel at once
        rather than leaving a partial state behind. When it does not have
        room, rendezvous order decides and the pair may split across two
        connections -- legal and modelled (R2), because both partial states
        are already convergent.

        Public because the routing rule is the part worth testing directly
        (T019a): the same broadcaster must come back to the same connection
        across reconciles.
        """
        # Before anything is placed, not after: a `full_at` left over from a
        # session that has since been replaced would take a connection with
        # room out of routing, and with no free reserve that is exactly the
        # capacity the last channel needs (decision 28).
        self._reevaluate_full_at()
        for connection in self._ordered_for(broadcaster_id):
            if self._has_room(connection, slots):
                return connection
        return None

    def _ordered_for(self, broadcaster_id: int) -> List[_Connection]:
        """Every connection, in the order this channel prefers them.

        Locality first -- the connections already carrying part of this
        channel's pair -- then rendezvous order. Deterministic, and shared by
        whole-pair routing and the split fallback so the two cannot disagree
        about which connection a channel prefers.
        """
        ordered = self._connections_holding(broadcaster_id)
        seen = {connection.connection_id for connection in ordered}
        ordered.extend(
            connection
            for connection in sorted(
                self._connections,
                key=lambda connection: _score(broadcaster_id, connection.connection_id),
                reverse=True,
            )
            if connection.connection_id not in seen
        )
        return ordered

    def _connections_holding(self, broadcaster_id: int) -> List[_Connection]:
        """The connections already carrying part of this channel's pair."""
        holding: List[_Connection] = []
        for coverage_type in CoverageType:
            slot = self._slots.get((broadcaster_id, coverage_type))
            if slot is None:
                continue
            connection = self._connection_by_id(slot.connection_id)
            if connection is not None and connection not in holding:
                holding.append(connection)
        return holding

    def _free_slots(self, connection: _Connection) -> int:
        """Subscriptions this connection can still take, right now.

        `load`, not `occupancy`: a reserved slot is spoken for. And the
        ceiling is the level Twitch actually refused at when there is one,
        because a connection full below the cap offers fewer slots than the
        capacity model counts on (decision 28).
        """
        ceiling = (
            self.cap if connection.full_at is None else min(self.cap, connection.full_at)
        )
        return max(0, ceiling - connection.load)

    def _usable_free_slots(self) -> int:
        """Free subscriptions across the OPEN connections.

        A connection that has not been opened is not a free slot, so this is
        deliberately not `max_connections * cap - occupancy`: growth is a
        separate decision from placement.
        """
        return sum(self._free_slots(connection) for connection in self._connections)

    def _has_room(self, connection: _Connection, slots: int) -> bool:
        """Can this connection take `slots` more subscriptions right now?

        `load` already counts in-flight reservations, so this is the same
        answer for a worker that has not created anything yet as for one that
        has.
        """
        return self._free_slots(connection) >= slots

    def _reevaluate_full_at(self) -> None:
        """Forget a `full_at` whose session is gone (decision 28, I27).

        `full_at` is one session's observation, not a property of the socket.
        A reconnect gives the connection a new session that has refused
        nothing and holds nothing Twitch counts against the old one, so the
        number is re-evaluated there rather than remembered -- otherwise the
        connection stays out of routing for the life of the process while the
        pool opens sockets around it.

        Time alone never clears it. Only a session transition does, because
        only a session transition invalidates the observation. Retirement
        needs nothing here: the whole connection goes.
        """
        for connection in self._connections:
            if connection.full_at is None:
                continue
            session = self._session_id(connection)
            if session == connection.full_at_session:
                continue
            logger.info(
                "EventSub session changed, re-evaluating the level it was full at",
                extra={
                    "connection": connection.connection_id,
                    "occupancy": connection.occupancy,
                    "was_full_at": connection.full_at,
                    "session_at_refusal": connection.full_at_session,
                    "session_now": session,
                },
            )
            connection.full_at = None
            connection.full_at_session = None

    async def _reserve(self, broadcaster_id: int, slots: int = 1) -> _Connection:
        """Pick ONE connection for this channel and hold `slots` on it.

        The single-connection reservation: a repair needs one slot, and a
        whole pair still comes here when one connection can hold both.
        `_reserve_pair` is the entry point for a new channel, because a pair
        may also be placed as one slot on each of two connections.
        """
        async with self._lock:
            connection = self.route(broadcaster_id, slots=slots)
            if connection is None:
                connection = await self._grow_under_lock()
            connection.reserved += slots
            return connection

    async def _reserve_pair(
        self, broadcaster_id: int
    ) -> Dict[CoverageType, _Connection]:
        """Hold BOTH of a channel's slots, atomically (decision 28, I25).

        Returns one connection per coverage type -- the same connection twice
        when the pair is co-located, two different ones when it is split.

        Three steps, in this order, all inside ONE critical section:

        1. **Co-location first.** The ordinary `route()` answer for two slots:
           the connection already holding part of the channel, else rendezvous
           order. A pair kept together means a socket death costs the whole
           channel at once instead of leaving a partial state behind.
        2. **Growth second**, while the pool is BELOW `max_connections`. A new
           session can hold the pair together, so growing preserves locality
           AND leaves the one-slot holes for the one-slot repairs that are the
           only thing able to use them. Splitting here instead spent both
           holes, permanently fragmented a channel, and still left the pool
           needing the connection it had declined to open.
        3. **Split last**, and only when growth is not available: at the
           connection ceiling, or when a connect has just failed and two
           existing connections have room for one slot each. Two free slots on
           two different connections are two free slots; refusing the pair
           because neither alone can hold it turns a full pool into a falsely
           full one, and at the ceiling that is the difference between
           converging at 450 and stalling at 449.

        A growth failure is not silently absorbed. The backoff `_grow_under_lock`
        arms stays armed, so the rest of the batch still fails fast rather than
        queueing behind its own connect; the split is a fallback for THIS
        placement only, and with no two one-slot homes to fall back on the
        error is re-raised unchanged.

        The atomicity is the point, and it is why every branch runs under one
        `_lock` acquisition. Reserving the two halves one at a time lets two
        concurrent pairs each take half of the same two free slots: both then
        fail on create, at the exact moment there is no slack to recover with.
        Either both halves are held here or neither is.
        """
        async with self._lock:
            connection = self.route(broadcaster_id, slots=2)
            if connection is not None:
                connection.reserved += 2
                return {coverage_type: connection for coverage_type in CoverageType}

            if len(self._connections) < self.max_connections:
                try:
                    connection = await self._grow_under_lock()
                except Exception as growth_error:
                    # The backoff this armed is deliberately left alone: it
                    # belongs to the pool, not to this placement.
                    split = self._split_candidates(broadcaster_id)
                    if split is None:
                        raise
                    logger.warning(
                        "Could not open a connection for a whole pair, splitting "
                        "it across the free slots that already exist",
                        extra={
                            "broadcaster_id": broadcaster_id,
                            "connections": len(self._connections),
                            "error": str(growth_error),
                        },
                    )
                    for half in split:
                        half.reserved += 1
                    return dict(zip(CoverageType, split))
                connection.reserved += 2
                return {coverage_type: connection for coverage_type in CoverageType}

            split = self._split_candidates(broadcaster_id)
            if split is None:
                # At the connection ceiling with fewer than two usable free
                # slots -- which, since `route()` just refused, is the same
                # statement as "no two connections have room for one each".
                # `_grow_under_lock` is where that hard ceiling becomes a
                # capacity error rather than something to wait out.
                connection = await self._grow_under_lock()
                connection.reserved += 2
                return {coverage_type: connection for coverage_type in CoverageType}

            for half in split:
                half.reserved += 1
            return dict(zip(CoverageType, split))

    def _split_candidates(
        self, broadcaster_id: int
    ) -> Optional[List[_Connection]]:
        """Two DIFFERENT connections with room for one slot each, or None.

        `None` means the pool holds fewer than two usable free slots. That
        equivalence is exact rather than convenient: a connection with two
        free slots would have been returned by `route(slots=2)`, so once that
        has refused, every connection has at most one free slot and "at least
        two free in total" and "at least two connections with room for one"
        are the same statement.
        """
        usable = [
            connection
            for connection in self._ordered_for(broadcaster_id)
            if self._has_room(connection, 1)
        ]
        return usable[:2] if len(usable) >= 2 else None

    async def _grow_under_lock(self) -> _Connection:
        """Open one more session for a placement. Called with the lock held.

        The two ways growth fails are not the same failure, and keeping them
        apart is what makes a full account legible (decision 28):

        * At `max_connections` there is nothing to wait for. `_grow` raises
          `PoolCapacityError`, and it is checked BEFORE the recent-growth
          backoff so a full pool always reports capacity distinctly instead of
          reporting whatever a much earlier connect failure left behind.
        * A connect that did not come up may recover, and that one still arms
          the backoff. Growth runs under the lock and a connect can take up to
          `connect_timeout_seconds` to give up, so without the guard every
          remaining channel in the batch queued behind its own 30 s attempt:
          200 channels waiting on a hung Twitch handshake froze the reconciler
          for about an hour, with `reconcile_last_success_timestamp` stopped
          throughout. One failure fails the rest of the batch fast, and the
          next pass tries again.

        A capacity refusal must NOT arm the backoff: waiting cannot empty a
        full account, and the timer would delay the next legitimate placement
        after a delete or a retirement -- which is available immediately.
        """
        if (
            len(self._connections) < self.max_connections
            and time.monotonic() < self._growth_blocked_until
        ):
            raise TransportError("pool growth failed recently, not retrying this pass")
        try:
            return await self._grow()
        except PoolCapacityError:
            raise
        except Exception:
            self._growth_blocked_until = time.monotonic() + self.connect_timeout_seconds
            raise

    async def _release(self, connection: _Connection, slots: int = 1):
        async with self._lock:
            connection.reserved = max(0, connection.reserved - slots)

    async def _grow(self) -> _Connection:
        """Open one more session. Called with the lock held.

        Refuses past `MAX_CONNECTIONS` with `PoolCapacityError`. Twitch allows
        three websocket connections with enabled subscriptions per
        client-id/user-id pair, so this transport tops out at
        `MAX_SUBSCRIPTIONS` subscriptions. Opening a fourth socket does not
        fail at connect time -- it fails later, per subscription, with an
        error this module cannot classify, and rendezvous routing keeps
        sending the same channels back to it. A clear refusal here is the
        difference between "the pool is full" in the log and a silent retry
        loop.

        The error TYPE is what makes that legible now that there is no free
        reserve: a hard ceiling is neither a provider refusal nor a transient
        fault, and the caller must not arm a backoff for it (decision 28).

        `EventSubWebsocket.start()` blocks the calling thread until the
        session_welcome arrives, so it runs on the default executor rather
        than stalling the service's event loop for the length of a connect.
        """
        if len(self._connections) >= self.max_connections:
            raise PoolCapacityError(
                f"pool is at its {self.max_connections}-connection limit "
                f"({self.max_connections * self.cap} subscriptions, "
                f"{MAX_SUBSCRIPTIONS} at the documented Twitch caps); "
                "Twitch allows no more websocket connections for this token"
            )
        websocket = self._connection_factory()
        loop = asyncio.get_running_loop()
        try:
            await asyncio.wait_for(
                loop.run_in_executor(None, websocket.start),
                timeout=self.connect_timeout_seconds,
            )
        except asyncio.TimeoutError as e:
            self._abandon_socket(websocket)
            raise TransportError(
                f"EventSub connection did not come up within "
                f"{self.connect_timeout_seconds}s"
            ) from e
        except asyncio.CancelledError:
            # Shutdown cancels the reconciler task, and that cancellation lands
            # wherever the pass happened to be -- including inside this connect.
            # `except Exception` does NOT catch it on 3.11, so without this
            # branch a SIGTERM during a cold-start `_grow` abandoned the socket
            # in exactly the state the timeout branch exists to clean up: the
            # executor thread still busy-waiting in `start()`, `_keep_loop_alive`
            # still spinning on its own loop, and an open `ClientSession` behind
            # both. Cancelling the future does not stop the thread the executor
            # is already running; releasing its busy-wait is what lets it end.
            # And the socket is never appended to `self._connections`, so
            # neither `aclose()` nor `reap_dead_connections()` could reach it.
            self._abandon_socket(websocket)
            raise
        except Exception as e:
            # No teardown here: `start()` raises only before it starts the
            # socket thread (already running, or missing user auth -- see
            # `EventSubWebsocket.start`), so there is nothing left behind.
            raise TransportError(f"could not open an EventSub connection: {e}") from e

        connection = _Connection(connection_id=self._next_connection_id, websocket=websocket)
        self._next_connection_id += 1
        self._connections.append(connection)
        # A session no snapshot has walked. Any preflight reading taken before
        # this one describes a pool that no longer exists.
        self._invalidate_adoption_snapshot("connection opened")
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

    def _abandon_socket(self, websocket) -> None:
        """Reclaim a socket that never joined the pool.

        `_grow` can leave a half-open session behind two ways -- the connect
        timing out, or the whole reconcile being cancelled at shutdown -- and
        both need the same three steps.

        `start()` busy-waits on `_startup_complete`, which only
        `_handle_welcome` ever sets. If the socket thread died on its way up --
        `_connect` gives up after a 255 s retry ladder and raises -- that flag
        is never set and `start()` spins for the life of the process, holding
        an executor worker. Setting it releases the busy-wait so the thread
        ends instead of spinning.

        That alone is not enough. `_keep_loop_alive()` runs on the socket's OWN
        loop and spins on `while not self._closing`, and only `_stop()` ever
        sets `_closing`. Without the teardown every abandoned connect left a
        thread spinning at 10 Hz for the life of the process, holding an open
        `ClientSession` and its file descriptors -- and invisibly, because the
        connection is never appended to `self._connections`, so neither
        `reap_dead_connections()` nor `aclose()` could see it.

        Nor is THAT enough on its own. Before `_keep_loop_alive` runs at all,
        `_run_socket` sits in `run_until_complete(self._connect(is_startup=
        True))`, and `_connect` never looks at `_closing`: it retries the
        connect through `reconnect_delay_steps`, catching every failure --
        including the AttributeError from the session this teardown has just
        set to None -- and sleeping between them. That ladder is
        `[0, 1, 2, 4, 8, 16, 32, 64, 128]`, so a socket abandoned during a
        FAILING connect kept a non-daemon thread alive for up to 255 s, and
        `threading._shutdown` joins it at interpreter exit: SIGTERM would sit
        there rather than exiting. Emptying the ladder ends that loop at its
        next condition check -- `retry >= len(...)` is then true, so `_connect`
        raises and `_run_socket` unwinds -- which bounds the thread by whatever
        sleep is already in progress instead of by the whole ladder.

        `_tear_down_socket` does the emptying, at the END of the teardown, for
        the reason spelled out there: the moment the list is empty `_connect`
        can stop the socket loop, and the teardown runs on that loop.
        """
        websocket._startup_complete = True
        websocket._running = False
        self._tear_down_socket(websocket, stop_retrying=True)

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

    async def _on_notification(self, event):
        """Hand one chat notification to the service. Same loop, other sink.

        Separate from `_on_event` on purpose: a notification handed to the
        chat publisher would be mapped as a chat message and land on
        `chat-messages`, which the Flink job reads as real chat.
        """
        try:
            await self.notification_handler(event)
        except Exception as e:
            logger.error(
                "Chat notification handler failed",
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
        # subscription object, condition AND type included, so the channel and
        # the coverage type are both right here. Both are needed: a reconnect
        # rotates the ids of both halves, so the channel alone would not say
        # WHICH half was revoked.
        condition = subscription.get("condition") or {}
        broadcaster_id = condition.get("broadcaster_user_id")
        self._loop.call_soon_threadsafe(
            self._forget_revoked,
            subscription_id,
            subscription.get("status"),
            broadcaster_id,
            subscription.get("type"),
        )

    def _forget_revoked(
        self,
        subscription_id: str,
        status: Optional[str],
        broadcaster_id: Optional[str] = None,
        subscription_type: Optional[str] = None,
    ):
        # Twitch has withdrawn this subscription, so any preflight row naming
        # it is a stale positive nothing else would clear.
        self._invalidate_adoption_snapshot("subscription revoked")
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
            slot = self._slot_for(broadcaster_id, subscription_type)
        if slot is None:
            logger.error(
                "Subscription revoked by Twitch, but the channel and coverage type "
                "could not be identified -- invalidating so the next pass "
                "re-enumerates",
                extra={
                    "subscription_id": subscription_id,
                    "status": status,
                    "subscription_type": subscription_type,
                },
            )
            # The channel is unknown, so the only safe move is to make the
            # reconciler rebuild its view from Twitch.
            if self.on_subscriptions_lost is not None:
                self.on_subscriptions_lost(1)
            return
        # Exactly one slot, never the pair. Twitch revokes one subscription,
        # so the sibling is still live and re-creating it would duplicate it.
        self._slots.pop((slot.broadcaster_id, slot.coverage_type), None)
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
                "coverage_type": slot.coverage_type.value,
                "subscription_id": subscription_id,
                "status": status,
            },
        )
        # One subscription, not one channel: the reconciler's count is in
        # subscriptions, and the channel usually still holds its sibling.
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
            # deletes can bring the connection back into routing -- and
            # remember the SESSION it refused on, so a reconnect clears it
            # rather than stranding slots on a session that never refused
            # anything (decision 28, `_reevaluate_full_at`).
            connection.full_at = connection.occupancy
            connection.full_at_session = self._session_id(connection)
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
        if any(marker in lowered for marker in _TRANSIENT_SESSION_MARKERS):
            # The session rotated under this create. Not a failure -- the next
            # pass recreates the channel on whatever session is live then.
            logger.warning(
                "EventSub session rotated mid-create, will retry next pass",
                extra={
                    "connection": connection.connection_id,
                    "occupancy": connection.occupancy,
                    "error": message,
                },
            )
            return TransientSessionError(message)

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

    async def _adopt_conflict(
        self, broadcaster_id: int, coverage_type: CoverageType
    ) -> str:
        """Answer a 409 with the id of the subscription of THIS type.

        Type-matched throughout. A chat conflict may only ever adopt a chat
        subscription and a notification conflict only a notification one: a
        cross-type adoption would record one half of the pair under the
        other's key, so the channel would read as complete while one of its
        two subscriptions had never been made and the other was indexed twice.
        """
        slot = self._slots.get((broadcaster_id, coverage_type))
        if slot is not None:
            return slot.subscription_id

        live_sessions = self._live_session_ids()
        result = await self.twitch.get_eventsub_subscriptions(
            sub_type=coverage_type.subscription_type, target_token=AuthType.USER
        )
        async for subscription in result:
            condition = getattr(subscription, "condition", None) or {}
            if condition.get("broadcaster_user_id") != str(broadcaster_id):
                continue
            # The `sub_type` filter is Twitch's; this is ours, because a
            # transport that ignored it would silently accept whatever the
            # filter let through.
            row_type = getattr(subscription, "type", None)
            if row_type is not None and row_type != coverage_type.subscription_type:
                continue
            if getattr(subscription, "status", None) not in ADOPTABLE_STATUSES:
                # Revoked or disconnected. Adopting it would count a
                # subscription that delivers nothing.
                continue
            transport = getattr(subscription, "transport", None) or {}
            session_id = transport.get("session_id")
            if session_id not in live_sessions:
                continue
            connection = self._connection_by_session(session_id)
            if connection is None:
                continue
            slot = _Slot(
                broadcaster_id=broadcaster_id,
                coverage_type=coverage_type,
                connection_id=connection.connection_id,
                subscription_id=subscription.id,
                session_id=self._session_id(connection),
            )
            connection.subscription_ids.add(subscription.id)
            self._record_slot(slot)
            logger.info(
                "Adopted a conflicting subscription",
                extra={
                    "broadcaster_id": broadcaster_id,
                    "coverage_type": coverage_type.value,
                    "subscription_id": subscription.id,
                    "connection": connection.connection_id,
                },
            )
            return subscription.id

        # It exists somewhere this pool cannot receive from. Do not claim it:
        # the channel stays out of the actual set and the next pass retries.
        raise TransportError(
            f"conflict for broadcaster {broadcaster_id} ({coverage_type.value}), "
            "but no matching subscription on a session this pool holds"
        )

    # -- deletes ----------------------------------------------------------

    async def _delete_one(self, subscription_id: str) -> None:
        # Every DELETE this module issues passes through here, which makes it
        # the one place a preflight row can become a stale POSITIVE: still in
        # the snapshot, no longer on Twitch. Invalidating here rather than at
        # each caller means no delete path can forget to.
        self._invalidate_adoption_snapshot("subscription deleted")
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
    def _tear_down_socket(websocket, *, stop_retrying: bool = False) -> None:
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

        `stop_retrying` empties `reconnect_delay_steps`, which is what ends a
        socket still inside `_connect`'s retry ladder (see `_abandon_socket`).
        It happens HERE, at the end of the teardown, and not before it: the
        moment that list is empty `_connect` can unwind `run_until_complete`
        and stop the socket loop, and this coroutine is scheduled ON that loop.
        Emptying it first therefore raced the very cleanup it was paired with
        -- the loop stopped with `teardown` still pending, so `_session` was
        never closed and the aiohttp connector leaked, with a "coroutine was
        never awaited" warning as the only trace. Ordering it last makes the
        unwind harmless, because by then there is nothing left to close.
        """
        try:
            socket_loop = getattr(websocket, "_socket_loop", None)
            if socket_loop is not None and socket_loop.is_running():
                async def teardown():
                    # Not `_stop()` alone. Its first statement is
                    # `await self._connection.close()`, and after a failed
                    # `ws_connect` that attribute is None -- so it raises
                    # straight away and never reaches
                    # `await self._session.close()`. That is precisely the
                    # timeout path this teardown exists for, so relying on
                    # `_stop()` leaked the aiohttp ClientSession, its connector
                    # sockets and the event loop every time, invisibly: the
                    # connection is never appended to `_connections`, so the
                    # supervisor cannot see it either. Close each piece on its
                    # own, so one failure cannot skip the next.
                    for closer in ("_connection", "_session"):
                        target = getattr(websocket, closer, None)
                        if target is None:
                            continue
                        try:
                            await target.close()
                        except Exception:
                            pass
                    try:
                        websocket._connection = None
                        websocket._session = None
                    except Exception:
                        pass
                    websocket._closing = True
                    if stop_retrying:
                        try:
                            websocket.reconnect_delay_steps = []
                        except Exception:  # pragma: no cover -- odd double
                            pass

                asyncio.run_coroutine_threadsafe(teardown(), socket_loop)
            else:
                websocket._closing = True
                if stop_retrying:
                    try:
                        websocket.reconnect_delay_steps = []
                    except Exception:  # pragma: no cover -- odd double
                        pass
        except Exception as e:  # pragma: no cover -- a test double without them
            logger.debug("Could not tear down a socket", extra={"error": str(e)})

    def _slot_for(
        self, broadcaster_id, subscription_type
    ) -> Optional[_Slot]:
        """The slot this pool holds for one (channel, type), whatever id it recorded.

        Used when a revocation names an id the pool has never seen, which is
        what a reconnect leaves behind: it rotates every id on the socket while
        the pool keeps the ones it recorded at create time. Both the
        broadcaster and the type come from the revocation payload rather than
        any local registry -- the library has already emptied those by the time
        it calls us -- and BOTH are required. Resolving on the channel alone
        would forget whichever half happened to be looked up first, which is
        the sibling half the time.
        """
        if broadcaster_id is None:
            return None
        coverage_type = CoverageType.for_subscription_type(subscription_type)
        if coverage_type is None:
            return None
        try:
            return self._slots.get((int(broadcaster_id), coverage_type))
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _session_id(connection: _Connection) -> Optional[str]:
        session = getattr(connection.websocket, "active_session", None)
        return getattr(session, "id", None) if session is not None else None

    def _session_changed(self, slot: _Slot, connection: _Connection) -> bool:
        """Has this connection reconnected since the slot was stamped?

        A reconnect always means a new session, and everything Twitch held on
        the old one is gone with it -- whatever the library's registry still
        claims. Each half of a pair is judged against its own stamp, because
        the two can be made either side of one reconnect.
        """
        current_session = self._session_id(connection)
        return (
            slot.session_id is not None
            and current_session is not None
            and slot.session_id != current_session
        )

    def _slot_is_current(self, slot: _Slot, connection: _Connection) -> bool:
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

        The session check catches it, and it holds for a partial failure too.
        """
        if self._session_changed(slot, connection):
            return False
        return self._connection_holds(connection, slot.broadcaster_id, slot.coverage_type)

    def _connection_holds(
        self,
        connection: _Connection,
        broadcaster_id: int,
        coverage_type: CoverageType,
    ) -> bool:
        """Does the library still have a subscription of THIS type here?

        The type is load-bearing. Matching on `broadcaster_user_id` alone
        makes a channel that holds only its chat subscription read as
        "current" for the notification type as well, so the missing half is
        never created and the channel is silently stuck in `chat_only` while
        every count says it is covered.

        The registry is private, and after a reconnect it can lie (see
        `_slot_is_current`), so this is the second of two checks rather than
        the only one. A test double without one is taken at its word.
        """
        active = getattr(connection.websocket, "_active_subscriptions", None)
        if not isinstance(active, dict):
            return True
        wanted = str(broadcaster_id)
        return any(
            subscription.get("sub_type") == coverage_type.subscription_type
            and (subscription.get("condition") or {}).get("broadcaster_user_id") == wanted
            for subscription in active.values()
        )

    def _forget_slot(self, slot: _Slot):
        """Drop one slot -- one coverage type -- from every index."""
        self._slots.pop((slot.broadcaster_id, slot.coverage_type), None)
        self._by_subscription.pop(slot.subscription_id, None)
        connection = self._connection_by_id(slot.connection_id)
        if connection is not None:
            connection.subscription_ids.discard(slot.subscription_id)

    def _resolve_unrecognised(
        self, subscription_id: str
    ) -> Optional[Tuple[_Connection, Optional[int], Optional[CoverageType]]]:
        """Who an id the pool's own indexes have never seen belongs to.

        Finds the channel AND the coverage type behind the id in the library's
        own registry -- the only place a rotated id appears. Both come back
        from the ONE lookup, which is what lets `delete()` finish the whole
        channel in the call it was given the id in; resolving the identity and
        then re-deriving it a second time to act on it is how the sibling half
        got left behind.

        `None` means no connection here has ever heard of the id. A connection
        with an unusable identity behind the id comes back with the connection
        and `None`s, so the caller can still clear the registry entry rather
        than leave the socket to resubscribe it.
        """
        for connection in list(self._connections):
            active = getattr(connection.websocket, "_active_subscriptions", None)
            if not isinstance(active, dict) or subscription_id not in active:
                continue
            entry = active[subscription_id]
            if not isinstance(entry, dict):
                return connection, None, None
            condition = entry.get("condition") or {}
            coverage_type = CoverageType.for_subscription_type(entry.get("sub_type"))
            try:
                broadcaster_id = int(condition.get("broadcaster_user_id"))
            except (TypeError, ValueError):
                broadcaster_id = None
            return connection, broadcaster_id, coverage_type
        return None

    def _live_subscription_ids(
        self, connection: Optional[_Connection], slot: _Slot
    ) -> List[str]:
        """The ids Twitch currently holds for this channel AND type here.

        The library re-subscribes everything after a reconnect and gets fresh
        ids, so the id recorded at create time can be stale. Its own
        `_active_subscriptions` map is the only record of the current one; it
        is private, but the alternative is deleting an id Twitch has already
        collected and leaving the live subscription delivering into nothing.

        Matched on the type as well as the channel: a rotated pair has one new
        id per coverage type, and resolving by broadcaster alone would return
        both -- deleting one of them twice and leaking the other.
        """
        if connection is None:
            return [slot.subscription_id]
        active = getattr(connection.websocket, "_active_subscriptions", None)
        if not isinstance(active, dict):
            return [slot.subscription_id]
        matches = [
            subscription_id
            for subscription_id, subscription in active.items()
            if subscription.get("sub_type") == slot.coverage_type.subscription_type
            and (subscription.get("condition") or {}).get("broadcaster_user_id")
            == str(slot.broadcaster_id)
        ]
        if matches and slot.subscription_id not in matches:
            logger.debug(
                "Subscription id rotated by a reconnect, deleting the live one",
                extra={
                    "broadcaster_id": slot.broadcaster_id,
                    "coverage_type": slot.coverage_type.value,
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
        # Every preflight row on this connection's session is now unadoptable.
        self._invalidate_adoption_snapshot("connection retired")

        self._connections = [
            live for live in self._connections if live.connection_id != connection.connection_id
        ]
        # Every slot on this connection goes, of either type -- and NOTHING
        # else does. A pair split across two connections keeps the half that
        # lives elsewhere, which leaves the channel in a partial state the
        # ordinary repair path already converges (R2).
        affected: Set[int] = set()
        for subscription_id in list(connection.subscription_ids):
            slot = self._by_subscription.pop(subscription_id, None)
            if slot is not None:
                self._slots.pop((slot.broadcaster_id, slot.coverage_type), None)
                affected.add(slot.broadcaster_id)
        # A slot can outlive its id if a reconnect rotated it; clear anything
        # still pointing at this connection.
        for key, slot in list(self._slots.items()):
            if slot.connection_id == connection.connection_id:
                self._slots.pop(key, None)
                self._by_subscription.pop(slot.subscription_id, None)
                affected.add(slot.broadcaster_id)
        connection.subscription_ids.clear()
        for broadcaster_id in affected:
            # Retirement is a free opportunity to retest a refusal that may
            # have been specific to the session that has just gone (I17), so
            # the channel becomes repairable at once rather than at the
            # hold-off's own deadline.
            self._clear_auxiliary_refusal(broadcaster_id, "connection retired")

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
