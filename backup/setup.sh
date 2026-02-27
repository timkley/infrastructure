#!/usr/bin/env bash
#
# setup.sh — One-time setup for the backup system
#
# Run on the VPS as root (or with sudo).
# Steps:
#   1. Install restic
#   2. Generate SSH key for Storage Box
#   3. Add SSH config entry
#   4. Create backup.env from template
#   5. Initialize restic repository
#   6. Install systemd timer
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SSH_KEY="$HOME/.ssh/id_storage_box"

echo "=== VPS Backup Setup ==="
echo ""

# ---- 1. Install restic ----
if command -v restic &>/dev/null; then
    echo "[ok] restic already installed: $(restic version)"
else
    echo "Installing restic..."
    apt-get update -qq && apt-get install -y -qq restic
    echo "[ok] Installed $(restic version)"
fi

# ---- 2. SSH key ----
if [[ -f "$SSH_KEY" ]]; then
    echo "[ok] SSH key already exists: $SSH_KEY"
else
    echo ""
    echo "Generating SSH key for Storage Box..."
    ssh-keygen -t ed25519 -f "$SSH_KEY" -N "" -C "vps-backup"
    echo ""
    echo "Public key:"
    echo "---"
    cat "${SSH_KEY}.pub"
    echo "---"
    echo ""
    echo "Add this key to your Storage Box:"
    echo "  Option A: Hetzner Robot → Storage Box → SSH Keys"
    echo "  Option B: ssh-copy-id -s -p 23 -i $SSH_KEY uXXXXXX@uXXXXXX.your-storagebox.de"
    echo ""
    read -rp "Press Enter after adding the key to continue..."
fi

# ---- 3. SSH config ----
if grep -q "^Host storagebox" "$HOME/.ssh/config" 2>/dev/null; then
    echo "[ok] SSH config entry 'storagebox' already exists"
else
    echo ""
    read -rp "Storage Box username (e.g. u123456): " sb_user
    read -rp "Storage Box hostname (e.g. u123456.your-storagebox.de): " sb_host

    mkdir -p "$HOME/.ssh"
    cat >> "$HOME/.ssh/config" <<EOF

Host storagebox
    HostName $sb_host
    User $sb_user
    Port 23
    IdentityFile $SSH_KEY
EOF
    chmod 600 "$HOME/.ssh/config"
    echo "[ok] Added SSH config entry 'storagebox'"
fi

# ---- 4. backup.env ----
if [[ -f "$SCRIPT_DIR/backup.env" ]]; then
    echo "[ok] backup.env already exists"
else
    echo ""
    restic_pw=$(openssl rand -base64 32)

    read -rp "Infrastructure directory on this VPS [/opt/docker]: " infra_dir
    infra_dir="${infra_dir:-/opt/docker}"

    cat > "$SCRIPT_DIR/backup.env" <<EOF
export RESTIC_REPOSITORY="sftp:storagebox:./backups"
export RESTIC_PASSWORD="$restic_pw"
export INFRA_DIR="$infra_dir"
EOF
    chmod 600 "$SCRIPT_DIR/backup.env"

    echo ""
    echo "[ok] Created backup.env"
    echo ""
    echo "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!"
    echo "!! SAVE THIS PASSWORD — without it backups are lost:  !!"
    echo "!! $restic_pw"
    echo "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!"
    echo ""
    read -rp "Press Enter after saving the password..."
fi

# ---- 5. Initialize restic repo ----
source "$SCRIPT_DIR/backup.env"

echo ""
echo "Testing Storage Box connection..."
if restic cat config &>/dev/null 2>&1; then
    echo "[ok] Restic repository already initialized"
else
    echo "Initializing restic repository..."
    restic init
    echo "[ok] Repository initialized"
fi

# ---- 6. Systemd units ----
echo ""
echo "Installing systemd timer..."

# Update ExecStart path to match actual location
sed "s|/opt/docker/backup/backup.sh|$SCRIPT_DIR/backup.sh|g" \
    "$SCRIPT_DIR/backup.service" > /etc/systemd/system/backup.service

cp "$SCRIPT_DIR/backup.timer" /etc/systemd/system/backup.timer

systemctl daemon-reload
systemctl enable --now backup.timer

echo "[ok] Systemd timer installed and enabled"

# ---- Done ----
echo ""
echo "=== Setup complete ==="
echo ""
echo "  Backup runs daily at 03:00 (±15 min jitter)"
echo ""
echo "  Useful commands:"
echo "    systemctl status backup.timer    — next run time"
echo "    systemctl start backup.service   — run backup now"
echo "    journalctl -u backup.service     — view logs"
echo "    ./backup.sh paperless            — back up one service"
echo "    ./restore.sh snapshots           — list all snapshots"
echo ""
