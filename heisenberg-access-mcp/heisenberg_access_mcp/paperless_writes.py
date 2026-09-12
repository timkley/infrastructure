"""Confirmed fixed-source Paperless delete and correspondent-create tools."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import httpx
from mcp.types import ToolAnnotations

from .paperless import (
    ClientFactory, CredentialsLoader, PaperlessError, _document_metadata,
    _endpoint, _error_payload, _get_json, _load_configuration,
    _normalize_base_url, _require_document_id,
)


PAPERLESS_WRITE_CAPABILITIES: dict[str, dict[str, object]] = {
    "paperless.delete_document": {
        "tool": "paperless.delete_document", "enabled": True, "read_only": False,
        "scope": "moves one confirmed fixed-source document to Paperless trash without exposing permanent deletion",
        "writes": "DELETEs one fixed-source document with confirm=true and verifies it is absent from active documents",
    },
    "paperless.create_correspondent": {
        "tool": "paperless.create_correspondent", "enabled": True, "read_only": False,
        "scope": "creates one confirmed fixed-source correspondent by name after an exact duplicate lookup",
        "writes": "POSTs only a name with confirm=true and reads the created correspondent back by ID",
    },
}

_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_MAX_CORRESPONDENT_NAME_LENGTH = 128
_DUPLICATE_PAGE_SIZE = 25
_DELETE_ANNOTATIONS = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True)
_CREATE_ANNOTATIONS = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True)


def _require_source(source: object) -> str:
    if source not in {"private", "work"}:
        raise PaperlessError("paperless_source_not_allowed")
    return source


def _require_dry_run(value: object) -> bool:
    if not isinstance(value, bool):
        raise PaperlessError("paperless_dry_run_invalid")
    return value


def _require_correspondent_name(value: object) -> str:
    if not isinstance(value, str):
        raise PaperlessError("paperless_correspondent_name_invalid")
    name = value.strip()
    if not name or len(name) > _MAX_CORRESPONDENT_NAME_LENGTH:
        raise PaperlessError("paperless_correspondent_name_invalid")
    return name


def _correspondent(payload: object, *, expected_id: int | None = None, expected_name: str | None = None) -> dict[str, object]:
    if not isinstance(payload, Mapping):
        raise PaperlessError("paperless_response_invalid")
    correspondent_id, name = payload.get("id"), payload.get("name")
    if isinstance(correspondent_id, bool) or not isinstance(correspondent_id, int) or correspondent_id < 1 or not isinstance(name, str) or not name:
        raise PaperlessError("paperless_response_invalid")
    if expected_id is not None and correspondent_id != expected_id:
        raise PaperlessError("paperless_response_invalid")
    if expected_name is not None and name != expected_name:
        raise PaperlessError("paperless_response_invalid")
    return {"id": correspondent_id, "name": name[:_MAX_CORRESPONDENT_NAME_LENGTH]}


async def _mutation_request(*, method: str, base_url: str, api_token: str, path: str, client_factory: ClientFactory, json_body: Mapping[str, object] | None = None) -> tuple[int, object | None]:
    """Issue one bounded non-retried mutation. Redirects and transport errors are uncertain."""
    try:
        async with client_factory(timeout=httpx.Timeout(10.0, connect=5.0), follow_redirects=False) as client:
            async with client.stream(
                method, _endpoint(base_url, path),
                headers={"Authorization": f"Token {api_token}", "Accept": "application/json", "Content-Type": "application/json"},
                json=dict(json_body) if json_body is not None else None,
            ) as response:
                if 300 <= response.status_code < 400:
                    raise PaperlessError("paperless_mutation_outcome_uncertain")
                chunks: list[bytes] = []
                received = 0
                async for chunk in response.aiter_bytes():
                    received += len(chunk)
                    if received > _MAX_RESPONSE_BYTES:
                        raise PaperlessError("paperless_mutation_outcome_uncertain")
                    chunks.append(chunk)
    except PaperlessError:
        raise
    except httpx.HTTPError as error:
        raise PaperlessError("paperless_mutation_outcome_uncertain") from error
    body = b"".join(chunks)
    if not body:
        return response.status_code, None
    try:
        return response.status_code, json.loads(body)
    except (TypeError, UnicodeDecodeError, json.JSONDecodeError):
        raise PaperlessError("paperless_mutation_outcome_uncertain") from None


async def _active_document_absent(*, base_url: str, api_token: str, document_id: int, client_factory: ClientFactory) -> bool:
    """Check only active documents; Paperless trash hides foreign-owned records from this account."""
    try:
        async with client_factory(timeout=httpx.Timeout(10.0, connect=5.0), follow_redirects=False) as client:
            async with client.stream(
                "GET", _endpoint(base_url, f"/api/documents/{document_id}/"),
                headers={"Authorization": f"Token {api_token}", "Accept": "application/json"},
            ) as response:
                if 300 <= response.status_code < 400:
                    raise PaperlessError("paperless_delete_outcome_uncertain")
                received = 0
                async for chunk in response.aiter_bytes():
                    received += len(chunk)
                    if received > _MAX_RESPONSE_BYTES:
                        raise PaperlessError("paperless_delete_outcome_uncertain")
    except PaperlessError:
        raise
    except httpx.HTTPError as error:
        raise PaperlessError("paperless_delete_outcome_uncertain") from error
    return response.status_code == 404


async def _find_duplicate_correspondent(*, base_url: str, api_token: str, name: str, client_factory: ClientFactory) -> dict[str, object] | None:
    payload = await _get_json(
        base_url=base_url, api_token=api_token, path="/api/correspondents/",
        params={"name__iexact": name, "page": 1, "page_size": _DUPLICATE_PAGE_SIZE}, client_factory=client_factory,
    )
    if not isinstance(payload, Mapping):
        raise PaperlessError("paperless_response_invalid")
    count, results, next_page = payload.get("count"), payload.get("results"), payload.get("next")
    if isinstance(count, bool) or not isinstance(count, int) or count < 0 or not isinstance(results, list) or len(results) > _DUPLICATE_PAGE_SIZE or count < len(results):
        raise PaperlessError("paperless_response_invalid")
    if any(not isinstance(item, Mapping) for item in results):
        raise PaperlessError("paperless_response_invalid")
    if count != len(results) or next_page is not None:
        raise PaperlessError("paperless_correspondent_duplicate_lookup_ambiguous")
    matches = [_correspondent(item) for item in results if item.get("name") == name]
    if len(matches) == 1:
        return matches[0]
    if count != 0:
        raise PaperlessError("paperless_correspondent_duplicate_lookup_ambiguous")
    return None


def register_paperless_write_tools(mcp: Any, *, source: str, load_credentials: CredentialsLoader, expected_base_url: str, client_factory: ClientFactory = httpx.AsyncClient) -> None:
    """Register the two fixed-source write tools; callers cannot supply URLs or methods."""
    source = _require_source(source)
    normalized_expected_base_url = _normalize_base_url(expected_base_url, require_https=True)

    @mcp.tool(name="paperless.delete_document", description="Move one fixed-source document to trash after confirm=true; no permanent-delete path exists.", annotations=_DELETE_ANNOTATIONS)
    async def delete_document(document_ref: str, confirm: bool = False, dry_run: bool = False) -> dict[str, object]:
        """Soft-delete one fixed-source document and prove it is no longer active."""
        try:
            document_id = _require_document_id(document_ref, source)
            is_dry_run = _require_dry_run(dry_run)
            if not is_dry_run and confirm is not True:
                raise PaperlessError("paperless_delete_confirmation_required")
            base_url, api_token = await _load_configuration(load_credentials, expected_base_url=normalized_expected_base_url)
            current = await _get_json(base_url=base_url, api_token=api_token, path=f"/api/documents/{document_id}/", params=None, client_factory=client_factory)
            metadata = _document_metadata(current, source=source, base_url=base_url, expected_document_id=document_id)
            if is_dry_run:
                return {"ok": True, **metadata, "dry_run": True, "would_move_to_trash": True, "verified": False}
        except PaperlessError as error:
            return _error_payload(error, source=source)
        except Exception:
            return _error_payload(PaperlessError("paperless_request_failed"), source=source)
        try:
            status, _ = await _mutation_request(method="DELETE", base_url=base_url, api_token=api_token, path=f"/api/documents/{document_id}/", client_factory=client_factory)
            if 400 <= status < 500:
                return _error_payload(PaperlessError("paperless_delete_rejected"), source=source)
            if status != 204 or not await _active_document_absent(base_url=base_url, api_token=api_token, document_id=document_id, client_factory=client_factory):
                return _error_payload(PaperlessError("paperless_delete_outcome_uncertain"), source=source)
        except Exception:
            return _error_payload(PaperlessError("paperless_delete_outcome_uncertain"), source=source)
        return {"ok": True, **metadata, "dry_run": False, "deletion_mode": "trash", "active_document_absent": True, "trash_membership_verified": False, "verification_note": "The Paperless trash API does not expose foreign-owned documents to this account.", "verified": True}

    @mcp.tool(name="paperless.create_correspondent", description="Create one fixed-source correspondent by name after confirm=true and an exact duplicate lookup.", annotations=_CREATE_ANNOTATIONS)
    async def create_correspondent(name: str, confirm: bool = False, dry_run: bool = False) -> dict[str, object]:
        """Create one name-only correspondent, or return its exact-name duplicate without writing."""
        try:
            normalized_name = _require_correspondent_name(name)
            is_dry_run = _require_dry_run(dry_run)
            if not is_dry_run and confirm is not True:
                raise PaperlessError("paperless_create_correspondent_confirmation_required")
            base_url, api_token = await _load_configuration(load_credentials, expected_base_url=normalized_expected_base_url)
            duplicate = await _find_duplicate_correspondent(base_url=base_url, api_token=api_token, name=normalized_name, client_factory=client_factory)
            if duplicate is not None:
                return {"ok": True, "source": source, **duplicate, "created": False, "duplicate": True, "dry_run": is_dry_run, "verified": True}
            if is_dry_run:
                return {"ok": True, "source": source, "name": normalized_name, "created": False, "duplicate": False, "dry_run": True, "would_create": True, "verified": False}
        except PaperlessError as error:
            return _error_payload(error, source=source)
        except Exception:
            return _error_payload(PaperlessError("paperless_request_failed"), source=source)
        try:
            status, receipt = await _mutation_request(method="POST", base_url=base_url, api_token=api_token, path="/api/correspondents/", json_body={"name": normalized_name}, client_factory=client_factory)
            if 400 <= status < 500:
                return _error_payload(PaperlessError("paperless_create_correspondent_rejected"), source=source)
            if status != 201:
                return _error_payload(PaperlessError("paperless_create_correspondent_outcome_uncertain"), source=source)
            created = _correspondent(receipt, expected_name=normalized_name)
            persisted = await _get_json(base_url=base_url, api_token=api_token, path=f"/api/correspondents/{created['id']}/", params=None, client_factory=client_factory)
            verified = _correspondent(persisted, expected_id=created["id"], expected_name=normalized_name)
        except Exception:
            return _error_payload(PaperlessError("paperless_create_correspondent_outcome_uncertain"), source=source)
        return {"ok": True, "source": source, **verified, "created": True, "duplicate": False, "dry_run": False, "verified": True}
