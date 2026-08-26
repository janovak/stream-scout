# Stream Scout — Quick Restart

## Restart everything

```bash
cd ~/stream-scout
./start.sh
```

This usually takes under a minute. The script stops all containers, starts them again, and waits for every health-checked container (including flink-jobmanager) to report healthy. It can take longer if a container is slow to start.

## Check the Flink job

```bash
docker exec streamscout-flink-jobmanager flink list
```

Should show exactly one "Clip Detector Job (RUNNING)". The flink-jobmanager container submits this job itself on startup — `start.sh` does not. If you see none, submission failed; check `docker logs streamscout-flink-jobmanager` for an `ERROR: job submission failed` line, fix the cause, and submit by hand:

```bash
docker exec streamscout-flink-jobmanager sh -c 'flink run -py /opt/flink/usrlib/clip_detector_job.py -pyFiles "$FLINK_PYFILES" -d'
```

If you see two, do not assume the newer or the older one is the broken one — check http://localhost:8081 instead. See "Confirm the Flink job is running" in `OPERATIONS.md` Part 1 for the full guidance.

## Things start.sh does not control

- **Postgres and Redis run on a different machine** (Tailscale host `streamer-summaries-api`, 100.112.97.111). The script does not start, stop, or check them. Local containers named `postgres16` and `redis` also run on this machine, but they belong to a different project — Stream Scout does not use them.
- **A code change to `stream_monitoring_service.py` needs only a container restart** (bind mount): `docker compose restart stream-monitoring`.
- **A change under `services/stream-monitoring/patches/` needs a rebuild first:** `docker compose build stream-monitoring`, then `docker compose up -d`.
- **A change to `docker-entrypoint-job.sh`, the Flink Dockerfile, or `flink-conf.yaml` needs a rebuild first** (these are baked into the image, not bind-mounted): `docker compose build flink-jobmanager flink-taskmanager`, then `./start.sh`. The four Flink `.py` job files and `secrets/` are bind-mounted, so those need only a restart.

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
