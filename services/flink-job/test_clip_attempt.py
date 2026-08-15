#!/usr/bin/env python3
"""
Unit tests for the clip attempt lifecycle.

Uses a FakeClock so the retry schedule is asserted on the requested sleep
sequence, not on elapsed wall-clock time -- the whole suite must run in
well under a second.
"""

from clip_attempt import AttemptResult, ClipAttempt, ClipPolicy
from clip_detector_job import TwitchAPIError


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

        # initial delay (10), then the 0-delay create retry is skipped (no
        # sleep for delay==0), then the metadata schedule: 5, 10, 15, 30
        assert clock.sleeps == [10, 5, 10, 15, 30]


class TestClipAttemptCreateRetries:
    def test_retryable_error_is_retried_then_succeeds(self):
        clock = FakeClock()
        twitch = StubTwitch(
            create_clip_effects=[
                TwitchAPIError("server error", 503, is_retryable=True),
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
                TwitchAPIError("bad request", 400, is_retryable=False),
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
                TwitchAPIError("forbidden", 403, is_retryable=False),
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
