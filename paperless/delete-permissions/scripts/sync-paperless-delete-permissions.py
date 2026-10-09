#!/usr/bin/env python3
"""Maintain only this service's bounded Paperless object delete grants.

The command is a preview unless --apply is present.  It never reads API
credentials and never adds global permissions.  State records only direct
``delete_document`` rows that this command created, so it can later withdraw
only those rows when an active foreign-owned document loses object-level view
or change access.  Trashed documents stay in the ledger for a possible restore.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import inspect
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from collections.abc import Iterator, Mapping, Sequence
from typing import Any


STATE_ROOT = Path("/var/lib/heisenberg-paperless-delete-permissions")
STATE_VERSION = 1
SOURCES: dict[str, dict[str, Any]] = {
    "work": {
        "container": "paperless-webserver-1",
        "user_id": 4,
        "username": "heisenberg-work-mcp",
        "group_count": 0,
        "require_unusable_password": True,
    },
    "private": {
        "container": "paperless-app-1",
        "user_id": 3,
        "username": "timkley",
        "group_count": 1,
        "require_unusable_password": False,
    },
}


def validate_source_identity(source: str, identity: Mapping[str, object]) -> None:
    """Reject a state file or database response for another fixed service user."""
    expected = SOURCES[source]
    if identity != {"id": expected["user_id"], "username": expected["username"]}:
        raise ValueError("source identity does not match the fixed Paperless account")


def plan_permission_sync(
    documents: Mapping[int, Mapping[str, object]],
    managed: Sequence[Mapping[str, object]],
    *,
    permission_id: int,
) -> dict[str, list[object]]:
    """Plan a sync from effective permissions; this pure function has no I/O.

    A document value has ``active``, ``trashed``, ``foreign_owned``,
    ``effective_view``, ``effective_change``, ``effective_delete``, and an
    optional ``direct_delete_row``.  The latter is the guardian row primary key.
    """
    managed_by_document: dict[int, Mapping[str, object]] = {}
    managed_rows: set[int] = set()
    for entry in managed:
        document_id = entry.get("document_id")
        row_id = entry.get("object_permission_id")
        if (
            not isinstance(document_id, int)
            or isinstance(document_id, bool)
            or document_id <= 0
            or not isinstance(row_id, int)
            or isinstance(row_id, bool)
            or row_id <= 0
            or entry.get("permission_id") != permission_id
            or document_id in managed_by_document
            or row_id in managed_rows
        ):
            raise ValueError("managed state is inconsistent")
        managed_by_document[document_id] = entry
        managed_rows.add(row_id)

    additions: list[int] = []
    revocations: list[dict[str, int]] = []
    pruned: list[dict[str, int]] = []
    for document_id, entry in managed_by_document.items():
        document = documents.get(document_id)
        if document is None:
            pruned.append({"document_id": document_id, "object_permission_id": int(entry["object_permission_id"])})
            continue
        if document.get("direct_delete_row") != entry["object_permission_id"]:
            raise ValueError("managed guardian row does not match state")
        if document.get("active") and (
            not document.get("foreign_owned")
            or not document.get("effective_view")
            or not document.get("effective_change")
        ):
            revocations.append({"document_id": document_id, "object_permission_id": int(entry["object_permission_id"])})

    for document_id, document in documents.items():
        if not isinstance(document_id, int) or isinstance(document_id, bool) or document_id <= 0:
            raise ValueError("document identifier is invalid")
        if (
            document.get("active")
            and document.get("foreign_owned")
            and document.get("effective_view")
            and document.get("effective_change")
            and not document.get("effective_delete")
        ):
            additions.append(document_id)
    return {
        "grant_document_ids": sorted(additions),
        "revoke": sorted(revocations, key=lambda item: item["document_id"]),
        "prune": sorted(pruned, key=lambda item: item["document_id"]),
    }


def _empty_state(source: str) -> dict[str, object]:
    expected = SOURCES[source]
    return {
        "version": STATE_VERSION,
        "source": source,
        "identity": {"id": expected["user_id"], "username": expected["username"]},
        "managed": [],
    }


def _validate_state(source: str, state: Mapping[str, object]) -> dict[str, object]:
    if state.get("version") != STATE_VERSION or state.get("source") != source:
        raise ValueError("state file has another source or version")
    identity = state.get("identity")
    managed = state.get("managed")
    if not isinstance(identity, Mapping) or not isinstance(managed, list):
        raise ValueError("state file is malformed")
    validate_source_identity(source, identity)
    if "pending" in state and not isinstance(state["pending"], Mapping):
        raise ValueError("state intent is malformed")
    return dict(state)


def _state_path(source: str) -> Path:
    return STATE_ROOT / f"{source}.json"


@contextlib.contextmanager
def state_lock(source: str) -> Iterator[None]:
    STATE_ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_path = STATE_ROOT / f"{source}.lock"
    with lock_path.open("a", encoding="utf-8") as handle:
        os.chmod(lock_path, 0o600)
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield


def read_state(source: str) -> dict[str, object]:
    path = _state_path(source)
    if not path.exists():
        return _empty_state(source)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("state file cannot be read") from error
    if not isinstance(raw, Mapping):
        raise ValueError("state file is malformed")
    return _validate_state(source, raw)


def write_state(source: str, state: Mapping[str, object]) -> None:
    checked = _validate_state(source, state)
    STATE_ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{source}.", suffix=".tmp", dir=STATE_ROOT)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(checked, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, _state_path(source))
        directory = os.open(STATE_ROOT, os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temporary.exists():
            temporary.unlink()


PROGRAM = r'''
import json, os
request = json.loads(REQUEST_JSON)
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'paperless.settings')
import django
django.setup()
from django.contrib.auth.models import Permission, User
from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from documents.models import Document
from guardian.models import GroupObjectPermission, UserObjectPermission
from guardian.shortcuts import get_objects_for_user
from collections.abc import Mapping, Sequence

PURE_PLANNER_SOURCE

def fail(message):
    raise RuntimeError(message)

def integer(value):
    return isinstance(value, int) and not isinstance(value, bool) and value > 0

def managed_entries(entries, permission_id):
    if not isinstance(entries, list): fail('managed state is malformed')
    result, documents, rows = [], set(), set()
    for entry in entries:
        if not isinstance(entry, dict): fail('managed entry is malformed')
        document_id, row_id = entry.get('document_id'), entry.get('object_permission_id')
        if (not integer(document_id) or not integer(row_id) or entry.get('permission_id') != permission_id
                or document_id in documents or row_id in rows):
            fail('managed entry is inconsistent')
        documents.add(document_id); rows.add(row_id)
        result.append({'document_id': document_id, 'object_permission_id': row_id, 'permission_id': permission_id})
    return result

def rows_for(user, content_type, permission):
    rows = list(UserObjectPermission.objects.select_for_update().filter(
        user=user, content_type=content_type, permission=permission))
    by_document = {}
    for row in rows:
        document_id = int(row.object_pk)
        if document_id in by_document: fail('duplicate direct delete grant')
        by_document[document_id] = row
    return by_document

def plan(user, content_type, delete_permission, managed):
    direct = rows_for(user, content_type, delete_permission)
    active_foreign = Document.objects.select_for_update().filter(owner__isnull=False).exclude(owner=user)
    foreign_ids = set(active_foreign.values_list('pk', flat=True))
    eligible = get_objects_for_user(
        user, ['documents.view_document', 'documents.change_document'],
        klass=active_foreign, any_perm=False, accept_global_perms=False,
        with_superuser=False,
    )
    eligible_ids = set(eligible.values_list('pk', flat=True))
    effective_delete = get_objects_for_user(
        user, ['documents.delete_document'], klass=active_foreign.filter(pk__in=eligible_ids),
        any_perm=False, accept_global_perms=False, with_superuser=False,
    )
    effective_delete_ids = set(effective_delete.values_list('pk', flat=True))
    tracked_ids = {entry['document_id'] for entry in managed}
    active_ids = set(Document.objects.filter(pk__in=tracked_ids | foreign_ids).values_list('pk', flat=True))
    trashed_ids = set(Document.deleted_objects.filter(pk__in=tracked_ids).values_list('pk', flat=True))
    documents = {}
    for document_id in active_ids:
        documents[document_id] = {
            'active': True, 'trashed': False, 'foreign_owned': document_id in foreign_ids,
            'effective_view': document_id in eligible_ids, 'effective_change': document_id in eligible_ids,
            'effective_delete': document_id in effective_delete_ids,
            'direct_delete_row': direct[document_id].pk if document_id in direct else None,
        }
    for document_id in trashed_ids:
        documents[document_id] = {
            'active': False, 'trashed': True, 'foreign_owned': False,
            'effective_view': False, 'effective_change': False, 'effective_delete': False,
            'direct_delete_row': direct[document_id].pk if document_id in direct else None,
        }
    planned = plan_permission_sync(documents, managed, permission_id=delete_permission.pk)
    return {
        'eligible_ids': sorted(eligible_ids),
        'effective_delete_ids': sorted(effective_delete_ids),
        **planned,
        'direct': direct,
        'active_document_count': Document.objects.count(),
        'trashed_document_count': Document.deleted_objects.count(),
    }

def preserve_before(user):
    return {
        'owners': set(Document.objects.values_list('pk', 'owner_id')) | set(Document.deleted_objects.values_list('pk', 'owner_id')),
        'contents': set(Document.objects.values_list('pk', 'checksum')) | set(Document.deleted_objects.values_list('pk', 'checksum')),
        'other_users': set(UserObjectPermission.objects.exclude(user=user).values_list('pk', 'user_id', 'permission_id', 'object_pk')),
        'groups': set(GroupObjectPermission.objects.values_list('pk', 'group_id', 'permission_id', 'object_pk')),
        'active_count': Document.objects.count(),
        'trash_count': Document.deleted_objects.count(),
    }

def preserve_after(user, before):
    if before['owners'] != (set(Document.objects.values_list('pk', 'owner_id')) | set(Document.deleted_objects.values_list('pk', 'owner_id'))):
        fail('document owners changed during permission sync')
    if before['contents'] != (set(Document.objects.values_list('pk', 'checksum')) | set(Document.deleted_objects.values_list('pk', 'checksum'))):
        fail('document contents changed during permission sync')
    if before['other_users'] != set(UserObjectPermission.objects.exclude(user=user).values_list('pk', 'user_id', 'permission_id', 'object_pk')):
        fail('another user object permission changed during permission sync')
    if before['groups'] != set(GroupObjectPermission.objects.values_list('pk', 'group_id', 'permission_id', 'object_pk')):
        fail('group object permissions changed during permission sync')
    if before['active_count'] != Document.objects.count() or before['trash_count'] != Document.deleted_objects.count():
        fail('document lifecycle changed during permission sync')

with transaction.atomic():
    config = request['config']
    user = User.objects.select_for_update().get(pk=config['user_id'], username=config['username'])
    if not user.is_active or user.is_staff or user.is_superuser:
        fail('fixed Paperless account is not a restricted active account')
    if user.groups.count() != config['group_count']:
        fail('fixed Paperless account group membership changed')
    if config['require_unusable_password'] and user.has_usable_password():
        fail('fixed Paperless account unexpectedly has a usable password')
    document_type = ContentType.objects.get_for_model(Document)
    required = ['documents.view_document', 'documents.change_document', 'documents.delete_document']
    if not user.has_perms(required):
        fail('required effective global document permissions are missing')
    delete_permission = Permission.objects.get(content_type=document_type, codename='delete_document')
    identity = {'id': user.pk, 'username': user.username}
    if identity != request['identity']:
        fail('fixed Paperless account identity changed')
    managed = managed_entries(request['managed'], delete_permission.pk)

    current = plan(user, document_type, delete_permission, managed)
    report = {
            'source': request['source'], 'identity': identity, 'dry_run': request['mode'] == 'plan',
            'eligible_documents': len(current['eligible_ids']),
            'effective_delete_documents': len(current['effective_delete_ids']),
            'new_document_delete_grants': len(current['grant_document_ids']),
            'managed_document_delete_revocations': len(current['revoke']),
            'physically_gone_managed_grants_pruned': len(current['prune']),
            'global_permissions_unchanged': True,
            'owners_other_grants_and_document_contents_preserved': True,
            'credentials_untouched': True,
    }
    if request['mode'] == 'plan':
        report.update(grant_document_ids=current['grant_document_ids'], revoke=current['revoke'], prune=current['prune'], permission_id=delete_permission.pk)
        print(json.dumps(report))
    else:
        pending = request['pending']
        if pending['permission_id'] != delete_permission.pk:
            fail('pending permission changed before apply')
        if pending['grant_document_ids'] != current['grant_document_ids']:
            fail('pending grants changed before apply')
        if pending['revoke'] != current['revoke']:
            fail('pending revocations changed before apply')
        if pending['prune'] != current['prune']:
            fail('pending prunes changed before apply')
        before = preserve_before(user)
        grant_ids = current['grant_document_ids']
        documents = list(Document.objects.filter(pk__in=grant_ids).order_by('pk'))
        if [document.pk for document in documents] != grant_ids:
            fail('planned document disappeared before its grant could be created')
        new_rows = []
        for document in documents:
            row = UserObjectPermission.objects.create(
                user=user,
                content_type=document_type,
                permission=delete_permission,
                object_pk=str(document.pk),
            )
            new_rows.append({'document_id': document.pk, 'object_permission_id': row.pk, 'permission_id': delete_permission.pk})
        pruned_row_ids = {entry['object_permission_id'] for entry in current['prune']}
        for entry in [*current['revoke'], *current['prune']]:
            deleted, _ = UserObjectPermission.objects.filter(
                pk=entry['object_permission_id'],
                user=user,
                content_type=document_type,
                permission=delete_permission,
                object_pk=str(entry['document_id']),
            ).delete()
            if deleted != 1 and not (deleted == 0 and entry['object_permission_id'] in pruned_row_ids):
                fail('managed guardian row changed before it could be removed')
        preserve_after(user, before)
        report.update(dry_run=False, new_rows=new_rows)
        print(json.dumps(report))
'''


def run_database(source: str, payload: Mapping[str, object]) -> dict[str, object]:
    config = SOURCES[source]
    request = {
        "source": source,
        "config": config,
        "identity": {"id": config["user_id"], "username": config["username"]},
        **payload,
    }
    command = [] if os.geteuid() == 0 else ["sudo", "-n"]
    command.extend([
        "docker", "exec", "-i", "-w", "/usr/src/paperless/src", config["container"], "python", "-",
    ])
    program = PROGRAM.replace("PURE_PLANNER_SOURCE", inspect.getsource(plan_permission_sync)).replace("REQUEST_JSON", repr(json.dumps(request, separators=(",", ":"))))
    result = subprocess.run(command, input=program, text=True, capture_output=True)
    if result.returncode:
        raise RuntimeError("Paperless permission sync failed; inspect any pending intent before retrying")
    try:
        response = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError("Paperless permission sync returned invalid JSON") from error
    if not isinstance(response, dict):
        raise RuntimeError("Paperless permission sync returned an invalid result")
    return response


def _pending_from_plan(plan: Mapping[str, object]) -> dict[str, object]:
    return {
        "permission_id": plan["permission_id"],
        "grant_document_ids": plan["grant_document_ids"],
        "revoke": plan["revoke"],
        "prune": plan["prune"],
    }


def _final_state(state: Mapping[str, object], pending: Mapping[str, object], new_rows: Sequence[object]) -> dict[str, object]:
    removed_rows = {entry["object_permission_id"] for entry in pending["revoke"]} | {entry["object_permission_id"] for entry in pending["prune"]}
    retained = [entry for entry in state["managed"] if entry["object_permission_id"] not in removed_rows]
    return {
        "version": state["version"], "source": state["source"], "identity": state["identity"],
        "managed": sorted([*retained, *new_rows], key=lambda entry: entry["document_id"]),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, choices=tuple(SOURCES), help="Fixed Paperless deployment to check.")
    parser.add_argument("--apply", action="store_true", help="Persist only the bounded direct object grants described by the preview.")
    args = parser.parse_args()
    with state_lock(args.source):
        state = read_state(args.source)
        if "pending" in state:
            raise RuntimeError("state has a pending intent; inspect the database and ledger before clearing it manually")

        plan = run_database(args.source, {"mode": "plan", "managed": state["managed"]})
        if not args.apply:
            print(json.dumps(plan, indent=2))
            return 0
        pending = _pending_from_plan(plan)
        if not pending["grant_document_ids"] and not pending["revoke"] and not pending["prune"]:
            print(json.dumps(plan, indent=2))
            return 0
        intent = dict(state)
        intent["pending"] = pending
        write_state(args.source, intent)
        applied = run_database(args.source, {"mode": "apply", "managed": state["managed"], "pending": pending})
        write_state(args.source, _final_state(state, pending, applied["new_rows"]))
        print(json.dumps(applied, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, ValueError, OSError) as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(1)
