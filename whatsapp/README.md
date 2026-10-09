# WhatsApp-Archiv

Archivdienst und WhatsApp-MCP werden im Holocron-Repo
[tim-kleyersburg.de](https://github.com/timkley/tim-kleyersburg.de) verwaltet.
Die aktuelle [Betriebsdokumentation](https://github.com/timkley/tim-kleyersburg.de/blob/main/docs/whatsapp-mcp.md)
und die [Compose-Definition](https://github.com/timkley/tim-kleyersburg.de/blob/main/deployment/whatsapp/docker-compose.yml)
liegen dort.

Auf Lando wird ausschließlich aus
`/var/www/tim-kleyersburg/deployment/whatsapp` gearbeitet. Der laufende Container
heißt `whatsapp-app`, das Image `whatsapp-archive:0.20.0`.
Der persistente Datenpfad bleibt `/home/admin/docker/whatsapp/data`.
Dieses Verzeichnis mit Store, Gerätesession, Medien und Sicherungen darf bei der
Bereinigung früherer Betriebsdateien nicht gelöscht werden.

Unter `/home/admin/docker/whatsapp` wird keine alte Compose-Datei mehr betrieben.
Kein `docker compose down` mit der alten Definition ausführen: Beide Versionen
verwenden denselben Projekt- und Containernamen und würden denselben Writer
stoppen. Ein zweiter Sync mit derselben Geräteidentität darf nicht gestartet
werden.
