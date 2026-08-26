# Stream Scout Operations Guide

This guide covers the full restart procedure, per-service restart steps, and troubleshooting. For the short version, see `QUICKSTART.md`.

## Prerequisites

1. **Docker** installed and running. Check with `docker info`.
2. **Environment variables** in a `.env` file in the project root:
   ```
   TWITCH_CLIENT_ID=your_client_id_here
   TWITCH_CLIENT_SECRET=your_client_secret_here
   ```
3. **Twitch tokens** in `./secrets/twitch_user_tokens.json`. Run `python seed_twitch_tokens.py` if this file is missing.

---

## How the Flink job gets submitted

The `flink-jobmanager` container submits the Clip Detector job itself, automatically, every time it starts. `docker-entrypoint-job.sh` does this. It starts the JobManager. It waits for the JobManager's REST API to answer. Then it submits the job.

This runs on a normal `start.sh` restart. It also runs on an unattended restart, from Docker's own `restart: unless-stopped` policy after a crash. Both cases start a brand-new JobManager process. The job does not use checkpointing, so neither case has a prior job to recover.

Nothing else submits the job. If you need to submit it by hand, run:
```bash
docker exec streamscout-flink-jobmanager sh -c 'flink run -py /opt/flink/usrlib/clip_detector_job.py -pyFiles "$FLINK_PYFILES" -d'
```
`$FLINK_PYFILES` is read inside the container, from the `FLINK_PYFILES` environment variable set in `docker-compose.yml`. That is the one place this list lives.

**Changing an existing entry** in that list needs no rebuild — edit `docker-compose.yml`, then `docker compose up -d`.

**Adding a genuinely new module** needs one more step. Each file in the list must already exist at that exact path inside both the flink-jobmanager and flink-taskmanager containers. The current four files get there through bind mounts in each service's `volumes:` section in `docker-compose.yml`. Add a matching bind-mount line for the new file, to both services, the same way — otherwise the path will not exist and submission will fail.

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

## Part 1: Full restart

The normal way to restart is the script:
```bash
cd ~/stream-scout
./start.sh
```
This stops all containers, starts them again, and waits for every container with a health check to report healthy. flink-jobmanager is one of them. It takes well under a minute in the common case.

It can take longer if a container is slow to start. Kafka's own health check allows up to about 150 seconds. flink-jobmanager's health check allows up to about 210 seconds on top of that, because its container does not even start until Kafka is healthy and `kafka-init` has finished creating the Kafka topics.

The script does not submit the Flink job. The flink-jobmanager container does that itself on startup — see "How the Flink job gets submitted" above.

**After it finishes, confirm the Flink job is running:**
```bash
docker exec streamscout-flink-jobmanager flink list
```
This should show one "Clip Detector Job (RUNNING)". If you see none, the submission likely failed — check `docker logs streamscout-flink-jobmanager` for an `ERROR: job submission failed` line and the Flink error above it, fix the cause, then submit by hand (see "How the Flink job gets submitted"). If you see two, check which one is actually healthy before cancelling either — do not assume the newer or the older one is the broken one:
```bash
docker exec streamscout-flink-jobmanager flink cancel <JOB_ID>
```

**If `start.sh` is not available**, run the same steps manually:
```bash
docker compose down
docker compose up -d --wait --wait-timeout 400
docker exec streamscout-flink-jobmanager flink list
```
`--wait` blocks until flink-jobmanager's REST API answers. That is not the same as the job being RUNNING — submission happens after that point, and can still fail. If `flink list` above shows none, wait a few seconds and check again, or see "How the Flink job gets submitted" above.

**Then verify:**
```bash
curl http://localhost:5000/health
```
Open http://localhost:8081 for the Flink dashboard, and http://localhost:3000 (`admin`/`admin`) for Grafana.

**Note on image rebuilds:** `start.sh` does not rebuild images. If you changed anything not bind-mounted into a container — `docker-entrypoint-job.sh`, the Dockerfile, `flink-conf.yaml` — rebuild it first:
```bash
docker compose build flink-jobmanager flink-taskmanager
```
The four `.py` job files and `secrets/` are bind-mounted (see the `volumes:` section for `flink-jobmanager` in `docker-compose.yml`), so editing those needs only a restart, not a rebuild.

---

## Part 2: Checking system status

```bash
docker compose ps
```
All listed services should show `running`.

```bash
docker exec streamscout-flink-jobmanager flink list
```
Should show one "Clip Detector Job (RUNNING)" — not two, not zero. See Part 1 above for what each of those means.

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
docker compose restart stream-monitoring flink-jobmanager flink-taskmanager
docker compose up -d --wait --wait-timeout 400 flink-jobmanager
```
flink-jobmanager resubmits the job itself on restart — no separate submit step needed. Confirm: `docker exec streamscout-flink-jobmanager flink list`.

### Stream monitoring service

**When:** not joining chat rooms, Twitch API errors, or websocket failures.
```bash
docker logs streamscout-stream-monitoring --tail 20
docker compose restart stream-monitoring
docker logs -f streamscout-stream-monitoring
```
Expect to see: `Stream Monitoring Service started`, `Polling for top streams`, `Joined chat room`. Press `Ctrl+C` to stop following.

### Flink job (Clip Detector)

**When:** no clips are appearing, the job shows FAILED, or TaskManager reports heartbeat timeouts.

1. Check the current job:
   ```bash
   docker exec streamscout-flink-jobmanager flink list
   ```
2. If a job is running but broken, cancel it:
   ```bash
   docker exec streamscout-flink-jobmanager flink cancel <JOB_ID>
   ```
3. Restart both Flink containers, then wait for flink-jobmanager to report healthy:
   ```bash
   docker compose restart flink-jobmanager flink-taskmanager
   docker compose up -d --wait --wait-timeout 400 flink-jobmanager
   ```
   flink-jobmanager resubmits the job itself on restart.
4. Confirm it is running, then check that it is processing:
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
The flink-jobmanager container submits the job itself on startup. Zero jobs after that means submission failed. Check why:
```bash
docker logs streamscout-flink-jobmanager | grep -A5 "ERROR: job submission failed"
```
Fix the cause, then submit by hand (see "How the Flink job gets submitted" above).

### Flink job fails with "heartbeat timeout"
```bash
docker compose restart flink-jobmanager flink-taskmanager
docker compose up -d --wait --wait-timeout 400 flink-jobmanager
```
The job resubmits itself. Confirm: `docker exec streamscout-flink-jobmanager flink list`.

### "Token file not found" in Flink logs
```bash
ls -la ./secrets/twitch_user_tokens.json
```
If missing, run `python seed_twitch_tokens.py`, then restart Flink:
```bash
docker compose restart flink-jobmanager flink-taskmanager
docker compose up -d --wait --wait-timeout 400 flink-jobmanager
```
The job resubmits itself.

### Stream monitoring is not joining any chat rooms
```bash
docker logs streamscout-stream-monitoring --tail 30
```
Authentication errors mean the Twitch tokens expired. Regenerate them:
```bash
python seed_twitch_tokens.py
docker compose restart stream-monitoring
```

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
| Submit Flink job by hand | `docker exec streamscout-flink-jobmanager sh -c 'flink run -py /opt/flink/usrlib/clip_detector_job.py -pyFiles "$FLINK_PYFILES" -d'` |
| Rebuild the Flink images | `docker compose build flink-jobmanager flink-taskmanager` |
| View service logs | `docker logs streamscout-<service-name>` |
| Restart a service | `docker compose restart <service-name>` |
