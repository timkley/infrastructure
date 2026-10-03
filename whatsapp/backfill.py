#!/usr/bin/env python3
"""Run on the Compose host. Private journal; always restore the follow service."""
from __future__ import annotations

import fcntl
import argparse
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import time
from datetime import UTC, datetime

ROOT = Path(__file__).resolve().parent
STORE = ROOT / "data/store"
WORKER = "whatsapp-backfill-worker"


def now():
    return datetime.now(UTC).isoformat()


def db():
    return sqlite3.connect((STORE / "wacli.db").as_uri() + "?mode=ro", uri=True, timeout=5)


def counts():
    with db() as c:
        return dict(zip(("messages", "downloadable_media", "downloaded_media", "unavailable_media"), c.execute(
            "SELECT COUNT(*), SUM(media_key IS NOT NULL AND direct_path <> ''), "
            "SUM(local_path IS NOT NULL AND local_path <> ''), SUM(media_unavailable_at IS NOT NULL) FROM messages"
        ).fetchone()))


def run(args, log, timeout):
    cmd = ["docker", "compose", "run", "--rm", "--no-deps", "--name", WORKER,
           "app", "--json", "--events", *args]
    with log.open("wb") as output:
        p = subprocess.Popen(cmd, cwd=ROOT, stdout=subprocess.PIPE, stderr=output)
        try:
            stdout, _ = p.communicate(timeout=timeout)
            output.write(stdout)
            code = p.returncode
        except subprocess.TimeoutExpired:
            code = 124
        finally:
            if p.poll() is None:
                # Remove only this runner's named container; release the store lock.
                subprocess.run(["docker", "rm", "-f", WORKER], capture_output=True)
                p.terminate()
                stdout, _ = p.communicate(timeout=30)
                output.write(stdout)
    events = []
    for line in log.read_text(errors="replace").splitlines():
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict):
            events.append(item)
    try:
        summary = json.loads(stdout)
        if isinstance(summary, dict):
            events.append(summary)
    except (ValueError, UnboundLocalError):
        pass
    return code, events


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resume", action="store_true", help="continue interrupted journal, skipping already attempted chats")
    options = parser.parse_args()
    os.umask(0o077)
    logs = ROOT / "data/backfill" / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    logs.mkdir(parents=True, mode=0o700)
    lock = (ROOT / "data/backfill/runner.lock").open("a")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    state = {"started_at": now(), "phase": "snapshot", "before": counts(), "chats": {},
             "history_exhaustiveness": "not_guaranteed", "media": {}}
    if options.resume:
        previous = STORE / "archive-backfill.json"
        if not previous.is_file():
            raise RuntimeError("no_backfill_journal_to_resume")
        state = json.loads(previous.read_text())
        state["resumed_at"] = now()

    def save():
        state["updated_at"] = now()
        state["after"] = counts()
        temporary = STORE / "archive-backfill.json.tmp"
        temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2))
        temporary.replace(STORE / "archive-backfill.json")
        (logs / "result.json").write_text(json.dumps(state, ensure_ascii=False, indent=2))

    def interrupted(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    subprocess.run(["docker", "compose", "stop", "app"], cwd=ROOT, check=True)
    try:
        for name in ("wacli.db", "session.db"):
            with sqlite3.connect((STORE / name).as_uri() + "?mode=ro", uri=True) as src:
                with sqlite3.connect(logs / name) as dst:
                    src.backup(dst)
                    if dst.execute("PRAGMA quick_check").fetchone() != ("ok",):
                        raise RuntimeError("snapshot_integrity_failed")
        with db() as c:
            chats = c.execute("SELECT c.jid, COUNT(m.rowid) FROM chats c LEFT JOIN messages m "
                              "ON m.chat_jid=c.jid GROUP BY c.jid ORDER BY MAX(m.ts) DESC").fetchall()
        state["selected_chats"] = len(chats)
        state["phase"] = "history"
        save()
        print(json.dumps({"phase": "history", "chats": len(chats), "before": state["before"]}), flush=True)
        for index, (jid, total) in enumerate(chats, 1):
            if options.resume and state["chats"].get(jid, {}).get("status") not in (None, "pending", "batch_limit_reached"):
                continue
            entry = {"status": "no_local_anchor" if not total else "pending", "batches": 0}
            state["chats"][jid] = entry
            if total:
                for batch in range(20):
                    with db() as c:
                        oldest = c.execute("SELECT msg_id FROM messages WHERE chat_jid=? ORDER BY ts,rowid LIMIT 1", (jid,)).fetchone()
                    code, events = run(["history", "backfill", "--chat", jid, "--count", "500", "--requests", "100",
                                        "--wait", "15s", "--idle-exit", "2s"], logs / f"history-{index}-{batch}.log", 1200)
                    entry["batches"] += 1
                    entry["responses"] = entry.get("responses", 0) + sum(e.get("event") == "backfill_response" for e in events)
                    reasons = [e["data"]["reason"] for e in events
                               if e.get("event") == "backfill_stopped" and e.get("data", {}).get("reason")]
                    if code:
                        entry["status"] = "command_timeout" if code == 124 else "request_failed"
                        break
                    if reasons:
                        entry["status"] = reasons[-1]
                        break
                    with db() as c:
                        newer = c.execute("SELECT msg_id FROM messages WHERE chat_jid=? ORDER BY ts,rowid LIMIT 1", (jid,)).fetchone()
                    if oldest == newer:
                        entry["status"] = "no_older_messages_added"
                        break
                else:
                    entry["status"] = "batch_limit_reached"
            save()
            summary = {}
            for item in state["chats"].values():
                summary[item["status"]] = summary.get(item["status"], 0) + 1
            print(json.dumps({"phase": "history", "processed": index, "total": len(chats), "outcomes": summary,
                              "messages": state["after"]["messages"]}), flush=True)
        for phase, args, timeout in (
            ("media_backfill", ["--timeout", "45m", "media", "backfill", "--limit", "0", "--workers", "4"], 2800),
            ("media_retry", ["--timeout", "90m", "media", "retry", "--limit", "0", "--batch", "32", "--wait", "15s"], 5500),
        ):
            state["phase"] = phase
            save()
            print(json.dumps({"phase": phase, "counts": state["after"]}), flush=True)
            code, events = run(args, logs / f"{phase}.log", timeout)
            # Keep the journal compact; per-message retry outcomes stay in private logs.
            summary = next((e.get("data") for e in reversed(events) if "success" in e), None)
            aggregate = {k: v for k, v in summary.items() if isinstance(v, int) and not isinstance(v, bool)} if isinstance(summary, dict) else None
            state["media"][phase] = {"exit_code": code, "summary": aggregate}
            save()
        state["phase"] = "finished"
        state["finished_at"] = now()
        save()
        print(json.dumps({"phase": "finished", "before": state["before"], "after": state["after"]}), flush=True)
    except BaseException as error:
        state["phase"] = "interrupted" if isinstance(error, KeyboardInterrupt) else "failed"
        save()
        raise
    finally:
        subprocess.run(["docker", "rm", "-f", WORKER], capture_output=True)
        subprocess.run(["docker", "compose", "up", "-d", "app"], cwd=ROOT, check=True)


if __name__ == "__main__":
    main()
