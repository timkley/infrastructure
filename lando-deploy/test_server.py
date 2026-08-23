#!/usr/bin/env python3
import fcntl
import importlib
import os
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch


try:
    server = importlib.import_module("server")
except ModuleNotFoundError as error:
    if error.name != "jwt":
        raise

    jwt_stub = types.ModuleType("jwt")
    jwt_stub.PyJWTError = Exception
    jwt_stub.PyJWKClient = object
    jwt_stub.decode = lambda *args, **kwargs: {}
    sys.modules["jwt"] = jwt_stub
    server = importlib.import_module("server")


REPOSITORY = "timkley/tim-kleyersburg.de"
REF = "refs/heads/main"
EVENT = "push"
SHA = "a" * 40
WORKFLOW_REF = "timkley/tim-kleyersburg.de/.github/workflows/ci.yml@refs/heads/main"


class ServerContractTest(unittest.TestCase):
    def setUp(self):
        self.config = {
            "apps": {
                REPOSITORY: {
                    "app": "tim-kleyersburg",
                    "refs": [REF],
                    "events": [EVENT, "workflow_dispatch"],
                    "workflow_refs": [WORKFLOW_REF],
                }
            }
        }
        self.claims = {
            "repository": REPOSITORY,
            "ref": REF,
            "event_name": EVENT,
            "sha": SHA,
            "workflow_ref": WORKFLOW_REF,
            "run_id": "123",
        }

    def test_accepts_payload_authorized_by_oidc_claims(self):
        result = server.resolve_app(
            self.claims,
            {
                "repository": REPOSITORY,
                "ref": REF,
                "event": EVENT,
                "sha": SHA,
                "workflow_ref": WORKFLOW_REF,
            },
            self.config,
        )

        self.assertEqual((REPOSITORY, "tim-kleyersburg", SHA, True), result)

    def test_rejects_payload_branch_mismatch(self):
        with self.assertRaisesRegex(server.DeployError, "Payload ref does not match"):
            server.resolve_app(
                self.claims,
                {
                    "repository": REPOSITORY,
                    "ref": "refs/heads/release",
                    "event": EVENT,
                    "sha": SHA,
                    "workflow_ref": WORKFLOW_REF,
                },
                self.config,
            )

    def test_rejects_payload_event_mismatch(self):
        with self.assertRaisesRegex(server.DeployError, "Payload event does not match"):
            server.resolve_app(
                self.claims,
                {
                    "repository": REPOSITORY,
                    "ref": REF,
                    "event": "workflow_dispatch",
                    "sha": SHA,
                    "workflow_ref": WORKFLOW_REF,
                },
                self.config,
            )

    def test_rejects_bad_payload_sha(self):
        with self.assertRaisesRegex(server.DeployError, "full 40-character"):
            server.resolve_app(
                self.claims,
                {
                    "repository": REPOSITORY,
                    "ref": REF,
                    "event": EVENT,
                    "sha": "not-a-sha",
                    "workflow_ref": WORKFLOW_REF,
                },
                self.config,
            )

    def test_rejects_payload_sha_that_differs_from_signed_oidc_sha(self):
        with self.assertRaisesRegex(server.DeployError, "does not match the OIDC token"):
            server.resolve_app(
                self.claims,
                {
                    "repository": REPOSITORY,
                    "ref": REF,
                    "event": EVENT,
                    "sha": "b" * 40,
                    "workflow_ref": WORKFLOW_REF,
                },
                self.config,
            )

    def test_rejects_a_workflow_ref_not_allowed_for_the_app(self):
        alternate_workflow_ref = "timkley/tim-kleyersburg.de/.github/workflows/other.yml@refs/heads/main"
        claims = {**self.claims, "workflow_ref": alternate_workflow_ref}

        with self.assertRaisesRegex(server.DeployError, "Workflow ref is not allowed"):
            server.resolve_app(
                claims,
                {
                    "repository": REPOSITORY,
                    "ref": REF,
                    "event": EVENT,
                    "sha": SHA,
                    "workflow_ref": alternate_workflow_ref,
                },
                self.config,
            )

    def test_legacy_empty_payload_keeps_oidc_sha_informational(self):
        result = server.resolve_app(self.claims, {}, self.config)

        self.assertEqual((REPOSITORY, "tim-kleyersburg", SHA, False), result)

        claims_without_sha = {key: value for key, value in self.claims.items() if key != "sha"}
        result_without_sha = server.resolve_app(claims_without_sha, {}, self.config)

        self.assertEqual((REPOSITORY, "tim-kleyersburg", "", False), result_without_sha)

    def test_run_deploy_passes_exact_sha_and_reports_active_sha(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            app_dir = Path(temporary_directory) / "tim-kleyersburg"
            app_dir.mkdir()
            self._init_git_repository(app_dir)
            deploy_sha = subprocess.check_output(
                ["git", "rev-parse", "HEAD"],
                cwd=app_dir,
                text=True,
            ).strip()
            script = app_dir / "deploy.sh"
            script.write_text(
                "#!/bin/sh\n"
                "printf 'active_sha=%s\\n' \"$1\"\n",
                encoding="utf-8",
            )
            script.chmod(0o700)
            server.APP_ROOT = Path(temporary_directory)
            server.LOCK_ROOT = Path(temporary_directory) / "locks"

            with patch.object(server, "prepare_app_revision"):
                return_code, _, output = server.run_deploy("tim-kleyersburg", deploy_sha, True)

            self.assertEqual(0, return_code)
            self.assertIn(f"active_sha={deploy_sha}", output)
            self.assertEqual(deploy_sha, server.extract_active_sha(output))

    def test_legacy_run_deploy_does_not_prepare_or_pass_a_sha(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            app_dir = Path(temporary_directory) / "tim-kleyersburg"
            app_dir.mkdir()
            (app_dir / ".git").mkdir()
            script = app_dir / "deploy.sh"
            script.write_text(
                "#!/bin/sh\n"
                "printf 'argument_count=%s\\n' \"$#\"\n",
                encoding="utf-8",
            )
            script.chmod(0o700)
            server.APP_ROOT = Path(temporary_directory)
            server.LOCK_ROOT = Path(temporary_directory) / "locks"

            with patch.object(server, "prepare_app_revision", side_effect=AssertionError("legacy prepare called")):
                return_code, _, output = server.run_deploy("tim-kleyersburg", SHA, False)

            self.assertEqual(0, return_code)
            self.assertIn("argument_count=0", output)

    def test_rejects_a_stale_or_divergent_revision(self):
        active_sha = "b" * 40
        with patch.object(
            server,
            "run_git",
            side_effect=[active_sha, server.DeployError(500, "not an ancestor")],
        ):
            with self.assertRaisesRegex(server.DeployError, "older than or divergent"):
                server.require_forward_revision(None, SHA)

    def test_allows_a_forward_revision(self):
        active_sha = "b" * 40
        with patch.object(server, "run_git", side_effect=[active_sha, ""]):
            server.require_forward_revision(None, SHA)

    def test_run_deploy_rejects_a_second_deploy_for_the_same_app(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            app_dir = Path(temporary_directory) / "tim-kleyersburg"
            app_dir.mkdir()
            (app_dir / ".git").mkdir()
            script = app_dir / "deploy.sh"
            script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            script.chmod(0o700)
            server.APP_ROOT = Path(temporary_directory)
            server.LOCK_ROOT = Path(temporary_directory) / "locks"
            server.LOCK_ROOT.mkdir()
            lock_path = server.LOCK_ROOT / "lando-deploy-tim-kleyersburg.lock"

            with lock_path.open("w", encoding="utf-8") as lock_file:
                fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with self.assertRaisesRegex(server.DeployError, "Deploy already running"):
                    server.run_deploy("tim-kleyersburg", SHA, False)

    def _init_git_repository(self, app_dir):
        commands = [
            ["git", "init", "--quiet"],
            ["git", "config", "user.email", "test@example.com"],
            ["git", "config", "user.name", "Test"],
        ]
        for command in commands:
            subprocess.run(command, cwd=app_dir, check=True)

        marker = app_dir / "marker.txt"
        marker.write_text("test\n", encoding="utf-8")
        subprocess.run(["git", "add", "marker.txt"], cwd=app_dir, check=True)
        subprocess.run(["git", "commit", "--quiet", "-m", "test"], cwd=app_dir, check=True)


if __name__ == "__main__":
    unittest.main()
