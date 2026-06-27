# OpenBao auf Lando

OpenBao ist der zentrale Secret Store für den späteren Heisenberg Access MCP. Der Dienst ist nicht öffentlich über Traefik erreichbar. Standardmäßig bindet Docker den Port nur an `127.0.0.1:8200`; direkter Zugriff über Tailscale ist nur aktiv, wenn `OPENBAO_BIND_ADDR` in `openbao/.env` auf Landos Tailscale-IP gesetzt wird.

## Betrieb

Starten:

```bash
docker compose --project-directory /home/admin/docker/openbao up -d
```

Lokaler Smoke-Test:

```bash
/home/admin/docker/openbao/smoke-test.sh
```

Zugriff vom eigenen Rechner über Tailscale/SSH:

```bash
ssh -N -L 8200:127.0.0.1:8200 lando
export BAO_ADDR=http://127.0.0.1:8200
bao status
```

## Initialisierung

Nur einmal ausführen. Die Ausgabe enthält Root-Token und Unseal-Keys und darf nicht ins Git-Repo oder in Chat-Threads kopiert werden.

```bash
cd /home/admin/docker/openbao
docker compose exec -T app bao operator init -format=json > /home/admin/openbao-init.json
chmod 600 /home/admin/openbao-init.json
```

Danach drei Unseal-Keys aus `/home/admin/openbao-init.json` verwenden:

```bash
for i in 0 1 2; do
  key="$(jq -r ".unseal_keys_b64[$i]" /home/admin/openbao-init.json)"
  docker compose exec -T app bao operator unseal "$key"
done
```

Root-Token und alle Unseal-Keys anschließend in Bitwarden speichern. Sobald das geprüft ist, die Bootstrap-Datei auf Lando löschen:

```bash
shred -u /home/admin/openbao-init.json
```

## Audit Logging

Audit Logging ist deklarativ in `config/openbao.hcl` konfiguriert und schreibt nach dem Unseal nach `/openbao/audit/openbao-audit.log`. OpenBao 2.5 verhindert API-basiertes Aktivieren von File-Audit-Geräten standardmäßig; deshalb gehört die Audit-Konfiguration in die Server-Konfiguration.

## Backup

Das zentrale Restic-Backup ruft `bao operator raft snapshot save` auf. Dafür wird ein nicht versioniertes `openbao/backup.env` benötigt:

```bash
cd /home/admin/docker/openbao
root_token="$(jq -r '.root_token' /home/admin/openbao-init.json)"

cat >/tmp/openbao-backup-policy.hcl <<'EOF'
path "sys/storage/raft/snapshot" {
  capabilities = ["read"]
}
EOF

docker compose exec -T -e BAO_TOKEN="$root_token" app \
  bao policy write backup-snapshot - </tmp/openbao-backup-policy.hcl

backup_token="$(
  docker compose exec -T -e BAO_TOKEN="$root_token" app \
    bao token create -orphan -period=720h -policy=backup-snapshot -format=json \
  | jq -r '.auth.client_token'
)"

install -m 600 -o admin -g admin /dev/null /home/admin/docker/openbao/backup.env
printf 'export OPENBAO_TOKEN=%q\n' "$backup_token" >/home/admin/docker/openbao/backup.env
rm -f /tmp/openbao-backup-policy.hcl
```

Der Token wird vom Backup-Skript vor jedem Snapshot erneuert. Ohne `backup.env` schlägt nur das OpenBao-Backup fehl; es wird kein inkonsistentes Datei-Backup als Ersatz erstellt.

Restore-Übersicht:

```bash
/home/admin/docker/backup/restore.sh snapshots openbao
/home/admin/docker/backup/restore.sh restore openbao latest
```
