# Data Model: EventSub Ingestion with a Parallel Reconciler

No new store. Redis and Postgres both already exist in the service. This feature
adds a Redis key layout for the desired set and two columns to one Postgres
table.

## Postgres — `streamers` table changes

Current columns: `streamer_id` (PK), `streamer_login`, `allows_clipping`,
`first_seen_at`, `last_seen_at`.

| New column | Type | Default | Meaning |
|---|---|---|---|
| `eventsub_refused_at` | `TIMESTAMPTZ` | `NULL` | Time the channel last returned `subscription missing proper authorization`. `NULL` means never refused |
| `clipping_disabled_at` | `TIMESTAMPTZ` | `NULL` | Time `allows_clipping` was last set to `FALSE`. `NULL` while `allows_clipping` is `TRUE` |

### Skip and re-check rule (FR-007, FR-013)

A streamer is **skipped** by the reconciler when `eventsub_refused_at IS NOT
NULL`. A streamer is **dropped from ranking** by the poller when
`allows_clipping = FALSE` (unchanged).

A mark older than 7 days is **stale**. A stale mark is retried once:

- `eventsub_refused_at < NOW() - INTERVAL '7 days'` → the reconciler attempts
  the subscription again. Success clears the column to `NULL`. A fresh refusal
  sets it to `NOW()`.
- `clipping_disabled_at < NOW() - INTERVAL '7 days'` → the poller lets the
  streamer back into ranking. The next real clip attempt in the Flink job
  either succeeds (sets `allows_clipping = TRUE`, `clipping_disabled_at =
  NULL`) or refuses again (`clipping_disabled_at = NOW()`).

The boolean `allows_clipping` stays as the fast filter. `clipping_disabled_at`
is written alongside every flip to `FALSE` so the two never disagree.

### Migration

`infrastructure/postgres/init.sql` runs only on a fresh database. The deployed
database needs a manual `ALTER TABLE streamers ADD COLUMN ...` for both columns.
This is the same manual step earlier specs needed; the task list calls it out.

Run it in one transaction so a half-apply cannot happen — Postgres DDL is
transactional:

```sql
BEGIN;
ALTER TABLE streamers ADD COLUMN eventsub_refused_at TIMESTAMPTZ;
ALTER TABLE streamers ADD COLUMN clipping_disabled_at TIMESTAMPTZ;
-- backfill so existing disabled rows are not retried immediately as "stale":
UPDATE streamers SET clipping_disabled_at = NOW() WHERE allows_clipping = FALSE;
COMMIT;
```

## Redis — desired set

`services/stream-monitoring/desired_set_store.py` owns this representation.
The poller and reconciler depend on its `DesiredSetStore` interface rather
than importing keys or repeating decoding and transaction rules.

| Key | Type | Written by | Read by | TTL |
|---|---|---|---|---|
| `chat:desired` | Sorted set: member = broadcaster login, score = rank (1 = top) | Poller, once per poll | Reconciler | none — overwritten each poll |
| `chat:desired:ids` | Hash: broadcaster login → broadcaster id | Poller, once per poll | Reconciler | none — overwritten each poll |
| `chat:desired:generation` | String: monotonic counter, bumped each poll | Poller | Reconciler (detect a stale in-flight reconcile) | none |
| `streamer:online:{login}` | String: broadcaster id (existing key, unchanged) | Poller | Poller | `REDIS_STREAMER_TTL` = 180 s |

The sorted set lets the reconciler work highest-rank-first with `ZRANGE
chat:desired 0 -1 WITHSCORES`.

`chat:desired:ids` was added in Phase 1. The poller ranks by login, but
EventSub subscribes by broadcaster id, so the map has to cross the seam. It is
written in the same transaction as `chat:desired`, so the two can never
disagree. The alternative — having the reconciler read each
`streamer:online:{login}` key — was rejected: those keys expire at 180 s, so a
poll that stalls would take the ids away from a reconciler that still wants
the channels.

### The write is `DEL` + `ZADD`, not a rank trim (corrected in Phase 1)

This document first specified one `ZADD` plus a `ZREMRANGEBYRANK` trim. **That
does not work.** A member that leaves the desired set keeps the score it was
last written with, and that score is also a low rank, so the trim cannot tell
it from a wanted member. It would keep the stale login and evict a wanted one
instead.

The poller instead issues `DEL chat:desired chat:desired:ids`, then `ZADD`,
then `HSET`, then `INCR` on the generation, all inside one MULTI/EXEC. Redis
runs a transaction whole, so the reconciler never reads the gap between the
delete and the write. This is still one round trip whatever changed, which is
what FR-003 actually requires: the cost follows the SIZE of the desired set,
never the size of the CHANGE.

The reconciler holds its own view of the **actual set** — the subscriptions
that exist — rebuilt from `get_eventsub_subscriptions()` at start-up (page
count, not `total`) and maintained in memory after that.

## In-memory — connection pool (reconciler process)

| Entity | Fields | Notes |
|---|---|---|
| `Connection` | `websocket`, `subscription_ids: set`, `occupancy` | `occupancy <= 300` (measured cap) |
| `Pool` | `connections: list[Connection]` | Starts empty. Grows on demand when every open connection is at the cap |
| Routing | Rendezvous hash: the connection whose `blake2b("{broadcaster_id}:{connection_id}")` digest scores highest, among those with room | A placed channel keeps its connection across reconciles and is never moved by growth (D6) |

### Routing is rendezvous hashing, not modulo (corrected in Phase 2)

This document first wrote the routing rule as
`consistent_hash(id) % len(connections)`. **Modulo is the thing D6 exists to
prevent.** Adding a connection changes the divisor, so nearly every channel
moves to a different socket. That is a full reshuffle of the pool on growth.

The implementation uses **rendezvous hashing**: score every connection against
the broadcaster id and take the highest. Adding a connection moves only the
channels that score higher on the new one. Two further details are
load-bearing:

- Connection ids come from a monotonic counter, so retiring one does not
  renumber the survivors.
- The digest is `blake2b`, not the built-in `hash()`. Python salts `hash()` of
  a string per process, so `hash()` would reshuffle every channel on restart.

**What this does not give you (narrowed in Phase 6).** The score is stable, but
the *placement* is not stable across restarts, because `route()` only considers
connections that are already open and the pool grows only once they are full. A
cold start therefore fills connection 0 to the cap before connection 1 exists,
so which socket a channel lands on depends on arrival order as well as its
score. That is deliberate and it costs nothing: a websocket session dies with
the process, so a restart has no subscriptions to preserve. The value D6 is
after is entirely within one process lifetime — growth must not move a channel
that is working, and a socket death must cost only that socket's channels.

## State transitions — a channel through the reconciler

```
        in desired set, not subscribed
                    │
                    ▼
             create subscription ───▶ 403 "missing proper authorization"
                    │                        │
              enabled                   set eventsub_refused_at, skip
                    │                        │
                    ▼                   (retry after 7 days)
             publishing to Kafka
                    │
     leaves desired set (below LEAVE_THRESHOLD)
                    │
                    ▼
             delete subscription  ──▶ "not found" is not an error
                    │
              socket death: subscription lost, channel re-enters
              "not subscribed" on the next reconcile
```
