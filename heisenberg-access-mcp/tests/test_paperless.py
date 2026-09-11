from __future__ import annotations

import json
import unittest
from collections.abc import Awaitable, Callable, Mapping

import httpx

from heisenberg_access_mcp.paperless import register_paperless_tools


class FakeMCP:
    def __init__(self) -> None:
        self.tools: dict[str, tuple[Callable[..., Awaitable[dict[str, object]]], object]] = {}

    def tool(self, *, name: str, annotations: object, **_kwargs: object) -> Callable[[Callable[..., Awaitable[dict[str, object]]]], Callable[..., Awaitable[dict[str, object]]]]:
        def register(function: Callable[..., Awaitable[dict[str, object]]]) -> Callable[..., Awaitable[dict[str, object]]]:
            self.tools[name] = (function, annotations)
            return function

        return register


def document(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "id": 123,
        "title": "Invoice",
        "content": "OCR text hidden from metadata",
        "created": "2026-09-01",
        "modified": "2026-09-02",
        "added": "2026-09-03T11:00:00Z",
        "correspondent": 7,
        "document_type": 4,
        "tags": [1, 2],
        "archive_serial_number": 99,
        "owner": "must-not-leak",
        "custom_fields": {"secret": "must-not-leak"},
    }
    value.update(overrides)
    return value


class PaperlessToolsTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.mcp = FakeMCP()
        self.credentials_calls = 0
        self.requests: list[httpx.Request] = []
        self.expected_base_url = "https://paperless.example/subpath"

        async def load_credentials() -> Mapping[str, object]:
            self.credentials_calls += 1
            return {"url": "https://paperless.example/subpath", "api_token": "top-secret-token"}

        self.load_credentials = load_credentials

    def register(self, handler: Callable[[httpx.Request], Awaitable[httpx.Response]]) -> None:
        transport = httpx.MockTransport(handler)
        register_paperless_tools(
            self.mcp,
            source="private",
            load_credentials=self.load_credentials,
            expected_base_url=self.expected_base_url,
            client_factory=lambda **kwargs: httpx.AsyncClient(transport=transport, **kwargs),
        )

    def tool(self, name: str) -> Callable[..., Awaitable[dict[str, object]]]:
        return self.mcp.tools[name][0]

    async def test_only_three_read_only_tools_register(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"count": 0, "results": []}, request=request)

        self.register(handler)
        self.assertEqual(set(self.mcp.tools), {
            "paperless.search_documents",
            "paperless.get_document",
            "paperless.read_document",
        })
        for _, annotations in self.mcp.tools.values():
            self.assertTrue(annotations.readOnlyHint)
            self.assertFalse(annotations.destructiveHint)
            self.assertTrue(annotations.openWorldHint)

    async def test_search_uses_fixed_get_endpoint_and_projects_metadata(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return httpx.Response(200, json={"count": 30, "next": "https://evil.example/api/documents/?page=2", "results": [document()]}, request=request)

        self.register(handler)
        result = await self.tool("paperless.search_documents")("tax", page=2, page_size=10)

        self.assertEqual(str(self.requests[0].url), "https://paperless.example/subpath/api/documents/?query=tax&page=2&page_size=10")
        self.assertEqual(self.requests[0].method, "GET")
        self.assertEqual(self.requests[0].headers["Authorization"], "Token top-secret-token")
        self.assertEqual(result["next_page"], 3)
        self.assertTrue(result["ok"])
        item = result["documents"][0]
        assert isinstance(item, dict)
        self.assertEqual(item["ref"], "private:123")
        self.assertEqual(item["url"], "https://paperless.example/subpath/documents/123/details/")
        self.assertNotIn("content", item)
        self.assertNotIn("owner", item)
        self.assertNotIn("custom_fields", item)

    async def test_invalid_refs_and_inputs_do_not_load_credentials(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            self.fail("request must not run")

        self.register(handler)
        for reference in ("work:123", "123", "private:0", "private:123/path"):
            result = await self.tool("paperless.get_document")(reference)
            self.assertEqual(result["error"], "paperless_document_ref_invalid")
            self.assertEqual(result["source"], "private")
        self.assertEqual((await self.tool("paperless.search_documents")("", page=1))["error"], "paperless_query_invalid")
        self.assertEqual((await self.tool("paperless.search_documents")("x", page=0))["error"], "paperless_page_invalid")
        self.assertEqual(self.credentials_calls, 0)

    async def test_configured_instance_mismatch_never_reaches_transport(self) -> None:
        transport_called = False

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal transport_called
            transport_called = True
            return httpx.Response(200, json={"count": 0, "results": []}, request=request)

        async def mismatched_credentials() -> Mapping[str, object]:
            return {"url": "https://other-paperless.example/subpath", "api_token": "top-secret-token"}

        transport = httpx.MockTransport(handler)
        register_paperless_tools(
            self.mcp,
            source="private",
            load_credentials=mismatched_credentials,
            expected_base_url=self.expected_base_url,
            client_factory=lambda **kwargs: httpx.AsyncClient(transport=transport, **kwargs),
        )
        result = await self.tool("paperless.search_documents")("tax")

        self.assertEqual(result, {"ok": False, "source": "private", "error": "paperless_instance_mismatch"})
        self.assertFalse(transport_called)

    async def test_get_and_read_have_fixed_endpoint_and_bounded_text(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return httpx.Response(200, json=document(content="abcdefghij"), request=request)

        self.register(handler)
        metadata = await self.tool("paperless.get_document")("private:123")
        text = await self.tool("paperless.read_document")("private:123", offset=2, limit=4)

        self.assertEqual(str(self.requests[0].url), "https://paperless.example/subpath/api/documents/123/")
        self.assertEqual(str(self.requests[1].url), "https://paperless.example/subpath/api/documents/123/")
        self.assertNotIn("content", metadata)
        self.assertTrue(metadata["ok"])
        self.assertEqual(text["text"], "cdef")
        self.assertTrue(text["ok"])
        self.assertTrue(text["has_more"])
        self.assertEqual(text["next_offset"], 6)
        self.assertEqual((await self.tool("paperless.read_document")("private:123", limit=20_001))["error"], "paperless_limit_invalid")

    async def test_missing_api_token_is_a_configuration_error_without_network(self) -> None:
        async def missing_token() -> Mapping[str, object]:
            return {"url": self.expected_base_url}

        async def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return httpx.Response(200, json=document(), request=request)

        self.load_credentials = missing_token
        self.register(handler)
        for name, argument in (
            ("paperless.search_documents", "tax"),
            ("paperless.get_document", "private:123"),
            ("paperless.read_document", "private:123"),
        ):
            with self.subTest(tool=name):
                self.assertEqual(await self.tool(name)(argument), {
                    "ok": False, "source": "private", "error": "paperless_configuration_invalid",
                })
        self.assertEqual(self.requests, [])

    async def test_redirect_malformed_and_large_responses_are_refused_without_leaks(self) -> None:
        async def redirect(request: httpx.Request) -> httpx.Response:
            return httpx.Response(302, headers={"location": "https://evil.example/"}, request=request)

        self.register(redirect)
        result = await self.tool("paperless.get_document")("private:123")
        self.assertEqual(result, {"ok": False, "source": "private", "error": "paperless_redirect_refused"})

        self.mcp = FakeMCP()
        async def malformed(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"count": "bad", "results": []}, request=request)

        self.register(malformed)
        self.assertEqual((await self.tool("paperless.search_documents")("tax"))["error"], "paperless_response_invalid")

        self.mcp = FakeMCP()
        async def oversized(request: httpx.Request) -> httpx.Response:
            return httpx.Response(403, content=b'{"api_token":"provider-secret"}', request=request)

        self.register(oversized)
        result = await self.tool("paperless.get_document")("private:123")
        self.assertEqual(result["error"], "paperless_request_failed")
        self.assertNotIn("provider-secret", json.dumps(result))
        self.assertNotIn("top-secret-token", json.dumps(result))

        self.mcp = FakeMCP()
        async def large(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"{" + (b" " * (2 * 1024 * 1024 + 1)) + b"}", request=request)

        self.register(large)
        result = await self.tool("paperless.search_documents")("tax")
        self.assertEqual(result["error"], "paperless_response_too_large")

    async def test_get_id_mismatch_and_search_overflow_are_rejected(self) -> None:
        async def mismatch(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=document(id=124), request=request)

        self.register(mismatch)
        result = await self.tool("paperless.get_document")("private:123")
        self.assertEqual(result["error"], "paperless_response_invalid")

        self.mcp = FakeMCP()
        async def boolean_id(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=document(id=True), request=request)

        self.register(boolean_id)
        self.assertEqual(
            (await self.tool("paperless.get_document")("private:1"))["error"],
            "paperless_response_invalid",
        )
        self.assertEqual(
            (await self.tool("paperless.read_document")("private:1"))["error"],
            "paperless_response_invalid",
        )

        self.mcp = FakeMCP()
        async def overflow(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={"count": 26, "results": [document(id=index) for index in range(1, 27)]},
                request=request,
            )

        self.register(overflow)
        result = await self.tool("paperless.search_documents")("tax", page_size=25)
        self.assertEqual(result["error"], "paperless_response_invalid")

        self.mcp = FakeMCP()
        async def ok(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=document(), request=request)

        self.register(ok)
        result = await self.tool("paperless.get_document")("private:12345678901234567890")
        self.assertEqual(result["error"], "paperless_document_ref_invalid")
