from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "sync-paperless-delete-permissions.py"
SPEC = importlib.util.spec_from_file_location("delete_permission_sync", SCRIPT)
assert SPEC and SPEC.loader
sync = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sync)


def document(**values: object) -> dict[str, object]:
    result: dict[str, object] = {
        "active": True,
        "trashed": False,
        "foreign_owned": True,
        "effective_view": True,
        "effective_change": True,
        "effective_delete": False,
        "direct_delete_row": None,
    }
    result.update(values)
    return result


class DeletePermissionSyncPlannerTest(unittest.TestCase):
    permission_id = 42

    def plan(self, documents: dict[int, dict[str, object]], managed: list[dict[str, object]] = []) -> dict[str, list[object]]:
        return sync.plan_permission_sync(documents, managed, permission_id=self.permission_id)

    def test_eligible_foreign_document_gets_a_new_direct_grant(self) -> None:
        plan = self.plan({101: document()})
        self.assertEqual(plan, {"grant_document_ids": [101], "revoke": [], "prune": []})

    def test_existing_manual_or_group_delete_is_preserved(self) -> None:
        manual = self.plan({101: document(effective_delete=True, direct_delete_row=700)})
        group = self.plan({102: document(effective_delete=True, direct_delete_row=None)})
        self.assertEqual(manual, {"grant_document_ids": [], "revoke": [], "prune": []})
        self.assertEqual(group, {"grant_document_ids": [], "revoke": [], "prune": []})

    def test_only_managed_grant_is_revoked_when_view_or_change_drops(self) -> None:
        managed = [{"document_id": 101, "object_permission_id": 700, "permission_id": self.permission_id}]
        plan = self.plan({101: document(effective_change=False, effective_delete=True, direct_delete_row=700)}, managed)
        self.assertEqual(plan["grant_document_ids"], [])
        self.assertEqual(plan["revoke"], [{"document_id": 101, "object_permission_id": 700}])
        self.assertEqual(plan["prune"], [])

    def test_trashed_managed_grant_stays_for_a_possible_restore(self) -> None:
        managed = [{"document_id": 101, "object_permission_id": 700, "permission_id": self.permission_id}]
        plan = self.plan(
            {101: document(active=False, trashed=True, effective_view=False, effective_change=False, effective_delete=True, direct_delete_row=700)},
            managed,
        )
        self.assertEqual(plan, {"grant_document_ids": [], "revoke": [], "prune": []})

    def test_gone_document_is_planned_for_managed_grant_removal(self) -> None:
        managed = [{"document_id": 101, "object_permission_id": 700, "permission_id": self.permission_id}]
        self.assertEqual(
            self.plan({}, managed),
            {"grant_document_ids": [], "revoke": [], "prune": [{"document_id": 101, "object_permission_id": 700}]},
        )

    def test_pending_intent_stops_before_any_database_call(self) -> None:
        state = sync._empty_state("work")
        state["pending"] = {"grant_document_ids": [101]}
        with (
            patch.object(sync, "state_lock") as lock,
            patch.object(sync, "read_state", return_value=state),
            patch.object(sync, "run_database") as database,
            patch.object(sync.sys, "argv", [str(SCRIPT), "--source", "work", "--apply"]),
        ):
            lock.return_value.__enter__.return_value = None
            with self.assertRaisesRegex(RuntimeError, "pending intent"):
                sync.main()
            database.assert_not_called()

    def test_identity_and_ledger_mismatches_fail_closed(self) -> None:
        with self.assertRaises(ValueError):
            sync.validate_source_identity("work", {"id": 3, "username": "timkley"})
        with self.assertRaises(ValueError):
            self.plan(
                {101: document(effective_delete=True, direct_delete_row=701)},
                [{"document_id": 101, "object_permission_id": 700, "permission_id": self.permission_id}],
            )


if __name__ == "__main__":
    unittest.main()
