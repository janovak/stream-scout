#!/usr/bin/env python3
"""
Write the detector reading of every second from a corpus, for Plan 06 Phase 4.

Step 17 asks for the full intensity distribution. The output of the detector
cannot give it. `Decision.emit` carries the peak of an elevation period that
ended before it. It thus gives one value for each period, and no value for
the quiet seconds between the periods. Those quiet seconds are most of them.
A distribution from that data has the same fault that Plan 06 describes for
the earlier edge-triggered detector.

This tool therefore replays the corpus through tools/replay.py with the
trigger off. It writes one row for each evaluated broadcaster-second, from
`Decision.measurement`. That field holds the arithmetic of the detector
itself. It is not a second copy of that arithmetic. The measured
distribution therefore agrees with the deployed detector.

`intensity` does not depend on `k`, `hold_cap_seconds` or `cooldown_seconds`.
Those three fields control what the state machine does with a reading. They
do not control the reading. One run thus serves each candidate value of all
three. tools/analyze_corpus.py builds the episodes again from this dump. It
does not replay the corpus for each candidate.

The warm-up gate is different, because it controls whether a reading exists.
Run this tool with the gate open. The default `--min-baseline-fraction` of
0.0001 gives a gate of 0 seconds. Each row then carries its own
`observed_seconds`. A stricter fraction is thus a filter that you apply
later. Step 22 needs the data on both sides of that filter.

Usage:
    python tools/measure_corpus.py \\
        --corpus ~/stream-scout-corpus/chat-corpus.jsonl \\
        --out /tmp/readings.tsv
"""

import argparse
import json
import math
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from spike_detector import DetectorConfig  # noqa: E402
from tools.replay import replay  # noqa: E402

COLUMNS = (
    "broadcaster_id",
    "second",
    "observed_seconds",
    "status",
    "window_count",
    "baseline_mean",
    "baseline_std",
    "intensity",
)

# status values
OK = "ok"           # measured; the numeric columns are populated
WARMUP = "warmup"   # the warm-up gate rejected the second
FLAT = "flat"       # the baseline had no spread, so there was nothing to divide by


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--corpus", required=True, help="Path to the captured chat JSONL")
    parser.add_argument("--out", required=True, help="Path for the TSV of per-second readings")
    parser.add_argument("--window-seconds", type=int, default=DetectorConfig.window_seconds)
    parser.add_argument("--baseline-seconds", type=int, default=DetectorConfig.baseline_seconds)
    parser.add_argument(
        "--min-baseline-fraction",
        type=float,
        default=0.0001,
        help="Deliberately near-zero: record the seconds a stricter gate would "
             "reject, and let analysis apply the gate afterwards (step 22). "
             "DetectorConfig rejects exactly 0.",
    )
    parser.add_argument(
        "--k",
        type=float,
        default=float("inf"),
        help="The trigger. Defaults to infinity, which disables it: no period "
             "ever opens, so nothing censors the readings (step 17).",
    )
    parser.add_argument("--progress-every", type=int, default=200_000,
                        help="Report progress to stderr every N corpus lines (0 to silence)")
    return parser.parse_args()


def build_config(args) -> DetectorConfig:
    return DetectorConfig(
        window_seconds=args.window_seconds,
        baseline_seconds=args.baseline_seconds,
        k=args.k,
        # A run with the trigger off does not use these two values. No period
        # can open, so the code does not read them. Use zero, and not the
        # shipped default. DetectorConfig rejects a cap that is not shorter
        # than baseline_seconds + window_seconds. The shipped cap of 25 would
        # thus refuse `--baseline-seconds 10`. That is the usual way to test
        # this tool. The caller did not set the cap, and the run never reads
        # it, so it must not stop the run.
        hold_cap_seconds=0,
        cooldown_seconds=0,
        min_baseline_fraction=args.min_baseline_fraction,
    )


def progress_lines(path, every, out=sys.stderr):
    """Yield the corpus's lines, reporting throughput as they go."""
    started = time.monotonic()
    with open(path) as f:
        for index, line in enumerate(f, start=1):
            if every and index % every == 0:
                elapsed = time.monotonic() - started
                print(f"  {index:>10,} lines  {elapsed:7.1f}s  "
                      f"{index / elapsed:,.0f} lines/s", file=out, flush=True)
            yield line


def status_of(evaluation, min_observed_seconds):
    if evaluation.measurement is not None:
        return OK
    # Both unmeasurable paths return measurement=None. The gate is the reason
    # only when the key has not been watched long enough; otherwise the
    # baseline had no spread. evaluate() tests them in this order.
    return WARMUP if evaluation.observed_seconds < min_observed_seconds else FLAT


def main():
    args = parse_args()
    config = build_config(args)
    min_observed_seconds = int(config.baseline_seconds * config.min_baseline_fraction)

    counts = {OK: 0, WARMUP: 0, FLAT: 0}
    broadcasters = set()
    started = time.monotonic()

    with open(args.out, "w") as out:
        # The dump describes itself. Analysis needs baseline_seconds, to
        # change a candidate min_baseline_fraction into a number of seconds.
        # A reader also needs to know that this was not a usual detector run.
        out.write("#" + json.dumps({
            "corpus": str(args.corpus),
            "window_seconds": config.window_seconds,
            "baseline_seconds": config.baseline_seconds,
            # As a string when infinite. json.dumps would otherwise write the
            # bare word Infinity, which Python reads back but no other JSON
            # parser accepts. Nothing downstream reads this field; it is here
            # so a reader can see that the trigger was off.
            "k": "disabled" if math.isinf(config.k) else config.k,
            "min_baseline_fraction": config.min_baseline_fraction,
            "min_observed_seconds": min_observed_seconds,
        }) + "\n")
        out.write("\t".join(COLUMNS) + "\n")

        for evaluation in replay(progress_lines(args.corpus, args.progress_every), config):
            status = status_of(evaluation, min_observed_seconds)
            counts[status] += 1
            broadcasters.add(evaluation.broadcaster_id)
            reading = evaluation.measurement
            if reading is None:
                out.write(f"{evaluation.broadcaster_id}\t{evaluation.second}\t"
                          f"{evaluation.observed_seconds}\t{status}\t\t\t\t\n")
            else:
                out.write(f"{evaluation.broadcaster_id}\t{evaluation.second}\t"
                          f"{evaluation.observed_seconds}\t{status}\t"
                          f"{reading.message_count}\t{reading.baseline_mean:.6f}\t"
                          f"{reading.baseline_std:.6f}\t{reading.intensity:.6f}\n")

    total = sum(counts.values())
    elapsed = time.monotonic() - started
    print(f"wrote {total:,} broadcaster-seconds from {len(broadcasters)} broadcasters "
          f"to {args.out} in {elapsed:,.0f}s", file=sys.stderr)
    for status in (OK, WARMUP, FLAT):
        share = counts[status] / total if total else 0.0
        print(f"  {status:<7} {counts[status]:>10,}  {share:6.2%}", file=sys.stderr)


if __name__ == "__main__":
    main()
