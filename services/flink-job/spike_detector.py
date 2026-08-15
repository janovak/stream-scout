"""
Pure spike-detection arithmetic, with no pyflink import.

Flink owns the state (MapState/ValueState) for checkpointing; this module takes
that state in as plain values and returns what should change. See
AnomalyDetector in clip_detector_job.py for the Flink adapter that calls this.
"""

import os
import statistics
from dataclasses import dataclass
from typing import List, Mapping, Optional


@dataclass(frozen=True)
class DetectorConfig:
    window_seconds: int = 5
    baseline_seconds: int = 10      # today's value. Plan 06 restores this to 300 --
                                     # 10 was an accidental regression in commit c7afdab.
                                     # Do NOT change it here; that is a behaviour change.
    std_dev_threshold: float = 5.0
    cooldown_seconds: int = 30
    min_baseline_fraction: float = 0.8

    @classmethod
    def from_env(cls) -> "DetectorConfig":
        return cls(
            window_seconds=int(os.getenv("DETECTION_WINDOW_SECONDS", cls.window_seconds)),
            baseline_seconds=int(os.getenv("DETECTION_BASELINE_SECONDS", cls.baseline_seconds)),
            std_dev_threshold=float(os.getenv("DETECTION_STD_DEV_THRESHOLD", cls.std_dev_threshold)),
            cooldown_seconds=int(os.getenv("DETECTION_COOLDOWN_SECONDS", cls.cooldown_seconds)),
        )


@dataclass(frozen=True)
class Spike:
    message_count: int
    baseline_mean: float
    baseline_std: float
    intensity: float


@dataclass(frozen=True)
class Decision:
    spike: Optional[Spike]
    expired_buckets: List[int]      # operator removes these from MapState


def evaluate(
    counts: Mapping[int, int],      # bucket second -> message count
    now_seconds: int,
    last_spike_ms: Optional[int],
    config: DetectorConfig,
) -> Decision:
    """Pure. No I/O, no clock, no globals. The caller supplies now_seconds.

    Moved unchanged from AnomalyDetector.process_element: same baseline/window
    overlap, same unit mix between window_sum and mean/std, same warm-up gate.
    Plan 06 fixes the arithmetic; this only gives it a seam.
    """
    baseline_start = now_seconds - config.baseline_seconds
    window_start = now_seconds - config.window_seconds

    counts_baseline = []
    counts_window = []
    expired_buckets = []

    for ts_bucket, count in counts.items():
        if ts_bucket < baseline_start:
            expired_buckets.append(ts_bucket)
        elif ts_bucket >= baseline_start:
            counts_baseline.append(count)
            if ts_bucket >= window_start:
                counts_window.append(count)

    min_required = int(config.baseline_seconds * config.min_baseline_fraction)
    if len(counts_baseline) < min_required:
        return Decision(spike=None, expired_buckets=expired_buckets)

    if len(counts_baseline) >= 2:
        mean = statistics.mean(counts_baseline)
        std_dev = statistics.stdev(counts_baseline)
    else:
        return Decision(spike=None, expired_buckets=expired_buckets)

    window_sum = sum(counts_window) if counts_window else 0

    threshold = mean + (config.std_dev_threshold * std_dev)
    if window_sum > threshold and std_dev > 0:
        current_ms = now_seconds * 1000
        if last_spike_ms is None or (current_ms - last_spike_ms) > (config.cooldown_seconds * 1000):
            intensity = (window_sum - mean) / std_dev if std_dev > 0 else 0.0
            spike = Spike(
                message_count=window_sum,
                baseline_mean=mean,
                baseline_std=std_dev,
                intensity=intensity,
            )
            return Decision(spike=spike, expired_buckets=expired_buckets)

    return Decision(spike=None, expired_buckets=expired_buckets)
