from __future__ import annotations

import json
import unittest
from collections.abc import Awaitable, Callable, Mapping

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

    @staticmethod
    def document() -> dict[str, object]:
        return {"id": 123, "title": "Work invoice"}

    @staticmethod
    def correspondent(identifier: int = 8, name: str = "Acme GmbH") -> dict[str, object]:
        return {"id": identifier, "name": name}

    async def test_registers_exactly_two_fixed_source_write_tools(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            self.fail(f"request must not run: {request.url}")

        self.register(handler)
        self.assertEqual(set(self.mcp.tools), {"paperless.delete_document", "paperless.create_correspondent"})
        self.assertTrue(self.mcp.annotations["paperless.delete_document"].destructiveHint)
        self.assertFalse(self.mcp.annotations["paperless.create_correspondent"].destructiveHint)
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
        swapped_host = await private_mcp.tools["paperless.delete_document"]("private:123", dry_run=True)
        self.assertEqual(
            swapped_host,
            {"ok": False, "source": "private", "error": "paperless_instance_mismatch"},
        )
        self.assertEqual(credentials_calls, 1)

    async def test_invalid_or_unconfirmed_inputs_make_no_http_request(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            self.fail(f"request must not run: {request.url}")

        self.register(handler)
        cases = (
            ("paperless.delete_document", {"document_ref": "private:123"}, "paperless_document_ref_invalid"),
            ("paperless.delete_document", {"document_ref": "work:123"}, "paperless_delete_confirmation_required"),
            ("paperless.create_correspondent", {"name": " "}, "paperless_correspondent_name_invalid"),
            ("paperless.create_correspondent", {"name": "x" * 129}, "paperless_correspondent_name_invalid"),
            ("paperless.create_correspondent", {"name": "Acme GmbH"}, "paperless_create_correspondent_confirmation_required"),
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
        result = await self.tool("paperless.delete_document")("work:123", dry_run=True)
        self.assertTrue(result["ok"])
        self.assertTrue(result["dry_run"])
        self.assertTrue(result["would_move_to_trash"])
        self.assertEqual([request.method for request in self.requests], ["GET"])

    async def test_delete_success_proves_active_document_absent_but_not_trash_membership(self) -> None:
        responses = iter([(200, self.document()), (204, None), (404, None)])

        async def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            status, payload = next(responses)
            return httpx.Response(status, json=payload, request=request) if payload is not None else httpx.Response(status, request=request)

        self.register(handler)
        result = await self.tool("paperless.delete_document")("work:123", confirm=True)
        self.assertTrue(result["ok"])
        self.assertEqual(result["deletion_mode"], "trash")
        self.assertTrue(result["active_document_absent"])
        self.assertFalse(result["trash_membership_verified"])
        self.assertEqual([request.method for request in self.requests], ["GET", "DELETE", "GET"])
        self.assertEqual(str(self.requests[1].url), "https://paperless.wacg.dev/api/documents/123/")

    async def test_delete_redirect_or_wrong_readback_is_uncertain_without_retry(self) -> None:
        for responses in (
            [(200, self.document()), (302, None)],
            [(200, self.document()), (204, None), (200, self.document())],
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
                result = await self.tool("paperless.delete_document")("work:123", confirm=True)
                self.assertEqual(result, {"ok": False, "source": "work", "error": "paperless_delete_outcome_uncertain"})

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
            ([(200, self.document()), (403, None)], ["GET", "DELETE"], "paperless_delete_rejected"),
            ([(200, self.document()), (204, None), (401, None)], ["GET", "DELETE", "GET"], "paperless_delete_outcome_uncertain"),
            ([(200, self.document()), (204, None), (302, None)], ["GET", "DELETE", "GET"], "paperless_delete_outcome_uncertain"),
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
                result = await self.tool("paperless.delete_document")("work:123", confirm=True)
                self.assertEqual(result, {"ok": False, "source": "work", "error": error})
                self.assertEqual([request.method for request in self.requests], methods)


if __name__ == "__main__":
    unittest.main()
