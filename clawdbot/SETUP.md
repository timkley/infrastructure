# Clawdbot VPS Deployment

Deploy clawdbot on your VPS with Traefik reverse proxy, accessible at `clawdbot.timkley.dev`.

## Prerequisites

- SSH access to your VPS
- Git installed on VPS
- Docker and Docker Compose installed
- Traefik running with the `web` network

## Installation Steps

### Step 1: Clone Clawdbot Source Repository

```bash
cd /opt
git clone https://github.com/clawdbot/clawdbot.git
```

### Step 2: Pull Infrastructure Repo

The infrastructure repo should already be at `/home/admin/docker`. If not:

```bash
cd /home/admin/docker
git pull
```

### Step 3: Create Data Directories

```bash
cd /home/admin/docker/clawdbot
mkdir -p data/.clawdbot data/clawd
```

### Step 4: Set Permissions

The clawdbot container runs as uid 1000. Set ownership:

```bash
chown -R 1000:1000 data/
```

### Step 5: Create Environment File

```bash
cp .env.example .env

# Generate and set tokens
GATEWAY_TOKEN=$(openssl rand -hex 32)
KEYRING_PASS=$(openssl rand -hex 32)

sed -i "s/your_gateway_token_here/$GATEWAY_TOKEN/" .env
sed -i "s/your_keyring_password_here/$KEYRING_PASS/" .env

# Save the gateway token - you'll need it for authentication
echo "Gateway token: $GATEWAY_TOKEN"
```

### Step 6: Build Docker Image

```bash
docker compose build
```

### Step 7: Run Initial Setup (Onboard Wizard)

Before starting the gateway, run the onboard wizard interactively:

```bash
docker compose run --rm gateway onboard
```

Follow the wizard to configure:
- WhatsApp connection (scan QR code)
- Gmail OAuth (if needed)
- Other integrations

### Step 8: Start the Gateway

```bash
docker compose up -d
```

### Step 9: DNS Configuration

Ensure `clawdbot.timkley.dev` points to your VPS IP (A record).

### Step 10: Verify Deployment

1. Check container is running:
   ```bash
   docker compose ps
   docker compose logs -f gateway
   ```

2. Access UI at: `https://clawdbot.timkley.dev`
   - Use the gateway token from Step 5 for authentication

## Directory Structure After Setup

```
/opt/clawdbot/                    # Clawdbot source repository (for building)
/home/admin/docker/clawdbot/      # Deployment configuration
├── docker-compose.yml
├── .env                          # Generated from .env.example
├── .env.example
├── .gitignore
├── SETUP.md
└── data/                         # Persistent data (gitignored)
    ├── .clawdbot/                # Config, tokens, OAuth profiles
    └── clawd/                    # Agent workspace
```

## Verification Checklist

- [ ] Container running: `docker compose ps`
- [ ] UI accessible: `curl -I https://clawdbot.timkley.dev`
- [ ] Logs clean: `docker compose logs gateway`
- [ ] WhatsApp connected: Check UI status

## Updating Clawdbot

To update to the latest version:

```bash
cd /opt/clawdbot && git pull
cd /home/admin/docker/clawdbot && docker compose build && docker compose up -d
```

## Notes

- Gateway token is required for all UI access
- Persistent data lives in `./data/` (not `/root/`)
- Traefik handles HTTPS termination automatically
- Container restarts automatically unless manually stopped
