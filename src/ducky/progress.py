from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from .paths import app_dir


class ProgressTracker:
    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = db_path or (app_dir() / "progress.db")
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.RLock()

    def _get_conn(self) -> sqlite3.Connection:
        with self._lock:
            if self._conn is None:
                self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
                self._conn.execute("""
                    CREATE TABLE IF NOT EXISTS attempts (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        lesson_id TEXT NOT NULL,
                        passed INTEGER NOT NULL,
                        diagnosis TEXT,
                        timestamp TEXT NOT NULL,
                        synced INTEGER DEFAULT 0
                    )
                """)
                self._conn.execute("CREATE INDEX IF NOT EXISTS idx_attempts_lesson ON attempts(lesson_id)")
                self._conn.commit()
                try:
                    self._conn.execute("SELECT synced FROM attempts LIMIT 1")
                except sqlite3.OperationalError:
                    self._conn.execute("ALTER TABLE attempts ADD COLUMN synced INTEGER DEFAULT 0")
                    self._conn.commit()
            return self._conn

    def record_attempt(self, lesson_id: str, passed: bool, diagnosis: str = "") -> None:
        with self._lock:
            conn = self._get_conn()
            conn.execute(
                "INSERT INTO attempts (lesson_id, passed, diagnosis, timestamp) VALUES (?, ?, ?, ?)",
                (lesson_id, int(passed), diagnosis, datetime.now(timezone.utc).isoformat()),
            )
            conn.commit()

    def get_lesson_stats(self, lesson_id: str) -> dict:
        with self._lock:
            conn = self._get_conn()
            rows = conn.execute(
                "SELECT passed, COUNT(*) FROM attempts WHERE lesson_id = ? GROUP BY passed",
                (lesson_id,),
            ).fetchall()
        stats = {"total": 0, "passed": 0, "failed": 0}
        for row_passed, count in rows:
            stats["total"] += count
            if row_passed:
                stats["passed"] = count
            else:
                stats["failed"] = count
        return stats

    def get_unsynced_attempts(self) -> list[dict]:
        """Get all attempts that haven't been synced to the teacher server."""
        with self._lock:
            conn = self._get_conn()
            rows = conn.execute(
                "SELECT id, lesson_id, passed, diagnosis, timestamp FROM attempts WHERE synced = 0 ORDER BY id"
            ).fetchall()
        return [
            {"id": r[0], "lesson_id": r[1], "passed": bool(r[2]), "diagnosis": r[3] or "", "timestamp": r[4]}
            for r in rows
        ]

    def mark_attempt_synced(self, attempt_ids: list[int]) -> None:
        """Mark attempts as synced to prevent re-sending."""
        if not attempt_ids:
            return
        with self._lock:
            conn = self._get_conn()
            placeholders = ",".join("?" * len(attempt_ids))
            conn.execute(
                f"UPDATE attempts SET synced = 1 WHERE id IN ({placeholders})",
                attempt_ids,
            )
            conn.commit()

    def get_progress_summary(self) -> dict:
        """Get summary of all lessons for progress sync."""
        with self._lock:
            conn = self._get_conn()
            rows = conn.execute(
                """SELECT lesson_id,
                          COUNT(*) as total,
                          SUM(CASE WHEN passed = 1 THEN 1 ELSE 0 END) as passed,
                          MIN(timestamp) as first_attempt,
                          MAX(timestamp) as last_attempt
                   FROM attempts
                   GROUP BY lesson_id"""
            ).fetchall()
        return {
            r[0]: {
                "total": r[1],
                "passed": r[2],
                "failed": r[1] - r[2],
                "first_attempt": r[3],
                "last_attempt": r[4],
            }
            for r in rows
        }

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None
