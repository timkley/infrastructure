#!/usr/bin/env bash
set -euo pipefail

# CouchDB init script for Obsidian Livesync
# Run once after first start from the couchdb directory: ./init.sh

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

if [ ! -f "$SCRIPT_DIR/.env" ]; then
  echo "Error: .env file not found. Copy .env.example to .env and fill in credentials."
  exit 1
fi

source "$SCRIPT_DIR/.env"

CURL="docker compose exec -T app curl -sf"
COUCHDB_URL="http://${COUCHDB_USER}:${COUCHDB_PASSWORD}@127.0.0.1:5984"

echo "Waiting for CouchDB to be ready..."
until $CURL "$COUCHDB_URL/_up" > /dev/null 2>&1; do
  sleep 2
done
echo "CouchDB is up."

# Single-node setup
echo "Configuring single-node setup..."
$CURL -X POST "$COUCHDB_URL/_cluster_setup" \
  -H "Content-Type: application/json" \
  -d "{\"action\":\"enable_single_node\",\"username\":\"${COUCHDB_USER}\",\"password\":\"${COUCHDB_PASSWORD}\",\"bind_address\":\"0.0.0.0\",\"port\":5984}"
echo ""

# Create system databases
echo "Creating system databases..."
for db in _users _replicator _global_changes; do
  $CURL -X PUT "$COUCHDB_URL/$db" || true
done
echo ""

# Create obsidian-livesync database
echo "Creating obsidian-livesync database..."
$CURL -X PUT "$COUCHDB_URL/obsidian-livesync"
echo ""

# Enable CORS
echo "Enabling CORS..."
$CURL -X PUT "$COUCHDB_URL/_node/_local/_config/httpd/enable_cors" -d '"true"'
$CURL -X PUT "$COUCHDB_URL/_node/_local/_config/cors/origins" -d '"app://obsidian.md,capacitor://localhost,http://localhost"'
$CURL -X PUT "$COUCHDB_URL/_node/_local/_config/cors/credentials" -d '"true"'
$CURL -X PUT "$COUCHDB_URL/_node/_local/_config/cors/methods" -d '"GET,PUT,POST,HEAD,DELETE"'
$CURL -X PUT "$COUCHDB_URL/_node/_local/_config/cors/headers" -d '"accept,authorization,content-type,origin,referer"'
echo ""

# Increase max HTTP request size (4GB for attachments)
echo "Setting max HTTP request size to 4GB..."
$CURL -X PUT "$COUCHDB_URL/_node/_local/_config/chttpd/max_http_request_size" -d '"4294967296"'
echo ""

# Increase max document size (unlimited, Livesync handles chunking)
$CURL -X PUT "$COUCHDB_URL/_node/_local/_config/couchdb/max_document_size" -d '"0"'
echo ""

echo ""
echo "=== CouchDB initialization complete! ==="
echo ""
echo "Obsidian Livesync Plugin Settings:"
echo "  URI:      https://couchdb.timkley.dev"
echo "  Username: ${COUCHDB_USER}"
echo "  Password: (from .env)"
echo "  Database: obsidian-livesync"
