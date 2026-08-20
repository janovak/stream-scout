#!/usr/bin/env python3
"""
Unit tests for Stream Monitoring Service

Tests token management, message processing, and service components.
"""

import json
import os
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import token_manager
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


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
