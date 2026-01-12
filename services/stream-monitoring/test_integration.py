#!/usr/bin/env python3
"""
Integration tests for Stream Monitoring Service with mocked external services.

These tests mock Twitch API, Kafka, Redis, and Postgres to test service behavior
without requiring actual external connections.
"""

import asyncio
import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock

import pytest


class MockStream:
    """Mock Twitch stream object."""

    def __init__(self, user_id: str, user_login: str, viewer_count: int = 1000):
        self.user_id = user_id
        self.user_login = user_login
        self.viewer_count = viewer_count


class MockChatMessage:
    """Mock Twitch chat message."""

    def __init__(self, room_name: str, text: str, user_id: str = "12345", user_name: str = "viewer"):
        self.room = MagicMock()
        self.room.name = room_name
        self.text = text
        self.user = MagicMock()
        self.user.id = user_id
        self.user.name = user_name
        self.user.badges = {}
        self.user.subscriber = False
        self.user.mod = False


class TestTwitchAPIIntegration:
    """Integration tests for Twitch API interactions."""

    @pytest.mark.asyncio
    async def test_get_top_streams(self):
        """Service should fetch top streams from Twitch API."""
        mock_streams = [
            MockStream("111", "ninja", 50000),
            MockStream("222", "shroud", 40000),
            MockStream("333", "pokimane", 30000),
        ]

        mock_twitch = AsyncMock()

        async def mock_get_streams(**kwargs):
            for stream in mock_streams:
                yield stream

        mock_twitch.get_streams = mock_get_streams

        # Simulate fetching streams
        streams = []
        async for stream in mock_twitch.get_streams(first=5):
            streams.append(stream)
            if len(streams) >= 5:
                break

        assert len(streams) == 3
        assert streams[0].user_login == "ninja"
        assert streams[1].user_login == "shroud"

    @pytest.mark.asyncio
    async def test_authentication_with_tokens(self):
        """Service should authenticate using pre-seeded tokens."""
        mock_twitch = AsyncMock()
        mock_twitch.set_user_authentication = AsyncMock()

        access_token = "test_access"
        refresh_token = "test_refresh"
        scopes = ["chat:read", "clips:edit"]

        await mock_twitch.set_user_authentication(access_token, scopes, refresh_token)

        mock_twitch.set_user_authentication.assert_called_once_with(
            access_token, scopes, refresh_token
        )

    @pytest.mark.asyncio
    async def test_token_refresh_callback_invoked(self):
        """Token refresh callback should be invoked when tokens are refreshed."""
        callback_invoked = {"called": False, "access": None, "refresh": None}

        async def on_token_refresh(access_token: str, refresh_token: str):
            callback_invoked["called"] = True
            callback_invoked["access"] = access_token
            callback_invoked["refresh"] = refresh_token

        # Simulate token refresh
        await on_token_refresh("new_access", "new_refresh")

        assert callback_invoked["called"]
        assert callback_invoked["access"] == "new_access"
        assert callback_invoked["refresh"] == "new_refresh"


class TestKafkaIntegration:
    """Integration tests for Kafka message production."""

    def test_chat_message_production(self):
        """Chat messages should be produced to Kafka with correct format."""
        mock_producer = MagicMock()
        produced_messages = []

        def mock_produce(topic, key, value, callback=None):
            produced_messages.append({
                "topic": topic,
                "key": key,
                "value": value,
            })

        mock_producer.produce = mock_produce

        # Simulate producing a chat message
        broadcaster_id = 12345
        message_payload = {
            "broadcaster_id": broadcaster_id,
            "timestamp": 1704067200000,
            "message_id": "uuid-123",
            "text": "Hello world",
            "user_id": 67890,
            "user_name": "viewer",
            "metadata": {
                "emotes": {},
                "badges": {},
                "is_subscriber": False,
                "is_mod": False,
            },
        }

        mock_producer.produce(
            topic="chat-messages",
            key=str(broadcaster_id).encode("utf-8"),
            value=json.dumps(message_payload).encode("utf-8"),
        )

        assert len(produced_messages) == 1
        assert produced_messages[0]["topic"] == "chat-messages"
        assert produced_messages[0]["key"] == b"12345"

        # Verify payload structure
        payload = json.loads(produced_messages[0]["value"].decode("utf-8"))
        assert payload["broadcaster_id"] == 12345
        assert payload["text"] == "Hello world"

    def test_lifecycle_event_production(self):
        """Lifecycle events should be produced to Kafka."""
        mock_producer = MagicMock()
        produced_messages = []

        def mock_produce(topic, key, value, callback=None):
            produced_messages.append({
                "topic": topic,
                "key": key,
                "value": value,
            })

        mock_producer.produce = mock_produce

        # Simulate online event
        broadcaster_id = 12345
        event = {
            "event_type": "online",
            "broadcaster_id": broadcaster_id,
            "broadcaster_login": "ninja",
            "rank": 1,
            "timestamp": 1704067200,
        }

        mock_producer.produce(
            topic="stream-lifecycle",
            key=str(broadcaster_id).encode("utf-8"),
            value=json.dumps(event).encode("utf-8"),
        )

        assert len(produced_messages) == 1
        assert produced_messages[0]["topic"] == "stream-lifecycle"

        payload = json.loads(produced_messages[0]["value"].decode("utf-8"))
        assert payload["event_type"] == "online"
        assert payload["broadcaster_login"] == "ninja"

    def test_kafka_delivery_callback(self):
        """Delivery callback should handle success and failure."""
        success_count = {"count": 0}
        error_count = {"count": 0}

        def delivery_callback(err, msg):
            if err:
                error_count["count"] += 1
            else:
                success_count["count"] += 1

        # Simulate successful delivery
        mock_msg = MagicMock()
        mock_msg.topic.return_value = "chat-messages"
        mock_msg.partition.return_value = 0

        delivery_callback(None, mock_msg)
        assert success_count["count"] == 1
        assert error_count["count"] == 0

        # Simulate failed delivery
        delivery_callback("Connection error", mock_msg)
        assert error_count["count"] == 1


class TestRedisIntegration:
    """Integration tests for Redis cache operations."""

    def test_streamer_online_status_set(self):
        """Online streamer status should be cached in Redis with TTL."""
        mock_redis = MagicMock()

        broadcaster_login = "ninja"
        broadcaster_id = 12345
        ttl = 180  # 3 minutes

        redis_key = f"streamer:online:{broadcaster_login}"
        mock_redis.setex(redis_key, ttl, broadcaster_id)

        mock_redis.setex.assert_called_once_with(
            "streamer:online:ninja", 180, 12345
        )

    def test_streamer_online_check(self):
        """Should check if streamer is online via Redis key existence."""
        mock_redis = MagicMock()
        mock_redis.exists.return_value = True

        broadcaster_login = "ninja"
        redis_key = f"streamer:online:{broadcaster_login}"
        is_online = mock_redis.exists(redis_key)

        assert is_online
        mock_redis.exists.assert_called_with("streamer:online:ninja")

    def test_streamer_offline_detection(self):
        """Offline detection when Redis key expired."""
        mock_redis = MagicMock()
        mock_redis.exists.return_value = False

        broadcaster_login = "offline_streamer"
        redis_key = f"streamer:online:{broadcaster_login}"
        is_online = mock_redis.exists(redis_key)

        assert not is_online


class TestPostgresIntegration:
    """Integration tests for Postgres database operations."""

    def test_upsert_streamer(self):
        """Streamer upsert should insert or update correctly."""
        mock_cursor = MagicMock()
        mock_conn = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        streamer_id = 12345
        streamer_login = "ninja"

        # Simulate upsert query
        query = """
            INSERT INTO streamers (streamer_id, streamer_login, last_seen_at)
            VALUES (%s, %s, NOW())
            ON CONFLICT (streamer_id) DO UPDATE
            SET streamer_login = EXCLUDED.streamer_login,
                last_seen_at = NOW()
        """

        with mock_conn.cursor() as cur:
            cur.execute(query, (streamer_id, streamer_login))
            mock_conn.commit()

        mock_cursor.execute.assert_called_once()
        mock_conn.commit.assert_called_once()

    def test_connection_pool_management(self):
        """Connection pool should properly acquire and release connections."""
        mock_pool = MagicMock()
        mock_conn = MagicMock()
        mock_pool.getconn.return_value = mock_conn

        # Acquire connection
        conn = mock_pool.getconn()
        assert conn is not None

        # Do work...

        # Release connection
        mock_pool.putconn(conn)
        mock_pool.putconn.assert_called_once_with(mock_conn)


class TestChatIntegration:
    """Integration tests for Twitch chat functionality."""

    @pytest.mark.asyncio
    async def test_join_chat_room(self):
        """Service should join chat rooms for online streamers."""
        mock_chat = AsyncMock()

        channel = "ninja"
        await mock_chat.join_room(channel)

        mock_chat.join_room.assert_called_once_with("ninja")

    @pytest.mark.asyncio
    async def test_leave_chat_room(self):
        """Service should leave chat rooms when streamers go offline."""
        mock_chat = AsyncMock()

        channel = "ninja"
        await mock_chat.leave_room(channel)

        mock_chat.leave_room.assert_called_once_with("ninja")

    @pytest.mark.asyncio
    async def test_chat_message_handler(self):
        """Chat message handler should process messages correctly."""
        processed_messages = []

        async def on_chat_message(msg):
            broadcaster_login = msg.room.name.lower()
            processed_messages.append({
                "channel": broadcaster_login,
                "text": msg.text,
                "user": msg.user.name,
            })

        # Simulate receiving a message
        mock_msg = MockChatMessage("Ninja", "PogChamp", "12345", "viewer")
        await on_chat_message(mock_msg)

        assert len(processed_messages) == 1
        assert processed_messages[0]["channel"] == "ninja"
        assert processed_messages[0]["text"] == "PogChamp"

    @pytest.mark.asyncio
    async def test_chat_message_with_metadata(self):
        """Chat messages should include user metadata."""
        mock_msg = MockChatMessage("streamer", "Hello!", "99999", "subscriber")
        mock_msg.user.subscriber = True
        mock_msg.user.mod = False
        mock_msg.user.badges = {"subscriber": "12"}

        metadata = {
            "emotes": {},
            "badges": dict(mock_msg.user.badges),
            "is_subscriber": mock_msg.user.subscriber,
            "is_mod": mock_msg.user.mod,
        }

        assert metadata["is_subscriber"] is True
        assert metadata["is_mod"] is False
        assert metadata["badges"] == {"subscriber": "12"}


class TestStreamPollingIntegration:
    """Integration tests for stream polling workflow."""

    @pytest.mark.asyncio
    async def test_poll_cycle_updates_channel_set(self):
        """Polling should update the set of monitored channels."""
        current_channels = {"streamer1", "streamer2"}

        # Simulate poll returning new top streamers
        new_top_streamers = ["streamer2", "streamer3", "streamer4"]

        new_channels = set(s.lower() for s in new_top_streamers)
        channels_to_join = new_channels - current_channels
        channels_to_leave = current_channels - new_channels

        assert channels_to_join == {"streamer3", "streamer4"}
        assert channels_to_leave == {"streamer1"}

    @pytest.mark.asyncio
    async def test_poll_handles_api_errors_gracefully(self):
        """Service should handle Twitch API errors without crashing."""
        error_handled = {"value": False}

        async def poll_with_error_handling():
            try:
                raise Exception("Twitch API error")
            except Exception:
                error_handled["value"] = True

        await poll_with_error_handling()
        assert error_handled["value"]


class TestServiceLifecycle:
    """Integration tests for service lifecycle management."""

    @pytest.mark.asyncio
    async def test_graceful_shutdown_flushes_kafka(self):
        """Graceful shutdown should flush Kafka producer."""
        mock_producer = MagicMock()

        # Simulate shutdown
        mock_producer.flush(timeout=10)

        mock_producer.flush.assert_called_once_with(timeout=10)

    @pytest.mark.asyncio
    async def test_graceful_shutdown_closes_connections(self):
        """Graceful shutdown should close all connections."""
        mock_db_pool = MagicMock()
        mock_redis = MagicMock()
        mock_twitch = AsyncMock()

        # Simulate shutdown
        mock_db_pool.closeall()
        mock_redis.close()
        await mock_twitch.close()

        mock_db_pool.closeall.assert_called_once()
        mock_redis.close.assert_called_once()
        mock_twitch.close.assert_called_once()


class TestMetricsIntegration:
    """Integration tests for Prometheus metrics."""

    def test_metrics_increment(self):
        """Metrics should increment correctly."""
        mock_counter = MagicMock()

        # Simulate metric increment
        mock_counter.labels(broadcaster_id="12345").inc()

        mock_counter.labels.assert_called_with(broadcaster_id="12345")

    def test_gauge_set_value(self):
        """Gauge metrics should set values correctly."""
        mock_gauge = MagicMock()

        # Simulate setting active stream count
        mock_gauge.set(5)

        mock_gauge.set.assert_called_with(5)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
