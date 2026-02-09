# Stream Scout - Fault-Tolerant Deployment Guide

This guide sets up Stream Scout on two machines with fault tolerance: the frontend stays up even if processing goes down.

## Architecture Overview

```
                         INTERNET
                            |
                   Cloudflare Tunnel
                            |
                    streamscout.dev
                            |
+---------------------------+----------------------------------+
|                    Tailscale Network                         |
|                                                              |
|   +---------------------+      +-------------------------+   |
|   |  MACHINE A (Oracle) |      |  MACHINE B (Home PC)    |   |
|   |  "Always Up"        |      |  "Processing"           |   |
|   |  100.x.x.x          |      |  100.x.x.x              |   |
|   |                     |      |                         |   |
|   |  - API Frontend  <---------+- Flink --------------+  |   |
|   |  - Postgres      <---------+- Kafka               |  |   |
|   |  - Redis            |      |  - Stream Monitoring-+--+-> Twitch
|   |                     |      |  - Prometheus        |  |   |
|   |                     |      |  - Grafana           |  |   |
|   +---------------------+      +-------------------------+   |
+--------------------------------------------------------------+
```

**Fault Tolerance:**
- When Machine B goes down: Frontend keeps serving existing clips
- When Machine B comes back: Flink catches up on any missed data
- Machine A stays up: Users always see the site working

## Prerequisites

- Machine A: Oracle Cloud VM (Ubuntu 22.04, 1GB RAM) - always on
- Machine B: Home PC/laptop with Ubuntu (4GB+ RAM) - may have downtime
- Domain: streamscout.dev
- Cloudflare account (free)
- Tailscale account (free)
- Your Twitch API credentials

---

# Part 1: Domain Setup (Cloudflare)

## Step 1.1: Buy the Domain

1. Go to https://www.cloudflare.com/products/registrar/
2. Search for `streamscout.dev`
3. Purchase the domain (~$10-12/year for .dev)

## Step 1.2: Verify Domain is Active

1. Go to Cloudflare Dashboard: https://dash.cloudflare.com
2. Click on `streamscout.dev`
3. Wait for status to show "Active" (can take a few minutes)

---

# Part 2: Tailscale Setup

Do these steps on BOTH machines.

## Step 2.1: Install Tailscale

SSH into each machine and run:

```bash
curl -fsSL https://tailscale.com/install.sh | sh
```

## Step 2.2: Start Tailscale

```bash
sudo tailscale up
```

This will print a URL. Open it in your browser to authenticate with your Tailscale account.

## Step 2.3: Verify Connection

On each machine, run:

```bash
tailscale ip -4
```

Note down the IPs. Example:
- Machine A: `100.x.x.x` (Oracle - reliable)
- Machine B: `100.x.x.x` (Home PC - processing)

## Step 2.4: Test Connectivity

From Machine A:

```bash
ping <MACHINE_B_IP>
```

From Machine B:

```bash
ping <MACHINE_A_IP>
```

Both should succeed.

---

# Part 3: Machine A Setup (Reliable - Oracle Cloud)

Machine A runs the user-facing services that must always be available.

**Services:** API Frontend, Postgres, Redis (~300MB RAM total)

SSH into Machine A.

## Step 3.1: Install Docker

```bash
# Update system
sudo apt update && sudo apt upgrade -y

# Install Docker
curl -fsSL https://get.docker.com | sh

# Add your user to docker group
sudo usermod -aG docker $USER

# Install Docker Compose
sudo apt install docker-compose-plugin -y

# Log out and back in for group changes
exit
```

SSH back in.

## Step 3.2: Clone the Repository

```bash
cd ~
git clone https://github.com/janovak/stream-scout.git
cd stream-scout
```

## Step 3.3: Create Environment File

```bash
cat > .env << 'EOF'
TWITCH_CLIENT_ID=your_client_id_here
TWITCH_CLIENT_SECRET=your_client_secret_here
EOF
```

Replace with your actual Twitch credentials.

## Step 3.4: Create Machine A Docker Compose

```bash
cat > docker-compose.reliable.yml << 'EOF'
services:
  postgres:
    image: postgres:15-alpine
    container_name: streamscout-postgres
    ports:
      - "5432:5432"
    environment:
      POSTGRES_USER: twitch
      POSTGRES_PASSWORD: twitch_password
      POSTGRES_DB: twitch
    volumes:
      - postgres_data:/var/lib/postgresql/data
      - ./infrastructure/postgres/init.sql:/docker-entrypoint-initdb.d/init.sql:ro
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U twitch"]
      interval: 5s
      timeout: 5s
      retries: 5
    restart: unless-stopped

  redis:
    image: redis:7-alpine
    container_name: streamscout-redis
    ports:
      - "6379:6379"
    volumes:
      - redis_data:/data
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 5s
      timeout: 5s
      retries: 5
    restart: unless-stopped

  api-frontend:
    build:
      context: ./services/api-frontend
      dockerfile: Dockerfile
    container_name: streamscout-api-frontend
    ports:
      - "5000:5000"
    environment:
      - POSTGRES_URL=postgresql://twitch:twitch_password@postgres:5432/twitch
      - HTTP_PORT=5000
      - LOG_LEVEL=INFO
    volumes:
      - ./services/api-frontend/static:/app/static:ro
      - ./services/api-frontend/api_frontend_service.py:/app/api_frontend_service.py:ro
    depends_on:
      postgres:
        condition: service_healthy
    restart: unless-stopped

volumes:
  postgres_data:
  redis_data:
EOF
```

## Step 3.5: Enable Docker on Boot

```bash
sudo systemctl enable docker
```

## Step 3.6: Start Machine A Services

```bash
docker compose -f docker-compose.reliable.yml up -d
```

## Step 3.7: Verify Services

```bash
docker compose -f docker-compose.reliable.yml ps
```

All services should show "running" or "healthy".

---

# Part 4: Machine B Setup (Processing - Home PC)

Machine B runs all data ingestion and processing services.

**Services:** Kafka, Stream Monitoring, Flink, Prometheus, Grafana (~3.5GB RAM)

## Step 4.1: Install Ubuntu on Home PC

If using an old Windows device:
1. Download Ubuntu Server: https://ubuntu.com/download/server
2. Create bootable USB with Rufus or Balena Etcher
3. Boot from USB and install Ubuntu
4. Choose minimal installation

## Step 4.2: Install Docker

SSH into Machine B:

```bash
# Update system
sudo apt update && sudo apt upgrade -y

# Install Docker
curl -fsSL https://get.docker.com | sh

# Add your user to docker group
sudo usermod -aG docker $USER

# Install Docker Compose
sudo apt install docker-compose-plugin -y

# Log out and back in
exit
```

SSH back in.

## Step 4.3: Clone the Repository

```bash
cd ~
git clone https://github.com/janovak/stream-scout.git
cd stream-scout
```

## Step 4.4: Create Environment File

```bash
cat > .env << 'EOF'
TWITCH_CLIENT_ID=your_client_id_here
TWITCH_CLIENT_SECRET=your_client_secret_here
EOF
```

## Step 4.5: Seed Twitch Tokens

```bash
# Install Python if needed
sudo apt install python3 python3-full -y

# Create secrets directory
mkdir -p secrets

# Create virtual environment (required on Ubuntu 24.04+ due to PEP 668)
python3 -m venv venv
source venv/bin/activate

# Install dependencies and run token seeder
pip install requests twitchAPI
python3 seed_twitch_tokens.py
```

Follow the browser prompts to authorize.

## Step 4.6: Fix Token File Permissions

```bash
chmod 666 secrets/twitch_user_tokens.json
```

## Step 4.7: Create Machine B Docker Compose

Replace `MACHINE_A_IP` with Machine A's Tailscale IP and `MACHINE_B_IP` with Machine B's Tailscale IP:

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
      # KRaft mode (no Zookeeper)
      KAFKA_NODE_ID: 1
      KAFKA_PROCESS_ROLES: broker,controller
      KAFKA_CONTROLLER_QUORUM_VOTERS: 1@kafka:9093
      KAFKA_LISTENERS: PLAINTEXT://0.0.0.0:29092,CONTROLLER://0.0.0.0:9093,PLAINTEXT_HOST://0.0.0.0:9092,EXTERNAL://0.0.0.0:9094
      KAFKA_ADVERTISED_LISTENERS: PLAINTEXT://kafka:29092,PLAINTEXT_HOST://localhost:9092,EXTERNAL://MACHINE_B_IP:9094
      KAFKA_LISTENER_SECURITY_PROTOCOL_MAP: CONTROLLER:PLAINTEXT,PLAINTEXT:PLAINTEXT,PLAINTEXT_HOST:PLAINTEXT,EXTERNAL:PLAINTEXT
      KAFKA_CONTROLLER_LISTENER_NAMES: CONTROLLER
      KAFKA_INTER_BROKER_LISTENER_NAME: PLAINTEXT
      # Cluster settings
      KAFKA_OFFSETS_TOPIC_REPLICATION_FACTOR: 1
      KAFKA_TRANSACTION_STATE_LOG_MIN_ISR: 1
      KAFKA_TRANSACTION_STATE_LOG_REPLICATION_FACTOR: 1
      KAFKA_AUTO_CREATE_TOPICS_ENABLE: "false"
      # KRaft cluster ID
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

**IMPORTANT:** Replace all occurrences of:
- `MACHINE_A_IP` with Machine A's Tailscale IP (e.g., `100.x.x.x`)
- `MACHINE_B_IP` with Machine B's Tailscale IP (e.g., `100.x.x.x`)

## Step 4.8: Create Prometheus Config

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

## Step 4.9: Start Machine B Services

```bash
docker compose -f docker-compose.processing.yml up -d
```

## Step 4.10: Verify Services

```bash
docker compose -f docker-compose.processing.yml ps
```

## Step 4.11: Submit Flink Job

Wait 60 seconds for Flink to start, then:

```bash
docker exec streamscout-flink-jobmanager flink run -py /opt/flink/usrlib/clip_detector_job.py -d
```

Verify:

```bash
docker exec streamscout-flink-jobmanager flink list
```

Should show "Clip Detector Job (RUNNING)".

---

# Part 5: Cloudflare Tunnel Setup

Do this on **Machine A** (where the API frontend runs).

## Step 5.1: Install cloudflared

```bash
curl -L --output cloudflared.deb https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb
sudo dpkg -i cloudflared.deb
```

## Step 5.2: Authenticate with Cloudflare

```bash
cloudflared tunnel login
```

This opens a browser. Select your `streamscout.dev` domain.

## Step 5.3: Create the Tunnel

```bash
cloudflared tunnel create streamscout
```

Note the tunnel ID (a UUID like `a1b2c3d4-e5f6-...`).

## Step 5.4: Create Tunnel Config

The config must be in `/etc/cloudflared/` for the systemd service to find it:

```bash
sudo mkdir -p /etc/cloudflared

# Copy credentials file (created during tunnel create)
sudo cp ~/.cloudflared/YOUR_TUNNEL_ID.json /etc/cloudflared/

# Create config file
sudo tee /etc/cloudflared/config.yml << 'EOF'
tunnel: YOUR_TUNNEL_ID
credentials-file: /etc/cloudflared/YOUR_TUNNEL_ID.json

ingress:
  - hostname: www.streamscout.dev
    service: http://localhost:5000
  - hostname: streamscout.dev
    service: http://localhost:5000
  - service: http_status:404
EOF
```

**IMPORTANT:** Replace `YOUR_TUNNEL_ID` with your actual tunnel ID (the UUID shown when you ran `cloudflared tunnel create`).

**Note:** Grafana is on Machine B, so it's not exposed via this tunnel. Access it directly via Machine B's IP:3000 when on Tailscale.

## Step 5.5: Create DNS Records

```bash
cloudflared tunnel route dns streamscout streamscout.dev
cloudflared tunnel route dns streamscout www.streamscout.dev
```

## Step 5.6: Test the Tunnel

```bash
cloudflared tunnel run streamscout
```

Open https://streamscout.dev in your browser. You should see the frontend!

Press Ctrl+C to stop.

## Step 5.7: Run Tunnel as a Service

```bash
sudo cloudflared service install
sudo systemctl start cloudflared
sudo systemctl enable cloudflared
```

Verify:

```bash
sudo systemctl status cloudflared
```

## Step 5.8: Enable HTTPS Redirect in Cloudflare

1. Go to Cloudflare Dashboard: https://dash.cloudflare.com
2. Select `streamscout.dev`
3. Go to **SSL/TLS** -> **Edge Certificates**
4. Enable **"Always Use HTTPS"**

## Step 5.9: Create Apex to www Redirect

1. Go to **Rules** -> **Redirect Rules**
2. Click **"Create rule"**
3. Configure:
   - Rule name: `apex to www`
   - **When incoming requests match...**:
     - Field: `Hostname`
     - Operator: `equals`
     - Value: `streamscout.dev`
   - **Then...**:
     - Type: `Dynamic`
     - Expression: `concat("https://www.streamscout.dev", http.request.uri.path)`
     - Status code: `301`
4. Click **Deploy**

---

# Part 6: Verification Checklist

## From Your Local Machine

- [ ] https://streamscout.dev loads the frontend
- [ ] Clips appear in the frontend after a few minutes

## From Machine A

```bash
# Check all services running
docker compose -f docker-compose.reliable.yml ps

# Check database has clips
docker exec streamscout-postgres psql -U twitch -d twitch \
  -c "SELECT COUNT(*) FROM clips;"
```

## From Machine B

```bash
# Check all services running
docker compose -f docker-compose.processing.yml ps

# Check Kafka has messages
docker exec streamscout-kafka kafka-console-consumer \
  --bootstrap-server localhost:9092 \
  --topic chat-messages \
  --max-messages 3 \
  --timeout-ms 10000

# Check Flink job running
docker exec streamscout-flink-jobmanager flink list
```

## Test Fault Tolerance

1. Stop Machine B: `sudo shutdown now` (or just stop Docker)
2. Verify frontend still works: https://streamscout.dev
3. Start Machine B again
4. Verify Flink job resumes: `docker exec streamscout-flink-jobmanager flink list`

---

# Part 7: Maintenance Commands

## Restart All Services

Machine A:
```bash
cd ~/stream-scout
docker compose -f docker-compose.reliable.yml restart
```

Machine B:
```bash
cd ~/stream-scout
docker compose -f docker-compose.processing.yml restart
```

## View Logs

```bash
# Machine A: API
docker logs -f streamscout-api-frontend

# Machine B: Stream monitoring
docker logs -f streamscout-stream-monitoring

# Machine B: Flink
docker logs -f streamscout-flink-taskmanager
```

## Update Code

On both machines:
```bash
cd ~/stream-scout
git pull
docker compose -f docker-compose.<reliable|processing>.yml up -d --build
```

## Resubmit Flink Job (if it fails)

On Machine B:
```bash
docker exec streamscout-flink-jobmanager flink run -py /opt/flink/usrlib/clip_detector_job.py -d
```

## Access Grafana

Grafana runs on Machine B. Access it via Tailscale:
- From your local machine (with Tailscale installed): `http://MACHINE_B_IP:3000`
- Login: admin / admin

---

# Troubleshooting

## Frontend shows no clips

1. Check Machine B is running
2. Check Flink job: `docker exec streamscout-flink-jobmanager flink list`
3. Check Postgres connection from Machine B: `nc -zv MACHINE_A_IP 5432`

## Kafka won't start

Clear the data and restart:

```bash
docker compose -f docker-compose.processing.yml down
docker volume rm stream-scout_kafka_data
docker compose -f docker-compose.processing.yml up -d
```

## Services can't connect across machines

1. Verify Tailscale is running: `tailscale status`
2. Test connectivity: `ping <OTHER_MACHINE_IP>`
3. Check IPs in docker-compose files match actual Tailscale IPs

## Cloudflare Tunnel not working

```bash
# Check tunnel status
sudo systemctl status cloudflared

# View tunnel logs
sudo journalctl -u cloudflared -f

# Restart tunnel
sudo systemctl restart cloudflared
```

## Token refresh errors

Re-run the token seeder on Machine B:
```bash
cd ~/stream-scout
source venv/bin/activate
python3 seed_twitch_tokens.py
```

## Machine B IP changes

If your home PC gets a new Tailscale IP after reboot:
1. Update `docker-compose.processing.yml` with new IP
2. Restart services: `docker compose -f docker-compose.processing.yml up -d`
