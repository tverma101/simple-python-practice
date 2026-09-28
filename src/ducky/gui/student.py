"""Student-mode menus for the Ducky terminal UI."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from ..quiz import QuizQuestion
from .widgets import (
    BOLD,
    CYAN,
    DIM,
    GREEN,
    RED,
    RESET,
    STUDENT_ACCENT,
    STUDENT_DIM,
    STUDENT_PROMPT,
    YELLOW,
    badge,
    card,
    clear,
    navigation_hint,
    progress_bar,
    show_notifications,
    # Student-specific imports:
    student_banner,
    student_confirm,
    student_error,
    student_heading,
    student_info,
    student_prompt,
    student_separator,
    student_success,
    student_warning,
    terminal_width,
)

logger = logging.getLogger(__name__)


class StudentMenuMixin:
    """Mixin providing student-mode, packs, workspace, and classroom menus."""

    @staticmethod
    def _quiz_duration_label(seconds: int) -> str:
        """Render a quiz limit without hiding short limits as ``0 min``."""
        seconds = max(0, int(seconds or 0))
        minutes, remainder = divmod(seconds, 60)
        if minutes and remainder:
            return f"{minutes}m {remainder}s"
        if minutes:
            return f"{minutes} min"
        return f"{remainder}s"

    def _student_set_assignment_catalog(
        self, assignments: list[dict], packs: list[dict]
    ) -> None:
        """Cache teacher assignment metadata for consistent student screens."""
        if not isinstance(assignments, list):
            return
        self._teacher_assignments = [
            item for item in assignments if isinstance(item, dict)
        ]
        self._teacher_pack_catalog = {}
        for item in packs:
            if not isinstance(item, dict):
                continue
            for key in (item.get("pack_id"), item.get("lesson_id")):
                if key:
                    self._teacher_pack_catalog[str(key)] = item

    def _student_annotate_assignments(self, lessons: list[dict]) -> list[dict]:
        """Label local lessons as assigned or merely published.

        A missing teacher pack remains in the separate Classroom assignment
        view, where it can truthfully be shown as waiting for the lesson. This
        helper only annotates lessons that already exist locally.
        """
        assignments = getattr(self, "_teacher_assignments", None)
        if not isinstance(assignments, list) or not assignments:
            return lessons

        annotated: list[dict] = []
        for lesson in lessons:
            lesson_copy = dict(lesson)
            lesson_id = str(lesson.get("lesson_id", ""))
            pack_file = lesson.get("pack_file")
            pack_name = Path(pack_file).stem if pack_file else ""
            assignment = next(
                (
                    item
                    for item in assignments
                    if str(item.get("pack_id", "")) in {lesson_id, pack_name}
                    or str(item.get("lesson_id", "")) == lesson_id
                ),
                None,
            )
            lesson_copy["assignment"] = assignment
            catalog = getattr(self, "_teacher_pack_catalog", {})
            published = any(
                key in catalog
                for key in (lesson_id, pack_name)
                if key
            )
            lesson_copy["assignment_state"] = (
                "assigned" if assignment else "published" if published else ""
            )
            annotated.append(lesson_copy)
        return annotated

    def _student_recent_report(self) -> tuple[dict | None, Path | None]:
        """Return the latest local report without exposing its file path."""
        current = getattr(self, "last_report", None)
        if current is not None:
            try:
                return current.to_dict(), None
            except Exception:
                logger.debug("Could not serialize the current student report", exc_info=True)

        from ..reports import ReportStore

        try:
            store = ReportStore()
            reports = sorted(
                store.paths["reports"].glob("*.json"),
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )
            for report_path in reports[:10]:
                try:
                    data = json.loads(report_path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if isinstance(data, dict):
                    return data, report_path
        except Exception:
            logger.debug("Could not load recent student reports", exc_info=True)
        return None, None

    def _student_resume_lesson(self) -> None:
        """Open the most relevant lesson directly from the student home."""
        from ..workspace import get_current_lesson, list_available_lessons

        lessons = self._student_annotate_assignments(list_available_lessons())
        current = get_current_lesson()
        current_id = current.get("lesson_id", "") if current else ""
        target_index = None
        if current and current.get("status", "in_progress") != "completed":
            target_index = next(
                (
                    index
                    for index, lesson in enumerate(lessons)
                    if current_id and lesson.get("lesson_id") == current_id
                ),
                None,
            )
        if target_index is None:
            target_index = next(
                (
                    index
                    for index, lesson in enumerate(lessons)
                    if lesson.get("status") == "in_progress"
                ),
                None,
            )
        if target_index is None:
            target_index = next(
                (
                    index
                    for index, lesson in enumerate(lessons)
                    if lesson.get("status") != "completed"
                ),
                None,
            )

        if target_index is None:
            if lessons:
                student_info("All available lessons are complete. Choose one from More → Lessons to review it.")
            else:
                student_info("No lessons yet. Connect to your teacher, then choose Sync.")
            input("\n  Press Enter...")
            return
        self._start_lesson_by_number(str(target_index + 1), lessons)

    def _student_report_summary(self, report: dict | None) -> str | None:
        """Build one concise, student-facing line for a saved test result."""
        if not report:
            return None
        title = report.get("title") or report.get("lesson_title") or "current lesson"
        if report.get("passed"):
            checks = report.get("tests_passed", report.get("tests_total", 0))
            return f"{title} — passed ({checks} checks)"
        failed = report.get("tests_failed", 0)
        return f"{title} — {failed} check(s) need another look"

    def _student_auto_pull_teacher_packs(self) -> dict:
        """Quietly pull assigned lesson/quiz packs once on student-mode entry."""
        from ..student_client import StudentClient, update_packs_from_server

        client = StudentClient.from_config()
        if not client.teacher_url or not client.token:
            return {"skipped": True, "reason": "not connected"}
        try:
            packs = client.list_packs()
            try:
                assignments = client.check_assignments()
            except Exception:
                assignments = None
            if isinstance(assignments, list):
                self._student_set_assignment_catalog(assignments, packs)
            result = update_packs_from_server(client, packs, quiet=True)
            self._apply_teacher_pack_updates(result, packs, quiet=True)
            if result.get("errors"):
                logger.info("Student pack sync had errors: %s", result["errors"])
                self._student_connection_state = "offline"
            else:
                self._student_connection_state = "connected"
            return result
        except Exception as exc:
            logger.info("Student pack sync failed on mode entry: %s", exc)
            self._student_connection_state = "offline"
            return {"skipped": True, "reason": str(exc), "errors": [str(exc)]}

    def _apply_teacher_pack_updates(
        self,
        result: dict,
        packs: list[dict],
        *,
        quiet: bool = False,
    ) -> dict:
        """Refresh visible student assignments after teacher pack updates.

        Downloading a new ``.duckypack`` is not enough: an existing assignment
        folder contains the runnable tests and instructions.  Apply only packs
        identified as updates, preserving starter edits and keeping a backup.
        """
        updated_ids = set(result.get("updated_pack_ids", []))
        if not updated_ids:
            return result

        from ..paths import app_dir
        from ..workspace import get_student_assignments_dir, refresh_lesson_from_pack

        conflicts: list[str] = []
        applied = 0
        for pack in packs:
            pack_id = str(pack.get("pack_id", ""))
            if pack_id not in updated_ids:
                continue
            pack_path = app_dir() / "packs" / f"{pack_id}.duckypack"
            try:
                refreshed = refresh_lesson_from_pack(
                    pack_path,
                    destination_root=get_student_assignments_dir(),
                )
            except Exception as exc:
                result.setdefault("errors", []).append(f"{pack_id}: assignment refresh failed: {exc}")
                logger.info("Could not refresh updated assignment %s", pack_id, exc_info=True)
                continue
            if refreshed.get("updated") or refreshed.get("created"):
                applied += 1
            for conflict in refreshed.get("conflicts", []):
                conflicts.append(f"{pack.get('title', pack_id)}: {conflict}")

        if applied:
            result["workspace_updates"] = applied
        if conflicts:
            result["workspace_conflicts"] = conflicts
            if not quiet:
                student_warning(
                    "Teacher updates arrived. Your edits were kept; "
                    f"{len(conflicts)} incoming starter file(s) are in the lesson's hidden conflict folder."
                )
        return result

    def _student_sync_on_entry(self) -> None:
        """Perform the one-time lesson/quiz discovery for a student session."""
        if getattr(self, "_auto_pull_done", False):
            return
        self._auto_pull_done = True
        # This is deliberately quiet and safe for an in-progress lesson. The
        # explicit Sync action remains the place where edit-conflict prompts
        # are shown.
        result = self._student_auto_pull_teacher_packs()
        if result.get("downloaded", 0) > 0:
            student_success(f"Synced {result['downloaded']} teacher lesson pack(s).")

    def _student_quiz_overview(self, lessons: list[dict]) -> list[dict]:
        """Return a small, human-readable status row for each available quiz.

        The old student home only exposed a quiz after a lesson had become the
        current lesson. That made teacher-assigned quizzes look missing even
        when the pack was present. Keep this read-only and cheap: inspect local
        packs and the bundled practice catalog, then read the existing local
        attempt/draft store once. Practice quizzes do not require a lesson
        workspace, so a brand-new student can start learning immediately.
        """
        from ..quiz import (
            QuizResultsStore,
            load_general_quizzes,
            load_quiz_from_pack,
            quiz_fingerprint,
        )

        rows: list[dict] = []
        store = QuizResultsStore()
        teacher_quiz_ids: set[str] = set()
        try:
            for index, lesson in enumerate(lessons):
                if not lesson.get("has_quizzes"):
                    continue
                try:
                    pack_file = lesson.get("pack_file")
                    quiz = load_quiz_from_pack(pack_file) if pack_file else None
                    if quiz is None:
                        # Do not advertise a dead-end quiz when the archive is
                        # corrupt or the teacher's manifest is stale.
                        continue
                    teacher_quiz_ids.add(quiz.quiz_id)
                    fingerprint = quiz_fingerprint(quiz)
                    stats = store.get_stats(quiz.quiz_id, fingerprint)
                    draft = store.get_draft(quiz.quiz_id, fingerprint)
                    if draft is not None:
                        status = "resume"
                    elif not stats["attempts"]:
                        status = "not started"
                    elif stats["last_passed"]:
                        status = f"passed · best {stats['best']}/{len(quiz.questions)}"
                    else:
                        status = f"needs review · best {stats['best']}/{len(quiz.questions)}"
                    rows.append(
                        {
                            "index": index,
                            "lesson": lesson,
                            "quiz": quiz,
                            "status": status,
                            "draft": draft is not None,
                            "teacher_assigned": True,
                        }
                    )
                except Exception:
                    # One broken teacher pack should not hide healthy quizzes
                    # from the rest of the student's Quiz Center.
                    logger.info(
                        "Skipping unusable quiz for lesson %s",
                        lesson.get("lesson_id", ""),
                        exc_info=True,
                    )

            # A fresh install has no lesson rows yet. Still expose every valid
            # bundled practice quiz, and let local files override bundled
            # copies by stable quiz ID without showing duplicates.
            for quiz in load_general_quizzes():
                if quiz.quiz_id in teacher_quiz_ids:
                    continue
                try:
                    fingerprint = quiz_fingerprint(quiz)
                    stats = store.get_stats(quiz.quiz_id, fingerprint)
                    draft = store.get_draft(quiz.quiz_id, fingerprint)
                    if draft is not None:
                        status = "resume"
                    elif not stats["attempts"]:
                        status = "not started"
                    elif stats["last_passed"]:
                        status = f"passed · best {stats['best']}/{len(quiz.questions)}"
                    else:
                        status = f"needs review · best {stats['best']}/{len(quiz.questions)}"
                    rows.append(
                        {
                            "index": None,
                            "lesson": {
                                "lesson_id": quiz.lesson_id or f"practice.{quiz.quiz_id}",
                                "title": quiz.title,
                                "status": "not_started",
                                "workspace_dir": None,
                                "pack_file": None,
                                "is_general_quiz": True,
                            },
                            "quiz": quiz,
                            "status": status,
                            "draft": draft is not None,
                            "teacher_assigned": False,
                        }
                    )
                except Exception:
                    # One malformed local override must not hide the rest of
                    # the bundled practice catalog.
                    logger.info("Skipping unusable practice quiz %s", quiz.quiz_id, exc_info=True)
        except Exception:
            logger.info("Could not build student quiz overview", exc_info=True)
        finally:
            store.close()
        return rows

    def _student_quiz_center(self) -> None:
        """Give quizzes a first-class home instead of hiding them in a lesson."""
        from ..workspace import list_available_lessons

        lessons = self._student_annotate_assignments(list_available_lessons())
        rows = self._student_quiz_overview(lessons)
        while True:
            if not rows:
                clear()
                student_heading("Quiz Center")
                navigation_hint("Student", "Home / Quiz Center")
                declared = any(
                    lesson.get("quiz_declared") and not lesson.get("has_quizzes")
                    for lesson in lessons
                )
                if declared:
                    student_warning(
                        "A teacher quiz is announced, but this copy is incomplete. "
                        "Sync once more or ask your teacher to republish it."
                    )
                else:
                    student_info(
                        "No quizzes are installed yet. Sync after your teacher publishes one."
                    )
                print(f"\n  {STUDENT_ACCENT}[s]{RESET} Sync now    {DIM}[0] Back{RESET}")
                choice = student_prompt("Choose", "0").strip().lower()
                if choice in ("s", "sync"):
                    self._update_flow()
                    lessons = self._student_annotate_assignments(list_available_lessons())
                    rows = self._student_quiz_overview(lessons)
                    continue
                return
            clear()
            student_heading("Quiz Center")
            navigation_hint("Student", "Home / Quiz Center")
            print(
                f"  {DIM}Choose a quiz to start, resume, or retry. "
                f"Answers and history stay on this computer.{RESET}\n"
            )
            for number, row in enumerate(rows, 1):
                quiz = row["quiz"]
                time_label = (
                    f" · {self._quiz_duration_label(quiz.time_limit_seconds)}"
                    if quiz.time_limit_seconds
                    else ""
                )
                state = row["status"]
                state_color = GREEN if state == "resume" or state.startswith("passed") else YELLOW
                print(
                    f"  {STUDENT_ACCENT}[{number}]{RESET}  {quiz.title}"
                    f"  {state_color}{state}{RESET}"
                    f"  {DIM}{len(quiz.questions)} questions{time_label}{RESET}"
                )
                if row["lesson"].get("status") == "completed":
                    print(f"       {DIM}Lesson complete — retry anytime for practice.{RESET}")
            print(f"\n  {DIM}[0] Back{RESET}")
            choice = student_prompt("Choose a quiz", "0").strip().lower()
            if choice in ("0", "q"):
                return
            if choice in ("?", "help"):
                student_info("Pick a number. Resume keeps a saved attempt; 0 returns home.")
                input("\n  Press Enter...")
                continue
            try:
                selected = rows[int(choice) - 1]
            except (ValueError, IndexError):
                student_error("Choose one of the quiz numbers shown, or 0 to go back.")
                continue

            lesson = selected["lesson"]
            print(
                f"\n  {DIM}[Enter] start/resume  [h] history  [0] back{RESET}"
            )
            action = student_prompt("Next", "").strip().lower()
            if action in ("0", "q"):
                continue
            if action in ("h", "history"):
                self._quiz_history_flow(
                    lesson=lesson,
                    quiz=selected["quiz"],
                    teacher_assigned=selected.get("teacher_assigned", False),
                )
                continue
            if action not in ("", "start", "resume", "s"):
                student_error("Press Enter to start, h for history, or 0 to go back.")
                continue
            if not lesson.get("is_general_quiz") and (
                not lesson.get("workspace_dir")
                or not Path(lesson["workspace_dir"]).is_dir()
            ):
                student_info(f"Start '{lesson.get('title', 'this lesson')}' before taking its quiz.")
                if student_confirm("Open this lesson now?"):
                    self._start_lesson_by_number(str(selected["index"] + 1), lessons)
                    lessons = self._student_annotate_assignments(list_available_lessons())
                    rows = self._student_quiz_overview(lessons)
                continue
            self._quiz_flow(
                lesson,
                quiz=selected["quiz"],
                teacher_assigned=selected.get("teacher_assigned", False),
            )
            lessons = self._student_annotate_assignments(list_available_lessons())
            rows = self._student_quiz_overview(lessons)

    # ─── Student Mode ────────────────────────────────────────

    def _student_lessons_menu(self):
        from ..background_sync import BackgroundSync
        from ..workspace import get_progress, list_available_lessons

        # Student build: check for app updates when entering lessons
        if not getattr(self, "_student_auto_update_done", False):
            self._student_auto_update_done = True
            from .. import __build_type__
            if __build_type__ != "full":
                self._check_auto_update()

        self._student_sync_on_entry()

        config = self._load_config()
        if config.get("auto_update", False):
            self._background_sync = BackgroundSync(
                on_update=self._on_background_update,
                on_announcement=self._on_announcements,
            )
            self._background_sync.start()

        try:
            while True:
                clear()
                # Show queued notifications
                if self._notifications:
                    show_notifications(self._notifications)
                    self._notifications.clear()

                if self._pending_update_notification:
                    result = self._pending_update_notification
                    self._pending_update_notification = None
                    if result.get("downloaded", 0) > 0:
                        msg = f"Background sync: {result['downloaded']} pack(s) updated"
                        if result.get("updated", 0):
                            msg += f" ({result['updated']} updated)"
                        student_success(msg)
                student_banner()
                student_separator()
                student_heading("Lessons and Quizzes")
                navigation_hint("Student", "Home / Lessons")

                # Show background sync status if configured
                config = self._load_config()
                if config.get("teacher_url") and self._background_sync and self._background_sync.is_running:
                    sync_status = self._background_sync.get_status()
                    if sync_status and sync_status.get("last_error"):
                        print(f"  {YELLOW}! Sync issue:{RESET} {sync_status['last_error']}{' ' * 20}")
                    elif sync_status and sync_status.get("last_success"):
                        print(f"  {GREEN}● Synced{RESET}  (last: {sync_status['last_success'][:19]}){' ' * 20}")
                    else:
                        print(f"  {GREEN}● Connected{RESET}  to teacher server{' ' * 20}")
                elif config.get("teacher_url"):
                    print(f"  {DIM}○ Not syncing{RESET}  (sync not started){' ' * 20}")

                lessons = self._student_annotate_assignments(list_available_lessons())
                progress = get_progress()
                quiz_lesson_ids = {
                    row["lesson"].get("lesson_id")
                    for row in self._student_quiz_overview(lessons)
                }

                if progress["total"] > 0:
                    card("Your Progress", [progress_bar(progress["completed"], progress["total"])])

                if not lessons:
                    print(
                        f"  {DIM}No lessons found. Use [c] to connect to your teacher, then [u] to pull lessons.{RESET}"
                    )
                else:
                    for i, lesson in enumerate(lessons, 1):
                        status = lesson["status"]
                        if status == "completed":
                            icon = f"{GREEN}✓ completed{RESET}"
                        elif status == "in_progress":
                            icon = f"{YELLOW}→ in progress{RESET}"
                        else:
                            icon = f"{DIM}○ not started{RESET}"
                        assignment_state = lesson.get("assignment_state")
                        assignment_label = ""
                        if assignment_state == "assigned":
                            assignment_label = f"  {GREEN}assigned{RESET}"
                            due_date = (lesson.get("assignment") or {}).get("due_date")
                            if due_date:
                                assignment_label += f"  {DIM}due {due_date}{RESET}"
                        elif assignment_state == "published":
                            assignment_label = f"  {DIM}published{RESET}"
                        print(
                            f"  {STUDENT_ACCENT}[{i}]{RESET}  {lesson['title']:<30}"
                            f" {icon}{assignment_label}"
                        )
                        if lesson.get("lesson_id") in quiz_lesson_ids:
                            print(f"       {DIM}{CYAN}? quiz available{RESET}")
                        elif lesson.get("quiz_declared"):
                            print(
                                f"       {YELLOW}! teacher quiz needs sync or repair{RESET}"
                            )

                print()
                student_separator()
                n_lessons = len(lessons) if lessons else 0
                print(f"  {STUDENT_ACCENT}[1-{n_lessons}]{RESET}  Start a lesson (enter number)")
                print(f"  {STUDENT_ACCENT}[t]{RESET}           Run tests on current lesson")
                print(f"  {STUDENT_ACCENT}[q]{RESET}           Take lesson quiz")
                print(f"  {STUDENT_ACCENT}[v]{RESET}           View quiz history")
                print(f"  {STUDENT_ACCENT}[h]{RESET}           Get help / chat with tutor")
                print(f"  {STUDENT_ACCENT}[c]{RESET}           Connect to teacher")
                print(f"  {STUDENT_ACCENT}[u]{RESET}           Pull new lessons from teacher")
                print(f"  {STUDENT_ACCENT}[0]{RESET}           Back")
                print()

                choice = student_prompt("Choose", "0")

                if choice in ("?", "help"):
                    student_info("Enter a lesson number to open it. t tests, q quiz, c connect, u sync, 0 returns home.")
                    input("\n  Press Enter...")
                elif choice == "0":
                    break
                elif choice == "t":
                    self._run_tests_flow()
                elif choice == "q":
                    self._quiz_flow()
                elif choice == "v":
                    self._quiz_history_flow()
                elif choice == "h":
                    self._hint_chat_flow()
                elif choice == "c":
                    self._connect_flow()
                elif choice == "u":
                    self._update_flow()
                else:
                    num_to_start = None
                    if choice.isdigit() and int(choice) > 0:
                        num_to_start = choice
                    elif choice.startswith("s"):
                        parts = choice.split()
                        if len(parts) >= 2:
                            num_to_start = parts[1]
                    if num_to_start:
                        self._start_lesson_by_number(num_to_start, lessons)
                    else:
                        student_error("Invalid choice. Try again.")
        finally:
            if self._background_sync:
                self._background_sync.stop()
                self._background_sync = None

    # Compatibility for callers that used the pre-unification method name.
    # New navigation enters ``_student_build_menu`` for the shared home and
    # uses ``_student_lessons_menu`` for the detailed list.
    def _student_mode_menu(self):
        return self._student_lessons_menu()

    def _on_background_update(self, result: dict) -> None:
        self._pending_update_notification = result

    def _on_announcements(self, announcements: list[dict]) -> None:
        if not announcements:
            return
        # The server returns announcements newest-first. Surface the newest
        # message rather than the oldest item in the response.
        latest = announcements[0]
        msg = latest.get("message", "")
        # Append in the dict form consumed by show_notifications() — a bare
        # string would raise AttributeError when it calls n.get("level", ...).
        self._notifications.append(
            {"level": "info", "message": f"Announcement: {msg}", "source": "teacher"}
        )

    def _student_progress_menu(self):
        """Show a student-friendly progress summary without teacher reports."""
        from ..workspace import get_progress, list_available_lessons

        clear()
        student_banner()
        student_separator()
        student_heading("My Progress")

        try:
            lessons = self._student_annotate_assignments(list_available_lessons())
            progress = get_progress()
        except Exception as exc:
            logger.exception("Could not load student progress", exc_info=exc)
            student_error("Progress is temporarily unavailable.")
            input("\n  Press Enter...")
            return

        total = int(progress.get("total", 0) or 0)
        completed = int(progress.get("completed", 0) or 0)
        if total:
            card("Course progress", [progress_bar(completed, total)])
        else:
            student_info("Start a lesson to begin tracking your progress.")

        if lessons:
            print()
            for lesson in lessons:
                status = lesson.get("status", "not_started")
                if status == "completed":
                    label = f"{GREEN}✓ Complete{RESET}"
                elif status == "in_progress":
                    label = f"{YELLOW}→ In progress{RESET}"
                else:
                    label = f"{DIM}○ Not started{RESET}"
                print(f"  {STUDENT_ACCENT}{lesson.get('title', 'Untitled')}{RESET}  {label}")
        else:
            print("  No lessons are available yet.")

        input("\n  Press Enter...")

    def _start_lesson_by_number(self, num_str, lessons):
        from ..workspace import get_lesson_instructions, get_student_assignments_dir, start_lesson

        try:
            idx = int(num_str) - 1
        except (ValueError, TypeError):
            student_error("Please enter a valid number.")
            input("  Press Enter...")
            return

        if idx < 0 or idx >= len(lessons):
            student_error(f"Invalid lesson number. Enter 1-{len(lessons)}.")
            input("  Press Enter...")
            return

        lesson = lessons[idx]
        pack_file = lesson.get("pack_file")

        if not pack_file or not Path(pack_file).is_file():
            student_error(f"Pack file not found for '{lesson['title']}'. Try pulling lessons again.")
            input("  Press Enter...")
            return

        clear()

        try:
            # Keep the student's editable files in the repository so the
            # Codespaces Explorer shows exactly what they should work on.
            lesson_dir = start_lesson(
                pack_file,
                destination_root=get_student_assignments_dir(),
            )
        except Exception as exc:
            student_error(f"Failed to start lesson: {exc}")
            input("  Press Enter...")
            return

        if not lesson_dir or not lesson_dir.is_dir():
            student_error("Lesson directory was not created properly.")
            input("  Press Enter...")
            return

        entry_file = lesson.get("entry_file", "")
        entry_path = lesson_dir / entry_file if entry_file else None

        if entry_path and not entry_path.is_file():
            student_error(f"Code file not found: {entry_path}")
            input("  Press Enter...")
            return

        # Give the student a short handoff; the complete instructions belong
        # in the repository file rather than flooding the terminal screen.
        instructions = lesson.get("instructions")
        if not instructions:
            instructions = get_lesson_instructions(lesson["lesson_id"])

        instructions_file = lesson_dir / "instructions.md"
        has_instructions_file = instructions_file.is_file()
        lines = [
            f"{GREEN}✓{RESET} Lesson folder ready in your repository.",
            f"{CYAN}📁 Folder:{RESET} {lesson_dir}",
            f"{CYAN}📝 Edit:{RESET} {entry_file or 'the starter code file'}",
            f"{CYAN}🧪 Test:{RESET} python run_tests.py",
        ]
        if has_instructions_file:
            lines.insert(1, f"{CYAN}📋 Read first:{RESET} {instructions_file}")
        elif instructions:
            lines.insert(1, f"{YELLOW}!{RESET} Instructions are available in Ducky, but no file was packaged.")
        card(f"Ready: {lesson['title']}", lines)

        from ..editor import show_lesson_info

        show_lesson_info(lesson_dir, entry_file, lesson["title"])

        print(f"\n  Test from the repo with {BOLD}python run_tests.py{RESET}, or select {BOLD}[t] Run tests{RESET} in Ducky.")
        print(f"  {DIM}The full instructions and starter code are now files in the folder above.{RESET}")
        print()
        input("  Press Enter to continue...")

    def _build_student_tutor_engine(self):
        from ..config import load_config
        from ..llm_client import GeminiClient, LMStudioClient, OllamaClient, OpenAIClient
        from ..student_client import StudentClient
        from ..tutor import TutorEngine

        config = load_config()

        client = StudentClient.from_config()
        if client.teacher_url:
            try:
                status = client.status()
                if status:
                    return TutorEngine(remote_client=client)
            except Exception:
                logger.info("Teacher server not reachable — will use local LLM if configured")

        backend = str(config.get("llm_backend", "none"))
        if backend == "ollama":
            model = str(config.get("ollama_model", "qwen2.5-coder:1.5b"))
            url = str(config.get("ollama_url", "http://localhost:11434"))
            return TutorEngine(OllamaClient(model=model, base_url=url))
        elif backend == "lmstudio":
            model = str(config.get("lmstudio_model", "default"))
            url = str(config.get("lmstudio_url", "http://localhost:1234"))
            max_tokens = int(config.get("lmstudio_max_tokens", 2500))
            return TutorEngine(LMStudioClient(model=model, base_url=url, max_tokens=max_tokens))
        elif backend == "gemini":
            api_key = str(config.get("gemini_api_key", ""))
            model = str(config.get("gemini_model", "gemini-2.5-flash"))
            return TutorEngine(GeminiClient(api_key=api_key, model=model))
        elif backend == "openai":
            api_key = str(config.get("openai_api_key", ""))
            model = str(config.get("openai_model", "gpt-4o-mini"))
            base_url = str(config.get("openai_base_url", "https://api.openai.com/v1"))
            return TutorEngine(OpenAIClient(api_key=api_key, model=model, base_url=base_url))

        return None

    def _run_tests_flow(self):
        from ..display import render_student_report
        from ..workflows import resolve_current_lesson, run_tests_workflow, save_and_record

        while True:
            resolved = resolve_current_lesson()
            if resolved is None:
                input("  Press Enter...")
                return

            clear()
            student_heading(f"Running tests: {resolved['title']}")

            tutor_engine = self._build_student_tutor_engine()

            try:
                report = run_tests_workflow(
                    resolved["pack_path"],
                    student_file=str(resolved["entry_path"]),
                    timeout_seconds=15,
                    tutor_engine=tutor_engine,
                    lesson_id=resolved["lesson_id"],
                )
            except Exception as exc:
                logger.exception("Test run failed")
                student_error(f"Test run failed: {exc}")
                input("  Press Enter...")
                return

            save_and_record(report, lesson_id=resolved["lesson_id"])
            # Keep the just-finished run available to the home Submit action.
            # Otherwise students must rediscover a report from disk immediately
            # after running it.
            self.last_report = report
            self.current_pack = resolved.get("pack_path") or None
            self.current_submission = str(resolved["entry_path"])

            print(render_student_report(report))

            if report.passed:
                print(f"\n  {GREEN}All tests passed! Great work!{RESET}")
            else:
                print(f"\n  Edit your code and run {BOLD}ducky test{RESET} again.")

            print()
            print(f"  {STUDENT_ACCENT}[r]{RESET}  Re-run tests")
            print(f"  {STUDENT_ACCENT}[q]{RESET}  Back")
            print()

            choice = input(f"  {STUDENT_PROMPT}Choose{RESET}: ").strip().lower()
            if choice == "" or choice == "r":
                continue  # re-run on Enter
            elif choice == "q":
                break

    @staticmethod
    def _parse_option_index(ans: str, n: int) -> int | None:
        """Resolve a multiple-choice selection to a 0-based index.

        Accepts a 1-based number (``1``..``n``) or a single UPPERCASE letter
        (``A``..``A+n-1``). Option letters are uppercase on purpose so they can
        never collide with the lowercase command keys ``b`` (back) and ``q``
        (quit) that the prompt advertises — a lowercase ``b`` always means back,
        never option 2. Returns ``None`` for anything else (empty strings,
        out-of-range values, lowercased/garbage tokens) so the caller re-prompts
        instead of silently advancing with a bogus answer.
        """
        if not ans:
            return None
        token = ans.strip()
        if token.isdigit():
            idx = int(token) - 1
            return idx if 0 <= idx < n else None
        if len(token) == 1 and "A" <= token.upper() <= "Z":
            idx = ord(token.upper()) - ord("A")
            return idx if 0 <= idx < n else None
        return None

    def _quiz_prompt(self, q: QuizQuestion) -> str | None:
        """Ask one quiz question and return a validated answer.

        Reuses :func:`ducky.quiz._parse_bool` for true/false normalization so a
        student's ``t``/``f``/``yes``/``no`` variants grade identically to the
        CLI path.

        Returns:
          * the canonical answer string for a valid response,
          * the sentinel ``"__back__"`` when the student entered ``b``,
          * the sentinel ``"__map__"`` when the student entered ``m``,
          * the sentinel ``"__help__"`` when the student entered ``?``,
          * ``None`` when the student entered ``q`` (pause / discard).

        Invalid or empty input is rejected and the student is re-prompted with a
        clear message; the flow never advances on a bad answer. ``predict_output``
        uses the themed ``student_prompt`` (consistent with every other type) and
        permits an empty answer, which grades as "the program prints nothing".
        """
        from ..quiz import _parse_bool

        last_err: str | None = None
        while True:
            if q.type == "multiple_choice":
                letters = "".join(chr(ord("A") + i) for i in range(len(q.options)))
                for oi, opt in enumerate(q.options, 1):
                    letter = chr(ord("A") + oi - 1)
                    print(f"     {STUDENT_ACCENT}{oi}.{RESET} {opt}  {DIM}({letter}){RESET}")
                ans = student_prompt(
                    f"Your answer (1-{len(q.options)} or {letters}) [b/m/q/?]"
                ).strip()
            elif q.type == "true_false":
                ans = student_prompt("Your answer (true/false) [b/m/q/?]").strip()
            elif q.type == "predict_output":
                print(f"     {STUDENT_DIM}{'─' * 40}{RESET}")
                for line in q.code.split("\n"):
                    print(f"     {line}")
                print(f"     {STUDENT_DIM}{'─' * 40}{RESET}")
                # Keep meaningful leading spaces for output predictions. The
                # grader ignores trailing line whitespace and blank lines, but
                # indentation can be part of the expected output.
                raw_ans = student_prompt(
                    "Predicted output (\\n for newline) [b/m/q]", strip=False
                )
                ans = raw_ans if raw_ans.strip() not in ("b", "m", "q", "?") else raw_ans.strip()
            else:
                ans = student_prompt("Your answer [b/m/q/?]").strip()

            # Commands are case-sensitive exact matches: only lowercase "b"
            # (back) and "q" (quit) are reserved. Uppercase option letters (A, B,
            # C…) must NOT be swallowed here — they pass through to the parser,
            # so option selection works even though the prompt shows [b/q].
            if ans == "q":
                return None
            if ans == "b":
                return "__back__"
            if ans == "m":
                return "__map__"
            if ans == "?":
                return "__help__"
            # Re-show the previous error (if any) before re-prompting.
            if last_err is not None:
                student_error(last_err)

            if q.type == "multiple_choice":
                idx = self._parse_option_index(ans, len(q.options))
                if idx is None:
                    first_letter = chr(ord("A"))
                    last_letter = chr(ord("A") + len(q.options) - 1)
                    last_err = f"Please enter a number 1-{len(q.options)} or a capital letter {first_letter}-{last_letter}."
                    continue
                return q.options[idx]
            if q.type == "true_false":
                norm = _parse_bool(ans)
                if norm is None:
                    last_err = "Please answer true or false."
                    continue
                return "true" if norm else "false"
            if q.type == "predict_output":
                # Empty is allowed: a program that prints nothing is a valid answer.
                return ans
            # Unknown types: accept any non-empty raw answer.
            if not ans:
                last_err = "Please enter an answer."
                continue
            return ans

    def _quiz_question_map(self, session) -> None:
        """Show answer state and let a student jump back to a question."""
        while True:
            clear()
            student_heading("Quiz Question Map")
            navigation_hint("Student", "Quiz / Question Map")
            for index, question in enumerate(session.questions, 1):
                if question.id in session.answers:
                    marker = f"{GREEN}answered{RESET}"
                else:
                    marker = f"{YELLOW}not answered{RESET}"
                current = f" {STUDENT_ACCENT}< current{RESET}" if index - 1 == session.position else ""
                print(f"  {STUDENT_ACCENT}[{index}]{RESET} Q{index} · {marker}{current}")
            print(f"\n  {DIM}Enter a question number to jump, or 0 to return to the quiz.{RESET}")
            choice = student_prompt("Question", "0").strip().lower()
            if choice in ("0", "q"):
                return
            if choice in ("?", "help"):
                student_info("Jumping back lets you edit an answer. Unanswered questions are marked clearly.")
                input("\n  Press Enter...")
                continue
            try:
                target = int(choice) - 1
            except ValueError:
                target = -1
            if 0 <= target < len(session.questions):
                session.position = target
                return
            student_error("Choose a question number from the map, or 0 to return.")

    def _quiz_review_missed(self, quiz, result) -> None:
        """Step through each missed question interactively for learning.

        Shows the full question (prompt + options / code box), the student's
        selected answer in {RED}red{RESET} versus the correct answer in
        {GREEN}green{RESET}, and the explanation. Purely a display pass — it
        never mutates answers or records anything. Returns when the student
        finishes or presses ``q`` early.
        """
        missed = [(i, pq) for i, pq in enumerate(result.per_question, 1) if not pq["correct"]]
        if not missed:
            return
        by_id = {q.id: q for q in quiz.questions}

        for i, pq in missed:
            q = by_id.get(pq["id"])
            if q is None:
                continue
            clear()
            student_heading(f"Review — Q{i} (missed)")
            navigation_hint("Student", "Quiz / Review")
            print(f"  {q.prompt}")

            if q.type == "multiple_choice":
                for oi, opt in enumerate(q.options, 1):
                    letter = chr(ord("A") + oi - 1)
                    if opt == pq["correct_answer"]:
                        tag = f"{GREEN}✓ correct{RESET}"
                    elif opt == pq["selected"]:
                        tag = f"{RED}✗ your answer{RESET}"
                    else:
                        tag = ""
                    print(f"     {STUDENT_ACCENT}{oi}.{RESET} {opt}  {DIM}({letter}){RESET}  {tag}")
            elif q.type == "true_false":
                print(f"     {RED}Your answer:    {pq['selected'] or '(none)'}{RESET}")
                print(f"     {GREEN}Correct answer: {pq['correct_answer']}{RESET}")
                # For true/false the auto-grader's detail adds the "unparseable
                # answer" hint that the contrast above doesn't capture.
                if pq.get("detail") and pq["detail"] != f"Correct answer: {pq['correct_answer']}":
                    print(f"     {DIM}{pq['detail']}{RESET}")
            elif q.type == "predict_output":
                print(f"     {STUDENT_DIM}{'─' * 40}{RESET}")
                for line in q.code.split("\n"):
                    print(f"     {line}")
                print(f"     {STUDENT_DIM}{'─' * 40}{RESET}")
                print(f"     {RED}Your output:      {pq['selected'] or '(nothing)'}{RESET}")
                print(f"     {GREEN}Correct output:  {pq['correct_answer'] or '(nothing)'}{RESET}")
                print(f"     {DIM}Graded by exact output match (trailing whitespace and blank lines ignored).{RESET}")
            else:
                print(f"     {RED}Your answer: {pq['selected'] or '(none)'}{RESET}")
                print(f"     {GREEN}Correct:    {pq['correct_answer']}{RESET}")
                if pq.get("detail"):
                    print(f"     {DIM}{pq['detail']}{RESET}")

            if pq["explanation"]:
                print(f"\n  {DIM}Why: {pq['explanation']}{RESET}")
            print()
            if student_prompt("Press Enter for next • q to finish review", "n").strip().lower() == "q":
                return

    def _quiz_show_weak_spots(self, quiz) -> None:
        """Print a recurring-weak-spots note, if the student has enough history.

        Re-grades each prior attempt against the current quiz (so the summary
        stays right even if the quiz was edited since the attempt) and reports
        only questions missed in 2+ attempts, with the teacher-authored
        explanation when present. Mirrors the CLI ``ducky quiz --status`` view.
        """
        from ..quiz import QuizResultsStore, grade_quiz, quiz_fingerprint

        store = QuizResultsStore()
        try:
            attempts = store.get_attempts(quiz.quiz_id, quiz_fingerprint(quiz))
        finally:
            store.close()

        # Need at least two attempts for a meaningful "you keep missing this"
        # signal; with one attempt the per-question list already covers it.
        if len(attempts) < 2 or not quiz.questions:
            return

        misses: dict[str, int] = {}
        for att in attempts:
            graded = grade_quiz(quiz, att["answers"], questions=quiz.questions)
            for pq in graded.per_question:
                if not pq["correct"]:
                    misses[pq["id"]] = misses.get(pq["id"], 0) + 1

        recurring = [
            (qid, count)
            for qid, count in misses.items()
            if count >= 2
        ]
        if not recurring:
            return

        by_id = {q.id: q for q in quiz.questions}
        # Most-missed first, tie-broken by question order.
        recurring.sort(key=lambda kv: (-kv[1], quiz.questions.index(by_id[kv[0]])))

        print(f"\n  {YELLOW}Weak spots (missed across attempts):{RESET}")
        for qid, count in recurring:
            q = by_id.get(qid)
            if q is None:
                continue
            pct = f"{round(100 * count / len(attempts))}% of attempts"
            print(f"     {DIM}• Q{qid}: missed {count}× ({pct}) — {q.prompt}{RESET}")
            if q.explanation:
                print(f"       {DIM}Why: {q.explanation}{RESET}")

    def _quiz_show_results(self, quiz, label, result, stats) -> bool:
        """Render the end-of-quiz review screen.

        Shows a per-question ✓/✗ grid, a color-coded missed-question summary,
        and a pass/fail banner, then offers ``[r] Retry``, ``[v] Review missed``
        (only when something was missed), and ``[q] Back``. Returns ``True`` to
        leave the quiz, ``False`` to take another attempt.
        """
        clear()
        verdict = "passed" if result.passed else "not passed"
        student_heading(f"Results — {result.correct}/{result.total} correct ({result.score_pct}%) · {verdict}")
        navigation_hint("Student", "Quiz / Results")

        grid = []
        for i, pq in enumerate(result.per_question, 1):
            mark = f"{GREEN}✓{RESET}" if pq["correct"] else f"{RED}✗{RESET}"
            grid.append(f"{mark} {BOLD}Q{i}{RESET}")
        # Print the grid compactly, a few marks per line.
        for start in range(0, len(grid), 4):
            print("  " + "  ".join(grid[start : start + 4]))

        missed = [(i, pq) for i, pq in enumerate(result.per_question, 1) if not pq["correct"]]
        if missed:
            print(f"\n  {YELLOW}Review {len(missed)} missed question(s):{RESET}")
            for i, pq in missed:
                print(
                    f"  {RED}✗{RESET} {BOLD}Q{i}{RESET}  "
                    f"{DIM}Your answer: {RED}{pq['selected'] or '(none)'}{RESET}  "
                    f"•  Correct: {GREEN}{pq['correct_answer']}{RESET}"
                )
                # Surface the auto-grader's rationale at a glance so the student
                # sees *why* it graded the way it did without drilling into the
                # interactive review. predict_output carries a `detail` (e.g.
                # "Expected output:\n…") that is informative; true_false's detail
                # is "Correct answer: …", which merely restates the already-shown
                # correct answer, so it is skipped to avoid noise. Mirrors the
                # CLI results view exactly.
                if pq.get("detail") and pq["detail"] != f"Correct answer: {pq['correct_answer']}":
                    print(f"     {DIM}{pq['detail']}{RESET}")
                if pq.get("explanation"):
                    print(f"     {DIM}Why: {pq['explanation']}{RESET}")
        else:
            print(f"\n  {GREEN}Perfect run — nothing to review.{RESET}")

        if result.passed:
            print(f"\n  {GREEN}Quiz passed! 🎉{RESET}")
        else:
            print(f"\n  {YELLOW}Some answers were incorrect — review and retry.{RESET}")

        # Surface recurring weak spots: re-grade every stored attempt against the
        # *current* quiz so the summary stays accurate even if the quiz was
        # edited since an earlier attempt, then show only questions missed in 2+
        # attempts. This is the GUI counterpart of ``ducky quiz --status``'s weak
        # spots — a GUI student otherwise had no way to see which questions they
        # keep missing. Historical attempts only (>= 2) so the just-finished
        # run's fresh misses aren't double-reported next to the per-question list.
        self._quiz_show_weak_spots(quiz)

        print()

        while True:
            if missed:
                print(f"  {DIM}[r] Retry   [v] Review missed   [q] Back{RESET}")
            else:
                print(f"  {DIM}[r] Retry   [q] Back{RESET}")
            choice = student_prompt("Choose", "q").strip().lower()
            if choice == "r":
                return False
            if choice == "v" and missed:
                self._quiz_review_missed(quiz, result)
                continue
            if choice == "q":
                return True
            # Any other key (typos included) re-prompts instead of silently
            # leaving the quiz — only the explicit [q] Back command exits here.
            student_error("Please choose [r], [v] (if missed), or [q].")

    def _quiz_save_draft(
        self,
        quiz,
        lesson_id: str,
        quiz_fingerprint: str,
        attempt_number: int,
        session,
        deadline_at: str = "",
    ) -> None:
        """Persist the current quiz session without interrupting the student."""
        from ..quiz import QuizResultsStore

        try:
            store = QuizResultsStore()
            try:
                store.save_draft(
                    quiz.quiz_id,
                    lesson_id,
                    quiz_fingerprint,
                    attempt_number,
                    session.position,
                    [question.id for question in session.questions],
                    session.answers,
                    deadline_at=deadline_at,
                )
            finally:
                store.close()
        except Exception:
            # A draft is a convenience; a local database hiccup must not lose
            # the current in-memory attempt or block the quiz.
            logger.warning("Could not save quiz draft", exc_info=True)

    def _quiz_history_flow(
        self,
        lesson: dict | None = None,
        quiz=None,
        teacher_assigned: bool | None = None,
    ):
        """Show all completed attempts for the current lesson quiz."""
        from ..quiz import QuizResultsStore, load_student_quiz
        from ..workflows import resolve_current_lesson

        if lesson is not None:
            lesson_dir = lesson.get("workspace_dir")
            if not lesson.get("is_general_quiz") and (
                not lesson_dir or not Path(lesson_dir).is_dir()
            ):
                student_info("Start this lesson before viewing its quiz history.")
                input("  Press Enter...")
                return
            resolved = {
                # Practice quizzes have no lesson workspace. The history store
                # is keyed by quiz ID/fingerprint, so a harmless placeholder
                # path is enough for this shared flow.
                "lesson_dir": Path(lesson_dir) if lesson_dir else Path.cwd(),
                "lesson_id": lesson.get("lesson_id", ""),
                "title": lesson.get("title", "Lesson"),
                "pack_path": lesson.get("pack_file"),
            }
        else:
            resolved = resolve_current_lesson()
        if resolved is None:
            student_info("Start a lesson before viewing quiz history.")
            input("  Press Enter...")
            return

        if quiz is None:
            quiz, teacher_assigned = load_student_quiz(
                resolved["lesson_dir"], resolved.get("pack_path") or None
            )
        elif teacher_assigned is None:
            teacher_assigned = False
        if quiz is None:
            student_info("No quiz is installed for this lesson yet.")
            input("  Press Enter...")
            return

        from ..quiz import quiz_fingerprint

        fingerprint = quiz_fingerprint(quiz)
        store = QuizResultsStore()
        try:
            attempts = store.get_attempts(quiz.quiz_id, fingerprint)
        finally:
            store.close()

        if not attempts:
            student_info("No completed quiz attempts yet. Choose Take lesson quiz to begin.")
            input("  Press Enter...")
            return

        label = "Teacher quiz" if teacher_assigned else "Practice quiz"
        page_size = 8 if terminal_width() < 60 else 12
        page = 0
        newest_first = list(reversed(attempts))
        while True:
            clear()
            student_heading(f"Quiz History — {quiz.title}")
            navigation_hint("Student", f"Quiz / {quiz.title} / History")
            print(f"  {DIM}{label} · {len(attempts)} completed attempt(s){RESET}")
            print()

            start = page * page_size
            visible = newest_first[start : start + page_size]
            for offset, attempt in enumerate(visible, start + 1):
                status = (
                    f"{GREEN}passed{RESET}"
                    if attempt.get("passed")
                    else f"{YELLOW}not passed{RESET}"
                )
                correct = int(attempt.get("correct", 0) or 0)
                total = int(attempt.get("total", 0) or 0)
                score = round((correct / total) * 100) if total else 0
                taken_at = str(attempt.get("taken_at", "")).replace("T", " ")[:16]
                print(f"  {offset}. {status} · {correct}/{total} correct ({score}%) · {taken_at}")

            print()
            has_next = start + page_size < len(newest_first)
            if has_next:
                print(f"  {DIM}[n] More history   [q] Back{RESET}")
            else:
                print(f"  {DIM}[q] Back{RESET}")
            choice = student_prompt("Choose", "q").strip().lower()
            if choice == "q":
                return
            if choice == "n" and has_next:
                page += 1
                continue
            student_error("Please choose [n] for more history or [q] to go back.")

    def _quiz_flow(
        self,
        lesson: dict | None = None,
        *,
        quiz=None,
        teacher_assigned: bool | None = None,
    ):
        from ..quiz import (
            QuizDraft,
            QuizResultsStore,
            QuizSession,
            grade_quiz,
            latest_missed_question_ids,
            load_student_quiz,
            ordered_questions,
            quiz_deadline_at,
            quiz_fingerprint,
            quiz_seconds_remaining,
        )
        from ..workflows import resolve_current_lesson

        if lesson is not None:
            lesson_dir = lesson.get("workspace_dir")
            if not lesson.get("is_general_quiz") and (
                not lesson_dir or not Path(lesson_dir).is_dir()
            ):
                student_error("Start this lesson before taking its quiz.")
                input("  Press Enter...")
                return
            resolved = {
                "lesson_dir": Path(lesson_dir) if lesson_dir else Path.cwd(),
                "lesson_id": lesson.get("lesson_id", ""),
                "title": lesson.get("title", "Lesson"),
                "pack_path": lesson.get("pack_file"),
            }
        else:
            resolved = resolve_current_lesson()
        if resolved is None:
            student_info("Choose a lesson first, then open Quiz Center to take its quiz.")
            input("  Press Enter...")
            return

        lesson_dir = resolved["lesson_dir"]
        lesson_id = resolved["lesson_id"]

        pack_path = resolved.get("pack_path") or None
        if quiz is None:
            quiz, teacher_assigned = load_student_quiz(lesson_dir, pack_path)
        elif teacher_assigned is None:
            teacher_assigned = False
        if quiz is None:
            student_info("No practice quiz is installed yet.")
            input("\n  Press Enter...")
            return

        store = QuizResultsStore()
        try:
            fingerprint = quiz_fingerprint(quiz)
            stats = store.get_stats(quiz.quiz_id, fingerprint)
            attempts = store.get_attempts(quiz.quiz_id, fingerprint)
            draft = store.get_draft(quiz.quiz_id, fingerprint)
        finally:
            store.close()

        label = "Teacher quiz" if teacher_assigned else "Practice quiz"
        # Completed-attempt count drives the deterministic shuffle seed. A
        # resumed draft keeps its original attempt number and question order.
        attempt_no = stats["attempts"] + 1

        while True:
            resumed = False
            missed_ids: list[str] = []
            if isinstance(draft, QuizDraft):
                by_id = {question.id: question for question in quiz.questions}
                draft_ids = draft.question_order
                if len(draft_ids) == len(quiz.questions) and set(draft_ids) == set(by_id):
                    session = QuizSession([by_id[question_id] for question_id in draft_ids])
                    session.position = min(draft.position, len(session.questions) - 1)
                    session.answers = {
                        question_id: answer
                        for question_id, answer in draft.answers.items()
                        if question_id in by_id
                    }
                    attempt_no = draft.attempt_number
                    resumed = True
                else:
                    logger.info("Ignoring invalid quiz draft for %s", quiz.quiz_id)
                    draft = None
            if not resumed:
                missed_ids = latest_missed_question_ids(quiz, attempts)
                session = QuizSession(
                    ordered_questions(
                        quiz,
                        attempt_number=attempt_no,
                        missed_ids=missed_ids,
                    )
                )
                deadline_at = ""
            else:
                deadline_at = draft.deadline_at

            clear()
            student_heading(f"{label}: {quiz.title}")
            navigation_hint("Student", f"Quiz / {quiz.title}")
            print(f"  {DIM}Attempt {attempt_no}{RESET}")
            if resumed:
                print(
                    f"  {GREEN}Resuming your saved quiz{RESET} · "
                    f"{session.answered_count}/{len(session.questions)} answered"
                )
            if quiz.description:
                print(f"  {DIM}{quiz.description}{RESET}")
            if quiz.time_limit_seconds:
                remaining = quiz_seconds_remaining(deadline_at)
                if remaining is None:
                    print(
                        f"  {YELLOW}Time limit:{RESET} "
                        f"{self._quiz_duration_label(quiz.time_limit_seconds)}"
                    )
                else:
                    timer_color = RED if remaining <= 30 else YELLOW
                    print(
                        f"  {timer_color}Time remaining:{RESET} "
                        f"{remaining // 60:02d}:{remaining % 60:02d}"
                    )
            if missed_ids:
                print(
                    f"  {YELLOW}Review focus:{RESET} {len(missed_ids)} question(s) "
                    "missed on your last attempt"
                )
            if stats["attempts"]:
                print(
                    f"  {DIM}Previous best: {stats['best']}/{len(quiz.questions)} correct "
                    f"over {stats['attempts']} attempt(s){RESET}"
                )
            print(
                f"  {DIM}{len(quiz.questions)} question(s) • [b] back "
                f"• [m] question map • [q] pause • [?] help{RESET}"
            )
            first_prompt = (
                "  Press Enter to continue..."
                if resumed
                else "  Press Enter to begin..."
            )
            print(first_prompt)
            action = student_prompt("Start", "").strip().lower()
            if action in ("q", "0"):
                return
            if action in ("h", "history"):
                self._quiz_history_flow(
                    lesson={
                        "workspace_dir": lesson_dir,
                        "lesson_id": lesson_id,
                        "title": resolved.get("title", quiz.title),
                        "pack_file": resolved.get("pack_path"),
                    },
                    quiz=quiz,
                    teacher_assigned=teacher_assigned,
                )
                continue
            if action not in ("", "start", "resume", "s"):
                student_error("Press Enter to start, h for history, or q to go back.")
                continue
            if not resumed:
                deadline_at = quiz_deadline_at(quiz)
                self._quiz_save_draft(
                    quiz, lesson_id, fingerprint, attempt_no, session, deadline_at
                )

            timed_out = False
            while not session.complete:
                if deadline_at and quiz_seconds_remaining(deadline_at) == 0:
                    timed_out = True
                    break
                q = session.current
                i = session.position + 1
                clear()
                meta_bits = []
                if q.concept:
                    meta_bits.append(q.concept)
                if q.difficulty is not None:
                    meta_bits.append(f"difficulty {q.difficulty}")
                meta = f" • {DIM}{' • '.join(meta_bits)}{RESET}" if meta_bits else ""
                print(f"  {BOLD}Question {i} of {len(session.questions)}{RESET}{meta}")
                print(
                    f"  {progress_bar(session.answered_count, len(session.questions))}"
                    f"{DIM}  {session.answered_count} answered{RESET}"
                )
                if deadline_at:
                    remaining = quiz_seconds_remaining(deadline_at)
                    timer_color = RED if remaining is not None and remaining <= 30 else YELLOW
                    print(
                        f"  {timer_color}Time left: "
                        f"{remaining // 60:02d}:{remaining % 60:02d}{RESET}"
                    )
                print()
                print(f"  {q.prompt}")
                print()
                ans = self._quiz_prompt(q)
                if ans is None:
                    self._quiz_save_draft(
                        quiz, lesson_id, fingerprint, attempt_no, session, deadline_at
                    )
                    student_info("Quiz paused. Your progress was saved on this computer.")
                    input("  Press Enter...")
                    return
                if ans == "__back__":
                    if not session.back():
                        student_info("You are at the first question.")
                        input("  Press Enter...")
                    self._quiz_save_draft(
                        quiz, lesson_id, fingerprint, attempt_no, session, deadline_at
                    )
                    continue
                if ans == "__help__":
                    student_info(
                        "Answer the question, b goes back, m opens the question map, "
                        "and q pauses with your progress saved."
                    )
                    input("  Press Enter...")
                    continue
                if ans == "__map__":
                    self._quiz_question_map(session)
                    self._quiz_save_draft(
                        quiz, lesson_id, fingerprint, attempt_no, session, deadline_at
                    )
                    continue
                if deadline_at and quiz_seconds_remaining(deadline_at) == 0:
                    timed_out = True
                    break
                session.answer(ans)
                self._quiz_save_draft(
                    quiz, lesson_id, fingerprint, attempt_no, session, deadline_at
                )

            answers_for_grading = session.answers
            if timed_out:
                answers_for_grading = dict(session.answers)
                for question in session.questions:
                    answers_for_grading.setdefault(question.id, "__DUCKY_TIMEOUT__")
            result = grade_quiz(quiz, answers_for_grading, questions=session.questions)

            if timed_out:
                student_warning("Time is up. Unanswered questions were graded incorrect.")

            store = QuizResultsStore()
            try:
                store.record(
                    quiz.quiz_id,
                    lesson_id,
                    result,
                    answers_for_grading,
                    quiz_fingerprint=fingerprint,
                )
                store.clear_draft(quiz.quiz_id, fingerprint)
            finally:
                store.close()
            draft = None

            # Best-effort share with teacher if connected (never blocks the quiz).
            submission_status = self._maybe_submit_quiz_result(quiz.quiz_id, lesson_id, result)
            if submission_status.get("status") == "submitted":
                student_success("Quiz result sent to your teacher.")
            elif submission_status.get("status") == "queued":
                student_warning("Teacher unavailable. Quiz result saved and queued for Sync.")

            if self._quiz_show_results(quiz, label, result, stats):
                return
            # Retry: refresh best score for the next attempt's header.
            store = QuizResultsStore()
            try:
                stats = store.get_stats(quiz.quiz_id, fingerprint)
                attempts = store.get_attempts(quiz.quiz_id, fingerprint)
            finally:
                store.close()
            attempt_no = max(attempt_no + 1, stats["attempts"] + 1)

    def _maybe_submit_quiz_result(self, quiz_id: str, lesson_id: str, result) -> dict:
        """Best-effort forward of a graded quiz result to the teacher (shared helper)."""
        from ..workflows import maybe_submit_quiz_result as _submit

        return _submit(quiz_id, lesson_id, result)

    def _hint_chat_flow(self):
        """Interactive chat with the LLM tutor — ask questions, get hints, re-ask."""
        from ..llm_client import HintRequest
        from ..reports import ReportStore
        from ..workflows import resolve_current_lesson

        resolved = resolve_current_lesson()
        if resolved is None:
            input("  Press Enter...")
            return

        lesson_id = resolved["lesson_id"]
        title = resolved["title"]
        lesson_dir = resolved["lesson_dir"]
        entry_path = resolved["entry_path"]

        # Gather full context for the LLM
        student_code = entry_path.read_text(encoding="utf-8") if entry_path.is_file() else ""
        instructions = ""
        from ..workspace import get_lesson_instructions
        instructions = get_lesson_instructions(lesson_id) or ""
        test_cases = ""
        test_dir = lesson_dir / "tests"
        if test_dir.is_dir():
            for tf in sorted(test_dir.glob("*.py")):
                test_cases += f"\n# {tf.name}\n{tf.read_text(encoding='utf-8')}"

        # Check for last report (for failure context)
        last_diagnosis = ""
        last_output = ""
        last_failure_name = ""
        try:
            store = ReportStore()
            reports = sorted(store.paths["reports"].glob("*.json"), reverse=True)
            if reports:
                data = json.loads(reports[0].read_text(encoding="utf-8"))
                failures = data.get("failures", [])
                if failures:
                    last_failure_name = failures[0].get("name", "")
                    last_diagnosis = failures[0].get("diagnosis", "")
                    # Reports persist via to_dict(include_raw=False): the redacted
                    # ``stdout`` field is written, but every ``raw_*`` key
                    # (raw_stdout/raw_stderr/raw_code) is dropped per the privacy
                    # contract, and there is no top-level ``raw_output`` key at
                    # all. Reading ``raw_output`` here always returned '' and
                    # silently starved the LLM of failure context — use stdout.
                    last_output = data.get("stdout", "")
        except Exception:
            pass

        tutor = self._build_student_tutor_engine()

        clear()
        student_heading(f"Tutor Chat — {title}")

        if instructions:
            print(f"  {DIM}📋 Lesson instructions loaded{RESET}")
        if test_cases:
            print(f"  {DIM}🧪 Test cases loaded — the tutor knows the expected behavior{RESET}")
        if student_code:
            print(f"  {DIM}📝 Your current code loaded{RESET}")
        if last_diagnosis:
            print(f"  {DIM}🔍 Last failure: {last_failure_name} ({last_diagnosis}){RESET}")
        if not tutor:
            student_warning("No LLM configured. Use Settings → Setup LLM to enable AI hints.")
            input("\n  Press Enter...")
            return

        while True:
            print()
            print(f"  {DIM}Ask anything about this assignment. Press Enter for a general hint.{RESET}")
            question = input(f"  {STUDENT_PROMPT}Your question{RESET}: ").strip()

            print(f"  {DIM}Thinking...{RESET}")

            hint_request = HintRequest(
                lesson_id=lesson_id,
                title=title,
                code_snippet=student_code[:2000],
                diagnosis=last_diagnosis,
                failure_name=last_failure_name,
                expected=None,
                actual=None,
                stdout="",
                error="",
                raw_output=last_output[:800] if last_output else "",
                concept_tags=[],
                instructions=instructions[:2000] if instructions else "",
                test_cases=test_cases[:2000] if test_cases else "",
                student_code=student_code[:2000],
                attempt_number=1,
                function_mismatch=None,
            )

            if question:
                hint_request.raw_output = f"Student question: {question}"

            hint = tutor.build_hint(hint_request, attempt_number=1)

            print(f"\n  {BOLD}🤖 Ducky:{RESET}")
            print(f"  {hint}")

            print()
            student_separator()
            print(f"  {STUDENT_ACCENT}[r]{RESET}  Ask another question")
            print(f"  {STUDENT_ACCENT}[q]{RESET}  Back")

            choice = input(f"  {STUDENT_PROMPT}Choose{RESET}: ").strip().lower()
            if choice == "" or choice == "r":
                continue
            elif choice == "q":
                break

    def _connect_to_teacher(self):
        """Shared connect flow: gather URL/code/name, connect via the persisting
        workflow, and report through student widgets.

        Both ``_connect_flow`` (Student Mode → [c]) and ``_classroom_connect``
        (Classroom → [1]) route here. ``connect_workflow`` persists
        ``teacher_url`` / ``teacher_token`` / ``student_name`` so downstream
        classroom flows (all built on ``StudentClient.from_config()``) observe
        the connection afterward — regardless of which menu the student used.
        """
        from ..student_client import StudentClientError
        from ..workflows import connect_workflow

        clear()
        student_heading("Connect to Teacher")

        config = self._load_config()
        saved_url = config.get("teacher_url", "")
        url = student_prompt("Teacher server URL (blank = find on LAN)", saved_url)

        code = ""
        while True:
            code = student_prompt("9-digit access code")
            if code and len(code) == 9 and code.isdigit():
                break
            student_error("Access code must be exactly 9 digits. Try again.")

        saved_name = config.get("student_name", "")
        name = ""
        while True:
            name = student_prompt("Your name", saved_name)
            if name:
                break
            student_error("Name is required. Try again.")

        try:
            result = connect_workflow(url, code, name)
        except StudentClientError as exc:
            student_error(f"Connection failed: {exc}")
            input("  Press Enter...")
            return None

        # A connection made from inside the same TUI session must trigger the
        # next home entry's assignment discovery; the previous offline check
        # must not suppress it.
        self._auto_pull_done = False
        self._student_connection_state = "connected"
        student_success(f"Connected to: {result.get('class_name', '?')}")
        input("  Press Enter...")
        return result

    def _connect_flow(self):
        self._connect_to_teacher()

    def _update_flow(self):
        from ..student_client import StudentClient, StudentClientError, update_packs_from_server

        client = StudentClient.from_config()
        if not client.teacher_url:
            print(f"\n  {DIM}Not connected. Connect first.{RESET}")
            input("  Press Enter...")
            return

        clear()
        student_heading("Pulling lessons from teacher...")

        try:
            packs = client.list_packs()
        except StudentClientError as exc:
            student_error(f"Failed: {exc}")
            input("  Press Enter...")
            return

        if not packs:
            print(f"  {DIM}No lessons available.{RESET}")
            input("  Press Enter...")
            return

        try:
            assignments = client.check_assignments()
        except StudentClientError:
            assignments = []
        if isinstance(assignments, list):
            self._student_set_assignment_catalog(assignments, packs)
        result = update_packs_from_server(client, packs, quiet=False)
        self._apply_teacher_pack_updates(result, packs)
        if result.get("downloaded"):
            student_success(f"Downloaded {result['downloaded']} lesson pack(s).")
        if result.get("workspace_updates"):
            student_success(f"Refreshed {result['workspace_updates']} existing assignment(s), keeping your edits.")
        if result.get("errors"):
            student_warning(f"Some updates could not be applied: {result['errors'][0]}")
        if not result.get("downloaded") and not result.get("workspace_updates") and not result.get("errors"):
            student_info("Everything is up to date.")

        input("\n  Press Enter...")

    # ─── Classroom (Student) ───────────────────────────────────

    def _classroom_menu(self):
        while True:
            clear()
            # Reload after every action so Connect/Disconnect immediately
            # updates the badge when the student returns to this menu.
            config = self._load_config()
            teacher_url = config.get("teacher_url", "")
            token = config.get("teacher_token", "")
            name = config.get("student_name", "")
            # Show queued notifications
            if self._notifications:
                show_notifications(self._notifications)
                self._notifications.clear()

            student_heading("Classroom — Student Connection")

            try:
                from ..outbox import DurableOutbox

                queued_count = DurableOutbox().count()
            except Exception:
                queued_count = 0

            if teacher_url and token:
                connection_lines = [
                    f"{GREEN}● Connected to:{RESET} {teacher_url}",
                    f"{GREEN}Student:{RESET} {name}",
                ]
                # Add sync status if available
                if self._background_sync and self._background_sync.is_running:
                    sync_status = self._background_sync.get_status()
                    if sync_status and sync_status.get("last_error"):
                        connection_lines.append(
                            f"{YELLOW}! Sync error:{RESET} {sync_status['last_error']}"
                        )
                    elif sync_status and sync_status.get("last_success"):
                        connection_lines.append(
                            f"{STUDENT_ACCENT}Last sync:{RESET} {sync_status['last_success'][:19]}"
                        )
                    else:
                        connection_lines.append(
                            f"{STUDENT_ACCENT}Sync:{RESET} active"
                        )
                card("Connection", connection_lines)
            else:
                print(f"  {DIM}Not connected to a teacher server{RESET}")
            if queued_count:
                student_warning(f"{queued_count} result(s) queued locally — choose Sync to retry.")

            print()
            print(f"  {STUDENT_ACCENT}[1]{RESET}  Connect to teacher")
            print(f"  {STUDENT_ACCENT}[2]{RESET}  Pull updates (new packs)")
            print(f"  {STUDENT_ACCENT}[3]{RESET}  Submit work")
            print(f"  {STUDENT_ACCENT}[4]{RESET}  View assignments")
            print(f"  {STUDENT_ACCENT}[5]{RESET}  View announcements")
            print(f"  {STUDENT_ACCENT}[6]{RESET}  View connection status")
            print(f"  {STUDENT_ACCENT}[7]{RESET}  Retry queued results")
            print(f"  {STUDENT_ACCENT}[8]{RESET}  View teacher feedback")
            print(f"  {STUDENT_ACCENT}[0]{RESET}  Back")
            print()
            choice = student_prompt("Choose", "0")
            if choice == "1":
                self._classroom_connect()
            elif choice == "2":
                self._classroom_update()
            elif choice == "3":
                self._classroom_submit()
            elif choice == "4":
                self._classroom_assignments()
            elif choice == "5":
                self._classroom_announcements()
            elif choice == "6":
                self._classroom_status()
            elif choice == "7":
                self._classroom_retry_queue()
            elif choice == "8":
                self._classroom_feedback()
            elif choice == "0":
                break

    def _classroom_connect(self):
        self._connect_to_teacher()

    def _classroom_retry_queue(self):
        from ..student_client import StudentClient, StudentClientError

        student_heading("Sync queued results")
        try:
            result = StudentClient.from_config().flush_queue()
        except StudentClientError as exc:
            student_error(f"Still offline: {exc}")
        else:
            if result.get("sent"):
                student_success(f"Sent {result['sent']} queued result(s).")
            if result.get("remaining"):
                student_warning(f"{result['remaining']} result(s) remain queued.")
            if not result.get("sent") and not result.get("remaining"):
                student_info("Nothing is waiting to sync.")
        input("\n  Press Enter...")

    def _classroom_update(self):
        from ..student_client import StudentClient, StudentClientError, update_packs_from_server

        student_heading("Sync with Teacher")
        client = StudentClient.from_config()
        if not client.teacher_url or not client.token:
            student_error("Not connected. Connect first.")
            input("  Press Enter...")
            return

        # From the student's point of view, Sync is one action: retry work
        # saved while offline, then pull any lesson updates.
        try:
            queue_result = client.flush_queue()
        except StudentClientError as exc:
            queue_result = {"sent": 0, "remaining": 0, "errors": [str(exc)]}
        if queue_result.get("sent"):
            student_success(f"Sent {queue_result['sent']} queued result(s).")
        if queue_result.get("remaining"):
            student_warning(f"{queue_result['remaining']} result(s) remain queued.")

        try:
            packs = client.list_packs()
        except StudentClientError as exc:
            student_error(f"Failed: {exc}")
            input("  Press Enter...")
            return

        try:
            assignments = client.check_assignments()
        except StudentClientError as exc:
            assignments = []
            student_warning(f"Could not refresh assignment list: {exc}")
        if isinstance(assignments, list):
            self._student_set_assignment_catalog(assignments, packs)

        if not packs:
            if assignments:
                student_warning(
                    f"Teacher lists {len(assignments)} assignment(s), but their lesson packs are not available yet."
                )
            else:
                student_info("No lesson packs available.")
            input("  Press Enter...")
            return

        from ..workspace import get_current_lesson

        current = get_current_lesson()
        versions = self._load_config().get("pack_versions", {})
        if current and current.get("status") == "in_progress":
            current_id = current.get("lesson_id", "")
            for pack in packs:
                pack_id = pack.get("pack_id", "")
                try:
                    server_version = int(pack.get("version", 0) or 0)
                    local_version = int(versions.get(pack_id, 0) or 0)
                except (TypeError, ValueError):
                    continue
                if pack.get("lesson_id") == current_id and server_version > local_version:
                    if not student_confirm(
                        f"Teacher has an updated '{pack.get('title', current_id)}'. Download it while keeping your edits?"
                    ):
                        packs = [candidate for candidate in packs if candidate.get("pack_id") != pack_id]

        result = update_packs_from_server(client, packs, quiet=False)
        self._apply_teacher_pack_updates(result, packs)
        if result["downloaded"]:
            student_success(f"Downloaded {result['downloaded']} lesson pack(s).")
        if result.get("workspace_updates"):
            student_success(f"Refreshed {result['workspace_updates']} existing assignment(s).")
        elif not queue_result.get("sent"):
            student_info("Everything is up to date.")
        if queue_result.get("errors"):
            student_warning(f"Queued work could not sync: {queue_result['errors'][0]}")
        input("  Press Enter...")

    def _classroom_submit(self):
        from ..student_client import StudentClient, StudentClientError
        from ..workspace import get_current_lesson

        student_heading("Submit Work")
        client = StudentClient.from_config()
        if not client.teacher_url or not client.token:
            student_error("Not connected. Connect first.")
            input("  Press Enter...")
            return

        try:
            packs = client.list_packs()
        except StudentClientError:
            packs = []

        current = get_current_lesson()
        current_id = str(current.get("lesson_id") or "") if current else ""
        matching = next(
            (
                p for p in packs
                if current_id
                and current_id in {
                    str(p.get("lesson_id") or ""),
                    str(p.get("pack_id") or ""),
                }
            ),
            None,
        )
        if matching:
            pack_id = matching.get("pack_id") or matching.get("lesson_id", "")
            print(f"  {STUDENT_ACCENT}Lesson:{RESET} {matching.get('title', pack_id)}")
        elif len(packs) == 1:
            pack_id = packs[0].get("pack_id") or packs[0].get("lesson_id", "")
            print(f"  {STUDENT_ACCENT}Lesson:{RESET} {packs[0].get('title', pack_id)}")
        elif packs:
            print(f"\n  {BOLD}Choose a lesson:{RESET}")
            for i, pack in enumerate(packs, 1):
                print(f"  {STUDENT_ACCENT}[{i}]{RESET}  {pack.get('title', '?')}")
            choice = student_prompt("Lesson number", "1")
            try:
                selected_pack = packs[int(choice) - 1]
                pack_id = selected_pack.get("pack_id") or selected_pack.get("lesson_id", "")
            except (ValueError, IndexError):
                pack_id = ""
                selected_pack = None
        else:
            pack_id = ""
            selected_pack = None

        if matching:
            selected_pack = matching
        elif len(packs) == 1 and packs:
            selected_pack = packs[0]

        target_ids = {
            str(value)
            for value in (
                (selected_pack or {}).get("pack_id"),
                (selected_pack or {}).get("lesson_id"),
                current_id if matching else "",
            )
            if value
        }

        if not pack_id:
            student_error("Choose a lesson before submitting.")
            input("  Press Enter...")
            return

        def report_matches_target(data: dict) -> bool:
            report_lesson_id = str(data.get("lesson_id") or "")
            if report_lesson_id:
                return report_lesson_id in target_ids
            # Legacy reports without lesson IDs are safe to auto-select only
            # when the student has exactly one teacher pack.
            return len(packs) == 1

        report_data, _ = self._student_recent_report()
        if report_data:
            if not report_matches_target(report_data):
                report_data = None
            else:
                summary = self._student_report_summary(report_data)
                if summary:
                    student_info(f"Using latest local result: {summary}")

        if not report_data:
            from ..reports import ReportStore

            store = ReportStore()
            reports_dir = store.paths["reports"]
            recent = sorted(
                reports_dir.glob("*.json"),
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )[:10]
            matching_recent = []
            for report_path in recent:
                try:
                    data = json.loads(report_path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if report_matches_target(data):
                    matching_recent.append(data)
            if matching_recent:
                print(f"\n  {BOLD}Recent results for this lesson:{RESET}")
                for i, data in enumerate(matching_recent, 1):
                    st = badge("PASS", "pass") if data.get("passed") else badge("NEEDS WORK", "fail")
                    print(
                        f"  {STUDENT_ACCENT}[{i}]{RESET}  {st} "
                        f"{self._student_report_summary(data) or 'saved result'}"
                    )
                print()
                rchoice = student_prompt("Result number (0 = cancel)", "0")
                if rchoice != "0":
                    try:
                        ridx = int(rchoice) - 1
                        if 0 <= ridx < len(matching_recent):
                            report_data = matching_recent[ridx]
                    except (ValueError, IndexError):
                        pass
            if not report_data:
                student_info("No saved report selected. Run tests from the home screen first.")

        if not report_data:
            student_error("No result to submit. Run tests first, then choose Submit result.")
            input("  Press Enter...")
            return

        student_code = ""
        from ..workspace import get_current_lesson

        current = get_current_lesson()
        if current and current_id in target_ids:
            entry_path = current["lesson_dir"] / current.get("entry_file", "")
            if entry_path.is_file():
                student_info("Code is optional and is only sent after your confirmation.")
                if student_confirm(f"Include code from {entry_path.name}?"):
                    try:
                        student_code = entry_path.read_text(encoding="utf-8")
                    except (OSError, UnicodeError) as exc:
                        student_warning(f"Could not read your code; submitting the result without code: {exc}")
        try:
            result = client.submit(pack_id, report_data, student_code, queue_on_failure=True)
        except StudentClientError as exc:
            student_error(f"Submission failed: {exc}")
            input("  Press Enter...")
            return

        if result.get("queued"):
            student_warning("Teacher unavailable. Result queued locally and will retry automatically.")
            student_info("You can see the queued result from Classroom → Sync.")
        else:
            student_success("Result submitted to your teacher.")
        input("  Press Enter...")

    def _classroom_assignments(self):
        from ..paths import app_dir
        from ..student_client import StudentClient, StudentClientError, update_packs_from_server

        student_heading("Assignments")
        client = StudentClient.from_config()
        if not client.teacher_url:
            student_error("Not connected. Connect first.")
            input("  Press Enter...")
            return

        try:
            assignments = client.check_assignments()
        except StudentClientError as exc:
            student_error(f"Failed: {exc}")
            input("  Press Enter...")
            return

        try:
            packs = client.list_packs()
        except StudentClientError:
            packs = []

        if isinstance(assignments, list) and isinstance(packs, list):
            self._student_set_assignment_catalog(assignments, packs)
            # Viewing assignments is also a safe discovery action. Pulling
            # here prevents the common mismatch where the teacher has assigned
            # a lesson but the student only sees WAITING FOR LESSON until they
            # visit a different Sync screen.
            try:
                sync_result = update_packs_from_server(client, packs, quiet=True)
                self._apply_teacher_pack_updates(sync_result, packs, quiet=True)
            except Exception as exc:
                student_warning(f"Could not download the latest lesson packs: {exc}")

        local_packs_dir = app_dir() / "packs"
        available_packs: set[str] = set()
        for pack in packs:
            if not isinstance(pack, dict):
                continue
            identifiers = {
                str(pack.get("pack_id") or ""),
                str(pack.get("lesson_id") or ""),
            }
            identifiers.discard("")
            if any((local_packs_dir / f"{identifier}.duckypack").is_file() for identifier in identifiers):
                available_packs.update(identifiers)
        if not packs:
            available_packs = set()

        if not assignments:
            student_info("No assignments.")
        else:
            for a in assignments:
                due = a.get("due_date") or "none"
                title = a.get("pack_title") or "Lesson assignment"
                assignment_ids = {
                    str(a.get("pack_id") or ""),
                    str(a.get("lesson_id") or ""),
                }
                assignment_ids.discard("")
                ready = bool(assignment_ids & available_packs)
                state = f"{GREEN}READY{RESET}" if ready else f"{YELLOW}WAITING FOR LESSON{RESET}"
                print(f"  {STUDENT_ACCENT}{title}{RESET} — {state} · due: {due}")
                if a.get("notes"):
                    print(f"    {DIM}{a['notes']}{RESET}")

        if assignments and any(
            not ({str(a.get("pack_id") or ""), str(a.get("lesson_id") or "")} & available_packs)
            for a in assignments
        ):
            student_info("Some assignments are listed by the teacher but their lesson pack is not available yet.")
        elif assignments:
            student_info("All listed assignments are ready. Start them from Student Mode.")
        input("\n  Press Enter...")

    def _classroom_feedback(self):
        from ..student_client import StudentClient, StudentClientError

        student_heading("Teacher feedback")
        try:
            submissions = StudentClient.from_config().feedback()
        except StudentClientError as exc:
            student_error(f"Could not load feedback: {exc}")
            input("\n  Press Enter...")
            return
        if not submissions:
            student_info("No teacher feedback yet.")
        for submission in submissions:
            try:
                report = json.loads(submission.get("report_json") or "{}")
            except (TypeError, ValueError):
                report = {}
            title = (
                report.get("lesson_title")
                or submission.get("pack_title")
                or "Lesson result"
            )
            print(
                f"  {STUDENT_ACCENT}{title}{RESET} "
                f"({submission.get('kind', 'result')}) "
                f"[{submission.get('status', 'submitted')}] {submission.get('submitted_at', '')[:16]}"
            )
            if submission.get("teacher_feedback"):
                print(f"    {submission['teacher_feedback']}")
        input("\n  Press Enter...")

    def _classroom_announcements(self):
        from ..student_client import StudentClient, StudentClientError

        student_heading("Announcements")
        client = StudentClient.from_config()
        if not client.teacher_url:
            student_error("Not connected. Connect first.")
            input("  Press Enter...")
            return

        try:
            announcements = client.check_announcements()
        except StudentClientError as exc:
            student_error(f"Failed: {exc}")
            input("  Press Enter...")
            return

        if not announcements:
            student_info("No announcements.")
        else:
            for a in announcements:
                print(f"  {YELLOW}{a.get('created_at', '?')[:16]}{RESET}")
                print(f"    {a.get('message', '?')}")
                print()
                if a.get("announcement_id"):
                    try:
                        client.mark_announcement_seen(a["announcement_id"])
                    except StudentClientError:
                        student_info("Could not acknowledge this announcement; it will remain pending.")

        input("  Press Enter...")

    def _classroom_status(self):
        from ..student_client import StudentClient, StudentClientError

        student_heading("Connection Status")
        client = StudentClient.from_config()
        if not client.teacher_url:
            student_info("Not connected to a teacher server.")
            input("  Press Enter...")
            return

        try:
            st = client.status()
        except StudentClientError as exc:
            student_error(f"Server unreachable: {exc}")
            input("  Press Enter...")
            return

        card("Connection Status", [
            f"{STUDENT_ACCENT}Teacher server:{RESET} {client.teacher_url}",
            f"{STUDENT_ACCENT}Connection:{RESET} connected",
            f"{STUDENT_ACCENT}Lessons available:{RESET} {st.get('pack_count', '?')}",
            f"{STUDENT_ACCENT}Assignments:{RESET} {st.get('assignment_count', '?')}",
            f"{STUDENT_ACCENT}Announcements:{RESET} {st.get('announcement_count', '?')}",
        ])
        input("\n  Press Enter...")
