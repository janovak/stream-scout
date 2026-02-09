# Stream Scout - Backend Deployment Guide (Machine B)

This guide sets up the backend processing system on Machine B (home PC/laptop). Machine B handles all data ingestion, stream monitoring, and Flink job processing.

## Architecture

Machine B runs these services:
- **Kafka** — Message broker for chat & stream events
- **Stream Monitoring** — Monitors Twitch streams and publishes chat messages
- **Flink** — Processes streams and detects clips
- **Prometheus** — Metrics collection
- **Grafana** — Dashboards (optional, for monitoring)

Data flows from Kafka → Flink → Postgres (on Machine A) and Redis (on Machine A).

## Prerequisites

### Hardware
- Home PC/laptop with Ubuntu (22.04+ recommended)
- 4GB+ RAM (can run on 2GB but slower)
- 20GB+ free disk space

### Software
- Docker and Docker Compose
- Git
- Curl (for Tailscale installation)

### Network
- Internet connection
- Access to Tailscale (free account)
- Twitch API credentials (from Stream Scout admin)
- Machine A's Tailscale IP address

### Before Starting
Ask for these from the Stream Scout admin:
- Machine A's Tailscale IP (e.g., `100.x.x.x`)
- Twitch API credentials if not already shared

---

# Part 1: Tailscale Network Setup

Tailscale creates a secure connection between your machine and Machine A so services can communicate across the internet.

## Step 1.1: Install Tailscale

```bash
curl -fsSL https://tailscale.com/install.sh | sh
```

## Step 1.2: Start Tailscale

```bash
sudo tailscale up
```

This prints a URL. Open it in your browser and sign in with your Tailscale account.

## Step 1.3: Get Your Machine B IP

```bash
tailscale ip -4
```

Note this IP (e.g., `100.106.110.127`). You'll need it later.

## Step 1.4: Test Connection to Machine A

Ask admin for Machine A's Tailscale IP, then run:

```bash
ping <MACHINE_A_IP>
```

Should succeed with no packet loss.

---

# Part 2: Backend Installation

Perform all steps below on Machine B.

## Step 2.1: Install Docker

```bash
# Update system
sudo apt update && sudo apt upgrade -y

# Install Docker
curl -fsSL https://get.docker.com | sh

# Add your user to docker group
sudo usermod -aG docker $USER

# Install Docker Compose
sudo apt install docker-compose-plugin -y

# Log out and back in for group changes to take effect
exit
```

SSH back in.

## Step 2.2: Clone the Repository

```bash
cd ~
git clone https://github.com/janovak/stream-scout.git
cd stream-scout
```

## Step 2.3: Create Environment File

```bash
cat > .env << 'EOF'
TWITCH_CLIENT_ID=<your-client-id>
TWITCH_CLIENT_SECRET=<your-client-secret>
EOF
```

Replace `<your-client-id>` and `<your-client-secret>` with credentials from the admin.

## Step 2.4: Seed Twitch User Tokens

This authorizes the app to read from your selected Twitch channels.

```bash
# Install Python if needed
sudo apt install python3 python3-full -y

# Create secrets directory
mkdir -p secrets

# Create virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies and run token seeder
pip install requests twitchAPI
python3 seed_twitch_tokens.py
```

A browser window opens. Sign in with your Twitch account and authorize the app.

## Step 2.5: Fix Token File Permissions

```bash
chmod 666 secrets/twitch_user_tokens.json
```

## Step 2.6: Create Docker Compose File

Replace `MACHINE_A_IP` with Machine A's Tailscale IP and `MACHINE_B_IP` with your machine's Tailscale IP:

```bash
cat > docker-compose.processing.yml << 'EOF'
services:
  kafka:
    image: confluentinc/cp-kafka:7.5.0
    container_name: streamscout-kafka
    ports:
      - "9092:9092"
      - "29092:29092"
      - "9093:9093"
      - "9094:9094"
    environment:
      KAFKA_NODE_ID: 1
      KAFKA_PROCESS_ROLES: broker,controller
      KAFKA_CONTROLLER_QUORUM_VOTERS: 1@kafka:9093
      KAFKA_LISTENERS: PLAINTEXT://0.0.0.0:29092,CONTROLLER://0.0.0.0:9093,PLAINTEXT_HOST://0.0.0.0:9092,EXTERNAL://0.0.0.0:9094
      KAFKA_ADVERTISED_LISTENERS: PLAINTEXT://kafka:29092,PLAINTEXT_HOST://localhost:9092,EXTERNAL://MACHINE_B_IP:9094
      KAFKA_LISTENER_SECURITY_PROTOCOL_MAP: CONTROLLER:PLAINTEXT,PLAINTEXT:PLAINTEXT,PLAINTEXT_HOST:PLAINTEXT,EXTERNAL:PLAINTEXT
      KAFKA_CONTROLLER_LISTENER_NAMES: CONTROLLER
      KAFKA_INTER_BROKER_LISTENER_NAME: PLAINTEXT
      KAFKA_OFFSETS_TOPIC_REPLICATION_FACTOR: 1
      KAFKA_TRANSACTION_STATE_LOG_MIN_ISR: 1
      KAFKA_TRANSACTION_STATE_LOG_REPLICATION_FACTOR: 1
      KAFKA_AUTO_CREATE_TOPICS_ENABLE: "false"
      CLUSTER_ID: MkU3OEVBNTcwNTJENDM2Qk
    volumes:
      - kafka_data:/var/lib/kafka/data
    healthcheck:
      test: ["CMD", "kafka-topics", "--bootstrap-server", "localhost:9092", "--list"]
      interval: 10s
      timeout: 10s
      retries: 5
    restart: unless-stopped

  kafka-init:
    image: confluentinc/cp-kafka:7.5.0
    depends_on:
      kafka:
        condition: service_healthy
    entrypoint: ["/bin/sh", "-c"]
    command: |
      "
      kafka-topics --bootstrap-server kafka:29092 --create --if-not-exists --topic chat-messages --partitions 20 --replication-factor 1
      kafka-topics --bootstrap-server kafka:29092 --create --if-not-exists --topic stream-lifecycle --partitions 5 --replication-factor 1
      echo 'Topics created successfully'
      "

  stream-monitoring:
    build:
      context: ./services/stream-monitoring
      dockerfile: Dockerfile
    container_name: streamscout-stream-monitoring
    environment:
      - KAFKA_BROKER_URL=kafka:29092
      - POSTGRES_URL=postgresql://twitch:twitch_password@MACHINE_A_IP:5432/twitch
      - REDIS_URL=redis://MACHINE_A_IP:6379
      - TWITCH_CLIENT_ID=${TWITCH_CLIENT_ID}
      - TWITCH_CLIENT_SECRET=${TWITCH_CLIENT_SECRET}
      - TWITCH_TOKEN_FILE=/app/secrets/twitch_user_tokens.json
      - PROMETHEUS_PORT=9100
      - HEALTH_CHECK_PORT=8080
      - LOG_LEVEL=INFO
    volumes:
      - ./secrets:/app/secrets:rw
      - ./services/stream-monitoring/stream_monitoring_service.py:/app/stream_monitoring_service.py:ro
    depends_on:
      kafka:
        condition: service_healthy
      kafka-init:
        condition: service_completed_successfully
    restart: unless-stopped

  flink-jobmanager:
    build:
      context: ./services/flink-job
      dockerfile: Dockerfile
    container_name: streamscout-flink-jobmanager
    command: /opt/flink/docker-entrypoint-job.sh
    ports:
      - "8081:8081"
    environment:
      - |
        FLINK_PROPERTIES=
        jobmanager.rpc.address: flink-jobmanager
        jobmanager.memory.process.size: 1536m
      - KAFKA_BOOTSTRAP_SERVERS=kafka:29092
      - POSTGRES_HOST=MACHINE_A_IP
      - POSTGRES_PORT=5432
      - POSTGRES_DB=twitch
      - POSTGRES_USER=twitch
      - POSTGRES_PASSWORD=twitch_password
      - TWITCH_CLIENT_ID=${TWITCH_CLIENT_ID}
      - TWITCH_CLIENT_SECRET=${TWITCH_CLIENT_SECRET}
      - TWITCH_TOKEN_FILE=/opt/flink/secrets/twitch_user_tokens.json
    volumes:
      - ./secrets:/opt/flink/secrets:rw
      - ./services/flink-job/clip_detector_job.py:/opt/flink/usrlib/clip_detector_job.py:ro
    depends_on:
      kafka:
        condition: service_healthy
    restart: unless-stopped

  flink-taskmanager:
    build:
      context: ./services/flink-job
      dockerfile: Dockerfile
    container_name: streamscout-flink-taskmanager
    command: taskmanager
    environment:
      - |
        FLINK_PROPERTIES=
        jobmanager.rpc.address: flink-jobmanager
        taskmanager.memory.process.size: 1536m
        taskmanager.numberOfTaskSlots: 4
      - KAFKA_BOOTSTRAP_SERVERS=kafka:29092
      - POSTGRES_HOST=MACHINE_A_IP
      - POSTGRES_PORT=5432
      - POSTGRES_DB=twitch
      - POSTGRES_USER=twitch
      - POSTGRES_PASSWORD=twitch_password
      - TWITCH_CLIENT_ID=${TWITCH_CLIENT_ID}
      - TWITCH_CLIENT_SECRET=${TWITCH_CLIENT_SECRET}
      - TWITCH_TOKEN_FILE=/opt/flink/secrets/twitch_user_tokens.json
    volumes:
      - ./secrets:/opt/flink/secrets:rw
      - ./services/flink-job/clip_detector_job.py:/opt/flink/usrlib/clip_detector_job.py:ro
    depends_on:
      - flink-jobmanager
    restart: unless-stopped

  prometheus:
    image: prom/prometheus:v2.47.0
    container_name: streamscout-prometheus
    ports:
      - "9090:9090"
    volumes:
      - ./configs/prometheus-processing.yml:/etc/prometheus/prometheus.yml:ro
      - prometheus_data:/prometheus
    command:
      - '--config.file=/etc/prometheus/prometheus.yml'
      - '--storage.tsdb.path=/prometheus'
      - '--storage.tsdb.retention.time=15d'
    restart: unless-stopped

  grafana:
    image: grafana/grafana:10.1.0
    container_name: streamscout-grafana
    ports:
      - "3000:3000"
    environment:
      - GF_SECURITY_ADMIN_PASSWORD=admin
      - GF_USERS_ALLOW_SIGN_UP=false
    volumes:
      - grafana_data:/var/lib/grafana
      - ./configs/grafana/provisioning:/etc/grafana/provisioning:ro
    restart: unless-stopped

volumes:
  kafka_data:
  prometheus_data:
  grafana_data:
EOF
```

**IMPORTANT:** Replace in the file:
- `MACHINE_A_IP` — with Machine A's Tailscale IP
- `MACHINE_B_IP` — with your machine's Tailscale IP

Example: If Machine A IP is `100.112.97.111` and Machine B is `100.106.110.127`, replace those exact IPs.

## Step 2.7: Create Prometheus Config

```bash
mkdir -p configs
cat > configs/prometheus-processing.yml << 'EOF'
global:
  scrape_interval: 15s
  evaluation_interval: 15s

scrape_configs:
  - job_name: 'prometheus'
    static_configs:
      - targets: ['localhost:9090']

  - job_name: 'flink'
    static_configs:
      - targets: ['flink-jobmanager:9249', 'flink-taskmanager:9249']

  - job_name: 'stream-monitoring'
    static_configs:
      - targets: ['stream-monitoring:9100']

  - job_name: 'kafka'
    static_configs:
      - targets: ['kafka:9092']
EOF
```

## Step 2.8: Start All Services

```bash
docker compose -f docker-compose.processing.yml up -d
```

## Step 2.9: Verify Services Are Running

```bash
docker compose -f docker-compose.processing.yml ps
```

Check that:
- `streamscout-kafka` — state should be "Up"
- `streamscout-flink-jobmanager` — state should be "Up"
- `streamscout-flink-taskmanager` — state should be "Up"
- `streamscout-stream-monitoring` — state should be "Up"
- `streamscout-prometheus` — state should be "Up"
- `streamscout-grafana` — state should be "Up"

## Step 2.10: Verify Flink Job Is Running

Wait 60 seconds for jobmanager to stabilize, then:

```bash
docker exec streamscout-flink-jobmanager /opt/flink/bin/flink list
```

Should show:
```
Clip Detector Job (RUNNING)
```

---

# Part 3: Verification Checklist

## From Machine B

### Check services are running:
```bash
docker compose -f docker-compose.processing.yml ps
```

All containers should show status "Up".

### Check Kafka has topics:
```bash
docker exec streamscout-kafka kafka-topics --bootstrap-server localhost:9092 --list
```

Should show:
- `chat-messages`
- `stream-lifecycle`

### Check Flink job:
```bash
docker exec streamscout-flink-jobmanager /opt/flink/bin/flink list
```

Should show "Clip Detector Job (RUNNING)".

### Check Prometheus metrics:
```bash
curl http://localhost:9090/api/v1/query?query=up
```

Should return JSON with active targets.

### Check Grafana:
Open http://localhost:3000 in browser.
- Login: admin / admin
- Should see grafana home page

### Verify connection to Machine A:
```bash
ping <MACHINE_A_IP>
```

Should succeed.

Test database connection:
```bash
nc -zv <MACHINE_A_IP> 5432
```

Should succeed (port 5432 open on Machine A).

---

# Part 4: Troubleshooting

## Services Won't Start

Check logs:
```bash
# Kafka
docker logs streamscout-kafka | tail -50

# Flink
docker logs streamscout-flink-jobmanager | tail -50

# Stream monitoring
docker logs streamscout-stream-monitoring | tail -50
```

## Flink Job Fails with Token Error

Tokens are missing or invalid:
1. Re-run seeding: `python3 seed_twitch_tokens.py`
2. Restart jobmanager: `docker compose restart streamscout-flink-jobmanager`

## Can't Connect to Machine A

Verify Tailscale is running:
```bash
tailscale status
```

Test connection:
```bash
ping <MACHINE_A_IP>
```

If it fails:
1. Verify Machine A's IP is correct
2. Ask admin to verify Machine A's Tailscale is running
3. Check your internet connection

## Kafka Won't Start

Clear data and restart:
```bash
docker compose -f docker-compose.processing.yml down
docker volume rm stream-scout_kafka_data
docker compose -f docker-compose.processing.yml up -d
```

## Out of Disk Space

Check usage:
```bash
docker system df
```

Free up space:
```bash
docker system prune
docker volume prune
```

---

# Part 5: Maintenance

## View Logs

Monitor stream detection:
```bash
docker logs -f streamscout-stream-monitoring
```

Monitor Flink job:
```bash
docker logs -f streamscout-flink-taskmanager
```

## Restart Services

Restart all:
```bash
docker compose -f docker-compose.processing.yml restart
```

Restart specific service:
```bash
docker compose -f docker-compose.processing.yml restart streamscout-flink-jobmanager
```

## Update Code

If Stream Scout is updated:
```bash
cd ~/stream-scout
git pull
docker compose -f docker-compose.processing.yml up -d --build
```

## Access Grafana Dashboards

Local (on Machine B):
```
http://localhost:3000
Login: admin / admin
```

From your computer (if on Tailscale):
```
http://<MACHINE_B_IP>:3000
Login: admin / admin
```

## Stop Services

```bash
docker compose -f docker-compose.processing.yml down
```

## Resume Services

```bash
docker compose -f docker-compose.processing.yml up -d
```

Data is preserved; services pick up where they left off.

---

# Part 6: What Happens During Downtime

If Machine B goes offline temporarily:

1. **While offline:** Clips detected on Machine A are stored in database (never lost)
2. **When it comes back online:**
   - Docker auto-restarts all containers
   - Flink job auto-submits (with seeded tokens if still valid)
   - Stream Monitoring reconnects to Kafka
   - Processing resumes from last checkpoint
3. **If offline for days:** Twitch tokens might expire. Re-run seeding if Flink job fails to submit.

---

# Part 7: FAQ

**Q: Can I run this on Windows?**
A: Use Windows Subsystem for Linux (WSL2) with Docker Desktop.

**Q: What if my internet cuts out?**
A: Services gracefully reconnect when internet returns. Kafka stores backlog temporarily.

**Q: How much disk space do I need?**
A: ~50GB recommended (Kafka logs grow over time, shrink when data is consumed).

**Q: Can I move this to a different machine?**
A: Yes. Copy `~/stream-scout` directory and repeat Part 2.

**Q: What if Flink job keeps crashing?**
A: Check logs: `docker logs streamscout-flink-jobmanager | grep -i error`. Most likely: missing tokens or connection to Machine A failed.

**Q: Is Grafana required?**
A: No, it's optional. You can skip it or access it only when troubleshooting.

---

# Getting Help

If something fails:
1. Check logs (see Part 5: Maintenance)
2. Verify Tailscale connection to Machine A
3. Verify `.env` file has correct Twitch credentials
4. Ask admin for Machine A's status (is it running? is Postgres accessible?)
5. Share the logs when asking for help
