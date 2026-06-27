#!/usr/bin/env bash
#
# backup.sh — Back up Docker service data to Hetzner Storage Box via restic
#
# Backs up: paperless, tandoor, unifi, beszel, traefik, couchdb, immich, openbao
# Run manually:  ./backup.sh
# Run one service: ./backup.sh paperless
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOCK_FILE="/tmp/vps-backup.lock"
ERRORS=()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
if [[ ! -f "$SCRIPT_DIR/backup.env" ]]; then
    echo "ERROR: backup.env not found. Run setup.sh first." >&2
    exit 1
fi
source "$SCRIPT_DIR/backup.env"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
log()       { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }
log_error() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] ERROR: $*" >&2; ERRORS+=("$*"); }

hc_ping() {
    [[ -z "${HEALTHCHECK_URL:-}" ]] && return
    curl -fsS -m 10 --retry 3 "$HEALTHCHECK_URL$1" >/dev/null 2>&1 || true
}

acquire_lock() {
    if ! mkdir "$LOCK_FILE" 2>/dev/null; then
        echo "Another backup is already running (lock: $LOCK_FILE)." >&2
        exit 1
    fi
    trap 'rm -rf "$LOCK_FILE"' EXIT
}

compose() {
    local service_dir="$1"; shift
    docker compose --project-directory "$service_dir" "$@"
}

container_running() {
    local service_dir="$1" container="$2"
    compose "$service_dir" ps --status running "$container" --quiet 2>/dev/null | grep -q .
}

# ---------------------------------------------------------------------------
# Service backup functions
# ---------------------------------------------------------------------------

backup_paperless() {
    local dir="$INFRA_DIR/paperless"
    log "paperless: starting backup"

    # Consistent SQLite copy while the app is running
    if container_running "$dir" app; then
        log "paperless: creating SQLite .backup copy"
        compose "$dir" exec -T app \
            python3 -c "import sqlite3; s=sqlite3.connect('/usr/src/paperless/data/db.sqlite3'); d=sqlite3.connect('/usr/src/paperless/data/db.sqlite3.backup'); s.backup(d); d.close(); s.close()" \
            || log_error "paperless: SQLite .backup failed — backing up live DB"
    else
        log "paperless: container not running, backing up files directly"
    fi

    restic backup --tag paperless \
        "$dir/paperless_data" \
        "$dir/paperless_media" \
        "$dir/export" \
        "$dir/consume"

    # Clean up .backup copy
    rm -f "$dir/paperless_data/db.sqlite3.backup"

    log "paperless: done"
}

backup_tandoor() {
    local dir="$INFRA_DIR/tandoor"
    log "tandoor: starting backup"

    # Dump Postgres — write to admin-owned location since postgresql/ is owned by postgres UID
    local dump_file="$dir/backup_tandoor.sql"
    if container_running "$dir" db; then
        log "tandoor: dumping PostgreSQL"
        compose "$dir" exec -T db \
            sh -c 'pg_dump -U "${POSTGRES_USER:-postgres}" "${POSTGRES_DB:-postgres}"' \
            > "$dump_file" \
            || log_error "tandoor: pg_dump failed"
    else
        log "tandoor: db container not running, backing up files directly"
    fi

    # pg_dump is the full DB backup — no need for the raw postgresql/ data dir
    restic backup --tag tandoor \
        "$dump_file" \
        "$dir/mediafiles"

    rm -f "$dump_file"

    log "tandoor: done"
}

backup_unifi() {
    local dir="$INFRA_DIR/unifi"
    log "unifi: starting backup"

    # Unifi creates its own autobackups in the ./backup bind mount.
    # Backing up that directory is the recommended restore approach.
    restic backup --tag unifi \
        "$dir/backup"

    log "unifi: done"
}

backup_beszel() {
    local dir="$INFRA_DIR/beszel"
    log "beszel: starting backup"

    restic backup --tag beszel \
        "$dir/beszel_data"

    log "beszel: done"
}

backup_traefik() {
    local dir="$INFRA_DIR/traefik"
    log "traefik: starting backup"

    # Only the ACME cert file — configs are in the git repo
    restic backup --tag traefik \
        "$dir/acme.json"

    log "traefik: done"
}

backup_couchdb() {
    local dir="$INFRA_DIR/couchdb"
    log "couchdb: starting backup"

    restic backup --tag couchdb \
        "$dir/data"

    log "couchdb: done"
}

backup_immich() {
    local dir="$INFRA_DIR/immich"
    local dump_file="$dir/backup_immich.sql.gz"
    log "immich: starting backup"

    if [[ ! -d "$dir/library" ]]; then
        log_error "immich: library directory missing at $dir/library"
        return
    fi

    if ! container_running "$dir" db; then
        log_error "immich: database container is not running; refusing incomplete backup"
        return
    fi

    log "immich: dumping PostgreSQL"
    if ! compose "$dir" exec -T db \
        sh -c 'pg_dump --clean --if-exists --dbname="${POSTGRES_DB:-immich}" --username="${POSTGRES_USER:-postgres}"' \
        | gzip > "$dump_file"; then
        rm -f "$dump_file"
        log_error "immich: pg_dump failed"
        return
    fi

    # Back up database first, then files, matching Immich's recommended ordering.
    restic backup --tag immich \
        "$dump_file" \
        "$dir/library"

    rm -f "$dump_file"

    log "immich: done"
}

backup_openbao() {
    local dir="$INFRA_DIR/openbao"
    local env_file="$dir/backup.env"
    local backup_dir="$dir/backup"
    local snapshot_file="$backup_dir/openbao-raft.snap"
    local audit_file="$backup_dir/openbao-audit.log"
    log "openbao: starting backup"

    if [[ ! -f "$env_file" ]]; then
        log_error "openbao: backup.env missing; refusing to create an inconsistent raw Raft backup"
        return
    fi

    # shellcheck source=/dev/null
    source "$env_file"

    if [[ -z "${OPENBAO_TOKEN:-}" ]]; then
        log_error "openbao: OPENBAO_TOKEN missing in backup.env"
        return
    fi

    if ! container_running "$dir" app; then
        log_error "openbao: app container is not running"
        return
    fi

    log "openbao: renewing backup token"
    if ! compose "$dir" exec -T -e BAO_TOKEN="$OPENBAO_TOKEN" app \
        bao token renew >/dev/null; then
        log_error "openbao: backup token renewal failed"
        return
    fi

    log "openbao: creating Raft snapshot"
    rm -rf "$backup_dir"
    install -d -m 700 "$backup_dir"

    if ! compose "$dir" exec -T -e BAO_TOKEN="$OPENBAO_TOKEN" app \
        bao operator raft snapshot save /tmp/openbao-raft.snap >/dev/null; then
        rm -rf "$backup_dir"
        log_error "openbao: Raft snapshot failed"
        return
    fi

    compose "$dir" cp app:/tmp/openbao-raft.snap "$snapshot_file" >/dev/null
    compose "$dir" exec -T app rm -f /tmp/openbao-raft.snap

    if ! compose "$dir" exec -T app sh -c 'test -f /openbao/audit/openbao-audit.log && cat /openbao/audit/openbao-audit.log' > "$audit_file"; then
        rm -rf "$backup_dir"
        log_error "openbao: audit log copy failed"
        return
    fi

    chmod 600 "$snapshot_file" "$audit_file"

    restic backup --tag openbao \
        "$snapshot_file" \
        "$audit_file"

    rm -rf "$backup_dir"

    log "openbao: done"
}

# ---------------------------------------------------------------------------
# Retention policy: daily 7, weekly 4, monthly 6
# ---------------------------------------------------------------------------
apply_retention() {
    log "Applying retention policy"
    restic forget \
        --keep-daily 7 \
        --keep-weekly 4 \
        --keep-monthly 6 \
        --group-by "host,tags" \
        --prune
    log "Retention policy applied"
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
ALL_SERVICES=(paperless tandoor unifi beszel traefik couchdb immich openbao)

main() {
    acquire_lock

    local services=("${@:-${ALL_SERVICES[@]}}")

    log "=== Backup starting: ${services[*]} ==="
    hc_ping /start

    for svc in "${services[@]}"; do
        case "$svc" in
            paperless) backup_paperless ;;
            tandoor)   backup_tandoor   ;;
            unifi)     backup_unifi     ;;
            beszel)    backup_beszel    ;;
            traefik)   backup_traefik   ;;
            couchdb)   backup_couchdb   ;;
            immich)    backup_immich    ;;
            openbao)   backup_openbao   ;;
            *) log_error "Unknown service: $svc" ;;
        esac
    done

    apply_retention

    if [[ ${#ERRORS[@]} -gt 0 ]]; then
        log "=== Backup finished with ${#ERRORS[@]} error(s) ==="
        printf '  - %s\n' "${ERRORS[@]}"
        hc_ping /fail
        exit 1
    fi

    log "=== Backup complete ==="
    hc_ping ""
}

main "$@"
