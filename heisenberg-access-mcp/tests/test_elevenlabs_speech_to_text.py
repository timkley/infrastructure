from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from starlette.requests import Request

os.environ.setdefault("HEISENBERG_ACCESS_MCP_TOKEN", "test-token")

from heisenberg_access_mcp.server import (
    CapabilityError,
    MAX_AUDIO_INPUT_ARTIFACT_BYTES,
    build_mcp,
    read_artifact_metadata,
    run_elevenlabs_speech_to_text,
    store_artifact,
    store_audio_input_artifact,
    upload_audio_artifact,
)

M4A_BYTES = b"\x00\x00\x00\x18ftypM4A \x00\x00\x00\x00M4A isom"


def request_with_body(
    body: bytes,
    *,
    mime_type: str = "audio/mp4",
    filename: str = "session.m4a",
    token: str | None = None,
) -> Request:
    sent = False

    async def receive() -> dict[str, object]:
        nonlocal sent
        if sent:
            return {"type": "http.request", "body": b"", "more_body": False}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    headers = [
        (b"content-type", mime_type.encode()),
        (b"content-length", str(len(body)).encode()),
        (b"x-artifact-filename", filename.encode()),
    ]
    if token is not None:
        headers.append((b"authorization", f"Bearer {token}".encode()))
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/artifacts/uploads/audio",
            "headers": headers,
        },
        receive,
    )


class ElevenLabsAudioUploadTest(unittest.IsolatedAsyncioTestCase):
    async def test_streams_m4a_to_private_input_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"HEISENBERG_ACCESS_MCP_ARTIFACT_DIR": directory}
        ):
            artifact = await store_audio_input_artifact(request_with_body(M4A_BYTES))

            self.assertEqual(artifact["artifact_kind"], "elevenlabs_speech_input")
            self.assertEqual(artifact["byte_size"], len(M4A_BYTES))
            self.assertNotIn("content", artifact)
            self.assertEqual((Path(directory) / artifact["filename"]).read_bytes(), M4A_BYTES)

    async def test_rejects_wrong_mime_filename_and_oversized_declaration(self) -> None:
        cases = [
            request_with_body(b"x", mime_type="application/octet-stream"),
            request_with_body(b"x", filename="../session.m4a"),
        ]
        oversized = request_with_body(b"x")
        oversized.scope["headers"] = [
            (b"content-type", b"audio/mp4"),
            (b"content-length", str(MAX_AUDIO_INPUT_ARTIFACT_BYTES + 1).encode()),
            (b"x-artifact-filename", b"session.m4a"),
        ]
        cases.append(oversized)

        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"HEISENBERG_ACCESS_MCP_ARTIFACT_DIR": directory}
        ):
            for request in cases:
                with self.assertRaises(CapabilityError):
                    await store_audio_input_artifact(request)
            self.assertEqual(list(Path(directory).glob("*")), [])

    async def test_rejects_mime_spoof_without_iso_bmff_ftyp_signature(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"HEISENBERG_ACCESS_MCP_ARTIFACT_DIR": directory}
        ):
            with self.assertRaisesRegex(CapabilityError, "audio_upload_invalid_m4a_signature"):
                await store_audio_input_artifact(request_with_body(b"not-an-m4a-file"))
            self.assertEqual(list(Path(directory).glob("*")), [])

    async def test_removes_only_stale_partial_uploads(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"HEISENBERG_ACCESS_MCP_ARTIFACT_DIR": directory}
        ):
            stale = Path(directory) / ".stale.upload"
            recent = Path(directory) / ".recent.upload"
            stale.write_bytes(b"partial")
            recent.write_bytes(b"partial")
            old = time.time() - (25 * 60 * 60)
            os.utime(stale, (old, old))

            await store_audio_input_artifact(request_with_body(M4A_BYTES))

            self.assertFalse(stale.exists())
            self.assertTrue(recent.exists())

    async def test_http_upload_requires_private_bearer_token(self) -> None:
        unauthorized = await upload_audio_artifact(request_with_body(M4A_BYTES))
        self.assertEqual(unauthorized.status_code, 401)

        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {
                "HEISENBERG_ACCESS_MCP_ARTIFACT_DIR": directory,
                "HEISENBERG_ACCESS_MCP_TOKEN": "test-token",
            },
        ):
            accepted = await upload_audio_artifact(request_with_body(M4A_BYTES, token="test-token"))
        self.assertEqual(accepted.status_code, 201)
        self.assertNotIn(M4A_BYTES, accepted.body)


class FakeStreamResponse:
    def __init__(self, status_code: int, content: bytes, content_type: str) -> None:
        self.status_code = status_code
        self.content = content
        self.headers = httpx.Headers({"content-type": content_type})

    async def __aenter__(self) -> "FakeStreamResponse":
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def aiter_bytes(self):
        midpoint = max(1, len(self.content) // 2)
        yield self.content[:midpoint]
        yield self.content[midpoint:]


class FakeAsyncClient:
    captured: dict[str, object] = {}
    response_status = 200
    response_content_type = "application/json"
    response_content = json.dumps(
        {
            "language_code": "deu",
            "language_probability": 0.99,
            "text": "Willkommen.",
            "words": [
                {"text": "Willkommen", "start": 0, "end": 1, "type": "word", "speaker_id": "speaker_0"},
                {"text": ".", "start": 1, "end": 1.1, "type": "word", "speaker_id": "speaker_1"},
            ],
        }
    ).encode()

    def __init__(self, **kwargs: object) -> None:
        self.captured["client_kwargs"] = kwargs

    async def __aenter__(self) -> "FakeAsyncClient":
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    def stream(self, method: str, url: str, **kwargs: object) -> FakeStreamResponse:
        self.captured["method"] = method
        self.captured["url"] = url
        self.captured["kwargs"] = kwargs
        files = kwargs["files"]
        self.captured["uploaded_bytes"] = files["file"][1].read()
        return FakeStreamResponse(
            self.response_status,
            self.response_content,
            self.response_content_type,
        )


class ElevenLabsSpeechToTextTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        FakeAsyncClient.captured = {}
        FakeAsyncClient.response_status = 200
        FakeAsyncClient.response_content_type = "application/json"
        FakeAsyncClient.response_content = json.dumps(
            {
                "language_code": "deu",
                "language_probability": 0.99,
                "text": "Willkommen.",
                "words": [
                    {
                        "text": "Willkommen",
                        "start": 0,
                        "end": 1,
                        "type": "word",
                        "speaker_id": "speaker_0",
                    },
                    {"text": ".", "start": 1, "end": 1.1, "type": "word", "speaker_id": "speaker_1"},
                ],
            }
        ).encode()

    def create_input_artifact(self) -> dict[str, object]:
        return store_artifact(
            b"m4a-audio",
            "audio/mp4",
            {
                "artifact_kind": "elevenlabs_speech_input",
                "original_filename": "session.m4a",
            },
        )

    async def test_posts_required_multipart_and_returns_only_compact_artifact_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"HEISENBERG_ACCESS_MCP_ARTIFACT_DIR": directory}
        ):
            source = await store_audio_input_artifact(request_with_body(M4A_BYTES))
            with patch("heisenberg_access_mcp.server.httpx.AsyncClient", FakeAsyncClient):
                result = await run_elevenlabs_speech_to_text(
                    "secret-api-key",
                    "http://localhost:8020/mcp",
                    source["artifact_id"],
                    10,
                    "deu",
                    True,
                    False,
                )

            self.assertTrue(result["ok"])
            self.assertNotIn("text", result)
            self.assertNotIn("words", result)
            self.assertEqual(result["speaker_count"], 2)
            self.assertEqual(FakeAsyncClient.captured["url"], "https://api.elevenlabs.io/v1/speech-to-text")
            request_kwargs = FakeAsyncClient.captured["kwargs"]
            self.assertEqual(
                request_kwargs["data"],
                {
                    "model_id": "scribe_v2",
                    "language_code": "deu",
                    "num_speakers": "10",
                    "diarize": "true",
                    "timestamps_granularity": "word",
                    "tag_audio_events": "true",
                },
            )
            self.assertEqual(FakeAsyncClient.captured["uploaded_bytes"], M4A_BYTES)
            self.assertIsNone(read_artifact_metadata(source["artifact_id"]))
            self.assertFalse((Path(directory) / source["filename"]).exists())
            metadata = read_artifact_metadata(result["artifact_id"])
            self.assertEqual(metadata["artifact_kind"], "elevenlabs_speech_transcript")
            transcript_path = Path(directory) / metadata["filename"]
            self.assertEqual(json.loads(transcript_path.read_text())["text"], "Willkommen.")

    async def test_rejects_tampered_input_before_provider_billing(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"HEISENBERG_ACCESS_MCP_ARTIFACT_DIR": directory}
        ):
            source = await store_audio_input_artifact(request_with_body(M4A_BYTES))
            source_path = Path(directory) / source["filename"]
            source_path.write_bytes(M4A_BYTES[:-1] + b"X")

            with (
                patch("heisenberg_access_mcp.server.httpx.AsyncClient", FakeAsyncClient),
                self.assertRaisesRegex(CapabilityError, "elevenlabs_input_artifact_sha256_mismatch"),
            ):
                await run_elevenlabs_speech_to_text(
                    "secret-api-key",
                    "http://localhost:8020/mcp",
                    source["artifact_id"],
                    10,
                    "deu",
                    True,
                    False,
                )

            self.assertNotIn("url", FakeAsyncClient.captured)
            self.assertIsNotNone(read_artifact_metadata(source["artifact_id"]))

    async def test_caps_streamed_provider_response_and_preserves_input_for_retry(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"HEISENBERG_ACCESS_MCP_ARTIFACT_DIR": directory}
        ):
            source = await store_audio_input_artifact(request_with_body(M4A_BYTES))
            FakeAsyncClient.response_content = b'{"text":"response exceeds test cap"}'
            with (
                patch("heisenberg_access_mcp.server.httpx.AsyncClient", FakeAsyncClient),
                patch("heisenberg_access_mcp.server.MAX_ARTIFACT_DOWNLOAD_BYTES", 10),
                self.assertRaisesRegex(CapabilityError, "elevenlabs_transcription_artifact_too_large"),
            ):
                await run_elevenlabs_speech_to_text(
                    "secret-api-key",
                    "http://localhost:8020/mcp",
                    source["artifact_id"],
                    10,
                    "deu",
                    True,
                    False,
                )

            self.assertIsNotNone(read_artifact_metadata(source["artifact_id"]))
            self.assertTrue((Path(directory) / source["filename"]).exists())

    async def test_preserves_input_on_provider_error_and_invalid_json(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"HEISENBERG_ACCESS_MCP_ARTIFACT_DIR": directory}
        ):
            source = await store_audio_input_artifact(request_with_body(M4A_BYTES))
            FakeAsyncClient.response_status = 503
            FakeAsyncClient.response_content = b'{"detail":"retry later"}'
            with patch("heisenberg_access_mcp.server.httpx.AsyncClient", FakeAsyncClient):
                provider_error = await run_elevenlabs_speech_to_text(
                    "secret-api-key",
                    "http://localhost:8020/mcp",
                    source["artifact_id"],
                    10,
                    "deu",
                    True,
                    False,
                )
            self.assertFalse(provider_error["ok"])
            self.assertIsNotNone(read_artifact_metadata(source["artifact_id"]))

            FakeAsyncClient.response_status = 200
            FakeAsyncClient.response_content = b"not-json"
            with (
                patch("heisenberg_access_mcp.server.httpx.AsyncClient", FakeAsyncClient),
                self.assertRaisesRegex(CapabilityError, "elevenlabs_transcription_invalid_json"),
            ):
                await run_elevenlabs_speech_to_text(
                    "secret-api-key",
                    "http://localhost:8020/mcp",
                    source["artifact_id"],
                    10,
                    "deu",
                    True,
                    False,
                )
            self.assertIsNotNone(read_artifact_metadata(source["artifact_id"]))

    async def test_dry_run_validates_without_external_request_and_caps_speakers_at_ten(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"HEISENBERG_ACCESS_MCP_ARTIFACT_DIR": directory}
        ):
            source = self.create_input_artifact()
            dry_run = await run_elevenlabs_speech_to_text(
                "",
                "http://localhost:8020/mcp",
                source["artifact_id"],
                10,
                "deu",
                False,
                True,
            )
            self.assertTrue(dry_run["dry_run"])
            with self.assertRaisesRegex(CapabilityError, "elevenlabs_num_speakers_invalid"):
                await run_elevenlabs_speech_to_text(
                    "", "http://localhost:8020/mcp", source["artifact_id"], 11, "deu", False, True
                )

    async def test_tool_is_registered(self) -> None:
        tools = {tool.name for tool in await build_mcp().list_tools()}
        self.assertIn("elevenlabs.speech_to_text", tools)

    async def test_tool_refuses_unconfirmed_provider_call_before_openbao(self) -> None:
        tool = build_mcp()._tool_manager.get_tool("elevenlabs.speech_to_text")
        assert tool is not None
        result = await tool.fn(None, "not-a-real-artifact-id", 10, "deu", False, False)
        self.assertEqual(result["error"], "confirmation_required_for_mutating_method")
