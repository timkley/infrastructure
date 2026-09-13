# Heisenberg Access MCP

Heisenberg Access MCP is the private policy and capability gate in front of OpenBao. Agents get narrow MCP tools, not raw secrets, refresh tokens, API keys, or arbitrary Vault paths.

The MCP exposes status tools plus narrow service capabilities:

- `access_status` reports MCP readiness and the declared capability registry.
- `openbao_status` reports whether OpenBao is reachable and ready, using only safe status fields.
- `x.get_tweet(tweet_id_or_url)` reads one public tweet through server-side X OAuth, refreshes the stored OAuth token when needed, verifies that the author is not protected, and returns tweet text, author metadata, URL, created time, public metrics, and media URLs.
- `x.list_bookmarks(page_size?, pagination_token?)` reads Tim's current X bookmarks through server-side X OAuth, with pagination and tweet/media/author context for Brain ingest.
- `x.unbookmark_tweets(tweet_ids_or_urls, confirm?, dry_run?)` removes one or more X bookmarks after successful ingest. It is a mutating X write and requires `confirm=true`; use `dry_run=true` to inspect the target IDs without writing.
- `google_health.access_status` refreshes a Google access token server-side and calls the harmless Google Health API v4 `users/me/identity` endpoint to report access/scope status without returning health datapoints.
- `google_health.list_data_types` documents the Google Health API v4 fitness, activity, workout, sleep, health metrics, nutrition, route/location, TCX, and required OAuth scopes exposed by the explicit tool allowlists.
- `google_health.get_activity_data_points(...)` reads paginated allowlisted activity datapoints such as steps, distance, calories, active minutes, heart-rate zones, heart rate, altitude, VO2, and exercise/workout sessions.
- `google_health.get_exercise_data_points(...)` reads paginated Google Health exercise/workout datapoints for a civil date range, with `page_size` capped at 25.
- `google_health.export_exercise_tcx(...)` exports a single workout route/location TCX file as a private runtime artifact; it returns artifact metadata only.
- `google_health.get_sleep_data_points(...)` and `google_health.summarize_sleep_day(date)` read sleep sessions, sleep summaries, and sleep-stage context through the readonly sleep scope.
- `google_health.get_health_metric_data_points(...)` and `google_health.summarize_health_day(date)` read allowlisted health metrics such as heart rate, resting HR, HRV, oxygen saturation / SpO2, respiratory rate, weight, body fat, temperature, and blood glucose.
- `google_health.summarize_activity_day(date)` returns a compact daily log summary for steps, calories, distance, active minutes, heart-rate zones, altitude, floors, VO2, heart rate, and workouts without dumping large raw health responses.
- `google_health.log_meal(timestamp, meal_type, items, confirm?, dry_run?)` stores one anonymous Google Health nutrition item per explicitly supplied food. The calling agent must provide the local timestamp with UTC offset, Google meal type, amount-bearing display names, and all core macros; the MCP does not estimate them.
- `google_health.get_nutrition_day(date)` returns individual foods with correction IDs, meal groups, and daily totals. `google_health.get_nutrition_range(start_date, end_date)` returns compact daily and meal totals without raw items.
- `google_health.correct_nutrition_item(data_point_id, changes, confirm?, dry_run?)` partially corrects one anonymous item by deleting and recreating it with a new ID. `google_health.delete_nutrition_items(data_point_ids, confirm?, dry_run?)` batch-deletes concrete nutrition IDs.
- `elevenlabs.text_to_speech(...)` creates speech through server-side ElevenLabs credentials after explicit `confirm=true` and stores the audio as a private runtime artifact. The MCP response returns metadata only: `artifact_id`, `mime_type`, `byte_size`, `sha256`, `created_at`, `voice_id`, `model_id`, `output_format`, and private download instructions.
- `elevenlabs.speech_to_text(input_artifact_id, num_speakers=10, language_code="deu", confirm=false, dry_run=false)` transcribes a private uploaded M4A with Scribe v2. It always requests diarization, word timestamps, and audio-event tags. The maximum configured speaker count is 10. The full JSON transcript is stored as a private artifact; MCP returns compact metadata and download instructions only.
- `elevenlabs.request(...)` is a service-scoped ElevenLabs request tool for `https://api.elevenlabs.io`. The API key is never returned. Known binary responses are stored as private artifacts, large JSON is redacted before artifact storage, and large text-like responses are refused.
- `homeassistant.request(...)` is a service-scoped Home Assistant request tool for the `url` configured in OpenBao.
- `freshrss.request(...)` is a service-scoped FreshRSS request tool for the `api_url` configured in OpenBao.
- `tandoor.request(...)` is a service-scoped Tandoor request tool for the `url` configured in OpenBao.

It intentionally does not provide a `read_secret(path)` style tool.

## Paperless private

This deployment exposes only the private Paperless instance. The work deployment
lives separately in the work infrastructure repository under
`docker/heisenberg-work-mcp`. The assistant workspace can connect to both; each
deployment keeps its own credentials, tools and network configuration. Separate
MCPs do not separate information inside a conversation that may call both.

The private server reads `secret/data/heisenberg/paperless` through its existing
OpenBao connection. Store only these fields in that secret:

```json
{ "url": "https://paperless.timkley.dev", "api_token": "..." }
```

Use the URL of the private instance and a dedicated non-superuser API account.
Grant the required global view/change permissions and matching rights for the
intended documents. Grant view rights for tags, correspondents and document types
so the MCP can resolve their names. Keep document ownership unchanged. The account
needs `delete_document`, `add_correspondent`, and `add_documenttype` for the
explicit write tools,
but no upload, ownership-change or administration rights. Paperless
enforces its account permissions independently of the MCP's field allowlist.
Never put the work instance's credentials in this deployment.

OpenBao still grants only read access to this secret: reading an API token does
not limit that token to read-only requests against Paperless.

The configured URL must match the private endpoint above. The adapter rejects
a different endpoint before making a request, so swapped instance settings fail
closed. Moving Paperless to another address requires changing the reviewed
`PAPERLESS_BASE_URL` constant as well as the secret.

These tools make only GET requests:

- `paperless.search_documents(query, page=1, page_size=10)` returns compact search
  results, without OCR text. Page size is capped at 25.
- `paperless.get_document(document_ref)` returns selected document metadata.
- `paperless.read_document(document_ref, offset=0, limit=8000)` reads a bounded
  section of the current OCR text. Follow its continuation fields for more text.
- `paperless.list_metadata(kind, query="", page=1, page_size=25)` resolves names
  to IDs for `tags`, `correspondents` or `document_types`.

`paperless.update_document(document_ref, changes, confirm=false, dry_run=false)`
updates one document's details. Allowed fields are `title` (up to 128 characters),
`created` (`YYYY-MM-DD`), `correspondent`, `document_type`, `tags` and
`archive_serial_number` (0 through 4294967295). Relation values use IDs from the
lookup tool; `null` clears a correspondent, document type or archive number.
`tags` replaces the entire tag list; an empty list removes all tags.

Use `dry_run=true` for a read-only before/after preview. An actual change needs
`confirm=true` and a user request covering the document and intended edits.
The flag records caller intent; it is not an independent proof of human approval.
The tool sends one PATCH, then reads the document again and verifies the changed
fields. It does not retry writes. If it reports `paperless_update_outcome_uncertain`,
read the document before deciding whether another write is needed.

`paperless.delete_document(document_ref, confirm=false, dry_run=false)` moves
one document to the Paperless trash via its normal DELETE endpoint. It requires
`confirm=true`; `dry_run=true` reads a preview only. A successful write requires
HTTP 204 and a subsequent HTTP 404 on the active document endpoint. The result
reports that absence and states that trash membership itself was not verified:
Paperless can hide foreign-owned trash records from non-superuser accounts.
Restoration remains possible until Paperless permanently empties that entry.
The MCP has no permanent deletion or trash-emptying endpoint.

`paperless.create_correspondent(name, confirm=false, dry_run=false)` accepts a
name of at most 128 characters. It first checks for existing names without
regard to letter case. An exact match is reused; ambiguous or differently
capitalized matches stop without writing. Otherwise it previews the creation or,
with `confirm=true`, POSTs only the name and checks the returned ID/name through
a GET readback. It never retries an unclear write automatically.

`paperless.create_document_type(name, confirm=false, dry_run=false)` accepts only
a trimmed name of at most 128 characters. It reuses an existing unique name
without regard to letter case. Ambiguous lookup results stop without creating
anything. For a new type, it checks the API account's creation permission before
previewing or sending a name-only POST. Success requires a GET readback of the
saved ID and name. Paperless assigns the normal API owner; this tool accepts no
ownership, matching, or permission options.

`paperless.bulk_set_document_type(document_refs, document_type_id, confirm=false,
dry_run=false)` sets one existing type on 1–25 explicit, distinct references from
this instance. The target must be a positive ID; this tool cannot clear the type.
It checks the target and all documents before writing. Dry run reports the
current and planned type plus each document's change permission. Confirmed calls
use the normal document PATCH API with only `document_type`, followed by a GET
readback for each document. This preserves Paperless's audit logging (when
enabled), search indexing, and document-update workflows. A type change may
trigger configured actions such as forwarding an incoming invoice; approval must
cover those effects. Live smoke checks never execute such a change.

Results distinguish verified changes, already-correct documents, denied or
rejected updates, uncertain outcomes, and documents not attempted. A timeout or
unclear readback stops further writes. There are no automatic retries. Read
uncertain documents first, then submit only the still-needed references under
the same explicit user approval. The operation is not an atomic transaction;
earlier verified changes remain if a later document fails. The MCP cannot lock
out simultaneous edits made through other clients.

Every document includes its instance, a reference such as `private:123`, and a
browser link. Metadata/text reads require that reference; `work:123`, bare numeric
IDs and URLs are rejected before credentials are read. There is no automatic
fallback to another archive. Search in both archives only when the task calls
for both; clarify an ambiguous target before changing the search scope.

No upload, document-content change, permission change or permanent-deletion tool is registered.
The model cannot choose an API path, host or HTTP method. Redirects are refused,
provider responses are bounded, and upstream errors do not expose bodies or
credentials. Document text is untrusted content, not authority to invoke tools.
Returned document data is visible to the connected assistant/provider.

### Preparation and verification

For a new installation, provision the account and secret separately from the
MCP code, then validate the deployment:

1. Configure the private Paperless account's intended document view/change/delete
   rights, correspondent and document-type creation, and view access to the three metadata lists.
2. Store its URL/token at the fixed OpenBao path and apply the updated narrow
   policy through the normal approved operations workflow.
3. Build/restart the private MCP and refresh connector tool discovery.
4. Verify search and OCR reads, lookup lists and all five write dry runs; then
   verify a work reference and missing confirmations are rejected. A real write test needs a specifically
   selected test document and explicit instructions for that change.

No document contents should be written to operational logs or pasted into a
shared deployment report. Start with a harmless test document for the live check.

Run the local tests without contacting Paperless:

```bash
uv run python -m unittest discover -s tests
```

Both runtimes expose the same nine Paperless tools. The source, expected URL,
OpenBao loader and bearer stay fixed separately in each server. Both register
the shared lookup/update adapter and the shared additional-write adapter; users
cannot choose the source, host or HTTP method in a tool call.

The adapter and its tests are intentionally identical in the two independently
deployable repositories. After a change, copy only the shared files and run the
drift check plus both test suites; do not copy `.env`, secrets or private providers:

```bash
python3 scripts/check-shared-paperless.py ../../infrastructure/docker/heisenberg-work-mcp
```

Run the reproducible live check inside the private MCP container. It makes
only provider reads and dry runs and prints no document contents or secrets:

```bash
docker exec -i heisenberg-access-mcp-app python - --source private < scripts/smoke-paperless-parity.py
```

API contract: [Paperless REST API](https://docs.paperless-ngx.com/api/) and
[Paperless permissions](https://docs.paperless-ngx.com/usage/#permissions).

### Live-Abgleich am 12. September 2026

Privat und Work stellen dieselben sieben Paperless-Tools bereit, einschließlich
`paperless.delete_document` und `paperless.create_correspondent`. Die private
Installation umfasst insgesamt 33 Tools; die übrigen privaten Anbieter bleiben
vorhanden. Das ChatGPT-Plugin „Heisenberg Lando“ wurde aktualisiert und zeigt die
beiden neuen Aktionen. Ein Aufruf über den OpenAI-Tunnel bestätigt den gleichen
Paperless-Funktionsumfang wie Work.

Beide Adapter, beide gemeinsamen Testdateien und das Vergleichsskript sind
bytegleich (5/5). Lokale Tests: Privat 80/80, Work 60/60. Live bestanden in beiden
Instanzen: Suche, Details, OCR, Metadaten, alle drei Schreibvorschauen,
Bestätigungspflicht, Ablehnung fremder Instanzreferenzen und Bearer-Prüfung.
Dokument und Korrespondentenzahl blieben bei den Tests gleich; es erfolgte kein
Paperless-Schreibaufruf. Beide MCP-Container sind gesund.

Das vorhandene private Tokenkonto hatte bereits `delete_document` und
`add_correspondent` sowie Löschzugriff auf alle für dieses Konto sichtbaren
Dokumente. Zugangsdaten, Gruppen, Eigentümer und Rechte wurden nicht geändert.
Private und berufliche OpenBao-Pfade bleiben getrennt.

Rollback-Image auf Lando: `heisenberg-access-mcp:before-parity-20260912`.
Die zuvor geänderten Dateien und die Liste neuer Dateien liegen unter
`/home/admin/.cache/heisenberg-mcp-deploy/private-before-parity-20260912/`.

## Network and Auth

- The service is not published through Traefik (`traefik.enable=false`).
- Docker binds it to `127.0.0.1:8020` by default.
- Set `HEISENBERG_ACCESS_MCP_BIND_ADDR` to Lando's Tailscale IP only when direct tailnet access is wanted.
- When using Tailscale/MagicDNS directly from Codex, set `HEISENBERG_ACCESS_MCP_RESOURCE_URL=http://lando:8020/mcp`, add `lando:8020` and Lando's Tailscale IP to `HEISENBERG_ACCESS_MCP_ALLOWED_HOSTS`, and configure Codex with the same URL.
- MCP calls require `Authorization: Bearer <HEISENBERG_ACCESS_MCP_TOKEN>`.
- Runtime artifacts are served from `/artifacts/<artifact_id>` on the same private HTTP service and require the same bearer token. The service remains `traefik.enable=false`; do not expose artifacts publicly.
- OpenBao is reached on Docker's internal `openbao_default` network at `http://openbao-app:8200`.

## ElevenLabs Artifacts

`elevenlabs.text_to_speech` does not return audio bytes or base64 through MCP. Audio is written under the non-versioned runtime directory configured by `HEISENBERG_ACCESS_MCP_ARTIFACT_DIR`, backed by the Docker named volume `artifacts`.

The tool response includes a private download URL. Clients fetch it with the same MCP bearer token:

```bash
curl -H "Authorization: Bearer $HEISENBERG_ACCESS_MCP_TOKEN" \
  "http://lando:8020/artifacts/<artifact_id>" \
  --output speech.mp3
```

Large artifacts above the server download limit are refused before storage instead of streamed through MCP. Small JSON/text ElevenLabs responses can be returned directly by `elevenlabs.request`; malformed JSON is refused, large JSON responses are redacted before artifact storage, and large text-like responses are refused because reliable secret redaction is not guaranteed. Large list-style operations should be configured with provider-side pagination or replaced by a summarizing capability.

Use dedicated tools, such as `elevenlabs.text_to_speech`, for binary or high-level workflows where the server should manage artifacts and metadata deliberately.

### Upload and transcribe an M4A

Speech-to-text input is uploaded directly to the private HTTP service, not passed through MCP as bytes or base64. The upload endpoint requires the same bearer token, an explicit supported audio MIME type, a safe `.m4a` filename, `Content-Length`, and a basic ISO-BMFF `ftyp` signature. It streams files up to 500 MiB into the private artifact volume.

```bash
curl --fail-with-body \
  -X POST \
  -H "Authorization: Bearer $HEISENBERG_ACCESS_MCP_TOKEN" \
  -H "Content-Type: audio/mp4" \
  -H "X-Artifact-Filename: session.m4a" \
  --data-binary @session.m4a \
  "http://lando:8020/artifacts/uploads/audio"
```

The response contains only the private input `artifact_id` and integrity metadata. First validate the planned provider request with `elevenlabs.speech_to_text(..., dry_run=true)`. Before a billable call, the server verifies the stored file against that SHA-256. The real provider call requires `confirm=true`, uses `POST https://api.elevenlabs.io/v1/speech-to-text` with `model_id=scribe_v2`, and may run for several hours for long recordings. Provider responses are streamed with a 100 MiB hard limit and must contain valid JSON before finalization. After the transcript artifact is finalized, its input audio and metadata are removed; failed or retryable calls preserve the input. Download the resulting JSON artifact with the same authenticated `/artifacts/<artifact_id>` flow shown above. Neither endpoint returns audio or transcript contents through MCP.

## X Bookmarks

X tools use the server-side OAuth token in `secret/data/heisenberg/x/oauth`. Agents never receive the raw access or refresh token. The token should include the X OAuth scopes needed for the workflows:

- `tweet.read`
- `users.read`
- `bookmark.read`
- `bookmark.write` for unbookmarking
- `offline.access` for refresh-token rotation

The X Bookmarks API requires the authenticated X user ID in the path. The MCP first uses `user_id`, `x_user_id`, `authenticated_user_id`, or `user.id` from the OpenBao secret when present. If it is absent, it calls X `/2/users/me` server-side, then caches `user_id` as non-secret metadata back to the same OpenBao secret.

Useful tools:

- `x.list_bookmarks(page_size?, pagination_token?)` calls `GET /2/users/{id}/bookmarks`, caps `page_size` at 100, and returns `tweets`, `result_count`, and `next_token`. Use `next_token` as the next call's `pagination_token`.
- `x.unbookmark_tweets(tweet_ids_or_urls, confirm?, dry_run?)` calls `DELETE /2/users/{id}/bookmarks/{tweet_id}` for each normalized tweet ID. Pass a one-item list for a single tweet. It requires `confirm=true` unless `dry_run=true`.

Brain ingest should switch away from local `~/.config/heisenberg/x-api.json` credentials and use this MCP flow instead:

1. Call `x.list_bookmarks(page_size=100)` and continue with `pagination_token` until `next_token` is empty.
2. Ingest the returned tweet objects into `raw/bookmarks/` and `wiki/sources/x/`.
3. Keep the successfully ingested tweet IDs.
4. Call `x.unbookmark_tweets(tweet_ids_or_urls=[...], dry_run=true)` for a final safety check.
5. Only after the ingest artifacts exist and the IDs match, call `x.unbookmark_tweets(..., confirm=true)`.

Do not unbookmark tweets speculatively. Failed or partially ingested tweets should remain bookmarked and be retried later.

## Google Health Fitness, Sleep, Metrics, and Nutrition

Google Health tools use the server-side OAuth client and refresh token from OpenBao. The intended scope set is:

- `https://www.googleapis.com/auth/googlehealth.activity_and_fitness.readonly`
- `https://www.googleapis.com/auth/googlehealth.sleep.readonly`
- `https://www.googleapis.com/auth/googlehealth.health_metrics_and_measurements.readonly`
- `https://www.googleapis.com/auth/googlehealth.location.readonly`
- `https://www.googleapis.com/auth/googlehealth.nutrition.readonly`
- `https://www.googleapis.com/auth/googlehealth.nutrition.writeonly`

Optional read-only scopes can be added to the same consent flow when needed: ECG, irregular rhythm notification, profile, and settings. Nutrition is the only Google Health write capability exposed by this MCP.

When changing Google Health scopes, Tim must perform a new OAuth consent and replace `secret/data/heisenberg/google-health/oauth-token` in OpenBao with the new refresh token plus metadata such as `scope_set`, `source`, and `stored_at`. Existing refresh tokens do not gain newly requested scopes automatically. If a tool returns a sanitized Google `PERMISSION_DENIED` or unsupported-data-type error, check the OpenBao `scope_set` and Google API support for that data type before changing code.

Useful tools:

- `google_health.list_data_types` explains the explicit allowlisted Google Health API v4 data types and required scopes for fitness data, exercise, workout, activity, daily log, sleep, sleep stages, health metrics, heart rate, HRV, recovery, weight, body fat, oxygen saturation / SpO2, respiratory rate, route, location, TCX, health datapoints, and date range queries. Google Health v4 does not expose a generic `users.dataTypes.list` REST endpoint, so this tool returns the documented allowlist.
- `google_health.get_activity_data_points(data_type, start_time?, end_time?, page_size?, page_token?)` reads allowlisted activity datapoints from `/v4/users/me/dataTypes/{data_type}/dataPoints`. Supported examples include `steps`, `distance`, `active-energy-burned`, `active-minutes`, `active-zone-minutes`, `heart-rate`, `time-in-heart-rate-zone`, `altitude`, `vo2-max`, `run-vo2-max`, and `exercise`.
- `google_health.get_exercise_data_points(start_time?, end_time?, page_size?, page_token?)` is a focused workout/session reader for `/v4/users/me/dataTypes/exercise/dataPoints`. It returns a sanitized `data_point_id` for optional TCX export and caps `page_size` at 25.
- `google_health.export_exercise_tcx(exercise_data_point_id, partial_data?)` calls `exportExerciseTcx?alt=media` for one exercise data point. It requires activity plus location readonly scopes, stores the TCX XML as a private artifact, and returns only artifact metadata.
- `google_health.get_sleep_data_points(start_time?, end_time?, page_size?, page_token?)` reads sleep sessions using the Google Health sleep-specific civil end time filter and caps `page_size` at 25.
- `google_health.summarize_sleep_day(date)` returns compact sleep-session summaries, sleep-stage counts, summary fields, and out-of-bed counts for one day.
- `google_health.get_health_metric_data_points(data_type, start_time?, end_time?, page_size?, page_token?)` reads allowlisted health metrics such as `heart-rate`, `daily-resting-heart-rate`, `daily-heart-rate-variability`, `heart-rate-variability`, `daily-oxygen-saturation`, `oxygen-saturation`, `daily-respiratory-rate`, `respiratory-rate-sleep-summary`, `weight`, `body-fat`, `core-body-temperature`, and `blood-glucose`.
- `google_health.summarize_health_day(date)` combines daily rollups and compact daily records for recovery and body metrics.
- `google_health.summarize_activity_day(date)` calls Google Health daily rollups for activity metrics such as `steps`, `distance`, `active-energy-burned`, `active-minutes`, `active-zone-minutes`, `time-in-heart-rate-zone`, `altitude`, `floors`, `run-vo2-max`, `heart-rate`, and `total-calories`, then adds compact exercise/workout summaries for the same date. Use this for Daily Log, Brain, and fitness data sync workflows where a concise one-day summary is better than raw datapoint dumps.

Nutrition tools deliberately use only anonymous `nutrition-log` data points. They do not use the Food or serving-unit catalogs:

- `google_health.log_meal(timestamp, meal_type, items, confirm?, dry_run?)` requires an RFC3339 timestamp with an explicit offset and one of Google's meal types: `BEFORE_BREAKFAST`, `BREAKFAST`, `BEFORE_LUNCH`, `LUNCH`, `BEFORE_DINNER`, `DINNER`, `AFTER_DINNER`, `SNACK`, or legacy `ANYTIME`. Each call accepts at most 100 items. Each item requires `display_name`, `energy_kcal`, `protein_g`, `carbohydrate_g`, and `fat_g`. Put the consumed amount directly in `display_name`, for example `Skyr, 250 g`. Optional `energy_from_fat_kcal` and `additional_nutrients_g` values map to native Google NutritionLog fields. There is no duplicate detection. Completed create operations report Google's actual `data_point_id`; an accepted but pending operation exposes only the requested ID until a read returns the Google-assigned ID.
- The calling agent must resolve the timestamp and meal type and estimate missing core values before it calls `log_meal`. The MCP never estimates or silently fills nutrition data. An explicit instruction such as “logge …” authorizes the write when `confirm=true`; an incidental food mention outside an active tracking context does not.
- `google_health.get_nutrition_day(date)` returns foods with `data_point_id`, meal groups keyed by exact `datetime + meal_type`, and day totals. Use those concrete IDs for corrections and deletion.
- `google_health.get_nutrition_range(start_date, end_date)` treats both dates as inclusive, accepts at most 90 days, and returns every requested civil day with compact meal/day totals. It intentionally omits raw food items and correction IDs.
- `google_health.correct_nutrition_item(data_point_id, changes, confirm?, dry_run?)` accepts partial changes, reads the existing anonymous item, deletes it, and creates a replacement under a new ID. It rejects catalog-backed identified foods instead of silently converting them. Google documents anonymous logs as non-editable. The two write operations are not a transaction and V1 does not roll back if recreation fails.
- `google_health.delete_nutrition_items(data_point_ids, confirm?, dry_run?)` sends Google's documented `names[]` batch-delete request for at most 10,000 concrete nutrition-log IDs.

Google create and batch-delete calls return `Operation` resources. V1 returns a sanitized accepted/pending/error summary and extracts the actual Google data point ID from completed create responses, but deliberately does not poll operations. Multi-item writes report every accepted and failed item and do not simulate a transaction.

Two documented API ambiguities remain explicit live-smoke checks: Google's prose permits anonymous logs using `foodDisplayName` plus manual nutrition values while the field table simultaneously marks `food` required; and V1 represents a meal timestamp with equal session `startTime` and `endTime`. Unit tests lock the intended payload shape, but only the post-consent live smoke test can prove that Google accepts it.

Large raw health responses are intentionally avoided. Raw reads are paginated, session reads cap `page_size` at 25, broader sample reads cap `page_size` at 100, daily summaries query one day at a time, and TCX exports become private artifacts. Verification reports should include only status, counts, keys, endpoints, and time windows rather than full health datapoints, tokens, identities, or health values.

## Service-Scoped Requests

Generic request tools are scoped to one configured service, not to arbitrary HTTP:

- `homeassistant.request` joins the caller-provided `path` onto the Home Assistant `url` from `secret/data/heisenberg/homeassistant` and adds the bearer token server-side.
- `freshrss.request` joins `path` onto the FreshRSS `api_url` from `secret/data/heisenberg/freshrss`, obtains a FreshRSS Google Reader auth token server-side, and uses it without returning it.
- `tandoor.request` joins `path` onto the Tandoor `url` from `secret/data/heisenberg/tandoor` and adds the bearer token server-side.
- `elevenlabs.request` joins `path` onto `https://api.elevenlabs.io` and adds the API key server-side.

The request tools reject absolute URLs, host changes, query strings in `path`, and `.`/`..` path traversal. Use `params` for query parameters.

Response handling defaults to `response_mode="auto"`:

- Small JSON/text responses are returned inline with sensitive fields redacted.
- Empty successful responses, such as `204 No Content`, are returned as metadata-only results with `response_empty=true`.
- Large JSON responses are redacted and stored as private artifacts; known binary responses are stored as private artifacts and returned as metadata plus download instructions.
- Malformed JSON responses are refused instead of being returned as text.
- Large text responses are refused instead of being stored raw.
- `response_mode="inline"` refuses large/binary responses instead of returning them.
- `response_mode="artifact"` stores known binary responses as artifacts and stores redacted JSON as artifacts; text-like and unknown content types are refused.

Mutating methods (`POST`, `PUT`, `PATCH`, `DELETE`) require `confirm=true`. Agents must only set that flag after Tim has explicitly approved the external write/action in the current turn. Use `dry_run=true` to inspect the target method/host/path without making the request.

## Setup on Lando

```bash
cd /home/admin/docker/heisenberg-access-mcp
cp -n .env.example .env
editor .env
docker compose up -d --build
./smoke-test.sh
```

Generate the MCP bearer token on Lando and paste it into `.env`:

```bash
openssl rand -base64 48
```

Do not commit `.env` and do not paste token values into chat threads.

## OpenBao Policy

`openbao_status` can call OpenBao's `/v1/sys/health` endpoint without an OpenBao token, but capability tools need the narrow policy in `policies/heisenberg-access-mcp.hcl`. Store the created token only in `heisenberg-access-mcp/.env` as `OPENBAO_TOKEN`.

The policy grants read access to these KV-v2 secret paths:

- `secret/data/heisenberg/homeassistant`
- `secret/data/heisenberg/freshrss`
- `secret/data/heisenberg/tandoor`
- `secret/data/heisenberg/paperless`
- `secret/data/heisenberg/elevenlabs`
- `secret/data/heisenberg/google-health/oauth-client`
- `secret/data/heisenberg/google-health/oauth-token`
- `secret/data/heisenberg/x/oauth`

It grants update access only to:

- `secret/data/heisenberg/google-health/oauth-token`
- `secret/data/heisenberg/x/oauth`

Those update capabilities are for provider token refresh/metadata only. Do not add a generic secret-reading MCP tool or a tool that returns raw API keys/tokens.

The Tandoor secret shape is explicit and minimal:

```json
{ "url": "https://tandoor.example.invalid", "api_key": "..." }
```

Example, run on Lando with a root token loaded only for this setup session:

```bash
cd /home/admin/docker/heisenberg-access-mcp
docker compose --project-directory /home/admin/docker/openbao exec -T -e BAO_TOKEN="$BAO_TOKEN" app \
  bao policy write heisenberg-access-mcp - < policies/heisenberg-access-mcp.hcl

docker compose --project-directory /home/admin/docker/openbao exec -T -e BAO_TOKEN="$BAO_TOKEN" app \
  bao token create -orphan -period=720h -policy=heisenberg-access-mcp
```

The token output is sensitive. Store only the client token in the non-versioned `.env`.

## Smoke Test

```bash
./smoke-test.sh
```

The script checks `/health`, performs an MCP initialize/list-tools/call flow through the official Python SDK client, and calls `openbao_status`.

## Capability Shape

Tools should stay explicit capabilities such as:

- `x.get_tweet`
- `x.list_bookmarks`
- `x.unbookmark_tweets`
- `google_health.access_status`
- `google_health.list_data_types`
- `google_health.get_activity_data_points`
- `google_health.get_exercise_data_points`
- `google_health.export_exercise_tcx`
- `google_health.get_sleep_data_points`
- `google_health.summarize_activity_day`
- `google_health.summarize_sleep_day`
- `google_health.get_health_metric_data_points`
- `google_health.summarize_health_day`
- `google_health.log_meal`
- `google_health.get_nutrition_day`
- `google_health.get_nutrition_range`
- `google_health.correct_nutrition_item`
- `google_health.delete_nutrition_items`
- `elevenlabs.text_to_speech`
- `elevenlabs.speech_to_text`
- `elevenlabs.request`, `homeassistant.request`, `freshrss.request`, and `tandoor.request` scoped to fixed service base URLs

Each tool should map to a narrow OpenBao policy and application-level behavior. Do not add generic secret-reading tools.
