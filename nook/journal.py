"""Local SQLite journal: audit log, outbox for events, used request ids.

Every event is written here before it is published, so nothing is lost while
the cabinet is offline; the outbox is flushed when the broker comes back.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time


class Journal:
    def __init__(self, path: str):
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._lock = threading.Lock()
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=NORMAL")
            self._db.executescript("""
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts REAL NOT NULL,
                    topic TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    sent INTEGER NOT NULL DEFAULT 0);
                CREATE INDEX IF NOT EXISTS events_unsent ON events(sent, id);
                CREATE TABLE IF NOT EXISTS seen (
                    ref TEXT PRIMARY KEY,
                    expires REAL NOT NULL);
            """)

    # outbox / audit log
    def record(self, topic: str, payload: dict) -> int:
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO events (ts, topic, payload) VALUES (?, ?, ?)",
                (time.time(), topic, json.dumps(payload, separators=(",", ":"))))
            return int(cur.lastrowid)

    def pending(self, limit: int = 200) -> list[tuple[int, str, dict]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT id, topic, payload FROM events WHERE sent = 0 ORDER BY id LIMIT ?",
                (limit,)).fetchall()
        return [(r[0], r[1], json.loads(r[2])) for r in rows]

    def pending_count(self) -> int:
        with self._lock:
            return self._db.execute("SELECT COUNT(*) FROM events WHERE sent = 0").fetchone()[0]

    def mark_sent(self, event_id: int) -> None:
        with self._lock:
            self._db.execute("UPDATE events SET sent = 1 WHERE id = ?", (event_id,))

    def recent(self, limit: int = 50) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT id, ts, topic, payload, sent FROM events ORDER BY id DESC LIMIT ?",
                (limit,)).fetchall()
        return [{"id": r[0], "ts": r[1], "topic": r[2], "payload": json.loads(r[3]),
                 "sent": bool(r[4])} for r in rows]

    # replay protection
    def remember(self, ref: str, expires: float) -> bool:
        """Store a single-use id. False if it was already used."""
        with self._lock:
            try:
                self._db.execute("INSERT INTO seen (ref, expires) VALUES (?, ?)", (ref, expires))
                return True
            except sqlite3.IntegrityError:
                return False

    def prune(self, keep_days: int = 90) -> None:
        now = time.time()
        with self._lock:
            self._db.execute("DELETE FROM seen WHERE expires < ?", (now,))
            self._db.execute("DELETE FROM events WHERE sent = 1 AND ts < ?",
                             (now - keep_days * 86400,))

    def close(self) -> None:
        with self._lock:
            self._db.close()
