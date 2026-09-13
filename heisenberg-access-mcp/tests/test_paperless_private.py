from __future__ import annotations

import json
import os
import unittest
from unittest.mock import AsyncMock, Mock, patch

import httpx

os.environ.setdefault("HEISENBERG_ACCESS_MCP_TOKEN", "private-test-token")

from heisenberg_access_mcp.server import (
    OPENBAO_ALLOWED_SECRETS,
    OPENBAO_WRITABLE_SECRETS,
    PAPERLESS_BASE_URL,
    OpenBaoError,
    OpenBaoKV2,
    PaperlessError,
    StaticBearerTokenVerifier,
    build_mcp,
)
from heisenberg_access_mcp.paperless import register_paperless_tools


class PrivatePaperlessIntegrationTest(unittest.IsolatedAsyncioTestCase):
    async def test_static_token_is_bound_to_the_private_resource(self) -> None:
        verifier = StaticBearerTokenVerifier("private-test-token", "http://localhost:8020/mcp")
        self.assertIsNone(await verifier.verify_token("work-test-token"))
        token = await verifier.verify_token("private-test-token")
        self.assertEqual(token.resource, "http://localhost:8020/mcp")
        self.assertTrue(build_mcp().settings.auth.validate_token_resource)

    async def test_paperless_is_added_without_removing_existing_tools(self) -> None:
        mcp = build_mcp()
        names = {tool.name for tool in await mcp.list_tools()}
        self.assertEqual(len(names), 35)
        self.assertTrue({
            "paperless.search_documents", "paperless.get_document", "paperless.read_document",
            "paperless.list_metadata", "paperless.update_document",
            "paperless.delete_document", "paperless.create_correspondent",
            "paperless.create_document_type", "paperless.bulk_set_document_type",
            "google_health.log_meal", "homeassistant.request", "elevenlabs.speech_to_text",
            "x.list_bookmarks", "tandoor.request", "freshrss.request", "openbao_status",
        }.issubset(names))
        status_tool = mcp._tool_manager.get_tool("access_status")
        status = await status_tool.fn(None)
        self.assertEqual(status["source"], "private")
        self.assertIn("paperless.search_documents", status["capabilities"])
        self.assertFalse(status["capabilities"]["paperless.update_document"]["read_only"])
        self.assertFalse(status["capabilities"]["paperless.delete_document"]["read_only"])
        self.assertFalse(status["capabilities"]["paperless.create_correspondent"]["read_only"])
        self.assertFalse(status["capabilities"]["paperless.create_document_type"]["read_only"])
        self.assertFalse(status["capabilities"]["paperless.bulk_set_document_type"]["read_only"])

    async def test_private_registration_uses_only_the_private_openbao_secret(self) -> None:
        expected = {"url": "https://private.example.invalid", "api_token": "secret-value"}
        with patch("heisenberg_access_mcp.server.register_paperless_tools") as register, patch(
            "heisenberg_access_mcp.server.register_paperless_write_tools"
        ) as write_register:
            build_mcp()
        self.assertEqual(register.call_args.kwargs["source"], "private")
        self.assertIs(register.call_args.kwargs["allow_updates"], True)
        self.assertEqual(register.call_args.kwargs["expected_base_url"], PAPERLESS_BASE_URL)
        self.assertEqual(write_register.call_args.kwargs["source"], "private")
        self.assertEqual(write_register.call_args.kwargs["expected_base_url"], PAPERLESS_BASE_URL)
        self.assertEqual(PAPERLESS_BASE_URL, "https://paperless.timkley.dev")
        loader = write_register.call_args.kwargs["load_credentials"]
        with patch.object(OpenBaoKV2, "read", new=AsyncMock(return_value=expected)) as read:
            self.assertEqual(await loader(), expected)
        read.assert_awaited_once_with("paperless")
        self.assertEqual(OPENBAO_ALLOWED_SECRETS["paperless"], "heisenberg/paperless")
        self.assertNotIn("paperless", OPENBAO_WRITABLE_SECRETS)
        self.assertNotIn("paperless_work", OPENBAO_ALLOWED_SECRETS)

    async def test_openbao_failure_is_sanitized_for_the_adapter(self) -> None:
        with patch("heisenberg_access_mcp.server.register_paperless_tools") as register:
            build_mcp()
        loader = register.call_args.kwargs["load_credentials"]
        with patch.object(OpenBaoKV2, "read", new=AsyncMock(side_effect=OpenBaoError("openbao_token_missing"))):
            with self.assertRaises(PaperlessError) as error:
                await loader()
        self.assertEqual(str(error.exception), "openbao_token_missing")

    async def test_wrong_instance_reference_cannot_load_any_private_credentials(self) -> None:
        mcp = build_mcp()
        with patch.object(OpenBaoKV2, "read", new=AsyncMock()) as read:
            tool = mcp._tool_manager.get_tool("paperless.get_document")
            result = await tool.fn(document_ref="work:123")
        self.assertFalse(result["ok"])
        self.assertEqual(result["source"], "private")
        self.assertNotIn("secret-value", json.dumps(result))
        read.assert_not_awaited()

    async def test_work_ref_is_rejected_by_private_delete_before_credentials(self) -> None:
        mcp = build_mcp()
        with patch.object(OpenBaoKV2, "read", new=AsyncMock()) as read:
            tool = mcp._tool_manager.get_tool("paperless.delete_document")
            result = await tool.fn(document_ref="work:123", confirm=True)
        self.assertEqual(
            result,
            {"ok": False, "source": "private", "error": "paperless_document_ref_invalid"},
        )
        read.assert_not_awaited()

    async def test_private_reads_use_fixed_host_and_reject_swapped_work_settings(self) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json={"id": 123, "title": "Test", "content": "Example text"})

        factory = Mock(side_effect=lambda **kwargs: httpx.AsyncClient(
            transport=httpx.MockTransport(handler), **kwargs
        ))

        def register(mcp, **kwargs):
            register_paperless_tools(mcp, **kwargs, client_factory=factory)

        with patch("heisenberg_access_mcp.server.register_paperless_tools", side_effect=register):
            mcp = build_mcp()
        tool = mcp._tool_manager.get_tool("paperless.read_document")
        with patch.object(OpenBaoKV2, "read", new=AsyncMock(return_value={
            "url": PAPERLESS_BASE_URL, "api_token": "private-api-test-token",
        })) as read:
            result = await tool.fn(document_ref="private:123", limit=7)
        self.assertTrue(result["ok"])
        self.assertEqual(result["ref"], "private:123")
        self.assertEqual(result["text"], "Example")
        read.assert_awaited_once_with("paperless")
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].method, "GET")
        self.assertEqual(str(requests[0].url), f"{PAPERLESS_BASE_URL}/api/documents/123/")
        self.assertEqual(requests[0].headers["authorization"], "Token private-api-test-token")

        factory.reset_mock()
        with patch.object(OpenBaoKV2, "read", new=AsyncMock(return_value={
            "url": "https://paperless.wacg.dev", "api_token": "work-api-test-token",
        })):
            result = await tool.fn(document_ref="private:123")
        self.assertEqual(result, {"ok": False, "source": "private", "error": "paperless_instance_mismatch"})
        factory.assert_not_called()

    async def test_private_update_requires_intent_and_verifies_the_persisted_change(self) -> None:
        stored = {"id": 123, "title": "Before", "content": "Must stay out of metadata"}
        methods: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(str(request.url), f"{PAPERLESS_BASE_URL}/api/documents/123/")
            self.assertEqual(request.headers["authorization"], "Token private-api-test-token")
            methods.append(request.method)
            if request.method == "PATCH":
                self.assertEqual(json.loads(request.content), {"title": "After"})
                stored["title"] = "After"
            return httpx.Response(200, json=stored.copy())

        def register(mcp, **kwargs):
            register_paperless_tools(mcp, **kwargs, client_factory=lambda **options: httpx.AsyncClient(
                transport=httpx.MockTransport(handler), **options
            ))

        with patch("heisenberg_access_mcp.server.register_paperless_tools", side_effect=register):
            mcp = build_mcp()
        tool = mcp._tool_manager.get_tool("paperless.update_document")
        with patch.object(OpenBaoKV2, "read", new=AsyncMock(return_value={
            "url": PAPERLESS_BASE_URL, "api_token": "private-api-test-token",
        })) as read:
            refused = await tool.fn(document_ref="private:123", changes={"title": "After"})
            self.assertEqual(refused["error"], "paperless_update_confirmation_required")
            read.assert_not_awaited()
            preview = await tool.fn(document_ref="private:123", changes={"title": "After"}, dry_run=True)
            self.assertTrue(preview["dry_run"])
            self.assertEqual(methods, ["GET"])
            self.assertEqual(stored["title"], "Before")
            methods.clear()
            result = await tool.fn(document_ref="private:123", changes={"title": "After"}, confirm=True)
        self.assertEqual(methods, ["GET", "PATCH", "GET"])
        self.assertTrue(result["ok"])
        self.assertTrue(result["verified"])
        self.assertEqual(result["ref"], "private:123")
        self.assertEqual(result["before"], {"title": "Before"})
        self.assertEqual(result["after"], {"title": "After"})
        self.assertNotIn("content", result)
        self.assertNotIn("Must stay out", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
