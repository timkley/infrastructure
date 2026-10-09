# Paperless

Paperless runs on Lando at `/home/admin/docker/paperless` with SQLite and Redis 7.
The Compose file pins Paperless to `3.3.0`. Keep `PAPERLESS_SECRET_KEY` in the
server's `.env`; changing it invalidates existing signed sessions and tokens.

## v3 settings

- `PAPERLESS_ARCHIVE_FILE_GENERATION=always` preserves the v2 default of
  generating an archive even when a PDF already contains text.
- `PAPERLESS_CONSUMER_DELETE_DUPLICATES=true` retains duplicate rejection.
- `PAPERLESS_CONSUMER_POLLING_INTERVAL=10` mitigates the v3.1.3 scanner upload
  issue described in [discussion #13969](https://github.com/paperless-ngx/paperless-ngx/discussions/13969).
  Finish uploads before restarting: the initial scan processes existing files
  immediately. Polling does not guarantee safety for uploads that remain empty
  for a long time.

## Upgrade on 2026-09-11

Upgraded from `2.20.15` using the official
[v3 migration guide](https://github.com/paperless-ngx/paperless-ngx/blob/v3.1.3/docs/migration-v3.md).
A stopped-stack Restic backup covered data, media, export, consume, and a
protected copy of the previous Compose file, environment and Redis dump.

The database restore test was byte-identical and passed SQLite integrity checks.
After migration, all 631 documents and 1,893 media files remained intact; the
media files were byte-identical. Metadata, ownership and document permissions
matched the backup after accounting for the official SHA256 conversion and
the new share-link bundle permissions. All 37 migrations completed.

Server-side checks covered search, original download checksums and thumbnails.
The public login page rendered, and unauthenticated API access returned 401.
An atomic duplicate import exercised the consumer and worker: Paperless removed
the test copy and kept the document count at 631. Its duplicate rejection is the
one expected error-level log entry from the verification.
After successful verification, the two temporary upgrade snapshots and the
server-side backup and audit directory were removed at the user's request.
All 14 regular Paperless snapshots and all other service snapshots remained
unchanged; the daily backup timer remains enabled.

For future restores, use the regular Restic backups and match the application
version to the database version. Do not start v2 against a migrated v3 database.
