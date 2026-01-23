# Clawdbot Setup Guide

Clawdbot is a personal AI assistant gateway that bridges messaging platforms (WhatsApp, Telegram, Discord) to AI agents like Claude.

## Prerequisites: Clone the Clawdbot Repository

The Docker image must be built locally from the clawdbot source code. Clone it to `/opt/clawdbot`:

```bash
git clone https://github.com/clawdbot/clawdbot.git /opt/clawdbot
```

This path is referenced in `.env` as `CLAWDBOT_REPO_PATH`. Keep it consistent to make upgrades easy.

## Security Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                         INTERNET                                    │
└─────────────────────────────────────────────────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────────┐
│  LAYER 1: Traefik Reverse Proxy                                     │
│  • HTTPS only (TLS via Let's Encrypt)                               │
│  • No direct port exposure to internet                              │
│  • Routes clawdbot.timkley.dev → gateway:18789                      │
└─────────────────────────────────────────────────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────────┐
│  LAYER 2: Gateway Token Authentication                              │
│  • WebSocket clients must send token in connect handshake           │
│  • Control UI requires token to access                              │
│  • Token set via CLAWDBOT_GATEWAY_TOKEN env var                     │
└─────────────────────────────────────────────────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────────┐
│  LAYER 3: Channel-Level Access Control (WhatsApp)                   │
│  • dmPolicy: "pairing" → new contacts need approval                 │
│  • allowFrom: ["+49..."] → whitelist specific numbers               │
│  • groups.requireMention → only respond when @mentioned             │
└─────────────────────────────────────────────────────────────────────┘
```

## Quick Start

### Step 1: Configure Environment

```bash
cd clawdbot
cp .env.example .env
```

Edit `.env` if you need to customize paths (defaults should work).

### Step 2: Create Persistent Directories

```bash
mkdir -p config workspace
chown -R 1000:1000 config workspace
```

### Step 3: Create Gateway Config

Create `config/clawdbot.json`:

```json
{
  "gateway": {
    "mode": "local",
    "port": 18789,
    "auth": {
      "token": "YOUR_TOKEN_HERE"
    },
    "controlUi": {
      "enabled": true
    }
  },
  "agents": {
    "defaults": {
      "workspace": "/home/node/clawd"
    }
  }
}
```

Generate a secure token with `openssl rand -base64 32` and replace `YOUR_TOKEN_HERE`.

### Step 5: Build and Start the Gateway

```bash
# Build the image from the clawdbot repo
docker compose build gateway

# Start the gateway
docker compose up -d gateway
docker compose logs -f gateway
```

Access Control UI at `https://clawdbot.timkley.dev` (requires your token).

### Step 6: Set Up Claude Code Authentication

```bash
docker compose run --rm cli bash

# Inside container:
claude setup-token

# Complete the OAuth flow in your browser, then verify:
clawdbot models status
exit
```

#### Alternative: Anthropic API Key

If `setup-token` doesn't work, use an API key instead.

**Option A:** Add to `config/clawdbot.json`:

```json
{
  "models": {
    "providers": {
      "anthropic": {
        "apiKey": "sk-ant-..."
      }
    }
  }
}
```

**Option B:** Add environment variable to `.env`:

```bash
ANTHROPIC_API_KEY=sk-ant-...
```

Then add to docker-compose.yml gateway environment:

```yaml
environment:
  - ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}
```

### Step 7: Link WhatsApp

```bash
docker compose run --rm cli channels login
```

Scan the QR code with: **WhatsApp → Settings → Linked Devices → Link a Device**

### Step 8: Configure WhatsApp Access Control

Add channels config to `config/clawdbot.json`:

```json
{
  "gateway": { ... },
  "agents": { ... },
  "channels": {
    "whatsapp": {
      "dmPolicy": "pairing",
      "allowFrom": ["+491234567890"],
      "groups": {
        "*": {
          "requireMention": true
        }
      }
    }
  }
}
```

**Options explained:**

| Setting | Effect |
|---------|--------|
| `dmPolicy: "pairing"` | Unknown contacts get a pairing code to approve |
| `allowFrom: ["+49..."]` | Only these numbers can message (skip pairing) |
| `groups.*.requireMention` | In groups, bot only responds when @mentioned |

### Step 9: Restart and Test

```bash
docker compose restart gateway
```

**Test:**

1. Send a WhatsApp message to your linked number
2. If not in `allowFrom`, you'll get a pairing code → approve in Control UI
3. Chat with Claude via WhatsApp!

## Adding More Channels

### Telegram

```bash
docker compose run --rm cli channels add --channel telegram --token "BOT_TOKEN"
```

### Discord

```bash
docker compose run --rm cli channels add --channel discord --token "BOT_TOKEN"
```

## Verification Checklist

- [ ] Container running: `docker compose ps` shows gateway healthy
- [ ] Traefik routing: `https://clawdbot.timkley.dev` loads (prompts for token)
- [ ] Health check: `docker compose exec gateway node dist/index.js health --token "$CLAWDBOT_GATEWAY_TOKEN"`
- [ ] WhatsApp linked: `docker compose run --rm cli channels status`
- [ ] End-to-end test: Send WhatsApp message, receive AI response

## Troubleshooting

### View Logs

```bash
docker compose logs -f gateway
```

### Check Container Status

```bash
docker compose ps
docker compose run --rm cli clawdbot doctor
```

### Restart Services

```bash
docker compose restart gateway
```

## Upgrading Clawdbot

To update to the latest version, pull the repo, rebuild the image, and restart:

```bash
cd /opt/clawdbot && git pull && cd - && docker compose build --no-cache gateway && docker compose up -d gateway
```

Or step by step:

```bash
# 1. Update the source code
cd /opt/clawdbot
git pull

# 2. Rebuild the Docker image
cd /path/to/infrastructure/clawdbot
docker compose build --no-cache gateway

# 3. Restart with new image
docker compose up -d gateway

# 4. Verify
docker compose logs -f gateway
```
