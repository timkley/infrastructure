#!/usr/bin/env bash
#
# restore.sh — Restore service data from restic backups
#
# Usage:
#   ./restore.sh snapshots [service]           List available snapshots
#   ./restore.sh restore <service> [id]        Restore to /tmp (default: latest)
#   ./restore.sh restore <service> latest      Restore most recent snapshot
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ ! -f "$SCRIPT_DIR/backup.env" ]]; then
    echo "ERROR: backup.env not found." >&2
    exit 1
fi
source "$SCRIPT_DIR/backup.env"

usage() {
    echo "Usage:"
    echo "  $0 snapshots [service]           List available snapshots"
    echo "  $0 restore <service> [snapshot]  Restore to temp directory"
    echo ""
    echo "Services: paperless, tandoor, unifi, beszel, traefik, couchdb, immich, openbao"
    exit 1
}

cmd_snapshots() {
    local service="${1:-}"
    if [[ -n "$service" ]]; then
        restic snapshots --tag "$service"
    else
        restic snapshots --group-by tags
    fi
}

cmd_restore() {
    local service="${1:?Missing service name}"
    local snapshot="${2:-latest}"
    local restore_dir="/tmp/restore-${service}-$(date +%Y%m%d-%H%M%S)"

    echo "Restoring '$service' (snapshot: $snapshot) → $restore_dir"
    echo ""

    restic restore "$snapshot" --tag "$service" --target "$restore_dir"

    echo ""
    echo "============================================================"
    echo "Restored to: $restore_dir"
    echo "============================================================"
    echo ""
    echo "Next steps to put the data back in place:"
    echo ""

    case "$service" in
        paperless)
            cat <<'EOF'
  1. docker compose --project-directory $INFRA_DIR/paperless down
  2. Move current data aside:
       mv $INFRA_DIR/paperless/paperless_data{,.old}
       mv $INFRA_DIR/paperless/paperless_media{,.old}
  3. Copy restored data:
       cp -a $RESTORE_DIR/$INFRA_DIR/paperless/paperless_data $INFRA_DIR/paperless/
       cp -a $RESTORE_DIR/$INFRA_DIR/paperless/paperless_media $INFRA_DIR/paperless/
  4. If db.sqlite3.backup exists, use it:
       cp $INFRA_DIR/paperless/paperless_data/db.sqlite3.backup \
          $INFRA_DIR/paperless/paperless_data/db.sqlite3
  5. docker compose --project-directory $INFRA_DIR/paperless up -d
  6. Verify, then: rm -rf $INFRA_DIR/paperless/paperless_data.old ...
EOF
            ;;
        tandoor)
            cat <<'EOF'
  1. docker compose --project-directory $INFRA_DIR/tandoor down
  2. Restore media files:
       mv $INFRA_DIR/tandoor/mediafiles{,.old}
       cp -a $RESTORE_DIR/$INFRA_DIR/tandoor/mediafiles $INFRA_DIR/tandoor/
  3. Start only the database:
       docker compose --project-directory $INFRA_DIR/tandoor up -d db
  4. Restore database from SQL dump:
       docker compose --project-directory $INFRA_DIR/tandoor exec -T db \
         sh -c 'psql -U "${POSTGRES_USER:-postgres}" "${POSTGRES_DB:-postgres}"' \
         < $RESTORE_DIR/$INFRA_DIR/tandoor/backup_tandoor.sql
  5. docker compose --project-directory $INFRA_DIR/tandoor up -d
  6. Verify, then: rm -rf $INFRA_DIR/tandoor/mediafiles.old
EOF
            ;;
        unifi)
            cat <<'EOF'
  1. Copy the Unifi autobackup .unf file from the restored directory
  2. In the Unifi web UI: Settings → Backups → Restore
  3. Upload the .unf file
EOF
            ;;
        couchdb)
            cat <<'EOF'
  1. docker compose --project-directory $INFRA_DIR/couchdb down
  2. Move current data aside:
       mv $INFRA_DIR/couchdb/data{,.old}
  3. Copy restored data:
       cp -a $RESTORE_DIR/$INFRA_DIR/couchdb/data $INFRA_DIR/couchdb/
  4. docker compose --project-directory $INFRA_DIR/couchdb up -d
  5. Verify, then: rm -rf $INFRA_DIR/couchdb/data.old
EOF
            ;;
        immich)
            cat <<'EOF'
  1. docker compose --project-directory $INFRA_DIR/immich down
  2. Move current data aside:
       mv $INFRA_DIR/immich/library{,.old}
       mv $INFRA_DIR/immich/postgres{,.old}
  3. Copy restored files:
       cp -a $RESTORE_DIR/$INFRA_DIR/immich/library $INFRA_DIR/immich/
  4. Load Immich database settings:
       set -a
       source $INFRA_DIR/immich/.env
       set +a
  5. Create containers and start only Postgres:
       docker compose --project-directory $INFRA_DIR/immich create
       docker start immich-db
       sleep 10
  6. Restore database dump:
       gunzip --stdout $RESTORE_DIR/$INFRA_DIR/immich/backup_immich.sql.gz \
       | sed "s/SELECT pg_catalog.set_config('search_path', '', false);/SELECT pg_catalog.set_config('search_path', 'public, pg_catalog', true);/g" \
       | docker exec -i immich-db psql --dbname="${DB_DATABASE_NAME:-immich}" --username="${DB_USERNAME:-postgres}" --single-transaction --set ON_ERROR_STOP=on
  7. docker compose --project-directory $INFRA_DIR/immich up -d
  8. Verify, then: rm -rf $INFRA_DIR/immich/library.old $INFRA_DIR/immich/postgres.old
EOF
            ;;
        openbao)
            cat <<'EOF'
  1. Find the restored snapshot file:
       ls $RESTORE_DIR/$INFRA_DIR/openbao/backup/openbao-raft.snap
  2. Start OpenBao with an empty or disposable data directory.
  3. Initialize and unseal that temporary OpenBao instance.
  4. Copy the snapshot into the container-mounted data directory:
       cp $RESTORE_DIR/$INFRA_DIR/openbao/backup/openbao-raft.snap \
         $INFRA_DIR/openbao/data/openbao-raft.snap
  5. Restore the Raft snapshot with an authorized token:
       docker compose --project-directory $INFRA_DIR/openbao exec -T app \
         bao operator raft snapshot restore -force /openbao/data/openbao-raft.snap
  6. Remove the temporary snapshot copy from the live data directory.
  7. Restart OpenBao, unseal with the restored unseal keys, and run:
       $INFRA_DIR/openbao/smoke-test.sh

  Keep Root-Token and Unseal-Keys out of shells with history where possible.
  Restore should be rehearsed before relying on OpenBao for production MCP access.
EOF
            ;;
        *)
            echo "  Review the files in $restore_dir and copy them back into place."
            ;;
    esac

    echo ""
    echo "RESTORE_DIR=$restore_dir"
}

# ---------------------------------------------------------------------------
case "${1:-}" in
    snapshots) shift; cmd_snapshots "$@" ;;
    restore)   shift; cmd_restore "$@" ;;
    *)         usage ;;
esac
