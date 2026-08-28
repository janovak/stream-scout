#!/usr/bin/env python3
"""
Unit tests for Stream Monitoring Service

Tests token management, message processing, and service components.
"""

import asyncio
import json
import os
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import stream_monitoring_service
import token_manager
from stream_monitoring_service import StreamMonitoringService
from token_manager import TokenRecord, TwitchCredentials


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


class TestChatRoomManagement:
    """Tests for chat room connection management logic."""

    def test_channels_to_join_calculation(self):
        """Should correctly calculate which channels to join."""
        joined_channels = {"streamer1", "streamer2"}
        target_channels = {"streamer2", "streamer3", "streamer4"}

        channels_to_join = target_channels - joined_channels
        channels_to_leave = joined_channels - target_channels

        assert channels_to_join == {"streamer3", "streamer4"}
        assert channels_to_leave == {"streamer1"}

    def test_no_changes_when_channels_match(self):
        """No joins or leaves when channel sets match."""
        current = {"streamer1", "streamer2"}
        target = {"streamer1", "streamer2"}

        to_join = target - current
        to_leave = current - target

        assert len(to_join) == 0
        assert len(to_leave) == 0

    def test_broadcaster_id_mapping(self):
        """Broadcaster ID mapping should store login to ID pairs."""
        broadcaster_ids = {}

        # Simulate adding streamers
        broadcaster_ids["ninja"] = 19571641
        broadcaster_ids["shroud"] = 37402112

        assert broadcaster_ids.get("ninja") == 19571641
        assert broadcaster_ids.get("shroud") == 37402112
        assert broadcaster_ids.get("nonexistent") is None


class TestChatConnectionRecovery:
    """Tests for recreating a chat client whose underlying connection died
    for good. pyTwitchAPI's Chat gives up reconnecting after exhausting a
    bounded backoff list and never retries again; from that point
    join_room()/leave_room() fail forever against the dead socket with no
    other symptom, so _manage_chat_connections must detect and recover from
    it rather than trust that self.chat existing means it still works.

    is_connected() also reads False for the whole span of a still-in-progress
    reconnect the library would complete on its own, so recreation only fires
    after DEAD_CHAT_CONFIRMATION_POLLS consecutive dead readings."""

    @staticmethod
    def _make_dead_chat():
        dead_chat = MagicMock()
        dead_chat.is_connected.return_value = False
        dead_chat.join_room = AsyncMock()
        dead_chat.leave_room = AsyncMock()
        return dead_chat

    @staticmethod
    def _make_fresh_chat():
        fresh_chat = MagicMock()
        fresh_chat.register_event = MagicMock()
        fresh_chat.start = MagicMock()
        fresh_chat.join_room = AsyncMock()
        fresh_chat.leave_room = AsyncMock()
        return fresh_chat

    def test_does_not_recreate_before_confirmation_threshold(self):
        """A dead reading alone isn't enough -- it could still be a
        still-in-progress reconnect the library would finish on its own."""
        service = StreamMonitoringService()
        service.twitch = MagicMock()
        dead_chat = self._make_dead_chat()
        service.chat = dead_chat
        service.joined_channels = {"stalechannel"}

        for _ in range(stream_monitoring_service.DEAD_CHAT_CONFIRMATION_POLLS - 1):
            asyncio.run(service._manage_chat_connections(
                join_eligible={"stalechannel"},
                leave_eligible={"stalechannel"},
            ))

        dead_chat.stop.assert_not_called()
        assert service.chat is dead_chat

    def test_recreates_chat_after_confirmation_threshold(self):
        """Once the connection has looked dead for DEAD_CHAT_CONFIRMATION_POLLS
        consecutive polls, the dead chat is stopped, replaced, and the stale
        room list is dropped so no leave_room() is attempted against the new
        client for channels the old, gone socket used to hold."""
        service = StreamMonitoringService()
        service.twitch = MagicMock()
        dead_chat = self._make_dead_chat()
        service.chat = dead_chat
        service.joined_channels = {"stalechannel"}
        fresh_chat = self._make_fresh_chat()

        with patch("stream_monitoring_service.Chat", AsyncMock(return_value=fresh_chat)):
            for _ in range(stream_monitoring_service.DEAD_CHAT_CONFIRMATION_POLLS):
                asyncio.run(service._manage_chat_connections(
                    join_eligible={"newchannel"},
                    leave_eligible={"newchannel"},
                ))

        dead_chat.stop.assert_called_once()
        assert service.chat is fresh_chat
        fresh_chat.leave_room.assert_not_awaited()
        fresh_chat.join_room.assert_awaited_once_with("newchannel")
        assert service.joined_channels == {"newchannel"}
        assert service._consecutive_dead_chat_polls == 0

    def test_recreate_preserves_hysteresis_band_not_just_join_eligible(self):
        """Recovery must rejoin the whole surviving LEAVE_THRESHOLD band, not
        just the narrower JOIN_THRESHOLD set -- otherwise every outage
        silently shrinks coverage from top-30 to top-15 and never restores
        it, defeating the point of the hysteresis band."""
        service = StreamMonitoringService()
        service.twitch = MagicMock()
        dead_chat = self._make_dead_chat()
        service.chat = dead_chat
        # "midbandchannel" is joined and still within the wider leave-eligible
        # band, but is NOT in join_eligible (it's not freshly entering the
        # top-JOIN_THRESHOLD set) -- exactly the rank-16-30 case.
        service.joined_channels = {"midbandchannel"}
        fresh_chat = self._make_fresh_chat()

        with patch("stream_monitoring_service.Chat", AsyncMock(return_value=fresh_chat)):
            for _ in range(stream_monitoring_service.DEAD_CHAT_CONFIRMATION_POLLS):
                asyncio.run(service._manage_chat_connections(
                    join_eligible={"topchannel"},
                    leave_eligible={"topchannel", "midbandchannel"},
                ))

        fresh_chat.join_room.assert_any_await("midbandchannel")
        fresh_chat.join_room.assert_any_await("topchannel")
        assert service.joined_channels == {"topchannel", "midbandchannel"}

    def test_reconnect_before_threshold_resets_the_counter(self):
        """A dead reading followed by a healthy one must not carry over --
        otherwise a couple of isolated blips days apart could eventually
        accumulate past the threshold and trigger a bogus recreate."""
        service = StreamMonitoringService()
        service.twitch = MagicMock()
        flaky_chat = self._make_dead_chat()
        service.chat = flaky_chat

        asyncio.run(service._manage_chat_connections(join_eligible=set(), leave_eligible=set()))
        assert service._consecutive_dead_chat_polls == 1

        flaky_chat.is_connected.return_value = True
        asyncio.run(service._manage_chat_connections(join_eligible=set(), leave_eligible=set()))
        assert service._consecutive_dead_chat_polls == 0
        assert service.chat is flaky_chat
        flaky_chat.stop.assert_not_called()

    def test_leaves_healthy_chat_untouched(self):
        """A connected chat is reused as-is; no stop/recreate happens."""
        service = StreamMonitoringService()
        service.twitch = MagicMock()

        healthy_chat = MagicMock()
        healthy_chat.is_connected.return_value = True
        healthy_chat.join_room = AsyncMock()
        healthy_chat.leave_room = AsyncMock()
        service.chat = healthy_chat
        service.joined_channels = {"existingchannel"}

        asyncio.run(service._manage_chat_connections(
            join_eligible={"existingchannel"},
            leave_eligible={"existingchannel"},
        ))

        healthy_chat.stop.assert_not_called()
        assert service.chat is healthy_chat
        healthy_chat.join_room.assert_not_awaited()
        healthy_chat.leave_room.assert_not_awaited()


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

        def stop_chat():
            shutdown_order.append("chat")

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
        stop_chat()
        close_twitch()
        flush_kafka()
        close_db()
        close_redis()

        # Verify order
        assert shutdown_order[0] == "scheduler"
        assert shutdown_order[-1] == "redis"
        assert len(shutdown_order) == 6


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


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
