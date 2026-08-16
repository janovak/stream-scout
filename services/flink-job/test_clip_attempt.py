#!/usr/bin/env python3
"""
Unit tests for the clip attempt lifecycle.

Uses a FakeClock so the retry schedule is asserted on the requested sleep
sequence, not on elapsed wall-clock time -- the whole suite must run in
well under a second.
"""

from dataclasses import replace

import pytest

from clip_attempt import ClipAttempt, ClipPolicy


class FakeTwitchAPIError(Exception):
    """Stands in for clip_detector_job.TwitchAPIError without importing that
    module (and its pyflink import chain) into this test file -- ClipAttempt
    only duck-types on is_retryable/status_code, so this is enough."""

    def __init__(self, message: str, status_code: int, is_retryable: bool):
        super().__init__(message)
        self.status_code = status_code
        self.is_retryable = is_retryable


class FakeClock:
    def __init__(self):
        self.sleeps = []
        self._now = 0.0

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self._now += seconds

    def now(self) -> float:
        return self._now


class StubTwitch:
    def __init__(self, create_clip_effects, get_clip_effects=None):
        # Each item in create_clip_effects is either a clip_id string/None,
        # or an exception instance to raise.
        self._create_clip_effects = list(create_clip_effects)
        self._get_clip_effects = list(get_clip_effects or [])
        self.create_clip_calls = 0
        self.get_clip_calls = 0

    def create_clip(self, broadcaster_id):
        self.create_clip_calls += 1
        effect = self._create_clip_effects.pop(0)
        if isinstance(effect, Exception):
            raise effect
        return effect

    def get_clip(self, clip_id):
        self.get_clip_calls += 1
        effect = self._get_clip_effects.pop(0)
        if isinstance(effect, Exception):
            raise effect
        return effect


DEFAULT_POLICY = ClipPolicy()


class TestClipAttemptHappyPath:
    def test_happy_path_sets_clip_id_and_data_with_no_failure(self):
        clock = FakeClock()
        clip_data = {"embed_url": "https://embed", "thumbnail_url": "https://thumb"}
        twitch = StubTwitch(
            create_clip_effects=["clip123"],
            get_clip_effects=[clip_data],
        )
        result = ClipAttempt(twitch, DEFAULT_POLICY, clock).run(broadcaster_id=1)

        assert result.clip_id == "clip123"
        assert result.clip_data == clip_data
        assert result.failure_reason is None
        assert result.clipping_disabled is False

    def test_clock_is_asked_for_exact_schedule(self):
        clock = FakeClock()
        twitch = StubTwitch(
            create_clip_effects=["clip123"],
            get_clip_effects=[None, None, None, {"embed_url": "u", "thumbnail_url": "t"}],
        )
        ClipAttempt(twitch, DEFAULT_POLICY, clock).run(broadcaster_id=1)

        # The initial delay is 0 and is skipped, as the 0-delay create retry
        # is. What remains is the metadata schedule: 5, 10, 15, 30.
        assert clock.sleeps == [5, 10, 15, 30]

    def test_a_configured_initial_delay_is_still_waited(self):
        clock = FakeClock()
        twitch = StubTwitch(
            create_clip_effects=["clip123"],
            get_clip_effects=[{"embed_url": "u", "thumbnail_url": "t"}],
        )
        policy = replace(DEFAULT_POLICY, initial_delay_seconds=10)
        ClipAttempt(twitch, policy, clock).run(broadcaster_id=1)
        assert clock.sleeps == [10, 5]


class TestClipAttemptCreateRetries:
    def test_retryable_error_is_retried_then_succeeds(self):
        clock = FakeClock()
        twitch = StubTwitch(
            create_clip_effects=[
                FakeTwitchAPIError("server error", 503, is_retryable=True),
                "clip123",
            ],
            get_clip_effects=[{"embed_url": "u", "thumbnail_url": "t"}],
        )
        result = ClipAttempt(twitch, DEFAULT_POLICY, clock).run(broadcaster_id=1)

        assert result.clip_id == "clip123"
        assert twitch.create_clip_calls == 2

    def test_non_retryable_error_is_not_retried(self):
        clock = FakeClock()
        twitch = StubTwitch(
            create_clip_effects=[
                FakeTwitchAPIError("bad request", 400, is_retryable=False),
                "clip123",  # would succeed if retried -- it must not be
            ],
        )
        result = ClipAttempt(twitch, DEFAULT_POLICY, clock).run(broadcaster_id=1)

        assert twitch.create_clip_calls == 1
        assert result.clip_id is None
        assert result.failure_reason == "api_error"

    def test_403_marks_clipping_disabled(self):
        clock = FakeClock()
        twitch = StubTwitch(
            create_clip_effects=[
                FakeTwitchAPIError("forbidden", 403, is_retryable=False),
            ],
        )
        result = ClipAttempt(twitch, DEFAULT_POLICY, clock).run(broadcaster_id=1)

        assert result.clipping_disabled is True
        assert result.failure_reason == "api_error"

    def test_all_create_attempts_exhausted_reports_max_retries(self):
        clock = FakeClock()
        twitch = StubTwitch(
            create_clip_effects=[None, None, None],
        )
        result = ClipAttempt(twitch, DEFAULT_POLICY, clock).run(broadcaster_id=1)

        assert twitch.create_clip_calls == 3
        assert result.clip_id is None
        assert result.failure_reason == "max_retries"
        assert result.clipping_disabled is False


class TestClipPolicyFromEnv:
    def test_empty_create_retry_delays_raises(self, monkeypatch):
        monkeypatch.setenv("CLIP_CREATE_RETRY_DELAYS", "")
        with pytest.raises(ValueError):
            ClipPolicy.from_env()

    def test_empty_metadata_retry_delays_raises(self, monkeypatch):
        monkeypatch.setenv("CLIP_METADATA_RETRY_DELAYS", "")
        with pytest.raises(ValueError):
            ClipPolicy.from_env()


class TestClipAttemptMetadataRetries:
    def test_metadata_never_arrives_reports_metadata_fetch_after_four_attempts(self):
        clock = FakeClock()
        twitch = StubTwitch(
            create_clip_effects=["clip123"],
            get_clip_effects=[None, None, None, None],
        )
        result = ClipAttempt(twitch, DEFAULT_POLICY, clock).run(broadcaster_id=1)

        assert twitch.get_clip_calls == 4
        assert result.clip_id == "clip123"
        assert result.clip_data is None
        assert result.failure_reason == "metadata_fetch"
