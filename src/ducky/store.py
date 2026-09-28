"""Persistence the practice server needs and was missing.

Two stores, both SQLite in the same file as the library would be over-coupled, so
each is its own module-level class with its own path:

:class:`RunLog`
    Who ran what, when, and whether it passed. The server's stated design is that
    a run is attributable to a person, and that a per-IP rate limit protects the
    machine. Neither claim survives without somewhere to put the run: the previous
    version plumbed a student token all the way into the grade handler and then
    dropped it on the floor.

    Kept deliberately thin. It answers "who ran this and did it pass", which is
    what a teacher reviewing a class needs, and nothing more -- no code is stored,
    because the server does not keep student submissions.

:class:`DraftStore`
    Autosaved code per student per problem.

    This exists because the alternative is silent data loss. The previous UI had
    a three-state save indicator and a ``beforeunload`` guard, and neither could
    ever fire, because the attribute the route read was never assigned -- so the
    server answered ``{"saved": false}`` and the client, checking only the HTTP
    status, displayed "Saved".
"""

from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = ["RunLog", "DraftStore", "RunRecord"]


_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    student_token  TEXT NOT NULL,
    slug           TEXT NOT NULL,
    client_ip      TEXT,
    passed         INTEGER NOT NULL,
    cases_passed   INTEGER NOT NULL DEFAULT 0,
    cases_total    INTEGER NOT NULL DEFAULT 0,
    elapsed_ms     INTEGER,
    at             REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_runs_student ON runs(student_token, at);
CREATE INDEX IF NOT EXISTS idx_runs_slug ON runs(slug, at);

CREATE TABLE IF NOT EXISTS drafts (
    student_token  TEXT NOT NULL,
    slug           TEXT NOT NULL,
    code           TEXT NOT NULL,
    updated_at     REAL NOT NULL,
    PRIMARY KEY (student_token, slug)
);
"""


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    # A grading request is a write on a machine that is also serving a LAN, so
    # WAL and a busy timeout rather than an unhandled "database is locked".
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


@dataclass
class RunRecord:
    student_token: str
    slug: str
    passed: bool
    cases_passed: int
    cases_total: int
    ip: str = ""
    elapsed_ms: int | None = None
    at: float = 0.0


class RunLog:
    """Append-only record of grading runs."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._conn = _connect(self.path)
        self._lock = threading.Lock()

    def record(
        self,
        *,
        student_token: str,
        slug: str,
        passed: bool,
        cases: int,
        total: int,
        ip: str = "",
        elapsed: float | None = None,
    ) -> None:
        if not student_token:
            # An unattributed run defeats the point of the log, so record it as
            # anonymous rather than pretending it was a student.
            student_token = "anonymous"
        row = (
            student_token, slug, ip, int(bool(passed)), int(cases), int(total),
            None if elapsed is None else int(elapsed * 1000), time.time(),
        )
        with self._lock:
            self._conn.execute(
                """INSERT INTO runs
                   (student_token, slug, client_ip, passed, cases_passed,
                    cases_total, elapsed_ms, at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                row,
            )
            self._conn.commit()

    def for_student(self, student_token: str, *, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """SELECT slug, passed, cases_passed, cases_total, at
                   FROM runs WHERE student_token = ?
                   ORDER BY at DESC LIMIT ?""",
                (student_token, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def attempts_on(self, slug: str, *, limit: int = 200) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) n FROM runs WHERE slug = ?", (slug,)
            ).fetchone()
        return int(row["n"])

    def close(self) -> None:
        with self._lock:
            self._conn.close()


class DraftStore:
    """Autosaved code, one row per student per problem.

    Student code is stored in the clear. That is a real exposure on a shared
    machine, and it is the same trade the rest of this project makes: the
    platform is local-first, nothing is uploaded, and the data stays in a file
    the user can delete. It is called out in PRIVACY.md rather than hidden here.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._conn = _connect(self.path)
        self._lock = threading.Lock()

    def save(self, student_token: str, slug: str, code: str) -> None:
        if not student_token:
            raise ValueError("a draft needs an identified student")
        with self._lock:
            self._conn.execute(
                """INSERT INTO drafts (student_token, slug, code, updated_at)
                   VALUES (?,?,?,?)
                   ON CONFLICT(student_token, slug)
                   DO UPDATE SET code = excluded.code, updated_at = excluded.updated_at""",
                (student_token, slug, code, time.time()),
            )
            self._conn.commit()

    def load(self, student_token: str, slug: str) -> str:
        if not student_token:
            return ""
        with self._lock:
            row = self._conn.execute(
                "SELECT code FROM drafts WHERE student_token = ? AND slug = ?",
                (student_token, slug),
            ).fetchone()
        return row["code"] if row else ""

    def clear(self, student_token: str, slug: str) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM drafts WHERE student_token = ? AND slug = ?",
                (student_token, slug),
            )
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()
