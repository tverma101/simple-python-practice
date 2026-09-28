from __future__ import annotations

import hashlib
import json
import logging
import re
import secrets
import shutil
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .paths import app_dir

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def generate_access_code() -> str:
    return f"{secrets.randbelow(10**9):09d}"


class ClassroomDB:
    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = db_path or (app_dir() / "teacher" / "classroom.db")
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.RLock()

    def _get_conn(self) -> sqlite3.Connection:
        with self._lock:
            if self._conn is None:
                self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
                self._conn.row_factory = sqlite3.Row
                self._conn.execute("PRAGMA foreign_keys = ON")
                self._create_tables()
        return self._conn

    def _create_tables(self) -> None:
        conn = self._conn
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS classes (
                class_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                access_code TEXT NOT NULL,
                llm_enabled INTEGER DEFAULT 0,
                llm_backend TEXT DEFAULT 'none',
                llm_model TEXT DEFAULT '',
                llm_url TEXT DEFAULT '',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS students (
                token TEXT PRIMARY KEY,
                class_id TEXT NOT NULL,
                name TEXT NOT NULL,
                connected_at TEXT NOT NULL,
                last_seen TEXT NOT NULL,
                ip_address TEXT,
                token_created_at TEXT,
                token_expires_at TEXT,
                revoked_at TEXT,
                FOREIGN KEY (class_id) REFERENCES classes(class_id)
            );

            CREATE TABLE IF NOT EXISTS packs (
                pack_id TEXT PRIMARY KEY,
                class_id TEXT NOT NULL,
                lesson_id TEXT NOT NULL,
                title TEXT NOT NULL,
                instructions TEXT DEFAULT '',
                version INTEGER DEFAULT 1,
                pack_sha256 TEXT DEFAULT '',
                pack_size INTEGER DEFAULT 0,
                pack_bytes BLOB,
                added_at TEXT NOT NULL,
                FOREIGN KEY (class_id) REFERENCES classes(class_id)
            );

            CREATE TABLE IF NOT EXISTS assignments (
                assignment_id TEXT PRIMARY KEY,
                class_id TEXT NOT NULL,
                pack_id TEXT NOT NULL,
                due_date TEXT,
                notes TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY (class_id) REFERENCES classes(class_id),
                FOREIGN KEY (pack_id) REFERENCES packs(pack_id)
            );

            CREATE TABLE IF NOT EXISTS pack_history (
                history_id INTEGER PRIMARY KEY AUTOINCREMENT,
                pack_id TEXT NOT NULL,
                class_id TEXT NOT NULL,
                lesson_id TEXT NOT NULL,
                title TEXT NOT NULL,
                instructions TEXT DEFAULT '',
                version INTEGER NOT NULL,
                pack_sha256 TEXT DEFAULT '',
                pack_size INTEGER DEFAULT 0,
                pack_bytes BLOB,
                added_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS submissions (
                submission_id TEXT PRIMARY KEY,
                token TEXT NOT NULL,
                class_id TEXT NOT NULL,
                pack_id TEXT,
                kind TEXT DEFAULT 'submission',
                report_json TEXT,
                student_code TEXT,
                status TEXT DEFAULT 'submitted',
                teacher_feedback TEXT DEFAULT '',
                submitted_at TEXT NOT NULL,
                FOREIGN KEY (token) REFERENCES students(token),
                FOREIGN KEY (class_id) REFERENCES classes(class_id),
                FOREIGN KEY (pack_id) REFERENCES packs(pack_id)
            );

            CREATE TABLE IF NOT EXISTS announcements (
                announcement_id TEXT PRIMARY KEY,
                class_id TEXT NOT NULL,
                message TEXT NOT NULL,
                seen_at TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY (class_id) REFERENCES classes(class_id)
            );
        """)
        self._migrate_tables(conn)
        conn.commit()

    def _migrate_tables(self, conn: sqlite3.Connection) -> None:
        """Add columns introduced after the initial schema without dropping data.

        Mirrors the additive-migration pattern used by ``progress.py`` so existing
        classroom databases (created before these columns existed) keep working.
        """
        migrations = {
            "submissions": {
                "kind": "TEXT DEFAULT 'submission'",
                "status": "TEXT DEFAULT 'submitted'",
                "teacher_feedback": "TEXT DEFAULT ''",
            },
            "classes": {"admin_token": "TEXT DEFAULT ''"},
            "students": {
                "token_created_at": "TEXT",
                "token_expires_at": "TEXT",
                "revoked_at": "TEXT",
                # Per-student access code. NULL for students provisioned the old
                # way (shared class code, token minted on connect). A partial
                # index below keeps the uniqueness constraint cheap and lets many
                # students share the class code without colliding.
                "access_code": "TEXT",
            },
            "packs": {
                "pack_sha256": "TEXT DEFAULT ''",
                "pack_size": "INTEGER DEFAULT 0",
            },
            "announcements": {"seen_at": "TEXT"},
        }
        for table, columns in migrations.items():
            existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
            for column, definition in columns.items():
                if column not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

        # A student access code must be unique across the whole deployment, not
        # just within a class: the code is what a student types on the LAN, with
        # no class selected, so two classes handing out the same code would make
        # the wrong class's roster receive the run. Partial (WHERE NOT NULL) so
        # that the many students with no code -- everyone still using the shared
        # class code -- do not collide with each other on NULL.
        conn.execute(
            """CREATE UNIQUE INDEX IF NOT EXISTS idx_students_access_code
               ON students(access_code) WHERE access_code IS NOT NULL"""
        )

        # classes.admin_token — separate, secret admin credential distinct from the
        # student access code (which is shared with every student in the class).
        # Backfill existing classes with a generated admin token so teachers are not
        # locked out of admin endpoints after an upgrade.
        for row in conn.execute("SELECT class_id, admin_token FROM classes").fetchall():
            if not row["admin_token"]:
                conn.execute(
                    "UPDATE classes SET admin_token = ? WHERE class_id = ?",
                    (secrets.token_hex(16), row["class_id"]),
                )

    # ── Classes ──────────────────────────────────────────

    def create_class(self, class_id: str, name: str, access_code: str | None = None) -> dict[str, Any] | None:
        code = access_code or generate_access_code()
        admin_token = secrets.token_hex(16)
        conn = self._get_conn()
        with self._lock:
            try:
                conn.execute(
                    "INSERT INTO classes (class_id, name, access_code, admin_token, created_at) VALUES (?, ?, ?, ?, ?)",
                    (class_id, name, code, admin_token, _now_iso()),
                )
                conn.commit()
            except sqlite3.IntegrityError as exc:
                raise ValueError(f"Class with id '{class_id}' already exists") from exc
        return {"class_id": class_id, "name": name, "access_code": code, "admin_token": admin_token}

    def list_classes(self) -> list[dict[str, Any]]:
        conn = self._get_conn()
        with self._lock:
            rows = conn.execute("SELECT * FROM classes ORDER BY created_at").fetchall()
        return [dict(r) for r in rows]

    def get_class(self, class_id: str) -> dict[str, Any] | None:
        conn = self._get_conn()
        with self._lock:
            row = conn.execute("SELECT * FROM classes WHERE class_id = ?", (class_id,)).fetchone()
        return dict(row) if row else None

    def get_class_by_code(self, access_code: str) -> dict[str, Any] | None:
        conn = self._get_conn()
        with self._lock:
            row = conn.execute("SELECT * FROM classes WHERE access_code = ?", (access_code,)).fetchone()
        return dict(row) if row else None

    def update_class_llm(
        self, class_id: str, llm_enabled: bool, llm_backend: str = "none", llm_model: str = "", llm_url: str = ""
    ) -> dict[str, Any] | None:
        conn = self._get_conn()
        with self._lock:
            conn.execute(
                "UPDATE classes SET llm_enabled = ?, llm_backend = ?, llm_model = ?, llm_url = ? WHERE class_id = ?",
                (1 if llm_enabled else 0, llm_backend, llm_model, llm_url, class_id),
            )
            conn.commit()
        return self.get_class(class_id)

    def get_admin_token(self, class_id: str) -> str | None:
        """Return the admin token for a class (used to authenticate teacher-only endpoints).

        Distinct from the student access code. Returns ``None`` if the class is missing.
        """
        cls = self.get_class(class_id)
        if not cls:
            return None
        return cls.get("admin_token") or None

    def delete_class(self, class_id: str) -> bool:
        conn = self._get_conn()
        with self._lock:
            for table in ("announcements", "submissions", "assignments", "pack_history", "packs", "students"):
                conn.execute(f"DELETE FROM {table} WHERE class_id = ?", (class_id,))
            cursor = conn.execute("DELETE FROM classes WHERE class_id = ?", (class_id,))
            conn.commit()
        return cursor.rowcount > 0

    # ── Students ─────────────────────────────────────────

    def connect_student(
        self,
        class_id: str,
        name: str,
        ip_address: str = "",
        session_ttl_seconds: int = 7 * 24 * 60 * 60,
    ) -> dict[str, Any]:
        token = secrets.token_hex(16)
        now = _now_iso()
        expires = datetime.now(timezone.utc).timestamp() + max(60, session_ttl_seconds)
        expires_at = datetime.fromtimestamp(expires, timezone.utc).isoformat()
        conn = self._get_conn()
        with self._lock:
            existing = conn.execute(
                "SELECT token FROM students WHERE class_id = ? AND name = ?", (class_id, name)
            ).fetchone()
            if existing:
                old_token = existing["token"]
                # SQLite foreign keys intentionally keep historical submissions
                # safe. Once a student has submitted, renew the existing token
                # instead of changing a referenced primary key.
                if conn.execute("SELECT 1 FROM submissions WHERE token = ? LIMIT 1", (old_token,)).fetchone():
                    token = old_token
                conn.execute(
                    """UPDATE students SET token = ?, last_seen = ?, ip_address = ?,
                       token_created_at = ?, token_expires_at = ?, revoked_at = NULL
                       WHERE class_id = ? AND name = ?""",
                    (token, now, ip_address, now, expires_at, class_id, name),
                )
                conn.commit()
                return {"token": token, "name": name, "class_id": class_id, "connected_at": now}
            conn.execute(
                """INSERT INTO students
                   (token, class_id, name, connected_at, last_seen, ip_address,
                    token_created_at, token_expires_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (token, class_id, name, now, now, ip_address, now, expires_at),
            )
            conn.commit()
        return {"token": token, "name": name, "class_id": class_id, "connected_at": now}

    def create_student(
        self,
        class_id: str,
        name: str,
        access_code: str | None = None,
    ) -> dict[str, Any]:
        """Pre-provision a student with their own access code.

        Why per-student rather than one code per class: the practice server is
        reachable from the local network and executes code on request, so "who ran
        this, and how much CPU did they burn" has to be answerable. A shared code
        makes both questions unanswerable, and lets one machine saturate the
        grader anonymously.

        The code is optional so an existing class that shares a single code keeps
        working unchanged; a student with no code is looked up by class code and
        name instead, exactly as before.
        """
        code = (access_code or generate_access_code()).strip()
        if not code:
            raise ValueError("access code must not be blank")
        if not name.strip():
            raise ValueError("student name must not be blank")

        conn = self._get_conn()
        if conn.execute("SELECT 1 FROM classes WHERE class_id = ?", (class_id,)).fetchone() is None:
            raise ValueError(f"unknown class: {class_id}")

        now = _now_iso()
        with self._lock:
            if conn.execute("SELECT 1 FROM students WHERE access_code = ?", (code,)).fetchone():
                raise ValueError("access code is already in use")
            # Reuse the student's existing row if they already connected, so
            # giving someone a code does not fork their submission history.
            existing = conn.execute(
                "SELECT token FROM students WHERE class_id = ? AND name = ?", (class_id, name)
            ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE students SET access_code = ? WHERE token = ?", (code, existing["token"])
                )
                token = existing["token"]
            else:
                token = secrets.token_hex(16)
                conn.execute(
                    """INSERT INTO students
                       (token, class_id, name, connected_at, last_seen, access_code)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (token, class_id, name, now, now, code),
                )
            conn.commit()
        return {"token": token, "name": name, "class_id": class_id, "access_code": code}

    def get_student_by_code(self, access_code: str) -> dict[str, Any] | None:
        """Resolve a typed access code to a student.

        Returns ``None`` for an unknown code and for a revoked student, so a
        revoked student cannot get in by presenting an old code.
        """
        code = (access_code or "").strip()
        if not code:
            return None
        conn = self._get_conn()
        with self._lock:
            row = conn.execute(
                "SELECT * FROM students WHERE access_code = ?", (code,)
            ).fetchone()
        if row is None or row["revoked_at"]:
            return None
        return dict(row)

    def list_access_codes(self, class_id: str) -> list[dict[str, Any]]:
        """Roster with codes, for a teacher to hand out. Codes are secrets."""
        conn = self._get_conn()
        with self._lock:
            rows = conn.execute(
                """SELECT name, access_code, revoked_at, last_seen
                   FROM students WHERE class_id = ?
                   ORDER BY name""",
                (class_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_student(self, token: str) -> dict[str, Any] | None:
        conn = self._get_conn()
        with self._lock:
            row = conn.execute("SELECT * FROM students WHERE token = ?", (token,)).fetchone()
        if not row:
            return None
        if row["revoked_at"]:
            return None
        expires_at = row["token_expires_at"]
        if expires_at:
            try:
                expiry = datetime.fromisoformat(expires_at)
                if expiry.tzinfo is None:
                    expiry = expiry.replace(tzinfo=timezone.utc)
                if expiry <= datetime.now(timezone.utc):
                    return None
            except (TypeError, ValueError):
                # Fail closed when persisted token metadata is malformed.
                logger.warning("Rejecting malformed token expiry for student token")
                return None
        return dict(row)

    def touch_student(self, token: str) -> None:
        conn = self._get_conn()
        with self._lock:
            conn.execute("UPDATE students SET last_seen = ? WHERE token = ?", (_now_iso(), token))
            conn.commit()

    def revoke_student(self, token: str, class_id: str | None = None) -> bool:
        """Revoke a student session without deleting its classroom history."""
        conn = self._get_conn()
        with self._lock:
            query = "UPDATE students SET revoked_at = ? WHERE token = ?"
            params: list[Any] = [_now_iso(), token]
            if class_id is not None:
                query += " AND class_id = ?"
                params.append(class_id)
            cursor = conn.execute(query, params)
            conn.commit()
        return cursor.rowcount > 0

    def list_students(self, class_id: str) -> list[dict[str, Any]]:
        conn = self._get_conn()
        with self._lock:
            rows = conn.execute(
                "SELECT * FROM students WHERE class_id = ? ORDER BY connected_at", (class_id,)
            ).fetchall()
        return [dict(r) for r in rows]

    # ── Packs ────────────────────────────────────────────

    def add_pack(
        self,
        class_id: str,
        lesson_id: str,
        title: str,
        pack_bytes: bytes,
        pack_id: str | None = None,
        instructions: str = "",
    ) -> dict[str, Any]:
        pid = pack_id or lesson_id
        if not pid or not re.fullmatch(r"[A-Za-z0-9_.-]+", pid):
            raise ValueError("pack_id must contain only letters, numbers, '.', '_' or '-'")
        pack_sha256 = hashlib.sha256(pack_bytes).hexdigest()
        conn = self._get_conn()
        with self._lock:
            existing = conn.execute(
                "SELECT * FROM packs WHERE pack_id = ?", (pid,)
            ).fetchone()
            if existing and existing["class_id"] != class_id:
                raise ValueError(f"Pack ID '{pid}' is already used by another class")
            version = (existing["version"] + 1) if existing else 1
            if existing:
                conn.execute(
                    """INSERT INTO pack_history
                       (pack_id, class_id, lesson_id, title, instructions, version,
                        pack_sha256, pack_size, pack_bytes, added_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (existing["pack_id"], existing["class_id"], existing["lesson_id"],
                     existing["title"], existing["instructions"], existing["version"],
                     existing["pack_sha256"] or "", existing["pack_size"] or 0,
                     existing["pack_bytes"], existing["added_at"]),
                )
                conn.execute(
                    """DELETE FROM pack_history WHERE pack_id = ? AND class_id = ?
                       AND history_id NOT IN (
                           SELECT history_id FROM pack_history
                           WHERE pack_id = ? AND class_id = ?
                           ORDER BY history_id DESC LIMIT 3
                       )""",
                    (pid, class_id, pid, class_id),
                )
                conn.execute(
                    """UPDATE packs SET lesson_id = ?, title = ?, instructions = ?, version = ?,
                       pack_sha256 = ?, pack_size = ?, pack_bytes = ?, added_at = ?
                       WHERE pack_id = ? AND class_id = ?""",
                    (lesson_id, title, instructions, version, pack_sha256, len(pack_bytes), pack_bytes,
                     _now_iso(), pid, class_id),
                )
            else:
                conn.execute(
                    """INSERT INTO packs
                       (pack_id, class_id, lesson_id, title, instructions, version,
                        pack_sha256, pack_size, pack_bytes, added_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (pid, class_id, lesson_id, title, instructions, version, pack_sha256,
                     len(pack_bytes), pack_bytes, _now_iso()),
                )
            conn.commit()
        return {
            "pack_id": pid,
            "lesson_id": lesson_id,
            "title": title,
            "instructions": instructions,
            "version": version,
            "sha256": pack_sha256,
            "size": len(pack_bytes),
        }

    def list_pack_history(self, pack_id: str, class_id: str) -> list[dict[str, Any]]:
        conn = self._get_conn()
        with self._lock:
            rows = conn.execute(
                "SELECT * FROM pack_history WHERE pack_id = ? AND class_id = ? ORDER BY history_id DESC",
                (pack_id, class_id),
            ).fetchall()
        return [dict(row) for row in rows]

    def rollback_pack(self, pack_id: str, class_id: str) -> dict[str, Any] | None:
        """Restore the most recently replaced pack version."""
        conn = self._get_conn()
        with self._lock:
            previous = conn.execute(
                """SELECT * FROM pack_history WHERE pack_id = ? AND class_id = ?
                   ORDER BY history_id DESC LIMIT 1""",
                (pack_id, class_id),
            ).fetchone()
            if not previous:
                return None
            current = conn.execute(
                "SELECT * FROM packs WHERE pack_id = ? AND class_id = ?", (pack_id, class_id)
            ).fetchone()
            if current:
                conn.execute(
                    """INSERT INTO pack_history
                       (pack_id, class_id, lesson_id, title, instructions, version,
                        pack_sha256, pack_size, pack_bytes, added_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (current["pack_id"], current["class_id"], current["lesson_id"],
                     current["title"], current["instructions"], current["version"],
                     current["pack_sha256"] or "", current["pack_size"] or 0,
                     current["pack_bytes"], current["added_at"]),
                )
            conn.execute(
                """UPDATE packs SET lesson_id = ?, title = ?, instructions = ?, version = ?,
                   pack_sha256 = ?, pack_size = ?, pack_bytes = ?, added_at = ?
                   WHERE pack_id = ? AND class_id = ?""",
                (previous["lesson_id"], previous["title"], previous["instructions"], previous["version"],
                 previous["pack_sha256"], previous["pack_size"], previous["pack_bytes"], _now_iso(),
                 pack_id, class_id),
            )
            conn.commit()
            return dict(
                conn.execute(
                    "SELECT * FROM packs WHERE pack_id = ? AND class_id = ?",
                    (pack_id, class_id),
                ).fetchone()
            )

    def get_pack(self, pack_id: str, class_id: str) -> dict[str, Any] | None:
        conn = self._get_conn()
        with self._lock:
            row = conn.execute("SELECT * FROM packs WHERE pack_id = ? AND class_id = ?", (pack_id, class_id)).fetchone()
        return dict(row) if row else None

    def list_packs(self, class_id: str) -> list[dict[str, Any]]:
        conn = self._get_conn()
        with self._lock:
            rows = conn.execute(
                """SELECT pack_id, class_id, lesson_id, title, instructions, version,
                          pack_sha256, pack_size, added_at
                   FROM packs WHERE class_id = ? ORDER BY added_at""",
                (class_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def delete_pack(self, pack_id: str, class_id: str) -> bool:
        conn = self._get_conn()
        with self._lock:
            conn.execute("DELETE FROM assignments WHERE pack_id = ? AND class_id = ?", (pack_id, class_id))
            conn.execute("DELETE FROM pack_history WHERE pack_id = ? AND class_id = ?", (pack_id, class_id))
            cursor = conn.execute("DELETE FROM packs WHERE pack_id = ? AND class_id = ?", (pack_id, class_id))
            conn.commit()
        return cursor.rowcount > 0

    # ── Assignments ──────────────────────────────────────

    def add_assignment(
        self, class_id: str, pack_id: str, due_date: str = "", notes: str = "", assignment_id: str | None = None
    ) -> dict[str, Any]:
        aid = assignment_id or secrets.token_hex(8)
        conn = self._get_conn()
        with self._lock:
            if not conn.execute(
                "SELECT 1 FROM packs WHERE pack_id = ? AND class_id = ?", (pack_id, class_id)
            ).fetchone():
                raise sqlite3.IntegrityError("assignment pack does not belong to class")
            conn.execute(
                "INSERT INTO assignments (assignment_id, class_id, pack_id, due_date, notes, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (aid, class_id, pack_id, due_date, notes, _now_iso()),
            )
            conn.commit()
        return {"assignment_id": aid, "pack_id": pack_id, "due_date": due_date, "notes": notes}

    def list_assignments(self, class_id: str) -> list[dict[str, Any]]:
        conn = self._get_conn()
        with self._lock:
            rows = conn.execute(
                """SELECT a.*, p.title as pack_title, p.lesson_id
                   FROM assignments a
                   LEFT JOIN packs p ON a.pack_id = p.pack_id AND a.class_id = p.class_id
                   WHERE a.class_id = ?
                   ORDER BY a.created_at""",
                (class_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def delete_assignment(self, assignment_id: str, class_id: str) -> bool:
        conn = self._get_conn()
        with self._lock:
            cursor = conn.execute(
                "DELETE FROM assignments WHERE assignment_id = ? AND class_id = ?", (assignment_id, class_id)
            )
            conn.commit()
        return cursor.rowcount > 0

    # ── Submissions ──────────────────────────────────────

    def add_submission(
        self,
        token: str,
        class_id: str,
        pack_id: str,
        report_json: str,
        student_code: str = "",
        kind: str = "submission",
        submission_id: str | None = None,
        status: str = "submitted",
        teacher_feedback: str = "",
    ) -> dict[str, Any]:
        sid = submission_id or secrets.token_hex(8)
        # Progress pings and quiz results are decoupled from a specific pack: store
        # NULL (FK-safe) rather than requiring the pack to exist, so they never 404.
        db_pack_id = pack_id if pack_id else None
        conn = self._get_conn()
        with self._lock:
            student = conn.execute(
                "SELECT class_id FROM students WHERE token = ?", (token,)
            ).fetchone()
            if not student or student["class_id"] != class_id:
                raise sqlite3.IntegrityError("student token does not belong to class")
            if db_pack_id and not conn.execute(
                "SELECT 1 FROM packs WHERE pack_id = ? AND class_id = ?", (db_pack_id, class_id)
            ).fetchone():
                raise sqlite3.IntegrityError("submission pack does not belong to class")
            existing = conn.execute(
                "SELECT * FROM submissions WHERE submission_id = ?", (sid,)
            ).fetchone()
            if existing:
                # Idempotent retries are allowed only for the same student and
                # class. Never return another student's report/code to a caller
                # who guesses or reuses a submission ID.
                if existing["token"] != token or existing["class_id"] != class_id:
                    raise sqlite3.IntegrityError("submission ID belongs to another student or class")
                return dict(existing)
            conn.execute(
                """INSERT INTO submissions
                   (submission_id, token, class_id, pack_id, kind, report_json,
                    student_code, status, teacher_feedback, submitted_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (sid, token, class_id, db_pack_id, kind, report_json, student_code,
                 status, teacher_feedback, _now_iso()),
            )
            conn.commit()
        return {"submission_id": sid, "pack_id": pack_id, "kind": kind, "status": status, "submitted_at": _now_iso()}

    def get_submission(self, submission_id: str, class_id: str | None = None) -> dict[str, Any] | None:
        conn = self._get_conn()
        with self._lock:
            query = "SELECT * FROM submissions WHERE submission_id = ?"
            params: list[Any] = [submission_id]
            if class_id is not None:
                query += " AND class_id = ?"
                params.append(class_id)
            row = conn.execute(query, params).fetchone()
        return dict(row) if row else None

    def update_submission_review(
        self,
        submission_id: str,
        status: str,
        feedback: str = "",
        class_id: str | None = None,
    ) -> bool:
        allowed = {"submitted", "pass", "needs_work", "resubmit"}
        if status not in allowed:
            raise ValueError(f"status must be one of: {', '.join(sorted(allowed))}")
        conn = self._get_conn()
        with self._lock:
            query = "UPDATE submissions SET status = ?, teacher_feedback = ? WHERE submission_id = ?"
            params: list[Any] = [status, feedback, submission_id]
            if class_id is not None:
                query += " AND class_id = ?"
                params.append(class_id)
            cursor = conn.execute(query, params)
            conn.commit()
        return cursor.rowcount > 0

    def list_submissions(
        self, class_id: str, pack_id: str | None = None, kind: str | None = None
    ) -> list[dict[str, Any]]:
        conn = self._get_conn()
        with self._lock:
            query = (
                """SELECT s.*, st.name as student_name
                   FROM submissions s
                   LEFT JOIN students st ON s.token = st.token
                   WHERE s.class_id = ?"""
            )
            params: list[Any] = [class_id]
            if pack_id:
                query += " AND s.pack_id = ?"
                params.append(pack_id)
            if kind:
                query += " AND s.kind = ?"
                params.append(kind)
            query += " ORDER BY s.submitted_at"
            rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    def list_student_submissions(self, token: str, class_id: str) -> list[dict[str, Any]]:
        conn = self._get_conn()
        with self._lock:
            rows = conn.execute(
                """SELECT s.submission_id, s.pack_id, s.kind, s.status,
                          s.teacher_feedback, s.submitted_at, s.report_json,
                          p.title AS pack_title, p.lesson_id AS pack_lesson_id
                   FROM submissions s
                   LEFT JOIN packs p ON s.pack_id = p.pack_id AND s.class_id = p.class_id
                   WHERE s.token = ? AND s.class_id = ? ORDER BY s.submitted_at DESC""",
                (token, class_id),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_submission_stats(self, class_id: str, pack_id: str) -> dict[str, Any]:
        conn = self._get_conn()
        with self._lock:
            total = conn.execute(
                "SELECT COUNT(*) as cnt FROM submissions WHERE class_id = ? AND pack_id = ?", (class_id, pack_id)
            ).fetchone()["cnt"]
            passed = 0
            rows = conn.execute(
                "SELECT report_json FROM submissions WHERE class_id = ? AND pack_id = ?", (class_id, pack_id)
            ).fetchall()
        for row in rows:
            try:
                report = json.loads(row["report_json"])
                if report.get("passed"):
                    passed += 1
            except (json.JSONDecodeError, TypeError) as exc:
                logger.debug("Malformed report_json in submission: %s", exc)
                continue
        return {"total": total, "passed": passed, "failed": total - passed}

    # ── Announcements ────────────────────────────────────

    def add_announcement(self, class_id: str, message: str) -> dict[str, Any]:
        aid = secrets.token_hex(8)
        conn = self._get_conn()
        with self._lock:
            conn.execute(
                "INSERT INTO announcements (announcement_id, class_id, message, created_at) VALUES (?, ?, ?, ?)",
                (aid, class_id, message, _now_iso()),
            )
            conn.commit()
        return {"announcement_id": aid, "message": message}

    def list_announcements(self, class_id: str) -> list[dict[str, Any]]:
        conn = self._get_conn()
        with self._lock:
            rows = conn.execute(
                "SELECT * FROM announcements WHERE class_id = ? ORDER BY created_at DESC", (class_id,)
            ).fetchall()
        return [dict(r) for r in rows]

    def mark_announcement_seen(self, announcement_id: str, class_id: str) -> bool:
        conn = self._get_conn()
        with self._lock:
            cursor = conn.execute(
                "UPDATE announcements SET seen_at = ? WHERE announcement_id = ? AND class_id = ?",
                (_now_iso(), announcement_id, class_id),
            )
            conn.commit()
        return cursor.rowcount > 0

    def delete_announcement(self, announcement_id: str, class_id: str) -> bool:
        conn = self._get_conn()
        with self._lock:
            cursor = conn.execute(
                "DELETE FROM announcements WHERE announcement_id = ? AND class_id = ?",
                (announcement_id, class_id),
            )
            conn.commit()
        return cursor.rowcount > 0

    # ── Utilities ────────────────────────────────────────

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    def backup_to(self, destination: Path | str) -> Path:
        """Create a consistent copy of the classroom database."""
        destination = Path(destination).expanduser().resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        conn = self._get_conn()
        with self._lock:
            destination_tmp = destination.with_suffix(destination.suffix + ".tmp")
            backup_conn = sqlite3.connect(str(destination_tmp))
            try:
                conn.backup(backup_conn)
            finally:
                backup_conn.close()
            destination_tmp.replace(destination)
        return destination

    def restore_from(self, source: Path | str) -> Path:
        """Replace this database from a backup after closing the active connection."""
        source = Path(source).expanduser().resolve()
        if not source.is_file():
            raise FileNotFoundError(f"Backup not found: {source}")
        self.close()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.db_path.with_suffix(".restore.tmp")
        shutil.copy2(source, temp)
        temp.replace(self.db_path)
        return self.db_path

    def export_class_data(self, class_id: str) -> list[dict[str, Any]]:
        conn = self._get_conn()
        with self._lock:
            rows = conn.execute(
                """SELECT s.submitted_at, s.pack_id, s.report_json, s.student_code,
                          st.name as student_name, p.lesson_id, p.title as pack_title
                   FROM submissions s
                   LEFT JOIN students st ON s.token = st.token
                   LEFT JOIN packs p ON s.pack_id = p.pack_id AND s.class_id = p.class_id
                   WHERE s.class_id = ?
                   ORDER BY s.submitted_at""",
                (class_id,),
            ).fetchall()
        return [dict(r) for r in rows]
