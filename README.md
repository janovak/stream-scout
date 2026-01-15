# Stream Scout

Automatically captures highlight moments from Twitch streams by detecting chat activity spikes.

## What It Does

When something exciting happens on a Twitch stream, chat goes crazy. Stream Scout watches for these spikes and automatically creates clips before the moment is gone.

The system monitors top live streamers, tracks chat message frequency, and when activity jumps significantly above the baseline, it triggers Twitch's clip API to capture the last 30 seconds.

## How It Works

```
Twitch Chat  →  Kafka  →  Flink  →  Clip Created  →  Web UI
```

**Stream Monitoring Service** connects to Twitch chat for popular streamers and pushes every message to Kafka.

**Flink Job** consumes those messages and runs anomaly detection. It keeps a rolling baseline of "normal" chat activity per stream and flags when the current rate exceeds that baseline by a statistically significant amount. There's a cooldown between detections so you don't get 10 clips of the same moment.

**API & Frontend** serves up the clips through a simple web interface where you can browse what's been captured.

## Running It

Needs Docker and Twitch API credentials. Run `python seed_twitch_tokens.py` once to authorize clip creation, then `docker-compose up -d` starts everything.

The web UI is at port 5000, Flink dashboard at 8081, and Grafana at 3000 if you want to poke around.
