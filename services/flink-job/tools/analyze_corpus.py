#!/usr/bin/env python3
"""
Change a tools/measure_corpus.py dump into the Plan 06 Phase 4 tables.

Steps 18 to 22 read the same per-second dump. `intensity` does not depend on
`k`, `hold_cap_seconds` or `cooldown_seconds`. Those three fields control
what the state machine does with a reading. They do not control the reading.
This tool thus measures the cost of each candidate value. It runs the state
machine again over the recorded readings. It does not replay 635 MB of chat
one more time.

The state machine here is `reconstruct`. It agrees with the hold, cooldown
and cap branches of spike_detector.evaluate(). But it is a second copy of
that logic, so it can become different from the first. `--verify` is the
control for that risk. It replays a corpus through the real detector at the
same settings. Then it compares the episodes, one against one. Run it before
you use a table below.

Usage:
    python tools/analyze_corpus.py /tmp/readings.tsv
    python tools/analyze_corpus.py /tmp/readings.tsv --step 19 --k 4.0
    python tools/analyze_corpus.py /tmp/readings.tsv --verify corpus/dev-slice.jsonl
"""

import argparse
import json
import os
import subprocess
import sys
from array import array
from dataclasses import dataclass
from pathlib import Path

OK, WARMUP, FLAT = 0, 1, 2
STATUS_CODES = {"ok": OK, "warmup": WARMUP, "flat": FLAT}


class Series:
    """One broadcaster's readings, in evaluation order (ascending seconds)."""

    __slots__ = ("second", "observed", "status", "count", "mean", "std", "intensity", "_index")

    def __init__(self):
        self.second = array("q")
        self.observed = array("h")
        self.status = array("b")
        self.count = array("i")
        self.mean = array("d")
        self.std = array("d")
        self.intensity = array("d")
        self._index = None

    def __len__(self):
        return len(self.second)

    def index_of(self, second):
        """Position of `second`, or None. Built lazily: only step 21 needs it."""
        if self._index is None:
            self._index = {s: i for i, s in enumerate(self.second)}
        return self._index.get(second)


@dataclass
class Readings:
    config: dict
    by_broadcaster: dict  # broadcaster_id -> Series

    @property
    def baseline_seconds(self):
        return self.config["baseline_seconds"]

    @property
    def window_seconds(self):
        return self.config["window_seconds"]

    def rows(self):
        for broadcaster_id, series in self.by_broadcaster.items():
            yield broadcaster_id, series

    def span_hours(self):
        first = min(s.second[0] for s in self.by_broadcaster.values())
        last = max(s.second[-1] for s in self.by_broadcaster.values())
        return (last - first) / 3600.0


def load(path) -> Readings:
    by_broadcaster = {}
    config = None
    with open(path) as f:
        for line in f:
            if line.startswith("#"):
                # json.loads accepts the non-standard `Infinity` that
                # json.dumps writes for a disabled trigger.
                config = json.loads(line[1:])
                continue
            fields = line.rstrip("\n").split("\t")
            if fields[0] == "broadcaster_id":
                continue
            broadcaster_id = int(fields[0])
            series = by_broadcaster.get(broadcaster_id)
            if series is None:
                series = by_broadcaster[broadcaster_id] = Series()
            status = STATUS_CODES[fields[3]]
            series.second.append(int(fields[1]))
            series.observed.append(int(fields[2]))
            series.status.append(status)
            if status == OK:
                series.count.append(int(fields[4]))
                series.mean.append(float(fields[5]))
                series.std.append(float(fields[6]))
                series.intensity.append(float(fields[7]))
            else:
                # Keep every array the same length so one index addresses a
                # whole row. These three are never read for a non-ok row.
                series.count.append(0)
                series.mean.append(0.0)
                series.std.append(0.0)
                series.intensity.append(float("nan"))
    if config is None:
        raise SystemExit(f"{path}: no '#' config header -- not a measure_corpus.py dump")
    return Readings(config=config, by_broadcaster=by_broadcaster)


# --------------------------------------------------------------------------
# statistics


def percentile(sorted_values, q):
    """Linear-interpolated percentile of an already-sorted list. q in [0, 1]."""
    if not sorted_values:
        return float("nan")
    if len(sorted_values) == 1:
        return sorted_values[0]
    position = q * (len(sorted_values) - 1)
    low = int(position)
    high = min(low + 1, len(sorted_values) - 1)
    weight = position - low
    return sorted_values[low] * (1 - weight) + sorted_values[high] * weight


def table(headers, rows):
    """A markdown table, so the output pastes straight into a PR."""
    widths = [len(str(h)) for h in headers]
    rendered = [[str(cell) for cell in row] for row in rows]
    for row in rendered:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    out = ["| " + " | ".join(str(h).ljust(widths[i]) for i, h in enumerate(headers)) + " |",
           "|" + "|".join("-" * (w + 2) for w in widths) + "|"]
    for row in rendered:
        out.append("| " + " | ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)) + " |")
    return "\n".join(out)


def heading(text):
    print(f"\n## {text}\n")


# --------------------------------------------------------------------------
# the detector's state machine, replayed over recorded readings


@dataclass(frozen=True)
class Episode:
    broadcaster_id: int
    started_at: int      # first elevated second
    fired_at: int        # the second the detector reported
    peak_at: int
    peak_intensity: float
    peak_count: int      # messages in the peak second's window -- step 21's burst
    start_mean: float    # the baseline at started_at -- the pre-spike reference
    start_std: float
    capped: bool         # reported because the cap expired, not because it fell back


def reconstruct(readings, k, cap, cooldown, gate):
    """Every episode the detector would report at these settings.

    Mirrors spike_detector.evaluate(): the hold survives an unmeasurable
    second without changing, the cap is tested against both the peak's age
    and the hold's start, and the cooldown blocks opening a period but never
    ends one that is already open. `--verify` checks this against the real
    detector.
    """
    episodes = []
    for broadcaster_id, series in readings.rows():
        # The open hold, as (started_at, peak_index, start_index), or None.
        # Indices rather than values so an emitted Episode carries one whole
        # measurement, as HoldState does.
        hold = None
        last_fire = None

        def emit(hold, fired_at, capped):
            started_at, peak_i, start_i = hold
            return Episode(
                broadcaster_id=broadcaster_id,
                started_at=started_at,
                fired_at=fired_at,
                peak_at=series.second[peak_i],
                peak_intensity=series.intensity[peak_i],
                peak_count=series.count[peak_i],
                start_mean=series.mean[start_i],
                start_std=series.std[start_i],
                capped=capped,
            )

        for i in range(len(series)):
            second = series.second[i]

            # evaluate() drops a hold whose peak is older than the cap before
            # anything else, including the warm-up gate.
            if hold is not None and (second - series.second[hold[1]]) > cap:
                hold = None

            if series.status[i] != OK or series.observed[i] < gate:
                continue  # unmeasurable: the hold is kept, untouched

            intensity = series.intensity[i]
            elevated = intensity >= k

            if hold is None:
                if not elevated:
                    continue
                if last_fire is not None and (second - last_fire) <= cooldown:
                    continue
                hold = (second, i, i)
            elif elevated:
                # Equal values keep the earlier second, as HoldState.with_peak
                # does, so a flat peak reports where it started.
                if intensity > series.intensity[hold[1]]:
                    hold = (hold[0], i, hold[2])
            else:
                episodes.append(emit(hold, second, capped=False))
                last_fire, hold = second, None
                continue

            if second - hold[0] >= cap:
                episodes.append(emit(hold, second, capped=True))
                last_fire, hold = second, None
    return episodes


def elevated_runs(readings, k, gate):
    """Maximal runs of consecutive elevated seconds -- no cap, no cooldown.

    This is the underlying phenomenon step 19 asks about ("how long do they
    stay elevated"), separate from any config that truncates it. A run breaks
    on a gap in evaluated seconds as well as on a reading under `k`, so a
    lapsed timer chain never joins two distant spikes.
    """
    runs = []
    for broadcaster_id, series in readings.rows():
        start = None
        previous_second = None
        peak = None
        for i in range(len(series)):
            second = series.second[i]
            measurable = series.status[i] == OK and series.observed[i] >= gate
            elevated = measurable and series.intensity[i] >= k
            contiguous = previous_second is not None and second == previous_second + 1
            if elevated and start is not None and contiguous:
                if series.intensity[i] > peak:
                    peak = series.intensity[i]
            elif elevated:
                if start is not None:
                    runs.append((broadcaster_id, start, previous_second, peak))
                start, peak = second, series.intensity[i]
            else:
                if start is not None:
                    runs.append((broadcaster_id, start, previous_second, peak))
                start = peak = None
            previous_second = second
        if start is not None:
            runs.append((broadcaster_id, start, previous_second, peak))
    return runs


def appearances(readings):
    """Maximal runs of consecutive evaluated seconds, per broadcaster.

    One appearance is one stretch of a channel being watched. The timer chain
    lapses once a key's last bucket expires, so a gap here is a channel that
    left and came back -- and came back with an empty baseline.
    """
    out = []
    for broadcaster_id, series in readings.rows():
        start = 0
        for i in range(1, len(series)):
            if series.second[i] != series.second[i - 1] + 1:
                out.append((broadcaster_id, start, i - 1))
                start = i
        out.append((broadcaster_id, start, len(series) - 1))
    return out


# --------------------------------------------------------------------------
# step 17/18 -- the distribution, and k off a percentile


def step_17_18(readings, gate, cap, cooldown, candidates):
    heading("Step 17/18 -- the uncensored per-second distribution, and k")

    # Test the gate first, then the spread. evaluate() uses that order. A
    # second that fails both tests thus belongs to the gate. A different order
    # gives a warm-up cost that disagrees with step 22 for the same dump.
    #
    # The order is also necessary for correctness. A row that the dump could
    # not measure holds no intensity. The `else` branch would put that
    # non-number into the distribution. main() also refuses a gate that is
    # more open than the gate of the dump. That is the only condition that
    # could let such a row get past the gate.
    measurable, warm, blocked, flat = [], 0, 0, 0
    for _, series in readings.rows():
        for i in range(len(series)):
            if series.observed[i] < gate:
                blocked += 1
            elif series.status[i] != OK:
                flat += 1
            else:
                warm += 1
                measurable.append(series.intensity[i])

    total = warm + blocked + flat
    print(f"{total:,} broadcaster-seconds over {readings.span_hours():.1f}h "
          f"from {len(readings.by_broadcaster)} broadcasters.\n")
    print(table(
        ["outcome", "seconds", "share"],
        [["measured (past the gate)", f"{warm:,}", f"{warm / total:.2%}"],
         [f"blocked by the warm-up gate (observed < {gate}s)", f"{blocked:,}",
          f"{blocked / total:.2%}"],
         ["baseline had no spread", f"{flat:,}", f"{flat / total:.2%}"]],
    ))

    ordered = sorted(measurable)
    quantiles = [0.0, 0.25, 0.5, 0.75, 0.9, 0.99, 0.999, 0.9999, 0.99999, 1.0]
    print("\nIntensity, every measured second (the step 17 distribution):\n")
    print(table(
        ["quantile", "intensity"],
        [[f"{q:.5g}" if q not in (0.0, 1.0) else ("min" if q == 0.0 else "max"),
          f"{percentile(ordered, q):.3f}"] for q in quantiles],
    ))

    hours = readings.span_hours()
    # Use broadcaster-hours, and not clock hours. The rate at one channel is
    # what shows whether a threshold is correct. The corpus watches
    # approximately 19 channels at the same time.
    broadcaster_hours = total / 3600.0
    rows = []
    for k in candidates:
        # Use the cap and the cooldown that this run received. Do not use
        # fixed values. A fixed cap here measures each k against a config that
        # the project does not ship. The cap also controls how many episodes
        # one long elevation becomes.
        episodes = reconstruct(readings, k, cap=cap, cooldown=cooldown, gate=gate)
        over = sum(1 for v in ordered if v >= k)
        # What the trigger means in chat, not in standard deviations: how many
        # times its own resting rate a channel has to reach to cross it.
        bursts = sorted(
            (e.peak_count / readings.window_seconds) / e.start_mean
            for e in episodes if e.start_mean > 0
        )
        per_broadcaster = {}
        for e in episodes:
            per_broadcaster[e.broadcaster_id] = per_broadcaster.get(e.broadcaster_id, 0) + 1
        rows.append([
            f"{k:.1f}",
            f"{over / len(ordered):.4%}",
            f"{len(episodes):,}",
            f"{len(episodes) / broadcaster_hours:.2f}",
            f"{len(episodes) / hours * 24:,.0f}",
            f"{percentile(bursts, 0.5):.1f}x" if bursts else "-",
            f"{len(per_broadcaster)}/{len(readings.by_broadcaster)}",
            f"{max(per_broadcaster.values()) / len(episodes):.0%}" if episodes else "-",
        ])
    print(f"\nWhat each candidate k costs (cap {cap}, cooldown {cooldown}):\n")
    print(table(
        ["k", "seconds >= k", "episodes/12h", "per broadcaster-hour", "clips/day",
         "median burst vs resting", "broadcasters firing", "busiest channel's share"],
        rows,
    ))


# --------------------------------------------------------------------------
# step 19 -- hold_cap_seconds


def step_19(readings, k, cooldown, gate, caps):
    heading(f"Step 19 -- hold_cap_seconds, from how long real spikes stay elevated (k={k})")

    runs = elevated_runs(readings, k, gate)
    if not runs:
        print(f"No elevated runs at k={k}.")
        return
    lengths = [end - start + 1 for _, start, end, _ in runs]
    ordered = sorted(lengths)
    print(f"{len(runs):,} elevated runs at k={k}, measured with no cap and no cooldown.\n")
    print(table(
        ["quantile", "seconds elevated"],
        [[name, f"{percentile(ordered, q):.1f}"] for name, q in
         [("min", 0.0), ("p50", 0.5), ("p75", 0.75), ("p90", 0.9), ("p95", 0.95),
          ("p99", 0.99), ("max", 1.0)]],
    ))

    # The table above measures the phenomenon. It is not the cap decision.
    # elevated_runs() ends a run at a second the detector could not measure,
    # and at a gap in evaluated seconds. evaluate() does neither: it keeps the
    # hold across those seconds and counts the cap from hold.started_at. A
    # channel that is elevated for 10s, blind for 1s, then elevated for 15s
    # more is two runs here and one 26-second period there.
    #
    # Use the state machine of the detector itself. The code sets `capped`
    # when the cap ended a period. It does not set `capped` when the reading
    # fell below the trigger. `capped` thus counts the periods that this cap
    # cut short.
    rows = []
    for cap in caps:
        episodes = reconstruct(readings, k, cap=cap, cooldown=cooldown, gate=gate)
        capped = [e for e in episodes if e.capped]
        shortfalls = []
        for episode in capped:
            series = readings.by_broadcaster[episode.broadcaster_id]
            # What the period would have peaked at with no cap: the best
            # reading from its onset until the intensity first falls back.
            i = series.index_of(episode.started_at)
            best = episode.peak_intensity
            second = episode.started_at
            while i is not None and i < len(series) and series.second[i] == second:
                if series.status[i] != OK or series.observed[i] < gate:
                    break
                if series.intensity[i] < k:
                    break
                best = max(best, series.intensity[i])
                i += 1
                second += 1
            shortfalls.append((best - episode.peak_intensity) / best if best > 0 else 0.0)
        rows.append([
            str(cap),
            f"{len(capped):,}",
            f"{len(capped) / len(episodes):.2%}" if episodes else "-",
            f"{percentile(sorted(shortfalls), 0.5):.2%}" if shortfalls else "-",
            f"{percentile(sorted(shortfalls), 0.95):.2%}" if shortfalls else "-",
        ])
    print(f"\nWhat each cap cuts short, from the detector's own state machine "
          f"(cooldown {cooldown}), and how much of the peak that loses:\n")
    print(table(
        ["cap (s)", "periods cut short", "share of periods", "median peak lost",
         "p95 peak lost"],
        rows,
    ))


# --------------------------------------------------------------------------
# step 20 -- the inter-spike interval, and whether the cooldown swallows events


def step_20(readings, k, cap, gate, cooldowns):
    heading(f"Step 20 -- inter-spike interval with the cooldown disabled (k={k}, cap={cap})")

    episodes = reconstruct(readings, k, cap=cap, cooldown=0, gate=gate)
    by_broadcaster = {}
    for episode in episodes:
        by_broadcaster.setdefault(episode.broadcaster_id, []).append(episode)

    gaps = []
    for series in by_broadcaster.values():
        series.sort(key=lambda e: e.fired_at)
        for previous, following in zip(series, series[1:]):
            gaps.append(following.started_at - previous.fired_at)

    if not gaps:
        print("No consecutive episode pairs -- nothing for a cooldown to swallow.")
        return
    ordered = sorted(gaps)
    print(f"{len(episodes):,} episodes with no cooldown; {len(gaps):,} consecutive pairs.\n")
    print(table(
        ["quantile", "seconds from one report to the next onset"],
        [[name, f"{percentile(ordered, q):.0f}"] for name, q in
         [("min", 0.0), ("p10", 0.1), ("p25", 0.25), ("p50", 0.5), ("p75", 0.75),
          ("p90", 0.9), ("max", 1.0)]],
    ))

    # Resolved finely under 10s on purpose: that band is intensity flickering
    # across the trigger inside one chat reaction, not two events. It is the
    # thing a cooldown is really for, and it wants a different tool from the
    # thing a cooldown is usually justified by.
    buckets = [(0, 3), (3, 5), (5, 10), (10, 20), (20, 30), (30, 60),
               (60, 300), (300, 10 ** 9)]
    print("\nWhere the gaps fall:\n")
    print(table(
        ["gap (s)", "pairs", "share"],
        [[f"{low}-{high}" if high < 10 ** 9 else f"{low}+",
          f"{sum(1 for g in gaps if low <= g < high):,}",
          f"{sum(1 for g in gaps if low <= g < high) / len(gaps):.2%}"]
         for low, high in buckets],
    ))

    rows = []
    for cooldown in cooldowns:
        kept = reconstruct(readings, k, cap=cap, cooldown=cooldown, gate=gate)
        rows.append([
            str(cooldown),
            f"{len(kept):,}",
            f"{len(episodes) - len(kept):,}",
            f"{(len(episodes) - len(kept)) / len(episodes):.2%}",
        ])
    print("\nWhat each cooldown suppresses:\n")
    print(table(["cooldown (s)", "episodes", "suppressed", "share suppressed"], rows))


# --------------------------------------------------------------------------
# step 21 -- baseline contamination after a spike


def step_21(readings, k, cap, cooldown, gate, offsets):
    heading(f"Step 21 -- how long a spike's own buckets suppress the next one "
            f"(k={k}, cap={cap}, cooldown={cooldown})")

    episodes = reconstruct(readings, k, cap=cap, cooldown=cooldown, gate=gate)
    if not episodes:
        print("No episodes.")
        return

    # A second spike inside the recovery window would confound the curve, so
    # measure on episodes that have the window to themselves.
    horizon = max(offsets)
    starts = {}
    for episode in episodes:
        starts.setdefault(episode.broadcaster_id, []).append(episode.started_at)
    clean = []
    for episode in episodes:
        following = [s for s in starts[episode.broadcaster_id]
                     if episode.fired_at < s <= episode.fired_at + horizon]
        if not following and episode.start_std > 0:
            clean.append(episode)

    print(f"{len(episodes):,} episodes, {len(clean):,} of them with no second episode "
          f"inside {horizon}s ({len(clean) / len(episodes):.1%}).\n")
    print("At each offset after the report: the same burst re-scored against the\n"
          "baseline as it stands then, relative to what it scored at onset.\n"
          "`sensitivity` of 1.00 means fully recovered; 0.50 means the detector\n"
          "needs twice the burst to react.\n")

    rows = []
    for offset in offsets:
        std_ratios, mean_ratios, spread_ratios, sensitivities = [], [], [], []
        for episode in clean:
            series = readings.by_broadcaster[episode.broadcaster_id]
            i = series.index_of(episode.fired_at + offset)
            if i is None or series.status[i] != OK or series.std[i] <= 0:
                continue
            if episode.start_mean <= 0:
                continue
            burst_mean = episode.peak_count / readings.window_seconds
            reference = (burst_mean - episode.start_mean) / episode.start_std
            if reference <= 0:
                continue
            later = (burst_mean - series.mean[i]) / series.std[i]
            mean_ratios.append(series.mean[i] / episode.start_mean)
            std_ratios.append(series.std[i] / episode.start_std)
            # std/mean, against std/mean at onset. This is what tells the two
            # explanations apart. A few outlier buckets from the spike would
            # inflate the standard deviation far more than they move a
            # 300-sample mean, so this would climb. A channel that is simply
            # busier for a while scales both together and leaves it at 1.
            spread_ratios.append(
                (series.std[i] / series.mean[i]) / (episode.start_std / episode.start_mean)
                if series.mean[i] > 0 else float("nan")
            )
            sensitivities.append(later / reference)
        if not sensitivities:
            continue
        rows.append([
            str(offset),
            f"{len(sensitivities):,}",
            f"{percentile(sorted(mean_ratios), 0.5):.2f}x",
            f"{percentile(sorted(std_ratios), 0.5):.2f}x",
            f"{percentile(sorted(spread_ratios), 0.5):.2f}x",
            f"{percentile(sorted(sensitivities), 0.5):.2f}",
            f"{percentile(sorted(sensitivities), 0.1):.2f}",
        ])
    print(table(
        ["offset after report (s)", "episodes", "median baseline mean",
         "median baseline std", "median std/mean", "median sensitivity",
         "p10 sensitivity"],
        rows,
    ))
    print(f"\nA spike's last bucket leaves the baseline "
          f"{readings.baseline_seconds + readings.window_seconds}s after it occurs "
          f"(baseline_seconds + window_seconds), which is where full recovery is due.")

    # The table above gives a ratio. This code changes it into a count of the
    # episodes that the baseline removed. It divides that count by cause. Only
    # one cause is a defect, and only one cause has a possible correction.
    #
    #   whole baseline held at onset: (window_mean - start_mean) / start_std
    #     The full cost of the baseline after the spike. This is an upper
    #     bound. It is not a defect by itself. The channel is truly more busy
    #     after a large moment, and a trailing baseline must follow that.
    #
    #   spread held, level current: (window_mean - mean_now) / start_std
    #     The full cost of the larger standard deviation. This value is too
    #     high for a robust estimator. Most of the increase is the whole
    #     distribution that grows with a more busy channel. The median
    #     absolute deviation of a truly more busy window grows with it.
    #
    #   spread scaled with the level: divide by start_std x (mean_now /
    #     start_mean). That is the deviation of a channel with the same
    #     burstiness at its new level. Only the additional spread stays. That
    #     spread is the outlier buckets of the spike, and nothing else. This
    #     is thus the correct estimate of what a robust baseline can recover.
    #     A robust baseline uses the median and MAD, or removes the buckets of
    #     the previous episode.
    #
    # The code ignores the seconds inside the cooldown. Those seconds could
    # never report a spike. To count them here would charge the baseline for
    # the suppression that the cooldown caused.
    horizon_seconds = readings.baseline_seconds + readings.window_seconds
    names = ("frozen", "spread", "excess")
    counts = {name: [0, 0] for name in names}  # name -> [runs, seconds]
    for episode in clean:
        series = readings.by_broadcaster[episode.broadcaster_id]
        in_run = dict.fromkeys(names, False)
        for offset in range(cooldown + 1, horizon_seconds + 1):
            i = series.index_of(episode.fired_at + offset)
            if i is None or series.status[i] != OK or series.mean[i] <= 0:
                in_run = dict.fromkeys(names, False)
                continue
            window_mean = series.count[i] / readings.window_seconds
            missed = series.intensity[i] < k
            scaled_std = episode.start_std * (series.mean[i] / episode.start_mean)
            candidates = {
                "frozen": (window_mean - episode.start_mean) / episode.start_std,
                "spread": (window_mean - series.mean[i]) / episode.start_std,
                "excess": (window_mean - series.mean[i]) / scaled_std,
            }
            for name, value in candidates.items():
                swallowed = missed and value >= k
                if swallowed:
                    counts[name][1] += 1
                    if not in_run[name]:
                        counts[name][0] += 1
                in_run[name] = swallowed

    print(f"\nCost, in detections rather than ratios. Inside the {horizon_seconds}s after a "
          f"report (past the {cooldown}s cooldown), episodes that did not cross k={k} but "
          f"would have:\n")
    print(table(
        ["counterfactual baseline", "episodes swallowed", "seconds",
         f"vs the {len(episodes):,} reported"],
        [[label, f"{counts[name][0]:,}", f"{counts[name][1]:,}",
          f"{counts[name][0] / len(episodes):.1%}"]
         for name, label in [
             ("frozen", "whole baseline frozen at onset -- upper bound, and mostly not a defect"),
             ("spread", "level current, standard deviation frozen -- overstates a robust baseline"),
             ("excess", "level current, spread scaled with it -- what a robust baseline recovers"),
         ]],
    ))


# --------------------------------------------------------------------------
# step 22 -- min_baseline_fraction


def step_22(readings, k, cap, cooldown, reference_fraction, fractions):
    heading("Step 22 -- min_baseline_fraction, the warm-up gate")

    baseline_seconds = readings.baseline_seconds
    segments = appearances(readings)
    lengths = sorted(end - start + 1 for _, start, end in segments)
    print(f"{len(segments):,} appearances (a stretch of consecutive evaluated seconds "
          f"for one broadcaster).\n")
    print(table(
        ["quantile", "appearance length (s)"],
        [[name, f"{percentile(lengths, q):,.0f}"] for name, q in
         [("min", 0.0), ("p10", 0.1), ("p50", 0.5), ("p90", 0.9), ("max", 1.0)]],
    ))

    total_seconds = sum(len(series) for _, series in readings.rows())
    rows = []
    for fraction in fractions:
        gate = int(baseline_seconds * fraction)
        blocked = 0
        never_clears = 0
        for broadcaster_id, start, end in segments:
            series = readings.by_broadcaster[broadcaster_id]
            in_segment = sum(1 for i in range(start, end + 1) if series.observed[i] < gate)
            blocked += in_segment
            if in_segment == end - start + 1:
                never_clears += 1
        episodes = reconstruct(readings, k, cap=cap, cooldown=cooldown, gate=gate)
        peaks = sorted(e.peak_intensity for e in episodes)
        rows.append([
            f"{fraction:.2f}",
            str(gate),
            f"{blocked:,}",
            f"{blocked / total_seconds:.2%}",
            f"{never_clears:,}",
            f"{never_clears / len(segments):.1%}",
            f"{len(episodes):,}",
            f"{percentile(peaks, 1.0):.1f}" if peaks else "-",
        ])
    print(f"\nAt each fraction (k={k}, cap={cap}, cooldown={cooldown}):\n")
    print(table(
        ["fraction", "gate (s)", "seconds blocked", "share of all seconds",
         "appearances that never clear it", "share", "episodes", "max peak"],
        rows,
    ))

    # The readings a looser gate lets through are the reason the gate exists.
    # Compare against the fraction this run was given, not a fixed 0.8, or the
    # column silently describes a gate the caller did not ask about.
    print(f"\nIntensity of the readings each fraction admits that "
          f"{reference_fraction:.2f} does not:\n")
    reference_gate = int(baseline_seconds * reference_fraction)
    rows = []
    for fraction in fractions:
        gate = int(baseline_seconds * fraction)
        if gate >= reference_gate:
            continue
        extra = []
        for _, series in readings.rows():
            for i in range(len(series)):
                if series.status[i] == OK and gate <= series.observed[i] < reference_gate:
                    extra.append(series.intensity[i])
        if not extra:
            continue
        ordered = sorted(extra)
        rows.append([
            f"{fraction:.2f}",
            f"{len(extra):,}",
            f"{percentile(ordered, 0.5):.2f}",
            f"{percentile(ordered, 0.999):.2f}",
            f"{percentile(ordered, 1.0):.2f}",
        ])
    if rows:
        print(table(["fraction", "extra seconds admitted", "median", "p99.9", "max"], rows))


# --------------------------------------------------------------------------
# --verify: the reconstruction against the real detector


def verify(readings_path, corpus, k, cap, cooldown, fraction):
    """Replay `corpus` through the real detector and diff against reconstruct().

    reconstruct() is a second implementation of evaluate()'s state machine.
    Without this check every table below it is only as good as that copy.

    The bucket geometry comes from the dump named on the command line, and
    both sides are given it explicitly. replay.py reads every DETECTION_*
    variable from the environment, and measure_corpus.py reads none of them,
    so one exported DETECTION_BASELINE_SECONDS would otherwise run the two
    sides at different baselines and report a MISMATCH that is not one.
    """
    flink_job = Path(__file__).resolve().parent.parent
    geometry = load(readings_path).config
    window_seconds = geometry["window_seconds"]
    baseline_seconds = geometry["baseline_seconds"]

    dump = Path(readings_path).with_suffix(".verify.tsv")
    try:
        subprocess.run(
            [sys.executable, "tools/measure_corpus.py", "--corpus", str(corpus),
             "--out", str(dump), "--progress-every", "0",
             "--window-seconds", str(window_seconds),
             "--baseline-seconds", str(baseline_seconds)],
            cwd=flink_job, check=True, capture_output=True, text=True,
        )
        readings = load(dump)
    finally:
        dump.unlink(missing_ok=True)
    expected = reconstruct(readings, k, cap, cooldown,
                           int(readings.baseline_seconds * fraction))

    environment = {
        "DETECTION_WINDOW_SECONDS": str(window_seconds),
        "DETECTION_BASELINE_SECONDS": str(baseline_seconds),
        "DETECTION_STD_DEV_THRESHOLD": str(k),
        "DETECTION_HOLD_CAP_SECONDS": str(cap),
        "DETECTION_COOLDOWN_SECONDS": str(cooldown),
        "DETECTION_MIN_BASELINE_FRACTION": str(fraction),
    }
    result = subprocess.run(
        [sys.executable, "tools/replay.py", str(corpus)],
        cwd=flink_job, check=True, capture_output=True, text=True,
        env={**os.environ, **environment},
    )
    actual = []
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) < 3 or fields[2] != "SPIKE":
            continue
        actual.append((
            int(fields[1]),                    # broadcaster_id
            int(fields[0]),                    # fired at
            int(fields[3].split("=")[1]),      # peak_at
            float(fields[7].split("=")[1]),    # intensity
        ))
    mine = [(e.broadcaster_id, e.fired_at, e.peak_at, e.peak_intensity) for e in expected]
    mine.sort()
    actual.sort()

    print(f"real detector: {len(actual):,} spikes; reconstruction: {len(mine):,} episodes")

    # Compare the episodes and the second that each one reported. That is the
    # full output of the state machine. Compare the intensity separately, and
    # with a tolerance. The two sides round the value differently. They do not
    # calculate it differently. replay.py prints %.4f from the float. These
    # values went through the %.6f of measure_corpus.py and back. Two
    # roundings can move the last printed digit by one.
    mine_identity = [row[:3] for row in mine]
    actual_identity = [row[:3] for row in actual]
    if mine_identity != actual_identity:
        print("MISMATCH -- different episodes")
        mine_set, actual_set = set(mine_identity), set(actual_identity)
        for row in [r for r in actual_identity if r not in mine_set][:10]:
            print(f"  only the detector reported: {row}")
        for row in [r for r in mine_identity if r not in actual_set][:10]:
            print(f"  only the reconstruction reported: {row}")
        return 1

    tolerance = 1e-3
    drifted = [(a, b) for a, b in zip(mine, actual) if abs(a[3] - b[3]) > tolerance]
    if drifted:
        print(f"MISMATCH -- same episodes, intensity apart by more than {tolerance}")
        for a, b in drifted[:10]:
            print(f"  {a[:3]}: reconstruction {a[3]:.6f} vs detector {b[3]:.6f}")
        return 1

    worst = max((abs(a[3] - b[3]) for a, b in zip(mine, actual)), default=0.0)
    print("MATCH -- the reconstruction agrees with spike_detector.evaluate() on every "
          f"episode (largest intensity difference {worst:.2e}, all of it rounding)")
    return 0


# --------------------------------------------------------------------------


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("readings", help="TSV from tools/measure_corpus.py")
    parser.add_argument("--k", type=float, default=4.0)
    parser.add_argument("--cap", type=int, default=25)
    parser.add_argument("--cooldown", type=int, default=30)
    parser.add_argument("--min-baseline-fraction", type=float, default=0.8)
    parser.add_argument("--step", action="append", type=int, choices=[18, 19, 20, 21, 22],
                        help="Only these steps (repeatable). Default: all of them.")
    parser.add_argument("--verify", metavar="CORPUS",
                        help="Check reconstruct() against the real detector on this corpus")
    return parser.parse_args()


def main():
    args = parse_args()
    if args.verify:
        sys.exit(verify(args.readings, args.verify, args.k, args.cap,
                        args.cooldown, args.min_baseline_fraction))

    readings = load(args.readings)
    gate = int(readings.baseline_seconds * args.min_baseline_fraction)

    # A dump only holds readings for the seconds its own gate admitted. Asking
    # for a looser gate here cannot invent the ones it rejected, and every
    # table would quietly describe a stricter gate than the one requested.
    # Re-run measure_corpus.py with a lower --min-baseline-fraction instead.
    dump_gate = readings.config["min_observed_seconds"]
    if gate < dump_gate:
        raise SystemExit(
            f"{args.readings} was measured with a gate of {dump_gate}s "
            f"(min_baseline_fraction {readings.config['min_baseline_fraction']}), so it "
            f"holds no reading for a second under that. This run asks for {gate}s "
            f"(--min-baseline-fraction {args.min_baseline_fraction}). Re-run "
            f"tools/measure_corpus.py with a --min-baseline-fraction of "
            f"{args.min_baseline_fraction} or lower."
        )

    steps = args.step or [18, 19, 20, 21, 22]

    if 18 in steps:
        step_17_18(readings, gate, args.cap, args.cooldown,
                   candidates=[2.0, 2.5, 3.0, 3.5, 4.0, 4.5, 5.0, 6.0, 8.0])
    if 19 in steps:
        step_19(readings, args.k, args.cooldown, gate, caps=[5, 10, 15, 20, 25, 30, 45, 60])
    if 20 in steps:
        step_20(readings, args.k, args.cap, gate, cooldowns=[0, 10, 20, 30, 60, 120])
    if 21 in steps:
        step_21(readings, args.k, args.cap, args.cooldown, gate,
                offsets=[0, 5, 10, 15, 20, 30, 45, 60, 90, 120, 180, 240, 300, 330])
    if 22 in steps:
        step_22(readings, args.k, args.cap, args.cooldown, args.min_baseline_fraction,
                fractions=[0.0, 0.1, 0.25, 0.5, 0.65, 0.8, 0.9, 1.0])


if __name__ == "__main__":
    main()
