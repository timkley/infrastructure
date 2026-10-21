#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=smoke-test.sh
source "$SCRIPT_DIR/smoke-test.sh"

if [[ "$OPENBAO_UID" != "100" ]]; then
  printf 'OPENBAO_UID is %s, expected 100\n' "$OPENBAO_UID" >&2
  exit 1
fi

sudo -n -u "#${OPENBAO_UID}" true

tmp="$(mktemp -d)"
chmod 755 "$tmp"
trap 'rm -rf "$tmp"' EXIT

install -d -m 755 "$tmp/open644"
install -m 644 /dev/null "$tmp/open644/openbao.hcl"

install -d -m 755 "$tmp/mode600"
install -m 600 /dev/null "$tmp/mode600/openbao.hcl"

install -d -m 755 "$tmp/owner100"
install -m 600 /dev/null "$tmp/owner100/openbao.hcl"
sudo chown "$OPENBAO_UID" "$tmp/owner100/openbao.hcl"

install -d -m 700 "$tmp/dir700"
install -m 644 /dev/null "$tmp/dir700/openbao.hcl"

install -d -m 755 "$tmp/group640"
install -m 640 /dev/null "$tmp/group640/openbao.hcl"
sudo chgrp 1000 "$tmp/group640/openbao.hcl"

kernel_can_read() {
  local file="$1"
  sudo -n -u "#${OPENBAO_UID}" test -x "$(dirname "$file")" \
    && sudo -n -u "#${OPENBAO_UID}" test -r "$file"
}

show_kernel() {
  local file="$1" label="$2"
  if kernel_can_read "$file"; then
    printf 'kernel %s: readable\n' "$label"
  else
    printf 'kernel %s: not readable\n' "$label"
  fi
}

want_readable() {
  local file="$1" want="$2" label="$3"
  local kernel=1 fn=1

  if kernel_can_read "$file"; then
    kernel=0
  fi
  if openbao_config_readable "$file" >/dev/null 2>&1; then
    fn=0
  fi
  if [[ "$kernel" != "$want" || "$fn" != "$want" ]]; then
    printf 'FAIL %s kernel=%s function=%s want=%s\n' "$label" "$kernel" "$fn" "$want" >&2
    exit 1
  fi
  printf 'ok %s\n' "$label"
}

show_kernel "$tmp/mode600/openbao.hcl" "mode 600 owner $(id -u)"
show_kernel "$tmp/open644/openbao.hcl" "mode 644 owner $(id -u)"
show_kernel "$tmp/owner100/openbao.hcl" "mode 600 owner ${OPENBAO_UID}"
show_kernel "$tmp/dir700/openbao.hcl" "directory 700"

if ! declare -F openbao_config_readable >/dev/null; then
  echo "openbao_config_readable is not defined" >&2
  exit 1
fi

want_readable "$tmp/open644/openbao.hcl" 0 "mode 644 owner $(id -u)"
want_readable "$tmp/mode600/openbao.hcl" 1 "mode 600 owner $(id -u)"
want_readable "$tmp/owner100/openbao.hcl" 0 "mode 600 owner ${OPENBAO_UID}"
want_readable "$tmp/dir700/openbao.hcl" 1 "directory 700"
want_readable "$tmp/group640/openbao.hcl" 1 "mode 640 group 1000"
want_readable "$SCRIPT_DIR/config/openbao.hcl" 0 "repo config/openbao.hcl"

if openbao_config_readable "$tmp/missing.hcl" >/dev/null 2>&1; then
  echo "FAIL missing file was accepted" >&2
  exit 1
fi
printf 'ok missing file\n'
