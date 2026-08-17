#!/usr/bin/env python3
"""Draw the second-by-second anatomy of one clip, replayed from a chat corpus.

The morning workflow this exists for: skim the clip feed, find one that looks
like it missed its moment, and ask *why*. A clip row on its own cannot answer
that -- it records the peak and the intensity, but not the shape of the chat
burst that produced them, and not how much of that burst the clip actually
contains. This replays the captured chat through the real detector and draws
both.

    python3 tools/clip_anatomy.py \\
        --clip-id GiftedSuavePotMau5-qcRfvqeLe1FFnIUg \\
        --corpus ~/stream-scout-corpus/chat-corpus-2026-08-17.jsonl \\
        --out /tmp/anatomy.html

The corpus must cover the clip. `chat-messages` has 1-hour retention and the
capture only runs forward, so a clip from before the capture started cannot be
drawn -- see tools/README-capture.md.

What it reconstructs, and what it does not:

  * The intensity curve, the baseline, the hold and the emit second come from
    `spike_detector.evaluate()` itself -- the same function the Flink job calls,
    driven off the same per-second counts. These are not modelled.
  * The clip request time IS modelled, as emit + WATERMARK_OUT_OF_ORDERNESS +
    CLIP_INITIAL_DELAY_SECONDS. The true request time only exists in the
    taskmanager log, which rotates. The model matched the log on every spike it
    was checked against, but it is a model, and the page says so.

Twitch captures the 30 seconds *before* the request, so "did the clip contain
its own peak" reduces to whether peak -> request stayed under 30 seconds.
"""

import argparse
import json
import os
import sys
from collections import Counter
from datetime import datetime, timezone

# The detector and its config live with the Flink job. Import them rather than
# restating the arithmetic here -- a second copy would drift, and the whole
# point is to show what the real detector did.
_JOB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "..", "services", "flink-job")
sys.path.insert(0, os.path.abspath(_JOB_DIR))

from spike_detector import DetectorConfig, evaluate  # noqa: E402
from clip_attempt import ClipPolicy  # noqa: E402

try:
    from clip_detector_job import WATERMARK_OUT_OF_ORDERNESS_SECONDS  # noqa: E402
except Exception:
    # clip_detector_job imports pyflink, which is not installed outside the job
    # image. The constant is the only thing needed here, so fall back to it.
    WATERMARK_OUT_OF_ORDERNESS_SECONDS = 5

# How much history to feed the detector before the peak. It must exceed
# baseline_seconds, or the replay opens with a cold baseline and reports
# "warmup" for the seconds that matter.
_LEAD_SECONDS = 420
_TRAIL_SECONDS = 60
# What the drawn window shows, either side of the peak.
_PLOT_BEFORE, _PLOT_AFTER = 75, 35


def _clip_from_db(clip_id):
    """Look up one clip. Returns (broadcaster_id, peak_second, intensity, meta)."""
    import psycopg2

    url = os.getenv("POSTGRES_URL")
    if url:
        conn = psycopg2.connect(url)
    else:
        conn = psycopg2.connect(
            host=os.getenv("POSTGRES_HOST", "100.112.97.111"),
            port=int(os.getenv("POSTGRES_PORT", "5432")),
            dbname=os.getenv("POSTGRES_DB", "twitch"),
            user=os.getenv("POSTGRES_USER", "twitch"),
            password=os.getenv("POSTGRES_PASSWORD", "twitch_password"),
        )
    with conn, conn.cursor() as cur:
        cur.execute(
            "SELECT c.broadcaster_id, c.detected_at, c.intensity, c.duration,"
            "       c.vod_offset, s.streamer_login "
            "FROM clips c LEFT JOIN streamers s ON s.streamer_id = c.broadcaster_id "
            "WHERE c.clip_id = %s",
            (clip_id,),
        )
        row = cur.fetchone()
    conn.close()
    if row is None:
        sys.exit(f"clip_id {clip_id!r} is not in the clips table")
    bid, detected_at, intensity, duration, vod_offset, login = row
    # detected_at is the second of the peak (Plan 06 Phase 3), stored naive UTC.
    peak = int(detected_at.replace(tzinfo=timezone.utc).timestamp())
    return bid, peak, intensity, {
        "clip_id": clip_id, "login": login or str(bid),
        "duration": duration, "vod_offset": vod_offset,
    }


def _counts(corpus_path, broadcaster_id, lo, hi):
    """Per-second message counts for one broadcaster over [lo, hi].

    Corpora run to gigabytes, so the broadcaster id is matched as a substring
    before paying for json.loads. The substring can collide (one id inside
    another, or inside a message body), so the decoded record is still checked.
    """
    needle = str(broadcaster_id).encode()
    counts = Counter()
    texts = []
    with open(corpus_path, "rb") as fh:
        for raw in fh:
            if needle not in raw:
                continue
            try:
                rec = json.loads(raw)
            except ValueError:
                continue
            if rec.get("broadcaster_id") != broadcaster_id:
                continue
            sent_at = rec.get("sent_at")
            if sent_at is None:
                continue
            second = sent_at // 1000
            if lo <= second <= hi:
                counts[second] += 1
                texts.append((second, rec.get("text", "")))
    return counts, texts


def _replay(counts, peak, config):
    """Drive the real detector across the window. Returns (series, emits)."""
    series, emits = {}, []
    hold, last_fire = None, None
    if not counts:
        return series, emits
    for second in range(min(counts), peak + _TRAIL_SECONDS + 1):
        live = {t: c for t, c in counts.items()
                if second - config.retained_seconds <= t <= second}
        decision = evaluate(live, second, hold, last_fire, config)
        hold = decision.hold
        if decision.emit is not None:
            last_fire = second
            emits.append((second, decision.emit))
        offset = second - peak
        if -_PLOT_BEFORE <= offset <= _PLOT_AFTER:
            measurement = decision.measurement
            series[offset] = {
                "o": offset,
                "w": 0 if measurement is None else measurement.message_count,
                "i": None if measurement is None else round(measurement.intensity, 2),
                "held": hold is not None,
            }
    return series, emits


def _fmt(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def build(args):
    if args.clip_id:
        bid, peak, db_intensity, meta = _clip_from_db(args.clip_id)
    else:
        bid, peak, db_intensity = args.broadcaster, args.peak, None
        meta = {"clip_id": None, "login": str(args.broadcaster),
                "duration": None, "vod_offset": None}

    config = DetectorConfig.from_env()
    counts, texts = _counts(args.corpus, bid, peak - _LEAD_SECONDS, peak + _TRAIL_SECONDS)
    if not counts:
        sys.exit(
            f"no chat for broadcaster {bid} near {_fmt(peak)} in {args.corpus}.\n"
            "The corpus probably does not cover this clip -- capture only runs forward."
        )
    series, emits = _replay(counts, peak, config)

    # The emit that carries this peak. The replay can produce neighbours; match
    # on the peak second the spike reports, not on proximity.
    emit_second, spike = None, None
    for second, candidate in emits:
        if candidate.detected_at_seconds == peak:
            emit_second, spike = second, candidate
            break
    if spike is None and emits:
        emit_second, spike = min(emits, key=lambda e: abs(e[1].detected_at_seconds - peak))

    # The clip delay belongs to ClipPolicy, not the detector -- read it from the
    # same place the job does rather than re-deriving it from the environment.
    policy = ClipPolicy.from_env()
    request_offset = None
    if emit_second is not None:
        # Modelled, not measured -- see the module docstring.
        request_offset = ((emit_second - peak)
                          + WATERMARK_OUT_OF_ORDERNESS_SECONDS
                          + policy.initial_delay_seconds)

    hold_open = next((o for o in sorted(series)
                      if series[o]["held"] and o <= (emit_second - peak if emit_second else 0)), None)

    payload = {
        "broadcaster_id": bid,
        "login": meta["login"],
        "clip_id": meta["clip_id"],
        "peak_second": peak,
        "peak_time": _fmt(peak),
        "config": {"k": config.k, "window_seconds": config.window_seconds,
                   "baseline_seconds": config.baseline_seconds,
                   "hold_cap_seconds": config.hold_cap_seconds,
                   "cooldown_seconds": config.cooldown_seconds,
                   "watermark_seconds": WATERMARK_OUT_OF_ORDERNESS_SECONDS},
        "series": [series[o] for o in sorted(series)],
        "peak_intensity": round(spike.intensity, 2) if spike else None,
        "peak_count": spike.message_count if spike else None,
        "baseline_mean": round(spike.baseline_mean, 2) if spike else None,
        "db_intensity": round(db_intensity, 2) if db_intensity is not None else None,
        "hold_open_offset": hold_open,
        "emit_offset": (emit_second - peak) if emit_second is not None else None,
        "request_offset": request_offset,
        "vod_offset": meta["vod_offset"],
        "duration": meta["duration"],
        # A poll (chat typing a/b/c) makes a large, genuine spike that is not a
        # moment. Surfacing the short-message share costs one pass and answers
        # "is this even worth clipping" before the graph is read.
        "short_share": round(
            sum(1 for s, t in texts if peak - config.window_seconds < s <= peak
                and len(t.strip()) <= 2)
            / max(1, sum(1 for s, _ in texts if peak - config.window_seconds < s <= peak)), 3),
    }

    if args.json:
        with open(args.json, "w") as fh:
            json.dump(payload, fh, indent=2)
        print(f"wrote {args.json}")

    from clip_anatomy_render import render  # local module, kept beside this one
    html = render(payload)
    with open(args.out, "w") as fh:
        fh.write(html)
    print(f"wrote {args.out}")
    print(f"  {meta['login']}  peak {payload['peak_time']}  intensity {payload['peak_intensity']}")
    if request_offset is not None:
        verdict = ("peak captured" if request_offset <= 25 else
                   "on the edge" if request_offset <= 30 else "PEAK MISSED")
        print(f"  peak -> request {request_offset}s ({verdict})")
    if payload["short_share"] >= 0.6:
        print(f"  warning: {payload['short_share']:.0%} of the peak window is 1-2 char "
              "messages -- this looks like a poll, not a moment")


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--clip-id", help="clip_id from the clips table")
    src.add_argument("--broadcaster", type=int, help="broadcaster id (with --peak)")
    p.add_argument("--peak", type=int, help="peak second, unix epoch (with --broadcaster)")
    p.add_argument("--corpus", required=True, help="captured chat JSONL covering the clip")
    p.add_argument("--out", default="anatomy.html", help="HTML output path")
    p.add_argument("--json", help="also dump the reconstructed data here")
    args = p.parse_args()
    if args.broadcaster and args.peak is None:
        p.error("--broadcaster requires --peak")
    build(args)


if __name__ == "__main__":
    main()
