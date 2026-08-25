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
The script does these steps, in order:
1. Rebuild the Flink images. This step runs first. If it fails, nothing
   has been stopped yet.
2. Stop all containers.
3. Start all containers. Wait for every container with a health check to
   report healthy, flink-jobmanager included (up to 240 seconds).
4. Submit the Flink job.
5. Wait for the job to start (15 seconds).

In the common case, step 3 finds every container healthy within a few
seconds, and the full sequence takes well under a minute. It can take
longer if the Flink images need a real rebuild, or if the JobManager is
slow to start. The script stops with an error if a container is not
healthy after 240 seconds, rather than wait forever.

The rebuild step matters. `docker-entrypoint-job.sh` is baked into the flink-jobmanager image at build time, not bind-mounted. A plain `docker compose up -d` would reuse the old image and skip any change to that file.

The jobmanager container starts the JobManager only. It does not submit a job. `start.sh` is the only thing that submits the job. A normal run of this script produces exactly one "Clip Detector Job".

**After it finishes, confirm exactly one Flink job is running:**
```bash
docker exec streamscout-flink-jobmanager flink list
```
This should show one "Clip Detector Job (RUNNING)". If you see none, submit the job by hand (see "Flink job" below).

**If `start.sh` is not available**, run the same steps manually:
```bash
docker compose build flink-jobmanager flink-taskmanager
docker compose down
docker compose up -d --wait --wait-timeout 240
docker exec streamscout-flink-jobmanager flink list
```
`--wait` blocks until flink-jobmanager is healthy, so this command does not return until the JobManager can accept a submission.

If the output shows "No running jobs", submit the job:
```bash
docker exec streamscout-flink-jobmanager flink run -py /opt/flink/usrlib/clip_detector_job.py -pyFiles /opt/flink/usrlib/spike_detector.py,/opt/flink/usrlib/token_manager.py,/opt/flink/usrlib/clip_attempt.py -d
```

**Then verify:**
```bash
curl http://localhost:5000/health
```
Open http://localhost:8081 for the Flink dashboard, and http://localhost:3000 (`admin`/`admin`) for Grafana.

---

## Part 2: Checking system status

```bash
docker compose ps
```
All listed services should show `running`.

```bash
docker exec streamscout-flink-jobmanager flink list
```
This should show one "Clip Detector Job (RUNNING)". Zero jobs is expected after an unattended container restart, such as a crash. It is not expected after a normal `start.sh` run. See "No running jobs" in Flink, in Part 6. Two jobs means something submitted twice. Cancel the extra one.

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
sleep 30
docker exec streamscout-kafka kafka-topics --bootstrap-server localhost:9092 --list
```
Should list `chat-messages` and `stream-lifecycle`.

**After a Kafka restart, also restart these** — they hold open connections to Kafka that do not reconnect on their own. `docker compose up -d` alone would not touch them: their container config has not changed, so compose would leave the old, dead connections running. Use `restart`, then wait for flink-jobmanager to report healthy again:
```bash
docker compose restart stream-monitoring flink-jobmanager flink-taskmanager
docker compose up -d --wait --wait-timeout 240 flink-jobmanager
```
Then resubmit the Flink job (see "Flink job" below).

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
3. Restart both Flink containers, then wait for flink-jobmanager to
   report healthy. `docker compose up -d` alone would not restart
   already-running containers with no config change, so use `restart`
   first:
   ```bash
   docker compose restart flink-jobmanager flink-taskmanager
   docker compose up -d --wait --wait-timeout 240 flink-jobmanager
   ```
4. The jobmanager container does not auto-submit a job, so submit one:
   ```bash
   docker exec streamscout-flink-jobmanager flink run -py /opt/flink/usrlib/clip_detector_job.py -pyFiles /opt/flink/usrlib/spike_detector.py,/opt/flink/usrlib/token_manager.py,/opt/flink/usrlib/clip_attempt.py -d
   ```
5. Confirm it is running, then check that it is processing:
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
The jobmanager container never submits a job by itself. This is true for a
normal `start.sh` restart. It is also true if the container crashes and
Docker restarts it on its own. Compose's `restart: unless-stopped` policy
brings the container back, but with an empty JobManager and no job. In
both cases, submit the job:
```bash
docker exec streamscout-flink-jobmanager flink run -py /opt/flink/usrlib/clip_detector_job.py -pyFiles /opt/flink/usrlib/spike_detector.py,/opt/flink/usrlib/token_manager.py,/opt/flink/usrlib/clip_attempt.py -d
```

### Flink job fails with "heartbeat timeout"
```bash
docker compose restart flink-jobmanager flink-taskmanager
docker compose up -d --wait --wait-timeout 240 flink-jobmanager
docker exec streamscout-flink-jobmanager flink run -py /opt/flink/usrlib/clip_detector_job.py -pyFiles /opt/flink/usrlib/spike_detector.py,/opt/flink/usrlib/token_manager.py,/opt/flink/usrlib/clip_attempt.py -d
```

### "Token file not found" in Flink logs
```bash
ls -la ./secrets/twitch_user_tokens.json
```
If missing, run `python seed_twitch_tokens.py`, then restart Flink and resubmit the job:
```bash
docker compose restart flink-jobmanager flink-taskmanager
docker compose up -d --wait --wait-timeout 240 flink-jobmanager
docker exec streamscout-flink-jobmanager flink run -py /opt/flink/usrlib/clip_detector_job.py -pyFiles /opt/flink/usrlib/spike_detector.py,/opt/flink/usrlib/token_manager.py,/opt/flink/usrlib/clip_attempt.py -d
```

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
| Submit Flink job | `docker exec streamscout-flink-jobmanager flink run -py /opt/flink/usrlib/clip_detector_job.py -pyFiles /opt/flink/usrlib/spike_detector.py,/opt/flink/usrlib/token_manager.py,/opt/flink/usrlib/clip_attempt.py -d` |
| View service logs | `docker logs streamscout-<service-name>` |
| Restart a service | `docker compose restart <service-name>` |
