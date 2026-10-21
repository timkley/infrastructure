#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# uid of `openbao` from `adduser -S` in openbao/openbao:2.7.1.
OPENBAO_UID=100

run_smoke() {
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
