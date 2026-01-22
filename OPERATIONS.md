# Stream Scout Operations Guide

This guide covers how to start, stop, and restart all components of the Stream Scout system.

## Prerequisites

Before starting, make sure you have:

1. **Docker Desktop** installed and running (check for the whale icon in your menu bar)
2. **Environment variables** set up in a `.env` file in the project root:
   ```
   TWITCH_CLIENT_ID=your_client_id_here
   TWITCH_CLIENT_SECRET=your_client_secret_here
   ```
3. **Twitch tokens** seeded in `./secrets/twitch_user_tokens.json` (run `python seed_twitch_tokens.py` if missing)

---

## Part 1: Starting Everything Fresh

Use this when starting the system for the first time or after a complete shutdown.

### Step 1: Navigate to the project directory

```bash
cd /Users/john/Projects/stream-scout
```

### Step 2: Make sure Docker Desktop is running

Open Docker Desktop from your Applications folder if it's not already running. Wait until the whale icon in your menu bar stops animating.

### Step 3: Stop any existing containers (clean slate)

```bash
docker-compose down
```

You should see output like:
```
[+] Running 12/12
 ⠿ Container streamscout-grafana          Removed
 ⠿ Container streamscout-promtail         Removed
 ...
```

### Step 4: Start all infrastructure services

```bash
docker-compose up -d
```

You should see output like:
```
[+] Running 12/12
 ⠿ Container streamscout-postgres         Started
 ⠿ Container streamscout-redis            Started
 ⠿ Container streamscout-kafka            Started
 ...
```

### Step 5: Wait for services to be healthy (about 30-60 seconds)

```bash
docker-compose ps
```

You should see all services with `running` status. If any show `starting`, wait and run the command again.

### Step 6: Verify the Flink job is running

```bash
docker exec streamscout-flink-jobmanager flink list
```

You should see:
```
------------------ Running/Restarting Jobs -------------------
xx.xx.xxxx xx:xx:xx : xxxxxxxx : Clip Detector Job (RUNNING)
--------------------------------------------------------------
```

**If you see "No running jobs"**, the job needs to be submitted manually:

```bash
docker exec streamscout-flink-jobmanager flink run -py /opt/flink/usrlib/clip_detector_job.py -d
```

Wait about 10 seconds, then verify again:

```bash
docker exec streamscout-flink-jobmanager flink list
```

### Step 7: Verify all services are working

**Check the API is responding:**
```bash
curl http://localhost:5000/health
```

You should see:
```json
{"status":"healthy"}
```

**Check the Flink Web UI:**
Open http://localhost:8081 in your browser. You should see the Flink dashboard with 1 running job.

**Check Grafana:**
Open http://localhost:3000 in your browser. Login with username `admin` and password `admin`.

### Step 8: Monitor the logs to see clips being created

```bash
docker logs -f streamscout-flink-taskmanager 2>&1 | grep -i "clip"
```

Press `Ctrl+C` to stop watching logs.

---

## Part 2: Checking System Status

Use these commands to see if everything is running correctly.

### Check all container statuses

```bash
docker-compose ps
```

All services should show `running` status.

### Check if Flink job is running

```bash
docker exec streamscout-flink-jobmanager flink list
```

Should show "Clip Detector Job (RUNNING)".

### Check recent clips in the database

```bash
docker exec streamscout-postgres psql -U twitch -d twitch -c "SELECT clip_id, broadcaster_id, created_at FROM clips ORDER BY created_at DESC LIMIT 5;"
```

### Check API is returning clips

```bash
curl -s "http://localhost:5000/v1.0/clip?limit=5" | python3 -m json.tool
```

---

## Part 3: Restarting Individual Components

Use these sections when a specific component fails.

---

### Restarting: Postgres (Database)

**When to restart:** Database connection errors, queries timing out.

**Step 1: Restart the container**
```bash
docker-compose restart postgres
```

**Step 2: Wait for it to be healthy (about 10 seconds)**
```bash
docker-compose ps postgres
```

Should show `running` status.

**Step 3: Verify it's working**
```bash
docker exec streamscout-postgres psql -U twitch -d twitch -c "SELECT 1;"
```

Should show:
```
 ?column?
----------
        1
```

---

### Restarting: Redis (Cache)

**When to restart:** Redis connection errors in stream monitoring logs.

**Step 1: Restart the container**
```bash
docker-compose restart redis
```

**Step 2: Verify it's working**
```bash
docker exec streamscout-redis redis-cli ping
```

Should show:
```
PONG
```

---

### Restarting: Kafka (Message Queue)

**When to restart:** Kafka connection errors, messages not flowing.

**Step 1: Restart Kafka**
```bash
docker-compose restart kafka
```

**Step 2: Wait for it to be healthy (about 30 seconds)**
```bash
docker-compose ps kafka
```

Should show `running` status.

**Step 3: Verify it's working**
```bash
docker exec streamscout-kafka kafka-topics --bootstrap-server localhost:9092 --list
```

Should show:
```
chat-messages
stream-lifecycle
```

**Step 4: After restarting Kafka, you MUST restart these services:**
```bash
docker-compose restart stream-monitoring
docker-compose restart flink-jobmanager flink-taskmanager
```

Then re-submit the Flink job (see "Restarting: Flink Job" below).

---

### Restarting: Stream Monitoring Service

**When to restart:** Not joining chat rooms, Twitch API errors, websocket connection failures.

**Step 1: Check current logs to see what's wrong**
```bash
docker logs streamscout-stream-monitoring --tail 20
```

**Step 2: Restart the container**
```bash
docker-compose restart stream-monitoring
```

**Step 3: Watch the logs to verify it's working**
```bash
docker logs -f streamscout-stream-monitoring 2>&1 | head -50
```

You should see:
```
... "message": "Stream Monitoring Service started"
... "message": "Polling for top streams"
... "message": "Joined chat room", "channel": "...", "reason": "entered top 5"
```

Press `Ctrl+C` to stop watching.

---

### Restarting: Flink Job (Clip Detector)

**When to restart:** No clips being created, job shows as FAILED, TaskManager heartbeat timeouts.

**Step 1: Check the current job status**
```bash
docker exec streamscout-flink-jobmanager flink list
```

**Step 2: If a job is running but broken, cancel it first**

Get the job ID from the list output (it's the long string of letters and numbers), then:
```bash
docker exec streamscout-flink-jobmanager flink cancel <JOB_ID>
```

Replace `<JOB_ID>` with the actual ID, for example:
```bash
docker exec streamscout-flink-jobmanager flink cancel e56f5ff6db31864ccb2c90700f06b70f
```

**Step 3: Restart both Flink containers**
```bash
docker-compose restart flink-jobmanager flink-taskmanager
```

**Step 4: Wait for containers to be ready (about 30 seconds)**
```bash
docker-compose ps flink-jobmanager flink-taskmanager
```

Both should show `running` status.

**Step 5: Check if the job auto-started**
```bash
docker exec streamscout-flink-jobmanager flink list
```

**Step 6: If no job is running, submit it manually**
```bash
docker exec streamscout-flink-jobmanager flink run -py /opt/flink/usrlib/clip_detector_job.py -d
```

You should see:
```
Job has been submitted with JobID xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

**Step 7: Verify the job is running**
```bash
docker exec streamscout-flink-jobmanager flink list
```

Should show "Clip Detector Job (RUNNING)".

**Step 8: Watch TaskManager logs to verify it's processing**
```bash
docker logs -f streamscout-flink-taskmanager 2>&1 | grep -i -E "(token|kafka|baseline)" | head -20
```

You should see token validation succeeded and Kafka consumer starting.

---

### Restarting: API & Frontend Service

**When to restart:** API not responding, 500 errors, frontend not loading.

**Step 1: Restart the container**
```bash
docker-compose restart api-frontend
```

**Step 2: Verify it's working**
```bash
curl http://localhost:5000/health
```

Should show:
```json
{"status":"healthy"}
```

**Step 3: Test the API endpoint**
```bash
curl -s "http://localhost:5000/v1.0/clip?limit=1" | python3 -m json.tool
```

Should return clip data (or empty array if no clips yet).

---

### Restarting: Prometheus (Metrics)

**When to restart:** Metrics not showing in Grafana.

**Step 1: Restart the container**
```bash
docker-compose restart prometheus
```

**Step 2: Verify it's working**

Open http://localhost:9090 in your browser. You should see the Prometheus web interface.

---

### Restarting: Grafana (Dashboards)

**When to restart:** Dashboards not loading, login issues.

**Step 1: Restart the container**
```bash
docker-compose restart grafana
```

**Step 2: Verify it's working**

Open http://localhost:3000 in your browser. Login with `admin` / `admin`.

---

### Restarting: Loki & Promtail (Logs)

**When to restart:** Logs not appearing in Grafana.

**Step 1: Restart both containers**
```bash
docker-compose restart loki promtail
```

**Step 2: Verify Loki is working**
```bash
curl http://localhost:3100/ready
```

Should show:
```
ready
```

---

## Part 4: Complete System Restart

Use this when everything is broken and you want to start over.

### Step 1: Stop everything
```bash
docker-compose down
```

### Step 2: Start everything
```bash
docker-compose up -d
```

### Step 3: Wait 60 seconds for all services to initialize
```bash
sleep 60
```

### Step 4: Check all services are running
```bash
docker-compose ps
```

### Step 5: Check Flink job is running
```bash
docker exec streamscout-flink-jobmanager flink list
```

If no job is running:
```bash
docker exec streamscout-flink-jobmanager flink run -py /opt/flink/usrlib/clip_detector_job.py -d
```

### Step 6: Verify the system end-to-end
```bash
curl http://localhost:5000/health
```

---

## Part 5: Complete Shutdown

Use this when you want to stop the entire system.

### Stop all containers (preserves data)
```bash
docker-compose down
```

### Stop all containers AND delete all data (fresh start)
```bash
docker-compose down -v
```

**WARNING:** The `-v` flag deletes all database data, clips, and metrics history. Only use this if you want to completely reset.

---

## Part 6: Viewing Logs

### View logs for a specific service
```bash
docker logs streamscout-<service-name>
```

Replace `<service-name>` with one of:
- `postgres`
- `redis`
- `kafka`
- `stream-monitoring`
- `flink-jobmanager`
- `flink-taskmanager`
- `api-frontend`
- `prometheus`
- `grafana`
- `loki`
- `promtail`
- `node-exporter`

### View last 50 lines of logs
```bash
docker logs streamscout-stream-monitoring --tail 50
```

### Follow logs in real-time (live view)
```bash
docker logs -f streamscout-stream-monitoring
```

Press `Ctrl+C` to stop following.

### View logs with timestamps
```bash
docker logs -t streamscout-stream-monitoring --tail 20
```

---

## Part 7: Common Problems and Solutions

### Problem: "No running jobs" in Flink

**Solution:** Submit the job manually:
```bash
docker exec streamscout-flink-jobmanager flink run -py /opt/flink/usrlib/clip_detector_job.py -d
```

---

### Problem: Flink job keeps failing with "heartbeat timeout"

**Solution:** Restart both Flink containers:
```bash
docker-compose restart flink-jobmanager flink-taskmanager
```

Wait 30 seconds, then submit the job:
```bash
docker exec streamscout-flink-jobmanager flink run -py /opt/flink/usrlib/clip_detector_job.py -d
```

---

### Problem: "Token file not found" error in Flink logs

**Solution:** Make sure the tokens file exists:
```bash
ls -la ./secrets/twitch_user_tokens.json
```

If it doesn't exist, generate tokens:
```bash
python seed_twitch_tokens.py
```

Then restart Flink:
```bash
docker-compose restart flink-jobmanager flink-taskmanager
```

---

### Problem: Stream monitoring not joining any chat rooms

**Solution:** Check if the service can reach Twitch:
```bash
docker logs streamscout-stream-monitoring --tail 30
```

If you see authentication errors, your Twitch tokens may have expired. Regenerate them:
```bash
python seed_twitch_tokens.py
```

Then restart:
```bash
docker-compose restart stream-monitoring
```

---

### Problem: No clips appearing even after 5+ minutes

**Possible causes:**

1. **Flink job not running** - Check with `docker exec streamscout-flink-jobmanager flink list`

2. **No messages in Kafka** - Check with:
   ```bash
   docker exec streamscout-kafka kafka-console-consumer --bootstrap-server localhost:9092 --topic chat-messages --max-messages 3 --timeout-ms 10000
   ```

3. **Stream monitoring not sending messages** - Check logs:
   ```bash
   docker logs streamscout-stream-monitoring --tail 30
   ```

4. **Baseline still building** - The Flink job needs 5 minutes of data before detecting anomalies. Check TaskManager logs:
   ```bash
   docker logs streamscout-flink-taskmanager --tail 50 2>&1 | grep -i baseline
   ```

---

### Problem: "403 Forbidden - User not authorized to create clips"

**This is normal!** Some streamers have disabled clip creation. The system will successfully create clips for streamers who allow it.

---

### Problem: API returns empty clips array

**Possible causes:**

1. **No clips created yet** - Wait for anomalies to be detected (may take 5+ minutes after startup)

2. **Database connection issue** - Restart API:
   ```bash
   docker-compose restart api-frontend
   ```

3. **Check if clips exist in database:**
   ```bash
   docker exec streamscout-postgres psql -U twitch -d twitch -c "SELECT COUNT(*) FROM clips;"
   ```

---

## Quick Reference: Service URLs

| Service | URL |
|---------|-----|
| Frontend / API | http://localhost:5000 |
| Flink Web UI | http://localhost:8081 |
| Grafana | http://localhost:3000 |
| Prometheus | http://localhost:9090 |

---

## Quick Reference: Key Commands

| Action | Command |
|--------|---------|
| Start everything | `docker-compose up -d` |
| Stop everything | `docker-compose down` |
| Check status | `docker-compose ps` |
| Check Flink job | `docker exec streamscout-flink-jobmanager flink list` |
| Submit Flink job | `docker exec streamscout-flink-jobmanager flink run -py /opt/flink/usrlib/clip_detector_job.py -d` |
| View service logs | `docker logs streamscout-<service-name>` |
| Restart a service | `docker-compose restart <service-name>` |
