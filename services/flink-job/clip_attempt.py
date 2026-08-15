#!/usr/bin/env python3
"""
Clip attempt lifecycle: the sleep schedule, the two retry loops, and the two
Twitch calls that create a clip and poll for its metadata.

No pyflink import here -- this module is plain Python driven by an injected
clock, so the retry schedule can be tested in milliseconds instead of
half an hour.
"""

import os
from dataclasses import dataclass
from typing import Optional, Protocol


class Clock(Protocol):
    def sleep(self, seconds: float) -> None: ...
    def now(self) -> float: ...


class RealClock:
    """Clock backed by the real wall clock."""

    def sleep(self, seconds: float) -> None:
        import time
        time.sleep(seconds)

    def now(self) -> float:
        import time
        return time.time()


def _parse_int_tuple(value: str) -> tuple:
    return tuple(int(x.strip()) for x in value.split(",") if x.strip())


@dataclass(frozen=True)
class ClipPolicy:
    # Twitch's own docs (dev.twitch.tv/docs/api/clips/) only say clip creation is
    # async and to "assume it failed" if Get Clips hasn't returned the clip after
    # 15 seconds -- no recommended poll interval or attempt count, and no stated
    # guarantee of a minimum or maximum processing time either way. A single
    # check at the 15s mark (the old behavior) missed ~15% of real clips per our
    # own metrics, so we retry with real backoff instead of waiting once and
    # giving up. Tried pushing this much further out (t=5,60,360,1260) to see if
    # the small residual failure rate (~1-1.5% even with the original schedule)
    # was just slow clips needing more time -- it wasn't: zero recoveries on the
    # added attempts 3/4 across everything we watched, same failure rate as
    # before, just taking up to 21 minutes to find out instead of 50 seconds.
    # Settled on a modest bump over the original instead: same 4 attempts, same
    # shape, just a longer last step (t=5,15,30,60 vs the original t=5,15,30,50).
    initial_delay_seconds: int = 10
    create_retry_delays: tuple = (0, 2, 4)
    metadata_retry_delays: tuple = (5, 10, 15, 30)

    @classmethod
    def from_env(cls) -> "ClipPolicy":
        return cls(
            initial_delay_seconds=int(os.getenv("CLIP_INITIAL_DELAY_SECONDS", "10")),
            create_retry_delays=_parse_int_tuple(os.getenv("CLIP_CREATE_RETRY_DELAYS", "0,2,4")),
            metadata_retry_delays=_parse_int_tuple(os.getenv("CLIP_METADATA_RETRY_DELAYS", "5,10,15,30")),
        )


@dataclass(frozen=True)
class AttemptResult:
    clip_id: Optional[str]
    clip_data: Optional[dict]
    failure_reason: Optional[str]  # "api_error" | "max_retries" | "metadata_fetch" | None
    clipping_disabled: bool  # True when Twitch returned 403
    duration_seconds: float


class ClipAttempt:
    """Runs one clip creation attempt end to end: wait, create (with retries),
    then poll for metadata (with retries). Performs no database write and
    records no metrics -- it only reports what happened; the caller decides
    what to do with the result.
    """

    def __init__(self, twitch, policy: ClipPolicy, clock: Clock):
        self._twitch = twitch
        self._policy = policy
        self._clock = clock

    def run(self, broadcaster_id: int) -> AttemptResult:
        start_time = self._clock.now()

        self._clock.sleep(self._policy.initial_delay_seconds)

        clip_id = None
        last_error = None
        for delay in self._policy.create_retry_delays:
            if delay > 0:
                self._clock.sleep(delay)

            try:
                clip_id = self._twitch.create_clip(broadcaster_id)
                if clip_id:
                    break
            except Exception as e:
                last_error = e
                # TwitchAPIError carries is_retryable; anything else (and any
                # TwitchAPIError with is_retryable=False) is not retryable.
                # Duck-typed rather than isinstance so this module never has
                # to import clip_detector_job (and its pyflink import chain).
                if not getattr(e, "is_retryable", False):
                    break

        if not clip_id:
            clipping_disabled = getattr(last_error, "status_code", None) == 403
            failure_reason = "api_error" if last_error else "max_retries"
            return AttemptResult(
                clip_id=None,
                clip_data=None,
                failure_reason=failure_reason,
                clipping_disabled=clipping_disabled,
                duration_seconds=self._clock.now() - start_time,
            )

        clip_data = None
        for delay in self._policy.metadata_retry_delays:
            self._clock.sleep(delay)
            clip_data = self._twitch.get_clip(clip_id)
            if clip_data:
                break

        if not clip_data:
            return AttemptResult(
                clip_id=clip_id,
                clip_data=None,
                failure_reason="metadata_fetch",
                clipping_disabled=False,
                duration_seconds=self._clock.now() - start_time,
            )

        return AttemptResult(
            clip_id=clip_id,
            clip_data=clip_data,
            failure_reason=None,
            clipping_disabled=False,
            duration_seconds=self._clock.now() - start_time,
        )
