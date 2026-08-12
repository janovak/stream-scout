# Stream Scout — Quick Restart

## Restart everything

```bash
cd ~/stream-scout
./start.sh
```

Wait about 80 seconds. The script stops all containers, starts them again, and submits the Flink job.

## Check for a duplicate Flink job

Run this after the script ends:

```bash
docker exec streamscout-flink-jobmanager flink list
```

Look for two "Clip Detector Job" entries. If you see two, cancel the older job:

```bash
docker exec streamscout-flink-jobmanager flink cancel <older-job-id>
```

**Why this happens:** the script does not check for a recovered job before it submits a new one. A recovered job appears after a restart if the jobmanager kept a checkpoint from before.

## Things start.sh does not control

- **Postgres and Redis run on a different machine** (Tailscale host `streamer-summaries-api`, 100.112.97.111). The script does not start, stop, or check them. Local containers named `postgres16` and `redis` also run on this machine, but they belong to a different project — Stream Scout does not use them.
- **A code change to `stream_monitoring_service.py` needs only a container restart** (bind mount): `docker compose restart stream-monitoring`.
- **A change under `services/stream-monitoring/patches/` needs a rebuild first:** `docker compose build stream-monitoring`, then `docker compose up -d`.

## Check system health

```bash
curl http://localhost:5000/health
docker exec streamscout-flink-jobmanager flink list
```

## URLs

| Service | URL |
|---|---|
| Frontend / API | http://localhost:5000 |
| Flink UI | http://localhost:8081 |
| Grafana | http://localhost:3000 (admin/admin) |

For per-service restart steps and troubleshooting, see `OPERATIONS.md`.
