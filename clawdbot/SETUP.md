# Clawdbot Setup Guide

Clawdbot is a personal AI assistant gateway that bridges messaging platforms (WhatsApp, Telegram, Discord) to AI agents like Claude.

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
│  • Token stored in clawdbot.json (not in git)                       │
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

### Step 2: Create Gateway Config

Create `config/clawdbot.json`:

```json
{
  "gateway": {
    "port": 18789,
    "auth": {
      "mode": "token",
      "token": "GENERATE_WITH_openssl_rand_-base64_32"
    },
    "controlUi": {
      "enabled": true
    }
  },
  "agent": {
    "workspace": "/workspace"
  }
}
```

Generate a secure token:

```bash
openssl rand -base64 32
```

### Step 3: Start the Gateway

```bash
docker compose up -d gateway
docker compose logs -f gateway
```

Access Control UI at `https://clawdbot.timkley.dev` (requires your token).

### Step 4: Set Up Claude Code Authentication

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
  "gateway": { ... },
  "models": {
    "providers": {
      "anthropic": {
        "apiKey": "sk-ant-..."
      }
    }
  }
}
```

**Option B:** Add environment variable in `docker-compose.yml`:

```yaml
environment:
  - ANTHROPIC_API_KEY=sk-ant-...
```

### Step 5: Link WhatsApp

```bash
docker compose run --rm cli channels login
```

Scan the QR code with: **WhatsApp → Settings → Linked Devices → Link a Device**

### Step 6: Configure WhatsApp Access Control

Update `config/clawdbot.json`:

```json
{
  "gateway": {
    "port": 18789,
    "auth": {
      "mode": "token",
      "token": "your-token"
    },
    "controlUi": {
      "enabled": true
    }
  },
  "agent": {
    "workspace": "/workspace"
  },
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

### Step 7: Restart and Test

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
- [ ] Claude connected: `docker compose run --rm cli clawdbot models status`
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
