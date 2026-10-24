"""
Single-file SQLite storage for everything JARVIS remembers.

Why SQLite: one small file, zero servers, works identically on the cloud VM and on the
phone, transactional, and ships FTS5 full-text search for conversation history. Typical
size for a year of personal use is a few MB.

Tables
  messages      conversation history (+ messages_fts for search). Content is redacted
                before it is written, so OTPs/card numbers never land on disk.
  requests      the user's inbox of things to decide: permission prompts and approvals.
  grants        permission grants per scope (always / once / deny).
  audit         every tool execution attempt and its outcome.
  kv            small metadata: preferences, focus entities, device snapshot, settings.
  secrets       OAuth tokens etc., encrypted with Fernet (AES-128-CBC + HMAC).
  graph_nodes / graph_edges   the knowledge graph (loaded into networkx at start-up).
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from typing import Any, Optional

from cryptography.fernet import Fernet, InvalidToken

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS messages(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, role TEXT NOT NULL,
  content TEXT NOT NULL, agent TEXT, meta TEXT);
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(content, content='messages', content_rowid='id');
CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
  INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content); END;
CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
  INSERT INTO messages_fts(messages_fts, rowid, content) VALUES('delete', old.id, old.content); END;
CREATE TABLE IF NOT EXISTS requests(
  id TEXT PRIMARY KEY, kind TEXT NOT NULL, status TEXT NOT NULL, created REAL NOT NULL,
  resolved REAL, payload TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS requests_status ON requests(status, created);
CREATE TABLE IF NOT EXISTS grants(
  scope TEXT PRIMARY KEY, mode TEXT NOT NULL, ts REAL NOT NULL);
CREATE TABLE IF NOT EXISTS audit(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, actor TEXT, tool TEXT,
  args TEXT, outcome TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS secrets(name TEXT PRIMARY KEY, token BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS graph_nodes(
  id TEXT PRIMARY KEY, type TEXT NOT NULL, label TEXT NOT NULL, attributes TEXT NOT NULL, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS graph_edges(
  src TEXT NOT NULL, dst TEXT NOT NULL, type TEXT NOT NULL, attributes TEXT NOT NULL, created REAL NOT NULL,
  PRIMARY KEY(src, dst, type));
"""


class Database:
    def __init__(self, path: str = ":memory:", data_key: Optional[str] = None):
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.path = path
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._conn.executescript(SCHEMA)
        if path != ":memory:":
            try:
                os.chmod(path, 0o600)  # owner-only on Unix
            except OSError:
                pass
        self._fernet = Fernet(self._resolve_key(data_key))

    # ------------------------------------------------------------------ helpers
    def _resolve_key(self, data_key: Optional[str]) -> bytes:
        if data_key:
            return data_key.encode()
        stored = self.kv_get("_data_key")
        if stored:
            # Fallback only. In production set JARVIS_DATA_KEY (env / keystore) so the key
            # does not live next to the data it protects.
            return stored.encode()
        key = Fernet.generate_key().decode()
        self.kv_set("_data_key", key)
        return key.encode()

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, params)

    def query(self, sql: str, params: tuple = ()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    # ------------------------------------------------------------------ kv
    def kv_get(self, key: str, default: Any = None) -> Any:
        rows = self.query("SELECT value FROM kv WHERE key=?", (key,))
        if not rows:
            return default
        try:
            return json.loads(rows[0]["value"])
        except json.JSONDecodeError:
            return rows[0]["value"]

    def kv_set(self, key: str, value: Any) -> None:
        self.execute("INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (key, json.dumps(value)))

    # ------------------------------------------------------------------ secrets
    def secret_set(self, name: str, value: str) -> None:
        token = self._fernet.encrypt(value.encode())
        self.execute("INSERT INTO secrets(name,token) VALUES(?,?) ON CONFLICT(name) DO UPDATE SET token=excluded.token",
                     (name, token))

    def secret_get(self, name: str) -> Optional[str]:
        rows = self.query("SELECT token FROM secrets WHERE name=?", (name,))
        if not rows:
            return None
        try:
            return self._fernet.decrypt(rows[0]["token"]).decode()
        except InvalidToken:
            return None

    def secrets_wipe(self) -> None:
        self.execute("DELETE FROM secrets")

    # ------------------------------------------------------------------ messages
    def add_message(self, role: str, content: str, agent: Optional[str] = None, meta: Optional[dict] = None) -> int:
        cur = self.execute("INSERT INTO messages(ts,role,content,agent,meta) VALUES(?,?,?,?,?)",
                           (time.time(), role, content, agent, json.dumps(meta or {})))
        return cur.lastrowid

    def recent_messages(self, limit: int = 8) -> list[dict]:
        rows = self.query("SELECT * FROM messages ORDER BY id DESC LIMIT ?", (limit,))
        return list(reversed(rows))

    def search_messages(self, text: str, limit: int = 5) -> list[dict]:
        terms = " OR ".join(f'"{t}"' for t in text.replace('"', " ").split() if len(t) > 2)
        if not terms:
            return []
        return self.query(
            "SELECT m.* FROM messages_fts f JOIN messages m ON m.id=f.rowid WHERE messages_fts MATCH ? "
            "ORDER BY rank LIMIT ?", (terms, limit))

    def trim_messages(self, keep: int = 2000) -> None:
        """Bounded history so the phone never fills up."""
        self.execute("DELETE FROM messages WHERE id NOT IN (SELECT id FROM messages ORDER BY id DESC LIMIT ?)", (keep,))

    # ------------------------------------------------------------------ requests (inbox)
    def add_request(self, kind: str, payload: dict) -> str:
        rid = uuid.uuid4().hex[:10]
        self.execute("INSERT INTO requests(id,kind,status,created,payload) VALUES(?,?,?,?,?)",
                     (rid, kind, "pending", time.time(), json.dumps(payload, default=str)))
        return rid

    def get_request(self, rid: str) -> Optional[dict]:
        rows = self.query("SELECT * FROM requests WHERE id=?", (rid,))
        if not rows:
            return None
        r = rows[0]
        r["payload"] = json.loads(r["payload"])
        return r

    def pending_requests(self) -> list[dict]:
        rows = self.query("SELECT * FROM requests WHERE status='pending' ORDER BY created")
        for r in rows:
            r["payload"] = json.loads(r["payload"])
        return rows

    def resolve_request(self, rid: str, status: str) -> bool:
        cur = self.execute("UPDATE requests SET status=?, resolved=? WHERE id=? AND status='pending'",
                           (status, time.time(), rid))
        return cur.rowcount == 1

    # ------------------------------------------------------------------ grants
    def set_grant(self, scope: str, mode: str) -> None:
        self.execute("INSERT INTO grants(scope,mode,ts) VALUES(?,?,?) ON CONFLICT(scope) DO UPDATE SET mode=excluded.mode, ts=excluded.ts",
                     (scope, mode, time.time()))

    def get_grant(self, scope: str) -> Optional[str]:
        rows = self.query("SELECT mode FROM grants WHERE scope=?", (scope,))
        return rows[0]["mode"] if rows else None

    def remove_grant(self, scope: str) -> None:
        self.execute("DELETE FROM grants WHERE scope=?", (scope,))

    def all_grants(self) -> dict[str, str]:
        return {r["scope"]: r["mode"] for r in self.query("SELECT scope, mode FROM grants")}

    def clear_grants(self) -> None:
        self.execute("DELETE FROM grants")

    # ------------------------------------------------------------------ audit
    def audit(self, actor: str, tool: str, args: dict, outcome: str, detail: str = "") -> None:
        self.execute("INSERT INTO audit(ts,actor,tool,args,outcome,detail) VALUES(?,?,?,?,?,?)",
                     (time.time(), actor, tool, json.dumps(args, default=str)[:2000], outcome, detail[:500]))

    def audit_log(self, limit: int = 50) -> list[dict]:
        return self.query("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,))

    def close(self) -> None:
        with self._lock:
            self._conn.close()
