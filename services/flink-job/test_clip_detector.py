#!/usr/bin/env python3
"""
Unit tests for Clip Detector Job

Tests the anomaly detection logic, command filtering, and clip creation flow.
"""

import json
from unittest.mock import MagicMock, patch

import pytest

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
        # First call for token refresh
        mock_post.return_value.status_code = 200
        mock_post.return_value.json.return_value = {
            "access_token": "new_token",
            "expires_in": 3600,
        }

        client = TwitchAPIClient("client_id", "client_secret", token_file, validate_on_init=False)
        client.access_token = None  # Force token refresh

        client._refresh()

        assert client.access_token == "new_token"


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
TEST_POSTGRES_URL = os.getenv(
    "TEST_POSTGRES_URL", "postgresql://twitch:twitch_password@100.112.97.111:5432/twitch"
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
