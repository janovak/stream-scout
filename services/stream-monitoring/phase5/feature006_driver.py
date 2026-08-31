#!/usr/bin/env python3
"""Feature 006 isolated evidence driver.

Importing this module is side-effect free. Real clients and production modules
are loaded only after CLI validation and only through an operator-provided
runtime factory.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import inspect
import json
import math
import os
import statistics
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from types import TracebackType
from typing import Any, Awaitable, Callable, Iterable, Mapping, MutableMapping, Sequence
from urllib.parse import unquote, urlparse

try:
    from .feature006_fixtures import RankingFixture, build_ranking_fixture
except ImportError:  # Direct execution: python phase5\feature006_driver.py
    from feature006_fixtures import RankingFixture, build_ranking_fixture

SCHEMA = "stream-scout.feature006.v1"
RECORD_KINDS = frozenset(
    {
        "operation-counts",
        "calibration",
        "poll-profile",
        "reconciler-gap",
        "cold-start",
    }
)
PROFILE_PHASES = (
    "ranking_fetch",
    "metadata_persistence",
    "online_snapshot",
    "online_refresh",
    "lifecycle_publication",
    "desired_set_publication",
)
NON_EMPTY_DISPATCH_COUNTS = {
    "clipping_execute": 1,
    "redis_zrange": 1,
    "redis_hgetall": 1,
    "redis_get": 1,
    "metadata_execute": 1,
    "metadata_commit": 1,
    "online_snapshot_mget": 1,
    "online_refresh_execute": 1,
    "desired_publication_execute": 1,
}
EMPTY_DISPATCH_COUNTS = {
    "clipping_execute": 0,
    "redis_zrange": 1,
    "redis_hgetall": 1,
    "redis_get": 1,
    "metadata_execute": 0,
    "metadata_commit": 0,
    "online_snapshot_mget": 0,
    "online_refresh_execute": 0,
    "desired_publication_execute": 1,
}

REQUIRED_EVIDENCE_FIELDS = {
    "calibration": frozenset(
        {
            "scale",
            "live_page_samples_ms",
            "live_page_p95_ms",
            "fixture_page_delay_ms",
            "raw_records",
            "disabled_records",
            "disabled_proportion",
            "eligible_records",
            "page_size",
            "page_count",
            "ranking_budget_ms",
            "non_ranking_budget_ms",
            "redis_rtt_samples_ms",
            "redis_median_ms",
            "postgres_rtt_samples_ms",
            "postgres_median_ms",
        }
    ),
    "poll-profile": frozenset(
        {
            "scale",
            "profile",
            "test_join_threshold",
            "test_leave_threshold",
            "test_fetch_buffer",
            "warmup_duration_ms",
            "measured_durations_ms",
            "nearest_rank_p95_ms",
            "overlap_skip_count",
            "excluded_poll_count",
            "phase_durations_ms",
            "dispatch_counts",
        }
    ),
    "reconciler-gap": frozenset(
        {
            "scale",
            "post_convergence_started_at",
            "run_duration_seconds",
            "pass_completion_monotonic_ns",
            "adjacent_gaps_ms",
            "maximum_gap_ms",
            "scheduler_events",
        }
    ),
    "cold-start": frozenset(
        {
            "target",
            "initialization_complete_at",
            "initial_subscription_count",
            "rate_limit_events",
            "backoff_events",
            "accepted_create_windows",
            "subscription_count_by_window",
            "poll_start_end_monotonic_ns",
            "poll_durations_ms",
            "overlap_skip_count",
            "final_subscription_count",
        }
    ),
}


def utc_now() -> str:
    """Return an RFC3339 UTC timestamp."""

    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _valid_timestamp(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() == timezone.utc.utcoffset(parsed)


def _number(value: Any, *, non_negative: bool = True) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
        and (not non_negative or value >= 0)
    )


def nearest_rank_p95(values: Sequence[float]) -> float:
    """Return nearest-rank p95 (ceil(.95*n), one-based) without interpolation."""

    if not values:
        raise ValueError("p95 requires at least one value")
    numeric = [float(value) for value in values]
    if any(not math.isfinite(value) for value in numeric):
        raise ValueError("p95 samples must be finite")
    ordered = sorted(numeric)
    return ordered[math.ceil(0.95 * len(ordered)) - 1]


def fixture_summary(fixture: RankingFixture) -> dict[str, Any]:
    """Return the fixture fields retained in external evidence."""

    return {
        "scale": fixture.scale,
        "fixture_id": fixture.fixture_id,
        "raw_records": len(fixture.records),
        "disabled_records": fixture.disabled_records,
        "disabled_proportion": fixture.disabled_proportion,
        "eligible_records": len(fixture.eligible_records),
        "page_size": fixture.page_size,
        "page_count": len(fixture.pages),
        "fixture_page_delay_ms": fixture.page_delay_ms,
        "test_join_threshold": fixture.test_join_threshold,
        "test_leave_threshold": fixture.test_leave_threshold,
        "test_fetch_buffer": fixture.test_fetch_buffer,
    }


def validate_isolated_targets(
    *, redis_url: str | None, postgres_url: str | None, namespace: str | None
) -> None:
    """Reject missing, default, or production-like validation targets."""

    isolated_error = ValueError(
        "explicit isolated Redis, Postgres, and namespace targets are required"
    )
    if not redis_url or not postgres_url or not namespace:
        raise isolated_error

    redis = urlparse(redis_url)
    if redis.scheme not in {"redis", "rediss"} or not redis.hostname:
        raise isolated_error
    try:
        redis_db = int(redis.path.strip("/") or "0")
    except ValueError:
        raise isolated_error from None
    if redis_db <= 0:
        raise isolated_error

    postgres = urlparse(postgres_url)
    if postgres.scheme not in {"postgres", "postgresql"} or not postgres.hostname:
        raise isolated_error
    database = unquote(postgres.path.strip("/")).lower()
    safe_database_markers = ("test", "testing", "isolated", "feature006", "feature_006")
    if not database or not any(marker in database for marker in safe_database_markers):
        raise isolated_error

    lowered_namespace = namespace.strip().lower()
    unsafe_namespaces = {"production", "prod", "live", "default", "main"}
    safe_namespace_markers = ("test", "isolated", "feature006", "feature-006")
    if (
        lowered_namespace in unsafe_namespaces
        or not any(marker in lowered_namespace for marker in safe_namespace_markers)
    ):
        raise isolated_error


class JsonlWriter:
    """Append validated, bounded evidence envelopes to a JSON Lines file."""

    def __init__(
        self,
        output: str | os.PathLike[str],
        *,
        run_id: str,
        clock: Callable[[], str] = utc_now,
    ) -> None:
        if not run_id or not run_id.strip():
            raise ValueError("run_id is required")
        self.output = Path(output)
        self.run_id = run_id
        self.clock = clock

    def write(self, kind: str, fields: Mapping[str, Any]) -> dict[str, Any]:
        if kind not in RECORD_KINDS:
            raise ValueError(f"unsupported record kind: {kind!r}")
        if kind in REQUIRED_EVIDENCE_FIELDS:
            validate_evidence_record(kind, fields)
        timestamp = self.clock()
        if not _valid_timestamp(timestamp):
            raise ValueError("clock must return an RFC3339 UTC timestamp")
        overlap = {"schema", "kind", "run_id", "timestamp"} & set(fields)
        if overlap:
            raise ValueError(f"evidence fields replace envelope keys: {sorted(overlap)}")
        row = {
            "schema": SCHEMA,
            "kind": kind,
            "run_id": self.run_id,
            "timestamp": timestamp,
            **dict(fields),
        }
        self.output.parent.mkdir(parents=True, exist_ok=True)
        with self.output.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(row, separators=(",", ":"), sort_keys=True) + "\n")
        return row


def validate_evidence_record(kind: str, fields: Mapping[str, Any]) -> Mapping[str, Any]:
    """Validate required contract fields and invariants without changing input."""

    required = REQUIRED_EVIDENCE_FIELDS.get(kind)
    if required is None:
        if kind == "operation-counts":
            return fields
        raise ValueError(f"unsupported record kind: {kind!r}")
    missing = required - set(fields)
    if missing:
        raise ValueError(f"{kind} record missing fields: {sorted(missing)}")

    scale = fields.get("scale", fields.get("target"))
    if scale not in {500, 900}:
        raise ValueError(f"{kind} scale/target must be 500 or 900")

    if kind == "calibration":
        for sample_name in (
            "live_page_samples_ms",
            "redis_rtt_samples_ms",
            "postgres_rtt_samples_ms",
        ):
            samples = fields[sample_name]
            if not isinstance(samples, Sequence) or len(samples) < 20:
                raise ValueError(f"{sample_name} requires at least 20 samples")
            if any(not _number(sample) for sample in samples):
                raise ValueError(f"{sample_name} contains an invalid sample")
        if fields["eligible_records"] != fields["scale"]:
            raise ValueError("calibration eligible_records must equal scale")
        if fields["page_size"] != 100:
            raise ValueError("calibration page_size must be 100")
        if fields["fixture_page_delay_ms"] < fields["live_page_p95_ms"]:
            raise ValueError("fixture page delay must be at least live page p95")
        if fields["live_page_p95_ms"] != nearest_rank_p95(
            fields["live_page_samples_ms"]
        ):
            raise ValueError("live page p95 is inconsistent")
        if fields["raw_records"] != (
            fields["eligible_records"] + fields["disabled_records"]
        ):
            raise ValueError("calibration raw record count is inconsistent")
        expected_pages = math.ceil(fields["raw_records"] / fields["page_size"])
        if fields["page_count"] != expected_pages:
            raise ValueError("calibration page count is inconsistent")
        expected_proportion = (
            fields["disabled_records"] / fields["raw_records"]
            if fields["raw_records"]
            else 0.0
        )
        if not math.isclose(fields["disabled_proportion"], expected_proportion):
            raise ValueError("calibration disabled proportion is inconsistent")
        expected_ranking_budget = (
            fields["page_count"] * fields["fixture_page_delay_ms"]
        )
        if fields["ranking_budget_ms"] != expected_ranking_budget:
            raise ValueError("calibration ranking budget is inconsistent")
        total_budget = 5000.0 if fields["scale"] == 500 else 10000.0
        if fields["non_ranking_budget_ms"] != total_budget - expected_ranking_budget:
            raise ValueError("calibration non-ranking budget is inconsistent")
        if fields["redis_median_ms"] != statistics.median(
            fields["redis_rtt_samples_ms"]
        ):
            raise ValueError("Redis median is inconsistent")
        if fields["postgres_median_ms"] != statistics.median(
            fields["postgres_rtt_samples_ms"]
        ):
            raise ValueError("Postgres median is inconsistent")
    elif kind == "poll-profile":
        if fields["profile"] not in {"stable", "complete_turnover"}:
            raise ValueError("profile must be stable or complete_turnover")
        durations = fields["measured_durations_ms"]
        if not isinstance(durations, Sequence) or len(durations) != 20:
            raise ValueError("poll-profile requires exactly 20 completed polls")
        if any(not _number(value) for value in durations):
            raise ValueError("poll-profile duration is invalid")
        if fields["nearest_rank_p95_ms"] != nearest_rank_p95(durations):
            raise ValueError("poll-profile nearest-rank p95 is inconsistent")
        if (
            fields["test_join_threshold"] != fields["scale"]
            or fields["test_leave_threshold"] != fields["scale"]
            or not _number(fields["test_fetch_buffer"])
        ):
            raise ValueError("poll-profile test-only configuration is invalid")
        if not _number(fields["warmup_duration_ms"]):
            raise ValueError("poll-profile warmup duration is invalid")
        if (
            not isinstance(fields["overlap_skip_count"], int)
            or fields["overlap_skip_count"] < 0
            or not isinstance(fields["excluded_poll_count"], int)
            or fields["excluded_poll_count"] < 0
        ):
            raise ValueError("poll-profile counts are invalid")
        if not isinstance(fields["phase_durations_ms"], Mapping):
            raise ValueError("phase_durations_ms must be a mapping")
        missing_phases = set(PROFILE_PHASES) - set(
            fields["phase_durations_ms"]
        )
        if missing_phases:
            raise ValueError(
                f"phase_durations_ms missing phases: {sorted(missing_phases)}"
            )
        for phase in PROFILE_PHASES:
            durations_for_phase = fields["phase_durations_ms"][phase]
            if (
                not isinstance(durations_for_phase, Sequence)
                or len(durations_for_phase) != 20
                or any(not _number(value) for value in durations_for_phase)
            ):
                raise ValueError(
                    f"{phase} must contain twenty valid phase durations"
                )
        if not isinstance(fields["dispatch_counts"], Mapping):
            raise ValueError("dispatch_counts must be a mapping")
        required_dispatches = {
            "metadata_execute",
            "metadata_commit",
            "online_snapshot_mget",
            "online_refresh_execute",
        }
        missing_dispatches = required_dispatches - set(
            fields["dispatch_counts"]
        )
        if missing_dispatches:
            raise ValueError(
                "dispatch_counts missing boundaries: "
                f"{sorted(missing_dispatches)}"
            )
        for boundary in required_dispatches:
            counts = fields["dispatch_counts"][boundary]
            if (
                not isinstance(counts, Sequence)
                or len(counts) != 20
                or any(count != 1 for count in counts)
            ):
                raise ValueError(
                    f"{boundary} must contain twenty positive one-dispatch counts"
                )
    elif kind == "reconciler-gap":
        if not _valid_timestamp(fields["post_convergence_started_at"]):
            raise ValueError("post_convergence_started_at must be RFC3339 UTC")
        if fields["run_duration_seconds"] < 1800:
            raise ValueError("reconciler-gap run must last at least 30 minutes")
        if len(fields["pass_completion_monotonic_ns"]) < 2:
            raise ValueError(
                "reconciler-gap requires at least two pass completions"
            )
        calculated = adjacent_gaps_ms(fields["pass_completion_monotonic_ns"])
        if list(fields["adjacent_gaps_ms"]) != calculated:
            raise ValueError("adjacent gaps do not match pass completions")
        expected_max = max(calculated, default=0.0)
        if fields["maximum_gap_ms"] != expected_max:
            raise ValueError("maximum gap is inconsistent")
    else:
        if fields["target"] != 900 or fields["initial_subscription_count"] != 0:
            raise ValueError("cold-start requires target 900 and zero initial subscriptions")
        if not _valid_timestamp(fields["initialization_complete_at"]):
            raise ValueError("initialization_complete_at must be RFC3339 UTC")
        if not isinstance(fields["poll_start_end_monotonic_ns"], Sequence):
            raise ValueError("poll timeline is invalid")
        for interval in fields["poll_start_end_monotonic_ns"]:
            if len(interval) != 2 or interval[1] < interval[0]:
                raise ValueError("poll start/end interval is invalid")
        backoffs = fields["backoff_events"]
        if not fields["rate_limit_events"]:
            raise ValueError("cold-start requires a rate-limit event")
        if not isinstance(backoffs, Sequence) or not backoffs:
            raise ValueError("cold-start requires a backoff event")
        for event in backoffs:
            if event["coverage_after"] < event["coverage_before"]:
                raise ValueError("backoff coverage must not decrease")
        windows = fields["accepted_create_windows"]
        if not isinstance(windows, Sequence) or not windows:
            raise ValueError("cold-start requires accepted create windows")
        for window in windows:
            if window["after"] <= window["before"]:
                raise ValueError("accepted create window must increase coverage")
        calculated_durations = [
            (interval[1] - interval[0]) / 1_000_000.0
            for interval in fields["poll_start_end_monotonic_ns"]
        ]
        if list(fields["poll_durations_ms"]) != calculated_durations:
            raise ValueError("cold-start poll durations do not match intervals")
        if any(duration >= 10_000.0 for duration in calculated_durations):
            raise ValueError("cold-start polls must remain under 10 seconds")
        if fields["overlap_skip_count"] != 0:
            raise ValueError("cold-start overlap_skip_count must be zero")
        counts = fields["subscription_count_by_window"]
        if (
            not isinstance(counts, Sequence)
            or len(counts) < 2
            or any(after < before for before, after in zip(counts, counts[1:]))
        ):
            raise ValueError(
                "subscription_count_by_window must show non-decreasing coverage"
            )
        final_count = fields["final_subscription_count"]
        if (
            not isinstance(final_count, int)
            or final_count < counts[-1]
            or final_count > fields["target"]
        ):
            raise ValueError("cold-start final subscription count is invalid")
    return fields


def build_calibration_record(
    scale: int,
    live_page_samples_ms: Sequence[float],
    fixture: RankingFixture,
    redis_rtt_samples_ms: Sequence[float],
    postgres_rtt_samples_ms: Sequence[float],
) -> dict[str, Any]:
    """Build calibrated page/datastore evidence for one supported scale."""

    for name, samples in (
        ("live page", live_page_samples_ms),
        ("Redis RTT", redis_rtt_samples_ms),
        ("Postgres RTT", postgres_rtt_samples_ms),
    ):
        if len(samples) < 20:
            raise ValueError(f"{name} calibration requires at least 20 samples")
        if any(not _number(value) for value in samples):
            raise ValueError(f"{name} calibration samples must be finite and non-negative")
    if fixture.scale != scale:
        raise ValueError("fixture scale does not match calibration scale")
    page_p95 = nearest_rank_p95(live_page_samples_ms)
    if fixture.page_delay_ms < page_p95:
        raise ValueError("fixture page delay must be at least the live page p95")
    redis_median = statistics.median(float(v) for v in redis_rtt_samples_ms)
    postgres_median = statistics.median(float(v) for v in postgres_rtt_samples_ms)
    ranking_budget = len(fixture.pages) * fixture.page_delay_ms
    total_budget = 5000.0 if scale == 500 else 10000.0
    record = {
        **{
            key: value
            for key, value in fixture_summary(fixture).items()
            if key
            in {
                "scale",
                "raw_records",
                "disabled_records",
                "disabled_proportion",
                "eligible_records",
                "page_size",
                "page_count",
                "fixture_page_delay_ms",
            }
        },
        "live_page_samples_ms": [float(v) for v in live_page_samples_ms],
        "live_page_p95_ms": page_p95,
        "ranking_budget_ms": ranking_budget,
        "non_ranking_budget_ms": total_budget - ranking_budget,
        "redis_rtt_samples_ms": [float(v) for v in redis_rtt_samples_ms],
        "redis_median_ms": redis_median,
        "postgres_rtt_samples_ms": [float(v) for v in postgres_rtt_samples_ms],
        "postgres_median_ms": postgres_median,
        "acceptance_valid": (
            len(fixture.eligible_records) == scale
            and 40.0 <= redis_median <= 110.0
            and 40.0 <= postgres_median <= 110.0
        ),
    }
    validate_evidence_record("calibration", record)
    return record


async def collect_rtt_samples(
    operation: Callable[[], Any],
    *,
    minimum_samples: int = 20,
    clock_ns: Callable[[], int] = time.monotonic_ns,
) -> list[float]:
    """Measure separate completed harmless operations in milliseconds."""

    if minimum_samples < 20:
        raise ValueError("RTT calibration requires at least 20 samples")
    samples = []
    for _ in range(minimum_samples):
        started = clock_ns()
        result = operation()
        if inspect.isawaitable(result):
            await result
        samples.append((clock_ns() - started) / 1_000_000.0)
    return samples


async def collect_live_page_samples(
    fetch_page: Callable[..., Any],
    *,
    minimum_samples: int = 20,
    clock_ns: Callable[[], int] = time.monotonic_ns,
) -> list[float]:
    """Measure completed live get_streams(first=100) page responses."""

    if minimum_samples < 20:
        raise ValueError("live calibration requires at least 20 page samples")
    samples = []
    cursor: Any = None
    for _ in range(minimum_samples):
        started = clock_ns()
        response = fetch_page(first=100, cursor=cursor)
        if inspect.isawaitable(response):
            response = await response
        elapsed = (clock_ns() - started) / 1_000_000.0
        if isinstance(response, tuple) and len(response) == 2:
            _page, cursor = response
        elif isinstance(response, Mapping):
            cursor = response.get("cursor")
        else:
            raise ValueError("live page fetch must return (page, cursor) or a mapping")
        samples.append(elapsed)
    return samples


async def collect_get_streams_page_samples(
    get_streams: Callable[..., Any],
    *,
    minimum_samples: int = 20,
    clock_ns: Callable[[], int] = time.monotonic_ns,
) -> list[float]:
    """Time real 100-row page consumption from get_streams(first=100)."""

    if minimum_samples < 20:
        raise ValueError("live calibration requires at least 20 page samples")
    samples: list[float] = []
    walks_without_page = 0
    while len(samples) < minimum_samples:
        before_walk = len(samples)
        stream = get_streams(first=100)
        if inspect.isawaitable(stream):
            stream = await stream
        page_rows = 0
        started = clock_ns()
        made_progress = False
        async for _record in stream:
            made_progress = True
            page_rows += 1
            if page_rows == 100:
                samples.append((clock_ns() - started) / 1_000_000.0)
                if len(samples) >= minimum_samples:
                    break
                page_rows = 0
                started = clock_ns()
        if not made_progress:
            raise ValueError("live get_streams returned no ranking records")
        # An incomplete tail is not a completed 100-row page observation.
        if len(samples) == before_walk:
            walks_without_page += 1
            if walks_without_page >= 2:
                raise ValueError("live get_streams produced no complete 100-row page")
        else:
            walks_without_page = 0
    return samples


def _normalized_profile(profile: str) -> str:
    normalized = profile.replace("-", "_")
    if normalized not in {"stable", "complete_turnover"}:
        raise ValueError("profile must be stable or complete-turnover")
    return normalized


def run_profile_measurements(
    scale: int,
    profile: str,
    warmups: int,
    measured_polls: int,
    prepare_state: Callable[[str], Any],
    run_poll: Callable[[str], Mapping[str, Any]],
) -> dict[str, Any]:
    """Run warmup plus completed measurements, replacing excluded whole polls."""

    if scale not in {500, 900}:
        raise ValueError("profile scale must be 500 or 900")
    normalized = _normalized_profile(profile)
    if warmups != 1:
        raise ValueError("acceptance profiles require exactly one warmup")
    if measured_polls <= 0:
        raise ValueError("measured_polls must be positive")

    attempt_index = 0

    def fixture_id() -> str:
        if normalized == "stable":
            return "A"
        return "A" if attempt_index % 2 == 0 else "B"

    warmup_duration = 0.0
    for _ in range(warmups):
        selected = "A"
        prepare_state(selected)
        result = run_poll(selected)
        if result.get("excluded"):
            raise ValueError("warmup poll failed and cannot establish warm state")
        warmup_duration = float(result["duration_ms"])
    # Warmup establishes process/connection state but is not part of the
    # measured A/B sequence. Complete turnover starts from fixture A.
    attempt_index = 0

    completed: list[float] = []
    phases: dict[str, list[float]] = {}
    dispatches: dict[str, list[int]] = {}
    excluded = 0
    overlap = 0
    while len(completed) < measured_polls:
        selected = fixture_id()
        prepare_state(selected)
        result = run_poll(selected)
        attempt_index += 1
        overlap += int(result.get("overlap_skip_count", 0))
        if result.get("excluded"):
            excluded += 1
            continue
        completed.append(float(result["duration_ms"]))
        for phase, duration in result.get("phase_durations_ms", {}).items():
            phases.setdefault(phase, []).append(float(duration))
        for boundary, count in result.get("dispatch_counts", {}).items():
            dispatches.setdefault(boundary, []).append(int(count))

    return {
        "scale": scale,
        "profile": normalized,
        "warmup_duration_ms": warmup_duration,
        "measured_durations_ms": completed,
        "nearest_rank_p95_ms": nearest_rank_p95(completed),
        "overlap_skip_count": overlap,
        "excluded_poll_count": excluded,
        "phase_durations_ms": phases,
        "dispatch_counts": dispatches,
    }


async def run_profile_measurements_async(
    scale: int,
    profile: str,
    warmups: int,
    measured_polls: int,
    prepare_state: Callable[[str], Any],
    run_poll: Callable[[str], Any],
) -> dict[str, Any]:
    """Async equivalent used by real runtime integrations."""

    if scale not in {500, 900}:
        raise ValueError("profile scale must be 500 or 900")
    normalized = _normalized_profile(profile)
    if warmups != 1:
        raise ValueError("acceptance profiles require exactly one warmup")
    if measured_polls <= 0:
        raise ValueError("measured_polls must be positive")

    await _invoke(prepare_state, "A")
    warmup = await _invoke(run_poll, "A")
    if warmup.get("excluded"):
        raise ValueError("warmup poll failed and cannot establish warm state")

    completed: list[float] = []
    phases: dict[str, list[float]] = {}
    dispatches: dict[str, list[int]] = {}
    excluded = overlap = attempt = 0
    while len(completed) < measured_polls:
        selected = "A" if normalized == "stable" or attempt % 2 == 0 else "B"
        await _invoke(prepare_state, selected)
        result = await _invoke(run_poll, selected)
        attempt += 1
        overlap += int(result.get("overlap_skip_count", 0))
        if result.get("excluded"):
            excluded += 1
            continue
        completed.append(float(result["duration_ms"]))
        for phase, duration in result.get("phase_durations_ms", {}).items():
            phases.setdefault(phase, []).append(float(duration))
        for boundary, count in result.get("dispatch_counts", {}).items():
            dispatches.setdefault(boundary, []).append(int(count))
    return {
        "scale": scale,
        "profile": normalized,
        "warmup_duration_ms": float(warmup["duration_ms"]),
        "measured_durations_ms": completed,
        "nearest_rank_p95_ms": nearest_rank_p95(completed),
        "overlap_skip_count": overlap,
        "excluded_poll_count": excluded,
        "phase_durations_ms": phases,
        "dispatch_counts": dispatches,
    }


def compose_pass_callbacks(
    production_callback: Callable[[int], Any] | None,
    recording_callback: Callable[[int], Any] | None,
) -> Callable[[int], None]:
    """Preserve the production callback and add recording after it."""

    def composed(count: int) -> None:
        if production_callback is not None:
            production_callback(count)
        if recording_callback is not None:
            recording_callback(count)

    return composed


class PassCompletionRecorder:
    def __init__(self, clock_ns: Callable[[], int] = time.monotonic_ns) -> None:
        self.clock_ns = clock_ns
        self.completions: list[int] = []
        self.counts: list[int] = []

    def record(self, count: int) -> None:
        self.completions.append(self.clock_ns())
        self.counts.append(count)


def adjacent_gaps_ms(completions_ns: Sequence[int]) -> list[float]:
    """Calculate adjacent completion gaps from monotonic in-process clocks."""

    values = [int(value) for value in completions_ns]
    if any(after < before for before, after in zip(values, values[1:])):
        raise ValueError("completion clocks must be monotonic")
    return [
        (after - before) / 1_000_000.0
        for before, after in zip(values, values[1:])
    ]


class SchedulerEventRecorder:
    """Classify only the bounded APScheduler event outcomes."""

    def __init__(self, clock_ns: Callable[[], int] = time.monotonic_ns) -> None:
        self.clock_ns = clock_ns
        self.events: list[dict[str, Any]] = []

    def record(self, event: Any) -> None:
        # Numeric constants are stable APScheduler public API values. Import
        # lazily when available so --help has no production dependency path.
        try:
            apscheduler_events = importlib.import_module("apscheduler.events")
            classifications = {
                apscheduler_events.EVENT_JOB_EXECUTED: "executed",
                apscheduler_events.EVENT_JOB_ERROR: "error",
                apscheduler_events.EVENT_JOB_MISSED: "missed",
                apscheduler_events.EVENT_JOB_MAX_INSTANCES: "max_instances",
            }
        except ImportError:
            classifications = {4096: "executed", 8192: "error", 16384: "missed", 65536: "max_instances"}
        kind = classifications.get(event.code)
        if kind is None:
            return
        self.events.append(
            {
                "kind": kind,
                "monotonic_ns": self.clock_ns(),
                "job_id": getattr(event, "job_id", None),
            }
        )


class ScheduledPollRecorder:
    """Record poll start/end clocks while preserving the wrapped callable."""

    def __init__(self, clock_ns: Callable[[], int] = time.monotonic_ns) -> None:
        self.clock_ns = clock_ns
        self.intervals: list[list[int]] = []

    async def run(self, poll: Callable[[], Any]) -> Any:
        started = self.clock_ns()
        try:
            result = poll()
            if inspect.isawaitable(result):
                result = await result
            return result
        finally:
            self.intervals.append([started, self.clock_ns()])


def build_reconciler_gap_record(
    scale: int,
    post_convergence_started_at: str,
    run_duration_seconds: float,
    pass_completion_monotonic_ns: Sequence[int],
    scheduler_events: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    gaps = adjacent_gaps_ms(pass_completion_monotonic_ns)
    record = {
        "scale": scale,
        "post_convergence_started_at": post_convergence_started_at,
        "run_duration_seconds": run_duration_seconds,
        "pass_completion_monotonic_ns": list(pass_completion_monotonic_ns),
        "adjacent_gaps_ms": gaps,
        "maximum_gap_ms": max(gaps, default=0.0),
        "scheduler_events": [dict(event) for event in scheduler_events],
    }
    validate_evidence_record("reconciler-gap", record)
    return record


def build_poll_profile_record(
    scale: int,
    profile: str,
    fixture: RankingFixture,
    warmup_duration_ms: float,
    measured_durations_ms: Sequence[float],
    overlap_skip_count: int,
    excluded_poll_count: int,
    phase_durations_ms: Mapping[str, Any],
    dispatch_counts: Mapping[str, Any],
) -> dict[str, Any]:
    """Build and strictly validate one complete poll-profile record."""

    if fixture.scale != scale:
        raise ValueError("profile fixture scale does not match record scale")
    record = {
        "scale": scale,
        "profile": _normalized_profile(profile),
        "test_join_threshold": fixture.test_join_threshold,
        "test_leave_threshold": fixture.test_leave_threshold,
        "test_fetch_buffer": fixture.test_fetch_buffer,
        "warmup_duration_ms": float(warmup_duration_ms),
        "measured_durations_ms": [float(value) for value in measured_durations_ms],
        "nearest_rank_p95_ms": nearest_rank_p95(measured_durations_ms),
        "overlap_skip_count": int(overlap_skip_count),
        "excluded_poll_count": int(excluded_poll_count),
        "phase_durations_ms": dict(phase_durations_ms),
        "dispatch_counts": dict(dispatch_counts),
    }
    validate_evidence_record("poll-profile", record)
    return record


def _is_rate_limited(exc: BaseException) -> bool:
    return any(cls.__name__ == "RateLimitedError" for cls in type(exc).__mro__)


class RecordingTransportProxy:
    """Delegate EventSub operations unchanged while recording cold-start facts."""

    def __init__(
        self,
        delegate: Any,
        *,
        clock_ns: Callable[[], int] = time.monotonic_ns,
        coverage: Callable[[], int] | None = None,
    ) -> None:
        self.delegate = delegate
        self.clock_ns = clock_ns
        self.coverage = coverage
        self.accepted_creates: list[dict[str, Any]] = []
        self.rate_limit_events: list[dict[str, Any]] = []
        self.backoff_events: list[dict[str, Any]] = []
        self.accepted_create_windows: list[dict[str, Any]] = []
        self.subscription_count_by_window: list[int] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self.delegate, name)

    async def list(self) -> Any:
        async for item in self.delegate.list():
            yield item

    async def create(self, broadcaster_id: int) -> Any:
        before = self.coverage() if self.coverage else None
        try:
            result = await self.delegate.create(broadcaster_id)
        except Exception as exc:
            if _is_rate_limited(exc):
                self.rate_limit_events.append(
                    {
                        "monotonic_ns": self.clock_ns(),
                        "broadcaster_id": broadcaster_id,
                        "retry_after_seconds": getattr(exc, "retry_after", None),
                    }
                )
            raise
        after = self.coverage() if self.coverage else None
        self.accepted_creates.append(
            {
                "monotonic_ns": self.clock_ns(),
                "broadcaster_id": broadcaster_id,
                "coverage_before": before,
                "coverage_after": after,
            }
        )
        return result

    async def delete(self, subscription_id: str) -> Any:
        return await self.delegate.delete(subscription_id)

    def record_backoff(
        self, *, seconds: float, coverage_before: int, coverage_after: int
    ) -> dict[str, Any]:
        event = {
            "monotonic_ns": self.clock_ns(),
            "seconds": float(seconds),
            "coverage_before": int(coverage_before),
            "coverage_after": int(coverage_after),
        }
        self.backoff_events.append(event)
        if not self.subscription_count_by_window:
            self.subscription_count_by_window.append(int(coverage_before))
        self.subscription_count_by_window.append(int(coverage_after))
        if coverage_after > coverage_before:
            self.accepted_create_windows.append(
                {
                    "before": int(coverage_before),
                    "after": int(coverage_after),
                    "monotonic_ns": event["monotonic_ns"],
                }
            )
        return event

    def record_progress(
        self, *, coverage_before: int, coverage_after: int
    ) -> dict[str, Any]:
        """Record an accepted-create window independently from its backoff."""

        if coverage_after <= coverage_before:
            raise ValueError("accepted create progress must increase coverage")
        event = {
            "before": int(coverage_before),
            "after": int(coverage_after),
            "monotonic_ns": self.clock_ns(),
        }
        self.accepted_create_windows.append(event)
        if not self.subscription_count_by_window:
            self.subscription_count_by_window.append(int(coverage_before))
        self.subscription_count_by_window.append(int(coverage_after))
        return event


def build_cold_start_record(
    target: int,
    initialization_complete_at: str,
    initial_subscription_count: int,
    transport: RecordingTransportProxy,
    poll_start_end_monotonic_ns: Sequence[Sequence[int]],
    scheduler_events: Sequence[Mapping[str, Any]],
    final_subscription_count: int,
    require_rate_limit_backoff: bool = True,
) -> dict[str, Any]:
    if require_rate_limit_backoff and (
        not transport.rate_limit_events or not transport.backoff_events
    ):
        raise ValueError("cold-start requires an observed real rate-limit backoff")
    if require_rate_limit_backoff and not transport.accepted_create_windows:
        raise ValueError("cold-start requires accepted-window coverage growth")
    intervals = [list(interval) for interval in poll_start_end_monotonic_ns]
    if not intervals:
        raise ValueError("cold-start requires at least one scheduled poll interval")
    durations = [
        (end - start) / 1_000_000.0 for start, end in intervals
    ]
    overlap = sum(
        event.get("kind") == "max_instances" for event in scheduler_events
    )
    if overlap:
        raise ValueError("cold-start scheduled polls must have zero overlap skips")
    if any(duration >= 10_000.0 for duration in durations):
        raise ValueError("cold-start completed polls must remain under 10 seconds")
    record = {
        "target": target,
        "initialization_complete_at": initialization_complete_at,
        "initial_subscription_count": initial_subscription_count,
        "rate_limit_events": list(transport.rate_limit_events),
        "backoff_events": list(transport.backoff_events),
        "accepted_create_windows": list(transport.accepted_create_windows),
        "subscription_count_by_window": list(transport.subscription_count_by_window),
        "poll_start_end_monotonic_ns": intervals,
        "poll_durations_ms": durations,
        "overlap_skip_count": overlap,
        "final_subscription_count": final_subscription_count,
    }
    validate_evidence_record("cold-start", record)
    record["acceptance_valid"] = (
        bool(transport.rate_limit_events)
        and bool(transport.backoff_events)
        and bool(transport.accepted_create_windows)
        and overlap == 0
        and all(duration < 10_000.0 for duration in durations)
    )
    return record


class OperationCounter:
    """Counts dispatch boundaries, not rows or queued batch commands."""

    def __init__(self) -> None:
        self.counts: Counter[str] = Counter()

    def increment(self, boundary: str) -> None:
        self.counts[boundary] += 1

    def report(self) -> dict[str, int]:
        return dict(sorted(self.counts.items()))


class CursorDispatchProxy:
    def __init__(self, delegate: Any, counter: OperationCounter) -> None:
        self._delegate = delegate
        self._counter = counter

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)

    def __enter__(self) -> "CursorDispatchProxy":
        self._delegate.__enter__()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> Any:
        return self._delegate.__exit__(exc_type, exc, traceback)

    def execute(self, *args: Any, **kwargs: Any) -> Any:
        query = args[0] if args else kwargs.get("query", "")
        if isinstance(query, bytes):
            query = query.decode("utf-8", errors="replace")
        normalized = " ".join(str(query).upper().split())
        if (
            "INSERT INTO STREAMERS" in normalized
            and "ON CONFLICT" in normalized
        ):
            boundary = "metadata_execute"
        elif (
            "SELECT STREAMER_ID FROM STREAMERS" in normalized
            and "ALLOWS_CLIPPING" in normalized
        ):
            boundary = "clipping_execute"
        else:
            boundary = "postgres_execute"
        self._counter.increment(boundary)
        return self._delegate.execute(*args, **kwargs)


class ConnectionDispatchProxy:
    def __init__(self, delegate: Any, counter: OperationCounter) -> None:
        self._delegate = delegate
        self._counter = counter

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)

    def cursor(self, *args: Any, **kwargs: Any) -> CursorDispatchProxy:
        return CursorDispatchProxy(self._delegate.cursor(*args, **kwargs), self._counter)

    def commit(self) -> Any:
        self._counter.increment("metadata_commit")
        return self._delegate.commit()

    def rollback(self) -> Any:
        self._counter.increment("metadata_rollback")
        return self._delegate.rollback()


class PoolDispatchProxy:
    """Wrap pooled connections while preserving getconn/putconn identity."""

    def __init__(self, delegate: Any, counter: OperationCounter) -> None:
        self._delegate = delegate
        self._counter = counter
        self._connections: dict[int, tuple[Any, ConnectionDispatchProxy]] = {}

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)

    def getconn(self, *args: Any, **kwargs: Any) -> ConnectionDispatchProxy:
        connection = self._delegate.getconn(*args, **kwargs)
        proxy = ConnectionDispatchProxy(connection, self._counter)
        self._connections[id(proxy)] = (connection, proxy)
        return proxy

    def putconn(
        self, connection: Any, *args: Any, **kwargs: Any
    ) -> Any:
        if isinstance(connection, ConnectionDispatchProxy):
            self._connections.pop(id(connection), None)
            connection = connection._delegate
        return self._delegate.putconn(connection, *args, **kwargs)


class PipelineDispatchProxy:
    def __init__(
        self, delegate: Any, counter: OperationCounter, *, transaction: bool
    ) -> None:
        self._delegate = delegate
        self._counter = counter
        self._transaction = transaction

    def __getattr__(self, name: str) -> Any:
        attribute = getattr(self._delegate, name)
        if not callable(attribute) or name == "execute":
            return attribute

        def delegated(*args: Any, **kwargs: Any) -> Any:
            result = attribute(*args, **kwargs)
            return self if result is self._delegate else result

        return delegated

    def __enter__(self) -> "PipelineDispatchProxy":
        self._delegate.__enter__()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> Any:
        return self._delegate.__exit__(exc_type, exc, traceback)

    def execute(self, *args: Any, **kwargs: Any) -> Any:
        boundary = (
            "desired_publication_execute"
            if self._transaction
            else "online_refresh_execute"
        )
        self._counter.increment(boundary)
        return self._delegate.execute(*args, **kwargs)


class RedisDispatchProxy:
    def __init__(self, delegate: Any, counter: OperationCounter) -> None:
        self._delegate = delegate
        self._counter = counter

    def __getattr__(self, name: str) -> Any:
        attribute = getattr(self._delegate, name)
        if not callable(attribute):
            return attribute

        def delegated(*args: Any, **kwargs: Any) -> Any:
            self._counter.increment(f"redis_{name}")
            return attribute(*args, **kwargs)

        return delegated

    def mget(self, *args: Any, **kwargs: Any) -> Any:
        self._counter.increment("online_snapshot_mget")
        return self._delegate.mget(*args, **kwargs)

    def pipeline(self, transaction: bool = True, *args: Any, **kwargs: Any) -> PipelineDispatchProxy:
        pipeline = self._delegate.pipeline(transaction=transaction, *args, **kwargs)
        return PipelineDispatchProxy(pipeline, self._counter, transaction=transaction)


def build_operation_count_record(
    *, case: str, scale: int | None, counts: Mapping[str, int]
) -> dict[str, Any]:
    allowed = {"stable", "complete_turnover", "empty-empty", "departures-only"}
    if case not in allowed:
        raise ValueError(f"unsupported operation-count case: {case}")
    if case in {"stable", "complete_turnover"} and scale not in {50, 500, 900}:
        raise ValueError("non-empty operation-count scale must be 50, 500, or 900")
    if any(not isinstance(value, int) or value < 0 for value in counts.values()):
        raise ValueError("operation counts must be non-negative integers")
    expected = dict(NON_EMPTY_DISPATCH_COUNTS)
    if case in {"empty-empty", "departures-only"}:
        expected = dict(EMPTY_DISPATCH_COUNTS)
        if case == "departures-only":
            expected["online_snapshot_mget"] = 1
    normalized = dict(counts)
    for boundary, expected_count in expected.items():
        actual_count = normalized.setdefault(boundary, 0)
        if actual_count != expected_count:
            raise ValueError(
                f"{boundary} must be {expected_count}, got {actual_count}"
            )
    if normalized.get("metadata_rollback", 0) != 0:
        raise ValueError("metadata_rollback must be 0 for successful evidence")
    return {
        "case": case,
        "scale": scale,
        "dispatch_counts": normalized,
        "acceptance_valid": True,
    }


def _target_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--redis-url", default=os.environ.get("TEST_REDIS_URL"))
    parser.add_argument("--postgres-url", default=os.environ.get("TEST_POSTGRES_URL"))
    parser.add_argument(
        "--namespace", default=os.environ.get("FEATURE006_NAMESPACE")
    )
    parser.add_argument(
        "--runtime-factory",
        default=os.environ.get("FEATURE006_RUNTIME_FACTORY"),
        help="operator integration callable as module:attribute",
    )
    run_id = os.environ.get("FEATURE006_RUN_ID")
    parser.add_argument("--run-id", default=run_id, required=not bool(run_id))
    parser.add_argument("--output", type=Path, required=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run feature 006 evidence against explicitly isolated targets."
    )
    commands = parser.add_subparsers(dest="command", required=True)

    operation = commands.add_parser("operation-counts")
    _target_arguments(operation)
    operation.add_argument("--scales", nargs="+", type=int, default=[50, 500, 900])
    operation.add_argument(
        "--case",
        action="append",
        dest="cases",
        choices=("stable", "complete-turnover", "empty-empty", "departures-only"),
    )

    calibrate = commands.add_parser("calibrate")
    _target_arguments(calibrate)
    calibrate.add_argument("--minimum-page-samples", type=int, default=20)
    calibrate.add_argument("--scales", nargs="+", type=int, default=[500, 900])

    profile = commands.add_parser("poll-profile")
    _target_arguments(profile)
    profile.add_argument("--scale", type=int, choices=(500, 900), required=True)
    profile.add_argument(
        "--profile", choices=("stable", "complete-turnover", "complete_turnover"), required=True
    )
    profile.add_argument("--warmups", type=int, default=1)
    profile.add_argument("--measured-polls", type=int, default=20)
    profile.add_argument("--calibration", type=Path, required=True)

    steady = commands.add_parser("steady-state")
    _target_arguments(steady)
    steady.add_argument("--scale", type=int, choices=(500, 900), required=True)
    steady.add_argument("--minutes", type=float, default=30.0)

    cold = commands.add_parser("cold-start")
    _target_arguments(cold)
    cold.add_argument("--scale", type=int, choices=(900,), default=900)
    cold.add_argument("--require-rate-limit-backoff", action="store_true")
    return parser


def _load_runtime(factory_spec: str | None, args: argparse.Namespace) -> Any:
    if not factory_spec:
        raise ValueError(
            "execution requires --runtime-factory module:attribute; "
            "no synthetic success fallback is available"
        )
    module_name, separator, attribute_name = factory_spec.partition(":")
    if not separator or not module_name or not attribute_name:
        raise ValueError("runtime factory must use module:attribute syntax")
    factory = getattr(importlib.import_module(module_name), attribute_name)
    runtime = factory(args)
    if runtime is None:
        raise ValueError("runtime factory returned no integration")
    return runtime


async def _invoke(callable_target: Any, *args: Any, **kwargs: Any) -> Any:
    result = callable_target(*args, **kwargs)
    if inspect.isawaitable(result):
        return await result
    return result


def _read_calibration(path: Path, scale: int) -> Mapping[str, Any]:
    matches = []
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            row = json.loads(line)
            if row.get("kind") == "calibration" and row.get("scale") == scale:
                matches.append(row)
    if not matches:
        raise ValueError(f"calibration has no scale {scale} record")
    validate_evidence_record("calibration", matches[-1])
    return matches[-1]


async def _run_command(args: argparse.Namespace, runtime: Any, writer: JsonlWriter) -> None:
    if args.command == "operation-counts":
        cases = args.cases or ["stable", "complete-turnover"]
        for requested_case in cases:
            case = requested_case.replace("-", "_")
            evidence_case = requested_case if requested_case in {"empty-empty", "departures-only"} else case
            scales = args.scales if case in {"stable", "complete_turnover"} else [None]
            for scale in scales:
                counter = OperationCounter()
                fixture = (
                    build_ranking_fixture(
                        scale,
                        "A",
                        disabled_proportion=0.0,
                        page_delay_ms=0.0,
                    )
                    if scale
                    else None
                )
                counts = await _invoke(
                    runtime.run_operation_count,
                    case=case,
                    scale=scale,
                    fixture=fixture,
                    counter=counter,
                )
                if counts is None:
                    counts = counter.report()
                writer.write(
                    "operation-counts",
                    build_operation_count_record(
                        case=evidence_case, scale=scale, counts=counts
                    ),
                )
    elif args.command == "calibrate":
        minimum = args.minimum_page_samples
        if minimum < 20:
            raise ValueError("--minimum-page-samples must be at least 20")
        if hasattr(runtime, "get_streams"):
            live = await collect_get_streams_page_samples(
                runtime.get_streams, minimum_samples=minimum
            )
        else:
            live = await collect_live_page_samples(
                runtime.fetch_live_page, minimum_samples=minimum
            )
        redis_samples = await collect_rtt_samples(
            runtime.redis_round_trip, minimum_samples=20
        )
        postgres_samples = await collect_rtt_samples(
            runtime.postgres_round_trip, minimum_samples=20
        )
        disabled = float(await _invoke(runtime.observed_disabled_proportion))
        page_delay = nearest_rank_p95(live)
        for scale in args.scales:
            fixture = build_ranking_fixture(
                scale, "A", disabled_proportion=disabled, page_delay_ms=page_delay
            )
            writer.write(
                "calibration",
                build_calibration_record(
                    scale=scale,
                    live_page_samples_ms=live,
                    fixture=fixture,
                    redis_rtt_samples_ms=redis_samples,
                    postgres_rtt_samples_ms=postgres_samples,
                ),
            )
    elif args.command == "poll-profile":
        calibration = _read_calibration(args.calibration, args.scale)
        calibration_valid = (
            40.0 <= calibration["redis_median_ms"] <= 110.0
            and 40.0 <= calibration["postgres_median_ms"] <= 110.0
        )
        profile = _normalized_profile(args.profile)
        fixtures = {
            fixture_id: build_ranking_fixture(
                args.scale,
                fixture_id,
                disabled_proportion=calibration["disabled_proportion"],
                page_delay_ms=calibration["fixture_page_delay_ms"],
            )
            for fixture_id in ("A", "B")
        }

        async def prepare(fixture_id: str) -> None:
            await _invoke(
                runtime.prepare_profile_state,
                profile=profile,
                fixture=fixtures[fixture_id],
                opposite_fixture=fixtures["B" if fixture_id == "A" else "A"],
            )

        async def poll(fixture_id: str) -> Mapping[str, Any]:
            started_ns = time.monotonic_ns()
            result = dict(
                await _invoke(runtime.run_profile_poll, fixtures[fixture_id])
            )
            result["duration_ms"] = (
                time.monotonic_ns() - started_ns
            ) / 1_000_000.0
            return result

        record = await run_profile_measurements_async(
            scale=args.scale,
            profile=profile,
            warmups=args.warmups,
            measured_polls=args.measured_polls,
            prepare_state=prepare,
            run_poll=poll,
        )
        record = build_poll_profile_record(
            scale=args.scale,
            profile=profile,
            fixture=fixtures["A"],
            warmup_duration_ms=record["warmup_duration_ms"],
            measured_durations_ms=record["measured_durations_ms"],
            overlap_skip_count=record["overlap_skip_count"],
            excluded_poll_count=record["excluded_poll_count"],
            phase_durations_ms=record["phase_durations_ms"],
            dispatch_counts=record["dispatch_counts"],
        )
        record["acceptance_valid"] = (
            calibration_valid
            and record["overlap_skip_count"] == 0
            and record["nearest_rank_p95_ms"]
            < (5000.0 if args.scale == 500 else 10000.0)
            and (
                args.scale != 900
                or max(record["measured_durations_ms"], default=0.0) < 120_000.0
            )
        )
        writer.write("poll-profile", record)
    elif args.command == "steady-state":
        if args.minutes < 30:
            raise ValueError("steady-state acceptance requires at least 30 minutes")
        pass_recorder = PassCompletionRecorder()
        scheduler = SchedulerEventRecorder()
        started_at = await _invoke(runtime.wait_for_convergence, args.scale)
        if started_at is None:
            started_at = utc_now()
        production_callback = getattr(
            runtime, "production_pass_callback", None
        )
        if not callable(production_callback):
            raise ValueError(
                "steady-state runtime must expose production_pass_callback"
            )
        clock_ns = getattr(runtime, "monotonic_ns", time.monotonic_ns)
        run_started_ns = clock_ns()
        pass_callback = compose_pass_callbacks(
            production_callback,
            pass_recorder.record,
        )
        await _invoke(
            runtime.run_steady_state,
            scale=args.scale,
            duration_seconds=args.minutes * 60.0,
            pass_callback=pass_callback,
            scheduler_callback=scheduler.record,
        )
        run_duration_seconds = (clock_ns() - run_started_ns) / 1_000_000_000.0
        record = build_reconciler_gap_record(
            scale=args.scale,
            post_convergence_started_at=started_at,
            run_duration_seconds=run_duration_seconds,
            pass_completion_monotonic_ns=pass_recorder.completions,
            scheduler_events=scheduler.events,
        )
        record["gap_gate_ms"] = 15000.0 if args.scale == 500 else 20000.0
        record["acceptance_valid"] = (
            record["maximum_gap_ms"] <= record["gap_gate_ms"]
            and not any(e["kind"] in {"error", "missed", "max_instances"} for e in scheduler.events)
        )
        writer.write("reconciler-gap", record)
    else:
        if not args.require_rate_limit_backoff:
            raise ValueError("cold-start requires --require-rate-limit-backoff")
        initialization = await _invoke(runtime.initialize_cold_start, target=900)
        if not isinstance(initialization, Mapping):
            raise ValueError("cold-start initialization must return a mapping")
        if (
            not initialization.get("process_initialized")
            or not initialization.get("pools_warm")
            or not initialization.get("transport_started")
            or initialization.get("subscription_count") != 0
            or initialization.get("desired_count") != 900
        ):
            raise ValueError("cold-start preconditions were not satisfied")
        scheduler = SchedulerEventRecorder()
        polls = ScheduledPollRecorder()
        proxy = RecordingTransportProxy(
            initialization["transport"],
            coverage=initialization.get("coverage"),
        )
        final_count = await _invoke(
            runtime.run_cold_start,
            target=900,
            transport=proxy,
            poll_recorder=polls,
            scheduler_callback=scheduler.record,
        )
        writer.write(
            "cold-start",
            build_cold_start_record(
                target=900,
                initialization_complete_at=initialization.get(
                    "initialization_complete_at", utc_now()
                ),
                initial_subscription_count=0,
                transport=proxy,
                poll_start_end_monotonic_ns=polls.intervals,
                scheduler_events=scheduler.events,
                final_subscription_count=int(final_count),
                require_rate_limit_backoff=True,
            ),
        )


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        validate_isolated_targets(
            redis_url=args.redis_url,
            postgres_url=args.postgres_url,
            namespace=args.namespace,
        )
        if args.command == "operation-counts":
            if any(scale not in {50, 500, 900} for scale in args.scales):
                raise ValueError("--scales values must be 50, 500, or 900")
        elif args.command == "calibrate":
            if any(scale not in {500, 900} for scale in args.scales):
                raise ValueError("calibration scales must be 500 or 900")
        runtime = _load_runtime(args.runtime_factory, args)
        writer = JsonlWriter(args.output, run_id=args.run_id)
        asyncio.run(_run_command(args, runtime, writer))
    except (ValueError, AttributeError, ImportError, OSError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
