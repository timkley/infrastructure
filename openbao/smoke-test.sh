#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# uid of `openbao` from `adduser -S` in openbao/openbao:2.7.1.
OPENBAO_UID=100

path_allows_uid() {
  local path="$1"
  local flag="$2"
  local uid="$3"
  local owner mode bits user_digit other_digit

  owner="$(stat -c '%u' "$path")"
  mode="$(stat -c '%a' "$path")"
  bits="${mode: -3}"
  if [[ ! "$bits" =~ ^[0-7]{3}$ ]]; then
    return 1
  fi
  user_digit="${bits:0:1}"
  other_digit="${bits:2:1}"
  if [[ "$owner" == "$uid" ]] && (( (8#$user_digit & flag) == flag )); then
    return 0
  fi
  if (( (8#$other_digit & flag) == flag )); then
    return 0
  fi
  return 1
}

openbao_config_readable() {
  local config="$1"
  local dir owner group mode dir_mode

  if [[ ! -f "$config" ]]; then
    printf 'OpenBao config missing: %s\n' "$config" >&2
    return 1
  fi
  dir="$(dirname "$config")"
  if path_allows_uid "$dir" 1 "$OPENBAO_UID" \
      && path_allows_uid "$config" 4 "$OPENBAO_UID"; then
    return 0
  fi

  owner="$(stat -c '%u' "$config")"
  group="$(stat -c '%g' "$config")"
  mode="$(stat -c '%a' "$config")"
  dir_mode="$(stat -c '%a' "$dir")"
  printf 'OpenBao cannot read %s (owner %s:%s, mode %s, directory mode %s).\n' \
    "$config" "$owner" "$group" "$mode" "$dir_mode" >&2
  printf 'The container runs as uid %s. ./config is mounted read-only and SKIP_CHOWN=true, so the entrypoint does not change ownership.\n' \
    "$OPENBAO_UID" >&2
  printf 'config/openbao.hcl has no secrets. chmod 755 %s && chmod 644 %s\n' \
    "$dir" "$config" >&2
  printf 'A mode 600 file also works when its owner is uid %s.\n' "$OPENBAO_UID" >&2
  return 1
}

run_smoke() {
  openbao_config_readable "$SCRIPT_DIR/config/openbao.hcl"

  local status_json
  status_json="$(
    docker compose --project-directory "$SCRIPT_DIR" exec -T app \
      bao status -address=http://127.0.0.1:8200 -format=json 2>/dev/null || true
  )"

  if [[ -z "$status_json" ]]; then
    echo "OpenBao status check failed: no response from container." >&2
    exit 1
  fi

  if ! jq -e '.initialized == true and .sealed == false' >/dev/null <<<"$status_json"; then
    echo "OpenBao is reachable but not ready:" >&2
    jq '{initialized, sealed, version, storage_type}' <<<"$status_json" >&2
    exit 1
  fi

  jq '{initialized, sealed, version, storage_type, cluster_name}' <<<"$status_json"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  run_smoke
fi
