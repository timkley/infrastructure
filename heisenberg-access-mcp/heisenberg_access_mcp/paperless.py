"""Narrow Paperless-ngx document tools with opt-in, confirmed metadata updates.

The selected Paperless deployment is a server property.  Tool callers can only
refer to documents from that deployment and cannot supply URLs, paths, or HTTP
methods.
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager
from datetime import date
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx
from mcp.types import ToolAnnotations


PAPERLESS_CAPABILITIES: dict[str, dict[str, object]] = {
    "paperless.search_documents": {
        "tool": "paperless.search_documents",
        "enabled": True,
        "read_only": True,
        "scope": "searches the fixed configured Paperless source with bounded compact metadata",
    },
    "paperless.get_document": {
        "tool": "paperless.get_document",
        "enabled": True,
        "read_only": True,
        "scope": "gets compact metadata for one source-qualified Paperless document",
    },
    "paperless.read_document": {
        "tool": "paperless.read_document",
        "enabled": True,
        "read_only": True,
        "scope": "reads a bounded OCR-text slice for one source-qualified Paperless document",
    },
}

PAPERLESS_UPDATE_CAPABILITIES: dict[str, dict[str, object]] = {
    "paperless.list_metadata": {
        "tool": "paperless.list_metadata",
        "enabled": True,
        "read_only": True,
        "scope": "lists bounded tag, correspondent, or document-type names for the fixed Paperless source",
    },
    "paperless.update_document": {
        "tool": "paperless.update_document",
        "enabled": True,
        "read_only": False,
        "scope": "updates an allowlisted set of Paperless document details after explicit confirmation",
        "writes": "PATCHes one fixed-source document only with confirm=true; reads it back and verifies the requested fields",
    },
}

_SOURCES = frozenset({"private", "work"})
_DOCUMENT_ID_RE = re.compile(r"^[1-9][0-9]{0,18}$")
_MAX_QUERY_LENGTH = 2_000
_MAX_PAGE = 10_000
_MAX_PAGE_SIZE = 25
_MAX_READ_LIMIT = 20_000
_MAX_OFFSET = 2 * 1024 * 1024
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_MAX_TITLE_LENGTH = 512
_MAX_DATE_LENGTH = 64
_MAX_LABEL_LENGTH = 256
_MAX_TAGS = 100
_MAX_UPDATE_TITLE_LENGTH = 128
_UPDATE_FIELDS = frozenset({
    "title",
    "created",
    "correspondent",
    "document_type",
    "tags",
    "archive_serial_number",
})
_METADATA_ENDPOINTS = {
    "tags": "/api/tags/",
    "correspondents": "/api/correspondents/",
    "document_types": "/api/document_types/",
}
_READ_ONLY_ANNOTATIONS = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=True,
)
_UPDATE_ANNOTATIONS = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=True,
    idempotentHint=False,
    openWorldHint=True,
)

CredentialsLoader = Callable[[], Awaitable[Mapping[str, object]]]
ClientFactory = Callable[..., AbstractAsyncContextManager[httpx.AsyncClient]]


class PaperlessError(Exception):
    """A deliberate, safe-to-return Paperless failure code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _require_source(source: str) -> str:
    if source not in _SOURCES:
        raise PaperlessError("paperless_source_not_allowed")
    return source


def _require_document_id(document_ref: object, source: str) -> int:
    if not isinstance(document_ref, str):
        raise PaperlessError("paperless_document_ref_invalid")
    prefix, separator, raw_id = document_ref.partition(":")
    if separator != ":" or prefix != source or not _DOCUMENT_ID_RE.fullmatch(raw_id):
        raise PaperlessError("paperless_document_ref_invalid")
    return int(raw_id)


def _require_query(query: object) -> str:
    if not isinstance(query, str):
        raise PaperlessError("paperless_query_invalid")
    normalized = query.strip()
    if not normalized or len(normalized) > _MAX_QUERY_LENGTH:
        raise PaperlessError("paperless_query_invalid")
    return normalized


def _require_int(value: object, *, code: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise PaperlessError(code)
    return value


def _normalize_positive_id(value: object, *, allow_none: bool) -> int | None:
    if value is None and allow_none:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise PaperlessError("paperless_update_changes_invalid")
    return value


def _normalize_created_date(value: object, *, response_value: bool = False) -> str:
    if not isinstance(value, str):
        raise PaperlessError("paperless_update_changes_invalid")
    candidate = value[:10] if response_value else value
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", candidate):
        raise PaperlessError("paperless_update_changes_invalid")
    try:
        return date.fromisoformat(candidate).isoformat()
    except ValueError:
        raise PaperlessError("paperless_update_changes_invalid") from None


def _normalize_tags(value: object) -> list[int]:
    if not isinstance(value, list) or len(value) > _MAX_TAGS:
        raise PaperlessError("paperless_update_changes_invalid")
    tags = [_normalize_positive_id(item, allow_none=False) for item in value]
    if len(set(tags)) != len(tags):
        raise PaperlessError("paperless_update_changes_invalid")
    return sorted(tags)


def _normalize_update_changes(changes: object) -> dict[str, object]:
    if not isinstance(changes, Mapping) or not changes:
        raise PaperlessError("paperless_update_changes_invalid")
    if any(not isinstance(key, str) or key not in _UPDATE_FIELDS for key in changes):
        raise PaperlessError("paperless_update_changes_invalid")
    normalized: dict[str, object] = {}
    for key, value in changes.items():
        if key == "title":
            if not isinstance(value, str) or not value.strip() or len(value) > _MAX_UPDATE_TITLE_LENGTH:
                raise PaperlessError("paperless_update_changes_invalid")
            normalized[key] = value
        elif key == "created":
            normalized[key] = _normalize_created_date(value)
        elif key in {"correspondent", "document_type"}:
            normalized[key] = _normalize_positive_id(value, allow_none=True)
        elif key == "tags":
            normalized[key] = _normalize_tags(value)
        elif key == "archive_serial_number":
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 4_294_967_295
            ):
                raise PaperlessError("paperless_update_changes_invalid")
            normalized[key] = value
    return normalized


def _require_metadata_kind(kind: object) -> str:
    if not isinstance(kind, str) or kind not in _METADATA_ENDPOINTS:
        raise PaperlessError("paperless_metadata_kind_invalid")
    return kind


def _optional_metadata_query(query: object) -> str:
    if not isinstance(query, str) or len(query) > _MAX_QUERY_LENGTH:
        raise PaperlessError("paperless_query_invalid")
    return query.strip()


def _normalize_base_url(value: object, *, require_https: bool) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PaperlessError("paperless_configuration_invalid")
    try:
        parsed = urlsplit(value.strip())
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError
        scheme = parsed.scheme.lower()
        if require_https and scheme != "https":
            raise ValueError
        port = parsed.port  # Access this early so malformed ports never reach a request.
        hostname = parsed.hostname
        if hostname is None:
            raise ValueError
        normalized_host = hostname.lower()
        if ":" in normalized_host:
            normalized_host = f"[{normalized_host}]"
        if port is not None and (scheme, port) not in {("http", 80), ("https", 443)}:
            normalized_host = f"{normalized_host}:{port}"
    except (TypeError, ValueError):
        raise PaperlessError("paperless_configuration_invalid") from None
    return urlunsplit((scheme, normalized_host, parsed.path.rstrip("/"), "", ""))


async def _load_configuration(
    load_credentials: CredentialsLoader,
    *,
    expected_base_url: str,
) -> tuple[str, str]:
    try:
        credentials = await load_credentials()
    except PaperlessError:
        raise
    except Exception as error:
        # A loader can contain secret-bearing exception text.  Never preserve it.
        raise PaperlessError("paperless_credentials_unavailable") from error
    if not isinstance(credentials, Mapping):
        raise PaperlessError("paperless_configuration_invalid")
    base_url = _normalize_base_url(credentials.get("url"), require_https=False)
    if base_url != expected_base_url:
        raise PaperlessError("paperless_instance_mismatch")
    api_token = credentials.get("api_token")
    if not isinstance(api_token, str) or not api_token.strip():
        raise PaperlessError("paperless_configuration_invalid")
    return base_url, api_token.strip()


def _endpoint(base_url: str, path: str) -> str:
    return f"{base_url}{path}"


def _document_url(base_url: str, document_id: int) -> str:
    return _endpoint(base_url, f"/documents/{document_id}/details/")


async def _get_json(
    *,
    base_url: str,
    api_token: str,
    path: str,
    params: Mapping[str, object] | None,
    client_factory: ClientFactory,
) -> object:
    try:
        async with client_factory(timeout=httpx.Timeout(10.0, connect=5.0), follow_redirects=False) as client:
            async with client.stream(
                "GET",
                _endpoint(base_url, path),
                params=params,
                headers={"Authorization": f"Token {api_token}", "Accept": "application/json"},
            ) as response:
                if 300 <= response.status_code < 400:
                    raise PaperlessError("paperless_redirect_refused")
                if response.status_code != 200:
                    raise PaperlessError("paperless_request_failed")
                chunks: list[bytes] = []
                received = 0
                async for chunk in response.aiter_bytes():
                    received += len(chunk)
                    if received > _MAX_RESPONSE_BYTES:
                        raise PaperlessError("paperless_response_too_large")
                    chunks.append(chunk)
    except PaperlessError:
        raise
    except httpx.HTTPError as error:
        raise PaperlessError("paperless_request_failed") from error
    try:
        return json.loads(b"".join(chunks))
    except (TypeError, UnicodeDecodeError, json.JSONDecodeError):
        raise PaperlessError("paperless_response_invalid") from None


async def _patch_document(
    *,
    base_url: str,
    api_token: str,
    document_id: int,
    changes: Mapping[str, object],
    client_factory: ClientFactory,
) -> None:
    """Make one PATCH attempt. Any ambiguous result is deliberately not retried."""
    try:
        async with client_factory(timeout=httpx.Timeout(10.0, connect=5.0), follow_redirects=False) as client:
            async with client.stream(
                "PATCH",
                _endpoint(base_url, f"/api/documents/{document_id}/"),
                headers={
                    "Authorization": f"Token {api_token}",
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
                json=dict(changes),
            ) as response:
                if 300 <= response.status_code < 400:
                    raise PaperlessError("paperless_update_outcome_uncertain")
                if 400 <= response.status_code < 500:
                    raise PaperlessError("paperless_update_rejected")
                if response.status_code not in {200, 204}:
                    raise PaperlessError("paperless_update_outcome_uncertain")
                chunks: list[bytes] = []
                received = 0
                async for chunk in response.aiter_bytes():
                    received += len(chunk)
                    if received > _MAX_RESPONSE_BYTES:
                        raise PaperlessError("paperless_update_outcome_uncertain")
                    chunks.append(chunk)
    except PaperlessError:
        raise
    except httpx.HTTPError as error:
        raise PaperlessError("paperless_update_outcome_uncertain") from error
    if response.status_code == 200:
        try:
            receipt = json.loads(b"".join(chunks))
        except (TypeError, UnicodeDecodeError, json.JSONDecodeError):
            raise PaperlessError("paperless_update_outcome_uncertain") from None
        if not isinstance(receipt, Mapping):
            raise PaperlessError("paperless_update_outcome_uncertain")


def _compact_text(value: object, *, maximum: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise PaperlessError("paperless_response_invalid")
    return value[:maximum]


def _compact_label(value: object) -> int | str | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise PaperlessError("paperless_response_invalid")
    return value if isinstance(value, int) else value[:_MAX_LABEL_LENGTH]


def _compact_tags(value: object) -> list[int | str]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > _MAX_TAGS:
        raise PaperlessError("paperless_response_invalid")
    tags: list[int | str] = []
    for item in value:
        compact = _compact_label(item)
        if compact is not None:
            tags.append(compact)
    return tags


def _document_metadata(
    document: object,
    *,
    source: str,
    base_url: str,
    expected_document_id: int | None = None,
) -> dict[str, object]:
    if not isinstance(document, Mapping):
        raise PaperlessError("paperless_response_invalid")
    document_id = document.get("id")
    if isinstance(document_id, bool) or not isinstance(document_id, int) or document_id < 1:
        raise PaperlessError("paperless_response_invalid")
    if expected_document_id is not None and document_id != expected_document_id:
        raise PaperlessError("paperless_response_invalid")
    title = document.get("title")
    if not isinstance(title, str):
        raise PaperlessError("paperless_response_invalid")
    return {
        "source": source,
        "ref": f"{source}:{document_id}",
        "url": _document_url(base_url, document_id),
        "id": document_id,
        "title": title[:_MAX_TITLE_LENGTH],
        "created": _compact_text(document.get("created"), maximum=_MAX_DATE_LENGTH),
        "modified": _compact_text(document.get("modified"), maximum=_MAX_DATE_LENGTH),
        "added": _compact_text(document.get("added"), maximum=_MAX_DATE_LENGTH),
        "correspondent": _compact_label(document.get("correspondent")),
        "document_type": _compact_label(document.get("document_type")),
        "tags": _compact_tags(document.get("tags")),
        "archive_serial_number": _compact_label(document.get("archive_serial_number")),
    }


def _update_values_from_document(document: object, fields: Mapping[str, object]) -> dict[str, object]:
    if not isinstance(document, Mapping):
        raise PaperlessError("paperless_response_invalid")
    values: dict[str, object] = {}
    for field in fields:
        value = document.get(field)
        if field == "title":
            if not isinstance(value, str) or len(value) > _MAX_UPDATE_TITLE_LENGTH:
                raise PaperlessError("paperless_response_invalid")
            values[field] = value
        elif field == "created":
            values[field] = _normalize_created_date(value, response_value=True)
        elif field in {"correspondent", "document_type"}:
            values[field] = _normalize_positive_id(value, allow_none=True)
        elif field == "tags":
            values[field] = _normalize_tags(value)
        elif field == "archive_serial_number":
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 4_294_967_295
            ):
                raise PaperlessError("paperless_response_invalid")
            values[field] = value
    return values


def _update_preview(
    document: object,
    *,
    source: str,
    base_url: str,
    document_id: int,
    changes: Mapping[str, object],
    dry_run: bool,
    verified: bool,
) -> dict[str, object]:
    metadata = _document_metadata(
        document,
        source=source,
        base_url=base_url,
        expected_document_id=document_id,
    )
    before = _update_values_from_document(document, changes)
    after = {**before, **changes}
    return {
        "ok": True,
        **metadata,
        "dry_run": dry_run,
        "before": before,
        "after": after,
        "changed_fields": list(changes),
        "verified": verified,
    }


def _search_result(payload: object, *, source: str, base_url: str, page: int, page_size: int) -> dict[str, object]:
    if not isinstance(payload, Mapping):
        raise PaperlessError("paperless_response_invalid")
    results = payload.get("results")
    count = payload.get("count")
    if (
        not isinstance(results, list)
        or isinstance(count, bool)
        or not isinstance(count, int)
        or count < 0
    ):
        raise PaperlessError("paperless_response_invalid")
    if len(results) > page_size or count < len(results):
        raise PaperlessError("paperless_response_invalid")
    documents = [_document_metadata(item, source=source, base_url=base_url) for item in results]
    has_more = page * page_size < count
    return {
        "ok": True,
        "source": source,
        "query_page": page,
        "page_size": page_size,
        "count": count,
        "documents": documents,
        "has_more": has_more,
        "next_page": page + 1 if has_more else None,
    }


def _metadata_result(
    payload: object,
    *,
    source: str,
    kind: str,
    page: int,
    page_size: int,
) -> dict[str, object]:
    if not isinstance(payload, Mapping):
        raise PaperlessError("paperless_response_invalid")
    results = payload.get("results")
    count = payload.get("count")
    if (
        not isinstance(results, list)
        or isinstance(count, bool)
        or not isinstance(count, int)
        or count < 0
        or len(results) > page_size
        or count < len(results)
    ):
        raise PaperlessError("paperless_response_invalid")
    items: list[dict[str, object]] = []
    for item in results:
        if not isinstance(item, Mapping):
            raise PaperlessError("paperless_response_invalid")
        item_id = item.get("id")
        name = item.get("name")
        if (
            isinstance(item_id, bool)
            or not isinstance(item_id, int)
            or item_id < 1
            or not isinstance(name, str)
        ):
            raise PaperlessError("paperless_response_invalid")
        items.append({"id": item_id, "name": name[:_MAX_LABEL_LENGTH]})
    has_more = page * page_size < count
    return {
        "ok": True,
        "source": source,
        "kind": kind,
        "query_page": page,
        "page_size": page_size,
        "count": count,
        "items": items,
        "has_more": has_more,
        "next_page": page + 1 if has_more else None,
    }


def _read_result(
    payload: object,
    *,
    source: str,
    base_url: str,
    document_id: int,
    offset: int,
    limit: int,
) -> dict[str, object]:
    if not isinstance(payload, Mapping):
        raise PaperlessError("paperless_response_invalid")
    response_document_id = payload.get("id")
    if (
        isinstance(response_document_id, bool)
        or not isinstance(response_document_id, int)
        or response_document_id != document_id
    ):
        raise PaperlessError("paperless_response_invalid")
    content = payload.get("content")
    if not isinstance(content, str):
        raise PaperlessError("paperless_response_invalid")
    text = content[offset : offset + limit]
    next_offset = offset + len(text)
    has_more = next_offset < len(content)
    return {
        "ok": True,
        "source": source,
        "ref": f"{source}:{document_id}",
        "url": _document_url(base_url, document_id),
        "offset": offset,
        "limit": limit,
        "text": text,
        "has_more": has_more,
        "next_offset": next_offset if has_more else None,
    }


def _error_payload(error: PaperlessError, *, source: str) -> dict[str, object]:
    payload: dict[str, object] = {"ok": False, "source": source, "error": error.code}
    if error.code == "paperless_update_outcome_uncertain":
        payload["description"] = "The update outcome is uncertain. Read the current document state before retrying."
    return payload


def register_paperless_tools(
    mcp: Any,
    *,
    source: str,
    load_credentials: CredentialsLoader,
    expected_base_url: str,
    allow_updates: bool = False,
    client_factory: ClientFactory = httpx.AsyncClient,
) -> None:
    """Register exactly the bounded Paperless read tools for one fixed source."""
    source = _require_source(source)
    if not isinstance(allow_updates, bool):
        raise PaperlessError("paperless_allow_updates_invalid")
    normalized_expected_base_url = _normalize_base_url(expected_base_url, require_https=True)

    @mcp.tool(
        name="paperless.search_documents",
        description=f"Search the fixed {source} Paperless source with bounded, compact document metadata.",
        annotations=_READ_ONLY_ANNOTATIONS,
    )
    async def search_documents(query: str, page: int = 1, page_size: int = 10) -> dict[str, object]:
        """Search this fixed Paperless source with bounded, compact document metadata."""
        try:
            normalized_query = _require_query(query)
            normalized_page = _require_int(page, code="paperless_page_invalid", minimum=1, maximum=_MAX_PAGE)
            normalized_page_size = _require_int(
                page_size,
                code="paperless_page_size_invalid",
                minimum=1,
                maximum=_MAX_PAGE_SIZE,
            )
            base_url, api_token = await _load_configuration(
                load_credentials,
                expected_base_url=normalized_expected_base_url,
            )
            payload = await _get_json(
                base_url=base_url,
                api_token=api_token,
                path="/api/documents/",
                params={"query": normalized_query, "page": normalized_page, "page_size": normalized_page_size},
                client_factory=client_factory,
            )
            return _search_result(
                payload,
                source=source,
                base_url=base_url,
                page=normalized_page,
                page_size=normalized_page_size,
            )
        except PaperlessError as error:
            return _error_payload(error, source=source)
        except Exception as error:
            return _error_payload(PaperlessError("paperless_request_failed"), source=source)

    @mcp.tool(
        name="paperless.get_document",
        description=f"Get compact metadata for a source-qualified document in the fixed {source} Paperless source.",
        annotations=_READ_ONLY_ANNOTATIONS,
    )
    async def get_document(document_ref: str) -> dict[str, object]:
        """Get compact metadata for a source-qualified document reference in this fixed source."""
        try:
            document_id = _require_document_id(document_ref, source)
            base_url, api_token = await _load_configuration(
                load_credentials,
                expected_base_url=normalized_expected_base_url,
            )
            payload = await _get_json(
                base_url=base_url,
                api_token=api_token,
                path=f"/api/documents/{document_id}/",
                params=None,
                client_factory=client_factory,
            )
            return {
                "ok": True,
                **_document_metadata(
                    payload,
                    source=source,
                    base_url=base_url,
                    expected_document_id=document_id,
                ),
            }
        except PaperlessError as error:
            return _error_payload(error, source=source)
        except Exception as error:
            return _error_payload(PaperlessError("paperless_request_failed"), source=source)

    @mcp.tool(
        name="paperless.read_document",
        description=f"Read a bounded OCR-text slice from the fixed {source} Paperless source.",
        annotations=_READ_ONLY_ANNOTATIONS,
    )
    async def read_document(document_ref: str, offset: int = 0, limit: int = 8_000) -> dict[str, object]:
        """Read a bounded OCR-text slice for a source-qualified document in this fixed source."""
        try:
            document_id = _require_document_id(document_ref, source)
            normalized_offset = _require_int(
                offset,
                code="paperless_offset_invalid",
                minimum=0,
                maximum=_MAX_OFFSET,
            )
            normalized_limit = _require_int(
                limit,
                code="paperless_limit_invalid",
                minimum=1,
                maximum=_MAX_READ_LIMIT,
            )
            base_url, api_token = await _load_configuration(
                load_credentials,
                expected_base_url=normalized_expected_base_url,
            )
            payload = await _get_json(
                base_url=base_url,
                api_token=api_token,
                path=f"/api/documents/{document_id}/",
                params=None,
                client_factory=client_factory,
            )
            return _read_result(
                payload,
                source=source,
                base_url=base_url,
                document_id=document_id,
                offset=normalized_offset,
                limit=normalized_limit,
            )
        except PaperlessError as error:
            return _error_payload(error, source=source)
        except Exception as error:
            return _error_payload(PaperlessError("paperless_request_failed"), source=source)

    if not allow_updates:
        return

    @mcp.tool(
        name="paperless.list_metadata",
        description=(
            f"List bounded tag, correspondent, or document-type names from the fixed {source} Paperless "
            "source for a document-details update."
        ),
        annotations=_READ_ONLY_ANNOTATIONS,
    )
    async def list_metadata(
        kind: str,
        query: str = "",
        page: int = 1,
        page_size: int = 25,
    ) -> dict[str, object]:
        """List fixed-source metadata for the allowlisted document-detail relations only."""
        try:
            normalized_kind = _require_metadata_kind(kind)
            normalized_query = _optional_metadata_query(query)
            normalized_page = _require_int(page, code="paperless_page_invalid", minimum=1, maximum=_MAX_PAGE)
            normalized_page_size = _require_int(
                page_size,
                code="paperless_page_size_invalid",
                minimum=1,
                maximum=_MAX_PAGE_SIZE,
            )
            base_url, api_token = await _load_configuration(
                load_credentials,
                expected_base_url=normalized_expected_base_url,
            )
            params: dict[str, object] = {"page": normalized_page, "page_size": normalized_page_size}
            if normalized_query:
                params["name__icontains"] = normalized_query
            payload = await _get_json(
                base_url=base_url,
                api_token=api_token,
                path=_METADATA_ENDPOINTS[normalized_kind],
                params=params,
                client_factory=client_factory,
            )
            return _metadata_result(
                payload,
                source=source,
                kind=normalized_kind,
                page=normalized_page,
                page_size=normalized_page_size,
            )
        except PaperlessError as error:
            return _error_payload(error, source=source)
        except Exception as error:
            return _error_payload(PaperlessError("paperless_request_failed"), source=source)

    @mcp.tool(
        name="paperless.update_document",
        description=(
            f"Update only title (max 128 characters), created (YYYY-MM-DD), correspondent or document_type "
            f"(positive integer or null), tags (full replacement list of positive integers), and "
            f"archive_serial_number (0..4294967295 or null) in the fixed {source} Paperless source after "
            "confirm=true. A dry run reads current values without writing."
        ),
        annotations=_UPDATE_ANNOTATIONS,
    )
    async def update_document(
        document_ref: str,
        changes: dict[str, object],
        confirm: bool = False,
        dry_run: bool = False,
    ) -> dict[str, object]:
        """Update title<=128, created YYYY-MM-DD, relation IDs/null, replacement tags, or ASN/null; then verify."""
        try:
            document_id = _require_document_id(document_ref, source)
            normalized_changes = _normalize_update_changes(changes)
            if not isinstance(dry_run, bool):
                raise PaperlessError("paperless_dry_run_invalid")
            if not dry_run and confirm is not True:
                raise PaperlessError("paperless_update_confirmation_required")
            base_url, api_token = await _load_configuration(
                load_credentials,
                expected_base_url=normalized_expected_base_url,
            )
            current = await _get_json(
                base_url=base_url,
                api_token=api_token,
                path=f"/api/documents/{document_id}/",
                params=None,
                client_factory=client_factory,
            )
            _document_metadata(
                current,
                source=source,
                base_url=base_url,
                expected_document_id=document_id,
            )
            before = _update_values_from_document(current, normalized_changes)
            if dry_run:
                return _update_preview(
                    current,
                    source=source,
                    base_url=base_url,
                    document_id=document_id,
                    changes=normalized_changes,
                    dry_run=True,
                    verified=False,
                )
        except PaperlessError as error:
            return _error_payload(error, source=source)
        except Exception as error:
            return _error_payload(PaperlessError("paperless_request_failed"), source=source)

        try:
            await _patch_document(
                base_url=base_url,
                api_token=api_token,
                document_id=document_id,
                changes=normalized_changes,
                client_factory=client_factory,
            )
        except PaperlessError as error:
            if error.code in {"paperless_redirect_refused", "paperless_update_rejected"}:
                return _error_payload(error, source=source)
            return _error_payload(PaperlessError("paperless_update_outcome_uncertain"), source=source)
        except Exception as error:
            return _error_payload(PaperlessError("paperless_update_outcome_uncertain"), source=source)

        try:
            persisted = await _get_json(
                base_url=base_url,
                api_token=api_token,
                path=f"/api/documents/{document_id}/",
                params=None,
                client_factory=client_factory,
            )
            after = _update_values_from_document(persisted, normalized_changes)
            if after != normalized_changes:
                raise PaperlessError("paperless_update_outcome_uncertain")
            metadata = _document_metadata(
                persisted,
                source=source,
                base_url=base_url,
                expected_document_id=document_id,
            )
        except Exception as error:
            return _error_payload(PaperlessError("paperless_update_outcome_uncertain"), source=source)

        return {
            "ok": True,
            **metadata,
            "dry_run": False,
            "before": before,
            "after": after,
            "changed_fields": list(normalized_changes),
            "verified": True,
        }
