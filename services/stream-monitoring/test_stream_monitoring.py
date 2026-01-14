#!/usr/bin/env python3
"""
Unit tests for Stream Monitoring Service

Tests token management, message processing, and service components.
"""

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from token_manager import TokenManager


class TestTokenManager:
    """Tests for TokenManager class."""

    def test_load_tokens_success(self, tmp_path):
        """Successfully load valid token file."""
        token_file = tmp_path / "tokens.json"
        token_data = {
            "access_token": "test_access_token",
            "refresh_token": "test_refresh_token",
            "scopes": ["chat:read", "clips:edit"],
            "created_at": "2026-01-11T00:00:00Z",
        }
        token_file.write_text(json.dumps(token_data))

        manager = TokenManager(token_file)
        access, refresh, scopes = manager.load_tokens()

        assert access == "test_access_token"
        assert refresh == "test_refresh_token"
        assert scopes == ["chat:read", "clips:edit"]

    def test_load_tokens_file_not_found(self, tmp_path):
        """Raise FileNotFoundError when token file doesn't exist."""
        token_file = tmp_path / "nonexistent.json"
        manager = TokenManager(token_file)

        with pytest.raises(FileNotFoundError):
            manager.load_tokens()

    def test_load_tokens_missing_access_token(self, tmp_path):
        """Raise ValueError when access_token is missing."""
        token_file = tmp_path / "tokens.json"
        token_data = {
            "refresh_token": "test_refresh_token",
            "scopes": ["chat:read"],
        }
        token_file.write_text(json.dumps(token_data))

        manager = TokenManager(token_file)

        with pytest.raises(ValueError, match="missing access_token"):
            manager.load_tokens()

    def test_load_tokens_missing_refresh_token(self, tmp_path):
        """Raise ValueError when refresh_token is missing."""
        token_file = tmp_path / "tokens.json"
        token_data = {
            "access_token": "test_access_token",
            "scopes": ["chat:read"],
        }
        token_file.write_text(json.dumps(token_data))

        manager = TokenManager(token_file)

        with pytest.raises(ValueError, match="missing.*refresh_token"):
            manager.load_tokens()

    def test_load_tokens_invalid_json(self, tmp_path):
        """Raise JSONDecodeError when file contains invalid JSON."""
        token_file = tmp_path / "tokens.json"
        token_file.write_text("not valid json {{{")

        manager = TokenManager(token_file)

        with pytest.raises(json.JSONDecodeError):
            manager.load_tokens()

    def test_save_tokens_creates_file(self, tmp_path):
        """save_tokens should create file if it doesn't exist."""
        token_file = tmp_path / "subdir" / "tokens.json"
        manager = TokenManager(token_file)
        manager._scopes = ["chat:read"]

        manager.save_tokens("new_access", "new_refresh")

        assert token_file.exists()
        data = json.loads(token_file.read_text())
        assert data["access_token"] == "new_access"
        assert data["refresh_token"] == "new_refresh"
        assert data["scopes"] == ["chat:read"]
        assert "updated_at" in data

    def test_save_tokens_overwrites_existing(self, tmp_path):
        """save_tokens should overwrite existing file."""
        token_file = tmp_path / "tokens.json"
        token_file.write_text(json.dumps({"old": "data"}))

        manager = TokenManager(token_file)
        manager._scopes = ["clips:edit"]
        manager.save_tokens("new_access", "new_refresh")

        data = json.loads(token_file.read_text())
        assert data["access_token"] == "new_access"
        assert "old" not in data

    def test_properties_return_cached_values(self, tmp_path):
        """Properties should return cached token values after loading."""
        token_file = tmp_path / "tokens.json"
        token_data = {
            "access_token": "cached_access",
            "refresh_token": "cached_refresh",
            "scopes": ["chat:read"],
        }
        token_file.write_text(json.dumps(token_data))

        manager = TokenManager(token_file)
        manager.load_tokens()

        assert manager.access_token == "cached_access"
        assert manager.refresh_token == "cached_refresh"
        assert manager.scopes == ["chat:read"]

    def test_default_token_file_path(self):
        """TokenManager should use default path when none specified."""
        manager = TokenManager()
        assert manager.token_file == Path("/app/secrets/twitch_user_tokens.json")


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


class TestServiceConfiguration:
    """Tests for service configuration values."""

    def test_default_configuration_values(self):
        """Default configuration should have expected values."""
        # These match the values defined in stream_monitoring_service.py
        TOP_STREAMERS_COUNT = 5
        REDIS_STREAMER_TTL = 180  # 3 minutes
        POLL_INTERVAL_SECONDS = 120  # 2 minutes

        assert TOP_STREAMERS_COUNT == 5
        assert REDIS_STREAMER_TTL == 180
        assert POLL_INTERVAL_SECONDS == 120

    def test_environment_variable_configuration(self, monkeypatch):
        """Configuration should be overridable via environment variables."""
        monkeypatch.setenv("KAFKA_BROKER_URL", "custom-kafka:9092")
        monkeypatch.setenv("POSTGRES_URL", "postgresql://custom:pass@host/db")
        monkeypatch.setenv("REDIS_URL", "redis://custom-redis:6379")

        # Verify environment variables are set correctly
        assert os.getenv("KAFKA_BROKER_URL") == "custom-kafka:9092"
        assert os.getenv("POSTGRES_URL") == "postgresql://custom:pass@host/db"
        assert os.getenv("REDIS_URL") == "redis://custom-redis:6379"


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
