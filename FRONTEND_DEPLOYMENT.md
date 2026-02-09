# StreamScout Frontend Deployment Guide

This guide sets up StreamScout frontend on a reliable, always-on server (Machine A) with Postgres database and public internet access via Cloudflare.

**What gets deployed:**
- API Frontend (Flask app serving clips and static assets)
- Postgres database (stores clips)

**Target:** One machine (Oracle Cloud VM or similar) running Ubuntu 22.04

---

## Architecture Overview

```
                         INTERNET
                            |
                   Cloudflare Tunnel
                            |
                    streamscout.dev
                            |
              Machine A (Oracle Cloud)
              100.x.x.x (Tailscale IP)
                            |
                   +--------+--------+
                   |                 |
             API Frontend       Postgres
             (Flask, port 5000) (port 5432)
```

---

## Prerequisites

Before starting, you need:

- **Machine A:** Ubuntu 22.04 VM (1GB+ RAM) — Oracle Cloud, Linode, DigitalOcean, etc.
  - SSH access as `ubuntu` user with sudo privileges
  - Internet connectivity (both incoming and outgoing)
- **Domain:** `streamscout.dev` (or your domain)
- **Cloudflare Account:** Free tier (https://dash.cloudflare.com)
- **Tailscale Account:** Free tier (https://tailscale.com) — optional but recommended for secure access
- **GitHub access:** To clone the repository
- **Twitch API credentials:** Client ID and Client Secret

---

# Part 1: Domain Setup (Cloudflare)

## Step 1.1: Buy the Domain

1. Go to https://www.cloudflare.com/products/registrar/
2. Search for `streamscout.dev`
3. Purchase the domain (~$10-12/year for .dev)

## Step 1.2: Verify Domain is Active

1. Go to Cloudflare Dashboard: https://dash.cloudflare.com
2. Click on `streamscout.dev`
3. Note the nameservers Cloudflare assigns
4. Update your registrar's nameserver settings to point to Cloudflare (if purchased elsewhere)
5. Wait 5-10 minutes for DNS propagation

---

# Part 2: Tailscale Setup (Optional but Recommended)

Tailscale provides secure, encrypted private network access for management. If skipping this, you'll use public IPs instead.

Do these steps on BOTH your local machine AND Machine A.

## Step 2.1: Install Tailscale

On Machine A (via SSH):

```bash
curl -fsSL https://tailscale.com/install.sh | sh
```

## Step 2.2: Start Tailscale

On Machine A:

```bash
sudo tailscale up
```

On your local machine:
```bash
tailscale up
```

Both will print a URL. Open each URL in your browser to authenticate with your Tailscale account.

## Step 2.3: Note the IPs

On Machine A:

```bash
tailscale ip -4
```

Example output: `100.x.x.x`

Note this down — you'll use it for management.

---

# Part 3: Machine A Setup (Frontend Services)

SSH into Machine A as the `ubuntu` user.

## Step 3.1: Install Docker

```bash
# Update system
sudo apt update && sudo apt upgrade -y

# Install Docker
curl -fsSL https://get.docker.com | sh

# Add ubuntu user to docker group
sudo usermod -aG docker ubuntu

# Install Docker Compose
sudo apt install docker-compose-plugin -y

# Log out and back in for group changes to take effect
exit
```

SSH back in.

## Step 3.2: Clone Repository

```bash
cd ~
git clone https://github.com/janovak/stream-scout.git
cd stream-scout
```

## Step 3.3: Create Environment File

```bash
cat > .env << 'EOF'
TWITCH_CLIENT_ID=your_twitch_client_id_here
TWITCH_CLIENT_SECRET=your_twitch_client_secret_here
EOF
```

**Replace the values with your actual Twitch credentials.**

## Step 3.4: Create Frontend Docker Compose File

```bash
cat > docker-compose.frontend.yml << 'EOF'
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
      POSTGRES_URL: "postgresql://twitch:twitch_password@postgres:5432/twitch"
      HTTP_PORT: "5000"
      LOG_LEVEL: "INFO"
    depends_on:
      postgres:
        condition: service_healthy
    restart: unless-stopped

volumes:
  postgres_data:
  redis_data:
EOF
```

## Step 3.5: Start Services

```bash
docker-compose -f docker-compose.frontend.yml up -d
```

Check status:

```bash
docker-compose -f docker-compose.frontend.yml ps
```

All three containers should show `Up` status.

## Step 3.6: Verify Services are Running

Test the API:

```bash
curl http://localhost:5000/health
```

Should return: `{"status": "healthy"}`

Check Postgres is accessible:

```bash
docker-compose -f docker-compose.frontend.yml exec postgres psql -U twitch -d twitch -c "SELECT COUNT(*) FROM clips;"
```

Should return a count (initially 0 if no clips yet).

View logs:

```bash
docker-compose -f docker-compose.frontend.yml logs api-frontend
```

---

# Part 4: Cloudflare Tunnel Setup

Expose the frontend to the public internet via Cloudflare Tunnel.

## Step 4.1: Install Cloudflare Tunnel

On Machine A:

```bash
# Download cloudflared
wget https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64

# Make executable
chmod +x cloudflared-linux-amd64

# Move to PATH
sudo mv cloudflared-linux-amd64 /usr/local/bin/cloudflared

# Verify
cloudflared --version
```

## Step 4.2: Authenticate Cloudflare

```bash
cloudflared tunnel login
```

This will print a URL. Copy it, open in your browser, log in with your Cloudflare account, and authorize the tunnel. You'll get a certificate file saved locally.

## Step 4.3: Create Tunnel

```bash
cloudflared tunnel create streamscout
```

Save the tunnel ID from the output (looks like a UUID).

## Step 4.4: Create Tunnel Config File

```bash
mkdir -p ~/.cloudflared
cat > ~/.cloudflared/config.yml << 'EOF'
tunnel: streamscout
credentials-file: /home/ubuntu/.cloudflared/streamscout.json

ingress:
  - hostname: streamscout.dev
    service: http://localhost:5000
  - hostname: www.streamscout.dev
    service: http://localhost:5000
  - service: http_status:404
EOF
```

## Step 4.5: Create DNS Records in Cloudflare

1. Log in to Cloudflare Dashboard: https://dash.cloudflare.com
2. Select `streamscout.dev`
3. Go to **DNS** tab
4. Add these CNAME records:

| Type | Name | Target | Proxied |
|------|------|--------|---------|
| CNAME | streamscout | streamscout.cfargotunnel.com | Yes (orange) |
| CNAME | www | streamscout.cfargotunnel.com | Yes (orange) |

5. Ensure both are orange (Cloudflare proxied), not gray

## Step 4.6: Start the Tunnel

Option A — Run in foreground (for testing):

```bash
cloudflared tunnel run streamscout
```

Option B — Run as system service (production):

```bash
sudo cloudflared service install
sudo systemctl start cloudflared
sudo systemctl enable cloudflared
```

To check status:

```bash
sudo systemctl status cloudflared
```

## Step 4.7: Test Public Access

From any machine (anywhere on internet):

```bash
curl https://streamscout.dev/health
```

Should return: `{"status": "healthy"}`

Open in browser: https://streamscout.dev

You should see the StreamScout frontend with the header:
```
StreamScout
Automatically capture Twitch highlights from sudden chat spikes.
```

---

# Part 5: Verification Checklist

From anywhere on the internet:

- [ ] https://streamscout.dev loads (HTTPS works)
- [ ] Page shows "StreamScout" header with tagline
- [ ] Intensity filter dropdown is visible and functional
- [ ] No console errors in browser (press F12 to check)

From Machine A:

```bash
# API endpoint works
curl http://localhost:5000/v1.0/clip?min_intensity=5&limit=5

# Should return JSON with clips (empty array if no clips yet)

# Postgres accessible
docker-compose -f docker-compose.frontend.yml exec postgres psql -U twitch -d twitch -c "SELECT * FROM clips LIMIT 5;"

# Health check
curl http://localhost:5000/health
```

---

# Part 6: Service Management

### View Logs

```bash
# Frontend API logs
docker-compose -f docker-compose.frontend.yml logs -f api-frontend

# Cloudflare tunnel logs
sudo journalctl -u cloudflared -f
```

### Stop Services

```bash
docker-compose -f docker-compose.frontend.yml down
sudo systemctl stop cloudflared
```

### Restart Services

```bash
docker-compose -f docker-compose.frontend.yml up -d
sudo systemctl restart cloudflared
```

### Update Services

```bash
# Pull latest code
git pull origin main

# Rebuild and restart
docker-compose -f docker-compose.frontend.yml up -d --build
```

---

# Troubleshooting

## Tunnel says "CNAME does not resolve"

- Wait 5-10 minutes for DNS propagation
- Check Cloudflare DNS settings: both `streamscout` and `www` should be orange (proxied)
- Try `nslookup streamscout.dev` from your machine

## Frontend returns 404

- Check API is running: `curl http://localhost:5000/health`
- Check Postgres is accessible: `docker-compose -f docker-compose.frontend.yml ps`
- View logs: `docker-compose -f docker-compose.frontend.yml logs api-frontend`

## Database connection fails

```bash
# Check Postgres is running
docker-compose -f docker-compose.frontend.yml logs postgres

# Check the connection string
docker-compose -f docker-compose.frontend.yml exec api-frontend env | grep POSTGRES_URL
```

## HTTPS certificate issues

- Cloudflare handles SSL automatically (free)
- If still seeing warnings, clear browser cache and retry
- Check Cloudflare SSL/TLS setting: should be "Flexible" or "Full"

## Clips don't load

- If database is empty, clips won't show (this is normal)
- Clips will appear once the backend (Flink) processes data
- Check database has the `clips` table: `docker-compose -f docker-compose.frontend.yml exec postgres psql -U twitch -d twitch -c "\dt"`

---

# Summary

You now have:
1. ✅ Public domain (streamscout.dev)
2. ✅ Machine A with Docker/Postgres/Frontend API running
3. ✅ Cloudflare Tunnel exposing frontend to internet
4. ✅ Health checks and verification tests passing

The frontend is now live at **https://streamscout.dev**

Ready for next steps (backend deployment, Flink, monitoring, etc.).
