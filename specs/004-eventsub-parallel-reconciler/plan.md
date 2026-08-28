# Implementation Plan: EventSub Ingestion with a Parallel Reconciler

**Branch**: `004-eventsub-parallel-reconciler` | **Date**: 2026-08-27 | **Spec**: [spec.md](./spec.md)

## Summary

Split the stream-monitoring service into a **poller** (decides what should be
watched, never blocks) and a **reconciler** (makes it so, in parallel, out of
band). Swap the transport underneath from IRC to EventSub websockets.

## Technical Context

**Language/Version**: Python 3.10/3.11, twitchAPI 4.5.0
**Primary Dependencies**: `twitchAPI.eventsub.websocket.EventSubWebsocket`, Redis, confluent-kafka
**Storage**: Redis (desired set, refusal cache), Postgres (streamers)
**Testing**: pytest — `test_stream_monitoring.py`
**Constraints**: 300 subscriptions per websocket connection (measured)
**Scale/Scope**: 500 channels near-term, no hard ceiling by design

## Constitution Check

| Principle | Status |
|---|---|
| Kafka for inter-service messaging | Unchanged — same topics, same schema |
| Postgres exclusively for persistence | Unchanged |
| PyFlink for stream processing | Unchanged — but see D3, event-time semantics |
| Prometheus metrics | Extended (FR-012) |
| **No data loss in the pipeline** | **At risk — see R1 and D3** |
| Filter bot commands | Unchanged (Flink-side) |
| Python virtual environments | `services/stream-monitoring/.venv` |

## Architecture

```
                 ┌──────────────┐
   Helix         │   Poller     │   every POLL_INTERVAL_SECONDS
   get_streams ─▶│              │   - rank, filter clip-disabled
                 │  (fast, no   │   - write desired set + rank
                 │   network    │   - refresh Redis online keys
                 │   fan-out)   │
                 └──────┬───────┘
                        │ desired set (Redis sorted set, by rank)
                        ▼
                 ┌──────────────┐
                 │ Reconciler   │   long-lived, independent of poll ticks
                 │              │   - diff desired vs actual
                 │ bounded      │   - create/drop, highest rank first
                 │ concurrency  │   - route to a connection with room
                 └──────┬───────┘
                        │
                        ▼
              ┌──────────────────────┐
              │ EventSub WS pool     │  N sockets x 300 subs
              │  on_message ─────────┼──▶ Kafka `chat-messages`
              └──────────────────────┘
```

The seam matters more than the transport. The poller writes intent; the
reconciler owns all network fan-out. That is what makes FR-003 achievable and
it is why the same design would have helped on IRC.

## Design decisions to make (these are the real work)

### D1 — Transport: websocket or webhook

**Recommend websocket.** No public ingress, no TLS cert, no challenge
handshake, and the library handles reconnect. Cost: ~300 subs per connection
and subscriptions die with the socket.

Webhook has a higher ceiling and server-side persistence across restarts, but
needs a public HTTPS endpoint. Revisit only if the connection pool becomes
unwieldy — that is a long way past 500.

### D2 — Reconciler concurrency

Measured: sequential is 2.1 subs/s, p50 421 ms per POST, **zero 429s** across
394 creations. So the limit is latency, not rate — but the safe concurrency is
**unmeasured**. Start at 10 (projecting ~20/s, 500 channels in ~25 s), watch
for 429s, and tune. Do not assume the projection; it is arithmetic, not data.

### D3 — `sent_at` semantics ⚠️ the subtle one

`SentAtTimestampAssigner` drives Flink's event time from `sent_at`, which today
is IRC's `tmi-sent-ts` — when Twitch *received* the message.

**`ChannelChatMessageData` has no timestamp field at all.** The only per-message
time is `metadata.message_timestamp` on the EventSub envelope, which is when
Twitch *dispatched* the event. These are different quantities.

They should track closely, but "should" is not evidence. This must be measured
against the IRC stream before cutover, because a silent shift in event time
would corrupt anomaly detection without any error surfacing.

### D4 — Watermark tolerance

`WATERMARK_OUT_OF_ORDERNESS_SECONDS = 1` (commit `4ce10e0`). Measured EventSub
lag at 394 channels: p50 154 ms, p95 220 ms, **max 1243 ms** — the tail already
breaches 1 s, and the tail grew with fan-out (max was 366 ms at 2 channels).

Choose deliberately: raise the tolerance and accept later detection, or keep it
and accept a measured drop rate. **Do not leave this to inertia.**

### D5 — Refusal cache

~1.5% of channels refuse with `subscription missing proper authorization`.
Store alongside the existing `allows_clipping` flag in Postgres, so refusals are
skipped rather than retried every cycle. Consider a re-check interval — a
refusal may not be permanent.

### D6 — Connection pool routing

Route by consistent hash so a channel lands on the same connection across
reconciles. On socket death, only that connection's subscriptions need
recreating. Track per-connection occupancy against the 300 cap.

## Risks

| ID | Risk | Mitigation |
|---|---|---|
| R1 | Events dropped during ramp — the library logs `received event for unknown subscription` | Warm-up period before trusting a channel's baseline; quantify the loss |
| R2 | D3 shifts event time silently and corrupts detection | Measure both timestamps side by side before cutover |
| R3 | Concurrency triggers 429s not seen sequentially | Start low, back off on 429, make it configurable |
| R4 | Socket death drops 300 channels at once | Hash routing plus fast reconcile; alert on subscription count drop |
| R5 | Removing IRC leaves no fallback if EventSub misbehaves in production | Deliberate — the operator accepted no intermediate compatibility. `git revert` is the fallback |

## Project Structure

```
services/stream-monitoring/
├── stream_monitoring_service.py   # poller; loses all chat/join logic
├── reconciler.py                  # NEW — desired vs actual, parallel
├── eventsub_pool.py               # NEW — connection pool, hash routing
├── token_manager.py               # +user:read:chat scope
└── test_stream_monitoring.py
```

**Structure Decision**: New modules rather than growing
`stream_monitoring_service.py`, which is already ~660 lines and carries most of
the service's hard-won edge-case comments. The poller keeps its Helix, Redis,
Postgres, and lifecycle-event logic; it loses `_manage_chat_connections`,
`joined_channels`, the dead-chat detection, and the `patches/` workaround —
all of which exist only because of IRC's connection-oriented membership model.

## Token scope change

The current token has `chat:read` and `clips:edit`. EventSub needs
`user:read:chat`. A superset token already exists from the spike (see
003 `research.md`). Re-seeding does **not** invalidate the existing token —
that was verified during the spike, with production running throughout.
