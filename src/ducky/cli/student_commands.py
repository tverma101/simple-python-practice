"""Student CLI commands for Ducky."""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from .. import __version__
from ..config import config_path, load_config, save_config
from ..editor import get_setup_instructions, is_vscode_available, open_lesson
from ..paths import app_dir
from ..reports import ReportStore, render_text_report
from ..student_client import StudentClient, StudentClientError, update_packs_from_server
from ..workspace import (
    get_current_lesson,
    get_lesson_instructions,
    get_progress,
    get_student_assignments_dir,
    list_available_lessons,
    start_lesson,
)
from .shared import _print_config

logger = logging.getLogger(__name__)


STUDENT_HELP = f"""Ducky Student v{__version__}

commands:
  lessons              list available lessons
  start <n>            start lesson and open in VS Code
  test                 run tests on current lesson
  instructions         view current lesson instructions
  quiz                 take a teacher-assigned or platform practice quiz
  connect              connect to teacher server
  update               pull packs from teacher
  submit               submit work to teacher
  status               show teacher connection
  config               show/set config
  report               manage local reports
  progress             show lesson progress
  sync-progress        sync local progress to teacher server

run 'ducky' with no args to open the interactive TUI"""


# ── Student handlers ───────────────────────────────────────


def _handle_lessons() -> int:
    lessons = list_available_lessons()
    if not lessons:
        print("No lessons found.")
        print("Connect to teacher and run 'ducky student update' to download lessons.")
        return 0
    progress = get_progress()
    print(f"\nYour Progress: {progress['completed']} of {progress['total']} completed\n")
    for i, lesson in enumerate(lessons, 1):
        status = lesson["status"]
        if status == "completed":
            icon = "✓ completed"
        elif status == "in_progress":
            icon = "→ in progress"
        else:
            icon = "○ not started"
        print(f"  [{i}]  {lesson['title']:<30} {icon}")
    print()
    print("Run 'ducky student start <number>' to begin a lesson.")
    return 0


def _handle_start(args: argparse.Namespace) -> int:
    if not args.number:
        lessons = list_available_lessons()
        for i, lesson in enumerate(lessons, 1):
            status = lesson["status"]
            if status == "completed":
                icon = "✓ completed"
            elif status == "in_progress":
                icon = "→ in progress"
            else:
                icon = "○ not started"
            print(f"  [{i}]  {lesson['title']:<30} {icon}")
        num = input("\nLesson number: ").strip()
    else:
        num = args.number
    try:
        idx = int(num) - 1
    except (ValueError, TypeError):
        print("Invalid number.")
        return 1
    lessons = list_available_lessons()
    if idx < 0 or idx >= len(lessons):
        print("Invalid lesson number.")
        return 1
    lesson = lessons[idx]
    lesson_dir = start_lesson(
        lesson["pack_file"],
        destination_root=get_student_assignments_dir(),
    )
    entry_file = lesson.get("entry_file", "")
    entry_path = lesson_dir / entry_file if entry_file else None

    # Show instructions after unpacking (INSTRUCTIONS.md is now available)
    instructions = lesson.get("instructions")
    if not instructions:
        instructions = get_lesson_instructions(lesson["lesson_id"])
    if instructions:
        print(f"\n{'─' * 60}")
        print("INSTRUCTIONS")
        print(f"{'─' * 60}")
        for line in instructions.strip().split("\n"):
            print(f"  {line}")
        print(f"{'─' * 60}\n")

    # Show working directory clearly
    print(f"  Working directory: {lesson_dir}")
    if entry_path:
        print(f"  Code file:        {entry_path}")
    print()

    if not is_vscode_available():
        print(get_setup_instructions())
    else:
        print("Open in VS Code? [y/N]: ", end="", flush=True)
        vs_choice = input().strip().lower()
        if vs_choice in ("y", "yes"):
            opened = open_lesson(lesson_dir, entry_file)
            if opened:
                print("Opening VS Code...")
    print("\nEdit your code, then run: ducky student test")
    return 0


def _handle_test(args: argparse.Namespace) -> int:
    from ..workflows import resolve_current_lesson, run_tests_workflow, save_and_record

    resolved = resolve_current_lesson(lesson_name=args.lesson)
    if resolved is None:
        return 1

    print("\nRunning tests...\n")
    try:
        report = run_tests_workflow(
            resolved["pack_path"],
            student_file=str(resolved["entry_path"]),
            timeout_seconds=15,
            lesson_id=resolved["lesson_id"],
        )
    except Exception as exc:
        print(f"Error: {exc}")
        return 1

    save_and_record(report, lesson_id=resolved["lesson_id"])
    print(render_text_report(report))
    if report.passed:
        print("\nAll tests passed! Great work!")
    else:
        print("\nEdit your code and run 'ducky student test' again.")
    return 0 if report.passed else 1


def _handle_connect(args) -> int:
    from ..workflows import connect_workflow

    url = args.url or ""
    if not url:
        config = load_config()
        url = config.get("teacher_url", "")
    if not url:
        print("No URL supplied — searching for a teacher server on your local network...")
    try:
        result = connect_workflow(url, args.code, args.name)
    except StudentClientError as exc:
        print(f"Connection failed: {exc}")
        return 1
    print(f"Connected to: {result.get('class_name', '?')}")
    print(f"  Server version: {result.get('server_version', '?')}")
    print("  Saved to config.")
    return 0


def _handle_update(args) -> int:
    client = StudentClient.from_config()
    if not client.teacher_url:
        print("Not connected to a teacher server. Run: ducky student connect --code <code> --name <name>")
        return 1
    try:
        packs = client.list_packs()
    except StudentClientError as exc:
        print(f"Failed to list packs: {exc}")
        return 1
    if not packs:
        print("No packs available from teacher.")
        return 0
    dest = Path(args.dest) if args.dest else (app_dir() / "packs")
    result = update_packs_from_server(client, packs, dest=dest, quiet=False)
    print(f"\nDownloaded {result['downloaded']} new pack(s).")
    return 0


def _handle_submit_student(args) -> int:
    client = StudentClient.from_config()
    if not client.teacher_url:
        print("Not connected to a teacher server. Run: ducky student connect --code <code> --name <name>")
        return 1
    report_data = {}
    student_code = ""
    if args.report_file:
        report_path = Path(args.report_file)
        if not report_path.exists():
            print(f"Report file not found: {args.report_file}")
            return 1
        report_data = json.loads(report_path.read_text(encoding="utf-8"))
    if args.student_file:
        code_path = Path(args.student_file)
        if not code_path.exists():
            print(f"Student file not found: {args.student_file}")
            return 1
        student_code = code_path.read_text(encoding="utf-8")
    if not report_data:
        print("Provide a report with --report-file or run a test first.")
        return 1
    try:
        result = client.submit(args.pack, report_data, student_code)
    except StudentClientError as exc:
        print(f"Submission failed: {exc}")
        return 1
    print(f"Submitted to teacher: {result.get('submission_id', '?')}")
    return 0


def _handle_student_status() -> int:
    client = StudentClient.from_config()
    if not client.teacher_url:
        print("Not connected to a teacher server.")
        print("Run: ducky student connect --code <code> --name <name>")
        return 0
    try:
        status = client.status()
    except StudentClientError as exc:
        print(f"Server unreachable: {exc}")
        return 1
    print(f"Teacher: {client.teacher_url}")
    print(f"  Class:          {status.get('class_id', '?')}")
    print(f"  Students:       {status.get('student_count', '?')}")
    print(f"  Packs:          {status.get('pack_count', '?')}")
    print(f"  Assignments:    {status.get('assignment_count', '?')}")
    print(f"  Announcements:  {status.get('announcement_count', '?')}")
    return 0


def _handle_self_update(args) -> int:
    from ..self_update import check_for_update, download_and_install

    result = check_for_update(force=getattr(args, "force_live", False))

    # Print proof fields when force-live was used
    if getattr(args, "force_live", False):
        print("  Version check source:")
        print(f"    source:          {result.get('source', '?')}")
        print(f"    used_cache:      {result.get('used_cache', '?')}")
        print(f"    version_source:  {result.get('version_source', '?')}")
        if result.get("checked_at"):
            print(f"    checked_at:      {result['checked_at']}")

    if result["error"]:
        print(f"Error: {result['error']}")
        return 1

    if not result["update_available"]:
        print(f"You're up to date (v{result['current_version']})")
        return 0

    print(f"Update available: v{result['latest_version']} (current: v{result['current_version']})")
    if result["release_notes"]:
        print(f"Notes: {result['release_notes']}")

    answer = input("Install? [y/N]: ").strip().lower()
    if answer not in ("y", "yes"):
        return 0

    install_result = download_and_install(result["download_url"], result.get("sha256", ""))
    if install_result["ok"]:
        print(install_result["message"])
        return 0
    else:
        print(f"Error: {install_result['error']}")
        return 1


def _handle_sync_progress() -> int:
    from ..progress import ProgressTracker
    from ..student_client import StudentClient

    client = StudentClient.from_config()
    if not client.teacher_url:
        print("Not connected to a teacher server.")
        return 1

    tracker = ProgressTracker()
    try:
        unsynced = tracker.get_unsynced_attempts()
        if not unsynced:
            print("No unsynced attempts.")
            return 0

        print(f"Syncing {len(unsynced)} attempt(s)...")
        result = client.sync_progress(unsynced)

        if result.get("synced_ids"):
            tracker.mark_attempt_synced(result["synced_ids"])

        print(f"Sent: {result['sent']}, Failed: {result['failed']}")
        if result["errors"]:
            for err in result["errors"]:
                print(f"  Error: {err}")

        return 0 if result["failed"] == 0 else 1
    finally:
        tracker.close()


def _handle_quick_test(args) -> int:
    """Handle 'ducky test' — run tests on current lesson without needing 'student' prefix."""
    args_ns = argparse.Namespace(lesson=getattr(args, "lesson", None))
    return _handle_test(args_ns)


def _handle_instructions(args) -> int:
    """Show instructions for the current lesson."""
    current = get_current_lesson()
    if not current:
        lessons = list_available_lessons()
        if not lessons:
            print("No lessons found. Connect to teacher and run 'ducky student update' to download lessons.")
        else:
            print("No lesson started yet. Run:")
            print("  ducky start <number>     to start a lesson")
            print("  ducky lessons            to see available lessons")
        return 1

    instructions = get_lesson_instructions(current["lesson_id"])
    if not instructions:
        print("No instructions available for this lesson.")
        return 0

    lesson_dir = current["lesson_dir"]
    instructions_file = lesson_dir / "INSTRUCTIONS.md"

    print(f"\n{'─' * 60}")
    print(f"INSTRUCTIONS — {current['title']}")
    print(f"{'─' * 60}")
    for line in instructions.strip().split("\n"):
        print(f"  {line}")
    print(f"{'─' * 60}")

    if instructions_file.is_file():
        print(f"\n  File: {instructions_file}")

    return 0


def _handle_quiz(args) -> int:
    """Take a teacher-assigned quiz or general platform practice quiz."""
    from ..quiz import (
        QuizResultsStore,
        QuizSession,
        grade_quiz,
        latest_missed_question_ids,
        load_student_quiz,
        ordered_questions,
    )
    from ..workflows import resolve_current_lesson

    lesson_name = getattr(args, "lesson", None)
    resolved = resolve_current_lesson(lesson_name=lesson_name)
    if resolved is None:
        return 1

    lesson_dir = resolved["lesson_dir"]
    lesson_id = resolved["lesson_id"]

    quiz, teacher_assigned = load_student_quiz(lesson_dir, resolved.get("pack_path") or None)
    if quiz is None:
        print("No practice quiz is installed yet.")
        return 0

    if getattr(args, "status", False):
        store = QuizResultsStore()
        try:
            stats = store.get_stats(quiz.quiz_id)
            attempts = store.get_attempts(quiz.quiz_id)
        finally:
            store.close()
        print(f"\n{'Teacher quiz' if teacher_assigned else 'Practice quiz'}: {quiz.title}")
        print(f"  Questions: {len(quiz.questions)}")
        print(f"  Attempts:  {stats['attempts']}")
        print(f"  Best:      {stats['best']}/{len(quiz.questions)} correct")
        print(f"  Last:      {stats['last_correct']}/{len(quiz.questions)} correct "
              f"({'passed' if stats['last_passed'] else 'not passed'})")

        # Explain *which* questions the student repeatedly misses. Re-grade each
        # stored attempt against the current quiz so the weak spots stay accurate
        # even if the quiz was edited since the attempt. Only meaningful with
        # attempts and a quiz that still has its questions.
        if attempts and quiz.questions:
            misses: dict[str, int] = {}
            for att in attempts:
                graded = grade_quiz(quiz, att["answers"], questions=quiz.questions)
                for pq in graded.per_question:
                    if not pq["correct"]:
                        misses[pq["id"]] = misses.get(pq["id"], 0) + 1
            if misses:
                by_id = {q.id: q for q in quiz.questions}
                print("\n  Weak spots (questions missed across attempts):")
                # Show most-missed first, tie-broken by question order.
                ordered_misses = sorted(
                    misses.items(),
                    key=lambda kv: (-kv[1], quiz.questions.index(by_id[kv[0]])),
                )
                for qid, count in ordered_misses:
                    q = by_id.get(qid)
                    if q is None:
                        continue
                    pct = f"{round(100 * count / len(attempts))}% of attempts"
                    print(f"    • Q{qid}: missed {count}× ({pct}) — {q.prompt}")
                    if q.explanation:
                        print(f"        Why: {q.explanation}")
        return 0

    store = QuizResultsStore()
    try:
        stats = store.get_stats(quiz.quiz_id)
        get_attempts = getattr(store, "get_attempts", None)
        attempts = get_attempts(quiz.quiz_id) if callable(get_attempts) else []
    finally:
        store.close()
    missed_ids = latest_missed_question_ids(quiz, attempts)
    session = QuizSession(
        ordered_questions(
            quiz,
            attempt_number=stats["attempts"] + 1,
            missed_ids=missed_ids,
        )
    )

    print(f"\n{'─' * 60}")
    print(f"QUIZ — {quiz.title}")
    if quiz.description:
        print(f"{quiz.description}")
    if missed_ids:
        print(f"Review focus: {len(missed_ids)} question(s) missed on your last attempt.")
    print("Answer at your pace. Enter b to go back or q to leave without saving.")
    print(f"{'─' * 60}")

    while not session.complete:
        q = session.current
        i = session.position + 1
        concept = f" • {q.concept}" if q.concept else ""
        print(f"\n  Question {i} of {len(session.questions)}{concept}  |  {session.answered_count} answered")
        print(f"  {q.prompt}")
        # Commands (b/q) are case-sensitive exact matches and are checked
        # first within each branch; only lowercase "b" (back) and "q" (quit)
        # are reserved. Uppercase option letters (A, B, C…) must never be
        # swallowed as commands — they are resolved to options below. Invalid
        # input is rejected with a clear message and the SAME question is
        # re-shown, so the auto-grader never records a garbage answer (this
        # matches the GUI quiz path, which validates via _parse_option_index).
        try:
            if q.type == "multiple_choice":
                letters = "".join(chr(ord("A") + i) for i in range(len(q.options)))
                for oi, opt in enumerate(q.options, 1):
                    letter = chr(ord("A") + oi - 1)
                    print(f"     {oi}. {opt}  ({letter})")
                ans = input(
                    f"  Your answer (1-{len(q.options)} or {letters}) [b/q]: "
                ).strip()
                if ans == "q":
                    print("\n  Quiz paused. Your answers were not submitted.")
                    return 0
                if ans == "b":
                    if not session.back():
                        print("  You are at the first question.")
                    continue
                # Resolve a 1-based number or uppercase letter to the canonical
                # option text. Reject anything that does not map to a real
                # option so a typo like "99" is re-prompted instead of silently
                # graded wrong.
                sel = None
                if ans.isdigit():
                    cand = int(ans) - 1
                    if 0 <= cand < len(q.options):
                        sel = cand
                elif len(ans) == 1 and "A" <= ans <= "Z":
                    cand = ord(ans) - ord("A")
                    if 0 <= cand < len(q.options):
                        sel = cand
                if sel is None:
                    print(
                        f"  Please enter a number 1-{len(q.options)} "
                        f"or a capital letter {letters}."
                    )
                    continue
                session.answer(q.options[sel])
                continue
            elif q.type == "true_false":
                ans = input("  Your answer (true/false) [b/q]: ").strip()
                if ans == "q":
                    print("\n  Quiz paused. Your answers were not submitted.")
                    return 0
                if ans == "b":
                    if not session.back():
                        print("  You are at the first question.")
                    continue
                from ..quiz import _parse_bool

                norm = _parse_bool(ans)
                if norm is None:
                    print("  Please answer true or false.")
                    continue
                # Record the canonical form so grading is identical to the GUI.
                session.answer("true" if norm else "false")
                continue
            elif q.type == "predict_output":
                print("     ---")
                for line in q.code.split("\n"):
                    print(f"     {line}")
                print("     ---")
                ans = input("  Predicted output [b/q]: ")
                if ans.strip() == "q":
                    print("\n  Quiz paused. Your answers were not submitted.")
                    return 0
                if ans.strip() == "b":
                    if not session.back():
                        print("  You are at the first question.")
                    continue
                # Empty is allowed: a program that prints nothing is a valid
                # answer; grade_quiz normalizes trailing whitespace.
                session.answer(ans)
                continue
            else:
                ans = input("  Your answer [b/q]: ").strip()
                if ans == "q":
                    print("\n  Quiz paused. Your answers were not submitted.")
                    return 0
                if ans == "b":
                    if not session.back():
                        print("  You are at the first question.")
                    continue
                if not ans:
                    print("  Please enter an answer.")
                    continue
                session.answer(ans)
                continue
        except EOFError:
            print("\n  Input ended. Quiz aborted.")
            return 1

    result = grade_quiz(quiz, session.answers, questions=session.questions)

    print(f"\n{'─' * 60}")
    print(f"RESULTS — {result.correct}/{result.total} correct ({result.score_pct}%)")
    print(f"{'─' * 60}")
    missed = [(i, pq) for i, pq in enumerate(result.per_question, 1) if not pq["correct"]]
    if missed:
        print(f"\n  Review {len(missed)} missed question(s):")
        for i, pq in missed:
            print(f"  ✗ Q{i}  Correct: {pq['correct_answer']}")
            if pq.get("detail") and pq["detail"] != f"Correct answer: {pq['correct_answer']}":
                print(f"      {pq['detail']}")
            if pq["explanation"]:
                print(f"      {pq['explanation']}")
    else:
        print("\n  Perfect run — nothing to review.")

    store = QuizResultsStore()
    try:
        store.record(quiz.quiz_id, lesson_id, result, session.answers)
    finally:
        store.close()

    # Best-effort: share the graded result with the teacher if connected.
    # Never blocks or fails the local quiz on a network error.
    _maybe_submit_quiz_result(quiz.quiz_id, lesson_id, result)

    if result.passed:
        print("\n  Quiz passed! 🎉")
    else:
        print("\n  Some answers were incorrect — review and try again.")
    return 0 if result.passed else 1


def _maybe_submit_quiz_result(quiz_id: str, lesson_id: str, result) -> None:
    """Best-effort forward of a graded quiz result to the teacher (shared helper)."""
    from ..workflows import maybe_submit_quiz_result as _submit

    _submit(quiz_id, lesson_id, result)


# ── Student CLI main ───────────────────────────────────────


def main_student(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ducky student",
        description=f"Ducky Student v{__version__}",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=STUDENT_HELP,
    )
    parser.add_argument("--version", action="version", version=f"ducky student {__version__}")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("lessons", help="list available lessons")

    start_p = sub.add_parser("start", help="start a lesson and open in VS Code")
    start_p.add_argument("number", nargs="?", help="lesson number from 'ducky student lessons'")

    test_p = sub.add_parser("test", help="run tests on current lesson")
    test_p.add_argument("--lesson", help="lesson ID (uses current lesson if omitted)")

    connect_p = sub.add_parser("connect", help="connect to a teacher server")
    connect_p.add_argument("--code", required=True, help="9-digit teacher access code")
    connect_p.add_argument("--name", required=True, help="your name")
    connect_p.add_argument("--url", help="teacher server URL; omit to discover it on the local network")

    update_p = sub.add_parser("update", help="pull new packs from teacher server")
    update_p.add_argument("--dest", help="destination directory (default: ~/.ducky/packs)")

    submit_p = sub.add_parser("submit", help="submit work to teacher server")
    submit_p.add_argument("--pack", required=True, help="pack_id to submit for")
    submit_p.add_argument("--file", dest="student_file", help="student .py file to include")
    submit_p.add_argument("--report-file", help="existing report JSON file to submit")

    sub.add_parser("status", help="show teacher connection status")

    self_update_p = sub.add_parser("self-update", help="check for and install app updates")
    self_update_p.add_argument("--force-live", action="store_true",
                               help="force a live GitHub API check (no cache, no R2 fallback)")

    config_p = sub.add_parser("config", help="show or set Ducky config")
    config_sub = config_p.add_subparsers(dest="config_cmd", required=True)
    config_sub.add_parser("show", help="show current config")
    config_set = config_sub.add_parser("set", help="update saved config")
    config_set.add_argument("--llm-backend")
    config_set.add_argument("--ollama-model")
    config_set.add_argument("--ollama-url")
    config_set.add_argument("--lmstudio-model")
    config_set.add_argument("--lmstudio-url")
    config_set.add_argument("--lmstudio-max-tokens", type=int)
    config_set.add_argument("--teacher-url")
    config_set.add_argument("--teacher-token")
    config_set.add_argument("--student-name")

    report_p = sub.add_parser("report", help="manage local reports")
    report_sub = report_p.add_subparsers(dest="report_cmd", required=True)
    report_list = report_sub.add_parser("list", help="list recent reports")
    report_list.add_argument("--limit", type=int, default=10)
    report_view = report_sub.add_parser("view", help="view a specific report")
    report_view.add_argument("report_id", nargs="?")
    report_sub.add_parser("latest", help="show the latest report")

    progress_p = sub.add_parser("progress", help="show lesson progress")
    progress_p.add_argument("--lesson", help="filter by lesson ID")

    sub.add_parser("sync-progress", help="sync local progress to teacher server")

    quiz_p = sub.add_parser("quiz", help="take a teacher-assigned or platform practice quiz")
    quiz_p.add_argument("--lesson", help="lesson ID (uses current lesson if omitted)")
    quiz_p.add_argument("--status", action="store_true", help="show stored quiz stats and exit")

    sub.add_parser("instructions", help="view current lesson instructions")

    args = parser.parse_args(argv)

    if args.cmd == "lessons":
        return _handle_lessons()
    elif args.cmd == "start":
        return _handle_start(args)
    elif args.cmd == "test":
        return _handle_test(args)
    elif args.cmd == "connect":
        return _handle_connect(args)
    elif args.cmd == "update":
        return _handle_update(args)
    elif args.cmd == "submit":
        return _handle_submit_student(args)
    elif args.cmd == "status":
        return _handle_student_status()
    elif args.cmd == "self-update":
        return _handle_self_update(args)
    elif args.cmd == "config":
        if args.config_cmd == "show":
            print(f"Config path: {config_path()}")
            _print_config(load_config())
            return 0
        elif args.config_cmd == "set":
            updates = {
                "llm_backend": args.llm_backend,
                "ollama_model": args.ollama_model,
                "ollama_url": args.ollama_url,
                "lmstudio_model": args.lmstudio_model,
                "lmstudio_url": args.lmstudio_url,
                "lmstudio_max_tokens": args.lmstudio_max_tokens,
                "teacher_url": args.teacher_url,
                "teacher_token": args.teacher_token,
                "student_name": args.student_name,
            }
            updates = {k: v for k, v in updates.items() if v is not None}
            if not updates:
                print("No config values provided.")
                return 1
            path = save_config(updates)
            print(f"Updated config: {path}")
            _print_config(load_config())
            return 0
    elif args.cmd == "report":
        store = ReportStore()
        reports_dir = store.paths["reports"]
        if args.report_cmd == "list":
            reports = sorted(reports_dir.glob("*.json"), reverse=True)[: args.limit]
            if not reports:
                print("No reports found.")
                return 0
            for r in reports:
                try:
                    data = json.loads(r.read_text(encoding="utf-8"))
                    status = "PASS" if data.get("passed") else "FAIL"
                    print(f"  [{status}] {data.get('lesson_id', '?')} — {r.stem}")
                except Exception:
                    print(f"  [????] {r.name}")
            return 0
        elif args.report_cmd == "view":
            if args.report_id:
                path = reports_dir / f"{args.report_id}.json"
                if not path.exists():
                    print(f"Report not found: {args.report_id}")
                    return 1
            else:
                reports = sorted(reports_dir.glob("*.json"), reverse=True)
                if not reports:
                    print("No reports found.")
                    return 0
                path = reports[0]
            data = json.loads(path.read_text(encoding="utf-8"))
            print(json.dumps(data, indent=2))
            return 0
        elif args.report_cmd == "latest":
            reports = sorted(reports_dir.glob("*.json"), reverse=True)
            if not reports:
                print("No reports found.")
                return 0
            data = json.loads(reports[0].read_text(encoding="utf-8"))
            print(json.dumps(data, indent=2))
            return 0
    elif args.cmd == "progress":
        from ..progress import ProgressTracker

        tracker = ProgressTracker()
        if args.lesson:
            stats = tracker.get_lesson_stats(args.lesson)
            print(f"  Lesson: {args.lesson}")
            print(f"    Total:  {stats['total']}")
            print(f"    Passed: {stats['passed']}")
            print(f"    Failed: {stats['failed']}")
        else:
            print("  No lesson specified. Use --lesson <id>.")
        tracker.close()
        return 0
    elif args.cmd == "sync-progress":
        return _handle_sync_progress()
    elif args.cmd == "quiz":
        return _handle_quiz(args)
    elif args.cmd == "instructions":
        return _handle_instructions(args)

    return 1
