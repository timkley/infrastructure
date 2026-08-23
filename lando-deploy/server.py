#!/usr/bin/env python3
import fcntl
import json
import os
import re
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen

import jwt
from jwt import PyJWKClient


CONFIG_PATH = Path(os.environ.get("LANDO_DEPLOY_CONFIG", "/home/admin/lando-deploy-webhook/config.json"))
DISCORD_WEBHOOK_ENV = "LANDO_DEPLOY_DISCORD_WEBHOOK_URL"
ISSUER = "https://token.actions.githubusercontent.com"
JWKS_URL = f"{ISSUER}/.well-known/jwks"
APP_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
SHA_PATTERN = re.compile(r"^[0-9a-fA-F]{40}$")
APP_ROOT = Path(os.environ.get("LANDO_DEPLOY_APP_ROOT", "/var/www"))
LOCK_ROOT = Path(os.environ.get("LANDO_DEPLOY_LOCK_DIR", "/tmp"))


class DeployError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def load_config():
    with CONFIG_PATH.open("r", encoding="utf-8") as config_file:
        return json.load(config_file)


def first_header(headers, name):
    value = headers.get(name)
    return value.strip() if value else ""


def verify_token(token, config):
    audience = config.get("audience", "lando-deploy")
    jwk_client = PyJWKClient(JWKS_URL)
    signing_key = jwk_client.get_signing_key_from_jwt(token)

    return jwt.decode(
        token,
        signing_key.key,
        algorithms=["RS256"],
        audience=audience,
        issuer=ISSUER,
    )


def normalize_list(value):
    if value is None:
        return []

    if isinstance(value, list):
        return value

    return [value]


def resolve_app(claims, request_body, config):
    repository = claims.get("repository")
    if not repository:
        raise DeployError(403, "Token is missing repository claim.")

    apps = config.get("apps", {})
    app_config = apps.get(repository)
    if not app_config:
        raise DeployError(403, f"Repository is not allowed: {repository}")

    allowed_refs = normalize_list(app_config.get("refs", "refs/heads/main"))
    token_ref = claims.get("ref")
    if allowed_refs and token_ref not in allowed_refs:
        raise DeployError(403, f"Ref is not allowed: {token_ref}")

    allowed_events = normalize_list(app_config.get("events", ["workflow_run", "workflow_dispatch"]))
    token_event = claims.get("event_name")
    if allowed_events and token_event not in allowed_events:
        raise DeployError(403, f"Event is not allowed: {token_event}")

    if request_body:
        requested_repository = request_body.get("repository")
        requested_ref = request_body.get("ref")
        requested_event = request_body.get("event")
        requested_event_name = request_body.get("event_name")
        requested_sha = request_body.get("sha")
        requested_workflow_ref = request_body.get("workflow_ref")

        if requested_event is not None and requested_event_name is not None and requested_event != requested_event_name:
            raise DeployError(400, "Payload event and event_name do not match.")

        requested_event = requested_event if requested_event is not None else requested_event_name
        missing_fields = [
            field
            for field, value in (
                ("repository", requested_repository),
                ("ref", requested_ref),
                ("event", requested_event),
                ("sha", requested_sha),
                ("workflow_ref", requested_workflow_ref),
            )
            if not isinstance(value, str) or not value.strip()
        ]
        if missing_fields:
            raise DeployError(400, f"Payload is missing required fields: {', '.join(missing_fields)}")

        if requested_repository != repository:
            raise DeployError(403, "Payload repository does not match the OIDC token.")
        if requested_ref != token_ref:
            raise DeployError(403, "Payload ref does not match the OIDC token.")
        if requested_event != token_event:
            raise DeployError(403, "Payload event does not match the OIDC token.")
        if not SHA_PATTERN.fullmatch(requested_sha):
            raise DeployError(400, "Payload SHA must be a full 40-character commit SHA.")

        token_sha = str(claims.get("sha") or "").lower()
        if not SHA_PATTERN.fullmatch(token_sha) or requested_sha.lower() != token_sha:
            raise DeployError(403, "Payload SHA does not match the OIDC token.")

        token_workflow_ref = str(claims.get("workflow_ref") or claims.get("job_workflow_ref") or "")
        if requested_workflow_ref != token_workflow_ref:
            raise DeployError(403, "Payload workflow_ref does not match the OIDC token.")

        allowed_workflow_refs = normalize_list(
            app_config.get("workflow_refs", app_config.get("workflow_ref"))
        )
        if not allowed_workflow_refs:
            raise DeployError(403, "No workflow_ref is configured for exact deployments.")
        if token_workflow_ref not in allowed_workflow_refs:
            raise DeployError(403, f"Workflow ref is not allowed: {token_workflow_ref}")

        deploy_sha = requested_sha.lower()
    else:
        # Keep existing `{}` callers working while their workflows migrate. The
        # token SHA is informational only; legacy scripts keep their old input
        # and checkout behavior until their workflows are migrated.
        deploy_sha = str(claims.get("sha") or "").lower()

    app_name = app_config.get("app") or repository.rsplit("/", 1)[-1]
    requested_app = request_body.get("app")
    if requested_app and requested_app != app_name:
        raise DeployError(403, "Requested app does not match repository mapping.")

    if not APP_NAME_PATTERN.match(app_name):
        raise DeployError(500, "Configured app name is invalid.")

    return repository, app_name, deploy_sha, bool(request_body)


def run_git(app_dir, *arguments):
    completed = subprocess.run(
        ["git", *arguments],
        cwd=str(app_dir),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=120,
        check=False,
    )
    output = completed.stdout or ""

    if completed.returncode != 0:
        raise DeployError(500, f"Git command failed: git {' '.join(arguments)}\n{output}")

    return output.strip()


def require_forward_revision(app_dir, deploy_sha):
    active_sha = run_git(app_dir, "rev-parse", "HEAD").lower()
    if active_sha == deploy_sha:
        return

    try:
        run_git(app_dir, "merge-base", "--is-ancestor", active_sha, deploy_sha)
    except DeployError as error:
        raise DeployError(
            409,
            f"Deployment commit is older than or divergent from the active commit: {deploy_sha}",
        ) from error


def prepare_app_revision(app_dir, deploy_sha):
    if not SHA_PATTERN.fullmatch(deploy_sha):
        raise DeployError(400, "Deployment SHA must be a full 40-character commit SHA.")

    for arguments in (("diff", "--quiet"), ("diff", "--cached", "--quiet")):
        completed = subprocess.run(
            ["git", *arguments],
            cwd=str(app_dir),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=120,
            check=False,
        )
        if completed.returncode != 0:
            raise DeployError(409, "Refusing to deploy with tracked working-tree changes.")

    run_git(app_dir, "fetch", "--prune", "--force", "origin", "refs/heads/main:refs/remotes/origin/main")

    try:
        run_git(app_dir, "cat-file", "-e", f"{deploy_sha}^{{commit}}")
    except DeployError as error:
        raise DeployError(409, f"Deployment commit is not available locally: {deploy_sha}") from error

    require_forward_revision(app_dir, deploy_sha)

    try:
        run_git(app_dir, "merge-base", "--is-ancestor", deploy_sha, "refs/remotes/origin/main")
    except DeployError as error:
        raise DeployError(409, f"Deployment commit is not in origin/main history: {deploy_sha}") from error

    run_git(app_dir, "checkout", "--detach", deploy_sha)
    active_sha = run_git(app_dir, "rev-parse", "HEAD").lower()
    if active_sha != deploy_sha:
        raise DeployError(500, f"Checked out an unexpected deployment commit: {active_sha}")


def run_deploy(app_name, deploy_sha, exact_revision):
    app_dir = APP_ROOT / app_name
    deploy_script = app_dir / "deploy.sh"
    if not (app_dir / ".git").is_dir():
        raise DeployError(404, f"App git directory does not exist: {app_name}")

    if not deploy_script.is_file() or not os.access(deploy_script, os.X_OK):
        raise DeployError(404, f"Deploy script is missing or not executable: {app_name}")

    LOCK_ROOT.mkdir(parents=True, exist_ok=True)
    lock_path = LOCK_ROOT / f"lando-deploy-{app_name}.lock"
    try:
        lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    except OSError as error:
        raise DeployError(500, f"Could not open deploy lock: {app_name}") from error

    with os.fdopen(lock_fd, "w", encoding="utf-8") as lock_file:
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise DeployError(409, f"Deploy already running: {app_name}")

        started_at = time.time()
        if exact_revision:
            prepare_app_revision(app_dir, deploy_sha)

        deploy_arguments = ["./deploy.sh", deploy_sha] if exact_revision else ["./deploy.sh"]
        completed = subprocess.run(
            deploy_arguments,
            cwd=str(app_dir),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=900,
            check=False,
        )

        duration = time.time() - started_at
        output = completed.stdout or ""
        try:
            active_sha = run_git(app_dir, "rev-parse", "HEAD").lower()
        except DeployError:
            active_sha = ""
        output = f"{output.rstrip()}\nactive_sha={active_sha}\n"

        if exact_revision and completed.returncode == 0 and active_sha != deploy_sha:
            output += f"Requested SHA is not active after deploy: {deploy_sha}\n"
            return 1, duration, output

        return completed.returncode, duration, output


def extract_active_sha(output):
    matches = re.findall(r"^active_sha=([0-9a-fA-F]{40})$", output, re.MULTILINE)

    return matches[-1].lower() if matches else ""


def notify_deploy_success(config, repository, app_name, deploy_sha, active_sha, claims, duration):
    webhook_url = os.environ.get(DISCORD_WEBHOOK_ENV, "").strip() or str(config.get("discord_webhook_url") or "").strip()
    if not webhook_url:
        return

    short_sha = (active_sha or deploy_sha)[:7] if (active_sha or deploy_sha) else "unknown"
    run_id = str(claims.get("run_id") or "")
    run_url = f"https://github.com/{repository}/actions/runs/{run_id}" if run_id else None

    payload = {
        "content": (
            f":white_check_mark: Deployment erfolgreich: `{app_name}`\n"
            f"Repo: `{repository}`\n"
            f"Commit: `{short_sha}`\n"
            f"Aktiv: `{active_sha or 'unbekannt'}`\n"
            f"Dauer: `{duration:.1f}s`"
        )
    }

    if run_url:
        payload["content"] += f"\nRun: {run_url}"

    request = Request(
        webhook_url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "User-Agent": "lando-deploy-webhook/1.0",
        },
        method="POST",
    )

    with urlopen(request, timeout=10) as response:
        response.read()


class Handler(BaseHTTPRequestHandler):
    server_version = "LandoDeployWebhook/1.0"

    def log_message(self, format, *args):
        sys.stderr.write("%s - - [%s] %s\n" % (self.address_string(), self.log_date_time_string(), format % args))

    def send_text(self, status, body):
        encoded_body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded_body)))
        self.end_headers()
        self.wfile.write(encoded_body)

    def do_GET(self):
        if urlparse(self.path).path == "/health":
            self.send_text(200, "ok\n")
            return

        self.send_text(404, "not found\n")

    def do_POST(self):
        if urlparse(self.path).path != "/deploy":
            self.send_text(404, "not found\n")
            return

        try:
            content_length = int(first_header(self.headers, "Content-Length") or "0")
            if content_length > 4096:
                raise DeployError(413, "Request body is too large.")

            raw_body = self.rfile.read(content_length) if content_length else b"{}"
            request_body = json.loads(raw_body.decode("utf-8") or "{}")
            if not isinstance(request_body, dict):
                raise DeployError(400, "Request body must be a JSON object.")

            authorization = first_header(self.headers, "Authorization")
            if not authorization.startswith("Bearer "):
                raise DeployError(401, "Missing bearer token.")

            config = load_config()
            claims = verify_token(authorization.removeprefix("Bearer ").strip(), config)
            repository, app_name, deploy_sha, exact_revision = resolve_app(claims, request_body, config)
            return_code, duration, output = run_deploy(app_name, deploy_sha, exact_revision)
            active_sha = extract_active_sha(output)

            if return_code == 0:
                try:
                    notify_deploy_success(config, repository, app_name, deploy_sha, active_sha, claims, duration)
                except Exception as error:
                    self.log_message("discord deploy notification failed: %s", repr(error))

            self.log_message(
                "deployment repository=%s app=%s mode=%s sha=%s active_sha=%s run_id=%s exit_code=%s",
                repository,
                app_name,
                "exact" if exact_revision else "legacy",
                deploy_sha,
                active_sha or "unknown",
                claims.get("run_id") or request_body.get("run_id") or "",
                return_code,
            )

            response = (
                f"repository={repository}\n"
                f"app={app_name}\n"
                f"mode={'exact' if exact_revision else 'legacy'}\n"
                f"sha={deploy_sha}\n"
                f"active_sha={active_sha}\n"
                f"run_id={claims.get('run_id') or request_body.get('run_id', '')}\n"
                f"duration={duration:.1f}s\n"
                f"exit_code={return_code}\n\n"
                f"{output}"
            )

            self.send_text(200 if return_code == 0 else 500, response)
        except json.JSONDecodeError:
            self.send_text(400, "Invalid JSON body.\n")
        except subprocess.TimeoutExpired as error:
            self.send_text(504, f"Deploy timed out.\n{error.stdout or ''}")
        except jwt.PyJWTError as error:
            self.send_text(401, f"Invalid GitHub OIDC token: {error}\n")
        except DeployError as error:
            self.send_text(error.status, f"{error.message}\n")
        except Exception as error:
            self.log_message("unexpected error: %s", repr(error))
            self.send_text(500, "Internal deploy webhook error.\n")


def main():
    config = load_config()
    host = config.get("host", "0.0.0.0")
    port = int(config.get("port", 8010))
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"Listening on {host}:{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
