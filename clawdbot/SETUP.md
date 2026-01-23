# Clawdbot Setup Guide

Clawdbot is a personal AI assistant gateway that bridges messaging platforms (WhatsApp, Telegram, Discord) to AI agents like Claude.

## Prerequisites

### 1. Clone the Clawdbot Repository

The Docker image must be built locally from the clawdbot source code. Clone it to `/opt/clawdbot`:

```bash
git clone https://github.com/clawdbot/clawdbot.git /opt/clawdbot
```

This path is referenced in `.env` as `CLAWDBOT_REPO_PATH`.

### 2. Docker and Docker Compose

Ensure Docker and Docker Compose are installed on your system.

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
│  • Token auto-generated during onboard wizard                       │
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

### Automated Setup (Recommended)

The setup script runs the official clawdbot `onboard` wizard, which automatically:
- Configures the gateway
- Generates a secure authentication token
- Creates required configuration files
- Starts the gateway service

```bash
cd clawdbot
./setup.sh
```

That's it! The script will guide you through the setup process.

### What setup.sh Does

1. Checks prerequisites (Docker, clawdbot repo)
2. Creates `.env` from `.env.example` if needed
3. Builds the Docker image from `/opt/clawdbot`
4. Runs the `onboard` wizard interactively
5. Extracts the generated token and saves it to `.env`
6. Starts the gateway service
7. Displays next steps

## Manual Setup (Alternative)

If you prefer to run the steps manually instead of using `setup.sh`:

### 1. Configure Environment

```bash
cd clawdbot
cp .env.example .env
```

### 2. Build the Image

```bash
docker compose build clawdbot-gateway
```

### 3. Run Onboard Wizard

```bash
docker compose run --rm clawdbot-cli onboard --no-install-daemon
```

Follow the prompts. The wizard will:
- Ask about gateway configuration (choose "local" mode)
- Generate a secure authentication token
- Create `~/.clawdbot/clawdbot.json` automatically
- Set up the workspace directory

### 4. Save Token to .env

Copy the token from the onboard output and add it to `.env`:

```bash
CLAWDBOT_GATEWAY_TOKEN=your-generated-token-here
```

### 5. Start the Gateway

```bash
docker compose up -d clawdbot-gateway
docker compose logs -f clawdbot-gateway
```

## Post-Setup Configuration

### 1. Access the Control UI

Navigate to:
```
https://clawdbot.timkley.dev
```

Paste the token from `.env` when prompted.

### 2. Set Up Claude Authentication

```bash
docker compose run --rm clawdbot-cli bash
```

Inside the container:

```bash
claude setup-token
```

Complete the OAuth flow in your browser, then verify:

```bash
clawdbot models status
exit
```

#### Alternative: Anthropic API Key

If `setup-token` doesn't work, you can use an API key instead by adding to `~/.clawdbot/clawdbot.json`:

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

Or add to `.env`:

```bash
ANTHROPIC_API_KEY=sk-ant-...
```

Then add to docker-compose.yml gateway environment:

```yaml
environment:
  - ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}
```

### 3. Link WhatsApp

```bash
docker compose run --rm clawdbot-cli channels login
```

Scan the QR code with: **WhatsApp → Settings → Linked Devices → Link a Device**

### 4. Configure WhatsApp Access Control

The onboard wizard creates `~/.clawdbot/clawdbot.json`. Edit this file to add access controls:

```json
{
  "gateway": {
    "mode": "local",
    "port": 18789,
    "auth": {
      "token": "your-token-here"
    }
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

**Access Control Options:**

| Setting | Effect |
|---------|--------|
| `dmPolicy: "pairing"` | Unknown contacts get a pairing code to approve |
| `allowFrom: ["+49..."]` | Only these numbers can message (skip pairing) |
| `groups.*.requireMention` | In groups, bot only responds when @mentioned |

### 5. Restart and Test

```bash
docker compose restart clawdbot-gateway
```

**Test the setup:**

1. Send a WhatsApp message to your linked number
2. If not in `allowFrom`, you'll get a pairing code → approve in Control UI
3. Chat with Claude via WhatsApp!

## Adding More Channels

### Telegram

```bash
docker compose run --rm clawdbot-cli channels add --channel telegram --token "BOT_TOKEN"
```

Get a bot token from [@BotFather](https://t.me/botfather) on Telegram.

### Discord

```bash
docker compose run --rm clawdbot-cli channels add --channel discord --token "BOT_TOKEN"
```

Get a bot token from the [Discord Developer Portal](https://discord.com/developers/applications).

## Verification Checklist

- [ ] Container running: `docker compose ps` shows clawdbot-gateway healthy
- [ ] Traefik routing: `https://clawdbot.timkley.dev` loads (prompts for token)
- [ ] Control UI: Can log in with token from `.env`
- [ ] Claude auth: `docker compose run --rm clawdbot-cli clawdbot models status` shows active
- [ ] WhatsApp linked: `docker compose run --rm clawdbot-cli channels status` shows connected
- [ ] End-to-end test: Send WhatsApp message, receive AI response

## Troubleshooting

### View Logs

```bash
docker compose logs -f clawdbot-gateway
```

### Check Container Status

```bash
docker compose ps
```

### Run Diagnostics

```bash
docker compose run --rm clawdbot-cli clawdbot doctor
```

### Check Channel Status

```bash
docker compose run --rm clawdbot-cli channels status
```

### Gateway Not Starting

1. Check if the token is set in `.env`:
   ```bash
   grep CLAWDBOT_GATEWAY_TOKEN .env
   ```

2. Verify the config file exists:
   ```bash
   ls -la ~/.clawdbot/clawdbot.json
   ```

3. Check for port conflicts:
   ```bash
   docker compose logs clawdbot-gateway | grep -i error
   ```

### WhatsApp Not Connecting

1. Ensure you're using the correct QR scanning method (WhatsApp → Settings → Linked Devices)
2. Check if the session is still active: `docker compose run --rm clawdbot-cli channels status`
3. Try re-linking: `docker compose run --rm clawdbot-cli channels login`

### Pairing Codes Not Working

1. Verify Control UI is accessible at `https://clawdbot.timkley.dev`
2. Check if the token in `.env` matches the token in `~/.clawdbot/clawdbot.json`
3. Restart the gateway: `docker compose restart clawdbot-gateway`

## Upgrading Clawdbot

To update to the latest version:

```bash
# 1. Update the source code
cd /opt/clawdbot
git pull

# 2. Rebuild the Docker image
cd /path/to/infrastructure/clawdbot
docker compose build --no-cache clawdbot-gateway

# 3. Restart with new image
docker compose up -d clawdbot-gateway

# 4. Verify
docker compose logs -f clawdbot-gateway
```

One-liner:

```bash
cd /opt/clawdbot && git pull && cd - && docker compose build --no-cache clawdbot-gateway && docker compose up -d clawdbot-gateway
```

## Configuration Reference

### Environment Variables (.env)

| Variable | Default | Description |
|----------|---------|-------------|
| `CLAWDBOT_IMAGE` | `clawdbot:local` | Docker image name |
| `CLAWDBOT_REPO_PATH` | `/opt/clawdbot` | Path to cloned repo for building |
| `CLAWDBOT_GATEWAY_BIND` | `lan` | Gateway bind mode (`lan` or `localhost`) |
| `CLAWDBOT_CONFIG_DIR` | `~/.clawdbot` | Host directory for config files |
| `CLAWDBOT_WORKSPACE_DIR` | `~/clawd` | Host directory for agent workspace |
| `CLAWDBOT_GATEWAY_TOKEN` | (generated) | Authentication token for gateway |

### Gateway Config (~/.clawdbot/clawdbot.json)

Created automatically by the `onboard` wizard. Key sections:

- `gateway`: Gateway settings (mode, port, auth)
- `channels`: Channel-specific configurations (WhatsApp, Telegram, Discord)
- `agents`: Agent settings (workspace, defaults)
- `models`: Model provider configurations (Claude, OpenAI, etc.)

## Additional Resources

- [Clawdbot GitHub](https://github.com/clawdbot/clawdbot)
- [Official Documentation](https://github.com/clawdbot/clawdbot/blob/main/README.md)
- [Traefik Documentation](https://doc.traefik.io/traefik/)

## Support

For issues with this setup:
1. Check the logs: `docker compose logs -f clawdbot-gateway`
2. Run diagnostics: `docker compose run --rm clawdbot-cli clawdbot doctor`
3. Review the troubleshooting section above

For clawdbot-specific issues, see the [official issue tracker](https://github.com/clawdbot/clawdbot/issues).
