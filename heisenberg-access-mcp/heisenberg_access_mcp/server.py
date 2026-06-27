from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import httpx
import uvicorn
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Route


LOGGER = logging.getLogger("heisenberg_access_mcp")
OPENBAO_HEALTH_STATUS_CODES = {200, 429, 472, 473, 501, 503}
OPENBAO_SECRET_MOUNT = "secret"
OPENBAO_ALLOWED_SECRETS = {
    "homeassistant": "heisenberg/homeassistant",
    "freshrss": "heisenberg/freshrss",
    "tandoor": "heisenberg/tandoor",
    "elevenlabs": "heisenberg/elevenlabs",
    "google_health_oauth_client": "heisenberg/google-health/oauth-client",
    "google_health_oauth_token": "heisenberg/google-health/oauth-token",
    "x_oauth": "heisenberg/x/oauth",
}
OPENBAO_WRITABLE_SECRETS = {"google_health_oauth_token", "x_oauth"}
DEFAULT_ARTIFACT_DIR = "/var/lib/heisenberg-access-mcp/artifacts"
MAX_ARTIFACT_DOWNLOAD_BYTES = 100 * 1024 * 1024
DEFAULT_INLINE_RESPONSE_BYTES = 256 * 1024
MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
SERVICE_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}
BINARY_ARTIFACT_CONTENT_TYPES = {
    "application/gzip",
    "application/pdf",
    "application/x-gzip",
    "application/zip",
}
TEXTUAL_ARTIFACT_CONTENT_TYPES = {
    "application/atom+xml",
    "application/javascript",
    "application/rss+xml",
    "application/x-www-form-urlencoded",
    "application/xhtml+xml",
    "application/xml",
    "application/x-yaml",
    "application/yaml",
    "image/svg+xml",
}
TEXTUAL_ARTIFACT_SUFFIXES = ("+xml", "+yaml")
BINARY_ARTIFACT_PREFIXES = ("audio/", "image/", "video/")
SAFE_OPENBAO_FIELDS = (
    "initialized",
    "sealed",
    "standby",
    "performance_standby",
    "replication_perf_mode",
    "replication_dr_mode",
    "server_time_utc",
    "version",
    "storage_type",
)
X_TWEET_ID_RE = re.compile(r"(?<!\d)(\d{5,25})(?!\d)")
ARTIFACT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
ELEVENLABS_VOICE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
GOOGLE_HEALTH_CIVIL_TIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}:\d{2})?$")
TOKEN_REFRESH_SKEW = timedelta(minutes=5)
GOOGLE_HEALTH_ACTIVITY_SCOPE = "https://www.googleapis.com/auth/googlehealth.activity_and_fitness.readonly"
GOOGLE_HEALTH_LISTABLE_ACTIVITY_DATA_TYPES = {
    "distance": {
        "field": "distance",
        "description": "Distance activity datapoints over an interval.",
        "supports_raw_points": True,
        "supports_daily_rollup": True,
    },
    "exercise": {
        "field": "exercise",
        "description": "Exercise/workout sessions with activity type, duration, and metrics summary.",
        "supports_raw_points": True,
        "supports_daily_rollup": False,
        "max_page_size": 25,
    },
    "steps": {
        "field": "steps",
        "description": "Step count activity datapoints over an interval.",
        "supports_raw_points": True,
        "supports_daily_rollup": True,
    },
}
GOOGLE_HEALTH_DAILY_ACTIVITY_DATA_TYPES = {
    "active-energy-burned": {
        "field": "activeEnergyBurned",
        "description": "Active calories burned, excluding basal calories.",
    },
    "active-minutes": {
        "field": "activeMinutes",
        "description": "Active minutes by activity level.",
    },
    "active-zone-minutes": {
        "field": "activeZoneMinutes",
        "description": "Active zone minutes in heart-rate zones.",
    },
    "distance": {
        "field": "distance",
        "description": "Daily distance rollup.",
    },
    "heart-rate": {
        "field": "heartRate",
        "description": "Daily heart-rate min/max/average rollup.",
    },
    "steps": {
        "field": "steps",
        "description": "Daily step count rollup.",
    },
    "total-calories": {
        "field": "totalCalories",
        "description": "Total calories burned, including basal calories.",
    },
}
LEGACY_ELEVENLABS_OPERATIONS: dict[str, dict[str, Any]] = {
    "voices": {
        "method": "GET",
        "path": "/v2/voices",
        "description": "List available voices and voice metadata.",
    },
    "models": {
        "method": "GET",
        "path": "/v1/models",
        "description": "List available ElevenLabs models.",
    },
    "user_subscription": {
        "method": "GET",
        "path": "/v1/user/subscription",
        "description": "Read subscription/quota metadata without exposing the API key.",
    },
}
SENSITIVE_RESPONSE_KEYS = {
    "access_token",
    "accessToken",
    "api_key",
    "apiKey",
    "auth_token",
    "authToken",
    "authorization",
    "client_secret",
    "clientSecret",
    "id_token",
    "idToken",
    "password",
    "refresh_token",
    "refreshToken",
    "secret",
    "token",
    "x-api-key",
    "x_api_key",
    "xi-api-key",
    "xi_api_key",
}
NORMALIZED_SENSITIVE_RESPONSE_KEYS = {
    re.sub(r"[^a-z0-9]", "", key.lower()) for key in SENSITIVE_RESPONSE_KEYS
}
STRUCTURAL_SECRET_NAME_KEYS = {"header", "headerkey", "headername", "key", "name"}
STRUCTURAL_SECRET_VALUE_KEYS = {"headervalue", "value", "values"}

CAPABILITIES: dict[str, dict[str, Any]] = {
    "access.status": {
        "tool": "access_status",
        "enabled": True,
        "secret_access": False,
    },
    "openbao.status": {
        "tool": "openbao_status",
        "enabled": True,
        "secret_access": False,
    },
    "x.get_tweet": {
        "tool": "x.get_tweet",
        "enabled": True,
        "secret_access": "server-side OpenBao OAuth token",
        "writes": "refreshes OAuth tokens back to OpenBao when needed",
    },
    "google_health.access_status": {
        "tool": "google_health.access_status",
        "enabled": True,
        "secret_access": "server-side OpenBao OAuth client and refresh token",
        "writes": "updates token metadata only if Google returns replacement token metadata",
    },
    "google_health.list_data_types": {
        "tool": "google_health.list_data_types",
        "enabled": True,
        "secret_access": False,
        "scope": "documents Google Health fitness data, exercise, workout, activity, steps, calories, distance, active minutes, and health datapoints supported by the current readonly activity scope",
    },
    "google_health.get_exercise_data_points": {
        "tool": "google_health.get_exercise_data_points",
        "enabled": True,
        "secret_access": "server-side OpenBao OAuth client and refresh token",
        "scope": "read-only Google Health exercise/workout data points for a date range; paginated with page_size <= 25",
        "writes": "updates token metadata only if Google returns replacement token metadata",
    },
    "google_health.summarize_activity_day": {
        "tool": "google_health.summarize_activity_day",
        "enabled": True,
        "secret_access": "server-side OpenBao OAuth client and refresh token",
        "scope": "read-only daily log activity summary for steps, calories, distance, active minutes, heart rate, and workouts",
        "writes": "updates token metadata only if Google returns replacement token metadata",
    },
    "elevenlabs.request": {
        "tool": "elevenlabs.request",
        "enabled": True,
        "secret_access": "server-side OpenBao API key",
        "scope": "https://api.elevenlabs.io only; binary/large responses become artifacts",
    },
    "elevenlabs.text_to_speech": {
        "tool": "elevenlabs.text_to_speech",
        "enabled": True,
        "secret_access": "server-side OpenBao API key",
        "returns": "artifact metadata only; audio is stored server-side",
    },
    "homeassistant.request": {
        "tool": "homeassistant.request",
        "enabled": True,
        "secret_access": "server-side OpenBao Home Assistant token",
        "scope": "configured Home Assistant base URL only",
    },
    "freshrss.request": {
        "tool": "freshrss.request",
        "enabled": True,
        "secret_access": "server-side OpenBao FreshRSS API password",
        "scope": "configured FreshRSS API base URL only",
    },
    "tandoor.request": {
        "tool": "tandoor.request",
        "enabled": True,
        "secret_access": "server-side OpenBao Tandoor API key",
        "scope": "configured Tandoor base URL only",
    },
}


def configure_logging() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(message)s")


def emit_event(event: str, **payload: Any) -> None:
    record = {
        "ts": datetime.now(UTC).isoformat(),
        "event": event,
        **payload,
    }
    LOGGER.info(json.dumps(record, sort_keys=True))


def require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} must be set")
    return value


def bool_from_health(data: dict[str, Any], name: str) -> bool | None:
    value = data.get(name)
    return value if isinstance(value, bool) else None


def redact_openbao_status(data: dict[str, Any]) -> dict[str, Any]:
    safe: dict[str, Any] = {}
    for field in SAFE_OPENBAO_FIELDS:
        if field in data:
            safe[field] = data[field]
    return safe


def openbao_target_summary(addr: str) -> dict[str, str | None]:
    parsed = urlparse(addr)
    return {
        "scheme": parsed.scheme or None,
        "host": parsed.hostname,
        "port": str(parsed.port) if parsed.port else None,
    }


def comma_list(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def utc_now() -> datetime:
    return datetime.now(UTC)


def iso_now() -> str:
    return utc_now().isoformat()


def artifact_dir() -> Path:
    return Path(os.environ.get("HEISENBERG_ACCESS_MCP_ARTIFACT_DIR", DEFAULT_ARTIFACT_DIR))


def artifact_base_url(resource_url: str) -> str | None:
    explicit = os.environ.get("HEISENBERG_ACCESS_MCP_ARTIFACT_BASE_URL", "").strip()
    if explicit:
        return explicit.rstrip("/")

    parsed = urlparse(resource_url)
    if not parsed.scheme or not parsed.netloc:
        return None

    return f"{parsed.scheme}://{parsed.netloc}"


def artifact_download_url(resource_url: str, artifact_id: str) -> str | None:
    base_url = artifact_base_url(resource_url)
    if not base_url:
        return None
    return f"{base_url}/artifacts/{artifact_id}"


def extension_for_mime_type(mime_type: str) -> str:
    normalized = mime_type.split(";", maxsplit=1)[0].strip().lower()
    return {
        "audio/mpeg": ".mp3",
        "audio/mp3": ".mp3",
        "audio/wav": ".wav",
        "audio/x-wav": ".wav",
        "audio/ogg": ".ogg",
        "audio/webm": ".webm",
        "audio/flac": ".flac",
    }.get(normalized, ".bin")


def inline_response_limit() -> int:
    raw = os.environ.get("HEISENBERG_ACCESS_MCP_INLINE_RESPONSE_BYTES", "").strip()
    if not raw:
        return DEFAULT_INLINE_RESPONSE_BYTES
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_INLINE_RESPONSE_BYTES
    return max(1024, value)


def metadata_path_for(artifact_id: str) -> Path:
    return artifact_dir() / f"{artifact_id}.json"


def artifact_path_for(artifact_id: str, mime_type: str) -> Path:
    return artifact_dir() / f"{artifact_id}{extension_for_mime_type(mime_type)}"


def read_artifact_metadata(artifact_id: str) -> dict[str, Any] | None:
    if not ARTIFACT_ID_RE.fullmatch(artifact_id):
        return None

    path = metadata_path_for(artifact_id)
    if not path.exists():
        return None

    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None

    return payload if isinstance(payload, dict) else None


def store_artifact(content: bytes, mime_type: str, metadata: dict[str, Any]) -> dict[str, Any]:
    artifact_dir().mkdir(parents=True, exist_ok=True)
    artifact_id = secrets.token_urlsafe(24)
    sha256 = hashlib.sha256(content).hexdigest()
    path = artifact_path_for(artifact_id, mime_type)
    path.write_bytes(content)

    payload = {
        "artifact_id": artifact_id,
        "mime_type": mime_type,
        "byte_size": len(content),
        "sha256": sha256,
        "created_at": iso_now(),
        "filename": path.name,
        **metadata,
    }
    metadata_path_for(artifact_id).write_text(json.dumps(payload, sort_keys=True))
    return payload


def parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value, tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None

    if not isinstance(value, str) or not value.strip():
        return None

    text = value.strip()
    if text.isdigit():
        return parse_datetime(int(text))

    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def expires_soon(expires_at: Any) -> bool:
    parsed = parse_datetime(expires_at)
    return parsed is None or parsed <= utc_now() + TOKEN_REFRESH_SKEW


def required_secret_value(secret: dict[str, Any], key: str) -> str:
    value = secret.get(key)
    if not isinstance(value, str) or not value.strip():
        raise CapabilityError("secret_missing_required_key", key=key)
    return value.strip()


def normalized_response_key(key: str) -> str:
    return re.sub(r"[^a-z0-9]", "", key.lower())


def is_sensitive_response_key(key: str) -> bool:
    normalized_key = normalized_response_key(key)
    sensitive_fragments = (
        "authorization",
        "apikey",
        "authtoken",
        "bearertoken",
        "clientsecret",
        "cookie",
        "idtoken",
        "personalaccesstoken",
        "password",
        "refreshtoken",
        "secret",
        "secretkey",
        "sessiontoken",
        "token",
        "xapikey",
        "xiapikey",
    )
    return (
        key in SENSITIVE_RESPONSE_KEYS
        or normalized_key in NORMALIZED_SENSITIVE_RESPONSE_KEYS
        or any(fragment in normalized_key for fragment in sensitive_fragments)
    )


def is_sensitive_structural_name(value: str) -> bool:
    text = value.strip().strip("'\"")
    if any(separator in text for separator in (":", "=", " ", "\t", "\r", "\n")):
        return False
    return is_sensitive_response_key(text)


def is_probable_standalone_token(value: str) -> bool:
    text = value.strip().strip("'\"")
    if not text or any(character.isspace() for character in text):
        return False
    if re.fullmatch(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}", text):
        return True
    if re.fullmatch(r"ya29\.[A-Za-z0-9._-]{20,}", text):
        return True
    if len(text) >= 40 and re.fullmatch(r"[A-Za-z0-9._~+/=-]+", text):
        has_letter = bool(re.search(r"[A-Za-z]", text))
        has_digit = bool(re.search(r"\d", text))
        return has_letter and has_digit
    return False


def redacted_array_item(value: Any) -> Any:
    if isinstance(value, str):
        redacted = redacted_text(value)
        if redacted != value:
            return redacted
        if is_probable_standalone_token(value):
            return "[redacted]"
    return redacted_json(value)


def has_sensitive_structural_name(value: dict[Any, Any]) -> bool:
    for key, item in value.items():
        if not isinstance(key, str) or not isinstance(item, str):
            continue
        if normalized_response_key(key) in STRUCTURAL_SECRET_NAME_KEYS and is_sensitive_structural_name(item):
            return True
    return False


def redacted_json(value: Any) -> Any:
    if isinstance(value, dict):
        redact_structural_values = has_sensitive_structural_name(value)
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            normalized_key = normalized_response_key(key)
            if is_sensitive_response_key(key):
                redacted[key] = "[redacted]"
            elif redact_structural_values and normalized_key in STRUCTURAL_SECRET_VALUE_KEYS:
                redacted[key] = "[redacted]"
            else:
                redacted[key] = redacted_json(item)
        return redacted

    if isinstance(value, list):
        if len(value) >= 2 and isinstance(value[0], str) and is_sensitive_structural_name(value[0]):
            return [redacted_json(value[0]), *(["[redacted]"] * (len(value) - 1))]
        return [redacted_array_item(item) for item in value]
    if isinstance(value, str):
        return redacted_text(value)

    return value


def redacted_text(value: str) -> str:
    patterns = (
        r"(?im)^(Auth=).+$",
        r"(?i)(Authorization['\"]?\s*[:=]\s*['\"]?)[^'\",\r\n}\]]+",
        r"(?i)(Set-Cookie\s*:\s*)[^\r\n]+",
        r"(?i)(Cookie\s*:\s*)[^\r\n]+",
        r"(?i)((?:x-api-key|xi-api-key)['\"]?\s*[:=]\s*['\"]?)[^'\",\s}\]\r\n]+",
        r"(?i)((?:[A-Za-z0-9-]*Auth-Token|[A-Za-z0-9-]*Access-Token|[A-Za-z0-9-]*Api-Key|[A-Za-z0-9-]*ApiKey)['\"]?\s*[:=]\s*['\"]?)[^'\",\s}\]\r\n]+",
        r"(?i)\b(Bearer\s+)[^'\",\s}\]\r\n]+",
        r"(?i)\b(Basic\s+)[A-Za-z0-9+/=._-]+",
        r"(?i)\b(Token\s+)[^'\",\s}\]\r\n]+",
        r"(?i)\b(GoogleLogin\s+auth=)[^&'\",\s}\]\r\n]+",
        r"(?i)^(sk_(?:live|test)?_?)[A-Za-z0-9_-]+",
        r"(?i)(auth=)[^&\s]+",
        r"(?i)((?<![A-Za-z0-9_-])(?:access[_-]?token|refresh[_-]?token|auth[_-]?token|id[_-]?token|api[_-]?key|client[_-]?secret|token)['\"]?\s*[:=]\s*['\"]?)[^&'\",\s}\]\r\n]+",
    )
    redacted = value
    for pattern in patterns:
        redacted = re.sub(pattern, r"\1[redacted]", redacted)
    return redacted


def safe_response_headers_from_headers(source_headers: httpx.Headers) -> dict[str, str]:
    headers: dict[str, str] = {}
    for key in ("content-type", "content-length", "etag", "last-modified"):
        value = source_headers.get(key)
        if value:
            headers[key] = value
    return headers


def safe_response_headers(response: httpx.Response) -> dict[str, str]:
    return safe_response_headers_from_headers(response.headers)


def normalize_service_method(method: str) -> str:
    normalized = method.upper().strip()
    if normalized not in SERVICE_METHODS:
        raise CapabilityError("method_not_supported", allowed_methods=sorted(SERVICE_METHODS))
    return normalized


def normalize_response_mode(response_mode: str) -> str:
    mode = response_mode.strip().lower()
    if mode not in {"auto", "inline", "artifact"}:
        raise CapabilityError("invalid_response_mode", allowed_modes=["auto", "inline", "artifact"])
    return mode


def response_read_limit_for_mode(mode: str) -> int:
    if mode == "inline":
        return inline_response_limit()
    return MAX_ARTIFACT_DOWNLOAD_BYTES


def validate_service_path(path: str) -> str:
    path = path.strip()
    if not path:
        raise CapabilityError("path_required")

    parsed = urlparse(path)
    if parsed.scheme or parsed.netloc or path.startswith("//"):
        raise CapabilityError("absolute_urls_not_allowed")
    if "?" in path:
        raise CapabilityError("query_string_not_allowed_use_params")

    segments = [segment for segment in path.split("/") if segment]
    for segment in segments:
        decoded_segment = unquote(segment)
        if decoded_segment in {".", ".."} or "/" in decoded_segment or "\\" in decoded_segment:
            raise CapabilityError("path_traversal_not_allowed")

    normalized_path = "/" + "/".join(segments)
    if segments and path.endswith("/"):
        return f"{normalized_path}/"
    return normalized_path


def join_service_url(base_url: str, path: str) -> str:
    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise CapabilityError("service_base_url_invalid")

    normalized_path = validate_service_path(path)
    return f"{base_url.rstrip('/')}{normalized_path}"


def normalize_mapping(value: dict[str, Any] | None, name: str) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise CapabilityError(f"{name}_must_be_object")
    return value


def ensure_write_confirmed(method: str, confirm: bool) -> None:
    if method in MUTATING_METHODS and not confirm:
        raise CapabilityError(
            "confirmation_required_for_mutating_method",
            method=method,
            message="Mutating service requests require explicit current user approval and confirm=true.",
        )


def dry_run_payload(method: str, url: str) -> dict[str, Any]:
    parsed = urlparse(url)
    return {
        "ok": True,
        "dry_run": True,
        "method": method,
        "target": {
            "scheme": parsed.scheme,
            "host": parsed.hostname,
            "port": parsed.port,
            "path": parsed.path,
        },
    }


def sanitized_api_error(response: httpx.Response) -> dict[str, Any]:
    payload: Any
    try:
        payload = response.json()
    except json.JSONDecodeError:
        payload = redacted_text(response.text[:500])
    return {
        "http_status": response.status_code,
        "body": redacted_json(payload),
    }


def is_inline_content_type(content_type: str) -> bool:
    normalized = content_type.split(";", maxsplit=1)[0].strip().lower()
    return is_json_content_type(content_type) or normalized.startswith("text/")


def inline_response_payload(response: httpx.Response, content_type: str) -> dict[str, Any]:
    if is_json_content_type(content_type):
        try:
            return {"response": redacted_json(response.json())}
        except json.JSONDecodeError:
            raise CapabilityError(
                "json_response_parse_failed",
                message="Malformed JSON responses are refused because text redaction is not reliable enough.",
            ) from None

    return {"response_text": redacted_text(response.text)}


def is_json_content_type(content_type: str) -> bool:
    normalized = content_type.split(";", maxsplit=1)[0].strip().lower()
    return normalized == "application/json" or normalized.endswith("+json")


def is_text_content_type(content_type: str) -> bool:
    normalized = content_type.split(";", maxsplit=1)[0].strip().lower()
    return (
        normalized.startswith("text/")
        or normalized in TEXTUAL_ARTIFACT_CONTENT_TYPES
        or normalized.endswith(TEXTUAL_ARTIFACT_SUFFIXES)
    )


def is_binary_artifact_content_type(content_type: str) -> bool:
    normalized = content_type.split(";", maxsplit=1)[0].strip().lower()
    return normalized in BINARY_ARTIFACT_CONTENT_TYPES or normalized.startswith(BINARY_ARTIFACT_PREFIXES)


def artifact_content_for_response(response: httpx.Response, content_type: str) -> tuple[bytes, str]:
    if is_json_content_type(content_type):
        try:
            payload = redacted_json(response.json())
        except json.JSONDecodeError:
            raise CapabilityError(
                "json_artifact_parse_failed",
                message="Malformed JSON responses are refused as artifacts because text redaction is not reliable enough.",
            ) from None
        return json.dumps(payload, sort_keys=True).encode(), "application/json"

    if is_text_content_type(content_type):
        raise CapabilityError(
            "text_artifact_refused",
            message="Text responses are not stored as artifacts because secret redaction is not reliable enough.",
        )

    if not is_binary_artifact_content_type(content_type):
        raise CapabilityError(
            "artifact_content_type_not_allowed",
            content_type=content_type,
            message="Only known binary content types and redacted JSON may be stored as artifacts.",
        )

    return response.content, content_type


def service_response_payload(
    response: httpx.Response,
    *,
    response_mode: str,
    resource_url: str,
    artifact_metadata: dict[str, Any],
) -> dict[str, Any]:
    mode = normalize_response_mode(response_mode)

    content_type = response.headers.get("content-type", "application/octet-stream")
    byte_size = len(response.content)
    can_inline = is_inline_content_type(content_type) and byte_size <= inline_response_limit()

    base = {
        "ok": response.status_code < 400,
        "http_status": response.status_code,
        "content_type": content_type,
        "byte_size": byte_size,
        "headers": safe_response_headers(response),
    }

    if byte_size == 0:
        return {**base, "response_empty": True}

    if mode == "inline" and not can_inline:
        return {
            **base,
            "ok": False,
            "error": "response_too_large_or_binary_for_inline",
            "max_inline_bytes": inline_response_limit(),
        }

    if mode == "artifact" or (mode == "auto" and not can_inline):
        if byte_size > MAX_ARTIFACT_DOWNLOAD_BYTES:
            return {
                **base,
                "ok": False,
                "error": "response_too_large_for_artifact",
                "max_artifact_bytes": MAX_ARTIFACT_DOWNLOAD_BYTES,
            }

        artifact_content, artifact_mime_type = artifact_content_for_response(response, content_type)
        if len(artifact_content) > MAX_ARTIFACT_DOWNLOAD_BYTES:
            return {
                **base,
                "ok": False,
                "error": "response_too_large_for_artifact",
                "max_artifact_bytes": MAX_ARTIFACT_DOWNLOAD_BYTES,
                "artifact_byte_size": len(artifact_content),
            }

        artifact = store_artifact(
            artifact_content,
            artifact_mime_type,
            {
                **artifact_metadata,
                "provider_http_status": response.status_code,
                "provider_content_type": content_type,
            },
        )
        artifact["download"] = {
            "url": artifact_download_url(resource_url, artifact["artifact_id"]),
            "authorization": "Bearer token required; same private MCP bearer token",
            "exposure": "same private MCP HTTP service; Traefik remains disabled",
        }
        return {**base, "artifact": artifact}

    return {**base, **inline_response_payload(response, content_type)}


def oversized_response_payload(
    *,
    mode: str,
    status_code: int,
    content_type: str,
    byte_size: int,
    headers: httpx.Headers,
) -> dict[str, Any]:
    base = {
        "ok": False,
        "http_status": status_code,
        "content_type": content_type,
        "byte_size": byte_size,
        "headers": safe_response_headers_from_headers(headers),
    }
    if mode == "inline":
        return {
            **base,
            "error": "response_too_large_or_binary_for_inline",
            "max_inline_bytes": inline_response_limit(),
        }
    return {
        **base,
        "error": "response_too_large_for_artifact",
        "max_artifact_bytes": MAX_ARTIFACT_DOWNLOAD_BYTES,
    }


async def limited_service_response(
    client: httpx.AsyncClient,
    *,
    method: str,
    url: str,
    headers: dict[str, str],
    params: dict[str, Any] | None,
    json_body: dict[str, Any] | None,
    form_body: dict[str, Any] | None,
    mode: str,
) -> tuple[httpx.Response | None, dict[str, Any] | None]:
    limit = response_read_limit_for_mode(mode)
    chunks: list[bytes] = []
    byte_size = 0

    async with client.stream(
        method,
        url,
        headers=headers,
        params=params,
        json=json_body,
        data=form_body,
    ) as response:
        content_type = response.headers.get("content-type", "application/octet-stream")
        async for chunk in response.aiter_bytes():
            byte_size += len(chunk)
            if byte_size > limit:
                return None, oversized_response_payload(
                    mode=mode,
                    status_code=response.status_code,
                    content_type=content_type,
                    byte_size=byte_size,
                    headers=response.headers,
                )
            chunks.append(chunk)

        buffered = httpx.Response(
            response.status_code,
            headers=response.headers,
            content=b"".join(chunks),
            request=response.request,
        )
        return buffered, None


class CapabilityError(Exception):
    def __init__(self, code: str, **details: Any) -> None:
        super().__init__(code)
        self.code = code
        self.details = details


class OpenBaoError(Exception):
    def __init__(self, code: str, *, status_code: int | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code


class OpenBaoKV2:
    def __init__(self, addr: str, token: str | None) -> None:
        self._addr = addr.rstrip("/")
        self._token = token

    @property
    def _headers(self) -> dict[str, str]:
        if not self._token:
            raise OpenBaoError("openbao_token_missing")
        return {
            "X-Vault-Request": "true",
            "X-Vault-Token": self._token,
        }

    def _path_for(self, secret_name: str) -> str:
        if secret_name not in OPENBAO_ALLOWED_SECRETS:
            raise OpenBaoError("openbao_secret_not_allowed")
        return OPENBAO_ALLOWED_SECRETS[secret_name]

    def _data_url_for(self, secret_name: str) -> str:
        secret_path = self._path_for(secret_name)
        return f"{self._addr}/v1/{OPENBAO_SECRET_MOUNT}/data/{secret_path}"

    async def read(self, secret_name: str) -> dict[str, Any]:
        url = self._data_url_for(secret_name)
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(url, headers=self._headers)

        if response.status_code != 200:
            raise OpenBaoError("openbao_read_failed", status_code=response.status_code)

        try:
            payload = response.json()
        except json.JSONDecodeError as error:
            raise OpenBaoError("openbao_invalid_json") from error

        data = payload.get("data", {})
        secret = data.get("data", {})
        if not isinstance(secret, dict):
            raise OpenBaoError("openbao_invalid_secret_shape")
        return secret

    async def write(self, secret_name: str, secret: dict[str, Any]) -> None:
        if secret_name not in OPENBAO_WRITABLE_SECRETS:
            raise OpenBaoError("openbao_secret_not_writable")

        url = self._data_url_for(secret_name)
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(url, headers=self._headers, json={"data": secret})

        if response.status_code not in {200, 204}:
            raise OpenBaoError("openbao_write_failed", status_code=response.status_code)


def transport_security_settings(resource_url: str) -> TransportSecuritySettings:
    parsed = urlparse(resource_url)
    allowed_hosts = {
        "127.0.0.1:*",
        "localhost:*",
        "[::1]:*",
    }
    allowed_origins = {
        "http://127.0.0.1:*",
        "http://localhost:*",
        "http://[::1]:*",
    }

    if parsed.netloc:
        allowed_hosts.add(parsed.netloc)
        allowed_origins.add(f"{parsed.scheme or 'http'}://{parsed.netloc}")

    allowed_hosts.update(comma_list(os.environ.get("HEISENBERG_ACCESS_MCP_ALLOWED_HOSTS", "")))
    allowed_origins.update(comma_list(os.environ.get("HEISENBERG_ACCESS_MCP_ALLOWED_ORIGINS", "")))

    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=sorted(allowed_hosts),
        allowed_origins=sorted(allowed_origins),
    )


class StaticBearerTokenVerifier(TokenVerifier):
    def __init__(self, expected_token: str) -> None:
        self._expected_token = expected_token

    async def verify_token(self, token: str) -> AccessToken | None:
        if not hmac.compare_digest(token, self._expected_token):
            return None

        return AccessToken(
            token="accepted-static-token",
            client_id="heisenberg-access-mcp-env-token",
            scopes=["mcp:call"],
        )


class AccessLogMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        started = asyncio.get_running_loop().time()
        response = await call_next(request)
        duration_ms = int((asyncio.get_running_loop().time() - started) * 1000)
        emit_event(
            "http_request",
            method=request.method,
            path=request.url.path,
            status=response.status_code,
            client_host=request.client.host if request.client else None,
            duration_ms=duration_ms,
        )
        return response


async def read_openbao_health(openbao_addr: str, openbao_token: str | None) -> dict[str, Any]:
    headers = {"X-Vault-Request": "true"}
    if openbao_token:
        headers["X-Vault-Token"] = openbao_token

    url = f"{openbao_addr.rstrip('/')}/v1/sys/health?standbyok=true&perfstandbyok=true"
    async with httpx.AsyncClient(timeout=5.0) as client:
        response = await client.get(url, headers=headers)

    reachable = response.status_code in OPENBAO_HEALTH_STATUS_CODES
    try:
        body = response.json()
    except json.JSONDecodeError:
        body = {}

    safe_body = redact_openbao_status(body if isinstance(body, dict) else {})
    initialized = bool_from_health(safe_body, "initialized")
    sealed = bool_from_health(safe_body, "sealed")

    return {
        "reachable": reachable,
        "ready": reachable and initialized is True and sealed is False,
        "http_status": response.status_code,
        **safe_body,
    }


def extract_tweet_id(tweet_id_or_url: str) -> str:
    match = X_TWEET_ID_RE.search(tweet_id_or_url)
    if not match:
        raise CapabilityError("invalid_tweet_id")
    return match.group(1)


async def refresh_x_access_token(openbao: OpenBaoKV2, oauth: dict[str, Any]) -> dict[str, Any]:
    client_id = required_secret_value(oauth, "client_id")
    refresh_token = required_secret_value(oauth, "refresh_token")

    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post(
            "https://api.x.com/2/oauth2/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": client_id,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

    if response.status_code != 200:
        raise CapabilityError("x_token_refresh_failed", **sanitized_api_error(response))

    try:
        payload = response.json()
    except json.JSONDecodeError as error:
        raise CapabilityError("x_token_refresh_invalid_json") from error

    access_token = payload.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise CapabilityError("x_token_refresh_missing_access_token")

    refreshed = dict(oauth)
    refreshed["access_token"] = access_token
    if isinstance(payload.get("refresh_token"), str) and payload["refresh_token"]:
        refreshed["refresh_token"] = payload["refresh_token"]

    expires_in = payload.get("expires_in")
    if isinstance(expires_in, (int, float)) and expires_in > 0:
        refreshed["expires_at"] = (utc_now() + timedelta(seconds=int(expires_in))).isoformat()
    else:
        refreshed.pop("expires_at", None)

    refreshed["stored_at"] = iso_now()
    await openbao.write("x_oauth", refreshed)
    emit_event("oauth_token_refreshed", provider="x")
    return refreshed


def extract_x_media_urls(media: list[Any]) -> list[str]:
    urls: list[str] = []
    for item in media:
        if not isinstance(item, dict):
            continue
        for key in ("url", "preview_image_url"):
            value = item.get(key)
            if isinstance(value, str) and value and value not in urls:
                urls.append(value)
        variants = item.get("variants")
        if isinstance(variants, list):
            for variant in variants:
                if not isinstance(variant, dict):
                    continue
                value = variant.get("url")
                if isinstance(value, str) and value and value not in urls:
                    urls.append(value)
    return urls


async def fetch_x_tweet(access_token: str, tweet_id: str) -> dict[str, Any]:
    params = {
        "tweet.fields": "attachments,author_id,created_at,entities,public_metrics",
        "expansions": "author_id,attachments.media_keys",
        "user.fields": "id,name,protected,username",
        "media.fields": "alt_text,media_key,preview_image_url,public_metrics,type,url,variants,width,height",
    }
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.get(
            f"https://api.x.com/2/tweets/{tweet_id}",
            headers={"Authorization": f"Bearer {access_token}"},
            params=params,
        )

    if response.status_code != 200:
        raise CapabilityError("x_tweet_request_failed", **sanitized_api_error(response))

    try:
        payload = response.json()
    except json.JSONDecodeError as error:
        raise CapabilityError("x_tweet_invalid_json") from error

    data = payload.get("data")
    if not isinstance(data, dict):
        raise CapabilityError("x_tweet_missing_data")

    includes = payload.get("includes") if isinstance(payload.get("includes"), dict) else {}
    users = includes.get("users", []) if isinstance(includes, dict) else []
    media = includes.get("media", []) if isinstance(includes, dict) else []
    author = next((item for item in users if isinstance(item, dict) and item.get("id") == data.get("author_id")), None)
    if not isinstance(author, dict) or author.get("protected") is not False:
        raise CapabilityError("x_tweet_public_status_unverified")
    username = author.get("username") if isinstance(author, dict) and isinstance(author.get("username"), str) else None
    url = f"https://x.com/{username}/status/{tweet_id}" if username else f"https://x.com/i/web/status/{tweet_id}"

    return {
        "ok": True,
        "id": data.get("id"),
        "text": data.get("text"),
        "author": author,
        "url": url,
        "created_at": data.get("created_at"),
        "public_metrics": data.get("public_metrics", {}),
        "media_urls": extract_x_media_urls(media if isinstance(media, list) else []),
    }


async def refresh_google_access_token(openbao: OpenBaoKV2) -> tuple[str, bool]:
    client_secret = await openbao.read("google_health_oauth_client")
    token_secret = await openbao.read("google_health_oauth_token")

    client_id = required_secret_value(client_secret, "client_id")
    client_secret_value = required_secret_value(client_secret, "client_secret")
    token_uri = required_secret_value(client_secret, "token_uri")
    refresh_token = required_secret_value(token_secret, "refresh_token")

    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post(
            token_uri,
            data={
                "client_id": client_id,
                "client_secret": client_secret_value,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

    if response.status_code != 200:
        raise CapabilityError("google_token_refresh_failed", **sanitized_api_error(response))

    try:
        payload = response.json()
    except json.JSONDecodeError as error:
        raise CapabilityError("google_token_refresh_invalid_json") from error

    access_token = payload.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise CapabilityError("google_token_refresh_missing_access_token")

    wrote_metadata = False
    if isinstance(payload.get("refresh_token"), str) and payload["refresh_token"] != refresh_token:
        updated = dict(token_secret)
        updated["refresh_token"] = payload["refresh_token"]
        updated["stored_at"] = iso_now()
        await openbao.write("google_health_oauth_token", updated)
        wrote_metadata = True
        emit_event("oauth_token_refreshed", provider="google_health")

    return access_token, wrote_metadata


async def fetch_google_health_access_status(access_token: str) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.get(
            "https://health.googleapis.com/v4/users/me/identity",
            headers={"Authorization": f"Bearer {access_token}"},
        )

    if response.status_code != 200:
        return {
            "ok": False,
            "endpoint": "google_health.identity",
            "status": "health_api_endpoint_unavailable",
            "error": sanitized_api_error(response),
        }

    try:
        payload = response.json()
    except json.JSONDecodeError:
        return {
            "ok": False,
            "endpoint": "google_health.identity",
            "status": "health_api_invalid_json",
        }

    health_user_id = payload.get("healthUserId")
    legacy_user_id = payload.get("legacyUserId")

    return {
        "ok": True,
        "endpoint": "google_health.identity",
        "identity": {
            "healthUserId": health_user_id if isinstance(health_user_id, str) else None,
            "legacyUserId": legacy_user_id if isinstance(legacy_user_id, str) else None,
        },
    }


def parse_iso_date(value: str, name: str = "date") -> date:
    if not isinstance(value, str) or not ISO_DATE_RE.fullmatch(value.strip()):
        raise CapabilityError(f"{name}_must_be_iso_date", expected_format="YYYY-MM-DD")
    try:
        return date.fromisoformat(value.strip())
    except ValueError as error:
        raise CapabilityError(f"{name}_invalid", expected_format="YYYY-MM-DD") from error


def google_health_default_start_date(days_back: int) -> str:
    return (utc_now().date() - timedelta(days=days_back)).isoformat()


def google_health_default_end_date() -> str:
    return (utc_now().date() + timedelta(days=1)).isoformat()


def normalize_google_health_civil_time(value: str | None, *, default: str, name: str) -> str:
    if value is None:
        text = default
    elif isinstance(value, str):
        text = value.strip()
    else:
        raise CapabilityError(
            f"{name}_must_be_civil_time",
            expected_format="YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS without timezone",
        )
    if not GOOGLE_HEALTH_CIVIL_TIME_RE.fullmatch(text):
        raise CapabilityError(
            f"{name}_must_be_civil_time",
            expected_format="YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS without timezone",
        )
    return text


def normalize_page_size(value: int | None, *, default: int, minimum: int, maximum: int, name: str = "page_size") -> int:
    if value is None:
        return default
    if not isinstance(value, int):
        raise CapabilityError(f"{name}_must_be_integer")
    if value < minimum or value > maximum:
        raise CapabilityError(f"{name}_out_of_range", min=minimum, max=maximum)
    return value


def google_health_date_json(value: date) -> dict[str, int]:
    return {
        "year": value.year,
        "month": value.month,
        "day": value.day,
    }


def sanitize_google_health_data_point(data_point: Any) -> Any:
    if not isinstance(data_point, dict):
        return data_point
    sanitized = redacted_json(data_point)
    if isinstance(sanitized, dict):
        sanitized.pop("name", None)
    return sanitized


def summarize_google_health_exercise(data_point: Any) -> dict[str, Any]:
    if not isinstance(data_point, dict):
        return {}
    exercise = data_point.get("exercise")
    if not isinstance(exercise, dict):
        return {}
    interval = exercise.get("interval") if isinstance(exercise.get("interval"), dict) else {}
    metrics = exercise.get("metricsSummary") if isinstance(exercise.get("metricsSummary"), dict) else {}
    return redacted_json(
        {
            "displayName": exercise.get("displayName"),
            "exerciseType": exercise.get("exerciseType"),
            "interval": {
                "civilStartTime": interval.get("civilStartTime") if isinstance(interval, dict) else None,
                "civilEndTime": interval.get("civilEndTime") if isinstance(interval, dict) else None,
                "startTime": interval.get("startTime") if isinstance(interval, dict) else None,
                "endTime": interval.get("endTime") if isinstance(interval, dict) else None,
            },
            "activeDuration": exercise.get("activeDuration"),
            "metricsSummary": {
                key: metrics.get(key)
                for key in (
                    "steps",
                    "distanceMillimeters",
                    "caloriesKcal",
                    "activeZoneMinutes",
                    "averageHeartRateBeatsPerMinute",
                    "averagePaceSecondsPerMeter",
                    "averageSpeedMillimetersPerSecond",
                    "elevationGainMillimeters",
                )
                if key in metrics
            },
        }
    )


def google_health_activity_data_types_payload() -> dict[str, Any]:
    return {
        "ok": True,
        "scope": GOOGLE_HEALTH_ACTIVITY_SCOPE,
        "source": {
            "discovery": "https://health.googleapis.com/$discovery/rest?version=v4",
            "data_points_list": "https://developers.google.com/health/reference/rest/v4/users.dataTypes.dataPoints/list",
        },
        "notes": [
            "Google Health v4 does not expose a generic users.dataTypes.list endpoint.",
            "Supported names are documented through the DataPoint and DailyRollupDataPoint union fields.",
            "Live probes with the current activity_and_fitness.readonly scope confirmed raw exercise, steps, and distance access, and daily rollups for the listed activity metrics.",
        ],
        "raw_data_points": {
            name: {
                "field": spec["field"],
                "description": spec["description"],
                "supports_raw_points": spec["supports_raw_points"],
                "supports_daily_rollup": spec["supports_daily_rollup"],
                **({"max_page_size": spec["max_page_size"]} if "max_page_size" in spec else {}),
            }
            for name, spec in sorted(GOOGLE_HEALTH_LISTABLE_ACTIVITY_DATA_TYPES.items())
        },
        "daily_rollup_data_types": {
            name: {
                "field": spec["field"],
                "description": spec["description"],
            }
            for name, spec in sorted(GOOGLE_HEALTH_DAILY_ACTIVITY_DATA_TYPES.items())
        },
        "recommended_tools": [
            "google_health.get_exercise_data_points for paginated exercise/workout sessions in a date range",
            "google_health.summarize_activity_day for daily log, brain, and fitness data sync summaries",
        ],
    }


async def google_health_list_data_points(
    access_token: str,
    *,
    data_type: str,
    filter_expression: str,
    page_size: int,
    page_token: str | None,
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "filter": filter_expression,
        "pageSize": page_size,
    }
    if page_token:
        params["pageToken"] = page_token

    endpoint = f"https://health.googleapis.com/v4/users/me/dataTypes/{data_type}/dataPoints"
    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.get(
            endpoint,
            headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
            params=params,
        )

    if response.status_code != 200:
        raise CapabilityError("google_health_data_points_request_failed", **sanitized_api_error(response))

    try:
        payload = response.json()
    except json.JSONDecodeError as error:
        raise CapabilityError("google_health_data_points_invalid_json") from error

    data_points = payload.get("dataPoints", [])
    if not isinstance(data_points, list):
        data_points = []

    result: dict[str, Any] = {
        "ok": True,
        "endpoint": "google_health.data_points.list",
        "data_type": data_type,
        "filter": filter_expression,
        "page_size": page_size,
        "data_point_count": len(data_points),
        "data_points": [sanitize_google_health_data_point(item) for item in data_points],
    }
    next_page_token = payload.get("nextPageToken")
    if isinstance(next_page_token, str) and next_page_token:
        result["next_page_token"] = next_page_token
    return result


async def google_health_daily_rollup(
    access_token: str,
    *,
    data_type: str,
    target_date: date,
) -> dict[str, Any]:
    next_day = target_date + timedelta(days=1)
    endpoint = f"https://health.googleapis.com/v4/users/me/dataTypes/{data_type}/dataPoints:dailyRollUp"
    body = {
        "range": {
            "start": {"date": google_health_date_json(target_date)},
            "end": {"date": google_health_date_json(next_day)},
        },
        "windowSizeDays": 1,
        "pageSize": 1,
    }
    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.post(
            endpoint,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            json=body,
        )

    if response.status_code != 200:
        return {
            "ok": False,
            "endpoint": "google_health.data_points.daily_rollup",
            "data_type": data_type,
            "error": sanitized_api_error(response),
        }

    try:
        payload = response.json()
    except json.JSONDecodeError:
        return {
            "ok": False,
            "endpoint": "google_health.data_points.daily_rollup",
            "data_type": data_type,
            "status": "invalid_json",
        }

    rollups = payload.get("rollupDataPoints", [])
    if not isinstance(rollups, list):
        rollups = []
    field_name = GOOGLE_HEALTH_DAILY_ACTIVITY_DATA_TYPES[data_type]["field"]
    first = rollups[0] if rollups and isinstance(rollups[0], dict) else {}
    value = first.get(field_name) if isinstance(first, dict) else None
    return {
        "ok": True,
        "endpoint": "google_health.data_points.daily_rollup",
        "data_type": data_type,
        "field": field_name,
        "rollup_count": len(rollups),
        "value": redacted_json(value),
    }


async def summarize_google_health_activity_day(access_token: str, target_date: date) -> dict[str, Any]:
    rollups: dict[str, Any] = {}
    for data_type in GOOGLE_HEALTH_DAILY_ACTIVITY_DATA_TYPES:
        rollups[data_type] = await google_health_daily_rollup(access_token, data_type=data_type, target_date=target_date)

    next_day = target_date + timedelta(days=1)
    exercises = await google_health_list_data_points(
        access_token,
        data_type="exercise",
        filter_expression=(
            f'exercise.interval.civil_start_time >= "{target_date.isoformat()}" '
            f'AND exercise.interval.civil_start_time < "{next_day.isoformat()}"'
        ),
        page_size=10,
        page_token=None,
    )
    exercise_points = exercises.get("data_points", [])
    exercise_summaries = [
        summarize_google_health_exercise(item)
        for item in exercise_points
        if isinstance(item, dict)
    ]

    return {
        "ok": True,
        "endpoint": "google_health.activity_day_summary",
        "date": target_date.isoformat(),
        "scope": GOOGLE_HEALTH_ACTIVITY_SCOPE,
        "daily_rollups": rollups,
        "exercise": {
            "data_point_count": exercises.get("data_point_count"),
            "has_more": bool(exercises.get("next_page_token")),
            "summaries": exercise_summaries,
        },
    }


async def service_request(
    *,
    base_url: str,
    path: str,
    method: str,
    headers: dict[str, str],
    resource_url: str,
    service_name: str,
    params: dict[str, Any] | None,
    json_body: dict[str, Any] | None,
    form_body: dict[str, Any] | None,
    response_mode: str,
    confirm: bool,
    dry_run: bool,
) -> dict[str, Any]:
    normalized_method = normalize_service_method(method)
    normalized_response_mode = normalize_response_mode(response_mode)
    params = normalize_mapping(params, "params")
    json_body = normalize_mapping(json_body, "json_body")
    form_body = normalize_mapping(form_body, "form_body")
    if json_body is not None and form_body is not None:
        raise CapabilityError("only_one_body_format_allowed")

    url = join_service_url(base_url, path)
    if dry_run:
        return dry_run_payload(normalized_method, url)

    ensure_write_confirmed(normalized_method, confirm)

    async with httpx.AsyncClient(timeout=20.0) as client:
        response, oversized_payload = await limited_service_response(
            client,
            method=normalized_method,
            url=url,
            headers=headers,
            params=params,
            json_body=json_body,
            form_body=form_body,
            mode=normalized_response_mode,
        )
    if oversized_payload is not None:
        return oversized_payload
    if response is None:
        raise CapabilityError("provider_response_missing")

    return service_response_payload(
        response,
        response_mode=normalized_response_mode,
        resource_url=resource_url,
        artifact_metadata={
            "provider": service_name,
            "method": normalized_method,
            "path": validate_service_path(path),
        },
    )


async def freshrss_auth_token(api_url: str, username: str, api_password: str) -> str:
    login_url = join_service_url(api_url, "/accounts/ClientLogin")
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post(
            login_url,
            data={
                "Email": username,
                "Passwd": api_password,
            },
        )

    if response.status_code != 200:
        raise CapabilityError("freshrss_auth_failed", **sanitized_api_error(response))

    for line in response.text.splitlines():
        key, _, value = line.partition("=")
        if key == "Auth" and value:
            return value

    raise CapabilityError("freshrss_auth_token_missing")


def legacy_elevenlabs_operation(operation: str) -> dict[str, str] | None:
    spec = LEGACY_ELEVENLABS_OPERATIONS.get(operation)
    if spec is None:
        return None
    return {
        "method": spec["method"],
        "path": spec["path"],
    }


async def run_elevenlabs_text_to_speech(
    api_key: str,
    resource_url: str,
    text: str,
    voice_id: str,
    model_id: str,
    output_format: str,
    stability: float | None,
    similarity_boost: float | None,
    style: float | None,
    use_speaker_boost: bool | None,
    confirm: bool,
) -> dict[str, Any]:
    ensure_write_confirmed("POST", confirm)

    text = text.strip()
    voice_id = voice_id.strip()
    model_id = model_id.strip()
    output_format = output_format.strip()

    if not text:
        raise CapabilityError("elevenlabs_text_required")
    if len(text) > 5000:
        raise CapabilityError("elevenlabs_text_too_large", max_chars=5000)
    if not ELEVENLABS_VOICE_ID_RE.fullmatch(voice_id):
        raise CapabilityError("elevenlabs_invalid_voice_id")
    if not model_id:
        raise CapabilityError("elevenlabs_model_id_required")
    if not output_format:
        raise CapabilityError("elevenlabs_output_format_required")

    body: dict[str, Any] = {
        "text": text,
        "model_id": model_id,
    }
    voice_settings: dict[str, Any] = {}
    if stability is not None:
        voice_settings["stability"] = stability
    if similarity_boost is not None:
        voice_settings["similarity_boost"] = similarity_boost
    if style is not None:
        voice_settings["style"] = style
    if use_speaker_boost is not None:
        voice_settings["use_speaker_boost"] = use_speaker_boost
    if voice_settings:
        body["voice_settings"] = voice_settings

    async with httpx.AsyncClient(timeout=60.0) as client:
        response, oversized_payload = await limited_service_response(
            client,
            method="POST",
            url=f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
            params={"output_format": output_format},
            headers={
                "xi-api-key": api_key,
                "Accept": "audio/mpeg",
                "Content-Type": "application/json",
            },
            json_body=body,
            form_body=None,
            mode="artifact",
        )
    if oversized_payload is not None:
        return oversized_payload
    if response is None:
        raise CapabilityError("provider_response_missing")

    if response.status_code != 200:
        return {
            "ok": False,
            "http_status": response.status_code,
            "error": sanitized_api_error(response),
        }
    if len(response.content) > MAX_ARTIFACT_DOWNLOAD_BYTES:
        return {
            "ok": False,
            "http_status": response.status_code,
            "content_type": response.headers.get("content-type", "application/octet-stream"),
            "byte_size": len(response.content),
            "headers": safe_response_headers(response),
            "error": "response_too_large_for_artifact",
            "max_artifact_bytes": MAX_ARTIFACT_DOWNLOAD_BYTES,
        }

    mime_header = response.headers.get("content-type")
    mime_type = mime_header.split(";", maxsplit=1)[0].strip() if mime_header else ""
    if not mime_type.lower().startswith("audio/"):
        raise CapabilityError(
            "elevenlabs_unexpected_content_type",
            content_type=mime_type or None,
            http_status=response.status_code,
        )
    artifact = store_artifact(
        response.content,
        mime_type,
        {
            "voice_id": voice_id,
            "model_id": model_id,
            "output_format": output_format,
            "provider": "elevenlabs",
        },
    )
    artifact["download"] = {
        "url": artifact_download_url(resource_url, artifact["artifact_id"]),
        "authorization": "Bearer token required; same private MCP bearer token",
        "exposure": "same private MCP HTTP service; Traefik remains disabled",
    }
    return {"ok": True, **artifact}


def capability_error_payload(error: CapabilityError) -> dict[str, Any]:
    return {
        "ok": False,
        "error": error.code,
        **redacted_json(error.details),
    }


def openbao_error_payload(error: OpenBaoError) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ok": False,
        "error": error.code,
    }
    if error.status_code is not None:
        payload["openbao_status"] = error.status_code
    return payload


def bearer_token_from_request(request: Request) -> str | None:
    authorization = request.headers.get("authorization", "")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    return token


def artifact_response_for(artifact_id: str) -> FileResponse | JSONResponse:
    metadata = read_artifact_metadata(artifact_id)
    if metadata is None:
        return JSONResponse({"ok": False, "error": "artifact_not_found"}, status_code=404)

    byte_size = metadata.get("byte_size")
    if not isinstance(byte_size, int) or byte_size > MAX_ARTIFACT_DOWNLOAD_BYTES:
        return JSONResponse({"ok": False, "error": "artifact_too_large"}, status_code=413)

    filename = metadata.get("filename")
    mime_type = metadata.get("mime_type")
    if not isinstance(filename, str) or not isinstance(mime_type, str):
        return JSONResponse({"ok": False, "error": "artifact_metadata_invalid"}, status_code=500)

    path = artifact_dir() / filename
    if not path.exists() or not path.is_file():
        return JSONResponse({"ok": False, "error": "artifact_file_missing"}, status_code=404)

    return FileResponse(path, media_type=mime_type, filename=filename)


def build_mcp() -> FastMCP:
    token = require_env("HEISENBERG_ACCESS_MCP_TOKEN")
    resource_url = os.environ.get("HEISENBERG_ACCESS_MCP_RESOURCE_URL", "http://127.0.0.1:8020/mcp")
    openbao_addr = os.environ.get("OPENBAO_ADDR", "http://openbao-app:8200")
    openbao_token = os.environ.get("OPENBAO_TOKEN", "").strip() or None
    openbao = OpenBaoKV2(openbao_addr, openbao_token)

    mcp = FastMCP(
        name="heisenberg-access-mcp",
        host=os.environ.get("HEISENBERG_ACCESS_MCP_HOST", "0.0.0.0"),
        port=int(os.environ.get("HEISENBERG_ACCESS_MCP_PORT", "8000")),
        stateless_http=True,
        token_verifier=StaticBearerTokenVerifier(token),
        auth=AuthSettings(
            issuer_url=resource_url,
            resource_server_url=resource_url,
            required_scopes=["mcp:call"],
        ),
        transport_security=transport_security_settings(resource_url),
    )

    @mcp.tool()
    async def access_status(ctx: Context) -> dict[str, Any]:
        emit_event("mcp_tool_call", tool="access_status", client_id="heisenberg-access-mcp-env-token")
        return {
            "service": "heisenberg-access-mcp",
            "ok": True,
            "openbao": openbao_target_summary(openbao_addr),
            "capabilities": CAPABILITIES,
        }

    @mcp.tool()
    async def openbao_status(ctx: Context) -> dict[str, Any]:
        emit_event("mcp_tool_call", tool="openbao_status", client_id="heisenberg-access-mcp-env-token")
        try:
            return await read_openbao_health(openbao_addr, openbao_token)
        except httpx.HTTPError as error:
            emit_event("openbao_status_error", error_type=type(error).__name__)
            return {
                "reachable": False,
                "ready": False,
                "error": type(error).__name__,
            }

    @mcp.tool(name="x.get_tweet")
    async def x_get_tweet(ctx: Context, tweet_id_or_url: str) -> dict[str, Any]:
        emit_event("mcp_tool_call", tool="x.get_tweet", client_id="heisenberg-access-mcp-env-token")
        try:
            tweet_id = extract_tweet_id(tweet_id_or_url)
            oauth = await openbao.read("x_oauth")
            if expires_soon(oauth.get("expires_at")):
                oauth = await refresh_x_access_token(openbao, oauth)

            try:
                return await fetch_x_tweet(required_secret_value(oauth, "access_token"), tweet_id)
            except CapabilityError as error:
                if error.code != "x_tweet_request_failed" or error.details.get("http_status") != 401:
                    raise
                refreshed = await refresh_x_access_token(openbao, oauth)
                return await fetch_x_tweet(required_secret_value(refreshed, "access_token"), tweet_id)
        except OpenBaoError as error:
            emit_event("capability_error", tool="x.get_tweet", error=error.code, openbao_status=error.status_code)
            return openbao_error_payload(error)
        except CapabilityError as error:
            emit_event("capability_error", tool="x.get_tweet", error=error.code)
            return capability_error_payload(error)
        except httpx.HTTPError as error:
            emit_event("capability_error", tool="x.get_tweet", error=type(error).__name__)
            return {"ok": False, "error": type(error).__name__}

    @mcp.tool(name="google_health.access_status")
    async def google_health_access_status(ctx: Context) -> dict[str, Any]:
        emit_event("mcp_tool_call", tool="google_health.access_status", client_id="heisenberg-access-mcp-env-token")
        try:
            access_token, wrote_metadata = await refresh_google_access_token(openbao)
            status = await fetch_google_health_access_status(access_token)
            status["token_metadata_updated"] = wrote_metadata
            return status
        except OpenBaoError as error:
            emit_event(
                "capability_error",
                tool="google_health.access_status",
                error=error.code,
                openbao_status=error.status_code,
            )
            return openbao_error_payload(error)
        except CapabilityError as error:
            emit_event("capability_error", tool="google_health.access_status", error=error.code)
            return capability_error_payload(error)
        except httpx.HTTPError as error:
            emit_event("capability_error", tool="google_health.access_status", error=type(error).__name__)
            return {"ok": False, "error": type(error).__name__}

    @mcp.tool(name="google_health.list_data_types")
    async def google_health_list_data_types(ctx: Context) -> dict[str, Any]:
        """List documented Google Health fitness data types for exercise, workout, activity, daily log, steps, calories, distance, active minutes, heart rate, health datapoints, and date range tools."""
        emit_event("mcp_tool_call", tool="google_health.list_data_types", client_id="heisenberg-access-mcp-env-token")
        return google_health_activity_data_types_payload()

    @mcp.tool(name="google_health.get_exercise_data_points")
    async def google_health_get_exercise_data_points(
        ctx: Context,
        start_time: str | None = None,
        end_time: str | None = None,
        page_size: int | None = None,
        page_token: str | None = None,
    ) -> dict[str, Any]:
        """Read paginated Google Health exercise/workout fitness data points for a civil date range; useful for daily log, activity sync, workout history, distance, calories, steps, and health datapoints context."""
        emit_event("mcp_tool_call", tool="google_health.get_exercise_data_points", client_id="heisenberg-access-mcp-env-token")
        try:
            start = normalize_google_health_civil_time(
                start_time,
                default=google_health_default_start_date(30),
                name="start_time",
            )
            end = normalize_google_health_civil_time(
                end_time,
                default=google_health_default_end_date(),
                name="end_time",
            )
            normalized_page_size = normalize_page_size(page_size, default=10, minimum=1, maximum=25)
            access_token, wrote_metadata = await refresh_google_access_token(openbao)
            payload = await google_health_list_data_points(
                access_token,
                data_type="exercise",
                filter_expression=(
                    f'exercise.interval.civil_start_time >= "{start}" '
                    f'AND exercise.interval.civil_start_time < "{end}"'
                ),
                page_size=normalized_page_size,
                page_token=page_token.strip() if isinstance(page_token, str) and page_token.strip() else None,
            )
            payload["token_metadata_updated"] = wrote_metadata
            return payload
        except OpenBaoError as error:
            emit_event(
                "capability_error",
                tool="google_health.get_exercise_data_points",
                error=error.code,
                openbao_status=error.status_code,
            )
            return openbao_error_payload(error)
        except CapabilityError as error:
            emit_event("capability_error", tool="google_health.get_exercise_data_points", error=error.code)
            return capability_error_payload(error)
        except httpx.HTTPError as error:
            emit_event("capability_error", tool="google_health.get_exercise_data_points", error=type(error).__name__)
            return {"ok": False, "error": type(error).__name__}

    @mcp.tool(name="google_health.summarize_activity_day")
    async def google_health_summarize_activity_day(ctx: Context, date: str) -> dict[str, Any]:
        """Summarize one Google Health activity day for daily log / brain / fitness data sync: steps, calories, distance, active minutes, heart rate, exercise, workouts, activity, health datapoints, and date range context."""
        emit_event("mcp_tool_call", tool="google_health.summarize_activity_day", client_id="heisenberg-access-mcp-env-token")
        try:
            target_date = parse_iso_date(date)
            access_token, wrote_metadata = await refresh_google_access_token(openbao)
            payload = await summarize_google_health_activity_day(access_token, target_date)
            payload["token_metadata_updated"] = wrote_metadata
            return payload
        except OpenBaoError as error:
            emit_event(
                "capability_error",
                tool="google_health.summarize_activity_day",
                error=error.code,
                openbao_status=error.status_code,
            )
            return openbao_error_payload(error)
        except CapabilityError as error:
            emit_event("capability_error", tool="google_health.summarize_activity_day", error=error.code)
            return capability_error_payload(error)
        except httpx.HTTPError as error:
            emit_event("capability_error", tool="google_health.summarize_activity_day", error=type(error).__name__)
            return {"ok": False, "error": type(error).__name__}

    @mcp.tool(name="elevenlabs.request")
    async def elevenlabs_request(
        ctx: Context,
        method: str = "GET",
        path: str = "/v2/voices",
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        form_body: dict[str, Any] | None = None,
        response_mode: str = "auto",
        confirm: bool = False,
        dry_run: bool = False,
        operation: str | None = None,
    ) -> dict[str, Any]:
        """Request the ElevenLabs API within its fixed service scope; binary/large responses become artifacts."""
        emit_event("mcp_tool_call", tool="elevenlabs.request", client_id="heisenberg-access-mcp-env-token")
        try:
            if operation is not None:
                legacy = legacy_elevenlabs_operation(operation)
                if legacy is None:
                    raise CapabilityError(
                        "elevenlabs_operation_not_known",
                        known_operations=sorted(LEGACY_ELEVENLABS_OPERATIONS),
                    )
                method = legacy["method"]
                path = legacy["path"]
            if dry_run:
                return await service_request(
                    base_url="https://api.elevenlabs.io",
                    path=path,
                    method=method,
                    headers={},
                    resource_url=resource_url,
                    service_name="elevenlabs",
                    params=params,
                    json_body=json_body,
                    form_body=form_body,
                    response_mode=response_mode,
                    confirm=confirm,
                    dry_run=True,
                )
            ensure_write_confirmed(normalize_service_method(method), confirm)
            secret = await openbao.read("elevenlabs")
            api_key = required_secret_value(secret, "api_key")
            return await service_request(
                base_url="https://api.elevenlabs.io",
                path=path,
                method=method,
                headers={
                    "xi-api-key": api_key,
                    "Accept": "application/json, audio/*;q=0.9, */*;q=0.1",
                },
                resource_url=resource_url,
                service_name="elevenlabs",
                params=params,
                json_body=json_body,
                form_body=form_body,
                response_mode=response_mode,
                confirm=confirm,
                dry_run=dry_run,
            )
        except OpenBaoError as error:
            emit_event(
                "capability_error",
                tool="elevenlabs.request",
                error=error.code,
                openbao_status=error.status_code,
            )
            return openbao_error_payload(error)
        except CapabilityError as error:
            emit_event("capability_error", tool="elevenlabs.request", error=error.code)
            return capability_error_payload(error)
        except httpx.HTTPError as error:
            emit_event("capability_error", tool="elevenlabs.request", error=type(error).__name__)
            return {"ok": False, "error": type(error).__name__}

    @mcp.tool(name="homeassistant.request")
    async def homeassistant_request(
        ctx: Context,
        method: str = "GET",
        path: str = "/api/",
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        form_body: dict[str, Any] | None = None,
        response_mode: str = "auto",
        confirm: bool = False,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Request Home Assistant within the configured base URL using server-side bearer auth."""
        emit_event("mcp_tool_call", tool="homeassistant.request", client_id="heisenberg-access-mcp-env-token")
        try:
            if not dry_run:
                ensure_write_confirmed(normalize_service_method(method), confirm)
            secret = await openbao.read("homeassistant")
            base_url = required_secret_value(secret, "url")
            token_value = required_secret_value(secret, "token")
            return await service_request(
                base_url=base_url,
                path=path,
                method=method,
                headers={
                    "Authorization": f"Bearer {token_value}",
                    "Accept": "application/json, text/*;q=0.9, */*;q=0.1",
                },
                resource_url=resource_url,
                service_name="homeassistant",
                params=params,
                json_body=json_body,
                form_body=form_body,
                response_mode=response_mode,
                confirm=confirm,
                dry_run=dry_run,
            )
        except OpenBaoError as error:
            emit_event(
                "capability_error",
                tool="homeassistant.request",
                error=error.code,
                openbao_status=error.status_code,
            )
            return openbao_error_payload(error)
        except CapabilityError as error:
            emit_event("capability_error", tool="homeassistant.request", error=error.code)
            return capability_error_payload(error)
        except httpx.HTTPError as error:
            emit_event("capability_error", tool="homeassistant.request", error=type(error).__name__)
            return {"ok": False, "error": type(error).__name__}

    @mcp.tool(name="freshrss.request")
    async def freshrss_request(
        ctx: Context,
        method: str = "GET",
        path: str = "/reader/api/0/user-info",
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        form_body: dict[str, Any] | None = None,
        response_mode: str = "auto",
        confirm: bool = False,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Request FreshRSS within the configured API base URL using server-side API-password auth."""
        emit_event("mcp_tool_call", tool="freshrss.request", client_id="heisenberg-access-mcp-env-token")
        try:
            if not dry_run:
                ensure_write_confirmed(normalize_service_method(method), confirm)
            secret = await openbao.read("freshrss")
            api_url = required_secret_value(secret, "api_url")
            username = required_secret_value(secret, "username")
            api_password = required_secret_value(secret, "api_password")
            auth_token = "dry-run" if dry_run else await freshrss_auth_token(api_url, username, api_password)
            return await service_request(
                base_url=api_url,
                path=path,
                method=method,
                headers={
                    "Authorization": f"GoogleLogin auth={auth_token}",
                    "Accept": "application/json, text/*;q=0.9, */*;q=0.1",
                },
                resource_url=resource_url,
                service_name="freshrss",
                params=params,
                json_body=json_body,
                form_body=form_body,
                response_mode=response_mode,
                confirm=confirm,
                dry_run=dry_run,
            )
        except OpenBaoError as error:
            emit_event(
                "capability_error",
                tool="freshrss.request",
                error=error.code,
                openbao_status=error.status_code,
            )
            return openbao_error_payload(error)
        except CapabilityError as error:
            emit_event("capability_error", tool="freshrss.request", error=error.code)
            return capability_error_payload(error)
        except httpx.HTTPError as error:
            emit_event("capability_error", tool="freshrss.request", error=type(error).__name__)
            return {"ok": False, "error": type(error).__name__}

    @mcp.tool(name="tandoor.request")
    async def tandoor_request(
        ctx: Context,
        method: str = "GET",
        path: str = "/api/recipe/",
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        response_mode: str = "auto",
        confirm: bool = False,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Request Tandoor within the configured base URL using server-side bearer auth."""
        emit_event("mcp_tool_call", tool="tandoor.request", client_id="heisenberg-access-mcp-env-token")
        try:
            if not dry_run:
                ensure_write_confirmed(normalize_service_method(method), confirm)
            secret = await openbao.read("tandoor")
            base_url = required_secret_value(secret, "url")
            api_key = required_secret_value(secret, "api_key")
            return await service_request(
                base_url=base_url,
                path=path,
                method=method,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Accept": "application/json, text/*;q=0.9, */*;q=0.1",
                    "Accept-Encoding": "identity",
                },
                resource_url=resource_url,
                service_name="tandoor",
                params=params,
                json_body=json_body,
                form_body=None,
                response_mode=response_mode,
                confirm=confirm,
                dry_run=dry_run,
            )
        except OpenBaoError as error:
            emit_event(
                "capability_error",
                tool="tandoor.request",
                error=error.code,
                openbao_status=error.status_code,
            )
            return openbao_error_payload(error)
        except CapabilityError as error:
            emit_event("capability_error", tool="tandoor.request", error=error.code)
            return capability_error_payload(error)
        except httpx.HTTPError as error:
            emit_event("capability_error", tool="tandoor.request", error=type(error).__name__)
            return {"ok": False, "error": type(error).__name__}

    @mcp.tool(name="elevenlabs.text_to_speech")
    async def elevenlabs_text_to_speech(
        ctx: Context,
        text: str,
        voice_id: str,
        model_id: str = "eleven_multilingual_v2",
        output_format: str = "mp3_44100_128",
        stability: float | None = None,
        similarity_boost: float | None = None,
        style: float | None = None,
        use_speaker_boost: bool | None = None,
        confirm: bool = False,
    ) -> dict[str, Any]:
        emit_event("mcp_tool_call", tool="elevenlabs.text_to_speech", client_id="heisenberg-access-mcp-env-token")
        try:
            ensure_write_confirmed("POST", confirm)
            secret = await openbao.read("elevenlabs")
            api_key = required_secret_value(secret, "api_key")
            return await run_elevenlabs_text_to_speech(
                api_key,
                resource_url,
                text,
                voice_id,
                model_id,
                output_format,
                stability,
                similarity_boost,
                style,
                use_speaker_boost,
                confirm,
            )
        except OpenBaoError as error:
            emit_event(
                "capability_error",
                tool="elevenlabs.text_to_speech",
                error=error.code,
                openbao_status=error.status_code,
            )
            return openbao_error_payload(error)
        except CapabilityError as error:
            emit_event("capability_error", tool="elevenlabs.text_to_speech", error=error.code)
            return capability_error_payload(error)
        except httpx.HTTPError as error:
            emit_event("capability_error", tool="elevenlabs.text_to_speech", error=type(error).__name__)
            return {"ok": False, "error": type(error).__name__}

    return mcp


configure_logging()
mcp_server = build_mcp()


async def health(_: Request) -> JSONResponse:
    return JSONResponse({"ok": True, "service": "heisenberg-access-mcp"})


async def download_artifact(request: Request) -> Response:
    expected_token = require_env("HEISENBERG_ACCESS_MCP_TOKEN")
    provided_token = bearer_token_from_request(request)
    if provided_token is None or not hmac.compare_digest(provided_token, expected_token):
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)

    return artifact_response_for(request.path_params["artifact_id"])


@asynccontextmanager
async def lifespan(_: Starlette):
    async with mcp_server.session_manager.run():
        yield


app = Starlette(
    routes=[
        Route("/health", health, methods=["GET"]),
        Route("/artifacts/{artifact_id}", download_artifact, methods=["GET"]),
        Mount("/", app=mcp_server.streamable_http_app()),
    ],
    middleware=[Middleware(AccessLogMiddleware)],
    lifespan=lifespan,
)


def main() -> None:
    host = os.environ.get("HEISENBERG_ACCESS_MCP_HOST", "0.0.0.0")
    port = int(os.environ.get("HEISENBERG_ACCESS_MCP_PORT", "8000"))
    uvicorn.run(app, host=host, port=port, log_config=None)


if __name__ == "__main__":
    main()
