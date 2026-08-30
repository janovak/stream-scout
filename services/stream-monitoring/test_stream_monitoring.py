#!/usr/bin/env python3
"""
Unit tests for Stream Monitoring Service

Tests token management, message processing, and service components.
"""

import asyncio
import json
import logging
import os
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import eventsub_pool
import reconciler as reconciler_module
import stream_monitoring_service
import token_manager
from eventsub_pool import (
    SUBSCRIPTIONS_PER_CONNECTION,
    EventSubPoolTransport,
    map_chat_message,
    to_epoch_ms,
)
from reconciler import (
    DESIRED_GENERATION_KEY,
    DESIRED_IDS_KEY,
    DESIRED_KEY,
    REFUSAL_RECHECK_DAYS,
    PostgresRefusalStore,
    RateLimitedError,
    Reconciler,
    ReconcilerConfig,
    RefusalStore,
    StubTransport,
    SubscriptionRefusedError,
    TransportError,
)
from stream_monitoring_service import StreamMonitoringService, compute_desired_set
from token_manager import TokenRecord, TwitchCredentials


class FakeRedis:
    """Enough Redis for the poller/reconciler seam, held in memory.

    Both sides of the seam use one instance in these tests, so a test
    exercises the real key layout rather than an assumption about it. Every
    operation is recorded in `calls`, which is what the FR-003 test counts: a
    pipeline records one `pipeline.execute`, because that is one round trip
    however many commands it carries.
    """

    def __init__(self):
        self.strings = {}
        self.zsets = {}
        self.hashes = {}
        self.calls = []
        self.fail_on = set()
        self._recording = True

    def _record(self, name):
        if name in self.fail_on:
            raise ConnectionError(f"simulated Redis failure on {name}")
        if self._recording:
            self.calls.append(name)

    def exists(self, key):
        self._record("exists")
        return 1 if key in self.strings or key in self.zsets or key in self.hashes else 0

    def setex(self, key, ttl, value):
        self._record("setex")
        self.strings[key] = str(value)

    def get(self, key):
        self._record("get")
        return self.strings.get(key)

    def incr(self, key):
        self._record("incr")
        self.strings[key] = str(int(self.strings.get(key, 0)) + 1)
        return int(self.strings[key])

    def delete(self, *keys):
        self._record("delete")
        for key in keys:
            self.strings.pop(key, None)
            self.zsets.pop(key, None)
            self.hashes.pop(key, None)

    def zadd(self, key, mapping):
        self._record("zadd")
        self.zsets.setdefault(key, {}).update(
            {member: float(score) for member, score in mapping.items()}
        )

    def zrange(self, key, start, end):
        self._record("zrange")
        ordered = sorted(self.zsets.get(key, {}).items(), key=lambda kv: (kv[1], kv[0]))
        if end == -1:
            end = len(ordered) - 1
        return [member for member, _ in ordered[start:end + 1]]

    def hset(self, key, mapping=None):
        self._record("hset")
        self.hashes.setdefault(key, {}).update(
            {field: str(value) for field, value in (mapping or {}).items()}
        )

    def hgetall(self, key):
        self._record("hgetall")
        return dict(self.hashes.get(key, {}))

    def pipeline(self):
        return FakePipeline(self)


class FakePipeline:
    """A MULTI/EXEC that applies its queued commands as one round trip."""

    def __init__(self, client):
        self.client = client
        self.queued = []

    def delete(self, *keys):
        self.queued.append(("delete", (keys), {}))
        return self

    def zadd(self, key, mapping):
        self.queued.append(("zadd", (key, mapping), {}))
        return self

    def hset(self, key, mapping=None):
        self.queued.append(("hset", (key,), {"mapping": mapping}))
        return self

    def incr(self, key):
        self.queued.append(("incr", (key,), {}))
        return self

    def execute(self):
        self.client._recording = False
        try:
            for name, args, kwargs in self.queued:
                if name == "delete":
                    self.client.delete(*args)
                else:
                    getattr(self.client, name)(*args, **kwargs)
        finally:
            self.client._recording = True
        self.client.calls.append("pipeline.execute")
        self.queued = []


def make_stream(login, user_id):
    """A stand-in for one Helix stream row."""
    stream = MagicMock()
    stream.user_login = login
    stream.user_id = str(user_id)
    return stream


class FakeTwitch:
    """Serves a fixed ranking through the auto-paginating get_streams API."""

    def __init__(self, streams):
        self.streams = streams

    def get_streams(self, first=100):
        async def pages():
            for stream in self.streams:
                yield stream

        return pages()


def make_poller(logins, fake_redis, reconciler=None):
    """A StreamMonitoringService wired for a poll, with Postgres/Kafka mocked.

    `logins` is the ranking, best first. Broadcaster ids are derived from the
    position so a test can predict them.
    """
    service = StreamMonitoringService()
    service.twitch = FakeTwitch([make_stream(login, 1000 + i) for i, login in enumerate(logins)])
    service.redis_client = fake_redis
    service.reconciler = reconciler
    service._get_clipping_disabled_ids = MagicMock(return_value=set())
    service._upsert_streamer = MagicMock()
    service._publish_lifecycle_event = MagicMock()
    return service


def broadcaster_id_for(logins, login):
    return 1000 + logins.index(login)


class TestTwitchCredentials:
    """Tests for TwitchCredentials -- real file I/O via tmp_path, zero mocks
    except `requests`, patched at the module seam for refresh()."""

    def test_load_returns_token_record(self, tmp_path):
        """Successfully load a valid token file."""
        token_file = tmp_path / "tokens.json"
        token_file.write_text(json.dumps({
            "access_token": "test_access_token",
            "refresh_token": "test_refresh_token",
            "scopes": ["chat:read", "clips:edit"],
            "created_at": "2026-01-11T00:00:00Z",
        }))

        record = TwitchCredentials(token_file).load()

        assert record == TokenRecord(
            access_token="test_access_token",
            refresh_token="test_refresh_token",
            scopes=["chat:read", "clips:edit"],
            created_at="2026-01-11T00:00:00Z",
            updated_at=None,
        )

    def test_load_seed_tool_record_loads_cleanly(self, tmp_path):
        """A record shaped like seed_twitch_tokens.py writes it (created_at,
        no updated_at) loads without error."""
        token_file = tmp_path / "tokens.json"
        token_file.write_text(json.dumps({
            "access_token": "seeded_access",
            "refresh_token": "seeded_refresh",
            "scopes": ["chat:read"],
            "created_at": "2026-01-11T00:00:00Z",
        }))

        record = TwitchCredentials(token_file).load()

        assert record.access_token == "seeded_access"
        assert record.created_at == "2026-01-11T00:00:00Z"
        assert record.updated_at is None

    def test_load_missing_file_raises_error_naming_seed_tool(self, tmp_path):
        """A missing token file should point the operator at the seed tool."""
        token_file = tmp_path / "nonexistent.json"

        with pytest.raises(FileNotFoundError, match="seed_twitch_tokens.py"):
            TwitchCredentials(token_file).load()

    def test_load_malformed_json_raises_clear_error(self, tmp_path):
        """Malformed JSON should raise JSONDecodeError, not something opaque."""
        token_file = tmp_path / "tokens.json"
        token_file.write_text("not valid json {{{")

        with pytest.raises(json.JSONDecodeError):
            TwitchCredentials(token_file).load()

    def test_load_missing_access_token_raises(self, tmp_path):
        """Raise ValueError when access_token is missing."""
        token_file = tmp_path / "tokens.json"
        token_file.write_text(json.dumps({
            "refresh_token": "test_refresh_token",
            "scopes": ["chat:read"],
        }))

        with pytest.raises(ValueError, match="access_token"):
            TwitchCredentials(token_file).load()

    def test_load_missing_refresh_token_raises(self, tmp_path):
        """Raise ValueError when refresh_token is missing."""
        token_file = tmp_path / "tokens.json"
        token_file.write_text(json.dumps({
            "access_token": "test_access_token",
            "scopes": ["chat:read"],
        }))

        with pytest.raises(ValueError, match="refresh_token"):
            TwitchCredentials(token_file).load()

    def test_persist_preserves_scopes_and_created_at_without_prior_load(self, tmp_path):
        """This is the bug that exists today: a caller that never called
        `load` first must not blank out scopes/created_at on persist."""
        token_file = tmp_path / "tokens.json"
        token_file.write_text(json.dumps({
            "access_token": "old_access",
            "refresh_token": "old_refresh",
            "scopes": ["chat:read", "clips:edit"],
            "created_at": "2026-01-11T00:00:00Z",
        }))

        record = TwitchCredentials(token_file).persist("new_access", "new_refresh")

        assert record.scopes == ["chat:read", "clips:edit"]
        assert record.created_at == "2026-01-11T00:00:00Z"
        data = json.loads(token_file.read_text())
        assert data["scopes"] == ["chat:read", "clips:edit"]
        assert data["created_at"] == "2026-01-11T00:00:00Z"

    def test_persist_sets_updated_at(self, tmp_path):
        """persist should stamp updated_at on every write."""
        token_file = tmp_path / "tokens.json"
        token_file.write_text(json.dumps({
            "access_token": "old_access",
            "refresh_token": "old_refresh",
            "scopes": [],
        }))

        record = TwitchCredentials(token_file).persist("new_access", "new_refresh")

        assert record.updated_at is not None
        data = json.loads(token_file.read_text())
        assert data["updated_at"] == record.updated_at

    def test_persist_interrupted_write_leaves_previous_file_intact(self, tmp_path):
        """A crash between the temp-file write and the atomic replace must
        not corrupt or lose the previous file."""
        token_file = tmp_path / "tokens.json"
        original = {
            "access_token": "old_access",
            "refresh_token": "old_refresh",
            "scopes": ["chat:read"],
            "created_at": "2026-01-11T00:00:00Z",
        }
        token_file.write_text(json.dumps(original))

        with patch("token_manager.os.replace", side_effect=OSError("simulated crash")):
            with pytest.raises(OSError):
                TwitchCredentials(token_file).persist("new_access", "new_refresh")

        assert json.loads(token_file.read_text()) == original
        assert list(tmp_path.glob(".tmp-tokens-*")) == []

    def test_persist_sets_group_and_permissions_before_replace(self, tmp_path):
        """Issue 1 (KNOWN_ISSUES.md): mkstemp() always creates the temp file
        at 0600 owned by whoever wrote it, which locks the other container
        out on the very next read. Every write must chown the temp file to
        TWITCH_TOKEN_GID and chmod it 0640 before the atomic replace, so a
        write from either container's uid stays readable by the other."""
        token_file = tmp_path / "tokens.json"
        token_file.write_text(json.dumps({
            "access_token": "old_access",
            "refresh_token": "old_refresh",
            "scopes": [],
        }))
        calls = []
        real_replace = os.replace

        def fake_chown(path, uid, gid):
            calls.append(("chown", path, gid))

        def fake_chmod(path, mode):
            calls.append(("chmod", path, mode))

        def fake_replace(src, dst):
            calls.append(("replace", src, dst))
            real_replace(src, dst)

        with patch("token_manager.os.chown", side_effect=fake_chown), \
             patch("token_manager.os.chmod", side_effect=fake_chmod), \
             patch("token_manager.os.replace", side_effect=fake_replace):
            TwitchCredentials(token_file).persist("new_access", "new_refresh")

        kinds = [c[0] for c in calls]
        assert kinds == ["chown", "chmod", "replace"], (
            "chown and chmod must happen before the atomic replace, or the "
            "published file is briefly at mkstemp's default 0600"
        )
        assert calls[0][2] == token_manager.TWITCH_TOKEN_GID
        assert calls[1][2] == 0o640

    @patch("token_manager.requests.post")
    def test_refresh_sets_group_and_permissions_before_replace(self, mock_post, tmp_path):
        """Same guarantee as persist(), for the refresh() write path -- this
        is the one Flink's 401 handler calls directly."""
        token_file = tmp_path / "tokens.json"
        token_file.write_text(json.dumps({
            "access_token": "old_access",
            "refresh_token": "old_refresh",
            "scopes": [],
        }))
        mock_post.return_value.status_code = 200
        mock_post.return_value.json.return_value = {
            "access_token": "new_access",
            "refresh_token": "new_refresh",
            "expires_in": 3600,
        }
        calls = []

        def fake_chown(path, uid, gid):
            calls.append(("chown", path, gid))

        def fake_chmod(path, mode):
            calls.append(("chmod", path, mode))

        with patch("token_manager.os.chown", side_effect=fake_chown), \
             patch("token_manager.os.chmod", side_effect=fake_chmod):
            TwitchCredentials(token_file).refresh("client_id", "client_secret")

        assert calls == [
            ("chown", calls[0][1], token_manager.TWITCH_TOKEN_GID),
            ("chmod", calls[1][1], 0o640),
        ]

    @patch("token_manager.requests.post")
    def test_refresh_stores_rotated_refresh_token(self, mock_post, tmp_path):
        """refresh should store the new refresh token when Twitch rotates it."""
        token_file = tmp_path / "tokens.json"
        token_file.write_text(json.dumps({
            "access_token": "old_access",
            "refresh_token": "old_refresh",
            "scopes": ["clips:edit"],
            "created_at": "2026-01-11T00:00:00Z",
        }))
        mock_post.return_value.status_code = 200
        mock_post.return_value.json.return_value = {
            "access_token": "new_access",
            "refresh_token": "rotated_refresh",
            "expires_in": 3600,
        }

        record = TwitchCredentials(token_file).refresh("client_id", "client_secret")

        assert record.access_token == "new_access"
        assert record.refresh_token == "rotated_refresh"
        assert record.scopes == ["clips:edit"]
        assert record.created_at == "2026-01-11T00:00:00Z"

    @patch("token_manager.requests.post")
    def test_refresh_keeps_old_refresh_token_when_omitted(self, mock_post, tmp_path):
        """refresh should keep the old refresh token when Twitch's response
        omits a new one."""
        token_file = tmp_path / "tokens.json"
        token_file.write_text(json.dumps({
            "access_token": "old_access",
            "refresh_token": "old_refresh",
            "scopes": [],
        }))
        mock_post.return_value.status_code = 200
        mock_post.return_value.json.return_value = {
            "access_token": "new_access",
            "expires_in": 3600,
        }

        record = TwitchCredentials(token_file).refresh("client_id", "client_secret")

        assert record.access_token == "new_access"
        assert record.refresh_token == "old_refresh"

    def test_concurrent_refreshes_serialize(self, tmp_path):
        """Two TwitchCredentials instances refreshing the same file at once
        must not interleave -- one waits for the other, and the file is
        never torn."""
        token_file = tmp_path / "tokens.json"
        token_file.write_text(json.dumps({
            "access_token": "old_access",
            "refresh_token": "old_refresh",
            "scopes": [],
        }))

        intervals = []
        intervals_lock = threading.Lock()

        def fake_post(*args, **kwargs):
            start = time.monotonic()
            time.sleep(0.05)
            end = time.monotonic()
            with intervals_lock:
                intervals.append((start, end))
            response = MagicMock()
            response.status_code = 200
            response.json.return_value = {
                "access_token": f"access-{start}",
                "refresh_token": f"refresh-{start}",
                "expires_in": 3600,
            }
            return response

        results = []

        def run():
            creds = TwitchCredentials(token_file)
            results.append(creds.refresh("client_id", "client_secret"))

        with patch("token_manager.requests.post", side_effect=fake_post):
            threads = [threading.Thread(target=run) for _ in range(2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        assert len(results) == 2
        (s1, e1), (s2, e2) = intervals
        assert e1 <= s2 or e2 <= s1, "refreshes overlapped -- lock did not serialize them"

        # File is valid JSON with the winning refresh's values -- never torn.
        data = json.loads(token_file.read_text())
        assert data["access_token"] in {r.access_token for r in results}


class TestMessagePayload:
    """Tests for chat message payload structure."""

    def test_message_payload_structure(self):
        """Message payload should match expected Kafka schema."""
        payload = {
            "broadcaster_id": 12345,
            "timestamp": 1704067200000,
            "message_id": "uuid-string",
            "text": "message content",
            "user_id": 67890,
            "user_name": "viewer_name",
            "metadata": {
                "emotes": {},
                "badges": {},
                "is_subscriber": False,
                "is_mod": False,
            },
        }

        # Validate required fields exist
        assert "broadcaster_id" in payload
        assert "timestamp" in payload
        assert "message_id" in payload
        assert "text" in payload
        assert "user_id" in payload
        assert "user_name" in payload
        assert "metadata" in payload

        # Validate types
        assert isinstance(payload["broadcaster_id"], int)
        assert isinstance(payload["timestamp"], int)
        assert isinstance(payload["text"], str)
        assert isinstance(payload["metadata"], dict)

    def test_lifecycle_event_structure(self):
        """Lifecycle event payload should match expected schema."""
        event = {
            "event_type": "online",
            "broadcaster_id": 12345,
            "broadcaster_login": "streamer_name",
            "rank": 1,
            "timestamp": 1704067200,
        }

        assert event["event_type"] in ["online", "offline"]
        assert isinstance(event["broadcaster_id"], int)
        assert isinstance(event["broadcaster_login"], str)
        assert isinstance(event["rank"], int)
        assert isinstance(event["timestamp"], int)


class TestKafkaDelivery:
    """Tests for Kafka message delivery."""

    def test_kafka_message_keyed_by_broadcaster_id(self):
        """Kafka messages should be keyed by broadcaster_id for partitioning."""
        broadcaster_id = 12345
        key = str(broadcaster_id).encode("utf-8")

        # Verify key format
        assert key == b"12345"
        assert isinstance(key, bytes)

    def test_kafka_message_value_is_json(self):
        """Kafka message value should be valid JSON."""
        message = {
            "broadcaster_id": 12345,
            "timestamp": 1704067200000,
            "text": "test message",
        }

        value = json.dumps(message).encode("utf-8")

        # Verify we can decode and parse
        decoded = json.loads(value.decode("utf-8"))
        assert decoded["broadcaster_id"] == 12345


class TestRedisKeyManagement:
    """Tests for Redis key patterns and TTL management."""

    def test_redis_key_format(self):
        """Redis keys should follow expected pattern."""
        broadcaster_login = "ninja"
        redis_key = f"streamer:online:{broadcaster_login}"

        assert redis_key == "streamer:online:ninja"
        assert redis_key.startswith("streamer:online:")

    def test_streamer_ttl_value(self):
        """Streamer TTL should be 3 minutes (180 seconds)."""
        REDIS_STREAMER_TTL = 180
        assert REDIS_STREAMER_TTL == 180

    def test_offline_detection_via_ttl_expiry(self):
        """Offline detection relies on Redis key expiration."""
        # When TTL expires, key is deleted
        # Service checks key existence to determine online status
        # This tests the logic concept

        def check_online(redis_client, broadcaster_login):
            redis_key = f"streamer:online:{broadcaster_login}"
            return redis_client.exists(redis_key)

        # Mock Redis client
        mock_redis = MagicMock()
        mock_redis.exists.return_value = False

        is_online = check_online(mock_redis, "expired_streamer")
        assert not is_online


class TestPrometheusMetrics:
    """Tests for Prometheus metrics configuration."""

    def test_metric_labels(self):
        """Metrics should use correct label names."""
        # Verify expected label patterns
        chat_message_labels = ["broadcaster_id"]
        kafka_labels = ["topic"]
        api_error_labels = ["error_type"]

        assert "broadcaster_id" in chat_message_labels
        assert "topic" in kafka_labels
        assert "error_type" in api_error_labels


class TestGracefulShutdown:
    """Tests for graceful shutdown handling."""

    def test_shutdown_sequence(self):
        """Shutdown should occur in correct order."""
        shutdown_order = []

        # Simulate shutdown steps
        def stop_scheduler():
            shutdown_order.append("scheduler")

        def close_twitch():
            shutdown_order.append("twitch")

        def flush_kafka():
            shutdown_order.append("kafka")

        def close_db():
            shutdown_order.append("db")

        def close_redis():
            shutdown_order.append("redis")

        # Execute shutdown
        stop_scheduler()
        close_twitch()
        flush_kafka()
        close_db()
        close_redis()

        # Verify order
        assert shutdown_order[0] == "scheduler"
        assert shutdown_order[-1] == "redis"
        assert len(shutdown_order) == 5


class TestChannelThresholdConfig:
    """Tests for the env-configurable monitored-set size.

    These call resolve_thresholds() directly with an env mapping rather than
    reloading the module: module-level Prometheus collectors cannot be
    registered twice in one process, so the module is not reloadable.
    """

    def test_thresholds_default_to_15_and_30(self):
        """Absent env vars, the shipped defaults are unchanged."""
        assert stream_monitoring_service.resolve_thresholds({}) == (15, 30)

    def test_thresholds_read_from_environment(self):
        """The monitored set can be ramped without editing code."""
        join, leave = stream_monitoring_service.resolve_thresholds(
            {"JOIN_THRESHOLD": "300", "LEAVE_THRESHOLD": "500"}
        )
        assert (join, leave) == (300, 500)

    def test_inverted_band_is_rejected(self):
        """LEAVE below JOIN leaves no hysteresis, so every joined channel would
        be instantly leave-eligible -- thrashing chat once per poll."""
        with pytest.raises(ValueError, match="must be >= JOIN_THRESHOLD"):
            stream_monitoring_service.resolve_thresholds(
                {"JOIN_THRESHOLD": "100", "LEAVE_THRESHOLD": "50"}
            )

    def test_equal_thresholds_are_allowed(self):
        """A zero-width band is degenerate but not incoherent -- it is the
        no-hysteresis case, and the operator may want it while ramping."""
        assert stream_monitoring_service.resolve_thresholds(
            {"JOIN_THRESHOLD": "50", "LEAVE_THRESHOLD": "50"}
        ) == (50, 50)

    def test_zero_join_threshold_is_rejected(self):
        """Monitoring nothing is a misconfiguration, not a valid state."""
        with pytest.raises(ValueError, match="JOIN_THRESHOLD must be >= 1"):
            stream_monitoring_service.resolve_thresholds(
                {"JOIN_THRESHOLD": "0", "LEAVE_THRESHOLD": "30"}
            )

    def test_module_defaults_match_shipped_values(self):
        """The imported module still ships 15/30 for anyone not setting env."""
        assert stream_monitoring_service.JOIN_THRESHOLD == 15
        assert stream_monitoring_service.LEAVE_THRESHOLD == 30


class TestFetchBudget:
    """Tests for the paginated fetch budget.

    Regression guard: fetch_count was previously min(..., 100), which silently
    capped the monitored set at 100 however high LEAVE_THRESHOLD was set.
    """

    def test_single_page_keeps_the_original_timeout(self):
        """At the shipped 15/30 the behaviour is unchanged: one page, 10s."""
        pages, timeout = stream_monitoring_service.fetch_budget(50)
        assert pages == 1
        assert timeout == stream_monitoring_service.GET_STREAMS_TIMEOUT_SECONDS

    def test_fetch_count_above_100_needs_multiple_pages(self):
        """Helix caps `first` at 100, so a larger set must paginate."""
        pages, _ = stream_monitoring_service.fetch_budget(520)
        assert pages == 6

    def test_timeout_scales_with_pages(self):
        """A larger threshold must not trip a bound sized for a single page."""
        _, one = stream_monitoring_service.fetch_budget(50)
        _, six = stream_monitoring_service.fetch_budget(520)
        assert six > one

    def test_timeout_never_reaches_the_poll_interval(self):
        """A stalled fetch must not eat several poll cycles at any threshold."""
        for fetch_count in (50, 520, 2020, 100_000):
            _, timeout = stream_monitoring_service.fetch_budget(fetch_count)
            assert timeout < stream_monitoring_service.POLL_INTERVAL_SECONDS

    def test_capped_timeout_still_far_exceeds_measured_cost(self):
        """The cap must stay a wide margin over the ~0.1s-per-page measurement,
        or a healthy large fetch would start timing out."""
        pages, timeout = stream_monitoring_service.fetch_budget(2020)
        assert pages == 21
        assert timeout >= pages * 0.1 * 10


def seed_desired(fake_redis, logins_with_ids):
    """Write a desired set the way the poller writes it, for reconciler tests."""
    fake_redis.zsets[DESIRED_KEY] = {
        login: float(rank) for rank, (login, _) in enumerate(logins_with_ids, 1)
    }
    fake_redis.hashes[DESIRED_IDS_KEY] = {
        login: str(broadcaster_id) for login, broadcaster_id in logins_with_ids
    }
    previous = int(fake_redis.strings.get(DESIRED_GENERATION_KEY, 0))
    fake_redis.strings[DESIRED_GENERATION_KEY] = str(previous + 1)


def make_reconciler(transport, fake_redis, refusal_store=None, **config_overrides):
    """A reconciler with test-speed timings: no real backoff, no idle wait."""
    settings = {
        "concurrency": 10,
        "idle_timeout_seconds": 0.01,
        "rate_limit_backoff_seconds": 0.0,
        "max_retry_rounds": 20,
    }
    settings.update(config_overrides)
    return Reconciler(
        transport=transport,
        redis_client=fake_redis,
        config=ReconcilerConfig(**settings),
        refusal_store=refusal_store,
    )


def counter_value(reason):
    """Read one labelled Counter sample, for before/after deltas."""
    return reconciler_module.subscription_create_failures_total.labels(
        reason=reason
    )._value.get()


class TestDesiredSetHysteresis:
    """T011 / FR-011 -- the hysteresis band survives the move.

    The band used to live in the join loop, where `joined_channels` was the
    state. It now lives in the desired-set computation, where the previous
    desired set is the state. The behaviour must not change: entry at top
    JOIN_THRESHOLD, exit only below top LEAVE_THRESHOLD, and the 16-30 band
    retained in between.
    """

    @staticmethod
    def _legacy_joined_after_poll(ranked, joined, join_threshold, leave_threshold):
        """What the old IRC join loop settled on, as set algebra.

        Lifted straight from the old code path: join everything newly in the
        top JOIN_THRESHOLD, leave everything joined that fell out of the top
        LEAVE_THRESHOLD, keep the rest.
        """
        top_join = {login for rank, login in enumerate(ranked, 1) if rank <= join_threshold}
        top_leave = {login for rank, login in enumerate(ranked, 1) if rank <= leave_threshold}
        to_join = top_join - joined
        to_leave = joined - top_leave
        return (joined | to_join) - to_leave

    def test_login_enters_on_reaching_the_join_threshold(self):
        """Top JOIN_THRESHOLD is the entry condition."""
        ranked = [f"s{i}" for i in range(1, 31)]

        desired = compute_desired_set(ranked, previous_desired=set(), join_threshold=15,
                                      leave_threshold=30)

        assert "s1" in desired
        assert "s15" in desired
        assert desired["s15"] == 15

    def test_login_in_the_band_cannot_enter(self):
        """Rank 16-30 is a RETAINING band, not an entry one. A newcomer there
        must stay out, or the band would quietly become the join threshold."""
        ranked = [f"s{i}" for i in range(1, 31)]

        desired = compute_desired_set(ranked, previous_desired=set(), join_threshold=15,
                                      leave_threshold=30)

        assert "s16" not in desired
        assert "s30" not in desired
        assert len(desired) == 15

    def test_band_member_is_retained(self):
        """A login already wanted stays through ranks 16-30. This is the whole
        point of hysteresis: it protects the Flink baseline the channel built."""
        ranked = ["newtop"] + [f"s{i}" for i in range(1, 30)]
        # "s15" now sits at rank 16, inside the retained band.
        assert ranked[15] == "s15"

        desired = compute_desired_set(ranked, previous_desired={"s15"}, join_threshold=15,
                                      leave_threshold=30)

        assert "s15" in desired
        assert desired["s15"] == 16

    def test_login_leaves_only_after_exiting_the_leave_threshold(self):
        """Falling past rank 30 is the exit condition, and nothing sooner."""
        ranked = [f"s{i}" for i in range(1, 31)] + ["faller"]

        retained = compute_desired_set(ranked[:30] + ["x"], previous_desired={"s30"},
                                       join_threshold=15, leave_threshold=30)
        dropped = compute_desired_set(["newcomer"] + ranked[:30], previous_desired={"s30"},
                                      join_threshold=15, leave_threshold=30)

        assert "s30" in retained, "rank 30 is still inside the band"
        assert "s30" not in dropped, "rank 31 is outside the band"

    def test_login_absent_from_the_ranking_is_dropped(self):
        """A streamer that went offline leaves the set, band or no band."""
        desired = compute_desired_set(["a", "b"], previous_desired={"a", "b", "gone"},
                                      join_threshold=15, leave_threshold=30)

        assert "gone" not in desired

    def test_ranking_longer_than_the_leave_threshold_is_truncated(self):
        """Nothing past LEAVE_THRESHOLD can be wanted, however long the list."""
        ranked = [f"s{i}" for i in range(1, 51)]

        desired = compute_desired_set(ranked, previous_desired=set(ranked), join_threshold=15,
                                      leave_threshold=30)

        assert max(desired.values()) == 30
        assert len(desired) == 30

    def test_matches_the_old_join_loop_on_random_rankings(self):
        """Byte-equivalence to today, checked against the old set algebra over
        many random rank shuffles rather than a handful of chosen cases."""
        import random as _random

        rng = _random.Random(20260828)
        population = [f"s{i}" for i in range(60)]

        for _ in range(300):
            join_threshold = rng.randint(1, 20)
            leave_threshold = join_threshold + rng.randint(0, 20)
            ranked = rng.sample(population, leave_threshold)
            previous = set(rng.sample(population, rng.randint(0, 30)))

            new = set(compute_desired_set(ranked, previous, join_threshold, leave_threshold))
            legacy = self._legacy_joined_after_poll(ranked, previous, join_threshold,
                                                    leave_threshold)

            assert new == legacy, (
                f"diverged at join={join_threshold} leave={leave_threshold}: "
                f"{new ^ legacy}"
            )


class TestPollWritesIntentOnly:
    """T008 / T010 -- FR-002 and FR-003.

    The poll ranks, writes intent, and returns. It makes no chat connection and
    no subscription, and its cost does not follow the size of the change.
    """

    def test_poll_writes_the_desired_set_and_bumps_the_generation(self):
        fake_redis = FakeRedis()
        logins = [f"s{i}" for i in range(1, 31)]
        service = make_poller(logins, fake_redis)

        asyncio.run(service.poll_top_streams())

        assert fake_redis.zrange(DESIRED_KEY, 0, -1) == logins[:15]
        assert fake_redis.hgetall(DESIRED_IDS_KEY) == {
            login: str(broadcaster_id_for(logins, login)) for login in logins[:15]
        }
        assert fake_redis.get(DESIRED_GENERATION_KEY) == "1"

    def test_desired_set_is_scored_by_rank_best_first(self):
        """The reconciler reads the set in score order to work highest rank
        first, so the score must be the rank and 1 must be the top."""
        fake_redis = FakeRedis()
        logins = [f"s{i}" for i in range(1, 31)]
        service = make_poller(logins, fake_redis)

        asyncio.run(service.poll_top_streams())

        assert fake_redis.zsets[DESIRED_KEY]["s1"] == 1.0
        assert fake_redis.zsets[DESIRED_KEY]["s15"] == 15.0

    def test_poll_makes_no_chat_connection_and_no_subscription(self):
        """FR-002. The poll path must not touch the transport at all."""
        fake_redis = FakeRedis()
        transport = StubTransport()
        reconciler = make_reconciler(transport, fake_redis)
        logins = [f"s{i}" for i in range(1, 31)]
        service = make_poller(logins, fake_redis, reconciler=reconciler)

        asyncio.run(service.poll_top_streams())

        assert transport.create_calls == []
        assert transport.delete_calls == []

    def test_poll_signals_the_reconciler(self):
        """The in-process fast path: the poller nudges the loop after writing."""
        fake_redis = FakeRedis()
        reconciler = make_reconciler(StubTransport(), fake_redis)
        service = make_poller(["a", "b"], fake_redis, reconciler=reconciler)

        asyncio.run(service.poll_top_streams())

        assert reconciler._wake.is_set()

    def test_poll_duration_does_not_scale_with_change_size(self):
        """FR-003. Both polls rank 500 streams. The first changes all 500, the
        second changes nothing. The work must be the same either way -- that is
        what "the poller writes intent" has to mean in practice.

        The operation count is the real assertion; wall clock is a loose
        backstop, because a per-channel network loop would blow past it."""
        logins = [f"s{i}" for i in range(1, 501)]
        fake_redis = FakeRedis()

        with patch.object(stream_monitoring_service, "JOIN_THRESHOLD", 500), \
             patch.object(stream_monitoring_service, "LEAVE_THRESHOLD", 500):
            service = make_poller(logins, fake_redis)

            fake_redis.calls.clear()
            start = time.perf_counter()
            asyncio.run(service.poll_top_streams())
            full_change_seconds = time.perf_counter() - start
            full_change_calls = list(fake_redis.calls)

            # Same ranking again: the desired set is already exactly right.
            service.twitch = FakeTwitch(
                [make_stream(login, 1000 + i) for i, login in enumerate(logins)]
            )
            fake_redis.calls.clear()
            start = time.perf_counter()
            asyncio.run(service.poll_top_streams())
            no_change_seconds = time.perf_counter() - start
            no_change_calls = list(fake_redis.calls)

        assert len(fake_redis.zrange(DESIRED_KEY, 0, -1)) == 500
        assert len(full_change_calls) == len(no_change_calls), (
            "the poll did more work when more changed -- something in it "
            "still scales with the change, not the set"
        )
        assert full_change_calls.count("pipeline.execute") == 1
        assert no_change_calls.count("pipeline.execute") == 1
        assert full_change_seconds < no_change_seconds * 5 + 0.5, (
            f"500-change poll took {full_change_seconds:.4f}s against "
            f"{no_change_seconds:.4f}s for a 0-change poll"
        )

    def test_offline_lifecycle_events_carry_the_real_broadcaster_id(self):
        """A login only leaves the desired set by dropping out of this poll's
        ranking, so this poll never has an id for it. That made every offline
        event publish `broadcaster_id: 0` and key every one of them to
        partition b"0". Before Phase 3 an instance dict carried ids across
        polls; it went with the IRC client. The id map the last poll wrote to
        Redis has it."""
        fake_redis = FakeRedis()
        logins = [f"s{i}" for i in range(1, 31)]
        service = make_poller(logins, fake_redis)
        asyncio.run(service.poll_top_streams())
        expected_id = broadcaster_id_for(logins, "s15")

        # s15 leaves the ranking, and its online key has expired at
        # REDIS_STREAMER_TTL -- which is what makes the poll call it offline.
        fake_redis.delete("streamer:online:s15")
        remaining = [login for login in logins if login != "s15"]
        later = make_poller(remaining, fake_redis)
        asyncio.run(later.poll_top_streams())

        offline = [
            call for call in later._publish_lifecycle_event.call_args_list
            if call.args[0] == "offline"
        ]
        assert [call.args[2] for call in offline] == ["s15"]
        assert offline[0].args[1] == expected_id, (
            "the offline event published a placeholder id, so every offline "
            "event keys to the same Kafka partition"
        )

    def test_hysteresis_survives_across_polls_through_redis(self):
        """T011 end to end: the band is read back from Redis, so it survives a
        restart instead of collapsing to the join threshold."""
        fake_redis = FakeRedis()
        logins = [f"s{i}" for i in range(1, 31)]
        service = make_poller(logins, fake_redis)
        asyncio.run(service.poll_top_streams())
        assert fake_redis.zrange(DESIRED_KEY, 0, -1) == logins[:15]

        # s15 slips to rank 16 -- inside the retained band, so it stays. A
        # brand-new service instance reads the band from Redis, not memory.
        reordered = ["newcomer"] + logins[:14] + ["s15"] + logins[15:]
        restarted = make_poller(reordered, fake_redis)
        asyncio.run(restarted.poll_top_streams())

        wanted = fake_redis.zrange(DESIRED_KEY, 0, -1)
        assert "s15" in wanted, "the retained band did not survive the poll"
        assert "newcomer" in wanted
        assert "s16" not in wanted, "a newcomer entered through the band"

    def test_streamer_falling_out_of_the_band_is_dropped(self):
        fake_redis = FakeRedis()
        logins = [f"s{i}" for i in range(1, 31)]
        asyncio.run(make_poller(logins, fake_redis).poll_top_streams())

        # s1 disappears from the ranking altogether.
        asyncio.run(make_poller(logins[1:], fake_redis).poll_top_streams())

        assert "s1" not in fake_redis.zrange(DESIRED_KEY, 0, -1)


class TestReconcilerDiff:
    """T014a -- the diff, adoption, revocation, and mid-pass changes."""

    def test_creates_everything_missing(self):
        fake_redis = FakeRedis()
        seed_desired(fake_redis, [("a", 1), ("b", 2), ("c", 3)])
        transport = StubTransport()
        reconciler = make_reconciler(transport, fake_redis)

        asyncio.run(reconciler.reconcile_once())

        assert sorted(transport.subscriptions) == [1, 2, 3]
        assert reconciler.subscription_count == 3

    def test_works_highest_rank_first(self):
        """Rank order matters at cold start: the busiest channels come up
        first, so coverage is useful before the ramp finishes."""
        fake_redis = FakeRedis()
        seed_desired(fake_redis, [("top", 1), ("middle", 2), ("bottom", 3)])
        transport = StubTransport(latency_seconds=0.001)
        reconciler = make_reconciler(transport, fake_redis, concurrency=1)

        asyncio.run(reconciler.reconcile_once())

        assert transport.create_calls == [1, 2, 3]

    def test_drops_what_is_no_longer_wanted(self):
        fake_redis = FakeRedis()
        seed_desired(fake_redis, [("a", 1), ("b", 2)])
        transport = StubTransport()
        reconciler = make_reconciler(transport, fake_redis)
        asyncio.run(reconciler.reconcile_once())

        seed_desired(fake_redis, [("a", 1)])
        asyncio.run(reconciler.reconcile_once())

        assert sorted(transport.subscriptions) == [1]
        assert len(transport.delete_calls) == 1

    def test_adopts_an_existing_subscription_instead_of_recreating_it(self):
        """FR-005. A restart must not duplicate what is already there."""
        fake_redis = FakeRedis()
        seed_desired(fake_redis, [("a", 1), ("b", 2)])
        transport = StubTransport()
        asyncio.run(transport.create(1))  # already subscribed before we start
        transport.create_calls.clear()
        reconciler = make_reconciler(transport, fake_redis)

        asyncio.run(reconciler.reconcile_once())

        assert transport.create_calls == [2], "channel 1 was re-created, not adopted"
        assert len(transport.subscriptions) == 2

    def test_revoked_subscription_is_recreated(self):
        """T014. A revoked subscription is not live, so it counts as absent."""
        fake_redis = FakeRedis()
        seed_desired(fake_redis, [("a", 1)])
        transport = StubTransport()
        asyncio.run(transport.create(1))
        transport.revoke(1)
        transport.create_calls.clear()
        reconciler = make_reconciler(transport, fake_redis)

        asyncio.run(reconciler.reconcile_once())

        assert transport.create_calls == [1]
        assert transport.statuses[1] == "enabled"

    def test_channel_that_leaves_mid_pass_is_not_created(self):
        """T014. A poll landing during a long cold ramp must be picked up
        through the generation counter, not acted on a pass too late."""
        fake_redis = FakeRedis()
        seed_desired(fake_redis, [("a", 1), ("b", 2), ("c", 3)])

        class ShrinkingTransport(StubTransport):
            """The poller lands right after the first create and shrinks the set."""

            reconciler = None

            async def create(self, broadcaster_id):
                result = await super().create(broadcaster_id)
                if len(self.create_calls) == 1:
                    seed_desired(fake_redis, [("a", 1)])
                    self.reconciler.notify_desired_changed()
                return result

        transport = ShrinkingTransport(latency_seconds=0.001)
        reconciler = make_reconciler(transport, fake_redis, concurrency=1)
        transport.reconciler = reconciler

        asyncio.run(reconciler.reconcile_once())

        assert transport.create_calls == [1], "kept creating channels nobody wants"
        assert reconciler.subscription_count == 1

    def test_converges_from_empty_partial_and_drifted(self):
        """FR-005 -- the same pass has to work from any starting state."""
        fake_redis = FakeRedis()
        seed_desired(fake_redis, [("a", 1), ("b", 2), ("c", 3)])
        transport = StubTransport()
        reconciler = make_reconciler(transport, fake_redis)

        asyncio.run(reconciler.reconcile_once())            # from empty
        assert sorted(transport.subscriptions) == [1, 2, 3]

        transport.revoke(2)                                  # drift
        transport.subscriptions.pop(3)                       # socket death
        reconciler._adoption_complete = False                # forces re-enumeration
        asyncio.run(reconciler.reconcile_once())

        assert sorted(transport.subscriptions) == [1, 2, 3]
        assert all(status == "enabled" for status in transport.statuses.values())

    def test_desired_login_without_an_id_is_skipped_not_crashed(self):
        """The poller writes both keys in one transaction, so this should not
        happen. If it ever does, skip the login rather than guess an id."""
        fake_redis = FakeRedis()
        seed_desired(fake_redis, [("a", 1), ("b", 2)])
        del fake_redis.hashes[DESIRED_IDS_KEY]["b"]
        transport = StubTransport()
        reconciler = make_reconciler(transport, fake_redis)

        asyncio.run(reconciler.reconcile_once())

        assert sorted(transport.subscriptions) == [1]


class TestReconcilerRateLimit:
    """D2 -- the 429 retry loop is load-bearing past about 400 channels."""

    def test_rate_limited_channels_are_retried_never_dropped(self):
        fake_redis = FakeRedis()
        channels = [(f"s{i}", i) for i in range(1, 201)]
        seed_desired(fake_redis, channels)
        # A burst budget like the measured one: creates succeed until it runs
        # out, then 429 until the backoff refills it.
        transport = StubTransport(burst_budget=50, budget_refill_seconds=0.0)
        reconciler = make_reconciler(transport, fake_redis)
        before = counter_value("rate_limited")

        asyncio.run(reconciler.reconcile_once())

        assert len(transport.subscriptions) == 200, "channels were dropped on a 429"
        assert counter_value("rate_limited") > before, "429s were not counted"

    def test_refusal_is_counted_and_does_not_block_the_rest(self):
        """About 1.5% of channels refuse. One refusal must not stall the ramp."""
        fake_redis = FakeRedis()
        seed_desired(fake_redis, [("a", 1), ("b", 2), ("c", 3)])
        transport = StubTransport(refuse={2})
        reconciler = make_reconciler(transport, fake_redis)
        before = counter_value("refused")

        asyncio.run(reconciler.reconcile_once())

        assert sorted(transport.subscriptions) == [1, 3]
        assert counter_value("refused") == before + 1

    def test_backoff_honours_the_retry_after_the_transport_offers(self):
        reconciler = make_reconciler(StubTransport(), FakeRedis())

        backoff = reconciler._backoff_for([(1, RateLimitedError(retry_after=7.0))])

        assert 7.0 <= backoff <= 7.7


class TestReconcilerAdoption:
    """T013 and T014b -- rebuilding the actual set, including a partial read."""

    def test_partial_enumeration_keeps_what_it_saw(self):
        """NFR-003. Pagination raising part way must not lose the entries
        already read, and must not present the unread ones as absent."""
        fake_redis = FakeRedis()
        seed_desired(fake_redis, [])
        transport = StubTransport()
        for broadcaster_id in (1, 2, 3, 4):
            asyncio.run(transport.create(broadcaster_id))
        transport.list_fails_after = 2
        reconciler = make_reconciler(transport, fake_redis)

        asyncio.run(reconciler.reconcile_once())

        assert reconciler.subscription_count == 2, "the partial read was thrown away"
        assert reconciler._adoption_complete is False

    def test_a_loss_during_enumeration_is_not_swallowed_by_the_completion(self):
        """`transport.list()` has awaits in it and the pool's supervisor runs
        on the same loop, so a socket can die *while* the actual set is being
        rebuilt. Marking the adoption complete afterwards threw that signal
        away: the walk's snapshot still held the dead session, so its channels
        stayed recorded as covered while nothing delivered for them, and
        nothing re-enumerated until some later, unrelated loss."""
        fake_redis = FakeRedis()
        seed_desired(fake_redis, [])
        transport = StubTransport()
        for broadcaster_id in (1, 2, 3, 4):
            asyncio.run(transport.create(broadcaster_id))
        reconciler = make_reconciler(transport, fake_redis)

        # A connection dies part way through the walk, exactly as the pool's
        # supervisor would report it.
        original_list = transport.list

        def list_with_a_loss_midway():
            async def wrapped():
                index = 0
                async for subscription in original_list():
                    if index == 1:
                        reconciler.invalidate_actual_set()
                    index += 1
                    yield subscription
            return wrapped()

        transport.list = list_with_a_loss_midway
        asyncio.run(reconciler.reconcile_once())

        assert reconciler._adoption_complete is False, (
            "the completion overwrote the invalidation, so nothing will "
            "re-enumerate and the lost channels stay recorded as covered"
        )

    def test_the_actual_set_is_re_enumerated_periodically(self):
        """Adoption used to run once and then never again unless the pool
        observed a dead socket or a revocation. A subscription lost by any
        route the pool cannot see -- the library's `_resubscribe()` failing
        part way through a reconnect is the known one -- was therefore
        permanent, and `eventsub_subscription_count` went on counting it, so
        the FR-012 alert could not fire."""
        fake_redis = FakeRedis()
        seed_desired(fake_redis, [("a", 1)])
        transport = StubTransport()
        reconciler = make_reconciler(transport, fake_redis, readopt_interval_seconds=3600)
        asyncio.run(reconciler.reconcile_once())
        assert reconciler._adoption_complete is True

        # Twitch loses the subscription behind the pool's back.
        transport.subscriptions.clear()
        transport.statuses.clear()

        # Not due yet: the stale view survives, which is the cheap steady state.
        asyncio.run(reconciler.reconcile_once())

        # Due now.
        reconciler._last_adopt = float("-inf")
        asyncio.run(reconciler.reconcile_once())
        assert 1 in reconciler._actual, "the channel was not re-created"
        assert transport.create_calls.count(1) == 2, (
            "the re-enumeration did not notice the subscription had gone"
        )

    def test_a_failed_enumeration_backs_off(self):
        """A walk that raised will probably raise again in 5 s, and retrying it
        every pass is a full multi-page Helix enumeration ~12 times a minute on
        the token the clip job shares.

        Stamping `_last_adopt` does NOT bound this, though an earlier comment
        here claimed it did: a partial walk also clears `_adoption_complete`,
        and `reconcile_once` re-adopts on `not complete OR readopt_due`, so the
        first disjunct fires however recently `_last_adopt` was stamped. The
        earlier version of this test could not tell the difference -- it only
        checked `_readopt_due()` after a SUCCESSFUL recovery walk, which stamps
        `_last_adopt` either way.
        """
        fake_redis = FakeRedis()
        seed_desired(fake_redis, [("a", 1), ("b", 2), ("c", 3), ("d", 4)])
        transport = StubTransport()
        for broadcaster_id in (1, 2, 3, 4):
            asyncio.run(transport.create(broadcaster_id))
        reconciler = make_reconciler(
            transport, fake_redis, readopt_interval_seconds=3600, adopt_retry_seconds=30
        )
        asyncio.run(reconciler.reconcile_once())
        assert reconciler._adoption_complete is True

        # The periodic re-check comes due and Twitch fails mid-pagination.
        reconciler._last_adopt = float("-inf")
        transport.list_fails_after = 2
        asyncio.run(reconciler.reconcile_once())
        assert reconciler._adoption_complete is False
        walks_after_failure = len(transport.list_calls)

        # The next passes must NOT re-enumerate, even though the view is known
        # to be incomplete. That is the whole point of the backoff.
        asyncio.run(reconciler.reconcile_once())
        asyncio.run(reconciler.reconcile_once())
        assert len(transport.list_calls) == walks_after_failure, (
            "a failed enumeration is being retried on every pass"
        )

        # And once the window passes it tries again and recovers.
        reconciler._adopt_retry_after = float("-inf")
        transport.list_fails_after = None
        asyncio.run(reconciler.reconcile_once())
        assert reconciler._adoption_complete is True
        assert reconciler._readopt_due() is False

    def test_a_socket_loss_is_not_delayed_by_the_enumeration_backoff(self):
        """The two failures are positively correlated -- one network blip both
        fails a walk and kills a socket -- so this pairing is likely, not
        exotic. While the backoff held, `_actual` still carried the dead
        socket's ids, so its channels never entered `to_create` and stayed dark
        for the full window.

        The earlier version of this test set no backoff at all, so it passed
        whether or not the loss path was gated. This one arms it first.
        """
        fake_redis = FakeRedis()
        seed_desired(fake_redis, [("a", 1)])
        transport = StubTransport()
        reconciler = make_reconciler(transport, fake_redis, adopt_retry_seconds=30)
        asyncio.run(reconciler.reconcile_once())
        walks = len(transport.list_calls)

        # A failed walk arms the backoff, then the socket dies a second later.
        reconciler._adopt_retry_after = time.monotonic() + 30
        reconciler.invalidate_actual_set()
        asyncio.run(reconciler.reconcile_once())

        assert len(transport.list_calls) > walks, (
            "a socket loss waited out the failed-enumeration backoff, so up to "
            "300 channels stay dark for the whole window"
        )
        assert reconciler._adoption_complete is True

    def test_partial_enumeration_never_deletes(self):
        """The dangerous move is deleting on an incomplete picture: an unseen
        subscription is not an unwanted one."""
        fake_redis = FakeRedis()
        seed_desired(fake_redis, [])  # nothing is wanted -- everything looks droppable
        transport = StubTransport()
        for broadcaster_id in (1, 2, 3, 4):
            asyncio.run(transport.create(broadcaster_id))
        transport.list_fails_after = 2
        reconciler = make_reconciler(transport, fake_redis)

        asyncio.run(reconciler.reconcile_once())

        assert transport.delete_calls == [], "deleted on an incomplete view"
        assert len(transport.subscriptions) == 4

    def test_enumeration_is_retried_until_one_succeeds(self):
        """Once the view is whole, the held-back drops go through."""
        fake_redis = FakeRedis()
        seed_desired(fake_redis, [])
        transport = StubTransport()
        for broadcaster_id in (1, 2, 3, 4):
            asyncio.run(transport.create(broadcaster_id))
        transport.list_fails_after = 2
        # No failure backoff here: this test is about the retry converging,
        # and `test_a_failed_enumeration_backs_off` covers the pacing.
        reconciler = make_reconciler(transport, fake_redis, adopt_retry_seconds=0)
        asyncio.run(reconciler.reconcile_once())

        transport.list_fails_after = None
        asyncio.run(reconciler.reconcile_once())

        assert reconciler._adoption_complete is True
        assert transport.subscriptions == {}
        assert len(transport.delete_calls) == 4

    def test_only_enabled_subscriptions_are_adopted(self):
        """A lingering `websocket_disconnected` entry is not a live one."""
        fake_redis = FakeRedis()
        seed_desired(fake_redis, [])
        transport = StubTransport()
        asyncio.run(transport.create(1))
        asyncio.run(transport.create(2))
        transport.statuses[2] = "websocket_disconnected"
        reconciler = make_reconciler(transport, fake_redis)

        asyncio.run(reconciler._adopt())

        assert reconciler.subscription_count == 1


class TestReconcilerResilience:
    """T014c -- NFR-002. A Redis fault is a skipped pass, not a dead service."""

    def test_redis_failure_skips_the_pass_without_crashing(self):
        fake_redis = FakeRedis()
        seed_desired(fake_redis, [("a", 1)])
        transport = StubTransport()
        reconciler = make_reconciler(transport, fake_redis)
        fake_redis.fail_on = {"zrange"}

        asyncio.run(reconciler.reconcile_once())  # must not raise

        assert transport.create_calls == []

    def test_redis_failure_drops_no_live_subscription(self):
        """The failure path must never look like "nothing is wanted"."""
        fake_redis = FakeRedis()
        seed_desired(fake_redis, [("a", 1), ("b", 2)])
        transport = StubTransport()
        reconciler = make_reconciler(transport, fake_redis)
        asyncio.run(reconciler.reconcile_once())
        assert len(transport.subscriptions) == 2

        fake_redis.fail_on = {"zrange"}
        asyncio.run(reconciler.reconcile_once())

        assert len(transport.subscriptions) == 2, "a Redis fault dropped live work"
        assert transport.delete_calls == []

    def test_the_next_pass_recovers(self):
        fake_redis = FakeRedis()
        seed_desired(fake_redis, [("a", 1)])
        transport = StubTransport()
        reconciler = make_reconciler(transport, fake_redis)
        fake_redis.fail_on = {"zrange"}
        asyncio.run(reconciler.reconcile_once())

        fake_redis.fail_on = set()
        asyncio.run(reconciler.reconcile_once())

        assert sorted(transport.subscriptions) == [1]

    def test_a_transport_failure_does_not_stop_the_other_channels(self):
        fake_redis = FakeRedis()
        seed_desired(fake_redis, [("a", 1), ("b", 2), ("c", 3)])

        class FlakyTransport(StubTransport):
            async def create(self, broadcaster_id):
                if broadcaster_id == 2:
                    raise TransportError("boom")
                return await super().create(broadcaster_id)

        transport = FlakyTransport()
        reconciler = make_reconciler(transport, fake_redis)

        asyncio.run(reconciler.reconcile_once())

        assert sorted(transport.subscriptions) == [1, 3]

    def test_the_loop_keeps_running_across_a_failing_pass(self):
        """The asyncio task must outlive a bad pass, or a stalled reconciler
        would look exactly like a healthy one."""
        fake_redis = FakeRedis()
        seed_desired(fake_redis, [("a", 1)])
        transport = StubTransport()
        reconciler = make_reconciler(transport, fake_redis, idle_timeout_seconds=0.01)
        fake_redis.fail_on = {"zrange"}

        async def run_briefly():
            task = asyncio.create_task(reconciler.run())
            await asyncio.sleep(0.05)
            fake_redis.fail_on = set()
            await asyncio.sleep(0.05)
            reconciler.stop()
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        asyncio.run(run_briefly())

        assert sorted(transport.subscriptions) == [1], "the loop did not recover"


class TestReconcilerConcurrencyBound:
    """T014d -- NFR-001. No task or thread per channel."""

    def test_task_count_stays_bounded_at_500_channels(self):
        fake_redis = FakeRedis()
        seed_desired(fake_redis, [(f"s{i}", i) for i in range(1, 501)])

        class TaskCountingTransport(StubTransport):
            max_tasks = 0

            async def create(self, broadcaster_id):
                TaskCountingTransport.max_tasks = max(
                    TaskCountingTransport.max_tasks, len(asyncio.all_tasks())
                )
                return await super().create(broadcaster_id)

        transport = TaskCountingTransport(latency_seconds=0.0005)
        reconciler = make_reconciler(transport, fake_redis, concurrency=10)

        asyncio.run(reconciler.reconcile_once())

        assert len(transport.subscriptions) == 500
        assert TaskCountingTransport.max_tasks <= 10 + 5, (
            f"{TaskCountingTransport.max_tasks} tasks alive for 500 channels -- "
            "the reconciler is spawning per-channel work"
        )

    def test_worker_pool_never_exceeds_the_configured_concurrency(self):
        fake_redis = FakeRedis()
        seed_desired(fake_redis, [(f"s{i}", i) for i in range(1, 101)])

        class ConcurrencyProbe(StubTransport):
            def __init__(self, **kw):
                super().__init__(**kw)
                self.in_flight = 0
                self.peak = 0

            async def create(self, broadcaster_id):
                self.in_flight += 1
                self.peak = max(self.peak, self.in_flight)
                try:
                    return await super().create(broadcaster_id)
                finally:
                    self.in_flight -= 1

        transport = ConcurrencyProbe(latency_seconds=0.001)
        reconciler = make_reconciler(transport, fake_redis, concurrency=4)

        asyncio.run(reconciler.reconcile_once())

        assert transport.peak <= 4


class TestRefusalStoreFaults:
    """A failed refusal READ must not read as "nothing is refused"."""

    def test_a_database_fault_reaches_the_caller(self):
        """Returning {} for a failed read is a wrong answer, not a degraded
        one, and it silently disabled `_drop_refused`'s own handling -- both
        its except branch and its "Refusal cache unavailable" log were
        unreachable for the real store. The symptom was every refused channel
        being retried every pass, one POST each, with nothing in the log."""
        pool = MagicMock()
        pool.getconn.side_effect = RuntimeError("postgres is away")
        store = reconciler_module.PostgresRefusalStore(pool)

        with pytest.raises(RuntimeError):
            store.refusals([1, 2, 3])

    def test_a_write_fault_is_still_swallowed(self):
        """The writes keep their old behaviour: a refusal that fails to record
        is re-learned next pass, which is harmless."""
        pool = MagicMock()
        pool.getconn.side_effect = RuntimeError("postgres is away")
        store = reconciler_module.PostgresRefusalStore(pool)

        store.mark_refused(1)
        store.clear_refusal(1)

    def test_the_reconciler_attempts_every_channel_when_the_store_faults(self):
        """And the caller's handler, now reachable, does the safe thing."""
        fake_redis = FakeRedis()
        seed_desired(fake_redis, [("a", 1), ("b", 2)])
        store = MagicMock()
        store.refusals.side_effect = RuntimeError("postgres is away")
        transport = StubTransport()
        reconciler = make_reconciler(transport, fake_redis, refusal_store=store)

        asyncio.run(reconciler.reconcile_once())

        assert sorted(transport.create_calls) == [1, 2], (
            "a store fault stopped the reconciler subscribing"
        )


class TestReconcilerMetrics:
    """T015 and T016 -- FR-012. A stalled reconciler must be visible while the
    polls keep succeeding (US2 acceptance scenario 2)."""

    def test_subscription_count_tracks_the_actual_set(self):
        fake_redis = FakeRedis()
        seed_desired(fake_redis, [("a", 1), ("b", 2)])
        reconciler = make_reconciler(StubTransport(), fake_redis)

        asyncio.run(reconciler.reconcile_once())

        assert reconciler_module.eventsub_subscription_count._value.get() == 2

    def test_the_subscription_count_drops_when_a_socket_takes_its_channels(self):
        """The FR-012 alert is the DIP, and there was no dip to alert on.

        The gauge was written once, at the end of a pass. A socket loss left
        `_actual` untouched, so the next pass re-created everything and set the
        gauge from the old value back to the same value -- no scrape in between
        could ever see it move. And if that pass's enumeration failed, which a
        blip that kills a socket is exactly what does, `_adopt` merged the
        stale entries back and the healthy-looking count survived pass after
        pass while those channels were dark.
        """
        fake_redis = FakeRedis()
        seed_desired(fake_redis, [(f"c{i}", i) for i in range(1, 6)])
        reconciler = make_reconciler(StubTransport(), fake_redis)

        asyncio.run(reconciler.reconcile_once())
        assert reconciler_module.eventsub_subscription_count._value.get() == 5

        # The socket went, and took three of them with it.
        reconciler.invalidate_actual_set(3)

        assert reconciler_module.eventsub_subscription_count._value.get() == 2, (
            "the gauge went on reporting subscriptions Twitch no longer has"
        )

    def test_the_subscription_count_climbs_during_a_ramp(self):
        """A cold start can run for tens of seconds, and a rate-limited one for
        minutes. The runbook tells the operator to check whether the count is
        still climbing before restarting anything, so it has to actually climb
        rather than appear all at once when the pass ends."""
        fake_redis = FakeRedis()
        seed_desired(fake_redis, [(f"c{i}", i) for i in range(1, 6)])
        seen = []

        class WatchedTransport(StubTransport):
            async def create(self, broadcaster_id):
                subscription_id = await super().create(broadcaster_id)
                seen.append(reconciler_module.eventsub_subscription_count._value.get())
                return subscription_id

        reconciler = make_reconciler(WatchedTransport(), fake_redis, concurrency=1)
        reconciler_module.eventsub_subscription_count.set(0)

        asyncio.run(reconciler.reconcile_once())

        assert seen == sorted(seen) and seen[-1] > seen[0], (
            f"the gauge did not move during the ramp (samples {seen})"
        )

    def test_repeated_losses_between_passes_accumulate(self):
        """Several revocations can land before the next enumeration. Deriving
        the dip from `len(_actual)` each time reported the SAME value for all
        of them -- the set does not shrink on a loss, because the transport
        says how many went, not which -- so three losses looked like one."""
        fake_redis = FakeRedis()
        seed_desired(fake_redis, [(f"c{i}", i) for i in range(1, 6)])
        reconciler = make_reconciler(StubTransport(), fake_redis)

        asyncio.run(reconciler.reconcile_once())
        for _ in range(3):
            reconciler.invalidate_actual_set(1)

        assert reconciler_module.eventsub_subscription_count._value.get() == 2

    def test_a_failed_walk_does_not_re_inflate_the_count(self):
        """`_adopt` merges what it saw into what it had when a walk fails, so
        the lost subscriptions are still in `_actual`. Publishing
        `len(_actual)` there would undo the dip on the very next pass and
        restore the healthy-looking count for as long as the walks keep
        failing -- which is the state this alert exists to catch."""
        fake_redis = FakeRedis()
        seed_desired(fake_redis, [(f"c{i}", i) for i in range(1, 6)])
        transport = StubTransport()
        reconciler = make_reconciler(transport, fake_redis)

        asyncio.run(reconciler.reconcile_once())
        reconciler.invalidate_actual_set(3)
        transport.list_fails_after = 0
        asyncio.run(reconciler.reconcile_once())

        assert reconciler_module.eventsub_subscription_count._value.get() == 2, (
            "a failed enumeration restored the count the loss had corrected"
        )
        # And a clean walk settles it exactly, once the failed walk's backoff
        # has expired.
        transport.list_fails_after = None
        reconciler._adopt_retry_after = float("-inf")
        asyncio.run(reconciler.reconcile_once())
        assert reconciler_module.eventsub_subscription_count._value.get() == 5

    def test_last_success_timestamp_advances_on_a_good_pass(self):
        fake_redis = FakeRedis()
        seed_desired(fake_redis, [("a", 1)])
        reconciler = make_reconciler(StubTransport(), fake_redis)

        asyncio.run(reconciler.reconcile_once())

        assert reconciler_module.reconcile_last_success_timestamp._value.get() > 0

    def test_last_success_timestamp_stalls_when_the_pass_fails(self):
        """This is the signal that separates "reconciler dead" from "poll
        dead". It must not move on a pass that could not read the intent."""
        fake_redis = FakeRedis()
        seed_desired(fake_redis, [("a", 1)])
        reconciler = make_reconciler(StubTransport(), fake_redis)
        asyncio.run(reconciler.reconcile_once())
        stalled_at = reconciler_module.reconcile_last_success_timestamp._value.get()

        fake_redis.fail_on = {"zrange"}
        asyncio.run(reconciler.reconcile_once())

        assert reconciler_module.reconcile_last_success_timestamp._value.get() == stalled_at

    def test_a_mid_pass_refresh_does_not_swallow_an_invalidation(self):
        """The two signals -- "the poller wrote a new set" and "a socket died"
        -- have their own events. Sharing one meant each fix for the other
        broke something: clearing it in the refresh swallowed the socket loss,
        and re-setting it there left it set for the rest of the pass, so every
        remaining channel re-read Redis three times over."""
        fake_redis = FakeRedis()
        seed_desired(fake_redis, [("a", 1)])
        reconciler = make_reconciler(StubTransport(), fake_redis)

        async def run():
            # Both signals land while a pass is in flight.
            reconciler.notify_desired_changed()
            reconciler.invalidate_actual_set()
            await reconciler._maybe_refresh_desired()

            assert reconciler._invalidated.is_set(), (
                "the desired-set refresh consumed the socket-loss wake-up"
            )
            # And the desired-set signal really is consumed, so the rest of the
            # pass does not re-read Redis per channel.
            assert not reconciler._wake.is_set()
            calls = len(fake_redis.calls)
            await reconciler._maybe_refresh_desired()
            assert len(fake_redis.calls) == calls, (
                "the refresh ran again for the next channel"
            )

        asyncio.run(run())

    def test_connection_occupancy_is_published(self):
        fake_redis = FakeRedis()
        seed_desired(fake_redis, [("a", 1), ("b", 2), ("c", 3)])
        reconciler = make_reconciler(StubTransport(connections=2), fake_redis)

        asyncio.run(reconciler.reconcile_once())

        total = sum(
            reconciler_module.eventsub_connection_occupancy.labels(connection=str(i))._value.get()
            for i in range(2)
        )
        assert total == 3

    def test_reconcile_duration_is_observed(self):
        fake_redis = FakeRedis()
        seed_desired(fake_redis, [("a", 1)])
        reconciler = make_reconciler(StubTransport(), fake_redis)
        before = reconciler_module.reconcile_duration_seconds._sum.get()

        asyncio.run(reconciler.reconcile_once())

        assert reconciler_module.reconcile_duration_seconds._sum.get() >= before

    def test_active_stream_count_follows_the_reconciler_not_joined_channels(self):
        """joined_channels is no longer maintained, so the old gauge would sit
        at zero forever. It has to follow the subscriptions that exist."""
        fake_redis = FakeRedis()
        seed_desired(fake_redis, [("a", 1), ("b", 2), ("c", 3)])
        observed = []
        reconciler = Reconciler(
            transport=StubTransport(),
            redis_client=fake_redis,
            config=ReconcilerConfig(concurrency=4, idle_timeout_seconds=0.01),
            on_pass_complete=observed.append,
        )

        asyncio.run(reconciler.reconcile_once())

        assert observed == [3]


class TestReconcilerLifecycle:
    """T012 -- the reconciler is a task in THIS process, started from start().

    Not a separate container: it shares the process /health endpoint and this
    logger. It must also be stopped before Redis closes underneath it.
    """

    def test_start_launches_the_reconciler_task(self):
        service = StreamMonitoringService()
        fake_redis = FakeRedis()
        seed_desired(fake_redis, [("a", 1)])
        transport = StubTransport()

        async def fake_initialize():
            service.redis_client = fake_redis
            service.scheduler = MagicMock()
            service.reconciler = make_reconciler(transport, fake_redis)

        async def run_briefly():
            service.initialize = fake_initialize
            # Let start() run for real and stop it the way a signal would.
            # Pre-setting `running = False` no longer works as a shortcut:
            # start() now treats that as "shutdown was signalled during
            # start-up" and deliberately launches nothing.
            starter = asyncio.create_task(service.start())
            await asyncio.sleep(0.05)
            assert service._reconciler_task is not None
            assert not service._reconciler_task.done()
            service.running = False
            await starter
            task = service._reconciler_task
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            return task

        task = asyncio.run(run_briefly())

        assert sorted(transport.subscriptions) == [1], "the task never reconciled"
        assert task.cancelled() or task.done(), "the loop outlived its event loop"

    def test_shutdown_survives_a_scheduler_that_was_never_started(self):
        """`start()` can now return before `scheduler.start()` when shutdown is
        signalled during start-up. APScheduler's `shutdown()` on a never-started
        scheduler raises `AttributeError: 'NoneType' object has no attribute
        'call_soon_threadsafe'`, and that escaped `stop()` and abandoned every
        step after it: the reconciler never stopped, websockets stayed open,
        and the Kafka producer was never flushed, so buffered chat was dropped.
        The tests missed it because they all pass a MagicMock scheduler.
        """

        async def run():
            from apscheduler.schedulers.asyncio import AsyncIOScheduler

            service = StreamMonitoringService()
            service.scheduler = AsyncIOScheduler()   # real, and never started
            service._scheduler_started = False
            service.kafka_producer = MagicMock()
            service.redis_client = MagicMock()

            await service.stop()   # must not raise

            # Everything after the scheduler still ran.
            service.kafka_producer.flush.assert_called_once()
            service.redis_client.close.assert_called_once()

        asyncio.run(run())

    def test_a_signal_during_start_up_tears_down_a_whole_service(self):
        """A signal can land while `initialize()` is still building things --
        a `docker compose restart` during a slow Twitch auth does it. Tearing
        down then closed the aiohttp session the auth call was using, while
        every other branch no-opped on a Kafka producer, DB pool, Redis client
        and transport that did not exist yet. `initialize()` went on to build
        all of them and nothing ever closed them, after "stopped" was logged.
        """

        async def run():
            service = StreamMonitoringService()
            built = asyncio.Event()

            async def slow_initialize():
                await asyncio.sleep(0.1)          # the Twitch auth
                service.scheduler = MagicMock()
                service.redis_client = MagicMock()
                service.kafka_producer = MagicMock()
                service.reconciler = None
                built.set()

            service.initialize = slow_initialize

            starter = asyncio.create_task(service.start())
            await asyncio.sleep(0.02)             # SIGTERM lands mid-auth
            stopper = asyncio.create_task(service.stop())
            await asyncio.wait_for(starter, timeout=2)
            await asyncio.wait_for(stopper, timeout=2)

            assert built.is_set(), "initialize() did not finish"
            # The resources initialize() built were actually torn down.
            service.redis_client.close.assert_called_once()
            service.kafka_producer.flush.assert_called_once()

        asyncio.run(run())

    def test_stop_cancels_a_start_up_that_will_not_finish(self):
        """Waiting on start-up was not enough on its own.

        `stop()` bounds that wait so a Twitch auth that never returns cannot
        block SIGTERM for ever -- but timing out and tearing down anyway left
        `initialize()` running, so it went on to build a Kafka producer, a
        Postgres pool, a Redis client and live websockets AFTER the teardown
        had already decided none of them existed. `_stopping` is set by then,
        so no later `stop()` could reach them either: the process logged
        "stopped" and then allocated everything it had just promised to close.
        The bound has to cancel, not just give up waiting.
        """

        async def run():
            service = StreamMonitoringService()
            built_after_teardown = []

            async def never_finishing_initialize():
                try:
                    await asyncio.sleep(60)
                except asyncio.CancelledError:
                    raise
                built_after_teardown.append("kafka")

            service.initialize = never_finishing_initialize
            starter = asyncio.create_task(service.start())
            await asyncio.sleep(0.02)

            with patch.object(stream_monitoring_service, "INITIALIZE_WAIT_SECONDS", 0.05):
                await asyncio.wait_for(service.stop(), timeout=2)

            await asyncio.wait_for(starter, timeout=2)
            assert service._init_task.cancelled(), (
                "start-up was left running after the teardown that gave up on it"
            )
            assert built_after_teardown == [], (
                "initialize() allocated resources after stop() had finished"
            )

        asyncio.run(run())

    def test_a_signal_before_start_up_stops_it_from_initializing(self):
        """The signal can also beat `start()` entirely -- `main()` awaits the
        health server first. `stop()` has nothing to wait on and nothing to
        close at that point, so it finishes immediately; `start()` must not
        then go on and build a service nobody will ever tear down."""

        async def run():
            service = StreamMonitoringService()
            initialized = []
            service.initialize = lambda: initialized.append(True)

            await service.stop()
            await service.start()

            assert initialized == []
            assert service._init_task is None

        asyncio.run(run())

    def test_a_cancellation_aimed_at_start_is_not_swallowed(self):
        """`start()` treats a `CancelledError` from awaiting the start-up task
        as "shutdown cancelled me". Only `stop()` does that, and it sets
        `_stopping` first -- any other cancellation is aimed at `start()`
        itself and passing through, so swallowing it would break cancellation
        for whoever asked for it."""

        async def run():
            service = StreamMonitoringService()

            async def slow_initialize():
                await asyncio.sleep(60)

            service.initialize = slow_initialize
            starter = asyncio.create_task(service.start())
            await asyncio.sleep(0.02)
            starter.cancel()

            with pytest.raises(asyncio.CancelledError):
                await starter

        asyncio.run(run())

    def test_stop_cancels_the_reconciler_before_closing_redis(self):
        service = StreamMonitoringService()
        fake_redis = FakeRedis()
        seed_desired(fake_redis, [("a", 1)])
        service.redis_client = MagicMock()
        service.reconciler = make_reconciler(StubTransport(), fake_redis)

        async def run_and_stop():
            service._reconciler_task = asyncio.create_task(service.reconciler.run())
            await asyncio.sleep(0.03)
            await service.stop()
            return service._reconciler_task

        task = asyncio.run(run_and_stop())

        assert task.done()
        assert service.reconciler.running is False
        service.redis_client.close.assert_called_once()


class TestReconcilerConfig:
    """Settings are validated up front, the way resolve_thresholds is."""

    def test_defaults_match_the_measured_decision(self):
        config = reconciler_module.resolve_reconciler_config({})
        assert config.concurrency == 10
        assert config.idle_timeout_seconds == 5.0
        assert config.rate_limit_backoff_seconds == 10.0

    def test_concurrency_is_read_from_the_environment(self):
        config = reconciler_module.resolve_reconciler_config({"RECONCILE_CONCURRENCY": "15"})
        assert config.concurrency == 15

    def test_zero_concurrency_is_rejected(self):
        """A pool of nothing would never converge, and would do it silently."""
        with pytest.raises(ValueError, match="RECONCILE_CONCURRENCY must be >= 1"):
            reconciler_module.resolve_reconciler_config({"RECONCILE_CONCURRENCY": "0"})

    def test_zero_idle_timeout_is_rejected(self):
        with pytest.raises(ValueError, match="RECONCILE_IDLE_TIMEOUT_SECONDS"):
            reconciler_module.resolve_reconciler_config({"RECONCILE_IDLE_TIMEOUT_SECONDS": "0"})

    def test_transport_interface_cannot_be_used_directly(self):
        """The seam is abstract on purpose: Phase 2 supplies the real pool."""
        with pytest.raises(TypeError):
            reconciler_module.SubscriptionTransport()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])


# ---------------------------------------------------------------------------
# Phase 2 -- the EventSub transport
# ---------------------------------------------------------------------------


class FakeTask:
    """One of the library's socket tasks. Done means the socket is finished."""

    def __init__(self, finished=False):
        self._finished = finished

    def done(self):
        return self._finished


class FakeWebsocket:
    """Enough EventSubWebsocket for the pool to drive.

    It mirrors the three private attributes the pool has to read on the real
    one: `_active_subscriptions` (to find an id a reconnect rotated),
    `_callbacks` (so a dropped channel is not resubscribed), and `_tasks`
    (whose completion is the socket-death signal).
    """

    counter = 0

    def __init__(self, *, fail_start=False):
        FakeWebsocket.counter += 1
        self.session_id = f"session-{FakeWebsocket.counter}"
        self.active_session = type("Session", (), {"id": self.session_id})()
        self._active_subscriptions = {}
        self._callbacks = {}
        self._tasks = [FakeTask(), FakeTask()]
        self._running = True
        self._closing = False
        self._socket_thread = None
        # The real ladder `_connect` walks, mirrored so a test can see it
        # emptied rather than merely set.
        self.reconnect_delay_steps = [0, 1, 2, 4, 8, 16, 32, 64, 128]
        self._next_id = 0
        self.started = False
        self.stopped = False
        self.fail_start = fail_start
        self.raise_on_subscribe = None

    def start(self):
        if self.fail_start:
            raise RuntimeError("could not connect")
        self.started = True

    async def stop(self):
        self.stopped = True

    async def listen_channel_chat_message(self, broadcaster_user_id, user_id, callback):
        if self.raise_on_subscribe is not None:
            raise self.raise_on_subscribe
        self._next_id += 1
        subscription_id = f"{self.session_id}-sub-{self._next_id}"
        self._active_subscriptions[subscription_id] = {
            "sub_type": "channel.chat.message",
            "condition": {"broadcaster_user_id": broadcaster_user_id, "user_id": user_id},
            "callback": callback,
        }
        self._callbacks[subscription_id] = {"callback": callback}
        return subscription_id

    def die(self):
        """What a socket that cannot reconnect looks like: the receive task ends."""
        self._tasks = [FakeTask(finished=True), FakeTask()]

    def rotate_ids(self):
        """What a keepalive-loss reconnect does: same channels, new ids."""
        rotated = {}
        for subscription in self._active_subscriptions.values():
            self._next_id += 1
            rotated[f"{self.session_id}-sub-{self._next_id}"] = subscription
        self._active_subscriptions = rotated
        self._callbacks = {key: {"callback": None} for key in rotated}


class FakePoolTwitch:
    """The Twitch client surface the pool uses: users, list, delete."""

    def __init__(self, subscriptions=None, user_id="99"):
        self.user_id = user_id
        self.subscriptions = list(subscriptions or [])
        self.deleted = []
        self.not_found = set()

    def get_users(self):
        async def pages():
            yield type("User", (), {"id": self.user_id})()

        return pages()

    async def get_eventsub_subscriptions(self, sub_type=None, target_token=None):
        rows = list(self.subscriptions)

        class Result:
            total = 10 ** 6  # Deliberately a lie. Nothing may read it.

            def __aiter__(self):
                async def gen():
                    for row in rows:
                        yield row

                return gen()

            def current_cursor(self):
                return None

        return Result()

    async def delete_eventsub_subscription(self, subscription_id, target_token=None):
        if subscription_id in self.not_found:
            raise eventsub_pool.TwitchResourceNotFound("subscription not found")
        self.deleted.append(subscription_id)


def make_pool(cap=SUBSCRIPTIONS_PER_CONNECTION, twitch=None, handler=None, **kwargs):
    """A pool whose connections are fakes, so no socket is ever opened."""
    return EventSubPoolTransport(
        twitch or FakePoolTwitch(),
        handler or AsyncMock(),
        user_id="99",
        cap=cap,
        connection_factory=FakeWebsocket,
        **kwargs,
    )


def existing_subscription(subscription_id, broadcaster_id, session_id, status="enabled"):
    return type(
        "Sub",
        (),
        {
            "id": subscription_id,
            "status": status,
            "condition": {"broadcaster_user_id": str(broadcaster_id)},
            "transport": {"method": "websocket", "session_id": session_id},
        },
    )()


class TestPoolRouting:
    """T019a / FR-006, D6 -- where a channel lands, and that it stays there."""

    def test_routing_is_stable_across_reconciles(self):
        """The same broadcaster comes back to the same connection.

        This is the whole point of D6. If routing moved, a socket death would
        force a reshuffle of the entire pool instead of costing only the
        subscriptions that were actually on the dead socket.

        The pool is given its two connections up front, so this measures
        routing and not the order the channels happened to arrive in -- see
        `test_a_growing_pool_does_not_move_what_it_already_placed` for that.
        """

        async def run():
            pool = make_pool()
            await pool._grow()
            await pool._grow()

            async def fill(order):
                for broadcaster_id in order:
                    await pool.create(broadcaster_id)
                placed = {bid: slot.connection_id for bid, slot in pool._slots.items()}
                for slot in list(pool._slots.values()):
                    await pool.delete(slot.subscription_id)
                assert pool._slots == {}
                return placed

            first = await fill(range(1, 401))
            second = await fill(reversed(range(1, 401)))
            assert second == first
            # And both connections are actually used, so "stable" is not just
            # "everything landed on connection 0".
            assert len(set(first.values())) == 2

        asyncio.run(run())

    def test_a_growing_pool_does_not_move_what_it_already_placed(self):
        """Growth places new channels; it never reshuffles the existing ones.

        A cold start therefore fills the first connection to the cap before
        the second one opens, and the pool stays lopsided until those channels
        are dropped for their own reasons. That is the deliberate trade: an
        even split would mean moving -- and so re-creating -- subscriptions
        that are working, every time the pool grows.
        """

        async def run():
            pool = make_pool()
            for broadcaster_id in range(1, SUBSCRIPTIONS_PER_CONNECTION + 1):
                await pool.create(broadcaster_id)
            before = {bid: slot.connection_id for bid, slot in pool._slots.items()}

            for broadcaster_id in range(SUBSCRIPTIONS_PER_CONNECTION + 1, 401):
                await pool.create(broadcaster_id)

            assert len(pool._connections) == 2
            for broadcaster_id, connection_id in before.items():
                assert pool._slots[broadcaster_id].connection_id == connection_id

        asyncio.run(run())

    def test_routing_survives_process_restart(self):
        """Routing must not depend on PYTHONHASHSEED.

        The built-in `hash()` of a str is salted per process. Using it would
        make every restart reshuffle every channel across the pool, which is
        the failure D6 exists to prevent, and no test inside one process would
        ever catch it.
        """
        scores = [eventsub_pool._score(4242, index) for index in range(4)]
        assert scores == [eventsub_pool._score(4242, index) for index in range(4)]
        # A literal, so a change of hash function has to be deliberate.
        assert eventsub_pool._score(4242, 0) == int.from_bytes(
            __import__("hashlib").blake2b(b"4242:0", digest_size=8).digest(), "big"
        )

    def test_losing_a_connection_moves_only_its_own_channels(self):
        """Rendezvous hashing, not modulo.

        With `hash(id) % len(connections)`, removing one connection of three
        re-routes about two thirds of ALL channels. Here only the channels
        that were on the dead connection may move.
        """

        async def run():
            pool = make_pool()
            for _ in range(3):
                await pool._grow()
            ids = list(range(1, 151))  # well under the cap, so capacity never binds
            before = {bid: pool.route(bid).connection_id for bid in ids}

            doomed = pool._connections[1]
            pool._retire(doomed)
            after = {bid: pool.route(bid).connection_id for bid in ids}

            for broadcaster_id in ids:
                if before[broadcaster_id] != doomed.connection_id:
                    assert after[broadcaster_id] == before[broadcaster_id]

        asyncio.run(run())


class TestPoolOccupancy:
    """T019a / T019 -- the cap holds and the count is the pool's own."""

    def test_occupancy_never_exceeds_the_cap(self):
        async def run():
            pool = make_pool()
            for broadcaster_id in range(1, 700):
                await pool.create(broadcaster_id)
            counts = pool.occupancy()
            assert sum(counts.values()) == 699
            assert all(count <= SUBSCRIPTIONS_PER_CONNECTION for count in counts.values())

        asyncio.run(run())

    def test_concurrent_creates_do_not_oversubscribe(self):
        """Ten workers routing at once must not overfill one session.

        Occupancy is only visible after a create returns, so without a
        reservation the last few creates of a filling connection all see room
        and all take it.
        """

        async def run():
            pool = make_pool(cap=10)
            await asyncio.gather(*(pool.create(bid) for bid in range(1, 26)))
            counts = pool.occupancy()
            assert sum(counts.values()) == 25
            assert all(count <= 10 for count in counts.values())

        asyncio.run(run())

    def test_pool_grows_at_the_cap_boundary(self):
        """One connection up to 300, a second at 301 (T018)."""

        async def run():
            pool = make_pool()
            for broadcaster_id in range(1, SUBSCRIPTIONS_PER_CONNECTION + 1):
                await pool.create(broadcaster_id)
            assert len(pool._connections) == 1
            assert pool.occupancy() == {"0": SUBSCRIPTIONS_PER_CONNECTION}

            await pool.create(SUBSCRIPTIONS_PER_CONNECTION + 1)
            assert len(pool._connections) == 2
            assert sum(pool.occupancy().values()) == SUBSCRIPTIONS_PER_CONNECTION + 1

        asyncio.run(run())

    def test_no_connection_is_opened_before_there_is_work(self):
        """Twitch closes a session that has no subscription within 10 s."""

        async def run():
            pool = make_pool()
            await pool.start()
            assert pool._connections == []
            assert pool.occupancy() == {}

        asyncio.run(run())

    def test_occupancy_is_not_read_back_from_the_library(self):
        """T019. The library's own count is measured-wrong; ours is authoritative."""

        async def run():
            pool = make_pool()
            for broadcaster_id in range(1, 6):
                await pool.create(broadcaster_id)
            # Corrupt the library's view the way the spike saw it corrupted.
            pool._connections[0]._active_subscriptions = {}
            pool._connections[0].websocket._active_subscriptions = {}
            assert pool.occupancy() == {"0": 5}

        asyncio.run(run())


class TestPoolDeletes:
    """T024 -- a subscription that is already gone is not a failure."""

    def test_delete_of_a_lingering_subscription_succeeds(self):
        """`websocket_disconnected` leftovers answer "not found" on DELETE."""

        async def run():
            twitch = FakePoolTwitch()
            pool = make_pool(twitch=twitch)
            subscription_id = await pool.create(7)
            twitch.not_found.add(
                next(iter(pool._connections[0].websocket._active_subscriptions))
            )
            await pool.delete(subscription_id)  # must not raise
            assert pool.occupancy() == {"0": 0}
            assert pool._slots == {}

        asyncio.run(run())

    def test_delete_follows_an_id_a_reconnect_rotated(self):
        """A reconnect re-subscribes everything and every id changes.

        Deleting the id recorded at create time would answer "not found" while
        the real subscription kept delivering into a channel nobody wants.
        """

        async def run():
            twitch = FakePoolTwitch()
            pool = make_pool(twitch=twitch)
            recorded = await pool.create(7)
            websocket = pool._connections[0].websocket
            websocket.rotate_ids()
            live = next(iter(websocket._active_subscriptions))
            assert live != recorded

            await pool.delete(recorded)
            assert twitch.deleted == [live]
            # And the library must not resubscribe it on its next reconnect.
            assert websocket._active_subscriptions == {}
            assert websocket._callbacks == {}

        asyncio.run(run())

    def test_delete_reports_a_real_failure(self):
        """Only "not found" is success. A 401 is still an error."""

        async def run():
            twitch = FakePoolTwitch()
            pool = make_pool(twitch=twitch)
            subscription_id = await pool.create(7)

            async def boom(sub_id, target_token=None):
                raise eventsub_pool.TwitchAPIException("unauthorized")

            twitch.delete_eventsub_subscription = boom
            with pytest.raises(TransportError):
                await pool.delete(subscription_id)

        asyncio.run(run())


class TestPoolEnumeration:
    """FR-005 -- what `list()` counts, and what it refuses to count."""

    def test_list_counts_pages_and_ignores_total(self):
        async def run():
            twitch = FakePoolTwitch()
            pool = make_pool(twitch=twitch)
            await pool.create(1)
            await pool.create(2)
            session = pool._connections[0].websocket.session_id
            twitch.subscriptions = [
                existing_subscription("a", 1, session),
                existing_subscription("b", 2, session),
            ]
            seen = [sub async for sub in pool.list()]
            assert {sub.broadcaster_id for sub in seen} == {1, 2}
            # `total` on the result object claims a million.
            assert len(seen) == 2

        asyncio.run(run())

    def test_list_skips_subscriptions_on_a_session_the_pool_does_not_hold(self):
        """A dead session's subscriptions can never deliver to this process.

        Counting one in the actual set would make the reconciler believe a
        channel is covered while it is silently dark.
        """

        async def run():
            twitch = FakePoolTwitch()
            pool = make_pool(twitch=twitch)
            await pool.create(1)
            session = pool._connections[0].websocket.session_id
            twitch.subscriptions = [
                existing_subscription("a", 1, session),
                existing_subscription("b", 2, "a-session-from-a-dead-process"),
            ]
            seen = [sub async for sub in pool.list()]
            assert [sub.broadcaster_id for sub in seen] == [1]

        asyncio.run(run())

    def test_list_at_startup_yields_nothing(self):
        """No connections yet, so nothing on disk belongs to this process."""

        async def run():
            twitch = FakePoolTwitch(
                subscriptions=[existing_subscription("a", 1, "old-session")]
            )
            pool = make_pool(twitch=twitch)
            assert [sub async for sub in pool.list()] == []

        asyncio.run(run())


class TestPoolErrorClassification:
    """The reconciler acts on the exception type, so the mapping matters."""

    @pytest.mark.parametrize(
        "message,expected",
        [
            ("subscription missing proper authorization", SubscriptionRefusedError),
            ("Forbidden", SubscriptionRefusedError),
            ("Too Many Requests", RateLimitedError),
            ("you have exceeded the rate limit", RateLimitedError),
            ("something else entirely", TransportError),
        ],
    )
    def test_error_text_maps_to_the_right_exception(self, message, expected):
        """pyTwitchAPI throws away the HTTP status, so the text is all there is."""

        async def run():
            pool = make_pool()
            websocket_error = eventsub_pool.EventSubSubscriptionError(message)
            await pool._grow()
            pool._connections[0].websocket.raise_on_subscribe = websocket_error
            with pytest.raises(expected):
                await pool.create(5)

        asyncio.run(run())

    def test_a_refusal_is_not_mistaken_for_a_rate_limit(self):
        """Order matters: a refusal must never enter the 429 retry loop."""

        async def run():
            pool = make_pool()
            await pool._grow()
            pool._connections[0].websocket.raise_on_subscribe = (
                eventsub_pool.EventSubSubscriptionError(
                    "subscription missing proper authorization"
                )
            )
            with pytest.raises(SubscriptionRefusedError):
                await pool.create(5)

        asyncio.run(run())

    def test_a_full_session_is_not_a_rate_limit(self):
        """A full session answers 400. Retrying routes straight back to it."""

        async def run():
            pool = make_pool()
            await pool._grow()
            retired = pool._connections[0].websocket
            retired.raise_on_subscribe = (
                eventsub_pool.EventSubSubscriptionError(
                    "websocket session has too many subscriptions"
                )
            )
            with pytest.raises(TransportError) as caught:
                await pool.create(5)
            assert not isinstance(caught.value, RateLimitedError)
            # Full while holding nothing means the session can carry nothing.
            # Merely skipping it in routing left its thread, event loop and
            # ClientSession leaked for the life of the process, because
            # `_is_dead()` cannot flag a socket the library still likes.
            assert pool._connections == [], "the unusable connection was kept"
            assert retired._closing is True, "its socket was never torn down"

            # And the next create opens a fresh one.
            retired.raise_on_subscribe = None
            await pool.create(5)
            assert len(pool._connections) == 1

        asyncio.run(run())

    def test_a_connection_that_reported_full_returns_to_routing_when_it_drains(self):
        """`full` used to be a permanent flag, so one report retired a socket
        from routing for the life of the process. Under ordinary hysteresis
        churn the pool then opened fresh sockets while drained ones sat idle,
        growing the socket count without bound."""

        async def run():
            pool = make_pool()
            await pool._grow()
            connection = pool._connections[0]
            # Three channels land, then Twitch calls the session full.
            for broadcaster_id in range(3):
                await pool.create(broadcaster_id)
            connection.websocket.raise_on_subscribe = (
                eventsub_pool.EventSubSubscriptionError("subscription limit reached")
            )
            with pytest.raises(TransportError):
                await pool.create(99)
            assert connection.full_at == 3
            assert pool.route(99) is not connection

            # Deleting one takes it back under the level it refused at.
            connection.websocket.raise_on_subscribe = None
            subscription_id = next(iter(connection.subscription_ids))
            await pool.delete(subscription_id)
            assert connection.occupancy == 2
            assert pool.route(99) is connection

        asyncio.run(run())

    def test_a_reconnect_race_is_not_mistaken_for_a_full_session(self):
        """`research.md` records the library raising
        `websocket session has already disconnected` twice from its own
        `_resubscribe` during a 500-channel ramp. A marker list wide enough to
        match that text classified a transient reconnect race as a full
        session -- and at occupancy 0 that retired a perfectly good socket."""

        async def run():
            pool = make_pool()
            await pool._grow()
            pool._connections[0].websocket.raise_on_subscribe = (
                eventsub_pool.EventSubSubscriptionError(
                    "websocket session has already disconnected"
                )
            )
            with pytest.raises(TransportError):
                await pool.create(5)

            assert len(pool._connections) == 1, "a live connection was retired"
            assert pool._connections[0].full_at is None

        asyncio.run(run())

    def test_an_unclassified_error_is_logged_with_its_raw_message(self, caplog):
        """The marker lists are string matches against wording nobody has seen
        from a full session. A miss must leave a diagnosable trace."""

        async def run():
            pool = make_pool()
            await pool._grow()
            pool._connections[0].websocket.raise_on_subscribe = (
                eventsub_pool.EventSubSubscriptionError("some entirely new wording")
            )
            with caplog.at_level(logging.WARNING, logger="stream_monitoring"):
                with pytest.raises(TransportError):
                    await pool.create(5)
            # The raw text rides in `extra`, which is what the JSON handler
            # ships and what makes the miss diagnosable.
            unclassified = [
                record for record in caplog.records
                if "Unclassified" in record.getMessage()
            ]
            assert len(unclassified) == 1
            assert unclassified[0].error == "some entirely new wording"

        asyncio.run(run())

    def test_a_reserved_slot_is_released_when_the_create_fails(self):
        """Otherwise a run of failures would slowly fill the pool with nothing."""

        async def run():
            pool = make_pool()
            await pool._grow()
            pool._connections[0].websocket.raise_on_subscribe = (
                eventsub_pool.EventSubSubscriptionError("Too Many Requests")
            )
            for broadcaster_id in range(20):
                with pytest.raises(RateLimitedError):
                    await pool.create(broadcaster_id)
            assert pool._connections[0].reserved == 0
            assert len(pool._connections) == 1

        asyncio.run(run())

    def test_the_reservation_is_held_until_the_subscription_is_recorded(self):
        """`load` must never dip between giving up the slot and recording the
        subscription.

        It used to: `_release` and the record block took the lock separately,
        so `reserved` fell to 0 while `subscription_ids` was still empty. A
        worker already queued on the lock at that moment -- which is the
        ordinary case at concurrency 10 -- is granted it before the record
        block runs, routes against the dip, and reserves a slot that is
        already spoken for. The session then goes one past its cap and Twitch
        refuses the overflow.

        The dip is what makes that possible, so the dip is what this asserts.
        Sampling at every lock release covers the whole create, including the
        window between the two acquisitions the old code left open.
        """

        async def run():
            pool = make_pool(cap=2)
            await pool._grow()
            connection = pool._connections[0]
            await pool.create(1)          # one recorded, so load starts at 1

            samples = []
            real_lock = pool._lock

            class ProbedLock:
                async def __aenter__(self):
                    await real_lock.acquire()

                async def __aexit__(self, *exc):
                    samples.append(connection.load)
                    real_lock.release()

            pool._lock = ProbedLock()
            await pool.create(2)

            assert samples, "the create took no lock at all"
            assert min(samples) == 2, (
                f"load dipped to {min(samples)} mid-create (samples {samples}); "
                "a worker routing in that window would oversubscribe the session"
            )
            assert (connection.occupancy, connection.reserved) == (2, 0)

        asyncio.run(run())


class TestPoolSocketDeath:
    """T023 / R4 -- a dead socket's channels go back to "not subscribed"."""

    def test_a_dead_connection_is_dropped_and_reported(self):
        async def run():
            lost = []
            pool = make_pool(on_subscriptions_lost=lost.append)
            for broadcaster_id in range(1, 6):
                await pool.create(broadcaster_id)
            pool._connections[0].websocket.die()

            assert pool.reap_dead_connections() == 5
            assert lost == [5]
            assert pool._connections == []
            assert pool.occupancy() == {}
            assert pool._slots == {}

        asyncio.run(run())

    def test_a_healthy_connection_is_left_alone(self):
        async def run():
            lost = []
            pool = make_pool(on_subscriptions_lost=lost.append)
            await pool.create(1)
            assert pool.reap_dead_connections() == 0
            assert lost == []
            assert len(pool._connections) == 1

        asyncio.run(run())

    def test_a_revoked_subscription_is_forgotten_and_reported(self):
        """A revocation stops delivery while every count still says "covered".

        Twitch revokes when the broadcaster withdraws authorization or the
        channel goes away. Without this the channel is silently dark and the
        subscription gauge never moves.
        """

        async def run():
            lost = []
            pool = make_pool(on_subscriptions_lost=lost.append)
            await pool.start()
            subscription_id = await pool.create(7)

            await pool._on_revocation(
                {"subscription": {"id": subscription_id, "status": "authorization_revoked"}}
            )
            await asyncio.sleep(0)  # let the threadsafe hop run

            assert lost == [1]
            assert pool._slots == {}
            assert pool.occupancy() == {"0": 0}

        asyncio.run(run())

    def test_a_revocation_on_a_rotated_id_still_finds_the_channel(self):
        """A reconnect re-creates every subscription on the socket with new
        ids, and the pool keeps the ones it recorded at create time. So the
        revocation Twitch sends afterwards names an id `_by_subscription` has
        never seen, and the channel must still be found.

        The resolution has to come from the payload. `_handle_revocation` pops
        the id out of `_active_subscriptions` and `_callbacks` BEFORE it calls
        this handler, so any lookup in the library's registries is guaranteed
        to miss -- this test models that by emptying the registry, which is
        the state the handler really runs in. An earlier version of this test
        re-inserted the rotated id by hand and so proved nothing.
        """

        async def run():
            lost = []
            pool = make_pool(on_subscriptions_lost=lost.append)
            await pool.start()
            await pool.create(7)
            websocket = pool._connections[0].websocket

            # The reconnect rotated the id; the library has already forgotten
            # it by the time the revocation reaches us.
            websocket._active_subscriptions.clear()
            websocket._callbacks.clear()

            await pool._on_revocation({
                "subscription": {
                    "id": "rotated-sub-1",
                    "status": "authorization_revoked",
                    "condition": {"broadcaster_user_id": "7"},
                }
            })
            await asyncio.sleep(0)

            assert lost == [1], "the revocation was dropped, leaving the channel dark"
            assert 7 not in pool._slots
            assert pool.occupancy() == {"0": 0}

        asyncio.run(run())

    def test_an_unresolvable_revocation_still_reports_the_loss(self):
        """Twitch only delivers revocations for subscriptions on this pool's
        own sessions, so a revocation carrying no usable condition is still a
        channel this pool has lost track of. Re-enumerating costs one listing;
        staying quiet costs a permanently dark channel."""

        async def run():
            lost = []
            pool = make_pool(on_subscriptions_lost=lost.append)
            await pool.start()
            await pool.create(7)
            await pool._on_revocation({"subscription": {"id": "not-ours"}})
            await asyncio.sleep(0)
            assert lost == [1]

        asyncio.run(run())

    def test_a_slot_from_a_previous_session_is_not_trusted(self):
        """The library's `_resubscribe()` restores the PRE-reconnect map
        wholesale when the FIRST re-subscribe fails (`if not
        self._active_subscriptions`). The registry then reports every old id as
        live while Twitch holds none of them on the new session -- so a
        registry check alone says yes for channels that do not exist, and the
        periodic re-adopt cannot repair it because `create()` keeps handing the
        ghost back. The session the slot was made on is the check that holds."""

        async def run():
            pool = make_pool()
            await pool.start()
            first_id = await pool.create(7)
            connection = pool._connections[0]

            # The reconnect: new session, and _resubscribe restored the old map
            # verbatim, so the registry still claims the old id.
            connection.websocket.active_session = type(
                "Session", (), {"id": "session-after-reconnect"}
            )()

            second_id = await pool.create(7)

            assert second_id != first_id, (
                "create() trusted a registry that survived a reconnect, so the "
                "channel is dark while every count says it is covered"
            )

        asyncio.run(run())

    def test_a_ghost_subscription_is_recreated_rather_than_handed_back(self):
        """`create()` short-circuits on the id it recorded, and a live
        connection is not proof of a live subscription: when the library's
        `_resubscribe()` gives up part way through a reconnect the socket stays
        up while the channels past the failure point no longer exist.

        Handing the recorded id back made no Twitch call, so the periodic
        re-adoption would drop the channel, ask for it again, be given the
        ghost straight back, and count it as covered for ever -- defeating the
        re-adoption that exists to catch exactly this.
        """

        async def run():
            pool = make_pool()
            await pool.start()
            first_id = await pool.create(7)
            websocket = pool._connections[0].websocket

            # The reconnect dropped this channel and never restored it.
            websocket._active_subscriptions.clear()
            websocket._callbacks.clear()

            second_id = await pool.create(7)

            assert second_id != first_id, (
                "create() handed back the id of a subscription that no longer "
                "exists, without contacting Twitch"
            )
            assert pool.occupancy() == {"0": 1}

        asyncio.run(run())

    def test_the_reconciler_recreates_what_the_dead_socket_held(self):
        """End to end: the loss reaches the reconciler and the next pass heals.

        The pool reports; the reconciler drives. The subscription count drops
        first -- that dip is the FR-012 alert -- and then recovers.
        """

        async def run():
            fake_redis = FakeRedis()
            twitch = FakePoolTwitch()
            pool = make_pool(twitch=twitch)
            reconciler = make_reconciler(pool, fake_redis)
            pool.on_subscriptions_lost = lambda lost: reconciler.invalidate_actual_set(lost)

            logins = [(f"c{i}", i) for i in range(1, 6)]
            seed_desired(fake_redis, logins)
            await reconciler.reconcile_once()
            assert reconciler.subscription_count == 5
            first_connection = pool._connections[0]

            # The socket dies. Everything it held is gone.
            first_connection.websocket.die()
            pool.reap_dead_connections()
            assert pool.occupancy() == {}
            assert reconciler_module.eventsub_subscription_count._value.get() == 0, (
                "the dip this alert is built on never reached the gauge"
            )

            # Twitch now reports nothing on a session we hold.
            twitch.subscriptions = []
            await reconciler.reconcile_once()

            assert reconciler.subscription_count == 5
            assert reconciler_module.eventsub_subscription_count._value.get() == 5
            assert pool._connections[0].connection_id != first_connection.connection_id
            assert sum(pool.occupancy().values()) == 5

        asyncio.run(run())


class TestPoolRaces:
    """Failure modes that only appear because the supervisor shares the loop."""

    def test_a_connection_retired_mid_create_leaves_no_ghost_slot(self):
        """The supervisor runs on this loop, and a create is an await.

        If a slot were recorded against a connection that has already been
        retired, nothing would ever clear it: `_retire` has been and gone, no
        revocation arrives for a dead session, and the reconciler does not
        delete a channel it still wants. Every later create would hand back
        that dead id without contacting Twitch, and the channel would be
        permanently dark while every count said "covered".
        """

        async def run():
            pool = make_pool()
            await pool._grow()
            connection = pool._connections[0]
            original = connection.websocket.listen_channel_chat_message

            async def retire_then_subscribe(*args, **kwargs):
                pool._retire(connection)
                return await original(*args, **kwargs)

            connection.websocket.listen_channel_chat_message = retire_then_subscribe

            with pytest.raises(TransportError):
                await pool.create(7)
            assert pool._slots == {}
            assert pool._by_subscription == {}

        asyncio.run(run())

    def test_a_slot_on_a_retired_connection_is_not_handed_back(self):
        """The early return must check the connection, not just the slot."""

        async def run():
            pool = make_pool()
            subscription_id = await pool.create(7)
            # Retire without going through _retire, the way a stale slot could
            # survive a bookkeeping slip.
            pool._connections = []

            recreated = await pool.create(7)
            assert recreated != subscription_id
            assert pool._slots[7].connection_id == pool._connections[0].connection_id

        asyncio.run(run())

    def test_deleting_a_rotated_id_still_clears_the_library(self):
        """`list()` reports the id a reconnect made; `_by_subscription` has the old.

        Deleting the reported id and stopping there would leave the library's
        own registry intact, so the socket re-creates the channel on its next
        reconnect -- exactly the resurrection this cleanup exists to prevent.
        """

        async def run():
            twitch = FakePoolTwitch()
            pool = make_pool(twitch=twitch)
            await pool.create(7)
            websocket = pool._connections[0].websocket
            websocket.rotate_ids()
            rotated = next(iter(websocket._active_subscriptions))

            # The reconciler asks for the id Twitch reports, which the pool
            # has never seen.
            await pool.delete(rotated)

            assert twitch.deleted == [rotated]
            assert websocket._active_subscriptions == {}
            assert websocket._callbacks == {}
            assert pool._slots == {}
            assert pool.occupancy() == {"0": 0}

        asyncio.run(run())

    def test_a_cancelled_connect_tears_its_socket_down(self):
        """The same abandoned socket, reached the other way.

        Shutdown cancels the reconciler task, and that cancellation lands
        wherever the pass happens to be -- including inside `_grow`'s connect.
        `except Exception` does not catch `CancelledError`, so this path skipped
        the teardown the timeout path performs, and cancelling the future does
        not stop the executor thread already running `start()`. The socket is
        never appended to `_connections`, so `aclose()` cannot reach it either:
        a SIGTERM during a cold start leaked a spinning thread, its event loop
        and an open ClientSession, and kept the process from exiting cleanly.
        """

        class NeverConnects(FakeWebsocket):
            def start(self):
                self._startup_complete = False
                while not self._startup_complete:
                    time.sleep(0.01)

        async def run():
            pool = make_pool(connect_timeout_seconds=30)
            opened = []

            def factory():
                websocket = NeverConnects()
                opened.append(websocket)
                return websocket

            pool._connection_factory = factory

            creating = asyncio.create_task(pool.create(7))
            await asyncio.sleep(0.1)
            creating.cancel()
            with pytest.raises(asyncio.CancelledError):
                await creating

            assert opened, "no socket was built"
            assert opened[0]._startup_complete is True, (
                "the executor thread was left busy-waiting in start()"
            )
            assert opened[0]._closing is True, (
                "the abandoned socket's keep-alive loop was left spinning"
            )
            assert opened[0].reconnect_delay_steps == [], (
                "`_connect` ignores `_closing`, so a socket abandoned during a "
                "failing connect keeps a non-daemon thread through the whole "
                "255 s retry ladder and SIGTERM hangs joining it"
            )
            assert pool._connections == [], "a half-open socket joined the pool"

        asyncio.run(run())

    def test_a_reconnect_mid_create_is_not_recorded_as_current(self):
        """The library builds the POST's transport from the session that is
        current when the request goes out, and its socket thread can finish a
        reconnect while that request is in flight. Stamping the slot with the
        session read AFTER the await labelled a subscription made on the OLD
        session with the NEW one -- and then agreed with itself for ever: the
        session check passes, the registry holds the id because `_subscribe`
        added it, and `create()` hands that ghost back with no Twitch call
        while nothing delivers for the channel. `_resubscribe()` cannot save
        it either; it only re-creates what was in the registry when it took
        its snapshot.
        """

        async def run():
            pool = make_pool()
            await pool.start()
            await pool._grow()
            connection = pool._connections[0]
            websocket = connection.websocket
            original_listen = websocket.listen_channel_chat_message

            async def reconnect_during_create(broadcaster_user_id, user_id, callback):
                subscription_id = await original_listen(
                    broadcaster_user_id, user_id, callback
                )
                websocket.active_session = type(
                    "Session", (), {"id": "session-after-reconnect"}
                )()
                return subscription_id

            websocket.listen_channel_chat_message = reconnect_during_create

            with pytest.raises(TransportError):
                await pool.create(7)

            assert pool._slots == {}, (
                "a subscription made on a closed session was recorded as current"
            )
            assert connection.occupancy == 0
            assert connection.reserved == 0, "the reservation outlived the create"
            assert websocket._active_subscriptions == {}, (
                "the library would resurrect the dead id on its next reconnect"
            )

            # And the channel is simply retried, on the live session.
            websocket.listen_channel_chat_message = original_listen
            assert await pool.create(7)
            assert pool._slots[7].session_id == "session-after-reconnect"

        asyncio.run(run())

    def test_a_reconnect_mid_create_deletes_rather_than_guessing(self):
        """Which session the subscription landed on cannot be known out here.

        `_subscribe` reads the session when it builds the POST body, so a
        reconnect that finished BEFORE that moment puts the subscription on the
        NEW session -- live, with a callback -- and one that finished after
        puts it on the old one. Both look identical from the pool: the session
        changed across the await.

        Guessing "it is dead" and only dropping the library's callback was
        wrong for the live case: the subscription went on existing while the
        library had no callback for it, so it delivered into nothing, and the
        next pass took Twitch's 409 and adopted it -- `_adopt_conflict`
        restores the pool's indexes but not the callback -- leaving the channel
        counted and dark. Deleting is correct either way: the dead one answers
        "not found", the live one is removed and re-created cleanly.
        """

        async def run():
            twitch = FakePoolTwitch()
            pool = make_pool(twitch=twitch)
            await pool.start()
            await pool._grow()
            connection = pool._connections[0]
            websocket = connection.websocket
            original_listen = websocket.listen_channel_chat_message

            async def reconnect_during_create(broadcaster_user_id, user_id, callback):
                subscription_id = await original_listen(
                    broadcaster_user_id, user_id, callback
                )
                websocket.active_session = type(
                    "Session", (), {"id": "session-after-reconnect"}
                )()
                return subscription_id

            websocket.listen_channel_chat_message = reconnect_during_create

            with pytest.raises(TransportError):
                await pool.create(7)

            assert twitch.deleted, (
                "the subscription was abandoned without being deleted, so a live "
                "one would linger with no callback and be adopted as covered"
            )
            assert pool._slots == {}
            assert websocket._active_subscriptions == {}
            assert connection.reserved == 0, "the reservation outlived the create"

        asyncio.run(run())

    def test_a_failed_delete_leaves_the_library_registry_alone(self):
        """The safe side of that error. If the DELETE fails we cannot know the
        subscription is gone, so the callback must stay: on the live-session
        branch it and the subscription are both still intact, and the next
        enumeration simply adopts something that works. Clearing it first
        would have thrown that away on a transient API error."""

        async def run():
            twitch = FakePoolTwitch()
            pool = make_pool(twitch=twitch)
            await pool.start()
            await pool._grow()
            connection = pool._connections[0]
            websocket = connection.websocket
            original_listen = websocket.listen_channel_chat_message

            async def reconnect_during_create(broadcaster_user_id, user_id, callback):
                subscription_id = await original_listen(
                    broadcaster_user_id, user_id, callback
                )
                websocket.active_session = type(
                    "Session", (), {"id": "session-after-reconnect"}
                )()
                return subscription_id

            websocket.listen_channel_chat_message = reconnect_during_create

            async def failing_delete(subscription_id, target_token=None):
                raise eventsub_pool.TwitchAPIException("twitch 503")

            twitch.delete_eventsub_subscription = failing_delete

            with pytest.raises(TransportError):
                await pool.create(7)

            assert websocket._active_subscriptions, (
                "the callback was dropped for a subscription that may still be live"
            )
            assert pool._slots == {}
            assert connection.reserved == 0

        asyncio.run(run())

    def test_the_pool_refuses_to_grow_past_the_twitch_connection_limit(self):
        """Twitch allows 3 websocket connections with enabled subscriptions per
        client-id/user-id pair, so this transport tops out at 900 channels.
        Growth was unbounded and knew nothing about it. A fourth socket does
        not fail at connect time -- it fails later, per subscription, with an
        error this module cannot classify, and rendezvous routing keeps sending
        the same channels back to it: a silent retry loop with only a WARNING.
        """

        async def run():
            pool = make_pool(cap=1)
            await pool.start()
            for broadcaster_id in range(1, 4):
                await pool.create(broadcaster_id)
            assert len(pool._connections) == 3

            with pytest.raises(TransportError) as caught:
                await pool.create(4)
            assert "connection limit" in str(caught.value)
            assert len(pool._connections) == 3, "a fourth socket was opened"

        asyncio.run(run())

    def test_a_timed_out_connect_tears_its_socket_down(self):
        """`_keep_loop_alive()` spins on `while not self._closing`, and only
        `_stop()` sets that flag. A timed-out connect that only released the
        startup busy-wait left the socket thread spinning at 10 Hz for the life
        of the process, holding an open ClientSession -- and invisibly, since
        the connection is never appended to `_connections` and so
        `reap_dead_connections()` could never see it."""

        class NeverConnects(FakeWebsocket):
            def start(self):
                self._startup_complete = False
                while not self._startup_complete:
                    time.sleep(0.01)

        async def run():
            pool = make_pool(connect_timeout_seconds=0.3)
            pool._connection_factory = NeverConnects
            opened = []
            original = pool._connection_factory

            def factory():
                websocket = original()
                opened.append(websocket)
                return websocket

            pool._connection_factory = factory

            with pytest.raises(TransportError):
                await pool.create(7)

            assert opened, "no socket was built"
            assert opened[0]._closing is True, (
                "the abandoned socket's keep-alive loop was left spinning"
            )

        asyncio.run(run())

    def test_a_connection_that_never_comes_up_does_not_wedge_the_pool(self):
        """`start()` busy-waits on a flag only session_welcome sets.

        A socket thread that dies on the way up never sets it, so `start()`
        spins for the life of the process. Holding the growth lock across an
        unbounded wait would freeze every later create, and with it the
        reconciler.
        """

        class NeverConnects(FakeWebsocket):
            def start(self):
                self._startup_complete = False
                while not self._startup_complete:
                    time.sleep(0.01)

        async def run():
            pool = make_pool(connect_timeout_seconds=0.3)
            pool._connection_factory = NeverConnects

            with pytest.raises(TransportError):
                await pool.create(7)

            # A failed connect blocks growth briefly, so the rest of the batch
            # fails fast instead of each channel waiting out its own timeout.
            pool._connection_factory = FakeWebsocket
            with pytest.raises(TransportError):
                await pool.create(8)

            # The lock is free, so the pool still works once that window ends.
            pool._growth_blocked_until = 0.0
            assert await pool.create(7)
            assert pool.occupancy() == {"0": 1}

        asyncio.run(run())


class TestDegradedWithoutUserAuth:
    """A missing token file must not crash-loop the container."""

    def test_a_missing_token_file_leaves_the_poller_running(self):
        """The pool resolves the auth user through get_users(), which an app
        token cannot do -- so building it would raise and take the container
        down in a restart loop. The warning this path logs promises the
        service keeps running with chat off, so it has to actually do that.

        This drives the real `initialize()`, because the branch under test is
        inside it and a test that re-implements the branch tests itself.
        """

        async def run():
            twitch = MagicMock()
            twitch.authenticate_app = AsyncMock()
            credentials = MagicMock()
            credentials.load.side_effect = FileNotFoundError("no token file")

            service = StreamMonitoringService()
            with patch.object(stream_monitoring_service, "Twitch", AsyncMock(return_value=twitch)), \
                 patch.object(stream_monitoring_service, "get_credentials", return_value=credentials), \
                 patch.object(stream_monitoring_service, "Producer"), \
                 patch.object(stream_monitoring_service.psycopg2.pool, "ThreadedConnectionPool"), \
                 patch.object(stream_monitoring_service.redis, "from_url", return_value=MagicMock()), \
                 patch.object(stream_monitoring_service, "start_http_server"), \
                 patch.object(StreamMonitoringService, "_build_transport", AsyncMock()) as build_transport:
                await service.initialize()

            build_transport.assert_not_called()
            assert service.transport is None
            assert service.reconciler is None
            # The poll job is still scheduled: intent keeps being written.
            assert service.scheduler.get_job("poll_streams") is not None

        asyncio.run(run())

    def test_a_bad_token_degrades_instead_of_crash_looping(self):
        """The fallback promised "running without user auth", but only
        FileNotFoundError reached it. A token that expired and cannot refresh
        raises InvalidTokenException, a scope-reduced one MissingScopeException,
        and a truncated file raises out of `credentials.load()` -- all of them
        far likelier than a missing file, and all of them used to crash-loop
        the container."""

        async def run():
            for failure in (
                ValueError("expired and could not refresh"),
                KeyError("access_token"),
            ):
                twitch = MagicMock()
                twitch.authenticate_app = AsyncMock()
                credentials = MagicMock()
                credentials.load.side_effect = failure

                service = StreamMonitoringService()
                with patch.object(stream_monitoring_service, "Twitch", AsyncMock(return_value=twitch)), \
                     patch.object(stream_monitoring_service, "get_credentials", return_value=credentials), \
                     patch.object(stream_monitoring_service, "Producer"), \
                     patch.object(stream_monitoring_service.psycopg2.pool, "ThreadedConnectionPool"), \
                     patch.object(stream_monitoring_service.redis, "from_url", return_value=MagicMock()), \
                     patch.object(stream_monitoring_service, "start_http_server"), \
                     patch.object(StreamMonitoringService, "_build_transport", AsyncMock()):
                    await service.initialize()   # must not raise

                assert service.has_user_auth is False
                assert service.scheduler.get_job("poll_streams") is not None

        asyncio.run(run())

    def test_the_metrics_server_starts_before_anything_that_can_fail(self):
        """Whatever kills start-up, /metrics has to be up first -- it is where
        the operator is sent to diagnose the failure."""

        async def run():
            service = StreamMonitoringService()
            with patch.object(
                stream_monitoring_service, "Twitch",
                AsyncMock(side_effect=RuntimeError("twitch is down")),
            ), \
                 patch.object(stream_monitoring_service, "start_http_server") as metrics:
                with pytest.raises(RuntimeError):
                    await service.initialize()

            metrics.assert_called_once()

        asyncio.run(run())

    def test_the_refresh_callback_is_registered_before_authenticating(self):
        """`set_user_authentication` refreshes internally on a 401 and invokes
        the callback during that call. Assigned afterwards it is still None at
        that moment, so the rotated refresh token is dropped and the file keeps
        the old one -- which locks the service out at the next restart."""

        async def run():
            seen = []
            twitch = MagicMock()

            async def set_user_authentication(*args, **kwargs):
                seen.append(twitch.user_auth_refresh_callback)

            twitch.set_user_authentication = set_user_authentication
            twitch.user_auth_refresh_callback = None

            credentials = MagicMock()
            record = MagicMock()
            record.access_token = "a"
            record.refresh_token = "r"
            record.scopes = ["user:read:chat", "clips:edit"]
            credentials.load.return_value = record

            service = StreamMonitoringService()
            with patch.object(stream_monitoring_service, "Twitch", AsyncMock(return_value=twitch)), \
                 patch.object(stream_monitoring_service, "get_credentials", return_value=credentials), \
                 patch.object(stream_monitoring_service, "Producer"), \
                 patch.object(stream_monitoring_service.psycopg2.pool, "ThreadedConnectionPool"), \
                 patch.object(stream_monitoring_service.redis, "from_url", return_value=MagicMock()), \
                 patch.object(stream_monitoring_service, "start_http_server"), \
                 patch.object(StreamMonitoringService, "_build_transport", AsyncMock()):
                await service.initialize()

            assert seen and seen[0] is not None, (
                "a refresh during set_user_authentication would have dropped "
                "the rotated refresh token"
            )

        asyncio.run(run())

    def test_a_transient_transport_failure_propagates_so_docker_restarts_us(self):
        """`_build_transport()` calls get_users(), so its failures are
        transient by nature. An earlier round swallowed them to avoid
        crash-looping, which was worse than the crash: the service ran for the
        rest of the process lifetime with no transport, no reconciler and zero
        chat ingestion, while the poll job kept working, /health kept returning
        OK, and nothing ever retried.

        Letting it propagate restarts the container, which recovers by itself.
        The metrics server is up before this point either way -- that was the
        fix worth keeping, and it is asserted here so the two cannot drift.
        """

        async def run():
            twitch = MagicMock()
            credentials = MagicMock()
            record = MagicMock()
            record.access_token = "a"
            record.refresh_token = "r"
            record.scopes = ["user:read:chat", "clips:edit"]
            credentials.load.return_value = record
            twitch.set_user_authentication = AsyncMock()

            service = StreamMonitoringService()
            with patch.object(stream_monitoring_service, "Twitch", AsyncMock(return_value=twitch)), \
                 patch.object(stream_monitoring_service, "get_credentials", return_value=credentials), \
                 patch.object(stream_monitoring_service, "Producer"), \
                 patch.object(stream_monitoring_service.psycopg2.pool, "ThreadedConnectionPool"), \
                 patch.object(stream_monitoring_service.redis, "from_url", return_value=MagicMock()), \
                 patch.object(stream_monitoring_service, "start_http_server") as metrics, \
                 patch.object(
                     StreamMonitoringService, "_build_transport",
                     AsyncMock(side_effect=RuntimeError("twitch 503")),
                 ):
                with pytest.raises(RuntimeError):
                    await service.start()

            metrics.assert_called_once()
            # And start-up is over even though it raised, so a shutdown racing
            # it does not wait out the full INITIALIZE_WAIT_SECONDS before
            # tearing down.
            assert service._init_task is not None and service._init_task.done()

        asyncio.run(run())

    def test_start_does_not_launch_a_reconciler_that_was_never_built(self):
        """The `if self.reconciler is not None` guard in `start()`.

        Pre-setting `running = False` no longer reaches it: `start()` now
        treats that as "shutdown was signalled" and returns before it
        initializes anything, which made this test pass whether the guard
        existed or not. Stop the keep-alive loop the way a signal does
        instead, so the guard is actually exercised.
        """

        async def run():
            service = StreamMonitoringService()

            async def fake_initialize():
                service.reconciler = None
                service.scheduler = MagicMock()

            service.initialize = fake_initialize
            starter = asyncio.create_task(service.start())
            await asyncio.sleep(0.05)
            service.running = False
            await asyncio.wait_for(starter, timeout=2)

            service.scheduler.start.assert_called_once(), "start() never got that far"
            assert service._reconciler_task is None

        asyncio.run(run())


class TestEventSubMessageMapping:
    """T020 / FR-008, FR-009 -- the payload the Flink job consumes."""

    def test_sent_at_is_epoch_milliseconds(self):
        moment = datetime(2026, 8, 28, 12, 0, 0, 500000, tzinfo=timezone.utc)
        assert to_epoch_ms(moment) == int(moment.timestamp() * 1000)
        assert to_epoch_ms(moment) % 1000 == 500

    def test_sent_at_parses_the_raw_rfc_3339_envelope(self):
        """Twitch sends up to nine fractional digits; 3.10 accepts three or six."""
        assert to_epoch_ms("2026-08-28T12:00:00.500000000Z") == to_epoch_ms(
            datetime(2026, 8, 28, 12, 0, 0, 500000, tzinfo=timezone.utc)
        )
        assert to_epoch_ms("2026-08-28T12:00:00Z") == 1787918400000
        assert to_epoch_ms(None) is None

    def test_sent_at_is_never_a_string(self):
        """Contract invariant 2. A string makes SentAtTimestampAssigner fall
        back to record time, silently, and event-time detection drifts."""
        payload = map_chat_message(make_eventsub_event())
        assert isinstance(payload["sent_at"], int)

    def test_an_envelope_without_a_timestamp_still_publishes(self):
        """`spec.md` Edge Cases: a missing or unparseable `message_timestamp`
        publishes with `sent_at` null, so the Flink assigner falls back to
        record time. It does NOT drop the message -- that would trade a field
        the contract already allows to be null for a chat message, against the
        constitution's no-data-loss rule.

        `TwitchObject.__init__` skips any field the payload omits, so an
        envelope without the timestamp has no such attribute at all rather
        than a None one, and the attribute access itself used to raise into
        `_on_eventsub_message`'s handler.
        """
        event = make_eventsub_event()
        del type(event.metadata).message_timestamp

        payload = map_chat_message(event)

        assert payload["sent_at"] is None
        assert payload["text"] == "hello world"
        assert payload["broadcaster_id"] == 123

    def test_an_unreadable_timestamp_publishes_a_null_not_an_exception(self):
        assert to_epoch_ms("not-a-timestamp") is None
        assert to_epoch_ms(1787918400) is None, "an int is not the envelope shape"
        assert to_epoch_ms("") is None
        payload = map_chat_message(make_eventsub_event(sent_at="not-a-timestamp"))
        assert payload["sent_at"] is None

    def test_badges_become_a_dict_and_drive_the_two_booleans(self):
        payload = map_chat_message(
            make_eventsub_event(badges=[("subscriber", "12"), ("moderator", "1")])
        )
        assert payload["metadata"]["badges"] == {"subscriber": "12", "moderator": "1"}
        assert payload["metadata"]["is_subscriber"] is True
        assert payload["metadata"]["is_mod"] is True

    def test_no_badges_means_neither_flag(self):
        payload = map_chat_message(make_eventsub_event(badges=[]))
        assert payload["metadata"]["badges"] == {}
        assert payload["metadata"]["is_subscriber"] is False
        assert payload["metadata"]["is_mod"] is False

    def test_emotes_stays_empty(self):
        """IRC never populated it. Starting now would change the payload in a
        feature that promises not to."""
        payload = map_chat_message(
            make_eventsub_event(badges=[("subscriber", "1")])
        )
        assert payload["metadata"]["emotes"] == {}

    def test_broadcaster_id_comes_from_the_event(self):
        """No login-to-id lookup, so no message is dropped for a missing map.

        The IRC handler returned early whenever `broadcaster_ids` had no entry
        for the room, which silently lost every message from a channel joined
        before the poll that named it.
        """
        payload = map_chat_message(make_eventsub_event(broadcaster_id=147))
        assert payload["broadcaster_id"] == 147
        assert isinstance(payload["broadcaster_id"], int)

    def test_an_anonymous_chatter_maps_to_user_id_zero(self):
        payload = map_chat_message(make_eventsub_event(chatter_id=""))
        assert payload["user_id"] == 0

    def test_message_id_falls_back_to_a_generated_uuid(self):
        payload = map_chat_message(make_eventsub_event(message_id=None))
        assert payload["message_id"]
        assert isinstance(payload["message_id"], str)

    def test_the_service_publishes_what_the_mapper_produced(self):
        """T022 -- the handler the pool calls reaches the existing producer."""

        async def run():
            service = StreamMonitoringService()
            service.kafka_producer = MagicMock()
            published = []
            service._publish_chat_message = lambda bid, msg: published.append((bid, msg))
            await service._on_eventsub_message(make_eventsub_event(broadcaster_id=99))
            assert published[0][0] == 99
            assert published[0][1]["broadcaster_id"] == 99

        asyncio.run(run())

    def test_a_broken_event_does_not_kill_the_socket(self):
        """One malformed event must not stop delivery for the other 299."""

        async def run():
            service = StreamMonitoringService()
            service._publish_chat_message = MagicMock()
            await service._on_eventsub_message(object())  # no .event at all
            service._publish_chat_message.assert_not_called()

        asyncio.run(run())


def make_eventsub_event(
    broadcaster_id=123,
    chatter_id="456",
    text="hello world",
    badges=(),
    message_id="msg-uuid",
    sent_at=None,
):
    """A stand-in for ChannelChatMessageEvent, shaped like the real one.

    pyTwitchAPI has already turned `metadata.message_timestamp` into a
    tz-aware datetime by the time an event reaches a callback, so the fake
    carries a datetime too.
    """
    badge_objects = [
        type("Badge", (), {"set_id": set_id, "id": badge_id})() for set_id, badge_id in badges
    ]
    return type(
        "Event",
        (),
        {
            "metadata": type(
                "Meta",
                (),
                {
                    "message_timestamp": sent_at
                    or datetime(2026, 8, 28, 12, 0, 0, 250000, tzinfo=timezone.utc)
                },
            )(),
            "event": type(
                "Data",
                (),
                {
                    "broadcaster_user_id": str(broadcaster_id),
                    "chatter_user_id": chatter_id,
                    "chatter_user_login": "a_viewer",
                    "message_id": message_id,
                    "message": type("Message", (), {"text": text})(),
                    "badges": badge_objects,
                },
            )(),
        },
    )()


class FakeRefusalStore(RefusalStore):
    """An in-memory `streamers.eventsub_refused_at`."""

    def __init__(self, marks=None):
        # broadcaster id -> stale?
        self.marks = dict(marks or {})
        self.marked = []
        self.cleared = []

    def refusals(self, broadcaster_ids):
        return {bid: stale for bid, stale in self.marks.items() if bid in set(broadcaster_ids)}

    def mark_refused(self, broadcaster_id):
        self.marked.append(broadcaster_id)
        self.marks[broadcaster_id] = False  # a fresh mark is never stale

    def clear_refusal(self, broadcaster_id):
        self.cleared.append(broadcaster_id)
        self.marks.pop(broadcaster_id, None)


class TestRefusalCache:
    """T025c / FR-007, D5 -- refusals persist, and they expire."""

    def test_a_recent_refusal_is_skipped(self):
        async def run():
            fake_redis = FakeRedis()
            transport = StubTransport()
            store = FakeRefusalStore({2: False})
            reconciler = make_reconciler(transport, fake_redis)
            reconciler.refusal_store = store
            seed_desired(fake_redis, [("a", 1), ("b", 2), ("c", 3)])

            await reconciler.reconcile_once()

            assert transport.create_calls == [1, 3]
            assert reconciler.subscription_count == 2

        asyncio.run(run())

    def test_a_stale_refusal_is_retried_once_and_cleared_on_success(self):
        async def run():
            fake_redis = FakeRedis()
            transport = StubTransport()
            store = FakeRefusalStore({2: True})
            reconciler = make_reconciler(transport, fake_redis)
            reconciler.refusal_store = store
            seed_desired(fake_redis, [("a", 1), ("b", 2)])

            await reconciler.reconcile_once()

            assert sorted(transport.create_calls) == [1, 2]
            assert store.cleared == [2]
            assert 2 not in store.marks

        asyncio.run(run())

    def test_a_refusal_is_recorded_so_the_next_pass_skips_it(self):
        async def run():
            fake_redis = FakeRedis()
            transport = StubTransport(refuse={2})
            store = FakeRefusalStore()
            reconciler = make_reconciler(transport, fake_redis)
            reconciler.refusal_store = store
            seed_desired(fake_redis, [("a", 1), ("b", 2)])

            await reconciler.reconcile_once()
            assert store.marked == [2]

            transport.create_calls.clear()
            await reconciler.reconcile_once()
            assert transport.create_calls == []  # 1 is held, 2 is now skipped

        asyncio.run(run())

    def test_a_fresh_refusal_resets_a_stale_mark(self):
        """The retry that fails restarts the 7 days rather than retrying forever."""

        async def run():
            fake_redis = FakeRedis()
            transport = StubTransport(refuse={2})
            store = FakeRefusalStore({2: True})
            reconciler = make_reconciler(transport, fake_redis)
            reconciler.refusal_store = store
            seed_desired(fake_redis, [("b", 2)])

            await reconciler.reconcile_once()

            assert transport.create_calls == [2]  # the stale mark bought a retry
            assert store.marked == [2]            # which refused, so the mark is reset
            assert store.cleared == []
            assert store.marks[2] is False

        asyncio.run(run())

    def test_a_database_fault_does_not_stop_the_reconciler(self):
        """A store that throws must not leave every channel unsubscribed.

        The reconciler runs as one long-lived task; an exception escaping a
        pass ends the loop and the service goes quiet with no subscriptions
        and no error after the first line. Falling back to "attempt
        everything" costs at most one wasted POST per refused channel.
        """

        async def run():
            fake_redis = FakeRedis()
            transport = StubTransport()
            store = FakeRefusalStore({2: False})
            store.refusals = MagicMock(side_effect=ConnectionError("postgres is away"))
            reconciler = make_reconciler(transport, fake_redis)
            reconciler.refusal_store = store
            seed_desired(fake_redis, [("a", 1), ("b", 2)])

            await reconciler.reconcile_once()

            assert sorted(transport.create_calls) == [1, 2]
            assert reconciler.subscription_count == 2

        asyncio.run(run())

    def test_without_a_store_every_channel_is_attempted(self):
        """Phase 1 behaviour is preserved when no store is supplied."""

        async def run():
            fake_redis = FakeRedis()
            transport = StubTransport()
            reconciler = make_reconciler(transport, fake_redis)
            seed_desired(fake_redis, [("a", 1), ("b", 2)])
            await reconciler.reconcile_once()
            assert sorted(transport.create_calls) == [1, 2]

        asyncio.run(run())


# ---------------------------------------------------------------------------
# The 7-day self-heal, against a real Postgres
# ---------------------------------------------------------------------------
#
# `make_interval`, `ANY(%s)` and the NULL handling are SQL, and a hand-written
# fake cursor can only confirm that the string was sent -- not that Postgres
# agrees with what it means. Phase 1 checked the Redis seam the same way, for
# the same reason.
#
# The fixture builds its own schema and drops it afterwards, and refuses to run
# unless `streamers` really resolves inside that schema, so it can never touch
# the deployed table. It skips when there is no database to talk to.

TEST_SCHEMA = "spec004_selfheal_test"
# Deliberately localhost, not the deployed host. The fixture runs DDL --
# CREATE SCHEMA and DROP SCHEMA CASCADE -- and defaulting that at the live
# database would put every `pytest` run on production, and put its credential
# in this file. `docker compose --profile local-db up postgres` gives a local
# one; set TEST_POSTGRES_URL to point somewhere else on purpose.
TEST_POSTGRES_URL = os.getenv(
    "TEST_POSTGRES_URL", "postgresql://twitch:twitch_password@localhost:5432/twitch"
)


class SingleConnectionPool:
    """The two-method slice of psycopg2's pool that this code uses."""

    def __init__(self, conn):
        self.conn = conn

    def getconn(self):
        return self.conn

    def putconn(self, conn):
        pass


@pytest.fixture
def streamers_table():
    psycopg2 = pytest.importorskip("psycopg2")
    try:
        conn = psycopg2.connect(TEST_POSTGRES_URL, connect_timeout=3)
    except Exception as e:  # pragma: no cover -- environment, not logic
        pytest.skip(f"no Postgres available for the self-heal check: {e}")

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
                first_seen_at TIMESTAMPTZ DEFAULT NOW(),
                last_seen_at TIMESTAMPTZ DEFAULT NOW(),
                eventsub_refused_at TIMESTAMPTZ,
                clipping_disabled_at TIMESTAMPTZ
            )
            """
        )
        # Refuse to go anywhere near the deployed table.
        cur.execute(
            "SELECT n.nspname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE c.oid = to_regclass('streamers')"
        )
        resolved = cur.fetchone()
        assert resolved and resolved[0] == TEST_SCHEMA, (
            f"'streamers' resolves to {resolved} rather than the test schema; refusing to run"
        )
    conn.commit()

    try:
        yield conn
    finally:
        with conn.cursor() as cur:
            cur.execute(f"DROP SCHEMA IF EXISTS {TEST_SCHEMA} CASCADE")
        conn.commit()
        conn.close()


def add_streamer(conn, streamer_id, *, allows_clipping=True, refused_days_ago=None,
                 disabled_days_ago=None, disabled_at_null=False):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO streamers (streamer_id, streamer_login, allows_clipping, "
            "eventsub_refused_at, clipping_disabled_at) VALUES (%s, %s, %s, "
            "CASE WHEN %s IS NULL THEN NULL ELSE NOW() - make_interval(days => %s) END, "
            "CASE WHEN %s THEN NULL WHEN %s IS NULL THEN NULL "
            "     ELSE NOW() - make_interval(days => %s) END)",
            (
                streamer_id, f"login{streamer_id}", allows_clipping,
                refused_days_ago, refused_days_ago or 0,
                disabled_at_null, disabled_days_ago, disabled_days_ago or 0,
            ),
        )
    conn.commit()


class TestRefusalStoreAgainstPostgres:
    """T025c / FR-007 -- the SQL behind the 7-day refusal re-check."""

    def test_a_fresh_mark_stands_and_an_old_one_is_stale(self, streamers_table):
        add_streamer(streamers_table, 1)  # never refused
        add_streamer(streamers_table, 2, refused_days_ago=1)
        add_streamer(streamers_table, 3, refused_days_ago=REFUSAL_RECHECK_DAYS + 1)

        store = PostgresRefusalStore(SingleConnectionPool(streamers_table))
        marks = store.refusals([1, 2, 3])

        assert 1 not in marks           # no mark at all -- attempt it
        assert marks[2] is False        # mark stands -- skip it
        assert marks[3] is True         # stale -- one retry

    def test_the_boundary_sits_at_seven_days(self, streamers_table):
        """An hour short of the interval still stands; an hour past is stale.

        Pins the interval itself, not just that some interval exists. Exactly
        at the boundary is not testable against a live clock: `NOW()` moves
        between the insert and the read, so the row is always a few
        milliseconds older than the offset it was written with.
        """
        with streamers_table.cursor() as cur:
            cur.execute(
                "INSERT INTO streamers (streamer_id, streamer_login, eventsub_refused_at) "
                "VALUES (1, 'just_inside',  NOW() - make_interval(hours => %s)), "
                "       (2, 'just_outside', NOW() - make_interval(hours => %s))",
                (REFUSAL_RECHECK_DAYS * 24 - 1, REFUSAL_RECHECK_DAYS * 24 + 1),
            )
        streamers_table.commit()

        store = PostgresRefusalStore(SingleConnectionPool(streamers_table))
        marks = store.refusals([1, 2])
        assert marks[1] is False
        assert marks[2] is True

    def test_mark_and_clear_round_trip(self, streamers_table):
        add_streamer(streamers_table, 1)
        store = PostgresRefusalStore(SingleConnectionPool(streamers_table))

        store.mark_refused(1)
        assert store.refusals([1]) == {1: False}

        store.clear_refusal(1)
        assert store.refusals([1]) == {}

    def test_a_fresh_refusal_resets_a_stale_timestamp(self, streamers_table):
        """The retry that refuses again restarts the seven days."""
        add_streamer(streamers_table, 1, refused_days_ago=REFUSAL_RECHECK_DAYS + 5)
        store = PostgresRefusalStore(SingleConnectionPool(streamers_table))
        assert store.refusals([1])[1] is True

        store.mark_refused(1)
        assert store.refusals([1])[1] is False

    def test_an_unknown_id_is_simply_absent(self, streamers_table):
        store = PostgresRefusalStore(SingleConnectionPool(streamers_table))
        assert store.refusals([999]) == {}
        assert store.refusals([]) == {}


class TestClippingRecheckAgainstPostgres:
    """T025c / FR-013 -- a stale `allows_clipping = FALSE` re-enters ranking."""

    def test_stale_disabled_streamers_re_enter_the_ranking(self, streamers_table):
        add_streamer(streamers_table, 1)                                   # allowed
        add_streamer(streamers_table, 2, allows_clipping=False, disabled_days_ago=1)
        add_streamer(
            streamers_table, 3, allows_clipping=False,
            disabled_days_ago=stream_monitoring_service.CLIPPING_RECHECK_DAYS + 1,
        )

        service = StreamMonitoringService()
        service.db_pool = SingleConnectionPool(streamers_table)

        # 3 is stale, so it is NOT in the disabled set and is ranked again.
        assert service._get_clipping_disabled_ids([1, 2, 3]) == {2}

    def test_a_row_with_no_timestamp_stays_disabled(self, streamers_table):
        """A FALSE flag with no timestamp predates the migration backfill.

        No timestamp is no evidence the mark is stale, so it keeps the skip
        rather than handing every legacy row a retry at once.
        """
        add_streamer(streamers_table, 1, allows_clipping=False, disabled_at_null=True)
        service = StreamMonitoringService()
        service.db_pool = SingleConnectionPool(streamers_table)
        assert service._get_clipping_disabled_ids([1]) == {1}

    def test_no_ids_costs_no_query(self, streamers_table):
        service = StreamMonitoringService()
        service.db_pool = SingleConnectionPool(streamers_table)
        assert service._get_clipping_disabled_ids([]) == set()


class TestScopeGuard:
    """T017 -- a token without `user:read:chat` must not poison the cache."""

    def test_the_refusal_cache_is_off_without_the_chat_scope(self):
        """Every channel refuses for one reason that is not the broadcasters'.

        Persisting those refusals would mark the whole monitored set and skip
        it for seven days, turning a token mistake into a week-long outage.
        """

        service = StreamMonitoringService()
        service.db_pool = MagicMock()

        service.has_chat_scope = False
        assert service._build_refusal_store() is None

        service.has_chat_scope = True
        assert isinstance(service._build_refusal_store(), PostgresRefusalStore)

    def test_the_scope_map_covers_every_seeded_scope(self):
        """A scope in the seed script but not in the map is silently dropped,
        and the feature that needs it fails at run time instead of at start."""
        seeded = {"user:read:chat", "clips:edit"}
        assert seeded <= set(stream_monitoring_service.SCOPE_MAP)
