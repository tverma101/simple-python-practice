"""Ducky Teacher-Student Server.

This module provides the HTTP server for teacher-student communication.
The server handles both student-facing and teacher-only endpoints, organized
into clear sections for maintainability.
"""

from __future__ import annotations

import json
import logging
import math
import re
import socket
import socketserver
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler
from typing import Any
from urllib.parse import urlparse

from . import __version__
from .classroom import ClassroomDB, generate_access_code
from .config import build_llm_client
from .llm_client import HintRequest

logger = logging.getLogger(__name__)

DEFAULT_PORT = 8420


class _RateLimiter:
    def __init__(self, max_attempts: int = 10, window_seconds: int = 60) -> None:
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self._attempts: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def check(self, key: str) -> bool:
        import time

        now = time.time()
        with self._lock:
            # This is a local 1:1 service, but malformed clients should not be
            # able to grow the key map forever.
            if len(self._attempts) > 1024:
                self._attempts = {
                    stored_key: stored_attempts
                    for stored_key, stored_attempts in self._attempts.items()
                    if stored_attempts and now - stored_attempts[-1] < self.window_seconds
                }
                if len(self._attempts) > 1024:
                    # Keep the most recently active keys. Without a hard cap,
                    # many one-off LAN clients could keep the limiter map
                    # growing even after stale entries were pruned.
                    recent = sorted(
                        self._attempts.items(),
                        key=lambda item: item[1][-1],
                        reverse=True,
                    )[:1024]
                    self._attempts = dict(recent)
            attempts = self._attempts.get(key, [])
            attempts = [t for t in attempts if now - t < self.window_seconds]
            if len(attempts) >= self.max_attempts:
                return False
            attempts.append(now)
            self._attempts[key] = attempts
            return True


class DuckyHandler(BaseHTTPRequestHandler):
    """HTTP request handler for Ducky teacher-student server.

    Endpoints are organized into two sections:
    - Student endpoints: /api/connect, /api/packs, /api/submit, /api/hint, etc.
    - Teacher endpoints: /api/teacher/students, /api/teacher/submissions, etc.
    """

    db: ClassroomDB
    current_class_id: str
    rate_limiter: _RateLimiter
    request_rate_limiter: _RateLimiter
    llm_config: dict[str, Any]
    server_version = __version__

    def log_message(self, format: str, *args: Any) -> None:
        """Log HTTP request info — enabled so teachers can diagnose connection issues."""
        logger.info("HTTP %s %s - %s", self.command, self.path, format % args if args else format)

    # ── Shared Helpers ───────────────────────────────────────

    def _send_json(self, data: Any, status: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _send_error_json(self, status: int, message: str) -> None:
        logger.info("Error %d %s for %s %s", status, message, self.command, self.path)
        self._send_json({"error": message}, status)

    _MAX_BODY = 10 * 1024 * 1024  # 10 MB

    def _read_body(self) -> bytes:
        raw_length = self.headers.get("Content-Length", "0")
        try:
            length = int(raw_length)
        except (ValueError, TypeError):
            logger.warning("Invalid Content-Length header: %r", raw_length)
            self._body_error = True
            self._send_error_json(400, "Invalid Content-Length header")
            return b""
        if length < 0:
            self._body_error = True
            self._send_error_json(400, "Invalid request body length")
            return b""
        if length > self._MAX_BODY:
            logger.warning("Request body too large: %d bytes (max %d)", length, self._MAX_BODY)
            self._body_error = True
            self._send_error_json(413, "Request body is too large")
            return b""
        return self.rfile.read(length) if length > 0 else b""

    def _read_json(self) -> dict[str, Any]:
        body = self._read_body()
        if getattr(self, "_body_error", False) or not body:
            return {}
        try:
            data = json.loads(body)
        except json.JSONDecodeError as exc:
            logger.warning("JSON decode error in %s %s: %s", self.command, self.path, exc)
            self._body_error = True
            self._send_error_json(400, "Request body must be valid JSON")
            return {}
        if not isinstance(data, dict):
            self._body_error = True
            self._send_error_json(400, "Request body must be a JSON object")
            return {}
        return data

    def _get_token(self) -> str | None:
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            return auth[7:].strip()
        return None

    def _require_token(self) -> dict[str, Any] | None:
        token = self._get_token()
        if not token:
            self._send_error_json(401, "Missing authorization token")
            return None
        student = self.db.get_student(token)
        if not student:
            self._send_error_json(401, "Invalid token")
            return None
        if student["class_id"] != self.current_class_id:
            self._send_error_json(403, "Token not valid for this class")
            return None
        if not self.request_rate_limiter.check(token):
            self._send_error_json(429, "Too many requests. Try again shortly.")
            return None
        self.db.touch_student(token)
        return student

    def do_OPTIONS(self) -> None:
        """Handle CORS preflight requests."""
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Access-Control-Max-Age", "86400")
        self.end_headers()

    # ── Routes ───────────────────────────────────────────────

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")

        # Health check (no auth)
        if path == "/api/health":
            self._send_json({"ok": True, "version": self.server_version})
            return

        # Student endpoints
        if path == "/api/status":
            return self._handle_status()
        if path == "/api/packs":
            return self._handle_list_packs()
        if path.startswith("/api/packs/") and path.endswith("/download"):
            pack_id = path.split("/")[3]
            return self._handle_download_pack(pack_id)
        if path == "/api/assignments":
            return self._handle_list_assignments()
        if path == "/api/announcements":
            return self._handle_list_announcements()
        if path == "/api/feedback":
            return self._handle_feedback()
        if path == "/api/llm-status":
            return self._handle_llm_status()

        # Teacher endpoints
        if path == "/api/teacher/students":
            return self._handle_teacher_students()
        if path == "/api/teacher/submissions":
            return self._handle_teacher_submissions()

        self._send_error_json(404, "Not found")

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")

        # Student endpoints
        if path == "/api/connect":
            return self._handle_connect()
        if path == "/api/submit":
            return self._handle_submit()
        if path == "/api/progress":
            return self._handle_progress()
        if path == "/api/quiz-result":
            return self._handle_quiz_result()
        if path == "/api/hint":
            return self._handle_hint()
        if path == "/api/disconnect":
            return self._handle_disconnect()
        if path.startswith("/api/announcements/") and path.endswith("/seen"):
            return self._handle_announcement_seen(path.split("/")[3])

        # Teacher endpoints
        if path == "/api/teacher/announce":
            return self._handle_teacher_announce()
        if path == "/api/teacher/packs":
            return self._handle_teacher_add_pack()
        if path.startswith("/api/teacher/packs/") and path.endswith("/rollback"):
            return self._handle_teacher_rollback_pack(path.split("/")[4])
        if path.startswith("/api/teacher/submissions/") and path.endswith("/review"):
            return self._handle_teacher_review(path.split("/")[4])

        self._send_error_json(404, "Not found")

    def do_DELETE(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")

        # Teacher endpoints
        if path.startswith("/api/teacher/packs/"):
            pack_id = path.split("/")[4]
            return self._handle_teacher_remove_pack(pack_id)
        if path.startswith("/api/teacher/students/"):
            return self._handle_teacher_revoke_student(path.split("/")[4])

        self._send_error_json(404, "Not found")

    # ═══════════════════════════════════════════════════════════════
    # Student Endpoints
    # ═══════════════════════════════════════════════════════════════

    def _handle_connect(self) -> None:
        """Student connects with access code."""
        ip = self.client_address[0]
        if not self.rate_limiter.check(ip):
            self._send_error_json(429, "Too many connection attempts. Try again later.")
            return

        data = self._read_json()
        if getattr(self, "_body_error", False):
            return
        name_value = data.get("name", "")
        code_value = data.get("code", "")
        if not isinstance(name_value, str) or not isinstance(code_value, str):
            self._send_error_json(400, "name and code must be text")
            return
        name = name_value.strip()
        code = code_value.strip()

        if not name:
            self._send_error_json(400, "Name is required")
            return
        if not code:
            self._send_error_json(400, "Access code is required")
            return

        target_class = self.db.get_class_by_code(code)
        if not target_class:
            self._send_error_json(401, "Invalid access code")
            return

        result = self.db.connect_student(target_class["class_id"], name, ip)
        self._send_json(
            {
                "token": result["token"],
                "server_version": self.server_version,
                "class_name": target_class["name"],
                "class_id": target_class["class_id"],
            }
        )

    def _handle_disconnect(self) -> None:
        student = self._require_token()
        if student is None:
            return
        self.db.revoke_student(student["token"])
        self._send_json({"ok": True, "status": "disconnected"})

    def _handle_status(self) -> None:
        """Server status for connected students."""
        student = self._require_token()
        if student is None:
            return

        students = self.db.list_students(self.current_class_id)
        packs = self.db.list_packs(self.current_class_id)
        assignments = self.db.list_assignments(self.current_class_id)
        announcements = self.db.list_announcements(self.current_class_id)

        self._send_json(
            {
                "server_version": self.server_version,
                "class_id": self.current_class_id,
                "student_count": len(students),
                "pack_count": len(packs),
                "assignment_count": len(assignments),
                "announcement_count": len(announcements),
                "connection": {
                    "state": "connected",
                    "student_name": student.get("name", ""),
                    "last_seen": student.get("last_seen", ""),
                    "token_expires_at": student.get("token_expires_at", ""),
                },
            }
        )

    def _handle_list_packs(self) -> None:
        """List available packs for students."""
        student = self._require_token()
        if student is None:
            return

        packs = self.db.list_packs(self.current_class_id)
        self._send_json({"packs": packs})

    def _handle_download_pack(self, pack_id: str) -> None:
        """Download a pack file."""
        student = self._require_token()
        if student is None:
            return

        pack = self.db.get_pack(pack_id, self.current_class_id)
        if not pack:
            self._send_error_json(404, f"Pack not found: {pack_id}")
            return

        pack_bytes = pack.get("pack_bytes")
        if not pack_bytes:
            self._send_error_json(404, "Pack data not available")
            return

        safe_name = re.sub(r"[^a-zA-Z0-9_\-\.]", "_", pack_id)

        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(pack_bytes)))
        self.send_header("Content-Disposition", f'attachment; filename="{safe_name}.duckypack"')
        if pack.get("pack_sha256"):
            self.send_header("X-Ducky-SHA256", pack["pack_sha256"])
        self.end_headers()
        self.wfile.write(pack_bytes)

    def _handle_list_assignments(self) -> None:
        """List assignments for students."""
        student = self._require_token()
        if student is None:
            return

        assignments = self.db.list_assignments(self.current_class_id)
        self._send_json({"assignments": assignments})

    def _handle_list_announcements(self) -> None:
        """List announcements for students."""
        student = self._require_token()
        if student is None:
            return

        announcements = self.db.list_announcements(self.current_class_id)
        self._send_json({"announcements": announcements})

    def _handle_feedback(self) -> None:
        student = self._require_token()
        if student is None:
            return
        self._send_json({"submissions": self.db.list_student_submissions(student["token"], self.current_class_id)})

    def _handle_submit(self) -> None:
        """Student submits work."""
        student = self._require_token()
        if student is None:
            return

        data = self._read_json()
        if getattr(self, "_body_error", False):
            return
        pack_id_value = data.get("pack_id", "")
        if not isinstance(pack_id_value, str):
            self._send_error_json(400, "pack_id must be text")
            return
        pack_id = pack_id_value.strip()
        report_json = data.get("report_json", "")
        student_code = data.get("student_code", "")
        submission_id = data.get("submission_id")

        if not pack_id:
            self._send_error_json(400, "pack_id is required")
            return
        if submission_id is not None and (
            not isinstance(submission_id, str) or not submission_id.strip() or len(submission_id) > 200
        ):
            self._send_error_json(400, "submission_id must be non-empty text up to 200 characters")
            return

        if isinstance(report_json, dict):
            report_json = json.dumps(report_json, ensure_ascii=False)
        if not isinstance(report_json, str) or len(report_json.encode("utf-8")) > 2 * 1024 * 1024:
            self._send_error_json(413, "Report is too large")
            return
        if not isinstance(student_code, str) or len(student_code.encode("utf-8")) > 2 * 1024 * 1024:
            self._send_error_json(413, "Student code is too large")
            return

        # Validate that the pack exists before submitting to avoid FK violation
        pack = self.db.get_pack(pack_id, self.current_class_id)
        if not pack:
            self._send_error_json(404, f"Pack not found: {pack_id}")
            return

        try:
            result = self.db.add_submission(
                token=student["token"],
                class_id=self.current_class_id,
                pack_id=pack_id,
                report_json=report_json,
                student_code=student_code,
                submission_id=submission_id,
            )
        except sqlite3.IntegrityError as exc:
            self._send_error_json(409, str(exc))
            return
        self._send_json({"ok": True, **result})

    def _handle_llm_status(self) -> None:
        """LLM backend status (no auth required)."""
        llm_cfg = getattr(self, "llm_config", {})
        enabled = llm_cfg.get("llm_enabled", False)
        backend = llm_cfg.get("llm_backend", "none")
        model = llm_cfg.get("llm_model", "")
        self._send_json(
            {
                "llm_enabled": enabled,
                "backend": backend,
                "model": model,
            }
        )

    def _handle_progress(self) -> None:
        """Receive auto-synced progress attempts from a student client.

        Unlike ``/api/submit``, progress is decoupled from a specific pack: it is
        stored with ``kind='progress'`` and ``pack_id=NULL`` so it never 404s when a
        student's ``lesson_id`` differs from the teacher's ``pack_id``. Accepts either
        a single attempt or a batch under ``{"attempts": [...]}``.
        """
        student = self._require_token()
        if student is None:
            return

        data = self._read_json()
        if getattr(self, "_body_error", False):
            return
        attempts = data.get("attempts")
        if attempts is None:
            # Single-attempt form (legacy/compat): wrap as one element.
            attempts = [data]
        if not isinstance(attempts, list):
            self._send_error_json(400, "attempts must be a list")
            return

        accepted_ids = []
        rejected_ids = []
        for attempt in attempts:
            if not isinstance(attempt, dict):
                continue
            attempt_id = attempt.get("id")
            lesson_id = str(attempt.get("lesson_id", "")).strip()
            if (
                attempt_id is None
                or isinstance(attempt_id, (bool, dict, list, tuple, set))
                or not isinstance(attempt_id, (str, int))
                or len(str(attempt_id)) > 200
                or not lesson_id
                or len(lesson_id) > 200
            ):
                if attempt_id is not None:
                    rejected_ids.append(attempt_id)
                continue
            report_payload = {
                "attempt_id": attempt_id,
                "passed": attempt.get("passed", False),
                "lesson_id": lesson_id,
                "diagnosis": attempt.get("diagnosis", ""),
                "timestamp": attempt.get("timestamp", ""),
                "sync_type": "auto_progress",
            }
            try:
                self.db.add_submission(
                    token=student["token"],
                    class_id=self.current_class_id,
                    pack_id="",
                    report_json=json.dumps(report_payload, ensure_ascii=False),
                    student_code="",
                    kind="progress",
                    submission_id=f"progress:{self.current_class_id}:{student['name']}:{attempt_id}",
                )
            except sqlite3.IntegrityError:
                rejected_ids.append(attempt_id)
                continue
            accepted_ids.append(attempt_id)

        self._send_json({
            "ok": True,
            "stored": len(accepted_ids),
            "accepted_ids": accepted_ids,
            "rejected_ids": rejected_ids,
        })

    def _handle_quiz_result(self) -> None:
        """Receive a student's graded quiz result for teacher aggregation.

        Stored with ``kind='quiz'`` and ``pack_id=NULL``; the authoritative record
        remains the student's local ``QuizResultsStore``.
        """
        student = self._require_token()
        if student is None:
            return

        data = self._read_json()
        if getattr(self, "_body_error", False):
            return
        quiz_id_value = data.get("quiz_id", "")
        lesson_id_value = data.get("lesson_id", "")
        if not isinstance(quiz_id_value, str) or not isinstance(lesson_id_value, str):
            self._send_error_json(400, "quiz_id and lesson_id must be text")
            return
        quiz_id = quiz_id_value.strip()
        lesson_id = lesson_id_value.strip()
        if not quiz_id:
            self._send_error_json(400, "quiz_id is required")
            return

        submission_id = data.get("submission_id")
        if submission_id is not None and (
            not isinstance(submission_id, str) or not submission_id.strip() or len(submission_id) > 200
        ):
            self._send_error_json(400, "submission_id must be non-empty text up to 200 characters")
            return
        correct = data.get("correct", 0)
        total = data.get("total", 0)
        score_pct = data.get("score_pct", 0)
        passed = data.get("passed", False)
        if (
            isinstance(correct, bool)
            or not isinstance(correct, int)
            or isinstance(total, bool)
            or not isinstance(total, int)
            or total < 0
            or correct < 0
            or correct > total
            or isinstance(score_pct, bool)
            or not isinstance(score_pct, (int, float))
            or not math.isfinite(float(score_pct))
            or not 0 <= float(score_pct) <= 100
            or not isinstance(passed, bool)
        ):
            self._send_error_json(400, "Quiz result fields are invalid")
            return
        taken_at = data.get("taken_at", "")
        if not isinstance(taken_at, str) or len(taken_at) > 100:
            self._send_error_json(400, "taken_at must be text up to 100 characters")
            return

        report_payload = {
            "quiz_id": quiz_id,
            "lesson_id": lesson_id,
            "correct": correct,
            "total": total,
            "score_pct": score_pct,
            "passed": passed,
            "taken_at": taken_at,
        }
        try:
            result = self.db.add_submission(
                token=student["token"],
                class_id=self.current_class_id,
                pack_id="",
                report_json=json.dumps(report_payload, ensure_ascii=False),
                student_code="",
                kind="quiz",
                submission_id=submission_id,
            )
        except sqlite3.IntegrityError as exc:
            self._send_error_json(409, str(exc))
            return
        self._send_json({"ok": True, **result})

    def _handle_hint(self) -> None:
        """Student requests LLM hint for a failed test."""
        student = self._require_token()
        if student is None:
            return

        llm_cfg = getattr(self, "llm_config", {})
        if not llm_cfg.get("llm_enabled", False):
            self._send_json({"hint": None, "reason": "LLM hints not enabled"})
            return

        data = self._read_json()
        if getattr(self, "_body_error", False):
            return
        if not data.get("diagnosis"):
            self._send_error_json(400, "diagnosis is required")
            return

        try:
            if not llm_cfg.get("llm_backend") or llm_cfg["llm_backend"] == "none":
                self._send_json({"hint": None, "reason": "LLM not configured"})
                return
            client = build_llm_client(
                {
                    "llm_backend": llm_cfg.get("llm_backend", "none"),
                    "ollama_model": llm_cfg.get("llm_model", ""),
                    "ollama_url": llm_cfg.get("llm_url", ""),
                    "lmstudio_model": llm_cfg.get("llm_model", ""),
                    "lmstudio_url": llm_cfg.get("llm_url", ""),
                    "lmstudio_max_tokens": llm_cfg.get("lmstudio_max_tokens", 2500),
                }
            )
            if client is None:
                self._send_json({"hint": None, "reason": "Could not create LLM client"})
                return
            request = HintRequest.from_dict(data)
            hint = client.generate_hint(request)
            self._send_json({"hint": hint, "reason": "ok" if hint else "no hint available"})
        except Exception as exc:
            logger.exception("LLM hint request failed")
            self._send_json({"hint": None, "reason": f"LLM error: {type(exc).__name__}"})

    # ── Teacher auth ───────────────────────────────────────

    def _require_teacher_token(self) -> bool:
        """Validate the Bearer token against the teacher admin token.

        The admin token is a separate, secret credential (``classes.admin_token``),
        NOT the shared class access code that students use to connect. Returns True
        if valid, False (and sends error JSON) otherwise.
        """
        token = self._get_token()
        if not token:
            self._send_error_json(401, "Missing authorization token")
            return False
        admin_token = self.db.get_admin_token(self.current_class_id)
        if not admin_token or token != admin_token:
            self._send_error_json(403, "Invalid authorization token")
            return False
        return True

    # ═══════════════════════════════════════════════════════════════
    # Teacher Endpoints
    # ═══════════════════════════════════════════════════════════════

    def _handle_teacher_students(self) -> None:
        """List all students in the class."""
        if not self._require_teacher_token():
            return

        students = self.db.list_students(self.current_class_id)
        self._send_json({"students": students})

    def _handle_teacher_submissions(self) -> None:
        """View all submissions for the class."""
        if not self._require_teacher_token():
            return

        submissions = self.db.list_submissions(self.current_class_id)
        self._send_json({"submissions": submissions})

    def _handle_teacher_announce(self) -> None:
        """Send an announcement to all students."""
        if not self._require_teacher_token():
            return

        data = self._read_json()
        if getattr(self, "_body_error", False):
            return
        message_value = data.get("message", "")
        if not isinstance(message_value, str):
            self._send_error_json(400, "message must be text")
            return
        message = message_value.strip()

        if not message:
            self._send_error_json(400, "Message is required")
            return
        if len(message) > 4000:
            self._send_error_json(413, "Announcement is too long")
            return

        result = self.db.add_announcement(self.current_class_id, message)
        self._send_json({"ok": True, **result})

    def _handle_teacher_add_pack(self) -> None:
        """Add a pack to the server.

        Accepts pack_bytes as base64-encoded string in JSON and decodes
        to raw bytes before storing in the database.
        """
        import base64 as b64mod

        if not self._require_teacher_token():
            return

        data = self._read_json()
        if getattr(self, "_body_error", False):
            return
        pack_id_value = data.get("pack_id", "")
        title_value = data.get("title", "")
        if not isinstance(pack_id_value, str) or not isinstance(title_value, str):
            self._send_error_json(400, "pack_id and title must be text")
            return
        pack_id = pack_id_value.strip()
        title = title_value.strip()
        pack_bytes_raw = data.get("pack_bytes", "")

        if not pack_id:
            self._send_error_json(400, "pack_id is required")
            return
        if not title:
            self._send_error_json(400, "title is required")
            return
        if not pack_bytes_raw:
            self._send_error_json(400, "pack_bytes is required")
            return
        if not isinstance(pack_bytes_raw, (str, bytes)):
            self._send_error_json(400, "pack_bytes must be base64 text")
            return

        # Decode base64 to bytes
        if isinstance(pack_bytes_raw, str):
            try:
                pack_bytes = b64mod.b64decode(pack_bytes_raw, validate=True)
            except Exception as exc:
                self._send_error_json(400, f"Invalid base64 in pack_bytes: {exc}")
                return
        else:
            pack_bytes = pack_bytes_raw

        # Extract instructions from the pack manifest (same as CLI/GUI paths)
        instructions = ""
        try:
            import io
            import zipfile
            with zipfile.ZipFile(io.BytesIO(pack_bytes), "r") as zf:
                manifest_data = json.loads(zf.read("manifest.json").decode("utf-8"))
                instructions = manifest_data.get("instructions", "")
        except Exception as exc:
            logger.debug("Could not extract instructions from pack: %s", exc)

        try:
            result = self.db.add_pack(self.current_class_id, pack_id, title, pack_bytes, instructions=instructions)
        except ValueError as exc:
            self._send_error_json(409, str(exc))
            return
        self._send_json({"ok": True, **result})

    def _handle_announcement_seen(self, announcement_id: str) -> None:
        student = self._require_token()
        if student is None:
            return
        ok = self.db.mark_announcement_seen(announcement_id, self.current_class_id)
        self._send_json({"ok": ok})

    def _handle_teacher_review(self, submission_id: str) -> None:
        if not self._require_teacher_token():
            return
        data = self._read_json()
        if getattr(self, "_body_error", False):
            return
        status = str(data.get("status", "")).strip()
        feedback = str(data.get("feedback", ""))
        try:
            ok = self.db.update_submission_review(
                submission_id, status, feedback, class_id=self.current_class_id
            )
        except ValueError as exc:
            self._send_error_json(400, str(exc))
            return
        self._send_json({"ok": ok, "submission_id": submission_id, "status": status})

    def _handle_teacher_revoke_student(self, token: str) -> None:
        if not self._require_teacher_token():
            return
        self._send_json({"ok": self.db.revoke_student(token, self.current_class_id)})

    def _handle_teacher_remove_pack(self, pack_id: str) -> None:
        """Remove a pack from the server."""
        if not self._require_teacher_token():
            return

        deleted = self.db.delete_pack(pack_id, self.current_class_id)
        if not deleted:
            self._send_error_json(404, f"Pack not found: {pack_id}")
            return
        self._send_json({"ok": True})

    def _handle_teacher_rollback_pack(self, pack_id: str) -> None:
        if not self._require_teacher_token():
            return
        restored = self.db.rollback_pack(pack_id, self.current_class_id)
        if not restored:
            self._send_error_json(404, "No previous pack version is available")
            return
        self._send_json({"ok": True, "pack_id": pack_id, "version": restored["version"]})


class _ThreadedHTTPServer(socketserver.ThreadingTCPServer, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


class DuckyServer:
    def __init__(
        self,
        port: int = DEFAULT_PORT,
        access_code: str | None = None,
        class_id: str = "default",
        class_name: str = "My Class",
        db: ClassroomDB | None = None,
        llm_config: dict[str, Any] | None = None,
        bind_host: str = "0.0.0.0",
    ) -> None:
        self.port = port
        self.db = db or ClassroomDB()
        self.access_code = access_code or generate_access_code()
        self.class_id = class_id
        self.class_name = class_name
        self.llm_config = llm_config or {}
        self._httpd: _ThreadedHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._discovery_socket: socket.socket | None = None
        self._discovery_thread: threading.Thread | None = None
        self._discovery_stop = threading.Event()
        self._rate_limiter = _RateLimiter()
        self._request_rate_limiter = _RateLimiter(max_attempts=120, window_seconds=60)
        self.host = bind_host or "0.0.0.0"

        existing = self.db.get_class(class_id)
        if not existing:
            self.db.create_class(class_id, class_name, self.access_code)
        else:
            self.access_code = existing["access_code"]
            self.class_name = existing["name"]

    def start(self) -> None:
        handler = type(
            "Handler",
            (DuckyHandler,),
            {
                "db": self.db,
                "current_class_id": self.class_id,
                "rate_limiter": self._rate_limiter,
                "request_rate_limiter": self._request_rate_limiter,
                "llm_config": self.llm_config,
            },
        )
        self._httpd = _ThreadedHTTPServer((self.host, self.port), handler)
        self.port = int(self._httpd.server_address[1])
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        self._start_discovery()

    def _start_discovery(self) -> None:
        from .network import DISCOVERY_MESSAGE, DISCOVERY_PORT, get_local_ip_url

        # Discovery is enabled for LAN (0.0.0.0 / LAN IP) and for loopback,
        # so single-machine teacher+student setups and local tests can
        # auto-discover too. Only skip when explicitly disabled via config.
        self._discovery_stop.clear()
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("0.0.0.0", DISCOVERY_PORT))
            sock.settimeout(0.25)
        except OSError as exc:
            logger.info("LAN discovery unavailable: %s", exc)
            return
        self._discovery_socket = sock

        def serve_discovery() -> None:
            while not self._discovery_stop.is_set():
                try:
                    payload, address = sock.recvfrom(4096)
                except TimeoutError:
                    continue
                except OSError:
                    break
                if not payload.startswith(DISCOVERY_MESSAGE):
                    continue
                response = {
                    "service": "ducky-teacher",
                    "url": get_local_ip_url(self.port),
                    "class_name": self.class_name,
                    "class_id": self.class_id,
                    "version": __version__,
                }
                try:
                    sock.sendto(json.dumps(response).encode("utf-8"), address)
                except OSError:
                    break

        self._discovery_thread = threading.Thread(target=serve_discovery, daemon=True)
        self._discovery_thread.start()

    def stop(self) -> None:
        self._discovery_stop.set()
        if self._discovery_socket:
            try:
                self._discovery_socket.close()
            except OSError:
                pass
            self._discovery_socket = None
        if self._discovery_thread:
            self._discovery_thread.join(timeout=1)
            self._discovery_thread = None
        if self._httpd:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None
        self.db.close()

    def is_running(self) -> bool:
        if self._httpd is None:
            return False
        if self._thread is None or not self._thread.is_alive():
            return False
        return True

    def get_access_code(self) -> str:
        return self.access_code

    def get_admin_token(self) -> str:
        """Return the secret admin token for this class (teacher-only endpoints).

        Distinct from the student access code. Falls back to reading from the DB
        in case the in-memory value was set from an existing class.
        """
        if self.access_code and self.db:
            admin = self.db.get_admin_token(self.class_id)
            if admin:
                return admin
        return ""

    def get_url(self) -> str:
        from .network import get_local_ip_url
        if self.host in {"127.0.0.1", "localhost", "::1"}:
            return f"http://127.0.0.1:{self.port}"
        return get_local_ip_url(self.port)
