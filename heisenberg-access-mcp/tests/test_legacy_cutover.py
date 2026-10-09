from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
import json
import os
import unittest

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

os.environ.setdefault("HEISENBERG_ACCESS_MCP_TOKEN", "test-token")
MCP_TOKEN = os.environ["HEISENBERG_ACCESS_MCP_TOKEN"]

from heisenberg_access_mcp.server import (  # noqa: E402
    OPENBAO_ALLOWED_SECRETS,
    OpenBaoError,
    OpenBaoKV2,
    StaticBearerTokenVerifier,
    app,
)


REMAINING_TOOLS = frozenset({
    "access_status",
    "openbao_status",
    "x.get_tweet",
    "x.list_bookmarks",
    "x.unbookmark_tweets",
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
    "google_health.log_meal",
    "google_health.get_nutrition_day",
    "google_health.get_nutrition_range",
    "google_health.correct_nutrition_item",
    "google_health.delete_nutrition_items",
    "elevenlabs.request",
    "elevenlabs.text_to_speech",
    "elevenlabs.speech_to_text",
    "homeassistant.request",
})

REMOVED_TOOLS = frozenset({
    "paperless.search_documents",
    "paperless.get_document",
    "paperless.read_document",
    "paperless.list_metadata",
    "paperless.update_document",
    "paperless.delete_document",
    "paperless.create_correspondent",
    "paperless.create_document_type",
    "paperless.bulk_set_document_type",
    "freshrss.request",
    "tandoor.request",
    "whatsapp.archive_status",
    "whatsapp.list_chats",
    "whatsapp.search_messages",
    "whatsapp.get_context",
    "whatsapp.get_attachment",
})


@asynccontextmanager
async def mcp_session() -> AsyncIterator[ClientSession]:
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1:8020",
            headers={"Authorization": f"Bearer {MCP_TOKEN}"},
        ) as client:
            async with streamable_http_client(
                "http://127.0.0.1:8020/mcp",
                http_client=client,
                terminate_on_close=False,
            ) as (read_stream, write_stream, _):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    yield session


class LegacyMcpCutoverTest(unittest.IsolatedAsyncioTestCase):
    async def test_static_token_remains_bound_to_private_resource(self) -> None:
        verifier = StaticBearerTokenVerifier(
            "private-test-token", "http://localhost:8020/mcp"
        )
        self.assertIsNone(await verifier.verify_token("wrong-token"))
        token = await verifier.verify_token("private-test-token")
        self.assertIsNotNone(token)
        assert token is not None
        self.assertEqual(token.resource, "http://localhost:8020/mcp")

    async def test_actual_mcp_discovery_and_calls_exclude_migrated_tools(self) -> None:
        async with mcp_session() as session:
            discovered = await session.list_tools()
            names = {tool.name for tool in discovered.tools}
            self.assertEqual(names, REMAINING_TOOLS)

            for name in sorted(REMOVED_TOOLS):
                result = await session.call_tool(name, {})
                self.assertTrue(result.isError, name)
                self.assertIn("Unknown tool", result.content[0].text, name)

            status = await session.call_tool("access_status", {})
            self.assertFalse(status.isError)
            payload = json.loads(status.content[0].text)
            self.assertEqual(payload["source"], "private")
            self.assertFalse(
                any(
                    name.startswith(("paperless.", "freshrss.", "tandoor.", "whatsapp."))
                    for name in payload["capabilities"]
                )
            )

    async def test_removed_provider_secrets_fail_closed_before_any_read(self) -> None:
        openbao = OpenBaoKV2("http://openbao.invalid", "test-token")
        for secret_name in ("paperless", "freshrss", "tandoor"):
            with self.assertRaisesRegex(OpenBaoError, "openbao_secret_not_allowed"):
                await openbao.read(secret_name)

        self.assertEqual(
            set(OPENBAO_ALLOWED_SECRETS),
            {
                "homeassistant",
                "elevenlabs",
                "google_health_oauth_client",
                "google_health_oauth_token",
                "x_oauth",
            },
        )

    async def test_removed_whatsapp_route_is_gone_and_artifacts_remain_protected(self) -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1:8020",
        ) as client:
            removed = await client.get(
                "/whatsapp/attachments/private:invalid",
                headers={"Authorization": f"Bearer {MCP_TOKEN}"},
            )
            self.assertEqual(removed.status_code, 404)

            unauthorized = await client.get("/artifacts/missing")
            self.assertEqual(unauthorized.status_code, 401)

            missing = await client.get(
                "/artifacts/missing",
                headers={"Authorization": f"Bearer {MCP_TOKEN}"},
            )
            self.assertEqual(missing.status_code, 404)


if __name__ == "__main__":
    unittest.main()
