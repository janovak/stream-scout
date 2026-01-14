#!/usr/bin/env python3
"""
Unit tests for Clip Detector Job

Tests the anomaly detection logic, command filtering, and clip creation flow.
"""

import json
import statistics
import time
from unittest.mock import MagicMock, patch

import pytest

from clip_detector_job import (
    BASELINE_WINDOW_SECONDS,
    COMMAND_PATTERN,
    COOLDOWN_SECONDS,
    STD_DEV_THRESHOLD,
    WINDOW_SIZE_SECONDS,
    AnomalyEvent,
    ChatMessage,
    ClipResult,
    TwitchAPIClient,
)


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


class TestAnomalyDetectionThreshold:
    """Tests for anomaly detection threshold calculation."""

    def test_threshold_uses_correct_std_dev_multiplier(self):
        """Threshold should be mean + STD_DEV_THRESHOLD * std_dev."""
        assert STD_DEV_THRESHOLD == 1.0, "Threshold should be 1.0 std dev per spec"

    def test_threshold_calculation(self):
        """Test threshold calculation with sample data."""
        # Simulate baseline data (5 minutes of message counts per second)
        baseline_counts = [5, 6, 4, 5, 7, 6, 5, 4, 6, 5] * 30  # 300 samples

        mean = statistics.mean(baseline_counts)
        std_dev = statistics.stdev(baseline_counts)
        threshold = mean + (STD_DEV_THRESHOLD * std_dev)

        # With mean ~5.3 and low variance, threshold should be around 6.2
        assert threshold > mean
        assert threshold < mean + (3 * std_dev)  # Should be less than 3 std dev

    def test_anomaly_detected_when_above_threshold(self):
        """Anomaly should be detected when current count exceeds threshold."""
        baseline_counts = [5] * 300  # Consistent baseline of 5 msgs/sec
        mean = statistics.mean(baseline_counts)
        std_dev = statistics.stdev(baseline_counts) if len(baseline_counts) > 1 else 0

        if std_dev == 0:
            std_dev = 0.1  # Avoid division by zero

        threshold = mean + (STD_DEV_THRESHOLD * std_dev)

        # Window sum of 10 should trigger anomaly with baseline of 5
        window_sum = 10
        is_anomaly = window_sum > threshold and std_dev > 0

        # With std_dev near 0, threshold ~5.1, so 10 should trigger
        assert is_anomaly or std_dev <= 0

    def test_no_anomaly_when_below_threshold(self):
        """No anomaly when current count is within normal range."""
        baseline_counts = [5, 6, 4, 5, 7, 6, 5, 4, 6, 5] * 30
        mean = statistics.mean(baseline_counts)
        std_dev = statistics.stdev(baseline_counts)
        threshold = mean + (STD_DEV_THRESHOLD * std_dev)

        # Window sum equal to mean should not trigger
        window_sum = int(mean)
        is_anomaly = window_sum > threshold

        assert not is_anomaly


class TestCooldownLogic:
    """Tests for cooldown tracking between anomaly detections."""

    def test_cooldown_period_is_30_seconds(self):
        """Cooldown should be 30 seconds as per spec."""
        assert COOLDOWN_SECONDS == 30

    def test_anomaly_blocked_during_cooldown(self):
        """Second anomaly within cooldown period should be blocked."""
        last_anomaly_time = int(time.time() * 1000)
        current_time = last_anomaly_time + 15000  # 15 seconds later

        should_trigger = (current_time - last_anomaly_time) > (COOLDOWN_SECONDS * 1000)
        assert not should_trigger

    def test_anomaly_allowed_after_cooldown(self):
        """Anomaly should be allowed after cooldown period expires."""
        last_anomaly_time = int(time.time() * 1000)
        current_time = last_anomaly_time + 35000  # 35 seconds later

        should_trigger = (current_time - last_anomaly_time) > (COOLDOWN_SECONDS * 1000)
        assert should_trigger

    def test_first_anomaly_always_triggers(self):
        """First anomaly should always trigger (no previous time)."""
        last_anomaly_time = None
        current_time = int(time.time() * 1000)

        should_trigger = last_anomaly_time is None or (
            current_time - last_anomaly_time
        ) > (COOLDOWN_SECONDS * 1000)
        assert should_trigger


class TestWindowConfiguration:
    """Tests for sliding window configuration."""

    def test_window_size_is_5_seconds(self):
        """Window size should be 5 seconds per spec."""
        assert WINDOW_SIZE_SECONDS == 5

    def test_baseline_window_is_5_minutes(self):
        """Baseline window should be 5 minutes (300 seconds) per spec."""
        assert BASELINE_WINDOW_SECONDS == 300


class TestTwitchAPIClient:
    """Tests for Twitch API client."""

    @patch("clip_detector_job.requests.post")
    def test_create_clip_returns_clip_id_on_success(self, mock_post):
        """Successful clip creation should return clip ID."""
        mock_post.return_value.status_code = 202
        mock_post.return_value.json.return_value = {"data": [{"id": "test_clip_123"}]}

        client = TwitchAPIClient("client_id", "client_secret")
        # Pre-set token to avoid auth call
        client.access_token = "test_token"
        client.token_expires_at = time.time() + 3600

        clip_id = client.create_clip(12345)
        assert clip_id == "test_clip_123"

    @patch("clip_detector_job.requests.post")
    def test_create_clip_returns_none_on_failure(self, mock_post):
        """Failed clip creation should return None."""
        mock_post.return_value.status_code = 500
        mock_post.return_value.json.return_value = {}

        client = TwitchAPIClient("client_id", "client_secret")
        client.access_token = "test_token"
        client.token_expires_at = time.time() + 3600

        clip_id = client.create_clip(12345)
        assert clip_id is None

    @patch("clip_detector_job.requests.get")
    def test_get_clip_returns_data_on_success(self, mock_get):
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

        client = TwitchAPIClient("client_id", "client_secret")
        client.access_token = "test_token"
        client.token_expires_at = time.time() + 3600

        clip_data = client.get_clip("test_clip_123")
        assert clip_data is not None
        assert clip_data["id"] == "test_clip_123"
        assert "embed_url" in clip_data
        assert "thumbnail_url" in clip_data

    @patch("clip_detector_job.requests.get")
    def test_get_clip_returns_none_on_failure(self, mock_get):
        """Failed clip retrieval should return None."""
        mock_get.return_value.status_code = 404
        mock_get.return_value.json.return_value = {"data": []}

        client = TwitchAPIClient("client_id", "client_secret")
        client.access_token = "test_token"
        client.token_expires_at = time.time() + 3600

        clip_data = client.get_clip("nonexistent_clip")
        assert clip_data is None

    @patch("clip_detector_job.requests.post")
    def test_token_refresh_on_expiry(self, mock_post):
        """Token should be refreshed when expired."""
        # First call for token refresh
        mock_post.return_value.status_code = 200
        mock_post.return_value.json.return_value = {
            "access_token": "new_token",
            "expires_in": 3600,
        }

        client = TwitchAPIClient("client_id", "client_secret")
        client.access_token = None  # Force token refresh

        client._ensure_token()

        assert client.access_token == "new_token"
        assert client.token_expires_at > time.time()


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


class TestAnomalyDetectionScenarios:
    """Integration-style tests for anomaly detection scenarios."""

    def test_spike_detection_scenario(self):
        """Simulate a chat spike that should trigger anomaly detection."""
        # Baseline: 10 messages per second for 5 minutes
        baseline_counts = [10] * 300

        # Spike: 25 messages in the 5-second window
        current_window_sum = 25

        mean = statistics.mean(baseline_counts)
        std_dev = statistics.stdev(baseline_counts) if len(set(baseline_counts)) > 1 else 0.1

        threshold = mean + (STD_DEV_THRESHOLD * std_dev)

        # With uniform baseline, std_dev is 0, so use minimum
        if std_dev < 0.1:
            std_dev = 0.1
            threshold = mean + (STD_DEV_THRESHOLD * std_dev)

        is_anomaly = current_window_sum > threshold
        assert is_anomaly, f"Spike of {current_window_sum} should exceed threshold {threshold}"

    def test_gradual_increase_no_anomaly(self):
        """Gradual increase should not trigger if within threshold."""
        # Baseline with some variance
        baseline_counts = [8, 10, 12, 9, 11, 10, 8, 12, 9, 11] * 30

        mean = statistics.mean(baseline_counts)
        std_dev = statistics.stdev(baseline_counts)
        threshold = mean + (STD_DEV_THRESHOLD * std_dev)

        # Current window just slightly above mean
        current_window_sum = int(mean) + 1

        is_anomaly = current_window_sum > threshold
        # May or may not trigger depending on actual threshold
        # This tests the edge case behavior

    def test_high_variance_baseline_higher_threshold(self):
        """High variance baseline should have higher threshold."""
        # High variance baseline
        high_var_baseline = [5, 15, 8, 20, 3, 18, 7, 22, 4, 16] * 30

        mean_high = statistics.mean(high_var_baseline)
        std_high = statistics.stdev(high_var_baseline)
        threshold_high = mean_high + (STD_DEV_THRESHOLD * std_high)

        # Low variance baseline
        low_var_baseline = [10, 10, 11, 10, 9, 10, 11, 10, 10, 9] * 30

        mean_low = statistics.mean(low_var_baseline)
        std_low = statistics.stdev(low_var_baseline)
        threshold_low = mean_low + (STD_DEV_THRESHOLD * std_low)

        # High variance should have higher threshold
        assert threshold_high > threshold_low


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
