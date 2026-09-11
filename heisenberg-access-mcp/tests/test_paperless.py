from __future__ import annotations

import json
import unittest
from collections.abc import Awaitable, Callable, Mapping

import httpx

from heisenberg_access_mcp.paperless import PaperlessError, register_paperless_tools


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

    def register(
        self,
        handler: Callable[[httpx.Request], Awaitable[httpx.Response]],
        *,
        allow_updates: bool = False,
    ) -> None:
        transport = httpx.MockTransport(handler)
        register_paperless_tools(
            self.mcp,
            source="private",
            load_credentials=self.load_credentials,
            expected_base_url=self.expected_base_url,
            allow_updates=allow_updates,
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

    async def test_update_tool_registers_only_when_explicitly_enabled(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=document(), request=request)

        self.register(handler, allow_updates=True)
        self.assertEqual(set(self.mcp.tools), {
            "paperless.search_documents",
            "paperless.get_document",
            "paperless.read_document",
            "paperless.list_metadata",
            "paperless.update_document",
        })
        _, annotations = self.mcp.tools["paperless.update_document"]
        self.assertFalse(annotations.readOnlyHint)
        self.assertTrue(annotations.destructiveHint)

    async def test_allow_updates_requires_a_real_boolean_before_registration(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            self.fail("request must not run")

        transport = httpx.MockTransport(handler)
        for invalid_value in ("false", 1):
            with self.subTest(allow_updates=invalid_value):
                with self.assertRaisesRegex(PaperlessError, "paperless_allow_updates_invalid"):
                    register_paperless_tools(
                        self.mcp,
                        source="private",
                        load_credentials=self.load_credentials,
                        expected_base_url=self.expected_base_url,
                        allow_updates=invalid_value,  # type: ignore[arg-type]
                        client_factory=lambda **kwargs: httpx.AsyncClient(transport=transport, **kwargs),
                    )
        self.assertEqual(self.mcp.tools, {})

    async def test_list_metadata_uses_only_allowlisted_endpoint_and_compact_projection(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return httpx.Response(
                200,
                json={"count": 5, "results": [{"id": 7, "name": "Invoices", "colour": "#fff"}]},
                request=request,
            )

        self.register(handler, allow_updates=True)
        result = await self.tool("paperless.list_metadata")("tags", query="invoice", page=2, page_size=2)

        self.assertEqual(
            str(self.requests[0].url),
            "https://paperless.example/subpath/api/tags/?page=2&page_size=2&name__icontains=invoice",
        )
        self.assertEqual(result["items"], [{"id": 7, "name": "Invoices"}])
        self.assertEqual(result["next_page"], 3)
        self.assertNotIn("colour", json.dumps(result))

    async def test_list_metadata_rejects_unknown_kind_without_credentials(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            self.fail("request must not run")

        self.register(handler, allow_updates=True)
        result = await self.tool("paperless.list_metadata")("owners")
        self.assertEqual(result["error"], "paperless_metadata_kind_invalid")
        self.assertEqual(self.credentials_calls, 0)

    async def test_update_rejects_invalid_or_unconfirmed_input_before_credentials(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            self.fail("request must not run")

        self.register(handler, allow_updates=True)
        update = self.tool("paperless.update_document")
        for document_ref, changes, confirm, expected in (
            ("work:123", {"title": "New"}, False, "paperless_document_ref_invalid"),
            ("private:123", {"content": "forbidden"}, False, "paperless_update_changes_invalid"),
            ("private:123", {}, False, "paperless_update_changes_invalid"),
            ("private:123", {"tags": [1, 1]}, False, "paperless_update_changes_invalid"),
            ("private:123", {"created": "2026-02-30"}, False, "paperless_update_changes_invalid"),
            ("private:123", {"title": "x" * 129}, False, "paperless_update_changes_invalid"),
            ("private:123", {"title": "New"}, False, "paperless_update_confirmation_required"),
        ):
            with self.subTest(document_ref=document_ref, changes=changes):
                result = await update(document_ref, changes, confirm=confirm)
                self.assertEqual(result["error"], expected)
        self.assertEqual(self.credentials_calls, 0)

    async def test_update_dry_run_reads_once_without_patch_and_excludes_content(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return httpx.Response(200, json=document(), request=request)

        self.register(handler, allow_updates=True)
        result = await self.tool("paperless.update_document")(
            "private:123",
            {"title": "Corrected", "created": "2026-09-10", "archive_serial_number": 0},
            dry_run=True,
        )

        self.assertEqual([request.method for request in self.requests], ["GET"])
        self.assertTrue(result["ok"])
        self.assertTrue(result["dry_run"])
        self.assertFalse(result["verified"])
        self.assertEqual(result["before"]["created"], "2026-09-01")
        self.assertEqual(result["after"]["archive_serial_number"], 0)
        self.assertNotIn("content", result)
        self.assertNotIn("custom_fields", result)

    async def test_update_patches_exact_allowlist_and_verifies_readback(self) -> None:
        updated = document(
            title="Corrected",
            created="2026-09-10T00:00:00Z",
            correspondent=None,
            document_type=3,
            tags=[2, 5],
            archive_serial_number=0,
        )
        reads = 0

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal reads
            self.requests.append(request)
            if request.method == "PATCH":
                return httpx.Response(200, json={"id": 123}, request=request)
            reads += 1
            return httpx.Response(200, json=document() if reads == 1 else updated, request=request)

        self.register(handler, allow_updates=True)
        changes = {
            "title": "Corrected",
            "created": "2026-09-10",
            "correspondent": None,
            "document_type": 3,
            "tags": [5, 2],
            "archive_serial_number": 0,
        }
        result = await self.tool("paperless.update_document")("private:123", changes, confirm=True)

        self.assertEqual([request.method for request in self.requests], ["GET", "PATCH", "GET"])
        patch_request = self.requests[1]
        self.assertEqual(str(patch_request.url), "https://paperless.example/subpath/api/documents/123/")
        self.assertEqual(patch_request.headers["Content-Type"], "application/json")
        self.assertEqual(json.loads(patch_request.content), {**changes, "tags": [2, 5]})
        self.assertTrue(result["ok"])
        self.assertTrue(result["verified"])
        self.assertEqual(result["changed_fields"], list(changes))
        self.assertEqual(result["after"], {**changes, "tags": [2, 5]})
        self.assertNotIn("content", result)
        self.assertNotIn("owner", result)

    async def test_update_mismatched_readback_is_uncertain_and_does_not_retry(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            if request.method == "PATCH":
                return httpx.Response(200, json={"id": 123}, request=request)
            return httpx.Response(200, json=document(), request=request)

        self.register(handler, allow_updates=True)
        result = await self.tool("paperless.update_document")("private:123", {"title": "Corrected"}, confirm=True)

        self.assertEqual(result["error"], "paperless_update_outcome_uncertain")
        self.assertIn("Read the current document state before retrying", result["description"])
        self.assertEqual([request.method for request in self.requests], ["GET", "PATCH", "GET"])

    async def test_update_wrong_initial_document_id_prevents_patch(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return httpx.Response(200, json=document(id=124), request=request)

        self.register(handler, allow_updates=True)
        result = await self.tool("paperless.update_document")("private:123", {"title": "Corrected"}, confirm=True)

        self.assertEqual(result["error"], "paperless_response_invalid")
        self.assertEqual([request.method for request in self.requests], ["GET"])

    async def test_update_provider_rejection_is_definite_and_has_no_readback(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            if request.method == "PATCH":
                return httpx.Response(400, json={"title": ["rejected"]}, request=request)
            return httpx.Response(200, json=document(), request=request)

        self.register(handler, allow_updates=True)
        result = await self.tool("paperless.update_document")("private:123", {"title": "Corrected"}, confirm=True)

        self.assertEqual(result["error"], "paperless_update_rejected")
        self.assertEqual([request.method for request in self.requests], ["GET", "PATCH"])

    async def test_update_redirect_is_uncertain_without_following_it(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            if request.method == "PATCH":
                return httpx.Response(302, headers={"location": "https://redirected.example/"}, request=request)
            return httpx.Response(200, json=document(), request=request)

        self.register(handler, allow_updates=True)
        result = await self.tool("paperless.update_document")("private:123", {"title": "Corrected"}, confirm=True)

        self.assertEqual(result["error"], "paperless_update_outcome_uncertain")
        self.assertEqual([request.method for request in self.requests], ["GET", "PATCH"])
        self.assertTrue(all(request.url.host == "paperless.example" for request in self.requests))

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
