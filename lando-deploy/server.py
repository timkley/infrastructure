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

import jwt
from jwt import PyJWKClient


CONFIG_PATH = Path(os.environ.get("LANDO_DEPLOY_CONFIG", "/home/admin/lando-deploy-webhook/config.json"))
ISSUER = "https://token.actions.githubusercontent.com"
JWKS_URL = f"{ISSUER}/.well-known/jwks"
APP_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")


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

    app_name = app_config.get("app") or repository.rsplit("/", 1)[-1]
    requested_app = request_body.get("app")
    if requested_app and requested_app != app_name:
        raise DeployError(403, "Requested app does not match repository mapping.")

    if not APP_NAME_PATTERN.match(app_name):
        raise DeployError(500, "Configured app name is invalid.")

    return repository, app_name


def run_deploy(app_name):
    app_dir = Path("/var/www") / app_name
    deploy_script = app_dir / "deploy.sh"
    if not (app_dir / ".git").is_dir():
        raise DeployError(404, f"App git directory does not exist: {app_name}")

    if not deploy_script.is_file() or not os.access(deploy_script, os.X_OK):
        raise DeployError(404, f"Deploy script is missing or not executable: {app_name}")

    lock_path = Path("/tmp") / f"lando-deploy-{app_name}.lock"
    with lock_path.open("w", encoding="utf-8") as lock_file:
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise DeployError(409, f"Deploy already running: {app_name}")

        started_at = time.time()
        completed = subprocess.run(
            ["./deploy.sh"],
            cwd=str(app_dir),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=900,
            check=False,
        )

    duration = time.time() - started_at
    output = completed.stdout or ""
    return completed.returncode, duration, output


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
            repository, app_name = resolve_app(claims, request_body, config)
            return_code, duration, output = run_deploy(app_name)

            response = (
                f"repository={repository}\n"
                f"app={app_name}\n"
                f"sha={claims.get('sha', '')}\n"
                f"run_id={claims.get('run_id', '')}\n"
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
