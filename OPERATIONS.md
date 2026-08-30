# Stream Scout Operations Guide

This guide covers the full restart procedure, per-service restart steps, and troubleshooting. For the short version, see `QUICKSTART.md`.

## Prerequisites

1. **Docker** installed and running. Check with `docker info`.
2. **Environment variables** in a `.env` file in the project root:
   ```
   TWITCH_CLIENT_ID=your_client_id_here
   TWITCH_CLIENT_SECRET=your_client_secret_here
   ```
3. **Twitch tokens** in `./secrets/twitch_user_tokens.json`. Run `python seed_twitch_tokens.py` if this file is missing. The token must carry **two scopes**, and `seed_twitch_tokens.py` asks for exactly these:
   - **`user:read:chat`** — the EventSub `channel.chat.message` subscription, which is how chat arrives. Without it every subscription refuses, and the service logs that at ERROR on start-up rather than going quiet.
   - **`clips:edit`** — clip creation, shared with the Flink job.

   `chat:read` is **no longer required and no longer requested**. It was the IRC scope, and IRC was removed in spec 004 Phase 3. A token seeded before that still works, because a superset is fine, but re-seeding drops it.

---

## How the Flink job runs

The Clip Detector job runs under Flink Application Mode. There is no separate "submit the job" step, ever. The job's code runs as part of the `flink-jobmanager` container's own startup — starting that container **is** starting the job. `docker-entrypoint-job.sh` does this by launching `standalone-job.sh` with `--job-classname org.apache.flink.client.python.PythonDriver`, Flink's own entry point for running a Python job this way.

This has one important consequence: **the job's lifecycle and the container's lifecycle are the same thing.** If the job fails to start, the whole `flink-jobmanager` container exits — it does not stay up in a broken state. If the job later reaches any terminal state (cancelled, finished, or failed for good), the container exits then too. Docker's `restart: unless-stopped` policy brings the container back afterward, which starts the job fresh.

This applies to a manual `flink cancel` too, not just a crash. Cancelling the job restarts the whole container, not just the job.

There is nothing to submit by hand and no `-pyFiles` command to run. To restart the job, restart the container:
```bash
docker compose restart flink-jobmanager
```

If you are restarting to pick up a **code change**, use `--force-recreate` instead — a plain restart does not re-resolve a single-file bind mount. See "Note on image rebuilds" below.

`-pyFiles` (here, `-pyfs`) comes from the `FLINK_PYFILES` environment variable in `docker-compose.yml`, not hardcoded in the entrypoint script. Changing which files it lists needs no image rebuild — see "Adding a new Python module" below.

---

## How chat ingestion runs

Chat arrives over **EventSub websockets**. There is no IRC client, no chat rooms
to join, and no JOIN rate limit — all of that was removed in spec 004 Phase 3.

Two pieces do the work, and **both live inside the one `stream-monitoring`
container**:

- **The poller** runs on the APScheduler tick. It ranks the top live streams,
  applies the `JOIN_THRESHOLD` / `LEAVE_THRESHOLD` hysteresis band, and writes
  the wanted channel set to Redis (`chat:desired`, plus the login-to-id map in
  `chat:desired:ids`). It does no network work for subscriptions and its
  duration does not grow with the size of the change.
- **The reconciler** is an **asyncio task in the same process**, started next to
  the poll job. It reads that wanted set and drives the live subscriptions
  toward it — creating and deleting concurrently, up to `RECONCILE_CONCURRENCY`
  (default 10) at a time. It wakes when the poller bumps the generation counter,
  or every 5 s, whichever comes first.

**There is no separate container or service to start, stop, or check.** If
`stream-monitoring` is up, the reconciler is up. Restarting the container
restarts it, and it converges from whatever state it finds — existing
subscriptions are adopted, not duplicated.

Every few minutes (`RECONCILE_READOPT_INTERVAL_SECONDS`, default 300) it also
re-lists the subscriptions from Twitch rather than trusting the set it holds in
memory. That is the backstop for a subscription lost by a route the pool cannot
observe — the known one is the library's reconnect re-subscribing part way and
giving up — where the count would otherwise keep reporting a channel that no
longer exists.

Subscriptions are spread over a pool of websocket connections, **300 per
connection** (Twitch's cap). The pool starts empty and opens another connection
when the ones it has are full, so ~500 channels run on two.

**This transport tops out at 900 channels.** Twitch allows a maximum of **3
websocket connections with enabled subscriptions** per client-id/user-id pair,
at 300 each. The pool refuses to open a fourth rather than let Twitch reject the
subscriptions on it one by one, so the ceiling shows up as a clear log line —
`pool is at its 3-connection limit` — instead of a silent retry loop. Past 900
the required transport is EventSub **webhook**, not more sockets; see
`specs/004-eventsub-parallel-reconciler/research.md` D1.

### Reading the reconciler metrics

All of these are on the stream-monitoring metrics endpoint, port **9100**:

```bash
curl -s http://localhost:9100/metrics | grep -E '^(eventsub_|reconcile_|subscription_create)'
```

| Metric | Read it as |
|---|---|
| `eventsub_subscription_count` | Live subscriptions held. Should equal the wanted set — compare against `ZCARD chat:desired`. It moves DURING a pass, not only at the end: it steps up as a cold start creates, and drops the moment a socket loss is reported, which is the dip to alert on. A steady small gap is normally one channel refusing authorization; check the logs for `subscription missing proper authorization` |
| `reconcile_last_success_timestamp` | Unix time of the last pass that **ran to completion**. **This is the stalled-reconciler alarm.** If it stops advancing while polls keep succeeding, the reconciler is stuck and the subscription set is frozen — the poller cannot tell you this, because it still works. Two things it does **not** mean: it is not "a pass with no failures" (at 500 channels one broadcaster refuses every pass, so gating on that would freeze the gauge and destroy the signal — per-channel failures are `subscription_create_failures_total`); and **a gap of a few minutes during a cold start is normal, not a stall**. A pass that hits 429s backs off and retries inside the pass, up to `RECONCILE_MAX_RETRY_ROUNDS` × `RECONCILE_RATE_LIMIT_BACKOFF_SECONDS` ≈ 200 s at the defaults, and the stamp only lands when the pass ends. Before restarting anything, check `reconcile_duration_seconds` and whether the subscription count is still climbing |
| `reconcile_duration_seconds` | Histogram of pass duration. A converged pass is milliseconds. Buckets run to 120 s because a cold start to 500 channels takes ~51 s |
| `subscription_create_failures_total` | Counter, labelled by `reason`. **No series at all is the healthy state**, not a broken exporter: this client registers a labelled series on its first increment |
| `eventsub_connection_occupancy` | Subscriptions per connection, labelled by connection id. None should exceed 300 |

---

## Important: Postgres and Redis are remote

Postgres and Redis do **not** run on this machine. They run on the Tailscale host `streamer-summaries-api` (100.112.97.111). `docker-compose.override.yml` points `api-frontend`, `stream-monitoring`, and both Flink containers at that host.

The `postgres` and `redis` services in `docker-compose.yml` exist but stay off. They sit behind the `local-db` compose profile, so `docker compose up -d` does not start them.

This machine also runs two unrelated standalone containers named `postgres16` and `redis`. **Stream Scout does not use them.** Do not restart them or query them for Stream Scout data — they belong to a different project.

To check the remote database from this machine, do not run `psql` or `redis-cli` directly against a local container. Instead, check through the app:
```bash
curl http://localhost:5000/health
docker logs streamscout-api-frontend --tail 20
```
A working `/v1.0/clip` response or a "Database connection pool initialized" log line confirms the remote database is reachable. To restart Postgres or Redis, you need access to the `streamer-summaries-api` host — this guide does not cover that host.

---

## Database migrations (manual)

`infrastructure/postgres/init.sql` runs **only when the database is created**.
The deployed database on `streamer-summaries-api` was created long ago, so a new
column in `init.sql` never reaches it. Every schema change needs the equivalent
`ALTER TABLE` run by hand against the remote host, before the code that reads
the column is deployed. Code that queries a column the database does not have
throws on every call.

Run the statement from this machine with the stream-monitoring virtualenv, which
already has `psycopg2` and can reach the Tailscale host:

```bash
cd services/stream-monitoring
.venv/bin/python -c "
import psycopg2
conn = psycopg2.connect('postgresql://twitch:twitch_password@100.112.97.111:5432/twitch')
with conn, conn.cursor() as cur:
    cur.execute(open('/path/to/migration.sql').read())
"
```

Wrap every migration in one transaction. Postgres DDL is transactional, so a
`BEGIN ... COMMIT` block cannot half-apply and leave the schema in a state
neither the old nor the new code understands.

### Spec 004 Phase 2 — the two self-heal timestamps

Applied 2026-08-28 to the deployed database: 1,404 rows, 134 backfilled. This is
the **only** migration spec 004 needs; it is complete as written below. Adds `eventsub_refused_at` (the reconciler writes it when a
channel refuses the chat subscription) and `clipping_disabled_at` (the Flink job
writes it beside every `allows_clipping = FALSE`). Both carry the same 7-day
re-check, so a channel that fixes its settings stops being skipped forever.

```sql
BEGIN;
ALTER TABLE streamers ADD COLUMN eventsub_refused_at TIMESTAMPTZ;
ALTER TABLE streamers ADD COLUMN clipping_disabled_at TIMESTAMPTZ;
-- Backfill, so the rows already disabled do not all look "stale" (older than
-- 7 days, therefore due a retry) the moment the new code starts:
UPDATE streamers SET clipping_disabled_at = NOW() WHERE allows_clipping = FALSE;
COMMIT;
```

Note: the deployed `streamers` table declares its existing time columns as
`timestamp without time zone`, while `init.sql` says `TIMESTAMPTZ`. The two new
columns follow `init.sql`. Both types compare correctly against `NOW()`, which is
all the 7-day rule needs.

---

## Part 1: Full restart

The normal way to restart is the script:
```bash
cd ~/stream-scout
./start.sh
```
This stops all containers, starts them again, and waits for every container with a health check to report healthy. flink-jobmanager is one of them. It takes well under a minute in the common case.

It can take longer if a container is slow to start. Kafka's own health check allows up to about 150 seconds. flink-jobmanager's container does not even start until Kafka is healthy and `kafka-init` has finished creating the Kafka topics. Once flink-jobmanager's container does start, its own health check allows up to about 210 more seconds.

The script does not submit the Flink job, and never needs to. The flink-jobmanager container runs the job as part of its own startup — see "How the Flink job runs" above. If the job fails to start, the container itself never reports healthy, so a broken job shows up as a failed restart, not a silently-empty one.

**After it finishes, confirm the Flink job is running:**
```bash
docker exec streamscout-flink-jobmanager flink list
```
This should show one "Clip Detector Job (RUNNING)". If flink-jobmanager did not become healthy, the job failed to start — check `docker logs streamscout-flink-jobmanager` for the error (a Python traceback, most often).

**If `start.sh` is not available**, run the same steps manually:
```bash
docker compose down
docker compose up -d --wait --wait-timeout 500
docker exec streamscout-flink-jobmanager flink list
```

**Then verify:**
```bash
curl http://localhost:5000/health
```
Open http://localhost:8081 for the Flink dashboard, and http://localhost:3000 (`admin`/`admin`) for Grafana.

**Note on image rebuilds:** `start.sh` does not rebuild images. If you changed anything not bind-mounted into a container — `docker-entrypoint-job.sh`, the Dockerfile, `flink-conf.yaml` — rebuild it first:
```bash
docker compose build flink-jobmanager flink-taskmanager
```
The four `.py` job files and `secrets/` are bind-mounted instead. See the `volumes:` section for `flink-jobmanager` in `docker-compose.yml`. Editing those needs no rebuild.

**But a restart is not always enough — use `--force-recreate` after anything that replaces the file.** Those `.py` files are bind-mounted **individually**, and a single-file bind mount follows the **inode**, not the path. Docker resolves it when the container is *created*. So any edit that writes a new file in place of the old one — `git checkout`, `git switch`, `sed -i`, an editor that saves by write-and-rename — leaves the container holding the old inode. The host file reads the new content and the container keeps running the old, with no error anywhere:

```bash
docker compose up -d --force-recreate flink-jobmanager flink-taskmanager
```

Editing a file *in place* (appending, or an editor that truncates and rewrites the same inode) does survive a plain `docker compose restart`. Do not rely on knowing which kind of edit you just made.

**This has already bitten once.** After spec 004 Phase 3 merged, `stream-monitoring` went on running the Phase-2-era service — IRC client still in it — for hours, because the branch checkout replaced `stream_monitoring_service.py` and the container kept the old inode. It was found only by comparing checksums. See `specs/004-eventsub-parallel-reconciler/research.md`, "Deployment trap found while verifying".

**Verify the deploy rather than assuming it.** Read the value back out of the running container:
```bash
docker exec streamscout-stream-monitoring md5sum /app/stream_monitoring_service.py
md5sum services/stream-monitoring/stream_monitoring_service.py    # must match

docker exec streamscout-flink-jobmanager python3 -c \
  "import sys; sys.path.insert(0,'/opt/flink/usrlib'); import spike_detector as s; \
   print(s.WATERMARK_OUT_OF_ORDERNESS_SECONDS, s.WATERMARK_IDLENESS_SECONDS)"
```

---

## Part 2: Checking system status

```bash
docker compose ps
```
All listed services should show `running`. flink-jobmanager should also show `healthy`.

```bash
docker exec streamscout-flink-jobmanager flink list
```
Should show one "Clip Detector Job (RUNNING)".

```bash
curl -s "http://localhost:5000/v1.0/clip?limit=5" | python3 -m json.tool
```
Should return recent clips, or an empty array if none exist yet.

---

## Part 3: Restarting individual components

Use these steps when one component fails. Skip Postgres and Redis — see "Important" above.

### Kafka

**When:** connection errors, or messages not flowing.
```bash
docker compose restart kafka
docker compose up -d --wait --wait-timeout 180 kafka
docker exec streamscout-kafka kafka-topics --bootstrap-server localhost:9092 --list
```
Should list `chat-messages` and `stream-lifecycle`.

**After a Kafka restart, also restart these** — they hold open connections to Kafka that do not reconnect on their own:
```bash
docker compose restart stream-monitoring flink-jobmanager
docker compose up -d --wait --wait-timeout 500 flink-jobmanager
```
Restarting flink-jobmanager runs the job fresh — see "How the Flink job runs" above. Confirm: `docker exec streamscout-flink-jobmanager flink list`.

### Stream monitoring service

**When:** chat messages stop reaching Kafka, Twitch API errors, or websocket failures.
```bash
docker logs streamscout-stream-monitoring --tail 20
docker compose restart stream-monitoring
docker logs -f streamscout-stream-monitoring
```
Expect to see: `Stream Monitoring Service started`, `Reconciler started`, `Adopted existing subscriptions`, `Polling for top streams`, `Poll complete`. Press `Ctrl+C` to stop following.

To pick up a **code change**, use `docker compose up -d --force-recreate stream-monitoring` instead of `restart` — see "Note on image rebuilds".

### Flink job (Clip Detector)

**When:** no clips are appearing, the job shows FAILED, or TaskManager reports heartbeat timeouts.

There is no separate job to cancel and resubmit — restarting flink-jobmanager restarts the job:
```bash
docker compose restart flink-jobmanager flink-taskmanager
docker compose up -d --wait --wait-timeout 500 flink-jobmanager
```
Confirm it is running, then check that it is processing:
```bash
docker exec streamscout-flink-jobmanager flink list
docker logs -f streamscout-flink-taskmanager 2>&1 | grep -iE "token|kafka|baseline"
```

### API and frontend service

**When:** the API does not respond, returns 500 errors, or the frontend does not load.
```bash
docker compose restart api-frontend
curl http://localhost:5000/health
curl -s "http://localhost:5000/v1.0/clip?limit=1" | python3 -m json.tool
```

### Prometheus

**When:** metrics do not appear in Grafana.
```bash
docker compose restart prometheus
```
Check http://localhost:9090.

### Grafana

**When:** dashboards do not load, or login fails.
```bash
docker compose restart grafana
```
Check http://localhost:3000 (`admin`/`admin`).

### Loki and Promtail

**When:** logs do not appear in Grafana.
```bash
docker compose restart loki promtail
curl http://localhost:3100/ready
```
Should print `ready`.

---

## Part 4: Complete shutdown

**Stop all containers, keep data:**
```bash
docker compose down
```

**Stop all containers and delete local data:**
```bash
docker compose down -v
```
**Warning:** `-v` deletes local volumes — Kafka data, Prometheus/Grafana/Loki history. It does **not** touch clips or the database, because those live on the remote host, not in a local volume.

---

## Part 5: Viewing logs

```bash
docker logs streamscout-<service-name>
```
Service names: `kafka`, `stream-monitoring`, `flink-jobmanager`, `flink-taskmanager`, `api-frontend`, `prometheus`, `grafana`, `loki`, `promtail`, `alertmanager`, `node-exporter`.

```bash
docker logs streamscout-stream-monitoring --tail 50   # last 50 lines
docker logs -f streamscout-stream-monitoring           # follow live
docker logs -t streamscout-stream-monitoring --tail 20 # with timestamps
```

---

## Part 6: Common problems

### "No running jobs" in Flink
This means flink-jobmanager's container is not healthy, or has restarted and is still starting up. Check:
```bash
docker compose ps flink-jobmanager
docker logs streamscout-flink-jobmanager --tail 50
```
A Python traceback near the end of the log is the usual cause — a bad token file, a Kafka connection problem, or similar. Fix the cause, then restart the container:
```bash
docker compose restart flink-jobmanager
docker compose up -d --wait --wait-timeout 500 flink-jobmanager
```

### Flink job fails with "heartbeat timeout"
```bash
docker compose restart flink-jobmanager flink-taskmanager
docker compose up -d --wait --wait-timeout 500 flink-jobmanager
```

### "Token file not found" in Flink logs
```bash
ls -la ./secrets/twitch_user_tokens.json
```
If missing, run `python seed_twitch_tokens.py`, then restart Flink:
```bash
docker compose restart flink-jobmanager
docker compose up -d --wait --wait-timeout 500 flink-jobmanager
```

### Adding a new Python module
Two steps, not one:
1. Add a bind-mount line for the new file, under both `flink-jobmanager` and `flink-taskmanager` in `docker-compose.yml` (`volumes:`), matching the existing four.
2. Add the new path to the `FLINK_PYFILES` environment variable, under `flink-jobmanager`.

Then `docker compose up -d` — no image rebuild needed for either step.

### No chat messages are reaching Kafka

Chat is EventSub now, so there are no chat rooms to join. Work down the chain:

```bash
docker logs streamscout-stream-monitoring --tail 30
curl -s http://localhost:9100/metrics | grep -E '^(eventsub_subscription_count|reconcile_last_success_timestamp|subscription_create_failures_total)'
```

1. **`eventsub_subscription_count` is 0 and every create fails.** Look for
   `subscription missing proper authorization` on every channel. That is the
   token, not the broadcasters: it has no **`user:read:chat`** scope. The
   service also logs this at ERROR on start-up. Re-seed and force-recreate:
   ```bash
   python seed_twitch_tokens.py
   docker compose up -d --force-recreate stream-monitoring
   ```
   The service deliberately does **not** persist refusals while that scope is
   missing, so a token mistake cannot mark the whole monitored set as refused
   for seven days. Nothing needs undoing in the database afterwards.
2. **`reconcile_last_success_timestamp` is not advancing.** Check whether this
   is a cold start first: a pass that is backing off 429s can legitimately run
   for about 200 s at the default settings, and the subscription count will be
   climbing throughout. Restart the container only if the count is flat and
   `reconcile_duration_seconds` shows no pass completing.
3. **The count is right but Kafka is empty.** The subscriptions exist and are
   silent, so the problem is downstream — check the Kafka producer logs and
   `kafka_messages_produced`.
4. **The count has plateaued and the log says `pool is at its 3-connection
   limit`.** This is not a fault to restart: the monitored set has been raised
   past what this transport can carry. Twitch allows 3 websocket connections ×
   300 subscriptions = **900 channels** per client-id/user-id pair, and the pool
   refuses a fourth socket deliberately. `subscription_create_failures_total
   {reason="error"}` climbs while `eventsub_subscription_count` sits flat at
   ~900. Either lower `LEAVE_THRESHOLD` back under 900, or move to EventSub
   webhook — see `specs/004-eventsub-parallel-reconciler/research.md` D1.
   Re-seeding the token and restarting both achieve nothing here.
5. **Authentication errors of any other kind** mean the tokens expired. Re-seed
   as above.

A single channel refusing on every pass is normal — roughly 1 in 500 does — and
it is recorded in `streamers.eventsub_refused_at` and retried after 7 days.

### No clips after 5+ minutes
Check each of these in order:
1. Flink job running? `docker exec streamscout-flink-jobmanager flink list`
2. Messages in Kafka? `docker exec streamscout-kafka kafka-console-consumer --bootstrap-server localhost:9092 --topic chat-messages --max-messages 3 --timeout-ms 10000`
3. Stream monitoring sending messages? `docker logs streamscout-stream-monitoring --tail 30`
4. Baseline still building? The job needs 5 minutes of data before it detects anomalies: `docker logs streamscout-flink-taskmanager --tail 50 2>&1 | grep -i baseline`

### "403 Forbidden — User not authorized to create clips"
This is expected. Some streamers turn off clip creation. The system still creates clips for streamers who allow it.

### API returns an empty clips array
1. No clips created yet — wait 5+ minutes after startup.
2. Database connection issue — restart the API: `docker compose restart api-frontend`
3. Check the remote database has clips — see "Important: Postgres and Redis are remote" above for how to check without a local `psql`.

---

## Quick reference: URLs

| Service | URL |
|---|---|
| Frontend / API | http://localhost:5000 |
| Flink Web UI | http://localhost:8081 |
| Grafana | http://localhost:3000 |
| Prometheus | http://localhost:9090 |

## Quick reference: commands

| Action | Command |
|---|---|
| Full restart | `./start.sh` |
| Stop everything | `docker compose down` |
| Check status | `docker compose ps` |
| Check Flink job | `docker exec streamscout-flink-jobmanager flink list` |
| Restart the Flink job | `docker compose restart flink-jobmanager` |
| Rebuild the Flink images | `docker compose build flink-jobmanager flink-taskmanager` |
| View service logs | `docker logs streamscout-<service-name>` |
| Restart a service | `docker compose restart <service-name>` |
