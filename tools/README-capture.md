# Corpus capture runbook

Feeds Plan 06. Read `plans/06-detection-math.md` Phase 1 for why this exists.

`chat-messages` has **1-hour retention**. This captures forward only — there is no going back for
data you did not capture. Start it before you need it.

## Step 1 — deploy `sent_at` FIRST

The capture is worthless without it, and the script will refuse to run.

In `services/stream-monitoring/stream_monitoring_service.py` (~line 417), **add** the field. Do not
replace `timestamp`:

```python
"timestamp": int(time.time() * 1000),   # ingestion clock, unchanged
"sent_at":   msg.sent_timestamp,        # Twitch server clock, from tmi-sent-ts
```

```bash
docker compose restart stream-monitoring      # bind-mounted; no rebuild needed
docker logs --tail 20 streamscout-stream-monitoring
```

## Step 2 — start the capture

```bash
mkdir -p ~/stream-scout-corpus

docker run -d --name streamscout-corpus-capture \
  --network stream-scout_streamscout \
  -v ~/stream-scout/tools:/tools:ro \
  -v ~/stream-scout-corpus:/corpus \
  stream-scout-stream-monitoring \
  python3 /tools/capture_corpus.py \
    --output /corpus/chat-corpus.jsonl \
    --hours 12
```

Detached, so it survives closing the terminal. It reuses the stream-monitoring image purely because
that image already has `confluent-kafka` installed.

**Confirm it cleared preflight before walking away:**

```bash
docker logs streamscout-corpus-capture
```

You want `sent_at present; ingestion lag on this sample: N ms`. If you instead see a FATAL about
`sent_at`, step 1 did not deploy — fix it and restart the container. Failing here is the whole point;
it costs a minute now instead of twelve hours later.

## Step 3 — check on it occasionally

```bash
docker logs --tail 5 streamscout-corpus-capture     # progress every 5 min
ls -lh ~/stream-scout-corpus/
```

Expect roughly 1.3–2.5M messages and 0.4–1 GB over 12 hours, depending on time of day.

## Step 4 — when it finishes

```bash
cat ~/stream-scout-corpus/chat-corpus.jsonl.meta.json
docker rm streamscout-corpus-capture
```

Then cut the dev slice for the fast edit-run-look loop (Plan 06 Phase 1 step 7):

```bash
head -n 200000 ~/stream-scout-corpus/chat-corpus.jsonl > ~/stream-scout-corpus/dev-slice.jsonl
```

Pick a slice that actually contains spikes — check against `clips` rows whose `detected_at` falls in
the captured window.

## Capturing alongside live clipping, to explain clips later

The capture above was cut for Plan 06 tuning, when nothing was clipping yet. Running it *while*
the job clips gives something else: for any clip made during the window, the chat that produced it
is on disk, so `tools/clip_anatomy.py` can redraw the moment second by second.

Use a dated output file. The script appends, and mixing a pre-clipping corpus into a new one makes
the replay meaningless:

```bash
docker run -d --name streamscout-corpus-capture \
  --network stream-scout_streamscout \
  -v ~/stream-scout/tools:/tools:ro \
  -v ~/stream-scout-corpus:/corpus \
  stream-scout-stream-monitoring \
  python3 /tools/capture_corpus.py \
    --output /corpus/chat-corpus-$(date +%F).jsonl \
    --hours 14
```

### In the morning: draw a clip that looks wrong

Pick a clip from the feed, then:

```bash
python3 tools/clip_anatomy.py \
  --clip-id <clip_id> \
  --corpus ~/stream-scout-corpus/chat-corpus-<date>.jsonl \
  --out /tmp/anatomy.html
```

It prints a one-line verdict and writes a page showing chat volume, intensity, the baseline, and
four markers: hold opens, peak, emit, clip requested. The shaded band is the 30 seconds the clip
actually contains, so a clip that missed its moment shows the band sitting clear of the peak.

`--broadcaster <id> --peak <unix_second>` replaces `--clip-id` for a spike that never became a clip.
`--json <path>` dumps the reconstructed numbers for checking without the drawing code.

Three limits worth knowing before trusting a page:

- **The corpus must cover the clip, with lead.** The baseline is 300 seconds, so a clip needs
  roughly seven minutes of capture before it. Clips from before the capture started cannot be
  drawn at all — the tool says so rather than guessing.
- **The request time is modelled**, as emit + watermark + clip delay. The true request time exists
  only in the taskmanager log, which rotates. Everything else — the curve, the baseline, the hold,
  the emit second — comes from `spike_detector.evaluate()` itself.
- **Replayed intensity can differ slightly from the stored value** (6.02 against a stored 5.9 in
  the first live check). The capture and the job consume the same topic independently, so their
  per-second counts are not guaranteed identical. Treat a small gap as normal and a large one as
  worth investigating.

## Safety notes

- The consumer uses a **random group id** and never commits offsets. It cannot disturb the Flink job
  or any other consumer.
- Output **appends**. A restart extends the corpus rather than truncating it.
- It flushes and `fsync`s every 2000 messages or 30 seconds, so a crash at hour 9 keeps the first 8.
- `SIGTERM` (i.e. `docker stop`) finishes cleanly and still writes the metadata sidecar.
- **Do not restart `stream-monitoring` or Kafka while this runs.** Anything you miss is unrecoverable.
- Do not write the corpus inside the repo — it is up to 1 GB. `~/stream-scout-corpus/` is outside it.
