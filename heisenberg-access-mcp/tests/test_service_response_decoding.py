from __future__ import annotations

import gzip
import os
import unittest

import httpx

os.environ.setdefault("HEISENBERG_ACCESS_MCP_TOKEN", "test-token")

from heisenberg_access_mcp.server import limited_service_response, service_response_payload


class ServiceResponseDecodingTest(unittest.IsolatedAsyncioTestCase):
    async def test_compressed_json_stream_is_not_decoded_twice(self) -> None:
        payload = b'{"unreadcounts": []}'
        encoded_payload = gzip.compress(payload)

        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={
                    "content-type": "application/json",
                    "content-encoding": "gzip",
                    "content-length": str(len(encoded_payload)),
                },
                content=encoded_payload,
                request=request,
            )

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            response, oversized_payload = await limited_service_response(
                client,
                method="GET",
                url="https://freshrss.example.invalid/api/greader.php/reader/api/0/unread-count",
                headers={"Accept": "application/json"},
                params={"output": "json"},
                json_body=None,
                form_body=None,
                mode="auto",
            )

        self.assertIsNone(oversized_payload)
        self.assertIsNotNone(response)
        assert response is not None
        self.assertNotIn("content-encoding", response.headers)
        self.assertEqual(response.headers["content-length"], str(len(payload)))

        result = service_response_payload(
            response,
            response_mode="auto",
            resource_url="http://localhost:8020/mcp",
            artifact_metadata={
                "provider": "freshrss",
                "method": "GET",
                "path": "/reader/api/0/unread-count",
            },
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["response"], {"unreadcounts": []})


if __name__ == "__main__":
    unittest.main()
