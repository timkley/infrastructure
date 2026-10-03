from __future__ import annotations

from datetime import UTC, datetime
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import httpx

os.environ.setdefault("HEISENBERG_ACCESS_MCP_TOKEN", "test-token")

from heisenberg_access_mcp.whatsapp import Archive, ArchiveError, connection, _ref
from heisenberg_access_mcp.server import app, build_mcp


class ArchiveFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.archive = Archive(self.root)
        self.writer = sqlite3.connect(self.root / "wacli.db")
        self.addCleanup(self.writer.close)
        self.writer.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE chats(jid TEXT PRIMARY KEY, name TEXT, kind TEXT);
        CREATE TABLE messages(rowid INTEGER PRIMARY KEY, chat_jid TEXT, chat_name TEXT, msg_id TEXT,
          sender_jid TEXT, sender_name TEXT, ts INTEGER, from_me INTEGER DEFAULT 0, text TEXT, display_text TEXT,
          media_caption TEXT, media_type TEXT, filename TEXT, mime_type TEXT, file_length INTEGER, local_path TEXT,
          media_unavailable_at INTEGER, quoted_msg_id TEXT, quoted_sender_jid TEXT, edited INTEGER DEFAULT 0,
          edited_ts INTEGER DEFAULT 0, revoked INTEGER DEFAULT 0, deleted_for_me INTEGER DEFAULT 0,
          media_key BLOB, direct_path TEXT, file_enc_sha256 BLOB);
        CREATE VIRTUAL TABLE messages_fts USING fts5(text,media_caption,filename,chat_name,sender_name,display_text);
        INSERT INTO chats VALUES ('one@g.us','Group 100%','group'),('two@s.whatsapp.net','Second','dm'),('empty@lid','Empty','unknown');
        """)

    def insert(self, mid, ts=1, jid="one@g.us", text="Test pizza", **kwargs):
        values = {"chat_jid": jid, "chat_name": "Group 100%", "msg_id": mid, "ts": ts, "text": text,
                  "sender_jid": "sender@lid", "sender_name": "Someone", "media_key": b"transport-secret-marker",
                  "direct_path": "private-cdn-path", "file_enc_sha256": b"encrypted-secret-marker", **kwargs}
        placeholders = ",".join("?" for _ in values)
        r = self.writer.execute("INSERT INTO messages(" + ",".join(values) + ") VALUES(" + placeholders + ")", list(values.values()))
        self.writer.execute("INSERT INTO messages_fts(rowid,text,media_caption,filename,chat_name,sender_name,display_text) "
            "VALUES(?,?,?,?,?,?,?)", [r.lastrowid, text, kwargs.get("media_caption"), kwargs.get("filename"),
                                      "Group 100%", "Someone", kwargs.get("display_text")])
        self.writer.commit()
        return _ref([jid, mid])

    def test_reader_sees_new_wal_data_and_cannot_write(self):
        self.insert("a", text="first")
        with connection(self.root) as reader:
            self.assertEqual(reader.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 1)
            with self.assertRaises(sqlite3.OperationalError):
                reader.execute("DELETE FROM messages")
            self.insert("b", text="second")
            self.assertEqual(reader.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 1)
        self.assertEqual(self.archive.status()["counts"]["messages"], 2)
        self.assertTrue((self.root / "wacli.db-wal").is_file())

    def test_search_filters_deleted_rows_and_never_exposes_transport_fields(self):
        self.insert("a", ts=1)
        self.insert("b", ts=2, revoked=1)
        self.insert("c", ts=3, deleted_for_me=1)
        ref = self.insert("d", ts=4, media_type="image", filename="pizza.jpg")
        self.insert("e", ts=5, jid="two@s.whatsapp.net", sender_jid="another@lid")
        result = self.archive.search("pizza", chat_ref=_ref(["one@g.us"]), sender_ref=_ref(["sender@lid"]), media_type="image")
        self.assertEqual([r["message_ref"] for r in result["messages"]], [ref])
        serialized = json.dumps(result)
        for value in ("transport-secret-marker", "encrypted-secret-marker", "private-cdn-path", "media_key", "direct_path", "local_path"):
            self.assertNotIn(value, serialized)
        self.assertEqual([r["message_id"] for r in self.archive.search("pizza")["messages"]], ["e", "d", "a"])

    def test_fts_input_is_literal_words_and_pagination_does_not_drop_ties(self):
        self.insert("a", ts=4, text="Pizza quote")
        self.insert("b", ts=4, text="Pizza quote")
        self.insert("c", ts=4, text="Pizza quote")
        page = self.archive.search('pizza " quote *', limit=2)
        self.assertEqual([r["message_id"] for r in page["messages"]], ["c", "b"])
        self.assertEqual([r["message_id"] for r in self.archive.search("pizza", limit=2, offset=page["next_offset"])["messages"]], ["a"])
        self.assertEqual(self.archive.search("pizza ' UNION SELECT secret")["messages"], [])
        with self.assertRaises(ArchiveError):
            self.archive.search("*")

    def test_berlin_date_filter_includes_25_hour_dst_day(self):
        ts = lambda value: int(datetime.fromisoformat(value).timestamp())
        self.insert("before", ts=ts("2026-10-24T21:59:59+00:00"))
        self.insert("start", ts=ts("2026-10-24T22:00:00+00:00"))
        self.insert("last", ts=ts("2026-10-25T22:59:59+00:00"))
        self.insert("after", ts=ts("2026-10-25T23:00:00+00:00"))
        found = self.archive.search("pizza", start="2026-10-25", end="2026-10-25")["messages"]
        self.assertEqual([r["message_id"] for r in found], ["last", "start"])
        with self.assertRaises(ArchiveError):
            self.archive.search("pizza", start="2026-10-25T10:00:00")

    def test_search_snippet_finds_terms_beyond_initial_text_slice(self):
        self.insert("long", text="before " * 500 + "needleword " + "after " * 500)
        result = self.archive.search("needleword")["messages"][0]
        self.assertNotIn("needleword", result["text"])
        self.assertIn("[needleword]", result["matched_text"])
        self.assertLessEqual(len(result["matched_text"]), 1000)

    def test_timeline_can_filter_voicenotes_without_any_searchable_text(self):
        older = self.insert("voice-old", ts=2, text="", media_type="audio")
        newer = self.insert("voice-new", ts=3, text="", media_type="audio")
        self.insert("other-chat", ts=1, text="", media_type="audio", jid="two@s.whatsapp.net")
        self.insert("deleted", ts=4, text="", media_type="audio", deleted_for_me=1)
        page = self.archive.search(chat_ref=_ref(["one@g.us"]), media_type="audio", order="oldest", limit=1)
        self.assertEqual(page["messages"][0]["message_ref"], older)
        self.assertEqual(page["match_mode"], "timeline")
        next_page = self.archive.search(chat_ref=_ref(["one@g.us"]), media_type="audio", order="oldest", limit=1, offset=page["next_offset"])
        self.assertEqual(next_page["messages"][0]["message_ref"], newer)
        self.assertIsNone(next_page["next_offset"])
        with self.assertRaisesRegex(ArchiveError, "query_required"):
            self.archive.search(order="relevance")

    def test_context_orders_timestamp_ties_scopes_chat_and_reads_reply(self):
        quoted = self.insert("quoted", ts=1)
        self.insert("before", ts=3)
        anchor = self.insert("anchor", ts=3, quoted_msg_id="quoted", text="a" * 1500 + "tail")
        self.insert("other", ts=3, jid="two@s.whatsapp.net")
        self.insert("after", ts=3)
        result = self.archive.context(anchor, before=1, after=1)
        self.assertEqual([r["message_id"] for r in result["messages"]], ["before", "anchor", "after"])
        self.assertEqual(result["quoted_message"]["message_ref"], quoted)
        self.assertEqual(result["messages"][1]["next_text_offset"], 1500)
        sliced = self.archive.context(anchor, before=0, after=0, message_text_offset=1500)["messages"][0]
        self.assertEqual(sliced["text"], "tail")
        self.assertIsNone(sliced["next_text_offset"])
        deleted = self.insert("deleted", text="original deleted secret", revoked=1)
        self.assertEqual(self.archive.context(deleted, 0, 0)["messages"][0]["text"], "")

    def test_chats_literal_filter_and_anchor_gap_status(self):
        self.insert("a")
        self.assertEqual([r["name"] for r in self.archive.chats(query="100%")["chats"]], ["Group 100%"])
        self.assertEqual(len(self.archive.chats(kind="dm")["chats"]), 1)
        self.assertEqual(self.archive.chats(limit=1)["next_offset"], 1)
        status = self.archive.status()
        self.assertEqual(status["counts"]["chats_without_anchor"], 2)
        self.assertEqual(status["history_exhaustiveness"], "not_guaranteed")
        (self.root / "archive-backfill.json").write_text(json.dumps({"phase": "history", "chats": {
            "private-chat-id": {"status": "request_failed"}, "other-id": {"status": "no_local_anchor"}}}))
        status = self.archive.status()
        self.assertEqual(status["backfill"]["outcomes"], {"request_failed": 1, "no_local_anchor": 1})
        self.assertNotIn("private-chat-id", json.dumps(status))

    def test_attachment_checks_actual_file_and_rejects_traversal_and_symlinks(self):
        (self.root / "media").mkdir()
        image = self.root / "media/picture.jpg"
        image.write_bytes(b"test image")
        valid = self.insert("valid", local_path="/data/store/media/picture.jpg", media_type="image", filename="Foto.jpg", mime_type="image/jpeg", file_length=999)
        path, metadata = self.archive.attachment(valid)
        self.assertEqual(path, image)
        self.assertEqual(metadata["byte_size"], 10)
        (self.root / "session.db").write_bytes(b"device secrets")
        (self.root / "media/escape").symlink_to(self.root / "session.db")
        for index, stored in enumerate(("media/../session.db", "media/escape", str(self.root / "session.db"))):
            ref = self.insert("invalid" + str(index), local_path=stored)
            with self.assertRaisesRegex(ArchiveError, "path_invalid"):
                self.archive.attachment(ref)
        image.unlink()
        with self.assertRaisesRegex(ArchiveError, "file_missing"):
            self.archive.attachment(valid)
        gone = self.insert("gone", media_type="image", media_unavailable_at=10)
        with self.assertRaisesRegex(ArchiveError, "unavailable_on_phone"):
            self.archive.attachment(gone)

    def test_media_failure_summary_does_not_expose_private_per_message_outcomes(self):
        (self.root / "archive-backfill.json").write_text(json.dumps({"phase": "finished", "chats": {}, "media": {
            "media_retry": {"exit_code": 0, "summary": {"requested": 12, "recovered": 8, "no_response": 4,
                "outcomes": [{"chat_jid": "private-chat-id", "path": "/private/path", "detail": "private-error"}]}}}}))
        result = self.archive.status()
        self.assertEqual(result["backfill"]["media_runs"]["media_retry"]["counts"], {"requested": 12, "recovered": 8, "no_response": 4})
        for value in ("private-chat-id", "/private/path", "private-error"):
            self.assertNotIn(value, json.dumps(result))

    def test_bounds_and_foreign_references_are_rejected(self):
        for invalid in ("work:1", "private:../../session.db", "private:WzFd"):
            with self.assertRaises(ArchiveError):
                self.archive.context(invalid)
        with self.assertRaises(ArchiveError):
            self.archive.search("pizza", limit=51)
        with self.assertRaises(ArchiveError):
            self.archive.context(_ref(["one@g.us", "a"]), before=16)


class PrivateWhatsAppIntegration(unittest.IsolatedAsyncioTestCase):
    async def test_tools_register_readonly_and_unconfigured_archive_fails_safely(self):
        with patch.dict(os.environ, {"HEISENBERG_ACCESS_MCP_WHATSAPP_STORE": ""}):
            mcp = build_mcp()
            tools = [t for t in await mcp.list_tools() if t.name.startswith("whatsapp.")]
            self.assertEqual({t.name for t in tools}, {"whatsapp.archive_status", "whatsapp.list_chats", "whatsapp.search_messages", "whatsapp.get_context", "whatsapp.get_attachment"})
            self.assertTrue(all(t.annotations.readOnlyHint and not t.annotations.destructiveHint for t in tools))
            status = await mcp._tool_manager.get_tool("access_status").fn(None)
            self.assertFalse(status["capabilities"]["whatsapp.search_messages"]["enabled"])
            result = mcp._tool_manager.get_tool("whatsapp.archive_status").fn()
            self.assertEqual(result, {"ok": False, "source": "private", "error": "whatsapp_archive_not_configured"})

    async def test_attachment_http_route_requires_bearer_before_any_archive_access(self):
        token = os.environ["HEISENBERG_ACCESS_MCP_TOKEN"]
        with patch("heisenberg_access_mcp.server.whatsapp_download_response", return_value=httpx.Response(200)) as download:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
                for headers in ({}, {"Authorization": "Bearer wrong"}):
                    response = await client.get("/whatsapp/attachments/private:invalid", headers=headers)
                    self.assertEqual(response.status_code, 401)
                download.assert_not_called()
        with patch.dict(os.environ, {"HEISENBERG_ACCESS_MCP_WHATSAPP_STORE": ""}):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
                response = await client.get("/whatsapp/attachments/private:invalid", headers={"Authorization": "Bearer " + token})
                self.assertEqual(response.status_code, 404)
                self.assertEqual(response.json()["error"], "whatsapp_archive_not_configured")
