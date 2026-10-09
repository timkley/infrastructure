from __future__ import annotations

import asyncio
import json
import unittest
from collections.abc import Awaitable, Callable, Mapping
from unittest.mock import patch

import httpx

from heisenberg_access_mcp.paperless import PaperlessError
from heisenberg_access_mcp.paperless_writes import register_paperless_write_tools


class FakeMCP:
    def __init__(self) -> None:
        self.tools: dict[str, Callable[..., Awaitable[dict[str, object]]]] = {}
        self.annotations: dict[str, object] = {}

    def tool(self, *, name: str, **kwargs: object) -> Callable[[Callable[..., Awaitable[dict[str, object]]]], Callable[..., Awaitable[dict[str, object]]]]:
        def register(function: Callable[..., Awaitable[dict[str, object]]]) -> Callable[..., Awaitable[dict[str, object]]]:
            self.tools[name] = function
            self.annotations[name] = kwargs["annotations"]
            return function
        return register


class PaperlessWriteToolsTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.mcp = FakeMCP()
        self.requests: list[httpx.Request] = []
        self.credentials_calls = 0

        async def credentials() -> Mapping[str, object]:
            self.credentials_calls += 1
            return {"url": "https://paperless.wacg.dev", "api_token": "work-token"}

        self.credentials = credentials

    def register(self, handler: Callable[[httpx.Request], Awaitable[httpx.Response]]) -> None:
        transport = httpx.MockTransport(handler)
        register_paperless_write_tools(
            self.mcp,
            source="work",
            load_credentials=self.credentials,
            expected_base_url="https://paperless.wacg.dev",
            client_factory=lambda **kwargs: httpx.AsyncClient(transport=transport, **kwargs),
        )

    def tool(self, name: str) -> Callable[..., Awaitable[dict[str, object]]]:
        return self.mcp.tools[name]

    async def delete_preview(self, document_ref: str = "work:123") -> str:
        preview = await self.tool("paperless.delete_document")(document_ref, dry_run=True)
        self.assertTrue(preview["ok"])
        self.assertTrue(preview["dry_run"])
        self.assertTrue(preview["would_move_to_trash"])
        self.assertFalse(preview["delete_permission_verified"])
        self.assertEqual(preview["expires_in_seconds"], 900)
        self.assertIsInstance(preview["preview_token"], str)
        return preview["preview_token"]

    @staticmethod
    def document() -> dict[str, object]:
        return {"id": 123, "title": "Work invoice"}

    @staticmethod
    def correspondent(identifier: int = 8, name: str = "Acme GmbH") -> dict[str, object]:
        return {"id": identifier, "name": name}

    async def test_registers_exactly_four_fixed_source_write_tools(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            self.fail(f"request must not run: {request.url}")

        self.register(handler)
        self.assertEqual(set(self.mcp.tools), {
            "paperless.delete_document",
            "paperless.create_correspondent",
            "paperless.create_document_type",
            "paperless.bulk_set_document_type",
        })
        self.assertTrue(self.mcp.annotations["paperless.delete_document"].destructiveHint)
        self.assertFalse(self.mcp.annotations["paperless.create_correspondent"].destructiveHint)
        self.assertTrue(self.mcp.annotations["paperless.bulk_set_document_type"].destructiveHint)
        private_mcp = FakeMCP()
        register_paperless_write_tools(
            private_mcp, source="private", load_credentials=self.credentials,
            expected_base_url="https://paperless.timkley.dev",
        )
        self.assertEqual(set(private_mcp.tools), set(self.mcp.tools))
        with self.assertRaisesRegex(PaperlessError, "paperless_source_not_allowed"):
            register_paperless_write_tools(
                FakeMCP(), source="other", load_credentials=self.credentials,
                expected_base_url="https://paperless.wacg.dev",
            )

    async def test_private_source_rejects_work_ref_and_swapped_host_before_http(self) -> None:
        credentials_calls = 0

        async def private_credentials() -> Mapping[str, object]:
            nonlocal credentials_calls
            credentials_calls += 1
            return {"url": "https://paperless.wacg.dev", "api_token": "work-token"}

        def client_factory(**kwargs: object) -> httpx.AsyncClient:
            self.fail("HTTP must not run")

        private_mcp = FakeMCP()
        register_paperless_write_tools(
            private_mcp,
            source="private",
            load_credentials=private_credentials,
            expected_base_url="https://paperless.timkley.dev",
            client_factory=client_factory,
        )
        wrong_ref = await private_mcp.tools["paperless.delete_document"]("work:123", confirm=True)
        self.assertEqual(
            wrong_ref,
            {"ok": False, "source": "private", "error": "paperless_document_ref_invalid"},
        )
        self.assertEqual(credentials_calls, 0)
        wrong_bulk_ref = await private_mcp.tools["paperless.bulk_set_document_type"](
            ["work:123"], 4, confirm=True,
        )
        self.assertEqual(
            wrong_bulk_ref,
            {"ok": False, "source": "private", "error": "paperless_document_ref_invalid"},
        )
        self.assertEqual(credentials_calls, 0)
        swapped_host = await private_mcp.tools["paperless.delete_document"]("private:123", dry_run=True)
        self.assertEqual(
            swapped_host,
            {"ok": False, "source": "private", "error": "paperless_instance_mismatch"},
        )
        self.assertEqual(credentials_calls, 1)
        for tool_name, arguments in (
            ("paperless.create_document_type", {"name": "Invoice", "dry_run": True}),
            ("paperless.bulk_set_document_type", {"document_refs": ["private:123"], "document_type_id": 4, "dry_run": True}),
        ):
            with self.subTest(tool_name=tool_name):
                result = await private_mcp.tools[tool_name](**arguments)
                self.assertEqual(
                    result,
                    {"ok": False, "source": "private", "error": "paperless_instance_mismatch"},
                )
        self.assertEqual(credentials_calls, 3)

    async def test_invalid_or_unconfirmed_inputs_make_no_http_request(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            self.fail(f"request must not run: {request.url}")

        self.register(handler)
        cases = (
            ("paperless.delete_document", {"document_ref": "private:123"}, "paperless_document_ref_invalid"),
            ("paperless.delete_document", {"document_ref": "work:123"}, "paperless_delete_confirmation_required"),
            ("paperless.delete_document", {"document_ref": "work:123", "confirm": True}, "paperless_delete_preview_required"),
            ("paperless.create_correspondent", {"name": " "}, "paperless_correspondent_name_invalid"),
            ("paperless.create_correspondent", {"name": "x" * 129}, "paperless_correspondent_name_invalid"),
            ("paperless.create_correspondent", {"name": "Acme GmbH"}, "paperless_create_correspondent_confirmation_required"),
            ("paperless.create_document_type", {"name": "Type"}, "paperless_create_document_type_confirmation_required"),
            ("paperless.create_document_type", {"name": " "}, "paperless_document_type_name_invalid"),
            ("paperless.create_document_type", {"name": "x" * 129}, "paperless_document_type_name_invalid"),
            ("paperless.create_document_type", {"name": "Invoice", "dry_run": 1}, "paperless_dry_run_invalid"),
            ("paperless.bulk_set_document_type", {"document_refs": ["work:123"], "document_type_id": 4}, "paperless_bulk_set_document_type_confirmation_required"),
            ("paperless.bulk_set_document_type", {"document_refs": ["private:123"], "document_type_id": 4, "confirm": True}, "paperless_document_ref_invalid"),
            ("paperless.bulk_set_document_type", {"document_refs": ["work:123"], "document_type_id": True, "confirm": True}, "paperless_document_type_id_invalid"),
            ("paperless.bulk_set_document_type", {"document_refs": [], "document_type_id": 4, "confirm": True}, "paperless_bulk_document_refs_invalid"),
            ("paperless.bulk_set_document_type", {"document_refs": ["work:123", "work:123"], "document_type_id": 4, "confirm": True}, "paperless_bulk_document_refs_invalid"),
            ("paperless.bulk_set_document_type", {"document_refs": ["work:123"] * 26, "document_type_id": 4, "confirm": True}, "paperless_bulk_document_refs_invalid"),
            ("paperless.bulk_set_document_type", {"document_refs": ["work:123"], "document_type_id": None, "confirm": True}, "paperless_document_type_id_invalid"),
            ("paperless.bulk_set_document_type", {"document_refs": ["work:123"], "document_type_id": -1, "confirm": True}, "paperless_document_type_id_invalid"),
            ("paperless.bulk_set_document_type", {"document_refs": ["work:123"], "document_type_id": 4, "confirm": True, "dry_run": 1}, "paperless_dry_run_invalid"),
        )
        for name, arguments, error in cases:
            with self.subTest(name=name, error=error):
                result = await self.tool(name)(**arguments)
                self.assertEqual(result, {"ok": False, "source": "work", "error": error})
        self.assertEqual(self.credentials_calls, 0)

    async def test_delete_dry_run_reads_once_and_never_deletes(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return httpx.Response(200, json=self.document(), request=request)

        self.register(handler)
        await self.delete_preview()
        self.assertEqual([request.method for request in self.requests], ["GET"])

    async def test_delete_success_proves_active_document_absent_but_not_trash_membership(self) -> None:
        responses = iter([(200, self.document()), (200, self.document()), (204, None), (404, None)])

        async def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            status, payload = next(responses)
            return httpx.Response(status, json=payload, request=request) if payload is not None else httpx.Response(status, request=request)

        self.register(handler)
        token = await self.delete_preview()
        result = await self.tool("paperless.delete_document")("work:123", confirm=True, preview_token=token)
        self.assertTrue(result["ok"])
        self.assertEqual(result["deletion_mode"], "trash")
        self.assertTrue(result["active_document_absent"])
        self.assertFalse(result["trash_membership_verified"])
        self.assertEqual([request.method for request in self.requests], ["GET", "GET", "DELETE", "GET"])
        self.assertEqual(str(self.requests[2].url), "https://paperless.wacg.dev/api/documents/123/")

    async def test_delete_redirect_or_wrong_readback_is_uncertain_without_retry(self) -> None:
        for responses in (
            [(200, self.document()), (200, self.document()), (302, None)],
            [(200, self.document()), (200, self.document()), (204, None), (200, self.document())],
        ):
            with self.subTest(responses=responses):
                self.requests = []
                sequence = iter(responses)

                async def handler(request: httpx.Request) -> httpx.Response:
                    self.requests.append(request)
                    status, payload = next(sequence)
                    return httpx.Response(status, json=payload, request=request) if payload is not None else httpx.Response(status, request=request)

                self.mcp = FakeMCP()
                self.register(handler)
                token = await self.delete_preview()
                result = await self.tool("paperless.delete_document")("work:123", confirm=True, preview_token=token)
                self.assertEqual(result, {"ok": False, "source": "work", "error": "paperless_delete_outcome_uncertain"})

    async def test_delete_preview_is_bound_to_ref_and_current_metadata(self) -> None:
        responses = iter([
            self.document(),
            {**self.document(), "modified": "2026-09-18T10:00:00Z"},
        ])

        async def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return httpx.Response(200, json=next(responses), request=request)

        self.register(handler)
        token = await self.delete_preview()
        wrong_ref = await self.tool("paperless.delete_document")(
            "work:124", confirm=True, preview_token=token,
        )
        self.assertEqual(
            wrong_ref,
            {"ok": False, "source": "work", "error": "paperless_delete_preview_wrong_ref"},
        )
        stale = await self.tool("paperless.delete_document")(
            "work:123", confirm=True, preview_token=token,
        )
        self.assertEqual(
            stale,
            {"ok": False, "source": "work", "error": "paperless_delete_preview_stale"},
        )
        self.assertEqual([request.method for request in self.requests], ["GET", "GET"])

    async def test_delete_preview_expiry_reuse_and_source_reject_without_http(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            if request.method == "DELETE":
                return httpx.Response(403, content=b"<html>forbidden</html>", request=request)
            return httpx.Response(200, json=self.document(), request=request)

        self.register(handler)
        with patch("heisenberg_access_mcp.paperless_writes.monotonic", return_value=100.0):
            expired_token = await self.delete_preview()
        with patch("heisenberg_access_mcp.paperless_writes.monotonic", return_value=1001.0):
            expired = await self.tool("paperless.delete_document")(
                "work:123", confirm=True, preview_token=expired_token,
            )
        self.assertEqual(
            expired,
            {"ok": False, "source": "work", "error": "paperless_delete_preview_expired"},
        )
        self.assertEqual([request.method for request in self.requests], ["GET"])

        token = await self.delete_preview()
        denied = await self.tool("paperless.delete_document")(
            "work:123", confirm=True, preview_token=token,
        )
        self.assertEqual(
            denied,
            {"ok": False, "source": "work", "error": "paperless_delete_permission_denied", "provider_status": 403},
        )
        reused = await self.tool("paperless.delete_document")(
            "work:123", confirm=True, preview_token=token,
        )
        self.assertEqual(
            reused,
            {"ok": False, "source": "work", "error": "paperless_delete_preview_reused"},
        )
        self.assertEqual([request.method for request in self.requests], ["GET", "GET", "GET", "DELETE"])

        private_mcp = FakeMCP()
        register_paperless_write_tools(
            private_mcp, source="private", load_credentials=self.credentials,
            expected_base_url="https://paperless.timkley.dev",
            client_factory=lambda **kwargs: self.fail("cross-source token must not make HTTP"),
        )
        wrong_source = await private_mcp.tools["paperless.delete_document"](
            "private:123", confirm=True, preview_token=token,
        )
        self.assertEqual(
            wrong_source,
            {"ok": False, "source": "private", "error": "paperless_delete_preview_wrong_source"},
        )

    async def test_concurrent_delete_confirmation_consumes_only_one_preview(self) -> None:
        confirmation_reads = 0

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal confirmation_reads
            self.requests.append(request)
            if request.method == "GET":
                confirmation_reads += 1
                if confirmation_reads > 1:
                    await asyncio.sleep(0)
                return httpx.Response(200, json=self.document(), request=request)
            if request.method == "DELETE":
                return httpx.Response(403, content=b"denied", request=request)
            self.fail(f"unexpected request: {request.method}")

        self.register(handler)
        token = await self.delete_preview()
        first, second = await asyncio.gather(
            self.tool("paperless.delete_document")("work:123", confirm=True, preview_token=token),
            self.tool("paperless.delete_document")("work:123", confirm=True, preview_token=token),
        )
        self.assertEqual(sum(request.method == "DELETE" for request in self.requests), 1)
        self.assertEqual(
            {first["error"], second["error"]},
            {"paperless_delete_permission_denied", "paperless_delete_preview_reused"},
        )

    async def test_delete_preview_expiring_during_reread_never_reaches_delete(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return httpx.Response(200, json=self.document(), request=request)

        self.register(handler)
        with patch("heisenberg_access_mcp.paperless_writes.monotonic", side_effect=[100.0, 100.0, 1001.0]):
            token = await self.delete_preview()
            result = await self.tool("paperless.delete_document")(
                "work:123", confirm=True, preview_token=token,
            )
        self.assertEqual(
            result,
            {"ok": False, "source": "work", "error": "paperless_delete_preview_expired"},
        )
        self.assertEqual([request.method for request in self.requests], ["GET", "GET"])

    async def test_delete_preview_evicted_during_reread_never_reaches_delete(self) -> None:
        evicting = False
        issuing = False

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal evicting, issuing
            self.requests.append(request)
            if request.method != "GET":
                self.fail(f"unexpected request: {request.method}")
            if evicting and not issuing:
                issuing = True
                for _ in range(256):
                    await self.tool("paperless.delete_document")("work:123", dry_run=True)
                issuing = False
                evicting = False
            return httpx.Response(200, json=self.document(), request=request)

        self.register(handler)
        token = await self.delete_preview()
        evicting = True
        result = await self.tool("paperless.delete_document")(
            "work:123", confirm=True, preview_token=token,
        )
        self.assertEqual(
            result,
            {"ok": False, "source": "work", "error": "paperless_delete_preview_invalid"},
        )
        self.assertEqual(sum(request.method == "DELETE" for request in self.requests), 0)

    async def test_create_dry_run_has_exact_lookup_and_no_post(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return httpx.Response(200, json={"count": 0, "results": []}, request=request)

        self.register(handler)
        result = await self.tool("paperless.create_correspondent")(" Acme GmbH ", dry_run=True)
        self.assertTrue(result["ok"])
        self.assertTrue(result["dry_run"])
        self.assertTrue(result["would_create"])
        self.assertEqual([request.method for request in self.requests], ["GET"])
        self.assertEqual(self.requests[0].url.params["name__iexact"], "Acme GmbH")

    async def test_create_success_posts_name_only_and_reads_back(self) -> None:
        responses = iter([
            (200, {"count": 0, "results": []}),
            (201, self.correspondent()),
            (200, self.correspondent()),
        ])

        async def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            status, payload = next(responses)
            return httpx.Response(status, json=payload, request=request)

        self.register(handler)
        result = await self.tool("paperless.create_correspondent")("Acme GmbH", confirm=True)
        self.assertEqual(result, {"ok": True, "source": "work", "id": 8, "name": "Acme GmbH", "created": True, "duplicate": False, "dry_run": False, "verified": True})
        self.assertEqual([request.method for request in self.requests], ["GET", "POST", "GET"])
        self.assertEqual(json.loads(self.requests[1].content), {"name": "Acme GmbH"})
        self.assertEqual(str(self.requests[2].url), "https://paperless.wacg.dev/api/correspondents/8/")

    async def test_create_duplicate_is_readback_without_post(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return httpx.Response(200, json={"count": 1, "results": [self.correspondent()]}, request=request)

        self.register(handler)
        result = await self.tool("paperless.create_correspondent")("Acme GmbH", confirm=True)
        self.assertEqual(result, {"ok": True, "source": "work", "id": 8, "name": "Acme GmbH", "created": False, "duplicate": True, "dry_run": False, "verified": True})
        self.assertEqual([request.method for request in self.requests], ["GET"])

    async def test_create_case_variant_or_pagination_fails_closed_without_post(self) -> None:
        for payload in (
            {"count": 1, "results": [self.correspondent(name="ACME GmbH")]},
            {"count": 2, "results": [self.correspondent()], "next": "https://paperless.wacg.dev/api/correspondents/?page=2"},
        ):
            with self.subTest(payload=payload):
                self.requests = []

                async def handler(request: httpx.Request) -> httpx.Response:
                    self.requests.append(request)
                    return httpx.Response(200, json=payload, request=request)

                self.mcp = FakeMCP()
                self.register(handler)
                result = await self.tool("paperless.create_correspondent")("Acme GmbH", confirm=True)
                self.assertEqual(result, {"ok": False, "source": "work", "error": "paperless_correspondent_duplicate_lookup_ambiguous"})
                self.assertEqual([request.method for request in self.requests], ["GET"])

    async def test_create_rejected_or_wrong_receipt_is_sanitized(self) -> None:
        for responses, error in (
            ([(200, {"count": 0, "results": []}), (403, {"detail": "no"})], "paperless_create_correspondent_rejected"),
            ([(200, {"count": 0, "results": []}), (302, None)], "paperless_create_correspondent_outcome_uncertain"),
            ([(200, {"count": 0, "results": []}), (201, {"id": 8, "name": "Wrong"})], "paperless_create_correspondent_outcome_uncertain"),
        ):
            with self.subTest(error=error):
                self.requests = []
                sequence = iter(responses)

                async def handler(request: httpx.Request) -> httpx.Response:
                    self.requests.append(request)
                    status, payload = next(sequence)
                    return httpx.Response(status, json=payload, request=request) if payload is not None else httpx.Response(status, request=request)

                self.mcp = FakeMCP()
                self.register(handler)
                result = await self.tool("paperless.create_correspondent")("Acme GmbH", confirm=True)
                self.assertEqual(result, {"ok": False, "source": "work", "error": error})

    async def test_create_timeout_or_oversize_post_is_uncertain_without_retry(self) -> None:
        for outcome in ("timeout", "oversize"):
            with self.subTest(outcome=outcome):
                self.requests = []

                async def handler(request: httpx.Request) -> httpx.Response:
                    self.requests.append(request)
                    if len(self.requests) == 1:
                        return httpx.Response(200, json={"count": 0, "results": []}, request=request)
                    if outcome == "timeout":
                        raise httpx.ReadTimeout("network", request=request)
                    return httpx.Response(201, content=b"x" * (2 * 1024 * 1024 + 1), request=request)

                self.mcp = FakeMCP()
                self.register(handler)
                result = await self.tool("paperless.create_correspondent")("Acme GmbH", confirm=True)
                self.assertEqual(result, {"ok": False, "source": "work", "error": "paperless_create_correspondent_outcome_uncertain"})
                self.assertEqual([request.method for request in self.requests], ["GET", "POST"])

    async def test_create_readback_id_or_name_mismatch_is_uncertain_without_retry(self) -> None:
        for persisted in (self.correspondent(identifier=9), self.correspondent(name="Wrong")):
            with self.subTest(persisted=persisted):
                self.requests = []
                responses = iter([
                    (200, {"count": 0, "results": []}),
                    (201, self.correspondent()),
                    (200, persisted),
                ])

                async def handler(request: httpx.Request) -> httpx.Response:
                    self.requests.append(request)
                    status, payload = next(responses)
                    return httpx.Response(status, json=payload, request=request)

                self.mcp = FakeMCP()
                self.register(handler)
                result = await self.tool("paperless.create_correspondent")("Acme GmbH", confirm=True)
                self.assertEqual(result, {"ok": False, "source": "work", "error": "paperless_create_correspondent_outcome_uncertain"})
                self.assertEqual([request.method for request in self.requests], ["GET", "POST", "GET"])

    async def test_delete_rejection_and_readback_auth_or_redirect_are_not_retried(self) -> None:
        scenarios = (
            ([(200, self.document()), (200, self.document()), (403, None)], ["GET", "GET", "DELETE"], "paperless_delete_permission_denied"),
            ([(200, self.document()), (200, self.document()), (204, None), (401, None)], ["GET", "GET", "DELETE", "GET"], "paperless_delete_outcome_uncertain"),
            ([(200, self.document()), (200, self.document()), (204, None), (302, None)], ["GET", "GET", "DELETE", "GET"], "paperless_delete_outcome_uncertain"),
        )
        for responses, methods, error in scenarios:
            with self.subTest(error=error, responses=responses):
                self.requests = []
                sequence = iter(responses)

                async def handler(request: httpx.Request) -> httpx.Response:
                    self.requests.append(request)
                    status, payload = next(sequence)
                    return httpx.Response(status, json=payload, request=request) if payload is not None else httpx.Response(status, request=request)

                self.mcp = FakeMCP()
                self.register(handler)
                token = await self.delete_preview()
                result = await self.tool("paperless.delete_document")("work:123", confirm=True, preview_token=token)
                if error == "paperless_delete_permission_denied":
                    self.assertEqual(result, {"ok": False, "source": "work", "error": error, "provider_status": 403})
                else:
                    self.assertEqual(result, {"ok": False, "source": "work", "error": error})
                self.assertEqual([request.method for request in self.requests], methods)

    @staticmethod
    def change_document(identifier: int, document_type: int | None, can_change: object = True) -> dict[str, object]:
        return {
            "id": identifier,
            "title": f"Document {identifier}",
            "document_type": document_type,
            "user_can_change": can_change,
        }

    @staticmethod
    def document_type(identifier: int = 4, name: str = "Invoice") -> dict[str, object]:
        return {"id": identifier, "name": name}

    async def test_create_document_type_dry_run_duplicate_and_permission_checks(self) -> None:
        for responses, name, expected, methods in (
            (
                [(200, {"count": 0, "results": []}), (200, {"actions": {"POST": {}}})],
                " Invoice ",
                {"ok": True, "source": "work", "name": "Invoice", "created": False, "duplicate": False, "dry_run": True, "would_create": True, "verified": False},
                ["GET", "OPTIONS"],
            ),
            (
                [(200, {"count": 1, "results": [self.document_type(name="INVOICE")]})],
                "Invoice",
                {"ok": True, "source": "work", "id": 4, "name": "INVOICE", "created": False, "duplicate": True, "dry_run": False, "verified": True},
                ["GET"],
            ),
            (
                [(200, {"count": 0, "results": []}), (200, {"actions": {}})],
                "Invoice",
                {"ok": False, "source": "work", "error": "paperless_document_type_create_permission_denied"},
                ["GET", "OPTIONS"],
            ),
        ):
            with self.subTest(expected=expected):
                self.requests = []
                sequence = iter(responses)

                async def handler(request: httpx.Request) -> httpx.Response:
                    self.requests.append(request)
                    status, payload = next(sequence)
                    return httpx.Response(status, json=payload, request=request)

                self.mcp = FakeMCP()
                self.register(handler)
                result = await self.tool("paperless.create_document_type")(name, confirm=True, dry_run=expected.get("dry_run", False))
                self.assertEqual(result, expected)
                self.assertEqual([request.method for request in self.requests], methods)
                self.assertEqual(self.requests[0].url.params["name__iexact"], "Invoice")

    async def test_create_document_type_posts_name_only_and_reads_back(self) -> None:
        responses = iter([
            (200, {"count": 0, "results": []}),
            (200, {"actions": {"POST": {}}}),
            (201, self.document_type()),
            (200, self.document_type()),
        ])

        async def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            status, payload = next(responses)
            return httpx.Response(status, json=payload, request=request)

        self.register(handler)
        result = await self.tool("paperless.create_document_type")("Invoice", confirm=True)
        self.assertEqual(result, {"ok": True, "source": "work", "id": 4, "name": "Invoice", "created": True, "duplicate": False, "dry_run": False, "verified": True})
        self.assertEqual([request.method for request in self.requests], ["GET", "OPTIONS", "POST", "GET"])
        self.assertEqual(json.loads(self.requests[2].content), {"name": "Invoice"})

    async def test_bulk_dry_run_preflights_every_document_without_patch(self) -> None:
        responses = iter([
            (200, self.document_type()),
            (200, self.change_document(123, None)),
            (200, {"id": 124, "title": "Document 124", "document_type": None}),
        ])

        async def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            status, payload = next(responses)
            return httpx.Response(status, json=payload, request=request)

        self.register(handler)
        result = await self.tool("paperless.bulk_set_document_type")(["work:123", "work:124"], 4, dry_run=True)
        self.assertFalse(result["ok"])
        self.assertTrue(result["dry_run"])
        self.assertEqual(result["status"], "preview")
        self.assertEqual([entry["outcome"] for entry in result["results"]], ["would_update", "denied"])
        self.assertEqual([request.method for request in self.requests], ["GET", "GET", "GET"])
        self.assertEqual(result["counts"]["would_update"], 1)
        self.assertEqual(result["counts"]["denied"], 1)

    async def test_bulk_known_rejection_continues_and_already_desired_skips(self) -> None:
        responses = iter([
            (200, self.document_type()),
            (200, self.change_document(123, None)),
            (200, self.change_document(124, None)),
            (200, self.change_document(125, 4)),
            (403, {"detail": "no"}),
            (204, None),
            (200, self.change_document(124, 4)),
        ])

        async def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            status, payload = next(responses)
            return httpx.Response(status, json=payload, request=request) if payload is not None else httpx.Response(status, request=request)

        self.register(handler)
        result = await self.tool("paperless.bulk_set_document_type")(["work:123", "work:124", "work:125"], 4, confirm=True)
        self.assertEqual([entry["outcome"] for entry in result["results"]], ["failed", "updated", "already_desired"])
        self.assertTrue(result["partial"])
        self.assertEqual(result["retryable_document_refs"], ["work:123"])
        self.assertEqual(result["results"][1]["after_document_type"], 4)
        self.assertEqual([request.method for request in self.requests], ["GET", "GET", "GET", "GET", "PATCH", "PATCH", "GET"])
        self.assertEqual(json.loads(self.requests[4].content), {"document_type": 4})
        self.assertEqual(json.loads(self.requests[5].content), {"document_type": 4})

    async def test_bulk_uncertain_outcome_stops_remaining_writes(self) -> None:
        responses = iter([
            (200, self.document_type()),
            (200, self.change_document(123, None)),
            (200, self.change_document(124, None)),
        ])

        async def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            if len(self.requests) == 3:
                status, payload = next(responses)
                return httpx.Response(status, json=payload, request=request)
            if len(self.requests) < 3:
                status, payload = next(responses)
                return httpx.Response(status, json=payload, request=request)
            raise httpx.ReadTimeout("network", request=request)

        self.register(handler)
        result = await self.tool("paperless.bulk_set_document_type")(["work:123", "work:124"], 4, confirm=True)
        self.assertEqual([entry["outcome"] for entry in result["results"]], ["uncertain", "not_attempted"])
        self.assertEqual(result["readback_required_document_refs"], ["work:123"])
        self.assertEqual(result["remaining_document_refs"], ["work:124"])
        self.assertEqual([request.method for request in self.requests], ["GET", "GET", "GET", "PATCH"])

    async def test_bulk_readback_wrong_document_id_is_uncertain(self) -> None:
        responses = iter([
            (200, self.document_type()),
            (200, self.change_document(123, None)),
            (204, None),
            (200, self.change_document(124, 4)),
        ])

        async def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            status, payload = next(responses)
            return httpx.Response(status, json=payload, request=request) if payload is not None else httpx.Response(status, request=request)

        self.register(handler)
        result = await self.tool("paperless.bulk_set_document_type")(["work:123"], 4, confirm=True)
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["results"][0]["outcome"], "uncertain")
        self.assertEqual(result["readback_required_document_refs"], ["work:123"])

    async def test_bulk_target_absent_or_mismatched_never_patches(self) -> None:
        for status, payload, error in (
            (404, None, "paperless_document_type_not_found"),
            (200, self.document_type(identifier=5), "paperless_response_invalid"),
        ):
            with self.subTest(error=error):
                self.requests = []

                async def handler(request: httpx.Request) -> httpx.Response:
                    self.requests.append(request)
                    return httpx.Response(status, json=payload, request=request) if payload is not None else httpx.Response(status, request=request)

                self.mcp = FakeMCP()
                self.register(handler)
                result = await self.tool("paperless.bulk_set_document_type")(["work:123"], 4, dry_run=True)
                self.assertEqual(result, {"ok": False, "source": "work", "error": error})
                self.assertEqual([request.method for request in self.requests], ["GET"])

    async def test_create_document_type_ambiguous_or_inaccessible_lookup_never_posts(self) -> None:
        for status, payload, error in (
            (200, {"count": 2, "results": [self.document_type(), self.document_type(identifier=5, name="Other")], "next": "https://paperless.wacg.dev/api/document_types/?page=2"}, "paperless_document_type_duplicate_lookup_ambiguous"),
            (403, {"detail": "no"}, "paperless_document_type_duplicate_lookup_inaccessible"),
        ):
            with self.subTest(error=error):
                self.requests = []

                async def handler(request: httpx.Request) -> httpx.Response:
                    self.requests.append(request)
                    return httpx.Response(status, json=payload, request=request)

                self.mcp = FakeMCP()
                self.register(handler)
                result = await self.tool("paperless.create_document_type")("Invoice", confirm=True)
                self.assertEqual(result, {"ok": False, "source": "work", "error": error})
                self.assertEqual([request.method for request in self.requests], ["GET"])

    async def test_create_document_type_timeout_or_bad_readback_is_uncertain_after_one_post(self) -> None:
        for outcome in ("timeout", "readback"):
            with self.subTest(outcome=outcome):
                self.requests = []

                async def handler(request: httpx.Request) -> httpx.Response:
                    self.requests.append(request)
                    if len(self.requests) == 1:
                        return httpx.Response(200, json={"count": 0, "results": []}, request=request)
                    if len(self.requests) == 2:
                        return httpx.Response(200, json={"actions": {"POST": {}}}, request=request)
                    if outcome == "timeout":
                        raise httpx.ReadTimeout("network", request=request)
                    if len(self.requests) == 3:
                        return httpx.Response(201, json=self.document_type(), request=request)
                    return httpx.Response(200, json=self.document_type(name="Wrong"), request=request)

                self.mcp = FakeMCP()
                self.register(handler)
                result = await self.tool("paperless.create_document_type")("Invoice", confirm=True)
                self.assertEqual(result, {"ok": False, "source": "work", "error": "paperless_create_document_type_outcome_uncertain"})
                self.assertEqual(sum(request.method == "POST" for request in self.requests), 1)

    async def test_bulk_readback_wrong_type_stops_remaining_writes(self) -> None:
        responses = iter([
            (200, self.document_type()),
            (200, self.change_document(123, None)),
            (200, self.change_document(124, None)),
            (204, None),
            (200, self.change_document(123, 5)),
        ])

        async def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            status, payload = next(responses)
            return httpx.Response(status, json=payload, request=request) if payload is not None else httpx.Response(status, request=request)

        self.register(handler)
        result = await self.tool("paperless.bulk_set_document_type")(["work:123", "work:124"], 4, confirm=True)
        self.assertEqual([entry["outcome"] for entry in result["results"]], ["uncertain", "not_attempted"])
        self.assertEqual([request.method for request in self.requests], ["GET", "GET", "GET", "PATCH", "GET"])

    async def test_bulk_already_desired_is_confirmed_without_patch(self) -> None:
        responses = iter([
            (200, self.document_type()),
            (200, self.change_document(123, 4)),
        ])

        async def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            status, payload = next(responses)
            return httpx.Response(status, json=payload, request=request)

        self.register(handler)
        result = await self.tool("paperless.bulk_set_document_type")(["work:123"], 4, confirm=True)
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["results"][0]["outcome"], "already_desired")
        self.assertEqual([request.method for request in self.requests], ["GET", "GET"])


if __name__ == "__main__":
    unittest.main()
