"""Confirmed fixed-source Paperless write tools."""

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
    "paperless.create_document_type": {
        "tool": "paperless.create_document_type", "enabled": True, "read_only": False,
        "scope": "creates one confirmed fixed-source document type by name after a case-insensitive duplicate lookup and API permission check",
        "writes": "POSTs only a name with confirm=true and reads the created document type back by ID",
    },
    "paperless.bulk_set_document_type": {
        "tool": "paperless.bulk_set_document_type", "enabled": True, "read_only": False,
        "scope": "sets one existing fixed-source document type on at most 25 confirmed source-qualified documents after per-document permission preflight",
        "writes": "PATCHes only document_type for each allowed document and reads every successful mutation back",
    },
}

_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_MAX_CORRESPONDENT_NAME_LENGTH = 128
_MAX_DOCUMENT_TYPE_NAME_LENGTH = 128
_MAX_BULK_DOCUMENTS = 25
_DUPLICATE_PAGE_SIZE = 25
_DELETE_ANNOTATIONS = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True)
_CREATE_ANNOTATIONS = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True)
_BULK_ANNOTATIONS = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True)


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


def _require_document_type_name(value: object) -> str:
    if not isinstance(value, str):
        raise PaperlessError("paperless_document_type_name_invalid")
    name = value.strip()
    if not name or len(name) > _MAX_DOCUMENT_TYPE_NAME_LENGTH:
        raise PaperlessError("paperless_document_type_name_invalid")
    return name


def _require_document_type_id(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise PaperlessError("paperless_document_type_id_invalid")
    return value


def _require_document_refs(value: object, source: str) -> list[tuple[str, int]]:
    if not isinstance(value, list) or not value or len(value) > _MAX_BULK_DOCUMENTS:
        raise PaperlessError("paperless_bulk_document_refs_invalid")
    refs = [(ref, _require_document_id(ref, source)) for ref in value]
    if len({document_id for _, document_id in refs}) != len(refs):
        raise PaperlessError("paperless_bulk_document_refs_invalid")
    return refs


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


def _document_type(
    payload: object, *, expected_id: int | None = None, expected_name: str | None = None,
) -> dict[str, object]:
    if not isinstance(payload, Mapping):
        raise PaperlessError("paperless_response_invalid")
    document_type_id, name = payload.get("id"), payload.get("name")
    if (
        isinstance(document_type_id, bool)
        or not isinstance(document_type_id, int)
        or document_type_id < 1
        or not isinstance(name, str)
        or not name
        or len(name) > _MAX_DOCUMENT_TYPE_NAME_LENGTH
    ):
        raise PaperlessError("paperless_response_invalid")
    if expected_id is not None and document_type_id != expected_id:
        raise PaperlessError("paperless_response_invalid")
    if expected_name is not None and name != expected_name:
        raise PaperlessError("paperless_response_invalid")
    return {"id": document_type_id, "name": name}


def _document_type_value(document: object) -> int | None:
    if not isinstance(document, Mapping):
        raise PaperlessError("paperless_response_invalid")
    value = document.get("document_type")
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise PaperlessError("paperless_response_invalid")
    return value


async def _bounded_json_request(
    *,
    method: str,
    base_url: str,
    api_token: str,
    path: str,
    client_factory: ClientFactory,
    params: Mapping[str, object] | None = None,
) -> tuple[int, object | None]:
    """Read one response without accepting redirects or unbounded bodies."""
    try:
        async with client_factory(timeout=httpx.Timeout(10.0, connect=5.0), follow_redirects=False) as client:
            async with client.stream(
                method,
                _endpoint(base_url, path),
                params=params,
                headers={"Authorization": f"Token {api_token}", "Accept": "application/json"},
            ) as response:
                if 300 <= response.status_code < 400:
                    raise PaperlessError("paperless_redirect_refused")
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
    body = b"".join(chunks)
    if not body:
        return response.status_code, None
    try:
        return response.status_code, json.loads(body)
    except (TypeError, UnicodeDecodeError, json.JSONDecodeError):
        raise PaperlessError("paperless_response_invalid") from None


async def _find_duplicate_document_type(
    *, base_url: str, api_token: str, name: str, client_factory: ClientFactory,
) -> dict[str, object] | None:
    status, payload = await _bounded_json_request(
        method="GET",
        base_url=base_url,
        api_token=api_token,
        path="/api/document_types/",
        params={"name__iexact": name, "page": 1, "page_size": _DUPLICATE_PAGE_SIZE},
        client_factory=client_factory,
    )
    if 400 <= status < 500:
        raise PaperlessError("paperless_document_type_duplicate_lookup_inaccessible")
    if status != 200 or not isinstance(payload, Mapping):
        raise PaperlessError("paperless_response_invalid")
    count, results, next_page = payload.get("count"), payload.get("results"), payload.get("next")
    if (
        isinstance(count, bool)
        or not isinstance(count, int)
        or count < 0
        or not isinstance(results, list)
        or len(results) > _DUPLICATE_PAGE_SIZE
        or count < len(results)
        or any(not isinstance(item, Mapping) for item in results)
    ):
        raise PaperlessError("paperless_response_invalid")
    if count != len(results) or next_page is not None:
        raise PaperlessError("paperless_document_type_duplicate_lookup_ambiguous")
    matches: list[dict[str, object]] = []
    for item in results:
        candidate_name = item.get("name")
        if not isinstance(candidate_name, str):
            raise PaperlessError("paperless_response_invalid")
        if candidate_name.casefold() == name.casefold():
            matches.append(_document_type(item))
    if count == 1 and len(matches) == 1:
        return matches[0]
    if count != 0:
        raise PaperlessError("paperless_document_type_duplicate_lookup_ambiguous")
    return None


async def _ensure_document_type_create_permission(
    *, base_url: str, api_token: str, client_factory: ClientFactory,
) -> None:
    status, payload = await _bounded_json_request(
        method="OPTIONS",
        base_url=base_url,
        api_token=api_token,
        path="/api/document_types/",
        client_factory=client_factory,
    )
    if 400 <= status < 500:
        raise PaperlessError("paperless_document_type_create_permission_denied")
    if status != 200 or not isinstance(payload, Mapping):
        raise PaperlessError("paperless_document_type_create_permission_denied")
    actions = payload.get("actions")
    if not isinstance(actions, Mapping) or "POST" not in actions:
        raise PaperlessError("paperless_document_type_create_permission_denied")


async def _get_target_document_type(
    *, base_url: str, api_token: str, document_type_id: int, client_factory: ClientFactory,
) -> dict[str, object]:
    status, payload = await _bounded_json_request(
        method="GET",
        base_url=base_url,
        api_token=api_token,
        path=f"/api/document_types/{document_type_id}/",
        client_factory=client_factory,
    )
    if status == 404:
        raise PaperlessError("paperless_document_type_not_found")
    if 400 <= status < 500:
        raise PaperlessError("paperless_document_type_target_rejected")
    if status != 200:
        raise PaperlessError("paperless_request_failed")
    return _document_type(payload, expected_id=document_type_id)


async def _preflight_document_type_change(
    *,
    base_url: str,
    api_token: str,
    source: str,
    document_ref: str,
    document_id: int,
    document_type_id: int,
    client_factory: ClientFactory,
) -> dict[str, object]:
    status, payload = await _bounded_json_request(
        method="GET",
        base_url=base_url,
        api_token=api_token,
        path=f"/api/documents/{document_id}/",
        client_factory=client_factory,
    )
    result: dict[str, object] = {
        "ref": document_ref,
        "planned_document_type": document_type_id,
        "allowed": False,
        "verified": False,
    }
    if 400 <= status < 500:
        return {**result, "outcome": "denied", "error": "paperless_document_permission_denied"}
    if status != 200:
        return {**result, "outcome": "denied", "error": "paperless_document_preflight_failed"}
    if not isinstance(payload, Mapping) or payload.get("user_can_change") is not True:
        return {**result, "outcome": "denied", "error": "paperless_document_permission_denied"}
    try:
        metadata = _document_metadata(
            payload, source=source, base_url=base_url, expected_document_id=document_id,
        )
        before = _document_type_value(payload)
    except PaperlessError:
        return {**result, "outcome": "denied", "error": "paperless_document_preflight_failed"}
    return {
        **result,
        "allowed": True,
        "outcome": "already_desired" if before == document_type_id else "ready",
        "before_document_type": before,
        "after_document_type": document_type_id,
        "document": metadata,
        "verified": True,
    }


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


def _bulk_result(
    *,
    source: str,
    document_type: Mapping[str, object],
    dry_run: bool,
    results: list[dict[str, object]],
) -> dict[str, object]:
    counts = {
        outcome: sum(result.get("outcome") == outcome for result in results)
        for outcome in ("ready", "would_update", "updated", "already_desired", "denied", "failed", "uncertain", "not_attempted")
    }
    retryable_refs = [
        result["ref"] for result in results
        if result.get("outcome") in {"denied", "failed"} and isinstance(result.get("ref"), str)
    ]
    uncertain_refs = [
        result["ref"] for result in results
        if result.get("outcome") == "uncertain" and isinstance(result.get("ref"), str)
    ]
    incomplete = any(counts[name] for name in ("denied", "failed", "uncertain", "not_attempted"))
    completed = any(counts[name] for name in ("updated", "already_desired"))
    status = "preview" if dry_run else ("complete" if not incomplete else ("partial" if completed else "failed"))
    return {
        "ok": not incomplete,
        "source": source,
        "document_type": dict(document_type),
        "dry_run": dry_run,
        "results": results,
        "counts": counts,
        "status": status,
        "partial": incomplete,
        "retryable_document_refs": retryable_refs,
        "remaining_document_refs": [
            result["ref"] for result in results
            if result.get("outcome") == "not_attempted" and isinstance(result.get("ref"), str)
        ],
        "readback_required_document_refs": uncertain_refs,
        "retry_requires_confirm": not dry_run,
    }


def register_paperless_write_tools(mcp: Any, *, source: str, load_credentials: CredentialsLoader, expected_base_url: str, client_factory: ClientFactory = httpx.AsyncClient) -> None:
    """Register fixed-source write tools; callers cannot supply URLs or methods."""
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

    @mcp.tool(name="paperless.create_document_type", description="Create one fixed-source document type by name after confirm=true, a case-insensitive duplicate lookup, and API permission preflight.", annotations=_CREATE_ANNOTATIONS)
    async def create_document_type(name: str, confirm: bool = False, dry_run: bool = False) -> dict[str, object]:
        """Create one name-only document type, or return its case-insensitive duplicate without writing."""
        try:
            normalized_name = _require_document_type_name(name)
            is_dry_run = _require_dry_run(dry_run)
            if not is_dry_run and confirm is not True:
                raise PaperlessError("paperless_create_document_type_confirmation_required")
            base_url, api_token = await _load_configuration(
                load_credentials, expected_base_url=normalized_expected_base_url,
            )
            duplicate = await _find_duplicate_document_type(
                base_url=base_url, api_token=api_token, name=normalized_name, client_factory=client_factory,
            )
            if duplicate is not None:
                return {
                    "ok": True, "source": source, **duplicate, "created": False,
                    "duplicate": True, "dry_run": is_dry_run, "verified": True,
                }
            await _ensure_document_type_create_permission(
                base_url=base_url, api_token=api_token, client_factory=client_factory,
            )
            if is_dry_run:
                return {
                    "ok": True, "source": source, "name": normalized_name, "created": False,
                    "duplicate": False, "dry_run": True, "would_create": True, "verified": False,
                }
        except PaperlessError as error:
            return _error_payload(error, source=source)
        except Exception:
            return _error_payload(PaperlessError("paperless_request_failed"), source=source)
        try:
            status, receipt = await _mutation_request(
                method="POST",
                base_url=base_url,
                api_token=api_token,
                path="/api/document_types/",
                json_body={"name": normalized_name},
                client_factory=client_factory,
            )
            if 400 <= status < 500:
                return _error_payload(PaperlessError("paperless_create_document_type_rejected"), source=source)
            if status != 201:
                return _error_payload(PaperlessError("paperless_create_document_type_outcome_uncertain"), source=source)
            created = _document_type(receipt, expected_name=normalized_name)
            persisted = await _get_json(
                base_url=base_url,
                api_token=api_token,
                path=f"/api/document_types/{created['id']}/",
                params=None,
                client_factory=client_factory,
            )
            verified = _document_type(
                persisted, expected_id=created["id"], expected_name=normalized_name,
            )
        except Exception:
            return _error_payload(
                PaperlessError("paperless_create_document_type_outcome_uncertain"), source=source,
            )
        return {
            "ok": True, "source": source, **verified, "created": True,
            "duplicate": False, "dry_run": False, "verified": True,
        }

    @mcp.tool(name="paperless.bulk_set_document_type", description="Preview with dry_run or, after confirm=true, set one existing fixed-source document type on up to 25 source-qualified documents. Returns per-document outcomes, verifies each successful write, and stops further writes after an uncertain result. Normal Paperless update workflows apply; obtain approval for their effects.", annotations=_BULK_ANNOTATIONS)
    async def bulk_set_document_type(
        document_refs: list[str],
        document_type_id: int,
        confirm: bool = False,
        dry_run: bool = False,
    ) -> dict[str, object]:
        """Set only document_type, stopping after any uncertain mutation outcome."""
        try:
            refs = _require_document_refs(document_refs, source)
            normalized_document_type_id = _require_document_type_id(document_type_id)
            is_dry_run = _require_dry_run(dry_run)
            if not is_dry_run and confirm is not True:
                raise PaperlessError("paperless_bulk_set_document_type_confirmation_required")
            base_url, api_token = await _load_configuration(
                load_credentials, expected_base_url=normalized_expected_base_url,
            )
            document_type = await _get_target_document_type(
                base_url=base_url,
                api_token=api_token,
                document_type_id=normalized_document_type_id,
                client_factory=client_factory,
            )
            results = [
                await _preflight_document_type_change(
                    base_url=base_url,
                    api_token=api_token,
                    source=source,
                    document_ref=document_ref,
                    document_id=document_id,
                    document_type_id=normalized_document_type_id,
                    client_factory=client_factory,
                )
                for document_ref, document_id in refs
            ]
            if is_dry_run:
                for result in results:
                    if result["outcome"] == "ready":
                        result["outcome"] = "would_update"
                return _bulk_result(
                    source=source, document_type=document_type, dry_run=True, results=results,
                )
        except PaperlessError as error:
            return _error_payload(error, source=source)
        except Exception:
            return _error_payload(PaperlessError("paperless_request_failed"), source=source)

        stop = False
        for result, (_, document_id) in zip(results, refs, strict=True):
            if result["outcome"] != "ready":
                continue
            if stop:
                result["outcome"] = "not_attempted"
                result["error"] = "paperless_prior_outcome_uncertain"
                continue
            try:
                status, _ = await _mutation_request(
                    method="PATCH",
                    base_url=base_url,
                    api_token=api_token,
                    path=f"/api/documents/{document_id}/",
                    json_body={"document_type": normalized_document_type_id},
                    client_factory=client_factory,
                )
                if 400 <= status < 500:
                    result["outcome"] = "failed"
                    result["error"] = "paperless_bulk_set_document_type_rejected"
                    continue
                if status not in {200, 204}:
                    raise PaperlessError("paperless_bulk_set_document_type_outcome_uncertain")
                persisted = await _get_json(
                    base_url=base_url,
                    api_token=api_token,
                    path=f"/api/documents/{document_id}/",
                    params=None,
                    client_factory=client_factory,
                )
                _document_metadata(
                    persisted,
                    source=source,
                    base_url=base_url,
                    expected_document_id=document_id,
                )
                if _document_type_value(persisted) != normalized_document_type_id:
                    raise PaperlessError("paperless_bulk_set_document_type_outcome_uncertain")
            except Exception:
                result["outcome"] = "uncertain"
                result["error"] = "paperless_bulk_set_document_type_outcome_uncertain"
                stop = True
                continue
            result["outcome"] = "updated"
            result["verified"] = True
        return _bulk_result(
            source=source, document_type=document_type, dry_run=False, results=results,
        )
