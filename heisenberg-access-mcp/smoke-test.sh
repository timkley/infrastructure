#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ -f "$SCRIPT_DIR/.env" ]]; then
  set -a
  # shellcheck source=/dev/null
  source "$SCRIPT_DIR/.env"
  set +a
fi

BASE_HOST="${HEISENBERG_ACCESS_MCP_BIND_ADDR:-127.0.0.1}"
BASE_PORT="${HEISENBERG_ACCESS_MCP_BIND_PORT:-8020}"
BASE_URL="${HEISENBERG_ACCESS_MCP_URL:-http://${BASE_HOST}:${BASE_PORT}}"
MCP_URL="${BASE_URL%/}/mcp"

if [[ -z "${HEISENBERG_ACCESS_MCP_TOKEN:-}" ]]; then
  echo "HEISENBERG_ACCESS_MCP_TOKEN is required. Set it in .env or the environment." >&2
  exit 1
fi

curl -fsS "${BASE_URL%/}/health" >/dev/null

if docker compose --project-directory "$SCRIPT_DIR" ps --status running app --quiet 2>/dev/null | grep -q .; then
  MCP_CLIENT_URL="http://127.0.0.1:8000/mcp"
  MCP_CLIENT_CMD=(docker compose --project-directory "$SCRIPT_DIR" exec -T app python)
else
  MCP_CLIENT_URL="$MCP_URL"
  if [[ -n "${PYTHON_BIN:-}" ]]; then
    MCP_CLIENT_CMD=("$PYTHON_BIN")
  elif [[ -x "$SCRIPT_DIR/.venv/bin/python" ]]; then
    MCP_CLIENT_CMD=("$SCRIPT_DIR/.venv/bin/python")
  else
    MCP_CLIENT_CMD=(python3)
  fi
fi

"${MCP_CLIENT_CMD[@]}" - "$MCP_CLIENT_URL" "$HEISENBERG_ACCESS_MCP_TOKEN" <<'PY'
import asyncio
import json
import sys

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


async def main() -> None:
    url, token = sys.argv[1], sys.argv[2]
    headers = {"Authorization": f"Bearer {token}"}

    async with streamablehttp_client(url, headers=headers) as (read_stream, write_stream, _):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            tools = await session.list_tools()
            tool_names = sorted(tool.name for tool in tools.tools)
            expected = {
                "access_status",
                "openbao_status",
                "x.get_tweet",
                "google_health.access_status",
                "google_health.list_data_types",
                "google_health.get_activity_data_points",
                "google_health.get_exercise_data_points",
                "google_health.export_exercise_tcx",
                "google_health.get_sleep_data_points",
                "google_health.summarize_activity_day",
                "google_health.summarize_sleep_day",
                "google_health.get_health_metric_data_points",
                "google_health.summarize_health_day",
                "homeassistant.request",
                "freshrss.request",
                "tandoor.request",
                "elevenlabs.request",
                "elevenlabs.text_to_speech",
            }
            missing = sorted(expected.difference(tool_names))
            if missing:
                raise SystemExit(f"Missing expected tools: {missing}; got {tool_names}")

            dotted = {name for name in tool_names if "." in name}
            if not expected.intersection(dotted):
                raise SystemExit(f"Missing expected tools: {tool_names}")

            result = await session.call_tool("openbao_status", {})
            payload = result.content[0].text
            status = json.loads(payload)
            if not status.get("reachable"):
                raise SystemExit(f"OpenBao is not reachable through MCP: {status}")

            print(json.dumps({"tools": tool_names, "openbao_status": status}, sort_keys=True))


asyncio.run(main())
PY
