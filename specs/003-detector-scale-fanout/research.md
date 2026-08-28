# Research: Scaling to 2000 Broadcasters

Date: 2026-08-27. All numbers here are measured, not estimated, unless the text
says otherwise.

## 1. The ingestion ceiling is removable

### The IRC constraint

`stream_monitoring_service.py` joins chat over IRC through pyTwitchAPI's `Chat`
client. The JOIN rate limit is **per account**, not per connection or per
machine: `RateLimitBucket(10, 2000 if is_verified_bot else 20, 'channel_join')`.
A non-verified account gets 20 JOINs per 10 seconds.

This limit fires in production today at only 15 channels. Observed in
`streamscout-stream-monitoring` logs during the spike:

```
Bucket channel_join got rate limited. waiting 9.60s...
```

More connections do not help. More machines do not help. Only more accounts, or
verified-bot status, raise it.

### The EventSub result

EventSub `channel.chat.message` was tested against live channels with a plain
user token carrying only `user:read:chat`.

**Two-channel spike:**

| Measure | Result |
|---|---|
| Subscriptions accepted | 2 of 2 (`tumblurr` 43k viewers, `zackrawrr` 35k) |
| Broadcaster consent needed | None |
| `total_cost` | 0 |
| `max_total_cost` | 10 |
| Messages in 45 s | 138 |
| Delivery lag | p50 154 ms, p95 222 ms, max 366 ms |

**394-channel fan-out spike:**

| Measure | Result |
|---|---|
| Subscriptions created | 394 across 2 sockets |
| Per-connection cap | **exactly 300** (one socket refused 5 in a row at 300) |
| Per-user ceiling | none found; 394 `enabled` ran concurrently |
| `total_cost` at 394 subs | **0** |
| Creation throughput | 2.1 subs/s sequential, p50 421 ms per POST, zero 429s |
| Refusals | 6 of 400 (~1.5%), `subscription missing proper authorization` |
| Messages in 60 s | 31,898 (531 msg/s) |
| Channels that spoke | 378 of 394 |
| Delivery lag | p50 154 ms, p95 220 ms, **max 1243 ms** |

### What this means

- The `user:bot` / `channel:bot` / moderator requirement applies only to the
  app-token path. The user-token path needs no broadcaster consent.
- User-authorized subscriptions **cost nothing**. The `max_total_cost=10` cap
  never applies. There is no subscription-count ceiling on this path.
- 300 is a **per-connection** cap, not a per-user one. 2000 channels needs
  about 7 sockets.
- Creation is **latency-bound, not rate-limited**. It parallelizes. This is the
  key difference from the IRC JOIN bucket, which blocks regardless.

### Traps found

- **`get_eventsub_subscriptions().total` is unreliable.** It reported 300 while
  enumerating the pages yielded 396. Count pages; do not trust `total`.
- **The per-message timestamp is on the envelope**, at
  `metadata.message_timestamp`. `ChannelChatMessageData` has no timestamp field
  at all. The envelope value is Twitch's *dispatch* time. `sent_at` today comes
  from IRC's `tmi-sent-ts`, which is a *send* time. These are different
  quantities.
- **The delivery-lag tail grows with fan-out.** At 2 channels, max was 366 ms.
  At 394 channels, p95 held at 220 ms but max reached **1243 ms**. That exceeds
  `WATERMARK_OUT_OF_ORDERNESS_SECONDS = 1` (commit `4ce10e0`). Those events
  would be dropped as late.
- **The library drops events during the subscribe ramp.** It logged `received
  event for unknown subscription with ID ...` — events arriving before the sub
  id is registered.
- Subscriptions linger as `websocket_disconnected` after a socket closes, and
  `DELETE` on them returns "not found". Twitch garbage-collects them. Restart
  reconciliation must tolerate stale entries.
- Re-authorizing the same app for the same user did **not** invalidate the
  existing production token.

### Throughput, honestly

The fan-out spike measured 531 msg/s from the **top** 394 channels. A naive
linear extrapolation gives ~2700 msg/s at 2000 channels. **That is an
overestimate.** The distribution is severely skewed: max 1880 messages in 60 s
(`hasanabi`, ~31 msg/s), median 32 (~0.53 msg/s). Channels ranked 400 to 2000
are far quieter than these. A realistic figure is roughly 1200 to 1800 msg/s.

## 2. Where the detector cost actually is

This is the part that changed the design, so the reasoning is recorded in full.

### ⚠️ T003 RESOLVED — the original premise was wrong

**Resolved 2026-08-27 by reading PyFlink 1.18's `fn_execution/state_impl.py`.
Read this before the section below, which states the superseded model.**

`MapState.items()` is **not** ~305 remote accesses. The real mechanism:

1. `items()` → `remote_data_iterator()` → `CachingMapStateHandler.lazy_iterator()`.
2. `lazy_iterator` **first checks a local LRU read cache**. If
   `cached_map_state.is_all_data_cached()`, it returns `create_cache_iterator(...)`
   over a plain Python dict — **zero remote calls**.
3. On a miss it does **one batched** `_iterate_raw` fetch with a continuation
   token — not one call per entry. The batch size
   (`python.map-state.iterate-response-batch-size`, default 1000) exceeds our
   305, so the whole map arrives in a single batch.
4. It then caches the entire map, if it fits `_max_cached_map_key_entries`.

So the corrected cost model is:

| Condition | Cost per broadcaster-second |
|---|---|
| Read-cache hit | 0 remote calls; a 305-entry in-memory dict iteration (microseconds) |
| Read-cache miss | 1 batched remote fetch + ~305 entry decodes |

**The real risk is an LRU capacity cliff, not a per-access cost.** The outer
`_state_cache` is an `LRUCache(state_cache_size)`, where `state_cache_size`
comes from `python.state.cache-size` (Flink default **1000**).

- At 30 broadcasters: 30 keys against a 1000-entry LRU. Everything stays
  cached. `items()` is nearly free. **This is why the detector is fine today.**
- At 2000 broadcasters: 2000 keys cycling every second through a 1000-entry
  LRU. **Every key is evicted before its next access.** Every `items()` becomes
  a cache miss — a batched fetch plus ~305 decodes, 2000 times a second.

`flink-conf.yaml` sets no `python.*` options, and neither does
`docker-compose.yml`, so the defaults apply.

**What this means for the fix.** The figure of ~610,000 is still meaningful,
but as *entry decodes on cache-miss*, not as round trips. And the cliff is
**configurable**, which opens a far cheaper option than the redesign below:

- **Option 1 (config only)**: raise `python.state.cache-size` above the
  broadcaster count and size `python.map-state.read-cache-size` for 305-entry
  maps. **Zero code change, zero detection risk.** Cost is Python-process
  memory: ~2000 x 305 = 610,000 cached entries.
- **Option 2 (the ring buffer below)**: fewer cached objects per key and no
  per-entry decode. More code, and it puts FR-002 equivalence at risk.
- **Option 3**: shrink `baseline_seconds`. Changes detection semantics.
  Rejected.

### The cliff is further away than it first looked

The LRU is per `RemoteKeyedStateBackend`, which is **per Python worker, so per
subtask** — not per job. Keyed state partitions across subtasks by hash, so:

| Broadcasters | Parallelism | Keys per subtask | vs `state_cache_size` = 1000 |
|---|---|---|---|
| 30 (today) | 4 | ~8 | fits easily |
| 2000 (target) | 4 | **~500** | **still fits** |
| 4000 | 4 | ~1000 | at the cliff |
| 4000 | 8 (2 TaskManagers) | ~500 | fits again |

Each cached map holds 305 entries, against `map_state_read_cache_size` = 1000,
so an individual map fits too.

**So at 2000 broadcasters with parallelism 4, the LRU does not thrash.** The
cliff arrives near 4000, and adding TaskManagers pushes it further out — which
is exactly the operator's stated plan for going higher.

This is the third correction to this feature's premise, and it points one way:
**the detector is probably not the wall at 2000.** The remaining per-key work is
modest — a 305-entry dict build plus `evaluate()` at ~38 µs, roughly 100-200 ms
of Python CPU per second spread over 4 worker processes.

### The likelier constraint: TaskManager heap

Measured 2026-08-27 on the live stack:

| Measure | Value |
|---|---|
| Machine RAM | 15 GiB total, 5.2 GiB available, 2.3 GiB swap in use |
| TaskManager | **1.47 GiB of its 2048 MB cap (~73%) at only ~30 broadcasters** |
| JobManager | 615 MiB |
| Kafka | 1.09 GiB |

`state.backend: hashmap` keeps all keyed state on the JVM heap, and
checkpointing is off. The TaskManager is already at 73% of its cap while
watching 30 broadcasters. Adding 2000 keys of bucket state is far more likely
to hit this ceiling than the Python LRU is.

**Recommendation: raise `taskmanager.memory.process.size` before touching any
code.** The box has 5.2 GiB free, so 4-6 GB is available at no cost beyond
config. That addresses the probable real constraint, and Option 1's cache sizes
become a cheap follow-on if measurement shows the LRU matters at all.

---

### The measurement (superseded — kept for the record)

`AnomalyDetector.on_timer` runs once per broadcaster per event-time second. It
begins:

```python
all_counts = {ts: c for ts, c in self.message_counts.items() if c is not None}
```

`message_counts` is a `MapState`. It holds `retained_seconds` buckets, which is
`baseline_seconds + window_seconds` = 300 + 5 = **305** at the defaults. So
this line performs about 305 keyed-state accesses per broadcaster-second.

| Broadcasters | State accesses/second |
|---|---|
| 30 (today) | ~9,150 |
| 2000 (target) | **~610,000** |

Those cross PyFlink's Python-to-Java boundary, spread over 4 subtasks, on a
2048 MB TaskManager.

### What is NOT the bottleneck

The arithmetic is cheap. `_mean_and_sample_stdev` documents its own cost: the
`statistics` module needs ~380 µs per 300-bucket baseline, and this
implementation is ~10x faster, so ~38 µs. At 2000 broadcasters that is ~76 ms
of CPU per wall-clock second. Real, but not the wall.

Iterating a 305-entry dict already in Python memory is also cheap —
microseconds.

**The expensive operation is state access, not iteration and not arithmetic.**

### Why this reverses the first design proposal

The first proposal in discussion was to keep running aggregates and use
Welford's online algorithm. That was wrong on three counts, and the record
should say so:

1. **Welford solves a problem this code does not have.** It guards against
   catastrophic cancellation in floating-point variance. These values are small
   non-negative integer counts over 300 samples. Sum-of-squares tops out near
   10^8, against float64's ~15-16 significant digits. There is no meaningful
   cancellation risk at this magnitude.
2. **Welford is the wrong shape.** It is built for *appending* samples. A
   sliding window must also *evict*, and Welford removal is poorly behaved.
3. **It would have contradicted a deliberate, documented decision.**
   `_mean_and_sample_stdev` explicitly rejects sum-of-squares: "It uses two
   passes, not a sum of squares, so it does not lose precision." Changing that
   silently would have reversed a considered choice.

Most importantly, all three miss the point: **the aggregate would have
optimised the arithmetic, which was never the bottleneck.**

### The correct target

Replace the 305-entry `MapState` with a **single `ValueState` holding the whole
retained window**. That turns the ~305 state accesses on the **timer path**
into one read and one write.

Note the trade, because it is easy to state this too simply: the write path
changes too. `process_element` currently does one `MapState.put` per message;
against a single stored window it becomes a read-modify-write per message. So
the feature removes a per-second scan and adds per-message cost. `plan.md` C1
carries this, and SC-001b requires the **total** to fall, not just the timer
path. Phase 0 must measure both.

The math does not change at all. The adapter rebuilds an ordinary Python dict
from the single stored value and passes it to `evaluate()` exactly as today.
Dict construction in local memory is microseconds; the 305 state reads it
replaces are not.

This is both the larger win and the far smaller change. `evaluate()` keeps its
signature, its two-pass statistics, and its entire test suite. Only the
adapter's storage layer changes.

See `plan.md` for the design and for the late-write and TTL consequences.

## 3. Downstream items this feature does not fix

- `ClipCreator.process_element` spawns an unbounded raw `threading.Thread` per
  anomaly, each able to live "the better part of half an hour" by its own
  comment. There is no global limiter, and 429 is marked retryable. At 2000
  broadcasters this means permanent backoff and hundreds of live threads.
- Clip creation is capped per account, like JOIN was. At 2000 channels the
  system will detect far more anomalies than it can act on. That calls for
  ranking anomalies against a scarce clip budget — a design change, not a
  limiter.
- `chat-messages` is pinned at 4 partitions to match `FLINK_PARALLELISM=4`.
  Kafka cannot shrink a partition count in place, so raising parallelism needs
  the topic re-provisioned.
