# Private Heisenberg MCP retirement

Retired on 2026-10-09 following the migration to Executor and Holocron.
The removed source is retained in Git history before the retirement commit.

Current routes:

- Google Health: personal Executor `google-health` app.
- Home Assistant: official Assist MCP in Executor, protected by Cloudflare Access
  at `https://ha-mcp.timkley.dev/api/mcp`. General REST access is no longer needed.
- ElevenLabs: Executor API app with speech generation, Scribe transcription
  (`POST /v1/speech-to-text`) and transcript retrieval.
- Paperless, Tandoor and FreshRSS: their connected Executor apps; Holocron also
  retains its existing bounded service tools.
- WhatsApp: Holocron's OAuth MCP and its separately deployed archive service.
- X: deliberately omitted.

Retirement removes only the private `heisenberg-access-mcp` container, its
OpenAI tunnel and its dedicated OpenBao token-renewal service. Provider services,
OpenBao and the separate Work-MCP remain in service. Provider secrets in OpenBao
are retained because other deployments may use them.

Private runtime backups are stored on Lando under
`/home/admin/backups/heisenberg-access-retirement-2026-10-09/` with restricted
permissions. The existing `heisenberg-access-mcp_artifacts` Docker volume is
retained and has a separate backup. No audio originals or transcripts are deleted.

The independent Paperless permission maintainer moved to
[paperless/delete-permissions](../paperless/delete-permissions/README.md).
Its existing host timer and state ledger remain active.

The ElevenLabs OpenAPI helper currently limits responses to 16 MiB and has a
30-second response timeout. Paid transcriptions retain explicit approval and
must not be automatically repeated after an uncertain response. Async mode needs
an already configured ElevenLabs webhook.
