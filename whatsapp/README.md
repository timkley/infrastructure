# WhatsApp-Archiv auf Lando

Ein Docker-Container betreibt `wacli sync --follow` und speichert eingehende und
ausgehende Nachrichten aller erreichbaren Chats sowie neue Anhänge lokal.
Es gibt keine Weboberfläche, veröffentlichten Ports oder zusätzlichen Dienste.

## Daten und Version

- `data/store/wacli.db`: Nachrichten, Kontakte, Gruppen, Medienzuordnungen und FTS5.
- `data/store/session.db`: WhatsApp-Geräteverknüpfung und Schlüssel.
- `data/store/media/`: heruntergeladene Originaldateien.
- `data/`: vollständiger persistenter Mount; niemals in Git übernehmen.

Das Image installiert das offizielle Release **wacli 0.20.0** mit festen
SHA-256-Prüfsummen für Linux amd64 und arm64. Die Python-Basis ist per Digest
gepinnt. Python übernimmt nur Installation und lokale Gesundheitsprüfung.
Es ist kein zusätzlicher Dienst. UID/GID sind standardmäßig 1000:1000 und
über `WHATSAPP_UID` / `WHATSAPP_GID` anpassbar.

## Einrichtung

Auf Lando im Verzeichnis dieses Dienstes arbeiten (aktuell
`/home/admin/docker/whatsapp`):

```sh
install -d -m 0700 data data/store
docker compose build
docker compose run --rm --no-deps app version
```

Wenn eine vorhandene Verknüpfung übernommen wird, den bisherigen lokalen Sync
beenden und beide SQLite-Datenbanken mit der SQLite-Backup-API konsistent in
`data/store/` übertragen. Vorhandene Medien ebenfalls übernehmen. Keine
`LOCK`-, `HEARTBEAT`-, Socket- oder WAL-Dateien einzeln übernehmen. Den alten
Store behalten, bis Datenbestand und Live-Verbindung auf Lando geprüft sind.
**Eine Geräteidentität darf nur von einem laufenden Sync genutzt werden.**

Bei übernommener Verknüpfung vor dem Dauerbetrieb die echte Anmeldung prüfen:

```sh
docker compose run --rm --no-deps app --json doctor --connect
```

Bei einem neuen Store oder einer abgemeldeten Verknüpfung:

```sh
docker compose stop app
docker compose run --rm --no-deps app auth --download-media
```

Am iPhone: **WhatsApp → Einstellungen → Verknüpfte Geräte → Gerät hinzufügen**
und den angezeigten QR-Code scannen. Das iPhone während des Erstimports online
lassen. Nach erfolgreichem Bootstrap endet `auth` selbstständig. Ein bereits
verknüpfter Store darf nicht vorsorglich mit `auth logout` abgemeldet werden.

Danach:

```sh
docker compose up -d
docker compose ps
docker compose logs --tail 30 app
docker compose exec -T app wacli --store /data/store --read-only --json doctor
```

Bei laufendem Sync meldet `doctor` ohne `--connect` gewöhnlich
`connected=false` und `connection_state=locked_by_other_process`. Das prüft
keine zweite Live-Verbindung und ist kein Verbindungsfehler. Eine bestätigte
Live-Anmeldung vor dem Start und erfolgreich empfangene Nachrichten sind die
entscheidenden Nachweise.

## Betrieb

Der Container startet nach Docker-/Host-Neustarts automatisch. wacli versucht
bei Verbindungsfehlern fünf Minuten lang erneut zu verbinden; danach übernimmt
Docker den Neustart. Keepalive-Fehler lösen nach zwei Minuten einen Reconnect
aus. `presence-mode quiet` vermeidet ein dauerhaftes Online-Signal und hilft,
Benachrichtigungen auf dem iPhone zu erhalten; WhatsApp entscheidet letztlich
über deren Zustellung.

Der Healthcheck prüft authentifizierten Store, nicht widerrufene Session, FTS5
und einen laufenden wacli-Prozess als Store-Lock-Besitzer. Er bewertet keine
alten Nachrichten- oder HEARTBEAT-Zeitstempel als Ausfall. Er beweist nicht,
dass WhatsApp gerade erreichbar ist. Docker startet einen lediglich
`unhealthy` markierten Container nicht automatisch neu.

Keine automatischen Größenlimits, Purges oder WhatsApp-Schreibaktionen sind
konfiguriert. Den Speicherplatz des Hosts weiter über das vorhandene Monitoring
beobachten. Bei einer von WhatsApp beendeten Verknüpfung ist eine neue
Verknüpfung am iPhone erforderlich; keine automatische Wiederanmeldung umgehen.

## Alte Historie und Anhänge

Der Dauerbetrieb lädt nur Medien neu empfangener Nachrichten. Für vorhandene
Nachrichten und ältere Historie sind eigene Läufe notwendig. Diese benötigen
denselben Store-Lock, deshalb vorher den Dauerbetrieb stoppen:

```sh
docker compose stop app
docker compose run --rm --no-deps app history coverage --include-blocked
docker compose run --rm --no-deps app --timeout 30m media backfill --limit 100 --json
docker compose run --rm --no-deps app --timeout 30m media retry --limit 100 --json
docker compose up -d
```

Für `history backfill` einen konkreten Chat und begrenzte Requests wählen;
das iPhone muss online sein. Fehlende Nachrichten, ausbleibende Antworten und
nicht mehr verfügbare Medien sind Lücken, kein Beweis eines vollständigen
Archivs. Backfill nicht mit dem Dauerbetrieb gleichzeitig starten.

Den vollständigen Lauf über alle gespeicherten Chats auf dem Compose-Host starten:

```sh
python3 -u backfill.py
# Nach einem abgebrochenen Lauf bereits bearbeitete Chats überspringen:
python3 -u backfill.py --resume
```

Das iPhone dabei online und WhatsApp geöffnet halten. Der Runner stoppt den
Dauerbetrieb, erstellt konsistente lokale Sicherheitskopien beider Datenbanken,
fordert pro Chat ältere Nachrichten an und lädt anschließend alle vorhandenen
Medien nach. Für abgelaufene CDN-Dateien versucht er einen Re-Upload vom iPhone.
Er startet den Dauerbetrieb auch bei Fehlern oder SIGTERM wieder. Ein Hostausfall
oder SIGKILL kann diese Wiederaufnahme verhindern; dann `docker compose up -d app`.
Ein eigener Runner-Lock verhindert parallele Backfill-Läufe.

Private Ergebnisse und Logs liegen unter `data/backfill/`, der aktuelle Status
unter `data/store/archive-backfill.json`. Diese Dateien enthalten Chat-IDs und
bleiben außerhalb von Git. Die lokalen Sicherheitskopien sind kein externes
Backup. Ein erneuter Lauf beginnt mit dem vorhandenen Archiv und lädt fehlende
Historie und Medien nach; er löscht keine Archivnachrichten. Requests und
Wartezeiten sind begrenzt. Timeout, fehlender Anker, Batch-Limit und die Antwort
„keine älteren Nachrichten“ sind getrennte Ergebnisse, kein Nachweis einer
vollständigen iPhone-Sicherung.

## Backups und MCP

`heisenberg-access-mcp` bindet den Store direkt read-only ein und stellt fünf
private `whatsapp.*`-Werkzeuge bereit. Die Aufnahme in `backup/backup.sh` ist ein
separater nächster Schritt.

Das bestehende Restic-Backup soll konsistente SQLite-Snapshots beider
Datenbanken zusammen mit den Medien sichern. Für einen vollständigen Transfer
den Container stoppen und das gesamte persistente `data/` übernehmen.
Geräteschlüssel nur geschützt übertragen. Nie aktive SQLite-Dateien mit ihren
WAL-Dateien unabhängig voneinander kopieren. Restore separat prüfen.

Der private MCP liest `wacli.db` direkt über einen read-only Verzeichnismount
und SQLite `mode=ro` plus `PRAGMA query_only=ON`. WAL/SHM müssen sichtbar sein;
bei laufendem Sync nicht `immutable=1` setzen. Suchwerkzeuge dürfen keine
Geräte- oder Medientransportschlüssel zurückgeben und `session.db` nicht abfragen.
Beide Container verwenden dieselbe UID/GID (standardmäßig 1000:1000), damit
SQLite-Dateien und Anhänge bei 0600/0700 bleiben können. Bei abweichender UID/GID
die Werte in beiden Compose-Projekten identisch setzen. Der MCP-Mount verhindert
Schreibzugriffe unabhängig von diesen Dateirechten.

## Quellen

- [wacli 0.20.0](https://github.com/openclaw/wacli/releases/tag/v0.20.0)
- [Dauerbetrieb](https://github.com/openclaw/wacli/blob/v0.20.0/docs/sync.md)
- [Verknüpfung](https://github.com/openclaw/wacli/blob/v0.20.0/docs/auth.md)
- [Medien](https://github.com/openclaw/wacli/blob/v0.20.0/docs/media.md)
- [SQLite-Integration](https://github.com/openclaw/wacli/blob/v0.20.0/docs/integrations.md)
