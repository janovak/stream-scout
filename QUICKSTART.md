# Stream Scout — Quick Restart

## Restart everything

```bash
cd ~/stream-scout
./start.sh
```

This usually takes under a minute. The script stops all containers and starts them again, then waits for every health-checked container (including flink-jobmanager) to report healthy. It can take longer if a container is slow to start.

## Check the Flink job

```bash
docker exec streamscout-flink-jobmanager flink list
```

Should show exactly one "Clip Detector Job (RUNNING)". The Flink job runs under Application Mode: there is no separate submission step, ever. Starting the flink-jobmanager container **is** starting the job — if you see none, the job failed to start, which means flink-jobmanager itself is not healthy. Check `docker logs streamscout-flink-jobmanager` for the error, fix it, then restart:

```bash
docker compose restart flink-jobmanager
```

There is no "two jobs" case to worry about here. Restarting the job restarts the whole container, so there is never a second one left running alongside it.

## Things start.sh does not control

- **Postgres and Redis run on a different machine** (Tailscale host `streamer-summaries-api`, 100.112.97.111). The script does not start, stop, or check them. Local containers named `postgres16` and `redis` also run on this machine, but they belong to a different project — Stream Scout does not use them.
- **A code change to a bind-mounted service file needs `--force-recreate`, not `restart`.** The four stream-monitoring modules (`stream_monitoring_service.py`, `reconciler.py`, `eventsub_pool.py`, `token_manager.py`) are bind-mounted **one file at a time**, and a single-file bind mount follows the **inode**. `git checkout`, `sed -i`, and any editor that writes-then-renames all replace the inode, so the container keeps running the old code and `docker compose restart` will not fix it:
  ```bash
  docker compose up -d --force-recreate stream-monitoring
  ```
  Editing the file in place is the only case a plain restart picks up, and it is not worth remembering the difference. Use `--force-recreate`. This has already cost hours once — see OPERATIONS.md, "Note on image rebuilds".
- **A change to `docker-entrypoint-job.sh`, the Flink Dockerfile, or `flink-conf.yaml` needs a rebuild first** (these are baked into the image, not bind-mounted): `docker compose build flink-jobmanager flink-taskmanager`, then `./start.sh`. The four Flink `.py` job files and `secrets/` are bind-mounted, so those need only a restart.

## Check the chat transport

Chat arrives over **EventSub websockets**, not IRC. The reconciler that keeps
the subscriptions matching the wanted channel set runs as an asyncio task
**inside** the `stream-monitoring` container — there is no separate service to
start or check. One line tells you whether it is working:

```bash
curl -s http://localhost:9100/metrics | grep -E '^(eventsub_subscription_count|reconcile_last_success_timestamp)'
```

`eventsub_subscription_count` should equal the wanted channel count, and
`reconcile_last_success_timestamp` should be within a few seconds of now. A
timestamp that stops advancing while polls keep working means the reconciler is
stalled. See OPERATIONS.md, "Reading the reconciler metrics".

## Check system health

```bash
curl http://localhost:5000/health
curl -s http://localhost:9100/metrics | head
docker exec streamscout-flink-jobmanager flink list
```

## URLs

| Service | URL |
|---|---|
| Frontend / API | http://localhost:5000 |
| Flink UI | http://localhost:8081 |
| Grafana | http://localhost:3000 (admin/admin) |
| Stream-monitoring health | http://localhost:8080/health |
| Stream-monitoring metrics | http://localhost:9100/metrics |

For per-service restart steps and troubleshooting, see `OPERATIONS.md`.
