#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=backup/backup.sh
source "$SCRIPT_DIR/backup.sh"

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
install -d -m 755 "$tmp/openbao"
printf 'OPENBAO_TOKEN=%q\n' 'test-token' >"$tmp/openbao/backup.env"
chmod 600 "$tmp/openbao/backup.env"

INFRA_DIR="$tmp"
calls=()

compose() {
  calls+=("$*")
  if [[ "${2:-}" == "logs" ]]; then
    printf 'open /openbao/config/openbao.hcl: permission denied\n'
    return 0
  fi
  return 1
}

container_running() { return 1; }

backup_openbao >"$tmp/stopped.out" 2>"$tmp/stopped.err"

found_logs=0
for call in "${calls[@]}"; do
  if [[ "$call" == *"logs --tail 40 --timestamps app" ]]; then
    found_logs=1
  fi
  if [[ "$call" == *"token renew"* || "$call" == *"raft snapshot"* ]]; then
    echo "stopped container continued into the snapshot" >&2
    exit 1
  fi
done
[[ "$found_logs" -eq 1 ]]
grep -q 'app container is not running' "$tmp/stopped.err"
grep -q 'permission denied' "$tmp/stopped.err"
printf 'ok stopped container logs\n'

calls=()
ERRORS=()
container_running() { return 0; }
backup_openbao >"$tmp/running.out" 2>"$tmp/running.err"
for call in "${calls[@]}"; do
  if [[ "$call" == *"logs --tail"* ]]; then
    echo "running container fetched logs" >&2
    exit 1
  fi
done
[[ "${#calls[@]}" -ge 1 ]]
grep -q 'backup token renewal failed' "$tmp/running.err"
printf 'ok running container skips logs\n'
