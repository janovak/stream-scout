#!/usr/bin/env python3
"""
Clip attempt lifecycle: the sleep schedule, the two retry loops, and the two
Twitch calls that create a clip and poll for its metadata.

No pyflink import here -- this module is plain Python driven by an injected
clock, so the retry schedule can be tested in milliseconds instead of
half an hour.
"""

import logging
import os
from dataclasses import dataclass
from typing import Optional, Protocol

logger = logging.getLogger("clip_detector.clip_attempt")


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
    # This wait was correct for the earlier detector, which reported at the
    # start of a spike. A wait then moved the clip window forward, onto the
    # moment. Plan 06 Phase 3 moved the report to the end of the spike. The
    # same wait now moves the window past the moment.
    #
    # Plan 06 Phase 4 measured the full chain on the 12-hour corpus. At a wait
    # of 10 seconds the API call occurs 15.1 seconds after the report: 5
    # seconds for the watermark to pass that second, 0.1 seconds from Twitch to
    # our Kafka, and the 10-second wait. Twitch publishes the last 30 seconds
    # of its capture, which is 25 seconds before the call to 5 seconds after.
    # The clip thus covers -9.9 to +20.1 seconds around the report. A peak more
    # than 9.9 seconds before the report is not in its own clip. That was true
    # for 6 of 516 clips.
    #
    # At 0 the clip covers -19.9 to +10.1 seconds. All 516 clips then contain
    # their peak. The median peak also moves from 26% to 60% through the clip.
    #
    # Do not use 3 seconds. It centers the median peak more exactly, but it
    # leaves only 0.9 seconds of margin on the worst case that the corpus
    # found. Margin is more important here, because the two failures are not
    # equal: a peak near the start of a clip is still a usable clip, and a
    # missing peak is not.
    initial_delay_seconds: int = 0
    create_retry_delays: tuple = (0, 2, 4)
    metadata_retry_delays: tuple = (5, 10, 15, 30)

    @classmethod
    def from_env(cls) -> "ClipPolicy":
        create_retry_delays = _parse_int_tuple(os.getenv("CLIP_CREATE_RETRY_DELAYS", "0,2,4"))
        metadata_retry_delays = _parse_int_tuple(os.getenv("CLIP_METADATA_RETRY_DELAYS", "5,10,15,30"))
        if not create_retry_delays:
            raise ValueError("CLIP_CREATE_RETRY_DELAYS must not be empty -- an empty schedule "
                              "means create_clip is never called")
        if not metadata_retry_delays:
            raise ValueError("CLIP_METADATA_RETRY_DELAYS must not be empty -- an empty schedule "
                              "means get_clip is never called")
        return cls(
            initial_delay_seconds=int(os.getenv("CLIP_INITIAL_DELAY_SECONDS", "0")),
            create_retry_delays=create_retry_delays,
            metadata_retry_delays=metadata_retry_delays,
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

        # Skip the sleep at 0, as the create-retry loop below does for a delay
        # of 0. The shipped value is 0, so this is the usual path.
        if self._policy.initial_delay_seconds > 0:
            logger.info(f"Waiting {self._policy.initial_delay_seconds}s before clip creation. "
                        f"This moves the clip window later, away from the peak.")
            self._clock.sleep(self._policy.initial_delay_seconds)

        clip_id = None
        last_error = None
        num_create_attempts = len(self._policy.create_retry_delays)
        for attempt, delay in enumerate(self._policy.create_retry_delays):
            if delay > 0:
                logger.info(f"Retry delay: waiting {delay}s before attempt {attempt + 1}")
                self._clock.sleep(delay)

            try:
                logger.info(f"Clip creation attempt {attempt + 1}/{num_create_attempts}")
                clip_id = self._twitch.create_clip(broadcaster_id)
                if clip_id:
                    logger.info(f"Clip creation successful on attempt {attempt + 1}: clip_id={clip_id}")
                    break
                logger.warning(f"Clip creation attempt {attempt + 1} returned no clip_id")
            except Exception as e:
                last_error = e
                # TwitchAPIError carries is_retryable; anything else (and any
                # TwitchAPIError with is_retryable=False) is not retryable.
                # Duck-typed rather than isinstance so this module never has
                # to import clip_detector_job (and its pyflink import chain).
                is_retryable = getattr(e, "is_retryable", False)
                logger.warning(f"Clip creation attempt {attempt + 1} failed: {e} (retryable={is_retryable})")
                if not is_retryable:
                    break

        if not clip_id:
            clipping_disabled = getattr(last_error, "status_code", None) == 403
            failure_reason = "api_error" if last_error else "max_retries"
            logger.error(f"CLIP CREATION FAILED for broadcaster {broadcaster_id}: reason={failure_reason}")
            return self._result(start_time, clip_id=None, clip_data=None,
                                 failure_reason=failure_reason, clipping_disabled=clipping_disabled)

        clip_data = None
        num_metadata_attempts = len(self._policy.metadata_retry_delays)
        for attempt, delay in enumerate(self._policy.metadata_retry_delays):
            logger.info(f"Waiting {delay}s before clip metadata attempt "
                        f"{attempt + 1}/{num_metadata_attempts} for {clip_id}...")
            self._clock.sleep(delay)
            clip_data = self._twitch.get_clip(clip_id)
            if clip_data:
                logger.info(f"Clip metadata retrieved on attempt {attempt + 1}: clip_id={clip_id}")
                break
            logger.info(f"Clip metadata attempt {attempt + 1}/{num_metadata_attempts} found nothing yet for {clip_id}")

        if not clip_data:
            logger.error(f"CLIP METADATA RETRIEVAL FAILED for clip_id={clip_id} after {num_metadata_attempts} attempts")
            return self._result(start_time, clip_id=clip_id, clip_data=None,
                                 failure_reason="metadata_fetch", clipping_disabled=False)

        return self._result(start_time, clip_id=clip_id, clip_data=clip_data,
                             failure_reason=None, clipping_disabled=False)

    def _result(self, start_time: float, *, clip_id, clip_data, failure_reason, clipping_disabled) -> AttemptResult:
        return AttemptResult(
            clip_id=clip_id,
            clip_data=clip_data,
            failure_reason=failure_reason,
            clipping_disabled=clipping_disabled,
            duration_seconds=self._clock.now() - start_time,
        )
