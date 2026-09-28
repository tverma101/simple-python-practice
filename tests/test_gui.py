import io
import re
import unittest
from contextlib import redirect_stdout
from unittest.mock import MagicMock, patch


class TestGuiImport(unittest.TestCase):
    def test_gui_module_has_main(self):
        from ducky import gui

        self.assertTrue(hasattr(gui, "main"))
        self.assertTrue(callable(gui.main))

    def test_student_entrypoint_dispatches_to_windowed_app(self):
        from ducky.gui import app

        with (
            patch.object(app, "__build_type__", "student"),
            patch("ducky.self_update.startup_student_update", return_value={"check": {}}),
            patch("ducky.gui.student_desktop.main") as desktop_main,
        ):
            app.main()

        desktop_main.assert_called_once_with(startup_update={"check": {}})

    def test_gui_module_has_terminal_class(self):
        from ducky.gui import DuckyTerminal

        self.assertTrue(DuckyTerminal)

    def test_gui_module_has_helper_functions(self):
        from ducky.gui import banner, confirm, error, heading, info, prompt, separator, success

        self.assertTrue(callable(banner))
        self.assertTrue(callable(separator))
        self.assertTrue(callable(prompt))
        self.assertTrue(callable(confirm))
        self.assertTrue(callable(success))
        self.assertTrue(callable(error))
        self.assertTrue(callable(info))
        self.assertTrue(callable(heading))

    def test_student_startup_checks_cloudflare_before_opening_the_tui(self):
        from ducky.gui.app import DuckyTerminal

        terminal = object.__new__(DuckyTerminal)
        terminal._notifications = []
        update = {
            "update_available": True,
            "current_version": "0.5.2s",
            "latest_version": "0.5.2",
            "download_url": "https://pub.example/releases/v0.5.2-build/student.zip",
            "sha256": "a" * 64,
            "error": None,
        }
        with (
            patch("ducky.gui.app.__build_type__", "student"),
            patch("ducky.gui.app.DuckyTerminal._load_config", return_value={"auto_update_app": True}),
            patch("ducky.self_update.apply_pending_update", return_value=None),
            patch("ducky.self_update.check_for_update", return_value=update) as check,
            patch("ducky.self_update.queue_pending_update") as queue,
        ):
            terminal._check_auto_update()

        check.assert_called_once_with()
        queue.assert_called_once_with(update)
        self.assertTrue(any("Student update" in item["message"] for item in terminal._notifications))

    def test_student_report_renders_human_assignment_feedback(self):
        from ducky.display import render_student_report
        from ducky.runner import TestFailure, TestRunReport

        report = TestRunReport(
            lesson_id="lesson_01",
            title="Grade Calculator",
            passed=False,
            returncode=1,
            duration_seconds=0.1,
            tests_total=2,
            tests_passed=1,
            tests_failed=1,
            failures=[
                TestFailure(
                    name="is_valid_grade: grade(100) valid",
                    diagnosis="boolean_logic",
                    expected=True,
                    actual=False,
                    stdout="debug: checking grade\n",
                    error="Traceback (most recent call last):\n  File \"test.py\", line 4\nAssertionError: False",
                    hint="Check the boundary condition for 0 and 100.",
                )
            ],
        )

        rendered = render_student_report(report)
        self.assertIn("What happened:", rendered)
        self.assertIn("Expected:", rendered)
        self.assertIn("Your result:", rendered)
        self.assertIn("Printed output:", rendered)
        self.assertIn("Try this:", rendered)
        self.assertNotIn("Traceback (most recent call last):", rendered)


class TestStudentWidgets(unittest.TestCase):
    def test_student_banner_is_callable(self):
        from ducky.gui import student_banner

        self.assertTrue(callable(student_banner))

    def test_student_heading_is_callable(self):
        from ducky.gui import student_heading

        self.assertTrue(callable(student_heading))

    def test_student_separator_is_callable(self):
        from ducky.gui import student_separator

        self.assertTrue(callable(student_separator))

    def test_student_prompt_is_callable(self):
        from ducky.gui import student_prompt

        self.assertTrue(callable(student_prompt))

    def test_student_confirm_is_callable(self):
        from ducky.gui import student_confirm

        self.assertTrue(callable(student_confirm))

    def test_card_is_callable(self):
        from ducky.gui import card

        self.assertTrue(callable(card))

    def test_progress_bar_returns_string(self):
        from ducky.gui import progress_bar

        result = progress_bar(5, 10)
        self.assertIsInstance(result, str)
        self.assertIn("5/10", result)

    def test_progress_bar_empty(self):
        from ducky.gui import progress_bar

        result = progress_bar(0, 10)
        self.assertIn("0/10", result)

    def test_progress_bar_full(self):
        from ducky.gui import progress_bar

        result = progress_bar(10, 10)
        self.assertIn("10/10", result)

    def test_progress_bar_zero_total(self):
        from ducky.gui import progress_bar

        result = progress_bar(0, 0)
        self.assertIn("0/0", result)

    def test_badge_pass(self):
        from ducky.gui import badge

        result = badge("PASS", "pass")
        self.assertIn("PASS", result)

    def test_badge_fail(self):
        from ducky.gui import badge

        result = badge("FAIL", "fail")
        self.assertIn("FAIL", result)

    def test_badge_info(self):
        from ducky.gui import badge

        result = badge("INFO", "info")
        self.assertIn("INFO", result)

    def test_student_success_is_callable(self):
        from ducky.gui import student_success

        self.assertTrue(callable(student_success))

    def test_student_error_is_callable(self):
        from ducky.gui import student_error

        self.assertTrue(callable(student_error))

    def test_student_warning_is_callable(self):
        from ducky.gui import student_warning

        self.assertTrue(callable(student_warning))

    def test_student_info_is_callable(self):
        from ducky.gui import student_info

        self.assertTrue(callable(student_info))

    def test_all_student_exports_exist(self):
        from ducky.gui import (
            STUDENT_ACCENT,
            STUDENT_DIM,
            STUDENT_HEADING,
            STUDENT_PROMPT,
            badge,
            card,
            progress_bar,
            student_banner,
            student_confirm,
            student_error,
            student_heading,
            student_info,
            student_prompt,
            student_separator,
            student_success,
            student_warning,
        )

        self.assertTrue(
            all(
                [
                    student_banner,
                    student_heading,
                    student_separator,
                    student_prompt,
                    student_confirm,
                    card,
                    progress_bar,
                    badge,
                    student_success,
                    student_error,
                    student_warning,
                    student_info,
                    STUDENT_ACCENT,
                    STUDENT_DIM,
                    STUDENT_HEADING,
                    STUDENT_PROMPT,
                ]
            )
        )


class CardBorderAlignmentTests(unittest.TestCase):
    """Card boxes must have aligned borders; a short bottom border is a visible defect."""

    def _capture(self, fn):
        import io
        from contextlib import redirect_stdout

        buf = io.StringIO()
        with redirect_stdout(buf):
            fn()
        return buf.getvalue()

    def test_card_borders_align(self):
        from ducky.gui import card

        out = self._capture(lambda: card("Title", ["one", "two"]))
        rows = [r for r in out.splitlines() if r.strip()]
        # top, 2 content, bottom
        self.assertGreaterEqual(len(rows), 4)
        # Strip the 2-space left indent and ANSI color codes for width comparison.
        import re

        def visible(r):
            r = r[2:] if r.startswith("  ") else r
            return re.sub(r"\033\[[0-9;]*m", "", r)

        top = visible(rows[0])
        bottom = visible(rows[-1])
        self.assertEqual(top[0], "┌")
        self.assertEqual(bottom[0], "└")
        self.assertEqual(
            len(top),
            len(bottom),
            f"top/bottom borders misaligned: {len(top)} != {len(bottom)}",
        )
        # Side rows share the same width as the top border.
        for r in rows[1:-1]:
            self.assertEqual(
                len(visible(r)),
                len(top),
                f"content border misaligned: {len(visible(r))} != {len(top)}",
            )

    def test_teacher_card_borders_align(self):
        from ducky.gui.widgets import teacher_card

        out = self._capture(lambda: teacher_card("Title", ["one"]))
        rows = [r for r in out.splitlines() if r.strip()]
        import re

        def visible(r):
            r = r[2:] if r.startswith("  ") else r
            return re.sub(r"\033\[[0-9;]*m", "", r)

        top = visible(rows[0])
        bottom = visible(rows[-1])
        self.assertEqual(len(top), len(bottom))

    def test_cards_wrap_long_paths_for_narrow_terminals(self):
        from ducky.gui.widgets import card, teacher_card

        with patch("ducky.gui.widgets.terminal_width", return_value=28):
            student_output = self._capture(
                lambda: card("Status", ["/a/very/long/lesson/path/that/needs/wrapping"])
            )
            teacher_output = self._capture(
                lambda: teacher_card("Status", ["http://teacher.local/a/very/long/path"])
            )

        self.assertGreater(student_output.count("│"), 2)
        self.assertGreater(teacher_output.count("│"), 2)


class NotificationFormatTests(unittest.TestCase):
    """Notifications must be dicts compatible with show_notifications().

    A bare string notification would crash the TUI with AttributeError when
    show_notifications() calls n.get("level", ...) on it.
    """

    def _render(self, notifications):
        from ducky.gui.widgets import show_notifications

        buf = io.StringIO()
        with redirect_stdout(buf):
            show_notifications(notifications)
        return buf.getvalue()

    def test_announcement_callback_produces_renderable_notification(self):
        from ducky.gui.student import StudentMenuMixin

        mixin = StudentMenuMixin()
        mixin._notifications = []
        # Simulate a teacher announcement arriving via BackgroundSync.
        mixin._on_announcements([{"announcement_id": "a1", "message": "Hi class"}])
        self.assertEqual(len(mixin._notifications), 1)
        # It must be a dict, not a string — otherwise show_notifications crashes.
        self.assertIsInstance(mixin._notifications[0], dict)
        # And rendering it must not raise (the original bug raised AttributeError).
        out = self._render(mixin._notifications)
        self.assertIn("Announcement: Hi class", out)

    def test_announcement_callback_empty_is_noop(self):
        from ducky.gui.student import StudentMenuMixin

        mixin = StudentMenuMixin()
        mixin._notifications = []
        mixin._on_announcements([])
        self.assertEqual(mixin._notifications, [])


class SettingsMenuStudentBuildTests(unittest.TestCase):
    """Methods called from SettingsMenuMixin must exist on the student TUI.

    _browse_ollama_models / _browse_lmstudio_models are invoked from the
    (student-facing) Settings menu but were only defined on TeacherMenuMixin,
    which the student build strips — leaving dangling calls that raised
    AttributeError for students. They now live on SettingsMenuMixin.
    """

    def test_browse_methods_available_on_terminal(self):
        from ducky.gui import DuckyTerminal

        self.assertTrue(hasattr(DuckyTerminal, "_browse_ollama_models"))
        self.assertTrue(hasattr(DuckyTerminal, "_browse_lmstudio_models"))

    def test_browse_methods_not_only_on_teacher_mixin(self):
        # Regression guard: the definition must be on the student-facing mixin,
        # not exclusively on the teacher mixin that gets stripped from the build.
        from ducky.gui.settings import SettingsMenuMixin
        from ducky.gui.student import StudentMenuMixin

        self.assertTrue(hasattr(SettingsMenuMixin, "_browse_ollama_models"))
        self.assertTrue(hasattr(SettingsMenuMixin, "_browse_lmstudio_models"))
        # The student menu itself must not define its own (single source of truth).
        self.assertFalse(hasattr(StudentMenuMixin, "_browse_ollama_models"))


class PacksWorkspaceTeacherSeparationTests(unittest.TestCase):
    """Teacher pack-authoring + grading-workspace screens must live on the
    teacher mixin (stripped by build_student.sh), never on the student mixin.

    These ~330 lines historically lived in gui/student.py, so they shipped
    inside the student wheel even though no student menu reaches them. They
    must still resolve on DuckyTerminal via MRO for the full (teacher) build.
    """

    MOVED = (
        "_packs_menu",
        "_pack_create",
        "_pack_inspect",
        "_pack_list",
        "_pick_pack",
        "_workspace_menu",
        "_workspace_load_pack",
        "_workspace_load_submission",
        "_workspace_show_manifest",
        "_workspace_run_tests",
    )

    def test_methods_live_on_teacher_mixin(self):
        from ducky.gui.teacher import TeacherMenuMixin

        for m in self.MOVED:
            self.assertTrue(hasattr(TeacherMenuMixin, m), m)

    def test_methods_absent_from_student_mixin(self):
        from ducky.gui.student import StudentMenuMixin

        for m in self.MOVED:
            self.assertFalse(
                hasattr(StudentMenuMixin, m), f"{m} leaked into the student mixin"
            )

    def test_methods_resolve_on_full_terminal_via_mro(self):
        from ducky.gui import DuckyTerminal

        for m in self.MOVED:
            self.assertTrue(hasattr(DuckyTerminal, m), m)

    def test_workspace_run_tests_without_pack_errors_and_returns(self):
        from unittest.mock import patch

        from ducky.gui.teacher import TeacherMenuMixin

        mixin = TeacherMenuMixin()
        mixin.current_pack = None
        buf = io.StringIO()
        with patch("builtins.input", return_value=""), redirect_stdout(buf):
            mixin._workspace_run_tests()
        # Pack load guard fires before any test runner access; the early
        # return keeps this safe with no pack loaded.
        self.assertIn("No pack loaded", buf.getvalue())


class StudentHomeMenuTests(unittest.TestCase):
    """The student distribution must open on student workflows only."""

    def test_student_home_uses_student_branding_and_hides_platform_tools(self):
        from unittest.mock import patch

        from ducky.gui import DuckyTerminal

        app = object.__new__(DuckyTerminal)
        app._notifications = []

        with patch("ducky.gui.app.clear"), patch("ducky.gui.app.student_banner"), patch(
            "ducky.gui.app.student_separator"
        ), patch("ducky.gui.app.student_heading"), patch(
            "ducky.gui.app.student_prompt", return_value="0"
        ), patch(
            "ducky.workspace.list_available_lessons", return_value=[]
        ), patch(
            "ducky.workspace.get_progress", return_value={"completed": 0, "total": 0}
        ), patch("builtins.input", return_value=""):
            output = io.StringIO()
            with redirect_stdout(output):
                app._student_build_menu()

        rendered = output.getvalue()
        self.assertIn("My lessons", rendered)
        self.assertIn("My progress", rendered)
        self.assertNotIn("Teacher", rendered)
        self.assertNotIn("create / inspect .duckypack", rendered)
        self.assertNotIn("Dataset", rendered)

    def test_student_progress_menu_is_available_on_terminal(self):
        from ducky.gui import DuckyTerminal

        self.assertTrue(hasattr(DuckyTerminal, "_student_progress_menu"))


class StudentIndividualExperienceTests(unittest.TestCase):
    """Keep the student home actions aligned with the student's next step."""

    @staticmethod
    def _terminal():
        from ducky.gui import DuckyTerminal

        app = object.__new__(DuckyTerminal)
        app._notifications = []
        app.last_report = None
        return app

    def test_resume_opens_current_lesson_before_other_lessons(self):
        lessons = [
            {"lesson_id": "first", "title": "First", "status": "not_started"},
            {"lesson_id": "current", "title": "Current", "status": "in_progress"},
        ]
        app = self._terminal()
        with patch("ducky.workspace.list_available_lessons", return_value=lessons), patch(
            "ducky.workspace.get_current_lesson",
            return_value={"lesson_id": "current", "status": "in_progress"},
        ), patch.object(app, "_start_lesson_by_number") as start:
            app._student_resume_lesson()

        start.assert_called_once_with("2", lessons)

    def test_resume_skips_completed_current_lesson(self):
        lessons = [
            {"lesson_id": "done", "title": "Done", "status": "completed"},
            {"lesson_id": "next", "title": "Next", "status": "not_started"},
        ]
        app = self._terminal()
        with patch("ducky.workspace.list_available_lessons", return_value=lessons), patch(
            "ducky.workspace.get_current_lesson",
            return_value={"lesson_id": "done", "status": "completed"},
        ), patch.object(app, "_start_lesson_by_number") as start:
            app._student_resume_lesson()

        start.assert_called_once_with("2", lessons)

    def test_student_mode_entry_pulls_teacher_quiz_packs_without_background_sync(self):
        from ducky.gui.student import StudentMenuMixin

        client = MagicMock()
        client.teacher_url = "http://teacher.local:8420"
        client.token = "student-token"
        client.list_packs.return_value = [{"pack_id": "lesson", "version": 2}]
        expected = {"downloaded": 1, "updated": 0, "errors": []}
        with patch("ducky.student_client.StudentClient.from_config", return_value=client), patch(
            "ducky.student_client.update_packs_from_server", return_value=expected
        ) as update:
            result = StudentMenuMixin()._student_auto_pull_teacher_packs()

        self.assertEqual(result, expected)
        update.assert_called_once_with(client, client.list_packs.return_value, quiet=True)


    def test_latest_test_run_is_kept_for_submit(self):
        from ducky.gui.student import StudentMenuMixin

        report = MagicMock()
        report.passed = True
        report.to_dict.return_value = {"lesson_id": "lesson", "passed": True}
        app = self._terminal()
        resolved = {
            "lesson_id": "lesson",
            "title": "Lesson",
            "pack_path": "/tmp/lesson.duckypack",
            "entry_path": "/tmp/lesson/main.py",
        }
        with patch("ducky.workflows.resolve_current_lesson", return_value=resolved), patch(
            "ducky.workflows.run_tests_workflow", return_value=report
        ), patch("ducky.workflows.save_and_record"), patch(
            "ducky.display.render_student_report", return_value="student report"
        ), patch.object(StudentMenuMixin, "_build_student_tutor_engine", return_value=None), patch(
            "builtins.input", return_value="q"
        ):
            app._run_tests_flow()

        self.assertIs(app.last_report, report)
        self.assertEqual(app.current_pack, "/tmp/lesson.duckypack")
        self.assertEqual(app.current_submission, "/tmp/lesson/main.py")

    def test_sync_retries_queue_before_checking_for_packs(self):
        client = MagicMock()
        client.teacher_url = "http://teacher:8420"
        client.token = "student-token"
        client.flush_queue.return_value = {"sent": 1, "remaining": 0, "errors": []}
        client.list_packs.return_value = []
        app = self._terminal()
        with patch("ducky.student_client.StudentClient.from_config", return_value=client), patch(
            "builtins.input", return_value=""
        ), patch("ducky.gui.student.student_heading"), patch(
            "ducky.gui.student.student_success"
        ):
            app._classroom_update()

        client.flush_queue.assert_called_once_with()
        client.list_packs.assert_called_once_with()

    def test_report_summary_does_not_use_backend_identifiers(self):
        app = self._terminal()

        summary = app._student_report_summary(
            {"lesson_id": "lesson.internal", "title": "Loops", "passed": False, "tests_failed": 2}
        )

        self.assertEqual(summary, "Loops — 2 check(s) need another look")
        self.assertNotIn("lesson.internal", summary)

    def test_quiz_overview_surfaces_resume_state_without_backend_ids(self):
        from ducky.gui.student import StudentMenuMixin

        quiz = StudentQuizGuiFlowTests()._make_quiz()
        fake_store = MagicMock()
        fake_store.get_stats.return_value = {
            "attempts": 1,
            "best": 2,
            "last_correct": 2,
            "last_passed": False,
        }
        fake_store.get_draft.return_value = object()
        lessons = [
            {
                "lesson_id": "internal.lesson",
                "title": "Loops",
                "status": "in_progress",
                "has_quizzes": True,
                "pack_file": "/tmp/loops.duckypack",
            }
        ]
        with patch("ducky.quiz.QuizResultsStore", return_value=fake_store), patch(
            "ducky.quiz.load_quiz_from_pack", return_value=quiz
        ):
            rows = StudentMenuMixin()._student_quiz_overview(lessons)

        teacher_rows = [row for row in rows if row["teacher_assigned"]]
        self.assertEqual(len(teacher_rows), 1)
        self.assertEqual(teacher_rows[0]["status"], "resume")
        self.assertEqual(teacher_rows[0]["quiz"].title, "GUI Quiz")
        self.assertNotIn("internal.lesson", teacher_rows[0]["quiz"].title)

    def test_fresh_install_lists_bundled_practice_quizzes_without_lessons(self):
        from ducky.gui.student import StudentMenuMixin

        quiz = StudentQuizGuiFlowTests()._make_quiz()
        fake_store = MagicMock()
        fake_store.get_stats.return_value = {
            "attempts": 0,
            "best": 0,
            "last_correct": 0,
            "last_passed": False,
        }
        fake_store.get_draft.return_value = None
        with patch("ducky.quiz.QuizResultsStore", return_value=fake_store), patch(
            "ducky.quiz.load_general_quizzes", return_value=[quiz]
        ):
            rows = StudentMenuMixin()._student_quiz_overview([])

        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0]["teacher_assigned"])
        self.assertTrue(rows[0]["lesson"]["is_general_quiz"])
        self.assertIsNone(rows[0]["lesson"]["workspace_dir"])
        self.assertEqual(rows[0]["status"], "not started")

    def test_one_broken_quiz_does_not_hide_other_quizzes(self):
        from ducky.gui.student import StudentMenuMixin

        quiz = StudentQuizGuiFlowTests()._make_quiz()
        fake_store = MagicMock()
        fake_store.get_stats.return_value = {
            "attempts": 0,
            "best": 0,
            "last_correct": 0,
            "last_passed": False,
        }
        fake_store.get_draft.return_value = None
        lessons = [
            {"lesson_id": "broken", "has_quizzes": True, "pack_file": "/tmp/broken.duckypack"},
            {"lesson_id": "healthy", "has_quizzes": True, "pack_file": "/tmp/healthy.duckypack"},
        ]
        with patch("ducky.quiz.QuizResultsStore", return_value=fake_store), patch(
            "ducky.quiz.load_quiz_from_pack",
            side_effect=[ValueError("bad quiz"), quiz],
        ):
            rows = StudentMenuMixin()._student_quiz_overview(lessons)

        self.assertEqual(
            [row["lesson"]["lesson_id"] for row in rows if row["teacher_assigned"]],
            ["healthy"],
        )

    def test_student_assignment_labels_match_teacher_catalog(self):
        from ducky.gui.student import StudentMenuMixin

        app = StudentMenuMixin()
        app._student_set_assignment_catalog(
            [{"pack_id": "loops-pack", "lesson_id": "loops", "due_date": "2026-08-01"}],
            [{"pack_id": "loops-pack", "lesson_id": "loops", "title": "Loops"}],
        )
        lessons = app._student_annotate_assignments(
            [
                {
                    "lesson_id": "loops",
                    "title": "Loops",
                    "pack_file": "/tmp/loops-pack.duckypack",
                },
                {
                    "lesson_id": "practice",
                    "title": "Practice",
                    "pack_file": "/tmp/practice.duckypack",
                },
            ]
        )

        self.assertEqual(lessons[0]["assignment_state"], "assigned")
        self.assertEqual(lessons[0]["assignment"]["due_date"], "2026-08-01")
        self.assertEqual(lessons[1]["assignment_state"], "")


class CrossRoleTuiConsistencyTests(unittest.TestCase):
    """Teacher and student screens must describe the same local classroom."""

    def test_full_build_student_entry_uses_shared_student_home(self):
        from ducky.gui import DuckyTerminal

        app = object.__new__(DuckyTerminal)
        with patch.object(app, "_student_build_menu") as home, patch(
            "ducky.gui.app.banner"
        ), patch("ducky.gui.app.separator"), patch(
            "ducky.gui.app.prompt", side_effect=["1", "0"]
        ):
            app._notifications = []
            app._auto_update_notification = None
            app._main_menu()
        home.assert_called_once_with()

    def test_teacher_snapshot_uses_student_titles_and_result_state(self):
        import tempfile
        from pathlib import Path

        from ducky.classroom import ClassroomDB
        from ducky.gui.teacher import TeacherMenuMixin

        root = Path(tempfile.mkdtemp())
        db = ClassroomDB(root / "classroom.db")
        try:
            db.create_class("one", "One to one")
            student = db.connect_student("one", "Maya")
            db.add_pack("one", "loops", "Loops", b"pack")
            db.add_submission(
                student["token"], "one", "loops",
                '{"lesson_id":"loops","lesson_title":"Loops","passed":true}',
            )
            db.add_submission(
                student["token"], "one", "",
                '{"lesson_id":"loops","lesson_title":"Loops Quiz","passed":false}',
                kind="quiz",
            )
            snapshot = TeacherMenuMixin()._teacher_student_snapshot(db, "one")
            assert snapshot["student"]["name"] == "Maya"
            assert snapshot["current_lesson"] == "Loops Quiz"
            assert snapshot["coding_label"] == "Loops"
            assert snapshot["quiz_label"] == "Loops Quiz"
            assert TeacherMenuMixin()._teacher_result_label(snapshot["latest_coding"]) == "passed"
            assert TeacherMenuMixin()._teacher_result_label(snapshot["latest_quiz"]) == "needs work"
        finally:
            db.close()

    def test_teacher_publish_helper_creates_one_assignment_row(self):
        import tempfile
        from pathlib import Path

        from ducky.classroom import ClassroomDB
        from ducky.gui.teacher import TeacherMenuMixin

        db = ClassroomDB(Path(tempfile.mkdtemp()) / "classroom.db")
        try:
            db.create_class("one", "One to one")
            db.add_pack("one", "loops-pack", "Loops", b"pack")
            TeacherMenuMixin._teacher_ensure_assignment(db, "one", "loops-pack")
            TeacherMenuMixin._teacher_ensure_assignment(db, "one", "loops-pack")
            assignments = db.list_assignments("one")
            self.assertEqual(len(assignments), 1)
            self.assertEqual(assignments[0]["pack_id"], "loops-pack")
        finally:
            db.close()

    def test_student_feedback_uses_lesson_title_not_backend_pack_id(self):
        import tempfile
        from pathlib import Path

        from ducky.classroom import ClassroomDB

        db = ClassroomDB(Path(tempfile.mkdtemp()) / "classroom.db")
        try:
            db.create_class("one", "One to one")
            student = db.connect_student("one", "Maya")
            db.add_pack("one", "loops.internal", "Loops", b"pack")
            db.add_submission(
                student["token"], "one", "loops.internal",
                '{"lesson_title":"Loops","passed":true}',
                teacher_feedback="Nice work.",
            )
            row = db.list_student_submissions(student["token"], "one")[0]
            assert row["pack_title"] == "Loops"
            assert row["pack_lesson_id"] == "loops.internal"
            assert row["teacher_feedback"] == "Nice work."
        finally:
            db.close()


class TeacherGuiLauncherTests(unittest.TestCase):
    def test_direct_teacher_gui_launcher_enters_teacher_menu(self):
        from ducky.gui import teacher_gui

        app = MagicMock()
        with patch.object(teacher_gui, "DuckyTerminal", return_value=app):
            teacher_gui.main()
        app._teacher_menu.assert_called_once_with()


class HintChatContextStudentGuiTests(unittest.TestCase):
    """Regression: gui.student._hint_chat_flow must source last-failure context
    from the persisted (redacted) ``stdout`` field, NOT a ``raw_output`` key.

    ReportStore.save uses to_dict(include_raw=False): it writes ``stdout`` but
    drops every ``raw_*`` key (raw_stdout/raw_stderr/raw_code) and never writes
    a top-level ``raw_output`` at all. An earlier version read
    data.get("raw_output","") and so fed the LLM tutor an empty string for
    every failing run, silently dropping all failure context. This ports the
    guard that previously pinned the now-removed dead-module _hint_chat_flow
    over to the live StudentMenuMixin._hint_chat_flow.
    """

    def test_hint_chat_feeds_persisted_stdout_to_tutor(self):
        import json
        import os
        import tempfile
        from pathlib import Path
        from unittest.mock import MagicMock, patch

        tmp = tempfile.mkdtemp()
        prev_home = os.environ.get("DUCKY_HOME")
        os.environ["DUCKY_HOME"] = tmp
        try:
            # Real saved-report shape: NO raw_* / raw_output (privacy contract).
            reports_dir = Path(tmp) / "reports"
            reports_dir.mkdir(parents=True, exist_ok=True)
            (reports_dir / "run_1.json").write_text(
                json.dumps(
                    {
                        "lesson_id": "lesson1",
                        "title": "L1",
                        "passed": False,
                        "returncode": 1,
                        "tests_total": 1,
                        "tests_passed": 0,
                        "tests_failed": 1,
                        "stdout": "AssertionError: expected 1 got 2",
                        "stderr": "",
                        "failures": [
                            {
                                "name": "test_x",
                                "diagnosis": "incorrect_logic",
                                "error": "boom",
                                "stdout": "out",
                            }
                        ],
                    }
                )
            )

            lesson_dir = Path(tmp) / "lesson1"
            lesson_dir.mkdir()
            entry_path = lesson_dir / "x.py"
            entry_path.write_text("def x():\n    return 1\n")

            fake_engine = MagicMock()
            fake_engine.build_hint.return_value = "hint text"

            from ducky.gui.student import StudentMenuMixin

            mixin = StudentMenuMixin()

            with patch("ducky.gui.student.clear"), patch(
                "ducky.workflows.resolve_current_lesson",
                return_value={
                    "lesson_id": "lesson1",
                    "title": "L1",
                    "lesson_dir": lesson_dir,
                    "entry_path": entry_path,
                    "pack_path": None,
                },
            ), patch(
                "ducky.workspace.get_lesson_instructions", return_value=""
            ), patch.object(
                StudentMenuMixin, "_build_student_tutor_engine", return_value=fake_engine
            ), patch(
                "builtins.input", side_effect=["", "q"]
            ):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    mixin._hint_chat_flow()

            self.assertTrue(fake_engine.build_hint.called, "tutor was never invoked")
            req = fake_engine.build_hint.call_args[0][0]
            # Context must come from the persisted redacted stdout, not a dead
            # raw_output lookup — so it must equal the report's stdout value.
            self.assertEqual(req.raw_output, "AssertionError: expected 1 got 2")
            self.assertEqual(req.diagnosis, "incorrect_logic")
            self.assertEqual(req.failure_name, "test_x")
        finally:
            if prev_home is None:
                os.environ.pop("DUCKY_HOME", None)
            else:
                os.environ["DUCKY_HOME"] = prev_home


class StudentTutorEngineBackendSelectionTests(unittest.TestCase):
    """_build_student_tutor_engine on the live StudentMenuMixin must select
    the right tutor backend — teacher remote / ollama / lmstudio / none — and
    fall through gracefully when the teacher server is unreachable.

    These branches had NO live-path coverage: the only tests lived in the now-
    removed test_student_ui.py and targeted ducky.student_ui._build_student_tutor_engine,
    a duplicate copy. That duplication was the drift vector that produced the
    hint-chat raw_output bug — same logic in two places, one fixed, one not.
    Pinning the live mixin closes the gap so a future divergence is caught.
    """

    def _mixin(self):
        from ducky.gui.student import StudentMenuMixin

        return StudentMenuMixin()

    def test_no_backend_returns_none(self):
        with patch("ducky.config.load_config", return_value={"llm_backend": "none"}):
            with patch("ducky.student_client.StudentClient") as MockClient:
                MockClient.from_config.return_value = MagicMock(teacher_url="")
                engine = self._mixin()._build_student_tutor_engine()
                self.assertIsNone(engine)

    def test_teacher_client_returns_remote_engine(self):
        from ducky.tutor import TutorEngine

        with patch("ducky.config.load_config", return_value={"llm_backend": "none"}):
            with patch("ducky.student_client.StudentClient") as MockClient:
                client = MagicMock()
                client.teacher_url = "http://localhost:8420"
                client.status.return_value = {"ok": True}
                MockClient.from_config.return_value = client
                engine = self._mixin()._build_student_tutor_engine()
                self.assertIsInstance(engine, TutorEngine)

    def test_ollama_backend_returns_engine(self):
        config = {
            "llm_backend": "ollama",
            "ollama_model": "qwen2.5-coder:1.5b",
            "ollama_url": "http://localhost:11434",
        }
        with patch("ducky.config.load_config", return_value=config):
            with patch("ducky.student_client.StudentClient") as MockClient:
                MockClient.from_config.return_value = MagicMock(teacher_url="")
                engine = self._mixin()._build_student_tutor_engine()
                self.assertIsNotNone(engine)

    def test_lmstudio_backend_returns_engine(self):
        config = {
            "llm_backend": "lmstudio",
            "lmstudio_model": "default",
            "lmstudio_url": "http://localhost:1234",
        }
        with patch("ducky.config.load_config", return_value=config):
            with patch("ducky.student_client.StudentClient") as MockClient:
                MockClient.from_config.return_value = MagicMock(teacher_url="")
                engine = self._mixin()._build_student_tutor_engine()
                self.assertIsNotNone(engine)

    def test_teacher_unreachable_falls_through_to_none(self):
        with patch("ducky.config.load_config", return_value={"llm_backend": "none"}):
            with patch("ducky.student_client.StudentClient") as MockClient:
                client = MagicMock()
                client.teacher_url = "http://localhost:8420"
                client.status.side_effect = Exception("connection refused")
                MockClient.from_config.return_value = client
                engine = self._mixin()._build_student_tutor_engine()
                self.assertIsNone(engine)


class ClassroomConnectPersistenceTests(unittest.TestCase):
    """Characterization: both student connect entry points must persist the
    teacher connection (URL/token/name) so downstream flows — Pull updates,
    Submit, Assignments, Status, all built on ``StudentClient.from_config()``,
    which reads the config file — observe it afterward.

    ``_connect_flow`` (Student Mode → [c]) and ``_classroom_connect``
    (Classroom → [1]) both route through the shared ``_connect_to_teacher``
    helper → ``connect_workflow``. Historically these were two near-duplicate
    flows; consolidating them removed ~25 lines of duplicated input-gathering,
    dropped the write-only dead ``self.student_client`` state (set in
    _classroom_connect, init'd in app.py, never read anywhere), and unified
    inconsistent raw-print/student_error reporting into student widgets. These
    tests lock that both entry points still persist, so a future divergence of
    either path from the persisting workflow is caught.

    Patch target note: we patch ``ducky.workflows.StudentClient`` — the name
    ``connect_workflow`` actually resolves (module-level import at the top of
    workflows.py). Patching ``ducky.student_client.StudentClient`` instead is
    order-dependent: it only reaches ``connect_workflow`` if ``ducky.workflows``
    happens to be imported while the patch is active (otherwise workflows.py has
    already bound the real class).
    """

    def _terminal(self):
        from ducky.gui import DuckyTerminal

        app = object.__new__(DuckyTerminal)
        app._notifications = []
        return app

    def _isolated_home(self):
        import os
        import tempfile

        tmp = tempfile.mkdtemp()
        self._prev_home = os.environ.get("DUCKY_HOME")
        os.environ["DUCKY_HOME"] = tmp
        return tmp

    def _restore_home(self):
        import os

        prev = getattr(self, "_prev_home", None)
        if prev is None:
            os.environ.pop("DUCKY_HOME", None)
        else:
            os.environ["DUCKY_HOME"] = prev

    def _patch_connect_inputs(self, url="http://teacher:8420"):
        """One fake StudentClient whose connect() returns a success payload,
        plus an input() side-effect matching the connect flow's prompt order:
        url, 9-digit access code, name, then the trailing 'Press Enter'."""
        fake_client = MagicMock()
        fake_client.connect.return_value = {
            "class_name": "Algebra 2",
            "token": "tok_abc123",
            "server_version": "0.5.2",
        }
        return (
            patch("ducky.workflows.StudentClient", return_value=fake_client),
            patch(
                "builtins.input",
                side_effect=[url, "123456789", "Maya", ""],
            ),
            fake_client,
        )

    def test_classroom_connect_persists_teacher_connection(self):
        import io

        from ducky.config import load_config

        self._isolated_home()
        try:
            self.assertEqual(load_config().get("teacher_url", ""), "")
            p_client, p_input, _ = self._patch_connect_inputs()
            with p_client, p_input, redirect_stdout(io.StringIO()):
                self._terminal()._classroom_connect()

            cfg = load_config()
            self.assertEqual(cfg.get("teacher_url"), "http://teacher:8420")
            self.assertEqual(cfg.get("teacher_token"), "tok_abc123")
            self.assertEqual(cfg.get("student_name"), "Maya")
        finally:
            self._restore_home()

    def test_student_mode_connect_persists_teacher_connection(self):
        """Student Mode → [c] must also persist (locks the pre-existing result)."""
        import io

        from ducky.config import load_config

        self._isolated_home()
        try:
            self.assertEqual(load_config().get("teacher_url", ""), "")
            p_client, p_input, _ = self._patch_connect_inputs()
            with p_client, p_input, redirect_stdout(io.StringIO()):
                self._terminal()._connect_flow()

            cfg = load_config()
            self.assertEqual(cfg.get("teacher_url"), "http://teacher:8420")
            self.assertEqual(cfg.get("teacher_token"), "tok_abc123")
            self.assertEqual(cfg.get("student_name"), "Maya")
        finally:
            self._restore_home()


class StudentQuizGuiFlowTests(unittest.TestCase):
    """End-to-end behavioral tests for the rewritten student quiz GUI flow.

    The host renderer (``_quiz_flow`` / ``_quiz_prompt`` / ``_quiz_show_results``
    in ``ducky.gui.student``) was rewritten to reject invalid answers instead of
    silently advancing, to show progress, to accept letter selections, and to
    offer an in-flow retry. These tests drive the real flow with a real ``Quiz``
    object and assert on recorded answers + rendered text, not on mocks alone —
    so a regression (e.g. storing "99" as the answer) fails loudly.
    """

    def _make_quiz(self):
        from ducky.quiz import parse_quiz_dict

        data = {
            "quiz_id": "quiz.gui_test",
            "lesson_id": "gui_lesson",
            "title": "GUI Quiz",
            "description": "A quiz for GUI testing.",
            "questions": [
                {
                    "id": "q1",
                    "type": "multiple_choice",
                    "prompt": "What does range(3) produce?",
                    "options": ["[0,1,2,3]", "[0,1,2]", "[1,2,3]"],
                    "correct": "[0,1,2]",
                    "concept": "ranges",
                    "explanation": "range(n) yields 0..n-1.",
                },
                {
                    "id": "q2",
                    "type": "true_false",
                    "prompt": "Python is dynamically typed.",
                    "correct": "true",
                    "explanation": "Yes, types are checked at runtime.",
                },
                {
                    "id": "q3",
                    "type": "predict_output",
                    "prompt": "What does this print?",
                    "code": "print('hi')",
                    "correct": "hi",
                    "explanation": "Single line output.",
                },
                {
                    "id": "q4",
                    "type": "predict_output",
                    "prompt": "What does this print?",
                    "code": "pass",
                    "correct": "",
                    "explanation": "Nothing is printed.",
                },
            ],
        }
        return parse_quiz_dict(data)

    def _run_flow(
        self,
        inputs,
        quiz,
        store_stats=None,
        record_capture=None,
        attempt_history=None,
        draft=None,
    ):
        """Drive ``_quiz_flow`` with a real Quiz and a list of input() values.

        ``inputs`` is a single consumption-ordered list feeding every call to
        ``input()``/``student_prompt`` (both resolve to ``builtins.input``).

        ``store_stats`` is the dict returned by the faked ``QuizResultsStore.get_stats``;
        ``record_capture`` is an optional list that receives each recorded
        ``(quiz_id, lesson_id, result, answers)`` tuple. ``attempt_history``
        is an optional callable returning the prior-attempt records the faked
        ``QuizResultsStore.get_attempts`` should return (used to exercise
        the weak-spots summary without a real on-disk DB).
        """
        from pathlib import Path

        from ducky.gui.student import StudentMenuMixin

        mixin = StudentMenuMixin()
        stats = store_stats if store_stats is not None else {
            "attempts": 0, "best": 0, "last_correct": 0, "last_passed": False
        }

        fake_store = MagicMock()
        fake_store.get_stats.return_value = stats
        fake_store.get_draft.return_value = draft
        if attempt_history is not None:
            fake_store.get_attempts.side_effect = attempt_history
        if record_capture is not None:
            def _record(qid, lid, result, answers, **kwargs):
                record_capture.append((qid, lid, result, answers))
            fake_store.record.side_effect = _record

        with patch("ducky.gui.student.clear"), patch(
            "builtins.input", side_effect=inputs
        ), patch(
            "ducky.workflows.resolve_current_lesson",
            return_value={
                "lesson_id": "gui_lesson",
                "title": "GUI Lesson",
                "lesson_dir": Path("/tmp/gui_lesson"),
                "entry_path": Path("/tmp/gui_lesson/main.py"),
                "pack_path": None,
            },
        ), patch(
            "ducky.quiz.load_student_quiz", return_value=(quiz, False)
        ), patch(
            "ducky.quiz.QuizResultsStore", return_value=fake_store
        ), patch(
            "ducky.gui.student.StudentMenuMixin._maybe_submit_quiz_result"
        ):
            buf = io.StringIO()
            with redirect_stdout(buf):
                mixin._quiz_flow()
            return buf.getvalue()

    def test_parse_option_index_unit(self):
        from ducky.gui.student import StudentMenuMixin

        # numeric
        self.assertEqual(StudentMenuMixin._parse_option_index("1", 3), 0)
        self.assertEqual(StudentMenuMixin._parse_option_index("3", 3), 2)
        # UPPERCASE letters (lowercase b/q are intercepted as commands upstream)
        self.assertEqual(StudentMenuMixin._parse_option_index("A", 3), 0)
        self.assertEqual(StudentMenuMixin._parse_option_index("C", 3), 2)
        # the parser is case-insensitive: lowercase 'b' maps to index 1, and the
        # reserved-key interception (b=back, q=quit) is _quiz_prompt's job, not
        # the parser's — tested at the flow level.
        self.assertEqual(StudentMenuMixin._parse_option_index("b", 3), 1)
        # out of range -> None
        self.assertIsNone(StudentMenuMixin._parse_option_index("0", 3))
        self.assertIsNone(StudentMenuMixin._parse_option_index("4", 3))
        self.assertIsNone(StudentMenuMixin._parse_option_index("Z", 3))
        # garbage -> None
        self.assertIsNone(StudentMenuMixin._parse_option_index("", 3))
        self.assertIsNone(StudentMenuMixin._parse_option_index("xyz", 3))
        self.assertIsNone(StudentMenuMixin._parse_option_index("1.5", 3))

    def test_happy_path_all_correct_records_full_score(self):
        quiz = self._make_quiz()
        captured = []
        out = self._run_flow(
            # begin -> Q1 mc, Q2 tf, Q3 pred, Q4 pred(empty), then results q
            ["", "2", "true", "hi", "", "q"],
            quiz,
            record_capture=captured,
        )
        self.assertIn("Results", out)
        self.assertIn("passed", out)
        self.assertEqual(len(captured), 1)
        _qid, _lid, result, answers = captured[0]
        self.assertEqual(result.correct, 4)
        self.assertEqual(result.total, 4)
        self.assertTrue(result.passed)
        # answers stored as canonical option text / normalized values
        self.assertEqual(answers["q1"], "[0,1,2]")
        self.assertEqual(answers["q2"], "true")
        self.assertEqual(answers["q3"], "hi")
        self.assertEqual(answers["q4"], "")

    def test_bundled_practice_quiz_runs_without_a_lesson_workspace(self):
        """A fresh install can take practice before syncing a teacher pack."""
        from ducky.gui.student import StudentMenuMixin

        quiz = self._make_quiz()
        fake_store = MagicMock()
        fake_store.get_stats.return_value = {
            "attempts": 0,
            "best": 0,
            "last_correct": 0,
            "last_passed": False,
        }
        fake_store.get_attempts.return_value = []
        fake_store.get_draft.return_value = None
        with patch("ducky.gui.student.clear"), patch(
            "builtins.input", side_effect=["", "2", "true", "hi", "", "q"]
        ), patch("ducky.quiz.QuizResultsStore", return_value=fake_store), patch(
            "ducky.gui.student.StudentMenuMixin._maybe_submit_quiz_result"
        ):
            buf = io.StringIO()
            with redirect_stdout(buf):
                StudentMenuMixin()._quiz_flow(
                    {
                        "lesson_id": "practice.gui_test",
                        "title": quiz.title,
                        "workspace_dir": None,
                        "pack_file": None,
                        "is_general_quiz": True,
                    },
                    quiz=quiz,
                    teacher_assigned=False,
                )

        out = buf.getvalue()
        self.assertIn("Practice quiz", out)
        self.assertIn("Results", out)

    def test_invalid_mcq_rejected_stores_valid_not_garbage(self):
        """Core regression: '99' must not be stored as the answer."""
        quiz = self._make_quiz()
        captured = []
        out = self._run_flow(
            # begin, bogus '99' then valid '2', rest correct, results q
            ["", "99", "2", "true", "hi", "", "q"],
            quiz,
            record_capture=captured,
        )
        self.assertIn("capital letter", out)
        self.assertEqual(len(captured), 1)
        _qid, _lid, result, answers = captured[0]
        # the bogus '99' must NOT be recorded; valid option text must be.
        self.assertEqual(answers["q1"], "[0,1,2]")
        self.assertEqual(result.correct, 4)

    def test_letter_selection_accepted(self):
        quiz = self._make_quiz()
        captured = []
        # 'B' (uppercase) selects option 2 — lowercase 'b' means "back".
        self._run_flow(
            # begin, 'B' selects option 2, rest correct, results q
            ["", "B", "true", "hi", "", "q"],
            quiz,
            record_capture=captured,
        )
        self.assertEqual(captured[0][3]["q1"], "[0,1,2]")

    def test_empty_mcq_reprompted(self):
        quiz = self._make_quiz()
        captured = []
        out = self._run_flow(
            # begin, empty then valid '2', rest correct, results q
            ["", "", "2", "true", "hi", "", "q"],
            quiz,
            record_capture=captured,
        )
        self.assertIn("capital letter", out)
        self.assertEqual(captured[0][3]["q1"], "[0,1,2]")

    def test_back_navigation_at_first_question(self):
        quiz = self._make_quiz()
        captured = []
        out = self._run_flow(
            # begin, 'b' at Q1 (no-op notice + press-enter), re-answer all, results q
            ["", "b", "", "2", "true", "hi", "", "q"],
            quiz,
            record_capture=captured,
        )
        self.assertIn("first question", out)
        # still recorded once, full score
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0][2].correct, 4)

    def test_pause_records_nothing(self):
        quiz = self._make_quiz()
        captured = []
        self._run_flow(
            # begin, 'q' pauses before answering anything, then press enter
            ["", "q", ""],
            quiz,
            record_capture=captured,
        )
        self.assertEqual(len(captured), 0)

    def test_retry_records_two_attempts(self):
        quiz = self._make_quiz()
        captured = []
        self._run_flow(
            # attempt 1: begin, answer all, results -> 'r' retry
            ["", "2", "true", "hi", "", "r",
             # attempt 2: begin, answer all, results -> 'q' leave
             "", "2", "true", "hi", "", "q"],
            quiz,
            record_capture=captured,
        )
        self.assertEqual(len(captured), 2)
        for _qid, _lid, result, _answers in captured:
            self.assertEqual(result.correct, 4)

    def test_predicted_output_empty_accepted(self):
        quiz = self._make_quiz()
        captured = []
        self._run_flow(
            ["", "2", "true", "hi", "", "q"],
            quiz,
            record_capture=captured,
        )
        # q4 had correct == "" (program prints nothing) -> empty answer is correct
        self.assertEqual(captured[0][3]["q4"], "")
        self.assertEqual(captured[0][2].correct, 4)

    def test_progress_indicator_shown(self):
        quiz = self._make_quiz()
        out = self._run_flow(
            ["", "2", "true", "hi", "", "q"],
            quiz,
        )
        self.assertIn("answered", out)

    def test_question_map_allows_editing_an_earlier_answer(self):
        """Students can jump back instead of restarting a whole quiz."""
        quiz = self._make_quiz()
        captured = []
        out = self._run_flow(
            [
                "",          # begin
                "m", "2",    # map from Q1 -> Q2
                "true", "hi", "",  # answer Q2-Q4
                "m", "1", "2",     # map back to Q1 and answer it
                "q",          # leave results
            ],
            quiz,
            record_capture=captured,
        )
        self.assertIn("Quiz Question Map", out)
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0][2].correct, 4)

    def test_timer_is_visible_while_answering(self):
        quiz = self._make_quiz()
        quiz.time_limit_seconds = 60
        out = self._run_flow(
            ["", "2", "true", "hi", "", "q"],
            quiz,
        )
        self.assertIn("Time left", out)

    def test_predict_output_prompt_keeps_leading_spaces_from_real_input(self):
        """The actual TUI prompt must not strip meaningful output indentation."""
        from ducky.gui.student import StudentMenuMixin
        from ducky.quiz import QuizQuestion, grade_question

        question = QuizQuestion(
            id="q",
            type="predict_output",
            prompt="p",
            code="print('  hi')",
            correct="  hi",
        )
        with patch("builtins.input", return_value="  hi "):
            answer = StudentMenuMixin()._quiz_prompt(question)
        self.assertEqual(answer, "  hi ")
        self.assertTrue(grade_question(question, answer)[0])

    def test_adversarial_out_of_range_letter_reprompted(self):
        quiz = self._make_quiz()
        captured = []
        out = self._run_flow(
            # begin, 'Z' (out of range for 3 options) then valid '2', rest correct, results q
            ["", "Z", "2", "true", "hi", "", "q"],
            quiz,
            record_capture=captured,
        )
        # 'Z' must be rejected (the re-prompt error mentions letters)
        self.assertIn("letter", out)
        self.assertEqual(captured[0][3]["q1"], "[0,1,2]")

    def test_missed_questions_shown_on_results(self):
        quiz = self._make_quiz()
        captured = []
        out = self._run_flow(
            # begin, wrong mcq '1', wrong tf 'false', correct pred, correct empty, results q
            ["", "1", "false", "hi", "", "q"],
            quiz,
            record_capture=captured,
        )
        self.assertIn("missed", out)
        # The summary shows the correct answer inline (full explanations live in
        # the interactive 'v' review screen, not the summary).
        self.assertIn("[0,1,2]", out)
        # Regression: the at-a-glance results summary must surface the
        # teacher-authored explanation for a missed question (the CLI path does
        # this; the GUI summary used to omit it, forcing a drill-in to learn why).
        self.assertIn("Why:", out)
        self.assertIn("range(n) yields 0..n-1.", out)
        self.assertEqual(captured[0][2].correct, 2)  # only the two predict questions right

    def test_results_summary_shows_grader_detail_for_predict_output(self):
        """When a predict_output question is missed, the at-a-glance results
        summary must print the auto-grader's rationale (``detail``, e.g.
        'Expected output: …') — not just the correct answer. This is what the
        CLI path does; the GUI summary previously omitted ``detail``."""

        # q3 is predict_output; answer it wrong to force a 'detail' line.
        quiz = self._make_quiz()
        captured = []
        out = self._run_flow(
            # begin, correct mcq '2', correct tf, WRONG pred 'bye', correct empty, results q
            ["", "2", "true", "bye", "", "q"],
            quiz,
            record_capture=captured,
        )
        self.assertIn("missed", out)
        # The 'detail' for a missed prediction is "Expected output:\n<correct>".
        self.assertIn("Expected output:", out)
        self.assertIn("hi", out)  # the correct output value must appear
        self.assertEqual(captured[0][2].correct, 3)  # only q3 missed

    def test_review_missed_shows_correct_vs_selected(self):
        """The interactive review must contrast the student's answer with the
        correct one and surface the explanation. Walk through both missed
        questions (two 'n' to advance, then 'q' to finish)."""
        from ducky.gui.student import StudentMenuMixin
        from ducky.quiz import grade_quiz

        quiz = self._make_quiz()
        # Answer Q1 wrong, Q2 wrong, Q3/Q4 right.
        answers = {"q1": "[0,1,2,3]", "q2": "false", "q3": "hi", "q4": ""}
        result = grade_quiz(quiz, answers)

        mixin = StudentMenuMixin()
        with patch("ducky.gui.student.clear"), patch(
            "ducky.gui.student.student_prompt", side_effect=["n", "n", "q"]
        ), patch("ducky.gui.student.student_heading"):
            buf = io.StringIO()
            with redirect_stdout(buf):
                mixin._quiz_review_missed(quiz, result)
            out = buf.getvalue()

        # The Contrast: selected (wrong) shown next to the correct answer.
        self.assertIn("[0,1,2,3]", out)   # the wrong MCQ selection
        self.assertIn("[0,1,2]", out)     # the correct MCQ answer
        self.assertIn("false", out)       # wrong true/false
        self.assertIn("true", out)        # correct true/false
        self.assertIn("range(n) yields", out)  # explanation rendered

    def test_results_review_loop_then_leave(self):
        """Typing 'v' opens the review, and after it the student can choose 'q'
        to leave. Records exactly one attempt (the review does not re-grade)."""
        quiz = self._make_quiz()
        captured = []
        out = self._run_flow(
            # begin, wrong mcq '1', wrong tf 'false', correct pred/empty,
            # results screen -> 'v' review (finishes with its own 'q'), then 'q' leave
            ["", "1", "false", "hi", "", "v", "q", "q"],
            quiz,
            record_capture=captured,
        )
        self.assertIn("Review —", out)  # review screen was rendered
        self.assertEqual(len(captured), 1)  # only the original attempt recorded
        # results banner + chosen leave via 'q'
        self.assertIn("missed", out)

    def test_perfect_run_skips_review_option(self):
        """A perfect score shows no review prompt and accepts 'q' to leave."""
        quiz = self._make_quiz()
        captured = []
        out = self._run_flow(
            ["", "2", "true", "hi", "", "q"],
            quiz,
            record_capture=captured,
        )
        self.assertIn("Perfect run", out)
        self.assertNotIn("Review missed", out)
        self.assertEqual(captured[0][2].correct, 4)

    def test_results_typo_reprompts_instead_of_exiting(self):
        """A stray key at the results screen must re-prompt (only [q] leaves),
        so a later [v] still opens the review. Before the fix, any non-[r]/[v]
        key (e.g. a typo) silently returned from the results screen."""
        quiz = self._make_quiz()
        captured = []
        out = self._run_flow(
            # begin, wrong mcq '1', wrong tf 'false', correct pred/empty,
            # results -> typo 'x' (must NOT exit) -> 'v' review -> 'q' finish
            # review -> 'q' leave
            ["", "1", "false", "hi", "", "x", "v", "q", "q"],
            quiz,
            record_capture=captured,
        )
        self.assertIn("Review —", out)  # the typo did not abort the flow
        self.assertEqual(len(captured), 1)  # review is display-only


    def test_attempt_number_shown_and_increments(self):
        quiz = self._make_quiz()
        captured = []
        out = self._run_flow(
            # attempt 1 -> retry, attempt 2 -> leave; both full-score
            ["", "2", "true", "hi", "", "r",
             "", "2", "true", "hi", "", "q"],
            quiz,
            record_capture=captured,
        )
        # First attempt is labelled "Attempt 1" on the intro screen.
        self.assertIn("Attempt 1", out)
        # After a retry the second attempt is labelled "Attempt 2".
        self.assertIn("Attempt 2", out)
        self.assertEqual(len(captured), 2)

    def test_retry_puts_last_missed_question_first(self):
        """The default missed-first review mode should focus the next attempt
        on the question the student missed most recently."""
        quiz = self._make_quiz()

        def _attempts(_qid, _fingerprint=None):
            return [
                {
                    "correct": 3,
                    "total": 4,
                    "passed": False,
                    "answers": {
                        "q1": "[0,1,2,3]",
                        "q2": "true",
                        "q3": "hi",
                        "q4": "",
                    },
                    "taken_at": "2026-01-01T00:00:00+00:00",
                }
            ]

        out = self._run_flow(
            ["", "2", "true", "hi", "", "q"],
            quiz,
            store_stats={
                "attempts": 1,
                "best": 3,
                "last_correct": 3,
                "last_passed": False,
            },
            attempt_history=_attempts,
        )

        # Strip ANSI escape codes before asserting on rendered text.
        out_plain = re.sub(r"\x1b\[[0-9;]*m", "", out)
        self.assertIn("Review focus: 1 question(s) missed on your last attempt", out_plain)
        self.assertLess(
            out.index("What does range(3) produce?"),
            out.index("Python is dynamically typed."),
        )

    def test_matching_draft_resumes_at_saved_question(self):
        from ducky.quiz import QuizDraft

        quiz = self._make_quiz()
        captured = []
        draft = QuizDraft(
            quiz_id=quiz.quiz_id,
            lesson_id=quiz.lesson_id,
            quiz_fingerprint="matching",
            attempt_number=2,
            position=2,
            question_order=["q1", "q2", "q3", "q4"],
            answers={"q1": "[0,1,2]", "q2": "true"},
            updated_at="2026-01-01T00:00:00+00:00",
        )
        out = self._run_flow(
            ["", "hi", "", "q"],
            quiz,
            record_capture=captured,
            draft=draft,
        )

        self.assertIn("Resuming your saved quiz", out)
        self.assertIn("Question 3 of 4", out)
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0][2].correct, 4)

    def test_quiz_history_is_newest_first_and_hides_backend_id(self):
        from pathlib import Path

        from ducky.gui.student import StudentMenuMixin

        quiz = self._make_quiz()
        fake_store = MagicMock()
        fake_store.get_attempts.return_value = [
            {
                "correct": 2,
                "total": 4,
                "passed": False,
                "answers": {},
                "taken_at": "2026-01-01T10:00:00+00:00",
            },
            {
                "correct": 4,
                "total": 4,
                "passed": True,
                "answers": {},
                "taken_at": "2026-01-02T10:00:00+00:00",
            },
        ]
        mixin = StudentMenuMixin()
        with patch("ducky.gui.student.clear"), patch(
            "ducky.gui.student.student_prompt", return_value="q"
        ), patch(
            "ducky.workflows.resolve_current_lesson",
            return_value={
                "lesson_id": "gui_lesson",
                "title": "GUI Lesson",
                "lesson_dir": Path("/tmp/gui_lesson"),
                "pack_path": None,
            },
        ), patch(
            "ducky.quiz.load_student_quiz", return_value=(quiz, False)
        ), patch(
            "ducky.quiz.QuizResultsStore", return_value=fake_store
        ):
            buf = io.StringIO()
            with redirect_stdout(buf):
                mixin._quiz_history_flow()
            out = buf.getvalue()

        self.assertLess(out.index("2026-01-02"), out.index("2026-01-01"))
        self.assertIn("passed", out)
        self.assertNotIn("quiz.gui_test", out)

    def test_predict_output_prompt_preserves_leading_whitespace(self):
        """Leading spaces are meaningful in captured output and must reach the
        shared auto-grader instead of being removed by the TUI input helper."""
        from ducky.gui.student import StudentMenuMixin
        from ducky.quiz import QuizQuestion, grade_question

        question = QuizQuestion(
            id="q",
            type="predict_output",
            prompt="p",
            code="print('  hi')",
            correct="  hi",
        )
        mixin = StudentMenuMixin()
        with patch("ducky.gui.student.student_prompt", return_value="  hi"):
            answer = mixin._quiz_prompt(question)

        assert answer == "  hi"
        assert grade_question(question, answer)[0] is True

    def test_weak_spots_shown_after_repeat_misses(self):
        """GUI counterpart of ``ducky quiz --status`` weak spots: after >=2
        attempts where the student keeps missing Q1, the results screen must
        surface 'Weak spots' with the question and its explanation. This is the
        gap the GUI had — CLI users got weak spots, GUI users did not."""
        quiz = self._make_quiz()
        captured = []

        # Two prior attempts, both miss Q1 (MCQ) but get the rest right.
        prior = [
            {"q1": "[0,1,2,3]", "q2": "true", "q3": "hi", "q4": ""},
            {"q1": "[0,1,2,3]", "q2": "true", "q3": "hi", "q4": ""},
        ]

        # The flow patches QuizResultsStore with a MagicMock; configure its
        # get_attempts to return the re-parsed prior attempts so the helper
        # re-grades them against the current quiz (mirrors real persistence).
        def _attempts(qid, _fingerprint=None):
            return [
                {
                    "correct": 3,
                    "total": 4,
                    "passed": False,
                    "answers": a,
                    "taken_at": f"2026-01-0{i+1}T00:00:00+00:00",
                }
                for i, a in enumerate(prior)
            ]

        out = self._run_flow(
            # current attempt: answer everything right, then leave via 'q'
            ["", "2", "true", "hi", "", "q"],
            quiz,
            record_capture=captured,
            attempt_history=_attempts,
        )
        self.assertIn("Weak spots", out)
        # Q1 is the recurring miss and must carry its explanation.
        self.assertIn("Qq1" if False else "q1", out)
        self.assertIn("range(n) yields 0..n-1.", out)

    def test_weak_spots_hidden_after_single_attempt(self):
        """With only one prior attempt, weak spots must NOT render on the
        results screen — that run's misses are already listed per-question, so
        re-reporting them as 'weak spots' would be noisy double-reporting."""
        quiz = self._make_quiz()
        captured = []
        prior = [
            {"q1": "[0,1,2,3]", "q2": "true", "q3": "hi", "q4": ""},
        ]

        def _attempts(qid, _fingerprint=None):
            return [
                {
                    "correct": 3,
                    "total": 4,
                    "passed": False,
                    "answers": prior[0],
                    "taken_at": "2026-01-01T00:00:00+00:00",
                }
            ]

        out = self._run_flow(
            ["", "2", "true", "hi", "", "q"],
            quiz,
            record_capture=captured,
            attempt_history=_attempts,
        )
        self.assertNotIn("Weak spots", out)

    def test_timed_quiz_expires_and_records_unanswered_questions(self):
        quiz = self._make_quiz()
        quiz.time_limit_seconds = 60
        captured = []
        with patch("ducky.quiz.quiz_seconds_remaining", return_value=0):
            out = self._run_flow(["", "q"], quiz, record_capture=captured)
        self.assertIn("Time is up", out)
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0][2].correct, 0)
        self.assertEqual(captured[0][2].total, len(quiz.questions))
