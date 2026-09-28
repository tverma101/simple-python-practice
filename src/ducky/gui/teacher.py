"""Teacher-mode menus for the Ducky terminal UI."""

from __future__ import annotations

import json
import logging
import os
import shlex
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .widgets import (
    BOLD,
    DIM,
    GREEN,
    RED,
    RESET,
    STATUS_FAIL,
    STATUS_OFFLINE,
    STATUS_PASS,
    STATUS_PENDING,
    STATUS_RUNNING,
    # Reused by the Packs & Grading Workspace lab (moved here from
    # gui/student.py). Kept verbatim — student-themed styling intact.
    STUDENT_ACCENT,
    TEACHER_ACCENT,
    YELLOW,
    card,
    clear,
    copy_pack_to_student_packs,
    navigation_hint,
    show_notifications,
    status_dot,
    student_error,
    student_heading,
    student_info,
    student_prompt,
    student_success,
    teacher_banner,
    teacher_card,
    teacher_confirm,
    teacher_error,
    teacher_heading,
    teacher_info,
    teacher_prompt,
    teacher_separator,
    teacher_success,
    teacher_warning,
)

logger = logging.getLogger(__name__)


class TeacherMenuMixin:
    """Mixin providing teacher menus, server control, and LLM settings."""

    # ─── Teacher ─────────────────────────────────────────────

    @staticmethod
    def _teacher_timer_seconds(value: str) -> int:
        """Parse the compact timer syntax used by the quiz authoring TUI."""
        text = value.strip().lower()
        if not text or text == "0":
            return 0
        multiplier = 60
        if text.endswith("s"):
            multiplier = 1
            text = text[:-1].strip()
        elif text.endswith("m"):
            text = text[:-1].strip()
        seconds = int(text) * multiplier
        if seconds < 0 or seconds > 86400:
            raise ValueError("Time limit must be between 0 and 86400 seconds.")
        return seconds

    @staticmethod
    def _teacher_is_online(student: dict[str, Any] | None) -> bool:
        """Return the same five-minute online state shown to the teacher."""
        if not student or not student.get("last_seen"):
            return False
        try:
            last_seen = datetime.fromisoformat(str(student["last_seen"]))
            return (datetime.now(timezone.utc) - last_seen).total_seconds() < 300
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _teacher_report(submission: dict[str, Any] | None) -> dict[str, Any]:
        if not submission:
            return {}
        try:
            report = json.loads(submission.get("report_json") or "{}")
        except (TypeError, ValueError):
            return {}
        return report if isinstance(report, dict) else {}

    @classmethod
    def _teacher_result_label(cls, submission: dict[str, Any] | None) -> str:
        """Render a result state without leaking submission IDs."""
        if not submission:
            return "not submitted"
        report = cls._teacher_report(submission)
        if submission.get("kind") == "progress":
            return "in progress"
        if report.get("passed"):
            return "passed"
        if report:
            return "needs work"
        return str(submission.get("status") or "submitted")

    def _teacher_student_snapshot(
        self, db, class_id: str, student: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Build the teacher view from the same student-owned result records.

        This deliberately derives current lesson/result labels from reports
        rather than showing internal pack IDs. It keeps the one-to-one teacher
        dashboard aligned with what the student sees after running tests,
        taking a quiz, or syncing progress.
        """
        students = db.list_students(class_id)
        selected = student or (students[0] if students else None)
        submissions = db.list_submissions(class_id)
        token = selected.get("token") if selected else None
        own = [row for row in submissions if not token or row.get("token") == token]
        own.sort(key=lambda row: row.get("submitted_at", ""))
        packs = {row["pack_id"]: row for row in db.list_packs(class_id)}

        def lesson_label(row: dict[str, Any] | None) -> str:
            report = self._teacher_report(row)
            pack = packs.get(row.get("pack_id")) if row else None
            return str(
                report.get("lesson_title")
                or report.get("title")
                or (pack or {}).get("title")
                or report.get("lesson_id")
                or "No lesson yet"
            )

        latest_activity = own[-1] if own else None
        coding = [row for row in own if row.get("kind", "submission") == "submission"]
        quizzes = [row for row in own if row.get("kind") == "quiz"]
        progress = [row for row in own if row.get("kind") == "progress"]
        latest_coding = coding[-1] if coding else None
        latest_quiz = quizzes[-1] if quizzes else None
        current = progress[-1] if progress else latest_activity
        return {
            "student": selected,
            "students": students,
            "online": self._teacher_is_online(selected),
            "current_lesson": lesson_label(current),
            "latest_activity": latest_activity,
            "latest_coding": latest_coding,
            "latest_quiz": latest_quiz,
            "coding_label": lesson_label(latest_coding),
            "quiz_label": lesson_label(latest_quiz),
            "submissions": own,
        }

    def _teacher_menu(self):
        while True:
            clear()
            # Show queued notifications
            if self._notifications:
                show_notifications(self._notifications)
                self._notifications.clear()
            self._print_teacher_dashboard()
            navigation_hint("Teacher", "Dashboard")
            print()
            print(f"  {TEACHER_ACCENT}[1]{RESET}  Start class     — server, URL/code, connection check")
            print(f"  {TEACHER_ACCENT}[2]{RESET}  Student         — current lesson, sync, submissions, feedback")
            print(f"  {TEACHER_ACCENT}[3]{RESET}  Lessons         — preview, publish, replace, rollback")
            print(f"  {TEACHER_ACCENT}[4]{RESET}  Submissions     — pass, needs work, note, resubmit")
            print(f"  {TEACHER_ACCENT}[5]{RESET}  Announcement    — send and review seen state")
            print(f"  {TEACHER_ACCENT}[6]{RESET}  Backup/restore  — protect the local classroom database")
            print(f"  {TEACHER_ACCENT}[7]{RESET}  Settings        — hints and server controls")
            print(f"  {TEACHER_ACCENT}[M]{RESET}  More tools      — workspace, classes, quizzes")
            print(f"  {TEACHER_ACCENT}[0]{RESET}  Back")
            print()
            choice = teacher_prompt("Choose", "0")
            if choice == "1":
                if self.teacher_server and self.teacher_server.is_running():
                    self._teacher_server_control()
                else:
                    # Start class is the short path for a one-teacher/
                    # one-student classroom; the running-server screen remains
                    # available for stop/reveal actions.
                    self._teacher_quick_start()
            elif choice == "2":
                self._teacher_students()
            elif choice == "3":
                self._teacher_packs()
            elif choice == "4":
                self._teacher_submissions()
            elif choice == "5":
                self._teacher_announce()
            elif choice == "6":
                self._teacher_backup_restore()
            elif choice == "7":
                self._teacher_llm_settings()
            elif choice in ("m", "M"):
                self._teacher_more_tools()
            elif choice in ("?", "help"):
                teacher_info("Use 1-7 for classroom actions, M for authoring tools, and 0 to leave Teacher mode.")
                input("\n  Press Enter...")
            elif choice == "0":
                if self.teacher_server and self.teacher_server.is_running():
                    teacher_info("Server still running in background. Stop it from the menu to shut down.")
                break
            else:
                teacher_error("Choose one of the actions shown, M for more tools, or 0 to go back.")

    def _teacher_more_tools(self):
        """Keep legacy authoring utilities available without crowding the dashboard."""
        clear()
        teacher_heading("More teacher tools")
        navigation_hint("Teacher", "Dashboard / More")
        print(f"  {TEACHER_ACCENT}[1]{RESET}  Workspace")
        print(f"  {TEACHER_ACCENT}[2]{RESET}  Classes")
        print(f"  {TEACHER_ACCENT}[3]{RESET}  Author lessons, quizzes, and tests")
        print(f"  {TEACHER_ACCENT}[0]{RESET}  Back")
        choice = teacher_prompt("Choose", "0")
        if choice == "1":
            self._teacher_workspace()
        elif choice == "2":
            self._teacher_classes()
        elif choice == "3":
            self._teacher_authoring_menu()

    def _teacher_edit_file(self, path: Path) -> bool:
        """Open a teacher-owned text file in the configured editor."""
        editor = self._load_config().get("editor") or os.environ.get("VISUAL") or os.environ.get("EDITOR")
        if not editor:
            teacher_warning("No editor configured. Set EDITOR or use the TUI prompts.")
            return False
        try:
            subprocess.run([*shlex.split(editor), str(path)], check=False)
            return True
        except OSError as exc:
            teacher_error(f"Could not open editor: {exc}")
            return False

    def _teacher_authoring_menu(self):
        """Compact teacher authoring hub for the local one-to-one workflow."""
        while True:
            clear()
            teacher_heading("Authoring workspace")
            navigation_hint("Teacher", "More / Authoring")
            print(f"  {TEACHER_ACCENT}[1]{RESET}  New coding assignment")
            print(f"  {TEACHER_ACCENT}[2]{RESET}  Add or edit quiz")
            print(f"  {TEACHER_ACCENT}[3]{RESET}  Import quiz from local database")
            print(f"  {TEACHER_ACCENT}[4]{RESET}  Add coding test case")
            print(f"  {TEACHER_ACCENT}[5]{RESET}  Preview lesson")
            print(f"  {TEACHER_ACCENT}[6]{RESET}  Validate lesson")
            print(f"  {TEACHER_ACCENT}[7]{RESET}  Build and publish lesson")
            print(f"  {TEACHER_ACCENT}[8]{RESET}  Edit instructions or starter")
            print(f"  {TEACHER_ACCENT}[0]{RESET}  Back")
            print()
            choice = teacher_prompt("Choose", "0")
            if choice == "1":
                self._teacher_new_assignment()
            elif choice == "2":
                self._teacher_quiz_flow()
            elif choice == "3":
                self._teacher_import_quiz_database()
            elif choice == "4":
                self._teacher_add_test_case()
            elif choice == "5":
                self._teacher_preview_lesson()
            elif choice == "6":
                self._teacher_validate_lesson()
            elif choice == "7":
                self._teacher_build_publish()
            elif choice == "8":
                self._teacher_edit_lesson_files()
            elif choice == "0":
                return

    def _teacher_lesson_choices(self) -> list[Path]:
        from ..teacher_workspace import get_repo_assignments_dir

        bases = (
            self.paths["base"] / "teacher" / "workspace" / "lessons",
            get_repo_assignments_dir(),
        )
        found: dict[Path, Path] = {}
        for base in bases:
            if base.is_dir():
                for path in sorted(base.iterdir()):
                    if path.is_dir() and not path.name.startswith("."):
                        found[path.resolve()] = path
        return list(found.values())

    def _teacher_edit_lesson_files(self):
        """Open the common lesson files without leaving the authoring hub."""
        clear()
        teacher_heading("Edit lesson files")
        lesson = self._teacher_pick_lesson("Pick lesson")
        if not lesson:
            return
        starter_files = sorted((lesson / "starter").glob("*.py"))
        files = [
            ("1", "Instructions", lesson / "INSTRUCTIONS.md"),
            ("2", "Starter Python file", starter_files[0] if starter_files else lesson / "starter" / "solution.py"),
        ]
        print()
        for key, label, path in files:
            print(f"  {TEACHER_ACCENT}[{key}]{RESET}  {label:<22} {path.name}")
        print(f"  {TEACHER_ACCENT}[0]{RESET}  Back")
        choice = teacher_prompt("Open", "1")
        selected = next((path for key, _label, path in files if key == choice), None)
        if selected is None:
            return
        if not selected.exists():
            teacher_error(f"File not found: {selected}")
        else:
            self._teacher_edit_file(selected)
        input("\n  Press Enter...")

    def _teacher_pick_lesson(self, prompt_text: str = "Lesson") -> Path | None:
        lessons = self._teacher_lesson_choices()
        if not lessons:
            teacher_warning("No lesson folders found. Create an assignment first.")
            input("\n  Press Enter...")
            return None
        print(f"\n  {BOLD}Lesson folders:{RESET}")
        for index, path in enumerate(lessons, 1):
            starter_count = len(list((path / "starter").glob("*.py")))
            test_count = len(list((path / "tests").rglob("*.py")))
            quiz_state = "quiz" if (
                (path / "quizzes.json").is_file()
                or any((path / "quizzes").glob("*.json"))
            ) else "no quiz"
            display_name = path.name
            manifest_path = path / "manifest.json"
            if manifest_path.is_file():
                try:
                    manifest_data = json.loads(manifest_path.read_text(encoding="utf-8"))
                    display_name = str(manifest_data.get("title") or display_name)
                except (OSError, UnicodeError, json.JSONDecodeError, TypeError):
                    pass
            print(
                f"  {TEACHER_ACCENT}[{index}]{RESET}  {display_name:<30}"
                f" {starter_count} starter · {test_count} tests · {quiz_state}"
            )
        print(f"  {TEACHER_ACCENT}[c]{RESET}  Custom path")
        choice = teacher_prompt(prompt_text, "1")
        if choice.lower() == "c":
            path = Path(teacher_prompt("Lesson folder path")).expanduser()
        else:
            try:
                path = lessons[int(choice) - 1]
            except (ValueError, IndexError):
                teacher_error("Invalid lesson selection.")
                input("\n  Press Enter...")
                return None
        if not path.is_dir():
            teacher_error("Lesson folder not found.")
            input("\n  Press Enter...")
            return None
        return path

    def _teacher_new_assignment(self):
        from ..teacher_workspace import create_blank_assignment

        clear()
        teacher_heading("New coding assignment")
        name = teacher_prompt("Lesson folder name", "lesson_01_functions")
        title = teacher_prompt("Assignment title", "Functions")
        if not name:
            teacher_error("Lesson name is required.")
            input("\n  Press Enter...")
            return
        result = create_blank_assignment(name, title)
        if not result["ok"]:
            teacher_error(result["error"])
            input("\n  Press Enter...")
            return
        lesson = Path(result["path"])
        teacher_success(f"Created {lesson.name}.")
        print(f"  Starter: {lesson / 'starter' / 'solution.py'}")
        print(f"  Tests:   {lesson / 'tests'}")
        if teacher_confirm("Open instructions in your editor now?"):
            self._teacher_edit_file(lesson / "INSTRUCTIONS.md")
        if teacher_confirm("Open the starter Python file now?"):
            self._teacher_edit_file(lesson / "starter" / "solution.py")
        if teacher_confirm("Add a quiz to this lesson now?"):
            self._teacher_quiz_flow(lesson)
        input("\n  Press Enter...")

    def _teacher_add_test_case(self):
        from ..authoring import add_function_test_case

        clear()
        teacher_heading("Add coding test case")
        lesson = self._teacher_pick_lesson("Pick lesson")
        if not lesson:
            return
        function_name = teacher_prompt("Function name", "main")
        test_name = teacher_prompt("Test name", "basic_case")
        args = teacher_prompt("Arguments (Python tuple/list)", "()")
        expected = teacher_prompt("Expected value (Python literal)")
        description = teacher_prompt("Description", "")
        hidden = teacher_prompt("Mark as hidden? (y/n)", "n").lower() == "y"
        try:
            destination = add_function_test_case(
                lesson,
                function_name=function_name,
                args=args,
                expected=expected,
                name=test_name,
                description=description,
                hidden=hidden,
            )
            teacher_success(f"Added test case to {destination.name}.")
        except ValueError as exc:
            teacher_error(str(exc))
        input("\n  Press Enter...")

    def _teacher_import_quiz_database(self):
        """Preview and import questions from a local SQLite question bank."""
        from ..authoring import atomic_write_json, load_quiz_questions_from_database
        from ..quiz import validate_quiz_dict

        clear()
        teacher_heading("Import Quiz Questions")
        lesson_dir = self._teacher_pick_lesson("Pick lesson folder")
        if not lesson_dir:
            return
        database = Path(teacher_prompt("SQLite database path").strip()).expanduser()
        table = teacher_prompt("Table name (blank = auto-detect)", "").strip() or None
        limit_text = teacher_prompt("Maximum questions (blank = all)", "").strip()
        try:
            limit = int(limit_text) if limit_text else None
        except ValueError:
            teacher_error("Maximum questions must be a whole number.")
            input("\n  Press Enter...")
            return

        imported = load_quiz_questions_from_database(database, table=table, limit=limit)
        if imported.errors:
            for message in imported.errors:
                teacher_error(message)
        if imported.warnings:
            for message in imported.warnings:
                teacher_warning(message)
        if not imported.ok:
            input("\n  Press Enter...")
            return

        print(f"\n  Found {imported.quiz_count} question(s) in table '{imported.table}'.")
        for index, question in enumerate(imported.questions[:8], 1):
            print(f"  {TEACHER_ACCENT}{index}.{RESET} [{question['type']}] {question['prompt']}")
        if imported.quiz_count > 8:
            print(f"  … and {imported.quiz_count - 8} more")
        print()
        if not teacher_confirm("Import these questions into the lesson quiz?"):
            teacher_info("Import cancelled — no files changed.")
            input("\n  Press Enter...")
            return

        destination = lesson_dir / "quizzes.json"
        existing = {}
        if destination.is_file():
            from ..authoring import load_quiz_source

            existing, errors = load_quiz_source(destination, lesson_id=lesson_dir.name)
            if errors:
                teacher_error("The existing quiz is invalid; fix or remove it before importing.")
                for message in errors:
                    print(f"  {YELLOW}- {message}{RESET}")
                input("\n  Press Enter...")
                return
            if not teacher_confirm("Replace the existing lesson quiz with the imported questions?"):
                teacher_info("Existing quiz kept unchanged.")
                input("\n  Press Enter...")
                return

        quiz = {
            "schema_version": 1,
            "quiz_id": str(existing.get("quiz_id") if existing else f"quiz.{lesson_dir.name}"),
            "lesson_id": str(existing.get("lesson_id") if existing else lesson_dir.name),
            "title": str(existing.get("title") if existing else f"{lesson_dir.name} Quiz"),
            "description": str(existing.get("description") if existing else f"Imported from {database.name}"),
            "source": f"local SQLite database: {database.name}",
            "shuffle_questions": bool(existing.get("shuffle_questions", False)),
            "review_mode": str(existing.get("review_mode", "missed_first")),
            "questions": imported.questions,
        }
        ok, errors, _warnings = validate_quiz_dict(quiz)
        if not ok:
            teacher_error("Imported questions failed Ducky validation; no file was changed.")
            for message in errors:
                print(f"  {YELLOW}- {message}{RESET}")
            input("\n  Press Enter...")
            return
        try:
            atomic_write_json(destination, quiz)
        except OSError as exc:
            teacher_error(f"Quiz import could not be saved: {exc}")
        else:
            teacher_success(f"Imported {len(imported.questions)} question(s) to {destination}")
        input("\n  Press Enter...")

    def _teacher_preview_lesson(self):
        from ..authoring import preview_lesson

        clear()
        teacher_heading("Lesson preview")
        lesson = self._teacher_pick_lesson("Pick lesson")
        if not lesson:
            return
        preview = preview_lesson(lesson)
        teacher_card("Publish preview", [
            f"{TEACHER_ACCENT}Title:{RESET} {preview['title']}",
            f"{TEACHER_ACCENT}Lesson:{RESET} {preview['lesson_id']}",
            f"{TEACHER_ACCENT}Entry:{RESET} {preview['entry_file'] or 'missing'}",
            f"{TEACHER_ACCENT}Tests:{RESET} {preview['test_files']}",
            f"{TEACHER_ACCENT}Quiz questions:{RESET} {preview['quiz_questions']}",
            f"{TEACHER_ACCENT}Quiz timer:{RESET} "
            f"{preview['quiz_time_limit_seconds'] // 60} minute(s)"
            if preview["quiz_time_limit_seconds"]
            else f"{TEACHER_ACCENT}Quiz timer:{RESET} untimed",
        ])
        if preview["issues"]:
            teacher_warning("Issues:")
            for issue in preview["issues"]:
                print(f"  {RED}- {issue}{RESET}")
        if preview.get("warnings"):
            teacher_warning("Warnings:")
            for warning in preview["warnings"]:
                print(f"  {YELLOW}- {warning}{RESET}")
        if not preview["issues"]:
            teacher_success("Ready to build and publish.")
        input("\n  Press Enter...")

    def _teacher_validate_lesson(self):
        from ..authoring import preview_lesson

        clear()
        teacher_heading("Validate lesson")
        lesson = self._teacher_pick_lesson("Pick lesson")
        if not lesson:
            return
        preview = preview_lesson(lesson)
        if preview["ok"]:
            teacher_success("Lesson structure and quiz are valid.")
        else:
            teacher_error("Lesson needs attention:")
            for issue in preview["issues"]:
                print(f"  {RED}- {issue}{RESET}")
        input("\n  Press Enter...")

    def _teacher_build_publish(self):
        from ..lesson_builder import build_pack_from_folder

        clear()
        teacher_heading("Build and publish lesson")
        lesson = self._teacher_pick_lesson("Pick lesson")
        if not lesson:
            return
        from ..teacher_workspace import get_built_packs_dir

        output = get_built_packs_dir() / f"{lesson.name}.duckypack"
        try:
            pack = build_pack_from_folder(lesson, output)
            from ..quiz import load_quiz_from_pack

            built_quiz = load_quiz_from_pack(pack)
            quiz_summary = "no teacher quiz"
            if built_quiz is not None:
                quiz_summary = f"{len(built_quiz.questions)} questions"
                if built_quiz.time_limit_seconds:
                    quiz_summary += f", {built_quiz.time_limit_seconds // 60} minute limit"
            teacher_success(
                f"Built {pack.name} ({pack.stat().st_size} bytes) · {quiz_summary}."
            )
            if teacher_confirm("Publish this pack to the running class?"):
                from ..pack import read_manifest

                manifest = read_manifest(pack)
                self._add_pack_to_server(pack, manifest.lesson_id, manifest.title)
                teacher_info("The student will receive this quiz update on the next sync.")
        except Exception as exc:
            logger.exception("Teacher build failed")
            teacher_error(f"Build failed: {exc}")
        input("\n  Press Enter...")

    def _print_teacher_dashboard(self):
        from ..authoring import preview_lesson
        from ..classroom import ClassroomDB
        from ..teacher_workspace import list_teacher_lessons

        teacher_banner()
        teacher_separator()
        print(f"  {BOLD}Ducky Classroom{RESET}")
        teacher_separator()

        db = ClassroomDB()
        try:
            classes = db.list_classes()
            class_name = classes[0]["name"] if classes else "No class"
            class_id = classes[0]["class_id"] if classes else ""
            packs = db.list_packs(class_id) if class_id else []
            submissions = db.list_submissions(class_id) if class_id else []
            announcements = db.list_announcements(class_id) if class_id else []
            snapshot = self._teacher_student_snapshot(db, class_id) if class_id else {
                "student": None,
                "online": False,
                "current_lesson": "No lesson yet",
                "latest_coding": None,
                "latest_quiz": None,
                "coding_label": "No coding result",
                "quiz_label": "No quiz result",
            }
            unseen_announcements = sum(1 for announcement in announcements if not announcement.get("seen_at"))
            coding_review = sum(
                1 for submission in submissions
                if submission.get("kind", "submission") == "submission"
                and submission.get("status", "submitted") in {"submitted", "resubmit", "needs_work"}
            )
            quiz_review = sum(
                1 for submission in submissions
                if submission.get("kind") == "quiz"
                and submission.get("status", "submitted") == "submitted"
            )

            # This is intentionally a one-student classroom. Keep the count
            # for existing statistics, but render the named student below.
            now = datetime.now(timezone.utc)
            # Recent submissions (last 24 hours)
            recent_cutoff = f"{(now - timedelta(hours=24)).isoformat()}"
            recent = [s for s in submissions if s.get("submitted_at", "") > recent_cutoff]
        finally:
            db.close()

        lessons = list_teacher_lessons()
        lessons_needing_attention = 0
        invalid_quizzes = 0
        for lesson in lessons:
            if lesson.get("has_issues"):
                lessons_needing_attention += 1
            try:
                preview = preview_lesson(lesson["dir_path"])
                if any(issue.startswith("Quiz:") for issue in preview["issues"]):
                    invalid_quizzes += 1
            except (OSError, TypeError):
                lessons_needing_attention += 1

        # Server status card
        if self.teacher_server and self.teacher_server.is_running():
            port = self.teacher_server.port
            code = self.teacher_server.get_access_code()
            url = self.teacher_server.get_url()
            teacher_card("Server", [
                f"{STATUS_RUNNING}● Running{RESET} on port {port}",
                f"{TEACHER_ACCENT}Access Code:{RESET} {BOLD}{code}{RESET}",
                f"{TEACHER_ACCENT}URL:{RESET} {url}",
            ])
        else:
            teacher_card("Server", [f"{STATUS_OFFLINE}○ Not running{RESET}"])

        # Quick stats card
        teacher_card("Quick Stats", [
            f"{status_dot(snapshot['online'])} "
            f"{snapshot['student'].get('name', 'No student') if snapshot['student'] else 'No student'} "
            f"({'online' if snapshot['online'] else 'offline'})    "
            f"{status_dot(len(recent) > 0)} {len(recent)} recent submissions",
            f"{status_dot(len(packs) > 0)} {len(packs)} assigned lesson(s)     "
            f"{status_dot(len(announcements) > 0)} {len(announcements)} announcements",
            f"{TEACHER_ACCENT}Announcement state:{RESET} {unseen_announcements} not yet seen",
            f"{TEACHER_ACCENT}Needs review:{RESET} {coding_review} coding / {quiz_review} quiz",
            f"{TEACHER_ACCENT}Authoring:{RESET} {lessons_needing_attention} lessons need attention / {invalid_quizzes} invalid quizzes",
        ])

        teacher_card("Student activity", [
            f"{TEACHER_ACCENT}Current lesson:{RESET} {snapshot['current_lesson']}",
            f"{TEACHER_ACCENT}Latest coding:{RESET} {snapshot['coding_label']} — "
            f"{self._teacher_result_label(snapshot['latest_coding'])}",
            f"{TEACHER_ACCENT}Latest quiz:{RESET} {snapshot['quiz_label']} — "
            f"{self._teacher_result_label(snapshot['latest_quiz'])}",
        ])

        # Class info card
        config = self._load_config()
        llm_enabled = config.get("llm_backend", "none") != "none"
        if llm_enabled:
            backend = config.get("llm_backend", "none")
            model = config.get(f"{backend}_model", "")
            llm_status = f"{STATUS_RUNNING}● Enabled{RESET} ({backend} / {model})"
        else:
            llm_status = f"{STATUS_OFFLINE}○ Disabled{RESET}"

        teacher_card(f"Class: {class_name}", [
            f"{TEACHER_ACCENT}Student:{RESET} {snapshot['student'].get('name', 'Not connected') if snapshot['student'] else 'Not connected'}  |  "
            f"{TEACHER_ACCENT}Lessons:{RESET} {len(packs)}  |  "
            f"{TEACHER_ACCENT}Submissions:{RESET} {len(submissions)}",
            f"{TEACHER_ACCENT}LLM Hints:{RESET} {llm_status}",
        ])

    def _teacher_dashboard(self):
        clear()
        self._print_teacher_dashboard()
        print()
        print(f"  {TEACHER_ACCENT}[s]{RESET}  Start server       {TEACHER_ACCENT}[p]{RESET}  Stop server")
        print(f"  {TEACHER_ACCENT}[a]{RESET}  Quick announce     {TEACHER_ACCENT}[c]{RESET}  Quick start server with new class")
        print(f"  {TEACHER_ACCENT}[r]{RESET}  Refresh dashboard  {TEACHER_ACCENT}[0]{RESET}  Back")
        print()
        choice = teacher_prompt("Choose", "0")
        if choice == "s":
            self._teacher_start_server()
        elif choice == "p":
            self._teacher_stop_server()
        elif choice == "a":
            self._teacher_announce()
        elif choice == "c":
            self._teacher_quick_start()
        elif choice == "r":
            pass  # Just refreshes on next loop iteration

    def _teacher_students(self):
        from ..classroom import ClassroomDB

        clear()
        teacher_heading("Students")
        navigation_hint("Teacher", "Dashboard / Student")
        db = ClassroomDB()
        try:
            classes = db.list_classes()
            if not classes:
                teacher_info("No classes. Create one first.")
                input("  Press Enter...")
                return

            if len(classes) == 1:
                class_id = classes[0]["class_id"]
                class_name = classes[0]["name"]
            else:
                print(f"\n  {BOLD}Classes:{RESET}")
                for i, c in enumerate(classes, 1):
                    print(f"  {TEACHER_ACCENT}[{i}]{RESET}  {c['name']} ({c['class_id']})")
                print()
                choice = teacher_prompt("Class number", "1")
                try:
                    idx = int(choice) - 1
                    class_id = classes[idx]["class_id"]
                    class_name = classes[idx]["name"]
                except (ValueError, IndexError):
                    class_id = classes[0]["class_id"]
                    class_name = classes[0]["name"]

            students = db.list_students(class_id)
            if not students:
                teacher_info("No students connected yet.")
            else:
                # Count online students
                now = datetime.now(timezone.utc)
                online_count = 0
                for s in students:
                    last_seen = s.get("last_seen", "")
                    if last_seen:
                        try:
                            ls = datetime.fromisoformat(last_seen)
                            if (now - ls).total_seconds() < 300:
                                online_count += 1
                        except (ValueError, TypeError):
                            pass

                print(f"\n  {BOLD}{class_name} — {len(students)} students ({online_count} online){RESET}")
                print(f"  {'─' * 60}")

                for i, s in enumerate(students, 1):
                    last_seen = s.get("last_seen", "?")
                    is_online = False
                    if last_seen:
                        try:
                            ls = datetime.fromisoformat(last_seen)
                            is_online = (now - ls).total_seconds() < 300
                        except (ValueError, TypeError):
                            pass

                    dot = STATUS_RUNNING if is_online else STATUS_OFFLINE
                    name = s.get('name', '?')
                    connected = s.get('connected_at', '?')[:19]
                    print(f"  {dot} {i}. {name:<20} Connected: {connected}")

                print()
                choice = teacher_prompt("View student details (number) or 0 to go back", "0")
                if choice != "0":
                    try:
                        idx = int(choice) - 1
                        if 0 <= idx < len(students):
                            self._teacher_student_detail(students[idx], class_id)
                    except (ValueError, IndexError):
                        pass
        finally:
            db.close()

    def _teacher_student_detail(self, student: dict, class_id: str):
        """Show detailed information about a single student."""
        from ..classroom import ClassroomDB

        clear()
        teacher_heading(f"Student: {student.get('name', '?')}")
        navigation_hint("Teacher", "Dashboard / Student / Detail")

        db = ClassroomDB()
        try:
            snapshot = self._teacher_student_snapshot(db, class_id, student)
            is_online = snapshot["online"]

            teacher_card("Student Info", [
                f"{TEACHER_ACCENT}Name:{RESET} {student.get('name', '?')}",
                f"{TEACHER_ACCENT}Status:{RESET} {status_dot(is_online)} {'Online' if is_online else 'Offline'}",
                f"{TEACHER_ACCENT}Connected:{RESET} {student.get('connected_at', '?')[:19]}",
                f"{TEACHER_ACCENT}Last Seen:{RESET} {student.get('last_seen', '?')[:19]}",
                f"{TEACHER_ACCENT}Current lesson:{RESET} {snapshot['current_lesson']}",
            ])

            teacher_card("Latest results", [
                f"{TEACHER_ACCENT}Coding:{RESET} {snapshot['coding_label']} — "
                f"{self._teacher_result_label(snapshot['latest_coding'])}",
                f"{TEACHER_ACCENT}Quiz:{RESET} {snapshot['quiz_label']} — "
                f"{self._teacher_result_label(snapshot['latest_quiz'])}",
            ])

            # Submissions for this student
            student_subs = snapshot["submissions"]

            if student_subs:
                print(f"\n  {BOLD}Submissions ({len(student_subs)}):{RESET}")
                print(f"  {'─' * 50}")
                for s in student_subs[-5:]:  # Show last 5
                    try:
                        report = json.loads(s.get("report_json", "{}"))
                        status = STATUS_PASS if report.get("passed") else STATUS_FAIL
                        status_text = f"{status}{'PASS' if report.get('passed') else 'FAIL'}{RESET}"
                    except Exception:
                        status_text = f"{STATUS_PENDING}N/A{RESET}"

                    packs = db.list_packs(class_id)
                    pack_names = {p["pack_id"]: p["title"] for p in packs}
                    if s.get("kind") == "quiz":
                        pack_title = report.get("lesson_title") or "Quiz result"
                    else:
                        pack_id = s.get("pack_id") or ""
                        pack_title = pack_names.get(pack_id, "Coding lesson")
                    review = s.get("status", "submitted")
                    submitted = s.get("submitted_at", "?")[:19]
                    print(f"    {status_text} {pack_title:<25} {review:<12} {submitted}")
            else:
                print(f"\n  {DIM}No submissions yet.{RESET}")
        finally:
            db.close()

        input("\n  Press Enter...")

    def _teacher_classes(self):
        from ..classroom import ClassroomDB

        while True:
            clear()
            teacher_heading("Classes")

            db = ClassroomDB()
            try:
                classes = db.list_classes()
            finally:
                db.close()

            if not classes:
                teacher_info("No classes yet. Create one to get started.")
            else:
                print(f"\n  {BOLD}{'#':<4} {'Name':<25} {'Class ID':<20} {'Access Code':<12} {'Students':<8}{RESET}")
                print(f"  {'─' * 4} {'─' * 25} {'─' * 20} {'─' * 12} {'─' * 8}")

                db = ClassroomDB()
                try:
                    for i, c in enumerate(classes, 1):
                        students = db.list_students(c["class_id"])
                        print(f"  {TEACHER_ACCENT}[{i}]{RESET}  {c['name']:<25} {c['class_id']:<20} {c['access_code']:<12} {len(students):<8}")
                finally:
                    db.close()

            print()
            print(f"  {TEACHER_ACCENT}[n]{RESET}  New class    {TEACHER_ACCENT}[d <n>]{RESET}  Delete class    {TEACHER_ACCENT}[0]{RESET}  Back")
            print()
            choice = teacher_prompt("Choose", "0")

            if choice == "0":
                break
            elif choice == "n":
                self._teacher_create_class()
            elif choice.startswith("d"):
                parts = choice.split()
                if len(parts) >= 2:
                    try:
                        idx = int(parts[1]) - 1
                        if 0 <= idx < len(classes):
                            if teacher_confirm(f"Delete class '{classes[idx]['name']}' and all its data?"):
                                db = ClassroomDB()
                                try:
                                    db.delete_class(classes[idx]["class_id"])
                                    teacher_success(f"Deleted: {classes[idx]['name']}")
                                finally:
                                    db.close()
                    except (ValueError, IndexError):
                        teacher_error("Invalid number.")
            else:
                try:
                    idx = int(choice) - 1
                    if 0 <= idx < len(classes):
                        self._teacher_class_detail(classes[idx])
                except (ValueError, IndexError):
                    teacher_error("Invalid number.")

    def _teacher_create_class(self):
        clear()
        teacher_heading("Create New Class")

        name = teacher_prompt("Class name", "My Class")
        class_id = teacher_prompt("Class ID (unique identifier)", name.lower().replace(" ", "_"))

        if not name or not class_id:
            teacher_error("Name and Class ID are required.")
            input("  Press Enter...")
            return

        from ..classroom import ClassroomDB
        db = ClassroomDB()
        try:
            result = db.create_class(class_id, name)
            teacher_success(f"Class created: {name}")
            print(f"\n  {BOLD}Class ID:{RESET} {result['class_id']}")
            print(f"  {BOLD}Access Code:{RESET} {result['access_code']}")
            print("\n  Share the access code with your students to let them connect.")
        except ValueError as e:
            teacher_error(str(e))
        finally:
            db.close()
        input("\n  Press Enter...")

    def _teacher_class_detail(self, class_info: dict):
        """Show details for a single class."""
        clear()
        teacher_heading(f"Class: {class_info.get('name', '?')}")

        from ..classroom import ClassroomDB
        db = ClassroomDB()
        try:
            class_id = class_info["class_id"]
            students = db.list_students(class_id)
            packs = db.list_packs(class_id)
            submissions = db.list_submissions(class_id)

            teacher_card(class_info["name"], [
                f"{TEACHER_ACCENT}Class ID:{RESET} {class_id}",
                f"{TEACHER_ACCENT}Access Code:{RESET} {BOLD}{class_info['access_code']}{RESET}",
                f"{TEACHER_ACCENT}Created:{RESET} {class_info.get('created_at', '?')[:19]}",
                f"{TEACHER_ACCENT}Students:{RESET} {len(students)}",
                f"{TEACHER_ACCENT}Packs:{RESET} {len(packs)}",
                f"{TEACHER_ACCENT}Submissions:{RESET} {len(submissions)}",
            ])

            if packs:
                print(f"\n  {BOLD}Packs:{RESET}")
                for p in packs[:5]:
                    print(f"    {TEACHER_ACCENT}•{RESET} {p['title']} (v{p['version']})")
                if len(packs) > 5:
                    print(f"    {DIM}... and {len(packs) - 5} more{RESET}")

            if students:
                print(f"\n  {BOLD}Students:{RESET}")
                now = datetime.now(timezone.utc)
                for s in students:
                    last_seen = s.get("last_seen", "")
                    is_online = False
                    if last_seen:
                        try:
                            ls = datetime.fromisoformat(last_seen)
                            is_online = (now - ls).total_seconds() < 300
                        except (ValueError, TypeError):
                            pass
                    dot = STATUS_RUNNING if is_online else STATUS_OFFLINE
                    print(f"    {dot} {s.get('name', '?')}")

        finally:
            db.close()
        input("\n  Press Enter...")

    def _teacher_workspace(self):
        from ..lesson_builder import build_all_packs
        from ..teacher_workspace import (
            create_blank_assignment,
            delete_assignment,
            get_built_packs_dir,
            get_lessons_dir,
            get_teacher_workspace_status,
            list_built_packs,
            list_teacher_lessons,
            sync_from_repo,
        )

        while True:
            clear()
            teacher_heading("Teacher Workspace")

            status = get_teacher_workspace_status()
            print(f"  {BOLD}Workspace:{RESET} {status['workspace_dir']}")
            print(
                f"  {BOLD}Lessons:{RESET}   {status['valid_lessons']} valid / {status['total_lessons']} total"
                + (
                    f"  {YELLOW}{status['lessons_with_issues']} with issues{RESET}"
                    if status["lessons_with_issues"]
                    else ""
                )
            )
            print(f"  {BOLD}Built Packs:{RESET} {status['total_packs']}")
            if status["repo_exists"]:
                print(f"  {BOLD}Repo:{RESET}       {status['repo_dir']} ({status['repo_lessons']} lessons)")

            lessons = list_teacher_lessons()
            if lessons:
                print(f"\n  {BOLD}Lesson Folders:{RESET}")
                for i, lesson in enumerate(lessons, 1):
                    has_issues = lesson.get("has_issues", False)
                    issues = lesson.get("issues", [])
                    if has_issues:
                        icon = f"{RED}✗{RESET}"
                    elif lesson.get("is_valid"):
                        icon = f"{GREEN}✓{RESET}"
                    else:
                        icon = f"{YELLOW}?{RESET}"
                    title = lesson.get("title", lesson.get("dir_name", "?"))
                    entry = lesson.get("entry_file", "")
                    print(f"    {icon} [{i}] {title:<30} entry={entry}")
                    for issue in issues:
                        print(f"         {YELLOW}└─ {issue}{RESET}")
            else:
                print(f"\n  {DIM}No lesson folders found.{RESET}")
                print(f"  {DIM}Place folders in: {status['lessons_dir']}{RESET}")
                print(f"  {DIM}Or use [n] to create a blank assignment.{RESET}")

            packs = list_built_packs()
            if packs:
                print(f"\n  {BOLD}Built Packs:{RESET}")
                for i, p in enumerate(packs, 1):
                    print(f"    [{i}] {p['lesson_id']:<30} {p['title']}")

            print()
            print(f"  {TEACHER_ACCENT}[n]{RESET}  New blank assignment  {TEACHER_ACCENT}[b]{RESET}  Build all packs")
            print(f"  {TEACHER_ACCENT}[i]{RESET}  Import from ZIP/folder {TEACHER_ACCENT}[a]{RESET}  Add pack to server")
            print(f"  {TEACHER_ACCENT}[s]{RESET}  Sync from repo        {TEACHER_ACCENT}[d <n>]{RESET} Delete lesson")
            print(f"  {TEACHER_ACCENT}[o]{RESET}  Open workspace folder")
            print(f"  {TEACHER_ACCENT}[0]{RESET}  Back")
            print()
            choice = teacher_prompt("Choose", "0")

            if choice == "n":
                clear()
                teacher_heading("New Blank Assignment")
                name = teacher_prompt("Lesson folder name (e.g. lesson_03_loops)")
                title = teacher_prompt("Title (e.g. Loops)")
                if not name:
                    teacher_error("Name is required.")
                    input("  Press Enter...")
                    continue
                result = create_blank_assignment(name, title)
                if result["ok"]:
                    teacher_success(f"Created: {result['path']}")
                    print(f"  Edit files in: {result['path']}")
                else:
                    teacher_error(result["error"])
                input("  Press Enter...")
            elif choice == "i":
                clear()
                teacher_heading("Import Assignment")
                print(f"  {TEACHER_ACCENT}[1]{RESET}  Scan Downloads for ZIPs")
                print(f"  {TEACHER_ACCENT}[2]{RESET}  Enter path manually")
                print(f"  {TEACHER_ACCENT}[0]{RESET}  Back")
                print()
                import_choice = teacher_prompt("Choose", "0")

                if import_choice == "0":
                    continue
                elif import_choice == "1":
                    from ..importer import import_assignment, scan_downloads_for_zips

                    zips = scan_downloads_for_zips()
                    if not zips:
                        teacher_info("No assignment ZIPs found in ~/Downloads")
                        input("  Press Enter...")
                        continue

                    print(f"\n  Found {len(zips)} assignment ZIP(s) in ~/Downloads:\n")
                    for i, z in enumerate(zips, 1):
                        tests_icon = f"{GREEN}✓{RESET} tests" if z["has_tests"] else f"{RED}✗{RESET} tests"
                        sol_icon = f"{GREEN}✓{RESET} solution" if z["has_solution"] else f"{DIM}○{RESET} solution"
                        print(
                            f"    {TEACHER_ACCENT}[{i}]{RESET} {z['name']:<40} {z['size_mb']}MB  {z['file_count']} files  {tests_icon}  {sol_icon}"
                        )
                    print(f"    {TEACHER_ACCENT}[0]{RESET}  Back")
                    print()

                    zip_choice = teacher_prompt("ZIP number to import", "0")
                    if zip_choice == "0":
                        continue

                    try:
                        zip_idx = int(zip_choice) - 1
                        if 0 <= zip_idx < len(zips):
                            source_path = zips[zip_idx]["path"]
                        else:
                            teacher_error(f"Invalid number. Enter 1-{len(zips)} or 0.")
                            input("  Press Enter...")
                            continue
                    except ValueError:
                        teacher_error("Please enter a number.")
                        input("  Press Enter...")
                        continue

                    packs_dir = get_built_packs_dir()
                    print(f"\n  Importing from: {source_path.name}...")
                    result = import_assignment(source_path, output_dir=packs_dir)
                    if result.get("ok"):
                        if "pack_path" in result:
                            teacher_success(f"Imported: {result['lesson_id']} -> {result['pack_path'].name}")
                            copied = copy_pack_to_student_packs(result["pack_path"])
                            if copied:
                                teacher_success(f"Also available in student packs: {copied.name}")
                        elif "packs" in result:
                            for p in result["packs"]:
                                if p.get("ok"):
                                    teacher_success(f"  Imported: {p['lesson_id']}")
                                    copy_pack_to_student_packs(p["pack_path"])
                                else:
                                    teacher_error(f"  Failed: {p.get('error', '?')}")
                            teacher_success(f"Imported {result['count']} assignment(s).")
                        for w in result.get("warnings", []):
                            print(f"  {YELLOW}Warning:{RESET} {w}")
                    else:
                        teacher_error(f"Import failed: {result.get('error', 'unknown error')}")
                    input("\n  Press Enter...")

                elif import_choice == "2":
                    source = teacher_prompt("Path to assignment ZIP or folder")
                    if not source:
                        input("  Press Enter...")
                        continue
                    source_path = Path(source)
                    if not source_path.exists():
                        teacher_error(f"Not found: {source}")
                        input("  Press Enter...")
                        continue
                    from ..importer import import_assignment

                    packs_dir = get_built_packs_dir()
                    print(f"\n  Importing from: {source_path.name}...")
                    result = import_assignment(source_path, output_dir=packs_dir)
                    if result.get("ok"):
                        if "pack_path" in result:
                            teacher_success(f"Imported: {result['lesson_id']} -> {result['pack_path'].name}")
                            copied = copy_pack_to_student_packs(result["pack_path"])
                            if copied:
                                teacher_success(f"Also available in student packs: {copied.name}")
                        elif "packs" in result:
                            for p in result["packs"]:
                                if p.get("ok"):
                                    teacher_success(f"  Imported: {p['lesson_id']}")
                                    copy_pack_to_student_packs(p["pack_path"])
                                else:
                                    teacher_error(f"  Failed: {p.get('error', '?')}")
                            teacher_success(f"Imported {result['count']} assignment(s).")
                        for w in result.get("warnings", []):
                            print(f"  {YELLOW}Warning:{RESET} {w}")
                    else:
                        teacher_error(f"Import failed: {result.get('error', 'unknown error')}")
                    input("\n  Press Enter...")
            elif choice == "b":
                clear()
                print("\n  Building packs from lessons...\n")
                lessons_dir = get_lessons_dir()
                packs_dir = get_built_packs_dir()
                results = build_all_packs(lessons_dir, packs_dir)
                if not results:
                    teacher_info("No valid lessons found to build.")
                else:
                    for item in results:
                        if item.get("pack_path"):
                            teacher_success(f"  Built: {item['lesson_id']} -> {item['pack_path'].name}")
                        else:
                            teacher_error(f"  Failed: {item['lesson_id']} - {item.get('status', '?')}")
                input("\n  Press Enter...")
            elif choice == "a":
                packs = list_built_packs()
                if not packs:
                    teacher_info("No built packs. Build first.")
                    input("  Press Enter...")
                    continue
                while True:
                    print("\n  Built packs:")
                    for i, p in enumerate(packs, 1):
                        print(f"    [{i}] {p['lesson_id']} — {p['title']}")
                    print(f"    {TEACHER_ACCENT}[0]{RESET}  Back")
                    num = teacher_prompt("Pack number to add to server", "0")
                    if num == "0":
                        break
                    try:
                        idx = int(num) - 1
                        if 0 <= idx < len(packs):
                            pack_file = packs[idx]["pack_file"]
                            lesson_id = packs[idx]["lesson_id"]
                            title = packs[idx]["title"]
                            self._add_pack_to_server(pack_file, lesson_id, title)
                            break
                        else:
                            teacher_error(f"Invalid number. Enter 1-{len(packs)} or 0.")
                    except ValueError:
                        teacher_error("Invalid number. Enter a number.")
                input("  Press Enter...")
            elif choice == "s":
                clear()
                teacher_heading("Sync from Repo")
                result = sync_from_repo()
                if result["ok"]:
                    if result["synced"]:
                        teacher_success(f"Synced {result['synced']} new lesson(s)")
                    if result["updated"]:
                        teacher_success(f"Updated {result['updated']} lesson(s)")
                    if result["skipped"]:
                        teacher_info(f"Skipped {result['skipped']} (unchanged)")
                    if result.get("deleted", 0):
                        teacher_info(f"Deleted {result['deleted']} lesson(s) no longer in repo")
                    if not result["synced"] and not result["updated"] and not result["deleted"]:
                        teacher_info("All lessons up to date")
                    for err in result["errors"]:
                        teacher_error(f"  {err}")
                else:
                    teacher_error(result.get("error", "Sync failed"))
                input("\n  Press Enter...")
            elif choice.startswith("d"):
                parts = choice.split()
                if len(parts) >= 2:
                    try:
                        idx = int(parts[1]) - 1
                        if 0 <= idx < len(lessons):
                            name = lessons[idx].get("dir_name", "")
                            confirm_choice = teacher_prompt(f"Delete '{name}'? (y/n)", "n")
                            if confirm_choice.lower() == "y":
                                result = delete_assignment(name)
                                if result["ok"]:
                                    teacher_success(f"Deleted: {name}")
                                else:
                                    teacher_error(result["error"])
                        else:
                            teacher_error("Invalid number.")
                    except (ValueError, IndexError):
                        teacher_error("Invalid number.")
                else:
                    teacher_error("Usage: d <number>")
                input("  Press Enter...")
            elif choice == "o":
                import subprocess

                subprocess.Popen(["open", str(get_lessons_dir())])
                input("  Press Enter...")
            elif choice == "0":
                break

    # ─── Packs & Grading Workspace (teacher-only lab) ──────
    # These screens load a .duckypack plus an optional student
    # submission and run tests against them — a teacher grading
    # workflow. They lived in gui/student.py historically and so
    # shipped inside the student distribution even though no
    # student menu reaches them. Moved here so build_student.sh
    # (which strips gui/teacher.py) keeps them out of student builds.

    # ─── Packs ──────────────────────────────────────────────

    def _packs_menu(self):
        while True:
            clear()
            student_heading("Packs")
            print(f"  {STUDENT_ACCENT}[1]{RESET}  Create a .duckypack")
            print(f"  {STUDENT_ACCENT}[2]{RESET}  Inspect a .duckypack")
            print(f"  {STUDENT_ACCENT}[3]{RESET}  List packs in examples/")
            print(f"  {STUDENT_ACCENT}[0]{RESET}  Back")
            print()
            choice = student_prompt("Choose", "0")
            if choice == "1":
                self._pack_create()
            elif choice == "2":
                self._pack_inspect()
            elif choice == "3":
                self._pack_list()
            elif choice == "0":
                break

    def _pack_create(self):
        from ..pack import DuckyManifest, create_duckypack
        from ..teacher_workspace import get_repo_assignments_dir

        student_heading("Create .duckypack")
        workspace = self.paths["base"] / "teacher" / "workspace" / "lessons"
        assignments = get_repo_assignments_dir()
        lesson_dirs = []
        if workspace.is_dir():
            for d in sorted(workspace.iterdir()):
                if d.is_dir() and not d.name.startswith("."):
                    lesson_dirs.append(d)
        if assignments.is_dir():
            for d in sorted(assignments.iterdir()):
                if d.is_dir() and not d.name.startswith("."):
                    lesson_dirs.append(d)

        source = ""
        if lesson_dirs:
            print(f"\n  {BOLD}Available lesson folders:{RESET}")
            for i, d in enumerate(lesson_dirs, 1):
                has_tests = (d / "tests").is_dir()
                status = f"{GREEN}ok{RESET}" if has_tests else f"{YELLOW}no tests{RESET}"
                print(f"  {STUDENT_ACCENT}[{i}]{RESET}  {d.name}  {DIM}{status}{RESET}")
            print(f"  {STUDENT_ACCENT}[c]{RESET}  Custom path")
            print()
            choice = student_prompt("Pick a lesson folder", "c")
            if choice == "c":
                source = student_prompt("Enter full path to lesson folder")
            else:
                try:
                    idx = int(choice) - 1
                    if 0 <= idx < len(lesson_dirs):
                        source = str(lesson_dirs[idx])
                    else:
                        student_error("Invalid selection")
                        input("  Press Enter...")
                        return
                except (ValueError, IndexError):
                    student_error("Invalid input")
                    input("  Press Enter...")
                    return
        else:
            source = student_prompt("Lesson folder path")

        if not source or not Path(source).is_dir():
            student_error("Folder not found.")
            input("  Press Enter...")
            return

        source_path = Path(source)
        starter = source_path / "starter" if (source_path / "starter").is_dir() else source_path
        tests = source_path / "tests"

        if not starter.is_dir():
            student_error(f"starter/ not found in {source}")
            input("  Press Enter...")
            return
        if not tests.is_dir():
            student_error(f"tests/ not found in {source}")
            input("  Press Enter...")
            return

        manifest_defaults: dict[str, str] = {}
        manifest_path = source_path / "manifest.json"
        if manifest_path.is_file():
            try:
                manifest_defaults = DuckyManifest.from_dict(
                    json.loads(manifest_path.read_text(encoding="utf-8"))
                ).to_dict()
            except Exception as exc:
                teacher_warning(f"Could not read the lesson manifest; using folder metadata: {exc}")
        starter_files = sorted(starter.glob("*.py"))
        default_id = str(manifest_defaults.get("lesson_id") or source_path.name)
        default_title = str(manifest_defaults.get("title") or source_path.name.replace("_", " ").title())
        default_entry = str(manifest_defaults.get("entry_file") or (starter_files[0].name if starter_files else ""))
        default_tags = ", ".join(str(tag) for tag in manifest_defaults.get("concept_tags", []))
        lesson_id = student_prompt("Lesson ID", default_id)
        title = student_prompt("Title", default_title)
        entry_file = student_prompt("Entry file (in starter/)", default_entry)
        tags = student_prompt("Concept tags (comma-separated)", default_tags)
        default_output = self.paths["packs"] / f"{lesson_id}.duckypack"
        out_path = student_prompt("Output .duckypack path", str(default_output))

        if not (starter / entry_file).exists():
            student_error(f"Entry file '{entry_file}' not found in {starter}")
            input("  Press Enter...")
            return

        try:
            Path(out_path).parent.mkdir(parents=True, exist_ok=True)
            manifest = DuckyManifest(
                lesson_id=lesson_id,
                title=title,
                entry_file=entry_file,
                concept_tags=[x.strip() for x in tags.split(",") if x.strip()],
            )
            result = create_duckypack(starter, tests, out_path, manifest, source_dir=source_path)
            student_success(f"Created pack: {result}")
        except Exception as exc:
            logger.exception("Failed to create pack")
            student_error(f"Failed to create pack: {exc}")
        input("  Press Enter...")

    def _pack_inspect(self):
        from ..pack import read_manifest

        student_heading("Inspect .duckypack")
        pack_path = self._pick_pack("Pick a pack to inspect")
        if not pack_path or not Path(pack_path).is_file():
            student_error("File not found.")
            input("  Press Enter...")
            return
        try:
            manifest = read_manifest(pack_path)
            data = manifest.to_dict()
            lines = []
            for key, val in data.items():
                if key == "diagnostic_cases":
                    lines.append(f"{STUDENT_ACCENT}{key}:{RESET}")
                    for case in val:
                        lines.append(
                            f"  • {case.get('name', '?')} — {case.get('function_name', '?')}({case.get('args', [])}) → {case.get('expected')}"
                        )
                elif key == "concept_tags":
                    lines.append(f"{STUDENT_ACCENT}{key}:{RESET} {', '.join(val)}")
                else:
                    lines.append(f"{STUDENT_ACCENT}{key}:{RESET} {val}")
            card("Pack Contents", lines)
        except Exception as exc:
            logger.exception("Could not read pack")
            student_error(f"Could not read pack: {exc}")
        input("  Press Enter...")

    def _pack_list(self):
        from ..pack import read_manifest

        student_heading("Available Packs")
        packs_dir = self.paths["packs"]
        packs = list(packs_dir.glob("*.duckypack"))
        example_packs = list(Path("examples/packs").glob("*.duckypack")) if Path("examples/packs").is_dir() else []
        all_packs = packs + example_packs

        if not all_packs:
            student_info("No .duckypack files found.")
        else:
            for i, p in enumerate(all_packs, 1):
                try:
                    m = read_manifest(p)
                    print(f"  {STUDENT_ACCENT}[{i}]{RESET}  {m.title}")
                    print(f"       {DIM}{p}{RESET}")
                    print(f"       ID: {m.lesson_id}  |  Language: {m.language}  |  Tags: {', '.join(m.concept_tags)}")
                except Exception:
                    print(f"  {STUDENT_ACCENT}[{i}]{RESET}  {p.name}  {DIM}(invalid pack){RESET}")
        print()
        input("  Press Enter...")

    def _pick_pack(self, prompt_text: str = "Pack path") -> str | None:
        from ..pack import read_manifest

        packs_dir = self.paths["packs"]
        packs = sorted(packs_dir.glob("*.duckypack"))
        teacher_packs = (
            sorted((self.paths["base"] / "teacher" / "workspace" / "built_packs").glob("*.duckypack"))
            if (self.paths["base"] / "teacher" / "workspace" / "built_packs").is_dir()
            else []
        )
        example_packs = sorted(Path("examples/packs").glob("*.duckypack")) if Path("examples/packs").is_dir() else []
        all_packs = packs + teacher_packs + example_packs

        if all_packs:
            while True:
                print(f"\n  {BOLD}Available packs:{RESET}")
                for i, p in enumerate(all_packs, 1):
                    try:
                        m = read_manifest(p)
                        print(f"  {STUDENT_ACCENT}[{i}]{RESET}  {m.title}  {DIM}{p.name}{RESET}")
                    except Exception:
                        print(f"  {STUDENT_ACCENT}[{i}]{RESET}  {p.name}")
                print(f"  {STUDENT_ACCENT}[c]{RESET}  Custom path")
                print(f"  {STUDENT_ACCENT}[0]{RESET}  Back")
                print()
                choice = student_prompt(prompt_text, "0")
                if choice == "0":
                    return None
                if choice == "c":
                    custom = student_prompt("Enter full path to .duckypack")
                    return custom if custom else None
                try:
                    idx = int(choice) - 1
                    if 0 <= idx < len(all_packs):
                        return str(all_packs[idx])
                except (ValueError, IndexError):
                    pass
                student_error(f"Invalid selection. Enter 1-{len(all_packs)}, c, or 0.")
        else:
            return student_prompt(prompt_text)

    # ─── Workspace ──────────────────────────────────────────

    def _workspace_menu(self):
        from ..pack import read_manifest

        while True:
            clear()
            student_heading("Workspace")
            status_lines = []
            if self.current_pack:
                try:
                    m = read_manifest(self.current_pack)
                    status_lines.append(f"{GREEN}Pack:{RESET} {m.title} ({self.current_pack})")
                except Exception:
                    status_lines.append(f"{YELLOW}Pack:{RESET} {self.current_pack}")
            else:
                status_lines.append(f"{DIM}No pack loaded{RESET}")

            if self.current_submission:
                status_lines.append(f"{GREEN}Submission:{RESET} {self.current_submission}")
            else:
                status_lines.append(f"{DIM}No submission loaded (will use starter code){RESET}")

            if self.use_ollama:
                status_lines.append(f"{GREEN}LLM:{RESET} Ollama ({self.ollama_model})")
            else:
                status_lines.append(f"{DIM}LLM:{RESET} off (rule-based hints only)")

            card("Workspace Status", status_lines)
            print()
            print(f"  {STUDENT_ACCENT}[1]{RESET}  Load a .duckypack")
            print(f"  {STUDENT_ACCENT}[2]{RESET}  Load a student submission")
            print(f"  {STUDENT_ACCENT}[3]{RESET}  Show pack manifest")
            print(f"  {STUDENT_ACCENT}[4]{RESET}  Run tests")
            print(f"  {STUDENT_ACCENT}[5]{RESET}  Run tests (with Ollama hints)")
            print(f"  {STUDENT_ACCENT}[0]{RESET}  Back")
            print()
            choice = student_prompt("Choose", "0")
            if choice == "1":
                self._workspace_load_pack()
            elif choice == "2":
                self._workspace_load_submission()
            elif choice == "3":
                self._workspace_show_manifest()
            elif choice == "4":
                self._workspace_run_tests(use_ollama=False)
            elif choice == "5":
                self._workspace_run_tests(use_ollama=True)
            elif choice == "0":
                break

    def _workspace_load_pack(self):
        from ..pack import read_manifest

        pack_path = self._pick_pack("Pick a pack to load")
        if not pack_path or not Path(pack_path).is_file():
            student_error("File not found.")
            input("  Press Enter...")
            return
        try:
            read_manifest(pack_path)
            self.current_pack = pack_path
            student_success(f"Loaded: {pack_path}")
        except Exception as exc:
            logger.exception("Could not load pack")
            student_error(f"Could not load pack: {exc}")
        input("  Press Enter...")

    def _workspace_load_submission(self):
        sub_path = student_prompt("Student .py file path")
        if not sub_path or not Path(sub_path).is_file():
            student_error("File not found.")
            input("  Press Enter...")
            return
        self.current_submission = sub_path
        student_success(f"Loaded: {sub_path}")
        input("  Press Enter...")

    def _workspace_show_manifest(self):
        from ..pack import read_manifest

        if not self.current_pack:
            student_error("No pack loaded.")
            input("  Press Enter...")
            return
        try:
            manifest = read_manifest(self.current_pack)
            print()
            print(json.dumps(manifest.to_dict(), indent=2))
        except Exception as exc:
            logger.exception("Could not read pack manifest")
            student_error(f"Could not read pack manifest: {exc}")
        input("  Press Enter...")

    def _workspace_run_tests(self, use_ollama: bool = False):
        from ..display import render_student_report
        from ..llm_client import OllamaClient
        from ..runner import run_pack
        from ..tutor import TutorEngine
        from .widgets import record_progress

        if not self.current_pack:
            student_error("No pack loaded. Load a pack first.")
            input("  Press Enter...")
            return

        tutor_engine = None
        if use_ollama:
            model = student_prompt("Ollama model", self.ollama_model)
            self.ollama_model = model
            tutor_engine = TutorEngine(OllamaClient(model=model))

        student_info("Running tests...")
        try:
            report = run_pack(
                self.current_pack,
                student_file=self.current_submission,
                tutor_engine=tutor_engine,
            )
            self.last_report = report
            self.report_store.save(report)
            record_progress(report)
            print()
            print(render_student_report(report))
        except Exception as exc:
            logger.exception("Test run failed")
            student_error(f"Test run failed: {exc}")
        input("\n  Press Enter...")

    def _add_pack_to_server(self, pack_file: Path, lesson_id: str, title: str):
        from ..classroom import ClassroomDB
        from ..pack import read_manifest
        from ..validator import validate_pack_for_distribution

        validation = validate_pack_for_distribution(pack_file)
        if not validation["ok"]:
            teacher_error("Pack was not published because validation failed.")
            print(validation["human_output"])
            return

        db = ClassroomDB()
        try:
            classes = db.list_classes()
            if not classes:
                teacher_error("No classes. Create one first.")
                return
            if len(classes) == 1:
                class_id = classes[0]["class_id"]
            else:
                print(f"\n  {BOLD}Classes:{RESET}")
                for i, c in enumerate(classes, 1):
                    print(f"  {TEACHER_ACCENT}[{i}]{RESET}  {c['name']} ({c['class_id']})")
                print()
                choice = teacher_prompt("Class number", "1")
                try:
                    idx = int(choice) - 1
                    class_id = classes[idx]["class_id"] if 0 <= idx < len(classes) else classes[0]["class_id"]
                except (ValueError, IndexError):
                    class_id = classes[0]["class_id"]
            pack_bytes = pack_file.read_bytes()
            instructions = ""
            try:
                manifest = read_manifest(pack_file)
                instructions = manifest.instructions
            except Exception as exc:
                logger.debug("Could not read instructions from pack '%s': %s", pack_file, exc)
            result = db.add_pack(class_id, lesson_id, title, pack_bytes, instructions=instructions)
            self._teacher_ensure_assignment(db, class_id, result["pack_id"])
            teacher_success(f"Published '{title}' to the class (version {result['version']}).")
        finally:
            db.close()

    def _teacher_packs(self):
        from ..classroom import ClassroomDB

        clear()
        teacher_heading("Packs")
        navigation_hint("Teacher", "Dashboard / Lessons")
        db = ClassroomDB()
        try:
            classes = db.list_classes()
            if not classes:
                teacher_info("No classes.")
                input("  Press Enter...")
                return
            class_id = classes[0]["class_id"]
            packs = db.list_packs(class_id)
            if not packs:
                teacher_info("No packs loaded.")
            else:
                print(f"\n  {BOLD}{'#':<4} {'Lesson':<30} {'Version':<8} {'Updated':<20}{RESET}")
                print(f"  {'─' * 4} {'─' * 30} {'─' * 8} {'─' * 20}")
                for i, p in enumerate(packs, 1):
                    print(
                        f"  {i:<4} {p['title']:<30} v{p['version']:<6} "
                        f"{p.get('added_at', '?')[:19]}"
                    )
            print()
            print(
                f"  {TEACHER_ACCENT}[a]{RESET}  Publish/replace pack   {TEACHER_ACCENT}[r <n>]{RESET}  Roll back   {TEACHER_ACCENT}[d <n>]{RESET}  Delete   {TEACHER_ACCENT}[0]{RESET}  Back"
            )
            print()
            choice = teacher_prompt("Choose", "0")
            if choice == "a":
                self._teacher_add_pack_to(class_id)
            elif choice.startswith("r"):
                parts = choice.split()
                if len(parts) >= 2:
                    try:
                        idx = int(parts[1]) - 1
                        if 0 <= idx < len(packs) and teacher_confirm(f"Roll back '{packs[idx]['title']}' to its previous version?"):
                            restored = db.rollback_pack(packs[idx]["pack_id"], class_id)
                            if restored:
                                teacher_success(f"Rolled back '{packs[idx]['title']}' to version {restored['version']}.")
                            else:
                                teacher_info("No previous version is available.")
                    except (ValueError, IndexError):
                        teacher_error("Choose a valid pack number.")
            elif choice.startswith("d"):
                parts = choice.split()
                if len(parts) >= 2:
                    try:
                        idx = int(parts[1]) - 1
                        if 0 <= idx < len(packs):
                            if teacher_confirm(f"Delete pack '{packs[idx]['title']}'?"):
                                db.delete_pack(packs[idx]["pack_id"], class_id)
                                teacher_success(f"Deleted lesson '{packs[idx]['title']}'.")
                    except ValueError:
                        pass
        finally:
            db.close()
        input("  Press Enter...")

    def _teacher_add_pack_to(self, class_id: str):
        from ..classroom import ClassroomDB
        from ..pack import read_manifest
        from ..validator import validate_pack_for_distribution

        pack_path = self._pick_pack("Pick a pack to add to server")
        if not pack_path or not Path(pack_path).is_file():
            teacher_error("File not found.")
            return

        validation = validate_pack_for_distribution(pack_path)
        if not validation["ok"]:
            print(f"\n  {RED}Pack has validation errors:{RESET}")
            print(validation["human_output"])
            input("\n  Press Enter...")
            return

        manifest = read_manifest(pack_path)
        lesson_id = teacher_prompt("Lesson ID", manifest.lesson_id)
        title = teacher_prompt("Title", manifest.title)
        if not lesson_id or not title:
            teacher_error("Lesson ID and title are required.")
            return

        db = ClassroomDB()
        try:
            pack_bytes = Path(pack_path).read_bytes()
            instructions = ""
            try:
                instructions = manifest.instructions
            except Exception as exc:
                logger.debug("Could not read instructions from pack '%s': %s", pack_path, exc)
            existing = db.get_pack(lesson_id, class_id)
            if existing and not teacher_confirm(
                f"Replace {existing['title']} v{existing['version']} with this validated pack?"
            ):
                teacher_info("Publish cancelled; the existing lesson was kept.")
                return
            result = db.add_pack(class_id, lesson_id, title, pack_bytes, instructions=instructions)
            self._teacher_ensure_assignment(db, class_id, result["pack_id"])
        except ValueError as exc:
            teacher_error(str(exc))
            return
        finally:
            db.close()

        teacher_success(f"Published '{title}' to the class (version {result['version']}).")

    @staticmethod
    def _teacher_ensure_assignment(db, class_id: str, pack_id: str) -> None:
        """Make a teacher-published pack visible as the student's assignment."""
        if any(
            str(item.get("pack_id", "")) == str(pack_id)
            for item in db.list_assignments(class_id)
        ):
            return
        db.add_assignment(class_id, pack_id)

    def _teacher_submissions(self):
        from ..classroom import ClassroomDB

        clear()
        teacher_heading("Submissions")
        navigation_hint("Teacher", "Dashboard / Submissions")
        db = ClassroomDB()
        try:
            classes = db.list_classes()
            if not classes:
                teacher_info("No classes.")
                input("  Press Enter...")
                return
            class_id = classes[0]["class_id"]
            packs = db.list_packs(class_id)
            pack_names = {p["pack_id"]: p["title"] for p in packs}

            submissions = db.list_submissions(class_id)
            if not submissions:
                teacher_info("No submissions yet.")
            else:
                print(f"\n  {BOLD}{'Status':<17} {'Student':<18} {'Pack':<25} {'Submitted':<20}{RESET}")
                print(f"  {'─' * 17} {'─' * 18} {'─' * 25} {'─' * 20}")
                for s in submissions:
                    tag = s.get("kind", "submission")
                    try:
                        report = json.loads(s.get("report_json", "{}"))
                    except Exception:
                        report = {}
                    if tag == "quiz":
                        status = f"{BOLD}[QUIZ]{RESET}"
                        passed = report.get("passed")
                        status += f"{STATUS_PASS}PASS{RESET}" if passed else f"{STATUS_FAIL}FAIL{RESET}"
                        student = s.get("student_name", "?") or "?"
                        pack_title = report.get("lesson_title") or "Quiz result"
                        submitted = s.get("submitted_at", "?")[:19]
                        print(f"  {status:<17} {student:<18} {pack_title:<25} {submitted:<20}")
                        continue
                    try:
                        if report.get("passed"):
                            status = f"{STATUS_PASS}✓ PASS{RESET}"
                        else:
                            status = f"{STATUS_FAIL}✗ FAIL{RESET}"
                        if tag == "progress":
                            status = f"{DIM}[PROG]{RESET} {('✓' if report.get('passed') else '✗')}{RESET}"
                    except Exception:
                        status = f"{STATUS_PENDING}PARSE ERR{RESET}"
                    student = s.get("student_name", "?")
                    if not student:
                        token = s.get("token", "")
                        stu = db.get_student(token) if token else None
                        student = stu["name"] if stu else "?"
                    if tag == "quiz":
                        pack_title = report.get("lesson_title") or "Quiz result"
                    else:
                        pack_id = s.get("pack_id") or ""
                        pack_title = pack_names.get(pack_id, "Coding lesson")
                    submitted = s.get("submitted_at", "?")[:19]
                    review = s.get("status", "submitted")
                    print(f"  {status:<17} {student:<18} {pack_title:<25} {review:<12} {submitted:<20}")
                print()
                choice = teacher_prompt("Review submission number (or 0)", "0")
                if choice != "0":
                    try:
                        selected = submissions[int(choice) - 1]
                    except (ValueError, IndexError):
                        teacher_error("Choose a valid submission number.")
                    else:
                        self._teacher_review_submission(selected["submission_id"])
                        return
        finally:
            db.close()
        input("\n  Press Enter...")

    def _teacher_review_submission(self, submission_id: str):
        from ..classroom import ClassroomDB

        clear()
        teacher_heading("Review submission")
        navigation_hint("Teacher", "Dashboard / Submissions / Review")
        db = ClassroomDB()
        try:
            submission = db.get_submission(submission_id)
            if not submission:
                teacher_error("Submission no longer exists.")
                input("  Press Enter...")
                return
            try:
                report = json.loads(submission.get("report_json") or "{}")
            except json.JSONDecodeError:
                report = {}
            teacher_card("Student result", [
                f"{TEACHER_ACCENT}Type:{RESET} {'Quiz' if submission.get('kind') == 'quiz' else 'Coding result'}",
                        f"{TEACHER_ACCENT}Lesson:{RESET} {report.get('lesson_title') or 'Coding lesson'}",
                        f"{TEACHER_ACCENT}Test result:{RESET} {'PASS' if report.get('passed') else 'NEEDS WORK'}",
                        f"{TEACHER_ACCENT}Current review:{RESET} {submission.get('status', 'submitted')}",
                        f"{TEACHER_ACCENT}Teacher feedback:{RESET} {submission.get('teacher_feedback') or 'None yet'}",
            ])
            print(f"  {TEACHER_ACCENT}[1]{RESET} Pass   {TEACHER_ACCENT}[2]{RESET} Needs work   {TEACHER_ACCENT}[3]{RESET} Resubmit   {TEACHER_ACCENT}[0]{RESET} Back")
            choice = teacher_prompt("Decision", "0")
            status = {"1": "pass", "2": "needs_work", "3": "resubmit"}.get(choice)
            if status:
                feedback = teacher_prompt("Teacher note", submission.get("teacher_feedback", ""))
                db.update_submission_review(submission_id, status, feedback)
                teacher_success("Review saved.")
        finally:
            db.close()
        input("\n  Press Enter...")

    def _teacher_announce(self):
        from ..classroom import ClassroomDB

        clear()
        teacher_heading("Send Announcement")
        message = teacher_prompt("Message")
        if not message:
            teacher_error("Message is required.")
            input("  Press Enter...")
            return

        db = ClassroomDB()
        try:
            classes = db.list_classes()
            if not classes:
                teacher_error("No classes. Create one first.")
                input("  Press Enter...")
                return
            class_id = classes[0]["class_id"]
            db.add_announcement(class_id, message)
        finally:
            db.close()

        teacher_success("Announcement sent.")
        input("  Press Enter...")

    def _teacher_llm_settings(self):
        clear()
        teacher_heading("LLM Hints for Students")
        config = self._load_config()
        backend = config.get("llm_backend", "none")
        llm_url = config.get(f"{backend}_url", "") if backend != "none" else ""
        llm_model = config.get(f"{backend}_model", "") if backend != "none" else ""
        llm_max_tokens = config.get("lmstudio_max_tokens", 2500) if backend == "lmstudio" else ""

        if backend != "none":
            print(f"  Status: {GREEN}● Enabled{RESET}")
            print(f"  Backend:  {BOLD}{backend}{RESET}")
            print(f"  URL:      {llm_url}")
            print(f"  Model:    {llm_model}")
            if backend == "lmstudio":
                print(f"  Tokens:   {llm_max_tokens} (min 2500)")
        else:
            print(f"  Status: {DIM}○ Disabled{RESET}")

        print()
        print(f"  {TEACHER_ACCENT}[1]{RESET}  Enable Ollama")
        print(f"  {TEACHER_ACCENT}[2]{RESET}  Enable LM Studio")
        print(f"  {TEACHER_ACCENT}[3]{RESET}  Disable LLM")
        print(f"  {TEACHER_ACCENT}[4]{RESET}  Set URL")
        if backend == "lmstudio":
            print(f"  {TEACHER_ACCENT}[5]{RESET}  Browse & select model from LM Studio")
        else:
            print(f"  {TEACHER_ACCENT}[5]{RESET}  Set model")
        if backend == "lmstudio":
            print(f"  {TEACHER_ACCENT}[6]{RESET}  Set min tokens (default 2500)")
        print(f"  {TEACHER_ACCENT}[7]{RESET}  Test hint")
        print(f"  {TEACHER_ACCENT}[0]{RESET}  Back")
        print()
        choice = teacher_prompt("Choose", "0")

        if choice == "1":
            self._save_config({"llm_backend": "ollama"})
            teacher_success("Enabled Ollama. Set URL and model below if needed.")
            input("  Press Enter...")
        elif choice == "2":
            self._save_config({"llm_backend": "lmstudio"})
            teacher_success("Enabled LM Studio. Set URL and model below if needed.")
            input("  Press Enter...")
        elif choice == "3":
            self._save_config({"llm_backend": "none"})
            teacher_success("LLM hints disabled.")
            input("  Press Enter...")
        elif choice == "4":
            current_url = config.get(
                f"{backend}_url", "http://localhost:1234" if backend == "lmstudio" else "http://localhost:11434"
            )
            new_url = teacher_prompt(f"URL ({backend})", current_url)
            if new_url:
                self._save_config({f"{backend}_url": new_url})
                teacher_success(f"URL set to: {new_url}")
            input("  Press Enter...")
        elif choice == "5":
            if backend == "lmstudio":
                self._browse_lmstudio_models(config)
            elif backend == "ollama":
                self._browse_ollama_models(config)
            else:
                current_model = config.get(f"{backend}_model", "")
                new_model = teacher_prompt(f"Model name ({backend})", current_model)
                if new_model:
                    self._save_config({f"{backend}_model": new_model})
                    teacher_success(f"Model set to: {new_model}")
                input("  Press Enter...")
        elif choice == "6" and backend == "lmstudio":
            current_tokens = config.get("lmstudio_max_tokens", 2500)
            new_tokens = teacher_prompt("Min tokens (default 2500)", str(current_tokens))
            try:
                tokens_val = int(new_tokens)
                if tokens_val < 2500:
                    tokens_val = 2500
                    teacher_warning("Minimum is 2500, using 2500")
                self._save_config({"lmstudio_max_tokens": tokens_val})
                teacher_success(f"Min tokens set to: {tokens_val}")
            except ValueError:
                teacher_error("Invalid number.")
            input("  Press Enter...")
        elif choice == "7":
            if backend == "none":
                teacher_error("Enable a backend first.")
                input("  Press Enter...")
                return
            from ..config import build_llm_client
            from ..llm_client import HintRequest

            client = build_llm_client(config)
            test_req = HintRequest(
                lesson_id="test",
                title="Test Lesson",
                code_snippet="def add(a, b): print(a + b)",
                diagnosis="prints_instead_of_returns",
            )
            hint = client.generate_hint(test_req)
            if hint:
                teacher_success(f"Hint received:\n  {hint}")
            else:
                teacher_error("No hint returned. Check URL and model.")
            input("  Press Enter...")

    def _save_teacher_admin_token(self, admin_token: str, class_id: str) -> None:
        """Persist the admin token so teacher admin commands can authenticate."""
        try:
            from ..config import save_config

            save_config({"teacher_admin_token": admin_token, "teacher_class_id": class_id})
        except Exception as exc:  # noqa: BLE001 - best-effort persistence
            logger.info("Could not persist teacher admin token: %s", exc)

    def _teacher_server_control(self):
        from ..classroom import ClassroomDB

        clear()
        teacher_heading("Server Control")

        if self.teacher_server and self.teacher_server.is_running():
            port = self.teacher_server.port
            code = self.teacher_server.get_access_code()
            admin_token = self.teacher_server.get_admin_token()
            self._save_teacher_admin_token(admin_token, self.teacher_server.class_id)

            db = ClassroomDB()
            try:
                students = db.list_students(self.teacher_server.class_id)
                online_count = 0
                now = datetime.now(timezone.utc)
                for s in students:
                    last_seen = s.get("last_seen", "")
                    if last_seen:
                        try:
                            ls = datetime.fromisoformat(last_seen)
                            if (now - ls).total_seconds() < 300:
                                online_count += 1
                        except (ValueError, TypeError):
                            pass
            finally:
                db.close()

            teacher_card("Server Status", [
                f"{STATUS_RUNNING}● Running{RESET}",
                f"{TEACHER_ACCENT}Port:{RESET} {port}",
                f"{TEACHER_ACCENT}Access Code:{RESET} {BOLD}{code}{RESET}",
                f"{DIM}Admin token:{RESET} hidden (press r to reveal)",
                f"{TEACHER_ACCENT}Students Connected:{RESET} {len(students)} ({online_count} online)",
            ])
            teacher_warning("Trusted local network only. Do not forward this URL beyond your classroom network.")
        else:
            teacher_card("Server Status", [f"{STATUS_OFFLINE}○ Not running{RESET}"])

        print()
        print(f"  {TEACHER_ACCENT}[s]{RESET}  Start class     {TEACHER_ACCENT}[p]{RESET}  Stop server")
        print(f"  {TEACHER_ACCENT}[r]{RESET}  Reveal admin token (teacher-only)")
        print(f"  {TEACHER_ACCENT}[0]{RESET}  Back")
        print()
        choice = teacher_prompt("Choose", "0")
        if choice == "s":
            self._teacher_start_server()
        elif choice == "p":
            self._teacher_stop_server()
        elif choice == "r" and self.teacher_server and self.teacher_server.is_running():
            print(f"\n  {DIM}Admin token (keep secret):{RESET} {admin_token}")
            input("  Press Enter...")

    def _teacher_backup_restore(self):
        from ..classroom import ClassroomDB
        from ..paths import app_dir

        clear()
        teacher_heading("Backup and restore")
        configured = self._load_config().get("teacher_backup_path", "")
        default = configured or str(app_dir() / "teacher" / "classroom-backup.db")
        print(f"  {DIM}Backups are local SQLite copies. Restore replaces current classroom history.{RESET}")
        print(f"  {TEACHER_ACCENT}[1]{RESET}  Create backup")
        print(f"  {TEACHER_ACCENT}[2]{RESET}  Restore backup")
        print(f"  {TEACHER_ACCENT}[0]{RESET}  Back")
        choice = teacher_prompt("Choose", "1")
        if choice == "1":
            destination = teacher_prompt("Backup file", default)
            db = ClassroomDB()
            try:
                path = db.backup_to(destination)
                self._save_config({"teacher_backup_path": str(path)})
                teacher_success(f"Backup created at {path}")
            except Exception as exc:
                teacher_error(f"Backup failed: {exc}")
            finally:
                db.close()
            input("  Press Enter...")
        elif choice == "2":
            if self.teacher_server and self.teacher_server.is_running():
                teacher_error("Stop the teacher server before restoring a backup.")
            else:
                source = teacher_prompt("Backup file", default)
                if teacher_confirm("Replace the local classroom database from this backup?"):
                    try:
                        ClassroomDB().restore_from(source)
                        teacher_success("Classroom database restored. Start the class again to reload it.")
                    except Exception as exc:
                        teacher_error(f"Restore failed: {exc}")
            input("  Press Enter...")

    def _teacher_quick_start(self):
        """Quick-start a server with a new class in one flow."""
        from ..classroom import ClassroomDB
        from ..network import get_local_ip_url
        from ..server import DEFAULT_PORT, DuckyServer

        clear()
        teacher_heading("Quick Start Server")

        name = teacher_prompt("Class name", "My Class")
        class_id = teacher_prompt("Class ID", name.lower().replace(" ", "_"))
        port_str = teacher_prompt("Port", str(DEFAULT_PORT))

        try:
            port = int(port_str)
            if not (1 <= port <= 65535):
                port = DEFAULT_PORT
        except ValueError:
            port = DEFAULT_PORT

        if self.teacher_server and self.teacher_server.is_running():
            teacher_error("Server already running. Stop it first.")
            input("  Press Enter...")
            return

        # Create class if needed
        db = ClassroomDB()
        try:
            existing = db.get_class(class_id)
            if not existing:
                db.create_class(class_id, name)
        finally:
            db.close()

        config = self._load_config()
        llm_cfg = {}
        backend = config.get("llm_backend", "none")
        if backend != "none":
            llm_cfg = {
                "llm_enabled": True,
                "llm_backend": backend,
                "llm_model": config.get(f"{backend}_model", ""),
                "llm_url": config.get(f"{backend}_url", ""),
                "lmstudio_max_tokens": config.get("lmstudio_max_tokens", 2500),
            }

        self.teacher_server = DuckyServer(
            port=port,
            class_id=class_id,
            class_name=name,
            llm_config=llm_cfg,
            bind_host=config.get("server_bind_host", "0.0.0.0"),
        )
        self.teacher_server.start()

        code = self.teacher_server.get_access_code()
        admin_token = self.teacher_server.get_admin_token()
        self._save_teacher_admin_token(admin_token, class_id)
        server_url = get_local_ip_url(port)
        teacher_success(f"Server started on port {port}")
        print(f"\n  {BOLD}Share this with your students:{RESET}")
        print(f"  {TEACHER_ACCENT}Server URL:{RESET}  {server_url}")
        print(f"  {TEACHER_ACCENT}Access Code:{RESET} {BOLD}{code}{RESET}")
        teacher_warning("Trusted local network only. The URL and access code are for this classroom network.")
        print(f"  {DIM}Admin token is saved locally and hidden by default.{RESET}")
        input("\n  Press Enter...")

    def _teacher_start_server(self):
        from ..server import DEFAULT_PORT, DuckyServer

        clear()
        teacher_heading("Start Teacher Server")

        port_str = teacher_prompt("Port", str(DEFAULT_PORT))
        try:
            port = int(port_str)
            if not (1 <= port <= 65535):
                teacher_error("Port must be between 1 and 65535. Using default.")
                port = DEFAULT_PORT
        except ValueError:
            teacher_error("Port must be a number. Using default.")
            port = DEFAULT_PORT

        class_id = teacher_prompt("Class ID", "default")
        class_name = teacher_prompt("Class name", "My Class")

        if self.teacher_server and self.teacher_server.is_running():
            teacher_error("Server already running. Stop it first.")
            input("  Press Enter...")
            return

        config = self._load_config()
        llm_cfg = {}
        backend = config.get("llm_backend", "none")
        if backend != "none":
            llm_cfg = {
                "llm_enabled": True,
                "llm_backend": backend,
                "llm_model": config.get(f"{backend}_model", ""),
                "llm_url": config.get(f"{backend}_url", ""),
                "lmstudio_max_tokens": config.get("lmstudio_max_tokens", 2500),
            }

        self.teacher_server = DuckyServer(
            port=port,
            class_id=class_id,
            class_name=class_name,
            llm_config=llm_cfg,
            bind_host=config.get("server_bind_host", "0.0.0.0"),
        )
        self.teacher_server.start()

        code = self.teacher_server.get_access_code()
        server_url = self.teacher_server.get_url()
        teacher_success(f"Server started on port {port}")
        print(f"\n  {BOLD}Share this with your students:{RESET}")
        print(f"  {TEACHER_ACCENT}Server URL:{RESET}  {server_url}")
        print(f"  {TEACHER_ACCENT}Access Code:{RESET} {BOLD}{code}{RESET}")
        teacher_warning("Trusted local network only. Do not share this URL outside the classroom network.")
        print("\n  Students use: [c] Connect to teacher")
        input("\n  Press Enter...")

    def _teacher_stop_server(self):
        if self.teacher_server and self.teacher_server.is_running():
            self.teacher_server.stop()
            teacher_success("Server stopped.")
        else:
            teacher_info("Server not running.")
        input("  Press Enter...")

    # ─── Quizzes (teacher authoring) ─────────────────────────

    def _teacher_quiz_flow(self, lesson_dir: Path | None = None):
        """Create or edit a quiz and save it without corrupting old content."""
        from ..authoring import atomic_write_json, load_quiz_source
        from ..quiz import validate_quiz_dict

        clear()
        teacher_heading("Quiz Authoring")

        lesson_dir = lesson_dir or self._teacher_pick_lesson("Pick lesson folder")
        if not lesson_dir:
            return

        dest = lesson_dir / "quizzes.json"
        existing, load_errors = (load_quiz_source(dest, lesson_id=lesson_dir.name) if dest.is_file() else (None, []))
        if load_errors:
            teacher_error("Existing quiz could not be loaded; no changes were made.")
            for error in load_errors:
                print(f"  {YELLOW}- {error}{RESET}")
            input("\n  Press Enter...")
            return
        quiz_id = str(existing.get("quiz_id") if existing else f"quiz.{lesson_dir.name}")
        title = teacher_prompt("Quiz title", str(existing.get("title") if existing else f"{lesson_dir.name} Quiz"))
        description = teacher_prompt("Description (optional)", str(existing.get("description") if existing else ""))
        existing_limit = int(existing.get("time_limit_seconds", 0) or 0) if existing else 0
        if existing_limit and existing_limit % 60:
            timer_default = f"{existing_limit}s"
        else:
            timer_default = str(existing_limit // 60) if existing_limit else "0"
        timer_minutes = teacher_prompt("Time limit (e.g. 20m, 90s, 0 = untimed)", timer_default)
        try:
            time_limit_seconds = self._teacher_timer_seconds(timer_minutes)
        except ValueError:
            teacher_error("Time limit must be 0, a whole number of minutes, or seconds such as 90s.")
            input("\n  Press Enter...")
            return
        lesson_id = str(existing.get("lesson_id") if existing else lesson_dir.name)
        questions: list[dict[str, Any]] = list(existing.get("questions", [])) if existing else []
        for index, question in enumerate(questions, 1):
            question.setdefault("id", f"q{index}")
        while True:
            clear()
            teacher_heading(f"Quiz: {title}")
            if questions:
                for i, q in enumerate(questions, 1):
                    print(f"  {TEACHER_ACCENT}[{i}]{RESET} {q.get('id', f'q{i}')} · {q['type']}: {q['prompt']}")
            print()
            print(f"  {TEACHER_ACCENT}[a]{RESET}  Add a question")
            print(f"  {TEACHER_ACCENT}[e <n>]{RESET} Edit a question")
            print(f"  {TEACHER_ACCENT}[c <n>]{RESET} Duplicate a question")
            print(f"  {TEACHER_ACCENT}[u <n> / d <n>]{RESET} Move up/down")
            print(f"  {TEACHER_ACCENT}[r]{RESET}  Remove a question")
            print(f"  {TEACHER_ACCENT}[p]{RESET}  Preview quiz")
            print(f"  {TEACHER_ACCENT}[s]{RESET}  Save & exit")
            print(f"  {TEACHER_ACCENT}[x]{RESET}  Cancel (discard)")
            print()
            choice = teacher_prompt("Choose", "a")
            if choice == "x":
                teacher_info("Discarded — no changes saved.")
                input("\n  Press Enter...")
                return
            elif choice == "s":
                break
            elif choice == "p":
                print(json.dumps({
                    "title": title,
                    "time_limit_seconds": time_limit_seconds,
                    "questions": questions,
                }, indent=2))
                input("\n  Press Enter...")
                continue
            elif choice == "r":
                if not questions:
                    teacher_warning("No questions to remove.")
                    input("\n  Press Enter...")
                    continue
                rm = teacher_prompt("Remove question number")
                try:
                    ridx = int(rm) - 1
                    if 0 <= ridx < len(questions):
                        questions.pop(ridx)
                    else:
                        teacher_error("Invalid number.")
                except ValueError:
                    teacher_error("Invalid number.")
                input("\n  Press Enter...")
                continue
            elif choice[:1] in ("e", "c", "u", "d"):
                parts = choice.split()
                try:
                    index = int(parts[1]) - 1
                    if not 0 <= index < len(questions):
                        raise IndexError
                    command = choice[0]
                    if command == "e":
                        replacement = self._teacher_quiz_add_question(index + 1)
                        if replacement:
                            replacement["id"] = questions[index].get("id", f"q{index + 1}")
                            questions[index] = replacement
                    elif command == "c":
                        duplicate = dict(questions[index])
                        base_id = str(duplicate.get("id", f"q{index + 1}"))
                        duplicate["id"] = f"{base_id}_copy"
                        suffix = 2
                        while any(q.get("id") == duplicate["id"] for q in questions):
                            duplicate["id"] = f"{base_id}_copy{suffix}"
                            suffix += 1
                        questions.insert(index + 1, duplicate)
                    elif command == "u" and index > 0:
                        questions[index - 1], questions[index] = questions[index], questions[index - 1]
                    elif command == "d" and index < len(questions) - 1:
                        questions[index + 1], questions[index] = questions[index], questions[index + 1]
                except (ValueError, IndexError):
                    teacher_error("Use e, c, u, or d followed by a valid question number.")
                input("\n  Press Enter...")
                continue
            elif choice == "a":
                q = self._teacher_quiz_add_question(len(questions) + 1)
                if q:
                    questions.append(q)
            else:
                teacher_error("Invalid choice.")
                input("\n  Press Enter...")
                continue

        quiz = {
            "schema_version": int(existing.get("schema_version", 1)) if existing else 1,
            "quiz_id": quiz_id,
            "lesson_id": lesson_id,
            "title": title,
            "description": description,
            "time_limit_seconds": time_limit_seconds,
            "source": str(existing.get("source", "teacher TUI")) if existing else "teacher TUI",
            "shuffle_questions": bool(existing.get("shuffle_questions", False)) if existing else False,
            "review_mode": str(existing.get("review_mode", "missed_first")) if existing else "missed_first",
            "questions": questions,
        }
        ok, errors, _warnings = validate_quiz_dict(quiz)
        if not ok:
            for e in errors:
                teacher_error(e)
            teacher_warning("Quiz not saved — fix the errors and try again.")
            input("\n  Press Enter...")
            return

        if dest.exists() and not teacher_confirm("Replace the existing quiz with these changes?"):
            teacher_info("Quiz unchanged.")
            input("\n  Press Enter...")
            return
        try:
            atomic_write_json(dest, quiz)
        except OSError as exc:
            teacher_error(f"Quiz was not saved: {exc}")
            input("\n  Press Enter...")
            return
        teacher_success(f"Quiz saved to: {dest}")
        teacher_info("Preview and validate the lesson, then build and publish it so quizzes.json ships to students.")
        input("\n  Press Enter...")

    def _teacher_quiz_add_question(self, n: int) -> dict[str, Any] | None:
        clear()
        teacher_heading(f"Add Question #{n}")
        print(f"  {TEACHER_ACCENT}[1]{RESET}  Multiple choice")
        print(f"  {TEACHER_ACCENT}[2]{RESET}  Predict output")
        print()
        qtype_choice = teacher_prompt("Type", "1")
        type_map = {"1": "multiple_choice", "2": "predict_output", "3": "true_false"}
        qtype = type_map.get(qtype_choice)
        if not qtype:
            teacher_error("Invalid type.")
            input("\n  Press Enter...")
            return None

        prompt = teacher_prompt("Prompt").strip()
        if not prompt:
            teacher_error("Prompt is required.")
            input("\n  Press Enter...")
            return None

        q: dict[str, Any] = {"id": f"q{n}", "type": qtype, "prompt": prompt}
        if qtype == "multiple_choice":
            q["options"] = []
            while True:
                opt = teacher_prompt(f"Option #{len(q['options']) + 1} (blank to finish)").strip()
                if not opt:
                    break
                q["options"].append(opt)
            if len(q["options"]) < 2:
                teacher_error("Need at least 2 options.")
                input("\n  Press Enter...")
                return None
            while True:
                correct = teacher_prompt("Correct option (exact text)").strip()
                if correct in q["options"]:
                    q["correct"] = correct
                    break
                teacher_error("Must match one of the options exactly.")
        elif qtype == "predict_output":
            print("  Enter the code snippet (blank line to finish):")
            code_lines = []
            while True:
                line = input("    ")
                if line == "" and code_lines:
                    break
                code_lines.append(line)
            q["code"] = "\n".join(code_lines).strip("\n")
            q["correct"] = teacher_prompt("Expected output (may be empty)").strip()
        elif qtype == "true_false":
            while True:
                ans = teacher_prompt("Correct answer (true/false)").strip().lower()
                if ans in ("true", "false"):
                    q["correct"] = ans
                    break
                teacher_error("Enter true or false.")

        q["explanation"] = teacher_prompt("Explanation shown after grading (optional)", "")
        return q
