#!/usr/bin/env python3
"""
Unit tests for Clip Detector Job

Tests the anomaly detection logic, command filtering, and clip creation flow.
"""

import json
import logging
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# The module itself, alongside the names below: Feature 007's operator surface
# (AnomalyDetector.process_element2, suppression_key, the suppression source
# builders, the new metric globals) is written before T041-T045 implement it,
# and a missing name must fail its own test rather than break collection for
# the clip-detector tests this file already carries.
import clip_detector_job
import spike_detector
from clip_detector_job import (
    AnomalyEvent,
    ChatMessage,
    ClipResult,
    TwitchAPIClient,
    TwitchAPIError,
)
from spike_detector import COMMAND_PATTERN


class TestCommandFilter:
    """Tests for command message filtering."""

    def test_command_pattern_matches_exclamation_commands(self):
        """Commands starting with ! should be matched."""
        assert COMMAND_PATTERN.match("!help")
        assert COMMAND_PATTERN.match("!roll")
        assert COMMAND_PATTERN.match("!bet100")
        assert COMMAND_PATTERN.match("!CAPS")

    def test_command_pattern_does_not_match_regular_messages(self):
        """Regular messages should not be matched."""
        assert not COMMAND_PATTERN.match("hello world")
        assert not COMMAND_PATTERN.match("this is great!")
        assert not COMMAND_PATTERN.match("? what")
        assert not COMMAND_PATTERN.match("")

    def test_command_pattern_does_not_match_special_chars_after_exclamation(self):
        """Only alphanumeric after ! should match."""
        assert not COMMAND_PATTERN.match("! space")
        assert not COMMAND_PATTERN.match("!@symbol")
        assert not COMMAND_PATTERN.match("!#hashtag")


class TestTwitchAPIClient:
    """Tests for Twitch API client."""

    @pytest.fixture
    def token_file(self, tmp_path):
        """A real token file on disk, so the client can construct without a network call."""
        path = tmp_path / "tokens.json"
        path.write_text(json.dumps({
            "access_token": "test_token",
            "refresh_token": "test_refresh",
            "scopes": ["clips:edit"],
        }))
        return str(path)

    @patch("clip_detector_job.requests.post")
    def test_create_clip_returns_clip_id_on_success(self, mock_post, token_file):
        """Successful clip creation should return clip ID."""
        mock_post.return_value.status_code = 202
        mock_post.return_value.json.return_value = {"data": [{"id": "test_clip_123"}]}

        client = TwitchAPIClient("client_id", "client_secret", token_file, validate_on_init=False)
        # Pre-set token to avoid auth call
        client.access_token = "test_token"

        clip_id = client.create_clip(12345)
        assert clip_id == "test_clip_123"

    @patch("clip_detector_job.requests.post")
    def test_create_clip_raises_retryable_on_server_error(self, mock_post, token_file):
        """Failed clip creation should raise TwitchAPIError flagged as retryable for a 5xx."""
        mock_post.return_value.status_code = 500
        mock_post.return_value.json.return_value = {}

        client = TwitchAPIClient("client_id", "client_secret", token_file, validate_on_init=False)
        client.access_token = "test_token"

        with pytest.raises(TwitchAPIError) as exc_info:
            client.create_clip(12345)
        assert exc_info.value.status_code == 500
        assert exc_info.value.is_retryable is True

    @patch("clip_detector_job.requests.get")
    def test_get_clip_returns_data_on_success(self, mock_get, token_file):
        """Successful clip retrieval should return clip data."""
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {
            "data": [
                {
                    "id": "test_clip_123",
                    "embed_url": "https://clips.twitch.tv/embed?clip=test_clip_123",
                    "thumbnail_url": "https://clips.twitch.tv/thumb.jpg",
                }
            ]
        }

        client = TwitchAPIClient("client_id", "client_secret", token_file, validate_on_init=False)
        client.access_token = "test_token"

        clip_data = client.get_clip("test_clip_123")
        assert clip_data is not None
        assert clip_data["id"] == "test_clip_123"
        assert "embed_url" in clip_data
        assert "thumbnail_url" in clip_data

    @patch("clip_detector_job.requests.get")
    def test_get_clip_returns_none_on_failure(self, mock_get, token_file):
        """Failed clip retrieval should return None."""
        mock_get.return_value.status_code = 404
        mock_get.return_value.json.return_value = {"data": []}

        client = TwitchAPIClient("client_id", "client_secret", token_file, validate_on_init=False)
        client.access_token = "test_token"

        clip_data = client.get_clip("nonexistent_clip")
        assert clip_data is None

    @patch("token_manager.requests.post")
    def test_token_refresh_on_expiry(self, mock_post, token_file):
        """Token should be refreshed when expired."""
        mock_post.return_value.status_code = 200
        mock_post.return_value.json.return_value = {
            "access_token": "new_token",
            "expires_in": 3600,
        }

        client = TwitchAPIClient("client_id", "client_secret", token_file, validate_on_init=False)
        client.access_token = None  # Force token refresh

        client._refresh()

        assert client.access_token == "new_token"

    @patch("token_manager.requests.post")
    def test_refresh_uses_the_on_disk_refresh_token_not_the_stale_in_memory_one(
        self, mock_post, token_file
    ):
        """refresh() reads the refresh token from disk inside its lock, so a
        client whose in-memory copy lags a peer's rotation still refreshes
        against the live token rather than a spent one."""
        Path(token_file).write_text(json.dumps({
            "access_token": "a_from_peer",
            "refresh_token": "r_from_peer",   # peer already rotated to this
            "scopes": ["clips:edit"],
        }))
        mock_post.return_value.status_code = 200
        mock_post.return_value.json.return_value = {
            "access_token": "a_new", "refresh_token": "r_new", "expires_in": 3600,
        }

        client = TwitchAPIClient("client_id", "client_secret", token_file, validate_on_init=False)
        client.access_token = "a_stale"
        client.refresh_token = "r_stale"

        client._refresh()

        sent = mock_post.call_args.kwargs["data"]["refresh_token"]
        assert sent == "r_from_peer"
        assert client.access_token == "a_new"

    @patch("clip_detector_job.requests.post")
    def test_create_clip_succeeds_when_refreshed_token_cannot_be_persisted(
        self, mock_post, token_file
    ):
        """A refresh whose write to secrets/ fails (dir not group-writable,
        disk full) must not turn into a hard clip failure -- the in-memory
        token still works. Patching clip_detector_job.requests.post also
        covers token_manager, which imports the same requests module."""
        if os.geteuid() == 0:
            pytest.skip("root ignores directory mode bits; can't simulate EACCES")
        # The lock sidecar persists in a real deployment; only the atomic
        # write of a fresh temp file is what fails when secrets/ goes
        # read-only. Pre-create the lock, then drop dir write permission.
        token_dir = Path(token_file).parent
        (token_dir / (Path(token_file).name + ".lock")).touch(mode=0o666)
        original_mode = token_dir.stat().st_mode

        clip_calls = []

        def route(url, *args, **kwargs):
            if "oauth2/token" in url:
                return MagicMock(status_code=200, json=MagicMock(return_value={
                    "access_token": "refreshed_token", "expires_in": 3600,
                }))
            clip_calls.append(url)
            if len(clip_calls) == 1:
                return MagicMock(status_code=401, text="Invalid OAuth token")
            return MagicMock(status_code=202, json=MagicMock(
                return_value={"data": [{"id": "clip_after_refresh"}]}))

        mock_post.side_effect = route
        token_dir.chmod(0o500)
        try:
            client = TwitchAPIClient("client_id", "client_secret", token_file, validate_on_init=False)
            # Our token matches what's on disk, so _refresh() does a real
            # (mocked) network refresh -- whose write-back is what fails.
            client.access_token = "test_token"

            assert client.create_clip(12345) == "clip_after_refresh"
            assert client.access_token == "refreshed_token"
        finally:
            token_dir.chmod(original_mode)


class TestDataClasses:
    """Tests for data class structures."""

    def test_chat_message_dataclass(self):
        """ChatMessage should store all required fields."""
        msg = ChatMessage(
            broadcaster_id=12345,
            timestamp=1704067200000,
            message_id="uuid-123",
            text="Hello world",
            user_id=67890,
            user_name="viewer",
        )

        assert msg.broadcaster_id == 12345
        assert msg.timestamp == 1704067200000
        assert msg.message_id == "uuid-123"
        assert msg.text == "Hello world"
        assert msg.user_id == 67890
        assert msg.user_name == "viewer"

    def test_anomaly_event_dataclass(self):
        """AnomalyEvent should store detection details."""
        event = AnomalyEvent(
            broadcaster_id=12345,
            detected_at=1704067200000,
            message_count=50,
            baseline_mean=10.5,
            baseline_std=2.3,
        )

        assert event.broadcaster_id == 12345
        assert event.detected_at == 1704067200000
        assert event.message_count == 50
        assert event.baseline_mean == 10.5
        assert event.baseline_std == 2.3

    def test_clip_result_dataclass(self):
        """ClipResult should store clip creation result."""
        result = ClipResult(
            broadcaster_id=12345,
            clip_id="clip_abc",
            embed_url="https://clips.twitch.tv/embed?clip=clip_abc",
            thumbnail_url="https://clips.twitch.tv/thumb.jpg",
            detected_at=1704067200000,
            success=True,
        )

        assert result.broadcaster_id == 12345
        assert result.clip_id == "clip_abc"
        assert result.embed_url == "https://clips.twitch.tv/embed?clip=clip_abc"
        assert result.thumbnail_url == "https://clips.twitch.tv/thumb.jpg"
        assert result.detected_at == 1704067200000
        assert result.success is True


class TestMessageParsing:
    """Tests for message JSON parsing."""

    def test_parse_valid_chat_message(self):
        """Valid JSON chat message should parse correctly."""
        message_json = json.dumps(
            {
                "broadcaster_id": 12345,
                "timestamp": 1704067200000,
                "message_id": "uuid-123",
                "text": "PogChamp",
                "user_id": 67890,
                "user_name": "viewer",
                "metadata": {"emotes": {}, "badges": {}, "is_subscriber": True, "is_mod": False},
            }
        )

        parsed = json.loads(message_json)
        assert parsed["broadcaster_id"] == 12345
        assert parsed["text"] == "PogChamp"

    def test_command_filtering_in_message_flow(self):
        """Command messages should be filtered from processing."""
        messages = [
            {"text": "!bet 100", "broadcaster_id": 1},
            {"text": "LUL that was funny", "broadcaster_id": 1},
            {"text": "!help", "broadcaster_id": 1},
            {"text": "POGGERS", "broadcaster_id": 1},
        ]

        filtered = [m for m in messages if not COMMAND_PATTERN.match(m["text"])]

        assert len(filtered) == 2
        assert filtered[0]["text"] == "LUL that was funny"
        assert filtered[1]["text"] == "POGGERS"


# ---------------------------------------------------------------------------
# The clipping self-heal (spec 004 T025a / FR-013), against a real Postgres
# ---------------------------------------------------------------------------
#
# `allows_clipping = FALSE` used to be permanent. It now carries
# `clipping_disabled_at`, which stream-monitoring uses to let a broadcaster
# back into the ranking after seven days. The two must be written together or
# they disagree, and that is a property of the SQL rather than of the Python
# around it -- so this runs against a real database, in a schema it creates and
# drops, and skips when there is none.

import os

TEST_SCHEMA = "spec004_clipping_test"
# Deliberately localhost, not the deployed host. The fixture runs DDL --
# CREATE SCHEMA and DROP SCHEMA CASCADE -- and defaulting that at the live
# database would put every `pytest` run on production, and put its credential
# in this file. `docker compose --profile local-db up postgres` gives a local
# one; set TEST_POSTGRES_URL to point somewhere else on purpose.
TEST_POSTGRES_URL = os.getenv(
    "TEST_POSTGRES_URL", "postgresql://twitch:twitch_password@localhost:5432/twitch"
)


@pytest.fixture
def clipping_client():
    psycopg2 = pytest.importorskip("psycopg2")
    from clip_detector_job import PostgresClient

    try:
        conn = psycopg2.connect(TEST_POSTGRES_URL, connect_timeout=3)
    except Exception as e:  # pragma: no cover -- environment, not logic
        pytest.skip(f"no Postgres available for the clipping self-heal check: {e}")

    with conn.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {TEST_SCHEMA} CASCADE")
        cur.execute(f"CREATE SCHEMA {TEST_SCHEMA}")
        cur.execute(f"SET search_path TO {TEST_SCHEMA}")
        cur.execute(
            """
            CREATE TABLE streamers (
                streamer_id BIGINT PRIMARY KEY,
                streamer_login VARCHAR(255) NOT NULL,
                allows_clipping BOOLEAN DEFAULT TRUE,
                clipping_disabled_at TIMESTAMPTZ
            )
            """
        )
        # Never let this touch the deployed table.
        cur.execute(
            "SELECT n.nspname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE c.oid = to_regclass('streamers')"
        )
        resolved = cur.fetchone()
        assert resolved and resolved[0] == TEST_SCHEMA, (
            f"'streamers' resolves to {resolved} rather than the test schema; refusing to run"
        )
        cur.execute(
            "INSERT INTO streamers (streamer_id, streamer_login) VALUES (1, 'a_streamer')"
        )
    conn.commit()

    client = PostgresClient("unused", "0", "unused", "unused", "unused")
    client._conn = conn
    try:
        yield client
    finally:
        with conn.cursor() as cur:
            cur.execute(f"DROP SCHEMA IF EXISTS {TEST_SCHEMA} CASCADE")
        conn.commit()
        conn.close()


def read_streamer(client, streamer_id=1):
    with client._conn.cursor() as cur:
        cur.execute(
            "SELECT allows_clipping, clipping_disabled_at FROM streamers WHERE streamer_id = %s",
            (streamer_id,),
        )
        return cur.fetchone()


class TestClippingSelfHeal:
    """T025a -- the boolean and its timestamp are written together."""

    def test_disabling_stamps_the_time(self, clipping_client):
        clipping_client.mark_clipping_disabled(1)
        allows, disabled_at = read_streamer(clipping_client)
        assert allows is False
        assert disabled_at is not None

    def test_a_successful_clip_clears_both(self, clipping_client):
        clipping_client.mark_clipping_disabled(1)
        clipping_client.mark_clipping_allowed(1)
        assert read_streamer(clipping_client) == (True, None)

    def test_a_fresh_refusal_resets_the_timestamp(self, clipping_client):
        """The 7-day retry that refuses again restarts the seven days."""
        with clipping_client._conn.cursor() as cur:
            cur.execute(
                "UPDATE streamers SET allows_clipping = FALSE, "
                "clipping_disabled_at = NOW() - make_interval(days => 30) WHERE streamer_id = 1"
            )
        clipping_client._conn.commit()
        _, stale = read_streamer(clipping_client)

        clipping_client.mark_clipping_disabled(1)

        _, refreshed = read_streamer(clipping_client)
        assert refreshed > stale

    def test_healing_leaves_an_already_allowed_streamer_alone(self, clipping_client):
        """The common case writes nothing, so a clip does not cost an UPDATE
        of a row that is already correct."""
        clipping_client.mark_clipping_allowed(1)
        assert read_streamer(clipping_client) == (True, None)


# ===========================================================================
# Feature 007 -- suppress gift and raid chat bursts. The operator half.
# ===========================================================================
#
# Tasks T002, T037-T040. Everything below runs on fakes: no broker, no
# MiniCluster, no gateway, no Twitch call, and main() is never executed. It is
# still conditional on the pinned apache-flink==1.18.0 being installed, because
# this file imports clip_detector_job, which imports PyFlink. That is why the
# guaranteed-offline half of the same evidence -- SuppressionSourceSettings,
# the pure decoder, the deadline arithmetic and the docker-compose assertions
# -- lives in test_spike_detector.py, which imports no PyFlink (research D16,
# plan "Offline testability"). When PyFlink is absent this file does not
# collect, and T047 reports it as pending rather than passed.
#
# References: specs/007-suppress-gift-raid-bursts/contracts/
# suppression-events.schema.md §4, data-model.md §3, research D4/D5/D6/D13/D15.

GIFT = "community_sub_gift"
SUB_GIFT = "sub_gift"
RAID = "raid"
EXCLUDED_NOTICE_TYPES = ("unraid", "sub", "resub", "announcement")

# Leave a field out of the payload entirely, which is a different failure from
# carrying it as null.
OMIT = object()

BROADCASTER = 123456789
OCCURRED_AT_MS = 1_772_668_800_123


# ---------------------------------------------------------------------------
# T002 -- record builders
# ---------------------------------------------------------------------------

def valid_suppression_record(
    schema_version=1,
    broadcaster_id=BROADCASTER,
    notice_type=GIFT,
    occurred_at_ms=OCCURRED_AT_MS,
    **optional,
):
    """One version-1 `suppression-events` value, as the topic carries it.

    Optional keywords (`notice_id`, `received_at_ms`, `viewer_count`) are added
    verbatim; pass OMIT for any field to drop it. The producer's own view of
    the same contract is built in test_stream_monitoring.py -- this builder is
    the consumer's, and it deliberately depends on nothing but the contract.
    """
    payload = {
        "schema_version": schema_version,
        "broadcaster_id": broadcaster_id,
        "notice_type": notice_type,
        "occurred_at_ms": occurred_at_ms,
    }
    payload.update(optional)
    return json.dumps({k: v for k, v in payload.items() if v is not OMIT})


# The invalid records, one per contract §4.1 rejection reason, so a test can
# name the reason it expects rather than re-deriving it.
INVALID_SUPPRESSION_RECORDS = {
    "not-json": ("{not json", "decode"),
    "empty": ("", "decode"),
    "json-array": ("[]", "decode"),
    "json-scalar": ("7", "decode"),
    "json-null": ("null", "decode"),
    "schema-missing": (valid_suppression_record(schema_version=OMIT), "schema_version"),
    "schema-null": (valid_suppression_record(schema_version=None), "schema_version"),
    "schema-future": (valid_suppression_record(schema_version=2), "schema_version"),
    "schema-string": (valid_suppression_record(schema_version="1"), "schema_version"),
    # bool is a subclass of int and True == 1, so a naive version check accepts
    # this record. It must not.
    "schema-bool": (valid_suppression_record(schema_version=True), "schema_version"),
    "id-missing": (valid_suppression_record(broadcaster_id=OMIT), "fields"),
    "id-string": (valid_suppression_record(broadcaster_id="123456789"), "fields"),
    "id-float": (valid_suppression_record(broadcaster_id=123456789.0), "fields"),
    "id-bool": (valid_suppression_record(broadcaster_id=True), "fields"),
    "id-null": (valid_suppression_record(broadcaster_id=None), "fields"),
    "time-missing": (valid_suppression_record(occurred_at_ms=OMIT), "fields"),
    "time-string": (valid_suppression_record(occurred_at_ms="1772668800123"), "fields"),
    "time-float": (valid_suppression_record(occurred_at_ms=1.772e12), "fields"),
    "time-bool": (valid_suppression_record(occurred_at_ms=True), "fields"),
    "time-null": (valid_suppression_record(occurred_at_ms=None), "fields"),
    "type-missing": (valid_suppression_record(notice_type=OMIT), "fields"),
    "type-null": (valid_suppression_record(notice_type=None), "fields"),
    "type-int": (valid_suppression_record(notice_type=7), "fields"),
    "type-excluded-unraid": (valid_suppression_record(notice_type="unraid"), "fields"),
    "type-excluded-sub": (valid_suppression_record(notice_type="sub"), "fields"),
    "type-excluded-resub": (valid_suppression_record(notice_type="resub"), "fields"),
    "type-unknown": (valid_suppression_record(notice_type="mystery_gift_2"), "fields"),
}


def chat_record(broadcaster_id=BROADCASTER, sent_at=OCCURRED_AT_MS, text="POGGERS"):
    """A `chat-messages` value, whose schema spec 004 FR-008 freezes."""
    return json.dumps({
        "broadcaster_id": broadcaster_id,
        "sent_at": sent_at,
        "timestamp": sent_at + 40,
        "message_id": "uuid-1",
        "text": text,
        "user_id": 42,
        "user_name": "viewer",
    })


# ---------------------------------------------------------------------------
# T002 -- keyed state, timer, context and metric doubles
# ---------------------------------------------------------------------------

class FakeValueState:
    """Flink's ValueState, with the writes visible.

    The write count is load-bearing: contract §4.1 rule 6 requires
    process_element2 to write only when the deadline actually moved, the same
    write-on-change rule `hold` already follows.
    """

    def __init__(self, value=None):
        self._value = value
        self.writes = []
        self.clears = 0

    def value(self):
        return self._value

    def update(self, value):
        self._value = value
        self.writes.append(value)

    def clear(self):
        self._value = None
        self.clears += 1


class FakeMapState:
    def __init__(self, data=None):
        self.data = dict(data or {})
        self.removed = []

    def get(self, key):
        return self.data.get(key)

    def put(self, key, value):
        self.data[key] = value

    def items(self):
        return list(self.data.items())

    def remove(self, key):
        self.data.pop(key, None)
        self.removed.append(key)


class FakeTimerService:
    def __init__(self, watermark=0):
        self.registered = []
        self._watermark = watermark

    def register_event_time_timer(self, timestamp):
        self.registered.append(timestamp)

    def current_watermark(self):
        return self._watermark


class FakeContext:
    """Serves for both KeyedCoProcessFunction.Context and OnTimerContext."""

    def __init__(self, key, timestamp=None, timer_service=None):
        self._key = key
        self._timestamp = timestamp
        self._timer_service = timer_service or FakeTimerService()

    def get_current_key(self):
        return self._key

    def timestamp(self):
        return self._timestamp

    def timer_service(self):
        return self._timer_service


class FakeStateStore:
    """One set of keyed-state doubles per broadcaster.

    Flink swaps the keyed state under the operator per record; bind() does the
    same, which is what makes the channel-isolation assertions (NFR-002, I13)
    mean anything rather than passing by accident on a single shared cell.
    """

    def __init__(self):
        self.states = {}

    def for_key(self, key):
        return self.states.setdefault(key, {
            "counts": FakeMapState(),
            "hold": FakeValueState(),
            "last_fire_second": FakeValueState(),
            "suppression": FakeValueState(),
        })

    def bind(self, detector, key):
        state = self.for_key(key)
        detector.message_counts = state["counts"]
        detector.hold = state["hold"]
        detector.last_fire_second = state["last_fire_second"]
        detector.suppression = state["suppression"]
        return state


class RecordingMetric:
    """A Prometheus-shaped double: .labels(...) -> child, then .inc()/.observe()."""

    def __init__(self, name, labelnames=()):
        self.name = name
        self.labelnames = tuple(labelnames)
        self.increments = []      # label dicts, one per inc()
        self.observations = []    # (label dict, value) per observe()

    def labels(self, *args, **kwargs):
        if args:
            kwargs = dict(zip(self.labelnames, args))
        return _RecordingChild(self, kwargs)

    def inc(self, amount=1):
        self.increments.append({})

    def observe(self, value):
        self.observations.append(({}, value))

    def values_for(self, **labels):
        return [d for d in self.increments if all(d.get(k) == v for k, v in labels.items())]


class _RecordingChild:
    def __init__(self, parent, labels):
        self.parent = parent
        self._labels = labels

    def inc(self, amount=1):
        self.parent.increments.append(dict(self._labels))

    def observe(self, value):
        self.parent.observations.append((dict(self._labels), value))


SUPPRESSION_METRIC_GLOBALS = {
    "_anomalies_detected_total": "anomalies_detected_total",
    "_clips_suppressed_total": "clips_suppressed_total",
    "_suppression_records_rejected_total": "suppression_records_rejected_total",
    "_suppression_records_consumed_total": "suppression_records_consumed_total",
    "_suppression_delivery_age_seconds": "suppression_delivery_age_seconds",
    "_hold_regressed_total": "hold_regressed_total",
}


@pytest.fixture
def metrics(monkeypatch):
    """Recording doubles in place of the module's lazy metric globals.

    _init_metrics is stubbed out as well: it binds an HTTP port, which no unit
    test may do.
    """
    monkeypatch.setattr(clip_detector_job, "_init_metrics", lambda subtask_index=0: None)
    doubles = {}
    for attr, name in SUPPRESSION_METRIC_GLOBALS.items():
        double = RecordingMetric(name)
        doubles[name] = double
        monkeypatch.setattr(clip_detector_job, attr, double, raising=False)
    return doubles


@pytest.fixture
def store():
    return FakeStateStore()


def make_detector(clock_ms=None, config=None, suppression_config=None):
    """A detector with open()'s work done by hand, so no runtime context, no
    metrics server and no environment read are involved."""
    detector = clip_detector_job.AnomalyDetector(clock_ms=clock_ms)
    detector.config = config or spike_detector.DetectorConfig()
    detector.suppression_config = suppression_config or spike_detector.SuppressionConfig()
    detector.subtask_index = 0
    return detector


def feed_suppression(detector, store, record, key=BROADCASTER, timer_service=None):
    """One record through the suppression input, keyed the way the job keys it."""
    store.bind(detector, key)
    ctx = FakeContext(key=key, timer_service=timer_service or FakeTimerService())
    emitted = detector.process_element2((key, record), ctx)
    return list(emitted or []), ctx


def fire_timer(detector, store, timestamp, key=BROADCASTER, watermark=None):
    store.bind(detector, key)
    timer_service = FakeTimerService(timestamp if watermark is None else watermark)
    ctx = FakeContext(key=key, timestamp=timestamp, timer_service=timer_service)
    return list(detector.on_timer(timestamp, ctx)), ctx


def emitting_decision(peak_second, expired_buckets=None, hold=None):
    """A Decision that reports a spike, so on_timer reaches its output gate."""
    spike = spike_detector.Spike(
        message_count=420,
        baseline_mean=10.0,
        baseline_std=2.0,
        intensity=9.5,
        detected_at_seconds=peak_second,
    )
    return spike_detector.Decision(
        emit=spike,
        hold=hold,
        expired_buckets=list(expired_buckets or []),
        measurement=spike,
        observed_seconds=300,
    )


def stub_evaluate(monkeypatch, decision):
    """Pin evaluate()'s answer so the gate, not the arithmetic, is under test."""
    calls = []

    def _evaluate(counts, second, hold, last_fire_second, config):
        calls.append((dict(counts), second, hold, last_fire_second))
        return decision

    monkeypatch.setattr(clip_detector_job, "evaluate", _evaluate)
    return calls


class TestSuppressionDoubles:
    """T002. The doubles are load-bearing, so they get their own checks; a
    silently broken fake would make every assertion below vacuous."""

    def test_the_valid_builder_matches_the_version_1_contract(self):
        payload = json.loads(valid_suppression_record())
        assert payload == {
            "schema_version": 1,
            "broadcaster_id": BROADCASTER,
            "notice_type": GIFT,
            "occurred_at_ms": OCCURRED_AT_MS,
        }

    def test_the_builder_can_drop_a_required_field_and_add_optional_ones(self):
        assert "occurred_at_ms" not in json.loads(
            valid_suppression_record(occurred_at_ms=OMIT)
        )
        payload = json.loads(valid_suppression_record(
            notice_type=RAID, notice_id="9c2b", received_at_ms=1, viewer_count=4200
        ))
        assert payload["notice_id"] == "9c2b"
        assert payload["viewer_count"] == 4200

    @pytest.mark.parametrize(
        "name,expected", [(k, v[1]) for k, v in INVALID_SUPPRESSION_RECORDS.items()]
    )
    def test_every_invalid_builder_decodes_to_its_documented_reason(self, name, expected):
        """The pure decoder is the shared one from spike_detector, so the
        operator and the offline suite cannot drift apart on what "malformed"
        means (contract §4.1)."""
        raw, _ = INVALID_SUPPRESSION_RECORDS[name]
        result = spike_detector.decode_suppression_record(
            raw, spike_detector.SuppressionConfig()
        )
        assert result.notice is None
        assert result.rejected_reason == expected

    def test_the_fake_value_state_reports_writes_and_clears(self):
        state = FakeValueState()
        assert state.value() is None
        state.update("a")
        state.update("b")
        state.clear()
        assert state.writes == ["a", "b"]
        assert state.clears == 1 and state.value() is None

    def test_the_fake_state_store_keeps_channels_apart(self, store):
        detector = object.__new__(clip_detector_job.AnomalyDetector)
        store.bind(detector, 1)
        detector.suppression.update("one")
        store.bind(detector, 2)
        assert detector.suppression.value() is None
        assert store.for_key(1)["suppression"].value() == "one"


class TestOperatorShape:
    """T043. The chat path must survive the conversion untouched: FR-008 and
    SC-004 are about the gate changing output only."""

    def test_the_detector_is_a_keyed_co_process_function(self):
        from pyflink.datastream import KeyedCoProcessFunction

        assert issubclass(clip_detector_job.AnomalyDetector, KeyedCoProcessFunction)

    def test_it_exposes_both_inputs_and_the_timer(self):
        for name in ("process_element1", "process_element2", "on_timer", "open"):
            assert callable(getattr(clip_detector_job.AnomalyDetector, name, None)), name

    def test_process_element1_buckets_and_arms_its_own_timer(self, metrics, store):
        detector = make_detector()
        store.bind(detector, BROADCASTER)
        timers = FakeTimerService()
        ctx = FakeContext(key=BROADCASTER, timestamp=OCCURRED_AT_MS, timer_service=timers)

        detector.process_element1((BROADCASTER, chat_record()), ctx)
        detector.process_element1((BROADCASTER, chat_record(text="LUL")), ctx)

        bucket = OCCURRED_AT_MS // 1000
        assert store.for_key(BROADCASTER)["counts"].data == {bucket: 2}
        # Registering the same timestamp twice is a no-op in Flink, so the
        # operator arms it per message rather than tracking what it armed.
        assert timers.registered == [bucket * 1000, bucket * 1000]

    def test_a_chat_message_never_touches_suppression_state(self, metrics, store):
        detector = make_detector()
        store.bind(detector, BROADCASTER)
        ctx = FakeContext(key=BROADCASTER, timestamp=OCCURRED_AT_MS)
        detector.process_element1((BROADCASTER, chat_record()), ctx)
        assert store.for_key(BROADCASTER)["suppression"].writes == []

    def test_open_registers_the_suppression_state_under_the_same_ttl(self, monkeypatch):
        """T043. The suppression state is JSON in a Types.STRING() ValueState
        and shares the operator's TTL policy (data-model §3)."""
        recorded = []

        class FakeDescriptor:
            def __init__(self, name, *type_args):
                self.name = name
                self.type_args = type_args
                self.ttl = None
                recorded.append(self)

            def enable_time_to_live(self, ttl_config):
                self.ttl = ttl_config

        class FakeRuntimeContext:
            def get_index_of_this_subtask(self):
                return 0

            def get_map_state(self, descriptor):
                return FakeMapState()

            def get_state(self, descriptor):
                return FakeValueState()

        monkeypatch.setattr(clip_detector_job, "_init_metrics", lambda subtask_index=0: None)
        monkeypatch.setattr(clip_detector_job, "ValueStateDescriptor", FakeDescriptor)
        monkeypatch.setattr(clip_detector_job, "MapStateDescriptor", FakeDescriptor)

        detector = clip_detector_job.AnomalyDetector()
        detector.open(FakeRuntimeContext())

        by_name = {d.name: d for d in recorded}
        assert "suppression" in by_name
        assert by_name["suppression"].ttl is not None
        # One TTL policy for the whole operator: a suppression deadline that
        # outlived the buckets it gates would be a stale window (§5.3).
        assert len({id(d.ttl) for d in recorded}) == 1
        assert detector.suppression_config is not None

    def test_the_ttl_never_returns_expired_state(self, monkeypatch):
        """§5.3 / FR-011: an expired deadline must read back as absent, so a
        previous window cannot suppress new activity after a channel returns."""
        from pyflink.datastream.state import StateTtlConfig

        detector = clip_detector_job.AnomalyDetector()
        detector.config = spike_detector.DetectorConfig()
        assert detector._state_ttl().get_state_visibility() == (
            StateTtlConfig.StateVisibility.NeverReturnExpired
        )


class TestSuppressionRouting:
    """T037/T045. Contract §4.0 and research D15: the sources deserialize
    values only, so the operator never sees the Kafka key and everything is
    derived from the payload broadcaster_id."""

    def test_the_key_comes_from_the_payload(self):
        assert clip_detector_job.suppression_key(valid_suppression_record()) == BROADCASTER

    @pytest.mark.parametrize("name", sorted(INVALID_SUPPRESSION_RECORDS))
    def test_an_unroutable_record_still_gets_a_key_instead_of_raising(self, name):
        """A malformed record must reach process_element2 to be counted
        (contract §4.1). A keying function that raised, or that dropped the
        record, would make the rejection metric structurally unreachable."""
        raw, _ = INVALID_SUPPRESSION_RECORDS[name]
        key = clip_detector_job.suppression_key(raw)
        assert isinstance(key, int)
        if name.startswith(("id-", "not-json", "empty", "json-")):
            assert key == clip_detector_job.SUPPRESSION_UNROUTABLE_KEY

    def test_the_sentinel_key_is_not_a_broadcaster_id(self):
        assert clip_detector_job.SUPPRESSION_UNROUTABLE_KEY < 0

    def test_the_key_function_takes_only_the_value(self):
        import inspect

        params = list(inspect.signature(clip_detector_job.suppression_key).parameters)
        assert params == ["value"]


class TestProcessElement2Decoding:
    """T037. Contract §4.1: reject defensively, count by reason, and never let
    an exception escape -- one would fail the operator and stop chat detection
    for every key on the subtask."""

    def test_a_valid_notice_writes_the_deadline(self, metrics, store):
        detector = make_detector(clock_ms=lambda: OCCURRED_AT_MS + 200)
        feed_suppression(detector, store, valid_suppression_record())

        state = spike_detector.SuppressionState.from_json(
            store.for_key(BROADCASTER)["suppression"].value()
        )
        assert state.suppress_until_ms == OCCURRED_AT_MS + 120_000
        assert state.notice_type == GIFT
        assert state.notice_at_ms == OCCURRED_AT_MS

    def test_a_raid_uses_the_raid_window(self, metrics, store):
        detector = make_detector(clock_ms=lambda: OCCURRED_AT_MS)
        feed_suppression(detector, store, valid_suppression_record(notice_type=RAID))
        state = spike_detector.SuppressionState.from_json(
            store.for_key(BROADCASTER)["suppression"].value()
        )
        assert state.suppress_until_ms == OCCURRED_AT_MS + 180_000

    def test_a_bare_json_value_is_accepted_as_well_as_the_keyed_tuple(self, metrics, store):
        """Defensive: the wiring hands the operator a keyed tuple today, and a
        shape change must not silently stop suppression."""
        detector = make_detector(clock_ms=lambda: OCCURRED_AT_MS)
        store.bind(detector, BROADCASTER)
        ctx = FakeContext(key=BROADCASTER)
        list(detector.process_element2(valid_suppression_record(), ctx) or [])
        assert store.for_key(BROADCASTER)["suppression"].value() is not None

    @pytest.mark.parametrize("name", sorted(INVALID_SUPPRESSION_RECORDS))
    def test_a_malformed_record_is_counted_and_changes_nothing(self, name, metrics, store):
        raw, reason = INVALID_SUPPRESSION_RECORDS[name]
        detector = make_detector(clock_ms=lambda: OCCURRED_AT_MS)
        timers = FakeTimerService()

        emitted, _ = feed_suppression(detector, store, raw, timer_service=timers)

        assert emitted == []
        assert timers.registered == []
        assert store.for_key(BROADCASTER)["suppression"].writes == []
        rejected = metrics["suppression_records_rejected_total"]
        assert rejected.increments == [{"reason": reason}]
        # A rejected record is not an accepted one, so it produces no delivery
        # sample and cannot make a broken path look healthy (contract §4.1 rule 5).
        assert metrics["suppression_records_consumed_total"].increments == []
        assert metrics["suppression_delivery_age_seconds"].observations == []

    def test_the_rejection_reasons_stay_inside_the_bounded_label_set(self, metrics, store):
        detector = make_detector(clock_ms=lambda: OCCURRED_AT_MS)
        for raw, _ in INVALID_SUPPRESSION_RECORDS.values():
            feed_suppression(detector, store, raw)
        reasons = {d["reason"] for d in metrics["suppression_records_rejected_total"].increments}
        assert reasons <= set(spike_detector.SUPPRESSION_REJECT_REASONS)

    def test_the_optional_fields_are_carried_but_never_read(self, metrics, store):
        """Contract §2.2: notice_id, received_at_ms and viewer_count are
        diagnostic only, and a raid's audience size cannot change the window."""
        deadlines = set()
        for viewer_count in (None, 1, 90_000):
            local_store = FakeStateStore()
            detector = make_detector(clock_ms=lambda: OCCURRED_AT_MS)
            feed_suppression(
                detector,
                local_store,
                valid_suppression_record(
                    notice_type=RAID,
                    notice_id="9c2b1f4e-a1",
                    received_at_ms=OCCURRED_AT_MS + 175,
                    viewer_count=viewer_count,
                ),
            )
            deadlines.add(
                spike_detector.SuppressionState.from_json(
                    local_store.for_key(BROADCASTER)["suppression"].value()
                ).suppress_until_ms
            )
        assert deadlines == {OCCURRED_AT_MS + 180_000}

    def test_an_unexpected_error_never_escapes(self, monkeypatch, metrics, store, caplog):
        """The operator's own defence, on top of the decoder's: whatever fails
        inside, chat detection for every other key on this subtask survives."""
        def explode(*args, **kwargs):
            raise RuntimeError("boom")

        monkeypatch.setattr(clip_detector_job, "apply_notice", explode)
        detector = make_detector(clock_ms=lambda: OCCURRED_AT_MS)
        with caplog.at_level(logging.ERROR, logger="clip_detector"):
            emitted, _ = feed_suppression(detector, store, valid_suppression_record())
        assert emitted == []
        assert any("boom" in record.getMessage() for record in caplog.records)


class TestProcessElement2State:
    """T039. data-model §3.1 and contract §4.1 rules 6-8."""

    def test_a_later_notice_extends_and_writes(self, metrics, store):
        detector = make_detector(clock_ms=lambda: OCCURRED_AT_MS + 60_000)
        feed_suppression(detector, store, valid_suppression_record())
        feed_suppression(
            detector, store, valid_suppression_record(occurred_at_ms=OCCURRED_AT_MS + 60_000)
        )
        writes = store.for_key(BROADCASTER)["suppression"].writes
        assert len(writes) == 2
        assert spike_detector.SuppressionState.from_json(writes[-1]).suppress_until_ms == (
            OCCURRED_AT_MS + 60_000 + 120_000
        )

    def test_an_earlier_or_duplicate_notice_writes_nothing(self, metrics, store):
        """Write-on-change, the rule `hold` already follows: the deadline is a
        max-register, so a redelivery or an out-of-order notice costs no write."""
        detector = make_detector(clock_ms=lambda: OCCURRED_AT_MS + 1)
        feed_suppression(detector, store, valid_suppression_record(notice_type=RAID))
        feed_suppression(detector, store, valid_suppression_record(notice_type=RAID))
        feed_suppression(detector, store, valid_suppression_record(notice_type=GIFT))
        feed_suppression(
            detector, store,
            valid_suppression_record(notice_type=GIFT, occurred_at_ms=OCCURRED_AT_MS - 30_000),
        )
        assert len(store.for_key(BROADCASTER)["suppression"].writes) == 1

    def test_it_registers_no_timer_and_emits_nothing(self, metrics, store):
        """Contract §4.1 rule 7: waiting for suppression before deciding is
        explicitly rejected -- the spec asks for fail-open, not a delay."""
        detector = make_detector(clock_ms=lambda: OCCURRED_AT_MS)
        timers = FakeTimerService()
        emitted, ctx = feed_suppression(
            detector, store, valid_suppression_record(), timer_service=timers
        )
        assert emitted == []
        assert timers.registered == []

    def test_a_notice_touches_only_its_own_channel(self, metrics, store):
        """NFR-002 / I13, through the keyed context rather than a payload
        lookup: the operator's state is whatever Flink bound for this key."""
        detector = make_detector(clock_ms=lambda: OCCURRED_AT_MS)
        feed_suppression(
            detector, store, valid_suppression_record(broadcaster_id=1), key=1
        )
        store.bind(detector, 2)
        assert store.for_key(2)["suppression"].value() is None
        assert store.for_key(1)["suppression"].value() is not None

    def test_a_late_notice_is_applied_normally(self, metrics, store):
        """Contract §4.1 rule 8: lateness is not an error. It affects only the
        decisions taken after it lands (FR-018)."""
        detector = make_detector(clock_ms=lambda: OCCURRED_AT_MS + 90_000)
        feed_suppression(
            detector, store, valid_suppression_record(occurred_at_ms=OCCURRED_AT_MS)
        )
        assert store.for_key(BROADCASTER)["suppression"].value() is not None


class TestDeliveryClassification:
    """T039/T042. I19 / research D13: one clamped value from the consumer
    clock, captured at receipt, decides everything."""

    def observe_one(self, store, record, receipt_ms, config=None):
        detector = make_detector(clock_ms=lambda: receipt_ms, suppression_config=config)
        feed_suppression(detector, store, record)
        return detector

    def test_a_fresh_record_is_healthy_and_observed_once(self, metrics, store):
        self.observe_one(store, valid_suppression_record(), OCCURRED_AT_MS + 250)
        consumed = metrics["suppression_records_consumed_total"]
        assert consumed.increments == [{"lag_class": "healthy"}]
        assert metrics["suppression_delivery_age_seconds"].observations == [({}, pytest.approx(0.25))]

    def test_the_threshold_boundary_is_at_or_below(self, metrics, store):
        self.observe_one(store, valid_suppression_record(), OCCURRED_AT_MS + 30_000)
        assert metrics["suppression_records_consumed_total"].increments == [
            {"lag_class": "healthy"}
        ]

    def test_one_millisecond_past_the_threshold_is_lagging(self, metrics, store, caplog):
        with caplog.at_level(logging.INFO, logger="clip_detector"):
            self.observe_one(store, valid_suppression_record(), OCCURRED_AT_MS + 30_001)
        assert metrics["suppression_records_consumed_total"].increments == [
            {"lag_class": "lagging"}
        ]
        assert any("lag" in record.getMessage().lower() for record in caplog.records)

    def test_the_injected_clock_decides_not_wall_clock(self, metrics, store):
        """The consumer clock is injected in tests and read at receipt at
        runtime. A test that depended on time.time() could not pin either
        class deterministically."""
        self.observe_one(store, valid_suppression_record(), OCCURRED_AT_MS + 3_600_000)
        assert metrics["suppression_records_consumed_total"].increments == [
            {"lag_class": "lagging"}
        ]

    def test_a_fast_producer_does_not_rescue_a_slow_consumer(self, metrics, store):
        """The case NFR-005 turns on: Twitch-to-producer latency is 175 ms, so
        `received_at_ms - occurred_at_ms` looks healthy, while the record only
        reaches process_element2 45 s after it occurred. Classification reads
        the consumer receipt, so this is lagging."""
        record = valid_suppression_record(received_at_ms=OCCURRED_AT_MS + 175)
        self.observe_one(store, record, OCCURRED_AT_MS + 45_000)
        assert metrics["suppression_records_consumed_total"].increments == [
            {"lag_class": "lagging"}
        ]

    def test_the_optional_producer_clock_changes_no_classification(self, metrics, store):
        for received_at_ms in (OMIT, None, OCCURRED_AT_MS + 175, OCCURRED_AT_MS + 40_000):
            local_metrics_store = FakeStateStore()
            self.observe_one(
                local_metrics_store,
                valid_suppression_record(received_at_ms=received_at_ms),
                OCCURRED_AT_MS + 250,
            )
        assert {tuple(sorted(d.items()))
                for d in metrics["suppression_records_consumed_total"].increments} == {
            (("lag_class", "healthy"),)
        }

    def test_a_negative_raw_age_is_clamped_and_logged_as_skew(self, metrics, store, caplog):
        with caplog.at_level(logging.INFO, logger="clip_detector"):
            self.observe_one(store, valid_suppression_record(), OCCURRED_AT_MS - 5_000)
        assert metrics["suppression_records_consumed_total"].increments == [
            {"lag_class": "healthy"}
        ]
        assert metrics["suppression_delivery_age_seconds"].observations == [({}, 0.0)]
        assert any("skew" in record.getMessage().lower() for record in caplog.records)

    def test_the_configured_threshold_is_what_is_compared(self, metrics, store):
        tuned = spike_detector.SuppressionConfig(delivery_lag_warn_seconds=5)
        self.observe_one(store, valid_suppression_record(), OCCURRED_AT_MS + 5_001, config=tuned)
        assert metrics["suppression_records_consumed_total"].increments == [
            {"lag_class": "lagging"}
        ]

    def test_silence_publishes_nothing(self, metrics, store):
        """I19 / decision 20: a window with no record is idle/unknown, read in
        Prometheus as `increase(...) == 0`. Nothing may refresh a delivery
        value from on_timer, because during legitimate silence any value it
        published would be invented."""
        detector = make_detector(clock_ms=lambda: OCCURRED_AT_MS)
        store.bind(detector, BROADCASTER)
        ctx = FakeContext(key=BROADCASTER, timestamp=OCCURRED_AT_MS)
        detector.process_element1((BROADCASTER, chat_record()), ctx)
        fire_timer(detector, store, OCCURRED_AT_MS)
        assert metrics["suppression_records_consumed_total"].increments == []
        assert metrics["suppression_delivery_age_seconds"].observations == []


class TestFarFutureNoticeTime:
    """T037/T039 regression. An occurrence time far in the future must be
    rejected before it reaches the monotone register.

    `apply_notice()` only ever moves the deadline outward (I6), so one record
    claiming to have occurred centuries from now -- a microsecond value read as
    milliseconds, a badly set producer clock, or anything that is not this
    producer -- would pin `suppress_until_ms` past every later notice and
    silence that channel's clips for as long as the keyed state lives. There is
    no path in the register that moves a deadline back, so the only defence is
    to refuse the record: no deadline, no delivery sample, and an
    operationally visible rejection instead (FR-017, contract §4.1 rule 4).

    Ordinary clock disagreement is not this. The allowance is
    SUPPRESSION_MAX_FUTURE_SKEW_SECONDS = 30 s, and inside it the existing
    clamp-and-log behaviour is unchanged (contract §2.2).
    """

    RECEIPT_MS = OCCURRED_AT_MS

    def feed(self, store, occurred_at_ms, receipt_ms=None, detector=None, **optional):
        receipt = self.RECEIPT_MS if receipt_ms is None else receipt_ms
        detector = detector or make_detector(clock_ms=lambda: receipt)
        emitted, ctx = feed_suppression(
            detector,
            store,
            valid_suppression_record(occurred_at_ms=occurred_at_ms, **optional),
        )
        return detector, emitted, ctx

    def test_a_notice_beyond_the_allowance_is_rejected_as_a_field_problem(
        self, metrics, store
    ):
        _, emitted, _ = self.feed(store, self.RECEIPT_MS + 30_001)

        assert emitted == []
        assert metrics["suppression_records_rejected_total"].increments == [
            {"reason": "fields"}
        ]
        assert "fields" in spike_detector.SUPPRESSION_REJECT_REASONS
        # No state, and therefore no deadline that a later notice could not move.
        assert store.for_key(BROADCASTER)["suppression"].writes == []
        assert store.for_key(BROADCASTER)["suppression"].value() is None
        # A rejected record is not an accepted one: it must not appear in the
        # delivery signals, where it would read as a healthy consumed record
        # (contract §4.1 rule 5).
        assert metrics["suppression_records_consumed_total"].increments == []
        assert metrics["suppression_delivery_age_seconds"].observations == []

    def test_exactly_the_allowance_is_still_accepted_and_applied(self, metrics, store):
        """30 s of future skew is tolerated, clamped to a zero delivery age, and
        logged as skew -- the behaviour that was already contracted."""
        self.feed(store, self.RECEIPT_MS + 30_000)

        assert metrics["suppression_records_rejected_total"].increments == []
        assert metrics["suppression_records_consumed_total"].increments == [
            {"lag_class": "healthy"}
        ]
        assert metrics["suppression_delivery_age_seconds"].observations == [({}, 0.0)]
        state = spike_detector.SuppressionState.from_json(
            store.for_key(BROADCASTER)["suppression"].value()
        )
        assert state.suppress_until_ms == self.RECEIPT_MS + 30_000 + 120_000

    @pytest.mark.parametrize(
        "occurred_at_ms",
        [
            OCCURRED_AT_MS * 1000,          # microseconds mistaken for milliseconds
            OCCURRED_AT_MS * 1_000_000,     # nanoseconds, likewise
            OCCURRED_AT_MS + 86_400_000,    # a day ahead
            OCCURRED_AT_MS + 30_001,
        ],
    )
    def test_every_untrustworthy_shape_is_refused(self, occurred_at_ms, metrics, store):
        self.feed(store, occurred_at_ms)
        assert metrics["suppression_records_rejected_total"].increments == [
            {"reason": "fields"}
        ]
        assert store.for_key(BROADCASTER)["suppression"].writes == []

    def test_the_check_runs_before_the_register_is_touched(
        self, monkeypatch, metrics, store, caplog
    ):
        """Ordering, asserted rather than assumed. With apply_notice() replaced
        by a bomb, the far-future record must pass through without reaching it,
        while an acceptable record still does -- which is what makes the first
        half evidence of ordering and not of a disabled code path."""
        def explode(*args, **kwargs):
            raise RuntimeError("boom")

        monkeypatch.setattr(clip_detector_job, "apply_notice", explode)

        with caplog.at_level(logging.ERROR, logger="clip_detector"):
            self.feed(store, self.RECEIPT_MS + 3_600_000)
        assert not any("boom" in r.getMessage() for r in caplog.records)
        assert store.for_key(BROADCASTER)["suppression"].writes == []

        caplog.clear()
        with caplog.at_level(logging.ERROR, logger="clip_detector"):
            self.feed(store, self.RECEIPT_MS)
        assert any("boom" in r.getMessage() for r in caplog.records)

    def test_the_rejection_is_visible_and_carries_no_payload_content(
        self, metrics, store, caplog
    ):
        """FR-017 / NFR-006: operationally visible, attributable to the channel,
        and bounded. A rejection log that echoed the record would put producer
        data -- and, if the payload were ever wrong, user content -- into the
        operator log for an input that is by definition untrusted."""
        with caplog.at_level(logging.WARNING, logger="clip_detector"):
            self.feed(
                store,
                self.RECEIPT_MS + 1_000_000_000_000,
                notice_id="SENTINEL-NOTICE-ID",
            )

        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert len(warnings) == 1
        message = warnings[0].getMessage()
        assert str(BROADCASTER) in message
        assert len(message) < 500
        assert "SENTINEL-NOTICE-ID" not in message
        assert "schema_version" not in message

    def test_the_refused_record_cannot_poison_a_later_notice(self, metrics, store):
        """The consequence the whole check exists for: after the bad record, an
        ordinary notice still sets the deadline it should, because the register
        was never moved."""
        detector = make_detector(clock_ms=lambda: self.RECEIPT_MS)
        self.feed(store, self.RECEIPT_MS + 4_000_000_000_000, detector=detector)
        self.feed(store, self.RECEIPT_MS, detector=detector)

        state = spike_detector.SuppressionState.from_json(
            store.for_key(BROADCASTER)["suppression"].value()
        )
        assert state.suppress_until_ms == self.RECEIPT_MS + 120_000
        assert state.notice_at_ms == self.RECEIPT_MS

    def test_a_channel_is_not_silenced_by_a_poisoned_record(
        self, monkeypatch, metrics, store
    ):
        """End to end through the gate: the poisoned notice is refused, so a
        later spike on the same channel still clips instead of being suppressed
        until the state's TTL expires."""
        detector = make_detector(clock_ms=lambda: self.RECEIPT_MS)
        self.feed(store, self.RECEIPT_MS * 1000, detector=detector)

        peak_second = self.RECEIPT_MS // 1000 + 600
        stub_evaluate(monkeypatch, emitting_decision(peak_second))
        emitted, _ = fire_timer(detector, store, (peak_second + 5) * 1000)

        assert len(emitted) == 1
        assert metrics["clips_suppressed_total"].increments == []


class TestOutputGate:
    """T040/T044. data-model §3.2 and §3.3: an output-only filter at the very
    end of on_timer, after every state write."""

    PEAK_SECOND = OCCURRED_AT_MS // 1000
    REPORT_MS = (OCCURRED_AT_MS // 1000 + 10) * 1000

    def suppress_until(self, store, until_ms, notice_type=GIFT, key=BROADCASTER,
                       from_ms=None):
        """Write the keyed suppression state directly, as a notice would have.

        The window is the half-open interval `[suppress_from_ms,
        suppress_until_ms)`, so both ends are written here. `from_ms` defaults
        to the notice instant, which is what apply_notice() records for a first
        notice, and it is far enough behind PEAK_SECOND that the tests which
        only care about the deadline keep asserting the deadline.
        """
        notice_at_ms = until_ms - 120_000
        store.for_key(key)["suppression"].update(
            spike_detector.SuppressionState(
                suppress_from_ms=notice_at_ms if from_ms is None else from_ms,
                suppress_until_ms=until_ms,
                notice_type=notice_type,
                notice_at_ms=notice_at_ms,
            ).to_json()
        )

    def test_an_unsuppressed_spike_emits_as_before(self, monkeypatch, metrics, store):
        stub_evaluate(monkeypatch, emitting_decision(self.PEAK_SECOND))
        detector = make_detector()
        emitted, _ = fire_timer(detector, store, self.REPORT_MS)
        assert len(emitted) == 1
        anomaly = json.loads(emitted[0])
        assert anomaly["broadcaster_id"] == BROADCASTER
        assert anomaly["detected_at"] == self.PEAK_SECOND * 1000
        assert metrics["clips_suppressed_total"].increments == []

    def test_an_active_window_stops_the_output(self, monkeypatch, metrics, store, caplog):
        """FR-007 / FR-012: no clip, one attributable metric, one structured
        log, and the anomaly counter still moves so "detected but not clipped"
        stays computable (research R8)."""
        stub_evaluate(monkeypatch, emitting_decision(self.PEAK_SECOND))
        self.suppress_until(store, (self.PEAK_SECOND + 1) * 1000, notice_type=RAID)
        detector = make_detector()

        with caplog.at_level(logging.INFO, logger="clip_detector"):
            emitted, _ = fire_timer(detector, store, self.REPORT_MS)

        assert emitted == []
        assert metrics["clips_suppressed_total"].increments == [
            {"broadcaster_id": str(BROADCASTER), "notice_type": RAID}
        ]
        assert len(metrics["anomalies_detected_total"].increments) == 1
        suppression_logs = [
            r for r in caplog.records if "suppress" in r.getMessage().lower()
        ]
        assert len(suppression_logs) == 1
        message = suppression_logs[0].getMessage()
        assert str(BROADCASTER) in message and RAID in message

    def test_the_deadline_boundary_is_strict(self, monkeypatch, metrics, store):
        """peak_second * 1000 < suppress_until_ms. A peak exactly at the
        deadline is outside the window and still clips."""
        stub_evaluate(monkeypatch, emitting_decision(self.PEAK_SECOND))
        self.suppress_until(store, self.PEAK_SECOND * 1000)
        detector = make_detector()
        emitted, _ = fire_timer(detector, store, self.REPORT_MS)
        assert len(emitted) == 1
        assert metrics["clips_suppressed_total"].increments == []

    def test_the_gate_compares_the_peak_not_the_report_second(self, monkeypatch, metrics, store):
        """Research D5: a burst that peaks inside the window must not escape by
        being reported hold_cap_seconds later."""
        stub_evaluate(monkeypatch, emitting_decision(self.PEAK_SECOND))
        # The deadline sits between the peak and the report second.
        self.suppress_until(store, (self.PEAK_SECOND + 5) * 1000)
        detector = make_detector()
        emitted, _ = fire_timer(detector, store, self.REPORT_MS)
        assert emitted == []

    def test_a_peak_reported_after_a_later_notice_still_clips(
        self, monkeypatch, metrics, store
    ):
        """T040 regression, and the real shape of the hold delay rather than
        bare arithmetic.

        A spike peaks, the hold keeps it open, and a gift notice arrives 15
        seconds AFTER that peak. on_timer then reports the held peak. The gift
        cannot have caused a burst that peaked before it happened, so the clip
        must still be emitted: the notice opens
        `[occurred_at_ms, occurred_at_ms + 120 s)` and the peak is outside it
        (FR-006, FR-007, FR-018).

        The state here is written by process_element2 from a real record, not
        by hand, so the near end of the window is whatever the operator and
        apply_notice() actually agree on.
        """
        notice_ms = (self.PEAK_SECOND + 15) * 1000
        detector = make_detector(clock_ms=lambda: notice_ms + 100)
        feed_suppression(
            detector, store, valid_suppression_record(occurred_at_ms=notice_ms)
        )
        assert store.for_key(BROADCASTER)["suppression"].value() is not None

        stub_evaluate(monkeypatch, emitting_decision(self.PEAK_SECOND))
        emitted, _ = fire_timer(detector, store, (self.PEAK_SECOND + 20) * 1000)

        assert len(emitted) == 1
        assert json.loads(emitted[0])["detected_at"] == self.PEAK_SECOND * 1000
        assert metrics["clips_suppressed_total"].increments == []

    def test_a_peak_inside_that_same_notices_window_is_still_gated(
        self, monkeypatch, metrics, store
    ):
        """The other half of the pair, so the test above cannot be satisfied by
        simply not suppressing: the same notice, a peak at its occurrence
        instant, and the clip is gated (FR-007)."""
        notice_ms = (self.PEAK_SECOND + 15) * 1000
        detector = make_detector(clock_ms=lambda: notice_ms + 100)
        feed_suppression(
            detector, store, valid_suppression_record(occurred_at_ms=notice_ms)
        )

        stub_evaluate(monkeypatch, emitting_decision(self.PEAK_SECOND + 15))
        emitted, _ = fire_timer(detector, store, (self.PEAK_SECOND + 40) * 1000)

        assert emitted == []
        assert metrics["clips_suppressed_total"].increments == [
            {"broadcaster_id": str(BROADCASTER), "notice_type": GIFT}
        ]

    def test_the_kill_switch_restores_pre_007_behaviour(self, monkeypatch, metrics, store):
        """D11: with SUPPRESSION_GATING_ENABLED false the detector behaves
        exactly as before, while the topic and the subscriptions stay in place."""
        stub_evaluate(monkeypatch, emitting_decision(self.PEAK_SECOND))
        self.suppress_until(store, (self.PEAK_SECOND + 60) * 1000)
        detector = make_detector(
            suppression_config=spike_detector.SuppressionConfig(gating_enabled=False)
        )
        emitted, _ = fire_timer(detector, store, self.REPORT_MS)
        assert len(emitted) == 1
        assert metrics["clips_suppressed_total"].increments == []

    @pytest.mark.parametrize("encoded", [None, "", "{corrupt"])
    def test_absent_or_unreadable_state_fails_open(self, encoded, monkeypatch, metrics, store):
        """FR-011 / I10. Absent, never-written, expired under
        NeverReturnExpired, and unreadable all mean the same thing: not
        suppressed."""
        stub_evaluate(monkeypatch, emitting_decision(self.PEAK_SECOND))
        if encoded is not None:
            store.for_key(BROADCASTER)["suppression"].update(encoded)
        detector = make_detector()
        emitted, _ = fire_timer(detector, store, self.REPORT_MS)
        assert len(emitted) == 1

    def test_every_state_write_happens_before_the_gate(self, monkeypatch, metrics, store):
        """I11 / SC-004. The gated run must be byte-identical in state to the
        ungated one -- including last_fire_second, which starts the cooldown as
        if a clip had been created (research D6)."""
        hold = spike_detector.HoldState(
            started_at=self.PEAK_SECOND, peak_intensity=9.5, peak_at=self.PEAK_SECOND,
            peak_message_count=420, peak_baseline_mean=10.0, peak_baseline_std=2.0,
        )
        expired = [self.PEAK_SECOND - 400, self.PEAK_SECOND - 399]

        def run(gating_enabled):
            local_store = FakeStateStore()
            local_store.for_key(BROADCASTER)["counts"] = FakeMapState(
                {self.PEAK_SECOND: 3, self.PEAK_SECOND - 400: 1, self.PEAK_SECOND - 399: 1}
            )
            # Identical suppression state in both runs; only the kill switch
            # differs, so any difference below is the gate touching state.
            self.suppress_until(local_store, (self.PEAK_SECOND + 5) * 1000)
            detector = make_detector(
                suppression_config=spike_detector.SuppressionConfig(
                    gating_enabled=gating_enabled
                )
            )
            emitted, ctx = fire_timer(detector, local_store, self.REPORT_MS)
            state = local_store.for_key(BROADCASTER)
            return emitted, {
                "hold": state["hold"].writes,
                "last_fire": state["last_fire_second"].writes,
                "removed": sorted(state["counts"].removed),
                "timers": ctx.timer_service().registered,
            }

        stub_evaluate(monkeypatch, emitting_decision(self.PEAK_SECOND, expired, hold))
        gated_out, gated_state = run(True)
        ungated_out, ungated_state = run(False)

        assert gated_state == ungated_state
        assert gated_state["last_fire"] == [self.REPORT_MS // 1000]
        assert gated_state["removed"] == expired
        assert gated_state["timers"]
        assert gated_out == [] and len(ungated_out) == 1

    def test_a_late_notice_never_retracts_an_emitted_clip(self, monkeypatch, metrics, store):
        """FR-018 / I12 / US1-6: the state is read at decision time only, and
        there is no retraction path. A notice that lands after the emission
        affects only later decisions."""
        stub_evaluate(monkeypatch, emitting_decision(self.PEAK_SECOND))
        detector = make_detector(clock_ms=lambda: self.REPORT_MS)
        first, _ = fire_timer(detector, store, self.REPORT_MS)
        assert len(first) == 1

        feed_suppression(
            detector,
            store,
            valid_suppression_record(occurred_at_ms=(self.PEAK_SECOND - 30) * 1000),
        )

        assert len(first) == 1
        assert metrics["clips_suppressed_total"].increments == []
        # The same notice does gate the next decision, whose peak is inside it.
        stub_evaluate(monkeypatch, emitting_decision(self.PEAK_SECOND + 20))
        second, _ = fire_timer(detector, store, self.REPORT_MS + 20_000)
        assert second == []
        assert len(metrics["clips_suppressed_total"].increments) == 1


class TestSuppressionSourceWiring:
    """T038's PyFlink half. Every value comes from the pure
    SuppressionSourceSettings of T031, which test_spike_detector.py asserts
    without PyFlink; this proves the job actually builds its source from them.
    Fakes only: no gateway, no cluster, and main() is never called."""

    @pytest.fixture
    def kafka_fakes(self, monkeypatch):
        calls = []

        class FakeBuilder:
            def set_bootstrap_servers(self, value):
                calls.append(("bootstrap_servers", value))
                return self

            def set_topics(self, *topics):
                calls.append(("topics", topics))
                return self

            def set_group_id(self, value):
                calls.append(("group_id", value))
                return self

            def set_starting_offsets(self, value):
                calls.append(("starting_offsets", value))
                return self

            def set_value_only_deserializer(self, value):
                calls.append(("value_only_deserializer", value))
                return self

            def build(self):
                calls.append(("build", None))
                return "SOURCE"

        monkeypatch.setattr(
            clip_detector_job, "KafkaSource",
            type("FakeKafkaSource", (), {"builder": staticmethod(lambda: FakeBuilder())}),
        )
        monkeypatch.setattr(
            clip_detector_job, "KafkaOffsetsInitializer",
            type("FakeOffsets", (), {
                "latest": staticmethod(lambda: "LATEST"),
                "earliest": staticmethod(lambda: "EARLIEST"),
            }),
        )
        monkeypatch.setattr(clip_detector_job, "SimpleStringSchema", lambda: "VALUE_ONLY")
        return calls

    @pytest.fixture
    def watermark_fakes(self, monkeypatch):
        calls = []

        class FakeStrategy:
            def with_idleness(self, duration):
                calls.append(("idleness", duration))
                return self

            def with_timestamp_assigner(self, assigner):
                calls.append(("assigner", assigner))
                return self

        monkeypatch.setattr(
            clip_detector_job, "Duration",
            type("FakeDuration", (), {"of_seconds": staticmethod(lambda n: ("seconds", n))}),
        )

        def for_bounded(duration):
            calls.append(("out_of_orderness", duration))
            return FakeStrategy()

        monkeypatch.setattr(
            clip_detector_job, "WatermarkStrategy",
            type("FakeWatermarkStrategy", (), {
                "for_bounded_out_of_orderness": staticmethod(for_bounded)
            }),
        )
        return calls

    def test_the_source_is_built_from_the_pure_settings(self, kafka_fakes):
        settings = spike_detector.SuppressionSourceSettings()
        assert clip_detector_job.build_suppression_source(settings) == "SOURCE"
        recorded = dict(kafka_fakes)
        assert recorded["topics"] == (settings.topic,)
        # latest(), never earliest(): old notices must not be replayed into
        # event time and pin the operator watermark in the past (research D4).
        assert recorded["starting_offsets"] == "LATEST"
        assert settings.starting_offsets == "latest"
        # Value-only, which is why the operator never sees the Kafka key
        # (contract §4.0, research D15).
        assert recorded["value_only_deserializer"] == "VALUE_ONLY"
        assert recorded["bootstrap_servers"] == clip_detector_job.KAFKA_BOOTSTRAP_SERVERS

    def test_the_watermark_strategy_is_built_from_the_pure_settings(self, watermark_fakes):
        settings = spike_detector.SuppressionSourceSettings()
        clip_detector_job.build_suppression_watermark_strategy(settings)
        recorded = dict(watermark_fakes)
        assert recorded["out_of_orderness"] == ("seconds", settings.out_of_orderness_seconds)
        assert recorded["idleness"] == ("seconds", settings.idleness_seconds)
        # I15: strictly below the chat stream's, so suppression is never the
        # binding watermark minimum in steady state.
        assert settings.idleness_seconds < spike_detector.WATERMARK_IDLENESS_SECONDS
        assert isinstance(
            recorded["assigner"], clip_detector_job.SuppressionTimestampAssigner
        )

    def test_event_time_comes_from_occurred_at_ms(self):
        """Twitch's clock, the same one and the same converter chat-messages
        uses, which is what makes peak_second * 1000 < suppress_until_ms
        meaningful (contract invariant 2)."""
        assigner = clip_detector_job.SuppressionTimestampAssigner()
        assert assigner.extract_timestamp(valid_suppression_record(), 999) == OCCURRED_AT_MS

    @pytest.mark.parametrize(
        "raw",
        [
            "{not json",
            valid_suppression_record(occurred_at_ms=OMIT),
            valid_suppression_record(occurred_at_ms=None),
        ],
    )
    def test_a_record_without_a_usable_time_falls_back_to_the_record_timestamp(self, raw):
        """Handing None to Flink's timestamp assignment is what the chat
        assigner already guards against; this one must not be different."""
        assigner = clip_detector_job.SuppressionTimestampAssigner()
        assert assigner.extract_timestamp(raw, 999) == 999

    def test_both_streams_are_keyed_then_connected_then_processed(self):
        """D3: a second KafkaSource connected to the keyed chat stream through
        a KeyedCoProcessFunction, both keyed on the payload broadcaster_id."""
        log = []

        class FakeStream:
            def __init__(self, name):
                self.name = name

            def map(self, fn):
                log.append(("map", self.name, fn))
                return FakeStream(self.name + ":mapped")

            def key_by(self, fn):
                log.append(("key_by", self.name, fn))
                return FakeStream(self.name + ":keyed")

            def connect(self, other):
                log.append(("connect", self.name, other.name))
                return FakeConnected()

        class FakeConnected:
            def process(self, function):
                log.append(("process", function))
                return FakeStream("processed")

        detector = clip_detector_job.AnomalyDetector()
        clip_detector_job.connect_detector(
            FakeStream("chat"), FakeStream("suppression"), detector
        )

        steps = [entry[0] for entry in log]
        assert steps.count("key_by") == 2
        assert steps.index("connect") > max(
            i for i, step in enumerate(steps) if step == "key_by"
        )
        assert steps.index("process") > steps.index("connect")
        assert log[-1][0] == "process" and log[-1][1] is detector

        keys = [entry[2] for entry in log if entry[0] == "map"]
        assert [fn(chat_record()) for fn in keys[:1]] == [(BROADCASTER, chat_record())]
        assert keys[1](valid_suppression_record())[0] == BROADCASTER

    def test_the_partition_count_matches_the_jobs_parallelism(self):
        settings = spike_detector.SuppressionSourceSettings()
        assert clip_detector_job.FLINK_PARALLELISM == settings.expected_parallelism
        assert settings.expected_partitions == settings.expected_parallelism


class TestSuppressionMetricRegistration:
    """T042. The label sets are the contract with the dashboards and alerts:
    only reason/category labels are bounded, and broadcaster attribution is
    kept because NFR-006 requires it."""

    @pytest.fixture
    def registered(self, monkeypatch):
        created = {}

        def fake_counter(name, description, labels):
            created[name] = ("counter", tuple(labels))
            return RecordingMetric(name, labels)

        def fake_gauge(name, description, labels):
            created[name] = ("gauge", tuple(labels))
            return RecordingMetric(name, labels)

        def fake_histogram(name, description, labels=(), **kwargs):
            created[name] = ("histogram", tuple(labels))
            return RecordingMetric(name, labels)

        monkeypatch.setattr(clip_detector_job, "Counter", fake_counter)
        monkeypatch.setattr(clip_detector_job, "Gauge", fake_gauge)
        monkeypatch.setattr(clip_detector_job, "Histogram", fake_histogram, raising=False)
        monkeypatch.setattr(clip_detector_job, "start_http_server", lambda port: None)
        monkeypatch.setattr(clip_detector_job, "_metrics_initialized", False)
        clip_detector_job._init_metrics(0)
        yield created
        monkeypatch.setattr(clip_detector_job, "_metrics_initialized", False)

    def test_the_suppressed_clip_signal_is_attributable(self, registered):
        kind, labels = registered["clips_suppressed_total"]
        assert kind == "counter"
        assert set(labels) == {"broadcaster_id", "notice_type"}

    def test_the_rejection_reason_label_is_bounded(self, registered):
        assert registered["suppression_records_rejected_total"][1] == ("reason",)
        assert spike_detector.SUPPRESSION_REJECT_REASONS == (
            "decode", "schema_version", "fields"
        )

    def test_the_delivery_signals(self, registered):
        assert registered["suppression_records_consumed_total"][1] == ("lag_class",)
        assert spike_detector.SUPPRESSION_LAG_CLASSES == ("healthy", "lagging")
        kind, labels = registered["suppression_delivery_age_seconds"]
        assert kind == "histogram"
        # No per-channel delivery gauge: during legitimate silence it would
        # have to invent a value (decision 20, NFR-005).
        assert labels == ()

    def test_the_existing_anomaly_counter_is_untouched(self, registered):
        """R8: `anomalies_detected_total` keeps incrementing for a suppressed
        decision, so AnomalyDetectionStalled keeps its meaning."""
        assert registered["anomalies_detected_total"] == ("counter", ("broadcaster_id",))
