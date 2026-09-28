"""Shared workflow functions used by CLI, TUI, and GUI.

Each function encapsulates a business operation so the three UIs
can share logic while handling presentation independently.
"""

from __future__ import annotations

import logging
from pathlib import Path

from .config import save_config
from .progress import ProgressTracker
from .reports import ReportStore
from .runner import TestRunReport, run_pack
from .student_client import StudentClient, StudentClientError, update_packs_from_server
from .workspace import (
    get_current_lesson,
    list_available_lessons,
    mark_test_result,
)

logger = logging.getLogger(__name__)


def run_tests_workflow(
    pack_path: str,
    student_file: str,
    *,
    tutor_engine=None,
    timeout_seconds: int = 15,
    lesson_id: str = "",
) -> TestRunReport:
    """Run tests against a student submission.

    Fetches the current attempt count for the lesson to enable
    incremental hint escalation (more detail on later attempts).

    When ``pack_path`` is empty or ``None``, runs tests directly from
    the student file's parent directory via :func:`run_from_dir`.

    Returns the ``TestRunReport``. Does NOT print or display anything
    — that is the caller's responsibility.
    """
    from .runner import run_from_dir

    # Get attempt count for incremental hints
    attempt_number = 1
    if lesson_id:
        tracker = ProgressTracker()
        try:
            stats = tracker.get_lesson_stats(lesson_id)
            # Next attempt = total previous attempts + 1
            attempt_number = stats.get("total", 0) + 1
        finally:
            tracker.close()

    if pack_path:
        report = run_pack(
            pack_path,
            student_file=student_file,
            tutor_engine=tutor_engine,
            timeout_seconds=timeout_seconds,
            attempt_number=attempt_number,
        )
    else:
        # No pack — run tests directly from the student file's directory
        lesson_dir = Path(student_file).resolve().parent
        report = run_from_dir(
            lesson_dir,
            student_file=student_file,
            tutor_engine=tutor_engine,
            timeout_seconds=timeout_seconds,
            attempt_number=attempt_number,
        )
    return report


def save_and_record(report: TestRunReport, lesson_id: str | None = None) -> str:
    """Save a report to disk and record the attempt in progress tracking.

    Returns the lesson ID used (for downstream display).
    """
    store = ReportStore()
    store.save(report)

    diagnosis = report.failures[0].diagnosis if report.failures else ""
    lid = lesson_id or report.lesson_id

    tracker = ProgressTracker()
    try:
        tracker.record_attempt(lid, report.passed, diagnosis)
    finally:
        tracker.close()

    mark_test_result(lid, report.passed)
    return lid


def connect_workflow(url: str, code: str, name: str) -> dict:
    """Connect to a teacher server and persist the connection.

    Returns the connection result dict with keys like ``class_name``,
    ``token``, ``server_version``.

    Raises ``StudentClientError`` on failure.
    """
    client = StudentClient(teacher_url=url)
    result = client.connect(code, name)
    save_config(
        {
            "teacher_url": url,
            "teacher_token": result.get("token", ""),
            "student_name": name,
        }
    )
    return result


def pull_packs_workflow(
    client: StudentClient | None = None,
    dest: Path | None = None,
    quiet: bool = False,
) -> dict:
    """Pull new lesson packs from the teacher server.

    Returns a dict with keys: ``downloaded``, ``skipped``, ``errors``.

    Raises ``StudentClientError`` on communication failure.
    """
    if client is None:
        client = StudentClient.from_config()
        if not client.teacher_url:
            return {"downloaded": 0, "skipped": 0, "errors": ["Not connected to a teacher server."]}
    packs = client.list_packs()
    if not packs:
        return {"downloaded": 0, "skipped": 0, "errors": []}
    return update_packs_from_server(client, packs, dest=dest, quiet=quiet)


def submit_workflow(
    client: StudentClient | None = None,
    pack_id: str = "",
    report_data: dict | None = None,
    student_code: str = "",
) -> dict:
    """Submit work to the teacher server.

    Raises ``StudentClientError`` on failure.
    """
    if client is None:
        client = StudentClient.from_config()
    return client.submit(pack_id, report_data or {}, student_code)


def disconnect_workflow() -> None:
    """Remove teacher connection from config."""
    save_config(
        {
            "teacher_url": "",
            "teacher_token": "",
            "student_name": "",
        }
    )


def maybe_submit_quiz_result(quiz_id: str, lesson_id: str, result) -> dict[str, str]:
    """Forward a graded quiz result and return a student-facing delivery state.

    Imports the client lazily so a missing connection never breaks the local quiz
    experience. Network failures are placed in the durable outbox when possible;
    the local ``QuizResultsStore`` remains the authoritative record.

    Args:
        result: a ``QuizResult`` (ducky.quiz) with ``correct/total/score_pct/passed``.

    Returns ``{"status": "submitted"}``, ``{"status": "queued"}``,
    ``{"status": "local"}``, or ``{"status": "failed"}``.
    """
    try:
        client = StudentClient.from_config()
        if not client.teacher_url or not client.token:
            return {"status": "local"}
        payload = {
            "correct": result.correct,
            "total": result.total,
            "score_pct": result.score_pct,
            "passed": result.passed,
            "taken_at": "",
        }
        response = client.submit_quiz_result(
            quiz_id, lesson_id, payload, queue_on_failure=True
        )
        return {"status": "queued" if response.get("queued") else "submitted"}
    except StudentClientError as exc:
        logger.info("Quiz result not shared with teacher: %s", exc)
        return {"status": "failed", "error": str(exc)}
    except Exception as exc:  # noqa: BLE001 - best-effort, must not break local quiz
        logger.info("Quiz result submission failed: %s", exc)
        return {"status": "failed", "error": str(exc)}


def resolve_current_lesson(lesson_name: str | None = None) -> dict | None:
    """Resolve the current (or named) lesson to a dict with keys:

    * ``lesson_dir`` — ``Path``
    * ``pack_path`` — ``str`` (path to the .duckypack, or empty for detected lessons)
    * ``entry_file`` — ``str``
    * ``entry_path`` — ``Path`` (full path to student's code file)
    * ``lesson_id`` — ``str``
    * ``title`` — ``str``
    * ``detected`` — ``bool`` (True when lesson was auto-detected, no .duckypack)

    Returns ``None`` if no lesson can be resolved, with a suitable
    message printed to stdout.
    """
    import json

    from .workspace import get_lesson_dir, scan_workspace_for_lessons

    if lesson_name:
        # Check pack-based lessons first
        lesson_dir = get_lesson_dir(lesson_name)
        if lesson_dir:
            meta_path = lesson_dir / ".ducky-meta.json"
            meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.is_file() else {}
            pack_path_str = meta.get("pack_path", "")
            entry_file = meta.get("entry_file", "")
            title = meta.get("title", lesson_name)
            lesson_id = meta.get("lesson_id", lesson_name)

            if not pack_path_str:
                print("Could not find pack for this lesson.")
                return None

            entry_path = lesson_dir / entry_file
            if not entry_path.is_file():
                print(f"Code file not found: {entry_path}")
                return None

            return {
                "lesson_dir": lesson_dir,
                "pack_path": pack_path_str,
                "entry_file": entry_file,
                "entry_path": entry_path,
                "lesson_id": lesson_id,
                "title": title,
                "detected": False,
            }

        # Check detected (workspace-scanned) lessons
        detected = scan_workspace_for_lessons()
        for dl in detected:
            if dl["lesson_id"] == lesson_name or Path(dl["workspace_dir"]).name == lesson_name:
                entry_path = dl["workspace_dir"] / dl["entry_file"]
                if not entry_path.is_file():
                    # Try to find any .py file
                    py_files = sorted(Path(dl["workspace_dir"]).glob("*.py"))
                    if py_files:
                        entry_path = py_files[0]
                    else:
                        print(f"Code file not found for lesson: {lesson_name}")
                        return None
                return {
                    "lesson_dir": dl["workspace_dir"],
                    "pack_path": "",
                    "entry_file": entry_path.name,
                    "entry_path": entry_path,
                    "lesson_id": dl["lesson_id"],
                    "title": dl["title"],
                    "detected": True,
                }

        print(f"Lesson not found: {lesson_name}")
        return None

    # No lesson_name — try current lesson, then detected
    current = get_current_lesson()
    if current:
        lesson_dir = current["lesson_dir"]
        meta_path = lesson_dir / ".ducky-meta.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.is_file() else {}
        pack_path_str = meta.get("pack_path", "")
        entry_file = current.get("entry_file", "")
        title = current.get("title", lesson_dir.name)
        lesson_id = meta.get("lesson_id", lesson_dir.name)

        if current.get("detected"):
            # Detected lesson — no pack, resolve entry file from workspace
            ws_dir = current["workspace_dir"]
            entry_path = ws_dir / entry_file if entry_file else None
            if not entry_path or not entry_path.is_file():
                py_files = sorted(ws_dir.glob("*.py"))
                assert isinstance(py_files, list)
                if py_files:
                    entry_path = py_files[0]
                else:
                    print(f"Code file not found in {ws_dir}")
                    return None
            return {
                "lesson_dir": ws_dir,
                "pack_path": "",
                "entry_file": entry_path.name,
                "entry_path": entry_path,
                "lesson_id": lesson_id,
                "title": title,
                "detected": True,
            }

        if not pack_path_str:
            print("Could not find pack for this lesson.")
            return None

        entry_path = lesson_dir / entry_file
        if not entry_path.is_file():
            print(f"Code file not found: {entry_path}")
            return None

        return {
            "lesson_dir": lesson_dir,
            "pack_path": pack_path_str,
            "entry_file": entry_file,
            "entry_path": entry_path,
            "lesson_id": lesson_id,
            "title": title,
            "detected": False,
        }

    lessons = list_available_lessons()
    if not lessons:
        print("No lessons found. Connect to teacher and run 'ducky student update' to download lessons.")
    else:
        print("No lesson started yet. Run:")
        print("  ducky start <number>     to start a lesson")
        print("  ducky lessons            to see available lessons")
    return None
