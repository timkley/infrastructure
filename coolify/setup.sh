#!/bin/bash
set -euo pipefail

# One-time setup for Coolify on the server.
# Run as root: bash coolify/setup.sh

# Create data directories
mkdir -p /data/coolify/{ssh/keys,ssh/mux,applications,databases,backups,services,proxy/dynamic,sentinel}
chown -R 9999:root /data/coolify
chmod -R 700 /data/coolify

# Generate SSH key for Coolify to manage Docker on the host
SSH_KEY="/data/coolify/ssh/keys/id.root@host.docker.internal"
if [ ! -f "$SSH_KEY" ]; then
    ssh-keygen -t ed25519 -f "$SSH_KEY" -q -N "" -C "coolify"
    chown 9999:root "$SSH_KEY" "$SSH_KEY.pub"

    # Add public key to authorized_keys for root
    mkdir -p /root/.ssh
    chmod 700 /root/.ssh
    cat "$SSH_KEY.pub" >> /root/.ssh/authorized_keys
    chmod 600 /root/.ssh/authorized_keys

    # Also add for the sudo-invoking user if applicable
    if [ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != "root" ]; then
        SUDO_USER_HOME=$(getent passwd "$SUDO_USER" | cut -d: -f6)
        mkdir -p "$SUDO_USER_HOME/.ssh"
        chmod 700 "$SUDO_USER_HOME/.ssh"
        cat "$SSH_KEY.pub" >> "$SUDO_USER_HOME/.ssh/authorized_keys"
        chmod 600 "$SUDO_USER_HOME/.ssh/authorized_keys"
        chown -R "$SUDO_USER:$(id -gn "$SUDO_USER")" "$SUDO_USER_HOME/.ssh"
    fi
    echo "SSH key generated and added to authorized_keys."
else
    echo "SSH key already exists, skipping."
fi

# Generate .env from .env.example if it doesn't exist
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
if [ ! -f "$SCRIPT_DIR/.env" ]; then
    cp "$SCRIPT_DIR/.env.example" "$SCRIPT_DIR/.env"
    sed -i "s|^APP_ID=.*|APP_ID=$(openssl rand -hex 16)|" "$SCRIPT_DIR/.env"
    sed -i "s|^APP_KEY=.*|APP_KEY=base64:$(openssl rand -base64 32)|" "$SCRIPT_DIR/.env"
    sed -i "s|^DB_PASSWORD=.*|DB_PASSWORD=$(openssl rand -base64 32)|" "$SCRIPT_DIR/.env"
    sed -i "s|^REDIS_PASSWORD=.*|REDIS_PASSWORD=$(openssl rand -base64 32)|" "$SCRIPT_DIR/.env"
    sed -i "s|^PUSHER_APP_ID=.*|PUSHER_APP_ID=$(openssl rand -hex 32)|" "$SCRIPT_DIR/.env"
    sed -i "s|^PUSHER_APP_KEY=.*|PUSHER_APP_KEY=$(openssl rand -hex 32)|" "$SCRIPT_DIR/.env"
    sed -i "s|^PUSHER_APP_SECRET=.*|PUSHER_APP_SECRET=$(openssl rand -hex 32)|" "$SCRIPT_DIR/.env"
    echo ".env generated with random secrets."
else
    echo ".env already exists, skipping."
fi

echo ""
echo "Setup complete. Next steps:"
echo "  1. cd $SCRIPT_DIR && docker compose up -d"
echo "  2. Open https://coolify.timkley.dev and create your account"
echo "  3. Go to Servers > localhost > Proxy and set it to 'None'"
