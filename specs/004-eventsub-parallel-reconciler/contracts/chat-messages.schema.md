# Contract: `chat-messages` Kafka topic

This is the schema the poller publishes today over IRC. FR-008 requires it to be
**byte-compatible** after the EventSub cutover. The Flink job
(`clip_detector_job.py`) is the consumer and must not need a change.

## Message — JSON, one per chat message

| Field | Type | Source today (IRC) | Source after cutover (EventSub) |
|---|---|---|---|
| `broadcaster_id` | int | `self.broadcaster_ids[login]` | `event.broadcaster_user_id` (int) |
| `timestamp` | int (epoch ms) | `int(time.time() * 1000)` — ingestion clock | unchanged — ingestion clock at receipt |
| `sent_at` | int (epoch ms) | `msg.sent_timestamp` from IRC `tmi-sent-ts` | **envelope `metadata.message_timestamp`**, RFC 3339 string parsed to epoch ms (D3) |
| `message_id` | str | `str(uuid.uuid4())` | `event.message_id` if present, else a generated UUID |
| `text` | str | `msg.text` | `event.message.text` |
| `user_id` | int | `int(msg.user.id)` or `0` | `int(event.chatter_user_id)` or `0` |
| `user_name` | str | `msg.user.name` | `event.chatter_user_login` |
| `metadata` | object | see below | see below |

### `metadata` object

| Field | Type | Source today | Source after cutover |
|---|---|---|---|
| `emotes` | object | `{}` (always empty today) | `{}` — keep empty; do not start populating it in this feature |
| `badges` | object | `dict(msg.user.badges)` | derived from `event.badges` (list of `{set_id, id}`) → `{set_id: id}` |
| `is_subscriber` | bool | `msg.user.subscriber` | `any(b.set_id == "subscriber" for b in event.badges)` |
| `is_mod` | bool | `msg.user.mod` | `any(b.set_id == "moderator" for b in event.badges)` |

## What the consumer actually reads

The Flink job's hot path reads only:

- `broadcaster_id` — the Kafka partition key and the keyed-stream key
- `sent_at` — `SentAtTimestampAssigner` uses it for event time; **must be an
  int or `null`**, never a string, or the assigner falls back to record time
- `text` — `CommandFilter` uses it to drop bot commands

`user_id` and `user_name` travel into `AnomalyEvent` for logging. The remaining
fields are carried for downstream and debugging. Preserve all of them anyway —
FR-008 is about the schema, not just the three hot fields.

## Invariants

1. `sent_at` is epoch **milliseconds**, matching `ctx.timestamp() // 1000`
   bucketing in `AnomalyDetector.process_element`. The EventSub timestamp is an
   RFC 3339 string; the mapper converts it.
2. `sent_at` is present-and-int or present-and-`null`. Never absent, never a
   string.
3. Key = `str(broadcaster_id).encode("utf-8")`, unchanged, so partition
   assignment does not move.
4. A test asserts a mapped EventSub message and a mapped IRC message produce the
   same JSON keys (FR-008, task in Phase 2).
