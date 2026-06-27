# Heisenberg Access MCP

Heisenberg Access MCP is the private policy and capability gate in front of OpenBao. Agents get narrow MCP tools, not raw secrets, refresh tokens, API keys, or arbitrary Vault paths.

The MCP exposes status tools plus narrow service capabilities:

- `access_status` reports MCP readiness and the declared capability registry.
- `openbao_status` reports whether OpenBao is reachable and ready, using only safe status fields.
- `x.get_tweet(tweet_id_or_url)` reads one public tweet through server-side X OAuth, refreshes the stored OAuth token when needed, verifies that the author is not protected, and returns tweet text, author metadata, URL, created time, public metrics, and media URLs.
- `google_health.access_status` refreshes a Google access token server-side and calls the harmless Google Health API v4 `users/me/identity` endpoint to report access/scope status without returning health datapoints.
- `elevenlabs.text_to_speech(...)` creates speech through server-side ElevenLabs credentials after explicit `confirm=true` and stores the audio as a private runtime artifact. The MCP response returns metadata only: `artifact_id`, `mime_type`, `byte_size`, `sha256`, `created_at`, `voice_id`, `model_id`, `output_format`, and private download instructions.
- `elevenlabs.request(...)` is a service-scoped ElevenLabs request tool for `https://api.elevenlabs.io`. The API key is never returned. Known binary responses are stored as private artifacts, large JSON is redacted before artifact storage, and large text-like responses are refused.
- `homeassistant.request(...)` is a service-scoped Home Assistant request tool for the `url` configured in OpenBao.
- `freshrss.request(...)` is a service-scoped FreshRSS request tool for the `api_url` configured in OpenBao.
- `tandoor.request(...)` is a service-scoped Tandoor request tool for the `url` configured in OpenBao.

It intentionally does not provide a `read_secret(path)` style tool.

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
- `google_health.access_status`
- `elevenlabs.text_to_speech`
- `elevenlabs.request`, `homeassistant.request`, `freshrss.request`, and `tandoor.request` scoped to fixed service base URLs

Each tool should map to a narrow OpenBao policy and application-level behavior. Do not add generic secret-reading tools.
