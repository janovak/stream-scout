# Implementation Plan: EventSub Ingestion with a Parallel Reconciler

**Branch**: `004-eventsub-parallel-reconciler` | **Date**: 2026-08-27 | **Spec**: [spec.md](./spec.md)
**Input**: Feature specification from `/specs/004-eventsub-parallel-reconciler/spec.md`

## Summary

Chat ingestion runs over IRC today. The Twitch JOIN limit is 20 per 10 seconds
per account, and it blocks rather than fails. That limit caps how many channels
the system can watch, and no extra connection or machine moves it.

This feature makes two changes that only deliver value together:

1. **Transport**: replace IRC with EventSub `channel.chat.message` websockets.
   This removes the ceiling. Subscriptions cost 0 against the budget.
2. **Structure**: move subscription management out of the poll job into a
   long-lived parallel reconciler. The poller writes intent. The reconciler
   owns all network fan-out and creates subscriptions concurrently.

The transport swap alone buys almost nothing at cold start: measured sequential
EventSub creation is 2.1/s, about the same as IRC's 2/s. The win needs parallel
creation *and* the move out of the poll tick.

IRC is removed outright. There is no dual-transport period. The operator does
not need a working system at each intermediate step.

## Technical Context

**Language/Version**: Python 3.11 (`services/stream-monitoring`); Python 3.10 (PyFlink job, unchanged)
**Primary Dependencies**: `twitchAPI` 4.5.0 (`twitchAPI.eventsub.websocket.EventSubWebsocket`), `redis`, `confluent-kafka`, `apscheduler`, `psycopg2`
**Storage**: Redis (desired set and its login-to-id map); Postgres (`streamers` table — adds refusal and re-check columns); Flink keyed state (unchanged). Connection routing is computed in memory, not stored
**Testing**: pytest — `services/stream-monitoring/test_stream_monitoring.py`; Flink side `test_replay.py`, `test_spike_detector.py`
**Target Platform**: Docker Compose stack; one `stream-monitoring` container; Flink standalone cluster
**Project Type**: single (backend service plus stream-processing job)
**Process model**: the reconciler runs as an `asyncio` task inside the existing `stream-monitoring` process, next to the APScheduler poll job — not a separate container. It shares that process's `/health` endpoint and its `jsonlogger` path (constitution: health endpoints, centralized logging)
**Reconcile cadence**: a continuous loop. Each pass diffs `chat:desired` against the actual set and acts. It wakes on a `chat:desired:generation` bump (poll wrote a new desired set) or after a short idle interval (default 5 s), whichever comes first. One pass never overlaps the next
**Performance Goals**: cold start to 500 channels under 60 s (target), 120 s (hard ceiling); poll duration flat against desired-set change size
**Constraints**: 300 subscriptions per websocket connection (measured); `WATERMARK_OUT_OF_ORDERNESS_SECONDS` moves 1 → 2; `chat-messages` Kafka schema unchanged (FR-008)
**Scale/Scope**: 15/30 channels today; 500 near-term; no hard ceiling by design

## Constitution Check

*GATE: must pass before Phase 0 research. Re-checked after Phase 1 design.*

| Principle | Status |
|---|---|
| Kafka for all inter-service messaging | Unchanged — same `chat-messages` and `stream-lifecycle` topics, same schema |
| Postgres exclusively for persistence | Unchanged — refusal state is two new columns on `streamers`, not a new store |
| PyFlink for stream processing | Unchanged — the job keeps its code; only `WATERMARK_OUT_OF_ORDERNESS_SECONDS` changes |
| Twitch API integration | Unchanged in principle — transport moves from IRC to EventSub, still one Twitch account |
| Prometheus metrics | Extended — subscription count, reconcile duration, creation failures, per-connection occupancy, and the time of the last successful reconcile (FR-012) |
| Grafana / Loki observability | Unchanged — same structured-log path |
| **No data loss in the pipeline** | **At risk** — see research R1 (ramp-window drops) and R2 (event-time shift). Both are measured in Phase 0 before cutover |
| Health check endpoints | Unchanged — `/health` on 8080 stays |
| Filter bot commands | Unchanged — `CommandFilter` is Flink-side and untouched |
| Python virtual environments | `services/stream-monitoring/.venv` |

No principle is violated by design. The two "at risk" items are gated by Phase 0
measurement, not accepted blind. If Phase 0 shows either would corrupt
detection, cutover does not proceed on this design (research R2, decision D3).

## Project Structure

### Documentation (this feature)

```text
specs/004-eventsub-parallel-reconciler/
├── plan.md              # This file
├── research.md          # Phase 0 — decisions D1–D6, risks R1–R5, measurement tasks
├── data-model.md        # Phase 1 — desired set, skip records, connection routing
├── contracts/
│   └── chat-messages.schema.md   # the Kafka schema that MUST NOT change (FR-008)
└── tasks.md             # Phase 2 — /speckit.tasks output
```

### Source Code (repository root)

```text
services/stream-monitoring/
├── stream_monitoring_service.py   # poller — loses all chat/join logic; keeps Helix, Redis, Postgres, lifecycle
├── reconciler.py                  # NEW — diff desired vs actual, bounded concurrency, highest rank first
├── eventsub_pool.py               # NEW — EventSubWebsocket pool, rendezvous-hash routing, 300-cap occupancy
├── token_manager.py               # +user:read:chat scope
├── test_stream_monitoring.py      # extended — poll-does-not-block, hysteresis-survives, reconciler convergence
├── Dockerfile                     # +COPY reconciler.py eventsub_pool.py; drop the patches/ step
└── patches/twitchapi_leave_room_timeout.py   # DELETED in Phase 3

services/flink-job/
└── spike_detector.py              # WATERMARK_OUT_OF_ORDERNESS_SECONDS: 1 → 2

infrastructure/postgres/
└── init.sql                       # +eventsub_refused_at, +clipping_disabled_at on streamers

docker-compose.yml                 # +bind-mounts for reconciler.py, eventsub_pool.py; +user:read:chat note
```

**Structure Decision**: New modules, not growth of `stream_monitoring_service.py`.
That file is ~700 lines and carries most of the service's hard-won edge-case
comments. The poller keeps its Helix, Redis, Postgres, and lifecycle-event
logic. It loses `_manage_chat_connections`, `joined_channels`, `_on_chat_ready`,
`_on_chat_message`, the dead-chat detection, and `patches/` — all of which exist
only because of IRC's connection-oriented membership model. `resolve_thresholds`
and the hysteresis band it validates stay; the band now feeds the desired-set
computation instead of the join loop.

### Deployment wiring (do not skip)

`stream-monitoring` mounts each source file by name in `docker-compose.yml` and
copies each by name in the `Dockerfile`. A new module is invisible until both
are updated. This is the same class of gap that bit the Flink job's `-pyFiles`
list in earlier specs. `reconciler.py` and `eventsub_pool.py` each need a
`COPY` line and a bind-mount entry.

## Phase Overview

| Phase | Purpose | Gate |
|---|---|---|
| 0 — Measure | Run IRC and EventSub side by side. Decide D2, D3, D4, and the R1 warm-up question from data | Blocks Phase 2 |
| 1 — The seam | Split poller from reconciler. Transport-independent. Fixes cold start on its own | Poller fast; reconciler owns network work |
| 2 — EventSub transport | Token scope, connection pool, message mapping, wire the reconciler to the pool | EventSub carries live traffic |
| 3 — Remove IRC | Delete the `Chat` client, dead-chat detection, and the `patches/` workaround. No dual-transport period | Service is simpler, not just changed |
| 4 — Watermark and detection | Set `WATERMARK_OUT_OF_ORDERNESS_SECONDS = 2`. Confirm the detector behaves unchanged. Add a warm-up gate only if T004 requires it | Late-drop rate < 0.1% |
| 5 — Verify | The seven success criteria | All SC pass |
| 6 — Review | `/speckit.analyze`, `/code-review high`, address findings | Clean |

## Complexity Tracking

No constitution violation needs justification. The one deliberate simplification
— removing IRC with no compatibility flag — reduces complexity rather than
adding it, and the operator has accepted the trade (research R5).
