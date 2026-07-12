#!/usr/bin/env python3
"""Small, dependency-free web UI for switching the active Minecraft world."""

from __future__ import annotations

import fcntl
import html
import ipaddress
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse


ROOT = Path(__file__).resolve().parent
ENV_FILE = ROOT / ".env"
DATA_DIR = ROOT / "data"
PORT = 8090
WARNING_SECONDS = 15
START_TIMEOUT = 180
LOCK_FILE = Path("/tmp/minecraft-world-switcher.lock")
SERVICE_FILE = Path("/etc/systemd/system/minecraft-world-switcher.service")
SERVICE_USER = "admin"
WORLD_NAME_PATTERN = re.compile(r"^[^\x00\r\n/]+$")
PLAYER_COUNT_PATTERN = re.compile(r"There are (\d+) of a max of (\d+) players online", re.IGNORECASE)


class SwitchError(RuntimeError):
    pass


@dataclass(frozen=True)
class PlayerStatus:
    online: int | None
    maximum: int | None
    server_running: bool


@dataclass(frozen=True)
class OperationState:
    running: bool = False
    target: str = ""
    message: str = "Bereit"
    error: bool = False


class StateStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state = OperationState()

    def get(self) -> OperationState:
        with self._lock:
            return self._state

    def set(self, state: OperationState) -> None:
        with self._lock:
            self._state = state

    def begin(self, target: str) -> bool:
        with self._lock:
            if self._state.running:
                return False
            self._state = OperationState(True, target, "Weltwechsel wird vorbereitet …")
            return True


def run_command(command: list[str], *, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    # Compose must read these values from the current .env file. Inherited values
    # have higher precedence and could otherwise keep the old world active.
    environment.pop("WORLD", None)
    environment.pop("RCON_PASSWORD", None)
    return subprocess.run(
        command,
        cwd=ROOT,
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
        check=False,
    )


def compose_command(*arguments: str) -> list[str]:
    return ["docker", "compose", "--project-directory", str(ROOT), *arguments]


def parse_env_value(raw_value: str) -> str:
    value = raw_value.strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value[1:-1]
    if len(value) >= 2 and value[0] == value[-1] == "'":
        return value[1:-1].replace("\\'", "'")
    return value.split(" #", 1)[0].strip()


def read_active_world(env_file: Path | None = None) -> str:
    env_file = env_file or ENV_FILE
    if not env_file.is_file():
        return ""
    for line in env_file.read_text(encoding="utf-8").splitlines():
        if line.startswith("WORLD="):
            return parse_env_value(line.partition("=")[2])
    return ""


def format_env_value(value: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9._-]+", value):
        return value
    return json.dumps(value, ensure_ascii=False)


def write_active_world(world: str, env_file: Path | None = None) -> None:
    env_file = env_file or ENV_FILE
    if not WORLD_NAME_PATTERN.fullmatch(world):
        raise SwitchError("Der Weltname enthält ungültige Zeichen.")

    existing = env_file.read_text(encoding="utf-8") if env_file.exists() else ""
    lines = existing.splitlines()
    replacement = f"WORLD={format_env_value(world)}"
    found = False
    for index, line in enumerate(lines):
        if line.startswith("WORLD="):
            lines[index] = replacement
            found = True
            break
    if not found:
        lines.insert(0, replacement)

    content = "\n".join(lines) + "\n"
    env_file.parent.mkdir(parents=True, exist_ok=True)
    mode = env_file.stat().st_mode & 0o777 if env_file.exists() else 0o600
    with tempfile.NamedTemporaryFile("w", dir=env_file.parent, encoding="utf-8", delete=False) as temporary:
        temporary.write(content)
        temporary.flush()
        os.fsync(temporary.fileno())
        temporary_path = Path(temporary.name)
    os.chmod(temporary_path, mode)
    os.replace(temporary_path, env_file)


def discover_worlds(data_dir: Path | None = None) -> list[str]:
    data_dir = data_dir or DATA_DIR
    if not data_dir.is_dir():
        return []
    return sorted(
        (
            entry.name
            for entry in data_dir.iterdir()
            if entry.is_dir()
            and WORLD_NAME_PATTERN.fullmatch(entry.name)
            and (entry / "level.dat").is_file()
        ),
        key=str.casefold,
    )


def display_name(world: str) -> str:
    return re.sub(r"[_-]+", " ", world).strip().title() or world


def discover_tailscale_ip() -> str:
    result = run_command(["tailscale", "ip", "-4"], timeout=10)
    if result.returncode != 0 or not result.stdout.strip():
        raise SwitchError("Tailscale-IP konnte nicht ermittelt werden.")
    host = result.stdout.splitlines()[0].strip()
    validate_bind_host(host)
    return host


def validate_bind_host(host: str) -> None:
    try:
        address = ipaddress.ip_address(host)
    except ValueError as error:
        raise SwitchError("WORLD_SWITCHER_HOST muss eine IP-Adresse sein.") from error
    tailscale_range = ipaddress.ip_network("100.64.0.0/10")
    if not (address.is_loopback or address in tailscale_range):
        raise SwitchError("Der World Switcher darf nur an Loopback oder eine Tailscale-IP binden.")


def install_service() -> None:
    if sys.platform != "linux" or not hasattr(os, "geteuid"):
        raise SwitchError("Die automatische Installation wird nur auf Linux unterstützt.")
    if os.geteuid() != 0:
        raise SwitchError("Installation benötigt Root-Rechte: sudo ./world_switcher.py install")

    script = Path(__file__).resolve()
    python = Path(sys.executable).resolve()
    unit = f"""[Unit]
Description=Minecraft World Switcher
After=docker.service tailscale-online.target
Wants=docker.service tailscale-online.target

[Service]
Type=simple
User={SERVICE_USER}
Group={SERVICE_USER}
WorkingDirectory={ROOT}
ExecStartPre=/usr/bin/tailscale wait --timeout=60s
ExecStart={python} {script}
Restart=on-failure
RestartSec=3
Environment=PYTHONUNBUFFERED=1
UMask=0077
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
"""
    SERVICE_FILE.write_text(unit, encoding="utf-8")
    os.chmod(SERVICE_FILE, 0o644)
    subprocess.run(["systemctl", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "enable", "--now", SERVICE_FILE.name], check=True)
    print(f"Installiert und gestartet: {SERVICE_FILE}")


def uninstall_service() -> None:
    if sys.platform != "linux" or not hasattr(os, "geteuid"):
        raise SwitchError("Die automatische Deinstallation wird nur auf Linux unterstützt.")
    if os.geteuid() != 0:
        raise SwitchError("Deinstallation benötigt Root-Rechte: sudo ./world_switcher.py uninstall")

    subprocess.run(["systemctl", "disable", "--now", SERVICE_FILE.name], check=False)
    SERVICE_FILE.unlink(missing_ok=True)
    subprocess.run(["systemctl", "daemon-reload"], check=True)
    print(f"Entfernt: {SERVICE_FILE}")


class MinecraftController:
    def __init__(self, state: StateStore, runner=run_command, sleeper=time.sleep) -> None:
        self.state = state
        self.runner = runner
        self.sleeper = sleeper

    def _run(self, command: list[str], *, timeout: int = 60, required: bool = True) -> subprocess.CompletedProcess[str]:
        result = self.runner(command, timeout=timeout)
        if required and result.returncode != 0:
            output = result.stdout.strip() or "Unbekannter Befehlsfehler"
            raise SwitchError(output[-800:])
        return result

    def server_running(self) -> bool:
        result = self._run(compose_command("ps", "--status", "running", "--quiet", "mc"), required=False)
        return result.returncode == 0 and bool(result.stdout.strip())

    def rcon(self, command: str, *, required: bool = True) -> subprocess.CompletedProcess[str]:
        return self._run(compose_command("exec", "-T", "mc", "rcon-cli", command), required=required)

    def player_status(self) -> PlayerStatus:
        if not self.server_running():
            return PlayerStatus(None, None, False)
        result = self.rcon("list", required=False)
        match = PLAYER_COUNT_PATTERN.search(result.stdout or "") if result.returncode == 0 else None
        if not match:
            return PlayerStatus(None, None, True)
        return PlayerStatus(int(match.group(1)), int(match.group(2)), True)

    def wait_until_ready(self) -> None:
        deadline = time.monotonic() + START_TIMEOUT
        while time.monotonic() < deadline:
            result = self.rcon("list", required=False)
            if result.returncode == 0:
                return
            self.sleeper(2)
        raise SwitchError(f"Minecraft war nach {START_TIMEOUT} Sekunden noch nicht per RCON erreichbar.")

    def switch(self, target: str) -> None:
        worlds = discover_worlds()
        if target not in worlds:
            raise SwitchError("Die ausgewählte Welt existiert nicht mehr.")
        previous = read_active_world()
        if target == previous:
            raise SwitchError("Diese Welt ist bereits aktiv.")

        with LOCK_FILE.open("w", encoding="utf-8") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise SwitchError("Ein anderer Weltwechsel läuft bereits.") from error

            was_running = self.server_running()
            if was_running:
                self.state.set(OperationState(True, target, "Spieler werden über den Neustart informiert …"))
                self.rcon(
                    f"say Der Server wechselt in {WARNING_SECONDS} Sekunden zu {display_name(target)} und startet neu."
                )
                self.sleeper(WARNING_SECONDS)
                self.state.set(OperationState(True, target, "Welt wird gespeichert …"))
                self.rcon("save-all flush")

            self.state.set(OperationState(True, target, "Minecraft wird gestoppt …"))
            if was_running:
                self._run(compose_command("stop", "-t", "60", "mc"), timeout=90)

            try:
                self.state.set(OperationState(True, target, "Neue Welt wird aktiviert …"))
                write_active_world(target)
                self._run(compose_command("up", "-d", "--force-recreate", "--no-deps", "mc"), timeout=120)
                self.state.set(OperationState(True, target, "Minecraft startet …"))
                self.wait_until_ready()
            except Exception as switch_error:
                rollback_message = ""
                if previous:
                    try:
                        self._run(compose_command("stop", "-t", "30", "mc"), timeout=60, required=False)
                        write_active_world(previous)
                        self._run(compose_command("up", "-d", "--force-recreate", "--no-deps", "mc"), timeout=120)
                        rollback_message = f" Die bisherige Welt {display_name(previous)} wurde wieder gestartet."
                    except Exception as rollback_error:
                        rollback_message = f" Auch das Zurückschalten ist fehlgeschlagen: {rollback_error}"
                raise SwitchError(f"Weltwechsel fehlgeschlagen: {switch_error}.{rollback_message}") from switch_error

    def switch_in_background(self, target: str) -> None:
        if not self.state.begin(target):
            raise SwitchError("Ein Weltwechsel läuft bereits.")

        def work() -> None:
            try:
                self.switch(target)
                self.state.set(OperationState(False, target, f"{display_name(target)} ist bereit."))
            except Exception as error:
                self.state.set(OperationState(False, target, str(error), True))

        threading.Thread(target=work, name="world-switch", daemon=True).start()


def render_page(controller: MinecraftController, csrf_token: str) -> str:
    worlds = discover_worlds()
    active = read_active_world()
    operation = controller.state.get()
    players = controller.player_status() if not operation.running else PlayerStatus(None, None, True)

    if operation.running:
        server_label = "Wechsel läuft"
        server_class = "busy"
    elif players.server_running:
        server_label = "Server bereit"
        server_class = "online"
    else:
        server_label = "Server gestoppt"
        server_class = "offline"

    if players.online is None:
        player_text = "Spielerzahl wird gerade nicht gemeldet"
        player_warning = ""
    elif players.online > 0:
        player_text = f"{players.online} von {players.maximum} spielen gerade"
        player_warning = (
            '<div class="player-warning" role="status">'
            '<span aria-hidden="true">⚠</span><div><strong>Gerade wird gespielt.</strong>'
            " Ein Weltwechsel trennt alle Spieler nach der angekündigten Wartezeit.</div></div>"
        )
    else:
        player_text = f"0 von {players.maximum} Spielern online"
        player_warning = ""

    cards = []
    for world in worlds:
        escaped_world = html.escape(world, quote=True)
        escaped_label = html.escape(display_name(world))
        is_active = world == active
        if is_active:
            action = '<span class="active-mark">Aktive Welt</span>'
        else:
            disabled = " disabled" if operation.running else ""
            action = (
                '<form method="post" action="/switch" '
                'onsubmit="return confirm(\'Wirklich die Welt wechseln? Alle Spieler werden getrennt.\');">'
                f'<input type="hidden" name="csrf" value="{html.escape(csrf_token, quote=True)}">'
                f'<input type="hidden" name="world" value="{escaped_world}">'
                f'<button type="submit"{disabled}>Zu {escaped_label} wechseln</button></form>'
            )
        cards.append(
            f'<article class="world-card{" active" if is_active else ""}">'
            '<div class="world-icon" aria-hidden="true"><span></span></div>'
            f'<div class="world-copy"><h2>{escaped_label}</h2><code>{escaped_world}</code></div>{action}</article>'
        )

    if not cards:
        cards.append(
            '<div class="empty"><strong>Keine Welten gefunden.</strong>'
            f' Lege vollständige Weltordner mit <code>level.dat</code> unter <code>{html.escape(str(DATA_DIR))}</code> ab.</div>'
        )

    operation_notice = ""
    if operation.message != "Bereit":
        notice_class = "notice error" if operation.error else "notice"
        operation_notice = f'<div class="{notice_class}" role="status">{html.escape(operation.message)}</div>'

    refresh = '<meta http-equiv="refresh" content="3">' if operation.running else ""
    active_label = html.escape(display_name(active)) if active else "Nicht gesetzt"
    return f"""<!doctype html>
<html lang="de">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  {refresh}
  <title>Minecraft · Weltwechsel</title>
  <style>
    :root {{
      --ground: #101923;
      --panel: #172431;
      --panel-raised: #203141;
      --text: #edf5ea;
      --muted: #9fb0b8;
      --accent: #70bd78;
      --accent-dark: #397b4c;
      --torch: #e8ad45;
      --danger: #ef806d;
      --line: #314555;
      --shadow: #091017;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      min-height: 100vh;
      color: var(--text);
      background-color: var(--ground);
      background-image: linear-gradient(rgba(255,255,255,.018) 1px, transparent 1px), linear-gradient(90deg, rgba(255,255,255,.018) 1px, transparent 1px);
      background-size: 32px 32px;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }}
    body::before {{
      content: "";
      position: fixed;
      inset: 0 0 auto 0;
      height: 5px;
      background: linear-gradient(90deg, var(--accent-dark) 0 33%, var(--accent) 33% 66%, var(--torch) 66%);
    }}
    main {{ width: min(900px, calc(100% - 32px)); margin: 0 auto; padding: 72px 0 64px; }}
    header {{ display: grid; grid-template-columns: 1fr auto; gap: 32px; align-items: end; padding-bottom: 28px; border-bottom: 1px solid var(--line); }}
    .eyebrow, code {{ font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace; }}
    .eyebrow {{ color: var(--accent); font-size: .75rem; font-weight: 700; letter-spacing: .16em; text-transform: uppercase; }}
    h1 {{ margin: 8px 0 0; font-family: Charter, "Bitstream Charter", Georgia, serif; font-size: clamp(2.4rem, 7vw, 4.8rem); font-weight: 600; line-height: .96; letter-spacing: -.045em; }}
    .status {{ min-width: 190px; padding: 14px 16px; border: 1px solid var(--line); background: rgba(23,36,49,.78); box-shadow: 8px 8px 0 var(--shadow); }}
    .status-line {{ display: flex; align-items: center; gap: 9px; font-weight: 700; }}
    .dot {{ width: 9px; height: 9px; background: var(--muted); box-shadow: 0 0 0 3px rgba(159,176,184,.12); }}
    .online .dot {{ background: var(--accent); box-shadow: 0 0 0 3px rgba(112,189,120,.15); }}
    .busy .dot {{ background: var(--torch); box-shadow: 0 0 0 3px rgba(232,173,69,.15); animation: pulse 1.2s steps(2) infinite; }}
    .status small {{ display: block; margin-top: 7px; color: var(--muted); }}
    .summary {{ display: grid; grid-template-columns: 1fr 1fr; gap: 1px; margin: 28px 0; border: 1px solid var(--line); background: var(--line); }}
    .summary > div {{ padding: 17px 18px; background: var(--panel); }}
    .summary span {{ display: block; margin-bottom: 5px; color: var(--muted); font-size: .75rem; letter-spacing: .08em; text-transform: uppercase; }}
    .summary strong {{ font-size: 1.05rem; }}
    .player-warning, .notice, .empty {{ padding: 16px 18px; border: 1px solid var(--torch); background: rgba(232,173,69,.09); }}
    .player-warning {{ display: flex; gap: 12px; align-items: flex-start; margin-bottom: 18px; }}
    .player-warning > span {{ color: var(--torch); font-size: 1.15rem; }}
    .notice {{ margin-bottom: 18px; color: var(--text); }}
    .notice.error {{ border-color: var(--danger); background: rgba(239,128,109,.09); }}
    .world-list {{ display: grid; gap: 12px; }}
    .world-card {{ display: grid; grid-template-columns: 52px 1fr auto; gap: 17px; align-items: center; min-height: 96px; padding: 18px; border: 1px solid var(--line); background: var(--panel); transition: transform .16s ease, border-color .16s ease, background .16s ease; }}
    .world-card:not(.active):hover {{ transform: translateY(-2px); border-color: #4b6475; background: var(--panel-raised); }}
    .world-card.active {{ border-left: 5px solid var(--accent); background: linear-gradient(90deg, rgba(112,189,120,.09), var(--panel) 24%); }}
    .world-icon {{ width: 48px; height: 48px; position: relative; background: #577f47; box-shadow: inset 0 -15px #745940, inset 0 -20px #896b4c, 4px 4px 0 var(--shadow); }}
    .world-icon::before, .world-icon::after, .world-icon span {{ content: ""; position: absolute; width: 8px; height: 8px; background: #83b967; }}
    .world-icon::before {{ left: 8px; top: 7px; }} .world-icon::after {{ right: 8px; top: 13px; }} .world-icon span {{ left: 23px; top: 2px; }}
    h2 {{ margin: 0 0 5px; font-size: 1.12rem; }}
    code {{ color: var(--muted); font-size: .78rem; }}
    button {{ min-height: 44px; padding: 0 16px; border: 1px solid var(--accent); color: var(--ground); background: var(--accent); font: inherit; font-weight: 750; cursor: pointer; box-shadow: 4px 4px 0 var(--shadow); }}
    button:hover {{ background: #86cd8c; }} button:active {{ transform: translate(2px,2px); box-shadow: 2px 2px 0 var(--shadow); }}
    button:focus-visible {{ outline: 3px solid var(--torch); outline-offset: 3px; }} button:disabled {{ opacity: .45; cursor: wait; }}
    .active-mark {{ color: var(--accent); font-size: .78rem; font-weight: 800; letter-spacing: .08em; text-transform: uppercase; }}
    footer {{ margin-top: 24px; color: var(--muted); font-size: .8rem; }}
    @keyframes pulse {{ 50% {{ opacity: .35; }} }}
    @media (max-width: 650px) {{
      main {{ width: min(100% - 22px, 900px); padding-top: 48px; }} header {{ grid-template-columns: 1fr; gap: 22px; }} .status {{ width: 100%; }}
      .summary {{ grid-template-columns: 1fr; }} .world-card {{ grid-template-columns: 48px 1fr; }} .world-card form, .active-mark {{ grid-column: 1 / -1; }} button {{ width: 100%; }}
    }}
    @media (prefers-reduced-motion: reduce) {{ *, *::before, *::after {{ animation: none !important; transition: none !important; }} }}
  </style>
</head>
<body>
  <main>
    <header>
      <div><div class="eyebrow">Minecraft · Weltsteuerung</div><h1>Welche Welt<br>ist heute dran?</h1></div>
      <div class="status {server_class}"><div class="status-line"><span class="dot"></span>{server_label}</div><small>{html.escape(player_text)}</small></div>
    </header>
    <section class="summary" aria-label="Aktueller Zustand"><div><span>Aktive Welt</span><strong>{active_label}</strong></div><div><span>Beim Wechsel</span><strong>{WARNING_SECONDS} Sek. Warnzeit · Neustart</strong></div></section>
    {player_warning}{operation_notice}
    <section class="world-list" aria-label="Verfügbare Welten">{''.join(cards)}</section>
    <footer>Nur im Tailscale-Netz erreichbar · Welten werden anhand ihrer <code>level.dat</code> erkannt</footer>
  </main>
</body>
</html>"""


class SwitcherServer(ThreadingHTTPServer):
    def __init__(self, address: tuple[str, int], controller: MinecraftController):
        super().__init__(address, SwitcherHandler)
        self.controller = controller
        self.csrf_token = secrets.token_urlsafe(32)


class SwitcherHandler(BaseHTTPRequestHandler):
    server_version = "MinecraftWorldSwitcher/1.0"

    @property
    def app(self) -> SwitcherServer:
        return self.server  # type: ignore[return-value]

    def log_message(self, format_string: str, *args: object) -> None:
        sys.stderr.write(f"{self.client_address[0]} [{self.log_date_time_string()}] {format_string % args}\n")

    def send_html(self, status: HTTPStatus, body: str) -> None:
        encoded = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(encoded)

    def redirect_home(self) -> None:
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", "/")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/health":
            body = b"ok\n"
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path != "/":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        self.send_html(HTTPStatus.OK, render_page(self.app.controller, self.app.csrf_token))

    def do_POST(self) -> None:
        if urlparse(self.path).path != "/switch":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 4096:
                raise SwitchError("Ungültige Anfrage.")
            body = self.rfile.read(length).decode("utf-8")
            fields = parse_qs(body, keep_blank_values=True)
            csrf = fields.get("csrf", [""])[0]
            target = fields.get("world", [""])[0]
            if not secrets.compare_digest(csrf, self.app.csrf_token):
                raise SwitchError("Die Seite ist veraltet. Bitte neu laden.")
            self.app.controller.switch_in_background(target)
            self.redirect_home()
        except SwitchError as error:
            self.app.controller.state.set(OperationState(False, "", str(error), True))
            self.redirect_home()
        except (UnicodeDecodeError, ValueError):
            self.send_error(HTTPStatus.BAD_REQUEST)


def main() -> None:
    if len(sys.argv) > 1:
        command = sys.argv[1]
        if command == "install" and len(sys.argv) == 2:
            install_service()
            return
        if command == "uninstall" and len(sys.argv) == 2:
            uninstall_service()
            return
        if command in {"-h", "--help"} and len(sys.argv) == 2:
            print(
                "Minecraft World Switcher\n\n"
                "  ./world_switcher.py            Webserver direkt starten\n"
                "  sudo ./world_switcher.py install    Autostart installieren\n"
                "  sudo ./world_switcher.py uninstall  Autostart entfernen"
            )
            return
        raise SwitchError("Unbekannter Aufruf. Verwende --help für die verfügbaren Befehle.")

    host = discover_tailscale_ip()
    state = StateStore()
    controller = MinecraftController(state)
    server = SwitcherServer((host, PORT), controller)
    print(f"Minecraft World Switcher hört auf http://{host}:{PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    try:
        main()
    except SwitchError as error:
        print(f"Fehler: {error}", file=sys.stderr)
        raise SystemExit(1) from error
    except subprocess.CalledProcessError as error:
        print(f"Fehler beim Systembefehl: {error}", file=sys.stderr)
        raise SystemExit(error.returncode or 1) from error
