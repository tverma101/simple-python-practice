"""Main DuckyTerminal application class — composes all menu mixins."""

from __future__ import annotations

import logging
import os
import sys
import threading
from typing import Any

from .. import __build_type__
from ..config import load_config, save_config
from ..paths import ensure_app_dirs
from ..reports import ReportStore
from ..runner import TestRunReport
from ..server import DuckyServer
from .settings import SettingsMenuMixin
from .student import StudentMenuMixin
from .teacher import TeacherMenuMixin
from .widgets import (
    BOLD,
    CYAN,
    DIM,
    GREEN,
    RESET,
    STUDENT_ACCENT,
    YELLOW,
    banner,
    card,
    clear,
    navigation_hint,
    progress_bar,
    prompt,
    separator,
    show_notifications,
    student_banner,
    student_heading,
    student_info,
    student_prompt,
    student_separator,
)

logger = logging.getLogger(__name__)


class DuckyTerminal(StudentMenuMixin, TeacherMenuMixin, SettingsMenuMixin):
    """Interactive terminal UI for Ducky — local-first coding tutor."""

    def __init__(self):
        self.paths = ensure_app_dirs()
        self.last_report: TestRunReport | None = None
        self.report_store = ReportStore()
        self.current_pack: str | None = None
        self.current_submission: str | None = None
        self.use_ollama = False
        self.ollama_model = "qwen2.5-coder:1.5b"
        self.teacher_server: DuckyServer | None = None
        self._background_sync = None
        self._pending_update_notification = None
        self._auto_update_notification = None
        self._auto_pull_done = False
        self._student_auto_update_done = True
        self._notifications: list[dict[str, Any]] = []

        # Both teacher and student GUIs check updates as soon as the GUI is
        # launched. The banner below always shows the baked-in version, while
        # the queued notification reports the live update result when ready.
        self._check_auto_update()

    # ── Config helpers ──────────────────────────────────────

    @staticmethod
    def _load_config() -> dict:
        return load_config()

    @staticmethod
    def _save_config(updates: dict) -> None:
        save_config(updates)

    def _add_notification(
        self, level: str, message: str, source: str = ""
    ) -> None:
        """Queue a notification for display at the next menu screen.

        Args:
            level: ``"error"``, ``"warning"``, ``"success"``, or ``"info"``.
            message: Human-readable message.
            source: Short context label (e.g. ``"sync"``, ``"update"``).
        """
        notification = {"level": level, "message": message, "source": source}
        if notification not in self._notifications:
            self._notifications.append(notification)
        # Notifications are ephemeral UI state, never a permanent event log.
        del self._notifications[:-50]

    # ── Main entry ──────────────────────────────────────────

    def run(self):
        try:
            self._main_menu()
        except KeyboardInterrupt:
            print(f"\n\n{DIM}Bye!{RESET}\n")
            sys.exit(0)
        except EOFError:
            # Piped/headless launches and closed terminal input should leave the
            # TUI cleanly instead of surfacing a traceback as an internal error.
            print(f"\n\n{DIM}Input ended — bye!{RESET}\n")

    def _check_auto_update(self):
        """Apply pending updates and check the live channel in the background."""
        try:
            from ..self_update import (
                apply_pending_update,
                auto_update_background,
                check_for_update,
                queue_pending_update,
            )

            applied = apply_pending_update()
            if applied and applied.get("ok"):
                self._auto_update_notification = {
                    "version": applied.get("version", "?"),
                }

            def report_update(result: dict[str, Any]) -> None:
                if result.get("disabled") or result.get("busy"):
                    return
                if result.get("error"):
                    self._add_notification("warning", "Update check unavailable; continuing offline.", "update")
                elif result.get("update_available"):
                    latest = result.get("latest_version", "?")
                    current = result.get("current_version", "?")
                    detail = "new build" if latest == current else f"v{latest}"
                    self._add_notification(
                        "info",
                        f"Student update {detail} is ready and will apply on next launch.",
                        "update",
                    )
                else:
                    self._add_notification(
                        "success",
                        f"Ducky v{result.get('current_version', '?')} is up to date.",
                        "update",
                    )

            # Student builds use Cloudflare as their release authority. Fetch
            # that manifest synchronously before the student TUI opens so a
            # first launch cannot miss a just-published build because the
            # background thread lost a race with the menu. The update itself
            # remains staged for the next launch.
            if __build_type__ == "student":
                config = self._load_config()
                if config.get("auto_update_app", True):
                    result = check_for_update()
                    report_update(result)
                    queue_pending_update(result)
                return

            threading.Thread(
                target=auto_update_background,
                kwargs={"on_result": report_update},
                daemon=True,
            ).start()
        except Exception as exc:
            logger.debug("Auto-update check failed on startup: %s", exc)

    def _student_build_menu(self):
        """Show the student-only home screen.

        The full build is a teacher platform with a student preview.  The
        student distribution should open on the student's next action instead
        of exposing platform internals such as pack authoring or dataset
        export.  Keeping this as a separate menu also gives the packaged
        student build a stable role-specific entry point.
        """
        from ..workspace import get_current_lesson, get_progress, list_available_lessons

        # Keep the packaged student home and the full-build student preview on
        # the same connection/distribution path.
        self._student_sync_on_entry()

        while True:
            clear()
            if self._notifications:
                show_notifications(self._notifications)
                self._notifications.clear()

            student_banner()
            student_separator()
            student_heading("Your Learning Home")
            navigation_hint("Student", "Home")

            try:
                lessons = self._student_annotate_assignments(list_available_lessons())
                progress = get_progress()
            except Exception as exc:
                logger.exception("Could not load student dashboard", exc_info=exc)
                lessons = []
                progress = {"completed": 0, "total": 0}

            total = int(progress.get("total", 0) or 0)
            completed = int(progress.get("completed", 0) or 0)
            if total:
                card("Course progress", [progress_bar(completed, total)])

            try:
                current = get_current_lesson()
            except OSError:
                current = None
            if current:
                current_status = current.get("status", "in_progress")
                status_label = "completed" if current_status == "completed" else "in progress"
                print(
                    f"  {STUDENT_ACCENT}Working on:{RESET} "
                    f"{current.get('title', current.get('lesson_id', 'current lesson'))} "
                    f"{DIM}({status_label}){RESET}"
                )

            next_lesson = next(
                (lesson for lesson in lessons if lesson.get("status") != "completed"),
                None,
            )
            if current:
                print(f"  {DIM}Resume opens this lesson directly.{RESET}")
            elif next_lesson:
                print(
                    f"  {STUDENT_ACCENT}Next up:{RESET} "
                    f"{next_lesson.get('title', 'Your next lesson')}"
                )
            elif lessons:
                print(f"  {STUDENT_ACCENT}All available lessons are complete. Nice work!{RESET}")
            else:
                print("  No lessons yet. Connect to your teacher to receive your first assignment.")

            report, _ = self._student_recent_report()
            report_summary = self._student_report_summary(report)
            if report_summary:
                print(f"  {DIM}Latest test:{RESET} {report_summary}")

            quiz_rows = self._student_quiz_overview(lessons)
            if quiz_rows:
                quiz_lines = []
                for row in quiz_rows[:3]:
                    quiz_lines.append(f"{row['quiz'].title} — {row['status']}")
                if len(quiz_rows) > 3:
                    quiz_lines.append(f"+ {len(quiz_rows) - 3} more in Quiz Center")
                card("Quiz check-in", quiz_lines)

            config = self._load_config()
            if config.get("teacher_url") and config.get("teacher_token"):
                connection_state = getattr(self, "_student_connection_state", "unknown")
                if connection_state == "offline":
                    print(f"  {YELLOW}Classroom:{RESET} offline · work is saved locally; choose Sync to retry")
                elif connection_state == "connected":
                    print(f"  {GREEN}Classroom:{RESET} connected · choose Sync to check for updates")
                else:
                    print(f"  {YELLOW}Classroom:{RESET} connection saved · choose Sync to verify")
            elif config.get("teacher_url"):
                print(f"  {YELLOW}Classroom:{RESET} connection saved · reconnect to continue syncing")
            else:
                print(f"  {DIM}Classroom:{RESET} local mode · your work stays on this computer")

            print()
            student_separator()
            print(f"  {STUDENT_ACCENT}[1]{RESET}  Resume lesson    — open the current lesson")
            print(f"  {STUDENT_ACCENT}[2]{RESET}  Run tests        — test your current edits")
            print(f"  {STUDENT_ACCENT}[3]{RESET}  Quiz center      — take, resume, or retry a quiz")
            print(f"  {STUDENT_ACCENT}[4]{RESET}  Get a hint       — diagnose the first failure")
            print(f"  {STUDENT_ACCENT}[5]{RESET}  Submit result    — send the latest report")
            print(f"  {STUDENT_ACCENT}[6]{RESET}  Sync             — pull lessons and retry queued work")
            print(f"  {STUDENT_ACCENT}[7]{RESET}  More             — My lessons, My progress, classroom, settings")
            print(f"  {STUDENT_ACCENT}[0]{RESET}  Exit")
            print()

            choice = student_prompt("What would you like to do?", "1").lower()
            if choice == "1":
                self._student_resume_lesson()
            elif choice == "2":
                self._run_tests_flow()
            elif choice == "3":
                self._student_quiz_center()
            elif choice == "4":
                self._hint_chat_flow()
            elif choice == "5":
                self._classroom_submit()
            elif choice == "6":
                self._classroom_update()
            elif choice == "7":
                self._student_more_menu()
            elif choice in ("?", "help"):
                student_info("Use the number beside an action. 0 goes back; Sync also retries offline work.")
                input("\n  Press Enter...")
            elif choice.lower() == "m":
                self._classroom_menu()
            elif choice == "0":
                print(f"\n{DIM}Keep learning — bye!{RESET}\n")
                return

    def _student_more_menu(self):
        """Secondary student actions kept out of the action-oriented home."""
        while True:
            clear()
            student_heading("More student tools")
            navigation_hint("Student", "Home / More")
            print(f"  {STUDENT_ACCENT}[1]{RESET}  Lessons and quizzes")
            print(f"  {STUDENT_ACCENT}[2]{RESET}  Ask Ducky")
            print(f"  {STUDENT_ACCENT}[3]{RESET}  My progress")
            print(f"  {STUDENT_ACCENT}[4]{RESET}  Classroom connection")
            print(f"  {STUDENT_ACCENT}[5]{RESET}  Settings")
            print(f"  {STUDENT_ACCENT}[0]{RESET}  Back")
            choice = student_prompt("Choose", "0")
            if choice == "1":
                self._student_lessons_menu()
            elif choice == "2":
                self._hint_chat_flow()
            elif choice == "3":
                self._student_progress_menu()
            elif choice == "4":
                self._classroom_menu()
            elif choice == "5":
                self._settings_menu()
            elif choice in ("?", "help"):
                student_info("Choose Lessons for the detailed lesson list, or 0 to return home.")
                input("\n  Press Enter...")
            elif choice == "0":
                return

    def _main_menu(self):
        from .. import __build_type__

        if __build_type__ != "full":
            self._student_build_menu()
            return

        while True:
            clear()
            # Show any queued notifications from background operations
            if self._notifications:
                show_notifications(self._notifications)
                self._notifications.clear()
            banner()
            if self._auto_update_notification:
                version = self._auto_update_notification.get("version", "?")
                print(f"  {GREEN}✓{RESET} Ducky auto-updated to v{version}")
                self._auto_update_notification = None
            separator()
            print(f"  {BOLD}Main Menu{RESET}")
            separator()
            print()
            print(f"  {CYAN}[1]{RESET}  Student home — lessons, tests, submit")
            print(f"  {CYAN}[2]{RESET}  Packs       — create / inspect .duckypack files")
            print(f"  {CYAN}[3]{RESET}  Workspace   — load a pack, run tests, get hints")
            print(f"  {CYAN}[4]{RESET}  Tutor       — view diagnosis and hints")
            print(f"  {CYAN}[5]{RESET}  Reports     — view reports and export dataset")
            print(f"  {CYAN}[6]{RESET}  Classroom   — connect to teacher, pull updates, submit")
            print(f"  {CYAN}[7]{RESET}  Teacher     — serve, manage students, packs, assignments")
            print(f"  {CYAN}[8]{RESET}  Settings    — LLM config, teacher connection")
            print(f"  {CYAN}[0]{RESET}  Quit")
            print()
            choice = prompt("Choose", "0")
            if choice == "1":
                # The full build and the student distribution now enter the
                # same action-oriented student home.
                self._student_build_menu()
            elif choice == "2":
                self._packs_menu()
            elif choice == "3":
                self._workspace_menu()
            elif choice == "4":
                self._tutor_menu()
            elif choice == "5":
                self._reports_menu()
            elif choice == "6":
                self._classroom_menu()
            elif choice == "7":
                self._teacher_menu()
            elif choice == "8":
                self._settings_menu()
            elif choice == "0":
                print(f"\n{DIM}Bye!{RESET}\n")
                break


def main():
    # The student distribution is a real windowed app. Keep DuckyTerminal
    # available for the full/teacher build and for compatibility tests, but do
    # not send students back into a terminal menu when they run ducky-gui.
    if __build_type__ != "full":
        from ..self_update import startup_student_update
        from .student_desktop import main as student_desktop_main

        update = startup_student_update()
        if update.get("restart_required"):
            # The package has just been replaced on disk. Restart the Python
            # process so the new module code is actually used in this launch.
            os.execv(sys.executable, [sys.executable, "-c", "from ducky.gui import main; main()"])
            return
        student_desktop_main(startup_update=update)
        return
    app = DuckyTerminal()
    app.run()
