# Immich on Lando

Immich is exposed through Traefik at `https://immich.timkley.dev`.

## First Start

1. Copy `.env.example` to `.env` on the server.
2. Set a strong alphanumeric `DB_PASSWORD`.
3. Start the stack:

   ```bash
   docker compose --project-directory /home/admin/docker/immich up -d
   ```

4. Open `https://immich.timkley.dev` and create the first admin user.

## Google Photos Migration

Use Google Takeout for Google Photos and keep the ZIP files until the import is verified.
Immich recommends `immich-go` for Google Photos Takeout imports.

For a first small test, import one Takeout ZIP or a subset before importing the full archive.

```bash
immich-go upload from-google-photos \
  --server=https://immich.timkley.dev \
  --api-key=replace-with-immich-api-key \
  --concurrent-tasks=4 \
  --client-timeout=60m \
  --pause-immich-jobs=true \
  --on-errors=continue \
  --session-tag \
  --takeout-tag \
  --manage-raw-jpeg=StackCoverRaw \
  --manage-burst=Stack \
  /path/to/takeout-*.zip
```

Do not delete anything from Google Photos until the import, album checks, and backups are verified.
