# Stream Scout — Quick Restart

## Restart everything

```bash
cd ~/stream-scout
./start.sh
```

This usually takes under a minute. The script rebuilds the Flink images, stops all containers, starts them again, waits for the JobManager to answer, submits the Flink job, and waits for the job to reach RUNNING. It can take longer if the Flink images need a real rebuild, or if the JobManager or the job itself is slow to start.

The script's exit code reflects whether the job actually reached RUNNING, not just whether the containers started. A script or cron job that checks `$?` sees a real failure signal, not just "the containers came up."

## Check the Flink job

Run this after the script ends:

```bash
docker exec streamscout-flink-jobmanager flink list
```

This should show exactly one "Clip Detector Job (RUNNING)". If you see none, submit the job by hand. See "Flink job" in `OPERATIONS.md`. If you see two, cancel one:

```bash
docker exec streamscout-flink-jobmanager flink cancel <JOB_ID>
```

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
